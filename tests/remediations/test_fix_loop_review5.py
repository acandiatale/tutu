"""Regression tests for the fifth review of the remediation and the second real-Maven smoke run (v2.1z-bo).

The review's reproductions (`out/scratch/review-bn/test_bn_a.py`, which asserted the faulty behaviour) turned around,
plus the review's other items and what the smoke run noticed:
1. a failure right after the result ZIP swap is rolled forward and said so; every entry repairs `delivery.json`;
2. no mid-node state save makes a re-entry of that node do what the uninterrupted run would not do;
3. a final state with its report pending whose owner is gone gets its report from the web service (Job modes too);
4. unknown LLM usage is never shown as an exact number;
5. the report and the state carry the same message after `chain._baseline_outcome`;
6. nits: a failing report noted once, `ScanSourceChanged` in the no-change copy, a leftover copy temp file, the kept
   copy of a tolerated run worded truthfully;
7. the fix section gives the round's total LLM calls;
8. a stop during the build fix loop is worded as such;
9. the round-0 to-do step says 「검사를 마친」;
11. an interrupted loop's work copy is removed once its usage is saved.
(Item 10, the loop's stop / time check before every LLM request, is tested in `tests/loop/test_fixloop_ask_halt.py`.)
No Maven, no LLM.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.core.config import ModelPrice
from sbmig.remediations import chain, graph, i18n, report
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations.fixer import _usage, fix_usage, interrupted_result
from sbmig.remediations.models import (
    FAILED,
    INCOMPLETE,
    REMEDIATIONS_DIR,
    STOPPED,
    SUCCEEDED,
    FixResult,
    FixUsage,
    RemediationState,
    RoundPlan,
    RoundState,
    RunPaths,
    read_json,
    sha256_file,
)

from .fakes import (
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)
from .test_baseline_tolerated import BASE_ERR, javac_log, unresolved_log
from .test_fix_loop_review2 import Killed
from .test_graph import POM2, _two_round_components, one_fix_components, run
from .test_graph_fix_loop import APP, FakeFixer


def _saved(rec: Recorder) -> RemediationState:
    return RemediationState.from_dict(rec.saves[-1])


def _marker(paths: RunPaths) -> Path:
    return paths.delivery_zip.parent / graph.NEXT_JSON


def _chained(tmp_path: Path) -> tuple[Path, RemediationState, RunPaths]:
    """A hand-made run folder moved into the chained layout `<run dir>/remediation/`."""
    run_dir = tmp_path / "mig-20261120-120000-abcdef"
    rpaths = chain.chain_paths(run_dir)
    state, paths = make_run(tmp_path / "x")
    rpaths.root.parent.mkdir(parents=True)
    os.replace(paths.root, rpaths.root)
    return run_dir, state, rpaths


def _disk_full_on_round_json(monkeypatch, paths: RunPaths, rnd: int):
    """`delivery.json` of round `rnd` cannot be written once (right after the swap)."""
    real = graph.write_json_atomic
    n = {"k": 0}

    def spy(path, data):
        if Path(path) == paths.delivery_json and data.get("round") == rnd and not data.get("scanned"):
            n["k"] += 1
            if n["k"] == 1:
                raise OSError(28, "No space left on device")
        return real(path, data)
    monkeypatch.setattr(graph, "write_json_atomic", spy)


# ------------------------------------------------------------------------------------------------ 1. after the swap
def test_disk_full_writing_delivery_json_after_the_swap_rolls_forward_and_says_so(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components(fail_round_two=False)
    _disk_full_on_round_json(monkeypatch, paths, 2)
    out, rec = run(state, paths, comps)
    monkeypatch.undo()
    assert out.status == FAILED and out.reason == "internal_error"
    assert "delivery/result.zip is now the result ZIP of round 2" in out.message
    assert "delivery/delivery.json could not be written (OSError" in out.message
    # the state describes the new ZIP; the marker stays for the repair; delivery.json is still round 1's
    assert out.delivery.round == 2 and out.delivery.sha256 == sha256_file(paths.delivery_zip)
    assert _saved(rec).delivery.round == 2
    assert _marker(paths).exists() and read_json(paths.delivery_json)["round"] == 1
    assert any(e.get("event") == "delivery_record_not_written" for e in rec.events)
    ko = i18n.ko_text(out.message)
    assert "결과 ZIP은 이제 라운드 2의 결과입니다." in ko and "delivery/delivery.json" in ko
    # the next entry (a final state) completes the record
    graph.run_remediation(_saved(rec), paths, make_cfg(), make_components(), save=Recorder().save,
                          journal=Recorder().journal, stop=lambda: False)
    assert read_json(paths.delivery_json)["round"] == 2 and not _marker(paths).exists()


def test_a_state_save_failing_after_the_swap_names_the_state_file_and_keeps_the_marker(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})

    class FullOnce(Recorder):
        failed = False

        def save(self, st):
            if st.delivery is not None and st.delivery.round == 2 and st.status == "RUNNING" and not self.failed:
                self.failed = True
                raise OSError(28, "No space left on device")
            super().save(st)
    rec = FullOnce()
    out = graph.run_remediation(state, paths, make_cfg(), _two_round_components(fail_round_two=False), save=rec.save,
                                journal=rec.journal, stop=rec.stop)
    assert out.status == FAILED and "state.json could not be written" in out.message
    assert "round 2" in out.message and _saved(rec).delivery.round == 2
    assert read_json(paths.delivery_json)["round"] == 2 and _marker(paths).exists()
    assert graph.delivery_swap_pending(paths)
    graph.recover_delivery(paths, _saved(rec))
    assert not _marker(paths).exists()


def test_chain_resume_of_a_final_state_repairs_delivery_json(tmp_path, monkeypatch):
    run_dir, state, rpaths = _chained(tmp_path)
    _disk_full_on_round_json(monkeypatch, rpaths, 1)
    rec = Recorder()
    out, _ = run(state, rpaths, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    monkeypatch.undo()
    assert out.status == FAILED and _marker(rpaths).exists()
    rpaths.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    back = chain.resume(run_dir, make_cfg(), journal=None, stop=None, components=make_components())
    assert back.status == FAILED and back.message == out.message
    assert read_json(rpaths.delivery_json)["round"] == 1 and not _marker(rpaths).exists()


# ------------------------------------------------------------------------------------------------ 2. mid-node saves
def _kill_after_the_swap_save(monkeypatch):
    """The pod dies right after `install_delivery` saved the state (before it removes the swap marker)."""
    real_unlink = graph._unlink_quiet
    n = {"k": 0}

    def die(p):
        if Path(p).name == graph.NEXT_JSON:
            n["k"] += 1
            if n["k"] == 2:                        # the first call clears a leftover marker before the swap
                raise Killed()
        return real_unlink(p)
    monkeypatch.setattr(graph, "_unlink_quiet", die)
    return real_unlink


def test_kill_after_a_no_finding_delivery_does_not_upload_the_baseline_again(tmp_path, monkeypatch):
    scan = FakeScanner({"baseline": [scan_result(), scan_result()]})
    state, paths = make_run(tmp_path)
    real = _kill_after_the_swap_save(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, make_components(scan=scan), rec=rec)
    monkeypatch.setattr(graph, "_unlink_quiet", real)
    st = _saved(rec)
    assert st.next_node == "baseline_scan" and not st.pending_upload and not st.pending_receipt
    st.status = "INTERRUPTED"
    out, rec2 = run(st, paths, make_components(scan=scan))
    assert scan.uploads == 1 and len(scan.calls) == 1
    assert out.status == SUCCEEDED and out.delivery.round == 0 and out.delivery.scanned
    assert any(e.get("event") == "baseline_scan_reused" for e in rec2.events)


def test_kill_after_the_no_change_swap_in_plan_resumes_like_the_uninterrupted_run(tmp_path, monkeypatch):
    def comps():
        return make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}),
                               plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                               proposals=[])]))
    s0, p0 = make_run(tmp_path / "ref")
    ref, _ = run(s0, p0, comps())
    state, paths = make_run(tmp_path / "k")
    real_unlink = _kill_after_the_swap_save(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, comps(), rec=rec)
    monkeypatch.setattr(graph, "_unlink_quiet", real_unlink)
    st = _saved(rec)
    assert st.next_node == "plan" and st.delivery is not None and st.final_scan == st.baseline_scan
    st.status = "INTERRUPTED"
    out, _ = run(st, paths, comps())
    assert (out.status, out.reason, out.reasons) == (ref.status, ref.reason, ref.reasons) == (
        INCOMPLETE, "residual_findings", ["residual_findings"])
    assert out.delivery.scanned


def test_kill_after_a_fresh_unusable_fix_is_discarded_does_not_run_the_loop_again(tmp_path, monkeypatch):
    class PomFixer(FakeFixer):
        def run(self, cfg, tree, work_dir, **kw):
            res = super().run(cfg, tree, work_dir, **kw)
            (Path(work_dir) / "fixed" / "pom.xml").write_text("<project>evil</project>\n")
            res.changed = ["pom.xml", APP]
            return res
    state, paths = make_run(tmp_path)
    fx = PomFixer()
    real_drop = graph._drop_fix_files
    monkeypatch.setattr(graph, "_drop_fix_files", lambda h, f: (_ for _ in ()).throw(Killed()))
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx), rec=rec)
    monkeypatch.setattr(graph, "_drop_fix_files", real_drop)
    st = _saved(rec)
    assert st.rounds[0].fix is None and st.rounds[0].fix_discarded[-1].reason == graph.R_FIX_UNUSABLE
    st.status = "INTERRUPTED"
    out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=fx))
    assert len(fx.calls) == 1                                          # the loop did not run a second time
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.message.endswith(" | automatic build fix: discarded: the stored fix is not usable")


def test_a_failed_folder_setup_stays_the_verdict_on_re_entry(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    real = graph.write_json_atomic
    monkeypatch.setattr(graph, "write_json_atomic", lambda path, data: (
        (_ for _ in ()).throw(OSError("read-only")) if Path(path).name == graph.LOOP_FILE else real(path, data)))

    class DieAfterNotStarted(Recorder):
        def journal(self, event):
            super().journal(event)
            if event.get("phase") == "not_started":
                raise Killed()
    rec = DieAfterNotStarted()
    fx = FakeFixer()
    with pytest.raises(Killed):
        graph.run_remediation(state, paths, make_cfg(), one_fix_components(maven=FakeMaven(outcomes=[True, False]),
                                                                           fixer=fx),
                              save=rec.save, journal=rec.journal, stop=rec.stop)
    monkeypatch.setattr(graph, "write_json_atomic", real)
    st = _saved(rec)
    assert st.rounds[0].fix is not None and not st.rounds[0].fix.attempted
    st.status = "INTERRUPTED"
    out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=fx))
    assert fx.calls == []                                              # neither entry ran the loop
    assert out.status == FAILED and "automatic build fix not started: error: OSError" in out.message


def test_a_loop_not_started_because_of_a_stop_may_start_on_resume(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 2:
            rec.stop_flag = True                   # the stop arrives during the candidate build
    fx = FakeFixer()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False], on_package=on_package),
                                                  fixer=fx), rec=rec)
    assert out.status == STOPPED and out.message.startswith("stop requested during the round 1 candidate build")
    assert out.rounds[0].fix is not None and out.rounds[0].fix.reason == graph.R_FIX_STOP and fx.calls == []
    st = RemediationState.from_dict(out.to_dict())
    out2, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx))
    assert len(fx.calls) == 1 and out2.status == SUCCEEDED


# ------------------------------------------------------------------------------------------------ 3. the web service
class _RunManager:
    """A stand-in for `RunManager`: the migration run's state word and whether a resume Job is active."""

    def __init__(self, state="finished", active=False):
        self.state, self.active = state, active

    def status(self, run_id):
        return {"state": self.state}

    def remediation_active(self, run_id):
        return self.active

    def remediation_job(self, run_id):
        return None

    def file_withheld(self, run_id, name):
        return False


def _kill_report_once(monkeypatch):
    real = report.write_report
    n = {"k": 0}

    def write(*a, **kw):
        n["k"] += 1
        if n["k"] == 1:
            raise Killed()
        return real(*a, **kw)
    monkeypatch.setattr(report, "write_report", write)


def _pending_chained(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_dir, state, rpaths = _chained(out_dir)
    state.id = run_dir.name
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, rpaths, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    rpaths.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    return out_dir, run_dir, rpaths


def _manager(tmp_path, out_dir, mode, rm):
    from sbmig.remediations.service import RemediationManager
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True, exist_ok=True)
    (repo / "config" / "default.yml").write_text("{}\n", encoding="utf-8")
    return RemediationManager(out_dir, repo, components_factory=lambda cfg, p: make_components(),
                              config_loader=lambda names: make_cfg(), mode=mode, run_manager=rm)


@pytest.mark.parametrize("mode", ["k8s-job", "in-process"])
def test_a_chained_final_state_whose_build_pod_is_gone_gets_its_report_from_the_web_service(tmp_path, monkeypatch,
                                                                                             mode):
    out_dir, run_dir, rpaths = _pending_chained(tmp_path, monkeypatch)
    before = RemediationState.from_dict(read_json(rpaths.state))
    assert graph.report_pending(rpaths, before) and not rpaths.report_md.exists()
    mgr = _manager(tmp_path, out_dir, mode, _RunManager("finished"))
    st = mgr.status(run_dir.name)
    after = RemediationState.from_dict(read_json(rpaths.state))
    assert rpaths.report_md.is_file() and not graph.report_pending(rpaths, after)
    assert (after.status, after.reason, after.reasons, after.message) == (before.status, before.reason, before.reasons,
                                                                          before.message)
    assert st["status"] == SUCCEEDED and st["updated"] == after.updated
    # written once: the next polls read nothing again and rewrite nothing
    mtime = rpaths.report_md.stat().st_mtime_ns
    monkeypatch.setattr(graph, "final_repair_needed", lambda *a: pytest.fail("checked again"))
    mgr.status(run_dir.name)
    mgr.status(run_dir.name)
    assert rpaths.report_md.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("rm", [_RunManager("running"), _RunManager("finished", active=True)])
def test_a_live_owner_keeps_the_web_service_away(tmp_path, monkeypatch, rm):
    out_dir, run_dir, rpaths = _pending_chained(tmp_path, monkeypatch)
    mgr = _manager(tmp_path, out_dir, "k8s-job", rm)
    mgr.status(run_dir.name)
    assert not rpaths.report_md.exists()


def test_a_fresh_claim_of_another_process_keeps_the_web_service_away(tmp_path, monkeypatch):
    out_dir, run_dir, rpaths = _pending_chained(tmp_path, monkeypatch)
    claim = chain.RunClaim(rpaths.root, purpose="resume").acquire()
    try:
        mgr = _manager(tmp_path, out_dir, "k8s-job", _RunManager("finished"))
        mgr.status(run_dir.name)
        assert not rpaths.report_md.exists()
    finally:
        claim.release()


def test_a_standalone_final_state_is_repaired_at_startup_in_job_mode_too(tmp_path, monkeypatch):
    rid = "r20261120-120000-abcdef"
    state, paths = make_run(tmp_path / "x")
    target = RunPaths(tmp_path / "out" / REMEDIATIONS_DIR / rid)
    target.root.parent.mkdir(parents=True)
    os.replace(paths.root, target.root)
    state.id = rid
    _kill_report_once(monkeypatch)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, target, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    target.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    _manager(tmp_path, tmp_path / "out", "k8s-job", None)
    assert target.report_md.is_file()


def test_the_web_service_completes_a_half_done_swap_of_a_final_state(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_dir, state, rpaths = _chained(out_dir)
    state.id = run_dir.name
    _disk_full_on_round_json(monkeypatch, rpaths, 1)
    rec = Recorder()
    run(state, rpaths, one_fix_components(maven=FakeMaven(outcomes=[True, True])), rec=rec)
    monkeypatch.undo()
    rpaths.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    assert _marker(rpaths).exists()
    _manager(tmp_path, out_dir, "k8s-job", _RunManager("finished")).status(run_dir.name)
    assert read_json(rpaths.delivery_json)["round"] == 1 and not _marker(rpaths).exists()


# ------------------------------------------------------------------------------------------------ 4. unknown usage
def test_an_interrupted_loop_with_an_unreadable_journal_is_marked_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(fixer_mod, "journal_usage", lambda folder, cfg=None: None)
    res = interrupted_result(tmp_path, None)
    assert res.usage_unknown and res.llm_calls == 0 and res.reason.endswith(fixer_mod.R_USAGE_UNKNOWN)
    assert FixResult.from_dict(res.to_dict()).usage_unknown
    assert "usage_unknown" not in FixResult().to_dict()
    fu = fix_usage({"rounds": [{"round": 1, "fix_discarded": [res.to_dict()]}]})
    assert fu["calls_unknown"] and fu["usage_unknown"] and not fu["tokens_unknown"]
    st = RemediationState.from_dict({"id": "x", "rounds": [{"round": 1, "fix_discarded": [res.to_dict()]}]})
    assert chain.summary(st, tmp_path)["fix_usage_unknown"] is True
    assert "fix_usage_unknown" not in chain.summary(RemediationState(id="y"), tmp_path)


class _JournalThenRaise:
    """A fixer that journals one answered LLM call in the loop folder, then raises (a programming error)."""

    def __init__(self, journal=True):
        self.journal = journal

    def run(self, cfg, tree, work_dir, **kw):
        if self.journal:
            (Path(work_dir) / "journal.jsonl").write_text(json.dumps(
                {"event": "llm_script", "model": "build_error_fixer", "calls": 1,
                 "tokens": {"input": 40, "output": 4}}) + "\n", encoding="utf-8")
        raise RuntimeError("boom")


def test_a_fixer_exception_in_the_graph_reads_the_loop_journal(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=_JournalThenRaise()))
    fix = out.rounds[0].fix
    assert fix.reason.startswith("error: RuntimeError") and fix.llm_calls == 1 and fix.input_tokens == 40
    assert not fix.usage_unknown


def test_a_fixer_exception_with_an_unreadable_journal_is_unknown_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(fixer_mod, "journal_usage", lambda folder, cfg=None: None)
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=_JournalThenRaise()))
    assert out.rounds[0].fix.usage_unknown and fix_usage(out)["usage_unknown"]
    md = paths.report_md.read_text(encoding="utf-8")
    assert report.USAGE_UNKNOWN_KO in md and "LLM 사용량 일부는 알 수 없음" in md


def test_calls_the_budget_counted_beyond_the_attempts_are_never_priced():
    att = SimpleNamespace(llm_usage=__import__("sbmig.core.models", fromlist=["LLMUsage"]).LLMUsage(
        model="build_error_fixer", calls=1, input_tokens=100, output_tokens=10), model="")
    cfg = SimpleNamespace(llm=SimpleNamespace(model="build_error_fixer", pricing_currency="USD", pricing_note="",
                                              pricing={"build_error_fixer": ModelPrice(input_per_mtok=1.0,
                                                                                       output_per_mtok=10.0)}))
    calls, _i, _o, _m, usage, _cur = _usage([SimpleNamespace(attempts=[att])], 3, cfg)
    # v2.1z-bo verification review: the unknown calls are a row of their own; the known row keeps its price
    known, row = usage
    assert calls == 3 and row.tokens_unknown and row.cost is None and row.calls == 2
    assert not known.tokens_unknown and known.cost is not None
    agg = fix_usage({"rounds": [{"round": 1, "fix": FixResult(attempted=True, llm_calls=3, usage=usage).to_dict()}]})
    assert agg["tokens_unknown"] and agg["by_model"][1]["cost"] is None
    # a row with unknown tokens is never priced, whatever cost it carries
    rows = fixer_mod.fix_usage_rows([FixResult(usage=[FixUsage(model="m", calls=2, cost=0.5, tokens_unknown=True)],
                                               currency="USD")])
    assert rows[0]["cost"] is None and rows[0]["tokens_unknown"]


def _unknown_state_file(run_dir: Path) -> None:
    st = RemediationState(id=run_dir.name, rounds=[RoundState(round=1, fix_discarded=[
        FixResult(attempted=True, reason=fixer_mod.R_INTERRUPTED + fixer_mod.R_USAGE_UNKNOWN, usage_unknown=True)],
        fix=FixResult(attempted=True, llm_calls=2, usage=[FixUsage(model="build_error_fixer", calls=2,
                                                                   input_tokens=10, output_tokens=1, cost=0.01)],
                      currency="USD"))])
    (run_dir / "remediation").mkdir(parents=True, exist_ok=True)
    (run_dir / "remediation" / "state.json").write_text(json.dumps(st.to_dict()), encoding="utf-8")


def test_the_readers_show_unknown_usage(tmp_path):
    from sbmig.web.admin import run_usage
    from sbmig.web.runs import run_llm_calls
    rd = tmp_path / "run-1"
    rd.mkdir()
    _unknown_state_file(rd)
    report_json = {"llm_usage": {"calls": 5, "input_tokens": 50, "output_tokens": 5},
                   "llm_usage_by_model": [], "llm_cost": {"currency": "USD", "total": 0.0, "by_model": []}}
    (rd / "report.json").write_text(json.dumps(report_json), encoding="utf-8")
    calls = run_llm_calls(rd, report_json)
    assert calls == {"llm_calls": 7, "remediation_llm_calls": 2, "remediation_llm_usage_unknown": True}
    row = run_usage(rd)
    # v2.1z-bo verification review: the priced part is still shown as a number, marked partly unknown
    assert row["cost"] == 0.01 and row["usage_partly_unknown"] is True and row["remediation_calls"] == 2
    page = (Path(__file__).resolve().parents[2] / "sbmig" / "web" / "static" / "admin.html").read_text(encoding="utf-8")
    assert "usage_partly_unknown" in page and "일부 알 수 없음" in page


def test_the_report_and_the_page_digest_say_unknown(tmp_path):
    st = RemediationState(id="x", rounds=[RoundState(round=1, committed=True, fix=FixResult(
        attempted=True, fixed=True, changed=[APP], iterations=2, llm_calls=1), fix_discarded=[
        FixResult(attempted=True, reason=fixer_mod.R_INTERRUPTED + fixer_mod.R_USAGE_UNKNOWN, usage_unknown=True)])])
    assert report.fix_summary(st)["llm_usage_unknown"] is True
    L: list[str] = []
    report._fix_section(L, st)
    text = "\n".join(L)
    assert "LLM을 1회 호출했습니다(버린 시도까지 알려진 것만 1회). LLM 사용량 일부는 알 수 없습니다." in text
    rec: list[str] = []
    report._fix_record_line(rec, st.rounds[0])
    assert any("LLM 호출 수는 알 수 없음" in ln for ln in rec)


# ------------------------------------------------------------------------------------------------ 5. one message
def test_the_baseline_outcome_message_reaches_the_report(tmp_path):
    run_dir, state, rpaths = _chained(tmp_path)
    out, rec = run(state, rpaths, one_fix_components(maven=FakeMaven(outcomes=[False])))
    assert out.status == FAILED and out.reason == "baseline_build_failed" and rpaths.report_md.is_file()
    rpaths.state.write_text(json.dumps(rec.saves[-1]), encoding="utf-8")
    st = chain._baseline_outcome(rpaths, _saved(rec), lambda e: None, [], make_cfg())
    assert st.message.startswith("the migration's final build passed on this same tree")
    on_disk = read_json(rpaths.report_json)["state"]
    assert on_disk["message"] == st.message == read_json(rpaths.state)["message"]
    assert on_disk["updated"] == read_json(rpaths.state)["updated"]
    assert not graph.report_pending(rpaths, chain.load_state(run_dir))


# ------------------------------------------------------------------------------------------------ 6. nits
def test_a_report_that_keeps_failing_is_noted_once_and_keeps_its_stamp(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    monkeypatch.setattr(report, "write_report", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    assert out.message.count("report not written") == 1
    stamp = out.updated
    for _ in range(2):
        graph.write_pending_report(out, paths, make_cfg(), save=Recorder().save, journal=Recorder().journal)
    assert out.message.count("report not written") == 1 and out.updated == stamp


def test_scan_source_changed_in_the_no_change_copy_is_zip_hash_mismatch(tmp_path, monkeypatch):
    state, paths = make_run(tmp_path)
    copy = tmp_path / "scan-source-copy.zip"
    copy.write_bytes(paths.original_zip.read_bytes())
    real_sha = sha256_file(copy)
    n = {"k": 0}

    def source(self):
        n["k"] += 1
        return copy, (real_sha if n["k"] == 1 else "0" * 64)
    monkeypatch.setattr(graph._Holder, "scan_source", source)
    out, rec = run(state, paths, make_components(scan=FakeScanner({"baseline": [scan_result()]})))
    assert out.status == FAILED and out.reason == "zip_hash_mismatch", out.message
    assert out.message == graph.SCAN_SOURCE_CHANGED and out.delivery is None
    assert not paths.delivery_zip.exists() and not graph.delivery_swap_pending(paths)
    assert not any(e.get("event") == "delivery_not_written" for e in rec.events)


def test_recover_delivery_removes_a_leftover_verified_copy_temp_file(tmp_path):
    state, paths = make_run(tmp_path)
    paths.delivery_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.delivery_zip.parent / ".result-r1-verified.zip.copy-tmp"
    tmp.write_bytes(b"half a zip")
    assert graph.delivery_swap_pending(paths)
    graph.recover_delivery(paths, state)
    assert not tmp.exists() and not graph.delivery_swap_pending(paths)


def test_the_kept_copy_of_a_tolerated_run_is_not_called_verified(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    state.baseline_tolerated = True
    scan = FakeScanner({"baseline": [scan_result(finding(), finding("example:other", "2.0"))],
                        "final-r1": [scan_result(finding("example:other", "2.0"))]})
    plan = ScriptedPlanner([
        RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(1)]),
        RoundPlan(round=2, source_tree_hash="", policy_hash="",
                  proposals=[proposal(1, round=2, ga="example:other", old="2.0", new="2.1")])])
    maven = FakeMaven(outcomes=[False, False, False],
                      logs=[javac_log(BASE_ERR), javac_log(BASE_ERR), unresolved_log("2.1")])
    out, _ = run(state, paths, make_components(scan=scan, plan=plan, maven=maven))
    assert out.status == FAILED and out.reason == "candidate_build_failed", out.message
    assert "the scanned delivery of an earlier round (its build was not confirmed) is kept as " \
           "delivery/result-r1-verified.zip" in out.message
    assert "verified delivery" not in out.message
    ko = i18n.ko_text(out.message)
    assert "재검사한 결과 ZIP(빌드로는 확인하지 못함)은 delivery/result-r1-verified.zip에 있습니다" in ko
    md = paths.report_md.read_text(encoding="utf-8")
    assert "빌드로는 확인하지 못함" in md and "라운드 1에서 검증한 결과 ZIP" not in md


# ------------------------------------------------------------------------------------------------ 7. round total
def test_the_fix_section_gives_the_rounds_total_when_other_loops_ran():
    st = RemediationState(id="x", rounds=[RoundState(round=1, committed=True, fix=FixResult(
        attempted=True, fixed=True, changed=[APP], iterations=3, llm_calls=1), fix_discarded=[
        FixResult(attempted=True, reason="discarded: the candidate was remade", llm_calls=2),
        FixResult(attempted=True, reason="stop requested", llm_calls=11)])])
    L: list[str] = []
    report._fix_section(L, st)
    assert "- 빌드와 수정을 3회 반복했고, LLM을 1회 호출했습니다(버린 시도까지 모두 14회)." in L
    assert report.fix_summary(st)["llm_calls"] == 14
    alone = RemediationState(id="y", rounds=[RoundState(round=1, committed=True, fix=FixResult(
        attempted=True, fixed=True, changed=[APP], iterations=2, llm_calls=1))])
    L2: list[str] = []
    report._fix_section(L2, alone)
    assert "- 빌드와 수정을 2회 반복했고, LLM을 1회 호출했습니다." in L2


# ------------------------------------------------------------------------------------------------ 8. stop in the loop
class _StopDuringLoop(FakeFixer):
    def __init__(self, rec):
        super().__init__(fixed=False, reason="stop requested")
        self.rec = rec

    def run(self, cfg, tree, work_dir, **kw):
        self.rec.stop_flag = True                  # the stop arrives while the loop runs
        return super().run(cfg, tree, work_dir, **kw)


def test_a_stop_during_the_build_fix_is_worded_as_such(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=_StopDuringLoop(rec)),
                 rec=rec)
    assert out.status == STOPPED and out.next_node == "build"
    assert out.message == "stop requested during the round 1 build fix"
    assert i18n.ko_text(out.message) == "라운드 1의 빌드 오류 자동 수정 중에 중지 요청을 받았습니다."


# ------------------------------------------------------------------------------------------------ 9. round 0 wording
def test_the_round_zero_step_says_checked_not_rechecked(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, make_components(scan=FakeScanner({"baseline": [scan_result()]})))
    assert out.status == SUCCEEDED and out.delivery.round == 0
    md = paths.report_md.read_text(encoding="utf-8")
    assert "**결과 ZIP 받기** — 검사를 마친 결과 ZIP을 받아 사용해 주세요." in md
    assert "재검사를 마친 결과 ZIP을 받아" not in md
    st2, p2 = make_run(tmp_path / "two")
    out2, _ = run(st2, p2, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    assert out2.status == SUCCEEDED and "재검사를 마친 결과 ZIP을" in p2.report_md.read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ 11. disk
class _KilledWithWorkCopy:
    """Writes a work copy (with a `.git` and `target/`) and one answered call into its journal, then the pod dies."""

    def run(self, cfg, tree, work_dir, **kw):
        work = Path(work_dir)
        for rel in ("tree/.git/HEAD", "tree/target/app.jar", "tree/src/App.java"):
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text("x", encoding="utf-8")
        (work / "journal.jsonl").write_text(json.dumps(
            {"event": "llm_script", "model": "build_error_fixer", "calls": 1, "tokens": {"input": 9, "output": 1}})
            + "\n", encoding="utf-8")
        (work / "mvn-1.log").write_text("[INFO] BUILD\n", encoding="utf-8")
        raise Killed()


def _killed_loop(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=_KilledWithWorkCopy()),
            rec=rec)
    st = _saved(rec)
    st.status = "INTERRUPTED"
    return st, paths


def test_an_interrupted_loops_work_copy_is_removed_once_its_usage_is_saved(tmp_path):
    st, paths = _killed_loop(tmp_path)
    rec = Recorder()
    out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False]), fixer=FakeFixer(fixed=False,
                                                                                                  reason="x")),
                 rec=rec)
    kept = paths.round_dir(1) / "fixloop-interrupted-1"
    assert kept.is_dir() and not (kept / "tree").exists()
    assert (kept / "journal.jsonl").is_file() and (kept / "mvn-1.log").is_file()
    interrupted = [f for f in out.rounds[0].fix_discarded if f.reason.startswith(fixer_mod.R_INTERRUPTED)]
    assert len(interrupted) == 1 and interrupted[0].llm_calls == 1
    # the usage was saved before the folder was touched
    first = next(i for i, s in enumerate(rec.saves)
                 if any(str(f.get("reason", "")).startswith(fixer_mod.R_INTERRUPTED)
                        for f in s["rounds"][0].get("fix_discarded") or []))
    assert first >= 0


def test_a_work_copy_that_cannot_be_removed_is_a_warning_only(tmp_path, monkeypatch, caplog):
    st, paths = _killed_loop(tmp_path)
    real = graph._safe_rmtree

    def refuse(p):
        if Path(p).name == "tree" and "fixloop-interrupted" in str(p):
            raise PermissionError("busy")
        return real(p)
    monkeypatch.setattr(graph, "_safe_rmtree", refuse)
    with caplog.at_level(logging.WARNING, logger="sbmig.remediations.graph"):
        out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False]),
                                                   fixer=FakeFixer(fixed=False, reason="x")))
    assert out.status == FAILED
    assert (paths.round_dir(1) / "fixloop-interrupted-1" / "tree").is_dir()
    assert any("work copy of the interrupted fix loop was not removed" in r.getMessage() for r in caplog.records)

