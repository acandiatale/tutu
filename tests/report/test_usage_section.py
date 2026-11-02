"""No LLM usage at all → no "LLM usage and cost" section (nothing to report); with calls the section is there.
(This guard used to live in the deleted v2.1i edits-report test file.)"""
from __future__ import annotations

from sbmig.core.models import LLMUsage
from sbmig.report import render_markdown
from sbmig.report.cost import compute_llm_cost

from .test_error_list import entries, report_with


def test_usage_section_needs_usage():
    r = report_with(entries())
    r.llm_usage = LLMUsage()
    r.llm_cost = compute_llm_cost([LLMUsage(model="m", calls=0)], None)
    assert "## LLM usage and cost" not in render_markdown(r)
    r.llm_usage = LLMUsage(model="m", calls=2, prompt_tokens=10, completion_tokens=5)
    r.llm_cost = compute_llm_cost([r.llm_usage], None)
    assert "## LLM usage and cost" in render_markdown(r)
