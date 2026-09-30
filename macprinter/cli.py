"""Command-line entry points for rendering (no kiosk/state yet)."""

import argparse
import sys

from .mac import InvalidMac, normalize
from .render_pdf import Calibration, render_alignment, render_sheet
from .sheets import SheetTemplate


def _cell(s: str) -> tuple[int, int]:
    r, c = s.split(",")
    return int(r), int(c)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="macprinter")
    p.add_argument("--template", default="OL25SP", help="template id or YAML path")
    p.add_argument("--dx", type=float, default=0.0, help="calibration offset, mm (+ = right)")
    p.add_argument("--dy", type=float, default=0.0, help="calibration offset, mm (+ = down)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("align", help="render the plain-paper alignment page")
    a.add_argument("-o", "--out", default="alignment.pdf")

    s = sub.add_parser("sheet", help="render MACs into the next free cells")
    s.add_argument("--mac", action="append", required=True, help="repeatable")
    s.add_argument("--used", action="append", default=[], type=_cell,
                   help="already-used cell 'row,col' (0-based), repeatable")
    s.add_argument("--outlines", action="store_true", help="draw label outlines (plain-paper preview)")
    s.add_argument("-o", "--out", default="sheet.pdf")

    args = p.parse_args(argv)
    tpl = SheetTemplate.load(args.template)
    cal = Calibration(args.dx, args.dy)

    if args.cmd == "align":
        render_alignment(tpl, args.out, cal)
        print(f"wrote {args.out}")
        return 0

    try:
        macs = [normalize(m) for m in args.mac]
    except InvalidMac as e:
        print(e, file=sys.stderr)
        return 2
    cells = tpl.next_free(set(args.used), len(macs))
    if len(cells) < len(macs):
        print(f"only {len(cells)} free cells for {len(macs)} labels", file=sys.stderr)
        return 3
    placements = dict(zip(cells, macs))
    render_sheet(tpl, placements, args.out, cal, outlines=args.outlines)
    for (r, c), m in placements.items():
        print(f"row {r:2d} col {c}  {m}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
