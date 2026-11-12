"""이관이 BUILD SUCCESS로 끝난 뒤 같은 빌드 파드에서 취약점 조치를 이어서 실행하는 모듈.
이관 그래프의 ``remediation`` 노드가 ``run_after_migration``을 호출하고, 이관 없이 조치만 하는 실행(``--flow
remediation``)은 ``run_only``를 호출한다.
- 조치 결과는 이관의 성공 여부, 종료 코드, 보고서 결과를 바꾸지 않으며 예외도 밖으로 던지지 않는다
- 실행 폴더는 ``<이관 실행 폴더>/remediation/``이고 작업 id는 이관 실행 id와 같다
- 이관이 끝난 프로젝트 트리를 ZIP으로 묶어 ``intake.accept_zip``에 넣는다.
  ``remediation.trusted_projects``는 확인하지 않는다
- Maven 로컬 저장소는 이관과 같은 것을 쓴다
- 빌드 파드는 승인을 기다리지 않는다. AWAITING_APPROVAL / AWAITING_CONFIRMATION 상태를 저장하고 끝나며,
  재개할 때는 새 빌드 파드가 ``resume``을 호출한다
- ``remediation.chain_budget_minutes``를 넘기면 STOPPED(사유 ``chain_budget``)로 멈추고 나중에 재개할 수 있다
- ``run_only``는 준비가 안 됐으면 상태를 만들지 않고 ``RemediationNotReady``를 던진다

Chained remediation: runs in the build pod after a migration that ended in BUILD SUCCESS. The migration graph's
`remediation` node calls `run_after_migration`; the remediation graph stays a separate feature. Its outcome never changes
the migration's verdict and no exception leaves this module's entry points. `run_only` is the entry point of the
`remediation` run flow (no migration): same files, state id and driver; the only exception it raises is
`RemediationNotReady` (no state was created).
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
# 시간 한도를 넘겨 STOPPED가 될 때의 사유 코드.
# 이관에 이어 실행하는 조치에만 쓰이므로 models가 아니라 여기에 둔다
R_CHAIN_BUDGET = "chain_budget"
_monotonic = time.monotonic               # 테스트에서 교체하는 시계
SCOPE = "remediation"                     # 이관 저널에서 취약점 조치가 남긴 줄을 구분하는 값
SOURCE_NAME = "source.zip"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)          # ZIP 내용이 매번 같도록 시각을 고정한다
# 프로젝트 폴더 안의 sbmig 작업 파일과 VCS 폴더는 조치에 넘길 ZIP에 넣지 않는다
EXCLUDED_DIRS = (".git", ".sbmig")
_MAVEN_OUTPUT_MARKERS = ("maven-status", "maven-archiver")


def chain_paths(run_dir: Path | str) -> RunPaths:
    """이관 실행 폴더 안에 있는 조치 실행 폴더 ``<run_dir>/remediation``의 경로들을 반환한다.

    The `RunPaths` of the chained remediation inside a migration run directory."""
    return RunPaths(Path(run_dir) / CHAIN_DIR)


def load_state(run_dir: Path | str) -> RemediationState | None:
    """이관에 이어 실행한 조치의 ``state.json``을 읽어 반환한다.
    파일이 없거나 읽을 수 없으면 None을 반환하고 예외를 던지지 않는다.

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
    """취약점 조치를 시작할 수 없게 하는 문제 목록을 반환한다. 빈 목록이면 시작할 수 있다.
    빠진 IVAS 환경 변수 이름과 Maven / JDK / settings.xml 문제만 담고 값은 넣지 않는다.
    ``components``가 None이면 운영 구현으로 확인한다.

    Problems that prevent a chained remediation (IVAS env names + Maven readiness; never values; no trusted_projects)."""
    from . import ivas as ivas_mod
    problems = [_problem(p) for p in ivas_mod.readiness(ivas_mod.ivas_settings(cfg))]
    try:
        comps = components if components is not None else graph_mod.default_components(cfg)
        problems += [_problem(p) for p in comps.maven.readiness()]
    except Exception as e:  # noqa: BLE001 - 구성 요소를 만들지 못한 것 자체를 준비 문제로 본다
        problems.append(f"remediation components cannot be built ({type(e).__name__})")
    return problems


def _is_build_output(dirpath: Path, rel_parts: tuple[str, ...], filenames: Iterable[str]) -> bool:
    """``dirpath/target`` 폴더가 Maven 빌드 산출물인지 판단한다.
    src 아래면 산출물이 아니고, 옆에 pom.xml이 있거나 안에 Maven이 남기는 폴더가 있으면 산출물이다.
    패키지 이름이 target인 소스를 빠뜨리지 않도록 ``Workspace.walk``, result.zip과 같은 규칙을 쓴다."""
    if "src" in rel_parts:
        return False
    return "pom.xml" in filenames or any((dirpath / "target" / m).is_dir() for m in _MAVEN_OUTPUT_MARKERS)


def source_zip(project_dir: Path | str, out_zip: Path | str, *, exclude_dirs: Iterable[str] = (),
               journal: Journal | None = None) -> None:
    """이관이 끝난 프로젝트 트리를 ZIP으로 묶는다. 일반 파일만 이름 순으로 넣고 시각과 권한을 고정해서 매번
    같은 바이트가 나오게 하며, 루트 pom.xml은 ZIP 최상위에 둔다.
    ``.git/``, ``.sbmig/``, pom.xml 옆의 ``target/``, ``exclude_dirs``는 뺀다.
    심볼릭 링크는 접수에서 거부되므로 넣지 않고 저널에 ``remediation_symlink_skipped``를 남긴다.

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
    """이관 저널에 기록하는 줄마다 ``"scope": "remediation"``을 붙이는 저널 함수를 반환한다.
    저널 기록 실패는 무시한다."""
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
        except Exception:  # noqa: BLE001 - 읽을 수 없는 중지 표시는 중지 요청으로 보지 않는다
            return False
    return check


class _Budget:
    """이번 호출의 시간 한도(분 단위, 0 이하면 한도 없음)를 관리하는 클래스.
    ``wrap(stop)``은 실제 중지 요청이 있거나 한도를 넘으면 참을 반환하는 중지 확인 함수를 만든다.
    한도를 처음 넘을 때 저널 ``remediation_budget``을 한 번 기록한다.

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
    """시간 한도 때문에 STOPPED가 된 상태의 사유를 ``chain_budget``으로 바꿔 저장한다.
    실제 중지 요청이 있었으면 사유를 그대로 둔다.
    화면에서 시간 한도로 멈췄으며 재개하면 이어서 진행한다고 안내하기 위해서다."""
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
    """잡은 예외를 저널 ``remediation_error``와 로그(비밀 값을 마스킹한 traceback)에 남기고 FAILED 상태로 저장한다."""
    msg = graph_mod.scrub_message(f"{type(e).__name__}: {e}", paths.root, 500)
    jr({"event": "remediation_error", "type": type(e).__name__, "message": msg})
    log.error("chained remediation failed: %s", graph_mod.scrub_message(traceback.format_exc(), paths.root, 20000))
    st = _failed_state(run_id, R_INTERNAL_ERROR, msg, base=state)
    _try_save(paths, st)
    return st


def clean_targets(targets: Iterable[str] | None) -> list[str]:
    """이관의 최종 빌드 대상 폴더 목록을 조치 상태에 넣을 형태로 정리한다.
    프로젝트 기준 상대 경로로 순서를 유지하고, 중복, 빈 값, 프로젝트 밖 경로는 뺀다.
    루트 하나뿐이면 빈 목록을 반환하며, 이때 조치는 루트에서 한 번 빌드한다.

    The migration's final-build target directories as stored in the state; [] when that is the root alone."""
    from .inventory import normalize_roots
    roots = normalize_roots([str(t) for t in (targets or []) if t is not None])
    return [] if roots == ["."] else roots


def _baseline_outcome(paths: RunPaths, state: RemediationState, jr: Journal, excluded: Iterable[str]) -> RemediationState:
    """같은 트리로 이관의 최종 빌드는 통과했는데 조치 전 빌드가 실패했으면 그 사실을 메시지 앞에 분명히 쓴다.
    같은 Maven 설정이라는 것, 비교할 두 로그, 조치가 빌드한 트리에서 빠진 것(.git / .sbmig / target 산출물 /
    접수에서 제외한 폴더)을 담고 저널 ``remediation_baseline_differs``를 남긴다. 이관 결과는 바꾸지 않는다."""
    from .models import R_BASELINE_BUILD_FAILED
    if state.status != FAILED or state.reason != R_BASELINE_BUILD_FAILED or "migration's final build passed" in (state.message or ""):
        return state
    left_out = ".git/, .sbmig/, target/ build output" + (f", ignored folders {', '.join(excluded)}" if excluded else "")
    where = (f" (build targets: {', '.join(state.build_targets)})" if state.build_targets else " (at the project root)")
    detail = state.message or ""
    state.message = ("the migration's final build passed on this same tree, but the remediation's baseline build failed"
                     f"{where} with the same Maven command base, settings.xml and local repository (only IVAS_* / LLM_* "
                     "variables are removed from its environment) — compare remediation/baseline/build.log with the "
                     "migration's final build log (logs/*-build-verify*.log); the remediation builds a copy "
                     f"(remediation/baseline/tree) without {left_out}"
                     + (f" | {detail}" if detail else ""))
    jr({"event": "remediation_baseline_differs", "remediation": state.id, "targets": list(state.build_targets or [])})
    log.warning("vulnerability remediation %s: the baseline build failed although the migration's final build passed on the "
                "same tree — see remediation/baseline/build.log", state.id)
    _try_save(paths, state)
    return state


_END_MESSAGE_CAP = 600


def _ended(jr: Journal, st: RemediationState, paths: RunPaths | None = None) -> None:
    jr({"event": "remediation_end", "remediation": st.id, "status": st.status, "reason": st.reason})
    text = " ".join(str(st.message or "").split())
    if text:
        # 회사 PC에서는 복사와 붙여넣기가 안 되므로 sbmig.log 한 줄만 보고도 실패 이유를 알 수 있게 한다
        msg = graph_mod.scrub_message(text, paths.root if paths is not None else None, _END_MESSAGE_CAP)
        msg = " ".join(msg.split())
        log.info("chained remediation %s: %s %s — %s", st.id, st.status, st.reason, msg)
    else:
        log.info("chained remediation %s: %s %s", st.id, st.status, st.reason)


def run_after_migration(run_dir: Path | str, project_dir: Path | str, cfg: Any, *, run_id: str, journal: Journal | None,
                        stop: StopCheck | None, components: Components | None = None,
                        exclude_dirs: Iterable[str] = (), build_targets: Iterable[str] = ()) -> RemediationState | None:
    """빌드에 성공한 이관 뒤에 취약점 조치를 한 번 실행하고 마지막 상태를 반환한다. 예외를 던지지 않는다.
    - None을 반환하면 건너뛴 것이다. 기능이 꺼져 있으면 조용히 건너뛰고, 준비가 안 됐으면 저널
      ``remediation_skipped``와 WARNING 한 줄을 남긴다. 조치 상태가 이미 있으면 새로 시작하지 않고 저널
      ``remediation_exists``를 남긴 뒤 그 상태를 반환한다
    - 접수에서 거부되면 FAILED(사유는 거부 코드), 다른 예외면 FAILED internal_error로 저장한다
    - ``build_targets``는 이관의 최종 빌드 대상이며 조치도 같은 대상을 같은 방식으로 빌드한다

    One remediation after a successful migration. None = skipped (disabled / not ready); an existing chained state is
    returned untouched; otherwise the final state. Never raises."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state: RemediationState | None = None
    try:
        budget = _Budget(cfg, jr)            # 시간 한도는 이 호출이 시작된 시점부터 잰다
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
                      exclude_dirs=exclude_dirs, budget=budget, real_stop=real_stop, build_targets=build_targets)
    except Exception as e:  # noqa: BLE001 — the migration's verdict must never depend on the remediation
        return _error(paths, jr, e, run_id, state)


def _start(paths: RunPaths, project_dir: Path, cfg: Any, comps: Components, *, run_id: str, jr: Journal,
           stop: StopCheck, exclude_dirs: Iterable[str], budget: _Budget | None = None,
           real_stop: StopCheck | None = None, build_targets: Iterable[str] = ()) -> RemediationState:
    from . import intake as intake_mod
    from . import planner as planner_mod
    from .models import IntakeRejected
    if paths.root.exists():
        # 상태 파일 없이 남은 폴더는 접수 중에 중단된 이전 시도다.
        # IVAS에 보낸 기록은 상태가 생긴 뒤에만 있으므로 지우고 다시 시작한다
        import shutil
        shutil.rmtree(paths.root)
    paths.root.mkdir(parents=True)
    policy = planner_mod.policy_hash(cfg)
    jr({"event": "remediation_start", "remediation": run_id})
    log.info("vulnerability remediation %s: start (after the migration's BUILD SUCCESS)", run_id)
    targets = clean_targets(build_targets)
    excluded = sorted({str(d).replace("\\", "/").strip("/") for d in exclude_dirs if str(d).strip("/")})
    if targets:
        jr({"event": "remediation_build_targets", "targets": targets})
        log.info("vulnerability remediation %s: building the migration's targets %s", run_id, ", ".join(targets))
    source_zip(project_dir, paths.original_zip, exclude_dirs=excluded, journal=jr)
    try:
        result = intake_mod.accept_zip(paths.original_zip, paths.root, intake_mod.limits_from_config(cfg),
                                       **({"build_targets": targets} if targets else {}))
    except IntakeRejected as e:
        st = _failed_state(run_id, e.code, graph_mod.scrub_message(str(e), paths.root, 500), policy)
        _try_save(paths, st)
        jr({"event": "remediation_intake_rejected", "remediation": run_id, "code": e.code})
        log.warning("vulnerability remediation %s: intake rejected the migrated tree (%s)", run_id, e.code)
        _ended(jr, st, paths)
        return st
    state = graph_mod.new_state(run_id, SOURCE_NAME, cfg, result, [], policy_hash=policy)
    state.build_targets = targets
    save = _saver(paths)
    save(state)
    return _drive(paths, state, cfg, comps, jr=jr, stop=stop, budget=budget, real_stop=real_stop, excluded=excluded)


class RemediationNotReady(Exception):
    """조치만 하는 실행(``run_only``)을 시작할 수 없을 때 던지는 예외.
    빠진 IVAS 환경 변수 이름과 Maven 준비 문제를 담고 값은 넣지 않으며, 이때 상태는 만들어지지 않는다.

    The remediation-only run cannot start (`missing` = problem sentences, names only); no state was created."""

    def __init__(self, missing: Iterable[str]):
        self.missing = [str(m) for m in missing]
        super().__init__("; ".join(self.missing) or "not ready")


def zip_targets(zip_path: Path | str, targets: Iterable[str]) -> list[str]:
    """프로젝트 루트 기준의 빌드 대상 폴더를 ZIP 최상위 기준 경로로 바꾼다.
    ZIP이 프로젝트를 폴더로 감쌌으면 모든 대상에 맞는 가장 짧은 앞부분을 붙인다.
    맞추지 못하거나 ZIP을 읽을 수 없으면 그대로 반환하며, 그 경우 ``accept_zip``이 ``no_pom``으로 거부한다.

    Re-root project-relative build targets onto the ZIP root (a wrapping folder is prefixed); unchanged when nothing fits."""
    tl = [str(t).strip("/") for t in targets if str(t).strip("/") not in ("", ".")]
    if not tl:
        return []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = {n.replace("\\", "/") for n in zf.namelist()}
    except (OSError, zipfile.BadZipFile, ValueError, EOFError):
        return tl
    if all(f"{t}/pom.xml" in names for t in tl):
        return tl
    tail = f"/{tl[0]}/pom.xml"
    prefixes = sorted({n[:-len(tail) + 1] for n in names if n.endswith(tail)}, key=lambda p: (len(p), p))
    for p in prefixes:
        if all(f"{p}{t}/pom.xml" in names for t in tl):
            return [f"{p}{t}" for t in tl]
    return tl


def run_only(run_dir: Path | str, source: Path | str, cfg: Any, *, run_id: str, journal: Journal | None,
             stop: StopCheck | None, components: Components | None = None,
             build_targets: Iterable[str] = ()) -> RemediationState:
    """이관 없이 취약점 조치만 한 번 실행한다(실행 방식 ``remediation``). ``source``가 ZIP 파일이면 그
    바이트를 그대로, 디렉토리면 ``source_zip``으로 묶어서 ``accept_zip``에 넣는다.
    - 준비가 안 됐으면 저널 ``remediation_skipped``를 남기고 상태를 만들지 않은 채 ``RemediationNotReady``를
      던진다. 던지는 예외는 이것뿐이다
    - 조치 상태가 이미 있으면 그대로 반환하고, 그 밖에는 이관에 이어 실행하는 조치와 같은 규칙으로 실행한다
    - ZIP이 폴더로 감싸여 있으면 ``build_targets``를 ``zip_targets``로 바꾼다

    One remediation without a migration. Raises only `RemediationNotReady` (nothing created); an existing state is
    returned untouched; otherwise the final state (intake rejection → FAILED, other errors → FAILED internal_error)."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    try:
        budget = _Budget(cfg, jr)
        existing = load_state(run_dir)
        if existing is not None:
            jr({"event": "remediation_exists", "remediation": existing.id, "status": existing.status})
            log.info("vulnerability remediation %s already exists (%s) — not started again", existing.id, existing.status)
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
            raise RemediationNotReady(missing)
        real_stop = _safe_stop(stop)
        return _start_only(paths, Path(source), cfg, comps, run_id=run_id, jr=jr, stop=budget.wrap(real_stop),
                           budget=budget, real_stop=real_stop, build_targets=build_targets)
    except RemediationNotReady:
        raise
    except Exception as e:  # noqa: BLE001 — a saved FAILED state, never a traceback out of here
        return _error(paths, jr, e, run_id, load_state(run_dir))


def _start_only(paths: RunPaths, source: Path, cfg: Any, comps: Components, *, run_id: str, jr: Journal,
                stop: StopCheck, budget: _Budget, real_stop: StopCheck,
                build_targets: Iterable[str]) -> RemediationState:
    from . import intake as intake_mod
    from . import planner as planner_mod
    from .models import IntakeRejected
    if paths.root.exists():
        # 상태 파일 없이 남은 폴더는 접수 중에 중단된 이전 시도다.
        # IVAS에 보낸 기록은 상태가 생긴 뒤에만 있으므로 지워도 된다
        import shutil
        shutil.rmtree(paths.root)
    paths.root.mkdir(parents=True)
    policy = planner_mod.policy_hash(cfg)
    jr({"event": "remediation_start", "remediation": run_id, "flow": "remediation"})
    log.info("vulnerability remediation %s: start (remediation only — the project as uploaded, no migration)", run_id)
    targets = clean_targets(build_targets)
    if source.is_dir():
        name = SOURCE_NAME
        source_zip(source, paths.original_zip, journal=jr)
        zip_in = paths.original_zip
    else:
        name = source.name or SOURCE_NAME
        targets = zip_targets(source, targets)
        zip_in = source
    if targets:
        jr({"event": "remediation_build_targets", "targets": targets})
        log.info("vulnerability remediation %s: building the targets %s (no root pom)", run_id, ", ".join(targets))
    try:
        result = intake_mod.accept_zip(zip_in, paths.root, intake_mod.limits_from_config(cfg),
                                       **({"build_targets": targets} if targets else {}))
    except IntakeRejected as e:
        st = _failed_state(run_id, e.code, graph_mod.scrub_message(str(e), paths.root, 500), policy)
        st.name = name
        _try_save(paths, st)
        jr({"event": "remediation_intake_rejected", "remediation": run_id, "code": e.code})
        log.warning("vulnerability remediation %s: intake rejected the uploaded project (%s)", run_id, e.code)
        _ended(jr, st, paths)
        return st
    state = graph_mod.new_state(run_id, name, cfg, result, [], policy_hash=policy)
    state.build_targets = targets
    _saver(paths)(state)
    return _drive(paths, state, cfg, comps, jr=jr, stop=stop, budget=budget, real_stop=real_stop, chained=False)


def _run_flow(run_dir: Path) -> str:
    """실행 폴더의 report.json에 기록된 실행 방식을 반환한다. 없거나 읽을 수 없으면 "full"을 반환한다.
    재개할 때 조치 전 빌드 실패 메시지를 고르는 데 쓴다."""
    try:
        data = read_json(Path(run_dir) / "report.json")
        return str(data.get("flow") or "full") if isinstance(data, dict) else "full"
    except Exception:  # noqa: BLE001 — an unreadable report means "the chained case", as before
        return "full"


def _drive(paths: RunPaths, state: RemediationState, cfg: Any, comps: Components, *, jr: Journal,
           stop: StopCheck, budget: _Budget | None = None, real_stop: StopCheck | None = None,
           excluded: Iterable[str] = (), chained: bool = True) -> RemediationState:
    save = _saver(paths)
    try:
        state = graph_mod.run_remediation(state, paths, cfg, comps, save=save, journal=jr, stop=stop)
    except Exception as e:  # noqa: BLE001 — run_remediation turns node errors into FAILED; this guards the rest
        return _error(paths, jr, e, state.id, state)
    if chained:                              # 이관 없이 실행한 조치에는 이관의 최종 빌드가 통과했다는 정보가 없다
        state = _baseline_outcome(paths, state, jr, excluded)
    if budget is not None:
        state = _budget_outcome(paths, state, budget, real_stop or (lambda: False))
    _ended(jr, state, paths)
    return state


def resume(run_dir: Path | str, cfg: Any, *, journal: Journal | None, stop: StopCheck | None,
           components: Components | None = None) -> RemediationState:
    """이관에 이어 실행한 취약점 조치를 재개한다.
    새 빌드 파드의 Worker가 remediation-resume 작업으로 호출한다.
    재개할 수 없는 상태면 ``StateConflict``를 던지고, 아니면 ``stop_requested``를 지우고 ``next_node``부터
    그래프를 실행한다. 시간 한도는 이 호출부터 다시 잰다.

    Continue a RESUMABLE chained remediation (`StateConflict` otherwise)."""
    paths = chain_paths(run_dir)
    jr = _scoped(journal)
    state = load_state(run_dir)
    if state is None:
        raise StateConflict("no chained remediation in this run directory")
    if not graph_mod.can_resume(state):
        raise StateConflict(f"chained remediation {state.id} cannot be resumed from {state.status}")
    try:
        budget = _Budget(cfg, jr)            # 재개한 이 파드에서 시간 한도를 처음부터 다시 잰다
        comps = components if components is not None else graph_mod.default_components(cfg, journal=jr)
        state.stop_requested = False
        jr({"event": "remediation_resume", "remediation": state.id, "next_node": state.next_node})
        real_stop = _safe_stop(stop)
        return _drive(paths, state, cfg, comps, jr=jr, stop=budget.wrap(real_stop), budget=budget, real_stop=real_stop,
                      chained=_run_flow(Path(run_dir)) != "remediation")
    except Exception as e:  # noqa: BLE001
        return _error(paths, jr, e, state.id, state)


def summary(state: RemediationState | None, run_dir: Path | str, *, skipped: dict[str, Any] | None = None) -> dict[str, Any]:
    """이관 보고서(``RunReport.remediation``)에 넣을 짧은 JSON 요약을 만든다.
    status(건너뛰었으면 "skipped"), reason, reasons, counts, 짧은 message, 결과 파일 경로, 결과 ZIP의
    build_ok / scanned를 담고, 조치 전 빌드를 실행했으면 ``baseline_build_ok``도 담는다.

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
    if state.baseline_build is not None:
        # 실행 방식이 remediation일 때 결과 줄의 build= 값.
        # 업로드한 그대로의 빌드가 실행되고 통과했는지를 나타낸다
        out["baseline_build_ok"] = bool(state.baseline_build.ok)
    return out
