"""Regression tests for the verification review of v2.1z-bo and the third real-Maven smoke run.

1. a stop or a kill during the rerun after a stored fix found not usable on resume does not keep the next entry
   from running the build fix loop (`FixResult.ended_round` marks the discard that ended the round);
2. calls the budget counted beyond what the attempts recorded get their own unpriced row; known rows keep their price;
3. `chain._budget_outcome` stamps a new `updated`, saves, and asks for the report once (a STOPPED state has no
   report node, so no report keeps the older text);
4. a damaged or missing loop journal is unknown usage, not an exact zero;
5. the loop's minimum time is checked once, in the graph;
6. wording: a partly unknown total is not presented as exact; a repaired report drops the "report not written" note;
7. `RemediationManager._repair_seen` is bounded.
No Maven, no LLM.
"""
from __future__ import annotations

import pytest

from sbmig.remediations import graph
from sbmig.remediations.models import STOPPED, SUCCEEDED, FixResult, RemediationState

from .fakes import FakeMaven, Recorder, make_run
from .test_fix_loop_review2 import Killed
from .test_graph import one_fix_components, run
from .test_graph_fix_loop import FakeFixer


# ------------------------------------------------------------------------------------------------ 1. ended_round
class _StopInLoop(FakeFixer):
    def __init__(self, rec):
        super().__init__()
        self.rec = rec

    def run(self, *a, **kw):
        res = super().run(*a, **kw)
        self.rec.stop_flag = True        # the stop arrives during the loop: fix kept, STOPPED before the confirm
        return res


def _stopped_with_unusable_fix(tmp_path):
    """A run STOPPED with a recorded fix whose tree hash no longer matches: not usable on resume."""
    state, paths = make_run(tmp_path)
    rec = Recorder()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=_StopInLoop(rec)),
                 rec=rec)
    assert out.status == STOPPED and out.rounds[0].fix is not None and out.rounds[0].fix.fixed, out.message
    st = RemediationState.from_dict(out.to_dict())
    st.rounds[0].fix.tree_hash = "0" * 64
    return st, paths


def test_resume_with_an_unusable_stored_fix_reruns_the_loop(tmp_path):
    st, paths = _stopped_with_unusable_fix(tmp_path)
    fx = FakeFixer()
    out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx))
    assert len(fx.calls) == 1
    gone = out.rounds[0].fix_discarded[-1]
    assert gone.reason == graph.R_FIX_UNUSABLE and not gone.ended_round
    assert "ended_round" not in gone.to_dict()


def test_kill_after_a_non_fresh_unusable_discard_reruns_the_loop_on_re_entry(tmp_path, monkeypatch):
    st, paths = _stopped_with_unusable_fix(tmp_path)
    real = graph._drop_fix_files
    monkeypatch.setattr(graph, "_drop_fix_files", lambda h, f: (_ for _ in ()).throw(Killed()))
    rec = Recorder()
    with pytest.raises(Killed):
        run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=FakeFixer()), rec=rec)
    monkeypatch.setattr(graph, "_drop_fix_files", real)
    st2 = RemediationState.from_dict(rec.saves[-1])
    assert st2.rounds[0].fix is None and st2.rounds[0].fix_discarded[-1].reason == graph.R_FIX_UNUSABLE
    assert not st2.rounds[0].fix_discarded[-1].ended_round
    st2.status = "INTERRUPTED"
    fx = FakeFixer()
    out, _ = run(st2, paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx))
    assert len(fx.calls) == 1, "re-entry did not run the loop the uninterrupted resume runs"
    assert out.status == SUCCEEDED, out.message


def test_stop_during_the_rerun_build_then_resume_runs_the_loop(tmp_path):
    st, paths = _stopped_with_unusable_fix(tmp_path)
    rec = Recorder()
    n = {"k": 0}

    def on_package(project):
        n["k"] += 1
        if n["k"] == 1:
            rec.stop_flag = True          # the stop arrives during the candidate build after the unusable discard
    out, _ = run(st, paths, one_fix_components(maven=FakeMaven(outcomes=[False], on_package=on_package),
                                               fixer=FakeFixer()), rec=rec)
    assert out.status == STOPPED
    st2 = RemediationState.from_dict(out.to_dict())
    fx = FakeFixer()
    out2, _ = run(st2, paths, one_fix_components(maven=FakeMaven(outcomes=[False, True]), fixer=fx))
    assert len(fx.calls) == 1 and out2.status == SUCCEEDED, out2.message


def test_the_fresh_unusable_discard_is_marked_in_the_same_save(tmp_path, monkeypatch):
    from pathlib import Path

    from .test_graph_fix_loop import APP

    class PomFixer(FakeFixer):
        def run(self, cfg, tree, work_dir, **kw):
            res = super().run(cfg, tree, work_dir, **kw)
            (Path(work_dir) / "fixed" / "pom.xml").write_text("<project>evil</project>\n")
            res.changed = ["pom.xml", APP]
            return res
    state, paths = make_run(tmp_path)
    monkeypatch.setattr(graph, "_drop_fix_files", lambda h, f: (_ for _ in ()).throw(Killed()))
    rec = Recorder()
    with pytest.raises(Killed):
        run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=PomFixer()), rec=rec)
    saved = rec.saves[-1]["rounds"][0]["fix_discarded"][-1]
    assert saved["ended_round"] is True and saved["reason"] == graph.R_FIX_UNUSABLE


def test_ended_round_is_left_out_when_false_and_old_json_reads_false():
    assert "ended_round" not in FixResult(attempted=True).to_dict()
    assert FixResult.from_dict({"attempted": True, "reason": graph.R_FIX_UNUSABLE}).ended_round is False
    assert FixResult.from_dict(FixResult(ended_round=True).to_dict()).ended_round is True


# ------------------------------------------------------------------------------------------------ 3. budget outcome
def _budget_run(tmp_path, monkeypatch, journal=None):
    from sbmig.remediations import chain

    from .fakes import make_cfg
    from .test_chain import Clock, go, project
    for k, v in {"IVAS_BASE_URL": "https://ivas.example.com", "IVAS_USER_ID": "u", "IVAS_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)
    clock = Clock()
    monkeypatch.setattr(chain, "_monotonic", clock)
    stamps = []
    real = chain._budget_outcome

    def spy(paths, state, *a, **kw):
        stamps.append(state.updated)               # the stamp the graph's last save gave the STOPPED state
        return real(paths, state, *a, **kw)
    monkeypatch.setattr(chain, "_budget_outcome", spy)
    comps = one_fix_components(maven=FakeMaven(on_package=lambda _p: setattr(clock, "t", 31 * 60.0)))
    run_dir, root = project(tmp_path)
    out, _ = go(run_dir, root, cfg=make_cfg(after_migration=True, chain_budget_minutes=30), comps=comps)
    return out, chain.chain_paths(run_dir), stamps


def test_the_budget_outcome_gets_a_new_stamp_and_is_saved(tmp_path, monkeypatch):
    from sbmig.remediations import chain
    from sbmig.remediations.models import read_json
    out, paths, stamps = _budget_run(tmp_path, monkeypatch)
    assert out.status == STOPPED and out.reason == chain.R_CHAIN_BUDGET
    saved = read_json(paths.state)
    assert saved["message"] == out.message and "time budget of 30 minute(s)" in saved["message"]
    assert saved["reason"] == chain.R_CHAIN_BUDGET and saved["updated"] == out.updated
    assert stamps and out.updated > stamps[0]       # a new `updated` for the new reason and message
    # a STOPPED state has no report node: no remediation report carries the older "stop requested" text
    assert not paths.report_md.exists() and not graph.report_pending(paths, chain.load_state(paths.root.parent))


def test_the_budget_outcome_asks_for_the_report_once(tmp_path, monkeypatch):
    seen = []
    real = graph.write_pending_report

    def spy(st, *a, **kw):
        seen.append((st.reason, st.message))
        return real(st, *a, **kw)
    monkeypatch.setattr(graph, "write_pending_report", spy)
    out, _paths, _ = _budget_run(tmp_path, monkeypatch)
    from sbmig.remediations import chain
    assert seen.count((chain.R_CHAIN_BUDGET, out.message)) == 1


# ------------------------------------------------------------------------------------------------ 2. own unknown row
def _priced_cfg():
    from types import SimpleNamespace

    from sbmig.core.config import ModelPrice
    return SimpleNamespace(llm=SimpleNamespace(model="build_error_fixer", pricing_currency="USD", pricing_note="",
                                               pricing={"build_error_fixer": ModelPrice(input_per_mtok=1.0,
                                                                                        output_per_mtok=10.0)}))


def _attempt(calls=1, inp=1_000_000, out=100_000):
    from types import SimpleNamespace

    from sbmig.core.models import LLMUsage
    return SimpleNamespace(llm_usage=LLMUsage(model="build_error_fixer", calls=calls, input_tokens=inp,
                                              output_tokens=out), model="")


def test_one_failed_call_gets_its_own_unpriced_row_and_the_known_row_keeps_its_price():
    from types import SimpleNamespace

    from sbmig.remediations.fixer import _usage, fix_usage
    calls, inp, out, models, usage, cur = _usage([SimpleNamespace(attempts=[_attempt(2)])], 3, _priced_cfg())
    assert (calls, inp, out, models, cur) == (3, 1_000_000, 100_000, ["build_error_fixer"], "USD")
    known, unknown = usage
    assert (known.calls, known.cost, known.tokens_unknown) == (2, 2.0, False)
    assert (unknown.model, unknown.calls, unknown.input_tokens, unknown.cost, unknown.tokens_unknown) == (
        "build_error_fixer", 1, 0, None, True)
    fx = FixResult(attempted=True, fixed=True, llm_calls=3, usage=usage, currency=cur)
    agg = fix_usage({"rounds": [{"round": 1, "fix": fx.to_dict()}]})
    assert agg["calls"] == 3 and agg["tokens_unknown"] and agg["usage_unknown"] and not agg["calls_unknown"]
    rows = agg["by_model"]
    assert [(r["model"], r["calls"], r["cost"], r.get("tokens_unknown", False)) for r in rows] == [
        ("build_error_fixer", 2, 2.0, False), ("build_error_fixer", 1, None, True)]


def test_all_known_usage_has_one_priced_row():
    from types import SimpleNamespace

    from sbmig.remediations.fixer import _usage
    calls, _i, _o, _m, usage, cur = _usage([SimpleNamespace(attempts=[_attempt(2)])], 2, _priced_cfg())
    assert calls == 2 and cur == "USD" and len(usage) == 1 and usage[0].cost == 2.0
    assert "tokens_unknown" not in usage[0].to_dict()


def _run_dir_with(tmp_path, fix: FixResult, discarded=(), report_cost=None, report_calls=0):
    import json
    rd = tmp_path / "run-1"
    (rd / "remediation").mkdir(parents=True)
    st = RemediationState(id="run-1")
    from sbmig.remediations.models import RoundState
    st.rounds = [RoundState(round=1, fix=fix, fix_discarded=list(discarded))]
    (rd / "remediation" / "state.json").write_text(json.dumps(st.to_dict()), encoding="utf-8")
    by_model = ([{"model": "build_error_fixer", "calls": report_calls, "input_tokens": 10, "output_tokens": 1,
                  "cost": report_cost}] if report_calls else [])
    rep = {"llm_usage": {"calls": report_calls, "input_tokens": 10 if report_calls else 0,
                         "output_tokens": 1 if report_calls else 0},
           "llm_usage_by_model": [{"model": "build_error_fixer", "calls": report_calls, "input_tokens": 10,
                                   "output_tokens": 1}] if report_calls else [],
           "llm_cost": {"currency": "USD", "total": report_cost if report_calls else 0.0, "by_model": by_model}}
    (rd / "report.json").write_text(json.dumps(rep), encoding="utf-8")
    return rd


def test_admin_row_all_known(tmp_path):
    from sbmig.remediations.models import FixUsage
    from sbmig.web.admin import run_usage
    rd = _run_dir_with(tmp_path, FixResult(attempted=True, llm_calls=2, usage=[
        FixUsage(model="build_error_fixer", calls=2, input_tokens=100, output_tokens=10, cost=0.5)], currency="USD"))
    row = run_usage(rd)
    assert row["cost"] == 0.5 and row["calls"] == 2 and "usage_partly_unknown" not in row


def test_admin_row_one_failed_call_beside_known_ones_keeps_the_known_cost(tmp_path):
    from sbmig.remediations.models import FixUsage
    from sbmig.web.admin import run_usage
    rd = _run_dir_with(tmp_path, FixResult(attempted=True, llm_calls=3, usage=[
        FixUsage(model="build_error_fixer", calls=2, input_tokens=100, output_tokens=10, cost=0.5),
        FixUsage(model="build_error_fixer", calls=1, tokens_unknown=True)], currency="USD"),
        report_cost=0.25, report_calls=1)
    row = run_usage(rd)
    assert row["usage_partly_unknown"] is True and row["remediation_calls"] == 3 and row["calls"] == 4
    assert row["cost"] == 0.75
    [m] = row["by_model"]
    assert m["calls"] == 4 and m["cost"] == 0.75 and m["input_tokens"] == 110


def test_admin_row_nothing_known_stays_unknown(tmp_path):
    from sbmig.remediations.models import FixUsage
    from sbmig.web.admin import run_usage
    only_unknown = _run_dir_with(tmp_path / "a", FixResult(attempted=True, llm_calls=1, usage=[
        FixUsage(model="build_error_fixer", calls=1, tokens_unknown=True)]))
    row = run_usage(only_unknown)
    assert row["cost"] is None and row["usage_partly_unknown"] is True and row["calls"] == 1
    assert row["by_model"][0]["cost"] is None
    unread = _run_dir_with(tmp_path / "b", FixResult(attempted=True, usage_unknown=True))
    row = run_usage(unread)
    assert row["cost"] is None and row["usage_partly_unknown"] is True and row["calls"] == 0


def test_admin_row_an_unread_loop_beside_a_priced_migration_keeps_the_migration_cost(tmp_path):
    from sbmig.web.admin import run_usage
    rd = _run_dir_with(tmp_path, FixResult(attempted=True, usage_unknown=True), report_cost=0.25, report_calls=1)
    row = run_usage(rd)
    assert row["cost"] == 0.25 and row["usage_partly_unknown"] is True and row["calls"] == 1


def test_the_admin_page_writes_the_note_beside_a_known_cost():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[2] / "sbmig" / "web" / "static" / "admin.html").read_text(encoding="utf-8")
    assert "${esc(fmtCost(c))}${unknown ? UNKNOWN_NOTE : ''}" in page


def test_the_report_keeps_the_unknown_sentence_for_a_failed_call_row():
    from sbmig.remediations import report
    from sbmig.remediations.models import FixUsage, RoundState

    from .test_graph_fix_loop import APP
    st = RemediationState(id="x", rounds=[RoundState(round=1, committed=True, fix=FixResult(
        attempted=True, fixed=True, changed=[APP], iterations=2, llm_calls=3, usage=[
            FixUsage(model="m", calls=2, input_tokens=5, output_tokens=1, cost=0.1),
            FixUsage(model="m", calls=1, tokens_unknown=True)]))])
    L: list[str] = []
    report._fix_section(L, st)
    assert "- 빌드와 수정을 2회 반복했고, LLM을 3회 호출했습니다. LLM 사용량 일부는 알 수 없습니다." in L
    assert report.fix_summary(st)["llm_usage_unknown"] is True


# ------------------------------------------------------------------------------------------------ 4. strict journal
_EV = '{"event": "llm_script", "model": "build_error_fixer", "calls": 1, "tokens": {"input": 40, "output": 4}}\n'


def test_a_damaged_journal_is_unknown_and_keeps_what_was_read(tmp_path):
    from sbmig.remediations.fixer import interrupted_result, journal_usage
    from sbmig.web.admin import usage_from_journal
    (tmp_path / "loop.json").write_text('{"loop": "t", "round": 1}', encoding="utf-8")
    (tmp_path / "journal.jsonl").write_text(_EV + '{"event": "llm_scr', encoding="utf-8")   # cut off by the kill
    rows, _cur, known = journal_usage(tmp_path)
    assert not known and rows[0].calls == 1
    res = interrupted_result(tmp_path)
    assert res.usage_unknown and res.llm_calls == 1 and res.input_tokens == 40
    assert res.reason.endswith(" (usage unknown)")
    # the admin page's own reader is unchanged for its other callers
    assert usage_from_journal(tmp_path)["build_error_fixer"]["calls"] == 1


def test_a_journal_of_garbage_is_unknown_not_zero(tmp_path):
    from sbmig.remediations.fixer import interrupted_result
    (tmp_path / "journal.jsonl").write_bytes(b"\x00\xff garbage\nnot json\n")
    res = interrupted_result(tmp_path)
    assert res.usage_unknown and res.llm_calls == 0 and res.reason.endswith(" (usage unknown)")


def test_a_missing_journal_beside_loop_json_is_unknown(tmp_path):
    from sbmig.remediations.fixer import interrupted_result, journal_usage
    (tmp_path / "loop.json").write_text('{"loop": "t", "round": 1}', encoding="utf-8")
    assert journal_usage(tmp_path) == ([], "", False)
    res = interrupted_result(tmp_path)
    assert res.usage_unknown and res.llm_calls == 0


def test_a_folder_with_neither_file_is_known_zero(tmp_path):
    from sbmig.remediations.fixer import interrupted_result, journal_usage
    assert journal_usage(tmp_path) == ([], "", True)
    res = interrupted_result(tmp_path)
    assert not res.usage_unknown and res.reason == "interrupted: the run ended during the loop"
    assert "usage_unknown" not in res.to_dict()


def test_a_clean_journal_with_blank_lines_is_known(tmp_path):
    from sbmig.remediations.fixer import interrupted_result
    (tmp_path / "loop.json").write_text('{"loop": "t", "round": 1}', encoding="utf-8")
    (tmp_path / "journal.jsonl").write_text('{"event": "start"}\n\n' + _EV, encoding="utf-8")
    res = interrupted_result(tmp_path)
    assert not res.usage_unknown and res.llm_calls == 1


# ------------------------------------------------------------------------------------------------ 5. one minimum
def test_the_fixer_starts_with_less_than_the_minimum_left(cfg, tmp_path, monkeypatch):
    from sbmig.remediations import fixer as fixer_mod

    from .test_fixer import FOO, make_tree, write_script
    from .test_fixer import run as run_loop
    clock = {"t": 0.0}
    monkeypatch.setattr(fixer_mod, "_clock", lambda: clock["t"])
    cfg.remediation.fix_loop_max_iterations = 3
    tree = make_tree(tmp_path)
    # the graph granted exactly the minimum; a few milliseconds passed before the loop looked at the clock
    res = run_loop(cfg, tree, scripts=[write_script({FOO: "class Foo { ok }\n"})],
                   deadline=fixer_mod.MIN_LOOP_SECONDS - 0.005)
    assert res.attempted and res.fixed


def test_fix_loop_max_minutes_one_starts_the_loop(tmp_path):
    import time

    from sbmig.remediations import fixer as fixer_mod

    from .fakes import make_cfg
    state, paths = make_run(tmp_path)
    fx = FakeFixer()
    before = time.monotonic()
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, False]), fixer=fx),
                 cfg=make_cfg(fix_loop_max_minutes=1))
    assert len(fx.calls) == 1 and out.rounds[0].fix.attempted
    after = time.monotonic()
    # the deadline was set between `before` and `after` with exactly the configured minute
    assert before + fixer_mod.MIN_LOOP_SECONDS <= fx.calls[0]["deadline"] <= after + fixer_mod.MIN_LOOP_SECONDS


# ------------------------------------------------------------------------------------------------ 6. wording
def test_a_partly_unknown_total_is_not_presented_as_exact():
    from sbmig.remediations import fixer as fixer_mod
    from sbmig.remediations import report
    from sbmig.remediations.models import RoundState

    from .test_graph_fix_loop import APP
    st = RemediationState(id="x", rounds=[RoundState(round=1, committed=True, fix=FixResult(
        attempted=True, fixed=True, changed=[APP], iterations=2, llm_calls=1), fix_discarded=[
        FixResult(attempted=True, reason=fixer_mod.R_INTERRUPTED, llm_calls=2),
        FixResult(attempted=True, reason=fixer_mod.R_INTERRUPTED + fixer_mod.R_USAGE_UNKNOWN, usage_unknown=True)])])
    L: list[str] = []
    report._fix_section(L, st)
    assert ("- 빌드와 수정을 2회 반복했고, LLM을 1회 호출했습니다(버린 시도까지 알려진 것만 3회). "
            "LLM 사용량 일부는 알 수 없습니다.") in L
    assert not any("모두 3회" in ln for ln in L)


def test_a_report_written_on_repair_drops_the_report_not_written_note(tmp_path, monkeypatch):
    from sbmig.remediations import report
    from sbmig.remediations.models import read_json

    from .fakes import make_cfg
    state, paths = make_run(tmp_path)
    real = report.write_report
    monkeypatch.setattr(report, "write_report", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[True, True])))
    out.message = "all changes verified | " + out.message           # a run whose message had text before the note
    assert out.message.endswith(" | report not written: RuntimeError: disk")
    plain = "all changes verified"
    monkeypatch.setattr(report, "write_report", real)
    rec = Recorder()
    assert graph.write_pending_report(out, paths, make_cfg(), save=rec.save, journal=rec.journal)
    assert out.message == plain and "report not written" not in out.message
    assert rec.saves[-1]["message"] == plain
    on_disk = read_json(paths.report_json)["state"]
    assert on_disk["message"] == plain and on_disk["updated"] == out.updated
    assert "report not written" not in paths.report_md.read_text(encoding="utf-8")


def test_the_note_is_cut_from_a_message_that_was_prefixed_later():
    msg = "the migration's final build passed … | baseline failed | report not written: OSError: x"
    assert graph._without_report_note(msg) == "the migration's final build passed … | baseline failed"
    assert graph._without_report_note("report not written: OSError: x") == ""
    assert graph._without_report_note("all good") == "all good"


# ------------------------------------------------------------------------------------------------ 7. bounded memory
def test_the_repair_memory_keeps_only_the_most_recent_entries(tmp_path, monkeypatch):
    from sbmig.remediations.models import SUCCEEDED, RunPaths

    from .test_fix_loop_review5 import _manager, _RunManager
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mgr = _manager(tmp_path, out_dir, "k8s-job", _RunManager("finished"))
    monkeypatch.setattr(type(mgr), "REPAIR_SEEN_MAX", 3)
    monkeypatch.setattr(graph, "final_repair_needed", lambda *a: False)
    for i in range(5):
        st = RemediationState(id=f"r{i}", status=SUCCEEDED, updated=float(i))
        mgr.repair_final(f"r{i}", RunPaths(tmp_path / f"r{i}"), st, False)
    assert list(mgr._repair_seen) == [str(tmp_path / f"r{i}") for i in (2, 3, 4)]
    # an evicted folder is simply checked once more
    checked = []
    monkeypatch.setattr(graph, "final_repair_needed", lambda p, s: checked.append(s.id) or False)
    mgr.repair_final("r0", RunPaths(tmp_path / "r0"), RemediationState(id="r0", status=SUCCEEDED, updated=0.0), False)
    mgr.repair_final("r4", RunPaths(tmp_path / "r4"), RemediationState(id="r4", status=SUCCEEDED, updated=4.0), False)
    assert checked == ["r0"] and len(mgr._repair_seen) == 3


def test_the_repair_memory_limit_is_two_thousand():
    from sbmig.remediations.service import RemediationManager
    assert RemediationManager.REPAIR_SEEN_MAX == 2000
