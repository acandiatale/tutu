from __future__ import annotations


import pytest

from sbmig.core.models import (CaseType, ErrorKind, FileChange, MigrationPlan, Phase, PlanStep,
                               RunReport, StepKind)
from sbmig.loop import MissingComponentError, NullKnowledgeBase, Orchestrator, lazy_attr
from sbmig.triage import HandlerRegistry, Triager
from sbmig.triage.handlers import Handler

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, FakeRestructurer, FakeRewriteRunner, bad_build, boot_plan,
                    compile_error, journal_events, make_profile, msa_plan, ok_build, write_fix)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def files_build(ws, call):
    errs = [compile_error(f) for f in (FOO, BAR) if "broken" in ws.read(f)]
    return bad_build(*errs) if errs else ok_build()


class PreHandler(Handler):
    id = "lombok-version-floor"
    description = "fake lombok floor"

    def matches(self, cluster, profile, cfg):
        return False

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        ws.write("pom.xml", "<project><lombok>1.18.38</lombok></project>\n")
        return [FileChange(path="pom.xml", op="modify")]


class SpringPreHandler(PreHandler):
    id = "javax-servlet-api-to-jakarta"
    preemptive_phase = Phase.SPRING

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        ws.write("servlet.txt", "jakarta\n")
        return [FileChange(path="servlet.txt", op="modify")]


def make_orch(cfg, ws, *, plan=None, profile=None, build=None, fixer=None, kb=None, rewrite=None,
              restructurer=None, pre=None, clock=None, **kw):
    reg = HandlerRegistry([PreHandler(), SpringPreHandler()])
    kwargs = dict(
        analyzer=lambda w, c: profile or make_profile(),
        planner=lambda p, c, k: plan or boot_plan(),
        pre_handlers=lambda p, c: pre if pre is not None else ["lombok-version-floor", "javax-servlet-api-to-jakarta"],
        restructurer=restructurer or FakeRestructurer(),
        rewrite_runner=rewrite or FakeRewriteRunner(ws),
        build_runner=build or FakeBuildRunner(ws, fn=files_build),
        triager=Triager(cfg, registry=reg), handlers=reg,
        fixer=fixer or FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "class X { ok }\n")),
        kb=kb or FakeKB(), llm=object(),
    )
    if clock is not None:
        kwargs["clock"] = clock
    kwargs.update(kw)
    return Orchestrator(cfg, ws, **kwargs)


def test_spring_boot_end_to_end(cfg, ws):
    kb = FakeKB()
    build = FakeBuildRunner(ws, fn=files_build)
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("rewritten.txt", ",".join(r.id for r in refs)) or ["rewritten.txt"]))
    orch = make_orch(cfg, ws, kb=kb, build=build, rewrite=rw)
    report = orch.run()
    assert report.success and report.final_build.ok
    assert [p.phase for p in report.phases] == [Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE, Phase.VERIFY]
    assert report.phases[0].build_ok and report.phases[0].attempts
    assert build.calls[-1]["label"] == "verify" and build.calls[-1]["goal"] == "clean package"
    assert kb.harvests == [Phase.JDK21, Phase.SPRING, Phase.RESTRUCTURE]
    assert report.harvest and report.harvest.cases_created
    log = ws._git("log", "--format=%s").stdout
    assert "handlers: lombok-version-floor" in log and "handlers: javax-servlet-api-to-jakarta" in log
    assert "rewrite: jdk21.rewrite" in log and "restructure: restructure.apply" in log
    # jakarta handler ran after the spring rewrite, jdk21 handler before the first rewrite
    subjects = log.splitlines()[::-1]
    assert subjects.index("handlers: lombok-version-floor") < subjects.index("rewrite: jdk21.rewrite")
    assert subjects.index("rewrite: spring.rewrite") < subjects.index("handlers: javax-servlet-api-to-jakarta")
    assert "web.xml not migrated" in report.manual_followups and "check web.xml" in report.manual_followups
    assert report.llm_usage.calls == 2 and report.llm_usage.input_tokens == 200
    for k in ("report_md", "report_json", "diff", "zip", "journal", "project_dir", "profile", "plan"):
        assert k in report.outputs, k
    md = (ws.run_dir / "report.md").read_text()
    assert "BUILD SUCCESS" in md and "Unresolved errors" in md
    rep = RunReport.model_validate_json((ws.run_dir / "report.json").read_text())
    assert rep.success
    ev = {e["event"] for e in journal_events(ws)}
    assert {"preflight", "analyze", "plan", "phase_start", "phase_end", "step_start", "step_end", "rewrite",
            "handlers_preemptive", "restructure", "verify", "harvest", "report", "build"} <= ev
    # plan-run recipes are not proposed again by triage
    assert "org.openrewrite.java.migrate.UpgradeToJava21" in orch.triager.engine.recipes_run_any


def test_msa_targets_passed_through(cfg, ws):
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    rw = FakeRewriteRunner(ws)
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, plan=msa_plan(), build=build, rewrite=rw, restructurer=restr,
                       profile=make_profile(CaseType.MSA)).run()
    assert report.success
    assert [r["module"] for r in rw.runs] == ["svc-a", "svc-b"]
    loop_modules = [c["module"] for c in build.calls if not c["label"].startswith("verify")]
    assert loop_modules == ["svc-a", "svc-a", "svc-b", "svc-b"]
    verify = [c for c in build.calls if c["label"].startswith("verify")]
    assert [(c["module"], c["label"]) for c in verify] == [("svc-a", "verify-svc-a"), ("svc-b", "verify-svc-b")]
    assert restr.calls == ["msa_pom"]
    assert [p.phase for p in report.phases][:3] == [Phase.JDK21, Phase.SPRING, Phase.JDK21]


def test_failed_loops_still_verify(cfg, ws):
    cfg.loop.max_llm_calls = 1
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(compile_error()))
    report = make_orch(cfg, ws, build=build, fixer=FakeFixer(fn=lambda w, c, ctx: write_fix(FOO, "class F { broken 2 }"))).run()
    assert not report.success
    assert build.calls[-1]["label"] == "verify"
    # L6: the LLM budget ran out → the remaining LLM-only cluster is BLOCKED(budget_exhausted), not "unresolved"
    assert report.unresolved == []
    assert [b.reason.value for b in report.blocked] == ["budget_exhausted"] and report.blocked[0].files == [FOO]
    assert "llm-budget-exhausted" in report.flags
    assert not report.phases[0].ok
    md = (ws.run_dir / "report.md").read_text()
    assert "NOT BUILDING" in md and FOO in md and "Blocked — needs human / library action" in md
    assert "(blocked clusters excluded)" not in md          # nothing unresolved left


def test_verify_skipped_when_disabled_after_failure(cfg, ws):
    cfg.loop.always_final_verify = False
    cfg.loop.max_llm_calls = 0
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(compile_error()))
    report = make_orch(cfg, ws, build=build).run()
    assert not report.success and all(c["label"] != "verify" for c in build.calls)


def test_synthetic_verify_and_time_budget(cfg, ws):
    t = {"now": 0.0}
    cfg.loop.max_total_minutes = 1

    def fix(w, c, ctx):
        t["now"] += 200
        return write_fix(c.file, "class X { broken 3 }\n")

    plan = boot_plan()
    plan.steps = [s for s in plan.steps if s.kind != StepKind.BUILD]          # no verify step in the plan
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, plan=plan, build=build, fixer=FakeFixer(fn=fix), clock=lambda: t["now"]).run()
    assert "time-budget-exhausted" in report.flags
    assert build.calls[-1]["label"] == "verify"                               # synthetic verify still ran
    notes = [n for p in report.phases for n in p.notes]
    assert any("time budget" in n for n in notes)


def test_stop_after_and_dry_run(cfg, ws):
    build = FakeBuildRunner(ws, fn=files_build)
    report = make_orch(cfg, ws, build=build).run(stop_after=Phase.JDK21)
    assert [p.phase for p in report.phases] == [Phase.JDK21]
    assert "stopped-after-jdk21" in report.flags and not report.success
    assert (ws.run_dir / "report.md").exists()


@pytest.mark.parametrize("phase", [Phase.INTAKE, Phase.ANALYZE, Phase.PLAN])
def test_stop_after_early(cfg, ws, phase):
    report = make_orch(cfg, ws).run(stop_after=phase)
    assert report.phases == [] and f"stopped-after-{phase.value}" in report.flags
    assert (report.plan is not None) == (phase == Phase.PLAN)


def test_dry_run_applies_nothing(cfg, ws):
    head = ws.head()
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(c.file, "x"))
    rw = FakeRewriteRunner(ws, effect=lambda w, refs: (w.write("r.txt", "x") or ["r.txt"]))
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, fixer=fixer, rewrite=rw, restructurer=restr).run(dry_run=True)
    assert set(ws._git("log", "--format=%s").stdout.split("\n")) <= {"sbmig: as-is snapshot", ""}
    assert not ws.is_dirty() and head
    assert not fixer.applied and restr.calls == []
    assert all(r["dry_run"] for r in rw.runs)
    assert "dry-run" in report.flags and not report.success
    assert "zip" not in report.outputs
    ev = journal_events(ws)
    assert any(e["event"] == "decision" for e in ev)
    assert any(e["event"] == "handlers_preemptive" and e["dry_run"] for e in ev)


def test_rewrite_failures(cfg, ws):
    rw = FakeRewriteRunner(ws, ok=False, error="Recipe(s) not found: org.Foo")
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    report = make_orch(cfg, ws, rewrite=rw, build=build).run()
    jdk = report.phases[0]
    assert not jdk.ok and any("recipe id not found" in n for n in jdk.notes)
    assert any("skipped: phase aborted" in n for n in jdk.notes)
    assert report.success                                   # verify still decides

    ws2_rw = FakeRewriteRunner(ws, ok=False, error="parse failure")
    report = make_orch(cfg, ws, rewrite=ws2_rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert report.phases[0].ok
    assert any("continuing" in n for n in report.phases[0].notes)


def test_restructure_failure_still_verifies(cfg, ws):
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    report = make_orch(cfg, ws, restructurer=FakeRestructurer(ok=False), build=build).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert not rs.ok and any("restructure failed" in n for n in rs.notes)
    assert any("restructure.loop skipped" in n for n in rs.notes)
    assert build.calls[-1]["label"] == "verify" and report.success


class CrashRestructurer:
    def apply(self, ws, profile, cfg, *, mode):
        ws.write("half.txt", "x")
        raise RuntimeError("kaboom")


def test_restructure_crash(cfg, ws):
    report = make_orch(cfg, ws, restructurer=CrashRestructurer(),
                       build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert any("crashed" in n for n in rs.notes) and not ws.exists("half.txt")


def test_handler_step_and_test_compile_flag(cfg, ws):
    terr = compile_error("src/test/java/T.java", kind=ErrorKind.TEST_COMPILE)
    plan = MigrationPlan(profile_fingerprint="x", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="jdk21.lombok-version-floor", kind=StepKind.HANDLER, phase=Phase.JDK21, title="h"),
        PlanStep(id="spring.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.SPRING, title="l",
                 build_goal="test-compile", max_iterations=1),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build() if c["skip_test_compile"] else bad_build(terr))
    report = make_orch(cfg, ws, plan=plan, build=build, pre=[]).run()
    assert "test-compile-skipped" in report.flags and report.success
    assert build.calls[-1]["skip_test_compile"] is True
    assert report.unresolved == [terr]
    assert any("-Dmaven.test.skip=true" in f for f in report.manual_followups)
    assert "handlers: lombok-version-floor" in ws._git("log", "--format=%s").stdout
    md = (ws.run_dir / "report.md").read_text()
    assert "test-compile-skipped" in md and "T.java" in md


def test_aborts(cfg, ws):
    report = make_orch(cfg, ws, profile=make_profile(build_tool="gradle")).run()
    assert "aborted" in report.flags and "gradle" in report.manual_followups[0]
    assert (ws.run_dir / "report.json").exists()


def test_abort_no_poms_and_internal_error(cfg, tmp_path, ws):
    report = make_orch(cfg, ws, profile=make_profile(poms=False)).run()
    assert "aborted" in report.flags and "no pom.xml" in report.manual_followups[-1]

    def boom(p, c, k):
        raise ValueError("bad plan")

    report = make_orch(cfg, ws, planner=boom).run()
    assert "aborted" in report.flags and "internal error" in report.manual_followups[-1]


def test_missing_component_reports_workstream(cfg, ws, monkeypatch):
    with pytest.raises(MissingComponentError, match="workstream Z"):
        lazy_attr("sbmig.does_not_exist", "X", "Z")
    with pytest.raises(MissingComponentError, match="does not expose"):
        lazy_attr("sbmig.core", "Nope", "Z")
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
    for name in ("analyzer", "planner", "pre_handlers"):
        try:
            assert callable(getattr(orch, name))
        except MissingComponentError:  # pragma: no cover - workstream not ready
            pass
    for name in ("kb", "build_runner", "rewrite_runner", "restructurer", "fixer"):
        try:
            assert getattr(orch, name) is not None
        except MissingComponentError:  # pragma: no cover
            pass


def test_harvest_errors_and_kb_disabled(cfg, ws):
    class BadKB(FakeKB):
        def harvest(self, *a, **k):
            raise RuntimeError("disk full")

    report = make_orch(cfg, ws, kb=BadKB(), build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert any("kb harvest failed" in n for p in report.phases for n in p.notes)
    cfg.kb.harvest = False
    kb = FakeKB()
    make_orch(cfg, ws, kb=kb, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert kb.harvests == []
