"""리소스 레코드로 인터페이스, 구현 클래스, 컨트롤러, 화면 컨트롤러의 자바 소스를 만드는 모듈.

Contract: docs/FW-CONVERT.md sections 3.2 to 3.4 and 4, scenarios A6 and A10. The generators return
``GeneratedFile`` records and write nothing; ``write_generated`` is the only function that touches the disk.

Rules decided here where the specification is silent:
- Every string that comes from the project goes through ``java_string`` (string literals) or ``java_comment``
  (line comments). Backslashes are doubled in both, so a ``\\u000a`` in the project text can never become a line
  break when javac translates unicode escapes; control characters become octal escapes in literals and spaces in
  comments; lone surrogates become ``\\uXXXX`` in literals and ``?`` in comments.
- Project text is never put into a ``/* */`` or Javadoc comment, only into ``//`` comments.
- Imports are collected from use (``_Imports.use``), so no import is unused. The file's own type claims its simple
  name first and the project types it references are used before any library type, so a library type whose simple
  name is already taken (``RestController`` for a resource named ``rest``) is written fully qualified.
- Method names go through ``naming.java_safe``; a second method with the same name is dropped (it could not
  compile). Helper methods of a class get a name no generated method has.
- Logic loops (upsert, delete) are written only when the method returns ``int``; otherwise the plain delegation.
- A ``selectList`` call for an ``int`` method returns the list size; an unknown call name falls back to the natural call
  of the return type (list ``selectList``, map ``selectOne``, int ``update``).
- POST / PUT / PATCH endpoints get two handlers for the same path, so that exactly one serves each request (a
  ``@RequestBody`` handler answers a form post with 415, and Spring skips ``consumes`` for a request without a body
  when the body is optional): the form handler ``consumes`` form-urlencoded and multipart and binds the parameters;
  the JSON handler ``consumes`` JSON, takes an optional body and merges it over the parameters. A POST without a
  body and without a content type reaches the JSON handler only; an empty POST that declares the form type matches
  both and Spring prefers the form handler (a real ``consumes`` match beats the empty match). Proved under MockMvc
  by ``tests/fwconvert/test_javagen_spring.py``.
- An ``int`` method on ``selectOne`` converts the result with a ``toInt`` helper: a number; a one-entry map (every
  mapper select has ``resultType="java.util.Map"``) whose value is a number or a numeric string; else 0.
- Mapper endpoints that land on the same verb and path a second time get ``/{kebab(method)}``; still taken: no
  handler and a ``[ENDPOINT_DUPLICATE]`` hand-over item. A verb outside GET / POST / PUT / PATCH / DELETE is a
  ``[ENDPOINT_VERB]`` item.
- Router answers: ``ResultName`` equal to ``ok`` is ignored (it would overwrite the flag); ``rows`` gets the same
  value anyway. A transition whose method name is not among the resource's methods is treated like ``""``.
- Page routes: a route is matched to a JSP stem by ``kebab`` and, failing that, by the stem with every ``-``, ``_``,
  ``.`` and space removed, case ignored. A name without lower-case letters (``AB1C000010``) is lower-cased before
  ``kebab`` so that it is not split at the digits. "Shortest path" = fewest folders, then fewest characters, then
  the path's own order. ``reserved`` names class names the caller already uses in the rest package; it and the
  numbered page-controller names are compared ignoring case (a case-insensitive file system would merge them).
- Mapper controllers are mapped at ``{api prefix}/{Resource.rest_base}`` (``kebab(resource)`` when it is empty);
  router controllers at ``{api prefix}/{resource as written}``. ``duplicate_mappings`` reads the generated
  controllers back and names every (verb, path, consumes) two handlers claim, exactly or ignoring case.
- A relative page link is resolved like a browser on a page at ``{prefix}/page`` (RFC 3986 5.2.4: a ``..`` at
  the root stays at the root); a link that is empty or ends in a folder (no page name) is named in ``skipped``.
- A page controller serves the URLs the JSPs wrote (``FrontendApiMap.page_urls``) and the kebab form; a JSP whose
  extension is not exactly ``.jsp`` gets none (the view name always gets ``.jsp`` appended).
- ``write_generated`` refuses a path equal to an earlier one ignoring case, so two classes whose names differ only
  in case never overwrite each other on macOS / Windows."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sbmig.fwconvert.model import Endpoint, FrontendApiMap, GeneratedFile, Layout, Method, QueueItem, Resource
from sbmig.fwconvert.naming import java_safe, kebab, x_name
from sbmig.fwconvert.rules import ConvertRules

INDENT = "    "

# 자주 쓰는 타입의 전체 이름
_MAP = "java.util.Map"
_LIST = "java.util.List"
_LINKED_MAP = "java.util.LinkedHashMap"
_SQL_SESSION = "org.apache.ibatis.session.SqlSession"
_AUTOWIRED = "org.springframework.beans.factory.annotation.Autowired"
_REPOSITORY = "org.springframework.stereotype.Repository"
_SERVICE = "org.springframework.stereotype.Service"
_CONTROLLER = "org.springframework.stereotype.Controller"
_TRANSACTIONAL = "org.springframework.transaction.annotation.Transactional"
_LOGGER = "org.slf4j.Logger"
_LOGGER_FACTORY = "org.slf4j.LoggerFactory"
_REST_CONTROLLER = "org.springframework.web.bind.annotation.RestController"
_REQUEST_MAPPING = "org.springframework.web.bind.annotation.RequestMapping"
_REQUEST_METHOD = "org.springframework.web.bind.annotation.RequestMethod"
_REQUEST_PARAM = "org.springframework.web.bind.annotation.RequestParam"
_REQUEST_BODY = "org.springframework.web.bind.annotation.RequestBody"
_PATH_VARIABLE = "org.springframework.web.bind.annotation.PathVariable"
_MEDIA_TYPE = "org.springframework.http.MediaType"
_VERB_ANNOTATIONS = {
    "GET": "org.springframework.web.bind.annotation.GetMapping",
    "POST": "org.springframework.web.bind.annotation.PostMapping",
    "PUT": "org.springframework.web.bind.annotation.PutMapping",
    "PATCH": "org.springframework.web.bind.annotation.PatchMapping",
    "DELETE": "org.springframework.web.bind.annotation.DeleteMapping",
}
# 본문을 받는 메서드. JSON 본문용 핸들러를 하나 더 만든다
_BODY_VERBS = {"POST", "PUT", "PATCH"}
_SESSION_CALLS = {"selectList", "selectOne", "insert", "update", "delete"}
_PATH_VAR = re.compile(r"\{([^{}/:]+)(?::[^{}]*)?\}")


# ---- 자바 문자열과 주석 ----------------------------------------------------------


def java_string(text: str) -> str:
    """문자열을 자바 문자열 리터럴로 만들어 반환한다. 따옴표까지 붙인다.
    역슬래시, 따옴표, 줄바꿈, 제어 문자는 이스케이프한다."""
    out: list[str] = []
    for ch in str(text if text is not None else ""):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\{code:03o}")
        elif 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def java_comment(text: str, limit: int = 300) -> str:
    """문자열을 ``//`` 주석 한 줄에 넣을 수 있게 고쳐 반환한다.
    줄바꿈과 제어 문자는 공백으로 바꾸고 역슬래시는 두 번 쓴다. ``limit``자보다 길면 자른다."""
    s = str(text if text is not None else "")
    s = "".join(" " if (ord(c) < 0x20 or ord(c) == 0x7F or c in "  \x85")
                else ("?" if 0xD800 <= ord(c) <= 0xDFFF else c) for c in s)
    s = re.sub(r" {2,}", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rstrip() + " ..."
    return s.replace("\\", "\\\\")


# ---- import 모으기 ----------------------------------------------------------------


class _Imports:
    def __init__(self, package: str, own: str) -> None:
        self.package = package
        # 파일 자신의 타입 이름을 먼저 잡아 둔다. 같은 이름의 다른 타입은 전체 이름으로 쓴다
        self.by_simple: dict[str, str] = {own: f"{package}.{own}"}

    def use(self, fqcn: str) -> str:
        simple = fqcn.rpartition(".")[2]
        have = self.by_simple.get(simple)
        if have is None:
            self.by_simple[simple] = fqcn
            return simple
        return simple if have == fqcn else fqcn

    def lines(self) -> list[str]:
        out = []
        for fqcn in self.by_simple.values():
            if fqcn.rpartition(".")[0] in ("java.lang", self.package):
                continue
            out.append(f"import {fqcn};")
        return sorted(out)


def _source(package: str, imports: _Imports, body: list[str]) -> str:
    head = [f"package {package};", ""]
    imp = imports.lines()
    if imp:
        head += imp + [""]
    return "\n".join(head + body).rstrip("\n") + "\n"


def _fresh(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    taken.add(name)
    return name


# ---- 메서드 시그니처 ----------------------------------------------------------------


def _type_x(res: Resource) -> str:
    x = re.sub(r"[^A-Za-z0-9_$]", "", str(res.x_name or ""))
    if not x or x[0].isdigit():
        x = x_name(res.resource) or ""
    return x or "Resource"


def _methods(res: Resource) -> list[tuple[str, Method]]:
    """리소스의 메서드를 (자바 이름, 레코드) 목록으로 반환한다. 이름이 같은 두 번째 메서드는 뺀다."""
    seen: set[str] = set()
    out: list[tuple[str, Method]] = []
    for m in res.methods:
        name = java_safe(m.name)
        if name in seen:
            continue
        seen.add(name)
        out.append((name, m))
    return out


def _ret(m: Method, imp: _Imports) -> str:
    mp = imp.use(_MAP)
    if m.returns == "int":
        return "int"
    if m.returns == "map":
        return f"{mp}<String, Object>"
    return f"{imp.use(_LIST)}<{mp}<String, Object>>"


def _param(imp: _Imports) -> str:
    return f"{imp.use(_MAP)}<String, Object> params"


def _reason(m: Method) -> tuple[str, str]:
    hit = m.pass_hit or "unknown"
    evidence = m.evidence or "the activity does not use the database"
    return hit, evidence


# ---- 인터페이스 --------------------------------------------------------------------


def _interface(package: str, name: str, methods: list[tuple[str, Method]]) -> str:
    imp = _Imports(package, name)
    body = [f"public interface {name} {{"]
    for i, (mname, m) in enumerate(methods):
        if i:
            body.append("")
        body.append(f"{INDENT}{_ret(m, imp)} {mname}({_param(imp)});")
    body.append("}")
    return _source(package, imp, body)


# ---- 순수 자바 Logic ----------------------------------------------------------------


def _logic(res: Resource, layout: Layout, x: str, methods: list[tuple[str, Method]]) -> str:
    name, store, spec = f"{x}Logic", f"{x}Store", f"{x}Service"
    imp = _Imports(layout.logic_pkg, name)
    store_t = imp.use(f"{layout.store_pkg}.{store}")
    spec_t = imp.use(f"{layout.spec_pkg}.{spec}")
    taken = {n for n, _ in methods}
    need_params_helper = False
    helper = _fresh("asParams", taken)
    members: list[list[str]] = [[f"{INDENT}private final {store_t} store;"],
                                [f"{INDENT}public {name}({store_t} store) {{",
                                 f"{INDENT * 2}this.store = store;", f"{INDENT}}}"]]
    mp = imp.use(_MAP)
    for mname, m in methods:
        lines = [f"{INDENT}@Override", f"{INDENT}public {_ret(m, imp)} {mname}({_param(imp)}) {{"]
        i2, i3, i4 = INDENT * 2, INDENT * 3, INDENT * 4
        if not m.db_backed:
            hit, evidence = _reason(m)
            lines += [f"{i2}// TODO[{java_comment(hit, 40)}] {java_comment(evidence)}",
                      f"{i2}throw new UnsupportedOperationException("
                      f"{java_string(f'{mname} is not converted [{hit}]: {evidence}')});"]
        elif m.kind == "upsert" and m.returns == "int":
            need_params_helper = True
            lst = imp.use(_LIST)
            lines += [f'{i2}Object rows = params == null ? null : params.get("rows");',
                      f"{i2}if (!(rows instanceof {lst}<?> list)) {{", f"{i3}return 0;", f"{i2}}}",
                      f"{i2}int total = 0;", f"{i2}for (Object row : list) {{",
                      f"{i3}if (row instanceof {mp}<?, ?> one) {{",
                      f"{i4}total += store.{mname}({helper}(one));", f"{i3}}}", f"{i2}}}",
                      f"{i2}return total;"]
        elif m.kind == "delete" and m.returns == "int":
            lst, lm = imp.use(_LIST), imp.use(_LINKED_MAP)
            lines += [f'{i2}Object ids = params == null ? null : params.get("ids");',
                      f"{i2}if (ids instanceof {lst}<?> list) {{", f"{i3}int total = 0;",
                      f"{i3}for (Object item : list) {{",
                      f"{i4}{mp}<String, Object> one = new {lm}<>();",
                      f'{i4}one.put("id", item);', f"{i4}total += store.{mname}(one);", f"{i3}}}",
                      f"{i3}return total;", f"{i2}}}", f"{i2}return store.{mname}(params);"]
        elif m.kind == "pagesearch":
            lm = imp.use(_LINKED_MAP)
            lines += [f"{i2}{mp}<String, Object> query = params == null ? new {lm}<>() : new {lm}<>(params);",
                      f'{i2}query.putIfAbsent("page", 1);', f'{i2}query.putIfAbsent("rows", 10);',
                      f"{i2}return store.{mname}(query);"]
        else:
            lines += [f"{i2}return store.{mname}(params);"]
        lines.append(f"{INDENT}}}")
        members.append(lines)
    if need_params_helper:
        lm = imp.use(_LINKED_MAP)
        members.append([f"{INDENT}private static {mp}<String, Object> {helper}({mp}<?, ?> source) {{",
                        f"{INDENT * 2}{mp}<String, Object> out = new {lm}<>();",
                        f"{INDENT * 2}for ({mp}.Entry<?, ?> e : source.entrySet()) {{",
                        f"{INDENT * 3}out.put(String.valueOf(e.getKey()), e.getValue());",
                        f"{INDENT * 2}}}", f"{INDENT * 2}return out;", f"{INDENT}}}"])
    return _source(layout.logic_pkg, imp, _class(f"public class {name} implements {spec_t}", members))


def _class(decl: str, members: list[list[str]], annotations: list[str] | None = None) -> list[str]:
    body = list(annotations or []) + [decl + " {", ""]
    for i, mem in enumerate(members):
        if i:
            body.append("")
        body += mem
    body.append("}")
    return body


# ---- MyBatis 저장소 -----------------------------------------------------------------


def _store_call(m: Method, imp: _Imports, ns: str, to_int: str) -> list[str]:
    i2 = INDENT * 2
    key = java_string(f"{ns}.{m.statement_id}")
    call = m.session_call if m.session_call in _SESSION_CALLS else ""
    if m.returns == "int":
        if call == "selectOne":
            return [f"{i2}return {to_int}(sqlSession.selectOne({key}, params));"]
        if call == "selectList":
            return [f"{i2}return sqlSession.selectList({key}, params).size();"]
        return [f"{i2}return sqlSession.{call or 'update'}({key}, params);"]
    if m.returns == "map":
        if call == "selectList":
            mp, lst = imp.use(_MAP), imp.use(_LIST)
            return [f"{i2}{lst}<{mp}<String, Object>> rows = sqlSession.selectList({key}, params);",
                    f"{i2}return rows.isEmpty() ? null : rows.get(0);"]
        return [f"{i2}return sqlSession.selectOne({key}, params);"]
    return [f"{i2}return sqlSession.selectList({key}, params);"]


def _to_int_helper(imp: _Imports, helper: str) -> list[str]:
    """``selectOne`` 결과를 int로 바꾸는 도우미 메서드의 줄을 반환한다.
    mapper의 select는 ``java.util.Map``을 돌려주므로 값이 하나뿐인 행이면 그 값을 쓴다."""
    i2, i3, i4 = INDENT * 2, INDENT * 3, INDENT * 4
    mp, bd = imp.use(_MAP), imp.use("java.math.BigDecimal")
    return [f"{INDENT}private static int {helper}(Object value) {{",
            f"{i2}Object one = value;",
            f"{i2}if (value instanceof {mp}<?, ?> row && row.size() == 1) {{",
            f"{i3}one = row.values().iterator().next();",
            f"{i3}if (one instanceof String text) {{",
            f"{i4}try {{",
            f"{i4}{INDENT}return new {bd}(text.trim()).intValue();",
            f"{i4}}} catch (NumberFormatException e) {{",
            f"{i4}{INDENT}return 0;",
            f"{i4}}}",
            f"{i3}}}",
            f"{i2}}}",
            f"{i2}return one instanceof Number number ? number.intValue() : 0;",
            f"{INDENT}}}"]


def _mybatis_store(res: Resource, layout: Layout, x: str, methods: list[tuple[str, Method]]) -> str:
    name, store = f"{x}MybatisStore", f"{x}Store"
    imp = _Imports(layout.mybatis_store_pkg, name)
    store_t = imp.use(f"{layout.store_pkg}.{store}")
    members: list[list[str]] = [[f"{INDENT}@{imp.use(_AUTOWIRED)}",
                                 f"{INDENT}private {imp.use(_SQL_SESSION)} sqlSession;"]]
    default_ns = layout.mapper_namespace(x)
    to_int = _fresh("toInt", {n for n, _ in methods})
    need_to_int = False
    for mname, m in methods:
        lines = [f"{INDENT}@Override", f"{INDENT}public {_ret(m, imp)} {mname}({_param(imp)}) {{"]
        if m.statement_id:
            need_to_int |= m.returns == "int" and m.session_call == "selectOne"
            lines += _store_call(m, imp, m.namespace or default_ns, to_int)
        else:
            why = f"{x}Store.{mname}: no mapper statement was found"
            if m.evidence:
                why += f" ({m.evidence})"
            lines += [f"{INDENT * 2}// TODO[no-statement] {java_comment(why)}",
                      f"{INDENT * 2}throw new UnsupportedOperationException({java_string(why)});"]
        lines.append(f"{INDENT}}}")
        members.append(lines)
    if need_to_int:
        members.append(_to_int_helper(imp, to_int))
    return _source(layout.mybatis_store_pkg, imp,
                   _class(f"public class {name} implements {store_t}", members, [f"@{imp.use(_REPOSITORY)}"]))


# ---- Spring 서비스 -------------------------------------------------------------------


def _spring_logic(res: Resource, layout: Layout, x: str, methods: list[tuple[str, Method]]) -> str:
    name, spec, store, logic = f"{x}SpringLogic", f"{x}Service", f"{x}Store", f"{x}Logic"
    imp = _Imports(layout.spring_logic_pkg, name)
    spec_t = imp.use(f"{layout.spec_pkg}.{spec}")
    members: list[list[str]] = []
    if res.stub:
        for mname, m in methods:
            why = f"{x}SpringLogic.{mname}: the service has no SQL statement to call (stub)"
            members.append([f"{INDENT}@Override",
                            f"{INDENT}public {_ret(m, imp)} {mname}({_param(imp)}) {{",
                            f"{INDENT * 2}// TODO[STUB_SPRING_LOGIC] {java_comment(why)}",
                            f"{INDENT * 2}throw new UnsupportedOperationException({java_string(why)});",
                            f"{INDENT}}}"])
    else:
        store_t = imp.use(f"{layout.store_pkg}.{store}")
        logic_t = imp.use(f"{layout.logic_pkg}.{logic}")
        tx = imp.use(_TRANSACTIONAL)
        members.append([f"{INDENT}private static final {imp.use(_LOGGER)} log = "
                        f"{imp.use(_LOGGER_FACTORY)}.getLogger({name}.class);"])
        members.append([f"{INDENT}@{imp.use(_AUTOWIRED)}", f"{INDENT}private {store_t} store;"])
        for mname, m in methods:
            ann = f"@{tx}(readOnly = true)" if m.read_only else f"@{tx}"
            members.append([f"{INDENT}@Override", f"{INDENT}{ann}",
                            f"{INDENT}public {_ret(m, imp)} {mname}({_param(imp)}) {{",
                            f'{INDENT * 2}log.debug("{mname} params={{}}", params == null ? null : params.keySet());',
                            f"{INDENT * 2}return new {logic_t}(store).{mname}(params);",
                            f"{INDENT}}}"])
    return _source(layout.spring_logic_pkg, imp,
                   _class(f"public class {name} implements {spec_t}", members, [f"@{imp.use(_SERVICE)}"]))


# ---- REST 컨트롤러 --------------------------------------------------------------------


def _ok_helper(imp: _Imports, helper: str) -> list[str]:
    mp, lm = imp.use(_MAP), imp.use(_LINKED_MAP)
    return [f"{INDENT}private static {mp}<String, Object> {helper}(Object data) {{",
            f"{INDENT * 2}{mp}<String, Object> answer = new {lm}<>();",
            f'{INDENT * 2}answer.put("ok", true);', f'{INDENT * 2}answer.put("data", data);',
            f"{INDENT * 2}return answer;", f"{INDENT}}}"]


def _norm_path(path: str) -> str:
    p = str(path or "").strip()
    if p in ("", "/"):
        return ""
    return p if p.startswith("/") else "/" + p


def _path_vars(path: str) -> list[tuple[str, str]]:
    """경로의 ``{name}`` 변수를 (원래 이름, 자바 이름) 목록으로 반환한다. 이름이 겹치면 한 번만 쓴다."""
    out: list[tuple[str, str]] = []
    taken = {"params", "body", "input", "service"}
    seen: set[str] = set()
    for raw in _PATH_VAR.findall(path):
        raw = raw.strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        base = java_safe(re.sub(r"-+(\w)", lambda mm: mm.group(1).upper(), raw))
        out.append((raw, _fresh(base, taken)))
    return out


def _mapper_controller(res: Resource, layout: Layout, rules: ConvertRules, x: str,
                       methods: list[tuple[str, Method]], service_label: str
                       ) -> tuple[str | None, list[QueueItem]]:
    exposed = [(n, m) for n, m in methods if m.endpoint is not None]
    if not exposed:
        return None, []
    name, spec = f"{x}Controller", f"{x}Service"
    imp = _Imports(layout.rest_pkg, name)
    spec_t = imp.use(f"{layout.spec_pkg}.{spec}")
    prefix = rules.text("frontend.api_prefix", "/api").rstrip("/")
    base = f"{prefix}/{str(res.rest_base or '').strip().strip('/') or kebab(res.resource)}"
    queue: list[QueueItem] = []
    taken = {n for n, _ in exposed} | {f"{n}Json" for n, _ in exposed}
    helper = _fresh("ok", taken)
    handler_names = {helper}
    used: set[tuple[str, str]] = set()
    members: list[list[str]] = [[f"{INDENT}@{imp.use(_AUTOWIRED)}", f"{INDENT}private {spec_t} service;"]]
    mp, lm = imp.use(_MAP), imp.use(_LINKED_MAP)
    for mname, m in exposed:
        ep: Endpoint = m.endpoint  # type: ignore[assignment]
        verb = str(ep.verb or "").strip().upper()
        component = f"{service_label}::{mname}"
        if verb not in _VERB_ANNOTATIONS:
            queue.append(QueueItem(component=component,
                                   reason=f"[ENDPOINT_VERB] the endpoint verb {ep.verb!r} is not supported; "
                                          f"no handler was generated"))
            continue
        path = _norm_path(ep.path)
        if (verb, path) in used:
            path = "/" + kebab(mname)
        if (verb, path) in used:
            queue.append(QueueItem(component=component,
                                   reason=f"[ENDPOINT_DUPLICATE] {verb} {base}{path} is already mapped; "
                                          f"no handler was generated"))
            continue
        used.add((verb, path))
        ann = imp.use(_VERB_ANNOTATIONS[verb])
        pvars = _path_vars(path)
        pv_params = [f"@{imp.use(_PATH_VARIABLE)}({java_string(raw)}) String {ident}" for raw, ident in pvars]
        rp = f"@{imp.use(_REQUEST_PARAM)} {mp}<String, Object> params"
        put_vars = [f"{INDENT * 2}input.put({java_string(raw)}, {ident});" for raw, ident in pvars]
        value = java_string(path) if path else ""
        if verb in _BODY_VERBS:
            # 폼 요청은 폼 핸들러만, 본문 없는 요청과 JSON 요청은 JSON 핸들러만 받는다
            mt = imp.use(_MEDIA_TYPE)
            inner = f"value = {value}, " if value else ""
            variants = [("form", f"@{ann}({inner}consumes = {{{mt}.APPLICATION_FORM_URLENCODED_VALUE, "
                                 f"{mt}.MULTIPART_FORM_DATA_VALUE}})"),
                        ("json", f"@{ann}({inner}consumes = {mt}.APPLICATION_JSON_VALUE)")]
        else:
            variants = [("form", f"@{ann}({value})" if value else f"@{ann}")]
        for variant, mapping in variants:
            hname = _fresh(mname if variant == "form" else f"{mname}Json", handler_names)
            args = pv_params + [rp]
            if variant == "json":
                args.append(f"@{imp.use(_REQUEST_BODY)}(required = false) {mp}<String, Object> body")
            lines = [f"{INDENT}{mapping}",
                     f"{INDENT}public {mp}<String, Object> {hname}({', '.join(args)}) {{",
                     f"{INDENT * 2}{mp}<String, Object> input = new {lm}<>(params);"]
            lines += put_vars
            if variant == "json":
                lines += [f"{INDENT * 2}if (body != null) {{", f"{INDENT * 3}input.putAll(body);",
                          f"{INDENT * 2}}}"]
            lines += [f"{INDENT * 2}return {helper}(service.{mname}(input));", f"{INDENT}}}"]
            members.append(lines)
    if len(members) == 1:
        return None, queue
    members.append(_ok_helper(imp, helper))
    annotations = [f"@{imp.use(_REST_CONTROLLER)}", f"@{imp.use(_REQUEST_MAPPING)}({java_string(base)})"]
    return _source(layout.rest_pkg, imp, _class(f"public class {name}", members, annotations)), queue


def _transition_ident(t: str) -> str:
    s = re.sub(r"-+([A-Za-z0-9])", lambda mm: mm.group(1).upper(), str(t or "").strip())
    return java_safe(s)


def _router_controller(res: Resource, layout: Layout, rules: ConvertRules, x: str,
                       methods: list[tuple[str, Method]]) -> str:
    name, spec = f"{x}Controller", f"{x}Service"
    imp = _Imports(layout.rest_pkg, name)
    spec_t = imp.use(f"{layout.spec_pkg}.{spec}")
    prefix = rules.text("frontend.api_prefix", "/api").rstrip("/")
    result_param = rules.text("frontend.result_param", "ResultName")
    known = {n for n, _ in methods}
    handler_names: set[str] = set()
    plan = [(t, _transition_ident(t)) for t in res.transitions]
    helper_taken = {h for _, h in plan}
    helper = _fresh("answer", helper_taken)
    handler_names.add(helper)
    mp, lm = imp.use(_MAP), imp.use(_LINKED_MAP)
    rm = imp.use(_REQUEST_METHOD)
    members: list[list[str]] = [[f"{INDENT}@{imp.use(_AUTOWIRED)}", f"{INDENT}private {spec_t} service;"]]
    for t, ident in plan:
        hname = _fresh(ident, handler_names)
        target = java_safe(res.transitions.get(t) or "") if res.transitions.get(t) else ""
        mapping = (f"@{imp.use(_REQUEST_MAPPING)}(value = {java_string('/' + t)}, "
                   f"method = {{{rm}.GET, {rm}.POST}})")
        lines = [f"{INDENT}{mapping}",
                 f"{INDENT}public {mp}<String, Object> {hname}(@{imp.use(_REQUEST_PARAM)} "
                 f"{mp}<String, Object> params) {{"]
        if target and target in known:
            lines += [f"{INDENT * 2}Object result = service.{target}(params);",
                      f"{INDENT * 2}return {helper}(result, params);"]
        else:
            why = f"transition {t} of {res.resource} has no activity method to call"
            lines += [f"{INDENT * 2}// TODO[no-method] {java_comment(why)}",
                      f"{INDENT * 2}throw new UnsupportedOperationException({java_string(why)});"]
        lines.append(f"{INDENT}}}")
        members.append(lines)
    rp = java_string(result_param)
    members.append([f"{INDENT}private static {mp}<String, Object> {helper}(Object result, "
                    f"{mp}<String, Object> params) {{",
                    f"{INDENT * 2}{mp}<String, Object> answer = new {lm}<>();",
                    f'{INDENT * 2}answer.put("ok", true);', f'{INDENT * 2}answer.put("rows", result);',
                    f"{INDENT * 2}Object name = params == null ? null : params.get({rp});",
                    f"{INDENT * 2}if (name != null && !String.valueOf(name).isBlank() "
                    f'&& !"ok".equals(String.valueOf(name))) {{',
                    f"{INDENT * 3}answer.put(String.valueOf(name), result);", f"{INDENT * 2}}}",
                    f"{INDENT * 2}return answer;", f"{INDENT}}}"])
    base = f"{prefix}/{res.resource}"
    annotations = [f"@{imp.use(_REST_CONTROLLER)}", f"@{imp.use(_REQUEST_MAPPING)}({java_string(base)})"]
    return _source(layout.rest_pkg, imp, _class(f"public class {name}", members, annotations))


# ---- 리소스 한 벌 ---------------------------------------------------------------------


def generate_resource(res: Resource, layout: Layout, rules: ConvertRules
                      ) -> tuple[list[GeneratedFile], list[QueueItem]]:
    """리소스 하나로 클래스 한 벌을 만들어 파일 목록과 인수인계 항목을 반환한다.
    ``{X}Service``, ``{X}Store``, ``{X}Logic``, ``{X}SpringLogic``은 항상 만든다.
    stub 리소스면 ``{X}SpringLogic``은 모든 메서드가 예외를 던지고 ``{X}MybatisStore``는 만들지 않는다.
    router가 있으면 전이마다 핸들러를 둔 컨트롤러를 만든다. 없으면 주소가 있는 메서드가 있을 때만 만든다.

    Order of the returned files: Service, Store, Logic, MybatisStore, SpringLogic, Controller. Project text never
    raises; a Resource that makes no sense (no methods, odd names) still gives compilable classes."""
    x = _type_x(res)
    methods = _methods(res)
    db = [(n, m) for n, m in methods if m.db_backed]
    label = res.service.name if res.service is not None and res.service.name else res.resource
    source_path = res.service.source if res.service is not None else (res.mapper.source if res.mapper else "")
    files: list[GeneratedFile] = []
    queue: list[QueueItem] = []

    def add(package: str, cls: str, text: str) -> None:
        files.append(GeneratedFile(path=layout.java_path(package, cls), text=text))

    add(layout.spec_pkg, f"{x}Service", _interface(layout.spec_pkg, f"{x}Service", methods))
    add(layout.store_pkg, f"{x}Store", _interface(layout.store_pkg, f"{x}Store", db))
    add(layout.logic_pkg, f"{x}Logic", _logic(res, layout, x, methods))
    if not res.stub:
        add(layout.mybatis_store_pkg, f"{x}MybatisStore", _mybatis_store(res, layout, x, db))
    add(layout.spring_logic_pkg, f"{x}SpringLogic", _spring_logic(res, layout, x, methods))
    if res.stub:
        queue.append(QueueItem(component=f"{label}::SpringLogic", source_path=source_path,
                               reason=f"[STUB_SPRING_LOGIC] {label} has a router but no method reaches a SQL "
                                      f"statement; {x}SpringLogic throws UnsupportedOperationException in every "
                                      f"method and no {x}MybatisStore was generated"))
    if res.has_router:
        add(layout.rest_pkg, f"{x}Controller", _router_controller(res, layout, rules, x, methods))
    else:
        text, items = _mapper_controller(res, layout, rules, x, methods, label)
        for it in items:
            it.source_path = it.source_path or source_path
        queue += items
        if text is not None:
            add(layout.rest_pkg, f"{x}Controller", text)
    return files, queue


# ---- 화면 컨트롤러 ---------------------------------------------------------------------


@dataclass
class PageResult:
    """화면 컨트롤러를 만든 결과를 담는 클래스.
    ``created``는 컨트롤러가 받는 모든 주소, ``skipped``는 만들지 않은 경로나 주소와 그 이유다."""
    files: list[GeneratedFile] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    url_prefix_used: str = ""


def _route_kebab(name: str) -> str:
    s = str(name or "").strip()
    return kebab(s.lower() if not re.search(r"[a-z]", s) else s)


def _squash(name: str) -> str:
    return re.sub(r"[-_.\s]+", "", str(name or "")).lower()


def _jsp_rank(path: str) -> tuple[int, int, str]:
    return (path.count("/"), len(path), path)


def _norm_jsp(path: str) -> str:
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _stem(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1]
    return leaf[: -len(".jsp")] if leaf.lower().endswith(".jsp") else leaf


def _page_class(package: str, cls: str, urls: list[str], view: str) -> str:
    imp = _Imports(package, cls)
    rm = imp.use(_REQUEST_METHOD)
    lits = [java_string(u) for u in urls]
    value = lits[0] if len(lits) == 1 else "{" + ", ".join(lits) + "}"
    members = [[f"{INDENT}@{imp.use(_REQUEST_MAPPING)}(value = {value}, method = {{{rm}.GET, {rm}.POST}})",
                f"{INDENT}public String page() {{",
                f"{INDENT * 2}return {java_string(view)};",
                f"{INDENT}}}"]]
    return _source(package, imp, _class(f"public class {cls}", members, [f"@{imp.use(_CONTROLLER)}"]))


def _fresh_ci(base: str, taken_lower: set[str]) -> str:
    name, n = base, 2
    while name.lower() in taken_lower:
        name, n = f"{base}{n}", n + 1
    taken_lower.add(name.lower())
    return name


def _page_url(raw: str, prefix: str) -> str | None:
    """JSP에 쓰여 있던 화면 주소를 서버 기준 경로로 바꿔 반환한다.
    상대 경로는 ``{prefix}/화면`` 주소에서 브라우저가 푸는 것처럼 푼다. 루트에서 만난 ``..``는 무시한다.
    결과가 비어 있거나 화면 이름이 없으면 None이다."""
    u = re.split(r"[?#]", str(raw or "").strip(), maxsplit=1)[0].strip()
    if not u:
        return None
    if u.startswith("/"):
        return u if u.rsplit("/", 1)[-1] else None
    parts = [p for p in prefix.split("/") if p]
    segs = u.split("/")
    for seg in segs:
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    # 마지막 조각이 폴더(빈 조각, ``.``, ``..``)면 화면 이름이 없다
    if segs[-1] in ("", ".", "..") or not parts:
        return None
    return "/" + "/".join(parts)


def generate_page_controllers(api_map: FrontendApiMap, jsp_paths: list[str], layout: Layout, rules: ConvertRules,
                              reserved: Iterable[str] = ()) -> PageResult:
    """화면 주소마다 JSP를 보여 주는 ``{X}PageController``를 만든다.
    프론트엔드에서 찾은 화면 주소가 있으면 주소마다 파일 이름이 같은 JSP를 찾는다. JSP가 없는 주소는 건너뛴다.
    화면 주소가 없으면 복사한 JSP마다 주소를 만든다. 제외 폴더 안의 JSP와 제외 주소는 뺀다.
    컨트롤러는 JSP에 쓰여 있던 주소와 규칙대로 만든 kebab 주소를 모두 받는다.
    같은 이름의 JSP가 여러 폴더에 있으면 가장 짧은 경로를 쓰고 나머지는 건너뛴다.
    확장자가 정확히 ``.jsp``가 아닌 JSP는 화면을 찾을 수 없어서 건너뛴다.
    ``reserved``는 rest 패키지에서 이미 쓰는 클래스 이름이다. 대소문자를 무시하고 겹치면 번호를 붙인다.

    ``jsp_paths`` are relative to ``layout.static_dir``; non-JSP entries are ignored. The view name is the path
    without ``.jsp``. URLs per controller: routes mode = every ``api_map.page_urls`` entry of the route (keys
    matched as written or by their kebab form; a bare ``name.mvc`` gets the prefix; a query or fragment is cut)
    plus ``{prefix}/{kebab route}{suffix}``; scan mode = ``{prefix}/{stem as written}{suffix}`` plus the kebab
    form. Unique, sorted; one value is written without braces. Controllers are made in route order and a URL an
    earlier controller serves is dropped from a later one (named in ``skipped``); a controller left without a URL
    is not made. ``created`` lists every URL served."""
    suffix = rules.text("frontend.page_suffix", ".mvc")
    prefix = str(api_map.mvc_url_prefix or "").strip().rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    result = PageResult(url_prefix_used=prefix)
    jsps: list[str] = []
    for p in sorted({_norm_jsp(x) for x in jsp_paths}):
        if p.endswith(".jsp"):
            jsps.append(p)
        elif p.lower().endswith(".jsp"):
            # 화면 이름에는 .jsp가 붙으므로 MENU.JSP 같은 파일은 찾지 못한다
            result.skipped.append(f"{p}: the extension is not .jsp, the view resolver would not find it")
    jsps.sort(key=_jsp_rank)

    # 주소 키마다 JSP 경로 목록과 받을 주소를 모은다
    pairs: list[tuple[str, list[str], list[str]]] = []
    if api_map.page_routes:
        written: dict[str, list[str]] = {}
        for key, urls in (api_map.page_urls or {}).items():
            written.setdefault(_route_kebab(key), []).extend(str(u) for u in (urls or []))
        routes = sorted({_route_kebab(r) for r in api_map.page_routes if str(r or "").strip()})
        by_kebab: dict[str, list[str]] = {}
        by_squash: dict[str, list[str]] = {}
        for p in jsps:
            by_kebab.setdefault(_route_kebab(_stem(p)), []).append(p)
            by_squash.setdefault(_squash(_stem(p)), []).append(p)
        for route in routes:
            found = by_kebab.get(route) or by_squash.get(_squash(route)) or []
            urls = []
            for u in written.get(route, []):
                resolved = _page_url(u, prefix)
                if resolved is None:
                    result.skipped.append(f"{u}: the link of {route} has no page name, not served")
                else:
                    urls.append(resolved)
            pairs.append((route, found, urls))
    else:
        exclude_dirs = {d.lower() for d in rules.strs("frontend.page_exclude_dirs")}
        exclude_routes = {_route_kebab(r) for r in rules.strs("frontend.page_exclude_routes")}
        grouped: dict[str, list[str]] = {}
        for p in jsps:
            folders = [s.lower() for s in p.split("/")[:-1]]
            if any(f in exclude_dirs for f in folders):
                continue
            route = _route_kebab(_stem(p))
            if not route or route in exclude_routes:
                continue
            grouped.setdefault(route, []).append(p)
        for route, found in sorted(grouped.items()):
            winner = min(found, key=_jsp_rank)
            pairs.append((route, found, [f"{prefix}/{_stem(winner)}{suffix}"]))

    taken = {r.lower() for r in reserved}
    served: dict[str, str] = {}
    pkg = layout.rest_pkg
    for route, found, extra in pairs:
        if not found:
            result.skipped.append(f"{route}: no JSP")
            continue
        ordered = sorted(found, key=_jsp_rank)
        winner = ordered[0]
        result.skipped += [f"{p}: same name as {winner}" for p in ordered[1:]]
        cls = _fresh_ci(f"{x_name(route) or 'Page'}PageController", taken)
        urls: list[str] = []
        for url in sorted({u for u in [f"{prefix}/{route}{suffix}", *extra] if u}):
            if url in served:
                result.skipped.append(f"{url}: already served by {served[url]}, left out of {cls}")
                continue
            urls.append(url)
        if not urls:
            result.skipped.append(f"{route}: every URL is served by another page controller")
            continue
        for url in urls:
            served[url] = cls
        view = winner[: -len(".jsp")]
        result.files.append(GeneratedFile(path=layout.java_path(pkg, cls), text=_page_class(pkg, cls, urls, view)))
        result.created += urls
    return result


# ---- 주소 중복 검사 -------------------------------------------------------------------

_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_VERB_OF = {"GetMapping": ["GET"], "PostMapping": ["POST"], "PutMapping": ["PUT"], "PatchMapping": ["PATCH"],
            "DeleteMapping": ["DELETE"]}
_ANNOTATION = re.compile(r"^( *)@([\w.]+)(?:\((.*)\))?\s*$")


def _unjava(lit: str) -> str:
    body, out, i = lit[1:-1], [], 0
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(c)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "u" and re.match(r"[0-9a-fA-F]{4}", body[i + 2:i + 6]):
            out.append(chr(int(body[i + 2:i + 6], 16)))
            i += 6
        elif nxt in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1:])
            out.append(chr(int(m.group(0), 8)))
            i += 1 + len(m.group(0))
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def _mapping_args(args: str) -> tuple[list[str], list[str], str]:
    """어노테이션 인자에서 경로 목록, HTTP 메서드 목록, consumes 문구를 꺼낸다."""
    args = (args or "").strip()
    paths: list[str] = []
    rest = args
    value = re.match(r"(?:value|path)\s*=\s*", args)
    if args.startswith(('"', "{")) or value:
        tail = args[value.end():] if value else args
        if tail.startswith("{"):
            depth, j = 0, 0
            for j, ch in enumerate(tail):
                depth += (ch == "{") - (ch == "}")
                if depth == 0:
                    break
            group, rest = tail[:j + 1], tail[j + 1:]
            paths = [_unjava(m.group(0)) for m in _LITERAL.finditer(group)]
        else:
            m = _LITERAL.match(tail)
            if m:
                paths, rest = [_unjava(m.group(0))], tail[m.end():]
    methods = re.search(r"method\s*=\s*\{([^}]*)\}|method\s*=\s*([\w.]+)", rest)
    verbs: list[str] = []
    if methods:
        verbs = [v.rsplit(".", 1)[-1] for v in re.findall(r"[\w.]+", methods.group(1) or methods.group(2))]
    consumes = ""
    cm = re.search(r"consumes\s*=\s*(\{[^}]*\}|[\w.]+)", rest)
    if cm:
        consumes = re.sub(r"\b(?:[a-z]\w*\.)+(?=[A-Z])", "", cm.group(1))
    return paths or [""], verbs, consumes


def duplicate_mappings(files: list[GeneratedFile]) -> list[str]:
    """생성한 컨트롤러들의 주소를 읽어서 두 핸들러가 같은 주소를 받는 경우를 한 줄씩 반환한다.
    HTTP 메서드, 전체 경로, consumes가 같으면 중복이다. 경로가 대소문자만 다르면 그렇다고 따로 적는다.
    겹치는 주소가 없으면 빈 목록을 반환한다.

    Reads only the texts this module writes (one annotation per line, class annotations at column 0, handler
    annotations indented, the method declaration right after its annotations). Files that are not controllers
    are ignored. Lines are sorted; a key claimed by n handlers gives one line naming all of them."""
    claims: dict[tuple[str, str, str], list[str]] = {}
    for f in sorted(files, key=lambda g: str(g.path)):
        text = str(f.text)
        if "Controller" not in text:
            continue
        cls = str(f.path).rsplit("/", 1)[-1].removesuffix(".java")
        base = ""
        pending: list[tuple[str, str]] = []
        for line in text.splitlines():
            m = _ANNOTATION.match(line)
            if m:
                name = m.group(2).rsplit(".", 1)[-1]
                if not m.group(1):
                    if name == "RequestMapping":
                        base = _mapping_args(m.group(3) or "")[0][0]
                elif name in _VERB_OF or name == "RequestMapping":
                    pending.append((name, m.group(3) or ""))
                continue
            decl = re.match(r"^ +public [^(]*?\b(\w+)\(", line)
            if decl and pending:
                for name, args in pending:
                    paths, verbs, consumes = _mapping_args(args)
                    for verb in _VERB_OF.get(name) or verbs or ["ANY"]:
                        for path in paths:
                            claims.setdefault((verb, base + path, consumes), []).append(f"{cls}.{decl.group(1)}")
            if not line.strip().startswith("@"):
                pending = []

    def show(verb: str, path: str, consumes: str) -> str:
        return f"{verb} {path}" + (f" [consumes {consumes}]" if consumes else "")

    lines: list[str] = []
    for (verb, path, consumes), who in sorted(claims.items()):
        if len(who) > 1:
            lines.append(f"{show(verb, path, consumes)}: claimed by {' and '.join(who)}")
    folded: dict[tuple[str, str, str], list[tuple[str, list[str]]]] = {}
    for (verb, path, consumes), who in sorted(claims.items()):
        folded.setdefault((verb, path.lower(), consumes), []).append((path, who))
    for (verb, _, consumes), spellings in sorted(folded.items()):
        owners = sorted({w for _, who in spellings for w in who})
        if len(spellings) > 1 and len(owners) > 1:
            names = " and ".join(show(verb, p, consumes) for p, _ in spellings)
            lines.append(f"{names} differ only in case: {', '.join(owners)}")
    return lines


# ---- 파일 쓰기 -------------------------------------------------------------------------


def write_generated(out_root: Path, files: list[GeneratedFile]) -> list[str]:
    """생성한 파일을 ``out_root`` 아래에 UTF-8로 쓰고 오류 목록을 반환한다.
    경로가 ``out_root`` 밖을 가리키면 그 파일은 쓰지 않고 오류로 남긴다.
    같은 경로에 내용이 다른 파일이 두 번 오면 두 번째는 쓰지 않는다.
    대소문자만 다른 경로도 앞의 파일을 덮어쓰므로 쓰지 않고 오류로 남긴다.

    Line ends are written as ``\\n`` (``\\r\\n`` and ``\\r`` in the text are turned into it). Files are written in
    path order. A write that fails (permissions, encoding) is an error string, never an exception."""
    root = Path(out_root)
    errors: list[str] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
        real_root = root.resolve()
    except OSError as e:
        return [f"{root}: cannot create the output folder: {e}"]
    done: dict[str, str] = {}
    # 대소문자를 무시한 경로와 처음 쓴 철자. 대소문자를 구분하지 않는 파일 시스템에서 덮어쓰지 않게 한다
    spelled: dict[str, str] = {}
    unique: list[GeneratedFile] = []
    seen_pairs: set[tuple[str, str]] = set()
    for f in sorted(files, key=lambda g: str(g.path)):
        if (str(f.path), str(f.text)) not in seen_pairs:
            seen_pairs.add((str(f.path), str(f.text)))
            unique.append(f)
    for f in unique:
        raw = str(f.path or "")
        rel = raw.replace("\\", "/")
        parts = rel.split("/")
        if (not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in parts
                or any(p in ("", ".") for p in parts) or "\x00" in rel):
            errors.append(f"{raw}: refused, the path leaves the output folder or is not a plain relative path")
            continue
        target = root.joinpath(*parts)
        try:
            real = Path(os.path.realpath(target))
            real.relative_to(real_root)
        except ValueError:
            errors.append(f"{raw}: refused, the path leaves the output folder")
            continue
        text = str(f.text).replace("\r\n", "\n").replace("\r", "\n")
        first = spelled.get(rel.lower())
        if first is not None and first != rel:
            errors.append(f"{raw}: refused, the path differs only in case from {first}")
            continue
        if rel in done:
            if done[rel] != text:
                errors.append(f"{raw}: refused, another file with different text has the same path")
            continue
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as e:
            errors.append(f"{raw}: not written, the text cannot be encoded as UTF-8: {e}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as e:
            errors.append(f"{raw}: not written: {e}")
            continue
        done[rel] = text
        spelled[rel.lower()] = rel
    return errors
