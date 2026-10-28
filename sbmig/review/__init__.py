"""sbmig.review — 구조 검토(v2): 재구성 직후·첫 빌드 전에 LLM 이 pom·의존성·모듈 구조·소스 인코딩·필터링되는
바이너리 리소스를 회사 규칙(`artifacts.*`, `layout.*`)에 비추어 검토하고, critical 은 직접 고친다. 흐름상
위치: plan 단계 `restructure.review` — 오케스트레이터가 StructureReviewer.review 를 부르고, 적용된 수정은
오케스트레이터가 `review: …` 로 커밋한다. 핵심: StructureReviewer(reviewer.py), collect_review_input /
ReviewInput(inputs.py, 프롬프트에 보여 줄 사실),
parse_review_answer / ParsedReview(parse.py).
지킬 것: 검토는 실행을 절대 멈추지 않는다(실패해도 verdict `failed` 로 빌드 진행). 소스는 고치지 않는다(빌드
파일만). KB 에는 직접 쓰지 않는다(루프가 오류 목록에 `review` 레코드로 남긴다).

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
