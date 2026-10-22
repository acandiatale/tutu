from __future__ import annotations

from pathlib import Path

from sbmig.analyze import LIBRARY_NAMES, PathView, detect_libraries, parse_poms, scan_sources
from tests.analyze.helpers import dep, make_boot27, make_servlet, pom, write, write_java

EXPECTED = {"lombok", "mapstruct", "querydsl", "spring-security", "spring-data-jpa", "spring-data-redis",
            "spring-kafka", "spring-batch", "spring-cloud", "hibernate", "mybatis", "jpa", "springdoc", "springfox",
            "feign", "jackson", "junit4", "junit5", "mockito", "testcontainers", "log4j2", "logback",
            "javax-servlet", "jakarta-servlet", "jsp", "jaxb", "javax-annotation", "validation", "thymeleaf",
            "flyway", "liquibase", "undertow", "jetty", "tomcat-embed", "spring-webflux", "spring-webmvc",
            "spring-context", "spring-retry", "elasticsearch", "mongodb",
            # library advisories (knowledge/libraries/advisories.yml)
            "sleuth", "hystrix", "ribbon", "zuul", "netflix-feign", "spring-cloud-bom", "swagger-v1",
            "spring-security-oauth2-legacy", "log4j1", "commons-lang2", "httpclient4", "jdk-removed-apis", "ibatis2",
            "jdk-generified-apis", "hibernate-commons-annotations"}


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


# ---------------------------------------------------------------------------- advisory rules
SC = "org.springframework.cloud"


def test_advisory_rules_from_deps_and_imports(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x", deps=(
        dep(SC, "spring-cloud-starter-sleuth"), dep(SC, "spring-cloud-starter-netflix-hystrix", "2.2.10.RELEASE"),
        dep(SC, "spring-cloud-starter-netflix-ribbon"), dep("com.netflix.zuul", "zuul-core", "1.3.1"),
        dep(SC, "spring-cloud-starter-feign", "1.4.7.RELEASE"), dep("io.swagger", "swagger-annotations", "1.6.2"),
        dep("org.springframework.security.oauth", "spring-security-oauth2", "2.5.2.RELEASE"),
        dep("log4j", "log4j", "1.2.17"), dep("commons-lang", "commons-lang", "2.6"),
        dep("org.apache.httpcomponents", "httpclient", "4.5.14"))))
    write_java(tmp_path, "", "com.a", "A", imports=(
        "com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand", "org.apache.http.HttpResponse",
        "org.springframework.security.oauth2.provider.token.TokenStore", "org.apache.log4j.Logger"))
    libs = _libs(tmp_path)
    assert libs["hystrix"].version == "2.2.10.RELEASE"
    assert "import:com.netflix.hystrix" in libs["hystrix"].evidence
    assert libs["sleuth"].version is None and "ribbon" in libs and libs["zuul"].version == "1.3.1"
    assert libs["netflix-feign"].version == "1.4.7.RELEASE"
    assert "feign" not in libs                  # the existing feign rule is unchanged (openfeign / com.netflix.feign)
    assert libs["swagger-v1"].version == "1.6.2"
    assert "import:org.springframework.security.oauth2.provider" in libs["spring-security-oauth2-legacy"].evidence
    assert libs["log4j1"].version == "1.2.17" and libs["commons-lang2"].version == "2.6"
    assert "import:org.apache.http.HttpResponse" in libs["httpclient4"].evidence
    assert "spring-cloud" in libs


def test_commons_lang2_import_does_not_match_lang3(tmp_path: Path) -> None:
    write_java(tmp_path, "", "com.a", "A", imports=("org.apache.commons.lang3.StringUtils",))
    assert "commons-lang2" not in _libs(tmp_path)
    write_java(tmp_path, "", "com.a", "B", imports=("org.apache.commons.lang.StringUtils",))
    assert _libs(tmp_path)["commons-lang2"].evidence == ["import:org.apache.commons.lang"]


def test_spring_cloud_bom_from_managed_and_parent(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x", props={"spring-cloud.version": "Hoxton.SR12"}, managed=(
        dep(SC, "spring-cloud-dependencies", "${spring-cloud.version}", scope="import", type_="pom"),)))
    lib = _libs(tmp_path)["spring-cloud-bom"]
    assert lib.version == "Hoxton.SR12"
    assert lib.evidence == ["pom-managed:org.springframework.cloud:spring-cloud-dependencies:Hoxton.SR12"]
    other = tmp_path / "p"
    write(other, "pom.xml", pom("y", parent=(SC, "spring-cloud-starter-parent", "2021.0.9")))
    lib = _libs(other)["spring-cloud-bom"]
    assert lib.version == "2021.0.9"
    assert lib.evidence == ["pom-parent:org.springframework.cloud:spring-cloud-starter-parent:2021.0.9"]
    plain = tmp_path / "plain"
    write(plain, "pom.xml", pom("z", deps=(dep(SC, "spring-cloud-starter-openfeign"),)))
    assert "spring-cloud-bom" not in _libs(plain)


def test_jdk_removed_apis_rule_is_import_only(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.a", "A", imports=("sun.misc.BASE64Encoder", "javax.activation.DataHandler",
                                                     "javax.xml.bind.DatatypeConverter"))
    libs = _libs(tmp_path)
    assert libs["jdk-removed-apis"].evidence == ["import:sun.misc", "import:javax.activation"]
    assert libs["jdk-removed-apis"].version is None
    assert "jaxb" in libs                       # JAXB stays on its own rule
    write_java(tmp_path, "", "com.a", "B", imports=("sun.misc.Unsafe",))
    assert "import:sun.misc" in _libs(tmp_path)["jdk-removed-apis"].evidence


def test_jdk_removed_apis_segment_boundary(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.a", "A", imports=("sun.miscellaneous.Foo", "com.sun.imagery.X"))
    assert "jdk-removed-apis" not in _libs(tmp_path)


def test_hibernate_commons_annotations_rule(tmp_path: Path) -> None:
    """HCANN (gone in Hibernate 7): detected from `org.hibernate.annotations.common` imports or the artifact; the
    generic `hibernate` rule fires alongside, a plain `org.hibernate.annotations` import does not trip it."""
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.a", "A", imports=("org.hibernate.annotations.common.util.impl.LoggerFactory",))
    libs = _libs(tmp_path)
    assert libs["hibernate-commons-annotations"].evidence == ["import:org.hibernate.annotations.common"]
    assert "hibernate" in libs
    other = tmp_path / "other"
    write(other, "pom.xml", pom("y"))
    write_java(other, "", "com.b", "B", imports=("org.hibernate.annotations.Type",))
    assert "hibernate-commons-annotations" not in _libs(other) and "hibernate" in _libs(other)
    declared = tmp_path / "declared"
    write(declared, "pom.xml", pom("z", deps=(dep("org.hibernate.common", "hibernate-commons-annotations", "6.0.6.Final"),)))
    assert _libs(declared)["hibernate-commons-annotations"].evidence == \
        ["pom:org.hibernate.common:hibernate-commons-annotations:6.0.6.Final"]


def test_ibatis2_and_mybatis_stay_distinct(tmp_path: Path) -> None:
    ib = tmp_path / "ib"
    write(ib, "pom.xml", pom("legacy", deps=(dep("org.apache.ibatis", "ibatis-sqlmap", "2.3.4.726"),
                                             dep("org.springframework", "spring-orm", "3.2.18.RELEASE"))))
    write_java(ib, "", "com.acme.dao", "OrderDao", imports=("com.ibatis.sqlmap.client.SqlMapClient",
                                                            "org.springframework.orm.ibatis.support.SqlMapClientDaoSupport"))
    libs = _libs(ib)
    assert "ibatis2" in libs and "mybatis" not in libs
    assert libs["ibatis2"].version == "2.3.4.726"
    assert libs["ibatis2"].evidence == ["pom:org.apache.ibatis:ibatis-sqlmap:2.3.4.726", "import:com.ibatis",
                                        "import:org.springframework.orm.ibatis"]
    mb = tmp_path / "mb"
    write(mb, "pom.xml", pom("modern", deps=(dep("org.mybatis", "mybatis", "3.5.16"),
                                             dep("org.mybatis", "mybatis-spring", "2.1.2"))))
    write_java(mb, "", "com.acme.dao", "OrderMapper", imports=("org.apache.ibatis.annotations.Mapper",
                                                               "org.apache.ibatis.session.SqlSession"))
    libs = _libs(mb)
    assert "mybatis" in libs and "ibatis2" not in libs


def test_ibatis2_old_group_ids_and_import_only(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("x", deps=(dep("com.ibatis", "ibatis2-sqlmap", "2.1.7.597"),)))
    assert _libs(tmp_path)["ibatis2"].evidence == ["pom:com.ibatis:ibatis2-sqlmap:2.1.7.597"]
    other = tmp_path / "o"
    write(other, "pom.xml", pom("y", deps=(dep("ibatis", "ibatis-sqlmap", "2.0"),)))
    write_java(other, "", "com.acme", "D", imports=("com.ibatis.common.resources.Resources",))
    assert set(_libs(other)["ibatis2"].evidence) == {"pom:ibatis:ibatis-sqlmap:2.0", "import:com.ibatis"}
