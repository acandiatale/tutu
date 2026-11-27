"""서비스 XML을 읽어 activity 목록을 만들고, activity마다 DB를 쓰는지 판단하는 모듈.

Contract: docs/FW-CONVERT.md sections 2.2 (dialect, activity kinds), 2.3 (the DB classifier, first match wins) and 4
(hand-over items). Elements are matched by local name, so a namespace on the root changes nothing. Bad input never
raises: an unreadable or foreign file is an entry of ``ServiceResult.errors``, a bean-dialect file and every
activity a person must look at is a ``QueueItem``.

Choices where the specification is silent (sbmig):
- an activity without a name gets ``activity{n}`` (1-based position); a second activity with a name already used in
  the same service is dropped (the router addresses activities by name, so only one can be meant);
- every ``service`` element of a file is read (``parse_services``), in document order; a ``service`` nested inside
  another one is part of the outer one; a service element that has no activity and no bean marker is an
  activity-dialect service with no activity when it is the root, else it is skipped;
- an entity reference in element text stays as the text ``&name;`` (entities are never resolved); in an attribute
  lxml substitutes an internal entity even so. ``decompose_services`` names the unresolved references in an error;
- two files define the same service when their names give the same ``{X}`` (case and ``-`` / ``_`` ignored), because
  both would become one resource;
- 2a counts distinct ``"ns.id"`` literals, so a class calling two statements of ONE mapper is ambiguous as well (the
  brief's rule); Java comments are removed before the source is searched, string literals are kept;
- the source of every activity that is not ``router`` / ``user`` is looked up (only step 2 reads it, the hand-over
  items use its path and excerpt)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree

from sbmig.fwconvert import naming
from sbmig.fwconvert.model import Activity, DbVerdict, MapperIndex, QueueItem, ServiceDef
from sbmig.fwconvert.rules import ConvertRules

EXCERPT_CHARS = 2000
HANDOVER_HITS = frozenset({"2b", "3", "java_only"})
NO_METHOD_KINDS = frozenset({"router", "user"})

_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.DOTALL)
_JAVA_NOISE = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*.*?\*/', re.DOTALL)


# ---- 결과 레코드 -----------------------------------------------------------------


@dataclass
class ServiceResult:
    """서비스 XML 전체를 분해한 결과를 담는 클래스.
    ``errors``에는 읽지 못한 파일과 무시한 파일의 사유가 들어간다."""
    services: list[ServiceDef] = field(default_factory=list)
    queue: list[QueueItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---- 읽기 -----------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _parse_xml(text: str) -> tuple[Any, bool]:
    """Returns (root or None, read tolerantly). The declaration is cut because lxml refuses a str that carries one."""
    body = _DECL.sub("", str(text or "").lstrip("﻿"), count=1)
    if not body.strip():
        return None, False
    try:
        return etree.fromstring(body, etree.XMLParser(resolve_entities=False, no_network=True)), False
    except (etree.XMLSyntaxError, ValueError):
        pass
    try:
        root = etree.fromstring(body, etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except (etree.XMLSyntaxError, ValueError):
        return None, True
    return root, True


def _local(el: Any) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _simple(class_name: str) -> str:
    return str(class_name or "").strip().rsplit(".", 1)[-1]


def _attr_or_text(el: Any, attr: str, entities: list[str]) -> str:
    v = el.get(attr)
    if v is not None:
        return str(v).strip()
    # 본문의 엔티티 참조는 풀지 않고 ``&name;`` 글자 그대로 남긴다. 호출한 쪽이 오류로 기록한다
    parts = [el.text or ""]
    for c in el:
        if c.tag is etree.Entity:
            ref = str(c.text or "")
            entities.append(ref)
            parts.append(ref)
        elif isinstance(c.tag, str):
            parts.append("".join(c.itertext()))
        parts.append(c.tail or "")
    return "".join(parts).strip()


def _fallback_name(source: str, rules: ConvertRules) -> str:
    base = PurePosixPath(str(source or "").replace("\\", "/")).name
    suffix = rules.text("files.service_xml_suffix", "-service.xml")
    if suffix and base.lower().endswith(suffix.lower()):
        return base[: len(base) - len(suffix)]
    return base.rsplit(".", 1)[0] if "." in base else base


def _is_bean(el: Any) -> bool:
    if el.get("id") is not None or el.get("class") is not None:
        return True
    return any(_local(c) == "property" and c.get("ref") is not None for c in el)


def activity_kind(class_name: str, rules: ConvertRules) -> str:
    """activity 클래스 이름으로 종류를 정한다.
    클래스 이름 끝이 ``activities.kinds``의 접미사와 같으면 그 종류다. 대소문자는 가리지 않고, 여러 개가 맞으면 가장 긴 접미사를 쓴다.
    그다음 이름에 ``router``가 들어 있으면 router, 회사 세션 클래스면 user, 나머지는 other다."""
    simple = _simple(class_name).lower()
    best, best_len = "", -1
    kinds = rules.get("activities.kinds", {}) or {}
    for suffix, kind in kinds.items():
        s = str(suffix).strip().lower()
        if s and simple.endswith(s) and len(s) > best_len:
            best, best_len = str(kind), len(s)
    if best:
        return best
    word = rules.text("activities.router_word", "router").lower()
    if word and word in str(class_name or "").lower():
        return "router"
    if any(u.strip() and simple.endswith(u.strip().lower()) for u in rules.strs("activities.user_suffixes")):
        return "user"
    return "other"


def _activity(el: Any, pos: int, rules: ConvertRules, entities: list[str]) -> Activity:
    class_name = str(el.get("class") or "").strip()
    name = str(el.get("name") or "").strip() or f"activity{pos}"
    props: dict[str, str] = {}
    trans: dict[str, str] = {}
    for c in el:
        loc = _local(c)
        key = str(c.get("name") or "").strip() if loc else ""
        if not key:
            continue
        # 같은 이름이 두 번 나오면 처음 값을 쓴다
        if loc == "property":
            props.setdefault(key, _attr_or_text(c, "value", entities))
        elif loc == "transition":
            trans.setdefault(key, _attr_or_text(c, "value", entities))
    return Activity(name=name, class_name=class_name, kind=activity_kind(class_name, rules),
                    properties=props, transitions=trans)


def _activity_service(el: Any, source: str, rules: ConvertRules, entities: list[str]) -> ServiceDef:
    name = str(el.get("name") or "").strip() or _fallback_name(source, rules)
    resource = naming.strip_suffix(name, rules.text("naming.service_suffix", "-service"))
    svc = ServiceDef(name=name, resource=resource, x_name=naming.x_name(resource), source=source)
    used: set[str] = set()
    pos = 0
    for a in el.iter():
        if _local(a) != "activity":
            continue
        pos += 1
        act = _activity(a, pos, rules, entities)
        if act.name in used:
            continue
        used.add(act.name)
        svc.activities.append(act)
    for act in svc.activities:
        if act.kind == "router":
            svc.has_router = True
            for t, target in act.transitions.items():
                svc.transitions.setdefault(t, target)
    return svc


def _parse_services(text: str, source: str, rules: ConvertRules) -> tuple[list[ServiceDef], bool, list[str]]:
    """Every service of the file in document order, whether it was read tolerantly, unresolved entity references."""
    root, recovered = _parse_xml(text)
    entities: list[str] = []
    if root is None or not isinstance(root.tag, str):
        return [], recovered, entities
    out: list[ServiceDef] = []
    inside: set[Any] = set()
    for el in root.iter():
        if _local(el) != "service" or el in inside:
            continue
        # 서비스 안에 다시 들어 있는 service 요소는 따로 읽지 않는다
        inside.update(e for e in el.iter() if e is not el)
        if any(_local(e) == "activity" for e in el.iter()):
            out.append(_activity_service(el, source, rules, entities))
        elif _is_bean(el):
            name = str(el.get("name") or el.get("id") or "").strip() or _fallback_name(source, rules)
            out.append(ServiceDef(name=name, source=source, dialect="bean"))
        elif el is root:
            out.append(_activity_service(el, source, rules, entities))
    return out, recovered, list(dict.fromkeys(entities))


def parse_services(text: str, source: str, rules: ConvertRules) -> list[ServiceDef]:
    """서비스 XML 본문에 있는 서비스를 모두 읽어 문서 순서대로 반환한다.
    activity 형식과 bean 형식의 서비스는 각각 레코드 하나가 된다.
    서비스가 하나도 없거나 XML로 전혀 읽을 수 없으면 빈 목록을 반환한다."""
    return _parse_services(text, source, rules)[0]


def parse_service_xml(text: str, source: str, rules: ConvertRules) -> ServiceDef | None:
    """서비스 XML 본문을 읽어 ``ServiceDef``를 반환한다.
    bean 형식이면 이름과 ``dialect="bean"``만 채워서 반환한다.
    서비스가 여럿이면 첫째를 반환한다. 서비스 정의가 아니거나 XML로 전혀 읽을 수 없으면 None을 반환한다.
    서비스 이름이 없으면 파일 이름에서 ``-service.xml``을 뗀 이름을 쓴다."""
    found = _parse_services(text, source, rules)[0]
    return found[0] if found else None


# ---- DB 사용 판단 ---------------------------------------------------------------


def _strip_java_comments(text: str) -> str:
    return _JAVA_NOISE.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", text)


def _dao_call_pattern(rules: ConvertRules) -> re.Pattern[str]:
    names = [re.escape(n.strip()).replace(r"\*", r"\w*") for n in rules.strs("activities.dao_call_methods") if n.strip()]
    alt = "|".join(sorted(names, key=len, reverse=True)) or r"(?!)"
    return re.compile(r"(?<![\w$])(" + alt + r")\s*\(\s*\"([^\"\\\n]*)\"")


def _external_words(rules: ConvertRules) -> list[tuple[str, list[str]]]:
    raw = rules.get("activities.external_calls", {}) or {}
    out: list[tuple[str, list[str]]] = []
    if isinstance(raw, dict):
        for tag, words in raw.items():
            ws = [words] if isinstance(words, str) else list(words or [])
            out.append((str(tag), [str(w).strip().lower() for w in ws if str(w).strip()]))
    return out


def classify_activity(activity: Activity, mappers: MapperIndex, java_text: str | None,
                      rules: ConvertRules) -> DbVerdict:
    """activity가 DB를 쓰는지 판단한다. 표의 위에서부터 처음 맞는 단계로 정한다.
    ``java_text``는 activity 클래스의 소스다. 소스를 찾지 못했으면 None을 넘긴다.
    소스가 서로 다른 SQL 문을 둘 이상 호출하면 DB를 쓰지 않는 것으로 보고 ambiguous를 반환한다.

    ``mappers`` is not consulted (the resource step resolves ``statement``); it is in the signature so that a later
    rule may check a 2a id against the mappers without changing callers. ``statement`` is the ``sql-key`` for 1a / 1b
    / 1c (possibly empty for 1a / 1b) and the one ``"ns.id"`` for 2a."""
    del mappers
    cls = str(activity.class_name or "").strip()
    fqn = cls.lower()
    simple = _simple(cls)
    sql_key = activity.properties.get(rules.text("activities.sql_key", "sql-key"), "").strip()

    word = rules.text("activities.router_word", "router").lower()
    if word and word in fqn:
        return DbVerdict(False, "not_db", "router", "",
                         f"Step 0: the class name {cls!r} contains {word!r}, so the activity is a router.")
    if activity.kind == "user":
        return DbVerdict(False, "not_db", "user", "",
                         f"Step 0: the class {cls!r} is a session-handling (user) activity.")
    for tag, words in _external_words(rules):
        hit = next((w for w in words if w in fqn), "")
        if hit:
            return DbVerdict(False, "external_call", tag, "",
                             f"Step 0: the class name {cls!r} contains the external-call word {hit!r} (tag {tag}).")

    low_simple = simple.lower()
    builtin = next((p for p in rules.strs("activities.builtin_prefixes")
                    if p.strip() and low_simple.startswith(p.strip().lower())), "")
    if builtin and activity.kind in ("search", "update"):
        return DbVerdict(True, "certain", "1a", sql_key,
                         f"Step 1a: {simple!r} is a built-in {activity.kind} activity of the framework.")
    extra = next((p for p in rules.strs("activities.extra_db_prefixes")
                  if p.strip() and low_simple.startswith(p.strip().lower())), "")
    if extra:
        return DbVerdict(True, "certain", "1b", sql_key,
                         f"Step 1b: {simple!r} starts with the company DB-activity prefix {extra!r}.")
    if sql_key:
        return DbVerdict(True, "strong", "1c", sql_key,
                         f"Step 1c: the activity {activity.name!r} has the sql-key {sql_key!r}.")

    if java_text is None:
        return DbVerdict(False, "ambiguous", "3", "",
                         f"Step 3: the Java source of {cls or activity.name!r} was not found.")
    code = _strip_java_comments(java_text)
    ids: list[str] = []
    calls: dict[str, str] = {}
    for m in _dao_call_pattern(rules).finditer(code):
        ident = m.group(2).strip()
        if "." not in ident:
            continue
        if ident not in calls:
            ids.append(ident)
            calls[ident] = m.group(1)
    if len(ids) == 1:
        return DbVerdict(True, "strong", "2a", ids[0],
                         f"Step 2a: the source calls {calls[ids[0]]}(\"{ids[0]}\", ...).")
    if len(ids) > 1:
        shown = ", ".join(f'"{i}"' for i in ids[:5])
        return DbVerdict(False, "ambiguous", "3", "",
                         f"Step 3: the source calls {len(ids)} different statements ({shown}); "
                         f"which one the activity means is not clear.")
    # 근거에는 맞은 표시 중 가장 긴 것을 쓴다. NamedParameterJdbcTemplate 안에는 JdbcTemplate도 들어 있다
    markers = sorted((mk for mk in rules.strs("activities.db_api_markers") if mk), key=len, reverse=True)
    marker = next((mk for mk in markers if mk in code), "")
    if marker:
        return DbVerdict(True, "strong", "2b", "",
                         f"Step 2b: the source uses the database API {marker!r} but names no statement.")
    return DbVerdict(False, "not_db", "java_only", "",
                     f"The source of {cls or activity.name!r} was found and shows no trace of a database; "
                     f"its business code must be carried over by hand.")


# ---- 파일 전체 분해 --------------------------------------------------------------


class _JavaIndex:
    def __init__(self, java_files: list[str]) -> None:
        self.files = sorted(dict.fromkeys(str(p).replace("\\", "/") for p in java_files))

    def find(self, class_name: str) -> str:
        cls = str(class_name or "").strip().split("$", 1)[0]
        if not cls:
            return ""
        suffix = cls.replace(".", "/") + ".java"
        hits = [p for p in self.files if p == suffix or p.endswith("/" + suffix)]
        if hits:
            return hits[0]
        base = _simple(cls) + ".java"
        hits = [p for p in self.files if PurePosixPath(p).name == base]
        return hits[0] if len(hits) == 1 else ""


def _service_key(name: str, rules: ConvertRules) -> str:
    return naming.x_name(naming.strip_suffix(name, rules.text("naming.service_suffix", "-service"))) or name.lower()


def decompose_services(src_root: Path, service_files: list[str], java_files: list[str], mappers: MapperIndex,
                       rules: ConvertRules) -> ServiceResult:
    """서비스 XML 파일들을 읽어 서비스 목록, 인수인계 항목, 오류 목록을 반환한다.
    activity마다 클래스 소스를 ``java_files``에서 찾아 DB 사용을 판단하고 ``verdicts``에 넣는다.
    같은 서비스 이름이 두 파일에 있으면 앞 파일만 쓰고 뒤 파일은 오류로 남긴다.
    파일 순서는 경로 순서다.

    Paths in ``service_files`` / ``java_files`` are POSIX, relative to ``src_root``. Hand-over items: ``[<tag>]``,
    ``[2b]``, ``[3]``, ``[java_only]`` with component ``{service}::{activity}``, the evidence after the tag, the source
    path and its first 2,000 characters when found; ``[BEAN_DIALECT]`` with component = the file."""
    root = Path(src_root)
    res = ServiceResult()
    seen: dict[str, str] = {}
    index = _JavaIndex(java_files)
    sources: dict[str, str | None] = {}

    def load(rel: str) -> str | None:
        if rel not in sources:
            try:
                sources[rel] = _decode((root / rel).read_bytes())
            except OSError as e:
                res.errors.append(f"{rel}: cannot read the Java source ({e.strerror or e})")
                sources[rel] = None
        return sources[rel]

    for rel in sorted(dict.fromkeys(str(p).replace("\\", "/") for p in service_files)):
        try:
            text = _decode((root / rel).read_bytes())
        except OSError as e:
            res.errors.append(f"{rel}: cannot read the service XML ({e.strerror or e})")
            continue
        found, recovered, entities = _parse_services(text, rel, rules)
        if not found:
            res.errors.append(f"{rel}: not a service definition, or not readable as XML")
            continue
        if recovered:
            res.errors.append(f"{rel}: the XML is malformed and was read tolerantly; check its activities")
        if entities:
            res.errors.append(f"{rel}: the entity references {', '.join(entities)} in element text are not resolved; "
                              f"the values keep them as text")
        for svc in found:
            key = _service_key(svc.name, rules)
            if key in seen:
                res.errors.append(f"{rel}: the service {svc.name!r} is already defined in {seen[key]}; "
                                  f"this definition is ignored")
                continue
            seen[key] = rel
            res.services.append(svc)
            if svc.dialect == "bean":
                res.queue.append(QueueItem(component=rel, source_path=rel, excerpt=text[:EXCERPT_CHARS],
                                           reason=f"[BEAN_DIALECT] the service {svc.name!r} is written in the bean "
                                                  f"dialect, which is not converted; rewrite it by hand"))
                continue
            for act in svc.activities:
                src = index.find(act.class_name) if act.kind not in NO_METHOD_KINDS else ""
                java_text = load(src) if src else None
                if java_text is None:
                    src = ""
                verdict = classify_activity(act, mappers, java_text, rules)
                svc.verdicts[act.name] = verdict
                if verdict.confidence == "external_call" or verdict.pass_hit in HANDOVER_HITS:
                    res.queue.append(QueueItem(component=f"{svc.name}::{act.name}",
                                               reason=f"[{verdict.pass_hit}] {verdict.evidence}", source_path=src,
                                               excerpt=(java_text or "")[:EXCERPT_CHARS]))
    return res
