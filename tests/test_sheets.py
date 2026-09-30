import pytest

from macprinter.sheets import SheetTemplate


@pytest.fixture
def ol25():
    return SheetTemplate.load("OL25SP")


def test_capacity(ol25):
    assert ol25.capacity == 80


def test_geometry_matches_vendor_pdf(ol25):
    # Values read from templates/OL25.pdf (points, top-left origin)
    xs = [ol25.cell_rect(0, c).x for c in range(4)]
    assert xs == pytest.approx([23.76, 170.01, 316.26, 462.51], abs=0.01)
    assert ol25.cell_rect(0, 0).y == pytest.approx(36.0)
    assert ol25.cell_rect(19, 0).y == pytest.approx(720.0)
    r = ol25.cell_rect(0, 0)
    assert (r.w, r.h) == pytest.approx((126.0, 36.0))


def test_cell_out_of_range(ol25):
    with pytest.raises(IndexError):
        ol25.cell_rect(20, 0)


def test_next_free_skips_used_in_row_major_order(ol25):
    used = {(0, 0), (0, 1), (0, 3)}
    assert ol25.next_free(used, 3) == [(0, 2), (1, 0), (1, 1)]


def test_next_free_runs_out(ol25):
    used = set(ol25.cells()[:78])
    assert ol25.next_free(used, 5) == [(19, 2), (19, 3)]
