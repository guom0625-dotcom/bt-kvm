"""X11 edge detection + input capture.

capture_method:
  "evdev"  - grabs /dev/input devices directly (pure Linux, no Barrier)
  "x11"    - grabs at X11 level via XGrab (works with Barrier-injected events)
  "auto"   - tries evdev first; falls back to x11 if no devices found
"""
import logging
import re
import select
import subprocess
import threading
import time
from typing import Callable, Optional

from evdev import InputDevice, list_devices, ecodes

logger = logging.getLogger(__name__)

DEFAULT_RETURN_THRESHOLD = 80
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
        self._ignore_motion_until = 0.0

        keyname = config.get('toggle_key', 'KEY_PAUSE')
        self._toggle_keycode = getattr(ecodes, keyname, ecodes.KEY_PAUSE)

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
            # Grab/event display — shared with X11GrabCapture
            d = xdisp.Display()
            screen = d.screen()
            self._display = d
            self._root = screen.root
            self._screen_w = screen.width_in_pixels
            self._screen_h = screen.height_in_pixels
            logger.info(f"X11 virtual desktop: {self._screen_w}x{self._screen_h}")
            # Separate connection for edge polling so query_pointer() calls
            # never race with the grab display's event loop.
            d2 = xdisp.Display()
            self._poll_root    = d2.screen().root
            self._poll_display = d2
        except Exception as e:
            raise RuntimeError(f"X11 init failed: {e}. Is DISPLAY set?")

        # Primary monitor bounds — used for warp target (center of screen).
        bounds = self._get_primary_monitor_bounds()
        if bounds:
            ox, oy, ow, oh = bounds
        else:
            ox, oy, ow, oh = 0, 0, self._screen_w, self._screen_h
        self._mon_x, self._mon_y = ox, oy
        self._mon_w, self._mon_h = ow, oh

        # Physical desktop bounds — extreme edges of ALL connected monitors.
        # Used for edge detection so multi-monitor layouts work correctly
        # (e.g. left edge = leftmost pixel of leftmost monitor, not primary).
        db = self._get_physical_desktop_bounds()
        if db:
            self._desk_x0, self._desk_y0, self._desk_x1, self._desk_y1 = db
        else:
            self._desk_x0, self._desk_y0 = 0, 0
            self._desk_x1, self._desk_y1 = self._screen_w, self._screen_h
        logger.info(f"Physical desktop: ({self._desk_x0},{self._desk_y0})"
                    f"–({self._desk_x1},{self._desk_y1})"
                    f"  warp target: {ow}x{oh} at ({ox},{oy})")

        # Passive grab: toggle hotkey fires even when not in remote mode
        self._register_toggle_hotkey()

    def _register_toggle_hotkey(self):
        from Xlib import X
        keycode = self._toggle_keycode + 8  # evdev → X11 keycode
        try:
            self._poll_root.grab_key(keycode, X.AnyModifier, True,
                                     X.GrabModeAsync, X.GrabModeAsync)
            self._poll_display.flush()
            keyname = self._config.get('toggle_key', 'KEY_PAUSE')
            logger.info(f"Toggle hotkey: {keyname} (X11 keycode {keycode}) — "
                        "press to enter/exit Android mode")
        except Exception as e:
            logger.warning(f"XGrabKey failed: {e}")

    def _check_hotkey_events(self):
        """Process passive hotkey events on the polling display."""
        from Xlib import X
        try:
            while self._poll_display.pending_events():
                ev = self._poll_display.next_event()
                if ev.type == X.KeyPress:
                    if ev.detail - 8 == self._toggle_keycode:
                        # The passive XGrabKey converted to an active grab on
                        # _poll_display.  Release it now so XGrabKeyboard on
                        # _display (a different X11 client) doesn't get
                        # AlreadyGrabbed.
                        self._poll_display.ungrab_keyboard(X.CurrentTime)
                        self._poll_display.flush()
                        if self.remote_mode:
                            self._leave_remote()
                        else:
                            self._enter_remote()
        except Exception:
            pass

    @staticmethod
    def _get_primary_monitor_bounds():
        """Return (x, y, w, h) of the primary monitor via xrandr."""
        try:
            r = subprocess.run(['xrandr'], capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                if ' primary ' in line:
                    m = re.search(r'(\d+)x(\d+)\+(\d+)\+(\d+)', line)
                    if m:
                        return (int(m.group(3)), int(m.group(4)),
                                int(m.group(1)), int(m.group(2)))
        except Exception:
            pass
        return None

    @staticmethod
    def _get_physical_desktop_bounds():
        """Return (min_x, min_y, max_x, max_y) spanning all connected monitors."""
        try:
            r = subprocess.run(['xrandr'], capture_output=True, text=True, timeout=3)
            monitors = []
            for line in r.stdout.splitlines():
                if ' connected' in line:
                    m = re.search(r'(\d+)x(\d+)\+(\d+)\+(\d+)', line)
                    if m:
                        w, h, x, y = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)), int(m.group(4)))
                        monitors.append((x, y, w, h))
            if monitors:
                return (min(x       for x, y, w, h in monitors),
                        min(y       for x, y, w, h in monitors),
                        max(x + w   for x, y, w, h in monitors),
                        max(y + h   for x, y, w, h in monitors))
        except Exception:
            pass
        return None

    def _choose_backend(self, method: str):
        if method == 'x11':
            # If physical mice are present, use them via evdev for unbounded
            # movement (Barrier cannot block evdev).  Keyboard + Barrier cursor
            # suppression still go through X11 grab.
            _, mice = _find_keyboards_and_mice()
            if mice:
                # Physical mice are grabbed via evdev, so only Barrier XTEST
                # events reach X11.  suppress_mouse=False forwards those Barrier
                # cursor deltas to Android (no double-movement risk).
                cap = self._make_x11_backend(suppress_mouse=False)
                logger.info(f"Capture backend: mixed "
                            f"(X11 keyboard + {len(mice)} evdev mouse/mice)")
                return {'type': 'mixed', 'capture': cap['capture'], 'mice': mice}
            cap = self._make_x11_backend()
            logger.info("Capture backend: X11 grab (Barrier-compatible)")
            return {'type': 'x11', 'capture': cap['capture']}

        # evdev or auto
        keyboards, mice = _find_keyboards_and_mice()
        if keyboards or mice:
            logger.info(f"evdev: {len(keyboards)} keyboard(s), {len(mice)} mouse/mice")
            return {'type': 'evdev', 'keyboards': keyboards, 'mice': mice}

        if method == 'auto':
            logger.info("No evdev devices found → falling back to X11 grab "
                        "(Barrier mode)")
            cap = self._make_x11_backend()
            logger.info("Capture backend: X11 grab (Barrier-compatible)")
            return {'type': 'x11', 'capture': cap['capture']}

        raise RuntimeError("No evdev input devices found. Run as root?")

    def _make_x11_backend(self, suppress_mouse: bool = False):
        from x11_grab import X11GrabCapture
        cap = X11GrabCapture(self._display, self._root,
                             suppress_mouse=suppress_mouse)
        return {'type': 'x11', 'capture': cap}

    # ------------------------------------------------------------------ #
    # X11 helpers

    def _mouse_pos(self):
        p = self._poll_root.query_pointer()
        return p.root_x, p.root_y

    def _warp(self, x, y):
        self._poll_root.warp_pointer(x, y)
        self._poll_display.flush()

    def _at_edge(self, x, y) -> bool:
        t = self._config.get('edge_threshold', 3)
        edge = self._config.get('edge', 'right')
        if edge == 'right':  return x >= self._desk_x1 - t
        if edge == 'left':   return x <= self._desk_x0 + t
        if edge == 'bottom': return y >= self._desk_y1 - t
        if edge == 'top':    return y <= self._desk_y0 + t
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

        cx = self._mon_x + self._mon_w // 2
        cy = self._mon_y + self._mon_h // 2

        if self._backend['type'] == 'evdev':
            for dev in self._backend['keyboards'] + self._backend['mice']:
                try:
                    dev.grab()
                except Exception as e:
                    logger.warning(f"evdev grab {dev.path}: {e}")
        elif self._backend['type'] == 'mixed':
            # Physical mice via evdev (unbounded, bypasses Barrier transfer).
            # X11 grab for keyboard + re-warp to suppress Barrier cursor transfer.
            for dev in self._backend['mice']:
                try:
                    dev.grab()
                except Exception as e:
                    logger.warning(f"evdev grab mouse {dev.path}: {e}")
            self._warp(cx, cy)
            self._backend['capture'].grab(warp_x=cx, warp_y=cy)
        else:
            self._warp(cx, cy)
            self._backend['capture'].grab(warp_x=cx, warp_y=cy)

        self._warp(cx, cy)
        self._ignore_motion_until = time.time() + 0.25
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
        elif self._backend['type'] == 'mixed':
            for dev in self._backend['mice']:
                try:
                    dev.ungrab()
                except Exception:
                    pass
            self._backend['capture'].ungrab()
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
        try:
            from Xlib import X
            self._poll_root.ungrab_key(self._toggle_keycode + 8, X.AnyModifier)
            self._poll_display.flush()
        except Exception:
            pass

    def _edge_loop(self):
        while self._running:
            self._check_hotkey_events()
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
        elif self._backend['type'] == 'mixed':
            # Physical mice via evdev thread; keyboard via x11 in this thread.
            threading.Thread(target=self._evdev_mice_loop,
                             daemon=True, name="evdev-mice").start()
            self._x11_loop()
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
                                event.code == self._toggle_keycode and
                                event.value == 1):
                            self._leave_remote()
                            break
                        if event.type == ecodes.EV_REL:
                            if event.code == ecodes.REL_X:
                                self._virt_x += event.value
                            elif event.code == ecodes.REL_Y:
                                self._virt_y += event.value
                            if (self._config.get('mouse_return', True) and
                                    self._past_return_edge()):
                                self._leave_remote()
                                break
                        if self.event_callback:
                            self.event_callback(event)
                except OSError:
                    pass

    # ---- evdev mice-only loop (mixed mode) ----

    def _evdev_mice_loop(self):
        """Handles physical mouse movement in mixed mode (no keyboard, no Scroll Lock)."""
        mice = self._backend['mice']
        while self._running:
            if not self.remote_mode:
                time.sleep(0.02)
                continue
            fd_map = {d.fd: d for d in mice}
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
                        if event.type == ecodes.EV_REL:
                            if event.code == ecodes.REL_X:
                                self._virt_x += event.value
                            elif event.code == ecodes.REL_Y:
                                self._virt_y += event.value
                            if (self._config.get('mouse_return', True) and
                                    self._past_return_edge()):
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
                    if code == self._toggle_keycode and value == 1:
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
                    if time.time() < self._ignore_motion_until:
                        continue  # discard warp-induced motion after entering remote
                    self._virt_x += dx
                    self._virt_y += dy
                    if (self._config.get('mouse_return', True) and
                            self._past_return_edge()):
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
