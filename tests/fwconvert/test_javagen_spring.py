"""The generated classes on the real Spring / MyBatis side: compile all of them, send requests through MockMvc and
run a count statement against H2 (jars from ~/.m2/repository, newest version present, nothing downloaded)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_javagen import a6_resource, make_layout, make_rules, stub_resource

from sbmig.fwconvert.javagen import generate_page_controllers, generate_resource, java_string, write_generated
from sbmig.fwconvert.model import Endpoint, FrontendApiMap, Method, Resource

pytestmark = pytest.mark.integration

M2 = Path.home() / ".m2" / "repository"
SPRING = ["spring-web", "spring-webmvc", "spring-context", "spring-beans", "spring-core", "spring-aop",
          "spring-expression", "spring-test", "spring-tx"]
# (group, artifacts that must share one version)
GROUPS = [("org.springframework", SPRING), ("tools.jackson.core", ["jackson-databind", "jackson-core"]),
          ("io.micrometer", ["micrometer-observation", "micrometer-commons"]),
          ("com.fasterxml.jackson.core", ["jackson-annotations"]), ("org.mybatis", ["mybatis"]),
          ("org.slf4j", ["slf4j-api"]), ("jakarta.servlet", ["jakarta.servlet-api"]),
          ("org.jspecify", ["jspecify"]), ("commons-logging", ["commons-logging"]), ("com.h2database", ["h2"])]


def _versions(group: str, artifact: str) -> set[str]:
    base = M2 / group.replace(".", "/") / artifact
    if not base.is_dir():
        return set()
    return {d.name for d in base.iterdir()
            if re.fullmatch(r"\d+(\.\d+)*", d.name) and (d / f"{artifact}-{d.name}.jar").is_file()}


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def resolve_jars() -> tuple[list[Path], dict[str, str], list[str]]:
    """Return the jar paths, the version chosen per group and what is missing."""
    jars, chosen, missing = [], {}, []
    for group, artifacts in GROUPS:
        common = set.intersection(*[_versions(group, a) for a in artifacts])
        if group == "org.springframework":
            common = {v for v in common if v.startswith("7.")}
        if not common:
            missing.append(f"{group}:{'/'.join(artifacts)}")
            continue
        v = max(common, key=_vkey)
        chosen[group] = v
        jars += [M2 / group.replace(".", "/") / a / v / f"{a}-{v}.jar" for a in artifacts]
    return jars, chosen, missing


def _tool(name: str) -> str | None:
    home = os.environ.get("JAVA21_HOME", "").strip()
    if home and (Path(home) / "bin" / name).is_file():
        return str(Path(home) / "bin" / name)
    return shutil.which(name)


# ---- generated input ------------------------------------------------------------------------------------

PREFIX = "/AB1/AB1C10"


def item_resource() -> Resource:
    """A mapper-only resource with select / count / insert / delete statements."""
    ns = make_layout().mapper_namespace("Item")

    def m(name, returns, call, ep, read_only=False):
        return Method(name, origin="mapper", kind="statement", returns=returns, db_backed=True, read_only=read_only,
                      namespace=ns, statement_id=name, session_call=call, endpoint=ep)

    return Resource("item", "Item", methods=[
        m("getList", "list", "selectList", Endpoint("GET", ""), True),
        m("countAll", "int", "selectOne", Endpoint("GET", "/count-all"), True),
        m("totalText", "int", "selectOne", None, True),
        m("save", "int", "insert", Endpoint("POST", "")),
        m("delete", "int", "delete", Endpoint("DELETE", "/{id}")),
    ])


MAPPER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="{ns}">
  <select id="getList" parameterType="map" resultType="java.util.Map"><![CDATA[ SELECT ITEM_ID FROM TB_ITEM ]]></select>
  <select id="countAll" parameterType="map" resultType="java.util.Map"><![CDATA[ SELECT COUNT(*) FROM TB_ITEM ]]></select>
  <select id="totalText" parameterType="map" resultType="java.util.Map"><![CDATA[
    SELECT CAST(COUNT(*) AS VARCHAR) AS N FROM TB_ITEM ]]></select>
  <insert id="save" parameterType="map"><![CDATA[ INSERT INTO TB_ITEM (ITEM_ID) VALUES (#{{ITEM_ID}}) ]]></insert>
  <delete id="delete" parameterType="map"><![CDATA[ DELETE FROM TB_ITEM WHERE ITEM_ID = #{{id}} ]]></delete>
</mapper>
"""

H2_DRIVER = """
import java.util.HashMap;
import java.util.Map;
import org.apache.ibatis.builder.xml.XMLMapperBuilder;
import org.apache.ibatis.datasource.unpooled.UnpooledDataSource;
import org.apache.ibatis.mapping.Environment;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.SqlSession;
import org.apache.ibatis.session.SqlSessionFactoryBuilder;
import org.apache.ibatis.transaction.jdbc.JdbcTransactionFactory;

public class H2Driver {
    public static void main(String[] a) throws Exception {
        UnpooledDataSource ds = new UnpooledDataSource("org.h2.Driver", "jdbc:h2:mem:items;DB_CLOSE_DELAY=-1", "sa", "");
        try (var c = ds.getConnection(); var st = c.createStatement()) {
            st.execute("CREATE TABLE TB_ITEM (ITEM_ID INT)");
            st.execute("INSERT INTO TB_ITEM VALUES (1), (2), (3)");
        }
        Configuration cfg = new Configuration(new Environment("t", new JdbcTransactionFactory(), ds));
        try (var in = new java.io.FileInputStream(a[0])) {
            new XMLMapperBuilder(in, cfg, a[0], cfg.getSqlFragments()).parse();
        }
        try (SqlSession ss = new SqlSessionFactoryBuilder().build(cfg).openSession(true)) {
            var store = new com.example.d1.shopProject.store.ItemMybatisStore();
            var field = store.getClass().getDeclaredField("sqlSession");
            field.setAccessible(true);
            field.set(store, ss);
            out("count", store.countAll(new HashMap<>()));
            out("text", store.totalText(new HashMap<>()));
            out("save", store.save(new HashMap<>(Map.of("ITEM_ID", 4))));
            out("count_after_save", store.countAll(new HashMap<>()));
            out("delete", store.delete(new HashMap<>(Map.of("id", "1"))));
            out("list", store.getList(new HashMap<>()).size());
        }
    }

    static void out(String name, Object value) {
        System.out.println("C\\t" + name + "\\t" + value);
    }
}
"""

MVC_DRIVER = """
import java.lang.reflect.Field;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.springframework.http.HttpMethod;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.request.MockMvcRequestBuilders;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.view.InternalResourceViewResolver;

public class MvcDriver {
    static final List<String> CALLS = new ArrayList<>();

    static Object fake(Class<?> type) {
        return Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, (p, m, a) -> {
            if (m.getDeclaringClass() == Object.class) {
                return switch (m.getName()) {
                    case "toString" -> "fake";
                    case "hashCode" -> 1;
                    case "equals" -> p == a[0];
                    default -> null;
                };
            }
            Map<String, Object> in = new TreeMap<>();
            if (a != null && a.length == 1 && a[0] instanceof Map<?, ?> given) {
                for (Map.Entry<?, ?> e : given.entrySet()) {
                    in.put(String.valueOf(e.getKey()), e.getValue());
                }
            }
            CALLS.add(m.getName() + in);
            if (m.getReturnType() == int.class) {
                return 7;
            }
            if (m.getReturnType() == List.class) {
                return List.of(in);
            }
            return in;
        });
    }

    static Object wire(Object controller, Class<?> service) throws Exception {
        Field f = controller.getClass().getDeclaredField("service");
        f.setAccessible(true);
        f.set(controller, fake(service));
        return controller;
    }

    public static void main(String[] args) throws Exception {
        InternalResourceViewResolver views = new InternalResourceViewResolver("/WEB-INF/static/shopProject/", ".jsp");
        MockMvc mvc = MockMvcBuilders.standaloneSetup(
                wire(new com.example.d1.shopProject.rest.ItemController(),
                     com.example.d1.shopProject.domain.spec.ItemService.class),
                wire(new com.example.d1.shopProject.rest.OrderController(),
                     com.example.d1.shopProject.domain.spec.OrderService.class),
                %PAGES%).setViewResolvers(views).build();
        String[][] reqs = {%REQUESTS%};
        for (String[] r : reqs) {
            MockHttpServletRequestBuilder b = MockMvcRequestBuilders.request(HttpMethod.valueOf(r[1]), r[2]);
            if (!r[3].isEmpty()) {
                b = b.contentType(r[3]);
            }
            if (!r[4].isEmpty()) {
                b = b.content(r[4]);
            }
            CALLS.clear();
            String status;
            String handler = "";
            String forward = "";
            String body;
            try {
                MvcResult res = mvc.perform(b).andReturn();
                status = String.valueOf(res.getResponse().getStatus());
                if (res.getHandler() instanceof HandlerMethod hm) {
                    handler = hm.getMethod().getName();
                }
                forward = String.valueOf(res.getResponse().getForwardedUrl());
                body = res.getResponse().getContentAsString();
            } catch (Exception e) {
                status = "EXC";
                body = String.valueOf(e);
            }
            System.out.println(String.join("\\t", "R", r[0], status, handler, forward,
                    body.replace('\\t', ' ').replace('\\n', ' '), String.join(";", CALLS)));
        }
    }
}
"""

# name, verb, url, content type, body
REQUESTS = [
    ("post_none", "POST", "/api/item?ITEM_ID=9", "", ""),
    ("post_form", "POST", "/api/item", "application/x-www-form-urlencoded", "ITEM_ID=9"),
    ("post_json", "POST", "/api/item?src=q", "application/json", '{"ITEM_ID": 9, "src": "body"}'),
    ("post_empty_form", "POST", "/api/item?ITEM_ID=9", "application/x-www-form-urlencoded", ""),
    ("get_list", "GET", "/api/item?q=a", "", ""),
    ("get_count", "GET", "/api/item/count-all", "", ""),
    ("delete", "DELETE", "/api/item/5?x=1", "", ""),
    ("router", "POST", "/api/order/find?a=1", "", ""),
    ("router_named", "GET", "/api/order/find?a=1&ResultName=list", "", ""),
    ("router_blank_name", "GET", "/api/order/find?a=1&ResultName=", "", ""),
    ("page_camel", "GET", f"{PREFIX}/orderList.mvc", "", ""),
    ("page_kebab", "GET", f"{PREFIX}/order-list.mvc", "", ""),
    ("page_upper", "GET", f"{PREFIX}/AB1C000010.mvc", "", ""),
    ("page_other_case", "GET", f"{PREFIX}/ORDERLIST.mvc", "", ""),
]


def _pages():
    api = FrontendApiMap(page_routes=["order-list", "ab1c000010"], mvc_url_prefix=PREFIX,
                         page_urls={"order-list": [f"{PREFIX}/orderList.mvc"], "ab1c000010": ["AB1C000010.mvc"]})
    return generate_page_controllers(api, ["springmvc/orderList.jsp", "springmvc/AB1C000010.jsp"], make_layout(),
                                     make_rules())


def _run(cmd: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    javac, java = _tool("javac"), _tool("java")
    if not javac or not java:
        pytest.skip("no javac / java (set JAVA21_HOME or put them on PATH)")
    jars, chosen, missing = resolve_jars()
    if missing:
        pytest.skip(f"jars not in {M2}: {', '.join(missing)}")
    work = tmp_path_factory.mktemp("spring")
    lay, rules = make_layout(), make_rules()
    files = []
    for res in (a6_resource(), stub_resource(), item_resource()):
        files += generate_resource(res, lay, rules)[0]
    pages = _pages()
    files += pages.files
    assert write_generated(work / "src", files) == []
    cp = os.pathsep.join(str(j) for j in jars)
    classes = work / "classes"
    srcs = sorted(str(p) for p in (work / "src").rglob("*.java"))
    gen = _run([javac, "--release", "21", "-encoding", "UTF-8", "-Xlint:all", "-Werror", "-cp", cp,
                "-d", str(classes), *srcs])
    drivers = work / "drivers"
    drivers.mkdir()
    page_new = ", ".join(f"new {lay.rest_pkg}.{f.path.rsplit('/', 1)[-1][:-5]}()" for f in pages.files)
    reqs = ", ".join("{" + ", ".join(java_string(x) for x in r) + "}" for r in REQUESTS)
    (drivers / "MvcDriver.java").write_text(MVC_DRIVER.replace("%PAGES%", page_new).replace("%REQUESTS%", reqs),
                                            encoding="utf-8")
    (drivers / "H2Driver.java").write_text(H2_DRIVER, encoding="utf-8")
    mapper = work / "item-mapper.xml"
    mapper.write_text(MAPPER.format(ns=lay.mapper_namespace("Item")), encoding="utf-8")
    drv = None
    if gen.returncode == 0:
        drv = _run([javac, "--release", "21", "-encoding", "UTF-8", "-cp", os.pathsep.join([str(classes), cp]),
                    "-d", str(classes), str(drivers / "MvcDriver.java"), str(drivers / "H2Driver.java")])
    return {"java": java, "cp": os.pathsep.join([str(classes), cp]), "gen": gen, "drv": drv, "files": files,
            "mapper": mapper, "chosen": chosen, "pages": pages}


def test_every_generated_class_compiles_without_warnings(built):
    gen = built["gen"]
    assert gen.returncode == 0, gen.stdout + gen.stderr
    assert gen.stderr.strip() == ""
    names = {f.path.rsplit("/", 1)[-1] for f in built["files"]}
    assert {"OrderMybatisStore.java", "NoticeSpringLogic.java", "ItemController.java",
            "OrderListPageController.java", "Ab1c000010PageController.java"} <= names
    print("jar versions:", built["chosen"])


@pytest.fixture(scope="module")
def mvc(built):
    assert built["gen"].returncode == 0 and built["drv"].returncode == 0, built["drv"].stdout + built["drv"].stderr
    proc = _run([built["java"], "-cp", built["cp"], "MvcDriver"])
    assert proc.returncode == 0, proc.stdout + proc.stderr[-4000:]
    rows = {}
    for line in proc.stdout.splitlines():
        if line.startswith("R\t"):
            _, name, status, handler, forward, body, calls = (line.split("\t") + [""] * 7)[:7]
            rows[name] = {"status": status, "handler": handler, "forward": forward, "body": body, "calls": calls}
    assert set(rows) == {r[0] for r in REQUESTS}, proc.stdout
    return rows


def _json(row):
    assert row["status"] == "200", row
    return json.loads(row["body"])


def test_each_post_shape_reaches_exactly_one_handler(mvc):
    assert mvc["post_none"]["handler"] == "saveJson"
    assert _json(mvc["post_none"]) == {"ok": True, "data": 7}
    assert mvc["post_none"]["calls"] == "save{ITEM_ID=9}"
    assert mvc["post_form"]["handler"] == "save"
    assert _json(mvc["post_form"]) == {"ok": True, "data": 7}
    assert mvc["post_form"]["calls"] == "save{ITEM_ID=9}"
    assert mvc["post_json"]["handler"] == "saveJson"
    assert _json(mvc["post_json"]) == {"ok": True, "data": 7}
    assert mvc["post_json"]["calls"] == "save{ITEM_ID=9, src=body}"   # the body wins over the parameters
    assert mvc["post_empty_form"]["handler"] == "save"
    assert mvc["post_empty_form"]["calls"] == "save{ITEM_ID=9}"


def test_get_and_delete_with_a_path_variable(mvc):
    assert _json(mvc["get_list"]) == {"ok": True, "data": [{"q": "a"}]}
    assert _json(mvc["get_count"]) == {"ok": True, "data": 7}
    assert mvc["get_count"]["calls"] == "countAll{}"
    assert _json(mvc["delete"]) == {"ok": True, "data": 7}
    assert mvc["delete"]["calls"] == "delete{id=5, x=1}"


def test_router_transition_with_and_without_result_name(mvc):
    assert mvc["router"]["body"] == '{"ok":true,"rows":[{"a":"1"}]}'
    assert mvc["router"]["calls"] == "getList{a=1}"
    named = _json(mvc["router_named"])
    assert list(named) == ["ok", "rows", "list"] and named["list"] == named["rows"]
    assert list(_json(mvc["router_blank_name"])) == ["ok", "rows"]


def test_page_controllers_answer_the_written_and_the_kebab_spelling(mvc):
    for name in ("page_camel", "page_kebab"):
        assert mvc[name]["status"] == "200"
        assert mvc[name]["forward"] == "/WEB-INF/static/shopProject/springmvc/orderList.jsp"
    assert mvc["page_upper"]["forward"] == "/WEB-INF/static/shopProject/springmvc/AB1C000010.jsp"
    assert mvc["page_other_case"]["status"] == "404"


def test_count_methods_against_h2(built):
    assert built["drv"] is not None and built["drv"].returncode == 0
    proc = _run([built["java"], "-cp", built["cp"], "H2Driver", str(built["mapper"])])
    assert proc.returncode == 0, proc.stdout + proc.stderr[-4000:]
    got = dict(line.split("\t")[1:3] for line in proc.stdout.splitlines() if line.startswith("C\t"))
    assert got == {"count": "3", "text": "3", "save": "1", "count_after_save": "4", "delete": "1", "list": "3"}
