from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path
from sbmig.core.models import GAV, CaseType, LibraryUsage, PluginInfo, PomInfo, ProjectProfile, SourceStats
from sbmig.plan import HANDLER_IDS, compare_versions, parse_version, pre_handlers_for, version_lt
from tests.analyze.helpers import make_boot27, make_boot33, make_servlet


def _profile(libs=(), poms=(), javax=None) -> ProjectProfile:
    return ProjectProfile(root="/x", name="x", case_type=CaseType.PURE_JAVA, libraries=list(libs), poms=list(poms),
                          source=SourceStats(javax_imports=javax or {}))


def _pom(deps=(), plugins=()) -> PomInfo:
    return PomInfo(path="pom.xml", artifact_id="x", dependencies=list(deps), plugins=list(plugins))


GOOD_COMPILER = PluginInfo(artifact_id="maven-compiler-plugin", version="3.13.0")


def test_boot27_handlers(tmp_path: Path, cfg) -> None:
    ids = pre_handlers_for(analyze_path(make_boot27(tmp_path), cfg), cfg)
    assert ids == ["lombok-version-floor", "maven-compiler-plugin-floor", "remove-classic-loader-implementation",
                   "mysql-connector-relocation"]
    assert set(ids) <= set(HANDLER_IDS)


def test_servlet_handlers(tmp_path: Path, cfg) -> None:
    ids = pre_handlers_for(analyze_path(make_servlet(tmp_path), cfg), cfg)
    assert ids == ["maven-compiler-plugin-floor", "javax-servlet-api-to-jakarta", "jstl-to-jakarta"]   # 9c: javax jstl


def test_boot33_no_compiler_declared(tmp_path: Path, cfg) -> None:
    assert pre_handlers_for(analyze_path(make_boot33(tmp_path), cfg), cfg) == ["maven-compiler-plugin-floor"]


@pytest.mark.parametrize("libs,expected", [
    ([LibraryUsage(name="lombok", version="1.18.30")], []),
    ([LibraryUsage(name="lombok")], ["lombok-version-floor"]),
    ([LibraryUsage(name="lombok", version="${lombok.version}")], ["lombok-version-floor"]),
    ([LibraryUsage(name="mapstruct", version="1.5.5.Final")], ["mapstruct-version-floor"]),
    ([LibraryUsage(name="mapstruct", version="1.6.3")], []),
    ([LibraryUsage(name="mapstruct")], []),
    ([LibraryUsage(name="querydsl")], ["querydsl-jakarta-classifier"]),
])
def test_library_floors(libs, expected, cfg) -> None:
    assert pre_handlers_for(_profile(libs, [_pom(plugins=[GOOD_COMPILER])]), cfg) == expected


def test_plugin_and_dependency_conditions(cfg) -> None:
    p = _pom(deps=[GAV.parse("javax.servlet:servlet-api:2.5")],
             plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.8.1"),
                      PluginInfo(artifact_id="maven-surefire-plugin", version="2.22.2"),
                      PluginInfo(artifact_id="maven-failsafe-plugin", version="3.2.5"),
                      PluginInfo(group_id="org.springframework.boot", artifact_id="spring-boot-maven-plugin",
                                 configuration={"loaderImplementation": "classic"})])
    ids = pre_handlers_for(_profile(poms=[p], javax={"javax.xml.bind": 2}), cfg)
    assert ids == ["maven-compiler-plugin-floor", "surefire-plugin-floor", "javax-servlet-api-to-jakarta",
                   "jaxb-runtime-add", "remove-classic-loader-implementation"]
    assert pre_handlers_for(_profile(poms=[_pom(plugins=[GOOD_COMPILER])]), cfg) == []
    assert pre_handlers_for(_profile(), cfg) == []                   # no poms → nothing to floor


def test_versions() -> None:
    assert version_lt("1.18.24", "1.18.30") is True
    assert version_lt("3.8.1", "3.13.0") is True
    assert compare_versions("2.7.18.RELEASE", "2.7.18") == 0
    assert compare_versions("3.0.0-M7", "3.0.0") == -1
    assert compare_versions("1.6.0.Beta1", "1.6.0") == -1
    assert compare_versions("3.2", "3.2.0") == 0
    assert compare_versions("33.4.8-jre", "33.4.8") == 0
    assert compare_versions("10", "9") == 1
    assert compare_versions(None, "1") is None and version_lt("${x}", "1") is None
    assert parse_version("abc") is None
    assert compare_versions("1.0.0.Final", "1.0.0-rc1") == 1


@pytest.mark.parametrize("configuration,expected", [
    ({"fork": "true", "executable": "/usr/java/jdk1.6/bin/javac"}, ["compiler-legacy-config"]),
    ({"compilerVersion": "1.6"}, ["compiler-legacy-config"]),
    ({"compilerArguments": ["${java.home}/lib/rt.jar"]}, ["compiler-legacy-config"]),      # flat config keeps values only
    ({"compilerArgument": "-bootclasspath ${java.home}/lib/rt.jar"}, ["compiler-legacy-config"]),
    ({"compilerArgs": ["-Xlint:all", "-Xbootclasspath/p:lib/patch.jar"]}, ["compiler-legacy-config"]),
    ({"compilerArgs": ["-Xlint:all", "-parameters"]}, []),
    ({"fork": "false", "encoding": "MS949"}, []),
    ({"release": "21"}, []),
])
def test_compiler_legacy_config_selection(configuration, expected, cfg) -> None:
    """Round 9: fork/executable/compilerVersion/bootclasspath-style compiler config selects the handler."""
    pom = _pom(plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.13.0", configuration=configuration)])
    assert pre_handlers_for(_profile(poms=[pom]), cfg) == expected



def test_jstl_to_jakarta_selected_from_the_pom(cfg) -> None:
    """Round 9c: a javax JSTL declaration selects the pre-emptive relocation (spring phase)."""
    p = _pom(deps=[GAV.parse("javax.servlet:jstl:1.2")], plugins=[GOOD_COMPILER])
    assert pre_handlers_for(_profile(poms=[p]), cfg) == ["jstl-to-jakarta"]
    assert pre_handlers_for(_profile(poms=[_pom(deps=[GAV.parse("taglibs:standard:1.1.2")], plugins=[GOOD_COMPILER])]),
                            cfg) == ["jstl-to-jakarta"]
