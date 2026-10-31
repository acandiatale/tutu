"""Fake collaborators for loop/orchestrator tests (no mvn, no network, no LLM).

v2 (flow-v2): `FakeScriptRunner` runs a fix script IN-PROCESS against a tiny helper `p` bound to the workspace (the
real `sbmig.script.ScriptRunner` uses a `python -I` process); `FakeFixer.propose_script` answers with scripted
`FixAnswer`s, a `script_fn`, or — for tests written against SEARCH/REPLACE proposals — turns the `FixProposal` of
`fn` / `proposals` into a script (`script_for`).

v2.1: `FakeCaseStore` is an in-memory `kb.cases` (best / code / find / add_script / credit / describe);
`FakeKB` the KB facade (`cases`, `match`, `record_run`); `FakeErrorList` records every error-list call (and keeps
FixTry objects per case); `FakeEditApplier` applies `Edit`s like `sbmig.script.apply_edits`; `answer(...)` builds a
FixAnswer with 원인 / 해결 / edits / script / no_script_reason (v2.1d: `verify`).

v2.1d: `FakeVerifyFixer` = a FakeFixer with `verify_guide` (the plain FakeFixer has none: `hasattr` is False).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Union

from sbmig.core.interfaces import RestructureResult
from pydantic import BaseModel, Field

from sbmig.core.models import (BuildError, BuildResult, CaseInfo, CaseScript, CaseType, Edit, EditNote, ErrorKind,
                               FileChange, FixAnswer, FixTry, KBMatch, LLMUsage, MigrationPlan, Phase, PlanStep,
                               PomInfo, ProjectProfile, RecipeRef, RewriteResult, ScriptProposal, ScriptRunResult,
                               StepKind, StructureReview)
from sbmig.core.workspace import Workspace


class FixProposal(BaseModel):
    """Test-only: the v1 SEARCH/REPLACE answer shape the loop fakes still describe fixes with (`write_fix`,
    `FakeFixer.fn`); `script_for` turns it into the fix script the v2 loop runs."""
    changes: list[FileChange]
    analysis: str = ""
    confidence: float = 0.5
    model: str = ""
    prompt_hash: str = ""
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    requested_files: list[str] = Field(default_factory=list)
    raw_response: str = ""

from tests.triage.fakes import err  # noqa: F401  (re-export)

ScriptItem = Union[BuildResult, Callable[[Workspace, dict], BuildResult]]


def ok_build(goal: str = "compile") -> BuildResult:
    return BuildResult(ok=True, goal=goal, command=["mvn", goal], exit_code=0, duration_s=0.1, log_path="/tmp/ok.log")


def bad_build(*errors: BuildError, goal: str = "compile") -> BuildResult:
    return BuildResult(ok=False, goal=goal, command=["mvn", goal], exit_code=1, duration_s=0.1,
                       log_path="/tmp/bad.log", errors=list(errors))


class FakeBuildRunner:
    def __init__(self, ws: Workspace, script: Optional[list[ScriptItem]] = None,
                 fn: Optional[Callable[[Workspace, dict], BuildResult]] = None):
        self.ws = ws
        self.script = list(script or [])
        self.fn = fn
        self.calls: list[dict] = []

    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False,
            skip_tests=False) -> BuildResult:
        call = {"goal": goal, "module": module, "label": label, "skip_test_compile": skip_test_compile,
                "skip_tests": skip_tests,
                "extra_args": list(extra_args),
                "head": self.ws.head() if (self.ws.root / ".git").exists() else None}
        self.calls.append(call)
        if self.fn is not None:
            item: ScriptItem = self.fn
        elif self.script:
            item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        else:
            item = ok_build(goal)
        res = item(self.ws, call) if callable(item) else item
        return res.model_copy(update={"goal": goal})


class FakeFixer:
    """propose_script → `scripts` (ScriptProposals, in order), `script_fn(ws, cluster, ctx)`, or — test shorthand —
    the FixProposal of `fn` / `proposals` turned into a script (`script_for`)."""

    def __init__(self, proposals: Optional[list[FixProposal]] = None,
                 fn: Optional[Callable[[Workspace, Any, Any], FixProposal]] = None,
                 scripts: Optional[list[ScriptProposal]] = None,
                 script_fn: Optional[Callable[[Workspace, Any, Any], ScriptProposal]] = None):
        self.proposals = list(proposals or [])
        self.fn = fn
        self.scripts = list(scripts or [])
        self.script_fn = script_fn
        self.contexts: list[Any] = []
        self.script_calls: list[tuple[Any, Any]] = []      # (cluster, ctx) per propose_script call

    def propose_script(self, ws, cluster, ctx) -> ScriptProposal:
        self.contexts.append(ctx)
        self.script_calls.append((cluster, ctx))
        if self.script_fn is not None:
            return self.script_fn(ws, cluster, ctx)
        if self.scripts:
            return self.scripts.pop(0)
        if self.fn is not None or self.proposals:
            fp = self.fn(ws, cluster, ctx) if self.fn is not None else self.proposals.pop(0)
            return ScriptProposal(code=script_for(fp.changes, cluster) if fp.changes else "", analysis=fp.analysis,
                                  model=fp.model, llm_usage=fp.llm_usage, prompt_hash=fp.prompt_hash,
                                  requested_files=list(fp.requested_files),
                                  error="" if fp.changes else "no <script> block")
        # the fixer ran out of answers: an unusable answer (no analysis → not a v2.1y "declined" one)
        return ScriptProposal(code="", analysis="", model="fake", error="no <script> block",
                              llm_usage=LLMUsage(model="fake", calls=1))


class FakeVerifyFixer(FakeFixer):
    """v2.1d: a FakeFixer that also has `verify_guide` (VerifyGuideLike) — answers `verify_guides` in order ("" once
    they are used up); records each call's (cluster, ctx, changes, diff). A plain FakeFixer has no such attribute."""

    def __init__(self, *args: Any, verify_guides: list[str] | None = None, **kw: Any):
        super().__init__(*args, **kw)
        self.verify_guides = list(verify_guides or [])
        self.verify_calls: list[tuple[Any, Any, list, str]] = []
        self.verify_solutions: list[str] = []
        self.last_verify_usage = LLMUsage()

    def verify_guide(self, ws, cluster, ctx, changes, diff, *, solution: str = "") -> str:
        self.verify_calls.append((cluster, ctx, list(changes), diff))
        self.verify_solutions.append(solution)
        self.last_verify_usage = LLMUsage(model="fake-verify", calls=1, input_tokens=50, output_tokens=10)
        return self.verify_guides.pop(0) if self.verify_guides else ""


def write_fix(path: Optional[str], content: str, analysis: str = "fix", model: str = "fake-model") -> FixProposal:
    """A v1-style proposal; path None / "*" = every file of the cluster's errors (v2 clusters span files)."""
    return FixProposal(changes=[FileChange(path=path or "*", op="create", content=content)], analysis=analysis,
                       model=model, llm_usage=LLMUsage(model=model, calls=1, input_tokens=100, output_tokens=20))


def script_for(changes: list[FileChange], cluster: Any = None) -> str:
    """A fix script doing what SEARCH/REPLACE changes did (path "*" = every error file of `cluster`)."""
    files = list(dict.fromkeys(e.file for e in (cluster.errors if cluster is not None else []) if e.file))
    lines = ["# sbmig-fix", "def fix(p):"]
    for ch in changes:
        for path in (files if ch.path == "*" else [ch.path]):
            if ch.op == "delete":
                lines.append(f"    p.delete({path!r})")
            elif ch.op == "modify" and ch.search is not None:
                lines.append(f"    if p.replace({path!r}, {ch.search!r}, {(ch.replace or '')!r}, 1) != 1:")
                lines.append(f"        raise ValueError('search text not found in ' + {path!r})")
            else:
                lines.append(f"    p.write({path!r}, {(ch.content or '')!r})")
    if len(lines) == 2:
        lines.append("    pass")
    return "\n".join(lines) + "\n"


def script(body: str, analysis: str = "fix", model: str = "fake-model", calls: int = 1) -> ScriptProposal:
    """A ScriptProposal whose `fix(p)` runs `body` (indented by the helper)."""
    code = "# sbmig-fix\ndef fix(p):\n" + "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
    return ScriptProposal(code=code, analysis=analysis, model=model,
                          llm_usage=LLMUsage(model=model, calls=calls, input_tokens=100, output_tokens=20))


def write_script(files: dict[str, str], analysis: str = "fix", model: str = "fake-model") -> ScriptProposal:
    """A script that writes `files` (path → content)."""
    return script("\n".join(f"p.write({k!r}, {v!r})" for k, v in files.items()) or "pass", analysis, model)


class _FakeHelper:
    """The helper `p` of the fake runner: project-relative files of the workspace (no refusal rules here)."""

    def __init__(self, ws: Workspace, errors: list[BuildError]):
        self._ws = ws
        self.errors = [{"file": e.file, "line": e.line, "message": e.message, "symbol": e.symbol,
                        "signature": e.signature, "signature_generic": e.signature_generic, "kind": e.kind.value,
                        "module": e.module} for e in errors]
        self.changed: list[str] = []
        self.lines: list[str] = []

    def files(self, glob: str = "**/*") -> list[str]:
        return [f for f in sorted(self._ws.glob("**/*")) if fnmatch.fnmatch(f, glob)]

    def exists(self, path: str) -> bool:
        return self._ws.exists(path)

    def read(self, path: str) -> str:
        return self._ws.read(path)

    def write(self, path: str, text: str) -> bool:
        # like sbmig.script.api: the same text again is no change (v2.1y's idempotency check relies on it)
        if self._ws.exists(path) and self._ws.read(path) == text:
            return False
        self._ws.write(path, text)
        self.changed.append(path)
        return True

    def create(self, path: str, text: str = "") -> bool:
        if self._ws.exists(path):
            raise FileExistsError(path)
        return self.write(path, text)

    def delete(self, path: str) -> bool:
        self._ws.delete(path)
        self.changed.append(path)
        return True

    def replace(self, path: str, old: str, new: str, count: int = -1) -> int:
        text = self._ws.read(path)
        n = text.count(old) if count < 0 else min(count, text.count(old))
        if n:
            self.write(path, text.replace(old, new, count))
        return n

    def log(self, msg: Any) -> None:
        self.lines.append(str(msg))


class FakeScriptRunner:
    """ScriptRunnerLike: runs `def fix(p)` in-process. `# reject` in the code → rejected, `# timeout` → timeout (after
    the code ran: no partial application is the loop's job), an exception → error; `results` (a list consumed in
    order, or a fn(ws, code) → ScriptRunResult) replace the execution."""

    def __init__(self, results: Optional[list[ScriptRunResult]] = None,
                 fn: Optional[Callable[[Workspace, str], ScriptRunResult]] = None):
        self.results = list(results or [])
        self.fn = fn
        self.runs: list[dict] = []

    def check(self, code: str) -> list[str]:
        return ["forbidden: # reject"] if "# reject" in code else ([] if "def fix(" in code else ["no def fix(p)"])

    def run(self, ws, code, *, errors=(), timeout_s=None) -> ScriptRunResult:
        self.runs.append({"code": code, "errors": list(errors), "head": ws.head()})
        if self.fn is not None:
            return self.fn(ws, code)
        if self.results:
            return self.results.pop(0)
        problems = self.check(code)
        if problems:
            return ScriptRunResult(status="rejected", problems=problems, error=f"rejected: {problems[0]}")
        p = _FakeHelper(ws, list(errors))
        scope: dict[str, Any] = {}
        try:
            exec(compile(code, "<fix-script>", "exec"), scope)       # test code only
            scope["fix"](p)
        except Exception as e:  # noqa: BLE001
            return ScriptRunResult(status="error", changed_files=p.changed, log=p.lines,
                                   error=f"{type(e).__name__}: {e}")
        if "# timeout" in code:
            return ScriptRunResult(status="timeout", changed_files=p.changed, log=p.lines,
                                   error="killed after 60s")
        return ScriptRunResult(status="ok", changed_files=list(dict.fromkeys(p.changed)), log=p.lines)


class FakeCaseStore:
    """In-memory `kb.cases` (E1 CaseStore): best = the newest proven version, else the newest unverified one (never
    retired); add_script stores the next version `unverified` (same code → the stored version); credit improved /
    build_ok → successes +1 and `proven` once that is a project other than the creator's."""

    def __init__(self):
        self.data: dict[str, CaseInfo] = {}
        self.codes: dict[tuple[str, int], str] = {}
        self.credits: list[tuple[str, int, str, str]] = []      # (case, version, result, project)
        self.credit_runs: list[dict] = []                        # v2.1e: every credit's keywords (reason, round, n, …)
        self.added: list[tuple[str, CaseScript, dict]] = []      # (case, script, kwargs)
        self.marked: list[tuple[str, int, str]] = []             # v2.1y: mark_non_idempotent calls

    def find(self, case_id):
        return self.data.get(case_id)

    def scripts(self, case_id):
        c = self.data.get(case_id)
        return list(c.scripts) if c else []

    def code(self, case_id, version):
        return self.codes.get((case_id, version))

    def best(self, case_id):
        ranked = sorted((s for s in self.scripts(case_id) if s.status != "retired" and not s.non_idempotent),
                        key=lambda s: (s.status == "proven", s.version), reverse=True)
        for s in ranked:
            code = self.code(case_id, s.version)
            if code:
                return s, code
        return None

    def latest(self, case_id):
        for s in sorted(self.scripts(case_id), key=lambda s: s.version, reverse=True):
            if s.status != "retired" and self.code(case_id, s.version):
                return s, self.code(case_id, s.version)
        return None

    def describe(self, case_id):
        if case_id not in self.data:
            return "저장된 스크립트 없음 (처음 보는 케이스)"
        b = self.best(case_id)
        if b is None:
            return "케이스는 있으나 쓸 스크립트 없음"
        s = b[0]
        return f"저장된 스크립트 v{s.version} ({s.status}, 성공 {s.successes} / 실패 {s.failures}, 프로젝트 {len(s.projects)})"

    def add_script(self, case_id, l2, code, *, source="llm-new", run_id="", project="", analysis="", solution="",
                   name="", kind=None, parent=None, reason=""):
        case = self.data.setdefault(case_id, CaseInfo(case_id=case_id, l2=l2))
        for sc in case.scripts:
            if self.codes.get((case_id, sc.version)) == code:
                return sc
        v = 1 + max((sc.version for sc in case.scripts), default=0)
        sc = CaseScript(version=v, file=f"fix_v{v}.py", source=source, created_by=run_id, project=project,
                        parent=parent or None, reason=reason)
        case.scripts.append(sc)
        self.codes[(case_id, v)] = code
        case.last_analysis, case.last_solution = analysis or case.last_analysis, solution or case.last_solution
        self.added.append((case_id, sc, {"source": source, "run_id": run_id, "project": project,
                                         "analysis": analysis, "solution": solution, "l2": l2, "parent": parent,
                                         "reason": reason}))
        return sc

    def seed(self, case_id, code, *, status="proven", successes=1, projects=("p0",), failures=0) -> CaseScript:
        sc = self.add_script(case_id, "", code, source="llm-new", project="p0")
        self.added.pop()
        sc.status, sc.successes, sc.failures, sc.projects = status, successes, failures, list(projects)
        return sc

    def credit(self, case_id, version, result, project="", run_id="", *, reason="", round=0, n=0,
               errors_before=None, errors_left=None, errors_new=None, files=()):
        self.credits.append((case_id, version, result, project))
        self.credit_runs.append({"case": case_id, "version": version, "result": result, "project": project,
                                 "run_id": run_id, "reason": reason, "round": round, "n": n,
                                 "errors_before": errors_before, "errors_left": errors_left,
                                 "errors_new": errors_new, "files": list(files)})
        sc = next((x for x in self.scripts(case_id) if x.version == version), None)
        if sc is None:
            return None
        if result in ("improved", "build_ok"):
            sc.successes += 1
            if project and project not in sc.projects:
                sc.projects.append(project)
            if project and project != sc.project and sc.status == "unverified":
                sc.status = "proven"
        elif result in ("no_change", "regressed", "reverted", "apply_failed"):
            sc.failures += 1
            sc.last_failure = reason or result
        sc.last_result = result
        return sc

    def case_ids(self):
        return sorted(self.data)

    def mark_non_idempotent(self, case_id, version, note=""):
        """v2.1y: like CaseStore.mark_non_idempotent (records the call in `marked`)."""
        sc = next((x for x in self.scripts(case_id) if x.version == version), None)
        if sc is None:
            return None
        sc.non_idempotent = True
        if note:
            sc.last_failure = note
        self.marked.append((case_id, version, note))
        return sc


class FakeKB:
    """The v2.1 KB facade: `cases` (FakeCaseStore; `cases=None` = no store), `match` (case hits only),
    `record_run(entries)` (recorded)."""

    def __init__(self, match: Optional[Callable[[Any], KBMatch]] = None, raise_match: bool = False,
                 cases: Any = "default"):
        self._match = match
        self.raise_match = raise_match
        self.cases = FakeCaseStore() if cases == "default" else cases
        self.recorded: list[list] = []

    def record_run(self, entries) -> int:
        self.recorded.append(list(entries))
        return len(self.recorded[-1])

    def match(self, cluster, ws, profile) -> KBMatch:
        if self.raise_match:
            raise RuntimeError("kb down")
        return self._match(cluster) if self._match else KBMatch()


class FakeErrorList:
    """Records every call of the error-list contract (`calls`: (name, args, kwargs)); keeps one FixTry per (case, n)
    so tests can read what the loop wrote. `new_per_build` = what add_build returns (a list of that many items)."""

    def __init__(self, ws=None, cfg=None, project: str = "", new_per_build: Optional[Callable[[Any], int]] = None):
        self.project = project
        self.calls: list[tuple[str, tuple, dict]] = []
        self.tries: dict[tuple[str, int], FixTry] = {}
        self.entries: list = []
        self.new_per_build = new_per_build

    def _rec(self, name, *args, **kw):
        self.calls.append((name, args, kw))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def of(self, name: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    def add_build(self, build, seen, *, round=None, void=False):
        self._rec("add_build", build, seen, round=round, void=void)
        n = self.new_per_build(build) if self.new_per_build else len(getattr(build, "errors", None) or [])
        return [object()] * n

    def add_pom_verify(self, file, note):
        self._rec("add_pom_verify", file, note)
        e = SimpleNamespace(id=f"p{len(self.entries)}", case_id="", status="pom-verify", file=file, note=note)
        self.entries.append(e)
        return e

    def add_note(self, file, text, seen="review", *, status="note", l2=""):
        self._rec("add_note", file, text, seen, status=status, l2=l2)
        e = SimpleNamespace(id=f"n{len(self.entries)}", case_id=hashlib.sha1(l2.encode()).hexdigest()[:10] if l2 else "",
                            status=status, file=file, note=text, resolved="")
        self.entries.append(e)
        return e

    def verify_poms(self, build):
        self._rec("verify_poms", build)
        hit = [e for e in self.entries if getattr(e, "status", "") == "pom-verify"]
        if getattr(build, "ok", False):
            for e in hit:
                e.status = "resolved"
            return hit
        return []

    def begin_try(self, case_id, round, source, script_version=0, *, entry_ids=None):
        self._rec("begin_try", case_id, round, source, script_version=script_version, entry_ids=entry_ids)
        n = 1 + max((k[1] for k in self.tries if k[0] == case_id), default=0)
        t = FixTry(n=n, round=round, source=source, script_version=script_version)
        self.tries[(case_id, n)] = t
        return t

    def finish_try(self, case_id, n, **fields):
        self._rec("finish_try", case_id, n, **fields)
        t = self.tries.get((case_id, n))
        if t is not None:
            for k, v in fields.items():
                if v is not None:
                    setattr(t, k, v)
        return t

    def resolve(self, case_id, round, n, *, entry_ids=None, how=""):
        self._rec("resolve", case_id, round, n, entry_ids=entry_ids)
        for e in self.entries:
            if entry_ids is not None and getattr(e, "id", None) in entry_ids:
                e.status, e.resolved = "resolved", "review" if not round else f"r{round} try {n}"
        return []

    def block(self, case_id, reason):
        self._rec("block", case_id, reason)
        return []

    def end_of_run(self):
        self._rec("end_of_run")
        return self.counts()

    def counts(self):
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def write(self):
        self._rec("write")

    def to_report(self):
        self._rec("to_report")
        return []

    def tries_of(self, case_id: str) -> list[FixTry]:
        return [t for (c, _n), t in sorted(self.tries.items(), key=lambda kv: kv[0][1]) if c == case_id]


class FakeEditApplier:
    """`sbmig.script.apply_edits` in-process: replace / write / create / delete on the workspace; a replace whose
    text is missing → error "edit N: text not found in <path>" (the caller restores the tree); `result` overrides."""

    def __init__(self, result: Optional[ScriptRunResult] = None):
        self.result = result
        self.calls: list[list[Edit]] = []

    def __call__(self, ws, edits, *, cfg=None, errors=(), runner=None, timeout_s=None) -> ScriptRunResult:
        self.calls.append(list(edits))
        if self.result is not None:
            return self.result
        changed: list[str] = []
        for i, e in enumerate(edits, 1):
            if e.op == "replace":
                text = ws.read(e.path) if ws.exists(e.path) else ""
                if e.old not in text or not e.old:
                    return ScriptRunResult(status="error", changed_files=changed,
                                           error=f"edit {i}: text not found in {e.path}")
                ws.write(e.path, text.replace(e.old, e.new) if e.count < 0 else text.replace(e.old, e.new, e.count))
            elif e.op in ("write", "create"):
                ws.write(e.path, e.content)
            elif e.op == "delete":
                ws.delete(e.path)
            else:
                return ScriptRunResult(status="rejected", problems=[f"edit {i}: op {e.op} not supported here"],
                                       error=f"rejected: edit {i}: op {e.op}")
            changed.append(e.path)
        return ScriptRunResult(status="ok", changed_files=list(dict.fromkeys(changed)))


def answer(*, code: str = "", edits: Optional[list] = None, analysis: str = "the cause.", solution: str = "the fix.",
           no_script_reason: str = "", model: str = "fake-model", calls: int = 1, error: str = "",
           changes: Optional[list] = None, verify: str = "") -> FixAnswer:
    """A FixAnswer (원인 · 해결 · edits · script · v2.1b `<changes>` · v2.1d `<verify>`); `edits` = Edit objects or
    dicts, `changes` = EditNote objects or dicts."""
    es = [e if isinstance(e, Edit) else Edit(**e) for e in (edits or [])]
    ch = [c if isinstance(c, EditNote) else EditNote(**c) for c in (changes or [])]
    return FixAnswer(code=code, edits=es, analysis=analysis, solution=solution, no_script_reason=no_script_reason,
                     model=model, error=error, changes=ch, verify=verify,
                     llm_usage=LLMUsage(model=model, calls=calls, input_tokens=100, output_tokens=20))


def write_edit(path: str, content: str, desc: str = "") -> Edit:
    return Edit(op="write", path=path, content=content, desc=desc)


class FakeRewriteRunner:
    def __init__(self, ws: Workspace, effect: Optional[Callable[[Workspace, list[RecipeRef]], list[str]]] = None,
                 ok: bool = True, error: Optional[str] = None):
        self.ws = ws
        self.effect = effect
        self.ok = ok
        self.error = error
        self.runs: list[dict] = []

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="") -> RewriteResult:
        self.runs.append({"recipes": [r.id for r in recipes], "module": module, "dry_run": dry_run, "label": label,
                          "files": [str(f) for f in extra_recipe_files]})
        changed = self.effect(self.ws, list(recipes)) if (self.effect and not dry_run) else []
        return RewriteResult(ok=self.ok, recipes=[r.id for r in recipes], changed_files=changed, error=self.error,
                             dry_run=dry_run)


class FakeRestructurer:
    def __init__(self, ok: bool = True, fail_modes: tuple[str, ...] = ()):
        self.ok = ok
        self.fail_modes = fail_modes
        self.calls: list[str] = []
        self.params: list[dict] = []

    def apply(self, ws, profile, cfg, *, mode, params=None):
        self.calls.append(mode)
        self.params.append(dict(params or {}))
        if mode in self.fail_modes:
            return RestructureResult(ok=False, error=f"{mode} failed", notes=[f"{mode} attempted"])
        if self.ok:
            ws.write("module/boot-jar/pom.xml", "<project/>\n")
        return RestructureResult(ok=self.ok, changed_files=["pom.xml"], generated_files=["module/boot-jar/pom.xml"],
                                 mandatory_added=["a:b"], optional_bumped=["c:d 1→2"],
                                 manual_followups=["web.xml not migrated"], notes=["restructured"],
                                 error=None if self.ok else "boom")


def make_project(tmp_path: Path, name: str = "demo") -> Path:
    src = tmp_path / "src" / name
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n")
    (src / "src/main/java/com/x/Foo.java").write_text("class Foo { broken }\n")
    (src / "src/main/java/com/x/Bar.java").write_text("class Bar { broken }\n")
    return src


def make_profile(case: CaseType = CaseType.SPRING_BOOT, build_tool: str = "maven", poms: bool = True) -> ProjectProfile:
    pl = [PomInfo(path="pom.xml", artifact_id="demo", is_root=True)] if poms else []
    return ProjectProfile(root="/x", name="demo", case_type=case, build_tool=build_tool, poms=pl,
                          root_pom=pl[0] if pl else None, fingerprint="fp123")


def boot_plan(max_it: int = 3) -> MigrationPlan:
    rr = lambda i: [RecipeRef(id=i, artifact="org.openrewrite.recipe:rewrite-migrate-java")]  # noqa: E731
    steps = [
        PlanStep(id="jdk21.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.JDK21, title="jdk21",
                 recipes=rr("org.openrewrite.java.migrate.UpgradeToJava21")),
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="loop", build_goal="compile",
                 max_iterations=max_it),
        PlanStep(id="spring.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.SPRING, title="spring",
                 recipes=rr("org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0")),
        PlanStep(id="spring.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.SPRING, title="loop",
                 build_goal="test-compile", max_iterations=max_it),
        PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE, title="restructure",
                 params={"mode": "layout"}),
        PlanStep(id="restructure.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.RESTRUCTURE, title="loop",
                 build_goal="package", max_iterations=max_it),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                 build_goal="clean package"),
    ]
    return MigrationPlan(profile_fingerprint="fp123", case_type=CaseType.SPRING_BOOT, steps=steps,
                         notes=["followup: check web.xml"])


def msa_plan() -> MigrationPlan:
    steps = []
    for svc in ("svc-a", "svc-b"):
        steps += [
            PlanStep(id=f"jdk21.rewrite@{svc}", kind=StepKind.OPENREWRITE, phase=Phase.JDK21, title="jdk21",
                     recipes=[RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")], target=svc),
            PlanStep(id=f"jdk21.loop@{svc}", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="loop",
                     build_goal="compile", max_iterations=2, target=svc),
            PlanStep(id=f"spring.loop@{svc}", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.SPRING, title="loop",
                     build_goal="test-compile", max_iterations=2, target=svc),
        ]
    steps += [
        PlanStep(id="restructure.msa_pom", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE, title="msa",
                 params={"mode": "msa_pom"}),
        PlanStep(id="verify.build@svc-a", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package",
                 target="svc-a", params={"service": "svc-a"}),
        PlanStep(id="verify.build@svc-b", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package",
                 target="svc-b", params={"service": "svc-b"}),
    ]
    return MigrationPlan(profile_fingerprint="fp123", case_type=CaseType.MSA, steps=steps)


class FakeReviewer:
    """StructureReviewerLike (v2.1): returns `review` (default verdict ok); `files` → applied like the review's edits
    (left uncommitted, script_result ok, `answer` = FixAnswer with those write edits + `code` as its script);
    `raises` → an exception; records the call's arguments."""

    def __init__(self, review: Optional[StructureReview] = None, files: Optional[dict[str, str]] = None,
                 raises: Optional[Exception] = None, status: str = "ok", code: str = "",
                 descs: Optional[dict[str, str]] = None):
        self.review_obj = review
        self.files = dict(files or {})
        self.descs = dict(descs or {})          # path → the edit's `desc` (v2.1b)
        self.raises = raises
        self.status = status
        self.code = code
        self.calls: list[dict] = []

    def review(self, ws, profile, cfg, *, run_id="", restructure_notes=(), restructure_results=()):
        self.calls.append({"run_id": run_id, "notes": list(restructure_notes),
                           "results": list(restructure_results), "head": ws.head()})
        if self.raises is not None:
            raise self.raises
        rv = (self.review_obj or StructureReview(verdict="ok")).model_copy(deep=True)
        rv.run_id = run_id
        if self.files:
            for k, v in self.files.items():
                ws.write(k, v)
            rv.answer = answer(code=self.code, edits=[write_edit(k, v, self.descs.get(k, ""))
                                                      for k, v in self.files.items()],
                               analysis=rv.analysis or "the poms break the company rules.",
                               solution="set the parent and the encoding.")
            rv.script = self.code
            rv.script_result = ScriptRunResult(status=self.status, changed_files=list(self.files))
            if self.status != "ok":
                ws.discard_uncommitted()
        return rv


def v2_plan(case: CaseType = CaseType.SPRING_BOOT, review: bool = True, compile: bool = False) -> MigrationPlan:
    """transform.rewrite [→ transform.compile (v2.1)] → restructure.apply → restructure.review → verify.build."""
    steps = [
        PlanStep(id="transform.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title="all recipes",
                 recipes=[RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"),
                          RecipeRef(id="org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0")]),
    ]
    if compile:
        steps.append(PlanStep(id="transform.compile", kind=StepKind.BUILD, phase=Phase.TRANSFORM, title="compile",
                              build_goal="clean compile"))
    steps += [
        PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE, title="restructure",
                 params={"mode": "layout"}),
    ]
    if review:
        steps.append(PlanStep(id="restructure.review", kind=StepKind.REVIEW, phase=Phase.RESTRUCTURE,
                              title="structure review"))
    steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                          build_goal="clean package"))
    return MigrationPlan(profile_fingerprint="fp123", case_type=case, steps=steps, notes=["followup: check web.xml"])


def v2_msa_plan() -> MigrationPlan:
    steps = [PlanStep(id=f"transform.rewrite@{svc}", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title="all",
                      recipes=[RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")], target=svc)
             for svc in ("svc-a", "svc-b")]
    steps += [
        PlanStep(id="restructure.msa_pom", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE, title="msa",
                 params={"mode": "msa_pom"}),
        PlanStep(id="restructure.review", kind=StepKind.REVIEW, phase=Phase.RESTRUCTURE, title="review"),
        PlanStep(id="verify.build@svc-a", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package",
                 target="svc-a", params={"service": "svc-a"}),
        PlanStep(id="verify.build@svc-b", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package",
                 target="svc-b", params={"service": "svc-b"}),
    ]
    return MigrationPlan(profile_fingerprint="fp123", case_type=CaseType.MSA, steps=steps)


def journal_events(ws: Workspace) -> list[dict]:
    return [json.loads(l) for l in ws.journal_path.read_text().splitlines() if l.strip()]


def compile_error(file: str = "src/main/java/com/x/Foo.java", msg: str = "cannot find symbol",
                  sig: Optional[str] = None, kind: ErrorKind = ErrorKind.COMPILE, l2: Optional[str] = None,
                  line: Optional[int] = None) -> BuildError:
    """An error with L1 `sig` (default: per file) and L2 `l2` (default `<kind>|x`: one case for every file)."""
    return BuildError(kind=kind, message=msg, file=file, module="demo", line=line,
                      signature=sig or f"{kind.value}|{msg}|{file}",
                      signature_generic=l2 or f"{kind.value}|x")


def case_of(l2: str) -> str:
    """The case id of an L2 signature (the catalog's formula)."""
    return hashlib.sha1(l2.encode("utf-8")).hexdigest()[:10]
