from __future__ import annotations

import pytest

from sbmig.core.models import ErrorCluster, ErrorKind, KBMatch
from sbmig.loop import LazyFixer, LoopBudget, NullKnowledgeBase, ScriptFixerMissing
from sbmig.loop.components import basic_profile_summary, profile_summary

from .fakes import make_profile


def test_null_kb_is_what_the_v2_flow_calls():
    kb = NullKnowledgeBase()
    c = ErrorCluster(id="x", kind=ErrorKind.COMPILE, errors=[])
    m = kb.match(c, None, None)
    assert isinstance(m, KBMatch) and m.case_hits == [] and m.hints == []
    assert kb.cases is None                                # no store: nothing reused, LLM scripts run unstored (v0)
    assert kb.record_run([object()]) == 0                  # --no-kb: errors.jsonl stays in the run dir
    assert not hasattr(kb, "scripts") and not hasattr(kb, "record_review")    # v2.1: gone from the facade


def test_budget_zero_is_unlimited_and_minus_one_is_no_llm():
    b = LoopBudget(max_llm_calls=0)
    b.charge_llm(10_000)
    assert b.llm_allowed() and not b.llm_exhausted() and b.llm_text() == "10000 calls (no limit)"
    capped = LoopBudget(max_llm_calls=2)
    capped.charge_llm(2)
    assert not capped.llm_allowed() and capped.llm_exhausted() and capped.llm_text() == "2/2 calls"
    off = LoopBudget(max_llm_calls=-1)
    assert not off.llm_allowed() and off.llm_exhausted() and off.llm_text() == "fleet"


def test_lazy_fixer_and_summaries():
    made = []

    class F:
        def propose_script(self, ws, cluster, ctx):
            return "s"

    lf = LazyFixer(lambda: made.append(1) or F())
    assert made == []
    assert lf.propose_script(None, None, None) == "s" and made == [1]
    assert "spring_boot" in basic_profile_summary(make_profile()) and basic_profile_summary(None) == ""
    assert profile_summary(make_profile())
    b = LoopBudget(max_llm_calls=2, max_minutes=0)
    b.charge_llm(2)
    assert not b.llm_allowed() and not b.time_exceeded()


def test_lazy_fixer_without_propose_script():
    class V1:
        def propose(self, ws, cluster, ctx):              # pragma: no cover - never called
            return "p"

    with pytest.raises(ScriptFixerMissing, match="propose_script"):
        LazyFixer(V1).propose_script(None, None, None)
    assert not hasattr(LazyFixer(V1), "apply") and not hasattr(LazyFixer(V1), "propose")
