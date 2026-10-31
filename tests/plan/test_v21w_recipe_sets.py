"""v2.1w (decision 7, "쓰는 경우만" like the Security chain): conditional recipe sets — spring_legacy + `spring-batch` →
SpringBatch5To6Migration, spring_legacy + native `hibernate` → MigrateToHibernate71, servlet_tomcat (and pure_java with
javax.servlet imports) → RemovalsServletJakarta10 in the jakarta bucket; Boot projects get none (the umbrella covers them);
MSA per service."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType
from sbmig.plan import build_plan, recipes_cli, transform_recipes
from sbmig.plan.planner import _Facts, v2_recipe_scope
from tests.analyze.helpers import dep, make_boot27, make_servlet, make_spring_legacy, pom, write, write_java

J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
SERVLET10 = "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"
SEC = [*(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
       "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0"]
BATCH = "org.openrewrite.java.spring.batch.SpringBatch5To6Migration"
HIB = "org.openrewrite.hibernate.MigrateToHibernate71"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
PROPS41 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid="transform.rewrite"):
    return [r.id for r in _step(plan, sid).recipes]


def _legacy(root: Path, *, batch: bool = False, hibernate_dep: bool = False, hibernate_import: bool = False,
            jpa: bool = False, security: bool = False) -> Path:
    """make_spring_legacy + optional Batch 4 / Hibernate 5 / JPA-only additions (pom deps + one source file)."""
    make_spring_legacy(root, security=security)
    deps = [dep("org.springframework", "spring-webmvc", "${spring.version}"),
            dep("org.springframework", "spring-context", "${spring.version}"),
            dep("javax.servlet", "javax.servlet-api", "4.0.1", scope="provided")]
    if security:
        deps.append(dep("org.springframework.security", "spring-security-web", "5.8.0"))
    if batch:
        deps.append(dep("org.springframework.batch", "spring-batch-core", "4.3.10"))
        write_java(root, "", "com.acme.legacy.batch", "BatchConfig",
                   imports=("org.springframework.batch.core.configuration.annotation.JobBuilderFactory",))
    if hibernate_dep:
        deps.append(dep("org.hibernate", "hibernate-core", "5.6.15.Final"))
    if hibernate_import:
        write_java(root, "", "com.acme.legacy.dao", "OrderDao", imports=("org.hibernate.SessionFactory",))
    if jpa:
        deps += [dep("javax.persistence", "javax.persistence-api", "2.2"),
                 dep("org.springframework.data", "spring-data-jpa", "2.7.18")]
        write_java(root, "", "com.acme.legacy.domain", "Order", annotations=("Entity",),
                   imports=("javax.persistence.Entity", "org.springframework.data.jpa.repository.JpaRepository"))
    write(root, "pom.xml", pom("legacy-spring", packaging="war", props={"spring.version": "5.3.30",
                                                                         "maven.compiler.source": "1.8"},
                               deps=tuple(deps)))
    return root


def _plan(root: Path, cfg):
    profile = analyze_path(root, cfg)
    return profile, build_plan(profile, cfg)


# ------------------------------------------------------------------------------------------------- spring_legacy
def test_spring_legacy_with_spring_batch_gets_the_batch_set(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(_legacy(tmp_path, batch=True), cfg)
    assert profile.case_type == CaseType.SPRING_LEGACY and profile.has_library("spring-batch")
    assert _recipes(plan) == [J21, JAKARTA, FW7, BATCH]
    tr = _step(plan, "transform.rewrite")
    assert tr.title == "OpenRewrite in one run: Java 21 + jakarta + Spring Framework 7 + Spring Batch 6"
    assert tr.recipes[-1].artifact == "org.openrewrite.recipe:rewrite-spring"
    assert ("transform: spring_legacy → jakarta set + Spring Framework 7 set + Spring Batch 6 set "
            "SpringBatch5To6Migration (spring-batch detected)") in plan.notes


def test_spring_legacy_without_spring_batch_or_hibernate_gets_neither(tmp_path: Path, cfg) -> None:
    _, plan = _plan(_legacy(tmp_path), cfg)
    assert _recipes(plan) == [J21, JAKARTA, FW7]
    assert not any("spring-batch" in n or "hibernate" in n for n in plan.notes if n.startswith("transform"))


def test_spring_legacy_with_native_hibernate_dependency_gets_the_hibernate_set(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(_legacy(tmp_path, hibernate_dep=True), cfg)
    assert profile.has_library("hibernate")
    assert _recipes(plan) == [J21, JAKARTA, FW7, HIB]
    assert _step(plan, "transform.rewrite").recipes[-1].artifact == "org.openrewrite.recipe:rewrite-hibernate"
    assert any("+ Hibernate 7.1 set MigrateToHibernate71 (hibernate detected)" in n for n in plan.notes)


def test_spring_legacy_with_hibernate_api_imports_only_gets_the_hibernate_set(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(_legacy(tmp_path, hibernate_import=True), cfg)
    assert profile.has_library("hibernate")
    assert HIB in _recipes(plan)


def test_spring_legacy_jpa_only_gets_no_hibernate_set(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(_legacy(tmp_path, jpa=True), cfg)
    assert profile.has_library("jpa") and not profile.has_library("hibernate")
    assert _recipes(plan) == [J21, JAKARTA, FW7]


def test_run_order_security_then_batch_then_hibernate(tmp_path: Path, cfg) -> None:
    _, plan = _plan(_legacy(tmp_path, batch=True, hibernate_dep=True, security=True), cfg)
    assert _recipes(plan) == [J21, JAKARTA, FW7, *SEC, BATCH, HIB]
    assert _step(plan, "transform.rewrite").title == (
        "OpenRewrite in one run: Java 21 + jakarta + Spring Framework 7 + Spring Security 7 + Spring Batch 6 "
        "+ Hibernate 7.1")
    note = next(n for n in plan.notes if n.startswith("transform: spring_legacy"))
    assert note.index("(spring-security detected)") < note.index("(spring-batch detected)") \
        < note.index("(hibernate detected)")


def test_empty_sets_are_noted_not_planned(tmp_path: Path, cfg) -> None:
    rs = cfg.openrewrite.recipes
    rs.spring_batch_6, rs.hibernate_7 = [], []
    _, plan = _plan(_legacy(tmp_path, batch=True, hibernate_dep=True), cfg)
    assert _recipes(plan) == [J21, JAKARTA, FW7]
    note = next(n for n in plan.notes if n.startswith("transform: spring_legacy"))
    assert "; spring-batch detected, the Spring Batch 6 set is empty" in note
    assert "; hibernate detected, the Hibernate 7.1 set is empty" in note


def test_spring_legacy_gets_the_servlet_removals_only_without_the_framework_set(tmp_path: Path, cfg) -> None:
    _, plan = _plan(_legacy(tmp_path / "a"), cfg)
    assert SERVLET10 not in _recipes(plan)                     # UpgradeSpringFramework_7_0 includes it (catalog covers)
    cfg.openrewrite.recipes.spring_framework_7 = []
    _, plan = _plan(_legacy(tmp_path / "b"), cfg)
    assert _recipes(plan) == [J21, JAKARTA, SERVLET10]
    assert any("+ Servlet 6 removals RemovalsServletJakarta10 (the Spring Framework 7 set is empty)" in n
               for n in plan.notes)


# ------------------------------------------------------------------------------------------------- other cases
def test_servlet_tomcat_gets_the_servlet_removals_after_jakarta(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(make_servlet(tmp_path), cfg)
    assert profile.case_type == CaseType.SERVLET_TOMCAT
    assert _recipes(plan) == [J21, JAKARTA, SERVLET10]
    tr = _step(plan, "transform.rewrite")
    assert tr.title == "OpenRewrite in one run: Java 21 + jakarta"                 # same bucket as the rename
    assert tr.recipes[-1].artifact == "org.openrewrite.recipe:rewrite-migrate-java"
    cfg.openrewrite.recipes.servlet_jakarta_10 = []
    _, plan = _plan(make_servlet(tmp_path / "off"), cfg)
    assert _recipes(plan) == [J21, JAKARTA]
    assert "transform: servlet_tomcat → jakarta set (javax → jakarta)" in plan.notes


def test_pure_java_gets_the_servlet_removals_only_with_javax_servlet_imports(cfg) -> None:
    notes: list[str] = []
    ids = [r.id for r in transform_recipes(_Facts(CaseType.PURE_JAVA, set(), {"javax.servlet.http": 2}), cfg, notes)]
    assert ids == [J21, JAKARTA, SERVLET10]
    assert notes == [("transform: pure_java with Java EE javax imports (javax.servlet.http) → jakarta set + Servlet 6 "
                      "removals RemovalsServletJakarta10 (javax.servlet imports)")]
    ids = [r.id for r in transform_recipes(_Facts(CaseType.PURE_JAVA, set(), {"javax.xml.bind": 1}), cfg, [])]
    assert ids == [J21, JAKARTA]


def test_spring_boot_gets_none_of_the_v21w_sets(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path)
    write(tmp_path, "pom.xml", (tmp_path / "pom.xml").read_text(encoding="utf-8").replace(
        "</dependencies>", dep("org.springframework.boot", "spring-boot-starter-batch")
        + dep("org.hibernate", "hibernate-core") + "</dependencies>"))
    profile, plan = _plan(tmp_path, cfg)
    assert profile.case_type == CaseType.SPRING_BOOT
    assert profile.has_library("spring-batch") and profile.has_library("hibernate")
    assert _recipes(plan) == [J21, BOOT4, PROPS41]                                # the umbrella covers all three


def test_facts_level_rules_per_case(cfg) -> None:
    libs = {"spring-batch", "hibernate", "spring-security"}
    for case in (CaseType.SERVLET_TOMCAT, CaseType.PURE_JAVA, CaseType.SPRING_BOOT, CaseType.UNKNOWN):
        ids = [r.id for r in transform_recipes(_Facts(case, libs, {}), cfg, [])]
        assert BATCH not in ids and HIB not in ids, case                         # spring_legacy only
    ids = [r.id for r in transform_recipes(_Facts(CaseType.SPRING_LEGACY, libs, {}), cfg, [])]
    assert ids == [J21, JAKARTA, FW7, *SEC, BATCH, HIB]


# ------------------------------------------------------------------------------------------------- MSA
def _msa(root: Path) -> Path:
    """Two deployables, no root aggregator: a Boot service with Spring Batch and a non-Boot Spring war with Batch."""
    make_boot27(root, module="shop", artifact="shop")
    shop = root / "shop" / "pom.xml"
    shop.write_text(shop.read_text(encoding="utf-8").replace(
        "</dependencies>", dep("org.springframework.boot", "spring-boot-starter-batch") + "</dependencies>"),
        encoding="utf-8")
    _legacy(root / "ops", batch=True)
    return root


def test_msa_per_service_sets(tmp_path: Path, cfg) -> None:
    profile, plan = _plan(_msa(tmp_path), cfg)
    assert profile.case_type == CaseType.MSA
    cases = {s.name: s.case_type for s in profile.services}
    ops = next(n for n, c in cases.items() if c == CaseType.SPRING_LEGACY)
    assert _recipes(plan, f"transform.rewrite@{ops}") == [J21, JAKARTA, FW7, BATCH]
    assert _recipes(plan, "transform.rewrite@shop") == [J21, BOOT4, PROPS41]
    assert any(n.startswith(f"transform [{ops}]: spring_legacy") and "(spring-batch detected)" in n for n in plan.notes)


# ------------------------------------------------------------------------------------------------- scope / CLI
def test_v2_recipe_scope_and_sets_cli(cfg) -> None:
    assert {BATCH, HIB, SERVLET10} <= v2_recipe_scope(cfg)
    assert {BATCH, HIB, SERVLET10} <= v2_recipe_scope()
    res = CliRunner().invoke(recipes_cli, ["sets", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["sets"]["spring_batch_6"] == [BATCH] and data["sets"]["hibernate_7"] == [HIB]
    assert data["sets"]["servlet_jakarta_10"] == [SERVLET10]
    per = data["per_case"]
    assert per["servlet_tomcat"]["transform"] == [J21, JAKARTA, SERVLET10]
    assert BATCH in per["spring_legacy"]["transform_conditional"] and HIB in per["spring_legacy"]["transform_conditional"]
    assert SERVLET10 in per["pure_java"]["transform_conditional"]
    assert per["spring_boot"]["transform_conditional"] == []
