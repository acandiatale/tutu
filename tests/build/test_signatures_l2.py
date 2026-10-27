"""Round 10 (B9): L2 is the case-catalog key (one case per L2; a hit applies the case's action), so it must not merge
different problems — and must stay free of paths, line numbers and project-owned FQCNs so it matches across
projects."""
from __future__ import annotations

import pytest

from sbmig.build import parse_maven_log, signature_for
from sbmig.core.models import BuildError, ErrorKind


def _c(msg: str, **kw) -> BuildError:
    return BuildError(**{"kind": ErrorKind.COMPILE, "message": msg, "file": "src/main/java/a/A.java", "line": 3,
                         **kw})


def l2(e: BuildError) -> str:
    return signature_for(e)[1]


@pytest.mark.parametrize("pkg", ["javax.xml.bind", "javax.xml.ws", "org.springframework.boot.autoconfigure.web",
                                 "org.springframework.boot.context.embedded", "kr.co.acme.common.util",
                                 "kr.co.acme.batch.job"])
def test_package_l2_is_the_whole_package(pkg):
    assert l2(_c(f"package {pkg} does not exist")) == f"compile|package_does_not_exist|{pkg}"


def test_package_l2_distinct_and_position_free():
    a = l2(_c("package javax.xml.bind does not exist", file="x/A.java", line=1, module="m1"))
    b = l2(_c("package javax.xml.ws does not exist"))
    assert a != b
    assert a == l2(_c("package javax.xml.bind does not exist", file="y/B.java", line=99, module="m2"))


def test_method_does_not_override_keyed_by_method():
    msg = "method does not override or implement a method from a supertype"
    a = _c(msg, symbol="method getErrorPath")
    b = _c(msg, symbol="method configure", file="src/main/java/b/Sec.java", line=30)
    assert l2(a) == "compile|method_does_not_override|getErrorPath"
    assert l2(b) == "compile|method_does_not_override|configure"
    assert l2(_c(msg)) == "compile|method_does_not_override"                 # source unreadable: bare class


def test_static_import_keyed_by_class_and_project_types_masked_in_l1():
    msg = "static import only from classes and interfaces"
    e = _c(msg, symbol="class org.junit.Assert")
    assert signature_for(e) == ("compile|static_import_only|static import only from classes and interfaces|"
                                "class org.junit.Assert", "compile|static_import_only|Assert")
    own = _c(msg, symbol="class com.mycorp.util.Consts")
    l1, l2_ = signature_for(own)
    assert "com.mycorp" not in l1 and l1.endswith("|class *") and l2_ == "compile|static_import_only|Consts"


@pytest.mark.parametrize("msg,key", [
    ("illegal start of expression", "illegal start of expression"),
    ("illegal start of type", "illegal start of type"),
    ("';' expected", "';' expected"),
    ("<identifier> expected", "<identifier> expected"),
    ("class, interface, enum, or record expected", "class, interface, enum, or record expected"),
    ("reached end of file while parsing", "reached end of file while parsing"),
])
def test_illegal_start_keyed_by_phrase(msg, key):
    assert l2(_c(msg)) == f"compile|illegal_start|{key}"


@pytest.mark.parametrize("message,goal,expected", [
    ("Error assembling WAR: webxml attribute is required (or pre-existing WEB-INF/web.xml if executing in update "
     "mode)", "war:war", "plugin_execution|war:war|error assembling war: webxml attribute is required"),
    ("Could not copy webapp sources [C:\\work\\proj\\src\\main\\webapp] -> [Help 1]", "war:war",
     "plugin_execution|war:war|could not copy webapp sources"),
    ("Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) on project web: "
     "Error assembling WAR: webxml attribute is required", None,
     "plugin_execution|war:war|error assembling war: webxml attribute is required"),
    ("Execution repackage of goal org.springframework.boot:spring-boot-maven-plugin:3.4.0:repackage failed: "
     "Unable to find a single main class from the following candidates [com.acme.App, com.acme.Tool]",
     "spring-boot:repackage", "plugin_execution|spring-boot:repackage|unable to find a single main class from the "
                              "following"),
    ("filtering /work/proj/src/main/resources/app.properties to /work/proj/target/classes/app.properties failed "
     "with MalformedInputException: Input length = 1", "resources:resources",
     "plugin_execution|resources:resources|filtering <path> to <path> failed with malformedinputexception: input "
     "length"),
    ("Duplicate class com.acme.billing.Invoice found in module", "shade:shade",
     "plugin_execution|shade:shade|duplicate class * found in module"),
])
def test_plugin_execution_l2_goal_and_cause(message, goal, expected):
    e = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, message=message, goal=goal, module="web")
    assert l2(e) == expected


def test_plugin_execution_l2_stable_across_projects():
    a = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, goal="war:war", module="shop-web",
                   message="Could not copy webapp sources [/home/a/shop/src/main/webapp]")
    b = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, goal="war:war", module="crm",
                   message="Could not copy webapp sources [D:\\b\\crm\\STATIC]")
    assert l2(a) == l2(b) == "plugin_execution|war:war|could not copy webapp sources"
    bare = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, message="one two three")
    assert l2(bare) == "plugin_execution|one two three"


# ------------------------------------------------------------------------- symbols read from the source line
OVERRIDES = """package com.acme.web;

import org.springframework.boot.web.servlet.error.ErrorController;

public class AppErrorController implements ErrorController {
    @Override
    public String getErrorPath() { return "/error"; }

    @Override @Deprecated
    @SuppressWarnings({"unchecked", "rawtypes"})
    protected
    java.util.List<String> configure(/* (legacy) */ String a) {
        return null;
    }

    @RequestMapping(value = "/x(y)", method = GET) @Override public void handle(int x) {}
}
"""
STATIC_IMPORTS = """package com.acme;

import static org.junit.Assert.*;
import static com.acme.util.Consts.MAX;
"""


def test_parser_reads_override_and_static_import_symbols(tmp_path):
    root = tmp_path / "proj"
    src = root / "src/main/java/com/acme/web"
    src.mkdir(parents=True)
    (src / "AppErrorController.java").write_text(OVERRIDES, encoding="utf-8")
    test = root / "src/test/java/com/acme"
    test.mkdir(parents=True)
    (test / "FooTest.java").write_bytes(("// 한글 주석\n" + STATIC_IMPORTS).encode("cp949"))
    f = f"{root}/src/main/java/com/acme/web/AppErrorController.java"
    t = f"{root}/src/test/java/com/acme/FooTest.java"
    log = (f"[INFO] --- compiler:3.13.0:compile (default-compile) @ app ---\n"
           f"[ERROR] {f}:[6,5] method does not override or implement a method from a supertype\n"
           f"[ERROR] {f}:[9,5] method does not override or implement a method from a supertype\n"
           f"[ERROR] {f}:[16,52] method does not override or implement a method from a supertype\n"
           f"[ERROR] {t}:[4,1] static import only from classes and interfaces\n"
           f"[ERROR] {t}:[5,1] static import only from classes and interfaces\n"
           f"[ERROR] {root}/src/main/java/Missing.java:[1,1] method does not override or implement a method from a "
           f"supertype\n[INFO] BUILD FAILURE\n")
    errs = parse_maven_log(log, root)
    assert [e.symbol for e in errs] == ["method getErrorPath", "method configure", "method handle",
                                        "class org.junit.Assert", "class com.acme.util.Consts", None]
    assert [e.signature_generic for e in errs] == [
        "compile|method_does_not_override|getErrorPath", "compile|method_does_not_override|configure",
        "compile|method_does_not_override|handle", "test_compile|static_import_only|Assert",
        "test_compile|static_import_only|Consts", "compile|method_does_not_override"]
    assert all(str(root) not in e.signature for e in errs)


def test_filtering_failures_share_one_case_whatever_the_file_or_path():
    """Company run 2026-10-27: Maven on Windows prints relative paths (`src\\main\\…\\a.otf`), which were not masked, so
    every font / data file under a filtered resource became its own case and the KB never matched. The plugin
    execution key is now the goal + the exception class (+ a few words), path-free."""
    from sbmig.build.signatures import normalize_message, signature_for
    from sbmig.core.models import BuildError, ErrorKind
    tail = "failed with MalformedInputException: Input length = 1"
    msgs = [r"filtering src\main\resources\static\fonts\NanumGothic.otf to target\classes\static\fonts\NanumGothic.otf " + tail,
            r"filtering src\main\resources\static\img\.dm\.temsdl.dmdb to target\classes\static\img\.dm\.temsdl.dmdb " + tail,
            r"filtering C:\w\app\src\main\resources\static\fonts\a.woff2 to C:\w\app\target\classes\static\fonts\a.woff2 " + tail,
            "filtering src/main/resources/static/fonts/b.ttf to target/classes/static/fonts/b.ttf " + tail.replace("= 1", "= 2")]
    keys = {signature_for(BuildError(kind=ErrorKind.PLUGIN_EXECUTION, message=m, module="app", goal="resources:resources",
                                     raw="[ERROR] " + m))[1] for m in msgs}
    assert keys == {"plugin_execution|resources:resources|malformedinputexception input length"}
    assert normalize_message(r"copy src\main\webapp\WEB-INF\web.xml failed") == "copy <path> failed"
    other = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, goal="war:war", module="app",
                       message="Error assembling WAR: webxml attribute is required (or pre-existing WEB-INF/web.xml if executing in update mode)")
    assert signature_for(other)[1].startswith("plugin_execution|war:war|error assembling war")   # no exception: as before
