"""X11 edge detection + evdev input capture (PC-attached devices only)."""
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

        keyname = config.get('toggle_key', 'KEY_PAUSE')
        self._toggle_keycode = getattr(ecodes, keyname, ecodes.KEY_PAUSE)

        self._init_x11()

        keyboards, mice = _find_keyboards_and_mice()
        if not keyboards and not mice:
            raise RuntimeError("No evdev input devices found. Run as root?")
        self._keyboards = keyboards
        self._mice = mice
        logger.info(f"evdev: {len(keyboards)} keyboard(s), {len(mice)} mouse/mice")

        self._running = False
        self._edge_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # X11 init (edge detection + hotkey passive grab only)

    def _init_x11(self):
        from Xlib import display as xdisp
        try:
            d = xdisp.Display()
            screen = d.screen()
            self._screen_w = screen.width_in_pixels
            self._screen_h = screen.height_in_pixels
            self._poll_root    = screen.root
            self._poll_display = d
        except Exception as e:
            raise RuntimeError(f"X11 init failed: {e}. Is DISPLAY set?")

        logger.info(f"X11 desktop: {self._screen_w}x{self._screen_h}")

        bounds = self._get_primary_monitor_bounds()
        if bounds:
            ox, oy, ow, oh = bounds
        else:
            ox, oy, ow, oh = 0, 0, self._screen_w, self._screen_h
        self._mon_x, self._mon_y = ox, oy
        self._mon_w, self._mon_h = ow, oh

        db = self._get_physical_desktop_bounds()
        if db:
            self._desk_x0, self._desk_y0, self._desk_x1, self._desk_y1 = db
        else:
            self._desk_x0, self._desk_y0 = 0, 0
            self._desk_x1, self._desk_y1 = self._screen_w, self._screen_h
        logger.info(f"Physical desktop: ({self._desk_x0},{self._desk_y0})"
                    f"–({self._desk_x1},{self._desk_y1})"
                    f"  warp target: {ow}x{oh} at ({ox},{oy})")

        self._register_toggle_hotkey()

    def _register_toggle_hotkey(self):
        from Xlib import X
        keycode = self._toggle_keycode + 8
        try:
            self._poll_root.grab_key(keycode, X.AnyModifier, True,
                                     X.GrabModeAsync, X.GrabModeAsync)
            self._poll_display.flush()
            keyname = self._config.get('toggle_key', 'KEY_PAUSE')
            logger.info(f"Toggle hotkey: {keyname} (X11 keycode {keycode})")
        except Exception as e:
            logger.warning(f"XGrabKey failed: {e}")

    def _check_hotkey_events(self):
        from Xlib import X
        try:
            while self._poll_display.pending_events():
                ev = self._poll_display.next_event()
                if ev.type == X.KeyPress and ev.detail - 8 == self._toggle_keycode:
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
                return (min(x     for x, y, w, h in monitors),
                        min(y     for x, y, w, h in monitors),
                        max(x + w for x, y, w, h in monitors),
                        max(y + h for x, y, w, h in monitors))
        except Exception:
            pass
        return None

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

        for dev in self._keyboards + self._mice:
            try:
                dev.grab()
            except Exception as e:
                logger.warning(f"evdev grab {dev.path}: {e}")

        cx = self._mon_x + self._mon_w // 2
        cy = self._mon_y + self._mon_h // 2
        self._warp(cx, cy)

        ret = RETURN_EDGE[self._config.get('edge', 'right')]
        logger.info(f">>> Android  (마우스를 {ret}으로 밀거나 {self._config.get('toggle_key','KEY_PAUSE')} 복귀)")
        self._on_enter_remote()

    def _leave_remote(self):
        if not self.remote_mode:
            return
        self.remote_mode = False

        for dev in self._keyboards + self._mice:
            try:
                dev.ungrab()
            except Exception:
                pass

        logger.info("<<< Linux")
        self._on_leave_remote()

    # ------------------------------------------------------------------ #
    # Thread loops

    def start(self):
        self._running = True
        self._edge_thread = threading.Thread(
            target=self._edge_loop, daemon=True, name="edge-detect")
        self._event_thread = threading.Thread(
            target=self._evdev_loop, daemon=True, name="evdev-reader")
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

    def _evdev_loop(self):
        while self._running:
            if not self.remote_mode:
                time.sleep(0.02)
                continue
            fd_map = {d.fd: d for d in self._keyboards + self._mice}
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
