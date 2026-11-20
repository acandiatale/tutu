"""v2.1z-aw: a framework project's reduced run converts and builds ONCE with `framework.fix_loop: false` (the default until v2.1z-bi): no
pre-try, no stored script, no LLM, no KB lookup for a fix, no edit after the conversion; the build's errors are the run's
unresolved errors, each entry carries the note, and one hand-over line comes first. `framework.fix_loop: true` keeps
the fix loop of v2.1z-ak (the default since v2.1z-bi)."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import FrameworkEvidence, FrameworkUse

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, bad_build, compile_error, journal_events, make_profile,
                    ok_build)
from .test_orchestrator import FOO, fix_all, make_orch
from .test_orchestrator_framework import reduced_plan

FW = "com.example.fw:example-fw-core"
LINE = ("framework project: build errors are not fixed automatically (framework.fix_loop: false) — 1 error(s) remain "
        "after the JDK 21 + jakarta conversion; see the error list")
NOTE = "not tried: framework project — build errors are not fixed automatically (framework.fix_loop: false)"


class NoLLM:
    """Any use of the LLM client fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the LLM was used: {name}")


class CountingKB(FakeKB):
    def __init__(self):
        super().__init__()
        self.matches = 0

    def match(self, cluster, ws, profile):
        self.matches += 1
        return super().match(cluster, ws, profile)


def use(fix_loop: bool = False, name: str = "Example") -> FrameworkUse:
    return FrameworkUse(patterns=["example-fw-core"], scope="jdk-jakarta", reason="r", name=name, fix_loop=fix_loop,
                        evidence=[FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")])


def broken_build(ws, call):
    return bad_build(compile_error(FOO)) if "broken" in ws.read(FOO) else ok_build()


def run(cfg, ws, *, fix_loop: bool, build_fn=broken_build, advisor: str = "none"):
    c = cfg.model_copy(deep=True)
    c.loop.engine = "graph"
    c.plan.advisor = advisor
    prof = make_profile()
    prof.framework = use(fix_loop)
    build = FakeBuildRunner(ws, fn=build_fn)
    fixer, kb = FakeFixer(script_fn=fix_all), CountingKB()
    orch = make_orch(c, ws, plan=reduced_plan(jsp=False), profile=prof, build=build, fixer=fixer, kb=kb, llm=NoLLM())
    return orch, orch.run(), build, fixer, kb


def test_failed_build_is_not_fixed(cfg, ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("as-is broken")
    orch, report, build, fixer, kb = run(cfg, ws, fix_loop=False, advisor="llm")
    # one build, nothing asked of the fixer / LLM / KB
    assert [c["label"] for c in build.calls] == ["verify"]
    assert fixer.script_calls == [] and kb.matches == 0
    assert report.llm_usage.calls == 0 and not any(pr.attempts for pr in report.phases)
    assert "broken" in ws.read(FOO)                                   # the tree is the converted one
    assert not report.success and run_exit_code(report) == 2 and report.final_build is not None
    assert [e.file for e in report.unresolved] == [FOO]
    # the hand-over line is the first follow-up; the entry says why nothing was tried
    assert report.manual_followups[0] == LINE
    [entry] = [e for e in report.error_entries if e.file == FOO]
    assert entry.status == "unresolved" and entry.tries == [] and entry.note == NOTE
    ev = journal_events(ws)
    names = [e["event"] for e in ev]
    assert "verify_loop_start" not in names and "plan_advice" not in names
    [skip] = [e for e in ev if e["event"] == "verify_loop_skipped"]
    assert skip == {**skip, "step": "verify.build", "reason": "framework.fix_loop: false", "errors": 1}
    assert any("framework.fix_loop: false" in n for n in report.phases[-1].notes)
    from sbmig.report.result_line import report_result_line
    assert " llm=0 " in report_result_line(report) + " "


def test_passed_build_is_a_success_without_the_line(cfg, ws):
    orch, report, build, fixer, kb = run(cfg, ws, fix_loop=False, build_fn=lambda w, call: ok_build())
    assert report.success and run_exit_code(report) == 0
    assert [c["label"] for c in build.calls] == ["verify"]
    assert not any(f.startswith("framework project: build errors") for f in report.manual_followups)
    assert not [e for e in journal_events(ws) if e["event"] == "verify_loop_skipped"]


def test_fix_loop_true_keeps_the_loop(cfg, ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("as-is broken")
    orch, report, build, fixer, kb = run(cfg, ws, fix_loop=True)
    assert fixer.script_calls and report.success
    names = [e["event"] for e in journal_events(ws)]
    assert "verify_loop_start" in names and "verify_loop_skipped" not in names
    assert not any(f.startswith("framework project: build errors") for f in report.manual_followups)


def test_a_full_run_is_untouched(cfg, ws):
    """Guard: a project without the framework keeps its fix loop whatever framework.fix_loop says."""
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("as-is broken")
    c = cfg.model_copy(deep=True)
    fixer = FakeFixer(script_fn=fix_all)
    report = make_orch(c, ws, build=FakeBuildRunner(ws, fn=broken_build), fixer=fixer).run()
    assert fixer.script_calls and report.success


@pytest.mark.parametrize("fix_loop", [False, True])
def test_journal_events_carry_name_and_mode(cfg, ws, fix_loop):
    orch, report, *_ = run(cfg, ws, fix_loop=fix_loop, build_fn=lambda w, call: ok_build())
    [plan] = [e for e in journal_events(ws) if e["event"] == "plan"]
    assert plan["scope"] == "jdk-jakarta" and plan["name"] == "Example" and plan["fix_loop"] is fix_loop


# ------------------------------------------------------------------------------------------------ the chained remediation
# v2.1z-aw: "취약점은 빌드가 실패해도 이어가도록!" — a conversion-only framework run whose single build failed is chained
# with baseline_may_fail=True (remediation.after_failed_build: framework); nothing else that failed is.
# v2.1z-bi: a framework run with the fix loop on whose final build still failed is chained the same way.
@pytest.fixture()
def spy(monkeypatch):
    from sbmig.remediations import chain
    from tests.graph.test_engine import ChainSpy
    s = ChainSpy(status="INCOMPLETE")
    monkeypatch.setattr(chain, "run_after_migration", s)
    return s


def chained_run(cfg, ws, *, fix_loop=False, build_fn=broken_build, after="framework", engine="graph", framework=True,
                fixer=None, rewrite=None):
    c = cfg.model_copy(deep=True)
    c.loop.engine = engine
    c.remediation.after_migration = True
    c.remediation.after_failed_build = after
    c.loop.max_llm_calls = 1
    prof = make_profile()
    prof.framework = use(fix_loop) if framework else None
    kw = dict(plan=reduced_plan(jsp=False)) if framework else {}
    if rewrite is not None:
        kw["rewrite"] = rewrite
    orch = make_orch(c, ws, profile=prof, build=FakeBuildRunner(ws, fn=build_fn),
                     fixer=fixer or FakeFixer(scripts=[]), **kw)
    return orch.run()


def broken_ws(ws):
    ws.write(FOO, "class Foo { broken }\n")
    ws.commit("as-is broken")


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_failed_conversion_only_run_is_chained(cfg, ws, spy, engine):
    from sbmig.report.result_line import report_result_line
    broken_ws(ws)
    report = chained_run(cfg, ws, engine=engine)
    assert not report.success and run_exit_code(report) == 2
    if engine == "inline":                        # the inline engine never chains; it notes the skip like for a success
        assert spy.calls == [] and report.remediation == {"status": "skipped", "reason": "inline_engine",
                                                          "dir": "remediation"}
        return
    assert [c["baseline_may_fail"] for c in spy.calls] == [True]
    assert report.remediation["status"] == "INCOMPLETE"
    line = report_result_line(report)
    assert " exit=2 build=FAIL " in line and line.endswith(" scope=jdk-jakarta remediation=INCOMPLETE")


def test_successful_framework_run_is_chained_as_before(cfg, ws, spy):
    report = chained_run(cfg, ws, build_fn=lambda w, call: ok_build())
    assert report.success and [c["baseline_may_fail"] for c in spy.calls] == [False]


@pytest.mark.parametrize("case", ["ordinary", "never", "never-loop-on", "not-attempted"])
def test_other_failed_runs_are_not_chained(cfg, ws, spy, case):
    broken_ws(ws)
    if case == "not-attempted":
        from .test_asis_parent import pom
        from .test_orchestrator_framework import ParentRewrite
        ws.write("pom.xml", pom("com.acme:acme-parent", "9.9"))
        ws.commit("foreign parent")
        report = chained_run(cfg, ws, rewrite=ParentRewrite(ws, "com.acme:acme-parent:9.9"))
        assert "aborted:parent-not-found" in report.flags and report.final_build is None
    else:
        report = chained_run(cfg, ws, framework=case != "ordinary", fix_loop=case == "never-loop-on",
                             after="framework" if case == "ordinary" else "never")
    assert not report.success and spy.calls == [] and report.remediation is None


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_failed_build_with_the_loop_on_is_chained(cfg, ws, spy, engine):
    """v2.1z-bi: the fix loop ran (framework.fix_loop: true) and the final build still failed → chained with
    baseline_may_fail=True, like a conversion-only run; the report says the loop ran."""
    from sbmig.report import render_markdown
    from sbmig.report.result_line import report_result_line
    broken_ws(ws)
    fixer = FakeFixer(scripts=[])                              # the loop runs but never fixes the error
    report = chained_run(cfg, ws, fix_loop=True, fixer=fixer, engine=engine)
    assert not report.success and run_exit_code(report) == 2
    assert report.final_build is not None and not report.final_build.ok
    assert fixer.script_calls                                  # the loop did run
    if engine == "inline":
        assert spy.calls == [] and report.remediation["reason"] == "inline_engine"
        return
    assert [c["baseline_may_fail"] for c in spy.calls] == [True]
    assert report.remediation["status"] == "INCOMPLETE"
    assert report_result_line(report).endswith(" scope=jdk-jakarta remediation=INCOMPLETE")
    en = render_markdown(report, "en")
    assert ("Chained after the migration although its final build still failed after the automatic build fix (a "
            "framework project: remediation.after_failed_build); it does not change the migration result above.") in en
    ko = render_markdown(report, "ko")
    assert "빌드 오류 자동 수정을 거쳤지만 마이그레이션 빌드는 통과하지 않았습니다." in ko
    for md in (en, ko):                                        # nothing says the errors were left unfixed by design
        assert "framework.fix_loop: false" not in md and "자동 수정은 하지 않" not in md


def test_loop_on_build_passed_is_chained_as_a_success(cfg, ws, spy):
    report = chained_run(cfg, ws, fix_loop=True, build_fn=lambda w, call: ok_build())
    assert report.success and [c["baseline_may_fail"] for c in spy.calls] == [False]


# v2.1z-ay: a reduced run whose conversion failed is not a success; the chained remediation still follows it with
# remediation.after_failed_build: framework, baseline_may_fail only when the build itself failed.
def failed_conversion(ws):
    from .fakes import FakeRewriteRunner
    return FakeRewriteRunner(ws, ok=False, error="Failed to parse pom: Invalid UTF-8 middle byte 0xce")


@pytest.mark.parametrize("build_ok", [True, False], ids=["build-passed", "build-failed"])
@pytest.mark.parametrize("fix_loop", [False, True], ids=["loop-off", "loop-on"])
@pytest.mark.parametrize("after", ["framework", "never"])
def test_failed_conversion_and_the_chained_remediation(cfg, ws, spy, build_ok, fix_loop, after):
    if not build_ok:
        broken_ws(ws)
    report = chained_run(cfg, ws, fix_loop=fix_loop, after=after, rewrite=failed_conversion(ws),
                         build_fn=(lambda w, call: ok_build()) if build_ok else broken_build)
    assert "rewrite-failed" in report.flags and not report.success and run_exit_code(report) == 2
    assert report.final_build is not None and report.final_build.ok is build_ok
    # chained: never with `never`; with `framework` whether or not the loop ran (v2.1z-bi)
    chained = after == "framework"
    assert [c["baseline_may_fail"] for c in spy.calls] == ([not build_ok] if chained else [])
