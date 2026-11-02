"""v2.1z-c (2026-11-02): hollow-reactor detection — the poms' source / web roots hold nothing while the tree holds the
sources elsewhere (company shape: `APP/WEB/pom.xml` packaging a JDeveloper workspace under `DEV/`, static files in
`STATIC/`). Directory names are never interpreted: only the roots the poms declare count."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze.analyzer import PathView
from sbmig.analyze.hollow import (
    HOLLOW_FLAG,
    HOLLOW_PREFIX,
    OUTSIDE_PREFIX,
    detect_hollow,
    hollow_followup,
    module_roots,
    outside_followup,
)
from sbmig.analyze.pom_parser import parse_poms
from sbmig.report.i18n import tr_line

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
JAVA = "package com.example.hollow;\npublic class HelloServlet {}\n"
WEBXML = '<?xml version="1.0"?><web-app version="3.1"/>'


def _pom(aid: str, packaging: str = "war", build: str = "", props: str = "", modules: tuple[str, ...] = (),
         parent: str = "") -> str:
    mods = "<modules>" + "".join(f"<module>{m}</module>" for m in modules) + "</modules>" if modules else ""
    return (f'<project {NS}><modelVersion>4.0.0</modelVersion>{parent}<groupId>com.example</groupId>'
            f"<artifactId>{aid}</artifactId><version>1.0</version><packaging>{packaging}</packaging>"
            f"{'<properties>' + props + '</properties>' if props else ''}{mods}{'<build>' + build + '</build>' if build else ''}"
            "</project>")


def _tree(tmp_path: Path, files: dict[str, str]) -> PathView:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return PathView(tmp_path)


def _detect(ws: PathView):
    poms, _ = parse_poms(ws)
    return detect_hollow(ws, poms)


HOLLOW = {".gitignore": "target/\n", "pom.xml": _pom("hollow"), "src/main/webapp/WEB-INF/web.xml": WEBXML,
          "DEV/s10/src/com/example/hollow/HelloServlet.java": JAVA, "DEV/s10/src/com/example/hollow/Util.java": JAVA,
          "DEV/s10/public_html/WEB-INF/web.xml": WEBXML, "DEV/s10/public_html/index.jsp": "<%= 1 %>",
          "STATIC/css/app.css": "body{}", "STATIC/js/app.js": "1;", "DB/schema.sql": "-- sql"}


def test_a_war_whose_sources_live_outside_its_roots_is_hollow(tmp_path):
    res = _detect(_tree(tmp_path, HOLLOW))
    assert res.hollow and not res.sources_outside and res.war and not res.error
    assert res.inside == {"java": 0, "jsp": 0, "web": 0}
    assert res.outside == {"java": 2, "jsp": 1, "web": 2}
    assert res.outside_by_dir == {"DEV": {"java": 2, "jsp": 1}, "STATIC": {"web": 2}}
    assert set(res.roots) >= {"src/main/java", "src/main/resources", "src/main/webapp"} and res.unresolved == []
    line = hollow_followup(res)
    assert line.startswith(HOLLOW_PREFIX) and "2 java, 1 jsp, 2 web files" in line
    assert "(DEV: java 2, jsp 1; STATIC: web 2)" in line and "unresolved" not in line
    assert tr_line("ko", line).startswith("Maven 리액터가 컴파일하는 소스가 없다: 모듈 밖에 2 java, 1 jsp, 2 web files이(가) 있고")
    assert HOLLOW_FLAG == "hollow-reactor"


def test_a_healthy_servlet_project_is_neither_hollow_nor_outside(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("ok"), "src/main/java/com/example/A.java": JAVA,
                          "src/main/webapp/WEB-INF/web.xml": WEBXML, "src/main/webapp/index.jsp": "x",
                          "src/main/webapp/css/a.css": "x", "src/test/java/com/example/ATest.java": JAVA,
                          "src/test/java/com/example/BTest.java": JAVA, "docs/index.html": "<html/>"})
    res = _detect(ws)
    assert not res.hollow and not res.sources_outside
    assert res.inside == {"java": 1, "jsp": 1, "web": 1}
    assert res.outside == {"java": 0, "jsp": 0, "web": 1} and res.outside_by_dir == {"docs": {"web": 1}}
    assert "src/test" in res.ignored                                         # test sources count for neither side


def test_a_custom_source_directory_outside_the_module_counts_as_inside(tmp_path):
    build = ("<sourceDirectory>../../DEV/s10/src</sourceDirectory><plugins><plugin><artifactId>maven-war-plugin"
             "</artifactId><configuration><warSourceDirectory>${basedir}/../../DEV/s10/public_html"
             "</warSourceDirectory></configuration></plugin></plugins>")
    ws = _tree(tmp_path, {"APP/pom.xml": _pom("app", "pom", modules=("WEB",)),
                          "APP/WEB/pom.xml": _pom("web", build=build),
                          "DEV/s10/src/com/example/hollow/HelloServlet.java": JAVA,
                          "DEV/s10/public_html/index.jsp": "x", "DEV/s10/public_html/WEB-INF/web.xml": WEBXML})
    res = _detect(ws)
    assert not res.hollow and res.inside == {"java": 1, "jsp": 1, "web": 0} and res.outside_total == 0
    assert "DEV/s10/src" in res.roots and "DEV/s10/public_html" in res.roots


def test_unresolved_property_directories_are_recorded_not_guessed(tmp_path):
    build = ("<sourceDirectory>${DIR_xxxJAR}</sourceDirectory><resources><resource><directory>${DIR_RMIJAR}"
             "</directory></resource></resources><plugins><plugin><artifactId>maven-war-plugin</artifactId>"
             "<configuration><warSourceDirectory>${DIR_xxxWEB}</warSourceDirectory></configuration></plugin></plugins>")
    ws = _tree(tmp_path, {"pom.xml": _pom("hollow", build=build), "src/main/webapp/WEB-INF/web.xml": WEBXML,
                          "DEV/s10/src/com/example/hollow/HelloServlet.java": JAVA,
                          "DEV/s10/public_html/index.jsp": "x"})
    res = _detect(ws)
    assert res.hollow and res.unresolved == ["${DIR_xxxJAR}", "${DIR_RMIJAR}", "${DIR_xxxWEB}"]
    assert "DEV" not in " ".join(res.roots)
    line = hollow_followup(res)
    assert "(unresolved directories: ${DIR_xxxJAR}, ${DIR_RMIJAR}, ${DIR_xxxWEB})" in line
    assert "못 푼 디렉토리 식: ${DIR_xxxJAR}, ${DIR_RMIJAR}, ${DIR_xxxWEB}" in tr_line("ko", line)


def test_properties_of_the_in_repo_parent_resolve_a_directory(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("root", "pom", props="<src.dir>../DEV/src</src.dir>", modules=("WEB",)),
                          "WEB/pom.xml": _pom("web", build="<sourceDirectory>${src.dir}</sourceDirectory>",
                                              parent="<parent><groupId>com.example</groupId><artifactId>root"
                                                     "</artifactId><version>1.0</version></parent>"),
                          "DEV/src/com/example/A.java": JAVA, "WEB/src/main/webapp/WEB-INF/web.xml": WEBXML,
                          "WEB/src/main/webapp/index.jsp": "x"})
    res = _detect(ws)
    assert not res.hollow and res.inside["java"] == 1 and res.unresolved == [] and "DEV/src" in res.roots


def test_more_java_outside_than_inside_is_the_soft_verdict(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("x", "jar"), "src/main/java/com/example/A.java": JAVA,
                          "old/src/com/example/B.java": JAVA, "old/src/com/example/C.java": JAVA,
                          "tools/D.java": JAVA})
    res = _detect(ws)
    assert not res.hollow and res.sources_outside and res.outside == {"java": 3, "jsp": 0, "web": 0}
    line = outside_followup(res)
    assert line.startswith(OUTSIDE_PREFIX) and "(old: java 2; tools: java 1)" in line
    assert tr_line("ko", line).startswith("Maven 리액터 밖의 소스: 모듈의 소스 루트 밖에 3 java files이(가) 있고")


def test_multi_module_reactor_with_sources_in_a_module_and_no_poms(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("root", "pom", modules=("core",)),
                          "core/pom.xml": _pom("core", "jar"), "core/src/main/java/com/example/A.java": JAVA,
                          "core/target/classes/com/example/A.java": JAVA})           # build output is pruned
    res = _detect(ws)
    assert not res.hollow and not res.sources_outside and res.inside["java"] == 1 and res.outside_total == 0
    assert not detect_hollow(ws, []).hollow                                  # no poms → nothing to judge


def test_module_roots_lists_defaults_and_declared_roots(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("w", build="<resources><resource><directory>conf</directory></resource>"
                                                     "</resources>")})
    poms, _ = parse_poms(ws)
    roots, ignored, unresolved, war = module_roots(ws, poms)
    assert roots == ["src/main/java", "conf", "src/main/webapp"] and ignored == ["src/test"]
    assert unresolved == [] and war
