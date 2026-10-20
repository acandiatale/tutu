"""Round 9 (D3): multi-module `merge` edge cases."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, dep, java, make_ws
from .test_restructurer import kept_boot_files, multi_files, multi_profile

COMP = "module/comp-jar"


def _apply(ws, cfg: Config, **params):
    res = Restructurer().apply(ws, multi_profile(ws), cfg, mode="layout", params=params or None)
    assert res.ok, res.error
    return res


# ---------------------------------------------------------------------------------------- (a) union registries
def _registry_files() -> dict[str, str]:
    files = multi_files()
    r = "src/main/resources/META-INF"
    files[f"core/{r}/services/com.acme.Plugin"] = "# core plugins\ncom.acme.core.A\ncom.acme.shared.S\n"
    files[f"web/{r}/services/com.acme.Plugin"] = "com.acme.shared.S  # dup\ncom.acme.web.B\n"
    files[f"core/{r}/spring.factories"] = ("org.springframework.context.ApplicationListener=\\\n"
                                           "  com.acme.core.L1,\\\n  com.acme.core.L2\n")
    files[f"web/{r}/spring.factories"] = ("# web\norg.springframework.context.ApplicationListener=com.acme.core.L2,"
                                          "com.acme.web.L3\norg.springframework.boot.env.EnvironmentPostProcessor="
                                          "com.acme.web.E\n")
    imports = "spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"
    files[f"core/{r}/{imports}"] = "com.acme.core.AutoA\n"
    files[f"web/{r}/{imports}"] = "com.acme.web.AutoB\ncom.acme.core.AutoA\n"
    files[f"core/{r}/services/same.Same"] = "x.Y\n"
    files[f"web/{r}/services/same.Same"] = "x.Y\n"
    return files


@pytest.mark.parametrize("collision", ["first-wins", "fail"])
def test_registries_are_merged_as_union(tmp_path: Path, cfg: Config, collision: str) -> None:
    ws = make_ws(tmp_path, _registry_files(), "union-" + collision)
    res = _apply(ws, cfg, merge_collision=collision)                 # union is never a collision, not even for fail
    meta = ws.root / COMP / "src/main/resources/META-INF"
    assert (meta / "services/com.acme.Plugin").read_text() == (
        "# core plugins\ncom.acme.core.A\ncom.acme.shared.S\ncom.acme.web.B\n")
    assert (meta / "spring.factories").read_text() == (
        "org.springframework.context.ApplicationListener=com.acme.core.L1,\\\ncom.acme.core.L2,\\\ncom.acme.web.L3\n"
        "org.springframework.boot.env.EnvironmentPostProcessor=com.acme.web.E\n")
    imports = meta / "spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"
    assert imports.read_text() == "com.acme.core.AutoA\ncom.acme.web.AutoB\n"
    by = {mc.path: mc for mc in res.merge_conflicts}
    plugin = by["src/main/resources/META-INF/services/com.acme.Plugin"]
    assert plugin.union and not plugin.identical and plugin.kept_from == "core" and plugin.dropped_from == ["web"]
    assert plugin.saved_copies == ["merge-conflicts/core/src/main/resources/META-INF/services/com.acme.Plugin",
                                   "merge-conflicts/web/src/main/resources/META-INF/services/com.acme.Plugin"]
    for saved in plugin.saved_copies:
        assert (ws.run_dir / saved).is_file()                       # every original copy is kept for review
    same = by["src/main/resources/META-INF/services/same.Same"]
    assert same.identical and not same.union and same.saved_copies == []
    assert sum(mc.union for mc in res.merge_conflicts) == 3
    assert any(n.startswith("3 registry file(s)") for n in res.notes)
    assert not any(f.startswith("merge conflicts:") for f in res.manual_followups)   # nothing lost → no follow-up


# ---------------------------------------------------------------------------------------- (b) test resources
def test_test_resource_collisions_follow_main_rules(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["core/src/test/resources/fixture.json"] = '{"m": "core"}\n'
    files["web/src/test/resources/fixture.json"] = '{"m": "web"}\n'
    files["core/src/test/resources/META-INF/services/t.T"] = "a.A\n"
    files["web/src/test/resources/META-INF/services/t.T"] = "b.B\n"
    ws = make_ws(tmp_path, files, "testres")
    res = _apply(ws, cfg)
    assert (ws.root / COMP / "src/test/resources/fixture.json").read_text() == '{"m": "core"}\n'
    assert (ws.run_dir / "merge-conflicts/web/src/test/resources/fixture.json").read_text() == '{"m": "web"}\n'
    assert (ws.root / COMP / "src/test/resources/META-INF/services/t.T").read_text() == "a.A\nb.B\n"
    assert any(f.startswith("merge conflicts: 1 file(s)") for f in res.manual_followups)
    ws2 = make_ws(tmp_path, files, "testres-fail")
    res2 = Restructurer().apply(ws2, multi_profile(ws2), cfg, mode="layout", params={"merge_collision": "fail"})
    assert not res2.ok and "src/test/resources/fixture.json" in (res2.error or "")


# ---------------------------------------------------------------------------------------- (c) pom-packaging modules
def _bom_files() -> dict[str, str]:
    files = multi_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        <module>web</module>\n", "        <module>web</module>\n        <module>bom</module>\n"
        "        <module>platform</module>\n").replace(
        "    <dependencies>\n", "    <dependencyManagement>\n        <dependencies>\n"
        "            <dependency>\n                <groupId>com.acme</groupId>\n"
        "                <artifactId>suite-bom</artifactId>\n                <version>${project.version}</version>\n"
        "                <type>pom</type>\n                <scope>import</scope>\n            </dependency>\n"
        "        </dependencies>\n    </dependencyManagement>\n    <dependencies>\n", 1)
    parent = ("    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n"
              "        <version>5.0</version>\n    </parent>\n")
    files["bom/pom.xml"] = (
        HEAD + parent + "    <artifactId>suite-bom</artifactId>\n    <packaging>pom</packaging>\n"
        "    <properties>\n        <jackson.version>2.17.0</jackson.version>\n"
        "        <lombok.version>9.9</lombok.version>\n    </properties>\n"
        "    <dependencyManagement>\n        <dependencies>\n"
        + dep("com.fasterxml.jackson.dataformat", "jackson-dataformat-yaml", "${jackson.version}").replace("\n        ", "\n            ")
        .replace("        <dependency>", "            <dependency>", 1)
        + "        </dependencies>\n    </dependencyManagement>\n</project>\n")
    files["platform/pom.xml"] = (
        HEAD + parent + "    <artifactId>suite-platform</artifactId>\n    <packaging>pom</packaging>\n"
        "    <modules>\n        <module>api</module>\n        <module>../../sibling</module>\n    </modules>\n"
        "    <properties>\n        <api.flag>on</api.flag>\n    </properties>\n</project>\n")
    files["platform/api/pom.xml"] = (
        HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite-platform</artifactId>\n"
        "        <version>5.0</version>\n    </parent>\n    <artifactId>suite-api</artifactId>\n"
        "    <dependencies>\n" + dep("commons-io", "commons-io", "2.11.0") + "    </dependencies>\n</project>\n")
    files["platform/api/src/main/java/com/acme/suite/api/Api.java"] = java("com.acme.suite.api", "Api")
    return files


def test_pom_modules_fold_management_and_merge_children(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, _bom_files(), "bom")
    res = _apply(ws, cfg)
    assert res.merged_modules == ["core", "web", "bom", "platform", "platform/api"]
    for gone in ("bom", "platform", "core", "web"):
        assert not (ws.root / gone).exists()
    assert (ws.root / COMP / "src/main/java/com/acme/suite/api/Api.java").exists()
    comp = PomEditor(ws.root / COMP / "pom.xml")
    assert "commons-io:commons-io" in [d.key for d in comp.dependencies()]
    root = PomEditor(ws.root / "pom.xml")
    managed = {d.key: d for d in root.managed_dependencies()}
    assert "com.acme:suite-bom" not in managed                                # internal BOM import removed
    assert managed["com.fasterxml.jackson.dataformat:jackson-dataformat-yaml"].version == "${jackson.version}"
    props = root.properties()
    assert props["jackson.version"] == "2.17.0" and props["api.flag"] == "on"
    assert props["lombok.version"] != "9.9"                                    # root's own wins
    assert any("root dependencyManagement entries for merged modules removed: com.acme:suite-bom" in n
               for n in res.notes)
    assert any(n.startswith("module bom: 1 dependencyManagement entry") for n in res.notes)
    # (d) nested module outside the project root: skipped with note + follow-up, nothing outside touched
    assert any("'../../sibling' declared in platform/pom.xml resolves outside the project root" in n
               for n in res.notes)
    assert any("module ../../sibling (platform/pom.xml) lives outside the project root" in f
               for f in res.manual_followups)
    assert not (tmp_path / "src-in" / "sibling").exists() and not (ws.root.parent / "sibling").exists()


def test_pom_module_with_sources_is_saved_not_merged(tmp_path: Path, cfg: Config) -> None:
    files = _bom_files()
    files["bom/src/main/java/Stray.java"] = "class Stray {}\n"
    ws = make_ws(tmp_path, files, "bom-src")
    res = _apply(ws, cfg)
    assert not (ws.root / COMP / "src/main/java/Stray.java").exists()
    assert (ws.run_dir / "merge-conflicts/bom/src/main/java/Stray.java").is_file()
    assert any("pom-packaging module bom has a src/ directory" in f for f in res.manual_followups)


# ---------------------------------------------------------------------------------------- (d) root-level outside
def test_merge_skips_root_module_outside_project(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["pom.xml"] = files["pom.xml"].replace("<module>web</module>",
                                                "<module>web</module>\n        <module>../sibling</module>")
    ws = make_ws(tmp_path, files, "outside-merge")
    res = _apply(ws, cfg)
    assert res.merged_modules == ["core", "web"]
    assert any("'../sibling' declared in pom.xml resolves outside the project root (../sibling)" in n
               for n in res.notes)
    assert any("module ../sibling (pom.xml) lives outside the project root" in f for f in res.manual_followups)
    assert PomEditor(ws.root / "pom.xml").modules() == ["module/comp-jar", "module/boot-jar"]


# ---------------------------------------------------------------------------------------- (e) war module webapp
def test_war_module_webapp_lands_where_single_module_puts_it(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()                                               # web is <packaging>war</packaging>
    files["web/src/main/webapp/WEB-INF/web.xml"] = "<web-app/>\n"
    files["web/src/main/webapp/index.jsp"] = "<html/>\n"
    ws = make_ws(tmp_path, files, "war-merge")
    prof = multi_profile(ws)
    prof.source.web_xml_paths = ["web/src/main/webapp/WEB-INF/web.xml"]
    prof.source.has_web_xml = True
    prof.source.jsp_count = 1
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / COMP / "src/main/webapp/WEB-INF/web.xml").is_file()      # same place as the single-module war
    assert (ws.root / COMP / "src/main/webapp/index.jsp").is_file()
    assert any(f"web.xml at {COMP}/src/main/webapp/WEB-INF/web.xml" in f for f in res.manual_followups)
    assert any(f.startswith("WAR packaging converted to executable jar") for f in res.manual_followups)
    assert any("JSPs under src/main/webapp" in f for f in res.manual_followups)
    assert PomEditor(ws.root / COMP / "pom.xml").packaging() == "jar"
    assert "org.apache.tomcat.embed:tomcat-embed-jasper" in [
        d.key for d in PomEditor(ws.root / "module/boot-jar/pom.xml").dependencies()]


# ---------------------------------------------------------------------------------------- (f) kept boot resources
def test_kept_boot_module_keeps_its_application_properties(tmp_path: Path, cfg: Config) -> None:
    files = kept_boot_files()
    files["boot-jar/src/main/resources/application.properties"] = "server.port=8081\n"
    files["core/src/main/resources/application.properties"] = "core.flag=true\n"
    files["web/src/main/resources/application.properties"] = "web.flag=true\n"
    ws = make_ws(tmp_path, files, "kept-props")
    res = _apply(ws, cfg)
    assert res.kept_boot_module == "module/boot-jar"
    assert (ws.root / "module/boot-jar/src/main/resources/application.properties").read_text() == "server.port=8081\n"
    assert (ws.root / COMP / "src/main/resources/application.properties").read_text() == "core.flag=true\n"
    by = {mc.path: mc for mc in res.merge_conflicts}                   # first-wins only within comp-jar
    assert list(by) == ["src/main/resources/application.properties"]
    assert by["src/main/resources/application.properties"].kept_from == "core"
    assert by["src/main/resources/application.properties"].dropped_from == ["web"]
    assert any("application.properties exist in both module/boot-jar (kept) and module/comp-jar" in f
               for f in res.manual_followups)


# ---------------------------------------------------------------------------------------- (g) custom resource dirs
def test_custom_resource_directory_is_carried_into_comp_pom(tmp_path: Path, cfg: Config) -> None:
    files = multi_files()
    files["core/pom.xml"] = files["core/pom.xml"].replace(
        "</project>", "    <build>\n        <resources>\n            <resource>\n"
        "                <directory>src/main/config</directory>\n                <filtering>true</filtering>\n"
        "            </resource>\n        </resources>\n    </build>\n</project>")
    files["core/src/main/config/core.properties"] = "c=1\n"
    ws = make_ws(tmp_path, files, "resdir")
    res = _apply(ws, cfg)
    assert (ws.root / COMP / "src/main/config/core.properties").read_text() == "c=1\n"
    comp = PomEditor(ws.root / COMP / "pom.xml")
    resources = comp.children(comp.find("build/resources"), "resource")
    assert [comp.text_of(r, "directory") for r in resources] == ["src/main/resources", "src/main/config"]
    assert comp.text_of(resources[1], "filtering") == "true"
    assert any("comp-jar <resources>: union" in n for n in res.notes)
    text = (ws.root / COMP / "pom.xml").read_text()
    assert "        <resources>\n            <resource>\n                <directory>src/main/resources</directory>" \
        in text
