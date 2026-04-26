#!/usr/bin/env python3
"""broker.py — launch rpcs3 on demand and expose a small HTTP API."""

import glob
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socket as _socket
import subprocess
import sys
import time
import zipfile as _zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock, Thread

# ── Config ────────────────────────────────────────────────────────────────────

PORT               = int(os.environ.get("BROKER_PORT", "8000"))
SECRET             = os.environ.get("BROKER_SECRET", "")
ROM_ROOT           = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
CACHE_DIR          = Path(os.environ.get("CACHE_DIR", "/config/rpcs3-cache"))
CACHE_MAX_GB       = float(os.environ.get("CACHE_MAX_GB", "0"))
RPCS3_BOOT_TIMEOUT = float(os.environ.get("RPCS3_BOOT_TIMEOUT", "60.0"))
XDOTOOL_TIMEOUT    = float(os.environ.get("XDOTOOL_TIMEOUT", "5.0"))
SAVE_WAIT          = float(os.environ.get("SAVE_WAIT", "30.0"))
SAVE_DIR           = Path(os.environ.get("SAVE_DIR", "/config/savestates"))

ENV = {
    "DISPLAY":            os.environ.get("DISPLAY", ":0"),
    "WAYLAND_DISPLAY":    os.environ.get("WAYLAND_DISPLAY", "wayland-1"),
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "HOME":               "/config",
    "USER":               "abc",
    "LD_PRELOAD":         "/usr/lib/selkies_joystick_interposer.so",
}

_XDOTOOL_ENV = {
    "DISPLAY":         ENV["DISPLAY"],
    "HOME":            "/config",
    "USER":            "abc",
    "XDG_RUNTIME_DIR": "/config/.XDG",
}

_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]

logging.basicConfig(
    level=getattr(logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "eboot_path":       None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
    "launch_status":    "idle",   # idle|evicting|extracting|launching|running|saving|error
    "launch_detail":    None,
    "launch_progress":  None,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_rom_path(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    if p.suffix.lower() not in (".zip", ".7z"):
        return None
    return p


def _pactl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        _PACTL_CMD + ["pactl"] + list(args),
        capture_output=True, text=True, timeout=5,
    )


def _pactl_get_mute() -> bool | None:
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        return None
    return result.stdout.strip().endswith("yes")


# ── Process management ────────────────────────────────────────────────────────

def _kill_rpcs3() -> None:
    with _lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None

    if proc is None or proc.poll() is not None:
        return

    log.info("Stopping rpcs3 (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("rpcs3 did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("rpcs3 did not exit after SIGKILL — giving up")
    except ProcessLookupError:
        pass


def _launch_rpcs3_internal(eboot_path: str | None) -> None:
    """Spawn rpcs3 as abc. Does not kill any existing instance — caller must do that."""
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "rpcs3", "--no-gui",
    ]
    if eboot_path:
        cmd.append(eboot_path)

    log.info("Launching rpcs3 (eboot=%s)", eboot_path or "library")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("Failed to launch rpcs3: %s", exc)
        with _lock:
            _session["process"] = None
            _session["is_managed"] = False
        return

    with _lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("rpcs3 launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()


def _monitor_process(proc, start_time: float) -> None:
    """Relaunch to library on unexpected rpcs3 exit."""
    proc.wait()
    duration = time.monotonic() - start_time

    with _lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        return

    wait_time = 5 if duration < 5 else 1
    log.info("rpcs3 exited after %.1fs — relaunching library in %ds", duration, wait_time)
    time.sleep(wait_time)

    with _lock:
        if not _session["is_managed"] or _session["process"] is not proc:
            return  # a new launch took over during the sleep — do nothing

    _return_to_library()


def _return_to_library() -> None:
    """Kill rpcs3, drain gamepads, relaunch to library, clear session state."""
    _kill_rpcs3()
    _drain_gamepad_sockets()
    time.sleep(1)
    with _lock:
        _session["rom_path"]        = None
        _session["rom_name"]        = None
        _session["eboot_path"]      = None
        _session["started_at"]      = None
        _session["launch_status"]   = "idle"
        _session["launch_detail"]   = None
        _session["launch_progress"] = None
    _launch_rpcs3_internal(None)


# ── Gamepad socket drain ──────────────────────────────────────────────────────

def _drain_gamepad_sockets() -> None:
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.debug("Socket drain: no gamepad sockets found.")
        return
    drained = removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
                s.shutdown(_socket.SHUT_WR)
            drained += 1
        except OSError:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    log.debug(
        "Socket drain: EOF to %d, removed %d (of %d total).",
        drained, removed, len(paths),
    )
