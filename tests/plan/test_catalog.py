"""Recipe catalog (knowledge/recipes/catalog.yml): one managed list behind the advisor whitelist and `sbmig recipes`."""
from pathlib import Path

import yaml
from typer.testing import CliRunner

from sbmig.core.models import CaseType, LibraryUsage, PomInfo, ProjectProfile, SourceStats
from sbmig.plan import ADVISOR_CATALOG, ADVISOR_COVERAGE, ADVISOR_GATES, recipes_cli
from sbmig.plan.catalog import GATES, RecipeCatalog, builtin_catalog, default_catalog
from sbmig.plan.planner import artifact_for

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "knowledge" / "recipes" / "catalog.yml"

# Snapshot of the literal dicts that lived in sbmig/plan/planner.py before the catalog (2026-10).
OLD_ADVISOR_CATALOG = {
    "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta": "javax.* EE packages → jakarta.* (servlet, persistence, validation, annotation, inject …)",
    "org.openrewrite.java.migrate.javax.AddJaxbDependenciesWithRuntime": "add JAXB API + runtime when javax.xml.bind is used (removed from the JDK)",
    "org.openrewrite.java.migrate.jakarta.JavaxXmlBindMigrationToJakartaXmlBind": "javax.xml.bind → jakarta.xml.bind",
    "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0": "Spring Boot 2.x/3.x → 4.0 umbrella (Boot projects only)",
    "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1": "Spring Boot 4.1 property renames",
    "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean": "@MockBean/@SpyBean → @MockitoBean/@MockitoSpyBean",
    "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0": "Spring Framework 5.x/6.x → 7.0 (non-Boot Spring projects)",
    "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0": "Spring Security → 7 (lambda DSL, requestMatchers, no WebSecurityConfigurerAdapter)",
    "org.openrewrite.java.testing.junit5.JUnit4to5Migration": "JUnit 4 → JUnit 5",
}
# Round 10: the 7.0 description says what the step does (the 5.x / 6.x API changes run in the 6.0 … 6.5 steps)
NEW_ADVISOR_CATALOG = {
    **OLD_ADVISOR_CATALOG,
    "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0":
        "Spring Security 6.x → 7.0 (version bump, SecurityConfigurer init/configure lose `throws Exception`, "
        "AuthorizationManager.check → authorize); planned after the 6.0 … 6.5 steps",
}
OLD_ADVISOR_COVERAGE = {
    "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0": [
        "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
        "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0",
        "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0",
        "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean",
        "org.openrewrite.java.spring.boot4.SpringBootProperties_4_0",
    ],
    "org.openrewrite.java.migrate.UpgradeToJava21": ["org.openrewrite.java.migrate.javax.AddJaxbDependenciesWithRuntime"],
}
OLD_GATES = {
    "org.openrewrite.java.testing.junit5.JUnit4to5Migration": "has_junit4",
    "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta": "has_javax_ee",
    "org.openrewrite.java.migrate.jakarta.JavaxXmlBindMigrationToJakartaXmlBind": "has_javax_xml_bind",
    "org.openrewrite.java.migrate.javax.AddJaxbDependenciesWithRuntime": "has_xml_bind_any",
    "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0": "has_spring_security",
    "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0": "is_spring",
    "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0": "is_boot",
    "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1": "is_boot",
    "org.openrewrite.java.spring.boot4.ReplaceMockBeanAndSpyBean": "has_tests",
}
# v2.1w (decision 7): the Batch / Hibernate entries carry the planner's library gates (not advisor-eligible; the built-in
# fallback catalog has no such entries)
NEW_GATES = {
    **OLD_GATES,
    "org.openrewrite.java.spring.batch.SpringBatch5To6Migration": "has_spring_batch",
    "org.openrewrite.hibernate.MigrateToHibernate71": "has_hibernate",
}
# v2.1w: every config set the catalog's `sets:` must mirror (invariant 5) — the three conditional sets included
V21W_SETS = {
    "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10": ["servlet_jakarta_10"],
    "org.openrewrite.java.spring.batch.SpringBatch5To6Migration": ["spring_batch_6"],
    "org.openrewrite.hibernate.MigrateToHibernate71": ["hibernate_7"],
}


def _profile(**kw):
    base = dict(root="/x", name="p", case_type=CaseType.SPRING_BOOT, spring_boot_version="2.7.18",
                root_pom=PomInfo(path="pom.xml", artifact_id="p", is_root=True),
                source=SourceStats(java_files=3, javax_imports={"javax.persistence": 2}),
                libraries=[LibraryUsage(name="junit4", version="4.12")], fingerprint="f")
    base.update(kw)
    return ProjectProfile(**base)


def test_catalog_loads_and_is_cached():
    cat = RecipeCatalog.load(CATALOG)
    assert cat is RecipeCatalog.load(CATALOG)
    assert len(cat) >= 15 and len(set(cat.ids())) == len(cat)
    e = cat.get("org.openrewrite.java.migrate.UpgradeToJava21")
    assert e is not None and e.origin == "openrewrite" and e.phase == "jdk21"
    assert all(x.origin == "openrewrite" for x in cat.entries)               # v2: no project recipes
    assert not any(x.id.startswith("com.company.") for x in cat.entries)
    assert cat.get("nope") is None


def test_repo_catalog_is_consistent_with_every_source(cfg):
    cat = default_catalog(cfg)
    assert cat.path == CATALOG.resolve()
    assert cat.check(cfg) == []
    rs = cfg.openrewrite.recipes
    for rid in [*rs.jdk21, *rs.jakarta, *rs.servlet_jakarta_10, *rs.spring_boot_4, *rs.spring_framework_7,
                *rs.spring_security_7, *rs.spring_batch_6, *rs.hibernate_7, *rs.junit5]:
        assert rid in cat, rid


def test_catalog_check_passes_with_every_shipped_config():
    """`sbmig recipes check` must stay green for the default, the company example and the local e2e config."""
    from sbmig.core.config import load_config

    for extra in ((), (REPO / "config" / "artifacts.example.yml",), (REPO / "config" / "artifacts.local-e2e.yml",)):
        c = load_config(*extra, repo_root=REPO, overrides=None if extra else
                        {"artifacts": {"parent": {"group_id": "unset", "artifact_id": "unset"}}})
        assert RecipeCatalog.load(CATALOG).check(c) == [], extra


# Round 4 (library advisories): the 4.0 umbrella's verified chain also includes these (rewrite-spring 6.37.1).
ADVISORY_COVERS = [
    "org.openrewrite.java.springdoc.SpringFoxToSpringDoc",
    "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0",
    "org.openrewrite.java.spring.cloud2022.MigrateCloudSleuthToMicrometerTracing",
    "org.openrewrite.java.spring.cloud2025.UpgradeSpringCloud_2025_1",
]
# Round 10 (verified in rewrite-spring 6.37.0 / rewrite-hibernate 2.25.0 / rewrite-apache 2.30.0 / rewrite-jackson 1.28.4)
ROUND10_BOOT_COVERS = [
    *(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
    "org.openrewrite.java.spring.batch.SpringBatch5To6Migration",
    "org.openrewrite.hibernate.MigrateToHibernate71",
    "org.openrewrite.java.spring.boot3.MigrateThymeleafDependencies",
    "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
    "org.openrewrite.apache.httpclient5.UpgradeApacheHttpClient_5",
    "org.openrewrite.java.jackson.CodehausToFasterXML",
]
ROUND10_FW7_COVERS = [
    "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta",
    "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10",
    "org.openrewrite.apache.httpclient5.UpgradeApacheHttpClient_5",
    "org.openrewrite.java.jackson.CodehausToFasterXML",
]
NEW_ADVISOR_COVERAGE = {
    **OLD_ADVISOR_COVERAGE,
    "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0":
        OLD_ADVISOR_COVERAGE["org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"] + ADVISORY_COVERS
        + ROUND10_BOOT_COVERS,
    "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0": ROUND10_FW7_COVERS,
}


def test_derived_advisor_constants_match_the_old_literals():
    assert ADVISOR_CATALOG == NEW_ADVISOR_CATALOG
    assert list(ADVISOR_CATALOG) == list(OLD_ADVISOR_CATALOG)       # prompt order unchanged
    assert ADVISOR_COVERAGE == NEW_ADVISOR_COVERAGE
    assert set(ADVISOR_GATES) == set(NEW_GATES)
    assert all(ADVISOR_GATES[rid] is GATES[g] for rid, g in NEW_GATES.items())
    # the built-in fallback (catalog file missing) is the same advisor view
    b = builtin_catalog()
    assert b.advisor_catalog() == NEW_ADVISOR_CATALOG and b.coverage() == NEW_ADVISOR_COVERAGE
    assert set(b.gates()) == set(OLD_GATES)


def test_gates_behave():
    p = _profile()
    assert GATES["has_junit4"](p) and not GATES["has_junit4"](_profile(libraries=[]))
    assert GATES["has_javax_ee"](p)
    assert not GATES["has_javax_xml_bind"](p)
    assert GATES["has_xml_bind_any"](_profile(source=SourceStats(jakarta_imports={"jakarta.xml.bind": 1})))
    assert GATES["is_boot"](p) and not GATES["is_boot"](_profile(spring_boot_version=None))
    assert GATES["is_spring"](p)
    assert not GATES["is_spring"](_profile(case_type=CaseType.PURE_JAVA, spring_boot_version=None))
    assert not GATES["has_tests"](p) and GATES["has_tests"](_profile(source=SourceStats(test_files=1)))
    assert not GATES["has_spring_security"](p) and GATES["always"](p)


def test_artifact_lookup_and_coverage(cfg):
    cat = default_catalog(cfg)
    assert cat.artifact_for("org.openrewrite.java.testing.junit5.JUnit4to5Migration") == \
        "org.openrewrite.recipe:rewrite-testing-frameworks"
    assert cat.artifact_for("org.openrewrite.staticanalysis.CommonStaticAnalysis") == \
        "org.openrewrite.recipe:rewrite-static-analysis"                      # prefix fallback
    assert artifact_for("x.y.Z") is None
    cov = cat.covered_by({"org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"})
    assert cov["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"] == \
        "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"


def _temp_catalog(tmp_path: Path, drop: str, mutate=None) -> Path:
    data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    data["recipes"] = [r for r in data["recipes"] if r["id"] != drop]
    if mutate:
        mutate(data)
    p = tmp_path / "catalog.yml"
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


def test_check_reports_missing_ids_and_bad_entries(tmp_path, cfg):
    def mutate(d):
        d["recipes"].append({"id": "org.example.Foo", "origin": "openrewrite", "artifact": "org.example:rewrite-foo",
                             "gate": "is_quantum"})
        d["recipes"].append({"id": "com.company.migrate.Ghost", "origin": "project"})
        for r in d["recipes"]:
            if r["id"] == "org.openrewrite.java.migrate.UpgradeToJava21":
                r["sets"] = ["jdk21", "jakarta"]
    # v2: the junit5 set is empty by default; a company config that opts back in must find the id in the catalog
    cfg.openrewrite.recipes.junit5 = ["org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
    p = _temp_catalog(tmp_path, "org.openrewrite.java.testing.junit5.JUnit4to5Migration", mutate)
    problems = RecipeCatalog.load(p).check(cfg)
    text = "\n".join(problems)
    assert "openrewrite.recipes.junit5: org.openrewrite.java.testing.junit5.JUnit4to5Migration is not in the catalog" in text
    assert "unknown gate 'is_quantum'" in text
    assert "artifact org.example:rewrite-foo is not in openrewrite.recipe_artifacts" in text
    assert "com.company.migrate.Ghost: origin 'project' is not supported" in text
    assert "UpgradeToJava21: lists set 'jakarta' but openrewrite.recipes.jakarta does not contain it" in text


def test_default_catalog_falls_back_when_missing(cfg, caplog):
    cfg.plan.catalog_file = "does/not/exist.yml"
    cat = default_catalog(cfg)
    assert cat.advisor_catalog() == NEW_ADVISOR_CATALOG
    assert any("not found" in r.message for r in caplog.records)


def test_cli_list_show_check(tmp_path):
    r = CliRunner()
    res = r.invoke(recipes_cli, ["check"])
    assert res.exit_code == 0, res.output
    assert "recipe catalog OK" in res.output
    res = r.invoke(recipes_cli, ["list"])
    assert res.exit_code == 0 and "org.openrewrite.java.migrate.UpgradeToJava21" in res.output
    res = r.invoke(recipes_cli, ["list", "--json"])
    import json
    rows = json.loads(res.output)
    assert any(x["id"] == "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10" for x in rows)
    assert not any("rules" in x or "file" in x for x in rows)
    assert "--kb" not in r.invoke(recipes_cli, ["list", "--help"]).output
    res = r.invoke(recipes_cli, ["show", "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"])
    assert res.exit_code == 0
    assert "referenced_by.config_sets: jakarta" in res.output
    assert "referenced_by.covered_by: org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0" in res.output
    assert "referenced_by.rules" not in res.output
    assert r.invoke(recipes_cli, ["show", "org.example.Unknown"]).exit_code == 1
    res = r.invoke(recipes_cli, ["sets"])
    assert res.exit_code == 0 and "spring_boot:" in res.output
    bad = _temp_catalog(tmp_path, "org.openrewrite.java.migrate.UpgradeToJava21")
    res = r.invoke(recipes_cli, ["check", "--catalog", str(bad)])
    assert res.exit_code == 1
    assert "org.openrewrite.java.migrate.UpgradeToJava21 is not in the catalog" in res.output


def test_advise_plan_uses_the_configured_catalog(tmp_path, cfg):
    from sbmig.core.interfaces import LLMResponse
    from sbmig.plan import advise_plan, build_plan

    jakarta = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"

    class _LLM:
        def complete(self, messages, **kw):
            return LLMResponse(content='{"add": [{"recipe": "%s", "reason": "javax.annotation"}], "concerns": []}'
                                       % jakarta, model="m", input_tokens=1, output_tokens=1)

    def no_advisor(d):
        for r in d["recipes"]:
            if r["id"] == jakarta:
                r["advisor"] = False
    # pure_java with javax.annotation only: the planner runs no jakarta set; the repo catalog lets the advisor add it
    prof = _profile(case_type=CaseType.PURE_JAVA, spring_boot_version=None, libraries=[],
                    source=SourceStats(java_files=3, javax_imports={"javax.annotation": 1}))
    cfg.plan.advisor = "llm"
    out = advise_plan(build_plan(prof, cfg), prof, llm=_LLM(), cfg=cfg)
    assert any(n.startswith(f"advisor: +{jakarta} → transform.rewrite") for n in out.notes)
    cfg.plan.catalog_file = str(_temp_catalog(tmp_path, "none", no_advisor))
    out = advise_plan(build_plan(prof, cfg), prof, llm=_LLM(), cfg=cfg)
    assert any(f"ignored unknown recipe {jakarta}" in n for n in out.notes)


def test_library_migrations_are_not_advisor_eligible_and_covered_by_the_umbrella(cfg):
    cat = default_catalog(cfg)
    cov = cat.covered_by({"org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"})
    for rid in ADVISORY_COVERS:
        e = cat.get(rid)
        assert e is not None and not e.advisor and rid not in ADVISOR_CATALOG
        assert cov[rid] == "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"


def test_v21w_sets_and_gates(cfg):
    """v2.1w: the three conditional sets are listed in the catalog's `sets:` (drift check) and gated on the analyzer's
    library names; the config sets and the catalog agree."""
    from sbmig.plan.catalog import config_recipe_sets

    cat = default_catalog(cfg)
    sets = config_recipe_sets(cfg)
    for rid, names in V21W_SETS.items():
        e = cat.get(rid)
        assert e is not None and e.sets == names and not e.advisor, rid
        assert all(rid in sets[n] for n in names)
        # still reached through the Boot 4 umbrella for Boot projects
        assert rid in cat.coverage()["org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"]
    assert "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10" in \
        cat.coverage()["org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"]
    assert cat.get("org.openrewrite.java.spring.batch.SpringBatch5To6Migration").gate == "has_spring_batch"
    assert cat.get("org.openrewrite.hibernate.MigrateToHibernate71").gate == "has_hibernate"
    assert cat.check(cfg) == []
    # emptying a set in a company config is consistent only when the catalog drops the set name too
    cfg.openrewrite.recipes.spring_batch_6 = []
    assert any("lists set 'spring_batch_6' but openrewrite.recipes.spring_batch_6 does not contain it" in x
               for x in cat.check(cfg))


def test_v21w_gates_behave():
    batch = _profile(case_type=CaseType.SPRING_LEGACY, spring_boot_version=None,
                     libraries=[LibraryUsage(name="spring-batch", version="4.3.10")])
    hib = _profile(case_type=CaseType.SPRING_LEGACY, spring_boot_version=None,
                   libraries=[LibraryUsage(name="hibernate", version="5.6.15.Final")])
    jpa_only = _profile(case_type=CaseType.SPRING_LEGACY, spring_boot_version=None,
                        libraries=[LibraryUsage(name="jpa"), LibraryUsage(name="spring-data-jpa")])
    assert GATES["has_spring_batch"](batch) and not GATES["has_spring_batch"](hib)
    assert GATES["has_hibernate"](hib) and not GATES["has_hibernate"](batch)
    assert not GATES["has_hibernate"](jpa_only)                          # JPA API only → no native Hibernate recipe
