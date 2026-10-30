"""v2 planner (flow-v2, v2.1): ONE transform.rewrite run → transform.compile → restructure → restructure.review →
verify.build, per case."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import CaseType, Phase, ProjectProfile, StepKind
from sbmig.plan import advise_plan, build_plan, spring_recipes, transform_recipes
from sbmig.plan.planner import artifact_for, ee_javax_packages
from tests.analyze.helpers import (make_boot27, make_boot33, make_pure_java, make_servlet, make_spring_legacy, pom,
                                   write)

ORDER = ["transform.rewrite", "transform.compile", "restructure.apply", "restructure.review", "verify.build"]
J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
PROPS41 = "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"
SEC = [*(f"org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_{v}" for v in range(6)),
       "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0"]
JUNIT5 = "org.openrewrite.java.testing.junit5.JUnit4to5Migration"
V1_IDS = ("jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.optional_bump", "spring.loop", "restructure.loop")


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid="transform.rewrite"):
    return [r.id for r in _step(plan, sid).recipes]


def _no_project_recipes(plan) -> bool:
    return not any(r.id.startswith("com.company.") or r.source != "builtin" for s in plan.steps for r in s.recipes)


def _opt_in(cfg):
    """A company config that fills the v1 sets again (Framework 7, the Security chain, JUnit 5)."""
    rs = cfg.openrewrite.recipes
    rs.spring_framework_7, rs.spring_security_7, rs.junit5 = [FW7], list(SEC), [JUNIT5]
    return cfg


# ------------------------------------------------------------------------------------------------- per case
def test_spring_boot_plan(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_boot27(tmp_path), cfg)
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ORDER
    assert plan.case_type == CaseType.SPRING_BOOT and plan.profile_fingerprint == profile.fingerprint
    tr = _step(plan, "transform.rewrite")
    assert tr.kind == StepKind.OPENREWRITE and tr.phase == Phase.TRANSFORM and tr.target is None and tr.params == {}
    assert _recipes(plan) == [J21, BOOT4, PROPS41]                    # no JUnit 5, no project recipes
    assert tr.recipes[0].artifact == "org.openrewrite.recipe:rewrite-migrate-java"
    assert tr.recipes[1].artifact == "org.openrewrite.recipe:rewrite-spring"
    assert tr.title == "OpenRewrite in one run: Java 21 + Spring Boot 4.1"
    assert _step(plan, "restructure.apply").params == {"mode": "layout", "starter": "auto"}
    rv = _step(plan, "restructure.review")
    assert rv.kind == StepKind.REVIEW and rv.phase == Phase.RESTRUCTURE
    assert rv.title == "LLM structure review (poms / dependencies / modules)"
    assert rv.params == {"restructure_step": "restructure.apply"} and "business rule" in rv.reason
    v = _step(plan, "verify.build")
    assert v.kind == StepKind.BUILD and v.phase == Phase.VERIFY and v.build_goal == "clean package"
    assert all(s.reason for s in plan.steps)
    assert not any(s.kind.value in ("build_fix_loop", "handler") for s in plan.steps)
    assert "transform: one OpenRewrite run (UpgradeToJava21, UpgradeSpringBoot_4_0, SpringBootProperties_4_1) — then " \
           "one compile, no fixing until the restructure is done (v2.1)" in plan.notes
    comp = _step(plan, "transform.compile")
    assert comp.kind == StepKind.BUILD and comp.phase == Phase.TRANSFORM and comp.build_goal == "clean compile"
    assert any(n.startswith("compile: one mvn clean compile after OpenRewrite") for n in plan.notes)
    assert any("umbrella migrates javax → jakarta itself" in n for n in plan.notes)
    assert not any("pre-emptive" in n or "lombok-version-floor" in n for n in plan.notes)
    assert _no_project_recipes(plan)


def test_spring_boot_without_junit4_has_no_junit_note(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert _recipes(plan) == [J21, BOOT4, PROPS41]
    assert not any("junit4" in n for n in plan.notes)


def test_spring_legacy_plan(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert _recipes(plan) == [J21, JAKARTA, *SEC]                     # v2.1l: Security chain (spring-security detected); no Framework 7 / JUnit 5
    assert _step(plan, "restructure.apply").params["starter"] == "spring-boot-starter-webmvc"
    assert any("@ImportResource" in n for n in plan.notes)
    assert any("Spring Security set UpgradeSpringSecurity_6_0 … UpgradeSpringSecurity_7_0 (spring-security detected)"
               in n for n in plan.notes)
    assert any("junit4 detected, the JUnit 5 set is empty (v2)" in n for n in plan.notes)
    assert _no_project_recipes(plan)


def test_servlet_plan(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_servlet(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert _recipes(plan) == [J21, JAKARTA]                           # not com.company.migrate.ServletToJakartaAndBoot
    assert _step(plan, "restructure.apply").params["starter"] == "spring-boot-starter-webmvc"
    assert any("web.xml" in n for n in plan.notes)
    assert "transform: servlet_tomcat → jakarta set (javax → jakarta)" in plan.notes
    assert _no_project_recipes(plan)


def test_pure_java_without_ee_runs_only_java21(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_pure_java(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert _recipes(plan) == [J21]
    assert _step(plan, "transform.rewrite").title == "OpenRewrite in one run: Java 21"
    assert _step(plan, "restructure.apply").params == {"mode": "layout", "starter": "spring-boot-starter"}
    assert any("no jakarta migration" in n for n in plan.notes)


def test_pure_java_with_ee_and_junit4(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_pure_java(tmp_path, javax_ee=True, junit4=True), cfg), cfg)
    assert _recipes(plan) == [J21, JAKARTA]
    assert any("Java EE javax imports (javax.xml.bind) → jakarta set" in n for n in plan.notes)


def test_ee_javax_filter() -> None:
    assert ee_javax_packages({"javax.swing": 1, "javax.sql": 2, "javax.crypto": 1, "javax.annotation": 1}, False) == []
    assert ee_javax_packages({"javax.annotation": 1}, True) == ["javax.annotation"]
    assert ee_javax_packages({"javax.servlet": 1, "javax.xml.parsers": 1, "javax.xml.bind": 1}, False) == [
        "javax.servlet", "javax.xml.bind"]


def test_unknown_case_runs_java21_only(cfg) -> None:
    from sbmig.core.models import PomInfo, SourceStats
    prof = ProjectProfile(root="/x", name="x", case_type=CaseType.UNKNOWN, source=SourceStats(java_files=2),
                          poms=[PomInfo(path="pom.xml", artifact_id="x")])
    plan = build_plan(prof, cfg)
    assert _ids(plan) == ORDER and _recipes(plan) == [J21]


# ------------------------------------------------------------------------------------------------- company opt-in
def test_company_config_opts_back_into_framework_security_and_junit5(tmp_path: Path, cfg) -> None:
    _opt_in(cfg)
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert _recipes(plan) == [J21, JAKARTA, FW7, *SEC, JUNIT5]        # jdk21, jakarta, framework / security, junit5
    assert _step(plan, "transform.rewrite").title == ("OpenRewrite in one run: Java 21 + jakarta + Spring Framework 7 "
                                                      "+ Spring Security 7 + JUnit 5")
    assert any("Spring Security set UpgradeSpringSecurity_6_0 … UpgradeSpringSecurity_7_0 (spring-security detected)"
               in n for n in plan.notes)
    assert any("junit4 detected → JUnit 5 set" in n for n in plan.notes)
    junit = _step(plan, "transform.rewrite").recipes[-1]
    assert junit.artifact == "org.openrewrite.recipe:rewrite-testing-frameworks"


def test_opt_in_sets_keep_their_gates(tmp_path: Path, cfg) -> None:
    _opt_in(cfg)
    legacy = build_plan(analyze_path(make_spring_legacy(tmp_path / "a", security=False), cfg), cfg)
    assert FW7 in _recipes(legacy) and not set(SEC) & set(_recipes(legacy))    # security only with spring-security
    boot = build_plan(analyze_path(make_boot33(tmp_path / "b"), cfg), cfg)
    assert _recipes(boot) == [J21, BOOT4, PROPS41]                    # no junit4 → no JUnit 5; Boot: no FW7 / Security
    boot4 = build_plan(analyze_path(make_boot27(tmp_path / "c"), cfg), cfg)
    assert _recipes(boot4) == [J21, BOOT4, PROPS41, JUNIT5]           # boot set before junit5


def test_extra_recipes_from_config_come_last(tmp_path: Path, cfg) -> None:
    cfg.openrewrite.recipes.extra = {"spring_boot": ["org.openrewrite.staticanalysis.CommonStaticAnalysis"],
                                     "all": ["com.company.migrate.Custom"]}
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert _recipes(plan) == [J21, BOOT4, PROPS41, "org.openrewrite.staticanalysis.CommonStaticAnalysis",
                              "com.company.migrate.Custom"]
    rec = {r.id: r for r in _step(plan, "transform.rewrite").recipes}
    assert rec["org.openrewrite.staticanalysis.CommonStaticAnalysis"].artifact == \
        "org.openrewrite.recipe:rewrite-static-analysis"
    assert rec["com.company.migrate.Custom"].source == "builtin"
    assert any("extra recipes from config (spring_boot/all)" in n for n in plan.notes)
    assert artifact_for("x.y.Z") is None
    # extras of another case are not planned
    other = build_plan(analyze_path(make_pure_java(tmp_path / "p"), cfg), cfg)
    assert _recipes(other) == [J21, "com.company.migrate.Custom"]


def test_empty_sets_omit_the_transform_step(tmp_path: Path, cfg) -> None:
    cfg.openrewrite.recipes.jdk21 = []
    plan = build_plan(analyze_path(make_pure_java(tmp_path), cfg), cfg)
    assert _ids(plan) == ["transform.compile", "restructure.apply", "restructure.review", "verify.build"]
    assert "transform: no recipes apply → transform.rewrite omitted" in plan.notes


def test_transform_recipes_helper_and_v1_alias(cfg) -> None:
    from sbmig.plan.planner import _Facts
    assert spring_recipes is transform_recipes
    notes: list[str] = []
    ids = [r.id for r in transform_recipes(_Facts(CaseType.SERVLET_TOMCAT, set(), {}), cfg, notes, "web")]
    assert ids == [J21, JAKARTA] and notes == ["transform [web]: servlet_tomcat → jakarta set (javax → jakarta)"]


# ------------------------------------------------------------------------------------------------- v1 knobs ignored
def test_re_enabled_v1_loops_are_ignored(tmp_path: Path, cfg) -> None:
    for k in ("jdk21", "spring", "restructure"):
        cfg.loop.phases[k].enabled = True
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert not set(V1_IDS) & set(_ids(plan))
    assert "v2: no intermediate build-fix loops (loop.phases.{jdk21,spring,restructure}.enabled=true ignored)" \
        in plan.notes


def test_v2_defaults_emit_no_v1_loop_notes(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert not any("loop.phases" in n or "intermediate_builds" in n or "parent_phase" in n for n in plan.notes)


def test_optional_bump_runs_inside_the_restructure(tmp_path: Path, cfg) -> None:
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert "spring.optional_bump" not in _ids(plan)
    assert "optional artifacts: bumped inside the restructure step (no separate bump step in v2)" in plan.notes
    cfg.artifacts.optional, cfg.artifacts.remove_when_present = [], []
    assert "optional artifacts: none configured" in build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg).notes


def test_verify_loop_disabled_is_noted(tmp_path: Path, cfg) -> None:
    cfg.loop.phases["verify"].enabled = False
    plan = build_plan(analyze_path(make_boot33(tmp_path), cfg), cfg)
    assert _ids(plan) == ORDER
    assert any("loop.phases.verify.enabled=false" in n for n in plan.notes)


# ------------------------------------------------------------------------------------------------- review
def test_review_disabled_is_omitted_with_a_note(tmp_path: Path, cfg) -> None:
    cfg.review.enabled = False
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert _ids(plan) == ["transform.rewrite", "transform.compile", "restructure.apply", "verify.build"]
    assert "review: review.enabled=false → restructure.review omitted (the build follows the restructure)" in plan.notes


def test_review_note_follows_fix_critical(tmp_path: Path, cfg) -> None:
    cfg.review.fix_critical = False
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert any(n.startswith("review:") and "findings recorded only" in n for n in plan.notes)


# ------------------------------------------------------------------------------------------------- KB
class FakeKB:
    """A v1 KB with active recipes and candidates: v2 must not plan any of them."""

    def active_recipe_ids(self):
        return ["com.company.kb.FixA"]

    def active_recipe_files(self):
        return ["/kb/a.yml"]

    def list_recipes(self, status=None):
        raise AssertionError("v2 never lists KB recipes")


def test_kb_recipes_are_not_used(tmp_path: Path, cfg) -> None:
    profile = analyze_path(make_pure_java(tmp_path), cfg)
    plan = build_plan(profile, cfg, kb=FakeKB())                  # the kb argument stays for the seam, unused
    assert _recipes(plan) == [J21] and _no_project_recipes(plan)
    assert plan.model_dump(exclude={"created_at"}) == build_plan(profile, cfg).model_dump(exclude={"created_at"})
    assert not any(n.startswith("kb:") for n in plan.notes)


# ------------------------------------------------------------------------------------------------- MSA / misc
def test_msa_plan_with_aggregator(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    profile = analyze_path(tmp_path, cfg)
    plan = build_plan(profile, cfg, kb=FakeKB())
    order = [s.name for s in profile.services]
    assert _ids(plan) == [f"transform.rewrite@{svc}" for svc in order] + [
        "transform.compile", "restructure.msa_pom", "restructure.review", "verify.build"]
    comp = _step(plan, "transform.compile")              # v2.1: one compile at the root aggregator, no fixing
    assert comp.kind == StepKind.BUILD and comp.phase == Phase.TRANSFORM and comp.build_goal == "clean compile"
    assert comp.target is None
    shop = _step(plan, "transform.rewrite@shop")
    assert shop.kind == StepKind.OPENREWRITE and shop.phase == Phase.TRANSFORM
    assert shop.target == "shop" and shop.params == {"service": "shop"}
    assert shop.title == "OpenRewrite in one run: Java 21 + Spring Boot 4.1 (shop)"
    assert _recipes(plan, "transform.rewrite@shop") == _recipes(plan, "transform.rewrite@billing") == [J21, BOOT4,
                                                                                                         PROPS41]
    msa = _step(plan, "restructure.msa_pom")
    assert msa.kind == StepKind.MSA_POM and msa.params["mode"] == "msa_pom"
    assert sorted(msa.params["services"]) == ["billing", "shop"]
    assert _step(plan, "restructure.review").params == {"restructure_step": "restructure.msa_pom"}
    assert _step(plan, "verify.build").target is None
    assert any("msa service shop" in n for n in plan.notes)
    assert any(n.startswith("transform [shop]: one OpenRewrite run") for n in plan.notes)


def test_msa_plan_without_aggregator(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_servlet(tmp_path / "web")
    write(tmp_path, "web/pom.xml", pom("web", packaging="war", deps=()))
    profile = analyze_path(tmp_path, cfg)
    assert profile.case_type == CaseType.MSA
    plan = build_plan(profile, cfg)
    verify = [s for s in plan.steps if s.kind == StepKind.BUILD and s.phase == Phase.VERIFY]
    assert sorted(s.id for s in verify) == ["verify.build@shop", "verify.build@web"]
    comp = [s for s in plan.steps if s.kind == StepKind.BUILD and s.phase == Phase.TRANSFORM]
    assert sorted(s.id for s in comp) == ["transform.compile@shop", "transform.compile@web"]   # per target, like verify
    assert {s.target for s in comp} == {"shop", "web"}
    assert {s.target for s in verify} == {"shop", "web"}
    assert _recipes(plan, "transform.rewrite@web") == [J21, JAKARTA]
    ids = _ids(plan)
    assert ids.index("restructure.msa_pom") < ids.index("restructure.review") < ids.index(verify[0].id)


def test_msa_without_services_falls_back(cfg) -> None:
    profile = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA)
    plan = build_plan(profile, cfg)
    assert "restructure.apply" in _ids(plan)
    assert _recipes(plan) == [J21, BOOT4, PROPS41]                    # planned as a Boot project
    assert any("without discovered services" in n for n in plan.notes)


def test_warnings_become_notes_and_advisor(cfg) -> None:
    profile = ProjectProfile(root="/x", name="x", case_type=CaseType.UNKNOWN, warnings=["no java sources found"])
    plan = build_plan(profile, cfg)
    assert plan.notes[-1] == "warning: no java sources found"
    assert advise_plan(plan, profile) is plan
    assert advise_plan(plan, profile, llm=object(), cfg=cfg) is plan          # advisor: none
    cfg.plan.advisor = "llm"
    out = advise_plan(plan, profile, llm=object(), cfg=cfg)
    assert any(n.startswith('advisor') for n in out.notes)
