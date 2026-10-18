"""evaluate_advisories / advisory_recipe_refs / followups_for."""
from __future__ import annotations


import pytest

from sbmig.advisory import AdvisoryCatalog, advisory_recipe_refs, evaluate_advisories, followups_for
from sbmig.core.models import AdvisoryFinding, CaseType, LibraryUsage
from tests.advisory.helpers import profile

FOX = "org.openrewrite.java.springdoc.SpringFoxToSpringDoc"
DOC3 = "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
CLOUD = "org.openrewrite.java.spring.cloud2025.UpgradeSpringCloud_2025_1"


def _cat(*entries) -> AdvisoryCatalog:
    return AdvisoryCatalog.from_dict({"advisories": list(entries)})


FOX_ENTRY = {"id": "springfox", "library": "springfox", "artifacts": ["io.swagger:swagger-annotations"],
             "title": "Springfox is dead", "policy": "recipe", "recipes": [FOX, DOC3],
             "replacement": "org.springdoc:springdoc-openapi-starter-webmvc-ui", "replacement_version": "3.x",
             "followup": "Check the swagger UI.", "docs": ["https://springdoc.org"], "notes": "knowledge/notes/x.md"}


def test_library_match_fills_the_finding(cfg):
    p = profile(libs=(LibraryUsage(name="springfox", version="3.0.0",
                                   evidence=["pom:io.springfox:springfox-boot-starter:3.0.0", "import:springfox"]),))
    [f] = evaluate_advisories(p, cfg, _cat(FOX_ENTRY))
    assert (f.advisory_id, f.library, f.status, f.policy, f.policy_reason) == \
        ("springfox", "springfox", "eol", "recipe", "catalog")
    assert f.detected_version == "3.0.0" and f.artifacts == ["io.springfox:springfox-boot-starter"]
    assert f.recipes == [FOX, DOC3] and f.recipe_source == "none" and f.outcome == "unknown"
    assert f.replacement_version == "3.x" and f.followup == "Check the swagger UI." and f.docs
    assert f.notes == ["see knowledge/notes/x.md"]


def test_artifact_glob_match_on_deps_managed_and_parent(cfg):
    e = {"id": "acme", "artifacts": ["com.acme:legacy-*"], "title": "Acme legacy", "status": "unmanaged"}
    [f] = evaluate_advisories(profile(deps=("com.acme:legacy-core:1.2",)), cfg, _cat(e))
    assert f.library == "acme" and f.detected_version == "1.2" and f.artifacts == ["com.acme:legacy-core"]
    assert f.evidence == ["pom:com.acme:legacy-core:1.2"]
    [f] = evaluate_advisories(profile(managed=("com.acme:legacy-bom:${v}",)), cfg, _cat(e))
    assert f.detected_version is None                                   # concrete versions only
    [f] = evaluate_advisories(profile(parent="com.acme:legacy-parent:7"), cfg, _cat(e))
    assert f.detected_version == "7"
    assert evaluate_advisories(profile(deps=("com.acme:modern:1",)), cfg, _cat(e)) == []
    # the library entry also matches through its extra artifacts
    [f] = evaluate_advisories(profile(deps=("io.swagger:swagger-annotations:1.6.2",)), cfg, _cat(FOX_ENTRY))
    assert f.library == "springfox" and f.detected_version == "1.6.2"


def test_managed_and_parent_library_rules(cfg):
    e = {"id": "spring-cloud-bom", "library": "spring-cloud-bom", "title": "old train", "status": "superseded",
         "policy": "recipe", "recipes": [CLOUD], "applies_when_version_below": "2025.1"}
    lib = lambda v: LibraryUsage(name="spring-cloud-bom", version=v,  # noqa: E731
                                 evidence=[f"pom-managed:org.springframework.cloud:spring-cloud-dependencies:{v}"])
    [f] = evaluate_advisories(profile(libs=(lib("Hoxton.SR12"),)), cfg, _cat(e))      # unparsable → below
    assert f.artifacts == ["org.springframework.cloud:spring-cloud-dependencies"] and f.policy == "recipe"
    assert evaluate_advisories(profile(libs=(lib("2021.0.9"),)), cfg, _cat(e))
    assert evaluate_advisories(profile(libs=(lib("2025.1.0"),)), cfg, _cat(e)) == []
    assert evaluate_advisories(profile(libs=(lib(None),)), cfg, _cat(e))               # unknown → below


def test_applies_to_gate(cfg):
    e = {**FOX_ENTRY, "applies_to": ["spring_legacy"]}
    p = profile(libs=(LibraryUsage(name="springfox"),))
    assert evaluate_advisories(p, cfg, _cat(e)) == []
    p2 = profile(libs=(LibraryUsage(name="springfox"),), case=CaseType.SPRING_LEGACY)
    assert len(evaluate_advisories(p2, cfg, _cat(e))) == 1


@pytest.mark.parametrize("override,policy,reason", [
    ("guide", "guide", "config override"), ("recipe", "recipe", "config override"), ("off", None, None)])
def test_policy_overrides(cfg, override, policy, reason):
    cfg.advisories.policy_overrides = {"springfox": override}
    out = evaluate_advisories(profile(libs=(LibraryUsage(name="springfox"),)), cfg, _cat(FOX_ENTRY))
    if policy is None:
        assert out == []
    else:
        assert (out[0].policy, out[0].policy_reason) == (policy, reason)
        assert out[0].recipes == ([FOX, DOC3] if policy == "recipe" else [])


def test_override_to_recipe_without_recipes_stays_guide(cfg):
    cfg.advisories.policy_overrides = {"h": "recipe"}
    [f] = evaluate_advisories(profile(libs=(LibraryUsage(name="hystrix"),)), cfg,
                              _cat({"id": "h", "library": "hystrix", "title": "H"}))
    assert f.policy == "guide" and "no recipes" in f.policy_reason


def test_allow_recipes_false_and_unavailable_recipe(cfg):
    p = profile(libs=(LibraryUsage(name="springfox"),))
    cfg.advisories.allow_recipes = False
    [f] = evaluate_advisories(p, cfg, _cat(FOX_ENTRY))
    assert (f.policy, f.policy_reason, f.recipes) == ("guide", "advisories.allow_recipes=false", [])
    cfg.advisories.allow_recipes = True
    [f] = evaluate_advisories(p, cfg, _cat({**FOX_ENTRY, "recipes": [FOX, "org.example.Gone"]}))
    assert (f.policy, f.policy_reason) == ("guide", "recipe org.example.Gone unavailable → guide")
    cfg.openrewrite.recipe_artifacts = []
    [f] = evaluate_advisories(p, cfg, _cat(FOX_ENTRY))
    assert f.policy_reason == f"recipe {FOX} unavailable → guide"


def test_disabled_order_and_dedupe(cfg):
    cat = _cat({"id": "z-super", "library": "httpclient4", "title": "Z", "status": "superseded"},
               {"id": "b-eol", "library": "log4j1", "title": "B"},
               {"id": "a-unm", "library": "log4j1", "title": "A", "status": "unmanaged"},
               {"id": "c-inc", "library": "log4j1", "title": "C", "status": "incompatible"},
               {"id": "a-eol", "library": "log4j1", "title": "A2"})
    p = profile(libs=(LibraryUsage(name="log4j1"), LibraryUsage(name="httpclient4")))
    assert [f.advisory_id for f in evaluate_advisories(p, cfg, cat)] == ["a-eol", "b-eol", "c-inc", "a-unm", "z-super"]
    cfg.advisories.enabled = False
    assert evaluate_advisories(p, cfg, cat) == []


def test_repo_catalog_on_a_cloud_profile(cfg):
    p = profile(libs=(LibraryUsage(name="springfox", version="3.0.0"), LibraryUsage(name="hystrix"),
                      LibraryUsage(name="spring-cloud-bom", version="2021.0.9")))
    out = evaluate_advisories(p, cfg)
    assert [f.advisory_id for f in out] == ["hystrix", "springfox", "spring-cloud-bom"]
    assert {f.advisory_id: f.policy for f in out} == {"hystrix": "guide", "springfox": "recipe",
                                                      "spring-cloud-bom": "recipe"}


# ---------------------------------------------------------------------------- recipe refs
def _finding(aid="springfox", policy="recipe", recipes=(FOX, DOC3), **kw) -> AdvisoryFinding:
    return AdvisoryFinding(advisory_id=aid, library=aid, status="eol", title=aid.title(), policy=policy,
                           recipes=list(recipes), **kw)


def test_recipe_refs_covered_by_the_umbrella(cfg):
    f, g = _finding(), _finding("hystrix", policy="guide", recipes=())
    assert advisory_recipe_refs([f, g], cfg, [BOOT4, "x.Y"]) == []
    assert f.recipe_source == "plan" and g.recipe_source == "none"
    assert any(f"included in {BOOT4}" in n for n in f.notes)


def test_recipe_refs_added_and_deduped(cfg):
    f, f2 = _finding(), _finding("swagger", recipes=(DOC3,))
    refs = advisory_recipe_refs([f, f2], cfg, ["org.openrewrite.java.spring.framework.UpgradeSpringFramework_7_0"])
    assert [r.id for r in refs] == [FOX, DOC3]
    assert all(r.source == "builtin" and r.artifact == "org.openrewrite.recipe:rewrite-spring" for r in refs)
    assert f.recipe_source == f2.recipe_source == "advisory"
    assert any("added" in n for n in f.notes)
    # partially planned: only the missing one is added
    f3 = _finding()
    assert [r.id for r in advisory_recipe_refs([f3], cfg, [FOX])] == [DOC3]
    assert f3.recipe_source == "advisory" and any("planned directly" in n for n in f3.notes)
    # sticky across steps (MSA): a later covered step does not flip it back
    advisory_recipe_refs([f], cfg, [BOOT4])
    assert f.recipe_source == "advisory"


def test_recipe_refs_project_recipe_and_empty(cfg):
    f = _finding("x", recipes=("com.company.migrate.SpringBoot41Version",))
    [r] = advisory_recipe_refs([f], cfg, [])
    assert r.source == "project" and r.file and r.file.endswith("spring-boot-41-version.yml") and r.artifact is None
    assert advisory_recipe_refs([], cfg, []) == [] and advisory_recipe_refs(None, cfg, []) == []


# ---------------------------------------------------------------------------- follow-ups
def test_followups_not_replaced_first_without_duplicates():
    a = _finding("hystrix", policy="guide", recipes=(), followup="Move to Resilience4j.",
                 replacement="org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j",
                 outcome="still-present")
    b = _finding(followup="Check the UI.", replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui",
                 outcome="replaced")
    c = _finding("log4j1", policy="guide", recipes=(), followup="", outcome="unknown")
    out = followups_for([b, a, c, a])
    assert out == [
        "Hystrix: Move to Resilience4j. (replacement: "
        "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j)",
        "Log4J1",
        "springfox → org.springdoc:springdoc-openapi-starter-webmvc-ui applied by SpringFoxToSpringDoc, "
        "UpgradeSpringDoc_3_0; Check the UI.",
    ]
    assert followups_for([]) == []
