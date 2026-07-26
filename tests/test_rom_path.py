"""ROM path handling: the boundary between RomM's input and rpcs3's boot target.

The broker is a single stdlib module under root/root; import it directly and
exercise path validation, the folder-to-file resolution /launch depends on, and
the launch branch that boots an already-decrypted game tree in place.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "root" / "root"))
import broker  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_session():
    with broker._lock:
        broker._session.update({
            "process": None,
            "rom_path": None,
            "rom_name": None,
            "eboot_path": None,
            "started_at": None,
            "launch_in_progress": False,
            "launch_status": "idle",
            "launch_detail": None,
            "launch_progress": None,
        })
    yield


@pytest.fixture
def rom_root(tmp_path, monkeypatch):
    root = tmp_path / "library"
    (root / "ps3").mkdir(parents=True)
    monkeypatch.setattr(broker, "ROM_ROOT", root.resolve())
    return root.resolve()


def _file(root, rel, data=b"game"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _jb_folder(root, rel):
    """A decrypted JB tree, the layout rpcs3 boots without any extraction."""
    eboot = _file(root, f"{rel}/PS3_GAME/USRDIR/EBOOT.BIN", b"SCE\0")
    _file(root, f"{rel}/PS3_DISC.SFB")
    return eboot


# ── Path validation ───────────────────────────────────────────────────────────


def test_validate_accepts_an_archive_inside_root(rom_root):
    rom = _file(rom_root, "ps3/game.7z")
    assert broker._validate_rom_path(str(rom)) == rom


def test_validate_accepts_a_game_folder_inside_root(rom_root):
    _jb_folder(rom_root, "ps3/Demon's Souls")
    folder = rom_root / "ps3" / "Demon's Souls"
    assert broker._validate_rom_path(str(folder)) == folder


def test_validate_rejects_traversal_outside_root(rom_root):
    raw = str(rom_root / "ps3" / ".." / ".." / "etc" / "passwd")
    assert broker._validate_rom_path(raw) is None


def test_validate_rejects_absolute_path_outside_root(rom_root):
    assert broker._validate_rom_path("/etc/passwd") is None


def test_validate_rejects_symlink_escaping_root(rom_root, tmp_path):
    outside = tmp_path / "outside.7z"
    outside.write_bytes(b"game")
    link = rom_root / "escape.7z"
    link.symlink_to(outside)
    assert broker._validate_rom_path(str(link)) is None


# ── Folder-organized ROMs ─────────────────────────────────────────────────────
#
# RomM addresses a folder-organized game by its folder: `Rom.full_path` is
# `fs_path/fs_name`, and for a multi-file ROM `fs_name` is the directory rather
# than anything rpcs3 can boot.


def test_resolve_passes_an_archive_through(rom_root):
    archive = _file(rom_root, "ps3/game.7z")
    assert broker._resolve_rom_file(archive) == archive


def test_resolve_passes_a_bare_iso_through(rom_root):
    iso = _file(rom_root, "ps3/game.iso")
    assert broker._resolve_rom_file(iso) == iso


def test_resolve_rejects_a_file_rpcs3_cannot_boot(rom_root):
    txt = _file(rom_root, "ps3/readme.txt")
    assert broker._resolve_rom_file(txt) is None


def test_resolve_returns_the_folder_itself_for_a_decrypted_tree(rom_root):
    """An extracted game on the share boots in place, so resolution keeps the
    folder: there is nothing to extract and nothing to cache."""
    _jb_folder(rom_root, "ps3/Demon's Souls")
    folder = rom_root / "ps3" / "Demon's Souls"
    assert broker._resolve_rom_file(folder) == folder


def test_resolve_finds_an_archive_inside_a_game_folder(rom_root):
    archive = _file(rom_root, "ps3/Demon's Souls/Demon's Souls.7z")
    folder = rom_root / "ps3" / "Demon's Souls"
    assert broker._resolve_rom_file(folder) == archive


def test_resolve_keeps_a_folder_holding_a_bare_iso(rom_root):
    """A bare .iso is a boot target in its own right, so the folder is kept and
    booted in place rather than resolved to an archive that would be unpacked
    first. See test_launch_boots_the_iso_in_a_folder_instead_of_the_archive."""
    _file(rom_root, "ps3/Game/Game.iso")
    _file(rom_root, "ps3/Game/Game.7z")
    assert broker._resolve_rom_file(rom_root / "ps3" / "Game") == (
        rom_root / "ps3" / "Game"
    )


def test_resolve_prefers_7z_over_zip(rom_root):
    sevenz = _file(rom_root, "ps3/Game/Game.7z")
    _file(rom_root, "ps3/Game/Game.zip")
    assert broker._resolve_rom_file(rom_root / "ps3" / "Game") == sevenz


def test_resolve_returns_none_for_a_folder_with_nothing_bootable(rom_root):
    _file(rom_root, "ps3/Game/cover.png")
    _file(rom_root, "ps3/Game/notes.txt")
    assert broker._resolve_rom_file(rom_root / "ps3" / "Game") is None


def test_resolve_looks_one_level_into_subfolders_for_an_archive(rom_root):
    archive = _file(rom_root, "ps3/Game/parts/Game.7z")
    assert broker._resolve_rom_file(rom_root / "ps3" / "Game") == archive


def test_resolve_ignores_hidden_files(rom_root):
    _file(rom_root, "ps3/Game/._Game.7z")
    assert broker._resolve_rom_file(rom_root / "ps3" / "Game") is None


def test_resolve_refuses_a_symlink_escaping_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside.7z"
    outside.write_bytes(b"game")
    folder = rom_root / "ps3" / "Game"
    folder.mkdir(parents=True)
    (folder / "link.7z").symlink_to(outside)
    assert broker._resolve_rom_file(folder) is None


def test_resolve_returns_none_for_a_missing_path(rom_root):
    assert broker._resolve_rom_file(rom_root / "ps3" / "nope") is None


# ── Booting a decrypted tree in place ─────────────────────────────────────────


def test_launch_boots_a_library_folder_without_extracting_or_caching(
    rom_root, tmp_path, monkeypatch
):
    eboot = _jb_folder(rom_root, "ps3/Demon's Souls")
    folder = rom_root / "ps3" / "Demon's Souls"
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(broker, "CACHE_DIR", cache)
    monkeypatch.setattr(broker, "_kill_rpcs3", lambda: None)
    monkeypatch.setattr(broker, "_drain_gamepad_sockets", lambda: None)
    monkeypatch.setattr(broker, "_finish_launch", lambda ok: None)
    monkeypatch.setattr(broker.time, "sleep", lambda s: None)
    spawned = []
    monkeypatch.setattr(
        broker, "_launch_rpcs3_internal", lambda p: spawned.append(p) or True
    )

    broker._do_launch_inner(str(folder))

    assert spawned == [str(eboot)]
    assert broker._session["eboot_path"] == str(eboot)
    assert broker._session["rom_name"] == "Demon's Souls"
    # Nothing was copied into the cache: the game is already on the share.
    assert list(cache.iterdir()) == []


# ── The /launch contract ──────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    """A live broker server with the launch thread stubbed out."""
    monkeypatch.setattr(broker, "SECRET", "")
    launched = []
    monkeypatch.setattr(broker, "_do_launch", launched.append)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), broker.BrokerHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", launched
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait(launched, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not launched:
        time.sleep(0.02)
    assert launched, "launch thread never ran"
    return launched


def test_launch_accepts_a_game_folder(client, rom_root):
    base, launched = client
    _jb_folder(rom_root, "ps3/Demon's Souls")
    folder = rom_root / "ps3" / "Demon's Souls"
    code, body = _post(base, "/launch", {"rom_path": str(folder)})
    assert code == 200
    assert body["rom_path"] == str(folder)
    assert _wait(launched)[-1] == str(folder)


def test_launch_resolves_a_folder_to_the_archive_inside_it(client, rom_root):
    base, launched = client
    archive = _file(rom_root, "ps3/Demon's Souls/Demon's Souls.7z")
    code, body = _post(
        base, "/launch", {"rom_path": str(rom_root / "ps3" / "Demon's Souls")}
    )
    assert code == 200
    assert body["rom_path"] == str(archive)
    assert _wait(launched)[-1] == str(archive)


def test_launch_reports_a_folder_with_nothing_bootable_distinctly(client, rom_root):
    base, launched = client
    _file(rom_root, "ps3/Game/cover.png")
    code, body = _post(base, "/launch", {"rom_path": str(rom_root / "ps3" / "Game")})
    assert code == 422
    assert "no bootable ROM file" in body["error"]
    assert ".7z" in body["extensions"]
    assert launched == []
    assert not broker._session["launch_in_progress"]


def test_launch_still_reports_a_missing_path_as_missing(client, rom_root):
    base, _launched = client
    code, body = _post(base, "/launch", {"rom_path": str(rom_root / "ps3" / "nope.7z")})
    assert code == 422
    assert body["error"] == "rom_path does not exist"


def test_launch_still_rejects_a_path_outside_the_library(client, rom_root):
    base, _launched = client
    code, body = _post(base, "/launch", {"rom_path": "/etc/passwd"})
    assert code == 400
    assert "rom_root" in body
