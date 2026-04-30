#!/usr/bin/with-contenv bash

# ── XDG / display socket cleanup ──────────────────────────────────────────────
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"
find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
echo "[rpcs3-broker-mod] Cleaned up stale display sockets."

# ── Package installation ───────────────────────────────────────────────────────
_need_apt=0
command -v python3 &>/dev/null || _need_apt=1
command -v wtype   &>/dev/null || _need_apt=1
command -v 7z      &>/dev/null || _need_apt=1
command -v unzip   &>/dev/null || _need_apt=1

if [ "$_need_apt" = "1" ]; then
    echo "[rpcs3-broker-mod] Installing missing packages..."
    apt-get update -qq && apt-get install -y -qq python3 wtype p7zip-full unzip \
        || { echo "[rpcs3-broker-mod] ERROR: apt-get install failed"; exit 1; }
fi

# ── rpcs3 binary upgrade ─────────────────────────────────────────────────────
RPCS3_TARGET_BUILD="0.0.40-19261-e05d3597"
RPCS3_APPIMAGE_URL="https://github.com/RPCS3/rpcs3-binaries-linux/releases/download/build-e05d35972192f7cb9a3af39dac83d6bd402c6861/rpcs3-v0.0.40-19261-e05d3597_linux64.AppImage"
RPCS3_APPIMAGE_CACHE="/config/rpcs3-v0.0.40.AppImage"

_current_build=$(/opt/rpcs3/usr/bin/rpcs3 --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+-\d+-[0-9a-f]+' || true)
if [[ "$_current_build" != "$RPCS3_TARGET_BUILD"* ]]; then
    echo "[rpcs3-broker-mod] rpcs3 is $_current_build, upgrading to $RPCS3_TARGET_BUILD..."
    if [ ! -f "$RPCS3_APPIMAGE_CACHE" ]; then
        echo "[rpcs3-broker-mod] Downloading rpcs3 AppImage..."
        curl -sL -o "$RPCS3_APPIMAGE_CACHE" "$RPCS3_APPIMAGE_URL" \
            || { echo "[rpcs3-broker-mod] ERROR: download failed"; rm -f "$RPCS3_APPIMAGE_CACHE"; }
    fi
    if [ -f "$RPCS3_APPIMAGE_CACHE" ]; then
        chmod +x "$RPCS3_APPIMAGE_CACHE"
        cd /tmp && "$RPCS3_APPIMAGE_CACHE" --appimage-extract > /dev/null 2>&1 \
            && cp -rf /tmp/squashfs-root/. /opt/rpcs3/ \
            && rm -rf /tmp/squashfs-root \
            && echo "[rpcs3-broker-mod] rpcs3 upgraded to $RPCS3_TARGET_BUILD." \
            || echo "[rpcs3-broker-mod] WARNING: upgrade failed, using bundled version."
    fi
else
    echo "[rpcs3-broker-mod] rpcs3 is already $RPCS3_TARGET_BUILD."
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

# ── Suppress rpcs3 welcome/quickstart dialog ──────────────────────────────────
RPCS3_GUI_CONFIG="/config/.config/rpcs3/GuiConfigs/CurrentSettings.ini"
mkdir -p "$(dirname "$RPCS3_GUI_CONFIG")"
if [ -f "$RPCS3_GUI_CONFIG" ]; then
    if grep -q "infoBoxEnabledWelcome" "$RPCS3_GUI_CONFIG"; then
        sed -i 's/infoBoxEnabledWelcome=.*/infoBoxEnabledWelcome=false/' "$RPCS3_GUI_CONFIG"
    elif grep -q '^\[main_window\]' "$RPCS3_GUI_CONFIG"; then
        sed -i '/^\[main_window\]/a infoBoxEnabledWelcome=false' "$RPCS3_GUI_CONFIG"
    else
        printf '\n[main_window]\ninfoBoxEnabledWelcome=false\n' >> "$RPCS3_GUI_CONFIG"
    fi
else
    printf '[main_window]\ninfoBoxEnabledWelcome=false\n' > "$RPCS3_GUI_CONFIG"
fi
echo "[rpcs3-broker-mod] Disabled welcome/quickstart dialog."

# ── Hide mouse cursor in game window ─────────────────────────────────────────
_set_ini_key() {
    local file="$1" key="$2" value="$3"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s/^${key}=.*/${key}=${value}/" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}
_set_ini_key "$RPCS3_GUI_CONFIG" "gs_disableMouse"    "true"
_set_ini_key "$RPCS3_GUI_CONFIG" "gs_hideMouseOnIdle" "true"
_set_ini_key "$RPCS3_GUI_CONFIG" "gs_hideMouseIdleTime" "1"
echo "[rpcs3-broker-mod] Configured cursor hide in game window."

# ── Fix ownership ─────────────────────────────────────────────────────────────
chown -R abc:abc /config/.config/rpcs3 2>/dev/null || true
echo "[rpcs3-broker-mod] Fixed rpcs3 config ownership."
