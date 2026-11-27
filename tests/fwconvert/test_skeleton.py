"""generate_skeleton / check_options: poms, the application class, the Web MVC configuration and application.yml
(out/scratch/fwconvert/ROUND2.md section P)."""
from __future__ import annotations

import yaml
from lxml import etree

from sbmig.fwconvert.identity import derive_identity
from sbmig.fwconvert.model import ConvertOptions, DatasourceInfo, Layout
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.skeleton import MYBATIS_STARTER, check_options, generate_skeleton

RULES = load_rules("Example", {"naming": {"package_root": "com.example", "domains": ["d1"], "default_domain": "d1"}})
LAYOUT = Layout(derive_identity("sample-shop-clean.zip", RULES, domain="d1"))
NS = {"m": "http://maven.apache.org/POM/4.0.0"}
FOUND = DatasourceInfo(configured=True, vendor="postgresql", url="jdbc:postgresql://db.invalid:5432/app",
                       driver_class="org.postgresql.Driver", username="app", has_password=True,
                       source="applicationContext.xml")


def _options(**kw) -> ConvertOptions:
    base = {"source_name": "sample-shop-clean.zip", "domain": "d1", "boot_version": "4.1.0",
            "versions": {MYBATIS_STARTER: "4.1.0"},
            "jdbc_drivers": {"postgresql": "org.postgresql:postgresql", "oracle": "com.oracle.database.jdbc:ojdbc11:23.1"}}
    base.update(kw)
    return ConvertOptions(**base)


def _files(options=None, ds=None, tops=("css", "js")) -> dict[str, str]:
    made = generate_skeleton(LAYOUT, options or _options(), ds or DatasourceInfo(reason="not_found"), RULES, list(tops))
    return {f.path: f.text for f in made}


def _pom(text: str):
    return etree.fromstring(text.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True))


def _deps(root) -> list[tuple[str, str, str, str]]:
    out = []
    for d in root.findall("m:dependencies/m:dependency", NS):
        out.append(tuple(d.findtext(f"m:{k}", default="", namespaces=NS) for k in ("groupId", "artifactId",
                                                                                    "version", "scope")))
    return out


def test_the_root_pom_is_a_boot_project_with_four_modules():
    root = _pom(_files()["pom.xml"])
    assert root.findtext("m:parent/m:groupId", namespaces=NS) == "org.springframework.boot"
    assert root.findtext("m:parent/m:artifactId", namespaces=NS) == "spring-boot-starter-parent"
    assert root.findtext("m:parent/m:version", namespaces=NS) == "4.1.0"
    assert root.findtext("m:groupId", namespaces=NS) == "com.example.d1"
    assert root.findtext("m:artifactId", namespaces=NS) == "shopProject"
    assert root.findtext("m:version", namespaces=NS) == "1.0.0"
    assert root.findtext("m:packaging", namespaces=NS) == "pom"
    assert root.findtext("m:properties/m:java.version", namespaces=NS) == "21"
    assert sorted(m.text for m in root.findall("m:modules/m:module", NS)) == [
        "shopProject-boot", "shopProject-domain", "shopProject-service", "shopProject-store"]


def test_an_explicit_group_id_and_version_are_used():
    files = _files(_options(group_id="org.acme.app", version="2.0.0"))
    root = _pom(files["pom.xml"])
    assert root.findtext("m:groupId", namespaces=NS) == "org.acme.app"
    boot = _pom(files["shopProject-boot/pom.xml"])
    assert boot.findtext("m:parent/m:groupId", namespaces=NS) == "org.acme.app"
    assert boot.findtext("m:parent/m:version", namespaces=NS) == "2.0.0"


def test_module_poms_and_only_the_given_versions():
    files = _files(ds=FOUND)
    domain = _pom(files["shopProject-domain/pom.xml"])
    assert domain.findtext("m:packaging", namespaces=NS) == "jar"
    assert not any("spring" in a for _, a, _, _ in _deps(domain))
    store = _deps(_pom(files["shopProject-store/pom.xml"]))
    assert ("com.example.d1", "shopProject-domain", "${project.version}", "") in store
    assert ("org.mybatis.spring.boot", "mybatis-spring-boot-starter", "4.1.0", "") in store
    assert ("org.springframework", "spring-context", "", "") in store
    service = _deps(_pom(files["shopProject-service/pom.xml"]))
    assert {a for _, a, _, _ in service} == {"shopProject-domain", "spring-webmvc", "spring-tx", "slf4j-api"}
    boot = _pom(files["shopProject-boot/pom.xml"])
    assert boot.findtext("m:packaging", namespaces=NS) == "war"
    assert boot.findtext("m:build/m:finalName", namespaces=NS) == "shopProject"
    assert boot.findtext("m:build/m:plugins/m:plugin/m:artifactId", namespaces=NS) == "spring-boot-maven-plugin"
    deps = _deps(boot)
    names = [a for _, a, _, _ in deps]
    assert names[:3] == ["shopProject-service", "shopProject-store", "shopProject-domain"]
    for a in ("spring-boot-starter-webmvc", "spring-boot-starter-jdbc", "mybatis-spring-boot-starter",
              "tomcat-embed-jasper", "jakarta.servlet.jsp.jstl-api", "jakarta.servlet.jsp.jstl"):
        assert a in names
    assert ("org.postgresql", "postgresql", "", "runtime") in deps
    # a version is written only for the MyBatis starter and the internal modules: nothing else, never an empty one
    for text in files.values():
        assert "<version></version>" not in text
    versions = {(a, v) for _, a, v, _ in deps if v}
    assert versions == {("shopProject-service", "${project.version}"), ("shopProject-store", "${project.version}"),
                        ("shopProject-domain", "${project.version}"), ("mybatis-spring-boot-starter", "4.1.0")}


def test_a_driver_coordinate_with_a_version_and_a_vendor_without_one():
    oracle = DatasourceInfo(configured=True, vendor="oracle", url="jdbc:oracle:thin:@h:1521:X")
    deps = _deps(_pom(_files(ds=oracle)["shopProject-boot/pom.xml"]))
    assert ("com.oracle.database.jdbc", "ojdbc11", "23.1", "runtime") in deps
    tibero = DatasourceInfo(configured=True, vendor="tibero", url="jdbc:tibero:thin:@h:8629:X")
    names = [a for _, a, _, _ in _deps(_pom(_files(ds=tibero)["shopProject-boot/pom.xml"]))]
    assert not any("tibero" in n or "ojdbc" in n or "postgres" in n for n in names)
    # no datasource: no driver
    names = [a for _, a, _, _ in _deps(_pom(_files()["shopProject-boot/pom.xml"]))]
    assert "postgresql" not in names


def test_a_missing_or_empty_mybatis_version_writes_no_pom_and_says_so():
    for versions in ({}, {MYBATIS_STARTER: ""}, {MYBATIS_STARTER: "  "}):
        options = _options(versions=versions)
        errors = check_options(options, DatasourceInfo())
        assert len(errors) == 1 and MYBATIS_STARTER in errors[0]
        files = _files(options)
        assert not any(p.endswith("pom.xml") for p in files)
        assert "shopProject-boot/src/main/resources/application.yml" in files
    errors = check_options(_options(boot_version=""), DatasourceInfo())
    assert any("Spring Boot version" in e for e in errors)
    assert not any(p.endswith("pom.xml") for p in _files(_options(boot_version="")))
    empty_other = _options(versions={MYBATIS_STARTER: "4.1.0", "org.springframework:spring-tx": ""})
    assert any("spring-tx" in e for e in check_options(empty_other, DatasourceInfo()))
    assert check_options(_options(), FOUND) == []


def test_a_malformed_driver_coordinate_is_an_error_and_no_dependency():
    options = _options(jdbc_drivers={"postgresql": "not a coordinate"})
    errors = check_options(options, FOUND)
    assert len(errors) == 1 and "postgresql" in errors[0]
    files = _files(options, FOUND)
    assert "shopProject-boot/pom.xml" in files and "not a coordinate" not in files["shopProject-boot/pom.xml"]


def test_the_application_class_and_the_web_configuration():
    files = _files(tops=["js", "css", "images", "../x", "a/b", ""])
    app = files["shopProject-boot/src/main/java/com/example/d1/shopProject/ShopProjectApplication.java"]
    assert "package com.example.d1.shopProject;" in app
    assert "@SpringBootApplication" in app and "extends SpringBootServletInitializer" in app
    assert "import org.springframework.boot.web.servlet.support.SpringBootServletInitializer;" in app
    assert "public static void main(String[] args)" in app
    web = files["shopProject-boot/src/main/java/com/example/d1/shopProject/config/WebMvcConfig.java"]
    assert "package com.example.d1.shopProject.config;" in web
    assert 'registry.jsp("/WEB-INF/static/shopProject/", ".jsp");' in web
    handlers = [line.strip() for line in web.splitlines() if "addResourceHandler(" in line]
    assert handlers == ['registry.addResourceHandler("/css/**")', 'registry.addResourceHandler("/images/**")',
                        'registry.addResourceHandler("/js/**")']
    assert '.addResourceLocations("/WEB-INF/static/shopProject/css/")' in web
    assert "NoJspResourceResolver" in web and '".jsp"' in web


def test_no_static_folder_gives_no_resource_handler_and_no_unused_import():
    web = _files(tops=[])["shopProject-boot/src/main/java/com/example/d1/shopProject/config/WebMvcConfig.java"]
    assert "addResourceHandlers" not in web and "PathResourceResolver" not in web and "IOException" not in web


def test_application_yml_with_and_without_a_datasource():
    yml = yaml.safe_load(_files(ds=FOUND)["shopProject-boot/src/main/resources/application.yml"])
    assert yml["spring"]["servlet"]["encoding"]["charset"] == "UTF-8"
    assert yml["spring"]["servlet"]["encoding"]["force"] is True
    assert yml["mybatis"]["mapper-locations"] == "classpath:mybatis/mapper/*.xml"
    assert yml["mybatis"]["configuration"]["map-underscore-to-camel-case"] is True
    assert yml["spring"]["datasource"] == {"url": "jdbc:postgresql://db.invalid:5432/app",
                                           "driver-class-name": "org.postgresql.Driver", "username": "app",
                                           "password": "${SPRING_DATASOURCE_PASSWORD:}"}
    none = yaml.safe_load(_files()["shopProject-boot/src/main/resources/application.yml"])
    assert "datasource" not in none["spring"]
    odd = DatasourceInfo(configured=True, vendor="h2", url='jdbc:h2:mem:a"b: #c\n')
    got = yaml.safe_load(_files(ds=odd)["shopProject-boot/src/main/resources/application.yml"])
    assert got["spring"]["datasource"]["url"] == 'jdbc:h2:mem:a"b: #c\n'


def test_project_values_are_escaped_in_the_poms():
    files = _files(_options(group_id="a<b&c", version="1.0&2"))
    root = _pom(files["pom.xml"])
    assert root.findtext("m:groupId", namespaces=NS) == "a<b&c"
    assert root.findtext("m:version", namespaces=NS) == "1.0&2"


def test_the_same_input_gives_the_same_files():
    assert _files(ds=FOUND, tops=["js", "css"]) == _files(ds=FOUND, tops=["css", "js", "css"])


def test_the_jstl_api_does_not_bring_a_second_servlet_api_into_the_war():
    boot = _pom(_files()["shopProject-boot/pom.xml"])
    jstl = [d for d in boot.findall("m:dependencies/m:dependency", NS)
            if d.findtext("m:artifactId", namespaces=NS) == "jakarta.servlet.jsp.jstl-api"]
    assert len(jstl) == 1
    excluded = [(e.findtext("m:groupId", namespaces=NS), e.findtext("m:artifactId", namespaces=NS))
                for e in jstl[0].findall("m:exclusions/m:exclusion", NS)]
    assert excluded == [("jakarta.servlet", "jakarta.servlet-api")]
