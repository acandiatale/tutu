"""The converters of sbmig.fwconvert run one after another on a small project: what one writes, the next must find.

Every unit test of this folder feeds a module records built by hand. This one runs the whole pipeline (``convert_project``) and checks the borders: the statement a
generated store calls exists in a generated mapper, the address a rewritten script calls is served by a generated
controller, and a page link of a JSP reaches a page controller."""
from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

from sbmig.fwconvert.model import ConvertOptions, Layout
from sbmig.fwconvert.pipeline import convert_project
from sbmig.fwconvert.rules import load_rules

OVERLAY = {
    "naming": {"package_root": "com.example", "domains": ["d1"], "default_domain": "d1"},
    "activities": {"kinds": {"ShopUpsert": "upsert", "ShopDelete": "delete"}, "extra_db_prefixes": ["Shop"],
                   "external_calls": {"extcall": ["remotecall"]}},
}

ORDER_SQL = """<?xml version="1.0" encoding="UTF-8"?>
<queryMap desc="orders" xmlns="http://example.invalid/query">
  <query id="order.getList" isNamed="true"><![CDATA[ SELECT ORDER_ID, AMOUNT FROM TB_ORDER WHERE CUST = :cust ]]></query>
  <query id="order.save" isNamed="false"><![CDATA[ INSERT INTO TB_ORDER (CUST, AMOUNT) VALUES (?, ?) ]]></query>
  <query id="order.remove" isNamed="false"><![CDATA[ DELETE FROM TB_ORDER WHERE ORDER_ID = ? ]]></query>
</queryMap>
"""

ITEM_SQL = """<?xml version="1.0" encoding="UTF-8"?>
<queryMap desc="items">
  <query id="getList"><![CDATA[ SELECT ITEM_ID FROM TB_ITEM ]]></query>
  <query id="countAll"><![CDATA[ SELECT COUNT(*) FROM TB_ITEM ]]></query>
</queryMap>
"""

ORDER_SERVICE = """<?xml version="1.0" encoding="UTF-8"?>
<service name="order-service">
  <activity name="list" class="com.example.fw.activity.ExampleMyBatisSearch">
    <property name="sql-key" value="order.getList"/>
    <property name="result-key" value="rows"/>
  </activity>
  <activity name="store" class="com.example.shop.activity.ShopUpsert">
    <property name="sql-key" value="order.save"/>
  </activity>
  <activity name="drop" class="com.example.shop.activity.ShopDelete">
    <property name="sql-key" value="order.remove"/>
  </activity>
  <activity name="notify" class="com.example.shop.activity.RemoteCallSender"/>
  <activity name="router" class="com.example.fw.activity.ExampleDefaultRouter">
    <transition name="find" value="list"/>
    <transition name="save" value="store"/>
    <transition name="remove" value="drop"/>
    <transition name="notify" value="notify"/>
  </activity>
</service>
"""

ORDER_JSP = """<%@ page contentType="text/html; charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jsp" %>
<html><body>
<a href="${pageContext.request.contextPath}/SH1/SH1A10/order-list.mvc">orders</a>
<a href="${pageContext.request.contextPath}/SH1/SH1A10/item-list.mvc">items</a>
<form action="${pageContext.request.contextPath}/SH1/SH1A10/orderDetail.mvc" method="post"></form>
<script src="${pageContext.request.contextPath}/js/order.js"></script>
<script>
  function reload(tran) { return "commonAjax.json?ServiceName=order-service&" + tran + "=1"; }
</script>
<jsp:include page="/WEB-INF/jsp/common/missing.jspf"/>
</body></html>
"""

ORDER_JS = """function load(tran) {
  var url = "/shop/commonAjax.json?ServiceName=order-service&" + tran + "=1&cust=7";
  return url;
}
"""


def _project(root: Path) -> None:
    files = {
        "src/main/resources/sql/order.example_sql": ORDER_SQL,
        "src/main/resources/sql/item.example_sql": ITEM_SQL,
        "src/main/resources/service/order-service.xml": ORDER_SERVICE,
        "WebContent/WEB-INF/jsp/shop/order-list.jsp": ORDER_JSP,
        "WebContent/WEB-INF/jsp/shop/item-list.jsp": "<html><body>items</body></html>\n",
        "WebContent/WEB-INF/jsp/shop/orderDetail.jsp": "<html><body>detail</body></html>\n",
        "WebContent/WEB-INF/jsp/common/header.jsp": "<div>head</div>\n",
        "WebContent/js/order.js": ORDER_JS,
        "WebContent/css/shop.css": "body { margin: 0 }\n",
        "WebContent/WEB-INF/web.xml": "<web-app/>\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


OPTIONS = ConvertOptions(source_name="sample-shop-clean.zip", domain="d1", boot_version="4.1.0",
                         versions={"org.mybatis.spring.boot:mybatis-spring-boot-starter": "4.1.0"})


def _convert(src: Path, out: Path):
    result = convert_project(src, out, load_rules("Example", OVERLAY), OPTIONS)
    assert result.converted and result.identity is not None
    return Layout(result.identity), result


def _page_urls(out: Path) -> list[str]:
    urls: list[str] = []
    for p in sorted(out.rglob("*PageController.java")):
        for args in re.findall(r"@RequestMapping\((.*)\)", p.read_text(encoding="utf-8")):
            urls += re.findall(r'"(/[^"]*)"', args)
    return sorted(urls)


def _statement_keys(out: Path, layout: Layout) -> set[str]:
    keys: set[str] = set()
    for xml in sorted((out / layout.mapper_dir).glob("*.xml")):
        root = etree.fromstring(xml.read_bytes(), etree.XMLParser(load_dtd=False, no_network=True))
        for st in root:
            if isinstance(st.tag, str) and st.get("id"):
                keys.add(f"{root.get('namespace')}.{st.get('id')}")
    return keys


def _java(out: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.java"))}


def test_the_whole_chain_leaves_no_error_and_the_expected_layout(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    layout, result = _convert(src, out)
    queue = result.queue
    assert result.errors == []
    assert layout.identity.project_name == "shopProject" and layout.pkg == "com.example.d1.shopProject"
    spec = out / layout.identity.modules.domain / "src/main/java/com/example/d1/shopProject/domain/spec"
    assert sorted(p.name.removesuffix("Service.java") for p in spec.glob("*Service.java")) == ["Item", "Order"]
    assert result.counts["resources"] == 2
    names = set(_java(out))
    assert {"OrderService.java", "OrderStore.java", "OrderLogic.java", "OrderSpringLogic.java",
            "OrderMybatisStore.java", "OrderController.java", "ItemService.java", "ItemStore.java",
            "ItemLogic.java", "ItemSpringLogic.java", "ItemMybatisStore.java", "ItemController.java"} <= names
    assert (out / layout.mapper_dir / "order-mapper.xml").is_file()
    assert (out / layout.static_dir / "shop/order-list.jsp").is_file()
    assert (out / layout.static_dir / "css/shop.css").is_file()
    # the external call is handed over, not dropped in silence
    assert any(q.component == "order-service::notify" and q.reason.startswith("[extcall]") for q in queue)


def test_every_statement_a_generated_store_calls_exists_in_a_generated_mapper(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    layout, *_ = _convert(src, out)
    known = _statement_keys(out, layout)
    called: set[str] = set()
    for name, text in _java(out).items():
        if name.endswith("MybatisStore.java"):
            called |= set(re.findall(r'sqlSession\.\w+\("([^"]+)"', text))
    assert called, "the generated stores call no statement at all"
    assert called <= known, f"stores call statements no mapper has: {sorted(called - known)}"
    assert "com.example.d1.shopProject.store.OrderStore.getList" in called


def test_the_address_a_rewritten_script_calls_is_served_by_the_router_controller(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    layout, *_ = _convert(src, out)
    js = (out / layout.static_dir / "js/order.js").read_text(encoding="utf-8")
    assert "commonAjax.json" not in js
    assert '"/api/order/" + tran + "?cust=7"' in js
    controller = _java(out)["OrderController.java"]
    assert '@RequestMapping("/api/order")' in controller
    for transition in ("find", "save", "remove", "notify"):
        assert f'"/{transition}"' in controller


def test_a_page_link_of_a_jsp_reaches_a_page_controller_that_names_the_copied_jsp(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    layout, _result = _convert(src, out)
    created = _page_urls(out)
    assert all(u.startswith("/SH1/SH1A10/") for u in created)
    # the form posts to the page as the JSP spells it; the controller serves that spelling and the kebab form
    assert created == ["/SH1/SH1A10/item-list.mvc", "/SH1/SH1A10/order-detail.mvc",
                                     "/SH1/SH1A10/order-list.mvc", "/SH1/SH1A10/orderDetail.mvc"]
    detail = _java(out)["OrderDetailPageController.java"]
    assert '"/SH1/SH1A10/orderDetail.mvc"' in detail and 'return "shop/orderDetail";' in detail
    java = _java(out)
    page = java["OrderListPageController.java"]
    assert '"/SH1/SH1A10/order-list.mvc"' in page and 'return "shop/order-list";' in page
    assert (out / layout.static_dir / "shop/order-list.jsp").is_file()


def test_the_same_project_converts_to_the_same_bytes(tmp_path):
    src = tmp_path / "src"
    _project(src)
    trees = []
    for name in ("a", "b"):
        out = tmp_path / name
        _convert(src, out)
        trees.append({p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()})
    assert trees[0] == trees[1]


def test_a_copied_jsp_still_finds_what_it_includes_and_calls(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    layout, result = _convert(src, out)
    queue = result.queue
    jsp = (out / layout.static_dir / "shop/order-list.jsp").read_text(encoding="utf-8")
    # the include names the place the header was copied to, and that file exists
    assert '<%@ include file="/WEB-INF/static/shopProject/common/header.jsp" %>' in jsp
    assert (out / layout.static_dir / "common/header.jsp").is_file()
    # the inline call is rewritten like the one in the script file
    assert "commonAjax.json" not in jsp and '"/api/order/" + tran' in jsp
    # a file that was not copied is handed over, not left to fail at run time without a word
    assert any(q.component == "jsp_path::shop/order-list.jsp" and "[JSP_PATH]" in q.reason
               and "missing.jspf" in q.reason for q in queue)


def test_a_rewritten_call_to_a_service_nobody_serves_is_handed_over(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "WebContent/js/stock.js").write_text(
        'function s(t) { return "commonAjax.json?ServiceName=stock-service&" + t + "=1"; }\n', encoding="utf-8")
    layout, result = _convert(src, out)
    queue = result.queue
    assert result.errors == []
    # the script is rewritten as the table says, and the address has no controller: said, not left to a 404
    assert '"/api/stock/" + t' in (out / layout.static_dir / "js/stock.js").read_text(encoding="utf-8")
    assert any(q.component == "js_ajax::js/stock.js" and q.reason.startswith("[AJAX_NO_CONTROLLER]") for q in queue)
    # the order service has a router: its calls are served and not reported
    assert not any(q.component == "js_ajax::js/order.js" for q in queue)


def test_an_endpoint_the_front_end_does_not_call_is_removed_and_can_be_traced(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "WebContent/js/item.js").write_text("function l() { return api.get('/api/item'); }\n", encoding="utf-8")
    _layout, result = _convert(src, out)
    assert result.errors == []
    controller = _java(out)["ItemController.java"]
    # the list is called and served; the count is not called: no handler, and one line that says which address went
    assert '@GetMapping' in controller and "countAll" not in controller
    assert result.unexposed == ["GET /api/item/count-all  (ItemService.countAll)"]
