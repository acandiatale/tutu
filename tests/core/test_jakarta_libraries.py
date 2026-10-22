"""knowledge/libraries/jakarta_libraries.yml + sbmig.core.jakarta_libraries (X1b, 2026-10-22)."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.jakarta_libraries import JAKARTA_LIBRARIES_FILE, jakarta_libraries, jakarta_library_for
from sbmig.triage.handlers import version_lt


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
