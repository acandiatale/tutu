"""web.xml 을 ``WebXmlModel`` 로 파싱한다(2026-10-23): 서블릿·매핑, 필터(체인 순서), 리스너, 세션
타임아웃, welcome-file, error-page, mime, context-param, 보안 제약 수, login-config, resource-ref, taglib.
흐름상 위치: 소스 스캐너가 부르고(``SourceStats.web_xml_*`` 개수), 재구성이 이 모델로 Boot 등록 코드
``WebXmlRegistrations`` 를 만든다(``layout.web_xml_registrations``) — 외장 Tomcat 의 web.xml 설정이 Boot
앱에서 사라지지 않게 하려는 것. 진입점: :func:`parse_web_xml` (예외를 절대 올리지 않음, 문제는 ``warnings``).
버전 무관(DTD 2.2 / 2.3 … jakarta 6.x, 로컬 이름으로만 매칭). 외부 DTD 는 절대 내려받지 않는다 (폐쇄망 + XXE
방지: ``load_dtd=False``, ``no_network=True``); 깨진 파일은 lxml 복구 파서로 한 번 더 읽는다.

web.xml → `WebXmlModel` (2026-10-23).

Namespace-agnostic (DTD 2.2 / 2.3 without a namespace, `java.sun.com` 2.4 / 2.5, `xmlns.jcp.org` 3.x / 4.0,
`jakarta.ee` 5.0 / 6.x): every element is matched by its local name. The external DTD of a 2.2 / 2.3 descriptor is
never fetched (lxml: `load_dtd=False`, `no_network=True`, entities not resolved); a malformed file is re-read with
lxml's recovering parser and, if even that gives no `<web-app>`, a model with a warning is returned.
`parse_web_xml` never raises.

Used by `sbmig.analyze.source_scanner` (counts in `SourceStats.web_xml_*`) and by the restructure, which renders
`WebXmlRegistrations` from the model (`layout.web_xml_registrations`).


"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from lxml import etree

from sbmig.core.models import WebXmlErrorPage, WebXmlFilter, WebXmlModel, WebXmlServlet

from .pom_parser import xml_without_declaration

log = logging.getLogger("sbmig.analyze")

_DTD_RE = re.compile(r"web-app_(\d+)_(\d+)\.dtd", re.I)
_VERSION_RE = re.compile(r"<web-app\b[^>]*?\bversion\s*=\s*[\"']([\d.]+)[\"']", re.S)
DISPATCHERS = ("REQUEST", "FORWARD", "INCLUDE", "ERROR", "ASYNC")


def _local(el: Any) -> str:
    """요소의 로컬 이름(네임스페이스 제거). 주석·PI 처럼 태그가 문자열이 아니면 ''."""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c) == name]


def _text(el: Any, name: str) -> Optional[str]:
    """``name`` 인 첫 자식의 전체 텍스트(공백 정리, 비면 None)."""
    for c in el:
        if _local(c) == name:
            t = "".join(c.itertext()).strip()
            return t or None
    return None


def _texts(el: Any, name: str) -> list[str]:
    """``name`` 인 모든 자식의 텍스트(빈 것 제외) — url-pattern 처럼 여러 번 나오는 요소용."""
    out = []
    for c in el:
        if _local(c) == name:
            t = "".join(c.itertext()).strip()
            if t:
                out.append(t)
    return out


def _int(value: Optional[str], what: str, warnings: list[str]) -> Optional[int]:
    """정수로 읽는다. 숫자가 아니면 경고를 남기고 None."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        warnings.append(f"{what}: not an integer ({value!r})")
        return None


def _params(el: Any, warnings: list[str], where: str) -> dict[str, str]:
    """``<init-param>`` 들을 dict 로(이름 없는 것은 경고 후 건너뜀)."""
    out: dict[str, str] = {}
    for p in _kids(el, "init-param"):
        k = _text(p, "param-name")
        if not k:
            warnings.append(f"{where}: <init-param> without <param-name>")
            continue
        out[k] = _text(p, "param-value") or ""
    return out


def _parse_root(text: str, warnings: list[str]) -> Any:
    """루트 요소를 얻는다: 엄격 파서 → 실패하면 복구(recover)
    파서 → 그래도 안 되면 None. DTD·네트워크·엔티티 없음."""
    # 이미 디코딩된 텍스트이므로 XML 선언을 뗀다 — EUC-KR 선언이 UTF-8 바이트와 어긋나 파싱이 깨지지 않게
    # the text is already decoded: drop the XML declaration so its `encoding=` cannot contradict the UTF-8 bytes
    data = xml_without_declaration(text).encode("utf-8")
    strict = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, remove_comments=True,
                             huge_tree=False)
    try:
        return etree.fromstring(data, strict)
    except etree.XMLSyntaxError as e:
        warnings.append(f"web.xml is not well-formed ({e}); parsed leniently")
    except ValueError as e:  # pragma: no cover - encoding declaration oddities
        warnings.append(f"web.xml could not be parsed strictly ({e}); parsed leniently")
    lenient = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, remove_comments=True,
                              recover=True)
    try:
        return etree.fromstring(data, lenient)
    except (etree.XMLSyntaxError, ValueError) as e:
        warnings.append(f"web.xml could not be parsed ({e})")
        return None


def _spec(root: Any, text: str) -> Optional[str]:
    """서블릿 스펙 버전: ``<web-app version=…>`` 속성, 없으면
    텍스트에서, 그다음 DTD 파일 이름(``web-app_2_3.dtd``)."""
    v = root.get("version") if root is not None else None
    if v:
        return v.strip()
    m = _VERSION_RE.search(text)
    if m:
        return m.group(1)
    m = _DTD_RE.search(text)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return None


def parse_web_xml(text: str, path: str) -> WebXmlModel:
    """web.xml 하나를 파싱한다(``path`` 는 기록용). 예외를 내지 않는다 — 잘못된 서술자 하나로 분석·재구성이
    멈추면 안 된다. 문제는 ``WebXmlModel.warnings`` 에 남는다.

    Parse a web.xml (`path` is only recorded). Never raises; problems land in `WebXmlModel.warnings`."""
    try:
        return _parse(text or "", path)
    except Exception as e:  # noqa: BLE001 — a bad descriptor must never stop the analysis / restructure
        log.warning("web.xml %s: %s", path, e)
        return WebXmlModel(path=path, warnings=[f"web.xml could not be parsed: {type(e).__name__}: {e}"])


def _parse(text: str, path: str) -> WebXmlModel:
    """:func:`parse_web_xml` 의 본체. 서블릿은 이름으로 합치고 같은 이름이 또 나오면 처음 것을 쓴다(경고)."""
    warnings: list[str] = []
    model = WebXmlModel(path=path)
    if not text.strip():
        model.warnings.append("web.xml is empty")
        return model
    root = _parse_root(text, warnings)
    model.spec = _spec(root, text)
    if root is None or _local(root) != "web-app":
        warnings.append("no <web-app> root element")
        model.warnings = warnings
        return model
    model.display_name = _text(root, "display-name")

    # servlets + mappings (merged by name, several <url-pattern> per mapping and several mappings)
    servlets: dict[str, WebXmlServlet] = {}
    for s in _kids(root, "servlet"):
        name = _text(s, "servlet-name")
        if not name:
            warnings.append("<servlet> without <servlet-name>")
            continue
        sv = WebXmlServlet(name=name, servlet_class=_text(s, "servlet-class"), jsp_file=_text(s, "jsp-file"),
                           init_params=_params(s, warnings, f"servlet {name}"))
        los = _text(s, "load-on-startup")
        if los is not None:
            sv.load_on_startup = _int(los, f"servlet {name} load-on-startup", warnings)
        elif _kids(s, "load-on-startup"):
            sv.load_on_startup = 0          # empty element: "load at any time during startup"
        sv.async_supported = (_text(s, "async-supported") or "").lower() == "true"
        for mp in _kids(s, "multipart-config"):
            sv.multipart_location = _text(mp, "location") or ""
            sv.multipart_max_file_size = _int(_text(mp, "max-file-size"), f"servlet {name} max-file-size", warnings)
            sv.multipart_max_request_size = _int(_text(mp, "max-request-size"),
                                                 f"servlet {name} max-request-size", warnings)
            sv.multipart_file_size_threshold = _int(_text(mp, "file-size-threshold"),
                                                    f"servlet {name} file-size-threshold", warnings)
        if not sv.servlet_class and not sv.jsp_file:
            warnings.append(f"servlet {name}: neither <servlet-class> nor <jsp-file>")
        if name in servlets:
            warnings.append(f"servlet {name} declared twice; the first declaration is used")
            continue
        servlets[name] = sv
    for m in _kids(root, "servlet-mapping"):
        name = _text(m, "servlet-name")
        patterns = _texts(m, "url-pattern")
        if not name or name not in servlets:
            warnings.append(f"<servlet-mapping> for unknown servlet {name!r} ({', '.join(patterns) or 'no pattern'})")
            continue
        for p in patterns:
            if p not in servlets[name].url_patterns:
                servlets[name].url_patterns.append(p)
    model.servlets = list(servlets.values())

    # 필터 순서가 중요하다: 서블릿 컨테이너는 <filter-mapping> 이 처음 나온 순서로 체인을 만든다 — Boot 등록
    # (FilterRegistrationBean 의 order)이 같은 순서를 지켜야 인코딩·보안 필터가 뒤바뀌지 않는다
    # filters + mappings; order = index of the filter's first <filter-mapping> (the chain order)
    filters: dict[str, WebXmlFilter] = {}
    for f in _kids(root, "filter"):
        name = _text(f, "filter-name")
        cls = _text(f, "filter-class")
        if not name or not cls:
            warnings.append(f"<filter> {name or '?'} without <filter-name> / <filter-class>")
            continue
        if name in filters:
            warnings.append(f"filter {name} declared twice; the first declaration is used")
            continue
        filters[name] = WebXmlFilter(name=name, filter_class=cls, init_params=_params(f, warnings, f"filter {name}"),
                                     async_supported=(_text(f, "async-supported") or "").lower() == "true")
    order = 0
    seen: set[str] = set()
    for m in _kids(root, "filter-mapping"):
        name = _text(m, "filter-name")
        if not name or name not in filters:
            warnings.append(f"<filter-mapping> for unknown filter {name!r}")
            continue
        fl = filters[name]
        if name not in seen:
            seen.add(name)
            order += 1
            fl.order = order
        for p in _texts(m, "url-pattern"):
            if p not in fl.url_patterns:
                fl.url_patterns.append(p)
        for s in _texts(m, "servlet-name"):
            if s not in fl.servlet_names:
                fl.servlet_names.append(s)
        for d in _texts(m, "dispatcher"):
            d = d.upper()
            if d not in DISPATCHERS:
                warnings.append(f"filter {name}: unknown dispatcher {d!r}")
            elif d not in fl.dispatchers:
                fl.dispatchers.append(d)
    model.filters = sorted(filters.values(), key=lambda x: (x.order == 0, x.order))

    for li in _kids(root, "listener"):
        cls = _text(li, "listener-class")
        if cls and cls not in model.listeners:
            model.listeners.append(cls)
        elif not cls:
            warnings.append("<listener> without <listener-class>")

    for sc in _kids(root, "session-config"):
        t = _text(sc, "session-timeout")
        if t is not None:
            model.session_timeout_minutes = _int(t, "session-timeout", warnings)
    for wl in _kids(root, "welcome-file-list"):
        model.welcome_files += [w for w in _texts(wl, "welcome-file") if w not in model.welcome_files]
    for ep in _kids(root, "error-page"):
        loc = _text(ep, "location")
        if not loc:
            warnings.append("<error-page> without <location>")
            continue
        page = WebXmlErrorPage(location=loc, exception_type=_text(ep, "exception-type"))
        code = _text(ep, "error-code")
        if code is not None:
            page.error_code = _int(code, "error-page error-code", warnings)
            if page.error_code is None:
                continue
        model.error_pages.append(page)
    for mm in _kids(root, "mime-mapping"):
        ext, typ = _text(mm, "extension"), _text(mm, "mime-type")
        if ext and typ:
            model.mime_mappings[ext] = typ
    for cp in _kids(root, "context-param"):
        k = _text(cp, "param-name")
        if k:
            model.context_params[k] = _text(cp, "param-value") or ""
    model.security_constraints = len(_kids(root, "security-constraint"))
    for lc in _kids(root, "login-config"):
        model.login_config = _text(lc, "auth-method") or model.login_config or "unspecified"
    for tag in ("resource-ref", "resource-env-ref"):
        for rr in _kids(root, tag):
            n = _text(rr, "res-ref-name") or _text(rr, "resource-env-ref-name")
            if n and n not in model.resource_refs:
                model.resource_refs.append(n)
    model.taglibs = len(_kids(root, "taglib")) + sum(len(_kids(j, "taglib")) for j in _kids(root, "jsp-config"))
    model.distributable = bool(_kids(root, "distributable"))
    model.warnings = warnings
    return model
