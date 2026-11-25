"""취약점 조치의 접수, 실행, 상태 조회, 재개를 관리하는 모듈. ``api.py``의 라우트가 :class:`RemediationManager`를 호출한다.
- in-process 모드에서만 작업 스레드로 조치를 실행한다. 한 번에 하나씩 접수 순서대로 실행한다
- Job 모드에서는 아무것도 실행하지 않고, 단독 업로드와 재개 요청은 ``NotAvailable``(HTTP 409)로 거절한다
- 재개는 사람이 요청할 때만 한다. 서버가 재시작되면 QUEUED / RUNNING이던 조치는 INTERRUPTED가 된다
- 이관 뒤 이어서 실행한 조치(``<out>/<이관 실행 id>/remediation/``)도 같은 API로 보여 준다. 삭제는 거절한다
- Job 모드에서 그 조치를 재개하면 확인 응답을 state.json에 먼저 기록하고, 새 빌드 파드는 ``RunManager``가 만든다
- 이관은 끝났는데 QUEUED / RUNNING으로 남은 조치는 파일을 고치지 않고 INTERRUPTED(``stale: true``)로 보여 준다
- 단독 업로드와 in-process 재개는 ``<build.local_repo>-remediation`` 저장소를 쓴다. 이관 뒤 조치는 이관의 저장소를 쓴다
- 작업 폴더의 상태를 바꿀 때는 ``RunClaim``으로 점유한다. 다른 프로세스가 점유 중이면 아무것도 바꾸지 않는다
- 진행하던 쪽이 없어진 끝 상태는 결과 ZIP 기록과 보고서만 맞추고, 상태와 사유와 메시지는 그대로 둔다
- 이전 버전이 AWAITING_APPROVAL로 저장한 조치는 끝난 작업으로 보여 주고 재개를 거절한다
- 상태는 ``state.json`` 파일에서 읽는다(DB 없음)

The remediation job manager: worker threads of this process (one remediation at a time), `state.json` per
run, resume / stop / delete, and the restart rule (QUEUED / RUNNING of an earlier process → INTERRUPTED, never
resumed automatically). File-based only.
- Job modes (`k8s-job` / `local-job`): nothing runs here (submit / resume → `NotAvailable`).
- Chained remediations (`<out>/<run id>/remediation/`, written by the build pod after a successful migration)
  are visible through the same API in every mode. In the Job modes a resume first writes the confirmation
  answer into their `state.json`, then `RunManager.resume_remediation` creates a NEW build pod. Deleting one
  is refused. A chained remediation still QUEUED / RUNNING after its migration run ended is shown as
  INTERRUPTED (`stale: true`) without touching its files.
- Local repository: a standalone upload and an in-process resume use `<build.local_repo>-remediation`, never
  the migration builds' repository; a chained resume keeps the repository of its migration.
- The service takes the run claim (`RunClaim(purpose="service")`) whenever it changes a run folder itself: to run
  a job, to repair a final state whose owner is gone, to write a pending report at startup. It does not take it to
  read; a claim another process holds means nothing is changed.
- A state an earlier version saved in `AWAITING_APPROVAL` (the approval step was removed) is served as it is,
  is neither active nor resumable, and a resume of it is refused (`StateConflict`, HTTP 409) with
  `LEGACY_APPROVAL_MESSAGE` and the Korean `message_ko`.
"""
from __future__ import annotations

import collections
import dataclasses
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sbmig.core import scrub as _scrub

from . import graph as graph_mod
from . import i18n as i18n_mod
from . import intake as intake_mod
from . import ivas as ivas_mod
from . import planner as planner_mod
from . import report as report_mod
from .inhouse import inhouse_keys
from .interfaces import Components
from .models import (
    ACTIVE,
    AWAITING_CONFIRMATION,
    FAILED,
    INTERRUPTED,
    LEGACY_APPROVAL_MESSAGE_KO,
    LEGACY_AWAITING_APPROVAL,
    QUEUED,
    R_INTERNAL_ERROR,
    REMEDIATIONS_DIR,
    RESUMABLE,
    STOPPED,
    TERMINAL,
    NotReady,
    RemediationError,
    RemediationState,
    RunPaths,
    StateConflict,
    UploadReceipt,
    read_json,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.service")

# 작업 ID는 r<날짜-시각>-<무작위 16진수>다. 이름순이 시간순이고, 무작위 부분 때문에 다른 작업의 주소를 짐작할 수 없다
ID_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{6,32}$")
# 이관 뒤 이어서 실행한 조치의 ID는 이관 실행 id이며 sbmig.web.runs.RUN_ID_RE와 같은 규칙이다
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
from .chain import CHAIN_DIR  # noqa: E402. 이관 실행 폴더 안의 조치 폴더 이름
from .chain import RunClaim  # noqa: E402. 상태를 진행시키는 프로세스 하나만 작업 폴더를 쓰게 하는 점유
STOP_REQUEST_FILE = "stop.request"            # sbmig.core.workspace.STOP_REQUEST_FILE. 다른 파드의 실행을 멈추는 파일
JOB_MODES = ("k8s-job", "local-job")
IN_PROCESS = "in-process"
KIND_CHAINED, KIND_STANDALONE = "chained", "standalone"
LIVE_FILES = ("journal.jsonl",)               # 쓸 때 바로 마스킹되므로 최종 마스킹 전에도 내려받을 수 있는 파일
# 이관 실행이 끝났다고 보는 RunManager.status 값이다. 이때 조치가 QUEUED / RUNNING이면 파드가 사라진 것이다
_RUN_ENDED = ("finished", "unknown", "missing")
NOT_AVAILABLE_MESSAGE = ("remediation runs in the build pod after a successful migration (remediation.after_migration); "
                         "the web pod runs nothing in {mode} mode")
RESUME_UNSUPPORTED = ("this web server cannot dispatch a build pod for the resume ({mode} mode without a run manager that "
                      "resumes remediations)")
RESUME_ACTIVE_NOTE = "이미 새 빌드 파드에서 이어서 진행 중이거나 자리를 기다리고 있습니다."      # 화면에 그대로 보이는 문장
LEGACY_STATUS_KO = "조치 미완료"     # 이전 버전이 승인 대기로 저장한 작업의 상태 이름이다. 화면에 보이므로 승인이라는 말을 쓰지 않는다
CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
FALLBACK_CONFIGS = ("artifacts.yml", "artifacts.local-e2e.yml")       # RunManager.FALLBACK_CONFIGS와 같은 규칙
LOCAL_REPO_SUFFIX = "-remediation"
MAX_PAGE_SIZE = 100
SHUTDOWN_JOIN_S = 5.0
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
STOP_NOTE = "takes effect at the next workflow boundary; a running IVAS analysis is not cancelled"
RESTART_MESSAGE = ("the server restarted while this remediation was {status}; nothing is re-sent automatically — "
                   "check the state and resume it explicitly")
SHUTDOWN_MESSAGE = ("the server shut down while this remediation was running; nothing is re-sent automatically — "
                    "resume it explicitly")
CONFIRM_REUPLOAD, CONFIRM_RECEIPT = "reupload", "receipt"
# 경로 조작을 막기 위해 내려받을 수 있는 파일 이름을 정해 둔다. 그 밖에는 아래 정규식의 라운드, 검사 파일만 허용한다
_FIXED_FILES: dict[str, Callable[[RunPaths], Path]] = {
    "report.md": lambda p: p.report_md,
    "report.json": lambda p: p.report_json,
    "result.zip": lambda p: p.delivery_zip,
    "source.zip": lambda p: p.original_zip,
    "manifest.json": lambda p: p.manifest,
    "journal.jsonl": lambda p: p.journal,
    "remediation.log": lambda p: p.log,
    "delivery.json": lambda p: p.delivery_json,
    # include 정책으로 실패한 수정본이 result.zip이 되었을 때 따로 보관한 앞 라운드의 검증된 ZIP
    "result-verified.zip": lambda p: graph_mod.find_verified_zip(p) or p.delivery_zip.with_name("result-verified.zip"),
    "baseline-build.log": lambda p: p.baseline_log,
    # 결과에 들어간 모든 자동 수정 라운드의 소스 diff를 모은 파일(report.write_fix_patch)
    "fix.patch": lambda p: _last_fix_patch(p),
}


def _last_fix_patch(paths: RunPaths) -> Path:
    """보고서가 모아 쓴 ``fix.patch``(``report.md`` 옆)의 경로를 반환한다.
    결과에 들어간 자동 수정 라운드가 없거나 state를 읽지 못하면 존재하지 않는 경로(``rounds/fix.patch``)를 반환한다.

    The combined `fix.patch` that `report.write_fix_patch` writes next to `report.md`, served only when the saved state
    has a round committed with `fix.fixed` and a non-empty `fix.changed` (`report.served_fix_round`) AND the file
    exists (the caller checks `is_file`). A combined file left from an earlier report while the state no longer has
    such a round is not served; per-round patches are never served under this name."""
    none = paths.root / "rounds" / "fix.patch"
    try:
        data = read_json(paths.state)
        if not isinstance(data, dict):
            return none
        rs = report_mod.served_fix_round(RemediationState.from_dict(data))
    except Exception:  # noqa: BLE001. 상태를 읽지 못하면 내려 주지 않는다
        log.debug("fix.patch: state unreadable", exc_info=True)
        return none
    return report_mod.fix_patch_path(paths) if rs is not None else none


_ROUND_FILES: dict[str, Callable[[RunPaths, int], Path]] = {
    "plan": lambda p, n: p.plan(n),
    "candidate": lambda p, n: p.patch(n),
    "build": lambda p, n: p.build_log(n),
}
_ROUND_FILE_RE = re.compile(r"^(plan|candidate|build)-r([0-9]{1,4})\.(json|patch|log)$")
_ROUND_FILE_EXT = {"plan": "json", "candidate": "patch", "build": "log"}
_SCAN_FILE_RE = re.compile(r"^scan-(baseline|final-r[0-9]{1,4})\.json$")
_ABS_PATH = re.compile(r"(?<![\w:/\\.])(?:[A-Za-z]:[\\/]|/)[^\s()'\"]+")



def _status_ko(status: str) -> str:
    """상태 코드에 맞는 보고서의 결과 문장을 그림 문자와 굵게 표시를 빼고 반환한다. 모르는 코드는 그대로 반환한다."""
    from .report import _VERDICT
    if status == LEGACY_AWAITING_APPROVAL:
        return LEGACY_STATUS_KO
    v = _VERDICT.get(status, "")
    return v.split(" ", 1)[1].replace("**", "") if v else str(status or "")

class UnknownRemediation(RemediationError):
    """ID에 맞는 작업이 없을 때 던지는 오류(HTTP 404)."""
    code = "not_found"


class BadRemediationId(RemediationError):
    """ID가 ``ID_RE`` 형식에 맞지 않을 때 던지는 오류(HTTP 400). 파일 시스템에 접근하기 전에 걸러낸다."""
    code = "bad_id"


class NotFinished(StateConflict):
    """작업이 아직 끝나지 않아 report.json을 줄 수 없을 때 던지는 오류(HTTP 409 ``not_finished``)."""
    code = "not_finished"


class NotAvailable(RemediationError):
    """현재 웹 모드에서 할 수 없는 요청일 때 던지는 오류(HTTP 409 ``not_available``).
    Job 모드의 단독 업로드와 재개, chain 모듈이 없는 경우다."""
    code = "not_available"


class Withheld(StateConflict):
    """이관 실행의 최종 마스킹이 아직 끝나지 않아 조치 파일을 내줄 수 없을 때 던지는 오류(HTTP 409 ``withheld``).

    Only chained files other than the journal wait."""
    code = "withheld"


ComponentsFactory = Callable[[Any, "RunPaths | None"], Components]
ConfigLoader = Callable[[list[str]], Any]


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_problem(text: str) -> str:
    """준비 문제 문장에서 비밀 값을 지우고, 서버 절대 경로는 마지막 이름만 남긴다."""
    out = _ABS_PATH.sub(lambda m: Path(m.group(0).rstrip("\\/")).name or "<path>", str(text))
    return graph_mod.scrub_message(out, limit=500)


def remediation_config(cfg: Any) -> Any:
    """조치용 설정을 반환한다. ``build.local_repo``가 있으면 ``<그 경로>-remediation``으로 바꾼 복사본을 반환한다.
    이관 빌드와 같은 로컬 저장소를 쓰지 않기 위해서다. 이미 그 이름이면 그대로 반환한다."""
    build = getattr(cfg, "build", None)
    repo = getattr(build, "local_repo", None) if build is not None else None
    if not repo or not str(repo).strip():
        return cfg
    base = str(repo).strip().rstrip("/\\")
    if base.endswith(LOCAL_REPO_SUFFIX):
        return cfg
    new = base + LOCAL_REPO_SUFFIX
    if hasattr(cfg, "model_copy") and hasattr(build, "model_copy"):
        return cfg.model_copy(update={"build": build.model_copy(update={"local_repo": new})})
    import copy
    out = copy.copy(cfg)
    out.build = copy.copy(build)
    out.build.local_repo = new
    return out


def default_components_factory(cfg: Any, paths: RunPaths | None) -> Components:
    """운영용 구성 요소(``graph.default_components``)를 만든다. ``paths``는 쓰지 않고, 준비 확인 때는 None이 넘어온다."""
    return graph_mod.default_components(cfg)


class _ScrubbingFormatter(logging.Formatter):
    """로그를 파일에 쓰기 직전에 비밀 값, URL 속 계정 정보, 실행 폴더 절대 경로를 지우는 포매터 클래스."""

    def __init__(self, root: Path):
        super().__init__(LOG_FORMAT)
        self.root = root

    def format(self, record: logging.LogRecord) -> str:
        return graph_mod.scrub_message(super().format(record), self.root, limit=200_000)


class _ThreadFilter(logging.Filter):
    """다른 실행의 로그가 섞이지 않도록 한 작업 스레드와 그 컨텍스트를 물려받은 스레드의 로그만 통과시키는 필터 클래스."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread == self.thread_id:
            return True
        try:
            from sbmig.cli import _LOG_THREAD
            return _LOG_THREAD.get() == self.thread_id
        except Exception:  # noqa: BLE001 - CLI 컨텍스트 변수가 없어도 스레드 번호로 거른다
            return False


class RemediationManager:
    """취약점 조치 작업을 접수, 실행, 관리하는 클래스. 웹 앱마다 하나 있다.
    상태는 ``<out>/_remediations/<id>/``의 파일로 관리하고, Job 모드면 작업 스레드를 띄우지 않는다.

    Tests pass fakes as `components_factory` / `config_loader`. `default_configs` is a list of config names or
    a callable returning them (the web app passes `RunManager.default_configs` so both features resolve the
    server default the same way). `mode` is the web mode (Job modes: no worker thread, ever); `run_manager`
    (optional) answers for the migration runs of chained remediations (run status, final redaction, stop)."""

    def __init__(self, out_dir: Path, repo_root: Path, default_configs: list[str] | Callable[[], list[str]] | None = None,
                 components_factory: ComponentsFactory | None = None, config_loader: ConfigLoader | None = None,
                 mode: str | None = None, run_manager: Any = None):
        self.out_dir = Path(out_dir)
        self.repo_root = Path(repo_root)
        self.mode = (mode or IN_PROCESS).strip() or IN_PROCESS
        self.run_manager = run_manager
        self.root = self.out_dir / REMEDIATIONS_DIR
        self._default_configs = default_configs
        self.components_factory: ComponentsFactory = components_factory or default_components_factory
        self.config_loader: ConfigLoader = config_loader or self._load_config
        self.owner = secrets.token_hex(16)            # 재시작 뒤 남은 작업을 가려내기 위한 이 프로세스의 토큰
        self._lock = threading.RLock()                # index, 대기열, 잠금, 이벤트를 보호한다
        self._cond = threading.Condition(self._lock)
        self._run_locks: dict[str, threading.RLock] = {}
        self._events: dict[str, threading.Event] = {}
        self._live: dict[str, RemediationState] = {}  # 중지 요청을 바로 넣을 수 있도록 작업 스레드가 쓰는 상태 객체를 둔다
        self._queue: collections.deque[str] = collections.deque()
        self._active: set[str] = set()                # 대기 중이거나 실행 중인 작업
        self._index: set[str] = set()
        self._worker: threading.Thread | None = None
        self._closing = threading.Event()
        self.interrupted: list[str] = []
        # 끝 상태 폴더를 정리한 기록 {폴더: (state.updated, 확인한 시각, 더 할 일이 없는지)}이다
        # 최근 REPAIR_SEEN_MAX개만 두고, 밀려난 폴더는 다음에 한 번 더 확인한다
        self._repair_seen: collections.OrderedDict[str, tuple[float, float, bool]] = collections.OrderedDict()
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()

    @property
    def job_mode(self) -> bool:
        """Job 모드(``k8s-job`` / ``local-job``)인지 확인한다. Job 모드면 이 프로세스는 조치를 실행하지 않는다."""
        return self.mode in JOB_MODES

    def _not_available(self) -> NotAvailable:
        return NotAvailable(NOT_AVAILABLE_MESSAGE.format(mode=self.mode), mode=self.mode)

    # ------------------------------------------------------------------ 설정
    def default_configs(self) -> list[str]:
        """서버의 기본 설정 이름 목록을 반환한다. ``RunManager.default_configs``와 같은 규칙이다."""
        d = self.repo_root / "config"
        src = self._default_configs
        if callable(src):
            return list(src())
        if src:
            return [c for c in src if CONFIG_NAME_RE.match(c) and (d / c).is_file()]
        return next(([c] for c in FALLBACK_CONFIGS if (d / c).is_file()), [])

    def _load_config(self, configs: list[str]) -> Any:
        from sbmig.core.config import load_config
        return load_config(*[self.repo_root / "config" / c for c in configs], repo_root=self.repo_root)

    def _config(self, configs: list[str]) -> Any:
        """설정 이름을 검사한 뒤 설정을 읽는다. 모르는 이름이면 ValueError(400), 읽지 못하면 NotReady(503)를 던진다."""
        for c in configs:
            if not CONFIG_NAME_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        try:
            return self.config_loader(list(configs))
        except Exception as e:  # noqa: BLE001 - pydantic 검증 오류나 yml 오류면 서버 설정을 쓸 수 없다
            problem = f"configuration cannot be loaded ({type(e).__name__})"
            raise NotReady(problem, missing=[problem]) from None

    def max_upload_bytes(self) -> int:
        """업로드 상한(바이트)을 반환한다.
        기본 설정의 ``remediation.max_upload_mb``를 쓰고, 읽지 못하면 코드 기본값 1024 MiB를 쓴다."""
        from sbmig.core.config import RemediationConfig
        try:
            mb = int(self._config(self.default_configs()).remediation.max_upload_mb)
        except Exception:  # noqa: BLE001 - artifacts yml이 없어도 웹 앱은 떠야 한다
            mb = int(RemediationConfig().max_upload_mb)
        return max(0, mb) * 1024 * 1024

    # ------------------------------------------------------------------ 경로와 ID
    def _check_id(self, rid: str) -> str:
        if not isinstance(rid, str) or not ID_RE.match(rid):
            raise BadRemediationId("bad remediation id")
        return rid

    def _migration_run_dir(self, run_id: str) -> Path:
        rm = self.run_manager
        if rm is not None and hasattr(rm, "run_dir"):
            return Path(rm.run_dir(run_id))
        return self.out_dir / run_id

    def chain_paths(self, run_id: str) -> RunPaths:
        """ID를 검사한 뒤 이관 뒤 이어서 실행한 조치의 경로 ``<out>/<이관 실행 id>/remediation/``을 반환한다."""
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
            raise BadRemediationId("bad remediation id")
        return RunPaths(self._migration_run_dir(run_id) / CHAIN_DIR)

    def _locate(self, rid: str) -> tuple[RunPaths, bool]:
        # 단독 조치 ID인지 먼저 보고, 이관 실행 id 형식이면 이어서 실행한 조치로 본다
        # 둘 다 아니면 파일 시스템에 접근하기 전에 걸러낸다
        if not isinstance(rid, str):
            raise BadRemediationId("bad remediation id")
        if ID_RE.match(rid):
            own = RunPaths(self.root / rid)
            if own.state.is_file():
                return own, False
            chained = self.chain_paths(rid)
            return (chained, True) if chained.state.is_file() else (own, False)
        return self.chain_paths(rid), True

    def is_chained(self, rid: str) -> bool:
        """이 ID가 이관 뒤 이어서 실행한 조치인지 확인한다."""
        return self._locate(rid)[1]

    def paths(self, rid: str) -> RunPaths:
        """ID를 검사한 뒤 작업 폴더 경로를 반환한다. 이관 뒤 이어서 실행한 조치면 이관 실행 폴더 안의 경로를 반환한다."""
        return self._locate(rid)[0]

    def zip_name(self, rid: str) -> str:
        """결과 ZIP을 내려받을 때 쓰는 이름 ``<프로젝트명>-remediation.zip``을 반환한다.
        이관 실행 폴더에 입력한 프로젝트명이 없거나 단독 조치면 ID를 쓴다.

        Contract "v2.1z-bt": `contract.zip_download_name(rid, "remediation",
        project)`. A chained or remediation-flow run (its id is the migration run id) reads the project entered at
        upload from the migration run folder (`contract.entered_project`); a standalone remediation has none, so its id
        stands in. A bad id raises `BadRemediationId` as everywhere here; reading the project never fails (any error
        leaves it out)."""
        paths, chained = self._locate(rid)
        return self._zip_name(rid, paths, chained)

    @staticmethod
    def _zip_name(rid: str, paths: RunPaths, chained: bool) -> str:
        # sbmig.web 패키지는 웹 앱 전체를 불러오므로 모듈 맨 위가 아니라 여기서 import한다
        from sbmig.web import contract
        project = None
        if chained:
            try:
                project = contract.entered_project(paths.root.parent)
            except Exception:  # 보여 주는 이름이므로 읽지 못해도 요청은 실패하지 않는다
                log.debug("remediation %s: entered project name not read", rid, exc_info=True)
        return contract.zip_download_name(rid, "remediation", project)

    def _new_id(self) -> str:
        while True:
            rid = f"r{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(6)}"
            if not (self.root / rid).exists():
                return rid

    def incoming_path(self) -> Path:
        """받는 중인 업로드를 담을 새 임시 파일 경로 ``<out>/_remediations/.incoming/<hex>.part``를 반환한다."""
        d = self.root / ".incoming"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{uuid.uuid4().hex}.part"

    def _run_lock(self, rid: str) -> threading.RLock:
        with self._lock:
            lk = self._run_locks.get(rid)
            if lk is None:
                lk = self._run_locks[rid] = threading.RLock()
            return lk

    def _load(self, rid: str) -> RemediationState:
        paths = self.paths(rid)
        data = read_json(paths.state)
        if not isinstance(data, dict):
            raise UnknownRemediation(f"unknown remediation {rid}")
        return RemediationState.from_dict(data)

    def _save(self, paths: RunPaths, state: RemediationState) -> None:
        write_json_atomic(paths.state, state.to_dict())

    def _journal(self, paths: RunPaths, event: dict[str, Any]) -> None:
        line = _scrub.scrub_json({"ts": _iso(), **event})
        text = json.dumps(line, ensure_ascii=False, default=str)
        with open(paths.journal, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def _journal_safe(self, paths: RunPaths, event: dict[str, Any]) -> None:
        try:
            self._journal(paths, event)
        except Exception:
            log.debug("journal line not written", exc_info=True)

    # ------------------------------------------------------------------ 이어서 실행한 조치: 이관 실행 정보
    def _migration_state(self, run_id: str) -> str | None:
        """이관 실행의 상태(``RunManager.status``)를 반환한다. 모르면 None을 반환한다.
        RunManager가 없으면 빌드 파드나 작업 스레드가 마지막에 쓰는 상태 파일이 있는지로 판단한다."""
        rm = self.run_manager
        if rm is not None and hasattr(rm, "status"):
            try:
                return str((rm.status(run_id) or {}).get("state") or "") or None
            except Exception:  # noqa: BLE001 - 이관 상태를 읽지 못하면 모르는 것으로 본다
                log.debug("migration status of %s not readable", run_id, exc_info=True)
                return None
        rd = self._migration_run_dir(run_id)
        if (rd / "job-status.json").is_file() or (rd / "web-status.json").is_file():
            return "finished"
        return None

    def _migration_ended(self, run_id: str) -> bool:
        return self._migration_state(run_id) in _RUN_ENDED

    # ---- 이어서 실행한 조치: RunManager의 재개 Job
    def _resume_job(self, run_id: str) -> dict[str, Any] | None:
        """이 조치의 재개 Job 정보(``RunManager.remediation_job``)를 반환한다. 기능이 없거나 읽지 못하면 None을 반환한다."""
        fn = getattr(self.run_manager, "remediation_job", None) if self.run_manager is not None else None
        if not callable(fn):
            return None
        try:
            out = fn(run_id)
        except Exception:  # noqa: BLE001 - 재개 Job을 읽지 못하면 모르는 것으로 본다
            log.debug("remediation_job(%s) failed", run_id, exc_info=True)
            return None
        return out if isinstance(out, dict) else None

    def _resume_active(self, run_id: str) -> bool:
        """재개 Job이 대기 중이거나 실행 중인지 확인한다. 확인하지 못하면 True를 반환한다.
        그동안에는 그 빌드 파드가 state를 쓰므로 재개를 거절하고 파일 제공을 보류한다."""
        fn = getattr(self.run_manager, "remediation_active", None) if self.run_manager is not None else None
        if not callable(fn):
            return False
        try:
            return bool(fn(run_id))
        except Exception:  # noqa: BLE001 - 모르면 빌드 파드가 state를 쓰는 중일 수 있으므로 거절한다
            log.debug("remediation_active(%s) failed", run_id, exc_info=True)
            return True

    def _can_dispatch(self) -> bool:
        # Job 모드에서 새 빌드 파드로 재개하려면 RunManager가 재개 Job을 만들 수 있어야 한다
        return self.run_manager is not None and callable(getattr(self.run_manager, "resume_remediation", None))

    def _withheld(self, run_id: str) -> bool:
        """조치 파일 제공을 보류해야 하는지 확인한다. 확인하지 못하면 보류한다.
        이관 실행의 최종 마스킹이 남았거나 재개 Job이 대기 중 또는 실행 중이면 보류한다."""
        if self._resume_active(run_id):
            return True
        rm = self.run_manager
        if rm is None or not hasattr(rm, "file_withheld"):
            return False
        try:
            return bool(rm.file_withheld(run_id, "report.md"))
        except Exception:  # noqa: BLE001 - 모르면 보류한다. 마스킹이 파일을 다시 쓸 수 있다
            log.debug("file_withheld of %s failed", run_id, exc_info=True)
            return True

    def _stale(self, rid: str, st: RemediationState, chained: bool) -> bool:
        # 조치가 QUEUED / RUNNING인데 이관 실행이 끝났다면 파드가 사라진 것이다. 이 프로세스가 재개한 경우는 제외한다
        if not chained or st.status not in ACTIVE:
            return False
        with self._lock:
            if rid in self._active:
                return False
        if self._resume_active(rid):          # 새 빌드 파드의 재개 Job이 이 상태를 쓰고 있다
            return False
        return self._migration_ended(rid)

    def _recorded_configs(self, run_id: str) -> list[str]:
        # 이관 실행이 접수 때 기록한 설정 이름을 찾는다. Job 모드는 control/request.json, in-process는 journal에 있다
        rd = self._migration_run_dir(run_id)
        data = read_json(rd / "control" / "request.json")
        if isinstance(data, dict) and isinstance(data.get("configs"), list):
            return [str(c) for c in data["configs"]]
        try:
            with open(rd / "journal.jsonl", encoding="utf-8") as f:
                for line in f:
                    if '"web_run"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") == "web_run" and isinstance(ev.get("configs"), list):
                        return [str(c) for c in ev["configs"]]
        except OSError:
            pass
        return []

    def chained_configs(self, run_id: str) -> list[str]:
        """이관 뒤 이어서 실행한 조치의 정책 해시에 쓸 설정 이름을 반환한다.
        이관 실행이 쓴 이름을 찾고, 기록이 없거나 이 서버의 config/에 없는 이름이 있으면 서버 기본값을 쓴다."""
        d = self.repo_root / "config"
        recorded = self._recorded_configs(run_id)
        usable = [c for c in recorded if CONFIG_NAME_RE.match(c) and (d / c).is_file()]
        if recorded and len(usable) == len(recorded):
            return usable
        if recorded:
            log.warning("remediation %s: the migration's configs %s are not all under config/ — using the server default",
                        run_id, ",".join(recorded))
        return self.default_configs()

    def _configs_for(self, rid: str, st: RemediationState, chained: bool) -> list[str]:
        return self.chained_configs(rid) if chained else list(st.configs)
    # ------------------------------------------------------------------ 재시작 규칙
    def _recover(self) -> None:
        """시작할 때 모든 state.json을 검사해 다른 프로세스가 남긴 QUEUED / RUNNING 작업을 INTERRUPTED로 바꾼다.
        자동으로 재개하지 않는다. 끝 상태인데 정리가 남은 작업은 ``_write_pending_report``로 정리한다."""
        for d in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not d.is_dir() or not ID_RE.match(d.name):
                continue
            paths = RunPaths(d)
            data = read_json(paths.state)
            if not isinstance(data, dict):
                continue
            self._index.add(d.name)
            st = RemediationState.from_dict(data)
            if st.status in ACTIVE and st.owner != self.owner:
                was = st.status
                st.status = INTERRUPTED
                st.message = RESTART_MESSAGE.format(status=was)
                st.updated = _now()
                try:
                    self._save(paths, st)
                    self._journal_safe(paths, {"event": "interrupted", "was": was, "next_node": st.next_node})
                    self.interrupted.append(d.name)
                except OSError as e:
                    log.warning("remediation %s: could not mark INTERRUPTED: %s", d.name, type(e).__name__)
            elif graph_mod.final_repair_needed(paths, st):
                self._write_pending_report(d.name, paths, st)
        if self.interrupted:
            log.warning("remediations interrupted by a server restart (resume explicitly): %s", ", ".join(self.interrupted))

    def _write_pending_report(self, rid: str, paths: RunPaths, st: RemediationState) -> None:
        """끝 상태로 남은 단독 조치의 결과 ZIP 기록과 보고서를 시작할 때 맞춘다. 실패해도 시작을 막지 않는다.

        At startup, in every mode (v2.1z-bo): a standalone final state whose report node was
        killed (`graph.report_pending`) or whose result ZIP swap was left half done gets `graph.repair_final` once,
        under the run claim."""
        try:
            claim = RunClaim(paths.root, purpose="service").acquire()
        except StateConflict:
            return
        try:
            cfg = remediation_config(self._config(list(st.configs)))
            graph_mod.repair_final(st, paths, cfg, save=lambda s: self._save(paths, s),
                                   journal=lambda e: self._journal_safe(paths, e))
        except Exception as e:  # noqa: BLE001 - 보고서를 못 써도 서버 시작은 막지 않는다
            log.warning("remediation %s: the report was not written at startup: %s", rid, type(e).__name__)
        finally:
            claim.release()

    # ------------------------------------------------------------------ 진행하던 쪽이 사라진 끝 상태
    REPAIR_RETRY_S = 60.0
    REPAIR_SEEN_MAX = 2000                  # 끝 상태 폴더를 확인한 기록을 이만큼만 둔다

    def _owner_gone(self, rid: str, chained: bool) -> bool:
        """이 조치를 진행하던 쪽(이 프로세스의 작업 스레드, 이관 실행, 재개 Job)이 모두 끝났는지 확인한다."""
        with self._lock:
            if rid in self._active:
                return False
        if not chained:
            return True
        if self._resume_active(rid):
            return False
        return self._migration_ended(rid)

    def repair_final(self, rid: str, paths: RunPaths, st: RemediationState, chained: bool) -> RemediationState:
        """진행하던 쪽이 끝났는데 결과 ZIP 기록이나 보고서가 남은 끝 상태를 웹 서버가 맞추고 그 상태를 반환한다.

        v2.1z-bo, every mode: a final state (`TERMINAL`) with a pending report or a half-done result ZIP swap
        (`graph.final_repair_needed`) whose owner is gone (`_owner_gone`: not run by this process; for a chained one
        the migration run has ended — the detection behind the page's "its build pod is gone" — and no resume Job is
        queued or running) gets `graph.repair_final` under the run claim (`RunClaim`, purpose `service`; a fresh claim
        of another process → nothing now) and this process's lock of the id. The status, reasons and message are
        not changed. Called by `status`, `result` and `file_path`; a folder found in order is remembered by its
        `updated` stamp (no file is read again until the state changes), a repair that left something to do is tried
        again at most every `REPAIR_RETRY_S` seconds."""
        if st.status not in TERMINAL:
            return st
        key = str(paths.root)
        seen = self._repair_seen.get(key)
        now = time.monotonic()
        if seen is not None and seen[0] == st.updated and (seen[2] or now - seen[1] < self.REPAIR_RETRY_S):
            return st
        if not graph_mod.final_repair_needed(paths, st):
            self._remember_repair(key, (st.updated, now, True))
            return st
        if not self._owner_gone(rid, chained):
            return st
        with self._run_lock(rid):
            try:
                claim = RunClaim(paths.root, purpose="service").acquire()
            except StateConflict:
                return st
            try:
                data = read_json(paths.state)
                cur = RemediationState.from_dict(data) if isinstance(data, dict) else st
                if cur.status in TERMINAL and graph_mod.final_repair_needed(paths, cur):
                    log.warning("remediation %s: its run has ended with the %s state not fully recorded; the web "
                                "server completes it", rid, cur.status)
                    self._journal_safe(paths, {"event": "remediation_final_repair", "remediation": cur.id,
                                               "status": cur.status, "by": "service"})
                    cfg = remediation_config(self._config(self._configs_for(rid, cur, chained)))
                    graph_mod.repair_final(cur, paths, cfg, save=lambda s: self._save(paths, s),
                                           journal=lambda e: self._journal_safe(paths, e))
                st = cur
            except Exception as e:  # noqa: BLE001 - 정리에 실패해도 상태 조회는 실패하지 않는다
                log.warning("remediation %s: the final state was not completed: %s", rid,
                            graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root))
            finally:
                claim.release()
        self._remember_repair(key, (st.updated, time.monotonic(), not graph_mod.final_repair_needed(paths, st)))
        return st

    def _remember_repair(self, key: str, value: tuple[float, float, bool]) -> None:
        """끝 상태 폴더를 확인한 기록을 남긴다. 가장 최근의 ``REPAIR_SEEN_MAX``개만 두고 오래된 것부터 버린다."""
        with self._lock:
            self._repair_seen[key] = value
            self._repair_seen.move_to_end(key)
            while len(self._repair_seen) > self.REPAIR_SEEN_MAX:
                self._repair_seen.popitem(last=False)

    # ------------------------------------------------------------------ 준비 확인
    def readiness(self, cfg: Any = None) -> list[str]:
        """조치를 시작할 수 없게 하는 문제 목록을 반환한다. 빈 목록이면 준비된 것이다.
        목록에는 설정 값과 서버 경로를 넣지 않는다.

        Problems that prevent a remediation (IVAS env names, Maven / JDK / settings problems; never values).
        In the Job modes the one line that says remediation runs in the build pod (Maven is not probed here)."""
        if self.job_mode:
            # 웹 파드는 Maven을 실행하지 않으므로 여기서 Maven / JDK를 확인하면 틀린 결과가 나온다
            return [NOT_AVAILABLE_MESSAGE.format(mode=self.mode)]
        if cfg is None:
            try:
                cfg = self._config(self.default_configs())
            except NotReady as e:
                return list(e.details.get("missing") or [str(e)])
            except ValueError as e:
                return [safe_problem(str(e))]
        problems = [safe_problem(p) for p in ivas_mod.readiness(ivas_mod.ivas_settings(cfg))]
        # Maven이 업로드된 프로젝트의 플러그인 코드를 서버에서 실행하므로 운영자가 명시적으로 허용해야 받는다
        if not bool(getattr(cfg.remediation, "trusted_projects", False)):
            problems.insert(0, "remediation.trusted_projects is not confirmed (set SBMIG_REMEDIATION__TRUSTED_PROJECTS=true "
                               "once the operator confirmed that uploaded projects may be built on this server)")
        try:
            maven = self.components_factory(remediation_config(cfg), None).maven
            problems += [safe_problem(p) for p in maven.readiness()]
        except Exception as e:  # noqa: BLE001 - 구성 요소를 만들 수 없는 것도 준비 문제다
            problems.append(f"remediation components cannot be built ({type(e).__name__})")
        return problems

    def readiness_info(self) -> dict[str, Any]:
        """준비 상태 응답(``ready`` / ``missing``, ``mode``, ``runs_in``: ``web`` 또는 ``build_pod``)을 만든다."""
        missing = self.readiness()
        return {"ready": not missing and not self.job_mode, "missing": list(missing), "mode": self.mode,
                "runs_in": "build_pod" if self.job_mode else "web"}

    # ------------------------------------------------------------------ 접수와 대기열
    def submit(self, tmp_zip: Path, name: str, configs: list[str] | None) -> RemediationState:
        """업로드 하나를 접수한다. 순서는 설정 -> 준비 확인 -> ZIP 검사 -> 첫 상태 저장 -> 대기열이다.
        - Job 모드면 다른 일을 하기 전에 ``NotAvailable``을 던진다
        - 준비되지 않았으면 ``NotReady``를, ZIP 검사에서 거부되면 ``IntakeRejected``를 던지고 폴더를 남기지 않는다
        - ``tmp_zip``은 복사해서 쓰므로 원본은 호출한 쪽이 지운다"""
        if self.job_mode:
            raise self._not_available()
        cfgs = self.default_configs() if configs is None else list(configs)
        cfg = self._config(cfgs)
        missing = self.readiness(cfg)
        if missing:
            raise NotReady("remediation is not ready: " + "; ".join(missing), missing=missing)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "project.zip").name) or "project.zip"
        with self._lock:
            rid = self._new_id()
            run_dir = self.root / rid
            run_dir.mkdir(parents=True)
        paths = RunPaths(run_dir)
        try:
            result = intake_mod.accept_zip(Path(tmp_zip), run_dir, intake_mod.limits_from_config(cfg))
            state = graph_mod.new_state(rid, safe, cfg, result, cfgs, policy_hash=planner_mod.policy_hash(cfg))
            state.owner = self.owner
            self._save(paths, state)
        except BaseException:
            self._remove_dir(run_dir)
            raise
        self._journal_safe(paths, {"event": "remediation_submitted", "name": safe, "configs": cfgs,
                                   "zip_sha256": result.zip_sha256, "files": result.files})
        log.info("remediation %s accepted (%s, %d files)", rid, safe, result.files)
        with self._lock:
            self._index.add(rid)
            self._enqueue_locked(rid)
        return state

    def _enqueue_locked(self, rid: str) -> None:
        if self.job_mode:      # Job 모드의 웹 파드는 작업 스레드를 띄우지 않는다
            raise self._not_available()
        self._events[rid] = threading.Event()
        self._active.add(rid)
        self._queue.append(rid)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._dispatch, name="sbmig-remediation-worker", daemon=True)
            self._worker.start()

    def _dispatch(self) -> None:
        # 이 프로세스 안의 Maven끼리 충돌하지 않도록 대기열 앞에서부터 하나씩 실행한다
        while True:
            with self._lock:
                if not self._queue or self._closing.is_set():
                    self._worker = None
                    self._cond.notify_all()
                    return
                rid = self._queue.popleft()
            try:
                self._execute(rid)
            except Exception:
                log.exception("remediation %s: worker failed", rid)
            finally:
                with self._lock:
                    self._active.discard(rid)
                    self._live.pop(rid, None)
                    self._cond.notify_all()

    def _execute(self, rid: str) -> None:
        paths, chained = self._locate(rid)
        if chained:
            self._execute_chained(rid, paths)
            return
        lock = self._run_lock(rid)
        with lock:
            try:
                state = self._load(rid)
            except UnknownRemediation:
                return
            if state.status != QUEUED:
                return
            try:
                # 상태를 진행시키는 동안 작업 폴더를 점유한다. 다른 프로세스가 점유 중이면 아무것도 바꾸지 않는다
                claim = RunClaim(paths.root, purpose="service").acquire()
            except StateConflict as e:
                log.warning("remediation %s: not started: %s", rid, graph_mod.scrub_message(str(e), paths.root))
                return
            state.owner = self.owner
            self._save(paths, state)
            with self._lock:
                self._live[rid] = state      # 중지 요청이 이 객체에 바로 들어가도록 같은 잠금 안에서 넣는다
        ev = self._events.get(rid) or threading.Event()
        handler = self._attach_log(paths)
        try:
            try:
                cfg = remediation_config(self._config(list(state.configs)))
                components = self.components_factory(cfg, paths)
            except Exception as e:  # noqa: BLE001 - QUEUED로 남기지 않고 실패로 보이게 한다
                with lock:
                    state.status, state.reason = FAILED, R_INTERNAL_ERROR
                    state.reasons = [R_INTERNAL_ERROR]
                    state.message = graph_mod.scrub_message(f"cannot start: {type(e).__name__}: {e}", paths.root)
                    state.updated = _now()
                    self._save(paths, state)
                return

            def save(st: RemediationState) -> None:
                with lock:
                    st.owner = self.owner
                    self._save(paths, st)

            def journal(event: dict[str, Any]) -> None:
                with lock:
                    self._journal(paths, event)

            def stop() -> bool:
                return ev.is_set() or self._closing.is_set()

            log.info("remediation %s: start at %s (round %s)", rid, state.next_node, state.round)
            try:
                state = graph_mod.run_remediation(state, paths, cfg, components, save=save, journal=journal, stop=stop)
            except Exception as e:  # noqa: BLE001 - 노드 오류는 run_remediation이 FAILED로 바꾸므로 여기는 버그 대비다
                log.error("remediation %s crashed: %s", rid, graph_mod.scrub_message(repr(e), paths.root))
                with lock:
                    state.status, state.reason = FAILED, R_INTERNAL_ERROR
                    state.reasons = [R_INTERNAL_ERROR]
                    state.message = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root)
                    state.updated = _now()
                    self._save(paths, state)
            # 사람이 중지하지 않고 서버 종료로 멈춘 작업은 STOPPED가 아니라 INTERRUPTED로 기록한다
            if self._closing.is_set() and state.status == STOPPED and not state.stop_requested:
                with lock:
                    state.status = INTERRUPTED
                    state.message = SHUTDOWN_MESSAGE
                    state.updated = _now()
                    self._save(paths, state)
            log.info("remediation %s: %s %s", rid, state.status, state.reason)
        finally:
            claim.release()
            self._detach_log(handler)

    @staticmethod
    def _chain_module() -> Any:
        # chain.py가 없거나 깨져도 이 모듈과 웹 앱은 떠야 하므로 필요할 때 import한다
        try:
            return importlib.import_module("sbmig.remediations.chain")
        except ImportError as e:
            raise NotAvailable(f"chained remediation cannot be resumed here: sbmig.remediations.chain is not available "
                               f"({type(e).__name__})") from None

    def _execute_chained(self, rid: str, paths: RunPaths) -> None:
        """in-process 모드에서 이관 뒤 이어서 실행한 조치를 ``chain.resume``으로 이어서 실행한다.
        사람이 재개를 요청했을 때만 호출된다."""
        lock = self._run_lock(rid)
        with lock:
            try:
                state = self._load(rid)
            except UnknownRemediation:
                return
            if state.status not in RESUMABLE:
                return
        ev = self._events.get(rid) or threading.Event()
        handler = self._attach_log(paths)
        try:
            try:
                chain = self._chain_module()
                # RunManager 대기열을 거치지 않으므로 다른 이관이 빌드 중일 수 있다
                # 두 Maven이 같은 저장소를 쓰지 않도록 단독 업로드처럼 -remediation 저장소를 쓴다
                cfg = remediation_config(self._config(self.chained_configs(rid)))
                components = self.components_factory(cfg, paths)

                def journal(event: dict[str, Any]) -> None:
                    with lock:
                        self._journal(paths, event)

                def stop() -> bool:
                    return ev.is_set() or self._closing.is_set()

                log.info("remediation %s (chained): resume at %s", rid, state.next_node)
                out = chain.resume(paths.root.parent, cfg, journal=journal, stop=stop, components=components)
                log.info("remediation %s (chained): %s %s", rid, getattr(out, "status", "?"), getattr(out, "reason", ""))
            except StateConflict as e:
                # 다른 프로세스가 점유 중이거나 그 사이에 상태가 바뀌었다. 그 프로세스의 상태를 덮어쓰지 않는다
                log.warning("remediation %s (chained): resume refused: %s", rid, graph_mod.scrub_message(str(e), paths.root))
            except Exception as e:  # noqa: BLE001 - 죽더라도 RUNNING으로 남기지 않고 FAILED로 보이게 한다
                log.error("remediation %s (chained) crashed: %s", rid, graph_mod.scrub_message(repr(e), paths.root))
                self._journal_safe(paths, {"event": "resume_failed", "error": type(e).__name__})
                with lock:
                    try:
                        st = self._load(rid)
                    except UnknownRemediation:
                        return
                    if st.status in ACTIVE:          # chain이 상태를 바꾸기 전에 죽었으면 RUNNING으로 남기지 않는다
                        st.status, st.reason = FAILED, R_INTERNAL_ERROR
                        st.reasons = [R_INTERNAL_ERROR]
                        st.message = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root)
                        st.updated = _now()
                        self._save(paths, st)
        finally:
            self._detach_log(handler)

    def _attach_log(self, paths: RunPaths) -> logging.Handler | None:
        try:
            root = logging.getLogger("sbmig")
            tid = threading.get_ident()
            fh = logging.FileHandler(paths.log, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(_ScrubbingFormatter(paths.root))
            fh.addFilter(_ThreadFilter(tid))
            if root.level == logging.NOTSET or root.level > logging.DEBUG:
                for h in root.handlers:     # 파일에는 DEBUG까지, 콘솔에는 원래 수준대로 남긴다
                    if h.level == logging.NOTSET:
                        h.setLevel(logging.INFO if root.level in (logging.NOTSET, logging.INFO) else root.level)
                root.setLevel(logging.DEBUG)
            root.addHandler(fh)
            try:
                from sbmig.cli import _LOG_THREAD
                _LOG_THREAD.set(tid)
            except Exception:  # noqa: BLE001
                pass
            return fh
        except OSError:
            log.debug("remediation.log not attached", exc_info=True)
            return None

    @staticmethod
    def _detach_log(handler: logging.Handler | None) -> None:
        if handler is None:
            return
        logging.getLogger("sbmig").removeHandler(handler)
        handler.close()

    def wait(self, rid: str, timeout: float = 30.0) -> bool:
        """작업이 대기열에서 빠지고 실행이 끝날 때까지 기다린다. ``timeout`` 안에 끝났으면 True를 반환한다.
        테스트와 도구에서 쓴다."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while rid in self._active:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """대기열이 비고 작업 스레드가 끝날 때까지 기다린다."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._active or (self._worker is not None and self._worker.is_alive()):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True

    # ------------------------------------------------------------------ 조회
    def _available_files(self, paths: RunPaths) -> list[str]:
        names = [n for n, f in _FIXED_FILES.items() if f(paths).is_file()]
        rounds = paths.root / "rounds"
        if rounds.is_dir():
            for d in sorted((x for x in rounds.iterdir() if x.is_dir() and x.name.isdigit()), key=lambda x: int(x.name)):
                n = int(d.name)
                for kind, fn in _ROUND_FILES.items():
                    if fn(paths, n).is_file():
                        names.append(f"{kind}-r{n}.{_ROUND_FILE_EXT[kind]}")
        scans = paths.root / "scans"
        if scans.is_dir():
            for f in sorted(scans.glob("*.json")):
                name = f"scan-{f.stem}.json"
                if _SCAN_FILE_RE.match(name):
                    names.append(name)
        return names

    def _queue_position(self, rid: str) -> int | None:
        with self._lock:
            try:
                return list(self._queue).index(rid) + 1
            except ValueError:
                return None

    def _resume_info(self, rid: str, st: RemediationState, chained: bool, shown: str) -> dict[str, Any]:
        # 화면의 재개 버튼을 누를 수 있는지와, 누를 수 없다면 그 이유
        if shown == LEGACY_AWAITING_APPROVAL:
            return {"available": False, "note": LEGACY_APPROVAL_MESSAGE_KO}
        if self.job_mode and not chained:
            return {"available": False, "note": "이 서버에서는 단독 조치를 돌리지 않습니다. 조치는 이관이 성공한 뒤 빌드 파드에서 돕니다."}
        if self.job_mode and not self._can_dispatch():
            return {"available": False, "note": "이 서버는 이어서 진행할 빌드 파드를 띄울 수 없습니다."}
        if chained and self.job_mode and self._resume_active(rid):
            return {"available": False, "note": RESUME_ACTIVE_NOTE}
        if shown not in RESUMABLE or not graph_mod.can_resume(dataclasses.replace(st, status=shown)):
            return {"available": False, "note": f"지금 상태({shown})에서는 이어서 진행할 수 없습니다."}
        with self._lock:
            if rid in self._active:
                return {"available": False, "note": "이미 진행 중이거나 자리를 기다리고 있습니다."}
        return {"available": True, "note": ""}

    def _shown(self, rid: str, st: RemediationState, chained: bool) -> tuple[str, bool]:
        stale = self._stale(rid, st, chained)
        return (INTERRUPTED if stale else st.status), stale

    def status(self, rid: str) -> dict[str, Any]:
        """API에 돌려줄 상태 dict를 만든다. ``kind``와 ``resume`` {available, note}를 넣는다.
        이관 뒤 이어서 실행한 조치면 ``run_id``를 더한다.
        파드가 사라진 조치는 파일을 고치지 않고 ``status = INTERRUPTED``, ``stale: true``로 보여 준다."""
        paths, chained = self._locate(rid)
        st = self.repair_final(rid, paths, self._load(rid), chained)
        shown, stale = self._shown(rid, st, chained)
        base = f"/api/remediations/{rid}"
        withheld = chained and self._withheld(rid)
        files = self._available_files(paths)
        if withheld:                      # 최종 마스킹 전에는 쓸 때 바로 마스킹되는 파일만 목록에 넣는다
            files = [f for f in files if f in LIVE_FILES]
        stop_requested = st.stop_requested
        if chained and st.status in ACTIVE and not stale:
            stop_requested = stop_requested or (paths.root.parent / STOP_REQUEST_FILE).is_file()
        out: dict[str, Any] = {
            "id": rid, "name": st.name, "status": shown, "reason": st.reason, "reasons": list(st.reasons),
            "message": st.message, "node": st.node, "next_node": st.next_node, "round": st.round,
            "counts": dict(sorted(st.counts.items())), "created": st.created, "updated": st.updated,
            "stop_requested": stop_requested,
            "delivery": {"sha256": st.delivery.sha256, "size": st.delivery.size, "round": st.delivery.round,
                         "build_ok": st.delivery.build_ok, "scanned": st.delivery.scanned,
                         "build_tolerated": st.delivery.build_tolerated} if st.delivery else None,
            # 조치 전 빌드 실패를 받아들이는 실행인지 화면이 진행 중에도 알 수 있게 항상 넣는다
            "baseline_tolerated": bool(st.baseline_tolerated),
            # 수정본 빌드 오류를 자동으로 고친 결과다. 화면이 「소스 N개 수정」과 LLM 호출 수에 쓴다. 실행하지 않았으면 None이다
            "fix": report_mod.fix_summary(st),
            "files": files,
            "links": {"self": base, "resume": f"{base}/resume", "stop": f"{base}/stop", "result": f"{base}/result",
                      "files": f"{base}/files/"},
            "kind": KIND_CHAINED if chained else KIND_STANDALONE, "mode": self.mode,
            "resume": self._resume_info(rid, st, chained, shown),
            # 화면의 「결과 ZIP 받기」가 브라우저가 저장할 이름을 그대로 보여 주도록 넣는다
            "zip_name": self._zip_name(rid, paths, chained),
        }
        if chained:
            out["run_id"] = rid
            if withheld:
                out["withheld"] = True
            # 새 빌드 파드로 실행한 마지막 재개 Job 정보
            rjob = self._resume_job(rid)
            if rjob is not None:
                out["resume_job"] = rjob
        if stale:
            out["stale"] = True
            out["recorded_status"] = st.status
            out["message"] = (f"the migration run has ended while this remediation was {st.status} (its build pod is "
                              f"gone); nothing is re-sent automatically")
        if shown == AWAITING_CONFIRMATION:
            out["pending_uploads"] = sorted(st.pending_upload)
        pos = self._queue_position(rid) if shown == QUEUED else None
        if pos is not None:
            out["queue_position"] = pos
        # 화면용 한국어 필드 message_ko, status_ko를 더하고 영어 message는 그대로 둔다
        # 화면은 Markdown이 아니므로 번역하지 못한 부분은 원문 그대로 뒤에 붙인다
        ko, raw = i18n_mod.split_known(out["message"]) if out["message"] else ([], [])
        out["message_ko"] = " ".join([*ko, *raw])
        if shown == LEGACY_AWAITING_APPROVAL:
            # 승인을 기다리던 이전 버전의 작업이다. 저장된 영어 message는 그대로 두고 화면에는 끝난 이유를 보여 준다
            out["message_ko"] = LEGACY_APPROVAL_MESSAGE_KO
        out["status_ko"] = _status_ko(shown)
        return out

    def _chained_ids(self) -> list[str]:
        # 이관 실행 폴더 가운데 remediation/state.json이 있는 것을 찾는다. `_`로 시작하는 폴더는 이관 실행이 아니다
        base = self.out_dir
        if self.run_manager is not None and getattr(self.run_manager, "out_dir", None) is not None:
            base = Path(self.run_manager.out_dir)
        out = []
        try:
            for d in base.iterdir() if base.is_dir() else []:
                if d.name.startswith("_") or not RUN_ID_RE.match(d.name) or ".." in d.name:
                    continue
                if (d / CHAIN_DIR / "state.json").is_file():
                    out.append(d.name)
        except OSError:
            log.debug("chained remediations not listed", exc_info=True)
        return out

    def list(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        """단독 조치와 이관 뒤 이어서 실행한 조치를 합친 목록에서 한 페이지를 최신순으로 반환한다.
        행마다 ``kind``가 들어가고, ``size``는 1에서 100까지다."""
        page = max(1, int(page))
        size = max(1, min(MAX_PAGE_SIZE, int(size)))
        with self._lock:
            ids = list(self._index)
        rows: list[tuple[RemediationState, str, bool]] = []
        for rid in ids:
            try:
                rows.append((self._load(rid), rid, False))
            except (UnknownRemediation, BadRemediationId):
                continue
        for rid in self._chained_ids():
            data = read_json(self.chain_paths(rid).state)
            if isinstance(data, dict):
                rows.append((RemediationState.from_dict(data), rid, True))
        rows.sort(key=lambda r: (r[0].created, r[1]), reverse=True)
        chunk = rows[(page - 1) * size: page * size]
        items = []
        for s, rid, chained in chunk:
            d = report_mod.summary(s)
            shown, stale = self._shown(rid, s, chained)
            d["id"] = rid
            d["status"] = shown
            d["stop_requested"] = s.stop_requested
            d["url"] = f"/api/remediations/{rid}"
            d["kind"] = KIND_CHAINED if chained else KIND_STANDALONE
            if chained:
                d["run_id"] = rid
            if stale:
                d["stale"] = True
            items.append(d)
        return {"items": items, "page": page, "size": size, "total": len(rows)}

    def result(self, rid: str) -> dict[str, Any]:
        """작업이 끝났으면 report.json 내용을 반환하고, 아니면 ``NotFinished``를 던진다.
        최종 마스킹 전인 조치는 ``Withheld``를 던진다. report.json이 없으면 다시 만들어 반환한다."""
        paths, chained = self._locate(rid)
        st = self.repair_final(rid, paths, self._load(rid), chained)
        if st.status not in TERMINAL:
            raise NotFinished(f"remediation {rid} is {st.status}", status=st.status)
        if chained and self._withheld(rid):
            raise self._withheld_error(rid, "report.json")
        data = read_json(paths.report_json)
        if isinstance(data, dict):
            return data
        # report.json이 없을 때만 그래프와 같은 ignore_actions로 다시 만든다
        inhouse = None
        try:
            cfg = self._config(self._configs_for(rid, st, chained))
            ignore = list(cfg.remediation.ignore_actions)
            inhouse = inhouse_keys(cfg)
        except Exception:  # noqa: BLE001. 설정을 읽지 못해도 보고서는 돌려준다
            ignore = None
        return report_mod.build_report_json(st, paths, ignore_actions=ignore, inhouse=inhouse)

    def file_path(self, rid: str, name: str) -> Path | None:
        """허용된 파일 이름에 맞는 작업 폴더 안의 실제 파일 경로를 반환한다.
        모르는 이름, 없는 파일, 링크를 따라 폴더 밖으로 나가는 경로면 None을 반환한다.

        A chained run's files other than the journal raise `Withheld` while the migration run's final
        redaction pass is pending."""
        paths, chained = self._locate(rid)
        if not paths.state.is_file():
            raise UnknownRemediation(f"unknown remediation {rid}")
        if name in ("report.md", "report.json", "result.zip", "fix.patch"):
            try:
                self.repair_final(rid, paths, self._load(rid), chained)
            except UnknownRemediation:
                raise
            except Exception:  # noqa: BLE001 - 정리에 실패해도 파일 제공은 실패하지 않는다
                log.debug("remediation %s: final repair before serving %s failed", rid, name, exc_info=True)
        target: Path | None = None
        if name in _FIXED_FILES:
            target = _FIXED_FILES[name](paths)
        elif (m := _ROUND_FILE_RE.match(name or "")) and _ROUND_FILE_EXT[m.group(1)] == m.group(3):
            target = _ROUND_FILES[m.group(1)](paths, int(m.group(2)))
        elif m := _SCAN_FILE_RE.match(name or ""):
            target = paths.scan(m.group(1))
        if target is None:
            return None
        if chained and name not in LIVE_FILES and self._withheld(rid):
            raise self._withheld_error(rid, name)
        try:
            if target.is_symlink() or not target.is_file():
                return None
            real = target.resolve()
            real.relative_to(paths.root.resolve())
        except (OSError, ValueError):
            return None
        return real

    @staticmethod
    def _withheld_error(rid: str, name: str) -> Withheld:
        return Withheld(f"{name} of remediation {rid} is available once the migration run has finished (its outputs are "
                        f"redacted at the end of the run)", status="withheld")

    # ------------------------------------------------------------------ 재개, 중지, 삭제
    def resume(self, rid: str, confirm: str | None = None, label: str | None = None, project_id: str | None = None,
               analysis_id: str | None = None, by: str = "") -> dict[str, Any]:
        """사람이 요청한 재개를 처리한다. 재개할 수 있는 상태이고 실행 중인 스레드가 없어야 한다.
        - 확인 대기에서 ``reupload``는 다시 올리고, ``receipt``는 사람이 IVAS에서 찾은 분석을 이어서 조회한다
        - 확인 대기인데 ``confirm``이 없거나 이전 버전이 남긴 승인 대기 작업이면 ``StateConflict``를 던진다
        - Job 모드에서 단독 조치는 ``NotAvailable``을 던지고, 이어서 실행한 조치는 새 빌드 파드로 재개한다

        Explicit resume. `confirm` is required in AWAITING_CONFIRMATION: "reupload" or "receipt" (+ project / analysis id).
        A state saved in `AWAITING_APPROVAL` by an earlier version is refused (`graph.approval_removed`), in every mode.
        In-process, a chained state is resumed through `chain.resume` in a worker thread. Job modes: a standalone one is
        `NotAvailable`; a chained one gets the confirmation written to its `state.json` here, then
        `RunManager.resume_remediation` queues a new build pod (the state file is restored when that is refused)."""
        paths, chained = self._locate(rid)
        lock = self._run_lock(rid)
        dispatch = self.job_mode
        with lock:
            st = self._load(rid)
            if (removed := graph_mod.approval_removed(st)) is not None:
                raise removed
            if dispatch:
                if not chained:
                    raise NotAvailable(NOT_AVAILABLE_MESSAGE.format(mode=self.mode), mode=self.mode, status=st.status)
                if not self._can_dispatch():
                    raise NotAvailable(RESUME_UNSUPPORTED.format(mode=self.mode), mode=self.mode, status=st.status)
                if self._resume_active(rid):
                    raise StateConflict(RESUME_ACTIVE_NOTE, status=st.status)
            with self._lock:
                alive = rid in self._active
            if chained and not alive:
                if self._stale(rid, st, chained):
                    st.status = INTERRUPTED           # 파드가 사라진 조치는 사람이 재개할 때 파일에도 기록한다
                elif st.status in ACTIVE or self._migration_state(rid) in ("queued", "running"):
                    raise StateConflict(f"the migration run {rid} is still running", status=st.status)
            if alive or st.status not in RESUMABLE or not graph_mod.can_resume(st):
                raise StateConflict(f"remediation {rid} is {st.status} and cannot be resumed", status=st.status)
            event: dict[str, Any] = {"event": "resume", "from": st.status, "by": by}
            if st.status == AWAITING_CONFIRMATION:
                if confirm not in (CONFIRM_REUPLOAD, CONFIRM_RECEIPT):
                    raise StateConflict("the last IVAS upload has an unknown result: resume with confirm=reupload (it did "
                                        "not land) or confirm=receipt (+ label, project_id, analysis_id)", status=st.status)
                labels = sorted(st.pending_upload)
                if label is None:
                    if len(labels) != 1:
                        raise ValueError(f"label required (pending uploads: {', '.join(labels) or '-'})")
                    label = labels[0]
                if label not in st.pending_upload:
                    raise ValueError(f"no pending upload for label {label!r}")
                if confirm == CONFIRM_REUPLOAD:
                    st.pending_upload.pop(label, None)
                else:
                    if not (project_id and str(project_id).strip()) or not (analysis_id and str(analysis_id).strip()):
                        raise ValueError("confirm=receipt needs project_id and analysis_id")
                    sha = st.pending_upload.pop(label)
                    st.pending_receipt[label] = UploadReceipt(project_id=str(project_id).strip(),
                                                              analysis_id=str(analysis_id).strip(),
                                                              zip_sha256=sha).to_dict()
                event.update({"confirm": confirm, "label": label})
            elif confirm is not None:
                raise StateConflict(f"confirm applies to {AWAITING_CONFIRMATION} only (status {st.status})",
                                    status=st.status)
            if chained and dispatch:
                return self._dispatch_chained(rid, paths, st, event)
            if chained:
                self._chain_module()                  # 상태 파일을 고치기 전에 chain 모듈이 없으면 NotAvailable을 던진다
                st.stop_requested = False
                st.message = ""
                st.updated = _now()
                self._save(paths, st)
                self._journal_safe(paths, {**event, "kind": KIND_CHAINED})
                with self._lock:
                    if self._closing.is_set():
                        raise StateConflict("the server is shutting down")
                    self._enqueue_locked(rid)
                return self.status(rid)
            st.stop_requested = False
            st.status = QUEUED
            st.message = ""
            st.owner = self.owner
            st.updated = _now()
            self._save(paths, st)
            self._journal_safe(paths, event)
            with self._lock:
                if self._closing.is_set():
                    raise StateConflict("the server is shutting down")
                self._enqueue_locked(rid)
        return self.status(rid)

    def _dispatch_chained(self, rid: str, paths: RunPaths, st: RemediationState, event: dict[str, Any]) -> dict[str, Any]:
        """Job 모드에서 이관 뒤 이어서 실행한 조치를 재개한다. 호출한 쪽이 잠금을 잡고 있어야 한다.
        확인 응답을 반영한 ``st``를 저장하고 저널에 남긴 뒤 ``RunManager.resume_remediation``을 호출한다.
        거절되면 state.json을 되돌리고 ``resume_refused``를 남긴 뒤 예외를 다시 던진다."""
        before = read_json(paths.state)
        st.stop_requested = False
        st.message = ""
        st.updated = _now()
        self._save(paths, st)
        self._journal_safe(paths, {**event, "kind": KIND_CHAINED, "runs_in": "build_pod"})
        try:
            job = self.run_manager.resume_remediation(rid)
        except Exception as e:
            # 다음 재개가 같은 확인 응답으로 시작할 수 있게 응답을 반영하기 전 상태로 되돌린다
            if isinstance(before, dict):
                try:
                    write_json_atomic(paths.state, before)
                except OSError:
                    log.warning("remediation %s: state.json not restored after a refused resume", rid, exc_info=True)
            self._journal_safe(paths, {"event": "resume_refused", "kind": KIND_CHAINED, "error": type(e).__name__,
                                       "code": getattr(e, "code", None)})
            raise
        log.info("remediation %s (chained): resume queued for a new build pod (attempt %s, job %s)", rid,
                 (job or {}).get("attempt"), (job or {}).get("job"))
        return self.status(rid)

    def stop(self, rid: str) -> dict[str, Any]:
        """QUEUED / RUNNING 작업에 중지를 요청한다. 다음 노드로 넘어갈 때 적용되고, 진행 중인 IVAS 분석은 취소하지 않는다.
        이관 뒤 이어서 실행한 조치는 재개 중인 스레드에 알리거나 이관 실행 폴더에 ``stop.request``를 쓴다.
        그 state는 빌드 파드가 관리하므로 고치지 않는다."""
        paths, chained = self._locate(rid)
        if chained:
            return self._stop_chained(rid, paths)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            if st.status not in ACTIVE:
                raise StateConflict(f"remediation {rid} is {st.status}, not queued or running", status=st.status)
            with self._lock:
                live = self._live.get(rid)
                ev = self._events.get(rid)
            if live is not None:
                live.stop_requested = True       # 작업 스레드가 다음에 저장할 때 덮어쓰지 않도록 같은 객체에 넣는다
                self._save(paths, live)
            else:
                st.stop_requested = True
                st.updated = _now()
                self._save(paths, st)
            if ev is not None:
                ev.set()
            self._journal_safe(paths, {"event": "stop_requested"})
        return {"stop_requested": True, "note": STOP_NOTE}

    def _stop_chained(self, rid: str, paths: RunPaths) -> dict[str, Any]:
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            # 재개 Job이 대기 중이면 대기열에서 빼고, 실행 중이면 stop.request만 쓴다. Job은 지우지 않는다
            stop_resume = getattr(self.run_manager, "stop_remediation", None) if self.run_manager is not None else None
            if callable(stop_resume) and self._resume_active(rid):
                how = stop_resume(rid)
                if how is not None:
                    self._journal_safe(paths, {"event": "stop_requested", "kind": KIND_CHAINED, "resume": how})
                    return {"stop_requested": True, "note": STOP_NOTE, "resume": how}
            with self._lock:
                alive = rid in self._active
                ev = self._events.get(rid)
            if alive and ev is not None:              # 이 프로세스의 작업 스레드로 재개한 경우
                ev.set()
                self._journal_safe(paths, {"event": "stop_requested", "kind": KIND_CHAINED})
                return {"stop_requested": True, "note": STOP_NOTE}
            if st.status not in ACTIVE or self._stale(rid, st, True):
                shown = self._shown(rid, st, True)[0]
                raise StateConflict(f"remediation {rid} is {shown}, not queued or running", status=shown)
            run_dir = paths.root.parent
            try:
                (run_dir / STOP_REQUEST_FILE).write_text("stop requested from the web UI (remediation)\n", encoding="utf-8")
            except OSError as e:
                raise StateConflict(f"the stop request could not be written ({type(e).__name__})", status=st.status) from None
            rm = self.run_manager
            run = getattr(rm, "runs", {}).get(rid) if rm is not None else None
            # 이 프로세스의 in-process 이관 실행이면 엔진도 멈춘다. Job 실행에는 RunManager.stop을 호출하지 않는다
            # RunManager.stop은 유예 시간 뒤 Job을 지워 진행 중인 IVAS 업로드와 Maven을 끊기 때문이다
            if run is not None and not getattr(run, "job", False):
                try:
                    rm.stop(rid)
                except Exception:  # noqa: BLE001 - 요청은 위 파일이 하므로 이 호출은 실패해도 된다
                    log.debug("RunManager.stop(%s) failed", rid, exc_info=True)
            log.info("remediation %s (chained): %s written", rid, STOP_REQUEST_FILE)
        return {"stop_requested": True, "note": STOP_NOTE}

    def delete(self, rid: str) -> None:
        """원본 ZIP을 포함해 작업 폴더 전체를 지운다. QUEUED / RUNNING이면 ``StateConflict``를 던진다.
        이관 뒤 이어서 실행한 조치는 폴더를 이관 실행이 관리하므로 항상 거절한다."""
        paths, chained = self._locate(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            if chained:
                raise StateConflict(f"remediation {rid} belongs to the migration run {rid}, which owns its directory; it "
                                    f"cannot be deleted on its own", status=st.status)
            with self._lock:
                alive = rid in self._active
            if alive or st.status in ACTIVE:
                raise StateConflict(f"remediation {rid} is {st.status}", status=st.status)
            self._remove_dir(paths.root)
            with self._lock:
                self._index.discard(rid)
                self._events.pop(rid, None)
        log.info("remediation %s deleted", rid)

    @staticmethod
    def _remove_dir(path: Path) -> None:
        def onerror(func: Any, p: str, _exc: Any) -> None:
            # 원본 ZIP은 읽기 전용이고 Windows는 읽기 전용 파일을 지우지 못하므로 쓰기 권한을 준 뒤 다시 지운다
            try:
                os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
                parent = os.path.dirname(p)
                os.chmod(parent, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
                func(p)
            except OSError:
                pass
        if not path.exists():
            return
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    os.chmod(f, stat.S_IWUSR | stat.S_IRUSR)
            except OSError:
                pass
        shutil.rmtree(path, onerror=onerror)

    # ------------------------------------------------------------------ 종료
    def shutdown(self) -> dict[str, Any]:
        """서버를 종료하면서 모든 작업에 중지를 알리고 잠깐 기다린다.
        그 사이 멈춘 작업은 INTERRUPTED로 기록하고, 아직 실행 중인 작업은 다음 시작 때 INTERRUPTED로 바뀐다."""
        # 작업별 이벤트는 건드리지 않는다. _closing으로 사람의 중지(STOPPED)와 서버 종료(INTERRUPTED)를 구분한다
        self._closing.set()
        with self._lock:
            active = sorted(self._active)
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(SHUTDOWN_JOIN_S)
        return {"active": active, "worker_alive": bool(worker is not None and worker.is_alive())}


__all__ = [
    "CHAIN_DIR",
    "ID_RE",
    "RUN_ID_RE",
    "BadRemediationId",
    "NotAvailable",
    "NotFinished",
    "RemediationManager",
    "UnknownRemediation",
    "Withheld",
    "default_components_factory",
    "remediation_config",
    "safe_problem",
]
