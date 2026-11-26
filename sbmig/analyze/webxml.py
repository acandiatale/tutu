"""web.xml을 읽어 서블릿, 필터, 리스너 같은 설정을 ``WebXmlModel``로 만드는 모듈.

Namespace-agnostic (DTD 2.2 / 2.3 without a namespace, `java.sun.com` 2.4 / 2.5, `xmlns.jcp.org` 3.x / 4.0,
`jakarta.ee` 5.0 / 6.x): every element is matched by its local name. The external DTD of a 2.2 / 2.3 descriptor is
never fetched (lxml: `load_dtd=False`, `no_network=True`, entities not resolved); a malformed file is re-read with
lxml's recovering parser and, if even that gives no `<web-app>`, a model with a warning is returned.
`parse_web_xml` never raises. The DTD is never fetched because the network is closed and XXE must be
blocked.

Used by `sbmig.analyze.source_scanner` (counts in `SourceStats.web_xml_*`) and by the restructure, which renders
`WebXmlRegistrations` from the model (`layout.web_xml_registrations`). The registrations keep the external
Tomcat's web.xml settings in the Boot app.
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
    """네임스페이스를 뗀 요소의 로컬 이름을 반환한다. 주석처럼 태그가 문자열이 아니면 빈 문자열을 반환한다."""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c) == name]


def _text(el: Any, name: str) -> Optional[str]:
    """이름이 ``name``인 첫 자식의 텍스트를 앞뒤 공백을 빼고 반환한다. 비어 있으면 None이다."""
    for c in el:
        if _local(c) == name:
            t = "".join(c.itertext()).strip()
            return t or None
    return None


def _texts(el: Any, name: str) -> list[str]:
    """이름이 ``name``인 자식마다 텍스트를 모아 반환한다. 빈 것은 뺀다.
    url-pattern처럼 여러 번 나오는 요소에 쓴다."""
    out = []
    for c in el:
        if _local(c) == name:
            t = "".join(c.itertext()).strip()
            if t:
                out.append(t)
    return out


def _int(value: Optional[str], what: str, warnings: list[str]) -> Optional[int]:
    """값을 정수로 읽는다. 숫자가 아니면 경고를 남기고 None을 반환한다."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        warnings.append(f"{what}: not an integer ({value!r})")
        return None


def _params(el: Any, warnings: list[str], where: str) -> dict[str, str]:
    """``<init-param>``을 딕셔너리로 읽는다. 이름이 없는 것은 경고를 남기고 건너뛴다."""
    out: dict[str, str] = {}
    for p in _kids(el, "init-param"):
        k = _text(p, "param-name")
        if not k:
            warnings.append(f"{where}: <init-param> without <param-name>")
            continue
        out[k] = _text(p, "param-value") or ""
    return out


def _parse_root(text: str, warnings: list[str]) -> Any:
    """루트 요소를 반환한다. 엄격한 파서가 실패하면 복구 파서로 다시 읽는다. 그래도 안 되면 None이다."""
    # 디코딩한 텍스트에서 XML 선언을 떼고 UTF-8로 넘긴다. 선언된 인코딩과 바이트가 어긋나면 파싱이 깨진다
    data = xml_without_declaration(text).encode("utf-8")
    strict = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, remove_comments=True,
                             huge_tree=False)
    try:
        return etree.fromstring(data, strict)
    except etree.XMLSyntaxError as e:
        warnings.append(f"web.xml is not well-formed ({e}); parsed leniently")
    except ValueError as e:  # pragma: no cover - 인코딩 선언이 이상한 경우
        warnings.append(f"web.xml could not be parsed strictly ({e}); parsed leniently")
    lenient = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False, remove_comments=True,
                              recover=True)
    try:
        return etree.fromstring(data, lenient)
    except (etree.XMLSyntaxError, ValueError) as e:
        warnings.append(f"web.xml could not be parsed ({e})")
        return None


def _spec(root: Any, text: str) -> Optional[str]:
    """서블릿 스펙 버전을 반환한다. 찾지 못하면 None이다.
    ``<web-app version=…>`` 속성, 텍스트, DTD 파일 이름(``web-app_2_3.dtd``) 순으로 본다."""
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
    """web.xml 하나를 파싱한다. ``path``는 모델에 기록만 한다.
    예외를 던지지 않는다. 문제는 ``WebXmlModel.warnings``에 남긴다.
    파일 하나 때문에 분석과 재구성이 멈추면 안 된다."""
    try:
        return _parse(text or "", path)
    except Exception as e:  # noqa: BLE001
        log.warning("web.xml %s: %s", path, e)
        return WebXmlModel(path=path, warnings=[f"web.xml could not be parsed: {type(e).__name__}: {e}"])


def _parse(text: str, path: str) -> WebXmlModel:
    """:func:`parse_web_xml`의 본체. 같은 이름의 서블릿이 또 나오면 경고를 남기고 처음 것을 쓴다."""
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

    # 서블릿과 매핑을 이름으로 합친다. 매핑 하나에 url-pattern이 여러 개일 수도, 매핑이 여러 개일 수도 있다
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
            sv.load_on_startup = 0          # 빈 요소는 시작 중 아무 때나 로드한다는 뜻이다
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

    # 컨테이너는 <filter-mapping>이 처음 나온 순서로 필터 체인을 만든다
    # Boot 등록도 이 순서를 지켜야 인코딩 필터와 보안 필터가 뒤바뀌지 않는다
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
