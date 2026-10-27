"""2026-10-23: a custom maven-war-plugin web root (`<warSourceDirectory>STATIC</warSourceDirectory>`) is split by
the restructure — JSPs + static web files → `{name}-front`, WEB-INF configs → the boot stash, what a `<resource>`
inside the web root covers (`STATIC/resources`) → the comp module's classpath; no `warSourceDirectory` /
`webResources` reaches a generated pom; large media files get a note."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import Restructurer

from .helpers import HEAD, all_, dep, make_ws, profile, t, xml
from .test_company_layout import _roles

WAR_PLUGIN = ("        <plugin>\n            <groupId>org.apache.maven.plugins</groupId>\n"
              "            <artifactId>maven-war-plugin</artifactId>\n            <version>3.2.3</version>\n"
              "            <configuration>\n{conf}            </configuration>\n        </plugin>\n")


def _pom(conf: str, management: str = "") -> str:
    return (HEAD + "    <groupId>com.example</groupId>\n    <artifactId>ndirs</artifactId>\n"
            "    <version>1.0.0</version>\n    <packaging>war</packaging>\n"
            "    <properties>\n        <maven.compiler.source>1.8</maven.compiler.source>\n"
            "        <maven.compiler.target>1.8</maven.compiler.target>\n    </properties>\n"
            "    <dependencies>\n" + dep("javax.servlet", "javax.servlet-api", "3.1.0", "provided")
            + "    </dependencies>\n    <build>\n        <sourceDirectory>APP/src</sourceDirectory>\n"
            "        <resources>\n            <resource>\n                <directory>STATIC/resources</directory>\n"
            "                <filtering>true</filtering>\n            </resource>\n        </resources>\n"
            + (f"        <pluginManagement>\n        <plugins>\n{management}        </plugins>\n"
               "        </pluginManagement>\n" if management else "")
            + "        <plugins>\n" + WAR_PLUGIN.format(conf=conf) + "        </plugins>\n    </build>\n</project>\n")


def _files(conf: str = "                <warSourceDirectory>${basedir}/STATIC</warSourceDirectory>\n"
                       "                <failOnMissingWebXml>true</failOnMissingWebXml>\n",
           management: str = "") -> dict[str, str]:
    return {
        "pom.xml": _pom(conf, management),
        "APP/src/com/example/ndirs/web/ReportServlet.java":
            "package com.example.ndirs.web;\n\npublic class ReportServlet extends javax.servlet.http.HttpServlet {}\n",
        "STATIC/index.jsp": "<html/>\n",
        "STATIC/WEB-INF/web.xml": "<web-app/>\n",
        "STATIC/WEB-INF/jsp/report.jsp": '<%@ page import="javax.servlet.http.HttpSession" %>\n',
        "STATIC/WEB-INF/lib/old.jar": "jar\n",
        "STATIC/css/app.css": "body{}\n",
        "STATIC/img/logo.png": "PNG\n",
        "STATIC/docs/manual.pdf": "PDF\n",                       # not a static extension, not a resource: web content
        "STATIC/resources/templates/report-template.xml": "<Workbook>${rows}</Workbook>\n",
        "STATIC/resources/sample.xlsx": "XLSX\n",
        "STATIC/resources/media/intro.mp4": "MP4\n",
        "DB/schema.sql": "create table t(id int);\n",
    }


def _profile(ws, **kw):
    src = {"base_package": "com.example.ndirs", "servlet_classes": ["com.example.ndirs.web.ReportServlet"],
           "has_web_xml": True, "web_xml_paths": ["STATIC/WEB-INF/web.xml"], "jsp_count": 2,
           "jsp_paths": ["STATIC/WEB-INF/jsp/report.jsp", "STATIC/index.jsp"], "web_static_dirs": ["STATIC"],
           "web_static_count": 2, "webapp_dirs": ["STATIC"]}
    src.update(kw)
    return profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="ndirs", group_id="com.example", version="1.0.0",
                   packaging="war", **src)


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def _tree(ws, d: str = "") -> list[str]:
    base = ws.root / d if d else ws.root
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and ".git" not in p.parts)


def _war_conf_texts(x) -> str:
    return "".join(t(p, "configuration") or "" for p in [*all_(x, "build/plugins/plugin"),
                                                          *all_(x, "build/pluginManagement/plugins/plugin")])


@pytest.mark.parametrize("roles", [True, False], ids=["role-parents", "root-parent"])
def test_custom_web_root_split(tmp_path: Path, ccfg: Config, roles: bool) -> None:
    if not roles:
        ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    mgmt = WAR_PLUGIN.format(conf="                <webResources><resource><directory>STATIC</directory>"
                                  "</resource></webResources>\n")
    ws = make_ws(tmp_path, _files(management=mgmt), "ndirs")
    res = Restructurer().apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    front = "ndirs-front/src/main/resources"
    assert (root / f"{front}/META-INF/resources/WEB-INF/jsp/report.jsp").is_file()
    assert (root / f"{front}/META-INF/resources/index.jsp").is_file()
    assert (root / f"{front}/static/css/app.css").is_file() and (root / f"{front}/static/img/logo.png").is_file()
    assert (root / f"{front}/static/docs/manual.pdf").is_file()
    assert (root / "ndirs-boot/src/main/resources/asis-webinf/web.xml").is_file()
    # what <resource><directory>STATIC/resources covers stays classpath content of the comp module …
    assert _tree(ws, "ndirs-back/STATIC") == ["WEB-INF/lib/old.jar", "resources/media/intro.mp4",
                                              "resources/sample.xlsx", "resources/templates/report-template.xml"]
    # … AND (2026-10-28, layout.front_copy_resource_dirs) is served as before from the front jar, same web path
    assert _tree(ws, f"{front}/static/resources") == ["media/intro.mp4", "sample.xlsx",
                                                      "templates/report-template.xml"]
    assert (root / f"{front}/static/resources/media/intro.mp4").read_bytes() == \
        (root / "ndirs-back/STATIC/resources/media/intro.mp4").read_bytes()
    assert (root / "ndirs-back/APP/src/com/example/ndirs/web/ReportServlet.java").is_file()
    assert not (root / "STATIC").exists() and not (root / "APP").exists()
    assert (root / "DB/schema.sql").is_file()                                  # unreferenced: left alone
    comp = xml(root / "ndirs-back/pom.xml")
    assert t(comp, "build/sourceDirectory") == "APP/src"
    assert t(comp, "build/resources/resource/directory") == "STATIC/resources"
    boot = xml(root / "ndirs-boot/pom.xml")
    assert t(boot, "packaging") == "war" and res.boot_packaging == "war"
    for rel in ("pom.xml", "ndirs-boot/pom.xml", "ndirs-back/pom.xml", "ndirs-front/pom.xml"):
        text = (root / rel).read_text()
        assert "warSourceDirectory" not in text and "webResources" not in text, rel
    assert any(n.startswith("front module ndirs-front: 3 static web file(s)") and "web root(s) from "
               "maven-war-plugin: STATIC" in n for n in res.notes), res.notes
    assert any(n.startswith("3 file(s) under a <resource> / source directory inside a web root stay in ndirs-back")
               and n.endswith("served as before from the front jar's src/main/resources/static copy") for n in res.notes)
    assert any("3 file(s) of <resource> dir(s) inside the web root copied to src/main/resources/static at their web "
               "path (the classpath copies stay in ndirs-back)" in n for n in res.notes), res.notes
    assert res.front_files == 3 + 2 + 3                                       # static + JSPs + copies
    assert any("maven-war-plugin webResources dropped from" in n for n in res.notes), res.notes
    assert any("WEB-INF/lib, WEB-INF/classes or META-INF stay in ndirs-back/STATIC" in f for f in res.manual_followups)
    assert not any("large media" in n for n in res.notes)


def test_static_files_under_a_resource_dir_are_copied_to_the_front_jar(tmp_path: Path, ccfg: Config) -> None:
    """2026-10-28 (user: "정적 리소스들도 front resources 에 디렉토리 그대로"): the css under the <resource> dir keeps its
    classpath copy in the comp module and is also served from the front jar; no follow-up any more."""
    files = _files()
    files["STATIC/resources/css/print.css"] = "@media print{}\n"
    ws = make_ws(tmp_path, files, "ndirs")
    res = Restructurer().apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "ndirs-back/STATIC/resources/css/print.css").is_file()
    assert (ws.root / "ndirs-front/src/main/resources/static/resources/css/print.css").read_text() == "@media print{}\n"
    assert not any("static web file(s) under a <resource> directory" in f for f in res.manual_followups)
    assert any("4 file(s) of <resource> dir(s) inside the web root copied" in n for n in res.notes), res.notes


def test_static_files_under_a_resource_dir_get_a_followup_when_copying_is_off(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.front_copy_resource_dirs = False                             # the pre-10-28 behaviour
    files = _files()
    files["STATIC/resources/css/print.css"] = "@media print{}\n"
    ws = make_ws(tmp_path, files, "ndirs")
    res = Restructurer().apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "ndirs-back/STATIC/resources/css/print.css").is_file()
    assert not (ws.root / "ndirs-front/src/main/resources/static/resources").exists()
    assert any(f.startswith("1 static web file(s) under a <resource> directory inside the web root stay classpath "
                            "resources in ndirs-back (ndirs-back/STATIC/resources/css/print.css)")
               for f in res.manual_followups), res.manual_followups


def test_webinf_views_and_fragments_go_to_the_front_jar_configs_to_the_stash(tmp_path: Path, ccfg: Config) -> None:
    """2026-10-28: WEB-INF files that are not configuration (html / js / css fragments, templates, downloads) keep
    their path under the front jar's META-INF/resources/WEB-INF/ (jsp:include / RequestDispatcher reach them, the
    browser does not); web.xml / Spring XML / properties still go to the boot stash."""
    files = _files()
    files["STATIC/WEB-INF/views/frag.html"] = "<div/>\n"
    files["STATIC/WEB-INF/js/inner.js"] = "x();\n"
    files["STATIC/WEB-INF/spring/app-context.xml"] = "<beans/>\n"
    files["STATIC/WEB-INF/classes/app.properties"] = "a=b\n"
    ws = make_ws(tmp_path, files, "ndirs")
    res = Restructurer().apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    front = ws.root / "ndirs-front/src/main/resources/META-INF/resources/WEB-INF"
    assert (front / "views/frag.html").is_file() and (front / "js/inner.js").is_file()
    assert (front / "jsp/report.jsp").is_file()
    stash = ws.root / "ndirs-boot/src/main/resources/asis-webinf"
    assert (stash / "web.xml").is_file() and (stash / "spring/app-context.xml").is_file()
    assert not (stash / "views").exists()
    assert (ws.root / "ndirs-back/STATIC/WEB-INF/classes/app.properties").is_file()     # classes/: left, follow-up


def test_web_resources_target_path(tmp_path: Path, ccfg: Config) -> None:
    conf = ("                <warSourceDirectory>STATIC</warSourceDirectory>\n"
            "                <webResources><resource><directory>extra-web</directory><targetPath>assets</targetPath>"
            "</resource></webResources>\n")
    files = _files(conf)
    files["extra-web/theme.css"] = "x{}\n"
    files["extra-web/WEB-INF/extra.xml"] = "<x/>\n"
    ws = make_ws(tmp_path, files, "ndirs")
    res = Restructurer().apply(ws, _profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    assert (root / "ndirs-front/src/main/resources/static/assets/theme.css").is_file()
    # targetPath prefixes the web path: assets/WEB-INF/extra.xml is not under the web root's WEB-INF
    assert (root / "ndirs-front/src/main/resources/static/assets/WEB-INF/extra.xml").is_file()
    assert not (root / "extra-web").exists() and not (root / "ndirs-back/extra-web").exists()
    assert "webResources" not in (root / "ndirs-boot/pom.xml").read_text()


def test_large_media_note(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _files(), "ndirs")
    prof = _profile(ws, large_media_files=["STATIC/resources/media/intro.mp4"], large_media_bytes=12 * 1024 * 1024)
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert "1 large media file(s) (12 MB) are packaged into the front / comp jar: consider moving them to a file " \
           "server (STATIC/resources/media/intro.mp4)" in res.notes


def test_default_webapp_unchanged_without_war_config(tmp_path: Path, ccfg: Config) -> None:
    """A project without warSourceDirectory keeps today's behaviour: only src/main/webapp is a web root, a `STATIC/`
    directory nothing declares is left at the root."""
    files = {k: v for k, v in _files().items() if not k.startswith("STATIC/")}
    files["pom.xml"] = _pom("                <failOnMissingWebXml>false</failOnMissingWebXml>\n").replace(
        "STATIC/resources", "src/main/resources")
    files["src/main/webapp/WEB-INF/jsp/a.jsp"] = "<p/>\n"
    files["STATIC/notes.txt"] = "x\n"
    ws = make_ws(tmp_path, files, "ndirs")
    res = Restructurer().apply(ws, _profile(ws, web_xml_paths=[], has_web_xml=False,
                                            jsp_paths=["src/main/webapp/WEB-INF/jsp/a.jsp"]), ccfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "ndirs-front/src/main/resources/META-INF/resources/WEB-INF/jsp/a.jsp").is_file()
    assert (ws.root / "STATIC/notes.txt").is_file()
    assert not any("web root(s) from maven-war-plugin" in n for n in res.notes)
    assert "<failOnMissingWebXml>" not in (ws.root / "ndirs-boot/pom.xml").read_text()
