"""웹 앱의 프로세스 안 실행 관리자: 이관마다 작업 스레드 하나, SSE 로 흘릴 이벤트를 모은다.
흐름상 위치: app.py 라우트 → :meth:`RunManager.start_file` (검사·설정·워크스페이스·오케스트레이터를 동기로
만들어 잘못된 요청은 400) → 작업 스레드 ``_worker`` → ``stream_graph`` 노드마다 기록 →
:meth:`RunManager.events` 가 그 기록과 ``journal.jsonl`` / ``sbmig.log`` 의 새 줄을 SSE 이벤트로 바꾼다.
지켜야 할 것: (1) 이관은 한 번에 하나 — KB 파일을 모두 함께 쓰므로 ``_turns`` (올린 순서대로의 FIFO
줄)에서 줄을 서고, 기다리는 실행은 ``queued``. (2) 멈춤은 그래프 노드 사이에서만 — LangGraph 는 진행 중인
동기 노드(Maven, OpenRewrite, LLM 호출)를 끊지 못한다. (3) 실행 로그는 스레드 필터로 나눈다 — 각
``sbmig.log`` 에는 그 실행 스레드의 줄만. (4) 메모리에는 끝난 실행을 ``max_finished_runs`` 개만 두고,
나머지는 실행 폴더의 파일로 답한다.

In-process run manager for the web app: one worker thread per migration, events for the SSE stream.

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
migrates projects strictly sequentially), so worker threads queue on `RunManager._turns`; a run waiting for
its turn reports state ``queued``. The queue is FIFO in upload order (2026-10-28, A10): a run takes its place
in the line when `start_file` accepts it (before its thread starts) and starts when it is first in line — a
plain `threading.Lock` (the earlier `_run_lock`) does not hand itself over in arrival order, so with two or
more uploads waiting the later one could start first.

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
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
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
                "plan.json": "application/json", "profile.json": "application/json",
                # v2.1: the run's error list — every error with its tries, the LLM's answer and the applied diff
                "errors.md": "text/markdown; charset=utf-8",
                "errors.jsonl": "application/x-ndjson; charset=utf-8"}
# 파일 꼬리(journal / sbmig.log)는 알림이 없으므로 0.5 초마다 다시 본다. 15 초 조용하면 ping: 몇 분짜리 Maven
# 빌드 동안 브라우저·프록시가 연결을 끊지 않게
POLL_S = 0.5
KEEPALIVE_S = 15.0
MAX_UPDATE_CHARS = 20_000          # a node update larger than this is summarized (keys only) in the `node` event
EXIT_ABORTED = 3                   # = sbmig.cli.EXIT_ABORTED (worker crashed / stopped by the user)
WEB_STATUS_FILE = "web-status.json"   # the worker's final status, read back for runs no longer in memory
DEFAULT_MAX_FINISHED_RUNS = 200


@dataclass
class Run:
    """이 서버 프로세스가 시작한 실행 하나의 메모리 상태: 이벤트 기록(``history``), 멈춤 요청(``cancel``),
    대기열을 벗어남(``active``), 끝남(``done``). 앞선 프로세스의 실행은 폴더로만 남는다.

    A run started by this server process (runs from earlier processes only exist as directories)."""
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


class _FifoTurns:
    """이관 차례 줄(FIFO): 한 번에 하나, 올린 순서대로. ``join`` 으로 줄 끝에 서고(업로드를 받을 때, 작업
    스레드 시작 전), ``turn`` 이 줄 맨 앞이 될 때까지 기다렸다가 끝나면(예외여도) 줄에서 빠지며 다음을 깨운다.
    ``threading.Lock`` 은 기다리는 스레드 중 누구에게 넘어갈지 정하지 않아 순서가 뒤바뀔 수 있었다(A10).

    One migration at a time, in upload order: `join` takes a place at the end of the line (when the upload
    is accepted, before the worker thread starts); `turn(key)` waits until `key` is first, and leaving it
    (even by an exception) removes `key` and wakes the next one. A plain `threading.Lock` does not grant
    waiters in order."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._line: deque[str] = deque()

    def join(self, key: str) -> None:
        """줄 끝에 선다(같은 키가 이미 있으면 그대로)."""
        with self._cond:
            if key not in self._line:
                self._line.append(key)

    def leave(self, key: str) -> None:
        """줄에서 빠지고(어디에 있든) 기다리는 스레드를 깨운다. 없는 키면 아무 일도 없다."""
        with self._cond:
            try:
                self._line.remove(key)
            except ValueError:
                pass
            self._cond.notify_all()

    @contextmanager
    def turn(self, key: str) -> Iterator[None]:
        """``key`` 차례가 올 때까지 기다린 뒤 블록을 돌리고, 끝나면 줄에서 뺀다. ``join`` 없이 불렀으면 지금
        줄 끝에 선다(늦게 온 만큼 뒤에서 기다린다)."""
        with self._cond:
            if key not in self._line:
                self._line.append(key)
            while self._line[0] != key:       # `key` stays in line until this block leaves: never empty
                self._cond.wait()
        try:
            yield
        finally:
            self.leave(key)


_LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d{3}))?\b")


def _event_time(kind: str, data: Any) -> float | None:
    """journal 이벤트(``ts``, UTC ISO 8601)나 ``sbmig.log`` 줄(로컬 시각)의 epoch 초 — 두 파일을 시간순으로
    섞으려고. 시각이 없는 줄(이어지는 줄, traceback)은 None.

    Epoch seconds of a journal event (`ts`, ISO 8601 UTC) or a `sbmig.log` line (`%(asctime)s`, local time);
    None when the line carries no timestamp (continuation lines, tracebacks)."""
    try:
        if kind == "journal" and isinstance(data, dict) and data.get("ts"):
            return datetime.fromisoformat(str(data["ts"])).timestamp()
        if kind == "log" and isinstance(data, str):
            m = _LOG_TS_RE.match(data)
            if m:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2) or 0) / 1000.0
    except (ValueError, OverflowError, OSError):
        return None
    return None


class RunManager:
    """이관을 작업 스레드로 시작하고, 상태·이벤트 질문에 메모리 + 실행 폴더로 답한다.
    ``_lock`` 은 ``runs`` dict 를, ``_turns`` 는 이관 자체를 올린 순서대로 직렬화한다(파일 KB 공유).

    Starts migrations in worker threads and answers status / event questions from memory + the run directory."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, *,
                 max_finished_runs: int = DEFAULT_MAX_FINISHED_RUNS):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.max_finished_runs = max(0, int(max_finished_runs))
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._turns = _FifoTurns()             # one migration at a time, in upload order (shared file KB)
        self.closing = threading.Event()       # server shutdown: open SSE streams end, running runs are asked to stop

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        """실행 폴더 ``<out>/<run id>`` (호출하는 라우트가 ``RUN_ID_RE`` 로 먼저 검사한다)."""
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        """페이지에서 고를 수 있는 ``config/*.yml`` 이름들. ``default.yml`` 은 늘 먼저 읽히므로 뺀다."""
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # ---- lifecycle
    def incoming_path(self) -> Path:
        """받는 중인 본문을 쓸 새 임시 파일 ``<out>/_uploads/.incoming-<hex>.part``. 최종 zip 과 같은 폴더라
        ``start_file`` 이 복사 없이 이름만 바꿔 옮긴다.

        A fresh `<out>/_uploads/.incoming-<hex>.part` for a body that is still arriving (same directory as the
        final `<run id>-<name>` so `start_file` can rename it without a copy)."""
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f".incoming-{uuid.uuid4().hex}.part"

    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: str | None = None,
              llm_provider: str | None = None) -> str:
        """메모리에 이미 있는 본문용 ``start_file`` (테스트, 다른 코드에서 부를 때). ValueError = 잘못된 요청.

        `start_file` for a body already in memory (tests, embedding callers). ValueError = bad request."""
        tmp = self.incoming_path()
        tmp.write_bytes(zip_bytes)
        try:
            return self.start_file(tmp, name, configs, stop_after, llm_provider)
        finally:
            tmp.unlink(missing_ok=True)

    def start_file(self, incoming: Path, name: str, configs: list[str], stop_after: str | None = None,
                   llm_provider: str | None = None) -> str:
        """검사 → 올라온 zip 을 실행 ID 이름으로 옮김 → 설정·워크스페이스·오케스트레이터 생성 → 작업 스레드
        시작. ValueError 면 ``incoming`` 은 호출자 몫(라우트가 지운다), 옮긴 뒤에는 실행 몫이다. zip 이 깨져
        워크스페이스를 못 만들면 실행 폴더·zip 을 남기지 않는다(유령 실행 방지).

        Validate, move the uploaded zip (`incoming`, written by the caller) under its run id, build config +
        workspace + orchestrator, start the worker. ValueError = bad request — `incoming` is then left to the caller
        (the web route deletes it); after the move it belongs to the run."""
        from sbmig.core.config import load_config
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator, parse_stop_after

        # 설정 이름도 RUN_ID_RE 로 검사: ``../`` 로 config 폴더 밖의 파일을 읽지 못하게
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
        # daemon: 서버 프로세스가 끝나면 스레드도 함께 끝난다(정리는 shutdown 의 멈춤 요청 + Maven 종료)
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-run-{run_id}", daemon=True)
        with self._lock:
            self.runs[run_id] = run
            # 줄은 업로드를 받은 지금 선다 — 스레드가 실제로 뜨는 순서는 정해져 있지 않으므로
            # 작업 스레드 안에서 서면 순서가 다시 뒤바뀔 수 있다
            self._turns.join(run_id)           # the place in line is taken here, in upload order (A10)
        try:
            run.thread.start()
        except RuntimeError:                   # no thread started: leave the line, or every later run waits
            self._turns.leave(run_id)
            with self._lock:
                self.runs.pop(run_id, None)
            raise
        log.info("run %s started: %s configs=%s stop_after=%s llm_provider=%s", run_id, zip_path.name,
                 ",".join(configs) or "-", stop_after or "-", llm_provider or "-")
        return run_id

    def _worker(self, run: Run) -> None:
        """작업 스레드 본체. ``_turns`` 줄 맨 앞이 될 때까지 ``queued``, 차례가 오면 ``active`` → 이
        스레드 전용 ``sbmig.log`` 를 붙이고 이관을 돌린 뒤 ``SBMIG-RESULT`` 줄을 남긴다. 무슨 일이
        있어도 finally 에서 로그를 떼고 ``finished`` 이벤트·web-status.json 을 쓰고 ``done`` 을 세워
        기다리는 SSE 를 깨운다."""
        log_path: Path | None = None
        try:
            # import 도 try 안에서: 여기서 죽어도 finally 가 줄에서 빼야 뒤의 실행이 영원히 기다리지 않는다
            from sbmig.cli import attach_file_log, run_exit_code

            # 한 번에 하나: 파일 KB 를 모든 실행이 함께 쓴다. 차례는 올린 순서(FIFO 줄, start_file 에서 섬) —
            # 예전의 threading.Lock 은 기다리는 스레드에게 넘겨주는 순서를 보장하지 않았다(A10)
            with self._turns.turn(run.run_id):         # one migration at a time, in upload order (docstring)
                run.active.set()
                # thread_id: 이 스레드(와 그 컨텍스트를 물려받은 LangGraph 실행기 스레드)의 로그만 이 파일로 —
                # 같은 프로세스의 다른 실행·서버 로그가 섞이지 않게
                log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
                report = self._run_thread_body(run)
                run.report = report
                run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
                if report is not None:          # the line operators retype (round 10), also in this run's sbmig.log
                    from sbmig.report.result_line import report_result_line
                    log.info("%s", report_result_line(report, run.exit_code, run_id=run.run_id))
        except Exception as e:  # the orchestrator never raises; a crash here is a bug in the engine / this module
            log.exception("run %s failed", run.run_id)
            run.error = f"{type(e).__name__}: {e}"
            run.exit_code = EXIT_ABORTED
            self._push(run, "error", {"message": run.error})
        finally:
            self._turns.leave(run.run_id)      # already left after a turn; this covers a failure before it
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
        """끝난 실행이 ``max_finished_runs`` 를 넘으면 오래된 것부터 메모리에서 뺀다(호출자가 ``self._lock``
        보유). 실행 중·대기 중인 것은 남고, 빠진 실행은 실행 폴더의 파일로 답한다.

        Drop the oldest finished runs beyond `max_finished_runs` from memory (caller holds `self._lock`). Running
        and queued runs stay; evicted runs are answered from their run directory."""
        finished = sorted((r for r in self.runs.values() if r.done.is_set()), key=lambda r: r.started)
        excess = len(finished) - self.max_finished_runs
        for r in finished[:max(0, excess)]:
            self.runs.pop(r.run_id, None)
            log.debug("run %s evicted from memory (max_finished_runs=%d)", r.run_id, self.max_finished_runs)

    def _run_thread_body(self, run: Run) -> Any:
        """이관을 돌리고 최종 RunReport 를 돌려준다(테스트가 이 메서드를 바꿔 끼운다). LangGraph 가 없으면
        inline 엔진(노드 이벤트 없음, 멈춤도 안 먹음). 노드가 끝날 때마다 ``node`` 이벤트를 쌓고 멈춤 요청을
        본다: 최종 빌드(verify)·finalize 뒤면 무시, 아니면 aborted 표시 → 그래프 닫기 → 보고서 마감.

        Drive the migration; return the final `RunReport`. Tests replace this method."""
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
                # 최종 빌드가 이미 돌았으면 이관은 끝난 것: 여기서 멈추면 성공을 중단으로 잘못 적을 뿐이다
                if name == "finalize" or (name == "phase" and update.get("phase") == Phase.VERIFY.value):
                    # the final build already ran: the migration is complete, a stop now would only mislabel it
                    log.warning("run %s: stop requested after the final build — finishing normally", run.run_id)
                    continue
                log.warning("run %s: stop requested — stopping after node %s (no final build)", run.run_id, name)
                # 닫기 전에 aborted 표시: 엔진은 일찍 닫힐 때 보고서를 마감하므로, 그 뒤에 표시하면 늦다
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
        """실행 기록에 이벤트를 붙이고 기다리는 SSE 스트림을 깨운다(``node`` 면 마지막 노드 이름도 갱신)."""
        with run.cond:
            run.history.append((kind, data))
            if kind == "node":
                run.last_node = data.get("node")
            run.cond.notify_all()

    def stop(self, run_id: str) -> bool:
        """지금 그래프 노드가 끝나면 멈추라고 요청한다(진행 중인 Maven·LLM 호출은 끊지 않는다).
        모르는 실행이거나 이미 끝났으면 False. 대기 중인 실행은 차례가 와서 첫 노드를 마친 뒤 멈춘다.

        Ask a running run to stop after the current graph node. False if the run is unknown or finished."""
        run = self.runs.get(run_id)
        if run is None or run.done.is_set():
            return False
        run.cancel.set()
        log.info("run %s: stop requested", run_id)
        return True

    def shutdown(self, reason: str = "server shutdown") -> dict[str, Any]:
        """서버 종료(lifespan): 열린 SSE 스트림을 모두 끝내고, 실행 중·대기 중인 실행에 멈춤을 요청하고,
        아직 도는 Maven / OpenRewrite 프로세스를 죽인다. 안 하면 uvicorn 이 SSE 연결을 기다리고, 윈도우에서는
        java.exe 가 서버보다 오래 산다(2026-10-21).

        Server shutdown (the web app's lifespan): end every open event stream, ask every running / queued run to
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
        """실행 상태 dict. 메모리에 있으면 queued / running / finished, 없으면(밀려남·이전 프로세스)
        web-status.json → report.json → 폴더 유무 순으로 finished / unknown / missing.
        report.json 이 있으면 성공·차단·미해결·LLM 호출·오류 목록 개수, 그리고 내려받을 수 있는 파일 목록."""
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
                from sbmig.report.result_line import entry_counts
                info["open"], info["resolved"] = entry_counts(data.get("error_entries") or [])   # v2.1 error list
            except (OSError, ValueError):
                pass
        for name in SERVED_FILES:
            if (rd / name).is_file():
                info.setdefault("files", []).append(name)
        return info

    def list_runs(self) -> list[dict[str, Any]]:
        """메모리와 출력 폴더의 모든 실행(``_`` 로 시작하는 폴더 제외)의 상태를 최신순으로."""
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
        """SSE 로 흘릴 (이벤트, 데이터)를 실행이 끝나고 다 비울 때까지 내는 막히는 생성기(스레드풀에서 돈다).
        처음 ``status`` → 노드·오류 기록과 journal·로그 파일의 새 줄(한 번에 모아 시간순) → 조용하면 ``ping``
        → 끝나면 ``report`` / ``status`` / ``end``. 서버 종료 때는 ``end`` (reason) 로 연결을
        놓는다(브라우저가 나중에 다시 붙는다).

        Yield (event, data) until the run is finished and everything has been drained (blocking generator)."""
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
            # journal 과 로그는 같은 스레드가 쓰는 두 파일이라, 하나씩 비우면 화면에서 시간이 뒤섞인다 → 한
            # 번에 모은 것을 시각으로 정렬한다(시각 없는 줄은 앞 줄의 시각을 따라 파일 순서 유지)
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
            # 끝났는지는 비우기 *전에* 본다: done 이 선 뒤의 비우기라면 실행이 쓴 것이 모두 파일에 들어 있다
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
            # 서버 종료: 연결을 놓아야 uvicorn 이 기다리지 않고 내려간다
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
    """작업 스레드가 끝날 때 web-status.json(종료 코드·오류·멈춤 요청·마지막 노드)을 쓴다 — 메모리에서
    밀려났거나 서버를 다시 띄운 뒤에도 상태를 답하려고. 못 써도 실행 결과에는 영향이 없다."""
    try:
        data = {"run_id": run.run_id, "exit_code": run.exit_code, "error": run.error,
                "stop_requested": run.cancel.is_set(), "last_node": run.last_node,
                "success": getattr(run.report, "success", None)}
        (run.run_dir / WEB_STATUS_FILE).write_text(json.dumps(data, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:   # the status file is a convenience; never fail the run on it
        log.debug("cannot write %s for %s: %s", WEB_STATUS_FILE, run.run_id, e)


def _read_web_status(rd: Path) -> dict[str, Any] | None:
    """web-status.json 을 읽는다(없거나 깨졌으면 None)."""
    try:
        data = json.loads((rd / WEB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _mark_stopped(orch: Any) -> None:
    """사용자 멈춤: 아직 마감 안 된 보고서를 aborted 로 표시한다(report.md 에 남고 종료 코드 3).
    이미 마감됐거나 이미 aborted 면 아무것도 안 한다.

    A user stop: flag the (not yet finalized) report as aborted, so report.md says so and the exit code is 3."""
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
    """멈춘 뒤 그래프는 노드 사이에 남아 있다: 엔진이 닫힐 때 보고서를 마감하지 않았다면 여기서 마감한다
    (report.md / report.json 쓰기). 예외는 로그만 남기고 삼킨다 — 멈춤 처리 때문에 보고서가 사라지지 않게.

    After a stop the graph was left between nodes: if the engine did not finalize the report on close, do it
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
    """이 파일의 실행별(스레드 필터) 핸들러만 뗀다. 같은 파일의 프로세스 전체 핸들러가 있으면 남긴다.

    Remove the per-run (thread-filtered) handlers of `path`; the process-wide one, if any, stays."""
    root = logging.getLogger("sbmig")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == Path(path).resolve() \
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
    """그래프 상태 갱신을 JSON 데이터로 바꾼다.
    ``MAX_UPDATE_CHARS`` 를 넘으면 키 이름만 보낸다(페이지가 무거워지지 않게).

    A graph state update as plain JSON data (small; big updates are reduced to their keys)."""
    try:
        text = json.dumps(update, default=_json_default, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(str(update))
    if len(text) > MAX_UPDATE_CHARS:
        keys = sorted(update) if isinstance(update, dict) else []
        return {"_truncated": len(text), "keys": keys}
    return json.loads(text)


def _new_lines(path: Path, offsets: dict[Path, int]) -> list[str]:
    """파일에 새로 붙은 *완성된* 줄만 돌려준다(읽은 위치를 ``offsets`` 에 기억). 줄바꿈이 아직 안 온 마지막
    조각은 다음 번으로 미룬다 — 쓰는 중인 줄을 반쪽으로 보내거나 JSON 을 깨진 채 읽지 않게."""
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
