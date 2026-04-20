"""Bluetooth HID peripheral: Linux acts as BT keyboard+mouse for Android."""
import logging
import socket
import subprocess
import tempfile
import threading
import os

from hid_reports import HID_DESCRIPTOR

logger = logging.getLogger(__name__)

P_CTRL = 17  # L2CAP PSM: HID Control
P_INTR = 19  # L2CAP PSM: HID Interrupt


def _build_sdp_xml(device_name: str) -> str:
    desc_hex = HID_DESCRIPTOR.hex()
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001">
    <sequence><uuid value="0x1124"/></sequence>
  </attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
      <sequence><uuid value="0x0011"/></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005">
    <sequence><uuid value="0x1002"/></sequence>
  </attribute>
  <attribute id="0x0009">
    <sequence>
      <sequence><uuid value="0x1124"/><uint16 value="0x0100"/></sequence>
    </sequence>
  </attribute>
  <attribute id="0x000d">
    <sequence>
      <sequence>
        <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
        <sequence><uuid value="0x0011"/></sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100"><text value="{device_name}"/></attribute>
  <attribute id="0x0101"><text value="Keyboard/Mouse KVM"/></attribute>
  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
  <attribute id="0x0201"><uint8  value="0x40"/></attribute>
  <attribute id="0x0202"><uint8  value="0x00"/></attribute>
  <attribute id="0x0203"><uint8  value="0x00"/></attribute>
  <attribute id="0x0204"><boolean value="false"/></attribute>
  <attribute id="0x0205"><boolean value="false"/></attribute>
  <attribute id="0x0206">
    <sequence>
      <sequence>
        <uint8 value="0x22"/>
        <text encoding="hex" value="{desc_hex}"/>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0207">
    <sequence>
      <sequence><uint16 value="0x0409"/><uint16 value="0x0100"/></sequence>
    </sequence>
  </attribute>
  <attribute id="0x020b"><uint16 value="0x0100"/></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
  <attribute id="0x020d"><boolean value="false"/></attribute>
  <attribute id="0x020e"><boolean value="false"/></attribute>
  <attribute id="0x020f"><uint16 value="0x0640"/></attribute>
  <attribute id="0x0210"><uint16 value="0x0320"/></attribute>
</record>"""


class BluetoothHID:
    def __init__(self, device_name: str = "Linux KVM"):
        self.device_name = device_name
        self._ctrl_server = None
        self._intr_server = None
        self._ctrl_client = None
        self._intr_client = None
        self.connected = False

    def setup(self):
        """Configure BT adapter as HID peripheral and register SDP record."""
        logger.info("Configuring Bluetooth adapter...")
        cmds = [
            ['hciconfig', 'hci0', 'up'],
            ['hciconfig', 'hci0', 'class', '0x002540'],
            ['hciconfig', 'hci0', 'name', self.device_name],
            ['hciconfig', 'hci0', 'piscan'],
        ]
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0:
                logger.warning(f"{' '.join(cmd)}: {r.stderr.decode().strip()}")

        self._register_sdp()

    def _register_sdp(self):
        xml = _build_sdp_xml(self.device_name)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml',
                                         delete=False) as f:
            f.write(xml)
            xml_path = f.name

        try:
            r = subprocess.run(
                ['sdptool', 'add', '--handle=0x00010001', f'--xml={xml_path}'],
                capture_output=True
            )
            if r.returncode != 0:
                # fallback: without explicit handle
                r = subprocess.run(
                    ['sdptool', 'add', f'--xml={xml_path}'],
                    capture_output=True
                )
            if r.returncode != 0:
                logger.error(f"sdptool failed: {r.stderr.decode().strip()}")
                logger.error("Continuing anyway - Android may still connect")
            else:
                logger.info("SDP HID record registered.")
        finally:
            os.unlink(xml_path)

    def listen(self):
        """Block until Android connects on both HID channels."""
        self._ctrl_server = self._make_l2cap_socket(P_CTRL)
        self._intr_server = self._make_l2cap_socket(P_INTR)

        logger.info("Waiting for Android connection "
                    f"(pair '{self.device_name}' in Android BT settings)...")

        # Accept both channels concurrently — Android may connect PSM 19
        # before PSM 17, so sequential accept() can deadlock.
        results: dict = {}
        errs: dict = {}

        def _accept(server, key):
            try:
                results[key] = server.accept()
            except OSError as e:
                errs[key] = e

        t_ctrl = threading.Thread(target=_accept,
                                   args=(self._ctrl_server, 'ctrl'), daemon=True)
        t_intr = threading.Thread(target=_accept,
                                   args=(self._intr_server, 'intr'), daemon=True)
        t_ctrl.start()
        t_intr.start()
        t_ctrl.join()
        t_intr.join()

        if errs:
            raise OSError(f"L2CAP accept failed: {errs}")

        self._ctrl_client, ctrl_addr = results['ctrl']
        self._intr_client, intr_addr = results['intr']
        logger.info(f"Control channel: {ctrl_addr[0]}")
        logger.info(f"Interrupt channel: {intr_addr[0]}")
        self.connected = True

    def send(self, report: bytes):
        if not self.connected:
            return
        try:
            self._intr_client.send(report)
        except OSError as e:
            logger.warning(f"BT send error: {e}")
            self.connected = False

    def close(self):
        self.connected = False
        for s in [self._ctrl_client, self._intr_client,
                  self._ctrl_server, self._intr_server]:
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self._ctrl_client = self._intr_client = None
        self._ctrl_server = self._intr_server = None

    @staticmethod
    def _make_l2cap_socket(psm: int) -> socket.socket:
        s = socket.socket(socket.AF_BLUETOOTH,
                          socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", psm))
        s.listen(1)
        return s
