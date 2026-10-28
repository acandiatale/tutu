"""sbmig.loop — 실행의 심장: 오케스트레이터(단계 구동)와 빌드-수정 루프.
오케스트레이터가 루프를 소유하고, 끝 = BUILD SUCCESS. 흐름: 접수 → 분석 → 계획 → 변환 → 재구성 → 구조 검토 →
검증 빌드 → (실패 시) 수정 루프 → 최종 빌드 → 보고서·KB 기록. 진입점:
``Orchestrator``(CLI·웹·플릿·``sbmig.api`` 가 부름), ``BuildFixLoop``(케이스(L2)별 사다리 재사용 → 보강 →
신규), ``RunErrors`` / ``CaseScripts``(오류 목록·KB 케이스 저장소 어댑터), ``LoopBudget`` (LLM 호출·시간
예산), ``BlockedRegistry``(실행 전체의 차단 목록). 불변조건(빌드 게이트): 무슨 일이 있어도 최종 ``mvn clean
package`` 시도와 미해결 오류를 적은 보고서로 끝난다.

sbmig.loop — orchestrator + build-fix loop (the orchestrator owns the loop; done = BUILD SUCCESS). v2.1: the fix loop
works case by case (L2) on the run's error list (`entries.RunErrors`): stored script → LLM augment / new (edits applied
directly, scripts stored in `kb.cases`), see `fixloop` / `sbmig.triage.scripts`."""
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, ScriptFixerMissing, lazy_attr
from .entries import CaseScripts, RunErrors
from .fixloop import BuildFixLoop
from .orchestrator import STOP_AFTER_PHASES, Orchestrator, parse_stop_after

# 드라이런: 결정(케이스 분류·사다리 단계)만 계산해 저널에 남기고, 프로젝트에는 아무것도 적용하지 않는다.
# DryRun mode: `Orchestrator(...).run(dry_run=True)` / `BuildFixLoop(..., dry_run=True)` — decisions are computed and
# journaled but nothing is applied.
__all__ = ["Orchestrator", "BuildFixLoop", "LoopBudget", "NullKnowledgeBase", "LazyFixer", "MissingComponentError",
           "LazyScriptRunner", "ScriptFixerMissing", "STOP_AFTER_PHASES", "parse_stop_after",
           "lazy_attr", "RunErrors", "CaseScripts"]
