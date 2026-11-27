"""convert_project: the step order's results, the refusals, the screen check, the datasource hand-over items and the
counts (out/scratch/fwconvert/ROUND2.md section P)."""
from __future__ import annotations

import hashlib

import yaml
from test_chain import OVERLAY, _project

from sbmig.fwconvert import pipeline
from sbmig.fwconvert.model import ConvertOptions
from sbmig.fwconvert.pipeline import convert_project
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.skeleton import MYBATIS_STARTER

RULES = load_rules("Example", OVERLAY)
SECRET = "pw-Never-Copied-42"
STATIC = "shopProject-boot/src/main/webapp/WEB-INF/static/shopProject"


def _options(**kw) -> ConvertOptions:
    base = {"source_name": "sample-shop-clean.zip", "domain": "d1", "boot_version": "4.1.0",
            "versions": {MYBATIS_STARTER: "4.1.0"}, "jdbc_drivers": {"h2": "com.h2database:h2"}}
    base.update(kw)
    return ConvertOptions(**base)


def _tree(root) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _digest(root) -> str:
    h = hashlib.sha256()
    for rel, data in _tree(root).items():
        h.update(rel.encode() + b"\0" + data)
    return h.hexdigest()


def test_nothing_to_convert_leaves_the_output_untouched(tmp_path):
    src = tmp_path / "src"
    (src / "WebContent").mkdir(parents=True)
    (src / "WebContent/a.jsp").write_text("<html/>", encoding="utf-8")
    (src / "src/main/java").mkdir(parents=True)
    (src / "src/main/java/A.java").write_text("class A {}", encoding="utf-8")
    out = tmp_path / "out"
    result = convert_project(src, out, RULES, _options())
    assert not result.converted and result.identity is None and not out.exists()
    out.mkdir()
    result = convert_project(src, out, RULES, _options())
    assert not result.converted and list(out.iterdir()) == []


def test_a_non_empty_output_folder_a_file_or_one_inside_the_source_is_refused(tmp_path):
    src = tmp_path / "src"
    _project(src)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    assert not result.converted and "not empty" in result.errors[0]
    assert [p.name for p in out.iterdir()] == ["keep.txt"]
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert "is a file" in convert_project(src, tmp_path / "file", RULES, _options()).errors[0]
    before = _digest(src)
    inside = convert_project(src, src / "generated", RULES, _options())
    assert "inside the source folder" in inside.errors[0] and _digest(src) == before


def test_the_source_is_never_written_and_the_counts_are_complete(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    before = _digest(src)
    result = convert_project(src, out, RULES, _options())
    assert _digest(src) == before
    assert result.converted and result.errors == []
    assert result.counts == {"services": 1, "mappers": 2, "statements": 5, "resources": 2, "java_files": 17,
                             "jsp": 4, "static": 2, "js_rewritten": 2, "pages": 3, "queue": len(result.queue)}
    tree = _tree(out)
    assert {"pom.xml", "shopProject-boot/pom.xml", "shopProject-domain/pom.xml", "shopProject-service/pom.xml",
            "shopProject-store/pom.xml", "shopProject-boot/src/main/resources/application.yml"} <= set(tree)
    assert sum(1 for p in tree if p.endswith(".java")) == result.counts["java_files"]
    web = tree["shopProject-boot/src/main/java/com/example/d1/shopProject/config/WebMvcConfig.java"].decode()
    # one handler per top folder of the static files: css and js here, not six fixed ones
    assert '"/css/**"' in web and '"/js/**"' in web and '"/images/**"' not in web
    # no datasource in this project: said, and no datasource keys
    assert any(q.reason.startswith("[DATASOURCE] not_found") for q in result.queue)
    assert "datasource" not in tree["shopProject-boot/src/main/resources/application.yml"].decode()
    assert result.datasource is not None and not result.datasource.configured


def test_a_datasource_bean_becomes_application_yml_and_a_driver_and_the_password_stays_behind(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "src/main/resources/applicationContext.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans">
  <bean id="dataSource" class="org.apache.commons.dbcp2.BasicDataSource">
    <property name="driverClassName" value="${jdbc.driver}"/>
    <property name="url" value="${jdbc.url}"/>
    <property name="username" value="${jdbc.user}"/>
    <property name="password" value="${jdbc.password}"/>
  </bean>
</beans>
""", encoding="utf-8")
    (src / "src/main/resources/jdbc.properties").write_text(
        f"jdbc.driver=org.h2.Driver\njdbc.url=jdbc:h2:mem:shop\njdbc.user=sa\njdbc.password={SECRET}\n",
        encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    assert result.errors == []
    yml = yaml.safe_load((out / "shopProject-boot/src/main/resources/application.yml").read_text(encoding="utf-8"))
    assert yml["spring"]["datasource"] == {"url": "jdbc:h2:mem:shop", "driver-class-name": "org.h2.Driver",
                                           "username": "sa", "password": "${SPRING_DATASOURCE_PASSWORD:}"}
    boot = (out / "shopProject-boot/pom.xml").read_text(encoding="utf-8")
    assert "<artifactId>h2</artifactId>" in boot and "<scope>runtime</scope>" in boot
    pw = [q for q in result.queue if q.reason.startswith("[DATASOURCE_PASSWORD]")]
    assert len(pw) == 1
    assert "src/main/resources/applicationContext.xml" in pw[0].reason
    assert "src/main/resources/jdbc.properties" in pw[0].reason
    assert not any(q.reason.startswith("[DATASOURCE] ") for q in result.queue)
    # the value is nowhere: not in the generated tree, not in the result
    assert all(SECRET.encode() not in data for data in _tree(out).values())
    assert SECRET not in repr(result)


def test_a_vendor_without_a_driver_coordinate_is_handed_over(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "src/main/resources/applicationContext.xml").write_text(
        '<beans><bean id="ds" class="x.BasicDataSource"><property name="url" value="jdbc:tibero:thin:@h:8629:d"/>'
        '</bean></beans>', encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    assert result.datasource.configured and result.datasource.vendor == "tibero"
    assert any(q.reason.startswith("[DATASOURCE_DRIVER]") for q in result.queue)


def test_the_screen_check_names_a_missing_file_and_lists_a_refused_one(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    # the same target from a second webapp root: the copy keeps the first, the second is missing on screen
    (src / "web/webapp/js").mkdir(parents=True)
    (src / "web/webapp/js/order.js").write_text("var other = 1;\n", encoding="utf-8")
    # a stylesheet inside WEB-INF is refused on purpose: listed, not missing
    (src / "WebContent/WEB-INF/css").mkdir(parents=True)
    (src / "WebContent/WEB-INF/css/hidden.css").write_text("a{}", encoding="utf-8")
    # a static file that lands at the top of the static folder is served by no handler: said
    (src / "WebContent/favicon.ico").write_bytes(b"\0\1")
    result = convert_project(src, out, RULES, _options())
    missing = [q for q in result.queue if q.reason.startswith("FRONTEND_MISSING")]
    assert [q.component for q in missing] == ["frontend::web/webapp/js/order.js"]
    assert "same target" in missing[0].reason
    assert any(s.startswith("WebContent/WEB-INF/css/hidden.css:") for s in result.skipped)
    assert not any("hidden.css" in q.component for q in result.queue)
    assert any(s.startswith("WebContent/favicon.ico:") and "resource handler" in s for s in result.skipped)


def test_duplicate_mappings_become_errors(tmp_path, monkeypatch):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    seen = []

    def clash(files):
        seen.append(sorted(f.path for f in files if f.path.endswith("PageController.java")))
        return ["GET /x: claimed by A.a and B.b"]

    monkeypatch.setattr(pipeline, "duplicate_mappings", clash)
    result = convert_project(src, out, RULES, _options())
    assert result.errors == ["GET /x: claimed by A.a and B.b"]
    # the check reads the page controllers too, not only the resource classes
    assert seen and len(seen[0]) == 3


def test_a_missing_mybatis_version_writes_no_pom_and_is_the_first_error(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    result = convert_project(src, out, RULES, _options(versions={}))
    assert result.converted and MYBATIS_STARTER in result.errors[0]
    assert not list(out.rglob("pom.xml"))
    assert (out / "shopProject-boot/src/main/resources/application.yml").is_file()


def test_unexposed_endpoints_and_risky_classes_reach_the_result(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "WebContent/js/item.js").write_text("function l() { return api.get('/api/item'); }\n", encoding="utf-8")
    (src / "src/main/java/com/example/batch").mkdir(parents=True)
    (src / "src/main/java/com/example/batch/Job.java").write_text(
        "package com.example.batch;\nclass Job { void r() throws Exception { Class.forName(\"x\"); "
        "Thread.sleep(1); synchronized (this) {} Object m = null; Method.invoke(); x.getInstance(); } }\n",
        encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    assert result.unexposed == ["GET /api/item/count-all  (ItemService.countAll)"]
    assert any(q.reason.startswith("risk_score>=") for q in result.queue)


def test_a_method_of_a_service_without_a_router_that_gets_no_address_is_named(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "src/main/resources/service/stock-service.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<service name="stock-service">
  <activity name="count" class="com.example.fw.activity.ExampleMyBatisSearch">
    <property name="sql-key" value="stock.nowhere"/>
  </activity>
</service>
""", encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    no_address = [u for u in result.unexposed if u.startswith("no address: ")]
    assert any(u.startswith("no address: StockService.") for u in no_address), result.unexposed
    # the two kinds stay apart: an endpoint left out by the front-end rule has no prefix
    assert all(u.startswith(("no address: ", "GET ", "POST ", "DELETE ")) for u in result.unexposed)


def test_a_plain_spring_project_with_a_service_named_context_file_is_not_converted(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    (src / "src/main/resources/spring").mkdir(parents=True)
    (src / "src/main/resources/spring/context-service.xml").write_text(
        '<beans xmlns="http://www.springframework.org/schema/beans"><bean id="orderService" '
        'class="com.acme.OrderServiceImpl"><property name="dao" ref="orderDao"/></bean></beans>\n', encoding="utf-8")
    (src / "src/main/java/com/acme").mkdir(parents=True)
    (src / "src/main/java/com/acme/OrderServiceImpl.java").write_text("package com.acme;\nclass OrderServiceImpl {}\n")
    assert not pipeline.has_convertible(src, RULES)
    result = convert_project(src, out, RULES, _options())
    assert not result.converted and not out.exists()


def test_has_convertible_counts_only_real_framework_files(tmp_path):
    def project(files: dict[str, str | bytes]):
        root = tmp_path / f"p{len(list(tmp_path.iterdir()))}"
        for rel, body in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
        return root

    no = [
        {"a/order-service.xml": '<service id="order" class="x.OrderService"><property name="dao" ref="d"/></service>'},
        {"a/order-service.xml": "<service name='order-service'></service>"},
        {"a/order-service.xml": ""},
        {"a/order-service.xml": b"\xff\xfe\x00garbage<"},
        {"a/x.example_sql": "<queryMap desc='x'></queryMap>"},
        {"a/x.example_sql": "<sqlMap><query id='a'>select 1</query></sqlMap>"},
        {"a/x.example_sql": '<!DOCTYPE q [<!ENTITY e SYSTEM "file:///etc/passwd">]><queryMap>&e;</queryMap>'},
        {"target/x.example_sql": "<queryMap><query id='a'>select 1</query></queryMap>"},
        {},
    ]
    for files in no:
        assert not pipeline.has_convertible(project(files), RULES), files
    yes = [
        {"a/x.example_sql": "<?xml version='1.0'?><queryMap xmlns='http://example.invalid/q'><query id='a'>"
                            "<![CDATA[select 1]]></query></queryMap>"},
        {"a/order-service.xml": "<service name='order-service'><activity name='l' class='a.B'/></service>"},
        {"a/order-service.xml": "<services><service name='o'><activity name='l' class='a.B'/></service></services>"},
        {"a/x.example_sql": b"<?xml version='1.0' encoding='MS949'?><queryMap><query id='a'>"
 + "선택".encode("cp949") + b"</query></queryMap>"},
    ]
    for files in yes:
        assert pipeline.has_convertible(project(files), RULES), files
    assert not pipeline.has_convertible(tmp_path / "missing", RULES)


def test_a_url_left_out_for_its_credentials_is_said_in_the_datasource_item(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "src/main/resources/applicationContext.xml").write_text(
        f'<beans><bean id="ds" class="x.BasicDataSource"><property name="url" '
        f'value="jdbc:mysql://dbhost/shop?db_password1={SECRET}"/></bean></beans>', encoding="utf-8")
    result = convert_project(src, out, RULES, _options())
    items = [q for q in result.queue if q.reason.startswith("[DATASOURCE] unresolved")]
    assert len(items) == 1 and "credentials" in items[0].reason
    assert all(SECRET.encode() not in data for data in _tree(out).values()) and SECRET not in repr(result)


def test_has_convertible_reads_as_tolerantly_as_the_converters(tmp_path):
    def project(name: str, data: bytes):
        root = tmp_path / f"p{len(list(tmp_path.iterdir()))}"
        (root / "src").mkdir(parents=True)
        (root / "src" / name).write_bytes(data)
        return root

    yes = [
        ("o.example_sql", b"<queryMap>\n  <query id='a.b'>select * from t where a < 3 & b = 1</query>\n</queryMap>"),
        ("o.example_sql", b"<queryMap><group><query id='a.b.c'>select 1</query></group></queryMap>"),
        ("o.example_sql", (b"<?xml version='1.0'?><!DOCTYPE queryMap SYSTEM 'q.dtd'><!-- <x> -->"
                          b"<q:queryMap xmlns:q='urn:x'><q:query id='a'>select 1</q:query></q:queryMap>")),
        ("order-service.xml", b"<service name='o'><desc>a&nbsp;b</desc><activity name='a' class='x.Y'/></service>"),
        ("order-service.xml", (b"<!DOCTYPE service SYSTEM 's.dtd'><service name='o'>&common;<activity name='a' class='x.Y'/>"
                              b"</service>")),
        # a comment longer than the head does not count against it
        ("order-service.xml", b"<service name='o'><!-- " + b"x" * (1024 * 1024 + 10) + b" --><activity name='a' class='x.Y'/>"
                              b"</service>"),
    ]
    for name, data in yes:
        assert pipeline.has_convertible(project(name, data), RULES), data[:80]
    no = [
        ("o.example_sql", b"<!-- <queryMap><query id='a'/></queryMap> --><other/>"),
        ("o.example_sql", b"<queryMap><!-- <query id='a'/> --></queryMap>"),
        ("o.example_sql", b"<root><queryMap><query id='a'/></queryMap></root>"),
        ("order-service.xml", b"<service id='o' class='x.Y'><property name='a' ref='b'/></service>"),
        ("order-service.xml", b"<beans><bean id='activity' class='x.Y'/></beans>"),
        ("order-service.xml", b"<service name='o'><!-- <activity name='a'/> --></service>"),
    ]
    for name, data in no:
        assert not pipeline.has_convertible(project(name, data), RULES), data[:80]


def test_convertible_summary_counts_the_candidates_by_name(tmp_path):
    root = tmp_path / "p"
    (root / "a").mkdir(parents=True)
    (root / "a/context-service.xml").write_text("<beans/>", encoding="utf-8")
    (root / "a/x.example_sql").write_text("<sqlMap/>", encoding="utf-8")
    (root / "a/README.txt").write_text("x", encoding="utf-8")
    assert pipeline.convertible_summary(root, RULES) == (False, 2)
    (root / "a/y.example_sql").write_text("<queryMap><query id='a'>select 1</query></queryMap>", encoding="utf-8")
    assert pipeline.convertible_summary(root, RULES) == (True, 3)
    assert pipeline.convertible_summary(tmp_path / "missing", RULES) == (False, 0)


def test_files_shaped_like_other_frameworks_are_not_convertible(tmp_path):
    cases = {
        "dubbo": ('<beans xmlns:dubbo="http://dubbo.apache.org/schema/dubbo" xmlns:act="http://acme/act">'
                  '<dubbo:service interface="a.B" ref="o"/><act:activity id="audit" class="a.C"/></beans>'),
        "blueprint": ('<blueprint><service ref="o" interface="a.B"/><bean id="t" class="a.T"><property name="b">'
                      '<value><![CDATA[<activity type="x" class="a.C"/>]]></value></property></bean></blueprint>'),
        "android": ('<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application>'
                    '<service android:name=".S"/><activity android:name=".Main"/></application></manifest>'),
        "activiti_attr": '<service name="o"><activity name="a" activiti:class="a.B"/></service>',
        "prefixed_service": '<x:service name="o"><activity name="a" class="a.B"/></x:service>',
    }
    for name, body in cases.items():
        root = tmp_path / name
        (root / "res").mkdir(parents=True)
        (root / "res" / f"{name}-service.xml").write_text(body, encoding="utf-8")
        assert pipeline.convertible_summary(root, RULES) == (False, 1), name
        out = tmp_path / f"{name}-out"
        assert not convert_project(root, out, RULES, _options()).converted and not out.exists()


def test_nothing_read_by_the_converters_means_not_converted(tmp_path, monkeypatch):
    # the pre-check says yes, but the converters find no activity service and no framework SQL statement:
    # a copied MyBatis mapper alone does not make a framework project
    src, out = tmp_path / "src", tmp_path / "out"
    (src / "src/main/resources/mybatis").mkdir(parents=True)
    (src / "src/main/resources/mybatis/order-mapper.xml").write_text(
        '<mapper namespace="a.OrderMapper"><select id="list" resultType="map">select 1</select></mapper>',
        encoding="utf-8")
    (src / "src/main/resources/bean-service.xml").write_text(
        '<service id="o" class="a.B"><property name="x" ref="y"/></service>', encoding="utf-8")
    monkeypatch.setattr(pipeline, "has_convertible", lambda root, rules: True)
    result = convert_project(src, out, RULES, _options())
    assert not result.converted and result.identity is None and not out.exists()
