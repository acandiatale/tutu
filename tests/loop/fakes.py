"""Fake collaborators for loop/orchestrator tests (no mvn, no network, no LLM).

v2 (flow-v2): `FakeScriptRunner` runs a fix script IN-PROCESS against a tiny helper `p` bound to the workspace (the
real `sbmig.script.ScriptRunner` uses a `python -I` process); `FakeScriptStore` is an in-memory `ScriptStoreLike`;
`FakeFixer.propose_script` answers with scripted `ScriptProposal`s, a `script_fn`, or — for tests written against
SEARCH/REPLACE proposals — turns the `FixProposal` of `fn` / `proposals` into a script (`script_for`).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from sbmig.core.interfaces import RestructureResult
from pydantic import BaseModel, Field

from sbmig.core.models import (BuildError, BuildResult, CaseType, ErrorKind, FileChange, KBMatch,
                               LLMUsage, MigrationPlan, Phase, PlanStep, PomInfo, ProjectProfile, RecipeRef,
                               RewriteResult, ScriptMeta, ScriptProposal, ScriptRunResult, StepKind, StructureReview)
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
        return ScriptProposal(code="", analysis="nothing", model="fake", error="no <script> block",
                              llm_usage=LLMUsage(model="fake", calls=1))


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


class FakeScriptStore:
    """In-memory ScriptStoreLike (best = active versions with a success: most success projects, then successes −
    failures, then the newest; same code → the existing version)."""

    def __init__(self):
        self.data: dict[str, list[tuple[ScriptMeta, str]]] = {}
        self.credits: list[tuple[str, int, str, str]] = []      # (case, version, outcome, project)
        self.added: list[ScriptMeta] = []

    def versions(self, case_id):
        return [m for m, _ in self.data.get(case_id, [])]

    def get(self, case_id, version):
        return next(((m, c) for m, c in self.data.get(case_id, []) if m.version == version), None)

    def best(self, case_id, *, limit=1):
        ok = [(m, c) for m, c in self.data.get(case_id, []) if m.status == "active" and m.successes >= 1]
        ok.sort(key=lambda mc: (len(mc[0].success_projects), mc[0].successes - mc[0].failures, mc[0].version),
                reverse=True)
        return ok[:limit]

    def latest(self, case_id):
        act = [(m, c) for m, c in self.data.get(case_id, []) if m.status == "active"]
        return act[-1] if act else None

    def add(self, case_id, signature_generic, code, *, parent=None, analysis="", model="", run_id="", project="",
            source="llm"):
        sha = hashlib.sha1(code.encode()).hexdigest()
        for m, _c in self.data.get(case_id, []):
            if m.sha1 == sha:
                return m
        n = len(self.data.get(case_id, [])) + 1
        meta = ScriptMeta(case_id=case_id, signature_generic=signature_generic, version=n, parent=parent, sha1=sha,
                          run_id=run_id, project=project, model=model, source=source, analysis=analysis)
        self.data.setdefault(case_id, []).append((meta, code))
        self.added.append(meta)
        return meta

    def seed(self, case_id, code, *, successes=1, projects=("p0",), failures=0, analysis="stored fix",
             signature_generic="") -> ScriptMeta:
        meta = self.add(case_id, signature_generic, code, analysis=analysis)
        meta.successes, meta.failures, meta.success_projects = successes, failures, list(projects)
        self.added.remove(meta)
        return meta

    def credit(self, case_id, version, outcome, *, project="", run_id=""):
        self.credits.append((case_id, version, outcome, project))
        hit = self.get(case_id, version)
        if hit is None:
            return
        m = hit[0]
        if outcome in ("improved", "build_ok"):
            m.successes += 1
            if project and project not in m.success_projects:
                m.success_projects.append(project)
        elif outcome == "partial":
            m.partials += 1
        elif outcome in ("no_change", "regressed", "reverted", "apply_failed", "blocked"):
            m.failures += 1
        m.last_outcome = outcome

    def case_ids(self):
        return sorted(self.data)


class FakeKB:
    """What the v2 flow calls on a KB: `match` (case hits only), `scripts` (ScriptStoreLike), `record_review`,
    `append_error_cases`."""

    def __init__(self, match: Optional[Callable[[Any], KBMatch]] = None, raise_match: bool = False,
                 scripts: Any = "default"):
        self._match = match
        self.raise_match = raise_match
        self.scripts = FakeScriptStore() if scripts == "default" else scripts     # v2: ScriptStoreLike
        self.reviews: list[StructureReview] = []
        self.appended: list[tuple[list, list]] = []

    def record_review(self, review) -> None:
        self.reviews.append(review)

    def append_error_cases(self, errors, attempts) -> int:
        self.appended.append((list(errors), list(attempts)))
        return len(errors) + len(attempts)

    def match(self, cluster, ws, profile) -> KBMatch:
        if self.raise_match:
            raise RuntimeError("kb down")
        return self._match(cluster) if self._match else KBMatch()


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
    """StructureReviewerLike: returns `review` (default verdict ok); `files` → written like a review script would (left
    uncommitted, script_result ok); `raises` → an exception; records the call's arguments."""

    def __init__(self, review: Optional[StructureReview] = None, files: Optional[dict[str, str]] = None,
                 raises: Optional[Exception] = None, status: str = "ok"):
        self.review_obj = review
        self.files = dict(files or {})
        self.raises = raises
        self.status = status
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
            rv.script = rv.script or "def fix(p):\n    pass\n"
            rv.script_result = ScriptRunResult(status=self.status, changed_files=list(self.files))
            if self.status != "ok":
                ws.discard_uncommitted()
        return rv


def v2_plan(case: CaseType = CaseType.SPRING_BOOT, review: bool = True) -> MigrationPlan:
    """transform.rewrite → restructure.apply → restructure.review → verify.build (docs/INTERFACES.md "Plan (L)")."""
    steps = [
        PlanStep(id="transform.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title="all recipes",
                 recipes=[RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"),
                          RecipeRef(id="org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0")]),
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
