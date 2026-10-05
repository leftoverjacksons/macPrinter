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
    p.add_argument("--template", default="AVERY60519", help="template id or YAML path")
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

    v = sub.add_parser("serve", help="run the dashboard")
    v.add_argument("--detector", default="sim", choices=["sim", "linux", "windows", "auto"],
                   help="sim = fake dongles from the dashboard (default); linux = Pi; windows = dev PC (experimental)")
    v.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from other machines")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--data", default="data", help="directory for the database and job PDFs")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        import logging
        from pathlib import Path

        import uvicorn

        from .web.app import create_app
        logging.basicConfig(level=logging.INFO)
        print(f"macPrinter dashboard: http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}")
        uvicorn.run(create_app(Path(args.data), args.detector), host=args.host, port=args.port, log_level="warning")
        return 0

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
