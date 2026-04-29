#!/usr/bin/env python3
"""Linux → Android Bluetooth KVM server. Must run as root."""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque

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


class HIDSender:
    """Decouples evdev event production from BT HID transmission.

    Two lanes:
      critical (FIFO): keyboard / mouse-button reports — never coalesced
      mouse    (single slot): motion reports — newer overwrites older

    The sender thread is the sole caller of hid.send(), so kernel L2CAP
    queue depth stays at 1, and stale mouse positions are dropped instead
    of accumulating during BT slowdowns.
    """

    # Mouse motion send floor. Slightly above the CSR air rate
    # (~12ms/packet observed) so the kernel L2CAP queue drains faster than
    # we feed it. Without this, sends complete instantly into a deep kernel
    # buffer and stale positions accumulate behind the air bottleneck.
    MOUSE_MIN_INTERVAL = 0.013

    def __init__(self, hid):
        self._hid = hid
        self._cond = threading.Condition()
        self._critical_q: deque = deque()
        self._mouse_slot = None
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="hid-sender")
        self._thread.start()

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def enqueue_critical(self, report: bytes):
        with self._cond:
            self._critical_q.append(report)
            self._cond.notify()

    def enqueue_mouse(self, report: bytes):
        with self._cond:
            self._mouse_slot = report
            self._cond.notify()

    def clear_mouse(self):
        with self._cond:
            self._mouse_slot = None

    def _loop(self):
        last_mouse_send = 0.0
        while True:
            with self._cond:
                while (self._running
                       and not self._critical_q
                       and self._mouse_slot is None):
                    self._cond.wait(timeout=0.5)
                if not self._running:
                    return

                if self._critical_q:
                    report = self._critical_q.popleft()
                    is_mouse = False
                else:
                    # mouse motion: throttle so the kernel L2CAP queue stays
                    # at depth 1 and the slot has time to coalesce updates
                    wait = self.MOUSE_MIN_INTERVAL - (time.time() - last_mouse_send)
                    if wait > 0:
                        # release lock during wait so producer can replace slot
                        # and a critical report can preempt
                        self._cond.wait(timeout=wait)
                        if not self._running:
                            return
                        if self._critical_q:
                            report = self._critical_q.popleft()
                            is_mouse = False
                        elif self._mouse_slot is not None:
                            report = self._mouse_slot
                            self._mouse_slot = None
                            is_mouse = True
                        else:
                            continue
                    else:
                        report = self._mouse_slot
                        self._mouse_slot = None
                        is_mouse = True

            self._hid.send(report)
            if is_mouse:
                last_mouse_send = time.time()


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
    p.add_argument('--adapter', metavar='HCIx',
                   help='Bluetooth adapter (e.g. hci1, overrides config.json)')
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
    if args.adapter:
        config['bt_adapter'] = args.adapter

    edge = config.get('edge', 'right')
    adapter = config.get('bt_adapter', 'hci0')
    logger.info(f"Edge: {edge} | Device: {config.get('device_name','Linux KVM')} "
                f"| Adapter: {adapter} "
                f"| Speed: {config.get('mouse_speed_multiplier', 1.0)}")
    device_name = config.get('device_name', 'Linux KVM')
    speed = config.get('mouse_speed_multiplier', 1.0)

    hid = BluetoothHID(device_name=device_name, adapter=adapter)
    state = HIDState()
    sender = HIDSender(hid)

    def on_enter_remote():
        pass  # nothing extra needed; monitor already grabbed devices

    def on_leave_remote():
        # drop any pending motion; deliver final button/key release reliably
        sender.clear_mouse()
        for _, report in state.release_all():
            sender.enqueue_critical(report)

    monitor = InputMonitor(config, on_enter_remote, on_leave_remote)

    def on_event(event):
        et = event.type

        if et == ecodes.EV_KEY:
            if event.code in _MOUSE_BTNS:
                reports = state.handle_mouse_button(event.code, event.value)
            else:
                reports = state.handle_key(event.code, event.value)
            for _, report in reports:
                sender.enqueue_critical(report)

        elif et == ecodes.EV_REL:
            code = event.code
            value = event.value
            if speed != 1.0 and code in _REL_XY:
                value = int(value * speed)
            state.handle_rel(code, value)

        elif et == ecodes.EV_SYN:
            reports = state.flush_mouse()
            if len(reports) == 1:
                sender.enqueue_mouse(reports[0][1])
            else:
                # large flick split into >127 chunks: send all in order so
                # cumulative motion isn't lost to coalescing
                for _, report in reports:
                    sender.enqueue_critical(report)

    monitor.event_callback = on_event

    # ---------- setup ----------
    logger.info("Setting up Bluetooth HID peripheral...")
    hid.setup()
    sender.start()

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
        sender.stop()
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
