"""Boot로 바뀌는 legacy Spring 컨텍스트(XML 설정, web.xml)를 분석하고 고치는 순수 함수 모듈.
파일 읽기와 쓰기는 restructurer가 한다.
Spring Framework / servlet 프로젝트를 Boot 앱으로 바꾸면 빌드는 통과해도 기동할 때 깨지는 것들이 있다. 빈
덮어쓰기와 순환 참조 금지, JNDI 없음, WEB-INF가 classpath가 아님, 기본 서블릿 미등록,
CommonsMultipartResolver 삭제처럼 Boot의 기본값이 as-is와 다르기 때문이다.
빌드도 LLM도 기동 단계는 확인하지 못하므로 재구성이 직접 처리한다.
- ``Restructurer._generate_application``이 web.xml ``contextConfigLocation``으로 진입 XML을
  고르고(:func:`entry_locations`, :func:`resolve_location`) ``legacy-config/``에 복사한 파일을 이 모듈의
  함수로 고친다. as-is 원본은 고치지 않는다
- 클래스, 요소 이름과 Boot 속성 이름은 ``knowledge/config/legacy_context_rules.yml``에 있고, 스위치는
  ``layout.legacy_context.*``다

The legacy Spring context at conversion time (v2.1x, docs/INTERFACES.md "v2.1x"). Pure helpers used by the
restructurer: which XML files the Boot context imports (web.xml `contextConfigLocation`, `<import>` graph), and the
text edits applied to the sbmig-owned `legacy-config/` copies (never to an as-is file). Element / class names and the
Boot property names come from `knowledge/config/legacy_context_rules.yml`.


"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

log = logging.getLogger("sbmig.restructure")

RULES_FILE = "knowledge/config/legacy_context_rules.yml"
TODO_MARK = "MIGRATION-TODO (sbmig)"

_COMMENT_RE = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>", re.S)
# a start / end / empty tag (attribute values may hold '>' inside quotes)
_TAG_RE = re.compile(r"<(/?)([A-Za-z_][\w:.-]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'))?)*)\s*(/?)>", re.S)
_ATTR_RE = re.compile(r"([^\s=/>]+)\s*=\s*(\"[^\"]*\"|'[^']*')", re.S)


# ---------------------------------------------------------------------------------------------------------- rules
@dataclass
class LegacyContextRules:
    """``legacy_context_rules.yml`` 파일 하나의 내용을 담는 데이터 클래스(필드 이름이 YAML 키).
    파일이 없거나 깨져도 빈 기본값을 쓰므로 재구성은 멈추지 않는다.

    `knowledge/config/legacy_context_rules.yml` (field names = YAML keys); a missing / broken file gives empty
    defaults (never raises)."""
    listeners: list[str] = field(default_factory=list)
    root_default: str = ""
    dispatcher_servlets: list[str] = field(default_factory=list)
    servlet_default: str = ""
    annotation_context_classes: list[str] = field(default_factory=list)
    legacy_properties: list[dict[str, str]] = field(default_factory=list)
    jndi_elements: list[str] = field(default_factory=list)
    jndi_classes: list[str] = field(default_factory=list)
    jndi_todo: str = ""
    jdbc_artifacts: list[str] = field(default_factory=list)
    placeholder_elements: list[str] = field(default_factory=list)
    placeholder_classes: list[str] = field(default_factory=list)
    default_servlet_elements: list[str] = field(default_factory=list)
    default_servlet_java: list[str] = field(default_factory=list)
    default_servlet_property: str = ""
    default_servlet_value: str = ""
    multipart_classes: list[str] = field(default_factory=list)
    multipart_properties: dict[str, list[str]] = field(default_factory=dict)
    multipart_servlet_config: dict[str, str] = field(default_factory=dict)
    enable_web_mvc_java: list[str] = field(default_factory=list)
    view_resolver_classes: list[str] = field(default_factory=list)
    view_resolver_elements: list[str] = field(default_factory=list)
    resources_elements: list[str] = field(default_factory=list)
    web_initializer_java: list[str] = field(default_factory=list)
    log4j1_files: list[str] = field(default_factory=list)
    security_xml: list[str] = field(default_factory=list)
    security_elements: list[str] = field(default_factory=list)
    profile_params: list[str] = field(default_factory=list)
    web_libraries: dict[str, str] = field(default_factory=dict)
    web_artifacts: dict[str, str] = field(default_factory=dict)
    scope_successors: dict[str, str] = field(default_factory=dict)


def _strs(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])


def load_rules(path: Path) -> LegacyContextRules:
    """규칙 YAML을 :class:`LegacyContextRules`로 읽는다.
    파일이 없거나 깨지면 경고를 남기고 빈 규칙을 반환하며, 이때는 아무것도 감지하지 않는다.

    Load the rules table; a missing / broken file is logged and yields empty rules (nothing is detected)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("legacy context rules not loaded from %s: %s", path, e)
        return LegacyContextRules()
    if not isinstance(raw, dict):
        return LegacyContextRules()

    def sect(name: str) -> dict[str, Any]:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    cl, jn, pc, ds, mp = (sect("context_loaders"), sect("jndi"), sect("placeholder_config"), sect("default_servlet"),
                          sect("multipart_resolver"))
    return LegacyContextRules(
        listeners=_strs(cl.get("listeners")), root_default=str(cl.get("root_default") or ""),
        dispatcher_servlets=_strs(cl.get("dispatcher_servlets")), servlet_default=str(cl.get("servlet_default") or ""),
        annotation_context_classes=_strs(cl.get("annotation_context_classes")),
        legacy_properties=[{str(k): str(v) for k, v in r.items()} for r in raw.get("legacy_properties") or []
                           if isinstance(r, dict) and r.get("key")],
        jndi_elements=_strs(jn.get("elements")), jndi_classes=_strs(jn.get("classes")),
        jndi_todo=str(jn.get("todo") or TODO_MARK), jdbc_artifacts=_strs(jn.get("jdbc_artifacts")),
        placeholder_elements=_strs(pc.get("elements")), placeholder_classes=_strs(pc.get("classes")),
        default_servlet_elements=_strs(ds.get("elements")), default_servlet_java=_strs(ds.get("java")),
        default_servlet_property=str(ds.get("property") or ""), default_servlet_value=str(ds.get("value") or "true"),
        multipart_classes=_strs(mp.get("classes")),
        multipart_properties={str(k): _strs(v) for k, v in (mp.get("properties") or {}).items()},
        multipart_servlet_config={str(k): str(v) for k, v in (mp.get("servlet_config") or {}).items()},
        enable_web_mvc_java=_strs(sect("enable_web_mvc").get("java")),
        view_resolver_classes=_strs(sect("view_resolvers").get("classes")),
        view_resolver_elements=_strs(sect("view_resolvers").get("elements")),
        resources_elements=_strs(sect("resources").get("elements")),
        web_initializer_java=_strs(sect("web_initializers").get("java")),
        log4j1_files=_strs(raw.get("log4j1_files")), security_xml=_strs(raw.get("security_xml")),
        security_elements=_strs(raw.get("security_elements")), profile_params=_strs(raw.get("profile_params")),
        web_libraries={str(k): str(v) for k, v in (raw.get("web_libraries") or {}).items()},
        web_artifacts={str(k): str(v) for k, v in (raw.get("web_artifacts") or {}).items()},
        scope_successors={str(k): str(v) for k, v in (raw.get("scope_successors") or {}).items()},
    )


# ---------------------------------------------------------------------------------------------------------- paths
def split_locations(value: str) -> list[str]:
    """``contextConfigLocation`` 값 하나를 위치 목록으로 나눈다.
    Spring의 ``CONFIG_LOCATION_DELIMITERS``처럼 쉼표, 세미콜론, 공백으로 구분한다.

    A `contextConfigLocation` value → its locations (comma / semicolon / whitespace separated, like Spring)."""
    return [x for x in re.split(r"[,;\s]+", value or "") if x]


def ant_match(pattern: str, path: str) -> bool:
    """경로가 Spring AntPathMatcher 방식의 패턴과 맞는지 반환한다.
    별표 하나는 한 경로 단계 안, 별표 둘은 여러 단계, ``?``는 한 문자에 맞는다.

    Ant-style match (`*` within a segment, `**` across segments, `?` one character)."""
    out: list[str] = []
    i = 0
    pat = pattern.lstrip("/")
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.fullmatch("".join(out), path.lstrip("/")) is not None


def classpath_rel(path: str) -> Optional[str]:
    """as-is 파일의 classpath 기준 경로(``…/src/main/resources/`` 또는 ``…/WEB-INF/classes/`` 뒤)를
    반환한다. classpath 리소스가 아니면 None을 반환한다.

    The classpath-relative path of an as-is file (after `src/main/resources/` or `WEB-INF/classes/`), else None."""
    p = "/" + path.replace("\\", "/")
    for marker in ("/src/main/resources/", "/WEB-INF/classes/"):
        i = p.find(marker)
        if i >= 0:
            return p[i + len(marker):]
    return None


def web_rel(path: str, web_dirs: Iterable[str] = ()) -> Optional[str]:
    """as-is 파일의 웹 루트 기준 경로(``WEB-INF/…`` 형태)를 반환한다.
    웹 루트는 pom의 web 디렉토리이며, 모르면 ``/WEB-INF/``부터 잘라낸다.

    The web-root-relative path of an as-is file (e.g. `WEB-INF/spring/x.xml`), else None."""
    p = path.replace("\\", "/")
    for d in sorted({d.strip("/") for d in web_dirs if d}, key=len, reverse=True):
        if d and p.startswith(d + "/"):
            return p[len(d) + 1:]
    i = ("/" + p).find("/WEB-INF/")
    return ("/" + p)[i + 1:] if i >= 0 else None


def is_test_path(path: str) -> bool:
    """``src/test/`` 아래 파일인지 반환한다. 테스트용 XML은 Boot 컨텍스트에 넣지 않는다."""
    return "/src/test/" in "/" + path.replace("\\", "/")


def resolve_location(loc: str, candidates: Iterable[str], web_dirs: Iterable[str] = ()) -> tuple[list[str], str]:
    """위치 문자열 하나(``classpath:`` / ``classpath*:`` / ``/WEB-INF/…`` /
    상대 웹 경로, Ant 패턴 포함)를 as-is 파일로 해석한다.
    (파일 목록, 해석하지 못한 이유)를 반환하며, 해석했으면 이유는 빈 문자열이다.
    ``${…}`` / ``file:`` / URL은 해석하지 않는다.

    Resolve one location against the as-is files `candidates` → (files, why-not). `classpath:` / `classpath*:`
    match the classpath-relative path, other locations the web-root-relative one; `${…}`, `file:` and URLs are not
    resolved (why-not says so)."""
    loc = loc.strip()
    if not loc:
        return [], "empty"
    if "${" in loc:
        return [], "placeholder in the location"
    low = loc.lower()
    if low.startswith(("file:", "http:", "https:", "jar:")):
        return [], "not a project file"
    cands = list(candidates)
    if low.startswith(("classpath:", "classpath*:")):
        pat = loc.split(":", 1)[1].lstrip("/")
        hits = [c for c in cands if (cp := classpath_rel(c)) is not None and ant_match(pat, cp)]
    else:
        pat = loc.lstrip("/")
        hits = [c for c in cands if (wr := web_rel(c, web_dirs)) is not None and ant_match(pat, wr)]
    return sorted(hits), ("" if hits else "no matching file")


def entry_locations(model: Any, rules: LegacyContextRules) -> list[tuple[str, list[str]]]:
    """web.xml 모델에서 (역할, 위치 목록)을 뽑는다.
    루트 컨텍스트는 context-param ``contextConfigLocation``을, 없으면 ContextLoaderListener 기본값을 쓴다.
    DispatcherServlet마다 init-param을, 없으면 ``/WEB-INF/<name>-servlet.xml``을 쓴다.
    ``contextClass``가 어노테이션 컨텍스트면 값이 클래스 이름이므로 뺀다.

    (role, locations) of the contexts web.xml declares: the root context (context-param `contextConfigLocation`, else
    the ContextLoaderListener default) and every DispatcherServlet (its init-param, else `/WEB-INF/<name>-servlet.xml`).
    An annotation-based `contextClass` names Java classes, not XML files → skipped."""
    out: list[tuple[str, list[str]]] = []

    def annotation(cls: Optional[str]) -> bool:
        return bool(cls) and any(a in str(cls) for a in rules.annotation_context_classes)

    params = dict(getattr(model, "context_params", {}) or {})
    has_listener = any(li in rules.listeners for li in getattr(model, "listeners", []) or [])
    if not annotation(params.get("contextClass")):
        if params.get("contextConfigLocation", "").strip():
            out.append(("root", split_locations(params["contextConfigLocation"])))
        elif has_listener and rules.root_default:
            out.append(("root", [rules.root_default]))
    for s in getattr(model, "servlets", []) or []:
        if not s.servlet_class or s.servlet_class not in rules.dispatcher_servlets:
            continue
        ip = dict(s.init_params or {})
        if annotation(ip.get("contextClass")):
            continue
        if ip.get("contextConfigLocation", "").strip():
            out.append((f"servlet {s.name}", split_locations(ip["contextConfigLocation"])))
        elif rules.servlet_default:
            out.append((f"servlet {s.name}", [rules.servlet_default.replace("{servlet}", s.name)]))
    return out


# ---------------------------------------------------------------------------------------------------------- XML text
def comment_spans(text: str) -> list[tuple[int, int]]:
    """XML 주석과 CDATA 구간을 찾아 반환한다. 그 안의 태그는 무시해야 하기 때문이다."""
    return [(m.start(), m.end()) for m in _COMMENT_RE.finditer(text)]


def _in(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(a <= pos < b for a, b in spans)


@dataclass
class Element:
    """XML 요소 하나의 위치와 속성을 담는 데이터 클래스.
    바이트, 주석, 들여쓰기를 그대로 두기 위해 파서 없이 정규식으로 찾는다.

    One element found by the tag scanner: its span in the text, qualified / local name and attributes."""
    start: int
    end: int                      # end of the whole element (after `/>` or the matching end tag)
    tag_end: int                  # end of the start tag
    qname: str
    local: str
    attrs: dict[str, str]
    empty: bool

    def body(self, text: str) -> str:
        """시작 태그와 끝 태그 사이의 본문을 반환한다. 빈 태그면 빈 문자열을 반환한다."""
        return "" if self.empty else text[self.tag_end:self.end]


def _attrs(raw: str) -> dict[str, str]:
    return {k: v[1:-1] for k, v in _ATTR_RE.findall(raw or "")}


def elements(text: str, local_names: Iterable[str] = ()) -> list[Element]:
    """주석 밖에서 로컬 이름이 ``local_names`` 중 하나인 요소(비어 있으면 모든 요소)를 문서 순서로 반환한다.
    요소의 끝 위치는 같은 이름의 중첩을 세어 찾는다.

    Elements outside comments whose local name is one of `local_names` (all when empty), in document order; the end
    is found by counting nested elements of the same qualified name."""
    want = set(local_names)
    spans = comment_spans(text)
    tags = [m for m in _TAG_RE.finditer(text) if not _in(spans, m.start())]
    out: list[Element] = []
    for i, m in enumerate(tags):
        if m.group(1):
            continue
        qname = m.group(2)
        local = qname.rsplit(":", 1)[-1]
        if want and local not in want:
            continue
        empty = bool(m.group(4))
        end = m.end()
        if not empty:
            depth = 1
            for n in tags[i + 1:]:
                if n.group(2) != qname:
                    continue
                if n.group(1):
                    depth -= 1
                    if depth == 0:
                        end = n.end()
                        break
                elif not n.group(4):
                    depth += 1
        out.append(Element(start=m.start(), end=end, tag_end=m.end(), qname=qname, local=local,
                           attrs=_attrs(m.group(3)), empty=empty))
    return out


def beans_of_class(text: str, classes: Iterable[str]) -> list[Element]:
    """``class="…"``가 ``classes`` 중 하나인 ``<bean>`` 요소를 반환한다."""
    want = set(classes)
    return [e for e in elements(text, ("bean",)) if e.attrs.get("class", "").strip() in want]


def bean_ids(text: str) -> list[str]:
    """주석 밖에서 ``id="…"``를 가진 요소의 id 목록을 반환한다. 빈과 네임스페이스
    요소(``<jee:jndi-lookup>``, ``<util:properties>`` 등)를 포함하며, 같은 파일 안의 중복은 한 번만 넣는다.

    The `id` attributes of the elements outside comments (beans and namespace elements), once per file."""
    seen: list[str] = []
    for e in elements(text):
        v = e.attrs.get("id", "").strip()
        if v and v not in seen and e.local not in ("import", "beans"):
            seen.append(v)
    return seen


def import_resources(text: str) -> list[str]:
    """주석 밖의 ``<import resource="…"/>`` 값 목록을 반환한다."""
    return [e.attrs.get("resource", "").strip() for e in elements(text, ("import",)) if e.attrs.get("resource")]


def bean_property(text: str, el: Element, name: str) -> Optional[str]:
    """빈 요소에서 이름이 ``name``인 속성 값을 찾는다. ``<property name="x" value="…"/>``,
    ``<property name="x"><value>…</value></property>``, ``p:x="…"``를 보고, 없으면 None을 반환한다."""
    if f"p:{name}" in el.attrs:
        return el.attrs[f"p:{name}"].strip()
    for p in elements(el.body(text), ("property",)):
        if p.attrs.get("name") != name:
            continue
        if "value" in p.attrs:
            return p.attrs["value"].strip()
        m = re.search(r"<(?:\w+:)?value>\s*([^<]*?)\s*</(?:\w+:)?value>", p.body(el.body(text)))
        if m:
            return m.group(1)
    return None


def line_start(text: str, pos: int) -> int:
    """``pos``가 있는 줄의 시작 위치를 반환한다."""
    return text.rfind("\n", 0, pos) + 1


def newline(text: str) -> str:
    """파일이 쓰는 줄바꿈 문자를 반환한다. CRLF를 쓰는 파일이면 CRLF를 반환한다."""
    return "\r\n" if "\r\n" in text else "\n"


def insert_comment_above(text: str, el: Element, comment: str) -> tuple[str, bool]:
    """요소 바로 위 줄에 같은 들여쓰기로 ``<!-- comment -->``를 넣는다. 같은 주석이 이미 있으면 그대로 둔다.

    Insert `<!-- comment -->` on its own line right above `el` (same indentation); idempotent."""
    ls = line_start(text, el.start)
    before = text[ls:el.start]
    nl = newline(text)
    prev = text[line_start(text, ls - 1):ls] if ls > 0 else ""
    mark = TODO_MARK if TODO_MARK in comment else comment[:40]
    if mark in prev:
        return text, False
    if not before.strip():
        return text[:ls] + f"{before}<!-- {comment} -->{nl}" + text[ls:], True
    return text[:el.start] + f"<!-- {comment} -->{nl}" + text[el.start:], True


def remove_element(text: str, el: Element, comment: str) -> str:
    """요소를 지우고 그 위치에 같은 들여쓰기로 ``<!-- comment -->`` 한 줄을 남긴다."""
    return text[:el.start] + f"<!-- {comment} -->" + text[el.end:]


def xml_encoding(data: bytes) -> Optional[str]:
    """XML 선언의 ``encoding="…"`` 값을 반환한다. 없으면 None을 반환한다."""
    m = re.match(rb"\s*<\?xml[^>]*?encoding\s*=\s*[\"']([\w.:-]+)[\"']", data)
    return m.group(1).decode("ascii", "replace") if m else None


# ---------------------------------------------------------------------------------------------------------- locations
_WEBINF_RE = re.compile(r"(?<![\w:/.\-])(/?WEB-INF/[^\s\"'<>,;]+)")
_PROFILE_LOC_RE = re.compile(r"(classpath\*?:[^\s\"'<>,;]*\$\{[^}]+\}[^\s\"'<>,;]*)")


def placeholder_elements(text: str, rules: LegacyContextRules) -> list[Element]:
    """프로퍼티 파일 위치를 가진 요소(``<context:property-placeholder>``, ``<util:properties>`` 등과
    PropertyPlaceholderConfigurer 계열 빈)를 반환한다.

    Elements that name property-file locations (placeholder namespace elements and configurer beans)."""
    out = [e for e in elements(text, rules.placeholder_elements)]
    out += beans_of_class(text, rules.placeholder_classes)
    return sorted(out, key=lambda e: e.start)


def location_values(text: str, el: Element) -> list[tuple[int, int, str]]:
    """요소 안의 위치 문자열을 (시작, 끝, 값) 목록으로 반환한다.
    ``location`` 속성 값과 빈의 ``location`` / ``locations`` 값을 본다.

    (start, end, value) of the location strings inside `el` (absolute offsets in `text`)."""
    chunk = text[el.start:el.end]
    out: list[tuple[int, int, str]] = []
    for m in re.finditer(r"""(?:\blocation|p:locations?)\s*=\s*(["'])(.*?)\1""", chunk, re.S):
        out.append((el.start + m.start(2), el.start + m.end(2), m.group(2)))
    for m in re.finditer(r"""<(?:\w+:)?property\b[^>]*\bname\s*=\s*["']locations?["'][^>]*?\bvalue\s*=\s*(["'])(.*?)\1""",
                         chunk, re.S):
        out.append((el.start + m.start(2), el.start + m.end(2), m.group(2)))
    for m in re.finditer(r"<(?:\w+:)?value>\s*([^<]*?)\s*</(?:\w+:)?value>", chunk):
        out.append((el.start + m.start(1), el.start + m.end(1), m.group(1)))
    seen: set[tuple[int, int]] = set()
    uniq = []
    for a, b, v in sorted(out):
        if (a, b) not in seen and not any(a >= x and b <= y and (a, b) != (x, y) for x, y in seen):
            seen.add((a, b))
            uniq.append((a, b, v))
    return uniq


def rewrite_webinf_locations(text: str, rules: LegacyContextRules,
                             target: Callable[[str], Optional[str]]) -> tuple[str, list[tuple[str, str]]]:
    """복사한 XML의 프로퍼티 위치 중 ``/WEB-INF/x`` / ``WEB-INF/x``를 ``target(x)``의 결과(보통
    ``classpath:asis-webinf/x``)로 바꾼다. 결과가 None이면 그대로 둔다.
    (새 텍스트, [(이전 값, 새 값)])을 반환한다. ``${…}``가 들어간 위치는 건드리지 않는다.

    Rewrite `/WEB-INF/x` property locations of the placeholder elements to `target(x)` (None = leave it)."""
    edits: list[tuple[int, int, str]] = []
    done: list[tuple[str, str]] = []
    for el in placeholder_elements(text, rules):
        for a, _b, val in location_values(text, el):
            for m in _WEBINF_RE.finditer(val):
                old = m.group(1)
                if "${" in old:
                    continue
                sub = old.lstrip("/")[len("WEB-INF/"):]
                new = target(sub)
                if new:
                    edits.append((a + m.start(1), a + m.end(1), new))
                    done.append((old, new))
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text, done


def profile_locations(text: str, rules: LegacyContextRules) -> list[str]:
    """복사한 XML의 프로퍼티 위치 중 ``${…}``가 들어간 ``classpath:`` 위치를 반환한다.

    `classpath:` property locations holding a `${…}` placeholder (D7)."""
    out: list[str] = []
    for el in placeholder_elements(text, rules):
        for _a, _b, val in location_values(text, el):
            for m in _PROFILE_LOC_RE.finditer(val):
                if m.group(1) not in out:
                    out.append(m.group(1))
    return out


def placeholder_keys(loc: str) -> list[tuple[str, Optional[str]]]:
    """위치 안의 ``${key}`` / ``${key:default}``를 (키, 기본값) 목록으로 반환한다."""
    out = []
    for m in re.finditer(r"\$\{([^}:]+)(?::([^}]*))?\}", loc):
        out.append((m.group(1).strip(), m.group(2)))
    return out


def glob_of(loc: str) -> str:
    """위치의 플레이스홀더를 ``*``로 바꾼 glob을 반환한다.
    예를 들어 ``classpath:config-${env}.properties``는 ``classpath*:config-*.properties``가 된다."""
    rest = loc.split(":", 1)[1] if ":" in loc else loc
    return "classpath*:" + re.sub(r"\$\{[^}]*\}", "*", rest)


def replace_value(text: str, old: str, new: str, rules: LegacyContextRules) -> str:
    """프로퍼티 위치 요소 안에서만 ``old``를 ``new``로 바꾼다. 다른 곳의 같은 문자열은 그대로 둔다."""
    edits = []
    for el in placeholder_elements(text, rules):
        for a, _b, val in location_values(text, el):
            i = val.find(old)
            while i >= 0:
                edits.append((a + i, a + i + len(old)))
                i = val.find(old, i + len(old))
    for a, b in sorted(set(edits), reverse=True):
        text = text[:a] + new + text[b:]
    return text


# ---------------------------------------------------------------------------------------------------------- views
def view_prefixes(text: str, rules: LegacyContextRules) -> list[tuple[int, int, str]]:
    """복사한 XML에서 JSP view prefix(InternalResourceViewResolver 빈의 ``prefix`` 속성, ``p:prefix``,
    ``<mvc:jsp prefix>``)를 찾아 (시작, 끝, 값) 목록으로 반환한다.

    (start, end, value) of the JSP view prefixes in a copy."""
    out: list[tuple[int, int, str]] = []
    for el in beans_of_class(text, rules.view_resolver_classes):
        chunk = text[el.start:el.end]
        for m in re.finditer(r"""(?:<(?:\w+:)?property\b[^>]*\bname\s*=\s*["']prefix["'][^>]*?\bvalue\s*=\s*|\bp:prefix\s*=\s*)(["'])(.*?)\1""",
                             chunk, re.S):
            out.append((el.start + m.start(2), el.start + m.end(2), m.group(2)))
        for m in re.finditer(r"""<(?:\w+:)?property\b[^>]*\bname\s*=\s*["']prefix["'][^>]*>\s*<(?:\w+:)?value>\s*([^<]*?)\s*</""",
                             chunk, re.S):
            out.append((el.start + m.start(1), el.start + m.end(1), m.group(1)))
    for el in elements(text, rules.view_resolver_elements):
        if el.qname.startswith("mvc:") or ":" not in el.qname:
            m = re.search(r"""\bprefix\s*=\s*(["'])(.*?)\1""", text[el.start:el.tag_end], re.S)
            if m:
                out.append((el.start + m.start(2), el.start + m.end(2), m.group(2)))
    return sorted(set(out))


def rewrite_spans(text: str, edits: list[tuple[int, int, str]]) -> str:
    """(시작, 끝, 새 값) 편집 목록을 텍스트 뒤쪽부터 적용한다."""
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text


def resource_handlers(text: str, rules: LegacyContextRules) -> list[Element]:
    """``<mvc:resources mapping=… location=…>`` 요소를 반환한다."""
    return [e for e in elements(text, rules.resources_elements) if "location" in e.attrs and "mapping" in e.attrs]


def has_element(text: str, local_names: Iterable[str], ns_hint: str = "") -> bool:
    """주석 밖에 그 로컬 이름의 요소가 있는지 반환한다.
    ``ns_hint``가 주어지면 그 문자열도 파일에 있어야 참이다."""
    if ns_hint and ns_hint not in text:
        return False
    return bool(elements(text, local_names))


def multipart_limits(text: str, el: Element, rules: LegacyContextRules) -> list[tuple[str, str, str]]:
    """CommonsMultipartResolver 빈의 업로드 한도를 (Boot 키, 값, 빈 속성 이름) 목록으로 반환한다.
    바이트 수는 KB / MB 문자열로 바꾼다.

    The limits of a CommonsMultipartResolver bean → (Boot key, DataSize value, bean property)."""
    from .webxml_registrations import data_size

    out: list[tuple[str, str, str]] = []
    keys_done: set[str] = set()
    # maxUploadSizePerFile이 있으면 파일 하나의 한도로 그 값을 먼저 쓴다
    order = sorted(rules.multipart_properties.items(), key=lambda kv: 0 if kv[0].endswith("PerFile") else 1)
    for prop, keys in order:
        v = bean_property(text, el, prop)
        size = data_size(v) if v is not None else None
        if size is None:
            continue
        for k in keys:
            if k not in keys_done:
                keys_done.add(k)
                out.append((k, size, prop))
    return out
