from __future__ import annotations

import pytest

from sbmig.build import classify_javac, normalize_message, signature_for
from sbmig.core.models import GAV, BuildError, ErrorKind


@pytest.mark.parametrize("msg,cls", [
    ("cannot find symbol", "cannot_find_symbol"),
    ("package javax.servlet does not exist", "package_does_not_exist"),
    ("incompatible types: int cannot be converted to java.lang.String", "incompatible_types"),
    ("method does not override or implement a method from a supertype", "method_does_not_override"),
    ("Foo is not abstract and does not override abstract method bar() in Baz", "not_abstract_and_does_not_override"),
    ("name clash: x in A does not override abstract method y", "does_not_override_abstract"),
    ("cannot access javax.servlet.ServletException", "cannot_access"),
    ("no suitable method found for of(int)", "no_suitable_method"),
    ("constructor Foo in class Foo cannot be applied to given types;", "cannot_be_applied"),
    ("unreported exception java.io.IOException; must be caught or declared to be thrown", "unreported_exception"),
    ("static import only from classes and interfaces", "static_import_only"),
    ("secret has private access in com.x.Y", "has_private_access"),
    ("getOne(ID) in JpaRepository has been deprecated and marked for removal", "deprecated_removed"),
    ("Impl is not public in com.x; cannot be accessed from outside package", "is_not_public"),
    ("class file for javax.annotation.Nullable not found", "class_not_found_in_package"),
    ("cannot inherit from final java.lang.String", "cannot_inherit_from_final"),
    ("illegal start of expression", "illegal_start"),
    ("';' expected", "illegal_start"),
    ("Unresolved reference: Foo", "cannot_find_symbol"),
    ("something javac never said", "other"),
])
def test_classify_javac(msg, cls):
    assert classify_javac(msg) == cls


def test_normalize_message_strips_machine_specifics():
    msg = ("Failed at /home/ci/work/proj/src/main/resources/app.properties on project my-svc using "
           "C:\\work\\x.txt, version 1.2.3 range [1.8,9) url https://nexus.local/repo @0x74a820bf "
           "@ line 4, column 27  and  42 things")
    out = normalize_message(msg, module="my-svc")
    assert out == ("failed at <path> on project <module> using <path>, version <v> range <range> url <url> <hex> "
                   "and # things")
    assert len(normalize_message("x " * 400)) <= 160
    assert normalize_message("Keep Case", lower=False) == "Keep Case"


def _e(**kw) -> BuildError:
    return BuildError(**{"kind": ErrorKind.COMPILE, "message": "cannot find symbol", "file": "A.java", "line": 1, **kw})


def test_architecture_examples():
    e = _e(symbol="class WebSecurityConfigurerAdapter",
           location="package org.springframework.security.config.annotation.web.configuration")
    assert signature_for(e) == (
        "compile|cannot_find_symbol|class WebSecurityConfigurerAdapter|package "
        "org.springframework.security.config.annotation.web.configuration",
        "compile|cannot_find_symbol|WebSecurityConfigurerAdapter")
    p = _e(message="package javax.servlet.http does not exist")
    assert signature_for(p) == ("compile|package_does_not_exist|javax.servlet.http",
                                "compile|package_does_not_exist|javax.servlet.http")


def test_location_masking_and_variable_types():
    e = _e(symbol="method antMatchers(java.lang.String)",
           location="variable http of type org.springframework.security.config.annotation.web.builders.HttpSecurity")
    l1, l2 = signature_for(e)
    assert l1.endswith("|variable of type org.springframework.security.config.annotation.web.builders.HttpSecurity")
    assert l2 == "compile|cannot_find_symbol|antMatchers"
    own = _e(symbol="variable foo", location="variable x of type com.mycorp.Thing")
    assert signature_for(own)[0] == "compile|cannot_find_symbol|variable foo|variable of type *"
    assert signature_for(_e(symbol="static method of(int)", location="interface java.util.List"))[1] == \
        "compile|cannot_find_symbol|of"


def test_signature_is_independent_of_position_file_and_module():
    a = _e(symbol="class Foo", file="a/A.java", line=3, column=4, module="m1")
    b = _e(symbol="class Foo", file="b/B.java", line=99, column=1, module="m2")
    assert signature_for(a) == signature_for(b)


@pytest.mark.parametrize("kind", [ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION,
                                  ErrorKind.PLUGIN_RESOLUTION])
def test_resolution_signatures(kind):
    e = BuildError(kind=kind, message="whatever /abs/path", artifact=GAV.parse("g:a:1.0"))
    assert signature_for(e) == (f"{kind.value}|g:a:1.0", f"{kind.value}|g:a")
    no_version = BuildError(kind=kind, message="x", artifact=GAV.parse("g:a"))
    assert signature_for(no_version) == (f"{kind.value}|g:a", f"{kind.value}|g:a")
    no_artifact = BuildError(kind=kind, message="Could not resolve 1.2.3 things")
    assert signature_for(no_artifact) == (f"{kind.value}|could not resolve <v> things",
                                          f"{kind.value}|could not resolve <v> things")


def test_message_kinds_generic_is_first_six_words():
    e = BuildError(kind=ErrorKind.ENFORCER, message="one two three four five six seven eight", module="svc")
    assert signature_for(e) == ("enforcer|one two three four five six seven eight",
                                "enforcer|one two three four five six")


def test_other_javac_classes_l2_keys():
    cases = {
        "secret has private access in com.x.Vault": "compile|has_private_access|Vault.secret",
        "Impl is not public in com.x; cannot be accessed from outside package": "compile|is_not_public|Impl",
        "cannot inherit from final java.lang.String": "compile|cannot_inherit_from_final|String",
        "no suitable method found for of(int)": "compile|no_suitable_method|of",
        "getOne(ID) in JpaRepository has been deprecated and marked for removal":
            "compile|deprecated_removed|JpaRepository.getOne",
        "class file for javax.annotation.Nullable not found": "compile|class_not_found_in_package|Nullable",
        "illegal start of expression": "compile|illegal_start|illegal start of expression",
        "weird thing happened here today ok yes": "compile|other|weird thing happened here today ok",
    }
    for msg, l2 in cases.items():
        assert signature_for(_e(message=msg))[1] == l2, msg


def test_test_compile_prefix_and_annotation_processor_without_lombok():
    e = _e(kind=ErrorKind.TEST_COMPILE, message="package org.junit does not exist")
    assert signature_for(e)[0] == "test_compile|package_does_not_exist|org.junit"
    ap = BuildError(kind=ErrorKind.COMPILE, message="annotation-processor: java.lang.IllegalStateException: boom")
    assert signature_for(ap)[1] == "compile|annotation_processor|IllegalStateException"
