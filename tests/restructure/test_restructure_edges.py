"""Restructure edge cases: optional_bump mode, MSA library roots, E7 system jars, S2 idempotency, S5 module layout,
S7 WAR modules (see test_restructurer.test_layout_multi_module_wrap), test-jar wiring for moved @SpringBootTest."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType, PomInfo, ServiceInfo
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, all_, dep, dep_keys, java, make_ws, profile, t, xml
from .test_restructurer import (MANDATORY, boot2_files, boot2_profile, msa_files, msa_profile, multi_files,
                                multi_profile)


@pytest.fixture()
def cfg_rm(cfg: Config) -> Config:
    cfg.artifacts.remove_when_present = ["com.legacy:legacy-bom-thing"]
    return cfg


def _tree(ws) -> list[str]:
    return sorted(p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*") if ".git" not in p.parts)


# ------------------------------------------------------------------------------------------------- optional_bump
def test_optional_bump_mode_only_bumps(tmp_path: Path, cfg_rm: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "ob")
    before = _tree(ws)
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="optional_bump")
    assert res.ok, res.error
    assert _tree(ws) == before                                                   # no moves, nothing generated
    assert res.changed_files == ["pom.xml"] and res.generated_files == [] and res.moved_files == 0
    r = xml(ws.root / "pom.xml")
    assert t(r, "parent/artifactId") == "spring-boot-starter-parent" and t(r, "parent/version") == "2.7.18"
    assert t(r, "properties/guava.version") == "33.4.8-jre"
    assert t(r, "properties/java.version") == "1.8"                              # no java fix in this mode
    assert "com.legacy:legacy-bom-thing" not in dep_keys(r)                       # remove_when_present
    assert sorted(res.optional_bumped) == sorted([
        "com.google.guava:guava 30.0-jre→33.4.8-jre", "org.apache.commons:commons-lang3 3.12.0→3.17.0",
        "org.projectlombok:lombok managed→1.18.38", "org.projectlombok:lombok 1.18.20→1.18.38"])
    assert res.mandatory_added == []
    assert any("optional_bump: 4 optional artifact version(s) bumped" in n for n in res.notes)
    assert not ws.is_dirty()

    # idempotent: a second optional_bump and the later layout find nothing left to bump
    res2 = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="optional_bump")
    assert res2.ok and res2.optional_bumped == [] and res2.changed_files == []
    assert any("nothing to bump" in n for n in res2.notes)
    res3 = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res3.ok, res3.error
    assert res3.optional_bumped == []
    c = PomEditor(ws.root / "module/comp-jar/pom.xml")
    vers = {d.key: d.version for d in c.dependencies()}
    assert vers["org.apache.commons:commons-lang3"] == "3.17.0" and vers["com.google.guava:guava"] == "${guava.version}"
    assert PomEditor(ws.root / "pom.xml").get_property("guava.version") == "33.4.8-jre"


def test_layout_twice_is_idempotent(tmp_path: Path, cfg_rm: Config) -> None:
    ws = make_ws(tmp_path, multi_files(), "twice")
    res = Restructurer().apply(ws, multi_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    snap = {p: (ws.root / p).read_bytes() for p in _tree(ws) if (ws.root / p).is_file()}
    res2 = Restructurer().apply(ws, multi_profile(ws), cfg_rm, mode="layout")
    assert res2.ok and "already restructured" in res2.notes
    assert res2.changed_files == [] and res2.optional_bumped == [] and res2.moved_files == 0
    assert {p: (ws.root / p).read_bytes() for p in _tree(ws) if (ws.root / p).is_file()} == snap


def test_optional_bump_with_target_only_touches_that_service(tmp_path: Path, cfg: Config) -> None:
    files = msa_files()
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        "    </dependencies>", dep("com.google.guava", "guava", "29.0-jre") + "    </dependencies>")
    ws = make_ws(tmp_path, files, "obt")
    res = Restructurer().apply(ws, msa_profile(ws), cfg, mode="optional_bump",
                               params={"mode": "optional_bump", "service": "order-service", "target": "order-service"})
    assert res.ok, res.error
    assert res.changed_files == ["order-service/pom.xml"]
    assert res.optional_bumped == ["com.google.guava:guava 30.0-jre→33.4.8-jre"]
    assert PomEditor(ws.root / "user-service/pom.xml").dependencies()[1].version == "29.0-jre"
    assert PomEditor(ws.root / "order-service/pom.xml").get_parent().version == "3.2.0"      # parent untouched
    res2 = Restructurer().apply(ws, msa_profile(ws), cfg, mode="optional_bump", params={"target": "nope"})
    assert res2.ok and any("no pom below nope" in n for n in res2.notes)


def test_optional_bump_target_uses_in_repo_parent_property(tmp_path: Path, cfg: Config) -> None:
    top = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>platform</artifactId>\n    <version>1.0</version>\n"
           "    <packaging>pom</packaging>\n    <modules>\n        <module>svc</module>\n    </modules>\n"
           "    <properties>\n        <guava.version>30.0-jre</guava.version>\n    </properties>\n</project>\n")
    svc = (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>platform</artifactId>\n"
           "        <version>1.0</version>\n    </parent>\n    <artifactId>svc</artifactId>\n"
           "    <dependencies>\n" + dep("com.google.guava", "guava", "${guava.version}") + "    </dependencies>\n"
           "</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": top, "svc/pom.xml": svc}, "obp")
    res = Restructurer().apply(ws, profile(ws, CaseType.MSA, artifact_id="platform", version="1.0"), cfg,
                               mode="optional_bump", params={"target": "svc"})
    assert res.ok and res.optional_bumped == ["com.google.guava:guava 30.0-jre→33.4.8-jre"]
    assert PomEditor(ws.root / "pom.xml").get_property("guava.version") == "33.4.8-jre"


# ------------------------------------------------------------------------------------------------- MSA libraries
def _common_pom() -> str:
    return (HEAD + "    <parent>\n        <groupId>org.springframework.boot</groupId>\n"
            "        <artifactId>spring-boot-starter-parent</artifactId>\n        <version>3.2.0</version>\n"
            "        <relativePath/>\n    </parent>\n"
            "    <groupId>com.acme</groupId>\n    <artifactId>common</artifactId>\n    <version>1.0</version>\n"
            "    <properties>\n        <java.version>17</java.version>\n    </properties>\n"
            "    <dependencies>\n" + dep("commons-io", "commons-io", "2.11.0")
            + dep("com.legacy", "legacy-bom-thing", "1.0") + "    </dependencies>\n</project>\n")


@pytest.mark.parametrize("via", ["params", "profile"])
def test_msa_pom_patches_library_roots_without_mandatory(tmp_path: Path, cfg_rm: Config, via: str) -> None:
    files = msa_files()
    files["common/pom.xml"] = _common_pom()
    files["common/src/main/java/com/acme/common/Money.java"] = java("com.acme.common", "Money")
    ws = make_ws(tmp_path, files, f"msalib-{via}")
    prof = msa_profile(ws)
    params = {"mode": "msa_pom"}
    if via == "params":
        params["library_roots"] = ["common"]
    else:
        prof.poms = [PomInfo(path="common/pom.xml", artifact_id="common")]
        prof.services.append(ServiceInfo(name="common", root_pom="common/pom.xml", case_type=CaseType.PURE_JAVA,
                                         poms=["common/pom.xml"]))
    res = Restructurer().apply(ws, prof, cfg_rm, mode="msa_pom", params=params)
    assert res.ok, res.error
    c = xml(ws.root / "common/pom.xml")
    assert t(c, "parent/artifactId") == "spring-boot-starter-parent" and t(c, "parent/version") == "4.1.0"
    keys = dep_keys(c)
    assert not set(MANDATORY) & set(keys)                                        # libraries: no mandatory deps
    assert "com.legacy:legacy-bom-thing" not in keys                              # remove_when_present
    assert t(c, "properties/java.version") == "21"
    assert "commons-io:commons-io 2.11.0→2.19.0" in res.optional_bumped
    for s in ("order-service", "user-service"):
        assert dep_keys(xml(ws.root / s / "pom.xml"))[-2:] == MANDATORY
    assert any("patched library root common/pom.xml" in n for n in res.notes)


def test_msa_pom_top_aggregator_libraries_only_bumped(tmp_path: Path, cfg: Config) -> None:
    cfg.layout.msa_parent_policy = "top-aggregator"
    files = msa_files()
    files["common/pom.xml"] = _common_pom()
    ws = make_ws(tmp_path, files, "msalib-top")
    res = Restructurer().apply(ws, msa_profile(ws), cfg, mode="msa_pom", params={"library_roots": ["common/pom.xml"]})
    assert res.ok, res.error
    assert PomEditor(ws.root / "common/pom.xml").get_parent().version == "3.2.0"
    assert "commons-io:commons-io 2.11.0→2.19.0" in res.optional_bumped
    assert any("top-aggregator policy: library roots" in n for n in res.notes)


def test_msa_pom_only_library_roots_and_missing(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, {"common/pom.xml": _common_pom()}, "libonly")
    p = profile(ws, CaseType.MSA, artifact_id="common", version="1.0")
    p.root_pom = None
    res = Restructurer().apply(ws, p, cfg, mode="msa_pom", params={"library_roots": ["common", "gone"]})
    assert res.ok, res.error
    assert PomEditor(ws.root / "common/pom.xml").get_parent().version == "4.1.0"
    assert any("library root pom gone/pom.xml not found" in n for n in res.notes)


# ------------------------------------------------------------------------------------------------- E7
def _sys(a: str, path: str) -> str:
    return (f"        <dependency>\n            <groupId>com.vendor</groupId>\n            <artifactId>{a}</artifactId>\n"
            f"            <version>1.0</version>\n            <scope>system</scope>\n"
            f"            <systemPath>{path}</systemPath>\n        </dependency>\n")


def test_layout_relocates_system_scoped_jars(tmp_path: Path, cfg: Config) -> None:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>app</artifactId>\n    <version>1</version>\n"
           "    <dependencies>\n"
           + _sys("x", "${basedir}/lib/x.jar")
           + _sys("y", "${project.basedir}/src/main/webapp/WEB-INF/lib/y.jar")
           + _sys("gone", "${basedir}/lib/gone.jar")
           + _sys("ext", "/opt/vendor/ext.jar")
           + "    </dependencies>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/a/A.java": java("a", "A"), "lib/x.jar": "PK-x",
                            "src/main/webapp/WEB-INF/lib/y.jar": "PK-y"}, "sysjar")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="app", version="1", base_package="a"),
                               cfg, mode="layout")
    assert res.ok, res.error
    comp = ws.root / "module/comp-jar"
    assert (comp / "lib/x.jar").read_text() == "PK-x" and (comp / "lib/y.jar").read_text() == "PK-y"
    c = xml(comp / "pom.xml")
    paths = {t(d, "artifactId"): t(d, "systemPath") for d in all_(c, "dependencies/dependency")}
    assert paths == {"x": "${project.basedir}/lib/x.jar", "y": "${project.basedir}/lib/y.jar",
                     "gone": "${basedir}/lib/gone.jar", "ext": "/opt/vendor/ext.jar"}
    fu = res.manual_followups
    assert any("system-scoped jar `x.jar` (com.vendor:x) should be installed into Nexus" in f
               and "mvn install:install-file" in f for f in fu)
    assert any("`gone.jar`" in f and "missing" in f for f in fu)
    assert any("com.vendor:ext" in f and "outside the project" in f for f in fu)
    assert "module/comp-jar/lib/x.jar" in res.changed_files


def test_wrap_relocates_module_system_jars(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["core/pom.xml"] = files["core/pom.xml"].replace(
        "    </dependencies>", _sys("shared", "${basedir}/../lib/shared.jar") + _sys("own", "${basedir}/lib/own.jar")
        + "    </dependencies>")
    files["lib/shared.jar"] = "PK-shared"
    files["core/lib/own.jar"] = "PK-own"
    ws = make_ws(tmp_path, files, "wrapsys")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout",
                               params={"multi_module_policy": "wrap"})
    assert res.ok, res.error
    core = ws.root / "module/core"
    assert (core / "lib/shared.jar").read_text() == "PK-shared" and (core / "lib/own.jar").exists()
    c = xml(core / "pom.xml")
    paths = {t(d, "artifactId"): t(d, "systemPath") for d in all_(c, "dependencies/dependency")}
    assert paths["shared"] == "${project.basedir}/lib/shared.jar"
    assert paths["own"] == "${project.basedir}/lib/own.jar"                       # normalized, jar not copied twice
    assert any("module/core/lib/shared.jar" in f for f in res.manual_followups)


# ------------------------------------------------------------------------------------------------- S5
def test_wrap_module_outside_project_root_aborts_without_changes(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["pom.xml"] = files["pom.xml"].replace("<module>web</module>", "<module>../sibling</module>")
    ws = make_ws(tmp_path, files, "outside")
    before = _tree(ws)
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout", params={"multi_module_policy": "wrap"})
    assert not res.ok
    assert "'../sibling' → ../sibling" in res.error and "outside the project root" in res.error
    assert "nothing was changed" in res.error and any("outside the project root" in n for n in res.notes)
    assert _tree(ws) == before and res.changed_files == []


def test_wrap_recomputes_nested_modules_and_odd_parent_paths(tmp_path: Path, cfg: Config) -> None:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>suite</artifactId>\n    <version>5.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>./group</module>\n    </modules>\n"
            "</project>\n")
    group = (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n"
             "        <version>5.0</version>\n    </parent>\n    <artifactId>group</artifactId>\n"
             "    <packaging>pom</packaging>\n    <modules>\n        <module>sub</module>\n"
             "        <module>../shared</module>\n    </modules>\n</project>\n")
    corp = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>corp-parent</artifactId>\n"
            "    <version>5.0</version>\n    <packaging>pom</packaging>\n</project>\n")

    def leaf(aid: str, parent: str, rel: str) -> str:
        return (HEAD + f"    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>{parent}</artifactId>\n"
                f"        <version>5.0</version>\n        <relativePath>{rel}</relativePath>\n    </parent>\n"
                f"    <artifactId>{aid}</artifactId>\n</project>\n")

    files = {"pom.xml": root, "group/pom.xml": group, "build/corp/pom.xml": corp,
             "group/sub/pom.xml": leaf("sub", "corp-parent", "../../build/corp/pom.xml"),
             "group/sub/src/main/java/com/acme/suite/Sub.java": java("com.acme.suite", "Sub"),
             "shared/pom.xml": leaf("shared", "corp-parent", "../build/corp"),
             "shared/src/main/java/com/acme/suite/Shared.java": java("com.acme.suite", "Shared")}
    ws = make_ws(tmp_path, files, "nested")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout",
                               params={"multi_module_policy": "wrap"})
    assert res.ok, res.error
    assert PomEditor(ws.root / "pom.xml").modules() == ["module/group", "module/boot-jar"]
    assert PomEditor(ws.root / "module/group/pom.xml").modules() == ["sub", "../../shared"]
    assert PomEditor(ws.root / "module/group/sub/pom.xml").parent_relative_path() == "../../../build/corp/pom.xml"
    assert PomEditor(ws.root / "module/group/pom.xml").parent_relative_path() == "../../pom.xml"
    assert PomEditor(ws.root / "shared/pom.xml").parent_relative_path() == "../build/corp"     # not moved
    assert any("<modules> of module/group/pom.xml recomputed" in n for n in res.notes)


def test_wrap_parent_outside_project_recomputed(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["core/pom.xml"] = files["core/pom.xml"].replace(
        "<version>5.0</version>\n    </parent>",
        "<version>5.0</version>\n        <relativePath>../../corp/pom.xml</relativePath>\n    </parent>")
    ws = make_ws(tmp_path, files, "outparent")
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout",
                               params={"multi_module_policy": "wrap"})
    assert res.ok, res.error
    assert PomEditor(ws.root / "module/core/pom.xml").parent_relative_path() == "../../../corp/pom.xml"
    assert any("lives outside the project" in n for n in res.notes)


# ------------------------------------------------------------------------------------------------- S2
def test_already_on_inhouse_parent_single_module_still_split(tmp_path: Path, cfg: Config) -> None:
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace("<version>2.7.18</version>\n        <relativePath/>",
                                                "<version>4.1.0</version>\n        <relativePath/>", 1)
    ws = make_ws(tmp_path, files, "onparent")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "module/comp-jar/pom.xml").exists() and (ws.root / "module/boot-jar/pom.xml").exists()
    assert any("already inherits the in-house parent" in n for n in res.notes)
    res2 = Restructurer().apply(ws, boot2_profile(ws), cfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes and res2.changed_files == []


def test_already_restructured_but_foreign_parent_noted(tmp_path: Path, cfg: Config) -> None:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>x</artifactId>\n    <version>1</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>module/comp-jar</module>\n"
            "        <module>module/boot-jar</module>\n    </modules>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": root}, "foreign")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="x", version="1"), cfg, mode="layout")
    assert res.ok and "already restructured" in res.notes
    assert any("does not inherit" in n for n in res.notes)


# ------------------------------------------------------------------------------------------------- test-jar
def test_moved_boot_tests_get_comp_test_jar_when_helpers_referenced(tmp_path: Path, cfg_rm: Config) -> None:
    files = boot2_files()
    files["src/test/java/com/acme/shop/TestData.java"] = java("com.acme.shop", "TestData")
    files["src/test/java/com/acme/shop/ShopApplicationTests.java"] = java(
        "com.acme.shop", "ShopApplicationTests", "@SpringBootTest", body="    Object o = TestData.class;")
    ws = make_ws(tmp_path, files, "testjar")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    b = xml(ws.root / "module/boot-jar/pom.xml")
    tj = [d for d in all_(b, "dependencies/dependency") if t(d, "type") == "test-jar"]
    assert len(tj) == 1 and t(tj[0], "artifactId") == "shop-comp" and t(tj[0], "scope") == "test"
    assert dep_keys(b).count("${project.groupId}:shop-comp") == 2                  # jar + test-jar
    c = xml(ws.root / "module/comp-jar/pom.xml")
    jar = [p for p in all_(c, "build/plugins/plugin") if t(p, "artifactId") == "maven-jar-plugin"]
    assert len(jar) == 1 and t(jar[0], "executions/execution/goals/goal") == "test-jar"
    assert any("TestData" in n and "test-jar dependency added" in n for n in res.notes)


def test_existing_jar_plugin_gets_test_jar_execution(tmp_path: Path, cfg_rm: Config) -> None:
    files = boot2_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        </plugins>\n    </build>",
        "            <plugin>\n                <groupId>org.apache.maven.plugins</groupId>\n"
        "                <artifactId>maven-jar-plugin</artifactId>\n                <version>3.4.1</version>\n"
        "            </plugin>\n        </plugins>\n    </build>")
    files["src/test/java/com/acme/shop/TestData.java"] = java("com.acme.shop", "TestData")
    files["src/test/java/com/acme/shop/ShopApplicationTests.java"] = java(
        "com.acme.shop", "ShopApplicationTests", "@SpringBootTest", body="    TestData d;")
    ws = make_ws(tmp_path, files, "testjar2")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    c = xml(ws.root / "module/comp-jar/pom.xml")
    jar = [p for p in all_(c, "build/plugins/plugin") if t(p, "artifactId") == "maven-jar-plugin"]
    assert len(jar) == 1 and t(jar[0], "version") == "3.4.1"
    assert t(jar[0], "executions/execution/goals/goal") == "test-jar"


def test_no_test_jar_without_helper_references(tmp_path: Path, cfg_rm: Config) -> None:
    files = boot2_files()
    files["src/test/java/com/acme/shop/ShopApplicationTests.java"] = java(
        "com.acme.shop", "ShopApplicationTests", "@SpringBootTest", body="    // GreeterTest is not used here")
    ws = make_ws(tmp_path, files, "notestjar")
    res = Restructurer().apply(ws, boot2_profile(ws), cfg_rm, mode="layout")
    assert res.ok, res.error
    b = xml(ws.root / "module/boot-jar/pom.xml")
    assert not [d for d in all_(b, "dependencies/dependency") if t(d, "type") == "test-jar"]
    c = xml(ws.root / "module/comp-jar/pom.xml")
    assert not [p for p in all_(c, "build/plugins/plugin") if t(p, "artifactId") == "maven-jar-plugin"]
