"""Round 10: library-conditional recipes (knowledge/recipes/library_recipes.yml) for plans without the Boot umbrella,
the Spring Security 6.0 … 7.0 chain, Servlet 6 removals and the Spring 7 / Boot 4 project recipes."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType
from sbmig.plan import build_plan
from sbmig.plan.catalog import config_recipe_sets, default_catalog
from sbmig.plan.library_recipes import library_recipe_rules, library_recipe_sets, rules_for
from tests.analyze.helpers import dep, make_boot27, make_servlet, pom, write, write_java

BATCH6 = "org.openrewrite.java.spring.batch.SpringBatch5To6Migration"
HIBERNATE71 = "org.openrewrite.hibernate.MigrateToHibernate71"
THYMELEAF = "org.openrewrite.java.spring.boot3.MigrateThymeleafDependencies"
SEC = [*(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
       "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0"]


def _recipes(plan, sid="spring.rewrite"):
    return [r.id for r in next(s for s in plan.steps if s.id == sid).recipes]


def _spring5_batch_security(root: Path, thymeleaf: str = "thymeleaf-spring5") -> Path:
    write(root, "pom.xml", pom("legacy-batch", packaging="war", props={"spring.version": "5.3.30"}, deps=(
        dep("org.springframework", "spring-webmvc", "${spring.version}"),
        dep("org.springframework.security", "spring-security-config", "5.8.0"),
        dep("org.springframework.batch", "spring-batch-core", "4.3.10"),
        dep("org.hibernate", "hibernate-core", "5.6.15.Final"),
        dep("org.thymeleaf", thymeleaf, "3.0.15.RELEASE"),
        dep("javax.servlet", "javax.servlet-api", "4.0.1", scope="provided"))))
    write_java(root, "", "com.acme.batch", "JobConfig", imports=("org.springframework.batch.core.Job",
                                                                   "javax.persistence.EntityManager"))
    return root


def test_table_loads_and_names_sets():
    rules = library_recipe_rules()
    assert [r.library for r in rules] == ["spring-batch", "hibernate", "thymeleaf"]
    assert library_recipe_sets() == {"library.spring-batch": [BATCH6], "library.hibernate": [HIBERNATE71],
                                     "library.thymeleaf": [THYMELEAF]}
    assert [r.library for r in rules_for(CaseType.SPRING_LEGACY, {"spring-batch", "thymeleaf"})] == \
        ["spring-batch", "thymeleaf"]
    assert rules_for(CaseType.SPRING_BOOT, {"spring-batch", "hibernate", "thymeleaf"}) == []    # the umbrella runs them
    assert [r.library for r in rules_for(CaseType.PURE_JAVA, {"hibernate", "spring-batch"})] == ["hibernate"]


def test_catalog_check_covers_the_library_sets(cfg):
    sets = config_recipe_sets(cfg)
    assert sets["library.hibernate"] == [HIBERNATE71]
    cat = default_catalog(cfg)
    assert cat.check(cfg) == []
    e = cat.get(HIBERNATE71)
    assert e.artifact == "org.openrewrite.recipe:rewrite-hibernate" and e.provided_by == "org.openrewrite.recipe:rewrite-spring"
    assert "library.hibernate" in e.sets
    # every library recipe is already inside the Boot umbrella (catalog covers, verified in rewrite-spring 6.37.0)
    covered = cat.covered_by(["org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"])
    assert {BATCH6, HIBERNATE71, THYMELEAF, *SEC[:-1]} <= set(covered)


def test_spring_legacy_plan_gets_security_chain_and_library_recipes(tmp_path, cfg):
    profile = analyze_path(_spring5_batch_security(tmp_path), cfg)
    assert profile.case_type == CaseType.SPRING_LEGACY
    plan = build_plan(profile, cfg)
    rec = _recipes(plan)
    assert rec[:5] == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                       "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
                       "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
                       "com.company.migrate.Spring7RemovedApis", "com.company.migrate.Jackson3SpringConverters"]
    assert rec[5:12] == SEC                                                 # order OpenRewrite's Boot chain uses
    assert rec[12:15] == [BATCH6, HIBERNATE71, THYMELEAF]                   # after Framework / Security, table order
    assert any("spring-batch detected → SpringBatch5To6Migration" in n for n in plan.notes)
    assert not any("thymeleaf-spring4" in n for n in plan.notes)


def test_thymeleaf_spring4_caveat_note(tmp_path, cfg):
    plan = build_plan(analyze_path(_spring5_batch_security(tmp_path, thymeleaf="thymeleaf-spring4"), cfg), cfg)
    assert THYMELEAF in _recipes(plan)
    assert any("thymeleaf-spring4 is not migrated by MigrateThymeleafDependencies" in n for n in plan.notes)


def test_boot_plan_gets_no_library_recipes_but_the_project_recipes(tmp_path, cfg):
    plan = build_plan(analyze_path(make_boot27(tmp_path), cfg), cfg)
    rec = _recipes(plan)
    assert not {BATCH6, HIBERNATE71, THYMELEAF} & set(rec)
    assert {"com.company.migrate.Boot4TypeRelocations", "com.company.migrate.Spring7RemovedApis",
            "com.company.migrate.Jackson3SpringConverters"} <= set(rec)


def test_servlet_plan_with_hibernate(tmp_path, cfg):
    root = make_servlet(tmp_path)
    text = (root / "pom.xml").read_text(encoding="utf-8").replace(
        "</dependencies>", "<dependency><groupId>org.hibernate</groupId><artifactId>hibernate-core</artifactId>"
                           "<version>5.4.33.Final</version></dependency></dependencies>")
    (root / "pom.xml").write_text(text, encoding="utf-8")
    plan = build_plan(analyze_path(root, cfg), cfg)
    assert _recipes(plan) == ["com.company.migrate.ServletToJakartaAndBoot", HIBERNATE71]


def test_servlet_project_recipe_runs_the_servlet6_removals():
    import yaml

    base = Path(__file__).resolve().parents[2] / "knowledge" / "recipes" / "base" / "spring-boot-41-version.yml"
    doc = next(d for d in yaml.safe_load_all(base.read_text(encoding="utf-8"))
               if d and d.get("name") == "com.company.migrate.ServletToJakartaAndBoot")
    ids = [r if isinstance(r, str) else next(iter(r)) for r in doc["recipeList"]]
    assert ids[:2] == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
                       "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10"]


def test_broken_table_never_breaks_planning(tmp_path):
    bad = tmp_path / "library_recipes.yml"
    bad.write_text("rules: [ {library: x, cases: [nope]} ]", encoding="utf-8")
    assert library_recipe_rules(bad) == () and rules_for(CaseType.SPRING_LEGACY, {"x"}, bad) == []
    assert library_recipe_rules(tmp_path / "missing.yml") == ()
