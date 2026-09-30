"""Windows detector (development convenience; untested on real hardware yet).

Polls Get-NetAdapter via PowerShell. Labels the adapter's PermanentAddress
(the MAC stored in the dongle), not a host-overridden current address.
"""

import json
import socket
import subprocess
import sys

from ..mac import InvalidMac, normalize
from .base import Detector, DongleInfo, http_check

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = @(Get-NetAdapter | Where-Object { $_.PnPDeviceID -like 'USB\*' -and $_.MediaType -eq '802.3' } | ForEach-Object {
  $ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 |
         Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress
  [pscustomobject]@{ Name = $_.Name; Index = $_.ifIndex; Desc = $_.InterfaceDescription;
                     Mac = $_.MacAddress; Perm = $_.PermanentAddress; Status = [string]$_.Status;
                     Speed = $_.Speed; Pnp = $_.PnPDeviceID; IPv4 = $ip }
})
ConvertTo-Json -Compress -InputObject $rows
"""


def _vid_pid(pnp: str) -> str:
    # USB\VID_0B95&PID_1790\...
    try:
        part = pnp.split("\\")[1]
        vid = part.split("VID_")[1][:4]
        pid = part.split("PID_")[1][:4]
        return f"{vid}:{pid}".lower()
    except IndexError:
        return ""


class WindowsDetector(Detector):
    name = "windows"
    poll_interval = 2.0

    def snapshot(self) -> dict[str, DongleInfo]:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
                             capture_output=True, text=True, timeout=15, creationflags=flags)
        text = res.stdout.strip()
        rows = json.loads(text) if text else []
        if isinstance(rows, dict):
            rows = [rows]
        out = {}
        for r in rows:
            try:
                current = normalize(r["Mac"])
                perm = normalize(r["Perm"]) if r.get("Perm") else None
            except (InvalidMac, KeyError, TypeError):
                continue
            up = r.get("Status") == "Up"
            key = f"if{r['Index']}"
            out[key] = DongleInfo(
                key=key, iface=r.get("Name", key), mac=perm or current,
                permanent=True if perm else None,
                driver=r.get("Desc", ""), vid_pid=_vid_pid(r.get("Pnp", "")),
                description=r.get("Desc", ""), link_up=up,
                speed_mbps=int(r["Speed"]) // 1_000_000 if up and r.get("Speed") else None,
                ipv4=r.get("IPv4") or None,
                note=f"Windows is overriding the MAC (current {current})" if perm and perm != current else "",
            )
        return out

    def connectivity(self, info: DongleInfo, url: str, timeout: float) -> tuple[bool, str]:
        # Windows uses the strong host model for sends: binding the source IP selects the interface.
        return http_check(url, timeout, lambda s: s.bind((info.ipv4, 0)))
