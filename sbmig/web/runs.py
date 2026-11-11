"""웹 앱의 프로세스 안 실행 관리자: 이관마다 작업 스레드 하나, SSE 로 흘릴 이벤트를 모은다.
흐름상 위치: app.py 라우트 → :meth:`RunManager.start_file` (검사·설정·워크스페이스·오케스트레이터를 동기로
만들어 잘못된 요청은 400) → 작업 스레드 ``_worker`` → ``stream_graph`` 노드마다 기록 →
:meth:`RunManager.events` 가 그 기록과 ``journal.jsonl`` / ``sbmig.log`` 의 새 줄을 SSE 이벤트로 바꾼다.
지켜야 할 것: (1) 동시에 도는 이관은 Job 모드(k8s-job / local-job)에서 ``SBMIG_JOB_MAX_PARALLEL`` 개까지(기본 1), 프로세스
안(in-process)에서는 한 번에 하나 — ``_turns`` (올린 순서대로의 FIFO 줄 + 번호 붙은 자리)에서 줄을 서고, 기다리는 실행은
``queued`` 이고 줄의 몇 번째인지(``queue``)를 상태로 알린다. (2) 멈춤은 그래프 노드 사이에서만 — LangGraph 는 진행 중인
동기 노드(Maven, OpenRewrite, LLM 호출)를 끊지 못한다. (3) 실행 로그는 스레드 필터로 나눈다 — 각
``sbmig.log`` 에는 그 실행 스레드의 줄만. (4) 메모리에는 끝난 실행을 ``max_finished_runs`` 개만 두고,
나머지는 실행 폴더의 파일로 답한다. (5) Job 모드에서는 Job 을 **만드는** Main 이 설정이 읽는 환경 변수의 값 지문(``env_pins``)과
정책 ``SBMIG_JOB_ENV_PIN``(k8s-job 기본 refuse, local-job 기본 off)을 ``control/state.json`` 의 creating 전이에 함께 적는다 —
Worker 가 자기 환경과 대조한다(리뷰 2026-11-10).

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

Up to `SBMIG_JOB_MAX_PARALLEL` migrations at a time in Job mode (default 1), one at a time in-process (v2.1z-ae): worker
threads queue on `RunManager._turns`, a FIFO line plus numbered slots. In-process the LLM call counter and the per-run
log filter are process-wide, so that mode always runs one; in Job mode each migration is its own pod, the shared file
KB takes its cross-process lock for every write, and each slot builds with its own Maven local repository (the Worker,
from `SBMIG_JOB_SLOT`). A run waiting for its turn reports state ``queued`` and its place in the line (`queue`). The
queue is FIFO in upload order (2026-10-28, A10): a run takes its place in the line when `start_file` accepts it
(before its thread starts) and starts when it is first in line and a slot is free — a plain `threading.Lock` (the
earlier `_run_lock`) does not hand itself over in arrival order, so with two or more uploads waiting the later one
could start first.

Every run uses the normal file KB (v2.1z: the v2.1i `fix_mode` / `no_kb` switches are gone). The run's
`sbmig.log` and `journal.jsonl` (event `web_run`) name its configs / stop_after / LLM provider at the start; a
`fix_mode` / `kb` key left in an older run's `web-status.json` or `report.json` is ignored when read.

Memory (round 9): `runs` keeps at most `max_finished_runs` finished runs; when a worker finishes, the oldest finished
ones are dropped from memory (running / queued runs never are). An evicted run is still answered from its run
directory: `report.json` and `web-status.json` (exit code / error / stop flag, written when the worker finishes).

Environment pins (review 2026-11-10): in the Job modes the Main that creates a Job writes fingerprints of the environment
values the configuration reads (`env_pins`, never a secret-looking name) and the policy `SBMIG_JOB_ENV_PIN` (default
`refuse` in k8s-job, `off` in local-job) into `control/state.json` with the `creating` transition; the Worker compares
them with its own environment. A run adopted after a restart keeps the pins already on disk.


"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from sbmig.core.config import PACKAGE_ROOT
from sbmig.core.scrub import output_scrubbing, scrub, scrub_json
from sbmig.core.workspace import STOP_REQUEST_FILE
from sbmig.web import contract
from sbmig.web.contract import (
    CONTROL_DIR,
    OWNER_LABEL,
    RUN_LABEL,
    STATE_FILE,
    ControlState,
    JobStatus,
    RequestRecord,
    RunState,
    config_digests,
    sbmig_version,
    sha256_file,
    utc_now,
)
from sbmig.web.k8s import (
    DEFAULT_TEMPLATE,
    EXIT_GRACE_ENV,
    MODE_ENV,
    MODES,
    POLL_ENV,
    REQUIRED_PERMISSIONS,
    STATUS_GRACE_ENV,
    STOP_GRACE_ENV,
    TEMPLATE_ENV,
    TERMINAL_PHASES,
    JobState,
    K8sClient,
    K8sError,
    cluster_host_present,
    in_cluster_available,
    job_name,
    kubeconfig_path,
    render_job,
    validate_worker_template,
)

log = logging.getLogger("sbmig.web")

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SERVED_FILES = {"report.md": "text/markdown; charset=utf-8", "report.json": "application/json",
                "result.zip": "application/zip", "sbmig.log": "text/plain; charset=utf-8",
                "journal.jsonl": "application/x-ndjson", "changes.patch": "text/plain; charset=utf-8",
                "plan.json": "application/json", "profile.json": "application/json",
                # v2.1: the run's error list — every error with its tries, the LLM's answer and the applied diff
                "errors.md": "text/markdown; charset=utf-8",
                "errors.jsonl": "application/x-ndjson; charset=utf-8"}
# 리뷰 2026-11-10 — 쓰는 순간 비밀 값이 지워지는 파일: 실행 중에도 내려받을 수 있다. 나머지(보고서, 오류 목록, plan …)는
# 실행이 끝날 때의 마지막 정제를 지난 뒤에만 내준다(`RunManager.file_withheld`)
LIVE_FILES = ("sbmig.log", "journal.jsonl")
# 파일 꼬리(journal / sbmig.log)는 알림이 없으므로 0.5 초마다 다시 본다. 15 초 조용하면 ping: 몇 분짜리 Maven
# 빌드 동안 브라우저·프록시가 연결을 끊지 않게
POLL_S = 0.5
KEEPALIVE_S = 15.0
MAX_UPDATE_CHARS = 20_000          # a node update larger than this is summarized (keys only) in the `node` event
EXIT_ABORTED = 3                   # = sbmig.cli.EXIT_ABORTED (worker crashed / stopped by the user)
WEB_STATUS_FILE = "web-status.json"   # the worker's final status, read back for runs no longer in memory
JOB_STATUS_FILE = "job-status.json"   # k8s Job mode: run-job.sh writes {exit_code, finished_at} on the shared volume
SOURCE_ZIP = "source.zip"             # k8s Job mode: the uploaded zip, in the run dir on the shared volume
JOB_STALE_S = 6 * 3600                # a job run of an earlier server process older than this with no status file is "unknown"
DEFAULT_MAX_FINISHED_RUNS = 200
LOCAL_LOST = "the local job did not survive the server restart"
# Job 생성 요청의 응답이 불확실한 경우(이미 있음 / 전송 오류): 같은 이름의 Job 이 있으면 이어받는다 — 두 번 만들지 않게
_UNCERTAIN_CREATE_RE = re.compile(r"HTTP 409|RequestException|ConnectionError|Timeout|ChunkedEncodingError|ProtocolError")
# 리뷰 2026-11-10 — 자리 수의 한도(Job 모드 SBMIG_JOB_MAX_PARALLEL, 기본 1)는 Job 이 끝난 것을 확인한 뒤에야 자리를 넘겨야 지켜진다
# (_hold_until_ended, 자리마다 따로). 그 기다림의 박자:
HOLD_WARN_S = 60.0                 # 끝나지 않는 Job 을 기다리는 동안 이 간격으로 경고를 남긴다
DELETE_RETRY_S = 15.0              # Job 삭제 요청이 실패하면 이 간격으로 다시 보낸다
API_REMIND_S = 600.0               # Kubernetes API 가 계속 답하지 않으면 이 간격으로 로그에 다시 남긴다
CREATE_CONFIRM_POLLS = 3           # 생성 응답이 불확실한데 Job 이 안 보이면 이만큼 더 묻는다(요청이 늦게 닿았을 수 있다)


class _ServerClosing(Exception):
    """Kubernetes API 의 답을 기다리는 중에 서버가 내려간다 — 실행을 실패로 적지 않고 제어 상태를 그대로 둔다(다음 Main 이 이어받는다).

    The server shuts down while a run waits for the Kubernetes API: the control state is kept for the next Main."""


class _LeftLine(Exception):
    """기다리던 실행이 멈춤 요청으로 줄에서 빠졌다(``_FifoTurns.withdraw``) — 자리를 받지 않았고 Job 도 만들지 않는다.

    A waiting run was taken out of the line by a stop (`_FifoTurns.withdraw`): no slot, no Job."""


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
    # k8s Job 모드(2026-11-05): 이 실행은 다른 파드에서 돈다 — Job 이름과 마지막으로 본 상태
    job: bool = False
    job_name: str | None = None
    job_phase: str | None = None
    job_pod: str | None = None
    # v2.1z-ac: 접수 때 고정한 입력(control/request.json)과 Main 의 제어 상태(control/state.json)
    request: Any = None
    control: Any = None
    verified: bool | None = None          # True = 이 실행·시도의 job-status.json 을 받았다
    result_source: str | None = None      # "status" (job-status.json) | "container" (컨테이너 종료 코드만)
    final_state: str | None = None        # _run_job 이 정한 끝 상태(없으면 결과 검증으로 finished / lost)
    keep_state: bool = False              # 서버 종료: Job 은 계속 돈다 — state.json 을 그대로 두어 다음 Main 이 이어받게
    recover: str | None = None            # 재시작 복구: None (새로 만듦) | "probe" (creating: 있으면 이어받음) | "adopt"
    status_warned: bool = False
    status_error: str | None = None       # 받은 job-status.json 의 error(Worker 가 실행 자체를 못 했다: 요청 거부, sbmig 를 못 띄움)
    pod_step_closed: bool = False         # 이 감시에서 '파드' 단계를 닫는 이벤트(completed / failed)를 이미 보냈다 — 한 번만
    # Kubernetes API 가 답하지 않는 동안(리뷰 2026-11-10): 처음 실패한 때, 마지막으로 크게 알린 때(monotonic), 화면에 보일 한 줄
    api_down_since: float | None = None
    api_down_reported: float | None = None
    job_note: str | None = None
    success: bool | None = None
    # v2.1z-ae: 이 Job 실행이 쥔(쥐었던) 자리 번호 — Job 의 env SBMIG_JOB_SLOT, control/state.json 의 slot
    slot: int | None = None

    @property
    def attempt(self) -> int:
        """이 실행의 시도 번호(제어 상태의 값, 없으면 1). / The attempt number (from the control state; 1 without one)."""
        return int(self.control.attempt) if self.control is not None else 1

    @property
    def owner(self) -> str | None:
        """Job 라벨 ``sbmig-owner`` 의 값(요청마다 난수). / The per-request owner token (label `sbmig-owner`)."""
        return self.control.owner if self.control is not None else None


class _FifoTurns:
    """이관 차례 줄(FIFO) + 번호 붙은 자리(v2.1z-ae): 올린 순서대로, 동시에 ``slots`` 개까지. ``join`` 으로 줄 끝에 서고(업로드를
    받을 때, 작업 스레드 시작 전), ``turn`` 이 **줄 맨 앞이고 빈 자리가 있을 때까지** 기다렸다가 비어 있는 가장 작은 번호(1 부터)를
    차지하고 그 번호를 내준다 — 아무도 앞사람을 앞지르지 못한다. 블록을 떠나면(예외여도) 자리를 비우고 기다리는 스레드를 깨운다.
    ``occupy`` 는 재시작 복구 전용: 클러스터에 이미 있는(있을 수 있는) Job 은 이미 돌고 있으므로 한도와 상관없이 지금 자리를 잡는다 —
    적힌 번호 그대로(다른 키가 쥐었어도; 새 실행은 어느 키도 쥐지 않은 번호만 받는다). ``withdraw`` 는 멈춘 대기 실행을 줄에서 뺀다.
    ``threading.Lock`` 은 기다리는 스레드 중 누구에게 넘어갈지 정하지 않아 순서가 뒤바뀔 수 있었다(A10). ``slots == 1`` 이면 예전과 같다.

    Upload-order line plus numbered slots: `join` takes a place at the end of the line (when the upload is accepted,
    before the worker thread starts); `turn(key)` waits until `key` is first in line AND fewer than `slots` keys hold a
    slot (strict FIFO), takes the lowest free slot number from 1 and yields it; leaving the block (even by an exception)
    frees the slot and wakes the waiters. `occupy` (restart recovery only) takes a slot at once, whatever the limit, and
    keeps the recorded number even when another key holds it (a new run only gets a number no key holds); `withdraw`
    takes a stopped waiting key out of the line. A
    plain `threading.Lock` does not grant waiters in order. With `slots == 1` the behaviour is the old one-at-a-time."""

    def __init__(self, slots: int = 1) -> None:
        self.slots = max(1, int(slots))
        self._cond = threading.Condition()
        self._line: deque[str] = deque()
        # key → slot number. 새 실행(turn)이 받는 번호는 어느 키도 쥐지 않은 번호뿐이다. 복구(occupy)만 적힌 번호를 그대로 지켜 두 키가
        # 한 번호를 쥘 수 있다 — 두 파드가 실제로 그 번호로 돌고 있으므로(리뷰 2026-11-10)
        self._active: dict[str, int] = {}
        self._withdrawn: set[str] = set()       # 기다리다 줄에서 빠진 키(멈춤) — turn() 이 깨어나 _LeftLine 을 낸다

    def _lowest_free(self) -> int:
        # 호출자가 _cond 를 쥐고 있다. 어느 키도 쥐지 않은 가장 작은 번호(1 부터)
        used = set(self._active.values())
        n = 1
        while n in used:
            n += 1
        return n

    def join(self, key: str) -> None:
        """줄 끝에 선다(같은 키가 이미 줄에 있거나 자리를 쥐고 있으면 그대로).

        Take a place at the end of the line (no-op when `key` is already waiting or holds a slot)."""
        with self._cond:
            if key not in self._line and key not in self._active:
                self._line.append(key)

    def leave(self, key: str) -> None:
        """줄과 자리 어디에 있든 빠지고 기다리는 스레드를 깨운다. 없는 키면 아무 일도 없다.

        Leave the line and the slot, wherever `key` is, and wake the waiters; an unknown key is harmless."""
        with self._cond:
            try:
                self._line.remove(key)
            except ValueError:
                pass
            self._active.pop(key, None)
            self._withdrawn.discard(key)
            self._cond.notify_all()

    def withdraw(self, key: str) -> bool:
        """기다리는 키를 지금 줄에서 뺀다(멈춤 요청, 리뷰 2026-11-10). 그 키의 ``turn()`` 은 깨어나 ``_LeftLine`` 을 낸다 — 자리를 받지
        않는다. 줄에 없으면(이미 자리를 받았거나 모르는 키) False 이고 아무것도 바꾸지 않는다. 자리를 받는 일과 같은 잠금 안이라 둘 중
        하나만 일어난다. 뒤의 실행들은 한 칸씩 앞으로(``notify_all``).

        Take a waiting key out of the line now; its `turn()` wakes up and raises `_LeftLine` instead of taking a slot. False
        (and nothing changed) when `key` is not waiting — it already holds a slot, or it is unknown. Decided under the same
        lock as taking a slot, so exactly one of the two happens."""
        with self._cond:
            if key in self._active or key not in self._line:
                return False
            self._line.remove(key)
            self._withdrawn.add(key)
            self._cond.notify_all()
            return True

    def occupy(self, key: str, prefer: int | None = None) -> int:
        """재시작 복구 전용: 한도와 상관없이 **지금** 자리를 잡고 그 번호를 돌려준다(줄에 있었으면 줄에서 빠진다). ``prefer`` (state.json 에
        적힌 번호)가 있으면 **다른 키가 이미 쥐었어도 그 번호** — 그 Job 의 파드는 env 로 받은 그 번호(그 번호의 Maven 로컬 저장소)로 돌고
        있으므로 메모리에서 다른 번호를 주면 나중에 그 번호가 새 실행에 넘어간다(리뷰 2026-11-10). 겹치면 두 실행을 이름 대어 WARNING.
        ``prefer`` 가 없거나 읽을 수 없으면 비어 있는 가장 작은 번호. 이미 자리를 쥔 키면 그 번호.

        Restart recovery only: take a slot now whatever the limit (a Job that exists already runs and must be counted).
        `prefer` (the recorded number) is honoured even when another key holds it — the pod really uses it — with a
        WARNING naming both runs; a number is free for a new run only when no key holds it. Without a readable `prefer`:
        the lowest free number. A key already holding a slot keeps it."""
        try:
            want: int | None = int(prefer) if prefer is not None else None
        except (TypeError, ValueError):
            want = None
        if want is not None and want < 1:
            want = None
        with self._cond:
            if key in self._active:
                return self._active[key]
            try:
                self._line.remove(key)
            except ValueError:
                pass
            self._withdrawn.discard(key)
            if want is None:
                slot = self._lowest_free()
            else:
                slot = want
                others = sorted(k for k, s in self._active.items() if s == slot)
                if others:
                    log.warning("recovery: run %s and run %s both recorded slot %d — both keep it (their pods use it); "
                                "a new run gets slot %d only after both ended", key, ", ".join(others), slot, slot)
            self._active[key] = slot
            self._cond.notify_all()            # the places of the waiting ones changed
            return slot

    @contextmanager
    def turn(self, key: str) -> Iterator[int]:
        """``key`` 차례가 올 때까지(줄 맨 앞 + 빈 자리) 기다린 뒤 자리 번호를 내주고 블록을 돌리고, 끝나면 자리를 비운다. ``join`` 없이
        불렀으면 지금 줄 끝에 선다(늦게 온 만큼 뒤에서 기다린다). ``occupy`` 로 이미 자리를 쥔 키면 기다리지 않고 그 번호를 내준다.
        기다리는 중에 ``withdraw`` 로 줄에서 빠졌으면(멈춤) 자리를 받지 않고 ``_LeftLine`` 을 낸다.

        Wait until `key` is first in line and a slot is free, yield the slot number, free it when the block leaves. A key
        that already holds a slot (`occupy`) gets it at once; a key that never joined joins now (at the end). A key taken
        out of the line by `withdraw` (a stop while waiting) raises `_LeftLine` without a slot."""
        with self._cond:
            slot = self._active.get(key)
            if slot is None:
                if key in self._withdrawn:         # withdrawn before this thread reached the line
                    self._withdrawn.discard(key)
                    raise _LeftLine(key)
                if key not in self._line:
                    self._line.append(key)
                while True:
                    held = self._active.get(key)
                    if held is not None:           # occupy() gave it a slot meanwhile (recovery)
                        slot = held
                        break
                    if key not in self._line:      # withdraw(): left the line while waiting
                        self._withdrawn.discard(key)
                        raise _LeftLine(key)
                    if self._line[0] == key and len(self._active) < self.slots:
                        self._line.popleft()
                        slot = self._lowest_free()
                        self._active[key] = slot
                        break
                    self._cond.wait()
                # 자리가 둘 이상이면 다음 맨 앞도 곧바로 들어갈 수 있다 — 깨워서 스스로 보게 한다
                self._cond.notify_all()
        try:
            yield slot
        finally:
            self.leave(key)

    def position(self, key: str) -> int | None:
        """기다리는 줄에서의 자리(1 = 다음 차례). 기다리고 있지 않으면 None. / 1-based place among the waiting; None when not waiting."""
        with self._cond:
            try:
                return self._line.index(key) + 1
            except ValueError:
                return None

    def slot_of(self, key: str) -> int | None:
        """이 키가 쥔 자리 번호(없으면 None). / The slot number `key` holds, or None."""
        with self._cond:
            return self._active.get(key)

    def counts(self) -> tuple[int, int]:
        """(자리를 쥔 키의 수, 기다리는 수) — 복구로 두 키가 한 번호를 쥐면 둘로 센다. / (keys holding a slot, waiting)."""
        with self._cond:
            return len(self._active), len(self._line)


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


def _resolve_max_parallel(value: int | str | None, mode: str) -> int:
    """동시에 도는 Job 의 수(v2.1z-ae): 인자가 이기고, 없으면 ``SBMIG_JOB_MAX_PARALLEL``. 1 이상의 정수만 — 아니면 그 값을 이름 대어
    경고하고 1. ``k8s-job`` / ``local-job`` 에서만 듣는다: in-process 는 늘 1(더 달라고 했으면 경고).

    The number of Jobs at a time: the argument wins over `SBMIG_JOB_MAX_PARALLEL`; an integer ≥ 1, anything else → 1 with a
    WARNING naming the value. Only the Job modes count it — in-process always runs one (a WARNING when more was asked)."""
    raw: Any = value if value is not None else os.environ.get(contract.MAX_PARALLEL_ENV, "")
    asked = 1
    if not (isinstance(raw, str) and not raw.strip()):
        try:
            n = int(raw.strip()) if isinstance(raw, str) else (int(raw) if not isinstance(raw, bool) and int(raw) == raw else 0)
        except (TypeError, ValueError):
            n = 0
        if n >= 1:
            asked = n
        else:
            log.warning("%s=%r is not an integer ≥ 1 — running 1 migration job at a time", contract.MAX_PARALLEL_ENV, raw)
    if mode not in ("k8s-job", "local-job"):
        if asked > 1:
            # 같은 프로세스 안의 이관 둘은 LLM 호출 수와 실행별 로그(스레드 필터)를 함께 쓴다 — 한도는 Job 모드에서만 듣는다
            log.warning("%s=%d is ignored in %s mode — one migration at a time (the LLM call counter and the per-run log "
                        "filter are process-wide); set SBMIG_WEB_MODE=k8s-job or local-job to run several",
                        contract.MAX_PARALLEL_ENV, asked, mode)
        return 1
    return asked


class RunManager:
    """이관을 작업 스레드로 시작하고, 상태·이벤트 질문에 메모리 + 실행 폴더로 답한다.
    ``_lock`` 은 ``runs`` dict 를 지키고, ``_turns`` 는 이관을 올린 순서대로 ``max_parallel`` 개까지 들여보낸다(Job 모드; in-process 는 하나).

    Starts migrations in worker threads and answers status / event questions from memory + the run directory;
    `_turns` lets them in in upload order, up to `max_parallel` at a time (Job modes; one in-process)."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, *,
                 max_finished_runs: int = DEFAULT_MAX_FINISHED_RUNS, default_configs: list[str] | None = None,
                 mode: str | None = None, job_client: Any = None, job_template: str | Path | None = None,
                 max_parallel: int | None = None):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.max_finished_runs = max(0, int(max_finished_runs))
        # 2026-11-05: 실행 모드 — in-process(이 프로세스의 스레드) | k8s-job(업로드마다 Job 하나, 공유 PVC 로 전달·결과)
        self.mode = (mode or os.environ.get(MODE_ENV, "") or "in-process").strip()
        if self.mode not in MODES:
            raise ValueError(f"{MODE_ENV} must be one of {', '.join(MODES)}, not {self.mode!r}")
        # 개발 PC(2026-11-05, 사용자: "개발 모드일 때는 파드 생성 없이"): k8s-job 으로 설정돼 있어도 클러스터 밖이면 local-job —
        # 같은 Job 모드 흐름, 파드 자리는 로컬 서브프로세스. 운영 파드에서는 KUBERNETES_SERVICE_HOST 가 항상 있어 내려가지 않는다
        self.effective_mode = self.mode
        # 2026-11-09: 서비스 계정 토큰 또는 KUBECONFIG 파일(인프라팀이 Secret 으로 주는 /etc/kube/config) — 둘 중 하나면 실제 Job 을 만든다
        if self.mode == "k8s-job" and job_client is None and not (in_cluster_available() or kubeconfig_path() is not None):
            if cluster_host_present():
                # inside a cluster but without a usable service account: do NOT silently run migrations in the web pod
                raise ValueError(f"{MODE_ENV}=k8s-job inside a cluster needs the web pod's service account token "
                                 "(serviceAccountName sbmig-web, deploy/k8s/45-web-rbac.yaml; automountServiceAccountToken on) "
                                 "or a kubeconfig file named by KUBECONFIG (the infra team's /etc/kube/config Secret)")
            self.effective_mode = "local-job"
            log.warning("%s=k8s-job but this process is not inside a Kubernetes cluster — running the Job mode with LOCAL "
                        "subprocesses instead (development); status.mode says local-job", MODE_ENV)
        self._job_client = job_client                     # None → K8sClient.connect() / LocalJobClient on the first Job
        self.job_template = Path(job_template or os.environ.get(TEMPLATE_ENV, "") or (self.repo_root / DEFAULT_TEMPLATE))
        self.job_poll_s = max(0.05, float(os.environ.get(POLL_ENV, "2") or 2))
        # the engine stops between nodes and the fix loop between build rounds — the grace must outlast one Maven build
        # (build.timeout_minutes 30) plus an OpenRewrite run before the Job is deleted (review 2026-11-05: 600 s lost the report)
        self.job_stop_grace_s = max(0.0, float(os.environ.get(STOP_GRACE_ENV, "2700") or 2700))
        # Kubernetes API 가 이만큼 답하지 않으면 한 번 크게 알린다(로그 error, 저널, 화면). **실행을 실패로 끝내지는 않는다**(리뷰
        # 2026-11-10): 파드가 살아 있는지 모르는 채로 차례를 넘기면 다음 이관과 겹친다 — API 가 답할 때까지 이 실행이 차례를 지킨다
        self.job_api_grace_s = max(0.0, float(os.environ.get("SBMIG_JOB_API_GRACE_S", "300") or 300))
        # 결과 파일(job-status.json)을 받은 뒤 파드가 스스로 끝나기를 기다리는 시간. 넘기면 그 Job 을 지우고 사라질 때까지 기다린다 —
        # Worker 는 그 파일을 맨 마지막에 쓰므로 지워도 잃는 것이 없고, 끝나지 않는 파드가 줄을 막지 않는다
        self.job_exit_grace_s = max(0.0, float(os.environ.get(EXIT_GRACE_ENV, "120") or 120))
        # v2.1z-ac: 파드가 끝났는데 job-status.json 이 아직 안 보이면 잠시 기다린다 — NFS 의 속성 캐시(기본 최대 60초)가 Worker 가 마지막에 쓴
        # 파일을 Main 에 늦게 보여 줄 수 있다. 끝난 결과를 "검증 안 됨" 으로 잘못 적지 않으려면 이 유예가 캐시보다 길어야 한다
        self.job_status_grace_s = max(0.0, float(os.environ.get(STATUS_GRACE_ENV, "90") or 90))
        # 리뷰 2026-11-10 — 환경 변수 고정의 정책(SBMIG_JOB_ENV_PIN, 시작 때 한 번 읽는다). k8s-job 의 기본은 refuse(Worker 의 환경이
        # 다르면 돌지 않는다), local-job 은 off(로컬 Worker 는 이 프로세스의 환경을 물려받는다 — 접수와 시작 사이의 .env 수정은 잘못된
        # 경보만 낸다). recover() 가 Job 을 만들 수 있으므로 그보다 먼저 정한다
        pin_default = contract.ENV_PIN_REFUSE if self.effective_mode == "k8s-job" else contract.ENV_PIN_OFF
        pin_raw = (os.environ.get(contract.ENV_PIN_POLICY_ENV) or "").strip()
        self.env_pin_policy = contract.env_pin_policy(pin_raw, pin_default)
        if pin_raw and pin_raw.lower() not in contract.ENV_PIN_POLICIES:
            log.warning("%s=%r is not one of %s — using the default %s", contract.ENV_PIN_POLICY_ENV, pin_raw,
                        " | ".join(contract.ENV_PIN_POLICIES), self.env_pin_policy)
        # 서버가 정한 기본 설정 파일 이름들(sbmig serve --config / SBMIG_WEB_CONFIGS): 화면은 이것으로 돈다
        self._default_configs = [c.strip() for c in (default_configs or []) if c and c.strip()]
        self._missing_warned: set[str] = set()
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        # v2.1z-ae: 동시에 도는 Job 의 수 — 인자 → SBMIG_JOB_MAX_PARALLEL → 1. 실행 모드가 정해진 뒤(k8s-job 이 local-job 으로 내려간
        # 뒤)에 정한다: in-process 는 늘 1(LLM 호출 수·실행별 로그 필터가 프로세스 전체에 하나뿐이다). recover() 가 자리를 잡으므로 그보다 먼저
        self.max_parallel = _resolve_max_parallel(max_parallel, self.effective_mode)
        self._turns = _FifoTurns(self.max_parallel)   # upload order; up to max_parallel Jobs at a time (one in-process)
        if self.effective_mode in ("k8s-job", "local-job"):
            log.info("web: up to %d migration job(s) at a time (%s), the rest wait in upload order", self.max_parallel,
                     contract.MAX_PARALLEL_ENV)
        else:
            log.info("web: one migration at a time (%s mode)", self.effective_mode)
        self.closing = threading.Event()       # server shutdown: open SSE streams end, running runs are asked to stop
        self._job_template_doc: dict | None = None   # k8s-job: the template validated at startup (Jobs are rendered from it)
        self.recovered: list[str] = []         # run ids picked up from control/state.json at startup (recover())
        # v2.1z-ac: 운영(k8s-job)은 잘못된 틀·권한·접속을 첫 업로드가 아니라 시작 때 거부한다(앱이 뜨지 않는다)
        if self.effective_mode == "k8s-job":
            self._startup_k8s(constructed=job_client is None)
        if self.effective_mode in ("k8s-job", "local-job"):
            self.recover()

    # ---- k8s-job startup (v2.1z-ac)
    def _startup_k8s(self, constructed: bool) -> None:
        """k8s-job 시작 검증: 클라이언트를 지금 만들고(토큰·kubeconfig 가 없으면 여기서 실패), Worker 틀을 검사하고, 권한을 스스로
        점검한다. 어느 것이든 실패하면 ValueError(앱이 뜨지 않는다). 테스트가 클라이언트를 넣었으면 만들기를 건너뛰고, 틀은 파일이
        있을 때만 마운트 규칙 없이 검사한다.

        k8s-job startup: build the client now, validate the Worker template, self-check RBAC; any failure is a ValueError.
        An injected client skips the construction; its template is validated (mount rules aside) only when the file exists."""
        if constructed:
            try:
                self._job_client = K8sClient.connect()
            except K8sError as e:
                raise ValueError(f"{MODE_ENV}=k8s-job cannot reach the Kubernetes API: {e}") from e
        if constructed or Path(self.job_template).is_file():
            try:
                self._job_template_doc = validate_worker_template(self.job_template, self.out_dir if constructed else None)
            except K8sError as e:
                raise ValueError(str(e)) from e
        self._check_permissions(self._job_client)

    def _check_permissions(self, client: Any) -> None:
        """시작 때 권한 자가 점검: Job 만들기·보기·목록·watch·지우기, 파드 보기·목록·watch(deploy/k8s/45-web-rbac.yaml 과 같은 목록).
        ``can_i`` 가 없는 클라이언트(가짜, 로컬)는 건너뛴다. 하나라도 거부되면 ValueError 로 무엇이 막혔는지 이름을 댄다.

        Startup RBAC self-check through `client.can_i` (skipped when the client has none); any denial → ValueError naming it."""
        can_i = getattr(client, "can_i", None)
        if not callable(can_i):
            return
        ns = getattr(client, "namespace", None) or "?"
        denied: list[str] = []
        for verb, resource, group in REQUIRED_PERMISSIONS:
            try:
                if not can_i(verb, resource, group):
                    denied.append(f"{verb} {resource}")
            except K8sError as e:
                raise ValueError(f"cannot check the web pod's permissions in {ns} ({verb} {resource}): {e}") from e
        if denied:
            raise ValueError(f"the web pod may not {', '.join(denied)} in {ns} — RBAC: deploy/k8s/45-web-rbac.yaml or the kubeconfig's user")

    # ---- restart recovery (v2.1z-ac)
    def recover(self) -> list[str]:
        """재시작 복구: ``<out>/*/control/state.json`` 중 끝나지 않은 실행을 찾는다(``_`` 폴더 제외). **k8s-job**: 접수 기록
        (request.json)을 읽어 ``Run`` 을 다시 세우고 ``accepted_at`` 순서로 FIFO 줄에 넣는다 — ``queued`` 는 평소대로 Job 을 만들고,
        ``creating`` 은 같은 이름의 Job 이 있으면 이어받고 없으면 만들고, ``created`` / ``running`` 은 이어받아 지켜보기만 한다. **local-job**:
        서브프로세스는 서버와 함께 죽었으므로 ``lost`` 로 적는다. 깨진 파일은 로그만 남기고 건너뛴다 — 절대 예외를 내지 않는다.

        v2.1z-ae: Job 이 있는(있을 수 있는) ``creating`` / ``created`` / ``running`` 을 **먼저** 모두 되살려 적힌 자리(``slot``, 없으면 1)를
        한도와 상관없이 지금 잡고(``_turns.occupy``), 그다음에야 ``queued`` 를 줄에 세운다 — 기다리던 실행의 스레드가 도는 Job 의 번호를
        먼저 가져가지 못하게.

        Restart recovery from the control states on disk (k8s-job: re-register and watch / create in `accepted_at` order;
        local-job: mark `lost`). Runs whose Job exists or may exist re-occupy their recorded slot first, before any queued
        run joins the line. Never raises; returns `self.recovered`."""
        if self.effective_mode not in ("k8s-job", "local-job"):
            return self.recovered
        try:
            paths = sorted(self.out_dir.glob(f"*/{CONTROL_DIR}/{STATE_FILE}"))
        except OSError as e:
            log.warning("recovery: cannot scan %s: %s", self.out_dir, e)
            return self.recovered
        pending: list[tuple[str, Path, ControlState, RequestRecord]] = []
        for sp in paths:
            rd = sp.parent.parent
            try:
                if rd.name.startswith("_") or rd.name in self.runs:
                    continue
                cs = ControlState.read(rd)
                if cs is None:
                    log.warning("recovery: %s is unreadable — skipped", sp)
                    continue
                if cs.terminal:
                    continue
                if cs.run_id != rd.name:
                    log.warning("recovery: %s names run %s, not its folder %s — skipped", sp, cs.run_id, rd.name)
                    continue
                if self.effective_mode == "local-job":
                    # 로컬 "파드" 는 이 서버의 자식이었다 — 서버가 죽으면 함께 죽어 이어받을 것이 없다
                    cs.transition(RunState.LOST, exit_code=EXIT_ABORTED, error=LOCAL_LOST).write(rd)
                    self.recovered.append(cs.run_id)
                    log.warning("recovery: run %s was %s — marked lost (%s)", cs.run_id, "in progress", LOCAL_LOST)
                    continue
                rec = RequestRecord.read(rd)
                if rec is None:
                    log.warning("recovery: run %s (state %s) has no readable control/request.json — skipped", cs.run_id, cs.state)
                    continue
                if cs.state not in (RunState.QUEUED.value, RunState.CREATING.value, RunState.CREATED.value, RunState.RUNNING.value):
                    log.warning("recovery: run %s has an unknown state %r — skipped", cs.run_id, cs.state)
                    continue
                pending.append((rec.accepted_at or cs.accepted_at or "", rd, cs, rec))
            except Exception as e:  # noqa: BLE001 — recovery must never keep the web app from starting
                log.warning("recovery: %s skipped (%s: %s)", sp, type(e).__name__, e)
        ordered = sorted(pending, key=lambda t: (t[0], t[1].name))
        # 자리를 쥔 실행(Job 이 이미 돈다)을 먼저, 기다리던 실행은 그 뒤에 — 각 무리 안에서는 accepted_at 순서
        holding = [t for t in ordered if t[2].state != RunState.QUEUED.value]
        waiting = [t for t in ordered if t[2].state == RunState.QUEUED.value]
        for _at, rd, cs, rec in holding + waiting:
            try:
                self._resume(rd, cs, rec)
            except Exception as e:  # noqa: BLE001 — recovery must never keep the web app from starting
                log.warning("recovery: run %s not resumed (%s: %s)", cs.run_id, type(e).__name__, e)
        return self.recovered

    def _resume(self, rd: Path, cs: ControlState, rec: RequestRecord) -> None:
        """복구한 실행 하나를 메모리에 다시 세우고 감시 스레드를 띄운다. ``queued`` 는 줄 끝에 서고(accepted_at 순서), Job 이 있는(있을 수
        있는) 실행은 적힌 자리를 지금 잡는다(``occupy`` — 한도와 상관없이; ``slot`` 이 없는 state.json 은 1번).

        Re-register one run: a queued one joins the line; one whose Job exists or may exist re-occupies its recorded slot
        now, whatever the limit (no `slot` on disk = slot 1)."""
        mode = {RunState.QUEUED.value: None, RunState.CREATING.value: "probe"}.get(cs.state, "adopt")
        run = Run(rd.name, rd, rd / SOURCE_ZIP, list(rec.configs or []), rec.stop_after, rec.llm_provider, job=True,
                  request=rec, control=cs, recover=mode)
        if cs.state != RunState.QUEUED.value:
            run.job_name = cs.job or job_name(run.run_id)
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-job-{run.run_id}", daemon=True)
        with self._lock:
            self.runs[run.run_id] = run
            if mode is None:
                self._turns.join(run.run_id)
            else:
                # 클러스터의 Job 은 이미 돈다: 자리를 세어야 새 업로드가 같은 번호(같은 Maven 로컬 저장소)를 받지 않는다. probe 가 결국
                # Job 을 스스로 만들면 이 번호를 creating 전이에 적는다
                run.slot = self._turns.occupy(run.run_id, prefer=cs.slot or 1)
        try:
            run.thread.start()
        except RuntimeError:
            self._turns.leave(run.run_id)
            with self._lock:
                self.runs.pop(run.run_id, None)
            raise
        self.recovered.append(run.run_id)
        log.info("recovery: run %s resumed from state %s (job %s, attempt %d%s) — %s", run.run_id, cs.state, run.job_name or "-",
                 run.attempt, f", slot {run.slot}" if run.slot is not None else "",
                 {None: "will create its job", "probe": "adopts the job if it exists, else creates it",
                  "adopt": "adopts the job and watches it"}[mode])

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        """실행 폴더 ``<out>/<run id>`` (호출하는 라우트가 ``RUN_ID_RE`` 로 먼저 검사한다)."""
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        """페이지에서 고를 수 있는 ``config/*.yml`` 이름들. ``default.yml`` 은 늘 먼저 읽히므로 뺀다."""
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # 아무것도 정해 주지 않았을 때 쓰는 이름(있는 첫 것): 회사 PC 의 관례 artifacts.yml, 개발 PC 의 로컬 e2e 설정
    FALLBACK_CONFIGS = ("artifacts.yml", "artifacts.local-e2e.yml")

    def default_configs(self) -> list[str]:
        """이번 실행에 쓸 기본 설정 파일 이름들(``config/`` 안, default.yml 뒤에 읽힌다). 화면은 담당자에게 고르게
        하지 않고 이것으로 돈다(2026-11-05: 설정은 하나로 고정, 고급 설정에서만 바꾼다). ``sbmig serve --config`` /
        ``SBMIG_WEB_CONFIGS`` 로 준 이름 중 지금 있는 파일만 — 없는 이름은 한 번 경고하고 뺀다(나중에 생기면
        다시 들어온다). 아무것도 안 줬으면 :attr:`FALLBACK_CONFIGS` 중 있는 첫 것, 그것도 없으면 빈 목록
        (default.yml 만으로 돈다).

        The server's default config names for a run (`sbmig serve --config` / `$SBMIG_WEB_CONFIGS`, existing files
        only; a missing name is warned about once). Without any: the first existing of `FALLBACK_CONFIGS`, else []."""
        d = self.repo_root / "config"
        if self._default_configs:
            out = []
            for c in self._default_configs:
                if RUN_ID_RE.match(c) and (d / c).is_file():
                    out.append(c)
                elif c not in self._missing_warned:
                    self._missing_warned.add(c)
                    log.warning("web: default config %s is not under %s — runs start without it until it exists", c, d)
            return out
        return next(([c] for c in self.FALLBACK_CONFIGS if (d / c).is_file()), [])

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
        overrides: dict[str, Any] = {}
        if llm_provider:
            overrides["llm"] = {"provider": llm_provider}
        cfg = load_config(*[self.repo_root / "config" / c for c in configs], overrides=overrides or None,
                          repo_root=self.repo_root)        # pydantic ValidationError is a ValueError

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        run_id = f"web-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if self.effective_mode in ("k8s-job", "local-job"):
            return self._start_job(incoming, run_id, safe, list(configs), stop_after, llm_provider)
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
        run.orch = Orchestrator(cfg, ws)
        # 실행 폴더의 첫 기록: 어떤 설정으로 돌았는지 journal 만 보고도 알 수 있게
        try:
            ws.journal({"event": "web_run", "configs": list(configs), "stop_after": stop_after, "llm_provider": llm_provider})
        except OSError as e:                   # the journal line is a convenience; never refuse the run on it
            log.debug("run %s: web_run journal line not written: %s", run_id, e)
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
        log.info("run %s started: %s configs=%s stop_after=%s llm_provider=%s", run_id,
                 zip_path.name, ",".join(configs) or "-", stop_after or "-", llm_provider or "-")
        return run_id

    def _start_job(self, incoming: Path, run_id: str, safe_name: str, configs: list[str], stop_after: str | None,
                   llm_provider: str | None) -> str:
        """k8s Job 모드의 시작: 실행 폴더를 만들고 zip 을 ``source.zip`` 으로 옮기고(둘 다 공유 볼륨), 첫 저널 줄을 쓰고,
        Job 을 만들어 지켜보는 작업 스레드를 띄운다. 이 프로세스는 이관을 돌리지 않는다.

        Job mode: create the run dir, move the zip there as `source.zip` (both on the shared volume), write the first
        journal line, start the worker that creates and watches the Job. Nothing migrates in this process."""
        run_dir = self.out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        zip_path = run_dir / SOURCE_ZIP
        Path(incoming).replace(zip_path)
        # v2.1z-ac: 입력을 접수 때 고정한다(request.json — Worker 가 digest 를 대조한다)와 제어 상태 queued — 작업 스레드보다 먼저,
        # 그래야 이 프로세스가 곧 죽어도 다음 Main 이 같은 요청을 이어받는다
        try:
            request = RequestRecord(run_id=run_id, accepted_at=utc_now(), upload=safe_name, configs=list(configs),
                                    zip_sha256=sha256_file(zip_path), config_digests=config_digests(self.repo_root, configs),
                                    sbmig_version=sbmig_version(), mode=self.effective_mode, stop_after=stop_after,
                                    llm_provider=llm_provider, attempt=1)
            request.write(run_dir)
            control = ControlState(run_id=run_id, state=RunState.QUEUED.value, attempt=request.attempt,
                                   accepted_at=request.accepted_at)
            control.write(run_dir)
        except OSError:                        # no run was started: leave no half-accepted run dir behind
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        run = Run(run_id, run_dir, zip_path, configs, stop_after, llm_provider, job=True, request=request, control=control)
        _append_journal(run_dir, {"event": "web_run", "run_id": run_id, "mode": self.effective_mode, "upload": safe_name,
                                  "configs": configs, "stop_after": stop_after, "llm_provider": llm_provider})
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-job-{run_id}", daemon=True)
        with self._lock:
            self.runs[run_id] = run
            self._turns.join(run_id)
        try:
            run.thread.start()
        except RuntimeError:
            self._turns.leave(run_id)
            with self._lock:
                self.runs.pop(run_id, None)
            raise
        log.info("run %s queued as a %s: %s configs=%s stop_after=%s", run_id,
                 "k8s Job" if self.effective_mode == "k8s-job" else "local job (development)", safe_name,
                 ",".join(configs) or "-", stop_after or "-")
        return run_id

    def _worker(self, run: Run) -> None:
        """작업 스레드 본체. ``_turns`` 줄 맨 앞이고 빈 자리가 생길 때까지 ``queued``, 차례가 오면 ``active`` (Job 실행은 자리 번호를
        ``run.slot`` 에) → 이 스레드 전용 ``sbmig.log`` 를 붙이고 이관을 돌린 뒤 ``SBMIG-RESULT`` 줄을 남긴다. 무슨 일이
        있어도 finally 에서 로그를 떼고 ``finished`` 이벤트·web-status.json 을 쓰고 ``done`` 을 세워
        기다리는 SSE 를 깨운다.

        The worker thread: `queued` until first in line with a free slot, then `active` (a Job run keeps its slot number in
        `run.slot`); whatever happens, the finally block writes the end of the run and wakes the streams."""
        log_path: Path | None = None
        settled = False                        # a Job run's terminal control state was written (or tried) inside its turn
        try:
            # import 도 try 안에서: 여기서 죽어도 finally 가 줄에서 빼야 뒤의 실행이 영원히 기다리지 않는다
            from sbmig.cli import attach_file_log, run_exit_code

            # Job 모드는 max_parallel 개까지(파드마다 따로, KB 쓰기는 파일 잠금, 자리마다 Maven 로컬 저장소), in-process 는 하나. 차례는
            # 올린 순서(FIFO 줄, start_file 에서 섬) — 예전의 threading.Lock 은 기다리는 스레드에게 넘겨주는 순서를 보장하지 않았다(A10)
            try:
                with self._turns.turn(run.run_id) as slot:  # upload order; up to max_parallel Jobs at a time (docstring)
                    if run.job:
                        run.slot = slot
                    run.active.set()
                    with run.cond:                         # the run's SSE streams see queued → running at once, not on the next tick
                        run.cond.notify_all()
                    if run.job:                            # k8s Job mode: the pod writes this run's sbmig.log — not this process
                        try:
                            run.exit_code = self._run_job(run)
                        except Exception as e:  # noqa: BLE001 — _run_job catches everything itself; a bug here must still settle
                            self._record_crash(run, e)
                        finally:
                            # 리뷰 2026-11-10: 끝 상태(control/state.json)를 **자리를 쥔 채로** 적는다. 자리를 먼저 비우면 그 사이(웹 파드가
                            # 죽거나 쓰기가 실패하면) 다음 실행이 같은 번호를 적고, 재시작 뒤 끝나지 않은 두 상태가 한 번호를 갖는다.
                            # 쓰기가 실패해도(로그만) 블록을 떠나며 자리는 비운다 — 새지 않는다
                            settled = True
                            self._close_job_run(run)
                        return
                    # thread_id: 이 스레드(와 그 컨텍스트를 물려받은 LangGraph 실행기 스레드)의 로그만 이 파일로 —
                    # 같은 프로세스의 다른 실행·서버 로그가 섞이지 않게
                    log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
                    # 이 실행 sbmig.log 의 첫 줄: 어떤 설정으로 돌았는지
                    log.info("run %s: configs=%s stop_after=%s llm_provider=%s", run.run_id,
                             ",".join(run.configs) or "-", run.stop_after or "-", run.llm_provider or "-")
                    report = self._run_thread_body(run)
                    run.report = report
                    run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
                    if report is not None:          # the line operators retype (round 10), also in this run's sbmig.log
                        from sbmig.report.result_line import report_result_line
                        log.info("%s", report_result_line(report, run.exit_code, run_id=run.run_id))
            except _LeftLine:
                # 리뷰 2026-11-10: 기다리던 Job 실행이 멈춤으로 줄에서 빠졌다 — 차례가 와서 cancel 을 본 것과 똑같이 끝낸다(Job 없음)
                log.info("run %s: stopped while waiting — left the line", run.run_id)
                run.exit_code = self._stopped_before_create(run)
        except Exception as e:  # noqa: BLE001 — the orchestrator never raises; a crash here is a bug (logged in _record_crash)
            self._record_crash(run, e)
        finally:
            self._turns.leave(run.run_id)      # already left after a turn; this covers a failure before it
            run.active.set()
            log.info("run %s finished: exit code %s", run.run_id, run.exit_code)
            if log_path is not None:
                _detach_file_log(log_path)
            if run.job:
                if not settled:                # no slot was taken (left the line, or a failure before the turn)
                    self._close_job_run(run)
                success = run.success
            else:
                # 마지막 정제(이 프로세스 안의 실행): 출력 정제가 걸려 있을 때. done 을 세우기 전에 — 그 전에는 file_withheld 가 내주지 않는다
                if output_scrubbing():
                    self._final_redaction(run)
                success = getattr(run.report, "success", None)
                run.success = success
            self._push(run, "finished", {"exit_code": run.exit_code, "success": success})
            if not run.keep_state:             # a Job left running at shutdown: its state.json speaks for it, not web-status.json
                _write_web_status(run)
            run.done.set()
            with run.cond:
                run.cond.notify_all()
            with self._lock:
                self._prune_locked()

    def _record_crash(self, run: Run, e: BaseException) -> None:
        # 작업 스레드 안의 예상 못 한 예외(엔진·이 모듈의 버그): 실행을 실패로 적고 화면에 알린다
        log.exception("run %s failed", run.run_id)
        run.error = scrub(f"{type(e).__name__}: {e}")
        run.exit_code = EXIT_ABORTED
        if run.job:
            run.final_state = run.final_state or RunState.FAILED.value
        self._push(run, "error", {"message": run.error})

    def _final_redaction(self, run: Run) -> None:
        # 마지막 정제: 쓰는 순간의 정제가 닿지 않는 파일(Maven 출력 logs/, 보고서)을 훑는다. 실패해도 실행을 막지 않는다(로그만)
        try:
            from sbmig.web.redact import redact_run_dir
            redact_run_dir(run.run_dir)
        except Exception:  # noqa: BLE001
            log.exception("run %s: the final output redaction failed", run.run_id)

    def _close_job_run(self, run: Run) -> None:
        """Job 실행의 마무리 — 마지막 정제(필요할 때)와 끝 상태 기록(``_settle_job_run``) 후 ``run.success``. 자리를 쥔 실행은 **자리를
        비우기 전에** 부른다(리뷰 2026-11-10). 무슨 일이 있어도 예외를 내지 않는다(자리는 부른 쪽의 블록이 비운다).
        Job 실행의 마지막 정제는 Worker 가 끝에 하지만(job-status.json 을 쓰기 직전), 그 파일 없이 끝난 Job(파드가 죽었다)은 그 단계를 못
        지났을 수 있어 Main 이 자기 환경의 비밀 값으로 대신한다 — Job 이 끝난 것을 확인했을 때만(서버 종료로 두고 나가는 Job, 우리 것이
        아닌 Job 의 파일은 건드리지 않는다).

        Close a Job run: the final redaction when Main must do it, then the terminal control state (`_settle_job_run`);
        sets `run.success`. Called before the slot is freed; never raises."""
        try:
            if run.verified is not True and run.job_phase in TERMINAL_PHASES and not run.keep_state:
                self._final_redaction(run)
            run.success = self._settle_job_run(run)
        except Exception:  # the slot must still be freed by the caller's block
            log.exception("run %s: settling the job run failed", run.run_id)

    def _stopped_before_create(self, run: Run) -> int:
        # 대기 중에 온 멈춤: Job 을 만들지 않고 stopped 로 끝낸다(차례가 와서 본 경우와 줄에서 빠진 경우가 같은 길)
        run.error = "stopped before the job was created"
        run.final_state = RunState.STOPPED.value
        return EXIT_ABORTED

    def _job_client_or_fail(self) -> Any:
        """Job 클라이언트(처음 부를 때 만든다). v2.1z-ae: 여러 감시 스레드가 동시에 부를 수 있어 ``_lock`` 안에서 한 번만 만든다 —
        둘이 따로 만들면 로컬 클라이언트의 프로세스 목록이 갈라진다.

        The Job client, built once under `_lock` on first use (several watcher threads may ask at the same time)."""
        with self._lock:
            if self._job_client is None:
                if self.effective_mode == "local-job":
                    from sbmig.web.localjob import LocalJobClient
                    self._job_client = LocalJobClient(self.repo_root)
                else:
                    self._job_client = K8sClient.connect()
            return self._job_client

    def _turn_words(self, run: Run) -> tuple[str, str]:
        # 끝나지 않은 Job 이 무엇을 막고 있는지 알리는 말 (무엇을 쥐고 있나, 뒤에 붙일 말): 자리가 하나면 예전 문장 그대로(다음 이관이
        # 기다린다), 여럿이면 그 자리 하나만 막힌다
        if self.max_parallel <= 1:
            return "its turn", ", the next migration waits"
        return (f"its slot {run.slot}" if run.slot is not None else "its slot"), ""

    def _settle_job_run(self, run: Run) -> bool | None:
        """Job 실행의 마무리(작업 스레드의 finally): 결과를 믿을 수 있나 — 이 실행·시도의 ``job-status.json`` 을 받았거나, 컨테이너
        종료 코드가 있고 ``report.json`` 이 있을 때만 report.json 의 success 를 읽는다. 아니면 success=None 이고 오류에 "result not
        verified". 그리고 제어 상태를 끝 상태로 적는다(서버 종료로 Job 을 두고 나갈 때는 그대로 둔다).

        Settle a job run: success only from a verified result; write the terminal control state (left as is at shutdown)."""
        has_report = (run.run_dir / "report.json").is_file()
        trusted = bool(run.verified) or (run.result_source == "container" and has_report)
        success = _report_success(run.run_dir) if trusted else None
        expected_result = not run.keep_state and run.final_state not in (RunState.STOPPED.value, RunState.FAILED.value)
        if not trusted and expected_result:
            note = "result not verified"
            run.error = (f"{run.error} ({note})" if run.error
                         else f"{note}: no job-status.json for run {run.run_id} attempt {run.attempt} and no report.json")
        if run.control is not None and not run.keep_state and not run.control.terminal:
            final = run.final_state or (RunState.FINISHED.value if trusted else RunState.LOST.value)
            try:
                self._transition(run, final, exit_code=run.exit_code, error=run.error)
            except OSError as e:               # the in-memory status still answers; the next Main sees the last state written
                log.error("run %s: control state %s not written: %s", run.run_id, final, e)
        return success

    def _transition(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """제어 상태를 바꾸고 바로 ``control/state.json`` 에 원자적으로 쓴다(OSError 는 부른 쪽 몫 — creating 은 못 쓰면 Job 을 만들지
        않는다). / Transition the control state and write it atomically."""
        if run.control is None:                # a run built without intake records (embedding callers): nothing to persist
            run.control = ControlState(run_id=run.run_id)
        run.control.transition(state, **changes).write(run.run_dir)

    def _owned(self, run: Run, st: JobState) -> bool:
        """이 Job 이 이 요청의 것인가: ``sbmig-owner`` 와 ``sbmig-run-id`` 라벨이 **둘 다** 이 요청의 값이어야 한다. 라벨이 하나도 없는
        Job 은 우리 것이 아니다(같은 이름으로 손으로 만든 Job, 다른 도구의 Job) — 이어받지도 지우지도 않는다. 라벨을 아예 알려 주지
        못하는 클라이언트(``labels is None``: 로컬 서브프로세스, 테스트용 가짜)만 이름을 믿는다.

        Is this Job this request's: both labels must match. An unlabelled Job is NOT ours (review 2026-11-10); only a client
        that cannot report labels at all (`labels is None`: the local subprocess, test fakes) is trusted by name."""
        labels = getattr(st, "labels", None)
        if labels is None:
            return True
        return labels.get(OWNER_LABEL) == run.owner and labels.get(RUN_LABEL) == run.run_id

    def _accept_status(self, run: Run) -> int | None:
        """``job-status.json`` 을 읽어 **이 실행·이 시도**의 것이면 종료 코드(그리고 verified), 아니면 None. 다른 실행·시도의 파일은 한
        번 경고하고 무시한다(옛 시도의 성공을 지금 성공으로 읽지 않게).

        The Worker's exit code when `job-status.json` matches this run and attempt; a foreign file is ignored (one warning)."""
        status = JobStatus.read(run.run_dir)
        if status is None:
            return None
        if not status.matches(run.run_id, run.attempt):
            if not run.status_warned:
                run.status_warned = True
                log.warning("run %s: job-status.json belongs to another run/attempt (run %s, attempt %s; expected attempt %d) — ignored",
                            run.run_id, status.run_id, status.attempt, run.attempt)
            return None
        run.verified = True
        run.result_source = "status"
        run.status_error = status.error or None
        if status.error and not run.error:
            run.error = status.error
        return int(status.exit_code)

    def _run_job(self, run: Run) -> int:
        """k8s Job 모드의 본체: Job 생성(또는 재시작 복구 때 이어받기) → ``_watch_job``. 만들기 전에 제어 상태를 ``creating`` + Job
        이름으로 적고(응답이 끊겨도 다음 Main 이 같은 이름을 찾는다), 응답이 409 이거나 전송 오류면 같은 이름의 Job 을 찾아 있으면
        이어받는다(두 번 만들지 않는다). 대기 중에 멈춤·서버 종료가 오면 Job 을 만들지 않는다.
        **차례(``_turns``)는 이 함수가 돌아올 때 넘어간다** — 그래서 Job 을 만들려 했거나 이어받은 뒤에는 그 Job 이 끝났거나 없는 것을
        API 로 확인하고서야 돌아온다(리뷰 2026-11-10): 실패로 끝날 때도 우리 Job 을 지우고 사라질 때까지 기다린다. 예외는 서버 종료
        (제어 상태를 그대로 두어 다음 Main 이 이어받는다)와 우리 것이 아닌 Job(건드리지 않는다)뿐이다.

        Create the Job (intent recorded first; an uncertain answer adopts an existing Job of the same name) or adopt it on
        recovery, then watch it. A stop or shutdown that arrived while queued never creates a Job. The turn is handed on
        when this returns, so once a Job was asked for it returns only after the API confirmed that Job ended or is gone."""
        name = run.job_name or job_name(run.run_id)
        client: Any = None
        try:
            if run.recover is None:
                # a stop or a server shutdown that arrived while this run was queued: never create the Job (review 2026-11-05: the
                # shutdown cascaded through the queue and created every queued Job)
                if run.cancel.is_set():
                    return self._stopped_before_create(run)
                if self.closing.is_set():
                    run.error = "web server shut down before the job was created"
                    run.keep_state = True          # still queued on disk: the next Main creates it
                    return EXIT_ABORTED
            client = self._job_client_or_fail()
            if run.recover == "adopt":
                # 이름만으로 이어받지 않는다: 같은 이름의 Job 이 있으면 라벨이 이 요청의 것이어야 한다(아니면 여기서 실패). 없으면 — 끝난 뒤
                # 지워진 Job — 그대로 이어받아 볼륨의 결과 파일이 답하게 한다
                self._job_exists(run, client, name)
                self._adopt(run, name, "restart: the job was created by an earlier web process")
            elif run.recover == "probe" and self._job_exists(run, client, name):
                self._adopt(run, name, "restart: the job was being created when the earlier web process stopped")
            else:
                self._create_job(run, client, name)
            return self._watch_job(run, client, name)
        except _ServerClosing:
            run.error = "web server shut down while waiting for the Kubernetes API — the next web process picks this run up"
            run.keep_state = True
            return EXIT_ABORTED
        except Exception as e:  # noqa: BLE001 — whatever broke here, the turn is not handed on while our Job may be alive
            run.error = f"{type(e).__name__}: {e}"
            run.final_state = RunState.FAILED.value
            if isinstance(e, (K8sError, OSError)):
                log.error("run %s: k8s job %s failed: %s", run.run_id, name, run.error)
            else:
                log.exception("run %s: k8s job %s failed", run.run_id, name)
            self._push(run, "error", {"message": run.error})
            if client is not None and run.job_name:
                # 생성을 시도했거나 이어받은 Job 이 있을 수 있다: 우리 것이면 지우고, 끝난(없는) 것을 확인한 뒤에야 차례를 넘긴다
                self._hold_until_ended(run, client, run.job_name, delete_after=0.0, why="the run failed in the web process")
            return EXIT_ABORTED

    def _job_env(self, run: Run) -> dict[str, str]:
        env = {"SBMIG_SOURCE_ZIP": str(run.zip_path), "SBMIG_OUT": str(self.out_dir), "SBMIG_RUN_ID": run.run_id,
               "SBMIG_CONFIG": " ".join(str(self.repo_root / "config" / c) for c in run.configs)}
        if run.stop_after:
            env["SBMIG_STOP_AFTER"] = run.stop_after
        if run.llm_provider:
            env["SBMIG_LLM_PROVIDER"] = run.llm_provider
        if run.slot is not None:               # v2.1z-ae: the Worker builds with one Maven local repository per slot
            env[contract.SLOT_ENV] = str(run.slot)
        return env

    def _env_pins(self, run: Run) -> dict[str, str] | None:
        """Job 을 만드는 지금, 이 프로세스 환경의 지문(``contract.env_pins`` — 이름 → 값의 sha256 앞부분; 비밀로 보이는 이름은 없다).
        정책이 off 면 None(Worker 가 대조하지 않는다). 계산이 실패해도 Job 만들기를 막지 않는다: 경고만 남기고 None — 읽히지 않는
        설정 파일은 Worker 가 request.json 의 digest 로 따로 잡는다.

        This process's environment pins at Job creation; None when the policy is off or when computing them failed (a
        warning, never a failed create — an unreadable config is caught by the Worker's config digests)."""
        if self.env_pin_policy == contract.ENV_PIN_OFF:
            return None
        try:
            return contract.env_pins(self.repo_root, list(run.configs))
        except Exception as e:  # noqa: BLE001 — computing the pins must never fail the Job creation
            log.warning("run %s: the job environment is not pinned (%s: %s) — the Worker will not compare its environment",
                        run.run_id, type(e).__name__, scrub(str(e)))
            return None

    def _create_job(self, run: Run, client: Any, name: str) -> None:
        """틀을 렌더하고(owner 라벨 포함) ``creating`` 을 적은 뒤 Job 을 만든다. ``creating`` 에는 이 Main 의 환경 지문(``env_pins``)과
        정책(``env_pin_policy``)도 함께 적는다 — Worker 가 자기 환경과 대조한다(Job 을 만드는 곳은 여기뿐이다). 409 / 전송 오류 → API 가
        답할 때까지 같은 이름의 Job 을 찾아, 있고 우리 것이면 이어받고, 없으면(몇 번 더 물어도) 원래 오류를 다시 낸다. 성공하면 ``created``.

        Render, record `creating` together with this Main's `env_pins` / `env_pin_policy` (the Worker compares them with its
        own environment; this is the only place a Job is created), create; an uncertain answer asks (until the API answers)
        for a Job of that name and adopts ours, else re-raises the original error."""
        template: Any = self._job_template_doc if self._job_template_doc is not None else self.job_template
        if self._job_template_doc is None and not Path(self.job_template).is_file():
            if self.effective_mode == "k8s-job":
                # 시작 검증을 건너뛴 경우(테스트가 넣은 클라이언트)에만 여기 온다 — 빈 틀로 Job 을 만들지 않는다
                raise K8sError(f"job template not found: {self.job_template} — mount the ConfigMap sbmig-job-template at /templates "
                               f"(deploy/README.md §3) or point {TEMPLATE_ENV} at the filled template")
            # the local "pod" needs only the env: a minimal manifest stands in for a template the checkout may lack
            template = {"kind": "Job", "metadata": {}, "spec": {"template": {"spec": {"containers": [{"name": "build"}]}}}}
        manifest = render_job(template, run.run_id, self._job_env(run), getattr(client, "namespace", None), owner=run.owner)
        # 의도를 먼저 적는다: 이 줄 뒤에 프로세스가 죽어도 다음 Main 은 creating + 이름을 보고 Job 을 찾는다. 환경 지문도 같은 전이로 —
        # 기준은 접수한 Main 이 아니라 Job 을 **만드는** 이 Main 의 지금 환경이다(대기 중에 재시작했으면 새 Main 의 것)
        pins = self._env_pins(run)
        # 자리 번호도 같은 전이로: 재시작한 Main 이 같은 번호를 다시 잡는다(v2.1z-ae)
        self._transition(run, RunState.CREATING, job=name, env_pins=pins, env_pin_policy=self.env_pin_policy, slot=run.slot)
        if pins is not None:
            log.info("run %s: %d environment value(s) pinned for job %s (%s=%s)", run.run_id, len(pins), name,
                     contract.ENV_PIN_POLICY_ENV, self.env_pin_policy)
        run.job_name = name
        try:
            created = client.create_job(manifest)
        except K8sError as e:
            if not _UNCERTAIN_CREATE_RE.search(str(e)):
                raise
            # 응답이 불확실하다: Job 이 있는지 API 가 답할 때까지 묻는다(모르는 채로 실패 처리하면 만들어진 파드를 두고 차례를 넘긴다).
            # 안 보여도 몇 번 더 묻는다 — 끊긴 요청이 뒤늦게 닿아 Job 이 생길 수 있다
            st = self._state_when_answered(run, client, name)
            for _ in range(CREATE_CONFIRM_POLLS - 1):
                if st.phase != "Missing":
                    break
                with run.cond:
                    run.cond.wait(self.job_poll_s)
                st = self._state_when_answered(run, client, name)
            if st.phase == "Missing":
                raise
            if not self._owned(run, st):
                raise K8sError(f"job {name} already exists but is not owned by this request ({e})") from e
            log.warning("run %s: creating job %s answered %s — the job exists, adopting it", run.run_id, name, e)
            self._adopt(run, name, f"create answered: {e}")
            return
        if created and created != name:        # the API named it differently (it never should: the name is deterministic)
            log.warning("run %s: job created as %s, expected %s", run.run_id, created, name)
            run.job_name = name = str(created)
        _append_journal(run.run_dir, {"event": "job_created", "run_id": run.run_id, "job": name, "attempt": run.attempt,
                                      "namespace": getattr(client, "namespace", None), "slot": run.slot})
        self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name}})

    def _poll_job(self, run: Run, client: Any, name: str) -> JobState | None:
        """Job 상태를 한 번 묻는다. API 가 답하지 않으면 None — **실행을 실패로 끝내지 않는다**: 파드가 살아 있는지 모르는 채로 차례를
        넘기면 그 파드가 다음 이관과 겹친다(리뷰 2026-11-10). ``job_api_grace_s`` 를 넘기면 한 번 크게 알리고(로그 error, 저널
        ``job_api_unavailable``, 화면의 한 줄 ``job.note``) 그 뒤로는 ``API_REMIND_S`` 마다 로그에 다시 남긴다. 다시 답하면 ``job_api_back``.

        One state poll; None while the API does not answer — reported once past `job_api_grace_s`, never a failure. ANY
        exception of the client counts as "API unavailable" (review 2026-11-10: a 200 with a non-JSON body from a proxy
        raised ValueError past this handler, failed the run and freed the slot while the Job ran)."""
        try:
            st = client.job_state(name)
        except Exception as e:  # noqa: BLE001 — 어떤 예외든 'API 가 답하지 않는다': 실행을 실패로 끝내지도, 자리를 비우지도 않는다
            if not isinstance(e, K8sError):
                e = K8sError(f"{type(e).__name__}: {e}")
            now = time.monotonic()
            if run.api_down_since is None:
                run.api_down_since = now
            down = now - run.api_down_since
            if run.api_down_reported is None and down <= self.job_api_grace_s:
                log.warning("run %s: job %s state unavailable (%s) — retrying", run.run_id, name, e)
                return None
            if run.api_down_reported is not None and now - run.api_down_reported < API_REMIND_S:
                return None
            first = run.api_down_reported is None
            run.api_down_reported = now
            noun, waits = self._turn_words(run)
            run.job_note = f"Kubernetes API unavailable for {down:.0f}s ({e}) — this run keeps {noun} until the API answers{waits}"
            log.error("run %s: job %s: %s", run.run_id, name, run.job_note)
            if first:
                _append_journal(run.run_dir, {"event": "job_api_unavailable", "run_id": run.run_id, "job": name,
                                              "seconds": round(down, 1), "error": str(e)})
                self._push(run, "node", {"node": "job", "update": {"state": "api-unavailable", "job": name, "message": str(e)}})
            return None
        if run.api_down_since is not None:
            if run.api_down_reported is not None:
                down = time.monotonic() - run.api_down_since
                log.warning("run %s: job %s: the Kubernetes API answers again after %.0fs", run.run_id, name, down)
                _append_journal(run.run_dir, {"event": "job_api_back", "run_id": run.run_id, "job": name, "seconds": round(down, 1)})
                self._push(run, "node", {"node": "job", "update": {"state": "api-back", "job": name}})
            run.api_down_since, run.api_down_reported, run.job_note = None, None, None
        return st

    def _state_when_answered(self, run: Run, client: Any, name: str) -> JobState:
        """API 가 답할 때까지 Job 상태를 묻는다(기한 없음 — Job 이 있는지 모르는 채로는 차례를 넘기지 않는다). 기다리는 중에 서버가
        내려가면 ``_ServerClosing``.

        Poll until the API answers (no deadline); raises `_ServerClosing` when the server shuts down meanwhile."""
        while True:
            st = self._poll_job(run, client, name)
            if st is not None:
                return st
            if self.closing.is_set():
                raise _ServerClosing(name)
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _job_exists(self, run: Run, client: Any, name: str) -> bool:
        """재시작 복구: 같은 이름의 Job 이 있나(API 가 답할 때까지 묻는다). 있는데 라벨이 이 요청의 것이 아니면 — 라벨이 아예 없는 Job 도 —
        이어받지 않고 오류를 낸다.

        Recovery: does a Job of that name exist (asked until the API answers)? One that is not this request's raises."""
        st = self._state_when_answered(run, client, name)
        if st.phase == "Missing":
            return False
        if not self._owned(run, st):
            raise K8sError(f"job {name} exists but is not owned by this request — not adopted")
        return True

    def _transition_soft(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """Job 이 이미 있는 뒤의 상태 기록: 파일을 못 써도 예외를 내지 않는다(로그만) — 기록 실패로 감시를 그만두면 도는 파드를 두고 차례를
        넘기게 된다. 메모리의 상태는 바뀌어 있고, 디스크에는 직전 상태가 남아 다음 Main 이 그 상태로 이어받는다.

        A control-state write once the Job exists: an `OSError` is logged, never raised (the Job must be watched on)."""
        try:
            self._transition(run, state, **changes)
        except OSError as e:
            log.error("run %s: control state %s not written (%s) — the job is watched on", run.run_id,
                      state.value if isinstance(state, RunState) else state, e)

    def _adopt(self, run: Run, name: str, reason: str) -> None:
        """이미 있는 Job 을 이 실행의 것으로 이어받는다: 저널 ``job_adopted``, 제어 상태 queued / creating → created, '파드' 단계 이벤트.

        Adopt an existing Job: journal `job_adopted`, control state → `created` (a `running` state stays), a `job` node event."""
        run.job_name = name
        _append_journal(run.run_dir, {"event": "job_adopted", "run_id": run.run_id, "job": name, "attempt": run.attempt,
                                      "reason": reason})
        log.info("run %s: adopted job %s (%s)", run.run_id, name, reason)
        if run.control is None or run.control.state in (RunState.QUEUED.value, RunState.CREATING.value):
            self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name, "adopted": True}})

    def _hold_until_ended(self, run: Run, client: Any, name: str, *, delete_after: float | None, why: str) -> bool:
        """**자리를 넘기기 전의 문**(리뷰 2026-11-10; v2.1z-ae 부터 자리마다): Job 과 그 파드가 끝났거나 사라진 것을 API 로 확인할 때까지
        이 실행이 자기 자리를 지킨다 — 자리 수의 한도는 파드가 실제로 끝나야 지켜진다(그 자리의 Maven 로컬 저장소·공유 볼륨을 다음 이관과
        함께 쓰지 않게). ``delete_after`` 초가
        지나도 남아 있으면 우리 라벨의 Job 을 지우고 계속 기다린다(None = 지우지 않는다). 기한은 없다: API 가 답하지 않으면 답할 때까지,
        파드가 안 끝나면 끝날 때까지(``HOLD_WARN_S`` 마다 경고). 돌려주는 값: True = 끝난 것을 확인했다; False = 확인하지 못하고 빠진다 —
        서버 종료(제어 상태를 그대로 두어 다음 Main 이 이어서 지켜본다)이거나 우리 Job 이 아니다(건드리지 않는다).

        The gate before the slot is handed on: wait until the API confirms the Job terminal / gone, deleting our Job once
        `delete_after` seconds passed. False = left unconfirmed (server shutdown: state kept; or a foreign Job: untouched)."""
        started = warned = time.monotonic()
        announced = deleted = False
        delete_retry_at = 0.0
        while True:
            st = self._poll_job(run, client, name)
            now = time.monotonic()
            if st is not None:
                if st.finished:
                    run.job_phase, run.job_pod = st.phase, st.pod or run.job_pod
                    if announced:
                        log.info("run %s: job %s ended (%s) %.0fs after %s", run.run_id, name, st.phase, now - started, why)
                    return True
                if not self._owned(run, st):
                    log.error("run %s: job %s is not owned by this request (labels %s) — left alone, not waited for",
                              run.run_id, name, st.labels)
                    return False
                if not announced:
                    announced = True
                    log.info("run %s: %s — waiting for job %s (%s) to end before the next migration starts", run.run_id, why,
                             name, st.phase)
                    self._push(run, "node", {"node": "job", "update": {"state": "ending", "job": name, "message": why}})
                if delete_after is not None and not deleted and now - started >= delete_after and now >= delete_retry_at:
                    try:
                        client.delete_job(name)
                    except Exception as e:  # noqa: BLE001 — 어떤 실패든 다시 보낸다; 끝난 것을 확인하기 전에는 빠지지 않는다
                        delete_retry_at = now + DELETE_RETRY_S
                        log.warning("run %s: job %s not deleted (%s) — retrying in %.0fs", run.run_id, name, e, DELETE_RETRY_S)
                    else:
                        deleted = True
                        _append_journal(run.run_dir, {"event": "job_deleted", "run_id": run.run_id, "job": name, "reason": why})
                        log.warning("run %s: job %s deleted (%s) — waiting until it is gone", run.run_id, name, why)
            if self.closing.is_set():              # server shutdown: the next Main reads the state on disk and watches the Job on
                run.keep_state = True
                log.warning("run %s: web server shutting down while job %s has not ended — the next web process watches it",
                            run.run_id, name)
                return False
            if now - warned >= HOLD_WARN_S:
                warned = now
                log.warning("run %s: job %s has not ended %.0fs after %s — this run keeps %s%s",
                            run.run_id, name, now - started, why, *self._turn_words(run))
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _watch_job(self, run: Run, client: Any, name: str) -> int:
        """Job 을 끝까지 지켜본다(생성 직후와 이어받기가 함께 쓴다): '파드' 단계 이벤트, 파드가 Running 이 되면 ``running``, 끝나면
        종료 코드 — 이 실행·시도의 ``job-status.json`` 이 먼저, 없으면 컨테이너 종료 코드(검증 안 됨), Job 이 사라졌으면 ``lost``. 멈춤
        요청은 ``stop.request`` 를 쓰고, 유예가 지나면 우리 라벨의 Job 만 지운다. 서버 종료 때는 Job 을 두고 빠진다.
        **이 함수는 Job 이 끝난 것을 확인하고서야 돌아온다**(리뷰 2026-11-10; 서버 종료와 남의 Job 만 예외): 결과 파일을 받았어도 파드가
        끝날 때까지 ``_hold_until_ended``, API 가 답하지 않으면 답할 때까지 기다린다(실패로 끝내지 않는다). '파드' 단계는 Job 의 Failed 를
        그대로 보내지 않는다: 결과 파일을 받으면 ``completed``, 유예 안에 못 받으면 그때 ``failed``(``_close_pod_step``).

        Watch the Job to its end (shared by a fresh create and an adoption). Returns only once the Job is confirmed ended,
        except at server shutdown or for a Job that is not this request's. The pod step's `Failed` is decided by the
        status file, not pushed as the raw phase (`_close_pod_step`)."""
        last: tuple[str, str | None] | None = None
        stop_sent: float | None = None
        run.pod_step_closed = False                # one closing event for the pod step per watch (fresh create or adoption alike)
        while True:
            code = self._accept_status(run)
            if code is not None:                   # the pod said it finished, on the volume — the turn waits until it really ended
                self._close_pod_step(run, name, code=code)
                self._hold_until_ended(run, client, name, delete_after=self.job_exit_grace_s, why="the result is on the volume")
                self._log_failed_phase_explained(run, name, code)
                return code
            st = self._poll_job(run, client, name)   # None: the API does not answer — keep the turn, ask again
            if st is not None:
                if (st.phase, st.pod) != last:
                    last = (st.phase, st.pod)
                    run.job_phase, run.job_pod = st.phase, st.pod
                    # Failed 는 아직 화면에 보내지 않는다: Worker 가 sbmig 의 종료 코드 3(ADF 거부 …)을 그대로 내면 Job 은 Failed 가 되지만
                    # 파드는 제 일을 다 했다 — 파드가 죽었는지는 결과 파일을 기다린 뒤(_await_status) 가린다
                    if st.phase != "Failed":
                        self._push(run, "node", {"node": "job", "update": {"state": st.phase.lower(), "job": name,
                                                                            "pod": st.pod, "message": st.message}})
                    log.info("run %s: job %s %s%s%s", run.run_id, name, st.phase, f" pod {st.pod}" if st.pod else "",
                             f" ({st.message})" if st.message else "")
                if st.phase == "Running" and run.control is not None and run.control.state in (
                        RunState.QUEUED.value, RunState.CREATING.value, RunState.CREATED.value):
                    self._transition_soft(run, RunState.RUNNING, job=name)
                if st.phase in ("Succeeded", "Failed") and not st.finished:
                    # 리뷰 2026-11-10: Job 은 끝났다고 하지만(activeDeadlineSeconds 로 Failed …) 파드가 아직 돌거나 종료 유예 중이다 — Maven 이
                    # 아직 쓸 수 있다. 파드가 끝날 때까지(종료 유예 SBMIG_JOB_EXIT_GRACE_S 가 지나면 우리 Job 을 지우고 사라질 때까지) 자리를 지킨다
                    log.info("run %s: job %s is %s but %d pod(s) of it are still running or terminating — waiting for them",
                             run.run_id, name, st.phase, st.active_pods)
                    ended = self._hold_until_ended(run, client, name, delete_after=self.job_exit_grace_s,
                                                   why=f"the job is {st.phase} while its pod is still terminating")
                    if not ended and run.keep_state:   # server shutdown: the next Main watches the Job on
                        run.error = "web server shut down while the job was running — the job continues in the cluster"
                        return EXIT_ABORTED
                    st = replace(st, active_pods=0)    # its pods ended (or the Job is not ours: left alone, as before)
                if st.finished:
                    code = self._await_status(run, name, st)   # the Worker writes job-status.json just before the pod ends — NFS may show it late
                    if code is None and self.closing.is_set() and st.phase != "Missing" and self.job_status_grace_s > 0:
                        # 서버 종료 중에는 결과 파일을 기다리지 않는다: 제어 상태를 그대로 두면 다음 Main 이 이어받아 그 파일을 읽는다
                        run.error = "web server shut down while waiting for the job's result — the next web process reads it"
                        run.keep_state = True
                        return EXIT_ABORTED
                    if code is not None:
                        self._close_pod_step(run, name, code=code)
                        self._log_failed_phase_explained(run, name, code)
                        return code
                    if st.phase == "Failed":       # no verified result within the grace: the pod died (OOMKilled, deadline, eviction …)
                        self._close_pod_step(run, name, ended=st)
                    run.verified = False
                    if st.phase == "Missing":
                        if run.cancel.is_set() or (run.run_dir / STOP_REQUEST_FILE).is_file():
                            # 멈춤을 요청한 실행의 Job 이 사라졌다(이전 Main 이 지우다 내려갔거나 밖에서 지웠다) — lost 가 아니라 stopped
                            run.error = f"stopped: job {name} is gone after the stop request — no job-status.json for this run/attempt"
                            run.final_state = RunState.STOPPED.value
                            return EXIT_ABORTED
                        run.error = f"job {name} disappeared (deleted outside sbmig?) — no job-status.json for this run/attempt"
                        run.final_state = RunState.LOST.value
                        return EXIT_ABORTED
                    if st.exit_code is not None:
                        run.result_source = "container"
                        log.warning("run %s: job %s ended (exit %s) without a job-status.json for attempt %d — result not verified",
                                    run.run_id, name, st.exit_code, run.attempt)
                        return int(st.exit_code)
                    run.error = st.message or f"job {name} {st.phase.lower()} without a container exit code"
                    run.final_state = RunState.LOST.value
                    return EXIT_ABORTED
            if self.closing.is_set():              # server shutdown: the Job goes on, the next server answers from files
                run.error = "web server shut down while the job was running — the job continues in the cluster"
                run.keep_state = True
                return EXIT_ABORTED
            if run.cancel.is_set():
                if stop_sent is None:
                    try:
                        (run.run_dir / STOP_REQUEST_FILE).write_text("stop requested from the web UI\n", encoding="utf-8")
                    except OSError as e:           # the volume hiccuped: ask again on the next poll, never drop the watch
                        log.warning("run %s: %s not written (%s) — retrying", run.run_id, STOP_REQUEST_FILE, e)
                    else:
                        stop_sent = time.monotonic()
                        _append_journal(run.run_dir, {"event": "stop_request_sent", "run_id": run.run_id, "job": name})
                        log.info("run %s: stop requested — %s written for job %s", run.run_id, STOP_REQUEST_FILE, name)
                elif st is not None and time.monotonic() - stop_sent > self.job_stop_grace_s:
                    run.final_state = RunState.STOPPED.value
                    if not self._owned(run, st):
                        # 같은 이름이라도 라벨이 다르면 다른 요청의 Job 이다 — 지우지 않는다
                        run.error = f"job {name} is not owned by this request — not deleted"
                        log.error("run %s: %s (labels %s)", run.run_id, run.error, st.labels)
                        _append_journal(run.run_dir, {"event": "job_delete_refused", "run_id": run.run_id, "job": name})
                        return EXIT_ABORTED
                    log.warning("run %s: job %s still running %.0fs after the stop request — deleting it",
                                run.run_id, name, self.job_stop_grace_s)
                    run.error = "stopped: the job was deleted after the grace period"
                    # 지운 뒤 사라질 때까지 기다린다(기한 없음) — 파드가 아직 볼륨에 쓰는 동안 다음 이관을 시작하지 않게
                    self._hold_until_ended(run, client, name, delete_after=0.0, why="the stop grace period is over")
                    return EXIT_ABORTED
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _close_pod_step(self, run: Run, name: str, *, code: int | None = None, ended: JobState | None = None) -> None:
        """'파드' 단계를 닫는 이벤트를 **이 감시에서 한 번만** 보낸다. '파드' 는 "파드가 제 일을 했나" 이지 이관의 종료 코드가 아니다
        (2026-11-10 회사 실행: ADF 프로젝트를 분석에서 멈춘 실행(종료 코드 3)의 Job 이 Failed 가 되자 화면이 '파드' 를 X 로 그렸다).
        ``code`` = 이 실행·시도의 ``job-status.json`` 을 받았다: ``error`` 가 없으면 ``completed``(종료 코드가 무엇이든), 있으면 — Worker 가
        요청을 거부했거나 sbmig 를 띄우지 못했다 — ``failed`` + 그 오류. ``ended`` = 결과 파일 없이 Job 이 Failed 로 끝났다(유예가 지났다):
        파드가 죽었다 — ``failed`` + 쿠버네티스 메시지(예전 모양 그대로).

        Push the pod step's closing event, at most once per watch: a verified status without `error` → `completed` (whatever
        the exit code), with `error` → `failed` carrying it; `ended` (the Job ended Failed, no verified status within the
        grace) → `failed` with the Kubernetes message, the pod died."""
        if run.pod_step_closed:
            return
        update: dict[str, Any]
        if code is not None:
            update = {"state": "failed" if run.status_error else "completed", "job": name, "pod": run.job_pod,
                      "exit_code": code}
            if run.status_error:
                update["message"] = run.status_error
            if run.job_phase:                  # the raw Kubernetes phase when one was seen (Failed, Running …)
                update["phase"] = run.job_phase
        elif ended is not None:
            update = {"state": "failed", "job": name, "pod": ended.pod, "message": ended.message}
        else:
            return
        run.pod_step_closed = True
        self._push(run, "node", {"node": "job", "update": update})

    def _log_failed_phase_explained(self, run: Run, name: str, code: int) -> None:
        """Job 이 Failed 로 끝났지만 그 까닭이 검증된 결과 파일의 종료 코드일 때 로그 한 줄 — 운영자가 sbmig.log 에서 "파드 실패" 로
        잘못 읽지 않게(Worker 는 sbmig 의 종료 코드 3 을 컨테이너 종료 코드로 그대로 낸다).

        One log line when a verified result explains a `Failed` Job phase (the Worker's own exit code, not a pod failure)."""
        if run.job_phase == "Failed" and run.verified and not run.status_error:
            log.info("run %s: job %s: Failed is the Worker's exit code %s (job-status.json verified) — not a pod failure",
                     run.run_id, name, code)

    def _await_status(self, run: Run, name: str, st: JobState) -> int | None:
        """Job 이 끝난 직후: 이 실행·시도의 ``job-status.json`` 이 보일 때까지 ``job_status_grace_s``(기본 90초) 동안 다시 읽는다 — Worker 는
        파드가 끝나기 직전에 그 파일을 쓰는데, NFS 의 속성 캐시는 새 파일을 최대 1분 늦게 보여 줄 수 있다(끝난 결과를 "검증 안 됨" 으로
        잘못 적지 않게). Missing(지워진 Job)은 기다리지 않는다.

        After the Job ended, re-read `job-status.json` for up to `job_status_grace_s` (NFS attribute caching); None when it
        never shows. A server shutdown ends the wait at once (None; the caller keeps the control state for the next Main)."""
        code = self._accept_status(run)
        if code is not None or st.phase == "Missing" or self.job_status_grace_s <= 0 or self.closing.is_set():
            return code
        deadline = time.monotonic() + self.job_status_grace_s
        log.info("run %s: job %s ended (%s) but job-status.json is not visible yet — waiting up to %.0fs for the shared volume",
                 run.run_id, name, st.phase, self.job_status_grace_s)
        while time.monotonic() < deadline and not self.closing.is_set():   # shutdown notifies run.cond: leave at once
            with run.cond:
                run.cond.wait(min(self.job_poll_s, 1.0))
            code = self._accept_status(run)
            if code is not None:
                return code
        return None

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
                # 2026-11-11: 이관 뒤에 이어 도는 remediation 노드도 최종 빌드 뒤다 — 여기서의 중지가 성공한 이관을 중단으로 적으면 안 된다
                if not update.get("aborted") and (name in ("finalize", "remediation")
                                                  or (name == "phase" and update.get("phase") == Phase.VERIFY.value)):
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
        """실행 기록에 이벤트를 붙이고 기다리는 SSE 스트림을 깨운다(``node`` 면 마지막 노드 이름도 갱신). 출력 정제가 걸려 있으면
        이벤트의 문자열도 지나간다 — 화면으로 가는 또 하나의 출력이다."""
        if output_scrubbing():
            data = scrub_json(data)
        with run.cond:
            run.history.append((kind, data))
            if kind == "node":
                run.last_node = data.get("node")
            run.cond.notify_all()

    def stop(self, run_id: str) -> bool:
        """지금 그래프 노드가 끝나면 멈추라고 요청한다(진행 중인 Maven·LLM 호출은 끊지 않는다).
        모르는 실행이거나 이미 끝났으면 False. Job 모드에서 아직 기다리는 실행은 **지금** 줄에서 빠져 stopped 로 끝난다(Job 을 만들지
        않는다; 리뷰 2026-11-10 — 예전에는 차례가 올 때까지 줄의 자리를 지켜 뒤의 실행들의 순번을 부풀렸다). in-process 의 대기 실행은
        예전처럼 차례가 와서 첫 노드를 마친 뒤 멈춘다.

        Ask a running run to stop after the current graph node. False if the run is unknown or finished. In the Job modes a
        run still waiting leaves the line at once and ends stopped (no Job is created); in-process it stops after its first
        node when its turn comes, as before."""
        run = self.runs.get(run_id)
        if run is None or run.done.is_set():
            return False
        run.cancel.set()                       # first: a turn that arrives in the meantime sees it and creates no Job
        if run.job and self._turns.withdraw(run_id):
            log.info("run %s: stop requested while waiting — it leaves the line", run_id)
        else:
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
            if not run.job:                       # a k8s Job keeps running; its watcher thread just wakes up and leaves
                run.cancel.set()
                stopped.append(run.run_id)
            with run.cond:
                run.cond.notify_all()
        killed = kill_live_processes(reason)
        log.info("%s: %d run(s) asked to stop (%s), %d live Maven process(es) killed", reason, len(stopped),
                 ", ".join(stopped) or "-", killed)
        return {"stopped": stopped, "killed": killed}

    @staticmethod
    def _final_pass_pending(run: Run | None, control: ControlState | None) -> bool:
        # 끝날 때의 마지막 정제가 아직 남았는가. 메모리의 실행: 끝나지 않았고, Job 이거나(Worker 가 끝에 지운다) 이 프로세스에
        # 출력 정제가 걸려 있다. 메모리에 없는 실행: Main 의 control 상태가 아직 끝나지 않았다(Job 이 돌고 있을 수 있다)
        if run is not None:
            return not run.done.is_set() and bool(run.job or output_scrubbing())
        return control is not None and not control.terminal

    def file_withheld(self, run_id: str, name: str) -> bool:
        """이 실행의 ``name`` 을 아직 내주면 안 되는가 — 마지막 정제(실행이 끝날 때 실행 폴더를 훑어 비밀 값을 지우는 단계)가 남았고
        ``name`` 이 쓰는 순간 정제되는 파일(``LIVE_FILES``)이 아닐 때 True.

        Must `name` of this run still be withheld: the end-of-run redaction pass is still to come and `name` is not one of
        the files redacted as they are written (`LIVE_FILES`)."""
        if name in LIVE_FILES:
            return False
        run = self.runs.get(run_id)
        control = None
        if run is None:
            rd = self.run_dir(run_id)
            control = ControlState.read(rd) if rd.is_dir() else None
        return self._final_pass_pending(run, control)

    def status(self, run_id: str) -> dict[str, Any]:
        """실행 상태 dict. 메모리에 있으면 queued / running / finished, 없으면(밀려남·이전 프로세스)
        web-status.json → report.json → 폴더 유무 순으로 finished / unknown / missing.
        report.json 이 있으면 성공·차단·미해결·LLM 호출·오류 목록 개수, 그리고 내려받을 수 있는 파일 목록.
        v2.1z-ae: 늘 ``parallel`` {slots, running, waiting}; 이 프로세스의 줄에서 기다리는 실행은 ``queue`` {position, ahead};
        자리를 쥔(쥐었던) Job 실행은 ``slot`` (메모리, 없으면 control/state.json).

        The run's status dict (memory first, else the run directory). Always `parallel`; `queue` for a run waiting in this
        process's line; `slot` for a Job run that holds or held one."""
        run = self.runs.get(run_id)
        rd = self.run_dir(run_id)
        info: dict[str, Any] = {"run_id": run_id, "run_dir": str(rd), "exists": rd.is_dir() or run is not None,
                                "mode": self.effective_mode, "parallel": self.parallel()}
        control: ControlState | None = None
        if run is not None:
            info["state"] = self._mem_state(run)
            info["exit_code"] = run.exit_code
            info["node"] = run.last_node
            if info["state"] == "queued":
                pos = self._turns.position(run_id)
                if pos is not None:            # None: it took a slot a moment ago, `active` follows at once
                    info["queue"] = {"position": pos, "ahead": pos - 1}
            if run.job and run.slot is not None:
                info["slot"] = run.slot
            if run.job:
                info["job"] = {"name": run.job_name, "phase": run.job_phase, "pod": run.job_pod}
                if run.job_note:               # e.g. the Kubernetes API does not answer: why this run still holds its turn
                    info["job"]["note"] = run.job_note
                info["attempt"] = run.attempt
                info["verified"] = run.verified
                if run.control is not None:
                    info["control"] = run.control.state
            if run.cancel.is_set():
                info["stop_requested"] = True
            if run.error:
                info["error"] = run.error
        else:   # evicted, or a run of an earlier server process: only the directory is known
            control = ControlState.read(rd) if rd.is_dir() else None     # v2.1z-ac: Main's control state speaks first
            saved = _read_web_status(rd) or _read_job_status(rd)      # the pod's job-status.json also ends a run
            if control is not None:
                info["attempt"] = control.attempt
                info["control"] = control.state
                if control.slot is not None:
                    info["slot"] = control.slot
                if not control.terminal:
                    info["state"] = "queued" if control.state == RunState.QUEUED.value else "running"
                    info["job"] = {"name": control.job, "phase": None, "pod": None}
                    info["exit_code"] = None
                else:
                    info["state"] = "finished"
                    info["exit_code"] = control.exit_code
                    if control.error:
                        info["error"] = control.error
                    if (saved or {}).get("stop_requested"):
                        info["stop_requested"] = True
                    if control.job:
                        info["job"] = {"name": control.job, "phase": None, "pod": None}
            elif saved is not None:
                info["state"] = "finished"
                info["exit_code"] = saved.get("exit_code")
                if saved.get("stop_requested"):
                    info["stop_requested"] = True
                if saved.get("error"):
                    info["error"] = saved["error"]
            else:
                job = _journal_job(rd)                # a Job created by an earlier web process may still be running in the cluster
                if job is not None and not (rd / "report.json").is_file():
                    info["state"] = "running"
                    info["job"] = {"name": job.get("job"), "phase": None, "pod": None}
                    info["node"] = job.get("last_node")
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
        pending = self._final_pass_pending(run, control)       # 마지막 정제 전: 쓰는 순간 정제되는 파일만 목록에 올린다
        for name in SERVED_FILES:
            if (rd / name).is_file() and (not pending or name in LIVE_FILES):
                info.setdefault("files", []).append(name)
        return info

    def _mem_state(self, run: Run) -> str:
        # 메모리 실행의 상태: 자리를 쥐었으면 running 이다 — 작업 스레드가 active 를 세우기 직전의 찰나(그리고 복구 직후 자리를 다시 잡은
        # 실행)에 자리 번호 없는 queued 로 보이지 않게
        if run.done.is_set():
            return "finished"
        if run.active.is_set() or self._turns.slot_of(run.run_id) is not None:
            return "running"
        return "queued"

    def parallel(self) -> dict[str, int]:
        """동시 실행의 지금 모습(v2.1z-ae): ``slots`` = 한도(``max_parallel``), ``running`` = 자리를 쥔 실행 수(복구 직후에는 한도보다
        많을 수 있다), ``waiting`` = 줄에서 기다리는 수.

        The parallel picture: the limit, the runs holding a slot (may exceed the limit right after a restart), the waiting."""
        running, waiting = self._turns.counts()
        return {"slots": self.max_parallel, "running": running, "waiting": waiting}

    def list_runs(self) -> list[dict[str, Any]]:
        """메모리와 출력 폴더의 모든 실행(``_`` 로 시작하는 폴더 제외)의 상태를 최신순으로. 줄마다 ``parallel`` 은 빼고(라우트가 한 번
        붙인다) ``queue`` / ``slot`` 은 아는 만큼 남는다.

        Every run's status, newest first; `parallel` is left to the route (once), `queue` / `slot` stay when known."""
        ids = set(self.runs)
        if self.out_dir.is_dir():
            ids |= {p.name for p in self.out_dir.iterdir() if p.is_dir() and not p.name.startswith("_")}
        rows = []
        for rid in ids:
            st = self.status(rid)
            st.pop("parallel", None)
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
        놓는다(브라우저가 나중에 다시 붙는다). v2.1z-ae: 끝나기 전에도 (상태, 줄의 자리)가 바뀌면 ``status`` 를 다시 보낸다 — 기다리는
        화면이 새로 고침 없이 3 → 2 → 1 → 시작을 본다(박자는 ``POLL_S`` 마다 보는 값싼 비교, 시작할 때는 실행의 조건 변수가 바로 깨운다).

        Yield (event, data) until the run is finished and everything has been drained (blocking generator). `status` is
        sent again whenever the run's (state, queue position) changes before it finished."""
        rd = self.run_dir(run_id)
        run = self.runs.get(run_id)
        offsets: dict[Path, int] = {}
        seen = 0
        last_beat = time.monotonic()
        first = self.status(run_id)
        last_sig = _queue_sig(first)
        yield "status", first

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
                    # k8s Job 모드 / 옛 실행 재생: 파드가 쓴 graph_node_end 줄이 화면의 node 이벤트가 된다(자기 프로세스 실행은
                    # _push 로 이미 받는다)
                    if item[0] == "journal" and isinstance(item[1], dict) and item[1].get("event") == "graph_node_end" \
                            and (run is None or run.job):
                        ev = item[1]
                        update = {k: ev[k] for k in ("phase", "phases", "aborted", "stopped") if ev.get(k) is not None}
                        batch.append((last_key, len(batch), "node", {"node": ev.get("node"), "update": update}))
                        if run is not None:
                            run.last_node = ev.get("node")
            for _key, _n, kind, data in sorted(batch, key=lambda t: (t[0], t[1])):
                yield kind, data

        while True:
            # 끝났는지는 비우기 *전에* 본다: done 이 선 뒤의 비우기라면 실행이 쓴 것이 모두 파일에 들어 있다
            now_st: dict[str, Any] | None = None
            if run is not None:
                finished = run.done.is_set()
            else:
                now_st = self.status(run_id)
                finished = now_st["state"] not in ("running", "queued")
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
            # v2.1z-ae: 기다리는 실행의 자리가 바뀌었거나 시작했다 — 값싼 비교로 먼저 보고, 바뀌었을 때만 status() 를 만든다
            if run is not None:
                state = self._mem_state(run)
                if (state, self._turns.position(run_id) if state == "queued" else None) != last_sig:
                    now_st = self.status(run_id)
            if now_st is not None and now_st.get("state") in ("running", "queued") and _queue_sig(now_st) != last_sig:
                last_sig = _queue_sig(now_st)
                yield "status", now_st
                emitted = True
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


def _queue_sig(st: dict[str, Any]) -> tuple[Any, Any]:
    # SSE 가 status 를 다시 보낼지 정하는 값: (상태, 줄의 자리). 자리는 이 프로세스의 줄에서 기다릴 때만 있다
    return st.get("state"), (st.get("queue") or {}).get("position")


def _write_web_status(run: Run) -> None:
    """작업 스레드가 끝날 때 web-status.json(종료 코드·오류·멈춤 요청·마지막 노드)을 쓴다 — 메모리에서
    밀려났거나 서버를 다시 띄운 뒤에도 상태를 답하려고. 못 써도 실행 결과에는 영향이 없다."""
    try:
        data = {"run_id": run.run_id, "exit_code": run.exit_code, "error": run.error,
                "stop_requested": run.cancel.is_set(), "last_node": run.last_node,
                "success": run.success if run.success is not None else getattr(run.report, "success", None)}
        (run.run_dir / WEB_STATUS_FILE).write_text(json.dumps(data, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:   # the status file is a convenience; never fail the run on it
        log.debug("cannot write %s for %s: %s", WEB_STATUS_FILE, run.run_id, e)


def _journal_job(rd: Path) -> dict[str, Any] | None:
    """실행 폴더 저널의 ``job_created`` 줄(+ 마지막 graph_node_end 의 노드) — 이전 웹 프로세스가 만든 Job 이 아직 돌고 있을 수 있다.
    ``JOB_STALE_S`` 보다 오래됐으면 None(끝났는데 상태 파일이 없는 것으로 본다).

    The run dir's `job_created` journal line (+ the last node ended), unless older than `JOB_STALE_S`; None otherwise."""
    try:
        created: dict[str, Any] | None = None
        last_node = None
        with open(rd / "journal.jsonl", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "job_created":
                    created = ev
                elif ev.get("event") == "graph_node_end":
                    last_node = ev.get("node")
        if created is None:
            return None
        ts = created.get("ts")
        if ts:
            started = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - started).total_seconds() > JOB_STALE_S:
                return None
        return {"job": created.get("job"), "last_node": last_node}
    except (OSError, ValueError, TypeError):
        return None


def _read_job_status(rd: Path) -> dict[str, Any] | None:
    """k8s Job 모드: 파드의 run-job.sh 가 쓴 ``job-status.json`` (없거나 깨졌으면 None)."""
    try:
        data = json.loads((rd / JOB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "exit_code" in data else None


def _report_success(rd: Path) -> bool | None:
    """report.json 의 success (없으면 None) — Job 모드의 finished 이벤트용."""
    try:
        return json.loads((rd / "report.json").read_text(encoding="utf-8")).get("success")
    except (OSError, ValueError, AttributeError):
        return None


def _append_journal(rd: Path, event: dict[str, Any]) -> None:
    """실행 폴더의 journal.jsonl 에 한 줄(ts 포함) — Job 모드에서 웹이 쓰는 몇 줄(web_run, job_created, stop_request_sent)."""
    try:
        line = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        text = json.dumps(line, default=str)
        if output_scrubbing():                    # 값 단위로: 직렬화한 줄을 통째로 바꾸면 JSON 이 깨질 수 있다
            text = json.dumps(scrub_json(json.loads(text)))
        with open(rd / "journal.jsonl", "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except (OSError, TypeError, ValueError) as e:
        log.debug("journal line %s not written: %s", event.get("event"), e)


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
