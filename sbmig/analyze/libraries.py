"""Table-driven library detection → `LibraryUsage` list.

The library *names* are a contract (planner, triage handlers, notes selection and KB tags rely on them):
lombok, mapstruct, querydsl, spring-security, spring-data-jpa, spring-data-redis, spring-kafka, spring-batch,
spring-cloud, hibernate, mybatis, jpa, springdoc, springfox, feign, jackson, junit4, junit5, mockito,
testcontainers, log4j2, logback, javax-servlet, jakarta-servlet, jsp, jaxb, javax-annotation, validation,
thymeleaf, flyway, liquibase, undertow, jetty, tomcat-embed, spring-webflux, spring-webmvc, spring-context,
spring-retry, elasticsearch, mongodb.

Edge-case names added by the analyzer (not table-driven, see `sbmig.analyze.edges`): system-scope-jars, kotlin,
groovy, scala, codegen:<plugin artifactId> (protobuf-maven-plugin, jaxb2-maven-plugin, openapi-generator-maven-plugin,
maven-jaxb2-plugin, querydsl-maven-plugin), legacy-encoding (evidence = detected encodings).

Evidence strings: `pom:<g>:<a>[:<v>]`, `import:<prefix>`, `file:<what>`.
"""
from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from sbmig.core.models import GAV, LibraryUsage, PomInfo

from .source_scanner import ScanResult

_B = "org.springframework.boot"


@dataclass(frozen=True)
class LibraryRule:
    name: str
    deps: tuple[str, ...] = ()        # "group:artifact-glob" (fnmatch on both parts)
    imports: tuple[str, ...] = ()     # import prefixes (matched on segment boundary)
    jsp: bool = False                 # *.jsp files count as evidence
    config: tuple[str, ...] = ()      # config file basename globs


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
