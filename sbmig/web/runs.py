"""웹 앱에서 이관 실행을 시작하고 상태와 진행 이벤트를 관리하는 모듈.
이관마다 작업 스레드 하나를 두고 SSE로 보낼 이벤트를 모은다.
라우트가 :meth:`RunManager.start_file`을 호출하면 설정과 오케스트레이터를 바로 만들고(잘못된 요청은 400),
``_worker``가 ``stream_graph``의 노드마다 이벤트를 기록한다. :meth:`RunManager.events`는 그 기록과 저널,
로그의 새 줄을 SSE로 보낸다.
- 동시에 실행하는 이관 수는 Job 모드에서 ``SBMIG_JOB_MAX_PARALLEL``개(기본 1), in-process에서 1개이며
  ``_turns``의 대기열과 슬롯으로 관리한다
- 중지는 그래프 노드 사이에서만 적용된다. 진행 중인 Maven, OpenRewrite, LLM 호출은 끊지 않는다
- 실행별 로그는 스레드 필터로 나누어 각 ``sbmig.log``에 그 실행의 줄만 남긴다
- 끝난 실행은 메모리에 ``max_finished_runs``개까지만 두고, 나머지는 실행 폴더의 파일로 응답한다
- Job을 만드는 Main이 환경 변수 해시(``env_pins``)와 정책 ``SBMIG_JOB_ENV_PIN``을 creating 상태와 함께
  기록하고, Worker가 자기 환경과 비교한다
- 취약점 조치 재개(``resume_remediation``)는 같은 실행의 새 시도를 별도 Job으로 만든다.
  업로드와 같은 대기열을 쓰고, 이관의 상태와 보고서는 바꾸지 않는다

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

Remediation resume (2026-11-11, wave 4): `resume_remediation(run_id)` dispatches a NEW attempt of a finished Job-mode run with
the action `remediation-resume` — the same template, run dir, labels (owner token of its own), FIFO line and slots as an upload;
its control state lives in `control/remediation-resume.json`, its result in `remediation-job-status.json`, its Job is named
`<migration job name>-rem<attempt>`. The migration's own state, report and `web-status.json` are never touched: `status()` keeps
the run finished and adds `remediation_job` {state, attempt, slot, queue …}. Recovery after a restart re-attaches to a resume
Job exactly like to a migration Job. At most one active resume per run.
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
from sbmig.core.models import FLOW_FULL, FLOW_REMEDIATION, RUN_FLOWS
from sbmig.core.scrub import output_scrubbing, scrub, scrub_json
from sbmig.core.workspace import STOP_REQUEST_FILE
from sbmig.web import contract
from sbmig.web.contract import (
    ACTION_REMEDIATION_RESUME,
    CONTROL_DIR,
    OWNER_LABEL,
    RESUME_STATE_FILE,
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
# 기록할 때 이미 마스킹되는 파일이므로 실행 중에도 내려받을 수 있다.
# 나머지 파일은 실행이 끝나고 마지막 마스킹을 거친 뒤에만 내준다(`RunManager.file_withheld`)
LIVE_FILES = ("sbmig.log", "journal.jsonl")
# journal과 sbmig.log의 새 줄은 변경 알림이 없으므로 0.5초마다 다시 읽는다. 몇 분 걸리는 Maven 빌드 동안
# 브라우저나 프록시가 연결을 끊지 않도록 15초 동안 보낼 것이 없으면 ping을 보낸다
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
# Job 생성 응답이 불확실하면(이미 있음, 전송 오류) Job을 두 번 만들지 않도록 같은 이름의 Job을 이어받는다
_UNCERTAIN_CREATE_RE = re.compile(r"HTTP 409|RequestException|ConnectionError|Timeout|ChunkedEncodingError|ProtocolError")
# Job이 끝난 것을 확인한 뒤에 슬롯을 넘겨야 슬롯 수 한도가 지켜진다(_hold_until_ended).
# 아래는 그 대기에 쓰는 간격이다
HOLD_WARN_S = 60.0                 # 끝나지 않는 Job을 기다리는 동안 경고를 남기는 간격
DELETE_RETRY_S = 15.0              # Job 삭제 요청이 실패했을 때 다시 시도하는 간격
API_REMIND_S = 600.0               # Kubernetes API가 계속 응답하지 않을 때 로그를 다시 남기는 간격
CREATE_CONFIRM_POLLS = 3           # 생성 응답이 불확실하고 Job이 보이지 않을 때 더 조회하는 횟수


class _ServerClosing(Exception):
    """Kubernetes API 응답을 기다리는 중에 서버가 종료될 때 쓰는 예외 클래스.
    실행을 실패로 기록하지 않고 제어 상태를 그대로 두어 다음에 뜨는 Main이 이어받게 한다.

    The server shuts down while a run waits for the Kubernetes API: the control state is kept for the next Main."""


class _LeftLine(Exception):
    """대기 중이던 실행이 중지 요청으로 대기열에서 빠졌을 때 쓰는 예외 클래스(``_FifoTurns.withdraw``).
    이 실행은 슬롯을 받지 않았고 Job도 만들지 않았다.

    A waiting run was taken out of the line by a stop (`_FifoTurns.withdraw`): no slot, no Job."""


@dataclass
class Run:
    """이 서버 프로세스가 시작한 실행 하나의 메모리 상태를 담는 클래스.
    이벤트 기록(``history``), 중지 요청(``cancel``), 대기열 통과(``active``), 종료(``done``)를 담는다.
    이전 프로세스가 시작한 실행은 실행 폴더로만 남는다.

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
    # k8s Job 모드에서는 이 실행이 다른 파드에서 실행되므로 Job 이름과 마지막으로 확인한 상태를 둔다
    job: bool = False
    job_name: str | None = None
    job_phase: str | None = None
    job_pod: str | None = None
    # 접수할 때 고정한 입력(control/request.json)과 Main의 제어 상태(control/state.json)
    request: Any = None
    control: Any = None
    verified: bool | None = None          # True면 이 실행과 시도의 job-status.json을 받았다는 뜻
    result_source: str | None = None      # "status"(job-status.json) | "container"(컨테이너 종료 코드만)
    final_state: str | None = None        # _run_job이 정한 종료 상태이며 없으면 결과 확인으로 finished / lost를 정한다
    keep_state: bool = False              # 서버가 종료될 때 다음 Main이 이어받도록 state.json을 그대로 둔다
    recover: str | None = None            # 재시작 후 복구 방식: None | "probe" | "adopt"
    status_warned: bool = False
    status_error: str | None = None       # Worker가 실행 자체를 하지 못했을 때 job-status.json에 담긴 error
    pod_step_closed: bool = False         # '파드' 단계를 닫는 이벤트는 한 번만 보내므로 이미 보냈는지 기억한다
    # Kubernetes API가 응답하지 않는 동안의 정보로, 처음 실패한 시각, 마지막으로 알린 시각(monotonic),
    # 화면에 보일 한 줄을 담는다
    api_down_since: float | None = None
    api_down_reported: float | None = None
    job_note: str | None = None
    success: bool | None = None
    # 이 Job 실행이 차지하고 있거나 차지했던 슬롯 번호이며 Job env SBMIG_JOB_SLOT, control/state.json의 slot과 같다
    slot: int | None = None
    # None은 이관이고 "remediation-resume"은 조치 재개 시도다.
    # 재개 시도는 제어 상태, 결과 파일, 대기열 키를 따로 쓴다
    action: str | None = None
    # 실행 방식으로 full(이관 전체) | remediation(이관 없이 취약점 조치만) 중 하나다.
    # Job 실행에서는 request.json의 값과 같다
    flow: str = FLOW_FULL
    # 사용자가 올린 파일 이름 그대로이며 화면이 실행을 이 이름으로 보여 준다
    upload: str | None = None

    @property
    def key(self) -> str:
        """대기열(``_turns``)에서 쓰는 키. 이관은 실행 id이고 재개 시도는 ``<실행 id>#<action>``이므로 같은
        실행의 두 시도가 서로를 막지 않는다.

        The turn key: the run id for a migration, `<run id>#<action>` for a resume attempt."""
        return f"{self.run_id}#{self.action}" if self.action else self.run_id

    @property
    def control_file(self) -> str:
        """이 시도의 제어 상태 파일 이름(``control/`` 안)을 반환한다."""
        return contract.state_file_for(self.action)

    @property
    def status_file(self) -> str:
        """이 시도의 Worker 결과 파일 이름(실행 폴더 안)을 반환한다."""
        return contract.status_file_for(self.action)

    @property
    def attempt(self) -> int:
        """제어 상태에 기록된 이 실행의 시도 번호를 반환한다. 제어 상태가 없으면 1을 반환한다. / The attempt number (from the control state; 1 without one)."""
        return int(self.control.attempt) if self.control is not None else 1

    @property
    def owner(self) -> str | None:
        """Job 라벨 ``sbmig-owner``의 값. 요청마다 다른 난수다."""
        return self.control.owner if self.control is not None else None


class _FifoTurns:
    """업로드 순서대로 실행을 들여보내는 대기열과 번호 붙은 슬롯을 관리하는 클래스. 동시에 ``slots``개까지
    실행하며, 기다리는 스레드 사이의 순서를 ``threading.Lock``이 보장하지 않으므로 직접 구현했다.
    - ``join``으로 줄 끝에 서고, ``turn``은 맨 앞이 되고 빈 슬롯이 생길 때까지 기다렸다가 비어 있는 가장
      작은 번호(1부터)를 차지한다. 블록을 벗어나면 예외가 나도 슬롯을 비운다
    - ``occupy``는 재시작 복구에서만 쓴다. 이미 실행 중인 Job은 한도와 상관없이 기록된 번호로 슬롯을 잡고,
      새 실행은 아무도 쓰지 않는 번호만 받는다
    - ``withdraw``는 중지된 대기 실행을 줄에서 뺀다

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
        # 키별 슬롯 번호다. 새 실행은 아무도 쓰지 않는 번호만 받지만, 복구(occupy)는 기록된 번호를 그대로
        # 쓰므로 두 키가 같은 번호를 가질 수 있다. 두 파드가 실제로 그 번호로 실행 중이기 때문이다
        self._active: dict[str, int] = {}
        self._withdrawn: set[str] = set()       # 대기 중에 중지되어 줄에서 빠진 키이며 turn()이 깨어나 _LeftLine을 던진다

    def _lowest_free(self) -> int:
        # 아무도 쓰지 않는 가장 작은 번호(1부터)를 반환한다. 호출하는 쪽이 _cond를 잡고 있어야 한다
        used = set(self._active.values())
        n = 1
        while n in used:
            n += 1
        return n

    def join(self, key: str) -> None:
        """키를 대기 줄 끝에 세운다. 같은 키가 이미 줄에 있거나 슬롯을 갖고 있으면 그대로 둔다.

        Take a place at the end of the line (no-op when `key` is already waiting or holds a slot)."""
        with self._cond:
            if key not in self._line and key not in self._active:
                self._line.append(key)

    def leave(self, key: str) -> None:
        """키를 대기 줄이나 슬롯에서 빼고 기다리는 스레드를 깨운다. 없는 키면 아무것도 하지 않는다.

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
        """대기 중인 키를 바로 줄에서 뺀다. 그 키의 ``turn()``은 깨어나서 슬롯 대신 ``_LeftLine``을 던진다.
        줄에 없으면(이미 슬롯을 받았거나 모르는 키) 아무것도 바꾸지 않고 False를 반환한다.
        슬롯 배정과 같은 잠금 안에서 판단하므로 둘 중 하나만 일어난다.

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
        """재시작 복구에서 한도와 상관없이 바로 슬롯을 잡고 그 번호를 반환한다.
        ``prefer``(state.json에 기록된 번호)가 있으면 다른 키가 쓰고 있어도 그 번호를 쓴다. 그 Job의 파드가 실제로 그
        번호의 Maven 로컬 저장소를 쓰고 있기 때문이며, 겹치면 두 실행 이름을 담아 WARNING을 남긴다.
        ``prefer``가 없으면 비어 있는 가장 작은 번호를, 이미 슬롯을 가진 키면 그 번호를 반환한다.

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
        """``key``가 줄 맨 앞이 되고 빈 슬롯이 생길 때까지 기다린 뒤 슬롯 번호를 넘기고, 블록이 끝나면 슬롯을 비운다.
        ``join`` 없이 호출하면 이때 줄 끝에 서고, ``occupy``로 이미 슬롯을 가진 키는 바로 받는다.
        기다리는 중에 ``withdraw``로 빠지면 ``_LeftLine``을 던진다.

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
                # 슬롯이 둘 이상이면 다음 차례도 바로 들어갈 수 있으므로 기다리는 스레드를 모두 깨워 직접 확인하게 한다
                self._cond.notify_all()
        try:
            yield slot
        finally:
            self.leave(key)

    def position(self, key: str) -> int | None:
        """대기열에서 몇 번째인지 반환한다(1이면 다음 차례). 기다리고 있지 않으면 None을 반환한다. / 1-based place among the waiting; None when not waiting."""
        with self._cond:
            try:
                return self._line.index(key) + 1
            except ValueError:
                return None

    def slot_of(self, key: str) -> int | None:
        """이 키가 가진 슬롯 번호를 반환한다. 없으면 None을 반환한다."""
        with self._cond:
            return self._active.get(key)

    def counts(self) -> tuple[int, int]:
        """(슬롯을 가진 키 수, 대기 중인 키 수)를 반환한다. 복구로 두 키가 같은 번호를 가지면 두 개로 센다."""
        with self._cond:
            return len(self._active), len(self._line)


_LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d{3}))?\b")


def _event_time(kind: str, data: Any) -> float | None:
    """journal 이벤트(``ts``, UTC ISO 8601)나 ``sbmig.log`` 줄(로컬 시각)의 시각을 epoch 초로 반환한다.
    두 파일의 줄을 시간순으로 섞을 때 쓴다.
    시각이 없는 줄(앞 줄에 이어지는 줄, traceback)은 None을 반환한다.

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
    """동시에 실행할 Job 수를 정한다. 인자를 먼저 쓰고, 없으면 ``SBMIG_JOB_MAX_PARALLEL``을 쓴다.
    1 이상의 정수가 아니면 경고를 남기고 1을 쓴다.
    ``k8s-job`` / ``local-job``에서만 적용하고, in-process는 항상 1이다.

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
            # 같은 프로세스 안의 이관들은 LLM 호출 수와 실행별 로그 필터를 함께 쓰므로 한도는 Job 모드에서만 적용한다
            log.warning("%s=%d is ignored in %s mode — one migration at a time (the LLM call counter and the per-run log "
                        "filter are process-wide); set SBMIG_WEB_MODE=k8s-job or local-job to run several",
                        contract.MAX_PARALLEL_ENV, asked, mode)
        return 1
    return asked


def resume_job_name(run_id: str, attempt: int) -> str:
    """조치 재개 시도의 Job 이름을 만든다.
    이관 Job 이름(``job_name``)에 ``-rem<시도 번호>``를 붙이고 63자를 넘지 않게 한다.

    The Job name of a remediation resume attempt: the migration's Job name + `-rem<attempt>` (≤ 63 characters)."""
    suffix = f"-rem{int(attempt)}"
    return job_name(run_id)[:63 - len(suffix)].rstrip("-") + suffix


# 실행 id 앞부분을 올린 파일 이름으로 만들어 어느 프로젝트인지 알 수 있게 한다. 24자로 제한해야 실행 id가
# 47자 이하가 되고, 재개 Job 이름이 시도 번호 네 자리까지 63자(DNS-1123 라벨 한도) 안에 들어간다
# 이름이 잘리면 끝의 난수가 먼저 사라진다
RUN_ID_SLUG_MAX = 24
RUN_ID_FALLBACK = "web"


def run_id_slug(upload_name: str | None) -> str:
    """올린 파일 이름으로 실행 id의 앞부분을 만든다. 경로와 ``.zip``을 떼고 소문자로 바꾼 뒤, ``[a-z0-9]``
    밖의 문자 묶음은 ``-`` 하나로 바꾸고 ``RUN_ID_SLUG_MAX``자로 자른다.
    영문자가 하나도 없으면 ``web``을 쓴다.
    숫자만으로는 어느 프로젝트인지 알 수 없고 뒤의 날짜와 헷갈리기 때문이다.

    The run-id prefix from the upload name: basename without `.zip`, lowercase, every run outside `[a-z0-9]` → one `-`,
    trimmed, cut to `RUN_ID_SLUG_MAX`; a result without any letter (empty, or only the digits left of a Korean name) →
    `web`."""
    name = re.split(r"[\\/]", str(upload_name or ""))[-1]      # OS와 상관없이 경로 구분자 앞부분은 버린다
    if name.lower().endswith(".zip"):
        name = name[:-4]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:RUN_ID_SLUG_MAX].rstrip("-")
    return slug if re.search(r"[a-z]", slug) else RUN_ID_FALLBACK


def run_id_for(upload_name: str | None, now: datetime | None = None, token: str | None = None) -> str:
    """새 실행 id ``<앞부분>-YYYYMMDD-HHMMSS-<hex 6>``를 만든다. ``now`` / ``token``은 테스트용이다.
    같은 초에 같은 파일을 두 번 올려도 난수로 구분되며, 이미 있는 폴더면 ``RunManager``가 다시 만든다.

    A new run id `<slug>-YYYYMMDD-HHMMSS-<6 hex>`; `now` / `token` are for tests."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{run_id_slug(upload_name)}-{stamp}-{token or uuid.uuid4().hex[:6]}"


UPLOAD_NAME_MAX = 255                 # 기록하는 원래 파일 이름의 최대 문자 수


def display_upload_name(name: str | None) -> str | None:
    """화면에 보일 원래 파일 이름을 만든다.
    경로와 제어 문자를 빼고 ``UPLOAD_NAME_MAX``자로 자르며, 유니코드는 그대로 둔다.
    남는 것이 없으면 None을 반환한다.

    The upload name to show: basename only, control characters dropped, capped; Unicode kept; None when empty."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return base[:UPLOAD_NAME_MAX] or None


def _abort_reason_ko(reason: str) -> str | None:
    # 중단 사유를 보고서와 같은 번역표(sbmig.report.i18n.tr_line)로 한국어 문장으로 바꾼다.
    # "run aborted: <사유>" 형식을 먼저, 그다음 사유만 번역해 본다
    # 앞부분("실행 중단: " 등)은 떼고 사유만 남기며, 번역되지 않으면 None을 반환한다
    try:
        from sbmig.report import i18n
    except Exception:  # noqa: BLE001 — a missing translation table only means "no Korean"
        return None
    for line in (f"run aborted: {reason}", reason):
        try:
            out = i18n.tr_line("ko", line)
        except Exception:  # noqa: BLE001
            return None
        if out and out != line:
            if line != reason:
                out = re.sub(r"^[^:：]*(?:중단|aborted)[^:：]*[:：]\s*", "", out, count=1)
            out = out.strip()
            return out if out and out != reason else None
    return None


def _recorded_upload(rd: Path) -> str | None:
    # 메모리에 없는 실행의 원래 파일 이름을 request.json(Job 모드), web-status.json(in-process) 순으로 찾는다
    rec = RequestRecord.read(rd)
    if rec is not None:
        return rec.upload_name or rec.upload or None
    try:
        data = json.loads((rd / WEB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    up = data.get("upload") if isinstance(data, dict) else None
    return up if isinstance(up, str) and up else None


def check_flow(flow: str | None) -> str:
    """실행 방식 값을 검사해 반환한다. 없거나 비었으면 ``full``을 반환하고, ``RUN_FLOWS`` 밖의 값이면 허용
    값을 담은 ValueError를 던진다(웹은 400).

    Validate a run flow: None / "" = `full`; anything outside `RUN_FLOWS` raises a ValueError naming the accepted values."""
    if flow is None or flow == "":
        return FLOW_FULL
    if flow not in RUN_FLOWS:
        raise ValueError(f"unknown flow {flow!r} — use one of: {', '.join(RUN_FLOWS)}")
    return flow


def _abort_reason(report: dict[str, Any]) -> str | None:
    # 엔진이 실행을 중단한 사유를 찾는다.
    # Orchestrator._abort가 manual_followups에 "run aborted: <사유>"로 남기며, 첫 줄이 첫 사유다
    for item in report.get("manual_followups") or []:
        if isinstance(item, str) and item.startswith("run aborted: "):
            return item[len("run aborted: "):].strip() or None
    return None


def _recorded_flow(rd: Path) -> str:
    # 메모리에 없는 실행의 실행 방식을 request.json(Job 모드), web-status.json(in-process),
    # report.json(엔진) 순으로 찾고, 없으면 full로 본다
    rec = RequestRecord.read(rd)
    if rec is not None:
        return rec.flow or FLOW_FULL
    for name in (WEB_STATUS_FILE, "report.json"):
        try:
            data = json.loads((rd / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("flow") in RUN_FLOWS:
            return data["flow"]
    return FLOW_FULL


class RunManager:
    """이관을 작업 스레드로 시작하고, 상태와 이벤트 요청에 메모리와 실행 폴더의 정보로 응답하는 클래스.
    ``_lock``은 ``runs`` dict를 보호하고, ``_turns``는 업로드 순서대로 ``max_parallel``개(in-process는
    1개)까지 실행을 들여보낸다.

    Starts migrations in worker threads and answers status / event questions from memory + the run directory;
    `_turns` lets them in in upload order, up to `max_parallel` at a time (Job modes; one in-process)."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, *,
                 max_finished_runs: int = DEFAULT_MAX_FINISHED_RUNS, default_configs: list[str] | None = None,
                 mode: str | None = None, job_client: Any = None, job_template: str | Path | None = None,
                 max_parallel: int | None = None):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.max_finished_runs = max(0, int(max_finished_runs))
        # 실행 모드로 in-process(이 프로세스의 스레드) | k8s-job(업로드마다
        # Job 하나, 입력과 결과는 공유 PVC로 전달) 중 하나다
        self.mode = (mode or os.environ.get(MODE_ENV, "") or "in-process").strip()
        if self.mode not in MODES:
            raise ValueError(f"{MODE_ENV} must be one of {', '.join(MODES)}, not {self.mode!r}")
        # 개발 PC에서는 k8s-job으로 설정되어 있어도 클러스터 밖이면 local-job으로 실행한다.
        # 운영 파드에는 KUBERNETES_SERVICE_HOST가 항상 있으므로 바뀌지 않는다
        self.effective_mode = self.mode
        # 서비스 계정 토큰이나 KUBECONFIG 파일(/etc/kube/config) 중 하나가 있으면 실제 Job을 만든다
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
        # Kubernetes API가 이 시간 동안 응답하지 않으면 로그 error, 저널,
        # 화면에 한 번 알리지만 실행을 실패로 끝내지는 않는다
        # 파드 상태를 모르는 채로 슬롯을 넘기면 다음 이관과 겹치므로 API가 응답할 때까지 슬롯을 유지한다
        self.job_api_grace_s = max(0.0, float(os.environ.get("SBMIG_JOB_API_GRACE_S", "300") or 300))
        # job-status.json을 받은 뒤 파드 종료를 기다리는 시간이며, 넘기면 Job을 지우고 사라질 때까지 기다린다
        # Worker는 그 파일을 마지막에 기록하므로 지워도 잃는 것이 없고, 끝나지 않는 파드가 대기열을 막지 않게 된다
        self.job_exit_grace_s = max(0.0, float(os.environ.get(EXIT_GRACE_ENV, "120") or 120))
        # 파드가 끝났는데 job-status.json이 보이지 않으면 잠시 기다린다. NFS 속성 캐시(기본 최대 60초)
        # 때문에 파일이 늦게 보일 수 있으므로 이 유예 시간이 캐시보다 길어야 한다
        self.job_status_grace_s = max(0.0, float(os.environ.get(STATUS_GRACE_ENV, "90") or 90))
        # 환경 변수 고정 정책(SBMIG_JOB_ENV_PIN)의 기본값은 k8s-job에서 refuse, local-job에서 off다.
        # 로컬 Worker는 이 프로세스의 환경을 물려받으므로 .env를 고치면 잘못된 경고만 나기 때문이다
        # recover()가 Job을 만들 수 있으므로 그보다 먼저 정한다
        pin_default = contract.ENV_PIN_REFUSE if self.effective_mode == "k8s-job" else contract.ENV_PIN_OFF
        pin_raw = (os.environ.get(contract.ENV_PIN_POLICY_ENV) or "").strip()
        self.env_pin_policy = contract.env_pin_policy(pin_raw, pin_default)
        if pin_raw and pin_raw.lower() not in contract.ENV_PIN_POLICIES:
            log.warning("%s=%r is not one of %s — using the default %s", contract.ENV_PIN_POLICY_ENV, pin_raw,
                        " | ".join(contract.ENV_PIN_POLICIES), self.env_pin_policy)
        # 서버가 정한 기본 설정 파일 이름(sbmig serve --config /
        # SBMIG_WEB_CONFIGS)이며 화면에서 시작한 실행은 이 설정을 쓴다
        self._default_configs = [c.strip() for c in (default_configs or []) if c and c.strip()]
        self._missing_warned: set[str] = set()
        self.runs: dict[str, Run] = {}
        # 실행 id별로 이 프로세스가 아는 마지막 조치 재개 시도다.
        # 상태가 섞이지 않도록 이관의 runs와 따로 둔다
        self.resumes: dict[str, Run] = {}
        self._lock = threading.Lock()
        # 동시 Job 수는 인자, SBMIG_JOB_MAX_PARALLEL, 1 순으로 정한다.
        # in-process는 LLM 호출 수와 로그 필터가 프로세스에 하나뿐이므로 항상 1이다
        # 실행 모드가 정해진 뒤, recover()가 슬롯을 잡기 전에 정해야 한다
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
        # 운영(k8s-job)에서는 잘못된 템플릿, 권한, 접속 정보를 첫 업로드
        # 때가 아니라 시작할 때 거부해 앱이 뜨지 않게 한다
        if self.effective_mode == "k8s-job":
            self._startup_k8s(constructed=job_client is None)
        if self.effective_mode in ("k8s-job", "local-job"):
            self.recover()

    # ---- k8s-job startup (v2.1z-ac)
    def _startup_k8s(self, constructed: bool) -> None:
        """k8s-job 모드의 시작 검증을 한다. 클라이언트를 만들고, Worker 템플릿을 검사하고, 권한을 점검하며,
        하나라도 실패하면 ValueError를 던져 앱이 뜨지 않게 한다.
        테스트가 클라이언트를 넣었으면 생성을 건너뛰고, 템플릿은 파일이 있을 때만 마운트 규칙 없이 검사한다.

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
        """시작할 때 클러스터 권한을 점검한다. 점검 목록은 deploy/k8s/45-web-rbac.yaml과 같다.
        ``can_i``가 없는 클라이언트(가짜 구현, 로컬)는 건너뛰고, 하나라도 거부되면 막힌 권한을 담은 ValueError를 던진다.

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
        """서버 재시작 뒤 끝나지 않은 실행을 복구하고, 복구한 실행 id 목록을 반환한다.
        ``<out>/*/control/state.json``에서 찾으며, 깨진 파일은 로그만 남기고 건너뛰고 예외를 던지지 않는다.
        - k8s-job: request.json으로 ``Run``을 다시 만든다. ``queued``는 평소처럼 Job을 만들고,
          ``creating``은 같은 이름의 Job이 있으면 이어받고, ``created`` / ``running``은 이어받아 감시만 한다
        - local-job: 서브프로세스는 서버와 함께 종료되었으므로 ``lost``로 기록한다
        대기 중인 실행이 실행 중인 Job의 슬롯 번호를 가져가지 않도록, Job이 있을 수 있는 실행을 먼저 복구해
        기록된 슬롯을 잡고(``_turns.occupy``) 그다음 ``queued`` 실행을 줄에 세운다.

        Restart recovery from the control states on disk (k8s-job: re-register and watch / create in `accepted_at` order;
        local-job: mark `lost`). Runs whose Job exists or may exist re-occupy their recorded slot first, before any queued
        run joins the line. Never raises; returns `self.recovered`."""
        if self.effective_mode not in ("k8s-job", "local-job"):
            return self.recovered
        try:
            # 조치 재개 시도의 Job도 클러스터에서 실행 중일 수 있으므로 그 제어 상태도 같은 규칙으로 복구한다
            paths = sorted(self.out_dir.glob(f"*/{CONTROL_DIR}/{STATE_FILE}"))
            paths += sorted(self.out_dir.glob(f"*/{CONTROL_DIR}/{RESUME_STATE_FILE}"))
        except OSError as e:
            log.warning("recovery: cannot scan %s: %s", self.out_dir, e)
            return self.recovered
        pending: list[tuple[str, Path, ControlState, RequestRecord]] = []
        for sp in paths:
            rd = sp.parent.parent
            resume = sp.name == RESUME_STATE_FILE
            try:
                if rd.name.startswith("_") or rd.name in (self.resumes if resume else self.runs):
                    continue
                cs = ControlState.read(rd, sp.name)
                if cs is not None and resume and cs.action != ACTION_REMEDIATION_RESUME:
                    log.warning("recovery: %s does not name the action %s — skipped", sp, ACTION_REMEDIATION_RESUME)
                    continue
                if cs is None:
                    log.warning("recovery: %s is unreadable — skipped", sp)
                    continue
                if cs.terminal:
                    continue
                if cs.run_id != rd.name:
                    log.warning("recovery: %s names run %s, not its folder %s — skipped", sp, cs.run_id, rd.name)
                    continue
                if self.effective_mode == "local-job":
                    # 로컬 "파드"는 이 서버의 자식 프로세스라 서버가 종료되면 함께 종료되므로 이어받을 것이 없다
                    cs.transition(RunState.LOST, exit_code=EXIT_ABORTED, error=LOCAL_LOST).write(rd, sp.name)
                    self.recovered.append(f"{cs.run_id}#{cs.action}" if resume else cs.run_id)
                    log.warning("recovery: run %s was %s — marked lost (%s)", cs.run_id, "in progress", LOCAL_LOST)
                    continue
                rec = RequestRecord.read(rd)
                if rec is None:
                    log.warning("recovery: run %s (state %s) has no readable control/request.json — skipped", cs.run_id, cs.state)
                    continue
                if cs.state not in (RunState.QUEUED.value, RunState.CREATING.value, RunState.CREATED.value, RunState.RUNNING.value):
                    log.warning("recovery: run %s has an unknown state %r — skipped", cs.run_id, cs.state)
                    continue
                # 재개 시도는 이관의 접수 시각이 아니라 그 시도의 accepted_at 순서로 줄을 선다
                at = (cs.accepted_at or "") if resume else (rec.accepted_at or cs.accepted_at or "")
                pending.append((at, rd, cs, rec))
            except Exception as e:  # noqa: BLE001 — recovery must never keep the web app from starting
                log.warning("recovery: %s skipped (%s: %s)", sp, type(e).__name__, e)
        ordered = sorted(pending, key=lambda t: (t[0], t[1].name))
        # Job이 이미 실행 중이라 슬롯을 가진 실행을 먼저, 대기하던 실행을 그 뒤에
        # 처리하고, 각 그룹 안에서는 accepted_at 순서를 따른다
        holding = [t for t in ordered if t[2].state != RunState.QUEUED.value]
        waiting = [t for t in ordered if t[2].state == RunState.QUEUED.value]
        for _at, rd, cs, rec in holding + waiting:
            try:
                self._resume(rd, cs, rec)
            except Exception as e:  # noqa: BLE001 — recovery must never keep the web app from starting
                log.warning("recovery: run %s not resumed (%s: %s)", cs.run_id, type(e).__name__, e)
        return self.recovered

    def _resume(self, rd: Path, cs: ControlState, rec: RequestRecord) -> None:
        """복구한 실행 하나를 메모리에 다시 등록하고 감시 스레드를 시작한다.
        ``queued`` 실행은 줄 끝에 서고, Job이 있을 수 있는 실행은 기록된 슬롯을 한도와 상관없이 바로
        잡는다(``occupy``). ``slot``이 없는 state.json은 1번 슬롯으로 본다.

        Re-register one run: a queued one joins the line; one whose Job exists or may exist re-occupies its recorded slot
        now, whatever the limit (no `slot` on disk = slot 1)."""
        mode = {RunState.QUEUED.value: None, RunState.CREATING.value: "probe"}.get(cs.state, "adopt")
        action = cs.action or None
        run = Run(rd.name, rd, rd / SOURCE_ZIP, list(rec.configs or []), rec.stop_after, rec.llm_provider, job=True,
                  request=rec, control=cs, recover=mode, action=action, flow=rec.flow or FLOW_FULL,
                  upload=rec.upload_name or rec.upload)
        if action:                             # 재개 시도의 Job 이름은 시도 번호로 처음부터 정해진다
            run.job_name = cs.job or resume_job_name(run.run_id, run.attempt)
        elif cs.state != RunState.QUEUED.value:
            run.job_name = cs.job or job_name(run.run_id)
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-job-{run.key}", daemon=True)
        table = self.resumes if action else self.runs
        with self._lock:
            table[run.run_id] = run
            if mode is None:
                self._turns.join(run.key)
            else:
                # 클러스터의 Job은 이미 실행 중이므로, 새 업로드가 같은 번호의 Maven 로컬 저장소를 받지
                # 않도록 슬롯을 차지한다. probe가 Job을 직접 만들면 이 번호를 creating 상태에 기록한다
                run.slot = self._turns.occupy(run.key, prefer=cs.slot or 1)
        try:
            run.thread.start()
        except RuntimeError:
            self._turns.leave(run.key)
            with self._lock:
                table.pop(run.run_id, None)
            raise
        self.recovered.append(run.key)
        log.info("recovery: run %s resumed from state %s (job %s, attempt %d%s) — %s", run.run_id, cs.state, run.job_name or "-",
                 run.attempt, f", slot {run.slot}" if run.slot is not None else "",
                 {None: "will create its job", "probe": "adopts the job if it exists, else creates it",
                  "adopt": "adopts the job and watches it"}[mode])

    # ---- paths
    def run_dir(self, run_id: str) -> Path:
        """실행 폴더 ``<out>/<run id>``의 경로를 반환한다.
        호출하는 라우트가 먼저 ``RUN_ID_RE``로 id를 검사한다."""
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        """화면에서 고를 수 있는 ``config/*.yml`` 이름 목록을 반환한다.
        ``default.yml``은 항상 먼저 읽히므로 목록에서 뺀다."""
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # 아무것도 지정하지 않았을 때 쓰는 이름이며, 회사 PC의 artifacts.yml,
    # 개발 PC의 로컬 e2e 설정 순으로 먼저 있는 파일을 쓴다
    FALLBACK_CONFIGS = ("artifacts.yml", "artifacts.local-e2e.yml")

    def default_configs(self) -> list[str]:
        """이번 실행에 쓸 기본 설정 파일 이름 목록을 반환한다. 이 파일들은 ``config/`` 안에 있고 default.yml
        다음에 읽히며, 화면은 사용자에게 고르게 하지 않고 이 목록을 쓴다.
        ``sbmig serve --config`` / ``SBMIG_WEB_CONFIGS``의 이름 중 지금
        있는 파일만 쓰고, 없는 이름은 한 번 경고한 뒤 뺀다.
        지정된 이름이 하나도 없으면 :attr:`FALLBACK_CONFIGS` 중 처음 있는 파일을, 그것도 없으면 빈 목록을 반환한다.

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
        """받는 중인 업로드 본문을 기록할 새 임시 파일 경로 ``<out>/_uploads/.incoming-<hex>.part``를
        만든다. 최종 zip과 같은 폴더이므로 ``start_file``이 복사 없이 이름만 바꾼다.

        A fresh `<out>/_uploads/.incoming-<hex>.part` for a body that is still arriving (same directory as the
        final `<run id>-<name>` so `start_file` can rename it without a copy)."""
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f".incoming-{uuid.uuid4().hex}.part"

    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: str | None = None,
              llm_provider: str | None = None, flow: str | None = None) -> str:
        """메모리에 있는 zip 본문으로 실행을 시작한다.
        테스트나 다른 코드에서 쓰며 ``start_file``과 같이 동작하고, 잘못된 요청이면 ValueError를 던진다.

        `start_file` for a body already in memory (tests, embedding callers). ValueError = bad request."""
        tmp = self.incoming_path()
        tmp.write_bytes(zip_bytes)
        try:
            return self.start_file(tmp, name, configs, stop_after, llm_provider, flow)
        finally:
            tmp.unlink(missing_ok=True)

    def start_file(self, incoming: Path, name: str, configs: list[str], stop_after: str | None = None,
                   llm_provider: str | None = None, flow: str | None = None) -> str:
        """업로드된 zip으로 새 실행을 시작하고 실행 id를 반환한다. 입력 검사 -> zip을 실행 id 이름으로 이동
        -> 설정, 워크스페이스, 오케스트레이터 생성 -> 작업 스레드 시작 순으로 처리한다.
        ValueError가 나면 ``incoming`` 파일은 호출한 쪽이 지우고, 이동한 뒤에는 실행이 관리한다.
        zip이 깨져 워크스페이스를 만들지 못하면 실행 폴더와 zip을 남기지 않는다.

        Validate, move the uploaded zip (`incoming`, written by the caller) under its run id, build config +
        workspace + orchestrator, start the worker. ValueError = bad request — `incoming` is then left to the caller
        (the web route deletes it); after the move it belongs to the run."""
        from sbmig.core.config import load_config
        from sbmig.core.workspace import Workspace
        from sbmig.loop import Orchestrator, parse_stop_after

        # ``../``로 config 폴더 밖의 파일을 읽지 못하도록 설정 이름도 RUN_ID_RE로 검사한다
        for c in configs:
            if not RUN_ID_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        phase = parse_stop_after(stop_after)                  # ValueError names the accepted values
        stop_after = phase.value if phase is not None else None
        flow = check_flow(flow)                               # v2.1z-aj; ValueError names the accepted values
        if flow == FLOW_REMEDIATION and stop_after:
            # 조치만 하는 실행에는 중지할 이관 단계가 없으므로 거부하며, CLI도 --stop-after와 --flow
            # remediation을 함께 받지 않는다
            raise ValueError("stop_after cannot be combined with flow=remediation (that flow runs no migration phase)")
        overrides: dict[str, Any] = {}
        if llm_provider:
            overrides["llm"] = {"provider": llm_provider}
        cfg = load_config(*[self.repo_root / "config" / c for c in configs], overrides=overrides or None,
                          repo_root=self.repo_root)        # pydantic ValidationError is a ValueError

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        shown = display_upload_name(name)                     # 화면에 보여 줄, 사용자가 올린 이름 그대로의 값
        run_id = self._claim_run_id(name)
        if self.effective_mode in ("k8s-job", "local-job"):
            return self._start_job(incoming, run_id, safe, list(configs), stop_after, llm_provider, flow, shown)
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
        run = Run(run_id, ws.run_dir, zip_path, list(configs), stop_after, llm_provider, flow=flow, upload=shown)
        run.orch = Orchestrator(cfg, ws)
        # journal만 보고도 어떤 설정으로 실행했는지 알 수 있도록 실행 폴더에 처음으로 기록한다
        try:
            ws.journal({"event": "web_run", "configs": list(configs), "stop_after": stop_after, "llm_provider": llm_provider,
                        "flow": flow, "upload": shown})
        except OSError as e:                   # the journal line is a convenience; never refuse the run on it
            log.debug("run %s: web_run journal line not written: %s", run_id, e)
        # daemon 스레드라 서버 프로세스가 끝나면 함께 끝나므로, 정리는 shutdown의 중지 요청과 Maven 종료가 맡는다
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-run-{run_id}", daemon=True)
        with self._lock:
            self.runs[run_id] = run
            # 스레드가 실제로 시작되는 순서는 보장되지 않으므로 대기열 순서는 업로드를 받은 지금 정한다
            self._turns.join(run_id)           # the place in line is taken here, in upload order (A10)
        try:
            run.thread.start()
        except RuntimeError:                   # no thread started: leave the line, or every later run waits
            self._turns.leave(run_id)
            with self._lock:
                self.runs.pop(run_id, None)
            raise
        log.info("run %s started: %s configs=%s stop_after=%s llm_provider=%s flow=%s", run_id,
                 zip_path.name, ",".join(configs) or "-", stop_after or "-", llm_provider or "-", flow)
        return run_id

    def _claim_run_id(self, upload_name: str) -> str:
        """새 실행 id를 만들고 실행 폴더를 배타적으로 생성한다.
        폴더가 이미 있거나 메모리에 같은 id가 있으면 id를 다시 만든다.

        Draw a run id and create its run directory exclusively (an existing directory or an id in memory → draw again)."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            rid = run_id_for(upload_name)
            if rid in self.runs or not RUN_ID_RE.match(rid):
                continue
            try:
                (self.out_dir / rid).mkdir()
            except FileExistsError:
                continue
            return rid
        raise OSError("no free run id after 100 tries")

    def _start_job(self, incoming: Path, run_id: str, safe_name: str, configs: list[str], stop_after: str | None,
                   llm_provider: str | None, flow: str = FLOW_FULL, upload_name: str | None = None) -> str:
        """k8s Job 모드에서 실행을 시작한다. 실행 폴더를 만들고 zip을 ``source.zip``으로 옮긴 뒤 첫 저널
        줄을 기록하고, Job을 만들어 감시할 작업 스레드를 시작한다.
        이 프로세스는 이관을 직접 실행하지 않는다.

        Job mode: create the run dir, move the zip there as `source.zip` (both on the shared volume), write the first
        journal line, start the worker that creates and watches the Job. Nothing migrates in this process."""
        run_dir = self.out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        zip_path = run_dir / SOURCE_ZIP
        Path(incoming).replace(zip_path)
        # 이 프로세스가 곧바로 죽어도 다음 Main이 같은 요청을 이어받도록 고정 입력(request.json)과 queued
        # 제어 상태를 작업 스레드보다 먼저 기록한다
        try:
            request = RequestRecord(run_id=run_id, accepted_at=utc_now(), upload=safe_name, configs=list(configs),
                                    zip_sha256=sha256_file(zip_path), config_digests=config_digests(self.repo_root, configs),
                                    sbmig_version=sbmig_version(), mode=self.effective_mode, stop_after=stop_after,
                                    llm_provider=llm_provider, attempt=1, flow=flow, upload_name=upload_name)
            request.write(run_dir)
            control = ControlState(run_id=run_id, state=RunState.QUEUED.value, attempt=request.attempt,
                                   accepted_at=request.accepted_at)
            control.write(run_dir)
        except OSError:                        # no run was started: leave no half-accepted run dir behind
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        run = Run(run_id, run_dir, zip_path, configs, stop_after, llm_provider, job=True, request=request, control=control,
                  flow=flow, upload=upload_name or safe_name)
        _append_journal(run_dir, {"event": "web_run", "run_id": run_id, "mode": self.effective_mode, "upload": safe_name,
                                  "configs": configs, "stop_after": stop_after, "llm_provider": llm_provider, "flow": flow,
                                  "upload_name": upload_name})
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
        log.info("run %s queued as a %s: %s configs=%s stop_after=%s flow=%s", run_id,
                 "k8s Job" if self.effective_mode == "k8s-job" else "local job (development)", safe_name,
                 ",".join(configs) or "-", stop_after or "-", flow)
        return run_id

    def _worker(self, run: Run) -> None:
        """실행 하나를 처리하는 작업 스레드의 본체.
        줄 맨 앞이 되고 빈 슬롯이 생길 때까지 ``queued``로 기다리고, 차례가 오면 ``active``가 된다.
        Job 실행은 슬롯 번호를 ``run.slot``에 둔다.
        이 스레드 전용 ``sbmig.log``를 붙이고 이관을 실행한 뒤 ``SBMIG-RESULT`` 줄을 남긴다. 어떤 경우에도 마지막에
        로그를 떼고, ``finished`` 이벤트와 web-status.json을 기록하고, ``done``을 세워 SSE를 깨운다.

        The worker thread: `queued` until first in line with a free slot, then `active` (a Job run keeps its slot number in
        `run.slot`); whatever happens, the finally block writes the end of the run and wakes the streams."""
        log_path: Path | None = None
        settled = False                        # a Job run's terminal control state was written (or tried) inside its turn
        try:
            # 여기서 실패해도 finally가 줄에서 빼야 뒤의 실행이 계속 기다리지 않으므로 import도 try 안에서 한다
            from sbmig.cli import attach_file_log, run_exit_code

            # Job 모드는 max_parallel개까지, in-process는 1개만 실행하며,
            # 차례는 start_file에서 정한 업로드 순서를 따른다
            try:
                with self._turns.turn(run.key) as slot:  # upload order; up to max_parallel Jobs at a time (docstring)
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
                            # 종료 상태(control/state.json)는 슬롯을 가진 채로 기록한다. 슬롯을 먼저 비우면 그 사이 다음
                            # 실행이 같은 번호를 기록해, 재시작 뒤 두 상태가 한 번호를 갖게 되기 때문이다
                            # 기록이 실패해도 로그만 남기고, 블록을 벗어나면서 슬롯은 비운다
                            settled = True
                            self._close_job_run(run)
                        return
                    # 다른 실행이나 서버 로그가 섞이지 않도록 이 스레드와, 컨텍스트를 물려받은 LangGraph
                    # 실행기 스레드의 로그만 이 파일에 기록한다
                    log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
                    # 어떤 설정으로 실행했는지 남기기 위해 이 실행의 sbmig.log 첫 줄을 기록한다
                    log.info("run %s: configs=%s stop_after=%s llm_provider=%s flow=%s", run.run_id,
                             ",".join(run.configs) or "-", run.stop_after or "-", run.llm_provider or "-", run.flow)
                    report = self._run_thread_body(run)
                    run.report = report
                    run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
                    if report is not None:          # the line operators retype (round 10), also in this run's sbmig.log
                        from sbmig.report.result_line import report_result_line
                        log.info("%s", report_result_line(report, run.exit_code, run_id=run.run_id))
            except _LeftLine:
                # 대기하던 Job 실행이 중지 요청으로 줄에서 빠진 경우다.
                # 차례가 와서 cancel을 확인한 경우와 똑같이 Job 없이 끝낸다
                log.info("run %s: stopped while waiting — left the line", run.run_id)
                run.exit_code = self._stopped_before_create(run)
        except Exception as e:  # noqa: BLE001 — the orchestrator never raises; a crash here is a bug (logged in _record_crash)
            self._record_crash(run, e)
        finally:
            self._turns.leave(run.key)         # already left after a turn; this covers a failure before it
            run.active.set()
            log.info("run %s%s finished: exit code %s", run.run_id, f" ({run.action})" if run.action else "", run.exit_code)
            if log_path is not None:
                _detach_file_log(log_path)
            if run.job:
                if not settled:                # no slot was taken (left the line, or a failure before the turn)
                    self._close_job_run(run)
                success = run.success
            else:
                # 출력 마스킹이 설치되어 있으면 done 전에 마지막 마스킹을 실행한다.
                # 그 전까지는 file_withheld가 파일을 내주지 않는다
                if output_scrubbing():
                    self._final_redaction(run)
                success = getattr(run.report, "success", None)
                run.success = success
            self._push(run, "finished", {"exit_code": run.exit_code, "success": success})
            # web-status.json은 이관의 결과이므로 재개 시도에서는 기록하지 않는다
            if not run.keep_state and not run.action:   # a Job left running at shutdown: its state.json speaks for it
                _write_web_status(run)
            run.done.set()
            with run.cond:
                run.cond.notify_all()
            with self._lock:
                self._prune_locked()

    def _record_crash(self, run: Run, e: BaseException) -> None:
        # 작업 스레드 안에서 예상하지 못한 예외는 엔진이나 이 모듈의 버그이므로 실행을 실패로 기록하고 화면에 알린다
        log.exception("run %s failed", run.run_id)
        run.error = scrub(f"{type(e).__name__}: {e}")
        run.exit_code = EXIT_ABORTED
        if run.job:
            run.final_state = run.final_state or RunState.FAILED.value
        self._push(run, "error", {"message": run.error})

    def _final_redaction(self, run: Run) -> None:
        # 기록할 때 마스킹되지 않는 파일(Maven 출력 logs/, 보고서)을 마지막으로 마스킹한다.
        # 실패해도 로그만 남기고 계속한다
        try:
            from sbmig.web.redact import redact_run_dir
            redact_run_dir(run.run_dir)
        except Exception:  # noqa: BLE001
            log.exception("run %s: the final output redaction failed", run.run_id)

    def _close_job_run(self, run: Run) -> None:
        """Job 실행을 마무리한다.
        필요하면 마지막 마스킹을 하고, 종료 상태를 기록(``_settle_job_run``)한 뒤 ``run.success``를 정한다.
        슬롯을 비우기 전에 호출하며 예외를 던지지 않는다.
        결과 파일 없이 끝난 Job은 Worker의 마스킹을 거치지 않았을 수 있으므로 Main이 대신 마스킹한다.
        Job 종료를 확인했을 때만 하고, 서버 종료로 남겨 두는 Job이나 우리 것이 아닌 Job은 건드리지 않는다.

        Close a Job run: the final redaction when Main must do it, then the terminal control state (`_settle_job_run`);
        sets `run.success`. Called before the slot is freed; never raises."""
        try:
            if run.verified is not True and run.job_phase in TERMINAL_PHASES and not run.keep_state:
                self._final_redaction(run)
            run.success = self._settle_job_run(run)
        except Exception:  # the slot must still be freed by the caller's block
            log.exception("run %s: settling the job run failed", run.run_id)

    def _stopped_before_create(self, run: Run) -> int:
        # 대기 중에 중지되면 Job을 만들지 않고 stopped로 끝낸다.
        # 차례가 와서 확인한 경우와 줄에서 빠진 경우 모두 같다
        run.error = "stopped before the job was created"
        run.final_state = RunState.STOPPED.value
        return EXIT_ABORTED

    def _job_client_or_fail(self) -> Any:
        """Job 클라이언트를 반환한다.
        여러 감시 스레드가 동시에 호출할 수 있으므로 ``_lock`` 안에서 처음 한 번만 만든다.
        따로 만들면 로컬 클라이언트의 프로세스 목록이 나뉘기 때문이다.

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
        # 끝나지 않은 Job이 무엇을 막고 있는지 알리는 문구다.
        # 슬롯이 하나면 다음 이관이 기다리고, 여럿이면 그 슬롯 하나만 막힌다
        if self.max_parallel <= 1:
            return "its turn", ", the next migration waits"
        return (f"its slot {run.slot}" if run.slot is not None else "its slot"), ""

    def _settle_job_run(self, run: Run) -> bool | None:
        """Job 실행의 결과를 확정한다. 이 실행과 시도의 ``job-status.json``을 받았거나 컨테이너 종료 코드와
        ``report.json``이 있을 때만 report.json의 success를 읽는다.
        그렇지 않으면 success는 None, 오류는 "result not verified"가 된다.
        제어 상태를 종료 상태로 기록하되, 서버 종료로 Job을 남겨 둘 때는 그대로 둔다.

        Settle a job run: success only from a verified result; write the terminal control state (left as is at shutdown)."""
        if run.action:
            # 재개 시도에서는 이관의 report.json이 이 시도의 결과가 아니므로 결과 파일이나 컨테이너 종료
            # 코드만 본다. 조치 결과는 remediation/state.json에 있고 이관의 success는 바뀌지 않는다
            trusted = bool(run.verified) or run.result_source == "container"
            success = None
        else:
            has_report = (run.run_dir / "report.json").is_file()
            trusted = bool(run.verified) or (run.result_source == "container" and has_report)
            success = _report_success(run.run_dir) if trusted else None
        expected_result = not run.keep_state and run.final_state not in (RunState.STOPPED.value, RunState.FAILED.value)
        if not trusted and expected_result:
            note = "result not verified"
            run.error = (f"{run.error} ({note})" if run.error
                         else f"{note}: no {run.status_file} for run {run.run_id} attempt {run.attempt}"
                              + ("" if run.action else " and no report.json"))
        if run.control is not None and not run.keep_state and not run.control.terminal:
            final = run.final_state or (RunState.FINISHED.value if trusted else RunState.LOST.value)
            try:
                self._transition(run, final, exit_code=run.exit_code, error=run.error)
            except OSError as e:               # the in-memory status still answers; the next Main sees the last state written
                log.error("run %s: control state %s not written: %s", run.run_id, final, e)
        return success

    def _transition(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """제어 상태를 바꾸고 바로 ``control/state.json``에 원자적으로 기록한다.
        OSError는 호출하는 쪽이 처리하며, creating 상태를 기록하지 못하면 Job을 만들지 않는다. / Transition the control state and write it atomically."""
        if run.control is None:                # a run built without intake records (embedding callers): nothing to persist
            run.control = ControlState(run_id=run.run_id, action=run.action)
        cs = run.control.transition(state, **changes)
        # 이관 시도는 기본 파일(state.json)에, 재개 시도는 자기 파일에 기록한다
        cs.write(run.run_dir, run.control_file) if run.action else cs.write(run.run_dir)

    def _owned(self, run: Run, st: JobState) -> bool:
        """이 Job이 이 요청의 것인지 확인한다.
        ``sbmig-owner``와 ``sbmig-run-id`` 라벨이 둘 다 이 요청의 값이어야 한다.
        라벨이 없는 Job은 우리 것이 아니므로 이어받지도 지우지도 않는다. 라벨을 알 수 없는
        클라이언트(``labels is None``: 로컬 서브프로세스, 테스트용 가짜 구현)일 때만 이름으로 판단한다.

        Is this Job this request's: both labels must match. An unlabelled Job is NOT ours (review 2026-11-10); only a client
        that cannot report labels at all (`labels is None`: the local subprocess, test fakes) is trusted by name."""
        labels = getattr(st, "labels", None)
        if labels is None:
            return True
        return labels.get(OWNER_LABEL) == run.owner and labels.get(RUN_LABEL) == run.run_id

    def _accept_status(self, run: Run) -> int | None:
        """``job-status.json``이 이 실행과 시도의 것이면 종료 코드를 반환하고 verified를 설정한다.
        아니면 None을 반환한다.
        다른 실행이나 이전 시도의 파일은 그 결과를 현재 결과로 읽지 않도록 한 번 경고하고 무시한다.

        The Worker's exit code when `job-status.json` matches this run and attempt; a foreign file is ignored (one warning)."""
        status = JobStatus.read(run.run_dir, run.status_file) if run.action else JobStatus.read(run.run_dir)
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
        """k8s Job 모드에서 실행 하나를 처리한다.
        Job을 만들거나 재시작 복구 때 이어받은 뒤 ``_watch_job``을 호출하고 종료 코드를 반환한다.
        - 만들기 전에 ``creating`` 상태와 Job 이름을 기록한다.
          응답이 409이거나 전송 오류면 같은 이름의 Job을 찾아 이어받는다
        - 대기 중에 중지 요청이나 서버 종료가 오면 Job을 만들지 않는다
        - 이 함수가 반환해야 다음 차례로 넘어가므로 Job 종료를 API로 확인한 뒤에만 반환한다.
          실패할 때도 우리 Job을 지우고 사라질 때까지 기다리며, 서버 종료와 우리 것이 아닌 Job만 예외다

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
                # 이름만으로 이어받지 않는다. 같은 이름의 Job이 있으면 라벨이 이 요청의 것이어야 하고,
                # 없으면 끝나서 지워진 Job으로 보고 볼륨의 결과 파일로 판단한다
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
                # 생성을 시도했거나 이어받은 Job이 있을 수 있으므로, 우리 것이면
                # 지우고 끝난 것을 확인한 뒤에 차례를 넘긴다
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
        if run.action:                         # 2026-11-11 wave 4: the Worker runs this action instead of `sbmig run`
            env[contract.ACTION_ENV] = run.action
        # Worker가 request.json의 flow와 비교하므로 full이어도 항상 기록한다.
        # Worker는 비어 있으면 full로 보므로 이전 Job과도 맞는다
        env[contract.FLOW_ENV] = run.flow or FLOW_FULL
        return env

    def _env_pins(self, run: Run) -> dict[str, str] | None:
        """Job을 만드는 시점의 이 프로세스 환경 해시(``contract.env_pins``)를 반환한다.
        비밀로 보이는 이름은 포함하지 않는다.
        정책이 off면 None을 반환한다. 계산이 실패해도 Job 생성을 막지 않도록 경고만 남기고 None을 반환하며,
        읽히지 않는 설정 파일은 Worker가 설정 파일 해시 비교로 잡아낸다.

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
        """템플릿에 owner 라벨까지 채워 매니페스트를 만들고, ``creating`` 상태를 기록한 뒤 Job을 만든다.
        Job을 만드는 곳은 이 함수뿐이다.
        ``creating`` 상태에는 Worker가 비교할 수 있도록 이 Main의 ``env_pins``와 ``env_pin_policy``도 기록한다.
        응답이 409이거나 전송 오류면 API가 응답할 때까지 같은 이름의 Job을 찾아 우리 것이면 이어받고, 없으면
        원래 오류를 다시 던진다. 성공하면 상태는 ``created``가 된다.

        Render, record `creating` together with this Main's `env_pins` / `env_pin_policy` (the Worker compares them with its
        own environment; this is the only place a Job is created), create; an uncertain answer asks (until the API answers)
        for a Job of that name and adopts ours, else re-raises the original error."""
        template: Any = self._job_template_doc if self._job_template_doc is not None else self.job_template
        if self._job_template_doc is None and not Path(self.job_template).is_file():
            if self.effective_mode == "k8s-job":
                # 시작 검증을 건너뛴 경우(테스트 클라이언트)에만 여기에 온다.
                # 빈 템플릿으로 Job을 만들지 않도록 예외를 던진다
                raise K8sError(f"job template not found: {self.job_template} — mount the ConfigMap sbmig-job-template at /templates "
                               f"(deploy/README.md §3) or point {TEMPLATE_ENV} at the filled template")
            # the local "pod" needs only the env: a minimal manifest stands in for a template the checkout may lack
            template = {"kind": "Job", "metadata": {}, "spec": {"template": {"spec": {"containers": [{"name": "build"}]}}}}
        manifest = render_job(template, run.run_id, self._job_env(run), getattr(client, "namespace", None), owner=run.owner)
        # 재개 시도의 Job은 이름이 <이관 Job>-rem<시도 번호>로 다르며, 라벨은 render_job이 붙인 그대로 쓴다
        manifest.setdefault("metadata", {})["name"] = name
        # 프로세스가 여기서 죽어도 다음 Main이 creating 상태와 이름으로 Job을 찾을 수 있도록 생성 의도를
        # 먼저 기록한다. 환경 해시는 접수한 Main이 아니라 Job을 만드는 이 Main의 현재 환경으로 만든다
        pins = self._env_pins(run)
        # 재시작한 Main이 같은 번호를 다시 잡을 수 있도록 슬롯 번호도 같은 상태 기록에 넣는다
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
            # 응답이 불확실하면 API가 응답할 때까지 Job이 있는지 조회한다.
            # 모르는 채로 실패 처리하면 이미 만들어진 파드를 둔 채 차례를 넘기게 된다
            # 끊긴 요청이 늦게 전달되어 Job이 생길 수 있으므로 보이지 않아도 몇 번 더 조회한다
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
                                      "namespace": getattr(client, "namespace", None), "slot": run.slot,
                                      **({"action": run.action} if run.action else {})})
        self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name}})

    def _poll_job(self, run: Run, client: Any, name: str) -> JobState | None:
        """Job 상태를 한 번 조회해 반환한다. API가 응답하지 않으면 None을 반환하며, 파드 상태를 모르는 채로
        차례를 넘기면 다음 이관과 겹치므로 실행을 실패로 끝내지 않는다.
        ``job_api_grace_s``를 넘기면 로그 error, 저널 ``job_api_unavailable``, 화면 ``job.note``로 한 번
        알리고, 이후 ``API_REMIND_S``마다 로그를 남긴다. 다시 응답하면 저널에 ``job_api_back``을 기록한다.

        One state poll; None while the API does not answer — reported once past `job_api_grace_s`, never a failure. ANY
        exception of the client counts as "API unavailable" (review 2026-11-10: a 200 with a non-JSON body from a proxy
        raised ValueError past this handler, failed the run and freed the slot while the Job ran)."""
        try:
            st = client.job_state(name)
        except Exception as e:  # noqa: BLE001 - 어떤 예외든 API 무응답으로 본다
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
        """API가 응답할 때까지 Job 상태를 조회해 반환한다.
        Job이 있는지 모르는 채로 차례를 넘기지 않기 위해 기한 없이 기다린다.
        기다리는 중에 서버가 종료되면 ``_ServerClosing``을 던진다.

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
        """재시작 복구에서 같은 이름의 Job이 있는지 API가 응답할 때까지 조회한다.
        Job이 있는데 라벨이 이 요청의 것이 아니면(라벨이 없는 경우 포함) 예외를 던진다.

        Recovery: does a Job of that name exist (asked until the API answers)? One that is not this request's raises."""
        st = self._state_when_answered(run, client, name)
        if st.phase == "Missing":
            return False
        if not self._owned(run, st):
            raise K8sError(f"job {name} exists but is not owned by this request — not adopted")
        return True

    def _transition_soft(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """Job이 이미 있을 때 제어 상태를 기록한다. 기록 실패로 감시를 멈추면 실행 중인 파드를 둔 채 차례를
        넘기게 되므로, 파일을 쓰지 못해도 로그만 남기고 예외를 던지지 않는다.
        이 경우 메모리 상태는 바뀌고 디스크에는 직전 상태가 남아, 다음 Main이 그 상태로 이어받는다.

        A control-state write once the Job exists: an `OSError` is logged, never raised (the Job must be watched on)."""
        try:
            self._transition(run, state, **changes)
        except OSError as e:
            log.error("run %s: control state %s not written (%s) — the job is watched on", run.run_id,
                      state.value if isinstance(state, RunState) else state, e)

    def _adopt(self, run: Run, name: str, reason: str) -> None:
        """이미 있는 Job을 이 실행의 것으로 이어받는다. 저널에 ``job_adopted``를 남기고, 제어 상태를 queued
        / creating에서 created로 바꾸고, '파드' 단계 이벤트를 보낸다.

        Adopt an existing Job: journal `job_adopted`, control state → `created` (a `running` state stays), a `job` node event."""
        run.job_name = name
        _append_journal(run.run_dir, {"event": "job_adopted", "run_id": run.run_id, "job": name, "attempt": run.attempt,
                                      "reason": reason, **({"action": run.action} if run.action else {})})
        log.info("run %s: adopted job %s (%s)", run.run_id, name, reason)
        if run.control is None or run.control.state in (RunState.QUEUED.value, RunState.CREATING.value):
            self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name, "adopted": True}})

    def _hold_until_ended(self, run: Run, client: Any, name: str, *, delete_after: float | None, why: str) -> bool:
        """Job과 파드가 끝났거나 사라진 것을 API로 확인할 때까지 슬롯을 놓지 않고 기다린다.
        그 슬롯의 Maven 로컬 저장소를 다음 이관과 함께 쓰지 않게 하기 위해서다.
        ``delete_after``초가 지나면 우리 라벨의 Job을 지우고 기한 없이 기다린다.
        종료를 확인하면 True를, 서버 종료나 다른 요청의 Job이라 확인 없이 빠지면 False를 반환한다.

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
                    except Exception as e:  # noqa: BLE001 - 어떤 실패든 다시 시도하며 종료 확인 전에는 빠지지 않는다
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
        """Job이 끝날 때까지 감시하고 종료 코드를 반환한다. 생성 직후와 이어받은 뒤에 모두 쓴다.
        - 파드가 Running이 되면 ``running``으로 바꾸고, 끝나면 ``job-status.json``이나 컨테이너 종료
          코드(확인되지 않은 결과)를 쓰며, Job이 사라졌으면 ``lost``로 끝낸다
        - 중지 요청이 오면 ``stop.request``를 기록하고, 유예 시간이 지나면 우리 라벨의 Job만 지운다
        - Job 종료를 확인한 뒤에만 반환하며, 서버 종료와 다른 요청의 Job만 예외다

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
                    # Failed는 아직 화면에 보내지 않는다. Worker가 sbmig의 종료 코드 3을 그대로 내면 Job은
                    # Failed지만 파드는 정상 동작한 것이므로 결과 파일을 기다린 뒤(_await_status) 판단한다
                    if st.phase != "Failed":
                        self._push(run, "node", {"node": "job", "update": {"state": st.phase.lower(), "job": name,
                                                                            "pod": st.pod, "message": st.message}})
                    log.info("run %s: job %s %s%s%s", run.run_id, name, st.phase, f" pod {st.pod}" if st.pod else "",
                             f" ({st.message})" if st.message else "")
                if st.phase == "Running" and run.control is not None and run.control.state in (
                        RunState.QUEUED.value, RunState.CREATING.value, RunState.CREATED.value):
                    self._transition_soft(run, RunState.RUNNING, job=name)
                if st.phase in ("Succeeded", "Failed") and not st.finished:
                    # Job은 끝났다고 하지만 파드가 아직 실행 중이거나 종료 유예 중이라 Maven이 계속 파일을 쓸 수 있다.
                    # 파드가 끝날 때까지 슬롯을 유지하고, SBMIG_JOB_EXIT_GRACE_S가 지나면 우리 Job을 지우고 기다린다
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
                        # 서버 종료 중에는 결과 파일을 기다리지 않는다.
                        # 제어 상태를 그대로 두면 다음 Main이 이어받아 읽는다
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
                            # 중지를 요청한 실행의 Job이 사라진 경우다. 이전 Main이 지우다가 종료되었거나
                            # 밖에서 지운 것이므로 lost가 아니라 stopped로 기록한다
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
                        # 같은 이름이라도 라벨이 다르면 다른 요청의 Job이므로 지우지 않는다
                        run.error = f"job {name} is not owned by this request — not deleted"
                        log.error("run %s: %s (labels %s)", run.run_id, run.error, st.labels)
                        _append_journal(run.run_dir, {"event": "job_delete_refused", "run_id": run.run_id, "job": name})
                        return EXIT_ABORTED
                    log.warning("run %s: job %s still running %.0fs after the stop request — deleting it",
                                run.run_id, name, self.job_stop_grace_s)
                    run.error = "stopped: the job was deleted after the grace period"
                    # 파드가 아직 볼륨에 쓰는 동안 다음 이관을 시작하지 않도록,
                    # Job을 지운 뒤 사라질 때까지 기한 없이 기다린다
                    self._hold_until_ended(run, client, name, delete_after=0.0, why="the stop grace period is over")
                    return EXIT_ABORTED
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _close_pod_step(self, run: Run, name: str, *, code: int | None = None, ended: JobState | None = None) -> None:
        """'파드' 단계를 닫는 이벤트를 이 감시에서 한 번만 보낸다.
        '파드' 단계는 파드가 제 역할을 했는지를 나타내며, 이관의 종료 코드와는 다르다.
        ``code``를 주면 이 실행과 시도의 ``job-status.json``을 받은 것이므로, ``error``가 없으면 종료 코드와
        상관없이 ``completed``를, 있으면 ``failed``와 그 오류를 보낸다.
        ``ended``를 주면 결과 파일 없이 Job이 Failed로 끝난 것이므로 파드가 죽었다고 보고 ``failed``와
        쿠버네티스 메시지를 보낸다.

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
        """Job이 Failed로 끝났지만 확인된 결과 파일의 종료 코드로 설명될 때 로그 한 줄을 남긴다.
        Worker가 sbmig 종료 코드 3을 그대로 내므로, 운영자가 파드 실패로 오해하지 않게 하기 위해서다.

        One log line when a verified result explains a `Failed` Job phase (the Worker's own exit code, not a pod failure)."""
        if run.job_phase == "Failed" and run.verified and not run.status_error:
            log.info("run %s: job %s: Failed is the Worker's exit code %s (job-status.json verified) — not a pod failure",
                     run.run_id, name, code)

    def _await_status(self, run: Run, name: str, st: JobState) -> int | None:
        """Job이 끝난 직후 이 실행과 시도의 ``job-status.json``이 보일 때까지 ``job_status_grace_s``(기본
        90초) 동안 다시 읽고, 찾으면 종료 코드를 반환한다.
        NFS 속성 캐시가 새 파일을 최대 1분 늦게 보여 줄 수 있기 때문이다.
        Missing(지워진 Job)이면 기다리지 않는다.

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
        """끝난 실행이 ``max_finished_runs``개를 넘으면 오래된 것부터 메모리에서 뺀다.
        호출하는 쪽이 ``self._lock``을 잡고 있어야 한다.
        실행 중이거나 대기 중인 실행은 남기며, 빠진 실행은 실행 폴더의 파일로 응답한다.

        Drop the oldest finished runs beyond `max_finished_runs` from memory (caller holds `self._lock`). Running
        and queued runs stay; evicted runs are answered from their run directory."""
        finished = sorted((r for r in self.runs.values() if r.done.is_set()), key=lambda r: r.started)
        excess = len(finished) - self.max_finished_runs
        for r in finished[:max(0, excess)]:
            self.runs.pop(r.run_id, None)
            log.debug("run %s evicted from memory (max_finished_runs=%d)", r.run_id, self.max_finished_runs)

    def _run_thread_body(self, run: Run) -> Any:
        """이관을 실행하고 최종 RunReport를 반환한다. 테스트가 이 메서드를 교체한다.
        LangGraph가 없으면 inline 엔진을 쓰며 이때는 노드 이벤트와 중지가 없다.
        노드가 끝날 때마다 ``node`` 이벤트를 쌓고 중지 요청을 확인한다.
        중지 요청은 최종 빌드(verify)나 finalize 뒤면 무시하고, 아니면 aborted
        표시 -> 그래프 닫기 -> 보고서 마감 순으로 처리한다.

        Drive the migration; return the final `RunReport`. Tests replace this method."""
        from sbmig.core.models import Phase

        orch = run.orch
        phase = Phase(run.stop_after) if run.stop_after else None
        # 실행 방식은 full이 아닐 때만 넘긴다.
        # full은 엔진의 기본값이므로 flow를 모르는 엔진에서도 이관이 실행된다
        extra: dict[str, Any] = {"flow": run.flow} if run.flow and run.flow != FLOW_FULL else {}
        try:
            from sbmig.graph import stream_graph
        except ImportError as e:      # no sbmig.graph / no langgraph: inline engine, no node events (kept on purpose)
            log.info("graph engine unavailable (%s) — running the inline engine", e)
            return orch.run(stop_after=phase, **extra)
        updates = stream_graph(orch, stop_after=phase, **extra)
        stopped = False
        try:
            for name, update in updates:
                self._push(run, "node", {"node": name, "update": _jsonable(update)})
                if not run.cancel.is_set():
                    continue
                # 최종 빌드가 이미 실행되었으면 이관은 끝난 것이므로, 여기서 중지하면 성공을 중단으로 잘못 기록하게 된다
                # 이관 뒤에 실행되는 remediation 노드도 최종 빌드 뒤에 있으므로 마찬가지다
                if not update.get("aborted") and (name in ("finalize", "remediation")
                                                  or (name == "phase" and update.get("phase") == Phase.VERIFY.value)):
                    # the final build already ran: the migration is complete, a stop now would only mislabel it
                    log.warning("run %s: stop requested after the final build — finishing normally", run.run_id)
                    continue
                log.warning("run %s: stop requested — stopping after node %s (no final build)", run.run_id, name)
                # 엔진은 일찍 닫힐 때 보고서를 마감하므로 닫기 전에 aborted를 표시한다
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
        """실행 기록에 이벤트를 추가하고 기다리는 SSE 스트림을 깨운다.
        ``node`` 이벤트면 마지막 노드 이름도 갱신한다.
        이벤트는 화면으로 나가므로 출력 마스킹이 설치되어 있으면 이벤트 문자열도 마스킹한다."""
        if output_scrubbing():
            data = scrub_json(data)
        with run.cond:
            run.history.append((kind, data))
            if kind == "node":
                run.last_node = data.get("node")
            run.cond.notify_all()

    def stop(self, run_id: str) -> bool:
        """현재 그래프 노드가 끝나면 실행을 중지하도록 요청한다. 진행 중인 Maven이나 LLM 호출은 끊지 않는다.
        모르는 실행이거나 이미 끝났으면 False를 반환한다. Job 모드에서 대기 중인 실행은 바로 줄에서 빠지고
        Job 없이 stopped로 끝나며, in-process의 대기 실행은 차례가 와서 첫 노드를 마친 뒤 멈춘다.

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

    # ---- remediation resume in a new build pod (2026-11-11, wave 4)
    def resume_remediation(self, run_id: str) -> dict[str, Any]:
        """끝난 Job 모드 실행의 취약점 조치를 새 빌드 파드에서 이어 가는 시도를 대기열에 넣고,
        ``remediation_job`` 정보를 반환한다.
        - Job 모드가 아니거나, 이관이 ``finished``가 아니거나, 재개가 이미 대기 중이거나 실행 중이면
          ``StateConflict``를 던진다
        - 남아 있는 ``stop.request``를 지우고, ``control/remediation-resume.json``에 queued를 먼저 기록한 뒤
          업로드와 같은 대기열에 선다

        Queue a NEW attempt of a finished Job-mode run that resumes its chained remediation in a new build pod. `StateConflict`
        when not in a Job mode, the run is unknown / not accepted in a Job mode / not finished, or a resume of it is already
        queued or running. Clears a leftover `stop.request`, records `queued` in `control/remediation-resume.json` first, then
        joins the same FIFO line as uploads. Returns `remediation_job(run_id)`."""
        from sbmig.remediations.models import StateConflict

        if self.effective_mode not in ("k8s-job", "local-job"):
            raise StateConflict(f"resuming a remediation in a build pod needs a Job mode (this server runs {self.effective_mode})",
                                mode=self.effective_mode)
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
            raise ValueError("bad run id")
        if self.closing.is_set():
            raise StateConflict("the web server is shutting down")
        rd = self.run_dir(run_id)
        rec = RequestRecord.read(rd) if rd.is_dir() else None
        if rec is None:
            raise StateConflict(f"run {run_id} has no control/request.json — only a run accepted in a Job mode can resume its "
                                "remediation in a build pod")
        mig = self.status(run_id).get("state")
        if mig != "finished":
            raise StateConflict(f"the migration run {run_id} is {mig}, not finished", status=mig)
        with self._lock:
            cur = self.resumes.get(run_id)
            disk = ControlState.read(rd, RESUME_STATE_FILE)
            if cur is not None and not cur.done.is_set():
                raise StateConflict(f"a remediation resume of run {run_id} is already {self._mem_state(cur)}",
                                    status=self._mem_state(cur))
            if cur is None and disk is not None and not disk.terminal:
                # 이전 웹 프로세스가 기록한 끝나지 않은 재개다.
                # 복구가 이어받지 않았으면 그 Job이 아직 실행 중일 수 있으므로 거부한다
                raise StateConflict(f"a remediation resume of run {run_id} is recorded as {disk.state}", status=disk.state)
            mig_cs = ControlState.read(rd)
            attempt = max(int(rec.attempt or 1), int(mig_cs.attempt) if mig_cs else 1, int(disk.attempt) if disk else 0) + 1
            stop_file = rd / STOP_REQUEST_FILE
            try:
                if stop_file.is_file():
                    # 이관은 이미 끝났으므로 남은 중지 요청은 이관의 것이다.
                    # 새 재개 시도가 바로 멈추지 않도록 지운다
                    stop_file.unlink()
                    log.info("run %s: a leftover %s removed before the remediation resume", run_id, STOP_REQUEST_FILE)
                control = ControlState(run_id=run_id, state=RunState.QUEUED.value, attempt=attempt,
                                       action=ACTION_REMEDIATION_RESUME)
                control.write(rd, RESUME_STATE_FILE)          # intent first: the next Main picks the request up
            except OSError as e:
                raise StateConflict(f"the resume request could not be recorded ({type(e).__name__})") from None
            run = Run(run_id, rd, rd / SOURCE_ZIP, list(rec.configs or []), rec.stop_after, rec.llm_provider, job=True,
                      request=rec, control=control, action=ACTION_REMEDIATION_RESUME, flow=rec.flow or FLOW_FULL,
                      upload=rec.upload_name or rec.upload)
            run.job_name = resume_job_name(run_id, attempt)
            run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-job-{run.key}", daemon=True)
            self.resumes[run_id] = run
            self._turns.join(run.key)          # the same line as uploads, in request order
        _append_journal(rd, {"event": "remediation_resume_queued", "run_id": run_id, "attempt": attempt,
                             "job": run.job_name, "action": ACTION_REMEDIATION_RESUME})
        try:
            run.thread.start()
        except RuntimeError:
            self._turns.leave(run.key)
            with self._lock:
                self.resumes.pop(run_id, None)
            raise
        log.info("run %s: remediation resume queued (attempt %d, job %s)", run_id, attempt, run.job_name)
        return self.remediation_job(run_id) or {}

    def remediation_job(self, run_id: str) -> dict[str, Any] | None:
        """이 실행의 마지막 조치 재개 시도 정보를 반환한다. ``state``(queued / running / finished),
        ``active``, ``attempt``, ``job``, ``control``과, 있으면 ``slot``, ``queue``, ``exit_code``,
        ``verified``, ``error``, ``note``, ``stop_requested``를 담는다.
        메모리에 없으면 ``control/remediation-resume.json``에서 읽고, 재개한 적이 없으면 None을 반환한다.

        The last remediation resume attempt of `run_id` (memory first, else its control file); None when there was none."""
        run = self.resumes.get(run_id)
        if run is not None:
            state = self._mem_state(run)
            info: dict[str, Any] = {"state": state, "active": state != "finished", "attempt": run.attempt,
                                    "action": run.action, "job": run.job_name,
                                    "control": run.control.state if run.control is not None else None}
            if run.slot is not None:
                info["slot"] = run.slot
            if state == "queued":
                pos = self._turns.position(run.key)
                if pos is not None:
                    info["queue"] = {"position": pos, "ahead": pos - 1}
            if run.job_phase:
                info["phase"] = run.job_phase
            if run.job_note:
                info["note"] = run.job_note
            if state == "finished":
                info["exit_code"] = run.exit_code
                info["verified"] = run.verified
            if run.error:
                info["error"] = run.error
            if run.cancel.is_set() or (state != "finished" and (run.run_dir / STOP_REQUEST_FILE).is_file()):
                info["stop_requested"] = True
            return info
        rd = self.run_dir(run_id)
        cs = ControlState.read(rd, RESUME_STATE_FILE) if rd.is_dir() else None
        if cs is None:
            return None
        active = not cs.terminal
        info = {"state": ("queued" if cs.state == RunState.QUEUED.value else "running") if active else "finished",
                "active": active, "attempt": cs.attempt, "action": cs.action, "job": cs.job, "control": cs.state}
        if cs.slot is not None:
            info["slot"] = cs.slot
        if not active:
            info["exit_code"] = cs.exit_code
        if cs.error:
            info["error"] = cs.error
        return info

    def remediation_active(self, run_id: str) -> bool:
        """이 실행의 조치 재개 시도가 대기 중이거나 실행 중인지 확인한다.
        그동안은 그 시도가 조치 상태 파일을 관리하므로 승인과 재개를 막고,
        마지막 마스킹 전이므로 조치 파일을 내주지 않는다.

        Whether a remediation resume of `run_id` is queued or running now."""
        try:
            return bool((self.remediation_job(run_id) or {}).get("active"))
        except Exception:  # noqa: BLE001 — unreadable = assume it may run (refuse rather than race the build pod)
            log.debug("remediation_job(%s) failed", run_id, exc_info=True)
            return True

    def stop_remediation(self, run_id: str) -> str | None:
        """조치 재개 시도를 중지한다.
        대기 중이면 바로 줄에서 빠지고 Job 없이 stopped로 끝나며 ``"withdrawn"``을 반환한다.
        Job이 이미 있으면 ``stop.request``만 기록하고 ``"requested"``를 반환한다.
        조치는 다음 단계 경계에서 멈추고 Job은 지우지 않는다. 실행 중인 재개가 없으면 None을 반환한다.

        Stop a remediation resume attempt: a waiting one leaves the line now (`"withdrawn"`); one whose Job exists gets
        `stop.request` only (`"requested"`, never a Job deletion); None when no resume is queued or running."""
        run = self.resumes.get(run_id)
        if run is None or run.done.is_set():
            return None
        if self._turns.withdraw(run.key):
            run.cancel.set()
            log.info("run %s: remediation resume stopped while waiting — it leaves the line", run_id)
            return "withdrawn"
        if run.control is None or run.control.state == RunState.QUEUED.value:
            run.cancel.set()                   # its turn came but no Job exists yet: _run_job creates none
            return "requested"
        try:
            (run.run_dir / STOP_REQUEST_FILE).write_text("stop requested from the web UI (remediation resume)\n",
                                                         encoding="utf-8")
        except OSError as e:
            from sbmig.remediations.models import StateConflict
            raise StateConflict(f"the stop request could not be written ({type(e).__name__})") from None
        _append_journal(run.run_dir, {"event": "stop_request_sent", "run_id": run_id, "job": run.job_name,
                                      "action": run.action})
        log.info("run %s: remediation resume — %s written for job %s", run_id, STOP_REQUEST_FILE, run.job_name)
        return "requested"

    def shutdown(self, reason: str = "server shutdown") -> dict[str, Any]:
        """서버 종료(lifespan) 때 정리 작업을 한다. 열린 SSE 스트림을 모두 끝내고, 실행 중이거나 대기 중인
        실행에 중지를 요청하고, 남은 Maven / OpenRewrite 프로세스를 종료한다.
        이렇게 하지 않으면 uvicorn이 SSE 연결이 끝나기를 기다리고, Windows에서는 java.exe가 서버보다 오래 남는다.

        Server shutdown (the web app's lifespan): end every open event stream, ask every running / queued run to
        stop after its current graph node, and kill the Maven / OpenRewrite processes still running — otherwise
        uvicorn waits on the SSE connections and a `java.exe` outlives the server on Windows (2026-10-21)."""
        from sbmig.build.mvn import kill_live_processes

        self.closing.set()
        stopped: list[str] = []
        for run in list(self.runs.values()) + list(self.resumes.values()):
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
        # 마지막 마스킹이 아직 남았는지 판단한다. 메모리의 실행은 끝나지 않았고 Job이거나 출력 마스킹이
        # 설치된 경우, 메모리에 없는 실행은 제어 상태가 아직 끝나지 않은 경우다
        if run is not None:
            return not run.done.is_set() and bool(run.job or output_scrubbing())
        return control is not None and not control.terminal

    def file_withheld(self, run_id: str, name: str) -> bool:
        """이 실행의 파일 ``name``을 아직 내주면 안 되는지 확인한다. 마지막 마스킹이 남았고 ``name``이
        기록할 때 마스킹되는 파일(``LIVE_FILES``)이 아니면 True를 반환한다.

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
        """실행 상태를 dict로 반환한다. 메모리에 있으면 queued / running / finished를, 없으면
        web-status.json, report.json, 폴더 유무 순으로 확인해 finished / unknown / missing을 정한다.
        report.json이 있으면 성공 여부, 차단, 미해결, LLM 호출, 오류 목록 개수와 내려받을 수 있는 파일 목록을 담는다.
        항상 ``parallel``을 담고, 대기 중이면 ``queue``를, 슬롯을 가진 Job 실행이면 ``slot``을 담는다.

        The run's status dict (memory first, else the run directory). Always `parallel`; `queue` for a run waiting in this
        process's line; `slot` for a Job run that holds or held one."""
        run = self.runs.get(run_id)
        rd = self.run_dir(run_id)
        info: dict[str, Any] = {"run_id": run_id, "run_dir": str(rd), "exists": rd.is_dir() or run is not None,
                                "mode": self.effective_mode, "parallel": self.parallel(),
                                # 실행 방식이며 화면의 단계 표시, 결과 칸, 지난 실행 목록이 이 값을 따른다
                                "flow": (run.flow or FLOW_FULL) if run is not None else _recorded_flow(rd),
                                # 사용자가 올린 파일 이름 그대로다.
                                # 화면이 실행을 이 이름으로 보여 주며, 이전 in-process 실행에는 없을 수 있다
                                "upload": (run.upload if run is not None else None) or _recorded_upload(rd)}
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
        # 조치 재개 Job이 있으면 이관의 상태와 따로 보여 준다
        rjob = self.remediation_job(run_id)
        if rjob is not None:
            info["remediation_job"] = rjob
        rep = rd / "report.json"
        if rep.is_file():
            try:
                data = json.loads(rep.read_text(encoding="utf-8"))
                info["success"] = data.get("success")
                info["blocked"] = len(data.get("blocked") or [])
                info["unresolved"] = len(data.get("unresolved") or [])
                info["llm_calls"] = (data.get("llm_usage") or {}).get("calls")
                # 실행 중단 사유(접수 거절 등)와 조치의 짧은 요약이 로그 탭에
                # 묻히지 않도록 화면 결과 칸에 문장으로 보여 준다
                why = _abort_reason(data)
                if why:
                    info["abort_reason"] = scrub(why) if output_scrubbing() else why
                    ko = _abort_reason_ko(info["abort_reason"])
                    if ko:
                        info["abort_reason_ko"] = ko
                rsum = data.get("remediation")
                if isinstance(rsum, dict) and rsum.get("status"):
                    info["remediation"] = {k: rsum[k] for k in ("status", "reason", "missing") if rsum.get(k) is not None}
                from sbmig.report.result_line import entry_counts
                info["open"], info["resolved"] = entry_counts(data.get("error_entries") or [])   # v2.1 error list
            except (OSError, ValueError):
                pass
        pending = self._final_pass_pending(run, control)       # 마지막 마스킹 전에는 기록할 때 마스킹되는 파일만 목록에 넣는다
        for name in SERVED_FILES:
            if (rd / name).is_file() and (not pending or name in LIVE_FILES):
                info.setdefault("files", []).append(name)
        return info

    def _mem_state(self, run: Run) -> str:
        # 슬롯을 가졌으면 running으로 보여 준다. 작업 스레드가 active를 세우기 직전이나 복구 직후에 슬롯
        # 번호 없는 queued로 보이지 않게 하기 위해서다
        if run.done.is_set():
            return "finished"
        if run.active.is_set() or self._turns.slot_of(run.key) is not None:
            return "running"
        return "queued"

    def parallel(self) -> dict[str, int]:
        """현재 동시 실행 현황을 반환한다. ``slots``는 한도(``max_parallel``), ``running``은 슬롯을 가진
        실행 수(복구 직후에는 한도보다 많을 수 있다), ``waiting``은 대기 중인 실행 수다.

        The parallel picture: the limit, the runs holding a slot (may exceed the limit right after a restart), the waiting."""
        running, waiting = self._turns.counts()
        return {"slots": self.max_parallel, "running": running, "waiting": waiting}

    def list_runs(self) -> list[dict[str, Any]]:
        """메모리와 출력 폴더에 있는 모든 실행의 상태를 최신순으로 반환한다(``_``로 시작하는 폴더는 제외).
        ``parallel``은 라우트가 한 번만 붙이고, ``queue`` / ``slot``은 알 수 있는 경우에만 담는다.

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
        """SSE로 보낼 (이벤트, 데이터)를 실행이 끝나고 남은 것을 모두 보낼 때까지 내보내는 블로킹 생성기.
        스레드풀에서 실행된다.
        처음 ``status`` -> 노드, 오류 기록, journal, 로그 파일의 새 줄(시간순) -> 보낼 것이 없으면 ``ping`` -> 끝나면
        ``report`` / ``status`` / ``end`` 순으로 보낸다. 서버 종료 때는 ``end``(reason)를 보내고 연결을 놓는다.
        끝나기 전에도 상태나 대기 순번이 바뀌면 ``status``를 다시 보내,
        화면이 새로 고침 없이 순번 변화를 보여 주게 한다.

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
            # journal과 로그는 같은 스레드가 기록하는 두 파일이라 하나씩 비우면 화면에서 시간 순서가
            # 뒤섞인다. 한 번에 모아 시각으로 정렬하고, 시각 없는 줄은 앞 줄의 시각을 따른다
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
                    # Job 모드나 지난 실행을 다시 보여 줄 때는 파드가 기록한 graph_node_end 줄을 화면의 node
                    # 이벤트로 바꾼다. 이 프로세스에서 실행한 것은 _push로 이미 받았다
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
            # 종료 여부는 비우기 전에 확인한다.
            # done이 선 뒤에 비우면 실행이 기록한 내용이 모두 파일에 들어 있다
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
            # 서버 종료 때 연결을 놓아야 uvicorn이 기다리지 않고 종료된다
            if self.closing.is_set():     # server shutdown: release the connection, the browser reconnects later
                yield "status", self.status(run_id)
                yield "end", {"run_id": run_id, "reason": "server shutdown"}
                return
            # 대기 실행의 순번이 바뀌었거나 실행이 시작되었는지 가벼운 비교로
            # 먼저 확인하고, 바뀌었을 때만 status()를 만든다
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
    # SSE가 status를 다시 보낼지 판단하는 값(상태, 대기 순번)이며, 순번은 이 프로세스의 줄에서 기다릴 때만 있다
    return st.get("state"), (st.get("queue") or {}).get("position")


def _write_web_status(run: Run) -> None:
    """작업 스레드가 끝날 때 web-status.json(종료 코드, 오류, 중지 요청, 마지막 노드)을 기록한다.
    메모리에서 빠졌거나 서버를 다시 띄운 뒤에도 상태를 응답하기 위해서다.
    기록에 실패해도 실행 결과에는 영향이 없다."""
    try:
        data = {"run_id": run.run_id, "exit_code": run.exit_code, "error": run.error, "flow": run.flow, "upload": run.upload,
                "stop_requested": run.cancel.is_set(), "last_node": run.last_node,
                "success": run.success if run.success is not None else getattr(run.report, "success", None)}
        (run.run_dir / WEB_STATUS_FILE).write_text(json.dumps(data, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:   # the status file is a convenience; never fail the run on it
        log.debug("cannot write %s for %s: %s", WEB_STATUS_FILE, run.run_id, e)


def _journal_job(rd: Path) -> dict[str, Any] | None:
    """실행 폴더 저널에서 ``job_created`` 줄과 마지막 graph_node_end의 노드를 찾아 반환한다.
    이전 웹 프로세스가 만든 Job이 아직 실행 중일 수 있기 때문이다.
    ``JOB_STALE_S``보다 오래되었으면 상태 파일 없이 끝난 것으로 보고 None을 반환한다.

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
    """k8s Job 모드에서 파드가 기록한 ``job-status.json``을 읽는다. 없거나 깨졌으면 None을 반환한다."""
    try:
        data = json.loads((rd / JOB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "exit_code" in data else None


def _report_success(rd: Path) -> bool | None:
    """report.json의 success 값을 반환한다.
    파일이 없으면 None을 반환하며, Job 모드의 finished 이벤트에 쓴다."""
    try:
        return json.loads((rd / "report.json").read_text(encoding="utf-8")).get("success")
    except (OSError, ValueError, AttributeError):
        return None


def _append_journal(rd: Path, event: dict[str, Any]) -> None:
    """실행 폴더의 journal.jsonl에 ts를 포함한 한 줄을 추가한다.
    Job 모드에서 웹이 직접 기록하는 줄(web_run, job_created, stop_request_sent)에 쓴다."""
    try:
        line = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        text = json.dumps(line, default=str)
        if output_scrubbing():                    # 직렬화한 줄을 통째로 바꾸면 JSON이 깨질 수 있으므로 값 단위로 마스킹한다
            text = json.dumps(scrub_json(json.loads(text)))
        with open(rd / "journal.jsonl", "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except (OSError, TypeError, ValueError) as e:
        log.debug("journal line %s not written: %s", event.get("event"), e)


def _read_web_status(rd: Path) -> dict[str, Any] | None:
    """web-status.json을 읽는다. 없거나 깨졌으면 None을 반환한다."""
    try:
        data = json.loads((rd / WEB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _mark_stopped(orch: Any) -> None:
    """사용자 중지를 보고서에 반영한다.
    아직 마감하지 않은 보고서를 aborted로 표시해 report.md에 남기고 종료 코드를 3으로 만든다.
    이미 마감되었거나 aborted면 아무것도 하지 않는다.

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
    """중지된 실행의 보고서를 마감한다. 중지 뒤 그래프는 노드 사이에 멈춰 있으므로, 엔진이 닫힐 때 보고서를
    마감하지 않았으면 여기서 report.md / report.json을 기록한다.
    중지 처리 때문에 보고서가 사라지지 않도록 예외는 로그만 남기고 무시한다.

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
    """이 파일에 붙은 실행별(스레드 필터) 핸들러만 뗀다. 같은 파일에 붙은 프로세스 전체 핸들러는 남긴다.

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
    """그래프 상태 갱신 값을 JSON으로 보낼 수 있는 데이터로 바꾼다.
    화면이 무거워지지 않도록 ``MAX_UPDATE_CHARS``를 넘으면 키 이름만 보낸다.

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
    """파일에 새로 추가된 완성된 줄만 반환하고, 읽은 위치는 ``offsets``에 기억한다.
    반쪽 줄이나 깨진 JSON을 보내지 않도록 줄바꿈이 오지 않은 마지막 조각은 다음으로 미룬다."""
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
