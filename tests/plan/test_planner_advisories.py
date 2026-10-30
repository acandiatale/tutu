"""Planner × library advisories: guide-only (LLM hints + Handover) — an advisory never adds a recipe to the plan."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import evaluate_advisories
from sbmig.analyze import analyze_path
from sbmig.core.models import AdvisoryFinding
from sbmig.plan import build_plan
from tests.analyze.helpers import dep, make_boot27, make_boot33, make_spring_legacy, pom, write, write_java

SC = "org.springframework.cloud"
J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"   # a default set since v2.1o
FOX = "org.openrewrite.java.springdoc.SpringFoxToSpringDoc"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
PROPS41 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"
GUIDE = "guide (LLM hints + Handover)"


def _guide(f) -> bool:
    """A guide-only finding — robust to `policy` / `policy_reason` / `recipes` / `recipe_source` leaving the model."""
    return (getattr(f, "policy", "guide") == "guide" and not getattr(f, "policy_reason", "")
            and not getattr(f, "recipes", []) and getattr(f, "recipe_source", "none") == "none")


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


def test_advisories_are_guide_only(tmp_path: Path, cfg) -> None:
    profile = analyze_path(_legacy_with_springfox(tmp_path), cfg)
    findings = evaluate_advisories(profile, cfg)
    assert {"springfox", "hystrix"} <= {f.advisory_id for f in findings}
    before = [f.model_copy(deep=True) for f in findings]
    plan = build_plan(profile, cfg, None, advisories=findings)
    assert _recipes(plan) == [J21, JAKARTA, FW7]                         # no advisory recipe in the transform (FW7 = a set, v2.1o)
    step = next(s for s in plan.steps if s.id == "transform.rewrite")
    assert step.title == "OpenRewrite in one run: Java 21 + jakarta + Spring Framework 7"
    assert f"advisory: springfox → {GUIDE}" in plan.notes and f"advisory: hystrix → {GUIDE}" in plan.notes
    assert findings == before                                           # the planner never mutates a finding
    assert all(_guide(f) for f in findings)


def test_no_advisories_keeps_the_plan_unchanged(tmp_path: Path, cfg) -> None:
    profile = analyze_path(_legacy_with_springfox(tmp_path), cfg)
    assert _recipes(build_plan(profile, cfg)) == _recipes(build_plan(profile, cfg, None, advisories=[]))
    assert not any(n.startswith("advisory:") for n in build_plan(profile, cfg).notes)


def test_boot_project_runs_only_the_boot_set(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path)
    _add_deps(tmp_path / "pom.xml", dep("io.springfox", "springfox-boot-starter", "3.0.0"),
              dep(SC, "spring-cloud-starter-sleuth", "3.1.9"))
    profile = analyze_path(tmp_path, cfg)
    findings = evaluate_advisories(profile, cfg)
    assert {f.advisory_id for f in findings} == {"springfox", "sleuth"}
    plan = build_plan(profile, cfg, None, advisories=findings)
    assert _recipes(plan)[:3] == [J21, BOOT4, PROPS41] and FOX not in _recipes(plan)
    assert sorted(n for n in plan.notes if n.startswith("advisory:")) == [f"advisory: sleuth → {GUIDE}",
                                                                          f"advisory: springfox → {GUIDE}"]


def test_msa_services_get_no_advisory_recipes(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    _add_deps(tmp_path / "shop" / "pom.xml", dep("io.springfox", "springfox-boot-starter", "3.0.0"))
    profile = analyze_path(tmp_path, cfg)
    findings = evaluate_advisories(profile, cfg)
    plan = build_plan(profile, cfg, None, advisories=findings)
    for sid in ("transform.rewrite@shop", "transform.rewrite@billing"):
        assert FOX not in _recipes(plan, sid) and _recipes(plan, sid)[:2] == [J21, BOOT4]
    assert plan.notes.count(f"advisory: springfox → {GUIDE}") == 1


def test_pom_only_project_notes_the_guide(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("bom", packaging="pom",
                                   deps=(dep("io.springfox", "springfox-swagger2", "2.9.2"),)))
    profile = analyze_path(tmp_path, cfg)
    findings = evaluate_advisories(profile, cfg)
    plan = build_plan(profile, cfg, None, advisories=findings)
    assert not any(s.recipes for s in plan.steps)
    assert f"advisory: springfox → {GUIDE}" in plan.notes
