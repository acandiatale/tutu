"""Round 8: tests-skipped fallback (-DskipTests when only runtime test failures remain) + report.properties."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import BuildError, ErrorKind, Phase, PropertyFinding, PropertyInventory, PropertyRef

from .fakes import (FakeBuildRunner, FakeFixer, FakeRestructurer, bad_build, compile_error, journal_events,
                    make_profile, ok_build)
from .test_fixloop import make_loop
from .test_orchestrator import make_orch

RUN_GOALS = ("package", "verify", "install", "test")


def test_failure(name: str = "OrderServiceTest.contextLoads",
                 msg: str = "IllegalArgument Could not resolve placeholder 'mail.host'") -> BuildError:
    return BuildError(kind=ErrorKind.TEST_FAILURE, message=f"{name}:12 {msg}", symbol=name, module="demo",
                      signature=f"test_failure|{name}", signature_generic="test_failure|x")


test_failure.__test__ = False     # a builder, not a pytest test


def runtime_fail_build(*errs: BuildError):
    """compile / test-compile pass; goals that run tests fail with `errs` unless -DskipTests (or test skip)."""
    errs = errs or (test_failure(),)

    def fn(w, call):
        runs_tests = any(g in call["goal"].split() for g in RUN_GOALS)
        if runs_tests and not (call.get("skip_tests") or call["skip_test_compile"]):
            return bad_build(*errs)
        return ok_build()
    return fn


# ---------------------------------------------------------------------------------------- loop unit
def test_fallback_when_only_test_failures(cfg, ws, profile):
    t1, t2 = test_failure(), test_failure("PaymentIT.charges", "Connection refused: db:5432")
    build = FakeBuildRunner(ws, fn=runtime_fail_build(t1, t2))
    loop = make_loop(cfg, ws, profile, build=build)               # the fixer proposes nothing
    pr = loop.run(Phase.RESTRUCTURE, "package", 2)
    assert pr.ok and pr.build_ok and loop.tests_skipped
    assert loop.tests_skipped_in(pr) and not loop.test_compile_skipped_in(pr)
    assert "tests-skipped" in loop.flags and "test-compile-skipped" not in loop.flags
    last = build.calls[-1]
    assert last["skip_tests"] is True and last["skip_test_compile"] is False
    assert last["label"] == "restructure-skip-tests-run"
    assert pr.remaining_errors == [t1, t2]
    assert any(n.startswith("tests-skipped: main code builds and packages, 2 test(s)") for n in pr.notes)
    fu = [f for f in loop.followups if f.startswith("test fails at runtime (packaged with -DskipTests)")]
    assert len(fu) == 2 and "OrderServiceTest.contextLoads" in fu[0] and "Properties to set" in fu[0]
    assert "PaymentIT.charges" in fu[1] and "Connection refused" in fu[1]
    ev = [e for e in journal_events(ws) if e["event"] == "tests_fallback"]
    assert ev == [{**ev[0], "phase": "restructure", "failing": 2}]
    assert pr.iteration_records[-1].build_ok


def test_no_fallback_with_mixed_errors(cfg, ws, profile):
    errs = (test_failure(), compile_error())
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=runtime_fail_build(*errs)))
    pr = loop.run(Phase.RESTRUCTURE, "package", 1)
    assert not pr.ok and not loop.tests_skipped and "tests-skipped" not in loop.flags
    assert not any(e["event"] == "tests_fallback" for e in journal_events(ws))


def test_no_fallback_when_disabled_or_goal_runs_no_tests(cfg, ws, profile):
    # goal `compile` runs no tests: the build_fn fails it anyway to prove the fallback is not tried
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=lambda w, c: bad_build(test_failure())))
    assert not loop.run(Phase.JDK21, "compile", 1).ok and not loop.tests_skipped
    cfg.build.allow_skip_tests_fallback = False
    build = FakeBuildRunner(ws, fn=runtime_fail_build())
    loop2 = make_loop(cfg, ws, profile, build=build)
    assert not loop2.run(Phase.RESTRUCTURE, "package", 1).ok and not loop2.tests_skipped
    assert not any(c["skip_tests"] for c in build.calls)


def test_fallback_build_fails_too(cfg, ws, profile):
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=lambda w, c: bad_build(test_failure())))
    pr = loop.run(Phase.RESTRUCTURE, "package", 1)
    assert not pr.ok and not loop.tests_skipped and any("tests fallback (-DskipTests) also failed" in n
                                                         for n in pr.notes)


def test_old_runner_without_skip_tests_kwarg(cfg, ws, profile):
    class OldRunner:
        def __init__(self):
            self.calls = []

        def run(self, goal, *, module=None, extra_args=(), label="", skip_test_compile=False):
            self.calls.append(list(extra_args))
            return ok_build() if "-DskipTests" in extra_args else bad_build(test_failure())

    r = OldRunner()
    loop = make_loop(cfg, ws, profile, build=r)
    assert loop.run(Phase.RESTRUCTURE, "package", 1).ok and loop.tests_skipped
    assert r.calls[-1] == ["-DskipTests"]


# ---------------------------------------------------------------------------------------- orchestrator, both engines
@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_orchestrator_tests_skipped_run(cfg, ws, engine):
    cfg.loop.engine = engine
    t1 = test_failure()
    build = FakeBuildRunner(ws, fn=runtime_fail_build(t1))
    report = make_orch(cfg, ws, build=build, pre=[], fixer=FakeFixer()).run()     # proposes nothing applicable
    assert "tests-skipped" in report.flags and "test-compile-skipped" not in report.flags
    verify = build.calls[-1]
    assert verify["label"] == "verify" and verify["skip_tests"] is True and verify["skip_test_compile"] is False
    assert report.success
    # the failing test is BLOCKED (below) → reported there, not repeated in `unresolved` (blocked errors are deduped)
    assert report.unresolved == []
    assert any(f.startswith("test fails at runtime") and "OrderServiceTest" in f for f in report.manual_followups)
    vs = next(p for p in report.phases if p.phase == Phase.VERIFY)
    assert any("built with -DskipTests" in n for n in vs.notes)
    # the fixer could not fix the runtime test failure → its cluster is BLOCKED and kept after the green verify
    # (mirror of the test-compile rule) → exit code 2 (EXIT_INCOMPLETE: packaged, but tests fail at runtime)
    assert report.blocked and all(e.kind == ErrorKind.TEST_FAILURE for b in report.blocked for e in b.errors)
    assert run_exit_code(report) == 2


def test_orchestrator_tests_skipped_failures_are_unresolved(cfg, ws, monkeypatch):
    """`_finalize` puts the failing tests of tests-skipped phases into `unresolved` (blocked errors are deduped out of
    it — neutralized here to see the path); a green verify with nothing blocked → exit code 0 (EXIT_SUCCESS, the
    same as a test-compile-skipped run: the build gate passed, the tests are listed for the owners)."""
    from sbmig.loop.blocked_registry import BlockedRegistry
    monkeypatch.setattr(BlockedRegistry, "is_blocked_error", lambda self, e: False)
    t1 = test_failure()
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=runtime_fail_build(t1)), pre=[])
    monkeypatch.setattr(orch, "_finalize_blocked", lambda report, unresolved: setattr(report, "blocked", []))
    report = orch.run()
    assert "tests-skipped" in report.flags and report.success and not report.blocked
    assert report.unresolved == [t1]
    assert run_exit_code(report) == 0


def test_graph_and_inline_same_report_on_tests_skipped(cfg, tmp_path):
    from ..graph.test_engine import comparable, run_engine
    kw = {"build": lambda w: FakeBuildRunner(w, fn=runtime_fail_build()), "pre": []}
    _, _, rg = run_engine(cfg, tmp_path, "graph", **kw)
    _, _, ri = run_engine(cfg, tmp_path, "inline", **kw)
    assert "tests-skipped" in rg.flags and comparable(rg) == comparable(ri)
    assert run_exit_code(rg) == run_exit_code(ri)


# ---------------------------------------------------------------------------------------- report.properties
def inventory() -> PropertyInventory:
    return PropertyInventory(findings=[
        PropertyFinding(key="mail.host", status="external", boot_keys=["mail.host"],
                        refs=[PropertyRef(key="mail.host", source="value-annotation", file="src/main/java/M.java",
                                          line=3)]),
        PropertyFinding(key="app.name", status="defined", defined_in=["application.properties"]),
    ])


class PlaceholderRestructurer(FakeRestructurer):
    def apply(self, ws, profile, cfg, *, mode, params=None):
        res = super().apply(ws, profile, cfg, mode=mode, params=params)
        return RestructureResult(**{**res.model_dump(),
                                    "placeholder_files": ["module/boot-jar/src/main/resources/application.properties"],
                                    "placeholders_written": ["mail.host", "mail.host"]})


def test_restructure_merges_placeholders_into_report(cfg, ws):
    prof = make_profile()
    prof.properties = inventory()
    report = make_orch(cfg, ws, profile=prof, restructurer=PlaceholderRestructurer(), pre=[],
                       build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    p = report.properties
    assert p is not None and [f.key for f in p.external] == ["mail.host"]
    assert p.placeholder_files == ["module/boot-jar/src/main/resources/application.properties"]
    assert p.placeholders_written == ["mail.host"]
    assert prof.properties.placeholder_files == []                 # the profile inventory is not mutated
    ev = [e for e in journal_events(ws) if e["event"] == "placeholders"]
    assert len(ev) == 1 and ev[0]["step"] == "restructure.apply" and ev[0]["keys"] == 1
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert any(n.endswith("; placeholders: 1") for n in rs.notes)
    an = next(e for e in journal_events(ws) if e["event"] == "analyze")
    assert an["properties_external"] == 1


def test_no_placeholder_event_without_placeholders(cfg, ws):
    report = make_orch(cfg, ws, pre=[], build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert report.properties is not None and report.properties.placeholder_files == []
    assert not any(e["event"] == "placeholders" for e in journal_events(ws))


def test_finalize_copies_profile_inventory_when_stopped_early(cfg, ws):
    prof = make_profile()
    prof.properties = inventory()
    report = make_orch(cfg, ws, profile=prof).run(stop_after=Phase.ANALYZE)
    assert report.properties is not None and [f.key for f in report.properties.external] == ["mail.host"]
