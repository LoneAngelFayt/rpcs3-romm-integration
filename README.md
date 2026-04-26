# rpcs3-romm-integration-mod

A [linuxserver Docker mod](https://docs.linuxserver.io/general/container-customization/#docker-mods) for [linuxserver/rpcs3](https://docs.linuxserver.io/images/docker-rpcs3/) that adds an HTTP broker for [RomM](https://github.com/rommapp/romm) streaming integration.

Enables RomM to launch PS3 games from ZIP or 7z archives, manage save states, and control audio in a remote streaming session.

## Prerequisites

rpcs3 requires firmware before games will run. **On first launch, open the container's web interface and install the PS3 firmware:**

- Configuration → Install Firmware (requires `PS3UPDAT.PUP` — download from [PlayStation's system software page](https://www.playstation.com/en-us/support/hardware/ps3/system-software/))

Once firmware is installed, rpcs3 is ready to launch games via RomM. Controller mapping and display settings can be adjusted through the rpcs3 UI.

## Game Archive Format

Store PS3 games as `.zip`, `.7z`, or `.rar` archives in your RomM library. The archive must contain the game folder with `EBOOT.BIN` somewhere inside (the broker finds it automatically regardless of folder depth).

To create an archive from a game folder:
```bash
# ZIP (larger, widely supported)
zip -r "Demon_Souls.zip" "Demon_Souls/"

# 7z (30–50% smaller, recommended)
7z a "Demon_Souls.7z" "Demon_Souls/"

# RAR (if you already have .rar archives from other sources)
# Extraction is handled automatically — no conversion needed
```

All three formats report extraction progress (0–100%) to the RomM frontend. 7z is recommended for new archives — it typically saves 5–15 GB per game. RAR archives are extracted via p7zip and behave identically to .7z at runtime.

## Usage

```yaml
services:
  rpcs3:
    image: lscr.io/linuxserver/rpcs3:latest
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - DOCKER_MODS=ghcr.io/YOUR_USERNAME/rpcs3-romm-integration-mod:latest
      - BROKER_PORT=8000
      - BROKER_SECRET=your-secret-here
      - ROM_ROOT=/romm/library
      - CACHE_DIR=/config/rpcs3-cache
      - CACHE_MAX_GB=150
    volumes:
      - ./config:/config
      - /path/to/romm/library:/romm/library:ro
      - ./rpcs3-cache:/config/rpcs3-cache
    ports:
      - 3000:3000   # selkies WebRTC stream
      - 8000:8000   # broker API
    restart: unless-stopped
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_PORT` | `8000` | HTTP API port |
| `BROKER_SECRET` | unset | Shared secret — POST/DELETE require `X-Broker-Secret` header |
| `ROM_ROOT` | `/romm/library` | Archive path validation root; paths outside rejected |
| `CACHE_DIR` | `/config/rpcs3-cache` | Extracted game cache directory |
| `CACHE_MAX_GB` | `0` | Max cache size in GB; `0` = unlimited |
| `RPCS3_BOOT_TIMEOUT` | `60.0` | Seconds to wait for rpcs3 to appear after launch |
| `XDOTOOL_TIMEOUT` | `5.0` | Seconds per xdotool keypress command |
| `SAVE_WAIT` | `30.0` | Seconds to wait for save state file write to complete |
| `SAVE_DIR` | `/config/savestates` | rpcs3 save state directory |
| `BROKER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Broker API

All write endpoints require `X-Broker-Secret: <secret>` when `BROKER_SECRET` is configured. Read endpoints are always public.

| Endpoint | Method | Body | Description |
|---|---|---|---|
| `/health` | GET | — | `{"status": "ok"}` |
| `/status` | GET | — | Session state, cache info, and launch progress |
| `/cache` | GET | — | List cached games with sizes and last-accessed times |
| `/launch` | POST | `{"rom_path": "..."}` | Extract archive (.zip/.7z/.rar) if needed and launch game |
| `/launch` | DELETE | — | Kill game, return to rpcs3 library view |
| `/save-state` | POST | — | Send Ctrl+S to rpcs3 |
| `/load-state` | POST | — | Send Ctrl+R to rpcs3 |
| `/save-and-exit` | POST | — | Save state then return to library |
| `/cache/{game}` | DELETE | — | Evict game from cache (saves unaffected) |
| `/volume` | POST | `{"level": 0–100}` | Set PulseAudio sink volume |
| `/mute` | POST | `{"mute": true\|false}` or `{}` | Set or toggle mute |

## RomM Frontend Integration

After calling `POST /launch`, poll `GET /status` every 2 seconds. The `launch_status` field drives the loading overlay:

| `launch_status` | `launch_progress` | Show |
|---|---|---|
| `"evicting"` | `null` | Spinner — "Freeing cache space…" |
| `"extracting"` | `0–100` | Progress bar — "Extracting game files… (45%)" |
| `"launching"` | `null` | Spinner — "Starting rpcs3…" |
| `"running"` | `null` | Stream view |
| `"saving"` | `null` | Spinner — "Saving game…" |
| `"error"` | `null` | Error — show `launch_detail` |

Cached games skip extraction and go directly to launching. First launch of a large game can take 1–5 minutes while the archive extracts.

## Cache Management

Extracted games are stored in `CACHE_DIR`. Set `CACHE_MAX_GB` to enable automatic LRU eviction — the least recently played game is evicted first when a new game would exceed the cap.

Most PS3 games are 2–25 GB extracted; plan for 50–200 GB depending on your library. Use 7z archives for best compression (typically 30–50% smaller than ZIP).

In-game saves (`/config/dev_hdd0/`) and save states (`/config/savestates/`) are stored separately and are **never** affected by cache eviction.

## Save States

rpcs3 has one save state slot per game. `/save-state` sends Ctrl+S and polls the save state directory for up to `SAVE_WAIT` seconds to confirm the write, then returns `{"status": "saving"}` immediately. **HTTP 200 from `/save-state` means the keypress was delivered — not that the write completed.** Poll `/status` until `launch_status` returns to `"running"` to confirm the save finished. `/load-state` sends Ctrl+R (fire-and-forget — rpcs3 loads immediately).

Save states are stored as `<TITLEID>.savestate` files in `SAVE_DIR` (default `/config/savestates/`). They persist across cache evictions and container restarts as long as the `/config` volume is persisted.

## Architecture

```
init-rpcs3-config (S6 oneshot)
  └── Clean stale display sockets (wayland-*, .X11-unix)
  └── Install missing packages (python3, wtype, p7zip-full, unzip)
  └── Disable labwc autostart — broker owns rpcs3 lifecycle
  └── Seed rpcs3 config.yml if it exists (fullscreen, no confirm-shutdown)
  └── Suppress welcome/quickstart dialog (GuiConfigs/CurrentSettings.ini)
  └── Create and chown cache directory

svc-broker (S6 longrun) → broker.py
  └── Startup: kill stale rpcs3 (AppRun.wrapped), launch to library view
  └── POST /launch  → background thread
      ├── Kill current rpcs3 + drain gamepad sockets
      ├── Cache hit → touch .last_accessed → launch
      ├── LRU eviction (if CACHE_MAX_GB set)
      ├── Extract .zip (zipfile stdlib) or .7z/.rar (7z -bsp1) → progress 0–100
      ├── Discover EBOOT.BIN (any depth)
      └── Launch: sudo -u abc /opt/rpcs3/AppRun --no-gui /path/EBOOT.BIN
  └── POST /save-state  → wtype ctrl+s → poll savestates/ for write
  └── POST /load-state  → wtype ctrl+r (fire-and-forget)
  └── DELETE /launch    → _return_to_library()
  └── DELETE /cache/X   → shutil.rmtree (saves unaffected)
  └── POST /volume      → pactl set-sink-volume
  └── POST /mute        → pactl set-sink-mute
```

## Troubleshooting

**Game doesn't launch after `/launch`**
Poll `/status` — check `launch_status` and `launch_detail`. If stuck on `"extracting"`, the archive may be corrupt. If stuck on `"launching"`, rpcs3 may have crashed — check container logs.

**No EBOOT.BIN found**
The archive must contain a `EBOOT.BIN` somewhere inside the extracted tree. Verify the archive structure: `unzip -l game.zip | grep EBOOT`.

**Save state not confirmed**
If `SAVE_WAIT` expires without detecting a file write, the keypress was still delivered — rpcs3 may have saved successfully. Increase `SAVE_WAIT` if saves are large. Check `/config/savestates/` for the `.savestate` file.

**Save/load state not working**
The broker uses `wtype` to inject Ctrl+S/Ctrl+R into the Wayland session (labwc compositor). If rpcs3 is not the focused window, the keypress may not reach it. In normal streaming use rpcs3 is always the only window and will have focus. Check container logs for `wtype` errors.

**Controllers not working**
The selkies joystick interposer requires an active streaming session before rpcs3 starts. Connect to the stream via the RomM player before launching a game.

**rpcs3 firmware not installed**
If rpcs3 shows a firmware error on launch, open the container's web interface and install the PS3 firmware via Configuration → Install Firmware.

**Write endpoints accessible without authentication**
If `BROKER_SECRET` is unset, all POST and DELETE endpoints accept requests from any source. When port 8000 is exposed, set `BROKER_SECRET` to prevent unauthorized game launches or cache operations.
