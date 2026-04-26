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


# ── Archive cache manager ─────────────────────────────────────────────────────

def _dir_size_bytes(path: Path) -> int:
    """Sum file sizes in a directory, excluding .last_accessed."""
    return sum(
        f.stat().st_size
        for f in path.rglob("*")
        if f.is_file() and f.name != ".last_accessed"
    )


def _cache_size_bytes() -> int:
    if not CACHE_DIR.is_dir():
        return 0
    return sum(_dir_size_bytes(d) for d in CACHE_DIR.iterdir() if d.is_dir())


def _touch_last_accessed(game_dir: Path) -> None:
    (game_dir / ".last_accessed").write_text(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )


def _evict_lru(needed_bytes: int, active_stem: str | None) -> None:
    """Evict least-recently-used games until needed_bytes fit within CACHE_MAX_GB."""
    if CACHE_MAX_GB <= 0:
        return
    max_bytes = int(CACHE_MAX_GB * 1024 ** 3)
    while True:
        current = _cache_size_bytes()
        if current + needed_bytes <= max_bytes:
            break
        candidates = []
        for game_dir in CACHE_DIR.iterdir():
            if not game_dir.is_dir() or game_dir.name == active_stem:
                continue
            la = game_dir / ".last_accessed"
            mtime = la.stat().st_mtime if la.exists() else 0
            size = _dir_size_bytes(game_dir)
            candidates.append((mtime, -size, game_dir))  # oldest first; largest on access-time tie
        if not candidates:
            log.warning("Cache: no evictable games — proceeding anyway")
            break
        candidates.sort()
        victim = candidates[0][2]
        log.info("Cache: evicting %s (LRU)", victim.name)
        shutil.rmtree(victim)


def _find_eboot(root: Path) -> Path | None:
    """Walk extracted tree and return the first EBOOT.BIN found."""
    for path in root.rglob("EBOOT.BIN"):
        return path
    return None


def _extract_zip(archive_path: str, dest: Path) -> None:
    """Extract ZIP archive to dest, updating launch_progress (0–100) per file."""
    with _zipfile.ZipFile(archive_path) as zf:
        members = zf.infolist()
        total = max(len(members), 1)
        for i, member in enumerate(members):
            zf.extract(member, dest)
            with _lock:
                _session["launch_progress"] = int((i + 1) / total * 100)


def _extract_7z(archive_path: str, dest: Path) -> None:
    """Extract 7z archive to dest, parsing -bsp1 stdout for launch_progress (0–100)."""
    cmd = ["7z", "x", "-bsp1", "-y", archive_path, f"-o{dest}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    buf = ""
    while True:
        chunk = proc.stdout.read(64)
        if not chunk:
            break
        buf += chunk
        parts = buf.split("\r")
        buf = parts[-1]
        for part in parts[:-1]:
            m = re.search(r"(\d+)%", part)
            if m:
                with _lock:
                    _session["launch_progress"] = int(m.group(1))
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"7z exited with code {proc.returncode}")


def _scan_cache() -> dict:
    """Return cache inventory: {stem: {path, eboot, size_bytes, last_accessed}}."""
    result = {}
    if not CACHE_DIR.is_dir():
        return result
    for game_dir in CACHE_DIR.iterdir():
        if not game_dir.is_dir():
            continue
        eboot = _find_eboot(game_dir)
        la = game_dir / ".last_accessed"
        result[game_dir.name] = {
            "path":          str(game_dir),
            "eboot":         str(eboot) if eboot else None,
            "size_bytes":    _dir_size_bytes(game_dir),
            "last_accessed": la.read_text().strip() if la.exists() else None,
        }
    return result


# ── Launch flow ───────────────────────────────────────────────────────────────

def _wait_for_rpcs3_window(timeout: float) -> bool:
    """Poll until rpcs3 process is running or timeout. Returns True if found."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.check_output(["pgrep", "-x", "rpcs3"], text=True)
            return True
        except subprocess.CalledProcessError:
            pass
        time.sleep(1.0)
    return False


def _finish_launch() -> None:
    """Wait for rpcs3 process to appear, then mark session as running."""
    if _wait_for_rpcs3_window(RPCS3_BOOT_TIMEOUT):
        log.info("rpcs3 window detected — marking running")
    else:
        log.warning("rpcs3 not detected within %.0fs — marking running anyway", RPCS3_BOOT_TIMEOUT)
    with _lock:
        _session["launch_status"] = "running"
        _session["launch_detail"] = None


def _do_launch(rom_path: str) -> None:
    """Background thread: kill current rpcs3, evict LRU, extract archive, launch."""
    archive = Path(rom_path)
    stem = archive.stem
    game_dir = CACHE_DIR / stem

    # Kill before extraction so stream shows progress overlay, not stale game
    _kill_rpcs3()
    _drain_gamepad_sockets()

    # Cache hit
    if game_dir.is_dir():
        eboot = _find_eboot(game_dir)
        if eboot:
            log.info("Cache hit: %s", stem)
            _touch_last_accessed(game_dir)
            with _lock:
                _session["rom_path"]        = rom_path
                _session["rom_name"]        = stem
                _session["eboot_path"]      = str(eboot)
                _session["started_at"]      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _session["launch_status"]   = "launching"
                _session["launch_detail"]   = "Starting rpcs3…"
                _session["launch_progress"] = None
            time.sleep(1)
            _launch_rpcs3_internal(str(eboot))
            _finish_launch()
            return
        log.warning("Cache dir exists but no EBOOT.BIN — re-extracting")
        shutil.rmtree(game_dir)

    # LRU eviction
    if CACHE_MAX_GB > 0:
        with _lock:
            _session["launch_status"]   = "evicting"
            _session["launch_detail"]   = "Freeing cache space…"
            _session["launch_progress"] = None
        estimated = int(archive.stat().st_size * 1.1)
        _evict_lru(estimated, stem)

    # Extraction
    with _lock:
        _session["launch_status"]   = "extracting"
        _session["launch_detail"]   = "Extracting game files…"
        _session["launch_progress"] = 0

    game_dir.mkdir(parents=True, exist_ok=True)
    try:
        if archive.suffix.lower() == ".zip":
            _extract_zip(rom_path, game_dir)
        else:
            _extract_7z(rom_path, game_dir)
        with _lock:
            _session["launch_progress"] = 100
    except Exception as exc:
        log.error("Extraction failed: %s", exc)
        shutil.rmtree(game_dir, ignore_errors=True)
        with _lock:
            _session["launch_status"]   = "error"
            _session["launch_detail"]   = f"Extraction failed: {exc}"
            _session["launch_progress"] = None
        return

    eboot = _find_eboot(game_dir)
    if eboot is None:
        log.error("No EBOOT.BIN found in %s", game_dir)
        shutil.rmtree(game_dir, ignore_errors=True)
        with _lock:
            _session["launch_status"]   = "error"
            _session["launch_detail"]   = "No EBOOT.BIN found in archive"
            _session["launch_progress"] = None
        return

    _touch_last_accessed(game_dir)

    with _lock:
        _session["rom_path"]        = rom_path
        _session["rom_name"]        = stem
        _session["eboot_path"]      = str(eboot)
        _session["started_at"]      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _session["launch_status"]   = "launching"
        _session["launch_detail"]   = "Starting rpcs3…"
        _session["launch_progress"] = None

    time.sleep(1)
    _launch_rpcs3_internal(str(eboot))
    _finish_launch()
