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

Access (round 10): the page hands out company source code (`result.zip`) and runs Maven on uploaded poms, so
* with a token (`create_app(token=…)`, `$SBMIG_WEB_TOKEN`, `sbmig serve --token`) every request needs it:
  `Authorization: Bearer <token>`, or the HttpOnly cookie a `?token=<token>` request sets once (the page is then
  reloaded without the token in the address bar; SameSite=Strict, so other sites cannot use it);
* without a token only loopback clients (127.0.0.1 / ::1) are served — a LAN client gets 403 with the hint, even
  when plain `uvicorn … --host 0.0.0.0` was started without one (`sbmig serve` refuses to start like that).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sbmig.core.config import PACKAGE_ROOT, load_dotenv_files

from .runs import RUN_ID_RE, SERVED_FILES, RunManager

log = logging.getLogger("sbmig.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_MAX_UPLOAD_MB = 512
MAX_UPLOAD = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024      # default upload limit in bytes (see resolve_max_upload)
MAX_UPLOAD_ENV = "SBMIG_MAX_UPLOAD_MB"
THREAD_POOL_SIZE = 256             # anyio worker threads: each open SSE stream (browser tab) holds one for the run
_END = object()
TOKEN_ENV = "SBMIG_WEB_TOKEN"
TOKEN_COOKIE = "sbmig_token"
COOKIE_MAX_AGE_S = 7 * 24 * 3600   # the browser keeps access for a week (the token itself never leaves the server)
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback_host(host: str | None) -> bool:
    """127.0.0.0/8, ::1, `localhost` — an address only this machine can reach. `0.0.0.0` / `::` / a LAN address
    are not."""
    h = (host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _remote_client(scope: Scope) -> bool:
    """A client with a non-loopback IP address (a unix socket / the test client have no IP: local)."""
    client = scope.get("client")
    host = client[0] if client else None
    try:
        return not ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def _cookie_value(token: str) -> str:
    return hashlib.sha256(("sbmig-web:" + token).encode("utf-8")).hexdigest()


class TokenGuard:
    """ASGI middleware (pure ASGI: SSE streams pass through untouched). See the module docstring."""

    def __init__(self, app: ASGIApp, token: str | None = None):
        self.app = app
        self.token = token or None
        self.cookie = _cookie_value(token) if token else ""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        req = Request(scope)
        if self.token is None:
            if _remote_client(scope):
                await self._deny(scope, receive, send, req, 403,
                                 f"this sbmig web UI serves this machine only: start it with {TOKEN_ENV} set "
                                 f"(sbmig serve --token) to share it on the network")
                return
            await self.app(scope, receive, send)
            return
        auth = req.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if (bearer and secrets.compare_digest(bearer, self.token)) or \
                secrets.compare_digest(req.cookies.get(TOKEN_COOKIE, ""), self.cookie):
            await self.app(scope, receive, send)
            return
        given = req.query_params.get("token", "")
        if given and secrets.compare_digest(given, self.token):
            query = urlencode([(k, v) for k, v in req.query_params.multi_items() if k != "token"])
            if req.method == "GET" and req.url.path in ("/", "/index.html"):
                resp = RedirectResponse(req.url.path + (f"?{query}" if query else ""), status_code=303)
            else:          # an API call with ?token= works once and leaves the cookie for the next ones
                resp = None
            set_cookie = {"key": TOKEN_COOKIE, "value": self.cookie, "httponly": True, "samesite": "strict",
                          "path": "/", "max_age": COOKIE_MAX_AGE_S}
            if resp is not None:
                resp.set_cookie(**set_cookie)
                await resp(scope, receive, send)
                return

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    tmp = PlainTextResponse("")
                    tmp.set_cookie(**set_cookie)
                    cookie_header = [(k, v) for k, v in tmp.raw_headers if k == b"set-cookie"]
                    message = {**message, "headers": list(message.get("headers", [])) + cookie_header}
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return
        await self._deny(scope, receive, send, req, 401,
                         f"access token required: open this page as /?token=<token> once ({TOKEN_ENV} on the "
                         f"server), API clients send 'Authorization: Bearer <token>'")

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, req: Request, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if req.method == "GET" and req.url.path in ("/", "/index.html"):
            resp = HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>sbmig</title>"
                                f"<body style='font-family:sans-serif;padding:2em'><h3>sbmig web UI</h3>"
                                f"<p>{message}</p></body>", status_code=status)
        else:
            resp = JSONResponse({"error": message}, status_code=status)
        await resp(scope, receive, send)


def resolve_max_upload(max_upload_mb: int | None = None) -> int:
    """Upload limit in bytes: the argument (`sbmig serve --max-upload-mb`), else `$SBMIG_MAX_UPLOAD_MB`, else 512 MB.
    0 = no limit — the operator lifts the cap for one oversized project (a 980 MB zip, 2026-10-23) without touching
    the code; a negative or unreadable value falls back to the default and is logged."""
    source = "--max-upload-mb"
    if max_upload_mb is None:
        raw = os.environ.get(MAX_UPLOAD_ENV, "").strip()
        if raw:
            source = MAX_UPLOAD_ENV
            try:
                max_upload_mb = int(raw)
            except ValueError:
                log.warning("%s=%r is not a whole number of MB: using the default %d MB",
                            MAX_UPLOAD_ENV, raw, DEFAULT_MAX_UPLOAD_MB)
                max_upload_mb = None
    if max_upload_mb is None:
        return MAX_UPLOAD
    if max_upload_mb < 0:
        log.warning("%s=%d is negative: using the default %d MB (0 = no limit)", source, max_upload_mb,
                    DEFAULT_MAX_UPLOAD_MB)
        return MAX_UPLOAD
    return max_upload_mb * 1024 * 1024


def describe_max_upload(max_upload: int) -> str:
    return f"{max_upload // (1024 * 1024)} MB" if max_upload else "no limit"


def _ensure_console_logging() -> None:
    """Under plain `uvicorn sbmig.web.app:app` nobody configured the sbmig logger: show INFO on the console."""
    from sbmig.cli import harden_console_streams

    harden_console_streams()                 # a cp949 console must not turn every em dash into "--- Logging error ---"
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


def create_app(out_dir: Path, repo_root: Path = PACKAGE_ROOT, max_upload_mb: int | None = None,
               token: str | None = None) -> FastAPI:
    """`token` (else `$SBMIG_WEB_TOKEN`): required on every request; without one only loopback clients are served."""
    load_dotenv_files()        # server-start variables (SBMIG_MAX_UPLOAD_MB) may live in .env too; the shell wins
    mgr = RunManager(Path(out_dir), Path(repo_root))
    max_upload = resolve_max_upload(max_upload_mb)       # bytes; 0 = unlimited
    token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip() or None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _ensure_console_logging()
        limiter = anyio.to_thread.current_default_thread_limiter()   # every SSE stream holds one of these threads
        limiter.total_tokens = max(limiter.total_tokens, THREAD_POOL_SIZE)
        log.info("sbmig web UI ready (runs under %s, configs from %s/config, upload limit %s, %s)", mgr.out_dir,
                 mgr.repo_root, describe_max_upload(max_upload),
                 f"access token required ({TOKEN_ENV})" if token else "no token: loopback clients only")
        yield
        info = mgr.shutdown()
        log.info("sbmig web UI shut down: %s", info)

    app = FastAPI(title="sbmig", summary="JDK 21 + Spring Boot 4.1 migration agent — web UI", lifespan=lifespan)
    app.state.manager = mgr
    app.state.max_upload_bytes = max_upload
    app.state.token_required = bool(token)
    app.add_middleware(TokenGuard, token=token)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/configs")
    def configs() -> dict:
        return {"configs": mgr.configs(), "repo_root": str(mgr.repo_root), "out_dir": str(mgr.out_dir),
                "max_upload_mb": max_upload // (1024 * 1024)}       # 0 = no limit

    @app.get("/api/runs")
    def runs() -> dict:
        return {"runs": mgr.list_runs()}

    @app.get("/api/threads", response_class=PlainTextResponse)
    def threads() -> str:
        """Every thread's stack (round 9c, operators on Windows: no SIGUSR1) — also written to <out>/threads-<ts>.txt."""
        from sbmig.core.diag import dump_threads, thread_dump_text
        p = dump_threads(mgr.out_dir)
        return (f"# written to {p}\n" if p else "") + thread_dump_text()

    @app.post("/api/runs", status_code=202)
    async def start(request: Request, name: str = "project.zip", configs: str = "",
                    stop_after: str | None = None, llm_provider: str | None = None) -> JSONResponse:
        def too_large() -> JSONResponse:
            return JSONResponse({"error": f"zip too large (max {describe_max_upload(max_upload)})"}, status_code=413)

        length = request.headers.get("content-length")
        if max_upload and length and length.isdigit() and int(length) > max_upload:
            return too_large()
        # The body is streamed to <out>/_uploads/.incoming-*.part and checked on the way: a project zip close to a
        # gigabyte must not sit in memory (twice) before it is even validated. The limit and the zip signature are
        # enforced per chunk; the run manager moves the file into place, a rejected upload is deleted below.
        tmp = mgr.incoming_path()
        size, head = 0, b""
        try:
            async with await anyio.open_file(tmp, "wb") as f:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    if len(head) < 2:
                        head = (head + chunk)[:2]
                        if len(head) == 2 and head != b"PK":
                            return JSONResponse({"error": "body is not a zip file"}, status_code=400)
                    size += len(chunk)
                    if max_upload and size > max_upload:
                        return too_large()
                    await f.write(chunk)
            if size == 0:
                return JSONResponse({"error": "zip body required"}, status_code=400)
            if head != b"PK":
                return JSONResponse({"error": "body is not a zip file"}, status_code=400)
            cfgs = [c for c in configs.split(",") if c]
            try:
                rid = await run_in_threadpool(mgr.start_file, tmp, name, cfgs, stop_after or None,
                                              llm_provider or None)
            except (ValueError, OSError, zipfile.BadZipFile) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"run_id": rid, "status": mgr.status(rid)}, status_code=202)
        finally:
            tmp.unlink(missing_ok=True)        # gone already when start_file moved it under its run id

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
        attach = download is not None or name.endswith((".zip", ".csv", ".jsonl"))
        return FileResponse(path, media_type=SERVED_FILES[name], headers={"Cache-Control": "no-store"},
                            filename=name if attach else None,
                            content_disposition_type="attachment" if attach else "inline")

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


load_dotenv_files()            # plain `uvicorn sbmig.web.app:app`: SBMIG_WEB_OUT / SBMIG_WEB_REPO_ROOT from .env as well
app = create_app(Path(os.environ.get("SBMIG_WEB_OUT", "out/web")),
                 Path(os.environ.get("SBMIG_WEB_REPO_ROOT", str(PACKAGE_ROOT))))
