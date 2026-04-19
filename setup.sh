#!/bin/bash
# Setup script for Linux KVM BT HID peripheral
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash setup.sh"
    exit 1
fi

echo "=== Installing dependencies ==="
apt-get update -qq
apt-get install -y bluetooth bluez python3-pip python3-dbus
pip3 install evdev python-xlib

echo ""
echo "=== Configuring BlueZ for HID compatibility ==="
BTCONF="/etc/bluetooth/main.conf"

# CompatibilityMode is required for sdptool to register custom SDP records
if ! grep -q "CompatibilityMode" "$BTCONF" 2>/dev/null; then
    cat >> "$BTCONF" << 'EOF'

[Policy]
CompatibilityMode = true
EOF
    echo "Added CompatibilityMode=true to $BTCONF"
else
    echo "CompatibilityMode already set."
fi

# Disable pnat plugin — conflicts with manual SDP registration
BLUETOOTHD_ARGS="/etc/systemd/system/bluetooth.service.d/override.conf"
mkdir -p "$(dirname "$BLUETOOTHD_ARGS")"
if ! grep -q "noplugin=pnat" "$BLUETOOTHD_ARGS" 2>/dev/null; then
    cat > "$BLUETOOTHD_ARGS" << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd --compat --noplugin=pnat
EOF
    echo "Added --compat --noplugin=pnat to bluetoothd startup."
fi

systemctl daemon-reload
systemctl restart bluetooth
sleep 2

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
