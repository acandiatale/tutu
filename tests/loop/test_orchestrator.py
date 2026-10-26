"""Orchestrator (v2 flow: transform → restructure → structure review → verify + script fix loop): reports, phases,
stop / dry-run, rewrite / restructure failures, aborts, finalize (advisories, handover, cost, flags). `make_orch` /
`files_build` are shared by the other loop / graph tests."""
from __future__ import annotations


import pytest

from sbmig.core.models import (CaseType, ErrorKind, FileChange, MigrationPlan, Phase, PlanStep,
                               RunReport, StepKind)
from sbmig.loop import MissingComponentError, NullKnowledgeBase, Orchestrator, lazy_attr
from sbmig.triage import HandlerRegistry, Triager
from sbmig.triage.handlers import Handler

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, FakeRestructurer, FakeReviewer, FakeRewriteRunner,
                    FakeScriptRunner, bad_build, compile_error, journal_events, make_profile, ok_build, v2_msa_plan,
                    v2_plan, write_fix, write_script)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def files_build(ws, call):
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


class PreHandler(Handler):
    """A v1 pre-emptive handler: registered, never run by the v2 flow."""
    id = "lombok-version-floor"
    description = "fake lombok floor"

    def matches(self, cluster, profile, cfg):
        return False

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        ws.write("pom.xml", "<project><lombok>1.18.38</lombok></project>\n")
        return [FileChange(path="pom.xml", op="modify")]


def fix_all(w, c, ctx):
    return write_script({e.file: "class X { ok }\n" for e in c.errors if e.file}, analysis="fixed")


def make_orch(cfg, ws, *, plan=None, profile=None, build=None, fixer=None, kb=None, rewrite=None,
              restructurer=None, clock=None, runner=None, reviewer=None, **kw):
    reg = HandlerRegistry([PreHandler()])
    kwargs = dict(
        analyzer=lambda w, c: profile or make_profile(),
        planner=lambda p, c, k: plan or v2_plan(),
        restructurer=restructurer or FakeRestructurer(),
        rewrite_runner=rewrite or FakeRewriteRunner(ws),
        build_runner=build or FakeBuildRunner(ws, fn=files_build),
        triager=Triager(cfg, registry=reg), handlers=reg,
        fixer=fixer or FakeFixer(script_fn=fix_all),
        kb=kb or FakeKB(), llm=object(), script_runner=runner or FakeScriptRunner(),
        reviewer=reviewer if reviewer is not None else FakeReviewer(),
    )
    if clock is not None:
        kwargs["clock"] = clock
    kwargs.update(kw)
    return Orchestrator(cfg, ws, **kwargs)


def test_v2_end_to_end(cfg, ws):
    kb = FakeKB()
    build = FakeBuildRunner(ws, fn=files_build)
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("rewritten.txt", ",".join(r.id for r in refs))
                                                       or ["rewritten.txt"]))
    orch = make_orch(cfg, ws, kb=kb, build=build, rewrite=rw,
                     fixer=FakeFixer(fn=lambda w, c, ctx: write_fix(None, "class X { ok }\n")))
    report = orch.run()
    assert report.success and report.final_build.ok
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM, Phase.RESTRUCTURE, Phase.VERIFY]
    assert not report.phases[0].build_ok and report.phases[-1].attempts
    assert build.calls[-1]["label"] == "verify" and build.calls[-1]["goal"] == "clean package"
    assert kb.harvests == [] and report.harvest is None
    log = ws._git("log", "--format=%s").stdout
    assert "handlers:" not in log
    assert "rewrite: transform.rewrite" in log and "restructure: restructure.apply" in log
    assert "web.xml not migrated" in report.manual_followups and "check web.xml" in report.manual_followups
    assert report.llm_usage.calls == 1 and report.llm_usage.input_tokens == 100
    for k in ("report_md", "report_json", "diff", "zip", "journal", "project_dir", "profile", "plan"):
        assert k in report.outputs, k
    md = (ws.run_dir / "report.md").read_text()
    assert "BUILD SUCCESS" in md and "Unresolved errors" in md
    rep = RunReport.model_validate_json((ws.run_dir / "report.json").read_text())
    assert rep.success and rep.structure_review is not None
    ev = {e["event"] for e in journal_events(ws)}
    assert {"preflight", "analyze", "plan", "phase_start", "phase_end", "step_start", "step_end", "rewrite",
            "restructure", "structure_review", "verify", "report", "build"} <= ev
    assert not ev & {"handlers_preemptive", "harvest", "kb_credit"}
    # plan-run recipes are recorded for the triager's bookkeeping
    assert "org.openrewrite.java.migrate.UpgradeToJava21" in orch.triager.engine.recipes_run_any


def test_report_md_follows_report_language(cfg, ws):
    """`report.language: ko` (2026-10-22): the default writer gets the Config, so report.md comes out in Korean;
    report.json stays the same."""
    cfg = cfg.model_copy(deep=True)
    cfg.report.language = "ko"
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run()
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8")
    assert report.success and "빌드 성공" in md and "## 단계별 결과" in md and "## Phases" not in md
    assert (ws.run_dir / "report.json").exists()


def test_msa_targets_passed_through(cfg, ws):
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    rw = FakeRewriteRunner(ws)
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, plan=v2_msa_plan(), build=build, rewrite=rw, restructurer=restr,
                       profile=make_profile(CaseType.MSA)).run()
    assert report.success
    assert [r["module"] for r in rw.runs] == ["svc-a", "svc-b"]
    assert [(c["module"], c["label"]) for c in build.calls] == [("svc-a", "verify-svc-a"), ("svc-b", "verify-svc-b")]
    assert restr.calls == ["msa_pom"]
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM, Phase.RESTRUCTURE, Phase.VERIFY]


def test_failed_fixes_still_end_with_the_final_build(cfg, ws):
    cfg.loop.max_llm_calls = 1
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(compile_error()))
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(FOO, "class F { broken 2 }"))
    report = make_orch(cfg, ws, build=build, fixer=fixer).run()
    assert not report.success
    assert build.calls[-1]["label"] == "verify"
    # L6: the LLM budget ran out → the remaining case is BLOCKED(budget_exhausted), not "unresolved"
    assert report.unresolved == []
    assert [b.reason.value for b in report.blocked] == ["budget_exhausted"] and report.blocked[0].files == [FOO]
    assert "llm-budget-exhausted" in report.flags
    md = (ws.run_dir / "report.md").read_text()
    assert "NOT BUILDING" in md and FOO in md and "Blocked — needs human / library action" in md
    assert "(blocked clusters excluded)" not in md          # nothing unresolved left


def test_verify_skipped_when_disabled_after_failure(cfg, ws):
    cfg.loop.always_final_verify = False
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    report = make_orch(cfg, ws, build=build, restructurer=FakeRestructurer(ok=False)).run()
    assert not report.success and build.calls == []
    assert any("skipped (an earlier phase failed" in n for p in report.phases for n in p.notes)


def test_synthetic_verify_and_time_budget(cfg, ws):
    t = {"now": 0.0}
    cfg.loop.max_total_minutes = 1

    def fix(w, c, ctx):
        t["now"] += 200
        return write_script({e.file: "class X { broken 3 }\n" for e in c.errors})

    plan = v2_plan()
    plan.steps = [s for s in plan.steps if s.kind != StepKind.BUILD]          # no verify step in the plan
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, plan=plan, build=build, fixer=FakeFixer(script_fn=fix), clock=lambda: t["now"]).run()
    assert "time-budget-exhausted" in report.flags
    assert build.calls[-1]["label"] == "verify"                               # synthetic verify still ran
    notes = [n for p in report.phases for n in p.notes]
    assert any("time budget" in n for n in notes)


def test_stop_after_transform(cfg, ws):
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, build=build).run(stop_after=Phase.TRANSFORM)
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM]
    assert "stopped-after-transform" in report.flags and not report.success and build.calls == []
    assert (ws.run_dir / "report.md").exists()


@pytest.mark.parametrize("phase", [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN])
def test_stop_after_early(cfg, ws, phase):
    report = make_orch(cfg, ws).run(stop_after=phase)
    assert report.phases == [] and f"stopped-after-{phase.value}" in report.flags
    assert (report.plan is not None) == (phase == Phase.PLAN)


def test_dry_run_applies_nothing(cfg, ws):
    head = ws.head()
    fixer = FakeFixer(script_fn=fix_all)
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("r.txt", "x") or ["r.txt"]))
    restr = FakeRestructurer()
    reviewer = FakeReviewer()
    report = make_orch(cfg, ws, fixer=fixer, rewrite=rw, restructurer=restr, reviewer=reviewer).run(dry_run=True)
    assert set(ws._git("log", "--format=%s").stdout.split("\n")) <= {"sbmig: as-is snapshot", ""}
    assert not ws.is_dirty() and head
    assert not fixer.contexts and restr.calls == [] and reviewer.calls == []
    assert all(r["dry_run"] for r in rw.runs)
    assert "dry-run" in report.flags and not report.success
    assert "zip" not in report.outputs and report.structure_review.verdict == "skipped"


def test_rewrite_failures(cfg, ws):
    rw = FakeRewriteRunner(ws, ok=False, error="Recipe(s) not found: org.Foo")
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    report = make_orch(cfg, ws, rewrite=rw, build=build).run()
    tr = report.phases[0]
    assert tr.phase == Phase.TRANSFORM and not tr.ok and any("recipe id not found" in n for n in tr.notes)
    assert report.success                                   # verify still decides

    ws2_rw = FakeRewriteRunner(ws, ok=False, error="parse failure")
    report = make_orch(cfg, ws, rewrite=ws2_rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert report.phases[0].ok
    assert any("continuing" in n for n in report.phases[0].notes)


def test_restructure_failure_still_verifies(cfg, ws):
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    reviewer = FakeReviewer()
    report = make_orch(cfg, ws, restructurer=FakeRestructurer(ok=False), build=build, reviewer=reviewer).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert not rs.ok and any("restructure failed" in n for n in rs.notes)
    assert any("restructure.review skipped" in n for n in rs.notes) and reviewer.calls == []
    # round 9c: the final build passed on the as-is tree, but the layout was not produced → not a success (exit 2)
    assert build.calls[-1]["label"] == "verify" and not report.success and "restructure-failed" in report.flags
    assert any("restructure step restructure.apply (layout) failed" in f for f in report.manual_followups)
    from sbmig.cli import run_exit_code
    assert run_exit_code(report) == 2


class CrashRestructurer:
    def apply(self, ws, profile, cfg, *, mode):
        ws.write("half.txt", "x")
        raise RuntimeError("kaboom")


def test_restructure_crash(cfg, ws):
    report = make_orch(cfg, ws, restructurer=CrashRestructurer(),
                       build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert any("crashed" in n for n in rs.notes) and not ws.exists("half.txt")


def test_handler_step_is_skipped_and_test_compile_fallback_in_the_verify_loop(cfg, ws):
    cfg.build.skip_test_compile = False
    cfg.loop.phases["verify"].max_iterations = 1        # one round: the test case is tried, not blocked
    terr = compile_error("src/test/java/T.java", kind=ErrorKind.TEST_COMPILE)
    plan = MigrationPlan(profile_fingerprint="x", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="transform.lombok-version-floor", kind=StepKind.HANDLER, phase=Phase.TRANSFORM, title="h"),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build() if c["skip_test_compile"] else bad_build(terr))
    report = make_orch(cfg, ws, plan=plan, build=build, fixer=FakeFixer()).run()
    assert "test-compile-skipped" in report.flags and report.success
    assert build.calls[-1]["skip_test_compile"] is True and build.calls[-1]["label"] == "verify"
    assert report.unresolved == [terr]
    assert any("-Dmaven.test.skip=true" in f for f in report.manual_followups)
    assert "handlers:" not in ws._git("log", "--format=%s").stdout
    assert any("handler steps are not part of the v2 flow" in n for n in report.phases[0].notes)
    md = (ws.run_dir / "report.md").read_text()
    assert "test-compile-skipped" in md and "T.java" in md


def test_aborts(cfg, ws):
    report = make_orch(cfg, ws, profile=make_profile(build_tool="gradle")).run()
    assert "aborted" in report.flags and "gradle" in report.manual_followups[0]
    assert (ws.run_dir / "report.json").exists()


def test_abort_no_poms_and_internal_error(cfg, tmp_path, ws):
    report = make_orch(cfg, ws, profile=make_profile(poms=False)).run()
    assert "aborted" in report.flags and "no pom.xml" in report.manual_followups[-1]
    assert "tests-not-compiled" not in report.flags                    # no build ran

    def boom(p, c, k):
        raise ValueError("bad plan")

    report = make_orch(cfg, ws, planner=boom).run()
    assert "aborted" in report.flags and "internal error" in report.manual_followups[-1]


def test_missing_component_reports_workstream(cfg, ws, monkeypatch):
    with pytest.raises(MissingComponentError, match="workstream Z"):
        lazy_attr("sbmig.does_not_exist", "X", "Z")
    with pytest.raises(MissingComponentError, match="does not expose"):
        lazy_attr("sbmig.core", "Nope", "Z")
    import sbmig.build as b       # the real preflight runs here: never depend on the host's `mvn -v` (JDK 21 check)
    monkeypatch.setattr(b, "maven_available", lambda *a, **k: (True, "Apache Maven 3.9.10; Java version: 21.0.1"))
    orch = Orchestrator(cfg, ws, analyzer=lambda w, c: (_ for _ in ()).throw(
        MissingComponentError("sbmig.analyze (workstream A) missing")), kb=NullKnowledgeBase(),
        report_writer=lambda r, w: {"x": "y"})
    report = orch.run()
    assert "aborted" in report.flags and "workstream A" in report.manual_followups[-1]
    assert report.outputs["x"] == "y"


def test_lazy_defaults_resolve(cfg, ws):
    """Default collaborators come from the other workstreams when they are importable."""
    orch = Orchestrator(cfg, ws)
    assert orch.triager is not None and orch.handlers.get("lombok-version-floor") is not None
    for name in ("analyzer", "planner"):
        try:
            assert callable(getattr(orch, name))
        except MissingComponentError:  # pragma: no cover - workstream not ready
            pass
    for name in ("kb", "build_runner", "rewrite_runner", "restructurer", "fixer", "script_runner"):
        try:
            assert getattr(orch, name) is not None
        except MissingComponentError:  # pragma: no cover
            pass
    # the lazy script runner builds sbmig.script.ScriptRunner on first use (never raises: an error result instead)
    assert callable(getattr(orch.script_runner, "run", None))


# ---------------------------------------------------------------- library advisories: finalize side (round 4)
def _finding(**kw):
    from sbmig.core.models import AdvisoryFinding

    base = dict(advisory_id="springfox", library="springfox", status="eol", title="Springfox is discontinued",
                policy="recipe", recipes=["org.openrewrite.java.springdoc.SwaggerToSpringDoc"],
                replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui", followup="check the UI path")
    base.update(kw)
    return AdvisoryFinding(**base)


@pytest.fixture()
def advisory_fakes(monkeypatch):
    import sbmig.advisory as adv

    calls = {"verify": 0, "followups": 0}

    def evaluate(profile, cfg, catalog=None):
        return [_finding(), _finding(advisory_id="hystrix", library="hystrix", policy="guide", recipes=[],
                                     title="Hystrix is discontinued", followup="use resilience4j")]

    def verify(findings, view, cfg):
        calls["verify"] += 1
        assert view.exists("pom.xml")
        return [f.model_copy(update={"outcome": "replaced" if f.advisory_id == "springfox" else "still-present"})
                for f in findings]

    def followups(findings):
        calls["followups"] += 1
        return [f"{f.title}: {f.followup}" for f in findings if f.outcome != "replaced"]

    monkeypatch.setattr(adv, "evaluate_advisories", evaluate)
    monkeypatch.setattr(adv, "verify_advisories", verify)
    monkeypatch.setattr(adv, "followups_for", followups)
    return calls


def test_finalize_verifies_advisories_and_builds_handover(cfg, ws, advisory_fakes):
    report = make_orch(cfg, ws).run()
    assert report.success
    assert {f.advisory_id: f.outcome for f in report.advisories} == {"springfox": "replaced",
                                                                      "hystrix": "still-present"}
    assert report.manual_followups.count("Hystrix is discontinued: use resilience4j") == 1
    assert report.handover is not None
    fh = report.handover.from_here
    assert fh[0].startswith("Hystrix is discontinued: use resilience4j")
    assert any(x.startswith("verify springfox → org.springdoc") for x in fh)
    assert not [x for x in fh if x == "Hystrix is discontinued: use resilience4j"][1:]   # no duplicate
    ev = journal_events(ws)
    ver = next(e for e in ev if e["event"] == "advisories_verified")
    assert ver["findings"] == [{"id": "springfox", "policy": "recipe", "outcome": "replaced"},
                               {"id": "hystrix", "policy": "guide", "outcome": "still-present"}]
    ho = next(e for e in ev if e["event"] == "handover")
    assert set(ho) >= {"changes", "carried_over", "from_here"} and ho["from_here"] == len(fh)
    # verified before the ledger / report: the report on disk carries the outcomes and the handover section
    rep = RunReport.model_validate_json((ws.run_dir / "report.json").read_text())
    assert rep.advisories[0].outcome == "replaced" and rep.handover is not None
    md = (ws.run_dir / "report.md").read_text()
    assert md.index("## Handover") < md.index("## Unresolved errors") and "| **hystrix** |" in md
    assert [e["event"] for e in ev].index("advisories_verified") < [e["event"] for e in ev].index("report")


def test_finalize_advisories_dry_run_skips_verify(cfg, ws, advisory_fakes):
    report = make_orch(cfg, ws).run(dry_run=True)
    assert advisory_fakes["verify"] == 0
    assert all(f.outcome == "unknown" for f in report.advisories)
    assert report.handover is not None
    assert not any(e["event"] == "advisories_verified" for e in journal_events(ws))


def test_finalize_advisories_verify_failure(cfg, ws, advisory_fakes, monkeypatch):
    import sbmig.advisory as adv

    def boom(findings, view, cfg):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(adv, "verify_advisories", boom)
    report = make_orch(cfg, ws).run()
    assert report.success and all(f.outcome == "unknown" for f in report.advisories)
    errs = [e for e in journal_events(ws) if e["event"] == "advisories_verified" and "error" in e]
    assert errs and "scan failed" in errs[0]["error"]
    assert "Springfox is discontinued: check the UI path" in report.manual_followups
    assert report.handover is not None


def test_finalize_handover_without_findings(cfg, ws, monkeypatch):
    import sbmig.advisory as adv

    monkeypatch.setattr(adv, "evaluate_advisories", lambda profile, cfg, catalog=None: [])
    report = make_orch(cfg, ws).run()
    assert report.advisories == [] and report.handover is not None
    assert any(e["event"] == "handover" for e in journal_events(ws))


def test_skip_test_compile_policy_flags_and_follow_up(cfg, ws):
    """build.skip_test_compile: every build asks for -Dmaven.test.skip=true (loop builds and the final verify), the
    report carries `tests-not-compiled` and the owning-team follow-up."""
    cfg = cfg.model_copy(deep=True)
    cfg.build.skip_test_compile = True
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, build=build).run()
    assert report.success and "tests-not-compiled" in report.flags
    assert all(c["skip_test_compile"] is True for c in build.calls)
    assert build.calls[-1]["label"] == "verify" and build.calls[-1]["skip_tests"] is False
    assert any("neither compiled nor run" in f for f in report.manual_followups)
    assert any("build.skip_test_compile" in n for n in report.phases[-1].notes)
    assert "test-compile-skipped" not in report.flags


def test_llm_cost_by_model_in_the_report(cfg, ws):
    """Fix-script attempts (their model), the structure review calls (through _BudgetedLLM) and the plan advisor are
    priced per model with llm.pricing; the report carries llm_usage_by_model and llm_cost."""
    from sbmig.core.config import ModelPrice
    from sbmig.core.interfaces import LLMResponse
    cfg = cfg.model_copy(deep=True)
    cfg.llm.pricing = {"gpt-5.6-terra": ModelPrice(input_per_mtok=2.0, output_per_mtok=12.0),
                       "gpt-6-luna": ModelPrice(input_per_mtok=0.1, output_per_mtok=0.5)}
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(None, "class X { ok }\n", model="gpt-5.6-terra"))

    class Inner:
        def complete(self, *a, **k):
            return LLMResponse(content="NONE", model="gpt-6-luna", input_tokens=1_000, output_tokens=10)

    class Reviewer:                       # a review call through the budgeted client the orchestrator hands over
        def __init__(self, cfg, llm, runner, kb):
            self.llm = llm

        def review(self, ws, profile, cfg, **kw):
            from sbmig.core.models import StructureReview
            self.llm.complete("x")
            return StructureReview(verdict="ok")
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build), fixer=fixer, llm=Inner(), reviewer=Reviewer)
    report = orch.run()
    by = {u.model: u for u in report.llm_usage_by_model}
    assert by["gpt-5.6-terra"].calls == 1 and by["gpt-5.6-terra"].input_tokens == 100
    assert by["gpt-6-luna"].calls == 1 and by["gpt-6-luna"].input_tokens == 1_000
    c = report.llm_cost
    assert c is not None and c.unpriced_models == [] and c.currency == "USD"
    terra = next(m for m in c.by_model if m.model == "gpt-5.6-terra")
    assert terra.cost == round(100 / 1e6 * 2.0 + 20 / 1e6 * 12.0, 6)
    assert c.total == round(terra.cost + (1_000 / 1e6 * 0.1 + 10 / 1e6 * 0.5), 6)
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8")
    assert "## LLM usage and cost" in md and "| `gpt-6-luna` | 1 | 1,000 | 10 |" in md


def test_rewrite_parse_problems_flag_note_and_journal(cfg, ws):
    """2026-10-22: files the rewrite plugin could not parse are flagged (recipes that need types skipped them)."""
    class ParseProblemRunner(FakeRewriteRunner):
        def run(self, recipes, **kw):
            res = super().run(recipes, **kw)
            if kw.get("label") == "transform.rewrite":
                res.parse_problems = ["src/main/java/com/x/vendor/Legacy.java", "src/main/java/com/x/Old.java"]
            return res

    orch = make_orch(cfg, ws, rewrite=ParseProblemRunner(ws))
    report = orch.run()
    assert report.success
    assert "rewrite-parse-problems" in report.flags
    tr = next(p for p in report.phases if p.phase == Phase.TRANSFORM)
    note = next(n for n in tr.notes if "could not parse 2 source file(s)" in n)
    assert "vendor/Legacy.java" in note and "fix scripts" in note
    ev = [e for e in journal_events(ws) if e["event"] == "rewrite_parse_problems"]
    assert len(ev) == 1 and ev[0]["step"] == "transform.rewrite" and len(ev[0]["files"]) == 2
