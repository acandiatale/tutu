"""v2.1h (A5): the restructure renames the javax EE names inside JSP / tag files to jakarta — page / tag imports,
useBean class / type and scriptlets only (Java literals / comments, taglib URIs, HTML and EL untouched), every file
written back in its own encoding with its own line ends, in every mode (layout, boot_pom in place), idempotent;
`layout.jsp_jakarta_rename: false` leaves the files and adds a follow-up. A24: two JSPs mapping to one front
destination count once."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze.jsp import jsp_javax_refs
from sbmig.core.config import Config
from sbmig.restructure import Restructurer
from sbmig.restructure.jsp_jakarta import (
    declared_encoding,
    decode_jsp,
    jsp_files,
    rename_jsp_files,
    rename_jsp_javax,
)

from .helpers import make_ws
from .test_company_layout import _boot_multi_files, _boot_multi_profile, _jsp_files, _jsp_profile, _roles

JSP = (
    '<%@ page contentType="text/html; charset=UTF-8" '
    'import="javax.servlet.http.HttpServletRequest, java.util.List, javax.annotation.processing.Generated" %>\n'
    '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n'
    '<jsp:useBean id="bean" class="javax.servlet.http.Cookie" />\n'
    "<%-- javax.servlet.http.HttpSession in a JSP comment --%>\n"
    "<p>javax.servlet.http.HttpSession is text; ${javax.servlet}</p>\n"
    "<%\n"
    '    javax.servlet.http.Cookie ck = new javax.servlet.http.Cookie("javax.servlet", "v"); // javax.servlet\n'
    "    /* javax.servlet.Filter */ List<String> names = null;\n"
    "%>\n"
    "<%= javax.servlet.jsp.PageContext.PAGE_SCOPE %>\n"
)
FRONT_JSP = "shop-front/src/main/resources/META-INF/resources/WEB-INF/jsp"


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


# ------------------------------------------------------------------------------------------------ the text rename
def test_rename_code_regions_only() -> None:
    new, n = rename_jsp_javax(JSP)
    assert n == 5
    assert 'import="jakarta.servlet.http.HttpServletRequest, java.util.List, ' in new
    assert "javax.annotation.processing.Generated" in new                 # JDK package: not in the table
    assert '<jsp:useBean id="bean" class="jakarta.servlet.http.Cookie" />' in new
    assert 'jakarta.servlet.http.Cookie ck = new jakarta.servlet.http.Cookie("javax.servlet", "v"); // javax.servlet' \
        in new                                                             # literal + line comment untouched
    assert "/* javax.servlet.Filter */" in new                            # block comment untouched
    assert "<%= jakarta.servlet.jsp.PageContext.PAGE_SCOPE %>" in new
    assert 'uri="http://java.sun.com/jsp/jstl/core"' in new                # taglib URI untouched (JSTL 3 serves it)
    assert "<%-- javax.servlet.http.HttpSession in a JSP comment --%>" in new
    assert "<p>javax.servlet.http.HttpSession is text; ${javax.servlet}</p>" in new     # HTML / EL untouched
    assert jsp_javax_refs(new) == []                                       # the analyzer sees nothing left
    assert rename_jsp_javax(new) == (new, 0)                               # idempotent


def test_rename_star_imports_and_table_targets() -> None:
    text = ('<%@ page import="javax.servlet.*,javax.servlet.http.*" %>\n'
            "<% javax.annotation.PostConstruct p; javax.transaction.xa.Xid x; javax.transaction.UserTransaction u;\n"
            "   javax.security.cert.X509Certificate cert; javax.sql.DataSource ds; %>\n")
    new, n = rename_jsp_javax(text)
    assert n == 5
    assert '<%@ page import="jakarta.servlet.*,jakarta.servlet.http.*" %>' in new
    assert "jakarta.annotation.PostConstruct p; javax.transaction.xa.Xid x; jakarta.transaction.UserTransaction u;" \
        in new                                                             # javax.transaction.xa is JDK
    assert "java.security.cert.X509Certificate cert;" in new               # the table's `to`, not jakarta.
    assert "javax.sql.DataSource ds;" in new
    assert rename_jsp_javax("<p>no code</p>\n") == ("<p>no code</p>\n", 0)


def test_tag_file_directive() -> None:
    new, n = rename_jsp_javax('<%@ tag import="javax.servlet.http.HttpServletRequest" %>\n<%@ attribute name="x" %>\n')
    assert n == 1 and new.startswith('<%@ tag import="jakarta.servlet.http.HttpServletRequest" %>')


# ------------------------------------------------------------------------------------------------ encodings
def test_declared_encoding_and_decode() -> None:
    assert declared_encoding(b'<%@ page contentType="text/html; charset=EUC-KR" pageEncoding="MS949" %>') == "cp949"
    assert declared_encoding(b'<%@ page contentType="text/html;charset=euc-kr" %>') == "euc_kr"
    assert declared_encoding(b'<?xml version="1.0" encoding="UTF-8"?><jsp:root/>') == "utf-8"
    assert declared_encoding(b"<p>none</p>") is None
    assert decode_jsp("<p>가</p>".encode("utf-8-sig")) == ("<p>가</p>", "utf-8-sig")
    # declared EUC-KR, but 똠 exists only in MS949: euc-kr cannot decode it strictly → cp949
    data = '<%@ page pageEncoding="EUC-KR" %>똠방각하'.encode("cp949")
    assert decode_jsp(data) == ('<%@ page pageEncoding="EUC-KR" %>똠방각하', "cp949")
    assert decode_jsp(b"<p>\xff\xfe</p>") is None


def test_euc_kr_file_keeps_encoding_and_crlf(tmp_path: Path) -> None:
    text = ('<%@ page contentType="text/html; charset=EUC-KR" pageEncoding="EUC-KR"\r\n'
            '         import="javax.servlet.http.HttpSession" %>\r\n'
            "<html><body><h1>한글 제목 · 회원 목록</h1>\r\n"
            "<% javax.servlet.http.Cookie c = null; // 주석: javax.servlet 그대로 %>\r\n"
            "</body></html>\r\n")
    ws = make_ws(tmp_path, {"pom.xml": "<project/>\n"}, "euckr")
    p = ws.root / "src/main/webapp/list.jsp"
    p.parent.mkdir(parents=True)
    p.write_bytes(text.encode("euc-kr"))
    res = rename_jsp_files(ws)
    assert res.renamed == {"src/main/webapp/list.jsp": 2} and res.failed == {}
    out = p.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        out.decode("utf-8")                                                 # not silently converted to UTF-8
    got = out.decode("cp949")
    assert got == text.replace("import=\"javax.servlet.http.HttpSession",
                               "import=\"jakarta.servlet.http.HttpSession").replace(
        "<% javax.servlet.http.Cookie", "<% jakarta.servlet.http.Cookie")
    assert "한글 제목 · 회원 목록" in got and "// 주석: javax.servlet 그대로" in got
    assert out.count(b"\r\n") == text.count("\r\n") and b"\n" not in out.replace(b"\r\n", b"")
    # idempotent: a second pass writes nothing
    mtime = p.stat().st_mtime_ns
    res2 = rename_jsp_files(ws)
    assert res2.renamed == {} and res2.failed == {} and p.stat().st_mtime_ns == mtime and p.read_bytes() == out


def test_walk_skips_and_failures(tmp_path: Path) -> None:
    ref = '<%@ page import="javax.servlet.http.HttpSession" %>\n'
    ws = make_ws(tmp_path, {
        "pom.xml": "<project/>\n",
        "src/main/webapp/a.jsp": ref,
        "src/main/webapp/WEB-INF/tags/t.tag": ref.replace("page", "tag"),
        "src/main/webapp/out/inner.jsp": ref,                    # `out/` below the root is a normal directory
        "src/main/webapp/plain.jsp": "<p>no javax</p>\n",
        "node_modules/pkg/x.jsp": ref,
        "out/run/y.jsp": ref,
    }, "walk")
    (ws.root / "target").mkdir()
    (ws.root / "target/maven-status").mkdir()
    (ws.root / "target/z.jsp").write_text(ref, encoding="utf-8")
    (ws.root / "src/main/webapp/bad.jsp").write_bytes(ref.encode() + b"<p>\xff\xfe\xfa</p>\n")
    assert jsp_files(ws) == ["src/main/webapp/WEB-INF/tags/t.tag", "src/main/webapp/a.jsp",
                             "src/main/webapp/bad.jsp", "src/main/webapp/out/inner.jsp", "src/main/webapp/plain.jsp"]
    assert jsp_files(ws, "src/main/webapp/WEB-INF") == ["src/main/webapp/WEB-INF/tags/t.tag"]
    res = rename_jsp_files(ws)
    assert res.renamed == {"src/main/webapp/WEB-INF/tags/t.tag": 1, "src/main/webapp/a.jsp": 1,
                           "src/main/webapp/out/inner.jsp": 1}
    assert res.failed == {"src/main/webapp/bad.jsp": "unknown encoding"} and res.scanned == 5
    assert (ws.root / "src/main/webapp/bad.jsp").read_bytes() == ref.encode() + b"<p>\xff\xfe\xfa</p>\n"
    assert (ws.root / "node_modules/pkg/x.jsp").read_text() == ref and (ws.root / "out/run/y.jsp").read_text() == ref
    assert (ws.root / "target/z.jsp").read_text() == ref


# ------------------------------------------------------------------------------------------------ through apply
def _jsp_project() -> dict[str, str]:
    files = _jsp_files()
    files["src/main/webapp/WEB-INF/jsp/home.jsp"] = (
        '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n'
        '<%@ page import="javax.servlet.http.HttpSession" %>\n'
        '<% HttpSession s = request.getSession(); String k = "javax.servlet"; %>\n<c:out value="hi"/>\n')
    return files


def test_layout_renames_the_moved_jsp(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _jsp_project(), "jsp-layout")
    prof = _jsp_profile(ws)
    prof.source.jsp_javax_files = 1
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    home = ws.root / FRONT_JSP / "home.jsp"
    text = home.read_text(encoding="utf-8")
    assert '<%@ page import="jakarta.servlet.http.HttpSession" %>' in text and '"javax.servlet"' in text
    assert 'uri="http://java.sun.com/jsp/jstl/core"' in text
    assert (res.jsp_jakarta_files, res.jsp_jakarta_refs) == (1, 1)
    assert res.jsp_jakarta_renamed == [f"{FRONT_JSP}/home.jsp: 1"]
    assert f"{FRONT_JSP}/home.jsp" in res.changed_files
    assert "JSP javax → jakarta: 1 files, 1 references" in res.notes
    assert not any("JSP javax references not renamed" in f for f in res.manual_followups)
    assert not ws.is_dirty()                                               # the rename is committed
    # a second restructure (already restructured) renames nothing
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res2.ok and res2.jsp_jakarta_files == 0 and not any(n.startswith("JSP javax") for n in res2.notes)


def test_rename_off_leaves_files_and_adds_followup(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.jsp_jakarta_rename = False
    ws = make_ws(tmp_path, _jsp_project(), "jsp-off")
    prof = _jsp_profile(ws)
    prof.source.jsp_javax_files = 1
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert '<%@ page import="javax.servlet.http.HttpSession" %>' in (ws.root / FRONT_JSP / "home.jsp").read_text()
    assert (res.jsp_jakarta_files, res.jsp_jakarta_refs, res.jsp_jakarta_renamed) == (0, 0, [])
    assert not any(n.startswith("JSP javax") for n in res.notes)
    assert "JSP javax references not renamed (layout.jsp_jakarta_rename: false): 1 files" in res.manual_followups
    prof.source.jsp_javax_files = 0                                        # nothing counted → no follow-up
    ws2 = make_ws(tmp_path / "b", _jsp_project(), "jsp-off-0")
    res2 = Restructurer().apply(ws2, _jsp_profile(ws2), ccfg, mode="layout")
    assert not any("JSP javax references" in f for f in res2.manual_followups)


def test_boot_pom_in_place_renames_webapp_jsp(tmp_path: Path, ccfg: Config) -> None:
    files = _boot_multi_files()
    jsp = "shop-boot/src/main/webapp/WEB-INF/jsp/index.jsp"
    files[jsp] = '<%@ page import="javax.servlet.http.HttpServletRequest" %>\n<p>hi</p>\n'
    ws = make_ws(tmp_path, files, "boot-jsp")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert (ws.root / jsp).read_text() == '<%@ page import="jakarta.servlet.http.HttpServletRequest" %>\n<p>hi</p>\n'
    assert res.jsp_jakarta_renamed == [f"{jsp}: 1"] and jsp in res.changed_files
    assert "JSP javax → jakarta: 1 files, 1 references" in res.notes


def test_failed_file_is_a_note_not_a_failure(tmp_path: Path, ccfg: Config) -> None:
    files = _boot_multi_files()
    ws = make_ws(tmp_path, files, "boot-bad-jsp")
    bad = ws.root / "shop-boot/src/main/webapp/bad.jsp"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'<%@ page import="javax.servlet.http.HttpSession" %>\n\xff\xfe\n')
    ws.commit("bad jsp")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert res.jsp_jakarta_files == 0
    assert any(n.startswith("JSP javax → jakarta: 1 file(s) not renamed (shop-boot/src/main/webapp/bad.jsp")
               for n in res.notes)


# ------------------------------------------------------------------------------------------------ A24
def test_two_jsps_one_destination_count_once(tmp_path: Path, ccfg: Config, monkeypatch) -> None:
    files = _jsp_files()
    files["src/main/webapp/WEB-INF/admin.jsp"] = "<p>WEB-INF/admin.jsp</p>\n"          # → jsp/admin.jsp
    files["src/main/webapp/WEB-INF/jsp/admin.jsp"] = "<p>WEB-INF/jsp/admin.jsp</p>\n"  # → jsp/admin.jsp (clash)
    ws = make_ws(tmp_path, files, "jsp-clash")
    seen: dict[str, int] = {}
    orig = Restructurer._populate_front

    def spy(self, c, *a, **kw):
        orig(self, c, *a, **kw)
        seen["jsp_moved"] = c.jsp_moved

    monkeypatch.setattr(Restructurer, "_populate_front", spy)
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    # home.jsp, admin/list.jsp and ONE of the two admin.jsp — the other is a clash follow-up, not a moved JSP
    assert seen["jsp_moved"] == 3
    assert any("web file(s) not moved to shop-front" in f for f in res.manual_followups)
    assert (ws.root / FRONT_JSP / "admin.jsp").is_file()
