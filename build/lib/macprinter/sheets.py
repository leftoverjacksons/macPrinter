"""Sheet templates and cell geometry/allocation."""

from dataclasses import dataclass
from pathlib import Path

import yaml

PT_PER_IN = 72.0
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class Rect:
    """Rectangle in points, origin at page top-left, y increasing downward."""
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class SheetTemplate:
    id: str
    page_w: float          # inches
    page_h: float
    label_w: float
    label_h: float
    corner_radius: float
    cols: int
    rows: int
    origin_left: float
    origin_top: float
    pitch_x: float
    pitch_y: float
    fill_order: str = "row-major"

    @classmethod
    def load(cls, name_or_path: str) -> "SheetTemplate":
        path = Path(name_or_path)
        if not path.suffix:
            path = TEMPLATE_DIR / f"{name_or_path}.yaml"
        d = yaml.safe_load(path.read_text())
        return cls(
            id=d["id"],
            page_w=d["page"]["width"], page_h=d["page"]["height"],
            label_w=d["label"]["width"], label_h=d["label"]["height"],
            corner_radius=d["label"].get("corner_radius", 0.0),
            cols=d["grid"]["cols"], rows=d["grid"]["rows"],
            origin_left=d["origin"]["left"], origin_top=d["origin"]["top"],
            pitch_x=d["pitch"]["x"], pitch_y=d["pitch"]["y"],
            fill_order=d.get("fill_order", "row-major"),
        )

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    def cells(self) -> list[tuple[int, int]]:
        """All (row, col) cells in fill order."""
        if self.fill_order == "row-major":
            return [(r, c) for r in range(self.rows) for c in range(self.cols)]
        if self.fill_order == "column-major":
            return [(r, c) for c in range(self.cols) for r in range(self.rows)]
        raise ValueError(f"unknown fill_order {self.fill_order!r}")

    def cell_rect(self, row: int, col: int) -> Rect:
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError(f"cell ({row}, {col}) outside {self.rows}x{self.cols} grid")
        return Rect(
            x=(self.origin_left + col * self.pitch_x) * PT_PER_IN,
            y=(self.origin_top + row * self.pitch_y) * PT_PER_IN,
            w=self.label_w * PT_PER_IN,
            h=self.label_h * PT_PER_IN,
        )

    def next_free(self, used: set[tuple[int, int]], n: int) -> list[tuple[int, int]]:
        """First n cells in fill order not in `used`. Returns fewer if the sheet runs out."""
        return [cell for cell in self.cells() if cell not in used][:n]
