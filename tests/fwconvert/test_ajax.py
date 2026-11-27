"""The ajax rewrite of sbmig.fwconvert.ajax (docs/FW-CONVERT.md 2.5, scenario A9)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.fwconvert.ajax import infer_service, rewrite_ajax_text, transform_ajax
from sbmig.fwconvert.model import Layout, ModuleNames, ProjectIdentity
from sbmig.fwconvert.rules import load_rules

RULES = load_rules("Example")


def _layout() -> Layout:
    return Layout(ProjectIdentity("shopProject", "d1", "com.example.d1.shopProject",
                                  ModuleNames("shopProject-boot", "shopProject-domain", "shopProject-service",
                                              "shopProject-store")))


def _rw(text: str, name: str = "order.js"):
    return rewrite_ajax_text(text, name, RULES)


A9_SOURCE = """var serviceName = "order";
function a(t) { return "/ctx/commonAjax.json?ServiceName=order-service&"+t+"=1&page=2"; }
function b(sn, t) { return "commonAjax.json?ServiceName="+sn+"-service&"+t+"=1"; }
function c(t) { return "commonAjax.json?"+t+"=1"; }
function d(t) { return "commonAjax.json?isPage=1&"+t+"=1"; }
function e(t) { return '/ctx/commonAjax.json?ServiceName=item-service&'+t+'=1&x=1'; }
function f(sn, t) { return 'commonAjax.json?ServiceName='+sn+'-service&'+t+'=1'; }
"""

A9_EXPECTED = """var serviceName = "order";
function a(t) { return "/api/order/"+t+"?page=2"; }
function b(sn, t) { return "/api/"+sn+"/"+t; }
function c(t) { return "/api/order/"+t; }
function d(t) { return "/api/order/"+t+"?isPage=1"; }
function e(t) { return '/api/item/'+t+'?x=1'; }
function f(sn, t) { return '/api/'+sn+'/'+t; }
"""


def test_a9_every_pattern_is_rewritten():
    out, count, bad = _rw(A9_SOURCE)
    assert out == A9_EXPECTED
    assert count == 6 and bad is False


def test_a9_an_unknown_shape_leaves_the_file_untouched(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    text = A9_SOURCE + 'function g(t) { return "commonAjax.json?mode=x&"+t+"=1"; }\n'
    (static / "order.js").write_text(text, encoding="utf-8")
    raw = (static / "order.js").read_bytes()
    res = transform_ajax(tmp_path, lay, RULES)
    assert (static / "order.js").read_bytes() == raw
    assert res.unmatched_files == [f"{lay.static_dir}/order.js"]
    assert res.files_transformed == [] and res.replaced == 0
    [item] = res.queue
    assert item.component == "js_ajax::order.js"
    assert item.reason.startswith("[AJAX_UNMATCHED] ")
    assert item.reason == "[AJAX_UNMATCHED] unknown query parameter (line 8, column 25)"
    assert item.last_error == 'function g(t) { return "commonAjax.json?mode=x&"+t+"=1"; }'
    assert item.source_path == f"{lay.static_dir}/order.js"
    assert item.excerpt == text[:2000]
    assert _rw(text) == (text, 0, True)


def test_whitespace_around_plus_is_kept_and_context_expression_in_front_stays():
    src = "u = ctx + '/AB1/commonAjax.json?ServiceName=order-service&' +\n    form.tx.value  +  '=1&rows=10';"
    out, count, bad = _rw(src)
    assert out == "u = ctx + '/api/order/' +\n    form.tx.value  +  '?rows=10';"
    assert (count, bad) == (1, False)


def test_el_context_path_inside_the_literal_is_dropped():
    src = 'u = "${pageContext.request.contextPath}/commonAjax.json?ServiceName=Order-service&"+t+"=1";'
    assert _rw(src)[0] == 'u = "/api/Order/"+t;'


@pytest.mark.parametrize("t", ["tx", "a.b", "a[b]", "a[\"k\"]", "getTx()", "obj.get(1).name"])
def test_t_expressions(t):
    src = f'u = "commonAjax.json?ServiceName=x-service&"+{t}+"=1";'
    assert _rw(src)[0] == f'u = "/api/x/"+{t};'


def test_rest_is_kept_and_no_dangling_question_mark_or_ampersand():
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1&a=1&b=2";')[0] == 'u = "/api/x/"+t+"?a=1&b=2";'
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1&";')[0] == 'u = "/api/x/"+t;'
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1";')[0] == 'u = "/api/x/"+t;'


def test_more_concatenation_after_the_flag_is_kept():
    # the literal ended with "&": the next operand is a new parameter
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1&" + q;')[0] == 'u = "/api/x/"+t+"?" + q;'
    assert _rw('u = "commonAjax.json?isPage=1&"+t+"=1&" + q;', "x.js")[0] == 'u = "/api/x/"+t+"?isPage=1&" + q;'
    # nothing of the query left: the next literal's leading & becomes ?
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1" + "&a=" + v;')[0] == \
        'u = "/api/x/"+t + "?a=" + v;'
    # a query left: the next literal stays as it was
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1&k=2" + "&a=" + v;')[0] == \
        'u = "/api/x/"+t+"?k=2" + "&a=" + v;'
    # a non-literal continuation gets a "?" in front of it, with the source's spacing and quote
    assert _rw('u = "commonAjax.json?ServiceName=x-service&"+t+"=1" + extra;')[0] == 'u = "/api/x/"+t+"?" + extra;'
    assert _rw('u = "commonAjax.json?ServiceName=order-service&" + tx + "=1" + qs;')[0] == \
        'u = "/api/order/" + tx + "?" + qs;'
    assert _rw("u = 'commonAjax.json?ServiceName=order-service&' + tx + '=1' + qs;")[0] == \
        "u = '/api/order/' + tx + '?' + qs;"


def test_b_with_page_flag_after_the_service_suffix():
    src = "u = 'commonAjax.json?ServiceName=' + sn + '-service&isPage=1&' + t + '=1';"
    assert _rw(src)[0] == "u = '/api/' + sn + '/' + t + '?isPage=1';"


def test_inference_order_form_field_first():
    src = ('doc.ServiceName.value = "field-service";\nvar serviceName = "var";\n'
           'u = "commonAjax.json?"+t+"=1";')
    assert _rw(src, "file.js")[0].endswith('u = "/api/field/"+t;')
    assert infer_service("document.frm.ServiceName.value='single-service';", "f.js", RULES) == "single"


def test_inference_variable_second():
    src = "let serviceName = 'var';\nu = \"commonAjax.json?\"+t+\"=1\";"
    assert _rw(src, "file.js")[0].endswith('u = "/api/var/"+t;')
    assert infer_service('const serviceName = "c";', "f.js", RULES) == "c"


def test_inference_file_name_last():
    assert _rw('u = "commonAjax.json?"+t+"=1";', "js/order.js")[0] == 'u = "/api/order/"+t;'
    assert infer_service("", "item-service.js", RULES) == "item"
    assert infer_service("", "list.min.js", RULES) == "list"


@pytest.mark.parametrize("name", ["common.js", "commonUtil.js", "CommonAjax.js"])
def test_common_files_never_use_their_name(name):
    src = 'u = "commonAjax.json?"+t+"=1";'
    assert _rw(src, name) == (src, 0, True)
    # with a variable in the file the inference works again
    assert _rw('var serviceName = "s";\n' + src, name)[1] == 1


def test_service_without_suffix_is_unmatched():
    src = 'u = "commonAjax.json?ServiceName=order&"+t+"=1";'
    assert _rw(src) == (src, 0, True)


def test_endpoint_left_in_a_comment_makes_the_file_unmatched():
    src = '// calls commonAjax.json\nu = "commonAjax.json?ServiceName=x-service&"+t+"=1";'
    assert _rw(src) == (src, 0, True)


def test_file_without_endpoint_is_not_touched_or_listed(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    (static / "a.js").write_text("var x = 1;\n", encoding="utf-8")
    res = transform_ajax(tmp_path, lay, RULES)
    assert res.files_transformed == [] and res.unmatched_files == [] and res.queue == [] and res.errors == []
    assert _rw("var x = 1;") == ("var x = 1;", 0, False)


def test_ms949_file_with_korean_comments_is_byte_identical_outside_the_call(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir / "js"
    static.mkdir(parents=True)
    before = "// 주문 목록을 조회한다\r\nvar 이름 = '한글';\r\n"
    after = "\r\n// 끝\r\n"
    call_old = 'u = "commonAjax.json?ServiceName=order-service&"+t+"=1";'
    call_new = 'u = "/api/order/"+t;'
    (static / "order.js").write_bytes((before + call_old + after).encode("cp949"))
    res = transform_ajax(tmp_path, lay, RULES)
    assert res.files_transformed == [f"{lay.static_dir}/js/order.js"] and res.replaced == 1
    assert (static / "order.js").read_bytes() == (before + call_new + after).encode("cp949")


def test_crlf_and_utf8_bom_are_kept(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    raw = b'\xef\xbb\xbfvar a = 1;\r\nu = "commonAjax.json?ServiceName=x-service&"+t+"=1";\r\n'
    (static / "x.js").write_bytes(raw)
    res = transform_ajax(tmp_path, lay, RULES)
    assert res.replaced == 1
    assert (static / "x.js").read_bytes() == b'\xef\xbb\xbfvar a = 1;\r\nu = "/api/x/"+t;\r\n'


def test_queue_reason_names_the_inference_failure(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir / "common"
    static.mkdir(parents=True)
    (static / "commonLib.js").write_text('u = "commonAjax.json?"+t+"=1";\n', encoding="utf-8")
    res = transform_ajax(tmp_path, lay, RULES)
    [item] = res.queue
    assert item.component == "js_ajax::common/commonLib.js"
    assert item.reason == "[AJAX_UNMATCHED] no service name for a call without ServiceName (line 1, column 6)"
    assert item.last_error == 'u = "commonAjax.json?"+t+"=1";'


def test_names_come_from_the_rules():
    rules = load_rules("Example", {"frontend": {"ajax_endpoint": "gate.do", "service_param": "svc", "page_param": "pg",
                                                "api_prefix": "/rest"}, "naming": {"service_suffix": "-svc"}})
    out, count, bad = rewrite_ajax_text('u = "gate.do?svc=x-svc&pg=1&"+t+"=1";', "a.js", rules)
    assert (out, count, bad) == ('u = "/rest/x/"+t+"?pg=1";', 1, False)
    out, _, bad = rewrite_ajax_text('var svc = "v";\nu = "gate.do?"+t+"=1";', "common.js", rules)
    assert out.endswith('u = "/rest/v/"+t;') and not bad


# ---- JSP files under the static folder ----------------------------------------------------------------

def test_jsp_inline_pattern_a_call_is_rewritten(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir / "p"
    static.mkdir(parents=True)
    src = ('<%@ page contentType="text/html" %>\n<script>\n'
           'var u = "<%=request.getContextPath()%>/commonAjax.json?ServiceName=order-service&"+t+"=1&page=2";\n'
           "var serviceName = 'item';\n"
           'var v = "${pageContext.request.contextPath}/commonAjax.json?"+t+"=1";\n</script>\n')
    (static / "orderList.jsp").write_text(src, encoding="utf-8")
    res = transform_ajax(tmp_path, lay, RULES)
    assert res.files_transformed == [f"{lay.static_dir}/p/orderList.jsp"] and res.replaced == 2
    assert (static / "orderList.jsp").read_text(encoding="utf-8") == (
        '<%@ page contentType="text/html" %>\n<script>\n'
        'var u = "/api/order/"+t+"?page=2";\n'
        "var serviceName = 'item';\n"
        'var v = "/api/item/"+t;\n</script>\n')
    assert res.services == {"p/orderList.jsp": ["item", "order"]}


def test_jsp_never_uses_its_file_name_for_the_service(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    raw = b'<script>var v = "commonAjax.json?"+t+"=1";</script>\n'
    (static / "order.jsp").write_bytes(raw)
    res = transform_ajax(tmp_path, lay, RULES)
    assert (static / "order.jsp").read_bytes() == raw
    [item] = res.queue
    assert item.component == "js_ajax::order.jsp" and "no service name" in item.reason
    assert res.services == {}
    assert infer_service("", "order.jsp", RULES) == "" and infer_service("", "order.js", RULES) == "order"


def test_services_named_literally_by_rewritten_calls(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir / "js"
    static.mkdir(parents=True)
    (static / "order.js").write_text(A9_SOURCE, encoding="utf-8")
    (static / "dyn.js").write_text('u = "commonAjax.json?ServiceName="+sn+"-service&"+t+"=1";', encoding="utf-8")
    (static / "Shop.js").write_text('u = "commonAjax.json?ServiceName=Shop-Item-service&"+t+"=1";\n'
                                    'v = "commonAjax.json?"+t+"=1";', encoding="utf-8")
    res = transform_ajax(tmp_path, lay, RULES)
    # A: order (written), C / D: order (the variable), E: item; B / F: expressions, not listed
    assert res.services == {"js/order.js": ["item", "order"], "js/Shop.js": ["Shop", "Shop-Item"]}
    assert f"{lay.static_dir}/js/dyn.js" in res.files_transformed


def test_jsp_with_an_unknown_shape_is_left_byte_identical(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    raw = b'<script>var u = "commonAjax.json?mode=1&"+t+"=1";</script>\r\n'
    (static / "a.jsp").write_bytes(raw)
    res = transform_ajax(tmp_path, lay, RULES)
    assert (static / "a.jsp").read_bytes() == raw
    [item] = res.queue
    assert item.component == "js_ajax::a.jsp" and item.reason.startswith("[AJAX_UNMATCHED] ")
    assert res.unmatched_files == [f"{lay.static_dir}/a.jsp"]


def test_ms949_jsp_keeps_its_bytes_outside_the_call(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    head = '<%-- 주문 화면 --%>\r\n<p>주문 목록</p>\r\n<script>var u = '
    (static / "o.jsp").write_bytes((head + '"commonAjax.json?ServiceName=order-service&"+t+"=1";</script>')
                                   .encode("cp949"))
    res = transform_ajax(tmp_path, lay, RULES)
    assert res.replaced == 1
    assert (static / "o.jsp").read_bytes() == (head + '"/api/order/"+t;</script>').encode("cp949")


def test_file_over_the_scan_limit_is_named_and_not_read(tmp_path):
    rules = load_rules("Example", {"frontend": {"max_scan_kb": 1}})
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    raw = ('u = "commonAjax.json?ServiceName=x-service&"+t+"=1";' + " " * 2000).encode()
    (static / "big.js").write_bytes(raw)
    res = transform_ajax(tmp_path, lay, rules)
    assert (static / "big.js").read_bytes() == raw
    assert res.errors == [f"{lay.static_dir}/big.js: larger than the scan limit of 1 KB, not read"]


# ---- hostile and minified input stays linear ----------------------------------------------------------

MB2 = 2 * 1024 * 1024


@pytest.mark.parametrize("unit", ['u="commonAjax.json?ServiceName=x-service&"+t+"=1";',
                                  'u="commonAjax.json?"+t+"=1";',
                                  "commonAjax.json", '"commonAjax.json?"+a(', "'", "a"])
def test_two_megabytes_of_one_shape_rewrite_quickly(unit):
    import time
    text = unit * (MB2 // len(unit)) + ' x = "commonAjax.json?ServiceName=x-service&"+t+"=1";'
    t0 = time.perf_counter()
    rewrite_ajax_text(text, "order.js", RULES)
    assert time.perf_counter() - t0 < 5.0



def test_the_reason_never_carries_text_of_the_file(tmp_path):
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    line = 'ajax("/shop/commonAjax.json?ServiceName=" + s, {dbPassword: "Zq9JsVal7"});'
    (static / "a.js").write_text("// header\nvar x = 1;\n" + line + "\n", encoding="utf-8")
    res = transform_ajax(tmp_path, lay, RULES)
    [item] = res.queue
    assert item.reason.startswith("[AJAX_UNMATCHED] ") and "(line 3, column 13)" in item.reason
    for piece in ("Zq9JsVal7", "dbPassword", "/shop/", "ajax("):
        assert piece not in item.reason
    assert item.last_error == line


def test_the_line_number_of_a_left_endpoint_points_into_the_original_file(tmp_path):
    # the call is rewritten, but its expression t still names the endpoint: the position in the rewritten text is
    # mapped back to the start of the call in the original file (line 2, column 5)
    src = 'var a = 1;\nu = "commonAjax.json?ServiceName=x-service&" +\n  m["commonAjax.json"] +\n  "=1";\n'
    lay = _layout()
    static = tmp_path / lay.static_dir
    static.mkdir(parents=True)
    (static / "a.js").write_text(src, encoding="utf-8")
    [item] = transform_ajax(tmp_path, lay, RULES).queue
    assert item.reason == "[AJAX_UNMATCHED] endpoint name still present (line 2, column 5)"
    assert item.last_error == 'u = "commonAjax.json?ServiceName=x-service&" +'
    assert (static / "a.js").read_text(encoding="utf-8") == src
    # a comment that names the endpoint stops earlier, at the comment itself
    (static / "a.js").write_text("var a = 1;\n// see commonAjax.json\n", encoding="utf-8")
    [item] = transform_ajax(tmp_path, lay, RULES).queue
    assert item.reason == "[AJAX_UNMATCHED] endpoint outside a string literal (line 2, column 8)"
