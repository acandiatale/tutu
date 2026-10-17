from __future__ import annotations

from pathlib import Path


from sbmig.analyze import analyze_path, analyze_project, classify
from sbmig.analyze.classifier import classify_single
from sbmig.core.config import ArtifactSpec
from sbmig.core.models import CaseType, SourceStats
from tests.analyze.helpers import (BOOT_PLUGIN, PSVM, dep, make_boot27, make_boot33, make_pure_java, make_servlet,
                                   make_spring_legacy, pom, write, write_java)


def test_spring_boot(tmp_path: Path, cfg) -> None:
    make_boot27(tmp_path)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.SPRING_BOOT and p.confidence >= 0.7
    assert p.spring_boot_version == "2.7.18" and p.java_version == "1.8"
    assert p.name == "boot-app" and p.root_pom.path == "pom.xml" and p.packaging == "jar"
    assert not p.is_multi_module and p.build_tool == "maven"
    assert p.fingerprint and len(p.fingerprint) == 16
    assert p.has_library("lombok")
    assert [g.coordinates for g in p.optional_artifacts_in_use] == ["org.projectlombok:lombok:1.18.24"]
    assert p.mandatory_already_present == []
    assert p.root == str(tmp_path.resolve())


def test_workspace_entry_point(tmp_path: Path, cfg, make_ws) -> None:
    src = make_boot33(tmp_path / "src")
    ws = make_ws(src)
    p = analyze_project(ws, cfg)
    assert p.case_type == CaseType.SPRING_BOOT and p.spring_boot_version == "3.3.5"
    assert p.has_library("spring-security") and p.has_library("junit5")


def test_spring_legacy(tmp_path: Path, cfg) -> None:
    make_spring_legacy(tmp_path)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.SPRING_LEGACY
    assert p.spring_version == "5.3.30" and p.java_version == "1.8" and p.packaging == "war"
    assert any("Spring wins" in r for r in p.reasons)


def test_spring_legacy_from_annotations_only(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.a", "C", annotations=("Controller",),
               imports=("org.springframework.stereotype.Controller",))
    assert analyze_path(tmp_path, cfg).case_type == CaseType.SPRING_LEGACY


def test_servlet(tmp_path: Path, cfg) -> None:
    make_servlet(tmp_path)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.SERVLET_TOMCAT
    assert p.source.jsp_count == 2 and p.source.has_web_xml


def test_pure_java(tmp_path: Path, cfg) -> None:
    make_pure_java(tmp_path)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.PURE_JAVA and p.java_version == "11"
    assert p.source.psvm_classes == ["com.acme.util.Main"]
    assert p.source.base_package == "com.acme.util"
    assert {g.key for g in p.optional_artifacts_in_use} == {"org.apache.commons:commons-lang3", "com.google.guava:guava"}


def test_optional_alias_and_mandatory(tmp_path: Path, cfg) -> None:
    cfg.artifacts.optional.append(ArtifactSpec(group_id="org.apache.commons", artifact_id="commons-text",
                                               version="1.12.0", aliases=["commons-lang:commons-lang"]))
    write(tmp_path, "pom.xml", pom("x", props={"cl.version": "2.6"},
                                   deps=(dep("commons-lang", "commons-lang", "${cl.version}"),
                                         dep("org.springframework.boot", "spring-boot-starter-actuator")),
                                   managed=(dep("org.slf4j", "slf4j-api", "1.7.36"),)))
    p = analyze_path(tmp_path, cfg)
    assert {g.coordinates for g in p.optional_artifacts_in_use} == {"commons-lang:commons-lang:2.6",
                                                                   "org.slf4j:slf4j-api:1.7.36"}
    assert [g.key for g in p.mandatory_already_present] == ["org.springframework.boot:spring-boot-starter-actuator"]
    assert "no java sources found" in p.warnings


def test_msa_two_services_under_aggregator(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("shop", "billing")))
    make_boot27(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA and p.confidence == 0.9
    assert p.is_multi_module and p.name == "platform" and p.root_pom.is_aggregator
    svcs = {s.name: s for s in p.services}
    assert set(svcs) == {"shop", "billing"}
    assert svcs["shop"].case_type == CaseType.SPRING_BOOT and svcs["shop"].spring_boot_version == "2.7.18"
    assert svcs["shop"].java_version == "1.8" and svcs["shop"].root_pom == "shop/pom.xml"
    assert svcs["billing"].spring_boot_version == "3.3.5"
    assert svcs["billing"].main_classes == ["com.acme.billing.BillingApplication"]
    assert p.source.javax_imports.get("javax.servlet") and p.source.jakarta_imports.get("jakarta.servlet")


def test_msa_nested_service_roots_and_app_class_rule(tmp_path: Path, cfg) -> None:
    # service a: aggregator a/ with modules a/api (boot app) and a/core; service b: b/ (boot app, no plugin)
    write(tmp_path, "a/pom.xml", pom("a", packaging="pom", modules=("api", "core")))
    write(tmp_path, "a/api/pom.xml", pom("a-api", parent=("com.acme", "a", "1.0.0"),
                                         deps=(dep("org.springframework.boot", "spring-boot-starter", "3.2.0"),)))
    write(tmp_path, "a/core/pom.xml", pom("a-core", parent=("com.acme", "a", "1.0.0")))
    write_java(tmp_path, "a/api", "com.a", "AApp", annotations=("SpringBootApplication",), body=PSVM,
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",))
    write(tmp_path, "b/pom.xml", pom("b", deps=(dep("org.springframework.boot", "spring-boot-starter", "2.7.0"),)))
    write_java(tmp_path, "b", "com.b", "BApp", annotations=("SpringBootApplication",), body=PSVM,
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",))
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA and p.confidence == 0.75
    assert {s.name: s.root_pom for s in p.services} == {"a": "a/pom.xml", "b": "b/pom.xml"}
    assert p.root_pom is None and p.name == tmp_path.name


def test_msa_independent_war_roots(tmp_path: Path, cfg) -> None:
    for name in ("one", "two"):
        write(tmp_path, f"{name}/pom.xml", pom(name, packaging="war"))
        write_java(tmp_path, name, f"com.{name}", "S", extends="HttpServlet", imports=("javax.servlet.http.HttpServlet",))
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA
    assert {s.case_type for s in p.services} == {CaseType.SERVLET_TOMCAT}
    assert "independent root poms" in p.reasons[0]


def test_same_name_services_are_disambiguated(tmp_path: Path, cfg) -> None:
    write(tmp_path, "x/svc/pom.xml", pom("s1", plugins=(BOOT_PLUGIN,)))
    write(tmp_path, "y/svc/pom.xml", pom("s2", plugins=(BOOT_PLUGIN,)))
    p = analyze_path(tmp_path, cfg)
    assert sorted(s.name for s in p.services) == ["svc", "svc-2"]


def test_multi_module_boot_is_not_msa(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("parent", packaging="pom", modules=("core", "app"),
                                   parent=("org.springframework.boot", "spring-boot-starter-parent", "3.1.0"),
                                   plugin_mgmt=(BOOT_PLUGIN,)))
    write(tmp_path, "core/pom.xml", pom("core", parent=("com.acme", "parent", "1.0.0")))
    write(tmp_path, "app/pom.xml", pom("app", parent=("com.acme", "parent", "1.0.0"), plugins=(BOOT_PLUGIN,)))
    write_java(tmp_path, "app", "com.acme", "App", annotations=("SpringBootApplication",), body=PSVM,
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",))
    write_java(tmp_path, "core", "com.acme.core", "Svc", annotations=("Service",),
               imports=("org.springframework.stereotype.Service",))
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.SPRING_BOOT and p.is_multi_module and p.services == []
    assert p.spring_boot_version == "3.1.0"


def test_gradle_ant_kotlin_and_empty(tmp_path: Path, cfg) -> None:
    g = tmp_path / "g"
    write(g, "build.gradle", "plugins {}")
    write_java(g, "", "com.g", "G")
    write(g, "src/main/kotlin/K.kt", "class K")
    p = analyze_path(g, cfg)
    assert p.build_tool == "gradle" and any("Gradle" in w for w in p.warnings)
    assert any("Kotlin" in w for w in p.warnings) and p.case_type == CaseType.PURE_JAVA
    a = tmp_path / "a"
    write(a, "build.xml", "<project/>")
    p = analyze_path(a, cfg)
    assert p.build_tool == "none" and any("Ant" in w for w in p.warnings)
    assert p.case_type == CaseType.UNKNOWN
    m = tmp_path / "m"
    write(m, "pom.xml", pom("m"))
    write(m, "build.xml", "<project/>")
    assert any("ignored" in w for w in analyze_path(m, cfg).warnings)


def test_unparsable_pom_does_not_crash(tmp_path: Path, cfg) -> None:
    make_pure_java(tmp_path)
    write(tmp_path, "bad/pom.xml", "<project>")
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.PURE_JAVA
    assert any("bad/pom.xml" in w for w in p.warnings)


def test_classify_without_scan() -> None:
    src = SourceStats(java_files=1, spring_annotations={"Service": 1})
    assert classify([], src, [])[0] == CaseType.SPRING_LEGACY
    assert classify_single([], SourceStats(), [])[0] == CaseType.UNKNOWN
