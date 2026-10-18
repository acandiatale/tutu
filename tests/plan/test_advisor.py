"""LLM plan advisor (recipe selection by the cheap recipe model) — additive, whitelist-only, never raises."""
from sbmig.core.interfaces import LLMResponse
from sbmig.core.models import CaseType, LibraryUsage, PomInfo, ProjectProfile, SourceStats
from sbmig.plan import ADVISOR_CATALOG, advise_plan, build_plan


class _LLM:
    def __init__(self, answer, raise_exc=None):
        self.answer, self.raise_exc, self.calls = answer, raise_exc, []
    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})
        if self.raise_exc:
            raise self.raise_exc
        return LLMResponse(content=self.answer, model=model or "default", input_tokens=10, output_tokens=5)


def _profile():
    return ProjectProfile(root="/x", name="p", case_type=CaseType.SPRING_BOOT, spring_boot_version="2.7.18",
                          root_pom=PomInfo(path="pom.xml", artifact_id="p", is_root=True),
                          source=SourceStats(java_files=3, javax_imports={"javax.persistence": 2}),
                          libraries=[LibraryUsage(name="junit4", version="4.12")], fingerprint="f")


def test_advisor_adds_catalog_recipe_uses_recipe_model_and_ignores_unknown(cfg):
    cfg.plan.advisor = "llm"
    cfg.llm.recipe_model = "claude-haiku-4-5"
    cfg.openrewrite.recipes.junit5 = []           # make the planner forget junit5 so the advisor has something to add
    plan = build_plan(_profile(), cfg)
    spring = next(s for s in plan.steps if s.id == "spring.rewrite")
    assert "org.openrewrite.java.testing.junit5.JUnit4to5Migration" not in [r.id for r in spring.recipes]
    llm = _LLM('```json\n{"add": [{"recipe": "org.openrewrite.java.testing.junit5.JUnit4to5Migration", "phase": "spring", '
               '"reason": "junit4 detected"}, {"recipe": "com.evil.DeleteEverything", "phase": "spring", "reason": "x"},'
               '{"recipe": "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0", "phase": "spring", "reason": "dup"}],'
               ' "concerns": ["WebSecurityConfigurerAdapter present"]}\n```')
    out = advise_plan(plan, _profile(), llm=llm, cfg=cfg)
    ids = [r.id for r in next(s for s in out.steps if s.id == "spring.rewrite").recipes]
    assert ids[-1] == "org.openrewrite.java.testing.junit5.JUnit4to5Migration"
    assert ids.count("org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0") == 1
    assert "com.evil.DeleteEverything" not in ids
    assert llm.calls[0]["model"] == "claude-haiku-4-5"
    assert llm.calls[0]["role"] == "plan_advisor" and llm.calls[0]["metadata"] == {"profile": "p"}
    assert any("ignored unknown recipe com.evil.DeleteEverything" in n for n in out.notes)
    assert any("already planned" in n for n in out.notes)
    assert any(n == "advisor concern: WebSecurityConfigurerAdapter present" for n in out.notes)
    added = next(r for r in next(s for s in out.steps if s.id == "spring.rewrite").recipes
                 if r.id.endswith("JUnit4to5Migration"))
    assert added.artifact == "org.openrewrite.recipe:rewrite-testing-frameworks"


def test_advisor_is_safe_on_garbage_errors_and_disabled(cfg):
    cfg.plan.advisor = "llm"
    plan = build_plan(_profile(), cfg)
    before = [(s.id, [r.id for r in s.recipes]) for s in plan.steps]
    out = advise_plan(plan, _profile(), llm=_LLM("I think you should migrate carefully."), cfg=cfg)
    assert [(s.id, [r.id for r in s.recipes]) for s in out.steps] == before
    assert any("not JSON" in n for n in out.notes)
    out = advise_plan(plan, _profile(), llm=_LLM("", raise_exc=RuntimeError("boom")), cfg=cfg)
    assert any("LLM call failed" in n for n in out.notes)
    cfg.plan.advisor = "none"
    assert advise_plan(plan, _profile(), llm=_LLM("{}"), cfg=cfg) is plan
    assert all(rid in ADVISOR_CATALOG for rid in ADVISOR_CATALOG)


def test_advisor_gates_reject_unwarranted_suggestions(cfg):
    """A small model may suggest JUnit4to5Migration without junit4 or Security 7 without spring-security: gated."""
    cfg.plan.advisor = "llm"
    cfg.openrewrite.recipes.junit5 = []
    prof = _profile()
    prof.libraries = []                      # no junit4, no spring-security
    plan = build_plan(prof, cfg)
    llm = _LLM('{"add": [{"recipe": "org.openrewrite.java.testing.junit5.JUnit4to5Migration", "phase": "spring", "reason": "just in case"},'
               '{"recipe": "org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0", "phase": "spring", "reason": "x"}], "concerns": []}')
    out = advise_plan(plan, prof, llm=llm, cfg=cfg)
    ids = [r.id for r in next(s for s in out.steps if s.id == "spring.rewrite").recipes]
    assert "org.openrewrite.java.testing.junit5.JUnit4to5Migration" not in ids
    assert ids.count("org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0") == 0
    # junit: gated (no junit4); security 7: already covered by the planned Boot 4.0 umbrella (coverage check runs first)
    assert sum(1 for n in out.notes if "not warranted" in n) == 1
    assert any("UpgradeSpringSecurity_7_0 already covered by org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0" in n
               for n in out.notes)
