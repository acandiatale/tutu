"""knowledge/libraries/jakarta_libraries.yml + sbmig.core.jakarta_libraries (X1b, 2026-10-22)."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.jakarta_libraries import JAKARTA_LIBRARIES_FILE, jakarta_libraries, jakarta_library_for
from sbmig.plan.versions import version_lt


def test_table_entries_are_consistent():
    libs = jakarta_libraries()
    assert libs, "the table must not be empty"
    keys = [lib.key for lib in libs]
    assert len(keys) == len(set(keys)), "duplicate keys"
    for lib in libs:
        assert lib.key.count(":") == 1 and all(lib.key.split(":"))
        assert lib.jakarta_since and lib.version
        # the version the handler sets is on the jakarta line
        assert version_lt(lib.version, lib.jakarta_since) is False, lib.key
        for old in lib.old_keys:
            assert old.count(":") == 1 and old != lib.key, lib.key
        assert lib.note.strip(), f"{lib.key}: a one-line note for the report is required"
        assert lib.docs, f"{lib.key}: at least one evidence URL"
        assert all(d.startswith("http") for d in lib.docs), lib.key
        assert lib.requires_java is None or lib.requires_java >= 8


def test_old_keys_never_collide_with_current_keys():
    libs = jakarta_libraries()
    current = {lib.key for lib in libs}
    olds = [k for lib in libs for k in lib.old_keys]
    assert not (set(olds) & current)
    assert len(olds) == len(set(olds))


def test_lookup_by_current_and_old_key():
    q = jakarta_library_for("org.qlrm:qlrm")
    assert q is not None and q.version == "4.2.1" and q.jakarta_since == "4.0.1" and q.requires_java == 17
    assert jakarta_library_for("ch.simas.qlrm:qlrm") is q
    assert jakarta_library_for("org.example:nothing") is None
    assert jakarta_library_for(None) is None and jakarta_library_for("") is None


def test_custom_file(tmp_path: Path):
    f = tmp_path / "libs.yml"
    f.write_text("version: 1\nlibraries:\n  - {key: 'a:b', jakarta_since: '2.0', version: '2.1', note: n, docs: ['http://x']}\n",
                 encoding="utf-8")
    assert jakarta_library_for("a:b", f).version == "2.1"
    assert JAKARTA_LIBRARIES_FILE.is_file()


# ---------------------------------------------------------------------------------------------------------------------
# 2026-10-22 era libraries: classifier field + new families
# ---------------------------------------------------------------------------------------------------------------------
def test_classifier_field():
    eh = jakarta_library_for("org.ehcache:ehcache")
    assert eh is not None and eh.classifier == "jakarta" and eh.jakarta_since == "3.10.0" and eh.version == "3.12.0"
    assert [lib.key for lib in jakarta_libraries() if lib.classifier] == ["org.ehcache:ehcache"]
    assert jakarta_library_for("org.qlrm:qlrm").classifier is None


def test_custom_file_with_classifier(tmp_path: Path):
    f = tmp_path / "libs.yml"
    f.write_text("version: 1\nlibraries:\n  - {key: 'a:b', jakarta_since: '2.0', version: '2.1', classifier: jakarta,"
                 " note: n, docs: ['http://x']}\n", encoding="utf-8")
    assert jakarta_library_for("a:b", f).classifier == "jakarta"


def test_lookup_new_keys():
    dt = jakarta_library_for("displaytag:displaytag")                    # old coordinates → the hazendaz fork
    assert dt is not None and dt.key == "com.github.hazendaz:displaytag" and dt.version == "3.8.0"
    assert dt.jakarta_since == "3.0.0" and dt.requires_java == 11
    assert jakarta_library_for("org.jooq:jooq").requires_java == 21
    assert jakarta_library_for("org.quartz-scheduler:quartz-jobs").version == "2.5.2"
    assert jakarta_library_for("org.mybatis:mybatis-spring").version == "4.1.0"
    assert jakarta_library_for("com.google.inject:guice").jakarta_since == "6.0.0"
    assert jakarta_library_for("org.apache.shiro:shiro-web") is None    # advisory shiro-legacy instead (classifier clash)
    assert jakarta_library_for("org.apache.tomcat:tomcat-jdbc") is None  # no javax EE API: not a jakarta library


def test_library_families_share_version_and_note():
    families = {"org.glassfish.jersey.": "4.0.2", "org.apache.cxf:": "4.2.4", "org.apache.tomcat:": "11.0.22",
                "org.jooq:": "3.21.5", "org.apache.axis2:": "2.0.1", "org.mybatis.spring.boot:": "4.1.0",
                "org.springframework.webflow:": "4.0.1", "org.eclipse.persistence:": "5.0.2"}
    for prefix, version in families.items():
        members = [lib for lib in jakarta_libraries() if lib.key.startswith(prefix)]
        assert len(members) >= 2, prefix
        assert {lib.version for lib in members} == {version}, prefix
        assert len({lib.note for lib in members}) == 1 and len({lib.jakarta_since for lib in members}) == 1, prefix


# ---------------------------------------------------------------------------------------------------------------------
# v2.1d (2026-10-27): the people's table <kb>/libraries.yml merged over the shipped one, prompt lines
# ---------------------------------------------------------------------------------------------------------------------
COMPANY = """version: 1
libraries:
  - key: opensymphony:sitemesh
    jakarta_since: "2.6.0"
    version: "2.6.1"
    note: Company build of SiteMesh 2. Second sentence.
  - key: org.sitemesh:sitemesh
    version: 3.3
    allow_major: true
  - {key: 'com.acme:widgets', jakarta_since: '5.0', version: '5.1', note: in-house}
  - {key: 'broken entry without colon', version: '1'}
  - just a string
"""


def test_company_file_replaces_by_key_and_appends_unknown(tmp_path: Path):
    from sbmig.core.jakarta_libraries import jakarta_libraries as libs_of

    f = tmp_path / "libraries.yml"
    f.write_text(COMPANY, encoding="utf-8")
    shipped = jakarta_libraries()
    merged = libs_of(company_path=f)
    assert len(merged) == len(shipped) + 1                              # 2 replaced, 1 appended, 2 bad skipped
    sm = jakarta_library_for("opensymphony:sitemesh", company_path=f)
    assert (sm.version, sm.source, sm.allow_major) == ("2.6.1", "company", False)
    assert [lib.key for lib in merged].index("opensymphony:sitemesh") == \
        [lib.key for lib in shipped].index("opensymphony:sitemesh")    # replaced in place
    s3 = jakarta_library_for("org.sitemesh:sitemesh", company_path=f)
    assert s3.allow_major and s3.version == "3.3" and s3.jakarta_since == "3.3"   # float → str, since := version
    assert merged[-1].key == "com.acme:widgets" and merged[-1].source == "company"
    assert jakarta_library_for("org.qlrm:qlrm", company_path=f).source == "shipped"
    assert jakarta_library_for("opensymphony:sitemesh").source == "shipped"        # without the company file
    assert libs_of(company_path=tmp_path / "missing.yml") == shipped


def test_company_entry_old_keys_replace_the_shipped_entry(tmp_path: Path):
    f = tmp_path / "libraries.yml"
    f.write_text("libraries:\n  - {key: 'org.sitemesh:sitemesh', old_keys: ['opensymphony:sitemesh'], "
                 "jakarta_since: '3.2.0', version: '3.3.0', allow_major: true}\n", encoding="utf-8")
    merged = jakarta_libraries(company_path=f)
    assert [lib.key for lib in merged].count("org.sitemesh:sitemesh") == 1
    assert "opensymphony:sitemesh" not in [lib.key for lib in merged]
    assert jakarta_library_for("opensymphony:sitemesh", company_path=f).key == "org.sitemesh:sitemesh"


def test_company_file_is_reread_when_it_changes(tmp_path: Path):
    import os

    f = tmp_path / "libraries.yml"
    f.write_text("libraries:\n  - {key: 'a:b', version: '1.0'}\n", encoding="utf-8")
    assert jakarta_library_for("a:b", company_path=f).version == "1.0"
    f.write_text("libraries:\n  - {key: 'a:b', version: '2.0.0'}\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert jakarta_library_for("a:b", company_path=f).version == "2.0.0"


def test_bad_company_file_uses_the_shipped_table(tmp_path: Path, caplog):
    shipped = jakarta_libraries()
    for i, text in enumerate(("libraries: [unclosed\n", "libraries: {a: b}\n", "", "version: 1\n", "\x00\x01")):
        f = tmp_path / f"bad{i}.yml"
        f.write_text(text, encoding="utf-8")
        with caplog.at_level("WARNING", logger="sbmig.core.jakarta_libraries"):
            assert jakarta_libraries(company_path=f) == shipped
    assert any("using the shipped table only" in r.getMessage() for r in caplog.records)


def test_company_libraries_file(tmp_path: Path):
    from sbmig.core.config import Config
    from sbmig.core.jakarta_libraries import company_libraries_file

    kb = tmp_path / "kb-v2"
    cfg = Config.model_validate({"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                 "kb": {"dir": str(kb)}})
    assert company_libraries_file(cfg) == kb / "libraries.yml"
    cfg.libraries.company_file = "people/libs.yml"
    assert company_libraries_file(cfg) == kb / "people" / "libs.yml"
    cfg.libraries.company_file = str(tmp_path / "elsewhere" / "libraries.yml")
    assert company_libraries_file(cfg) == tmp_path / "elsewhere" / "libraries.yml"
    rel = Config.model_validate({"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                 "repo_root": str(tmp_path)})
    assert company_libraries_file(rel) == tmp_path / "kb-v2" / "libraries.yml"     # kb.dir relative to the repo root


def test_library_hint_line():
    from sbmig.core.jakarta_libraries import JakartaLibrary, library_hint_line

    sm = JakartaLibrary(key="opensymphony:sitemesh", jakarta_since="2.6.0", version="2.6.0", requires_java=17,
                        note="Same packages and filter class as 2.x. Long term: SiteMesh 3.", source="company")
    assert library_hint_line(sm, "2.4.2") == (
        "opensymphony:sitemesh: approved — raise to 2.6.0 (jakarta since 2.6.0, requires Java 17; Same packages and "
        "filter class as 2.x; source: company table)")
    assert "already at or above the table's 2.6.0" in library_hint_line(sm, "2.6.0")
    assert "already at or above" in library_hint_line(sm, "2.7.0")
    s3 = JakartaLibrary(key="org.sitemesh:sitemesh", jakarta_since="3.2.0", version="3.3.0", allow_major=True,
                        source="company")
    line = library_hint_line(s3, "2.4.2")
    assert line.startswith("org.sitemesh:sitemesh: approved — a person allowed the major upgrade to 3.3.0 (")
    assert "crosses" not in line and "source: company table" in line
    q = jakarta_library_for("org.qlrm:qlrm")
    line = library_hint_line(q, "1.7.1")
    assert "crosses 1.x → 4.x" in line and "<verify>" in line and "source: shipped table" in line
    assert line.startswith("org.qlrm:qlrm: the table knows the jakarta release 4.2.1 (") and "approved" not in line
    eh = library_hint_line(jakarta_library_for("org.ehcache:ehcache"))
    assert "classifier jakarta" in eh and "\n" not in eh
