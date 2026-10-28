"""트리아지 패키지 — 빌드 오류를 케이스별 클러스터로 묶고,
케이스마다 무엇을 할지(스크립트 / 차단 / 건너뜀) 정한다. 흐름상 위치: verify.build 가 실패하면 수정
루프(``sbmig.loop.fixloop``)가 라운드마다 ``Triager`` 를 부른다. 핵심 진입점: ``Triager``(cluster / decide /
plan_iteration), ``cluster_cases``(L2 서명별 묶음, 근본 원인 먼저), ``DecisionEngine``(재사용 → 보강 → 신규
사다리, 차단 정책), ``classify_blocked``(코드로 못 고치는 오류 판정).

sbmig.triage — error clustering (v2: by case), the decision engine (v2: fix-script ladder), blocked
classification."""
from .blocked import BlockedCandidate, classify_blocked, extract_owner, is_project_class
from .cluster import (BASE_PRIORITY, CASE_PREFIX, CAUSE_RANK, RESOLUTION_KINDS, case_id_of, cause_rank, cluster_cases,
                      cluster_errors, cluster_id, cluster_label, error_class, is_coarse_l2, l2_case_id,
                      refine_signature, refine_signatures)
from .decide import DecisionEngine, IterationItem, plan_iteration, related_attempts
from .scripts import ScriptStep, plan_script_step
from .triager import Triager

__all__ = [
    "Triager", "DecisionEngine", "IterationItem", "plan_iteration", "related_attempts", "cluster_errors", "cluster_id",
    "cluster_label", "cluster_cases", "case_id_of", "l2_case_id", "CASE_PREFIX", "CAUSE_RANK", "cause_rank",
    "error_class", "is_coarse_l2", "refine_signature", "refine_signatures", "ScriptStep", "plan_script_step",
    "BASE_PRIORITY", "RESOLUTION_KINDS", "BlockedCandidate", "classify_blocked", "extract_owner", "is_project_class",
]
