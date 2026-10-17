"""Planner edge cases: spring.optional_bump, MSA library roots / top-aggregator, pom-only (S3), Gradle (S4)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType, Phase, ProjectProfile, StepKind
from sbmig.plan import build_plan
from tests.analyze.helpers import BOOT_PLUGIN, dep, make_boot33, pom, write, write_java
from tests.analyze.test_edges import make_msa_with_common

BOOT_UMBRELLA = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


# ------------------------------------------------------------------------------------------------- optional bump
def test_optional_bump_step_right_after_spring_rewrite(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    ids = _ids(plan)
    i = ids.index("spring.optional_bump")
    assert ids[i - 1] == "spring.rewrite" and ids[i + 1] == "spring.loop"
    s = _step(plan, "spring.optional_bump")
    assert s.kind == StepKind.RESTRUCTURE and s.phase == Phase.SPRING and s.optional
    assert s.params == {"mode": "optional_bump"} and s.target is None and s.reason
    assert any("spring.optional_bump" in n for n in plan.notes)


def test_optional_bump_phase_restructure_keeps_old_order(tmp_path: Path, cfg) -> None:
    cfg.plan.optional_bump_phase = "restructure"
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert "spring.optional_bump" not in _ids(plan)
    assert _ids(plan)[2:4] == ["spring.rewrite", "spring.loop"]
    assert any("optional_bump_phase=restructure" in n for n in plan.notes)


def test_optional_bump_omitted_without_configured_artifacts(tmp_path: Path, cfg) -> None:
    cfg.artifacts.optional = []
    cfg.artifacts.remove_when_present = []
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert "spring.optional_bump" not in _ids(plan)
    assert any("none configured" in n for n in plan.notes)


def test_optional_bump_without_spring_rewrite_still_before_loop(tmp_path: Path, cfg) -> None:
    from tests.analyze.helpers import make_pure_java
    plan = build_plan(analyze_path(make_pure_java(tmp_path), cfg), cfg)
    ids = _ids(plan)
    assert "spring.rewrite" not in ids
    assert ids.index("jdk21.loop") < ids.index("spring.optional_bump") < ids.index("spring.loop")


# ------------------------------------------------------------------------------------------------- MSA S1
def test_msa_library_root_planned_first(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_msa_with_common(tmp_path, boot_dep_in_common=True), cfg)
    plan = build_plan(profile, cfg)
    ids = _ids(plan)
    per = ["jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.optional_bump", "spring.loop"]
    svcs = [s.name for s in profile.services if s.name != "common"]
    assert ids == ([f"{i}@common" for i in per] + [f"{i}@{s}" for s in svcs for i in per]
                   + ["restructure.msa_pom", "restructure.loop", "verify.build"])
    lib_recipes = [r.id for r in _step(plan, "spring.rewrite@common").recipes]
    assert BOOT_UMBRELLA not in lib_recipes
    assert "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta" in lib_recipes
    assert "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0" in lib_recipes
    assert BOOT_UMBRELLA in [r.id for r in _step(plan, "spring.rewrite@shop").recipes]
    ob = _step(plan, "spring.optional_bump@common")
    assert ob.target == "common" and ob.params == {"mode": "optional_bump", "service": "common", "target": "common"}
    msa = _step(plan, "restructure.msa_pom")
    assert msa.params["library_roots"] == ["common/pom.xml"]
    assert sorted(msa.params["services"]) == ["billing", "shop"]
    assert any("no mandatory dependencies" in n for n in plan.notes)
    assert any("msa library root common" in n for n in plan.notes)


def test_msa_libraries_in_dependency_order_and_per_target_verify(tmp_path: Path, cfg) -> None:
    # no root aggregator → per-target verify incl. libraries; lib "alpha" depends on lib "zeta" → zeta first
    for svc in ("shop", "billing"):
        make_boot33(tmp_path, module=svc, artifact=svc)
    write(tmp_path, "alpha/pom.xml", pom("alpha", deps=(dep("com.acme", "zeta", "1.0.0"),)))
    write_java(tmp_path, "alpha", "com.acme.alpha", "A")
    write(tmp_path, "zeta/pom.xml", pom("zeta"))
    write_java(tmp_path, "zeta", "com.acme.zeta", "Z")
    profile = analyze_path(tmp_path, cfg)
    plan = build_plan(profile, cfg)
    ids = _ids(plan)
    assert ids.index("jdk21.rewrite@zeta") < ids.index("jdk21.rewrite@alpha") < ids.index("jdk21.rewrite@shop")
    verify = sorted(s.id for s in plan.steps if s.kind == StepKind.BUILD)
    assert verify == ["verify.build@alpha", "verify.build@billing", "verify.build@shop", "verify.build@zeta"]
    assert any("and library root" in n for n in plan.notes)


def test_msa_top_aggregator_single_root_rewrite(tmp_path: Path, cfg) -> None:
    cfg.layout.msa_parent_policy = "top-aggregator"
    profile = analyze_path(make_msa_with_common(tmp_path), cfg)
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ["jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.optional_bump", "spring.loop",
                          "restructure.msa_pom", "restructure.loop", "verify.build"]
    rw = _step(plan, "spring.rewrite")
    assert rw.target is None
    ids = [r.id for r in rw.recipes]
    assert BOOT_UMBRELLA in ids and "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0" in ids
    assert len(ids) == len(set(ids))
    assert any("one rewrite + loop at the repo root" in n for n in plan.notes)


def test_msa_top_aggregator_without_covering_aggregator_stays_per_service(tmp_path: Path, cfg) -> None:
    cfg.layout.msa_parent_policy = "top-aggregator"
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop",)))     # billing not aggregated
    make_boot33(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    assert "jdk21.rewrite@shop" in _ids(plan) and "jdk21.rewrite@billing" in _ids(plan)


def test_msa_without_sources_has_no_rewrite_steps(cfg) -> None:
    from sbmig.core.models import PomInfo, ServiceInfo
    prof = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA, root_pom=PomInfo(
        path="pom.xml", artifact_id="x", packaging="pom", modules=["a", "b"], is_aggregator=True),
        services=[ServiceInfo(name="a", root_pom="a/pom.xml", case_type=CaseType.SPRING_BOOT),
                  ServiceInfo(name="b", root_pom="b/pom.xml", case_type=CaseType.SPRING_BOOT)])
    plan = build_plan(prof, cfg)
    assert _ids(plan) == ["restructure.msa_pom", "restructure.loop", "verify.build"]


# ------------------------------------------------------------------------------------------------- S3 / S4
def test_pom_only_project_plan(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("bom", packaging="pom", managed=(dep("com.google.guava", "guava", "31.0-jre"),),
                                   plugins=(BOOT_PLUGIN,)))
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    assert _ids(plan) == ["restructure.pom_only", "restructure.loop", "verify.build"]
    s = _step(plan, "restructure.pom_only")
    assert s.kind == StepKind.MSA_POM and s.params["mode"] == "msa_pom" and s.params["pom_only"] is True
    assert not any(st.phase in (Phase.JDK21, Phase.SPRING) for st in plan.steps)
    assert any("no java sources" in n for n in plan.notes)


def test_gradle_project_empty_plan(tmp_path: Path, cfg) -> None:
    write(tmp_path, "build.gradle", "plugins {}")
    write_java(tmp_path, "", "com.g", "G")
    profile = analyze_path(tmp_path, cfg)
    plan = build_plan(profile, cfg)
    assert plan.steps == []
    assert any("build tool 'gradle' is not supported" in n and "Gradle build detected" in n for n in plan.notes)
