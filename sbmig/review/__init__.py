"""재구성이 끝난 프로젝트를 첫 빌드 전에 LLM으로 검토하는 패키지.
pom, 의존성, 모듈 구조, 소스 인코딩, 필터링되는 리소스가 회사 규칙에 맞는지 본다. critical 문제는 바로 고친다.

Spec: docs/v2/PLAN-v2.md §3.3, docs/INTERFACES.md "Structure review". Edits for critical issues go through
`sbmig.script.apply_edits` and stay uncommitted for the orchestrator; a script-only answer runs through the
`ScriptRunnerLike`. Every review ends as a `StructureReview` (the four-part answer in `answer`).

The orchestrator calls ``StructureReviewer.review`` in the ``restructure.review`` step and commits the edits
as ``review: …``. Modules: reviewer.py (StructureReviewer), inputs.py (collect_review_input, ReviewInput),
parse.py (parse_review_answer, ParsedReview). A failed review never stops the run: the verdict is ``failed``
and the build goes on. The review edits build files only and never writes the KB.
"""
from .inputs import ReviewInput, collect_review_input
from .parse import ParsedReview, parse_review_answer
from .reviewer import StructureReviewer, system_prompt

__all__ = ["ParsedReview", "ReviewInput", "StructureReviewer", "collect_review_input", "parse_review_answer",
           "system_prompt"]
