from __future__ import annotations

from pathlib import Path

from sbmig.analyze import PathView, scan_sources
from sbmig.analyze.source_scanner import base_package, scan_java_text, strip_comments
from tests.analyze.helpers import PSVM, make_boot27, make_servlet, make_spring_legacy, write, write_java


def test_boot_sources(tmp_path: Path) -> None:
    make_boot27(tmp_path)
    write(tmp_path, "src/main/resources/logback-spring.xml", "<configuration/>")
    write(tmp_path, "src/main/resources/bootstrap.properties", "a=b")
    write(tmp_path, "target/generated/Gen.java", "package x; public class Gen {}")
    write(tmp_path, "src/main/kotlin/K.kt", "class K")
    s = scan_sources(PathView(tmp_path)).stats()
    assert s.java_files == 4 and s.test_files == 1 and s.kotlin_files == 1
    assert s.base_package == "com.acme.shop"
    assert s.main_classes == ["com.acme.shop.ShopApplication"]
    assert s.psvm_classes == []
    assert s.controllers == ["com.acme.shop.web.OrderController"]
    assert s.javax_imports == {"javax.persistence": 1, "javax.servlet": 1}
    assert s.spring_annotations["SpringBootApplication"] == 1 and s.spring_annotations["RunWith"] == 1
    assert s.spring_boot_test_classes == ["com.acme.shop.ShopApplicationTests"]
    assert s.lombok
    assert s.config_files == ["src/main/resources/application.yml", "src/main/resources/bootstrap.properties",
                              "src/main/resources/logback-spring.xml"]
    assert s.xml_bean_configs == []


def test_servlet_and_legacy_files(tmp_path: Path) -> None:
    make_servlet(tmp_path / "s")
    s = scan_sources(PathView(tmp_path / "s")).stats()
    assert s.servlet_classes == ["com.acme.web.HelloServlet"]
    assert s.filter_listener_classes == ["com.acme.web.AuthFilter"]
    assert s.has_web_xml and s.web_xml_paths == ["src/main/webapp/WEB-INF/web.xml"] and s.jsp_count == 2
    make_spring_legacy(tmp_path / "l")
    s = scan_sources(PathView(tmp_path / "l")).stats()
    assert s.xml_bean_configs == ["src/main/webapp/WEB-INF/spring/root-context.xml"]


def test_comments_and_jakarta_and_psvm(tmp_path: Path) -> None:
    src = '''package com.x.y;
// import javax.servlet.Bogus;
/* @SpringBootApplication */
import static jakarta.persistence.Persistence.createEntityManagerFactory;
import jakarta.inject.Inject;
import javax.xml.bind.JAXBContext;
import javax.xml.parsers.DocumentBuilder;
import javax.xml.XMLConstants;
@WebListener
public class App implements javax.servlet.ServletContextListener {
  String s = "http://example.com // not a comment";
  static public void main(String[] a) {}
}'''
    info = scan_java_text("src/main/java/com/x/y/App.java", src)
    assert info.fqcn == "com.x.y.App" and info.has_psvm and not info.is_boot_app
    assert "javax.servlet.Bogus" not in info.imports
    assert info.imports[:2] == ["jakarta.persistence.Persistence.createEntityManagerFactory", "jakarta.inject.Inject"]
    assert info.is_filter_listener
    assert "http://example.com" in strip_comments(src)
    write(tmp_path, "src/main/java/com/x/y/App.java", src)
    write_java(tmp_path, "", "com.x.z", "Tool", body=PSVM)
    s = scan_sources(PathView(tmp_path)).stats()
    assert s.jakarta_imports == {"jakarta.inject": 1, "jakarta.persistence": 1}
    assert s.javax_imports == {"javax.xml": 1, "javax.xml.bind": 1, "javax.xml.parsers": 1}
    assert s.psvm_classes == ["com.x.y.App", "com.x.z.Tool"]
    assert s.base_package == "com.x"


def test_base_package_rules() -> None:
    assert base_package([]) is None
    assert base_package(["com.acme.a.b", "com.acme.a.c"]) == "com.acme.a"
    assert base_package(["com.acme.a", "org.other.b", "com.acme.c"]) == "com.acme"
    assert base_package(["single"]) == "single"
    assert base_package(["x", "y"]) == "x"


def test_subset(tmp_path: Path) -> None:
    write_java(tmp_path, "a", "com.a", "A")
    write_java(tmp_path, "b", "com.b", "B")
    scan = scan_sources(PathView(tmp_path))
    assert [f.fqcn for f in scan.subset("a").files] == ["com.a.A"]
    assert len(scan.subset("").files) == 2


def test_jsp_javax_files(tmp_path: Path) -> None:
    write(tmp_path, "src/main/webapp/a.jsp", '<%@ page import="javax.servlet.http.Cookie, java.util.List" %>')
    write(tmp_path, "src/main/webapp/b.jsp", "<% javax.servlet.http.HttpSession s = null; %>")
    write(tmp_path, "src/main/webapp/WEB-INF/tags/t.tag", '<jsp:useBean id="x" class="javax.mail.Session"/>')
    write(tmp_path, "src/main/webapp/inc.jspf", "<%= ((javax.servlet.http.HttpServletRequest) request).getMethod() %>")
    write(tmp_path, "src/main/webapp/c.jsp", '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>'
                                            "<p>javax.servlet</p><% javax.sql.DataSource d; %>")
    write(tmp_path, "src/main/webapp/d.jsp", '<%-- <% javax.servlet.http.HttpSession s; %> --%><% String k = '
                                            '"javax.servlet.error"; %>')
    write(tmp_path, "target/x/e.jsp", '<%@ page import="javax.servlet.http.Cookie" %>')
    res = scan_sources(PathView(tmp_path))
    assert res.jsp_javax_paths == ["src/main/webapp/WEB-INF/tags/t.tag", "src/main/webapp/a.jsp",
                                   "src/main/webapp/b.jsp", "src/main/webapp/inc.jspf"]
    s = res.stats()
    assert s.jsp_javax_files == 4 and s.jsp_count == 4
    assert res.subset("src/main/webapp/WEB-INF").stats().jsp_javax_files == 1
