"""In-memory simulated dongles, driven from the dashboard. Works on any OS."""

import random
import threading
import time
from dataclasses import dataclass

from .base import Detector, DongleInfo


@dataclass
class _SimDongle:
    key: str
    mac: str
    permanent: bool
    cable: bool
    internet: bool
    plugged_at: float
    cable_at: float
    n: int


class SimDetector(Detector):
    name = "sim"
    poll_interval = 0.3
    LINK_DELAY = 1.0     # seconds from cable-in to carrier
    DHCP_DELAY = 1.5     # seconds from carrier to IPv4 address

    def __init__(self):
        self._lock = threading.Lock()
        self._devs: dict[str, _SimDongle] = {}
        self._n = 0

    def plug(self, mac: str | None = None, permanent: bool = True, cable: bool = True,
             internet: bool = True) -> str:
        from ..mac import normalize
        with self._lock:
            self._n += 1
            key = f"sim{self._n}"
            mac = normalize(mac) if mac else "9C-69-D3-" + "-".join(f"{random.randrange(256):02X}" for _ in range(3))
            now = time.monotonic()
            self._devs[key] = _SimDongle(key, mac, permanent, cable, internet, now, now, self._n)
            return key

    def unplug(self, key: str) -> None:
        with self._lock:
            self._devs.pop(key, None)

    def update(self, key: str, cable: bool | None = None, internet: bool | None = None) -> None:
        with self._lock:
            d = self._devs[key]
            if cable is not None and cable != d.cable:
                d.cable, d.cable_at = cable, time.monotonic()
            if internet is not None:
                d.internet = internet

    def sim_state(self) -> dict[str, dict]:
        with self._lock:
            return {k: {"cable": d.cable, "internet": d.internet} for k, d in self._devs.items()}

    def snapshot(self) -> dict[str, DongleInfo]:
        now = time.monotonic()
        out = {}
        with self._lock:
            for d in self._devs.values():
                link = d.cable and now - d.cable_at >= self.LINK_DELAY
                has_ip = link and now - d.cable_at >= self.LINK_DELAY + self.DHCP_DELAY
                out[d.key] = DongleInfo(
                    key=d.key, iface=f"enx{d.mac.replace('-', '').lower()}", mac=d.mac,
                    permanent=d.permanent, driver="ax88179_178a (sim)", vid_pid="0b95:1790",
                    description="Simulated AX88179", link_up=link, speed_mbps=1000 if link else None,
                    ipv4=f"192.168.50.{100 + d.n}" if has_ip else None)
        return out

    def connectivity(self, info: DongleInfo, url: str, timeout: float) -> tuple[bool, str]:
        time.sleep(0.4)
        with self._lock:
            d = self._devs.get(info.key)
            ok = bool(d and d.internet)
        return (True, "HTTP 204 from connectivitycheck.gstatic.com in 23 ms (simulated)") if ok \
            else (False, f"connect timed out after {timeout:.0f} s (simulated)")
