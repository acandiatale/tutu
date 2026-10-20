"""`sbmig serve` — a one-page web UI to upload a project zip, run the migration and watch it live over SSE.

Standard library only (ThreadingHTTPServer). Each run is a normal `python -m sbmig.cli run <zip> …` subprocess
(same exit codes, same run directory, same `sbmig.log`), so the UI never changes how a migration behaves; the
server only streams what the run writes (`sbmig.log`, `journal.jsonl`, console output) and serves the results
(`report.md` / `report.json` / `result.zip`).

    GET  /                               the page (static/index.html)
    GET  /api/configs                    config/*.yml names the page can tick
    GET  /api/runs                       known runs (newest first) with status
    POST /api/runs?name=<file.zip>&configs=a.yml,b.yml[&stop_after=…][&llm_provider=…]
         body = the zip bytes
    GET  /api/runs/<id>                  status
    GET  /api/runs/<id>/events           SSE: log | journal | console | status | report | ping | end
    POST /api/runs/<id>/stop             terminate the subprocess
    GET  /api/runs/<id>/files/<name>     report.md | report.json | result.zip | sbmig.log | journal.jsonl |
                                         changes.patch | plan.json | profile.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlsplit

from sbmig.core.config import PACKAGE_ROOT

log = logging.getLogger("sbmig.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SERVED_FILES = {"report.md": "text/markdown; charset=utf-8", "report.json": "application/json",
                "result.zip": "application/zip", "sbmig.log": "text/plain; charset=utf-8",
                "journal.jsonl": "application/x-ndjson", "changes.patch": "text/plain; charset=utf-8",
                "plan.json": "application/json", "profile.json": "application/json"}
MAX_UPLOAD = 512 * 1024 * 1024
POLL_S = 0.5
KEEPALIVE_S = 15.0

CommandBuilder = Callable[["RunSpec"], list[str]]


@dataclass
class RunSpec:
    run_id: str
    zip_path: Path
    out_dir: Path
    configs: list[str] = field(default_factory=list)
    stop_after: Optional[str] = None
    llm_provider: Optional[str] = None


def default_command(spec: RunSpec) -> list[str]:
    cmd = [sys.executable, "-m", "sbmig.cli", "run", str(spec.zip_path), "--out", str(spec.out_dir),
           "--run-id", spec.run_id]
    for c in spec.configs:
        cmd += ["--config", c]
    if spec.stop_after:
        cmd += ["--stop-after", spec.stop_after]
    if spec.llm_provider:
        cmd += ["--llm-provider", spec.llm_provider]
    return cmd


class RunManager:
    """Starts runs as subprocesses and answers status questions from the process table + the run directory."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, command: CommandBuilder = default_command):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.command = command
        self.procs: dict[str, subprocess.Popen] = {}
        self.specs: dict[str, RunSpec] = {}
        self._lock = threading.Lock()
        (self.out_dir / "_uploads").mkdir(parents=True, exist_ok=True)
        (self.out_dir / "_console").mkdir(parents=True, exist_ok=True)

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        return self.out_dir / run_id

    def console_log(self, run_id: str) -> Path:
        return self.out_dir / "_console" / f"{run_id}.log"

    def configs(self) -> list[str]:
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # ---- lifecycle
    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: Optional[str] = None,
              llm_provider: Optional[str] = None) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        run_id = f"web-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        zip_path = self.out_dir / "_uploads" / f"{run_id}-{safe}"
        zip_path.write_bytes(zip_bytes)
        for c in configs:
            if not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        spec = RunSpec(run_id, zip_path, self.out_dir, [f"config/{c}" for c in configs], stop_after, llm_provider)
        cmd = self.command(spec)
        console = open(self.console_log(run_id), "w", encoding="utf-8")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "160"}
        proc = subprocess.Popen(cmd, cwd=str(self.repo_root), stdout=console, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env)
        with self._lock:
            self.procs[run_id] = proc
            self.specs[run_id] = spec
        log.info("run %s started: %s", run_id, " ".join(cmd))
        return run_id

    def stop(self, run_id: str) -> bool:
        proc = self.procs.get(run_id)
        if proc is None or proc.poll() is not None:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True

    def status(self, run_id: str) -> dict[str, Any]:
        proc = self.procs.get(run_id)
        rd = self.run_dir(run_id)
        info: dict[str, Any] = {"run_id": run_id, "run_dir": str(rd), "exists": rd.is_dir() or proc is not None}
        if proc is not None:
            code = proc.poll()
            info["state"] = "running" if code is None else "finished"
            info["exit_code"] = code
        else:
            info["state"] = "finished" if (rd / "report.json").is_file() else ("unknown" if rd.is_dir() else "missing")
            info["exit_code"] = None
        rep = rd / "report.json"
        if rep.is_file():
            try:
                data = json.loads(rep.read_text(encoding="utf-8"))
                info["success"] = data.get("success")
                info["blocked"] = len(data.get("blocked") or [])
                info["unresolved"] = len(data.get("unresolved") or [])
                info["llm_calls"] = (data.get("llm_usage") or {}).get("calls")
            except (OSError, ValueError):
                pass
        for name in SERVED_FILES:
            if (rd / name).is_file():
                info.setdefault("files", []).append(name)
        return info

    def list_runs(self) -> list[dict[str, Any]]:
        ids = {p.name for p in self.out_dir.iterdir() if p.is_dir() and not p.name.startswith("_")} | set(self.procs)
        rows = []
        for rid in ids:
            st = self.status(rid)
            try:
                st["started"] = (self.run_dir(rid)).stat().st_mtime if self.run_dir(rid).exists() else time.time()
            except OSError:
                st["started"] = 0
            rows.append(st)
        return sorted(rows, key=lambda r: r.get("started", 0), reverse=True)

    # ---- streaming
    def events(self, run_id: str) -> Iterator[tuple[str, Any]]:
        """Yield (event, data) until the run is finished and every file has been drained."""
        rd = self.run_dir(run_id)
        offsets: dict[Path, int] = {}
        files = [("log", rd / "sbmig.log"), ("journal", rd / "journal.jsonl"), ("console", self.console_log(run_id))]
        last_beat = time.monotonic()
        yield "status", self.status(run_id)
        while True:
            emitted = False
            for kind, path in files:
                for line in _new_lines(path, offsets):
                    emitted = True
                    if kind == "journal":
                        try:
                            yield "journal", json.loads(line)
                        except ValueError:
                            yield "console", line
                    else:
                        yield kind, line
            st = self.status(run_id)
            finished = st["state"] in ("finished", "missing")
            if finished:
                # one more drain after the process ended, then the report and the end marker
                for kind, path in files:
                    for line in _new_lines(path, offsets):
                        yield (("journal", json.loads(line)) if kind == "journal" and line.startswith("{")
                               else (kind, line))
                rep = rd / "report.md"
                if rep.is_file():
                    yield "report", rep.read_text(encoding="utf-8", errors="replace")
                yield "status", st
                yield "end", {"run_id": run_id}
                return
            if emitted:
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat > KEEPALIVE_S:
                yield "ping", {"t": time.time()}
                last_beat = time.monotonic()
            time.sleep(POLL_S)


def _new_lines(path: Path, offsets: dict[Path, int]) -> list[str]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    pos = offsets.get(path, 0)
    if size <= pos:
        return []
    with open(path, "rb") as fh:
        fh.seek(pos)
        chunk = fh.read(size - pos)
    nl = chunk.rfind(b"\n")
    if nl < 0:                                   # partial line: wait for the rest
        return []
    offsets[path] = pos + nl + 1
    return chunk[:nl].decode("utf-8", errors="replace").splitlines()


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "sbmig-web/1"
    manager: RunManager  # set on the server

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default; sbmig.web logger at DEBUG
        log.debug("%s " + fmt, self.address_string(), *args)

    # ---- helpers
    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, download: bool = False) -> None:
        if not path.is_file():
            self._json(404, {"error": f"{path.name} not found"})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _run_id(self, raw: str) -> Optional[str]:
        if RUN_ID_RE.match(raw):
            return raw
        self._json(400, {"error": "bad run id"})
        return None

    # ---- routes
    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        mgr: RunManager = self.server.manager  # type: ignore[attr-defined]
        parts = [p for p in url.path.split("/") if p]
        if not parts or url.path == "/index.html":
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parts == ["api", "configs"]:
            self._json(200, {"configs": mgr.configs(), "repo_root": str(mgr.repo_root), "out_dir": str(mgr.out_dir)})
        elif parts == ["api", "runs"]:
            self._json(200, {"runs": mgr.list_runs()})
        elif len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            rid = self._run_id(parts[2])
            if rid is None:
                return
            if len(parts) == 3:
                st = mgr.status(rid)
                self._json(200 if st["exists"] else 404, st)
            elif parts[3] == "events":
                self._sse(mgr, rid)
            elif parts[3] == "files" and len(parts) == 5 and parts[4] in SERVED_FILES:
                self._file(mgr.run_dir(rid) / parts[4], SERVED_FILES[parts[4]],
                           download="download" in q or parts[4].endswith(".zip"))
            else:
                self._json(404, {"error": "unknown route"})
        else:
            self._json(404, {"error": "unknown route"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        mgr: RunManager = self.server.manager  # type: ignore[attr-defined]
        parts = [p for p in url.path.split("/") if p]
        if parts == ["api", "runs"]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_UPLOAD:
                self._json(400, {"error": f"zip body required (max {MAX_UPLOAD // (1024 * 1024)} MB)"})
                return
            data = self.rfile.read(length)
            if data[:2] != b"PK":
                self._json(400, {"error": "body is not a zip file"})
                return
            configs = [c for c in (q.get("configs", [""])[0]).split(",") if c]
            try:
                rid = mgr.start(data, q.get("name", ["project.zip"])[0], configs,
                                stop_after=(q.get("stop_after") or [None])[0] or None,
                                llm_provider=(q.get("llm_provider") or [None])[0] or None)
            except (ValueError, OSError) as e:
                self._json(400, {"error": str(e)})
                return
            self._json(202, {"run_id": rid, "status": mgr.status(rid)})
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stop":
            rid = self._run_id(parts[2])
            if rid is not None:
                self._json(200, {"stopped": mgr.stop(rid)})
        else:
            self._json(404, {"error": "unknown route"})

    def _sse(self, mgr: RunManager, rid: str) -> None:
        if not mgr.status(rid)["exists"]:
            self._json(404, {"error": "unknown run"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event, data in mgr.events(rid):
                payload = json.dumps(data, ensure_ascii=False, default=str)
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.debug("sse client for %s went away", rid)


def make_server(host: str, port: int, manager: RunManager) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.manager = manager  # type: ignore[attr-defined]
    return srv


def serve(host: str = "127.0.0.1", port: int = 8765, out_dir: Path = Path("out/web"),
          repo_root: Path = PACKAGE_ROOT) -> None:
    mgr = RunManager(Path(out_dir), repo_root)
    srv = make_server(host, port, mgr)
    log.info("sbmig web UI on http://%s:%d  (runs under %s)", host, srv.server_address[1], mgr.out_dir)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
