"""Build directories a module inherits from a parent pom inside the project count for the hollow-reactor verdict
(review P3): `<sourceDirectory>`, the test source directory, `<resources>` and the war plugin's directories, the
child's own values winning."""
from __future__ import annotations

from sbmig.analyze.hollow import module_roots

from .test_hollow import JAVA, WEBXML, _detect, _pom, _tree

PARENT_REF = ("<parent><groupId>com.example</groupId><artifactId>parent</artifactId><version>1.0</version>"
              "<relativePath>../pom.xml</relativePath></parent>")
WAR_PLUGIN = ("<plugins><plugin><artifactId>maven-war-plugin</artifactId><configuration>"
              "<warSourceDirectory>web</warSourceDirectory></configuration></plugin></plugins>")


def test_an_inherited_source_directory_is_not_a_hollow_reactor(tmp_path):
    build = ("<sourceDirectory>src</sourceDirectory><testSourceDirectory>test</testSourceDirectory>"
             "<resources><resource><directory>conf</directory></resource></resources>")
    ws = _tree(tmp_path, {"pom.xml": _pom("parent", "pom", build=build, modules=("app",)),
                          "app/pom.xml": _pom("app", "jar", parent=PARENT_REF),
                          "app/src/com/example/A.java": JAVA, "app/test/com/example/ATest.java": JAVA})
    res = _detect(ws)
    assert not res.hollow and not res.sources_outside
    assert res.inside["java"] == 1 and res.outside["java"] == 0          # the test root is ignored, not "outside"
    assert "app/src" in res.roots and "app/conf" in res.roots and "app/test" in res.ignored
    assert "app/src/main/java" not in res.roots and "app/src/main/resources" not in res.roots


def test_the_childs_own_values_win(tmp_path):
    build = "<sourceDirectory>src</sourceDirectory><resources><resource><directory>conf</directory></resource></resources>"
    child = "<sourceDirectory>java</sourceDirectory>"
    ws = _tree(tmp_path, {"pom.xml": _pom("parent", "pom", build=build, modules=("app",)),
                          "app/pom.xml": _pom("app", "jar", parent=PARENT_REF, build=child),
                          "app/java/com/example/A.java": JAVA})
    roots, _ign, _unres, _war = module_roots(ws, __import__("sbmig.analyze.pom_parser", fromlist=["x"])
                                             .parse_poms(ws)[0])
    assert "app/java" in roots and "app/src" not in roots and "app/conf" in roots


def test_an_inherited_war_source_directory_counts(tmp_path):
    ws = _tree(tmp_path, {"pom.xml": _pom("parent", "pom", build=WAR_PLUGIN, modules=("app",)),
                          "app/pom.xml": _pom("app", "war", parent=PARENT_REF),
                          "app/src/main/java/com/example/A.java": JAVA,
                          "app/web/WEB-INF/web.xml": WEBXML, "app/web/index.jsp": "<%= 1 %>"})
    res = _detect(ws)
    assert not res.hollow and "app/web" in res.roots and res.outside["jsp"] == 0
