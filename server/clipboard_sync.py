"""Bidirectional clipboard sync over RFCOMM.

Linux ←→ Android via a simple line protocol:
  CLIP:<base64(utf-8)>\\n   — clipboard content
  PING\\n / PONG\\n          — keepalive
"""
import base64
import logging
import socket
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

RFCOMM_CHANNEL = 4          # must match Android app constant
POLL_INTERVAL  = 0.5        # seconds between clipboard polls on Linux


def _clip_get() -> Optional[str]:
    try:
        r = subprocess.run(
            ['xclip', '-selection', 'clipboard', '-o'],
            capture_output=True, timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.decode('utf-8', errors='replace')
    except Exception:
        pass
    return None


def _clip_set(text: str):
    try:
        subprocess.run(
            ['xclip', '-selection', 'clipboard', '-i'],
            input=text.encode('utf-8'),
            capture_output=True, timeout=2,
        )
    except Exception as e:
        logger.error(f"xclip set: {e}")


class ClipboardSync:
    def __init__(self):
        self._server_sock = None
        self._client_sock = None
        self._connected   = False
        self._last_clip   = None   # avoids re-sending what we just received
        self._running     = False
        self._lock        = threading.Lock()

    # ------------------------------------------------------------------ #

    def start(self):
        self._running  = True
        self._last_clip = _clip_get()
        threading.Thread(target=self._accept_loop,
                         daemon=True, name="clip-accept").start()
        threading.Thread(target=self._monitor_loop,
                         daemon=True, name="clip-monitor").start()
        logger.info(f"Clipboard sync ready (RFCOMM channel {RFCOMM_CHANNEL})")

    def stop(self):
        self._running = False
        self._close_client()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #

    def _accept_loop(self):
        while self._running:
            try:
                self._server_sock = socket.socket(
                    socket.AF_BLUETOOTH,
                    socket.SOCK_STREAM,
                    socket.BTPROTO_RFCOMM,
                )
                self._server_sock.setsockopt(socket.SOL_SOCKET,
                                              socket.SO_REUSEADDR, 1)
                self._server_sock.bind(("", RFCOMM_CHANNEL))
                self._server_sock.listen(1)
                logger.info("Clipboard: waiting for Android…")
                client, addr = self._server_sock.accept()
                with self._lock:
                    self._client_sock = client
                    self._connected   = True
                logger.info(f"Clipboard: connected ({addr[0]})")
                self._recv_loop()
            except OSError as e:
                logger.debug(f"clip accept: {e}")
            finally:
                self._close_client()
                try:
                    self._server_sock.close()
                except Exception:
                    pass
                if self._running:
                    time.sleep(3)

    def _recv_loop(self):
        buf = b""
        while self._running and self._connected:
            try:
                data = self._client_sock.recv(8192)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._handle(raw.decode("utf-8", errors="replace").strip())
            except OSError:
                break
        self._close_client()

    def _handle(self, line: str):
        if line.startswith("CLIP:"):
            try:
                text = base64.b64decode(line[5:]).decode("utf-8")
            except Exception as e:
                logger.error(f"clip decode: {e}")
                return
            logger.info(f"← Android clipboard ({len(text)} chars)")
            self._last_clip = text
            _clip_set(text)
        elif line == "PING":
            self._send_raw("PONG\n")

    def _monitor_loop(self):
        """Detect Linux clipboard changes and push to Android."""
        while self._running:
            time.sleep(POLL_INTERVAL)
            if not self._connected:
                continue
            clip = _clip_get()
            if clip and clip != self._last_clip:
                self._last_clip = clip
                encoded = base64.b64encode(clip.encode("utf-8")).decode()
                logger.info(f"→ Android clipboard ({len(clip)} chars)")
                self._send_raw(f"CLIP:{encoded}\n")

    def _send_raw(self, msg: str):
        with self._lock:
            sock = self._client_sock
        if sock:
            try:
                sock.sendall(msg.encode("utf-8"))
            except OSError as e:
                logger.debug(f"clip send: {e}")
                self._connected = False

    def _close_client(self):
        with self._lock:
            self._connected = False
            sock, self._client_sock = self._client_sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
