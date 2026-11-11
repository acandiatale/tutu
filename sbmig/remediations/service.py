"""취약점 조치 작업 관리자 — 웹 파드 프로세스 안에서 작업 스레드로 조치 그래프를 돌리고, state.json 으로 상태를 지킨다.
흐름상 위치: ``api.py`` 라우트 → :class:`RemediationManager` → ``graph.run_remediation``. 접수(``intake.accept_zip``) →
QUEUED → 작업 스레드(한 번에 하나: 이 프로세스 안의 Maven 이 서로 부딪히지 않게, 나머지는 접수 순서대로 기다린다) →
대기 상태(승인·업로드 확인·중지)나 끝 상태. 승인(``decide``)은 제안 해시·원본 트리 해시·정책 해시·diff 를 **모두** 맞춰 본 뒤에만
plan.json 과 approvals.jsonl 에 쓴다(하나라도 틀리면 아무것도 쓰지 않는다). 재개(``resume``)는 사람이 부를 때만 — 서버가 새로
뜨면 QUEUED / RUNNING 으로 남은 작업은 INTERRUPTED 가 되고 **아무것도 자동으로 다시 보내지 않는다**.
Maven 로컬 저장소: 설정의 ``build.local_repo`` 가 있으면 조치 실행은 ``<그 경로>-remediation`` 을 쓴다 — 이관 빌드 파드들이
쓰는 저장소를 여러 Maven 프로세스가 함께 쓰지 않게(:func:`remediation_config`). 비어 있으면 Maven 기본값 그대로다.
데이터베이스 없음: 목록·상태는 ``<out>/_remediations/<id>/state.json`` 파일에서 읽는다.

The remediation job manager: worker threads of this process (one remediation at a time), `state.json` per run,
approvals (all-or-nothing hash checks), resume / stop / delete, and the restart rule (QUEUED / RUNNING of an earlier
process → INTERRUPTED, never resumed automatically). File-based only.
"""
from __future__ import annotations

import collections
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

    One per web app. `default_configs` is a list of config names or a callable returning them (the web app passes
    `RunManager.default_configs` so both features resolve the server default the same way)."""

    def __init__(self, out_dir: Path, repo_root: Path, default_configs: list[str] | Callable[[], list[str]] | None = None,
                 components_factory: ComponentsFactory | None = None, config_loader: ConfigLoader | None = None):
        self.out_dir = Path(out_dir)
        self.repo_root = Path(repo_root)
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

    def paths(self, rid: str) -> RunPaths:
        """작업 폴더의 경로 규칙(ID 검사 뒤). / The run paths of a validated id."""
        return RunPaths(self.root / self._check_id(rid))

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

        Problems that prevent a remediation (IVAS env names, Maven / JDK / settings problems; never values)."""
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

    # ------------------------------------------------------------------ submit / queue
    def submit(self, tmp_zip: Path, name: str, configs: list[str] | None) -> RemediationState:
        """업로드 하나를 접수한다: 설정 → 준비 확인(NotReady) → ZIP 검사(IntakeRejected 면 폴더를 남기지 않는다) → 첫 상태 →
        줄 세우기. ``tmp_zip`` 은 호출한 쪽이 지운다(여기서는 복사한다).

        Accept one upload: config, readiness (`NotReady`), intake (`IntakeRejected` leaves no directory), the first state,
        then queue it. The caller deletes `tmp_zip`."""
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
        paths = self.paths(rid)
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

    def status(self, rid: str) -> dict[str, Any]:
        """API 의 상태 dict(API.md 의 모양). / The status dict of the API contract."""
        paths = self.paths(rid)
        st = self._load(rid)
        plan = self._current_plan(paths, st)
        pending = len(plan.pending()) if plan is not None else 0
        base = f"/api/remediations/{st.id}"
        out: dict[str, Any] = {
            "id": st.id, "name": st.name, "status": st.status, "reason": st.reason, "reasons": list(st.reasons),
            "message": st.message, "node": st.node, "next_node": st.next_node, "round": st.round,
            "counts": dict(sorted(st.counts.items())), "created": st.created, "updated": st.updated,
            "stop_requested": st.stop_requested, "pending_approvals": pending,
            "delivery": {"sha256": st.delivery.sha256, "size": st.delivery.size} if st.delivery else None,
            "files": self._available_files(paths),
            "links": {"self": base, "proposals": f"{base}/proposals", "approvals": f"{base}/approvals",
                      "resume": f"{base}/resume", "stop": f"{base}/stop", "result": f"{base}/result",
                      "files": f"{base}/files/"},
        }
        if st.status == AWAITING_CONFIRMATION:
            out["pending_uploads"] = sorted(st.pending_upload)
        pos = self._queue_position(rid) if st.status == QUEUED else None
        if pos is not None:
            out["queue_position"] = pos
        return out

    def list(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        """목록(새것부터) 한 쪽. ``size`` 는 1…100. / One page of summaries, newest first."""
        page = max(1, int(page))
        size = max(1, min(MAX_PAGE_SIZE, int(size)))
        with self._lock:
            ids = list(self._index)
        states: list[RemediationState] = []
        for rid in ids:
            try:
                states.append(self._load(rid))
            except (UnknownRemediation, BadRemediationId):
                continue
        states.sort(key=lambda s: (s.created, s.id), reverse=True)
        chunk = states[(page - 1) * size: page * size]
        items = []
        for s in chunk:
            d = report_mod.summary(s)
            d["stop_requested"] = s.stop_requested
            d["url"] = f"/api/remediations/{s.id}"
            items.append(d)
        return {"items": items, "page": page, "size": size, "total": len(states)}

    def proposals(self, rid: str, all_rounds: bool = False) -> dict[str, Any]:
        """지금 라운드(``all_rounds`` 면 모든 라운드)의 제안 전체(diff·해시 세 개·결정 포함)와 남긴 항목.

        The current round's proposals (all rounds with `all_rounds`) with diffs, hashes and decisions."""
        paths = self.paths(rid)
        st = self._load(rid)
        plan = self._current_plan(paths, st)
        out: dict[str, Any] = {"status": st.status, "round": st.round,
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
        """끝 상태면 report.json 내용, 아니면 ``NotFinished``. / report.json once terminal, else `NotFinished`."""
        paths = self.paths(rid)
        st = self._load(rid)
        if st.status not in TERMINAL:
            raise NotFinished(f"remediation {rid} is {st.status}", status=st.status)
        data = read_json(paths.report_json)
        if isinstance(data, dict):
            return data
        return report_mod.build_report_json(st, paths)

    def file_path(self, rid: str, name: str) -> Path | None:
        """화이트리스트 이름 → 작업 폴더 안의 실제 파일. 모르는 이름·없는 파일·폴더 밖(링크 포함)은 None.

        Whitelisted name → the real file inside the run directory; None for anything else."""
        paths = self.paths(rid)
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
        try:
            if target.is_symlink() or not target.is_file():
                return None
            real = target.resolve()
            real.relative_to(paths.root.resolve())
        except (OSError, ValueError):
            return None
        return real

    # ------------------------------------------------------------------ approvals
    def decide(self, rid: str, decisions: list[Decision], by: str) -> dict[str, Any]:
        """승인·거절을 받는다(AWAITING_APPROVAL 에서만). 모든 결정을 먼저 맞춰 보고(``planner.validate_decision``: 제안 해시·
        원본 트리 해시·정책 해시·diff, 지금 트리·정책과도), 하나라도 틀리면 아무것도 쓰지 않고 ``ApprovalMismatch``.
        통과하면 plan.json 을 원자적으로 고쳐 쓰고 approvals.jsonl 에 한 줄씩 덧붙인다. 재개하지 않는다.

        All-or-nothing approval: every decision is validated before anything is written. Does not resume."""
        paths = self.paths(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            if st.status != AWAITING_APPROVAL or rid in self._active:
                raise StateConflict(f"remediation {rid} is {st.status}, not {AWAITING_APPROVAL}", status=st.status)
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
            cfg = self._config(list(st.configs))
            current_policy = planner_mod.policy_hash(cfg)
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

        Explicit resume. `confirm` is required in AWAITING_CONFIRMATION: "reupload" or "receipt" (+ project / analysis id)."""
        paths = self.paths(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
            with self._lock:
                alive = rid in self._active
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

        Request a stop at the next workflow boundary (QUEUED / RUNNING only)."""
        paths = self.paths(rid)
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

    def delete(self, rid: str) -> None:
        """작업 폴더 전체(원본 ZIP 포함)를 지운다. QUEUED / RUNNING 이면 ``StateConflict``.

        Remove the whole run directory (incl. the read-only original ZIP); refused while queued / running."""
        paths = self.paths(rid)
        lock = self._run_lock(rid)
        with lock:
            st = self._load(rid)
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
    "ID_RE",
    "BadRemediationId",
    "NotFinished",
    "RemediationManager",
    "UnknownRemediation",
    "default_components_factory",
    "remediation_config",
    "safe_problem",
]
