import pytest

from macprinter.mac import InvalidMac, is_labelable, is_locally_administered, normalize


@pytest.mark.parametrize("raw", ["9c:69:d3:9c:12:65", "9C-69-D3-9C-12-65", "9c69.d39c.1265", "9C69D39C1265"])
def test_normalize_formats(raw):
    assert normalize(raw) == "9C-69-D3-9C-12-65"


@pytest.mark.parametrize("raw", ["", "9C-69-D3-9C-12", "9C-69-D3-9C-12-6G", "9C-69-D3-9C-12-65-00"])
def test_normalize_rejects(raw):
    with pytest.raises(InvalidMac):
        normalize(raw)


def test_address_bits():
    assert is_labelable("9C-69-D3-9C-12-65")
    assert not is_labelable("01-00-5E-00-00-01")      # multicast
    assert not is_labelable("00-00-00-00-00-00")
    assert is_locally_administered("02-00-00-00-00-01")
    assert not is_locally_administered("9C-69-D3-9C-12-65")
