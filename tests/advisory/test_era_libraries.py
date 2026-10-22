"""2026-10-22 era-library advisories (knowledge/libraries/advisories.yml + knowledge/notes/era-libraries.md)."""
from __future__ import annotations

import re
from pathlib import Path

from sbmig.advisory import advisory_recipe_refs, evaluate_advisories, hints_for, load_advisories
from sbmig.advisory.hints import note_section
from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage
from tests.advisory.helpers import profile

REPO = Path(__file__).resolve().parents[2]
NOTE = REPO / "knowledge" / "notes" / "era-libraries.md"
COLLECTIONS = "org.openrewrite.apache.commons.collections.UpgradeApacheCommonsCollections_3_4"
# `## fix-loop: <library>` sections — keyed on the finding's library (analyzer rule name, or the advisory id for
# artifact-only advisories), which is what sbmig.advisory.hints looks up
FIX_LOOP = ("hibernate-template", "commons-fileupload", "jackson1", "tiles", "velocity", "ehcache2",
            "commons-collections3", "json-lib", "jjwt", "poi", "commons-httpclient3", "axis1", "dbcp1", "itext",
            "slf4j-1-pin")


def _ids(cfg, **kw) -> list[str]:
    return [f.advisory_id for f in evaluate_advisories(profile(**kw), cfg)]


def _lib(name: str, version: str | None = None, ev: str = "pom:x:y") -> LibraryUsage:
    return LibraryUsage(name=name, version=version, evidence=[ev])


def test_era_entries_are_guide_except_collections_and_point_to_existing_notes(cfg):
    cat = load_advisories(cfg)
    era = [e for e in cat.entries if e.notes == "knowledge/notes/era-libraries.md"]
    assert len(era) >= 15
    for e in cat.entries:
        if e.notes:
            assert (REPO / e.notes).is_file(), e.id
        assert e.policy in {"guide", "recipe"}
    assert cat.get("commons-collections3").policy == "recipe" and cat.get("commons-collections3").recipes == [COLLECTIONS]
    assert cat.get("jjwt-legacy").library == "jjwt" and cat.get("poi-legacy").library == "poi"
    assert cat.get("commons-dbcp1").library == "dbcp1"


def test_fix_loop_sections_exist_and_stay_short():
    text = NOTE.read_text(encoding="utf-8")
    for lib in FIX_LOOP:
        title, body = note_section(text, lib)
        assert title == f"fix-loop: {lib}", lib
        assert body and len(body.splitlines()) <= 25, lib
    assert len(text.splitlines()) <= 160


def test_version_gates(cfg):
    assert "jjwt-legacy" in _ids(cfg, libs=(_lib("jjwt", "0.9.1"),))
    assert "jjwt-legacy" not in _ids(cfg, libs=(_lib("jjwt", "0.12.6"),))
    assert "poi-legacy" in _ids(cfg, libs=(_lib("poi", "3.17"),))
    assert "poi-legacy" not in _ids(cfg, libs=(_lib("poi", "5.5.1"),))
    assert "struts2" in _ids(cfg, libs=(_lib("struts2", "2.5.33"),))
    assert "struts2" not in _ids(cfg, libs=(_lib("struts2", "7.4.0"),))
    assert "jdom1" in _ids(cfg, deps=("org.jdom:jdom:1.1.3",))
    assert "jdom1" not in _ids(cfg, deps=("org.jdom:jdom:2.0.2",))                  # org.jdom:jdom 2.x is JDOM 2
    assert "shiro-legacy" in _ids(cfg, deps=("org.apache.shiro:shiro-spring:1.13.0",))
    assert "shiro-legacy" not in _ids(cfg, deps=("org.apache.shiro:shiro-web:3.0.1",))
    assert "hazelcast-legacy" in _ids(cfg, deps=("com.hazelcast:hazelcast:3.12.12",))
    assert "hazelcast-legacy" not in _ids(cfg, deps=("com.hazelcast:hazelcast:5.5.0",))
    assert "jasperreports6" not in _ids(cfg, deps=("net.sf.jasperreports:jasperreports:7.1.0",))
    assert "hibernate-search-legacy" in _ids(cfg, deps=("org.hibernate:hibernate-search-orm:5.11.12.Final",))
    assert "hibernate-search-legacy" not in _ids(cfg, deps=("org.hibernate.search:hibernate-search-mapper-orm:8.4.0.Final",))


def test_no_noise_from_unversioned_managed_artifacts(cfg):
    ids = _ids(cfg, deps=("org.slf4j:slf4j-api", "org.mockito:mockito-core", "io.hypersistence:hypersistence-utils-hibernate-73:3.16.0",
                          "com.github.dozermapper:dozer-core:7.0.0", "org.springframework:spring-orm"))
    assert not {"slf4j-1-pin", "mockito-legacy", "hypersistence-utils", "dozer", "hibernate-template"} & set(ids)
    assert "slf4j-1-pin" in _ids(cfg, deps=("org.slf4j:jcl-over-slf4j",))
    assert "mockito-legacy" in _ids(cfg, deps=("org.mockito:mockito-all:1.10.19",))
    assert "hypersistence-utils" in _ids(cfg, deps=("com.vladmihalcea:hibernate-types-52:2.21.1",))


def test_commons_collections3_recipe_is_added_to_spring_rewrite(cfg):
    p = profile(libs=(_lib("commons-collections3", "3.2.2"),), case=CaseType.SPRING_LEGACY)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "commons-collections3"]
    assert f.policy == "recipe" and f.recipes == [COLLECTIONS]
    refs = advisory_recipe_refs([f], cfg, planned_ids=["org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"])
    assert [r.id for r in refs] == [COLLECTIONS] and refs[0].artifact == "org.openrewrite.recipe:rewrite-apache"
    assert f.recipe_source == "advisory"


def _hint(cfg, library: str, message: str, advisory: str, version: str | None = None, ev: str = "import:x"):
    p = profile(libs=(_lib(library, version, ev),), case=CaseType.SPRING_LEGACY)
    findings = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == advisory]
    assert findings, advisory
    e = BuildError(kind=ErrorKind.COMPILE, message=message, file="src/main/java/com/acme/A.java")
    return hints_for([e], findings)


def test_hint_hibernate_template_carries_the_fix_loop_note(cfg):
    hints, ids = _hint(cfg, "hibernate-template", "package org.springframework.orm.hibernate5 does not exist",
                       "hibernate-template", ev="import:org.springframework.orm.hibernate5")
    assert ids == ["hibernate-template"]
    assert hints[0].startswith("hibernate-template (incompatible, policy guide): replace with "
                               "org.springframework.orm.jpa.hibernate.LocalSessionFactoryBean")
    assert "Note (era-libraries.md § fix-loop: hibernate-template)" in hints[0]
    assert "getCurrentSession()" in hints[0]


def test_hint_keys_on_the_rule_name_not_the_advisory_id(cfg):
    hints, ids = _hint(cfg, "jjwt", "cannot find symbol: method parserBuilder() location: class io.jsonwebtoken.Jwts",
                       "jjwt-legacy", version="0.9.1")
    assert ids == ["jjwt-legacy"] and "Note (era-libraries.md § fix-loop: jjwt)" in hints[0]
    hints, ids = _hint(cfg, "commons-fileupload", "package org.apache.commons.fileupload.servlet does not exist",
                       "commons-fileupload")
    assert ids == ["commons-fileupload"] and "JakartaServletFileUpload" in hints[0]
    # segment boundary: an error about collections4 is not a commons-collections3 cluster
    hints, ids = _hint(cfg, "commons-collections3", "package org.apache.commons.collections4.map does not exist",
                       "commons-collections3", version="3.2.2")
    assert ids == [] or "commons-collections4" in hints[0]                          # matched only via the replacement


def test_note_keywords_select_the_era_note():
    from sbmig.fix.context import load_notes_index

    by = {e.file: e for e in load_notes_index(REPO / "knowledge" / "notes")}
    n = by["era-libraries.md"]
    assert n.score("package org.springframework.orm.hibernate5 does not exist") > 0
    assert n.score("package org.codehaus.jackson.type does not exist") > 0
    assert n.score("package org.apache.commons.collections does not exist") > 0
    assert n.score("package org.apache.commons.collections4 does not exist") == 0
    assert n.score("package org.apache.commons.dbcp2 does not exist") == 0
    assert n.score("package org.apache.axis2.client does not exist") == 0
    assert re.search(r"fix-loop: hibernate-template", n.body())
