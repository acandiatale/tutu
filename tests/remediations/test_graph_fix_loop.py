"""v2.1z-bi part B in the remediation graph (docs/INTERFACES.md "v2.1z-bi", "When" and "Graph", amended by "v2.1z-bj").

A fake `Fixer` goes in through `Components.fixer` (no Maven, no LLM). Checked: the conditions under which the loop
is not called (switch off, no fixer, baseline failed, nothing applied, a stop pending), the fixed path (confirming
build → verify → commit → delivery `build_ok` with the changed source), a failing confirming build (reset +
`candidate_build_failed`), the not-fixed path (today's outcome + the message suffix), the journal events, the loop's
time (time left minus the reserve, the `fix_loop_max_minutes` cap, not started), a ZIP with one top-level folder
(paths relative to the project root). The fake never writes the candidate: fixed bytes go to `work_dir/fixed/` and the
graph overlays them. The review's scenarios are in `test_fix_loop_review.py`.
"""
from __future__ import annotations

import time
import zipfile
from pathlib import Path

from sbmig.remediations import graph
from sbmig.remediations.models import FAILED, STOPPED, SUCCEEDED, FixResult, RemediationState, RoundState

from .fakes import FakeApply, FakeMaven, Recorder, file_sha, make_cfg, make_components, make_run
from .test_graph import one_fix_components, run

APP = "src/main/java/App.java"
FIXED = "class App { fixed }\n"


class FakeFixer:
    """A `Fixer` under the v2.1z-bj contract: records each call; never writes the candidate; `fixed=True` stores the
    fixed App.java under `work_dir/fixed/` and writes the patch file; `retry=True` is fixed with nothing changed."""

    def __init__(self, fixed: bool = True, reason: str = "", raises: Exception | None = None, retry: bool = False,
                 llm_calls: int = 1):
        self.fixed, self.reason, self.raises, self.retry, self.llm_calls = fixed, reason, raises, retry, llm_calls
        self.calls: list[dict] = []

    def run(self, cfg, tree, work_dir, *, run_id, targets, applied, manifest_paths, first_outcome, stop, deadline,
            journal):
        self.calls.append({"tree": Path(tree), "work_dir": Path(work_dir), "run_id": run_id, "targets": list(targets),
                           "applied": [p.id for p in applied], "manifest_paths": list(manifest_paths),
                           "first_ok": first_outcome.ok, "deadline": deadline, "stop": stop(),
                           "app": (Path(tree) / APP).read_bytes() if (Path(tree) / APP).exists() else None})
        if self.raises is not None:
            raise self.raises
        if not self.fixed:
            return FixResult(attempted=True, iterations=3, attempts=2, llm_calls=2, reason=self.reason)
        if self.retry:
            return FixResult(attempted=True, fixed=True, retry_passed=True, iterations=1)
        fixed = Path(work_dir) / "fixed"
        (fixed / APP).parent.mkdir(parents=True, exist_ok=True)
        (fixed / APP).write_text(FIXED, encoding="utf-8")
        patch = Path(work_dir).parent / "fix.patch"
        patch.write_text(f"--- a/{APP}\n+++ b/{APP}\n", encoding="utf-8")
        return FixResult(attempted=True, fixed=True, iterations=2, attempts=1, llm_calls=self.llm_calls,
                         input_tokens=10, output_tokens=5, models=["build_error_fixer"], changed=[APP],
                         patch=str(patch), fixed_dir=str(fixed))


def zip_files(p: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(p) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def fix_events(rec: Recorder) -> list[dict]:
    return [e for e in rec.events if e.get("event") == "remediation_fix_loop"]


# ------------------------------------------------------------------------------------------------ when it is not called
def test_switch_off_does_not_call_the_fixer(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx),
                   cfg=make_cfg(fix_loop=False))
    assert fx.calls == [] and out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.rounds[0].fix is None and "automatic build fix" not in out.message and fix_events(rec) == []


def test_no_fixer_in_the_components_keeps_todays_path(tmp_path):
    state, paths = make_run(tmp_path)
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.rounds[0].fix is None and fix_events(rec) == []
    assert "fix" not in rec.saves[-1]["rounds"][0]


def test_a_failed_tolerated_baseline_does_not_call_the_fixer(tmp_path):
    state, paths = make_run(tmp_path)
    state.baseline_tolerated = True
    fx = FakeFixer()
    # the candidate fails with an error the baseline did not have: today's candidate_build_failed, no loop
    logs = [lambda p: "[ERROR] /x/src/main/java/App.java:[1,1] old error\n[INFO] BUILD FAILURE\n",
            lambda p: "[ERROR] /x/src/main/java/App.java:[2,1] new error\n[INFO] BUILD FAILURE\n"]
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[False, False], logs=logs), fixer=fx))
    assert fx.calls == [] and out.rounds[0].fix is None


def test_nothing_applied_does_not_call_the_fixer(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    out, rec = run(state, paths, one_fix_components(apply=FakeApply(skip={"p-1-1": "edit line did not match"}),
                                                  maven=FakeMaven(outcomes=[True, False]), fixer=fx))
    assert fx.calls == [] and out.rounds[0].fix is None and fix_events(rec) == []


def test_fix_wanted_needs_an_applied_proposal(tmp_path):
    state, paths = make_run(tmp_path)
    from sbmig.remediations.models import BuildOutcome
    state.baseline_build = BuildOutcome(ok=True)
    h = graph._Holder(state, paths, make_cfg(), make_components(fixer=FakeFixer()), lambda s: None, lambda e: None,
                      lambda: False)
    h.round_state(1)
    assert graph._fix_wanted(h, 1) is False
    h.round_state(1).applied = ["p-1-1"]
    assert graph._fix_wanted(h, 1) is True


def test_a_stop_during_the_candidate_build_does_not_call_the_fixer(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    fx = FakeFixer()
    calls = {"n": 0}

    def on_package(project):
        calls["n"] += 1
        if calls["n"] == 2:
            rec.stop_flag = True
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False], on_package=on_package),
                                                  fixer=fx), rec=rec)
    assert fx.calls == [] and out.status == STOPPED


# ------------------------------------------------------------------------------------------------ fixed
def test_fixed_round_is_confirmed_verified_committed_and_delivered(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    maven = FakeMaven(outcomes=[True, False, True])
    out, rec = run(state, paths, one_fix_components(maven=maven, fixer=fx))
    assert out.status == SUCCEEDED, out.message
    [call] = fx.calls
    assert call["tree"] == paths.candidate(1) and call["work_dir"] == paths.round_dir(1) / "fixloop"
    assert call["manifest_paths"] == ["pom.xml", APP] and call["targets"] == [] and call["applied"] == ["p-1-1"]
    assert call["run_id"] == "rem-1-fix-r1" and call["first_ok"] is False
    # no time budget: the cap fix_loop_max_minutes (30) alone
    assert call["deadline"] is not None and call["deadline"] - time.monotonic() < 30 * 60
    assert call["app"] != FIXED.encode()                       # the candidate was not touched by the loop
    rs = out.rounds[0]
    assert rs.committed and rs.fix.fixed and rs.fix.changed == [APP] and rs.fix.patch == "rounds/1/fix.patch"
    assert rs.fix.fixed_dir == "rounds/1/fixloop/fixed" and rs.fix.tree_hash and not rs.fix.retry_passed
    assert rs.fix_first_build is not None and not rs.fix_first_build.ok and rs.fix_first_build.log == "rounds/1/build.log"
    assert rs.build.ok and rs.build.log == "rounds/1/build-fixed.log"
    assert (paths.round_dir(1) / "build-fixed.log").is_file()
    assert len(maven.package_calls) == 3 and maven.package_calls[2] == paths.candidate(1)
    # accepted/ and the delivery carry the fixed source and the raised version
    assert (paths.accepted / APP).read_text(encoding="utf-8") == FIXED
    d = out.delivery
    assert d.build_ok and d.scanned
    files = zip_files(paths.delivery_zip)
    assert files[APP] == FIXED.encode() and b"<version>1.1</version>" in files["pom.xml"]
    assert file_sha(paths.delivery_zip) == d.sha256
    # journal: start, then end with the counts
    start, end = fix_events(rec)
    assert start == {"remediation": "rem-1", "event": "remediation_fix_loop", "round": 1, "phase": "start"}
    assert end["phase"] == "end" and end["fixed"] is True and end["iterations"] == 2 and end["llm_calls"] == 1
    assert end["changed"] == 1 and end["reason"] == ""
    # the saved state carries the result
    saved = rec.saves[-1]["rounds"][0]
    assert saved["fix"]["fixed"] is True and saved["fix"]["models"] == ["build_error_fixer"]
    assert saved["fix_first_build"]["ok"] is False
    assert RemediationState.from_dict(rec.saves[-1]).rounds[0].fix == rs.fix


def test_a_failing_confirming_build_resets_the_change_and_fails_as_today(tmp_path):
    state, paths = make_run(tmp_path)
    original = (paths.accepted / APP).read_bytes()
    fx = FakeFixer()
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, False]), fixer=fx))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    rs = out.rounds[0]
    assert not rs.fix.fixed and rs.fix.reason == "error: confirming build failed"
    assert rs.fix.changed == [] and rs.fix.patch == "" and not (paths.round_dir(1) / "fix.patch").exists()
    assert rs.fix.discarded_changed == [APP] and rs.fix.fixed_dir == ""
    assert not (paths.round_dir(1) / "fixloop" / "fixed").exists()
    assert rs.fix_first_build is None and rs.build.log == "rounds/1/build.log"
    assert (paths.candidate(1) / APP).read_bytes() == original
    # the failed candidate is delivered as today, without the reset source change
    files = zip_files(paths.delivery_zip)
    assert files[APP] == original and b"<version>1.1</version>" in files["pom.xml"]
    assert out.message.endswith(" | automatic build fix: error: confirming build failed")
    assert fix_events(rec)[-1]["reason"] == "error: confirming build failed"


# ------------------------------------------------------------------------------------------------ not fixed
def test_not_fixed_ends_as_today_with_the_reason_appended(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer(fixed=False, reason="not fixed: 2 error(s) left")
    out, rec = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx))
    assert len(fx.calls) == 1
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is not None and not out.delivery.build_ok          # the failed candidate, as today
    assert "not rescanned" in out.message
    assert out.message.endswith(" | automatic build fix: not fixed: 2 error(s) left")
    rs = out.rounds[0]
    assert rs.fix.attempted and not rs.fix.fixed and rs.fix.llm_calls == 2 and rs.fix_first_build is None
    end = fix_events(rec)[-1]
    assert end["fixed"] is False and end["reason"] == "not fixed: 2 error(s) left" and end["changed"] == 0


def test_not_fixed_under_discard_keeps_the_message_suffix(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer(fixed=False, reason="llm unavailable")
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx),
                 cfg=make_cfg(failed_candidate="discard"))
    assert out.status == FAILED and out.delivery is None
    assert out.message.endswith(" | automatic build fix: llm unavailable")


def test_a_fixer_that_raises_is_not_fixed_and_the_candidate_not_delivered(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer(raises=RuntimeError("boom"))
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.rounds[0].fix.reason == "error: RuntimeError: boom"
    assert "automatic build fix: error: RuntimeError: boom" in out.message
    assert out.delivery is None                       # the tree is in an unknown state: never delivered


# ------------------------------------------------------------------------------------------------ deadline / layout
def _timed_stop(left: float):
    def stop():
        return False
    stop.time_left = lambda: left
    return stop


def test_the_time_left_minus_the_reserve_becomes_the_deadline(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    before = time.monotonic()
    out = graph.run_remediation(state, paths, make_cfg(), one_fix_components(maven=FakeMaven(outcomes=[True, False, True]),
                                                                             fixer=fx),
                                save=lambda s: None, journal=lambda e: None, stop=_timed_stop(900.0))
    assert out.status == SUCCEEDED
    # reserve = max(300, 2 x builds of 0 s) = 300 → 600 s for the loop
    assert before + 590 < fx.calls[0]["deadline"] < time.monotonic() + 610


def test_the_reserve_grows_with_the_build_seconds_and_the_cap_bounds_the_loop(tmp_path):
    from sbmig.remediations.models import BuildOutcome
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    slow = BuildOutcome(ok=False, exit_code=1, summary="[ERROR] x", seconds=200.0)
    before = time.monotonic()
    graph.run_remediation(state, paths, make_cfg(), one_fix_components(maven=FakeMaven(outcomes=[True, slow, True]),
                                                                       fixer=fx),
                          save=lambda s: None, journal=lambda e: None, stop=_timed_stop(1000.0))
    assert before + 590 < fx.calls[0]["deadline"] < time.monotonic() + 610          # 1000 - 2 x 200
    state, paths = make_run(tmp_path / "b")
    fx2 = FakeFixer()
    before = time.monotonic()
    graph.run_remediation(state, paths, make_cfg(fix_loop_max_minutes=5),
                          one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=fx2),
                          save=lambda s: None, journal=lambda e: None, stop=_timed_stop(100000.0))
    assert before + 290 < fx2.calls[0]["deadline"] < time.monotonic() + 310         # the cap: 5 minutes


def test_too_little_time_after_the_reserve_does_not_start_the_loop(tmp_path):
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    rec = Recorder()
    out = graph.run_remediation(state, paths, make_cfg(), one_fix_components(maven=FakeMaven(outcomes=[True, False]),
                                                                             fixer=fx),
                                save=rec.save, journal=rec.journal, stop=_timed_stop(330.0))
    assert fx.calls == []
    assert out.status == FAILED and out.reason == "candidate_build_failed" and out.delivery is not None
    assert not out.rounds[0].fix.attempted and out.rounds[0].fix.reason == "time budget used up"
    assert out.message.endswith(" | automatic build fix not started: time budget used up")
    assert fix_events(rec)[-1]["phase"] == "not_started"


def test_a_zip_with_one_top_level_folder_passes_project_relative_paths(tmp_path):
    from .fakes import POM
    state, paths = make_run(tmp_path, {"app/pom.xml": POM, "app/src/main/java/App.java": "class App {}\n",
                                       "README.txt": "hello\n"})
    state.intake.project_root = "app"
    fx = FakeFixer()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False, True]), fixer=fx))
    assert out.status == SUCCEEDED, out.message
    call = fx.calls[0]
    assert call["tree"] == paths.candidate(1) / "app" and call["manifest_paths"] == ["pom.xml", APP]
    files = zip_files(paths.delivery_zip)
    assert files["app/" + APP] == FIXED.encode() and files["README.txt"] == b"hello\n"


def test_old_round_json_reads_back_without_fix():
    rs = RoundState.from_dict({"round": 1, "applied": ["p-1-1"], "committed": True})
    assert rs.fix is None and rs.fix_first_build is None
    assert "fix" not in rs.to_dict() and "fix_first_build" not in RemediationState(id="x", rounds=[rs]).to_dict()["rounds"][0]


def test_default_components_carry_the_real_fixer(monkeypatch):
    import sbmig.remediations.ivas as ivas_mod
    from sbmig.remediations.fixer import RemediationFixer

    class Settings:
        pass
    monkeypatch.setattr(ivas_mod, "ivas_settings", lambda cfg: Settings())
    monkeypatch.setattr(ivas_mod, "IvasHttpClient", lambda settings: object())
    import sbmig.remediations.maven as maven_mod
    import sbmig.remediations.planner as planner_mod
    monkeypatch.setattr(maven_mod, "ServerMaven", lambda cfg, journal=None: type("M", (), {"allowed_hosts": lambda s: []})())
    monkeypatch.setattr(planner_mod, "RepositoryVersionSource", lambda cfg, allowed_hosts=None: object())
    comps = graph.default_components(make_cfg())
    assert isinstance(comps.fixer, RemediationFixer)
