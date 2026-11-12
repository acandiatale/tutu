"""실행 전체를 이끄는 오케스트레이터와 빌드 오류 수정 루프를 묶은 패키지.
실행 순서: 접수 -> 분석 -> 계획 -> 변환 -> 재구성 -> 구조 검토 -> 검증
빌드 -> 수정 루프 -> 최종 빌드 -> 보고서와 KB 기록.
- ``Orchestrator``: CLI, 웹, 플릿, ``sbmig.api``가 호출하는 진입점
- ``BuildFixLoop``: 빌드 오류를 케이스(L2 서명)별로 묶어 재사용 -> 보강 -> 신규 순서로 고친다
- ``RunErrors`` / ``CaseScripts``: 실행의 오류 목록과 KB 케이스 저장소를 다루는 어댑터
- ``LoopBudget``: LLM 호출 수와 실행 시간 예산
- ``BlockedRegistry``: 더 이상 시도하지 않는 오류의 차단 목록
어떤 경우에도 최종 ``mvn clean package``를 시도하고, 해결하지 못한 오류를 담은 보고서로 끝낸다.

sbmig.loop — orchestrator + build-fix loop (the orchestrator owns the loop; done = BUILD SUCCESS). v2.1: the fix loop
works case by case (L2) on the run's error list (`entries.RunErrors`): stored script → LLM augment / new (edits applied
directly, scripts stored in `kb.cases`), see `fixloop` / `sbmig.triage.scripts`."""
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, ScriptFixerMissing, lazy_attr
from .entries import CaseScripts, RunErrors
from .fixloop import BuildFixLoop
from .orchestrator import STOP_AFTER_PHASES, Orchestrator, check_flow, parse_flow, parse_stop_after

# 드라이런은 케이스 분류와 시도 단계 결정만 저널에 남기고 프로젝트에는 아무것도 적용하지 않는다
# DryRun mode: `Orchestrator(...).run(dry_run=True)` / `BuildFixLoop(..., dry_run=True)` — decisions are computed and
# journaled but nothing is applied.
__all__ = ["Orchestrator", "BuildFixLoop", "LoopBudget", "NullKnowledgeBase", "LazyFixer", "MissingComponentError",
           "LazyScriptRunner", "ScriptFixerMissing", "STOP_AFTER_PHASES", "parse_stop_after",
           "parse_flow", "check_flow",
           "lazy_attr", "RunErrors", "CaseScripts"]
