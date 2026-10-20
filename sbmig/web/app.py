"""FastAPI web app: upload a project zip, run the migration in-process, watch it live over SSE.

    uvicorn sbmig.web.app:app --port 8765          (env SBMIG_WEB_OUT, SBMIG_WEB_REPO_ROOT)
    sbmig serve --port 8765                         (same app, via uvicorn.run)

Routes:
    GET  /                               the page (static/index.html); /static/* = sbmig/web/static
    GET  /api/configs                    config/*.yml names the page can tick
    GET  /api/runs                       known runs (newest first) with status
    POST /api/runs?name=<file.zip>&configs=a.yml,b.yml[&stop_after=…][&llm_provider=…]
         body = the zip bytes                                       → 202 {"run_id", "status"}
    GET  /api/runs/{id}                  status
    GET  /api/runs/{id}/events           SSE: status | node | error | journal | log | report | ping | end
    POST /api/runs/{id}/stop             stop after the current graph node
    GET  /api/runs/{id}/files/{name}     report.md | report.json | result.zip | sbmig.log | journal.jsonl |
                                         changes.patch | plan.json | profile.json

Runs execute in worker threads of this process (see `sbmig.web.runs`); the SSE generator blocks on the run's events
in a threadpool thread, never on the event loop.
"""
from __future__ import annotations

import json
import logging
import os
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from sbmig.core.config import PACKAGE_ROOT

from .runs import RUN_ID_RE, SERVED_FILES, RunManager

log = logging.getLogger("sbmig.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 512 * 1024 * 1024
THREAD_POOL_SIZE = 256             # anyio worker threads: each open SSE stream (browser tab) holds one for the run
_END = object()


def _ensure_console_logging() -> None:
    """Under plain `uvicorn sbmig.web.app:app` nobody configured the sbmig logger: show INFO on the console."""
    root = logging.getLogger("sbmig")
    if not any(not isinstance(h, logging.FileHandler) for h in root.handlers):
        h = logging.StreamHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(h)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        root.propagate = False


def _check_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(400, "bad run id")
    return run_id


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode()


async def _stream(events: Iterator[tuple[str, Any]]) -> AsyncIterator[bytes]:
    """Async wrapper over the blocking `RunManager.events` generator: each `next()` runs in a worker thread."""
    try:
        while True:
            item = await anyio.to_thread.run_sync(next, events, _END)
            if item is _END:
                return
            yield _sse(*item)
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            try:
                close()
            except ValueError:      # "generator already executing": the thread still runs next(); it ends on its own
                pass


def create_app(out_dir: Path, repo_root: Path = PACKAGE_ROOT) -> FastAPI:
    mgr = RunManager(Path(out_dir), Path(repo_root))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _ensure_console_logging()
        limiter = anyio.to_thread.current_default_thread_limiter()   # every SSE stream holds one of these threads
        limiter.total_tokens = max(limiter.total_tokens, THREAD_POOL_SIZE)
        log.info("sbmig web UI ready (runs under %s, configs from %s/config)", mgr.out_dir, mgr.repo_root)
        yield

    app = FastAPI(title="sbmig", summary="JDK 21 + Spring Boot 4.1 migration agent — web UI", lifespan=lifespan)
    app.state.manager = mgr
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/configs")
    def configs() -> dict:
        return {"configs": mgr.configs(), "repo_root": str(mgr.repo_root), "out_dir": str(mgr.out_dir)}

    @app.get("/api/runs")
    def runs() -> dict:
        return {"runs": mgr.list_runs()}

    @app.post("/api/runs", status_code=202)
    async def start(request: Request, name: str = "project.zip", configs: str = "",
                    stop_after: str | None = None, llm_provider: str | None = None) -> JSONResponse:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_UPLOAD:
            return JSONResponse({"error": f"zip too large (max {MAX_UPLOAD // (1024 * 1024)} MB)"}, status_code=413)
        data = await request.body()
        if not data:
            return JSONResponse({"error": "zip body required"}, status_code=400)
        if len(data) > MAX_UPLOAD:
            return JSONResponse({"error": f"zip too large (max {MAX_UPLOAD // (1024 * 1024)} MB)"}, status_code=413)
        if data[:2] != b"PK":
            return JSONResponse({"error": "body is not a zip file"}, status_code=400)
        cfgs = [c for c in configs.split(",") if c]
        try:
            rid = await run_in_threadpool(mgr.start, data, name, cfgs, stop_after or None, llm_provider or None)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"run_id": rid, "status": mgr.status(rid)}, status_code=202)

    @app.get("/api/runs/{run_id}")
    def status(run_id: str) -> JSONResponse:
        st = mgr.status(_check_id(run_id))
        return JSONResponse(st, status_code=200 if st["exists"] else 404)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str):
        if not mgr.status(_check_id(run_id))["exists"]:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return StreamingResponse(_stream(mgr.events(run_id)), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/stop")
    def stop(run_id: str) -> dict:
        return {"stopped": mgr.stop(_check_id(run_id))}

    @app.get("/api/runs/{run_id}/files/{name}")
    def files(run_id: str, name: str, download: str | None = None):
        if name not in SERVED_FILES:
            return JSONResponse({"error": "unknown file"}, status_code=404)
        path = mgr.run_dir(_check_id(run_id)) / name
        if not path.is_file():
            return JSONResponse({"error": f"{name} not found"}, status_code=404)
        attach = download is not None or name.endswith(".zip")
        return FileResponse(path, media_type=SERVED_FILES[name], headers={"Cache-Control": "no-store"},
                            filename=name if attach else None,
                            content_disposition_type="attachment" if attach else "inline")

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app(Path(os.environ.get("SBMIG_WEB_OUT", "out/web")),
                 Path(os.environ.get("SBMIG_WEB_REPO_ROOT", str(PACKAGE_ROOT))))
