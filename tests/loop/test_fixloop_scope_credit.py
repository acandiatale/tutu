"""K1 of the three-pods review (v2.1z-ap) in the fix loop: a stored version records the scope of the run that wrote
it, every credit carries the run's scope, and a framework-guard rollback of a full-migration version reused by a
reduced (jdk-jakarta) run is credited ``out_of_scope`` — not a failure of the version. The ladder reads the run's
scope from the loop's KB adapter (``CaseScripts.run_scope``)."""
from __future__ import annotations

from sbmig.core.models import Phase
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import OUT_OF_SCOPE

from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.triage import Triager

from .fakes import FakeBuildRunner, FakeFixer, FakeKB, FakeScriptRunner, answer, journal_events, make_profile
from .test_fixloop_framework import fw_cfg, fw_pom, reduce
from .test_v21l_versions import FOO, PomApplier, _ws, pom_build

L2 = "compile|cannot_find_symbol|X"            # what pom_build reports for the broken file
CASE = case_id_for(L2)
PARENT_2 = fw_pom(parent="com.example.fw:example-fw-parent:2.0")
# the full-migration fix: it also moves the parent (fine in a full run, out of bounds in a reduced one)
FULL_FIX = f"def fix(p):\n    p.write('pom.xml', {PARENT_2!r})\n    p.write({FOO!r}, 'class Foo {{ ok }}\\n')\n"
REDUCED_FIX = f"def fix(p):\n    p.write({FOO!r}, 'class Foo {{ ok }}\\n')\n"


def _store(tmp_path) -> CaseStore:
    store = CaseStore(tmp_path / "kb", None)
    v = store.add_script(CASE, L2, FULL_FIX, run_id="F1", project="full-a", scope="full").version
    store.credit(CASE, v, "improved", project="full-b", run_id="F2", round=1, n=1, scope="full")
    return store


def _run(cfg, tmp_path, store, *, reduced: bool, answers=()):
    ws = _ws(tmp_path / ("red" if reduced else "full"),
             {"pom.xml": fw_pom(parent="com.example.fw:example-fw-parent:1.0"), FOO: "class Foo { broken }\n"})
    c = fw_cfg(cfg)
    loop = BuildFixLoop(c, ws, FakeBuildRunner(ws, fn=pom_build(broken_file=FOO)), Triager(c),
                        FakeFixer(scripts=list(answers)), FakeKB(cases=store), make_profile(),
                        budget=LoopBudget.from_config(c), edit_applier=PomApplier(), script_runner=FakeScriptRunner())
    if reduced:
        reduce(loop)
    assert loop.cases.run_scope() == ("jdk-jakarta" if reduced else "full")   # the ladder's scope, set by the loop
    return ws, loop, loop.run(Phase.VERIFY, "package", 4)


def test_a_guard_rollback_of_a_full_version_is_credited_out_of_scope(cfg, tmp_path):
    store = _store(tmp_path)
    ws, loop, pr = _run(cfg, tmp_path, store, reduced=True, answers=[answer(code=REDUCED_FIX)])
    assert pr.ok
    first, second = pr.attempts
    assert (first.script_mode, first.script_version, first.outcome) == ("reuse", 1, "apply_failed")
    assert "<version>1.0</version>" in ws.read("pom.xml")                       # the parent stayed
    v1, v2 = sorted(store.find(CASE).scripts, key=lambda s: s.version)
    # the full version: no failure, no last_failure; the rollback is in runs[] with its scope
    assert (v1.status, v1.successes, v1.failures, v1.last_failure) == ("proven", 1, 0, "")
    assert (v1.runs[-1].result, v1.runs[-1].scope, v1.runs[-1].run_id) == (OUT_OF_SCOPE, "jdk-jakarta", "t-run")
    # the reduced run's own script records its scope, and its success is credited in that scope
    assert second.script_version == v2.version and v2.scope == "jdk-jakarta" and v2.parent == 1
    assert (v2.successes, v2.runs[-1].scope) == (1, "jdk-jakarta")
    credits = [e for e in journal_events(ws) if e["event"] == "script_credit"]
    assert credits[0]["outcome"] == "apply_failed" and credits[0]["credited"] == OUT_OF_SCOPE
    assert "credited" not in credits[-1]


def test_a_full_run_reuses_the_full_version_before_a_newer_reduced_one(cfg, tmp_path):
    store = _store(tmp_path)
    v2 = store.add_script(CASE, L2, REDUCED_FIX, run_id="R1", project="fw-a", scope="jdk-jakarta").version
    store.credit(CASE, v2, "improved", project="fw-b", run_id="R2", round=1, n=1, scope="jdk-jakarta")
    assert store.find(CASE).scripts[-1].status == "proven"                    # newer and proven, other scope
    ws, loop, pr = _run(cfg, tmp_path, store, reduced=False)
    assert pr.ok and len(pr.attempts) == 1
    att = pr.attempts[0]
    assert (att.script_mode, att.script_version, att.outcome) == ("reuse", 1, "build_ok")
    v1 = store.find(CASE).scripts[0]
    assert (v1.successes, v1.runs[-1].scope, v1.runs[-1].result) == (2, "full", "build_ok")
    # and a reduced run asks for its own scope's version first
    _ws2, _loop2, pr2 = _run(cfg, tmp_path, store, reduced=True)
    assert pr2.ok and (pr2.attempts[0].script_mode, pr2.attempts[0].script_version) == ("reuse", v2)
