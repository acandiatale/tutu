"""web.xml의 servlet, filter, listener 선언을 Boot 등록 클래스 ``WebXmlRegistrations``로 옮길 계획을 만드는
모듈(``layout.web_xml_registrations: generate``).
Servlet 3 이전 프로젝트를 Boot로 바꾸면 web.xml의 매핑이 모두 사라진다.
내장 Tomcat은 web.xml을 읽지 않고, 회사 Boot WAR에도 web.xml이 들어가지 않기 때문이다.
재구성 단계의 ``Restructurer._web_xml_registrations``가 ``plan_registrations``를 호출하고,
``templates/WebXmlRegistrations.java.j2``로 boot 모듈에 클래스를 만든다.
- 순수 함수만 둔다. 파일 읽기와 쓰기는 restructurer가 한다
- 빈으로 옮길 수 없는 것(jsp-file servlet, welcome-file, MIME, security-constraint 등)은 후속 작업으로 남긴다
- 같은 것을 두 번 등록하지 않는다. Boot가 대신하는 클래스는 ``knowledge/config/web_xml_rules.yml``의 skip
  표로 빼고, ``@WebServlet`` 등이 붙은 클래스는 ``@ServletComponentScan``이 있으면 스캔에 맡긴다

web.xml → `WebXmlRegistrations` (2026-10-23, `layout.web_xml_registrations: generate`).

Pre-Servlet-3 projects (web.xml 2.2 / 2.3 / 2.4, no `@WebServlet`) lose every servlet / filter / listener mapping
when they become a Spring Boot application: embedded Tomcat never reads web.xml and the company's Boot WAR does not
package it. `plan_registrations` turns the parsed `WebXmlModel` (`sbmig.analyze.webxml`) into the render context of
`templates/WebXmlRegistrations.java.j2` — `ServletRegistrationBean` / `FilterRegistrationBean` /
`ServletListenerRegistrationBean` beans plus one `ErrorPageRegistrar` — and the follow-ups for what has no bean
(jsp-file servlets, welcome files, MIME mappings, security constraints, …). Pure: the restructurer does the I/O.

Classes in the skip table (`knowledge/config/web_xml_rules.yml`, invariant 2) are left to Spring Boot; classes the
project annotates with `@WebServlet` / `@WebFilter` / `@WebListener` are left to `@ServletComponentScan` (when the
generated Application carries it), so nothing is registered twice.

Boot 4.1 types used (verified with `javap` against spring-boot-4.1.0.jar, 2026-10-23):
`org.springframework.boot.web.servlet.{ServletRegistrationBean, FilterRegistrationBean,
ServletListenerRegistrationBean}`, `org.springframework.boot.web.error.{ErrorPage, ErrorPageRegistrar}`
(`ErrorPage(HttpStatus, String)`, `ErrorPage(Class<? extends Throwable>, String)`, `ErrorPage(String)`),
`AbstractFilterRegistrationBean.setDispatcherTypes(jakarta.servlet.DispatcherType, jakarta.servlet.DispatcherType...)`,
`ServletRegistrationBean.setMultipartConfig(jakarta.servlet.MultipartConfigElement)`,
`DelegatingFilterProxyRegistrationBean(String, ServletRegistrationBean<?>...)` (v2.1x, spring-boot-4.1.0.jar).

건너뛴 선언의 init-param은 Boot 속성(``RegistrationPlan.properties``)으로 옮긴다.
표의 ``requires:`` 조건이 맞을 때만 건너뛴다.


"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from sbmig.core.models import WebXmlModel

log = logging.getLogger("sbmig.restructure")

RULES_FILE = "knowledge/config/web_xml_rules.yml"
CLASS_NAME = "WebXmlRegistrations"
# ErrorPage(HttpStatus, …) needs HttpStatus.valueOf(code), which throws for codes Spring does not know: only the
# common error statuses are generated, others become a follow-up
ERROR_STATUS_CODES = frozenset({400, 401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414, 415, 416,
                                417, 422, 423, 424, 426, 428, 429, 431, 451, 500, 501, 502, 503, 504, 505, 507,
                                508, 511})
_FQCN_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_JAVA_KEYWORDS = {"abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const",
                  "continue", "default", "do", "double", "else", "enum", "extends", "final", "finally", "float",
                  "for", "goto", "if", "implements", "import", "instanceof", "int", "interface", "long", "native",
                  "new", "package", "private", "protected", "public", "return", "short", "static", "strictfp",
                  "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try", "void",
                  "volatile", "while", "true", "false", "null"}


@dataclass
class SkipRule:
    """web_xml_rules.yml의 skip 규칙 한 건을 담는 데이터 클래스.
    등록하지 않을 클래스(FQCN), 이유, 대신 설정할 Boot 속성, 건너뛸 조건(``requires``), 조건이 맞지 않을
    때의 처리(``otherwise``: register / proxy / followup)를 담는다.

    One skip row of web_xml_rules.yml; v2.1x adds `properties` / `set` (Boot properties written from the as-is
    declaration), `requires` (the skip applies only when every condition holds) and `otherwise`."""
    cls: str
    reason: str
    property: Optional[str] = None
    properties: dict[str, str] = field(default_factory=dict)       # init-param name → Boot key
    set: dict[str, str] = field(default_factory=dict)              # Boot key → value (written when skipped)
    requires: dict[str, list[str]] = field(default_factory=dict)   # filter_name / libraries / artifacts
    otherwise: str = "register"                                    # register | proxy | followup
    register_requires: dict[str, list[str]] = field(default_factory=dict)
    followup: str = ""


@dataclass
class RegistrationFacts:
    """skip 규칙의 ``requires:`` 조건을 확인하는 데 필요한 프로젝트 정보를 담는 데이터 클래스.
    분석기가 찾은 라이브러리 이름과 원래 pom이 선언한 g:a를 담는다.

    Project facts the `requires:` conditions are checked against (analyzer library names, the g:a keys the as-is
    poms declare) and the v2.1x switches (`filter_properties`; the multipart-config key table)."""
    libraries: set[str] = field(default_factory=set)
    artifacts: set[str] = field(default_factory=set)
    filter_properties: bool = True
    multipart_keys: dict[str, str] = field(default_factory=dict)   # location / max_file_size / … → Boot key


@dataclass
class ClassInfo:
    """web.xml에 나온 클래스를 프로젝트 소스에서 찾은 결과를 담는 데이터 클래스.
    파일 경로, @Web* 어노테이션, HttpServlet 직접 상속 여부를 담는다.

    What the project sources say about a class named in web.xml (None from the lookup = not in the project)."""
    path: str
    annotated: bool = False          # @WebServlet / @WebFilter / @WebListener on it
    http_servlet: bool = False       # `extends HttpServlet` directly


@dataclass
class RegistrationPlan:
    """``plan_registrations``의 결과를 담는 데이터 클래스.
    템플릿에 넘길 servlet, filter, listener, error page 목록과 건너뛴 선언,
    스캔에 맡긴 선언, 후속 작업, session-timeout을 담는다."""
    servlets: list[dict[str, Any]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    listeners: list[dict[str, Any]] = field(default_factory=list)
    error_pages: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)        # {kind, name, cls, reason, property}
    # v2.1x D4: (Boot key, as-is value, comment) of skipped declarations — the restructure appends them to the boot
    # application.properties (keys already configured there are left alone)
    properties: list[tuple[str, str, str]] = field(default_factory=list)
    # v2.1x D11: (filter-name, target bean) of DelegatingFilterProxy declarations registered as
    # DelegatingFilterProxyRegistrationBean (a non-security target)
    proxies: list[tuple[str, str]] = field(default_factory=list)
    dispatcher_mappings: list[tuple[str, list[str]]] = field(default_factory=list)   # (servlet, non-/ patterns)
    annotated: list[str] = field(default_factory=list)                 # FQCNs left to @ServletComponentScan
    jsp_file_servlets: list[str] = field(default_factory=list)         # "name → /path.jsp"
    notes: list[str] = field(default_factory=list)                     # also rendered into the class javadoc
    followups: list[str] = field(default_factory=list)
    session_timeout: Optional[int] = None

    @property
    def has_beans(self) -> bool:
        """등록할 빈이 하나라도 있는지 확인한다. 없으면 WebXmlRegistrations 클래스를 만들지 않는다."""
        return bool(self.servlets or self.filters or self.listeners or self.error_pages)


def load_skip_rules(path: Path) -> dict[str, SkipRule]:
    """skip 규칙 YAML을 읽어 {FQCN: SkipRule} 사전으로 반환한다.
    파일이 없거나 깨져 있으면 경고 로그만 남기고 빈 사전을 반환하므로, 모든 선언이 등록 대상이 될 뿐 재구성은 계속된다.

    `knowledge/config/web_xml_rules.yml` → {FQCN: rule}. Missing / broken file → {} (logged, never raises)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("web.xml rules not loaded from %s: %s", path, e)
        return {}
    out: dict[str, SkipRule] = {}

    def strmap(v: Any) -> dict[str, str]:
        return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}

    def conds(v: Any) -> dict[str, list[str]]:
        if not isinstance(v, dict):
            return {}
        return {str(k): [str(x) for x in (x_ if isinstance(x_, list) else [x_])] for k, x_ in v.items()}

    for row in raw.get("skip") or []:
        if isinstance(row, dict) and row.get("class"):
            other = str(row.get("otherwise") or "register").strip()
            out[str(row["class"]).strip()] = SkipRule(
                cls=str(row["class"]).strip(), reason=str(row.get("reason") or ""),
                property=(str(row["property"]) if row.get("property") else None),
                properties=strmap(row.get("properties")), set=strmap(row.get("set")),
                requires=conds(row.get("requires")),
                otherwise=other if other in ("register", "proxy", "followup") else "register",
                register_requires=conds(row.get("register_requires")), followup=str(row.get("followup") or ""))
    return out


def conditions_hold(conds: dict[str, list[str]], facts: Optional[RegistrationFacts], names: list[str]) -> bool:
    """``requires:`` 조건이 모두 맞는지 확인한다. 조건이 없으면 True를 반환한다.
    ``names``에는 filter-name과 init-param ``targetBeanName`` 값을 넘긴다.

    Do all `requires:` conditions hold (no condition = true)? `filter_name`: one of `names` is listed; `libraries`:
    the analyzer found one of them; `artifacts`: an as-is pom declares one of them. Unknown keys never hold."""
    for key, values in conds.items():
        if key == "filter_name":
            ok = any(n in values for n in names if n)
        elif key == "libraries":
            ok = facts is not None and bool(set(values) & facts.libraries)
        elif key == "artifacts":
            ok = facts is not None and bool(set(values) & facts.artifacts)
        else:
            ok = False
        if not ok:
            return False
    return True


def data_size(value: Optional[int | str]) -> Optional[str]:
    """바이트 수를 Boot DataSize 문자열로 바꾼다.
    1048576은 ``1MB``, 512000은 ``500KB``, 나머지는 ``<n>B``가 되고 -1은 그대로 둔다.
    숫자가 아닌 값(``${upload.max}``)은 그대로 반환한다.

    Bytes → a Spring Boot DataSize string (`1MB`, `500KB`, `123B`; `-1` = unlimited kept); a non-number (a
    placeholder) is returned unchanged (Boot reads a bare number as bytes)."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        n = int(text)
    except ValueError:
        return text or None
    if n < 0:
        return "-1"
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n and n % size == 0:
            return f"{n // size}{unit}"
    return f"{n}B"


def java_string(value: str) -> str:
    """값을 Java 문자열 리터럴로 바꾼다. 소스 인코딩(MS949 / UTF-8)과 상관없이 같은 값으로 컴파일되도록
    ASCII 밖의 문자는 유니코드 이스케이프로 바꾼다.

    A Java string literal (ASCII only: everything else as \\uXXXX, so the source encoding does not matter)."""
    out = ['"']
    for ch in value:
        o = ord(ch)
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
        elif 0x20 <= o < 0x7F:
            out.append(ch)
        elif o > 0xFFFF:
            o -= 0x10000
            out.append(f"\\u{0xD800 + (o >> 10):04x}\\u{0xDC00 + (o & 0x3FF):04x}")
        else:
            out.append(f"\\u{o:04x}")
    out.append('"')
    return "".join(out)


def doc_text(value: str) -> str:
    """javadoc 주석 안에 넣어도 안전한 텍스트로 바꾼다. web.xml의 이름이 주석을 닫거나 javadoc 태그로 읽히지
    않도록 ``*/``, ``@``, 중괄호, ASCII 밖 문자를 HTML 엔티티로 바꾼다.

    Text safe inside a javadoc comment."""
    value = value.replace("\u2192", "->").replace("*/", "*&#47;").replace("@", "&#64;")
    value = value.replace("{", "&#123;").replace("}", "&#125;")
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else " " if ch in "\r\n\t" else f"&#{ord(ch)};" for ch in value)


def source_name(fqcn: str) -> Optional[str]:
    """web.xml의 FQCN을 Java 소스 표기로 바꾼다(``a.B$C``는 ``a.B.C``).
    클래스 이름이 될 수 없으면 None을 반환하고, 호출하는 쪽이 수동 등록을 후속 작업으로 남긴다.

    The FQCN as written in Java source (`a.B$C` → `a.B.C`); None when it is not a valid class name."""
    fqcn = (fqcn or "").strip()
    if not _FQCN_RE.match(fqcn):
        return None
    parts = fqcn.replace("$", ".").split(".")
    if any(not p or p in _JAVA_KEYWORDS for p in parts):
        return None
    return ".".join(parts)


def _bean_name(raw: str, suffix: str, used: set[str]) -> str:
    """web.xml의 이름으로 다른 것과 겹치지 않는 @Bean 메서드 이름을 만든다.
    camelCase로 바꿔 접미사를 붙이고, 숫자로 시작하는 이름과 예약어를 피하며, 이미 쓰인 이름이면 2, 3 등을 붙인다."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", raw) if w]
    base = (words[0][:1].lower() + words[0][1:] + "".join(w[:1].upper() + w[1:] for w in words[1:])) if words else "x"
    if base[0].isdigit():
        base = "_" + base
    if not base.lower().endswith(suffix.lower()):
        base += suffix
    if base in _JAVA_KEYWORDS:
        base += "_"
    name, i = base, 2
    while name in used:
        name, i = f"{base}{i}", i + 1
    used.add(name)
    return name


def _jakarta(fqcn: str) -> str:
    """exception-type의 ``javax.servlet.*`` / ``javax.el.*`` 이름을 ``jakarta.*``로 바꾼다."""
    return "jakarta." + fqcn[len("javax."):] if fqcn.startswith(("javax.servlet.", "javax.el.")) else fqcn


def dispatcher_mapping_followup(name: str, patterns: list[str]) -> str:
    """Boot가 대신하는 DispatcherServlet에 ``/`` 외의 매핑이 있었을 때 인수인계 문장을 만든다.
    ``*.do`` 같은 확장자 매핑은 Boot의 ``/``가 그대로 받지만, ``.do`` 없이 호출하던 URL은 Spring 6부터 접미사 매칭이
    없어 맞지 않는다. ``/app/*`` 같은 경로 접두어는 ``spring.mvc.servlet.path``로 옮기라고 안내한다.

    The Handover sentence for a non-`/` mapping of the DispatcherServlet Spring Boot replaces (v2.1x D12). A suffix
    mapping (`*.do`) keeps reaching Boot's DispatcherServlet on `/`; a path prefix (`/app/*`) maps to
    `spring.mvc.servlet.path`."""
    shown = ", ".join(patterns)
    suffix = [p for p in patterns if p.startswith("*.")]
    prefix = [p for p in patterns if p.startswith("/") and p.endswith("/*") and len(p) > 2]
    parts = [f"web.xml DispatcherServlet {name} was mapped to {shown}; Spring Boot maps its DispatcherServlet to / "
             "(it receives those requests too)"]
    if suffix:
        exts = ", ".join(p[1:] for p in suffix)
        parts.append(f"request mappings that end in {exts} keep working; mappings written without {exts} relied on "
                     "suffix pattern matching, which Spring 6+ removed — add the suffix to them; "
                     "spring.mvc.servlet.path takes a path prefix only, not a suffix")
    if prefix:
        parts.append(f"for the path prefix {prefix[0]} set spring.mvc.servlet.path={prefix[0][:-2]} in the boot "
                     "application.properties (or drop the prefix from the URLs)")
    if not suffix and not prefix:
        parts.append("set spring.mvc.servlet.path or adjust the request mappings")
    return "; ".join(parts)


def plan_registrations(model: WebXmlModel, *, rules: dict[str, SkipRule],
                       lookup: Callable[[str], Optional[ClassInfo]], component_scan: bool,
                       web_path: Callable[[str], str] = lambda p: p, war: bool = False,
                       facts: Optional[RegistrationFacts] = None) -> RegistrationPlan:
    """web.xml 모델 하나로 등록 계획을 만든다.
    선언마다 skip 표에 있는 클래스는 Boot에 맡기고(속성 설정이 필요하면 후속 작업으로 남긴다), @Web*이
    붙었고 ``component_scan``이면 @ServletComponentScan에 맡기며, 나머지만 Registration 빈으로 만든다.
    ``lookup``은 프로젝트 소스에서 클래스를 찾는 함수이고, ``web_path``는
    JSP를 front 모듈로 옮긴 뒤의 웹 경로를 구하는 함수다.
    ``facts``로 ``requires:`` 조건을 확인하고, 건너뛴 선언의 init-param은 ``plan.properties``로 돌려준다.

    The render context + follow-ups for `model`. `lookup(fqcn)` finds a class in the project sources;
    `component_scan` = the Application carries @ServletComponentScan (annotated classes are left to it);
    `web_path` maps an as-is web path (JSP location) to where it lives after the front-module move. v2.1x: `facts`
    decides the rows' `requires:` (a DelegatingFilterProxy that is not the security chain becomes a
    DelegatingFilterProxyRegistrationBean) and enables `plan.properties` (init-params of skipped declarations →
    Boot keys)."""
    plan = RegistrationPlan()
    src = model.path
    props_on = facts is None or facts.filter_properties
    # 다른 빈이 같은 이름을 쓰지 않도록 템플릿에 있는 ErrorPageRegistrar 빈의 메서드 이름을 미리 예약한다
    used: set[str] = {"webXmlErrorPages"}
    skipped_servlets: set[str] = set()
    registered_servlets: set[str] = set()
    forced: dict[str, str] = {}             # filter-name → "register" | "proxy" (requires: not met)

    def skip(kind: str, name: str, cls: str, params: Optional[dict[str, str]] = None, quiet: bool = False) -> bool:
        rule = rules.get(cls)
        if rule is None:
            return False
        params = params or {}
        if rule.requires and not conditions_hold(rule.requires, facts, [name, params.get("targetBeanName", "")]):
            # 표의 건너뛸 사유가 이 선언에는 맞지 않는 경우다.
            # 보안용이 아닌 DelegatingFilterProxy나 Boot JPA가 없을 때의 OEMIV가 여기에 해당한다
            action = rule.otherwise
            if action == "register" and rule.register_requires and \
                    not conditions_hold(rule.register_requires, facts, [name]):
                action = "followup"
            if action in ("register", "proxy"):
                forced[name] = action
                plan.notes.append(f"{kind} {name} ({cls}) registered in {CLASS_NAME}: the skip reason does not hold "
                                  f"here ({rule.reason})")
                return False
            text = (rule.followup or f"web.xml {kind} {{name}} ({{cls}}) not registered: {rule.reason}")
            plan.skipped.append({"kind": kind, "name": name, "cls": cls, "reason": rule.reason, "property": ""})
            plan.followups.append(text.replace("{name}", name).replace("{cls}", cls))
            return True
        plan.skipped.append({"kind": kind, "name": name, "cls": cls, "reason": rule.reason,
                             "property": rule.property or ""})
        # 운영자가 직접 옮기지 않아도 똑같이 동작하도록 건너뛴 선언의 원래 값을 Boot 속성으로 옮긴다
        written: list[str] = []
        if props_on:
            for pname, key in rule.properties.items():
                if pname in params and params[pname].strip():
                    plan.properties.append((key, params[pname].strip(), f"web.xml {kind} {name} ({cls}) {pname}"))
                    written.append(f"{key}={params[pname].strip()}")
            for key, value in rule.set.items():
                plan.properties.append((key, value, f"web.xml {kind} {name} ({cls})"))
                written.append(f"{key}={value}")
        if written:
            plan.notes.append(f"{kind} {name} ({cls}) not registered in {CLASS_NAME}: {rule.reason}; written to the boot "
                              f"application.properties: {', '.join(written)}")
        # a skipped class with a Boot property to set is the owners' job (follow-up); one Boot simply provides is a note
        elif rule.property and (rule.properties or rule.set) and not props_on:
            plan.followups.append(f"web.xml {kind} {name} ({cls}) not registered in {CLASS_NAME}: {rule.reason} "
                                  f"(property: {rule.property}; layout.legacy_context.filter_properties: false)")
        elif rule.property and not (rule.properties or rule.set) and not quiet:
            plan.followups.append(f"web.xml {kind} {name} ({cls}) not registered in {CLASS_NAME}: {rule.reason} "
                                  f"(property: {rule.property})")
        else:
            plan.notes.append(f"{kind} {name} ({cls}) not registered in {CLASS_NAME}: {rule.reason}")
        return True

    def annotated(kind: str, name: str, cls: str, info: Optional[ClassInfo]) -> bool:
        if info is not None and info.annotated and component_scan:
            plan.annotated.append(cls)
            plan.notes.append(f"{kind} {name} ({cls}) is annotated and registered by @ServletComponentScan")
            return True
        return False

    for s in model.servlets:
        # <jsp-file> servlet은 등록할 클래스가 없으므로 옮겨진 JSP 경로와 함께 컨트롤러 매핑을 후속 작업으로 남긴다
        if s.jsp_file and not s.servlet_class:
            target = web_path(s.jsp_file)
            plan.jsp_file_servlets.append(f"{s.name} → {target}")
            plan.followups.append(f"jsp-file servlet {s.name} → {target} ({', '.join(s.url_patterns) or 'no mapping'}): "
                                  "map it in a controller or link to the JSP directly")
            continue
        if not s.servlet_class:
            continue
        # 매핑이 Boot의 /와 같거나 없으면 속성 안내가 필요 없고, 다른
        # 매핑이면 아래 매핑 안내가 대신하므로 노트로만 남긴다
        rule_ = rules.get(s.servlet_class)
        quiet = s.url_patterns in ([], ["/"]) or (rule_ is not None and rule_.cls.endswith("DispatcherServlet"))
        if skip("servlet", s.name, s.servlet_class, s.init_params, quiet=quiet):
            skipped_servlets.add(s.name)
            # 원래 DispatcherServlet을 Boot의 것으로 대신하면 매핑이 /가 되므로, 다른 매핑이었으면 URL이
            # 바뀐다는 후속 작업을 남긴다
            dispatcher = rules[s.servlet_class].cls.endswith("DispatcherServlet")
            if dispatcher and s.url_patterns and s.url_patterns != ["/"]:
                plan.dispatcher_mappings.append((s.name, list(s.url_patterns)))
                plan.followups.append(dispatcher_mapping_followup(s.name, s.url_patterns))
            # Boot가 대신하는 DispatcherServlet의 <multipart-config>는 사라지므로
            # spring.servlet.multipart.*로 옮긴다
            if dispatcher and s.multipart_location is not None:
                keys = (facts.multipart_keys if facts is not None else {}) or {}
                vals = {"location": s.multipart_location or None,
                        "max_file_size": data_size(s.multipart_max_file_size),
                        "max_request_size": data_size(s.multipart_max_request_size),
                        "file_size_threshold": data_size(s.multipart_file_size_threshold)}
                got = [(keys[k], v) for k, v in vals.items() if v is not None and keys.get(k)]
                if props_on and got:
                    for key, v in got:
                        plan.properties.append((key, v, f"web.xml servlet {s.name} <multipart-config>"))
                    plan.notes.append(f"servlet {s.name} <multipart-config> → "
                                      + ", ".join(f"{k}={v}" for k, v in got))
                else:
                    plan.followups.append(f"web.xml servlet {s.name} <multipart-config> is dropped with the servlet "
                                          "Spring Boot replaces — set spring.servlet.multipart.* (Boot's default is 1MB "
                                          "per file)")
            continue
        info = lookup(s.servlet_class)
        if annotated("servlet", s.name, s.servlet_class, info):
            registered_servlets.add(s.name)
            continue
        cls = source_name(s.servlet_class)
        if cls is None:
            plan.followups.append(f"web.xml servlet {s.name}: class {s.servlet_class!r} is not a valid Java class name "
                                  "— register it by hand")
            continue
        if info is None:
            plan.notes.append(f"servlet {s.name}: {s.servlet_class} is not a project class (library servlet); "
                              "it compiles only when that library (jakarta build) is on the classpath")
        # 매핑 없는 servlet도 load-on-startup 용도일 수 있으므로 등록한다.
        # alwaysMapUrl=false라서 /*에 매핑되지는 않는다
        if not s.url_patterns:
            plan.notes.append(f"servlet {s.name} has no servlet-mapping in web.xml; registered without URL mappings "
                              "(alwaysMapUrl=false, so Boot does not map it to /*)")
        mp = None
        if s.multipart_location is not None:
            mp = ", ".join([java_string(s.multipart_location or ""),
                            f"{s.multipart_max_file_size if s.multipart_max_file_size is not None else -1}L",
                            f"{s.multipart_max_request_size if s.multipart_max_request_size is not None else -1}L",
                            str(s.multipart_file_size_threshold or 0)])
        plan.servlets.append({
            "name": doc_text(s.name), "name_lit": java_string(s.name), "cls": cls,
            "bean": _bean_name(s.name, "Servlet", used),
            "type": "jakarta.servlet.http.HttpServlet" if info is not None and info.http_servlet
                    else "jakarta.servlet.Servlet",
            "patterns": [java_string(p) for p in s.url_patterns],
            "patterns_doc": doc_text(", ".join(s.url_patterns)),
            "load_on_startup": s.load_on_startup,
            "params": [(java_string(k), java_string(v)) for k, v in s.init_params.items()],
            "async": s.async_supported, "multipart": mp,
        })
        registered_servlets.add(s.name)

    for f in model.filters:
        if skip("filter", f.name, f.filter_class, f.init_params):
            continue
        info = lookup(f.filter_class)
        if annotated("filter", f.name, f.filter_class, info):
            continue
        cls = source_name(f.filter_class)
        if cls is None:
            plan.followups.append(f"web.xml filter {f.name}: class {f.filter_class!r} is not a valid Java class name "
                                  "— register it by hand")
            continue
        if info is None and forced.get(f.name) != "proxy":
            plan.notes.append(f"filter {f.name}: {f.filter_class} is not a project class (library filter); "
                              "it compiles only when that library (jakarta build) is on the classpath")
        # filter-mapping이 없는 filter는 원래도 호출된 적이 없으므로, 등록하면 오히려 동작이 바뀌어 건너뛴다
        if not f.order:
            plan.notes.append(f"filter {f.name} has no filter-mapping in web.xml (never invoked there); not registered")
            continue
        for sn in f.servlet_names:
            # servlet 이름으로 매핑된 filter는 그 servlet을 Boot가 대신하면
            # 이름이 dispatcherServlet으로 바뀌어 매핑이 끊긴다
            if sn in skipped_servlets:
                plan.followups.append(f"web.xml filter {f.name} is mapped to servlet {sn}, which Spring Boot replaces "
                                      "(its DispatcherServlet is named dispatcherServlet) — check the filter mapping")
            elif sn not in registered_servlets and sn != "*":
                plan.notes.append(f"filter {f.name} names servlet {sn}, which is not registered by {CLASS_NAME}")
        # 보안 체인이 아닌 DelegatingFilterProxy는 대상 빈에 위임하는 Boot
        # 등록 빈으로 만들고, 원래 매핑과 순서는 유지한다
        proxy = None
        params = dict(f.init_params)
        if forced.get(f.name) == "proxy":
            proxy = params.pop("targetBeanName", "").strip() or f.name
            plan.proxies.append((f.name, proxy))
        plan.filters.append({
            "name": doc_text(f.name), "name_lit": java_string(f.name), "cls": cls,
            "bean": _bean_name(f.name, "Filter", used),
            "patterns": [java_string(p) for p in f.url_patterns],
            "servlet_names": [java_string(n) for n in f.servlet_names],
            "dispatchers": [f"jakarta.servlet.DispatcherType.{d}" for d in f.dispatchers],
            "params": [(java_string(k), java_string(v)) for k, v in params.items()],
            "async": f.async_supported, "order": f.order,
            "proxy": java_string(proxy) if proxy else None, "proxy_doc": doc_text(proxy or ""),
        })

    for li in model.listeners:
        if skip("listener", li.rsplit(".", 1)[-1], li):
            continue
        info = lookup(li)
        if annotated("listener", li.rsplit(".", 1)[-1], li, info):
            continue
        cls = source_name(li)
        if cls is None:
            plan.followups.append(f"web.xml listener {li!r} is not a valid Java class name — register it by hand")
            continue
        if info is None:
            plan.notes.append(f"listener {li} is not a project class (library listener); it compiles only when that "
                              "library (jakarta build) is on the classpath")
        plan.listeners.append({"cls": cls, "bean": _bean_name(cls.rsplit(".", 1)[-1], "Listener", used)})

    for ep in model.error_pages:
        loc = java_string(web_path(ep.location))
        if ep.error_code is not None:
            # HttpStatus.valueOf는 모르는 코드에서 예외를 던져 앱 기동이 실패하므로 알려진 코드만 생성하고
            # 나머지는 후속 작업으로 남긴다
            if ep.error_code not in ERROR_STATUS_CODES:
                plan.followups.append(f"web.xml error-page {ep.error_code} → {ep.location}: status not generated "
                                      "(unknown to HttpStatus.valueOf) — register it in an ErrorPageRegistrar")
                continue
            plan.error_pages.append(f"new ErrorPage(org.springframework.http.HttpStatus.valueOf({ep.error_code}), {loc})")
        elif ep.exception_type:
            # exception-type의 javax.servlet / javax.el 이름은 jakarta 이름으로 바꾼다
            cls = source_name(_jakarta(ep.exception_type))
            if cls is None:
                plan.followups.append(f"web.xml error-page {ep.exception_type!r} → {ep.location}: not a valid class "
                                      "name — register it by hand")
                continue
            plan.error_pages.append(f"new ErrorPage({cls}.class, {loc})")
        else:
            plan.error_pages.append(f"new ErrorPage({loc})")

    if model.session_timeout_minutes is not None:
        plan.session_timeout = model.session_timeout_minutes
    if any(w.strip("/") != "index.html" for w in model.welcome_files):   # index.html alone: Boot serves it
        plan.followups.append(f"web.xml welcome-file-list ({', '.join(model.welcome_files)}): Spring Boot serves a "
                              "static index.html only; for a JSP welcome page add a view controller / redirect for /")
    if model.mime_mappings:
        maps = ", ".join(f"{k} → {v}" for k, v in model.mime_mappings.items())
        plan.followups.append(f"web.xml mime-mapping ({maps}) is not applied by Spring Boot: add a "
                              "WebServerFactoryCustomizer<ConfigurableServletWebServerFactory> that adds them to "
                              "MimeMappings (java -jar, embedded Tomcat); an external Tomcat deployment reads them from "
                              "its conf/web.xml")
    if model.security_constraints or model.login_config:
        bits = []
        if model.security_constraints:
            bits.append(f"{model.security_constraints} security-constraint(s)")
        if model.login_config:
            bits.append(f"login-config auth-method {model.login_config}")
        plan.followups.append(f"web.xml {' and '.join(bits)} are not applied by Spring Boot: re-create them with "
                              "Spring Security (SecurityFilterChain) or the container's configuration")
    if model.distributable:
        plan.notes.append("web.xml is <distributable/>: session replication is configured in the container")
    for w in model.warnings:
        plan.notes.append(f"web.xml parse warning: {w}")
    # 외부 Tomcat에 배포하는 WAR는 server.servlet.*를 읽지 않으므로 session-timeout을 Tomcat 설정에도 넣어야 한다
    if plan.session_timeout is not None and war:
        plan.followups.append(f"session-timeout {plan.session_timeout} min (web.xml) is written as "
                              "server.servlet.session.timeout, which applies when the WAR runs with java -jar (embedded "
                              "Tomcat); deployed to an external Tomcat instead, set it in that Tomcat's conf/web.xml "
                              "<session-config> (default 30)")
    log.debug("web.xml %s: %d servlet(s), %d filter(s), %d listener(s), %d error page(s), %d skipped", src,
              len(plan.servlets), len(plan.filters), len(plan.listeners), len(plan.error_pages), len(plan.skipped))
    return plan
