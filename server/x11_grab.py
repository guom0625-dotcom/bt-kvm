"""X11 grab-based input capture (XGrabKeyboard + XGrabPointer).

Works with Barrier-injected events that never reach /dev/input/.
X11 keycode = evdev keycode + 8, so we can reuse hid_reports KEY_MAP directly.
"""
import logging
import queue
import select
import threading
import time

from Xlib import X
from evdev import ecodes

logger = logging.getLogger(__name__)

# X11 button → evdev button code
_BTN_MAP = {
    1: ecodes.BTN_LEFT,
    2: ecodes.BTN_MIDDLE,
    3: ecodes.BTN_RIGHT,
    8: ecodes.BTN_SIDE,
    9: ecodes.BTN_EXTRA,
}
_SCROLL_UP   = 4
_SCROLL_DOWN = 5


class X11GrabCapture:
    """Grabs all keyboard and pointer input at X11 level.

    Produces synthetic evdev-style events so the rest of the pipeline
    (hid_reports, main.py) needs no changes.
    """

    def __init__(self, display_obj, root):
        self._d  = display_obj
        self._root = root
        self._grabbed = False
        self._prev_x = 0
        self._prev_y = 0
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()   # serializes all Xlib calls
        self._thread = threading.Thread(target=self._event_loop,
                                        daemon=True, name="x11-grab-reader")
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Public API

    def grab(self, warp_x: int = None, warp_y: int = None):
        with self._lock:
            if warp_x is not None and warp_y is not None:
                self._prev_x = warp_x
                self._prev_y = warp_y
            else:
                p = self._root.query_pointer()
                self._prev_x = p.root_x
                self._prev_y = p.root_y
            self._root.grab_keyboard(
                True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime
            )
            self._root.grab_pointer(
                False,  # owner_events=False: all events go only to us
                X.PointerMotionMask | X.ButtonPressMask | X.ButtonReleaseMask,
                X.GrabModeAsync, X.GrabModeAsync,
                X.NONE, X.NONE, X.CurrentTime,
            )
            self._d.flush()
            self._grabbed = True
            self._set_cursor_visible(False)
        logger.debug("X11 grab active")

    def ungrab(self):
        with self._lock:
            self._grabbed = False
            self._d.ungrab_keyboard(X.CurrentTime)
            self._d.ungrab_pointer(X.CurrentTime)
            self._d.flush()
            self._set_cursor_visible(True)
        # drain leftover events
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        logger.debug("X11 grab released")

    def read(self, timeout: float = 0.1):
        """Return list of synthetic evdev-like event tuples."""
        events = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                events.append(self._q.get(timeout=min(remaining, 0.02)))
            except queue.Empty:
                break
        return events

    # ------------------------------------------------------------------ #
    # Internal

    def _set_cursor_visible(self, visible: bool):
        try:
            from Xlib.ext import xfixes
            if visible:
                xfixes.show_cursor(self._d, self._root)
            else:
                xfixes.hide_cursor(self._d, self._root)
            self._d.flush()
        except Exception:
            pass

    def _event_loop(self):
        """Non-blocking X11 event reader: select + lock to serialize Xlib calls."""
        fd = self._d.fileno()
        while True:
            try:
                readable, _, _ = select.select([fd], [], [], 0.05)
                if not readable:
                    continue
                with self._lock:
                    while self._d.pending_events():
                        ev = self._d.next_event()
                        if self._grabbed:
                            self._dispatch(ev)
            except Exception as e:
                logger.debug(f"x11 event_loop: {e}")
                time.sleep(0.01)

    def _dispatch(self, ev):
        t = ev.type

        if t in (X.KeyPress, X.KeyRelease):
            evdev_code = ev.detail - 8          # X11 keycode → evdev keycode
            value = 1 if t == X.KeyPress else 0
            self._q.put(('key', evdev_code, value))

        elif t == X.ButtonPress:
            if ev.detail == _SCROLL_UP:
                self._q.put(('scroll', 1))
            elif ev.detail == _SCROLL_DOWN:
                self._q.put(('scroll', -1))
            elif ev.detail in _BTN_MAP:
                self._q.put(('btn', _BTN_MAP[ev.detail], 1))

        elif t == X.ButtonRelease:
            if ev.detail in _BTN_MAP:
                self._q.put(('btn', _BTN_MAP[ev.detail], 0))

        elif t == X.MotionNotify:
            dx = ev.root_x - self._prev_x
            dy = ev.root_y - self._prev_y
            self._prev_x = ev.root_x
            self._prev_y = ev.root_y
            if dx or dy:
                self._q.put(('move', dx, dy))
