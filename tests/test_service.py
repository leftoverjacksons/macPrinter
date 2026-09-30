"""End-to-end tests of the dashboard API using the simulated detector."""

import time

import pytest
from fastapi.testclient import TestClient

from macprinter.detect import sim as sim_mod
from macprinter.web.app import create_app


@pytest.fixture(autouse=True)
def fast_sim(monkeypatch):
    monkeypatch.setattr(sim_mod.SimDetector, "LINK_DELAY", 0.0)
    monkeypatch.setattr(sim_mod.SimDetector, "DHCP_DELAY", 0.0)
    orig = sim_mod.SimDetector.connectivity

    def quick(self, info, url, timeout):
        monkeypatch.setattr(sim_mod.time, "sleep", lambda s: None)
        return orig(self, info, url, timeout)
    monkeypatch.setattr(sim_mod.SimDetector, "connectivity", quick)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, "sim")) as c:
        c.put("/api/settings", json={"link_timeout_s": 2, "dhcp_timeout_s": 2, "internet_timeout_s": 1})
        yield c


def wait_status(c, key, statuses=("queued", "pass", "fail", "duplicate"), timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for d in c.get("/api/state").json()["detections"]:
            if d["key"] == key and d["status"] in statuses:
                return d
        time.sleep(0.05)
    raise AssertionError(f"{key} never reached {statuses}")


def plug(c, **kw):
    return c.post("/api/sim/plug", json=kw).json()["key"]


def commit(c):
    tok = c.get("/api/preview").json()["token"]
    return c.post("/api/commit", json={"token": tok})


def test_happy_path(client):
    client.post("/api/session/start")
    d = wait_status(client, plug(client, mac="9c:69:d3:9c:12:65"))
    assert d["status"] == "queued"
    assert {k: v["status"] for k, v in d["checks"].items()} == {"mac": "pass", "link": "pass", "internet": "pass"}
    pv = client.get("/api/preview").json()
    assert pv["placements"] == [{"row": 0, "col": 0, "mac": "9C-69-D3-9C-12-65", "reprint": False}]
    pdf = client.get("/api/preview.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    r = commit(client).json()
    assert r == {"job_id": 1, "placed": 1, "remaining": 0}
    s = client.get("/api/state").json()
    assert s["session"] is None                               # queue empty -> session done
    assert s["sheet"]["free"] == 79
    assert client.get("/api/jobs/1.pdf").status_code == 200
    assert [h["mac"] for h in client.get("/api/history").json()] == ["9C-69-D3-9C-12-65"]


def test_dongle_passed_before_session_is_queued_on_start(client):
    key = plug(client)
    assert wait_status(client, key)["status"] == "pass"
    client.post("/api/session/start")
    assert len(client.get("/api/state").json()["queue"]) == 1


def test_random_mac_rejected_and_not_overridable(client):
    client.post("/api/session/start")
    key = plug(client, permanent=False)
    d = wait_status(client, key)
    assert d["status"] == "fail" and not d["can_override"]
    assert d["checks"]["link"]["status"] == "skip"
    assert client.post(f"/api/detections/{key}/queue").status_code == 409


def test_no_link_then_retry(client):
    client.post("/api/session/start")
    key = plug(client, cable=False)
    d = wait_status(client, key, timeout=6)
    assert d["status"] == "fail" and d["checks"]["link"]["status"] == "fail"
    client.post(f"/api/sim/{key}/set", json={"cable": True})
    client.post(f"/api/detections/{key}/retry")
    assert wait_status(client, key, statuses=("queued",))["status"] == "queued"


def test_internet_failure_can_be_overridden(client):
    client.post("/api/session/start")
    key = plug(client, internet=False)
    d = wait_status(client, key)
    assert d["status"] == "fail" and d["can_override"]
    assert client.post(f"/api/detections/{key}/queue").status_code == 200
    q = client.get("/api/state").json()["queue"]
    assert len(q) == 1 and q[0]["override"]


def test_checks_can_be_disabled(client):
    client.put("/api/settings", json={"check_link": False, "check_internet": False})
    client.post("/api/session/start")
    d = wait_status(client, plug(client, cable=False))
    assert d["status"] == "queued"
    assert d["checks"]["link"]["status"] == "skip"


def test_previously_labeled_mac_flagged(client):
    client.post("/api/session/start")
    k1 = plug(client, mac="9C-69-D3-00-00-01")
    wait_status(client, k1)
    commit(client)
    client.post(f"/api/sim/{k1}/unplug")
    client.post("/api/session/start")
    d = wait_status(client, plug(client, mac="9C-69-D3-00-00-01"))
    assert d["status"] == "fail" and d["reprint"] and d["can_override"]
    assert "already labeled" in d["checks"]["mac"]["detail"]


def test_same_dongle_twice_in_session_is_duplicate(client):
    client.post("/api/session/start")
    wait_status(client, plug(client, mac="9C-69-D3-00-00-02"))
    d = wait_status(client, plug(client, mac="9C-69-D3-00-00-02"))
    assert d["status"] == "duplicate"
    assert len(client.get("/api/state").json()["queue"]) == 1


def test_stale_preview_rejected(client):
    client.post("/api/session/start")
    wait_status(client, plug(client))
    tok = client.get("/api/preview").json()["token"]
    wait_status(client, plug(client))                       # queue changes after preview
    r = client.post("/api/commit", json={"token": tok})
    assert r.status_code == 409


def test_skips_used_and_void_cells(client):
    client.post("/api/sheet/cell", json={"row": 0, "col": 0, "state": "used"})
    client.post("/api/sheet/cell", json={"row": 0, "col": 1, "state": "void"})
    client.post("/api/session/start")
    wait_status(client, plug(client))
    assert [(p["row"], p["col"]) for p in client.get("/api/preview").json()["placements"]] == [(0, 2)]


def test_overflow_continues_on_new_sheet(client):
    for r in range(20):
        for col in range(4):
            if (r, col) != (19, 3):
                client.post("/api/sheet/cell", json={"row": r, "col": col, "state": "used"})
    client.post("/api/session/start")
    wait_status(client, plug(client, mac="9C-69-D3-00-00-0A"))
    wait_status(client, plug(client, mac="9C-69-D3-00-00-0B"))
    pv = client.get("/api/preview").json()
    assert pv["overflow"] == 1 and len(pv["placements"]) == 1
    assert commit(client).json()["remaining"] == 1
    s = client.get("/api/state").json()
    assert s["session"] is not None and [q["mac"] for q in s["queue"]] == ["9C-69-D3-00-00-0B"]
    client.post("/api/sheet/new", json={})
    pv = client.get("/api/preview").json()
    assert pv["overflow"] == 0 and pv["placements"][0]["row"] == 0
    assert commit(client).json()["remaining"] == 0


def test_state_survives_restart(tmp_path):
    with TestClient(create_app(tmp_path, "sim")) as c:
        c.post("/api/sheet/cell", json={"row": 2, "col": 3, "state": "void"})
        c.post("/api/session/start")
        wait_status(c, plug(c, mac="9C-69-D3-00-00-0C"))
    with TestClient(create_app(tmp_path, "sim")) as c:
        s = c.get("/api/state").json()
        assert s["session"] is not None
        assert [q["mac"] for q in s["queue"]] == ["9C-69-D3-00-00-0C"]
        assert s["sheet"]["cells"] == [{"row": 2, "col": 3, "state": "void", "mac": None}]


def test_reprint_from_history(client):
    client.post("/api/session/start")
    wait_status(client, plug(client, mac="9C-69-D3-00-00-0D"))
    commit(client)
    client.post("/api/session/start")
    assert client.post("/api/history/9C-69-D3-00-00-0D/reprint").status_code == 200
    q = client.get("/api/state").json()["queue"]
    assert q[0]["reprint"] is True
