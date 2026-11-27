"""File kinds, component kind / risk / order and the risk hand-over items (docs/FW-CONVERT.md 2.1, 2.4, 4)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sbmig.fwconvert import inventory as inv_mod
from sbmig.fwconvert.inventory import Component, classify_component, conversion_order, risk_queue, scan_tree
from sbmig.fwconvert.rules import load_rules

MAPPER = '<?xml version="1.0"?>\n<!DOCTYPE mapper>\n<mapper namespace="a.b.Order">\n<select id="x">SELECT 1</select>\n</mapper>\n'


@pytest.fixture
def rules():
    return load_rules("Example", {"components": {"batch_markers": ["ShopNightJob"], "risk": {"LegacyRunner": 0.4}}})


def _write(root: Path, rel: str, text: str = "", data: bytes | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_bytes(data)
    else:
        p.write_text(text, encoding="utf-8")
    return p


def _tree(root: Path) -> None:
    _write(root, "src/sql/order.example_sql", "<queryMap><query id='a'>SELECT 1</query></queryMap>")
    _write(root, "src/sql/UPPER.EXAMPLE_SQL", "<queryMap/>")
    _write(root, "conf/order-service.xml", "<service name='order-service'/>")
    _write(root, "res/mybatis/order.xml", MAPPER)
    _write(root, "res/mybatis/item.mapper", MAPPER)
    _write(root, "res/mybatis/mybatis-config.xml", "<configuration/>")          # not a mapper: other
    _write(root, "res/mybatis/root-context.xml", "<beans/>")                    # not a mapper: db_config
    _write(root, "res/mybatis/x-service.xml", MAPPER)                           # a mapper beats the name
    _write(root, "res/mybatis/plain.mapper", "nothing here")                    # .mapper without namespace
    _write(root, "res/sqlmap/order.xml", MAPPER)                                # not under mybatis
    _write(root, "conf/applicationContext.xml", "<beans/>")
    _write(root, "conf/db-context.xml", "<beans/>")
    _write(root, "conf/MyDataSource.xml", "<beans/>")
    _write(root, "conf/jdbc.properties", "a=b")
    _write(root, "conf/datasource-service.xml", "<service/>")                   # service beats db_config
    _write(root, "WebContent/WEB-INF/web.xml", "<web-app/>")
    _write(root, "WebContent/WEB-INF/jsp/p/z.jsp", "<html/>")
    _write(root, "WebContent/WEB-INF/jsp/p/part.jspf", "<html/>")
    _write(root, "WebContent/js/app.js", "var a;")
    _write(root, "WebContent/img/LOGO.PNG", data=b"\x89PNG\r\n\x00\x00")
    _write(root, "src/java/a/Order.java", "package a;\npublic class Order {}\n")
    _write(root, "README.txt", "x")
    for skipped in ("target/x.java", ".git/config", "node_modules/m/index.js", "build/a.jsp", ".idea/w.xml",
                    ".settings/s.properties", "sub/target/classes/a.properties"):
        _write(root, skipped, "x")


def test_every_file_kind_and_precedence(tmp_path, rules):
    _tree(tmp_path)
    inv = scan_tree(tmp_path, rules)
    assert inv.sql == ["src/sql/UPPER.EXAMPLE_SQL", "src/sql/order.example_sql"]
    assert inv.service_xml == ["conf/datasource-service.xml", "conf/order-service.xml"]
    assert inv.mapper == ["res/mybatis/item.mapper", "res/mybatis/order.xml", "res/mybatis/x-service.xml"]
    assert inv.db_config == ["conf/MyDataSource.xml", "conf/applicationContext.xml", "conf/db-context.xml",
                             "conf/jdbc.properties", "res/mybatis/root-context.xml"]
    assert inv.ignored == ["WebContent/WEB-INF/web.xml"]
    assert inv.jsp == ["WebContent/WEB-INF/jsp/p/z.jsp"]
    assert inv.static == ["WebContent/img/LOGO.PNG", "WebContent/js/app.js"]
    assert inv.java == ["src/java/a/Order.java"]
    assert inv.other == ["README.txt", "WebContent/WEB-INF/jsp/p/part.jspf", "res/mybatis/mybatis-config.xml",
                         "res/mybatis/plain.mapper", "res/sqlmap/order.xml"]
    assert inv.errors == []
    assert inv.kind_of("res/mybatis/x-service.xml") == "mapper" and inv.kind_of("nope") is None
    assert [c.fqn for c in inv.components] == ["a.Order"]


def test_skipped_folders_are_never_entered(tmp_path, rules):
    _tree(tmp_path)
    inv = scan_tree(tmp_path, rules)
    every = [p for k in inv_mod.KINDS for p in inv.files(k)]
    for folder in ("target/", ".git/", "node_modules/", "build/", ".idea/", ".settings/"):
        assert not any(p.startswith(folder) or f"/{folder}" in p for p in every), folder


def test_two_scans_are_equal(tmp_path, rules):
    _tree(tmp_path)
    assert scan_tree(tmp_path, rules) == scan_tree(tmp_path, rules)


def test_mapper_check_reads_only_the_head(tmp_path, rules):
    big = "<mapper>" + " " * (inv_mod.MAPPER_HEAD_BYTES + 10) + "<mapper namespace='late'/>"
    _write(tmp_path, "mybatis/late.xml", big)
    _write(tmp_path, "mybatis/early.xml", "<mapper namespace='e'>" + "x" * (inv_mod.MAPPER_HEAD_BYTES * 2))
    inv = scan_tree(tmp_path, rules)
    assert inv.mapper == ["mybatis/early.xml"] and inv.other == ["mybatis/late.xml"]


def test_ms949_java_with_korean_comments(tmp_path, rules):
    src = "package com.shop.service;\n// 주문을 처리하는 서비스\nimport com.shop.dao.OrderDao;\n" \
          "/** 한글 설명 */\npublic class OrderServiceImpl { }\n"
    _write(tmp_path, "src/OrderServiceImpl.java", data=src.encode("cp949"))
    inv = scan_tree(tmp_path, rules)
    assert inv.errors == []
    (c,) = inv.components
    assert (c.package, c.class_name, c.kind, c.imports) == \
        ("com.shop.service", "OrderServiceImpl", "service", ["com.shop.dao.OrderDao"])
    items = risk_queue([Component(c.path, c.package, c.class_name, risk=1.0)], tmp_path, rules)
    assert "주문을 처리하는 서비스" in items[0].excerpt


def test_component_header_ignores_comments_and_strings(tmp_path, rules):
    src = ('/* package wrong.pkg; class Wrong */\npackage right.pkg;\n'
           'import static java.util.Collections.emptyList;\nimport java.util.*;\n'
           'public final class Real { String s = "class Fake"; }\n')
    _write(tmp_path, "Real.java", src)
    (c,) = scan_tree(tmp_path, rules).components
    assert (c.package, c.class_name, c.imports) == \
        ("right.pkg", "Real", ["java.util.Collections.emptyList", "java.util.*"])


def test_unreadable_file_is_an_error_entry(tmp_path, rules, monkeypatch):
    _write(tmp_path, "src/Broken.java", "class Broken {}")
    _write(tmp_path, "mybatis/m.xml", MAPPER)
    real_open = os.open

    def failing(path, *a, **kw):
        if Path(path).name in ("Broken.java", "m.xml"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(inv_mod.os, "open", failing)
    inv = scan_tree(tmp_path, rules)
    assert inv.java == ["src/Broken.java"] and inv.components == []
    assert inv.other == ["mybatis/m.xml"]
    assert inv.errors == ["mybatis/m.xml: cannot read: Permission denied",
                          "src/Broken.java: cannot read: Permission denied"]


def test_large_java_is_classified_on_its_head(tmp_path, rules, monkeypatch):
    monkeypatch.setattr(inv_mod, "MAX_JAVA_BYTES", 64)
    _write(tmp_path, "Big.java", "package a.dao;\nclass Big {}\n" + "// 한" * 100)
    inv = scan_tree(tmp_path, rules)
    assert [c.kind for c in inv.components] == ["dao"]
    assert inv.errors == ["Big.java: larger than 64 bytes, classified on its head"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinks_are_not_followed(tmp_path, rules):
    outside = tmp_path / "outside"
    _write(outside, "Secret.java", "class Secret {}")
    src = tmp_path / "src"
    _write(src, "A.java", "class A {}")
    (src / "link").symlink_to(outside, target_is_directory=True)
    (src / "S.java").symlink_to(outside / "Secret.java")
    inv = scan_tree(src, rules)
    assert inv.java == ["A.java"]
    assert inv.errors == ["S.java: symbolic link not followed", "link: symbolic link not followed"]


def test_missing_root_is_an_error(tmp_path, rules):
    inv = scan_tree(tmp_path / "nope", rules)
    assert inv.java == [] and len(inv.errors) == 1


# ---- component kind and risk ----


@pytest.mark.parametrize("text, kind", [
    ("package a.controller; class X extends ExampleChunkProcessor {}", "batch"),     # batch beats controller
    ("package a.web; class X implements ShopNightJob {}", "batch"),                    # overlay batch marker
    ("package a.batch; class X {}", "batch"),
    ("package a.service; @ExampleController class X {}", "controller"),              # controller beats service
    ("package a.dao; class OrderServiceImpl {}", "service"),                          # service beats dao
    ("package a.domain; class OrderDao {}", "dao"),                                   # dao beats domain
    ("package a.vo; class Order {}", "domain"),
    ("package a.util; class Strings {}", "unknown"),
])
def test_component_kind_precedence(rules, text, kind):
    assert classify_component(text, rules)[0] == kind


def test_risk_sum_cap_and_markers(rules):
    _kind, risk, markers = classify_component("Class.forName(x); Method.invoke(y); Thread.sleep(1);", rules)
    assert risk == 0.55 and markers == ["Class.forName", "Method.invoke", "Thread."]
    text = ("ExampleChunkProcessor ExampleFileWriter ExampleBatchContext Class.forName Method.invoke "
            "Thread. synchronized .getInstance() LegacyRunner")
    assert classify_component(text, rules)[1] == 1.0
    assert classify_component("class Plain {}", rules)[1:] == (0.0, [])


def test_threshold_is_inclusive_and_reason_names_markers(tmp_path, rules):
    _write(tmp_path, "a/Hot.java", "package a; class Hot { synchronized void m() { Class.forName(x); "
                                   "Method.invoke(y); ExampleChunkProcessor p; } }")
    _write(tmp_path, "a/Cool.java", "package a; class Cool { void m() { Class.forName(x); Method.invoke(y); } }")
    inv = scan_tree(tmp_path, rules)
    risks = {c.class_name: c.risk for c in inv.components}
    assert risks == {"Hot": 0.75, "Cool": 0.4}
    edge = Component("a/Cool.java", "a", "Cool", risk=0.7, markers=["X"])            # exactly the threshold
    items = risk_queue(inv.components + [edge], tmp_path, rules)
    assert [i.component for i in items] == ["a.Cool", "a.Hot"]
    hot = items[1]
    assert hot.reason == "risk_score>=0.7 ExampleChunkProcessor, Class.forName, Method.invoke, synchronized"
    assert hot.source_path == "a/Hot.java" and hot.excerpt.startswith("package a; class Hot")


def test_risk_queue_excerpt_cap_and_missing_source(tmp_path, rules):
    _write(tmp_path, "Long.java", "x" * 5000)
    items = risk_queue([Component("Long.java", "", "Long", risk=0.9), Component("Gone.java", "p", "Gone", risk=0.9)],
                       tmp_path, rules)
    assert len(items[1].excerpt) == 2000 and items[1].last_error == ""
    assert items[0].component == "p.Gone" and items[0].excerpt == "" and items[0].last_error.startswith("cannot read")


# ---- order ----


def _c(path: str, pkg: str, name: str, kind: str, *imports: str) -> Component:
    return Component(path=path, package=pkg, class_name=name, imports=list(imports), kind=kind)


def test_order_by_kind_then_dependency_chain(rules):
    comps = [
        _c("z/A.java", "p", "A", "domain", "p.B"),
        _c("y/B.java", "p", "B", "domain", "p.C"),
        _c("x/C.java", "p", "C", "domain"),
        _c("a/Ctl.java", "c", "Ctl", "controller"),
        _c("b/Dao.java", "d", "Dao", "dao", "p.A"),
        _c("c/U.java", "u", "U", "unknown"),
        _c("d/Job.java", "j", "Job", "batch"),
        _c("e/Svc.java", "s", "Svc", "service", "d.Dao.x"),
    ]
    order = [c.class_name for c in conversion_order(comps, rules)]
    assert order == ["C", "B", "A", "Dao", "Svc", "Ctl", "Job", "U"]


def test_order_ties_by_path_and_imports_of_members_and_wildcards(rules):
    comps = [
        _c("b/Two.java", "q", "Two", "dao", "q.One.CONST"),      # a static member import
        _c("a/Three.java", "r", "Three", "dao", "q.*"),          # a wildcard import
        _c("c/One.java", "q", "One", "dao"),
        _c("d/Free.java", "f", "Free", "dao"),
    ]
    assert [c.class_name for c in conversion_order(comps, rules)] == ["One", "Two", "Three", "Free"]


def test_order_with_a_cycle_appends_the_rest_in_path_order(rules):
    comps = [
        _c("c/X.java", "p", "X", "service", "p.Y"),
        _c("a/Y.java", "p", "Y", "service", "p.X"),
        _c("b/Z.java", "p", "Z", "service", "p.Y"),
        _c("d/Root.java", "p", "Root", "service"),
    ]
    assert [c.path for c in conversion_order(comps, rules)] == ["d/Root.java", "a/Y.java", "b/Z.java", "c/X.java"]


def test_unlisted_kind_goes_last(rules):
    comps = [_c("a.java", "", "A", "weird"), _c("b.java", "", "B", "unknown")]
    assert [c.kind for c in conversion_order(comps, rules)] == ["unknown", "weird"]


# ---- hostile input ----


@pytest.mark.parametrize("unit", ["package a", "extends ", "/* a", "'a", '"a', "class "])
def test_a_2mb_one_line_file_is_classified_quickly(tmp_path, rules, unit):
    text = unit * (2 * 1024 * 1024 // len(unit))
    started = time.monotonic()
    classify_component(text, rules)
    _write(tmp_path, "Hostile.java", text)
    scan_tree(tmp_path, rules)
    assert time.monotonic() - started < 5


def test_many_long_hostile_lines_stay_linear(rules):
    text = "\n".join(("package a" * 400) + str(i) for i in range(600))       # ~2 MB over 600 long lines
    started = time.monotonic()
    classify_component(text, rules)
    assert time.monotonic() - started < 5


def test_patterns_match_per_line_and_long_lines_are_cut(rules):
    assert classify_component("package a.\nbatch;", rules)[0] == "unknown"          # one line only
    tail = "x" * inv_mod.MATCH_LINE_CHARS + " ExampleController"
    assert classify_component(tail, rules)[0] == "unknown"                         # beyond the cut
    assert classify_component("ExampleController " + tail, rules)[0] == "controller"
    assert classify_component(tail, rules)[1] == 0.0 and \
        classify_component(tail + " Class.forName", rules)[1] == 0.2               # risk keys search the whole text


def test_unclosed_comment_and_strings_end_the_header(tmp_path, rules):
    _write(tmp_path, "A.java", "package p.q;\nimport a.B;\nclass A { String s = \"open\n; char c = 'x; }\n/* open")
    (c,) = scan_tree(tmp_path, rules).components
    assert (c.package, c.class_name, c.imports) == ("p.q", "A", ["a.B"])


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX modes and a non-root user")
def test_an_unreadable_folder_is_an_error(tmp_path, rules):
    _write(tmp_path, "ok/A.java", "class A {}")
    locked = tmp_path / "locked"
    _write(locked, "B.java", "class B {}")
    locked.chmod(0)
    try:
        inv = scan_tree(tmp_path, rules)
    finally:
        locked.chmod(0o755)
    assert inv.java == ["ok/A.java"]
    assert len(inv.errors) == 1 and inv.errors[0].startswith("locked: cannot read the folder (")


# ---- special files ----


def _finishes(fn, seconds: float = 5.0):
    import threading
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - handed back to the test thread
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "the call blocked on a special file"
    if "error" in box:
        raise box["error"]
    return box["value"]


needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is not available")


@needs_fifo
def test_a_named_pipe_is_left_out_and_never_opened(tmp_path, rules):
    _write(tmp_path, "src/A.java", "class A {}")
    os.mkfifo(tmp_path / "src/Pipe.java")
    (tmp_path / "mybatis").mkdir()
    os.mkfifo(tmp_path / "mybatis/x.xml")
    inv = _finishes(lambda: scan_tree(tmp_path, rules))
    every = [p for k in inv_mod.KINDS for p in inv.files(k)]
    assert every == ["src/A.java"]
    assert inv.errors == ["mybatis/x.xml: not a regular file", "src/Pipe.java: not a regular file"]


@needs_fifo
def test_readers_refuse_a_named_pipe(tmp_path, rules):
    pipe = tmp_path / "P.java"
    os.mkfifo(pipe)
    assert _finishes(lambda: inv_mod.read_java_text(pipe)) == ("", "not a regular file")
    with pytest.raises(OSError):
        _finishes(lambda: inv_mod._read_head(pipe, 16))
    (item,) = _finishes(lambda: risk_queue([Component("P.java", "p", "P", risk=1.0)], tmp_path, rules))
    assert item.excerpt == "" and item.last_error == "not a regular file"
