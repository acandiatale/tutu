import re
from pathlib import Path

import yaml

from sbmig.fix import load_notes_index
from sbmig.fix.context import split_front_matter

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
REQUIRED = {"jdk21.md", "jakarta.md", "boot2-to-3.md", "boot3-to-4.md", "boot40-to-41.md", "spring-framework-7.md",
            "spring-security-7.md", "servlet-to-boot.md", "junit4-to-5.md", "maven-plugins.md", "fix-policy.md",
            "externalized-config.md", "ibatis2.md", "hibernate-7.md"}
CASES = {"pure_java", "spring_legacy", "spring_boot", "servlet_tomcat", "msa"}
PHASES = {"jdk21", "spring", "restructure"}


def test_index_lists_existing_notes_with_front_matter():
    entries = load_notes_index(NOTES)
    files = {e.file for e in entries}
    assert REQUIRED <= files
    assert files == {p.name for p in NOTES.glob("*.md")}
    raw = yaml.safe_load((NOTES / "index.yml").read_text())["notes"]
    for item in raw:
        assert set(item) >= {"file", "title", "keywords", "cases", "phases", "always"}
        p = NOTES / item["file"]
        assert p.exists()
        text = p.read_text()
        meta, body = split_front_matter(text)
        assert meta.get("title") == item["title"]
        assert meta.get("keywords") == item["keywords"]          # index and front-matter in sync
        assert set(meta.get("cases") or []) <= CASES and set(meta.get("phases") or []) <= PHASES
        assert len(text.splitlines()) <= 160, f"{p.name} too long"
        assert body.strip()
        for k in item["keywords"]:
            re.compile(k)
    assert [e.file for e in entries if e.always] == ["fix-policy.md"]


def test_keywords_hit_expected_notes():
    by = {e.file: e for e in load_notes_index(NOTES)}
    assert by["jakarta.md"].score("package javax.persistence does not exist") > 0
    assert by["spring-security-7.md"].score("cannot find symbol class WebSecurityConfigurerAdapter") > 0
    assert by["boot3-to-4.md"].score("cannot find symbol class MockBean") > 0
    assert by["jdk21.md"].score("package sun.misc does not exist") > 0
    assert by["junit4-to-5.md"].score("import org.junit.Test;") > 0
    assert by["hibernate-7.md"].score("package org.hibernate.annotations.common.util.impl does not exist") > 0


def test_every_note_applies_in_the_verify_loop():
    """v2 fixes every error in the `verify` script loop: a note's `phases` (the v1 phase it was written for) never
    keeps it out of a verify-phase prompt; cases still restrict, other phases are still narrowed."""
    by = {e.file: e for e in load_notes_index(NOTES)}
    for e in by.values():
        assert e.applies(None, "verify"), e.file
    n = by["hibernate-7.md"]
    assert n.applies("spring_boot", "restructure") and n.applies("spring_boot", "verify")
    assert not n.applies("spring_boot", "jdk21")
    assert by["ibatis2.md"].applies("spring_boot", "verify") and not by["ibatis2.md"].applies("spring_boot", "jdk21")
    assert by["jdk21.md"].phases == ["jdk21"] and by["jdk21.md"].applies("pure_java", "verify")
    sec = by["spring-security-7.md"]
    assert sec.applies("spring_legacy", "verify") and not sec.applies("pure_java", "verify")    # cases still count
    assert by["fix-policy.md"].applies("pure_java", "verify")           # no phase restriction


def test_index_fallback_to_front_matter(tmp_path):
    (tmp_path / "a.md").write_text("---\ntitle: A\nkeywords: ['foo']\ncases: []\nphases: []\n---\nbody A\n")
    (tmp_path / "b.md").write_text("no front matter\n")
    entries = load_notes_index(tmp_path)
    assert [e.title for e in entries] == ["A", "b"]
    assert entries[0].score("foo bar") == 1 and entries[0].body() == "body A"
    assert load_notes_index(tmp_path / "missing") == []


def test_ibatis2_note_hits_orm_ibatis_errors():
    by = {e.file: e for e in load_notes_index(NOTES)}
    assert by["ibatis2.md"].score("package org.springframework.orm.ibatis does not exist") > 0
    assert by["ibatis2.md"].score("cannot find symbol class SqlMapClientDaoSupport") > 0
    assert by["ibatis2.md"].score("package org.apache.ibatis.session does not exist") == 0   # MyBatis 3
