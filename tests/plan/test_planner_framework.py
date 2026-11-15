"""Framework projects (v2.1z-ak): the reduced plan (scope jdk-jakarta) and the unchanged full-scope plan.

Note: tests/plan/conftest.py switches `plan.compile_after_rewrite` on for every test here; the reduced plan never
plans `transform.compile` either way."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path
from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import CaseType, FrameworkEvidence, FrameworkUse, Phase, ProjectProfile, SourceStats, StepKind
from sbmig.plan import advise_plan, build_plan
from tests.analyze.helpers import (make_boot27, make_boot33, make_pure_java, make_servlet, make_spring_legacy, pom,
                                   write)

FW = "com.example.fw:example-fw-core"
J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"


def _reduced(profile: ProjectProfile) -> ProjectProfile:
    profile.framework = FrameworkUse(
        patterns=[FW], evidence=[FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")],
        scope="jdk-jakarta", reason="framework artifact … found → scope jdk-jakarta")
    return profile


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _recipes(plan, sid="transform.rewrite"):
    return [r.id for r in _step(plan, sid).recipes]


def _no_restructure(plan):
    assert not any(s.id.startswith("restructure.") or s.id.startswith("transform.compile") for s in plan.steps)
    assert not any(s.kind in (StepKind.REVIEW, StepKind.MSA_POM) for s in plan.steps)


BUILDERS = {
    "spring_legacy": lambda p: make_spring_legacy(p),
    "servlet_tomcat": lambda p: make_servlet(p),
    "spring_boot": lambda p: make_boot27(p),
    "pure_java": lambda p: make_pure_java(p),
    "pure_java_ee": lambda p: make_pure_java(p, javax_ee=True),
}


# ------------------------------------------------------------------------------------------------- full scope
@pytest.mark.parametrize("kind", sorted(BUILDERS))
@pytest.mark.parametrize("compile_first", [True, False])
def test_full_scope_plan_is_unchanged(tmp_path: Path, cfg, kind, compile_first):
    cfg.plan.compile_after_rewrite = compile_first
    cfg.framework.artifacts = [FW]
    base = analyze_path(BUILDERS[kind](tmp_path), cfg)
    assert base.framework is not None and not base.framework.found
    base.framework = None
    plain = build_plan(base, cfg)
    full = base.model_copy(deep=True)
    full.framework = FrameworkUse(
        patterns=[FW], evidence=[FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")],
        scope="full", reason="framework.scope=full → full migration")
    with_fw = build_plan(full, cfg)
    dump = lambda p: [s.model_dump() for s in p.steps]  # noqa: E731
    assert dump(with_fw) == dump(plain) and with_fw.notes == plain.notes
    assert plain.scope == with_fw.scope == "full"
    assert any(s.id.startswith("restructure.") for s in plain.steps)


def test_full_scope_msa_unchanged(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    profile = analyze_path(tmp_path, cfg)
    plain = build_plan(profile, cfg)
    profile.framework = FrameworkUse(patterns=[FW], scope="full")
    other = build_plan(profile, cfg)
    assert [s.model_dump() for s in other.steps] == [s.model_dump() for s in plain.steps]
    assert other.notes == plain.notes and other.scope == "full"


# ------------------------------------------------------------------------------------------------- single
@pytest.mark.parametrize("kind,recipes", [
    ("spring_legacy", [J21, JAKARTA]), ("servlet_tomcat", [J21, JAKARTA]), ("spring_boot", [J21, JAKARTA]),
    ("pure_java", [J21]), ("pure_java_ee", [J21, JAKARTA])])
def test_reduced_single(tmp_path: Path, cfg, kind, recipes):
    profile = _reduced(analyze_path(BUILDERS[kind](tmp_path), cfg))
    plan = build_plan(profile, cfg)
    assert plan.scope == "jdk-jakarta"
    _no_restructure(plan)
    assert _recipes(plan) == recipes
    tr = _step(plan, "transform.rewrite")
    assert tr.kind == StepKind.OPENREWRITE and tr.phase == Phase.TRANSFORM and tr.target is None
    v = _step(plan, "verify.build")
    assert v.phase == Phase.VERIFY and v.build_goal == "clean package" and plan.steps[-1].id == "verify.build"
    assert plan.notes[0].startswith(f"scope: jdk-jakarta — framework project ({FW} 1.2.0 (direct: pom.xml))")
    assert "no restructure, no structure review" in plan.notes[0]
    assert not any(n.startswith("optional artifacts:") for n in plan.notes)
    assert not any(n.startswith("review:") or n.startswith("restructure:") for n in plan.notes)
    # conftest switched compile_after_rewrite on: a note, never a step
    assert any("plan.compile_after_rewrite=true does not apply in the jdk-jakarta scope" in n for n in plan.notes)


def test_reduced_without_compile_note(tmp_path: Path, cfg):
    cfg.plan.compile_after_rewrite = False
    plan = build_plan(_reduced(analyze_path(make_spring_legacy(tmp_path), cfg)), cfg)
    assert _ids(plan) == ["transform.rewrite", "verify.build"]
    assert not any("compile_after_rewrite" in n for n in plan.notes)


def test_reduced_jsp_step(tmp_path: Path, cfg):
    profile = _reduced(analyze_path(make_servlet(tmp_path), cfg))
    assert profile.source.jsp_paths or profile.source.jsp_count
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ["transform.rewrite", "transform.jsp_jakarta", "verify.build"]
    jsp = _step(plan, "transform.jsp_jakarta")
    assert jsp.kind == StepKind.RESTRUCTURE and jsp.phase == Phase.TRANSFORM
    assert jsp.params == {"mode": "jsp_jakarta"} and jsp.target is None


def test_reduced_jsp_rename_off(tmp_path: Path, cfg):
    cfg.layout.jsp_jakarta_rename = False
    plan = build_plan(_reduced(analyze_path(make_servlet(tmp_path), cfg)), cfg)
    assert "transform.jsp_jakarta" not in _ids(plan)
    assert any("layout.jsp_jakarta_rename=false" in n for n in plan.notes)


def test_reduced_no_jsp_without_jakarta(cfg):
    profile = _reduced(ProjectProfile(root="/x", name="x", case_type=CaseType.PURE_JAVA,
                                      source=SourceStats(java_files=2, jsp_count=1, jsp_paths=["a.jsp"])))
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ["transform.rewrite", "verify.build"] and _recipes(plan) == [J21]


def test_reduced_no_java_sources(cfg):
    profile = _reduced(ProjectProfile(root="/x", name="x", case_type=CaseType.SPRING_LEGACY,
                                      source=SourceStats(java_files=0, jsp_count=2)))
    plan = build_plan(profile, cfg)
    assert _ids(plan) == ["verify.build"] and plan.scope == "jdk-jakarta"
    assert any(n.startswith("no java sources → no transform step") for n in plan.notes)


def test_reduced_unknown_case_with_ee_imports(cfg):
    profile = _reduced(ProjectProfile(root="/x", name="x", case_type=CaseType.UNKNOWN,
                                      source=SourceStats(java_files=2, javax_imports={"javax.persistence": 1})))
    assert _recipes(build_plan(profile, cfg)) == [J21, JAKARTA]


def test_reduced_ignores_extra_and_junit_sets(tmp_path: Path, cfg):
    rs = cfg.openrewrite.recipes
    rs.junit5 = ["org.openrewrite.java.testing.junit5.JUnit4to5Migration"]
    rs.extra = {"all": ["org.example.Extra"]}
    plan = build_plan(_reduced(analyze_path(make_boot27(tmp_path), cfg)), cfg)
    assert _recipes(plan) == [J21, JAKARTA]


# ------------------------------------------------------------------------------------------------- MSA
def test_reduced_msa_with_covering_aggregator(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    cfg.layout.msa_parent_policy = "per-service-root"               # does not matter in the reduced scope
    plan = build_plan(_reduced(analyze_path(tmp_path, cfg)), cfg)
    assert _ids(plan) == ["transform.rewrite", "verify.build"]
    assert _recipes(plan) == [J21, JAKARTA]
    assert _step(plan, "verify.build").target is None
    _no_restructure(plan)


def test_reduced_msa_without_aggregator(tmp_path: Path, cfg):
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_servlet(tmp_path / "web")
    write(tmp_path, "web/pom.xml", pom("web", packaging="war", deps=()))
    profile = _reduced(analyze_path(tmp_path, cfg))
    assert profile.case_type == CaseType.MSA
    plan = build_plan(profile, cfg)
    ids = _ids(plan)
    assert sorted(i for i in ids if i.startswith("transform.rewrite@")) == ["transform.rewrite@shop",
                                                                            "transform.rewrite@web"]
    assert _recipes(plan, "transform.rewrite@shop") == _recipes(plan, "transform.rewrite@web") == [J21, JAKARTA]
    assert _step(plan, "transform.rewrite@web").target == "web"
    verify = [s for s in plan.steps if s.phase == Phase.VERIFY]
    assert sorted(s.id for s in verify) == ["verify.build@shop", "verify.build@web"]
    assert {s.target for s in verify} == {"shop", "web"}
    assert ids.index("transform.jsp_jakarta") > max(ids.index("transform.rewrite@shop"), ids.index("transform.rewrite@web"))
    assert ids.index("transform.jsp_jakarta") < min(ids.index(s.id) for s in verify)
    _no_restructure(plan)


# ------------------------------------------------------------------------------------------------- advisor
class _LLM:
    def __init__(self):
        self.calls = 0

    def complete(self, *a, **k):
        self.calls += 1
        return LLMResponse(content='{"add": [{"recipe": "%s"}]}' % JAKARTA, model="m", input_tokens=1,
                           output_tokens=1)


def test_advise_plan_leaves_a_reduced_plan_unchanged(tmp_path: Path, cfg):
    cfg.plan.advisor = "llm"
    plan = build_plan(_reduced(analyze_path(make_pure_java(tmp_path), cfg)), cfg)
    steps, notes = [s.model_dump() for s in plan.steps], list(plan.notes)
    llm = _LLM()
    out = advise_plan(plan, _reduced(analyze_path(tmp_path, cfg)), llm, cfg)
    assert llm.calls == 0
    assert [s.model_dump() for s in out.steps] == steps
    assert out.notes[:-1] == notes and len(out.notes) == len(notes) + 1
    assert out.notes[-1].startswith("advisor: jdk-jakarta scope")


# ------------------------------------------------------------------------------------------------- framework.recipe_sets
SERVLET10 = "org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"


def test_default_recipe_sets_note(tmp_path: Path, cfg):
    assert cfg.framework.recipe_sets == ["jdk21", "jakarta"]
    plan = build_plan(_reduced(analyze_path(make_spring_legacy(tmp_path), cfg)), cfg)
    assert "transform: jdk-jakarta scope, spring_legacy → recipe sets: jdk21, jakarta" in plan.notes


@pytest.mark.parametrize("kind", ["servlet_tomcat", "spring_legacy"])
def test_servlet_removals_listed(tmp_path: Path, cfg, kind):
    cfg.framework.recipe_sets = ["jdk21", "jakarta", "servlet_jakarta_10"]
    plan = build_plan(_reduced(analyze_path(BUILDERS[kind](tmp_path), cfg)), cfg)
    assert _recipes(plan) == [J21, JAKARTA, SERVLET10]
    assert any(n.endswith("→ recipe sets: jdk21, jakarta, servlet_jakarta_10") for n in plan.notes)


def test_servlet_removals_left_out_without_jakarta(tmp_path: Path, cfg):
    cfg.framework.recipe_sets = ["jdk21", "servlet_jakarta_10", "jakarta"]
    plan = build_plan(_reduced(analyze_path(make_pure_java(tmp_path), cfg)), cfg)
    assert _recipes(plan) == [J21]
    assert ("transform: jdk-jakarta scope, pure_java → recipe sets: jdk21; left out: jakarta (no Java EE javax "
            "imports), servlet_jakarta_10 (no jakarta set planned)") in plan.notes


def test_jakarta_not_listed(tmp_path: Path, cfg):
    cfg.framework.recipe_sets = ["jdk21"]
    plan = build_plan(_reduced(analyze_path(make_servlet(tmp_path), cfg)), cfg)
    assert _recipes(plan) == [J21]
    assert _ids(plan) == ["transform.rewrite", "verify.build"]           # no JSP step without the jakarta set


def test_other_set_listed_in_bucket_order(tmp_path: Path, cfg):
    cfg.openrewrite.recipes.spring_framework_7 = [FW7]
    cfg.framework.recipe_sets = ["spring_framework_7", "jakarta", "jdk21"]
    plan = build_plan(_reduced(analyze_path(make_pure_java(tmp_path, javax_ee=True), cfg)), cfg)
    assert _recipes(plan) == [J21, JAKARTA, FW7]
    assert _step(plan, "transform.rewrite").title == "OpenRewrite in one run: Java 21 + jakarta + Spring Framework 7"


def test_msa_union_with_recipe_sets(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    cfg.framework.recipe_sets = ["jdk21", "jakarta", "servlet_jakarta_10"]
    plan = build_plan(_reduced(analyze_path(tmp_path, cfg)), cfg)
    assert _ids(plan) == ["transform.rewrite", "verify.build"]
    assert _recipes(plan) == [J21, JAKARTA, SERVLET10]
