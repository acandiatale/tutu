"""실행 전체를 이끄는 오케스트레이터와 빌드 오류 수정 루프를 묶은 패키지.
어떤 경우에도 최종 ``mvn clean package``를 시도하고, 해결하지 못한 오류를 담은 보고서로 끝낸다.

The orchestrator owns the loop; done = BUILD SUCCESS. The fix loop works case by case (L2) on the run's error
list (`entries.RunErrors`): stored script -> LLM augment / new (edits applied directly, scripts stored in
`kb.cases`), see `fixloop` / `sbmig.triage.scripts`.

Run order: intake -> analyze -> plan -> transform -> restructure -> structure review -> verify build -> fix
loop -> final build -> report and KB record. ``Orchestrator`` is the entry point for the CLI, the web, the
fleet and ``sbmig.api``; ``BuildFixLoop`` fixes build errors per case (L2) by reuse -> augment -> new;
``RunErrors`` / ``CaseScripts`` adapt the run's error list and the KB case store; ``LoopBudget`` = LLM call
and time budget; ``BlockedRegistry`` = the errors no longer tried."""
from .budget import LoopBudget
from .components import LazyFixer, LazyScriptRunner, MissingComponentError, NullKnowledgeBase, ScriptFixerMissing, lazy_attr
from .entries import CaseScripts, RunErrors
from .fixloop import BuildFixLoop
from .orchestrator import STOP_AFTER_PHASES, Orchestrator, check_flow, parse_flow, parse_stop_after

# dry-run(`run(dry_run=True)`, `BuildFixLoop(..., dry_run=True)`)은 결정만 저널에 남긴다. 프로젝트는 바꾸지 않는다

__all__ = ["Orchestrator", "BuildFixLoop", "LoopBudget", "NullKnowledgeBase", "LazyFixer", "MissingComponentError",
           "LazyScriptRunner", "ScriptFixerMissing", "STOP_AFTER_PHASES", "parse_stop_after",
           "parse_flow", "check_flow",
           "lazy_attr", "RunErrors", "CaseScripts"]
