"""Linux detector: polls /sys/class/net for USB Ethernet interfaces.

The interface must be administratively up for carrier/speed to be readable and
for DHCP to run; on the Pi, NetworkManager does that (see deploy/README.md).
"""

import fcntl
import os
import socket
import struct
from pathlib import Path

from ..mac import InvalidMac, normalize
from .base import Detector, DongleInfo, http_check

SYS_NET = Path("/sys/class/net")
SO_BINDTODEVICE = 25
SIOCGIFADDR = 0x8915


def _read(p: Path, default: str = "") -> str:
    try:
        return p.read_text().strip()
    except OSError:              # e.g. 'carrier' on a down interface raises EINVAL
        return default


def _ipv4(iface: str) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            raw = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", iface[:15].encode()))
        ip = socket.inet_ntoa(raw[20:24])
    except OSError:
        return None
    return None if ip.startswith("169.254.") else ip


class LinuxDetector(Detector):
    name = "linux"
    poll_interval = 0.5

    def snapshot(self) -> dict[str, DongleInfo]:
        out = {}
        for dev in SYS_NET.iterdir():
            link = dev / "device"
            if not link.exists() or (dev / "wireless").exists():
                continue
            intf = link.resolve()
            if "/usb" not in str(intf):
                continue
            try:
                mac = normalize(_read(dev / "address"))
            except InvalidMac:
                continue
            usb = intf.parent
            aat = _read(dev / "addr_assign_type")
            speed = _read(dev / "speed", "-1")
            driver = link / "driver"
            out[dev.name] = DongleInfo(
                key=dev.name, iface=dev.name, mac=mac,
                permanent=(aat == "0") if aat else None,
                driver=os.path.basename(os.readlink(driver)) if driver.exists() else "",
                vid_pid=f"{_read(usb / 'idVendor')}:{_read(usb / 'idProduct')}",
                serial=_read(usb / "serial"),
                description=_read(usb / "product"),
                link_up=_read(dev / "carrier") == "1",
                speed_mbps=int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
                ipv4=_ipv4(dev.name),
                note="" if aat in ("0", "") else f"addr_assign_type={aat}",
            )
        return out

    def connectivity(self, info: DongleInfo, url: str, timeout: float) -> tuple[bool, str]:
        def pin(s: socket.socket) -> None:
            s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, info.iface.encode() + b"\0")
        return http_check(url, timeout, pin)
