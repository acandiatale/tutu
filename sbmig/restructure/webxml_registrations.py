"""web.xml → `WebXmlRegistrations` (2026-10-23, `layout.web_xml_registrations: generate`).

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
`ServletRegistrationBean.setMultipartConfig(jakarta.servlet.MultipartConfigElement)`.
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
    cls: str
    reason: str
    property: Optional[str] = None


@dataclass
class ClassInfo:
    """What the project sources say about a class named in web.xml (None from the lookup = not in the project)."""
    path: str
    annotated: bool = False          # @WebServlet / @WebFilter / @WebListener on it
    http_servlet: bool = False       # `extends HttpServlet` directly


@dataclass
class RegistrationPlan:
    servlets: list[dict[str, Any]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    listeners: list[dict[str, Any]] = field(default_factory=list)
    error_pages: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)        # {kind, name, cls, reason, property}
    annotated: list[str] = field(default_factory=list)                 # FQCNs left to @ServletComponentScan
    jsp_file_servlets: list[str] = field(default_factory=list)         # "name → /path.jsp"
    notes: list[str] = field(default_factory=list)                     # also rendered into the class javadoc
    followups: list[str] = field(default_factory=list)
    session_timeout: Optional[int] = None

    @property
    def has_beans(self) -> bool:
        return bool(self.servlets or self.filters or self.listeners or self.error_pages)


def load_skip_rules(path: Path) -> dict[str, SkipRule]:
    """`knowledge/config/web_xml_rules.yml` → {FQCN: rule}. Missing / broken file → {} (logged, never raises)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("web.xml rules not loaded from %s: %s", path, e)
        return {}
    out: dict[str, SkipRule] = {}
    for row in raw.get("skip") or []:
        if isinstance(row, dict) and row.get("class"):
            out[str(row["class"]).strip()] = SkipRule(cls=str(row["class"]).strip(), reason=str(row.get("reason") or ""),
                                                      property=(str(row["property"]) if row.get("property") else None))
    return out


def java_string(value: str) -> str:
    """A Java string literal (ASCII only: everything else as \\uXXXX, so the source encoding does not matter)."""
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
    """Text safe inside a javadoc comment."""
    value = value.replace("\u2192", "->").replace("*/", "*&#47;").replace("@", "&#64;")
    value = value.replace("{", "&#123;").replace("}", "&#125;")
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else " " if ch in "\r\n\t" else f"&#{ord(ch)};" for ch in value)


def source_name(fqcn: str) -> Optional[str]:
    """The FQCN as written in Java source (`a.B$C` → `a.B.C`); None when it is not a valid class name."""
    fqcn = (fqcn or "").strip()
    if not _FQCN_RE.match(fqcn):
        return None
    parts = fqcn.replace("$", ".").split(".")
    if any(not p or p in _JAVA_KEYWORDS for p in parts):
        return None
    return ".".join(parts)


def _bean_name(raw: str, suffix: str, used: set[str]) -> str:
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
    return "jakarta." + fqcn[len("javax."):] if fqcn.startswith(("javax.servlet.", "javax.el.")) else fqcn


def plan_registrations(model: WebXmlModel, *, rules: dict[str, SkipRule],
                       lookup: Callable[[str], Optional[ClassInfo]], component_scan: bool,
                       web_path: Callable[[str], str] = lambda p: p, war: bool = False) -> RegistrationPlan:
    """The render context + follow-ups for `model`. `lookup(fqcn)` finds a class in the project sources;
    `component_scan` = the Application carries @ServletComponentScan (annotated classes are left to it);
    `web_path` maps an as-is web path (JSP location) to where it lives after the front-module move."""
    plan = RegistrationPlan()
    src = model.path
    used: set[str] = {"webXmlErrorPages"}
    skipped_servlets: set[str] = set()
    registered_servlets: set[str] = set()

    def skip(kind: str, name: str, cls: str) -> bool:
        rule = rules.get(cls)
        if rule is None:
            return False
        plan.skipped.append({"kind": kind, "name": name, "cls": cls, "reason": rule.reason,
                             "property": rule.property or ""})
        # a skipped class with a Boot property to set is the owners' job (follow-up); one Boot simply provides is a note
        if rule.property:
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
        if s.jsp_file and not s.servlet_class:
            target = web_path(s.jsp_file)
            plan.jsp_file_servlets.append(f"{s.name} → {target}")
            plan.followups.append(f"jsp-file servlet {s.name} → {target} ({', '.join(s.url_patterns) or 'no mapping'}): "
                                  "map it in a controller or link to the JSP directly")
            continue
        if not s.servlet_class:
            continue
        if skip("servlet", s.name, s.servlet_class):
            skipped_servlets.add(s.name)
            if rules[s.servlet_class].cls.endswith("DispatcherServlet") and s.url_patterns and s.url_patterns != ["/"]:
                plan.followups.append(f"web.xml DispatcherServlet {s.name} was mapped to {', '.join(s.url_patterns)}; "
                                      "Spring Boot maps its DispatcherServlet to / — set spring.mvc.servlet.path or "
                                      "adjust the request mappings")
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
        if skip("filter", f.name, f.filter_class):
            continue
        info = lookup(f.filter_class)
        if annotated("filter", f.name, f.filter_class, info):
            continue
        cls = source_name(f.filter_class)
        if cls is None:
            plan.followups.append(f"web.xml filter {f.name}: class {f.filter_class!r} is not a valid Java class name "
                                  "— register it by hand")
            continue
        if info is None:
            plan.notes.append(f"filter {f.name}: {f.filter_class} is not a project class (library filter); "
                              "it compiles only when that library (jakarta build) is on the classpath")
        if not f.order:
            plan.notes.append(f"filter {f.name} has no filter-mapping in web.xml (never invoked there); not registered")
            continue
        for sn in f.servlet_names:
            if sn in skipped_servlets:
                plan.followups.append(f"web.xml filter {f.name} is mapped to servlet {sn}, which Spring Boot replaces "
                                      "(its DispatcherServlet is named dispatcherServlet) — check the filter mapping")
            elif sn not in registered_servlets and sn != "*":
                plan.notes.append(f"filter {f.name} names servlet {sn}, which is not registered by {CLASS_NAME}")
        plan.filters.append({
            "name": doc_text(f.name), "name_lit": java_string(f.name), "cls": cls,
            "bean": _bean_name(f.name, "Filter", used),
            "patterns": [java_string(p) for p in f.url_patterns],
            "servlet_names": [java_string(n) for n in f.servlet_names],
            "dispatchers": [f"jakarta.servlet.DispatcherType.{d}" for d in f.dispatchers],
            "params": [(java_string(k), java_string(v)) for k, v in f.init_params.items()],
            "async": f.async_supported, "order": f.order,
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
            if ep.error_code not in ERROR_STATUS_CODES:
                plan.followups.append(f"web.xml error-page {ep.error_code} → {ep.location}: status not generated "
                                      "(unknown to HttpStatus.valueOf) — register it in an ErrorPageRegistrar")
                continue
            plan.error_pages.append(f"new ErrorPage(org.springframework.http.HttpStatus.valueOf({ep.error_code}), {loc})")
        elif ep.exception_type:
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
                              "MimeMappings (embedded) or keep them in the external Tomcat conf/web.xml")
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
    if plan.session_timeout is not None and war:
        plan.followups.append(f"session-timeout {plan.session_timeout} min (web.xml) is written as "
                              "server.servlet.session.timeout, which only the embedded Tomcat applies: on the external "
                              "Tomcat set it in its conf/web.xml <session-config> (default 30)")
    log.debug("web.xml %s: %d servlet(s), %d filter(s), %d listener(s), %d error page(s), %d skipped", src,
              len(plan.servlets), len(plan.filters), len(plan.listeners), len(plan.error_pages), len(plan.skipped))
    return plan
