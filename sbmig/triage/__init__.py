"""빌드 오류를 케이스별로 묶고 케이스마다 할 일(스크립트, 차단, 건너뜀)을 정하는 패키지.

Error clustering by case, the decision engine (fix-script ladder), blocked classification.

After a failed verify build the fix loop (``sbmig.loop.fixloop``) calls ``Triager`` every round.
``cluster_cases`` groups the errors by L2 signature, root causes first. ``DecisionEngine`` picks the try in
the order reuse -> augment -> new and applies the blocked policy. ``classify_blocked`` finds the errors no
code change in the project fixes."""
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
