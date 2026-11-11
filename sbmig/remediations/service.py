"""취약점 조치 작업 관리자 — 웹 파드 프로세스 안에서 작업 스레드로 조치 그래프를 돌리고, state.json 으로 상태를 지킨다.
흐름상 위치: ``api.py`` 라우트 → :class:`RemediationManager` → ``graph.run_remediation``. 접수(``intake.accept_zip``) →
QUEUED → 작업 스레드(한 번에 하나: 이 프로세스 안의 Maven 이 서로 부딪히지 않게, 나머지는 접수 순서대로 기다린다) →
대기 상태(승인·업로드 확인·중지)나 끝 상태. 승인(``decide``)은 제안 해시·원본 트리 해시·정책 해시·diff 를 **모두** 맞춰 본 뒤에만
plan.json 과 approvals.jsonl 에 쓴다(하나라도 틀리면 아무것도 쓰지 않는다). 재개(``resume``)는 사람이 부를 때만 — 서버가 새로
뜨면 QUEUED / RUNNING 으로 남은 작업은 INTERRUPTED 가 되고 **아무것도 자동으로 다시 보내지 않는다**.
Maven 로컬 저장소: 설정의 ``build.local_repo`` 가 있으면 조치 실행은 ``<그 경로>-remediation`` 을 쓴다 — 이관 빌드 파드들이
쓰는 저장소를 여러 Maven 프로세스가 함께 쓰지 않게(:func:`remediation_config`). 비어 있으면 Maven 기본값 그대로다.
데이터베이스 없음: 목록·상태는 ``<out>/_remediations/<id>/state.json`` 파일에서 읽는다.

웹 모드(2026-11-11, 사용자 결정 "조치는 웹 파드에서 돌지 않는다"): ``in-process`` (개발 PC)만 위의 작업 스레드를 쓴다.
Job 모드(``k8s-job`` / ``local-job``)에서는 이 프로세스가 **아무것도 실행하지 않는다** — 단독 업로드(``submit``)·재개는
``NotAvailable`` (HTTP 409 ``not_available``), 준비 확인은 Maven 을 재 보지 않고 "빌드 파드에서 이관 성공 뒤에 돈다"고만 답한다.
이어 돈 조치(chained): 빌드 파드가 이관 성공 뒤 ``<out>/<이관 실행 id>/remediation/`` 에 남긴 조치(상태 id = 이관 실행 id)도
같은 API 로 보인다 — 상태·목록·제안·결과·파일·승인은 모든 모드에서, 재개는 in-process 에서만(``chain.resume``), 삭제는 거절(폴더의
주인은 이관 실행). 시작 때의 재시작 규칙은 단독 조치 폴더만 본다(이어 돈 조치는 빌드 파드의 것). 이관 실행이 이미 끝났는데
QUEUED / RUNNING 으로 남은 이어 돈 조치는 파일을 고치지 않고 INTERRUPTED (``stale: true``)로 **보여 준다**.

The remediation job manager: worker threads of this process (one remediation at a time), `state.json` per run,
approvals (all-or-nothing hash checks), resume / stop / delete, and the restart rule (QUEUED / RUNNING of an earlier
process → INTERRUPTED, never resumed automatically). File-based only. In the Job modes nothing runs here (submit /
resume → `NotAvailable`); chained remediations (`<out>/<run id>/remediation/`, written by the build pod after a
successful migration) are visible through the same API in every mode.
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
from . import intake as intake_mod
from . import ivas as ivas_mod
from . import planner as planner_mod
from . import report as report_mod
from .interfaces import Components
from .models import (
    ACTIVE,
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    FAILED,
    INTERRUPTED,
    QUEUED,
    R_INTERNAL_ERROR,
    REMEDIATIONS_DIR,
    RESUMABLE,
    STOPPED,
    TERMINAL,
    ApprovalMismatch,
    Decision,
    NotReady,
    RemediationError,
    RemediationState,
    RoundPlan,
    RunPaths,
    StateConflict,
    UploadReceipt,
    read_json,
    sha256_text,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.service")

# 작업 ID: r<날짜-시각>-<무작위 16진수> — 이름순 = 시간순이고, 무작위 부분 때문에 남의 작업 주소를 짐작하지 못한다
ID_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{6,32}$")
# 이어 돈 조치의 ID = 이관 실행 id — sbmig.web.runs.RUN_ID_RE 와 같은 규칙(경로 문자 없음, 첫 글자는 영숫자)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
from .chain import CHAIN_DIR  # noqa: E402 — 이관 실행 폴더 안의 조치 폴더 이름(한 곳에서만 정한다)
STOP_REQUEST_FILE = "stop.request"            # sbmig.core.workspace.STOP_REQUEST_FILE — 다른 파드의 실행을 멈추는 파일
JOB_MODES = ("k8s-job", "local-job")
IN_PROCESS = "in-process"
KIND_CHAINED, KIND_STANDALONE = "chained", "standalone"
LIVE_FILES = ("journal.jsonl",)               # 마지막 정제 전에도 내주는 이어 돈 조치의 파일(쓰는 순간 정제된다)
# 이관 실행이 끝났다고 보는 RunManager.status 의 state 값 — 그런데도 QUEUED / RUNNING 인 이어 돈 조치는 파드가 사라진 것이다
_RUN_ENDED = ("finished", "unknown", "missing")
NOT_AVAILABLE_MESSAGE = ("remediation runs in the build pod after a successful migration (remediation.after_migration); "
                         "the web pod runs nothing in {mode} mode")
RESUME_NEXT_STEP = ("resuming a chained remediation in a new build pod is the next increment; in {mode} mode it cannot "
                    "be resumed from the web pod yet")
CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
FALLBACK_CONFIGS = ("artifacts.yml", "artifacts.local-e2e.yml")       # RunManager.FALLBACK_CONFIGS 와 같은 규칙
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
# 내려받을 수 있는 고정 이름(나머지는 아래 정규식의 라운드·검사 파일뿐) — 경로 조작을 막는 화이트리스트
_FIXED_FILES: dict[str, Callable[[RunPaths], Path]] = {
    "report.md": lambda p: p.report_md,
    "report.json": lambda p: p.report_json,
    "result.zip": lambda p: p.delivery_zip,
    "source.zip": lambda p: p.original_zip,
    "manifest.json": lambda p: p.manifest,
    "journal.jsonl": lambda p: p.journal,
    "remediation.log": lambda p: p.log,
    "delivery.json": lambda p: p.delivery_json,
    # 정책 include 로 실패한 수정본이 result.zip 을 차지했을 때 앞 라운드의 검증된 ZIP(delivery/result-r<k>-verified.zip)
    "result-verified.zip": lambda p: graph_mod.find_verified_zip(p) or p.delivery_zip.with_name("result-verified.zip"),
    "baseline-build.log": lambda p: p.baseline_log,
}
_ROUND_FILES: dict[str, Callable[[RunPaths, int], Path]] = {
    "plan": lambda p, n: p.plan(n),
    "candidate": lambda p, n: p.patch(n),
    "build": lambda p, n: p.build_log(n),
}
_ROUND_FILE_RE = re.compile(r"^(plan|candidate|build)-r([0-9]{1,4})\.(json|patch|log)$")
_ROUND_FILE_EXT = {"plan": "json", "candidate": "patch", "build": "log"}
_SCAN_FILE_RE = re.compile(r"^scan-(baseline|final-r[0-9]{1,4})\.json$")
_ABS_PATH = re.compile(r"(?<![\w:/\\.])(?:[A-Za-z]:[\\/]|/)[^\s()'\"]+")


class UnknownRemediation(RemediationError):
    """그런 ID 의 작업이 없다(HTTP 404)."""
    code = "not_found"


class BadRemediationId(RemediationError):
    """ID 모양이 규칙(``ID_RE``)에 맞지 않는다(HTTP 400) — 파일 시스템에 닿기 전에 거른다."""
    code = "bad_id"


class NotFinished(StateConflict):
    """아직 끝 상태가 아니라 결과(report.json)를 줄 수 없다(HTTP 409 ``not_finished``)."""
    code = "not_finished"


class NotAvailable(RemediationError):
    """이 웹 모드에서는 할 수 없는 일(HTTP 409 ``not_available``) — Job 모드의 단독 업로드·재개, 아직 없는 chain 모듈.

    Not available in this web mode (Job modes: standalone upload / resume; chain module missing)."""
    code = "not_available"


class Withheld(StateConflict):
    """이관 실행의 마지막 정제(끝날 때 비밀 값을 지우는 단계)가 아직 남아 이어 돈 조치의 파일을 내줄 수 없다(HTTP 409 ``withheld``).

    The migration run's final redaction pass is still pending: chained files other than the journal wait."""
    code = "withheld"


ComponentsFactory = Callable[[Any, "RunPaths | None"], Components]
ConfigLoader = Callable[[list[str]], Any]


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_problem(text: str) -> str:
    """준비 문제 한 줄에서 서버의 절대 경로를 마지막 이름만 남기고 지운다(응답에 서버 경로·값을 내지 않는다).

    Strip absolute server paths (keep the last component) and secrets from a readiness problem line."""
    out = _ABS_PATH.sub(lambda m: Path(m.group(0).rstrip("\\/")).name or "<path>", str(text))
    return graph_mod.scrub_message(out, limit=500)


def remediation_config(cfg: Any) -> Any:
    """조치 실행에 쓸 설정 사본: ``build.local_repo`` 가 있으면 ``<그 경로>-remediation`` 으로 바꾼다 — 이관 빌드 파드와
    Maven 로컬 저장소를 함께 쓰지 않게(한 저장소를 여러 Maven 프로세스가 쓰는 것을 피한다). 없으면 그대로.

    A copy of `cfg` whose `build.local_repo` gets the `-remediation` suffix (never shared with the migration build pods)."""
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
    """운영 부품 묶음(``graph.default_components``). ``paths`` 는 쓰지 않는다(준비 확인 때는 None).

    The real components; `paths` is unused (None for readiness checks)."""
    return graph_mod.default_components(cfg)


class _ScrubbingFormatter(logging.Formatter):
    """로그 줄을 파일에 쓰기 직전 비밀 값·URL 자격증명·실행 폴더의 절대 경로를 지운다."""

    def __init__(self, root: Path):
        super().__init__(LOG_FORMAT)
        self.root = root

    def format(self, record: logging.LogRecord) -> str:
        return graph_mod.scrub_message(super().format(record), self.root, limit=200_000)


class _ThreadFilter(logging.Filter):
    """한 작업 스레드(또는 그 컨텍스트를 물려받은 스레드)의 로그만 통과 — 다른 실행의 로그가 섞이지 않게."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread == self.thread_id:
            return True
        try:
            from sbmig.cli import _LOG_THREAD
            return _LOG_THREAD.get() == self.thread_id
        except Exception:  # noqa: BLE001 — the CLI context variable is a convenience
            return False


class RemediationManager:
    """조치 작업 관리자(웹 앱 하나에 하나). 폴더 ``<out>/_remediations/<id>/``, 작업 스레드 하나씩 차례로, 상태 파일 기반.
    ``components_factory(cfg, paths)`` / ``config_loader(configs)`` 는 테스트가 가짜를 넣는 자리다.

    ``mode`` = 웹 모드(``RunManager.effective_mode``): Job 모드면 작업 스레드를 한 번도 띄우지 않는다. ``run_manager`` (선택)는
    이어 돈 조치의 이관 실행 상태·마지막 정제·중지에 쓴다.

    One per web app. `default_configs` is a list of config names or a callable returning them (the web app passes
    `RunManager.default_configs` so both features resolve the server default the same way). `mode` is the web mode
    (Job modes: no worker thread, ever); `run_manager` (optional) answers for the migration runs of chained remediations."""

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
        self.owner = secrets.token_hex(16)            # 이 프로세스의 토큰 — 재시작 뒤 남은 작업을 알아본다
        self._lock = threading.RLock()                # index / queue / locks / events
        self._cond = threading.Condition(self._lock)
        self._run_locks: dict[str, threading.RLock] = {}
        self._events: dict[str, threading.Event] = {}
        self._live: dict[str, RemediationState] = {}  # 작업 스레드가 지금 쥐고 있는 상태 객체(중지 요청을 바로 넣는다)
        self._queue: collections.deque[str] = collections.deque()
        self._active: set[str] = set()                # 줄을 섰거나 도는 작업
        self._index: set[str] = set()
        self._worker: threading.Thread | None = None
        self._closing = threading.Event()
        self.interrupted: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()

    @property
    def job_mode(self) -> bool:
        """Job 모드(``k8s-job`` / ``local-job``)인가 — 이 프로세스는 조치를 실행하지 않는다. / True in the Job modes."""
        return self.mode in JOB_MODES

    def _not_available(self) -> NotAvailable:
        return NotAvailable(NOT_AVAILABLE_MESSAGE.format(mode=self.mode), mode=self.mode)

    # ------------------------------------------------------------------ config
    def default_configs(self) -> list[str]:
        """이번 서버의 기본 설정 이름들(``RunManager.default_configs`` 와 같은 규칙: 준 이름 중 있는 파일, 없으면
        artifacts.yml / artifacts.local-e2e.yml 중 있는 첫 것).

        The server default config names (same rule as `RunManager.default_configs`)."""
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
        """설정 이름 검사(``../`` 차단) 뒤 읽는다. 모르는 이름 = ValueError(400), 읽기 실패 = NotReady(503)."""
        for c in configs:
            if not CONFIG_NAME_RE.match(c) or not (self.repo_root / "config" / c).is_file():
                raise ValueError(f"unknown config {c}")
        try:
            return self.config_loader(list(configs))
        except Exception as e:  # noqa: BLE001 — pydantic ValidationError, yml errors: the server config is not usable
            problem = f"configuration cannot be loaded ({type(e).__name__})"
            raise NotReady(problem, missing=[problem]) from None

    def max_upload_bytes(self) -> int:
        """업로드 상한(바이트) = 기본 설정의 ``remediation.max_upload_mb``. 설정을 읽지 못하면 코드 기본값(1024 MiB).

        The upload limit from the default config's `remediation.max_upload_mb` (code default when unreadable)."""
        from sbmig.core.config import RemediationConfig
        try:
            mb = int(self._config(self.default_configs()).remediation.max_upload_mb)
        except Exception:  # noqa: BLE001 — a missing artifacts yml must not keep the web app from starting
            mb = int(RemediationConfig().max_upload_mb)
        return max(0, mb) * 1024 * 1024

    # ------------------------------------------------------------------ paths / ids
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
        """이어 돈 조치의 경로 규칙 ``<out>/<이관 실행 id>/remediation/`` (ID 검사 뒤). / Chained run paths."""
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
            raise BadRemediationId("bad remediation id")
        return RunPaths(self._migration_run_dir(run_id) / CHAIN_DIR)

    def _locate(self, rid: str) -> tuple[RunPaths, bool]:
        # 단독 조치 ID(r<날짜>-…)가 먼저, 그 밖의 이관 실행 id 모양은 이어 돈 조치 — 둘 다 아니면 파일 시스템에 닿기 전에 거른다
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
        """이 ID 가 이어 돈 조치(이관 실행 폴더 안)인가. / Whether `rid` names a chained remediation."""
        return self._locate(rid)[1]

    def paths(self, rid: str) -> RunPaths:
        """작업 폴더의 경로 규칙(ID 검사 뒤; 이어 돈 조치면 이관 실행 폴더 안). / The run paths of a validated id."""
        return self._locate(rid)[0]

    def _new_id(self) -> str:
        while True:
            rid = f"r{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(6)}"
            if not (self.root / rid).exists():
                return rid

    def incoming_path(self) -> Path:
        """받는 중인 업로드 본문의 임시 파일 ``<out>/_remediations/.incoming/<hex>.part`` (작업 폴더와 섞이지 않는 자리).

        A fresh temp file for an upload body that is still arriving."""
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

    # ------------------------------------------------------------------ chained: the migration run's facts
    def _migration_state(self, run_id: str) -> str | None:
        """이관 실행의 state(``RunManager.status``) — 관리자가 없으면 빌드 파드·작업 스레드가 끝에 쓰는 파일로 짐작한다."""
        rm = self.run_manager
        if rm is not None and hasattr(rm, "status"):
            try:
                return str((rm.status(run_id) or {}).get("state") or "") or None
            except Exception:  # noqa: BLE001 — an unreadable migration status is "not known", never a crash
                log.debug("migration status of %s not readable", run_id, exc_info=True)
                return None
        rd = self._migration_run_dir(run_id)
        if (rd / "job-status.json").is_file() or (rd / "web-status.json").is_file():
            return "finished"
        return None

    def _migration_ended(self, run_id: str) -> bool:
        return self._migration_state(run_id) in _RUN_ENDED

    def _withheld(self, run_id: str) -> bool:
        """이관 실행의 마지막 정제가 아직 남았는가(``RunManager.file_withheld`` — 쓰는 순간 정제되지 않는 파일 기준)."""
        rm = self.run_manager
        if rm is None or not hasattr(rm, "file_withheld"):
            return False
        try:
            return bool(rm.file_withheld(run_id, "report.md"))
        except Exception:  # noqa: BLE001 — when in doubt, withhold (the pass may still rewrite the files)
            log.debug("file_withheld of %s failed", run_id, exc_info=True)
            return True

    def _stale(self, rid: str, st: RemediationState, chained: bool) -> bool:
        # 이어 돈 조치가 QUEUED / RUNNING 이라는데 이관 실행은 끝났다 = 그 파드는 사라졌다(이 프로세스가 재개해 돌리는 중이면 아니다)
        if not chained or st.status not in ACTIVE:
            return False
        with self._lock:
            if rid in self._active:
                return False
        return self._migration_ended(rid)

    def _recorded_configs(self, run_id: str) -> list[str]:
        # 이관 실행이 접수 때 적은 설정 이름: control/request.json(Job 모드) → journal 의 web_run 줄(in-process)
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
        """이어 돈 조치의 정책 해시에 쓸 설정 이름들 = 이관 실행이 쓴 것(``control/request.json`` → journal ``web_run``).
        기록이 없거나 그 이름이 이 서버의 config/ 에 없으면 서버 기본값.

        The config names the migration run used (request.json, else the journal's `web_run`), else the server default."""
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
    # ------------------------------------------------------------------ restart rule
    def _recover(self) -> None:
        """시작 때 모든 state.json 을 훑는다: 다른 프로세스가 남긴 QUEUED / RUNNING → INTERRUPTED(자동 재개 없음)."""
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
        if self.interrupted:
            log.warning("remediations interrupted by a server restart (resume explicitly): %s", ", ".join(self.interrupted))

    # ------------------------------------------------------------------ readiness
    def readiness(self, cfg: Any = None) -> list[str]:
        """조치를 시작할 수 없게 하는 문제들(빈 목록 = 준비됨): IVAS 환경 변수 이름, Maven / JDK / settings.xml 문제.
        값·서버 경로는 넣지 않는다.

        Problems that prevent a remediation (IVAS env names, Maven / JDK / settings problems; never values).
        In the Job modes the one line that says remediation runs in the build pod (Maven is not probed here)."""
        if self.job_mode:
            # 웹 파드는 Maven 을 돌리지 않는다 — 여기서 Maven / JDK 를 재 보면 틀린 답(빌드 파드의 사정이 아니다)이 된다
            return [NOT_AVAILABLE_MESSAGE.format(mode=self.mode)]
        if cfg is None:
            try:
                cfg = self._config(self.default_configs())
            except NotReady as e:
                return list(e.details.get("missing") or [str(e)])
            except ValueError as e:
                return [safe_problem(str(e))]
        problems = [safe_problem(p) for p in ivas_mod.readiness(ivas_mod.ivas_settings(cfg))]
        # 신뢰 프로젝트 확인: Maven 이 올라온 프로젝트의 플러그인 코드를 이 서버에서 실행하므로, 운영자의 명시적 확인 없이는 받지 않는다
        if not bool(getattr(cfg.remediation, "trusted_projects", False)):
            problems.insert(0, "remediation.trusted_projects is not confirmed (set SBMIG_REMEDIATION__TRUSTED_PROJECTS=true "
                               "once the operator confirmed that uploaded projects may be built on this server)")
        try:
            maven = self.components_factory(remediation_config(cfg), None).maven
            problems += [safe_problem(p) for p in maven.readiness()]
        except Exception as e:  # noqa: BLE001 — a factory that cannot build is itself a readiness problem
            problems.append(f"remediation components cannot be built ({type(e).__name__})")
        return problems

    def readiness_info(self) -> dict[str, Any]:
        """준비 확인 라우트의 답: ``ready`` / ``missing`` + ``mode`` 와 ``runs_in`` (``web`` = 이 프로세스, ``build_pod`` = 이관
        성공 뒤 빌드 파드). / The readiness answer incl. where remediation runs."""
        missing = self.readiness()
        return {"ready": not missing and not self.job_mode, "missing": list(missing), "mode": self.mode,
                "runs_in": "build_pod" if self.job_mode else "web"}

    # ------------------------------------------------------------------ submit / queue
    def submit(self, tmp_zip: Path, name: str, configs: list[str] | None) -> RemediationState:
        """업로드 하나를 접수한다: 설정 → 준비 확인(NotReady) → ZIP 검사(IntakeRejected 면 폴더를 남기지 않는다) → 첫 상태 →
        줄 세우기. ``tmp_zip`` 은 호출한 쪽이 지운다(여기서는 복사한다).

        Accept one upload: config, readiness (`NotReady`), intake (`IntakeRejected` leaves no directory), the first state,
        then queue it. The caller deletes `tmp_zip`. Job modes: `NotAvailable` before anything else."""
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
        if self.job_mode:      # 마지막 방어선: Job 모드의 웹 파드는 작업 스레드를 띄우지 않는다
            raise self._not_available()
        self._events[rid] = threading.Event()
        self._active.add(rid)
        self._queue.append(rid)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._dispatch, name="sbmig-remediation-worker", daemon=True)
            self._worker.start()

    def _dispatch(self) -> None:
        # 줄의 맨 앞부터 하나씩 — 한 번에 하나만 돈다(이 프로세스 안의 Maven 이 서로 부딪히지 않게)
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
            state.owner = self.owner
            self._save(paths, state)
            with self._lock:
                self._live[rid] = state      # 같은 잠금 안에서: 중지 요청이 이 객체에 바로 들어가게
        ev = self._events.get(rid) or threading.Event()
        handler = self._attach_log(paths)
        try:
            try:
                cfg = remediation_config(self._config(list(state.configs)))
                components = self.components_factory(cfg, paths)
            except Exception as e:  # noqa: BLE001 — the run fails visibly instead of staying QUEUED
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
            except Exception as e:  # noqa: BLE001 — run_remediation turns node errors into FAILED; this is a bug guard
                log.error("remediation %s crashed: %s", rid, graph_mod.scrub_message(repr(e), paths.root))
                with lock:
                    state.status, state.reason = FAILED, R_INTERNAL_ERROR
                    state.reasons = [R_INTERNAL_ERROR]
                    state.message = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root)
                    state.updated = _now()
                    self._save(paths, state)
            # 서버 종료 때문에 멈춘 작업(사람이 중지를 누르지 않음)은 STOPPED 가 아니라 INTERRUPTED — 재시작 규칙과 같은 뜻
            if self._closing.is_set() and state.status == STOPPED and not state.stop_requested:
                with lock:
                    state.status = INTERRUPTED
                    state.message = SHUTDOWN_MESSAGE
                    state.updated = _now()
                    self._save(paths, state)
            log.info("remediation %s: %s %s", rid, state.status, state.reason)
        finally:
            self._detach_log(handler)

    @staticmethod
    def _chain_module() -> Any:
        # 늦게 부른다: chain.py 가 없는(또는 깨진) 동안에도 이 모듈과 웹 앱은 뜬다 — sys.modules 를 먼저 보는 import_module
        try:
            return importlib.import_module("sbmig.remediations.chain")
        except ImportError as e:
            raise NotAvailable(f"chained remediation cannot be resumed here: sbmig.remediations.chain is not available "
                               f"({type(e).__name__})") from None

    def _execute_chained(self, rid: str, paths: RunPaths) -> None:
        """in-process 모드에서 이어 돈 조치를 ``chain.resume`` 으로 이어서 돌린다(사람이 재개했을 때만)."""
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
            except Exception as e:  # noqa: BLE001 — a crash must leave a visible FAILED, never a silent RUNNING
                log.error("remediation %s (chained) crashed: %s", rid, graph_mod.scrub_message(repr(e), paths.root))
                self._journal_safe(paths, {"event": "resume_failed", "error": type(e).__name__})
                with lock:
                    try:
                        st = self._load(rid)
                    except UnknownRemediation:
                        return
                    if st.status in ACTIVE:          # chain 이 손대기 전에 실패했으면 재개할 수 있는 상태 그대로 둔다
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
                for h in root.handlers:     # 파일은 DEBUG 까지, 콘솔은 원래 수준 그대로
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
        """그 작업이 줄에서 빠지고 스레드가 끝날 때까지 기다린다(테스트·도구용). 끝났으면 True.

        Wait until `rid` is neither queued nor running; True when it finished within `timeout`."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while rid in self._active:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """줄이 비고 작업 스레드가 끝날 때까지 기다린다. / Wait until nothing is queued or running."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._active or (self._worker is not None and self._worker.is_alive()):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True

    # ------------------------------------------------------------------ reads
    def _current_plan(self, paths: RunPaths, state: RemediationState) -> RoundPlan | None:
        data = read_json(paths.plan(state.round)) if state.round else None
        return RoundPlan.from_dict(data) if isinstance(data, dict) else None

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
        # 화면의 재개 버튼: 누를 수 있는가와 못 누르는 까닭(Job 모드 = 다음 단계, 결정 대기, 끝 상태 …)
        if self.job_mode:
            note = RESUME_NEXT_STEP.format(mode=self.mode) if chained else NOT_AVAILABLE_MESSAGE.format(mode=self.mode)
            return {"available": False, "note": note}
        if shown not in RESUMABLE or not graph_mod.can_resume(dataclasses.replace(st, status=shown)):
            return {"available": False, "note": f"{shown} cannot be resumed"}
        with self._lock:
            if rid in self._active:
                return {"available": False, "note": "already queued or running"}
        return {"available": True, "note": ""}

    def _shown(self, rid: str, st: RemediationState, chained: bool) -> tuple[str, bool]:
        stale = self._stale(rid, st, chained)
        return (INTERRUPTED if stale else st.status), stale

    def status(self, rid: str) -> dict[str, Any]:
        """API 의 상태 dict(API.md 의 모양) + ``kind`` (chained / standalone), 이어 돈 조치면 ``run_id``, 파드가 사라진 이어 돈
        조치면 ``status = INTERRUPTED`` 와 ``stale: true`` (파일은 고치지 않는다), ``resume`` {available, note}.

        The status dict of the API contract plus `kind`, `run_id` (chained), `stale`, `resume` {available, note}."""
        paths, chained = self._locate(rid)
        st = self._load(rid)
        shown, stale = self._shown(rid, st, chained)
        plan = self._current_plan(paths, st)
        pending = len(plan.pending()) if plan is not None else 0
        base = f"/api/remediations/{rid}"
        withheld = chained and self._withheld(rid)
        files = self._available_files(paths)
        if withheld:                      # 마지막 정제 전: 쓰는 순간 정제되는 파일만 목록에 올린다
            files = [f for f in files if f in LIVE_FILES]
        stop_requested = st.stop_requested
        if chained and st.status in ACTIVE and not stale:
            stop_requested = stop_requested or (paths.root.parent / STOP_REQUEST_FILE).is_file()
        out: dict[str, Any] = {
            "id": rid, "name": st.name, "status": shown, "reason": st.reason, "reasons": list(st.reasons),
            "message": st.message, "node": st.node, "next_node": st.next_node, "round": st.round,
            "counts": dict(sorted(st.counts.items())), "created": st.created, "updated": st.updated,
            "stop_requested": stop_requested, "pending_approvals": pending,
            "delivery": {"sha256": st.delivery.sha256, "size": st.delivery.size, "round": st.delivery.round,
                         "build_ok": st.delivery.build_ok, "scanned": st.delivery.scanned} if st.delivery else None,
            "files": files,
            "links": {"self": base, "proposals": f"{base}/proposals", "approvals": f"{base}/approvals",
                      "resume": f"{base}/resume", "stop": f"{base}/stop", "result": f"{base}/result",
                      "files": f"{base}/files/"},
            "kind": KIND_CHAINED if chained else KIND_STANDALONE, "mode": self.mode,
            "resume": self._resume_info(rid, st, chained, shown),
        }
        if chained:
            out["run_id"] = rid
            if withheld:
                out["withheld"] = True
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
        return out

    def _chained_ids(self) -> list[str]:
        # 출력 폴더의 이관 실행 폴더 가운데 remediation/state.json 이 있는 것(`_` 로 시작하는 폴더는 이관 실행이 아니다)
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
        """목록(새것부터) 한 쪽: 단독 조치와 이어 돈 조치를 합친다(줄마다 ``kind``, 이어 돈 조치는 ``run_id``). ``size`` 는 1…100.

        One page of summaries (standalone + chained, `kind` per row), newest first."""
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

    def proposals(self, rid: str, all_rounds: bool = False) -> dict[str, Any]:
        """지금 라운드(``all_rounds`` 면 모든 라운드)의 제안 전체(diff·해시 세 개·결정 포함)와 남긴 항목.

        The current round's proposals (all rounds with `all_rounds`) with diffs, hashes and decisions."""
        paths, chained = self._locate(rid)
        st = self._load(rid)
        plan = self._current_plan(paths, st)
        out: dict[str, Any] = {"status": self._shown(rid, st, chained)[0], "round": st.round,
                               "source_tree_hash": plan.source_tree_hash if plan else "",
                               "policy_hash": plan.policy_hash if plan else ""}
        if all_rounds:
            plans = []
            for rs in sorted(st.rounds, key=lambda r: r.round):
                data = read_json(paths.plan(rs.round))
                if isinstance(data, dict):
                    plans.append(RoundPlan.from_dict(data))
            out["proposals"] = [p.to_dict() for pl in plans for p in pl.proposals]
            out["residuals"] = [r.to_dict() for pl in plans for r in pl.residuals]
            out["rounds"] = [pl.round for pl in plans]
        else:
            out["proposals"] = [p.to_dict() for p in plan.proposals] if plan else []
            out["residuals"] = [r.to_dict() for r in plan.residuals] if plan else []
        return out

    def result(self, rid: str) -> dict[str, Any]:
        """끝 상태면 report.json 내용, 아니면 ``NotFinished``(이어 돈 조치는 이관 실행의 마지막 정제 전이면 ``Withheld``).

        report.json once terminal, else `NotFinished` (`Withheld` for a chained one before the final redaction pass)."""
        paths, chained = self._locate(rid)
        st = self._load(rid)
        if st.status not in TERMINAL:
            raise NotFinished(f"remediation {rid} is {st.status}", status=st.status)
        if chained and self._withheld(rid):
            raise self._withheld_error(rid, "report.json")
        data = read_json(paths.report_json)
        if isinstance(data, dict):
            return data
        return report_mod.build_report_json(st, paths)

    def file_path(self, rid: str, name: str) -> Path | None:
        """화이트리스트 이름 → 작업 폴더 안의 실제 파일. 모르는 이름·없는 파일·폴더 밖(링크 포함)은 None.

        Whitelisted name → the real file inside the run directory; None for anything else. A chained run's files other
        than the journal raise `Withheld` while the migration run's final redaction pass is pending."""
        paths, chained = self._locate(rid)
        if not paths.state.is_file():
            raise UnknownRemediation(f"unknown remediation {rid}")
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

    # ------------------------------------------------------------------ approvals
    def decide(self, rid: str, decisions: list[Decision], by: str) -> dict[str, Any]:
        """승인·거절을 받는다(AWAITING_APPROVAL 에서만). 모든 결정을 먼저 맞춰 보고(``planner.validate_decision``: 제안 해시·
        원본 트리 해시·정책 해시·diff, 지금 트리·정책과도), 하나라도 틀리면 아무것도 쓰지 않고 ``ApprovalMismatch``.
        통과하면 plan.json 을 원자적으로 고쳐 쓰고 approvals.jsonl 에 한 줄씩 덧붙인다. 재개하지 않는다.

        All-or-nothing approval: every decision is validated before anything is written. Does not resume.
        Chained: the policy hash comes from the configs the migration run used (`chained_configs`); refused (`Withheld`)
        while the migration run's final redaction pass may still rewrite the directory."""
        paths, chained = self._locate(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            if st.status != AWAITING_APPROVAL or rid in self._active:
                raise StateConflict(f"remediation {rid} is {st.status}, not {AWAITING_APPROVAL}", status=st.status)
            if chained and self._withheld(rid):
                raise self._withheld_error(rid, "plan.json")
            plan = self._current_plan(paths, st)
            if plan is None:
                raise StateConflict(f"round {st.round} has no plan", status=st.status)
            if not decisions:
                raise ApprovalMismatch("no decision given", fields=["decisions"])
            ids = [d.proposal_id for d in decisions]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ApprovalMismatch(f"duplicate proposal id(s) in one request: {', '.join(dupes)}",
                                       fields=["proposal_id"], proposals={i: ["proposal_id"] for i in dupes})
            # 이어 탄 조치(chained)의 정책 기준은 그 조치를 돌린 빌드 파드가 state.json 에 적은 정책 해시다: 웹 파드의 환경 값이
            # 빌드 파드와 한 글자라도 다르면 여기서 다시 계산한 해시가 달라져 멀쩡한 승인이 거부된다. 정책이 정말 바뀌었는지는
            # 재개하는 빌드 파드가 자기 설정으로 다시 확인한다(graph 의 plan 재사용 검사: 다르면 계획을 새로 세우고 옛 승인은 버린다).
            if chained and st.policy_hash:
                current_policy = st.policy_hash
            else:
                current_policy = planner_mod.policy_hash(self._config(self._configs_for(rid, st, chained)))
            current_tree = intake_mod.tree_hash(paths.accepted, intake_mod.manifest_paths(paths.manifest))
            by_id = {p.id: p for p in plan.proposals}
            problems: list[str] = []
            fields: set[str] = set()
            per: dict[str, list[str]] = {}
            for d in decisions:
                p = by_id.get(d.proposal_id)
                if p is None:
                    problems.append(f"unknown proposal {d.proposal_id!r} in round {st.round}")
                    fields.add("proposal_id")
                    per[str(d.proposal_id)] = ["proposal_id"]
                    continue
                try:
                    planner_mod.validate_decision(p, d, current_tree_hash=current_tree, current_policy_hash=current_policy)
                except ApprovalMismatch as e:
                    problems.append(str(e))
                    f = list(e.details.get("fields") or [])
                    fields.update(f)
                    per[p.id] = f
            if problems:
                raise ApprovalMismatch("; ".join(problems), fields=sorted(fields), proposals=per)
            now = _iso()
            lines = []
            for d in decisions:
                p = by_id[d.proposal_id]
                p.decision = d.decision
                lines.append({"ts": now, "by": by, "proposal_id": p.id, "decision": d.decision, "round": st.round,
                              "proposal_hash": p.proposal_hash, "source_tree_hash": p.source_tree_hash,
                              "policy_hash": p.policy_hash, "diff_sha256": sha256_text(p.diff or ""),
                              "comment": (d.comment or "")[:2000]})
            write_json_atomic(paths.plan(st.round), plan.to_dict())
            with open(paths.approvals, "a", encoding="utf-8") as f:
                f.writelines(json.dumps(_scrub.scrub_json(ln), ensure_ascii=False) + "\n" for ln in lines)
            pending = len(plan.pending())
            st.counts = {**st.counts, "proposals_pending": pending}
            st.updated = _now()
            self._save(paths, st)
            self._journal_safe(paths, {"event": "approvals", "by": by, "round": st.round,
                                       "decisions": {d.proposal_id: d.decision for d in decisions}, "pending": pending})
        return {"accepted": ids, "pending": pending}

    # ------------------------------------------------------------------ resume / stop / delete
    def resume(self, rid: str, confirm: str | None = None, label: str | None = None, project_id: str | None = None,
               analysis_id: str | None = None, by: str = "") -> dict[str, Any]:
        """재개(사람이 부를 때만). 재개할 수 있는 상태이고 도는 스레드가 없어야 한다. 승인 대기에 결정 안 된 제안이 남았으면,
        업로드 확인 대기에 ``confirm`` 이 없으면 ``StateConflict``. ``reupload`` = 업로드가 IVAS 에 닿지 않았음을 사람이 확인 →
        다시 올린다. ``receipt`` = 사람이 IVAS 에서 찾은 프로젝트·분석 ID 로 이어서 조회한다(다시 올리지 않는다).

        Job 모드에서는 늘 ``NotAvailable`` (이어 돈 조치를 새 빌드 파드에서 재개하는 것은 다음 단계). in-process 의 이어 돈 조치는
        상태를 그대로 둔 채(``chain.resume`` 은 재개할 수 있는 상태를 받는다) 이 프로세스의 작업 스레드가 ``chain.resume`` 을 부른다.

        Explicit resume. `confirm` is required in AWAITING_CONFIRMATION: "reupload" or "receipt" (+ project / analysis id).
        Job modes: always `NotAvailable`. In-process, a chained state is resumed through `chain.resume` in a worker thread."""
        paths, chained = self._locate(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            if self.job_mode:
                msg = RESUME_NEXT_STEP if chained else NOT_AVAILABLE_MESSAGE
                raise NotAvailable(msg.format(mode=self.mode), mode=self.mode, status=st.status)
            with self._lock:
                alive = rid in self._active
            if chained and not alive:
                if self._stale(rid, st, chained):
                    st.status = INTERRUPTED           # 파드가 사라진 이어 돈 조치: 사람이 재개할 때 비로소 파일에도 적는다
                elif st.status in ACTIVE or self._migration_state(rid) in ("queued", "running"):
                    raise StateConflict(f"the migration run {rid} is still running", status=st.status)
            if alive or st.status not in RESUMABLE or not graph_mod.can_resume(st):
                raise StateConflict(f"remediation {rid} is {st.status} and cannot be resumed", status=st.status)
            if st.status == AWAITING_APPROVAL:
                plan = self._current_plan(paths, st)
                if plan is not None and plan.pending():
                    raise StateConflict(f"{len(plan.pending())} proposal(s) still wait for a decision", status=st.status,
                                        pending=len(plan.pending()))
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
            if chained:
                self._chain_module()                  # 없으면 여기서 NotAvailable — 상태 파일을 고치기 전에
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

    def stop(self, rid: str) -> dict[str, Any]:
        """중지 요청(QUEUED / RUNNING 에서만). 다음 workflow 경계에서 먹는다 — 도는 IVAS 분석을 취소했다고 하지 않는다.

        이어 돈 조치: 이 프로세스가 재개해 돌리는 중이면 그 스레드에 알리고, 아니면 이관 실행 폴더에 ``stop.request`` 를 쓴다
        (RunManager 가 Job 실행에 쓰는 것과 같은 파일 — 빌드 파드가 경계에서 본다). 상태 파일은 빌드 파드의 것이라 고치지 않는다.

        Request a stop at the next workflow boundary (QUEUED / RUNNING only). Chained: signal this process's thread, else
        write the migration run's `stop.request` (the build pod reads it at a boundary); its state file is left alone."""
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
                live.stop_requested = True       # 작업 스레드의 다음 저장이 이 값을 덮지 않게 같은 객체에 넣는다
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
            with self._lock:
                alive = rid in self._active
                ev = self._events.get(rid)
            if alive and ev is not None:              # in-process 재개: 이 프로세스의 작업 스레드
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
            # 이 프로세스의 이관 실행(in-process)이면 엔진의 멈춤도 켠다. Job 실행에는 RunManager.stop 을 부르지 않는다 — 유예 뒤
            # Job 을 지워 도는 IVAS 업로드·Maven 을 끊기 때문(멈춤은 stop.request 하나로 경계에서)
            if run is not None and not getattr(run, "job", False):
                try:
                    rm.stop(rid)
                except Exception:  # noqa: BLE001 — the file above is the request; this is a convenience
                    log.debug("RunManager.stop(%s) failed", rid, exc_info=True)
            log.info("remediation %s (chained): %s written", rid, STOP_REQUEST_FILE)
        return {"stop_requested": True, "note": STOP_NOTE}

    def delete(self, rid: str) -> None:
        """작업 폴더 전체(원본 ZIP 포함)를 지운다. QUEUED / RUNNING 이면 ``StateConflict``. 이어 돈 조치는 늘 거절한다 — 그 폴더의
        주인은 이관 실행이다.

        Remove the whole run directory (incl. the read-only original ZIP); refused while queued / running, and always for
        a chained remediation (the migration run owns that directory)."""
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
            # 원본 ZIP 은 읽기 전용이다(Windows 는 읽기 전용 파일을 지우지 못한다) — 쓰기 권한을 준 뒤 다시
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

    # ------------------------------------------------------------------ shutdown
    def shutdown(self) -> dict[str, Any]:
        """서버 종료: 모든 작업에 멈춤을 알리고 잠깐 기다린다. 그 사이 경계에서 멈춘 작업은 INTERRUPTED 로 적고, 아직 도는
        작업은 다음 시작이 INTERRUPTED 로 바꾼다(자동 재개 없음).

        Server shutdown: signal every run, join briefly; the next start marks what was left as INTERRUPTED."""
        # 작업마다의 이벤트는 건드리지 않는다: _closing 이 stop() 을 켜고, 사람의 중지 요청(STOPPED)과 종료(INTERRUPTED)를 가른다
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
