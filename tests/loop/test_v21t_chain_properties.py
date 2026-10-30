"""v2.1t, fix-loop side (docs/INTERFACES.md "v2.1t", "Prompt facts and rule (fix loop)"): the parent chain's own version
properties (`load_parent_chain_facts(cfg).properties` + `property_sources`) reach the prompt as `parent chain property:`
lines for the case's artifacts (artifactId / stem, the group's family keyword, the in-house family), and an in-house
dependency the LLM added gets `${<that property>}` instead of a literal (`_fill_versions`, source `chain-property`)."""
from __future__ import annotations

import pytest

from sbmig.core.config import ArtifactSpec
from sbmig.core.models import GAV, BuildError, ErrorCluster, ErrorKind, Phase
from sbmig.loop.fixloop import artifact_stem, version_property_matches

from .fakes import FakeFixer, answer, journal_events
from .test_v21l_versions import FOO, Lookup, _captures, _pom, _versions, _ws, make_loop, pom_build, use_lookup

BASE = "com.company.platform:base-parent:4.0.0"
BOOT = "org.springframework.boot:spring-boot-dependencies:4.1.0"
PROPS = {"spring-boot.version": "4.1.0", "spring-framework.version": "7.0.8", "spring-cloud.version": "2025.1.0",
         "spring-data.version": "2025.1.0", "xxx-reuse.version": "4.0.0", "lombok.version": "1.18.36",
         "snakeyaml.version": "2.4", "java.version": "21", "project.build.sourceEncoding": "UTF-8",
         "commons-lang3.version": "3.18.0", "xxx-mid.version": "4.0.1"}
# the company's own poms define most of them; the Boot BOM (the add-on / a Boot parent) lombok and commons-lang3, an
# in-house sub-group's parent xxx-mid
SOURCES = {**{k: BASE for k in PROPS}, "lombok.version": BOOT, "commons-lang3.version": BOOT,
           "xxx-mid.version": "com.company.platform.mid:mid-parent:4.0.1"}
REUSE_WEB = "com.company.platform:xxx-reuse-web"
LANG3_EXT = "com.company.platform:commons-lang3-ext"     # in-house, its family property is the Boot BOM's
REUSE_CORE = "com.company.platform:xxx-reuse-core"
OTHER = "com.company.platform:other-lib"
MANAGED = "com.company.platform:managed-lib"
YAML = "org.yaml:snakeyaml"


def gav(key: str, version: str | None = None) -> GAV:
    g, a = key.split(":")
    return GAV(group_id=g, artifact_id=a, version=version)


@pytest.fixture()
def company(cfg, monkeypatch):
    """The company parent (in-house group `com.company.platform`) and its chain facts: the properties above, its own
    managed `managed-lib`."""
    from sbmig.build import parent_chain
    cfg = cfg.model_copy(deep=True)
    cfg.artifacts.parent = ArtifactSpec(group_id="com.company.platform", artifact_id="base-parent", version="4.0.0")
    facts = parent_chain.ParentChainFacts(roots=[BASE], managed={MANAGED: "4.0.0"}, properties=dict(PROPS),
                                          property_sources=dict(SOURCES), boot_bom_added=True, chain_managed={
                                              MANAGED: "4.0.0"})
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)
    return cfg


# ------------------------------------------------------------------------------------------------ matching
def test_version_properties_match_by_stem_family_and_in_house_family():
    names = list(PROPS)
    assert artifact_stem("spring-boot-starter") == "spring-boot" and artifact_stem("x-bom") == "x"
    assert version_property_matches(gav("org.springframework.boot:spring-boot-starter-web"), names) == [
        "spring-boot.version"]                                                # the group's family keyword
    assert version_property_matches(gav("org.springframework.boot:spring-boot-starter"), names) == [
        "spring-boot.version"]                                                # the stem
    assert version_property_matches(gav("org.springframework:spring-webmvc"), names) == ["spring-framework.version"]
    assert version_property_matches(gav("org.springframework.cloud:spring-cloud-starter-openfeign"), names) == [
        "spring-cloud.version"]
    assert version_property_matches(gav("org.springframework.data:spring-data-jpa"), names) == ["spring-data.version"]
    assert version_property_matches(gav("org.springframework.security:spring-security-web"), names) == []
    assert version_property_matches(gav("org.projectlombok:lombok"), names) == ["lombok.version"]
    assert version_property_matches(gav(REUSE_WEB), names, in_house=True) == ["xxx-reuse.version"]
    assert version_property_matches(gav(REUSE_WEB), names) == []              # the family rule is in-house only
    assert version_property_matches(gav(OTHER), names, in_house=True) == []
    assert version_property_matches(gav("x:java"), names) == ["java.version"]  # a plain artifactId match


# ------------------------------------------------------------------------------------------------ prompt facts
def test_the_chain_properties_of_the_cases_artifacts_reach_the_prompt(company, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    loop = make_loop(company, ws, FakeFixer(), pom_build())
    errs = [BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                       artifact=gav(k), signature_generic=f"dependency_resolution|{k}")
            for k in ("org.springframework.boot:spring-boot-starter-web", "org.springframework:spring-webmvc",
                      REUSE_WEB, "org.springframework.boot:spring-boot-starter-validation", "org.projectlombok:lombok",
                      "org.apache.commons:commons-lang3", "com.company.platform:xxx-mid-api")]
    c = ErrorCluster(id="case:x", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=errs)
    hints = loop._library_hints(c)
    assert [h for h in hints if h.startswith("parent chain property:")] == [
        f"parent chain property: spring-boot.version=4.1.0 ({BASE})",
        f"parent chain property: spring-framework.version=7.0.8 ({BASE})",
        f"parent chain property: xxx-reuse.version=4.0.0 ({BASE})",
        "parent chain property: xxx-mid.version=4.0.1 (com.company.platform.mid:mid-parent:4.0.1)"]
    # once each, no unrelated property — and none the Boot BOM defines (lombok / commons-lang3 would not resolve on the
    # company chain)
    assert not [h for h in hints if "lombok.version" in h or "commons-lang3.version" in h]


def test_only_the_company_chains_properties_count(cfg):
    from sbmig.build import parent_chain
    from sbmig.loop.fixloop import chain_company_properties
    cfg = cfg.model_copy(deep=True)
    cfg.artifacts.parent = ArtifactSpec(group_id="com.company.platform", artifact_id="base-parent", version="4.0.0")
    facts = parent_chain.ParentChainFacts(roots=[BASE], properties=dict(PROPS), property_sources=dict(SOURCES))
    props, sources = chain_company_properties(facts, cfg)
    assert "lombok.version" not in props and "commons-lang3.version" not in props
    assert props["xxx-reuse.version"] == "4.0.0" and sources["xxx-mid.version"].startswith("com.company.platform.mid:")
    # a spring-boot-starter-parent chain (the local e2e config's parent): nothing of it is the company's
    boot_chain = parent_chain.ParentChainFacts(roots=["org.springframework.boot:spring-boot-starter-parent:4.1.0"],
                                               properties={"lombok.version": "1.18.36"},
                                               property_sources={"lombok.version": BOOT})
    assert chain_company_properties(boot_chain, cfg) == ({}, {})
    # the loader's own split wins when it filled it
    split = facts.model_copy(update={"company_properties": {"xxx-reuse.version": "4.0.0"},
                                     "company_property_sources": {"xxx-reuse.version": BASE}})
    assert chain_company_properties(split, cfg) == ({"xxx-reuse.version": "4.0.0"}, {"xxx-reuse.version": BASE})
    # a property without a known defining pom does not count either
    assert chain_company_properties(parent_chain.ParentChainFacts(roots=[BASE], properties={"x.version": "1"}),
                                    cfg) == ({}, {})


def test_no_chain_facts_no_property_lines(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    c = ErrorCluster(id="case:x", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=[
        BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="x", artifact=gav("org.springframework:spring-core"),
                   signature_generic="dependency_resolution|x")])
    assert loop._chain_property_facts(c) == []


# ------------------------------------------------------------------------------------------------ _fill_versions
def test_an_added_in_house_dependency_references_the_chain_property(company, tmp_path, monkeypatch):
    use_lookup(monkeypatch, Lookup({YAML: ["2.3"], OTHER: ["3.1.0"], LANG3_EXT: ["1.2.0"]}))
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    _captures(ws, {"direct": {OTHER: "3.0.0"}})          # no post-restructure capture: the chain facts decide step 1

    def add(key: str, version: str | None) -> dict:
        g, a = key.split(":")
        return {"group_id": g, "artifact_id": a, "version": version}

    fixer = FakeFixer(scripts=[answer(edits=[
        {"op": "write", "path": FOO, "content": "class Foo { ok }\n"},
        {"op": "pom", "path": "pom.xml", "pom": {"add_dependency": [
            add(REUSE_WEB, None), add(REUSE_CORE, "9.9"), add(OTHER, None), add(MANAGED, "1.0"), add(YAML, None),
            add(LANG3_EXT, None)]}}])])
    pr = make_loop(company, ws, fixer, pom_build(managed=frozenset({MANAGED}), broken_file=FOO)).run(
        Phase.VERIFY, "package", 3)
    assert pr.ok
    assert _versions(ws) == {
        REUSE_WEB: "${xxx-reuse.version}",                # in-house family property, not a literal
        REUSE_CORE: "${xxx-reuse.version}",               # the LLM's literal 9.9 replaced
        OTHER: "3.0.0",                                   # in-house, no matching property: the resolver's (as-is)
        MANAGED: None,                                    # the chain manages it: the version goes
        YAML: "2.3",                                      # not in-house: the resolver's result, even with a property
        LANG3_EXT: "1.2.0",                               # commons-lang3.version is the Boot BOM's: never ${…}
    }
    ev = {e["key"]: e for e in journal_events(ws) if e["event"] == "version_filled"}
    assert (ev[REUSE_WEB]["source"], ev[REUSE_WEB]["property"], ev[REUSE_WEB]["version"]) == (
        "chain-property", "xxx-reuse.version", "${xxx-reuse.version}")
    assert ev[REUSE_CORE]["replaced"] == "9.9" and "replaced" not in ev[REUSE_WEB]
    assert ev[OTHER]["source"] == "asis" and ev[MANAGED]["source"] == "managed" and ev[YAML]["source"] == "repository"
    [att] = pr.attempts
    assert (f"version filled by sbmig: {REUSE_WEB} ${{xxx-reuse.version}} in pom.xml (a version property of the "
            "parent chain)") in att.notes
