"""취약점 조치에서 버전을 올린 수정본의 빌드가 실패했을 때 이관의 수정 루프(``BuildFixLoop``)로 소스를 고치는 모듈.
수정본은 읽기만 하고, 루프는 ``rounds/<r>/fixloop/tree/``에 만든 작업 복사본에서 실행한다. 고친 파일은 바이트를 비교해
찾고 새 바이트를 ``fixloop/fixed/``에 남기며, 수정본에 덮어쓰는 일은 그래프가 한다.
- pom, 숨김 경로, 실행 스크립트, 바이너리 묶음의 수정과 파일 추가, 삭제는 ``edit_guard``가 시도마다 거절하고 끝에서 한 번 더 확인한다
- KB의 저장된 스크립트는 쓰지 않고 이 루프에서 배운 것도 KB에 기록하지 않는다(``NullKnowledgeBase``)
- 루프의 저널과 로그는 ``rounds/<r>/fixloop/``에 남고 실행 전체의 저널에는 쓰지 않는다

The build fix loop on a failed remediation candidate (docs/INTERFACES.md "v2.1z-bi" part B, amended by "v2.1z-bj"):
`run_fix_loop` and the `Fixer` the graph reaches through `Components.fixer` (`RemediationFixer`). The candidate tree is
never written: the loop runs in a work copy of the loop paths, changed files are found by byte comparison, the fixed
bytes are stored durably in `fixloop/fixed/` and the graph overlays them. Paths are relative to the project root.
"""
from __future__ import annotations

import logging
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
R_LLM = "llm unavailable"


def plan_notes(applied: list[Proposal]) -> list[str]:
    """수정 루프의 프롬프트에 넣을 영어 안내 문단을 만든다. 올린 라이브러리 버전과 지켜야 할 규칙을 적는다.

    The one English paragraph the fix prompt gets (`plan_notes`): the raised versions and the rules of this loop."""
    bumps = "; ".join(f"{p.ga} {p.from_version} -> {p.to_version}" for p in applied) or "(none recorded)"
    return [("The build broke after a vulnerability remediation raised these library versions: " + bumps + ". "
             "Fix the project's source code so that it compiles against the new versions. "
             "pom.xml must not be edited, and no file may be created or deleted: change existing source files only.")]


# 사람이 편집하는 소스가 아니라 빌드나 실행에 쓰는 파일이다. 루프가 바꾸면 거절한다
_FIXED_NAMES = frozenset({"pom.xml", "mvnw", "mvnw.cmd"})
_FIXED_SUFFIXES = (".sh", ".bat", ".cmd", ".ps1", ".jar", ".war", ".ear", ".class", ".zip")


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").strip("/")


def loop_paths(manifest_paths: Iterable[str]) -> list[str]:
    """작업 복사본에 넣을 경로(``.git`` 경로 조각이 없는 manifest 경로)를 정렬해 반환한다.

    The *loop paths*: the manifest paths (project-relative) without a `.git` path segment (a ZIP may carry the
    project's own `.git/` or a nested checkout; git inside the work copy must see one repository only)."""
    out = {_norm(p) for p in manifest_paths}
    return sorted(p for p in out if p and ".git" not in p.split("/"))


def maven_pom_paths(project: Path, targets: Iterable[str] = ()) -> set[str]:
    """Maven이 이 빌드에서 읽는 pom 가운데 이름이 ``pom.xml``이 아닌 파일의 경로를 모은다. 예외를 던지지 않는다.

    Poms Maven reads for this build under another name: read with the inventory's pom reader (`ProjectPoms`, the
    reactor from the build targets) — a `<relativePath>` or `<module>` naming an `.xml` file other than `pom.xml`, and
    a build target that is a file. Project-relative paths; empty when nothing can be read."""
    out: set[str] = set()
    try:
        import posixpath

        from .inventory import ProjectPoms, _kid, _kids, _norm_dir, _text
        tl = [str(t) for t in targets if t]
        for t in tl:
            if t.endswith(".xml"):
                out.add(_norm(t))
        roots = [posixpath.dirname(_norm(t)) or "." if t.endswith(".xml") else t for t in tl]
        pp = ProjectPoms(Path(project), roots or None)
        for pf in pp.poms.values():
            if not pf.ok:
                continue
            refs: list[str] = []
            par = _kid(pf.root, "parent")
            rp = _text(_kid(par, "relativePath")) if par is not None else ""
            refs.append(rp)
            refs += [_text(m) for m in _kids(_kid(pf.root, "modules"), "module")]
            for prof in _kids(_kid(pf.root, "profiles"), "profile"):
                refs += [_text(m) for m in _kids(_kid(prof, "modules"), "module")]
            for ref in refs:
                ref = (ref or "").replace("\\", "/")
                if not ref.endswith(".xml") or posixpath.basename(ref) == "pom.xml":
                    continue
                d = _norm_dir(pf.dir, posixpath.dirname(ref) or ".")
                if d is not None:
                    out.add(posixpath.basename(ref) if d == "." else f"{d}/{posixpath.basename(ref)}")
    except Exception:  # noqa: BLE001 - 읽지 못하면 이름 규칙(pom.xml)만으로 판단한다
        log.debug("poms under another name not read", exc_info=True)
    return out


def is_editable(path: str, other_poms: set[str] | frozenset[str] = frozenset()) -> bool:
    """루프가 바꿔도 되는 경로인지 확인한다. pom, 숨김 경로, 실행 스크립트, 바이너리 묶음은 바꾸면 안 된다.

    Rule 2 of v2.1z-bj without the loop-path part: not a `pom.xml`, not a pom under another name, no segment
    starting with `.`, not `mvnw` / `mvnw.cmd` / a shell or batch script, not a jar / war / ear / class / zip file."""
    p = _norm(path)
    parts = p.split("/")
    name = parts[-1]
    if name in _FIXED_NAMES or p in other_poms:
        return False
    if any(seg.startswith(".") for seg in parts):
        return False
    return not name.lower().endswith(_FIXED_SUFFIXES)


def _not_editable_reason(p: str) -> str:
    return f"pom.xml must not be edited: {p}" if p.rsplit("/", 1)[-1] == "pom.xml" else f"{p} must not be edited"


def check_paths(root: Path, touched: list[str], loops: set[str], editable: set[str]) -> str | None:
    """바뀐 경로 목록을 규칙에 비춰 보고, 어긴 경로가 있으면 그 경로를 적은 영어 사유를 반환한다. 없으면 None이다.

    The edit guard's rules: a deleted path, a path that is not editable (`pom.xml must not be edited: <p>` for a
    pom.xml, `<p> must not be edited` otherwise), a path that is no loop path (`<p> is not a file of the uploaded
    project`); the first violation in path order is returned."""
    for raw in sorted(dict.fromkeys(touched)):
        p = _norm(raw)
        if not p:
            continue
        f = Path(root) / p
        if not f.exists() and not f.is_symlink():
            if p in loops:
                return f"{p} must not be deleted"
            continue                                  # 만들었다가 지운 파일은 남은 것이 없다
        if p.rsplit("/", 1)[-1] == "pom.xml":
            return f"pom.xml must not be edited: {p}"
        if p not in loops:
            return f"{p} is not a file of the uploaded project"
        if p not in editable or f.is_symlink():
            return _not_editable_reason(p)
    return None


def make_edit_guard(loops: Iterable[str], editable: Iterable[str]) -> Callable[[Any, str, list[str]], str | None]:
    """``BuildFixLoop``의 ``edit_guard``로 넘길 함수를 만든다. 시도가 바꾼 경로를 ``check_paths``로 확인한다.

    The loop's `edit_guard(ws, pre_head, touched_paths)` over `check_paths`."""
    lp = {_norm(p) for p in loops}
    ed = {_norm(p) for p in editable}

    def guard(ws: Any, pre_head: str, touched: list[str]) -> str | None:
        return check_paths(Path(ws.root), list(touched), lp, ed)
    return guard


def compare_trees(tree: Path, work: Path, loops: list[str], editable: set[str]) -> tuple[list[str], str | None]:
    """원래 트리와 작업 복사본의 바이트를 비교해 바뀐 편집 가능 경로와 첫 위반 사유를 반환한다.

    Rule 3: bytes of every loop path in `tree` against the work copy. An editable path that differs is changed; a
    missing loop path, a symlink, or a difference in a non-editable path other than the root `.gitignore` (git_init
    writes sbmig's lines into it) is a violation."""
    changed: list[str] = []
    for p in loops:
        a, b = Path(tree) / p, Path(work) / p
        if b.is_symlink():
            return changed, _not_editable_reason(p)
        if not b.is_file():
            return changed, f"{p} must not be deleted"
        if p == GITIGNORE:
            continue
        if a.read_bytes() == b.read_bytes():
            continue
        if p not in editable:
            return changed, _not_editable_reason(p)
        changed.append(p)
    return sorted(changed), None


def unified_patch(tree: Path, work: Path, changed: list[str]) -> str:
    """바뀐 파일의 두 바이트 판으로 통합 diff를 만든다. UTF-8이 아닌 파일은 ``Binary files differ``로만 적는다.

    `a/<path>` → `b/<path>`, project-relative, made with `difflib` from the two byte versions."""
    import difflib
    out: list[str] = []
    for p in changed:
        a, b = (Path(tree) / p).read_bytes(), (Path(work) / p).read_bytes()
        try:
            at, bt = a.decode("utf-8"), b.decode("utf-8")
        except UnicodeDecodeError:
            out.append(f"--- a/{p}\n+++ b/{p}\nBinary files differ\n")
            continue
        for line in difflib.unified_diff(at.splitlines(keepends=True), bt.splitlines(keepends=True),
                                         fromfile=f"a/{p}", tofile=f"b/{p}"):
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _rmtree_quiet(p: Path, what: str) -> None:
    """폴더를 지운다. 지우지 못하면 WARNING으로 기록하고 오류로 보지 않는다."""
    try:
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.exists():
            shutil.rmtree(p)
    except OSError as e:
        log.warning("remediation fix loop: %s not removed: %s", what, e)


class _DeadlineRunner:
    """빌드마다 시간 한도를 마감까지 남은 시간과 ``remediation.build_timeout_minutes`` 가운데 짧은 쪽으로 맞추는 래퍼."""

    def __init__(self, inner: Any, cap_s: float, time_left: Callable[[], float | None]):
        self.inner, self.cap_s, self.time_left = inner, cap_s, time_left
        self.timeouts: list[float] = []

    def run(self, goal: str, **kw: Any) -> Any:
        t = self.time_left()
        limit = self.cap_s if t is None else max(1.0, min(self.cap_s, t))
        self.timeouts.append(limit)
        if hasattr(self.inner, "timeout_s"):
            self.inner.timeout_s = limit
        return self.inner.run(goal, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


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

    The remediation's fix-loop usage over every round of a state (`rounds[].fix` and every discarded fix in
    `rounds[].fix_discarded`, fixed or not): `{calls, input_tokens, output_tokens, by_model}`
    (`by_model` = `fix_usage_rows`). The one place the result line, `GET /api/runs/{id}` and the admin page read the
    remediation's LLM calls from (beside the migration's `report.json`, never written into it). Never raises; an
    unreadable state counts nothing."""
    try:
        fixes: list[Any] = []
        if isinstance(state, dict):
            for r in (state.get("rounds") or []):
                if isinstance(r, dict):
                    fixes += [r.get("fix"), *(r.get("fix_discarded") or [])]
        else:
            for r in (getattr(state, "rounds", None) or []):
                fixes += [getattr(r, "fix", None), *(getattr(r, "fix_discarded", None) or [])]
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
    """수정본의 프로젝트 루트 ``tree``를 복사한 작업 폴더에서 빌드 오류 수정 루프를 실행하고 결과를 반환한다.
    ``tree``는 읽기만 하고, 고친 파일의 새 바이트는 ``work_dir/fixed/``에 둔다. 운영 중 실패는 예외 대신 결과로 반환한다.

    `tree` = the project root inside the candidate (never written); `work_dir` = `rounds/<r>/fixloop/` (the loop's
    journal and logs, the work copy `tree/`, the fixed bytes `fixed/`); `targets` = the remediation's build targets
    ([] = one build at the root); `manifest_paths` relative to `tree`; `first_outcome` = the failed candidate build
    (logged only: the loop builds once itself); `deadline` = a `time.monotonic()` instant or None; `patch_file` =
    where the unified diff goes when fixed with changes (default `work_dir.parent / "fix.patch"`). `FixResult.patch`
    and `FixResult.fixed_dir` are absolute paths here; the graph makes them run-relative. The keyword-only parts after
    it replace the real ones (tests): `build_runner(ws)` → a build runner, `llm_fixer`, `script_runner`,
    `edit_applier`, `profile`."""
    tree = Path(tree).resolve()
    work_dir = Path(work_dir)
    work = work_dir / "tree"
    fixed_root = work_dir / "fixed"
    patch_file = Path(patch_file) if patch_file is not None else work_dir.parent / "fix.patch"
    res = FixResult()

    def time_left() -> float | None:
        return None if deadline is None else deadline - _clock()

    def out_of_time() -> bool:
        t = time_left()
        return t is not None and t <= 0

    def stopped() -> bool:
        try:
            return bool(stop())
        except Exception:  # noqa: BLE001 - 읽을 수 없는 중지 표시는 중지 요청으로 보지 않는다
            return False

    def scrub(text: str) -> str:
        from .graph import scrub_message
        return scrub_message(scrub_message(text, work, 1000), work_dir, 500)

    left = time_left()
    if left is not None and left < MIN_LOOP_SECONDS:
        res.reason = R_TIME
        return res
    if stopped():
        res.reason = R_STOP
        return res

    res.attempted = True
    loops = loop_paths(manifest_paths)
    editable: set[str] = set()
    loop = None
    try:
        from sbmig.core.models import Phase
        from sbmig.core.workspace import Workspace
        from sbmig.loop.budget import LoopBudget
        from sbmig.loop.components import LazyFixer, LazyScriptRunner, NullKnowledgeBase, lazy_attr
        from sbmig.loop.fixloop import BuildFixLoop

        from .intake import copy_tree

        work_dir.mkdir(parents=True, exist_ok=True)
        for old in (work, fixed_root):
            if old.exists() or old.is_symlink():
                shutil.rmtree(old) if old.is_dir() and not old.is_symlink() else old.unlink()
        if patch_file.exists():
            patch_file.unlink()
        # 수정본은 읽기만 한다. 루프(git, 스크립트, 빌드)는 모두 이 작업 복사본에서 실행한다
        copy_tree(tree, work, loops)
        other_poms = maven_pom_paths(work, targets)
        editable = {p for p in loops if is_editable(p, other_poms)}

        ws = Workspace(work, work_dir, run_id)

        def ws_stop() -> bool:
            return out_of_time() or stopped()
        ws.stop_requested = ws_stop                    # type: ignore[method-assign]
        ws.git_init()
        # 프로젝트의 .gitignore가 무시하는 업로드 파일도 추적해야 경로 확인이 그 파일을 본다
        ws.commit("sbmig: uploaded files", paths=[p for p in loops if (work / p).is_file()])

        if profile is None:
            from sbmig.analyze.analyzer import analyze_project
            profile = analyze_project(ws, cfg)
        inner = build_runner(ws) if build_runner is not None else \
            lazy_attr("sbmig.build", "MavenRunner", "C (build)")(ws, cfg)
        cap_s = float(getattr(cfg.remediation, "build_timeout_minutes", 60) or 60) * 60.0
        runner = _DeadlineRunner(inner, cap_s, time_left)
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
                            edit_guard=make_edit_guard(loops, editable), **kw)
        ws.journal({"event": "remediation_fix_loop_start", "targets": list(targets) or [""],
                    "first_build_log": getattr(first_outcome, "log", "") if first_outcome is not None else "",
                    "max_iterations": int(cfg.remediation.fix_loop_max_iterations),
                    "loop_paths": len(loops), "editable": len(editable)})
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

        if all_ok:
            changed, violation = compare_trees(tree, work, loops, editable)
            if violation is not None:
                res.reason = scrub(f"rejected: {violation}")
                if journal is not None:
                    journal({"event": "remediation_fix_rejected", "reason": scrub(violation)})
            elif not changed:
                # 루프의 첫 빌드가 바꾼 것 없이 통과했다. 처음 빌드 실패는 일시적이었다
                res.fixed, res.retry_passed, res.reason = True, True, ""
            else:
                for p in changed:
                    dst = fixed_root / p
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes((work / p).read_bytes())
                patch_file.parent.mkdir(parents=True, exist_ok=True)
                patch_file.write_text(unified_patch(tree, work, changed), encoding="utf-8")
                res.fixed, res.changed, res.reason = True, changed, ""
                res.patch, res.fixed_dir = str(patch_file), str(fixed_root)
        # 마감이 지난 것을 중지 요청보다 먼저 본다. 실행 시간 한도의 중지 함수도 그때 참이 되기 때문이다
        elif out_of_time() or loop.budget.time_exceeded():
            res.reason = R_TIME
        elif stopped():
            res.reason = R_STOP
        elif loop.llm_unavailable:
            log.info("remediation fix loop: llm unavailable (details in %s)", work_dir / "journal.jsonl")
            ws.journal({"event": "remediation_fix_llm_unavailable", "detail": str(loop.llm_unavailable)[:300]})
            res.reason = R_LLM
        else:
            res.reason = f"not fixed: {res.remaining} error(s) left"
    except Exception as e:  # noqa: BLE001 - 수정 루프 안의 실패는 결과로 돌려준다. 수정본은 건드리지 않았다
        log.warning("remediation fix loop failed: %s: %s", type(e).__name__, e, exc_info=True)
        res.reason = scrub(f"error: {type(e).__name__}: {e}")
        res.fixed, res.changed, res.patch, res.fixed_dir, res.retry_passed = False, [], "", "", False
        if loop is not None and not res.llm_calls:
            try:
                (res.llm_calls, res.input_tokens, res.output_tokens, res.models, res.usage,
                 res.currency) = _usage([], loop.budget.llm_calls, cfg)
            except Exception:  # noqa: BLE001 - 사용량을 셀 수 없어도 결과는 돌려준다
                log.debug("fix loop usage not counted", exc_info=True)
    if not res.fixed or not res.changed:
        for leftover in (patch_file, fixed_root):
            _rmtree_quiet(leftover, leftover.name)
    _rmtree_quiet(work, "the work copy")
    log.info("remediation fix loop: fixed=%s iterations=%d attempts=%d llm_calls=%d changed=%d%s%s", res.fixed,
             res.iterations, res.attempts, res.llm_calls, len(res.changed), " retry passed" if res.retry_passed else "",
             f" ({res.reason})" if res.reason else "")
    return res


class RemediationFixer:
    """그래프가 ``Components.fixer``로 호출하는 실제 구현. ``run_fix_loop``를 그대로 호출한다.

    The real `Fixer`: delegates to `run_fix_loop` with the real parts."""

    def run(self, cfg: Any, tree: Path, work_dir: Path, *, run_id: str, targets: list[str], applied: list[Proposal],
            manifest_paths: list[str], first_outcome: BuildOutcome | None, stop: Callable[[], bool],
            deadline: float | None, journal: Callable[[dict[str, Any]], None] | None = None) -> FixResult:
        """수정본의 프로젝트 루트를 복사한 작업 폴더에서 빌드 오류 수정 루프를 실행한다."""
        return run_fix_loop(cfg, tree, work_dir, run_id=run_id, targets=targets, applied=applied,
                            manifest_paths=manifest_paths, first_outcome=first_outcome, stop=stop, deadline=deadline,
                            journal=journal)


__all__ = ["MIN_LOOP_SECONDS", "RemediationFixer", "check_paths", "compare_trees", "fix_usage", "fix_usage_rows",
           "is_editable", "loop_paths", "make_edit_guard", "maven_pom_paths", "plan_notes", "run_fix_loop",
           "run_fix_usage", "unified_patch"]
