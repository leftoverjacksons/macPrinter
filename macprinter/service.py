"""Core kiosk logic: watches detectors, runs checks, manages sessions and sheet state.

Everything runs on one asyncio loop; blocking detector calls go through
asyncio.to_thread. All database access happens on the loop thread.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .db import Store
from .detect import Detector, DongleInfo
from .mac import is_labelable, is_locally_administered
from .render_pdf import Calibration, render_sheet
from .sheets import SheetTemplate

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "template": "AVERY60519",
    "cal_dx_mm": 0.0,
    "cal_dy_mm": 0.0,
    "check_link": True,
    "link_timeout_s": 30,
    "expected_speed_mbps": 1000,
    "check_internet": True,
    "dhcp_timeout_s": 25,
    "internet_url": "http://connectivitycheck.gstatic.com/generate_204",
    "internet_timeout_s": 5,
}

PENDING, RUNNING, PASS, WARN, FAIL, SKIP = "pending", "running", "pass", "warn", "fail", "skip"


class Unplugged(Exception):
    pass


class ActionError(Exception):
    """User-facing error from an action (HTTP 400/409)."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Detection:
    key: str
    info: DongleInfo
    present: bool = True
    status: str = "checking"          # checking | pass | fail | queued | duplicate
    message: str = ""
    can_override: bool = False
    reprint: bool = False
    checks: dict = field(default_factory=dict)
    seen_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def to_json(self) -> dict:
        return {"key": self.key, "info": asdict(self.info), "present": self.present,
                "status": self.status, "message": self.message, "can_override": self.can_override,
                "reprint": self.reprint, "checks": self.checks, "seen_at": self.seen_at}


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Service:
    MAX_GONE = 30   # unplugged detections kept on screen

    def __init__(self, store: Store, detector: Detector, data_dir: Path):
        self.store = store
        self.detector = detector
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.detections: dict[str, Detection] = {}
        self.detector_error = ""
        self.version = 0
        self._event = asyncio.Event()

    # --- change notification -------------------------------------------
    def changed(self) -> None:
        self.version += 1
        self._event.set()
        self._event = asyncio.Event()

    async def wait_change(self, since: int, timeout: float) -> None:
        ev = self._event
        if self.version != since:
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass

    @property
    def settings(self) -> dict:
        return self.store.get_settings(DEFAULT_SETTINGS)

    def template(self) -> SheetTemplate:
        return SheetTemplate.load(self.sheet()["template_id"])

    def sheet(self) -> dict:
        return self.store.active_sheet(self.settings["template"])

    # --- detector polling ----------------------------------------------
    async def run(self) -> None:
        while True:
            try:
                snap = await asyncio.to_thread(self.detector.snapshot)
                if self.detector_error:
                    self.detector_error = ""
                    self.changed()
                self.apply_snapshot(snap)
            except Exception as e:                 # keep the kiosk alive; show the error
                log.exception("detector failed")
                if str(e) != self.detector_error:
                    self.detector_error = f"{type(e).__name__}: {e}"
                    self.changed()
            await asyncio.sleep(self.detector.poll_interval)

    def apply_snapshot(self, snap: dict[str, DongleInfo]) -> None:
        dirty = False
        for key, info in snap.items():
            d = self.detections.get(key)
            if d is None or not d.present or d.info.mac != info.mac:
                if d and d.task:
                    d.task.cancel()
                d = Detection(key=key, info=info)
                self.detections[key] = d
                d.task = asyncio.create_task(self._pipeline(d))
                dirty = True
            elif d.info != info:
                d.info = info
                dirty = True
        for key, d in self.detections.items():
            if d.present and key not in snap:
                d.present = False
                dirty = True
        gone = [k for k, d in sorted(self.detections.items(), key=lambda kv: kv[1].seen_at) if not d.present]
        for k in gone[:-self.MAX_GONE] if len(gone) > self.MAX_GONE else []:
            del self.detections[k]
        if dirty:
            self.changed()

    # --- check pipeline --------------------------------------------------
    def _set(self, d: Detection, name: str, status: str, detail: str = "") -> None:
        d.checks[name] = {"status": status, "detail": detail}
        self.changed()

    def _finish(self, d: Detection, status: str, message: str = "", can_override: bool = False) -> None:
        d.status, d.message, d.can_override = status, message, can_override
        if status in ("fail", "duplicate"):
            for c in d.checks.values():
                if c["status"] == PENDING:
                    c["status"], c["detail"] = SKIP, "not run"
        self.changed()

    async def _wait_for(self, d: Detection, pred, timeout: float) -> DongleInfo | None:
        deadline = time.monotonic() + timeout
        while True:
            if not d.present:
                raise Unplugged
            if pred(d.info):
                return d.info
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def _pipeline(self, d: Detection) -> None:
        s = self.settings
        d.status, d.message, d.can_override, d.reprint = "checking", "", False, False
        d.checks = {"mac": {"status": PENDING, "detail": ""},
                    "link": {"status": PENDING if s["check_link"] else SKIP, "detail": ""},
                    "internet": {"status": PENDING if s["check_internet"] else SKIP, "detail": ""}}
        self.changed()
        try:
            if not self._check_mac(d):
                return
            if s["check_link"]:
                self._set(d, "link", RUNNING, "waiting for cable / link…")
                info = await self._wait_for(d, lambda i: i.link_up, s["link_timeout_s"])
                if info is None:
                    self._set(d, "link", FAIL, f"no link within {s['link_timeout_s']} s")
                    return self._finish(d, "fail", "No Ethernet link. Check the cable.", can_override=True)
                sp = info.speed_mbps
                if sp and sp < s["expected_speed_mbps"]:
                    self._set(d, "link", WARN, f"{sp} Mb/s (expected {s['expected_speed_mbps']})")
                else:
                    self._set(d, "link", PASS, f"{sp} Mb/s" if sp else "link up")
            if s["check_internet"]:
                self._set(d, "internet", RUNNING, "waiting for DHCP address…")
                info = await self._wait_for(d, lambda i: bool(i.ipv4), s["dhcp_timeout_s"])
                if info is None:
                    self._set(d, "internet", FAIL, f"no DHCP address within {s['dhcp_timeout_s']} s")
                    return self._finish(d, "fail", "No IP address from DHCP.", can_override=True)
                self._set(d, "internet", RUNNING, f"{info.ipv4} → contacting Google…")
                ok, detail = await asyncio.to_thread(
                    self.detector.connectivity, info, s["internet_url"], s["internet_timeout_s"])
                if not d.present:
                    raise Unplugged
                self._set(d, "internet", PASS if ok else FAIL, f"{info.ipv4}: {detail}")
                if not ok:
                    return self._finish(d, "fail", "Internet check failed.", can_override=True)
            self._finish(d, "pass")
            self._auto_queue(d)
        except Unplugged:
            for c in d.checks.values():
                if c["status"] in (PENDING, RUNNING):
                    c["status"], c["detail"] = FAIL, "unplugged during test"
            self._finish(d, "fail", "Unplugged before the checks finished.")

    def _check_mac(self, d: Detection) -> bool:
        info = d.info
        mac = info.mac
        if not is_labelable(mac):
            self._set(d, "mac", FAIL, "multicast or all-zero address")
            self._finish(d, "fail", "Not a valid device MAC.")
            return False
        if info.permanent is False:
            self._set(d, "mac", FAIL, f"not burned in ({info.note or 'random / host-assigned'})")
            self._finish(d, "fail", "The dongle did not report a permanent MAC; it would change on the next plug-in.")
            return False
        sess = self.store.active_session()
        if sess and self.store.in_queue(sess["id"], mac):
            self._set(d, "mac", PASS, mac)
            self._finish(d, "duplicate", "Already in this session.")
            return False
        prior = self.store.labels_for(mac)
        if prior:
            p = prior[-1]
            self._set(d, "mac", FAIL, f"already labeled {_fmt_time(p['created_at'])} "
                                      f"(sheet {p['sheet_id']}, row {p['row'] + 1}, col {p['col'] + 1})")
            d.reprint = True
            self._finish(d, "fail", "This MAC was labeled before — possible duplicate MAC, or a reprint.",
                         can_override=True)
            return False
        notes = []
        if info.permanent is None:
            notes.append("permanence unknown")
        if is_locally_administered(mac):
            notes.append("locally administered address")
        if info.note:
            notes.append(info.note)
        self._set(d, "mac", WARN if notes else PASS, mac + (f" — {'; '.join(notes)}" if notes else ""))
        return True

    def _info_record(self, d: Detection) -> dict:
        return {"vid_pid": d.info.vid_pid, "driver": d.info.driver, "serial": d.info.serial,
                "checks": {k: v for k, v in d.checks.items()}}

    def _auto_queue(self, d: Detection) -> None:
        sess = self.store.active_session()
        if sess:
            self.store.enqueue(sess["id"], d.info.mac, self._info_record(d))
            self._finish(d, "queued")
        else:
            self._finish(d, "pass", "Passed. Start a session to queue it.")

    # --- operator actions -------------------------------------------------
    def _detection(self, key: str) -> Detection:
        if key not in self.detections:
            raise ActionError("unknown dongle", 404)
        return self.detections[key]

    def _require_session(self) -> dict:
        sess = self.store.active_session()
        if not sess:
            raise ActionError("Start a session first.", 409)
        return sess

    def retry(self, key: str) -> None:
        d = self._detection(key)
        if not d.present:
            raise ActionError("Dongle is unplugged.", 409)
        if d.task:
            d.task.cancel()
        d.task = asyncio.create_task(self._pipeline(d))

    def force_queue(self, key: str) -> None:
        d = self._detection(key)
        sess = self._require_session()
        if d.status == "pass":
            self.store.enqueue(sess["id"], d.info.mac, self._info_record(d))
        elif d.can_override:
            self.store.enqueue(sess["id"], d.info.mac, self._info_record(d),
                               reprint=d.reprint, override=d.message)
        else:
            raise ActionError("This dongle cannot be queued.", 409)
        self._finish(d, "queued", "Queued by operator override." if d.can_override else "")

    def dismiss(self, key: str) -> None:
        d = self._detection(key)
        if d.task:
            d.task.cancel()
        del self.detections[key]
        self.changed()

    def start_session(self) -> None:
        if self.store.active_session():
            raise ActionError("A session is already active.", 409)
        sess = self.store.start_session()
        for d in self.detections.values():          # dongles that passed before the session started
            if d.present and d.status == "pass":
                self.store.enqueue(sess["id"], d.info.mac, self._info_record(d))
                self._finish(d, "queued")
        self.changed()

    def cancel_session(self) -> None:
        sess = self._require_session()
        self.store.end_session(sess["id"], "cancelled")
        for d in self.detections.values():
            if d.status in ("queued", "duplicate"):
                self._finish(d, "pass", "Session cancelled.")
        self.changed()

    def remove_from_queue(self, mac: str) -> None:
        sess = self._require_session()
        self.store.dequeue(sess["id"], mac)
        for d in self.detections.values():
            if d.info.mac == mac and d.status in ("queued", "duplicate"):
                self._finish(d, "pass", "Removed from queue.")
        self.changed()

    def reprint(self, mac: str) -> None:
        sess = self._require_session()
        prior = self.store.labels_for(mac)
        if not prior:
            raise ActionError("No label history for that MAC.", 404)
        self.store.enqueue(sess["id"], mac, json.loads(prior[-1]["info"] or "{}"), reprint=True,
                           override="reprint requested from history")
        self.changed()

    def set_cell(self, row: int, col: int, state: str) -> None:
        if state not in ("free", "used", "void"):
            raise ActionError("state must be free, used or void")
        tpl = self.template()
        if not (0 <= row < tpl.rows and 0 <= col < tpl.cols):
            raise ActionError("cell outside the sheet")
        self.store.set_cell(self.sheet()["id"], row, col, state)
        self.changed()

    def new_sheet(self, template_id: str) -> None:
        SheetTemplate.load(template_id)            # validate
        self.store.put_settings({"template": template_id})
        self.store.new_sheet(template_id)
        self.changed()

    def update_settings(self, values: dict) -> None:
        clean = {}
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS or k == "template":
                continue
            default = DEFAULT_SETTINGS[k]
            try:
                clean[k] = type(default)(v) if not isinstance(default, bool) else bool(v)
            except (TypeError, ValueError):
                raise ActionError(f"bad value for {k}")
        self.store.put_settings(clean)
        self.changed()

    # --- preview & commit ---------------------------------------------------
    def allocation(self) -> dict:
        sess = self.store.active_session()
        sheet = self.sheet()
        tpl = SheetTemplate.load(sheet["template_id"])
        items = self.store.queue(sess["id"]) if sess else []
        cells = tpl.next_free(set(self.store.cells(sheet["id"])), len(items))
        placements = list(zip(cells, items))
        token_src = json.dumps([sheet["id"], [(c, i["mac"]) for c, i in placements]])
        return {
            "session_id": sess["id"] if sess else None,
            "sheet_id": sheet["id"],
            "placements": placements,
            "overflow": len(items) - len(placements),
            "token": hashlib.sha1(token_src.encode()).hexdigest()[:16],
        }

    def render_preview(self, path: Path, outlines: bool) -> dict:
        a = self.allocation()
        s = self.settings
        render_sheet(self.template(), {c: i["mac"] for c, i in a["placements"]}, str(path),
                     Calibration(s["cal_dx_mm"], s["cal_dy_mm"]), outlines=outlines)
        return a

    def commit(self, token: str) -> dict:
        """Record the previewed placements as printed. (No printer is driven yet.)"""
        sess = self._require_session()
        a = self.allocation()
        if not a["placements"]:
            raise ActionError("Nothing to place: the queue is empty or the sheet is full.", 409)
        if token != a["token"]:
            raise ActionError("The queue or sheet changed since the preview. Review the preview again.", 409)
        job_id = self.store.create_job(sess["id"], a["sheet_id"])
        pdf = self.jobs_dir / f"job_{job_id:05d}.pdf"
        s = self.settings
        render_sheet(self.template(), {c: i["mac"] for c, i in a["placements"]}, str(pdf),
                     Calibration(s["cal_dx_mm"], s["cal_dy_mm"]))
        self.store.commit_job(job_id, sess["id"], a["sheet_id"], a["placements"], str(pdf))
        done = {i["mac"] for _, i in a["placements"]}
        for d in self.detections.values():
            if d.info.mac in done and d.status == "queued":
                self._finish(d, "printed", "Labeled.")
        if not self.store.queue(sess["id"]):
            self.store.end_session(sess["id"], "done")
        self.changed()
        return {"job_id": job_id, "placed": len(done), "remaining": a["overflow"]}

    # --- state for the UI -------------------------------------------------
    def state(self) -> dict:
        sheet = self.sheet()
        tpl = SheetTemplate.load(sheet["template_id"])
        cells = self.store.cells(sheet["id"])
        sess = self.store.active_session()
        queue = self.store.queue(sess["id"]) if sess else []
        free = tpl.capacity - len(cells)
        dets = sorted(self.detections.values(), key=lambda d: (not d.present, -d.seen_at))
        out = {
            "version": self.version,
            "detector": self.detector.name,
            "detector_error": self.detector_error,
            "settings": self.settings,
            "session": sess,
            "queue": [{"mac": q["mac"], "reprint": bool(q["reprint"]), "override": q["override"],
                       "added_at": q["added_at"]} for q in queue],
            "detections": [d.to_json() for d in dets],
            "sheet": {
                "id": sheet["id"], "template": tpl.id, "rows": tpl.rows, "cols": tpl.cols,
                "capacity": tpl.capacity, "free": free, "created_at": sheet["created_at"],
                "cells": [{"row": r, "col": c, "state": v["state"], "mac": v["mac"]} for (r, c), v in cells.items()],
            },
        }
        if hasattr(self.detector, "sim_state"):
            out["sim"] = self.detector.sim_state()
        return out
