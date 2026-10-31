"""v2.1x (2026-10-31, docs/INTERFACES.md "v2.1x"): the legacy Spring context at conversion time — entry XML only (D2),
byte copies (D3), bean overriding / circular references (D1), web.xml filter properties and `requires:` (D4 / D11), JNDI
(D5), WEB-INF / `${…}` locations (D6 / D7), default servlet (D8), CommonsMultipartResolver (D9), @EnableWebMvc / view
prefix / <mvc:resources> (D10), Handover sentences (D12), as-is provided scopes (E-M6), the starter by web evidence
(E-M9) and the source encoding (D-M7). Every switch off = the Handover sentence only."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze.analyzer import analyze_project
from sbmig.core.config import Config
from sbmig.core.models import WebXmlModel, WebXmlServlet
from sbmig.restructure import Restructurer
from sbmig.restructure import legacy_context as lc
from sbmig.restructure.webxml_registrations import (
    RegistrationFacts,
    data_size,
    dispatcher_mapping_followup,
    load_skip_rules,
    plan_registrations,
)

from .helpers import HEAD, all_, dep, make_ws, t, xml

REPO = Path(__file__).resolve().parents[2]
RULES = lc.load_rules(REPO / lc.RULES_FILE)
WEB_RULES = load_skip_rules(REPO / "knowledge" / "config" / "web_xml_rules.yml")

BEANS = ('<?xml version="1.0" encoding="UTF-8"?>\n<beans xmlns="http://www.springframework.org/schema/beans"\n'
         '       xmlns:context="http://www.springframework.org/schema/context"\n'
         '       xmlns:mvc="http://www.springframework.org/schema/mvc"\n'
         '       xmlns:jee="http://www.springframework.org/schema/jee">\n{body}</beans>\n')


def beans(body: str, header: str = "") -> str:
    out = BEANS.format(body=body)
    if header:
        out = out.replace('<?xml version="1.0" encoding="UTF-8"?>', header, 1)
    return out


def pom(deps: str = "", packaging: str = "war", extra: str = "") -> str:
    return (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0</version>\n"
            f"    <packaging>{packaging}</packaging>\n{extra}    <dependencies>\n"
            + dep("org.springframework", "spring-webmvc", "5.3.31") + deps + "    </dependencies>\n</project>\n")


WEB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<web-app xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="3.1">
    <context-param><param-name>contextConfigLocation</param-name>
        <param-value>classpath*:spring/context-*.xml</param-value></context-param>
    <listener><listener-class>org.springframework.web.context.ContextLoaderListener</listener-class></listener>
{filters}
    <servlet><servlet-name>action</servlet-name>
        <servlet-class>org.springframework.web.servlet.DispatcherServlet</servlet-class>
        <init-param><param-name>contextConfigLocation</param-name>
            <param-value>/WEB-INF/config/action-servlet.xml</param-value></init-param>
    </servlet>
    <servlet-mapping><servlet-name>action</servlet-name><url-pattern>*.do</url-pattern></servlet-mapping>
{extra}
</web-app>
"""


def files(**over: str) -> dict[str, str]:
    out = {
        "pom.xml": pom(dep("javax.servlet", "javax.servlet-api", "4.0.1", "provided")),
        "src/main/java/com/acme/shop/web/HomeController.java": (
            "package com.acme.shop.web;\n\nimport org.springframework.stereotype.Controller;\n\n"
            "@Controller\npublic class HomeController {\n}\n"),
        "src/main/java/com/acme/shop/service/ShopService.java": (
            "package com.acme.shop.service;\n\nimport org.springframework.beans.factory.annotation.Value;\n\n"
            "public class ShopService {\n    @Value(\"${app.title}\")\n    private String title;\n}\n"),
        "src/main/webapp/WEB-INF/web.xml": WEB_XML.format(filters="", extra=""),
        "src/main/resources/spring/context-root.xml": beans(
            '    <context:property-placeholder location="/WEB-INF/config/app.properties"/>\n'
            '    <import resource="include/context-ds.xml"/>\n'
            '    <bean id="messageSource" class="org.springframework.context.support.ResourceBundleMessageSource"/>\n'),
        "src/main/resources/spring/include/context-ds.xml": beans(
            '    <jee:jndi-lookup id="dataSource" jndi-name="jdbc/shopDS"/>\n'),
        "src/main/resources/spring/other-tool.xml": beans('    <bean id="tool" class="java.lang.Object"/>\n'),
        "src/test/resources/spring/context-test.xml": beans('    <bean id="t" class="java.lang.Object"/>\n'),
        "src/main/webapp/WEB-INF/config/action-servlet.xml": beans(
            '    <mvc:default-servlet-handler/>\n'
            '    <mvc:resources mapping="/resources/**" location="/resources/"/>\n'
            '    <bean id="messageSource" class="org.springframework.context.support.ResourceBundleMessageSource"/>\n'
            '    <bean id="multipartResolver" class="org.springframework.web.multipart.commons.CommonsMultipartResolver">\n'
            '        <property name="maxUploadSize" value="5242880"/>\n'
            '        <property name="maxInMemorySize" value="10240"/>\n'
            '    </bean>\n'
            '    <bean class="org.springframework.web.servlet.view.InternalResourceViewResolver">\n'
            '        <property name="prefix" value="/WEB-INF/views/"/>\n'
            '        <property name="suffix" value=".jsp"/>\n'
            '    </bean>\n'),
        "src/main/webapp/WEB-INF/config/app.properties": "app.title=Shop\n",
        "src/main/webapp/WEB-INF/views/home.jsp": "<p>home</p>\n",
        "src/main/webapp/resources/css/site.css": "body{}\n",
    }
    out.update(over)
    return {k: v for k, v in out.items() if v is not None}


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    return cfg


def run(tmp_path: Path, cfg: Config, name: str, tree: dict[str, str], mode: str = "layout"):
    ws = make_ws(tmp_path, tree, name)
    prof = analyze_project(ws, cfg)
    res = Restructurer(commit=False).apply(ws, prof, cfg, mode=mode)
    assert res.ok, res.error
    return ws, res


def props(ws) -> str:
    p = ws.root / "shop-boot/src/main/resources/application.properties"
    return p.read_text() if p.exists() else ""


def fu(res) -> str:
    return "\n".join(res.manual_followups)


# ---------------------------------------------------------------------------------------------- pure helpers
def test_resolve_location_forms() -> None:
    cands = ["src/main/resources/spring/context-a.xml", "src/main/resources/spring/x/context-b.xml",
             "src/main/webapp/WEB-INF/config/action-servlet.xml", "src/main/webapp/WEB-INF/a.xml"]
    assert lc.resolve_location("classpath*:spring/context-*.xml", cands)[0] == ["src/main/resources/spring/context-a.xml"]
    assert lc.resolve_location("classpath:spring/**/context-*.xml", cands)[0] == cands[:2]
    assert lc.resolve_location("/WEB-INF/config/*-servlet.xml", cands, ["src/main/webapp"])[0] == [cands[2]]
    assert lc.resolve_location("WEB-INF/a.xml", cands)[0] == [cands[3]]
    assert lc.resolve_location("classpath:${env}/a.xml", cands) == ([], "placeholder in the location")
    assert lc.resolve_location("file:/etc/a.xml", cands)[1] == "not a project file"
    assert lc.split_locations("a.xml, b.xml;c.xml\n d.xml") == ["a.xml", "b.xml", "c.xml", "d.xml"]
    assert lc.is_test_path("src/test/resources/x.xml") and not lc.is_test_path("src/main/resources/x.xml")


def test_entry_locations_defaults_and_annotation_contexts() -> None:
    m = WebXmlModel(path="w", listeners=["org.springframework.web.context.ContextLoaderListener"],
                    servlets=[WebXmlServlet(name="app", servlet_class="org.springframework.web.servlet.DispatcherServlet"),
                              WebXmlServlet(name="api", servlet_class="org.springframework.web.servlet.DispatcherServlet",
                                            init_params={"contextClass": "org.springframework.web.context.support."
                                                                         "AnnotationConfigWebApplicationContext",
                                                         "contextConfigLocation": "com.acme.ApiConfig"})])
    assert lc.entry_locations(m, RULES) == [("root", ["/WEB-INF/applicationContext.xml"]),
                                            ("servlet app", ["/WEB-INF/app-servlet.xml"])]
    m.context_params = {"contextConfigLocation": "classpath:a.xml classpath:b.xml"}
    assert lc.entry_locations(m, RULES)[0] == ("root", ["classpath:a.xml", "classpath:b.xml"])


def test_xml_helpers_edit_copies_only_where_meant() -> None:
    text = beans('    <!-- <bean id="gone" class="a.B"/> -->\n'
                 '    <context:property-placeholder location="/WEB-INF/c/a.properties, classpath:p-${env}.properties"/>\n'
                 '    <jee:jndi-lookup id="ds" jndi-name="jdbc/x"/>\n'
                 '    <bean id="mr" class="org.springframework.web.multipart.commons.CommonsMultipartResolver"\n'
                 '          p:maxUploadSize="1048576"><property name="maxInMemorySize"><value>2048</value></property></bean>\n'
                 '    <bean class="org.springframework.web.servlet.view.InternalResourceViewResolver" p:prefix="/WEB-INF/v/"/>\n'
                 '    <import resource="sub/x.xml"/>\n')
    assert lc.bean_ids(text) == ["ds", "mr"] and lc.import_resources(text) == ["sub/x.xml"]
    el = lc.elements(text, RULES.jndi_elements)[0]
    once, changed = lc.insert_comment_above(text, el, RULES.jndi_todo)
    assert changed and once.count(lc.TODO_MARK) == 1
    again, changed2 = lc.insert_comment_above(once, lc.elements(once, RULES.jndi_elements)[0], RULES.jndi_todo)
    assert not changed2 and again == once                                                        # idempotent
    new, done = lc.rewrite_webinf_locations(text, RULES, lambda sub: f"classpath:asis-webinf/{sub}")
    assert done == [("/WEB-INF/c/a.properties", "classpath:asis-webinf/c/a.properties")]
    assert 'location="classpath:asis-webinf/c/a.properties, classpath:p-${env}.properties"' in new
    assert lc.profile_locations(text, RULES) == ["classpath:p-${env}.properties"]
    assert lc.glob_of("classpath:p-${env}.properties") == "classpath*:p-*.properties"
    mr = lc.beans_of_class(text, RULES.multipart_classes)[0]
    assert lc.multipart_limits(text, mr, RULES) == [
        ("spring.servlet.multipart.max-request-size", "1MB", "maxUploadSize"),
        ("spring.servlet.multipart.max-file-size", "1MB", "maxUploadSize"),
        ("spring.servlet.multipart.file-size-threshold", "2KB", "maxInMemorySize")]
    assert [v for _a, _b, v in lc.view_prefixes(text, RULES)] == ["/WEB-INF/v/"]
    assert data_size(10485760) == "10MB" and data_size(512000) == "500KB" and data_size(100) == "100B"
    assert data_size(-1) == "-1" and data_size("${max}") == "${max}" and data_size(None) is None


def test_dispatcher_mapping_followup_suffix_and_prefix() -> None:
    s = dispatcher_mapping_followup("action", ["*.do"])
    assert "request mappings that end in .do keep working" in s and "path prefix only" in s
    p = dispatcher_mapping_followup("app", ["/app/*"])
    assert "spring.mvc.servlet.path=/app" in p


# ---------------------------------------------------------------------------------------------- D2 / D3 / D1
def test_entry_xml_only_imports_and_skips(tmp_path: Path, ccfg: Config) -> None:
    ws, res = run(tmp_path, ccfg, "d2", files())
    app = next((ws.root / "shop-boot/src/main/java").rglob("Application.java")).read_text()
    assert res.xml_imports == ["classpath:legacy-config/context-root.xml", "classpath:legacy-config/action-servlet.xml"]
    for x in res.xml_imports:
        assert f'"{x}"' in app
    skipped = "\n".join(res.xml_skipped)
    assert "src/test/resources/spring/context-test.xml (test)" in skipped
    assert "src/main/resources/spring/include/context-ds.xml (reached by <import> from context-root.xml)" in skipped
    assert "src/main/resources/spring/other-tool.xml (not in contextConfigLocation)" in skipped
    lcfg = ws.root / "shop-boot/src/main/resources/legacy-config"
    assert (lcfg / "include/context-ds.xml").is_file()                    # reached by <import>: copied, relative path
    assert not (lcfg / "context-test.xml").exists() and not (lcfg / "other-tool.xml").exists()
    assert "context-ds.xml" not in app and "context-test.xml" not in app
    # D1: the as-is Spring Framework behaviour + the duplicate ids in one sentence
    p = props(ws)
    assert "spring.main.allow-bean-definition-overriding=true\n" in p and "spring.main.allow-circular-references=true\n" in p
    assert res.duplicate_bean_ids == ["messageSource: legacy-config/context-root.xml, legacy-config/action-servlet.xml"]
    assert any(f.startswith("Spring Boot refuses duplicate bean names") and "messageSource" in f
               for f in res.manual_followups)
    assert any(x.startswith("spring.main.allow-bean-definition-overriding=true (") for x in res.legacy_properties_written)
    assert "the root (ContextLoaderListener) and DispatcherServlet contexts are one Spring Boot context" in fu(res)


def test_no_web_xml_imports_every_xml_but_tests(tmp_path: Path, ccfg: Config) -> None:
    tree = files(**{"src/main/webapp/WEB-INF/web.xml": None})
    ws, res = run(tmp_path, ccfg, "d2-noweb", tree)
    names = sorted(x.rsplit("/", 1)[-1] for x in res.xml_imports)
    assert "context-test.xml" not in names and "other-tool.xml" in names and "context-root.xml" in names
    assert any(x.endswith("(test)") for x in res.xml_skipped)


def test_switches_off_keep_the_old_behaviour(tmp_path: Path, ccfg: Config) -> None:
    LC = ccfg.layout.legacy_context
    LC.entry_xml_only = LC.allow_bean_overriding = LC.allow_circular_references = False
    LC.exclude_test_xml = LC.multipart_resolver = LC.default_servlet = LC.rewrite_webinf_locations = False
    LC.filter_properties = LC.handover_sentences = LC.restore_asis_scopes = False
    ws, res = run(tmp_path, ccfg, "off", files())
    assert any(x.endswith("context-test.xml") for x in res.xml_imports)               # tests imported as before
    p = props(ws)
    assert "spring.main.allow" not in p and "register-default-servlet" not in p and "multipart" not in p
    lcfg = ws.root / "shop-boot/src/main/resources/legacy-config"
    assert "CommonsMultipartResolver" in (lcfg / "action-servlet.xml").read_text()          # D9 off: bean kept
    assert lc.TODO_MARK not in (lcfg / "include/context-ds.xml").read_text()                 # D5 off: no comment
    assert 'location="/WEB-INF/config/app.properties"' in (lcfg / "context-root.xml").read_text()   # D6 off
    assert res.legacy_properties_written == [] and res.webinf_locations_rewritten == []
    f = fu(res)
    assert "Spring 6 deleted the class, startup fails" in f                          # sentence only
    assert "needs Tomcat's default servlet" in f and "every XML bean config is imported" in f
    assert "duplicate bean ids across the imported XML" in f
    assert "property locations under /WEB-INF are not on the Spring Boot classpath" in f
    assert "Spring 6 deleted the class, startup fails" in f
    assert res.scope_restored == [] and "Spring Boot refuses duplicate bean names" in f
    assert "XML under src/test" in f


def test_euc_kr_xml_is_copied_byte_for_byte(tmp_path: Path, ccfg: Config) -> None:
    head = '<?xml version="1.0" encoding="EUC-KR"?>'
    root_xml = beans('    <!-- 루트 컨텍스트 -->\n    <bean id="greeting" class="java.lang.String">\n'
                     '        <constructor-arg value="안녕하세요"/>\n    </bean>\n', header=head)
    ws = make_ws(tmp_path, {**files(), "src/main/resources/spring/context-root.xml": ""}, "euckr")
    (ws.root / "src/main/resources/spring/context-root.xml").write_bytes(root_xml.encode("euc-kr"))
    res = Restructurer(commit=False).apply(ws, analyze_project(ws, ccfg), ccfg, mode="layout")
    assert res.ok, res.error
    copy = (ws.root / "shop-boot/src/main/resources/legacy-config/context-root.xml").read_bytes()
    assert copy == root_xml.encode("euc-kr")                                     # D3: the declaration stays true
    assert "안녕하세요" in copy.decode("euc-kr")
    original = (ws.root / "shop-back/src/main/resources/spring/context-root.xml").read_bytes()
    assert original == root_xml.encode("euc-kr")                                 # as-is file untouched


# ---------------------------------------------------------------------------------------------- D5 / D6 / D7 / D9 / D10
def test_copies_get_jndi_todo_webinf_location_multipart_and_prefix(tmp_path: Path, ccfg: Config) -> None:
    ws, res = run(tmp_path, ccfg, "copies", files())
    lcfg = ws.root / "shop-boot/src/main/resources/legacy-config"
    ds = (lcfg / "include/context-ds.xml").read_text()
    assert f"<!-- {RULES.jndi_todo} -->\n    <jee:jndi-lookup id=\"dataSource\"" in ds           # D5: bean stays
    assert "JNDI DataSource dataSource (legacy-config/include/context-ds.xml) has no JNDI" in fu(res)
    assert "spring-boot-starter-jdbc present: no" in fu(res)
    root_copy = (lcfg / "context-root.xml").read_text()
    assert 'location="classpath:asis-webinf/config/app.properties"' in root_copy                  # D6
    assert (ws.root / "shop-boot/src/main/resources/asis-webinf/config/app.properties").is_file()
    assert res.webinf_locations_rewritten == [
        "legacy-config/context-root.xml: /WEB-INF/config/app.properties → classpath:asis-webinf/config/app.properties"]
    srv = (lcfg / "action-servlet.xml").read_text()
    assert "CommonsMultipartResolver\"" not in srv and "multipartResolver removed" in srv          # D9: copy only
    assert res.beans_removed == ["legacy-config/action-servlet.xml: multipartResolver (CommonsMultipartResolver → "
                                 "spring.servlet.multipart.*)"]
    p = props(ws)
    assert "spring.servlet.multipart.max-file-size=5MB\n" in p and "spring.servlet.multipart.file-size-threshold=10KB\n" in p
    assert '<property name="prefix" value="/WEB-INF/jsp/views/"/>' in srv                       # D10: like the props
    assert "spring.mvc.view.prefix=/WEB-INF/jsp/views/\n" in p
    assert 'location="/resources/, classpath:/static/resources/"' in srv
    assert '<mvc:resources mapping="/resources/**" location="/resources/"> (legacy-config/action-servlet.xml)' in fu(res)
    assert "server.servlet.register-default-servlet=true\n" in p                                  # D8
    assert "view prefix /WEB-INF/views/" not in fu(res)                     # rewritten in the copy: no follow-up
    # the as-is XML (comp module) is never edited
    asis = (ws.root / "shop-back/src/main/resources/spring/include/context-ds.xml").read_text()
    assert lc.TODO_MARK not in asis
    facts = res.conversion_facts
    assert facts["jndi_beans"] == ["dataSource (legacy-config/include/context-ds.xml)"]
    assert facts["view_prefix_rewritten"] == ["legacy-config/action-servlet.xml: /WEB-INF/views/ → /WEB-INF/jsp/views/"]


def test_profile_location_one_file_glob_several_left(tmp_path: Path, ccfg: Config) -> None:
    root_xml = beans('    <context:property-placeholder location="classpath:config-${env}.properties"/>\n')
    one = files(**{"src/main/resources/spring/context-root.xml": root_xml,
                   "src/main/resources/config-dev.properties": "a=1\n"})
    ws, res = run(tmp_path, ccfg, "d7-one", one)
    copy = (ws.root / "shop-boot/src/main/resources/legacy-config/context-root.xml").read_text()
    assert 'location="classpath*:config-*.properties"' in copy
    assert "chose its file by ${env}" in fu(res) and "env=__SET_ME__" not in props(ws)
    several = {**one, "src/main/resources/config-prod.properties": "a=2\n"}
    ws2, res2 = run(tmp_path / "2", ccfg, "d7-two", several)
    copy2 = (ws2.root / "shop-boot/src/main/resources/legacy-config/context-root.xml").read_text()
    assert 'location="classpath:config-${env}.properties"' in copy2                    # a glob would mix them
    assert "set env (-D" in fu(res2) and "2 packaged files match" in fu(res2)
    with_default = beans('    <context:property-placeholder location="classpath:config-${env:dev}.properties"/>\n')
    ws3, res3 = run(tmp_path / "3", ccfg, "d7-default",
                    {**several, "src/main/resources/spring/context-root.xml": with_default})
    assert "chooses its file" not in fu(res3) and "chose its file" not in fu(res3)       # resolvable as before


def test_enable_web_mvc_writes_no_view_props(tmp_path: Path, ccfg: Config) -> None:
    mvc = ("package com.acme.shop.web;\n\nimport org.springframework.web.servlet.config.annotation.EnableWebMvc;\n\n"
           "@EnableWebMvc\npublic class MvcConfig {\n}\n")
    ws, res = run(tmp_path, ccfg, "d10", files(**{"src/main/java/com/acme/shop/web/MvcConfig.java": mvc}))
    assert "spring.mvc.view.prefix" not in props(ws)
    assert res.conversion_facts["enable_web_mvc"] == ["com.acme.shop.web.MvcConfig"]
    assert "com.acme.shop.web.MvcConfig carries @EnableWebMvc" in fu(res)


# ---------------------------------------------------------------------------------------------- D4 / D11 / D12
FILTERS = """    <filter><filter-name>enc</filter-name>
        <filter-class>org.springframework.web.filter.CharacterEncodingFilter</filter-class>
        <init-param><param-name>encoding</param-name><param-value>EUC-KR</param-value></init-param>
        <init-param><param-name>forceEncoding</param-name><param-value>true</param-value></init-param></filter>
    <filter><filter-name>xssFilter</filter-name>
        <filter-class>org.springframework.web.filter.DelegatingFilterProxy</filter-class>
        <init-param><param-name>targetBeanName</param-name><param-value>xssEscape</param-value></init-param></filter>
    <filter><filter-name>hidden</filter-name>
        <filter-class>org.springframework.web.filter.HiddenHttpMethodFilter</filter-class></filter>
    <filter><filter-name>oemiv</filter-name>
        <filter-class>org.springframework.orm.jpa.support.OpenEntityManagerInViewFilter</filter-class></filter>
    <filter-mapping><filter-name>enc</filter-name><url-pattern>/*</url-pattern></filter-mapping>
    <filter-mapping><filter-name>xssFilter</filter-name><url-pattern>*.do</url-pattern></filter-mapping>
    <filter-mapping><filter-name>hidden</filter-name><url-pattern>/*</url-pattern></filter-mapping>
    <filter-mapping><filter-name>oemiv</filter-name><url-pattern>/*</url-pattern></filter-mapping>"""
EXTRA = """    <context-param><param-name>spring.profiles.active</param-name><param-value>dev</param-value></context-param>
    <jsp-config><jsp-property-group><url-pattern>*.jsp</url-pattern><page-encoding>EUC-KR</page-encoding>
        <include-prelude>/WEB-INF/views/taglibs.jspf</include-prelude></jsp-property-group>
        <taglib><taglib-uri>/my</taglib-uri><taglib-location>/WEB-INF/my.tld</taglib-location></taglib></jsp-config>"""


def test_web_xml_filters_properties_proxy_and_sentences(tmp_path: Path, ccfg: Config) -> None:
    ws, res = run(tmp_path, ccfg, "d4", files(**{"src/main/webapp/WEB-INF/web.xml": WEB_XML.format(filters=FILTERS,
                                                                                               extra=EXTRA)}))
    p = props(ws)
    assert "spring.servlet.encoding.charset=EUC-KR\n" in p and "spring.servlet.encoding.force=true\n" in p
    assert "server.servlet.encoding" not in p and "spring.mvc.hiddenmethod.filter.enabled=true\n" in p
    reg = next((ws.root / "shop-boot/src/main/java").rglob("WebXmlRegistrations.java")).read_text()
    assert 'new DelegatingFilterProxyRegistrationBean("xssEscape");' in reg                     # D11: not security
    assert 'bean.addUrlPatterns("*.do");' in reg and "targetBeanName" not in reg.split("*/", 1)[1]
    assert "import org.springframework.boot.web.servlet.DelegatingFilterProxyRegistrationBean;" in reg
    assert "OpenEntityManagerInViewFilter()" not in reg                                         # no JPA: not registered
    f = fu(res)
    assert "web.xml filter oemiv (org.springframework.orm.jpa.support.OpenEntityManagerInViewFilter) not registered: " \
           "the project uses no JPA" in f
    assert "web.xml DispatcherServlet action was mapped to *.do" in f
    assert "web.xml servlet action (org.springframework.web.servlet.DispatcherServlet) not registered" not in f
    assert "web.xml <jsp-config> (1 jsp-property-group(s): page-encoding EUC-KR; include-prelude " \
           "/WEB-INF/views/taglibs.jspf; 1 taglib alias(es)) is not applied" in f
    assert "web.xml context-param spring.profiles.active=dev is written to" in f
    assert "registered via @ServletComponentScan" not in f
    assert res.conversion_facts["delegating_filter_proxies"] == ["xssFilter → xssEscape"]
    assert res.conversion_facts["dispatcher_mappings"] == ["action: *.do"]


def test_plan_requires_security_chain_and_jpa() -> None:
    from sbmig.analyze.webxml import parse_web_xml

    text = ("<web-app><filter><filter-name>springSecurityFilterChain</filter-name><filter-class>"
            "org.springframework.web.filter.DelegatingFilterProxy</filter-class></filter>"
            "<filter><filter-name>oemiv</filter-name><filter-class>"
            "org.springframework.orm.jpa.support.OpenEntityManagerInViewFilter</filter-class></filter>"
            "<filter-mapping><filter-name>springSecurityFilterChain</filter-name><url-pattern>/*</url-pattern>"
            "</filter-mapping><filter-mapping><filter-name>oemiv</filter-name><url-pattern>/*</url-pattern>"
            "</filter-mapping></web-app>")
    model = parse_web_xml(text, "w")
    sec = RegistrationFacts(libraries={"spring-security", "hibernate"})
    plan = plan_registrations(model, rules=WEB_RULES, lookup=lambda _: None, component_scan=False, facts=sec)
    assert {s["name"] for s in plan.skipped} == {"springSecurityFilterChain"}                   # the security chain
    assert [f["cls"] for f in plan.filters] == ["org.springframework.orm.jpa.support.OpenEntityManagerInViewFilter"]
    none = plan_registrations(model, rules=WEB_RULES, lookup=lambda _: None, component_scan=False,
                              facts=RegistrationFacts())
    assert [f["proxy"] for f in none.filters] == ['"springSecurityFilterChain"']               # no spring-security
    boot_jpa = RegistrationFacts(libraries={"spring-data-jpa"},
                                 artifacts={"org.springframework.boot:spring-boot-starter-data-jpa"})
    skipped = plan_registrations(model, rules=WEB_RULES, lookup=lambda _: None, component_scan=False, facts=boot_jpa)
    assert "oemiv" in {s["name"] for s in skipped.skipped}                                      # Boot's interceptor


def test_multipart_config_of_the_replaced_dispatcher() -> None:
    from sbmig.analyze.webxml import parse_web_xml

    text = ("<web-app><servlet><servlet-name>d</servlet-name><servlet-class>"
            "org.springframework.web.servlet.DispatcherServlet</servlet-class><multipart-config>"
            "<max-file-size>2097152</max-file-size><max-request-size>4194304</max-request-size>"
            "<file-size-threshold>0</file-size-threshold></multipart-config></servlet>"
            "<servlet-mapping><servlet-name>d</servlet-name><url-pattern>/</url-pattern></servlet-mapping></web-app>")
    plan = plan_registrations(parse_web_xml(text, "w"), rules=WEB_RULES, lookup=lambda _: None, component_scan=False,
                              facts=RegistrationFacts(multipart_keys=dict(RULES.multipart_servlet_config)))
    assert [(k, v) for k, v, _ in plan.properties] == [("spring.servlet.multipart.max-file-size", "2MB"),
                                                       ("spring.servlet.multipart.max-request-size", "4MB"),
                                                       ("spring.servlet.multipart.file-size-threshold", "0B")]


def test_project_sentences_initializer_log4j_context_root(tmp_path: Path, ccfg: Config) -> None:
    init = ("package com.acme.shop.web;\n\nimport org.springframework.web.WebApplicationInitializer;\n\n"
            "public class Boot implements WebApplicationInitializer {\n}\n")
    mpj = ("package com.acme.shop.web;\n\npublic class Up {\n    Object r() { return new org.springframework.web."
           "multipart.commons.CommonsMultipartResolver(); }\n}\n")
    sec = beans('    <beans:http xmlns:beans="x"/>\n').replace(
        'xmlns:jee="http://www.springframework.org/schema/jee"',
        'xmlns:jee="http://www.springframework.org/schema/jee" xmlns:sec="http://www.springframework.org/schema/security"')
    sec = sec.replace('<beans:http xmlns:beans="x"/>', "<sec:http/>")
    ws, res = run(tmp_path, ccfg, "d12", files(**{"src/main/java/com/acme/shop/web/Boot.java": init,
                                                  "src/main/java/com/acme/shop/web/Up.java": mpj,
                                                  "src/main/resources/log4j.properties": "log4j.rootLogger=INFO\n",
                                                  "src/main/resources/spring/context-security.xml": sec}))
    f = fu(res)
    assert "com.acme.shop.web.Boot is a WebApplicationInitializer: java -jar never calls it" in f
    assert "log4j 1 configuration shop-back/src/main/resources/log4j.properties is not read" in f
    assert "the as-is WAR shop-1.0.war was served under the context path /shop-1.0" in f
    assert "com.acme.shop.web.Up creates a CommonsMultipartResolver" in f
    assert "legacy-config/context-security.xml configure Spring Security in XML" in f


def test_view_prefix_followup_only_with_mvc(tmp_path: Path, ccfg: Config) -> None:
    servlet_app = {
        "pom.xml": (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0</version>\n"
                    "    <packaging>war</packaging>\n    <dependencies>\n"
                    + dep("javax.servlet", "javax.servlet-api", "4.0.1", "provided") + "    </dependencies>\n</project>\n"),
        "src/main/java/com/acme/shop/Hello.java": ("package com.acme.shop;\n\nimport javax.servlet.http.HttpServlet;\n\n"
                                                   "public class Hello extends HttpServlet {\n}\n"),
        "src/main/webapp/WEB-INF/web.xml": "<web-app/>\n",
        "src/main/webapp/WEB-INF/jsp/a.jsp": "<p/>\n",
    }
    ws, res = run(tmp_path, ccfg, "noise", servlet_app)
    assert "no InternalResourceViewResolver prefix" not in fu(res)                        # 0 controllers, no webmvc
    assert res.starter == "spring-boot-starter-webmvc" and "servlet-api" in res.web_evidence


# ---------------------------------------------------------------------------------------------- E-M6 / E-M9 / D-M7
def test_asis_provided_scope_restored(tmp_path: Path, ccfg: Config) -> None:
    tree = files()
    ws = make_ws(tmp_path, tree, "scope")
    prof = analyze_project(ws, ccfg)
    # OpenRewrite already ran: javax.servlet-api provided became jakarta.servlet-api compile
    (ws.root / "pom.xml").write_text(pom(dep("jakarta.servlet", "jakarta.servlet-api", "6.0.0", "compile")))
    res = Restructurer(commit=False).apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    back = xml(ws.root / "shop-back/pom.xml")
    scopes = {t(d, "artifactId"): t(d, "scope") for d in all_(back, "dependencies/dependency")}
    assert scopes["jakarta.servlet-api"] == "provided"
    assert res.scope_restored == ["jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, shop-back/pom.xml)"]


def test_no_web_evidence_plain_starter(tmp_path: Path, ccfg: Config) -> None:
    cli = {"pom.xml": (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n"
                       "    <version>1.0</version>\n    <dependencies>\n" + dep("org.springframework", "spring-context",
                                                                                  "5.3.31")
                       + "    </dependencies>\n</project>\n"),
           "src/main/java/com/acme/shop/Main.java": ("package com.acme.shop;\n\npublic class Main {\n    public static void "
                                                     "main(String[] a) {\n    }\n}\n"),
           "src/main/resources/applicationContext.xml": beans('    <bean id="x" class="java.lang.Object"/>\n')}
    ws, res = run(tmp_path, ccfg, "cli", cli)
    assert res.web_evidence == [] and res.starter == "spring-boot-starter"
    assert "org.springframework.boot:spring-boot-starter-webmvc" not in (ws.root / "shop-boot/pom.xml").read_text()
    assert "no web evidence: shop-boot gets spring-boot-starter" in fu(res)
    ccfg.layout.legacy_context.web_starter_by_evidence = False
    ws2, res2 = run(tmp_path / "2", ccfg, "cli2", cli)
    assert res2.starter == "spring-boot-starter-webmvc" and "web_starter_by_evidence: false" in fu(res2)


def test_boot_pom_mode_reports_asis_starters_and_no_legacy_props(tmp_path: Path, ccfg: Config) -> None:
    boot = {"pom.xml": (HEAD + "    <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent"
                        "</artifactId><version>2.7.18</version></parent>\n    <groupId>com.acme</groupId>\n"
                        "    <artifactId>shop</artifactId>\n    <version>1.0</version>\n    <dependencies>\n"
                        + dep("org.springframework.boot", "spring-boot-starter-web") + "    </dependencies>\n</project>\n"),
            "src/main/java/com/acme/shop/App.java": ("package com.acme.shop;\n\nimport org.springframework.boot.autoconfigure."
                                                     "SpringBootApplication;\n\n@SpringBootApplication\npublic class App {\n}\n")}
    ws, res = run(tmp_path, ccfg, "boot", boot, mode="boot_pom")
    assert res.starter == "as-is: spring-boot-starter-web" and res.legacy_properties_written == []


def _cp949_tree() -> dict[str, str]:
    return {"pom.xml": (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n"
                        "    <version>1.0</version>\n</project>\n")}


def test_source_encoding_written_for_ms949_module(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _cp949_tree(), "enc")
    src = ws.root / "src/main/java/com/acme/shop"
    src.mkdir(parents=True)
    (src / "Greeting.java").write_bytes("package com.acme.shop;\n\n/** 인사 */\npublic class Greeting {\n"
                                        "    String s = \"안녕하세요\";\n}\n".encode("cp949"))
    (src / "Plain.java").write_text("package com.acme.shop;\n\npublic class Plain {\n}\n")
    res = Restructurer(commit=False).apply(ws, analyze_project(ws, ccfg), ccfg, mode="layout")
    assert res.ok, res.error
    assert res.source_encodings_written == [
        "shop-back/pom.xml: project.build.sourceEncoding=MS949 (1 of 2 sampled .java files cp949)"]
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in \
        (ws.root / "shop-back/pom.xml").read_text()
    assert "안녕하세요" in next(ws.root.rglob("Greeting.java")).read_bytes().decode("cp949")   # never re-encoded


def test_source_encoding_mixed_module_sentence_only(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _cp949_tree(), "enc-mixed")
    src = ws.root / "src/main/java/com/acme/shop"
    src.mkdir(parents=True)
    (src / "A.java").write_bytes("package com.acme.shop;\nclass A { String s = \"가나다\"; }\n".encode("cp949"))
    (src / "B.java").write_text("package com.acme.shop;\nclass B { String s = \"라마바\"; }\n", encoding="utf-8")
    res = Restructurer(commit=False).apply(ws, analyze_project(ws, ccfg), ccfg, mode="layout")
    assert res.ok, res.error
    assert res.source_encodings_written == [] and "the sources mix MS949 and UTF-8 files" in fu(res)


def test_source_encoding_switch_off(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.legacy_context.write_source_encoding = False
    ws = make_ws(tmp_path, _cp949_tree(), "enc-off")
    src = ws.root / "src/main/java/com/acme/shop"
    src.mkdir(parents=True)
    (src / "A.java").write_bytes("package com.acme.shop;\nclass A { String s = \"가나다\"; }\n".encode("cp949"))
    res = Restructurer(commit=False).apply(ws, analyze_project(ws, ccfg), ccfg, mode="layout")
    assert res.ok and res.source_encodings_written == []
    assert "the sources are MS949 but no project.build.sourceEncoding says so" in fu(res)       # sentence only


def test_scope_successor_table() -> None:
    """E-M6: the javax → jakarta successors of the recipe's ChangeDependency rules live in the YAML table."""
    assert RULES.scope_successors["javax.servlet:javax.servlet-api"] == "jakarta.servlet:jakarta.servlet-api"
    assert RULES.scope_successors["javax.servlet:jstl"] == "jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api"
