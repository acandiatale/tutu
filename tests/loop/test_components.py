from __future__ import annotations

from sbmig.core.models import ErrorCluster, ErrorKind, KBCase, PhaseResult, Phase
from sbmig.loop import LazyFixer, LoopBudget, NullKnowledgeBase
from sbmig.loop.components import basic_profile_summary, profile_summary

from .fakes import make_profile


def test_null_kb():
    kb = NullKnowledgeBase()
    c = ErrorCluster(id="x", kind=ErrorKind.COMPILE, errors=[])
    assert kb.match(c, None, None).recipe_ids == []
    assert kb.active_recipe_files() == [] and kb.active_recipe_ids() == [] and kb.list_recipes() == []
    assert kb.record_case(KBCase(id="k")) == "k" and kb.reindex() == 0
    assert kb.credit_recipe("r", True, "fp") is None
    assert kb.harvest(PhaseResult(phase=Phase.SPRING), None, None).cases_created == []


def test_lazy_fixer_and_summaries():
    made = []

    class F:
        def propose(self, ws, cluster, ctx):
            return "p"

        def apply(self, ws, changes):
            return "a"

    lf = LazyFixer(lambda: made.append(1) or F())
    assert made == []
    assert lf.propose(None, None, None) == "p" and lf.apply(None, []) == "a" and made == [1]
    assert "spring_boot" in basic_profile_summary(make_profile()) and basic_profile_summary(None) == ""
    assert profile_summary(make_profile())
    b = LoopBudget(max_llm_calls=2, max_minutes=0)
    b.charge_llm(2)
    assert not b.llm_allowed() and not b.time_exceeded()
