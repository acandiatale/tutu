"""The remediation's build-fix-loop LLM calls counted honestly (v2.1z-bi follow-up): `FixResult.usage` per asked
model (priced under the run's `llm.pricing`), one reader-side sum over every round (`fixer.fix_usage`), and the three
places that show a total — `SBMIG-RESULT … llm=`, `GET /api/runs/{id}` `llm_calls`, the admin usage rows — each
counting a call exactly once. The migration's report.json is never rewritten."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.core.config import ModelPrice
from sbmig.core.models import LLMUsage
from sbmig.remediations import chain
from sbmig.remediations.fixer import _usage, fix_usage, fix_usage_rows, run_fix_usage
from sbmig.remediations.models import FixResult, FixUsage, RemediationState, RoundState
from sbmig.report.result_line import report_llm_calls, report_result_line
from sbmig.web.admin import collect_usage, run_usage
from sbmig.web.runs import run_llm_calls
from tests.loop.fakes import write_script
from tests.remediations import test_fixer as tf

rcfg = tf.rcfg                                   # the remediation fixture of the fixer tests


def _attempt(model: str, calls: int, inp: int, out: int, served=(), attempt_model: str = ""):
    return SimpleNamespace(llm_usage=LLMUsage(model=model, calls=calls, input_tokens=inp, output_tokens=out,
                                              served=list(served)), model=attempt_model)


def _cfg(pricing=None, model="build_error_fixer"):
    return SimpleNamespace(llm=SimpleNamespace(model=model, pricing=pricing or {}, pricing_currency="USD",
                                               pricing_note=""))


# ------------------------------------------------------------------------------------------------ FixResult.usage
def test_usage_is_per_asked_model_and_adds_up_to_the_totals():
    results = [SimpleNamespace(attempts=[_attempt("build_error_fixer", 1, 100, 10, ["gpt-5.5"]),
                                         _attempt("escalation_fixer", 2, 300, 30, ["gpt-5.6-sol"]),
                                         _attempt("unknown", 1, 50, 5, attempt_model="build_error_fixer"),
                                         _attempt("", 0, 0, 0)]),
               SimpleNamespace(attempts=[_attempt("build_error_fixer", 1, 100, 10, ["gpt-5.5", "build_error_fixer"])])]
    pricing = {"build_error_fixer": ModelPrice(input_per_mtok=1.0, output_per_mtok=10.0)}
    calls, inp, out, models, usage, currency = _usage(results, 0, _cfg(pricing))
    assert (calls, inp, out) == (5, 550, 55)
    assert models == ["build_error_fixer", "escalation_fixer"]
    assert [(u.model, u.served, u.calls, u.input_tokens, u.output_tokens) for u in usage] == [
        ("build_error_fixer", ["gpt-5.5"], 3, 250, 25), ("escalation_fixer", ["gpt-5.6-sol"], 2, 300, 30)]
    assert usage[0].cost == pytest.approx(250 / 1e6 * 1.0 + 25 / 1e6 * 10.0)
    assert usage[1].cost is None and currency == "USD"


def test_calls_the_budget_counted_beyond_the_attempts_go_to_the_configured_model():
    calls, _inp, _out, models, usage, currency = _usage([SimpleNamespace(attempts=[_attempt("x", 1, 10, 1)])], 4,
                                                         _cfg())
    assert calls == 4 and models == ["x", "build_error_fixer"]
    assert [(u.model, u.calls, u.input_tokens) for u in usage] == [("x", 1, 10), ("build_error_fixer", 3, 0)]
    assert currency == "" and all(u.cost is None for u in usage)


def test_no_call_no_rows():
    assert _usage([SimpleNamespace(attempts=[])], 0, _cfg()) == (0, 0, 0, [], [], "")


def test_the_loop_fills_usage(rcfg, tmp_path):
    rcfg.llm.pricing = {"fake-model": ModelPrice(input_per_mtok=2.0, output_per_mtok=4.0)}
    res = tf.run(rcfg, tf.make_tree(tmp_path), scripts=[write_script({"src/main/java/com/x/Foo.java": "class Foo {}\n"})])
    assert res.fixed and res.llm_calls == 1 and res.models == ["fake-model"]
    assert [(u.model, u.calls, u.input_tokens, u.output_tokens) for u in res.usage] == [("fake-model", 1, 100, 20)]
    assert res.usage[0].cost == pytest.approx(100 / 1e6 * 2.0 + 20 / 1e6 * 4.0)
    assert res.currency == rcfg.llm.pricing_currency


def test_json_leaves_usage_out_when_empty_and_reads_old_json_back():
    assert "usage" not in FixResult().to_dict() and "currency" not in FixResult().to_dict()
    f = FixResult(llm_calls=2, usage=[FixUsage(model="m", calls=2, input_tokens=5, output_tokens=1)])
    d = f.to_dict()
    assert d["usage"] == [{"model": "m", "calls": 2, "input_tokens": 5, "output_tokens": 1}]   # no cost, no served
    assert FixResult.from_dict(d) == f
    priced = FixResult(usage=[FixUsage(model="m", served=["s"], calls=1, cost=0.5)], currency="USD").to_dict()
    assert priced["usage"][0]["cost"] == 0.5 and priced["usage"][0]["served"] == ["s"] and priced["currency"] == "USD"
    old = FixResult.from_dict({"attempted": True, "llm_calls": 3, "models": ["m"]})
    assert old.usage == [] and old.currency == ""


# ------------------------------------------------------------------------------------------------ reader-side sum
def test_rows_over_rounds_count_unfixed_rounds_and_old_results():
    r1 = FixResult(fixed=False, llm_calls=2, input_tokens=20, output_tokens=2, models=["a"],
                   usage=[FixUsage(model="a", calls=2, input_tokens=20, output_tokens=2, cost=0.2)], currency="USD")
    r2 = FixResult(fixed=True, llm_calls=1, input_tokens=10, output_tokens=1, models=["a"],
                   usage=[FixUsage(model="a", served=["real-a"], calls=1, input_tokens=10, output_tokens=1, cost=0.1)],
                   currency="USD")
    old = {"llm_calls": 4, "input_tokens": 40, "output_tokens": 4, "models": ["b", "c"]}     # written before usage
    rows = fix_usage_rows([r1, None, r2.to_dict(), old, FixResult()])
    assert rows == [
        {"model": "a", "served": ["real-a"], "calls": 3, "input_tokens": 30, "output_tokens": 3, "cost": 0.3,
         "currency": "USD"},
        {"model": "b", "served": [], "calls": 4, "input_tokens": 40, "output_tokens": 4, "cost": None, "currency": ""}]
    # one unpriced part makes the row's cost unknown
    rows = fix_usage_rows([r1, FixResult(usage=[FixUsage(model="a", calls=1)])])
    assert rows[0]["calls"] == 3 and rows[0]["cost"] is None


def _state(*fixes) -> RemediationState:
    return RemediationState(id="run-1", rounds=[RoundState(round=i + 1, fix=f) for i, f in enumerate(fixes)])


def test_fix_usage_reads_a_state_or_its_json(tmp_path):
    st = _state(FixResult(llm_calls=2, usage=[FixUsage(model="a", calls=2, input_tokens=7, output_tokens=3)]), None,
                FixResult(llm_calls=1, usage=[FixUsage(model="b", calls=1, input_tokens=1, output_tokens=1)]))
    want = {"calls": 3, "input_tokens": 8, "output_tokens": 4}
    got = fix_usage(st)
    assert {k: got[k] for k in want} == want and [r["model"] for r in got["by_model"]] == ["a", "b"]
    assert fix_usage(st.to_dict()) == got
    rd = tmp_path / "run-1"
    assert run_fix_usage(rd)["calls"] == 0                       # no remediation
    (rd / "remediation").mkdir(parents=True)
    (rd / "remediation" / "state.json").write_text("{not json", encoding="utf-8")
    assert run_fix_usage(rd)["calls"] == 0                       # unreadable counts nothing
    (rd / "remediation" / "state.json").write_text(json.dumps(st.to_dict()), encoding="utf-8")
    assert run_fix_usage(rd) == got
    assert fix_usage({"rounds": "x"})["calls"] == 0 and fix_usage(None)["calls"] == 0


# ------------------------------------------------------------------------------------------------ result line
def _report(calls: int, remediation=None, flow=None):
    return SimpleNamespace(run_id="r", flags=[], blocked=[], unresolved=[], error_entries=[], success=True,
                           llm_usage=LLMUsage(calls=calls), structure_review=None, operator_checks=[],
                           remediation=remediation, flow=flow, plan=None, profile=None, final_build=None)


def test_summary_carries_the_fix_calls_and_the_result_line_adds_them(tmp_path):
    st = _state(FixResult(llm_calls=2), FixResult(fixed=True, llm_calls=3))
    st.status = "SUCCEEDED"
    summ = chain.summary(st, tmp_path)
    assert summ["fix_llm_calls"] == 5
    assert "fix_llm_calls" not in chain.summary(_state(None), tmp_path)
    assert "fix_llm_calls" not in chain.summary(None, tmp_path)
    # chained run: the migration's 7 calls + the remediation's 5
    assert report_llm_calls(_report(7, summ)) == 12
    assert " llm=12 " in report_result_line(_report(7, summ), 0)
    # remediation-only run: the migration counts nothing
    assert " llm=5 " in report_result_line(_report(0, summ, flow="remediation"), 0)
    assert report_llm_calls(_report(4, {"status": "skipped"})) == 4
    assert report_llm_calls(_report(4, {"fix_llm_calls": "9"})) == 4 == report_llm_calls(_report(4, None))


# ------------------------------------------------------------------------------------------------ web: run status
def _write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_run_status_counts_the_remediation_calls_once(tmp_path):
    rd = tmp_path / "run-1"
    report = {"llm_usage": {"calls": 7}, "remediation": {"status": "SUCCEEDED", "fix_llm_calls": 99}}
    assert run_llm_calls(rd, report) == {"llm_calls": 7, "remediation_llm_calls": 0, "remediation_llm_usage_unknown": False}
    _write(rd / "remediation" / "state.json", _state(FixResult(llm_calls=2), FixResult(llm_calls=1)).to_dict())
    # the state is the source (a resume pod's calls are there); the summary's digest is never added
    assert run_llm_calls(rd, report) == {"llm_calls": 10, "remediation_llm_calls": 3, "remediation_llm_usage_unknown": False}
    assert run_llm_calls(rd, {}) == {"llm_calls": 3, "remediation_llm_calls": 3, "remediation_llm_usage_unknown": False}
    assert run_llm_calls(tmp_path / "none", {}) == {"llm_calls": None, "remediation_llm_calls": 0,
                                                    "remediation_llm_usage_unknown": False}


# ------------------------------------------------------------------------------------------------ web: admin
REQ = {"accepted_at": "2026-11-19T10:00:00+00:00", "upload_name": "shop.zip", "flow": "full"}


def _run_dir(out: Path, rid: str, *, by_model, total, cost=None, fixes=(), flow="full") -> Path:
    rd = out / rid
    _write(rd / "control" / "request.json", {**REQ, "run_id": rid, "flow": flow})
    _write(rd / "report.json", {"run_id": rid, "started_at": REQ["accepted_at"], "flow": flow, "llm_usage": total,
                                "llm_usage_by_model": by_model, "llm_cost": cost,
                                "remediation": {"status": "SUCCEEDED", "fix_llm_calls": 1000}})
    if fixes:
        _write(rd / "remediation" / "state.json", _state(*fixes).to_dict())
    return rd


def _priced(model, calls, inp, out, cost):
    return FixResult(llm_calls=calls, input_tokens=inp, output_tokens=out, models=[model], currency="USD",
                     usage=[FixUsage(model=model, calls=calls, input_tokens=inp, output_tokens=out, cost=cost)])


def test_admin_adds_the_remediation_rows_and_cost(tmp_path):
    out = tmp_path / "out"
    mig_cost = {"currency": "USD", "total": 1.0, "unpriced_models": [], "usage_missing": False,
                "by_model": [{"model": "build_error_fixer", "cost": 1.0}]}
    rd = _run_dir(out, "shop-20261119-100000-aaaaaa",
                  by_model=[{"model": "build_error_fixer", "calls": 4, "input_tokens": 1000, "output_tokens": 100,
                             "served": ["gpt-5.5"]}],
                  total={"calls": 4, "input_tokens": 1000, "output_tokens": 100}, cost=mig_cost,
                  fixes=[_priced("build_error_fixer", 2, 200, 20, 0.25),          # a round that was not fixed
                         _priced("escalation_fixer", 1, 100, 10, 0.5)])
    row = run_usage(rd)
    assert (row["calls"], row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (7, 1300, 130, 1430)
    assert row["remediation_calls"] == 3 and row["cost"] == pytest.approx(1.75)
    by = {m["model"]: m for m in row["by_model"]}
    assert (by["build_error_fixer"]["calls"], by["build_error_fixer"]["cost"]) == (6, pytest.approx(1.25))
    assert by["build_error_fixer"]["served"] == ["gpt-5.5"]
    assert (by["escalation_fixer"]["calls"], by["escalation_fixer"]["total_tokens"]) == (1, 110)
    data = collect_usage(out)
    assert data["totals"]["calls"] == 7 and data["totals"]["cost"] == pytest.approx(1.75)
    assert {m["model"]: m["calls"] for m in data["by_model"]} == {"build_error_fixer": 6, "escalation_fixer": 1}


def test_admin_remediation_only_run_is_priced_by_the_remediation(tmp_path):
    rd = _run_dir(tmp_path / "out", "shop-20261119-100000-bbbbbb", by_model=[],
                  total={"calls": 0, "input_tokens": 0, "output_tokens": 0}, flow="remediation",
                  fixes=[_priced("build_error_fixer", 2, 200, 20, 0.25)])
    row = run_usage(rd)
    assert row["calls"] == 2 and row["remediation_calls"] == 2 and row["cost"] == pytest.approx(0.25)
    assert [m["model"] for m in row["by_model"]] == ["build_error_fixer"]


def test_admin_unpriced_remediation_makes_the_cost_unknown(tmp_path):
    rd = _run_dir(tmp_path / "out", "shop-20261119-100000-cccccc", by_model=[],
                  total={"calls": 0, "input_tokens": 0, "output_tokens": 0},
                  fixes=[FixResult(llm_calls=3, input_tokens=30, output_tokens=3, models=["m"])])   # old JSON shape
    row = run_usage(rd)
    assert row["calls"] == 3 and row["cost"] is None and row["by_model"][0]["model"] == "m"


def test_admin_without_remediation_calls_keeps_the_old_row(tmp_path):
    rd = _run_dir(tmp_path / "out", "shop-20261119-100000-dddddd",
                  by_model=[{"model": "a", "calls": 1, "input_tokens": 1, "output_tokens": 1}],
                  total={"calls": 1, "input_tokens": 1, "output_tokens": 1}, fixes=[FixResult(attempted=True)])
    row = run_usage(rd)
    assert row["calls"] == 1 and "remediation_calls" not in row


# ------------------------------------------------------------------------------------------------ v2.1z-bj
def test_discarded_fixes_and_a_failed_confirming_build_still_count(tmp_path):
    """A fix thrown away (remade candidate → `fix_discarded`; failed confirming build → `fixed: false`) spent its
    calls all the same: `fix_usage` and `run_fix_usage` count them."""
    gone = FixResult(attempted=True, reason="discarded: the candidate was remade", llm_calls=3, discarded_changed=["A"],
                     usage=[FixUsage(model="a", calls=3, input_tokens=30, output_tokens=3)])
    confirm_failed = FixResult(attempted=True, reason="error: confirming build failed", llm_calls=2,
                               discarded_changed=["B"], usage=[FixUsage(model="a", calls=2, input_tokens=20,
                                                                        output_tokens=2)])
    st = RemediationState(id="run-1", rounds=[
        RoundState(round=1, fix=FixResult(attempted=True, fixed=True, changed=["A"], llm_calls=1,
                                          usage=[FixUsage(model="b", calls=1, input_tokens=1, output_tokens=1)]),
                   fix_discarded=[gone]),
        RoundState(round=2, fix=confirm_failed)])
    got = fix_usage(st)
    assert (got["calls"], got["input_tokens"], got["output_tokens"]) == (6, 51, 6)
    assert {r["model"]: r["calls"] for r in got["by_model"]} == {"a": 5, "b": 1}
    assert fix_usage(st.to_dict()) == got
    rd = tmp_path / "run-1"
    (rd / "remediation").mkdir(parents=True)
    (rd / "remediation" / "state.json").write_text(json.dumps(st.to_dict()), encoding="utf-8")
    assert run_fix_usage(rd) == got
