"""pom.xml 편집기(lxml) — 서식·주석·줄 끝·인코딩을 지키면서 pom
을 고친다(``PomEditorLike`` 구현, sbmig/core/interfaces.py). 쓰는 곳: 재구성 단계(restructurer.py)의 모든 pom
수정(사내 부모 pom 교체, 모듈 목록, 필수 / 선택 사내 아티팩트, java 버전, 플러그인), 수정 스크립트 helper API
의 ``p.pom(path)`` (sbmig/script/api.py), pom 변경 비교(sbmig/build/depchanges.py). 핵심 진입점:
``PomEditor(path)`` 로 읽고 조회·수정 메서드를 부른 뒤 ``save()``. 수정 메서드(``@_mutator``)는 문서가 실제로
바뀌었을 때만 True — 같은 호출을 두 번 하면 두 번째는 False(멱등: 재실행해도 diff 가 없다). 지킬 것: 사람이
손으로 고친 pom 처럼 보여야 한다(최소 diff). XML 선언·루트 앞 주석·끝부분은 바이트 그대로, 들여쓰기는 파일에서
찾은 단위, 새 섹션은 Maven 관례 순서(``PROJECT_ORDER``
등)에 넣는다. 기본 POM 네임스페이스가 있든 없든 동작한다.

Formatting/comment-preserving pom.xml editor (lxml based). Implements ``PomEditorLike``.

Design notes
------------
* The document is parsed with lxml keeping comments, CDATA and whitespace. On output we keep the original
  prolog (XML declaration, comments/doctype before the root) and epilog byte-for-byte, and we restore the
  original root start tag (lxml would normalise its line breaks between attributes).
* Inserted elements are indented with the indent unit detected from the file (default 4 spaces, tabs supported);
  new top-level sections are placed following the conventional Maven element order.
* Every mutator returns True iff the serialised document changed, so calling it twice returns False the 2nd time.
* Works with poms in the default POM namespace and with poms without a namespace.


"""
from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from lxml import etree

from sbmig.analyze.pom_parser import pom_encoding, xml_codec, xml_without_declaration
from sbmig.core.models import GAV, PluginInfo

log = logging.getLogger("sbmig.restructure.pom")

POM_NS = "http://maven.apache.org/POM/4.0.0"
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"

PROJECT_ORDER = [
    "modelVersion", "parent", "groupId", "artifactId", "version", "packaging", "name", "description", "url",
    "inceptionYear", "organization", "licenses", "developers", "contributors", "mailingLists", "prerequisites",
    "modules", "scm", "issueManagement", "ciManagement", "distributionManagement", "properties",
    "dependencyManagement", "dependencies", "repositories", "pluginRepositories", "build", "reporting", "profiles",
]
PARENT_ORDER = ["groupId", "artifactId", "version", "relativePath"]
DEP_ORDER = ["groupId", "artifactId", "version", "type", "classifier", "scope", "systemPath", "exclusions", "optional"]
PLUGIN_ORDER = ["groupId", "artifactId", "version", "extensions", "executions", "dependencies", "goals",
                "inherited", "configuration"]
BUILD_ORDER = ["sourceDirectory", "scriptSourceDirectory", "testSourceDirectory", "outputDirectory",
               "testOutputDirectory", "extensions", "defaultGoal", "resources", "testResources", "directory",
               "finalName", "filters", "pluginManagement", "plugins"]
COORD_ORDER = ["groupId", "artifactId", "version"]

_ORDERS: dict[str, list[str]] = {
    "project": PROJECT_ORDER, "parent": PARENT_ORDER, "dependency": DEP_ORDER, "plugin": PLUGIN_ORDER,
    "build": BUILD_ORDER, "path": COORD_ORDER, "dependencyManagement": ["dependencies"],
    "pluginManagement": ["plugins"],
}

_PROLOG_RE = re.compile(r"\A(?:\s+|<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>)*", re.DOTALL)
_START_TAG_RE = re.compile(r"<([^\s/>]+)(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*\s*(/?)>", re.DOTALL)
_ENC_RE = re.compile(rb"^\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")
_PROP_REF_RE = re.compile(r"^\$\{([^}]+)\}$")

T = TypeVar("T")


class PomEditError(Exception):
    """깨졌거나 지원하지 않는 pom(읽기 실패, 디코딩 실패, XML 오류, 루트가 <project> 아님). 재구성은 이 예외를
    잡아 ok=False 로 보고한다.

    Raised for malformed / unsupported pom files."""


@dataclass
class ArtifactRef:
    """pom 안에서 groupId / artifactId / version 을 가진 요소 하나(의존성, dependencyManagement, 플러그인
    의존성, annotationProcessorPath) — 선택 사내 아티팩트 버전 올림이 이 목록을 돈다.

    An element carrying groupId/artifactId/version somewhere in the pom (for version bumps)."""
    section: str                 # dependencies | dependencyManagement | plugin-dependency | annotationProcessorPath
    element: Any                 # lxml element
    group_id: str
    artifact_id: str
    version: str | None

    @property
    def key(self) -> str:
        """``groupId:artifactId`` (버전 없는 비교 키)."""
        return f"{self.group_id}:{self.artifact_id}"


def property_ref(value: str | None) -> str | None:
    """값이 통째로 ``${이름}`` 하나면 그 속성 이름, 아니면 None (``1.0-${x}`` 처럼 섞인 값은 None).

    '${foo.version}' -> 'foo.version', else None."""
    if not value:
        return None
    m = _PROP_REF_RE.match(value.strip())
    return m.group(1) if m else None


def _is_el(node: Any) -> bool:
    return isinstance(node.tag, str)


def _local(node: Any) -> str:
    return etree.QName(node).localname if _is_el(node) else ""


def _mutator(fn: Callable[..., Any]) -> Callable[..., bool]:
    """수정 메서드 데코레이터: 호출 전후 직렬화 결과를 비교해 실제로 바뀌었을 때만 True. 호출부는 이 값으로
    바뀐 경우에만 노트를 남긴다.

    Wrap a mutating method: returns True iff the serialised document changed."""
    def wrapper(self: PomEditor, *args: Any, **kwargs: Any) -> bool:
        before = etree.tostring(self._root)
        fn(self, *args, **kwargs)
        return etree.tostring(self._root) != before
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class PomEditor:
    """pom.xml 하나를 제자리에서 고친다. ``PomEditor(path)`` 가 읽고 ``save()`` 가 다시 쓴다(``text=`` 로
    문자열에서 만들 수도 있음).

    Edit one pom.xml in place. ``PomEditor(path)`` reads the file; ``save()`` writes it back."""

    def __init__(self, path: str | Path, text: str | None = None):
        """``path`` 의 파일을 읽거나(``text`` 가 없을 때) 주어진 문자열을 쓴다. 읽기 실패는 PomEditError."""
        self.path = Path(path)
        if text is not None:
            # 문자열로 받은 pom 은 XML 선언의 encoding 으로 다시 바이트로
            # 만든다: 파일에서 읽은 것과 같은 길(_load)로 파싱하려고
            m = _ENC_RE.match(text.encode("ascii", "replace"))
            # v2.1h: Java 이름(x-windows-949)·한글 계열(EUC-KR / MS949 → cp949)도 파이썬 코덱으로
            raw = text.encode((xml_codec(m.group(1).decode()) if m else None) or "utf-8", "xmlcharrefreplace")
        else:
            try:
                raw = self.path.read_bytes()
            except OSError as e:
                raise PomEditError(f"cannot read {self.path}: {e}") from e
        self._load(raw)

    @classmethod
    def from_string(cls, text: str, path: str | Path = "pom.xml") -> PomEditor:
        """문자열 pom 으로 편집기를 만든다(파일에 쓰기 전 검사·비교용). ``path`` 는 저장
        위치·메시지용 이름일 뿐이다."""
        return cls(path, text=text)

    # ------------------------------------------------------------------ load / serialise
    def _load(self, raw: bytes) -> None:
        """바이트 → lxml 트리. BOM, XML 선언의 encoding, 줄 끝(CRLF), 루트 앞 prolog / 뒤 epilog, 원래 루트
        시작 태그를 기억해 둔다 — 저장할 때 바꾸지 않은 부분이 바이트 그대로 남게."""
        # UTF-8 BOM 은 떼어 두었다가 save 때 다시 붙인다(Windows 편집기로 저장된 회사 pom 이 그대로 남게)
        self._bom = raw.startswith(b"\xef\xbb\xbf")
        if self._bom:
            raw = raw[3:]
        m = _ENC_RE.match(raw)
        self.encoding = m.group(1).decode() if m else "utf-8"       # the declared name, as written
        # v2.1h (A1 의 재구성 쪽): 선언 이름으로 바로 풀지 않는다 — MS949 / x-windows-949 는 lxml·파이썬이 모르고,
        # EUC-KR 선언 + cp949 확장 한글("똠"), UTF-8 선언 + cp949 바이트는 엄격히 풀리지 않아 재구성이 ok=False 가
        # 됐다. 분석기와 같은 규칙(pom_encoding: BOM → UTF-8 → 선언(한글 계열 cp949) → 후보)으로 풀고, 저장은 선언이
        # 가리키는 계열(xml_codec, EUC-KR 계열은 cp949)로 — 선언이 없거나 모르는 이름이면 푼 코덱 그대로.
        # Decode like the analyzer (`pom_encoding`), save in the declared family (`xml_codec`; the codec read with when
        # there is no / an unknown declaration; UTF-8 when the file has a UTF-8 BOM).
        codec = pom_encoding(raw)
        try:
            text = raw.decode(codec)
        except (LookupError, UnicodeDecodeError) as e:
            raise PomEditError(f"{self.path}: cannot decode as {codec} (declared {self.encoding}): {e}") from e
        self.codec = "utf-8" if self._bom else (xml_codec(m.group(1).decode()) if m else None) or codec
        # CRLF 파일은 안에서 LF 로 다루고 저장할 때 CRLF 로 되돌린다:
        # 줄 끝 때문에 git diff 가 파일 전체 변경이 되지 않게
        self.newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        # 공백·주석·CDATA 를 모두 유지(최소 diff). resolve_entities=False / no_network=True: pom 의 DOCTYPE 로
        # 외부 파일·네트워크를 읽지 않는다
        parser = etree.XMLParser(remove_blank_text=False, strip_cdata=False, resolve_entities=False,
                                 remove_comments=False, remove_pis=False, huge_tree=True, no_network=True)
        try:
            # 선언을 뗀 텍스트를 UTF-8 바이트로: 선언의 encoding(EUC-KR / MS949 / x-windows-949)이 바이트와 어긋나거나
            # lxml 이 모르는 이름이어도 파싱된다. 원래 선언 줄은 prolog(아래)에 그대로 남아 저장된다
            root = etree.fromstring(xml_without_declaration(text).encode("utf-8"), parser)
        except etree.XMLSyntaxError as e:
            raise PomEditError(f"{self.path}: malformed XML: {e}") from e
        if root is None or _local(root) != "project":
            raise PomEditError(f"{self.path}: root element is not <project>")
        self._root = root
        self._ns = etree.QName(root).namespace
        # lxml 은 루트 시작 태그 속성 사이의 줄바꿈을 정규화한다: 원래 시작 태그 문자열과 앞뒤 텍스트를
        # 보관했다가 to_string 에서 되돌린다
        pm = _PROLOG_RE.match(text)
        pos = pm.end() if pm else 0
        sm = _START_TAG_RE.match(text, pos)
        self._prolog = text[:pos]
        self._start_tag = sm.group(0) if sm else None
        self._self_closing = bool(sm and sm.group(2))
        if sm and not self._self_closing:
            close = text.rfind(f"</{sm.group(1)}")
            end = text.find(">", close)
            self._epilog = text[end + 1:] if close >= 0 and end >= 0 else "\n"
        else:
            self._epilog = text[sm.end():] if sm else "\n"
        self.indent_unit = self._detect_indent()
        self._original = self.to_string()

    def _detect_indent(self) -> str:
        """파일의 들여쓰기 단위(탭 / 공백 n 개)를 찾는다. 새로 넣는 요소가 주변과 같은 들여쓰기를
        갖게 하려는 것(기본 공백 4).

        Indent unit from the whitespace before the first child of <project> (fallback: deeper levels)."""
        candidates: list[str] = []
        for node in self._root.iter():
            if not _is_el(node) or len(node) == 0:
                continue
            ws = node.text or ""
            if "\n" in ws and not ws.strip():
                depth = self._depth(node) + 1
                ind = ws.rsplit("\n", 1)[1]
                if ind and depth:
                    if ind.startswith("\t"):
                        return "\t"
                    if len(ind) % depth == 0:
                        candidates.append(" " * (len(ind) // depth))
            if len(candidates) >= 3:
                break
        return candidates[0] if candidates else "    "

    def to_string(self) -> str:
        """현재 문서를 문자열로: 보관한 prolog + 원래 루트 시작 태그 + 본문 + epilog,
        원래 줄 끝(CRLF 면 CRLF)."""
        body = etree.tostring(self._root, encoding="unicode", with_tail=False)
        if self._start_tag and not self._self_closing:
            gt = body.find(">")
            if gt > 0 and not body[:gt + 1].endswith("/>"):
                body = self._start_tag + body[gt + 1:]
        out = self._prolog + body + self._epilog
        if self.newline != "\n":
            out = out.replace("\n", self.newline)
        return out

    def save(self, path: str | Path | None = None) -> None:
        """원래 인코딩(+ BOM)으로 저장한다 — XML 선언이 가리키는 계열(``self.codec``: EUC-KR / MS949 / x-windows-949 는
        cp949), 선언 줄은 그대로. ``path`` 를 주면 그 위치에 쓰고, 자기 파일에 쓴 경우에만
        '변경 없음' 기준을 갱신한다."""
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_string().encode(self.codec, "xmlcharrefreplace")
        target.write_bytes((b"\xef\xbb\xbf" if self._bom else b"") + data)
        if path is None or target == self.path:
            self._original = self.to_string()

    @property
    def changed(self) -> bool:
        """읽은 뒤(또는 마지막 저장 뒤) 문서가 바뀌었는가.

        True if the document differs from what was loaded / last saved."""
        return self.to_string() != self._original

    def clone_to(self, path: str | Path) -> PomEditor:
        """현재 내용을 ``path`` 에 쓰고 그 사본의 편집기를 돌려준다.

        Write the current content to `path` and return an editor for the copy."""
        self.save(path)
        return PomEditor(path)

    # ------------------------------------------------------------------ low level helpers
    @property
    def root(self) -> Any:
        """lxml 루트 요소(<project>)."""
        return self._root

    @property
    def namespace(self) -> str | None:
        """루트의 XML 네임스페이스(기본 POM 네임스페이스 또는 None)."""
        return self._ns

    def q(self, name: str) -> str:
        """로컬 이름 → 이 문서의 네임스페이스를 붙인 태그 이름(새 요소를 만들 때)."""
        return f"{{{self._ns}}}{name}" if self._ns else name

    def _depth(self, el: Any) -> int:
        d = 0
        p = el.getparent()
        while p is not None:
            d += 1
            p = p.getparent()
        return d

    def _ind(self, depth: int) -> str:
        return "\n" + self.indent_unit * depth

    @staticmethod
    def child(el: Any, name: str) -> Any:
        """``el`` 의 자식 중 로컬 이름이 ``name`` 인 첫 요소(없으면 None, ``el`` 이 None 이어도 None)."""
        if el is None:
            return None
        for c in el:
            if _is_el(c) and _local(c) == name:
                return c
        return None

    @staticmethod
    def children(el: Any, name: str | None = None) -> list[Any]:
        """``el`` 의 자식 요소 목록(``name`` 을 주면 그 이름만; 주석 노드는 빠진다)."""
        if el is None:
            return []
        return [c for c in el if _is_el(c) and (name is None or _local(c) == name)]

    @staticmethod
    def text_of(el: Any, name: str | None = None) -> str | None:
        """요소(또는 그 자식 ``name``)의 앞뒤 공백을 뺀 텍스트(요소가 없으면 None)."""
        target = PomEditor.child(el, name) if name else el
        if target is None:
            return None
        return (target.text or "").strip()

    @staticmethod
    def local_name(el: Any) -> str:
        """네임스페이스를 뺀 태그 이름."""
        return _local(el)

    def find(self, path: str) -> Any:
        """<project> 아래 슬래시 경로의 요소(예: ``build/pluginManagement/plugins``), 없으면 None.

        Element at a slash path of local names below <project>, e.g. 'build/pluginManagement/plugins'."""
        el = self._root
        for part in [p for p in path.split("/") if p]:
            el = self.child(el, part)
            if el is None:
                return None
        return el

    def ensure_path(self, path: str) -> Any:
        """``find`` 와 같지만 없는 요소는 Maven 관례 순서 위치에 만들어 가며 내려간다.

        Like find() but creates missing elements (placed in conventional Maven order)."""
        el = self._root
        for part in [p for p in path.split("/") if p]:
            nxt = self.child(el, part)
            if nxt is None:
                nxt = self._insert_ordered(el, part)
            el = nxt
        return el

    def _order_for(self, parent: Any) -> list[str]:
        return _ORDERS.get(_local(parent), [])

    def _sep_like(self, ref: Any, depth: int) -> str:
        """``ref`` 뒤에 새 요소를 넣을 때 쓸 구분 공백(줄바꿈 1~2개 + 들여쓰기). 빈 줄로 섹션을 나눈
        파일이면 그 모양을 따른다.

        Separator (newlines + indent) to use after `ref` for an element at `depth`."""
        tail = ref.tail or ""
        n = tail.count("\n") if not tail.strip() else 1
        n = max(1, min(2, n))
        if ref.getnext() is None:      # ref is last: its tail is the closing indent → look at previous separator
            prev = ref.getprevious()
            pt = (prev.tail or "") if prev is not None else ""
            n = max(1, min(2, pt.count("\n"))) if prev is not None and not pt.strip() else 1
        return "\n" * n + self.indent_unit * depth

    def _insert_after(self, ref: Any, new: Any) -> None:
        """떼어 낸 ``new`` 를 ``ref`` 바로 뒤에 넣고 공백(tail)을 ``ref`` 의 모양에 맞춘다(마지막 자식이면
        닫는 태그 들여쓰기를 넘겨받는다).

        Insert detached `new` right after `ref`, giving it ref's separator style."""
        parent = ref.getparent()
        depth = self._depth(ref)
        sep = self._sep_like(ref, depth)
        if ref.getnext() is None:
            new.tail = ref.tail if ref.tail is not None and not ref.tail.strip() else self._ind(depth - 1)
        else:
            new.tail = ref.tail
        ref.tail = sep
        parent.insert(parent.index(ref) + 1, new)

    def _insert_first(self, parent: Any, new: Any) -> None:
        """``new`` 를 ``parent`` 의 첫 자식으로 넣고 여는 태그 뒤 공백과 ``new`` 의
        tail 을 들여쓰기에 맞춘다."""
        depth = self._depth(parent) + 1
        if len(parent) == 0:
            new.tail = self._ind(depth - 1)
        else:
            t = parent.text or ""
            new.tail = t if not t.strip() and "\n" in t else self._ind(depth)
        parent.text = self._ind(depth)
        parent.insert(0, new)

    def _append(self, parent: Any, new: Any) -> None:
        nodes = list(parent)
        if nodes:
            self._insert_after(nodes[-1], new)
        else:
            self._insert_first(parent, new)

    def _position_new(self, parent: Any, el: Any, order: list[str] | None = None) -> None:
        """새 요소를 관례 순서(``order``)에 맞는 자리로 옮긴다: 순서상 앞서는 마지막 형제
        뒤, 모르는 이름은 맨 뒤.

        `el` (created with SubElement, or detached) goes to its conventional position; whitespace is fixed.
        Known names go after the last sibling that precedes them in `order`; unknown names go last."""
        if el.getparent() is parent:
            parent.remove(el)
        order = order if order is not None else self._order_for(parent)
        name = _local(el)
        if name not in order:
            self._append(parent, el)
            return
        idx = order.index(name)
        ref = None
        for c in parent:
            if _is_el(c) and _local(c) in order and order.index(_local(c)) <= idx:
                ref = c
        if ref is not None:
            self._insert_after(ref, el)
        elif len(parent) == 0:
            self._append(parent, el)
        else:
            self._insert_first(parent, el)

    def _insert_ordered(self, parent: Any, name: str, text: str | None = None) -> Any:
        el = etree.SubElement(parent, self.q(name))
        if text is not None:
            el.text = text
        self._position_new(parent, el)
        return el

    def _append_new(self, parent: Any, name: str) -> Any:
        el = etree.SubElement(parent, self.q(name))
        parent.remove(el)
        self._append(parent, el)
        return el

    def reindent(self, el: Any) -> None:
        """우리가 만들거나 복사한 요소 안의 공백을 이 파일의 들여쓰기로 다시 맞춘다(다른 pom 에서 복사한
        요소의 들여쓰기가 달라도 정렬되게).

        Normalise whitespace inside `el` (an element we created or copied) to the file's indentation."""
        depth = self._depth(el)
        kids = list(el)
        if not kids:
            return
        if el.text is None or not el.text.strip():
            el.text = self._ind(depth + 1)
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            if c.tail is None or not c.tail.strip():
                blank = c.tail is not None and c.tail.count("\n") >= 2 and not last
                c.tail = ("\n" if blank else "") + self._ind(depth if last else depth + 1)
            if _is_el(c):
                self.reindent(c)

    def remove_node(self, el: Any) -> None:
        """요소(또는 주석)를 지우고 주변 들여쓰기를 정리한다 — 빈 줄이나 어긋난 닫는 태그가 남지 않게.

        Remove an element (or comment) and keep the surrounding indentation tidy."""
        parent = el.getparent()
        if parent is None:
            return
        prev = el.getprevious()
        if el.getnext() is None:
            if prev is not None:
                prev.tail = el.tail
            else:
                parent.text = el.tail if (el.tail or "").strip() == "" else parent.text
        parent.remove(el)
        if len(parent) == 0 and parent.text is not None and not parent.text.strip():
            parent.text = None

    def _remove_if_empty(self, el: Any) -> None:
        if el is not None and el.getparent() is not None and not self.children(el) and not (el.text or "").strip() \
                and not any(not _is_el(c) for c in el):
            self.remove_node(el)

    def _set_child_text(self, parent: Any, name: str, value: str | None) -> None:
        """텍스트 자식 ``name`` 을 설정한다(None 이면 삭제, 없으면 관례 순서로 추가). 값이 이미 같으면
        손대지 않는다 — 수정 메서드가 '변경 없음' 을 정확히 돌려주게."""
        c = self.child(parent, name)
        if value is None:
            if c is not None:
                self.remove_node(c)
            return
        if c is None:
            self._insert_ordered(parent, name, value)
        elif (c.text or "").strip() != value or len(c):
            for k in list(c):
                c.remove(k)
            c.text = value

    def set_child_text(self, parent: Any, name: str, value: str | None) -> None:
        """공개 저수준 helper: 임의 요소의 텍스트 자식 ``name`` 을 설정(None 이면 삭제,
        없으면 관례 순서로 추가).

        Public low-level helper: set/remove the text child `name` of an arbitrary element (ordered insert)."""
        self._set_child_text(parent, name, value)

    def remove_if_empty(self, el: Any) -> None:
        """자식도 텍스트도 주석도 없는 요소면 지운다(빈 <dependencies/> 같은 껍데기를 남기지 않기 위해)."""
        self._remove_if_empty(el)

    def _build_value(self, parent: Any, name: str, value: Any) -> Any:
        """Append <name> with a value built from str/bool/int/dict/list to `parent` (no positioning)."""
        el = etree.SubElement(parent, self.q(name))
        self._fill(el, name, value)
        return el

    def _fill(self, el: Any, name: str, value: Any) -> None:
        """파이썬 값 → 하위 요소: bool 은 true/false, dict 는 자식 요소, list 는 단수형 이름의 반복
        요소(``goals`` → ``goal``)."""
        if value is None:
            return
        if isinstance(value, bool):
            el.text = "true" if value else "false"
        elif isinstance(value, dict):
            for k, v in value.items():
                self._build_value(el, str(k), v)
        elif isinstance(value, (list, tuple)):
            item_name = _singular(name)
            for item in value:
                if isinstance(item, dict) and len(item) == 1:
                    (k, v), = item.items()
                    self._build_value(el, str(k), v)
                else:
                    self._build_value(el, item_name, item)
        else:
            el.text = str(value)

    def copy_into(self, parent: Any, src: Any, *, ordered: bool = False) -> Any:
        """요소 / 주석 ``src`` (다른 pom, 다른 네임스페이스여도 됨)를 깊은 복사해 ``parent`` 의 마지막
        자식으로(``ordered`` 면 관례 위치에) 넣고 들여쓰기를 맞춘다.

        Deep-copy element/comment `src` (possibly from another pom, other namespace) as last child of `parent`
        (or at its conventional position when `ordered`)."""
        new = self._copy_node(parent, src)
        if ordered and _is_el(new):
            self._position_new(parent, new)
        else:
            parent.remove(new)
            self._append(parent, new)
        if _is_el(new):
            self.reindent(new)
        return new

    def _copy_node(self, parent: Any, src: Any) -> Any:
        """네임스페이스를 이 문서 것으로 바꿔 가며 재귀 복사한다(네임스페이스 없는
        pom ↔ 있는 pom 사이 복사)."""
        if not _is_el(src):
            node = copy.copy(src)
            node.tail = None
            parent.append(node)
            return node
        el = etree.SubElement(parent, self.q(_local(src)))
        for k, v in src.attrib.items():
            el.set(k, v)
        el.text = src.text
        for c in src:
            n = self._copy_node(el, c)
            n.tail = c.tail
        return el

    # ------------------------------------------------------------------ coordinates
    def artifact_id(self) -> str | None:
        """이 pom 자신의 artifactId(부모 것 아님)."""
        return self.text_of(self._root, "artifactId")

    def group_id(self) -> str | None:
        """이 pom 에 직접 적힌 groupId(없으면 None — 부모에서 물려받는 경우)."""
        return self.text_of(self._root, "groupId")

    def version(self) -> str | None:
        """이 pom 에 직접 적힌 version(없으면 None — 부모에서 물려받는 경우)."""
        return self.text_of(self._root, "version")

    def effective_group_id(self) -> str | None:
        """groupId, 없으면 <parent> 의 groupId(Maven 상속 규칙과 같음)."""
        g = self.group_id()
        p = self.get_parent()
        return g or (p.group_id if p else None)

    def effective_version(self) -> str | None:
        """version, 없으면 <parent> 의 version."""
        v = self.version()
        p = self.get_parent()
        return v or (p.version if p else None)

    @_mutator
    def set_coordinate(self, name: str, value: str | None) -> None:
        """이 pom 자신의 groupId / artifactId / version / name / description 을 설정하거나(None 이면) 지운다.

        Set/remove the project's own groupId / artifactId / version / name / description."""
        self._set_child_text(self._root, name, value)

    def packaging(self) -> str:
        """<packaging> 값(없으면 Maven 기본값 jar)."""
        return self.text_of(self._root, "packaging") or "jar"

    # ------------------------------------------------------------------ parent
    def get_parent(self) -> GAV | None:
        """<parent> 의 GAV(없으면 None)."""
        p = self.child(self._root, "parent")
        if p is None:
            return None
        return GAV(group_id=self.text_of(p, "groupId") or "", artifact_id=self.text_of(p, "artifactId") or "",
                   version=self.text_of(p, "version") or None)

    def parent_relative_path(self) -> str | None:
        """<parent><relativePath> 값: None = 요소 없음(Maven 기본 ../pom.xml), '' = 빈
        <relativePath/>(저장소에서만 찾음).

        None = element absent (Maven default ../pom.xml); '' = <relativePath/>."""
        p = self.child(self._root, "parent")
        return self.text_of(p, "relativePath") if p is not None else None

    @_mutator
    def set_parent(self, gav: GAV, relative_path: str | None = None) -> None:
        """<parent> 를 ``gav`` 로 설정(없으면 만든다). ``relative_path`` 가 None 이나 '' 이면
        빈 <relativePath/> — 사내 부모 pom 은 저장소(Nexus)에서 받으므로 Maven 이 ../pom.xml
        을 먼저 뒤지지 않게 한다."""
        p = self.child(self._root, "parent")
        if p is None:
            p = self._insert_ordered(self._root, "parent")
        self._set_child_text(p, "groupId", gav.group_id)
        self._set_child_text(p, "artifactId", gav.artifact_id)
        self._set_child_text(p, "version", gav.version)
        rp = self.child(p, "relativePath")
        # None / '' → 빈 <relativePath/>: 부모를 디렉토리에서 찾지 말고 저장소에서 받으라는 뜻(사내 부모 pom)
        if relative_path is None or relative_path == "":
            if rp is None:
                self._insert_ordered(p, "relativePath")
            elif (rp.text or "").strip() or len(rp):
                for k in list(rp):
                    rp.remove(k)
                rp.text = None
        else:
            self._set_child_text(p, "relativePath", relative_path)

    @_mutator
    def set_parent_relative_path(self, relative_path: str | None) -> None:
        """기존 <parent> 의 <relativePath> 만 바꾼다('' → 빈 <relativePath/>, None → 요소 삭제). 부모가
        없으면 아무것도 안 한다.

        Only change <relativePath> of an existing parent ('' → <relativePath/>, None → remove element)."""
        p = self.child(self._root, "parent")
        if p is None:
            return
        if relative_path is None:
            self._set_child_text(p, "relativePath", None)
        elif relative_path == "":
            rp = self.child(p, "relativePath")
            if rp is None:
                self._insert_ordered(p, "relativePath")
            elif (rp.text or "").strip():
                rp.text = None
        else:
            self._set_child_text(p, "relativePath", relative_path)

    @_mutator
    def remove_parent(self) -> None:
        """<parent> 를 지운다."""
        p = self.child(self._root, "parent")
        if p is not None:
            self.remove_node(p)

    @_mutator
    def set_packaging(self, packaging: str) -> None:
        """<packaging> 을 설정한다. jar 는 기본값이라 요소가 없을 때 굳이 추가하지 않는다(최소 diff)."""
        cur = self.child(self._root, "packaging")
        # jar 는 Maven 기본값: <packaging> 이 없던 pom 에 jar 를 굳이 적지 않는다(최소 diff)
        if cur is None and packaging == "jar":
            return
        self._set_child_text(self._root, "packaging", packaging)

    # ------------------------------------------------------------------ modules
    def modules(self) -> list[str]:
        """<modules> 의 <module> 값 목록(선언 순서)."""
        return [(m.text or "").strip() for m in self.children(self.child(self._root, "modules"), "module")]

    @_mutator
    def set_modules(self, modules: list[str]) -> None:
        """<modules> 를 주어진 목록으로 통째로 바꾼다(빈 목록이면 <modules> 삭제, 같으면 변경 없음)."""
        if self.modules() == list(modules):
            return
        mods = self.child(self._root, "modules")
        if not modules:
            if mods is not None:
                self.remove_node(mods)
            return
        if mods is None:
            mods = self._insert_ordered(self._root, "modules")
        for c in list(mods):
            mods.remove(c)
        mods.text = None
        for m in modules:
            el = self._append_new(mods, "module")
            el.text = m

    @_mutator
    def add_module(self, module: str) -> None:
        """<module> 하나를 끝에 추가(이미 있으면 그대로)."""
        if module in self.modules():
            return
        mods = self.child(self._root, "modules")
        if mods is None:
            mods = self._insert_ordered(self._root, "modules")
        self._append_new(mods, "module").text = module

    # ------------------------------------------------------------------ properties
    def properties(self) -> dict[str, str]:
        """<properties> 의 {이름: 값}."""
        return {_local(c): (c.text or "").strip() for c in self.children(self.child(self._root, "properties"))}

    def get_property(self, key: str) -> str | None:
        """속성 값 하나(없으면 None)."""
        return self.properties().get(key)

    def has_property(self, key: str) -> bool:
        """이 pom 의 <properties> 에 속성이 있는가(부모 것은 보지 않는다)."""
        return key in self.properties()

    @_mutator
    def set_property(self, key: str, value: str) -> None:
        """속성을 설정(있으면 값만 바꾸고, 없으면 <properties> 끝에 추가)."""
        props = self.child(self._root, "properties")
        if props is None:
            props = self._insert_ordered(self._root, "properties")
        for c in self.children(props):
            if _local(c) == key:
                if (c.text or "").strip() != value:
                    c.text = value
                return
        self._append_new(props, key).text = value

    @_mutator
    def remove_property(self, key: str) -> None:
        """속성을 지운다."""
        props = self.child(self._root, "properties")
        for c in self.children(props):
            if _local(c) == key:
                self.remove_node(c)

    def resolve(self, value: str | None) -> str | None:
        """``${prop}`` 을 이 pom 의 속성과 project.version / groupId / artifactId 로 한 단계만 푼다(best
        effort; 못 푼 것은 그대로).

        Resolve ${prop} references against this pom's properties (single level, best effort)."""
        if value is None:
            return None
        props = self.properties()
        builtins = {"project.version": self.effective_version() or "",
                    "project.groupId": self.effective_group_id() or "",
                    "project.artifactId": self.artifact_id() or ""}
        def repl(m: re.Match[str]) -> str:
            k = m.group(1)
            return props.get(k, builtins.get(k, m.group(0)))
        return re.sub(r"\$\{([^}]+)\}", repl, value)

    # ------------------------------------------------------------------ dependencies
    def _deps_container(self, managed: bool, create: bool = False) -> Any:
        """<dependencies> 또는 <dependencyManagement><dependencies> 컨테이너(``create`` 면
        없을 때 관례 위치에 만든다)."""
        if managed:
            dm = self.find("dependencyManagement/dependencies")
            if dm is None and create:
                dm = self.ensure_path("dependencyManagement/dependencies")
            return dm
        d = self.child(self._root, "dependencies")
        if d is None and create:
            d = self._insert_ordered(self._root, "dependencies")
        return d

    def dependency_elements(self, managed: bool = False) -> list[Any]:
        """<dependencies> (``managed`` 면 <dependencyManagement><dependencies>)의 <dependency> 요소들."""
        return self.children(self._deps_container(managed), "dependency")

    def _gav_of(self, el: Any) -> GAV:
        opt = (self.text_of(el, "optional") or "").lower() == "true"
        return GAV(group_id=self.text_of(el, "groupId") or "", artifact_id=self.text_of(el, "artifactId") or "",
                   version=self.text_of(el, "version") or None, scope=self.text_of(el, "scope") or None,
                   type=self.text_of(el, "type") or None, classifier=self.text_of(el, "classifier") or None,
                   optional=opt)

    def gav_of(self, el: Any) -> GAV:
        """<dependency> 요소 → GAV(version, scope, type, classifier, optional 포함)."""
        return self._gav_of(el)

    def _find_deps(self, group_id: str, artifact_id: str, managed: bool) -> list[Any]:
        return [e for e in self.dependency_elements(managed)
                if self.text_of(e, "groupId") == group_id and self.text_of(e, "artifactId") == artifact_id]

    def dependencies(self) -> list[GAV]:
        """직접 선언된 의존성의 GAV 목록."""
        return [self._gav_of(e) for e in self.dependency_elements(False)]

    def managed_dependencies(self) -> list[GAV]:
        """<dependencyManagement> 의 GAV 목록(BOM import 포함)."""
        return [self._gav_of(e) for e in self.dependency_elements(True)]

    def has_dependency(self, group_id: str, artifact_id: str) -> bool:
        """직접 의존성에 이 g:a 가 있는가."""
        return bool(self._find_deps(group_id, artifact_id, False))

    def has_managed_dependency(self, group_id: str, artifact_id: str) -> bool:
        """<dependencyManagement> 에 이 g:a 가 있는가."""
        return bool(self._find_deps(group_id, artifact_id, True))

    def _write_dep(self, el: Any, gav: GAV) -> None:
        """기존 <dependency> 요소에 GAV 를 덮어쓴다(값이 있는 항목만; 없는 항목은 지우지 않는다)."""
        self._set_child_text(el, "groupId", gav.group_id)
        self._set_child_text(el, "artifactId", gav.artifact_id)
        if gav.version:
            self._set_child_text(el, "version", gav.version)
        if gav.type:
            self._set_child_text(el, "type", gav.type)
        if gav.classifier:
            self._set_child_text(el, "classifier", gav.classifier)
        if gav.scope:
            self._set_child_text(el, "scope", gav.scope)
        if gav.optional:
            self._set_child_text(el, "optional", "true")

    def _add_dep(self, gav: GAV, managed: bool, if_absent: bool, first: bool = False) -> None:
        """의존성을 추가한다. 이미 있으면 ``if_absent`` 일 때 그대로 두고, 아니면 그 선언을 ``gav`` 로
        덮어쓴다. ``first`` 는 맨 앞(BOM import 를 다른 관리 항목보다 앞에 두기 위함)."""
        existing = self._find_deps(gav.group_id, gav.artifact_id, managed)
        if existing:
            if if_absent:
                return
            self._write_dep(existing[0], gav)
            return
        cont = self._deps_container(managed, create=True)
        el = etree.SubElement(cont, self.q("dependency"))
        cont.remove(el)
        if first and self.children(cont):
            first_el = self.children(cont)[0]
            prev = first_el.getprevious()
            if prev is None:
                self._insert_first(cont, el)
            else:
                self._insert_after(prev, el)
        else:
            self._append(cont, el)
        for name, val in (("groupId", gav.group_id), ("artifactId", gav.artifact_id), ("version", gav.version),
                          ("type", gav.type), ("classifier", gav.classifier), ("scope", gav.scope),
                          ("optional", "true" if gav.optional else None)):
            if val:
                etree.SubElement(el, self.q(name)).text = val
        self.reindent(el)

    @_mutator
    def add_dependency(self, gav: GAV, if_absent: bool = True) -> None:
        """직접 의존성 추가(기본: 이미 있으면 그대로)."""
        self._add_dep(gav, managed=False, if_absent=if_absent)

    @_mutator
    def add_managed_dependency(self, gav: GAV, if_absent: bool = True, first: bool = False) -> None:
        """<dependencyManagement> 에 추가. BOM import 는 GAV(type='pom', scope='import'), ``first`` 면 맨 위 —
        BOM import 끼리 같은 g:a 를 관리하면 먼저 선언된 BOM 이 이기므로 순서가 의미를 가진다.

        Add to <dependencyManagement>; BOM imports: GAV(type='pom', scope='import'). `first` puts it on top."""
        self._add_dep(gav, managed=True, if_absent=if_absent, first=first)

    @_mutator
    def remove_dependency(self, group_id: str, artifact_id: str) -> None:
        """직접 의존성에서 이 g:a 를 모두 지우고, 비면 <dependencies> 도 지운다."""
        cont = self._deps_container(False)
        for e in self._find_deps(group_id, artifact_id, False):
            self.remove_node(e)
        self._remove_if_empty(cont)

    @_mutator
    def remove_all_dependencies(self) -> None:
        """<dependencies> 를 통째로 지운다(루트를 aggregator 로 만들 때)."""
        cont = self._deps_container(False)
        if cont is not None:
            self.remove_node(cont)

    @_mutator
    def remove_managed_dependency(self, group_id: str, artifact_id: str) -> None:
        """<dependencyManagement> 에서 이 g:a 를 지우고, 비면 껍데기도 지운다."""
        cont = self._deps_container(True)
        for e in self._find_deps(group_id, artifact_id, True):
            self.remove_node(e)
        self._remove_if_empty(cont)
        dm = self.child(self._root, "dependencyManagement")
        self._remove_if_empty(dm)

    def _set_version_el(self, el: Any, version: str | None) -> None:
        """의존성 같은 요소의 버전을 설정한다. 버전이 이 pom 의 ``${prop}`` 을 가리키면 속성 값을 고친다 —
        같은 속성을 쓰는 다른 선언도 함께 맞고 pom 모양이 유지된다.

        Set the version of a dependency-like element, going through a locally defined ${prop}."""
        cur = self.text_of(el, "version")
        if version is None:
            self._set_child_text(el, "version", None)
            return
        prop = property_ref(cur)
        if prop and self.has_property(prop):
            self.set_property(prop, version)
            return
        self._set_child_text(el, "version", version)

    @_mutator
    def set_dependency_version(self, group_id: str, artifact_id: str, version: str | None) -> None:
        """직접 + 관리 의존성의 버전을 바꾼다. None 이면 직접 의존성의 <version> 을 지우고 로컬 관리 항목도
        빼서 부모가 버전을 관리하게 한다.

        Direct + managed. version=None removes <version> from direct deps and drops the local managed entry
        (the version is then managed by the parent)."""
        for e in self._find_deps(group_id, artifact_id, False):
            self._set_version_el(e, version)
        if version is None:
            for e in self._find_deps(group_id, artifact_id, True):
                self.remove_node(e)
            self._remove_if_empty(self._deps_container(True))
            self._remove_if_empty(self.child(self._root, "dependencyManagement"))
        else:
            for e in self._find_deps(group_id, artifact_id, True):
                self._set_version_el(e, version)

    @_mutator
    def replace_dependency(self, old_group: str, old_artifact: str, new: GAV) -> None:
        """g:a 를 제자리에서 새 좌표로 바꾼다(직접 + 관리; scope / exclusions 유지). 새 좌표가 이미 선언돼
        있으면 옛 것만 지운다 — 같은 의존성이 두 번 선언되지 않게.

        Rename g:a in place (direct + managed); keeps scope/exclusions; sets version/scope/type when given."""
        for managed in (False, True):
            olds = self._find_deps(old_group, old_artifact, managed)
            if not olds:
                continue
            if (old_group, old_artifact) != (new.group_id, new.artifact_id) and \
                    self._find_deps(new.group_id, new.artifact_id, managed):
                # 이름만 바꾸면 같은 g:a 가 두 번 선언된다(Maven 경고, 버전 충돌) — 옛 선언을 지운다
                for e in olds:                 # target already declared → drop the old one
                    self.remove_node(e)
                continue
            for e in olds:
                self._set_child_text(e, "groupId", new.group_id)
                self._set_child_text(e, "artifactId", new.artifact_id)
                if new.version:
                    self._set_child_text(e, "version", new.version)
                if new.scope:
                    self._set_child_text(e, "scope", new.scope)
                if new.type:
                    self._set_child_text(e, "type", new.type)
                if new.classifier:
                    self._set_child_text(e, "classifier", new.classifier)

    # ------------------------------------------------------------------ generic artifact references
    def artifact_refs(self) -> list[ArtifactRef]:
        """GAV 를 가진 모든 위치(의존성, 관리 의존성, 플러그인 의존성, annotationProcessorPaths) — 선택 사내
        아티팩트는 어디에 선언돼 있든 올려야 하므로.

        All groupId/artifactId/version carriers: deps, managed deps, plugin deps, annotationProcessorPaths."""
        out: list[ArtifactRef] = []

        def add(section: str, el: Any) -> None:
            out.append(ArtifactRef(section, el, self.text_of(el, "groupId") or "",
                                   self.text_of(el, "artifactId") or "", self.text_of(el, "version") or None))

        for e in self.dependency_elements(False):
            add("dependencies", e)
        for e in self.dependency_elements(True):
            add("dependencyManagement", e)
        for p in self.plugin_elements("all"):
            for d in self.children(self.child(p, "dependencies"), "dependency"):
                add("plugin-dependency", d)
            for path in self.children(self.find_in(p, "configuration/annotationProcessorPaths")):
                add("annotationProcessorPath", path)
        return out

    def find_in(self, el: Any, path: str) -> Any:
        """임의 요소 ``el`` 아래 슬래시 경로의 요소(없으면 None)."""
        for part in [p for p in path.split("/") if p]:
            el = self.child(el, part)
            if el is None:
                return None
        return el

    @_mutator
    def set_ref_version(self, el: Any, version: str | None) -> None:
        """의존성 같은 요소에 버전 문자열을 그대로 적는다(None 이면 <version> 삭제; 속성을 따라가지 않음).

        Literal version on a dependency-like element (None removes <version>)."""
        self._set_child_text(el, "version", version)

    @_mutator
    def remove_version(self, el: Any) -> None:
        """의존성 같은 요소의 <version> 만 지운다(부모 체인 / dependencyManagement 가 관리). 그 버전이
        가리키던 속성은 그대로 둔다.

        Drop the `<version>` child of a dependency-like element (the version is then managed by the parent
        chain / dependencyManagement). Properties the version referenced are left alone."""
        self._set_child_text(el, "version", None)

    @_mutator
    def rename_ref(self, el: Any, group_id: str, artifact_id: str) -> None:
        """의존성 같은 요소의 groupId / artifactId 를 바꾼다(별칭 → 정식 좌표)."""
        self._set_child_text(el, "groupId", group_id)
        self._set_child_text(el, "artifactId", artifact_id)

    # ------------------------------------------------------------------ plugins
    def plugin_elements(self, where: str = "build") -> list[Any]:
        """<plugin> 요소들: ``build`` = <build><plugins>, ``management`` = pluginManagement, ``all`` = 둘 다.

        where: build | management | all"""
        out: list[Any] = []
        if where in ("build", "all"):
            out += self.children(self.find("build/plugins"), "plugin")
        if where in ("management", "all"):
            out += self.children(self.find("build/pluginManagement/plugins"), "plugin")
        return out

    def plugin_key(self, el: Any) -> str:
        """플러그인의 ``groupId:artifactId`` (groupId 가 없으면 Maven 기본 org.apache.maven.plugins)."""
        return f"{self.text_of(el, 'groupId') or DEFAULT_PLUGIN_GROUP}:{self.text_of(el, 'artifactId') or ''}"

    def _find_plugins(self, group_id: str, artifact_id: str, where: str) -> list[Any]:
        key = f"{group_id or DEFAULT_PLUGIN_GROUP}:{artifact_id}"
        return [p for p in self.plugin_elements(where) if self.plugin_key(p) == key]

    def find_plugins(self, group_id: str, artifact_id: str, where: str = "all") -> list[Any]:
        """이 g:a 의 플러그인 요소들(기본: build + pluginManagement 모두)."""
        return self._find_plugins(group_id, artifact_id, where)

    def _plugin_info(self, el: Any) -> PluginInfo:
        conf = self.child(el, "configuration")
        return PluginInfo(group_id=self.text_of(el, "groupId") or DEFAULT_PLUGIN_GROUP,
                          artifact_id=self.text_of(el, "artifactId") or "",
                          version=self.text_of(el, "version") or None,
                          configuration=_to_value(conf) if conf is not None and len(conf) else {})

    def plugins(self) -> list[PluginInfo]:
        """<build><plugins> 의 PluginInfo 목록(configuration 은 dict 로)."""
        return [self._plugin_info(p) for p in self.plugin_elements("build")]

    def managed_plugins(self) -> list[PluginInfo]:
        """pluginManagement 의 PluginInfo 목록."""
        return [self._plugin_info(p) for p in self.plugin_elements("management")]

    def has_plugin(self, group_id: str, artifact_id: str, where: str = "build") -> bool:
        """이 g:a 플러그인이 있는가(기본: <build><plugins> 만)."""
        return bool(self._find_plugins(group_id, artifact_id, where))

    @_mutator
    def add_plugin(self, plugin: PluginInfo, if_absent: bool = True,
                   executions: list[dict[str, Any]] | None = None) -> None:
        """<build><plugins> 에 추가한다. 이미 있으면 ``if_absent`` 일 때 그대로, 아니면 버전과(다를 때만)
        configuration 을 바꾼다 — 설정이 같으면 손대지 않아 두 번 불러도 변경이 없다.

        Add to <build><plugins>. `configuration` dict → nested elements; `executions` e.g.
        [{"goals": ["repackage"]}] → <executions><execution><goals><goal>repackage</goal>…"""
        existing = self._find_plugins(plugin.group_id, plugin.artifact_id, "build")
        if existing:
            if if_absent:
                return
            el = existing[0]
            if plugin.version:
                self._set_child_text(el, "version", plugin.version)
            if plugin.configuration:
                old = self.child(el, "configuration")
                # 기존 설정과 같으면 그대로 둔다: 같은 호출을 다시 해도 문서가 바뀌지 않아야 한다(멱등)
                if old is None or _to_value(old) != _stringify(plugin.configuration):
                    if old is not None:
                        self.remove_node(old)
                    conf = self._build_value(el, "configuration", plugin.configuration)
                    self._position_new(el, conf)
                    self.reindent(conf)
            return
        plugins = self.ensure_path("build/plugins")
        el = etree.SubElement(plugins, self.q("plugin"))
        plugins.remove(el)
        self._append(plugins, el)
        etree.SubElement(el, self.q("groupId")).text = plugin.group_id or DEFAULT_PLUGIN_GROUP
        etree.SubElement(el, self.q("artifactId")).text = plugin.artifact_id
        if plugin.version:
            etree.SubElement(el, self.q("version")).text = plugin.version
        if executions:
            ex = etree.SubElement(el, self.q("executions"))
            for e in executions:
                self._build_value(ex, "execution", e)
        if plugin.configuration:
            self._build_value(el, "configuration", plugin.configuration)
        self.reindent(el)

    @_mutator
    def set_plugin_version(self, group_id: str, artifact_id: str, version: str) -> None:
        """build + pluginManagement 의 이 플러그인 버전을 바꾼다(로컬 ``${prop}`` 이면 속성을)."""
        for p in self._find_plugins(group_id, artifact_id, "all"):
            self._set_version_el(p, version)

    @_mutator
    def remove_plugin(self, group_id: str, artifact_id: str) -> None:
        """build + pluginManagement 에서 이 플러그인을 지우고, 비게 된 <plugins> /
        <pluginManagement> / <build> 도 지운다."""
        for p in self._find_plugins(group_id, artifact_id, "all"):
            parent = p.getparent()
            self.remove_node(p)
            self._remove_if_empty(parent)
        self._remove_if_empty(self.find("build/pluginManagement"))
        self._remove_if_empty(self.child(self._root, "build"))

    @_mutator
    def remove_build_plugins_except(self, keys: Iterable[str]) -> None:
        """<build><plugins> 에서 ``keys`` 에 없는 플러그인을 모두 지운다(pluginManagement 는 그대로).

        Remove every <build><plugins> plugin whose g:a is not in `keys` (pluginManagement untouched)."""
        keep = set(keys)
        for p in self.plugin_elements("build"):
            if self.plugin_key(p) not in keep:
                self.remove_node(p)
        plugins = self.find("build/plugins")
        self._remove_if_empty(plugins)
        self._remove_if_empty(self.child(self._root, "build"))

    @_mutator
    def remove_element(self, path: str) -> None:
        """<project> 아래 슬래시 경로의 요소를 지우고(예: ``build/finalName``), 그 때문에 빈 부모도 지운다.

        Remove the element at slash path below <project> (e.g. 'build/finalName'); drops an emptied parent."""
        el = self.find(path)
        if el is not None:
            parent = el.getparent()
            self.remove_node(el)
            if parent is not self._root:
                self._remove_if_empty(parent)


# ---------------------------------------------------------------------- module helpers
def _singular(name: str) -> str:
    """목록 요소 이름의 단수형(``goals`` → ``goal``, ``dependencies`` → ``dependency``); 모르면 ``item``."""
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith(("ses", "xes")):
        return name[:-2]
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return "item"


def _to_value(el: Any) -> Any:
    """요소 → 파이썬 값(텍스트 / dict / list). 같은 이름만 반복되는 자식은 list(``<goals><goal>``).
    ``add_plugin`` 이 기존 configuration 과 새 설정을 비교할 때 쓴다."""
    kids = [c for c in el if _is_el(c)]
    if not kids:
        return (el.text or "").strip()
    names = [_local(c) for c in kids]
    if len(names) > 1 and len(set(names)) == 1:          # <goals><goal>a</goal><goal>b</goal></goals>
        return [_to_value(c) for c in kids]
    out: dict[str, Any] = {}
    for c in kids:
        k = _local(c)
        v = _to_value(c)
        if k in out:                                       # mixed repeated names → keep a list
            out[k] = (out[k] if isinstance(out[k], list) else [out[k]]) + [v]
        else:
            out[k] = v
    return out


def _stringify(value: Any) -> Any:
    """설정 dict 의 값을 XML 텍스트 형태(문자열, bool → true/false)로 맞춘다 — ``_to_value`` 결과와
    같은 모양으로 비교하기 위해."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return {str(k): _stringify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify(v) for v in value]
    return "" if value is None else str(value)
