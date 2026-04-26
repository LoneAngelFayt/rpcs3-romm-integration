#!/usr/bin/with-contenv bash

# ── XDG / display socket cleanup ──────────────────────────────────────────────
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"
find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
echo "[rpcs3-broker-mod] Cleaned up stale display sockets."

# ── Package installation ───────────────────────────────────────────────────────
_need_apt=0
command -v python3    &>/dev/null || _need_apt=1
command -v xdotool    &>/dev/null || _need_apt=1
command -v 7z         &>/dev/null || _need_apt=1
command -v unzip      &>/dev/null || _need_apt=1

if [ "$_need_apt" = "1" ]; then
    echo "[rpcs3-broker-mod] Installing missing packages..."
    apt-get update -qq && apt-get install -y -qq python3 xdotool p7zip-full unzip \
        || { echo "[rpcs3-broker-mod] ERROR: apt-get install failed"; exit 1; }
fi

# ── sudoers permissions ───────────────────────────────────────────────────────
chmod 0440 /etc/sudoers.d/broker \
    || { echo "[rpcs3-broker-mod] ERROR: sudoers file missing or chmod failed"; exit 1; }
echo "[rpcs3-broker-mod] sudoers rule set."

# ── Disable labwc autostart (broker owns rpcs3 lifecycle) ────────────────────
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by rpcs3-broker-mod — broker.py owns rpcs3 lifecycle\n' > "$AUTOSTART"
echo "[rpcs3-broker-mod] Disabled labwc autostart."

# ── Create cache directory ────────────────────────────────────────────────────
CACHE_DIR="${CACHE_DIR:-/config/rpcs3-cache}"
mkdir -p "$CACHE_DIR"
chown -R abc:abc "$CACHE_DIR"
echo "[rpcs3-broker-mod] Cache dir ready: $CACHE_DIR"

# ── Seed rpcs3 config.yml ─────────────────────────────────────────────────────
RPCS3_CONFIG="/config/.config/rpcs3/config.yml"
if [ -f "$RPCS3_CONFIG" ]; then
    python3 - "$RPCS3_CONFIG" <<'PYEOF'
import sys, re
from pathlib import Path

p = Path(sys.argv[1])
text = p.read_text()

def _set_key(txt, key, value):
    """Replace 'key: anything' or append 'key: value' if absent."""
    pattern = rf'^(\s*{re.escape(key)}:\s*).*$'
    if re.search(pattern, txt, re.MULTILINE):
        return re.sub(pattern, lambda m: m.group(1) + value, txt, flags=re.MULTILINE), False
    txt += f'\n{key}: {value}'
    return txt, True

changes = [
    ("Fullscreen Mode",                "true"),
    ("Start games in fullscreen mode", "true"),
    ("Confirm Shutdown",               "false"),
]
for key, val in changes:
    text, added = _set_key(text, key, val)
    action = "Seeded" if added else "Patched"
    print(f"[rpcs3-broker-mod] {action} config: {key} = {val}")

p.write_text(text)
PYEOF
else
    echo "[rpcs3-broker-mod] rpcs3 config.yml not found — will patch on next boot after first run."
fi

# ── Fix ownership ─────────────────────────────────────────────────────────────
chown -R abc:abc /config/.config/rpcs3 2>/dev/null || true
echo "[rpcs3-broker-mod] Fixed rpcs3 config ownership."
