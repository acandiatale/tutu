"""Round 6: advisory-guided LLM fixes — the loop attaches the matching library advisories to the fixer context,
records them on the attempt (journal + ledger line), and finalize attributes the rewritten files to the advisory
(AdvisoryFinding.llm_fixed_files / llm_fix_notes → Handover "from here on")."""
from __future__ import annotations

import pytest

from sbmig.core.models import (
    AdvisoryFinding,
    CaseType,
    DecisionKind,
    FileChange,
    FixAttempt,
    Phase,
    PhaseResult,
    RunReport,
    TriageDecision,
    utcnow,
)
from sbmig.kb.ledger import attempt_record
from sbmig.loop import BuildFixLoop, LoopBudget, Orchestrator
from sbmig.triage import HandlerRegistry, Triager

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeKB,
    FakeRewriteRunner,
    bad_build,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    write_fix,
)
from .test_orchestrator import make_orch

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
HYSTRIX_MSG = "package com.netflix.hystrix does not exist"
ANALYSIS = "Foo extended HystrixCommand; ported to a Resilience4j CircuitBreaker. Second sentence."


def hystrix(**kw) -> AdvisoryFinding:
    base = dict(advisory_id="hystrix", library="hystrix", status="eol", title="Hystrix is discontinued",
                policy="recipe", recipes=["com.company.migrate.HystrixToResilience4j"],
                replacement="org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j",
                followup="configure resilience4j.circuitbreaker.instances")
    base.update(kw)
    return AdvisoryFinding(**base)


def hystrix_build(ws, call):
    """Foo fails on the vanished Hystrix package, Bar on an unrelated symbol, while they still say 'broken'."""
    errs = []
    if "broken" in ws.read(FOO):
        errs.append(compile_error(FOO, HYSTRIX_MSG))
    if "broken" in ws.read(BAR):
        errs.append(compile_error(BAR, "cannot find symbol"))
    return bad_build(*errs) if errs else ok_build()


def fix(w, c, ctx):
    return write_fix(c.file, "class X { ok }\n", analysis=ANALYSIS if c.file == FOO else "renamed a symbol")


def test_loop_passes_hints_and_records_advisory_ids(cfg, ws):
    fixer = FakeFixer(fn=fix)
    triager = Triager(cfg, registry=HandlerRegistry())
    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=hystrix_build), triager, triager.registry, fixer, FakeKB(),
                        FakeRewriteRunner(ws), make_profile(), budget=LoopBudget.from_config(cfg),
                        advisories=[hystrix(), hystrix(advisory_id="springfox", library="springfox",
                                                       replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui")])
    pr = loop.run(Phase.SPRING, "test-compile", 3)
    assert pr.ok
    by_file = {a.cluster_errors[0].file: a for a in pr.attempts}
    assert by_file[FOO].advisory_ids == ["hystrix"] and by_file[BAR].advisory_ids == []
    hints = [c.advisory_hints for c in fixer.contexts]
    assert sorted(len(h) for h in hints) == [0, 1]
    hint = next(h for h in hints if h)[0]
    assert hint.startswith("hystrix (eol, policy recipe): replace with "
                           "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j.")
    ev = [e for e in journal_events(ws) if e["event"] == "attempt"]
    assert {tuple(e["advisory_ids"]) for e in ev} == {("hystrix",), ()}


def test_loop_without_advisories_sends_no_hints(cfg, ws):
    fixer = FakeFixer(fn=fix)
    triager = Triager(cfg, registry=HandlerRegistry())
    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=hystrix_build), triager, triager.registry, fixer, FakeKB(),
                        FakeRewriteRunner(ws), make_profile(), budget=LoopBudget.from_config(cfg))
    assert loop.run(Phase.SPRING, "test-compile", 3).ok
    assert all(c.advisory_hints == [] for c in fixer.contexts)
    assert all(a.advisory_ids == [] for a in loop.history)


@pytest.fixture()
def hystrix_advisory(monkeypatch):
    import sbmig.advisory as adv

    monkeypatch.setattr(adv, "evaluate_advisories", lambda profile, cfg, catalog=None: [hystrix()])
    monkeypatch.setattr(adv, "verify_advisories",
                        lambda findings, view, cfg: [f.model_copy(update={"outcome": "replaced"}) for f in findings])


def test_orchestrator_attributes_llm_fixes_to_the_advisory(cfg, ws, hystrix_advisory):
    fixer = FakeFixer(fn=fix)
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=hystrix_build), fixer=fixer).run()
    assert report.success
    assert any(c.advisory_hints for c in fixer.contexts)          # Orchestrator → BuildFixLoop(advisories=…)
    f = report.advisories[0]
    assert f.llm_fixed_files == [FOO]                             # Bar's fix carried no advisory
    assert f.llm_fix_notes == ["Foo extended HystrixCommand; ported to a Resilience4j CircuitBreaker."]
    line = next(x for x in report.handover.from_here if x.startswith("code rewritten by the LLM for hystrix"))
    assert line == (f"code rewritten by the LLM for hystrix: {FOO} — {f.llm_fix_notes[0]} — review these changes "
                    "(changes.patch, attempts table)")


def _report(*attempts: FixAttempt) -> RunReport:
    return RunReport(run_id="r", project_name="p", case_type=CaseType.SPRING_BOOT, started_at=utcnow(),
                     phases=[PhaseResult(phase=Phase.SPRING, attempts=list(attempts))])


def _att(**kw) -> FixAttempt:
    base = dict(id="s-1-1", phase=Phase.SPRING, iteration=1, cluster_id="c1", advisory_ids=["hystrix"],
                decision=TriageDecision(cluster_id="c1", kind=DecisionKind.LLM_FIX), applied=True, outcome="build_ok",
                analysis="ported the command. more.", changes=[FileChange(path="a/Foo.java", op="modify")])
    base.update(kw)
    return FixAttempt(**base)


@pytest.mark.parametrize("update,attributed", [
    ({}, True),
    ({"outcome": "improved"}, True),
    ({"outcome": "regressed"}, False),
    ({"applied": False}, False),
    ({"cluster_resolved": False}, False),
    ({"advisory_ids": ["springfox"]}, False),
    ({"decision": TriageDecision(cluster_id="c1", kind=DecisionKind.HANDLER, handler_id="h")}, False),
])
def test_attribution_predicate(update, attributed):
    f = hystrix()
    report = _report(_att(**update))
    Orchestrator._attribute_llm_fixes(report, [f])
    assert (f.llm_fixed_files, f.llm_fix_notes) == ((["a/Foo.java"], ["ported the command."]) if attributed
                                                    else ([], []))


def test_attribution_dedupes_files_and_includes_renames():
    f = hystrix()
    a1 = _att(changes=[FileChange(path="a/Foo.java", op="modify"),
                       FileChange(path="a/Old.java", op="rename", new_path="a/New.java")])
    a2 = _att(id="s-2-1", analysis="x" * 300, changes=[FileChange(path="a/Foo.java", op="modify")])
    report = _report(a1, a2)
    Orchestrator._attribute_llm_fixes(report, [f])
    assert f.llm_fixed_files == ["a/Foo.java", "a/Old.java", "a/New.java"]
    assert f.llm_fix_notes[0] == "ported the command." and len(f.llm_fix_notes[1]) == 160


def test_ledger_line_carries_advisory_ids(cfg, ws):
    rec = attempt_record(_att(advisory_ids=["hystrix", "ribbon"]), make_profile(), ws, Phase.SPRING)
    assert rec.resolution == "llm_patch" and rec.notes == ["advisory:hystrix", "advisory:ribbon"]
    assert attempt_record(_att(advisory_ids=[]), make_profile(), ws, Phase.SPRING).notes == []
