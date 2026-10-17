"""Fake collaborators for loop/orchestrator tests (no mvn, no network, no LLM)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import (BuildError, BuildResult, CaseType, ErrorKind, FileChange, FixProposal,
                               HarvestResult, KBMatch, LLMUsage, MigrationPlan, Phase, PlanStep, PomInfo,
                               ProjectProfile, RecipeRef, RewriteResult, StepKind)
from sbmig.core.workspace import Workspace

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

    def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False) -> BuildResult:
        call = {"goal": goal, "module": module, "label": label, "skip_test_compile": skip_test_compile,
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
    """propose → scripted proposals (or a function); apply → writes files (search/replace or create)."""

    def __init__(self, proposals: Optional[list[FixProposal]] = None,
                 fn: Optional[Callable[[Workspace, Any, Any], FixProposal]] = None):
        self.proposals = list(proposals or [])
        self.fn = fn
        self.contexts: list[Any] = []
        self.applied: list[list[FileChange]] = []

    def propose(self, ws, cluster, ctx) -> FixProposal:
        self.contexts.append(ctx)
        if self.fn is not None:
            return self.fn(ws, cluster, ctx)
        if self.proposals:
            return self.proposals.pop(0)
        return FixProposal(changes=[], analysis="nothing", model="fake", llm_usage=LLMUsage(model="fake", calls=1))

    def apply(self, ws, changes):
        self.applied.append(list(changes))
        done, errs = [], []
        for ch in changes:
            if ch.op == "create":
                ws.write(ch.path, ch.content or "")
                done.append(ch)
            elif ch.op == "delete":
                ws.delete(ch.path)
                done.append(ch)
            elif ch.op == "modify":
                if not ws.exists(ch.path):
                    errs.append(f"{ch.path}: missing")
                    continue
                text = ws.read(ch.path)
                if ch.search is None or text.count(ch.search) != 1:
                    errs.append(f"{ch.path}: search not found")
                    continue
                ws.write(ch.path, text.replace(ch.search, ch.replace or ""))
                done.append(ch)
        return done, errs


def write_fix(path: str, content: str, analysis: str = "fix", model: str = "fake-model") -> FixProposal:
    return FixProposal(changes=[FileChange(path=path, op="create", content=content)], analysis=analysis,
                       model=model, llm_usage=LLMUsage(model=model, calls=1, input_tokens=100, output_tokens=20))


class FakeKB:
    def __init__(self, match: Optional[Callable[[Any], KBMatch]] = None, raise_match: bool = False):
        self._match = match
        self.raise_match = raise_match
        self.harvests: list[Phase] = []
        self.credits: list[tuple[str, bool]] = []

    def match(self, cluster, ws, profile) -> KBMatch:
        if self.raise_match:
            raise RuntimeError("kb down")
        return self._match(cluster) if self._match else KBMatch()

    def active_recipe_files(self):
        return []

    def active_recipe_ids(self):
        return []

    def record_case(self, case):
        return case.id

    def credit_recipe(self, recipe_id, success, project_fingerprint):
        self.credits.append((recipe_id, success))

    def harvest(self, phase_result, profile, ws, llm=None) -> HarvestResult:
        self.harvests.append(phase_result.phase)
        ok_attempts = [a.id for a in phase_result.attempts if a.outcome in ("improved", "build_ok")]
        return HarvestResult(cases_created=[f"case-{a}" for a in ok_attempts], notes=[f"harvested {phase_result.phase.value}"])

    def list_recipes(self, status=None):
        return []

    def promote(self, recipe_id):  # pragma: no cover
        raise KeyError(recipe_id)

    def reject(self, recipe_id, reason=""):  # pragma: no cover
        raise KeyError(recipe_id)

    def reindex(self):
        return 0


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
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[str] = []

    def apply(self, ws, profile, cfg, *, mode):
        self.calls.append(mode)
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


def journal_events(ws: Workspace) -> list[dict]:
    return [json.loads(l) for l in ws.journal_path.read_text().splitlines() if l.strip()]


def compile_error(file: str = "src/main/java/com/x/Foo.java", msg: str = "cannot find symbol",
                  sig: Optional[str] = None, kind: ErrorKind = ErrorKind.COMPILE) -> BuildError:
    return BuildError(kind=kind, message=msg, file=file, module="demo", signature=sig or f"{kind.value}|{msg}|{file}",
                      signature_generic=f"{kind.value}|x")
