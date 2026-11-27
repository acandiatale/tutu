"""서비스와 mapper를 같은 이름끼리 묶어 리소스를 만들고, 리소스마다 메서드와 REST 주소를 정하는 모듈.

Contract: docs/FW-CONVERT.md sections 3.3 and 3.4 (the endpoint table and the SqlSession-call table). Pure: records
in, records out. Consumes ``ServiceDef`` (servicexml.py) and ``MapperIndex`` (sqlmap.py); javagen.py turns the
returned ``Resource`` records into Java.

Choices where the specification is silent (sbmig):
- two services with the same ``{X}``: the first in the given order is used; every mapper with that ``{X}`` gives
  methods (in the given order), each method carrying the namespace of its own mapper; ``Resource.mapper`` is the
  first, ``Resource.mappers`` all of them, and endpoints are made for statements of any of them;
- "the same name twice is one method" holds only for the same statement: the same resolved statement, else the same
  ``sql-key`` (case ignored), else the same activity; a method of another statement under a taken name, from the
  service or from a mapper, gets the first free name ``{name}2``, ``{name}3``, ... and the transition points at it;
- ``{X}`` names equal ignoring case (they would overwrite each other on a case-insensitive disk): the later one in
  sorted order gets the first free ``{X}2``, ``{X}3``, ... (compared ignoring case); only ``x_name`` changes;
- word lists (``rest.*_words``, ``activities.mutate_words``) match whole words of the kebab split of the name's last
  part, never substrings (``selectAccountList`` has no ``count``). The "kind ``other`` returns int" rule reads only
  the FIRST word of the ``sql-key``'s last part (``updateStatus`` mutates, ``selectLastUpdateDate`` does not);
- returns of a service method: the kind table (``activities.kind_returns``) first; for any other kind (a project's
  own class) the resolved statement decides like a mapper statement (insert / update / delete: int, select: list or
  int by the count words), and only when nothing resolves the first-word rule on the ``sql-key`` applies;
- the ``rest.lov_prefix`` rule applies to selects only, as in the endpoint table;
- an endpoint that still collides after the ``/{kebab(id)}`` fallback gets ``-2``, ``-3``, ... appended;
- ``rest_base``: a resource with a router keeps its resource name as written (its controller is served there) and
  reserves it exactly; every other resource gets ``kebab(resource)``, unique ignoring case among those and never
  exactly equal to a reserved router name (a clash gets ``-2``, ``-3``, ... in ``x_name`` order); the exposure filter
  compares the kebab form of a call's first path part after the api prefix with it;
- ``unreachable_methods`` names every method no address reaches (no endpoint and nothing unexposed in a resource
  without a router; no transition in a resource with one: the mapper statements appended there), with the reason;
- an endpoint the exposure filter removes stays on its method as ``Method.unexposed``; ``unexposed_endpoints`` lists
  them for the report;
- exposure filter: unmatched endpoints are removed only when at least one call MATCHES an endpoint of this
  resource; when calls name the resource but none matches, every endpoint stays. A call's query string and a trailing ``/`` are ignored; a call without a verb
  matches every verb; ``DEL`` is ``DELETE``; ``{...}`` on either side matches any one segment;
- a verb other than select / insert / update / delete reaches the tables as select, except ``merge`` (insert), the
  same rule the SQL-file converter uses."""
from __future__ import annotations

import re

from sbmig.fwconvert import naming
from sbmig.fwconvert.model import (
    Activity,
    DbVerdict,
    Endpoint,
    FrontendApiMap,
    MapperFile,
    MapperIndex,
    Method,
    QueueItem,
    Resource,
    ServiceDef,
    Statement,
)
from sbmig.fwconvert.rules import ConvertRules
from sbmig.fwconvert.servicexml import NO_METHOD_KINDS, classify_activity

_PARAM = re.compile(r"^\{[^/{}]*\}$")


def _words(rules: ConvertRules, path: str) -> list[str]:
    return [w.strip().lower() for w in rules.strs(path) if w.strip()]


def _name_words(name: str) -> list[str]:
    """The lower-case words of the last part of a name: ``order.selectLastUpdateDate`` -> select, last, update, date."""
    return [w for w in naming.kebab(naming.last_token(name)).split("-") if w]


def _has(name: str, words: list[str]) -> bool:
    # 단어 목록은 이름을 단어로 나눈 뒤 단어 단위로 비교한다. account 안의 count는 맞지 않는다
    return bool(set(_name_words(name)) & set(words))


def _verb(verb: str) -> str:
    """The statement verb as the two tables read it: ``""`` when it is not one of the four (unknown)."""
    v = str(verb or "").strip().lower()
    if v == "merge":
        return "insert"
    return v if v in ("select", "insert", "update", "delete") else ""


def session_call(returns: str, verb: str, name: str, rules: ConvertRules) -> str:
    """메서드가 호출할 ``SqlSession`` 메서드 이름을 정한다.
    list면 ``selectList``, map이면 ``selectOne``이다. int면 SQL 종류와 이름의 단어로 정하고, 정하지 못하면 ``update``다."""
    if returns == "list":
        return "selectList"
    if returns == "map":
        return "selectOne"
    v = _verb(verb)
    if v == "select" and _has(name, _words(rules, "rest.count_words")):
        return "selectOne"
    if v in ("insert", "update", "delete"):
        return v
    if not v:
        if _has(name, _words(rules, "rest.delete_words")):
            return "delete"
        if _has(name, _words(rules, "rest.insert_words")):
            return "insert"
        if _has(name, _words(rules, "rest.update_words")):
            return "update"
    return "update"


def endpoint_for(statement: Statement, rules: ConvertRules) -> Endpoint | None:
    """SQL 문 하나가 mapper 컨트롤러에서 노출될 REST 주소를 정한다.
    id는 소문자로 비교한다. id가 ``lov``로 시작하는 조회 문은 None을 반환한다."""
    sid = str(statement.id or "")
    low = sid.lower()
    v = _verb(statement.verb) or "select"
    if v == "select":
        lov = rules.text("rest.lov_prefix", "lov").lower()
        if lov and low.startswith(lov):
            return None
        suffix = rules.text("rest.summary_suffix", "summary").lower()
        if low in _words(rules, "rest.summary_ids") or (suffix and low.endswith(suffix)):
            return Endpoint("GET", "/summary")
        if low in _words(rules, "rest.list_ids"):
            return Endpoint("GET", "")
        return Endpoint("GET", f"/{naming.kebab(sid)}")
    if v in ("insert", "update"):
        if low in _words(rules, "rest.save_ids"):
            return Endpoint("POST", "")
        return Endpoint("POST", f"/{naming.kebab(sid)}")
    if low in _words(rules, "rest.delete_ids"):
        return Endpoint("DELETE", "/{id}")
    return Endpoint("DELETE", f"/{naming.kebab(sid)}")


# ---- 노출 필터 -------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    p = str(path or "").split("?", 1)[0].split("#", 1)[0]
    return [s for s in p.split("/") if s]


def _norm_verb(verb: str) -> str:
    v = str(verb or "").strip().upper()
    return "DELETE" if v == "DEL" else v


def _calls_for(rest_base: str, api_map: FrontendApiMap | None, rules: ConvertRules) -> list[tuple[str, list[str]]]:
    """(verb, segments after /api/{rest_base}) of every front-end call that names this resource."""
    if api_map is None:
        return []
    prefix = _segments(rules.text("frontend.api_prefix", "/api"))
    want = rest_base
    out: list[tuple[str, list[str]]] = []
    for call in api_map.rest_endpoints:
        segs = _segments(call.path)
        if len(segs) <= len(prefix) or segs[: len(prefix)] != prefix:
            continue
        if naming.kebab(segs[len(prefix)]) != want:
            continue
        out.append((_norm_verb(call.verb), segs[len(prefix) + 1:]))
    return out


def _seg_match(a: str, b: str) -> bool:
    return a == b or bool(_PARAM.match(a)) or bool(_PARAM.match(b))


def _called(ep: Endpoint, calls: list[tuple[str, list[str]]]) -> bool:
    mine = _segments(ep.path)
    for verb, segs in calls:
        if verb and verb != ep.verb:
            continue
        if len(segs) == len(mine) and all(_seg_match(a, b) for a, b in zip(mine, segs)):
            return True
    return False


# ---- 메서드 ----------------------------------------------------------------------


def _statement_returns(st: Statement, name: str, rules: ConvertRules) -> str:
    if _verb(st.verb) in ("insert", "update", "delete"):
        return "int"
    return "int" if _has(name, _words(rules, "rest.count_words")) else "list"


def _returns_for_activity(act: Activity, sql_key: str, found: tuple[MapperFile, Statement] | None, name: str,
                          rules: ConvertRules) -> str:
    table = rules.get("activities.kind_returns", {}) or {}
    if act.kind in table:
        return str(table[act.kind])
    # 프로젝트 클래스가 호출하는 SQL 문을 찾았으면 그 문의 종류로 정한다
    if found:
        return _statement_returns(found[1], name, rules)
    # 변경 문인지는 id의 첫 단어로만 본다. selectLastUpdateDate는 조회다
    first = _name_words(sql_key)[:1]
    return "int" if first and first[0] in _words(rules, "activities.mutate_words") else "list"


def _service_method(act: Activity, verdict: DbVerdict, mappers: MapperIndex,
                    rules: ConvertRules) -> tuple[Method, tuple[MapperFile, Statement] | None]:
    sql_key = act.properties.get(rules.text("activities.sql_key", "sql-key"), "").strip()
    found = mappers.find(verdict.statement) if verdict.statement else None
    if found:
        name = naming.method_name(found[1].id)
    elif sql_key:
        name = naming.method_name(sql_key)
    else:
        name = naming.method_name(act.name)
    returns = _returns_for_activity(act, sql_key, found, name, rules)
    m = Method(name=name, origin="service", kind=act.kind, returns=returns, db_backed=verdict.db_backed,
               activity=act.name, pass_hit=verdict.pass_hit, evidence=verdict.evidence)
    if found:
        mapper, st = found
        m.namespace, m.statement_id = mapper.namespace, st.id
        m.session_call = session_call(returns, st.verb, name, rules)
        m.read_only = m.session_call.startswith("select")
    return m, found


def _mapper_method(mapper: MapperFile, st: Statement, name: str, rules: ConvertRules) -> Method:
    returns = _statement_returns(st, name, rules)
    call = session_call(returns, st.verb, name, rules)
    return Method(name=name, origin="mapper", kind="statement", returns=returns, db_backed=True,
                  read_only=call.startswith("select"), namespace=mapper.namespace, statement_id=st.id,
                  session_call=call)


def _free_name(name: str, taken: set[str]) -> str:
    n = 2
    while f"{name}{n}" in taken:
        n += 1
    return f"{name}{n}"


def _endpoints(res: Resource, methods: list[Method], target: dict[int, tuple[MapperFile, Statement]],
               api_map: FrontendApiMap | None, rules: ConvertRules) -> None:
    own = {id(mf) for mf in res.mappers}
    used: set[tuple[str, str]] = set()
    for m in methods:
        found = target.get(id(m))
        if not found or id(found[0]) not in own:
            continue
        ep = endpoint_for(found[1], rules)
        if ep is None:
            continue
        if (ep.verb, ep.path) in used:
            ep = Endpoint(ep.verb, f"/{naming.kebab(found[1].id)}")
            base, n = ep.path, 2
            while (ep.verb, ep.path) in used:
                ep = Endpoint(ep.verb, f"{base}-{n}")
                n += 1
        used.add((ep.verb, ep.path))
        m.endpoint = ep
    calls = _calls_for(res.rest_base, api_map, rules)
    exposed = [m for m in methods if m.endpoint is not None]
    matched = {id(m) for m in exposed if _called(m.endpoint, calls)} if calls else set()
    # 프론트엔드 호출과 맞는 주소가 하나라도 있을 때만 맞지 않는 주소를 뺀다
    if matched:
        for m in exposed:
            if id(m) not in matched:
                # 뺀 주소는 보고서에 남길 수 있게 ``unexposed``에 둔다
                m.unexposed, m.endpoint = m.endpoint, None


def _build(x: str, rest_base: str, svc: ServiceDef | None, own: list[MapperFile], mappers: MapperIndex,
           api_map: FrontendApiMap | None, rules: ConvertRules) -> Resource:
    first = own[0] if own else None
    res = Resource(resource=_resource_name(x, svc, own), x_name=x, service=svc, mapper=first, mappers=list(own),
                   rest_base=rest_base)
    methods: list[Method] = []
    taken: set[str] = set()
    # 메서드가 호출하는 SQL 문. 같은 mapper에 있는지 객체로 비교한다
    target: dict[int, tuple[MapperFile, Statement]] = {}
    of_activity: dict[str, str] = {}
    sql_key_name = rules.text("activities.sql_key", "sql-key")

    if svc is not None:
        res.has_router = svc.has_router
        targets = set(svc.transitions.values()) if svc.has_router else None
        by_key: dict[tuple, str] = {}
        for act in svc.activities:
            if act.kind in NO_METHOD_KINDS:
                continue
            if targets is not None and act.name not in targets:
                continue
            verdict = svc.verdicts.get(act.name) or classify_activity(act, mappers, None, rules)
            m, found = _service_method(act, verdict, mappers, rules)
            sql_key = act.properties.get(sql_key_name, "").strip()
            if found:
                key: tuple = ("statement", id(found[0]), found[1].id)
            elif sql_key:
                key = ("sql-key", sql_key.lower())
            else:
                key = ("activity", act.name)
            # 같은 SQL 문을 호출하는 activity는 한 메서드로 본다. 먼저 나온 activity가 남는다
            if key in by_key:
                of_activity[act.name] = by_key[key]
                continue
            # 다른 SQL 문이 이미 이 이름을 쓰면 뒤에 숫자를 붙인다
            if m.name in taken:
                m.name = _free_name(m.name, taken)
            taken.add(m.name)
            by_key[key] = m.name
            of_activity[act.name] = m.name
            methods.append(m)
            if found:
                target[id(m)] = found
        if svc.has_router:
            res.transitions = {t: of_activity.get(a, "") for t, a in svc.transitions.items()}

    called = {(id(f[0]), f[1].id) for f in target.values()}
    for mapper in own:
        for st in mapper.statements:
            if (id(mapper), st.id) in called:
                continue
            called.add((id(mapper), st.id))
            name = naming.method_name(st.id)
            # 다른 SQL 문의 메서드가 이미 이 이름을 쓰면 뒤에 숫자를 붙인다
            if name in taken:
                name = _free_name(name, taken)
            taken.add(name)
            m = _mapper_method(mapper, st, name, rules)
            methods.append(m)
            target[id(m)] = (mapper, st)

    if own and not res.has_router:
        _endpoints(res, methods, target, api_map, rules)

    res.methods = methods
    res.stub = res.has_router and not any(m.statement_id for m in methods)
    return res


def _resource_name(x: str, svc: ServiceDef | None, own: list[MapperFile]) -> str:
    return svc.resource if svc else (own[0].resource if own else x)


def _unique_case(names: list[str], sep: str = "") -> list[str]:
    """The names in the same order, a name equal to an earlier one ignoring case with ``{sep}2``, ``{sep}3``, ..."""
    out: list[str] = []
    taken: set[str] = set()
    for x in names:
        final = x
        if final.lower() in taken:
            n = 2
            while f"{x}{sep}{n}".lower() in taken:
                n += 1
            final = f"{x}{sep}{n}"
        taken.add(final.lower())
        out.append(final)
    return out


def build_resources(services: list[ServiceDef], mappers: MapperIndex, api_map: FrontendApiMap | None,
                    rules: ConvertRules) -> list[Resource]:
    """서비스와 mapper를 ``{X}`` 이름으로 묶어 리소스 목록을 만든다. 목록은 ``x_name`` 순서다.
    bean 형식 서비스는 리소스가 되지 않는다. 이름이 같은 mapper가 여럿이면 모두에서 메서드를 만든다.
    대소문자만 다른 이름이 있으면 뒤의 이름에 숫자를 붙인다.
    router가 있는 리소스는 REST 주소를 만들지 않는다. 프론트엔드 호출과 맞는 주소가 있으면 맞는 주소만 남긴다.

    Methods: service activities first (router targets only when there is a router; never ``router`` / ``user``),
    then every statement of the resource's own mappers (in the given order) that no method calls yet. ``namespace``
    is the namespace of the mapper where the statement is, which may be another resource's mapper. ``stub`` = router
    and no method with a ``statement_id``. A case-only rename changes ``x_name`` only; namespaces and ``resource``
    stay as they were."""
    suffix = rules.text("naming.service_suffix", "-service")
    by_service: dict[str, ServiceDef] = {}
    for s in services:
        if s.dialect != "activity":
            continue
        x = s.x_name or naming.x_name(s.resource or naming.strip_suffix(s.name, suffix))
        if x:
            by_service.setdefault(x, s)
    by_mapper: dict[str, list[MapperFile]] = {}
    for mf in mappers.mappers:
        if mf.x_name:
            by_mapper.setdefault(mf.x_name, []).append(mf)
    keys = sorted(set(by_service) | set(by_mapper))
    finals = _unique_case(keys)
    bases = _rest_bases(keys, by_service, by_mapper)
    out = [_build(final, base, by_service.get(x), by_mapper.get(x, []), mappers, api_map, rules)
           for x, final, base in zip(keys, finals, bases)]
    return sorted(out, key=lambda r: r.x_name)


def _rest_bases(keys: list[str], by_service: dict[str, ServiceDef],
                by_mapper: dict[str, list[MapperFile]]) -> list[str]:
    """``rest_base`` per group key, in the order of ``keys``.

    A resource with a router is served at ``/api/{resource as written}``: it reserves that name exactly (Spring
    compares paths exactly) and takes no kebab slot. The others get ``kebab(resource)``, unique ignoring case among
    themselves and never exactly equal to a reserved router name; a clash gets ``-2``, ``-3``, ..."""
    routed = {x: by_service[x].resource for x in keys if x in by_service and by_service[x].has_router}
    reserved = set(routed.values())
    taken: set[str] = set()
    out: list[str] = []
    for x in keys:
        if x in routed:
            out.append(routed[x])
            continue
        base = naming.kebab(_resource_name(x, by_service.get(x), by_mapper.get(x, [])))
        final, n = base, 2
        # router가 쓰는 이름과 같거나 다른 리소스와 대소문자만 다르면 뒤에 번호를 붙인다
        while final.lower() in taken or final in reserved:
            final, n = f"{base}-{n}", n + 1
        taken.add(final.lower())
        out.append(final)
    return out


def unexposed_endpoints(resources: list[Resource], rules: ConvertRules) -> list[str]:
    """프론트엔드가 호출하지 않아 노출에서 뺀 REST 주소를 보고서용 한 줄씩 정렬해서 반환한다.
    줄 형식은 ``GET /api/order/detail  (OrderService.detail)``이다."""
    prefix = rules.text("frontend.api_prefix", "/api").rstrip("/")
    lines = [f"{m.unexposed.verb} {prefix}/{r.rest_base}{m.unexposed.path}  ({r.x_name}Service.{m.name})"
             for r in resources for m in r.methods if m.unexposed is not None]
    return sorted(lines)


def _unreachable_why(res: Resource, m: Method, rules: ConvertRules) -> str:
    if not m.db_backed:
        return f"not database-backed: {m.pass_hit or '-'}"
    if not m.statement_id:
        return "no statement"
    own = any(mf.namespace == m.namespace and any(st.id == m.statement_id for st in mf.statements)
              for mf in res.mappers)
    if not own:
        return f"statement in another mapper: {m.namespace}.{m.statement_id}"
    lov = rules.text("rest.lov_prefix", "lov").lower()
    if lov and m.statement_id.lower().startswith(lov):
        return "list-of-values statement"
    return "no endpoint"


def unreachable_methods(resources: list[Resource], rules: ConvertRules) -> list[str]:
    """어떤 주소로도 호출할 수 없는 메서드를 보고서용 한 줄씩 정렬해서 반환한다.
    router가 없는 리소스는 REST 주소가 없는 메서드를, router가 있는 리소스는 전이가 가리키지 않는 메서드를 쓴다.
    노출에서 뺀 주소가 있는 메서드는 ``unexposed_endpoints``에 나오므로 여기에는 쓰지 않는다.

    Line: ``<X>Service.<method>  (<why>)``; why = ``statement in another mapper: <namespace>.<id>``, ``no statement``,
    ``list-of-values statement``, ``not database-backed: <pass_hit>``, ``no transition`` (a router resource's mapper
    statement that no transition calls), ``no endpoint`` (a fallback no current rule produces). Read-only."""
    lines: list[str] = []
    for r in resources:
        if r.has_router:
            targets = set(r.transitions.values())
            lines += [f"{r.x_name}Service.{m.name}  (no transition)" for m in r.methods if m.name not in targets]
            continue
        lines += [f"{r.x_name}Service.{m.name}  ({_unreachable_why(r, m, rules)})"
                  for m in r.methods if m.endpoint is None and m.unexposed is None]
    return sorted(lines)


def ajax_targets_without_controller(called: dict[str, list[str]], resources: list[Resource],
                                    rules: ConvertRules) -> list[QueueItem]:
    """바꾼 ajax 호출이 가리키는 서비스에 router 컨트롤러가 없으면 인수인계 항목을 만든다.
    ``called``는 정적 폴더 기준 파일 경로와 그 파일의 호출이 가리키는 서비스 이름 목록이다.
    이름은 대소문자까지 같아야 맞는다. 항목은 파일과 이름 순서로 반환한다.

    The ajax rewrite always writes ``/api/{service as written}/{transition}``; only a resource with ``has_router``
    serves that address (the router controller keeps the resource as written; Spring matches paths case-sensitively).
    One item per file and name: component ``js_ajax::{file}``, reason ``[AJAX_NO_CONTROLLER] service '<name>': <why>``.
    ``rules`` is in the signature for the service suffix of the messages."""
    suffix = rules.text("naming.service_suffix", "-service")
    services = [r for r in resources if r.service is not None]
    out: list[QueueItem] = []
    for file in sorted(called):
        for name in sorted(set(called[file] or [])):
            if any(r.has_router and r.resource == name for r in services):
                continue
            exact = [r for r in services if r.resource == name]
            other = sorted(r.resource for r in services if r.resource != name and r.resource.lower() == name.lower())
            if exact:
                why = f"the service {name + suffix!r} has no router, so no controller serves /api/{name}/..."
                # mapper 컨트롤러가 있을 때만 그 주소를 알려 준다
                if any(m.endpoint is not None for m in exact[0].methods):
                    why += f"; its mapper controller is at /api/{exact[0].rest_base}"
            elif other:
                why = (f"no service of exactly that name was converted; {', '.join(repr(o) for o in other)} differs "
                       f"only in case, and Spring matches paths case-sensitively")
            else:
                why = "no service of that name was converted, so no controller serves the rewritten address"
            out.append(QueueItem(component=f"js_ajax::{file}", source_path=file,
                                 reason=f"[AJAX_NO_CONTROLLER] service {name!r}: {why}"))
    return out
