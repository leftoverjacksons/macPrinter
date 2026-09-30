"""SQLite persistence: settings, sheets, cells, sessions, queue, jobs, printed labels."""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sheets (
    id          INTEGER PRIMARY KEY,
    template_id TEXT NOT NULL,
    created_at  REAL NOT NULL,
    retired_at  REAL
);
-- Only non-free cells are stored; a missing row means the cell is free.
CREATE TABLE IF NOT EXISTS cells (
    sheet_id   INTEGER NOT NULL REFERENCES sheets(id),
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('used', 'void')),
    mac        TEXT,
    job_id     INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY (sheet_id, row, col)
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at   REAL,
    status     TEXT NOT NULL CHECK (status IN ('active', 'done', 'cancelled'))
);
CREATE TABLE IF NOT EXISTS queue (
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    mac        TEXT NOT NULL,
    added_at   REAL NOT NULL,
    reprint    INTEGER NOT NULL DEFAULT 0,
    override   TEXT,               -- reason the operator queued despite a failed check
    info       TEXT,               -- JSON: vid_pid, driver, check results
    state      TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'printed')),
    job_id     INTEGER,
    PRIMARY KEY (session_id, mac)
);
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    sheet_id   INTEGER NOT NULL,
    created_at REAL NOT NULL,
    status     TEXT NOT NULL,      -- 'not_sent' until a printer backend exists
    pdf_path   TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    id         INTEGER PRIMARY KEY,
    mac        TEXT NOT NULL,
    job_id     INTEGER NOT NULL,
    sheet_id   INTEGER NOT NULL,
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    reprint    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    info       TEXT
);
CREATE INDEX IF NOT EXISTS labels_mac ON labels(mac);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)   # autocommit; explicit BEGIN for transactions
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def _one(self, sql, *args):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # --- settings -------------------------------------------------------
    def get_settings(self, defaults: dict) -> dict:
        out = dict(defaults)
        for r in self._all("SELECT key, value FROM settings"):
            if r["key"] in defaults:
                out[r["key"]] = json.loads(r["value"])
        return out

    def put_settings(self, values: dict) -> None:
        self.db.execute("BEGIN")
        for k, v in values.items():
            self.db.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
        self.db.execute("COMMIT")

    # --- sheets ---------------------------------------------------------
    def active_sheet(self, default_template: str) -> dict:
        s = self._one("SELECT * FROM sheets WHERE retired_at IS NULL ORDER BY id DESC LIMIT 1")
        return s or self.new_sheet(default_template)

    def new_sheet(self, template_id: str) -> dict:
        now = time.time()
        self.db.execute("BEGIN")
        self.db.execute("UPDATE sheets SET retired_at = ? WHERE retired_at IS NULL", (now,))
        cur = self.db.execute("INSERT INTO sheets(template_id, created_at) VALUES (?, ?)", (template_id, now))
        self.db.execute("COMMIT")
        return self._one("SELECT * FROM sheets WHERE id = ?", cur.lastrowid)

    def cells(self, sheet_id: int) -> dict[tuple[int, int], dict]:
        return {(r["row"], r["col"]): r for r in self._all("SELECT * FROM cells WHERE sheet_id = ?", sheet_id)}

    def set_cell(self, sheet_id: int, row: int, col: int, state: str) -> None:
        if state == "free":
            self.db.execute("DELETE FROM cells WHERE sheet_id = ? AND row = ? AND col = ?", (sheet_id, row, col))
        else:
            self.db.execute(
                "INSERT INTO cells(sheet_id, row, col, state, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(sheet_id, row, col) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (sheet_id, row, col, state, time.time()))

    # --- sessions & queue -----------------------------------------------
    def active_session(self) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1")

    def start_session(self) -> dict:
        cur = self.db.execute("INSERT INTO sessions(started_at, status) VALUES (?, 'active')", (time.time(),))
        return self._one("SELECT * FROM sessions WHERE id = ?", cur.lastrowid)

    def end_session(self, session_id: int, status: str) -> None:
        self.db.execute("UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?", (status, time.time(), session_id))

    def queue(self, session_id: int, state: str = "queued") -> list[dict]:
        rows = self._all("SELECT * FROM queue WHERE session_id = ? AND state = ? ORDER BY added_at", session_id, state)
        for r in rows:
            r["info"] = json.loads(r["info"] or "{}")
        return rows

    def in_queue(self, session_id: int, mac: str) -> dict | None:
        return self._one("SELECT * FROM queue WHERE session_id = ? AND mac = ?", session_id, mac)

    def enqueue(self, session_id: int, mac: str, info: dict, reprint: bool = False, override: str | None = None):
        self.db.execute(
            "INSERT OR IGNORE INTO queue(session_id, mac, added_at, reprint, override, info) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, mac, time.time(), int(reprint), override, json.dumps(info)))

    def dequeue(self, session_id: int, mac: str) -> None:
        self.db.execute("DELETE FROM queue WHERE session_id = ? AND mac = ? AND state = 'queued'", (session_id, mac))

    # --- commit ("printed") ---------------------------------------------
    def create_job(self, session_id: int, sheet_id: int) -> int:
        cur = self.db.execute("INSERT INTO jobs(session_id, sheet_id, created_at, status) VALUES (?, ?, ?, 'pending')",
                              (session_id, sheet_id, time.time()))
        return cur.lastrowid

    def commit_job(self, job_id: int, session_id: int, sheet_id: int,
                   placements: list[tuple[tuple[int, int], dict]], pdf_path: str) -> None:
        """Atomically mark cells used, record labels, and mark queue items printed."""
        now = time.time()
        self.db.execute("BEGIN")
        try:
            for (row, col), item in placements:
                self.db.execute(
                    "INSERT INTO cells(sheet_id, row, col, state, mac, job_id, updated_at) VALUES (?, ?, ?, 'used', ?, ?, ?)",
                    (sheet_id, row, col, item["mac"], job_id, now))
                self.db.execute(
                    "INSERT INTO labels(mac, job_id, sheet_id, row, col, reprint, created_at, info) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["mac"], job_id, sheet_id, row, col, item["reprint"], now, json.dumps(item["info"])))
                self.db.execute("UPDATE queue SET state = 'printed', job_id = ? WHERE session_id = ? AND mac = ?",
                                (job_id, session_id, item["mac"]))
            self.db.execute("UPDATE jobs SET status = 'not_sent', pdf_path = ? WHERE id = ?", (pdf_path, job_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def job(self, job_id: int) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id = ?", job_id)

    # --- history --------------------------------------------------------
    def labels_for(self, mac: str) -> list[dict]:
        return self._all("SELECT * FROM labels WHERE mac = ? ORDER BY created_at", mac)

    def history(self, limit: int = 500) -> list[dict]:
        rows = self._all("SELECT * FROM labels ORDER BY created_at DESC LIMIT ?", limit)
        for r in rows:
            r["info"] = json.loads(r["info"] or "{}")
        return rows
