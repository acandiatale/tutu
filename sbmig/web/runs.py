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
after the current node and is then finalized as aborted ("stopped by user"; exit code 3). With the inline fallback
engine there are no intermediate updates and the flag has no effect.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
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
    done: threading.Event = field(default_factory=threading.Event)
    report: Any = None
    exit_code: int | None = None
    error: str | None = None
    last_node: str | None = None
    started: float = field(default_factory=time.time)


class RunManager:
    """Starts migrations in worker threads and answers status / event questions from memory + the run directory."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # ---- lifecycle
    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: str | None = None,
              llm_provider: str | None = None) -> str:
        """Validate, save the zip, build config + workspace + orchestrator, start the worker.
        ValueError = bad request."""
        from sbmig.core.config import load_config
        from sbmig.core.models import Phase
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator

        for c in configs:
            if not RUN_ID_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        if stop_after:
            try:
                Phase(stop_after.lower())
            except ValueError:
                raise ValueError(f"stop_after must be one of: {', '.join(p.value for p in Phase)}") from None
            stop_after = stop_after.lower()
        overrides = {"llm": {"provider": llm_provider}} if llm_provider else None
        cfg = load_config(*[self.repo_root / "config" / c for c in configs], overrides=overrides,
                          repo_root=self.repo_root)        # pydantic ValidationError is a ValueError

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        run_id = f"web-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        zip_path = uploads / f"{run_id}-{safe}"
        zip_path.write_bytes(zip_bytes)
        ws = Workspace.from_input(zip_path, self.out_dir, run_id=run_id)
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

        log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
        try:
            report = self._run_thread_body(run)
            run.report = report
            run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
        except Exception as e:  # the orchestrator never raises; a crash here is a bug in the engine / this module
            log.exception("run %s failed", run.run_id)
            run.error = f"{type(e).__name__}: {e}"
            run.exit_code = EXIT_ABORTED
            self._push(run, "error", {"message": run.error})
        finally:
            log.info("run %s finished: exit code %s", run.run_id, run.exit_code)
            _detach_file_log(log_path)
            self._push(run, "finished", {"exit_code": run.exit_code,
                                         "success": getattr(run.report, "success", None)})
            run.done.set()
            with run.cond:
                run.cond.notify_all()

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
        try:
            for name, update in updates:
                self._push(run, "node", {"node": name, "update": _jsonable(update)})
                if run.cancel.is_set():
                    log.warning("run %s: stop requested — stopping after node %s", run.run_id, name)
                    _mark_stopped(orch)          # before close(): the engine finalizes the report when closed early
                    break
        finally:
            close = getattr(updates, "close", None)
            if callable(close):
                close()
        if run.cancel.is_set():
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

    def status(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        rd = self.run_dir(run_id)
        info: dict[str, Any] = {"run_id": run_id, "run_dir": str(rd), "exists": rd.is_dir() or run is not None}
        if run is not None:
            info["state"] = "finished" if run.done.is_set() else "running"
            info["exit_code"] = run.exit_code
            info["node"] = run.last_node
            if run.cancel.is_set():
                info["stop_requested"] = True
            if run.error:
                info["error"] = run.error
        else:   # a run of an earlier server process (or not started here): only the directory is known
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
            for kind, path in (("journal", rd / "journal.jsonl"), ("log", rd / "sbmig.log")):
                for line in _new_lines(path, offsets):
                    if kind == "journal":
                        try:
                            yield "journal", json.loads(line)
                            continue
                        except ValueError:
                            pass
                    yield "log", line

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


def _mark_stopped(orch: Any) -> None:
    """A user stop: flag the (not yet finalized) report as aborted, so report.md says so and the exit code is 3."""
    report = getattr(orch, "report", None)
    if report is None or getattr(report, "finished_at", None) is not None or "aborted" in report.flags:
        return
    try:
        abort = getattr(orch, "_abort", None)
        if callable(abort):
            abort(report, "stopped by user (web UI)")
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
