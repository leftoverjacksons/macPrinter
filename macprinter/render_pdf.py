"""Render label sheets and alignment pages to PDF.

All placement is absolute. The PDF must be printed at 100% scale; any
"fit to page" in the print path shifts every label (see DESIGN.md §9).
"""

from dataclasses import dataclass

import segno
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .mac import normalize
from .sheets import PT_PER_IN, Rect, SheetTemplate

PRINTER_DPI = 600
DOT = 72.0 / PRINTER_DPI          # one printer dot in points
MODULE_DOTS = 10                  # QR module = 10 dots = 0.423 mm (DESIGN.md §5.1)
MODULE = MODULE_DOTS * DOT
FONT = "Courier-Bold"
MAX_FONT_SIZE = 9.0
MIN_ONE_LINE_SIZE = 6.0           # below this, wrap the MAC onto two lines instead
QUIET = 4 * MODULE                # QR quiet zone (4 modules, per the QR spec)
QR_MARGIN = QUIET                 # label edge to QR
TEXT_GAP = QUIET                  # QR to text; text inside the quiet zone hurts decoding
RIGHT_MARGIN = 0.8 * mm
CAP_HEIGHT = 0.57                 # Courier cap height, in em
LEADING = 1.25                    # line spacing, in em
ADVANCE = 0.6                     # Courier character advance, in em


@dataclass(frozen=True)
class Calibration:
    """Printer offset in mm; positive dx moves right, positive dy moves down."""
    dx_mm: float = 0.0
    dy_mm: float = 0.0


def _snap(v: float) -> float:
    """Snap to the printer dot grid so every QR module is exactly MODULE_DOTS wide."""
    return round(v / DOT) * DOT


def qr_matrix(payload: str) -> list[list[int]]:
    """QR Version 1, ECC M, alphanumeric mode. Raises if the payload does not fit."""
    qr = segno.make_qr(payload, error="m", mode="alphanumeric", version=1, boost_error=False)
    return [list(row) for row in qr.matrix]


class _Page:
    """Wraps a reportlab canvas with top-left-origin coordinates and calibration."""

    def __init__(self, c: canvas.Canvas, tpl: SheetTemplate, cal: Calibration):
        self.c = c
        self.page_h = tpl.page_h * PT_PER_IN
        self.dx = cal.dx_mm * mm
        self.dy = cal.dy_mm * mm

    def rect(self, x, y, w, h, fill=0, stroke=1, radius=0.0):
        x, y = x + self.dx, y + self.dy
        if radius:
            self.c.roundRect(x, self.page_h - y - h, w, h, radius, stroke=stroke, fill=fill)
        else:
            self.c.rect(x, self.page_h - y - h, w, h, stroke=stroke, fill=fill)

    def line(self, x1, y1, x2, y2):
        self.c.line(x1 + self.dx, self.page_h - y1 - self.dy, x2 + self.dx, self.page_h - y2 - self.dy)

    def text(self, x, baseline_y, s, font=FONT, size=8.0):
        self.c.setFont(font, size)
        self.c.drawString(x + self.dx, self.page_h - baseline_y - self.dy, s)

    def snapped_origin(self, x, y) -> tuple[float, float]:
        """Snap in *device* coordinates (after calibration), return template coordinates."""
        return _snap(x + self.dx) - self.dx, _snap(y + self.dy) - self.dy


def draw_label(page: _Page, r: Rect, mac: str) -> None:
    text = normalize(mac)
    matrix = qr_matrix(text)
    n = len(matrix)
    qr_size = n * MODULE

    qx, qy = page.snapped_origin(r.x + QR_MARGIN, r.y + (r.h - qr_size) / 2)
    page.c.setFillColorRGB(0, 0, 0)
    for i, row in enumerate(matrix):
        j = 0
        while j < n:                       # draw horizontal runs: no seams between modules
            if row[j]:
                k = j
                while k < n and row[k]:
                    k += 1
                page.rect(qx + j * MODULE, qy + i * MODULE, (k - j) * MODULE, MODULE, fill=1, stroke=0)
                j = k
            else:
                j += 1

    tx = qx + qr_size + TEXT_GAP
    lines, size = text_layout(text, r.x + r.w - RIGHT_MARGIN - tx)
    block = (CAP_HEIGHT + LEADING * (len(lines) - 1)) * size
    baseline = r.y + (r.h - block) / 2 + CAP_HEIGHT * size   # centre the cap-height block
    for i, line in enumerate(lines):
        page.text(tx, baseline + i * LEADING * size, line, size=size)


def text_layout(text: str, avail_w: float) -> tuple[list[str], float]:
    """One line if it fits at a readable size, else split 9C-69-D3 / 9C-12-65."""
    one = avail_w / (len(text) * ADVANCE)
    if one >= MIN_ONE_LINE_SIZE:
        return [text], min(MAX_FONT_SIZE, one)
    parts = text.split("-")
    lines = ["-".join(parts[:3]), "-".join(parts[3:])]
    return lines, min(MAX_FONT_SIZE, avail_w / (max(map(len, lines)) * ADVANCE))


def _outline(page: _Page, tpl: SheetTemplate, r: Rect) -> None:
    page.rect(r.x, r.y, r.w, r.h, radius=tpl.corner_radius * PT_PER_IN)


def render_sheet(tpl: SheetTemplate, placements: dict[tuple[int, int], str], out_path: str,
                 cal: Calibration = Calibration(), outlines: bool = False) -> None:
    """One page with each MAC drawn in its (row, col) cell."""
    c = canvas.Canvas(out_path, pagesize=(tpl.page_w * PT_PER_IN, tpl.page_h * PT_PER_IN))
    c.setTitle(f"macprinter {tpl.id}")
    page = _Page(c, tpl, cal)
    if outlines:
        c.setLineWidth(0.25)
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        for cell in tpl.cells():
            _outline(page, tpl, tpl.cell_rect(*cell))
    for (row, col), mac in placements.items():
        draw_label(page, tpl.cell_rect(row, col), mac)
    c.showPage()
    c.save()


def render_alignment(tpl: SheetTemplate, out_path: str, cal: Calibration = Calibration()) -> None:
    """Plain-paper test page: label outlines, centre marks, scale rulers, sample labels.

    Print it, lay it over a label sheet against a light, and measure the offset.
    Enter the offset as calibration dx/dy and reprint until outlines coincide.
    """
    W, H = tpl.page_w * PT_PER_IN, tpl.page_h * PT_PER_IN
    c = canvas.Canvas(out_path, pagesize=(W, H))
    c.setTitle(f"macprinter alignment {tpl.id}")
    page = _Page(c, tpl, cal)

    samples = {(0, 0): "9C-69-D3-9C-12-65",
               (tpl.rows - 1, tpl.cols - 1): "9C-69-D3-9C-12-65",
               (tpl.rows // 2, tpl.cols // 2): "00-11-22-33-44-55"}

    c.setLineWidth(0.3)
    for row, col in tpl.cells():
        r = tpl.cell_rect(row, col)
        _outline(page, tpl, r)
        if (row, col) in samples:
            continue
        cx, cy = r.x + r.w / 2, r.y + r.h / 2
        page.line(cx - 3, cy, cx + 3, cy)
        page.line(cx, cy - 3, cx, cy + 3)

    # 100 mm scale rulers with 10 mm ticks, to detect scaling in the print path
    x0, y0 = 1.0 * PT_PER_IN, tpl.origin_top * PT_PER_IN / 2
    page.line(x0, y0, x0 + 100 * mm, y0)
    for i in range(11):
        page.line(x0 + i * 10 * mm, y0 - 3, x0 + i * 10 * mm, y0 + 3)
    page.text(x0 + 100 * mm + 4, y0 + 2, "100 mm", font="Helvetica", size=6)
    xv = tpl.origin_left * PT_PER_IN / 2
    page.line(xv, 1.5 * PT_PER_IN, xv, 1.5 * PT_PER_IN + 100 * mm)
    for i in range(11):
        page.line(xv - 3, 1.5 * PT_PER_IN + i * 10 * mm, xv + 3, 1.5 * PT_PER_IN + i * 10 * mm)

    last = tpl.cell_rect(tpl.rows - 1, 0)
    footer_y = (last.y + last.h + H) / 2
    page.text(1.0 * PT_PER_IN, footer_y,
              f"{tpl.id} alignment  dx={cal.dx_mm:+.2f} mm  dy={cal.dy_mm:+.2f} mm  "
              f"(top edge feeds first)", font="Helvetica", size=6)

    for (row, col), mac in samples.items():
        draw_label(page, tpl.cell_rect(row, col), mac)
    c.showPage()
    c.save()
