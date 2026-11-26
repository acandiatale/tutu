"""프로젝트가 어떤 라이브러리를 쓰는지 규칙 표로 찾는 모듈.

The analyzer calls it for the profile's ``libraries``, and once more per service for MSA. The fix-script
prompt reads the names too; renaming a rule silently stops its advisory, and a new rule takes the name of
its ``library`` key in ``knowledge/libraries/advisories.yml``.
The library *names* are a contract (planner, notes selection, advisories and KB tags rely on them):
lombok, mapstruct, querydsl, spring-security, spring-data-jpa, spring-data-redis, spring-kafka, spring-batch,
spring-cloud, hibernate, mybatis, jpa, springdoc, springfox, feign, jackson, junit4, junit5, mockito,
testcontainers, log4j2, logback, javax-servlet, jakarta-servlet, jsp, jaxb, javax-annotation, validation,
thymeleaf, flyway, liquibase, undertow, jetty, tomcat-embed, spring-webflux, spring-webmvc, spring-context,
spring-retry, elasticsearch, mongodb,
plus the library-advisory rules (knowledge/libraries/advisories.yml, docs/ADVISORIES.md): sleuth, hystrix, ribbon,
zuul, netflix-feign, spring-cloud-bom, swagger-v1, spring-security-oauth2-legacy, log4j1, commons-lang2, httpclient4,
jdk-removed-apis (imports of JDK-internal / removed packages: sun.misc, sun.security, com.sun.image, javax.activation;
JAXB `javax.xml.bind` stays on the `jaxb` rule), jdk-generified-apis, ibatis2 (iBATIS 2: `com.ibatis` /
`org.springframework.orm.ibatis` imports, `org.apache.ibatis:ibatis-*` artifacts; `mybatis` = MyBatis 3 only),
hibernate-commons-annotations (`org.hibernate.annotations.common.*` imports / the HCANN artifact, gone from
Hibernate ORM 7), and the era-library rules (advisories of the same name unless noted): hibernate-template
(Spring's `org.springframework.orm.hibernate3|4|5` imports; import-only, spring-orm itself is too broad),
commons-fileupload, jackson1, tiles, velocity, axis1 (`org.apache.axis` / `javax.xml.rpc`; not
`org.apache.axis2`), commons-httpclient3, commons-collections3 (not `collections4`), ehcache2, json-lib, jjwt
(advisory jjwt-legacy, < 0.10),
poi (advisory poi-legacy, < 5.0), struts2 (< 7.0), egovframe, dbcp1 (advisory commons-dbcp1; not `dbcp2`),
querydsl-hibernate-native (`com.querydsl.jpa.hibernate.*` imports only).

Edge-case names added by the analyzer (not table-driven, see `sbmig.analyze.edges`): system-scope-jars, kotlin,
groovy, scala, codegen:<plugin artifactId> (protobuf-maven-plugin, jaxb2-maven-plugin, openapi-generator-maven-plugin,
maven-jaxb2-plugin, querydsl-maven-plugin), legacy-encoding (evidence = detected encodings).

Evidence strings: `pom:<g>:<a>[:<v>]`, `pom-managed:<g>:<a>[:<v>]` (dependencyManagement entry, rules with
`managed`), `pom-parent:<g>:<a>[:<v>]` (parent coordinates, rules with `parents`), `import:<prefix>`, `file:<what>`.
"""
from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from sbmig.core.models import GAV, LibraryUsage, PomInfo

from .source_scanner import ScanResult

_B = "org.springframework.boot"
_SC = "org.springframework.cloud"


@dataclass(frozen=True)
class LibraryRule:
    """라이브러리 하나를 알아보는 규칙을 담는 클래스.
    의존성, import, JSP, 설정 파일, dependencyManagement, 부모 pom 중 무엇을 근거로 볼지 정한다."""
    name: str
    deps: tuple[str, ...] = ()        # "group:artifact-glob", 양쪽 모두 fnmatch로 비교한다
    imports: tuple[str, ...] = ()     # import 접두어, 패키지 경계에서만 비교한다
    jsp: bool = False                 # True면 *.jsp 파일도 근거로 본다
    config: tuple[str, ...] = ()      # 설정 파일 이름 glob
    managed: tuple[str, ...] = ()     # dependencyManagement 패턴, BOM import 등
    parents: tuple[str, ...] = ()     # 부모 pom 패턴


# 규칙 표. 결과도 이 순서로 나온다. import 접두어는 패키지 경계에서만 비교해서 짧게 써도 된다
LIBRARY_RULES: tuple[LibraryRule, ...] = (
    LibraryRule("lombok", ("org.projectlombok:lombok",), ("lombok",)),
    LibraryRule("mapstruct", ("org.mapstruct:mapstruct", "org.mapstruct:mapstruct-*"), ("org.mapstruct",)),
    LibraryRule("querydsl", ("com.querydsl:*", "com.mysema.querydsl:*"), ("com.querydsl", "com.mysema.query")),
    LibraryRule("spring-security", ("org.springframework.security:*", f"{_B}:spring-boot-starter-security*"),
                ("org.springframework.security",)),
    LibraryRule("spring-data-jpa", ("org.springframework.data:spring-data-jpa", f"{_B}:spring-boot-starter-data-jpa"),
                ("org.springframework.data.jpa",)),
    LibraryRule("spring-data-redis", ("org.springframework.data:spring-data-redis",
                                      f"{_B}:spring-boot-starter-data-redis*"), ("org.springframework.data.redis",)),
    LibraryRule("spring-kafka", ("org.springframework.kafka:*", f"{_B}:spring-boot-starter-kafka"),
                ("org.springframework.kafka",)),
    LibraryRule("spring-batch", ("org.springframework.batch:*", f"{_B}:spring-boot-starter-batch*"),
                ("org.springframework.batch",)),
    LibraryRule("spring-cloud", ("org.springframework.cloud:*",), ("org.springframework.cloud",)),
    LibraryRule("hibernate", ("org.hibernate:hibernate-core", "org.hibernate:hibernate-entitymanager",
                              "org.hibernate.orm:*"), ("org.hibernate.annotations", "org.hibernate.Session",
                                                       "org.hibernate.SessionFactory", "org.hibernate.query",
                                                       "org.hibernate.type", "org.hibernate.cfg")),
    LibraryRule("mybatis", ("org.mybatis:*", "org.mybatis.spring.boot:*"), ("org.apache.ibatis", "org.mybatis")),
    LibraryRule("jpa", ("javax.persistence:*", "jakarta.persistence:*", "org.hibernate.javax.persistence:*",
                        f"{_B}:spring-boot-starter-data-jpa"), ("javax.persistence", "jakarta.persistence")),
    LibraryRule("springdoc", ("org.springdoc:*",), ("org.springdoc",)),
    LibraryRule("springfox", ("io.springfox:*",), ("springfox",)),
    LibraryRule("feign", ("io.github.openfeign:*", "com.netflix.feign:*",
                          "org.springframework.cloud:spring-cloud-starter-openfeign"),
                ("feign", "org.springframework.cloud.openfeign")),
    LibraryRule("jackson", ("com.fasterxml.jackson.core:*", "com.fasterxml.jackson.*:*", "tools.jackson.*:*"),
                ("com.fasterxml.jackson", "tools.jackson")),
    LibraryRule("junit4", ("junit:junit", "org.junit.vintage:*"),
                ("org.junit.Test", "org.junit.Assert", "org.junit.Before", "org.junit.After", "org.junit.BeforeClass",
                 "org.junit.AfterClass", "org.junit.Ignore", "org.junit.Rule", "org.junit.ClassRule",
                 "org.junit.runner", "org.junit.rules", "org.junit.Assume", "org.junit.experimental")),
    LibraryRule("junit5", ("org.junit.jupiter:*", "org.junit:junit-bom", "org.junit.platform:*"),
                ("org.junit.jupiter", "org.junit.platform")),
    LibraryRule("mockito", ("org.mockito:*",), ("org.mockito",)),
    LibraryRule("testcontainers", ("org.testcontainers:*",), ("org.testcontainers",)),
    LibraryRule("log4j2", ("org.apache.logging.log4j:*", f"{_B}:spring-boot-starter-log4j2"),
                ("org.apache.logging.log4j",), config=("log4j2*.xml",)),
    LibraryRule("logback", ("ch.qos.logback:*",), ("ch.qos.logback",), config=("logback*.xml",)),
    LibraryRule("javax-servlet", ("javax.servlet:javax.servlet-api", "javax.servlet:servlet-api"), ("javax.servlet",)),
    LibraryRule("jakarta-servlet", ("jakarta.servlet:*",), ("jakarta.servlet",)),
    LibraryRule("jsp", ("javax.servlet.jsp:*", "jakarta.servlet.jsp:*", "javax.servlet:jstl", "jstl:jstl",
                        "jakarta.servlet.jsp.jstl:*", "org.apache.tomcat.embed:tomcat-embed-jasper"),
                ("javax.servlet.jsp", "jakarta.servlet.jsp"), jsp=True),
    LibraryRule("jaxb", ("javax.xml.bind:*", "jakarta.xml.bind:*", "org.glassfish.jaxb:*", "com.sun.xml.bind:*"),
                ("javax.xml.bind", "jakarta.xml.bind")),
    LibraryRule("javax-annotation", ("javax.annotation:javax.annotation-api", "javax.annotation:jsr250-api"),
                ("javax.annotation.PostConstruct", "javax.annotation.PreDestroy", "javax.annotation.Resource",
                 "javax.annotation.Resources", "javax.annotation.Priority", "javax.annotation.ManagedBean",
                 "javax.annotation.Generated", "javax.annotation.security", "javax.annotation.sql")),
    LibraryRule("validation", ("javax.validation:*", "jakarta.validation:*", "org.hibernate.validator:*",
                               "org.hibernate:hibernate-validator", f"{_B}:spring-boot-starter-validation"),
                ("javax.validation", "jakarta.validation")),
    LibraryRule("thymeleaf", ("org.thymeleaf:*", f"{_B}:spring-boot-starter-thymeleaf"), ("org.thymeleaf",)),
    LibraryRule("flyway", ("org.flywaydb:*",), ("org.flywaydb",)),
    LibraryRule("liquibase", ("org.liquibase:*",), ("liquibase",)),
    LibraryRule("undertow", ("io.undertow:*", f"{_B}:spring-boot-starter-undertow"), ("io.undertow",)),
    LibraryRule("jetty", ("org.eclipse.jetty:*", f"{_B}:spring-boot-starter-jetty"), ("org.eclipse.jetty",)),
    LibraryRule("tomcat-embed", ("org.apache.tomcat.embed:tomcat-embed-core", f"{_B}:spring-boot-starter-tomcat",
                                 f"{_B}:spring-boot-starter-web"), ("org.apache.catalina",)),
    LibraryRule("spring-webflux", ("org.springframework:spring-webflux", f"{_B}:spring-boot-starter-webflux"),
                ("org.springframework.web.reactive",)),
    LibraryRule("spring-webmvc", ("org.springframework:spring-webmvc", f"{_B}:spring-boot-starter-web",
                                  f"{_B}:spring-boot-starter-webmvc"), ("org.springframework.web.servlet",)),
    LibraryRule("spring-context", ("org.springframework:spring-context",), ("org.springframework.context",)),
    LibraryRule("spring-retry", ("org.springframework.retry:*",), ("org.springframework.retry",)),
    LibraryRule("elasticsearch", ("org.elasticsearch:*", "org.elasticsearch.client:*", "co.elastic.clients:*",
                                  "org.springframework.data:spring-data-elasticsearch",
                                  f"{_B}:spring-boot-starter-data-elasticsearch"), ("org.elasticsearch", "co.elastic")),
    LibraryRule("mongodb", ("org.mongodb:*", "org.springframework.data:spring-data-mongodb",
                            f"{_B}:spring-boot-starter-data-mongodb*"),
                ("com.mongodb", "org.springframework.data.mongodb")),
    # ---- 권고용 규칙: EOL이거나 Boot 4 BOM이 관리하지 않는 라이브러리. 안내만 하고 차단하지 않는다
    LibraryRule("sleuth", (f"{_SC}:spring-cloud-starter-sleuth", f"{_SC}:spring-cloud-sleuth-*"),
                ("org.springframework.cloud.sleuth",)),
    LibraryRule("hystrix", (f"{_SC}:spring-cloud-starter-netflix-hystrix*", f"{_SC}:spring-cloud-starter-hystrix*",
                            f"{_SC}:spring-cloud-netflix-hystrix*", "com.netflix.hystrix:*"),
                ("com.netflix.hystrix",)),
    LibraryRule("ribbon", (f"{_SC}:spring-cloud-starter-netflix-ribbon", f"{_SC}:spring-cloud-starter-ribbon",
                           "com.netflix.ribbon:*"),
                ("com.netflix.loadbalancer", "com.netflix.client", "org.springframework.cloud.netflix.ribbon")),
    LibraryRule("zuul", (f"{_SC}:spring-cloud-starter-netflix-zuul", f"{_SC}:spring-cloud-starter-zuul",
                         "com.netflix.zuul:*"), ("com.netflix.zuul", "org.springframework.cloud.netflix.zuul")),
    LibraryRule("netflix-feign", ("com.netflix.feign:*", f"{_SC}:spring-cloud-starter-feign"),
                ("com.netflix.feign",)),
    LibraryRule("spring-cloud-bom", managed=(f"{_SC}:spring-cloud-dependencies",),
                parents=(f"{_SC}:spring-cloud-starter-parent",)),
    LibraryRule("swagger-v1", ("io.swagger:swagger-annotations", "io.swagger:swagger-models",
                               "io.swagger:swagger-core"), ("io.swagger.annotations", "io.swagger.models")),
    LibraryRule("spring-security-oauth2-legacy", ("org.springframework.security.oauth:*",
                                                  "org.springframework.security.oauth.boot:*"),
                ("org.springframework.security.oauth2.config.annotation",
                 "org.springframework.security.oauth2.provider", "org.springframework.security.oauth2.common")),
    LibraryRule("log4j1", ("log4j:log4j",), ("org.apache.log4j",)),
    LibraryRule("commons-lang2", ("commons-lang:commons-lang",), ("org.apache.commons.lang",)),
    LibraryRule("httpclient4", ("org.apache.httpcomponents:httpclient", "org.apache.httpcomponents:httpmime",
                                "org.apache.httpcomponents:fluent-hc"),
                ("org.apache.http.client", "org.apache.http.impl.client", "org.apache.http.HttpResponse")),
    # JDK 내부 패키지나 JDK에서 빠진 패키지. import만 본다
    LibraryRule("jdk-removed-apis", (), ("sun.misc", "sun.security", "com.sun.image", "javax.activation")),
    # javax.swing.tree는 JDK 9에서 제네릭으로 바뀌었다. 이전 코드는 javac 21에서 추론 오류가 난다. import만 본다
    # javac 21: "inference variable T has incompatible equality constraints"
    LibraryRule("jdk-generified-apis", (), ("javax.swing.tree",)),
    # iBATIS 2는 코드로 고치기 어려운 경우가 많다. 찾아서 인수인계에 남긴다
    # MyBatis 3과 달리 com.ibatis import와 ibatis-* 아티팩트로 본다. 스캔 결과에 XML 내용이 없어 빈 설정은 보지 않는다
    LibraryRule("ibatis2", ("org.apache.ibatis:ibatis-sqlmap", "org.apache.ibatis:ibatis-core",
                            "org.apache.ibatis:ibatis*", "ibatis:ibatis*", "com.ibatis:*"),
                ("com.ibatis", "org.springframework.orm.ibatis")),
    # hibernate-core 6.x까지는 따라오던 jar라 pom에 없어도 컴파일됐다. Hibernate ORM 7에서 빠져 패키지 없음 오류가 난다
    # 같은 import로 `hibernate` 규칙도 맞는다. 이 규칙은 빠진 jar 이름을 알려 주려고 둔다
    LibraryRule("hibernate-commons-annotations", ("org.hibernate.common:hibernate-commons-annotations",
                                                  "org.hibernate:hibernate-commons-annotations"),
                ("org.hibernate.annotations.common",)),
    # ---- 오래된 라이브러리 규칙: advisories.yml과 knowledge/notes/era-libraries.md에 같은 이름의 권고가 있다
    # import는 패키지 경계로 비교한다. 그래서 collections는 collections4에, dbcp는 dbcp2에, axis는 axis2에 맞지 않는다
    # hibernate-template은 import만 본다. spring-orm은 JPA 프로젝트마다 있다. HibernateTemplate은 Spring 7에서 빠졌다
    LibraryRule("hibernate-template", (), ("org.springframework.orm.hibernate3", "org.springframework.orm.hibernate4",
                                           "org.springframework.orm.hibernate5")),
    LibraryRule("commons-fileupload", ("commons-fileupload:commons-fileupload",),
                ("org.apache.commons.fileupload", "org.springframework.web.multipart.commons")),
    LibraryRule("jackson1", ("org.codehaus.jackson:*",), ("org.codehaus.jackson",)),
    LibraryRule("tiles", ("org.apache.tiles:*",), ("org.apache.tiles", "org.springframework.web.servlet.view.tiles2",
                                                   "org.springframework.web.servlet.view.tiles3")),
    LibraryRule("velocity", ("org.apache.velocity:velocity", "org.apache.velocity:velocity-tools",
                             "org.apache.velocity.tools:velocity-tools-view*"),
                ("org.apache.velocity", "org.springframework.web.servlet.view.velocity",
                 "org.springframework.ui.velocity")),
    LibraryRule("axis1", ("org.apache.axis:axis", "org.apache.axis:axis-jaxrpc", "org.apache.axis:axis-saaj",
                          "axis:axis", "axis:axis-jaxrpc"), ("org.apache.axis", "javax.xml.rpc")),
    LibraryRule("commons-httpclient3", ("commons-httpclient:commons-httpclient",), ("org.apache.commons.httpclient",)),
    LibraryRule("commons-collections3", ("commons-collections:commons-collections",),
                ("org.apache.commons.collections",)),
    LibraryRule("ehcache2", ("net.sf.ehcache:*",), ("net.sf.ehcache", "org.springframework.cache.ehcache")),
    LibraryRule("json-lib", ("net.sf.json-lib:json-lib",), ("net.sf.json",)),
    LibraryRule("jjwt", ("io.jsonwebtoken:*",), ("io.jsonwebtoken",)),
    LibraryRule("poi", ("org.apache.poi:*",), ("org.apache.poi",)),
    LibraryRule("struts2", ("org.apache.struts:struts2-*", "org.apache.struts.xwork:*"),
                ("org.apache.struts2", "com.opensymphony.xwork2")),
    LibraryRule("egovframe", ("egovframework.rte:*", "org.egovframe.rte:*"), ("egovframework.rte", "org.egovframe.rte")),
    LibraryRule("dbcp1", ("commons-dbcp:commons-dbcp",), ("org.apache.commons.dbcp",)),
    # QueryDSL의 Hibernate 전용 API는 Hibernate 6, 7과 맞지 않는다. JPAQueryFactory는 괜찮아서 import만 본다
    LibraryRule("querydsl-hibernate-native", (), ("com.querydsl.jpa.hibernate",)),
)

LIBRARY_NAMES: tuple[str, ...] = tuple(r.name for r in LIBRARY_RULES)


def _dep_matches(pattern: str, g: GAV) -> bool:
    """``"group:artifact-glob"`` 패턴이 좌표에 맞는지 확인한다. 대소문자를 구분한다."""
    pg, _, pa = pattern.partition(":")
    return fnmatch.fnmatchcase(g.group_id, pg) and fnmatch.fnmatchcase(g.artifact_id, pa)


def _import_matches(prefix: str, imp: str) -> bool:
    """import가 접두어와 같거나 ``접두어.``로 시작하는지 확인한다.
    ``collections``가 ``collections4``에 맞지 않도록 패키지 경계에서만 비교한다."""
    return imp == prefix or imp.startswith(prefix + ".")


def _concrete(v: Optional[str]) -> Optional[str]:
    """버전을 반환한다. 풀리지 않은 ``${...}``가 남아 있으면 None을 반환한다."""
    return v if v and "${" not in v else None


def detect_libraries(poms: Iterable[PomInfo], scan: Optional[ScanResult] = None,
                     imports: Optional[Counter] = None) -> list[LibraryUsage]:
    """프로젝트가 쓰는 라이브러리를 근거와 함께 반환한다. 근거가 하나도 없는 규칙은 빠진다.
    버전은 처음 맞은 의존성의 버전이다. 의존성에 버전이 없으면 dependencyManagement의 버전을 쓴다.
    ``scan``과 ``imports``가 모두 없으면 pom만 본다."""
    poms = list(poms)
    deps = [d for p in poms for d in p.dependencies]
    managed = [d for p in poms for d in p.dependency_management]
    if imports is None:
        imports = scan.import_counts() if scan is not None else Counter()
    import_list = list(imports.keys())
    out: list[LibraryUsage] = []
    for rule in LIBRARY_RULES:
        evidence: list[str] = []
        version: Optional[str] = None
        # 규칙 패턴 순서대로 처음 맞은 의존성의 버전을 쓴다
        for pattern in rule.deps:
            for d in deps:
                if _dep_matches(pattern, d):
                    ev = f"pom:{d.coordinates}"
                    if ev not in evidence:
                        evidence.append(ev)
                    if version is None:
                        version = _concrete(d.version)
        # 의존성에 버전이 없으면 부모나 BOM이 관리하는 버전이다. dependencyManagement에서 가져온다
        if evidence and version is None:
            for pattern in rule.deps:
                for d in managed:
                    if _dep_matches(pattern, d) and _concrete(d.version):
                        version = d.version
                        break
                if version:
                    break
        for pattern in rule.managed:
            for d in managed:
                if _dep_matches(pattern, d):
                    ev = f"pom-managed:{d.coordinates}"
                    if ev not in evidence:
                        evidence.append(ev)
                    if version is None:
                        version = _concrete(d.version)
        for pattern in rule.parents:
            for p in poms:
                if p.parent is not None and _dep_matches(pattern, p.parent):
                    ev = f"pom-parent:{p.parent.coordinates}"
                    if ev not in evidence:
                        evidence.append(ev)
                    if version is None:
                        version = _concrete(p.parent.version)
        for prefix in rule.imports:
            if any(_import_matches(prefix, i) for i in import_list):
                evidence.append(f"import:{prefix}")
        if scan is not None:
            if rule.jsp and scan.jsp_paths:
                evidence.append(f"file:*.jsp({len(scan.jsp_paths)})")
            for pat in rule.config:
                hits = [c for c in scan.config_files if fnmatch.fnmatch(c.rsplit("/", 1)[-1], pat)]
                if hits:
                    evidence.append(f"file:{hits[0]}")
        if evidence:
            out.append(LibraryUsage(name=rule.name, version=version, evidence=evidence))
    return out
