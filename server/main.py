#!/usr/bin/env python3
"""Linux → Android Bluetooth KVM server. Must run as root."""
import argparse
import json
import logging
import os
import signal
import sys
import time

# Adjust path so sibling imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

from evdev import ecodes

from bt_hid import BluetoothHID
from clipboard_sync import ClipboardSync
from hid_reports import HIDState
from input_monitor import InputMonitor

_MOUSE_BTNS = frozenset((ecodes.BTN_LEFT, ecodes.BTN_RIGHT,
                         ecodes.BTN_MIDDLE, ecodes.BTN_SIDE,
                         ecodes.BTN_EXTRA))
_REL_XY = frozenset((ecodes.REL_X, ecodes.REL_Y))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.json')


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def parse_args():
    p = argparse.ArgumentParser(description='bt-kvm: Linux → Android BT KVM')
    p.add_argument('--edge', choices=['right', 'left', 'top', 'bottom'],
                   help='Edge that triggers switch to Android (overrides config.json)')
    p.add_argument('--name', metavar='NAME',
                   help='Bluetooth device name (overrides config.json)')
    p.add_argument('--speed', type=float, metavar='N',
                   help='Mouse speed multiplier (overrides config.json)')
    return p.parse_args()


def main():
    if os.geteuid() != 0:
        logger.error("Must run as root: sudo python3 server/main.py")
        sys.exit(1)

    args = parse_args()
    config = load_config()

    # CLI args override config.json
    if args.edge:
        config['edge'] = args.edge
    if args.name:
        config['device_name'] = args.name
    if args.speed is not None:
        config['mouse_speed_multiplier'] = args.speed

    edge = config.get('edge', 'right')
    logger.info(f"Edge: {edge} | Device: {config.get('device_name','Linux KVM')} "
                f"| Speed: {config.get('mouse_speed_multiplier', 1.0)}")
    device_name = config.get('device_name', 'Linux KVM')
    speed = config.get('mouse_speed_multiplier', 1.0)

    hid = BluetoothHID(device_name=device_name)
    state = HIDState()

    def on_enter_remote():
        pass  # nothing extra needed; monitor already grabbed devices

    def on_leave_remote():
        # release any held keys/buttons
        for _, report in state.release_all():
            hid.send(report)

    monitor = InputMonitor(config, on_enter_remote, on_leave_remote)

    def on_event(event):
        et = event.type
        reports = None

        if et == ecodes.EV_KEY:
            if event.code in _MOUSE_BTNS:
                reports = state.handle_mouse_button(event.code, event.value)
            else:
                reports = state.handle_key(event.code, event.value)

        elif et == ecodes.EV_REL:
            code = event.code
            value = event.value
            if speed != 1.0 and code in _REL_XY:
                value = int(value * speed)
            state.handle_rel(code, value)

        elif et == ecodes.EV_SYN:
            reports = state.flush_mouse()

        if reports:
            for _, report in reports:
                hid.send(report)

    monitor.event_callback = on_event

    # ---------- setup ----------
    logger.info("Setting up Bluetooth HID peripheral...")
    hid.setup()

    clip = ClipboardSync()
    if config.get('clipboard_sync', True):
        clip.start()

    def reconnect_loop():
        while True:
            logger.info(f"Pair '{device_name}' from Android BT settings, then connect.")
            try:
                hid.listen()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"listen() failed: {e}")
                time.sleep(3)
                continue

            logger.info(f"Android connected! "
                        f"Move mouse to the {config.get('edge','right')} edge "
                        "to switch control. Press Scroll Lock to return.")
            monitor.start()

            # wait until BT drops
            try:
                while hid.connected:
                    time.sleep(1)
            except KeyboardInterrupt:
                raise

            logger.warning("BT disconnected. Waiting for reconnect...")
            monitor.stop()
            hid.close()
            hid.setup()

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        monitor.stop()
        clip.stop()
        hid.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        reconnect_loop()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == '__main__':
    main()
