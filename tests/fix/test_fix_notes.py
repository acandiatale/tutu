import re
from pathlib import Path

import yaml

from sbmig.fix import load_notes_index
from sbmig.fix.context import split_front_matter

NOTES = Path(__file__).resolve().parents[2] / "knowledge" / "notes"
REQUIRED = {"jdk21.md", "jakarta.md", "boot2-to-3.md", "boot3-to-4.md", "boot4-packages.md", "boot40-to-41.md",
            "spring-framework-7.md", "jackson-2-3.md", "spring-security-7.md", "spring-batch-6.md", "servlet-to-boot.md",
            "junit4-to-5.md", "maven-plugins.md", "externalized-config.md", "ibatis2.md", "hibernate-7.md"}
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
    assert [e.file for e in entries if e.always] == []                 # v2.1u: fix-policy.md deleted, no always-note
    assert not (NOTES / "fix-policy.md").exists()


def test_keywords_hit_expected_notes():
    by = {e.file: e for e in load_notes_index(NOTES)}
    assert by["jakarta.md"].score("package javax.persistence does not exist") > 0
    assert by["spring-security-7.md"].score("cannot find symbol class WebSecurityConfigurerAdapter") > 0
    assert by["boot4-packages.md"].score("cannot find symbol class MockBean") > 0
    assert by["jdk21.md"].score("package sun.misc does not exist") > 0
    assert by["junit4-to-5.md"].score("import org.junit.Test;") > 0
    assert by["hibernate-7.md"].score("package org.hibernate.annotations.common.util.impl does not exist") > 0
    assert by["hibernate-7.md"].score("package org.jboss.logging does not exist") > 0


def test_keywords_hit_the_v21u_notes():
    """v2.1u / v2.1w: the error texts report C §4 found without a note now select one (one line per new keyword)."""
    by = {e.file: e for e in load_notes_index(NOTES)}
    hits = {
        "jackson-2-3.md": ["package com.fasterxml.jackson.databind does not exist",
                           "cannot access ObjectMapper\n  class file for com.fasterxml.jackson.databind.ObjectMapper not found",
                           "cannot find symbol\n  symbol: class Jackson2ObjectMapperBuilder",
                           "incompatible types: tools.jackson.databind.ObjectMapper cannot be converted to X"],
        "spring-batch-6.md": ["package org.springframework.batch.item does not exist",
                              "cannot find symbol\n  symbol: class JobBuilderFactory",
                              "cannot find symbol\n  symbol: class DefaultBatchConfigurer"],
        "spring-framework-7.md": ["cannot find symbol\n  symbol: variable APPLICATION_JSON_UTF8_VALUE",
                                  "cannot find symbol\n  symbol: variable MOVED_TEMPORARILY",
                                  "cannot find symbol\n  symbol: method getStatusCodeValue()",
                                  "package org.springframework.remoting.httpinvoker does not exist",
                                  "package org.springframework.util.concurrent does not exist",
                                  "cannot find symbol\n  symbol: class Required",
                                  "no suitable method found for notNull(java.lang.Object)",
                                  "cannot find symbol\n  symbol: class OkHttp3ClientHttpRequestFactory",
                                  "cannot find symbol\n  symbol: class AbstractExcelView",
                                  "cannot find symbol\n  symbol: method queryForInt(java.lang.String)"],
        "jakarta.md": ["cannot find symbol\n  symbol: method findOne(java.lang.Long)",
                       "PageRequest(int,int) has protected access in org.springframework.data.domain.PageRequest",
                       "package org.hibernate.validator.constraints does not exist",
                       "cannot find symbol\n  symbol: class WhereJoinTable\n  location: package org.hibernate.annotations"],
        "spring-security-7.md": ["package org.springframework.security.authentication.encoding does not exist",
                                 "cannot find symbol\n  symbol: class EnableResourceServer"],
        "era-libraries.md": ["package org.apache.commons.lang does not exist", "package org.apache.log4j does not exist"],
        "servlet-to-boot.md": ["cannot find symbol\n  symbol: method getValue(java.lang.String)\n  location: interface HttpSession",
                               "cannot find symbol\n  symbol: method getRealPath(java.lang.String)",
                               "incompatible types: com.x.XssFilter cannot be converted to jakarta.servlet.Filter",
                               "package javax.servlet.jsp.tagext does not exist"],
        "boot4-packages.md": ["cannot find symbol\n  symbol: class TomcatServletWebServerFactory",
                              "package org.springframework.boot.web.server does not exist",
                              "cannot find symbol\n  symbol: class JsonComponent",
                              "cannot find symbol\n  symbol: class RestTemplateBuilder"],
    }
    for note, texts in hits.items():
        for text in texts:
            assert by[note].score(text) > 0, (note, text)


def test_keywords_no_longer_fire_on_unrelated_text():
    """Report C §4 false positives: none of these texts may select the note any more."""
    by = {e.file: e for e in load_notes_index(NOTES)}
    misses = {
        "junit4-to-5.md": ['http.authorizeRequests().antMatchers("/a").permitAll()',
                           "cannot find symbol: class ParameterizedTypeReference"],
        "maven-plugins.md": ["Could not find artifact x:y:jar:1.0 in nexus (https://nexus.example.com/repository/maven-releases/)",
                             "import com.x.layout.MainLayout;"],
        "boot40-to-41.md": ["incompatible types: Optional<User> cannot be converted to User",
                            "warning: [removal] MappingJackson2HttpMessageConverter has been deprecated and marked for removal"],
        "jdk21.md": ["warning: [removal] MappingJackson2HttpMessageConverter has been deprecated and marked for removal"],
        "jakarta.md": ["import com.x.web.XssFilterSupport;", "cannot find symbol: class LayoutFilterUtil"],
        "servlet-to-boot.md": ["import org.jspecify.annotations.Nullable;",
                               "cannot find symbol\n  symbol: method getValue()\n  location: interface Entry"],
        "era-libraries.md": ["package org.apache.commons.lang3 does not exist",
                             "package org.apache.logging.log4j does not exist"],
    }
    for note, texts in misses.items():
        for text in texts:
            assert by[note].score(text) == 0, (note, text, [r.pattern for r in by[note].compiled() if r.search(text)])


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
    for every_case in ("boot4-packages.md", "jackson-2-3.md", "spring-batch-6.md"):     # cases: [] = every case
        assert by[every_case].cases == [], every_case
        assert all(by[every_case].applies(c, "verify") for c in CASES), every_case
    assert by["boot3-to-4.md"].applies("spring_boot", "verify") and not by["boot3-to-4.md"].applies("spring_legacy", "verify")


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
