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
    assert ids == ["lombok-version-floor", "maven-compiler-plugin-floor", "jakarta-namespace-sweep",
                   "boot4-type-relocation-sweep",                                   # round 10: Boot as-is project
                   "spring-data-crud-repository", "remove-classic-loader-implementation", "mysql-connector-relocation"]
    assert set(ids) <= set(HANDLER_IDS)


def test_servlet_handlers(tmp_path: Path, cfg) -> None:
    ids = pre_handlers_for(analyze_path(make_servlet(tmp_path), cfg), cfg)
    assert ids == ["maven-compiler-plugin-floor", "javax-servlet-api-to-jakarta", "jstl-to-jakarta",   # 9c: javax jstl
                   "jakarta-namespace-sweep"]                                                     # E13: javax.servlet


def test_boot33_no_compiler_declared(tmp_path: Path, cfg) -> None:
    assert pre_handlers_for(analyze_path(make_boot33(tmp_path), cfg), cfg) == ["maven-compiler-plugin-floor",
                                                                                 "boot4-type-relocation-sweep"]


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
                   "jaxb-runtime-add", "jakarta-namespace-sweep", "remove-classic-loader-implementation"]
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


def test_resources_binary_filtering_selection(cfg) -> None:
    from sbmig.core.models import PropertyInventory
    from sbmig.plan.pre_handlers import pre_handler_reasons
    prof = _profile(poms=[_pom(plugins=[GOOD_COMPILER])])
    assert "resources-binary-filtering" not in pre_handlers_for(prof, cfg)
    prof.properties = PropertyInventory(filtered_binary_files=["src/main/resources/static/fonts/fa.woff2",
                                                               "src/main/resources/cert/app.jks"])
    reasons = dict(pre_handler_reasons(prof, cfg))
    assert "resources-binary-filtering" in reasons
    assert "woff2" in reasons["resources-binary-filtering"] and "jks" in reasons["resources-binary-filtering"]
    assert pre_handlers_for(prof, cfg)[-1] == "resources-binary-filtering"


def test_strip_pom_repositories_selection_is_gated_by_config(cfg) -> None:
    from sbmig.plan.pre_handlers import pre_handler_reasons
    prof = _profile(poms=[_pom(plugins=[GOOD_COMPILER])])
    prof.poms[0].repositories = ["http://old-nexus.corp/repo"]
    assert "strip-pom-repositories" not in pre_handlers_for(prof, cfg)                 # keep (default)
    on = cfg.model_copy(deep=True)
    on.build.pom_repositories = "remove"
    reasons = dict(pre_handler_reasons(prof, on))
    assert "old-nexus.corp" in reasons["strip-pom-repositories"]
    prof.poms[0].repositories = []
    assert "strip-pom-repositories" not in pre_handlers_for(prof, on)                  # nothing declared


@pytest.mark.parametrize("deps,expected", [
    (["org.qlrm:qlrm:2.1.1"], ["jakarta-library-floor"]),
    (["org.qlrm:qlrm:4.0.1"], []),
    (["org.qlrm:qlrm:4.2.1"], []),
    (["org.qlrm:qlrm"], ["jakarta-library-floor"]),                                  # version unknown
    (["com.google.guava:guava:33.4.8-jre"], []),
])
def test_jakarta_library_floor_selection(deps, expected, cfg) -> None:
    """X1b: a table library (knowledge/libraries/jakarta_libraries.yml) declared below its first jakarta release."""
    from sbmig.plan.pre_handlers import pre_handler_reasons
    prof = _profile(poms=[_pom(deps=[GAV.parse(d) for d in deps], plugins=[GOOD_COMPILER])])
    assert pre_handlers_for(prof, cfg) == expected
    if expected:
        reason = dict(pre_handler_reasons(prof, cfg))["jakarta-library-floor"]
        assert reason.startswith("org.qlrm:qlrm declared below their first jakarta release (org.qlrm:qlrm ")
        assert reason.endswith(" < 4.0.1)")


def test_jakarta_library_floor_property_resolved(cfg) -> None:
    pom = PomInfo(path="pom.xml", artifact_id="x", properties={"qlrm.version": "2.1.1"},
                  dependencies=[GAV.parse("org.qlrm:qlrm:${qlrm.version}")], plugins=[GOOD_COMPILER])
    from sbmig.plan.pre_handlers import pre_handler_reasons
    assert dict(pre_handler_reasons(_profile(poms=[pom]), cfg))["jakarta-library-floor"].endswith(
        "(org.qlrm:qlrm 2.1.1 < 4.0.1)")
    pom.properties["qlrm.version"] = "4.2.1"
    assert pre_handlers_for(_profile(poms=[pom]), cfg) == []


def test_dependency_relocation_selected_for_javax_api_artifacts(cfg) -> None:
    from sbmig.plan.pre_handlers import javax_api_relocation_keys, pre_handler_reasons
    keys = javax_api_relocation_keys()
    assert "javax.persistence:javax.persistence-api" in keys and "com.sun.mail:javax.mail" in keys
    assert not any(k.startswith(("org.springframework.boot:", "org.hibernate:", "javax.servlet:")) for k in keys)
    p = _pom(deps=[GAV.parse("javax.persistence:javax.persistence-api:2.2"),
                   GAV.parse("javax.validation:validation-api:2.0.1.Final")], plugins=[GOOD_COMPILER])
    reasons = dict(pre_handler_reasons(_profile(poms=[p]), cfg))
    assert reasons["dependency-relocation"] == (
        "javax API artifact(s) declared: javax.persistence:javax.persistence-api, javax.validation:validation-api "
        "→ jakarta coordinates after the spring recipes")
    # servlet / jstl are covered by their own handlers; Boot starters / hibernate rows never select it
    for d in ("javax.servlet:javax.servlet-api:4.0.1", "javax.servlet:jstl:1.2",
              "org.springframework.boot:spring-boot-starter-web:2.7.18", "org.hibernate:hibernate-core:5.6.15.Final"):
        assert "dependency-relocation" not in pre_handlers_for(_profile(poms=[_pom(deps=[GAV.parse(d)],
                                                                                   plugins=[GOOD_COMPILER])]), cfg)


def test_jakarta_library_floor_selected_for_old_coordinates(cfg) -> None:
    from sbmig.plan.pre_handlers import pre_handler_reasons
    prof = _profile(poms=[_pom(deps=[GAV.parse("ch.simas.qlrm:qlrm:1.7.1")], plugins=[GOOD_COMPILER])])
    assert pre_handlers_for(prof, cfg) == ["jakarta-library-floor"]
    assert dict(pre_handler_reasons(prof, cfg))["jakarta-library-floor"] == (
        "ch.simas.qlrm:qlrm declared below their first jakarta release "
        "(ch.simas.qlrm:qlrm 1.7.1 → org.qlrm:qlrm < 4.0.1)")


def test_jakarta_library_floor_selected_for_missing_classifier(cfg) -> None:
    """Ehcache 3 at a jakarta-line version but without the `jakarta` classifier is still the javax build."""
    from sbmig.plan.pre_handlers import jakarta_library_reason
    plain = _profile(poms=[_pom(deps=[GAV.parse("org.ehcache:ehcache:3.12.0")])])
    r = jakarta_library_reason(plain)
    assert r and "org.ehcache:ehcache" in r and "without classifier jakarta" in r
    ok = _profile(poms=[_pom(deps=[GAV(group_id="org.ehcache", artifact_id="ehcache", version="3.12.0",
                                       classifier="jakarta")])])
    assert jakarta_library_reason(ok) is None


def test_jakarta_sweep_selected_for_jsp_only_projects(cfg) -> None:
    """2026-10-23: JSPs with javax page imports / scriptlets are swept even when no Java file imports javax EE."""
    from sbmig.plan.pre_handlers import pre_handler_reasons
    prof = _profile(poms=[_pom()])
    prof.source.jsp_javax_files = 3
    reasons = dict(pre_handler_reasons(prof, cfg))
    assert "jakarta-namespace-sweep" in reasons and "3 JSP / tag file(s)" in reasons["jakarta-namespace-sweep"]
    both = _profile(poms=[_pom()], javax={"javax.servlet": 2})
    both.source.jsp_javax_files = 1
    r = dict(pre_handler_reasons(both, cfg))["jakarta-namespace-sweep"]
    assert "javax.servlet" in r and "1 JSP / tag file(s)" in r
    assert "jakarta-namespace-sweep" not in dict(pre_handler_reasons(_profile(poms=[_pom()]), cfg))


# ---------------------------------------------------------------------------------------------- round 10
def test_round10_preemptive_handlers(cfg) -> None:
    from sbmig.core.models import ServiceInfo
    from sbmig.plan import pre_handler_reasons

    jdk = _profile(libs=[LibraryUsage(name="jdk-removed-apis", evidence=["import:sun.misc"])],
                   poms=[_pom(deps=[GAV(group_id="jdk.tools", artifact_id="jdk.tools", version="1.8", scope="system")],
                              plugins=[GOOD_COMPILER])])
    reasons = dict(pre_handler_reasons(jdk, cfg))
    assert "sun.misc.BASE64Encoder/Decoder" in reasons["jdk-base64-shim"]
    assert "jdk.tools:jdk.tools declared" in reasons["remove-tools-jar"]
    assert "boot4-type-relocation-sweep" not in reasons                      # not a Boot project
    msa = _profile(poms=[_pom(plugins=[GOOD_COMPILER])])
    msa.services = [ServiceInfo(name="order", root_pom="order/pom.xml", case_type=CaseType.SPRING_BOOT,
                                spring_boot_version="2.7.18")]
    assert "boot4-type-relocation-sweep" in pre_handlers_for(msa, cfg)
    assert {"jdk-base64-shim", "remove-tools-jar", "boot4-type-relocation-sweep"} <= set(HANDLER_IDS)
