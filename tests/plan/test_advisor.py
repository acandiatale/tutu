"""LLM plan advisor (recipe selection by the cheap recipe model) — additive, whitelist-only, never raises.

v2: the whitelist is the advisor-eligible catalog recipes inside the configured sets + extras, and additions go to the
transform.rewrite step(s)."""
from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import CaseType, LibraryUsage, PomInfo, ProjectProfile, SourceStats
from sbmig.plan import ADVISOR_CATALOG, advise_plan, advisor_whitelist, build_plan, v2_recipe_scope

JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"
JUNIT5 = "org.openrewrite.java.testing.junit5.JUnit4to5Migration"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
SEC7 = "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0"
FW7 = "org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"


class _LLM:
    def __init__(self, answer, raise_exc=None):
        self.answer, self.raise_exc, self.calls = answer, raise_exc, []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        if self.raise_exc:
            raise self.raise_exc
        return LLMResponse(content=self.answer, model=model or "default", input_tokens=10, output_tokens=5)


def _boot_profile():
    return ProjectProfile(root="/x", name="p", case_type=CaseType.SPRING_BOOT, spring_boot_version="2.7.18",
                          root_pom=PomInfo(path="pom.xml", artifact_id="p", is_root=True),
                          source=SourceStats(java_files=3, javax_imports={"javax.persistence": 2}),
                          libraries=[LibraryUsage(name="junit4", version="4.12")], fingerprint="f")


def _pure_java_profile():
    """javax.annotation without the analyzer's EE confirmation: the planner runs no jakarta set, the catalog gate
    (has_javax_ee) still lets the advisor add it."""
    return ProjectProfile(root="/x", name="p", case_type=CaseType.PURE_JAVA,
                          root_pom=PomInfo(path="pom.xml", artifact_id="p", is_root=True),
                          source=SourceStats(java_files=3, javax_imports={"javax.annotation": 2}), fingerprint="f")


def _transform(plan):
    return [r.id for r in next(s for s in plan.steps if s.id == "transform.rewrite").recipes]


def test_whitelist_is_the_v2_scope(cfg):
    wl = advisor_whitelist(cfg)
    assert set(wl) <= set(ADVISOR_CATALOG) and set(wl) <= v2_recipe_scope(cfg)
    assert JAKARTA in wl and BOOT4 in wl
    assert JUNIT5 not in wl                                                 # the JUnit 5 set stays empty
    assert SEC7 in wl and FW7 in wl                                         # v2.1l / v2.1o: default sets
    cfg.openrewrite.recipes.junit5 = [JUNIT5]
    cfg.openrewrite.recipes.extra = {"spring_legacy": [FW7]}
    wl = advisor_whitelist(cfg)
    assert JUNIT5 in wl and FW7 in wl                                       # company opt-in / extras widen it
    assert JAKARTA in advisor_whitelist()                                   # no cfg: the default v2 sets


def test_advisor_adds_in_scope_recipe_uses_recipe_model_and_ignores_the_rest(cfg):
    cfg.plan.advisor = "llm"
    cfg.llm.recipe_model = "claude-haiku-4-5"
    plan = build_plan(_pure_java_profile(), cfg)
    assert _transform(plan) == ["org.openrewrite.java.migrate.UpgradeToJava21"]
    llm = _LLM('```json\n{"add": [{"recipe": "%s", "reason": "javax.annotation"}, '
               '{"recipe": "com.evil.DeleteEverything", "reason": "x"},'
               '{"recipe": "%s", "phase": "spring", "reason": "junit"},'
               '{"recipe": "%s", "reason": "dup"}],'
               ' "concerns": ["WebSecurityConfigurerAdapter present"]}\n```' % (JAKARTA, JUNIT5, JAKARTA))
    out = advise_plan(plan, _pure_java_profile(), llm=llm, cfg=cfg)
    ids = _transform(out)
    assert ids[-1] == JAKARTA and ids.count(JAKARTA) == 1
    assert "com.evil.DeleteEverything" not in ids and JUNIT5 not in ids
    assert llm.calls[0]["model"] == "claude-haiku-4-5"
    assert llm.calls[0]["role"] == "plan_advisor" and llm.calls[0]["metadata"] == {"profile": "p"}
    prompt = llm.calls[0]["messages"][1].content
    assert "transform.rewrite [transform]" in prompt and JUNIT5 not in prompt.split("ALREADY COVERED")[0]
    assert any("ignored unknown recipe com.evil.DeleteEverything" in n for n in out.notes)
    assert any(f"ignored unknown recipe {JUNIT5}" in n for n in out.notes)          # outside the v2 scope
    assert any(f"advisor: {JAKARTA} already planned" in n for n in out.notes)
    assert f"advisor: +{JAKARTA} → transform.rewrite (javax.annotation)" in out.notes
    assert any(n == "advisor concern: WebSecurityConfigurerAdapter present" for n in out.notes)
    added = next(r for r in next(s for s in out.steps if s.id == "transform.rewrite").recipes if r.id == JAKARTA)
    assert added.artifact == "org.openrewrite.recipe:rewrite-migrate-java"


def test_advisor_attaches_to_every_msa_transform_step(cfg):
    from sbmig.core.models import MigrationPlan, Phase, PlanStep, StepKind
    cfg.plan.advisor = "llm"
    steps = [PlanStep(id=f"transform.rewrite@{s}", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title=s)
             for s in ("a", "b")]
    plan = MigrationPlan(profile_fingerprint="f", case_type=CaseType.MSA, steps=steps)
    out = advise_plan(plan, _pure_java_profile(), llm=_LLM('{"add": [{"recipe": "%s"}]}' % JAKARTA), cfg=cfg)
    assert all([r.id for r in s.recipes] == [JAKARTA] for s in out.steps)
    empty = MigrationPlan(profile_fingerprint="f", case_type=CaseType.PURE_JAVA, steps=[])
    out = advise_plan(empty, _pure_java_profile(), llm=_LLM('{"add": [{"recipe": "%s"}]}' % JAKARTA), cfg=cfg)
    assert any(f"advisor: no transform.rewrite step to attach {JAKARTA}; ignored" in n for n in out.notes)


def test_advisor_is_safe_on_garbage_errors_and_disabled(cfg):
    cfg.plan.advisor = "llm"
    plan = build_plan(_boot_profile(), cfg)
    before = [(s.id, [r.id for r in s.recipes]) for s in plan.steps]
    out = advise_plan(plan, _boot_profile(), llm=_LLM("I think you should migrate carefully."), cfg=cfg)
    assert [(s.id, [r.id for r in s.recipes]) for s in out.steps] == before
    assert any("not JSON" in n for n in out.notes)
    out = advise_plan(plan, _boot_profile(), llm=_LLM("", raise_exc=RuntimeError("boom")), cfg=cfg)
    assert any("LLM call failed" in n for n in out.notes)
    cfg.plan.advisor = "none"
    assert advise_plan(plan, _boot_profile(), llm=_LLM("{}"), cfg=cfg) is plan


def test_advisor_gates_reject_unwarranted_suggestions(cfg):
    """A small model may suggest JUnit4to5Migration without junit4 or Security 7 without spring-security: gated
    (a company config that opted into both sets, so both are inside the whitelist)."""
    cfg.plan.advisor = "llm"
    cfg.openrewrite.recipes.junit5 = [JUNIT5]
    cfg.openrewrite.recipes.spring_security_7 = [SEC7]
    prof = _boot_profile()
    prof.libraries = []                      # no junit4, no spring-security
    plan = build_plan(prof, cfg)
    assert JUNIT5 not in _transform(plan)
    llm = _LLM('{"add": [{"recipe": "%s", "reason": "just in case"}, {"recipe": "%s", "reason": "x"}], '
               '"concerns": []}' % (JUNIT5, SEC7))
    out = advise_plan(plan, prof, llm=llm, cfg=cfg)
    ids = _transform(out)
    assert JUNIT5 not in ids and SEC7 not in ids
    # junit: gated (no junit4); security 7: already covered by the planned Boot 4.0 umbrella (coverage check runs first)
    assert sum(1 for n in out.notes if "not warranted" in n) == 1
    assert any(f"{SEC7} already covered by {BOOT4}" in n for n in out.notes)
