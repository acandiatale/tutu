"""2026-10-23: sibling poms without an aggregator (`APP/BATCH` jar, `APP/WEB` war, `APP/EAR` ear packaging both) —
the intake writes a root aggregator pom (`sbmig.analyze.aggregator`, artifactId `<name>-aggregator` when the EAR has
`<group>:<name>`, property `sbmig.project-name`), then the merge restructure runs: the EAR module merges no sources
(its descriptors go to `<boot>/src/main/resources/asis-ear/`, its lib dependencies to the back module, its context
root becomes a follow-up), no dependency on a module of this project reaches a generated pom, the root takes the
project name, standalone `main` classes of merged jar modules and web content no pom references get follow-ups.

The aggregator pom is written by hand here (the intake writes it in a real run)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.core.workspace import Workspace
from sbmig.restructure import PomEditor, Restructurer

from .helpers import HEAD, all_, dep, dep_keys, java, make_ws, profile, t, xml
from .test_company_layout import _roles

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "projects" / "ear-sibling-poms"
GROUP = "com.example.portal"
PROJECT_INTERNAL = {"portalWEB", "portalBATCH", "portal"}


def _aggregator(aid: str, name: str | None = "portal") -> str:
    props = f"    <properties>\n        <sbmig.project-name>{name}</sbmig.project-name>\n    </properties>\n" \
        if name else ""
    return (HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>{aid}</artifactId>\n    <version>1.0.0</version>\n"
            "    <packaging>pom</packaging>\n" + props
            + "    <modules>\n        <module>APP/BATCH</module>\n        <module>APP/WEB</module>\n"
              "        <module>APP/EAR</module>\n    </modules>\n</project>\n")


def _fixture_ws(tmp_path: Path, aid: str = "portal-aggregator") -> Workspace:
    src = tmp_path / "src-in" / "portal"
    shutil.copytree(FIXTURE, src)
    for extra in ("sbmig.e2e.yml", "expected.json", "README.md"):
        (src / extra).unlink(missing_ok=True)
    (src / "pom.xml").write_text(_aggregator(aid), encoding="utf-8")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-ear")
    ws.git_init()
    return ws


def _fixture_profile(ws: Workspace, aid: str = "portal-aggregator"):
    p = profile(ws, CaseType.SERVLET_TOMCAT, artifact_id=aid, group_id=GROUP, version="1.0.0", packaging="pom",
                base_package="com.example.portal", servlet_classes=["com.example.portal.web.NoticeServlet"],
                has_web_xml=True, web_xml_paths=["APP/WEB/src/main/webapp/WEB-INF/web.xml"], jsp_count=2,
                jsp_paths=["APP/WEB/src/main/webapp/WEB-INF/jsp/notice/list.jsp", "STATIC/index.jsp"],
                psvm_classes=["com.example.portal.batch.NoticeCleaner"], webapp_dirs=["APP/WEB/src/main/webapp"])
    p.is_multi_module = True
    return p


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The company layout (config/default.yml layout defaults) with the in-house role parents."""
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def _generated_poms(root: Path) -> list[Path]:
    return [root / "pom.xml", *(root / m / "pom.xml" for m in ("portal-back", "portal-front", "portal-boot"))]


def _tree(base: Path) -> dict[str, bytes]:
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------------------------- end to end
@pytest.mark.parametrize("roles", [True, False], ids=["role-parents", "root-parent"])
def test_ear_sibling_poms_end_to_end(tmp_path: Path, ccfg: Config, roles: bool) -> None:
    if not roles:
        ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = _fixture_ws(tmp_path)
    res = Restructurer().apply(ws, _fixture_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    r = xml(root / "pom.xml")
    assert [m.text for m in all_(r, "modules/module")] == ["portal-back", "portal-front", "portal-boot"]
    assert t(r, "artifactId") == "portal" and t(r, "packaging") == "pom"         # renamed from portal-aggregator
    assert any(n.startswith("root pom artifactId portal-aggregator → portal (sbmig.project-name") for n in res.notes)
    for pom in _generated_poms(root):
        text = pom.read_text()
        assert "maven-ear-plugin" not in text and "sbmig.project-name" not in text, pom
        x = xml(pom)
        for path in ("dependencies/dependency", "dependencyManagement/dependencies/dependency"):
            assert not {k.split(":")[1] for k in dep_keys(x, path)} & PROJECT_INTERNAL, (pom, dep_keys(x, path))
    # module artifactIds follow the project name; non-role modules inherit the renamed root
    for m in ("portal-back", "portal-front", "portal-boot"):
        x = xml(root / m / "pom.xml")
        assert t(x, "artifactId") == m
        if not roles:
            assert (t(x, "parent/groupId"), t(x, "parent/artifactId"), t(x, "parent/relativePath")) == (
                GROUP, "portal", "../pom.xml")
        else:
            assert t(x, "parent/groupId") == "com.company.platform"
    back = root / "portal-back"
    assert (back / "src/main/java/com/example/portal/web/NoticeServlet.java").is_file()
    assert (back / "src/main/java/com/example/portal/batch/NoticeCleaner.java").is_file()
    assert "commons-io:commons-io" in dep_keys(xml(back / "pom.xml"))           # the EAR's lib/ classpath
    front = root / "portal-front/src/main/resources"
    assert (front / "META-INF/resources/WEB-INF/jsp/notice/list.jsp").is_file()
    assert (front / "static/css/portal.css").is_file()
    boot = xml(root / "portal-boot/pom.xml")
    assert t(boot, "packaging") == "war" and res.boot_packaging == "war"
    stash = root / "portal-boot/src/main/resources/asis-ear/EAR/src/main/application/META-INF/jeus-application-dd.xml"
    assert stash.read_bytes() == (FIXTURE / "APP/EAR/src/main/application/META-INF/jeus-application-dd.xml").read_bytes()
    # STATIC/ (referenced by no pom) and DB/ are left untouched; the module directories are gone
    assert _tree(root / "STATIC") == _tree(FIXTURE / "STATIC")
    assert _tree(root / "DB") == _tree(FIXTURE / "DB")
    assert not (root / "APP").exists()
    assert res.merged_modules == ["APP/BATCH", "APP/WEB"]
    assert {"ear-module-dropped", "web-content-left", "web-xml-registered"} <= set(res.flags)
    assert "war-overlay-moved" not in res.flags
    fu = res.manual_followups
    assert ("EAR APP/EAR deployed portalWEB at context root /portal: run portal-boot as an executable WAR (java -jar, "
            "embedded Tomcat) with server.servlet.context-path=/portal, or deploy it as portal.war on an external Tomcat") in fu
    assert ("web content not referenced by any pom left in place: STATIC/ (1 JSP, 2 other files) — serve it as before "
            "(web server) or move it into portal-front") in fu
    assert ("module APP/BATCH had main class com.example.portal.batch.NoticeCleaner (a standalone batch?): merged into "
            "portal-back — run it from the boot WAR's classpath or keep it as its own module") in fu
    assert not any(f.startswith("existing main class(es)") for f in fu)            # reported per module instead
    ear_note = next(n for n in res.notes if n.startswith("EAR module APP/EAR (com.example.portal:portal) dropped"))
    assert "portal-boot/src/main/resources/asis-ear/EAR/" in ear_note
    assert "com.example.portal:portalWEB, com.example.portal:portalBATCH" in ear_note
    assert "commons-io:commons-io" in ear_note


def test_ear_sibling_poms_with_the_analyzer(tmp_path: Path, ccfg: Config) -> None:
    """The same tree through the real analyzer (as the orchestrator runs it after the intake aggregator)."""
    from sbmig.analyze.analyzer import analyze_project

    ws = _fixture_ws(tmp_path)
    prof = analyze_project(ws, ccfg)
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    assert t(xml(root / "pom.xml"), "artifactId") == "portal"
    assert (root / "portal-front/src/main/resources/META-INF/resources/WEB-INF/jsp/notice/list.jsp").is_file()
    assert (root / "portal-boot/src/main/resources/asis-ear/EAR/src/main/application/META-INF/"
                   "jeus-application-dd.xml").is_file()
    assert {"ear-module-dropped", "web-content-left"} <= set(res.flags)
    for pom in _generated_poms(root):
        assert "portalWEB" not in pom.read_text() and "maven-ear-plugin" not in pom.read_text()


def test_aggregator_named_like_the_project_is_not_renamed(tmp_path: Path, ccfg: Config) -> None:
    """`sbmig.project-name` equal to the aggregator's artifactId: nothing to rename, the marker property goes."""
    ws = _fixture_ws(tmp_path, aid="portal-app")
    (ws.root / "pom.xml").write_text(_aggregator("portal-app", name="portal-app"), encoding="utf-8")
    ws.commit("aggregator")
    res = Restructurer().apply(ws, _fixture_profile(ws, "portal-app"), ccfg, mode="layout")
    assert res.ok, res.error
    r = xml(ws.root / "pom.xml")
    assert t(r, "artifactId") == "portal-app"
    assert [m.text for m in all_(r, "modules/module")] == ["portal-app-back", "portal-app-front", "portal-app-boot"]
    assert "sbmig.project-name" not in (ws.root / "pom.xml").read_text()
    assert not any(n.startswith("root pom artifactId") for n in res.notes)


# ---------------------------------------------------------------------------------------------- EAR details
def _ear_pom(extra_deps: str = "", plugin: str = "", app_group: str = "${project.groupId}") -> str:
    return (HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>shop</artifactId>\n    <version>2.0</version>\n"
            "    <packaging>ear</packaging>\n    <dependencies>\n"
            + dep(app_group, "shopWEB", "${project.version}", extra="            <type>war</type>\n")
            + extra_deps + "    </dependencies>\n"
            + (f"    <build>\n        <plugins>\n{plugin}        </plugins>\n    </build>\n" if plugin else "")
            + "</project>\n")


def _small_ear_files(ear_pom: str, web_jsp: bool = True, root_extra: str = "") -> dict[str, str]:
    files = {
        "pom.xml": HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>shop-aggregator</artifactId>\n"
                   "    <version>2.0</version>\n    <packaging>pom</packaging>\n"
                   "    <properties>\n        <sbmig.project-name>shop</sbmig.project-name>\n    </properties>\n"
                   "    <modules>\n        <module>WEB</module>\n        <module>EAR</module>\n    </modules>\n"
                   + root_extra + "</project>\n",
        "WEB/pom.xml": HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>shopWEB</artifactId>\n"
                       "    <version>2.0</version>\n    <packaging>war</packaging>\n    <dependencies>\n"
                       + dep("javax.servlet", "javax.servlet-api", "3.1.0", "provided") + "    </dependencies>\n"
                       "</project>\n",
        "WEB/src/main/java/com/example/shop/web/Home.java": java("com.example.shop.web", "Home"),
        "EAR/pom.xml": ear_pom,
        "EAR/src/main/application/META-INF/weblogic-application.xml": "<weblogic-application/>\n",
    }
    if web_jsp:
        files["WEB/src/main/webapp/WEB-INF/jsp/home.jsp"] = "<html/>\n"
    return files


def _small_profile(ws, jsp: bool = True):
    p = profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="shop-aggregator", group_id=GROUP, version="2.0",
                packaging="pom", base_package="com.example.shop", jsp_count=1 if jsp else 0,
                jsp_paths=["WEB/src/main/webapp/WEB-INF/jsp/home.jsp"] if jsp else [])
    p.is_multi_module = True
    return p


EAR_PLUGIN_XML = ("            <plugin>\n                <artifactId>maven-ear-plugin</artifactId>\n"
                  "                <version>3.3.0</version>\n{conf}            </plugin>\n")


def test_ear_application_xml_default_context_root_and_external_web_module(tmp_path: Path, ccfg: Config) -> None:
    """Context roots from application.xml, the maven-ear-plugin default (`/<artifactId>`) for a WAR without one,
    an external WAR web module (not of this project) is never carried; maven-ear-plugin in the root pluginManagement
    does not reach a generated pom."""
    ext = dep("com.partner", "partner-portal", "1.4", extra="            <type>war</type>\n")
    ear = _ear_pom(extra_deps=ext + dep("org.apache.commons", "commons-text", "1.10.0"))
    root_mgmt = ("    <build>\n        <pluginManagement>\n            <plugins>\n"
                 + EAR_PLUGIN_XML.format(conf="").replace("            ", "                ")
                 + "            </plugins>\n        </pluginManagement>\n    </build>\n")
    files = _small_ear_files(ear, root_extra=root_mgmt)
    files["EAR/src/main/application/META-INF/application.xml"] = (
        '<application xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="7">\n  <module>\n    <web>\n'
        "      <web-uri>shopWEB-2.0.war</web-uri>\n      <context-root>/shop</context-root>\n    </web>\n"
        "  </module>\n</application>\n")
    ws = make_ws(tmp_path, files, "ear-appxml")
    res = Restructurer().apply(ws, _small_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    assert ("EAR EAR deployed shopWEB at context root /shop: run shop-boot as an executable WAR (java -jar, embedded "
            "Tomcat) with server.servlet.context-path=/shop, or deploy it as shop.war on an external Tomcat") in res.manual_followups
    assert any(f.startswith("EAR EAR deployed partner-portal at context root /partner-portal (the maven-ear-plugin "
                            "default)") for f in res.manual_followups)
    assert any(f.startswith("EAR module EAR also packaged the web module com.partner:partner-portal:1.4")
               for f in res.manual_followups)
    for rel in ("pom.xml", "shop-back/pom.xml", "shop-front/pom.xml", "shop-boot/pom.xml"):
        text = (root / rel).read_text()
        assert "maven-ear-plugin" not in text and "partner-portal" not in text and "shopWEB" not in text, rel
    assert "org.apache.commons:commons-text" in dep_keys(xml(root / "shop-back/pom.xml"))
    stash = root / "shop-boot/src/main/resources/asis-ear/EAR/src/main/application/META-INF"
    assert sorted(p.name for p in stash.iterdir()) == ["application.xml", "weblogic-application.xml"]
    assert not (root / "EAR").exists() and not (root / "WEB").exists()


def test_ear_context_root_with_an_executable_boot_jar(tmp_path: Path, ccfg: Config) -> None:
    """No JSP and `jsp_boot_packaging: jar`: the boot module is an executable jar — the context root follow-up names
    server.servlet.context-path only."""
    ccfg.layout.jsp_boot_packaging = "jar"
    plugin = EAR_PLUGIN_XML.format(conf=(
        "                <configuration>\n                    <modules>\n                        <webModule>\n"
        "                            <groupId>com.example.portal</groupId>\n"
        "                            <artifactId>shopWEB</artifactId>\n"
        "                            <contextRoot>/</contextRoot>\n                        </webModule>\n"
        "                    </modules>\n                </configuration>\n"))
    ws = make_ws(tmp_path, _small_ear_files(_ear_pom(plugin=plugin), web_jsp=False), "ear-jar")
    res = Restructurer().apply(ws, _small_profile(ws, jsp=False), ccfg, mode="layout")
    assert res.ok, res.error
    assert res.boot_packaging == "jar"
    assert ("EAR EAR deployed shopWEB at context root /: set server.servlet.context-path=/ for shop-boot (embedded "
            "Tomcat) so the URLs stay the same") in res.manual_followups
    assert "ear-module-dropped" in res.flags


def test_ear_property_group_id_is_internal(tmp_path: Path, ccfg: Config) -> None:
    """The company case: the EAR named its WAR module with a property groupId — still internal, never a back
    dependency (Maven would try to download `shopWEB:war` from central)."""
    ear = _ear_pom(app_group="${portal.group}")
    ws = make_ws(tmp_path, _small_ear_files(ear), "ear-propgroup")
    res = Restructurer().apply(ws, _small_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    back = (ws.root / "shop-back/pom.xml").read_text()
    assert "shopWEB" not in back and "<type>war</type>" not in back
    note = next(n for n in res.notes if n.startswith("EAR module EAR"))
    assert "${portal.group}:shopWEB" in note


# ---------------------------------------------------------------------------------------------- project name
def _kept_boot_files(boot_aid: str = "portal-app") -> dict[str, str]:
    return {
        "pom.xml": HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>portal-aggregator</artifactId>\n"
                   "    <version>1.0.0</version>\n    <packaging>pom</packaging>\n"
                   "    <properties>\n        <sbmig.project-name>portal</sbmig.project-name>\n"
                   "        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n    </properties>\n"
                   "    <modules>\n        <module>core</module>\n        <module>portal-boot</module>\n"
                   "        <module>EAR</module>\n    </modules>\n</project>\n",
        "core/pom.xml": HEAD + f"    <parent>\n        <groupId>{GROUP}</groupId>\n"
                        "        <artifactId>portal-aggregator</artifactId>\n        <version>1.0.0</version>\n"
                        "    </parent>\n    <artifactId>portal-core</artifactId>\n</project>\n",
        "core/src/main/java/com/example/portal/core/Repo.java": java("com.example.portal.core", "Repo"),
        "portal-boot/pom.xml": HEAD + f"    <parent>\n        <groupId>{GROUP}</groupId>\n"
                               "        <artifactId>portal-aggregator</artifactId>\n        <version>1.0.0</version>\n"
                               "    </parent>\n"
                               f"    <artifactId>{boot_aid}</artifactId>\n    <dependencies>\n"
                               + dep("${project.groupId}", "portal-core", "${project.version}")
                               + "    </dependencies>\n</project>\n",
        "portal-boot/src/main/java/com/example/portal/PortalApp.java": java(
            "com.example.portal", "PortalApp", "@SpringBootApplication"),
        "EAR/pom.xml": HEAD + f"    <groupId>{GROUP}</groupId>\n    <artifactId>portal</artifactId>\n"
                       "    <version>1.0.0</version>\n    <packaging>ear</packaging>\n    <dependencies>\n"
                       + dep(GROUP, boot_aid, "1.0.0") + "    </dependencies>\n</project>\n",
    }


def _kept_profile(ws):
    p = profile(ws, CaseType.SPRING_LEGACY, artifact_id="portal-aggregator", group_id=GROUP, version="1.0.0",
                packaging="pom", base_package="com.example.portal", main_classes=["com.example.portal.PortalApp"])
    p.is_multi_module = True
    return p


def test_kept_boot_module_follows_the_renamed_root(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = make_ws(tmp_path, _kept_boot_files(), "kept-rename")
    res = Restructurer().apply(ws, _kept_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert res.kept_boot_module == "portal-boot"
    assert t(xml(ws.root / "pom.xml"), "artifactId") == "portal"
    kept = xml(ws.root / "portal-boot/pom.xml")
    assert (t(kept, "parent/artifactId"), t(kept, "artifactId")) == ("portal", "portal-app")
    assert dep_keys(kept)[0] == "${project.groupId}:portal-back"          # the merged core → the back module
    assert t(xml(ws.root / "portal-back/pom.xml"), "parent/artifactId") == "portal"
    assert any("re-pointed to the renamed root pom (portal-aggregator → portal)" in n for n in res.notes)
    assert not any(n.startswith("kept boot module portal-boot does not inherit") for n in res.notes)
    assert res.merged_modules == ["core"] and "ear-module-dropped" in res.flags


def test_project_name_not_applied_when_the_kept_boot_module_has_it(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = make_ws(tmp_path, _kept_boot_files(boot_aid="portal"), "kept-collide")
    res = Restructurer().apply(ws, _kept_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert t(xml(ws.root / "pom.xml"), "artifactId") == "portal-aggregator"
    assert any(n.startswith("sbmig.project-name=portal not applied to the root pom: the kept boot module "
                            "portal-boot/pom.xml") for n in res.notes)
    assert t(xml(ws.root / "portal-back/pom.xml"), "artifactId") == "portal-aggregator-back"


# ---------------------------------------------------------------------------------------------- main classes / left web
def test_main_classes_are_reported_per_merged_jar_module(tmp_path: Path, ccfg: Config) -> None:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>suite</artifactId>\n    <version>1</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>batch</module>\n"
            "        <module>tools</module>\n        <module>web</module>\n    </modules>\n</project>\n")

    def mod(aid: str, packaging: str = "jar") -> str:
        return (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>suite</artifactId>\n"
                f"        <version>1</version>\n    </parent>\n    <artifactId>{aid}</artifactId>\n"
                f"    <packaging>{packaging}</packaging>\n</project>\n")

    main = "    public static void main(String[] args) {}"
    files = {"pom.xml": root, "batch/pom.xml": mod("suite-batch"), "tools/pom.xml": mod("suite-tools"),
             "web/pom.xml": mod("suite-web", "war"),
             "batch/src/main/java/com/acme/batch/Nightly.java": java("com.acme.batch", "Nightly", body=main),
             "batch/src/main/java/com/acme/batch/Weekly.java": java("com.acme.batch", "Weekly",
                                                                     body="    static public final void main(String... a) {}"),
             "batch/src/main/java/com/acme/batch/Helper.java": java("com.acme.batch", "Helper",
                                                                     body="    // public static void main(String[] a)"),
             "tools/src/main/java/com/acme/BootApp.java": java("com.acme", "BootApp", "@SpringBootApplication",
                                                                 body=main),
             "web/src/main/java/com/acme/web/Cli.java": java("com.acme.web", "Cli", body=main)}
    ws = make_ws(tmp_path, files, "mains")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="suite", version="1", packaging="pom",
                   base_package="com.acme", psvm_classes=["com.acme.batch.Nightly", "com.acme.batch.Weekly",
                                                          "com.acme.web.Cli"])
    prof.is_multi_module = True
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    mains = [f for f in res.manual_followups if f.startswith("module ")]
    assert mains == [("module batch had main classes com.acme.batch.Nightly, com.acme.batch.Weekly (a standalone "
                      "batch?): merged into suite-back — run it from the boot jar's classpath or keep it as its own "
                      "module")]
    # the WAR module's main class is not a merged jar module's: still the generic follow-up
    assert any(f.startswith("existing main class(es) com.acme.web.Cli kept in comp-jar") for f in res.manual_followups)


def test_web_content_at_the_project_root_is_reported(tmp_path: Path, ccfg: Config) -> None:
    files = _small_ear_files(_ear_pom())
    files["index.jsp"] = "<html/>\n"
    files["legacy/admin/main.jsp"] = "<html/>\n"
    files["legacy/admin/admin.css"] = "a{}\n"
    ws = make_ws(tmp_path, files, "left-root")
    prof = _small_profile(ws)
    prof.source.jsp_paths = [*prof.source.jsp_paths, "index.jsp", "legacy/admin/main.jsp"]
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert ("web content not referenced by any pom left in place: the project root (1 JSP) — serve it as before (web "
            "server) or move it into shop-front") in res.manual_followups
    assert ("web content not referenced by any pom left in place: legacy/ (1 JSP, 1 other file) — serve it as before "
            "(web server) or move it into shop-front") in res.manual_followups
    assert (ws.root / "index.jsp").is_file() and (ws.root / "legacy/admin/main.jsp").is_file()
    assert res.flags.count("web-content-left") == 1


def test_no_web_content_left_flag_without_stray_jsps(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _small_ear_files(_ear_pom()), "left-none")
    res = Restructurer().apply(ws, _small_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert "web-content-left" not in res.flags
    assert PomEditor(ws.root / "shop-boot/pom.xml").packaging() == "war"
