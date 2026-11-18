"""v2.1z-at: the recipe set `hystrix_resilience4j` — planned when the analyzer found `hystrix`, for every case whose
plan has the transform step, in the one OpenRewrite run right after the jakarta bucket and before the Framework /
Security / Boot sets, with one plan note; `[]` switches it off; never in the jdk-jakarta scope unless listed."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType
from sbmig.plan import build_plan, transform_recipes
from sbmig.plan.planner import _Facts, _reduced_buckets, _flatten, v2_recipe_scope
from tests.analyze.helpers import dep, make_boot27, make_spring_legacy, write

HYSTRIX = "com.company.migrate.HystrixToResilience4j"
J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
SERVLET10 = "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"
SEC = [*(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
       "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0"]
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
PROPS41 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"
SC = "org.springframework.cloud"
NOTE = ("transform: hystrix detected → Hystrix → Resilience4j set HystrixToResilience4j before the Boot / Framework "
        "sets (no version written: the restructure's version rules decide the new starter)")


def _step(plan, sid="transform.rewrite"):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid="transform.rewrite"):
    return [r.id for r in _step(plan, sid).recipes]


def _boot_with_hystrix(root: Path) -> Path:
    make_boot27(root)
    pom = root / "pom.xml"
    write(root, "pom.xml", pom.read_text(encoding="utf-8").replace(
        "</dependencies>", dep(SC, "spring-cloud-starter-netflix-hystrix", "2.2.10.RELEASE") + "</dependencies>"))
    return root


def test_boot_project_with_hystrix_gets_the_set_before_the_boot_set(tmp_path: Path, cfg) -> None:
    profile = analyze_path(_boot_with_hystrix(tmp_path), cfg)
    assert profile.case_type == CaseType.SPRING_BOOT and profile.has_library("hystrix")
    plan = build_plan(profile, cfg)
    assert _recipes(plan) == [J21, HYSTRIX, BOOT4, PROPS41]
    tr = _step(plan)
    assert tr.title == "OpenRewrite in one run: Java 21 + Hystrix → Resilience4j + Spring Boot 4.1"
    hy = next(r for r in tr.recipes if r.id == HYSTRIX)
    assert hy.artifact is None                                     # declarative file, core building blocks only
    assert plan.notes.count(NOTE) == 1
    assert [s.id for s in plan.steps].count("transform.rewrite") == 1    # the ONE OpenRewrite run


def test_without_hystrix_nothing_is_planned(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path)
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    assert HYSTRIX not in _recipes(plan)
    assert not any("hystrix" in n for n in plan.notes)


def test_spring_legacy_order_is_jakarta_then_hystrix_then_framework_and_security(cfg) -> None:
    ids = [r.id for r in transform_recipes(_Facts(CaseType.SPRING_LEGACY, {"hystrix", "spring-security"}, {}), cfg, [])]
    assert ids == [J21, JAKARTA, HYSTRIX, FW7, *SEC]


def test_every_case_with_a_transform_gets_it(cfg) -> None:
    for case in (CaseType.PURE_JAVA, CaseType.SERVLET_TOMCAT, CaseType.SPRING_LEGACY, CaseType.SPRING_BOOT,
                 CaseType.UNKNOWN):
        notes: list[str] = []
        ids = [r.id for r in transform_recipes(_Facts(case, {"hystrix"}, {}), cfg, notes)]
        assert HYSTRIX in ids, case
        assert notes.count(NOTE) == 1, case
        later = [i for i in ids if i in (FW7, BOOT4, PROPS41)]
        assert all(ids.index(HYSTRIX) < ids.index(i) for i in later), case
    ids = [r.id for r in transform_recipes(_Facts(CaseType.SERVLET_TOMCAT, {"hystrix"}, {}), cfg, [])]
    assert ids == [J21, JAKARTA, SERVLET10, HYSTRIX]


def test_an_empty_set_switches_it_off_with_a_note(tmp_path: Path, cfg) -> None:
    cfg.openrewrite.recipes.hystrix_resilience4j = []
    plan = build_plan(analyze_path(_boot_with_hystrix(tmp_path), cfg), cfg)
    assert _recipes(plan) == [J21, BOOT4, PROPS41]
    assert ("transform: hystrix detected, the hystrix_resilience4j set is empty → Hystrix stays at its as-is version"
            in plan.notes)


def test_msa_services_get_it_per_service(tmp_path: Path, cfg) -> None:
    _boot_with_hystrix(tmp_path / "orders")
    make_boot27(tmp_path / "shop", module="shop", artifact="shop")
    profile = analyze_path(tmp_path, cfg)
    if profile.case_type != CaseType.MSA or len(profile.services) < 2:
        # the two-service tree must be an MSA for this test to say anything
        raise AssertionError(f"expected an MSA, got {profile.case_type} {[s.name for s in profile.services]}")
    plan = build_plan(profile, cfg)
    with_h = [s for s in plan.steps if s.id.startswith("transform.rewrite") and HYSTRIX in [r.id for r in s.recipes]]
    without = [s for s in plan.steps if s.id.startswith("transform.rewrite") and HYSTRIX not in [r.id for r in s.recipes]]
    assert len(with_h) == 1 and len(without) == 1
    assert [r.id for r in with_h[0].recipes] == [J21, HYSTRIX, BOOT4, PROPS41]


def test_jdk_jakarta_scope_plans_it_only_when_listed(cfg) -> None:
    facts = _Facts(CaseType.SPRING_LEGACY, {"hystrix"}, {})
    assert HYSTRIX not in _flatten(_reduced_buckets(facts, cfg, []))
    cfg.framework.recipe_sets = ["jdk21", "jakarta", "hystrix_resilience4j"]
    assert _flatten(_reduced_buckets(facts, cfg, [])) == [J21, JAKARTA, HYSTRIX]


def test_the_set_is_in_the_v2_recipe_scope(cfg) -> None:
    assert HYSTRIX in v2_recipe_scope(cfg) and HYSTRIX in v2_recipe_scope()


def test_spring_legacy_project_with_hystrix(tmp_path: Path, cfg) -> None:
    make_spring_legacy(tmp_path, security=False)
    pom = tmp_path / "pom.xml"
    write(tmp_path, "pom.xml", pom.read_text(encoding="utf-8").replace(
        "<dependencies>", "<dependencies>" + dep("com.netflix.hystrix", "hystrix-javanica", "1.5.18"), 1))
    profile = analyze_path(tmp_path, cfg)
    assert profile.case_type == CaseType.SPRING_LEGACY and profile.has_library("hystrix")
    assert _recipes(build_plan(profile, cfg)) == [J21, JAKARTA, HYSTRIX, FW7]
