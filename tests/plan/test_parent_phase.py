"""Round 9c: `plan.parent_phase: spring` applies the in-house parents / layout right after spring.rewrite, before the
spring build-fix loop (the company's call: fix against the company parent chain, fail fast on a missing parent)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import Phase, StepKind
from sbmig.plan import build_plan
from tests.analyze.helpers import make_boot27, make_spring_legacy

REPO = Path(__file__).resolve().parents[2]


def _ids(plan):
    return [s.id for s in plan.steps]


def _restructure_steps(plan):
    return [s for s in plan.steps if s.kind in (StepKind.RESTRUCTURE, StepKind.MSA_POM)
            and str(s.params.get("mode")) != "optional_bump"]


def test_default_keeps_the_restructure_phase(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    [st] = _restructure_steps(plan)
    assert st.phase == Phase.RESTRUCTURE and "restructure.loop" in _ids(plan)


def test_spring_moves_layout_before_the_spring_loop(tmp_path: Path, cfg) -> None:
    cfg.plan.parent_phase = "spring"
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    ids = _ids(plan)
    [st] = _restructure_steps(plan)
    assert st.id == "restructure.apply" and st.phase == Phase.SPRING
    assert "restructure.loop" not in ids and ids[-1] == "verify.build"
    assert ids.index("spring.rewrite") < ids.index("restructure.apply") < ids.index("spring.loop")
    if "spring.optional_bump" in ids:
        assert ids.index("spring.optional_bump") < ids.index("restructure.apply")
    assert "plan.parent_phase=spring" in st.reason
    assert any("applied in the spring phase right after spring.rewrite" in n for n in plan.notes)


def test_spring_moves_boot_pom_too(tmp_path: Path, cfg) -> None:
    cfg.plan.parent_phase = "spring"
    cfg.layout.boot_as_is = "in-place"
    plan = build_plan(analyze_path(make_boot27(tmp_path), cfg), cfg)
    ids = _ids(plan)
    [st] = _restructure_steps(plan)
    assert st.id == "restructure.boot_pom" and st.phase == Phase.SPRING
    assert ids.index("restructure.boot_pom") < ids.index("spring.loop") and "restructure.loop" not in ids


def test_spring_moves_msa_pom_before_the_first_service_loop(cfg) -> None:
    cfg.plan.parent_phase = "spring"
    plan = build_plan(analyze_path(REPO / "fixtures" / "projects" / "msa", cfg), cfg)
    ids = _ids(plan)
    [st] = _restructure_steps(plan)
    assert st.id == "restructure.msa_pom" and st.phase == Phase.SPRING
    first_loop = next(i for i, s in enumerate(plan.steps) if s.kind == StepKind.BUILD_FIX_LOOP and s.phase == Phase.SPRING)
    assert ids.index("restructure.msa_pom") < first_loop and "restructure.loop" not in ids
    assert [s for s in plan.steps if s.phase == Phase.RESTRUCTURE] == []       # only the verify steps remain after spring

