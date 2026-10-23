"""sbmig.analyze.webxml: web.xml (DTD 2.2 / 2.3, XSD 2.4 … 6.0) → WebXmlModel, never raising."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import PathView, parse_web_xml, scan_sources
from tests.analyze.helpers import write

DTD23 = """<?xml version="1.0" encoding="ISO-8859-1"?>
<!DOCTYPE web-app PUBLIC "-//Sun Microsystems, Inc.//DTD Web Application 2.3//EN"
        "http://java.sun.com/dtd/web-app_2_3.dtd">
<web-app>
    <display-name>legacy</display-name>
    <distributable/>
    <context-param><param-name>region</param-name><param-value>kr</param-value></context-param>
    <filter>
        <filter-name>audit</filter-name>
        <filter-class>com.acme.web.AuditFilter</filter-class>
    </filter>
    <filter>
        <filter-name>encoding</filter-name>
        <filter-class>com.acme.web.EncodingFilter</filter-class>
        <init-param><param-name>encoding</param-name><param-value>UTF-8</param-value></init-param>
    </filter>
    <filter>
        <filter-name>unused</filter-name>
        <filter-class>com.acme.web.UnusedFilter</filter-class>
    </filter>
    <filter-mapping><filter-name>encoding</filter-name><url-pattern>/*</url-pattern></filter-mapping>
    <filter-mapping>
        <filter-name>audit</filter-name>
        <url-pattern>/admin/*</url-pattern>
        <servlet-name>front</servlet-name>
        <dispatcher>request</dispatcher>
        <dispatcher>FORWARD</dispatcher>
        <dispatcher>BOGUS</dispatcher>
    </filter-mapping>
    <filter-mapping><filter-name>encoding</filter-name><url-pattern>*.do</url-pattern></filter-mapping>
    <listener><listener-class>com.acme.web.StartupListener</listener-class></listener>
    <servlet>
        <servlet-name>front</servlet-name>
        <servlet-class>com.acme.web.FrontServlet</servlet-class>
        <init-param><param-name>mode</param-name><param-value>fast</param-value></init-param>
        <load-on-startup>1</load-on-startup>
    </servlet>
    <servlet>
        <servlet-name>help</servlet-name>
        <jsp-file>/WEB-INF/jsp/help.jsp</jsp-file>
    </servlet>
    <servlet>
        <servlet-name>report</servlet-name>
        <servlet-class>com.acme.web.ReportServlet</servlet-class>
    </servlet>
    <servlet-mapping><servlet-name>front</servlet-name><url-pattern>/front/*</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>front</servlet-name><url-pattern>*.do</url-pattern></servlet-mapping>
    <servlet-mapping><servlet-name>ghost</servlet-name><url-pattern>/ghost</url-pattern></servlet-mapping>
    <session-config><session-timeout>45</session-timeout></session-config>
    <mime-mapping><extension>hwp</extension><mime-type>application/x-hwp</mime-type></mime-mapping>
    <welcome-file-list><welcome-file>index.jsp</welcome-file><welcome-file>index.html</welcome-file></welcome-file-list>
    <error-page><error-code>404</error-code><location>/WEB-INF/jsp/404.jsp</location></error-page>
    <error-page><exception-type>java.lang.Throwable</exception-type><location>/error.jsp</location></error-page>
    <taglib><taglib-uri>/tags/x</taglib-uri><taglib-location>/WEB-INF/x.tld</taglib-location></taglib>
    <resource-ref><res-ref-name>jdbc/main</res-ref-name><res-type>javax.sql.DataSource</res-type></resource-ref>
    <security-constraint><web-resource-collection><url-pattern>/admin/*</url-pattern></web-resource-collection></security-constraint>
    <login-config><auth-method>BASIC</auth-method></login-config>
</web-app>
"""

XSD31 = """<?xml version="1.0" encoding="UTF-8"?>
<web-app xmlns="http://xmlns.jcp.org/xml/ns/javaee" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://xmlns.jcp.org/xml/ns/javaee http://xmlns.jcp.org/xml/ns/javaee/web-app_3_1.xsd"
         version="3.1">
    <servlet>
        <servlet-name>upload</servlet-name>
        <servlet-class>com.acme.web.UploadServlet</servlet-class>
        <async-supported>true</async-supported>
        <multipart-config>
            <location>/tmp</location>
            <max-file-size>1048576</max-file-size>
            <max-request-size>2097152</max-request-size>
            <file-size-threshold>4096</file-size-threshold>
        </multipart-config>
    </servlet>
    <servlet-mapping><servlet-name>upload</servlet-name><url-pattern>/upload</url-pattern><url-pattern>/up</url-pattern></servlet-mapping>
    <error-page><location>/oops.jsp</location></error-page>
    <jsp-config><taglib><taglib-uri>/t</taglib-uri><taglib-location>/WEB-INF/t.tld</taglib-location></taglib></jsp-config>
</web-app>
"""


def test_dtd_23_descriptor() -> None:
    m = parse_web_xml(DTD23, "src/main/webapp/WEB-INF/web.xml")
    assert m.spec == "2.3" and m.display_name == "legacy" and m.distributable
    front, help_, report = m.servlets
    assert (front.name, front.servlet_class, front.load_on_startup) == ("front", "com.acme.web.FrontServlet", 1)
    assert front.url_patterns == ["/front/*", "*.do"]                    # several mappings merged by name
    assert front.init_params == {"mode": "fast"}
    assert help_.jsp_file == "/WEB-INF/jsp/help.jsp" and help_.servlet_class is None
    assert report.url_patterns == [] and report.load_on_startup is None
    # filter order = position of the first <filter-mapping>; an unmapped filter keeps order 0 and goes last
    assert [(f.name, f.order) for f in m.filters] == [("encoding", 1), ("audit", 2), ("unused", 0)]
    enc, audit, _ = m.filters
    assert enc.url_patterns == ["/*", "*.do"] and enc.init_params == {"encoding": "UTF-8"}
    assert audit.dispatchers == ["REQUEST", "FORWARD"] and audit.servlet_names == ["front"]
    assert m.listeners == ["com.acme.web.StartupListener"]
    assert m.session_timeout_minutes == 45
    assert m.welcome_files == ["index.jsp", "index.html"]
    assert [(e.error_code, e.exception_type, e.location) for e in m.error_pages] == [
        (404, None, "/WEB-INF/jsp/404.jsp"), (None, "java.lang.Throwable", "/error.jsp")]
    assert m.mime_mappings == {"hwp": "application/x-hwp"} and m.context_params == {"region": "kr"}
    assert m.security_constraints == 1 and m.login_config == "BASIC"
    assert m.resource_refs == ["jdbc/main"] and m.taglibs == 1
    assert any("ghost" in w for w in m.warnings) and any("BOGUS" in w for w in m.warnings)


def test_dtd_22_spec_from_system_id() -> None:
    text = ('<!DOCTYPE web-app PUBLIC "-//Sun Microsystems, Inc.//DTD Web Application 2.2//EN" '
            '"http://java.sun.com/j2ee/dtds/web-app_2_2.dtd">\n<web-app><servlet><servlet-name>a</servlet-name>'
            '<servlet-class>x.A</servlet-class></servlet></web-app>')
    m = parse_web_xml(text, "web.xml")
    assert m.spec == "2.2" and m.servlets[0].servlet_class == "x.A" and m.warnings == []


def test_xsd_31_multipart_async_default_error_page() -> None:
    m = parse_web_xml(XSD31, "web.xml")
    assert m.spec == "3.1"
    s = m.servlets[0]
    assert s.async_supported and s.url_patterns == ["/upload", "/up"]
    assert (s.multipart_location, s.multipart_max_file_size, s.multipart_max_request_size,
            s.multipart_file_size_threshold) == ("/tmp", 1048576, 2097152, 4096)
    assert m.error_pages[0].error_code is None and m.error_pages[0].exception_type is None
    assert m.taglibs == 1


def test_broken_and_empty_files_never_raise() -> None:
    m = parse_web_xml("", "web.xml")
    assert m.servlets == [] and m.warnings == ["web.xml is empty"]
    m = parse_web_xml("<web-app><servlet><servlet-name>a</servlet-name><servlet-class>x.A</servlet-class>", "w")
    assert m.servlets and m.servlets[0].servlet_class == "x.A"         # recovered leniently
    assert any("not well-formed" in w for w in m.warnings)
    m = parse_web_xml("this is not xml", "w")
    assert m.servlets == [] and m.warnings
    m = parse_web_xml("<beans/>", "w")
    assert "no <web-app> root element" in m.warnings
    m = parse_web_xml('<web-app><session-config><session-timeout>x</session-timeout></session-config></web-app>', "w")
    assert m.session_timeout_minutes is None and any("not an integer" in w for w in m.warnings)


def test_scan_sources_fills_web_xml_counts(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", "<project/>")
    write(tmp_path, "src/main/webapp/WEB-INF/web.xml", DTD23)
    s = scan_sources(PathView(tmp_path)).stats()
    assert (s.web_xml_spec, s.web_xml_servlets, s.web_xml_filters, s.web_xml_listeners) == ("2.3", 3, 3, 1)
    sub = scan_sources(PathView(tmp_path)).subset("other")
    assert sub.stats().web_xml_spec is None and sub.stats().web_xml_servlets == 0
