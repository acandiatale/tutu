"""취약점 조치에서 버전을 올린 수정본의 빌드가 실패했을 때 이관의 수정 루프(``BuildFixLoop``)로 소스를 고치는 모듈.
수정본 안의 프로젝트 루트를 잠시 git 저장소로 만들고 루프를 실행한 뒤, 고쳤으면 ``fix.patch``를 남기고 고치지 못했으면
트리를 루프 전 바이트로 되돌린다. 어느 경우든 ``.git``은 지우고 루트 ``.gitignore``는 원래대로 돌려놓는다.
- pom.xml 수정, 업로드한 파일 목록 밖의 경로, 파일 삭제는 ``edit_guard``가 시도마다 거절하고 끝에서 한 번 더 확인한다
- KB의 저장된 스크립트는 쓰지 않고 이 루프에서 배운 것도 KB에 기록하지 않는다(``NullKnowledgeBase``)
- 루프의 저널과 로그는 ``rounds/<r>/fixloop/``에 남고 실행 전체의 저널에는 쓰지 않는다

The build fix loop on a failed remediation candidate (docs/INTERFACES.md "v2.1z-bi", part B): `run_fix_loop` and the
`Fixer` the graph reaches through `Components.fixer` (`RemediationFixer`). Paths are relative to the project root
inside the candidate (the loop's repository root).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .models import BuildOutcome, FixResult, FixUsage, Proposal

log = logging.getLogger("sbmig.remediations.fixer")

_clock = time.monotonic                 # 테스트에서 교체하는 시계
# 남은 시간이 이보다 짧으면 Maven 빌드 한 번도 끝내기 어려우므로 루프를 시작하지 않는다
MIN_LOOP_SECONDS = 60.0
GITIGNORE = ".gitignore"

R_STOP = "stop requested"
R_TIME = "time budget used up"


def plan_notes(applied: list[Proposal]) -> list[str]:
    """수정 루프의 프롬프트에 넣을 영어 안내 문단을 만든다. 올린 라이브러리 버전과 지켜야 할 규칙을 적는다.

    The one English paragraph the fix prompt gets (`plan_notes`): the raised versions and the rules of this loop."""
    bumps = "; ".join(f"{p.ga} {p.from_version} -> {p.to_version}" for p in applied) or "(none recorded)"
    return [("The build broke after a vulnerability remediation raised these library versions: " + bumps + ". "
             "Fix the project's source code so that it compiles against the new versions. "
             "pom.xml must not be edited, and no file may be created or deleted: change existing source files only.")]


def check_paths(root: Path, touched: list[str], manifest_paths: set[str]) -> str | None:
    """바뀐 경로 목록을 규칙에 비춰 보고, 어긴 경로가 있으면 그 경로를 적은 영어 사유를 반환한다. 없으면 None이다.

    Rule 4 of the contract: a deleted path, a path named `pom.xml`, a path not in the manifest each reject (the first
    violation's sentence is returned, naming the path)."""
    for raw in sorted(dict.fromkeys(touched)):
        p = str(raw).replace("\\", "/").strip("/")
        if not p:
            continue
        if not (Path(root) / p).exists():
            return f"{p} must not be deleted"
        if p.rsplit("/", 1)[-1] == "pom.xml":
            return f"pom.xml must not be edited: {p}"
        if p not in manifest_paths:
            return f"{p} is not a file of the uploaded project"
    return None


def make_edit_guard(manifest_paths: list[str]) -> Callable[[Any, str, list[str]], str | None]:
    """``BuildFixLoop``의 ``edit_guard``로 넘길 함수를 만든다. 시도가 바꾼 경로를 ``check_paths``로 확인한다.

    The loop's `edit_guard(ws, pre_head, touched_paths)` over `check_paths`."""
    allowed = {str(p).replace("\\", "/").strip("/") for p in manifest_paths}

    def guard(ws: Any, pre_head: str, touched: list[str]) -> str | None:
        return check_paths(Path(ws.root), list(touched), allowed)
    return guard


def _snapshot_files(root: Path) -> set[str]:
    """루프 전에 있던 파일 경로(프로젝트 루트 기준)를 모은다. ``.git``과 빌드 산출물은 뺀다."""
    from sbmig.core.workspace import is_build_output_path
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if d != ".git"
                       and not is_build_output_path(f"{rel_dir}/{d}" if rel_dir else d, root)]
        for f in filenames:
            out.add(f"{rel_dir}/{f}" if rel_dir else f)
    return out


def _drop_new_files(root: Path, before: set[str]) -> None:
    """루프가 새로 만든 파일(빌드 산출물 밖)과 그래서 생긴 빈 폴더를 지운다."""
    now = _snapshot_files(root)
    for rel in sorted(now - before):
        try:
            (root / rel).unlink()
        except OSError:
            log.debug("leftover %s not removed", rel, exc_info=True)
    dirs_before = {str(Path(p).parent.as_posix()) for p in before}
    for rel in sorted({str(Path(p).parent.as_posix()) for p in now - before}, key=len, reverse=True):
        d = root / rel
        while rel not in ("", ".") and rel not in dirs_before and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            rel = str(Path(rel).parent.as_posix())
            d = root / rel


def _restore(root: Path, gitignore: bytes | None, before: set[str]) -> None:
    """``.git``을 지우고 루트 ``.gitignore``를 루프 전 바이트(없었으면 없는 상태)로 돌려놓은 뒤 남은 파일을 정리한다."""
    git = root / ".git"
    if git.is_symlink() or git.is_file():
        git.unlink()
    elif git.exists():
        shutil.rmtree(git)
    ig = root / GITIGNORE
    if gitignore is None:
        if ig.exists() or ig.is_symlink():
            ig.unlink()
    else:
        ig.write_bytes(gitignore)
    _drop_new_files(root, before)


def _usage(results: list[Any], budget_calls: int, cfg: Any = None) -> tuple[int, int, int, list[str], list[FixUsage], str]:
    """루프 결과의 시도들에서 LLM 사용량을 모델별로 모으고 합계, 요청한 모델 이름, 모델별 행, 통화를 반환한다.

    Per-model usage of the loop's attempts, summed the way the orchestrator sums the migration's
    (`Orchestrator._usage_by_model`): the row name is `asked_model_name(usage.model, attempt.model, llm.model)`,
    `served` merged without the asked name. Calls the loop's budget counted beyond the attempts' usage go to the
    configured model's row (no tokens), so the rows always add up to `llm_calls`. Each row is priced with
    `cost.compute_llm_cost` under the run's `llm.pricing` (None = no price)."""
    from sbmig.core.models import LLMUsage, asked_model_name, served_beside
    default = getattr(getattr(cfg, "llm", None), "model", "") if cfg is not None else ""
    by: dict[str, LLMUsage] = {}
    for pr in results:
        for a in pr.attempts:
            u = a.llm_usage
            if not (u.calls or u.input_tokens or u.output_tokens):
                continue
            key = asked_model_name(u.model, a.model, default)
            by[key] = by.get(key, LLMUsage(model=key)).add(
                u.model_copy(update={"model": key, "served": served_beside(key, u.served)}))
    counted = sum(int(u.calls or 0) for u in by.values())
    extra = int(budget_calls or 0) - counted
    if extra > 0:
        key = asked_model_name(default)
        by[key] = by.get(key, LLMUsage(model=key)).add(LLMUsage(model=key, calls=extra))
    rows = list(by.values())
    costs: dict[str, float | None] = {}
    currency = ""
    if cfg is not None and rows:
        try:
            from sbmig.report.cost import compute_llm_cost
            priced = compute_llm_cost(rows, cfg)
            costs = {c.model: c.cost for c in priced.by_model}
            if any(c is not None for c in costs.values()):
                currency = priced.currency
        except Exception:  # 단가를 계산하지 못해도 사용량은 남긴다
            log.debug("fix loop usage not priced", exc_info=True)
    usage = [FixUsage(model=u.model, served=list(u.served), calls=int(u.calls or 0),
                      input_tokens=int(u.input_tokens or 0), output_tokens=int(u.output_tokens or 0),
                      cost=costs.get(u.model)) for u in rows]
    return (sum(r.calls for r in usage), sum(r.input_tokens for r in usage), sum(r.output_tokens for r in usage),
            [r.model for r in usage], usage, currency)


def fix_usage_rows(fixes: Iterable[Any]) -> list[dict[str, Any]]:
    """여러 라운드의 수정 루프 결과(``FixResult`` 또는 그 dict)를 모델별 사용량 행으로 합친다.
    모델별 행이 없는 예전 결과는 합계를 첫 모델 이름의 행 하나로 만들고 비용은 모르는 것으로 둔다.

    Per-model rows `{model, served, calls, input_tokens, output_tokens, cost, currency}` of several rounds' fix loops
    (rounds that were not fixed count too). `cost` is None when any part of the row has calls or tokens and no price;
    `currency` is "" when no part was priced. A FixResult written before `usage` existed becomes one row of its totals
    named `asked_model_name(*models)`, unpriced."""
    from sbmig.core.models import asked_model_name, merge_model_names
    rows: dict[str, dict[str, Any]] = {}

    def add(model: str, served: list[str], calls: int, inp: int, out: int, cost: float | None, currency: str) -> None:
        if not (calls or inp or out):
            return
        r = rows.setdefault(model.lower(), {"model": model, "served": [], "calls": 0, "input_tokens": 0,
                                            "output_tokens": 0, "cost": 0.0, "currency": ""})
        r["served"] = [n for n in merge_model_names(r["served"], served) if n.lower() != model.lower()]
        r["calls"] += calls
        r["input_tokens"] += inp
        r["output_tokens"] += out
        if cost is None or r["cost"] is None or (currency and r["currency"] and currency != r["currency"]):
            r["cost"] = None
        else:
            r["cost"] = round(r["cost"] + cost, 6)
            r["currency"] = r["currency"] or currency

    for f in fixes:
        if f is None:
            continue
        fx = f if isinstance(f, FixResult) else FixResult.from_dict(f) if isinstance(f, dict) else None
        if fx is None:
            continue
        if fx.usage:
            for u in fx.usage:
                if not isinstance(u, FixUsage):
                    continue
                add(asked_model_name(u.model), list(u.served or []), int(u.calls or 0), int(u.input_tokens or 0),
                    int(u.output_tokens or 0), u.cost if isinstance(u.cost, (int, float)) else None, fx.currency or "")
        else:
            add(asked_model_name(*(fx.models or [])), [], int(fx.llm_calls or 0), int(fx.input_tokens or 0),
                int(fx.output_tokens or 0), None, "")
    return list(rows.values())


def fix_usage(state: Any) -> dict[str, Any]:
    """조치 상태(``RemediationState`` 또는 state.json의 dict)에서 모든 라운드의 수정 루프 LLM 사용량을 모은다.
    결과 줄, 실행 상태, 관리자 페이지가 이 함수 하나로 조치의 호출 수를 센다.

    The remediation's fix-loop usage over every round of a state: `{calls, input_tokens, output_tokens, by_model}`
    (`by_model` = `fix_usage_rows`). The one place the result line, `GET /api/runs/{id}` and the admin page read the
    remediation's LLM calls from (beside the migration's `report.json`, never written into it). Never raises; an
    unreadable state counts nothing."""
    try:
        if isinstance(state, dict):
            fixes = [r.get("fix") for r in (state.get("rounds") or []) if isinstance(r, dict)]
        else:
            fixes = [getattr(r, "fix", None) for r in (getattr(state, "rounds", None) or [])]
        rows = fix_usage_rows(fixes)
    except Exception:  # 사용량을 읽지 못해도 호출한 쪽은 계속한다
        log.debug("remediation fix usage unreadable", exc_info=True)
        rows = []
    return {"calls": sum(r["calls"] for r in rows), "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows), "by_model": rows}


def run_fix_usage(run_dir: Path | str) -> dict[str, Any]:
    """실행 폴더의 ``remediation/state.json``을 읽어 ``fix_usage``를 반환한다. 파일이 없거나 읽을 수 없으면 0이다.

    `fix_usage` of `<run dir>/remediation/state.json` (zero when missing or unreadable)."""
    import json

    from .models import RunPaths
    try:
        with open(RunPaths(Path(run_dir) / "remediation").state, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    return fix_usage(data if isinstance(data, dict) else {})


def run_fix_loop(cfg: Any, tree: Path, work_dir: Path, *, run_id: str, targets: list[str], applied: list[Proposal],
                 manifest_paths: list[str], first_outcome: BuildOutcome | None, stop: Callable[[], bool],
                 deadline: float | None, journal: Callable[[dict[str, Any]], None] | None = None,
                 patch_file: Path | None = None, build_runner: Callable[[Any], Any] | None = None,
                 llm_fixer: Any = None, script_runner: Any = None, edit_applier: Any = None,
                 profile: Any = None) -> FixResult:
    """수정본의 프로젝트 루트 ``tree``에서 빌드 오류 수정 루프를 실행하고 결과를 반환한다. 예외를 던지지 않는다.

    `tree` = the project root inside the candidate; `work_dir` = `rounds/<r>/fixloop/` (the loop's journal and logs);
    `targets` = the remediation's build targets ([] = one build at the root); `manifest_paths` relative to `tree`;
    `first_outcome` = the failed candidate build (logged only: the loop builds once itself, see the module notes);
    `deadline` = a `time.monotonic()` instant or None; `patch_file` = where the unified diff goes when fixed (default
    `work_dir.parent / "fix.patch"`; `FixResult.patch` is that absolute path, the graph makes it run-relative).
    The keyword-only parts after it replace the real ones (tests): `build_runner(ws)` → a build runner, `llm_fixer`,
    `script_runner`, `edit_applier`, `profile`."""
    tree = Path(tree).resolve()
    work_dir = Path(work_dir)
    patch_file = Path(patch_file) if patch_file is not None else work_dir.parent / "fix.patch"
    res = FixResult()

    def time_left() -> float | None:
        return None if deadline is None else deadline - _clock()

    def stopped() -> bool:
        try:
            return bool(stop())
        except Exception:  # noqa: BLE001 - 읽을 수 없는 중지 표시는 중지 요청으로 보지 않는다
            return False

    left = time_left()
    if left is not None and left < MIN_LOOP_SECONDS:
        res.reason = R_TIME
        return res
    if stopped():
        res.reason = R_STOP
        return res

    res.attempted = True
    ig = tree / GITIGNORE
    gitignore = ig.read_bytes() if ig.is_file() else None
    if (tree / ".git").exists():
        # 수정본은 manifest 파일만 복사한 트리라 .git이 있을 수 없다. 남은 것이 있으면 손대지 않고 끝낸다
        res.reason = "error: RuntimeError: the candidate tree already holds a .git directory"
        return res
    before = _snapshot_files(tree)
    ws = None
    base = ""
    try:
        from sbmig.core.models import Phase
        from sbmig.core.workspace import Workspace
        from sbmig.loop.budget import LoopBudget
        from sbmig.loop.components import LazyFixer, LazyScriptRunner, NullKnowledgeBase, lazy_attr
        from sbmig.loop.fixloop import BuildFixLoop

        work_dir.mkdir(parents=True, exist_ok=True)
        ws = Workspace(tree, work_dir, run_id)

        def ws_stop() -> bool:
            t = time_left()
            return stopped() or (t is not None and t <= 0)
        ws.stop_requested = ws_stop                    # type: ignore[method-assign]
        ws.git_init()
        # 프로젝트의 .gitignore가 무시하는 업로드 파일도 추적해야 되돌리기와 경로 확인이 그 파일을 본다
        base = ws.commit("sbmig: uploaded files", paths=[p for p in manifest_paths if (tree / p).is_file()])

        if profile is None:
            from sbmig.analyze.analyzer import analyze_project
            profile = analyze_project(ws, cfg)
        runner = build_runner(ws) if build_runner is not None else \
            lazy_attr("sbmig.build", "MavenRunner", "C (build)")(ws, cfg)
        from sbmig.triage import Triager
        fixer = llm_fixer if llm_fixer is not None else LazyFixer(
            lambda: lazy_attr("sbmig.fix", "LLMFixer", "D (fix)")(
                cfg, lazy_attr("sbmig.fix", "make_llm_client", "D (fix)")(cfg)))
        scripts = script_runner if script_runner is not None else LazyScriptRunner(
            lambda: lazy_attr("sbmig.script", "ScriptRunner", "S (script)")(cfg))
        budget = LoopBudget.from_config(cfg, clock=_clock)
        t = time_left()
        if t is not None:
            minutes = t / 60.0
            budget.max_minutes = min(budget.max_minutes, minutes) if budget.max_minutes > 0 else minutes
        kw: dict[str, Any] = {}
        if edit_applier is not None:
            kw["edit_applier"] = edit_applier
        loop = BuildFixLoop(cfg, ws, runner, Triager(cfg), fixer, NullKnowledgeBase(), profile,
                            plan_notes=plan_notes(applied), budget=budget, script_runner=scripts,
                            edit_guard=make_edit_guard(manifest_paths), **kw)
        ws.journal({"event": "remediation_fix_loop_start", "targets": list(targets) or [""],
                    "first_build_log": getattr(first_outcome, "log", "") if first_outcome is not None else "",
                    "max_iterations": int(cfg.remediation.fix_loop_max_iterations)})
        results: list[Any] = []
        all_ok = True
        for target in (list(targets) or [None]):
            pr = loop.run(Phase.VERIFY, "clean package", int(cfg.remediation.fix_loop_max_iterations), target=target)
            results.append(pr)
            if not pr.ok:
                all_ok = False
                break
        res.iterations = sum(int(pr.iterations or 0) for pr in results)
        res.attempts = sum(len(pr.attempts) for pr in results)
        (res.llm_calls, res.input_tokens, res.output_tokens, res.models, res.usage,
         res.currency) = _usage(results, loop.budget.llm_calls, cfg)
        res.remaining = len(results[-1].remaining_errors) if results and not all_ok else 0

        head = ws.head()
        changed = sorted(p for p in ws.changed_files_since(base) if p != GITIGNORE)
        violation = check_paths(tree, changed, {str(p).replace("\\", "/").strip("/") for p in manifest_paths}) \
            if all_ok else None
        if all_ok and violation is None:
            text = ws.diff(base, head) if head != base else ""
            patch_file.parent.mkdir(parents=True, exist_ok=True)
            patch_file.write_text(text, encoding="utf-8")
            res.fixed, res.changed, res.patch = True, changed, str(patch_file)
            res.reason = ""
        else:
            if violation is not None:
                res.reason = f"rejected: {violation}"
                if journal is not None:
                    journal({"event": "remediation_fix_rejected", "reason": violation})
            elif stopped():
                res.reason = R_STOP
            elif (time_left() is not None and time_left() <= 0) or loop.budget.time_exceeded():
                res.reason = R_TIME
            elif loop.llm_unavailable:
                res.reason = f"llm unavailable: {str(loop.llm_unavailable)[:300]}"
            else:
                res.reason = f"not fixed: {res.remaining} error(s) left"
            ws.revert_to(base)
    except Exception as e:
        log.warning("remediation fix loop failed: %s: %s", type(e).__name__, e, exc_info=True)
        from .graph import scrub_message
        res.fixed, res.changed, res.patch = False, [], ""
        res.reason = scrub_message(f"error: {type(e).__name__}: {e}", tree, 500)
        if ws is not None and base:
            try:
                ws.revert_to(base)
            except Exception:
                log.warning("remediation fix loop: reset to the base commit failed", exc_info=True)
        if patch_file.exists():
            patch_file.unlink()
    finally:
        try:
            _restore(tree, gitignore, before)
        except Exception as e:
            log.warning("remediation fix loop: cleanup failed: %s", e, exc_info=True)
            if res.fixed or not res.reason.startswith("error:"):
                res.fixed, res.changed, res.patch = False, [], ""
                res.reason = f"error: {type(e).__name__}: cleanup failed"
    log.info("remediation fix loop: fixed=%s iterations=%d attempts=%d llm_calls=%d changed=%d%s", res.fixed,
             res.iterations, res.attempts, res.llm_calls, len(res.changed), f" ({res.reason})" if res.reason else "")
    return res


class RemediationFixer:
    """그래프가 ``Components.fixer``로 호출하는 실제 구현. ``run_fix_loop``를 그대로 호출한다.

    The real `Fixer`: delegates to `run_fix_loop` with the real parts."""

    def run(self, cfg: Any, tree: Path, work_dir: Path, *, run_id: str, targets: list[str], applied: list[Proposal],
            manifest_paths: list[str], first_outcome: BuildOutcome | None, stop: Callable[[], bool],
            deadline: float | None, journal: Callable[[dict[str, Any]], None] | None = None) -> FixResult:
        """수정본의 프로젝트 루트에서 빌드 오류 수정 루프를 실행한다."""
        return run_fix_loop(cfg, tree, work_dir, run_id=run_id, targets=targets, applied=applied,
                            manifest_paths=manifest_paths, first_outcome=first_outcome, stop=stop, deadline=deadline,
                            journal=journal)


__all__ = ["MIN_LOOP_SECONDS", "RemediationFixer", "check_paths", "fix_usage", "fix_usage_rows", "make_edit_guard",
           "plan_notes", "run_fix_loop", "run_fix_usage"]
