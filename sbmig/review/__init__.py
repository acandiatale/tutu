"""sbmig.review — v2 structure review (flow-v2, docs/v2/PLAN-v2.md §3.3, docs/INTERFACES.md "Structure review").

After the restructure, before the first build: the LLM checks the poms, dependencies and module layout against the
company rules; critical issues → one fix script (`ScriptRunnerLike`, left uncommitted for the orchestrator); every
review → `StructureReview` (report section "Structure review", Handover follow-ups, `<kb>/reviews.jsonl`).
"""
from .inputs import ReviewInput, collect_review_input
from .parse import ParsedReview, parse_review_answer
from .reviewer import StructureReviewer, system_prompt

__all__ = ["ParsedReview", "ReviewInput", "StructureReviewer", "collect_review_input", "parse_review_answer",
           "system_prompt"]
