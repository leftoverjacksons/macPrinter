"""Detector parsing tests with faked OS data (no hardware needed)."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from macprinter.detect import windows


def _fake_ps(monkeypatch, rows):
    out = json.dumps(rows)
    monkeypatch.setattr(windows.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out, returncode=0))
    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        monkeypatch.setattr(windows.subprocess, "CREATE_NO_WINDOW", 0, raising=False)


ROW = {"Name": "Ethernet 3", "Index": 17, "Desc": "ASIX AX88179 USB 3.0 to Gigabit Ethernet Adapter",
       "Mac": "9C-69-D3-9C-12-65", "Perm": "9C69D39C1265", "Status": "Up", "Speed": 1000000000,
       "Pnp": "USB\\VID_0B95&PID_1790\\000000000000E7", "IPv4": "192.168.1.50"}


def test_windows_parses_adapter(monkeypatch):
    _fake_ps(monkeypatch, ROW)                       # single object, as PowerShell emits for one result
    info = windows.WindowsDetector().snapshot()["if17"]
    assert info.mac == "9C-69-D3-9C-12-65" and info.permanent is True
    assert info.vid_pid == "0b95:1790" and info.speed_mbps == 1000 and info.link_up
    assert info.ipv4 == "192.168.1.50" and info.note == ""


def test_windows_labels_permanent_address_when_overridden(monkeypatch):
    _fake_ps(monkeypatch, [dict(ROW, Mac="02-11-22-33-44-55")])
    info = windows.WindowsDetector().snapshot()["if17"]
    assert info.mac == "9C-69-D3-9C-12-65"
    assert "overriding" in info.note


def test_windows_disconnected(monkeypatch):
    _fake_ps(monkeypatch, [dict(ROW, Status="Disconnected", IPv4=None, Perm="")])
    info = windows.WindowsDetector().snapshot()["if17"]
    assert not info.link_up and info.speed_mbps is None and info.permanent is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux sysfs")
def test_linux_snapshot_runs():
    from macprinter.detect.linux import LinuxDetector
    assert isinstance(LinuxDetector().snapshot(), dict)   # no USB NICs in CI: just must not crash
