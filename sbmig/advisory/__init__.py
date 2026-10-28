"""라이브러리 권고 — EOL / 더는 관리되지 않는 라이브러리 안내(springfox, hystrix, sleuth, 옛 Spring Cloud …).
흐름상 위치: 분석 뒤 ``evaluate_advisories`` 가 이 프로젝트에 맞는 권고를 찾고, 수정 루프는 ``hints_for`` 로
오류에 맞는 권고를 스크립트 프롬프트 힌트로 넣고, 마무리의 ``verify_advisories`` 가 최종 트리를 다시 훑어
결과(replaced / still-present / unknown)를 정한다. 보고서 인수인계 섹션이 그 결과를 담당팀에 보여 준다.
결정(사용자): 권고는 안내만 한다 — 레시피를 더하지 않고, 차단 정책("block")은 절대 없다. 500개 프로젝트가 모두
완벽할 수는 없으므로 권고가 실행을 실패시키거나 종료 코드를 바꾸지 않는다. 권고 파일이 없거나 비어도 안전해야
한다. 아래의 import 실패 대비(fallback) 정의는 패키지가
반쯤 빠져도 오케스트레이터가 깨지지 않게 하려는 것이다.

Library advisories: EOL / no-longer-managed libraries (springfox, hystrix, sleuth, old Spring Cloud trains …).

Contract (PM, round 4; v2: guide-only) — the orchestrator, the planner, the report and the fleet depend on exactly
these names:

    load_advisories(cfg)                       -> AdvisoryCatalog        knowledge/libraries/advisories.yml (+ extra_files)
    evaluate_advisories(profile, cfg, catalog=None) -> list[AdvisoryFinding]
                                               findings for this project (entries set to "off" in
                                               advisories.policy_overrides are skipped)
    verify_advisories(findings, view, cfg)     -> list[AdvisoryFinding]
                                               finalize: re-detect libraries on the final tree (`view` = Workspace or
                                               PathView over the migrated project) and set outcome
                                               (replaced | still-present | unknown); never raises
    followups_for(findings)                    -> list[str]   one actionable sentence per finding, not-replaced first
    hints_for(errors, findings, profile=None)  -> (list[str], list[str])   round 6: guidance lines + advisory ids for
                                               the findings whose library / artifacts the cluster's errors mention
                                               (FixContext.advisory_hints, FixAttempt.advisory_ids); never raises
    advisories_cli                             typer sub-app mounted as `sbmig advisories` (list | show | check)

Advisories are guide-only (LLM hints + Handover): they never add a recipe, and there is deliberately no "block" —
an advisory never fails the run or changes the exit code. Everything here must be safe to call with an empty /
missing advisories file (→ no findings).

Implementation: sbmig/advisory/{catalog,evaluate,verify,cli}.py (agent A). This module re-exports them and keeps
the import-safe fallbacks below so the orchestrator never breaks when the package is half-built.


"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

log = logging.getLogger("sbmig.advisory")

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.config import Config
    from sbmig.core.models import AdvisoryFinding, ProjectProfile

try:
    from .catalog import AdvisoryCatalog, load_advisories
    from .evaluate import evaluate_advisories, followups_for
    from .verify import verify_advisories
except ImportError as _e:  # pragma: no cover - until agent A lands the modules
    # 하위 모듈을 못 읽으면 "권고 없음" 으로 동작하는 대체 정의 — 권고 때문에 실행이 멈추면 안 된다
    log.debug("sbmig.advisory not fully available yet: %s", _e)

    class AdvisoryCatalog:  # type: ignore[no-redef]
        entries: list = []

        def get(self, advisory_id: str) -> Any:
            return None

        def __len__(self) -> int:
            return 0

    def load_advisories(cfg: "Config") -> "AdvisoryCatalog":  # type: ignore[misc]
        return AdvisoryCatalog()

    def evaluate_advisories(profile: "ProjectProfile", cfg: "Config",
                            catalog: Optional["AdvisoryCatalog"] = None) -> list["AdvisoryFinding"]:  # type: ignore[misc]
        return []

    def verify_advisories(findings: list["AdvisoryFinding"], view: Any, cfg: "Config") -> list["AdvisoryFinding"]:  # type: ignore[misc]
        return list(findings)

    def followups_for(findings: list["AdvisoryFinding"]) -> list[str]:  # type: ignore[misc]
        return [f"{f.title}: {f.followup}".rstrip(": ") for f in findings if f.outcome != "replaced"]

try:
    from .hints import hints_for
except ImportError:  # pragma: no cover
    def hints_for(errors: Any, findings: Any, profile: Any = None, **_kw: Any) -> tuple[list[str], list[str]]:  # type: ignore[misc]
        return [], []

try:
    from .cli import advisories_cli
except ImportError:  # pragma: no cover
    advisories_cli = None  # type: ignore[assignment]

__all__ = ["AdvisoryCatalog", "load_advisories", "evaluate_advisories", "verify_advisories", "followups_for",
           "hints_for", "advisories_cli"]
