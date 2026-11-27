"""Java generation for resources and page controllers (docs/FW-CONVERT.md 3.2 to 3.4, scenarios A6 and A10)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbmig.fwconvert.javagen import (
    PageResult,
    duplicate_mappings,
    generate_page_controllers,
    generate_resource,
    java_comment,
    java_string,
    write_generated,
)
from sbmig.fwconvert.model import (
    Endpoint,
    FrontendApiMap,
    GeneratedFile,
    Layout,
    Method,
    ModuleNames,
    ProjectIdentity,
    Resource,
    ServiceDef,
)
from sbmig.fwconvert.rules import load_rules

PKG = "com.example.d1.shopProject"
NS = f"{PKG}.store.OrderStore"


# ---- fixtures ---------------------------------------------------------------------------------------


def make_layout() -> Layout:
    ident = ProjectIdentity("shopProject", "d1", PKG,
                            ModuleNames("shopProject-boot", "shopProject-domain", "shopProject-service",
                                        "shopProject-store"))
    return Layout(ident)


def make_rules():
    return load_rules("Example", {"naming": {"package_root": "com.example", "domains": ["d1"]},
                                  "activities": {"kinds": {"ShopUpsert": "upsert"}}})


def a6_resource() -> Resource:
    """A router service with an upsert, a delete, a page search, a plain search and one non-DB method."""
    svc = ServiceDef(name="order-service", resource="order", x_name="Order", source="svc/order-service.xml",
                     has_router=True)
    methods = [
        Method("saveOrder", kind="upsert", returns="int", db_backed=True, activity="saveOrder", pass_hit="1b",
               namespace=NS, statement_id="saveOrder", session_call="update"),
        Method("removeOrder", kind="delete", returns="int", db_backed=True, activity="removeOrder", pass_hit="1b",
               namespace=NS, statement_id="removeOrder", session_call="delete"),
        Method("getPage", kind="pagesearch", returns="list", db_backed=True, read_only=True, pass_hit="1b",
               namespace=NS, statement_id="getPage", session_call="selectList"),
        Method("getList", kind="search", returns="list", db_backed=True, read_only=True, pass_hit="1a",
               namespace=NS, statement_id="getList", session_call="selectList"),
        Method("calcPrice", kind="other", returns="int", db_backed=False, pass_hit="java_only",
               evidence="ShopPriceCalc has no database access"),
    ]
    return Resource("order", "Order", methods=methods, service=svc, has_router=True,
                    transitions={"save": "saveOrder", "remove": "removeOrder", "page": "getPage",
                                 "find": "getList", "calc": "calcPrice"})


def mapper_resource() -> Resource:
    ns = f"{PKG}.store.OrderItemStore"

    def m(name, returns, call, ep, read_only=False):
        return Method(name, origin="mapper", kind="statement", returns=returns, db_backed=True, read_only=read_only,
                      namespace=ns, statement_id=name, session_call=call, endpoint=ep)

    return Resource("order-item", "OrderItem", methods=[
        m("getList", "list", "selectList", Endpoint("GET", ""), True),
        m("getSummary", "map", "selectOne", Endpoint("GET", "/summary"), True),
        m("countAll", "int", "selectOne", Endpoint("GET", "/count-all"), True),
        m("save", "int", "update", Endpoint("POST", "")),
        m("delete", "int", "delete", Endpoint("DELETE", "/{id}")),
        m("archiveOld", "int", "update", Endpoint("POST", "/archive-old")),
        m("lovCodes", "list", "selectList", None, True),
    ])


def stub_resource() -> Resource:
    """A10: a router service with nothing to call."""
    svc = ServiceDef(name="notice-service", resource="notice", x_name="Notice", source="svc/notice-service.xml",
                     has_router=True)
    return Resource("notice", "Notice", service=svc, has_router=True, stub=True,
                    methods=[Method("sendMail", kind="other", returns="list", pass_hit="extcall",
                                    evidence="class name has remotecall"),
                             Method("showForm", kind="other", returns="int", pass_hit="3",
                                    evidence="source not found")],
                    transitions={"send": "sendMail", "form": "showForm", "empty": ""})


def hostile_resource() -> Resource:
    evil = 'say "hi" \\ path C:\\new\\u000a */ end\nsecond line\r\tand \\u0022 \x01 \ud800'
    svc = ServiceDef(name="we\"ird-service", resource='we"ird', x_name="Weird", has_router=True)
    return Resource('we"ird', "Weird", service=svc, has_router=True, methods=[
        Method("load", kind="search", returns="list", db_backed=True, read_only=True,
               namespace='ns."x\\', statement_id='id"*/\\n', session_call="selectList"),
        Method("broken", kind="other", returns="map", pass_hit="java]_only\n*/", evidence=evil),
        Method("noStatement", kind="update", returns="int", db_backed=True, evidence=evil),
    ], transitions={"class": "load", "a-b-c": "broken", "a_b_c": "broken", "x\"/y": "noStatement",
                    "missing": "nope"})


# ---- structural checker -----------------------------------------------------------------------------

JAVA_LANG = {"String", "Object", "Integer", "Number", "Boolean", "Long", "Override", "SuppressWarnings",
             "UnsupportedOperationException", "RuntimeException", "NumberFormatException", "Math", "System", "Class"}


def strip_code(text: str) -> tuple[str, list[str]]:
    """Blank comments and string / char literals; report what would break javac's lexer."""
    out: list[str] = []
    problems: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            if re.search(r"(?<!\\)(?:\\\\)*\\u+[0-9a-fA-F]{4}", text[i:j]):
                problems.append(f"unicode escape inside a line comment: {text[i:j]!r}")
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                problems.append("unterminated block comment")
                break
            out.append(re.sub(r"[^\n]", " ", text[i:j + 2]))
            i = j + 2
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j >= n or text[j] != c:
                problems.append(f"unterminated literal at {i}: {text[i:i + 40]!r}")
                break
            lit = text[i:j + 1]
            if re.search(r"(?<!\\)(?:\\\\)*\\u+000[aAdD]", lit):
                problems.append(f"unicode line break inside a literal: {lit!r}")
            out.append(c + " " * (j - i - 1) + c)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), problems


def check_java(path: str, text: str, known_types: dict[str, str]) -> list[str]:
    problems: list[str] = []
    code, lex = strip_code(text)
    problems += lex
    if any(ord(ch) < 0x20 and ch not in "\n" for ch in text):
        problems.append("control character in the source")
    for o, c in ("{}", "()", "[]"):
        depth = 0
        for ch in code:
            depth += (ch == o) - (ch == c)
            if depth < 0:
                break
        if depth != 0:
            problems.append(f"unbalanced {o}{c}")
    m = re.search(r"^package\s+([\w.]+);", code, re.MULTILINE)
    if not m:
        return problems + ["no package declaration"]
    pkg = m.group(1)
    if "/src/main/java/" + pkg.replace(".", "/") + "/" not in path:
        problems.append(f"package {pkg} does not match the path {path}")
    depth, tops = 0, []
    for mm in re.finditer(r"[{}]|\b(class|interface|enum|record)\s+(\w+)", code):
        tok = mm.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        elif depth == 0:
            tops.append(mm.group(2))
    stem = path.rsplit("/", 1)[-1][:-len(".java")]
    if tops != [stem]:
        problems.append(f"top-level types {tops}, expected [{stem}]")
    imports = re.findall(r"^import\s+([\w.*]+);", code, re.MULTILINE)
    body = re.sub(r"^(package|import)\s+[\w.*]+;", "", code, flags=re.MULTILINE)
    simple: dict[str, str] = {}
    for imp in imports:
        if imp.endswith("*"):
            problems.append(f"wildcard import {imp}")
            continue
        if imp.startswith("javax."):
            problems.append(f"javax import {imp}")
        s = imp.rsplit(".", 1)[-1]
        if s in simple:
            problems.append(f"two imports of {s}")
        simple[s] = imp
        if not re.search(rf"(?<![\w.$]){re.escape(s)}\b", body):
            problems.append(f"unused import {imp}")
    declared = set(re.findall(r"\b(?:class|interface|enum|record)\s+(\w+)", body))
    for name in sorted(set(re.findall(r"(?<![\w.$])([A-Z]\w*)\b", body))):
        if name in simple or name in JAVA_LANG or name in declared or known_types.get(name) == pkg:
            continue
        problems.append(f"type {name} used but not imported")
    return problems


def check_files(files: list[GeneratedFile]) -> list[str]:
    known: dict[str, str] = {}
    for f in files:
        m = re.search(r"^package\s+([\w.]+);", f.text, re.MULTILINE)
        if m:
            known[f.path.rsplit("/", 1)[-1][:-5]] = m.group(1)
    out = []
    for f in files:
        out += [f"{f.path}: {p}" for p in check_java(f.path, f.text, known)]
    return out


def by_name(files: list[GeneratedFile]) -> dict[str, str]:
    return {f.path.rsplit("/", 1)[-1][:-5]: f.text for f in files}


def method_block(text: str, name: str) -> str:
    """The text of the first method declaration `name(` up to its closing brace."""
    start = re.search(rf"^\s*public [^\n]*\b{re.escape(name)}\(", text, re.MULTILINE)
    assert start, f"no method {name}"
    i = text.index("{", start.end())
    depth = 0
    for j in range(i, len(text)):
        depth += (text[j] == "{") - (text[j] == "}")
        if depth == 0:
            # include the annotations above the declaration
            head = text.rfind("\n\n", 0, start.start())
            return text[head + 2 if head >= 0 else 0:j + 1]
    raise AssertionError("unbalanced")


# ---- A6: router resource ----------------------------------------------------------------------------


def test_a6_file_set_and_packages():
    lay = make_layout()
    files, queue = generate_resource(a6_resource(), lay, make_rules())
    paths = [f.path for f in files]
    base = "src/main/java/com/example/d1/shopProject"
    assert paths == [
        f"shopProject-domain/{base}/domain/spec/OrderService.java",
        f"shopProject-domain/{base}/domain/store/OrderStore.java",
        f"shopProject-domain/{base}/domain/logic/OrderLogic.java",
        f"shopProject-store/{base}/store/OrderMybatisStore.java",
        f"shopProject-service/{base}/logic/OrderSpringLogic.java",
        f"shopProject-service/{base}/rest/OrderController.java",
    ]
    assert queue == []
    assert check_files(files) == []
    assert all(f.text.endswith("\n") and "\r" not in f.text and "\t" not in f.text for f in files)


def test_a6_interfaces_have_the_right_members():
    t = by_name(generate_resource(a6_resource(), make_layout(), make_rules())[0])
    spec = t["OrderService"]
    assert f"package {PKG}.domain.spec;" in spec and "public interface OrderService {" in spec
    members = re.findall(r"^    (.+);$", spec, re.MULTILINE)
    assert members == ["int saveOrder(Map<String, Object> params)", "int removeOrder(Map<String, Object> params)",
                       "List<Map<String, Object>> getPage(Map<String, Object> params)",
                       "List<Map<String, Object>> getList(Map<String, Object> params)",
                       "int calcPrice(Map<String, Object> params)"]
    store = t["OrderStore"]
    assert re.findall(r"^    \S.+ (\w+)\(", store, re.MULTILINE) == ["saveOrder", "removeOrder", "getPage", "getList"]
    assert "calcPrice" not in store


def test_a6_logic_bodies():
    logic = by_name(generate_resource(a6_resource(), make_layout(), make_rules())[0])["OrderLogic"]
    assert "import org.springframework" not in logic
    assert "public class OrderLogic implements OrderService {" in logic
    assert "public OrderLogic(OrderStore store) {" in logic
    up = method_block(logic, "saveOrder")
    assert 'params.get("rows")' in up and "instanceof List<?> list" in up and "return 0;" in up
    assert "for (Object row : list)" in up and "instanceof Map<?, ?> one" in up
    assert "total += store.saveOrder(asParams(one));" in up and up.rstrip().endswith("return total;\n    }")
    de = method_block(logic, "removeOrder")
    assert 'params.get("ids")' in de and 'one.put("id", item);' in de and "total += store.removeOrder(one);" in de
    assert de.count("return store.removeOrder(params);") == 1
    pg = method_block(logic, "getPage")
    assert 'query.putIfAbsent("page", 1);' in pg and 'query.putIfAbsent("rows", 10);' in pg
    assert "new LinkedHashMap<>(params)" in pg and "return store.getPage(query);" in pg
    assert "return store.getList(params);" in method_block(logic, "getList")
    calc = method_block(logic, "calcPrice")
    assert "// TODO[java_only] ShopPriceCalc has no database access" in calc
    assert 'throw new UnsupportedOperationException("calcPrice is not converted [java_only]: ' \
           'ShopPriceCalc has no database access");' in calc
    assert "store." not in calc
    assert "private static Map<String, Object> asParams(Map<?, ?> source)" in logic


def test_a6_mybatis_store_calls():
    t = by_name(generate_resource(a6_resource(), make_layout(), make_rules())[0])
    st = t["OrderMybatisStore"]
    assert "@Repository\npublic class OrderMybatisStore implements OrderStore {" in st
    assert "import org.apache.ibatis.session.SqlSession;" in st
    assert "    @Autowired\n    private SqlSession sqlSession;" in st
    assert f'return sqlSession.update("{NS}.saveOrder", params);' in st
    assert f'return sqlSession.delete("{NS}.removeOrder", params);' in st
    assert f'return sqlSession.selectList("{NS}.getPage", params);' in st
    assert f'return sqlSession.selectList("{NS}.getList", params);' in st
    assert "calcPrice" not in st


def test_a6_spring_logic_transactions():
    sl = by_name(generate_resource(a6_resource(), make_layout(), make_rules())[0])["OrderSpringLogic"]
    assert "@Service\npublic class OrderSpringLogic implements OrderService {" in sl
    assert "    @Autowired\n    private OrderStore store;" in sl
    assert "private static final Logger log = LoggerFactory.getLogger(OrderSpringLogic.class);" in sl
    for name, ro in [("saveOrder", False), ("removeOrder", False), ("getPage", True), ("getList", True),
                     ("calcPrice", False)]:
        block = method_block(sl, name)
        assert ("@Transactional(readOnly = true)" in block) is ro, name
        assert "@Transactional" in block
        assert f'log.debug("{name} params={{}}"' in block
        assert f"return new OrderLogic(store).{name}(params);" in block
    assert sl.count("readOnly = true") == 2


def test_a6_router_controller_mappings_and_answer():
    c = by_name(generate_resource(a6_resource(), make_layout(), make_rules())[0])["OrderController"]
    assert '@RestController\n@RequestMapping("/api/order")\npublic class OrderController {' in c
    assert "    @Autowired\n    private OrderService service;" in c
    maps = re.findall(r'@RequestMapping\(value = "(/[^"]*)", method = \{RequestMethod.GET, RequestMethod.POST\}\)\n'
                      r"    public Map<String, Object> (\w+)\(@RequestParam Map<String, Object> params\)", c)
    assert maps == [("/save", "save"), ("/remove", "remove"), ("/page", "page"), ("/find", "find"),
                    ("/calc", "calc")]
    assert "Object result = service.getList(params);" in method_block(c, "find")
    assert "Object result = service.saveOrder(params);" in method_block(c, "save")
    assert 'answer.put("ok", true);' in c and 'answer.put("rows", result);' in c
    assert 'params.get("ResultName")' in c and "isBlank()" in c
    assert "answer.put(String.valueOf(name), result);" in c
    assert c.index('answer.put("ok"') < c.index('answer.put("rows"') < c.index("answer.put(String.valueOf")


# ---- mapper-only resource -----------------------------------------------------------------------------


def test_mapper_controller_endpoints_and_binding():
    files, queue = generate_resource(mapper_resource(), make_layout(), make_rules())
    assert queue == [] and check_files(files) == []
    c = by_name(files)["OrderItemController"]
    assert '@RestController\n@RequestMapping("/api/order-item")\npublic class OrderItemController {' in c
    rows = re.findall(r"^    (@\w+Mapping(?:\([^\n]*\))?)\n    public Map<String, Object> (\w+)\(([^\n]*)\) \{", c, re.MULTILINE)
    rp = "@RequestParam Map<String, Object> params"
    FORM = "{MediaType.APPLICATION_FORM_URLENCODED_VALUE, MediaType.MULTIPART_FORM_DATA_VALUE}"
    body = "@RequestBody(required = false) Map<String, Object> body"
    assert rows == [
        ("@GetMapping", "getList", rp),
        ('@GetMapping("/summary")', "getSummary", rp),
        ('@GetMapping("/count-all")', "countAll", rp),
        (f"@PostMapping(consumes = {FORM})", "save", rp),
        ("@PostMapping(consumes = MediaType.APPLICATION_JSON_VALUE)", "saveJson", f"{rp}, {body}"),
        ('@DeleteMapping("/{id}")', "delete", f'@PathVariable("id") String id, {rp}'),
        (f'@PostMapping(value = "/archive-old", consumes = {FORM})', "archiveOld", rp),
        ('@PostMapping(value = "/archive-old", consumes = MediaType.APPLICATION_JSON_VALUE)', "archiveOldJson",
         f"{rp}, {body}"),
    ]
    assert "lovCodes" not in c
    dl = method_block(c, "delete")
    assert 'input.put("id", id);' in dl and "return ok(service.delete(input));" in dl
    js = method_block(c, "saveJson")
    assert "new LinkedHashMap<>(params)" in js and "input.putAll(body);" in js
    assert js.index("new LinkedHashMap<>(params)") < js.index("input.putAll(body)")
    ok = c[c.index("private static Map<String, Object> ok(Object data)"):]
    assert ok.index('answer.put("ok", true);') < ok.index('answer.put("data", data);')


def test_mapper_store_return_conversions():
    st = by_name(generate_resource(mapper_resource(), make_layout(), make_rules())[0])["OrderItemMybatisStore"]
    ns = f"{PKG}.store.OrderItemStore"
    cnt = method_block(st, "countAll")
    assert f'return toInt(sqlSession.selectOne("{ns}.countAll", params));' in cnt
    helper = method_block(st.replace("private static int toInt", "public static int toInt"), "toInt")
    assert "row.size() == 1" in helper and "instanceof Number number" in helper
    assert "new BigDecimal(text.trim()).intValue()" in helper and "catch (NumberFormatException e)" in helper
    assert "toInt" not in method_block(st, "getList")
    assert f'return sqlSession.selectOne("{ns}.getSummary", params);' in method_block(st, "getSummary")
    assert "public Map<String, Object> getSummary(" in st


def test_mapper_controller_address_comes_from_rest_base():
    res = mapper_resource()
    res.rest_base = "order-item-2"
    c = by_name(generate_resource(res, make_layout(), make_rules())[0])["OrderItemController"]
    assert '@RequestMapping("/api/order-item-2")' in c
    router = a6_resource()
    router.rest_base = "ignored"
    c = by_name(generate_resource(router, make_layout(), make_rules())[0])["OrderController"]
    assert '@RequestMapping("/api/order")' in c


def _two(first: str, second: str, base1: str = "", base2: str = ""):
    files = []
    for name, x, base in ((first, "A" + str(len(first)), base1), (second, "B" + str(len(second)), base2)):
        res = mapper_resource()
        res.resource, res.x_name, res.rest_base = name, x, base
        files += generate_resource(res, make_layout(), make_rules())[0]
    return files


def test_duplicate_mappings_finds_two_controllers_on_one_address():
    assert duplicate_mappings(generate_resource(mapper_resource(), make_layout(), make_rules())[0]) == []
    assert duplicate_mappings(generate_resource(a6_resource(), make_layout(), make_rules())[0]) == []
    lines = duplicate_mappings(_two("order-item", "order_item"))
    assert "GET /api/order-item: claimed by A10Controller.getList and B10Controller.getList" in lines
    assert "DELETE /api/order-item/{id}: claimed by A10Controller.delete and B10Controller.delete" in lines
    assert any(ln.startswith("POST /api/order-item [consumes MediaType.APPLICATION_JSON_VALUE]: claimed by")
               for ln in lines)
    assert len(lines) == 8                        # every handler of the second controller
    case = duplicate_mappings(_two("x", "y", "Order", "order"))
    assert "GET /api/Order and GET /api/order differ only in case: A1Controller.getList, B1Controller.getList" \
        in case
    assert all("differ only in case" in ln for ln in case) and len(case) == 8
    assert duplicate_mappings(_two("order-item", "order_item", "order-item", "order-item-2")) == []


def test_duplicate_mappings_reads_page_controllers():
    api = FrontendApiMap(page_routes=["a"], page_urls={"a": ["/x.mvc", "/We\"ird.mvc"]})
    one = generate_page_controllers(api, ["a.jsp"], make_layout(), make_rules()).files
    api2 = FrontendApiMap(page_routes=["b"], page_urls={"b": ["/x.mvc", "/we\"ird.mvc"]})
    two = generate_page_controllers(api2, ["b.jsp"], make_layout(), make_rules()).files
    assert duplicate_mappings(one) == []
    lines = duplicate_mappings(one + two)
    assert "GET /x.mvc: claimed by APageController.page and BPageController.page" in lines
    assert "POST /x.mvc: claimed by APageController.page and BPageController.page" in lines
    assert 'GET /We"ird.mvc and GET /we"ird.mvc differ only in case: APageController.page, BPageController.page' \
        in lines
    assert len(lines) == 4


def test_no_controller_without_router_and_endpoints():
    res = mapper_resource()
    for m in res.methods:
        m.endpoint = None
    names = by_name(generate_resource(res, make_layout(), make_rules())[0])
    assert "OrderItemController" not in names and "OrderItemMybatisStore" in names


def test_duplicate_endpoint_moves_then_is_reported():
    res = mapper_resource()
    res.methods = [Method("a", returns="list", db_backed=True, statement_id="a", session_call="selectList",
                          endpoint=Endpoint("GET", "")),
                   Method("c", returns="list", db_backed=True, statement_id="c", session_call="selectList",
                          endpoint=Endpoint("GET", "/b")),
                   Method("b", returns="list", db_backed=True, statement_id="b", session_call="selectList",
                          endpoint=Endpoint("GET", "")),
                   Method("d", returns="list", db_backed=True, statement_id="d", session_call="selectList",
                          endpoint=Endpoint("TRACE", ""))]
    files, queue = generate_resource(res, make_layout(), make_rules())
    c = by_name(files)["OrderItemController"]
    assert '@GetMapping("/b")\n    public Map<String, Object> c(' in c and " b(" not in c
    assert [q.reason.split("]")[0] + "]" for q in queue] == ["[ENDPOINT_DUPLICATE]", "[ENDPOINT_VERB]"]
    assert queue[0].component == "order-item::b"
    assert check_files(files) == []


# ---- A10: stub ----------------------------------------------------------------------------------------


def test_a10_stub_set_and_queue_item():
    files, queue = generate_resource(stub_resource(), make_layout(), make_rules())
    names = by_name(files)
    assert list(names) == ["NoticeService", "NoticeStore", "NoticeLogic", "NoticeSpringLogic", "NoticeController"]
    assert check_files(files) == []
    sl = names["NoticeSpringLogic"]
    assert "store" not in sl and "Transactional" not in sl and "@Service" in sl
    assert sl.count("throw new UnsupportedOperationException(") == 2
    assert re.search(r"^    public interface|\n    \S[^\n]*\(", names["NoticeStore"]) is None
    assert [(q.component, q.reason.split(" ")[0], q.source_path) for q in queue] == \
        [("notice-service::SpringLogic", "[STUB_SPRING_LOGIC]", "svc/notice-service.xml")]
    logic = names["NoticeLogic"]
    assert "// TODO[extcall] class name has remotecall" in logic and "// TODO[3] source not found" in logic
    ctl = names["NoticeController"]
    empty = method_block(ctl, "empty")
    assert "throw new UnsupportedOperationException(" in empty and "transition empty" in empty
    assert "service.sendMail(params)" in ctl


def test_empty_store_interface_still_gets_a_mybatis_store():
    res = a6_resource()
    for m in res.methods:
        m.db_backed = False
        m.pass_hit, m.evidence = "java_only", "no db"
    files, _ = generate_resource(res, make_layout(), make_rules())
    names = by_name(files)
    assert re.findall(r"\(Map", names["OrderStore"]) == []
    assert "import" not in names["OrderStore"]
    assert "private SqlSession sqlSession;" in names["OrderMybatisStore"]
    assert "private OrderStore store;" in names["OrderSpringLogic"]
    assert check_files(files) == []


def test_db_method_without_statement_throws_in_the_store():
    res = a6_resource()
    res.methods[3].statement_id = ""
    res.methods[3].evidence = "sql-key order.gone did not resolve"
    st = by_name(generate_resource(res, make_layout(), make_rules())[0])["OrderMybatisStore"]
    block = method_block(st, "getList")
    assert "sqlSession" not in block
    assert 'throw new UnsupportedOperationException("OrderStore.getList: no mapper statement was found ' \
           '(sql-key order.gone did not resolve)");' in block


def test_transition_without_a_method_throws():
    c = by_name(generate_resource(stub_resource(), make_layout(), make_rules())[0])["NoticeController"]
    assert '@RequestMapping(value = "/empty", method = {RequestMethod.GET, RequestMethod.POST})' in c


# ---- hostile strings ----------------------------------------------------------------------------------


def test_hostile_strings_stay_valid_java():
    files, _ = generate_resource(hostile_resource(), make_layout(), make_rules())
    assert check_files(files) == []
    names = by_name(files)
    c = names["WeirdController"]
    assert '@RequestMapping("/api/we\\"ird")' in c
    hnames = re.findall(r"public Map<String, Object> (\w+)\(@RequestParam", c)
    assert hnames == ["class_", "aBC", "a_b_c", "x__y", "missing"]
    assert "service.load(params)" in method_block(c, "class_")
    assert 'value = "/x\\"/y"' in c
    assert "throw new UnsupportedOperationException(" in method_block(c, "missing")
    logic = names["WeirdLogic"]
    todo = [ln for ln in logic.splitlines() if "// TODO[" in ln]
    assert len(todo) == 1 and "\\\\u000a" in todo[0] and "*/" in todo[0] and "second line" in todo[0]
    st = names["WeirdMybatisStore"]
    assert 'sqlSession.selectList("ns.\\"x\\\\.id\\"*/\\\\n", params)' in st
    assert "\\001" in method_block(logic, "broken") and "\\ud800" in method_block(logic, "broken")


def test_java_string_and_comment_helpers():
    assert java_string('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    assert java_string("\x00\x1f\x7f") == '"\\000\\037\\177"'
    assert java_string("한글") == '"한글"'
    assert java_comment("a\nb\r\nc  d") == "a b c d"
    assert java_comment("\\u000a") == "\\\\u000a"
    assert java_comment("x" * 400, limit=10).endswith(" ...")


def test_resource_name_that_clashes_with_an_annotation():
    res = mapper_resource()
    res.resource, res.x_name = "rest", "Rest"
    files, _ = generate_resource(res, make_layout(), make_rules())
    c = by_name(files)["RestController"]
    assert "@org.springframework.web.bind.annotation.RestController" in c
    assert "import org.springframework.web.bind.annotation.RestController;" not in c
    assert check_files(files) == []


def test_duplicate_method_names_are_dropped():
    res = a6_resource()
    res.methods.append(Method("getList", kind="search", returns="int", db_backed=True, statement_id="x",
                              session_call="selectOne"))
    spec = by_name(generate_resource(res, make_layout(), make_rules())[0])["OrderService"]
    assert spec.count(" getList(") == 1


# ---- page controllers ---------------------------------------------------------------------------------


JSPS = ["springmvc/ab1c000010.jsp", "springmvc/order-list.jsp", "legacy/old/order-list.jsp",
        "common/header.jsp", "include/menu.jsp", "springmvc/index.jsp", "springmvc/error.jsp",
        "springmvc/AB2C000020.jsp", "springmvc/style.css"]


def test_page_controllers_from_routes():
    api = FrontendApiMap(page_routes=["ab1c000010", "order-list", "missing-page", "header"],
                         mvc_url_prefix="/AB1/AB1C10/")
    res = generate_page_controllers(api, JSPS, make_layout(), make_rules())
    assert isinstance(res, PageResult) and res.url_prefix_used == "/AB1/AB1C10"
    names = by_name(res.files)
    assert sorted(names) == ["Ab1c000010PageController", "HeaderPageController", "OrderListPageController"]
    assert check_files(res.files) == []
    ab = names["Ab1c000010PageController"]
    assert "@Controller\npublic class Ab1c000010PageController {" in ab
    assert '@RequestMapping(value = "/AB1/AB1C10/ab1c000010.mvc", method = {RequestMethod.GET, RequestMethod.POST})' \
        in ab                                   # a single value without braces
    assert 'return "springmvc/ab1c000010";' in ab
    assert 'return "springmvc/order-list";' in names["OrderListPageController"]
    assert 'return "common/header";' in names["HeaderPageController"]   # routes mode excludes nothing
    assert "missing-page: no JSP" in res.skipped
    assert "legacy/old/order-list.jsp: same name as springmvc/order-list.jsp" in res.skipped
    assert res.files[0].path.startswith("shopProject-service/src/main/java/com/example/d1/shopProject/rest/")


def test_page_controllers_from_jsps_with_exclusions():
    res = generate_page_controllers(FrontendApiMap(), JSPS, make_layout(), make_rules())
    names = by_name(res.files)
    assert sorted(names) == ["Ab1c000010PageController", "Ab2c000020PageController", "OrderListPageController"]
    assert res.url_prefix_used == ""
    assert 'value = {"/AB2C000020.mvc", "/ab2c000020.mvc"}, method' in names["Ab2c000020PageController"]
    assert 'return "springmvc/AB2C000020";' in names["Ab2c000020PageController"]
    assert res.skipped == ["legacy/old/order-list.jsp: same name as springmvc/order-list.jsp"]
    assert sorted(res.created) == ["/AB2C000020.mvc", "/ab1c000010.mvc", "/ab2c000020.mvc", "/order-list.mvc"]
    assert check_files(res.files) == []


def test_page_controller_class_names_are_numbered():
    api = FrontendApiMap(page_routes=["order-list", "order_list", "orderList"])
    res = generate_page_controllers(api, ["a/order-list.jsp", "b/order_list.jsp", "c/orderList.jsp"],
                                    make_layout(), make_rules(), reserved=["OrderListPageController"])
    assert sorted(by_name(res.files)) == ["OrderListPageController2"]
    api = FrontendApiMap(page_routes=["a-b", "ab"])
    res = generate_page_controllers(api, ["x/a-b.jsp", "y/ab.jsp"], make_layout(), make_rules())
    assert sorted(by_name(res.files)) == ["ABPageController", "AbPageController2"]   # case is ignored
    api = FrontendApiMap(page_routes=["a_b", "a-b"])
    res = generate_page_controllers(api, ["x/a_b.jsp"], make_layout(), make_rules())
    assert len(res.files) == 1


def test_page_controllers_serve_the_spellings_the_jsps_use():
    api = FrontendApiMap(page_routes=["order-list", "ab1c000010"], mvc_url_prefix="/AB1/AB1C10",
                         page_urls={"order-list": ["/AB1/AB1C10/orderList.mvc", "orderList.mvc",
                                                   "/AB1/AB1C10/order-list.mvc"],
                                    "ab1c000010": ["/AB1/AB1C10/AB1C000010.mvc", "/OTHER/AB1C000010.mvc?x=1"]})
    res = generate_page_controllers(api, ["p/orderList.jsp", "p/AB1C000010.jsp"], make_layout(), make_rules())
    names = by_name(res.files)
    assert check_files(res.files) == []
    assert ('@RequestMapping(value = {"/AB1/AB1C10/order-list.mvc", "/AB1/AB1C10/orderList.mvc"}, '
            'method = {RequestMethod.GET, RequestMethod.POST})') in names["OrderListPageController"]
    assert ('value = {"/AB1/AB1C10/AB1C000010.mvc", "/AB1/AB1C10/ab1c000010.mvc", "/OTHER/AB1C000010.mvc"}'
            in names["Ab1c000010PageController"])
    assert sorted(res.created) == ["/AB1/AB1C10/AB1C000010.mvc", "/AB1/AB1C10/ab1c000010.mvc",
                                   "/AB1/AB1C10/order-list.mvc", "/AB1/AB1C10/orderList.mvc",
                                   "/OTHER/AB1C000010.mvc"]


def test_two_page_controllers_never_claim_one_url():
    api = FrontendApiMap(page_routes=["a", "b"], page_urls={"a": ["/x.mvc"], "b": ["/x.mvc"]})
    res = generate_page_controllers(api, ["a.jsp", "b.jsp"], make_layout(), make_rules())
    names = by_name(res.files)
    assert '"/x.mvc"' in names["APageController"] and '"/x.mvc"' not in names["BPageController"]
    assert "/x.mvc: already served by APageController, left out of BPageController" in res.skipped
    assert res.created.count("/x.mvc") == 1
    api = FrontendApiMap(page_routes=["a", "b"], page_urls={"b": ["/a.mvc"]})
    res = generate_page_controllers(api, ["a.jsp", "b.jsp"], make_layout(), make_rules())
    assert 'value = {"/a.mvc", "/b.mvc"}' not in by_name(res.files)["BPageController"]


def test_jsp_with_a_non_lower_case_extension_gets_no_controller():
    res = generate_page_controllers(FrontendApiMap(page_routes=["menu"]), ["p/MENU.JSP"], make_layout(), make_rules())
    assert res.files == []
    assert res.skipped == ["p/MENU.JSP: the extension is not .jsp, the view resolver would not find it",
                           "menu: no JSP"]
    res = generate_page_controllers(FrontendApiMap(), ["p/MENU.JSP", "p/menu.jsp"], make_layout(), make_rules())
    assert [f.path.rsplit("/", 1)[-1] for f in res.files] == ["MenuPageController.java"]
    assert 'return "p/menu";' in res.files[0].text


def test_relative_page_links_resolve_against_the_prefix():
    api = FrontendApiMap(page_routes=["order-detail"], mvc_url_prefix="/SH1/SH1A10",
                         page_urls={"order-detail": ["../SH1A20/order-detail.mvc", "sub/orderDetail.mvc",
                                                     "./orderDetail.mvc", "../../../up.mvc", "/ABS/x.mvc",
                                                     "../", "sub/.."]})
    res = generate_page_controllers(api, ["shop/order-detail.jsp"], make_layout(), make_rules())
    # a ".." at the root stays at the root, as a browser resolves it (RFC 3986 5.2.4)
    assert sorted(res.created) == ["/ABS/x.mvc", "/SH1/SH1A10/order-detail.mvc", "/SH1/SH1A10/orderDetail.mvc",
                                   "/SH1/SH1A10/sub/orderDetail.mvc", "/SH1/SH1A20/order-detail.mvc", "/up.mvc"]
    assert res.skipped == ["../: the link of order-detail has no page name, not served",
                           "sub/..: the link of order-detail has no page name, not served"]


def test_relative_link_above_an_empty_prefix_is_clamped_at_the_root():
    api = FrontendApiMap(page_routes=["order-detail"],
                         page_urls={"order-detail": ["../SH1A20/order-detail.mvc", "x/a.mvc"]})
    res = generate_page_controllers(api, ["order-detail.jsp"], make_layout(), make_rules())
    assert sorted(res.created) == ["/SH1A20/order-detail.mvc", "/order-detail.mvc", "/x/a.mvc"]
    assert res.skipped == []


def test_page_controller_escapes_view_names():
    res = generate_page_controllers(FrontendApiMap(), ['p/we"ird.jsp', 'q\\r.jsp'], make_layout(), make_rules())
    assert check_files(res.files) == []
    texts = by_name(res.files)
    assert 'return "p/we\\"ird";' in texts["WeirdPageController"]
    assert 'return "q/r";' in texts["RPageController"]   # a backslash is a folder separator


# ---- write_generated ----------------------------------------------------------------------------------


def test_write_generated_writes_and_refuses_escapes(tmp_path: Path):
    out = tmp_path / "out"
    files = [GeneratedFile("a/b/C.java", "x\r\ny\n"), GeneratedFile("../x", "evil"),
             GeneratedFile("/abs/y", "evil"), GeneratedFile("a/./d", "evil"), GeneratedFile("ok.txt", "한글\n"),
             GeneratedFile("ok.txt", "other\n"), GeneratedFile("s\ud800.txt", "z")]
    errors = write_generated(out, files)
    assert (out / "a/b/C.java").read_bytes() == b"x\ny\n"
    assert (out / "ok.txt").read_bytes() == "한글\n".encode()
    assert not (tmp_path / "x").exists()
    assert len([e for e in errors if e.startswith("../x:")]) == 1
    assert any(e.startswith("/abs/y:") for e in errors) and any(e.startswith("a/./d:") for e in errors)
    assert any(e.startswith("ok.txt:") for e in errors)
    assert len(errors) == 5


def test_write_generated_refuses_paths_that_differ_only_in_case(tmp_path: Path):
    out = tmp_path / "out"
    files = [GeneratedFile("m/OrderItemService.java", "a\n"), GeneratedFile("m/OrderitemService.java", "b\n"),
             GeneratedFile("m/same.txt", "x\n"), GeneratedFile("m/same.txt", "x\n"),
             GeneratedFile("M/Same.txt", "x\n")]
    errors = write_generated(out, files)
    # sorted by path: "M/Same.txt" comes first and is written, the lower-case spellings are refused
    assert sorted(e.split(":")[0] for e in errors) == ["m/OrderitemService.java", "m/same.txt"]
    assert all("differs only in case" in e for e in errors)
    assert (out / "M/Same.txt").read_text() == "x\n"
    assert (out / "m/OrderItemService.java").read_text() == "a\n"


def test_reserved_page_names_are_compared_ignoring_case():
    res = generate_page_controllers(FrontendApiMap(page_routes=["order"]), ["order.jsp"], make_layout(),
                                    make_rules(), reserved=["ORDERPageController"])
    assert [f.path.rsplit("/", 1)[-1] for f in res.files] == ["OrderPageController2.java"]


def test_write_generated_refuses_a_symlink_out(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (out / "link").symlink_to(tmp_path / "elsewhere")
    errors = write_generated(out, [GeneratedFile("link/x.java", "x")])
    assert errors and not (tmp_path / "elsewhere" / "x.java").exists()


@pytest.mark.parametrize("res_fn", [a6_resource, mapper_resource, stub_resource, hostile_resource])
def test_generation_is_deterministic(res_fn):
    a = generate_resource(res_fn(), make_layout(), make_rules())
    b = generate_resource(res_fn(), make_layout(), make_rules())
    assert [(f.path, f.text) for f in a[0]] == [(f.path, f.text) for f in b[0]]
