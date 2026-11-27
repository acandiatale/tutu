"""Resources from services and mappers: methods, names, SqlSession calls, endpoints and the exposure filter."""
from __future__ import annotations

import pytest

from sbmig.fwconvert.model import (
    Activity,
    DbVerdict,
    Endpoint,
    FrontendApiMap,
    MapperFile,
    MapperIndex,
    RestCall,
    ServiceDef,
    Statement,
)
from sbmig.fwconvert.resources import (
    ajax_targets_without_controller,
    build_resources,
    endpoint_for,
    session_call,
    unexposed_endpoints,
)
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.servicexml import classify_activity, parse_service_xml

OVERLAY = {
    "activities": {
        "kinds": {"ShopUpsert": "upsert", "ShopDelete": "delete"},
        "extra_db_prefixes": ["Shop"],
        "user_suffixes": ["SessionActivity"],
        "external_calls": {"extcall": ["remotecall"]},
    },
}


@pytest.fixture(scope="module")
def rules():
    return load_rules("Example", OVERLAY)


def _mapper(resource: str, x: str, *stmts: tuple[str, str], keys: list[str] | None = None) -> MapperFile:
    return MapperFile(resource, x, f"com.example.store.{x}Store", f"m/{resource}-mapper.xml",
                      keys=keys if keys is not None else [resource],
                      statements=[Statement(i, f"{resource}.{i}", verb=v) for i, v in stmts])


def _service(text: str, rules, mappers: MapperIndex, java: dict[str, str] | None = None) -> ServiceDef:
    svc = parse_service_xml(text, "s.xml", rules)
    for act in svc.activities:
        svc.verdicts[act.name] = classify_activity(act, mappers, (java or {}).get(act.name), rules)
    return svc


def _by_x(resources):
    return {r.x_name: r for r in resources}


A6_XML = """<service name="shop-service">
  <activity name="router" class="com.example.fw.ExampleDefaultRouter">
    <transition name="list" value="getList"/>
    <transition name="save" value="saveRows"/>
    <transition name="remove" value="removeRows"/>
    <transition name="login" value="session"/>
    <transition name="ghost" value="nothingHere"/>
  </activity>
  <activity name="getList" class="com.example.fw.ExampleMyBatisSearch"><property name="sql-key" value="shop.getList"/></activity>
  <activity name="saveRows" class="com.example.biz.ShopUpsert"><property name="sql-key" value="shop.upsertItem"/></activity>
  <activity name="removeRows" class="com.example.biz.ShopDelete"><property name="sql-key" value="shop.deleteItem"/></activity>
  <activity name="session" class="com.example.biz.LoginSessionActivity"/>
  <activity name="unrouted" class="com.example.fw.ExampleMyBatisSearch"><property name="sql-key" value="shop.other"/></activity>
</service>"""


def test_a6_router_service_with_upsert_and_delete(rules):
    idx = MapperIndex([_mapper("shop", "Shop", ("getList", "select"), ("upsertItem", "insert"),
                               ("deleteItem", "delete"), ("countAll", "select"))])
    res = _by_x(build_resources([_service(A6_XML, rules, idx)], idx, None, rules))["Shop"]
    assert res.has_router and not res.stub and res.resource == "shop"
    m = {x.name: x for x in res.methods}
    assert [x.name for x in res.methods] == ["getList", "upsertItem", "deleteItem", "countAll"]
    assert (m["getList"].kind, m["getList"].returns, m["getList"].session_call, m["getList"].read_only) == \
        ("search", "list", "selectList", True)
    assert (m["upsertItem"].kind, m["upsertItem"].returns, m["upsertItem"].session_call) == ("upsert", "int", "insert")
    assert (m["deleteItem"].kind, m["deleteItem"].returns, m["deleteItem"].session_call) == ("delete", "int", "delete")
    assert all(x.db_backed for x in res.methods)
    assert m["upsertItem"].pass_hit == "1b" and m["upsertItem"].activity == "saveRows" and not m["upsertItem"].read_only
    assert m["countAll"].origin == "mapper" and m["countAll"].returns == "int" and m["countAll"].session_call == "selectOne"
    assert all(x.namespace == "com.example.store.ShopStore" for x in res.methods)
    assert res.transitions == {"list": "getList", "save": "upsertItem", "remove": "deleteItem", "login": "",
                               "ghost": ""}
    assert all(x.endpoint is None for x in res.methods)          # a router resource has no endpoints


def test_a7_external_call_is_not_db(rules):
    text = """<service name="pay-service">
      <activity name="charge" class="com.example.RemoteCallActivity"><property name="sql-key" value="pay.saveCharge"/></activity>
    </service>"""
    idx = MapperIndex([_mapper("pay", "Pay", ("saveCharge", "insert"))])
    res = _by_x(build_resources([_service(text, rules, idx)], idx, None, rules))["Pay"]
    charge = res.methods[0]
    assert (charge.name, charge.db_backed, charge.pass_hit, charge.namespace, charge.session_call) == \
        ("saveCharge", False, "extcall", "", "")
    assert charge.returns == "int"
    # the statement itself is still a method of the mapper, under a new name
    assert [(x.name, x.origin, x.statement_id) for x in res.methods[1:]] == [("saveCharge2", "mapper", "saveCharge")]


def test_a10_router_with_nothing_to_call_is_a_stub(rules):
    text = """<service name="empty-service">
      <activity name="router" class="ExampleDefaultRouter"><transition name="go" value="work"/></activity>
      <activity name="work" class="com.example.WorkActivity"/>
    </service>"""
    idx = MapperIndex([])
    res = _by_x(build_resources([_service(text, rules, idx)], idx, None, rules))["Empty"]
    assert res.stub and res.has_router and res.mapper is None
    assert [(x.name, x.db_backed, x.pass_hit) for x in res.methods] == [("work", False, "3")]
    assert res.transitions == {"go": "work"}


def test_without_router_every_activity_but_router_and_user(rules):
    text = """<service name="o-service">
      <activity name="a" class="ExampleJdbcSearch"><property name="sql-key" value="o.a"/></activity>
      <activity name="s" class="x.LoginSessionActivity"/>
    </service>"""
    idx = MapperIndex([])
    res = build_resources([_service(text, rules, idx)], idx, None, rules)[0]
    assert [x.name for x in res.methods] == ["a"] and not res.has_router and not res.stub and res.transitions == {}


def test_method_name_three_fallbacks_and_dedupe(rules):
    text = """<service name="order-service">
      <activity name="byOriginal" class="x.OrderActivity"><property name="sql-key" value="ord.legacyList"/></activity>
      <activity name="byKey" class="x.OrderActivity"><property name="sql-key" value="nowhere.fetchThings"/></activity>
      <activity name="DoWork" class="x.WorkActivity"/>
      <activity name="again" class="x.OrderActivity"><property name="sql-key" value="nowhere.fetchThings"/></activity>
      <activity name="third" class="x.OrderActivity"><property name="sql-key" value="other.fetchThings"/></activity>
    </service>"""
    order = MapperFile("order", "Order", "com.example.store.OrderStore", "m/order-mapper.xml", keys=["order"],
                       statements=[Statement("getAllRows", "ord.legacyList", verb="select")])
    idx = MapperIndex([order])
    res = _by_x(build_resources([_service(text, rules, idx)], idx, None, rules))["Order"]
    names = [x.name for x in res.methods]
    # "again" names the same sql-key: one method; "third" names another one: renamed
    assert names == ["getAllRows", "fetchThings", "doWork", "fetchThings2"]
    m = {x.name: x for x in res.methods}
    assert m["getAllRows"].statement_id == "getAllRows" and m["getAllRows"].activity == "byOriginal"
    assert m["fetchThings"].activity == "byKey" and m["fetchThings"].namespace == "" and m["fetchThings"].db_backed
    assert m["doWork"].pass_hit == "3" and not m["doWork"].db_backed
    assert m["fetchThings2"].activity == "third"


def test_returns_of_other_kind_follow_the_sql_key(rules):
    text = """<service name="r-service">
      <activity name="a" class="x.A"><property name="sql-key" value="r.saveThing"/></activity>
      <activity name="b" class="x.B"><property name="sql-key" value="r.getThing"/></activity>
      <activity name="c" class="x.C"><property name="sql-key" value="r.UPSERT_rows"/></activity>
    </service>"""
    idx = MapperIndex([])
    res = build_resources([_service(text, rules, idx)], idx, None, rules)[0]
    assert [x.returns for x in res.methods] == ["int", "list", "int"]


@pytest.mark.parametrize("key, returns", [("order.updateStatus", "int"), ("order.saveRows", "int"),
                                          ("order.selectLastUpdateDate", "list"), ("order.getSaved", "list"),
                                          ("order.resaveAll", "list"), ("insert", "int")])
def test_other_kind_mutation_is_the_first_word_of_the_id(rules, key, returns):
    svc = ServiceDef(name="o-service", resource="o", x_name="O",
                     activities=[Activity("a", "x.A", "other", {"sql-key": key})],
                     verdicts={"a": DbVerdict(True, "strong", "1c", key)})
    assert build_resources([svc], MapperIndex([]), None, rules)[0].methods[0].returns == returns


@pytest.mark.parametrize("sid, returns, call", [
    ("selectAccountList", "list", "selectList"), ("getDiscount", "list", "selectList"),
    ("selectCountry", "list", "selectList"), ("resizeImages", "list", "selectList"),
    ("countAll", "int", "selectOne"), ("getTotal", "int", "selectOne"), ("listSize", "int", "selectOne"),
    ("idExists", "int", "selectOne"),
])
def test_count_words_match_whole_words(rules, sid, returns, call):
    idx = MapperIndex([_mapper("m", "M", (sid, "select"))])
    m = build_resources([], idx, None, rules)[0].methods[0]
    assert (m.returns, m.session_call) == (returns, call)


def test_two_transitions_to_different_statements_with_one_id_get_two_methods(rules):
    ma = MapperFile("order", "Order", "ns.OrderStore", "a.xml", keys=["order"],
                    statements=[Statement("getList", "order.getList", verb="select")])
    mb = MapperFile("item", "Item", "ns.ItemStore", "b.xml", keys=["item"],
                    statements=[Statement("getList", "item.getList", verb="select")])
    acts = [Activity("orders", "x.ExampleMyBatisSearch", "search", {"sql-key": "order.getList"}),
            Activity("items", "x.ExampleMyBatisSearch", "search", {"sql-key": "item.getList"}),
            Activity("again", "x.ExampleMyBatisSearch", "search", {"sql-key": "order.getList"}),
            Activity("router", "x.ExampleDefaultRouter", "router",
                     transitions={"orders": "orders", "items": "items", "again": "again"})]
    svc = ServiceDef(name="shop-service", resource="shop", x_name="Shop", activities=acts, has_router=True,
                     transitions={"orders": "orders", "items": "items", "again": "again"},
                     verdicts={a: DbVerdict(True, "certain", "1a", k) for a, k in
                               (("orders", "order.getList"), ("items", "item.getList"), ("again", "order.getList"))})
    res = _by_x(build_resources([svc], MapperIndex([ma, mb]), None, rules))["Shop"]
    assert [(m.name, m.namespace) for m in res.methods] == [("getList", "ns.OrderStore"), ("getList2", "ns.ItemStore")]
    assert res.transitions == {"orders": "getList", "items": "getList2", "again": "getList"}


def test_every_mapper_of_one_x_gives_methods(rules):
    conv = MapperFile("order", "Order", "com.example.store.OrderStore", "m/order-mapper.xml", keys=["order"],
                      statements=[Statement("getList", "getList", verb="select")])
    copied = MapperFile("OrderMapper", "Order", "com.legacy.OrderMapper", "m/order-mapper-2.xml", origin="copied",
                        keys=["OrderMapper"], statements=[Statement("getDetail", "getDetail", verb="select"),
                                                          Statement("getList", "getList", verb="select")])
    [res] = build_resources([], MapperIndex([conv, copied]), None, rules)
    assert res.mapper is conv and res.mappers == [conv, copied]
    assert [(m.name, m.namespace, m.endpoint) for m in res.methods] == [
        ("getList", "com.example.store.OrderStore", Endpoint("GET", "")),
        ("getDetail", "com.legacy.OrderMapper", Endpoint("GET", "/get-detail")),
        ("getList2", "com.legacy.OrderMapper", Endpoint("GET", "/get-list")),
    ]


def test_x_names_differing_only_in_case_are_renamed(rules):
    a = _mapper("order_item", "OrderItem", ("getList", "select"))
    b = _mapper("orderitem", "Orderitem", ("getList", "select"))
    c = _mapper("ORDERITEM", "ORDERITEM", ("getList", "select"))
    res = build_resources([], MapperIndex([b, a, c]), None, rules)
    assert [(r.x_name, r.mapper.resource) for r in res] == [("ORDERITEM", "ORDERITEM"), ("OrderItem2", "order_item"),
                                                           ("Orderitem3", "orderitem")]
    assert res[1].methods[0].namespace == "com.example.store.OrderItemStore"
    assert res[1].resource == "order_item"


def test_statement_found_in_another_resources_mapper(rules):
    text = """<service name="cart-service">
      <activity name="items" class="ExampleMyBatisSearch"><property name="sql-key" value="item.getList"/></activity>
    </service>"""
    item = _mapper("item", "Item", ("getList", "select"))
    idx = MapperIndex([item])
    res = _by_x(build_resources([_service(text, rules, idx)], idx, None, rules))
    cart = res["Cart"]
    assert cart.mapper is None and cart.methods[0].namespace == "com.example.store.ItemStore"
    assert cart.methods[0].statement_id == "getList" and cart.methods[0].endpoint is None
    # the mapper's own resource still has its statement as a method with an endpoint
    assert [(x.name, x.origin, x.endpoint) for x in res["Item"].methods] == [("getList", "mapper", Endpoint("GET", ""))]


def test_mapper_name_collision_with_another_statement_is_renamed(rules):
    text = """<service name="order-service">
      <activity name="items" class="ExampleMyBatisSearch"><property name="sql-key" value="item.getList"/></activity>
    </service>"""
    idx = MapperIndex([_mapper("item", "Item", ("getList", "select")), _mapper("order", "Order", ("getList", "select"))])
    order = _by_x(build_resources([_service(text, rules, idx)], idx, None, rules))["Order"]
    assert [(x.name, x.namespace.rsplit(".", 1)[-1], x.origin) for x in order.methods] == \
        [("getList", "ItemStore", "service"), ("getList2", "OrderStore", "mapper")]
    # only the method on the own mapper's statement is exposed
    assert order.methods[0].endpoint is None and order.methods[1].endpoint == Endpoint("GET", "")


def test_service_methods_on_own_mapper_get_endpoints_too(rules):
    text = """<service name="order-service">
      <activity name="a" class="ExampleMyBatisSearch"><property name="sql-key" value="order.getList"/></activity>
    </service>"""
    idx = MapperIndex([_mapper("order", "Order", ("getList", "select"), ("save", "insert"))])
    order = build_resources([_service(text, rules, idx)], idx, None, rules)[0]
    assert [(x.name, x.origin, x.endpoint) for x in order.methods] == \
        [("getList", "service", Endpoint("GET", "")), ("save", "mapper", Endpoint("POST", ""))]


def test_mapper_only_resources_sorted_and_bean_services_ignored(rules):
    idx = MapperIndex([_mapper("zeta", "Zeta", ("getList", "select")), _mapper("alpha", "Alpha", ("insertX", "insert"),
                                                                             ("deleteY", "delete"), ("totalOf", "select"))])
    bean = ServiceDef(name="beta-service", dialect="bean")
    res = build_resources([bean], idx, None, rules)
    assert [r.x_name for r in res] == ["Alpha", "Zeta"]
    alpha = res[0]
    assert alpha.service is None and not alpha.has_router and not alpha.stub
    assert [(x.name, x.kind, x.returns, x.session_call, x.read_only, x.db_backed) for x in alpha.methods] == [
        ("insertX", "statement", "int", "insert", False, True),
        ("deleteY", "statement", "int", "delete", False, True),
        ("totalOf", "statement", "int", "selectOne", True, True),
    ]


def test_missing_verdict_is_computed(rules):
    svc = ServiceDef(name="v-service", resource="v", x_name="V",
                     activities=[Activity("a", "ExampleJdbcSearch", "search", {"sql-key": "v.getList"})])
    idx = MapperIndex([_mapper("v", "V", ("getList", "select"))])
    res = build_resources([svc], idx, None, rules)[0]
    assert res.methods[0].pass_hit == "1a" and res.methods[0].statement_id == "getList"


def test_two_services_with_one_x_name_first_wins(rules):
    a = ServiceDef(name="order-service", resource="order", x_name="Order",
                   activities=[Activity("one", "ExampleJdbcSearch", "search")],
                   verdicts={"one": DbVerdict(True, "certain", "1a")})
    b = ServiceDef(name="Order_service", resource="Order", x_name="Order",
                   activities=[Activity("two", "ExampleJdbcSearch", "search")])
    res = build_resources([a, b], MapperIndex([]), None, rules)
    assert len(res) == 1 and res[0].service is a and [x.name for x in res[0].methods] == ["one"]


# ---- SqlSession call table ----------------------------------------------------------


@pytest.mark.parametrize("returns, verb, name, call", [
    ("list", "insert", "anything", "selectList"),
    ("int", "select", "countRows", "selectOne"),
    ("int", "select", "getTotal", "selectOne"),
    ("int", "select", "listSize", "selectOne"),
    ("int", "select", "idExists", "selectOne"),
    ("int", "insert", "x", "insert"),
    ("int", "update", "x", "update"),
    ("int", "delete", "x", "delete"),
    ("int", "merge", "x", "insert"),
    ("int", "", "removeRow", "delete"),
    ("int", "call", "deleteRow", "delete"),
    ("int", "", "saveRow", "insert"),
    ("int", "", "createRow", "insert"),
    ("int", "", "addRow", "insert"),
    ("int", "", "modifyRow", "update"),
    ("int", "", "mergeRow", "update"),
    ("int", "", "doSomething", "update"),
    ("int", "select", "getValue", "update"),
    ("map", "select", "x", "selectOne"),
])
def test_session_call_table(rules, returns, verb, name, call):
    assert session_call(returns, verb, name, rules) == call


# ---- endpoint table -----------------------------------------------------------------


@pytest.mark.parametrize("sid, verb, endpoint", [
    ("lovItems", "select", None),
    ("LOVCodes", "select", None),
    ("getSummary", "select", Endpoint("GET", "/summary")),
    ("orderSummary", "select", Endpoint("GET", "/summary")),
    ("getList", "select", Endpoint("GET", "")),
    ("find", "select", Endpoint("GET", "")),
    ("list", "select", Endpoint("GET", "")),
    ("selectAll", "select", Endpoint("GET", "")),
    ("SELECTLIST", "select", Endpoint("GET", "")),
    ("getListPaged", "select", Endpoint("GET", "")),
    ("getDetail", "select", Endpoint("GET", "/get-detail")),
    ("upsert", "insert", Endpoint("POST", "")),
    ("save", "update", Endpoint("POST", "")),
    ("insert", "insert", Endpoint("POST", "")),
    ("update", "update", Endpoint("POST", "")),
    ("merge", "merge", Endpoint("POST", "")),
    ("saveDetail", "insert", Endpoint("POST", "/save-detail")),
    ("lovSave", "insert", Endpoint("POST", "/lov-save")),
    ("delete", "delete", Endpoint("DELETE", "/{id}")),
    ("deleteAll", "delete", Endpoint("DELETE", "/delete-all")),
])
def test_endpoint_table(rules, sid, verb, endpoint):
    assert endpoint_for(Statement(sid, sid, verb=verb), rules) == endpoint


def test_same_verb_and_path_collision(rules):
    idx = MapperIndex([_mapper("order", "Order", ("getList", "select"), ("selectAll", "select"),
                               ("getSummary", "select"), ("summary", "select"))])
    order = build_resources([], idx, None, rules)[0]
    assert [x.endpoint for x in order.methods] == [Endpoint("GET", ""), Endpoint("GET", "/select-all"),
                                                   Endpoint("GET", "/summary"), Endpoint("GET", "/summary-2")]


# ---- exposure filter ----------------------------------------------------------------


def _order_idx() -> MapperIndex:
    return MapperIndex([_mapper("order-item", "OrderItem", ("getList", "select"), ("save", "insert"),
                                ("delete", "delete"), ("getDetail", "select"))])


def test_exposure_filter_keeps_only_called_endpoints(rules):
    api = FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/order-item?page=1"), RestCall("del", "/api/orderItem/{id}"),
                                         RestCall("GET", "/api/other/x")])
    res = build_resources([], _order_idx(), api, rules)[0]
    assert [(x.name, x.endpoint) for x in res.methods] == [("getList", Endpoint("GET", "")), ("save", None),
                                                          ("delete", Endpoint("DELETE", "/{id}")), ("getDetail", None)]


def test_exposure_filter_parameter_matches_concrete_segment(rules):
    api = FrontendApiMap(rest_endpoints=[RestCall("DELETE", "/api/order-item/42/"), RestCall("", "/api/order-item/get-detail")])
    res = build_resources([], _order_idx(), api, rules)[0]
    assert [x.name for x in res.methods if x.endpoint] == ["delete", "getDetail"]


def test_exposure_filter_keeps_all_when_no_call_matches(rules):
    api = FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/order-item/getItems")])
    res = build_resources([], _order_idx(), api, rules)[0]
    assert all(x.endpoint is not None for x in res.methods)


def test_exposure_filter_without_calls_for_this_resource_keeps_all(rules):
    api = FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/other"), RestCall("GET", "/static/order-item")])
    res = build_resources([], _order_idx(), api, rules)[0]
    assert all(x.endpoint is not None for x in res.methods)
    assert all(x.endpoint is not None for x in build_resources([], _order_idx(), FrontendApiMap(), rules)[0].methods)


# ---- REST base ----------------------------------------------------------------------


def test_rest_base_is_unique_ignoring_case(rules):
    a = _mapper("orderitem", "Orderitem", ("getList", "select"))
    b = _mapper("ORDERITEM", "ORDERITEM", ("getList", "select"))
    c = _mapper("order-item", "OrderItem", ("getList", "select"))
    d = _mapper("shop", "Shop", ("getList", "select"))
    res = build_resources([], MapperIndex([a, b, c, d]), None, rules)
    assert [(r.x_name, r.rest_base) for r in res] == [("ORDERITEM", "orderitem"), ("OrderItem2", "order-item"),
                                                      ("Orderitem3", "orderitem-2"), ("Shop", "shop")]


def test_rest_base_of_a_service_resource(rules):
    svc = ServiceDef(name="orderMgmt-service", resource="orderMgmt", x_name="OrderMgmt")
    assert build_resources([svc], MapperIndex([]), None, rules)[0].rest_base == "order-mgmt"


def test_exposure_filter_reads_the_rest_base(rules):
    a = _mapper("orderitem", "Orderitem", ("getList", "select"), ("getDetail", "select"))
    b = _mapper("ORDERITEM", "ORDERITEM", ("getList", "select"), ("getDetail", "select"))
    api = FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/orderitem-2/get-detail")])
    first, second = build_resources([], MapperIndex([a, b]), api, rules)
    assert first.rest_base == "orderitem" and all(m.endpoint is not None for m in first.methods)
    assert second.rest_base == "orderitem-2"
    assert [m.name for m in second.methods if m.endpoint] == ["getDetail"]


# ---- ajax calls without a router controller -----------------------------------------


def _router_service(name: str) -> ServiceDef:
    return ServiceDef(name=f"{name}-service", resource=name, x_name=name[:1].upper() + name[1:], has_router=True,
                      activities=[Activity("router", "x.ExampleDefaultRouter", "router", transitions={"go": "w"}),
                                  Activity("w", "x.WorkActivity")], transitions={"go": "w"})


def test_ajax_targets_without_controller(rules):
    plain = ServiceDef(name="orderMgmt-service", resource="orderMgmt", x_name="OrderMgmt",
                       activities=[Activity("a", "ExampleJdbcSearch", "search", {"sql-key": "x.getList"})])
    res = build_resources([_router_service("shop"), _router_service("Cart"), plain], MapperIndex([]), None, rules)
    called = {"js/z.js": ["shop", "orderMgmt", "nothing", "cart", "shop"], "js/a.js": ["Cart", "orderMgmt"]}
    items = ajax_targets_without_controller(called, res, rules)
    assert [(q.component, q.reason.split(":")[0]) for q in items] == [
        ("js_ajax::js/a.js", "[AJAX_NO_CONTROLLER] service 'orderMgmt'"),
        ("js_ajax::js/z.js", "[AJAX_NO_CONTROLLER] service 'cart'"),
        ("js_ajax::js/z.js", "[AJAX_NO_CONTROLLER] service 'nothing'"),
        ("js_ajax::js/z.js", "[AJAX_NO_CONTROLLER] service 'orderMgmt'"),
    ]
    reasons = {q.reason.split("'")[1]: q.reason for q in items if q.component == "js_ajax::js/z.js"}
    assert "has no router" in reasons["orderMgmt"]
    assert "'Cart'" in reasons["cart"] and "case" in reasons["cart"]
    assert "no service of that name was converted" in reasons["nothing"]
    assert ajax_targets_without_controller({}, res, rules) == []


# ---- router names reserve their address ---------------------------------------------


def test_router_reserves_its_name_as_written_and_takes_no_kebab_slot(rules):
    router = _router_service("ORDER")
    mapper = _mapper("order", "Order", ("detail", "select"))
    res = _by_x(build_resources([router], MapperIndex([mapper]), None, rules))
    assert res["ORDER"].rest_base == "ORDER" and res["ORDER"].has_router
    assert res["Order2"].rest_base == "order"          # nothing occupies /api/order


def test_mapper_base_never_equals_a_router_name(rules):
    router = _router_service("order")
    mapper = _mapper("ORDER", "ORDER", ("detail", "select"))
    other = _mapper("Order-x", "OrderX", ("detail", "select"))
    res = _by_x(build_resources([router], MapperIndex([mapper, other]), None, rules))
    assert res["Order2"].rest_base == "order" and res["Order2"].has_router
    assert res["ORDER"].rest_base == "order-2"         # /api/order is the router's
    assert res["OrderX"].rest_base == "order-x"


# ---- endpoints the exposure filter removed -------------------------------------------


def test_removed_endpoints_stay_traceable(rules):
    api = FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/order-item")])
    lov = _mapper("order-item", "OrderItem", ("getList", "select"), ("save", "insert"), ("lovCodes", "select"),
                  ("delete", "delete"))
    [res] = build_resources([], MapperIndex([lov]), api, rules)
    got = {m.name: (m.endpoint, m.unexposed) for m in res.methods}
    assert got == {"getList": (Endpoint("GET", ""), None), "save": (None, Endpoint("POST", "")),
                   "lovCodes": (None, None), "delete": (None, Endpoint("DELETE", "/{id}"))}
    assert unexposed_endpoints([res], rules) == ["DELETE /api/order-item/{id}  (OrderItemService.delete)",
                                                 "POST /api/order-item  (OrderItemService.save)"]


def test_no_removal_leaves_nothing_unexposed(rules):
    [res] = build_resources([], _order_idx(), FrontendApiMap(rest_endpoints=[RestCall("GET", "/api/order-item/x")]),
                            rules)
    assert all(m.unexposed is None for m in res.methods) and unexposed_endpoints([res], rules) == []
