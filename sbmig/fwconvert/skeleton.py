"""생성할 Spring Boot 프로젝트의 뼈대를 만드는 모듈.
루트 pom, 네 모듈의 pom, 애플리케이션 클래스, Web MVC 설정 클래스, application.yml을 만든다.

Contract: out/scratch/fwconvert/ROUND2.md section P and docs/FW-CONVERT.md section 3.1 / 3.7.

Artifact names were checked against the real ``spring-boot-dependencies`` 4.1.0 pom (and the jars it names) in
``~/.m2``: the web starter is ``spring-boot-starter-webmvc`` (``spring-boot-starter-web`` still exists and is marked
deprecated in favour of it), ``spring-boot-starter-jdbc``, ``org.apache.tomcat.embed:tomcat-embed-jasper``,
``jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api`` + ``org.glassfish.web:jakarta.servlet.jsp.jstl``,
``spring-tx`` / ``spring-webmvc`` / ``spring-context`` through the imported ``spring-framework-bom``, ``slf4j-api``,
``jakarta.annotation-api``; ``SpringBootServletInitializer`` lives in ``org.springframework.boot.web.servlet.support``
(``spring-boot`` jar). The servlet encoding keys moved to ``spring.servlet.encoding.*`` in Boot 4 (the
``server.servlet.encoding.*`` names are deprecated with level ``error``). The MyBatis starter is not managed by Boot:
its version must come from ``ConvertOptions.versions``. ``jakarta.servlet.jsp.jstl-api`` 3.0.2 depends on
``jakarta.servlet-api`` at compile scope (``mvn dependency:tree``); the boot pom excludes it there, because the
embedded Tomcat (``tomcat-embed-core``) already carries the servlet API and a second copy in ``WEB-INF/lib`` would
duplicate its classes.

Versions: a dependency gets ``<version>`` only when ``options.versions`` has its ``g:a`` or the driver coordinate
carries one; everything else is managed by the Boot parent. ``check_options`` names what is missing (the Boot version,
the Java version, the MyBatis starter's version, an empty value for a key that is used); when it reports anything,
``generate_skeleton`` writes NO pom at all, so a pom with an empty ``<version>`` never exists.

Every resource handler gets a resolver that refuses ``.jsp`` paths: the handlers map top folders of the static
folder, which may hold JSPs, and a JSP must be rendered, never served as text."""
from __future__ import annotations

import json
import re
from xml.sax.saxutils import escape

from sbmig.fwconvert.javagen import java_string
from sbmig.fwconvert.model import ConvertOptions, DatasourceInfo, GeneratedFile, Layout
from sbmig.fwconvert.naming import x_name
from sbmig.fwconvert.rules import ConvertRules

MYBATIS_STARTER = "org.mybatis.spring.boot:mybatis-spring-boot-starter"
BOOT_PARENT = "org.springframework.boot:spring-boot-starter-parent"
WEB_STARTER = "org.springframework.boot:spring-boot-starter-webmvc"
JDBC_STARTER = "org.springframework.boot:spring-boot-starter-jdbc"
JASPER = "org.apache.tomcat.embed:tomcat-embed-jasper"
JSTL_API = "jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api"
JSTL_IMPL = "org.glassfish.web:jakarta.servlet.jsp.jstl"
SPRING_CONTEXT = "org.springframework:spring-context"
SPRING_WEBMVC = "org.springframework:spring-webmvc"
SPRING_TX = "org.springframework:spring-tx"
SLF4J = "org.slf4j:slf4j-api"
ANNOTATION_API = "jakarta.annotation:jakarta.annotation-api"
BOOT_PLUGIN = "org.springframework.boot:spring-boot-maven-plugin"
# JSTL API가 compile 범위로 끌어오는 서블릿 API. 내장 Tomcat에 이미 있으므로 WAR에 넣지 않는다
SERVLET_API = "jakarta.servlet:jakarta.servlet-api"
PASSWORD_PLACEHOLDER = "${SPRING_DATASOURCE_PASSWORD:}"
MAPPER_LOCATIONS = "classpath:mybatis/mapper/*.xml"
CONFIG_CLASS = "WebMvcConfig"

_COORD = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(?::[A-Za-z0-9_.\-+]+)?$")
_POM_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<project xmlns="http://maven.apache.org/POM/4.0.0" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
             '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
             'https://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
             '    <modelVersion>4.0.0</modelVersion>\n')


def application_class(layout: Layout) -> str:
    """애플리케이션 클래스의 이름을 반환한다. 프로젝트 이름 뒤에 ``Application``을 붙인다."""
    return f"{x_name(layout.identity.project_name) or 'App'}Application"


def group_id(layout: Layout, options: ConvertOptions) -> str:
    """생성하는 pom의 groupId를 반환한다. 옵션에 없으면 기본 패키지에서 마지막 조각을 뗀 값이다."""
    if options.group_id.strip():
        return options.group_id.strip()
    pkg = layout.pkg
    return pkg.rsplit(".", 1)[0] if "." in pkg else pkg


def driver_coordinate(options: ConvertOptions, datasource: DatasourceInfo) -> tuple[str, str]:
    """데이터소스 벤더의 JDBC 드라이버 좌표를 ``(g:a, 버전)``으로 반환한다.
    데이터소스를 찾지 못했거나 벤더의 좌표가 없거나 좌표 모양이 틀리면 빈 문자열 둘을 반환한다."""
    if not datasource.configured or not datasource.vendor:
        return "", ""
    coord = str(options.jdbc_drivers.get(datasource.vendor) or "").strip()
    if not _COORD.match(coord):
        return "", ""
    parts = coord.split(":")
    return f"{parts[0]}:{parts[1]}", (parts[2] if len(parts) == 3 else "")


def check_options(options: ConvertOptions, datasource: DatasourceInfo) -> list[str]:
    """pom을 만들 수 없게 하는 옵션 문제를 한 줄씩 반환한다. 문제가 없으면 빈 목록이다.
    Boot 버전, Java 버전, MyBatis 스타터 버전이 비어 있거나, 쓰는 의존성의 버전 값이 빈 경우다.
    드라이버 좌표의 모양이 틀린 것도 여기에 넣는다. 그 경우 pom은 만들되 드라이버는 넣지 않는다."""
    errors: list[str] = []
    if not str(options.boot_version or "").strip():
        errors.append("skeleton: the Spring Boot version (ConvertOptions.boot_version) is empty, no pom written")
    if not str(options.java_version or "").strip():
        errors.append("skeleton: the Java version (ConvertOptions.java_version) is empty, no pom written")
    if not str(options.versions.get(MYBATIS_STARTER) or "").strip():
        errors.append(f"skeleton: no version for {MYBATIS_STARTER} in ConvertOptions.versions "
                      "(Spring Boot does not manage it), no pom written")
    for ga in sorted(_used_keys()):
        if ga != MYBATIS_STARTER and ga in options.versions and not str(options.versions[ga] or "").strip():
            errors.append(f"skeleton: the version of {ga} in ConvertOptions.versions is empty, no pom written")
    if datasource.configured and datasource.vendor:
        coord = str(options.jdbc_drivers.get(datasource.vendor) or "").strip()
        if coord and not _COORD.match(coord):
            errors.append(f"skeleton: the JDBC driver coordinate of {datasource.vendor} is not g:a or g:a:v "
                          f"({coord!r}), no driver dependency written")
    return errors


def _pom_blocking(errors: list[str]) -> bool:
    return any("no pom written" in e for e in errors)


def _used_keys() -> set[str]:
    return {MYBATIS_STARTER, WEB_STARTER, JDBC_STARTER, JASPER, JSTL_API, JSTL_IMPL, SPRING_CONTEXT, SPRING_WEBMVC,
            SPRING_TX, SLF4J, ANNOTATION_API, BOOT_PLUGIN}


# ---- pom ----------------------------------------------------------------------------


def _x(text: str) -> str:
    return escape(str(text))


def _dep(ga: str, options: ConvertOptions, *, version: str = "", scope: str = "",
         exclude: tuple[str, ...] = ()) -> list[str]:
    g, a = ga.split(":", 1)
    v = version or str(options.versions.get(ga) or "").strip()
    lines = ["        <dependency>", f"            <groupId>{_x(g)}</groupId>",
             f"            <artifactId>{_x(a)}</artifactId>"]
    if v:
        lines.append(f"            <version>{_x(v)}</version>")
    if scope:
        lines.append(f"            <scope>{_x(scope)}</scope>")
    if exclude:
        lines.append("            <exclusions>")
        for ex in exclude:
            eg, ea = ex.split(":", 1)
            lines += ["                <exclusion>", f"                    <groupId>{_x(eg)}</groupId>",
                      f"                    <artifactId>{_x(ea)}</artifactId>", "                </exclusion>"]
        lines.append("            </exclusions>")
    lines.append("        </dependency>")
    return lines


def _module_dep(gid: str, artifact: str) -> list[str]:
    return ["        <dependency>", f"            <groupId>{_x(gid)}</groupId>",
            f"            <artifactId>{_x(artifact)}</artifactId>",
            "            <version>${project.version}</version>", "        </dependency>"]


def _module_pom(layout: Layout, options: ConvertOptions, artifact: str, packaging: str, deps: list[str],
                build: list[str] | None = None) -> str:
    gid = group_id(layout, options)
    lines = [_POM_HEAD.rstrip("\n"),
             "    <parent>", f"        <groupId>{_x(gid)}</groupId>",
             f"        <artifactId>{_x(layout.identity.project_name)}</artifactId>",
             f"        <version>{_x(options.version)}</version>", "    </parent>",
             f"    <artifactId>{_x(artifact)}</artifactId>", f"    <packaging>{packaging}</packaging>"]
    if deps:
        lines += ["    <dependencies>", *deps, "    </dependencies>"]
    if build:
        lines += build
    lines.append("</project>")
    return "\n".join(lines) + "\n"


def _root_pom(layout: Layout, options: ConvertOptions) -> str:
    g, a = BOOT_PARENT.split(":")
    m = layout.identity.modules
    lines = [_POM_HEAD.rstrip("\n"),
             "    <parent>", f"        <groupId>{g}</groupId>", f"        <artifactId>{a}</artifactId>",
             f"        <version>{_x(options.boot_version.strip())}</version>", "        <relativePath/>",
             "    </parent>",
             f"    <groupId>{_x(group_id(layout, options))}</groupId>",
             f"    <artifactId>{_x(layout.identity.project_name)}</artifactId>",
             f"    <version>{_x(options.version)}</version>", "    <packaging>pom</packaging>",
             "    <properties>", f"        <java.version>{_x(options.java_version.strip())}</java.version>",
             "    </properties>",
             "    <modules>",
             *(f"        <module>{_x(name)}</module>" for name in (m.domain, m.store, m.service, m.boot)),
             "    </modules>", "</project>"]
    return "\n".join(lines) + "\n"


def _poms(layout: Layout, options: ConvertOptions, datasource: DatasourceInfo) -> list[GeneratedFile]:
    m = layout.identity.modules
    gid = group_id(layout, options)
    files = [GeneratedFile("pom.xml", _root_pom(layout, options))]
    files.append(GeneratedFile(f"{m.domain}/pom.xml", _module_pom(
        layout, options, m.domain, "jar", _dep(ANNOTATION_API, options))))
    files.append(GeneratedFile(f"{m.store}/pom.xml", _module_pom(
        layout, options, m.store, "jar",
        _module_dep(gid, m.domain) + _dep(MYBATIS_STARTER, options) + _dep(SPRING_CONTEXT, options))))
    files.append(GeneratedFile(f"{m.service}/pom.xml", _module_pom(
        layout, options, m.service, "jar",
        _module_dep(gid, m.domain) + _dep(SPRING_WEBMVC, options) + _dep(SPRING_TX, options)
        + _dep(SLF4J, options))))
    deps = (_module_dep(gid, m.service) + _module_dep(gid, m.store) + _module_dep(gid, m.domain)
            + _dep(WEB_STARTER, options) + _dep(JDBC_STARTER, options) + _dep(MYBATIS_STARTER, options)
            + _dep(JASPER, options) + _dep(JSTL_API, options, exclude=(SERVLET_API,)) + _dep(JSTL_IMPL, options))
    driver, driver_version = driver_coordinate(options, datasource)
    if driver:
        deps += _dep(driver, options, version=driver_version, scope="runtime")
    pg, pa = BOOT_PLUGIN.split(":")
    plugin_version = str(options.versions.get(BOOT_PLUGIN) or "").strip()
    build = ["    <build>", f"        <finalName>{_x(layout.identity.project_name)}</finalName>",
             "        <plugins>", "            <plugin>", f"                <groupId>{pg}</groupId>",
             f"                <artifactId>{pa}</artifactId>"]
    if plugin_version:
        build.append(f"                <version>{_x(plugin_version)}</version>")
    build += ["            </plugin>", "        </plugins>", "    </build>"]
    files.append(GeneratedFile(f"{m.boot}/pom.xml", _module_pom(layout, options, m.boot, "war", deps, build)))
    return files


# ---- 자바 클래스 ------------------------------------------------------------------------


def _application(layout: Layout) -> GeneratedFile:
    name = application_class(layout)
    text = "\n".join([
        f"package {layout.pkg};",
        "",
        "import org.springframework.boot.SpringApplication;",
        "import org.springframework.boot.autoconfigure.SpringBootApplication;",
        "import org.springframework.boot.builder.SpringApplicationBuilder;",
        "import org.springframework.boot.web.servlet.support.SpringBootServletInitializer;",
        "",
        "@SpringBootApplication",
        f"public class {name} extends SpringBootServletInitializer {{",
        "",
        "    @Override",
        "    protected SpringApplicationBuilder configure(SpringApplicationBuilder builder) {",
        f"        return builder.sources({name}.class);",
        "    }",
        "",
        "    public static void main(String[] args) {",
        f"        SpringApplication.run({name}.class, args);",
        "    }",
        "}",
    ]) + "\n"
    return GeneratedFile(layout.java_path(layout.pkg, name), text)


def _clean_tops(static_top: list[str]) -> list[str]:
    tops: set[str] = set()
    for t in static_top:
        s = str(t or "").strip().strip("/")
        # 폴더 이름 하나만 받는다. 경로를 벗어나는 이름과 와일드카드는 쓰지 않는다
        if s and "/" not in s and "\\" not in s and s not in (".", "..") and "*" not in s and "{" not in s:
            tops.add(s)
    return sorted(tops)


def _web_config(layout: Layout, static_top: list[str]) -> GeneratedFile:
    pkg = f"{layout.pkg}.config"
    base = f"/WEB-INF/static/{layout.identity.project_name}/"
    tops = _clean_tops(static_top)
    imports = ["import org.springframework.context.annotation.Configuration;"]
    if tops:
        imports += ["import org.springframework.core.io.Resource;",
                    "import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;"]
    imports += ["import org.springframework.web.servlet.config.annotation.ViewResolverRegistry;",
                "import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;"]
    if tops:
        imports.append("import org.springframework.web.servlet.resource.PathResourceResolver;")
        imports = ["import java.io.IOException;", "import java.util.Locale;", ""] + imports
    body = [
        f"package {pkg};",
        "",
        *imports,
        "",
        "@Configuration",
        f"public class {CONFIG_CLASS} implements WebMvcConfigurer {{",
        "",
        "    @Override",
        "    public void configureViewResolvers(ViewResolverRegistry registry) {",
        f"        registry.jsp({java_string(base)}, \".jsp\");",
        "    }",
    ]
    if tops:
        body += ["", "    @Override", "    public void addResourceHandlers(ResourceHandlerRegistry registry) {"]
        for top in tops:
            body += [f"        registry.addResourceHandler({java_string('/' + top + '/**')})",
                     f"                .addResourceLocations({java_string(base + top + '/')})",
                     "                .resourceChain(false)",
                     "                .addResolver(new NoJspResourceResolver());"]
        body += ["    }", "",
                 "    // 정적 파일 폴더에 JSP가 섞여 있어도 JSP 소스를 그대로 내려 주지 않는다",
                 "    private static final class NoJspResourceResolver extends PathResourceResolver {",
                 "",
                 "        @Override",
                 "        protected Resource getResource(String resourcePath, Resource location) throws IOException {",
                 "            if (resourcePath.toLowerCase(Locale.ROOT).contains(\".jsp\")) {",
                 "                return null;",
                 "            }",
                 "            return super.getResource(resourcePath, location);",
                 "        }",
                 "    }"]
    body.append("}")
    return GeneratedFile(layout.java_path(pkg, CONFIG_CLASS), "\n".join(body) + "\n")


# ---- application.yml --------------------------------------------------------------------


def _yaml_str(text: str) -> str:
    # JSON 문자열은 YAML의 큰따옴표 문자열로도 읽힌다
    return json.dumps(str(text), ensure_ascii=False)


def _application_yml(datasource: DatasourceInfo) -> str:
    lines = ["spring:", "  servlet:", "    encoding:", "      charset: UTF-8", "      enabled: true",
             "      force: true"]
    if datasource.configured and datasource.url:
        lines += ["  datasource:", f"    url: {_yaml_str(datasource.url)}"]
        if datasource.driver_class:
            lines.append(f"    driver-class-name: {_yaml_str(datasource.driver_class)}")
        if datasource.username:
            lines.append(f"    username: {_yaml_str(datasource.username)}")
        lines.append(f"    password: {_yaml_str(PASSWORD_PLACEHOLDER)}")
    lines += ["mybatis:", f"  mapper-locations: {_yaml_str(MAPPER_LOCATIONS)}", "  configuration:",
              "    map-underscore-to-camel-case: true"]
    return "\n".join(lines) + "\n"


# ---- 진입점 -------------------------------------------------------------------------------


def generate_skeleton(layout: Layout, options: ConvertOptions, datasource: DatasourceInfo, rules: ConvertRules,
                      static_top: list[str]) -> list[GeneratedFile]:
    """루트 pom, 네 모듈의 pom, 애플리케이션 클래스, Web MVC 설정, ``application.yml``을 만들어 반환한다.
    ``check_options``가 pom을 막는 문제를 찾으면 pom은 하나도 만들지 않는다.
    ``static_top``은 정적 파일의 최상위 폴더 이름들이다. 폴더마다 리소스 핸들러를 하나씩 만든다."""
    files: list[GeneratedFile] = []
    if not _pom_blocking(check_options(options, datasource)):
        files += _poms(layout, options, datasource)
    files.append(_application(layout))
    files.append(_web_config(layout, static_top))
    files.append(GeneratedFile(f"{layout.identity.modules.boot}/src/main/resources/application.yml",
                               _application_yml(datasource)))
    return sorted(files, key=lambda f: f.path)
