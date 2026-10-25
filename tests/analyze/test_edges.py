"""Analyzer edge cases: E1 encoding, E7 system jars, X6 languages/codegen, S1 MSA library roots, S3 pom-only, S4."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import (analyze_path, deployable_services, detect_encoding, is_library_root, library_services,
                           normalize_java_version, resolve_system_path)
from sbmig.analyze.edges import legacy_java_level
from sbmig.core.models import CaseType, ServiceInfo
from tests.analyze.helpers import BOOT_PLUGIN, dep, make_boot33, make_pure_java, plugin, pom, write, write_java


def _sysdep(a: str, path: str) -> str:
    return (f"<dependency><groupId>com.vendor</groupId><artifactId>{a}</artifactId><version>1.0</version>"
            f"<scope>system</scope><systemPath>{path}</systemPath></dependency>")


def _lib(p, name):
    return next((lib for lib in p.libraries if lib.name == name), None)


# ------------------------------------------------------------------------------------------------- E7
def test_system_scoped_dependencies(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("app", deps=(
        _sysdep("x", "${basedir}/lib/x.jar"),
        _sysdep("y", "${project.basedir}/src/main/webapp/WEB-INF/lib/y.jar"),
        _sysdep("z", "/opt/vendor/z.jar"),
        _sysdep("w", "${vendor.home}/w.jar"))))
    write(tmp_path, "lib/x.jar", "PK")
    write_java(tmp_path, "", "com.acme", "A")
    p = analyze_path(tmp_path, cfg)
    warns = [w for w in p.warnings if w.startswith("system-scoped dependency")]
    assert len(warns) == 4
    assert any("com.vendor:x" in w and "lib/x.jar (exists, in project)" in w for w in warns)
    assert any("com.vendor:y" in w and "src/main/webapp/WEB-INF/lib/y.jar (MISSING, in project)" in w for w in warns)
    assert any("com.vendor:z" in w and "outside the project" in w for w in warns)
    assert any("com.vendor:w" in w and "unresolvable property" in w for w in warns)
    lib = _lib(p, "system-scope-jars")
    assert lib is not None
    assert "pom:com.vendor:x:1.0" in lib.evidence and "file:lib/x.jar" in lib.evidence
    assert "file:src/main/webapp/WEB-INF/lib/y.jar (missing)" in lib.evidence


def test_resolve_system_path_variants(tmp_path: Path) -> None:
    assert resolve_system_path("${basedir}/lib/a.jar", "") == ("lib/a.jar", True)
    assert resolve_system_path("${project.basedir}/../lib/a.jar", "core") == ("lib/a.jar", True)
    assert resolve_system_path("${basedir}/../../x.jar", "core") == ("../x.jar", False)
    assert resolve_system_path("lib/${v}.jar", "m", {"v": "a-1"}) == ("m/lib/a-1.jar", True)
    assert resolve_system_path("${nope}/a.jar", "") == (None, False)
    assert resolve_system_path(f"{tmp_path}/lib/a.jar", "", root=tmp_path) == ("lib/a.jar", True)
    assert resolve_system_path("/elsewhere/a.jar", "", root=tmp_path) == ("/elsewhere/a.jar", False)


# ------------------------------------------------------------------------------------------------- X6
def test_languages_and_codegen_plugins(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("poly", plugins=(
        plugin("protobuf-maven-plugin", "0.6.1", g="org.xolstice.maven.plugins"),
        plugin("querydsl-maven-plugin", g="com.querydsl"),
        plugin("kotlin-maven-plugin", "1.9.0", g="org.jetbrains.kotlin"))))
    write_java(tmp_path, "", "com.acme", "A")
    write(tmp_path, "src/main/kotlin/K.kt", "class K")
    write(tmp_path, "src/test/groovy/SpecTest.groovy", "class SpecTest {}")
    write(tmp_path, "src/main/scala/S.scala", "object S")
    p = analyze_path(tmp_path, cfg)
    names = {lib.name for lib in p.libraries}
    assert {"kotlin", "groovy", "scala", "codegen:protobuf-maven-plugin", "codegen:querydsl-maven-plugin"} <= names
    assert "plugin:kotlin-maven-plugin" in _lib(p, "kotlin").evidence
    assert _lib(p, "codegen:protobuf-maven-plugin").version == "0.6.1"
    assert any("Kotlin" in w and "not migrated" in w for w in p.warnings)
    assert any("Groovy" in w for w in p.warnings) and any("Scala" in w for w in p.warnings)
    assert any("source generator plugin protobuf-maven-plugin:0.6.1" in w for w in p.warnings)


# ------------------------------------------------------------------------------------------------- java levels
def test_legacy_java_level_warning_and_normalization(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("old", props={"maven.compiler.source": "6", "maven.compiler.target": "6"}))
    write_java(tmp_path, "", "com.acme", "A")
    p = analyze_path(tmp_path, cfg)
    assert p.java_version == "1.6"
    assert any("Java source/target level 6 cannot be compiled by JDK 21" in w for w in p.warnings)
    assert any("jdk-removed-apis advisory" in w for w in p.warnings)
    assert normalize_java_version("1.7") == "1.7" and normalize_java_version("7") == "1.7"
    assert normalize_java_version("1.5.0") == "1.5"
    assert normalize_java_version("1.8") == "1.8" and normalize_java_version("17") == "17"
    assert normalize_java_version("${x}") == "${x}" and normalize_java_version(None) is None
    assert legacy_java_level("1.7") and not legacy_java_level("1.8") and not legacy_java_level("11")


def test_modern_java_level_has_no_warning(tmp_path: Path, cfg) -> None:
    p = analyze_path(make_pure_java(tmp_path), cfg)
    assert p.java_version == "11" and not any("cannot be compiled by JDK 21" in w for w in p.warnings)


# ------------------------------------------------------------------------------------------------- E1
KOREAN = "package com.acme;\n// 한글 주석\npublic class K {}\n"


def test_legacy_encoding_detected(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("enc"))
    f = tmp_path / "src/main/java/com/acme/K.java"
    f.parent.mkdir(parents=True)
    f.write_bytes(KOREAN.encode("cp949"))
    write_java(tmp_path, "", "com.acme", "Plain")
    p = analyze_path(tmp_path, cfg)
    w = next(w for w in p.warnings if w.startswith("sources are not UTF-8"))
    assert "src/main/java/com/acme/K.java" in w and "set project.build.sourceEncoding or enable convert-utf8" in w
    assert _lib(p, "legacy-encoding").evidence == ["cp949"]


def test_declared_encoding_or_utf8_sources_no_warning(tmp_path: Path, cfg) -> None:
    a = tmp_path / "a"
    write(a, "pom.xml", pom("enc", props={"project.build.sourceEncoding": "MS949"}))
    f = a / "src/main/java/com/acme/K.java"
    f.parent.mkdir(parents=True)
    f.write_bytes(KOREAN.encode("cp949"))
    p = analyze_path(a, cfg)
    assert not any("UTF-8" in w for w in p.warnings) and _lib(p, "legacy-encoding") is None
    b = tmp_path / "b"
    write(b, "pom.xml", pom("enc"))
    write(b, "src/main/java/com/acme/K.java", KOREAN)
    assert _lib(analyze_path(b, cfg), "legacy-encoding") is None
    c = tmp_path / "c"
    write(c, "pom.xml", pom("enc", plugins=(plugin("maven-compiler-plugin", config="<encoding>EUC-KR</encoding>"),)))
    g = c / "src/main/java/K.java"
    g.parent.mkdir(parents=True)
    g.write_bytes(KOREAN.encode("euc-kr"))
    assert _lib(analyze_path(c, cfg), "legacy-encoding") is None


def test_detect_encoding_order() -> None:
    assert detect_encoding("abc".encode()) == "utf-8"
    assert detect_encoding(b"\xef\xbb\xbfabc") == "utf-8-sig"
    assert detect_encoding("한글".encode("cp949")) == "cp949"
    assert detect_encoding(b"\xff\xff\xff", ("utf-8",)) is None


# ------------------------------------------------------------------------------------------------- S3 / S4
def test_pom_only_aggregator(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("bom", packaging="pom", managed=(dep("com.google.guava", "guava", "31.0-jre"),)))
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.PURE_JAVA and p.confidence < 0.5
    assert "no java sources found" in p.warnings
    assert any(w.startswith("pom-only project") for w in p.warnings)


def test_gradle_and_ant_warning_first(tmp_path: Path, cfg) -> None:
    g = tmp_path / "g"
    write(g, "build.gradle", "plugins {}")
    write_java(g, "", "com.g", "G")
    p = analyze_path(g, cfg)
    assert p.build_tool == "gradle" and p.warnings[0].startswith("Gradle build detected")
    a = tmp_path / "a"
    write(a, "build.xml", "<project/>")
    write_java(a, "", "com.a", "A")
    p = analyze_path(a, cfg)
    assert p.build_tool == "ant" and p.warnings[0].startswith("Ant build.xml detected")
    n = tmp_path / "n"
    write_java(n, "", "com.n", "N")
    p = analyze_path(n, cfg)
    assert p.build_tool == "none" and p.warnings[0].startswith("no pom.xml found")


# ------------------------------------------------------------------------------------------------- S1
def make_msa_with_common(root: Path, *, boot_dep_in_common: bool = False) -> Path:
    write(root, "pom.xml", pom("platform", packaging="pom", modules=("common", "shop", "billing")))
    deps = [dep("org.springframework", "spring-context", "5.3.30")]
    if boot_dep_in_common:
        deps.append(dep("org.springframework.boot", "spring-boot-starter-web", "2.7.18"))
    write(root, "common/pom.xml", pom("common", parent=("com.acme", "platform", "1.0.0"), deps=tuple(deps)))
    write_java(root, "common", "com.acme.common", "Money", imports=("javax.validation.constraints.NotNull",))
    for svc in ("shop", "billing"):
        write(root, f"{svc}/pom.xml", pom(
            svc, parent=("org.springframework.boot", "spring-boot-starter-parent", "3.3.5"), relative_path="",
            deps=(dep("org.springframework.boot", "spring-boot-starter-web"), dep("com.acme", "common", "1.0.0")),
            plugins=(BOOT_PLUGIN,)))
        write_java(root, svc, f"com.acme.{svc}", f"{svc.title()}App", annotations=("SpringBootApplication",),
                   imports=("org.springframework.boot.autoconfigure.SpringBootApplication",))
    return root


def test_msa_library_roots(tmp_path: Path, cfg) -> None:
    p = analyze_path(make_msa_with_common(tmp_path, boot_dep_in_common=True), cfg)
    assert p.case_type == CaseType.MSA
    libs = library_services(p)
    assert [s.name for s in libs] == ["common"]
    common = libs[0]
    assert common.root_pom == "common/pom.xml" and common.main_classes == [] and common.spring_boot_version is None
    assert common.case_type == CaseType.SPRING_LEGACY                         # boot dependency → never the umbrella
    assert sorted(s.name for s in deployable_services(p)) == ["billing", "shop"]
    assert p.services[-1].name == "common"                                     # libraries after deployables
    assert any("shared library roots" in r for r in p.reasons)


def test_is_library_root_never_flags_war_services(cfg) -> None:
    from sbmig.core.models import PomInfo, ProjectProfile
    prof = ProjectProfile(root="/x", name="x", case_type=CaseType.MSA, poms=[
        PomInfo(path="web/pom.xml", artifact_id="web", packaging="war"),
        PomInfo(path="lib/pom.xml", artifact_id="lib")])
    web = ServiceInfo(name="web", root_pom="web/pom.xml", case_type=CaseType.SERVLET_TOMCAT, poms=["web/pom.xml"])
    lib = ServiceInfo(name="lib", root_pom="lib/pom.xml", case_type=CaseType.PURE_JAVA, poms=["lib/pom.xml"])
    unknown = ServiceInfo(name="u", root_pom="u/pom.xml", case_type=CaseType.PURE_JAVA)
    boot = ServiceInfo(name="b", root_pom="lib/pom.xml", case_type=CaseType.SPRING_BOOT, main_classes=["a.B"])
    assert not is_library_root(web, prof) and is_library_root(lib, prof)
    assert not is_library_root(unknown, prof) and not is_library_root(boot, prof)
    assert is_library_root(web)                                                 # without profile: plain convention


def test_msa_nested_library_aggregator(tmp_path: Path, cfg) -> None:
    make_boot33(tmp_path, module="shop", artifact="shop")
    make_boot33(tmp_path, module="billing", artifact="billing")
    write(tmp_path, "libs/pom.xml", pom("libs", packaging="pom", modules=("a", "b")))
    write(tmp_path, "libs/a/pom.xml", pom("lib-a", parent=("com.acme", "libs", "1.0.0")))
    write(tmp_path, "libs/b/pom.xml", pom("lib-b", parent=("com.acme", "libs", "1.0.0")))
    write_java(tmp_path, "libs/a", "com.acme.a", "A")
    p = analyze_path(tmp_path, cfg)
    libs = library_services(p)
    assert [(s.name, s.root_pom) for s in libs] == [("libs", "libs/pom.xml")]
    assert sorted(libs[0].poms) == ["libs/a/pom.xml", "libs/b/pom.xml", "libs/pom.xml"]
    assert libs[0].case_type == CaseType.PURE_JAVA


# ------------------------------------------------------------------------------------------------- round 10 (D15)
def test_wsdl_code_generators_are_reported_with_advice(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("soap", plugins=(
        plugin("cxf-codegen-plugin", "3.5.5", g="org.apache.cxf"),
        plugin("jaxws-maven-plugin", "2.6", g="org.codehaus.mojo"))))
    write_java(tmp_path, "", "com.acme", "A")
    p = analyze_path(tmp_path, cfg)
    names = {lib.name for lib in p.libraries}
    assert {"codegen:cxf-codegen-plugin", "codegen:jaxws-maven-plugin"} <= names
    assert "plugin:org.codehaus.mojo:jaxws-maven-plugin" in _lib(p, "codegen:jaxws-maven-plugin").evidence
    cxf = next(w for w in p.warnings if "cxf-codegen-plugin:3.5.5" in w)
    assert "4.2.4" in cxf and "jakarta-library-floor" in cxf
    jaxws = next(w for w in p.warnings if "jaxws-maven-plugin:2.6" in w)
    assert "com.sun.xml.ws:jaxws-maven-plugin 4.0.5" in jaxws and "javax.xml.ws" in jaxws
