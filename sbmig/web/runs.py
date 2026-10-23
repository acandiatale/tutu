"""In-process run manager for the web app: one worker thread per migration, events for the SSE stream.

A run = an uploaded zip + the ticked `config/*.yml` files. `start()` validates the request, builds the `Config`,
the `Workspace` (`<out>/<run id>/`) and the `Orchestrator` synchronously (so a bad request fails with a 400), then a
worker thread drives the migration through `sbmig.graph.stream_graph` and records every graph node update. When
`sbmig.graph` is not importable (no LangGraph installed) the worker falls back to `Orchestrator.run()` — same
report, same exit code, just no `node` events.

What the SSE stream relays (`events()`):
    status   the `status()` dict (first and last event)
    node     {"node": <graph node>, "update": <JSON-able state update>} — one per finished graph node
    error    {"message": …} — the worker raised (the run is then finished with exit code 3)
    journal  one parsed `journal.jsonl` record (tailed from the run directory)
    log      one `sbmig.log` line (DEBUG included; the per-run file only holds this run's thread)
    report   the `report.md` text, once the run finished
    ping     keep-alive after 15 s of silence
    end      last event

Stop: `stop()` sets a cancel flag the worker checks between graph updates. A node in progress (a Maven build, an
OpenRewrite run, an LLM call) is not interrupted — LangGraph cannot cancel a running sync node — so the run stops
after the current node and is then finalized as aborted ("stopped by user"; exit code 3) *without* the final
`mvn clean package` (the report says so). A stop that arrives once the verify phase has run changes nothing: the run
finishes normally. With the inline fallback engine there are no intermediate updates and the flag has no effect.

One migration at a time: the knowledge base under `kb/` is file-based and shared by every run (CLAUDE.md: one agent
migrates projects strictly sequentially), so worker threads queue on `RunManager._run_lock`; a run waiting for its
turn reports state ``queued``.

Memory (round 9): `runs` keeps at most `max_finished_runs` finished runs; when a worker finishes, the oldest finished
ones are dropped from memory (running / queued runs never are). An evicted run is still answered from its run
directory: `report.json` and `web-status.json` (exit code / error / stop flag, written when the worker finishes).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sbmig.core.config import PACKAGE_ROOT

log = logging.getLogger("sbmig.web")

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SERVED_FILES = {"report.md": "text/markdown; charset=utf-8", "report.json": "application/json",
                "result.zip": "application/zip", "sbmig.log": "text/plain; charset=utf-8",
                "journal.jsonl": "application/x-ndjson", "changes.patch": "text/plain; charset=utf-8",
                "plan.json": "application/json", "profile.json": "application/json"}
POLL_S = 0.5
KEEPALIVE_S = 15.0
MAX_UPDATE_CHARS = 20_000          # a node update larger than this is summarized (keys only) in the `node` event
EXIT_ABORTED = 3                   # = sbmig.cli.EXIT_ABORTED (worker crashed / stopped by the user)
WEB_STATUS_FILE = "web-status.json"   # the worker's final status, read back for runs no longer in memory
DEFAULT_MAX_FINISHED_RUNS = 200


@dataclass
class Run:
    """A run started by this server process (runs from earlier processes only exist as directories)."""
    run_id: str
    run_dir: Path
    zip_path: Path
    configs: list[str] = field(default_factory=list)
    stop_after: str | None = None
    llm_provider: str | None = None
    orch: Any = None
    thread: threading.Thread | None = None
    history: list[tuple[str, Any]] = field(default_factory=list)    # node / error / finished events, append-only
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel: threading.Event = field(default_factory=threading.Event)
    active: threading.Event = field(default_factory=threading.Event)     # left the queue, migration in progress
    done: threading.Event = field(default_factory=threading.Event)
    report: Any = None
    exit_code: int | None = None
    error: str | None = None
    last_node: str | None = None
    started: float = field(default_factory=time.time)


_LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.](\d{3})\b")


def _event_time(kind: str, data: Any) -> float | None:
    """Epoch seconds of a journal event (`ts`, ISO 8601 UTC) or a `sbmig.log` line (`%(asctime)s`, local time);
    None when the line carries no timestamp (continuation lines, tracebacks)."""
    try:
        if kind == "journal" and isinstance(data, dict) and data.get("ts"):
            return datetime.fromisoformat(str(data["ts"])).timestamp()
        if kind == "log" and isinstance(data, str):
            m = _LOG_TS_RE.match(data)
            if m:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000.0
    except (ValueError, OverflowError, OSError):
        return None
    return None


class RunManager:
    """Starts migrations in worker threads and answers status / event questions from memory + the run directory."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, *,
                 max_finished_runs: int = DEFAULT_MAX_FINISHED_RUNS):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.max_finished_runs = max(0, int(max_finished_runs))
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()      # serializes the migrations (shared file-based KB)
        self.closing = threading.Event()       # server shutdown: open SSE streams end, running runs are asked to stop

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # ---- lifecycle
    def incoming_path(self) -> Path:
        """A fresh `<out>/_uploads/.incoming-<hex>.part` for a body that is still arriving (same directory as the
        final `<run id>-<name>` so `start_file` can rename it without a copy)."""
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f".incoming-{uuid.uuid4().hex}.part"

    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: str | None = None,
              llm_provider: str | None = None) -> str:
        """`start_file` for a body already in memory (tests, embedding callers). ValueError = bad request."""
        tmp = self.incoming_path()
        tmp.write_bytes(zip_bytes)
        try:
            return self.start_file(tmp, name, configs, stop_after, llm_provider)
        finally:
            tmp.unlink(missing_ok=True)

    def start_file(self, incoming: Path, name: str, configs: list[str], stop_after: str | None = None,
                   llm_provider: str | None = None) -> str:
        """Validate, move the uploaded zip (`incoming`, written by the caller) under its run id, build config +
        workspace + orchestrator, start the worker. ValueError = bad request — `incoming` is then left to the caller
        (the web route deletes it); after the move it belongs to the run."""
        from sbmig.core.config import load_config
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator, parse_stop_after

        for c in configs:
            if not RUN_ID_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        phase = parse_stop_after(stop_after)                  # ValueError names the accepted values
        stop_after = phase.value if phase is not None else None
        overrides = {"llm": {"provider": llm_provider}} if llm_provider else None
        cfg = load_config(*[self.repo_root / "config" / c for c in configs], overrides=overrides,
                          repo_root=self.repo_root)        # pydantic ValidationError is a ValueError

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        run_id = f"web-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        zip_path = uploads / f"{run_id}-{safe}"
        Path(incoming).replace(zip_path)
        try:
            ws = Workspace.from_input(zip_path, self.out_dir, run_id=run_id)
        except Exception:                      # BadZipFile / OSError: no run was started, leave no ghost run dir
            shutil.rmtree(self.out_dir / run_id, ignore_errors=True)
            zip_path.unlink(missing_ok=True)
            raise
        run = Run(run_id, ws.run_dir, zip_path, list(configs), stop_after, llm_provider)
        run.orch = Orchestrator(cfg, ws)                      # KB on (the web app has no --no-kb)
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-run-{run_id}", daemon=True)
        with self._lock:
            self.runs[run_id] = run
        run.thread.start()
        log.info("run %s started: %s configs=%s stop_after=%s llm_provider=%s", run_id, zip_path.name,
                 ",".join(configs) or "-", stop_after or "-", llm_provider or "-")
        return run_id

    def _worker(self, run: Run) -> None:
        from sbmig.cli import attach_file_log, run_exit_code

        log_path: Path | None = None
        try:
            with self._run_lock:                       # one migration at a time (see the module docstring)
                run.active.set()
                log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
                report = self._run_thread_body(run)
                run.report = report
                run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
        except Exception as e:  # the orchestrator never raises; a crash here is a bug in the engine / this module
            log.exception("run %s failed", run.run_id)
            run.error = f"{type(e).__name__}: {e}"
            run.exit_code = EXIT_ABORTED
            self._push(run, "error", {"message": run.error})
        finally:
            run.active.set()
            log.info("run %s finished: exit code %s", run.run_id, run.exit_code)
            if log_path is not None:
                _detach_file_log(log_path)
            self._push(run, "finished", {"exit_code": run.exit_code,
                                         "success": getattr(run.report, "success", None)})
            _write_web_status(run)
            run.done.set()
            with run.cond:
                run.cond.notify_all()
            with self._lock:
                self._prune_locked()

    def _prune_locked(self) -> None:
        """Drop the oldest finished runs beyond `max_finished_runs` from memory (caller holds `self._lock`). Running
        and queued runs stay; evicted runs are answered from their run directory."""
        finished = sorted((r for r in self.runs.values() if r.done.is_set()), key=lambda r: r.started)
        excess = len(finished) - self.max_finished_runs
        for r in finished[:max(0, excess)]:
            self.runs.pop(r.run_id, None)
            log.debug("run %s evicted from memory (max_finished_runs=%d)", r.run_id, self.max_finished_runs)

    def _run_thread_body(self, run: Run) -> Any:
        """Drive the migration; return the final `RunReport`. Tests replace this method."""
        from sbmig.core.models import Phase

        orch = run.orch
        phase = Phase(run.stop_after) if run.stop_after else None
        try:
            from sbmig.graph import stream_graph
        except ImportError as e:      # no sbmig.graph / no langgraph: inline engine, no node events (kept on purpose)
            log.info("graph engine unavailable (%s) — running the inline engine", e)
            return orch.run(stop_after=phase)
        updates = stream_graph(orch, stop_after=phase)
        stopped = False
        try:
            for name, update in updates:
                self._push(run, "node", {"node": name, "update": _jsonable(update)})
                if not run.cancel.is_set():
                    continue
                if name == "finalize" or (name == "phase" and update.get("phase") == Phase.VERIFY.value):
                    # the final build already ran: the migration is complete, a stop now would only mislabel it
                    log.warning("run %s: stop requested after the final build — finishing normally", run.run_id)
                    continue
                log.warning("run %s: stop requested — stopping after node %s (no final build)", run.run_id, name)
                _mark_stopped(orch)          # before close(): the engine finalizes the report when closed early
                stopped = True
                break
        finally:
            close = getattr(updates, "close", None)
            if callable(close):
                close()
        if stopped:
            _finalize_stopped(orch)
        return orch.report

    def _push(self, run: Run, kind: str, data: Any) -> None:
        with run.cond:
            run.history.append((kind, data))
            if kind == "node":
                run.last_node = data.get("node")
            run.cond.notify_all()

    def stop(self, run_id: str) -> bool:
        """Ask a running run to stop after the current graph node. False if the run is unknown or finished."""
        run = self.runs.get(run_id)
        if run is None or run.done.is_set():
            return False
        run.cancel.set()
        log.info("run %s: stop requested", run_id)
        return True

    def shutdown(self, reason: str = "server shutdown") -> dict[str, Any]:
        """Server shutdown (the web app's lifespan): end every open event stream, ask every running / queued run to
        stop after its current graph node, and kill the Maven / OpenRewrite processes still running — otherwise
        uvicorn waits on the SSE connections and a `java.exe` outlives the server on Windows (2026-10-21)."""
        from sbmig.build.mvn import kill_live_processes

        self.closing.set()
        stopped: list[str] = []
        for run in list(self.runs.values()):
            if run.done.is_set():
                continue
            run.cancel.set()
            stopped.append(run.run_id)
            with run.cond:
                run.cond.notify_all()
        killed = kill_live_processes(reason)
        log.info("%s: %d run(s) asked to stop (%s), %d live Maven process(es) killed", reason, len(stopped),
                 ", ".join(stopped) or "-", killed)
        return {"stopped": stopped, "killed": killed}

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        rd = self.run_dir(run_id)
        info: dict[str, Any] = {"run_id": run_id, "run_dir": str(rd), "exists": rd.is_dir() or run is not None}
        if run is not None:
            info["state"] = "finished" if run.done.is_set() else ("running" if run.active.is_set() else "queued")
            info["exit_code"] = run.exit_code
            info["node"] = run.last_node
            if run.cancel.is_set():
                info["stop_requested"] = True
            if run.error:
                info["error"] = run.error
        else:   # evicted, or a run of an earlier server process: only the directory is known
            saved = _read_web_status(rd)
            if saved is not None:
                info["state"] = "finished"
                info["exit_code"] = saved.get("exit_code")
                if saved.get("stop_requested"):
                    info["stop_requested"] = True
                if saved.get("error"):
                    info["error"] = saved["error"]
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
        ids = set(self.runs)
        if self.out_dir.is_dir():
            ids |= {p.name for p in self.out_dir.iterdir() if p.is_dir() and not p.name.startswith("_")}
        rows = []
        for rid in ids:
            st = self.status(rid)
            run = self.runs.get(rid)
            try:
                st["started"] = run.started if run else self.run_dir(rid).stat().st_mtime
            except OSError:
                st["started"] = 0
            rows.append(st)
        return sorted(rows, key=lambda r: r.get("started", 0), reverse=True)

    # ---- streaming
    def events(self, run_id: str) -> Iterator[tuple[str, Any]]:
        """Yield (event, data) until the run is finished and everything has been drained (blocking generator)."""
        rd = self.run_dir(run_id)
        run = self.runs.get(run_id)
        offsets: dict[Path, int] = {}
        seen = 0
        last_beat = time.monotonic()
        yield "status", self.status(run_id)

        def drain() -> Iterator[tuple[str, Any]]:
            nonlocal seen
            if run is not None:
                with run.cond:
                    new = run.history[seen:]
                    seen = len(run.history)
                for kind, data in new:
                    if kind in ("node", "error"):
                        yield kind, data
            # journal events and log lines are two files written by the same thread; drained one file after the
            # other they can show up out of time order in the UI ("build FAILED 09:35:05" after "mvn … 09:35:06"
            # of the *next* build) → merge each tick's batch by timestamp (round 9)
            batch: list[tuple[float, int, str, Any]] = []
            for kind, path in (("journal", rd / "journal.jsonl"), ("log", rd / "sbmig.log")):
                last_key = 0.0
                for line in _new_lines(path, offsets):
                    item: tuple[str, Any] = ("log", line)
                    if kind == "journal":
                        try:
                            item = ("journal", json.loads(line))
                        except ValueError:
                            pass
                    key = _event_time(item[0], item[1])
                    last_key = key if key is not None else last_key     # unparsable → keep its file position
                    batch.append((last_key, len(batch), item[0], item[1]))
            for _key, _n, kind, data in sorted(batch, key=lambda t: (t[0], t[1])):
                yield kind, data

        while True:
            finished = run.done.is_set() if run is not None else self.status(run_id)["state"] != "running"
            emitted = False
            for item in drain():
                emitted = True
                yield item
            if finished:          # `done` was set before this drain: everything the run wrote is in the files now
                rep = rd / "report.md"
                if rep.is_file():
                    yield "report", rep.read_text(encoding="utf-8", errors="replace")
                yield "status", self.status(run_id)
                yield "end", {"run_id": run_id}
                return
            if self.closing.is_set():     # server shutdown: release the connection, the browser reconnects later
                yield "status", self.status(run_id)
                yield "end", {"run_id": run_id, "reason": "server shutdown"}
                return
            if emitted:
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat > KEEPALIVE_S:
                yield "ping", {"t": time.time()}
                last_beat = time.monotonic()
            if run is not None:
                with run.cond:
                    if len(run.history) == seen and not run.done.is_set():
                        run.cond.wait(POLL_S)
            else:
                time.sleep(POLL_S)


def _write_web_status(run: Run) -> None:
    try:
        data = {"run_id": run.run_id, "exit_code": run.exit_code, "error": run.error,
                "stop_requested": run.cancel.is_set(), "last_node": run.last_node,
                "success": getattr(run.report, "success", None)}
        (run.run_dir / WEB_STATUS_FILE).write_text(json.dumps(data, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:   # the status file is a convenience; never fail the run on it
        log.debug("cannot write %s for %s: %s", WEB_STATUS_FILE, run.run_id, e)


def _read_web_status(rd: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((rd / WEB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _mark_stopped(orch: Any) -> None:
    """A user stop: flag the (not yet finalized) report as aborted, so report.md says so and the exit code is 3."""
    report = getattr(orch, "report", None)
    if report is None or getattr(report, "finished_at", None) is not None or "aborted" in report.flags:
        return
    try:
        abort = getattr(orch, "_abort", None)
        if callable(abort):
            abort(report, "stopped by user (web UI) — the final `mvn clean package` did not run")
        else:
            report.flags.append("aborted")
    except Exception:  # never let the stop path hide the report
        log.exception("marking the stopped run failed")


def _finalize_stopped(orch: Any) -> None:
    """After a stop the graph was left between nodes: if the engine did not finalize the report on close, do it
    here (writes report.md / report.json)."""
    report = getattr(orch, "report", None)
    if report is None or getattr(report, "finished_at", None) is not None:
        return
    _mark_stopped(orch)
    try:
        finalize = getattr(orch, "_finalize", None)
        if callable(finalize):
            finalize(report)
    except Exception:  # never let the stop path hide the report
        log.exception("finalizing the stopped run failed")


def _detach_file_log(path: Path) -> None:
    root = logging.getLogger("sbmig")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == Path(path) \
                and getattr(h, "sbmig_thread", None) is not None:
            root.removeHandler(h)
            h.close()


def _json_default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _jsonable(update: Any) -> Any:
    """A graph state update as plain JSON data (small; big updates are reduced to their keys)."""
    try:
        text = json.dumps(update, default=_json_default, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(str(update))
    if len(text) > MAX_UPDATE_CHARS:
        keys = sorted(update) if isinstance(update, dict) else []
        return {"_truncated": len(text), "keys": keys}
    return json.loads(text)


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
