"""Programmatic builders for tiny Maven projects used by analyze/plan tests."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

NS = 'xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def dep(g: str, a: str, v: Optional[str] = None, scope: Optional[str] = None, type_: Optional[str] = None) -> str:
    out = f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
    if v:
        out += f"<version>{v}</version>"
    if type_:
        out += f"<type>{type_}</type>"
    if scope:
        out += f"<scope>{scope}</scope>"
    return out + "</dependency>"


def plugin(a: str, v: Optional[str] = None, g: Optional[str] = None, config: str = "") -> str:
    out = "<plugin>"
    if g:
        out += f"<groupId>{g}</groupId>"
    out += f"<artifactId>{a}</artifactId>"
    if v:
        out += f"<version>{v}</version>"
    if config:
        out += f"<configuration>{config}</configuration>"
    return out + "</plugin>"


BOOT_PLUGIN = plugin("spring-boot-maven-plugin", g="org.springframework.boot")


def pom(artifact: str, *, group: Optional[str] = "com.acme", version: Optional[str] = "1.0.0",
        packaging: Optional[str] = None, parent: Optional[tuple[str, str, str]] = None,
        relative_path: Optional[str] = None, modules: tuple[str, ...] = (), props: Optional[dict] = None,
        deps: tuple[str, ...] = (), managed: tuple[str, ...] = (), plugins: tuple[str, ...] = (),
        plugin_mgmt: tuple[str, ...] = (), namespace: bool = True) -> str:
    parts = [f"<project {NS if namespace else ''}>", "<modelVersion>4.0.0</modelVersion>"]
    if parent:
        parts.append(f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId>"
                     f"<version>{parent[2]}</version>"
                     + (f"<relativePath>{relative_path}</relativePath>" if relative_path is not None else "")
                     + "</parent>")
    if group:
        parts.append(f"<groupId>{group}</groupId>")
    parts.append(f"<artifactId>{artifact}</artifactId>")
    if version:
        parts.append(f"<version>{version}</version>")
    if packaging:
        parts.append(f"<packaging>{packaging}</packaging>")
    if modules:
        parts.append("<modules>" + "".join(f"<module>{m}</module>" for m in modules) + "</modules>")
    if props:
        parts.append("<properties>" + "".join(f"<{k}>{v}</{k}>" for k, v in props.items()) + "</properties>")
    if managed:
        parts.append("<dependencyManagement><dependencies>" + "".join(managed) + "</dependencies></dependencyManagement>")
    if deps:
        parts.append("<dependencies>" + "".join(deps) + "</dependencies>")
    if plugins or plugin_mgmt:
        b = "<build>"
        if plugin_mgmt:
            b += "<pluginManagement><plugins>" + "".join(plugin_mgmt) + "</plugins></pluginManagement>"
        if plugins:
            b += "<plugins>" + "".join(plugins) + "</plugins>"
        parts.append(b + "</build>")
    parts.append("</project>")
    return "\n".join(parts)


def java(pkg: str, name: str, body: str = "", imports: tuple[str, ...] = (), annotations: tuple[str, ...] = (),
         extends: str = "", implements: str = "") -> str:
    head = f"package {pkg};\n\n" + "".join(f"import {i};\n" for i in imports) + "\n"
    ann = "".join(f"@{a}\n" for a in annotations)
    ext = f" extends {extends}" if extends else ""
    imp = f" implements {implements}" if implements else ""
    return f"{head}{ann}public class {name}{ext}{imp} {{\n{body}\n}}\n"


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def write_java(root: Path, module: str, pkg: str, name: str, *, test: bool = False, **kw) -> Path:
    base = f"{module}/" if module else ""
    kind = "test" if test else "main"
    return write(root, f"{base}src/{kind}/java/{pkg.replace('.', '/')}/{name}.java", java(pkg, name, **kw))


PSVM = "public static void main(String[] args) { }"


# ---------------------------------------------------------------------------
# canned projects
# ---------------------------------------------------------------------------
def make_boot27(root: Path, module: str = "", artifact: str = "boot-app", junit4: bool = True) -> Path:
    deps = [dep("org.springframework.boot", "spring-boot-starter-web"),
            dep("org.springframework.boot", "spring-boot-starter-data-jpa"),
            dep("org.projectlombok", "lombok", "1.18.24", scope="provided"),
            dep("mysql", "mysql-connector-java")]
    if junit4:
        deps.append(dep("junit", "junit", "4.13.2", scope="test"))
    write(root, (f"{module}/" if module else "") + "pom.xml", pom(
        artifact, parent=("org.springframework.boot", "spring-boot-starter-parent", "2.7.18"), relative_path="",
        props={"java.version": "1.8"}, deps=tuple(deps),
        plugins=(plugin("spring-boot-maven-plugin", g="org.springframework.boot",
                        config="<loaderImplementation>CLASSIC</loaderImplementation>"),
                 plugin("maven-compiler-plugin", "3.8.1"))))
    write_java(root, module, "com.acme.shop", "ShopApplication", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    write_java(root, module, "com.acme.shop.web", "OrderController", annotations=("RestController",),
               imports=("org.springframework.web.bind.annotation.RestController", "javax.servlet.http.HttpServletRequest"))
    write_java(root, module, "com.acme.shop.domain", "Order", annotations=("Entity", "Data"),
               imports=("javax.persistence.Entity", "lombok.Data"))
    if junit4:
        write_java(root, module, "com.acme.shop", "ShopApplicationTests", test=True,
                   annotations=("RunWith(SpringRunner.class)", "SpringBootTest"),
                   imports=("org.junit.Test", "org.junit.runner.RunWith",
                            "org.springframework.boot.test.context.SpringBootTest"),
                   body="@Test public void contextLoads() {}")
    write(root, (f"{module}/" if module else "") + "src/main/resources/application.yml", "server:\n  port: 8080\n")
    return root


def make_boot33(root: Path, module: str = "", artifact: str = "boot33-app") -> Path:
    write(root, (f"{module}/" if module else "") + "pom.xml", pom(
        artifact, parent=("org.springframework.boot", "spring-boot-starter-parent", "3.3.5"), relative_path="",
        props={"java.version": "17"},
        deps=(dep("org.springframework.boot", "spring-boot-starter-web"),
              dep("org.springframework.boot", "spring-boot-starter-security"),
              dep("org.springframework.boot", "spring-boot-starter-test", scope="test")),
        plugins=(BOOT_PLUGIN,)))
    write_java(root, module, "com.acme.billing", "BillingApplication", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    write_java(root, module, "com.acme.billing.web", "InvoiceController", annotations=("RestController",),
               imports=("org.springframework.web.bind.annotation.RestController", "jakarta.servlet.http.HttpServletRequest"))
    write_java(root, module, "com.acme.billing", "BillingApplicationTests", test=True,
               annotations=("SpringBootTest",),
               imports=("org.junit.jupiter.api.Test", "org.springframework.boot.test.context.SpringBootTest"),
               body="@Test void contextLoads() {}")
    return root


def make_servlet(root: Path) -> Path:
    write(root, "pom.xml", pom("legacy-web", packaging="war",
                               deps=(dep("javax.servlet", "javax.servlet-api", "3.1.0", scope="provided"),
                                     dep("javax.servlet", "jstl", "1.2"))))
    write_java(root, "", "com.acme.web", "HelloServlet", extends="HttpServlet",
               imports=("javax.servlet.http.HttpServlet", "javax.servlet.annotation.WebServlet"),
               annotations=('WebServlet("/hello")',))
    write_java(root, "", "com.acme.web", "AuthFilter", implements="Filter",
               imports=("javax.servlet.Filter", "javax.servlet.FilterChain"))
    write(root, "src/main/webapp/WEB-INF/web.xml", "<web-app></web-app>")
    write(root, "src/main/webapp/index.jsp", "<html></html>")
    write(root, "src/main/webapp/WEB-INF/views/list.jsp", "<html></html>")
    return root


def make_spring_legacy(root: Path, security: bool = True) -> Path:
    deps = [dep("org.springframework", "spring-webmvc", "${spring.version}"),
            dep("org.springframework", "spring-context", "${spring.version}"),
            dep("javax.servlet", "javax.servlet-api", "4.0.1", scope="provided"),
            dep("junit", "junit", "4.12", scope="test")]
    if security:
        deps.append(dep("org.springframework.security", "spring-security-web", "5.8.0"))
    write(root, "pom.xml", pom("legacy-spring", packaging="war", props={"spring.version": "5.3.30",
                                                                         "maven.compiler.source": "1.8"},
                               deps=tuple(deps)))
    write_java(root, "", "com.acme.legacy.web", "HomeController", annotations=("Controller",),
               imports=("org.springframework.stereotype.Controller",))
    write(root, "src/main/webapp/WEB-INF/spring/root-context.xml",
          '<beans xmlns="http://www.springframework.org/schema/beans"></beans>')
    write(root, "src/main/webapp/WEB-INF/web.xml", "<web-app></web-app>")
    return root


def make_pure_java(root: Path, *, javax_ee: bool = False, junit4: bool = False) -> Path:
    deps = [dep("org.apache.commons", "commons-lang3", "3.12.0"),
            dep("com.google.guava", "guava", "31.1-jre")]
    if junit4:
        deps.append(dep("junit", "junit", "4.13.2", scope="test"))
    write(root, "pom.xml", pom("util-lib", props={"maven.compiler.release": "11"}, deps=tuple(deps)))
    imports = ["javax.swing.JFrame", "javax.sql.DataSource", "javax.annotation.Nullable"]
    if javax_ee:
        imports.append("javax.xml.bind.JAXBContext")
    write_java(root, "", "com.acme.util", "Main", imports=tuple(imports), body=PSVM)
    write_java(root, "", "com.acme.util.text", "Strings")
    if junit4:
        write_java(root, "", "com.acme.util", "MainTest", test=True, imports=("org.junit.Test",))
    return root
