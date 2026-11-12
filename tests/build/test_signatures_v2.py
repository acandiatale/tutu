"""Signature rules 2 (docs/INTERFACES.md "v2.1z-ai"): a member symbol is keyed by the library type that owns it;
every other L2 is byte-identical to rules 1."""
from __future__ import annotations

import pytest

from sbmig.build import signatures as sigmod
from sbmig.build.signatures import (
    SIGNATURE_VERSION,
    classes_changed_since,
    register_external_prefixes,
    signature_for,
)
from sbmig.core.models import GAV, BuildError, ErrorKind

C = ErrorKind.COMPILE


def _cfs(symbol, location=None, kind=C):
    return BuildError(kind=kind, message="cannot find symbol", file="src/main/java/com/example/A.java", line=7,
                      symbol=symbol, location=location)


def _nsm(message, raw="", kind=C):
    return BuildError(kind=kind, message=message, file="src/main/java/com/example/A.java", line=7, raw=raw)


def l2(err):
    return signature_for(err)[1]


def l1(err):
    return signature_for(err)[0]


# --------------------------------------------------------------------------------------------- cannot_find_symbol
def test_member_on_library_variable_type_gets_the_owner():
    e = _cfs("method keySet()", "variable headers of type org.springframework.http.HttpHeaders")
    assert l2(e) == "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet"


def test_member_on_project_variable_type_keeps_rules1_key():
    e = _cfs("method save(com.example.members.domain.Member)",
             "variable repository of type com.example.members.repository.MemberRepository")
    assert l2(e) == "compile|cannot_find_symbol|save"


def test_variable_symbol_at_project_class_keeps_rules1_key():
    e = _cfs("variable JAXBContext", "class com.example.inventory.io.XmlExporter")
    assert l2(e) == "compile|cannot_find_symbol|JAXBContext"


def test_class_symbol_import_and_usage_are_one_unchanged_case():
    imp = _cfs("class ConfigAttribute", "package org.springframework.security.access")
    use = _cfs("class ConfigAttribute", "class com.example.ops.security.DbUrlSecurityMetadataSource")
    lib = _cfs("class ConfigAttribute", "class org.springframework.security.access.SecurityConfig")
    assert l2(imp) == l2(use) == l2(lib) == "compile|cannot_find_symbol|ConfigAttribute"


@pytest.mark.parametrize("symbol", ["interface Foo", "enum Foo", "record Foo", "annotation Foo", "type Foo", "Foo"])
def test_other_class_like_symbols_ignore_a_library_location(symbol):
    e = _cfs(symbol, "class org.springframework.web.Thing")
    assert l2(e) == "compile|cannot_find_symbol|Foo"


def test_variable_symbol_at_library_class_gets_the_owner():
    e = _cfs("variable FOO", "class org.apache.commons.lang.StringUtils")
    assert l2(e) == "compile|cannot_find_symbol|org.apache.commons.lang.StringUtils.FOO"


@pytest.mark.parametrize("kw", ["interface", "enum", "record", "@interface"])
def test_other_type_locations(kw):
    e = _cfs("method value()", f"{kw} org.springframework.web.bind.annotation.RequestMapping")
    assert l2(e) == "compile|cannot_find_symbol|org.springframework.web.bind.annotation.RequestMapping.value"


def test_static_method_and_constructor_are_members():
    e = _cfs("static method of(int)", "class java.util.List")
    assert l2(e) == "compile|cannot_find_symbol|java.util.List.of"
    e = _cfs("constructor HttpHeaders(java.util.Map)", "class org.springframework.http.HttpHeaders")
    assert l2(e) == "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.HttpHeaders"


def test_generic_arguments_are_stripped_from_the_location_type():
    e = _cfs("method putIfAbsentX(java.lang.String)",
             "variable m of type java.util.Map<java.lang.String,java.util.List<java.lang.String>>")
    assert l2(e) == "compile|cannot_find_symbol|java.util.Map.putIfAbsentX"
    spaced = _cfs("method putIfAbsentX(java.lang.String)",
                  "variable m of type java.util.Map<java.lang.String, java.util.List<java.lang.String>>")
    assert l2(spaced) == l2(e)
    cls = _cfs("method foo()", "class org.springframework.data.domain.Page<com.example.Member>")
    assert l2(cls) == "compile|cannot_find_symbol|org.springframework.data.domain.Page.foo"


def test_array_type_is_stripped():
    e = _cfs("variable size", "variable arr of type java.lang.String[]")
    assert l2(e) == "compile|cannot_find_symbol|java.lang.String.size"
    e = _cfs("variable size", "variable arr of type java.util.List<java.lang.String>[][]")
    assert l2(e) == "compile|cannot_find_symbol|java.util.List.size"


@pytest.mark.parametrize("location", [
    None, "", "package org.springframework.http", "variable t of type T", "class T",
    "variable x of type capture#1 of ? extends java.lang.Number",
    "variable x of type com.example.app.Thing", "class com.example.app.Thing",
])
def test_member_without_a_library_owner_keeps_rules1_key(location):
    e = _cfs("method keySet()", location)
    assert l2(e) == "compile|cannot_find_symbol|keySet"


def test_test_compile_has_its_own_prefix():
    e = _cfs("method keySet()", "variable headers of type org.springframework.http.HttpHeaders",
             kind=ErrorKind.TEST_COMPILE)
    assert l2(e) == "test_compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet"


def test_l1_is_unchanged_by_rules2():
    e = _cfs("method keySet()", "variable headers of type org.springframework.http.HttpHeaders")
    assert l1(e) == ("compile|cannot_find_symbol|method keySet()|"
                     "variable of type org.springframework.http.HttpHeaders")


def test_kotlin_unresolved_reference_unchanged():
    e = BuildError(kind=C, message="Unresolved reference: keySet", file="src/main/kotlin/A.kt", line=3,
                   location="variable headers of type org.springframework.http.HttpHeaders")
    assert l2(e) == "compile|cannot_find_symbol|keySet"


@pytest.fixture
def in_house_prefix():
    before = set(sigmod._EXTRA_EXTERNAL_PREFIXES)
    register_external_prefixes(["com.inhouse.lib"])
    yield
    sigmod._EXTRA_EXTERNAL_PREFIXES.clear()
    sigmod._EXTRA_EXTERNAL_PREFIXES.update(before)


def test_registered_in_house_prefix_is_library_owned(in_house_prefix):
    e = _cfs("method token()", "variable c of type com.inhouse.lib.auth.AuthClient")
    assert l2(e) == "compile|cannot_find_symbol|com.inhouse.lib.auth.AuthClient.token"
    e = _nsm("no suitable method found for call(int)",
             "    method com.inhouse.lib.auth.AuthClient.call(java.lang.String) is not applicable")
    assert l2(e) == "compile|no_suitable_method|com.inhouse.lib.auth.AuthClient.call"


def test_without_registration_the_in_house_type_is_project_owned():
    e = _cfs("method token()", "variable c of type com.inhouse.lib.auth.AuthClient")
    assert l2(e) == "compile|cannot_find_symbol|token"


# --------------------------------------------------------------------------------------------- no_suitable_method
_FINDALL_RAW = (
    "[ERROR] module/comp-jar/src/main/java/com/example/members/service/MemberService.java:[45,19] "
    "no suitable method found for findAll(no arguments)\n"
    "    method org.springframework.data.repository.PagingAndSortingRepository.findAll("
    "org.springframework.data.domain.Sort) is not applicable\n"
    "      (actual and formal argument lists differ in length)\n"
    "    method org.springframework.data.repository.PagingAndSortingRepository.findAll("
    "org.springframework.data.domain.Pageable) is not applicable\n"
    "      (actual and formal argument lists differ in length)")


def test_no_suitable_method_fqcn_candidate():
    e = _nsm("no suitable method found for findAll(no arguments)", _FINDALL_RAW)
    assert l2(e) == "compile|no_suitable_method|org.springframework.data.repository.PagingAndSortingRepository.findAll"


def test_no_suitable_method_generic_method_candidate():
    raw = ("no suitable method found for toArray(int)\n"
           "    method java.util.Collection.<T>toArray(java.util.function.IntFunction<T[]>) is not applicable\n"
           "    method java.util.List.<T>toArray(T[]) is not applicable\n")
    e = _nsm("no suitable method found for toArray(int)", raw)
    assert l2(e) == "compile|no_suitable_method|java.util.Collection.toArray"


def test_no_suitable_constructor_candidate():
    raw = ("[ERROR] A.java:[3,9] no suitable constructor found for File(no arguments)\n"
           "[ERROR]     constructor java.io.File.File(java.lang.String) is not applicable\n")
    e = _nsm("no suitable constructor found for File(no arguments)", raw)
    assert l2(e) == "compile|no_suitable_method|java.io.File.File"


def test_no_suitable_method_first_library_candidate_wins_over_a_project_one():
    raw = ("no suitable method found for findAll(int)\n"
           "    method com.example.repo.BaseRepository.findAll(java.lang.String) is not applicable\n"
           "    method org.springframework.data.repository.PagingAndSortingRepository.findAll("
           "org.springframework.data.domain.Sort) is not applicable\n")
    e = _nsm("no suitable method found for findAll(int)", raw)
    assert l2(e) == "compile|no_suitable_method|org.springframework.data.repository.PagingAndSortingRepository.findAll"


def test_no_suitable_method_project_candidate_unchanged():
    raw = ("no suitable method found for save(int)\n"
           "    method com.example.repo.BaseRepository.save(com.example.Member) is not applicable\n")
    e = _nsm("no suitable method found for save(int)", raw)
    assert l2(e) == "compile|no_suitable_method|save"


def test_no_suitable_method_without_candidates_unchanged():
    assert l2(_nsm("no suitable method found for of(int)")) == "compile|no_suitable_method|of"
    assert l2(_nsm("no suitable method found for of(int)", "no suitable method found for of(int)")) \
        == "compile|no_suitable_method|of"


def test_no_suitable_method_candidate_of_another_name_is_ignored():
    raw = ("no suitable method found for of(int)\n"
           "    method java.util.List.copyOf(java.util.Collection) is not applicable\n")
    assert l2(_nsm("no suitable method found for of(int)", raw)) == "compile|no_suitable_method|of"


def test_no_suitable_method_test_compile_prefix():
    e = _nsm("no suitable method found for findAll(no arguments)", _FINDALL_RAW, kind=ErrorKind.TEST_COMPILE)
    assert l2(e) == \
        "test_compile|no_suitable_method|org.springframework.data.repository.PagingAndSortingRepository.findAll"


# --------------------------------------------------------------------------------------------- unchanged classes
_F = dict(file="src/main/java/com/example/A.java", line=3)
# Expected L2s captured from signature_for BEFORE rules 2 (rules 1): ids outside the two classes never move.
_UNCHANGED = [
    (dict(kind=C, message="package javax.xml.bind does not exist", **_F),
     "compile|package_does_not_exist|javax.xml.bind"),
    (dict(kind=C, message="cannot access javax.servlet.Filter", **_F), "compile|cannot_access|Filter"),
    (dict(kind=C, message="incompatible types: java.lang.String cannot be converted to "
                          "java.util.Optional<java.lang.String>", **_F),
     "compile|incompatible_types|String->Optional"),
    (dict(kind=C, message="illegal start of type", **_F), "compile|illegal_start|illegal start of type"),
    (dict(kind=C, message="method does not override or implement a method from a supertype",
          symbol="method getErrorPath", **_F), "compile|method_does_not_override|getErrorPath"),
    (dict(kind=C, message="static import only from classes and interfaces", symbol="class org.junit.Assert", **_F),
     "compile|static_import_only|Assert"),
    (dict(kind=C, message="com.example.web.ErrCtl is not abstract and does not override abstract method "
                          "getErrorPath() in org.springframework.boot.web.servlet.error.ErrorController", **_F),
     "compile|not_abstract_and_does_not_override|ErrorController.getErrorPath"),
    (dict(kind=C, message="method encode in class sun.misc.BASE64Encoder cannot be applied to given types;",
          raw="    method sun.misc.BASE64Encoder.encode(byte[]) is not applicable", **_F),
     "compile|cannot_be_applied|BASE64Encoder.encode"),
    (dict(kind=C, message="unreported exception java.io.IOException; must be caught or declared to be thrown", **_F),
     "compile|unreported_exception|IOException"),
    (dict(kind=C, message="cannot inherit from final org.example.Foo", **_F), "compile|cannot_inherit_from_final|Foo"),
    (dict(kind=C, message="getFoo() in org.springframework.Foo has been deprecated and marked for removal", **_F),
     "compile|deprecated_removed|Foo.getFoo"),
    (dict(kind=C, message="class file for javax.servlet.ServletException not found", **_F),
     "compile|class_not_found_in_package|ServletException"),
    (dict(kind=C, message="foo has private access in org.springframework.Bar", **_F),
     "compile|has_private_access|Bar.foo"),
    (dict(kind=C, message="has private access", **_F), "compile|other|has private access"),
    (dict(kind=C, message="Compilation failure"), "compile|build|compilation failure"),
    (dict(kind=C, message="annotation-processor: java.lang.NoSuchFieldError: "
                          "com.sun.tools.javac.tree.JCTree qualid", raw="lombok"),
     "compile|annotation_processor|lombok"),
    (dict(kind=ErrorKind.TEST_COMPILE, message="package org.junit does not exist", **_F),
     "test_compile|package_does_not_exist|org.junit"),
    (dict(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact",
          artifact=GAV(group_id="javax.xml.bind", artifact_id="jaxb-api", version="2.3.1")),
     "dependency_resolution|javax.xml.bind:jaxb-api"),
    (dict(kind=ErrorKind.PARENT_RESOLUTION, message="Non-resolvable parent POM",
          artifact=GAV(group_id="com.example", artifact_id="base-parent", version="1.0.0-SNAPSHOT")),
     "parent_resolution|com.example:base-parent"),
    (dict(kind=ErrorKind.PLUGIN_EXECUTION,
          message="Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) on "
                  "project demo-web: Error assembling WAR: webxml attribute is required (or pre-existing "
                  "WEB-INF/web.xml if executing in update mode)"),
     "plugin_execution|war:war|error assembling war: webxml attribute is required"),
    (dict(kind=ErrorKind.POM_INVALID, message="'dependencies.dependency.version' for org.foo:bar:jar is missing.",
          artifact=GAV(group_id="org.foo", artifact_id="bar")), "pom_invalid|missing_version"),
    (dict(kind=ErrorKind.POM_INVALID, message="Non-parseable POM /x/pom.xml: unexpected end tag at line 12"),
     "pom_invalid|non-parseable pom <path>: unexpected end tag"),
    (dict(kind=ErrorKind.UNKNOWN, message="Some odd thing happened 12 times"),
     "unknown|some odd thing happened # times"),
]


@pytest.mark.parametrize("fields,expected", _UNCHANGED, ids=[x[1][:60] for x in _UNCHANGED])
def test_l2_outside_the_two_classes_is_unchanged(fields, expected):
    assert l2(BuildError(**fields)) == expected


# --------------------------------------------------------------------------------------------- versions
def test_rule_version_and_changed_classes():
    assert SIGNATURE_VERSION == 2
    both = {"cannot_find_symbol", "no_suitable_method"}
    assert classes_changed_since(0) == classes_changed_since(1) == both
    assert classes_changed_since(2) == frozenset()
