"""MAC address parsing and formatting."""

import re

_HEX12 = re.compile(r"^[0-9A-F]{12}$")


class InvalidMac(ValueError):
    pass


def normalize(mac: str) -> str:
    """Return MAC in label format: uppercase, dash-separated (9C-69-D3-9C-12-65).

    Accepts colon, dash, dot (Cisco) or no separators, any case.
    """
    digits = re.sub(r"[:\-.\s]", "", mac).upper()
    if not _HEX12.match(digits):
        raise InvalidMac(f"not a MAC address: {mac!r}")
    return "-".join(digits[i:i + 2] for i in range(0, 12, 2))


def first_octet(mac: str) -> int:
    return int(normalize(mac)[:2], 16)


def is_multicast(mac: str) -> bool:
    return bool(first_octet(mac) & 0x01)


def is_locally_administered(mac: str) -> bool:
    return bool(first_octet(mac) & 0x02)


def is_labelable(mac: str) -> bool:
    """A unicast, non-zero address. Locally administered is allowed (warned elsewhere)."""
    m = normalize(mac)
    return not is_multicast(m) and m != "00-00-00-00-00-00"
