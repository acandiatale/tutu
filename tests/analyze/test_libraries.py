from __future__ import annotations

from pathlib import Path

from sbmig.analyze import LIBRARY_NAMES, PathView, detect_libraries, parse_poms, scan_sources
from tests.analyze.helpers import dep, make_boot27, make_servlet, pom, write, write_java

EXPECTED = {"lombok", "mapstruct", "querydsl", "spring-security", "spring-data-jpa", "spring-data-redis",
            "spring-kafka", "spring-batch", "spring-cloud", "hibernate", "mybatis", "jpa", "springdoc", "springfox",
            "feign", "jackson", "junit4", "junit5", "mockito", "testcontainers", "log4j2", "logback",
            "javax-servlet", "jakarta-servlet", "jsp", "jaxb", "javax-annotation", "validation", "thymeleaf",
            "flyway", "liquibase", "undertow", "jetty", "tomcat-embed", "spring-webflux", "spring-webmvc",
            "spring-context", "spring-retry", "elasticsearch", "mongodb"}


def test_names_are_the_contract() -> None:
    assert set(LIBRARY_NAMES) == EXPECTED and len(LIBRARY_NAMES) == len(EXPECTED)


def _libs(root: Path):
    ws = PathView(root)
    poms, _ = parse_poms(ws)
    return {lib.name: lib for lib in detect_libraries(poms, scan_sources(ws))}


def test_boot27_libraries(tmp_path: Path) -> None:
    make_boot27(tmp_path)
    write(tmp_path, "src/main/resources/logback.xml", "<configuration/>")
    libs = _libs(tmp_path)
    assert libs["lombok"].version == "1.18.24"
    assert "pom:org.projectlombok:lombok:1.18.24" in libs["lombok"].evidence
    assert "import:lombok" in libs["lombok"].evidence
    assert libs["junit4"].version == "4.13.2"
    assert "import:javax.persistence" in libs["jpa"].evidence
    assert {"spring-data-jpa", "spring-webmvc", "tomcat-embed", "javax-servlet", "logback"} <= set(libs)
    assert "file:src/main/resources/logback.xml" in libs["logback"].evidence
    assert "junit5" not in libs and "spring-security" not in libs


def test_version_from_dependency_management_and_jsp(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x", deps=(dep("org.mapstruct", "mapstruct"), dep("com.querydsl", "querydsl-jpa")),
                                   managed=(dep("org.mapstruct", "mapstruct", "1.5.5.Final"),)))
    make_servlet(tmp_path / "web")
    libs = _libs(tmp_path)
    assert libs["mapstruct"].version == "1.5.5.Final"
    assert libs["querydsl"].version is None
    assert any(e.startswith("file:*.jsp") for e in libs["jsp"].evidence)


def test_import_only_and_junit_boundaries(tmp_path: Path) -> None:
    write_java(tmp_path, "", "com.a", "T", test=True, imports=("org.junit.jupiter.api.Test", "org.mockito.Mockito",
                                                               "javax.annotation.PostConstruct"))
    libs = _libs(tmp_path)
    assert "junit5" in libs and "junit4" not in libs and "mockito" in libs and "javax-annotation" in libs
    assert libs["junit5"].evidence == ["import:org.junit.jupiter"]
    # detect_libraries without a scan
    assert detect_libraries([]) == []
