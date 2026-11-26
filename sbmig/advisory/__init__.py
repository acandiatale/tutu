"""EOL이거나 더 이상 관리되지 않는 라이브러리(springfox, hystrix 등)를 찾아 안내하는 권고 패키지.
권고는 안내만 한다. 빌드를 막거나 실행 결과를 바꾸지 않는다.

Advisories are guide-only: they add no recipe, never block, and never change a run's verdict or exit code.
The report's Handover section shows each finding's outcome to the owners. A missing or empty advisories file
must be safe; when a submodule cannot be imported the fallback definitions below stand in (no advisories).

The orchestrator, the planner, the report and the fleet depend on exactly these names:

    load_advisories(cfg)                       -> AdvisoryCatalog        knowledge/libraries/advisories.yml (+ extra_files)
    evaluate_advisories(profile, cfg, catalog=None) -> list[AdvisoryFinding]
                                               findings for this project (entries set to "off" in
                                               advisories.policy_overrides are skipped)
    verify_advisories(findings, view, cfg)     -> list[AdvisoryFinding]
                                               finalize: re-detect libraries on the final tree (`view` = Workspace or
                                               PathView over the migrated project) and set outcome
                                               (replaced | still-present | unknown); never raises
    followups_for(findings)                    -> list[str]   one actionable sentence per finding, not-replaced first
    hints_for(errors, findings, profile=None)  -> (list[str], list[str])   guidance lines + advisory ids for
                                               the findings whose library / artifacts the cluster's errors mention
                                               (FixContext.advisory_hints, FixAttempt.advisory_ids); never raises
    advisories_cli                             typer sub-app mounted as `sbmig advisories` (list | show | check)

Implementation: sbmig/advisory/{catalog,evaluate,hints,verify,cli}.py; this module re-exports them.
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
except ImportError as _e:  # pragma: no cover - 하위 모듈을 import하지 못한 경우
    # 하위 모듈을 읽지 못하면 권고가 없는 것으로 동작한다. 권고 때문에 실행이 멈추면 안 된다
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
