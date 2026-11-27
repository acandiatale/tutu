"""버전을 올린 수정본의 빌드가 실패했을 때 이관의 수정 루프(``BuildFixLoop``)로 소스를 고치는 모듈.

The work copy is ``rounds/<r>/fixloop/tree/``. The ``edit_guard`` refuses edits of poms, hidden paths, run
scripts and binary files, and any created or deleted file. No KB script is used and nothing of this loop is
recorded in the KB. The loop's journal and log stay under ``rounds/<r>/fixloop/``. Tracked files a build
changed are restored after every build; only the paths an attempt committed count as fixed.

`run_fix_loop` and the `Fixer` the graph reaches through `Components.fixer` (`RemediationFixer`). The
candidate tree is never written: the loop runs in a work copy of the loop paths, changed files are found by
byte comparison, the fixed bytes are stored durably in `fixloop/fixed/` and the graph overlays them. Paths are
relative to the project root. Contract: `docs/INTERFACES.md` "v2.1z-bi" part B (amended by "v2.1z-bj",
"v2.1z-bk")."""
from __future__ import annotations

import logging
import shutil  # noqa: F401 - 테스트가 이 모듈을 거쳐 shutil.rmtree를 바꿔 넣는다
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import fsops
from .models import BuildOutcome, FixResult, FixUsage, Proposal

log = logging.getLogger("sbmig.remediations.fixer")

_clock = time.monotonic                 # 테스트에서 교체하는 시계
# 남은 시간이 이보다 짧으면 Maven 빌드 한 번도 끝내기 어렵다. 루프를 시작하지 않는다
MIN_LOOP_SECONDS = 60.0
GITIGNORE = ".gitignore"

R_STOP = "stop requested"
R_TIME = "time budget used up"
R_LLM = "llm unavailable"


def plan_notes(applied: list[Proposal]) -> list[str]:
    """수정 루프 프롬프트에 넣을 영어 안내 문단을 만든다. 올린 버전과 지켜야 할 규칙이 들어간다."""
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
    """작업 복사본에 넣을 경로를 정렬해서 반환한다. 경로 중간에 ``.git``이 있는 manifest 경로는 뺀다.

    A ZIP may carry the project's own `.git/` or a nested checkout; git inside the work copy must see one
    repository only."""
    out = {_norm(p) for p in manifest_paths}
    return sorted(p for p in out if p and ".git" not in p.split("/"))


def maven_pom_paths(project: Path, targets: Iterable[str] = ()) -> set[str]:
    """Maven이 이 빌드에서 읽는 pom 가운데 이름이 ``pom.xml``이 아닌 파일의 경로를 모은다.
    예외를 던지지 않고, 읽지 못하면 빈 집합을 반환한다.

    Read with the inventory's pom reader (`ProjectPoms`, the reactor from the build targets): a
    `<relativePath>` or `<module>` naming an `.xml` file other than `pom.xml`, and a build target that is a
    file."""
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
    """루프가 바꿔도 되는 경로면 True를 반환한다. pom, 숨김 경로, 실행 스크립트, 바이너리 파일은 안 된다.

    Rule 2 of `v2.1z-bj` without the loop-path part: not a `pom.xml`, not a pom under another name, no segment
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
    """바뀐 경로가 규칙을 어기면 그 경로를 담은 영어 사유를 반환한다. 어긴 경로가 없으면 None을 반환한다.

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
    """``BuildFixLoop``의 ``edit_guard``로 넘길 함수를 만든다. 시도가 바꾼 경로를 ``check_paths``로 확인한다."""
    lp = {_norm(p) for p in loops}
    ed = {_norm(p) for p in editable}

    def guard(ws: Any, pre_head: str, touched: list[str]) -> str | None:
        return check_paths(Path(ws.root), list(touched), lp, ed)
    return guard


def compare_trees(tree: Path, work: Path, loops: list[str], editable: set[str],
                  attempted: set[str] | None = None) -> tuple[list[str], str | None]:
    """원래 수정본과 작업 복사본의 바이트를 비교해서 (바뀐 경로, 첫 위반 사유)를 반환한다.
    ``attempted``를 주면 시도가 바꾼 경로만 본다. 나머지 차이는 빌드가 남긴 것으로 보고 넘어간다.

    Rule 3: bytes of every loop path in `tree` against the work copy. An editable path that differs is changed; a
    missing loop path, a symlink, or a difference in a non-editable path other than the root `.gitignore` (git_init
    writes sbmig's lines into it) is a violation. With `attempted` (the paths the work copy's history
    records as changed by an attempt since the base commit) a difference or a missing file outside that set is a build
    leftover and ignored; only a symlink is a violation wherever it is. Such a path is skipped before its bytes are
    read, so a path no attempt changed that is unreadable in the work copy or absent from `tree` never
    raises."""
    changed: list[str] = []
    for p in loops:
        a, b = Path(tree) / p, Path(work) / p
        if b.is_symlink():
            return changed, _not_editable_reason(p)
        by_attempt = attempted is None or p in attempted
        if not b.is_file():
            if by_attempt:
                return changed, f"{p} must not be deleted"
            continue
        if p == GITIGNORE or not by_attempt:
            continue                                  # 시도가 바꾸지 않은 경로의 차이는 빌드가 남긴 것으로 본다
        if a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes():
            continue
        if p not in editable:
            return changed, _not_editable_reason(p)
        changed.append(p)
    return sorted(changed), None


# 패치를 보여 줄 때 시도하는 인코딩. ``restructure.pom_charset``과 같은 순서다
_PATCH_CODECS = (("utf-8", "UTF-8"), ("cp949", "MS949"))


def _decode_pair(a: bytes, b: bytes) -> tuple[str, str, str] | None:
    """두 바이트를 같은 인코딩으로 풀어서 (이전 내용, 새 내용, 인코딩 이름)을 반환한다.
    UTF-8, cp949 순서로 시도하고 둘 다 안 되면 None을 반환한다."""
    for codec, name in _PATCH_CODECS:
        try:
            return a.decode(codec), b.decode(codec), name
        except UnicodeDecodeError:
            continue
    return None


def unified_patch(tree: Path, work: Path, changed: list[str],
                  new_bytes: Callable[[str], bytes] | None = None) -> str:
    """바뀐 파일들의 unified diff를 만든다. 두 인코딩 모두로 풀리지 않는 파일은 ``Binary files differ``로 쓴다.

    `a/<path>` → `b/<path>`, project-relative, made with `difflib` from the two byte versions, decoded with
    the pom-charset order (UTF-8, then cp949); a file shown through cp949 gets a first line
    `# <path>: shown as UTF-8, the file is MS949`; only bytes that decode under neither are `Binary files differ`.
    `new_bytes(path)` gives the new version (default: the file under `work`)."""
    import difflib
    out: list[str] = []
    for p in changed:
        a = (Path(tree) / p).read_bytes()
        b = new_bytes(p) if new_bytes is not None else (Path(work) / p).read_bytes()
        pair = _decode_pair(a, b)
        if pair is None:
            out.append(f"--- a/{p}\n+++ b/{p}\nBinary files differ\n")
            continue
        at, bt, name = pair
        if name != "UTF-8":
            out.append(f"# {p}: shown as UTF-8, the file is {name}\n")
        for line in difflib.unified_diff(at.splitlines(keepends=True), bt.splitlines(keepends=True),
                                         fromfile=f"a/{p}", tofile=f"b/{p}"):
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _remove_leftovers(work: Path, unused: list[Path]) -> None:
    """루프가 끝난 뒤 쓰지 않는 결과 파일과 작업 복사본을 지운다.
    다른 프로그램이 잡고 있는 파일이 여럿이어도 끝까지 기다리는 것은 한 번뿐이다."""
    with fsops.one_wait():
        for leftover in unused:
            _rmtree_quiet(leftover, leftover.name)
        _rmtree_quiet(work, "the work copy")


def _rmtree_quiet(p: Path, what: str) -> None:
    """파일이나 폴더를 지운다. 지우지 못하면 WARNING만 기록하고 넘어간다."""
    try:
        if p.is_symlink() or p.is_file():
            fsops.unlink(p)
        elif p.exists():
            fsops.rmtree(p)
    except OSError as e:
        log.warning("remediation fix loop: %s not removed: %s", what, e)


def _untracked_entries(ws: Any) -> set[str]:
    """작업 복사본에서 git이 추적하지 않는 항목을 반환한다. 무시된 항목도 넣고 폴더는 한 항목으로 센다.
    읽지 못하면 빈 집합을 반환한다."""
    try:
        cp = ws._git("ls-files", "--others", "--directory", "-z", check=False)
        return {x.replace("\\", "/") for x in (cp.stdout or "").split("\0") if x}
    except Exception:  # noqa: BLE001 - git을 쓸 수 없으면 빌드가 만든 파일을 모르는 것으로 둔다
        return set()


class _DeadlineRunner:
    """빌드마다 시간 한도를 맞추는 래퍼 클래스. 남은 시간과 ``remediation.build_timeout_minutes`` 중 짧은 쪽을 쓴다.
    빌드가 바꾸거나 지운 추적 파일은 되돌리고, 빌드가 새로 만든 항목은 기록해 둔다.

    After each build every tracked file the build modified or deleted that was clean before it is checked
    out again (`Workspace.restore_build_byproducts`, the rule `BuildFixLoop._build` applies too), so an attempt is
    never blamed for or credited with the build's own edits; the untracked entries the build created (ignored ones
    included, `git ls-files --others --directory`) are collected in `created` for the end check."""

    def __init__(self, inner: Any, cap_s: float, time_left: Callable[[], float | None], ws: Any = None):
        self.inner, self.cap_s, self.time_left, self.ws = inner, cap_s, time_left, ws
        self.timeouts: list[float] = []
        self.created: set[str] = set()
        self.restored: set[str] = set()

    def run(self, goal: str, **kw: Any) -> Any:
        t = self.time_left()
        limit = self.cap_s if t is None else max(1.0, min(self.cap_s, t))
        self.timeouts.append(limit)
        if hasattr(self.inner, "timeout_s"):
            self.inner.timeout_s = limit
        ws = self.ws
        before: set[str] = set()
        others: set[str] = set()
        if ws is not None:
            try:
                before = set(ws.status_paths())
            except Exception:  # noqa: BLE001 - git을 쓸 수 없으면 되돌리지 않는다
                before = set()
            others = _untracked_entries(ws)
        try:
            return self.inner.run(goal, **kw)
        finally:
            if ws is not None:
                self.created |= _untracked_entries(ws) - others
                try:
                    self.restored |= set(ws.restore_build_byproducts(before) or [])
                except Exception:  # noqa: BLE001 - 정리하지 못해도 빌드 결과는 돌려준다
                    log.debug("build-side changes not restored", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _walk_files(root: Path) -> set[str]:
    """``root`` 아래 파일 경로를 모은다. ``.git`` 폴더와 빌드 산출물(pom 옆의 ``target/``)은 들어가지 않는다."""
    import os

    from sbmig.core.workspace import is_build_output_path
    out: set[str] = set()
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in dirnames:
            rd = f"{rel_dir}/{d}" if rel_dir else d
            if d == ".git" or is_build_output_path(rd, root):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            out.add(f"{rel_dir}/{f}" if rel_dir else f)
    return out


def _created_by_build(p: str, created: set[str]) -> bool:
    return any(p == e or (e.endswith("/") and p.startswith(e)) for e in created)


def _usage(results: list[Any], budget_calls: int, cfg: Any = None) -> tuple[int, int, int, list[str], list[FixUsage], str]:
    """루프의 시도들에서 LLM 사용량을 모델별로 모은다.
    (호출 수, 입력 토큰, 출력 토큰, 모델 이름, 모델별 행, 통화)를 반환한다.

    Per-model usage of the loop's attempts, summed the way the orchestrator sums the migration's
    (`Orchestrator._usage_by_model`): the row name is `asked_model_name(usage.model, attempt.model, llm.model)`,
    `served` merged without the asked name. Calls the loop's budget counted beyond the attempts' usage go to a row of
    their OWN named after the configured model (`tokens_unknown`, no tokens, never priced), so the rows always add up
    to `llm_calls` and a known row of the same name keeps its tokens and its price. Every known row is priced with
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
    if extra > 0:
        # 시도에 사용량이 없는 호출은 토큰 수를 모른다. 따로 한 행으로 두고 비용을 매기지 않는다
        usage.append(FixUsage(model=asked_model_name(default), calls=extra, cost=None, tokens_unknown=True))
    return (sum(r.calls for r in usage), sum(r.input_tokens for r in usage), sum(r.output_tokens for r in usage),
            list(dict.fromkeys(r.model for r in usage)), usage, currency)


def fix_usage_rows(fixes: Iterable[Any]) -> list[dict[str, Any]]:
    """여러 라운드의 수정 루프 결과(``FixResult`` 또는 dict)를 모델별 사용량 행으로 합친다.
    모델별 행이 없는 이전 결과는 합계를 한 행으로 만들고 비용은 모르는 것으로 둔다.

    Per-model rows `{model, served, calls, input_tokens, output_tokens, cost, currency}` of several rounds' fix loops
    (rounds that were not fixed count too), plus `tokens_unknown: true` on the row of calls whose tokens are unknown
    (kept apart from the known row of the same name, never priced). `cost` is None when any part of the row
    has calls or tokens and no price; `currency` is "" when no part was priced. A FixResult written before
    `usage` existed becomes one row of its totals named `asked_model_name(*models)`, unpriced."""
    from sbmig.core.models import asked_model_name, merge_model_names
    rows: dict[str, dict[str, Any]] = {}

    def add(model: str, served: list[str], calls: int, inp: int, out: int, cost: float | None, currency: str,
            unknown: bool = False) -> None:
        if not (calls or inp or out):
            return
        # 토큰 수를 모르는 호출은 같은 이름이어도 따로 한 행으로 둔다. 아는 행의 토큰과 비용은 그대로 둔다
        r = rows.setdefault(f"{model.lower()}\0{int(unknown)}", {"model": model, "served": [], "calls": 0,
                                                                "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                                                                "currency": ""})
        if unknown:
            r["tokens_unknown"] = True          # 이 키는 토큰 수를 모르는 호출이 있을 때만 둔다
            cost = None                         # 토큰 수를 모르는 행에는 비용을 매기지 않는다
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
                    int(u.output_tokens or 0), u.cost if isinstance(u.cost, (int, float)) else None, fx.currency or "",
                    bool(getattr(u, "tokens_unknown", False)))
        else:
            add(asked_model_name(*(fx.models or [])), [], int(fx.llm_calls or 0), int(fx.input_tokens or 0),
                int(fx.output_tokens or 0), None, "")
    return list(rows.values())


def fix_usage(state: Any) -> dict[str, Any]:
    """조치 상태(``RemediationState`` 또는 dict)에서 모든 라운드의 수정 루프 LLM 사용량을 모은다.
    결과 줄, 실행 상태 API, 관리자 페이지가 모두 이 함수로 호출 수를 센다. 예외를 던지지 않는다.

    The remediation's fix-loop usage over every round of a state (`rounds[].fix` and every discarded fix in
    `rounds[].fix_discarded`, fixed or not): `{calls, input_tokens, output_tokens, by_model, calls_unknown,
    tokens_unknown, usage_unknown}` (`by_model` = `fix_usage_rows`). `calls_unknown`: a loop's usage could not be read
    at all (`FixResult.usage_unknown`), so `calls` is a lower bound; `tokens_unknown`: a row has calls without token
    counts (`by_model[].tokens_unknown`, never priced); `usage_unknown` = either. The one place the result line,
    `GET /api/runs/{id}` and the admin page read the remediation's LLM calls from (beside the migration's
    `report.json`, never written into it). Never raises; an unreadable state counts nothing."""
    calls_unknown = False
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
        calls_unknown = any(bool(f.get("usage_unknown")) if isinstance(f, dict) else bool(getattr(f, "usage_unknown", False))
                            for f in fixes if f is not None)
    except Exception:  # 사용량을 읽지 못해도 호출한 쪽은 계속한다
        log.debug("remediation fix usage unreadable", exc_info=True)
        rows = []
    tokens_unknown = any(bool(r.get("tokens_unknown")) for r in rows)
    return {"calls": sum(r["calls"] for r in rows), "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows), "by_model": rows,
            "calls_unknown": calls_unknown, "tokens_unknown": tokens_unknown,
            "usage_unknown": calls_unknown or tokens_unknown}


R_INTERRUPTED = "interrupted: the run ended during the loop"
R_USAGE_UNKNOWN = " (usage unknown)"


def interrupted_result(folder: Path, cfg: Any = None) -> FixResult:
    """중간에 끝난 수정 루프의 폴더에서 LLM 사용량을 읽어서 버린 결과 하나를 만든다.
    사용량을 다 읽지 못하면 사유 끝에 ``(usage unknown)``을 붙인다.

    A loop killed mid-way left `fixloop/` (renamed by the graph). Its usage is read from the loop's own
    `journal.jsonl` (the `llm_script` / `llm_verify_guide` events, with the admin page's reader in its strict mode
    `sbmig.web.admin.journal_usage_checked`; the loop writes no `sbmig.log` of its own) and priced like `_usage`. A
    folder without `journal.jsonl` (with or without `loop.json`: no LLM call was made): zero, known. A journal that
    cannot be read to the end or a line that cannot be parsed (a line a kill cut off) → the usage that was read is
    kept, `usage_unknown: true` and `reason` ending ` (usage unknown)`. Never raises."""
    res = FixResult(attempted=True, fixed=False, reason=R_INTERRUPTED)
    if not usage_from_folder(res, folder, cfg):
        log.warning("remediation: the usage of an interrupted fix loop could not be read")
        res.reason = R_INTERRUPTED + R_USAGE_UNKNOWN
    return res


def usage_from_folder(res: FixResult, folder: Path, cfg: Any = None) -> bool:
    """수정 루프 폴더의 저널에서 읽은 사용량을 결과에 넣는다.
    다 읽지 못하면 ``usage_unknown``을 켜고 False를 반환한다.

    `journal_usage(folder, cfg)` into `res.usage` / `res.currency` and the totals; a journal that could not be read
    completely keeps what was read with `res.usage_unknown = True` (unknown, never an exact number) and returns
    False."""
    got = journal_usage(folder, cfg)
    res.usage, res.currency, known = got if got is not None else ([], "", False)
    _set_totals(res)
    if not known:
        res.usage_unknown = True
        return False
    return True


def _set_totals(res: FixResult) -> None:
    """모델별 행(``res.usage``)에서 호출 수, 토큰 수, 모델 이름 합계를 채운다."""
    res.llm_calls = sum(u.calls for u in res.usage)
    res.input_tokens = sum(u.input_tokens for u in res.usage)
    res.output_tokens = sum(u.output_tokens for u in res.usage)
    res.models = list(dict.fromkeys(u.model for u in res.usage))


def journal_usage(folder: Path, cfg: Any = None) -> tuple[list[FixUsage], str, bool] | None:
    """수정 루프 폴더의 ``journal.jsonl``에서 (모델별 사용량, 통화, 끝까지 읽었는지)를 반환한다.
    저널은 있는데 열 수 없으면 None을 반환한다.

    The usage the loop journaled (`llm_script` / `llm_verify_guide` events, read with the admin page's reader in its
    strict mode, `sbmig.web.admin.journal_usage_checked`), priced like `_usage`: `(rows, currency, known)`.
    `([], "", True)` for a folder without `journal.jsonl`, with or without `loop.json`: the graph writes `loop.json`
    before calling the fixer, and `run_fix_loop` writes its first journal line (`remediation_fix_loop_start`) before
    `BuildFixLoop.run`, the only place an LLM request can be made, so a kill during the setup (`copy_tree`,
    `git_init`, analysis) leaves no journal and made no call: known, zero. `known = False` when a line cannot be
    parsed (a line a kill cut off, garbage) or the file cannot be read to the end, the rows that were read kept; None
    when the journal exists but cannot be opened at all. Never raises."""
    try:
        jf = Path(folder) / "journal.jsonl"
        if not jf.exists() and not jf.is_symlink():
            # 저널이 없으면 LLM 호출도 없었다. run_fix_loop는 첫 LLM 요청 전에 저널 첫 줄을 쓴다
            return [], "", True
        if jf.is_symlink() or not jf.is_file():
            raise OSError("journal is not a regular file")
        with open(jf, "rb") as fh:                      # 읽을 수 없는 파일을 여기서 알아낸다
            fh.read(1)
        from sbmig.core.models import LLMUsage, asked_model_name
        from sbmig.web.admin import journal_usage_checked
        rows, known = journal_usage_checked(Path(folder), strict=True)
        default = getattr(getattr(cfg, "llm", None), "model", "") if cfg is not None else ""
        llm_rows = [LLMUsage(model=asked_model_name(m, default), served=list(r.get("served") or []), calls=int(r["calls"]),
                             input_tokens=int(r["input_tokens"]), output_tokens=int(r["output_tokens"]))
                    for m, r in rows.items()]
        costs: dict[str, float | None] = {}
        currency = ""
        if cfg is not None and llm_rows:
            try:
                from sbmig.report.cost import compute_llm_cost
                priced = compute_llm_cost(llm_rows, cfg)
                costs = {c.model: c.cost for c in priced.by_model}
                if any(c is not None for c in costs.values()):
                    currency = priced.currency
            except Exception:  # noqa: BLE001 - 단가를 계산하지 못해도 사용량은 남긴다
                log.debug("fix loop journal usage not priced", exc_info=True)
        usage = [FixUsage(model=u.model, served=list(u.served), calls=int(u.calls or 0),
                          input_tokens=int(u.input_tokens or 0), output_tokens=int(u.output_tokens or 0),
                          cost=costs.get(u.model)) for u in llm_rows if u.calls or u.input_tokens or u.output_tokens]
        return usage, currency, known
    except Exception:  # noqa: BLE001 - 읽지 못하면 None으로 알린다
        log.debug("fix loop journal usage unreadable", exc_info=True)
        return None


def _usage_after_error(work_dir: Path, budget_calls: int, cfg: Any) -> tuple[list[FixUsage], str]:
    """루프 밖으로 예외가 나왔을 때 루프의 저널에서 LLM 사용량을 읽는다.
    저널에 없는 호출은 토큰 수를 모르는 것으로 두고 비용을 매기지 않는다.

    Usage of a loop an exception escaped from: the loop's own journal (`journal_usage`, the reader of
    `interrupted_result`); calls the budget counted beyond the journal's (or all of them when the journal cannot be
    read) go to a row of their own named after the configured model, with `tokens_unknown: true` and no cost (unknown,
    never 0); a known row of the same name keeps its tokens and price."""
    default = getattr(getattr(cfg, "llm", None), "model", "") if cfg is not None else ""
    got = journal_usage(work_dir, cfg)
    rows, currency, _known = got if got is not None else ([], "", False)
    extra = int(budget_calls or 0) - sum(u.calls for u in rows)
    if extra > 0:
        from sbmig.core.models import asked_model_name
        # 토큰 수를 모르는 호출은 따로 한 행으로 두고 비용을 매기지 않는다
        rows = [*rows, FixUsage(model=asked_model_name(default), calls=extra, cost=None, tokens_unknown=True)]
        if not any(u.cost is not None for u in rows):
            currency = ""
    return rows, currency


def run_fix_usage(run_dir: Path | str) -> dict[str, Any]:
    """실행 폴더의 ``remediation/state.json``을 읽어서 ``fix_usage``를 반환한다.
    파일이 없거나 읽을 수 없으면 모든 값이 0이다."""
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
    ``tree``는 읽기만 한다. 고친 파일은 ``work_dir/fixed/``에 둔다.
    루프 안의 실패는 예외로 던지지 않고 결과로 반환한다.

    `tree` = the project root inside the candidate (never written); `work_dir` = `rounds/<r>/fixloop/` (the loop's
    journal and logs, the work copy `tree/`, the fixed bytes `fixed/`); `targets` = the remediation's build targets
    ([] = one build at the root); `manifest_paths` relative to `tree`; `first_outcome` = the failed candidate build
    (logged only: the loop builds once itself); `deadline` = a `time.monotonic()` instant or None; `patch_file` =
    where the unified diff goes when fixed with changes (default `work_dir.parent / "fix.patch"`). `FixResult.patch`
    and `FixResult.fixed_dir` are absolute paths here; the graph makes them run-relative. The keyword-only parts after
    it replace the real ones (tests): `build_runner(ws)` → a build runner, `llm_fixer`, `script_runner`,
    `edit_applier`, `profile`. The minimum loop time (`MIN_LOOP_SECONDS`) is checked by the graph, which knows the
    budget; this function refuses (`time budget used up`, `attempted: false`) only when `deadline` has already
    passed."""
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

    # 최소 시간은 예산을 아는 graph가 한 곳에서 확인한다. 여기서는 마감이 이미 지났을 때만 시작하지 않는다
    if out_of_time():
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
                fsops.rmtree(old) if old.is_dir() and not old.is_symlink() else fsops.unlink(old)
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
        base = ws.head()

        if profile is None:
            from sbmig.analyze.analyzer import analyze_project
            profile = analyze_project(ws, cfg)
        inner = build_runner(ws) if build_runner is not None else \
            lazy_attr("sbmig.build", "MavenRunner", "C (build)")(ws, cfg)
        cap_s = float(getattr(cfg.remediation, "build_timeout_minutes", 60) or 60) * 60.0
        runner = _DeadlineRunner(inner, cap_s, time_left, ws)
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
        # 루프 전에 있던 파일. 끝에서 시도가 만든 파일을 찾을 때 뺀다(sbmig의 .gitignore, 분석이 남긴 파일)
        initial = _walk_files(work)
        # journal_usage는 저널이 없으면 호출도 없었다고 본다. 그래서 첫 LLM 요청 전에 저널 첫 줄을 쓴다
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
            # 시도가 커밋한 경로만 시도의 수정으로 본다. 그 밖의 바이트 차이는 빌드가 남긴 것으로 본다
            attempted = {_norm(x) for x in ws.changed_between(base, ws.head())}
            changed, violation = compare_trees(tree, work, loops, editable, attempted)
            if violation is None:
                lp = set(loops)
                made = sorted(x for x in _walk_files(work) - initial
                              if x not in lp and x != GITIGNORE and not _created_by_build(x, runner.created))
                if made:
                    violation = f"{made[0]} must not be created"
            if runner.restored:
                ws.journal({"event": "remediation_fix_build_side", "restored": sorted(runner.restored)[:20]})
            if violation is not None:
                res.reason = scrub(f"rejected: {violation}")
                if journal is not None:
                    journal({"event": "remediation_fix_rejected", "reason": scrub(violation)})
            elif not changed:
                # 바꾼 것 없이 루프의 첫 빌드가 통과했다. 처음 빌드 실패는 일시적이었다
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
        # 시간 한도가 지나면 중지 함수도 참이 된다. 그래서 마감을 중지 요청보다 먼저 확인한다
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
    except Exception as e:  # noqa: BLE001 - 루프 안의 실패는 결과로 돌려준다. 수정본은 건드리지 않았다
        log.warning("remediation fix loop failed: %s: %s", type(e).__name__, e, exc_info=True)
        res.reason = scrub(f"error: {type(e).__name__}: {e}")
        res.fixed, res.changed, res.patch, res.fixed_dir, res.retry_passed = False, [], "", "", False
        if loop is not None and not res.llm_calls:
            # 시도 결과를 받지 못했다. 루프가 저널에 남긴 사용량을 읽는다
            try:
                res.usage, res.currency = _usage_after_error(work_dir, loop.budget.llm_calls, cfg)
                _set_totals(res)
            except Exception:  # noqa: BLE001 - 사용량을 셀 수 없어도 결과는 돌려준다
                log.debug("fix loop usage not counted", exc_info=True)
                res.usage, res.currency, res.usage_unknown = [], "", True
                _set_totals(res)
    _remove_leftovers(work, [] if res.fixed and res.changed else [patch_file, fixed_root])
    log.info("remediation fix loop: fixed=%s iterations=%d attempts=%d llm_calls=%d changed=%d%s%s", res.fixed,
             res.iterations, res.attempts, res.llm_calls, len(res.changed), " retry passed" if res.retry_passed else "",
             f" ({res.reason})" if res.reason else "")
    return res


class RemediationFixer:
    """그래프가 ``Components.fixer``로 호출하는 실제 구현 클래스. ``run_fix_loop``를 그대로 호출한다."""

    def run(self, cfg: Any, tree: Path, work_dir: Path, *, run_id: str, targets: list[str], applied: list[Proposal],
            manifest_paths: list[str], first_outcome: BuildOutcome | None, stop: Callable[[], bool],
            deadline: float | None, journal: Callable[[dict[str, Any]], None] | None = None) -> FixResult:
        """수정본의 프로젝트 루트를 복사한 작업 폴더에서 빌드 오류 수정 루프를 실행한다."""
        return run_fix_loop(cfg, tree, work_dir, run_id=run_id, targets=targets, applied=applied,
                            manifest_paths=manifest_paths, first_outcome=first_outcome, stop=stop, deadline=deadline,
                            journal=journal)


__all__ = ["MIN_LOOP_SECONDS", "R_INTERRUPTED", "R_USAGE_UNKNOWN", "RemediationFixer", "check_paths", "compare_trees",
           "fix_usage", "fix_usage_rows", "interrupted_result", "is_editable", "journal_usage", "loop_paths",
           "make_edit_guard",
           "maven_pom_paths", "plan_notes", "run_fix_loop", "run_fix_usage", "unified_patch", "usage_from_folder"]
