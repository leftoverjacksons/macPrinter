# macPrinter — Design Document

Status: **In development** — dashboard, checks, sheet tracking and PDF preview work;
printing not connected. Hardware: ASIX AX88179 dongles, **Avery 60519** metallic asset-tag
sheets (OL25SP still supported), Canon imageCLASS MF3010 (USB). This document records requirements,
decisions, risks, and open questions. Update it whenever a decision is made or
changed; record the change in the Decision Log at the bottom.

---

## 1. Problem statement

We ship USB-to-Ethernet adapter dongles with our device. The dongles carry no
printed MAC address. When plugged into a host, they enumerate as a network
interface and expose their MAC address.

Goal: a Raspberry Pi–based kiosk that

1. detects a dongle when it is plugged in,
2. reads its MAC address,
3. produces a label containing a QR code of the MAC plus the MAC in
   human-readable text,
4. prints labels onto partially-used label sheets without wasting labels,
   remembering which positions on the current sheet are already consumed.

## 2. Scope

### Phase 1 (initial)
- Printer: office **laser printer** (network or USB, via CUPS).
- Media: sheets of **metallic laser labels**. Default stock: **Avery PermaTrack 60519**
  (1" × ½", 84/sheet). OnlineLabels OL25SP (1.75" × ½", 80/sheet) remains selectable.
- Batch workflow: start session → plug in N dongles → review → print all at
  once onto the next free positions of the current sheet.
- Persistent sheet state across sessions and reboots.

### Phase 2 (future)
- **Thermal-transfer roll printer** (e.g. Zebra / TSC, ZPL/TSPL). Roll media has
  no "sheet state"; labels can print one-at-a-time immediately after detection.
- The architecture must keep label *content* separate from label *placement*
  and from the printer *backend* so Phase 2 is an additional backend, not a
  rewrite.

### Out of scope (for now)
- Programming / flashing MACs onto dongles.
- Network functional testing of the dongle (link test). *(Candidate for later —
  see Open Questions.)*

---

## 3. System overview

```
 ┌──────────────┐   udev event    ┌──────────────────┐
 │ USB dongle   │ ──────────────▶ │ Detector         │
 └──────────────┘                 │ (pyudev monitor) │
                                  └────────┬─────────┘
                                           │ DongleSeen(mac, vid:pid, iface, addr_type)
                                           ▼
 ┌──────────────┐  websocket   ┌───────────────────────┐     ┌─────────────┐
 │ Kiosk UI     │ ◀──────────▶ │ Core service          │ ──▶ │ SQLite DB   │
 │ (Chromium    │              │ - session state       │     │ sheets,     │
 │  kiosk mode) │              │ - validation / dedupe │     │ cells,      │
 └──────────────┘              │ - layout engine       │     │ dongles,    │
                               └──────────┬────────────┘     │ print jobs  │
                                          │ render            └─────────────┘
                                          ▼
                               ┌───────────────────────┐
                               │ Printer backend       │
                               │  • SheetPDF → CUPS    │  (Phase 1)
                               │  • ZPL → roll printer │  (Phase 2)
                               └───────────────────────┘
```

### Proposed stack
| Concern            | Choice (proposed)                          | Rationale |
|--------------------|--------------------------------------------|-----------|
| OS                 | Raspberry Pi OS Lite (Bookworm) + minimal X/Wayland for Chromium | Standard, well supported |
| Language           | Python 3.11+                               | pyudev, reportlab, good Pi support |
| USB/net detection  | Pluggable `Detector`: **sim** (any OS), **linux** (polls `/sys/class/net`, 0.5 s), **windows** (PowerShell `Get-NetAdapter`, 2 s) | Develop on any PC; same code on the Pi. Polling sysfs is simpler than udev and fast enough |
| Web backend        | FastAPI + server-sent events (SSE)         | Live push of state to the UI; auto-reconnect built into browsers |
| UI                 | Plain HTML/CSS/JS, no build step; Chromium `--kiosk` on the Pi | Nothing to compile on the Pi; also reachable from another PC |
| Persistence        | SQLite (WAL mode)                          | Single-file, transactional, survives power loss better than JSON |
| QR generation      | `segno`                                    | Pure Python, vector output, explicit version/ECC control |
| Label rendering    | `reportlab` → PDF with absolute mm coordinates | Precise placement; printer-independent |
| Printing           | CUPS (`lp`)                                | Handles network/USB lasers |
| Service mgmt       | systemd unit                               | Auto-start, restart on failure |

---

## 4. Dongle detection & MAC acquisition

### 4.1 Mechanism
- Listen for udev `add` events on subsystem `net` where the parent device is
  on the USB bus (`ID_BUS=usb`).
- Read MAC from `/sys/class/net/<iface>/address`.
- Also capture: USB VID:PID, USB serial (if any), driver (`r8152`, `ax88179_178a`,
  `cdc_ether`, …), interface name, timestamp.

### 4.2 Validity checks (important)
Cheap USB-Ethernet dongles have known failure modes that would produce **wrong
labels** if not detected:

| Risk | Detection | Action |
|------|-----------|--------|
| No EEPROM → driver assigns a **random MAC** each plug-in | `/sys/class/net/<iface>/addr_assign_type` ≠ 0 (0 = permanent/burned-in; 1 = random; 3 = set by host, e.g. systemd) | Reject with a visible error; do not print |
| **Duplicate MACs** across units (clone chips with a shared default, e.g. Realtek default ranges) | Lookup in `dongles` table of all previously labeled MACs | Warn prominently; require operator override |
| Same dongle re-plugged in the same session | Session-level dedupe | Ignore / show "already queued" |
| Host software rewrites MAC (NetworkManager cloned-MAC, etc.) | Configure NM to leave USB NICs **unmanaged**; prefer permanent address (`ethtool -P` / `addr_assign_type`) | Documented in provisioning |
| Dongle enumerates first as mass-storage "driver CD" (some RTL8153 units) | No `net` event appears | `usb_modeswitch` rule, or document as unsupported model |
| Multicast / locally-administered bit set | Check bits in first octet | Warn |

**Our dongle:** ASIX **AX88179** (USB 3.0 → Gigabit). Linux driver
`ax88179_178a` (in mainline kernel; no install needed). USB VID:PID normally
`0b95:1790`. The driver reads the MAC from the dongle's EEPROM; if the EEPROM
is blank/invalid it falls back to a random address, which `addr_assign_type`
will reveal.

**Assumption to verify with real hardware:** our specific dongle model reports
a stable, permanent, unique MAC. Test: plug the same dongle in 5× across two
reboots; MAC must be identical and `addr_assign_type` must be 0.

### 4.2a Reading a MAC by hand (for M0 testing)
| OS | Command |
|----|---------|
| Linux / Pi | `ip -br link` (USB NIC usually `eth1` or `enx<mac>`); or `cat /sys/class/net/*/address`; permanent vs random: `cat /sys/class/net/<iface>/addr_assign_type` (0 = burned-in) |
| Windows | `getmac /v` or `ipconfig /all` ("Physical Address", shown with dashes) |
| macOS | `networksetup -listallhardwareports` |

Note: on Linux, predictable interface naming may name the interface
`enx9c69d39c1265` — the MAC itself — which is a handy sanity check.

### 4.2b ⚠️ Finding (2026-09-30): MAC changes between plug-ins
Observed: the same AX88179 dongle reports a **different MAC on re-plug**
(seen on Windows via `ipconfig /all`). Since Windows is not in the final
system, re-confirm on the Pi with `tools/mac_probe.sh`.

Windows check (PowerShell) that separates cause 1 from cause 2:
`Get-NetAdapter | Format-List Name, InterfaceDescription, MacAddress, PermanentAddress`
— and in Device Manager → adapter → Advanced, "Network Address" should be
*Not Present*. If `PermanentAddress` is empty/changes, the dongle stores no MAC.
Without a stable MAC there is nothing meaningful to label, so this blocks the
project until resolved.

**How the MAC normally gets there.** At power-on the AX88179 loads its MAC
from an external EEPROM into its node-ID register. The Linux driver
(`ax88179_178a`, `ax88179_get_mac_addr()`) reads that register; if the value is
not a valid unicast address it logs `invalid MAC address, using random` and
the kernel generates a random, locally administered MAC
(`addr_assign_type` = 1). A new random value is generated on every plug-in.

**Candidate causes, in order of likelihood:**
1. **Blank/absent EEPROM** — cheap dongles often omit programming it (or omit
   the chip). Evidence: dmesg message above; `addr_assign_type` = 1; first
   octet has the locally administered bit set (2nd hex digit 2/6/A/E);
   `ethtool -e` returns all `ff`/`00`.
2. **Host software overriding the MAC** (NetworkManager
   `cloned-mac-address=random/stable`, Windows adapter "Network Address"
   property). Evidence: `addr_assign_type` = 3 and `ethtool -P` shows a
   different, stable permanent address. Fix is host configuration only.
3. **systemd on Linux** (relevant to both kiosk and product, see below).
4. A different driver binding (e.g. `cdc_ncm`) that obtains the MAC another
   way. Evidence: `driver` field in the probe output.

**systemd pitfall (Linux, incl. Raspberry Pi OS).** systemd-udevd's default
`MACAddressPolicy=persistent` replaces a *random* kernel MAC with one derived
from the host's `/etc/machine-id` and the interface name
(`addr_assign_type` becomes 3). Consequences:
- On the **kiosk**, every blank dongle enumerating under the same interface
  name would get the **same** MAC — a silent duplicate-label generator.
  Mitigation: kiosk ships a `.link` file with `MACAddressPolicy=none` for USB
  NICs, and the detector accepts **only** `addr_assign_type` = 0.
- On the **product**, the same blank dongle would show a MAC that depends on
  which product Pi it is plugged into — i.e. different from anything the
  kiosk could print.

**Diagnosis tool:** `sudo tools/mac_probe.sh` after each plug-in; compare two
plug-ins of the same unit, and a few different units.

**Remedies (choose after diagnosis):**

| Option | What | Pros | Cons |
|--------|------|------|------|
| A. Fix host | Cause 2 only: stop the host overriding the MAC | No hardware change | Only applies if EEPROM is actually programmed |
| B. Supplier | Buy dongles with an EEPROM programmed with unique MACs from a registered OUI; verify samples with the probe | Kiosk design unchanged; MAC works on any host | Different/pricier SKU; must verify every new lot |
| C. Program at the kiosk | Kiosk **assigns** a MAC, writes it to the dongle's EEPROM, re-enumerates, verifies `addr_assign_type`=0 and the value, then labels | Uses current dongles; MACs unique by construction (allocated from our DB) | Requires an EEPROM chip to be present; must know the EEPROM layout/checksum; a bad write can leave a unit unusable; needs a MAC source (below) |
| D. Fix MAC in our product's software | Our product is a Raspberry Pi, so it can assign the interface a fixed MAC from our allocated range (systemd `.link`: `MACAddress=`), stored in the product's config | No dongle modification; dongles interchangeable; MAC survives a dongle swap (good for customer DHCP reservations / allow-lists) | MAC belongs to the **product**, not the dongle — the label goes on the product (or its packaging) at product provisioning, and the dongle kiosk is no longer needed in its current form |

**Option C details (if chosen):**
- Linux supports it directly: `ethtool -E <iface> magic 0x17900b95 offset <n> value <b>`
  (magic = AX88179 EEPROM magic in the driver). ASIX also publishes
  vendor EEPROM programming tools; the EEPROM layout (MAC offset, checksum)
  must come from ASIX documentation or a dump of a correctly programmed unit.
  Try on 1–2 sacrificial dongles first.
- MAC source:
  - **Locally administered addresses** (first octet `x2`/`x6`/`xA`/`xE`):
    free, standards-legitimate, uniqueness guaranteed only within our
    allocations.
  - **IEEE-registered block** (MA-S = 4,096 addresses, MA-M ≈ 1M, MA-L ≈ 16M):
    globally unique; costs a registration fee.
- Kiosk flow becomes: detect → allocate next MAC from DB → write → re-plug
  or USB reset → verify → queue label. Allocation and label are recorded
  in one transaction so a MAC is never issued twice.

**Identifying a dongle independent of MAC:** USB serial number
(`/sys/bus/usb/devices/*/serial`) if present and unique per unit — cheap
dongles often have none or a shared constant value. Probe output will show.

**Update (2026-09-30):** the same dongle was checked on several Windows PCs.
The MAC was identical on all but one, so the dongle **does** carry a programmed
MAC; the outlier PC was overriding it on the host side (cause 2). Blocker
downgraded. Still to do later: check a batch of units for duplicate MACs, and
confirm `addr_assign_type` = 0 on the Pi with `tools/mac_probe.sh`.

### 4.3 Network handling (revised 2026-09-30)
The internet check (§4.5) needs each dongle to get a DHCP address, so dongles
are *managed*, but they must never carry the Pi's own traffic:
- NetworkManager profile `deploy/macprinter-dongle.nmconnection`, matched by
  driver `ax88179_178a`: DHCP, IPv6 off, `cloned-mac-address=preserve`,
  `route-metric=900` (the Pi's own uplink has a lower metric and stays preferred),
  `multi-connect=multiple` so several dongles can be up at once.
- systemd `.link` file (`deploy/10-macprinter-usb-nic.link`):
  `MACAddressPolicy=none`, `NamePolicy=mac` (interface named `enx<mac>`).
- The internet check pins its socket to the dongle with `SO_BINDTODEVICE`, so the
  request really leaves through the dongle regardless of route preference.
  The service needs `CAP_NET_RAW` for that (granted in the systemd unit).
- DNS resolution for the check uses the Pi's normal resolver, not the dongle.

### 4.5 Link and internet checks (decided 2026-09-30)
Physical setup: one Ethernet patch cable from a switch/router with DHCP and
internet; the operator plugs it into each dongle along with the USB side.

Per dongle, in order. A failure stops the later checks, which are shown as "not run".

| Check | Pass condition | Default timeout | On failure |
|-------|----------------|-----------------|------------|
| MAC | Unicast, non-zero; permanent (`addr_assign_type`=0 / Windows `PermanentAddress`); not already labeled; not already in this session | — | Random MAC: blocked. Already labeled: "Queue as reprint" override. Locally administered / permanence unknown: warning only |
| Link | Carrier up | 30 s | "Queue anyway" override. Speed below 1000 Mb/s is a warning |
| Internet | DHCP address, then HTTP `GET http://connectivitycheck.gstatic.com/generate_204` through the dongle returns 204 | 25 s DHCP + 5 s request | "Queue anyway" override |

Each check can be disabled and its timeouts/URL changed in Settings. Check
results are stored with each queued/printed label (`info` JSON).
An HTTP check is used rather than ICMP ping: it needs no raw sockets, passes
through more firewalls, and proves DNS + TCP + HTTP work.

### 4.4 Removal
Unplug events are informational only (UI shows "removed"). A queued MAC stays
queued after unplug — the operator may unplug each dongle as they go.

---

## 5. Label content

**Decided format:** `9C-69-D3-9C-12-65` — uppercase hex, dash-separated,
17 characters. Identical string in the QR code and in the printed text. No
other text on the label.

- **QR payload:** exactly the string above, no prefix, no newline. Uppercase
  hex and `-` are in the QR *alphanumeric* character set, so it fits a
  **Version 1 (21×21 modules) QR at ECC level M** (capacity 20 alphanumeric
  characters). Encoder must be forced to alphanumeric mode and uppercase —
  lowercase would force byte mode and a larger symbol.
- **Human-readable text:** same string, bold monospace, right of the QR code.

### 5.1 Label layout (same rules for every template)
Printer is 600 dpi, so module size is chosen as an integer number of dots
to avoid uneven modules:

| Element | Value |
|---------|-------|
| QR module | 10 dots = 0.423 mm |
| QR symbol | 21 modules = 8.89 mm square, vertically centred |
| QR quiet zone | 4 modules (1.69 mm) left of the QR and between QR and text; ≈1.9 mm top/bottom on ½"-tall labels |
| Text | Courier-Bold, vertically centred, 0.8 mm right margin, ≤ 9 pt |
| Line breaking | One line if it fits at ≥ 6 pt; otherwise two lines split at the middle dash: `9C-69-D3` / `9C-12-65` |

Result per stock:

| Stock | Label | Text width available | Text |
|-------|-------|----------------------|------|
| **Avery 60519** | 1" × ½" (25.4 × 12.7 mm) | ≈ 12.3 mm | **two lines, ≈ 7.3 pt** (one line would be ≈ 3.6 pt — unreadable) |
| OL25SP | 1.75" × ½" (44.45 × 12.7 mm) | ≈ 31.4 mm | one line, ≈ 8.7 pt |

Same string, same QR on both; only the wrapping differs. Tests check that the
QR decodes from a 600 dpi render on both stocks and that text stays ≥ 7 pt.

**Safe zone:** keep all ink ≥ 1.5 mm from the top and bottom label edges. This
matters most on OL25, whose rows are butt-cut (no vertical gap); Avery 60519
has a 0.225" gap between rows.
- **Metallized media caveat:** glossy/metallic surfaces can cause specular
  glare that defeats some scanners. Validate scanning with the actual scanners
  used downstream (phone camera, handheld imager) early.

Label content is described by a backend-independent model:
```
LabelContent { mac: str, qr_payload: str, lines: [str] }
```
Rendered by a `LabelRenderer` into a box of given width × height (mm). The
same content model feeds both the sheet PDF renderer and a future ZPL renderer.

---

## 6. Sheet model & state tracking

### 6.1 Sheet templates (configuration, YAML)
Templates live in `macprinter/templates/*.yaml`; the vendor PDFs they were
measured from are kept in `templates/`. Units: inches, origin at the top-left of
the page as it enters the printer. Adding a stock = adding one YAML file; it
then appears in the dashboard's "Load new sheet" picker.

**Avery PermaTrack 60519** (default) — metallic silver asset tags, laser,
from Avery's template PDF (`templates/AVERY60519.pdf`, drawn in 0.1 pt units):
```yaml
id: AVERY60519
page:   { width: 8.5,  height: 11.0 }        # US Letter
label:  { width: 1.0, height: 0.5, corner_radius: 0.09375 }
grid:   { cols: 6, rows: 14 }                # 84 labels / sheet
origin: { left: 0.4375, top: 0.55 }
pitch:  { x: 1.325, y: 0.725 }               # col gap 0.325", row gap 0.225"
fill_order: row-major
```
Derived from the PDF: column x-origins 31.5 / 126.9 / 222.3 / 317.7 / 413.1 /
508.5 pt; first row top at 39.6 pt from the top edge; last row's bottom edge
37.8 pt above the page bottom.

**OnlineLabels OL25SP** — weatherproof silver polyester, laser,
from the vendor template PDF (`OL25.pdf`, 612 × 792 pt page):
```yaml
id: OL25SP
vendor: OnlineLabels
material: weatherproof silver polyester (laser)
page:   { width: 8.5,  height: 11.0 }        # US Letter
label:  { width: 1.75, height: 0.5, corner_radius: 0.047 }
grid:   { cols: 4, rows: 20 }                # 80 labels / sheet
origin: { left: 0.33, top: 0.5 }             # top-left of label (row 0, col 0)
pitch:  { x: 2.03125, y: 0.5 }               # col gap 0.28125", row gap 0
fill_order: row-major                        # see §7 on fill order
```
Derived from the PDF: column x-origins 23.76 / 170.01 / 316.26 / 462.51 pt;
rows start at 36 pt and step 36 pt (0.5") through 720 pt. **Rows have no
vertical gap** — adjacent labels share an edge.
Plus a **per-printer calibration offset** (`dx`, `dy`, optional scale) set via a
printed alignment test page, because lasers commonly shift output by 0.5–2 mm.

### 6.2 Persistent state (SQLite)
```
sheet_templates(id, yaml)
sheets(id, template_id, created_at, retired_at, label)      -- a physical sheet
cells(sheet_id, row, col, state, dongle_mac, print_job_id)  -- state: free|used|void
dongles(mac PK, vid_pid, driver, first_seen, label_count)   -- global registry
sessions(id, started_at, ended_at, sheet_id)
print_jobs(id, session_id, created_at, status, pdf_path)    -- status: sent|confirmed|failed
```
- Exactly one sheet is "active" at a time.
- `void` = cell ruined or skipped (misprint, damaged label) — never reused.

### 6.3 Commit semantics (avoid silent loss or double-use)
1. Operator ends session → layout engine assigns queued MACs to the next free
   cells (fill order from template).
2. UI shows a **preview** of the sheet: used cells greyed, new labels
   highlighted.
3. On "Print": cells move to `used` and a `print_job` is created *in the same
   DB transaction* as sending to CUPS.
4. Operator confirms "printed OK" or reports failure. On failure: choose
   either *revert cells to free* (sheet not consumed, e.g. printer jammed
   before feeding) or *mark void* (labels printed badly). Reprint the MACs
   onto new cells.

Rationale: marking cells used *before* confirmation errs toward wasting a label
rather than double-printing over an already-printed label.

### 6.4 Overflow
If queued labels exceed free cells on the active sheet:
- Fill the remainder of the current sheet, then prompt to load a new sheet and
  print the rest as a second job. (Two separate CUPS jobs, so the operator
  can swap media between them.)

### 6.5 Sheet lifecycle actions (UI)
- Load new sheet (select template) → becomes active; previous is retired.
- Manually mark cells used/void (e.g. sheet arrived with damage, or state got
  out of sync with the physical sheet).
- Print alignment/test page on plain paper.

---

## 7. ⚠️ Key physical risk: re-feeding partially used label sheets through a laser

Label manufacturers commonly warn **against** re-feeding partially used sheets
through laser printers:
- Exposed adhesive where labels were removed can contaminate the fuser or
  cause jams (costly printer damage).
- Remaining labels can lift/peel along the curved paper path at fuser
  temperatures.

**Printer-specific note:** the Canon imageCLASS MF3010 appears to feed only
from its 150-sheet front cassette (curved "C" path; no straight-through
bypass found in the specs). It lists "Label" as a supported media type.
This makes re-feeding a partially used sheet **riskier** than on a printer
with a straight path. Select media type = Label (lower speed / fuser setting).

Mitigations to evaluate before committing to the sheet-reuse workflow:
1. Use a printer with a **straight-through / manual bypass** feed path
   (not available on the MF3010).
2. Check the chosen label stock's datasheet for re-feed guidance and max
   fuser temperature (metallized polyester must be rated for laser use).
3. Remove labels only from the **trailing** end of the sheet (fill order such
   that the leading edge stays intact longest), or remove labels only after the
   whole sheet is fully printed (i.e. peel the printed ones immediately but
   leave backing matrix / weed intact where possible).
4. Consider printing sheets fully and simply *storing* the unused printed
   labels — tradeoff is that pre-printing is impossible because MACs are only
   known at plug-in.
5. Hold this concern as a strong argument for moving to the Phase 2 roll
   printer sooner.

**Action:** test with a sacrificial printer or confirm with stock vendor.

---

## 8. Kiosk UI

Target: 7" touchscreen (or small monitor + mouse), large touch targets,
glanceable from a standing position.

**Implemented (2026-09-30)** as a web dashboard (`macprinter serve`); screenshots
in `docs/img/dashboard_*.png`. Tabs: Session, Sheet, History, Settings. The
Confirm step is replaced for now by **Mark as printed** (no printer connected).
UI rule learned in testing: never rebuild unchanged DOM on state updates, or
taps landing mid-rebuild are lost (`setHTML` diffing in `app.js`).

### Screens / states
1. **Idle** — active sheet summary (e.g. "31 / 52 free"), buttons:
   *Start session*, *Sheet…*, *Settings*.
2. **Session active** — live list of detected dongles with status badges
   (✔ queued, ⚠ duplicate, ✖ random MAC), count vs. remaining capacity,
   buttons: *Remove item*, *Cancel session*, *Finish & preview*.
3. **Preview** — sheet grid rendering (used / new / free / void), *Print*.
4. **Confirm** — "Did the labels print correctly?" → *Yes* / *Reprint* /
   *Printer jammed (nothing printed)*.
5. **Sheet management** — load new sheet, choose template, edit cell states,
   print alignment page.
6. **History / export** — searchable list of labeled MACs; CSV export.

Feedback: audible beep + visual flash on each detection; distinct sound on
error (duplicate / random MAC). Operators will be looking at dongles, not
the screen.

---

## 9. Printer backend abstraction

```
class PrinterBackend:
    def capabilities(self) -> {"media": "sheet" | "roll", ...}
    def print_labels(self, placements: list[Placement]) -> JobHandle
    def job_status(self, handle) -> Status

Placement { content: LabelContent, cell: (row, col) | None }
```
- `SheetPdfCupsBackend` (Phase 1): renders a single-page PDF per sheet with
  labels at absolute positions; sends to CUPS with `-o fit-to-page=false`
  / scaling 100%,
  selecting the "Label" media type.

**MF3010 driver on the Pi:** the MF3010 is not IPP-Everywhere / driverless; it
needs Canon's proprietary **UFR II / UFRII LT** CUPS driver
(`cnrdrvcups-lb`, v5.x+). That package ships PPDs `CNRCUPSMF3010ZK.ppd` /
`CNRCUPSMF3010ZS.ppd`, and Canon publishes ARM builds of v5.x+ — but
**MF3010 operation on a Raspberry Pi (arm64) is unverified**. This is an M0
blocker to test first. Fallbacks if it fails: (a) a different printer with
PostScript/PCL or driverless IPP support (lowest risk), (b) print via a
small x86 host, (c) jump directly to the Phase 2 roll printer.
- `ZplRollBackend` (Phase 2): sends each label as ZPL immediately; no sheet
  state; session workflow may collapse to "print on detect".

**PDF scaling is the #1 alignment failure mode** — any "fit to page" in the
print path shifts every label. Verify with the alignment page.

---

## 10. Data & traceability

- Every labeled MAC is stored permanently with timestamps, job id, and sheet
  position. Provides an inventory of shipped dongle MACs.
- CSV export from the UI.
- **Google Sheets sync (decided):** append one row per printed label to a
  Google Sheet we own.
  - Auth: a Google Cloud **service account**; the sheet is shared with the
    service-account e-mail (Editor). Key JSON stored on the Pi with `0600`
    permissions, outside the repo, never committed.
  - Library: `gspread` (Sheets API v4, `values.append`).
  - **Outbox pattern:** rows are written to a local `sync_outbox` table in the
    same transaction as the print job; a background worker pushes them and
    marks them synced. Network outages never block printing; rows are retried
    until acknowledged. SQLite remains the source of truth.
  - Idempotency: each row carries a unique `event_id`; the worker checks for
    it before appending after a retry, so a crash mid-append does not
    duplicate rows.
  - Proposed columns: `timestamp_utc, mac, event (printed|reprinted|voided),
    sheet_id, row, col, print_job_id, kiosk_id, usb_vid_pid, event_id`.
  - The sheet is write-only from the kiosk's view (it never reads back for
    decisions); duplicate detection uses the local DB.
- Daily SQLite backup to USB stick or network share — TBD.

---

## 11. Deployment & provisioning

- Pi model: Pi 4 or Pi 5 (USB 3; ample power). Powered USB hub if dongles are
  plugged via hub (dongles can draw 150–350 mA each).
- Read-only root / overlayfs considered later for SD-card longevity; DB then
  lives on a writable partition.
- Provisioning script: install packages, CUPS printer setup, NM unmanaged rule
  for USB NICs, systemd service, Chromium kiosk autostart.
- Time: DB timestamps require correct clock — Pi has no RTC by default (Pi 5
  has RTC with battery); rely on NTP or add RTC.

---

## 12. Repository layout

```
macPrinter/
├── DESIGN.md, README.md, pyproject.toml
├── macprinter/
│   ├── cli.py             # `macprinter serve | align | sheet`
│   ├── mac.py             # MAC parsing/formatting (9C-69-D3-9C-12-65)
│   ├── sheets.py          # sheet templates, cell geometry, fill order
│   ├── render_pdf.py      # label + sheet PDF renderer, alignment page
│   ├── db.py              # SQLite schema and queries
│   ├── service.py         # detection pipeline, checks, sessions, allocation, commit
│   ├── detect/            # base.py (interface + HTTP check), sim.py, linux.py, windows.py
│   ├── templates/         # sheet template YAMLs (AVERY60519.yaml, OL25SP.yaml)
│   └── web/               # app.py (FastAPI + SSE), static/ (dashboard HTML/CSS/JS)
├── templates/             # vendor template PDFs the YAML geometry was measured from
├── deploy/                # systemd unit, NetworkManager profile, .link file, Pi guide
├── tools/mac_probe.sh     # manual dongle diagnostics
├── docs/img/              # screenshots
└── tests/                 # geometry, rendering/QR decode, API flows, detector parsing
```

A printer backend (`backends/`) and Google Sheets sync are not written yet.

---

## 13. Milestones

1. **M0 – Hardware validation:** (a) AX88179 MAC stable across 5 re-plugs and
   2 reboots, `addr_assign_type`=0, no duplicates across ~10 units;
   (b) install `cnrdrvcups-lb` on the Pi and print a test page on the MF3010;
   (c) print one full Avery 60519 sheet — check toner adhesion/fusing on metallic
   polyester and QR scan reliability; (d) peel a few labels from the trailing
   end and re-feed that sheet — check for jams/lifting.
2. **M1 – Detection CLI:** prints MAC + validation to terminal on plug-in.
3. **M2 – Sheet renderer:** template YAML → PDF; alignment test page; calibrate.
   *Status: renderer + alignment page implemented and tested (QR decodes at
   600 dpi); calibration on the MF3010 pending.*
4. **M3 – State + allocation:** SQLite, sessions, cell allocation, commit
   semantics, unit tests. *(Status: done — preview token guards against committing a
   stale preview; overflow continues on the next sheet; API tests.)*
5. **M4 – Kiosk UI:** full workflow on touchscreen. *(Status: dashboard done with simulator;
   Linux/Windows detectors untested on hardware; Pi deploy files written, untested.)*
6. **M5 – Hardening:** provisioning script, backups, power-loss testing.
7. **M6 – Roll printer backend** (Phase 2).

---

## 14. Open questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| 1 | Exact dongle model(s)? | | **closed:** ASIX AX88179 |
| 2 | Label stock? | | **closed:** Avery PermaTrack 60519 (1" × ½", 84/sheet, Letter); OL25SP kept as an option |
| 3 | Printer? | | **closed:** Canon imageCLASS MF3010, USB. Pi driver support = M0 test |
| 4 | MAC format? | | **closed:** `9C-69-D3-9C-12-65` |
| 5 | Additional label text? | | **closed:** none |
| 6 | Who/what scans the QR downstream, and with what device? | | open |
| 7 | Reprint a MAC that was already labeled? | | **closed:** yes, low priority (warn + confirm) |
| 8 | Link test as part of the flow? | | **closed:** yes, link + internet (HTTP to Google) checks, §4.5 |
| 9 | Export/integration target? | | **closed:** Google Sheet (service account, outbox) |
| 10 | Display: touchscreen size, or monitor + keyboard? | | open |
| 11 | One operator station, or multiple kiosks sharing state? | | open |
| 13 | AX88179 MAC is not stable across plug-ins | | **mostly closed:** stable on all PCs but one (host override). Duplicate check across units deferred |
| 12 | Does silver polyester survive the MF3010 fuser, and does a partially used sheet re-feed without jamming? | | open — M0 test |

---

## 15. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-09-30 | Initial draft created; Phase 1 = laser + sheets, Phase 2 = thermal roll | Per project kickoff |
| 2026-09-30 | Printer backends abstracted behind a common interface | Allow roll printer without rewrite |
| 2026-09-30 | Cells marked `used` at print time, not at confirmation | Prefer wasting a label over double-printing a cell |
| 2026-09-30 | Hardware: AX88179 dongles, OL25SP sheets (4×20), MF3010 via USB | User input |
| 2026-09-30 | Label = `9C-69-D3-9C-12-65` text + QR of same string, nothing else | User input |
| 2026-09-30 | QR: V1-M alphanumeric, 10-dot (0.423 mm) modules at 600 dpi | Fits 0.5" label height with full quiet zone |
| 2026-09-30 | Google Sheet sync via service account + local outbox | Offline-tolerant; SQLite stays source of truth |
| 2026-09-30 | Observed unstable MAC on AX88179 re-plug; added diagnosis + remedy options | Test result from user |
| 2026-09-30 | Unstable MAC was observed on Windows; final product host is a Raspberry Pi (Linux) | User input; makes remedy D viable, adds systemd pitfall |
| 2026-09-30 | MAC confirmed stable on all but one PC; proceed, duplicate check deferred | User test |
| 2026-09-30 | Renderer: QR modules drawn on the 600 dpi dot grid after calibration offset; text auto-sized to fit (≤ 9 pt Courier-Bold) | Keeps every module exactly 10 dots; prevents text overflow |
| 2026-09-30 | Link + internet checks per dongle; internet = HTTP 204 from Google through the dongle | User request; proves the dongle works end-to-end |
| 2026-09-30 | Dongles get DHCP via an NM profile with route-metric 900 (replaces "unmanaged") | Internet check needs an address; high metric keeps Pi traffic off dongles |
| 2026-09-30 | Detectors poll (sysfs / PowerShell) behind one interface; simulator for development | Develop on any OS, same code on the Pi |
| 2026-09-30 | Dashboard = FastAPI + SSE + plain JS; no printing yet ("Mark as printed" commits) | User request: stop at showing the document |
| 2026-10-05 | Default stock changed to Avery PermaTrack 60519 (1" × ½", 6×14); OL25SP kept selectable | User request |
| 2026-10-05 | MAC text wraps to two lines (`9C-69-D3` / `9C-12-65`) when one line would be < 6 pt; QR quiet zone set to exactly 4 modules on both sides | 1"-wide label cannot fit 17 characters legibly on one line |
