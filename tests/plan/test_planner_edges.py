"""v2 planner edge cases: MSA library roots / dependency order / top-aggregator, pom-only (S3), Gradle (S4)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType, Phase, ProjectProfile, StepKind
from sbmig.plan import build_plan
from tests.analyze.helpers import BOOT_PLUGIN, dep, make_boot33, pom, write, write_java
from tests.analyze.test_edges import make_msa_with_common

J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
BOOT_UMBRELLA = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
PROPS41 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid):
    return [r.id for r in _step(plan, sid).recipes]


# ------------------------------------------------------------------------------------------------- MSA S1
def test_msa_library_root_planned_first(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_msa_with_common(tmp_path, boot_dep_in_common=True), cfg)
    plan = build_plan(profile, cfg)
    svcs = [s.name for s in profile.services if s.name != "common"]
    assert _ids(plan) == (["transform.rewrite@common"] + [f"transform.rewrite@{s}" for s in svcs]
                          + ["restructure.msa_pom", "restructure.review", "verify.build"])
    lib = _step(plan, "transform.rewrite@common")
    assert lib.kind == StepKind.OPENREWRITE and lib.phase == Phase.TRANSFORM
    assert lib.target == "common" and lib.params == {"service": "common"}
    assert _recipes(plan, "transform.rewrite@common") == [J21, JAKARTA]     # never the Boot set
    assert _recipes(plan, "transform.rewrite@shop") == [J21, BOOT_UMBRELLA, PROPS41]
    msa = _step(plan, "restructure.msa_pom")
    assert msa.params["library_roots"] == ["common/pom.xml"]
    assert sorted(msa.params["services"]) == ["billing", "shop"]
    assert msa.params["parent_policy"] == cfg.layout.msa_parent_policy and "boot_modules" in msa.params
    assert any("no mandatory dependencies" in n for n in plan.notes)
    assert any(n.startswith("msa library root common") and "never the Boot set" in n for n in plan.notes)
    assert not any(s.id.startswith("spring.optional_bump") for s in plan.steps)


def test_msa_library_root_gets_the_opt_in_framework_set(tmp_path: Path, cfg) -> None:
    cfg.openrewrite.recipes.spring_framework_7 = [FW7]
    plan = build_plan(analyze_path(make_msa_with_common(tmp_path, boot_dep_in_common=True), cfg), cfg)
    assert _recipes(plan, "transform.rewrite@common") == [J21, JAKARTA, FW7]
    assert FW7 not in _recipes(plan, "transform.rewrite@shop")


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
    assert ids.index("transform.rewrite@zeta") < ids.index("transform.rewrite@alpha") < ids.index(
        "transform.rewrite@shop")
    verify = sorted(s.id for s in plan.steps if s.kind == StepKind.BUILD)
    assert verify == ["verify.build@alpha", "verify.build@billing", "verify.build@shop", "verify.build@zeta"]
    assert all(_step(plan, v).params == {"service": v.split("@")[1]} for v in verify)
    assert ids.index("restructure.review") < min(ids.index(v) for v in verify)
    assert any("and library root" in n for n in plan.notes)


def test_msa_top_aggregator_single_root_rewrite(tmp_path: Path, cfg) -> None:
    cfg.layout.msa_parent_policy = "top-aggregator"
    profile = analyze_path(make_msa_with_common(tmp_path), cfg)
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ["transform.rewrite", "restructure.msa_pom", "restructure.review", "verify.build"]
    rw = _step(plan, "transform.rewrite")
    assert rw.target is None and rw.params == {} and rw.phase == Phase.TRANSFORM
    ids = [r.id for r in rw.recipes]
    assert ids == [J21, JAKARTA, BOOT_UMBRELLA, PROPS41]               # union, in set order (jdk21, jakarta, boot)
    assert any("one transform.rewrite at the repo root" in n for n in plan.notes)
    assert "msa (all services)" in rw.reason


def test_msa_top_aggregator_without_covering_aggregator_stays_per_service(tmp_path: Path, cfg) -> None:
    cfg.layout.msa_parent_policy = "top-aggregator"
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop",)))     # billing not aggregated
    make_boot33(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    assert "transform.rewrite@shop" in _ids(plan) and "transform.rewrite@billing" in _ids(plan)
    assert "transform.rewrite" not in _ids(plan)


def test_msa_without_sources_has_no_transform_steps(cfg) -> None:
    from sbmig.core.models import PomInfo, ServiceInfo
    prof = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA, root_pom=PomInfo(
        path="pom.xml", artifact_id="x", packaging="pom", modules=["a", "b"], is_aggregator=True),
        services=[ServiceInfo(name="a", root_pom="a/pom.xml", case_type=CaseType.SPRING_BOOT),
                  ServiceInfo(name="b", root_pom="b/pom.xml", case_type=CaseType.SPRING_BOOT)])
    plan = build_plan(prof, cfg)
    assert _ids(plan) == ["restructure.msa_pom", "restructure.review", "verify.build"]
    assert any(n.startswith("msa: no java sources → no transform step") for n in plan.notes)


def test_msa_review_disabled(tmp_path: Path, cfg) -> None:
    cfg.review.enabled = False
    plan = build_plan(analyze_path(make_msa_with_common(tmp_path), cfg), cfg)
    assert "restructure.review" not in _ids(plan) and _ids(plan)[-2:] == ["restructure.msa_pom", "verify.build"]


# ------------------------------------------------------------------------------------------------- S3 / S4
def test_pom_only_project_plan(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("bom", packaging="pom", managed=(dep("com.google.guava", "guava", "31.0-jre"),),
                                   plugins=(BOOT_PLUGIN,)))
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    assert _ids(plan) == ["restructure.pom_only", "restructure.review", "verify.build"]
    s = _step(plan, "restructure.pom_only")
    assert s.kind == StepKind.MSA_POM and s.params["mode"] == "msa_pom" and s.params["pom_only"] is True
    assert _step(plan, "restructure.review").params == {"restructure_step": "restructure.pom_only"}
    assert not any(st.phase in (Phase.TRANSFORM, Phase.JDK21, Phase.SPRING) for st in plan.steps)
    assert any(n.startswith("no java sources → no transform step") for n in plan.notes)


def test_gradle_project_empty_plan(tmp_path: Path, cfg) -> None:
    write(tmp_path, "build.gradle", "plugins {}")
    write_java(tmp_path, "", "com.g", "G")
    profile = analyze_path(tmp_path, cfg)
    plan = build_plan(profile, cfg)
    assert plan.steps == []
    assert any("build tool 'gradle' is not supported" in n and "Gradle build detected" in n for n in plan.notes)
    assert not any(n.startswith(("pre-emptive", "transform", "review")) for n in plan.notes)
