"""K1 / K2 of the three-pods review (v2.1z-ap): full-migration and reduced-scope (jdk-jakarta) scripts in one KB case,
and a case id a re-sign moved while a pod held it.

K1: every stored version records the scope of the run that wrote it, every credited run its own scope. Reuse ranks
(same scope, proven, version); a version of the other scope runs only after the run's own ones, at most one per case
and run. A framework-guard rollback of another scope's version is ``out_of_scope`` (not a failure); other-scope runs
stay in ``runs[]`` and leave the version's counters / status alone. Legacy versions and runs ("" scope) are ``full``.

K2: ``credit`` / ``add_script`` on an id a re-sign moved resolve it through the moved case's ``aliases``; nothing
found → one WARNING; a fresh case under the old id never names a parent it does not have.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.core.models import (BuildError, CaseScript, DecisionKind, ErrorCluster, ErrorEntry, ErrorKind, FixAttempt,
                               FixTry, Phase, TriageDecision)
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import FAILURE_RESULTS, OUT_OF_SCOPE, SUCCESS_RESULTS, run_line, scripts_text
from sbmig.kb import FileKnowledgeBase, kb_cli
from sbmig.triage.scripts import plan_script_step, stored_next

REPO = Path(__file__).resolve().parents[2]
L2 = "compile|package_not_found|javax.servlet.http"
C = case_id_for(L2)
FULL = "def fix(p):\n    # FULL: jakarta imports + version from the Boot 4 parent\n    pass\n"
REDUCED = "def fix(p):\n    # REDUCED: jakarta imports + explicit servlet-api 6 version, parent kept\n    pass\n"
GUARD = "framework project: the attempt changed the <parent> of pom.xml — nothing of it was kept"


def cluster(l2: str = L2) -> ErrorCluster:
    err = BuildError(kind=ErrorKind.COMPILE, message="package javax.servlet.http does not exist", file="A.java", line=1,
                     signature=l2 + "|L1", signature_generic=l2)
    return ErrorCluster(id="c", kind=ErrorKind.COMPILE, errors=[err], case_id=case_id_for(l2), signatures=[l2 + "|L1"])


def reuse_att(version: int, outcome: str = "no_change") -> FixAttempt:
    return FixAttempt(id=f"verify-1-{version}", phase=Phase.VERIFY, iteration=1, cluster_id="c",
                      decision=TriageDecision(cluster_id="c", kind=DecisionKind.SCRIPT), outcome=outcome,
                      script_mode="reuse", script_version=version, script_code="def fix(p):\n    pass\n")


def first_step(store, cfg, scope: str):
    return plan_script_step(cluster(), [], store, cfg, scope=scope)


@pytest.fixture()
def store(tmp_path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def by_version(store: CaseStore, v: int) -> CaseScript:
    return next(s for s in store.find(C).scripts if s.version == v)


# --------------------------------------------------------------------------- K1: the reviewer's scenario end to end
def test_full_and_reduced_runs_each_reuse_their_own_scope_first(store, cfg):
    # two full runs: v1 (full) proven
    v1 = store.add_script(C, L2, FULL, run_id="F1", project="full-a", scope="full").version
    store.credit(C, v1, "improved", project="full-a", run_id="F1", round=1, n=1, scope="full")
    store.credit(C, v1, "improved", project="full-b", run_id="F2", round=1, n=1, scope="full")
    assert by_version(store, v1).status == "proven" and by_version(store, v1).scope == "full"
    # reduced R1: nothing of its scope yet → the full version is tried (the KB is not partitioned) ...
    st = first_step(store, cfg, "jdk-jakarta")
    assert (st.mode, st.version) == ("reuse", v1) and "다른 범위: full" in st.kb_line
    # ... the framework guard rolls it back: not a failure of the full version
    store.credit(C, v1, OUT_OF_SCOPE, project="fw-a", run_id="R1", round=1, n=1, reason=GUARD, scope="jdk-jakarta")
    # R1's augment = the reduced-scope version
    v2 = store.add_script(C, L2, REDUCED, run_id="R1", project="fw-a", source="llm-augment", parent=v1,
                          reason="v1: out_of_scope", scope="jdk-jakarta").version
    store.credit(C, v2, "improved", project="fw-a", run_id="R1", round=1, n=2, scope="jdk-jakarta")
    # reduced R2 reuses its own scope's version first, which resolves it in a second project → proven
    st = first_step(store, cfg, "jdk-jakarta")
    assert (st.mode, st.version) == ("reuse", v2) and "다른 범위" not in st.kb_line
    store.credit(C, v2, "improved", project="fw-b", run_id="R2", round=1, n=1, scope="jdk-jakarta")
    # after two full and two reduced runs
    full, red = by_version(store, v1), by_version(store, v2)
    assert (full.status, full.successes, full.failures, full.last_failure) == ("proven", 2, 0, "")
    assert (red.status, red.successes, red.failures, red.scope) == ("proven", 2, 0, "jdk-jakarta")
    assert first_step(store, cfg, "full").version == v1           # a full run: the full-scope version
    assert first_step(store, cfg, "jdk-jakarta").version == v2    # a reduced run: the reduced-scope version
    # the guard rollback is visible in runs[] with its scope and the new result
    guarded = [r for r in full.runs if r.result == OUT_OF_SCOPE]
    assert len(guarded) == 1 and guarded[0].scope == "jdk-jakarta" and guarded[0].run_id == "R1"
    assert OUT_OF_SCOPE not in FAILURE_RESULTS | SUCCESS_RESULTS


def test_an_out_of_scope_rollback_of_the_runs_own_scope_is_a_failure(store):
    v = store.add_script(C, L2, REDUCED, run_id="R1", project="fw-a", scope="jdk-jakarta").version
    s = store.credit(C, v, OUT_OF_SCOPE, project="fw-b", run_id="R2", round=1, n=1, reason=GUARD, scope="jdk-jakarta")
    assert (s.failures, s.runs[-1].result) == (1, "apply_failed") and s.last_failure.startswith("framework project:")


def test_other_scope_runs_leave_counters_status_and_last_failure_alone(store):
    v = store.add_script(C, L2, FULL, run_id="F1", project="full-a", scope="full").version
    store.credit(C, v, "improved", project="fw-a", run_id="R1", round=1, n=1, scope="jdk-jakarta")
    store.credit(C, v, "no_change", project="fw-b", run_id="R2", round=1, n=1, reason="no_change: 1 of 1",
                 scope="jdk-jakarta")
    s = by_version(store, v)
    assert (s.status, s.successes, s.failures, s.projects, s.last_failure, s.last_result) == \
        ("unverified", 0, 0, [], "", "")
    assert [(r.result, r.scope) for r in s.runs] == [("improved", "jdk-jakarta"), ("no_change", "jdk-jakarta")]
    store.credit(C, v, "improved", project="full-b", run_id="F2", round=1, n=1, scope="full")
    s = by_version(store, v)
    assert (s.status, s.successes, s.projects) == ("proven", 1, ["full-b"])   # only same-scope successes prove it


def test_an_other_scope_version_runs_only_after_the_runs_own_and_once_per_run(store, cfg):
    cfg.script.reuse_versions = 4
    v1 = store.add_script(C, L2, FULL, run_id="F1", project="a", scope="full").version
    store.credit(C, v1, "improved", project="b", run_id="F2", scope="full")            # v1 full proven
    v2 = store.add_script(C, L2, REDUCED, run_id="R1", project="c", scope="jdk-jakarta").version
    v3 = store.add_script(C, L2, REDUCED + "# 2\n", run_id="R1", project="c", scope="jdk-jakarta").version
    v4 = store.add_script(C, L2, FULL + "# 2\n", run_id="F3", project="d", scope="full").version
    order, tried = [], []
    while True:
        st = plan_script_step(cluster(), tried, store, cfg, scope="jdk-jakarta")
        if st.mode != "reuse":
            break
        order.append(st.version)
        tried.append(reuse_att(st.version))
    # the reduced run: its own versions (unverified newest first), then ONE full version (the proven one)
    assert order == [v3, v2, v1] and v4 not in order and st.mode == "augment"
    # a full run with the same store: the full versions first, then one reduced one
    assert [s.version for s in _all(store, "full")] == [v1, v4, v3]


def _all(store, scope):
    out, done = [], set()
    while (nxt := stored_next(store, C, done, scope=scope)) is not None:
        out.append(nxt[0])
        done.add(nxt[0].version)
    return out


def test_legacy_versions_without_a_scope_behave_as_full(store, cfg):
    v1 = store.add_script(C, L2, FULL, run_id="F1", project="a").version                 # an older image: no scope
    assert by_version(store, v1).scope == ""
    store.credit(C, v1, "improved", project="b", run_id="F2")                          # an older image's credit
    store.credit(C, v1, "improved", project="c", run_id="F3", scope="full")            # a full run of today
    s = by_version(store, v1)
    assert (s.status, s.successes) == ("proven", 2)
    v2 = store.add_script(C, L2, REDUCED, run_id="R1", project="fw", scope="jdk-jakarta").version
    assert first_step(store, cfg, "full").version == v1
    assert first_step(store, cfg, "jdk-jakarta").version == v2
    assert store.best(C)[0].version == v1 and store.best(C, "jdk-jakarta")[0].version == v2
    # without any recorded scope the order is the store's own (best), whatever the asking scope
    only = CaseStore(store.root.parent / "kb2", None)
    a = only.add_script(C, L2, FULL, run_id="F1", project="a").version
    b = only.add_script(C, L2, FULL + "# b\n", run_id="F2", project="b").version
    assert first_step(only, cfg, "jdk-jakarta").version == b and a < b


def test_the_hint_names_the_scope_of_a_last_solution_from_the_other_scope(kb_cfg):
    kb = FileKnowledgeBase(kb_cfg)
    kb.cases.add_script(C, L2, FULL, run_id="F1", project="a", solution="import jakarta.servlet, Boot 4 parent version",
                        scope="full")

    class Prof:
        def __init__(self, reduced):
            self.reduced_scope = reduced

    red = kb.match(cluster(), None, Prof(True))
    assert red.hints and "last solution [from a full-migration run] — import jakarta.servlet" in red.hints[0]
    full = kb.match(cluster(), None, Prof(False))
    assert "last solution — import jakarta.servlet" in full.hints[0] and "[from" not in full.hints[0]
    kb.cases.add_script(C, L2, REDUCED, run_id="R1", project="fw", solution="explicit servlet-api 6 version",
                        scope="jdk-jakarta")
    assert "[from a jdk-jakarta (reduced-scope) run]" in kb.match(cluster(), None, Prof(False)).hints[0]
    hit = kb.match(cluster(), None, Prof(True))
    assert "[from" not in hit.hints[0] and hit.case_hits[0].action.script_version == 2


def test_kb_cases_show_and_cases_md_print_the_scope(kb_cfg):
    kb = FileKnowledgeBase(kb_cfg)
    kb.cases.add_script(C, L2, FULL, run_id="F1", project="a")
    kb.cases.add_script(C, L2, REDUCED, run_id="R1", project="fw", scope="jdk-jakarta")
    kb.cases.credit(C, 1, OUT_OF_SCOPE, project="fw", run_id="R1", round=1, n=1, reason=GUARD, scope="jdk-jakarta")
    res = CliRunner().invoke(kb_cli, ["cases", "show", C, "--no-code"], obj={"kb": kb}, catch_exceptions=False)
    assert res.exit_code == 0
    assert "script v1 (fix_v1.py) unverified, scope full (not recorded)," in res.output
    assert "script v2 (fix_v2.py) unverified, scope jdk-jakarta," in res.output
    assert "try 1: out_of_scope (scope jdk-jakarta)" in res.output
    case = kb.cases.find(C)
    assert scripts_text(case) == "v1 unverified 0/0, v2 jdk-jakarta unverified 0/0"
    assert run_line(case.scripts[0].runs[-1]).startswith("run R1 fw r1 try 1: out_of_scope (scope jdk-jakarta)")
    kb.cases.write_cases_md()
    assert "v2 jdk-jakarta unverified 0/0" in (kb.cases.md_path).read_text(encoding="utf-8")


def test_reindex_keeps_the_scopes_and_reads_a_guard_rollback_as_out_of_scope(store):
    v1 = store.add_script(C, L2, FULL, run_id="F1", project="a", scope="full").version
    v2 = store.add_script(C, L2, REDUCED, run_id="R1", project="fw", scope="jdk-jakarta").version
    store.credit(C, v2, "improved", project="fw", run_id="R1", round=1, n=2, scope="jdk-jakarta")
    # R9's credit was lost (an older image); its errors.jsonl line still shows the guard rollback of v1
    e = ErrorEntry(id="e9", run_id="R9", project="fw9", case_id=C, l2=L2, kind=ErrorKind.COMPILE, file="A.java", line=1,
                   message="package javax.servlet.http does not exist", seen="compile", status="unresolved",
                   tries=[FixTry(n=1, round=1, source="kb-script", script_version=v1, applied="script",
                                 result="apply_failed", note=GUARD)])
    store.record_run([e])
    store.recount()
    full, red = by_version(store, v1), by_version(store, v2)
    assert [(r.run_id, r.result, r.scope) for r in full.runs] == [("R9", OUT_OF_SCOPE, "jdk-jakarta")]
    assert full.last_failure == "" and full.failures == 0
    assert [(r.run_id, r.scope) for r in red.runs] == [("R1", "jdk-jakarta")]


# --------------------------------------------------------------------------- K1: three processes, two scopes
ROUNDS = 6


def _scope_worker() -> None:
    """``python -c '…_scope_worker()' <kb> <out> <index>``: credits one shared case in its own scope."""
    kb_dir, out, i = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
    store = CaseStore(kb_dir, None, lock_timeout=60, script_runs=0)
    scope = "jdk-jakarta" if i == 2 else "full"
    (out / f"ready-{i}").write_text("ready")
    t0 = time.monotonic()
    while not (out / "start").exists():
        if time.monotonic() - t0 > 60:
            raise SystemExit("no start signal")
        time.sleep(0.002)
    for k in range(ROUNDS):
        for v in (1, 2):                                      # v1 is full, v2 is jdk-jakarta (stored before)
            result = "improved" if k % 2 == 0 else "no_change"
            assert store.credit(C, v, result, project=f"p{i}", run_id=f"r{i}", round=1 + v, n=k + 1,
                                scope=scope) is not None
    (out / f"done-{i}").write_text("done")


def test_three_processes_credit_one_case_in_two_scopes(tmp_path):
    kb = tmp_path / "kb"
    seed = CaseStore(kb, None, script_runs=0)
    seed.add_script(C, L2, FULL, run_id="F0", project="p-full", scope="full")
    seed.add_script(C, L2, REDUCED, run_id="R0", project="p-red", scope="jdk-jakarta")
    out = tmp_path / "out"
    out.mkdir()
    code = "from tests.kb.test_case_scope import _scope_worker; _scope_worker()"
    env = {**os.environ, "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    ps = [subprocess.Popen([sys.executable, "-c", code, str(kb), str(out), str(i)], cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for i in range(3)]
    try:
        t0 = time.monotonic()
        while not all((out / f"ready-{i}").exists() for i in range(3)):
            assert time.monotonic() - t0 < 60, "the writers never got ready"
            assert all(p.poll() is None for p in ps), "a writer died before the start"
            time.sleep(0.01)
        (out / "start").write_text("go")
        outputs = [p.communicate(timeout=120)[0].decode("utf-8", "replace") for p in ps]
    finally:
        for p in ps:
            if p.poll() is None:
                p.kill()
    for i, p in enumerate(ps):
        assert p.returncode == 0, f"writer {i} failed:\n{outputs[i]}"
    store = CaseStore(kb, None)
    v1, v2 = by_version(store, 1), by_version(store, 2)
    half = ROUNDS // 2
    # v1 (full): two full writers count, the reduced one is in runs[] only; v2 (jdk-jakarta): the other way round
    assert (v1.successes, v1.failures) == (2 * half, 2 * half) and sorted(v1.projects) == ["p0", "p1"]
    assert (v2.successes, v2.failures) == (half, half) and v2.projects == ["p2"]
    assert len(v1.runs) == len(v2.runs) == 3 * ROUNDS                # nothing lost under the lock
    assert sum(1 for r in v1.runs if r.scope == "jdk-jakarta") == ROUNDS
    assert sum(1 for r in v2.runs if r.scope == "full") == 2 * ROUNDS
    assert v1.status == "proven" and v2.status == "proven"            # p0 / p1 resp. p2 are not the makers


# --------------------------------------------------------------------------- K2: an id a re-sign moved
def test_credit_and_add_script_follow_a_case_a_resign_moved(tmp_path, cfg, monkeypatch, caplog):
    """Port of the reviewer's ``par3/kb/stale_id.py``: pod B matched case O, pod A's record_run re-signed (an
    older-image pod wrote sig-0 lines) and moved O whole to N; B credits / augments under O afterwards."""
    from sbmig.build import signatures as sigs
    monkeypatch.setattr(sigs, "SIGNATURE_VERSION", 2)
    monkeypatch.setattr(sigs, "SIGNATURE_CHANGES", {2: frozenset({"foo"})})
    l2 = "compile|foo|keySet"
    old_id = case_id_for(l2)
    new_l2 = "compile|foo|Lib.keySet"
    new_id = case_id_for(new_l2)
    v1_code = "def fix(p):\n  p.log('rewrite Lib.keySet')\n"

    def signer(err):  # rules 2: a member of a library type carries the type
        return new_l2 if "Lib" in (err.location or "") else err.signature_generic

    def ent(eid, l2_, sig, run, proj, tries, loc=""):
        return ErrorEntry(id=eid, run_id=run, project=proj, case_id=case_id_for(l2_), l2=l2_, kind=ErrorKind.COMPILE,
                          file="src/A.java", line=1, message="cannot find symbol", seen="compile", status="resolved",
                          symbol="method keySet()", location=loc, sig=sig, tries=tries)
    kbd = tmp_path / "kb"
    older = CaseStore(kbd, cfg)
    older.add_script(old_id, l2, v1_code, run_id="r0", project="p0")
    older.record_run([ent("o1", l2, 0, "r0", "p0", [FixTry(n=1, round=1, source="llm-new", script_version=1,
                                                         applied="script", result="improved")],
                          loc="variable h of type com.lib.Lib")])
    a, b = FileKnowledgeBase(cfg, kb_dir=kbd), FileKnowledgeBase(cfg, kb_dir=kbd)
    a.cases.signer = b.cases.signer = signer
    m = b.match(cluster(l2))
    assert [(h.case_id, h.action.script_version) for h in m.case_hits] == [(old_id, 1)]
    a.record_run([ent("a1", "compile|bar|other", 2, "rA", "pA", [])])        # A re-signs: O moves whole to N
    assert old_id not in a.cases.case_ids() and new_id in a.cases.case_ids()
    # B credits the version it ran under the id it holds: credited where the script went
    s = b.cases.credit(old_id, 1, "no_change", project="pB", run_id="rB", round=1, n=1, code=v1_code)
    assert s is not None and s.version == 1
    moved = b.cases.find(new_id)
    assert [(r.run_id, r.result) for r in moved.scripts[0].runs][-1] == ("rB", "no_change")
    assert moved.scripts[0].failures == 1
    # B's augment: its error's L2 (keySet on a project type) is still O's under rules 2 → a fresh O, no parent
    caplog.set_level(logging.WARNING, logger="sbmig.errors")
    aug = b.cases.add_script(old_id, l2, "def fix(p):\n  p.log('B augment for project keySet')\n", run_id="rB",
                             project="pB", source="llm-augment", parent=1, reason="v1: no_change")
    assert aug.parent is None and aug.version == 2                        # numbered above the dropped parent
    assert "the new script's parent is dropped" in caplog.text
    fresh = b.cases.find(old_id)
    assert [x.version for x in fresh.scripts] == [2]
    # B's errors.jsonl names v1 (the moved script) and v2 (its augment): v1 never lands on O's script
    b.record_run([ent("b1", l2, 2, "rB", "pB", [FixTry(n=1, round=1, source="kb-script", script_version=1,
                                                       applied="script", result="no_change"),
                                                FixTry(n=2, round=1, source="llm-augment", script_version=2,
                                                       applied="script", result="improved")])])
    b.reindex()
    fresh = b.cases.find(old_id)
    assert [(r.run_id, r.result) for r in fresh.scripts[0].runs] == [("rB", "improved")]
    assert fresh.scripts[0].parent is None


def test_a_credit_for_a_case_nobody_has_is_one_warning(store, caplog):
    caplog.set_level(logging.WARNING, logger="sbmig.errors")
    assert store.credit("0123456789", 1, "no_change", project="p", run_id="r") is None
    warned = [r for r in caplog.records if "0123456789" in r.getMessage()]
    assert len(warned) == 1 and "no script v1 to credit (no such case)" in warned[0].getMessage()


def test_a_credit_whose_code_is_not_the_stored_version_is_not_recorded_there(store, caplog):
    v = store.add_script(C, L2, FULL, run_id="F1", project="a", scope="full").version
    assert store.credit(C, v, "improved", project="b", run_id="F2", code=REDUCED) is None
    assert by_version(store, v).successes == 0 and "no such version" in caplog.text
    assert store.credit(C, v, "improved", project="b", run_id="F2", code=FULL).successes == 1


def test_case_json_of_a_scope_written_today_round_trips(store):
    store.add_script(C, L2, REDUCED, run_id="R1", project="fw", scope="jdk-jakarta")
    store.credit(C, 1, "improved", project="fw", run_id="R1", scope="jdk-jakarta")
    data = json.loads((store.case_dir(C) / "case.json").read_text(encoding="utf-8"))
    assert data["scripts"][0]["scope"] == "jdk-jakarta" and data["scripts"][0]["runs"][0]["scope"] == "jdk-jakarta"
