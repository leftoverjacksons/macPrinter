# macPrinter

Raspberry Pi kiosk that reads the MAC address of a USB-Ethernet dongle and
prints a QR + text label onto partially used label sheets. See
[DESIGN.md](DESIGN.md) for requirements, decisions, and open questions.

**Status:** label/sheet rendering and the alignment page work (milestone M2).
Detection service, sheet state, kiosk UI, and printing are not built yet.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Render labels

```sh
# Plain-paper alignment page for OL25SP (outlines, centre marks, 100 mm rulers, sample labels)
macprinter align -o alignment.pdf

# Put MACs into the next free cells; --used marks cells already peeled (row,col, 0-based)
macprinter sheet --mac 9C-69-D3-9C-12-65 --mac 9c:69:d3:9c:12:66 \
    --used 0,0 --used 0,1 -o sheet.pdf

# Add --outlines to preview on plain paper; add --dx/--dy (mm) once calibrated
```

![Sample labels](docs/img/sheet_top.png)

## Print (MF3010, milestone M0)

The PDF **must print at 100 %** — any fit-to-page scaling moves every label.

```sh
lpstat -p                                  # find the queue name
lpoptions -p <queue> -l                    # find the driver's media-type option
lp -d <queue> -o media=Letter -o print-scaling=none -o fit-to-page=false alignment.pdf
```

Calibration: print `alignment.pdf` on plain paper, lay it on a label sheet
against a light, and measure how far the outlines sit from the real labels.
Check the 100 mm rulers measure 100 mm (if not, scaling is on somewhere).
Re-render with `--dx` (+ = right) and `--dy` (+ = down) until they coincide.

## Check a dongle

```sh
sudo tools/mac_probe.sh     # MAC, whether it's permanent, USB IDs, EEPROM dump
```
