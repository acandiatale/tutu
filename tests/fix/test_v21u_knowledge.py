"""v2.1u / v2.1w knowledge clean-up (docs/INTERFACES.md "v2.1u … v2.1y", review C W1–W12 / O2–O16, B-M7, E-H5):
the migration notes and the library tables the LLM reads must not name deleted v1 handlers, must not present an
external Tomcat as the target, must not keep an always-note, and must state the corrected facts."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from sbmig.fix import load_notes_index
from sbmig.fix.context import split_front_matter

REPO = Path(__file__).resolve().parents[2]
NOTES = REPO / "knowledge" / "notes"
LIBRARIES = REPO / "knowledge" / "libraries"

# v1 handlers / shims deleted in v2 step 4 (2026-10-27) — text that names them misleads the LLM and the owners
V1_NAMES = ("sbmig.compat", "add-known-dependency", "jdk-base64-shim", "querydsl-jakarta-classifier",
            "jakarta-library-floor", "carry-over-managed-version")
# "handler <kebab-case id>" (optionally in backticks) — how the v1 text referred to a handler
HANDLER_ID = re.compile(r"\bhandler\s+`?[a-z][a-z0-9]*(?:-[a-z0-9]+)+`?")


def _note_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(NOTES.glob("*.md"))}


def _knowledge_texts() -> dict[str, str]:
    out = {f"notes/{k}": v for k, v in _note_texts().items()}
    out.update({f"libraries/{p.name}": p.read_text(encoding="utf-8") for p in sorted(LIBRARIES.glob("*.yml"))})
    return out


def test_no_v1_handler_wording_in_notes_or_library_tables():
    for name, text in _knowledge_texts().items():
        for v1 in V1_NAMES:
            assert v1 not in text, (name, v1)
        m = HANDLER_ID.search(text)
        assert m is None, (name, m.group(0) if m else None)


def test_no_always_note_and_fix_policy_is_gone():
    raw = yaml.safe_load((NOTES / "index.yml").read_text(encoding="utf-8"))["notes"]
    assert all(item.get("always") is False for item in raw)
    assert "fix-policy.md" not in {item["file"] for item in raw}
    assert not (NOTES / "fix-policy.md").exists()
    assert not re.search(r"(?m)^always:\s*true", (NOTES / "index.yml").read_text(encoding="utf-8"))
    for name, text in _note_texts().items():
        meta, _ = split_front_matter(text)
        assert not meta.get("always"), name


def test_external_tomcat_is_only_the_alternative_or_the_as_is():
    """v2.1q: the converted JSP case is an executable WAR (`java -jar`); a note may name the external Tomcat only as
    the as-is container or as the alternative deployment of the same file."""
    allowed = re.compile(r"(?i)as-is|used to|alternative|also deploys|instead")
    for name, text in _note_texts().items():
        flat = re.sub(r"\s+", " ", text)          # phrases wrap across lines
        for m in re.finditer(r"(?i)external[ -]Tomcat", flat):
            window = flat[max(0, m.start() - 120): m.end() + 60]
            assert allowed.search(window), (name, window)
        assert "Packaged as a WAR for an external Tomcat" not in text, name
        assert "JSPs served by an external Tomcat" not in text, name


def test_new_notes_and_their_case_scope():
    by = {e.file: e for e in load_notes_index(NOTES)}
    for f in ("boot4-packages.md", "jackson-2-3.md", "spring-batch-6.md"):
        assert f in by and by[f].cases == [], f
    assert set(by["boot3-to-4.md"].cases) == {"spring_boot", "msa"}
    assert set(by["boot2-to-3.md"].cases) == {"spring_boot", "msa"}
    # the relocations moved out of the Boot-only note: boot3-to-4 no longer carries them
    b34 = by["boot3-to-4.md"].body()
    assert "TomcatServletWebServerFactory" not in b34 and "ServletComponentScan" not in b34
    b4p = by["boot4-packages.md"].body()
    for s in ("org.springframework.boot.web.error", "org.springframework.boot.web.server.servlet.context.ServletComponentScan",
              "org.springframework.boot.tomcat.servlet", "@JacksonComponent", "spring-boot-starter-webmvc",
              "spring-boot-starter-aspectj", "org.springframework.boot.restclient", "spring-boot-starter-restclient"):
        assert s in b4p, s
    jk = by["jackson-2-3.md"].body()
    for rule in ("Rule A", "Rule B", "Rule C", "Rule D"):
        assert rule in jk, rule
    assert "tools.jackson.core.JacksonException" in jk and "JsonMapper.builder()" in jk and "spring-boot-jackson2" in jk
    sb = by["spring-batch-6.md"].body()
    assert "## fix-loop:" in sb and "new JobBuilder(" in sb and "write(Chunk" in sb and "DefaultBatchConfigurer" in sb


def test_servlet_to_boot_states_the_executable_war_and_the_generated_registrations_rule():
    body = {e.file: e for e in load_notes_index(NOTES)}["servlet-to-boot.md"].body()
    assert "executable WAR" in body and "java -jar" in body and "META-INF/resources/WEB-INF/jsp" in body
    assert "provided" in body and "WEB-INF/lib-provided" in body
    assert "## fix-loop: generated WebXmlRegistrations" in body and "MIGRATION-TODO web.xml" in body
    assert "spring.servlet.multipart.max-file-size" in body and "spring.servlet.multipart.max-request-size" in body
    assert "executable jar" not in body.split("## Compile-level changes")[0].replace("without JSPs an executable jar", "")


def test_review_c_corrections_stay_corrected():
    t = _note_texts()
    fw7 = t["spring-framework-7.md"]
    assert "REMOVED in 7.0 (both overloads)" in fw7 and "setReadTimeout(int|Duration)" in fw7          # W1
    assert "/**/*.do" in fw7 and "only a `**` in the middle" in fw7                                   # W4
    assert "may fail to resolve" not in fw7                                                           # W7
    assert "APPLICATION_JSON_UTF8" in fw7 and "MOVED_TEMPORARILY" in fw7 and "org.springframework.remoting" in fw7
    assert "never add `javax.annotation-api`" in fw7                                                  # O13
    sec = t["spring-security-7.md"]
    assert "PathPattern rejects it at startup" not in sec and "/**/*.do" in sec                       # W4
    assert "REMOVED in 7.0" in sec and "authentication.encoding" in sec                               # O6 / M5
    assert "runtime" in t["hibernate-7.md"] and "org.jboss.logging:jboss-logging" in t["hibernate-7.md"]  # W3
    b41 = t["boot40-to-41.md"]
    assert "spring-boot-starter-batch-data-mongodb" in b41 and "spring-boot-batch-data-mongo`" not in b41  # W12
    assert "Still present in 4.1.0" in b41                                                            # W5
    assert "ant_path_matcher` removed" not in t["boot2-to-3.md"] and "vintage" not in t["boot2-to-3.md"]  # W6 / W10
    era = t["era-libraries.md"]
    assert "After UpgradeApacheCommonsCollections_3_4" not in era and "3.2.2 compiles on JDK 21" in era  # W8
    assert "## fix-loop: commons-lang2" in era and "## fix-loop: log4j1" in era                        # M6
    assert "3.0.x line or later" not in t["ibatis2.md"] and "4.x line" in t["ibatis2.md"]              # W9
    jak = t["jakarta.md"]
    assert "`session.createQuery(hql)` → `session.createQuery(hql, Result.class)`" not in jak          # W11
    assert "findById(id).orElse(null)" in jak and "WhereJoinTable" in jak                             # M4 / O12
    assert "getValue/putValue" in t["servlet-to-boot.md"] and "getRealPath" in t["servlet-to-boot.md"]  # O2


def test_versions_are_facts_not_instructions():
    """C-O4 / C-O10: no "use version X" literals for libraries / plugins the parent chain or the BOM decides."""
    t = _note_texts()
    stale = {
        "maven-plugins.md": ("3.14.0", "3.5.3", "3.4.2", "3.3.1", "3.11.2", "3.7.1", "3.8.1", "0.8.13"),
        "jdk21.md": ("nashorn-core:15.4", "1.6.3", "lombok-mapstruct-binding:0.2.0", "(3.5.3)"),
        "era-libraries.md": ("4.5.0", "2.0.0-M5", "2.4.1", "jaxb-api` 2.3.1", "(5.5.1)", "openpdf` 2.4.0"),
        "hibernate-7.md": ("hibernate-commons-annotations:7.0.3.Final",),
        "spring-cloud.md": ("5.0.3 when no Spring Cloud BOM",),
        "jakarta.md": ("jakarta.servlet-api:6.1.0", "jakarta.persistence-api:3.2.0", "jsp-api:4.0", "el-api:6.0",
                       "faces-api:4.1"),
        "junit4-to-5.md": ("junit:junit:4.13.2", "use 3.5.3"),
    }
    for note, literals in stale.items():
        for lit in literals:
            assert lit not in t[note], (note, lit)
    plugins = t["maven-plugins.md"]
    assert "manages dependency versions only — never plugin versions" in plugins       # O3: BOM import vs parent


def test_advisory_wording_follows_v2():
    adv = yaml.safe_load((LIBRARIES / "advisories.yml").read_text(encoding="utf-8"))["advisories"]
    by = {a["id"]: a for a in adv}
    jdk = by["jdk-removed-apis"]
    assert "UseJavaUtilBase64" in jdk["summary"] and "getMimeEncoder" in jdk["followup"]
    assert "Review every ported DAO" not in by["ibatis2"]["followup"]
    assert by["log4j1"]["notes"] == "knowledge/notes/era-libraries.md"
    assert by["commons-lang2"]["notes"] == "knowledge/notes/era-libraries.md"
    # C-O16: a pointer leads to a note with a section for that library (else no pointer)
    from sbmig.advisory.hints import note_section
    from sbmig.fix.context import strip_front_matter
    checked = [a for a in adv if a.get("notes") and a.get("library") and (
        a["notes"].endswith("era-libraries.md") or a["id"] in ("log4j1", "commons-lang2", "spring-security-oauth2-legacy"))]
    assert {"log4j1", "commons-lang2", "spring-security-oauth2-legacy"} <= {a["id"] for a in checked}
    for a in checked:
        text = strip_front_matter((REPO / a["notes"]).read_text(encoding="utf-8"))
        assert note_section(text, a["library"])[0].startswith("fix-loop:"), (a["id"], a["notes"])
    assert "notes" not in by["struts2"] and "notes" not in by["egovframe"]
