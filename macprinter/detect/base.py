"""Detector interface and the internet reachability check shared by real detectors."""

import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class DongleInfo:
    key: str                     # stable id of the attachment (iface name / ifIndex / sim id)
    iface: str
    mac: str                     # label format, 9C-69-D3-9C-12-65
    permanent: bool | None       # True = MAC burned into the dongle; None = cannot tell
    driver: str = ""
    vid_pid: str = ""
    serial: str = ""
    description: str = ""
    link_up: bool = False
    speed_mbps: int | None = None
    ipv4: str | None = None      # None until DHCP gives a (non link-local) address
    note: str = ""


class Detector:
    name = "base"
    poll_interval = 1.0

    def snapshot(self) -> dict[str, DongleInfo]:
        """All USB Ethernet adapters currently attached, keyed by DongleInfo.key."""
        raise NotImplementedError

    def connectivity(self, info: DongleInfo, url: str, timeout: float) -> tuple[bool, str]:
        """Fetch `url` *through this dongle*. Returns (ok, human-readable detail)."""
        raise NotImplementedError


def http_check(url: str, timeout: float, prepare: Callable[[socket.socket], None]) -> tuple[bool, str]:
    """Plain-HTTP GET on a socket that `prepare` pins to one interface.

    Default URL is Google's connectivity check, which answers 204 No Content.
    DNS resolution uses the host's normal resolver, not the dongle.
    """
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http":
        return False, "only http:// URLs are supported"
    host, port, path = u.hostname, u.port or 80, (u.path or "/") + (f"?{u.query}" if u.query else "")
    try:
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
    except OSError as e:
        return False, f"DNS lookup of {host} failed: {e}"
    t0 = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            prepare(s)
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: macprinter\r\n"
                      f"Connection: close\r\n\r\n".encode())
            status_line = s.recv(128).split(b"\r\n", 1)[0].decode("latin-1")
    except PermissionError as e:
        return False, f"not permitted ({e}); the service needs CAP_NET_RAW on Linux"
    except OSError as e:
        return False, f"{host}: {e}"
    ms = (time.monotonic() - t0) * 1000
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return False, f"unexpected reply {status_line!r}"
    code = int(parts[1])
    return code in (200, 204), f"HTTP {code} from {host} in {ms:.0f} ms"
