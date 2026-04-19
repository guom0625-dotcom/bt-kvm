#!/usr/bin/env python3
"""Linux → Android Bluetooth KVM server. Must run as root."""
import json
import logging
import os
import signal
import sys
import time

# Adjust path so sibling imports work when run directly
sys.path.insert(0, os.path.dirname(__file__))

from bt_hid import BluetoothHID
from hid_reports import HIDState
from input_monitor import InputMonitor

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


def main():
    if os.geteuid() != 0:
        logger.error("Must run as root: sudo python3 server/main.py")
        sys.exit(1)

    config = load_config()
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
        from evdev import ecodes
        result = None

        if event.type == ecodes.EV_KEY:
            if event.code in (ecodes.BTN_LEFT, ecodes.BTN_RIGHT,
                              ecodes.BTN_MIDDLE, ecodes.BTN_SIDE,
                              ecodes.BTN_EXTRA):
                result = state.handle_mouse_button(event.code, event.value)
            else:
                result = state.handle_key(event.code, event.value)

        elif event.type == ecodes.EV_REL:
            if speed != 1.0 and event.code in (ecodes.REL_X, ecodes.REL_Y):
                from evdev import InputEvent
                event = InputEvent(event.sec, event.usec, event.type,
                                   event.code, int(event.value * speed))
            state.handle_rel(event.code, event.value)

        elif event.type == ecodes.EV_SYN:
            result = state.flush_mouse()

        if result:
            _, report = result
            hid.send(report)

    monitor.event_callback = on_event

    # ---------- setup ----------
    logger.info("Setting up Bluetooth HID peripheral...")
    hid.setup()

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
