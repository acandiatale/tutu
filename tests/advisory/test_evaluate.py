"""evaluate_advisories / followups_for (guide-only advisories: no recipe policy)."""
from __future__ import annotations


import pytest

import sbmig.advisory as advisory_pkg
from sbmig.advisory import AdvisoryCatalog, evaluate_advisories, followups_for
from sbmig.core.models import AdvisoryFinding, CaseType, LibraryUsage
from tests.advisory.helpers import profile

FOX = "org.openrewrite.java.springdoc.SpringFoxToSpringDoc"
DOC3 = "org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0"


def _guide(f) -> bool:
    """A guide-only finding — robust to `policy` / `policy_reason` / `recipes` / `recipe_source` leaving the model."""
    return (getattr(f, "policy", "guide") == "guide" and not getattr(f, "policy_reason", "")
            and not getattr(f, "recipes", []) and getattr(f, "recipe_source", "none") == "none")


def _cat(*entries) -> AdvisoryCatalog:
    return AdvisoryCatalog.from_dict({"advisories": list(entries)})


FOX_ENTRY = {"id": "springfox", "library": "springfox", "artifacts": ["io.swagger:swagger-annotations"],
             "title": "Springfox is dead", "policy": "recipe", "recipes": [FOX, DOC3],   # v1 keys: ignored
             "replacement": "org.springdoc:springdoc-openapi-starter-webmvc-ui", "replacement_version": "3.x",
             "followup": "Check the swagger UI.", "docs": ["https://springdoc.org"], "notes": "knowledge/notes/x.md"}


def test_library_match_fills_the_finding(cfg):
    p = profile(libs=(LibraryUsage(name="springfox", version="3.0.0",
                                   evidence=["pom:io.springfox:springfox-boot-starter:3.0.0", "import:springfox"]),))
    [f] = evaluate_advisories(p, cfg, _cat(FOX_ENTRY))
    assert (f.advisory_id, f.library, f.status) == ("springfox", "springfox", "eol") and _guide(f)
    assert f.detected_version == "3.0.0" and f.artifacts == ["io.springfox:springfox-boot-starter"]
    assert f.outcome == "unknown"
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
         "applies_when_version_below": "2025.1"}
    lib = lambda v: LibraryUsage(name="spring-cloud-bom", version=v,  # noqa: E731
                                 evidence=[f"pom-managed:org.springframework.cloud:spring-cloud-dependencies:{v}"])
    [f] = evaluate_advisories(profile(libs=(lib("Hoxton.SR12"),)), cfg, _cat(e))      # unparsable → below
    assert f.artifacts == ["org.springframework.cloud:spring-cloud-dependencies"] and _guide(f)
    assert evaluate_advisories(profile(libs=(lib("2021.0.9"),)), cfg, _cat(e))
    assert evaluate_advisories(profile(libs=(lib("2025.1.0"),)), cfg, _cat(e)) == []
    assert evaluate_advisories(profile(libs=(lib(None),)), cfg, _cat(e))               # unknown → below


def test_applies_to_gate(cfg):
    e = {**FOX_ENTRY, "applies_to": ["spring_legacy"]}
    p = profile(libs=(LibraryUsage(name="springfox"),))
    assert evaluate_advisories(p, cfg, _cat(e)) == []
    p2 = profile(libs=(LibraryUsage(name="springfox"),), case=CaseType.SPRING_LEGACY)
    assert len(evaluate_advisories(p2, cfg, _cat(e))) == 1


@pytest.mark.parametrize("override,kept", [("guide", True), ("recipe", True), ("off", False)])
def test_policy_overrides_only_switch_entries_off(cfg, override, kept):
    cfg.advisories.policy_overrides = {"springfox": override}
    out = evaluate_advisories(profile(libs=(LibraryUsage(name="springfox"),)), cfg, _cat(FOX_ENTRY))
    assert bool(out) is kept
    assert all(_guide(f) for f in out)


def test_v1_recipe_keys_change_nothing(cfg):
    """An entry that still carries `policy: recipe` / `recipes:` is guide-only; there is no planner hook any more."""
    [f] = evaluate_advisories(profile(libs=(LibraryUsage(name="springfox"),)), cfg, _cat(FOX_ENTRY))
    assert _guide(f)
    assert not hasattr(advisory_pkg, "advisory_recipe_refs")


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
    assert all(_guide(f) for f in out)


def _finding(aid="springfox", **kw) -> AdvisoryFinding:
    return AdvisoryFinding(advisory_id=aid, library=aid, status="eol", title=aid.title(), policy="guide", **kw)


# ---------------------------------------------------------------------------- follow-ups
def test_followups_not_replaced_first_without_duplicates():
    a = _finding("hystrix", followup="Move to Resilience4j.",
                 replacement="org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j",
                 outcome="still-present")
    b = _finding(followup="Check the UI.", replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui",
                 outcome="replaced")
    c = _finding("log4j1", followup="", outcome="unknown")
    out = followups_for([b, a, c, a])
    assert out == [
        "Hystrix: Move to Resilience4j. (replacement: "
        "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j)",
        "Log4J1",
        "springfox → org.springdoc:springdoc-openapi-starter-webmvc-ui applied by the migration; Check the UI.",
    ]
    assert followups_for([]) == []


# ---------------------------------------------------------------------------------------------- round 10 (D16)
def test_httpclient4_and_jackson1_spring_and_plain_java_twins(cfg):
    libs = (LibraryUsage(name="httpclient4", evidence=["pom:org.apache.httpcomponents:httpclient:4.5.13"]),
            LibraryUsage(name="jackson1", evidence=["pom:org.codehaus.jackson:jackson-mapper-asl:1.9.13"]))
    spring = {f.advisory_id: f for f in evaluate_advisories(profile(libs=libs, case=CaseType.SPRING_LEGACY), cfg)}
    assert {"httpclient4", "jackson1"} <= set(spring) and "httpclient4-direct" not in spring
    assert all(_guide(f) for f in spring.values())
    plain = {f.advisory_id: f for f in evaluate_advisories(profile(libs=libs, case=CaseType.PURE_JAVA), cfg)}
    assert set(plain) >= {"httpclient4-direct", "jackson1-direct"} and "httpclient4" not in plain
    assert _guide(plain["httpclient4-direct"]) and _guide(plain["jackson1-direct"])


def test_undertow_advisory(cfg):
    f = {x.advisory_id: x for x in evaluate_advisories(profile(libs=(LibraryUsage(name="undertow"),)), cfg)}
    assert _guide(f["undertow"]) and "spring-boot-starter-tomcat" in (f["undertow"].replacement or "")
