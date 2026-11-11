"""이관(migration) 빌드 파드에서 이관이 BUILD SUCCESS 로 끝난 뒤 이어서 도는 취약점 조치(remediation) — 연결 고리.
이관 그래프는 노드 하나(``remediation``)만 더하고 이 파일의 ``run_after_migration`` 을 부른다. 조치 흐름 자체는 별개의
그래프(``graph.run_remediation``)이고 이관의 성공·종료 코드·보고서 판정을 절대 바꾸지 않는다 — 여기서 어떤 예외도 밖으로 나가지 않는다.

실행 폴더: ``<이관 실행 폴더>/remediation/`` (``CHAIN_DIR``, 안의 구조는 ``models.RunPaths`` 그대로), 작업 id = 이관 실행 id.
이관이 끝낸 프로젝트 트리로 원본 ZIP(``original/source.zip``)을 결정적으로 만들고(``source_zip``) ``intake.accept_zip`` 에 넣는다.
준비 확인은 IVAS 환경 변수 + Maven 준비뿐이다 — ``remediation.trusted_projects`` 는 묻지 않는다(이관이 이미 이 프로젝트를 빌드했다).
Maven 로컬 저장소는 이 파드의 이관과 같은 것(``cfg.build.local_repo``, 슬롯 저장소 포함)을 쓴다 — ``-remediation`` 접미사를 붙이지
않는다(웹 파드에서 돌던 시절의 규칙). 빌드 파드는 사람을 기다릴 수 없다: AWAITING_APPROVAL / AWAITING_CONFIRMATION 은 저장하고 끝난다;
재개는 새 빌드 파드가 ``resume`` 을 부른다(Worker 동작 remediation-resume). 시간 한도: ``remediation.chain_budget_minutes`` (0 = 없음) —
이 파드에서 이번 호출이 그만큼 돌면 ``stop`` 이 참이 되어 다음 경계에서 STOPPED, 이유 ``chain_budget`` (재개 가능; 진행 중인 검사의 영수증은
남아 재개가 같은 IVAS 분석을 이어서 조회한다).

Chained remediation: runs in the build pod after a migration that ended in BUILD SUCCESS. The migration graph's
`remediation` node calls `run_after_migration`; the remediation graph stays a separate feature. Its outcome never changes
the migration's verdict and no exception leaves this module's entry points.
"""
from __future__ import annotations

import logging
import os
import time
import traceback
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import graph as graph_mod
from .interfaces import Components, Journal, StopCheck
from .models import (
    FAILED,
    R_INTERNAL_ERROR,
    RemediationState,
    RunPaths,
    StateConflict,
    read_json,
    write_json_atomic,
)

log = logging.getLogger("sbmig.remediations.chain")

CHAIN_DIR = "remediation"
# 시간 한도로 멈춘 STOPPED 의 이유(models.R_* 는 이 파일이 고치지 않는다 — 이어서 도는 조치에만 있는 이유라 여기 둔다)
R_CHAIN_BUDGET = "chain_budget"
_monotonic = time.monotonic               # 시험이 바꿔 끼우는 시계(경과 시간만 잰다)
SCOPE = "remediation"                     # 이관 저널에 섞이는 조치 줄의 표시("scope": "remediation")
SOURCE_NAME = "source.zip"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)          # 결정적 ZIP: 시각을 고정한다(ZIP 이 표현하는 가장 이른 값)
# 프로젝트 폴더 안의 sbmig 작업 파일·VCS — 조치의 원본 ZIP 에 넣지 않는다
EXCLUDED_DIRS = (".git", ".sbmig")
_MAVEN_OUTPUT_MARKERS = ("maven-status", "maven-archiver")


def chain_paths(run_dir: Path | str) -> RunPaths:
    """이관 실행 폴더 안의 조치 실행 폴더 ``<run_dir>/remediation`` 의 경로 규칙.

    The `RunPaths` of the chained remediation inside a migration run directory."""
    return RunPaths(Path(run_dir) / CHAIN_DIR)


def load_state(run_dir: Path | str) -> RemediationState | None:
    """이어서 돈 조치의 ``state.json``. 없거나 읽을 수 없으면 None. 예외 없음.

    The chained remediation's state, or None."""
    try:
        data = read_json(chain_paths(run_dir).state)
        return RemediationState.from_dict(data) if isinstance(data, dict) else None
    except Exception:
        log.debug("chained remediation state unreadable", exc_info=True)
        return None


def _problem(text: Any) -> str:
    return graph_mod.scrub_message(str(text), limit=500)


def readiness(cfg: Any, components: Components | None) -> list[str]:
    """이어서 도는 조치를 시작할 수 없게 하는 문제들(빈 목록 = 준비됨): 빠진 IVAS 환경 변수 이름 + Maven / JDK / settings.xml
    문제. 값·비밀은 넣지 않는다. ``trusted_projects`` 는 이 경로에서 묻지 않는다. ``components`` 가 None 이면 운영 부품으로 확인한다.

    Problems that prevent a chained remediation (IVAS env names + Maven readiness; never values; no trusted_projects)."""
    from . import ivas as ivas_mod
    problems = [_problem(p) for p in ivas_mod.readiness(ivas_mod.ivas_settings(cfg))]
    try:
        comps = components if components is not None else graph_mod.default_components(cfg)
        problems += [_problem(p) for p in comps.maven.readiness()]
    except Exception as e:  # noqa: BLE001 — 부품을 못 만들면 그것이 준비 문제다
        problems.append(f"remediation components cannot be built ({type(e).__name__})")
    return problems


def _is_build_output(dirpath: Path, rel_parts: tuple[str, ...], filenames: Iterable[str]) -> bool:
    """``dirpath/target`` 이 Maven 빌드 산출물인가: src 아래면 아니고, 옆에 pom.xml 이 있거나 안에 Maven 기록 폴더가 있으면 그렇다
    (``Workspace.walk`` / result.zip 과 같은 규칙 — 패키지 이름이 target 인 소스를 빠뜨리지 않게)."""
    if "src" in rel_parts:
        return False
    return "pom.xml" in filenames or any((dirpath / "target" / m).is_dir() for m in _MAVEN_OUTPUT_MARKERS)


def source_zip(project_dir: Path | str, out_zip: Path | str, *, exclude_dirs: Iterable[str] = (),
               journal: Journal | None = None) -> None:
    """이관이 끝낸 프로젝트 트리의 결정적 ZIP(조치 접수용): 일반 파일만, 이름 순, 고정 시각·권한, 루트 pom.xml 이 ZIP 뿌리에.
    빼는 것: ``.git/`` · ``.sbmig/`` (sbmig 작업 파일), Maven 빌드 산출물 ``target/`` (pom.xml 옆), ``exclude_dirs`` (프로젝트 기준 상대
    경로 — 접수가 무시한 떠도는 pom 복사본 폴더). 심볼릭 링크는 넣지 않고(접수가 거부한다) 저널에 ``remediation_symlink_skipped``.

    A deterministic ZIP of the migrated project (regular files only; `.git`, `.sbmig`, Maven `target/` output and
    `exclude_dirs` left out; symlinks skipped and journaled); the root pom.xml sits at the ZIP root."""
    root = Path(project_dir)
    out = Path(out_zip)
    ignored = {str(d).replace("\\", "/").strip("/") for d in exclude_dirs if str(d).strip("/")}
    files: list[str] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        parts = tuple(rel_dir.split("/")) if rel_dir else ()
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if (d / name).is_symlink():
                skipped.append(rel)
            elif name in EXCLUDED_DIRS or rel in ignored or any(rel.startswith(i + "/") for i in ignored) or name == "target" and _is_build_output(d, parts, filenames):
                continue
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            p = d / name
            if p.is_symlink():
                skipped.append(rel)
            elif p.is_file():
                files.append(rel)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(root / rel, "rb") as src, zf.open(info, "w") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
    os.replace(tmp, out)
    if skipped:
        log.warning("remediation source ZIP: %d symlink(s) left out (%s)", len(skipped), ", ".join(skipped[:5]))
        if journal is not None:
            try:
                journal({"event": "remediation_symlink_skipped", "paths": sorted(skipped)[:50], "count": len(skipped)})
            except Exception:
                log.debug("symlink journal line not written", exc_info=True)


def _scoped(journal: Journal | None) -> Journal:
    """이관 저널로 가는 줄에 ``"scope": "remediation"`` 을 붙이는 저널. 저널 실패는 삼킨다."""
    def write(event: dict[str, Any]) -> None:
        if journal is None:
            return
        try:
            journal({**event, "scope": SCOPE})
        except Exception:
            log.debug("remediation journal line not written: %s", event.get("event"), exc_info=True)
    return write


def _safe_stop(stop: StopCheck | None) -> StopCheck:
    def check() -> bool:
        try:
            return bool(stop()) if stop is not None else False
        except Exception:  # noqa: BLE001 — 읽을 수 없는 멈춤 표시는 멈춤 요청이 아니다
            return False
    return check


class _Budget:
    """이번 호출의 시간 한도(분, 0 이하 = 없음). ``wrap(stop)`` 은 진짜 중지 요청이거나 한도를 넘었으면 참인 stop 을 돌려준다.
    한도를 처음 넘을 때 저널 ``remediation_budget`` 을 한 번 쓴다. ``exhausted`` = 한도 때문에 참이 된 적이 있다.

    The wall-time budget of one chained invocation; `wrap(stop)` also turns true once it is used up."""

    def __init__(self, cfg: Any, jr: Journal):
        rem = getattr(cfg, "remediation", None)
        try:
            minutes = float(getattr(rem, "chain_budget_minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        self.minutes = minutes
        self.started = _monotonic()
        self.exhausted = False
        self._jr = jr

    def over(self) -> bool:
        if self.minutes <= 0:
            return False
        if self.exhausted:
            return True
        if _monotonic() - self.started >= self.minutes * 60.0:
            self.exhausted = True
            self._jr({"event": "remediation_budget", "minutes": self.minutes})
            log.warning("chained remediation: time budget of %g minute(s) used up — stopping at the next boundary",
                        self.minutes)
            return True
        return False

    def wrap(self, stop: StopCheck) -> StopCheck:
        def check() -> bool:
            return bool(stop()) or self.over()
        return check


def _budget_outcome(paths: RunPaths, state: RemediationState, budget: _Budget, real_stop: StopCheck) -> RemediationState:
    """한도 때문에 멈춘 STOPPED 의 이유를 ``chain_budget`` 으로 바꿔 저장한다(진짜 중지 요청이 있으면 그대로 stop_requested).
    UI 가 "시간 한도로 멈춤 — 재개하면 이어서" 를 말할 수 있게 하려는 것이다."""
    from .models import R_STOP_REQUESTED, STOPPED
    if state.status != STOPPED or state.reason != R_STOP_REQUESTED or real_stop() or not budget.over():
        return state
    detail = state.message or ""
    state.reason, state.reasons = R_CHAIN_BUDGET, [R_CHAIN_BUDGET]
    state.stop_requested = False
    state.message = (f"the chained remediation used its time budget of {budget.minutes:g} minute(s) in this pod; "
                     "resume continues from the stopped node"
                     + (" (the remote IVAS analysis keeps running and is polled again without a new upload)"
                        if state.pending_receipt else "")
                     + (f" | {detail}" if detail else ""))
    _try_save(paths, state)
    return state


def _saver(paths: RunPaths) -> Callable[[RemediationState], None]:
    owner = f"chain-{os.getpid()}"

    def save(st: RemediationState) -> None:
        st.owner = owner
        write_json_atomic(paths.state, st.to_dict())
    return save


def _failed_state(run_id: str, reason: str, message: str, policy_hash: str = "",
                  base: RemediationState | None = None) -> RemediationState:
    now = time.time()
    st = base or RemediationState(id=run_id, name=SOURCE_NAME, created=now, policy_hash=policy_hash, next_node="")
    st.status, st.reason, st.reasons, st.message, st.updated = FAILED, reason, [reason], message, now
    return st


def _try_save(paths: RunPaths, st: RemediationState) -> None:
    try:
        _saver(paths)(st)
    except Exception:
        log.warning("chained remediation state not saved", exc_info=True)


def _error(paths: RunPaths, jr: Journal, e: BaseException, run_id: str,
           state: RemediationState | None) -> RemediationState:
    """잡은 예외 → 저널 ``remediation_error`` + 로그(traceback, 비밀 지움) + 저장한 FAILED 상태."""
    msg = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root, 500)
    jr({"event": "remediation_error", "type": type(e).__name__, "message": msg})
    log.error("chained remediation failed: %s", graph_mod.scrub_message(traceback.format_exc(), paths.root, 20000))
    st = _failed_state(run_id, R_INTERNAL_ERROR, msg, base=state)
    _try_save(paths, st)
    return st


def _ended(jr: Journal, st: RemediationState) -> None:
    jr({"event": "remediation_end", "remediation": st.id, "status": st.status, "reason": st.reason})
    log.info("chained remediation %s: %s %s", st.id, st.status, st.reason)


def run_after_migration(run_dir: Path | str, project_dir: Path | str, cfg: Any, *, run_id: str, journal: Journal | None,
                        stop: StopCheck | None, components: Components | None = None,
                        exclude_dirs: Iterable[str] = ()) -> RemediationState | None:
    """빌드에 성공한 이관 뒤 취약점 조치 한 번. None = 건너뜀(꺼져 있음: 조용히 / 준비 안 됨: 저널 ``remediation_skipped`` +
    WARNING 한 줄). 이미 이어서 돈 조치가 있으면(재시도 등) 새로 시작하지 않고 저널 ``remediation_exists`` 뒤 그 상태를 그대로 돌려준다.
    그 밖에는 원본 ZIP → ``intake.accept_zip`` → 첫 상태 → ``graph.run_remediation`` 의 끝 상태. 접수 거부는 FAILED 상태(이유 = 거부 코드),
    다른 예외는 FAILED internal_error — 어떤 예외도 밖으로 나가지 않는다. 저널 줄은 이관 저널로(``"scope": "remediation"``).

    One remediation after a successful migration. None = skipped (disabled / not ready); an existing chained state is
    returned untouched; otherwise the final state. Never raises."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state: RemediationState | None = None
    try:
        budget = _Budget(cfg, jr)            # 시간 한도는 이 호출이 시작된 때부터 잰다
        rem = getattr(cfg, "remediation", None)
        if rem is None or not bool(getattr(rem, "after_migration", False)):
            return None
        existing = load_state(run_dir)
        if existing is not None:
            jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
            log.info("chained remediation %s already exists (%s) — not started again", existing.id, existing.status)
            return existing
        comps = components
        if comps is None:
            try:
                comps = graph_mod.default_components(cfg, journal=jr)
            except Exception:
                log.debug("remediation components not built", exc_info=True)
        missing = readiness(cfg, comps)
        if missing:
            jr({"event": "remediation_skipped", "reason": "not_ready", "missing": missing})
            log.warning("vulnerability remediation skipped (not ready): %s", "; ".join(missing))
            return None
        real_stop = _safe_stop(stop)
        return _start(paths, Path(project_dir), cfg, comps, run_id=run_id, jr=jr, stop=budget.wrap(real_stop),
                      exclude_dirs=exclude_dirs, budget=budget, real_stop=real_stop)
    except Exception as e:  # noqa: BLE001 — the migration's verdict must never depend on the remediation
        return _error(paths, jr, e, run_id, state)


def _start(paths: RunPaths, project_dir: Path, cfg: Any, comps: Components, *, run_id: str, jr: Journal,
           stop: StopCheck, exclude_dirs: Iterable[str], budget: _Budget | None = None,
           real_stop: StopCheck | None = None) -> RemediationState:
    from . import intake as intake_mod
    from . import planner as planner_mod
    from .models import IntakeRejected
    if paths.root.exists():
        # 상태 파일 없이 남은 폴더 = 접수 도중에 죽은 지난 시도. IVAS 에 보낸 것은 상태가 생긴 뒤에만 있으므로 지우고 다시 한다
        import shutil
        shutil.rmtree(paths.root)
    paths.root.mkdir(parents=True)
    policy = planner_mod.policy_hash(cfg)
    jr({"event": "remediation_start", "remediation": run_id})
    log.info("vulnerability remediation %s: start (after the migration's BUILD SUCCESS)", run_id)
    source_zip(project_dir, paths.original_zip, exclude_dirs=exclude_dirs, journal=jr)
    try:
        result = intake_mod.accept_zip(paths.original_zip, paths.root, intake_mod.limits_from_config(cfg))
    except IntakeRejected as e:
        st = _failed_state(run_id, e.code, graph_mod.scrub_message(str(e), paths.root, 500), policy)
        _try_save(paths, st)
        jr({"event": "remediation_intake_rejected", "remediation": run_id, "code": e.code})
        log.warning("vulnerability remediation %s: intake rejected the migrated tree (%s)", run_id, e.code)
        _ended(jr, st)
        return st
    state = graph_mod.new_state(run_id, SOURCE_NAME, cfg, result, [], policy_hash=policy)
    save = _saver(paths)
    save(state)
    return _drive(paths, state, cfg, comps, jr=jr, stop=stop, budget=budget, real_stop=real_stop)


def _drive(paths: RunPaths, state: RemediationState, cfg: Any, comps: Components, *, jr: Journal,
           stop: StopCheck, budget: _Budget | None = None, real_stop: StopCheck | None = None) -> RemediationState:
    save = _saver(paths)
    try:
        state = graph_mod.run_remediation(state, paths, cfg, comps, save=save, journal=jr, stop=stop)
    except Exception as e:  # noqa: BLE001 — run_remediation turns node errors into FAILED; this guards the rest
        return _error(paths, jr, e, state.id, state)
    if budget is not None:
        state = _budget_outcome(paths, state, budget, real_stop or (lambda: False))
    _ended(jr, state)
    return state


def resume(run_dir: Path | str, cfg: Any, *, journal: Journal | None, stop: StopCheck | None,
           components: Components | None = None) -> RemediationState:
    """이어서 돈 조치를 다시 잇는다(새 빌드 파드의 Worker 동작 remediation-resume 이 부른다): 상태를 읽고, 재개할 수 있는 상태가 아니면 ``StateConflict``,
    ``stop_requested`` 를 지우고 그래프를 ``next_node`` 부터 돌린다. 그래프 안의 예외는 FAILED 상태로 돌아온다. 시간 한도
    (``chain_budget_minutes``)는 이 호출부터 다시 잰다.

    Continue a RESUMABLE chained remediation (`StateConflict` otherwise)."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state = load_state(run_dir)
    if state is None:
        raise StateConflict("no chained remediation in this run directory")
    if not graph_mod.can_resume(state):
        raise StateConflict(f"chained remediation {state.id} cannot be resumed from {state.status}")
    try:
        budget = _Budget(cfg, jr)            # 재개한 이 파드에서 다시 처음부터 잰다
        comps = components if components is not None else graph_mod.default_components(cfg, journal=jr)
        state.stop_requested = False
        jr({"event": "remediation_resume", "remediation": state.id, "next_node": state.next_node})
        real_stop = _safe_stop(stop)
        return _drive(paths, state, cfg, comps, jr=jr, stop=budget.wrap(real_stop), budget=budget, real_stop=real_stop)
    except Exception as e:  # noqa: BLE001
        return _error(paths, jr, e, state.id, state)


def summary(state: RemediationState | None, run_dir: Path | str, *, skipped: dict[str, Any] | None = None) -> dict[str, Any]:
    """이관 보고서(``RunReport.remediation``)에 넣을 작은 JSON 요약: status(건너뛰면 "skipped"), reason, reasons, counts, 짧은 message,
    이관 실행 폴더 기준 파일 경로(report.md / report.json / state.json / delivery/result.zip 중 있는 것), 전달 ZIP 의 build_ok / scanned.

    A small JSON-able digest for the migration report."""
    if state is None:
        out: dict[str, Any] = {"status": "skipped", "dir": CHAIN_DIR}
        out.update({k: v for k, v in (skipped or {}).items() if k in ("reason", "missing")})
        return out
    paths = chain_paths(run_dir)
    files: dict[str, str] = {}
    for key, p in (("report_md", paths.report_md), ("report_json", paths.report_json), ("state", paths.state),
                   ("delivery_zip", paths.delivery_zip)):
        try:
            if p.is_file():
                files[key] = f"{CHAIN_DIR}/{p.relative_to(paths.root).as_posix()}"
        except OSError:
            continue
    out = {"status": state.status, "reason": state.reason, "reasons": list(state.reasons or [])[:10],
           "counts": dict(state.counts or {}), "message": graph_mod.scrub_message(state.message or "", paths.root, 300),
           "dir": CHAIN_DIR, "files": files}
    if state.delivery is not None:
        out["delivery"] = {"build_ok": bool(state.delivery.build_ok), "scanned": bool(state.delivery.scanned),
                           "sha256": state.delivery.sha256}
    return out
