"""``Triager`` — 수정 루프가 쓰는 트리아지 창구(TriagerLike): 묶기 + 가리기 + 결정.
흐름상 위치: 수정 루프가 라운드마다 ``cluster``(케이스별 클러스터) → ``plan_iteration``(이번 라운드 순서) →
클러스터마다 ``decide``(SCRIPT / BLOCKED / SKIP / ESCALATE) 를 부르고, SCRIPT 면 ``script_step`` 으로 사다리
단계(재사용 / 보강 / 신규)를 받아 간다. 실제 판단은 전부 ``DecisionEngine`` 이 한다.

`Triager` (TriagerLike): clustering + masking + decisions.

v2 (flow-v2): `cluster` groups by case (L2) across files (`cluster_cases`, scoped by the loop's build target);
`decide` returns SCRIPT / BLOCKED / SKIP / ESCALATE and leaves the script step of a SCRIPT decision in
`script_step(cluster_id)` for the fix loop."""
from __future__ import annotations

from typing import Optional

from sbmig.core.config import Config
from sbmig.core.models import BuildError, ErrorCluster, KBMatch, ProjectProfile, TriageDecision

from .blocked import BlockedCandidate
from .cluster import cluster_cases
from .decide import DecisionEngine, IterationItem, plan_iteration
from .scripts import ScriptStep


class Triager:
    """트리아지 창구. 실행 하나 동안 같은 ``DecisionEngine`` 을
    들고 있어 케이스별 시도 이력·차단 상태가 이어진다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = DecisionEngine(cfg)

    # TriagerLike ---------------------------------------------------------
    def cluster(self, errors: list[BuildError], profile: Optional[ProjectProfile] = None,
                scope: Optional[str] = None) -> list[ErrorCluster]:
        """v2: 파일을 가로질러 케이스(L2 서명) 하나당 클러스터 하나. ``scope`` = 루프의 빌드 대상(MSA 서비스).

        v2: one cluster per case (L2) across files; `scope` = the loop's build target (MSA service)."""
        return cluster_cases(errors, profile, scope=scope)

    def decide(self, cluster: ErrorCluster, kb_match: Optional[KBMatch], ctx, profile: ProjectProfile) -> TriageDecision:
        """클러스터 하나에 대한 결정(``DecisionEngine.decide``): SCRIPT / BLOCKED / SKIP / ESCALATE."""
        return self.engine.decide(cluster, kb_match, ctx, profile)

    # extras used by the build-fix loop -------------------------------------
    def plan_iteration(self, clusters: list[ErrorCluster]) -> list[IterationItem]:
        """이번 라운드의 처리 순서(우선순위 순, 같은 모듈의 해결형 오류가 컴파일 오류를 가린다)."""
        return plan_iteration(clusters)

    # run-wide blocked state (ERROR-HANDLING §1/§3) -----------------------------
    def attach_run(self, blocked_registry=None, ws=None, cases=None) -> None:
        """실행 전체의 차단 목록(BlockedRegistry), 작업 공간(프로젝트 클래스 판별용), KB 케이스
        저장소(``kb.cases``, v2.1)를 결정 엔진과 공유한다.

        Share the run-wide BlockedRegistry, the workspace (project-class checks) and the KB's case store
        (`kb.cases`, v2.1) with the decision engine."""
        if blocked_registry is not None:
            self.engine.blocked_registry = blocked_registry
        if ws is not None:
            self.engine.ws = ws
        if cases is not None:
            self.engine.cases = cases

    def set_llm_unavailable(self, reason: Optional[str]) -> None:
        """LLM 이 연속 실패해 못 쓰게 됐음을 알린다. 그 뒤로 저장된 스크립트가 없는 케이스는
        ``llm_unavailable`` 로 차단되고, 저장본 재사용과 최종 빌드는 계속된다."""
        self.engine.llm_unavailable = reason

    def blocked_candidate(self, cluster_id: str) -> Optional[BlockedCandidate]:
        """``cluster_id`` 의 마지막 BLOCKED 결정 이유(차단 이유, 타입, 소유 클래스, 포함된 오류).

        Why the last BLOCKED decision for `cluster_id` was taken (reason, types, owner, covered errors)."""
        return self.engine.blocked_info.get(cluster_id)

    def script_step(self, cluster_id: str) -> Optional[ScriptStep]:
        """``cluster_id`` 의 마지막 SCRIPT 결정이 고른 사다리 단계(재사용 / 보강 / 신규 + KB 탐색 로그 줄).

        The ladder step (reuse / augment / new, with its KB search line) of the last SCRIPT decision for
        `cluster_id`."""
        return self.engine.script_steps.get(cluster_id)
