"""Regression tests for the third review of the remediation's build fix loop.

The review's reproductions (`out/scratch/review-bk/test_repro_bk.py`, which asserted the buggy behaviour) turned around
to assert the right outcome, plus the rules that came with them:
1. a candidate is used for a build only with the proof that its applied files are stored completely
   (`rounds/<r>/expected.json` + its sha256 in `candidate.ready.json`); a run started by the previous version (no
   `applied/`, a marker without the proof) goes back through `apply` and keeps its pom edits;
2. a path the sync cannot put back fails the round (`internal_error`, nothing of the round delivered); a read-only
   folder or file left by the build is made writable once first;
3. a loop result `stop requested` is not a verdict: a resume may run the loop again;
4. a killed loop's LLM calls are counted at the next entry of the build node whatever happens next, exactly once;
5. the sync reads each candidate file at most once and never re-reads accepted/ for unchanged files; the expected
   table is built from hashes that already exist; `_fix_tree_hash` reads no tree;
6. the candidate is synchronised right before it is committed and right before it is frozen as a failed delivery.
No Maven, no LLM.
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from sbmig.remediations import graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations.fixer import fix_usage
from sbmig.remediations.models import (
    FAILED,
    STOPPED,
    SUCCEEDED,
    FixResult,
    RemediationError,
    RemediationState,
    read_json,
)

from .fakes import FakeMaven, Recorder, make_cfg, make_run
from .test_fix_loop_review2 import Killed, KilledFixer
from .test_graph import POM2, _two_round_components, one_fix_components, run
from .test_graph_fix_loop import APP, FakeFixer, zip_files

ORIGINAL_APP = b"class App {}\n"


def _saved(rec: Recorder) -> RemediationState:
    return RemediationState.from_dict(rec.saves[-1])


def _stopped_at_build(tmp_path):
    """A run stopped during the candidate build: candidate + marker + applied/ + expected.json exist."""
    state, paths = make_run(tmp_path)
    rec = Recorder()
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:
            rec.stop_flag = True
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False], on_package=on_package)),
                 rec=rec)
    assert out.status == STOPPED and out.next_node == "build"
    return paths, rec


def _as_the_old_version_left_it(paths):
    """What the previous deployed version left: no applied/, no expected.json, a marker without the proof."""
    shutil.rmtree(paths.round_dir(1) / "applied")
    (paths.round_dir(1) / graph.EXPECTED_FILE).unlink()
    marker = paths.round_dir(1) / graph.CANDIDATE_READY
    data = read_json(marker)
    data.pop("expected")
    marker.write_text(json.dumps(data), encoding="utf-8")


def _events(rec, name):
    return [e for e in rec.events if e.get("event") == name]


# ------------------------------------------------------------------------------------------------ 1. the proof
def test_old_version_run_resumed_at_build_keeps_the_applied_pom_edits(tmp_path):
    paths, rec = _stopped_at_build(tmp_path)
    _as_the_old_version_left_it(paths)
    rec2 = Recorder()
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False])), rec=rec2)
    files = zip_files(paths.delivery_zip)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert "change set" in out.message
    assert b"<version>1.1</version>" in files["pom.xml"]               # the applied edit is delivered
    assert out.rounds[0].build_changed == []                            # and not filed as a build-side change
    [ev] = _events(rec2, "candidate_rebuilt")
    assert ev["why"] == graph.WHY_NO_PROOF
    assert (paths.round_dir(1) / graph.EXPECTED_FILE).is_file()
    assert (paths.round_dir(1) / "applied" / "pom.xml").is_file()


def test_old_version_run_resumed_at_build_with_a_passing_build_commits_the_edit(tmp_path):
    paths, rec = _stopped_at_build(tmp_path)
    _as_the_old_version_left_it(paths)
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True])), rec=Recorder())
    assert out.status == SUCCEEDED, out.message
    assert out.rounds[0].build_changed == []
    assert b"<version>1.1</version>" in zip_files(paths.delivery_zip)["pom.xml"]
    assert b"<version>1.1</version>" in (paths.accepted / "pom.xml").read_bytes()


@pytest.mark.parametrize("kill_point,why", [
    # a kill after `unmark_candidate` and before `mark_candidate` (copy, apply, applied/, expected.json)
    ("no_marker", graph.WHY_NO_MARKER),
    # expected.json written, then the pod died before the marker: same, no marker
    ("expected_without_marker", graph.WHY_NO_MARKER),
    # a marker written by the previous version (no `expected` key)
    ("old_marker", graph.WHY_NO_PROOF),
    # expected.json is not the file the marker vouched for
    ("expected_replaced", graph.WHY_NO_PROOF),
    # a stored applied file is gone
    ("applied_file_missing", graph.WHY_NO_PROOF),
])
def test_every_kill_point_of_apply_sends_the_candidate_back_through_apply(tmp_path, kill_point, why):
    paths, rec = _stopped_at_build(tmp_path)
    rd = paths.round_dir(1)
    marker, exp = rd / graph.CANDIDATE_READY, rd / graph.EXPECTED_FILE
    if kill_point == "no_marker":
        marker.unlink()
        exp.unlink()
        shutil.rmtree(rd / "applied")
        (paths.candidate(1) / "pom.xml").write_bytes((paths.accepted / "pom.xml").read_bytes())   # apply not done
    elif kill_point == "expected_without_marker":
        marker.unlink()
    elif kill_point == "old_marker":
        data = read_json(marker)
        data.pop("expected")
        marker.write_text(json.dumps(data), encoding="utf-8")
    elif kill_point == "expected_replaced":
        data = read_json(exp)
        data["files"]["pom.xml"] = [1, "0" * 64]
        exp.write_text(json.dumps(data), encoding="utf-8")
    else:
        (rd / "applied" / "pom.xml").unlink()
    rec2 = Recorder()
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True])), rec=rec2)
    assert [e["why"] for e in _events(rec2, "candidate_rebuilt")] == [why]
    assert out.status == SUCCEEDED, out.message
    assert out.rounds[0].build_changed == []
    assert b"<version>1.1</version>" in zip_files(paths.delivery_zip)["pom.xml"]


def test_apply_stores_applied_then_expected_then_the_marker(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    order: list[str] = []
    real = graph.write_json_atomic

    def spy(path, data):
        name = Path(path).name
        if name == graph.EXPECTED_FILE:
            assert (paths.round_dir(1) / "applied" / "pom.xml").is_file()     # applied/ is complete before
            assert not (paths.round_dir(1) / graph.CANDIDATE_READY).exists()
        if name == graph.CANDIDATE_READY:
            assert (paths.round_dir(1) / graph.EXPECTED_FILE).is_file()
        order.append(name)
        return real(path, data)
    monkeypatch.setattr(graph, "write_json_atomic", spy)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    assert out.status == SUCCEEDED
    names = [n for n in order if n in (graph.EXPECTED_FILE, graph.CANDIDATE_READY)]
    assert names == [graph.EXPECTED_FILE, graph.CANDIDATE_READY]
    rec = read_json(paths.round_dir(1) / graph.EXPECTED_FILE)
    assert rec["round"] == 1 and rec["applied"] == ["p-1-1"] and rec["applied_files"] == ["pom.xml"]
    cand = intake_mod.file_table(paths.candidate(1), sorted(rec["files"]))
    assert {k: tuple(v) for k, v in rec["files"].items()} == cand
    marker = read_json(paths.round_dir(1) / graph.CANDIDATE_READY)
    assert marker["expected"] == intake_mod.sha256_file(paths.round_dir(1) / graph.EXPECTED_FILE)


# ------------------------------------------------------------------------------------------------ 2. a failed sync
def test_a_read_only_folder_left_by_the_build_is_made_writable_and_the_edit_is_reported(tmp_path):
    state, paths = make_run(tmp_path)
    n = {"k": 0}
    held = []

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:                                  # the passing candidate build
            p = project / APP
            p.write_bytes(p.read_bytes() + b"// written by the build\n")
            os.chmod(p, 0o444)
            os.chmod(p.parent, 0o555)                    # e.g. a plugin leaves the folder read-only
            held.append(p.parent)
    try:
        out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True], on_package=on_package)))
    finally:
        for d in held:
            os.chmod(d, 0o755)
    assert out.status == SUCCEEDED, out.message
    assert zip_files(paths.delivery_zip)[APP] == ORIGINAL_APP           # the build's edit is not delivered
    assert out.rounds[0].build_changed == [APP]                          # and it is reported
    assert _events(rec, "build_side_changes")[0]["paths"] == [APP]


def _unwritable(monkeypatch, rel: str):
    real = intake_mod._write_back

    def refuse(tree, r, src, mode, want_sha=None):
        if r == rel:
            raise PermissionError(13, "Permission denied")
        return real(tree, r, src, mode, want_sha)
    monkeypatch.setattr(intake_mod, "_write_back", refuse)


def _edit_on_build(k: int):
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == k:
            (project / APP).write_bytes(b"class App { build }\n")
            (project / "pom.xml").write_bytes((project / "pom.xml").read_bytes() + b"<!-- sortpom -->\n")
    return on_package


EXPECTED_MESSAGE = ("the round 1 candidate could not be put back to its expected state after the build (1 path(s), "
                    f"first: {APP}: PermissionError); nothing of this round was delivered")


def test_a_path_that_cannot_be_put_back_after_a_passing_build_fails_the_round(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    _unwritable(monkeypatch, APP)
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True],
                                                                    on_package=_edit_on_build(2))))
    assert out.status == FAILED and out.reason == "internal_error"
    assert out.message == EXPECTED_MESSAGE
    rs = out.rounds[0]
    assert not rs.committed and out.delivery is None and not paths.delivery_zip.exists()
    assert (paths.accepted / APP).read_bytes() == ORIGINAL_APP           # nothing committed
    assert rs.build_changed == ["pom.xml"]                               # what could be put back is still recorded
    [ev] = _events(rec, "candidate_sync_failed")
    assert ev["count"] == 1 and ev["paths"] == [APP] and ev["errors"] == ["PermissionError"]
    assert _events(rec, "candidate_not_restored")
    assert "되돌리지 못해 이 라운드의 결과는 전달하지 않았습니다" in (paths.root / "report.md").read_text(encoding="utf-8")


def test_a_failed_sync_after_a_failed_build_delivers_no_failed_candidate(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    _unwritable(monkeypatch, APP)
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False],
                                                                    on_package=_edit_on_build(2))))
    assert out.status == FAILED and out.reason == "internal_error" and out.message == EXPECTED_MESSAGE
    assert out.delivery is None and not paths.delivery_zip.exists()     # failed_candidate: include does not apply
    assert not _events(rec, "failed_candidate_delivered")


def test_a_failed_sync_in_round_two_keeps_the_verified_round_one_delivery(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components(fail_round_two=False)
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 3:                                                  # round 2's candidate build
            (project / "pom.xml").write_bytes((project / "pom.xml").read_bytes() + b"<!-- sortpom -->\n")
    comps.maven.on_package = on_package
    real = intake_mod._write_back

    def refuse(tree, r, src, mode, want_sha=None):
        if r == "pom.xml" and "rounds/2/" in Path(tree).as_posix():
            raise PermissionError(13, "Permission denied")
        return real(tree, r, src, mode, want_sha)
    monkeypatch.setattr(intake_mod, "_write_back", refuse)
    out, rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "internal_error"
    assert out.message.startswith("the round 2 candidate could not be put back")
    d = out.delivery
    assert d is not None and d.round == 1 and d.build_ok and d.scanned
    assert intake_mod.sha256_file(paths.delivery_zip) == d.sha256
    assert b"<version>2.0</version>" in zip_files(paths.delivery_zip)["pom.xml"]
    assert _events(rec, "earlier_delivery_kept")


def test_sync_tree_tries_every_path_and_reports_what_it_put_back(tmp_path, monkeypatch):
    acc, cand = tmp_path / "acc", tmp_path / "cand"
    for d in (acc, cand):
        d.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (acc / name).write_bytes(b"orig")
        (cand / name).write_bytes(b"built")
    (cand / "c.txt").unlink()
    _unwritable(monkeypatch, "a.txt")
    with pytest.raises(intake_mod.SyncFailed) as e:
        intake_mod.sync_tree(cand, lambda rel: acc / rel, ["a.txt", "b.txt", "c.txt"])
    assert e.value.failed == [("a.txt", "PermissionError", "")] and isinstance(e.value, RemediationError)
    assert e.value.changed == ["b.txt"] and e.value.missing == ["c.txt"]
    assert (cand / "b.txt").read_bytes() == b"orig" and (cand / "c.txt").read_bytes() == b"orig"


@pytest.mark.parametrize("fn", ["sync_tree", "sync_tree_table"])
def test_a_read_only_file_in_a_read_only_folder_is_put_back(tmp_path, fn):
    acc, cand = tmp_path / "acc", tmp_path / "cand"
    (acc / "d").mkdir(parents=True)
    (cand / "d").mkdir(parents=True)
    (acc / "d" / "x.txt").write_bytes(b"orig")
    (acc / "d" / "y.txt").write_bytes(b"gone")
    (cand / "d" / "x.txt").write_bytes(b"built!")
    os.chmod(cand / "d" / "x.txt", 0o444)
    os.chmod(cand / "d", 0o555)
    try:
        if fn == "sync_tree":
            res = intake_mod.sync_tree(cand, lambda rel: acc / rel, ["d/x.txt", "d/y.txt"])
        else:
            table = intake_mod.file_table(acc, ["d/x.txt", "d/y.txt"])
            res = intake_mod.sync_tree_table(cand, lambda rel: acc / rel, table, ["d/x.txt", "d/y.txt"])
    finally:
        os.chmod(cand / "d", 0o755)
    assert res == (["d/y.txt"], ["d/x.txt"])
    assert (cand / "d" / "x.txt").read_bytes() == b"orig" and (cand / "d" / "y.txt").read_bytes() == b"gone"
    assert stat.S_IMODE(os.lstat(cand / "d" / "x.txt").st_mode) == 0o644


# ------------------------------------------------------------------------------------------------ 3. stop during the loop
class StoppedLoop:
    """The loop is stopped on request: one LLM call made, nothing fixed."""

    def __init__(self, rec: Recorder, reason: str = "stop requested"):
        self.rec, self.reason, self.calls = rec, reason, 0

    def run(self, *a, **kw):
        self.calls += 1
        self.rec.stop_flag = True
        return FixResult(attempted=True, iterations=1, llm_calls=1, reason=self.reason)


def test_stop_during_the_loop_then_resume_runs_the_loop_again(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=StoppedLoop(rec)),
                 rec=rec)
    assert out.status == STOPPED and out.next_node == "build"
    assert out.rounds[0].fix.reason == "stop requested"
    fx = FakeFixer(fixed=False, reason="not fixed: 1 error(s) left")
    rec2 = Recorder()
    out2, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=fx), rec=rec2)
    assert len(fx.calls) == 1                                            # the loop ran again
    assert out2.status == FAILED and out2.reason == "candidate_build_failed"
    assert not out2.message.endswith("automatic build fix: stop requested")
    assert out2.message.endswith("automatic build fix: not fixed: 1 error(s) left")
    rs = out2.rounds[0]
    [gone] = rs.fix_discarded
    assert gone.reason == "stop requested" and gone.llm_calls == 1
    assert fix_usage(out2)["calls"] == 1 + 2                             # the stopped loop's call still counts
    [ev] = _events(rec2, "remediation_fix_discarded")
    assert ev["reason"] == "stop requested"


def test_stop_during_the_loop_then_resume_with_the_loop_off_says_nothing_about_the_stop(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=StoppedLoop(rec)), rec=rec)
    out2, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=FakeFixer()),
                  rec=Recorder(), cfg=make_cfg(fix_loop=False))
    assert out2.status == FAILED and "stop requested" not in out2.message


def test_a_loop_out_of_time_stays_final_on_resume(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]),
                                                  fixer=StoppedLoop(rec, reason="time budget used up")), rec=rec)
    assert out.status == STOPPED
    fx = FakeFixer()
    out2, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=fx), rec=Recorder())
    assert fx.calls == []
    assert out2.status == FAILED and out2.message.endswith("automatic build fix made no attempt: time budget used up")
    assert out2.rounds[0].fix_discarded == []


# ------------------------------------------------------------------------------------------------ 4. a killed loop
def _killed(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=KilledFixer()), rec=rec)
    return paths, rec


def test_killed_loop_usage_is_counted_when_the_resumed_build_passes(tmp_path):
    paths, rec = _killed(tmp_path)
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True]), fixer=FakeFixer()),
                 rec=Recorder())
    assert out.status == SUCCEEDED
    assert fix_usage(out)["calls"] == 2
    [gone] = out.rounds[0].fix_discarded
    assert gone.reason == "interrupted: the run ended during the loop"
    assert not (paths.round_dir(1) / "fixloop").exists()
    assert (paths.round_dir(1) / "fixloop-interrupted-1" / "journal.jsonl").is_file()


def test_killed_loop_usage_is_counted_when_the_loop_is_switched_off(tmp_path):
    paths, rec = _killed(tmp_path)
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=FakeFixer()),
                 rec=Recorder(), cfg=make_cfg(fix_loop=False))
    assert out.status == FAILED and fix_usage(out)["calls"] == 2


def test_killed_loop_usage_is_counted_when_a_stop_comes_during_the_build(tmp_path):
    paths, rec = _killed(tmp_path)
    rec2 = Recorder()

    def on_package(project):
        rec2.stop_flag = True
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False], on_package=on_package),
                                                        fixer=FakeFixer()), rec=rec2)
    assert out.status == STOPPED and fix_usage(out)["calls"] == 2
    assert fix_usage(rec2.saves[-1])["calls"] == 2


def test_a_kill_between_the_save_and_the_rename_counts_the_loop_once(tmp_path, monkeypatch):
    paths, rec = _killed(tmp_path)
    real = os.replace

    def dies_on_rename(src, dst):
        if Path(dst).name.startswith("fixloop-interrupted-"):
            raise Killed()
        return real(src, dst)
    monkeypatch.setattr(graph.os, "replace", dies_on_rename)
    rec2 = Recorder()
    with pytest.raises(Killed):
        run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True]), fixer=FakeFixer()), rec=rec2)
    monkeypatch.setattr(graph.os, "replace", real)
    assert fix_usage(rec2.saves[-1])["calls"] == 2                       # saved before the rename
    assert (paths.round_dir(1) / "fixloop").is_dir()
    rec3 = Recorder()
    out, _ = run(_saved(rec2), paths, one_fix_components(maven=FakeMaven(outcomes=[True]), fixer=FakeFixer()),
                 rec=rec3)
    assert out.status == SUCCEEDED
    assert fix_usage(out)["calls"] == 2 and len(out.rounds[0].fix_discarded) == 1     # no double counting
    assert not _events(rec3, "remediation_fix_interrupted")
    assert not (paths.round_dir(1) / "fixloop").exists()
    assert (paths.round_dir(1) / "fixloop-interrupted-1" / "journal.jsonl").is_file()


def test_a_folder_without_loop_json_gets_a_token_first(tmp_path):
    paths, rec = _killed(tmp_path)
    (paths.round_dir(1) / "fixloop" / graph.LOOP_FILE).unlink()          # killed before loop.json was written
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[True]), fixer=FakeFixer()),
                 rec=Recorder())
    [gone] = out.rounds[0].fix_discarded
    assert gone.loop_id and fix_usage(out)["calls"] == 2
    kept = read_json(paths.round_dir(1) / "fixloop-interrupted-1" / graph.LOOP_FILE)
    assert kept["loop"] == gone.loop_id


def test_the_report_node_counts_a_leftover_loop(tmp_path):
    """Defensive: the graph always passes the build node after a kill in the loop; a state that reaches the report
    node without it still gets the killed loop's usage."""
    paths, rec = _killed(tmp_path)
    saved = _saved(rec)
    saved.next_node = "report"
    out, _ = run(saved, paths, one_fix_components(maven=FakeMaven(outcomes=[True]), fixer=FakeFixer()),
                 rec=Recorder())
    assert fix_usage(out)["calls"] == 2
    assert (paths.round_dir(1) / "fixloop-interrupted-1").is_dir()


# ------------------------------------------------------------------------------------------------ 5. cost of the sync
def test_sync_tree_table_reads_each_candidate_file_once_and_no_unchanged_expected_file(tmp_path, monkeypatch):
    acc, cand = tmp_path / "acc", tmp_path / "cand"
    acc.mkdir()
    cand.mkdir()
    names = ["same.jar", "same2.jar", "edited.txt", "resized.txt", "gone.txt"]
    for n in names:
        (acc / n).write_bytes(b"A" * 64)
    for n in ("same.jar", "same2.jar"):
        (cand / n).write_bytes(b"A" * 64)
    (cand / "edited.txt").write_bytes(b"B" * 64)
    (cand / "resized.txt").write_bytes(b"A" * 65)
    table = intake_mod.file_table(acc, names)
    opened: list[str] = []
    real_open = builtins.open

    def spy(path, *a, **k):
        opened.append(Path(path).parent.name + "/" + Path(path).name)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    res = intake_mod.sync_tree_table(cand, lambda rel: acc / rel, table, names)
    monkeypatch.setattr(builtins, "open", real_open)
    assert res == (["gone.txt"], ["edited.txt", "resized.txt"])
    assert not [o for o in opened if o in ("acc/same.jar", "acc/same2.jar")]     # unchanged: accepted/ never opened
    assert all(opened.count(f"cand/{n}") <= 1 for n in names)                    # the candidate read at most once
    assert "cand/resized.txt" not in opened                                       # a size difference needs no read
    assert opened.count("acc/edited.txt") == 1 and opened.count("acc/gone.txt") == 1


def test_sync_tree_table_refuses_an_expected_file_that_is_not_the_recorded_one(tmp_path):
    acc, cand = tmp_path / "acc", tmp_path / "cand"
    acc.mkdir()
    cand.mkdir()
    (acc / "a.txt").write_bytes(b"orig")
    table = intake_mod.file_table(acc, ["a.txt"])
    (acc / "a.txt").write_bytes(b"ORIG")                                 # changed behind the table's back
    (cand / "a.txt").write_bytes(b"built")
    with pytest.raises(intake_mod.SyncFailed) as e:
        intake_mod.sync_tree_table(cand, lambda rel: acc / rel, table, ["a.txt"])
    assert e.value.code == "expected_changed" and (cand / "a.txt").read_bytes() == b"built"


def test_a_round_syncs_by_table_and_never_hashes_accepted_again(tmp_path, monkeypatch):
    """Round 1 starts from original/manifest.json, round 2 from round 1's expected.json: both are vouched by the
    plan's tree hash, so `intake.file_table` (a full read of accepted/) is never needed; every sync is the table one."""
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components(fail_round_two=False)
    hashed = []
    real = intake_mod.file_table
    monkeypatch.setattr(intake_mod, "file_table", lambda tree, ps: hashed.append(Path(tree).name) or real(tree, ps))
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED, out.message
    assert hashed == []
    assert "sync_tree" not in comps.intake.calls and comps.intake.calls.count("sync_tree_table") == 4
    r2 = read_json(paths.round_dir(2) / graph.EXPECTED_FILE)
    assert {k: tuple(v) for k, v in r2["files"].items()} == real(paths.candidate(2), sorted(r2["files"]))


def test_a_table_that_does_not_match_accepted_falls_back_to_one_read(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    man = read_json(paths.manifest)
    man["entries"][0]["sha256"] = "0" * 64                               # not the bytes in accepted/
    paths.manifest.write_text(json.dumps(man), encoding="utf-8")
    hashed = []
    real = intake_mod.file_table
    monkeypatch.setattr(intake_mod, "file_table", lambda tree, ps: hashed.append(Path(tree).name) or real(tree, ps))
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    assert out.status == SUCCEEDED and hashed == ["accepted"]


def test_fix_tree_hash_reads_no_tree_and_equals_the_tree_hash_after_the_overlay(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    read: list[str] = []
    real_sha = intake_mod._file_sha
    monkeypatch.setattr(intake_mod, "_file_sha", lambda p: read.append(str(p)) or real_sha(p))
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=FakeFixer()))
    assert out.status == SUCCEEDED, out.message
    assert read == []
    fix = out.rounds[0].fix
    assert fix.tree_hash == intake_mod.tree_hash(paths.candidate(1), sorted(read_json(
        paths.round_dir(1) / graph.EXPECTED_FILE)["files"]))


# ------------------------------------------------------------------------------------------------ 6. later Maven calls
class EditingResolveMaven(FakeMaven):
    """`resolve` (run by verify after the build's sync) writes into a manifest path."""

    def resolve(self, project, work, targets=None):
        (Path(project) / APP).write_bytes(b"class App { resolved }\n")
        return super().resolve(project, work, targets)


def test_the_candidate_is_synchronised_right_before_it_is_committed(tmp_path):
    state, paths = make_run(tmp_path)
    out, rec = run(state, paths, one_fix_components(maven=EditingResolveMaven(outcomes=[True, True])))
    assert out.status == SUCCEEDED, out.message
    assert (paths.accepted / APP).read_bytes() == ORIGINAL_APP
    assert zip_files(paths.delivery_zip)[APP] == ORIGINAL_APP
    assert out.rounds[0].build_changed == [APP]
    assert any(e["paths"] == [APP] for e in _events(rec, "build_side_changes"))


class CandidateTouchingFixer:
    """A fixer that breaks its contract and writes into the candidate, then gives up."""

    def run(self, cfg, tree, work_dir, **kw):
        (Path(tree) / APP).write_bytes(b"class App { touched }\n")
        return FixResult(attempted=True, iterations=1, llm_calls=1, reason="not fixed: 1 error(s) left")


def test_the_candidate_is_synchronised_right_before_it_is_frozen_as_a_failed_delivery(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]),
                                                    fixer=CandidateTouchingFixer()))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    files = zip_files(paths.delivery_zip)
    assert files[APP] == ORIGINAL_APP and b"<version>1.1</version>" in files["pom.xml"]
    assert out.rounds[0].build_changed == [APP]
