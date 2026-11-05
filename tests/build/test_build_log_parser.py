"""parse_maven_log against REAL Maven 3.9.10 / JDK 21 logs (tests/build/logs/real-*.log, temp prefix → /work/proj)
and hand-written logs for rarer formats (synthetic-*.log, marked as such in their first line)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import parse_maven_log, parse_reactor_summary
from sbmig.build.log_parser import parse_coords, plugin_short_name, strip_ansi
from sbmig.core.models import ErrorKind

LOGS = Path(__file__).parent / "logs"
ROOT = "/work/proj"


def load(name: str) -> str:
    return (LOGS / name).read_text(encoding="utf-8")


def parse(name: str, root: str = ROOT):
    return parse_maven_log(load(name), root)


def tup(e):
    return (e.kind, e.file, e.line, e.column, e.symbol, e.location)


# ---------------------------------------------------------------------------------------------- (a) javac errors
def test_real_compile_errors_full_structure():
    errs = parse("real-compile-errors.log")
    sec = "src/main/java/com/example/sec/SecurityConfig.java"
    web = "src/main/java/com/example/web/HelloServlet.java"
    C = ErrorKind.COMPILE
    assert [tup(e) for e in errs] == [
        (C, sec, 3, 72, None, None),
        (C, sec, 5, 37, "class WebSecurityConfigurerAdapter", None),
        (C, sec, 6, 13, "class Foo", "class com.example.sec.SecurityConfig"),
        (C, web, 3, 26, None, None),
        (C, web, 4, 26, None, None),
        (C, web, 6, 35, "class HttpServlet", None),
        (C, web, 7, 23, "class HttpServletRequest", "class com.example.web.HelloServlet"),
        (C, sec, 8, 20, None, None),
        (C, sec, 9, 9, "method bar()", "class com.example.sec.SecurityConfig"),
    ]
    assert {e.module for e in errs} == {"compile-errors"}
    assert {e.goal for e in errs} == {"compiler:compile"}
    assert errs[0].message == "package org.springframework.security.config.annotation.web.configuration does not exist"
    assert errs[7].message == "incompatible types: int cannot be converted to java.lang.String"


def test_real_compile_error_signatures():
    errs = parse("real-compile-errors.log")
    sigs = [(e.signature, e.signature_generic) for e in errs]
    assert sigs[0] == ("compile|package_does_not_exist|org.springframework.security.config.annotation.web.configuration",
                       "compile|package_does_not_exist|org.springframework.security.config.annotation.web.configuration")
    assert sigs[1] == ("compile|cannot_find_symbol|class WebSecurityConfigurerAdapter",
                       "compile|cannot_find_symbol|WebSecurityConfigurerAdapter")
    assert sigs[2] == ("compile|cannot_find_symbol|class Foo|class *", "compile|cannot_find_symbol|Foo")
    assert sigs[3] == ("compile|package_does_not_exist|javax.servlet.http",
                       "compile|package_does_not_exist|javax.servlet.http")
    assert sigs[3] == sigs[4]
    assert sigs[7] == ("compile|incompatible_types|incompatible types: int cannot be converted to java.lang.String",
                       "compile|incompatible_types|int->String")
    assert sigs[8] == ("compile|cannot_find_symbol|method bar()|class *", "compile|cannot_find_symbol|bar")
    for e in errs:
        assert "/work/proj" not in e.signature and "/work/proj" not in e.raw


def test_continuations_go_to_raw_and_duplicates_merge():
    errs = parse("real-compile-errors.log")
    # the error appears twice in the log (COMPILATION ERROR block + final [ERROR] block) → once, raw from first
    assert len([e for e in errs if e.line == 6 and e.column == 13]) == 1
    e = errs[2]
    assert "symbol:   class Foo" in e.raw and "location: class com.example.sec.SecurityConfig" in e.raw
    assert e.raw.startswith("[ERROR] src/main/java/com/example/sec/SecurityConfig.java:[6,13]")


def test_project_root_none_keeps_paths():
    errs = parse_maven_log(load("real-compile-errors.log"), None)
    assert errs[0].file == "/work/proj/src/main/java/com/example/sec/SecurityConfig.java"
    assert errs[0].signature == parse("real-compile-errors.log")[0].signature


# ---------------------------------------------------------------------------------------------- (b) testCompile
def test_real_test_compile_only():
    errs = parse("real-test-compile-errors.log")
    f = "src/test/java/com/example/CalcTest.java"
    T = ErrorKind.TEST_COMPILE
    assert [tup(e) for e in errs] == [
        (T, f, 3, 29, None, None),
        (T, f, 6, 6, "class Test", "class com.example.CalcTest"),
        (T, f, 8, 19, "method subtract(int,int)", "class com.example.Calc"),
    ]
    assert {e.goal for e in errs} == {"compiler:testCompile"}
    assert errs[0].signature == "test_compile|package_does_not_exist|org.junit.jupiter.api"
    assert errs[0].signature_generic == "test_compile|package_does_not_exist|org.junit.jupiter.api"
    assert errs[2].signature_generic == "test_compile|cannot_find_symbol|subtract"


# ---------------------------------------------------------------------------------------------- (c) dependency
def test_real_missing_dependency():
    errs = parse("real-missing-dependency.log")
    assert [(e.kind, e.artifact.coordinates, e.module) for e in errs] == [
        (ErrorKind.DEPENDENCY_RESOLUTION, "com.example:does-not-exist:1.0", "missing-dep"),
        (ErrorKind.DEPENDENCY_RESOLUTION, "com.example:also-missing:2.0", "missing-dep"),
    ]
    assert errs[0].message == ("Could not find artifact com.example:does-not-exist:jar:1.0 in central "
                               "(https://repo.maven.apache.org/maven2)")
    assert errs[0].signature == "dependency_resolution|com.example:does-not-exist:1.0"
    assert errs[0].signature_generic == "dependency_resolution|com.example:does-not-exist"


def test_synthetic_legacy_dependency_formats_in_reactor():
    errs = parse("synthetic-dependency-legacy-formats.log")
    assert [(e.artifact.coordinates, e.module) for e in errs] == [
        ("javax.servlet:jstl:1.2.1", "shop-web"),
        ("com.acme.legacy:auth-client:2.3", "shop-web"),
        ("com.acme.legacy:auth-model:2.3", "shop-web"),
    ]
    assert all(e.kind == ErrorKind.DEPENDENCY_RESOLUTION for e in errs)
    assert errs[0].message.startswith("Could not find artifact javax.servlet:jstl:jar:1.2.1 in nexus")
    assert errs[1].message == "Could not resolve artifact com.acme.legacy:auth-client:jar:2.3 (absent)"
    assert errs[2].message.endswith("Connection refused")
    assert parse_reactor_summary(load("synthetic-dependency-legacy-formats.log")) == (["shop-core"], "shop-web")


def test_missing_dependency_version_is_pom_invalid():
    errs = parse("real-missing-dependency-version.log")
    assert len(errs) == 1
    e = errs[0]
    assert (e.kind, e.file, e.module, e.artifact.key) == (ErrorKind.POM_INVALID, "pom.xml", "missing-version",
                                                          "org.apache.commons:commons-lang3")
    assert e.signature == "pom_invalid|missing_version|org.apache.commons:commons-lang3"
    assert e.signature_generic == "pom_invalid|missing_version"


def test_unresolvable_build_extension_is_one_pom_problem_per_extension():
    """2026-11-05 (company run): `Unresolveable build extension: Plugin org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 …`
    stops Maven before any goal; the summary line and the project block name the same extension with different tails
    (the block carries the transfer reason) → ONE error, the extension as the artifact, a per-extension case."""
    from sbmig.build import parse_maven_log
    log = (
        "[INFO] Scanning for projects...\n"
        "[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
        "[ERROR] Unresolveable build extension: Plugin org.apache.maven.wagon:wagon-webdav:1.0-beta-2 or one of its "
        "dependencies could not be resolved: Failed to read artifact descriptor for org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 @ \n"
        " @ \n"
        "[ERROR] The build could not read 1 project -> [Help 1]\n"
        "[ERROR]   \n"
        "[ERROR]   The project com.example:app:1.0 (/work/proj/pom.xml) has 1 error\n"
        "[ERROR]     Unresolveable build extension: Plugin org.apache.maven.wagon:wagon-webdav:1.0-beta-2 or one of its "
        "dependencies could not be resolved: Failed to read artifact descriptor for org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2: "
        "Could not transfer artifact org.apache.maven.wagon:wagon-webdav:pom:1.0-beta-2 from/to maven-default-http-blocker "
        "(http://nexus.company.local/repository/maven-public/): status code: 401, reason phrase: Unauthorized (401) -> [Help 2]\n"
        "[ERROR] \n"
        "[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.\n")
    errs = parse_maven_log(log, Path("/work/proj"))
    assert len(errs) == 1
    e = errs[0]
    assert (e.kind, e.file, e.artifact.coordinates) == (ErrorKind.POM_INVALID, "pom.xml",
                                                        "org.apache.maven.wagon:wagon-webdav:1.0-beta-2")
    assert "status code: 401" in e.message                                       # the block's reason wins over the summary
    assert e.signature == "pom_invalid|build_extension|org.apache.maven.wagon:wagon-webdav:1.0-beta-2"
    assert e.signature_generic == "pom_invalid|build_extension|org.apache.maven.wagon:wagon-webdav"
    assert not [x for x in errs if x.kind == ErrorKind.PLUGIN_RESOLUTION]      # the project-block line is not a plugin block
    # the `:jar:` form some people retype and Maven 4's spelling parse the same way
    for variant in (log.replace("wagon-webdav:1.0-beta-2 or", "wagon-webdav:jar:1.0-beta-2 or"),
                    log.replace("Unresolveable", "Unresolvable")):
        v = parse_maven_log(variant, Path("/work/proj"))
        assert len(v) == 1 and v[0].artifact.key == "org.apache.maven.wagon:wagon-webdav" and v[0].kind == ErrorKind.POM_INVALID


# ---------------------------------------------------------------------------------------------- (d) parent
def test_real_non_resolvable_parent():
    errs = parse("real-bad-parent.log")
    assert len(errs) == 1                                   # [FATAL] line and project block deduped
    e = errs[0]
    assert e.kind == ErrorKind.PARENT_RESOLUTION
    assert e.artifact.coordinates == "com.company:company-parent:9.9.9"
    assert (e.module, e.file) == ("bad-parent", "pom.xml")
    assert e.signature == "parent_resolution|com.company:company-parent:9.9.9"
    assert e.signature_generic == "parent_resolution|com.company:company-parent"


# ---------------------------------------------------------------------------------------------- (e) plugin
def test_real_plugin_resolution():
    errs = parse("real-plugin-resolution.log")
    assert len(errs) == 1
    e = errs[0]
    assert e.kind == ErrorKind.PLUGIN_RESOLUTION
    assert e.artifact.coordinates == "com.example.plugins:nonexistent-maven-plugin:1.0.0"
    assert e.module == "bad-plugin"
    assert "Could not find artifact com.example.plugins:nonexistent-maven-plugin:jar:1.0.0" in e.message
    assert e.signature == "plugin_resolution|com.example.plugins:nonexistent-maven-plugin:1.0.0"
    assert e.signature_generic == "plugin_resolution|com.example.plugins:nonexistent-maven-plugin"


def test_synthetic_plugin_version_resolution():
    (e,) = parse("synthetic-plugin-version.log")
    assert e.kind == ErrorKind.PLUGIN_RESOLUTION
    assert e.artifact.key == "com.acme.build:acme-codegen-maven-plugin" and e.artifact.version is None
    assert e.signature == e.signature_generic == "plugin_resolution|com.acme.build:acme-codegen-maven-plugin"


# ---------------------------------------------------------------------------------------------- (f) reactor
def test_real_multimodule_failure_in_second_module():
    text = load("real-multimodule-failure.log")
    errs = parse_maven_log(text, ROOT)
    assert [(e.file, e.line, e.column, e.symbol, e.location, e.module) for e in errs] == [
        ("api/src/main/java/com/example/api/Api.java", 4, 24, "class Missing", "package com.example.core", "api"),
        ("api/src/main/java/com/example/api/Api.java", 7, 36, "method nme()", "class com.example.core.Core", "api"),
    ]
    assert errs[0].signature == "compile|cannot_find_symbol|class Missing|package *"
    built, failed = parse_reactor_summary(text)
    assert built == ["multi-parent", "core"]                # "Multi Parent" name mapped to artifactId
    assert failed == "api"


# ---------------------------------------------------------------------------------------------- (g)/(i) success
@pytest.mark.parametrize("name,module", [("real-package-success.log", "ok-package"),
                                         ("real-test-skip-success.log", "test-skip")])
def test_real_success_has_no_errors(name, module):
    text = load(name)
    assert parse_maven_log(text, ROOT) == []
    assert parse_reactor_summary(text) == ([module], None)


# ---------------------------------------------------------------------------------------------- (h) lombok
def test_real_lombok_on_jdk21_is_annotation_processor_error():
    (e,) = parse("real-lombok-jdk21.log")
    assert e.kind == ErrorKind.COMPILE and e.file is None
    assert e.message.startswith("annotation-processor: java.lang.IllegalAccessError: class lombok.javac.apt")
    assert e.goal == "compiler:compile" and e.module == "lombok-old"
    assert e.signature_generic == "compile|annotation_processor|lombok"
    assert "<hex>" in e.signature and "0x" not in e.signature


@pytest.mark.parametrize("name,generic", [
    ("synthetic-lombok-typetag.log", "compile|annotation_processor|ExceptionInInitializerError"),
    ("synthetic-lombok-jcimport.log", "compile|annotation_processor|NoSuchFieldError"),
])
def test_synthetic_annotation_processor_crashes(name, generic):
    (e,) = parse(name)
    assert e.kind == ErrorKind.COMPILE and e.message.startswith("annotation-processor:")
    assert e.signature_generic == generic


def test_real_release_not_supported_is_compile_without_file():
    (e,) = parse("real-jdk11-release21.log")
    assert (e.kind, e.file, e.message) == (ErrorKind.COMPILE, None, "error: release version 21 not supported")
    assert e.signature == "compile|build|error: release version # not supported"


# ---------------------------------------------------------------------------------------------- enforcer / pom / tests
def test_real_enforcer_rules():
    errs = parse("real-enforcer.log")
    assert [e.kind for e in errs] == [ErrorKind.ENFORCER, ErrorKind.ENFORCER]
    assert errs[0].message.startswith("RequireJavaVersion: Detected JDK version 21.0.1")
    assert errs[0].signature == "enforcer|requirejavaversion: detected jdk version <v> is not in the allowed range <range>."
    assert errs[0].signature_generic == "enforcer|requirejavaversion: detected jdk version <v> is"
    assert errs[1].message.startswith("BannedDependencies:") and "junit:junit:jar:4.13.2" in errs[1].message
    assert "enforcer-fail" not in errs[1].signature                       # module name stripped
    assert {e.goal for e in errs} == {"enforcer:enforce"}


def test_synthetic_legacy_enforcer():
    errs = parse("synthetic-enforcer-legacy.log")
    assert [e.message for e in errs] == [
        "RequireJavaVersion: Detected JDK Version: 21.0.1 is not in the allowed range [1.8,9).",
        "DependencyConvergence: Failed while enforcing releasability. See above detailed error message.",
    ]
    assert all(e.module == "legacy-app" for e in errs)


def test_real_malformed_pom():
    (e,) = parse("real-malformed-pom.log")
    assert (e.kind, e.file) == (ErrorKind.POM_INVALID, "pom.xml")
    assert e.message.startswith("Non-parseable POM pom.xml: end tag name </dependencies>")
    assert e.signature.startswith("pom_invalid|non-parseable pom pom.xml: end tag name </dependencies>")
    assert e.signature_generic == "pom_invalid|non-parseable pom pom.xml: end tag name"


def test_real_test_failures():
    (e,) = parse("real-test-failure.log")
    assert (e.kind, e.symbol, e.module, e.goal) == (ErrorKind.TEST_FAILURE, "ATest.one", "test-failure", "surefire:test")
    assert e.message == "ATest.one:5 expected:<1> but was:<2>"
    assert e.signature == "test_failure|atest.one:# expected:<#> but was:<#>"


# ---------------------------------------------------------------------------------------------- synthetic formats
def test_synthetic_maven36_compile_with_ansi_and_relative_paths():
    text = load("synthetic-maven36-legacy-compile.log").replace("[1;31m", "\x1b[1;31m").replace("[m", "\x1b[m")
    errs = parse_maven_log(text, ROOT)
    assert [(e.file, e.line) for e in errs] == [
        ("src/main/java/com/acme/web/UserController.java", 21), ("src/main/java/com/acme/sec/WebSecurity.java", 30),
        ("src/main/java/com/acme/sec/WebSecurity.java", 12), ("src/main/java/com/acme/Old.java", 40),
        ("src/main/java/com/acme/Old.java", 44)]
    assert {e.goal for e in errs} == {"compiler:compile"} and {e.module for e in errs} == {"legacy-app"}
    assert [e.signature_generic for e in errs] == [
        "compile|cannot_be_applied|CrudRepository.findById",
        "compile|method_does_not_override",
        "compile|not_abstract_and_does_not_override|WebSecurityConfigurer.configure",
        "compile|unreported_exception|IOException",
        "compile|cannot_access|ServletException",
    ]
    assert "required: ID" in errs[0].raw and "reason: actual and formal argument lists differ" in errs[0].raw
    assert errs[2].signature.startswith("compile|not_abstract_and_does_not_override|* is not abstract")
    assert "class file for javax.servlet.ServletException not found" in errs[4].raw


def test_synthetic_kotlin():
    errs = parse("synthetic-kotlin.log")
    assert [(e.kind, e.file, e.line, e.column, e.goal) for e in errs] == [
        (ErrorKind.COMPILE, "src/main/kotlin/com/acme/Svc.kt", 12, 5, "kotlin:compile"),
        (ErrorKind.COMPILE, "src/main/kotlin/com/acme/Repo.kt", 7, 20, "kotlin:compile"),
    ]
    assert errs[0].signature_generic == "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"
    assert errs[1].signature_generic == "compile|cannot_find_symbol|javax"


def test_synthetic_plugin_execution_failures():
    errs = parse("synthetic-plugin-execution.log")
    assert [(e.kind, e.module, e.goal) for e in errs] == [
        (ErrorKind.PLUGIN_EXECUTION, "webapp", "war:war"),
        (ErrorKind.PLUGIN_EXECUTION, "batch", "resources:resources"),
    ]
    assert errs[1].message.startswith("filtering src/main/resources/app.properties to target/classes/app.properties")
    assert errs[1].signature == "plugin_execution|filtering <path> to <path> failed with malformedinputexception: input length = #"


def test_synthetic_unknown_failure_fallback():
    (e,) = parse("synthetic-unknown.log")
    assert e.kind == ErrorKind.UNKNOWN and e.message.startswith('Unknown lifecycle phase "comple"')
    assert "Scanning for projects" in e.raw


def test_synthetic_timestamps_and_windows_paths():
    (e,) = parse_maven_log(load("synthetic-timestamped-windows.log"), "C:\\work\\proj")
    assert (e.kind, e.file, e.line, e.column, e.module) == (ErrorKind.TEST_COMPILE, "src/test/java/com/acme/FooTest.java",
                                                           5, 8, "win-app")
    assert e.signature == "test_compile|static_import_only|static import only from classes and interfaces"


def test_build_failure_without_error_lines():
    (e,) = parse_maven_log("[INFO] Scanning\n[INFO] BUILD FAILURE\n", ROOT)
    assert (e.kind, e.message) == (ErrorKind.UNKNOWN, "BUILD FAILURE")


def test_empty_and_garbage_input():
    assert parse_maven_log("", ROOT) == []
    assert parse_maven_log("random\nlines\n[WARNING] x\n", None) == []


def test_compiler_failure_without_javac_lines_emits_one_compile_error():
    text = ("[INFO] --- compiler:3.13.0:testCompile (default-testCompile) @ x ---\n[INFO] BUILD FAILURE\n"
            "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:testCompile "
            "(default-testCompile) on project x: Compilation failure -> [Help 1]\n")
    (e,) = parse_maven_log(text, ROOT)
    assert (e.kind, e.goal, e.module) == (ErrorKind.TEST_COMPILE, "compiler:testCompile", "x")


def test_kind_from_path_when_goal_unknown():
    text = "[ERROR] /work/proj/m/src/test/java/A.java:[1,1] cannot find symbol\n[INFO] BUILD FAILURE\n"
    (e,) = parse_maven_log(text, ROOT)
    assert (e.kind, e.file, e.goal) == (ErrorKind.TEST_COMPILE, "m/src/test/java/A.java", "compiler:testCompile")


def test_import_bom_and_child_module_problems():
    text = ("[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
            "[ERROR] Non-resolvable import POM: The following artifacts could not be resolved: "
            "com.acme:acme-bom:pom:1.0 (absent): Could not find artifact com.acme:acme-bom:pom:1.0 in central "
            "(https://repo.maven.apache.org/maven2) @ line 20, column 19\n"
            "[ERROR] Child module /work/proj/web of /work/proj/pom.xml does not exist @ \n"
            "[WARNING] 'build.plugins.plugin.version' for org.apache.maven.plugins:maven-jar-plugin is missing.\n"
            " @ \n[ERROR] The build could not read 1 project -> [Help 1]\n")
    errs = parse_maven_log(text, ROOT)
    assert [e.kind for e in errs] == [ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.POM_INVALID]
    assert errs[0].artifact.coordinates == "com.acme:acme-bom:1.0"
    assert errs[1].message == "Child module web of pom.xml does not exist @"


def test_helpers():
    assert plugin_short_name("maven-compiler-plugin") == "compiler"
    assert plugin_short_name("kotlin-maven-plugin") == "kotlin"
    assert plugin_short_name("rewrite") == "rewrite"
    g = parse_coords("g:a:jar:tests:1.0")
    assert (g.type, g.classifier, g.version) == ("jar", "tests", "1.0")
    assert parse_coords("g:a").version is None
    assert strip_ansi("\x1b[1;31mERROR\x1b[m") == "ERROR"


def test_reactor_summary_failure_without_reactor():
    built, failed = parse_reactor_summary(load("real-compile-errors.log"))
    assert (built, failed) == ([], "compile-errors")
    assert parse_reactor_summary(load("real-bad-parent.log")) == ([], None)


def test_kind_for_windows_backslash_test_path():
    """A javac path that could not be relativised keeps its backslashes on Windows; it is still a test source."""
    from sbmig.build.log_parser import _Parser
    p = _Parser.__new__(_Parser)
    assert p._kind_for(r"D:\work\xxx\xxx-back\src\test\java\com\acme\FooTest.java", None) == ErrorKind.TEST_COMPILE
    assert p._kind_for(r"D:\work\xxx\src\main\java\com\acme\Foo.java", None) == ErrorKind.COMPILE
    assert p._kind_for("src/main/java/com/acme/Foo.java", "compiler:testCompile") == ErrorKind.TEST_COMPILE


# ------------------------------------------------------------- (h) a POM problem inherited from a parent pom (2026-11-05)
INHERITED_MISSING_VERSION = (
    "[INFO] Scanning for projects...\n"
    "[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
    "[ERROR] 'dependencies.dependency.version' for org.springframework:spring-dao:jar is missing. "
    "@ com.company:app:1.0, {root}pom.xml, line 88, column 21\n"
    " @ \n"
    "[ERROR] The build could not read 2 projects -> [Help 1]\n"
    "[ERROR]   \n"
    "[ERROR]   The project com.company:app-back:1.0 ({root}app-back{sep}pom.xml) has 1 error\n"
    "[ERROR]     'dependencies.dependency.version' for org.springframework:spring-dao:jar is missing. "
    "@ com.company:app:1.0, {root}pom.xml, line 88, column 21\n"
    "[ERROR]   \n"
    "[ERROR]   The project com.company:app-boot:1.0 ({root}app-boot{sep}pom.xml) has 1 error\n"
    "[ERROR]     'dependencies.dependency.version' for org.springframework:spring-dao:jar is missing. "
    "@ com.company:app:1.0, {root}pom.xml, line 88, column 21\n"
    "[ERROR]   \n"
    "[ERROR] -> [Help 1]\n"
)


@pytest.mark.parametrize("root, sep", [("/work/proj/", "/"), ("D:\\work\\proj\\", "\\")])
def test_inherited_missing_version_points_at_the_declaring_pom(root, sep):
    """The company run of 2026-11-05: `spring-dao` without a version sat in the ROOT pom; Maven reports the problem for
    every child module with `@ <parent gav>, <parent pom>, line N` — the error must name the parent pom (the file the
    fix edits), not the child module's pom, and carry the line."""
    (e,) = parse_maven_log(INHERITED_MISSING_VERSION.format(root=root, sep=sep), root.rstrip("/\\"))
    assert e.kind == ErrorKind.POM_INVALID and e.artifact.coordinates == "org.springframework:spring-dao"
    assert (e.file, e.line, e.column) == ("pom.xml", 88, 21)
    assert e.module == "app-back"                        # the first project Maven could not read
    assert e.signature_generic == "pom_invalid|missing_version"


def test_own_pom_problem_keeps_the_project_pom_and_takes_the_line():
    text = ("[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
            "[ERROR] 'dependencies.dependency.version' for com.acme:lib:jar is missing. @ line 12, column 21\n"
            " @ \n[ERROR] The build could not read 1 project -> [Help 1]\n"
            "[ERROR]   \n"
            "[ERROR]   The project com.acme:app-back:1.0 (/work/proj/app-back/pom.xml) has 1 error\n"
            "[ERROR]     'dependencies.dependency.version' for com.acme:lib:jar is missing. @ line 12, column 21\n")
    (e,) = parse_maven_log(text, ROOT)
    assert (e.file, e.line, e.column, e.module) == ("app-back/pom.xml", 12, 21, "app-back")

