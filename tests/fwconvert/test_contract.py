"""The shared pieces of sbmig.fwconvert: the name rules, the rules loader and the mapper index."""
from __future__ import annotations

import pytest

from sbmig.fwconvert import naming
from sbmig.fwconvert.model import Layout, MapperFile, MapperIndex, ModuleNames, ProjectIdentity, Statement
from sbmig.fwconvert.rules import RulesError, load_rules


def _layout() -> Layout:
    ident = ProjectIdentity("shopProject", "d1", "com.example.d1.shopProject",
                            ModuleNames("shopProject-boot", "shopProject-domain", "shopProject-service",
                                        "shopProject-store"))
    return Layout(ident)


@pytest.mark.parametrize("raw, want", [("ab1234", "Ab1234"), ("itemLOV", "ItemLOV"), ("order-item", "OrderItem"),
                                       ("order_item", "OrderItem"), ("9lives", "X9lives")])
def test_x_name(raw, want):
    assert naming.x_name(raw) == want


@pytest.mark.parametrize("raw, want", [("OrderItem", "order-item"), ("orderItem", "order-item"),
                                       ("order_item", "order-item"), ("ab1234", "ab1234"), ("getHTTPCode", "get-http-code")])
def test_kebab(raw, want):
    assert naming.kebab(raw) == want


@pytest.mark.parametrize("raw, want", [("order-item.getList", "getList"), ("GetList", "getList"), ("class", "class_"),
                                       ("select-all", "selectAll"), ("1st", "_1st")])
def test_method_name(raw, want):
    assert naming.method_name(raw) == want


def test_strip_suffix_ignores_case():
    assert naming.strip_suffix("Order-Service", "-service") == "Order"
    assert naming.strip_suffix("order", "-service") == "order"


def test_rules_fill_the_framework_name_in_keys_and_values():
    rules = load_rules("Example")
    assert rules.get("activities.kinds")["ExampleMyBatisSearch"] == "search"
    assert rules.text("files.sql_extension") == ".example_sql"
    assert "example-" in rules.strs("naming.name_prefixes")
    assert "{Fw}" not in repr(rules.data) and "{fw}" not in repr(rules.data)


def test_rules_overlay_merges_dicts_extends_lists_and_replaces_scalars():
    rules = load_rules("Example", {"naming": {"package_root": "org.sample", "name_prefixes": ["d1-", "demo-"]},
                                   "activities": {"kinds": {"ShopUpsert": "upsert"}}})
    assert rules.text("naming.package_root") == "org.sample"
    assert rules.strs("naming.name_prefixes").count("demo-") == 1 and "d1-" in rules.strs("naming.name_prefixes")
    kinds = rules.get("activities.kinds")
    assert kinds["ShopUpsert"] == "upsert" and kinds["ExampleJdbcUpdate"] == "update"


def test_rules_need_a_name_and_a_readable_file(tmp_path):
    with pytest.raises(RulesError):
        load_rules("")
    with pytest.raises(RulesError):
        load_rules("Example", path=tmp_path / "missing.yml")


def test_rules_get_returns_a_copy():
    rules = load_rules("Example")
    rules.get("activities.kinds")["x"] = "y"
    assert "x" not in rules.get("activities.kinds")


def test_layout_paths_and_packages():
    lay = _layout()
    assert lay.java_path(lay.spec_pkg, "OrderService") == \
        "shopProject-domain/src/main/java/com/example/d1/shopProject/domain/spec/OrderService.java"
    assert lay.java_path(lay.mybatis_store_pkg, "OrderMybatisStore").startswith("shopProject-store/")
    assert lay.java_path(lay.spring_logic_pkg, "OrderSpringLogic").startswith("shopProject-service/")
    assert lay.java_path(lay.rest_pkg, "OrderController").startswith("shopProject-service/")
    assert lay.java_path(lay.pkg, "Application").startswith("shopProject-boot/")
    assert lay.mapper_path("OrderItem") == "shopProject-boot/src/main/resources/mybatis/mapper/order-item-mapper.xml"
    assert lay.mapper_namespace("Order") == "com.example.d1.shopProject.store.OrderStore"
    assert lay.static_dir == "shopProject-boot/src/main/webapp/WEB-INF/static/shopProject"


def test_mapper_index_prefers_the_exact_original_id_over_a_prefix_match():
    # one file holds a.getList and b.getList; the second was renamed, and both prefixes are keys of the file
    m = MapperFile("mixed", "Mixed", "ns.MixedStore", "m.xml", keys=["mixed", "a", "b"],
                   statements=[Statement("getList", "a.getList"), Statement("getList_2", "b.getList")])
    idx = MapperIndex([m])
    assert idx.find("b.getList")[1].id == "getList_2"
    assert idx.find("a.getList")[1].id == "getList"
    assert idx.find("mixed.getList")[1].id == "getList"


def test_mapper_index_finds_by_original_id_then_prefix_then_unique_id():
    a = MapperFile("order", "Order", "ns.OrderStore", "a.xml", keys=["order"],
                   statements=[Statement("getList", "order.getList"), Statement("save", "save")])
    b = MapperFile("item", "Item", "ns.ItemStore", "b.xml", keys=["item", "it"],
                   statements=[Statement("getList", "it.getList"), Statement("remove", "remove")])
    idx = MapperIndex([a, b])
    assert idx.find("order.getList")[0] is a
    assert idx.find("IT.getList")[0] is b
    assert idx.find("other.remove")[0] is b            # no mapper is called `other`; the id is unique
    assert idx.find("other.getList") is None            # two mappers have it
    assert idx.find("") is None and idx.find("nope.nothing") is None
    assert idx.by_x("Item") is b and idx.by_x("Nope") is None
