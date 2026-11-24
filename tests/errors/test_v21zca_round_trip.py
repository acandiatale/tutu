"""v2.1z-ca, the store part: ``CaseStore.credit`` with the result ``round_trip``.

A stored version that took part in a round trip (two cases undid each other's fix) is credited ``round_trip`` (a
failure), a success it got for that same try is taken back, and the version carries the mark ``round_trip`` that
keeps ``best`` from selecting it until a later success that was not a reuse (in either scope) or ``activate``
clears it. A ``kb import`` takes the larger counts, corrected by the take-backs the merge can see.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from sbmig.core.models import CaseScript
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import FAILURE_RESULTS, ROUND_TRIP, SUCCESS_RESULTS, scripts_text

L2 = "compile|cannot_find_symbol|org.example.Thing"
CID = case_id_for(L2)
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\n"
WHY = ("round trip: the error(s) this version resolved came back when attempt verify-2-1 (case abc) put back the "
       "lines it had changed in src/A.java")


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def v(store: CaseStore, version: int, cid: str = CID) -> CaseScript:
    return next(s for s in store.find(cid).scripts if s.version == version)


def test_round_trip_is_a_failure_result():
    assert ROUND_TRIP == "round_trip" and ROUND_TRIP in FAILURE_RESULTS and ROUND_TRIP not in SUCCESS_RESULTS


def test_a_round_trip_credit_counts_a_failure_and_marks_the_version(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    s = store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    assert s is not None and s.round_trip
    got = v(store, 1)
    assert (got.successes, got.failures, got.last_result, got.round_trip) == (0, 1, ROUND_TRIP, True)
    assert got.last_failure.startswith("round trip: the error(s) this version resolved")
    assert [(r.result, r.n) for r in got.runs] == [(ROUND_TRIP, 1)]


def test_the_success_of_the_same_try_is_taken_back(store, caplog):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=2, n=3)
    assert (v(store, 1).successes, v(store, 1).projects) == (1, ["p1"])
    with caplog.at_level(logging.INFO, logger="sbmig"):
        store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=2, n=3, reason=WHY)
    got = v(store, 1)
    assert (got.successes, got.failures, got.projects, got.status, got.round_trip) == (0, 1, [], "unverified", True)
    assert [(r.run_id, r.round, r.n, r.result) for r in got.runs] == [("r1", 2, 3, ROUND_TRIP)]   # replaced
    assert any("taken back (round trip)" in r.getMessage() for r in caplog.records)


def test_a_proven_version_goes_back_to_unverified_when_the_taken_back_success_made_it_proven(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    assert v(store, 1).status == "proven"
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.status, got.successes, got.failures, got.projects) == ("unverified", 0, 1, [])


def test_proven_stays_when_another_project_still_resolved_it(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="a", run_id="r2", round=1, n=1)
    store.credit(CID, 1, "improved", project="b", run_id="r3", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="b", run_id="r3", round=1, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.status, got.successes, got.failures, got.projects) == ("proven", 1, 1, ["a"])


def test_the_project_stays_when_another_kept_success_run_names_it(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="a", run_id="r2", round=1, n=1)
    store.credit(CID, 1, "build_ok", project="a", run_id="r2", round=4, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="a", run_id="r2", round=4, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.successes, got.projects, got.status) == (1, ["a"], "proven")


def test_a_retired_version_stays_retired(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    store.retire(CID, 1)
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.status, got.successes, got.failures, got.round_trip) == ("retired", 0, 1, True)


def test_successes_never_go_below_zero(store, tmp_path):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    # a hand-edited case.json whose counter disagrees with its runs
    path = tmp_path / "kb" / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scripts"][0]["successes"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    assert v(store, 1).successes == 0


@pytest.mark.parametrize("result", ["no_change", "regressed", "reverted", "apply_failed", "blocked", "partial",
                                    "inconclusive", "improved", "build_ok"])
def test_no_other_result_takes_a_success_back(store, result):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    store.credit(CID, 1, result, project="other", run_id="r2", round=1, n=1)
    got = v(store, 1)
    assert got.successes == (2 if result in SUCCESS_RESULTS else 1)
    assert got.projects == ["other"] and got.status == "proven" and not got.round_trip
    assert got.failures == (1 if result in FAILURE_RESULTS else 0)


def test_a_try_without_a_number_or_of_another_try_takes_nothing_back(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=2, reason=WHY)    # another try
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", reason=WHY)                  # n = 0
    got = v(store, 1)
    assert (got.successes, got.failures, got.status, got.projects) == (1, 2, "proven", ["other"])


def test_the_same_try_credited_round_trip_twice_counts_once(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.successes, got.failures, got.status) == (0, 1, "unverified")
    assert len(got.runs) == 1


def test_round_trip_over_a_failure_of_the_same_try_is_not_a_second_failure(store):
    """B in mode `revert`: the attempt's credit is `round_trip` instead of `reverted` — one failure for one try."""
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, "reverted", project="p1", run_id="r1", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    got = v(store, 1)
    assert (got.failures, got.round_trip, got.last_result) == (1, True, ROUND_TRIP)


def test_a_round_trip_run_of_the_other_scope_counts_nothing_but_sets_the_mark(store):
    """The counts and the status stay own-scope only; the mark is about reuse and is set in every scope."""
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker", scope="full")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1, scope="full")
    store.credit(CID, 1, ROUND_TRIP, project="fw", run_id="r3", round=1, n=1, reason=WHY, scope="jdk-jakarta")
    got = v(store, 1)
    assert (got.successes, got.failures, got.status, got.round_trip, got.last_result) == \
        (1, 0, "proven", True, "improved")
    assert [r.result for r in got.runs][-1] == ROUND_TRIP
    assert store.best(CID) is None and store.best(CID, "jdk-jakarta") is None
    # the same try key in the other scope does not take the own-scope success back either
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY, scope="jdk-jakarta")
    got = v(store, 1)
    assert (got.successes, got.failures, got.status, got.round_trip) == (1, 0, "proven", True)


def test_a_full_run_that_reused_a_jdk_jakarta_version_marks_it(store):
    """A full run may reuse one other-scope version per case and run; its round trip keeps the version from
    being picked again without the LLM (stored_best / stored_next, either scope)."""
    from sbmig.triage.scripts import stored_best, stored_next
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0", scope="jdk-jakarta")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1, scope="full")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, scope="full", reason=WHY)
    got = v(store, 1)
    assert (got.successes, got.failures, got.status, got.round_trip) == (0, 0, "unverified", True)
    assert stored_best(store, CID, "full") is None and stored_best(store, CID, "jdk-jakarta") is None
    assert stored_next(store, CID, (), scope="full") is None


def test_a_success_of_a_reuse_counts_and_keeps_the_mark(store, caplog):
    """Another pod planned a reuse before the mark was set and credits its success afterwards."""
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    with caplog.at_level(logging.INFO, logger="sbmig"):
        store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1, reused=True)
    got = v(store, 1)
    assert (got.successes, got.failures, got.round_trip, got.status, got.projects) == \
        (1, 1, True, "proven", ["p2"])
    assert store.best(CID) is None
    assert not any("round-trip mark cleared" in r.getMessage() for r in caplog.records)
    # an LLM answer whose code is the stored code (not a reuse) clears it
    store.credit(CID, 1, "build_ok", project="p3", run_id="r3", round=1, n=1, reused=False)
    assert not v(store, 1).round_trip and store.best(CID)[0].version == 1


def test_best_never_selects_a_marked_version_and_falls_through(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.add_script(CID, L2, CODE2, run_id="r1", project="p1")
    store.credit(CID, 2, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    assert store.best(CID)[0].version == 1
    assert store.latest(CID)[0].version == 2                       # still the augment base
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=2, reason=WHY)
    assert store.best(CID) is None


def test_a_later_success_of_its_own_scope_clears_the_mark(store, caplog):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1", scope="full")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1, scope="jdk-jakarta", reused=True)
    assert v(store, 1).round_trip and store.best(CID) is None   # a reuse of the other scope leaves it too
    with caplog.at_level(logging.INFO, logger="sbmig"):
        store.credit(CID, 1, "improved", project="p2", run_id="r3", round=1, n=1)
    assert not v(store, 1).round_trip and store.best(CID)[0].version == 1
    assert sum("round-trip mark cleared" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("vscope,run_scope", [("jdk-jakarta", "full"), ("full", "jdk-jakarta")])
def test_a_success_of_the_other_scope_that_was_not_a_reuse_clears_the_mark(store, caplog, vscope, run_scope):
    """add_script finds a version by its code across scopes: an LLM answer of the other scope with the stored code
    runs this version; its success clears the mark, the counts stay own-scope only."""
    store.add_script(CID, L2, CODE1, run_id="r0", project="maker", scope=vscope)
    store.credit(CID, 1, "no_change", project="p1", run_id="r1", round=1, n=1, scope=vscope, reason="nc")
    store.credit(CID, 1, ROUND_TRIP, project="p2", run_id="r2", round=1, n=1, scope=run_scope, reason=WHY)
    assert store.add_script(CID, L2, CODE1, run_id="r3", project="p3", scope=run_scope).version == 1
    before = v(store, 1)
    with caplog.at_level(logging.INFO, logger="sbmig"):
        store.credit(CID, 1, "improved", project="p3", run_id="r3", round=1, n=1, scope=run_scope)
    got = v(store, 1)
    assert not got.round_trip
    assert (got.successes, got.failures, got.projects, got.status, got.last_result) == \
        (before.successes, before.failures, before.projects, before.status, before.last_result) == \
        (0, 1, [], "unverified", "no_change")
    assert [(r.run_id, r.result, r.scope) for r in got.runs][-1] == ("r3", "improved", run_scope)
    assert sum("round-trip mark cleared" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("run_scope", ["", "jdk-jakarta"])
def test_a_failure_in_either_scope_never_clears_the_mark(store, run_scope):
    store.add_script(CID, L2, CODE1, run_id="r0", project="maker")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.credit(CID, 1, "no_change", project="p2", run_id="r2", round=1, n=1, scope=run_scope, reason="nc")
    store.credit(CID, 1, "partial", project="p2", run_id="r3", round=1, n=1, scope=run_scope)
    assert v(store, 1).round_trip


def test_activate_clears_the_mark(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    s = store.activate(CID, 1)
    assert not s.round_trip and not v(store, 1).round_trip and store.best(CID)[0].version == 1
    # retire keeps it
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=2, reason=WHY)
    assert store.retire(CID, 1).round_trip


def test_scripts_text_and_describe_name_the_mark(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    assert scripts_text(store.find(CID)) == "v1 unverified round-trip 0/1"
    assert store.describe(CID) == "케이스는 있으나 쓸 스크립트 없음 (v1 unverified round-trip 0/1)"
    s = v(store, 1)
    s2 = s.model_copy(update={"non_idempotent": True})
    info = store.find(CID).model_copy(update={"scripts": [s2]})
    assert scripts_text(info) == "v1 unverified non-idempotent round-trip 0/1"


def test_a_case_json_without_the_field_reads_as_false(store, tmp_path):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    path = tmp_path / "kb" / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for s in data["scripts"]:
        s.pop("round_trip", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert v(store, 1).round_trip is False and store.best(CID)[0].version == 1
    assert CaseScript(version=1, file="fix_v1.py").round_trip is False


def test_a_kb_merge_keeps_the_mark(store, tmp_path):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    other = CaseStore(tmp_path / "kb2", None)
    other.add_script(CID, L2, CODE1, run_id="r9", project="p9")
    other.merge_from(store)
    assert [x.round_trip for x in other.find(CID).scripts if other.code(CID, x.version) == CODE1] == [True]
    # and the other way round: a marked local version stays marked when the incoming one is not
    third = CaseStore(tmp_path / "kb3", None)
    third.add_script(CID, L2, CODE1, run_id="r8", project="p8")
    store.merge_from(third)
    assert v(store, 1).round_trip


def test_a_round_trip_credit_follows_a_case_a_resign_moved(store, tmp_path):
    old_l2 = "compile|cannot_find_symbol|Thing"
    old = case_id_for(old_l2)
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    path = tmp_path / "kb" / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["aliases"] = [old]
    path.write_text(json.dumps(data), encoding="utf-8")
    s = store.credit(old, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY, code=CODE1)
    assert s is not None
    got = v(store, 1)
    assert (got.successes, got.failures, got.status, got.round_trip) == (0, 1, "unverified", True)


def test_cases_show_names_the_marks_on_the_version_line(tmp_path, cfg):
    from typer.testing import CliRunner

    from sbmig.kb import FileKnowledgeBase, kb_cli
    kb = FileKnowledgeBase(cfg.model_copy(update={"kb": cfg.kb.model_copy(update={"dir": str(tmp_path / "kb")})}))
    store = kb.cases
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.add_script(CID, L2, CODE2, run_id="r1", project="p1")
    store.add_script(CID, L2, "def fix(p):\n    p.log('v3')\n", run_id="r1", project="p1")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.mark_non_idempotent(CID, 2)
    store.credit(CID, 2, ROUND_TRIP, project="p1", run_id="r1", round=1, n=2, reason=WHY)
    res = CliRunner().invoke(kb_cli, ["cases", "show", CID, "--no-code"], obj={"kb": kb}, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "script v1 (fix_v1.py) unverified round-trip, scope full" in res.output
    assert "script v2 (fix_v2.py) unverified non-idempotent round-trip, scope full" in res.output
    assert "script v3 (fix_v3.py) unverified, scope full" in res.output


# ---------------------------------------------------------------- kb import of an export made before the round trip
def _proven_then_exported(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    assert v(store, 1).status == "proven"
    earlier = CaseStore(tmp_path / "kb-earlier", cfg)
    earlier.merge_from(store)
    assert (v(earlier, 1).successes, v(earlier, 1).status) == (1, "proven")
    return earlier


def test_import_of_an_earlier_export_does_not_bring_the_taken_back_success_back(store, tmp_path, cfg):
    earlier = _proven_then_exported(store, tmp_path, cfg)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.merge_from(earlier)
    got = v(store, 1)
    assert (got.round_trip, got.successes, got.failures, got.status, got.projects) == \
        (True, 0, 1, "unverified", [])
    assert [(r.run_id, r.n, r.result) for r in got.runs] == [("r1", 1, ROUND_TRIP)]


def test_an_earlier_kb_importing_the_later_export_takes_the_success_back_too(store, tmp_path, cfg):
    """The other direction: the local KB holds the success, the imported one the round trip of the same try."""
    earlier = _proven_then_exported(store, tmp_path, cfg)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    earlier.merge_from(store)
    got = v(earlier, 1)
    assert (got.round_trip, got.successes, got.failures, got.status, got.projects) == \
        (True, 0, 1, "unverified", [])
    assert [(r.run_id, r.n, r.result) for r in got.runs] == [("r1", 1, ROUND_TRIP)]


def test_a_marked_version_without_a_shared_try_merges_by_the_larger_counts(store, tmp_path, cfg):
    """No try is round_trip on one side and a success on the other: the larger counts, as for any version (the
    withdrawn recount from the runs read 2 successes here)."""
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="a", run_id="r1", round=1, n=1)
    store.credit(CID, 1, "improved", project="fw", run_id="r5", round=1, n=1, scope="jdk-jakarta")   # other scope
    store.credit(CID, 1, ROUND_TRIP, project="b", run_id="r2", round=1, n=1, reason=WHY)
    other = CaseStore(tmp_path / "kb-other", cfg)
    other.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    other.credit(CID, 1, "improved", project="c", run_id="r3", round=1, n=1)
    store.merge_from(other)
    got = v(store, 1)
    assert (got.round_trip, got.successes, got.failures, got.status) == (True, 1, 1, "proven")
    assert sorted(got.projects) == ["a", "c"]


def test_a_retired_marked_version_stays_retired_after_an_import(store, tmp_path, cfg):
    earlier = _proven_then_exported(store, tmp_path, cfg)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.retire(CID, 1)
    store.merge_from(earlier)
    assert (v(store, 1).status, v(store, 1).successes) == ("retired", 0)


def test_an_unmarked_version_still_merges_by_the_larger_counts(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    other = CaseStore(tmp_path / "kb-other", cfg)
    other.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    path = tmp_path / "kb-other" / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["scripts"][0].update(successes=3, failures=2, projects=["x"], status="proven")   # counts without runs
    path.write_text(json.dumps(data), encoding="utf-8")
    store.merge_from(other)
    got = v(store, 1)
    assert (got.successes, got.failures, got.projects, got.status, got.round_trip) == (3, 2, ["x"], "proven", False)


# ---------------------------------------------------------------- kb import: the larger counts, corrected by the take-backs
def _snap(s: CaseScript):
    return (s.successes, s.failures, list(s.projects), s.status, s.round_trip)


def _copy(store: CaseStore, tmp_path: Path, cfg, name: str) -> CaseStore:
    other = CaseStore(tmp_path / name, cfg)
    other.merge_from(store)
    return other


def test_import_keeps_the_counts_a_capped_history_no_longer_holds(tmp_path, cfg):
    cfg.errors.script_runs = 3
    store = CaseStore(tmp_path / "kb", cfg)
    store.add_script(CID, L2, CODE1, run_id="r0", project="maker")
    for i in range(1, 6):
        store.credit(CID, 1, "improved", project=f"p{i}", run_id=f"r{i}", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="maker", run_id="r6", round=1, n=1, reason=WHY)
    store.credit(CID, 1, "no_change", project="maker", run_id="r7", round=1, n=1, reason="nc")
    store.credit(CID, 1, "no_change", project="maker", run_id="r8", round=1, n=1, reason="nc")
    before = _snap(v(store, 1))
    assert before == (5, 3, ["p1", "p2", "p3", "p4", "p5"], "proven", True)
    assert [r.result for r in v(store, 1).runs] == [ROUND_TRIP, "no_change", "no_change"]   # successes capped away
    store.merge_from(_copy(store, tmp_path, cfg, "kb-exp"))
    assert _snap(v(store, 1)) == before


def test_import_keeps_counts_that_predate_run_records(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="maker")
    case, extra = store._read(CID)
    case.scripts[0].successes, case.scripts[0].projects, case.scripts[0].status = 4, ["a", "b"], "proven"
    store._save(case, extra)
    store.credit(CID, 1, ROUND_TRIP, project="c", run_id="r9", round=1, n=1, reason=WHY)
    before = _snap(v(store, 1))
    assert before == (4, 1, ["a", "b"], "proven", True)
    store.merge_from(_copy(store, tmp_path, cfg, "kb-exp"))
    assert _snap(v(store, 1)) == before


def test_reimporting_a_kbs_own_export_changes_nothing(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="p2", run_id="r2", round=1, n=1, reason=WHY)
    store.credit(CID, 1, "no_change", project="p3", run_id="r3", round=1, n=0, reason="nc")
    before = v(store, 1).model_dump(exclude={"runs"}), [(r.run_id, r.n, r.result) for r in v(store, 1).runs]
    exp = _copy(store, tmp_path, cfg, "kb-exp")
    for _ in range(2):
        store.merge_from(exp)
        after = v(store, 1).model_dump(exclude={"runs"}), [(r.run_id, r.n, r.result) for r in v(store, 1).runs]
        assert after == before


@pytest.mark.parametrize("local_marked", [True, False])
def test_the_take_back_holds_in_both_directions(store, tmp_path, cfg, local_marked):
    """One side took the success of try r2 back (round trip), the other still counts it; both have more history."""
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1)
    unmarked = _copy(store, tmp_path, cfg, "kb-unmarked")
    unmarked.credit(CID, 1, "improved", project="p4", run_id="r4", round=1, n=1)      # 3 successes there
    store.credit(CID, 1, ROUND_TRIP, project="p2", run_id="r2", round=1, n=1, reason=WHY)
    store.credit(CID, 1, "no_change", project="p3", run_id="r3", round=1, n=1, reason="nc")   # 1 / 2 here
    assert _snap(v(store, 1)) == (1, 2, ["p1"], "proven", True)
    assert _snap(v(unmarked, 1)) == (3, 0, ["p1", "p2", "p4"], "proven", False)
    dst, src = (store, unmarked) if local_marked else (unmarked, store)
    dst.merge_from(src)
    got = v(dst, 1)
    # successes max(1, 3 - 1) = 2, failures max(2, 0 + 1) = 2
    assert _snap(got) == (2, 2, ["p1", "p4"], "proven", True)
    assert {(r.run_id, r.result) for r in got.runs} == {("r1", "improved"), ("r2", ROUND_TRIP), ("r3", "no_change"),
                                                       ("r4", "improved")}


@pytest.mark.parametrize("local_marked", [True, False])
def test_a_merge_take_back_drops_the_project_and_proven(store, tmp_path, cfg, local_marked):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    store.credit(CID, 1, "improved", project="p0", run_id="r5", round=1, n=1)         # the maker's own project
    unmarked = _copy(store, tmp_path, cfg, "kb-unmarked")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    assert _snap(v(store, 1)) == (1, 1, ["p0"], "unverified", True)
    dst, src = (store, unmarked) if local_marked else (unmarked, store)
    dst.merge_from(src)
    assert _snap(v(dst, 1)) == (1, 1, ["p0"], "unverified", True)


def test_a_merge_take_back_keeps_a_project_another_kept_success_run_names(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    store.credit(CID, 1, "improved", project="p1", run_id="r2", round=1, n=1)
    unmarked = _copy(store, tmp_path, cfg, "kb-unmarked")
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r2", round=1, n=1, reason=WHY)
    unmarked.merge_from(store)
    assert _snap(v(unmarked, 1)) == (1, 1, ["p1"], "proven", True)


def test_a_merge_take_back_leaves_retired(store, tmp_path, cfg):
    earlier = _proven_then_exported(store, tmp_path, cfg)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    earlier.retire(CID, 1)
    earlier.merge_from(store)
    assert _snap(v(earlier, 1)) == (0, 1, [], "retired", True)


def test_activate_then_import_of_an_earlier_export_keeps_the_success_taken_back(store, tmp_path, cfg):
    earlier = _proven_then_exported(store, tmp_path, cfg)
    store.credit(CID, 1, ROUND_TRIP, project="p1", run_id="r1", round=1, n=1, reason=WHY)
    store.activate(CID, 1)
    assert _snap(v(store, 1)) == (0, 1, [], "unverified", False)
    store.merge_from(earlier)
    got = v(store, 1)
    assert _snap(got) == (0, 1, [], "unverified", False)
    assert [(r.run_id, r.result) for r in got.runs] == [("r1", ROUND_TRIP)]


def test_a_round_trip_of_the_other_scope_takes_nothing_back_on_merge(store, tmp_path, cfg):
    store.add_script(CID, L2, CODE1, run_id="r0", project="p0", scope="jdk-jakarta")
    store.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1, scope="jdk-jakarta")
    store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1, scope="full")   # not counted
    exp = _copy(store, tmp_path, cfg, "kb-exp")
    store.credit(CID, 1, ROUND_TRIP, project="p2", run_id="r2", round=1, n=1, scope="full", reason=WHY)
    before = _snap(v(store, 1))
    assert before == (1, 0, ["p1"], "proven", True)
    store.merge_from(exp)
    exp.merge_from(store)
    assert _snap(v(store, 1)) == before
    assert _snap(v(exp, 1)) == before


def _unmarked_scenarios(work: Path, cfg):
    """Merges without a round_trip anywhere (the verifier's compare_merge scenarios)."""
    c3 = "def fix(p):\n    p.log('v3')\n"
    def mk(name, runs=None):
        if runs:
            c = cfg.model_copy(deep=True)
            c.errors.script_runs = runs
            return CaseStore(work / name, c)
        return CaseStore(work / name, cfg)
    a, b = mk("s1a"), mk("s1b")
    for st in (a, b):
        st.add_script(CID, L2, CODE1, run_id="r0", project="p0")
    a.credit(CID, 1, "improved", project="p1", run_id="r1", round=1, n=1)
    a.credit(CID, 1, "no_change", project="p2", run_id="r2", round=1, n=0, reason="x")
    b.credit(CID, 1, "improved", project="p3", run_id="r3", round=1, n=2)
    b.credit(CID, 1, "no_change", project="p1", run_id="r1", round=1, n=1, reason="y")   # same try as a's success
    b.credit(CID, 1, "regressed", project="p4", run_id="r4", round=2, n=1, reason="z")
    b.add_script(CID, L2, CODE2, run_id="r3", project="p3", parent=1, reason="aug")
    b.credit(CID, 2, "build_ok", project="p5", run_id="r5", round=1, n=1)
    a.merge_from(b)
    yield a
    b.merge_from(a)
    yield b
    a, b = mk("s2a", 3), mk("s2b", 3)
    for st in (a, b):
        st.add_script(CID, L2, c3, run_id="r0", project="p0", scope="jdk-jakarta")
    for i in range(5):
        a.credit(CID, 1, "improved", project=f"pa{i}", run_id=f"ra{i}", round=1, n=1, scope="jdk-jakarta")
        b.credit(CID, 1, "improved", project=f"pb{i}", run_id=f"rb{i}", round=1, n=1,
                 scope="full" if i % 2 else "jdk-jakarta")
    a.merge_from(b)
    yield a


def test_a_merge_without_a_round_trip_is_the_merge_of_before(tmp_path, cfg):
    """The values the merge of commit 8a618d7 (before round_trip existed) gives for the same scenarios."""
    got = [[(s.version, s.successes, s.failures, sorted(s.projects), s.status,
             [(r.run_id, r.n, r.result) for r in s.runs]) for s in st.find(CID).scripts]
           for st in _unmarked_scenarios(tmp_path, cfg)]
    v2 = (2, 1, 0, ["p5"], "proven", [("r5", 1, "build_ok")])
    assert got == [
        [(1, 1, 2, ["p1", "p3"], "proven",
          [("r1", 1, "improved"), ("r2", 0, "no_change"), ("r3", 2, "improved"), ("r4", 1, "regressed")]), v2],
        [(1, 1, 2, ["p1", "p3"], "proven",
          [("r2", 0, "no_change"), ("r3", 2, "improved"), ("r1", 1, "no_change"), ("r4", 1, "regressed")]), v2],
        [(1, 5, 0, ["pa0", "pa1", "pa2", "pa3", "pa4", "pb0", "pb2", "pb4"], "proven",
          [("rb3", 1, "improved"), ("ra4", 1, "improved"), ("rb4", 1, "improved")])],
    ]
