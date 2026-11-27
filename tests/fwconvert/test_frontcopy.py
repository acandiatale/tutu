"""The front-end copy of sbmig.fwconvert.frontcopy (docs/FW-CONVERT.md 2.1, 3.6, scenario A12)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbmig.fwconvert.frontcopy import copy_frontend, decode_source, fix_jsp_paths, target_rel
from sbmig.fwconvert.model import Layout, ModuleNames, ProjectIdentity
from sbmig.fwconvert.rules import load_rules

RULES = load_rules("Example")


def _layout() -> Layout:
    return Layout(ProjectIdentity("shopProject", "d1", "com.example.d1.shopProject",
                                  ModuleNames("shopProject-boot", "shopProject-domain", "shopProject-service",
                                              "shopProject-store")))


STATIC = _layout().static_dir


def _put(root: Path, rel: str, data: bytes = b"x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_a12_web_inf_jsp_folder_is_dropped(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "app/src/main/webapp/WEB-INF/jsp/p/z.jsp", b"<html/>")
    res = copy_frontend(src, out, _layout(), RULES)
    assert res.copied == [("app/src/main/webapp/WEB-INF/jsp/p/z.jsp", f"{STATIC}/p/z.jsp")]
    assert (out / STATIC / "p/z.jsp").read_bytes() == b"<html/>"
    assert res.by_kind == {"jsp": 1} and res.errors == [] and res.rejected_web_inf == []


@pytest.mark.parametrize("rel, want", [
    ("WebContent/css/a.css", "css/a.css"),
    ("x/webapp/y/WebContent/js/a.js", "js/a.js"),                 # the LAST webapp root counts
    ("webapp/WEB-INF/views/a.jsp", "views/a.jsp"),                 # only WEB-INF dropped
    ("webapp/WEB-INF/a.jsp", "a.jsp"),
    ("webapp/jsp/a.jsp", "jsp/a.jsp"),                             # jsp/ only right after WEB-INF
    ("webapp/WEB-INF/jsp.jsp", "jsp.jsp"),
    ("some/where/logo.png", "logo.png"),                           # no webapp root: file name only
    ("webapp.jsp", "webapp.jsp"),
])
def test_target_paths(rel, want):
    assert target_rel(rel, RULES) == want


def test_static_file_inside_web_inf_is_refused(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "webapp/WEB-INF/js/secret.js")
    _put(src, "webapp/WEB-INF/jsp/ok.jsp")
    res = copy_frontend(src, out, _layout(), RULES)
    assert res.rejected_web_inf == ["webapp/WEB-INF/js/secret.js"]
    assert [t for _, t in res.copied] == [f"{STATIC}/ok.jsp"]
    assert not (out / STATIC / "js/secret.js").exists()


def test_collision_keeps_the_first_and_never_overwrites(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "a/logo.png", b"first")
    _put(src, "b/logo.png", b"second")
    _put(src, "c/LOGO.png", b"third")
    res = copy_frontend(src, out, _layout(), RULES)
    assert res.copied == [("a/logo.png", f"{STATIC}/logo.png")]
    assert len(res.errors) == 2
    assert "b/logo.png" in res.errors[0] and "a/logo.png" in res.errors[0]
    assert (out / STATIC / "logo.png").read_bytes() == b"first"


def test_existing_target_is_not_overwritten(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "webapp/a.css", b"new")
    _put(out, f"{STATIC}/a.css", b"old")
    res = copy_frontend(src, out, _layout(), RULES)
    assert res.copied == [] and "already exists" in res.errors[0]
    assert (out / STATIC / "a.css").read_bytes() == b"old"


def test_binary_file_is_byte_identical(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    data = bytes(range(256)) * 40 + b"\r\n\x00\xff"
    _put(src, "WebContent/images/x.gif", data)
    res = copy_frontend(src, out, _layout(), RULES)
    assert (out / STATIC / "images/x.gif").read_bytes() == data
    assert res.by_kind == {"static": 1}


def test_files_argument_versus_the_walk(tmp_path):
    src, out1, out2 = tmp_path / "src", tmp_path / "o1", tmp_path / "o2"
    _put(src, "webapp/a.jsp")
    _put(src, "webapp/b.css")
    _put(src, "webapp/c.java")
    _put(src, "target/webapp/d.css")
    _put(src, ".git/webapp/e.js")
    _put(src, "node_modules/x/f.js")
    walked = copy_frontend(src, out1, _layout(), RULES)
    assert [s for s, _ in walked.copied] == ["webapp/a.jsp", "webapp/b.css"]
    given = copy_frontend(src, out2, _layout(), RULES, files=["webapp/b.css", "target/webapp/d.css", "webapp/c.java"])
    assert [s for s, _ in given.copied] == ["target/webapp/d.css", "webapp/b.css"]
    assert given.by_kind == {"static": 2}


def test_bad_inputs_become_errors(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    res = copy_frontend(src, out, _layout(), RULES, files=["../escape.js", "/abs/x.js", "missing/y.css"])
    assert len(res.errors) == 3 and res.copied == []
    assert any("cannot read" in e for e in res.errors)
    assert copy_frontend(tmp_path / "nope", out, _layout(), RULES).errors


@pytest.mark.skipif(os.name == "nt", reason="chmod")
def test_unreadable_file_is_an_error(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "webapp/a.css")
    _put(src, "webapp/b.css")
    (src / "webapp/a.css").chmod(0)
    try:
        if os.access(src / "webapp/a.css", os.R_OK):
            pytest.skip("running as a user who can read anything")
        res = copy_frontend(src, out, _layout(), RULES)
    finally:
        (src / "webapp/a.css").chmod(0o644)
    assert [s for s, _ in res.copied] == ["webapp/b.css"]
    assert "webapp/a.css" in res.errors[0]


def test_decode_source_order():
    assert decode_source("한글".encode("utf-8")) == ("한글", "utf-8")
    assert decode_source("한글".encode("cp949")) == ("한글", "cp949")
    assert decode_source(b"\x80\xff")[1] == "latin-1"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks")
def test_file_list_through_a_symlinked_folder_outside_is_refused(tmp_path):
    outside = tmp_path / "outside"
    _put(outside, "secret.jsp", b"SECRET")
    src, out = tmp_path / "src", tmp_path / "out"
    _put(src, "WebContent/ok.jsp", b"ok")
    os.symlink(outside, src / "WebContent/link")
    os.symlink(outside / "secret.jsp", src / "WebContent/file.jsp")
    res = copy_frontend(src, out, _layout(), RULES,
                        files=["WebContent/link/secret.jsp", "WebContent/file.jsp", "WebContent/ok.jsp"])
    assert [s for s, _ in res.copied] == ["WebContent/ok.jsp"]
    assert len(res.errors) == 2 and all("outside the source folder" in e for e in res.errors)
    assert not (out / STATIC / "link").exists()


# ---- absolute paths inside the copied JSPs ------------------------------------------------------------

WEB = "/WEB-INF/static/shopProject"


def _copy(tmp_path, sources: dict[str, bytes]):
    src, out = tmp_path / "src", tmp_path / "out"
    for rel, data in sources.items():
        _put(src, rel, data)
    res = copy_frontend(src, out, _layout(), RULES)
    assert res.errors == []
    return out, res


def test_each_construct_is_pointed_at_the_new_place(tmp_path):
    order = (b'<%@ include file="/WEB-INF/jsp/common/header.jsp" %>\n'
             b"<jsp:include page='/WEB-INF/jsp/common/header.jsp'/>\n"
             b'<jsp:forward page="/WEB-INF/jsp/p/next.jsp?x=1" />\n'
             b'<c:import var="v" url="/WEB-INF/jsp/p/next.jsp"/>\n'
             b'<jsp:include page="common/header.jsp"/>\n'
             b'<a href="/WEB-INF/jsp/p/next.jsp">not a construct</a>\n')
    out, res = _copy(tmp_path, {"WebContent/WEB-INF/jsp/order.jsp": order,
                                "WebContent/WEB-INF/jsp/common/header.jsp": b"h",
                                "WebContent/WEB-INF/jsp/p/next.jsp": b"n"})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    assert fixed.files_changed == [f"{STATIC}/order.jsp"] and fixed.replaced == 4
    assert fixed.queue == [] and fixed.errors == []
    assert (out / STATIC / "order.jsp").read_bytes() == (
        f'<%@ include file="{WEB}/common/header.jsp" %>\n'
        f"<jsp:include page='{WEB}/common/header.jsp'/>\n"
        f'<jsp:forward page="{WEB}/p/next.jsp?x=1" />\n'
        f'<c:import var="v" url="{WEB}/p/next.jsp"/>\n'
        '<jsp:include page="common/header.jsp"/>\n'
        '<a href="/WEB-INF/jsp/p/next.jsp">not a construct</a>\n').encode()


def test_path_to_a_file_not_copied_is_left_and_handed_over(tmp_path):
    text = (b'<%@ include file="/WEB-INF/jsp/common/top.jspf" %>\n'
            b'<%@ include file="/WEB-INF/jsp/common/header.jsp" %>\n'
            b'<jsp:include page="/WEB-INF/jsp/common/top.jspf"/>\n')
    out, res = _copy(tmp_path, {"WebContent/WEB-INF/jsp/p/a.jsp": text,
                                "WebContent/WEB-INF/jsp/common/header.jsp": b"h",
                                "WebContent/WEB-INF/jsp/common/top.jspf": b"t"})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    [item] = fixed.queue
    assert item.component == "jsp_path::p/a.jsp"
    assert item.reason == "[JSP_PATH] /WEB-INF/jsp/common/top.jspf"
    assert item.source_path == f"{STATIC}/p/a.jsp" and item.excerpt == text.decode()
    assert fixed.replaced == 1
    assert b'file="/WEB-INF/jsp/common/top.jspf"' in (out / STATIC / "p/a.jsp").read_bytes()


def test_dynamic_values_are_left_alone(tmp_path):
    text = (b'<jsp:include page="<%= base %>/x.jsp"/>\n<c:import url="${base}/x.jsp"/>\n'
            b'<jsp:include page="#{b}/x.jsp"/>\n<c:import url="//cdn/x.jsp"/>\n')
    out, res = _copy(tmp_path, {"WebContent/WEB-INF/jsp/a.jsp": text})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    assert fixed.files_changed == [] and fixed.queue == []
    assert (out / STATIC / "a.jsp").read_bytes() == text


def test_ms949_jsp_and_crlf_keep_every_other_byte(tmp_path):
    text = '<%-- 머리글 --%>\r\n<%@ include file="/WEB-INF/jsp/common/header.jsp" %>\r\n<p>한글</p>\r\n'
    out, res = _copy(tmp_path, {"WebContent/WEB-INF/jsp/a.jsp": text.encode("cp949"),
                                "WebContent/WEB-INF/jsp/common/header.jsp": b"h"})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    assert fixed.replaced == 1
    assert (out / STATIC / "a.jsp").read_bytes() == \
        text.replace("/WEB-INF/jsp/common/", f"{WEB}/common/").encode("cp949")


def test_page_and_api_urls_are_not_files(tmp_path):
    text = (b'<c:import url="/SH1/SH1A10/menu.mvc"/>\n<jsp:include page="/SH1/SH1A10/top.mvc?x=1"/>\n'
            b'<c:import url="/api/menu"/>\n<c:import url="/api"/>\n<c:import url="/apix/menu.html"/>\n')
    out, res = _copy(tmp_path, {"WebContent/WEB-INF/jsp/shop/a.jsp": text})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    [item] = fixed.queue
    assert item.reason == "[JSP_PATH] /apix/menu.html"
    assert fixed.files_changed == []


def test_a_copied_jsp_under_a_folder_named_like_the_api_prefix_is_still_a_file(tmp_path):
    text = (b'<%@ include file="/api/part.jsp" %>\n<jsp:include page="/api/doc.jsp"/>\n'
            b'<jsp:include page="/api/gone.jsp"/>\n<c:import url="/api/orders"/>\n')
    out, res = _copy(tmp_path, {"WebContent/main.jsp": text, "WebContent/api/part.jsp": b"p",
                                "WebContent/api/doc.jsp": b"d"})
    fixed = fix_jsp_paths(out, _layout(), RULES, res.copied)
    assert fixed.replaced == 2
    body = (out / STATIC / "main.jsp").read_text(encoding="utf-8")
    assert f'file="{WEB}/api/part.jsp"' in body and f'page="{WEB}/api/doc.jsp"' in body
    # /api/orders is a controller address; /api/gone.jsp has a file extension and was not copied
    [item] = fixed.queue
    assert item.reason == "[JSP_PATH] /api/gone.jsp"



def _finishes(fn, seconds=5.0):
    """Run fn in a thread; a regression that blocks fails here instead of hanging the suite."""
    import threading
    box: list = []
    th = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    th.start()
    th.join(seconds)
    return box


def _unblock(fifo) -> None:
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes")
def test_named_pipes_are_neither_copied_nor_read(tmp_path):
    src, out1, out2 = tmp_path / "src", tmp_path / "o1", tmp_path / "o2"
    _put(src, "webapp/ok.jsp", b"ok")
    os.mkfifo(src / "webapp/pipe.js")
    os.mkfifo(src / "webapp/pipe.jsp")
    try:
        walked = _finishes(lambda: copy_frontend(src, out1, _layout(), RULES))
        given = _finishes(lambda: copy_frontend(src, out2, _layout(), RULES, files=["webapp/pipe.js", "webapp/ok.jsp"]))
    finally:
        _unblock(src / "webapp/pipe.js")
        _unblock(src / "webapp/pipe.jsp")
    assert walked and given, "the copy blocked on a named pipe"
    assert [s for s, _ in walked[0].copied] == ["webapp/ok.jsp"]
    assert sorted(walked[0].errors) == ["webapp/pipe.js: not a regular file, not copied",
                                        "webapp/pipe.jsp: not a regular file, not copied"]
    assert given[0].errors == ["webapp/pipe.js: not a regular file, not copied"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes")
def test_fix_jsp_paths_and_transform_ajax_skip_named_pipes(tmp_path):
    from sbmig.fwconvert.ajax import transform_ajax
    static = tmp_path / STATIC
    static.mkdir(parents=True)
    os.mkfifo(static / "a.jsp")
    os.mkfifo(static / "b.js")
    try:
        fixed = _finishes(lambda: fix_jsp_paths(tmp_path, _layout(), RULES, [("WebContent/a.jsp", f"{STATIC}/a.jsp")]))
        ajax = _finishes(lambda: transform_ajax(tmp_path, _layout(), RULES))
    finally:
        _unblock(static / "a.jsp")
        _unblock(static / "b.js")
    assert fixed and ajax, "a rewrite blocked on a named pipe"
    assert fixed[0].errors == [f"{STATIC}/a.jsp: not a regular file, not read"]
    assert sorted(ajax[0].errors) == [f"{STATIC}/a.jsp: not a regular file, not rewritten",
                                      f"{STATIC}/b.js: not a regular file, not rewritten"]
