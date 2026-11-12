"""재구성 직후 첫 빌드 전에 LLM으로 구조를 검토하는 패키지. LLM이 pom, 의존성, 모듈 구조, 소스 인코딩,
필터링되는 리소스를 회사 규칙에 맞춰 검토하고 critical 문제는 직접 고친다.
오케스트레이터가 `restructure.review` 단계에서 StructureReviewer.review를 호출하고, 수정 내용을 `review: …`로 커밋한다.
주요 구성은 StructureReviewer(reviewer.py), collect_review_input / ReviewInput(inputs.py),
parse_review_answer / ParsedReview(parse.py).
- 검토가 실패해도 실행을 멈추지 않고 verdict `failed`로 빌드를 진행한다
- 빌드 파일만 고치고 KB에는 직접 쓰지 않는다

sbmig.review — v2 structure review (flow-v2, docs/v2/PLAN-v2.md §3.3, docs/INTERFACES.md "Structure review").

After the restructure, before the first build: the LLM checks the poms, dependencies and module layout against the
company rules and fixes what is wrong itself (v2.1): its `<edits>` for the critical issues are applied through
`sbmig.script.apply_edits` (left uncommitted for the orchestrator), a v2 answer's script through the
`ScriptRunnerLike`; every review → `StructureReview` with the four-part answer in `answer` (report section
"Structure review", Handover follow-ups; the loop records it in the error list — the reviewer never writes the KB).


"""
from .inputs import ReviewInput, collect_review_input
from .parse import ParsedReview, parse_review_answer
from .reviewer import StructureReviewer, system_prompt

__all__ = ["ParsedReview", "ReviewInput", "StructureReviewer", "collect_review_input", "parse_review_answer",
           "system_prompt"]
