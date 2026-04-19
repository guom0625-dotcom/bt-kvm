"""X11 edge detection + input capture.

capture_method:
  "evdev"  - grabs /dev/input devices directly (pure Linux, no Barrier)
  "x11"    - grabs at X11 level via XGrab (works with Barrier-injected events)
  "auto"   - tries evdev first; falls back to x11 if no devices found
"""
import logging
import select
import threading
import time
from typing import Callable, Optional

from evdev import InputDevice, list_devices, ecodes

logger = logging.getLogger(__name__)

DEFAULT_RETURN_THRESHOLD = 80
SWITCH_BACK_KEY = ecodes.KEY_SCROLLLOCK
RETURN_EDGE = {'right': 'left', 'left': 'right',
               'top': 'bottom', 'bottom': 'top'}


def _find_keyboards_and_mice():
    keyboards, mice = [], []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities()
            if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
                keyboards.append(dev)
            elif ecodes.EV_REL in caps and ecodes.REL_X in caps[ecodes.EV_REL]:
                mice.append(dev)
        except Exception:
            pass
    return keyboards, mice


class InputMonitor:
    def __init__(self, config: dict,
                 on_enter_remote: Callable,
                 on_leave_remote: Callable):
        self._config = config
        self._on_enter_remote = on_enter_remote
        self._on_leave_remote = on_leave_remote

        self.event_callback: Optional[Callable] = None
        self.remote_mode = False
        self._virt_x = 0
        self._virt_y = 0

        self._display = None
        self._root = None
        self._screen_w = 0
        self._screen_h = 0
        self._init_x11()

        # Choose capture backend
        method = config.get('capture_method', 'auto')
        self._backend = self._choose_backend(method)

        self._running = False
        self._edge_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # Init

    def _init_x11(self):
        from Xlib import display as xdisp
        try:
            d = xdisp.Display()
            screen = d.screen()
            self._display = d
            self._root = screen.root
            self._screen_w = screen.width_in_pixels
            self._screen_h = screen.height_in_pixels
            logger.info(f"X11 screen: {self._screen_w}x{self._screen_h}")
        except Exception as e:
            raise RuntimeError(f"X11 init failed: {e}. Is DISPLAY set?")

    def _choose_backend(self, method: str):
        if method == 'x11':
            return self._make_x11_backend()

        # evdev or auto
        keyboards, mice = _find_keyboards_and_mice()
        if keyboards or mice:
            logger.info(f"evdev: {len(keyboards)} keyboard(s), {len(mice)} mouse/mice")
            return {'type': 'evdev', 'keyboards': keyboards, 'mice': mice}

        if method == 'auto':
            logger.info("No evdev devices found → falling back to X11 grab "
                        "(Barrier mode)")
            return self._make_x11_backend()

        raise RuntimeError("No evdev input devices found. Run as root?")

    def _make_x11_backend(self):
        from x11_grab import X11GrabCapture
        cap = X11GrabCapture(self._display, self._root)
        logger.info("Capture backend: X11 grab (Barrier-compatible)")
        return {'type': 'x11', 'capture': cap}

    # ------------------------------------------------------------------ #
    # X11 helpers

    def _mouse_pos(self):
        p = self._root.query_pointer()
        return p.root_x, p.root_y

    def _warp(self, x, y):
        self._root.warp_pointer(x, y)
        self._display.flush()

    def _at_edge(self, x, y) -> bool:
        t = self._config.get('edge_threshold', 3)
        edge = self._config.get('edge', 'right')
        if edge == 'right':  return x >= self._screen_w - t
        if edge == 'left':   return x <= t
        if edge == 'bottom': return y >= self._screen_h - t
        if edge == 'top':    return y <= t
        return False

    def _past_return_edge(self) -> bool:
        thr = self._config.get('return_threshold', DEFAULT_RETURN_THRESHOLD)
        ret = RETURN_EDGE[self._config.get('edge', 'right')]
        if ret == 'left':   return self._virt_x < -thr
        if ret == 'right':  return self._virt_x >  thr
        if ret == 'top':    return self._virt_y < -thr
        if ret == 'bottom': return self._virt_y >  thr
        return False

    # ------------------------------------------------------------------ #
    # Mode switching

    def _enter_remote(self):
        if self.remote_mode:
            return
        self.remote_mode = True
        self._virt_x = 0
        self._virt_y = 0

        if self._backend['type'] == 'evdev':
            for dev in self._backend['keyboards'] + self._backend['mice']:
                try:
                    dev.grab()
                except Exception as e:
                    logger.warning(f"evdev grab {dev.path}: {e}")
        else:
            self._backend['capture'].grab()

        self._warp(self._screen_w // 2, self._screen_h // 2)
        ret = RETURN_EDGE[self._config.get('edge', 'right')]
        logger.info(f">>> Android  (마우스를 {ret}으로 밀거나 Scroll Lock 복귀)")
        self._on_enter_remote()

    def _leave_remote(self):
        if not self.remote_mode:
            return
        self.remote_mode = False

        if self._backend['type'] == 'evdev':
            for dev in self._backend['keyboards'] + self._backend['mice']:
                try:
                    dev.ungrab()
                except Exception:
                    pass
        else:
            self._backend['capture'].ungrab()

        logger.info("<<< Linux")
        self._on_leave_remote()

    # ------------------------------------------------------------------ #
    # Thread loops

    def start(self):
        self._running = True
        self._edge_thread = threading.Thread(
            target=self._edge_loop, daemon=True, name="edge-detect")
        self._event_thread = threading.Thread(
            target=self._event_loop, daemon=True, name="event-reader")
        self._edge_thread.start()
        self._event_thread.start()

    def stop(self):
        self._running = False
        if self.remote_mode:
            self._leave_remote()

    def _edge_loop(self):
        while self._running:
            if not self.remote_mode:
                try:
                    x, y = self._mouse_pos()
                    if self._at_edge(x, y):
                        self._enter_remote()
                except Exception as e:
                    logger.debug(f"edge loop: {e}")
            time.sleep(0.01)

    def _event_loop(self):
        if self._backend['type'] == 'evdev':
            self._evdev_loop()
        else:
            self._x11_loop()

    # ---- evdev loop ----

    def _evdev_loop(self):
        keyboards = self._backend['keyboards']
        mice      = self._backend['mice']
        while self._running:
            if not self.remote_mode:
                time.sleep(0.02)
                continue
            fd_map = {d.fd: d for d in keyboards + mice}
            try:
                readable, _, _ = select.select(fd_map, [], [], 0.1)
            except Exception:
                time.sleep(0.01)
                continue
            for fd in readable:
                dev = fd_map[fd]
                try:
                    for event in dev.read():
                        if not self.remote_mode:
                            break
                        if (event.type == ecodes.EV_KEY and
                                event.code == SWITCH_BACK_KEY and
                                event.value == 1):
                            self._leave_remote()
                            break
                        if event.type == ecodes.EV_REL:
                            if event.code == ecodes.REL_X:
                                self._virt_x += event.value
                            elif event.code == ecodes.REL_Y:
                                self._virt_y += event.value
                            if self._past_return_edge():
                                self._leave_remote()
                                break
                        if self.event_callback:
                            self.event_callback(event)
                except OSError:
                    pass

    # ---- x11 grab loop ----

    def _x11_loop(self):
        from x11_grab import X11GrabCapture
        cap: X11GrabCapture = self._backend['capture']

        while self._running:
            if not self.remote_mode:
                time.sleep(0.02)
                continue

            for evt in cap.read(timeout=0.1):
                if not self.remote_mode:
                    break

                kind = evt[0]

                if kind == 'key':
                    _, code, value = evt
                    # Scroll Lock → switch back (don't forward to Android)
                    if code == SWITCH_BACK_KEY and value == 1:
                        self._leave_remote()
                        break
                    if self.event_callback:
                        self.event_callback(self._make_key_event(code, value))

                elif kind == 'btn':
                    _, btn, value = evt
                    if self.event_callback:
                        self.event_callback(self._make_key_event(btn, value))

                elif kind == 'move':
                    _, dx, dy = evt
                    self._virt_x += dx
                    self._virt_y += dy
                    if self._past_return_edge():
                        self._leave_remote()
                        break
                    if self.event_callback:
                        self.event_callback(self._make_rel_event(ecodes.REL_X, dx))
                        self.event_callback(self._make_rel_event(ecodes.REL_Y, dy))
                        self.event_callback(self._make_syn_event())

                elif kind == 'scroll':
                    _, value = evt
                    if self.event_callback:
                        self.event_callback(
                            self._make_rel_event(ecodes.REL_WHEEL, value))
                        self.event_callback(self._make_syn_event())

    # ---- synthetic evdev-compatible event constructors ----

    @staticmethod
    def _make_key_event(code, value):
        from evdev import InputEvent
        return InputEvent(0, 0, ecodes.EV_KEY, code, value)

    @staticmethod
    def _make_rel_event(code, value):
        from evdev import InputEvent
        return InputEvent(0, 0, ecodes.EV_REL, code, value)

    @staticmethod
    def _make_syn_event():
        from evdev import InputEvent
        return InputEvent(0, 0, ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
