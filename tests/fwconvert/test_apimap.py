"""Page routes, the .mvc prefix and REST calls of sbmig.fwconvert.apimap (docs/FW-CONVERT.md 2.5)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sbmig.fwconvert import apimap
from sbmig.fwconvert.apimap import (
    _api_path, extract_api_map, jsp_parts, mvc_urls, normalise_url, rest_calls, route_name, scan_skipped)
from sbmig.fwconvert.model import RestCall
from sbmig.fwconvert.rules import load_rules

RULES = load_rules("Example")


def _put(root: Path, rel: str, text: str, enc: str = "utf-8") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode(enc))


def test_page_routes_from_every_context_path_form(tmp_path):
    _put(tmp_path, "webapp/a.jsp", """
<a href="${pageContext.request.contextPath}/AB1/AB1C10/AB1C000010.mvc">x</a>
<a href='<%=request.getContextPath()%>/AB1/AB1C10/orderList.mvc?x=1'>y</a>
<a href="<c:url value='/AB1/AB1C10/item_detail.mvc'/>">z</a>
<a href="/AB1/AB1C10/plain.mvc#top">p</a>
<a href="${ctx}/AB1/AB1C10/error.mvc">e</a>
<a href="${ctx}/AB1/AB1C10/plain.mvc">dup</a>
<a href="/not/a/page.do">n</a>
""")
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == ["ab1c000010", "item-detail", "order-list", "plain"]
    assert m.mvc_url_prefix == "/AB1/AB1C10"


@pytest.mark.parametrize("name, want", [("orderList", "order-list"), ("AB1C000010", "ab1c000010"),
                                        ("item_detail", "item-detail"), ("main", "main")])
def test_route_name(name, want):
    assert route_name(name) == want


def test_routes_from_every_mvc_url_in_jsps_and_js(tmp_path):
    _put(tmp_path, "WebContent/WEB-INF/jsp/list.jsp",
         '<a href="${ctx}/P/list.mvc">l</a><form action="${ctx}/P/edit.mvc" method="post"></form>'
         '<script>window.open("<%=request.getContextPath()%>/P/popup.mvc"); location.replace(ctx + "/P/back.mvc");'
         '</script>')
    _put(tmp_path, "WebContent/js/nav.js", 'function go(){ location.href = ctx + "/P/detail.mvc"; }')
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == ["back", "detail", "edit", "list", "popup"]
    assert m.mvc_url_prefix == "/P"


def test_page_urls_keep_the_spelling_of_the_source(tmp_path):
    _put(tmp_path, "menu.jsp", '<a href="${ctx}/AB1/AB1C10/orderList.mvc">o</a>'
                               '<a href="${ctx}/AB1/AB1C10/AB1C000010.mvc">a</a>'
                               '<a href="orderList.mvc">rel</a><a href="/X/order-list.mvc">k</a>'
                               '<a href="../AB1/./AB1C10/orderList.mvc">dots</a>')
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == ["ab1c000010", "order-list"]
    # a relative link is kept exactly as written
    assert m.page_urls == {"ab1c000010": ["/AB1/AB1C10/AB1C000010.mvc"],
                           "order-list": ["../AB1/./AB1C10/orderList.mvc", "/AB1/AB1C10/orderList.mvc",
                                          "/X/order-list.mvc", "orderList.mvc"]}


def test_relative_links_do_not_vote_for_the_prefix(tmp_path):
    _put(tmp_path, "a.jsp", '<a href="/SH1/SH1A10/order-list.mvc">s</a><a href="/SH1/SH1A10/a.mvc">a</a>'
                            '<a href="../SH1A20/order-detail.mvc">d</a><a href="../SH1A20/x.mvc">x</a>'
                            '<a href="../SH1A20/y.mvc">y</a><a href="sub/z.mvc">z</a>')
    m = extract_api_map(tmp_path, RULES)
    assert m.mvc_url_prefix == "/SH1/SH1A10"
    assert m.page_urls["order-detail"] == ["../SH1A20/order-detail.mvc"]
    assert m.page_urls["z"] == ["sub/z.mvc"]


def test_commented_out_urls_do_not_count(tmp_path):
    _put(tmp_path, "a.jsp", '<a href="/SH1/SH1A10/order-list.mvc">x</a><a href="/SH1/SH1A10/order-list.mvc">y</a>'
                            '<%-- <a href="/OLD/A/j1.mvc"></a><a href="/OLD/A/j2.mvc"></a> --%>'
                            '<!-- <a href="/OLD/A/h1.mvc"></a><a href="/OLD/A/h2.mvc"></a> -->'
                            '<script>// go("/OLD/A/s1.mvc")\n/* go("/OLD/A/s2.mvc") */ go("/SH1/SH1A10/live.mvc");'
                            '</script>')
    _put(tmp_path, "b.js", "\n".join(f"// was /OLD/X/legacy{i}.mvc" for i in range(3)) +
         "\n/* /OLD/X/a.mvc /OLD/X/b.mvc */\nvar u = 'http://h/x//y'; // /OLD/X/c.mvc\n")
    m = extract_api_map(tmp_path, RULES)
    assert m.mvc_url_prefix == "/SH1/SH1A10"
    assert m.page_routes == ["live", "order-list"]


def test_old_style_html_comment_inside_a_script_is_script(tmp_path):
    _put(tmp_path, "a.jsp", '<script type="text/javascript"><!--\nfunction go(){ location.href = "/P/next.mvc"; '
                            'api.post("/api/next"); }\n//--></script>')
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == ["next"]
    assert m.rest_endpoints == [RestCall("POST", "/api/next", "a.jsp")]


def test_jsp_event_handlers_and_javascript_urls_are_script(tmp_path):
    _put(tmp_path, "WebContent/js/item.js", "function load() { return api.get('/api/item'); }\n")
    _put(tmp_path, "WebContent/WEB-INF/jsp/item.jsp",
         "<button onclick=\"api.post('/api/item', form)\">save</button>\n"
         "<a href='javascript:api.del(&quot;/api/item/&quot; + id)'>x</a>\n"
         '<a href="/api/not-a-call">plain link</a>\n'
         "<p>api.put('/api/text-only') is not script</p>\n"
         '<input onChange="fetch(\'/api/check\', {method: \'PUT\'})">\n')
    m = extract_api_map(tmp_path, RULES)
    assert {(c.verb, c.path) for c in m.rest_endpoints} == {
        ("GET", "/api/item"), ("POST", "/api/item"), ("DELETE", "/api/item/{id}"), ("PUT", "/api/check")}


@pytest.mark.parametrize("text", ["var re = /'/; api.get('/api/x');", "x = a.replace(/\\(/g, ''); api.get('/api/x');",
                                  "if (/[\"']/.test(s)) { return /\\)/; } api.get('/api/x');",
                                  "var half = a / b; var c = d / e; api.get('/api/x');",
                                  "var s = 'it\\'s'; api.get('/api/x');",
                                  "var t = 'unclosed;\napi.get('/api/x');"])
def test_regex_literals_and_unclosed_strings_do_not_hide_calls(text):
    assert rest_calls(text) == [("GET", "/api/x")]


def test_prefix_vote_most_frequent_then_longest_then_alphabetical(tmp_path):
    _put(tmp_path, "a.jsp", '<a href="/A/one.mvc"></a><a href="/A/two.mvc"></a><a href="/BB/three.mvc"></a>')
    assert extract_api_map(tmp_path, RULES).mvc_url_prefix == "/A"
    _put(tmp_path, "b.js", 'go("/BB/four.mvc");')
    # A and BB both have two votes: the longer wins
    assert extract_api_map(tmp_path, RULES).mvc_url_prefix == "/BB"
    _put(tmp_path, "c.js", 'go("/C/five.mvc"); go("/C/six.mvc");')
    _put(tmp_path, "a.jsp", '<a href="/A/one.mvc"></a><a href="/A/two.mvc"></a>')
    _put(tmp_path, "b.js", 'go("/B/x.mvc"); go("/B/y.mvc");')
    # A, B, C two votes each, same length: alphabetical
    assert extract_api_map(tmp_path, RULES).mvc_url_prefix == "/A"


def test_no_mvc_url_gives_an_empty_prefix(tmp_path):
    _put(tmp_path, "a.jsp", '<a href="x.mvc"></a>')
    m = extract_api_map(tmp_path, RULES)
    assert m.mvc_url_prefix == "" and m.page_routes == ["x"]


def test_js_over_the_size_limit_does_not_vote(tmp_path):
    rules = load_rules("Example", {"frontend": {"js_scan_limit_kb": 1}})
    _put(tmp_path, "a.jsp", '<a href="/A/one.mvc"></a>')
    _put(tmp_path, "big.js", 'go("/BIG/x.mvc"); go("/BIG/y.mvc"); api.get("/api/big");' + " " * 2000)
    m = extract_api_map(tmp_path, rules)
    assert m.mvc_url_prefix == "/A"
    assert m.rest_endpoints == [RestCall("GET", "/api/big", "big.js")]


def test_every_rest_call_form():
    text = """
api.get('/api/order');
api.post("/api/order/save", body);
api.put(`/api/order/${row.id}`, body);
api.del('/api/order/' + id);
fetch('/api/item');
window.fetch("/api/item/" + item.code + "/detail?full=1", { method: 'post', body: x });
fetch(url);
$.ajax({ url: '/api/cart', type: 'POST', data: d });
jQuery.ajax({ method: "delete", url: "/api/cart/" + getId() });
$.ajax({ url: '/api/plain' });
$.ajax('/api/legacy', { type: 'PUT' });
"""
    assert rest_calls(text) == [
        ("GET", "/api/order"), ("POST", "/api/order/save"), ("PUT", "/api/order/{id}"), ("DELETE", "/api/order/{id}"),
        ("GET", "/api/item"), ("POST", "/api/item/{code}/detail"),
        ("POST", "/api/cart"), ("DELETE", "/api/cart/{getId}"), ("GET", "/api/plain"), ("PUT", "/api/legacy")]


@pytest.mark.parametrize("expr, want", [("'/api/x/' + id", "/api/x/{id}"), ("`/api/x/${id}`", "/api/x/{id}"),
                                        ("'/api/x?y=1'", "/api/x"), ("ctx + '/api/x'", "{ctx}/api/x"),
                                        ("url", ""), ("\"/api/a/\" + o.b + '/c'", "/api/a/{b}/c")])
def test_normalise_url(expr, want):
    assert normalise_url(expr) == want


def test_extract_filters_by_prefix_dedups_and_sorts(tmp_path):
    _put(tmp_path, "b.js", "api.get('/api/z'); fetch('/apix/no'); fetch('/other');"
                           "api.get(ctx + '/api/y/' + id);")
    _put(tmp_path, "a.js", "api.get('/api/z'); api.post('/api/z');")
    _put(tmp_path, "p.jsp", "<script>$.ajax({url: '${pageContext.request.contextPath}/api/j', type: 'GET'});</script>")
    m = extract_api_map(tmp_path, RULES)
    assert m.rest_endpoints == [RestCall("GET", "/api/j", "p.jsp"), RestCall("GET", "/api/y/{id}", "b.js"),
                                RestCall("GET", "/api/z", "a.js"), RestCall("POST", "/api/z", "a.js")]


def test_excluded_route_and_files_argument(tmp_path):
    _put(tmp_path, "a.jsp", '<a href="/A/error.mvc"></a><a href="/A/Error.mvc"></a><a href="/A/ok.mvc"></a>')
    _put(tmp_path, "b.jsp", '<a href="/A/other.mvc"></a>')
    m = extract_api_map(tmp_path, RULES, files=["a.jsp", "missing.jsp", "../x.jsp"])
    assert m.page_routes == ["ok"]
    assert "error" not in m.page_urls and m.mvc_url_prefix == "/A"      # excluded routes still vote
    assert scan_skipped(tmp_path, RULES, files=["a.jsp", "missing.jsp"]) == [
        "missing.jsp: cannot read (FileNotFoundError: [Errno 2] No such file or directory: "
        f"'{tmp_path / 'missing.jsp'}')"]


def test_files_over_the_scan_limit_are_skipped_and_named(tmp_path):
    rules = load_rules("Example", {"frontend": {"max_scan_kb": 1}})
    _put(tmp_path, "big.jsp", '<a href="/A/big.mvc"></a>' + " " * 2000)
    _put(tmp_path, "small.jsp", '<a href="/A/small.mvc"></a>')
    m = extract_api_map(tmp_path, rules)
    assert m.page_routes == ["small"]
    assert scan_skipped(tmp_path, rules) == ["big.jsp: larger than the scan limit of 1 KB"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks")
def test_symlinked_folder_outside_the_source_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    _put(outside, "secret.jsp", '<a href="/S/secret.mvc"></a>')
    src = tmp_path / "src"
    _put(src, "WebContent/a.jsp", '<a href="/A/a.mvc"></a>')
    os.symlink(outside, src / "WebContent/link")
    m = extract_api_map(src, RULES, files=["WebContent/a.jsp", "WebContent/link/secret.jsp"])
    assert m.page_routes == ["a"]
    assert scan_skipped(src, RULES, files=["WebContent/link/secret.jsp"]) == [
        "WebContent/link/secret.jsp: resolves to a file outside the source folder"]


def test_ms949_jsp_is_read(tmp_path):
    _put(tmp_path, "a.jsp", '<%-- 주문 화면 --%><a href="/A/order.mvc">주문</a>', "cp949")
    assert extract_api_map(tmp_path, RULES).page_routes == ["order"]


# ---- hostile and minified input stays linear ----------------------------------------------------------

MB2 = 2 * 1024 * 1024
LIMIT_S = 5.0


def _timed(fn, *args):
    t = time.perf_counter()
    fn(*args)
    return time.perf_counter() - t


def _fill(unit: str) -> str:
    return unit * (MB2 // len(unit))


@pytest.mark.parametrize("unit", ["/a.mvc", "a", "<c:url ", "api.get(", "fetch('/api/x', {", "(", "'",
                                  "/*", "api.get('/api/x' + (", "{[(", "<%=a%>", "a.mvc/", "a.mvcb", "\\'", "x=/[",
                                  "s.replace(/\\(/g,'');", "a[i]];", "<!--", "<script>", 'onclick="', "]", "`a",
                                  "\\'\\\n", '\\"\\\n', "'a\n", "(/a\n", "(/[a\n", "=/", "${", "<%",
                                  'onclick="${', '<a onclick="x" ', "<script>a</script>", "<%--"])
def test_two_megabytes_of_one_shape_scan_quickly(tmp_path, unit):
    text = _fill(unit)
    assert _timed(mvc_urls, text, ".mvc") < LIMIT_S
    assert _timed(rest_calls, text) < LIMIT_S
    _put(tmp_path, "x.jsp", text)
    _put(tmp_path, "x.js", text)
    assert _timed(extract_api_map, tmp_path, RULES) < 2 * LIMIT_S


def test_context_path_strip_is_linear():
    path = "<%=a%>" * 400_000 + "x"
    assert _timed(_api_path, path, "/api") < LIMIT_S
    assert _api_path("<%=a%>{b}${c}/api/x", "/api") == "/api/x"
    assert _api_path("<%=a%>/other", "/api") == ""


def test_nested_closed_calls_are_linear():
    text = "api.get(" * 100_000 + "'/api/x'" + ")" * 100_000
    assert _timed(rest_calls, text) < LIMIT_S
    assert ("GET", "/api/x") in rest_calls(text)


def test_unclosed_call_is_skipped_and_a_later_one_still_read():
    text = "fetch('/api/broken', {" + " " * 10_000 + "\nfetch('/api/ok');"
    assert rest_calls(text) == [("GET", "/api/ok")]


@pytest.mark.parametrize("opening, closing", [("(", "]"), ("[", ")"), ("{", ")"), ("(", "}")])
def test_unmatched_closers_are_linear(opening, closing):
    half = MB2 // 2
    text = "api.get('/api/x');" + opening * half + closing * half
    assert _timed(rest_calls, text) < LIMIT_S
    assert rest_calls(text) == [("GET", "/api/x")]
    shape = "s.replace(/\\(/g,'');" * (half // 20) + "a[i]];" * (half // 6)
    assert _timed(rest_calls, "api " + shape) < LIMIT_S



def test_escaped_quotes_in_continued_lines_are_linear():
    for q in ("'", '"'):
        shape = f"\\{q}\\\n" * (MB2 // 4)
        assert _timed(rest_calls, "api.get('/api/x');\n" + shape) < LIMIT_S
        assert _timed(jsp_parts, "<script>" + shape + "</script>") < LIMIT_S
    assert rest_calls("api.get('/api/x');\n" + "\\'\\\n" * 1000) == [("GET", "/api/x")]


def test_the_work_wall_stops_a_file_whatever_its_shape(tmp_path, monkeypatch):
    _put(tmp_path, "a.js", "api.get('/api/a'); go('/A/a.mvc');")
    _put(tmp_path, "b.jsp", '<a href="/B/b.mvc"></a><script>api.post("/api/b")</script>')
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == ["a", "b"] and len(m.rest_endpoints) == 2
    assert scan_skipped(tmp_path, RULES) == []
    # a budget far below the normal work: every file stops, gives nothing and is named
    monkeypatch.setattr(apimap, "_WORK_FACTOR", 0)
    monkeypatch.setattr(apimap, "_WORK_SLACK", 5)
    m = extract_api_map(tmp_path, RULES)
    assert m.page_routes == [] and m.rest_endpoints == [] and m.mvc_url_prefix == ""
    assert scan_skipped(tmp_path, RULES) == [f"a.js: {apimap.TOO_MUCH_WORK}", f"b.jsp: {apimap.TOO_MUCH_WORK}"]
    assert rest_calls("api.get('/api/a');") == [] and mvc_urls("/A/a.mvc", ".mvc") == []


def test_the_work_budget_leaves_room_for_ordinary_files(tmp_path):
    js = "".join(f"function f{i}(id) {{ return api.get('/api/x/' + id, {{a: [1, 2]}}); }} // /A/p{i}.mvc\n"
                 for i in range(2000))
    jsp = "".join(f'<a href="/A/p{i}.mvc" onclick="api.post(\'/api/y\')">x</a><script>go("/A/q{i}.mvc")</script>\n'
                  for i in range(2000))
    _put(tmp_path, "a.js", js)
    _put(tmp_path, "b.jsp", jsp)
    assert scan_skipped(tmp_path, RULES) == []
    assert len(extract_api_map(tmp_path, RULES).page_routes) == 4000


def test_jsp_expression_and_el_holding_quotes_inside_an_event_handler(tmp_path):
    _put(tmp_path, "a.jsp",
         '<button onclick="api.post(\'<%= request.getAttribute("ctx") %>/api/order/save\')">go</button>\n'
         '<a href="#" onclick="api.get(\'${fn:replace(ctx, "x", "")}/api/order/list\')">x</a>\n'
         '<a onclick="api.del(&quot;/api/order/&quot; + id)">x</a>\n')
    m = extract_api_map(tmp_path, RULES)
    assert [(c.verb, c.path) for c in m.rest_endpoints] == [
        ("GET", "/api/order/list"), ("POST", "/api/order/save"), ("DELETE", "/api/order/{id}")]


def test_division_after_jsp_expressions_is_not_a_regex(tmp_path):
    assert rest_calls('var half = ${total} / 2; api.get("/api/order/list");\n', jsp=True) == \
        [("GET", "/api/order/list")]
    assert rest_calls('var h = <%= n %> / 2; api.get("/api/a"); var g = #{m} / 3;', jsp=True) == [("GET", "/api/a")]
    _put(tmp_path, "a.jsp", '<script>var half = ${total} / 2; api.get("${ctx}/api/order/list");</script>')
    m = extract_api_map(tmp_path, RULES)
    assert [(c.verb, c.path) for c in m.rest_endpoints] == [("GET", "/api/order/list")]



def test_escaped_el_in_a_jsp_is_a_browser_template_placeholder(tmp_path):
    _put(tmp_path, "a.jsp", "<script>var contextPath = '${pageContext.request.contextPath}';\n"
                            "api.get(`/api/items/\\${id}`);\napi.get(`\\${contextPath}/api/order/list`);\n"
                            "api.post(`/api/x/\\#{k}`);</script>")
    m = extract_api_map(tmp_path, RULES)
    assert [(c.verb, c.path) for c in m.rest_endpoints] == [
        ("GET", "/api/items/{id}"), ("GET", "/api/order/list"), ("POST", "/api/x/")]
    assert rest_calls("api.get(`/api/items/\\${id}`);", jsp=True) == [("GET", "/api/items/{id}")]


@pytest.mark.parametrize("text", ["var OPEN = '${';\nfunction load() { api.get('/api/a'); }\nvar CLOSE = '}';\n",
                                  "var o = '<%';\nfunction load() { api.get('/api/a'); }\nvar c = '%>';\n",
                                  "var o = '#{';\nfunction load() { api.get('/api/a'); }\nvar c = '}';\n"])
def test_a_js_file_is_lexed_as_it_is(tmp_path, text):
    assert rest_calls(text) == [("GET", "/api/a")]
    _put(tmp_path, "a.js", text)
    assert [(c.verb, c.path) for c in extract_api_map(tmp_path, RULES).rest_endpoints] == [("GET", "/api/a")]


@pytest.mark.parametrize("unit", ["${a}", "<%a%>", "#{a}", "${\"x\"}"])
def test_one_long_closed_attribute_full_of_spans_is_linear(tmp_path, unit):
    text = '<a onclick="api.get(\'/api/x\'); ' + unit * (MB2 // len(unit)) + '">x</a>'
    assert _timed(jsp_parts, text) < LIMIT_S
    _put(tmp_path, "x.jsp", text)
    assert _timed(extract_api_map, tmp_path, RULES) < LIMIT_S
    assert _timed(scan_skipped, tmp_path, RULES) < LIMIT_S
    assert [(c.verb, c.path) for c in extract_api_map(tmp_path, RULES).rest_endpoints] == [("GET", "/api/x")]


def test_the_work_factor_has_room_for_el_dense_jsps(tmp_path):
    assert apimap._WORK_FACTOR == 32
    row = ("data.push({ id: '${row.id}', name: '${fn:escapeXml(row.name)}', amt: ${row.amt} / 100, "
           "url: '${ctx}/AB1/AB1C10/detail.mvc?id=${row.id}' });\n")
    body = ('<c:forEach var="row" items="${list}">\n' + row * 3 + "</c:forEach>\n") * 2000
    _put(tmp_path, "x.jsp", "<script>\nvar data = [];\n" + body + "api.get('${ctx}/api/order/list');\n</script>\n")
    assert scan_skipped(tmp_path, RULES) == []


# ---- character references, the per-file net, files that are not regular files --------------------------------

def test_a_numeric_charref_longer_than_any_code_point_is_left_as_written(tmp_path):
    text = ('<button onclick="api.post(\'/api/save\')">s</button>\n'
            '<a onclick="x(\'&#' + "1" * 5000 + ';\')">x</a>\n<a href="/A/p.mvc">p</a>\n')
    blanked, scripts = jsp_parts(text)
    assert any("&#" + "1" * 5000 in piece for piece in scripts)
    assert apimap._unescape("&#65;&amp;&#123456789;&#x41;") == "A&&#123456789;A"
    _put(tmp_path, "a.jsp", text)
    _put(tmp_path, "b.js", "api.get('/api/b');")
    m = extract_api_map(tmp_path, RULES)
    assert {c.path for c in m.rest_endpoints} == {"/api/save", "/api/b"} and m.page_routes == ["p"]
    assert scan_skipped(tmp_path, RULES) == []


def test_an_unexpected_error_skips_only_that_file(tmp_path, monkeypatch):
    _put(tmp_path, "a.js", "api.get('/api/a'); go('/A/a.mvc');")
    _put(tmp_path, "boom.js", "api.get('/api/boom'); go('/B/boom.mvc');")
    real = apimap._mvc_urls

    def flaky(text, suffix, budget):
        if "boom" in text:
            raise ValueError("unexpected")
        return real(text, suffix, budget)

    monkeypatch.setattr(apimap, "_mvc_urls", flaky)
    m = extract_api_map(tmp_path, RULES)
    assert [c.path for c in m.rest_endpoints] == ["/api/a"] and m.page_routes == ["a"]
    assert scan_skipped(tmp_path, RULES) == ["boom.js: scan failed: ValueError"]
    assert mvc_urls("/B/boom.mvc", ".mvc") == []


def test_public_helpers_return_empty_on_an_unexpected_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(apimap, "_calls", boom)
    assert rest_calls("api.get('/api/a');") == []
    monkeypatch.setattr(apimap, "_jsp_parts", boom)
    assert jsp_parts("<script>x</script>") == ("<script>x</script>", [])
    monkeypatch.setattr(apimap._Js, "url", boom)
    assert normalise_url("'/api/x/' + id") == ""


def _finishes(fn, seconds=5.0):
    """Run fn in a thread; a regression that blocks fails here instead of hanging the suite."""
    import threading
    box: list = []
    th = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    th.start()
    th.join(seconds)
    return box


def _unblock(fifo: Path) -> None:
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes")
def test_a_named_pipe_is_not_read(tmp_path):
    _put(tmp_path, "a.js", "api.get('/api/a');")
    os.mkfifo(tmp_path / "x.js")
    try:
        got = _finishes(lambda: (extract_api_map(tmp_path, RULES), scan_skipped(tmp_path, RULES)))
    finally:
        _unblock(tmp_path / "x.js")
    assert got, "the scan blocked on a named pipe"
    m, skipped = got[0]
    assert [c.path for c in m.rest_endpoints] == ["/api/a"]
    assert skipped == ["x.js: not a regular file"]


def test_an_escaped_el_opener_in_an_attribute_is_not_a_span(tmp_path):
    _put(tmp_path, "a.jsp", '<a onclick="alert(\'\\${\')">x</a>\n<button onclick="api.post(\'/api/save\')">s</button>\n'
                            '<a onclick="alert(\'\\#{ \')">y</a><a onclick="api.get(\'/api/two\')">z</a>\n')
    m = extract_api_map(tmp_path, RULES)
    assert [(c.verb, c.path) for c in m.rest_endpoints] == [("POST", "/api/save"), ("GET", "/api/two")]
