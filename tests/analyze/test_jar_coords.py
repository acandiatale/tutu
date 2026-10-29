"""v2.1j: jar → Maven coordinates (`sbmig.analyze.jar_coords`): the ladder, file-name stems, the company name table,
the sha1 lookup (fake repository / fetch only — never the network) and the `used` check."""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from sbmig.analyze import jar_packages, mark_used, resolve_jar, stem_and_version
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.jar_coords import (
    Sha1Lookup,
    is_container_api,
    jar_name_table,
    nexus_base,
    parse_central_search,
    parse_nexus_assets,
    sha1_search_url,
    top_packages,
)
from sbmig.core.models import JarCoordinate

CLASS = b"\xca\xfe\xba\xbe\x00\x00\x00\x34"


def jar_bytes(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("name, expected", [
    ("commons-lang-2.6.jar", ("commons-lang", "2.6")),
    ("foo_1.2.3.jar", ("foo", "1.2.3")),
    ("foo-1.0-SNAPSHOT.jar", ("foo", "1.0-SNAPSHOT")),
    ("foo.jar", ("foo", "")),
    ("ojdbc6.jar", ("ojdbc6", "")),
    ("commons-lang3-3.1.jar", ("commons-lang3", "3.1")),
    ("spring-webmvc-4.3.30.RELEASE.jar", ("spring-webmvc", "4.3.30.RELEASE")),
    ("log4j-1.2-api-2.17.1.jar", ("log4j-1.2-api", "2.17.1")),
    ("mysql-connector-java-5.1.49-bin.jar", ("mysql-connector-java", "5.1.49")),
    ("poi-3.9-20121203.jar", ("poi", "3.9")),
    ("WEB-INF/lib/bcprov-jdk15on-1.70.jar", ("bcprov-jdk15on", "1.70")),
])
def test_stem_and_version(name, expected):
    assert stem_and_version(name) == expected


def test_pom_properties_wins():
    data = jar_bytes({"META-INF/maven/org.acme/acme-core/pom.properties": "groupId=org.acme\nartifactId=acme-core\n"
                                                                          "version=7.1\n",
                      "org/acme/core/A.class": CLASS, "org/acme/core/impl/B.class": CLASS})
    jc = resolve_jar("lib/whatever-name.jar", data, None, lookup=False)
    assert (jc.group_id, jc.artifact_id, jc.version, jc.source, jc.scope) == \
        ("org.acme", "acme-core", "7.1", "pom_properties", "compile")
    assert jc.packages == ["org.acme.core"] and len(jc.sha1) == 40 and jc.key == "org.acme:acme-core"


def test_shaded_jar_takes_the_matching_pom_properties():
    data = jar_bytes({"META-INF/maven/a/dep/pom.properties": "groupId=a\nartifactId=dep\nversion=1\n",
                      "META-INF/maven/b/tool/pom.properties": "groupId=b\nartifactId=tool\nversion=2.0\n"})
    jc = resolve_jar("tool-2.0.jar", data, None, lookup=False)
    assert (jc.key, jc.version) == ("b:tool", "2.0") and "shaded jar" in jc.note


def test_name_table_and_provided_scope(cfg):
    jc = resolve_jar("WEB-INF/lib/servlet-api-2.5.jar", jar_bytes({"javax/servlet/Servlet.class": CLASS}), cfg,
                     lookup=False)
    assert (jc.key, jc.version, jc.source, jc.scope) == ("javax.servlet:servlet-api", "2.5", "name_table", "provided")
    jstl = resolve_jar("jstl-1.2.jar", jar_bytes({"javax/servlet/jsp/jstl/core/Config.class": CLASS}), cfg,
                       lookup=False)
    assert (jstl.key, jstl.scope) == ("javax.servlet:jstl", "compile")       # JSTL is packaged, not provided


def test_table_hit_without_version_asks_the_lookup(cfg):
    data = jar_bytes({"oracle/jdbc/OracleDriver.class": CLASS, "META-INF/services/java.sql.Driver": "x"})

    class Lk:
        note = ""

        def find(self, sha1):
            return ("com.oracle.database.jdbc", "ojdbc6", "11.2.0.4")

    jc = resolve_jar("lib/ojdbc6.jar", data, cfg, lookup=Lk())
    assert (jc.key, jc.version, jc.source) == ("com.oracle.database.jdbc:ojdbc6", "11.2.0.4", "sha1_lookup")
    off = resolve_jar("lib/ojdbc6.jar", data, cfg, lookup=False)
    assert (off.key, off.version, off.source) == ("com.oracle.database.jdbc:ojdbc6", "", "name_table")
    assert "no version in the file name" in off.note and "META-INF/services" in off.note


def test_unknown_jar_manifest_note_and_bad_zip(cfg):
    data = jar_bytes({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nImplementation-Title: Acme Util\n"
                                              "Bundle-SymbolicName: com.acme.util\n",
                      "com/acme/util/X.class": CLASS})
    jc = resolve_jar("acme-util-1.0.jar", data, cfg, lookup=False)
    assert jc.source == "unknown" and jc.key == "" and jc.packages == ["com.acme.util"]
    assert "Implementation-Title=Acme Util" in jc.note and "Bundle-SymbolicName=com.acme.util" in jc.note
    bad = resolve_jar("broken.jar", b"not a zip", cfg, lookup=False)
    assert bad.source == "unknown" and "not a readable jar" in bad.note


def test_big_jar_not_scanned_but_hashed(tmp_path, cfg):
    cfg.intake.max_jar_bytes_scan = 10
    p = tmp_path / "commons-io-2.4.jar"
    p.write_bytes(jar_bytes({"META-INF/maven/x/y/pom.properties": "groupId=x\nartifactId=y\nversion=1\n"}))
    jc = resolve_jar("lib/commons-io-2.4.jar", p, cfg, lookup=False)
    assert (jc.key, jc.version, jc.source) == ("commons-io:commons-io", "2.4", "name_table")   # pom.properties unseen
    assert "not scanned" in jc.note and jc.packages == [] and len(jc.sha1) == 40


def test_jar_packages_and_container_api():
    data = jar_bytes({"org/apache/commons/lang/StringUtils.class": CLASS,
                      "org/apache/commons/lang/builder/ToStringBuilder.class": CLASS,
                      "META-INF/versions/9/org/apache/commons/lang/text/X.class": CLASS,
                      "module-info.class": CLASS})
    assert jar_packages(data) == ["org.apache.commons.lang"]
    assert top_packages(["com.a.x", "com.a.y", "com.a.x.z"]) == ["com.a.x", "com.a.y"]
    assert is_container_api(["javax.servlet", "javax.servlet.http"])
    assert not is_container_api(["javax.servlet.jsp.jstl.core"]) and not is_container_api([])
    assert not is_container_api(["javax.servlet", "com.acme"])


def test_company_jar_names_merge(tmp_path, cfg):
    kb = tmp_path / "kb"
    kb.mkdir()
    cfg.kb.dir = str(kb)
    data = jar_bytes({"com/acme/util/X.class": CLASS})
    assert resolve_jar("acme-util-1.0.jar", data, cfg, lookup=False).source == "unknown"
    f = kb / "jar_names.yml"
    f.write_text('version: 1\njars:\n  - { stem: acme-util, artifact: "com.acme:acme-util" }\n'
                 '  - { stem: commons-lang, artifact: "com.example:patched-lang" }\n', encoding="utf-8")
    jc = resolve_jar("acme-util-1.0.jar", data, cfg, lookup=False)
    assert (jc.key, jc.version, jc.source) == ("com.acme:acme-util", "1.0", "name_table")
    assert jar_name_table(cfg)["commons-lang"][0] == "com.example:patched-lang"       # company row wins
    assert jar_name_table(cfg)["servlet-api"][0] == "javax.servlet:servlet-api"        # shipped rows stay
    # edited between runs (a fleet is one process): read again without a redeploy
    f.write_text('jars:\n  - { stem: acme-util, artifact: "com.acme.new:acme-util" }\n', encoding="utf-8")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert resolve_jar("acme-util-1.0.jar", data, cfg, lookup=False).key == "com.acme.new:acme-util"


# ------------------------------------------------------------------------------------------------ sha1 lookup
class FakeRepo:
    def __init__(self, url):
        self.url = url
        self._creds = ("u", "p")

    def repository_url(self):
        return self.url


def test_nexus_urls():
    assert nexus_base("https://nexus.acme.com/repository/maven-public/") == "https://nexus.acme.com/"
    assert nexus_base("https://h/nexus/repository/public") == "https://h/nexus/"
    assert nexus_base("https://h/content/groups/public/") == "https://h/content/groups/public/"
    assert sha1_search_url("https://nexus.acme.com/repository/maven-public/", "ab") == \
        "https://nexus.acme.com/service/rest/v1/search/assets?sha1=ab"
    assert sha1_search_url("https://repo1.maven.org/maven2/", "ab") == \
        "https://search.maven.org/solrsearch/select?q=1:%22ab%22&rows=1&wt=json"


def test_parse_answers():
    nexus = {"items": [{"path": "org/x/x-src/1.0/x-src-1.0-sources.jar",
                        "maven2": {"groupId": "org.x", "artifactId": "x-src", "version": "1.0", "classifier": "sources",
                                   "extension": "jar"}},
                       {"path": "com/acme/acme-util/1.0/acme-util-1.0.jar", "maven2": {"extension": "jar"}}],
             "continuationToken": None}
    assert parse_nexus_assets(json.dumps(nexus)) == ("com.acme", "acme-util", "1.0")
    assert parse_nexus_assets('{"items": []}') is None
    central = {"response": {"numFound": 1, "docs": [{"id": "commons-lang:commons-lang:2.6", "g": "commons-lang",
                                                     "a": "commons-lang", "v": "2.6"}]}}
    assert parse_central_search(json.dumps(central)) == ("commons-lang", "commons-lang", "2.6")
    with pytest.raises(ValueError):
        parse_nexus_assets("<html>login</html>")


def test_sha1_lookup_nexus_with_credentials(cfg):
    calls = []

    def fetch(url, creds, timeout):
        calls.append((url, creds, timeout))
        return json.dumps({"items": [{"maven2": {"groupId": "com.acme", "artifactId": "acme-util",
                                                 "version": "1.0", "extension": "jar"}}]})

    lk = Sha1Lookup(cfg, fetch=fetch, repo=FakeRepo("https://nexus.acme.com/repository/maven-public/"))
    assert lk.find("abc") == ("com.acme", "acme-util", "1.0") and lk.note == ""
    assert lk.find("abc") == ("com.acme", "acme-util", "1.0")                  # cached: asked once
    assert calls == [("https://nexus.acme.com/service/rest/v1/search/assets?sha1=abc", ("u", "p"),
                      float(cfg.build.repository_timeout_seconds))]


def test_sha1_lookup_not_found_and_two_failures_stop(cfg):
    answers = iter(['{"items": []}', None, "<html/>", "never asked"])
    calls = []

    def fetch(url, creds, timeout):
        calls.append(url)
        return next(answers)

    lk = Sha1Lookup(cfg, fetch=fetch, repo=FakeRepo("https://nexus.acme.com/repository/maven-public/"))
    assert lk.find("s1") is None and "not found" in lk.note and lk.failures == 0
    assert lk.find("s2") is None and "failed" in lk.note and lk.failures == 1
    assert lk.find("s3") is None and "not the expected JSON" in lk.note and lk.dead
    assert lk.find("s4") is None and "skipped" in lk.note and len(calls) == 3   # no more lookups in this run


def test_sha1_lookup_central_and_offline(cfg):
    lk = Sha1Lookup(cfg, fetch=lambda u, c, t: json.dumps({"response": {"docs": [{"g": "g", "a": "a", "v": "1"}]}}),
                    repo=FakeRepo("https://repo1.maven.org/maven2/"))
    assert lk.find("ff") == ("g", "a", "1")
    assert Sha1Lookup(cfg, repo=FakeRepo(None)).find("ff") is None
    # the default RepositoryLookup is the conftest offline double: no URL, no request
    off = Sha1Lookup(cfg)
    assert off.find("ff") is None and "offline" in off.note and off.asked == 0


def test_resolve_jar_never_raises_with_a_broken_lookup(cfg):
    class Boom:
        def find(self, sha1):
            raise RuntimeError("gateway down")

    jc = resolve_jar("x-1.0.jar", jar_bytes({"x/X.class": CLASS}), cfg, lookup=Boom())
    assert jc.source == "unknown" and "gateway down" in jc.note


# ------------------------------------------------------------------------------------------------ used
def test_mark_used_imports_jsp_and_xml(tmp_path):
    root = tmp_path / "p"
    (root / "src/com/a").mkdir(parents=True)
    (root / "src/com/a/A.java").write_text("package com.a;\nimport org.apache.commons.lang.builder.ToStringBuilder;\n"
                                           "import static org.joda.time.DateTimeUtils.currentTimeMillis;\n"
                                           "class A { Object o = new com.acme.util.X(); }\n", encoding="utf-8")
    (root / "web/WEB-INF").mkdir(parents=True)
    (root / "web/a.jsp").write_text('<%@ page import="java.util.*, net.sf.json.JSONObject" %>', encoding="utf-8")
    (root / "web/WEB-INF/ctx.xml").write_text('<bean class="org.apache.commons.dbcp.BasicDataSource"/>',
                                              encoding="utf-8")
    jars = [JarCoordinate(path="lang.jar", packages=["org.apache.commons.lang"]),
            JarCoordinate(path="joda.jar", packages=["org.joda.time"]),
            JarCoordinate(path="acme.jar", packages=["com.acme.util"]),
            JarCoordinate(path="json.jar", packages=["net.sf.json"]),
            JarCoordinate(path="dbcp.jar", packages=["org.apache.commons.dbcp"]),
            JarCoordinate(path="old.jar", packages=["com.old.report"]),
            JarCoordinate(path="unscanned.jar")]
    mark_used(jars, ["src"], PathView(root), extra_dirs=["web"])
    used = {j.path: j.used for j in jars}
    assert used == {"lang.jar": True, "joda.jar": True, "acme.jar": True, "json.jar": True, "dbcp.jar": True,
                    "old.jar": False, "unscanned.jar": None}
    # without the web root, JSP / XML references are not seen
    again = [JarCoordinate(path="json.jar", packages=["net.sf.json"])]
    assert mark_used(again, ["src"], PathView(root))[0].used is False


def test_sha1_of_a_file_path(tmp_path, cfg):
    p = tmp_path / "a.jar"
    data = jar_bytes({"a/A.class": CLASS})
    p.write_bytes(data)
    assert resolve_jar("a.jar", p, cfg, lookup=False).sha1 == resolve_jar("a.jar", data, cfg, lookup=False).sha1
    missing = resolve_jar("gone.jar", Path(tmp_path / "gone.jar"), cfg, lookup=False)
    assert missing.source == "unknown" and "unreadable" in missing.note
