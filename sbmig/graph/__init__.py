"""sbmig.graph — 이관 파이프라인을 모는 LangGraph 엔진(회사 관례: 에이전트는 LangGraph 그래프).
그래프는 오케스트레이터의 step_* 를 '몰기만' 한다: 빌드 게이트, 중단 처리, stop_after, 오류 목록, 저널은 전부
sbmig.loop.orchestrator 에 있다. 자세한 것은 engine 모듈과 docs/ARCHITECTURE.md "Graph engine".

sbmig.graph — LangGraph driver of the migration pipeline (company convention: agents are LangGraph graphs).

The graph only *drives* the orchestrator's shared step methods (`Orchestrator.step_*`); build gate, abort semantics,
`stop_after`, the error-case records and the journal stay in `sbmig.loop.orchestrator`. See docs/ARCHITECTURE.md
"Graph engine".


"""
from .engine import NODES, RECURSION_LIMIT, MigrationState, build_graph, run_graph, stream_graph

__all__ = ["NODES", "RECURSION_LIMIT", "MigrationState", "build_graph", "run_graph", "stream_graph"]
