from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType, Phase, ProjectProfile, StepKind
from sbmig.plan import advise_plan, build_plan
from sbmig.plan.planner import artifact_for, ee_javax_packages
from tests.analyze.helpers import (make_boot27, make_boot33, make_pure_java, make_servlet, make_spring_legacy, pom,
                                   write)

ORDER = ["jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.loop", "restructure.apply", "restructure.loop",
         "verify.build"]


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid="spring.rewrite"):
    return [r.id for r in _step(plan, sid).recipes]


def test_spring_boot_plan(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_boot27(tmp_path), cfg)
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ORDER
    assert plan.case_type == CaseType.SPRING_BOOT and plan.profile_fingerprint == profile.fingerprint
    jdk = _step(plan, "jdk21.rewrite")
    assert jdk.kind == StepKind.OPENREWRITE and jdk.phase == Phase.JDK21
    assert jdk.recipes[0].id == "org.openrewrite.java.migrate.UpgradeToJava21"
    assert jdk.recipes[0].artifact == "org.openrewrite.recipe:rewrite-migrate-java"
    loop = _step(plan, "jdk21.loop")
    assert loop.kind == StepKind.BUILD_FIX_LOOP and loop.build_goal == "compile" and loop.max_iterations == 3
    spring = _step(plan, "spring.rewrite")
    assert [r.id for r in spring.recipes] == [
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1",
        "com.company.migrate.SpringBoot41Version",
        "org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
    assert spring.recipes[0].artifact == "org.openrewrite.recipe:rewrite-spring"
    proj = spring.recipes[2]
    assert proj.source == "project" and proj.artifact is None
    assert proj.file and proj.file.endswith("spring-boot-41-version.yml")
    assert spring.recipes[3].artifact == "org.openrewrite.recipe:rewrite-testing-frameworks"
    assert _step(plan, "spring.loop").build_goal == "test-compile"
    r = _step(plan, "restructure.apply")
    assert r.kind == StepKind.RESTRUCTURE and r.params == {"mode": "layout", "starter": "auto"}
    assert _step(plan, "restructure.loop").build_goal == "package"
    v = _step(plan, "verify.build")
    assert v.kind == StepKind.BUILD and v.phase == Phase.VERIFY and v.build_goal == "clean package"
    assert all(s.reason for s in plan.steps)
    assert any("lombok-version-floor" in n for n in plan.notes)


def test_boot_without_junit4(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert "org.openrewrite.java.testing.junit5.JUnit4to5Migration" not in _recipes(plan)


def test_spring_legacy_plan(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert _recipes(plan) == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                              "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
                              "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
                              "org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
    assert _step(plan, "restructure.apply").params["starter"] == "spring-boot-starter-webmvc"
    assert any("@ImportResource" in n for n in plan.notes)


def test_spring_legacy_without_security(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path, security=False), cfg), cfg)
    assert "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0" not in _recipes(plan)


def test_servlet_plan(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_servlet(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    rec = _step(plan, "spring.rewrite").recipes
    assert [r.id for r in rec] == ["com.company.migrate.ServletToJakartaAndBoot"]
    assert rec[0].source == "project" and rec[0].file.endswith("spring-boot-41-version.yml")
    assert _step(plan, "restructure.apply").params["starter"] == "spring-boot-starter-webmvc"
    assert any("web.xml" in n for n in plan.notes)


def test_pure_java_without_ee_omits_spring_rewrite(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_pure_java(tmp_path), cfg), cfg)
    assert _ids(plan) == [i for i in ORDER if i != "spring.rewrite"]
    assert _step(plan, "restructure.apply").params == {"mode": "layout", "starter": "spring-boot-starter"}
    assert any("no jakarta migration" in n for n in plan.notes)
    assert any("spring.rewrite omitted" in n for n in plan.notes)


def test_pure_java_with_ee_and_junit4(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_pure_java(tmp_path, javax_ee=True, junit4=True), cfg), cfg)
    assert _recipes(plan) == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                              "org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
    assert any("javax.xml.bind" in n for n in plan.notes)


def test_ee_javax_filter() -> None:
    assert ee_javax_packages({"javax.swing": 1, "javax.sql": 2, "javax.crypto": 1, "javax.annotation": 1}, False) == []
    assert ee_javax_packages({"javax.annotation": 1}, True) == ["javax.annotation"]
    assert ee_javax_packages({"javax.servlet": 1, "javax.xml.parsers": 1, "javax.xml.bind": 1}, False) == [
        "javax.servlet", "javax.xml.bind"]


def test_intermediate_builds_off_and_disabled_loop(tmp_path: Path, cfg) -> None:
    cfg.plan.intermediate_builds = False
    cfg.loop.phases["restructure"].enabled = False
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert _ids(plan) == ["jdk21.rewrite", "spring.rewrite", "spring.loop", "restructure.apply", "verify.build"]
    assert any("intermediate_builds=false" in n for n in plan.notes)
    assert any("restructure build-fix loop omitted" in n for n in plan.notes)


def test_extra_recipes_from_config(tmp_path: Path, cfg) -> None:
    cfg.openrewrite.recipes.extra = {"spring_boot": ["org.openrewrite.staticanalysis.CommonStaticAnalysis"],
                                     "all": ["com.company.migrate.Custom"]}
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    rec = {r.id: r for r in _step(plan, "spring.rewrite").recipes}
    assert rec["org.openrewrite.staticanalysis.CommonStaticAnalysis"].artifact == "org.openrewrite.recipe:rewrite-static-analysis"
    assert rec["com.company.migrate.Custom"].source == "project" and rec["com.company.migrate.Custom"].file is None
    assert artifact_for("x.y.Z") is None


class FakeKB:
    def __init__(self, ids, files):
        self._ids, self._files = ids, files

    def active_recipe_ids(self):
        return self._ids

    def active_recipe_files(self):
        return self._files


class BrokenKB:
    def active_recipe_ids(self):
        raise FileNotFoundError("kb dir missing")


def test_kb_hook(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_pure_java(tmp_path), cfg)
    kb = FakeKB(["com.company.kb.FixA", "com.company.kb.FixB"], [Path("/kb/a.yml")])
    plan = build_plan(profile, cfg, kb=kb)
    rec = _step(plan, "spring.rewrite").recipes
    assert [(r.id, r.source, r.file) for r in rec] == [("com.company.kb.FixA", "kb", "/kb/a.yml"),
                                                        ("com.company.kb.FixB", "kb", None)]
    # broken KB never breaks planning
    plan = build_plan(profile, cfg, kb=BrokenKB())
    assert "spring.rewrite" not in _ids(plan)
    assert any("unavailable" in n for n in plan.notes)
    # disabled in config
    cfg.kb.use_active_recipes = False
    assert "spring.rewrite" not in _ids(build_plan(profile, cfg, kb=kb))


def test_msa_plan_with_aggregator(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    profile = analyze_path(tmp_path, cfg)
    plan = build_plan(profile, cfg, kb=FakeKB(["com.company.kb.FixA"], ["/kb/a.yml"]))
    per = ["jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.loop"]
    order = [s.name for s in profile.services]
    expected = [f"{i}@{svc}" for svc in order for i in per] + ["restructure.msa_pom", "restructure.loop", "verify.build"]
    assert _ids(plan) == expected
    assert _step(plan, "jdk21.rewrite@shop").target == "shop"
    assert _step(plan, "spring.loop@billing").params == {"service": "billing"}
    shop = _recipes(plan, "spring.rewrite@shop")
    billing = _recipes(plan, "spring.rewrite@billing")
    assert "org.openrewrite.java.testing.junit5.JUnit4to5Migration" in shop           # shop has junit4
    assert "org.openrewrite.java.testing.junit5.JUnit4to5Migration" not in billing    # billing is junit5 only
    assert shop[-1] == billing[-1] == "com.company.kb.FixA"
    msa = _step(plan, "restructure.msa_pom")
    assert msa.kind == StepKind.MSA_POM and msa.params["mode"] == "msa_pom"
    assert _step(plan, "verify.build").target is None
    assert any("msa service shop" in n for n in plan.notes)


def test_msa_plan_without_aggregator(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_servlet(tmp_path / "web")
    write(tmp_path, "web/pom.xml", pom("web", packaging="war",
                                        deps=()))
    profile = analyze_path(tmp_path, cfg)
    assert profile.case_type == CaseType.MSA
    plan = build_plan(profile, cfg)
    verify = [s for s in plan.steps if s.kind == StepKind.BUILD]
    assert sorted(s.id for s in verify) == ["verify.build@shop", "verify.build@web"]
    assert {s.target for s in verify} == {"shop", "web"}
    assert _recipes(plan, "spring.rewrite@web") == ["com.company.migrate.ServletToJakartaAndBoot"]


def test_msa_without_services_falls_back(cfg) -> None:
    profile = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA)
    plan = build_plan(profile, cfg)
    assert "restructure.apply" in _ids(plan)
    assert any("without discovered services" in n for n in plan.notes)


def test_warnings_become_notes_and_advisor(cfg) -> None:
    profile = ProjectProfile(root="/x", name="x", case_type=CaseType.UNKNOWN, warnings=["no java sources found"])
    plan = build_plan(profile, cfg)
    assert "warning: no java sources found" in plan.notes
    assert advise_plan(plan, profile) is plan
    assert advise_plan(plan, profile, llm=object(), cfg=cfg) is plan          # advisor: none
    cfg.plan.advisor = "llm"
    with pytest.raises(NotImplementedError):
        advise_plan(plan, profile, llm=object(), cfg=cfg)
