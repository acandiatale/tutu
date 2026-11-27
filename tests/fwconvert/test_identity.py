"""Project name, domain and package of the generated project (docs/FW-CONVERT.md 3.2, scenario A13)."""
from __future__ import annotations

import pytest

from sbmig.fwconvert.identity import FALLBACK_NAME, derive_identity, java_segment, strip_archive_name
from sbmig.fwconvert.rules import load_rules

OVERLAY = {"naming": {"domains": ["d1", "d2"], "default_domain": "d1", "package_root": "com.example"}}


@pytest.fixture
def rules():
    return load_rules("Example", OVERLAY)


def test_a13_sample_shop_clean(rules):
    ident = derive_identity("sample-shop-clean.zip", rules, domain="d1")
    assert ident.project_name == "shopProject"
    assert ident.package_base == "com.example.d1.shopProject"
    assert ident.domain == "d1" and ident.source == "file"
    assert (ident.modules.boot, ident.modules.domain, ident.modules.service, ident.modules.store) == \
        ("shopProject-boot", "shopProject-domain", "shopProject-service", "shopProject-store")


@pytest.mark.parametrize("raw, want", [
    ("shop.zip", "shop"), ("shop.tar.gz", "shop"), ("SHOP.TGZ", "SHOP"), ("app.war", "app"), ("lib.jar", "lib"),
    ("dir/sub/shop.zip", "shop"), ("C:\\up\\shop.zip", "shop"), ("shop.war.zip", "shop"), ("shop", "shop"),
    ("shop.v2.zip", "shop.v2"),
])
def test_strip_archive_name(raw, want):
    assert strip_archive_name(raw) == want


@pytest.mark.parametrize("file_name, want", [
    ("sample-demo-shop.zip", "demoShopProject"),         # one prefix only
    ("shop-final-clean.zip", "shopFinalProject"),        # one suffix only
    ("SAMPLE-Shop-CLEAN.zip", "shopProject"),            # case ignored
    ("order-item-v2.tgz", "orderItemProject"),           # kebab to camel
    ("example-shop.zip", "shopProject"),                 # the {fw}- prefix
    ("shop-backup.zip", "shopProject"),
    ("my shop.zip", "myShopProject"),
])
def test_name_rules(rules, file_name, want):
    assert derive_identity(file_name, rules).project_name == want


def test_longest_prefix_wins():
    r = load_rules("Example", {"naming": {"name_prefixes": ["d1-", "d1-app-"]}})
    assert derive_identity("d1-app-shop.zip", r).project_name == "shopProject"
    assert derive_identity("d1-shop.zip", r).project_name == "shopProject"
    r = load_rules("Example", {"naming": {"name_suffixes": ["-old", "-shop-old"]}})
    assert derive_identity("main-shop-old.zip", r).project_name == "mainProject"


def test_override_is_used_as_given_without_suffix(rules):
    ident = derive_identity("sample-shop.zip", rules, override="MyShop")
    assert ident.project_name == "MyShop" and ident.source == "override"
    assert ident.package_base == "com.example.d1.MyShop"
    assert derive_identity("x.zip", rules, override="my-shop!").project_name == "myshop"
    assert derive_identity("x.zip", rules, override="class").project_name == "class_"
    assert derive_identity("x.zip", rules, override="9shop").project_name == "x9shop"


def test_override_that_cleans_to_nothing_falls_back_to_the_file(rules):
    ident = derive_identity("sample-shop.zip", rules, override="---")
    assert ident.project_name == "shopProject" and ident.source == "file"


@pytest.mark.parametrize("file_name, want", [
    ("sample-.zip", "sampleProject"),                    # nothing left after the prefix: the name before it
    ("-clean.zip", "cleanProject"),
    ("@@@.zip", FALLBACK_NAME + "Project"),              # only symbols
    (".zip", FALLBACK_NAME + "Project"),
    ("", FALLBACK_NAME + "Project"),
    ("상점.zip", FALLBACK_NAME + "Project"),             # no ASCII letter
])
def test_empty_name_fallback(rules, file_name, want):
    assert derive_identity(file_name, rules).project_name == want


def test_digit_leading_name_is_repaired(rules):
    ident = derive_identity("2024-shop.zip", rules)
    assert ident.project_name == "x2024ShopProject"
    assert ident.package_base == "com.example.d1.x2024ShopProject"


def test_domain_rules(rules):
    assert derive_identity("shop.zip", rules, domain="D2").domain == "d2"
    assert derive_identity("shop.zip", rules, domain="zz").domain == "d1"        # not listed: the default
    assert derive_identity("shop.zip", rules).domain == "d1"
    bare = load_rules("Example", {"naming": {"package_root": "com.example"}})
    ident = derive_identity("shop.zip", bare, domain="d1")                     # no list, no default
    assert ident.domain == "" and ident.package_base == "com.example.shopProject"


def test_bad_package_segments_are_repaired():
    r = load_rules("Example", {"naming": {"package_root": "com.9corp.int", "domains": ["new", "1a"]}})
    assert derive_identity("shop.zip", r, domain="new").package_base == "com.x9corp.int_.new_.shopProject"
    assert derive_identity("shop.zip", r, domain="1A").package_base == "com.x9corp.int_.x1a.shopProject"


@pytest.mark.parametrize("raw, want", [("shop", "shop"), ("9a", "x9a"), ("int", "int_"), ("_", ""), ("a-b", "ab"),
                                       ("", ""), ("_x", "_x")])
def test_java_segment(raw, want):
    assert java_segment(raw) == want


def test_deterministic(rules):
    assert derive_identity("sample-shop-clean.zip", rules, domain="d1") == \
        derive_identity("sample-shop-clean.zip", rules, domain="d1")
