"""Table-driven library detection → `LibraryUsage` list.

The library *names* are a contract (planner, triage handlers, notes selection and KB tags rely on them):
lombok, mapstruct, querydsl, spring-security, spring-data-jpa, spring-data-redis, spring-kafka, spring-batch,
spring-cloud, hibernate, mybatis, jpa, springdoc, springfox, feign, jackson, junit4, junit5, mockito,
testcontainers, log4j2, logback, javax-servlet, jakarta-servlet, jsp, jaxb, javax-annotation, validation,
thymeleaf, flyway, liquibase, undertow, jetty, tomcat-embed, spring-webflux, spring-webmvc, spring-context,
spring-retry, elasticsearch, mongodb,
plus the library-advisory rules (knowledge/libraries/advisories.yml, docs/ADVISORIES.md): sleuth, hystrix, ribbon,
zuul, netflix-feign, spring-cloud-bom, swagger-v1, spring-security-oauth2-legacy, log4j1, commons-lang2, httpclient4,
jdk-removed-apis (imports of JDK-internal / removed packages: sun.misc, sun.security, com.sun.image, javax.activation;
JAXB `javax.xml.bind` stays on the `jaxb` rule), ibatis2 (iBATIS 2: `com.ibatis` / `org.springframework.orm.ibatis`
imports, `org.apache.ibatis:ibatis-*` artifacts; `mybatis` = MyBatis 3 only), hibernate-commons-annotations
(`org.hibernate.annotations.common.*` imports / the HCANN artifact — gone from Hibernate ORM 7),
and the 2026-10-22 era-library rules (advisories of the same name unless noted): hibernate-template (Spring's
`org.springframework.orm.hibernate3|4|5` imports; import-only — spring-orm itself is too broad), commons-fileupload,
jackson1, tiles, velocity, axis1 (`org.apache.axis` / `javax.xml.rpc`; not `org.apache.axis2`), commons-httpclient3,
commons-collections3 (not `collections4`), ehcache2, json-lib, jjwt (advisory jjwt-legacy, < 0.10), poi (advisory
poi-legacy, < 5.0), struts2 (< 7.0), egovframe, dbcp1 (advisory commons-dbcp1; not `dbcp2`), querydsl-hibernate-native
(`com.querydsl.jpa.hibernate.*` imports only).

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
    name: str
    deps: tuple[str, ...] = ()        # "group:artifact-glob" (fnmatch on both parts)
    imports: tuple[str, ...] = ()     # import prefixes (matched on segment boundary)
    jsp: bool = False                 # *.jsp files count as evidence
    config: tuple[str, ...] = ()      # config file basename globs
    managed: tuple[str, ...] = ()     # dependencyManagement "group:artifact-glob" patterns (BOM imports …)
    parents: tuple[str, ...] = ()     # parent "group:artifact-glob" patterns


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
    # ---- library advisories (EOL / no longer managed by the Boot 4 BOM; knowledge/libraries/advisories.yml)
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
    # JDK-internal / removed-from-the-JDK packages (pre-Java-8 code; advisory jdk-removed-apis). Import-only.
    LibraryRule("jdk-removed-apis", (), ("sun.misc", "sun.security", "com.sun.image", "javax.activation")),
    # JDK 9 generified javax.swing.tree (DefaultMutableTreeNode.children() → Enumeration<TreeNode>): pre-Java-9 code
    # stops compiling on javac 21 with "inference variable T has incompatible equality constraints" (advisory
    # jdk-generified-apis, round 9c). Import-only.
    LibraryRule("jdk-generified-apis", (), ("javax.swing.tree",)),
    # iBATIS 2 (round 9; advisory ibatis2): Spring 4.0 removed org.springframework.orm.ibatis. Distinct from `mybatis`
    # (MyBatis 3 = org.mybatis:* artifacts, org.apache.ibatis.* imports); iBATIS 2 = com.ibatis.* imports and the
    # org.apache.ibatis:ibatis-* artifacts. Pom + imports only: Spring XML bean classes (SqlMapClientFactoryBean)
    # are not scanned — the ScanResult keeps XML paths, not their content.
    LibraryRule("ibatis2", ("org.apache.ibatis:ibatis-sqlmap", "org.apache.ibatis:ibatis-core",
                            "org.apache.ibatis:ibatis*", "ibatis:ibatis*", "com.ibatis:*"),
                ("com.ibatis", "org.springframework.orm.ibatis")),
    # Hibernate Commons Annotations (2026-10-22, company run; advisory hibernate-commons-annotations): hibernate-core
    # 3.x–6.x pulled it in transitively, so `org.hibernate.annotations.common.*` imports (LoggerFactory / Log, XClass …)
    # compiled without a pom entry; Hibernate ORM 7 (Boot 4) dropped it → "package … does not exist". The generic
    # `hibernate` rule still fires on the same import (org.hibernate.annotations); this one names the lost jar.
    LibraryRule("hibernate-commons-annotations", ("org.hibernate.common:hibernate-commons-annotations",
                                                  "org.hibernate:hibernate-commons-annotations"),
                ("org.hibernate.annotations.common",)),
    # ---- 2026-10-22 era libraries (out/research/era-libraries-*.yml → advisories.yml, knowledge/notes/era-libraries.md).
    # Import prefixes match on a segment boundary, so `org.apache.commons.collections` never matches `collections4`,
    # `org.apache.commons.dbcp` never `dbcp2`, `org.apache.axis` never `org.apache.axis2`, `org.apache.commons.fileupload`
    # never `fileupload2`. Spring 7 removed HibernateTemplate / HibernateDaoSupport / orm.hibernate5 (import-only rule:
    # org.springframework:spring-orm is in every JPA project).
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
    # QueryDSL's Hibernate-native API does not link against Hibernate 6/7 (advisory querydsl-hibernate-native); plain
    # JPAQueryFactory users stay on the Boot-managed com.querydsl line, so this rule is import-only.
    LibraryRule("querydsl-hibernate-native", (), ("com.querydsl.jpa.hibernate",)),
)

LIBRARY_NAMES: tuple[str, ...] = tuple(r.name for r in LIBRARY_RULES)


def _dep_matches(pattern: str, g: GAV) -> bool:
    pg, _, pa = pattern.partition(":")
    return fnmatch.fnmatchcase(g.group_id, pg) and fnmatch.fnmatchcase(g.artifact_id, pa)


def _import_matches(prefix: str, imp: str) -> bool:
    return imp == prefix or imp.startswith(prefix + ".")


def _concrete(v: Optional[str]) -> Optional[str]:
    return v if v and "${" not in v else None


def detect_libraries(poms: Iterable[PomInfo], scan: Optional[ScanResult] = None,
                     imports: Optional[Counter] = None) -> list[LibraryUsage]:
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
        # dependencies: the first rule pattern that matches (in rule order) decides the version
        for pattern in rule.deps:
            for d in deps:
                if _dep_matches(pattern, d):
                    ev = f"pom:{d.coordinates}"
                    if ev not in evidence:
                        evidence.append(ev)
                    if version is None:
                        version = _concrete(d.version)
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
