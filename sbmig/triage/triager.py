"""수정 루프가 오류를 묶고 케이스마다 할 일을 정할 때 쓰는 파사드(``Triager``) 모듈.
실제 판단은 모두 ``DecisionEngine``이 한다.

``Triager`` implements TriagerLike. Every round the loop calls ``cluster`` -> ``plan_iteration`` -> ``decide``
per cluster.

`decide` leaves the script step of a SCRIPT decision in `script_step(cluster_id)` for the fix loop; `cluster`
is scoped by the loop's build target."""
from __future__ import annotations

from typing import Optional

from sbmig.core.config import Config
from sbmig.core.models import BuildError, ErrorCluster, KBMatch, ProjectProfile, TriageDecision

from .blocked import BlockedCandidate
from .cluster import cluster_cases
from .decide import DecisionEngine, IterationItem, plan_iteration
from .scripts import ScriptStep


class Triager:
    """오류 묶기, 건너뛰기, 결정을 묶어 제공하는 클래스.
    실행 하나 동안 같은 ``DecisionEngine``을 쓴다. 그래서 케이스별 시도 이력과 차단 상태가 이어진다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = DecisionEngine(cfg)

    # TriagerLike ---------------------------------------------------------
    def cluster(self, errors: list[BuildError], profile: Optional[ProjectProfile] = None,
                scope: Optional[str] = None) -> list[ErrorCluster]:
        """파일을 가리지 않고 케이스(L2 서명)마다 클러스터 하나를 만든다.
        ``scope``는 루프의 빌드 대상(MSA 서비스)이다."""
        return cluster_cases(errors, profile, scope=scope)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        """클러스터 하나의 결정(SCRIPT / BLOCKED / SKIP / ESCALATE)을 반환한다."""
        return self.engine.decide(cluster, kb_match, ctx, profile)

    # 수정 루프가 추가로 쓰는 메서드 -------------------------------------
    def plan_iteration(self, clusters: list[ErrorCluster]) -> list[IterationItem]:
        """이번 라운드에 다룰 클러스터 목록을 만든다. 순서는 받은 그대로다.
        같은 모듈에 의존성 해결 오류가 있으면 컴파일 오류는 건너뛴다."""
        return plan_iteration(clusters)

    # 실행 전체의 차단 상태 -----------------------------
    def attach_run(self, blocked_registry=None, ws=None, cases=None, notes=None) -> None:
        """실행 전체의 차단 목록, 작업 공간, KB 케이스 저장소(``kb.cases``), 지식 노트 목록을 결정 엔진에 넘긴다.
        None인 인자는 그대로 둔다. ``replacement: true`` 노트가 맞는 라이브러리는 차단하지 않는다."""
        if blocked_registry is not None:
            self.engine.blocked_registry = blocked_registry
        if ws is not None:
            self.engine.ws = ws
        if cases is not None:
            self.engine.cases = cases
        if notes is not None:
            self.engine.notes = list(notes)

    def set_llm_unavailable(self, reason: Optional[str]) -> None:
        """LLM이 연속으로 실패해 쓸 수 없게 되었음을 알린다.
        그 뒤로 저장된 스크립트가 없는 케이스는 ``llm_unavailable``로 차단된다.
        저장된 스크립트 재사용과 최종 빌드는 계속된다."""
        self.engine.llm_unavailable = reason

    def blocked_candidate(self, cluster_id: str) -> Optional[BlockedCandidate]:
        """``cluster_id``의 마지막 BLOCKED 결정 이유를 반환한다.
        차단 이유, 타입, 소유 클래스, 차단한 오류가 들어 있다."""
        return self.engine.blocked_info.get(cluster_id)

    def script_step(self, cluster_id: str) -> Optional[ScriptStep]:
        """``cluster_id``의 마지막 SCRIPT 결정이 고른 시도 단계를 반환한다.
        재사용, 보강, 신규 중 무엇인지와 KB 탐색 로그 줄이 들어 있다."""
        return self.engine.script_steps.get(cluster_id)
