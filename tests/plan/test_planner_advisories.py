"""Planner × library advisories (v2): guide-only by default (`advisories.allow_recipes: false` — LLM hints + Handover);
a config that sets allow_recipes runs the advisory recipes inside the transform step, right after the jdk21 set."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.advisory import evaluate_advisories
from sbmig.analyze import analyze_path
from sbmig.core.models import AdvisoryFinding
from sbmig.plan import advise_plan, build_plan
from tests.analyze.helpers import dep, make_boot27, make_boot33, make_spring_legacy, pom, write, write_java

SC = "org.springframework.cloud"
J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
FOX = "org.openrewrite.java.springdoc.SpringFoxToSpringDoc"
DOC3 = "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0"
SLEUTH = "org.openrewrite.java.spring.cloud2022.MigrateCloudSleuthToMicrometerTracing"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
HYSTRIX = "com.company.migrate.HystrixToResilience4j"


@pytest.fixture()
def rcfg(cfg):
    """A config that opts back into advisory recipes (v1 behaviour)."""
    cfg.advisories.allow_recipes = True
    return cfg


def _add_deps(pom_path: Path, *deps: str) -> None:
    text = pom_path.read_text(encoding="utf-8")
    pom_path.write_text(text.replace("<dependencies>", "<dependencies>" + "".join(deps), 1), encoding="utf-8")


def _recipes(plan, sid="transform.rewrite"):
    return [r.id for r in next(s for s in plan.steps if s.id == sid).recipes]


def _legacy_with_springfox(root: Path) -> Path:
    make_spring_legacy(root, security=False)
    _add_deps(root / "pom.xml", dep("io.springfox", "springfox-swagger2", "2.9.2"),
              dep(SC, "spring-cloud-starter-netflix-hystrix", "2.2.10.RELEASE"))
    write_java(root, "", "com.acme.legacy.web", "Api", imports=("springfox.documentation.spring.web.plugins.Docket",))
    return root


# ------------------------------------------------------------------------------------------------- v2 default
def test_default_advisories_are_guide_only(tmp_path: Path, cfg) -> None:
    assert cfg.advisories.allow_recipes is False
    profile = analyze_path(_legacy_with_springfox(tmp_path), cfg)
    findings = evaluate_advisories(profile, cfg)
    assert {"springfox", "hystrix"} <= {f.advisory_id for f in findings}
    plan = build_plan(profile, cfg, None, advisories=findings)
    assert _recipes(plan) == [J21, JAKARTA]                              # no advisory recipe in the transform
    assert "advisory: springfox → guide (advisories.allow_recipes=false)" in plan.notes
    assert "advisory: hystrix → guide (advisories.allow_recipes=false)" in plan.notes
    assert all(f.policy == "guide" and f.recipe_source == "none" for f in findings)


def test_no_advisories_keeps_the_plan_unchanged(tmp_path: Path, rcfg) -> None:
    profile = analyze_path(_legacy_with_springfox(tmp_path), rcfg)
    assert _recipes(build_plan(profile, rcfg)) == _recipes(build_plan(profile, rcfg, None, advisories=[]))
    assert not any(n.startswith("advisory:") for n in build_plan(profile, rcfg).notes)


# ------------------------------------------------------------------------------------------------- allow_recipes
def test_boot_project_advisory_recipes_are_covered_by_the_umbrella(tmp_path: Path, rcfg) -> None:
    make_boot27(tmp_path)
    _add_deps(tmp_path / "pom.xml", dep("io.springfox", "springfox-boot-starter", "3.0.0"),
              dep(SC, "spring-cloud-starter-sleuth", "3.1.9"))
    profile = analyze_path(tmp_path, rcfg)
    findings = evaluate_advisories(profile, rcfg)
    assert {f.advisory_id for f in findings} == {"springfox", "sleuth"}
    plan = build_plan(profile, rcfg, None, advisories=findings)
    recipes = _recipes(plan)
    assert recipes[:2] == [J21, BOOT4] and FOX not in recipes and SLEUTH not in recipes
    assert all(f.recipe_source == "plan" for f in findings)
    assert "advisory: springfox → SpringFoxToSpringDoc, UpgradeSpringDoc_3_0 (covered by the plan)" in plan.notes
    assert "advisory: sleuth → MigrateCloudSleuthToMicrometerTracing (covered by the plan)" in plan.notes


def test_non_boot_project_gets_advisory_recipes_after_java21(tmp_path: Path, rcfg) -> None:
    profile = analyze_path(_legacy_with_springfox(tmp_path), rcfg)
    findings = evaluate_advisories(profile, rcfg)
    by = {f.advisory_id: f for f in findings}
    assert by["springfox"].policy == "recipe" and by["hystrix"].policy == "recipe"
    plan = build_plan(profile, rcfg, None, advisories=findings)
    assert _recipes(plan) == [J21, HYSTRIX, FOX, DOC3, JAKARTA]          # jdk21, advisories, then the jakarta set
    step = next(s for s in plan.steps if s.id == "transform.rewrite")
    assert step.title == "OpenRewrite in one run: Java 21 + advisory recipes + jakarta"
    assert step.recipes[1].source == "project" and step.recipes[1].file.endswith("hystrix-to-resilience4j.yml")
    assert step.recipes[2].source == "builtin" and step.recipes[2].artifact == "org.openrewrite.recipe:rewrite-spring"
    assert by["springfox"].recipe_source == "advisory" and by["hystrix"].recipe_source == "advisory"
    assert "advisory: springfox → SpringFoxToSpringDoc, UpgradeSpringDoc_3_0 (added)" in plan.notes
    assert "advisory: hystrix → HystrixToResilience4j (added)" in plan.notes


def test_msa_per_service_only_where_the_library_is_used(tmp_path: Path, rcfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    _add_deps(tmp_path / "shop" / "pom.xml", dep("io.springfox", "springfox-boot-starter", "3.0.0"))
    profile = analyze_path(tmp_path, rcfg)
    findings = evaluate_advisories(profile, rcfg)
    plan = build_plan(profile, rcfg, None, advisories=findings)
    for sid in ("transform.rewrite@shop", "transform.rewrite@billing"):
        assert FOX not in _recipes(plan, sid) and _recipes(plan, sid)[:2] == [J21, BOOT4]
    fox = next(f for f in findings if f.advisory_id == "springfox")
    assert fox.recipe_source == "plan" and fox.policy == "recipe"
    assert "advisory: springfox → SpringFoxToSpringDoc, UpgradeSpringDoc_3_0 (covered by the plan) [shop]" in plan.notes
    assert not any("springfox" in n and "[billing]" in n for n in plan.notes)


def test_msa_finding_outside_every_service_becomes_guide(tmp_path: Path, rcfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing", "legacy")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    _legacy_with_springfox(tmp_path / "legacy")              # not a service of its own
    profile = analyze_path(tmp_path, rcfg)
    assert "legacy" not in {s.name for s in profile.services}
    findings = evaluate_advisories(profile, rcfg)
    plan = build_plan(profile, rcfg, None, advisories=findings)
    fox = next(f for f in findings if f.advisory_id == "springfox")
    assert fox.policy == "guide" and fox.recipe_source == "none" and fox.recipes == []
    assert "advisory: springfox → guide (no transform.rewrite step for springfox → guide)" in plan.notes


def test_pom_only_project_downgrades_recipe_findings_to_guide(tmp_path: Path, rcfg) -> None:
    write(tmp_path, "pom.xml", pom("bom", packaging="pom",
                                   deps=(dep("io.springfox", "springfox-swagger2", "2.9.2"),)))
    profile = analyze_path(tmp_path, rcfg)
    findings = evaluate_advisories(profile, rcfg)
    plan = build_plan(profile, rcfg, None, advisories=findings)
    fox = next(f for f in findings if f.advisory_id == "springfox")
    assert fox.policy == "guide" and fox.recipes == [] and fox.policy_reason == "no transform.rewrite step → guide"
    assert any(n.startswith("advisory: springfox → guide") for n in plan.notes)


def test_advise_plan_treats_advisory_recipes_as_planned(tmp_path: Path, rcfg) -> None:
    profile = analyze_path(_legacy_with_springfox(tmp_path), rcfg)
    plan = build_plan(profile, rcfg, None, advisories=evaluate_advisories(profile, rcfg))
    rcfg.plan.advisor = "llm"

    class Resp:
        content = '{"add": [{"recipe": "%s", "reason": "x"}], "concerns": []}' % FOX
        input_tokens, output_tokens, model = 10, 5, "m"

    class LLM:
        def complete(self, messages, **kw):
            return Resp()

    plan = advise_plan(plan, profile, LLM(), rcfg, catalog={FOX: "springfox → springdoc"})
    assert _recipes(plan).count(FOX) == 1
    assert any(f"advisor: {FOX} already planned" in n for n in plan.notes)


def test_planner_tolerates_foreign_findings(tmp_path: Path, rcfg) -> None:
    make_boot27(tmp_path)
    profile = analyze_path(tmp_path, rcfg)
    f = AdvisoryFinding(advisory_id="x", library="nope", status="eol", title="X", policy="recipe",
                        recipes=["org.example.NotInCatalog"])
    plan = build_plan(profile, rcfg, None, advisories=[f])
    assert _recipes(plan)[:2] == [J21, "org.example.NotInCatalog"]    # the planner trusts evaluate's policy resolution
    assert f.recipe_source == "advisory"
