"""Regression tests for the second review of the remediation's fix loop (docs/INTERFACES.md "v2.1z-bk", "Graph and
fixer").

The review's reproductions (`out/scratch/review-bj/test_repro_bj.py`, which asserted the buggy behaviour) turned around
to assert the right outcome, plus the other points of the section:
1. a stop right before / during the confirming build keeps the fix; STOPPED at `build`; a resume confirms again;
2. wherever a fix is dropped the state is saved before `fixed/` and the patch are removed;
3. a fixer that raises leaves the completeness marker alone: the failed candidate is delivered;
4. files a build writes into manifest paths never reach a delivery (first build, confirming build, the build after a
   dropped override, a pom under another name, inside the loop's work copy);
5. a loop killed mid-way: its folder is kept, its LLM usage recovered and counted;
6. `fix.patch` of a source that is not UTF-8;
plus `_drop_ineffective` keeping a not-fixed record, `_overlay_fix` re-checking the paths, `tree_hash` saved with the
fix. No Maven, no LLM.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from sbmig.remediations import apply as apply_mod
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations import graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations import planner as P
from sbmig.remediations.fixer import fix_usage, interrupted_result, unified_patch
from sbmig.remediations.models import (
    FAILED,
    STOPPED,
    SUCCEEDED,
    FixResult,
    Inventory,
    RemediationState,
    RoundPlan,
    RoundState,
)
from tests.loop.fakes import FakeBuildRunner, FakeScriptRunner, write_script
from tests.loop.fakes import FakeFixer as LoopFixer

from . import test_fixer as tf
from . import test_module_overrides as MO
from .fakes import (
    POM,
    FakeMaven,
    FakeScanner,
    FakeVersions,
    Recorder,
    ScriptedInventory,
    ScriptedPlanner,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)
from .test_fixer import BAR, FOO, MANIFEST, make_tree, snapshot
from .test_fixer import run as run_fixer
from .test_graph import one_fix_components, run
from .test_graph_fix_loop import APP, FIXED, FakeFixer, zip_files

rcfg = tf.rcfg

ORIGINAL_APP = b"class App {}\n"


class Killed(BaseException):
    """The pod dies (not an Exception: nothing in the graph catches it)."""


def _saved(rec: Recorder) -> RemediationState:
    return RemediationState.from_dict(rec.saves[-1])


def _resume(rec, paths, maven, fx, **kw):
    return run(_saved(rec), paths, one_fix_components(maven=maven, fixer=fx, **kw), rec=Recorder())


# ------------------------------------------------------------------------------------------------ 1. stop
def test_review1_a_stop_during_the_confirming_build_keeps_the_fix_and_a_resume_confirms_it(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = FakeFixer()
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 3:                      # baseline, first candidate build, confirming build
            rec.stop_flag = True
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, False], on_package=on_package),
                                                  fixer=fx), rec=rec)
    assert out.status == STOPPED and out.next_node == "build", out.message
    rs = out.rounds[0]
    assert rs.fix.fixed and rs.fix.changed == [APP] and rs.fix.reason == ""        # nothing discarded
    assert (paths.round_dir(1) / "fixloop" / "fixed" / APP).is_file() and (paths.round_dir(1) / "fix.patch").is_file()
    assert (paths.candidate(1) / APP).read_text(encoding="utf-8") == FIXED          # the overlay stays
    assert "confirming build" in out.message
    # resume: the overlay is made again (idempotent) and confirmed; the loop is not run again
    fx2 = FakeFixer()
    maven = FakeMaven(outcomes=[True])
    out2, _ = _resume(rec, paths, maven, fx2)
    assert out2.status == SUCCEEDED, out2.message
    assert fx2.calls == [] and maven.package_calls == [paths.candidate(1)]
    assert out2.rounds[0].fix.fixed and out2.rounds[0].build.log == "rounds/1/build-fixed.log"
    assert zip_files(paths.delivery_zip)[APP] == FIXED.encode()


def test_review1_a_stop_right_before_the_confirming_build_halts_without_building(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()

    class StopAfterFix(FakeFixer):
        def run(self, *a, **kw):
            res = super().run(*a, **kw)
            rec.stop_flag = True            # the stop arrives while the loop finishes
            return res
    maven = FakeMaven(outcomes=[True, False])
    out, _ = run(state, paths, one_fix_components(maven=maven, fixer=StopAfterFix()), rec=rec)
    assert out.status == STOPPED and out.next_node == "build"
    assert len(maven.package_calls) == 2                                            # no confirming build
    assert out.rounds[0].fix.fixed and out.rounds[0].fix.tree_hash
    out2, _ = _resume(rec, paths, FakeMaven(outcomes=[True]), FakeFixer())
    assert out2.status == SUCCEEDED and zip_files(paths.delivery_zip)[APP] == FIXED.encode()


# ------------------------------------------------------------------------------------------------ 2. save, then delete
def test_review2_a_kill_after_the_failed_confirm_does_not_run_the_loop_again(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    seen = []
    real_drop = graph._drop_fix_files

    def drop_then_die(h, fix):
        seen.append(json.loads(json.dumps(rec.saves[-1]["rounds"][0]["fix"])))
        seen.append((paths.candidate(1) / APP).read_bytes())
        real_drop(h, fix)
        raise Killed()
    monkeypatch.setattr(graph, "_drop_fix_files", drop_then_die)
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, False]), fixer=FakeFixer()), rec=rec)
    monkeypatch.setattr(graph, "_drop_fix_files", real_drop)
    saved_fix, cand_app = seen
    # the state said "not fixed" before the files went, and the overlay was already undone
    assert saved_fix["fixed"] is False and saved_fix["reason"] == "error: confirming build failed"
    assert saved_fix["discarded_changed"] == [APP] and "fixed_dir" not in saved_fix
    assert cand_app == ORIGINAL_APP
    fx2 = FakeFixer(fixed=False, reason="not fixed: 1 error(s) left")
    out2, _ = _resume(rec, paths, FakeMaven(outcomes=[False]), fx2)
    assert fx2.calls == []                                                           # not run under a false reason
    assert out2.rounds[0].fix_discarded == []
    assert out2.status == FAILED and out2.message.endswith(" | automatic build fix: error: confirming build failed")
    assert zip_files(paths.delivery_zip)[APP] == ORIGINAL_APP


def test_review2_a_kill_before_the_files_are_removed_leaves_a_consistent_state(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    rec = Recorder()

    def die(h, fix):
        raise Killed()
    monkeypatch.setattr(graph, "_drop_fix_files", die)
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, False]), fixer=FakeFixer()), rec=rec)
    monkeypatch.undo()
    assert (paths.round_dir(1) / "fixloop" / "fixed" / APP).exists()               # left behind, harmless
    fx2 = FakeFixer()
    out2, _ = _resume(rec, paths, FakeMaven(outcomes=[False]), fx2)
    assert fx2.calls == [] and out2.status == FAILED
    assert zip_files(paths.delivery_zip)[APP] == ORIGINAL_APP


# ------------------------------------------------------------------------------------------------ 3. a raising fixer
def test_review3_a_raising_fixer_delivers_the_untouched_failed_candidate(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]),
                                                  fixer=FakeFixer(raises=RuntimeError("bug"))))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is not None and not out.delivery.build_ok
    assert "delivery/result.zip contains the round 1 change set" in out.message
    assert out.message.endswith(" | automatic build fix: error: RuntimeError: bug")
    files = zip_files(paths.delivery_zip)
    assert files[APP] == ORIGINAL_APP and b"<version>1.1</version>" in files["pom.xml"]


# ------------------------------------------------------------------------------------------------ 4. build-side changes
def _rewrite(project: Path, rel: str, extra: bytes) -> None:
    p = project / rel
    p.write_bytes(p.read_bytes() + extra)


def test_review4_a_build_that_rewrites_a_source_and_the_pom_reaches_no_delivery(tmp_path):
    state, paths = make_run(tmp_path)
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:                                   # the passing candidate build (formatter, sortpom)
            _rewrite(project, APP, b"// reformatted by the build\n")
            _rewrite(project, "pom.xml", b"<!-- sorted -->\n")
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True], on_package=on_package)),
                 rec=rec)
    assert out.status == SUCCEEDED, out.message
    files = zip_files(paths.delivery_zip)
    assert files[APP] == ORIGINAL_APP
    assert b"<version>1.1</version>" in files["pom.xml"] and b"sorted" not in files["pom.xml"]   # applied, not accepted
    assert (paths.accepted / APP).read_bytes() == ORIGINAL_APP
    rs = out.rounds[0]
    assert rs.build_changed == ["pom.xml", APP]
    [ev] = [e for e in rec.events if e.get("event") == "build_side_changes"]
    assert ev["round"] == 1 and ev["missing"] == 0 and ev["changed"] == 2 and ev["paths"] == ["pom.xml", APP]
    assert rec.saves[-1]["rounds"][0]["build_changed"] == ["pom.xml", APP]
    assert (paths.round_dir(1) / "applied" / "pom.xml").read_bytes() == (paths.accepted / "pom.xml").read_bytes()


def test_review4_a_failing_build_that_rewrites_a_source_delivers_the_failed_candidate_without_it(tmp_path):
    state, paths = make_run(tmp_path)
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:
            _rewrite(project, APP, b"// build edit\n")
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False], on_package=on_package)))
    assert out.status == FAILED and out.delivery is not None
    assert zip_files(paths.delivery_zip)[APP] == ORIGINAL_APP
    assert out.rounds[0].build_changed == [APP]


def test_review4_the_confirming_build_keeps_the_reported_fix_and_drops_its_own_edits(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM, APP: "class App {}\n", "README.txt": "hello\n"})
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 3:                                   # the confirming build
            _rewrite(project, APP, b"// reformatted\n")
            _rewrite(project, "pom.xml", b"<!-- sorted -->\n")
            (project / "README.txt").unlink()
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True], on_package=on_package),
                                                  fixer=FakeFixer()), rec=rec)
    assert out.status == SUCCEEDED, out.message
    files = zip_files(paths.delivery_zip)
    assert files[APP] == FIXED.encode() and files["README.txt"] == b"hello\n"
    assert b"<version>1.1</version>" in files["pom.xml"] and b"sorted" not in files["pom.xml"]
    assert out.rounds[0].build_changed == ["README.txt", "pom.xml", APP]
    ev = [e for e in rec.events if e.get("event") == "build_side_changes"][-1]
    assert ev["missing"] == 1 and ev["changed"] == 2 and ev["missing_paths"] == ["README.txt"]


ALT = "build-parent.xml"
ALT_POM = POM.replace("<artifactId>app</artifactId>", "<artifactId>parent</artifactId>")


def test_review4_a_pom_under_another_name_is_stored_in_applied_and_kept_as_applied(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM.replace("<version>1.0</version>", "<version>9</version>"),
                                       ALT: ALT_POM, APP: "class App {}\n"})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(pom=ALT)])])
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:
            (project / ALT).write_bytes((project / ALT).read_bytes().replace(b"1.1", b"1.0"))   # the build undoes it
    out, _ = run(state, paths, one_fix_components(plan=plan, maven=FakeMaven(outcomes=[True, True], on_package=on_package)))
    assert out.status == SUCCEEDED, out.message
    stored = paths.round_dir(1) / "applied" / ALT
    assert stored.is_file() and b"<version>1.1</version>" in stored.read_bytes()
    assert sorted(p.name for p in (paths.round_dir(1) / "applied").iterdir()) == [ALT]
    assert b"<version>1.1</version>" in zip_files(paths.delivery_zip)[ALT]
    assert out.rounds[0].build_changed == [ALT]


def _drop_run(tmp_path, outcomes, fixer, *, models=None, rec=None, state_paths=None):
    files = {**MO.company_files(back_body=MO.DM_SECTION), "app-back/src/main/java/Back.java": "class Back { broken }\n"}
    state, paths = state_paths or make_run(tmp_path, files)
    models = models if models is not None else [MO._three(), MO._three(boot="1.0", back="1.1", front="1.1"),
                                                MO._three(boot="1.0", back="1.1", front="1.1")]
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(MO._finding())],
                                              "final-r1": [scan_result(MO._finding())]}),
                            inventory=ScriptedInventory([Inventory(items=[MO.item(modules=list(MO.THREE))])]),
                            maven=FakeMaven(models=models, outcomes=outcomes),
                            versions=FakeVersions({"example:t": ["1.0", "1.1"]}),
                            plan=P.plan_round, apply=apply_mod, fixer=fixer)
    rec = rec or Recorder()
    out = graph.run_remediation(state, paths, make_cfg(max_rounds=1), comps, save=rec.save, journal=rec.journal,
                                stop=rec.stop)
    return out, paths, rec


def test_review4_a_remade_candidate_keeps_no_applied_file_of_a_dropped_proposal(tmp_path):
    out, paths, _ = _drop_run(tmp_path, [True, True, True], None)
    assert out.rounds[0].committed, out.message
    applied = paths.round_dir(1) / "applied"
    stored = sorted(p.relative_to(applied).as_posix() for p in applied.rglob("*") if p.is_file())
    assert "app-boot/pom.xml" not in stored and stored                                # the dropped entry's pom
    for rel in stored:
        assert (applied / rel).read_bytes() == (paths.accepted / rel).read_bytes()
    assert b"<artifactId>t</artifactId>" not in (paths.accepted / "app-boot/pom.xml").read_bytes()


def test_review4_inside_the_loop_build_edits_are_neither_blamed_nor_credited(rcfg, tmp_path):
    tree = make_tree(tmp_path)
    before = snapshot(tree)

    def build(ws, call):
        # a formatter and sortpom bound to the lifecycle: every build rewrites Bar.java and the pom
        for rel, extra in ((BAR, "// fmt\n"), ("pom.xml", "<!-- sorted -->\n")):
            (ws.root / rel).write_text((ws.root / rel).read_text() + extra)
        (ws.root / "gen").mkdir(exist_ok=True)
        (ws.root / "gen" / "Out.txt").write_text("generated\n")       # a file the build creates outside target/
        return tf.broken_build(ws, call)
    res = run_fixer(rcfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})], build=build)
    assert res.fixed and res.reason == "" and res.changed == [FOO], res.reason
    assert snapshot(Path(res.fixed_dir)) == {FOO: b"class Foo { ok }\n"}
    text = Path(res.patch).read_text(encoding="utf-8")
    assert BAR not in text and "pom.xml" not in text
    assert snapshot(tree) == before


def test_review4_a_file_an_attempt_created_under_an_ignored_path_is_rejected_at_the_end(rcfg, tmp_path):
    tree = make_tree(tmp_path, b"*.tmp\n")

    def script_fn(ws, code):
        from sbmig.core.models import ScriptRunResult
        (ws.root / FOO).write_text("class Foo { ok }\n")
        (ws.root / "notes.tmp").write_text("scratch\n")            # not reported, hidden by the project's .gitignore
        return ScriptRunResult(status="ok", changed_files=[FOO])
    res = fixer_mod.run_fix_loop(rcfg, tree, tree.parent / "fixloop", run_id="r", targets=[], applied=[tf.BUMP],
                                 manifest_paths=[*MANIFEST, ".gitignore"], first_outcome=tf.FIRST, stop=lambda: False,
                                 deadline=None, build_runner=lambda ws: FakeBuildRunner(ws, fn=tf.broken_build),
                                 llm_fixer=LoopFixer(scripts=[write_script({FOO: "x"})]),
                                 script_runner=FakeScriptRunner(fn=script_fn), profile=tf.make_profile())
    assert not res.fixed and res.reason == "rejected: notes.tmp must not be created"
    assert not (tree / "notes.tmp").exists()


# ------------------------------------------------------------------------------------------------ 5. a killed loop
def _llm_event(model, calls, inp, out):
    return json.dumps({"event": "llm_script", "model": model, "calls": calls, "tokens": {"input": inp, "output": out}})


class KilledFixer:
    """Writes two answered LLM calls into the loop's own journal, then the pod dies."""

    def run(self, cfg, tree, work_dir, **kw):
        (Path(work_dir) / "journal.jsonl").write_text(
            "\n".join([json.dumps({"event": "remediation_fix_loop_start"}), _llm_event("build_error_fixer", 1, 100, 20),
                       _llm_event("build_error_fixer", 1, 50, 10)]) + "\n", encoding="utf-8")
        raise Killed()


def test_review5_a_killed_loop_is_kept_and_its_usage_counted_before_the_new_loop(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=KilledFixer()), rec=rec)
    assert "fix" not in rec.saves[-1]["rounds"][0]                                    # nothing was recorded
    rec2 = Recorder()

    class SeeingFixer(FakeFixer):
        def run(self, *a, **kw):
            self.saved_then = json.loads(json.dumps(rec2.saves[-1]["rounds"][0]))
            return super().run(*a, **kw)
    fx = SeeingFixer()
    out, _ = run(_saved(rec), paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx), rec=rec2)
    assert out.status == SUCCEEDED, out.message
    rs = out.rounds[0]
    [gone] = rs.fix_discarded
    assert gone.attempted and not gone.fixed and gone.reason == "interrupted: the run ended during the loop"
    assert gone.llm_calls == 2 and gone.input_tokens == 150 and gone.output_tokens == 30
    assert gone.models == ["build_error_fixer"] and gone.usage[0].calls == 2
    # saved before the new loop started, and the folder kept under its new name
    assert fx.saved_then["fix_discarded"][0]["reason"] == "interrupted: the run ended during the loop"
    kept = paths.round_dir(1) / "fixloop-interrupted-1"
    assert (kept / "journal.jsonl").is_file() and (kept / "loop.json").is_file()
    assert (paths.round_dir(1) / "fixloop" / "loop.json").is_file()
    assert fix_usage(out)["calls"] == 3 and fix_usage(rec2.saves[-1])["calls"] == 3
    assert next(e for e in rec2.events if e.get("event") == "remediation_fix_interrupted")["llm_calls"] == 2


def test_review5_a_killed_loop_whose_journal_cannot_be_read_counts_zero_usage_unknown(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()

    class UnreadableFixer:
        def run(self, cfg, tree, work_dir, **kw):
            (Path(work_dir) / "journal.jsonl").mkdir()               # not a readable file
            raise Killed()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=UnreadableFixer()), rec=rec)
    out, _ = _resume(rec, paths, FakeMaven(outcomes=[False]), FakeFixer(fixed=False, reason="not fixed: 1 error(s) left"))
    [gone] = out.rounds[0].fix_discarded
    assert gone.reason == "interrupted: the run ended during the loop (usage unknown)"
    assert gone.llm_calls == 0 and gone.usage == []
    assert fix_usage(out)["calls"] == 2                                               # the new loop's calls


def test_review5_a_recorded_loops_folder_is_not_taken_for_a_killed_one(tmp_path):
    """The second loop after a dropped override finds the first loop's folder: it was recorded, so nothing is added."""
    from .test_fix_loop_review import SrcFixer
    out, paths, rec = _drop_run(tmp_path, [True, False, True, False, True], SrcFixer())
    rs = out.rounds[0]
    assert [f.reason for f in rs.fix_discarded] == ["discarded: the candidate was remade"]
    assert not (paths.round_dir(1) / "fixloop-interrupted-1").exists()
    assert not [e for e in rec.events if e.get("event") == "remediation_fix_interrupted"]


def test_interrupted_result_without_a_journal_is_zero_and_known(tmp_path):
    res = interrupted_result(tmp_path)
    assert res.attempted and not res.fixed and res.reason == "interrupted: the run ended during the loop"
    assert res.llm_calls == 0 and res.usage == []


def test_interrupted_result_prices_the_recovered_usage(tmp_path):
    from types import SimpleNamespace
    (tmp_path / "journal.jsonl").write_text(_llm_event("m-priced", 2, 1_000_000, 0) + "\nnot json\n", encoding="utf-8")
    cfg = SimpleNamespace(llm=SimpleNamespace(model="m-priced", pricing={"m-priced": {"input_per_mtok": 3.0,
                                                                                       "output_per_mtok": 15.0}},
                                              pricing_currency="USD", pricing_note=""))
    res = interrupted_result(tmp_path, cfg)
    assert res.llm_calls == 2 and res.input_tokens == 1_000_000 and res.models == ["m-priced"]
    assert res.usage[0].cost == pytest.approx(3.0) and res.currency


# ------------------------------------------------------------------------------------------------ 6. patch encoding
def test_review6_a_cp949_source_is_shown_as_utf8_in_the_patch(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    old = "// 사용자 목록\nclass Foo { broken }\n".encode("cp949")
    new = "// 사용자 목록\nclass Foo { ok }\n".encode("cp949")
    for root, data in ((a, old), (b, new)):
        (root / "src").mkdir(parents=True)
        (root / "src/Foo.java").write_bytes(data)
    (a / "bin.dat").write_bytes(b"\xff\xfe\x00\x81\xff\x80")
    (b / "bin.dat").write_bytes(b"\xff\xfe\x00\x82\xff\x80")
    text = unified_patch(a, b, ["src/Foo.java", "bin.dat"])
    assert text.startswith("# src/Foo.java: shown as UTF-8, the file is MS949\n--- a/src/Foo.java\n")
    assert " // 사용자 목록\n" in text and "-class Foo { broken }\n" in text and "+class Foo { ok }\n" in text
    assert "--- a/bin.dat\n+++ b/bin.dat\nBinary files differ\n" in text
    assert unified_patch(a, b, ["src/Foo.java"]).count("Binary") == 0


def test_review6_a_utf8_source_gets_no_encoding_line(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root, data in ((a, "// 한글\nx\n"), (b, "// 한글\ny\n")):
        root.mkdir()
        (root / "F.java").write_text(data, encoding="utf-8")
    text = unified_patch(a, b, ["F.java"])
    assert text.startswith("--- a/F.java\n") and "shown as UTF-8" not in text


# ------------------------------------------------------------------------------------------------ low items
class NotFixer:
    def __init__(self):
        self.calls = 0

    def run(self, cfg, tree, work_dir, **kw):
        self.calls += 1
        return FixResult(attempted=True, iterations=2, llm_calls=2, reason="not fixed: 1 error(s) left")


def test_low7_drop_ineffective_keeps_a_recorded_not_fixed_result(tmp_path, monkeypatch):
    rec = Recorder()
    real = graph._candidate_failed
    calls = {"n": 0}

    def failed(h, r, outcome):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Killed()                                  # after the loop's not-fixed result was saved
        return real(h, r, outcome)
    monkeypatch.setattr(graph, "_candidate_failed", failed)
    fx = NotFixer()
    files = {**MO.company_files(back_body=MO.DM_SECTION), "app-back/src/main/java/Back.java": "class Back {}\n"}
    sp = make_run(tmp_path, files)
    with pytest.raises(Killed):
        _drop_run(tmp_path, [True, False], fx, models=[MO._three()], rec=rec, state_paths=sp)
    # resume: the build passes now, one override entry was ineffective and is dropped; the record stays
    after = [MO._three(boot="1.0", back="1.1", front="1.1"), MO._three(boot="1.0", back="1.1", front="1.1")]
    out, _, _ = _drop_run(tmp_path, [True, True], fx, models=after,
                              state_paths=(_saved(rec), sp[1]))
    rs = out.rounds[0]
    assert rs.committed, out.message
    assert rs.fix is not None and not rs.fix.fixed and rs.fix.reason == "not fixed: 1 error(s) left"
    assert rs.fix_discarded == [] and fx.calls == 1
    assert fix_usage(out)["calls"] == 2


def _killed_in_confirm(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 3:
            raise Killed()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True], on_package=on_package),
                                             fixer=FakeFixer()), rec=rec)
    return paths, rec


@pytest.mark.parametrize("tamper", ["pom", "not_listed", "hash"])
def test_low8_overlay_rechecks_the_stored_paths_and_discards_an_unusable_fix(tmp_path, monkeypatch, tamper):
    paths, rec = _killed_in_confirm(tmp_path)
    data = rec.saves[-1]
    fix = data["rounds"][0]["fix"]
    fixed_dir = paths.root / fix["fixed_dir"]
    if tamper == "pom":
        (fixed_dir / "pom.xml").write_text("<project>evil</project>\n")
        fix["changed"] = ["pom.xml", APP]
    elif tamper == "not_listed":
        (fixed_dir / "src/main/java/New.java").write_text("class New {}\n")
        fix["changed"] = [APP, "src/main/java/New.java"]
    else:
        fix["tree_hash"] = "0" * 64
    order = []
    real_drop = graph._drop_fix_files
    monkeypatch.setattr(graph, "_drop_fix_files",
                        lambda h, f: (order.append(rec2.saves[-1]["rounds"][0].get("fix")), real_drop(h, f)))
    rec2 = Recorder()
    fx = FakeFixer()
    out, _ = run(RemediationState.from_dict(data), paths,
                 one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx), rec=rec2)
    rs = out.rounds[0]
    assert [f.reason for f in rs.fix_discarded] == ["discarded: the stored fix is not usable"]
    assert order and order[0] is None                                                 # saved before the delete
    assert len(fx.calls) == 1 and fx.calls[0]["app"] == ORIGINAL_APP                  # the candidate was put back
    assert out.status == SUCCEEDED, out.message
    files = zip_files(paths.delivery_zip)
    assert b"evil" not in files["pom.xml"] and "src/main/java/New.java" not in files


def test_low9_the_tree_hash_is_saved_with_the_fix(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=FakeFixer()),
                 rec=rec)
    first = next(s["rounds"][0]["fix"] for s in rec.saves if s["rounds"] and "fix" in s["rounds"][0])
    assert first["fixed"] is True and first["tree_hash"]
    manifest = intake_mod.manifest_paths(paths.manifest)
    # the hash of the candidate as it is after the overlay, by the rule of intake.tree_hash
    assert first["tree_hash"] == intake_mod.tree_hash(paths.candidate(1), manifest) == out.rounds[0].fix.tree_hash
    assert first["loop_id"].startswith("rem-1-fix-r1-")


def test_old_round_json_reads_back_without_the_new_fields():
    rs = RoundState.from_dict({"round": 1, "fix": {"attempted": True, "fixed": False, "reason": "x"}})
    assert rs.build_changed == [] and rs.fix.loop_id == ""
    d = rs.to_dict()
    assert "build_changed" not in d and "loop_id" not in d["fix"]
    rs.build_changed = ["pom.xml"]
    assert RoundState.from_dict(rs.to_dict()).build_changed == ["pom.xml"]


def test_the_delivery_zip_matches_the_expected_files_for_a_zip_with_one_top_folder(tmp_path):
    state, paths = make_run(tmp_path, {"app/pom.xml": POM, "app/" + APP: "class App {}\n", "README.txt": "hello\n"})
    state.intake.project_root = "app"
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 3:
            _rewrite(project, APP, b"// fmt\n")
            _rewrite(project, "pom.xml", b"<!-- sorted -->\n")
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True], on_package=on_package),
                                                  fixer=FakeFixer()))
    assert out.status == SUCCEEDED, out.message
    with zipfile.ZipFile(paths.delivery_zip) as zf:
        assert zf.read("app/" + APP) == FIXED.encode()
        assert b"<version>1.1</version>" in zf.read("app/pom.xml") and b"sorted" not in zf.read("app/pom.xml")
    assert out.rounds[0].build_changed == ["app/pom.xml", "app/" + APP]
    assert (paths.round_dir(1) / "applied" / "app" / "pom.xml").is_file()



def test_low8_a_fresh_fix_that_fails_the_check_ends_not_fixed_without_a_second_loop(tmp_path):
    class PomFixer(FakeFixer):
        def run(self, cfg, tree, work_dir, **kw):
            res = super().run(cfg, tree, work_dir, **kw)
            (Path(work_dir) / "fixed" / "pom.xml").write_text("<project>evil</project>\n")
            res.changed = ["pom.xml", APP]
            return res
    state, paths = make_run(tmp_path)
    fx = PomFixer()
    maven = FakeMaven(outcomes=[True, False])
    out, _ = run(state, paths, one_fix_components(maven=maven, fixer=fx))
    assert len(fx.calls) == 1 and len(maven.package_calls) == 2
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.message.endswith(" | automatic build fix: discarded: the stored fix is not usable")
    assert [f.reason for f in out.rounds[0].fix_discarded] == ["discarded: the stored fix is not usable"]
    files = zip_files(paths.delivery_zip)
    assert b"evil" not in files["pom.xml"] and files[APP] == ORIGINAL_APP
