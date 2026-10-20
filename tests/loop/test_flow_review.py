"""Regressions from the 2026-10-20 end-to-end flow review (docs/PLAN.md "round 7a"): the nodes take the right path
in the odd cases — KB patch replay without an LLM, the final build after an internal abort, LLM transport
classification, attempt ids across re-rounds, KB credit without a build, budgeted harvest calls, ledger lines of
aborted runs, `stop_after` validation."""
from __future__ import annotations

import pytest

from sbmig.core.models import (CaseType, DecisionKind, FileChange, FixProposal, KBCase, KBMatch, KBRecipe, LLMUsage,
                               MigrationPlan, Phase, PlanStep, RecipeRef, StepKind)
from sbmig.core.workspace import Workspace
from sbmig.loop import MissingComponentError, parse_stop_after
from sbmig.loop.fixloop import is_llm_transport_error

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, FakeRestructurer, FakeRewriteRunner, boot_plan, make_project, write_fix
from .test_orchestrator import FOO, files_build, make_orch


@pytest.fixture()
def ws(tmp_path):
    w = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


# ------------------------------------------------------------------ B1: KB patch replay needs no LLM
def test_kb_patch_replay_works_without_an_llm(cfg, ws, monkeypatch):
    cfg = cfg.model_copy(deep=True)
    cfg.llm.provider = "company"
    cfg.llm.base_url = "http://gw.invalid/v1/chat"
    monkeypatch.delenv("LLM_API_KEY", raising=False)          # → make_llm_client raises LLMConfigError
    monkeypatch.setenv("SBMIG_ENV_FILE", str(ws.run_dir / "none.env"))
    case = KBCase(id="c1", signatures=[], changes=[FileChange(path=FOO, op="modify", search="x", replace="y")],
                  summary="replay")

    def match(cluster):
        return KBMatch(replayable=[case.model_copy(update={"changes": [FileChange(path=cluster.file, op="modify",
                                                                                 search="broken", replace="ok")]})])
    orch = make_orch(cfg, ws, kb=FakeKB(match=match))
    orch._fixer = None          # the production default: LazyFixer(LLMFixer(cfg, make_llm_client(cfg)))
    orch._llm = None
    report = orch.run()
    kbp = [a for pr in report.phases for a in pr.attempts if a.decision.kind == DecisionKind.KB_PATCH]
    assert kbp and all(a.applied and a.outcome != "apply_failed" for a in kbp)
    assert report.success and report.llm_usage.calls == 0


# ------------------------------------------------------------------ B2: internal error after plan → final build still runs
def test_internal_error_still_attempts_the_final_build(cfg, ws):
    class Boom(FakeRestructurer):
        def apply(self, *a, **k):
            raise MissingComponentError("sbmig.restructure.Restructurer cannot be imported")
    build = FakeBuildRunner(ws, fn=files_build)
    orch = make_orch(cfg, ws, build=build, restructurer=Boom())
    report = orch.run()
    assert "aborted" in report.flags and any("missing component" in f for f in report.manual_followups)
    assert build.calls[-1]["label"] == "verify" and build.calls[-1]["goal"] == "clean package"
    assert report.final_build is not None and report.phases[-1].phase == Phase.VERIFY
    assert any("after the run aborted" in n for n in report.phases[-1].notes)


def test_no_final_build_after_abort_when_disabled_or_dry_run(cfg, ws):
    class Boom(FakeRestructurer):
        def apply(self, *a, **k):
            raise MissingComponentError("sbmig.restructure.Restructurer cannot be imported")
    cfg = cfg.model_copy(deep=True)
    cfg.loop.always_final_verify = False
    build = FakeBuildRunner(ws, fn=files_build)
    orch = make_orch(cfg, ws, build=build, restructurer=Boom())
    report = orch.run()
    assert "aborted" in report.flags and "verify" not in [c["label"] for c in build.calls]
    assert report.final_build is None and report.unresolved == []
    orch._dry_run, orch.cfg.loop.always_final_verify = True, True
    orch._final_build_after_abort(report)
    assert "verify" not in [c["label"] for c in build.calls]


# ------------------------------------------------------------------ B3: transport vs. request/response classification
@pytest.mark.parametrize("analysis,transport", [
    ("llm-error: invalid JSON from LLM: Expecting value: line 1 column 1 (char 0)", True),      # HTML 200 from a proxy
    ("llm-error: llm.response_path 'data.x' not found in the response (KeyError: 'x')", True),  # config error
    ("llm-error: cannot find the reply text in the response; set LLM_RESPONSE_PATH", True),
    ("llm-error: LLM request failed after 4 attempt(s) (status=503): Service Unavailable", True),
    ("llm-error: LLM request failed (status=400): {\"error\":\"maximum context length exceeded\"}", False),
    ("llm-error: LLM request failed (status=413): payload too large", False),
    ("llm-error: the model did not follow the protocol", False),
    ("llm-error: content filtered by the gateway (finish_reason=content_filter); this request only", False),  # round 9
    ("fixed it", False),
])
def test_transport_classification(analysis, transport):
    assert is_llm_transport_error(analysis) is transport


# ------------------------------------------------------------------ B4: attempt ids stay unique across re-rounds
def test_reround_keeps_attempt_ids_unique(cfg, ws):
    calls = {"n": 0}

    def fx(w, c, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return FixProposal(changes=[], analysis="need more", model="fake", llm_usage=LLMUsage(model="fake", calls=1))
        return write_fix(c.file, "class X { ok }\n")
    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="l", build_goal="compile",
                 max_iterations=3),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    cfg = cfg.model_copy(deep=True)
    cfg.loop.max_clusters_per_iteration = 1
    report = make_orch(cfg, ws, plan=plan, fixer=FakeFixer(fn=fx), pre=[]).run()
    ids = [a.id for a in report.phases[0].attempts]
    assert len(ids) >= 2 and len(ids) == len(set(ids))


# ------------------------------------------------------------------ R1: a phase without a build credits nothing
class KBRecipesKB(FakeKB):
    def list_recipes(self, status=None):
        return [KBRecipe(id="com.company.kb.R", status="active", path="/tmp/r.yml")]


class AppliedRewrite(FakeRewriteRunner):
    def run(self, recipes, **kw):
        res = super().run(recipes, **kw)
        if "com.company.kb.R" in [r.id for r in recipes]:
            return res.model_copy(update={"recipes_applied": {"com.company.kb.R": [FOO]}})
        return res


def test_phase_without_a_loop_does_not_credit_kb_recipes(cfg, ws):
    steps = [s for s in boot_plan().steps if s.id != "spring.loop"]
    for s in steps:
        if s.id == "spring.rewrite":
            s.recipes = [*s.recipes, RecipeRef(id="com.company.kb.R", source="kb")]
    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=steps)
    kb = KBRecipesKB()
    report = make_orch(cfg, ws, plan=plan, kb=kb, rewrite=AppliedRewrite(ws)).run()
    assert report.success
    assert not [c for c in kb.credits if c[0] == "com.company.kb.R"]


def test_phase_with_a_loop_credits_kb_recipes(cfg, ws):
    steps = boot_plan().steps
    for s in steps:
        if s.id == "spring.rewrite":
            s.recipes = [*s.recipes, RecipeRef(id="com.company.kb.R", source="kb")]
    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=steps)
    kb = KBRecipesKB()
    report = make_orch(cfg, ws, plan=plan, kb=kb, rewrite=AppliedRewrite(ws)).run()
    assert report.success and ("com.company.kb.R", True) in kb.credits


# ------------------------------------------------------------------ R5: harvest LLM calls are budgeted
class CountingLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        from sbmig.core.interfaces import LLMResponse
        return LLMResponse(content="NONE", model="fake")


class HarvestingKB(FakeKB):
    def harvest(self, phase_result, profile, ws, llm=None):
        if llm is not None:
            llm.complete([])              # one recipe-candidate call per phase
        return super().harvest(phase_result, profile, ws, llm)


def test_harvest_recipe_candidate_calls_are_charged_to_the_budget(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.kb.generate_recipe_candidates = True
    cfg.kb.harvest = True
    llm = CountingLLM()
    orch = make_orch(cfg, ws, kb=HarvestingKB(), llm=llm)
    report = orch.run()
    fix_calls = sum(a.llm_usage.calls for pr in report.phases for a in pr.attempts)
    assert llm.calls == 3                                   # jdk21 / spring / restructure harvests
    assert orch.budget.llm_calls == fix_calls + llm.calls == report.llm_usage.calls


def test_harvest_skips_candidates_when_the_budget_is_exhausted(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.kb.generate_recipe_candidates = True
    cfg.loop.max_llm_calls = 1
    llm = CountingLLM()
    report = make_orch(cfg, ws, kb=HarvestingKB(), llm=llm).run()
    assert llm.calls == 0 and "llm-budget-exhausted" in report.flags


# ------------------------------------------------------------------ R7: no `unresolved` ledger lines without a final build
def test_record_run_skips_unresolved_without_a_final_build(cfg, tmp_path):
    from sbmig.core.models import BuildError, ErrorKind, RunReport, utcnow
    from sbmig.kb.store import FileKnowledgeBase
    from .fakes import bad_build, make_profile
    cfg = cfg.model_copy(deep=True)
    cfg.kb.harvest = cfg.kb.ledger = True
    kb = FileKnowledgeBase(cfg, tmp_path / "kb")
    ws = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="r1")
    err = BuildError(kind=ErrorKind.COMPILE, file=FOO, message="cannot find symbol", signature="compile|x|Foo.java")
    base = dict(run_id="r1", project_name="p", case_type=CaseType.SPRING_BOOT, started_at=utcnow(), unresolved=[err])
    assert kb.record_run(RunReport(**base, flags=["stopped-after-jdk21"]), make_profile(), ws).ledger_entries == 0
    assert kb.record_run(RunReport(**base, flags=["aborted"], final_build=bad_build(goal="package")),
                         make_profile(), ws).ledger_entries == 0
    assert kb.record_run(RunReport(**base, final_build=bad_build(goal="package")),
                         make_profile(), ws).ledger_entries == 1


# ------------------------------------------------------------------ N1: stop_after validation (CLI / web / api)
def test_parse_stop_after():
    assert parse_stop_after(None) is None and parse_stop_after("") is None
    assert parse_stop_after(" Spring ") is Phase.SPRING and parse_stop_after(Phase.VERIFY) is Phase.VERIFY
    for bad in ("report", "nope", 3):
        with pytest.raises(ValueError, match="stop_after must be one of: intake, analyze, plan, jdk21, spring, "
                                             "restructure, verify"):
            parse_stop_after(bad)


# ------------------------------------------------------------------ N5: a reused orchestrator starts clean
def test_orchestrator_reuse_starts_from_a_clean_state(cfg, ws):
    orch = make_orch(cfg, ws)
    first = orch.run(stop_after=Phase.JDK21)
    assert first.final_build is None
    second = orch.run()
    assert second is not first and second.final_build is not None and second.success
    assert len(second.phases) == 4 and "stopped-after-jdk21" not in second.flags
