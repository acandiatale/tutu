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
    assert kb.scripts is None                              # no store: nothing reused, LLM scripts run unstored (v0)
    assert kb.record_review(object()) is None
    assert not hasattr(kb, "append_error_cases")           # --no-kb: the error cases stay in the run dir


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
