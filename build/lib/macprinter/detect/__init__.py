"""Dongle detectors. All share the Detector interface in base.py."""

import sys

from .base import Detector, DongleInfo


def make_detector(kind: str) -> Detector:
    if kind == "auto":
        kind = {"linux": "linux", "win32": "windows"}.get(sys.platform, "sim")
    if kind == "sim":
        from .sim import SimDetector
        return SimDetector()
    if kind == "linux":
        from .linux import LinuxDetector
        return LinuxDetector()
    if kind == "windows":
        from .windows import WindowsDetector
        return WindowsDetector()
    raise ValueError(f"unknown detector {kind!r}")


__all__ = ["Detector", "DongleInfo", "make_detector"]
