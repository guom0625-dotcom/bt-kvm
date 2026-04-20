#!/bin/bash
# bt-kvm setup/restore script
# Usage:
#   sudo bash setup.sh          — initial setup (install deps + configure BlueZ)
#   sudo bash setup.sh restore  — revert BlueZ config to original

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash setup.sh [restore]"
    exit 1
fi

BTCONF="/etc/bluetooth/main.conf"
OVERRIDE="/etc/systemd/system/bluetooth.service.d/override.conf"

# ------------------------------------------------------------------ #
# restore
# ------------------------------------------------------------------ #
if [ "${1}" = "restore" ]; then
    echo "=== Restoring BlueZ config ==="

    # Remove [Policy] CompatibilityMode block added by setup
    if grep -q "CompatibilityMode" "$BTCONF" 2>/dev/null; then
        # Delete the blank line + [Policy] + CompatibilityMode line we appended
        sed -i '/^$/{ N; /^\n\[Policy\]/{ N; /\nCompatibilityMode = true/d } }' "$BTCONF"
        # Fallback: simpler line-by-line removal if sed above didn't match
        if grep -q "CompatibilityMode" "$BTCONF" 2>/dev/null; then
            sed -i '/^\[Policy\]/d; /^CompatibilityMode = true/d' "$BTCONF"
        fi
        echo "Removed CompatibilityMode from $BTCONF"
    else
        echo "CompatibilityMode not found in $BTCONF — skipping."
    fi

    # Remove override.conf
    if [ -f "$OVERRIDE" ]; then
        rm -f "$OVERRIDE"
        rmdir --ignore-fail-on-non-empty "$(dirname "$OVERRIDE")"
        echo "Removed $OVERRIDE"
    else
        echo "$OVERRIDE not found — skipping."
    fi

    systemctl daemon-reload
    systemctl restart bluetooth
    echo ""
    echo "=== BlueZ restored. bluetoothd is back to default settings. ==="
    exit 0
fi

# ------------------------------------------------------------------ #
# setup (default)
# ------------------------------------------------------------------ #
echo "=== Installing dependencies ==="
apt-get update -qq
apt-get install -y bluetooth bluez python3-dbus python3-gi python3-evdev python3-xlib xclip

echo ""
echo "=== Configuring BlueZ for HID compatibility ==="

if ! grep -q "CompatibilityMode" "$BTCONF" 2>/dev/null; then
    cat >> "$BTCONF" << 'EOF'

[Policy]
CompatibilityMode = true
EOF
    echo "Added CompatibilityMode=true to $BTCONF"
else
    echo "CompatibilityMode already set."
fi

mkdir -p "$(dirname "$OVERRIDE")"
cat > "$OVERRIDE" << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd --compat --noplugin=pnat,input,a2dp,avrcp,network,sap
EOF
echo "Set bluetoothd: --compat --noplugin=pnat,input"

systemctl daemon-reload
systemctl restart bluetooth
# Stop obexd — it registers File Transfer, Phone Book, Message Access SDP records
# that make the device look like a phone/PC to Android MDM policies.
systemctl stop obex 2>/dev/null || true
systemctl disable obex 2>/dev/null || true
sleep 2

echo ""
echo "=== Registering RFCOMM Serial Port (clipboard channel) ==="
sdptool add --channel=4 SP && echo "RFCOMM channel 4 registered." || echo "sdptool SP failed (non-fatal)"

echo ""
echo "=== Done ==="
echo ""
echo "Usage:"
echo "  sudo python3 server/main.py"
echo ""
echo "1. Run the command above"
echo "2. On Android: Settings → Bluetooth → scan → connect to '$(python3 -c "import json; print(json.load(open('config.json')).get('device_name','Linux KVM'))" 2>/dev/null || echo Linux KVM)'"
echo "3. Move mouse to right screen edge → control switches to Android"
echo "4. Press Scroll Lock → control returns to Linux"
echo ""
echo "To revert all BlueZ changes:  sudo bash setup.sh restore"
