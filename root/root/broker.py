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
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

# ── Config ────────────────────────────────────────────────────────────────────

PORT               = int(os.environ.get("BROKER_PORT", "8000"))
SECRET             = os.environ.get("BROKER_SECRET", "")
ROM_ROOT           = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()
CACHE_DIR          = Path(os.environ.get("CACHE_DIR", "/config/rpcs3-cache"))
CACHE_MAX_GB       = float(os.environ.get("CACHE_MAX_GB", "0"))
RPCS3_BOOT_TIMEOUT = float(os.environ.get("RPCS3_BOOT_TIMEOUT", "60.0"))

# rpcs3's stdout/stderr is redirected at fd level to this file. A Python
# reader thread on a PIPE is not used: if the reader dies, rpcs3 blocks
# forever once the 64 KB pipe buffer fills.
RPCS3_LOG_PATH = Path(os.environ.get("RPCS3_LOG_PATH", "/config/rpcs3.log"))
_LOG_UID = int(os.environ.get("PUID", "1000"))
_LOG_GID = int(os.environ.get("PGID", "1000"))
XDOTOOL_TIMEOUT    = float(os.environ.get("XDOTOOL_TIMEOUT", "5.0"))
SAVE_WAIT          = float(os.environ.get("SAVE_WAIT", "30.0"))
SAVE_DIR           = Path(os.environ.get("SAVE_DIR", "/config/.config/rpcs3/savestates"))

ENV = {
    "DISPLAY":            os.environ.get("DISPLAY", ":0"),
    "WAYLAND_DISPLAY":    os.environ.get("WAYLAND_DISPLAY", "wayland-1"),
    "XDG_RUNTIME_DIR":    "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "HOME":               "/config",
    "USER":               "abc",
    # The joystick interposer hooks open() on /dev/input/* and redirects to
    # selkies Unix sockets. The fake libudev must be preloaded alongside it:
    # /dev/input is empty in the container, so udev-based enumeration (rpcs3's
    # Evdev pad handler) finds no devices without it and the virtual pads are
    # never opened.
    "LD_PRELOAD":         os.environ.get("LD_PRELOAD")
                          or "/usr/lib/selkies_joystick_interposer.so:/opt/lib/libudev.so.1.0.0-fake",
}

# Wayland key injection (wtype) — used for save/load state hotkeys.
# rpcs3 runs under the labwc Wayland compositor; xdotool (X11) cannot
# address Wayland-native windows.  wtype sends to the focused window,
# which in a single-app streaming session is always rpcs3.
_WTYPE_ENV = {
    "WAYLAND_DISPLAY": ENV["WAYLAND_DISPLAY"],
    "XDG_RUNTIME_DIR": "/config/.XDG",
    "HOME":            "/config",
    "USER":            "abc",
}

_WTYPE_CMD = (
    ["sudo", "-u", "abc", "env"]
    + [f"{k}={v}" for k, v in _WTYPE_ENV.items()]
    + ["wtype"]
)

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
_crash_count = 0          # rapid-crash counter; resets on any launch lasting > 10s
_MAX_CRASHES  = 5         # stop auto-relaunching after this many rapid crashes
_session: dict = {
    "process":           None,
    "rom_path":          None,
    "rom_name":          None,
    "eboot_path":        None,
    "started_at":        None,
    "is_managed":        False,
    "save_in_progress":  False,
    "launch_in_progress": False,  # guards against concurrent /launch extractions
    "launch_status":     "idle",   # idle|evicting|extracting|launching|running|saving|error
    "launch_detail":     None,
    "launch_progress":   None,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

# What rpcs3 can be pointed at, best first: a folder holding several
# candidates picks by this order. A decrypted .iso wins because it boots in
# place with no extraction at all, and .7z beats .zip because a PS3 game
# compresses far better with it, so there is less to unpack.
ROM_EXTENSIONS = (".iso", ".7z", ".zip", ".rar")

# Where to look below a game folder. The folder itself first, then one level
# down. Nothing deeper: a launch must not pay for a full walk of a large set,
# and anything further down is extras, not the game.
_ROM_SEARCH_GLOBS = ("*", "*/*")

# "Disc 1", "(Disc 2)", "CD1", "Disk_3" in a folder or file name. The leading
# boundary keeps it off words that merely end in the letters, so "abcd2.iso" is
# not read as disc 2.
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _disc_number(rel: Path) -> int:
    """Disc number named anywhere in `rel`, or 1 when nothing names one.

    Unmarked files count as disc 1 so that a single-disc game ranks level with
    the first disc of a set, and so a false positive can only ever mean "first".
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _validate_rom_path(raw: str) -> Path | None:
    """Resolve raw to an absolute path and confirm it lives under ROM_ROOT.

    Whether the path is something rpcs3 can actually boot is settled later, by
    _resolve_rom_file: a game folder carries no extension to check here, and
    deciding it at this stage would report a folder-organized game as a 400
    ("outside the library") when it is really a 422 ("nothing bootable here").
    """
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


def _resolve_rom_path(p: Path) -> Path | None:
    """Return an existing path, trying roms/ prefix alternatives.

    RomM's full_path may or may not include a leading roms/ component depending
    on library structure and when the ROM was scanned.  If the exact path
    doesn't exist, try stripping or inserting one roms/ level under ROM_ROOT.
    """
    log.debug("resolve_rom_path: checking %s", p)
    if p.exists():
        log.debug("resolve_rom_path: exact path exists")
        return p
    rel = p.relative_to(ROM_ROOT)
    parts = rel.parts
    candidates = []
    if parts and parts[0] == "roms":
        candidates.append(ROM_ROOT.joinpath(*parts[1:]))
    else:
        candidates.append(ROM_ROOT / "roms" / rel)
    for c in candidates:
        log.debug("resolve_rom_path: trying candidate %s", c)
        if c.is_relative_to(ROM_ROOT) and c.exists():
            log.debug("resolve_rom_path: candidate exists, using %s", c)
            return c
    log.warning("resolve_rom_path: no file found for %s (tried %s)", p, candidates)
    return None


def _resolve_rom_file(path: Path) -> Path | None:
    """Return what rpcs3 should be pointed at for `path`, or None if there is
    nothing bootable there.

    RomM addresses a folder-organized game by its folder: `Rom.full_path` is
    `fs_path/fs_name`, and for a multi-file ROM `fs_name` is the directory. So
    /launch regularly receives something like `.../roms/ps3/Demon's Souls` for
    a library laid out one game per folder.

    Three shapes come back, and the launch flow branches on all three:
      * a file, when the exact archive or .iso was addressed;
      * the folder itself, when it already holds a decrypted tree, which boots
        in place with no extraction and no cache entry;
      * an archive found inside the folder, extracted and cached as usual.
    """
    if path.is_file():
        return path if path.suffix.lower() in ROM_EXTENSIONS else None
    if not path.is_dir():
        return None
    # An already-decrypted game on the share needs no unpacking, so it beats
    # any archive sitting beside it.
    if _find_boot_target(path) is not None:
        return path
    # Every level is collected before anything is ranked. Taking the first level
    # that merely yields a match would let an extras file with a bootable
    # extension beat the real game one level down.
    candidates: list[Path] = []
    for pattern in _ROM_SEARCH_GLOBS:
        try:
            candidates.extend(path.glob(pattern))
        except OSError:
            # Libraries are routinely NFS mounts, so a stalled or vanished
            # share surfaces here as an OSError mid-walk. Report it as "no
            # bootable file" rather than 500-ing the launch.
            return None
    return _pick_rom_file(candidates, path)


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Path | None:
    """Best bootable file among `candidates`, all of them somewhere under `base`.

    Ranked by disc number, then format, then depth, then name:

      * disc first, so a set starts on disc 1 whatever format the later discs
        are in. Comparing the numbers also keeps 'Disc 2' ahead of 'Disc 10',
        which sorting the names as text does not.
      * format next, because among candidates for the same disc it decides
        which archive to unpack.
      * then depth, so the archive sitting in the game folder wins over one
        buried in an extras subfolder.
    """
    ranked: list[tuple[int, int, int, str, Path]] = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            # A symlink in the folder must not walk the launch out of
            # ROM_ROOT: _validate_rom_path only vetted the folder itself.
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts),
             p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance.

    A hung or missing pactl is reported as a non-zero CompletedProcess (rather
    than raising) so the /volume and /mute handlers return a 500 instead of
    dropping the connection with an unhandled exception."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        log.error("pactl timed out: %s", " ".join(args))
        return subprocess.CompletedProcess(cmd, 124, "", "pactl timed out")
    except OSError as exc:
        log.error("pactl failed to run: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _pactl_get_mute() -> bool | None:
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        log.error("pactl get-sink-mute failed (rc=%s): %s",
                  result.returncode, result.stderr.strip())
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
        broker_pgid = os.getpgrp()
        if pgid == broker_pgid:
            # Safety guard: don't killpg if rpcs3 somehow shares our process group.
            log.warning(
                "rpcs3 pgid %d == broker pgid — using SIGTERM to PID only", pgid
            )
            proc.send_signal(signal.SIGTERM)
        else:
            os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("rpcs3 did not exit after SIGTERM — sending SIGKILL")
            if pgid != broker_pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.send_signal(signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("rpcs3 did not exit after SIGKILL — giving up")
    except ProcessLookupError:
        pass


def _launch_rpcs3_internal(eboot_path: str | None) -> bool:
    """Spawn rpcs3 as abc. Does not kill any existing instance — caller must do that.

    Returns True if the process was spawned, False if Popen failed. Callers must
    not mark the session "running" on a False return."""
    global _crash_count
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "/opt/rpcs3/AppRun",
    ]
    if eboot_path:
        # --no-gui requires a boot target; library mode uses the full GUI
        cmd += ["--no-gui", eboot_path]

    log.info("Launching rpcs3 (boot=%s)", eboot_path or "library")

    # Append mode keeps history across launches. Failure to open is non-fatal:
    # rpcs3 still launches, just without captured output.
    log_fh = None
    try:
        log_fh = open(RPCS3_LOG_PATH, "ab", buffering=0)
        try:
            os.chown(RPCS3_LOG_PATH, _LOG_UID, _LOG_GID)
        except (OSError, PermissionError):
            pass
        log_fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch (boot={eboot_path or 'library'}) ===\n".encode())
        log_fh.flush()
    except OSError as exc:
        log.warning("Cannot open %s for rpcs3 output capture (%s); continuing without capture.", RPCS3_LOG_PATH, exc)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
    except Exception as exc:
        log.error("Failed to launch rpcs3: %s", exc)
        with _lock:
            _session["process"] = None
            _session["is_managed"] = False
        return False
    finally:
        # Popen dup'd the fd; close our handle so it isn't leaked.
        if log_fh:
            log_fh.close()

    with _lock:
        _session["process"] = proc
        _session["is_managed"] = True
        _crash_count = 0
    log.info("rpcs3 launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()
    return True


def _monitor_process(proc, start_time: float) -> None:
    """Relaunch to library on unexpected rpcs3 exit."""
    global _crash_count
    proc.wait()
    duration = time.monotonic() - start_time
    exit_code = proc.returncode
    log.info("rpcs3 exited (code=%s) after %.1fs", exit_code, duration)

    with _lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        return

    rapid = duration < 10
    with _lock:
        if rapid:
            _crash_count += 1
        else:
            _crash_count = 0
        crashes = _crash_count

    if crashes >= _MAX_CRASHES:
        log.error(
            "rpcs3 crashed %d times rapidly — stopping auto-relaunch. "
            "Check firmware installation and container logs.",
            crashes,
        )
        with _lock:
            _session["is_managed"]   = False
            _session["launch_status"] = "error"
            _session["launch_detail"] = (
                f"rpcs3 crashed {crashes} times rapidly — "
                "check firmware and container logs"
            )
        return

    wait_time = 5 if rapid else 1
    log.info("rpcs3 exited after %.1fs — relaunching library in %ds (crash #%d)", duration, wait_time, crashes)
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
    total = 0
    for f in path.rglob("*"):
        if f.is_file() and f.name != ".last_accessed":
            try:
                total += f.stat().st_size
            except FileNotFoundError:
                pass
    return total


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
    current = _cache_size_bytes()  # single upfront scan; updated incrementally
    while current + needed_bytes > max_bytes:
        candidates = []
        for game_dir in CACHE_DIR.iterdir():
            if not game_dir.is_dir() or game_dir.name == active_stem:
                continue
            la = game_dir / ".last_accessed"
            mtime = la.stat().st_mtime if la.exists() else 0
            size = _dir_size_bytes(game_dir)
            candidates.append((mtime, -size, size, game_dir))  # oldest first; largest on access-time tie
        if not candidates:
            log.warning("Cache: no evictable games — proceeding anyway")
            break
        candidates.sort()
        _, _, victim_size, victim = candidates[0]
        log.info("Cache: evicting %s (LRU)", victim.name)
        shutil.rmtree(victim)
        current -= victim_size


def _find_boot_target(root: Path) -> Path | None:
    """Return the best boot target for rpcs3 --no-gui in the extracted game tree.

    Priority:
    1. EBOOT.BIN — works reliably with --no-gui for both disc dumps and
       digital/PKG-installed games.  rpcs3 resolves the disc context from the
       parent directory structure, so passing the EBOOT.BIN path is sufficient.
    2. Decrypted ISO image (.iso) — for archives that contain a bare ISO rather
       than an extracted JB folder.
    """
    for eboot in root.rglob("EBOOT.BIN"):
        return eboot
    for iso in root.rglob("*.iso"):
        return iso
    return None


def _extract_zip(archive_path: str, dest: Path) -> None:
    """Extract ZIP archive; poll output-dir size for launch_progress (0–99).

    Per-file progress via ZipInfo.file_size looks right for multi-file archives
    but blocks the whole loop on a single large file (e.g. a 14 GB .iso inside
    the zip).  Instead we run extractall() in a thread and poll du -sb from the
    main thread — identical to the 7z strategy and accurate for any layout.
    """
    log.info("Extracting %s (zip)", Path(archive_path).name)

    # Read central directory upfront to get total uncompressed bytes.
    with _zipfile.ZipFile(archive_path) as zf:
        total_bytes = max(1, sum(m.file_size for m in zf.infolist()))

    exc_holder: list[BaseException | None] = [None]

    def _do_extract() -> None:
        try:
            with _zipfile.ZipFile(archive_path) as zf2:
                zf2.extractall(dest)
        except Exception as exc:
            exc_holder[0] = exc

    t = Thread(target=_do_extract, daemon=True)
    t.start()
    while t.is_alive():
        try:
            r = subprocess.run(
                ["du", "-sb", str(dest)],
                capture_output=True, text=True, timeout=10,
            )
            extracted = int(r.stdout.split()[0]) if r.returncode == 0 else 0
        except Exception:
            extracted = 0
        with _lock:
            _session["launch_progress"] = min(99, int(extracted / total_bytes * 100))
        t.join(timeout=3)  # sleep 3 s then re-check

    if exc_holder[0]:
        raise exc_holder[0]
    log.info("Extraction complete: %s", Path(archive_path).name)


def _extract_7z(archive_path: str, dest: Path) -> None:
    """Extract 7z/rar archive; poll output-dir size for launch_progress.

    7z suppresses its progress output when stdout is not a TTY, so parsing
    -bsp1 stdout is unreliable.  Instead we run 7z with all output discarded
    and track progress by comparing the growing output directory to an
    estimated uncompressed total (compressed × 3 — conservative for PS3 games).
    Progress is reported 0–99 during extraction; the caller sets 100 on success.
    """
    archive = Path(archive_path)
    log.info("Extracting %s", archive.name)
    cmd = ["7z", "x", "-y", archive_path, f"-o{dest}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        est_total = max(1, int(archive.stat().st_size * 3))
    except OSError:
        est_total = 1

    while proc.poll() is None:
        try:
            r = subprocess.run(
                ["du", "-sb", str(dest)],
                capture_output=True, text=True, timeout=10,
            )
            extracted = int(r.stdout.split()[0]) if r.returncode == 0 else 0
        except Exception:
            extracted = 0
        with _lock:
            _session["launch_progress"] = min(99, int(extracted / est_total * 100))
        time.sleep(3)

    if proc.returncode != 0:
        raise RuntimeError(f"7z exited with code {proc.returncode}")
    log.info("Extraction complete: %s", archive.name)


def _scan_cache() -> dict:
    """Return cache inventory: {stem: {path, eboot, size_bytes, last_accessed}}."""
    result = {}
    if not CACHE_DIR.is_dir():
        return result
    for game_dir in CACHE_DIR.iterdir():
        if not game_dir.is_dir():
            continue
        eboot = _find_boot_target(game_dir)
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
    """Poll until rpcs3 process (AppRun.wrapped) is running or timeout."""
    # rpcs3 is installed as an AppDir; the actual process name is AppRun.wrapped
    # (AppRun execs AppRun.wrapped, which is a symlink to usr/bin/rpcs3).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.check_output(["pgrep", "-x", "AppRun.wrapped"], text=True)
            return True
        except subprocess.CalledProcessError:
            pass
        time.sleep(1.0)
    return False


def _finish_launch(launched: bool) -> None:
    """Wait for rpcs3 process to appear, then mark session as running.

    If the spawn itself failed (launched=False), mark the session as errored
    rather than waiting out the boot timeout and falsely reporting "running"."""
    if not launched:
        log.error("rpcs3 failed to spawn — marking launch error")
        with _lock:
            _session["launch_status"]   = "error"
            _session["launch_detail"]   = "Failed to start rpcs3 — see container logs"
            _session["launch_progress"] = None
        return
    if _wait_for_rpcs3_window(RPCS3_BOOT_TIMEOUT):
        log.info("rpcs3 window detected — marking running")
    else:
        log.warning("rpcs3 not detected within %.0fs — marking running anyway", RPCS3_BOOT_TIMEOUT)
    with _lock:
        _session["launch_status"] = "running"
        _session["launch_detail"] = None


def _do_launch(rom_path: str) -> None:
    """Background thread entry: run the launch flow, surface any failure via
    launch_status, and always release the launch_in_progress guard."""
    try:
        _do_launch_inner(rom_path)
    except Exception as exc:
        log.exception("Launch failed: %s", exc)
        with _lock:
            _session["launch_status"]   = "error"
            _session["launch_detail"]   = f"Launch failed: {exc}"
            _session["launch_progress"] = None
    finally:
        with _lock:
            _session["launch_in_progress"] = False


def _do_launch_inner(rom_path: str) -> None:
    """Kill current rpcs3, evict LRU, extract archive, launch."""
    archive = Path(rom_path)
    stem = archive.stem
    game_dir = CACHE_DIR / stem

    # Kill before extraction so stream shows progress overlay, not stale game
    _kill_rpcs3()
    _drain_gamepad_sockets()

    # A game folder already holding a decrypted tree boots straight off the
    # library share. Copying it into the cache would double the space a game
    # costs and buy nothing, since the share is where it already lives, so the
    # LRU never sees these launches.
    if archive.is_dir():
        boot_target = _find_boot_target(archive)
        if boot_target is None:
            log.error("No boot target found in %s", archive)
            with _lock:
                _session["launch_status"]   = "error"
                _session["launch_detail"]   = "No boot target found — the folder must hold a decrypted EBOOT.BIN or a decrypted ISO"
                _session["launch_progress"] = None
            return
        log.info("Booting %s in place (boot target: %s)", archive.name, boot_target.name)
        with _lock:
            _session["rom_path"]        = rom_path
            _session["rom_name"]        = archive.name
            _session["eboot_path"]      = str(boot_target)
            _session["started_at"]      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _session["launch_status"]   = "launching"
            _session["launch_detail"]   = "Starting rpcs3…"
            _session["launch_progress"] = None
        time.sleep(1)
        _finish_launch(_launch_rpcs3_internal(str(boot_target)))
        return

    # Direct ISO: boot in-place — no extraction or caching needed.
    # The ISO must be decrypted (EBOOT.BIN inside starts with SCE magic);
    # encrypted ISOs from disc rips will fail inside rpcs3 with "invalid file".
    if archive.suffix.lower() == ".iso":
        log.info("Direct ISO boot: %s", archive.name)
        with _lock:
            _session["rom_path"]        = rom_path
            _session["rom_name"]        = stem
            _session["eboot_path"]      = rom_path
            _session["started_at"]      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _session["launch_status"]   = "launching"
            _session["launch_detail"]   = "Starting rpcs3…"
            _session["launch_progress"] = None
        time.sleep(1)
        _finish_launch(_launch_rpcs3_internal(rom_path))
        return

    # Cache hit
    if game_dir.is_dir():
        eboot = _find_boot_target(game_dir)
        if eboot:
            log.info("Cache hit: %s (boot target: %s)", stem, eboot.name)
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
            _finish_launch(_launch_rpcs3_internal(str(eboot)))
            return
        log.warning("Cache dir exists but no boot target found — re-extracting")
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

    boot_target = _find_boot_target(game_dir)
    if boot_target is None:
        log.error("No boot target found in %s", game_dir)
        shutil.rmtree(game_dir, ignore_errors=True)
        with _lock:
            _session["launch_status"]   = "error"
            _session["launch_detail"]   = "No boot target found — archive must contain a decrypted EBOOT.BIN or a decrypted ISO"
            _session["launch_progress"] = None
        return
    eboot = boot_target

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
    _finish_launch(_launch_rpcs3_internal(str(eboot)))


# ── Save states ───────────────────────────────────────────────────────────────

def _sstate_snapshot() -> dict:
    """Return {Path: (size, mtime)} for every .SAVESTAT.zst file in SAVE_DIR."""
    if not SAVE_DIR.is_dir():
        return {}
    snap = {}
    for p in SAVE_DIR.rglob("*.SAVESTAT.zst"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _wait_for_sstate_write(before: dict, deadline: float) -> bool:
    """Poll SAVE_DIR until a .savestate write completes or deadline is reached."""
    STABLE_SECS  = 0.5
    POLL_SECS    = 0.1
    start        = time.monotonic()
    target       = None
    last_size    = None
    stable_since = None

    while time.monotonic() < deadline:
        after = _sstate_snapshot()

        if target is None:
            for p, (size, mtime) in after.items():
                prev = before.get(p)
                if prev is None or prev[1] != mtime:
                    target       = p
                    last_size    = size
                    stable_since = time.monotonic()
                    log.debug("Save: write detected — %s (%d bytes)", p.name, size)
                    break
        else:
            cur = after.get(target)
            if cur is None:
                target = None
            else:
                cur_size = cur[0]
                if cur_size != last_size:
                    last_size    = cur_size
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= STABLE_SECS:
                    log.info(
                        "Save state write complete — %s (%d bytes) in %.1fs",
                        target.name, last_size, time.monotonic() - start,
                    )
                    return True

        time.sleep(POLL_SECS)
    return False


def _rpcs3_is_running() -> bool:
    """Return True if the rpcs3 AppDir process (AppRun.wrapped) is alive."""
    try:
        subprocess.check_output(["pgrep", "-x", "AppRun.wrapped"], text=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _wtype_save_state() -> bool:
    """Send Ctrl+S to the focused Wayland window (rpcs3) and wait for write."""
    if not _rpcs3_is_running():
        log.error("wtype: rpcs3 process not found")
        return False

    before = _sstate_snapshot()
    try:
        subprocess.run(
            _WTYPE_CMD + ["-M", "ctrl", "-k", "s", "-m", "ctrl"],
            timeout=XDOTOOL_TIMEOUT, check=True,
        )
    except Exception as exc:
        log.error("wtype: ctrl+s failed: %s", exc)
        return False

    log.info("wtype: ctrl+s sent — waiting for .SAVESTAT.zst write (max %.1fs)", SAVE_WAIT)
    if not _wait_for_sstate_write(before, time.monotonic() + SAVE_WAIT):
        log.warning("wtype: save state write not confirmed within %.1fs", SAVE_WAIT)
        return False
    return True


def _wtype_load_state() -> bool:
    """Send Ctrl+L to the focused Wayland window (rpcs3) to load the save state."""
    if not _rpcs3_is_running():
        log.error("wtype: rpcs3 process not found")
        return False

    try:
        subprocess.run(
            _WTYPE_CMD + ["-M", "ctrl", "-k", "l", "-m", "ctrl"],
            timeout=XDOTOOL_TIMEOUT, check=True,
        )
        log.info("wtype: ctrl+l sent")
        return True
    except Exception as exc:
        log.error("wtype: ctrl+l failed: %s", exc)
        return False


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(self.headers.get("X-Broker-Secret", ""), SECRET)

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", 0)), 64 * 1024))
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        # /health stays open for container healthchecks; all other GETs
        # require the shared secret, matching POST/DELETE.
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/status":
            with _lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                )
                snap = dict(_session)
            # Cache stats are omitted here — use GET /cache for that.
            # Scanning the cache dir on every 2-second status poll is expensive
            # (rglob across a directory that may be actively written during extraction).
            self._send_json(200, {
                "active":          active,
                "rom_path":        snap["rom_path"],
                "rom_name":        snap["rom_name"],
                "eboot_path":      snap["eboot_path"],
                "started_at":      snap["started_at"],
                "launch_status":   snap["launch_status"],
                "launch_detail":   snap["launch_detail"],
                "launch_progress": snap["launch_progress"],
            })
            return

        if self.path == "/cache":
            cache = _scan_cache()
            with _lock:
                active_stem = Path(_session["rom_path"]).stem if _session["rom_path"] else None
            total_bytes = sum(g["size_bytes"] for g in cache.values())
            games = [
                {
                    "name":          name,
                    "size_gb":       round(g["size_bytes"] / 1024 ** 3, 2),
                    "last_accessed": g["last_accessed"],
                    "active":        name == active_stem,
                }
                for name, g in sorted(cache.items())
            ]
            self._send_json(200, {
                "games":         games,
                "total_size_gb": round(total_bytes / 1024 ** 3, 2),
                "max_gb":        CACHE_MAX_GB,
            })
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/launch":
            body = self._read_body()
            raw_path = body.get("rom_path", "").strip()
            if not raw_path:
                self._send_json(400, {"error": "rom_path is required"})
                return

            rom_path = _validate_rom_path(raw_path)
            if rom_path is None:
                self._send_json(400, {
                    "error": "rom_path must be within ROM_ROOT",
                    "rom_root": str(ROM_ROOT),
                })
                return
            resolved = _resolve_rom_path(rom_path)
            if resolved is None:
                self._send_json(422, {
                    "error": "rom_path does not exist",
                    "path": str(rom_path),
                    "rom_root": str(ROM_ROOT),
                    "also_tried": str(ROM_ROOT / "roms" / rom_path.relative_to(ROM_ROOT))
                        if rom_path.relative_to(ROM_ROOT).parts[0] != "roms"
                        else str(ROM_ROOT.joinpath(*rom_path.relative_to(ROM_ROOT).parts[1:])),
                })
                return
            # A folder-organized game arrives as its folder; find what rpcs3
            # can actually be pointed at inside it.
            rom_file = _resolve_rom_file(resolved)
            if rom_file is None:
                self._send_json(422, {
                    "error": "no bootable ROM file found under rom_path",
                    "path": str(resolved),
                    "extensions": list(ROM_EXTENSIONS),
                })
                return
            if rom_file != resolved:
                log.info("Resolved ROM folder %s to %s", resolved, rom_file)
            resolved = rom_file

            # Check save_in_progress and claim launch_in_progress in the same
            # lock acquisition — checking them separately lets a save start in
            # the gap and the launch would then kill rpcs3 mid-savestate.
            with _lock:
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save in progress"})
                    return
                if _session["launch_in_progress"]:
                    self._send_json(409, {"error": "launch already in progress"})
                    return
                _session["launch_in_progress"] = True
            Thread(target=_do_launch, args=(str(resolved),), daemon=True).start()
            self._send_json(200, {"status": "launching", "rom_path": str(resolved)})
            return

        if self.path == "/save-state":
            with _lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
                _session["launch_status"]    = "saving"

            def _bg_save():
                try:
                    _wtype_save_state()
                finally:
                    with _lock:
                        _session["save_in_progress"] = False
                        if _session["launch_status"] == "saving":
                            _session["launch_status"] = "running"

            Thread(target=_bg_save, daemon=True).start()
            self._send_json(200, {"status": "saving"})
            return

        if self.path == "/load-state":
            with _lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            ok = _wtype_load_state()
            self._send_json(
                200 if ok else 503,
                {"status": "ok" if ok else "error", "loaded": ok},
            )
            return

        if self.path == "/save-and-exit":
            with _lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
                _session["launch_status"]    = "saving"

            # rpcs3 has a single savestate per game (Ctrl+S), so "slot" is
            # accepted for API compatibility but ignored.
            body = self._read_body()
            wait = body.get("wait", True)

            def _save_then_exit() -> bool:
                ok = False
                try:
                    ok = _wtype_save_state()
                    if not ok:
                        log.warning("save-and-exit: save failed — returning to library anyway")
                except Exception as exc:
                    log.error("save-and-exit: unexpected error during save: %s", exc)
                finally:
                    with _lock:
                        _session["save_in_progress"] = False
                try:
                    _return_to_library()
                except Exception as exc:
                    log.error("save-and-exit: unexpected error returning to library: %s", exc)
                return ok

            if wait:
                ok = _save_then_exit()
                self._send_json(200, {"status": "ok", "saved": ok})
            else:
                Thread(target=_save_then_exit, daemon=True).start()
                self._send_json(200, {"status": "queued"})
            return

        if self.path == "/volume":
            body = self._read_body()
            level = body.get("level")
            if not isinstance(level, int) or not (0 <= level <= 100):
                self._send_json(400, {"error": "level must be an integer 0–100"})
                return
            result = _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            mute_val = body.get("mute", None)
            if "mute" not in body:
                mute_arg = "toggle"
            elif mute_val is True:
                mute_arg = "1"
            elif mute_val is False:
                mute_arg = "0"
            else:
                self._send_json(400, {"error": "mute must be a boolean"})
                return
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            self._send_json(200, {"status": "ok", "mute": _pactl_get_mute()})
            return

        self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/launch":
            Thread(target=_return_to_library, daemon=True).start()
            self._send_json(200, {"status": "ok"})
            return

        if self.path.startswith("/cache/"):
            game_name = self.path[len("/cache/"):]
            if not game_name:
                self._send_json(400, {"error": "game name required"})
                return
            with _lock:
                active_stem = Path(_session["rom_path"]).stem if _session["rom_path"] else None
            if game_name == active_stem:
                self._send_json(409, {"error": "cannot evict active game"})
                return
            game_dir = (CACHE_DIR / game_name).resolve()
            if not str(game_dir).startswith(str(CACHE_DIR.resolve()) + "/"):
                self._send_json(400, {"error": "invalid game name"})
                return
            if not game_dir.is_dir():
                self._send_json(404, {"error": "game not in cache"})
                return
            freed = _dir_size_bytes(game_dir)
            shutil.rmtree(game_dir)
            log.info("Cache: manually evicted %s (%.2f GB)", game_name, freed / 1024 ** 3)
            self._send_json(200, {"status": "ok", "freed_gb": round(freed / 1024 ** 3, 2)})
            return

        self._send_json(404, {"error": "not found"})


# ── Main ──────────────────────────────────────────────────────────────────────

def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """SIGTERM/SIGINT: stop accepting requests, let an in-flight save finish,
    then kill rpcs3 so it isn't orphaned when the broker exits."""
    log.info("Received signal %d — shutting down", signum)
    # serve_forever() must be unblocked from another thread.
    Thread(target=server.shutdown, daemon=True).start()

    deadline = time.monotonic() + max(SAVE_WAIT, 5.0)
    while time.monotonic() < deadline:
        with _lock:
            if not _session["save_in_progress"]:
                break
        time.sleep(0.2)
    else:
        log.warning("Save still in progress at shutdown deadline — killing rpcs3 anyway")

    _kill_rpcs3()


def main():
    log.info("Broker starting — waiting 5s for desktop...")
    time.sleep(5)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # rpcs3 AppDir: the real process is AppRun.wrapped (AppRun execs into it).
    result = subprocess.run(["pkill", "-9", "-x", "AppRun.wrapped"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale rpcs3 instance(s) on startup.")
        time.sleep(2)

    if not SECRET:
        log.warning("BROKER_SECRET not set — all POST/DELETE endpoints are unauthenticated")

    _launch_rpcs3_internal(None)

    # Threading server: /save-state polls savestate writes for up to SAVE_WAIT
    # seconds; a single-threaded server would block /health and /status meanwhile.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("rpcs3 broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    signal.signal(signal.SIGTERM, lambda signum, frame: _graceful_shutdown(server, signum))
    signal.signal(signal.SIGINT, lambda signum, frame: _graceful_shutdown(server, signum))

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
