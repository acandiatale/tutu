"""2026-10-23: non-standard web roots declared by maven-war-plugin (`warSourceDirectory`, `<webResources>`) count as
web roots (`SourceStats.webapp_dirs`, jsp_paths / web_static_dirs), large media files under a web root / resource dir."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path, source_scanner
from sbmig.analyze.source_scanner import WebDir, clean_pom_dir, pom_build_dirs, pom_web_dirs, web_static_root
from tests.analyze.helpers import dep, pom, write, write_java


def _war(conf: str) -> str:
    return ("<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-war-plugin</artifactId>"
            f"<version>3.2.3</version><configuration>{conf}</configuration></plugin>")


def test_clean_pom_dir() -> None:
    assert clean_pom_dir("${basedir}/STATIC") == "STATIC"
    assert clean_pom_dir("${project.basedir}/web/root/") == "web/root"
    assert clean_pom_dir("STATIC\\web") == "STATIC/web"
    assert clean_pom_dir("./STATIC") == "STATIC"
    for bad in ("", None, "/opt/web", "C:/web", "${web.root}", "../shared/web", ".", "${basedir}"):
        assert clean_pom_dir(bad) is None, bad


def test_pom_web_dirs_from_war_plugin() -> None:
    text = pom("w", packaging="war", plugins=(_war(
        "<warSourceDirectory>${basedir}/STATIC</warSourceDirectory>"
        "<webResources><resource><directory>extra-web</directory><targetPath>assets</targetPath></resource>"
        "<resource><directory>/abs/web</directory></resource>"
        "<resource><directory>${web.dir}</directory></resource></webResources>"),),
        plugin_mgmt=(_war("<webResources><resource><directory>more</directory></resource></webResources>"),))
    assert pom_web_dirs(text) == [WebDir("STATIC", "root"), WebDir("extra-web", "resources", "assets"),
                                  WebDir("more", "resources")]
    assert pom_web_dirs(pom("j")) == []
    assert pom_web_dirs("<project><build>") == []                      # unparsable: nothing declared


def test_pom_build_dirs() -> None:
    text = ("<project><build><sourceDirectory>APP/src</sourceDirectory><resources>"
            "<resource><directory>STATIC/resources</directory></resource><resource><targetPath>x</targetPath>"
            "</resource></resources></build></project>")
    assert pom_build_dirs(text) == ["APP/src", "STATIC/resources", "src/main/resources"]
    assert pom_build_dirs(pom("j")) == ["src/main/resources"]


def test_web_static_root_custom_roots() -> None:
    roots, res = ["STATIC", "web/STATIC"], ["STATIC/resources", "src/main/resources"]
    assert web_static_root("STATIC/css/app.css", roots, res) == "STATIC"
    assert web_static_root("web/STATIC/img/a.png", roots, res) == "web/STATIC"         # longest match
    assert web_static_root("STATIC/WEB-INF/views/x.html", roots, res) is None
    assert web_static_root("STATIC/resources/img/x.png", roots, res) is None            # classpath resource
    assert web_static_root("STATIC/resources/templates/t.xml", roots, res) is None
    assert web_static_root("STATIC/index.jsp", roots, res) is None                       # JSPs are separate
    assert web_static_root("src/main/webapp/a.css", roots, res) == "src/main/webapp"    # defaults still apply
    assert web_static_root("STATIC/css/app.css") is None                                 # no roots given: old rule


def _ndirs(root: Path, prefix: str = "") -> None:
    p = f"{prefix}/" if prefix else ""
    write(root, f"{p}pom.xml", pom("ndirs", packaging="war",
                                  deps=(dep("javax.servlet", "javax.servlet-api", "3.1.0", scope="provided"),),
                                  plugins=(_war("<warSourceDirectory>${basedir}/STATIC</warSourceDirectory>"),))
          .replace("<build>", "<build><sourceDirectory>APP/src</sourceDirectory><resources><resource>"
                              "<directory>STATIC/resources</directory></resource></resources>"))
    write(root, f"{p}APP/src/com/n/ReportServlet.java", "package com.n;\npublic class ReportServlet {}\n")
    write(root, f"{p}STATIC/index.jsp", "<html/>")
    write(root, f"{p}STATIC/WEB-INF/web.xml", "<web-app/>")
    write(root, f"{p}STATIC/WEB-INF/jsp/report.jsp", "<html/>")
    write(root, f"{p}STATIC/css/app.css", "body{}")
    write(root, f"{p}STATIC/img/logo.png", "PNG")
    write(root, f"{p}STATIC/resources/templates/report.xml", "<x/>")
    write(root, f"{p}STATIC/resources/img/inner.png", "PNG")
    write(root, f"{p}DB/schema.sql", "create table t(id int);")


def test_analyze_custom_web_root(tmp_path: Path, cfg) -> None:
    _ndirs(tmp_path)
    s = analyze_path(tmp_path, cfg).source
    assert s.webapp_dirs == ["STATIC"]
    assert s.jsp_paths == ["STATIC/WEB-INF/jsp/report.jsp", "STATIC/index.jsp"] and s.jsp_count == 2
    assert s.web_static_dirs == ["STATIC"] and s.web_static_count == 2               # resources/ img excluded
    assert s.web_xml_paths == ["STATIC/WEB-INF/web.xml"]
    assert s.large_media_files == [] and s.large_media_bytes == 0


def test_analyze_custom_web_root_multi_module(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("parent", packaging="pom", modules=("web",)))
    _ndirs(tmp_path, "web")
    s = analyze_path(tmp_path, cfg).source
    assert s.webapp_dirs == ["web/STATIC"]
    assert s.web_static_dirs == ["web/STATIC"] and s.web_static_count == 2


def test_default_webapp_listed(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("w", packaging="war"))
    write_java(tmp_path, "", "com.w", "W")
    write(tmp_path, "src/main/webapp/a.css", "x")
    assert analyze_path(tmp_path, cfg).source.webapp_dirs == ["src/main/webapp"]


def test_large_media_files(tmp_path: Path, cfg, monkeypatch) -> None:
    _ndirs(tmp_path)
    write(tmp_path, "STATIC/resources/media/intro.mp4", "x" * 40)
    write(tmp_path, "STATIC/video/tour.MOV", "x" * 50)
    write(tmp_path, "STATIC/video/small.mp4", "x" * 5)                    # below the threshold
    write(tmp_path, "STATIC/docs/manual.pdf", "x" * 60)                   # not a media / archive extension
    write(tmp_path, "DB/dump.zip", "x" * 70)                              # outside every web root / resource dir
    monkeypatch.setattr(source_scanner, "LARGE_MEDIA_BYTES", 10)
    s = analyze_path(tmp_path, cfg).source
    assert s.large_media_files == ["STATIC/resources/media/intro.mp4", "STATIC/video/tour.MOV"]
    assert s.large_media_bytes == 90
    monkeypatch.setattr(source_scanner, "MAX_LARGE_MEDIA", 1)
    s = analyze_path(tmp_path, cfg).source
    assert s.large_media_files == ["STATIC/resources/media/intro.mp4"] and s.large_media_bytes == 90
