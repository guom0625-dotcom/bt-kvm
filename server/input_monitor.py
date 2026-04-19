"""X11 edge detection + evdev capture for keyboard and mouse."""
import logging
import select
import threading
import time
from typing import Callable, Optional

from evdev import InputDevice, list_devices, ecodes

logger = logging.getLogger(__name__)

# Return-edge overshoot required to trigger switch-back (pixels of relative movement)
DEFAULT_RETURN_THRESHOLD = 80

# Fallback hotkey: Scroll Lock
SWITCH_BACK_KEY = ecodes.KEY_SCROLLLOCK

# Return edge by configured edge
RETURN_EDGE = {
    'right':  'left',
    'left':   'right',
    'top':    'bottom',
    'bottom': 'top',
}


def _find_keyboards_and_mice() -> tuple[list[InputDevice], list[InputDevice]]:
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

        # Virtual cursor: tracks relative movement while in remote mode
        self._virt_x = 0
        self._virt_y = 0

        self._keyboards, self._mice = _find_keyboards_and_mice()
        if not self._keyboards and not self._mice:
            raise RuntimeError("No input devices found. Run as root?")
        logger.info(f"Found {len(self._keyboards)} keyboard(s), "
                    f"{len(self._mice)} mouse/mice")

        self._display = None
        self._screen_w = 0
        self._screen_h = 0
        self._init_x11()

        self._running = False
        self._edge_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None

    def _init_x11(self):
        try:
            from Xlib import display as xdisp
            d = xdisp.Display()
            screen = d.screen()
            self._display = d
            self._root = screen.root
            self._screen_w = screen.width_in_pixels
            self._screen_h = screen.height_in_pixels
            logger.info(f"X11 screen: {self._screen_w}x{self._screen_h}")
        except Exception as e:
            raise RuntimeError(f"X11 init failed: {e}. Is DISPLAY set?")

    def _mouse_pos(self) -> tuple[int, int]:
        p = self._root.query_pointer()
        return p.root_x, p.root_y

    def _warp(self, x: int, y: int):
        self._root.warp_pointer(x, y)
        self._display.flush()

    def _at_edge(self, x: int, y: int) -> bool:
        t = self._config.get('edge_threshold', 3)
        edge = self._config.get('edge', 'right')
        if edge == 'right':
            return x >= self._screen_w - t
        if edge == 'left':
            return x <= t
        if edge == 'bottom':
            return y >= self._screen_h - t
        if edge == 'top':
            return y <= t
        return False

    def _past_return_edge(self) -> bool:
        """True when virtual cursor has been pushed past the return edge."""
        threshold = self._config.get('return_threshold', DEFAULT_RETURN_THRESHOLD)
        edge = self._config.get('edge', 'right')
        ret = RETURN_EDGE[edge]
        if ret == 'left':
            return self._virt_x < -threshold
        if ret == 'right':
            return self._virt_x > threshold
        if ret == 'top':
            return self._virt_y < -threshold
        if ret == 'bottom':
            return self._virt_y > threshold
        return False

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
                logger.warning(f"grab {dev.path}: {e}")
        self._warp(self._screen_w // 2, self._screen_h // 2)
        edge = self._config.get('edge', 'right')
        ret = RETURN_EDGE[edge]
        logger.info(f">>> Android mode  (마우스를 {ret}으로 밀거나 Scroll Lock으로 복귀)")
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
        logger.info("<<< Linux mode")
        self._on_leave_remote()

    def start(self):
        self._running = True
        self._edge_thread = threading.Thread(
            target=self._edge_loop, daemon=True, name="edge-detect")
        self._event_thread = threading.Thread(
            target=self._event_loop, daemon=True, name="evdev-reader")
        self._edge_thread.start()
        self._event_thread.start()

    def stop(self):
        self._running = False
        if self.remote_mode:
            self._leave_remote()

    # ------------------------------------------------------------------ #

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
        while self._running:
            if not self.remote_mode:
                time.sleep(0.02)
                continue

            devices = self._keyboards + self._mice
            if not devices:
                time.sleep(0.1)
                continue

            fd_map = {d.fd: d for d in devices}
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

                        # Scroll Lock → 즉시 복귀 (Scroll Lock은 Android로 안 보냄)
                        if (event.type == ecodes.EV_KEY and
                                event.code == SWITCH_BACK_KEY and
                                event.value == 1):
                            self._leave_remote()
                            break

                        # 가상 커서 위치 추적
                        if event.type == ecodes.EV_REL:
                            if event.code == ecodes.REL_X:
                                self._virt_x += event.value
                            elif event.code == ecodes.REL_Y:
                                self._virt_y += event.value

                            # 반대 경계 이탈 감지 → Linux 복귀
                            if self._past_return_edge():
                                self._leave_remote()
                                break

                        if self.event_callback:
                            self.event_callback(event)
                except OSError:
                    pass
