"""FastAPI app: JSON API, server-sent state updates, and the static dashboard."""

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..db import Store
from ..detect import make_detector
from ..render_pdf import Calibration, render_alignment
from ..service import ActionError, Service
from ..sheets import TEMPLATE_DIR

STATIC = Path(__file__).parent / "static"


def create_app(data_dir: Path, detector_kind: str = "sim") -> FastAPI:
    data_dir = Path(data_dir)
    service_box: dict[str, Service] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = Service(Store(data_dir / "macprinter.db"), make_detector(detector_kind), data_dir)
        service_box["svc"] = svc
        task = asyncio.create_task(svc.run())
        yield
        task.cancel()

    app = FastAPI(title="macPrinter", lifespan=lifespan)

    def svc() -> Service:
        return service_box["svc"]

    @app.exception_handler(ActionError)
    async def _action_error(request: Request, exc: ActionError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def body(request: Request) -> dict:
        try:
            return await request.json()
        except json.JSONDecodeError:
            return {}

    # --- state ---------------------------------------------------------
    @app.get("/api/state")
    async def state():
        return svc().state()

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            last = -1
            while not await request.is_disconnected():
                s = svc()
                if s.version != last:
                    last = s.version
                    yield f"data: {json.dumps(s.state())}\n\n"
                else:
                    await s.wait_change(last, 15)
                    if s.version == last:
                        yield ": keepalive\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    # --- session -------------------------------------------------------
    @app.post("/api/session/start")
    async def start_session():
        svc().start_session()
        return {"ok": True}

    @app.post("/api/session/cancel")
    async def cancel_session():
        svc().cancel_session()
        return {"ok": True}

    @app.delete("/api/queue/{mac}")
    async def remove(mac: str):
        svc().remove_from_queue(mac)
        return {"ok": True}

    @app.post("/api/detections/{key}/{action}")
    async def detection_action(key: str, action: str):
        s = svc()
        fn = {"retry": s.retry, "queue": s.force_queue, "dismiss": s.dismiss}.get(action)
        if not fn:
            raise HTTPException(404)
        fn(key)
        return {"ok": True}

    # --- preview & commit -----------------------------------------------
    @app.get("/api/preview")
    async def preview():
        a = svc().allocation()
        return {"token": a["token"], "overflow": a["overflow"], "sheet_id": a["sheet_id"],
                "placements": [{"row": r, "col": c, "mac": i["mac"], "reprint": bool(i["reprint"])}
                               for (r, c), i in a["placements"]]}

    @app.get("/api/preview.pdf")
    async def preview_pdf(outlines: int = 0):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = Path(f.name)
        try:
            svc().render_preview(path, bool(outlines))
            data = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="preview.pdf"',
                                 "Cache-Control": "no-store"})

    @app.post("/api/commit")
    async def commit(request: Request):
        return svc().commit((await body(request)).get("token", ""))

    @app.get("/api/jobs/{job_id}.pdf")
    async def job_pdf(job_id: int):
        j = svc().store.job(job_id)
        if not j or not j["pdf_path"] or not Path(j["pdf_path"]).exists():
            raise HTTPException(404)
        return FileResponse(j["pdf_path"], media_type="application/pdf")

    # --- sheet ---------------------------------------------------------
    @app.get("/api/templates")
    async def templates():
        return sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml"))

    @app.post("/api/sheet/new")
    async def new_sheet(request: Request):
        b = await body(request)
        svc().new_sheet(b.get("template") or svc().settings["template"])
        return {"ok": True}

    @app.post("/api/sheet/cell")
    async def set_cell(request: Request):
        b = await body(request)
        svc().set_cell(int(b["row"]), int(b["col"]), b["state"])
        return {"ok": True}

    @app.get("/api/alignment.pdf")
    async def alignment():
        s = svc()
        st = s.settings
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = Path(f.name)
        try:
            render_alignment(s.template(), str(path), Calibration(st["cal_dx_mm"], st["cal_dy_mm"]))
            data = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="alignment.pdf"'})

    # --- history & settings --------------------------------------------
    @app.get("/api/history")
    async def history():
        return svc().store.history()

    @app.post("/api/history/{mac}/reprint")
    async def reprint(mac: str):
        svc().reprint(mac)
        return {"ok": True}

    @app.put("/api/settings")
    async def settings(request: Request):
        svc().update_settings(await body(request))
        return svc().settings

    # --- simulator -----------------------------------------------------
    def sim():
        d = svc().detector
        if d.name != "sim":
            raise HTTPException(404, "simulator not active")
        return d

    @app.post("/api/sim/plug")
    async def sim_plug(request: Request):
        b = await body(request)
        try:
            key = sim().plug(mac=b.get("mac") or None, permanent=b.get("permanent", True),
                             cable=b.get("cable", True), internet=b.get("internet", True))
        except ValueError as e:
            raise ActionError(str(e))
        return {"key": key}

    @app.post("/api/sim/{key}/unplug")
    async def sim_unplug(key: str):
        sim().unplug(key)
        return {"ok": True}

    @app.post("/api/sim/{key}/set")
    async def sim_set(key: str, request: Request):
        b = await body(request)
        try:
            sim().update(key, cable=b.get("cable"), internet=b.get("internet"))
        except KeyError:
            raise HTTPException(404)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
