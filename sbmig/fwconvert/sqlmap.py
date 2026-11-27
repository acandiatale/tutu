"""프레임워크의 SQL 파일을 MyBatis mapper로 바꾸고, 이미 있는 mapper는 복사하는 모듈.

Contract: docs/FW-CONVERT.md sections 2.1, 2.6, 3.2 and 3.5. Nothing here writes to disk: ``convert_mappers`` returns
``GeneratedFile`` records. A bad input never raises; it becomes ``SqlConversion.error`` / ``MapperResult.errors``.

Rules decided here where the specification is silent:
- Parameters: ``files.sql_named_attr`` (``isNamed``) ``true`` (case ignored) makes only ``:name`` a parameter; a
  ``?`` stays as written and is never numbered. A bare ``?`` left in code (outside literals and comments) is still a
  JDBC bind marker when MyBatis prepares the statement, so it fails at run time; such a statement is named in
  ``error`` instead of passing in silence. Any other value or no attribute: ``:name`` and ``?``
  are both replaced. Oracle-style numeric binds ``:1`` are left alone. Nothing is replaced inside single-quoted
  literals, Oracle q-quotes (``q'[…]'``, ``Q``, ``nq``; ``[] {} () <>`` pairs, any other delimiter closes itself),
  double-quoted identifiers, ``--`` and ``/* */`` comments. A literal ends only at an unpaired quote (``''`` is an
  escaped quote; a backslash is not an escape character). A body that ends inside an open literal, identifier or
  block comment is written and named in ``error``.
- Entity references: entities are never expanded (``resolve_entities=False``, no DTD loaded, no network). A reference
  lxml keeps as a node is written back as ``&name;`` text and named in ``error``; the tolerant read keeps unknown
  references as text and names them too.
- ``WITH``: the verb is the first ``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``MERGE`` outside parentheses
  after the CTE list, else select.
- ``useGeneratedKeys``: only for an ``INSERT INTO t (cols…)`` whose column list has no column named ``id`` (case
  ignored, quotes and table qualifiers removed). An insert without a column list (``VALUES`` / ``SELECT`` directly)
  supplies every column, so no key is generated and none is declared. ``MERGE`` (written as ``<insert>``) never gets
  it either.
- Two statements whose cut ids are equal: the first keeps the id, every later one gets ``{id}_{n}`` (n from 2, never
  an id that exists in the file) and the original id as a comment; ``error`` names them. A query without an id gets
  ``query{n}`` (its position) the same way. A query with an empty body is left out and named in ``error``.
- A file that is not well-formed is read again by a tolerant scanner (start / end tags by local name, CDATA kept as it
  is, the five XML entities and character references decoded, XML comments outside CDATA dropped). It is linear in
  the file size, and a file over ``TOLERANT_MAX_CHARS`` (8 Mi characters) is not read tolerantly at all. A clean
  tolerant read is not an error; an unclosed CDATA section / comment, a query without end tag or a query tag without
  ``>`` is. When nothing can be read the mapper holds one empty ``<sql id="emptyMapper"/>``: the MyBatis mapper
  DTD requires at least one child and MyBatis validates against it.
- ``convert_mappers``: SQL files first, then the copied mappers, each list in sorted order. A path taken already (case
  ignored) gives the next file ``-2``, ``-3`` …; a converted file is renamed as a resource (``order-2``) so that its
  namespace differs too, a copied one keeps its own namespace. Two outputs with one namespace are named in ``errors``
  (MyBatis refuses equal statement ids under one namespace). A copied mapper whose namespace cannot be read is not
  copied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lxml import etree

from sbmig.fwconvert import naming
from sbmig.fwconvert.model import GeneratedFile, Layout, MapperFile, Statement
from sbmig.fwconvert.rules import ConvertRules

MAPPER_DOCTYPE = ('<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" '
                  '"https://mybatis.org/dtd/mybatis-3-mapper.dtd">')
EMPTY_PLACEHOLDER = '<sql id="emptyMapper"></sql>'
STATEMENT_TAGS = ("select", "insert", "update", "delete")
_KEYWORD_VERBS = {"select": "select", "with": "select", "insert": "insert", "update": "update",
                  "delete": "delete", "merge": "insert"}


@dataclass
class SqlConversion:
    """SQL 파일 하나를 mapper로 바꾼 결과를 담는 클래스.
    ``error``가 비어 있지 않으면 사람이 확인해야 할 내용이 있다."""
    xml: str
    statements: list[Statement] = field(default_factory=list)
    error: str = ""


@dataclass
class MapperResult:
    """출력할 mapper 파일 전체와 그 목록, 오류를 담는 클래스."""
    files: list[GeneratedFile] = field(default_factory=list)
    mappers: list[MapperFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---- SQL 본문 읽기 --------------------------------------------------------------

# Every alternative is linear: a tempered dot or a negated class, and a missing terminator only at the end of the text
# (the ``*_end`` group is then None). Oracle q-quotes come first; ``q`` must not end an identifier.
_QQ = r"(?<![A-Za-z0-9_$#])[nN]?[qQ]'"
_LEX = re.compile(
    _QQ + r"\[(?:(?!\]').)*(?P<q1_end>\]')?"
    "|" + _QQ + r"\{(?:(?!\}').)*(?P<q2_end>\}')?"
    "|" + _QQ + r"\((?:(?!\)').)*(?P<q3_end>\)')?"
    "|" + _QQ + r"<(?:(?!>').)*(?P<q4_end>>')?"
    "|" + _QQ + r"(?P<qd>[^\s\[{(<'])(?:(?!(?P=qd)').)*(?P<q5_end>(?P=qd)')?"
    r"|'(?:[^']|'')*(?P<str_end>')?"
    r"|\"[^\"]*(?P<qid_end>\")?"
    r"|(?P<line>--[^\n]*)"
    r"|/\*(?:(?!\*/).)*(?P<block_end>\*/)?",
    re.DOTALL)
_PARAM = re.compile(r"\?|(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _lex_kind(m: re.Match[str]) -> tuple[str, bool]:
    """구간의 종류와 끝까지 닫혔는지를 반환한다."""
    text = m.group(0)
    if m.group("line") is not None:
        return "comment", True
    if text.startswith("/*"):
        return "comment", m.group("block_end") is not None
    if text.startswith('"'):
        return "qid", m.group("qid_end") is not None
    if text.startswith("'"):
        return "str", m.group("str_end") is not None
    closed = any(m.group(f"q{i}_end") is not None for i in range(1, 6))
    return "str", closed


def _spans(sql: str) -> list[tuple[str, int, int]]:
    """본문을 코드, 문자열, 따옴표 식별자, 주석 구간으로 나눈다."""
    out: list[tuple[str, int, int]] = []
    pos = 0
    for m in _LEX.finditer(sql):
        if m.start() > pos:
            out.append(("code", pos, m.start()))
        out.append((_lex_kind(m)[0], m.start(), m.end()))
        pos = m.end()
    if pos < len(sql):
        out.append(("code", pos, len(sql)))
    return out


def open_at_end(sql: str) -> str:
    """본문이 닫히지 않은 문자열이나 주석으로 끝나면 그 종류를 반환한다. 아니면 빈 문자열이다."""
    last = None
    for last in _LEX.finditer(sql):
        pass
    if last is None or last.end() != len(sql):
        return ""
    kind, closed = _lex_kind(last)
    if closed:
        return ""
    return "comment" if kind == "comment" else ("quoted identifier" if kind == "qid" else "string literal")


def _masked(sql: str, keep_qid: bool = False) -> str:
    """문자열과 주석을 같은 길이의 공백으로 지운 본문을 반환한다. 줄바꿈은 남긴다."""
    parts = []
    for kind, a, b in _spans(sql):
        seg = sql[a:b]
        if kind == "code" or (keep_qid and kind == "qid"):
            parts.append(seg)
        else:
            parts.append(re.sub(r"[^\n]", " ", seg))
    return "".join(parts)


def sql_verb(sql: str) -> str | None:
    """본문의 첫 SQL 키워드로 문장 종류를 정한다. 앞쪽 주석과 괄호는 건너뛴다.
    select, insert, update, delete 중 하나를 반환한다. ``MERGE``는 insert다.
    알 수 없는 키워드거나 키워드가 없으면 None을 반환한다."""
    masked = _masked(sql)
    m = _WORD.search(masked)
    if not m:
        return None
    word = m.group(0).lower()
    if word != "with":
        return _KEYWORD_VERBS.get(word)
    depth = 0
    for tok in re.finditer(r"[()]|[A-Za-z_][A-Za-z0-9_]*", masked[m.end():]):
        t = tok.group(0)
        if t == "(":
            depth += 1
        elif t == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and t.lower() in _KEYWORD_VERBS and t.lower() != "with":
            return _KEYWORD_VERBS[t.lower()]
    return "select"


def replace_params(sql: str, *, named_only: bool = False) -> tuple[str, list[str]]:
    """``:name``은 ``#{name}``으로, 남은 ``?``는 ``#{param1}``부터 차례로 바꾼다.
    ``named_only``가 True면 ``:name``만 바꾸고 ``?``는 그대로 둔다.
    문자열과 주석 안은 바꾸지 않는다. 바꾼 본문과 파라미터 이름 목록을 반환한다.
    목록은 처음 쓰인 순서이고 같은 이름은 한 번만 들어간다."""
    code = [(a, b) for kind, a, b in _spans(sql) if kind == "code"]
    pattern = _NAMED if named_only else _PARAM
    out: list[str] = []
    names: list[str] = []
    seen: set[str] = set()
    pos = 0
    seq = 0
    for a, b in code:
        for m in pattern.finditer(sql, a, b):
            if m.group(0) == "?":
                seq += 1
                name = f"param{seq}"
            else:
                name = m.group(1)
            out.append(sql[pos:m.start()])
            out.append("#{" + name + "}")
            pos = m.end()
            if name not in seen:
                seen.add(name)
                names.append(name)
    out.append(sql[pos:])
    return "".join(out), names


def _insert_generates_key(sql: str) -> bool:
    """INSERT의 열 목록에 ``id``가 없으면 True를 반환한다. 열 목록이 없으면 False다."""
    masked = _masked(sql, keep_qid=True)
    m = re.match(r"[\s(]*insert\s+(?:ignore\s+)?into\s+([^\s(]+)\s*(?:([A-Za-z_]\w*)\s*)?(\()?", masked, re.IGNORECASE)
    if not m or (m.group(1).lower() == "select"):
        return False
    alias = (m.group(2) or "").lower()
    if alias in ("values", "value", "select", "with", "set", "default") or not m.group(3):
        return False
    start = m.end()
    depth, end = 1, start
    while end < len(masked) and depth:
        if masked[end] == "(":
            depth += 1
        elif masked[end] == ")":
            depth -= 1
        end += 1
    inner = masked[start:end - 1] if depth == 0 else masked[start:]
    first = _WORD.search(inner)
    if first and first.group(0).lower() in ("select", "with"):
        return False
    cols, depth, cur = [], 0, []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            cols.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    cols.append("".join(cur))
    names = {c.strip().rsplit(".", 1)[-1].strip().strip('"`[]').lower() for c in cols}
    return "id" not in names


# ---- XML 쓰기 --------------------------------------------------------------------


def _attr(value: str) -> str:
    v = _BAD_XML_CHARS.sub("", value)
    return v.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;").replace(">", "&gt;")


def _comment(text: str) -> str:
    """XML 주석으로 쓸 수 있게 ``--``와 끝의 ``-``를 고친 문자열을 반환한다."""
    t = _BAD_XML_CHARS.sub("", str(text)).replace("\r", " ").replace("\n", " ")
    while "--" in t:
        t = t.replace("--", "- -")
    return f"<!-- {t}{' ' if t.endswith('-') else ''} -->"


def _cdata(body: str) -> str:
    return "<![CDATA[" + _BAD_XML_CHARS.sub("", body).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _mapper_xml(namespace: str, blocks: list[str]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', MAPPER_DOCTYPE, f'<mapper namespace="{_attr(namespace)}">']
    lines.extend(blocks)
    lines.append("</mapper>")
    return "\n".join(lines) + "\n"


def _empty_mapper(namespace: str, why: str) -> str:
    return _mapper_xml(namespace, ["    " + _comment(why), "    " + EMPTY_PLACEHOLDER])


# ---- SQL 파일 읽기 --------------------------------------------------------------

_DECL = re.compile(r"^\s*<\?xml\b[^>]*\?>", re.DOTALL)
_ENTITY = re.compile(r"&(lt|gt|amp|quot|apos|#[0-9]+|#x[0-9A-Fa-f]+);")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_NAMED_REF = re.compile(r"&([A-Za-z_][\w.-]*);")
_TAG_HEAD = re.compile(r"</?(?:[\w.-]+:)?([\w.-]+)")
# 속성 이름은 한 번에 끝까지 읽는다. 값이 없는 긴 이름도 다시 읽지 않는다
_ATTR = re.compile(r"""([^\s=/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?|.""", re.DOTALL)
# 이보다 큰 파일은 느슨하게 읽지 않는다
TOLERANT_MAX_CHARS = 8 * 1024 * 1024

# (속성, 본문, 본문에 남은 엔티티 이름)
_Query = tuple[dict[str, str], str, list[str]]


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_blank_text=False)


def _local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _parse(text: str):
    """선언부를 떼고 lxml로 읽는다. 잘 짜인 XML이 아니면 None을 반환한다."""
    body = _DECL.sub("", text.lstrip("﻿"), count=1)
    try:
        return etree.fromstring(body.encode("utf-8"), _parser())
    except (etree.XMLSyntaxError, ValueError):
        return None


def _decode_entities(text: str) -> str:
    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _ENTITIES:
            return _ENTITIES[name]
        try:
            return chr(int(name[2:], 16) if name[1] in "xX" else int(name[1:]))
        except (ValueError, OverflowError):
            return m.group(0)
    return _ENTITY.sub(one, text)


def _text_of(el, skip: tuple[str, ...] = (), entities: list[str] | None = None) -> str:
    """요소 안의 글을 이어 붙인다. 주석과 ``skip``에 있는 요소는 뺀다.
    풀지 않은 엔티티 참조는 ``&name;`` 글로 되돌려 넣고 ``entities``에 이름을 더한다. 같은 이름이 여러 번 들어갈 수 있다."""
    parts = [el.text or ""]
    for child in el:
        if isinstance(child, etree._Entity):
            parts.append(f"&{child.name};")
            if entities is not None:
                entities.append(child.name)
        elif isinstance(child.tag, str) and _local(child) not in skip:
            parts.append(_text_of(child, skip, entities))
        parts.append(child.tail or "")
    return "".join(parts)


def _read_strict(root, rules: ConvertRules) -> tuple[list[_Query] | None, str]:
    want_root = rules.text("files.sql_root", "queryMap")
    want_query = rules.text("files.sql_query", "query")
    if _local(root) != want_root:
        return None, f"the root element is <{_local(root)}>, not <{want_root}>"
    out: list[_Query] = []
    for el in root.iter():
        if _local(el) == want_query:
            ents: list[str] = []
            body = _text_of(el, entities=ents)
            out.append(({str(k): str(v) for k, v in el.attrib.items()}, body, list(dict.fromkeys(ents))))
    return out, ""


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR.finditer(raw):
        if m.group(1) is None:
            continue
        value = m.group(2) if m.group(2) is not None else m.group(3)
        if value is not None:
            out[m.group(1)] = _decode_entities(value)
    return out


def _read_tolerant(text: str, rules: ConvertRules) -> tuple[list[_Query], list[str]]:
    """잘 짜인 XML이 아닌 파일에서 query 요소를 찾아 읽는다. 본문의 ``<``, ``>``, ``&``는 그대로 둔다.
    query 요소 목록과 문제를 설명하는 메모 목록을 반환한다. 걸리는 시간은 파일 길이에 비례한다.

    One forward pass with ``str.find``: every search for a closing ``>`` is bounded by the next ``<``, an unclosed
    CDATA section or XML comment runs to the end once. Only start / end tags whose local name is the query name are
    structure; any other ``<`` is text. A ``>`` inside a quoted attribute value is not supported (ends the tag)."""
    qname = rules.text("files.sql_query", "query")
    n = len(text)
    out: list[_Query] = []
    notes: list[str] = []
    bad_tags = 0
    cur: tuple[dict[str, str], list[str]] | None = None

    def finish(q: tuple[dict[str, str], list[str]]) -> _Query:
        body = "".join(q[1])
        return q[0], body, list(dict.fromkeys(m.group(1) for m in _NAMED_REF.finditer(body)))

    def add_text(seg: str) -> None:
        if cur is not None and seg:
            cur[1].append(_decode_entities(seg))

    pos = 0
    while pos < n:
        lt = text.find("<", pos)
        if lt == -1:
            add_text(text[pos:])
            break
        add_text(text[pos:lt])
        if text.startswith("<![CDATA[", lt):
            end = text.find("]]>", lt + 9)
            if end == -1:
                notes.append("a CDATA section is not closed: it runs to the end of the file")
                content, pos = text[lt + 9:], n
            else:
                content, pos = text[lt + 9:end], end + 3
            if cur is not None:
                cur[1].append(content)
            continue
        if text.startswith("<!--", lt):
            end = text.find("-->", lt + 4)
            if end == -1:
                notes.append("an XML comment is not closed: the rest of the file is ignored")
                pos = n
            else:
                pos = end + 3
            continue
        nxt = text.find("<", lt + 1)
        stop = n if nxt == -1 else nxt
        head = _TAG_HEAD.match(text, lt, stop)
        if head and head.group(1) == qname:
            gt = text.find(">", head.end(), stop)
            if gt == -1:
                bad_tags += 1
                add_text(text[lt:stop])
                pos = stop
                continue
            raw = text[head.end():gt]
            if text[lt + 1] == "/":
                if cur is not None:
                    out.append(finish(cur))
                    cur = None
            else:
                if cur is not None:
                    notes.append(f"query {cur[0].get('id', '')} has no end tag")
                    out.append(finish(cur))
                    cur = None
                if raw.rstrip().endswith("/"):
                    out.append((_attrs(raw.rstrip()[:-1]), "", []))
                else:
                    cur = (_attrs(raw), [])
            pos = gt + 1
            continue
        add_text(text[lt:stop])
        pos = stop
    if cur is not None:
        notes.append(f"query {cur[0].get('id', '')} has no end tag")
        out.append(finish(cur))
    if bad_tags:
        notes.append(f"{bad_tags} <{qname}> tag(s) without a closing '>' were ignored")
    return out, notes


def _clean_body(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


# error에 넣는 메모 수의 상한. 나머지는 개수만 남긴다
MAX_NOTES = 20


def _join_notes(notes: list[str]) -> str:
    if len(notes) <= MAX_NOTES:
        return "; ".join(notes)
    return "; ".join(notes[:MAX_NOTES] + [f"and {len(notes) - MAX_NOTES} more"])


def _is_named(attrs: dict[str, str], rules: ConvertRules) -> bool:
    return str(attrs.get(rules.text("files.sql_named_attr", "isNamed"), "")).strip().lower() == "true"


def convert_sql_text(text: str, *, namespace: str, rules: ConvertRules) -> SqlConversion:
    """SQL 파일의 글을 MyBatis mapper XML로 바꾼다.
    잘 짜인 XML이 아니면 한 번 더 느슨하게 읽는다. 그래도 읽지 못하면 이유를 주석으로 단 빈 mapper를 반환하고 ``error``를 채운다.
    ``isNamed``가 true인 query는 ``:name``만 파라미터로 본다.
    같은 입력이면 늘 같은 글을 반환한다."""
    notes: list[str] = []
    text = str(text or "")
    root = _parse(text)
    queries: list[_Query] | None
    if root is not None:
        queries, why = _read_strict(root, rules)
        if queries is None:
            return SqlConversion(xml=_empty_mapper(namespace, f"not converted: {why}"), error=why)
    elif len(text) > TOLERANT_MAX_CHARS:
        why = f"not well-formed XML and larger than {TOLERANT_MAX_CHARS} characters: not read"
        return SqlConversion(xml=_empty_mapper(namespace, f"not converted: {why}"), error=why)
    else:
        queries, notes = _read_tolerant(text, rules)
        if not queries:
            why = _join_notes(["not well-formed XML and no query element could be read"] + notes)
            return SqlConversion(xml=_empty_mapper(namespace, f"not converted: {why}"), error=why)
    if not queries:
        return SqlConversion(xml=_empty_mapper(namespace, "the source file holds no query"))

    rows: list[tuple[str, str, str, bool]] = []      # (원본 id, 자른 id, 본문, isNamed)
    for n, (attrs, body, ents) in enumerate(queries, start=1):
        original = attrs.get("id", "").strip()
        cut = naming.last_token(original)
        if not cut:
            cut = f"query{n}"
            notes.append(f"query {n} has no id: named {cut}")
        if ents:
            shown = ", ".join(f"&{e};" for e in ents[:10]) + (f" and {len(ents) - 10} more" if len(ents) > 10 else "")
            notes.append(f"query {original or cut} uses the entity reference(s) {shown}: kept as text, not expanded")
        sql = _clean_body(body)
        if not sql:
            notes.append(f"query {original or cut} has an empty body: left out")
            continue
        rows.append((original, cut, sql, _is_named(attrs, rules)))

    taken = {cut for _, cut, _, _ in rows}
    used: set[str] = set()
    next_k: dict[str, int] = {}
    blocks: list[str] = []
    statements: list[Statement] = []
    for original, cut, sql, named in rows:
        ident = cut
        if ident in used:
            k = next_k.get(cut, 2)
            while f"{cut}_{k}" in taken or f"{cut}_{k}" in used:
                k += 1
            next_k[cut] = k + 1
            ident = f"{cut}_{k}"
            notes.append(f"duplicate statement id {cut} ({original or cut}): renamed {ident}")
        used.add(ident)
        opened = open_at_end(sql)
        if opened:
            notes.append(f"statement {ident} ends inside an open {opened}")
        keyword_verb = sql_verb(sql) or "select"
        body, params = replace_params(sql, named_only=named)
        # 이름 파라미터 query에 남은 ?는 드라이버가 바인드 자리로 읽는다. 실행할 때 실패하므로 error에 남긴다
        if named and any("?" in sql[a:b] for kind, a, b in _spans(sql) if kind == "code"):
            notes.append(f"statement {ident}: a '?' was left as it is in a named query (isNamed=true); "
                         "the database driver will read it as a parameter marker")
        attrs = [f'id="{_attr(ident)}"']
        if params:
            attrs.append('parameterType="map"')
        if keyword_verb == "select":
            attrs.append('resultType="java.util.Map"')
        if keyword_verb == "insert" and _insert_generates_key(sql):
            attrs.append('useGeneratedKeys="true" keyProperty="id"')
        if original != ident:
            blocks.append("    " + _comment(original or "(no id)"))
        blocks.append(f"    <{keyword_verb} {' '.join(attrs)}>{_cdata(chr(10) + body + chr(10) + '    ')}"
                      f"</{keyword_verb}>")
        statements.append(Statement(id=ident, original_id=original or ident, verb=keyword_verb, tag=keyword_verb,
                                    params=params))
    if not statements:
        why = _join_notes(notes) or "no statement"
        return SqlConversion(xml=_empty_mapper(namespace, f"not converted: {why}"), error=why)
    return SqlConversion(xml=_mapper_xml(namespace, blocks), statements=statements, error=_join_notes(notes))


# ---- 있던 mapper 읽기 -----------------------------------------------------------

_HASH_PARAM = re.compile(r"#\{\s*([^,}\s]+)")


def read_mapper(text: str) -> tuple[str, list[Statement]]:
    """MyBatis mapper의 namespace와 SQL 문 목록을 반환한다.
    ``verb``는 동적 태그를 풀어 이은 본문의 첫 SQL 키워드로 정한다. 키워드를 알 수 없으면 태그 이름을 쓴다.
    읽을 수 없는 글이면 빈 namespace와 빈 목록을 반환한다. 예외를 던지지 않는다."""
    try:
        root = _parse(str(text or ""))
        if root is None or _local(root) != "mapper":
            return "", []
        statements = []
        for el in root:
            tag = _local(el)
            if tag not in STATEMENT_TAGS:
                continue
            body = _text_of(el, skip=("selectKey",))
            params = list(dict.fromkeys(m.group(1) for m in _HASH_PARAM.finditer(body)))
            ident = str(el.get("id") or "").strip()
            statements.append(Statement(id=ident, original_id=ident, verb=sql_verb(body) or tag, tag=tag,
                                        params=params))
        return str(root.get("namespace") or "").strip(), statements
    except Exception:          # noqa: BLE001 - 어떤 글이 와도 예외를 던지지 않는다
        return "", []


# ---- 파일 단위 변환 --------------------------------------------------------------


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """파일 내용을 글로 바꾼다. utf-8, cp949, latin-1 순서로 시도한다.
    글과 읽은 인코딩 이름을 반환한다. 앞의 BOM은 뗀다."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


_DECL_ENC = re.compile(r"""^(\s*<\?xml\b[^>]*?\bencoding\s*=\s*)(["'])([^"']*)(\2)""", re.DOTALL)


def _utf8_declaration(text: str) -> str:
    m = _DECL_ENC.match(text)
    if not m or m.group(3).strip().lower().replace("_", "-") in ("utf-8", "utf8"):
        return text
    return text[:m.start(3)] + "UTF-8" + text[m.end(3):]


def _copied_resource(namespace: str, stem: str) -> str:
    last = namespace.rsplit(".", 1)[-1]
    for suffix in ("Store", "Mapper", "Dao", "DAO"):
        if last.endswith(suffix) and len(last) > len(suffix):
            return last[: -len(suffix)]
    return naming.strip_suffix(stem, "-mapper") or stem


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _numbered(path: str, n: int) -> str:
    p = PurePosixPath(path)
    return str(p.with_name(f"{p.stem}-{n}{p.suffix}"))


def convert_mappers(src_root: Path, sql_files: list[str], mapper_files: list[str], layout: Layout,
                    rules: ConvertRules) -> MapperResult:
    """SQL 파일은 mapper로 바꾸고, 있던 mapper는 UTF-8로 복사할 파일로 만든다.
    경로는 ``src_root`` 기준 POSIX 문자열이다. 디스크에는 쓰지 않는다.
    출력 경로가 겹치면 먼저 온 파일이 그 경로를 갖고, 다음 파일에는 번호를 붙인다. 이 일은 ``errors``에 남긴다.
    읽지 못한 파일은 건너뛰고 ``errors``에 남긴다."""
    result = MapperResult()
    taken: dict[str, str] = {}
    namespaces: dict[str, str] = {}
    ext = rules.text("files.sql_extension", ".sql")

    def read(rel: str) -> str | None:
        try:
            raw = (Path(src_root) / rel).read_bytes()
        except OSError as e:
            result.errors.append(f"{rel}: cannot read the file: {e.strerror or e}")
            return None
        return decode_bytes(raw)[0]

    def claim(path: str, rel: str) -> bool:
        if path.lower() in taken:
            return False
        taken[path.lower()] = rel
        return True

    def note_namespace(ns: str, rel: str) -> None:
        if ns in namespaces:
            result.errors.append(f"{rel}: namespace {ns} is used by {namespaces[ns]} too")
        else:
            namespaces[ns] = rel

    for rel in sorted(dict.fromkeys(sql_files)):
        text = read(rel)
        if text is None:
            continue
        name = PurePosixPath(rel).name
        base = name[: -len(ext)] if ext and name.lower().endswith(ext.lower()) else PurePosixPath(name).stem
        resource, n = base, 1
        while not claim(layout.mapper_path(resource), rel):
            n += 1
            resource = f"{base}-{n}"
        if n > 1:
            result.errors.append(f"{rel}: {layout.mapper_path(base)} is taken by "
                                 f"{taken[layout.mapper_path(base).lower()]}: written as {layout.mapper_path(resource)}")
        x = naming.x_name(resource)
        ns = layout.mapper_namespace(x)
        conv = convert_sql_text(text, namespace=ns, rules=rules)
        prefixes = [s.original_id.rpartition(".")[0] for s in conv.statements if "." in s.original_id]
        path = layout.mapper_path(resource)
        mf = MapperFile(resource=resource, x_name=x, namespace=ns, path=path, source=rel, origin="converted",
                        statements=conv.statements, keys=_unique([base, resource] + prefixes), error=conv.error)
        if conv.error:
            result.errors.append(f"{rel}: {conv.error}")
        note_namespace(ns, rel)
        result.mappers.append(mf)
        result.files.append(GeneratedFile(path=path, text=conv.xml))

    for rel in sorted(dict.fromkeys(mapper_files)):
        text = read(rel)
        if text is None:
            continue
        ns, statements = read_mapper(text)
        if not ns:
            result.errors.append(f"{rel}: not a readable MyBatis mapper with a namespace: not copied")
            continue
        stem = PurePosixPath(PurePosixPath(rel).name).stem
        first = f"{layout.mapper_dir}/{stem}.xml"
        path, n = first, 1
        while not claim(path, rel):
            n += 1
            path = _numbered(first, n)
        if n > 1:
            result.errors.append(f"{rel}: {first} is taken by {taken[first.lower()]}: written as {path}")
        resource = _copied_resource(ns, stem)
        mf = MapperFile(resource=resource, x_name=naming.x_name(resource), namespace=ns, path=path, source=rel,
                        origin="copied", statements=statements,
                        keys=_unique([resource, stem, ns, ns.rsplit(".", 1)[-1]]))
        note_namespace(ns, rel)
        result.mappers.append(mf)
        result.files.append(GeneratedFile(path=path, text=_utf8_declaration(text)))
    return result
