"""2026-10-23 (sibling poms + EAR follow-ups): no dependency on a module of the project reaches a generated pom
whatever its groupId spelling; external `<type>war</type>` dependencies (maven-war-plugin overlays) never go into a
jar module and move to the boot WAR with their `<overlays>` configuration; directories a merged module references
outside itself but inside the project (`${basedir}/../../STATIC`) are folded into the back module and used from
there; `source_scanner.clean_pom_dir(allow_parent=True)`."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze.source_scanner import clean_pom_dir, pom_build_dirs, pom_web_dirs
from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import Restructurer

from .helpers import HEAD, all_, dep, dep_keys, java, make_ws, profile, t, xml
from .test_company_layout import _roles

WAR = "            <type>war</type>\n"
OVERLAY_GA = "com.company.legacy:legacy-base-web"


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def _all_generated(root: Path, name: str) -> list[Path]:
    return [p for p in [root / "pom.xml", *(root / f"{name}-{m}" / "pom.xml" for m in ("back", "front", "boot"))]
            if p.is_file()]


# ---------------------------------------------------------------------------------------------- scanner flag
def test_clean_pom_dir_allow_parent() -> None:
    assert clean_pom_dir("${basedir}/../../STATIC") is None
    assert clean_pom_dir("${basedir}/../../STATIC", allow_parent=True) == "../../STATIC"
    assert clean_pom_dir("${project.basedir}/../x/./y", allow_parent=True) == "../x/y"
    for raw in ("..", "/abs/web", "C:/web", "${other}/web", "${basedir}"):
        assert clean_pom_dir(raw, allow_parent=True) is None, raw
    assert clean_pom_dir("a/../b", allow_parent=True) == "b"
    text = (HEAD + "    <artifactId>w</artifactId>\n    <build>\n        <sourceDirectory>../src</sourceDirectory>\n"
            "        <resources><resource><directory>${basedir}/../../STATIC/res</directory></resource></resources>\n"
            "        <plugins><plugin><artifactId>maven-war-plugin</artifactId><configuration>"
            "<warSourceDirectory>${basedir}/../../STATIC</warSourceDirectory></configuration></plugin></plugins>\n"
            "    </build>\n</project>\n")
    assert [w.dir for w in pom_web_dirs(text)] == [] and pom_build_dirs(text) == []          # default unchanged
    assert [w.dir for w in pom_web_dirs(text, allow_parent=True)] == ["../../STATIC"]
    assert pom_build_dirs(text, allow_parent=True) == ["../src", "../../STATIC/res"]


# ---------------------------------------------------------------------------------------------- internal deps
def _internal_files() -> dict[str, str]:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>suite</artifactId>\n    <version>3.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>core</module>\n"
            "        <module>web</module>\n    </modules>\n"
            "    <dependencyManagement>\n        <dependencies>\n"
            + dep("${suite.group}", "suite-core", "3.0").replace("        <", "            <")
            + dep("com.google.guava", "guava", "31.0-jre").replace("        <", "            <")
            + "        </dependencies>\n    </dependencyManagement>\n</project>\n")
    core = (HEAD + "    <parent>\n        <groupId>com.corp</groupId>\n        <artifactId>corp-parent</artifactId>\n"
            "        <version>9</version>\n        <relativePath/>\n    </parent>\n"
            "    <artifactId>suite-core</artifactId>\n    <version>3.0</version>\n    <dependencies>\n"
            + dep("com.google.guava", "guava") + "    </dependencies>\n</project>\n")
    web = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>suite-web</artifactId>\n    <version>3.0</version>\n"
           "    <dependencies>\n"
           + dep("com.corp", "suite-core", "3.0")                                    # core's (parent's) groupId
           + dep("${parent.groupId}", "suite-core", "3.0", "test", extra="            <type>test-jar</type>\n")
           + dep("org.apache.commons", "commons-lang3", "3.12.0")
           + "    </dependencies>\n</project>\n")
    return {"pom.xml": root, "core/pom.xml": core, "web/pom.xml": web,
            "core/src/main/java/com/acme/core/Repo.java": java("com.acme.core", "Repo"),
            "web/src/main/java/com/acme/web/Page.java": java("com.acme.web", "Page")}


@pytest.mark.parametrize("roles", [True, False], ids=["role-parents", "root-parent"])
def test_module_dependencies_are_internal_whatever_the_group_spelling(tmp_path: Path, ccfg: Config,
                                                                      roles: bool) -> None:
    if not roles:
        ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = make_ws(tmp_path, _internal_files(), "internal")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="suite", version="3.0", packaging="pom",
                   base_package="com.acme")
    prof.is_multi_module = True
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    for pom in _all_generated(ws.root, "suite"):
        x = xml(pom)
        for path in ("dependencies/dependency", "dependencyManagement/dependencies/dependency"):
            keys = dep_keys(x, path)
            assert not [k for k in keys if k.endswith((":suite-core", ":suite-web"))], (pom, keys)
    back = dep_keys(xml(ws.root / "suite-back/pom.xml"))
    assert "com.google.guava:guava" in back and "org.apache.commons:commons-lang3" in back


# ---------------------------------------------------------------------------------------------- WAR overlays
OVERLAYS = ("                <configuration>\n                    <overlays>\n                        <overlay>\n"
            "                            <groupId>com.company.legacy</groupId>\n"
            "                            <artifactId>legacy-base-web</artifactId>\n"
            "                            <excludes><exclude>WEB-INF/web.xml</exclude></excludes>\n"
            "                        </overlay>\n                    </overlays>\n"
            "                    <warSourceDirectory>src/main/webapp</warSourceDirectory>\n"
            "                </configuration>\n")


def _overlay_pom(aid: str = "board", jsp_scope: str = "") -> str:
    return (HEAD + "    <groupId>com.example</groupId>\n"
            f"    <artifactId>{aid}</artifactId>\n    <version>1.0.0</version>\n    <packaging>war</packaging>\n"
            "    <properties>\n        <base.web.version>1.0.0</base.web.version>\n    </properties>\n"
            "    <dependencies>\n"
            + dep("com.company.legacy", "legacy-base-web", "${base.web.version}", jsp_scope or None, extra=WAR)
            + dep("javax.servlet", "javax.servlet-api", "3.1.0", "provided")
            + "    </dependencies>\n    <build>\n        <plugins>\n            <plugin>\n"
              "                <groupId>org.apache.maven.plugins</groupId>\n"
              "                <artifactId>maven-war-plugin</artifactId>\n                <version>3.2.3</version>\n"
            + OVERLAYS + "            </plugin>\n        </plugins>\n    </build>\n</project>\n")


def _overlay_files(jsp: bool = True) -> dict[str, str]:
    files = {"pom.xml": _overlay_pom(),
             "src/main/java/com/example/board/BoardServlet.java": java("com.example.board", "BoardServlet"),
             "src/main/webapp/css/board.css": "b{}\n"}
    if jsp:
        files["src/main/webapp/WEB-INF/jsp/board/list.jsp"] = "<%@ include file=\"/WEB-INF/jsp/common/header.jsp\" %>\n"
    return files


def _overlay_profile(ws, jsp: bool = True):
    return profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="board", group_id="com.example", version="1.0.0",
                   packaging="war", base_package="com.example.board", jsp_count=1 if jsp else 0,
                   jsp_paths=["src/main/webapp/WEB-INF/jsp/board/list.jsp"] if jsp else [])


def _overlay_dep(x) -> list[tuple[str | None, ...]]:
    return [(t(d, "groupId"), t(d, "artifactId"), t(d, "version"), t(d, "type"), t(d, "scope"))
            for d in all_(x, "dependencies/dependency") if t(d, "type") == "war"]


def _war_plugin_conf(x):
    for p in all_(x, "build/plugins/plugin"):
        if t(p, "artifactId") == "maven-war-plugin":
            return p
    return None


@pytest.mark.parametrize("jsp", [True, False], ids=["with-jsp", "overlay-only"])
def test_war_overlay_moves_to_the_boot_war(tmp_path: Path, ccfg: Config, jsp: bool) -> None:
    ws = make_ws(tmp_path, _overlay_files(jsp), "overlay-" + str(jsp))
    res = Restructurer().apply(ws, _overlay_profile(ws, jsp), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    boot = xml(root / "board-boot/pom.xml")
    assert t(boot, "packaging") == "war" and res.boot_packaging == "war"     # the overlay alone makes it a WAR
    assert _overlay_dep(boot) == [("com.company.legacy", "legacy-base-web", "1.0.0", "war", None)]
    plugin = _war_plugin_conf(boot)
    assert plugin is not None and t(plugin, "version") is None
    assert t(plugin, "configuration/overlays/overlay/artifactId") == "legacy-base-web"
    assert t(plugin, "configuration/overlays/overlay/excludes/exclude") == "WEB-INF/web.xml"
    assert "warSourceDirectory" not in (root / "board-boot/pom.xml").read_text()
    for jar in ("board-back", "board-front"):
        if (root / jar / "pom.xml").is_file():
            text = (root / jar / "pom.xml").read_text()
            assert "legacy-base-web" not in text.split("<overlays>")[0] and "<type>war</type>" not in text, jar
    assert "war-overlay-moved" in res.flags
    assert any(n.startswith(f"WAR overlay {OVERLAY_GA}:1.0.0 (pom.xml) moved to board-boot") for n in res.notes)
    assert (f"WAR overlay {OVERLAY_GA}:1.0.0 must be resolvable from the configured repositories: install it into "
            "the company Nexus (build.settings_file mirror) — it is not on Maven Central") in res.manual_followups
    pack = [f for f in res.manual_followups if f.startswith("boot module")]
    if jsp:
        assert pack == [("boot module packaged as an executable WAR (java -jar runs the embedded Tomcat; an external Tomcat works "
                         "too); JSPs are served from the front jar (META-INF/resources) — board-boot, board-front")]
    else:
        assert pack == [(f"boot module board-boot packaged as an executable WAR (java -jar runs the embedded Tomcat; an external "
                         f"Tomcat works too): the WAR overlay(s) {OVERLAY_GA}:1.0.0 carry web content")]
        assert any("SpringBootServletInitializer" in t_ for t_ in [
            (root / "board-boot/src/main/java/com/example/board/Application.java").read_text()])


def test_war_overlay_not_carried_into_an_executable_jar(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.jsp_boot_packaging = "jar"
    ws = make_ws(tmp_path, _overlay_files(jsp=False), "overlay-jar")
    res = Restructurer().apply(ws, _overlay_profile(ws, jsp=False), ccfg, mode="layout")
    assert res.ok, res.error
    assert res.boot_packaging == "jar"
    for pom in _all_generated(ws.root, "board"):
        assert "legacy-base-web" not in pom.read_text(), pom
    assert "war-overlay-moved" not in res.flags
    assert (f"WAR overlay {OVERLAY_GA}:1.0.0 (pom.xml) not carried: board-boot is an executable jar "
            "(layout.jsp_boot_packaging: jar) — copy the overlay's web content into board-front or set "
            "layout.jsp_boot_packaging: war") in res.manual_followups
    assert any(f.startswith(f"WAR overlay {OVERLAY_GA}:1.0.0 must be resolvable") for f in res.manual_followups)


def test_war_overlay_of_a_merged_module(tmp_path: Path, ccfg: Config) -> None:
    """Merge: the overlay of a WAR module (scope kept, version from the root dependencyManagement) goes to the
    boot WAR; an internal WAR module dependency (another module of the project) is not an overlay."""
    root = (HEAD + "    <groupId>com.example</groupId>\n    <artifactId>board-parent</artifactId>\n"
            "    <version>1.0.0</version>\n    <packaging>pom</packaging>\n"
            "    <modules>\n        <module>admin</module>\n        <module>web</module>\n    </modules>\n"
            "    <dependencyManagement>\n        <dependencies>\n"
            + dep("com.company.legacy", "legacy-base-web", "1.2.0", extra=WAR).replace("        <", "            <")
            + "        </dependencies>\n    </dependencyManagement>\n</project>\n")
    web = (_overlay_pom("board-web").replace("<version>${base.web.version}</version>", "<scope>runtime</scope>")
           .replace("    <groupId>com.example</groupId>\n    <artifactId>board-web</artifactId>\n"
                    "    <version>1.0.0</version>\n",
                    "    <parent>\n        <groupId>com.example</groupId>\n"
                    "        <artifactId>board-parent</artifactId>\n        <version>1.0.0</version>\n"
                    "    </parent>\n    <artifactId>board-web</artifactId>\n"))
    web = web.replace("    </dependencies>", dep("${project.groupId}", "board-admin", "${project.version}", extra=WAR)
                      + "    </dependencies>")
    admin = (HEAD + "    <parent>\n        <groupId>com.example</groupId>\n        <artifactId>board-parent</artifactId>\n"
             "        <version>1.0.0</version>\n    </parent>\n    <artifactId>board-admin</artifactId>\n"
             "    <packaging>war</packaging>\n</project>\n")
    files = {"pom.xml": root, "web/pom.xml": web, "admin/pom.xml": admin,
             "admin/src/main/webapp/admin.html": "<html/>\n",
             "web/src/main/java/com/example/board/BoardServlet.java": java("com.example.board", "BoardServlet"),
             "web/src/main/webapp/WEB-INF/jsp/list.jsp": "<html/>\n"}
    ws = make_ws(tmp_path, files, "overlay-merge")
    prof = profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="board-parent", group_id="com.example", version="1.0.0",
                   packaging="pom", base_package="com.example.board", jsp_count=1,
                   jsp_paths=["web/src/main/webapp/WEB-INF/jsp/list.jsp"])
    prof.is_multi_module = True
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    boot = xml(ws.root / "board-parent-boot/pom.xml")
    assert _overlay_dep(boot) == [("com.company.legacy", "legacy-base-web", "1.2.0", "war", "runtime")]
    assert t(_war_plugin_conf(boot), "configuration/overlays/overlay/artifactId") == "legacy-base-web"
    for pom in _all_generated(ws.root, "board-parent"):
        assert "board-admin" not in pom.read_text(), pom
    assert (ws.root / "board-parent-front/src/main/resources/static/admin.html").is_file()
    assert "war-overlay-moved" in res.flags


# ---------------------------------------------------------------------------------------------- outside a module
def _outside_files(web_conf: str, resources: str = "") -> dict[str, str]:
    root = (HEAD + "    <groupId>com.example.portal</groupId>\n    <artifactId>portal-aggregator</artifactId>\n"
            "    <version>1.0.0</version>\n    <packaging>pom</packaging>\n"
            "    <properties>\n        <sbmig.project-name>portal</sbmig.project-name>\n    </properties>\n"
            "    <modules>\n        <module>APP/COMMON</module>\n        <module>APP/WEB</module>\n"
            "    </modules>\n</project>\n")
    web = (HEAD + "    <groupId>com.example.portal</groupId>\n    <artifactId>portalWEB</artifactId>\n"
           "    <version>1.0.0</version>\n    <packaging>war</packaging>\n    <dependencies>\n"
           + dep("${project.groupId}", "portalCOMMON", "${project.version}")
           + "    </dependencies>\n    <build>\n" + resources
           + "        <plugins>\n            <plugin>\n                <artifactId>maven-war-plugin</artifactId>\n"
             "                <configuration>\n" + web_conf + "                </configuration>\n"
             "            </plugin>\n        </plugins>\n    </build>\n</project>\n")
    common = (HEAD + "    <groupId>com.example.portal</groupId>\n    <artifactId>portalCOMMON</artifactId>\n"
              "    <version>1.0.0</version>\n</project>\n")
    return {
        "pom.xml": root, "APP/WEB/pom.xml": web, "APP/COMMON/pom.xml": common,
        "APP/WEB/src/main/java/com/example/portal/web/Home.java": java("com.example.portal.web", "Home"),
        "APP/COMMON/src/main/java/com/example/portal/common/Util.java": java("com.example.portal.common", "Util"),
        "APP/COMMON/web/shared.css": "s{}\n",
        "STATIC/index.jsp": "<html/>\n",
        "STATIC/WEB-INF/web.xml": "<web-app/>\n",
        "STATIC/WEB-INF/jsp/main.jsp": "<html/>\n",
        "STATIC/css/site.css": "x{}\n",
        "STATIC/resources/templates/report.xml": "<Workbook/>\n",
        "STATIC/resources/form.xlsx": "XLSX\n",
        "DB/schema.sql": "create table t(id int);\n",
    }


def _outside_profile(ws):
    p = profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="portal-aggregator", group_id="com.example.portal",
                version="1.0.0", packaging="pom", base_package="com.example.portal", has_web_xml=True,
                web_xml_paths=["STATIC/WEB-INF/web.xml"], jsp_count=2,
                jsp_paths=["STATIC/WEB-INF/jsp/main.jsp", "STATIC/index.jsp"])
    p.is_multi_module = True
    return p


def _tree(base: Path) -> list[str]:
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


@pytest.mark.parametrize("roles", [True, False], ids=["role-parents", "root-parent"])
def test_directory_outside_the_module_is_folded_into_the_back_module(tmp_path: Path, ccfg: Config,
                                                                    roles: bool) -> None:
    if not roles:
        ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    conf = ("                    <warSourceDirectory>${basedir}/../../STATIC</warSourceDirectory>\n"
            "                    <webResources>\n"
            "                        <resource><directory>../COMMON/web</directory>"
            "<targetPath>shared</targetPath></resource>\n"
            "                        <resource><directory>${basedir}/../../../OUTSIDE</directory></resource>\n"
            "                    </webResources>\n")
    resources = ("        <resources>\n            <resource>\n"
                 "                <directory>${basedir}/../../STATIC/resources</directory>\n"
                 "                <filtering>false</filtering>\n            </resource>\n        </resources>\n")
    ws = make_ws(tmp_path, _outside_files(conf, resources), "outside-" + str(roles))
    res = Restructurer().apply(ws, _outside_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    front = root / "portal-front/src/main/resources"
    assert (front / "META-INF/resources/index.jsp").is_file()
    assert (front / "META-INF/resources/WEB-INF/jsp/main.jsp").is_file()
    assert (front / "static/css/site.css").is_file()
    assert (front / "static/shared/shared.css").is_file()                 # ../COMMON/web of another merged module
    assert (root / "portal-boot/src/main/resources/asis-webinf/web.xml").is_file()
    # what the <resource> inside the web root covers stays classpath content of the back module
    assert _tree(root / "portal-back/STATIC") == ["resources/form.xlsx", "resources/templates/report.xml"]
    back = xml(root / "portal-back/pom.xml")
    dirs = [t(r, "directory") for r in all_(back, "build/resources/resource")]
    assert "STATIC/resources" in dirs and not [d for d in dirs if ".." in (d or "")], dirs
    assert not (root / "STATIC").exists() and not (root / "APP").exists()
    assert (root / "DB/schema.sql").is_file()
    for pom in _all_generated(root, "portal"):
        text = pom.read_text()
        assert "../" not in text.replace("<relativePath>../pom.xml</relativePath>", ""), pom
    assert "web-content-left" not in res.flags
    assert any(n.startswith("directory ../../STATIC of module APP/WEB lies outside the module but inside the "
                            "project (STATIC): moved to portal-back/STATIC") for n in res.notes), res.notes
    assert any(n.startswith("directory ../COMMON/web of module APP/WEB lies in module APP/COMMON") for n in res.notes)
    assert any(n.startswith("directory ../../../OUTSIDE of module APP/WEB resolves outside the project root")
               for n in res.notes)
    assert t(xml(root / "pom.xml"), "artifactId") == "portal"


def test_outside_directory_of_a_single_module_is_only_noted(tmp_path: Path, ccfg: Config) -> None:
    pom = (HEAD + "    <groupId>com.example</groupId>\n    <artifactId>solo</artifactId>\n    <version>1</version>\n"
           "    <build>\n        <resources><resource><directory>../shared-res</directory></resource></resources>\n"
           "    </build>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/com/example/A.java": java("com.example", "A")}, "solo")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="solo", group_id="com.example",
                                           version="1", base_package="com.example"), ccfg, mode="layout")
    assert res.ok, res.error
    assert any(n.startswith("directory ../shared-res of the root pom resolves outside the project root")
               for n in res.notes)
    assert t(xml(ws.root / "solo-back/pom.xml"), "build/resources/resource/directory") == "../shared-res"
