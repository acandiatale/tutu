"""`scan_webapp_tree`: read-only inventory of a pom-less Tomcat webapp tree (fixtures/webapps/legacy-exploded)."""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from sbmig.analyze import guess_jar, jar_names, scan_webapp_tree
from sbmig.analyze.webapp_inventory import java_level

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "webapps" / "legacy-exploded"


@pytest.fixture(scope="module")
def inv():
    return scan_webapp_tree(FIXTURE)


def _root(inv, d):
    return next(w for w in inv.webapp_roots if w.dir == d)


def test_fixture_webapp_roots(inv):
    assert [w.dir for w in inv.webapp_roots] == ["admin", "portal"]
    portal, admin = _root(inv, "portal"), _root(inv, "admin")
    assert portal.servlet_spec == "2.3" and admin.servlet_spec == "2.5"
    assert portal.frameworks == ["plain-servlet"] and admin.frameworks == ["spring-mvc"]
    assert (portal.servlets, portal.filters, portal.listeners, portal.taglibs) == (1, 1, 0, 1)
    assert (admin.servlets, admin.listeners) == (1, 1)
    assert portal.jndi_resources == ["jdbc/PortalDS"] and admin.jndi_resources == []
    assert portal.jsp_count == 3 and admin.jsp_count == 2 and "portal/common/header.jspf" in portal.jsp_sample
    assert portal.static_count == 2 and admin.static_count == 0
    assert admin.spring_xml == ["admin/WEB-INF/spring/admin-servlet.xml"] and portal.spring_xml == []
    assert inv.build_files == ["build.xml"] and inv.warnings == []


def test_fixture_jars(inv):
    portal, admin = _root(inv, "portal"), _root(inv, "admin")
    jars = {j.name: j for j in portal.lib_jars + admin.lib_jars}
    assert (jars["commons-lang-2.6.jar"].artifact_guess, jars["commons-lang-2.6.jar"].version_guess) == \
        ("commons-lang:commons-lang", "2.6")
    assert jars["ojdbc6.jar"].artifact_guess == "com.oracle.database.jdbc:ojdbc6" and \
        jars["ojdbc6.jar"].version_guess is None
    assert jars["acme-util.jar"].artifact_guess is None and not jars["acme-util.jar"].ignored
    assert jars["json-simple-1.1.1.jar"].version_guess == "1.1.1"
    assert jars["spring-webmvc-4.3.30.RELEASE.jar"].version_guess == "4.3.30.RELEASE"
    assert jars["servlet-api-2.5.jar"].artifact_guess == "javax.servlet:servlet-api"
    old = jars["commons-io-2.4.jar.old"]
    assert old.ignored and old.artifact_guess == "commons-io:commons-io" and old.version_guess == "2.4"
    assert set(portal.ignored_files) == {"portal/WEB-INF/lib/commons-io-2.4.jar.old", "portal/WEB-INF/lib/README.txt"}
    assert all(len(j.sha1) == 40 and j.size > 0 for j in jars.values())


def test_fixture_classes_and_sources(inv):
    portal = _root(inv, "portal")
    assert portal.classes_count == 1 and portal.class_major_max == 50 and portal.java_version_from_classes == "1.6"
    assert portal.classes_without_source == 1 and portal.classes_without_source_sample == ["com.acme.portal.Legacy"]
    roots = {s.dir: s for s in inv.source_roots}
    assert set(roots) == {"portal/WEB-INF/src", "admin/src"}
    ps, ad = roots["portal/WEB-INF/src"], roots["admin/src"]
    assert ps.encoding == "cp949" and ad.encoding == "utf-8"
    assert ps.java_files == 3 and ad.java_files == 2 and ps.packages == ["com.acme"]
    assert ps.inside_webapp == "portal" and ad.inside_webapp == "admin"
    assert ps.javax_imports["javax.servlet"] == 1 and ad.javax_imports == {"javax.servlet": 1}
    assert ps.framework_imports == {"jdbc-only": 1} and ad.framework_imports == {"spring": 1}
    assert inv.duplicate_classes == 1 and inv.duplicate_classes_sample == ["com.acme.common.Util"]


def test_fixture_proposals(inv):
    props = {p.name: p for p in inv.proposals}
    assert set(props) == {"portal", "admin"}
    portal, admin = props["portal"], props["admin"]
    assert portal.java_version == "1.6" and admin.java_version == "1.8"
    assert portal.packaging == admin.packaging == "war"
    assert portal.source_dirs == ["portal/WEB-INF/src"] and admin.source_dirs == ["admin/src"]
    assert (portal.dependencies_known, portal.dependencies_unknown) == (3, 1)
    assert (admin.dependencies_known, admin.dependencies_unknown) == (2, 0)
    notes = "\n".join(portal.notes)
    assert "acme-util.jar" in notes and "2.3 DTD" in notes and "have no source" in notes and "MS949" in notes
    assert "jdbc/PortalDS" in notes and "JSP(s) import javax" in notes


def test_jar_name_guessing():
    assert "commons-lang3" in jar_names() and jar_names()["commons-lang"][0] == "commons-lang:commons-lang"
    assert guess_jar("commons-lang3-3.1.jar")[:2] == ("org.apache.commons:commons-lang3", "3.1")
    assert guess_jar("Commons-Lang-2.6.jar")[:2] == ("commons-lang:commons-lang", "2.6")
    assert guess_jar("log4j-1.2.17.jar")[:2] == ("log4j:log4j", "1.2.17")
    assert guess_jar("acme-core_2.1.jar") == (None, "2.1", "acme-core")
    assert guess_jar("acme-util.jar") == (None, None, "acme-util")
    assert guess_jar("commons-io-2.4.jar.bak")[:2] == ("commons-io:commons-io", "2.4")
    assert [java_level(m) for m in (49, 50, 51, 52, 53, 55, 61, 65)] == \
        ["1.5", "1.6", "1.7", "1.8", "9", "11", "17", "21"]


def _w(root: Path, rel: str, data: str | bytes = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def test_edges_single_root_webapp_pom_and_oddities(tmp_path):
    root = tmp_path / "shop"
    _w(root, "WEB-INF/web.xml", '<web-app version="3.0"><servlet><servlet-name>a</servlet-name>'
                                "<servlet-class>org.apache.struts.action.ActionServlet</servlet-class></servlet>"
                                "</web-app>")
    _w(root, "WEB-INF/lib/log4j-1.2.15.jar", b"PK")
    _w(root, "WEB-INF/lib/log4j-1.2.17.jar", b"PK")
    _w(root, "WEB-INF/lib/bad.jar_", b"x")
    _w(root, "WEB-INF/classes/a/B.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x33")
    _w(root, "WEB-INF/classes/a/B$1.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34")
    _w(root, "WEB-INF/classes/a/C.class", b"not a class")
    _w(root, "src/x/y/Wrong.java", "package a.b;\nimport sun.misc.BASE64Encoder;\nclass Wrong {}\n")
    _w(root, "src/NoPkg.java", "class NoPkg {}\n")
    _w(root, "tools/pom.xml", "<project/>")
    _w(root, "target/ignored/Ignored.java", "package z;\nclass Ignored {}\n")
    inv = scan_webapp_tree(root)
    wr = inv.webapp_roots[0]
    assert wr.dir == "" and wr.servlet_spec == "3.0" and wr.frameworks == ["struts1"]
    assert wr.classes_count == 3 and (wr.class_major_min, wr.class_major_max) == (51, 52)
    assert wr.classes_without_source_sample == ["a.B", "a.C"]
    assert "shop/WEB-INF/lib/bad.jar_" not in wr.ignored_files and "WEB-INF/lib/bad.jar_" in wr.ignored_files
    assert inv.build_files == ["tools/pom.xml"]
    text = "\n".join(inv.warnings)
    assert "pom at tools/pom.xml" in text and "2 versions of the same library (log4j:log4j)" in text
    assert "without a package declaration" in text and "does not match the directory" in text
    assert "mixed Java levels (1.7 … 1.8)" in text
    assert {s.dir for s in inv.source_roots} == {"src", "src/x/y"}
    assert next(s for s in inv.source_roots if s.dir == "src/x/y").jdk_internal_imports == 1
    assert len(inv.proposals) == 1 and inv.proposals[0].name == "shop"
    assert set(inv.proposals[0].source_dirs) == {"src", "src/x/y"} and inv.proposals[0].java_version == "1.8"


def test_edges_no_webapp_and_shared_sources(tmp_path):
    lib = tmp_path / "lib-only"
    _w(lib, "src/com/x/A.java", "package com.x;\nclass A {}\n")
    inv = scan_webapp_tree(lib)
    assert inv.webapp_roots == [] and any("no WEB-INF" in w for w in inv.warnings)
    assert [(p.name, p.packaging, p.source_dirs) for p in inv.proposals] == [("lib-only", "jar", ["src"])]

    two = tmp_path / "two"
    _w(two, "a/WEB-INF/web.xml", "<web-app/>")
    _w(two, "b/WEB-INF/.keep", "")
    _w(two, "common/src/com/x/A.java", "package com.x;\nclass A {}\n")
    inv = scan_webapp_tree(two)
    assert [w.dir for w in inv.webapp_roots] == ["a", "b"]
    assert any("b/WEB-INF has no web.xml" in w for w in inv.warnings)
    for p in inv.proposals:
        assert p.source_dirs == ["common/src"] and any("`common` jar module" in n for n in p.notes)


def test_bad_files_never_raise(tmp_path, monkeypatch):
    root = tmp_path / "w"
    shutil.copytree(FIXTURE, root)
    _w(root, "portal/WEB-INF/web.xml", b"\xff\xfe<web-app version=")       # garbage
    import sbmig.analyze.webapp_inventory as mod

    real = mod.scan_java_text

    def boom(path, text):
        if path.endswith("DbUtil.java"):
            raise ValueError("boom")
        return real(path, text)

    monkeypatch.setattr(mod, "scan_java_text", boom)
    inv = scan_webapp_tree(root)
    assert any("DbUtil.java: could not be read" in w for w in inv.warnings)
    assert len(inv.webapp_roots) == 2


def test_zip_of_fixture_scans_the_same(tmp_path):
    z = tmp_path / "legacy.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in FIXTURE.rglob("*"):
            if p.is_file():
                zf.write(p, "legacy/" + p.relative_to(FIXTURE).as_posix())
    out = tmp_path / "x"
    with zipfile.ZipFile(z) as zf:
        zf.extractall(out)
    inv = scan_webapp_tree(out / "legacy")
    assert [p.name for p in inv.proposals] == ["admin", "portal"]
