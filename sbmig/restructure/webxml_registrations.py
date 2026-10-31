"""web.xml → 생성 클래스 ``WebXmlRegistrations`` 의 등록 계획(2026-10-23,
``layout.web_xml_registrations: generate``).
왜: Servlet 3 이전(web.xml 2.2 / 2.3 / 2.4, ``@WebServlet`` 없음) 프로젝트가 Spring Boot 가 되면 web.xml 의
servlet / filter / listener 매핑을 전부 잃는다 — 내장 Tomcat 은 web.xml 을 읽지 않고 회사 Boot WAR 도 web.xml
을 넣지 않는다. 흐름상 위치: 재구성 단계의 ``Restructurer._web_xml_registrations`` 가 ``plan_registrations``
로 계획을 받아 ``templates/WebXmlRegistrations.java.j2`` 로 boot 모듈에 클래스를 쓴다. 이 모듈은 순수
함수뿐이다(파일 I/O 는 restructurer). 빈으로 옮길 수 없는 것(jsp-file servlet, welcome-file, MIME,
security-constraint ...)은 후속 작업(follow-up)이 된다. 중복 등록 금지: Boot 가 대신 해 주는 클래스는
``knowledge/config/web_xml_rules.yml`` 의 skip 표로 빼고(불변조건 2: 규칙은 YAML), ``@WebServlet`` 등이 붙은
클래스는 Application 에 ``@ServletComponentScan`` 이 있으면 스캔에 맡긴다.

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

v2.1x (2026-10-31, 프롬프트 검토 §4 D4 / D11): 건너뛴 선언의 init-param 을 Boot 속성으로 돌려주고
(``RegistrationPlan.properties`` — CharacterEncodingFilter → ``spring.servlet.encoding.*``, Boot 가 대신하는
DispatcherServlet 의 ``<multipart-config>`` → ``spring.servlet.multipart.*``), 표의 ``requires:`` 가 맞을 때만 건너뛴다
(보안 아닌 DelegatingFilterProxy → ``DelegatingFilterProxyRegistrationBean``, JPA 를 쓰는데 Boot JPA 가 없으면
OpenEntityManagerInViewFilter 를 그대로 등록).


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
    """web_xml_rules.yml 의 skip 한 줄: 등록하지 않을 클래스(FQCN), 그 이유, 대신
    설정해야 할 Boot 속성(있으면). v2.1x: init-param → Boot 키(``properties``), 늘 쓰는 Boot 값(``set``),
    건너뛸 조건(``requires``)과 조건이 안 맞을 때의 처리(``otherwise``: register / proxy / followup).

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
    """``requires:`` 를 판정할 프로젝트 사실(분석기 라이브러리 이름, as-is pom 이 선언한 g:a)과 v2.1x 스위치.

    Project facts the `requires:` conditions are checked against (analyzer library names, the g:a keys the as-is
    poms declare) and the v2.1x switches (`filter_properties`; the multipart-config key table)."""
    libraries: set[str] = field(default_factory=set)
    artifacts: set[str] = field(default_factory=set)
    filter_properties: bool = True
    multipart_keys: dict[str, str] = field(default_factory=dict)   # location / max_file_size / … → Boot key


@dataclass
class ClassInfo:
    """web.xml 이 이름 댄 클래스를 프로젝트 소스에서 찾은 결과(경로, @Web* 어노테이션, HttpServlet 직접 상속).

    What the project sources say about a class named in web.xml (None from the lookup = not in the project)."""
    path: str
    annotated: bool = False          # @WebServlet / @WebFilter / @WebListener on it
    http_servlet: bool = False       # `extends HttpServlet` directly


@dataclass
class RegistrationPlan:
    """``plan_registrations`` 의 결과 = 템플릿 렌더링 문맥(servlets / filters / listeners / error_pages)과
    건너뛴 것(skipped), 스캔에 맡긴 것(annotated), 노트, 후속 작업, session-timeout."""
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
        """등록할 빈이 하나라도 있는가 — 없으면 WebXmlRegistrations 클래스를 만들지 않는다."""
        return bool(self.servlets or self.filters or self.listeners or self.error_pages)


def load_skip_rules(path: Path) -> dict[str, SkipRule]:
    """skip 규칙 YAML → {FQCN: SkipRule}. 파일이 없거나 깨져도 예외 없이 {} (경고 로그만): 규칙이 없으면 전부
    등록 대상이 될 뿐 재구성은 멈추지 않는다.

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
    """``requires:`` 조건이 모두 맞는가(조건 없음 = 맞음). ``names`` = filter-name 과 ``targetBeanName`` init-param.

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
    """바이트 수 → Boot DataSize 문자열(1048576 → ``1MB``, 512000 → ``500KB``, 나머지는 ``<n>B``; -1 = 무제한
    그대로). 숫자가 아니면(``${upload.max}``) 그대로 — 단위 없는 숫자는 Boot 가 바이트로 읽는다.

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
    """값 → Java 문자열 리터럴. ASCII 밖의 문자는 전부 유니코드 이스케이프로 바꾼다 — 한글 init-param 등이
    소스 인코딩(MS949 / UTF-8)과 상관없이 같은 값으로 컴파일되게.

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
    """javadoc 주석 안에 넣어도 안전한 텍스트(``*/``, ``@``, 중괄호, ASCII 밖 문자 → HTML 엔티티): web.xml 의
    이름·경로가 생성 클래스의 주석을 닫거나 javadoc 태그로 읽히지 않게.

    Text safe inside a javadoc comment."""
    value = value.replace("\u2192", "->").replace("*/", "*&#47;").replace("@", "&#64;")
    value = value.replace("{", "&#123;").replace("}", "&#125;")
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else " " if ch in "\r\n\t" else f"&#{ord(ch)};" for ch in value)


def source_name(fqcn: str) -> Optional[str]:
    """web.xml 의 FQCN → Java 소스 표기(``a.B$C`` → ``a.B.C``). 클래스 이름이 될 수 없으면(예약어, 잘못된
    문자) None — 호출부가 '손으로 등록' 후속 작업을 남긴다.

    The FQCN as written in Java source (`a.B$C` → `a.B.C`); None when it is not a valid class name."""
    fqcn = (fqcn or "").strip()
    if not _FQCN_RE.match(fqcn):
        return None
    parts = fqcn.replace("$", ".").split(".")
    if any(not p or p in _JAVA_KEYWORDS for p in parts):
        return None
    return ".".join(parts)


def _bean_name(raw: str, suffix: str, used: set[str]) -> str:
    """web.xml 이름 → 겹치지 않는 @Bean 메서드 이름(camelCase + 접미사, 숫자 시작·예약어 회피,
    중복이면 2, 3 ... 을 붙임)."""
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
    """exception-type 의 ``javax.servlet.*`` / ``javax.el.*`` → ``jakarta.*`` (Boot 4 / Servlet 6 이름)."""
    return "jakarta." + fqcn[len("javax."):] if fqcn.startswith(("javax.servlet.", "javax.el.")) else fqcn


def dispatcher_mapping_followup(name: str, patterns: list[str]) -> str:
    """Boot 가 대신하는 DispatcherServlet 의 ``/`` 아닌 매핑에 대한 인수인계 문장(v2.1x D12): ``*.do`` 같은 확장자
    매핑은 Boot 의 ``/`` 가 그대로 받지만 ``.do`` 없이 쓴 매핑은 Spring 6+ 에서 접미사 매칭이 없어 안 맞고,
    ``/app/*`` 같은 경로 접두는 ``spring.mvc.servlet.path`` 로 옮긴다.

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
    """web.xml 모델 하나 → 등록 계획. 선언마다 (1) skip 표의 클래스는 Boot 에 맡기고(속성이 필요하면 후속
    작업), (2) @Web* 이 붙었고 ``component_scan`` 이면 @ServletComponentScan 에 맡기고, (3) 나머지만
    Registration 빈으로 만든다. ``lookup`` = 프로젝트 소스의 클래스
    찾기, ``web_path`` = JSP 가 front 모듈로 옮겨진 뒤의 웹 경로. v2.1x: ``facts`` 로 ``requires:`` 를 판정하고
    건너뛴 선언의 init-param 을 ``plan.properties`` 로 돌려준다(``facts.filter_properties``).

    The render context + follow-ups for `model`. `lookup(fqcn)` finds a class in the project sources;
    `component_scan` = the Application carries @ServletComponentScan (annotated classes are left to it);
    `web_path` maps an as-is web path (JSP location) to where it lives after the front-module move. v2.1x: `facts`
    decides the rows' `requires:` (a DelegatingFilterProxy that is not the security chain becomes a
    DelegatingFilterProxyRegistrationBean) and enables `plan.properties` (init-params of skipped declarations →
    Boot keys)."""
    plan = RegistrationPlan()
    src = model.path
    props_on = facts is None or facts.filter_properties
    # 템플릿의 ErrorPageRegistrar 빈 메서드 이름(webXmlErrorPages)을
    # 미리 예약: 다른 빈이 같은 이름을 받지 않게
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
            # v2.1x D11: 표의 이유가 이 선언에는 맞지 않는다(보안 아닌 DelegatingFilterProxy, Boot JPA 없는 OEMIV)
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
        # v2.1x D4: 건너뛴 선언의 as-is 값을 Boot 속성으로 — 운영자가 직접 옮기지 않아도 같은 동작
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
        # <jsp-file> servlet 은 등록할 클래스가 없다: 옮겨진 JSP 경로와 함께 컨트롤러 매핑을 사람에게 넘긴다
        if s.jsp_file and not s.servlet_class:
            target = web_path(s.jsp_file)
            plan.jsp_file_servlets.append(f"{s.name} → {target}")
            plan.followups.append(f"jsp-file servlet {s.name} → {target} ({', '.join(s.url_patterns) or 'no mapping'}): "
                                  "map it in a controller or link to the JSP directly")
            continue
        if not s.servlet_class:
            continue
        # Boot 의 / 와 같은 매핑(또는 매핑 없음)이면 속성 안내는 소음, 다른 매핑이면 아래 매핑 문장이 대신한다 — 노트로만
        rule_ = rules.get(s.servlet_class)
        quiet = s.url_patterns in ([], ["/"]) or (rule_ is not None and rule_.cls.endswith("DispatcherServlet"))
        if skip("servlet", s.name, s.servlet_class, s.init_params, quiet=quiet):
            skipped_servlets.add(s.name)
            # as-is DispatcherServlet 을 Boot 것으로 대신하면 매핑이
            # / 가 된다: 다른 매핑이었으면 URL 이 바뀌므로 후속 작업
            dispatcher = rules[s.servlet_class].cls.endswith("DispatcherServlet")
            if dispatcher and s.url_patterns and s.url_patterns != ["/"]:
                plan.dispatcher_mappings.append((s.name, list(s.url_patterns)))
                plan.followups.append(dispatcher_mapping_followup(s.name, s.url_patterns))
            # v2.1x D4: Boot 가 대신하는 DispatcherServlet 의 <multipart-config> 는 사라진다 → spring.servlet.multipart.*
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
        # 매핑 없는 servlet 도 등록은 한다(load-on-startup 용일
        # 수 있음). alwaysMapUrl=false 라서 /* 로 잡히지 않는다
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
        # filter-mapping 이 없는 filter 는 as-is 에서도 한 번도 불리지 않았다 — 등록하면 오히려 동작이 바뀐다
        if not f.order:
            plan.notes.append(f"filter {f.name} has no filter-mapping in web.xml (never invoked there); not registered")
            continue
        for sn in f.servlet_names:
            # servlet 이름으로 걸린 filter: 그 servlet 을 Boot 가
            # 대신하면 이름이 dispatcherServlet 이 되어 매핑이 끊긴다
            if sn in skipped_servlets:
                plan.followups.append(f"web.xml filter {f.name} is mapped to servlet {sn}, which Spring Boot replaces "
                                      "(its DispatcherServlet is named dispatcherServlet) — check the filter mapping")
            elif sn not in registered_servlets and sn != "*":
                plan.notes.append(f"filter {f.name} names servlet {sn}, which is not registered by {CLASS_NAME}")
        # v2.1x D11: 보안 체인이 아닌 DelegatingFilterProxy — 대상 빈에 위임하는 Boot 등록 빈(as-is 매핑·순서 유지)
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
            # HttpStatus.valueOf 는 모르는 코드에서 예외를 던져 앱
            # 기동이 실패한다: 알려진 코드만 생성, 나머지는 후속 작업
            if ep.error_code not in ERROR_STATUS_CODES:
                plan.followups.append(f"web.xml error-page {ep.error_code} → {ep.location}: status not generated "
                                      "(unknown to HttpStatus.valueOf) — register it in an ErrorPageRegistrar")
                continue
            plan.error_pages.append(f"new ErrorPage(org.springframework.http.HttpStatus.valueOf({ep.error_code}), {loc})")
        elif ep.exception_type:
            # exception-type 의 javax.servlet / javax.el 은 Boot 4 (jakarta) 에 맞게 이름을 바꾼다
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
    # 외부 Tomcat 에 배포하는 WAR 는 server.servlet.* 를 읽지
    # 않는다: session-timeout 을 Tomcat 설정에 다시 적어야 함
    if plan.session_timeout is not None and war:
        plan.followups.append(f"session-timeout {plan.session_timeout} min (web.xml) is written as "
                              "server.servlet.session.timeout, which applies when the WAR runs with java -jar (embedded "
                              "Tomcat); deployed to an external Tomcat instead, set it in that Tomcat's conf/web.xml "
                              "<session-config> (default 30)")
    log.debug("web.xml %s: %d servlet(s), %d filter(s), %d listener(s), %d error page(s), %d skipped", src,
              len(plan.servlets), len(plan.filters), len(plan.listeners), len(plan.error_pages), len(plan.skipped))
    return plan
