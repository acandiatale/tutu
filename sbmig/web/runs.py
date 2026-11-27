"""웹 화면에서 올린 이관 실행을 시작하고, 상태와 진행 이벤트를 관리하는 모듈.
in-process에서는 이 프로세스의 작업 스레드가, Job 모드에서는 Job(개발 PC에서는 로컬 Worker 프로세스)이 이관을 실행한다.

A run = an uploaded zip + the server's `config/*.yml` files. `start()` validates the request, builds the
`Config`, the `Workspace` (`<out>/<run id>/`) and the `Orchestrator` synchronously (so a bad request fails
with a 400), then a worker thread drives the migration through `sbmig.graph.stream_graph` and records every
graph node update. When `sbmig.graph` is not importable (no LangGraph installed) the worker falls back to
`Orchestrator.run()`: same report, same exit code, just no `node` events.

Rules: Job mode runs up to ``SBMIG_JOB_MAX_PARALLEL`` runs at a time (default 1), in-process always one. The
line keeps upload order, and a run gets the lowest free slot number (from 1). A Job is adopted or deleted
only when both the ``sbmig-owner`` label and the run label match. A slot is handed on only once the Job is
confirmed ended; an API that does not answer never fails a run. A run whose result file could not be
verified is never reported as a success.

What the SSE stream relays (`events()`):
    status   the `status()` dict (first and last event)
    node     {"node": <graph node>, "update": <JSON-able state update>}, one per finished graph node
    error    {"message": ...}: the worker raised (the run is then finished with exit code 3)
    journal  one parsed `journal.jsonl` record (tailed from the run directory)
    log      one `sbmig.log` line (DEBUG included; the per-run file only holds this run's thread)
    report   the `report.md` text, once the run finished
    ping     keep-alive after 15 s of silence
    end      last event

Stop: `stop()` sets a cancel flag the worker checks between graph updates. A node in progress is not
interrupted (LangGraph cannot cancel a running sync node), so the run stops after the current node and is
finalized as aborted ("stopped by user"; exit code 3) without the final `mvn clean package`. A stop that
arrives once the verify phase has run changes nothing. With the inline fallback engine there are no
intermediate updates and the flag has no effect.

Concurrency: worker threads queue on `RunManager._turns` (FIFO line + numbered slots). In-process the LLM call
counter and the per-run log filter are process-wide, so that mode always runs one; in Job mode each migration
is its own pod, the shared file KB takes its cross-process lock for every write, and each slot builds with its
own Maven local repository (the Worker, from `SBMIG_JOB_SLOT`). A waiting run reports state ``queued`` and its
place (`queue`). A run takes its place in the line when `start_file` accepts it (before its thread starts); a
plain `threading.Lock` does not hand itself over in arrival order.

Memory: `runs` keeps at most `max_finished_runs` finished runs (running / queued ones are never dropped). An
evicted run is answered from its run directory: `report.json` and `web-status.json` (exit code / error / stop
flag, written when the worker finishes). A `fix_mode` / `kb` key left in an older run's `web-status.json` or
`report.json` is ignored when read.

Environment pins: in the Job modes the Main that creates a Job writes fingerprints of the environment values
the configuration reads (`env_pins`, never a secret-looking name) and the policy `SBMIG_JOB_ENV_PIN` (default
`refuse` in k8s-job, `off` in local-job) into `control/state.json` with the `creating` transition; the Worker
compares them with its own environment. A run adopted after a restart keeps the pins already on disk.

Remediation resume: `resume_remediation(run_id)` dispatches a NEW attempt of a finished Job-mode run with the
action `remediation-resume`: same template, run dir, labels (owner token of its own), FIFO line and slots as
an upload; its control state is `control/remediation-resume.json`, its result `remediation-job-status.json`,
its Job is named `<migration job name>-rem<attempt>`. The migration's own state, report and `web-status.json`
are never touched: `status()` keeps the run finished and adds `remediation_job` {state, attempt, slot, queue
...}. Recovery after a restart re-attaches to a resume Job exactly like to a migration Job. At most one active
resume per run. Contract: docs/INTERFACES.md "v2.1z-ae".
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
from sbmig.core.workspace import STOP_REQUEST_FILE, STOP_REQUEST_REASON
from sbmig.web import contract
from sbmig.web.contract import (
    ACTION_REMEDIATION_RESUME,
    CONTROL_DIR,
    OWNER_LABEL,
    RESUME_STATE_FILE,
    RESUME_STATUS_FILE,
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
    NOT_STARTED_PHASES,
    PENDING_NOTICE_ENV,
    POLL_ENV,
    REQUIRED_PERMISSIONS,
    STATUS_GRACE_ENV,
    STOP_GRACE_ENV,
    TEMPLATE_ENV,
    TEMPLATE_HINT,
    TERMINAL_PHASES,
    JobState,
    K8sClient,
    K8sError,
    cluster_host_present,
    in_cluster_available,
    job_name,
    kubeconfig_path,
    pod_wait_hint_ko,
    pod_wait_reason,
    render_job,
    validate_worker_template,
)

log = logging.getLogger("sbmig.web")

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SERVED_FILES = {"report.md": "text/markdown; charset=utf-8", "report.json": "application/json",
                "result.zip": "application/zip", "sbmig.log": "text/plain; charset=utf-8",
                "journal.jsonl": "application/x-ndjson", "changes.patch": "text/plain; charset=utf-8",
                "plan.json": "application/json", "profile.json": "application/json",
                # 실행의 오류 목록. 오류마다 시도, LLM 답변, 적용한 diff가 들어 있다
                "errors.md": "text/markdown; charset=utf-8",
                "errors.jsonl": "application/x-ndjson; charset=utf-8"}
# 사람이 내려받을 때 쓰는 이름. 실행 폴더 안의 파일 이름과 주소는 그대로다
DOWNLOAD_NAMES = {"report.md": "migration-report.md", "report.json": "migration-report.json"}
# 기록할 때 이미 마스킹되는 파일. 그래서 실행 중에도 내려받을 수 있다.
# 나머지 파일은 실행이 끝나고 마지막 마스킹을 거친 뒤에만 내준다(`RunManager.file_withheld`)
LIVE_FILES = ("sbmig.log", "journal.jsonl")
# journal과 sbmig.log의 새 줄은 알려 주는 쪽이 없다. 0.5초마다 다시 읽는다.
# 15초 동안 보낼 것이 없으면 ping을 보낸다. 몇 분 걸리는 Maven 빌드 동안 브라우저나 프록시가 연결을 끊지 않게 한다
POLL_S = 0.5
KEEPALIVE_S = 15.0
MAX_UPDATE_CHARS = 20_000          # 노드 갱신이 이보다 크면 `node` 이벤트에는 키 이름만 보낸다
EXIT_ABORTED = 3                   # sbmig.cli.EXIT_ABORTED와 같은 값. 작업 스레드가 죽었거나 사용자가 중지했을 때 쓴다
WEB_STATUS_FILE = "web-status.json"   # 작업 스레드의 최종 상태. 메모리에서 빠진 실행은 이 파일로 응답한다
JOB_STATUS_FILE = "job-status.json"   # Job 모드에서 Worker가 공유 볼륨에 기록하는 결과 파일
SOURCE_ZIP = "source.zip"             # Job 모드에서 업로드한 zip을 실행 폴더에 두는 이름
JOB_STALE_S = 6 * 3600                # 이전 프로세스의 Job 실행이 결과 파일 없이 이보다 오래되면 unknown으로 본다
DEFAULT_MAX_FINISHED_RUNS = 200
LOCAL_LOST = "the local job did not survive the server restart"
# Job 생성 응답이 불확실하면(이미 있음, 전송 오류) 같은 이름의 Job을 이어받는다. Job을 두 번 만들지 않는다
_UNCERTAIN_CREATE_RE = re.compile(r"HTTP 409|RequestException|ConnectionError|Timeout|ChunkedEncodingError|ProtocolError")
# 슬롯은 Job이 끝난 것을 확인한 뒤에 넘긴다. 그래야 슬롯 수 한도가 지켜진다. 아래는 _hold_until_ended가 쓰는 간격이다
HOLD_WARN_S = 60.0                 # 끝나지 않는 Job을 기다리는 동안 경고를 남기는 간격
DELETE_RETRY_S = 15.0              # Job 삭제 요청이 실패했을 때 다시 시도하는 간격
API_REMIND_S = 600.0               # Kubernetes API가 계속 응답하지 않을 때 로그를 다시 남기는 간격
CREATE_CONFIRM_POLLS = 3           # 생성 응답이 불확실하고 Job이 보이지 않을 때 더 조회하는 횟수
# 시작하지 못한 파드의 안내는 이유가 바뀌거나 이 간격이 지나면 다시 보낸다
PENDING_RESEND_S = 300.0
PENDING_EVENTS_EVERY_S = 15.0      # 파드 이벤트를 다시 조회하는 최소 간격
POD_WAIT_MESSAGE_MAX = 300
STOP_BEFORE_START_ERROR = "stopped: the build pod had not started — the job was deleted"
STOP_BEFORE_START_KO = "빌드 파드가 시작되기 전이라 작업을 바로 정리했습니다."
EVENTS_UNREADABLE_LINE = "pod events are not readable with this account — the page will not show why a pod does not start"


class _ServerClosing(Exception):
    """Kubernetes API 응답을 기다리는 중에 서버가 종료될 때 던지는 예외 클래스.
    실행을 실패로 기록하지 않는다. 제어 상태를 그대로 두어서 다음 Main이 이어받게 한다."""


class _LeftLine(Exception):
    """대기 중이던 실행이 중지 요청으로 대기열에서 빠졌을 때 던지는 예외 클래스. ``_FifoTurns.withdraw``가 뺀다.
    이 실행은 슬롯을 받지 않았고 Job도 만들지 않았다."""


@dataclass
class Run:
    """이 서버 프로세스가 시작한 실행 하나의 메모리 상태를 담는 클래스.
    이전 프로세스가 시작한 실행은 실행 폴더로만 남는다."""
    run_id: str
    run_dir: Path
    zip_path: Path
    configs: list[str] = field(default_factory=list)
    stop_after: str | None = None
    llm_provider: str | None = None
    orch: Any = None
    thread: threading.Thread | None = None
    history: list[tuple[str, Any]] = field(default_factory=list)    # node, error, finished 이벤트. 뒤에 붙이기만 한다
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel: threading.Event = field(default_factory=threading.Event)
    active: threading.Event = field(default_factory=threading.Event)     # 대기열을 빠져나와 실행 중이면 설정된다
    done: threading.Event = field(default_factory=threading.Event)
    report: Any = None
    exit_code: int | None = None
    error: str | None = None
    last_node: str | None = None
    started: float = field(default_factory=time.time)
    # Job 모드에서는 실행이 다른 파드에서 진행된다. Job 이름과 마지막으로 확인한 상태를 둔다
    job: bool = False
    job_name: str | None = None
    job_phase: str | None = None
    job_pod: str | None = None
    # 접수할 때 고정한 입력(control/request.json)과 Main의 제어 상태(control/state.json)
    request: Any = None
    control: Any = None
    verified: bool | None = None          # True면 이 실행, 이 시도의 job-status.json을 받았다는 뜻
    result_source: str | None = None      # "status"는 job-status.json, "container"는 컨테이너 종료 코드만 본 경우
    final_state: str | None = None        # _run_job이 정한 종료 상태. 없으면 결과 확인으로 finished나 lost를 정한다
    keep_state: bool = False              # 서버가 종료될 때 다음 Main이 이어받도록 state.json을 그대로 둔다
    recover: str | None = None            # 재시작 후 복구 방식. None, "probe", "adopt" 중 하나
    status_warned: bool = False
    status_error: str | None = None       # Worker가 실행 자체를 하지 못했을 때 job-status.json에 담긴 error
    pod_step_closed: bool = False         # '파드' 단계를 닫는 이벤트를 이미 보냈는지. 이 이벤트는 한 번만 보낸다
    # Kubernetes API가 응답하지 않을 때의 기록. 처음 실패한 시각, 마지막으로 알린 시각(monotonic), 화면에 보일 한 줄
    api_down_since: float | None = None
    api_down_reported: float | None = None
    job_note: str | None = None
    success: bool | None = None
    # 이 Job 실행이 차지했던 슬롯 번호. Job env SBMIG_JOB_SLOT, control/state.json의 slot과 같다
    slot: int | None = None
    # None이면 이관, "remediation-resume"이면 조치 재개 시도다. 재개 시도는 제어 상태, 결과 파일, 대기열 키를 따로 쓴다
    action: str | None = None
    # full이면 이관 전체, remediation이면 이관 없이 취약점 조치만 한다. Job 실행에서는 request.json의 값과 같다
    flow: str = FLOW_FULL
    # 사용자가 올린 파일 이름 그대로
    upload: str | None = None
    # 업로드할 때 입력한 프로젝트명. 화면은 실행을 이 이름으로 보여 준다. 이전 실행에는 없다
    project: str | None = None
    # 파드가 시작하지 못한 이유 안내. status()의 job.pending으로 나가고, 파드가 시작하면 None이 된다
    job_pending: dict[str, Any] | None = None
    # 그 안내를 만들 때 쓰는 기록. 처음 본 시각, 마지막으로 보낸 시각, 이미 알린 이유를 담는다
    pod_wait: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """대기열(``_turns``)에서 쓰는 키를 반환한다.
        이관은 실행 id, 재개 시도는 ``<실행 id>#<action>``이다. 그래서 같은 실행의 두 시도가 서로를 막지 않는다."""
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
        """제어 상태에 기록된 시도 번호를 반환한다. 제어 상태가 없으면 1을 반환한다."""
        return int(self.control.attempt) if self.control is not None else 1

    @property
    def owner(self) -> str | None:
        """Job 라벨 ``sbmig-owner``의 값을 반환한다. 요청마다 다른 난수다."""
        return self.control.owner if self.control is not None else None


class _FifoTurns:
    """업로드 순서대로 실행을 들여보내는 대기열과 번호 붙은 슬롯을 관리하는 클래스. 동시에 ``slots``개까지 실행한다.
    ``threading.Lock``은 기다리는 순서를 보장하지 않아서 직접 구현했다.
    - ``join``으로 줄 끝에 선다. ``turn``은 맨 앞이 되고 슬롯이 빌 때까지 기다린다. 블록을 벗어나면 슬롯을 비운다
    - ``occupy``는 재시작 복구에서만 쓴다. 한도와 상관없이 기록된 번호로 슬롯을 잡는다
    - ``withdraw``는 중지된 대기 실행을 줄에서 뺀다

    `turn(key)` waits until `key` is first in line AND fewer than `slots` keys hold a slot (strict FIFO),
    takes the lowest free number from 1; leaving the block (even by an exception) frees the slot and wakes the
    waiters. `occupy` keeps the recorded number even when another key holds it (a new run only gets a number
    no key holds)."""

    def __init__(self, slots: int = 1) -> None:
        self.slots = max(1, int(slots))
        self._cond = threading.Condition()
        self._line: deque[str] = deque()
        # 키별 슬롯 번호. 복구(occupy)는 기록된 번호를 그대로 쓴다. 그래서 두 키가 같은 번호를 가질 수 있다
        self._active: dict[str, int] = {}
        self._withdrawn: set[str] = set()       # 대기 중에 중지되어 빠진 키. turn()이 깨어나서 _LeftLine을 던진다

    def _lowest_free(self) -> int:
        # 아무도 쓰지 않는 가장 작은 번호(1부터)를 반환한다. 호출하는 쪽이 _cond를 잡고 있어야 한다
        used = set(self._active.values())
        n = 1
        while n in used:
            n += 1
        return n

    def join(self, key: str) -> None:
        """키를 대기 줄 끝에 세운다. 같은 키가 이미 줄에 있거나 슬롯을 갖고 있으면 그대로 둔다."""
        with self._cond:
            if key not in self._line and key not in self._active:
                self._line.append(key)

    def leave(self, key: str) -> None:
        """키를 대기 줄이나 슬롯에서 빼고 기다리는 스레드를 깨운다. 없는 키면 아무것도 하지 않는다."""
        with self._cond:
            try:
                self._line.remove(key)
            except ValueError:
                pass
            self._active.pop(key, None)
            self._withdrawn.discard(key)
            self._cond.notify_all()

    def withdraw(self, key: str) -> bool:
        """대기 중인 키를 바로 줄에서 뺀다. 그 키의 ``turn()``은 깨어나서 ``_LeftLine``을 던진다.
        이미 슬롯을 받았거나 모르는 키면 아무것도 바꾸지 않고 False를 반환한다.

        Decided under the same lock as taking a slot, so exactly one of the two happens."""
        with self._cond:
            if key in self._active or key not in self._line:
                return False
            self._line.remove(key)
            self._withdrawn.add(key)
            self._cond.notify_all()
            return True

    def occupy(self, key: str, prefer: int | None = None) -> int:
        """재시작 복구에서 한도와 상관없이 바로 슬롯을 잡고 그 번호를 반환한다.
        - ``prefer``(state.json에 기록된 번호)는 다른 키가 쓰고 있어도 쓴다. 파드가 실제로 그 번호의 Maven 저장소를 쓴다
        - 번호가 겹치면 두 실행 이름을 담아 WARNING을 남긴다
        - ``prefer``가 없으면 비어 있는 가장 작은 번호를 준다
        - 이미 슬롯을 가진 키는 그 번호를 그대로 쓴다

        A Job that exists already runs and must be counted, hence "whatever the limit"."""
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
            self._cond.notify_all()            # 기다리는 실행들의 순번이 바뀌었다. 깨운다
            return slot

    @contextmanager
    def turn(self, key: str) -> Iterator[int]:
        """``key``가 줄 맨 앞이 되고 슬롯이 빌 때까지 기다린 뒤 슬롯 번호를 넘긴다. 블록이 끝나면 슬롯을 비운다.
        ``join`` 없이 호출하면 이때 줄 끝에 선다. ``occupy``로 이미 슬롯을 가진 키는 바로 받는다.
        기다리는 중에 ``withdraw``로 빠지면 ``_LeftLine``을 던진다."""
        with self._cond:
            slot = self._active.get(key)
            if slot is None:
                if key in self._withdrawn:         # 이 스레드가 줄에 서기 전에 이미 빠졌다
                    self._withdrawn.discard(key)
                    raise _LeftLine(key)
                if key not in self._line:
                    self._line.append(key)
                while True:
                    held = self._active.get(key)
                    if held is not None:           # 기다리는 동안 복구의 occupy()가 슬롯을 주었다
                        slot = held
                        break
                    if key not in self._line:      # 기다리는 동안 withdraw()로 빠졌다
                        self._withdrawn.discard(key)
                        raise _LeftLine(key)
                    if self._line[0] == key and len(self._active) < self.slots:
                        self._line.popleft()
                        slot = self._lowest_free()
                        self._active[key] = slot
                        break
                    self._cond.wait()
                # 기다리는 스레드를 모두 깨워서 각자 확인하게 한다. 슬롯이 둘 이상이면 다음 차례도 바로 들어갈 수 있다
                self._cond.notify_all()
        try:
            yield slot
        finally:
            self.leave(key)

    def position(self, key: str) -> int | None:
        """대기열에서 몇 번째인지 반환한다. 1이면 다음 차례다. 기다리고 있지 않으면 None을 반환한다."""
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
    """journal 이벤트나 ``sbmig.log`` 줄의 시각을 epoch 초로 반환한다. 두 파일의 줄을 시간순으로 섞을 때 쓴다.
    journal의 ``ts``는 UTC, 로그는 로컬 시각이다. 시각이 없는 줄(이어지는 줄, traceback)은 None을 반환한다."""
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
    """동시에 실행할 Job 수를 정한다. 인자가 있으면 인자를, 없으면 ``SBMIG_JOB_MAX_PARALLEL``을 쓴다.
    1 이상의 정수가 아니면 WARNING을 남기고 1을 쓴다.
    ``k8s-job``, ``local-job``에서만 적용하고 in-process는 항상 1이다. 더 큰 값을 주면 WARNING을 남긴다."""
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
            # 한도는 Job 모드에서만 적용한다. 같은 프로세스 안의 이관들은 LLM 호출 수와 실행별 로그 필터를 함께 쓴다
            log.warning("%s=%d is ignored in %s mode — one migration at a time (the LLM call counter and the per-run log "
                        "filter are process-wide); set SBMIG_WEB_MODE=k8s-job or local-job to run several",
                        contract.MAX_PARALLEL_ENV, asked, mode)
        return 1
    return asked


def resume_job_name(run_id: str, attempt: int) -> str:
    """조치 재개 시도의 Job 이름을 만든다. 이관 Job 이름에 ``-rem<시도 번호>``를 붙이고 63자를 넘지 않게 한다.

    A name that has to be cut loses its head, never the run id's random tail."""
    suffix = f"-rem{int(attempt)}"
    return job_name(run_id, 63 - len(suffix)) + suffix


# 프로젝트명이 없으면 올린 파일 이름으로 실행 id 앞부분을 만들고 24자로 자른다.
# Job 이름이 63자를 넘으면 job_name이 중간을 잘라 낸다. 끝의 난수는 남는다
RUN_ID_SLUG_MAX = 24
RUN_ID_FALLBACK = "web"


def run_id_slug(upload_name: str | None) -> str:
    """올린 파일 이름으로 실행 id의 앞부분을 만든다.
    경로와 ``.zip``을 떼고 소문자로 바꾼다. ``[a-z0-9]`` 밖의 문자 묶음은 ``-`` 하나로 바꾸고 ``RUN_ID_SLUG_MAX``자로 자른다.
    영문자가 하나도 없으면 ``web``을 쓴다. 숫자만으로는 프로젝트를 알 수 없고 뒤의 날짜와 헷갈린다."""
    name = re.split(r"[\\/]", str(upload_name or ""))[-1]      # OS와 상관없이 경로 구분자 앞부분은 버린다
    if name.lower().endswith(".zip"):
        name = name[:-4]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:RUN_ID_SLUG_MAX].rstrip("-")
    return slug if re.search(r"[a-z]", slug) else RUN_ID_FALLBACK


def run_id_for(upload_name: str | None, now: datetime | None = None, token: str | None = None,
               project: str | None = None) -> str:
    """새 실행 id ``<앞부분>-YYYYMMDD-HHMMSS-<hex 6>``를 만든다. ``now``, ``token``은 테스트용이다.
    앞부분은 검사를 마친 프로젝트명이다. 프로젝트명이 없으면 올린 파일 이름으로 만든다.
    같은 초에 같은 이름으로 올려도 난수로 구분된다. 이미 있는 폴더면 ``RunManager``가 id를 다시 만든다."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    prefix = check_project(project) if project is not None else run_id_slug(upload_name)
    return f"{prefix}-{stamp}-{token or uuid.uuid4().hex[:6]}"


UPLOAD_NAME_MAX = 255                 # 기록하는 원래 파일 이름의 최대 문자 수


def display_upload_name(name: str | None) -> str | None:
    """화면에 보일 원래 파일 이름을 만든다. 경로와 제어 문자를 빼고 ``UPLOAD_NAME_MAX``자로 자른다.
    유니코드는 그대로 둔다. 남는 것이 없으면 None을 반환한다."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return base[:UPLOAD_NAME_MAX] or None


def _abort_reason_ko(reason: str) -> str | None:
    # 중단 사유를 한국어 문장으로 바꾼다. 보고서와 같은 번역표(sbmig.report.i18n.tr_line)를 쓴다.
    # "run aborted: <사유>" 형식부터 번역해 보고, 앞의 "실행 중단: " 같은 부분은 뗀다. 번역되지 않으면 None
    try:
        from sbmig.report import i18n
    except Exception:  # noqa: BLE001 - 번역표가 없으면 한국어 문장이 없을 뿐이다
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


class _RunRecords:
    """상태 응답 하나를 만드는 동안 request.json, web-status.json, report.json을 한 번씩만 읽는 클래스.
    각 파일은 처음 필요할 때 읽는다.

    Every later question of the same answer gets that same content."""

    _UNREAD = object()

    def __init__(self, rd: Path) -> None:
        self.rd = rd
        self._request: Any = self._UNREAD
        self._web: Any = self._UNREAD
        self._report: Any = self._UNREAD

    @property
    def report(self) -> dict[str, Any] | None:
        # 없거나 읽지 못했거나 객체가 아닌 보고서는 모두 None이다.
        # 폴더에 들어갈 수 없어서 is_file이 던진 OSError는 기억해 두고 매번 다시 던진다.
        # `_recorded_flow` catches it, `status` lets it reach the run list's per-row net.
        if self._report is self._UNREAD:
            try:
                self._report = _read_report(self.rd)
            except OSError as e:
                self._report = e
        if isinstance(self._report, OSError):
            raise self._report
        return self._report

    @property
    def request(self) -> RequestRecord | None:
        if self._request is self._UNREAD:
            self._request = RequestRecord.read(self.rd)
        return self._request

    @property
    def web(self) -> dict[str, Any] | None:
        if self._web is self._UNREAD:
            self._web = _read_web_status(self.rd)
        return self._web


def _read_report(rd: Path) -> dict[str, Any] | None:
    """report.json을 읽어 반환한다. 파일이 없거나 읽지 못했거나 객체가 아니면 None을 반환한다."""
    rep = rd / "report.json"
    if not rep.is_file():          # 들어갈 수 없는 폴더의 OSError는 그대로 던진다
        return None
    try:
        data = json.loads(rep.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _recorded_project(rd: Path, records: _RunRecords | None = None) -> str | None:
    # 메모리에 없는 실행의 프로젝트명을 찾는다. request.json(Job 모드), web-status.json(in-process) 순으로 본다.
    # 마지막으로 journal 첫 줄들의 web_run을 본다(끝나기 전에 서버가 내려간 in-process 실행)
    records = records or _RunRecords(rd)
    rec = records.request
    if rec is not None:
        return rec.project if isinstance(rec.project, str) and rec.project else None
    web = records.web
    if web is not None and "project" in web:
        val = web.get("project")
        return val if isinstance(val, str) and val else None
    try:
        with open(rd / "journal.jsonl", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("event") == "web_run":
                    val = ev.get("project")
                    return val if isinstance(val, str) and val else None
    except OSError:
        pass
    return None


def _recorded_upload(rd: Path, records: _RunRecords | None = None) -> str | None:
    # 메모리에 없는 실행의 원래 파일 이름을 찾는다. request.json(Job 모드), web-status.json(in-process) 순이다
    records = records or _RunRecords(rd)
    rec = records.request
    if rec is not None:
        return rec.upload_name or rec.upload or None
    data = records.web
    up = data.get("upload") if isinstance(data, dict) else None
    return up if isinstance(up, str) and up else None


# 프로젝트명 규칙. 영문, 숫자, _, -만 받는다. 실행 id 앞부분이 되므로 셸에서 치기 쉬워야 한다.
# 화면(index.html의 PROJECT_RE)도 같은 식을 쓴다
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$")
PROJECT_REQUIRED = ("project name is required", "프로젝트명을 입력해 주세요.")
PROJECT_INVALID = ("project name: letters, digits, _ and - only, at most 30 characters, starting with a letter or a digit",
                   "프로젝트명은 영문, 숫자, _, - 만 쓸 수 있습니다(공백 없이 30자까지, 첫 글자는 영문이나 숫자).")


class ProjectNameError(ValueError):
    """프로젝트명이 규칙에 맞지 않을 때 던지는 예외 클래스. 메시지는 영어다. 화면에 보일 문장은 ``message_ko``에 있다."""

    def __init__(self, error: str, message_ko: str) -> None:
        super().__init__(error)
        self.message_ko = message_ko


def check_project(value: str | None) -> str:
    """프로젝트명을 검사해 그대로 반환한다. 공백을 지우거나 고치지 않는다.
    값이 없거나 규칙에 맞지 않으면 ProjectNameError를 던진다. 메시지는 ``PROJECT_REQUIRED``, ``PROJECT_INVALID``다."""
    if value is None or value == "":
        raise ProjectNameError(*PROJECT_REQUIRED)
    if not isinstance(value, str) or not PROJECT_RE.fullmatch(value):
        raise ProjectNameError(*PROJECT_INVALID)
    return value


def check_flow(flow: str | None) -> str:
    """실행 방식 값을 검사해 반환한다. 없거나 비었으면 ``full``을 반환한다.
    ``RUN_FLOWS`` 밖의 값이면 허용 값을 담은 ValueError를 던진다. 웹은 이것을 400으로 응답한다."""
    if flow is None or flow == "":
        return FLOW_FULL
    if flow not in RUN_FLOWS:
        raise ValueError(f"unknown flow {flow!r} — use one of: {', '.join(RUN_FLOWS)}")
    return flow


def run_llm_calls(run_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
    """실행 상태에 보일 LLM 호출 수를 반환한다. 이관 보고서의 수에 취약점 조치의 수정 루프가 호출한 수를 더한다.
    조치 사용량을 읽지 못하면 0으로 센다. 예외는 던지지 않는다.

    `llm_calls` of `GET /api/runs/{id}` = the migration report's `llm_usage.calls` + the remediation's fix-loop calls of
    every round (`fixer.run_fix_usage`, read from `remediation/state.json` next to the report, so a resume pod's calls
    count too); `remediation_llm_calls` = the second part, so that the page can swap it for the live number of
    `GET /api/remediations/{id}` without counting a call twice. None when the report has no `llm_usage` and no
    remediation call was made. `remediation_llm_usage_unknown` (bool): a part of the remediation's usage is unknown
    (`fix_usage()["usage_unknown"]`: a loop whose usage could not be read, or calls without token counts), so
    `remediation_llm_calls` counts only the known calls."""
    usage = report.get("llm_usage") if isinstance(report.get("llm_usage"), dict) else {}
    own = usage.get("calls")
    own = own if isinstance(own, int) and not isinstance(own, bool) else None
    unknown = False
    try:
        from sbmig.remediations.fixer import run_fix_usage
        fu = run_fix_usage(run_dir)
        rem = int(fu["calls"])
        unknown = bool(fu.get("usage_unknown"))
    except Exception:  # noqa: BLE001 - 상태 응답은 조치 사용량 때문에 실패하면 안 된다
        log.debug("remediation fix usage of %s not readable", run_dir, exc_info=True)
        rem = 0
    total = None if own is None and not rem else (own or 0) + rem
    # 조치 사용량 일부를 알 수 없으면 그렇다고 알린다. 화면이 호출 수를 정확한 값처럼 보이지 않게 한다
    return {"llm_calls": total, "remediation_llm_calls": rem, "remediation_llm_usage_unknown": unknown}


def _report_list(report: dict[str, Any], key: str) -> list[Any]:
    """보고서의 목록 항목을 반환한다. 없거나 목록이 아니면 빈 목록을 반환한다."""
    val = report.get(key)
    return val if isinstance(val, list) else []


def _abort_reason(report: dict[str, Any]) -> str | None:
    # 엔진이 실행을 중단한 사유를 찾는다.
    # Orchestrator._abort가 manual_followups에 "run aborted: <사유>"로 남긴다. 처음 나온 줄이 첫 사유다
    followups = [item for item in _report_list(report, "manual_followups") if isinstance(item, str)]
    for item in followups:
        if item.startswith("run aborted: "):
            return item[len("run aborted: "):].strip() or None
    # 수정 루프가 HTTP 저장소 차단으로 멈추면 "run aborted:" 줄 없이 인수인계 문장만 남는다. 그 문장을 사유로 쓴다
    from sbmig.report.handover import HTTP_BLOCKED_FLAG, HTTP_BLOCKED_PREFIX
    if HTTP_BLOCKED_FLAG in _report_list(report, "flags"):
        for item in followups:
            if item.startswith(HTTP_BLOCKED_PREFIX):
                return item.strip() or None
    return None


def _recorded_flow(rd: Path, records: _RunRecords | None = None) -> str:
    # 메모리에 없는 실행의 실행 방식을 찾는다. request.json(Job 모드), web-status.json(in-process),
    # report.json(엔진) 순으로 보고, 없으면 full로 본다
    records = records or _RunRecords(rd)
    rec = records.request
    if rec is not None:
        return rec.flow or FLOW_FULL
    web = records.web
    if isinstance(web, dict) and web.get("flow") in RUN_FLOWS:
        return web["flow"]
    try:
        data = records.report
    except OSError:
        return FLOW_FULL
    if data is not None and data.get("flow") in RUN_FLOWS:
        return data["flow"]
    return FLOW_FULL


# 사람이 중지한 실행의 엔진 사유는 이 문장으로 시작한다(STOP_REQUEST_REASON, STOP_LOOP_REASON)
USER_STOP_PREFIX = "stopped by user (stop.request)"
assert STOP_REQUEST_REASON.startswith(USER_STOP_PREFIX)

FRAMEWORK_SCOPE = "jdk-jakarta"
# 저널에서 프레임워크 범위를 찾을 때 한 번에 읽는 최대 바이트 수
_FW_READ_CHUNK = 1 << 20


def _framework_entry(src: Any) -> dict[str, Any] | None:
    """줄인 범위(``jdk-jakarta``)로 실행한 기록이면 화면에 보낼 ``{name, scope, fix_loop}``를 반환한다. 아니면 None이다.
    ``fix_loop``가 없는 이전 기록은 True로 본다. 그때는 수정 루프를 실행했다. 이름이 없으면 빈 문자열이다.

    An empty name makes the page say 사내 프레임워크."""
    if not isinstance(src, dict) or src.get("scope") != FRAMEWORK_SCOPE:
        return None
    name = src.get("name")
    fix = src.get("fix_loop")
    return {"name": name.strip() if isinstance(name, str) else "", "scope": FRAMEWORK_SCOPE,
            "fix_loop": fix if isinstance(fix, bool) else True}


def _framework_from_report(data: Any) -> dict[str, Any] | None:
    """report.json의 ``profile.framework``에서 화면용 프레임워크 정보를 만든다."""
    prof = data.get("profile") if isinstance(data, dict) else None
    return _framework_entry(prof.get("framework") if isinstance(prof, dict) else None)


# 변환 완료로 보여 줄 수 있는 실행의 종료 코드. 종료 코드를 기록하지 않은 실행(None)도 완료로 보여 준다
_CONVERTED_EXIT_CODES = (0, 2)


def _conversion_done(data: Any) -> bool:
    # 확인하다 예외가 나면 변환을 마치지 않은 것으로 본다. 보고서 모양이 이상해도 상태 응답은 실패하지 않아야 한다
    from sbmig.report.result_line import conversion_complete
    try:
        return conversion_complete(data) is True
    except Exception:  # noqa: BLE001 - 상태 응답은 이 표시 때문에 실패하면 안 된다
        log.debug("conversion_complete not decidable from the report", exc_info=True)
        return False


@dataclass
class _JournalScan:
    # 실행 하나의 저널을 어디까지 읽었는지와 찾은 프레임워크 이벤트를 담는다
    offset: int = 0
    raw: dict[str, Any] | None = None
    done: bool = False                 # plan 이벤트를 지나면 범위가 정해진다. 더 읽지 않는다


class RunManager:
    """웹에서 올린 이관을 시작하고, 상태와 이벤트 요청에 답하는 클래스. 메모리와 실행 폴더의 정보로 답한다.
    ``_lock``은 ``runs`` dict를 보호한다. ``_turns``는 업로드 순서대로 ``max_parallel``개까지 실행을 들여보낸다."""

    def __init__(self, out_dir: Path, repo_root: Path = PACKAGE_ROOT, *,
                 max_finished_runs: int = DEFAULT_MAX_FINISHED_RUNS, default_configs: list[str] | None = None,
                 mode: str | None = None, job_client: Any = None, job_template: str | Path | None = None,
                 max_parallel: int | None = None):
        self.out_dir = Path(out_dir).resolve()
        self.repo_root = Path(repo_root).resolve()
        self.max_finished_runs = max(0, int(max_finished_runs))
        # in-process는 이 프로세스의 스레드로, k8s-job은 업로드마다 Job 하나로 실행한다. k8s-job의 입력과 결과는 공유 PVC로 주고받는다
        self.mode = (mode or os.environ.get(MODE_ENV, "") or "in-process").strip()
        if self.mode not in MODES:
            raise ValueError(f"{MODE_ENV} must be one of {', '.join(MODES)}, not {self.mode!r}")
        # 개발 PC에서는 k8s-job으로 설정되어 있어도 클러스터 밖이면 local-job으로 실행한다.
        # 운영 파드에는 KUBERNETES_SERVICE_HOST가 항상 있어서 모드가 바뀌지 않는다
        self.effective_mode = self.mode
        # 서비스 계정 토큰이나 KUBECONFIG 파일(/etc/kube/config) 중 하나가 있으면 실제 Job을 만든다
        if self.mode == "k8s-job" and job_client is None and not (in_cluster_available() or kubeconfig_path() is not None):
            if cluster_host_present():
                # 클러스터 안인데 쓸 수 있는 계정이 없으면 시작을 막는다. 웹 파드에서 몰래 이관을 실행하지 않는다
                raise ValueError(f"{MODE_ENV}=k8s-job inside a cluster needs the web pod's service account token "
                                 "(serviceAccountName sbmig-web, deploy/k8s/45-web-rbac.yaml; automountServiceAccountToken on) "
                                 "or a kubeconfig file named by KUBECONFIG (the infra team's /etc/kube/config Secret)")
            self.effective_mode = "local-job"
            log.warning("%s=k8s-job but this process is not inside a Kubernetes cluster — running the Job mode with LOCAL "
                        "subprocesses instead (development); status.mode says local-job", MODE_ENV)
        self._job_client = job_client                     # None이면 첫 Job 때 K8sClient나 LocalJobClient를 만든다
        self.job_template = Path(job_template or os.environ.get(TEMPLATE_ENV, "") or (self.repo_root / DEFAULT_TEMPLATE))
        self.job_poll_s = max(0.05, float(os.environ.get(POLL_ENV, "2") or 2))
        # 중지는 노드나 빌드 라운드 사이에서만 적용된다. 그래서 Job을 지우기 전 유예가 Maven 빌드 한 번(30분)과
        # OpenRewrite 실행보다 길어야 보고서를 잃지 않는다
        self.job_stop_grace_s = max(0.0, float(os.environ.get(STOP_GRACE_ENV, "2700") or 2700))
        # Kubernetes API가 이 시간 동안 응답하지 않으면 로그, 저널, 화면에 한 번 알린다. 실행을 실패로 끝내지는 않는다.
        # API가 응답할 때까지 슬롯을 잡고 있는다. 파드 상태를 모르는 채로 슬롯을 넘기면 다음 이관과 겹친다
        self.job_api_grace_s = max(0.0, float(os.environ.get("SBMIG_JOB_API_GRACE_S", "300") or 300))
        # job-status.json을 받은 뒤 파드 종료를 기다리는 시간. 넘기면 Job을 지우고 사라질 때까지 기다린다.
        # Worker는 그 파일을 마지막에 기록한다. 그래서 지워도 잃는 것이 없고, 끝나지 않는 파드가 대기열을 막지 않는다
        self.job_exit_grace_s = max(0.0, float(os.environ.get(EXIT_GRACE_ENV, "120") or 120))
        # 파드가 끝났는데 job-status.json이 보이지 않으면 잠시 기다린다.
        # NFS 속성 캐시(기본 최대 60초) 때문에 파일이 늦게 보일 수 있다. 그래서 캐시보다 길어야 한다
        self.job_status_grace_s = max(0.0, float(os.environ.get(STATUS_GRACE_ENV, "90") or 90))
        # 파드가 이 초 동안 시작하지 못하면 이유를 찾아 화면에 알린다. 0이면 알리지 않는다
        self.job_pending_notice_s = max(0.0, float(os.environ.get(PENDING_NOTICE_ENV, "300") or 300))
        self._events_readable = True           # 시작 점검에서 이벤트 조회 권한이 없으면 False가 된다
        # 환경 변수 고정 정책(SBMIG_JOB_ENV_PIN)의 기본값은 k8s-job에서 refuse, local-job에서 off다.
        # 로컬 Worker는 이 프로세스의 환경을 물려받는다. 그래서 .env를 고치면 잘못된 경고만 난다.
        # recover()가 Job을 만들 수 있어서 그보다 먼저 정한다
        pin_default = contract.ENV_PIN_REFUSE if self.effective_mode == "k8s-job" else contract.ENV_PIN_OFF
        pin_raw = (os.environ.get(contract.ENV_PIN_POLICY_ENV) or "").strip()
        self.env_pin_policy = contract.env_pin_policy(pin_raw, pin_default)
        if pin_raw and pin_raw.lower() not in contract.ENV_PIN_POLICIES:
            log.warning("%s=%r is not one of %s — using the default %s", contract.ENV_PIN_POLICY_ENV, pin_raw,
                        " | ".join(contract.ENV_PIN_POLICIES), self.env_pin_policy)
        # 서버가 정한 기본 설정 파일 이름(sbmig serve --config, SBMIG_WEB_CONFIGS). 화면에서 시작한 실행은 이 설정을 쓴다
        self._default_configs = [c.strip() for c in (default_configs or []) if c and c.strip()]
        self._missing_warned: set[str] = set()
        self.runs: dict[str, Run] = {}
        # 실행 id별로 이 프로세스가 아는 마지막 조치 재개 시도. 이관의 runs와 따로 둔다. 상태가 섞이지 않게 한다
        self.resumes: dict[str, Run] = {}
        self._lock = threading.Lock()
        # 실행별 저널에서 찾은 프레임워크 범위와 기본 설정의 프레임워크 정보. 목록과 화면이 자주 조회해서 캐시해 둔다
        self._fw_lock = threading.Lock()
        self._fw_scans: dict[str, _JournalScan] = {}
        self._fw_config: tuple[Any, dict[str, Any]] | None = None
        # 동시 Job 수는 인자, SBMIG_JOB_MAX_PARALLEL, 1 순으로 정한다.
        # 실행 모드가 정해진 뒤, recover()가 슬롯을 잡기 전에 정해야 한다
        self.max_parallel = _resolve_max_parallel(max_parallel, self.effective_mode)
        self._turns = _FifoTurns(self.max_parallel)
        if self.effective_mode in ("k8s-job", "local-job"):
            log.info("web: up to %d migration job(s) at a time (%s), the rest wait in upload order", self.max_parallel,
                     contract.MAX_PARALLEL_ENV)
        else:
            log.info("web: one migration at a time (%s mode)", self.effective_mode)
        self.closing = threading.Event()       # 서버 종료. 열린 SSE 스트림을 닫고 실행 중인 실행에 중지를 요청한다
        self._job_template_doc: dict | None = None   # k8s-job에서 시작할 때 검증한 템플릿. Job은 이것으로 만든다
        self.recovered: list[str] = []         # 시작할 때 recover()가 control/state.json에서 이어받은 실행 id
        # k8s-job에서는 잘못된 템플릿, 권한, 접속 정보를 첫 업로드 때가 아니라 시작할 때 거부한다. 그러면 앱이 뜨지 않는다
        if self.effective_mode == "k8s-job":
            self._startup_k8s(constructed=job_client is None)
        if self.effective_mode in ("k8s-job", "local-job"):
            self.recover()
        if self.effective_mode != "local-job":
            self._warn_missing_settings_file()

    def free_space_problem(self, configs: list[str] | None = None) -> str | None:
        """출력 볼륨의 여유 공간이 ``fleet.min_free_gb``보다 작으면 거절 문장을 반환한다. 충분하면 None을 반환한다.
        값이 0이면 확인하지 않는다. 설정을 읽지 못하거나 공간을 잴 수 없을 때도 막지 않는다.
        설정 오류는 접수할 때 알린다."""
        from sbmig.core.config import load_config
        from sbmig.fleet.preflight import check_disk
        names = self.default_configs() if configs is None else list(configs)
        try:
            if any(not RUN_ID_RE.match(c) or not (self.repo_root / "config" / c).is_file() for c in names):
                return None
            cfg = load_config(*[self.repo_root / "config" / c for c in names], repo_root=self.repo_root)
            floor = float(cfg.fleet.min_free_gb or 0)
            check = check_disk(self.out_dir, floor)
        except Exception:  # noqa: BLE001 - 여유 공간 확인이 접수 자체를 깨뜨리면 안 된다
            log.debug("free space check skipped", exc_info=True)
            return None
        if check.ok or check.skipped:
            return None
        free = (check.data or {}).get("free_gb")
        return (f"not enough free space on the output volume: {free:.1f} GB free on {self.out_dir}, at least {floor:g} GB "
                f"needed (fleet.min_free_gb) — free space on the volume (old run folders: sbmig clean) or enlarge it")

    def _warn_missing_settings_file(self) -> None:
        """기본 설정의 ``build.settings_file`` 파일이 없거나 비어 있으면 WARNING 한 줄을 남긴다. 시작은 막지 않는다.

        In the cluster it is a plain file the operator copies onto the data volume; the run's preflight aborts
        a run that starts without a usable file."""
        try:
            from sbmig.core.config import load_config
            configs = self.default_configs()
            cfg = load_config(*[self.repo_root / "config" / c for c in configs], repo_root=self.repo_root)
            value = cfg.build.settings_file
            if value is None or not str(value).strip() or str(value).strip().lower() == "null":
                return
            path = Path(str(value).strip()).expanduser().absolute()
            if not path.is_file():
                log.warning("Maven settings file %s is missing — copy settings.xml there before the first run "
                            "(deploy/README.md §3)", path)
            elif path.stat().st_size == 0:
                log.warning("Maven settings file %s is empty — copy settings.xml there before the first run "
                            "(deploy/README.md §3)", path)
        except Exception as e:  # noqa: BLE001 - 시작 점검은 앱 시작을 막지 않는다
            log.debug("web: settings file check skipped (%s: %s)", type(e).__name__, e)

    # ---- k8s-job 시작 검증
    def _startup_k8s(self, constructed: bool) -> None:
        """k8s-job 모드의 시작 검증을 한다. 클라이언트를 만들고, Worker 템플릿과 권한을 검사한다.
        하나라도 실패하면 ValueError를 던진다. 그러면 앱이 뜨지 않는다.
        테스트가 클라이언트를 넣었으면 생성을 건너뛴다. 이때 템플릿은 파일이 있을 때만, 마운트 규칙 없이 검사한다."""
        if constructed:
            try:
                self._job_client = K8sClient.connect()
            except K8sError as e:
                raise ValueError(f"{MODE_ENV}=k8s-job cannot reach the Kubernetes API: {e}") from e
        if constructed or Path(self.job_template).is_file():
            try:
                # 테스트가 넣은 클라이언트는 실제 클러스터가 아니다. 채우지 않은 템플릿을 그대로 써도 된다
                self._job_template_doc = validate_worker_template(self.job_template, self.out_dir if constructed else None,
                                                                  allow_placeholders=not constructed)
            except K8sError as e:
                raise ValueError(str(e)) from e
        self._check_permissions(self._job_client)

    def _check_permissions(self, client: Any) -> None:
        """시작할 때 클러스터 권한을 점검한다. 점검 목록은 ``deploy/k8s/45-web-rbac.yaml``과 같다.
        ``can_i``가 없는 클라이언트(가짜 구현, 로컬)는 건너뛴다.
        하나라도 거부되면 막힌 권한을 담은 ValueError를 던진다."""
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
        # 이벤트 조회 권한이 없어도 시작은 막지 않는다. 파드가 시작하지 못한 이유를 보여 줄 때만 쓴다
        try:
            if not can_i("list", "events", ""):
                self._events_readable = False
                log.info(EVENTS_UNREADABLE_LINE)
        except K8sError as e:
            log.debug("cannot check the events permission (%s) — events are asked when needed", e)

    # ---- 재시작 복구
    def recover(self) -> list[str]:
        """서버가 다시 뜬 뒤 끝나지 않은 실행을 복구하고, 복구한 id 목록을 반환한다.
        ``<out>/*/control/``의 제어 상태를 읽는다. 깨진 파일은 로그만 남기고 건너뛴다. 예외는 던지지 않는다.
        - k8s-job: ``queued``는 Job을 새로 만들고, ``creating``은 같은 이름의 Job이 있으면 이어받는다
        - k8s-job: ``created``, ``running``은 Job을 이어받아 감시만 한다
        - local-job: 서브프로세스는 서버와 함께 종료되었다. ``lost``로 기록한다
        - Job이 있을 수 있는 실행이 기록된 슬롯을 먼저 잡고, 그다음 ``queued`` 실행을 줄에 세운다

        Within each group the order is `accepted_at`."""
        if self.effective_mode not in ("k8s-job", "local-job"):
            return self.recovered
        try:
            # 조치 재개 시도의 Job도 클러스터에서 실행 중일 수 있다. 그 제어 상태도 같은 규칙으로 복구한다
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
                    # 로컬 파드는 이 서버의 자식 프로세스라서 서버와 함께 종료되었다. 이어받을 것이 없다
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
            except Exception as e:  # noqa: BLE001 - 복구가 웹 앱 시작을 막으면 안 된다
                log.warning("recovery: %s skipped (%s: %s)", sp, type(e).__name__, e)
        ordered = sorted(pending, key=lambda t: (t[0], t[1].name))
        # Job이 이미 실행 중이라 슬롯을 가진 실행을 먼저, 대기하던 실행을 그 뒤에 처리한다
        holding = [t for t in ordered if t[2].state != RunState.QUEUED.value]
        waiting = [t for t in ordered if t[2].state == RunState.QUEUED.value]
        for _at, rd, cs, rec in holding + waiting:
            try:
                self._resume(rd, cs, rec)
            except Exception as e:  # noqa: BLE001 - 복구가 웹 앱 시작을 막으면 안 된다
                log.warning("recovery: run %s not resumed (%s: %s)", cs.run_id, type(e).__name__, e)
        return self.recovered

    def _resume(self, rd: Path, cs: ControlState, rec: RequestRecord) -> None:
        """복구한 실행 하나를 메모리에 다시 등록하고 감시 스레드를 시작한다.
        ``queued`` 실행은 줄 끝에 선다. Job이 있을 수 있는 실행은 기록된 슬롯을 한도와 상관없이 바로 잡는다.
        ``slot``이 없는 state.json은 1번 슬롯으로 본다."""
        mode = {RunState.QUEUED.value: None, RunState.CREATING.value: "probe"}.get(cs.state, "adopt")
        action = cs.action or None
        run = Run(rd.name, rd, rd / SOURCE_ZIP, list(rec.configs or []), rec.stop_after, rec.llm_provider, job=True,
                  request=rec, control=cs, recover=mode, action=action, flow=rec.flow or FLOW_FULL,
                  upload=rec.upload_name or rec.upload, project=rec.project or None)
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
                # Job이 이미 실행 중이라서 슬롯을 잡아 둔다. 새 업로드가 같은 번호의 Maven 로컬 저장소를 받지 않게 한다.
                # probe가 Job을 직접 만들면 이 번호를 creating 상태에 기록한다
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

    # ---- 경로
    def run_dir(self, run_id: str) -> Path:
        """실행 폴더 ``<out>/<run id>``의 경로를 반환한다. 호출하는 라우트가 먼저 ``RUN_ID_RE``로 id를 검사한다."""
        return self.out_dir / run_id

    def configs(self) -> list[str]:
        """``config/*.yml`` 이름 목록을 반환한다. ``default.yml``은 빠진다. 항상 먼저 읽히기 때문이다."""
        d = self.repo_root / "config"
        return sorted(p.name for p in d.glob("*.yml") if p.name != "default.yml") if d.is_dir() else []

    # 아무것도 지정하지 않았을 때 쓰는 이름. 회사의 artifacts.yml, 개발 PC의 로컬 e2e 설정 중 먼저 있는 파일을 쓴다
    FALLBACK_CONFIGS = ("artifacts.yml", "artifacts.local-e2e.yml")

    def default_configs(self) -> list[str]:
        """실행에 쓸 기본 설정 파일 이름 목록을 반환한다. ``config/`` 안의 파일이고 default.yml 다음에 읽힌다.
        ``sbmig serve --config``, ``SBMIG_WEB_CONFIGS``로 지정한 이름 중 지금 있는 파일만 쓴다. 없는 이름은 한 번 경고한다.
        지정한 이름이 없으면 :attr:`FALLBACK_CONFIGS` 중 처음 있는 파일을 쓴다. 그것도 없으면 빈 목록이다."""
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

    def framework_info(self) -> dict[str, Any]:
        """기본 설정의 사내 프레임워크 정보 ``{configured, name, fix_loop, remediation_always}``를 반환한다.
        설정 파일이 바뀌지 않았으면 캐시한 값을 쓴다. 설정을 읽지 못하면 configured가 False다.

        `GET /api/configs`. `configured` = framework artifacts are listed AND `framework.scope` is
        `jdk-jakarta` (with `full` such a project gets the whole migration, so the page has nothing to
        announce). `remediation_always` = such a run chains the remediation whatever its build result
        (`remediation.after_migration` on, `remediation.after_failed_build: framework`), whether or not the
        fix loop runs. Cached per (file, mtime) of the package default.yml + the default configs. Contract:
        v2.1z-aw."""
        off: dict[str, Any] = {"configured": False, "name": "", "fix_loop": False, "remediation_always": False}
        try:
            from sbmig.core.config import DEFAULT_CONFIG_PATH
            d = self.repo_root / "config"
            names = self.default_configs()
            files = [Path(DEFAULT_CONFIG_PATH), *[d / c for c in names]]
            key = tuple((str(p), p.stat().st_mtime_ns if p.is_file() else None) for p in files)
        except Exception:  # noqa: BLE001 - 설정 정보 때문에 /api/configs가 실패하면 안 된다
            log.debug("framework info: config files not readable", exc_info=True)
            return dict(off)
        with self._fw_lock:
            hit = self._fw_config
        if hit is not None and hit[0] == key:
            return dict(hit[1])
        out = dict(off)
        try:
            from sbmig.core.config import load_config
            cfg = load_config(*files[1:], repo_root=self.repo_root)
            fw = cfg.framework
            if fw.patterns() and fw.scope == FRAMEWORK_SCOPE:
                rem = getattr(cfg, "remediation", None)
                # 프레임워크 프로젝트는 빌드가 실패해도 취약점 조치를 이어서 한다. 수정 루프를 실행했는지와 상관없다
                always = (bool(getattr(rem, "after_migration", False))
                          and str(getattr(rem, "after_failed_build", "never") or "never") == "framework")
                out = {"configured": True, "name": fw.display_name(), "fix_loop": bool(fw.fix_loop),
                       "remediation_always": always}
        except Exception:  # noqa: BLE001 - 읽지 못한 설정은 프레임워크 없음으로 알린다
            log.debug("framework info: default configs cannot be loaded", exc_info=True)
        with self._fw_lock:
            self._fw_config = (key, out)
        return dict(out)

    def _journal_framework(self, run_id: str, rd: Path) -> dict[str, Any] | None:
        """report.json이 아직 없는 실행의 프레임워크 정보를 저널에서 찾아 반환한다.
        ``framework_project``, ``plan`` 이벤트를 본다. 지난번에 읽은 곳부터 이어서 읽고, plan 이벤트를 지나면 멈춘다.

        Incremental (offset per run, whole lines only) because `list_runs` asks for every run every 15 s.
        A later event's `name` / `fix_loop` fill in an earlier one's."""
        path = rd / "journal.jsonl"
        with self._fw_lock:
            scan = self._fw_scans.setdefault(run_id, _JournalScan())
            if not scan.done:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = -1
                if 0 <= size < scan.offset:        # 다시 만든 파일이면 처음부터 읽는다
                    scan.offset, scan.raw = 0, None
                while 0 <= scan.offset < size and not scan.done:
                    with open(path, "rb") as fh:
                        fh.seek(scan.offset)
                        chunk = fh.read(_FW_READ_CHUNK)
                    end = chunk.rfind(b"\n")
                    if end < 0:
                        if len(chunk) < _FW_READ_CHUNK:
                            break                  # 아직 쓰는 중인 줄
                        scan.offset += len(chunk)  # 한 줄이 읽기 단위보다 길면 그 부분은 건너뛴다
                        continue
                    scan.offset += end + 1
                    for line in chunk[:end].split(b"\n"):
                        if b'"framework_project"' not in line and b'"plan"' not in line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(ev, dict) or ev.get("event") not in ("framework_project", "plan"):
                            continue
                        if ev.get("scope") == FRAMEWORK_SCOPE:
                            merged = dict(scan.raw or {})
                            merged.update({k: ev[k] for k in ("scope", "name", "fix_loop") if k in ev})
                            scan.raw = merged
                        if ev.get("event") == "plan":
                            scan.done = True
                            break
            return _framework_entry(scan.raw)

    # ---- 실행 시작과 종료
    def incoming_path(self) -> Path:
        """받는 중인 업로드 본문을 기록할 임시 파일 경로(``<out>/_uploads/.incoming-<hex>.part``)를 새로 만든다.
        최종 zip과 같은 폴더다. 그래서 ``start_file``이 복사 없이 이름만 바꾼다."""
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        return uploads / f".incoming-{uuid.uuid4().hex}.part"

    def start(self, zip_bytes: bytes, name: str, configs: list[str], stop_after: str | None = None,
              llm_provider: str | None = None, flow: str | None = None, project: str | None = None) -> str:
        """메모리에 있는 zip 본문으로 실행을 시작한다. 테스트나 다른 코드에서 쓴다.
        ``start_file``과 똑같이 동작한다. 잘못된 요청이면 ValueError를 던진다."""
        tmp = self.incoming_path()
        tmp.write_bytes(zip_bytes)
        try:
            return self.start_file(tmp, name, configs, stop_after, llm_provider, flow, project=project)
        finally:
            tmp.unlink(missing_ok=True)

    def start_file(self, incoming: Path, name: str, configs: list[str], stop_after: str | None = None,
                   llm_provider: str | None = None, flow: str | None = None, project: str | None = None) -> str:
        """업로드된 zip으로 새 실행을 시작하고 실행 id를 반환한다.
        입력 검사 -> zip 이동 -> 설정, 워크스페이스, 오케스트레이터 생성 -> 작업 스레드 시작 순서다.
        잘못된 요청은 ValueError를 던진다. 이때 ``incoming`` 파일은 호출한 쪽이 지운다. 옮긴 뒤에는 실행이 관리한다.
        zip이 깨져서 워크스페이스를 만들지 못하면 실행 폴더와 zip을 남기지 않는다."""
        from sbmig.core.config import load_config
        from sbmig.core.workspace import Workspace, ZipLimits
        from sbmig.loop import Orchestrator, parse_stop_after

        # 설정 이름도 RUN_ID_RE로 검사한다. ``../``로 config 폴더 밖의 파일을 읽지 못하게 한다
        for c in configs:
            if not RUN_ID_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        phase = parse_stop_after(stop_after)                  # 잘못된 값이면 허용 값을 담은 ValueError를 던진다
        stop_after = phase.value if phase is not None else None
        flow = check_flow(flow)                               # 잘못된 값이면 허용 값을 담은 ValueError를 던진다
        if project is not None:                               # 라우트가 이미 검사했다. 다른 호출자도 같은 규칙을 따르게 한다
            project = check_project(project)
        if flow == FLOW_REMEDIATION and stop_after:
            # 조치만 하는 실행에는 멈출 이관 단계가 없다. 거부한다. CLI도 두 옵션을 함께 받지 않는다
            raise ValueError("stop_after cannot be combined with flow=remediation (that flow runs no migration phase)")
        overrides: dict[str, Any] = {}
        if llm_provider:
            overrides["llm"] = {"provider": llm_provider}
        cfg = load_config(*[self.repo_root / "config" / c for c in configs], overrides=overrides or None,
                          repo_root=self.repo_root)        # pydantic ValidationError도 ValueError다

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        shown = display_upload_name(name)                     # 화면에 보여 줄 원래 파일 이름
        run_id = self._claim_run_id(name, project)
        if self.effective_mode in ("k8s-job", "local-job"):
            return self._start_job(incoming, run_id, safe, list(configs), stop_after, llm_provider, flow, shown, project)
        uploads = self.out_dir / "_uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        zip_path = uploads / f"{run_id}-{safe}"
        Path(incoming).replace(zip_path)
        try:
            ws = Workspace.from_input(zip_path, self.out_dir, run_id=run_id, limits=ZipLimits.from_config(cfg.intake))
            if ws.intake_refusal:
                # 풀기 한도를 넘은 zip은 아무것도 풀지 않았다. 실행을 만들지 않고 사유를 400으로 답한다
                raise ValueError(ws.intake_refusal)
        except Exception:                      # 실행이 시작되지 않았다. 빈 실행 폴더를 남기지 않는다
            shutil.rmtree(self.out_dir / run_id, ignore_errors=True)
            zip_path.unlink(missing_ok=True)
            raise
        run = Run(run_id, ws.run_dir, zip_path, list(configs), stop_after, llm_provider, flow=flow, upload=shown,
                  project=project)
        run.orch = Orchestrator(cfg, ws)
        # 어떤 설정으로 실행했는지 journal 첫 줄에 기록한다. journal만 보고도 알 수 있게 한다
        try:
            ws.journal({"event": "web_run", "configs": list(configs), "stop_after": stop_after, "llm_provider": llm_provider,
                        "flow": flow, "upload": shown, "project": project})
        except OSError as e:                   # 참고용 줄이다. 기록하지 못해도 실행은 거부하지 않는다
            log.debug("run %s: web_run journal line not written: %s", run_id, e)
        # daemon 스레드라서 서버와 함께 끝난다. 정리는 shutdown의 중지 요청과 Maven 종료가 맡는다
        run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-run-{run_id}", daemon=True)
        with self._lock:
            self.runs[run_id] = run
            # 대기열 순서는 업로드를 받은 지금 정한다. 스레드가 시작되는 순서는 보장되지 않는다
            self._turns.join(run_id)
        try:
            run.thread.start()
        except RuntimeError:                   # 스레드가 없으면 줄에서 뺀다. 그래야 뒤의 실행이 계속 기다리지 않는다
            self._turns.leave(run_id)
            with self._lock:
                self.runs.pop(run_id, None)
            raise
        log.info("run %s started: %s configs=%s stop_after=%s llm_provider=%s flow=%s", run_id,
                 zip_path.name, ",".join(configs) or "-", stop_after or "-", llm_provider or "-", flow)
        return run_id

    def _claim_run_id(self, upload_name: str, project: str | None = None) -> str:
        """새 실행 id를 만들고 실행 폴더를 배타적으로 생성한다. 프로젝트명이 있으면 id가 그 이름으로 시작한다.
        폴더가 이미 있거나 메모리에 같은 id가 있으면 다시 만든다. 100번 실패하면 OSError를 던진다."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            rid = run_id_for(upload_name, project=project) if project else run_id_for(upload_name)
            if rid in self.runs or not RUN_ID_RE.match(rid):
                continue
            try:
                (self.out_dir / rid).mkdir()
            except FileExistsError:
                continue
            return rid
        raise OSError("no free run id after 100 tries")

    def _start_job(self, incoming: Path, run_id: str, safe_name: str, configs: list[str], stop_after: str | None,
                   llm_provider: str | None, flow: str = FLOW_FULL, upload_name: str | None = None,
                   project: str | None = None) -> str:
        """Job 모드에서 실행을 시작한다. 이 프로세스는 이관을 직접 실행하지 않는다.
        zip을 공유 볼륨의 실행 폴더에 ``source.zip``으로 옮기고 request.json과 queued 상태를 기록한다.
        그다음 Job을 만들고 감시할 작업 스레드를 시작한다."""
        run_dir = self.out_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        zip_path = run_dir / SOURCE_ZIP
        Path(incoming).replace(zip_path)
        # request.json과 queued 상태를 작업 스레드보다 먼저 기록한다. 이 프로세스가 바로 죽어도 다음 Main이 이어받는다
        try:
            request = RequestRecord(run_id=run_id, accepted_at=utc_now(), upload=safe_name, configs=list(configs),
                                    zip_sha256=sha256_file(zip_path), config_digests=config_digests(self.repo_root, configs),
                                    sbmig_version=sbmig_version(), mode=self.effective_mode, stop_after=stop_after,
                                    llm_provider=llm_provider, attempt=1, flow=flow, upload_name=upload_name,
                                    project=project)
            request.write(run_dir)
            control = ControlState(run_id=run_id, state=RunState.QUEUED.value, attempt=request.attempt,
                                   accepted_at=request.accepted_at)
            control.write(run_dir)
        except OSError:                        # 실행이 시작되지 않았다. 반쯤 접수된 폴더를 남기지 않는다
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        run = Run(run_id, run_dir, zip_path, configs, stop_after, llm_provider, job=True, request=request, control=control,
                  flow=flow, upload=upload_name or safe_name, project=project)
        _append_journal(run_dir, {"event": "web_run", "run_id": run_id, "mode": self.effective_mode, "upload": safe_name,
                                  "configs": configs, "stop_after": stop_after, "llm_provider": llm_provider, "flow": flow,
                                  "upload_name": upload_name, "project": project})
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
        """실행 하나를 처리하는 작업 스레드 본체.
        차례가 올 때까지 ``queued``로 기다리고, 차례가 오면 ``active``가 된다. Job 실행은 슬롯 번호를 ``run.slot``에 둔다.
        in-process는 이 스레드 전용 ``sbmig.log``를 붙여 이관을 실행한다. 끝나면 ``SBMIG-RESULT`` 줄을 남긴다.
        어떤 경우에도 마지막에 ``finished`` 이벤트와 web-status.json을 기록한다. 그리고 ``done``을 세워 SSE를 깨운다."""
        log_path: Path | None = None
        settled = False                        # Job 실행의 종료 상태를 슬롯을 가진 채로 기록했거나 시도했다
        try:
            # import도 try 안에서 한다. 여기서 실패해도 finally가 줄에서 빼야 뒤의 실행이 계속 기다리지 않는다
            from sbmig.cli import attach_file_log, run_exit_code

            # 차례는 start_file에서 정한 업로드 순서를 따른다
            try:
                with self._turns.turn(run.key) as slot:
                    if run.job:
                        run.slot = slot
                    run.active.set()
                    with run.cond:                         # SSE 스트림이 다음 주기를 기다리지 않고 바로 running을 보게 한다
                        run.cond.notify_all()
                    if run.job:                            # Job 모드에서는 이 실행의 sbmig.log를 파드가 기록한다
                        try:
                            run.exit_code = self._run_job(run)
                        except Exception as e:  # noqa: BLE001 - 버그로 예외가 나도 종료 상태는 기록해야 한다
                            self._record_crash(run, e)
                        finally:
                            # 종료 상태는 슬롯을 가진 채로 기록한다. 먼저 비우면 다음 실행이 같은 번호를 기록해서
                            # 재시작 뒤 두 상태가 한 번호를 갖는다. 기록이 실패해도 로그만 남기고 슬롯은 비운다
                            settled = True
                            self._close_job_run(run)
                        return
                    # 이 스레드와, 컨텍스트를 물려받은 LangGraph 실행기 스레드의 로그만 이 파일에 기록한다.
                    # 다른 실행이나 서버 로그는 섞이지 않는다
                    log_path = attach_file_log(run.run_dir / "sbmig.log", thread_id=threading.get_ident())
                    # 어떤 설정으로 실행했는지 sbmig.log 첫 줄에 남긴다
                    log.info("run %s: configs=%s stop_after=%s llm_provider=%s flow=%s", run.run_id,
                             ",".join(run.configs) or "-", run.stop_after or "-", run.llm_provider or "-", run.flow)
                    report = self._run_thread_body(run)
                    run.report = report
                    run.exit_code = run_exit_code(report) if report is not None else EXIT_ABORTED
                    if report is not None:          # 운영자가 옮겨 적는 결과 줄을 이 실행의 sbmig.log에도 남긴다
                        from sbmig.report.result_line import report_result_line
                        log.info("%s", report_result_line(report, run.exit_code, run_id=run.run_id))
            except _LeftLine:
                # 대기하던 Job 실행이 중지 요청으로 줄에서 빠졌다. 차례가 와서 cancel을 본 경우처럼 Job 없이 끝낸다
                log.info("run %s: stopped while waiting — left the line", run.run_id)
                run.exit_code = self._stopped_before_create(run)
        except Exception as e:  # noqa: BLE001 - 오케스트레이터는 예외를 던지지 않는다. 여기 오면 버그다
            self._record_crash(run, e)
        finally:
            self._turns.leave(run.key)         # 차례를 받기 전에 실패한 경우에도 줄에서 뺀다
            run.active.set()
            log.info("run %s%s finished: exit code %s", run.run_id, f" ({run.action})" if run.action else "", run.exit_code)
            if log_path is not None:
                _detach_file_log(log_path)
            if run.job:
                if not settled:                # 슬롯을 받지 못했다. 줄에서 빠졌거나 차례 전에 실패했다
                    self._close_job_run(run)
                success = run.success
            else:
                # 출력 마스킹이 켜져 있으면 done 전에 마지막 마스킹을 한다. 그 전까지 file_withheld가 파일을 막는다
                if output_scrubbing():
                    self._final_redaction(run)
                success = getattr(run.report, "success", None)
                run.success = success
            self._push(run, "finished", {"exit_code": run.exit_code, "success": success})
            # web-status.json은 이관의 결과다. 재개 시도에서는 기록하지 않는다
            if not run.keep_state and not run.action:   # 서버 종료로 남겨 둔 Job은 state.json이 상태를 말한다
                _write_web_status(run)
            run.done.set()
            with run.cond:
                run.cond.notify_all()
            with self._lock:
                self._prune_locked()

    def _record_crash(self, run: Run, e: BaseException) -> None:
        # 작업 스레드 안의 예상하지 못한 예외는 엔진이나 이 모듈의 버그다. 실행을 실패로 기록하고 화면에 알린다
        log.exception("run %s failed", run.run_id)
        run.error = scrub(f"{type(e).__name__}: {e}")
        run.exit_code = EXIT_ABORTED
        if run.job:
            run.final_state = run.final_state or RunState.FAILED.value
        self._push(run, "error", {"message": run.error})

    def _final_redaction(self, run: Run) -> None:
        # 기록할 때 마스킹되지 않는 파일(Maven 출력 logs/, 보고서)을 마지막으로 마스킹한다. 실패해도 로그만 남긴다
        try:
            from sbmig.web.redact import redact_run_dir
            redact_run_dir(run.run_dir)
        except Exception:  # noqa: BLE001
            log.exception("run %s: the final output redaction failed", run.run_id)

    def _close_job_run(self, run: Run) -> None:
        """Job 실행을 마무리하고 ``run.success``를 정한다. 슬롯을 비우기 전에 호출한다. 예외는 던지지 않는다.
        - 결과 파일 없이 끝난 Job은 Worker의 마스킹을 거치지 않았을 수 있다. 그래서 Main이 대신 마스킹한다
        - 이 마스킹은 Job 종료를 확인했을 때만 한다. 서버 종료로 남겨 두는 Job은 건드리지 않는다
        - 그다음 ``_settle_job_run``으로 종료 상태를 기록한다"""
        try:
            if run.verified is not True and run.job_phase in TERMINAL_PHASES and not run.keep_state:
                self._final_redaction(run)
            run.success = self._settle_job_run(run)
        except Exception:  # 여기서 멈추지 않는다. 호출한 쪽의 블록이 슬롯을 비워야 한다
            log.exception("run %s: settling the job run failed", run.run_id)

    def _stopped_before_create(self, run: Run) -> int:
        # 대기 중에 중지되면 Job을 만들지 않고 stopped로 끝낸다. 차례가 와서 중지를 본 경우와 줄에서 빠진 경우 모두 같다
        run.error = "stopped before the job was created"
        run.final_state = RunState.STOPPED.value
        return EXIT_ABORTED

    def _job_client_or_fail(self) -> Any:
        """Job 클라이언트를 반환한다. 처음 호출될 때 ``_lock`` 안에서 한 번만 만든다.
        감시 스레드마다 따로 만들면 로컬 클라이언트의 프로세스 목록이 나뉘기 때문이다."""
        with self._lock:
            if self._job_client is None:
                if self.effective_mode == "local-job":
                    from sbmig.web.localjob import LocalJobClient
                    self._job_client = LocalJobClient(self.repo_root)
                else:
                    self._job_client = K8sClient.connect()
            return self._job_client

    def _turn_words(self, run: Run) -> tuple[str, str]:
        # 끝나지 않은 Job이 무엇을 막는지 알리는 문구. 슬롯이 하나면 다음 이관이 기다린다. 여럿이면 그 슬롯만 막힌다
        if self.max_parallel <= 1:
            return "its turn", ", the next migration waits"
        return (f"its slot {run.slot}" if run.slot is not None else "its slot"), ""

    def _settle_job_run(self, run: Run) -> bool | None:
        """Job 실행의 결과를 확정하고 제어 상태를 종료 상태로 기록한다. 서버 종료로 Job을 남겨 둘 때는 기록하지 않는다.
        - 결과를 믿는 경우는 둘이다. 이 실행, 이 시도의 ``job-status.json``이 있거나, 컨테이너 종료 코드와 ``report.json``이 있을 때다
        - 믿을 수 없으면 success는 None이고, 오류에 "result not verified"가 붙는다"""
        if run.action:
            # 재개 시도에서는 이관의 report.json이 이 시도의 결과가 아니다. 결과 파일이나 컨테이너 종료 코드만 본다.
            # 조치 결과는 remediation/state.json에 있다. 이관의 success는 바뀌지 않는다
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
            except OSError as e:               # 메모리 상태로 계속 응답한다. 다음 Main은 마지막으로 기록된 상태를 본다
                log.error("run %s: control state %s not written: %s", run.run_id, final, e)
        return success

    def _transition(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """제어 상태를 바꾸고 바로 제어 파일에 원자적으로 기록한다.
        OSError는 그대로 던진다. creating 상태를 기록하지 못하면 Job을 만들지 않는다."""
        if run.control is None:                # 접수 기록 없이 만든 실행(다른 코드에서 호출)이면 새로 만든다
            run.control = ControlState(run_id=run.run_id, action=run.action)
        cs = run.control.transition(state, **changes)
        # 이관 시도는 기본 파일(state.json)에, 재개 시도는 자기 파일에 기록한다
        cs.write(run.run_dir, run.control_file) if run.action else cs.write(run.run_dir)

    def _owned(self, run: Run, st: JobState) -> bool:
        """이 Job이 이 요청의 것인지 확인한다. ``sbmig-owner``와 ``sbmig-run-id`` 라벨이 둘 다 맞아야 한다.
        라벨이 없는 Job은 우리 것이 아니다. 이어받지도 지우지도 않는다.
        라벨을 알 수 없는 클라이언트(로컬 서브프로세스, 테스트용 가짜 구현)일 때만 이름으로 판단한다.

        `labels is None` = a client that cannot report labels at all."""
        labels = getattr(st, "labels", None)
        if labels is None:
            return True
        return labels.get(OWNER_LABEL) == run.owner and labels.get(RUN_LABEL) == run.run_id

    def _accept_status(self, run: Run) -> int | None:
        """``job-status.json``이 이 실행, 이 시도의 것이면 종료 코드를 반환하고 verified를 켠다. 아니면 None이다.
        다른 실행이나 이전 시도의 파일은 한 번 경고하고 무시한다."""
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
        """Job 모드에서 실행 하나를 처리하고 종료 코드를 반환한다. Job을 만들거나 이어받은 뒤 ``_watch_job``을 호출한다.
        - 만들기 전에 ``creating`` 상태와 Job 이름을 기록한다. 응답이 불확실하면 같은 이름의 Job을 이어받는다
        - 대기 중에 중지 요청이나 서버 종료가 오면 Job을 만들지 않는다
        - Job이 끝났거나 없어진 것을 API로 확인한 뒤에만 반환한다. 반환하면 다음 차례로 넘어간다
        - 실패해도 우리 Job은 지우고 사라질 때까지 기다린다. 서버 종료와 우리 것이 아닌 Job만 예외다"""
        name = run.job_name or job_name(run.run_id)
        client: Any = None
        try:
            if run.recover is None:
                # 대기 중에 중지나 서버 종료가 왔으면 Job을 만들지 않는다. 서버 종료 중에 대기열의 Job이 만들어지면 안 된다
                if run.cancel.is_set():
                    return self._stopped_before_create(run)
                if self.closing.is_set():
                    run.error = "web server shut down before the job was created"
                    run.keep_state = True          # 파일에는 queued로 남아 있다. 다음 Main이 Job을 만든다
                    return EXIT_ABORTED
            client = self._job_client_or_fail()
            if run.recover == "adopt":
                # 이름만으로 이어받지 않는다. 같은 이름의 Job이 있으면 라벨이 이 요청의 것이어야 한다.
                # 없으면 끝나서 지워진 Job으로 보고, 볼륨의 결과 파일로 결과를 정한다
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
        except Exception as e:  # noqa: BLE001 - 무엇이 깨졌든 우리 Job이 살아 있을 수 있으면 차례를 넘기지 않는다
            run.error = f"{type(e).__name__}: {e}"
            run.final_state = RunState.FAILED.value
            if isinstance(e, (K8sError, OSError)):
                log.error("run %s: k8s job %s failed: %s", run.run_id, name, run.error)
            else:
                log.exception("run %s: k8s job %s failed", run.run_id, name)
            self._push(run, "error", {"message": run.error})
            if client is not None and run.job_name:
                # 만들려고 했거나 이어받은 Job이 있을 수 있다. 우리 것이면 지우고, 끝난 것을 확인한 뒤 차례를 넘긴다
                self._hold_until_ended(run, client, run.job_name, delete_after=0.0, why="the run failed in the web process")
            return EXIT_ABORTED

    def _job_env(self, run: Run) -> dict[str, str]:
        env = {"SBMIG_SOURCE_ZIP": str(run.zip_path), "SBMIG_OUT": str(self.out_dir), "SBMIG_RUN_ID": run.run_id,
               "SBMIG_CONFIG": " ".join(str(self.repo_root / "config" / c) for c in run.configs)}
        if run.stop_after:
            env["SBMIG_STOP_AFTER"] = run.stop_after
        if run.llm_provider:
            env["SBMIG_LLM_PROVIDER"] = run.llm_provider
        if run.slot is not None:               # Worker가 슬롯마다 다른 Maven 로컬 저장소로 빌드한다
            env[contract.SLOT_ENV] = str(run.slot)
        # 시도 번호를 넘긴다. Worker가 제어 상태를 읽지 못하고 거절해도 이 번호를 결과 파일에 기록한다
        env[contract.ATTEMPT_ENV] = str(run.attempt)
        if run.action:                         # Worker가 `sbmig run` 대신 이 동작을 실행한다
            env[contract.ACTION_ENV] = run.action
        # full이어도 항상 넣는다. Worker가 request.json의 flow와 비교한다. 비어 있으면 Worker는 full로 본다
        env[contract.FLOW_ENV] = run.flow or FLOW_FULL
        return env

    def _env_pins(self, run: Run) -> dict[str, str] | None:
        """Job을 만드는 시점의 환경 변수 해시(``contract.env_pins``)를 반환한다. 비밀로 보이는 이름은 넣지 않는다.
        정책이 off면 None을 반환한다. 계산이 실패해도 Job 생성은 막지 않는다. 경고만 남기고 None을 반환한다.

        An unreadable config is caught by the Worker's config digests."""
        if self.env_pin_policy == contract.ENV_PIN_OFF:
            return None
        try:
            return contract.env_pins(self.repo_root, list(run.configs))
        except Exception as e:  # noqa: BLE001 - 해시 계산 때문에 Job 생성이 실패하면 안 된다
            log.warning("run %s: the job environment is not pinned (%s: %s) — the Worker will not compare its environment",
                        run.run_id, type(e).__name__, scrub(str(e)))
            return None

    def _image_pins(self, run: Run) -> dict[str, Any]:
        """Job을 만드는 시점의 설정 파일 해시와 sbmig 버전을 반환한다.
        계산에 실패하면 빈 dict를 반환한다. 이때 Worker는 ``request.json``의 값과 비교한다."""
        try:
            return {"config_digests": config_digests(self.repo_root, list(run.configs)), "sbmig_version": sbmig_version()}
        except Exception as e:  # noqa: BLE001 - 고정 값 때문에 Job 생성이 실패하면 안 된다
            log.warning("run %s: config digests not recorded at job creation (%s: %s) — the Worker compares the intake pins",
                        run.run_id, type(e).__name__, scrub(str(e)))
            return {}

    def _create_job(self, run: Run, client: Any, name: str) -> None:
        """Job을 만든다. Job을 만드는 곳은 이 함수뿐이다. 성공하면 상태는 ``created``가 된다.
        템플릿에 owner 라벨까지 채워 매니페스트를 만들고, ``creating`` 상태를 먼저 기록한다.
        - ``creating`` 상태에 이 Main의 ``env_pins``와 ``env_pin_policy``를 함께 기록한다. Worker가 비교한다
        - 응답이 409이거나 전송 오류면 API가 응답할 때까지 같은 이름의 Job을 찾는다
        - 그 Job이 우리 것이면 이어받는다. 없으면 원래 오류를 다시 던진다"""
        template: Any = self._job_template_doc if self._job_template_doc is not None else self.job_template
        if self._job_template_doc is None and not Path(self.job_template).is_file():
            if self.effective_mode == "k8s-job":
                # 시작 검증을 건너뛴 경우(테스트 클라이언트)에만 여기 온다. 빈 템플릿으로 Job을 만들지 않는다
                raise K8sError(f"job template not found: {self.job_template} — {TEMPLATE_HINT}")
            # 로컬 파드는 환경 변수만 쓴다. 템플릿이 없으면 최소 매니페스트로 대신한다
            template = {"kind": "Job", "metadata": {}, "spec": {"template": {"spec": {"containers": [{"name": "build"}]}}}}
        manifest = render_job(template, run.run_id, self._job_env(run), getattr(client, "namespace", None), owner=run.owner)
        # 재개 시도의 Job 이름은 <이관 Job>-rem<시도 번호>다. 라벨은 render_job이 붙인 그대로 쓴다
        manifest.setdefault("metadata", {})["name"] = name
        # 생성 의도를 먼저 기록한다. 여기서 죽어도 다음 Main이 creating 상태와 이름으로 Job을 찾는다.
        # 환경 해시는 접수한 Main이 아니라 지금 Job을 만드는 이 Main의 환경으로 만든다
        pins = self._env_pins(run)
        # 설정 해시와 sbmig 버전도 지금 Job을 만드는 Main의 값이다. 대기 중에 웹 파드가 새 이미지로 바뀌었어도
        # 지금 웹 파드와 빌드 파드가 같으면 통과한다
        image_pins = self._image_pins(run)
        # 슬롯 번호도 함께 기록한다. 재시작한 Main이 같은 번호를 다시 잡는다
        self._transition(run, RunState.CREATING, job=name, env_pins=pins, env_pin_policy=self.env_pin_policy, slot=run.slot,
                         **image_pins)
        if pins is not None:
            log.info("run %s: %d environment value(s) pinned for job %s (%s=%s)", run.run_id, len(pins), name,
                     contract.ENV_PIN_POLICY_ENV, self.env_pin_policy)
        run.job_name = name
        try:
            created = client.create_job(manifest)
        except K8sError as e:
            if not _UNCERTAIN_CREATE_RE.search(str(e)):
                raise
            # 응답이 불확실하면 API가 응답할 때까지 Job이 있는지 조회한다. 모르는 채로 실패 처리하면
            # 이미 만들어진 파드를 둔 채 차례를 넘긴다. 끊긴 요청이 늦게 전달될 수 있어서 몇 번 더 조회한다
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
        if created and created != name:        # 이름은 정해진 값이라 다를 일이 없다. 그래도 API가 다르게 답하면 따른다
            log.warning("run %s: job created as %s, expected %s", run.run_id, created, name)
            run.job_name = name = str(created)
        _append_journal(run.run_dir, {"event": "job_created", "run_id": run.run_id, "job": name, "attempt": run.attempt,
                                      "namespace": getattr(client, "namespace", None), "slot": run.slot,
                                      **({"action": run.action} if run.action else {})})
        self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name}})

    def _poll_job(self, run: Run, client: Any, name: str) -> JobState | None:
        """Job 상태를 한 번 조회해 반환한다. API가 응답하지 않으면 None을 반환한다. 실행을 실패로 만들지 않는다.
        - 클라이언트의 어떤 예외든 API 무응답으로 본다
        - ``job_api_grace_s``를 넘기면 로그, 저널 ``job_api_unavailable``, 화면 ``job.note``로 한 번 알린다
        - 그 뒤로는 ``API_REMIND_S``마다 로그를 남기고, 다시 응답하면 저널에 ``job_api_back``을 기록한다

        A proxy answering 200 with a non-JSON body raises ValueError; it must not fail the run and free the
        slot while the Job runs."""
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
        """API가 응답할 때까지 Job 상태를 조회해 반환한다. 기한 없이 기다린다.
        Job이 있는지 모르는 채로 차례를 넘기지 않기 위해서다.
        기다리는 중에 서버가 종료되면 ``_ServerClosing``을 던진다."""
        while True:
            st = self._poll_job(run, client, name)
            if st is not None:
                return st
            if self.closing.is_set():
                raise _ServerClosing(name)
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _job_exists(self, run: Run, client: Any, name: str) -> bool:
        """재시작 복구에서 같은 이름의 Job이 있는지 조회한다. API가 응답할 때까지 기다린다.
        Job이 있는데 라벨이 이 요청의 것이 아니면(라벨이 없는 경우 포함) K8sError를 던진다."""
        st = self._state_when_answered(run, client, name)
        if st.phase == "Missing":
            return False
        if not self._owned(run, st):
            raise K8sError(f"job {name} exists but is not owned by this request — not adopted")
        return True

    def _transition_soft(self, run: Run, state: RunState | str, **changes: Any) -> None:
        """Job이 이미 있을 때 제어 상태를 기록한다. 기록하지 못해도 로그만 남긴다. 예외는 던지지 않는다.
        여기서 감시를 멈추면 실행 중인 파드를 둔 채 차례를 넘기기 때문이다. 디스크에는 직전 상태가 남는다."""
        try:
            self._transition(run, state, **changes)
        except OSError as e:
            log.error("run %s: control state %s not written (%s) — the job is watched on", run.run_id,
                      state.value if isinstance(state, RunState) else state, e)

    def _adopt(self, run: Run, name: str, reason: str) -> None:
        """이미 있는 Job을 이 실행의 것으로 이어받는다. 저널에 ``job_adopted``를 남기고 '파드' 단계 이벤트를 보낸다.
        제어 상태가 queued나 creating이면 created로 바꾼다. 그 밖의 상태는 그대로 둔다."""
        run.job_name = name
        _append_journal(run.run_dir, {"event": "job_adopted", "run_id": run.run_id, "job": name, "attempt": run.attempt,
                                      "reason": reason, **({"action": run.action} if run.action else {})})
        log.info("run %s: adopted job %s (%s)", run.run_id, name, reason)
        if run.control is None or run.control.state in (RunState.QUEUED.value, RunState.CREATING.value):
            self._transition_soft(run, RunState.CREATED, job=name)
        self._push(run, "node", {"node": "job", "update": {"state": "created", "job": name, "adopted": True}})

    def _hold_until_ended(self, run: Run, client: Any, name: str, *, delete_after: float | None, why: str) -> bool:
        """Job과 파드가 끝났거나 사라진 것을 API로 확인할 때까지 슬롯을 잡고 기다린다.
        그 슬롯의 Maven 로컬 저장소를 다음 이관과 함께 쓰지 않게 하기 위해서다.
        ``delete_after``초가 지나면 우리 라벨의 Job을 지우고 기한 없이 기다린다. 삭제가 실패하면 다시 시도한다.
        종료를 확인하면 True를 반환한다. 서버 종료(상태 유지)나 다른 요청의 Job(건드리지 않음)이면 False다."""
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
                    except Exception as e:  # noqa: BLE001 - 어떤 실패든 다시 시도한다. 종료를 확인하기 전에는 빠지지 않는다
                        delete_retry_at = now + DELETE_RETRY_S
                        log.warning("run %s: job %s not deleted (%s) — retrying in %.0fs", run.run_id, name, e, DELETE_RETRY_S)
                    else:
                        deleted = True
                        _append_journal(run.run_dir, {"event": "job_deleted", "run_id": run.run_id, "job": name, "reason": why})
                        log.warning("run %s: job %s deleted (%s) — waiting until it is gone", run.run_id, name, why)
            if self.closing.is_set():              # 서버 종료. 다음 Main이 디스크의 상태를 읽고 Job을 이어서 감시한다
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
        """Job이 끝날 때까지 감시하고 종료 코드를 반환한다. 새로 만든 Job과 이어받은 Job에 모두 쓴다.
        - 파드가 Running이 되면 ``running``으로 바꾼다
        - 끝나면 ``job-status.json``의 결과를 쓴다. 이 파일이 없으면 컨테이너 종료 코드를 확인되지 않은 결과로 쓴다
        - Job이 사라졌으면 ``lost``로, 중지 요청 뒤에 사라졌으면 ``stopped``로 끝낸다
        - 중지 요청이 오면 ``stop.request``를 기록한다. 유예 시간이 지나면 우리 라벨의 Job만 지운다
        - 파드가 아직 시작하지 않았으면(Creating, Pending) 중지 요청 때 유예 없이 바로 지운다
        - 파드가 ``job_pending_notice_s``초 넘게 시작하지 못하면 이유를 찾아 화면에 알린다
        - Job 종료를 확인한 뒤에만 반환한다. 서버 종료와 다른 요청의 Job만 예외다

        The pod step's `Failed` is decided by the status file, not pushed as the raw phase
        (`_close_pod_step`)."""
        last: tuple[str, str | None] | None = None
        stop_sent: float | None = None
        run.pod_step_closed = False                # 감시 한 번에 '파드' 단계를 닫는 이벤트는 하나만 보낸다
        run.pod_wait, run.job_pending = {}, None
        while True:
            code = self._accept_status(run)
            if code is not None:                   # 파드가 볼륨에 끝났다고 기록했다. 실제로 끝날 때까지 차례를 잡고 기다린다
                self._close_pod_step(run, name, code=code)
                self._hold_until_ended(run, client, name, delete_after=self.job_exit_grace_s, why="the result is on the volume")
                self._log_failed_phase_explained(run, name, code)
                return code
            st = self._poll_job(run, client, name)   # None이면 API가 응답하지 않은 것이다. 차례를 잡은 채 다시 조회한다
            if st is not None:
                if (st.phase, st.pod) != last:
                    last = (st.phase, st.pod)
                    run.job_phase, run.job_pod = st.phase, st.pod
                    # Failed는 아직 화면에 보내지 않는다. Worker가 sbmig의 종료 코드 3을 그대로 내면 Job은 Failed지만
                    # 파드는 정상 동작한 것이다. 결과 파일을 기다린 뒤(_await_status) 정한다
                    if st.phase != "Failed":
                        self._push(run, "node", {"node": "job", "update": {"state": st.phase.lower(), "job": name,
                                                                            "pod": st.pod, "message": st.message}})
                    log.info("run %s: job %s %s%s%s", run.run_id, name, st.phase, f" pod {st.pod}" if st.pod else "",
                             f" ({st.message})" if st.message else "")
                if st.phase in NOT_STARTED_PHASES:
                    self._note_pod_waiting(run, client, name, st)
                elif run.pod_wait:
                    self._pod_started(run, name, st)
                if st.phase == "Running" and run.control is not None and run.control.state in (
                        RunState.QUEUED.value, RunState.CREATING.value, RunState.CREATED.value):
                    self._transition_soft(run, RunState.RUNNING, job=name)
                if st.phase in ("Succeeded", "Failed") and not st.finished:
                    # Job은 끝났다지만 파드가 아직 실행 중이거나 종료 중이다. Maven이 계속 파일을 쓸 수 있다.
                    # 파드가 끝날 때까지 슬롯을 잡는다. SBMIG_JOB_EXIT_GRACE_S가 지나면 우리 Job을 지우고 기다린다
                    log.info("run %s: job %s is %s but %d pod(s) of it are still running or terminating — waiting for them",
                             run.run_id, name, st.phase, st.active_pods)
                    ended = self._hold_until_ended(run, client, name, delete_after=self.job_exit_grace_s,
                                                   why=f"the job is {st.phase} while its pod is still terminating")
                    if not ended and run.keep_state:   # 서버 종료. 다음 Main이 Job을 이어서 감시한다
                        run.error = "web server shut down while the job was running — the job continues in the cluster"
                        return EXIT_ABORTED
                    st = replace(st, active_pods=0)    # 파드가 끝났다. 우리 것이 아닌 Job이면 건드리지 않았다
                if st.finished:
                    # Worker는 파드가 끝나기 직전에 job-status.json을 기록한다. NFS에서는 늦게 보일 수 있다
                    code = self._await_status(run, name, st)
                    if code is None and self.closing.is_set() and st.phase != "Missing" and self.job_status_grace_s > 0:
                        # 서버 종료 중에는 결과 파일을 기다리지 않는다. 제어 상태를 그대로 두면 다음 Main이 결과를 읽는다
                        run.error = "web server shut down while waiting for the job's result — the next web process reads it"
                        run.keep_state = True
                        return EXIT_ABORTED
                    if code is not None:
                        self._close_pod_step(run, name, code=code)
                        self._log_failed_phase_explained(run, name, code)
                        return code
                    if st.phase == "Failed":       # 유예 안에 결과 파일이 없다. 파드가 죽은 것이다(OOMKilled, 기한 초과, 축출 등)
                        self._close_pod_step(run, name, ended=st)
                    run.verified = False
                    if st.phase == "Missing":
                        if run.cancel.is_set() or (run.run_dir / STOP_REQUEST_FILE).is_file():
                            # 중지를 요청한 실행의 Job이 사라졌다. 이전 Main이 지우다가 종료되었거나 밖에서 지운 것이다.
                            # lost가 아니라 stopped로 기록한다
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
            if self.closing.is_set():              # 서버 종료. Job은 계속 실행되고 다음 서버가 파일로 응답한다
                run.error = "web server shut down while the job was running — the job continues in the cluster"
                run.keep_state = True
                return EXIT_ABORTED
            if run.cancel.is_set():
                if stop_sent is None:
                    stop_file = run.run_dir / STOP_REQUEST_FILE
                    if run.action and stop_file.is_file():
                        stop_sent = time.monotonic()   # 조치 재개는 stop_remediation이 이미 기록하고 저널에 남겼다
                    else:
                        try:
                            stop_file.write_text("stop requested from the web UI\n", encoding="utf-8")
                        except OSError as e:       # 볼륨이 잠시 응답하지 않으면 다음 조회 때 다시 기록한다. 감시는 계속한다
                            log.warning("run %s: %s not written (%s) — retrying", run.run_id, STOP_REQUEST_FILE, e)
                        else:
                            stop_sent = time.monotonic()
                            _append_journal(run.run_dir, {"event": "stop_request_sent", "run_id": run.run_id, "job": name})
                            log.info("run %s: stop requested — %s written for job %s", run.run_id, STOP_REQUEST_FILE, name)
                if st is not None and st.phase in NOT_STARTED_PHASES:
                    # 파드가 시작하지 않았으면 stop.request를 읽을 Worker가 없다. 유예 없이 Job을 지운다
                    return self._delete_before_start(run, client, name, st)
                if run.action:
                    # 실행 중인 조치 재개는 stop.request로만 멈추고 Job을 지우지 않는다
                    pass
                elif stop_sent is not None and st is not None and time.monotonic() - stop_sent > self.job_stop_grace_s:
                    run.final_state = RunState.STOPPED.value
                    if not self._owned(run, st):
                        # 같은 이름이라도 라벨이 다르면 다른 요청의 Job이다. 지우지 않는다
                        run.error = f"job {name} is not owned by this request — not deleted"
                        log.error("run %s: %s (labels %s)", run.run_id, run.error, st.labels)
                        _append_journal(run.run_dir, {"event": "job_delete_refused", "run_id": run.run_id, "job": name})
                        return EXIT_ABORTED
                    log.warning("run %s: job %s still running %.0fs after the stop request — deleting it",
                                run.run_id, name, self.job_stop_grace_s)
                    run.error = "stopped: the job was deleted after the grace period"
                    # Job을 지운 뒤 사라질 때까지 기다린다. 파드가 아직 볼륨에 쓰는 동안 다음 이관이 시작하지 않게 한다
                    self._hold_until_ended(run, client, name, delete_after=0.0, why="the stop grace period is over")
                    return EXIT_ABORTED
            with run.cond:
                run.cond.wait(self.job_poll_s)

    def _delete_before_start(self, run: Run, client: Any, name: str, st: JobState) -> int:
        """파드가 시작하기 전에 중지 요청이 오면 우리 Job을 바로 지우고 사라질 때까지 기다린다.
        우리 것이 아닌 Job은 지우지 않는다. 실행은 stopped로 끝나고, 저널에 ``job_deleted_before_start``를 남긴다.

        A container that starts in the same second sees `stop.request` or the SIGTERM of the delete
        (the Worker handles both)."""
        run.final_state = RunState.STOPPED.value
        if not self._owned(run, st):
            run.error = f"job {name} is not owned by this request — not deleted"
            log.error("run %s: %s (labels %s)", run.run_id, run.error, st.labels)
            _append_journal(run.run_dir, {"event": "job_delete_refused", "run_id": run.run_id, "job": name})
            return EXIT_ABORTED
        pending = run.job_pending or {}
        reason = pending.get("reason") or st.waiting_reason or st.sched_reason or st.message or st.phase
        run.error = STOP_BEFORE_START_ERROR
        _append_journal(run.run_dir, {"event": "job_deleted_before_start", "run_id": run.run_id, "job": name,
                                      "phase": st.phase, "reason": reason,
                                      **({"action": run.action} if run.action else {})})
        log.warning("run %s: stop requested while job %s is %s (the build pod has not started) — deleting it now",
                    run.run_id, name, st.phase)
        run.job_pending = None
        self._hold_until_ended(run, client, name, delete_after=0.0, why="the stop came before the build pod started")
        self._push(run, "node", {"node": "job", "update": {"state": "stopped-before-start", "job": name, "phase": st.phase,
                                                            "message_ko": STOP_BEFORE_START_KO}})
        return EXIT_ABORTED

    def _note_pod_waiting(self, run: Run, client: Any, name: str, st: JobState) -> None:
        """파드가 시작하지 못한 채 ``job_pending_notice_s``초가 지나면 이유를 찾아 화면에 알린다.
        이유가 바뀌거나 ``PENDING_RESEND_S``가 지나면 다시 보낸다. 알리기만 한다. 실행을 끝내거나 Job을 지우지 않는다.

        Sources: waiting reason, unschedulable condition, pod events. Page: `job` node update `pod-pending`
        and `status().job.pending`. Journal `job_pod_pending` and one WARNING per distinct reason."""
        w = run.pod_wait
        now = time.monotonic()
        since = w.setdefault("since", now)
        if self.job_pending_notice_s <= 0:
            return
        waited = now - since
        if waited < self.job_pending_notice_s:
            return
        events: list[dict[str, str]] = w.get("events") or []
        wr = st.waiting_reason
        needs_events = not ((wr and wr not in ("ContainerCreating", "PodInitializing")) or st.sched_reason or st.sched_message)
        pod_events = getattr(client, "pod_events", None)
        if (needs_events and st.pod and callable(pod_events) and self._events_readable
                and getattr(client, "events_readable", True) and now - w.get("events_at", -1e18) >= PENDING_EVENTS_EVERY_S):
            w["events_at"] = now
            try:
                events = list(pod_events(st.pod) or [])
            except Exception as e:  # noqa: BLE001 - 이벤트를 읽지 못해도 감시는 계속한다
                log.debug("run %s: events of pod %s not read (%s)", run.run_id, st.pod, e)
                events = []
            w["events"] = events
            if getattr(client, "events_readable", True) is False:
                self._events_readable = False
        family, reason, message, source = pod_wait_reason(st, events if needs_events else [])
        message = message[:POD_WAIT_MESSAGE_MAX]
        if output_scrubbing():
            message, reason = scrub(message), scrub(reason)
        info = {"seconds": int(waited), "reason": reason, "message": message, "family": family, "source": source,
                "hint_ko": pod_wait_hint_ko(family, waited, st.pod, name), "pod": st.pod, "phase": st.phase}
        run.job_pending = info
        key = (family, reason)
        if key == w.get("key") and now - w.get("sent", -1e18) < PENDING_RESEND_S:
            return
        w["key"], w["sent"] = key, now
        self._push(run, "node", {"node": "job", "update": {"state": "pod-pending", "job": name, "pod": st.pod,
                                                            "pending": dict(info)}})
        seen = w.setdefault("seen", set())
        if key in seen:
            return
        seen.add(key)
        _append_journal(run.run_dir, {"event": "job_pod_pending", "run_id": run.run_id, "job": name, "pod": st.pod,
                                      "phase": st.phase, "seconds": int(waited), "family": family, "reason": reason,
                                      "message": message, "source": source})
        log.warning("run %s: job %s pod %s has not started for %ds: %s", run.run_id, name, st.pod or "-", int(waited),
                    f"{reason}: {message}" if reason else "no reason reported (events not readable or empty)")

    def _pod_started(self, run: Run, name: str, st: JobState) -> None:
        """파드가 시작했거나 끝났으면 시작하지 못한 이유 안내를 지운다. 화면은 다음 ``job`` 이벤트에서 안내를 감춘다."""
        had = run.job_pending is not None
        run.pod_wait, run.job_pending = {}, None
        if had:
            log.info("run %s: job %s pod %s left %s — the waiting notice is cleared", run.run_id, name, st.pod or "-",
                     "/".join(NOT_STARTED_PHASES))
            self._push(run, "node", {"node": "job", "update": {"state": "pod-started", "job": name, "pod": st.pod}})

    def _close_pod_step(self, run: Run, name: str, *, code: int | None = None, ended: JobState | None = None) -> None:
        """'파드' 단계를 닫는 이벤트를 보낸다. 감시 한 번에 한 번만 보낸다. 이 단계는 파드가 제 역할을 했는지를 나타낸다.
        - ``code``가 있으면 결과 파일을 받은 경우다. ``error``가 없으면 종료 코드와 상관없이 ``completed``를 보낸다
        - 결과 파일에 ``error``가 있으면 ``failed``와 그 오류를 보낸다
        - ``ended``가 있으면 결과 파일 없이 Job이 Failed로 끝난 경우다. ``failed``와 쿠버네티스 메시지를 보낸다"""
        if run.pod_step_closed:
            return
        update: dict[str, Any]
        if code is not None:
            update = {"state": "failed" if run.status_error else "completed", "job": name, "pod": run.job_pod,
                      "exit_code": code}
            if run.status_error:
                update["message"] = run.status_error
            if run.job_phase:                  # 본 적이 있으면 쿠버네티스 phase 값도 함께 보낸다
                update["phase"] = run.job_phase
        elif ended is not None:
            update = {"state": "failed", "job": name, "pod": ended.pod, "message": ended.message}
        else:
            return
        run.pod_step_closed = True
        self._push(run, "node", {"node": "job", "update": update})

    def _log_failed_phase_explained(self, run: Run, name: str, code: int) -> None:
        """Job이 Failed로 끝났지만 확인된 결과 파일의 종료 코드로 설명되면 로그 한 줄을 남긴다.
        Worker는 sbmig 종료 코드 3을 그대로 낸다. 운영자가 이것을 파드 실패로 오해하지 않게 한다."""
        if run.job_phase == "Failed" and run.verified and not run.status_error:
            log.info("run %s: job %s: Failed is the Worker's exit code %s (job-status.json verified) — not a pod failure",
                     run.run_id, name, code)

    def _await_status(self, run: Run, name: str, st: JobState) -> int | None:
        """Job이 끝난 뒤 ``job-status.json``이 보일 때까지 다시 읽는다. 최대 ``job_status_grace_s``(기본 90초) 기다린다.
        찾으면 종료 코드를, 끝내 없으면 None을 반환한다. NFS 속성 캐시가 새 파일을 최대 1분 늦게 보여 줄 수 있다.
        Missing(지워진 Job)이면 기다리지 않는다. 서버가 종료되면 바로 None을 반환한다."""
        code = self._accept_status(run)
        if code is not None or st.phase == "Missing" or self.job_status_grace_s <= 0 or self.closing.is_set():
            return code
        deadline = time.monotonic() + self.job_status_grace_s
        log.info("run %s: job %s ended (%s) but job-status.json is not visible yet — waiting up to %.0fs for the shared volume",
                 run.run_id, name, st.phase, self.job_status_grace_s)
        while time.monotonic() < deadline and not self.closing.is_set():   # 서버 종료는 run.cond를 깨운다. 바로 빠진다
            with run.cond:
                run.cond.wait(min(self.job_poll_s, 1.0))
            code = self._accept_status(run)
            if code is not None:
                return code
        return None

    def _prune_locked(self) -> None:
        """끝난 실행이 ``max_finished_runs``개를 넘으면 오래된 것부터 메모리에서 뺀다.
        호출하는 쪽이 ``self._lock``을 잡고 있어야 한다. 실행 중이거나 대기 중인 실행은 그대로 둔다."""
        finished = sorted((r for r in self.runs.values() if r.done.is_set()), key=lambda r: r.started)
        excess = len(finished) - self.max_finished_runs
        for r in finished[:max(0, excess)]:
            self.runs.pop(r.run_id, None)
            log.debug("run %s evicted from memory (max_finished_runs=%d)", r.run_id, self.max_finished_runs)

    def _run_thread_body(self, run: Run) -> Any:
        """이관을 실행하고 최종 RunReport를 반환한다. 테스트가 이 메서드를 교체한다.
        - LangGraph가 없으면 inline 엔진을 쓴다. 이때는 노드 이벤트와 중지가 없다
        - 노드가 끝날 때마다 ``node`` 이벤트를 쌓고 중지 요청을 확인한다
        - 최종 빌드(verify)나 finalize 뒤의 중지 요청은 무시한다
        - 그 전이면 aborted 표시 -> 그래프 닫기 -> 보고서 마감 순서로 멈춘다"""
        from sbmig.core.models import Phase

        orch = run.orch
        phase = Phase(run.stop_after) if run.stop_after else None
        # 실행 방식은 full이 아닐 때만 넘긴다. full은 엔진의 기본값이라서 flow를 모르는 엔진에서도 실행된다
        extra: dict[str, Any] = {"flow": run.flow} if run.flow and run.flow != FLOW_FULL else {}
        try:
            from sbmig.graph import stream_graph
        except ImportError as e:      # LangGraph가 없으면 inline 엔진으로 실행한다. 노드 이벤트는 없다
            log.info("graph engine unavailable (%s) — running the inline engine", e)
            return orch.run(stop_after=phase, **extra)
        updates = stream_graph(orch, stop_after=phase, **extra)
        stopped = False
        try:
            for name, update in updates:
                self._push(run, "node", {"node": name, "update": _jsonable(update)})
                if not run.cancel.is_set():
                    continue
                # 최종 빌드가 이미 실행되었으면 이관은 끝난 것이다. 여기서 멈추면 성공을 중단으로 잘못 기록한다.
                # 그 뒤에 실행되는 remediation 노드도 마찬가지다
                if not update.get("aborted") and (name in ("finalize", "remediation")
                                                  or (name == "phase" and update.get("phase") == Phase.VERIFY.value)):
                    log.warning("run %s: stop requested after the final build — finishing normally", run.run_id)
                    continue
                log.warning("run %s: stop requested — stopping after node %s (no final build)", run.run_id, name)
                # close() 전에 aborted를 표시한다. 엔진은 일찍 닫힐 때 보고서를 마감한다
                _mark_stopped(orch)
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
        """실행 기록에 이벤트를 추가하고 기다리는 SSE 스트림을 깨운다. ``node`` 이벤트면 마지막 노드 이름도 바꾼다.
        출력 마스킹이 켜져 있으면 이벤트 문자열도 마스킹한다. 이벤트는 화면으로 나가기 때문이다."""
        if output_scrubbing():
            data = scrub_json(data)
        with run.cond:
            run.history.append((kind, data))
            if kind == "node":
                run.last_node = data.get("node")
            run.cond.notify_all()

    def stop(self, run_id: str) -> bool:
        """현재 그래프 노드가 끝나면 실행을 멈추도록 요청한다. 진행 중인 Maven이나 LLM 호출은 끊지 않는다.
        모르는 실행이거나 이미 끝났으면 False를 반환한다.
        - Job 모드에서 대기 중인 실행은 바로 줄에서 빠지고 Job 없이 stopped로 끝난다
        - 파드가 아직 시작하지 않은 Job은 감시 스레드가 바로 지운다(``_delete_before_start``)
        - in-process의 대기 실행은 차례가 와서 첫 노드를 마친 뒤 멈춘다"""
        run = self.runs.get(run_id)
        if run is None or run.done.is_set():
            return False
        run.cancel.set()                       # 먼저 설정한다. 그래야 그 사이 차례가 온 실행이 이것을 보고 Job을 만들지 않는다
        if run.job and self._turns.withdraw(run_id):
            log.info("run %s: stop requested while waiting — it leaves the line", run_id)
        else:
            log.info("run %s: stop requested", run_id)
            with run.cond:                     # 감시 스레드가 다음 조회를 기다리지 않고 바로 확인한다
                run.cond.notify_all()
        return True

    # ---- 새 빌드 파드에서 취약점 조치 재개
    def resume_remediation(self, run_id: str) -> dict[str, Any]:
        """끝난 Job 모드 실행의 취약점 조치를 새 빌드 파드에서 이어 간다. 이 시도를 대기열에 넣는다.
        ``remediation_job(run_id)`` 정보를 반환한다.
        - Job 모드가 아니거나 Job 모드로 접수한 실행이 아니면 ``StateConflict``를 던진다
        - 이관이 ``finished``가 아니거나, 재개가 이미 대기 중이거나 실행 중일 때도 ``StateConflict``를 던진다
        - 남아 있는 ``stop.request``를 지운다. ``control/remediation-resume.json``에 queued를 먼저 기록한 뒤 줄에 선다"""
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
                # 이전 웹 프로세스가 기록한 끝나지 않은 재개다. 복구가 이어받지 않았으면 Job이 아직 실행 중일 수 있다
                raise StateConflict(f"a remediation resume of run {run_id} is recorded as {disk.state}", status=disk.state)
            mig_cs = ControlState.read(rd)
            attempt = max(int(rec.attempt or 1), int(mig_cs.attempt) if mig_cs else 1, int(disk.attempt) if disk else 0) + 1
            stop_file = rd / STOP_REQUEST_FILE
            try:
                if stop_file.is_file():
                    # 이관은 이미 끝났다. 남은 중지 요청은 이관의 것이다. 새 재개 시도가 바로 멈추지 않도록 지운다
                    stop_file.unlink()
                    log.info("run %s: a leftover %s removed before the remediation resume", run_id, STOP_REQUEST_FILE)
                # 시도 번호 없이 기록된 이전 재개 결과는 어느 시도와도 맞는 것으로 읽힌다. 새 시도를 넣기 전에 지운다
                old_status = rd / RESUME_STATUS_FILE
                if old_status.is_file():
                    old_status.unlink()
                    log.info("run %s: the previous %s removed before the remediation resume", run_id, RESUME_STATUS_FILE)
                control = ControlState(run_id=run_id, state=RunState.QUEUED.value, attempt=attempt,
                                       action=ACTION_REMEDIATION_RESUME)
                control.write(rd, RESUME_STATE_FILE)          # 의도를 먼저 기록한다. 그래야 다음 Main이 이어받는다
            except OSError as e:
                raise StateConflict(f"the resume request could not be recorded ({type(e).__name__})") from None
            run = Run(run_id, rd, rd / SOURCE_ZIP, list(rec.configs or []), rec.stop_after, rec.llm_provider, job=True,
                      request=rec, control=control, action=ACTION_REMEDIATION_RESUME, flow=rec.flow or FLOW_FULL,
                      upload=rec.upload_name or rec.upload, project=rec.project or None)
            run.job_name = resume_job_name(run_id, attempt)
            run.thread = threading.Thread(target=self._worker, args=(run,), name=f"sbmig-job-{run.key}", daemon=True)
            self.resumes[run_id] = run
            self._turns.join(run.key)          # 업로드와 같은 줄에 요청 순서대로 선다
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
        """이 실행의 마지막 조치 재개 시도 정보를 반환한다. 재개한 적이 없으면 None을 반환한다.
        메모리에 없으면 ``control/remediation-resume.json``에서 읽는다.

        Keys: `state` (queued / running / finished), `active`, `attempt`, `job`, `control`; when known `slot`,
        `queue`, `exit_code`, `verified`, `error`, `note`, `pending`, `stop_requested`."""
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
            if run.job_pending and state != "finished":
                info["pending"] = dict(run.job_pending)
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
        """이 실행의 조치 재개 시도가 대기 중이거나 실행 중인지 확인한다. 읽지 못하면 True를 반환한다.
        그동안은 그 시도가 조치 상태 파일을 관리한다. 그래서 다른 재개를 막고 조치 파일을 내주지 않는다."""
        try:
            return bool((self.remediation_job(run_id) or {}).get("active"))
        except Exception:  # noqa: BLE001 - 읽지 못하면 실행 중으로 본다. 빌드 파드와 겹치지 않게 한다
            log.debug("remediation_job(%s) failed", run_id, exc_info=True)
            return True

    def stop_remediation(self, run_id: str) -> str | None:
        """조치 재개 시도를 중지한다. 대기 중이거나 실행 중인 재개가 없으면 None을 반환한다.
        - 대기 중이면 바로 줄에서 빠지고 Job 없이 stopped로 끝난다. ``"withdrawn"``을 반환한다
        - Job이 있으면 ``stop.request``를 기록하고 ``"requested"``를 반환한다
        - 실행 중인 재개 파드는 지우지 않는다. 다음 단계 경계에서 멈춘다
        - 파드가 아직 시작하지 않았으면 감시 스레드가 Job을 바로 지운다"""
        run = self.resumes.get(run_id)
        if run is None or run.done.is_set():
            return None
        if self._turns.withdraw(run.key):
            run.cancel.set()
            log.info("run %s: remediation resume stopped while waiting — it leaves the line", run_id)
            return "withdrawn"
        if run.control is None or run.control.state == RunState.QUEUED.value:
            run.cancel.set()                   # 차례는 왔지만 Job이 아직 없다. _run_job이 Job을 만들지 않는다
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
        run.cancel.set()                       # 파드가 아직 시작하지 않았으면 _watch_job이 Job을 바로 지운다
        with run.cond:
            run.cond.notify_all()
        return "requested"

    def shutdown(self, reason: str = "server shutdown") -> dict[str, Any]:
        """서버가 종료될 때(lifespan) 정리한다. 열린 SSE 스트림을 끝내고 in-process 실행에 중지를 요청한다.
        남은 Maven, OpenRewrite 프로세스도 종료한다. Job은 클러스터에서 계속 실행된다.
        정리하지 않으면 uvicorn이 SSE 연결을 기다리고, Windows에서는 java.exe가 서버보다 오래 남는다."""
        from sbmig.build.mvn import kill_live_processes

        self.closing.set()
        stopped: list[str] = []
        for run in list(self.runs.values()) + list(self.resumes.values()):
            if run.done.is_set():
                continue
            if not run.job:                       # Job은 계속 실행되고 감시 스레드는 깨어나서 빠진다
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
        # 마지막 마스킹이 아직 남았는지 확인한다. 메모리의 실행은 끝나지 않았고 Job이거나 출력 마스킹이 켜졌으면 남았다.
        # 메모리에 없는 실행은 제어 상태가 아직 끝나지 않았으면 남았다
        if run is not None:
            return not run.done.is_set() and bool(run.job or output_scrubbing())
        return control is not None and not control.terminal

    def file_withheld(self, run_id: str, name: str) -> bool:
        """이 실행의 파일 ``name``을 아직 내주면 안 되는지 확인한다.
        마지막 마스킹이 남았으면 True다. 단, 기록할 때 이미 마스킹되는 파일(``LIVE_FILES``)은 언제나 내준다."""
        if name in LIVE_FILES:
            return False
        run = self.runs.get(run_id)
        control = None
        if run is None:
            rd = self.run_dir(run_id)
            control = ControlState.read(rd) if rd.is_dir() else None
        return self._final_pass_pending(run, control)

    def project_name(self, run_id: str) -> str | None:
        """업로드할 때 입력한 프로젝트명을 반환한다. status()와 같은 값이다. 없으면 None을 반환한다.
        파일 라우트가 결과 ZIP을 내려받는 이름을 만들 때 쓴다.

        Memory first, else the run folder (`control/request.json`, `web-status.json`, the journal's
        `web_run` line)."""
        run = self.runs.get(run_id)
        return (run.project if run is not None else None) or _recorded_project(self.run_dir(run_id))

    def status(self, run_id: str) -> dict[str, Any]:
        """실행 상태를 dict로 반환한다.
        - 메모리에 있으면 queued, running, finished 중 하나다
        - 없으면 제어 상태, web-status.json, 저널, 폴더 순으로 확인해 상태를 정한다
        - report.json이 있으면 성공했는지, 차단, 미해결, LLM 호출, 오류 수와 내려받을 파일 목록을 담는다
        - ``parallel``은 항상 담는다. 대기 중이면 ``queue``를, 슬롯을 가졌던 Job 실행이면 ``slot``을 담는다"""
        run = self.runs.get(run_id)
        rd = self.run_dir(run_id)
        # request.json과 web-status.json은 이 응답에서 한 번씩만 읽는다
        records = _RunRecords(rd)
        # 실행 폴더 경로는 보내지 않는다. 화면은 쓰지 않고, 서버 코드는 run_dir()로 찾는다
        info: dict[str, Any] = {"run_id": run_id, "exists": rd.is_dir() or run is not None,
                                "mode": self.effective_mode, "parallel": self.parallel(),
                                # 화면의 단계 표시, 결과 칸, 지난 실행 목록이 이 값을 따른다
                                "flow": (run.flow or FLOW_FULL) if run is not None else _recorded_flow(rd, records),
                                # 사용자가 올린 파일 이름 그대로. 이전 in-process 실행에는 없을 수 있다
                                "upload": (run.upload if run is not None else None) or _recorded_upload(rd, records)}
        # 업로드할 때 입력한 프로젝트명. 이전 실행에는 이 키가 없다. 그때 화면은 파일 이름을 쓴다
        project = (run.project if run is not None else None) or _recorded_project(rd, records)
        if project:
            info["project"] = project
        # 결과 ZIP을 내려받을 때의 이름. ZIP이 아직 없어도 담는다. 화면은 이 값을 그대로 보여 준다
        info["zip_name"] = contract.zip_download_name(run_id, "migration", project)
        control: ControlState | None = None
        if run is not None:
            info["state"] = self._mem_state(run)
            info["exit_code"] = run.exit_code
            info["node"] = run.last_node
            if info["state"] == "queued":
                pos = self._turns.position(run_id)
                if pos is not None:            # None이면 방금 슬롯을 받은 것이다. 곧 active가 된다
                    info["queue"] = {"position": pos, "ahead": pos - 1}
            if run.job and run.slot is not None:
                info["slot"] = run.slot
            if run.job:
                info["job"] = {"name": run.job_name, "phase": run.job_phase, "pod": run.job_pod}
                if run.job_note:               # API 무응답처럼 이 실행이 아직 차례를 잡고 있는 이유
                    info["job"]["note"] = run.job_note
                if run.job_pending and not run.done.is_set():   # 파드가 시작하지 못한 이유. 다시 연결한 화면도 보여 준다
                    info["job"]["pending"] = dict(run.job_pending)
                info["attempt"] = run.attempt
                info["verified"] = run.verified
                if run.control is not None:
                    info["control"] = run.control.state
            if run.cancel.is_set():
                info["stop_requested"] = True
            if run.error:
                info["error"] = run.error
        else:   # 메모리에서 빠졌거나 이전 서버 프로세스의 실행이다. 폴더만 안다
            control = ControlState.read(rd) if rd.is_dir() else None     # Main의 제어 상태를 먼저 본다
            saved = records.web or _read_job_status(rd)      # 파드의 job-status.json도 실행이 끝났다는 표시다
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
                job = _journal_job(rd)                # 이전 웹 프로세스가 만든 Job이 아직 실행 중일 수 있다
                if job is not None and not (rd / "report.json").is_file():
                    info["state"] = "running"
                    info["job"] = {"name": job.get("job"), "phase": None, "pod": None}
                    info["node"] = job.get("last_node")
                else:
                    info["state"] = _folder_state(rd)
                info["exit_code"] = None
        # 파드가 시작하기 전에 중지해서 Job을 지운 실행이면 화면에 그 사실을 한 줄로 보여 준다
        if str(info.get("error") or "").startswith(STOP_BEFORE_START_ERROR):
            info["stop_reason_ko"] = STOP_BEFORE_START_KO
        # 웹 파드와 빌드 파드의 이미지가 달라서 Worker가 거절한 실행이면 결과 칸에 한국어 문장을 보여 준다
        if contract.IMAGE_DIFFERS_HINT in str(info.get("error") or ""):
            info["abort_reason_ko"] = contract.IMAGE_DIFFERS_KO
        # 조치 재개 Job이 있으면 이관의 상태와 따로 보여 준다
        rjob = self.remediation_job(run_id)
        if rjob is not None:
            info["remediation_job"] = rjob
        framework: dict[str, Any] | None = None
        from_report = False
        # 실행 방식을 찾을 때 이미 읽은 보고서면 그 내용을 쓴다. 객체가 아닌 JSON은 읽지 못한 보고서로 본다
        data = records.report
        if data is not None:
            try:
                # 사내 프레임워크 프로젝트의 줄인 범위는 보고서의 profile.framework가 먼저다
                framework = _framework_from_report(data)
                from_report = True
                info["success"] = data.get("success")
                # 사내 프레임워크 실행이 JDK 21, Jakarta 변환을 마쳤으면 빌드가 실패해도 화면에 완료로 보여 준다.
                # 보고서를 쓴 뒤 다른 종료 코드로 끝난 파드(137 등)는 완료로 보이지 않게 한다
                ec = info.get("exit_code")
                ec_ok = ec is None or (type(ec) is int and ec in _CONVERTED_EXIT_CODES)
                if info.get("flow") != FLOW_REMEDIATION and ec_ok and _conversion_done(data):
                    info["conversion_complete"] = True
                # 목록이 아닌 값은 없는 것으로 본다
                info["blocked"] = len(_report_list(data, "blocked"))
                info["unresolved"] = len(_report_list(data, "unresolved"))
                info.update(run_llm_calls(rd, data))
                # 중단 사유(접수 거절 등)와 조치 요약은 결과 칸에 문장으로 보여 준다. 로그 탭에 묻히지 않게 한다
                why = _abort_reason(data)
                if why:
                    info["abort_reason"] = scrub(why) if output_scrubbing() else why
                    ko = _abort_reason_ko(info["abort_reason"])
                    if ko:
                        info["abort_reason_ko"] = ko
                rsum = data.get("remediation")
                if isinstance(rsum, dict) and rsum.get("status"):
                    info["remediation"] = {k: rsum[k] for k in ("status", "reason", "missing") if rsum.get(k) is not None}
                    # 빌드가 실패한 프로젝트에 이어서 한 조치면 화면이 일반 실패와 다르게 안내한다
                    if isinstance(rsum.get("baseline_tolerated"), bool):
                        info["remediation"]["baseline_tolerated"] = rsum["baseline_tolerated"]
                    # 사유 목록이 있으면 넘긴다. 목록 화면이 빌드 확인만 남은 조치와 일부만 한 조치를 구분한다
                    if isinstance(rsum.get("reasons"), list):
                        info["remediation"]["reasons"] = [str(x) for x in rsum["reasons"]][:10]
                from sbmig.report.result_line import entry_counts
                info["open"], info["resolved"] = entry_counts(_report_list(data, "error_entries"))
            except (OSError, ValueError):
                pass
        if from_report:
            # 보고서가 있으면 저널을 어디까지 읽었는지는 필요 없다. 보고서가 없어지면 저널을 처음부터 다시 읽는다
            with self._fw_lock:
                self._fw_scans.pop(run_id, None)
        # 보고서가 아직 없으면 저널의 framework_project, plan 이벤트에서 찾는다. 다른 실행에는 이 키가 없다
        elif info.get("flow") != FLOW_REMEDIATION:
            try:
                framework = self._journal_framework(run_id, rd)
            except Exception:  # noqa: BLE001 - 상태 응답은 프레임워크 정보 때문에 실패하면 안 된다
                log.debug("framework scope of %s not readable from the journal", run_id, exc_info=True)
                framework = None
        if framework is not None:
            info["framework"] = framework
        # 사람이 중지해서 결과 없이 끝난 실행은 화면에 실패가 아니라 주의 색으로 보여 준다
        if info.get("state") == "finished" and info.get("exit_code") in (EXIT_ABORTED, None) and (
                info.get("stop_requested") or info.get("control") == RunState.STOPPED.value
                or str(info.get("abort_reason") or "").startswith(USER_STOP_PREFIX)
                or str(info.get("error") or "").startswith(STOP_BEFORE_START_ERROR)):
            info["user_stop"] = True
        pending = self._final_pass_pending(run, control)       # 마지막 마스킹 전에는 기록할 때 마스킹되는 파일만 목록에 넣는다
        for name in SERVED_FILES:
            if (rd / name).is_file() and (not pending or name in LIVE_FILES):
                info.setdefault("files", []).append(name)
        if not pending:            # 코드 변경 요약도 다른 결과 파일처럼 마지막 마스킹 뒤에만 내준다
            from .codediff import read_summary
            code_diff = read_summary(rd)
            if code_diff is not None:
                info["code_diff"] = code_diff
        return info

    def _mem_state(self, run: Run) -> str:
        # 슬롯을 가졌으면 running이다. active를 세우기 직전이나 복구 직후에 queued로 보이지 않게 한다
        if run.done.is_set():
            return "finished"
        if run.active.is_set() or self._turns.slot_of(run.key) is not None:
            return "running"
        return "queued"

    def parallel(self) -> dict[str, int]:
        """동시 실행 현황 ``{slots, running, waiting}``을 반환한다.
        ``slots``는 한도다. ``running``은 슬롯을 가진 실행 수, ``waiting``은 대기 중인 실행 수다.
        복구 직후에는 ``running``이 한도보다 많을 수 있다."""
        running, waiting = self._turns.counts()
        return {"slots": self.max_parallel, "running": running, "waiting": waiting}

    def _unreadable_row(self, run_id: str) -> dict[str, Any]:
        """목록 줄을 만들지 못한 실행의 기본 줄을 반환한다. status()가 그런 폴더에 내는 항목과 같다.
        파일 시스템 오류로 예외를 던지지 않는다. 들어갈 수 없는 폴더(권한 000, NFS의 다른 uid)는 ``unknown``이다."""
        rd = self.run_dir(run_id)
        try:
            exists = rd.is_dir()
        except OSError:
            exists = True                      # 목록에 올라온 폴더다
        try:
            state = _folder_state(rd)
        except OSError:                        # 들어갈 수 없는 폴더는 상태를 알 수 없다
            state = "unknown"
        return {"run_id": run_id, "exists": exists or run_id in self.runs, "mode": self.effective_mode,
                "flow": FLOW_FULL, "upload": None, "zip_name": contract.zip_download_name(run_id, "migration"),
                "state": state, "exit_code": None}

    def list_runs(self) -> list[dict[str, Any]]:
        """메모리와 출력 폴더에 있는 모든 실행의 상태를 최신순으로 반환한다. ``_``로 시작하는 폴더는 뺀다.
        ``parallel``은 라우트가 한 번만 붙인다. 줄 하나를 만들지 못하면 WARNING을 남기고 읽지 못한 실행으로 넣는다."""
        ids = set(self.runs)
        if self.out_dir.is_dir():
            ids |= {p.name for p in self.out_dir.iterdir() if not p.name.startswith("_") and _is_dir(p)}
        # 폴더도 메모리도 없는 실행은 저널을 어디까지 읽었는지 지운다. 다시 조회하면 저널을 처음부터 읽는다
        with self._fw_lock:
            for rid in [k for k in self._fw_scans if k not in ids]:
                del self._fw_scans[rid]
        rows = []
        for rid in ids:
            try:
                st = self.status(rid)
            except Exception as e:  # noqa: BLE001 - 실행 하나 때문에 목록 전체가 실패하면 안 된다
                # 파일을 읽지 못한 실행처럼 넣는다. 응답과 경고에는 예외 문장을 넣지 않는다
                log.warning("run list: the row of run %s could not be built (%s); listed as unreadable",
                            rid, type(e).__name__)
                st = self._unreadable_row(rid)
            st.pop("parallel", None)
            run = self.runs.get(rid)
            try:
                st["started"] = run.started if run else self.run_dir(rid).stat().st_mtime
            except OSError:
                st["started"] = 0
            rows.append(st)
        return sorted(rows, key=lambda r: r.get("started", 0), reverse=True)

    # 사람의 확인을 기다리거나 아직 진행 중인 조치 상태. 오래된 실행이어도 목록에 남긴다
    WAITING_REMEDIATION = ("AWAITING_CONFIRMATION", "QUEUED", "RUNNING")

    def list_recent(self, days: int = 0, keep: str | None = None) -> dict[str, Any]:
        """최근 ``days``일 안에 시작한 실행만 담은 목록 ``{runs, days, hidden}``을 반환한다. ``days``가 0이면 모두 담는다.
        기간과 상관없이 담는 실행도 있다. 대기나 진행 중인 실행, IVAS 확인을 기다리거나 진행 중인 조치,
        재개 중인 조치, 화면이 열고 있는 실행(``keep``)이다.

        A remediation an older version left waiting for approval is not kept. The boundary is inclusive
        (started at or after now - days x 24 h)."""
        rows = self.list_runs()
        if days <= 0:
            return {"runs": rows, "days": 0, "hidden": 0}
        cutoff = time.time() - days * 86400

        def always(r: dict[str, Any]) -> bool:
            rem = r.get("remediation") if isinstance(r.get("remediation"), dict) else {}
            rjob = r.get("remediation_job") if isinstance(r.get("remediation_job"), dict) else {}
            return (r.get("state") in ("queued", "running") or r.get("run_id") == keep
                    or rem.get("status") in self.WAITING_REMEDIATION or bool(rjob.get("active")))

        shown = [r for r in rows if (r.get("started") or 0) >= cutoff or always(r)]
        return {"runs": shown, "days": days, "hidden": len(rows) - len(shown)}

    # ---- 이벤트 스트림
    def events(self, run_id: str) -> Iterator[tuple[str, Any]]:
        """SSE로 보낼 (이벤트, 데이터)를 내보내는 블로킹 생성기. 실행이 끝나고 남은 것을 다 보낼 때까지 계속한다.
        - 처음에 ``status``를 보내고, 노드, 오류, journal, 로그의 새 줄을 시간순으로 보낸다
        - 한동안 보낼 것이 없으면 ``ping``을 보낸다. 끝나면 ``report``, ``status``, ``end``를 보낸다
        - 상태나 대기 순번이 바뀌면 ``status``를 다시 보낸다. 화면이 새로 고침 없이 보여 준다
        - 서버 종료 때는 ``end``에 reason을 담아 보내고 연결을 놓는다"""
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
            # journal과 로그는 같은 스레드가 기록하는 두 파일이다. 하나씩 비우면 화면에서 시간 순서가 뒤섞인다.
            # 한 번에 모아 시각으로 정렬한다. 시각이 없는 줄은 앞 줄의 시각을 따른다
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
                    last_key = key if key is not None else last_key     # 시각을 못 읽으면 파일 안 위치를 지킨다
                    batch.append((last_key, len(batch), item[0], item[1]))
                    # Job 모드나 지난 실행에서는 파드가 기록한 graph_node_end 줄을 node 이벤트로 바꾼다.
                    # 이 프로세스에서 실행한 것은 _push로 이미 받았다
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
            # 끝났는지는 비우기 전에 확인한다. done이 선 뒤에 비우면 실행이 기록한 내용이 모두 파일에 있다
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
            if finished:
                rep = rd / "report.md"
                if rep.is_file():
                    yield "report", rep.read_text(encoding="utf-8", errors="replace")
                yield "status", self.status(run_id)
                yield "end", {"run_id": run_id}
                return
            # 서버 종료 때는 연결을 놓는다. 그래야 uvicorn이 기다리지 않고 종료된다. 브라우저는 나중에 다시 연결한다
            if self.closing.is_set():
                yield "status", self.status(run_id)
                yield "end", {"run_id": run_id, "reason": "server shutdown"}
                return
            # 순번이 바뀌었거나 실행이 시작되었는지 먼저 가볍게 비교한다. 바뀌었을 때만 status()를 만든다
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
    # SSE가 status를 다시 보낼지 정하는 (상태, 대기 순번). 순번은 이 프로세스의 줄에서 기다릴 때만 있다
    return st.get("state"), (st.get("queue") or {}).get("position")


def _write_web_status(run: Run) -> None:
    """작업 스레드가 끝날 때 web-status.json(종료 코드, 오류, 중지 요청, 마지막 노드)을 기록한다.
    메모리에서 빠졌거나 서버를 다시 띄운 뒤에는 이 파일로 상태를 응답한다. 기록에 실패해도 실행 결과는 그대로다."""
    try:
        data = {"run_id": run.run_id, "exit_code": run.exit_code, "error": run.error, "flow": run.flow, "upload": run.upload,
                **({"project": run.project} if run.project else {}),
                "stop_requested": run.cancel.is_set(), "last_node": run.last_node,
                "success": run.success if run.success is not None else getattr(run.report, "success", None)}
        (run.run_dir / WEB_STATUS_FILE).write_text(json.dumps(data, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:   # 참고용 파일이다. 기록에 실패해도 실행을 실패로 만들지 않는다
        log.debug("cannot write %s for %s: %s", WEB_STATUS_FILE, run.run_id, e)


def _journal_job(rd: Path) -> dict[str, Any] | None:
    """저널에서 ``job_created`` 줄과 마지막 graph_node_end의 노드를 찾아 반환한다.
    이전 웹 프로세스가 만든 Job이 아직 실행 중인지 볼 때 쓴다.
    ``JOB_STALE_S``보다 오래되었거나 줄이 없으면 None을 반환한다."""
    try:
        created: dict[str, Any] | None = None
        last_node = None
        with open(rd / "journal.jsonl", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):    # 객체가 아닌 줄은 읽지 못한 줄처럼 건너뛴다
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


def _is_dir(p: Path) -> bool:
    # 확인하지 못한 항목은 실행 폴더로 보지 않는다(NFS에서 지워진 폴더의 ESTALE 등)
    try:
        return p.is_dir()
    except OSError:
        return False


def _folder_state(rd: Path) -> str:
    # 상태 파일이 없는 실행의 상태를 보고서와 폴더 유무로 정한다
    return "finished" if (rd / "report.json").is_file() else ("unknown" if rd.is_dir() else "missing")


def _read_job_status(rd: Path) -> dict[str, Any] | None:
    """Job 모드에서 파드가 기록한 ``job-status.json``을 읽는다. 없거나 깨졌으면 None을 반환한다."""
    try:
        data = json.loads((rd / JOB_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "exit_code" in data else None


def _report_success(rd: Path) -> bool | None:
    """report.json의 success 값을 반환한다. 파일이 없거나 깨졌으면 None을 반환한다."""
    try:
        return json.loads((rd / "report.json").read_text(encoding="utf-8")).get("success")
    except (OSError, ValueError, AttributeError):
        return None


def _append_journal(rd: Path, event: dict[str, Any]) -> None:
    """실행 폴더의 journal.jsonl에 ts를 붙인 한 줄을 추가한다. 기록하지 못해도 예외를 던지지 않는다.
    Job 모드에서 웹이 직접 기록하는 줄(web_run, job_created, stop_request_sent 등)에 쓴다."""
    try:
        line = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        text = json.dumps(line, default=str)
        if output_scrubbing():                    # 값 단위로 마스킹한다. 직렬화한 줄을 통째로 바꾸면 JSON이 깨질 수 있다
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
    """사용자 중지를 보고서에 반영한다. 마감 전 보고서를 aborted로 표시한다. report.md에 남고 종료 코드는 3이 된다.
    이미 마감되었거나 aborted면 아무것도 하지 않는다."""
    report = getattr(orch, "report", None)
    if report is None or getattr(report, "finished_at", None) is not None or "aborted" in report.flags:
        return
    try:
        abort = getattr(orch, "_abort", None)
        if callable(abort):
            abort(report, "stopped by user (web UI) — the final `mvn clean package` did not run")
        else:
            report.flags.append("aborted")
    except Exception:  # 중지 처리 때문에 보고서가 사라지면 안 된다
        log.exception("marking the stopped run failed")


def _finalize_stopped(orch: Any) -> None:
    """중지된 실행의 보고서를 마감한다. 엔진이 닫힐 때 마감하지 않았으면 여기서 report.md와 report.json을 기록한다.
    예외는 로그만 남긴다. 중지 처리 때문에 보고서가 사라지면 안 된다."""
    report = getattr(orch, "report", None)
    if report is None or getattr(report, "finished_at", None) is not None:
        return
    _mark_stopped(orch)
    try:
        finalize = getattr(orch, "_finalize", None)
        if callable(finalize):
            finalize(report)
    except Exception:  # 중지 처리 때문에 보고서가 사라지면 안 된다
        log.exception("finalizing the stopped run failed")


def _detach_file_log(path: Path) -> None:
    """이 파일에 붙은 실행별(스레드 필터) 핸들러만 뗀다. 같은 파일에 붙은 프로세스 전체 핸들러는 남긴다."""
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
    ``MAX_UPDATE_CHARS``를 넘으면 키 이름만 보낸다. 화면이 무거워지지 않게 한다."""
    try:
        text = json.dumps(update, default=_json_default, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(str(update))
    if len(text) > MAX_UPDATE_CHARS:
        keys = sorted(update) if isinstance(update, dict) else []
        return {"_truncated": len(text), "keys": keys}
    return json.loads(text)


def _new_lines(path: Path, offsets: dict[Path, int]) -> list[str]:
    """파일에 새로 추가된 완성된 줄만 반환한다. 읽은 위치는 ``offsets``에 기억한다.
    줄바꿈이 오지 않은 마지막 조각은 다음으로 미룬다. 반쪽 줄이나 깨진 JSON을 보내지 않는다."""
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
    if nl < 0:                                   # 반쪽 줄이면 나머지를 기다린다
        return []
    offsets[path] = pos + nl + 1
    return chunk[:nl].decode("utf-8", errors="replace").splitlines()
