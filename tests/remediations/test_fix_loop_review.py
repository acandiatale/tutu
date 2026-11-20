"""Regression tests for the review of v2.1z-bh / v2.1z-bi (docs/INTERFACES.md "v2.1z-bj", "Fixer" and "Graph").

Each test is one of the review's reproductions (`out/scratch/review-bi/`, which asserted the buggy behaviour), turned
around to assert the right outcome:
1. a failing git reset / cleanup never leaves the loop's edits, sbmig's `.gitignore` lines or `.git` in the candidate;
2. a git checkout nested in the upload (and a root `.git`) neither hides edits from the guard nor breaks the loop;
3. a fixed round that then drops an ineffective override does not claim a fix the result does not have;
4. the loop never uses the run's whole time budget: "not fixed" stays FAILED with a delivery, never STOPPED;
5. a pod killed after the fix and before the commit resumes with the fix kept, overlaid again and confirmed.
Plus the nits: the loop is not re-run for a round whose loop ran, the flaky first build (`retry_passed`), files a
build removes are restored before freezing. No Maven, no LLM.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sbmig.core.workspace import Workspace
from sbmig.remediations import apply as apply_mod
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations import graph
from sbmig.remediations import planner as P
from sbmig.remediations.fixer import fix_usage
from sbmig.remediations.models import FAILED, SUCCEEDED, FixResult, Inventory, RemediationState
from tests.loop.fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeScriptRunner,
    bad_build,
    compile_error,
    make_profile,
    ok_build,
    write_script,
)

from . import test_fixer as tf
from . import test_module_overrides as MO
from .fakes import (
    POM,
    FakeMaven,
    FakeScanner,
    FakeVersions,
    Recorder,
    ScriptedInventory,
    make_cfg,
    make_components,
    make_run,
    scan_result,
)
from .test_fixer import BUMP, FIRST, FOO, make_tree, snapshot
from .test_fixer import run as run_fixer
from .test_graph import one_fix_components, run
from .test_graph_fix_loop import APP, FIXED, fix_events, zip_files
from .test_graph_fix_loop import FakeFixer as GraphFixer

rcfg = tf.rcfg                                   # the remediation fixture of the fixer tests


class Killed(BaseException):
    """The pod dies (not an Exception: nothing in the graph catches it)."""


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@example.com", "-c", "user.name=a",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _checkout(path: Path) -> list[str]:
    """Make `path` a git checkout with one commit; return its `.git` files relative to `path`."""
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "x")
    return sorted(p.relative_to(path).as_posix() for p in (path / ".git").rglob("*") if p.is_file())


# ------------------------------------------------------------------------------------------------ 1. tree integrity
def test_review1_a_failing_cleanup_never_leaves_git_or_gitignore_lines_in_the_tree(rcfg, tmp_path, monkeypatch,
                                                                                    caplog):
    tree = make_tree(tmp_path, b"build/\n")
    before = snapshot(tree)
    real = shutil.rmtree

    def flaky(p, *a, **k):
        if Path(p).name == "tree":                    # the work copy: an NFS silly-rename file keeps it busy
            raise OSError(39, "Directory not empty")
        return real(p, *a, **k)
    monkeypatch.setattr(fixer_mod.shutil, "rmtree", flaky)
    caplog.set_level(logging.WARNING, logger="sbmig.remediations.fixer")
    res = run_fixer(rcfg, tree, scripts=[write_script({FOO: f"class Foo {{ broken {i} }}\n"}) for i in range(4)])
    assert res.attempted and not res.fixed and res.reason == "not fixed: 1 error(s) left"     # not an error
    assert snapshot(tree) == before                   # no .git, the .gitignore byte for byte, no edit
    assert any("work copy not removed" in r.getMessage() for r in caplog.records)


def test_review1_a_failing_git_reset_never_leaves_the_loops_edits_in_the_tree(rcfg, tmp_path, monkeypatch):
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def revert(self, sha):
        raise RuntimeError("git reset --hard failed: index.lock exists")
    monkeypatch.setattr(Workspace, "revert_to", revert)
    res = run_fixer(rcfg, tree, scripts=[write_script({FOO: "class Foo { broken }\nclass Extra {}\n"})] * 4)
    assert not res.fixed and res.changed == []
    assert snapshot(tree) == before


def test_review1_a_cleanup_failure_after_a_fix_keeps_the_fix_and_the_tree(rcfg, tmp_path, monkeypatch):
    tree = make_tree(tmp_path, b"x\n")
    before = snapshot(tree)
    real = shutil.rmtree
    monkeypatch.setattr(fixer_mod.shutil, "rmtree",
                        lambda p, *a, **k: (_ for _ in ()).throw(OSError(16, "busy")) if Path(p).name == "tree"
                        else real(p, *a, **k))
    res = run_fixer(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})])
    assert res.fixed and res.changed == [FOO] and snapshot(tree) == before
    assert (Path(res.fixed_dir) / FOO).read_text(encoding="utf-8") == "class Foo { ok }\n"


# ------------------------------------------------------------------------------------------------ 2. nested / root .git
S = "svc/src/main/java/S.java"
SVC_FILES = {"pom.xml": "<project/>\n", "svc/pom.xml": "<project/>\n", S: "class S { broken }\n"}


def _svc_build(ws, call):
    return bad_build(compile_error(file=S), goal=call["goal"]) if "broken" in (ws.root / S).read_text() \
        else ok_build(call["goal"])


def _svc_run(rcfg, tree, manifest, scripts):
    return fixer_mod.run_fix_loop(rcfg, tree, tree.parent / "fixloop", run_id="r", targets=[], applied=[BUMP],
                                  manifest_paths=manifest, first_outcome=FIRST, stop=lambda: False, deadline=None,
                                  build_runner=lambda ws: FakeBuildRunner(ws, fn=_svc_build),
                                  llm_fixer=FakeFixer(scripts=scripts), script_runner=FakeScriptRunner(),
                                  profile=make_profile())


def test_review2_a_nested_checkout_neither_hides_edits_nor_survives_in_the_tree(rcfg, tmp_path):
    tree = make_tree(tmp_path, files=SVC_FILES)
    manifest = sorted(set(SVC_FILES) | {f"svc/{p}" for p in _checkout(tree / "svc")})
    before = snapshot(tree)
    res = _svc_run(rcfg, tree, manifest, [write_script({S: f"class S {{ broken {i} }}\n"}) for i in range(4)])
    assert res.attempted and not res.fixed and res.reason == "not fixed: 1 error(s) left"
    assert res.attempts >= 1                                  # the edits were seen and scored, not hidden
    assert snapshot(tree) == before
    # and a fixing try is found by the byte comparison and stored, the nested .git untouched
    res = _svc_run(rcfg, tree, manifest, [write_script({S: "class S { ok }\n"})])
    assert res.fixed and res.changed == [S] and snapshot(tree) == before
    assert (Path(res.fixed_dir) / S).read_text(encoding="utf-8") == "class S { ok }\n"


def test_review2_a_root_git_in_the_upload_does_not_stop_the_loop(rcfg, tmp_path):
    tree = make_tree(tmp_path, files=SVC_FILES)
    manifest = sorted(set(SVC_FILES) | set(_checkout(tree)))
    assert ".git/HEAD" in manifest
    before = snapshot(tree)
    res = _svc_run(rcfg, tree, manifest, [write_script({S: "class S { ok }\n"})])
    assert res.fixed and res.changed == [S], res.reason
    assert snapshot(tree) == before                           # the project's own .git byte for byte


# ------------------------------------------------------------------------------------------------ 3. dropped override
SRC = "app-back/src/main/java/Back.java"


class SrcFixer:
    """Fixes Back.java the v2.1z-bj way (bytes under work_dir/fixed/), never touching the candidate."""

    def __init__(self):
        self.calls = 0

    def run(self, cfg, tree, work_dir, *, manifest_paths, **kw):
        self.calls += 1
        assert "broken" in (Path(tree) / SRC).read_text()
        fixed = Path(work_dir) / "fixed"
        (fixed / SRC).parent.mkdir(parents=True, exist_ok=True)
        (fixed / SRC).write_text(f"class Back {{ fixed {self.calls} }}\n")
        patch = Path(work_dir).parent / "fix.patch"
        patch.write_text("diff\n")
        return FixResult(attempted=True, fixed=True, iterations=2, attempts=1, llm_calls=3, changed=[SRC],
                         patch=str(patch), fixed_dir=str(fixed))


def _drop_run(tmp_path, outcomes):
    files = {**MO.company_files(back_body=MO.DM_SECTION), SRC: "class Back { broken }\n"}
    state, paths = make_run(tmp_path, files)
    models = [MO._three(), MO._three(boot="1.0", back="1.1", front="1.1"),
              MO._three(boot="1.0", back="1.1", front="1.1")]
    fx = SrcFixer()
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(MO._finding())],
                                              "final-r1": [scan_result(MO._finding())]}),
                            inventory=ScriptedInventory([Inventory(items=[MO.item(modules=list(MO.THREE))])]),
                            maven=FakeMaven(models=models, outcomes=outcomes),
                            versions=FakeVersions({"example:t": ["1.0", "1.1"]}),
                            plan=P.plan_round, apply=apply_mod, fixer=fx)
    rec = Recorder()
    out = graph.run_remediation(state, paths, make_cfg(max_rounds=1), comps, save=rec.save, journal=rec.journal,
                                stop=rec.stop)
    return out, paths, fx, rec


def test_review3_a_dropped_override_discards_the_fix_and_claims_nothing(tmp_path):
    # baseline ok, candidate FAILS, confirming build ok (fixed), the rebuilt candidate without the entry passes
    out, paths, fx, rec = _drop_run(tmp_path, [True, False, True, True])
    rs = out.rounds[0]
    assert fx.calls == 1 and rs.committed
    assert rs.fix is None                                     # no fix is claimed for the committed round
    [gone] = rs.fix_discarded
    assert not gone.fixed and gone.reason == "discarded: the candidate was remade"
    assert gone.discarded_changed == [SRC] and gone.changed == [] and gone.patch == "" and gone.fixed_dir == ""
    assert (paths.accepted / SRC).read_text() == "class Back { broken }\n"      # and indeed the result has none
    assert not (paths.round_dir(1) / "fix.patch").exists() and not (paths.round_dir(1) / "fixloop" / "fixed").exists()
    assert fix_usage(out)["calls"] == 3                       # its LLM calls still count
    assert any(e.get("event") == "remediation_fix_discarded" for e in rec.events)
    saved = rec.saves[-1]["rounds"][0]
    assert "fix" not in saved and saved["fix_discarded"][0]["reason"] == "discarded: the candidate was remade"


def test_review3_the_remade_candidates_failing_build_runs_the_loop_once_more(tmp_path):
    # baseline ok, candidate FAILS, confirm ok, rebuilt candidate FAILS, the second loop fixes, its confirm ok
    out, paths, fx, _ = _drop_run(tmp_path, [True, False, True, False, True])
    rs = out.rounds[0]
    assert fx.calls == 2 and rs.committed, out.message
    assert rs.fix.fixed and rs.fix.changed == [SRC] and len(rs.fix_discarded) == 1
    assert (paths.accepted / SRC).read_text() == "class Back { fixed 2 }\n"    # the second fix is the result
    assert fix_usage(out)["calls"] == 6


# ------------------------------------------------------------------------------------------------ 4. time budget
def test_review4_a_loop_that_used_its_time_ends_failed_with_a_delivery_not_stopped(tmp_path):
    state, paths = make_run(tmp_path)
    used = {"s": 0.0}

    def stop():                                               # the chain budget: true once the time is used up
        return 1000.0 - used["s"] <= 0
    stop.time_left = lambda: 1000.0 - used["s"]

    class TimeFixer:
        deadline = None

        def run(self, cfg, tree, work_dir, *, deadline, **kw):
            TimeFixer.deadline = deadline - time.monotonic()
            used["s"] += TimeFixer.deadline                   # the loop runs until its own deadline
            return FixResult(attempted=True, iterations=9, llm_calls=8, reason="time budget used up")
    rec = Recorder()
    out = graph.run_remediation(state, paths, make_cfg(),
                                one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=TimeFixer()),
                                save=rec.save, journal=rec.journal, stop=stop)
    assert 690 < TimeFixer.deadline <= 700                    # 1000 s left - the 300 s reserve
    assert out.status == FAILED and out.reason == "candidate_build_failed", out.message
    assert out.delivery is not None and not out.delivery.build_ok
    assert out.message.endswith(" | automatic build fix: time budget used up")


# ------------------------------------------------------------------------------------------------ 5. kill and resume
def _resume(saves, paths, maven, fx):
    saved = RemediationState.from_dict(saves[-1])
    return run(saved, paths, one_fix_components(maven=maven, fixer=fx), rec=Recorder())


def test_review5_killed_after_the_fix_and_before_the_commit_resumes_with_the_fix(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = GraphFixer()
    real_verify = graph._verify
    calls = {"n": 0}

    def verify(h, r, cand, applied):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Killed()                    # after the confirming build, before the round commit
        return real_verify(h, r, cand, applied)
    monkeypatch.setattr(graph, "_verify", verify)
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=fx), rec=rec)
    assert rec.saves[-1]["rounds"][0]["fix"]["fixed"] is True          # saved right after the loop
    maven = FakeMaven(outcomes=[True])
    out, _ = _resume(rec.saves, paths, maven, fx)
    rs = out.rounds[0]
    assert out.status == SUCCEEDED, out.message
    assert len(fx.calls) == 1                                           # the loop is not run again
    assert rs.committed and rs.fix.fixed and rs.fix.changed == [APP]   # the fix is kept and reported
    assert rs.build.log == "rounds/1/build-fixed.log" and maven.package_calls == [paths.candidate(1)]
    assert zip_files(paths.delivery_zip)[APP] == FIXED.encode()


def test_review5_killed_during_the_confirming_build_overlays_again_and_confirms(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = GraphFixer()
    n = {"calls": 0}

    def on_package(project):
        n["calls"] += 1
        if n["calls"] == 3:
            raise Killed()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True], on_package=on_package),
                                             fixer=fx), rec=rec)
    out, _ = _resume(rec.saves, paths, FakeMaven(outcomes=[True]), fx)
    assert out.status == SUCCEEDED and len(fx.calls) == 1
    assert out.rounds[0].fix.fixed and zip_files(paths.delivery_zip)[APP] == FIXED.encode()


def test_review5_the_overlay_is_redone_on_a_remade_candidate(tmp_path):
    """A resume that finds no completeness marker remakes the candidate from accepted/; the recorded fix is overlaid
    on the new candidate (same tree hash) and confirmed."""
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = GraphFixer()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(
            maven=FakeMaven(outcomes=[True, False, True],
                            on_package=lambda p: (_ for _ in ()).throw(Killed()) if (p / APP).read_text() == FIXED
                            else None), fixer=fx), rec=rec)
    (paths.round_dir(1) / graph.CANDIDATE_READY).unlink()
    out, _ = _resume(rec.saves, paths, FakeMaven(outcomes=[True]), fx)
    assert out.status == SUCCEEDED and len(fx.calls) == 1 and out.rounds[0].fix.fixed
    assert zip_files(paths.delivery_zip)[APP] == FIXED.encode()


def test_review5_a_recorded_loop_that_did_not_fix_is_not_run_again(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = GraphFixer(fixed=False, reason="not fixed: 2 error(s) left")
    real = graph._candidate_failed
    calls = {"n": 0}

    def failed(h, r, outcome):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Killed()                    # after the loop's result was saved
        return real(h, r, outcome)
    monkeypatch.setattr(graph, "_candidate_failed", failed)
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx), rec=rec)
    out, _ = _resume(rec.saves, paths, FakeMaven(outcomes=[False]), fx)
    assert len(fx.calls) == 1
    assert out.status == FAILED and out.message.endswith(" | automatic build fix: not fixed: 2 error(s) left")


# ------------------------------------------------------------------------------------------------ nits
def test_nit_a_flaky_first_build_is_retry_passed_and_committed_without_a_fix(tmp_path):
    state, paths = make_run(tmp_path)
    fx = GraphFixer(retry=True)
    maven = FakeMaven(outcomes=[True, False, True])
    out, _ = run(state, paths, one_fix_components(maven=maven, fixer=fx))
    rs = out.rounds[0]
    assert out.status == SUCCEEDED and rs.committed
    assert rs.fix.fixed and rs.fix.retry_passed and rs.fix.changed == [] and rs.fix.patch == ""
    assert rs.build.log == "rounds/1/build-fixed.log" and rs.fix_first_build is not None
    assert (paths.accepted / APP).read_text(encoding="utf-8") == "class App {}\n"
    assert out.delivery.build_ok


def test_nit_a_retry_passed_whose_confirming_build_fails_is_not_fixed(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, False]),
                                                  fixer=GraphFixer(retry=True)))
    rs = out.rounds[0]
    assert out.status == FAILED and not rs.fix.fixed and not rs.fix.retry_passed
    assert out.message.endswith(" | automatic build fix: error: confirming build failed")


def test_nit_a_pending_stop_records_a_loop_not_started_and_a_resume_may_start_it(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = GraphFixer()
    n = {"calls": 0}

    def on_package(project):
        n["calls"] += 1
        if n["calls"] == 2:
            rec.stop_flag = True
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False], on_package=on_package),
                                                  fixer=fx), rec=rec)
    assert fx.calls == [] and out.status == "STOPPED"
    assert not out.rounds[0].fix.attempted and out.rounds[0].fix.reason == "stop requested"
    out2, _ = _resume(rec.saves, paths, FakeMaven(outcomes=[False, True]), fx)
    assert len(fx.calls) == 1 and out2.status == SUCCEEDED and out2.rounds[0].fix.fixed


@pytest.mark.parametrize("outcomes, fixer", [([True, False, True], "fixed"), ([True, True], None),
                                             ([True, False], None)])
def test_nit_files_a_build_removed_are_restored_before_freezing(tmp_path, outcomes, fixer):
    files = {"pom.xml": POM, APP: "class App {}\n", "README.txt": "hello\n"}
    state, paths = make_run(tmp_path, files)

    def on_package(project):
        if project != paths.root / "baseline" / "tree" and (project / "README.txt").exists():
            (project / "README.txt").unlink()             # a build step that deletes an uploaded file
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=outcomes, on_package=on_package),
                                                  fixer=GraphFixer() if fixer else None), rec=rec)
    assert out.reason != "internal_error", out.message
    assert zip_files(paths.delivery_zip)["README.txt"] == b"hello\n"
    restored = [e for e in rec.events if e.get("event") == "manifest_files_restored"]
    assert restored and restored[0]["paths"] == ["README.txt"] and restored[0]["count"] == 1
    assert fix_events(rec) or fixer is None
