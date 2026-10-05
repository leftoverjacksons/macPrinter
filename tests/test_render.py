import pytest

from macprinter.render_pdf import Calibration, qr_matrix, render_alignment, render_sheet, text_layout
from macprinter.sheets import PT_PER_IN, SheetTemplate


def test_qr_is_version1():
    assert len(qr_matrix("9C-69-D3-9C-12-65")) == 21


def test_qr_rejects_lowercase_alphanumeric():
    with pytest.raises(Exception):
        qr_matrix("9c-69-d3-9c-12-65")


def _decode_cells(pdf_path, tpl, cells, dpi=600):
    fitz = pytest.importorskip("pymupdf")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    page = fitz.open(pdf_path)[0]
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    s = dpi / 72.0
    det = cv2.QRCodeDetector()
    out = {}
    for (row, col) in cells:
        r = tpl.cell_rect(row, col)
        crop = img[int(r.y * s):int((r.y + r.h) * s), int(r.x * s):int((r.x + r.h) * s)]
        crop = cv2.copyMakeBorder(crop, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
        data, _, _ = det.detectAndDecode(crop)
        out[(row, col)] = data
    return out


TEMPLATES = ["AVERY60519", "OL25SP"]


@pytest.mark.parametrize("template", TEMPLATES)
def test_rendered_qr_decodes(tmp_path, template):
    tpl = SheetTemplate.load(template)
    last = (tpl.rows - 1, tpl.cols - 1)
    placements = {(0, 0): "9C-69-D3-9C-12-65", (7, 2): "00-0E-C6-AB-CD-EF", last: "02-00-00-00-00-01"}
    pdf = tmp_path / "s.pdf"
    render_sheet(tpl, placements, str(pdf), Calibration(0.4, -0.3), outlines=True)
    assert _decode_cells(pdf, tpl, placements) == placements


@pytest.mark.parametrize("template", TEMPLATES)
def test_content_stays_inside_label(tmp_path, template):
    """Nothing within 1.5 mm of the top/bottom edge (OL25 rows are butt-cut); text inside the label."""
    fitz = pytest.importorskip("pymupdf")
    tpl = SheetTemplate.load(template)
    pdf = tmp_path / "s.pdf"
    render_sheet(tpl, {(3, 1): "9C-69-D3-9C-12-65"}, str(pdf))
    page = fitz.open(str(pdf))[0]
    r = tpl.cell_rect(3, 1)
    margin = 1.5 / 25.4 * PT_PER_IN
    for d in page.get_drawings():
        b = d["rect"]
        assert b.y0 >= r.y + margin - 0.01 and b.y1 <= r.y + r.h - margin + 0.01
        assert b.x0 >= r.x and b.x1 <= r.x + r.w
    lines = [ln for blk in page.get_text("blocks") for ln in blk[4].split("\n") if ln]
    assert "-".join(lines) == "9C-69-D3-9C-12-65"           # one line, or wrapped at the middle dash
    for b in page.get_text("blocks"):
        x0, y0, x1, y1 = b[:4]
        assert x0 >= r.x and x1 <= r.x + r.w
        assert y0 >= r.y and y1 <= r.y + r.h


def test_alignment_page_renders(tmp_path):
    tpl = SheetTemplate.load("OL25SP")
    pdf = tmp_path / "a.pdf"
    render_alignment(tpl, str(pdf))
    assert pdf.stat().st_size > 1000


def test_text_wraps_on_narrow_labels():
    avery = SheetTemplate.load("AVERY60519")
    lines, size = text_layout("9C-69-D3-9C-12-65", 34.9)          # ~available width on a 1" label
    assert lines == ["9C-69-D3", "9C-12-65"] and size >= 7.0
    lines, size = text_layout("9C-69-D3-9C-12-65", 89.0)          # OL25: one line
    assert lines == ["9C-69-D3-9C-12-65"] and size >= 8.5
    assert avery.label_w == 1.0


@pytest.mark.parametrize("template,n_lines", [("AVERY60519", 2), ("OL25SP", 1)])
def test_rendered_text_lines_and_size(tmp_path, template, n_lines):
    fitz = pytest.importorskip("pymupdf")
    tpl = SheetTemplate.load(template)
    pdf = tmp_path / "s.pdf"
    render_sheet(tpl, {(0, 0): "9C-69-D3-9C-12-65"}, str(pdf))
    spans = [sp for b in fitz.open(str(pdf))[0].get_text("dict")["blocks"]
             for ln in b.get("lines", []) for sp in ln["spans"]]
    assert len(spans) == n_lines
    assert min(sp["size"] for sp in spans) >= 7.0
