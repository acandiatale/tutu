"""2026-10-23: web.xml → generated `WebXmlRegistrations` @Configuration in the boot module
(`layout.web_xml_registrations: generate`), follow-ups for what has no bean, `followup` policy = today's text."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.analyze.webxml import parse_web_xml
from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import Restructurer
from sbmig.restructure.restructurer import render
from sbmig.restructure.webxml_registrations import (
    ClassInfo,
    SkipRule,
    java_string,
    load_skip_rules,
    plan_registrations,
)

from .helpers import HEAD, dep, make_ws, profile

REPO = Path(__file__).resolve().parents[2]
RULES = load_skip_rules(REPO / "knowledge" / "config" / "web_xml_rules.yml")

WEB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE web-app PUBLIC "-//Sun Microsystems, Inc.//DTD Web Application 2.3//EN"
        "http://java.sun.com/dtd/web-app_2_3.dtd">
<web-app>
    <filter><filter-name>enc</filter-name>
        <filter-class>org.springframework.web.filter.CharacterEncodingFilter</filter-class></filter>
    <filter><filter-name>audit</filter-name><filter-class>com.acme.web.AuditFilter</filter-class>
        <init-param><param-name>tag</param-name><param-value>say "hi"</param-value></init-param></filter>
    <filter><filter-name>annotated</filter-name><filter-class>com.acme.web.TaggedFilter</filter-class></filter>
    <filter-mapping><filter-name>enc</filter-name><url-pattern>/*</url-pattern></filter-mapping>
    <filter-mapping><filter-name>audit</filter-name><url-pattern>/admin/*</url-pattern>
        <dispatcher>REQUEST</dispatcher><dispatcher>FORWARD</dispatcher></filter-mapping>
    <filter-mapping><filter-name>annotated</filter-name><url-pattern>/x/*</url-pattern></filter-mapping>
    <listener><listener-class>org.springframework.web.context.ContextLoaderListener</listener-class></listener>
    <listener><listener-class>com.acme.web.StartupListener</listener-class></listener>
    <servlet><servlet-name>front</servlet-name><servlet-class>com.acme.web.FrontServlet</servlet-class>
        <init-param><param-name>mode</param-name><param-value>fast</param-value></init-param>
        <load-on-startup>1</load-on-startup></servlet>
    <servlet><servlet-name>report</servlet-name><servlet-class>com.acme.web.ReportServlet</servlet-class></servlet>
    <servlet><servlet-name>help</servlet-name><jsp-file>/WEB-INF/jsp/help.jsp</jsp-file></servlet>
    <servlet><servlet-name>action</servlet-name>
        <servlet-class>org.apache.struts.action.ActionServlet</servlet-class></servlet>
    <servlet><servlet-name>dispatcher</servlet-name>
        <servlet-class>org.springframework.web.servlet.DispatcherServlet</servlet-class></servlet>
    <servlet><servlet-name>tagged</servlet-name><servlet-class>com.acme.web.TaggedServlet</servlet-class></servlet>
    <servlet-mapping><servlet-name>front</servlet-name><url-pattern>/front/*</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>front</servlet-name><url-pattern>*.do</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>action</servlet-name><url-pattern>*.action</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>dispatcher</servlet-name><url-pattern>*.htm</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>help</servlet-name><url-pattern>/help</url-pattern></servlet-mapping>
    <session-config><session-timeout>30</session-timeout></session-config>
    <mime-mapping><extension>hwp</extension><mime-type>application/x-hwp</mime-type></mime-mapping>
    <welcome-file-list><welcome-file>index.jsp</welcome-file></welcome-file-list>
    <error-page><error-code>404</error-code><location>/WEB-INF/jsp/error.jsp</location></error-page>
    <error-page><error-code>599</error-code><location>/WEB-INF/jsp/error.jsp</location></error-page>
    <error-page><exception-type>javax.servlet.ServletException</exception-type><location>/e.jsp</location></error-page>
    <error-page><location>/default.jsp</location></error-page>
    <security-constraint><web-resource-collection><url-pattern>/admin/*</url-pattern></web-resource-collection>
    </security-constraint>
    <login-config><auth-method>FORM</auth-method></login-config>
</web-app>
"""

PROJECT = {
    "com.acme.web.FrontServlet": ClassInfo(path="FrontServlet.java", http_servlet=True),
    "com.acme.web.ReportServlet": ClassInfo(path="ReportServlet.java", http_servlet=False),
    "com.acme.web.TaggedServlet": ClassInfo(path="TaggedServlet.java", annotated=True, http_servlet=True),
    "com.acme.web.AuditFilter": ClassInfo(path="AuditFilter.java"),
    "com.acme.web.TaggedFilter": ClassInfo(path="TaggedFilter.java", annotated=True),
    "com.acme.web.StartupListener": ClassInfo(path="StartupListener.java"),
}


def _plan(**kw):
    model = parse_web_xml(WEB_XML, "src/main/webapp/WEB-INF/web.xml")
    return model, plan_registrations(model, rules=RULES, lookup=PROJECT.get, component_scan=True, **kw)


def _java(model, plan) -> str:
    return render("WebXmlRegistrations.java.j2", package="com.acme", class_name="WebXmlRegistrations",
                  date="2026-10-23", web_xml_path=model.path, spec=model.spec, servlets=plan.servlets,
                  filters=plan.filters, listeners=plan.listeners, error_pages=plan.error_pages,
                  skipped=plan.skipped, notes=plan.notes)


def test_skip_table_loads() -> None:
    assert "org.springframework.web.servlet.DispatcherServlet" in RULES
    assert RULES["org.springframework.web.filter.CharacterEncodingFilter"].property
    assert load_skip_rules(REPO / "does-not-exist.yml") == {}


def test_plan_servlets_filters_listeners() -> None:
    model, plan = _plan()
    assert [s["bean"] for s in plan.servlets] == ["frontServlet", "reportServlet", "actionServlet"]
    front, report, action = plan.servlets
    assert front["type"] == "jakarta.servlet.http.HttpServlet" and front["load_on_startup"] == 1
    assert front["patterns"] == ['"/front/*"', '"*.do"'] and front["params"] == [('"mode"', '"fast"')]
    assert report["type"] == "jakarta.servlet.Servlet" and report["patterns"] == []   # not a direct HttpServlet
    assert action["cls"] == "org.apache.struts.action.ActionServlet"                    # library servlet: generated
    assert any("ActionServlet is not a project class" in n for n in plan.notes)
    assert any("servlet report has no servlet-mapping" in n for n in plan.notes)
    # skip table + @ServletComponentScan
    assert {s["cls"] for s in plan.skipped} == {"org.springframework.web.filter.CharacterEncodingFilter",
                                                "org.springframework.web.context.ContextLoaderListener",
                                                "org.springframework.web.servlet.DispatcherServlet"}
    assert plan.annotated == ["com.acme.web.TaggedServlet", "com.acme.web.TaggedFilter"]
    assert [f["bean"] for f in plan.filters] == ["auditFilter"]
    audit = plan.filters[0]
    assert audit["order"] == 2 and audit["dispatchers"] == ["jakarta.servlet.DispatcherType.REQUEST",
                                                            "jakarta.servlet.DispatcherType.FORWARD"]
    assert audit["params"] == [('"tag"', '"say \\"hi\\""')]
    assert [li["cls"] for li in plan.listeners] == ["com.acme.web.StartupListener"]
    assert plan.jsp_file_servlets == ["help → /WEB-INF/jsp/help.jsp"]
    fu = "\n".join(plan.followups)
    assert "jsp-file servlet help → /WEB-INF/jsp/help.jsp (/help): map it in a controller" in fu
    assert "DispatcherServlet dispatcher was mapped to *.htm" in fu and "spring.mvc.servlet.path" in fu
    assert "CharacterEncodingFilter) not registered in WebXmlRegistrations" in fu
    assert "server.servlet.encoding.charset" in fu                                   # property hint of the table
    assert "welcome-file-list (index.jsp)" in fu and "mime-mapping (hwp → application/x-hwp)" in fu
    assert "1 security-constraint(s) and login-config auth-method FORM" in fu
    assert "error-page 599" in fu                                                    # unknown status → follow-up
    assert plan.session_timeout == 30 and "only the embedded Tomcat" not in fu       # war=False


def test_plan_error_pages_and_war_session_note() -> None:
    _, plan = _plan(war=True, web_path=lambda p: p.replace("/WEB-INF/jsp/", "/views/"))
    assert plan.error_pages == [
        'new ErrorPage(org.springframework.http.HttpStatus.valueOf(404), "/views/error.jsp")',
        'new ErrorPage(jakarta.servlet.ServletException.class, "/e.jsp")',
        'new ErrorPage("/default.jsp")']
    assert plan.jsp_file_servlets == ["help → /views/help.jsp"]
    assert any("only the embedded Tomcat applies" in f for f in plan.followups)


def test_annotated_classes_registered_without_component_scan() -> None:
    model = parse_web_xml(WEB_XML, "web.xml")
    plan = plan_registrations(model, rules=RULES, lookup=PROJECT.get, component_scan=False)
    assert "taggedServlet" in [s["bean"] for s in plan.servlets] and plan.annotated == []


def test_unmapped_filter_and_bad_names() -> None:
    text = ("<web-app><filter><filter-name>lonely</filter-name><filter-class>a.Lonely</filter-class></filter>"
            "<servlet><servlet-name>bad</servlet-name><servlet-class>a.b-c.D</servlet-class></servlet>"
            "<servlet><servlet-name>1 odd name</servlet-name><servlet-class>a.Outer$Inner</servlet-class></servlet>"
            "</web-app>")
    plan = plan_registrations(parse_web_xml(text, "w"), rules={}, lookup=lambda _: None, component_scan=True)
    assert plan.filters == [] and any("lonely has no filter-mapping" in n for n in plan.notes)
    assert any("'a.b-c.D' is not a valid Java class name" in f for f in plan.followups)
    assert [(s["bean"], s["cls"]) for s in plan.servlets] == [("_1OddNameServlet", "a.Outer.Inner")]


def test_render_template() -> None:
    model, plan = _plan()
    text = _java(model, plan)
    assert "@Configuration(proxyBeanMethods = false)\npublic class WebXmlRegistrations {" in text
    assert ("new ServletRegistrationBean<>(new com.acme.web.FrontServlet(), \"/front/*\", \"*.do\");" in text)
    assert "ServletRegistrationBean<jakarta.servlet.Servlet> bean = new ServletRegistrationBean<>(" \
           "new com.acme.web.ReportServlet(), false);" in text
    assert "bean.setLoadOnStartup(1);" in text and 'bean.setName("front");' in text
    assert "bean.setDispatcherTypes(jakarta.servlet.DispatcherType.REQUEST, " \
           "jakarta.servlet.DispatcherType.FORWARD);" in text
    assert "bean.setOrder(2);" in text and 'bean.addUrlPatterns("/admin/*");' in text
    assert "new ServletListenerRegistrationBean<>(new com.acme.web.StartupListener())" in text
    assert "public ErrorPageRegistrar webXmlErrorPages()" in text
    assert "import org.springframework.boot.web.error.ErrorPage;" in text
    assert "TaggedServlet" not in text.split("*/", 1)[1]                                 # left to the scan
    assert all(ord(ch) < 128 for ch in text)                                             # source encoding safe
    assert text.count("{") == text.count("}")


def test_java_string_escapes() -> None:
    assert java_string('a"b\\c\n') == '"a\\"b\\\\c\\n"'
    assert java_string("한") == '"\\ud55c"'


# ---------------------------------------------------------------------------------------------- restructure
def _files() -> dict[str, str]:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>legacy</artifactId>\n"
           "    <version>1.0</version>\n    <packaging>war</packaging>\n"
           "    <dependencies>\n" + dep("javax.servlet", "servlet-api", "2.5", "provided")
           + "    </dependencies>\n</project>\n")
    servlet = ("package com.acme.web;\n\nimport javax.servlet.http.HttpServlet;\n\n"
               "public class FrontServlet extends HttpServlet {\n}\n")
    tagged = ("package com.acme.web;\n\nimport javax.servlet.annotation.WebServlet;\nimport javax.servlet.http.HttpServlet;\n\n"
              '@WebServlet("/t")\npublic class TaggedServlet extends HttpServlet {\n}\n')
    return {
        "pom.xml": pom,
        "src/main/java/com/acme/web/FrontServlet.java": servlet,
        "src/main/java/com/acme/web/TaggedServlet.java": tagged,
        "src/main/java/com/acme/service/Svc.java": "package com.acme.service;\n\npublic class Svc {\n}\n",
        "src/main/webapp/WEB-INF/web.xml": (
            "<web-app>\n<servlet><servlet-name>front</servlet-name><servlet-class>com.acme.web.FrontServlet"
            "</servlet-class><load-on-startup>1</load-on-startup></servlet>\n"
            "<servlet><servlet-name>tagged</servlet-name><servlet-class>com.acme.web.TaggedServlet</servlet-class>"
            "</servlet>\n<servlet-mapping><servlet-name>front</servlet-name><url-pattern>*.do</url-pattern>"
            "</servlet-mapping>\n<session-config><session-timeout>20</session-timeout></session-config>\n"
            "<error-page><error-code>500</error-code><location>/WEB-INF/jsp/error.jsp</location></error-page>\n"
            "</web-app>\n"),
        "src/main/webapp/WEB-INF/jsp/error.jsp": "<html/>\n",
        "src/main/webapp/css/a.css": "body{}\n",
    }


def _profile(ws):
    return profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="legacy", version="1.0", packaging="war",
                   base_package="com.acme", servlet_classes=["com.acme.web.FrontServlet", "com.acme.web.TaggedServlet"],
                   has_web_xml=True, web_xml_paths=["src/main/webapp/WEB-INF/web.xml"], jsp_count=1,
                   jsp_paths=["src/main/webapp/WEB-INF/jsp/error.jsp"], web_static_dirs=["src/main/webapp"],
                   web_static_count=1, web_xml_spec=None, web_xml_servlets=2)


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    return cfg


def test_restructure_generates_registrations(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _files(), "webxml")
    res = Restructurer(commit=False).apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    rel = "legacy-boot/src/main/java/com/acme/WebXmlRegistrations.java"
    assert rel in res.generated_files and res.flags == ["web-xml-registered"]
    text = (ws.root / rel).read_text()
    assert "new ServletRegistrationBean<>(new com.acme.web.FrontServlet(), \"*.do\");" in text
    assert "ServletRegistrationBean<jakarta.servlet.http.HttpServlet>" in text
    assert "TaggedServlet" not in text.split("*/", 1)[1]                                 # @WebServlet → the scan
    assert 'HttpStatus.valueOf(500), "/WEB-INF/jsp/error.jsp")' in text
    props = (ws.root / "legacy-boot/src/main/resources/application.properties").read_text()
    assert "server.servlet.session.timeout=20m" in props
    fu = res.manual_followups
    assert any(f.startswith("web.xml at legacy-boot/src/main/resources/asis-webinf/web.xml is kept for reference")
               and "registered by the generated com.acme.WebXmlRegistrations" in f for f in fu)
    assert "web.xml (spec unknown) → WebXmlRegistrations: 1 servlet(s), 0 filter(s), 0 listener(s), " \
           "1 error page(s)" in fu
    assert not any("register servlets/filters as beans" in f or "via @ServletComponentScan" in f for f in fu)
    assert any("only the embedded Tomcat applies" in f for f in fu)                      # WAR boot module
    events = [json.loads(x) for x in ws.journal_path.read_text().splitlines()]
    ev = next(e for e in events if e["event"] == "web_xml_registrations")
    assert (ev["servlets"], ev["filters"], ev["listeners"], ev["error_pages"]) == (1, 0, 0, 1)
    assert ev["class"] == "com.acme.WebXmlRegistrations" and ev["path"] == "src/main/webapp/WEB-INF/web.xml"


def test_followup_policy_generates_nothing(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.web_xml_registrations = "followup"
    ws = make_ws(tmp_path, _files(), "webxml-fu")
    res = Restructurer(commit=False).apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert not list(ws.root.rglob("WebXmlRegistrations.java")) and res.flags == []
    props = ws.root / "legacy-boot/src/main/resources/application.properties"
    assert not props.exists() or "session.timeout" not in props.read_text()
    assert any("servlets/filters are registered via @ServletComponentScan" in f for f in res.manual_followups)


def test_only_skipped_declarations_generate_nothing(tmp_path: Path, ccfg: Config) -> None:
    files = _files()
    files["src/main/webapp/WEB-INF/web.xml"] = (
        "<web-app><listener><listener-class>org.springframework.web.context.ContextLoaderListener</listener-class>"
        "</listener><servlet><servlet-name>d</servlet-name><servlet-class>"
        "org.springframework.web.servlet.DispatcherServlet</servlet-class></servlet>"
        "<servlet-mapping><servlet-name>d</servlet-name><url-pattern>/</url-pattern></servlet-mapping></web-app>")
    ws = make_ws(tmp_path, files, "webxml-skip")
    res = Restructurer(commit=False).apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert not list(ws.root.rglob("WebXmlRegistrations.java")) and res.flags == []
    assert any("DispatcherServlet) not registered" in f for f in res.manual_followups)      # has a property hint
    assert not any("ContextLoaderListener) not registered" in f for f in res.manual_followups)
    assert any("ContextLoaderListener) not registered" in n for n in res.notes)           # Boot provides it: a note
    assert not any("was mapped to" in f for f in res.manual_followups)                 # mapped to / already


def test_skip_rule_dataclass_defaults() -> None:
    r = SkipRule(cls="a.B", reason="x")
    assert r.property is None


def test_welcome_index_html_alone_needs_no_followup() -> None:
    text = "<web-app><welcome-file-list><welcome-file>index.html</welcome-file></welcome-file-list></web-app>"
    plan = plan_registrations(parse_web_xml(text, "w"), rules={}, lookup=lambda _: None, component_scan=True)
    assert plan.followups == []
