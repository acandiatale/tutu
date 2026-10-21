"""Round 9c: `plan.parent_phase: spring` — the restructure step runs inside the spring phase (needs the loop `ws` fixture)."""
from __future__ import annotations

from sbmig.core.models import Phase, StepKind


def test_orchestrator_runs_the_layout_inside_the_spring_phase(cfg, ws):
    """The orchestrator dispatches steps by kind, so a restructure step placed in the spring phase runs there, before
    the spring build-fix loop, and the restructure phase is verify-only."""
    import json

    from sbmig.core.models import CaseType, MigrationPlan, PlanStep, RecipeRef
    from tests.loop.fakes import FakeRestructurer
    from tests.loop.test_orchestrator import make_orch

    rr = [RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21", artifact="org.openrewrite.recipe:rewrite-migrate-java")]
    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_LEGACY, steps=[
        PlanStep(id="jdk21.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.JDK21, title="j", recipes=rr),
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="l", build_goal="compile",
                 max_iterations=2),
        PlanStep(id="spring.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.SPRING, title="s", recipes=rr),
        PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.SPRING, title="r",
                 params={"mode": "layout"}, reason="plan.parent_phase=spring"),
        PlanStep(id="spring.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.SPRING, title="l", build_goal="test-compile",
                 max_iterations=2),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, plan=plan, restructurer=restr, pre=[]).run()
    assert restr.calls == ["layout"] and report.success
    events = [json.loads(l) for l in ws.journal_path.read_text(encoding="utf-8").splitlines()]
    starts = [e["step"] for e in events if e.get("event") == "step_start"]
    assert starts.index("restructure.apply") < starts.index("spring.loop")
    assert [p.phase.value for p in report.phases] == ["jdk21", "spring", "verify"]       # no restructure phase
    spring = next(p for p in report.phases if p.phase == Phase.SPRING)
    assert any("restructure.apply (layout)" in n for n in spring.notes)
