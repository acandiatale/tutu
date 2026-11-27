"""Service XML parsing, the DB classifier of FW-CONVERT 2.3 and the service decomposition (hand-over items)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.fwconvert.model import Activity, MapperFile, MapperIndex, Statement
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.servicexml import (
    EXCERPT_CHARS,
    ServiceResult,
    activity_kind,
    classify_activity,
    decompose_services,
    parse_service_xml,
    parse_services,
)

OVERLAY = {
    "activities": {
        "kinds": {"ShopUpsert": "upsert", "ShopDelete": "delete", "ShopPageSearch": "pagesearch"},
        "extra_db_prefixes": ["Shop"],
        "user_suffixes": ["SessionActivity"],
        "external_calls": {"extcall": ["remotecall"]},
    },
}


@pytest.fixture(scope="module")
def rules():
    return load_rules("Example", OVERLAY)


def _mappers() -> MapperIndex:
    return MapperIndex([MapperFile("order", "Order", "com.example.store.OrderStore", "m/order-mapper.xml",
                                   keys=["order"], statements=[Statement("getList", "order.getList")])])


def _act(cls: str, kind: str = "other", **props: str) -> Activity:
    return Activity(name="a1", class_name=cls, kind=kind, properties=dict(props))


A6_XML = """<?xml version="1.0" encoding="UTF-8"?>
<service name="shop-service" xmlns="http://example.com/schema/service">
  <activity name="router" class="com.example.fw.ExampleDefaultRouter">
    <transition name="list" value="getList"/>
    <transition name="save" value="saveRows"/>
    <transition name="remove" value="removeRows"/>
    <transition name="login" value="session"/>
  </activity>
  <activity name="getList" class="com.example.fw.ExampleMyBatisSearch">
    <property name="sql-key" value="shop.getList"/>
    <property name="result-key" value="rows"/>
  </activity>
  <activity name="saveRows" class="com.example.biz.ShopUpsert">
    <property name="sql-key" value="shop.upsertItem"/>
  </activity>
  <activity name="removeRows" class="com.example.biz.ShopDelete">
    <property name="sql-key" value="shop.deleteItem"/>
  </activity>
  <activity name="session" class="com.example.biz.LoginSessionActivity"/>
</service>
"""


# ---- parse_service_xml ------------------------------------------------------------


def test_parse_activity_dialect_with_namespace_router_and_kinds(rules):
    svc = parse_service_xml(A6_XML, "x/shop-service.xml", rules)
    assert svc is not None and svc.dialect == "activity"
    assert (svc.name, svc.resource, svc.x_name, svc.source) == ("shop-service", "shop", "Shop", "x/shop-service.xml")
    kinds = {a.name: a.kind for a in svc.activities}
    assert kinds == {"router": "router", "getList": "search", "saveRows": "upsert", "removeRows": "delete",
                     "session": "user"}
    assert svc.has_router
    assert svc.transitions == {"list": "getList", "save": "saveRows", "remove": "removeRows", "login": "session"}
    get_list = svc.activities[1]
    assert get_list.properties == {"sql-key": "shop.getList", "result-key": "rows"}
    assert svc.verdicts == {}


def test_parse_strips_service_suffix_ignoring_case(rules):
    svc = parse_service_xml('<service name="Order-SERVICE"><activity name="a" class="X"/></service>', "f.xml", rules)
    assert svc.resource == "Order" and svc.x_name == "Order"


def test_parse_falls_back_to_the_file_name(rules):
    svc = parse_service_xml('<service><activity name="a" class="X"/></service>', "p/q/order-item-service.xml", rules)
    assert svc.name == "order-item" and svc.x_name == "OrderItem"


def test_parse_without_router(rules):
    svc = parse_service_xml('<service name="a-service"><activity name="x" class="ExampleJdbcUpdate"/></service>',
                            "a-service.xml", rules)
    assert not svc.has_router and svc.transitions == {} and svc.activities[0].kind == "update"


def test_parse_bean_dialect(rules):
    text = """<services><service id="orderService" class="com.example.OrderServiceImpl">
      <property name="dao" ref="orderDao"/><transaction/></service></services>"""
    svc = parse_service_xml(text, "order-service.xml", rules)
    assert svc is not None and svc.dialect == "bean"
    assert svc.activities == [] and svc.resource == "" and svc.x_name == "" and not svc.has_router


@pytest.mark.parametrize("text", ["", "plain text, no xml {{", "<beans><bean id='x'/></beans>",
                                  "<root><other/></root>", "\x00\x01\x02"])
def test_parse_returns_none_for_no_service(rules, text):
    assert parse_service_xml(text, "f.xml", rules) is None


def test_parse_reads_a_broken_file_tolerantly(rules):
    svc = parse_service_xml('<service name="b-service"><activity name="x" class="ExampleMyBatisSearch">'
                            '<property name="sql-key" value="b.x"/>', "b.xml", rules)
    assert svc is not None and svc.activities[0].properties["sql-key"] == "b.x"


def test_parse_names_nameless_activities_and_drops_duplicates(rules):
    svc = parse_service_xml('<service name="s"><activity class="A"/><activity name="x" class="B"/>'
                            '<activity name="x" class="C"/></service>', "s.xml", rules)
    assert [(a.name, a.class_name) for a in svc.activities] == [("activity1", "A"), ("x", "B")]


@pytest.mark.parametrize("cls, kind", [
    ("com.example.ExampleMyBatisSearch", "search"), ("examplejdbcsearch", "search"),
    ("a.ExampleMyBatisUpdate", "update"), ("a.ExampleJdbcUpdate", "update"),
    ("a.MyShopUpsert", "upsert"), ("a.ShopDelete", "delete"), ("a.ShopPageSearch", "pagesearch"),
    ("a.ExampleDefaultRouter", "router"), ("a.LoginSessionActivity", "user"), ("a.OrderActivity", "other"),
    ("", "other"),
])
def test_activity_kind(rules, cls, kind):
    assert activity_kind(cls, rules) == kind


# ---- classify_activity: every row of 2.3 -------------------------------------------


def test_step0_router(rules):
    v = classify_activity(_act("a.ExampleDefaultRouter", "router"), _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit) == (False, "not_db", "router")


def test_step0_router_substring_weakness(rules):
    # a package that merely contains the word wins over a sql-key (known weakness, kept)
    act = _act("com.example.routerutil.OrderSearch", "other", **{"sql-key": "order.getList"})
    v = classify_activity(act, _mappers(), None, rules)
    assert v.pass_hit == "router" and not v.db_backed


def test_step0_user(rules):
    v = classify_activity(_act("a.LoginSessionActivity", "user"), _mappers(), "class X { SqlSession s; }", rules)
    assert (v.db_backed, v.confidence, v.pass_hit) == (False, "not_db", "user")


def test_step0_external_call_a7(rules):
    act = _act("com.example.RemoteCallActivity", **{"sql-key": "x.y"})
    v = classify_activity(act, _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit) == (False, "external_call", "extcall")
    assert "remotecall" in v.evidence


def test_step0_external_word_matches_by_substring_in_package(rules):
    v = classify_activity(_act("com.remotecalls.Order"), _mappers(), None, rules)
    assert v.confidence == "external_call"


def test_step1a_builtin(rules):
    v = classify_activity(_act("a.ExampleMyBatisSearch", "search", **{"sql-key": "order.getList"}),
                          _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (True, "certain", "1a", "order.getList")


def test_step1a_needs_search_or_update_kind(rules):
    # starts with the framework prefix but is no search / update: falls to 1c
    v = classify_activity(_act("a.ExampleCustomThing", "other", **{"sql-key": "k.v"}), _mappers(), None, rules)
    assert v.pass_hit == "1c"


def test_step1b_company_prefix(rules):
    v = classify_activity(_act("a.ShopUpsert", "upsert", **{"sql-key": "shop.save"}), _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (True, "certain", "1b", "shop.save")
    v = classify_activity(_act("a.ShopAnything", "other"), _mappers(), None, rules)
    assert v.pass_hit == "1b" and v.statement == ""


def test_step1c_sql_key(rules):
    v = classify_activity(_act("a.OrderActivity", **{"sql-key": "order.getList"}), _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (True, "strong", "1c", "order.getList")


@pytest.mark.parametrize("call", ['find', 'findOne', 'findList', 'findOneItem', 'select', 'selectByKey', 'update',
                                  'insert', 'delete', 'execute', 'count', 'batchUpdate', 'exec', 'executeProc'])
def test_step2a_every_method_name_form(rules, call):
    src = f'class A {{ void run() {{ dao.{call}("order.getList", params); }} }}'
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (True, "strong", "2a", "order.getList")
    assert call in v.evidence


def test_step2a_id_needs_a_dot(rules):
    src = 'class A { void run() { dao.find("getList", p); } }'
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert v.pass_hit == "java_only"


def test_step2a_unknown_method_and_comment_do_not_count(rules):
    src = 'class A { void run() { log.info("order.getList"); // dao.find("order.x")\n /* dao.update("o.y") */ } }'
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert v.pass_hit == "java_only"


def test_step2a_same_id_twice_is_one(rules):
    src = 'dao.find("order.getList", a); dao.findList("order.getList", b);'
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert v.pass_hit == "2a"


def test_step2a_two_different_ids_is_ambiguous_a8(rules):
    src = 'class A { void run() { dao.find("order.getList", p); dao.update("item.save", p); } }'
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (False, "ambiguous", "3", "")
    assert "order.getList" in v.evidence and "item.save" in v.evidence


@pytest.mark.parametrize("marker", ["SqlSession", "ExampleDao", "ExampleMyBatis", "JdbcTemplate",
                                    "NamedParameterJdbcTemplate", "PreparedStatement", "getDao(", "executeQuery",
                                    "executeUpdate", "@Mapper"])
def test_step2b_every_marker(rules, marker):
    src = f"class A {{ /* none */ Object x = {marker}; }}"
    v = classify_activity(_act("a.OrderActivity"), _mappers(), src, rules)
    assert (v.db_backed, v.confidence, v.pass_hit, v.statement) == (True, "strong", "2b", "")
    assert marker in v.evidence


def test_java_only(rules):
    v = classify_activity(_act("a.OrderActivity"), _mappers(), "class A { int add(int a) { return a + 1; } }", rules)
    assert (v.db_backed, v.confidence, v.pass_hit) == (False, "not_db", "java_only")


def test_step3_source_not_found(rules):
    v = classify_activity(_act("a.OrderActivity"), _mappers(), None, rules)
    assert (v.db_backed, v.confidence, v.pass_hit) == (False, "ambiguous", "3")
    assert "not found" in v.evidence


def test_first_match_wins_over_source(rules):
    # a sql-key (1c) is decided before the source is read
    src = 'dao.find("a.b", p); dao.find("c.d", p);'
    v = classify_activity(_act("a.OrderActivity", **{"sql-key": "order.getList"}), _mappers(), src, rules)
    assert v.pass_hit == "1c"


# ---- decompose_services ------------------------------------------------------------


def _write(root: Path, rel: str, data: bytes | str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return rel


MIXED_XML = """<service name="mix-service">
  <activity name="remote" class="com.example.biz.RemoteCallActivity"/>
  <activity name="two" class="com.example.biz.TwoMapperActivity"/>
  <activity name="api" class="com.example.biz.ApiActivity"/>
  <activity name="plain" class="com.example.biz.PlainActivity"/>
  <activity name="missing" class="com.example.biz.MissingActivity"/>
  <activity name="byName" class="com.other.pkg.ByNameActivity"/>
  <activity name="direct" class="com.example.biz.DirectActivity"/>
</service>
"""


def test_decompose_writes_verdicts_and_handover_items(tmp_path, rules):
    files = [
        _write(tmp_path, "src/com/example/biz/RemoteCallActivity.java", "class RemoteCallActivity {}"),
        _write(tmp_path, "src/com/example/biz/TwoMapperActivity.java",
               'class T { void r() { dao.find("order.getList", p); dao.find("item.getList", p); } }'),
        _write(tmp_path, "src/com/example/biz/ApiActivity.java", "class A { SqlSession s; }"),
        _write(tmp_path, "src/com/example/biz/PlainActivity.java", "class P { int x; }" + "y" * 3000),
        _write(tmp_path, "lib/ByNameActivity.java", 'class B { void r() { dao.find("order.getList", p); } }'),
        _write(tmp_path, "src/com/example/biz/DirectActivity.java",
               'class D { void r() { dao.find("order.getList", p); } }'),
    ]
    svc_rel = _write(tmp_path, "cfg/mix-service.xml", MIXED_XML)
    res = decompose_services(tmp_path, [svc_rel], files, _mappers(), rules)
    assert isinstance(res, ServiceResult) and res.errors == []
    svc = res.services[0]
    hits = {k: (v.pass_hit, v.confidence) for k, v in svc.verdicts.items()}
    assert hits == {"remote": ("extcall", "external_call"), "two": ("3", "ambiguous"), "api": ("2b", "strong"),
                    "plain": ("java_only", "not_db"), "missing": ("3", "ambiguous"), "byName": ("2a", "strong"),
                    "direct": ("2a", "strong")}
    queue = {q.component: q for q in res.queue}
    assert set(queue) == {"mix-service::remote", "mix-service::two", "mix-service::api", "mix-service::plain",
                          "mix-service::missing"}
    assert queue["mix-service::remote"].reason.startswith("[extcall] ")
    assert queue["mix-service::two"].reason.startswith("[3] ")
    assert queue["mix-service::api"].reason.startswith("[2b] ")
    assert queue["mix-service::plain"].reason.startswith("[java_only] ")
    assert queue["mix-service::missing"].reason.startswith("[3] ") and "not found" in queue["mix-service::missing"].reason
    plain = queue["mix-service::plain"]
    assert plain.source_path == "src/com/example/biz/PlainActivity.java"
    assert len(plain.excerpt) == EXCERPT_CHARS and plain.excerpt.startswith("class P")
    assert queue["mix-service::missing"].source_path == "" and queue["mix-service::missing"].excerpt == ""
    assert svc.verdicts["byName"].statement == "order.getList"


def test_decompose_simple_name_must_be_unique(tmp_path, rules):
    files = [_write(tmp_path, "a/Dup.java", 'dao.find("o.x", p);'), _write(tmp_path, "b/Dup.java", "class Dup {}")]
    rel = _write(tmp_path, "s-service.xml", '<service name="s-service"><activity name="d" class="z.Dup"/></service>')
    res = decompose_services(tmp_path, [rel], files, _mappers(), rules)
    assert res.services[0].verdicts["d"].pass_hit == "3"


def test_decompose_a6_db_activities_need_no_source(tmp_path, rules):
    rel = _write(tmp_path, "shop-service.xml", A6_XML)
    res = decompose_services(tmp_path, [rel], [], _mappers(), rules)
    v = res.services[0].verdicts
    assert {k: x.pass_hit for k, x in v.items()} == {"router": "router", "getList": "1a", "saveRows": "1b",
                                                     "removeRows": "1b", "session": "user"}
    assert res.queue == [] and res.errors == []


def test_decompose_bean_dialect_is_a_handover_item(tmp_path, rules):
    text = '<service id="orderService" class="com.example.OrderServiceImpl"><property name="d" ref="dao"/></service>'
    rel = _write(tmp_path, "conf/order-service.xml", text)
    res = decompose_services(tmp_path, [rel], [], _mappers(), rules)
    assert res.services[0].dialect == "bean"
    assert len(res.queue) == 1
    q = res.queue[0]
    assert q.component == "conf/order-service.xml" and q.reason.startswith("[BEAN_DIALECT]")
    assert q.source_path == rel and q.excerpt == text


def test_decompose_broken_and_missing_files_are_errors(tmp_path, rules):
    bad = _write(tmp_path, "bad-service.xml", b"\x00\x01 not xml at all")
    res = decompose_services(tmp_path, [bad, "gone-service.xml"], [], _mappers(), rules)
    assert res.services == [] and res.queue == []
    assert len(res.errors) == 2
    assert any(e.startswith("bad-service.xml:") for e in res.errors)
    assert any(e.startswith("gone-service.xml:") for e in res.errors)


def test_decompose_tolerant_read_is_noted(tmp_path, rules):
    rel = _write(tmp_path, "t-service.xml", '<service name="t-service"><activity name="x" class="ExampleJdbcSearch">')
    res = decompose_services(tmp_path, [rel], [], _mappers(), rules)
    assert len(res.services) == 1 and any("tolerantly" in e for e in res.errors)


def test_decompose_two_files_one_service_name(tmp_path, rules):
    a = _write(tmp_path, "a/order-service.xml", '<service name="order-service"><activity name="x" class="A"/></service>')
    b = _write(tmp_path, "b/order-service.xml", '<service name="Order-Service"><activity name="y" class="B"/></service>')
    res = decompose_services(tmp_path, [b, a], [], _mappers(), rules)
    assert [s.source for s in res.services] == ["a/order-service.xml"]
    assert len(res.errors) == 1 and res.errors[0].startswith("b/order-service.xml:") and "a/order-service.xml" in res.errors[0]


def test_decompose_reads_ms949(tmp_path, rules):
    text = ('<?xml version="1.0" encoding="EUC-KR"?>\n<service name="kor-service">'
            '<activity name="getList" class="ExampleMyBatisSearch"><property name="desc" value="주문 목록"/>'
            '<property name="sql-key" value="kor.getList"/></activity></service>')
    rel = _write(tmp_path, "kor-service.xml", text.encode("cp949"))
    java = _write(tmp_path, "src/K.java", '// 한글 주석\nclass K { void r() { dao.find("kor.x", p); } }'.encode("cp949"))
    rel2 = _write(tmp_path, "k2-service.xml", '<service name="k2-service"><activity name="k" class="K"/></service>')
    res = decompose_services(tmp_path, [rel, rel2], [java], _mappers(), rules)
    assert res.errors == []
    by_name = {s.name: s for s in res.services}
    kor = by_name["kor-service"]
    assert kor.activities[0].properties["desc"] == "주문 목록"
    assert kor.verdicts["getList"].pass_hit == "1a"
    assert by_name["k2-service"].verdicts["k"].statement == "kor.x"


# ---- several services in one file, entities -----------------------------------------


TWO_SERVICES = """<services>
  <service name="a-service"><activity name="x" class="p.ExampleMyBatisSearch"><property name="sql-key" value="a.x"/></activity></service>
  <service id="beanOne" class="com.example.BeanImpl"><property name="d" ref="dao"/></service>
  <service name="b-service"><activity name="y" class="p.ExampleMyBatisSearch"><property name="sql-key" value="b.y"/></activity></service>
</services>"""


def test_parse_services_returns_every_service(rules):
    svcs = parse_services(TWO_SERVICES, "all-service.xml", rules)
    assert [(s.name, s.dialect) for s in svcs] == [("a-service", "activity"), ("beanOne", "bean"), ("b-service", "activity")]
    assert [a.name for a in svcs[2].activities] == ["y"]
    assert parse_service_xml(TWO_SERVICES, "all-service.xml", rules).name == "a-service"
    assert parse_services("<root/>", "f.xml", rules) == []


def test_decompose_reads_every_service_of_a_file(tmp_path, rules):
    rel = _write(tmp_path, "all-service.xml", TWO_SERVICES)
    res = decompose_services(tmp_path, [rel], [], _mappers(), rules)
    assert [s.name for s in res.services] == ["a-service", "beanOne", "b-service"] and res.errors == []
    assert [(q.component, q.reason.split()[0]) for q in res.queue] == [("all-service.xml", "[BEAN_DIALECT]")]
    assert "beanOne" in res.queue[0].reason


ENTITY_XML = """<?xml version="1.0"?>
<!DOCTYPE service [<!ENTITY ns "order">]>
<service name="e-service">
  <activity name="a" class="p.ExampleMyBatisSearch"><property name="sql-key">&ns;.getList</property></activity>
  <activity name="b" class="p.ExampleMyBatisSearch"><property name="sql-key" value="&ns;.save"/></activity>
</service>"""


def test_entity_in_element_text_is_kept_as_text(rules):
    svc = parse_service_xml(ENTITY_XML, "e-service.xml", rules)
    assert svc.activities[0].properties["sql-key"] == "&ns;.getList"
    assert svc.activities[1].properties["sql-key"] == "order.save"


def test_decompose_reports_an_unresolved_entity(tmp_path, rules):
    rel = _write(tmp_path, "e-service.xml", ENTITY_XML)
    res = decompose_services(tmp_path, [rel], [], _mappers(), rules)
    assert len(res.services) == 1
    assert len(res.errors) == 1 and res.errors[0].startswith("e-service.xml:") and "&ns;" in res.errors[0]
