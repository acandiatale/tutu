"""Round 9c: the company layout — `{name}-boot` (Boot configuration only) + `{name}-back` (every source) +
`{name}-front` (web resources, only when present) directly under the root; JSP projects get a WAR boot module for
external Tomcat; Spring Boot as-is projects keep their directories (`boot_pom`); `artifacts.mandatory_target`;
MSA role parents. (h) proves the pinned pre-9c layout (config/artifacts.local-e2e.yml) produces the same trees."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from sbmig.core.config import ArtifactSpec, Config
from sbmig.core.models import CaseType
from sbmig.restructure import Restructurer

from .helpers import HEAD, all_, dep, dep_keys, java, make_ws, profile, t, xml
from .test_restructurer import (
    boot2_files,
    boot2_profile,
    kept_boot_files,
    msa_files,
    msa_profile,
    multi_files,
    multi_profile,
    servlet_files,
)

PLATFORM = "com.company.platform"
SNAPSHOT = Path(__file__).parent / "snapshots" / "pre9c_trees.json"


def _spec(aid: str, version: str = "4.0.0") -> ArtifactSpec:
    return ArtifactSpec(group_id=PLATFORM, artifact_id=aid, version=version)


def _roles(cfg: Config, version: str = "4.0.0") -> Config:
    cfg.artifacts.parent = _spec("base-parent", version)
    cfg.artifacts.boot_parent = _spec("boot-jar", version)
    cfg.artifacts.comp_parent = _spec("comp-jar", version)
    return cfg


def _tree(ws) -> dict[str, str]:
    return {p.relative_to(ws.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(ws.root.rglob("*")) if p.is_file() and ".git" not in p.parts}


# ---------------------------------------------------------------------------------------------- (h) pre-9c layout
def _servlet_web_files() -> dict[str, str]:
    files = servlet_files()
    files["src/main/webapp/css/site.css"] = "body{}\n"
    files["src/main/webapp/WEB-INF/jsp/home.jsp"] = '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n'
    files["src/main/resources/static/app.js"] = "x=1;\n"
    return files


def _servlet_web_profile(ws):
    return profile(ws, CaseType.SERVLET_TOMCAT, artifact_id="legacyweb", version="2.0", packaging="war",
                   base_package="com.acme.web", servlet_classes=["com.acme.web.HelloServlet"], has_web_xml=True,
                   web_xml_paths=["src/main/webapp/WEB-INF/web.xml"], jsp_count=2,
                   jsp_paths=["src/main/webapp/WEB-INF/jsp/home.jsp", "src/main/webapp/index.jsp"])


def _legacy_role_files():
    from .test_role_parents import _legacy_files
    return _legacy_files()


def _legacy_role_profile(ws):
    from .test_role_parents import _legacy_profile
    return _legacy_profile(ws)


def _role_modules(cfg: Config) -> Config:
    _roles(cfg)
    cfg.artifacts.mandatory_target = "modules"
    return cfg


PRE9C = {   # name → (files, profile builder, mode, params, cfg tweak)
    "boot2-single": (boot2_files, boot2_profile, "layout", {},
                     lambda c: setattr(c.artifacts, "remove_when_present", ["com.legacy:legacy-bom-thing"])),
    "servlet-web": (_servlet_web_files, _servlet_web_profile, "layout", {}, None),
    "multi-merge": (multi_files, multi_profile, "layout", {}, None),
    "multi-wrap": (multi_files, multi_profile, "layout", {"multi_module_policy": "wrap"}, None),
    "kept-boot": (kept_boot_files, multi_profile, "layout", {}, None),
    "msa": (msa_files, msa_profile, "msa_pom", {}, None),
    "boot2-roles": (boot2_files, boot2_profile, "layout", {}, _role_modules),
    "legacy-roles": (_legacy_role_files, _legacy_role_profile, "layout", {}, _role_modules),
}


def _run_pre9c(tmp_path: Path, cfg: Config, name: str) -> dict:
    files, prof, mode, params, tweak = PRE9C[name]
    if tweak:
        tweak(cfg)
    ws = make_ws(tmp_path, files(), name)
    res = Restructurer(commit=False).apply(ws, prof(ws), cfg, mode=mode, params=dict(params))
    assert res.ok, res.error
    run = str(ws.run_dir)
    return {"tree": _tree(ws),
            "notes": [n.replace(run, "<run>") for n in res.notes],
            "followups": [f.replace(run, "<run>") for f in res.manual_followups],
            "generated": res.generated_files, "parents_set": res.parents_set,
            "mandatory_added": res.mandatory_added}


@pytest.mark.parametrize("name", sorted(PRE9C))
def test_pre9c_layout_is_unchanged(tmp_path: Path, cfg: Config, name: str) -> None:
    """The pinned pre-9c configuration (module/boot-jar + module/comp-jar, front off, Boot split) yields the same
    trees, notes and follow-ups as before round 9c (snapshot taken on the round-9b code)."""
    got = _run_pre9c(tmp_path, cfg, name)
    if os.environ.get("SBMIG_WRITE_SNAPSHOT"):
        data = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        data[name] = got
        SNAPSHOT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    want = json.loads(SNAPSHOT.read_text())[name]
    assert got == want


# ---------------------------------------------------------------------------------------------- company layout
BASE, BOOTP, COMPP = f"{PLATFORM}:base-parent:4.0.0", f"{PLATFORM}:boot-jar:4.0.0", f"{PLATFORM}:comp-jar:4.0.0"
MANDATORY = ["org.springframework.boot:spring-boot-starter-validation",
             "org.springframework.boot:spring-boot-starter-actuator"]


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout (config/default.yml) on top of the e2e artifacts, with role parents."""
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"            # artifactIds follow the module names (`<root>-back`)
    _roles(cfg)
    assert cfg.artifacts.mandatory_target == "root"
    return cfg


def _parent(x) -> str:
    return f"{t(x, 'parent/groupId')}:{t(x, 'parent/artifactId')}:{t(x, 'parent/version')}"


def _modules(x) -> list[str]:
    return [m.text for m in all_(x, "modules/module")]


def _files(ws, d: str) -> list[str]:
    base = ws.root / d
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def _pom(ws, rel: str = "pom.xml"):
    return xml(ws.root / rel)


WEB_POM = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <packaging>war</packaging>\n"
           "    <properties>\n        <maven.compiler.source>1.8</maven.compiler.source>\n"
           "        <maven.compiler.target>1.8</maven.compiler.target>\n    </properties>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-webmvc", "5.3.30")
           + dep("javax.servlet", "javax.servlet-api", "4.0.1", "provided")
           + dep("commons-io", "commons-io", "2.6") + "    </dependencies>\n</project>\n")


def _legacy_web_files() -> dict[str, str]:
    return {
        "pom.xml": WEB_POM,
        "src/main/java/com/acme/shop/web/HomeController.java": java("com.acme.shop.web", "HomeController",
                                                                     "@Controller"),
        "src/main/java/com/acme/shop/service/ShopService.java": java("com.acme.shop.service", "ShopService",
                                                                      "@Service"),
        "src/main/resources/log4j2.xml": "<Configuration/>\n",
        "src/main/resources/messages.properties": "hello=Hello\n",
        "src/main/webapp/index.html": "<html/>\n",
        "src/main/webapp/css/site.css": "body{}\n",
        "src/main/webapp/img/logo.png": "PNG\n",
        "src/main/webapp/WEB-INF/web.xml": "<web-app/>\n",
    }


def _legacy_web_profile(ws, **kw):
    src = {"base_package": "com.acme.shop", "controllers": ["com.acme.shop.web.HomeController"], "has_web_xml": True,
           "web_xml_paths": ["src/main/webapp/WEB-INF/web.xml"], "web_static_dirs": ["src/main/webapp"],
           "web_static_count": 3}
    src.update(kw)
    return profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", packaging="war", **src)


# ---------------------------------------------------------------------------------------------- (a) static front
def test_spring_legacy_static_front(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _legacy_web_files(), "legacy-web")
    res = Restructurer().apply(ws, _legacy_web_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    assert not (root / "src").exists()
    r = _pom(ws)
    assert _parent(r) == BASE and t(r, "packaging") == "pom"
    assert _modules(r) == ["shop-back", "shop-front", "shop-boot"]
    assert dep_keys(r) == MANDATORY                                         # mandatory_target: root
    # boot: only the Boot configuration
    assert _files(ws, "shop-boot") == ["pom.xml", "src/main/java/com/acme/shop/Application.java",
                                       "src/main/resources/asis-webinf/web.xml", "src/main/resources/log4j2.xml"]
    b = _pom(ws, "shop-boot/pom.xml")
    assert _parent(b) == BOOTP and t(b, "parent/relativePath") == "" and t(b, "packaging") == "jar"
    assert (t(b, "groupId"), t(b, "artifactId"), t(b, "version")) == ("com.acme", "shop-boot", "1.0.0")
    assert dep_keys(b)[:2] == ["${project.groupId}:shop-back", "${project.groupId}:shop-front"]
    assert not set(MANDATORY) & set(dep_keys(b))
    app = (root / "shop-boot/src/main/java/com/acme/shop/Application.java").read_text()
    assert "SpringBootServletInitializer" not in app
    # back: every source incl. controllers, services and their resources
    assert _files(ws, "shop-back") == ["pom.xml", "src/main/java/com/acme/shop/service/ShopService.java",
                                       "src/main/java/com/acme/shop/web/HomeController.java",
                                       "src/main/resources/messages.properties"]
    k = _pom(ws, "shop-back/pom.xml")
    assert _parent(k) == COMPP and t(k, "artifactId") == "shop-back"
    assert dep_keys(k) == ["org.springframework:spring-webmvc", "javax.servlet:javax.servlet-api",
                           "commons-io:commons-io"]
    # front: static web files only, same relative path under src/main/resources/static
    assert _files(ws, "shop-front") == ["pom.xml", "src/main/resources/static/css/site.css",
                                        "src/main/resources/static/img/logo.png",
                                        "src/main/resources/static/index.html"]
    f = _pom(ws, "shop-front/pom.xml")
    assert _parent(f) == COMPP and t(f, "packaging") == "jar" and dep_keys(f) == []
    assert (t(f, "groupId"), t(f, "artifactId"), t(f, "version")) == ("com.acme", "shop-front", "1.0.0")
    assert (res.boot_module, res.comp_module, res.front_module) == ("shop-boot", "shop-back", "shop-front")
    assert res.boot_packaging == "jar" and res.front_files == 3
    assert sorted(res.parents_set) == sorted([
        f"pom.xml: - → {BASE}", f"shop-back/pom.xml: - → {COMPP}", f"shop-front/pom.xml: - → {COMPP}",
        f"shop-boot/pom.xml: - → {BOOTP}"])
    assert any("web.xml at shop-boot/src/main/resources/asis-webinf/web.xml" in fu for fu in res.manual_followups)
    assert any(fu.startswith("WAR packaging converted to executable jar (shop-boot)") for fu in res.manual_followups)
    # (c) idempotent: the second run finds the company layout
    before = _tree(ws)
    res2 = Restructurer().apply(ws, _legacy_web_profile(ws), ccfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes and _tree(ws) == before
    assert (res2.boot_module, res2.comp_module, res2.front_module) == ("shop-boot", "shop-back", "shop-front")


# ---------------------------------------------------------------------------------------------- (b) JSP → WAR
DISPATCHER = ('<beans xmlns="http://www.springframework.org/schema/beans">\n'
              '    <bean class="org.springframework.web.servlet.view.InternalResourceViewResolver">\n'
              '        <property name="prefix" value="/WEB-INF/jsp/"/>\n'
              '        <property name="suffix" value=".jsp"/>\n    </bean>\n</beans>\n')


def _jsp_files() -> dict[str, str]:
    files = _legacy_web_files()
    del files["src/main/webapp/index.html"], files["src/main/webapp/img/logo.png"]
    files["src/main/webapp/WEB-INF/dispatcher-servlet.xml"] = DISPATCHER
    files["src/main/webapp/WEB-INF/jsp/home.jsp"] = (
        '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n<c:out value="hi"/>\n')
    files["src/main/webapp/WEB-INF/jsp/admin/list.jsp"] = "<p>list</p>\n"
    files["src/main/java/com/acme/shop/web/HelloServlet.java"] = java("com.acme.shop.web", "HelloServlet",
                                                                      '@WebServlet("/hello")')
    return files


def _jsp_profile(ws):
    return _legacy_web_profile(ws, jsp_count=2, servlet_classes=["com.acme.shop.web.HelloServlet"],
                               jsp_paths=["src/main/webapp/WEB-INF/jsp/admin/list.jsp",
                                          "src/main/webapp/WEB-INF/jsp/home.jsp"],
                               xml_bean_configs=["src/main/webapp/WEB-INF/dispatcher-servlet.xml"])


def test_jsp_project_boot_war(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _jsp_files(), "jsp")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    root = ws.root
    b = _pom(ws, "shop-boot/pom.xml")
    assert t(b, "packaging") == "war" and res.boot_packaging == "war"
    deps = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}": t(d, "scope") for d in all_(b, "dependencies/dependency")}
    assert deps["org.springframework.boot:spring-boot-starter-tomcat"] == "provided"
    assert deps["org.apache.tomcat.embed:tomcat-embed-jasper"] == "provided"
    assert deps["jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api"] is None      # JSTL: managed by Boot 4.1
    assert deps["org.glassfish.web:jakarta.servlet.jsp.jstl"] is None
    assert deps["org.springframework.boot:spring-boot-web-server"] is None    # @ServletComponentScan in WEB-INF/lib
    assert list(deps)[:2] == ["${project.groupId}:shop-back", "${project.groupId}:shop-front"]
    plugins = [t(p, "artifactId") for p in all_(b, "build/plugins/plugin")]
    assert plugins == ["spring-boot-maven-plugin"]                          # repackage stays, no war-plugin config
    app = (root / "shop-boot/src/main/java/com/acme/shop/Application.java").read_text()
    assert "public class Application extends SpringBootServletInitializer {" in app
    assert "import org.springframework.boot.web.servlet.support.SpringBootServletInitializer;" in app
    assert "protected SpringApplicationBuilder configure(SpringApplicationBuilder builder)" in app
    assert "@ServletComponentScan" in app and '"classpath:legacy-config/dispatcher-servlet.xml"' in app
    jsp = "shop-front/src/main/resources/META-INF/resources/WEB-INF/jsp"
    assert (root / jsp / "home.jsp").is_file() and (root / jsp / "admin/list.jsp").is_file()
    assert (root / "shop-front/src/main/resources/static/css/site.css").is_file()
    assert t(_pom(ws, "shop-front/pom.xml"), "packaging") == "jar"
    assert res.front_files == 3
    assert not (root / "shop-back/src/main/webapp").exists()
    # the bean XML is kept once: legacy-config/ (imported); the asis-webinf/ stash copy is dropped
    assert (root / "shop-boot/src/main/resources/legacy-config/dispatcher-servlet.xml").is_file()
    assert not (root / "shop-boot/src/main/resources/asis-webinf/dispatcher-servlet.xml").exists()
    assert any("kept once in shop-boot/src/main/resources/legacy-config/" in n for n in res.notes)
    props = (root / "shop-boot/src/main/resources/application.properties").read_text()
    assert "spring.mvc.view.prefix=/WEB-INF/jsp/\nspring.mvc.view.suffix=.jsp\n" in props
    assert "boot module packaged as an executable WAR (java -jar runs the embedded Tomcat; an external Tomcat works too); " \
           "JSPs are served from the front jar " \
           "(META-INF/resources) — shop-boot, shop-front" in res.manual_followups
    assert not any("executable jar" in fu for fu in res.manual_followups)
    # the comp pom has no tomcat; the root keeps only the mandatory dependencies
    assert "org.springframework.boot:spring-boot-starter-tomcat" not in dep_keys(_pom(ws, "shop-back/pom.xml"))
    assert dep_keys(_pom(ws)) == MANDATORY


def test_jsp_view_prefix_mapped_and_missing_resolver(tmp_path: Path, ccfg: Config) -> None:
    files = _jsp_files()
    del files["src/main/webapp/WEB-INF/dispatcher-servlet.xml"]
    files["src/main/webapp/WEB-INF/views/old.jsp"] = "<p/>\n"
    files["src/main/java/com/acme/shop/web/MvcConfig.java"] = java(
        "com.acme.shop.web", "MvcConfig", "@Configuration",
        body='    ViewResolver v() { InternalResourceViewResolver r = new InternalResourceViewResolver();\n'
             '        r.setPrefix("/WEB-INF/views/"); r.setSuffix(".jsp"); return r; }')
    ws = make_ws(tmp_path, files, "jsp-views")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "shop-front/src/main/resources/META-INF/resources/WEB-INF/jsp/views/old.jsp").is_file()
    props = (ws.root / "shop-boot/src/main/resources/application.properties").read_text()
    assert "spring.mvc.view.prefix=/WEB-INF/jsp/views/\nspring.mvc.view.suffix=.jsp\n" in props
    assert any(fu.startswith("view prefix /WEB-INF/views/ (shop-back/src/main/java/com/acme/shop/web/MvcConfig.java)"
                             " maps to /WEB-INF/jsp/views/") for fu in res.manual_followups)
    assert any("1 JSP(s) outside /WEB-INF/jsp/" in fu for fu in res.manual_followups)
    # no view resolver at all → follow-up, nothing written
    files = _jsp_files()
    del files["src/main/webapp/WEB-INF/dispatcher-servlet.xml"]
    ws2 = make_ws(tmp_path, files, "jsp-none")
    res2 = Restructurer().apply(ws2, _jsp_profile(ws2), ccfg, mode="layout")
    assert res2.ok, res2.error
    assert not (ws2.root / "shop-boot/src/main/resources/application.properties").exists()
    assert any(fu.startswith("no InternalResourceViewResolver prefix / suffix found") for fu in res2.manual_followups)


def test_jsp_with_jar_packaging_keeps_executable_jar(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.jsp_boot_packaging = "jar"
    ws = make_ws(tmp_path, _jsp_files(), "jsp-jar")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    b = _pom(ws, "shop-boot/pom.xml")
    assert t(b, "packaging") == "jar" and "org.apache.tomcat.embed:tomcat-embed-jasper" in dep_keys(b)
    assert "SpringBootServletInitializer" not in (
        ws.root / "shop-boot/src/main/java/com/acme/shop/Application.java").read_text()
    assert any("does not serve JSPs from an executable jar" in fu for fu in res.manual_followups)


# ---------------------------------------------------------------------------------------------- (c) no web → no front
def test_pure_java_has_no_front(tmp_path: Path, ccfg: Config) -> None:
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>batch</artifactId>\n    <version>2.0</version>\n"
           "    <dependencies>\n" + dep("commons-io", "commons-io", "2.6") + "    </dependencies>\n</project>\n")
    files = {"pom.xml": pom, "src/main/java/com/acme/batch/Job.java": java("com.acme.batch", "Job"),
             "src/main/resources/job.properties": "a=1\n"}
    ws = make_ws(tmp_path, files, "batch")
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="batch", version="2.0",
                                           base_package="com.acme.batch"), ccfg, mode="layout")
    assert res.ok, res.error
    assert _modules(_pom(ws)) == ["batch-back", "batch-boot"] and not (ws.root / "batch-front").exists()
    assert (res.boot_module, res.comp_module, res.front_module, res.front_files) == ("batch-boot", "batch-back",
                                                                                     None, 0)
    assert dep_keys(_pom(ws, "batch-boot/pom.xml"))[0] == "${project.groupId}:batch-back"
    assert (ws.root / "batch-back/src/main/resources/job.properties").is_file()


def test_project_name_override(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.project_name = "mall"
    ws = make_ws(tmp_path, _legacy_web_files(), "named")
    res = Restructurer().apply(ws, _legacy_web_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert _modules(_pom(ws)) == ["mall-back", "mall-front", "mall-boot"]
    assert t(_pom(ws, "mall-boot/pom.xml"), "artifactId") == "shop-boot"      # artifactIds keep the root artifactId


# ---------------------------------------------------------------------------------------------- (d) boot_pom modules
def _boot_multi_files() -> dict[str, str]:
    root = (HEAD + "    <parent>\n        <groupId>org.springframework.boot</groupId>\n"
            "        <artifactId>spring-boot-starter-parent</artifactId>\n        <version>2.7.18</version>\n"
            "        <relativePath/>\n    </parent>\n"
            "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>3.0.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>shop-boot</module>\n"
            "        <module>shop-core</module>\n        <module>shop-api</module>\n    </modules>\n"
            "    <properties>\n        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n"
            "        <guava.version>31.0-jre</guava.version>\n        <unused.flag>x</unused.flag>\n"
            "    </properties>\n"
            "    <dependencyManagement>\n        <dependencies>\n"
            + dep("com.google.guava", "guava", "${guava.version}").replace("        <", "            <")
            + "        </dependencies>\n    </dependencyManagement>\n"
            "    <dependencies>\n" + dep("org.projectlombok", "lombok", scope="provided") + "    </dependencies>\n"
            "    <build>\n        <plugins>\n            <plugin>\n"
            "                <artifactId>maven-surefire-plugin</artifactId>\n"
            "                <version>3.2.5</version>\n            </plugin>\n        </plugins>\n    </build>\n"
            "</project>\n")

    def child(aid: str, deps: str, boot: bool = False) -> str:
        plugin = ("    <build>\n        <plugins>\n            <plugin>\n"
                  "                <groupId>org.springframework.boot</groupId>\n"
                  "                <artifactId>spring-boot-maven-plugin</artifactId>\n"
                  "            </plugin>\n        </plugins>\n    </build>\n") if boot else ""
        return (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>shop</artifactId>\n"
                "        <version>3.0.0</version>\n    </parent>\n"
                f"    <artifactId>{aid}</artifactId>\n    <dependencies>\n{deps}    </dependencies>\n{plugin}"
                "</project>\n")

    return {
        "pom.xml": root,
        "shop-boot/pom.xml": child("shop-boot", dep("com.acme", "shop-core", "${project.version}")
                                   + dep("com.acme", "shop-api", "${project.version}")
                                   + dep("org.springframework.boot", "spring-boot-starter-web"), boot=True),
        "shop-boot/src/main/java/com/acme/shop/ShopApplication.java": java(
            "com.acme.shop", "ShopApplication", "@SpringBootApplication"),
        "shop-boot/src/main/resources/application.yml": "server:\n  port: 8080\n",
        "shop-core/pom.xml": child("shop-core", dep("com.google.guava", "guava")),
        "shop-core/src/main/java/com/acme/shop/core/Repo.java": java("com.acme.shop.core", "Repo"),
        "shop-api/pom.xml": child("shop-api", dep("org.springframework.boot", "spring-boot-starter-web")),
        "shop-api/src/main/java/com/acme/shop/api/Api.java": java("com.acme.shop.api", "Api", "@RestController"),
    }


def _boot_multi_profile(ws, boot_modules=("shop-boot",)):
    p = profile(ws, CaseType.SPRING_BOOT, artifact_id="shop", version="3.0.0", packaging="pom",
                base_package="com.acme.shop", main_classes=["com.acme.shop.ShopApplication"])
    p.is_multi_module = True
    p.boot_modules = list(boot_modules)
    return p


@pytest.mark.parametrize("boot_modules", [("shop-boot",), ()], ids=["analyzer", "fallback"])
def test_boot_pom_modules_in_place(tmp_path: Path, ccfg: Config, boot_modules) -> None:
    ws = make_ws(tmp_path, _boot_multi_files(), "boot-multi")
    before_paths = sorted(p for p in _tree(ws))
    res = Restructurer().apply(ws, _boot_multi_profile(ws, boot_modules), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert sorted(_tree(ws)) == before_paths                                 # directories / files kept
    r = _pom(ws)
    assert _parent(r) == BASE and t(r, "parent/relativePath") == "" and t(r, "packaging") == "pom"
    assert _modules(r) == ["shop-boot", "shop-core", "shop-api"]
    assert dep_keys(r) == MANDATORY                                         # mandatory_target: root
    assert all_(r, "dependencyManagement") == [] and all_(r, "build") == [] and all_(r, "properties") == []
    for mod, par in (("shop-boot", BOOTP), ("shop-core", COMPP), ("shop-api", COMPP)):
        x = _pom(ws, f"{mod}/pom.xml")
        assert _parent(x) == par and t(x, "parent/relativePath") == ""
        assert (t(x, "groupId"), t(x, "artifactId"), t(x, "version")) == ("com.acme", mod, "3.0.0")
        assert not set(MANDATORY) & set(dep_keys(x))
        assert "org.projectlombok:lombok" in dep_keys(x)                    # inherited root dependency declared
        assert [t(p, "artifactId") for p in all_(x, "build/plugins/plugin")][-1] == "maven-surefire-plugin"
        assert t(x, "properties/project.build.sourceEncoding") == "UTF-8"
        assert dep_keys(x, "dependencyManagement/dependencies/dependency") == ["com.google.guava:guava"]
        assert t(x, "properties/maven.compiler.release") == "21"
    core = _pom(ws, "shop-core/pom.xml")
    assert t(core, "properties/guava.version") == "33.4.8-jre"              # referenced root property, bumped
    assert t(core, "properties/unused.flag") is None
    b = _pom(ws, "shop-boot/pom.xml")
    assert dep_keys(b)[:2] == ["com.acme:shop-core", "com.acme:shop-api"]   # sibling dependencies untouched
    assert "spring-boot-maven-plugin" in [t(p, "artifactId") for p in all_(b, "build/plugins/plugin")]
    assert "spring-boot-maven-plugin" not in [t(p, "artifactId") for p in all_(core, "build/plugins/plugin")]
    assert res.boot_module == "shop-boot" and res.boot_packaging == "jar"
    assert sorted(res.parents_set) == sorted([
        f"pom.xml: org.springframework.boot:spring-boot-starter-parent:2.7.18 → {BASE}",
        f"shop-boot/pom.xml: com.acme:shop:3.0.0 → {BOOTP}", f"shop-core/pom.xml: com.acme:shop:3.0.0 → {COMPP}",
        f"shop-api/pom.xml: com.acme:shop:3.0.0 → {COMPP}"])
    # idempotent
    before = _tree(ws)
    res2 = Restructurer().apply(ws, _boot_multi_profile(ws, boot_modules), ccfg, mode="boot_pom")
    assert res2.ok, res2.error
    assert _tree(ws) == before and res2.changed_files == [] and res2.parents_set == []
    assert any(n.startswith("boot_pom: already in place") for n in res2.notes)


def test_boot_pom_placeholders_in_the_boot_module(tmp_path: Path, ccfg: Config) -> None:
    from .test_placeholders import inventory
    ws = make_ws(tmp_path, _boot_multi_files(), "boot-ph")
    prof = _boot_multi_profile(ws)
    prof.properties = inventory()
    res = Restructurer().apply(ws, prof, ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert res.placeholder_files == ["shop-boot/src/main/resources/application.properties"]


def test_boot_pom_without_role_parents(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = make_ws(tmp_path, _boot_multi_files(), "boot-norole")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    r = _pom(ws)
    assert _parent(r) == BASE and dep_keys(r) == ["org.projectlombok:lombok", *MANDATORY]
    assert t(r, "properties/guava.version") == "33.4.8-jre"                  # still inherited by the modules
    for mod in ("shop-boot", "shop-core", "shop-api"):
        assert t(_pom(ws, f"{mod}/pom.xml"), "parent/artifactId") == "shop"   # keep inheriting the root
    assert res.parents_set == [f"pom.xml: org.springframework.boot:spring-boot-starter-parent:2.7.18 → {BASE}"]


# ---------------------------------------------------------------------------------------------- (e) boot_pom single
def test_boot_pom_single_module_is_wrapped(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "boot-single")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    root = ws.root
    assert not (root / "src").exists()
    assert (root / "shop-boot/src/main/java/com/acme/shop/ShopApplication.java").is_file()
    assert (root / "shop-boot/src/main/java/com/acme/shop/web/HelloController.java").is_file()
    assert (root / "shop-boot/src/test/java/com/acme/shop/ShopApplicationTests.java").is_file()
    assert (root / "lombok.config").is_file() and (root / "README.md").is_file()
    r = _pom(ws)
    assert _parent(r) == BASE and t(r, "packaging") == "pom" and _modules(r) == ["shop-boot"]
    assert dep_keys(r) == MANDATORY and all_(r, "build") == [] and all_(r, "properties") == []
    assert t(r, "repositories/repository/id") == "corp"
    b = _pom(ws, "shop-boot/pom.xml")
    assert _parent(b) == BOOTP and t(b, "parent/relativePath") == ""
    assert (t(b, "groupId"), t(b, "artifactId"), t(b, "version"), t(b, "packaging")) == (
        "com.acme", "shop-boot", "1.0.0-SNAPSHOT", None)
    assert dep_keys(b)[:3] == ["org.springframework.boot:spring-boot-starter-web", "com.google.guava:guava",
                               "org.apache.commons:commons-lang3"]
    assert "org.springframework.boot:spring-boot-starter-validation" not in dep_keys(b)
    actuator = [d for d in all_(b, "dependencies/dependency") if t(d, "artifactId") == "spring-boot-starter-actuator"]
    assert len(actuator) == 1 and t(actuator[0], "version") is None        # as-is declaration kept, version aligned
    assert t(b, "build/finalName") == "shop-app"
    assert [t(p, "artifactId") for p in all_(b, "build/plugins/plugin")] == ["spring-boot-maven-plugin",
                                                                              "maven-compiler-plugin"]
    assert t(b, "repositories/repository/id") == "corp" and t(b, "properties/guava.version") == "33.4.8-jre"
    assert (res.boot_module, res.boot_packaging) == ("shop-boot", "jar")
    before = _tree(ws)
    res2 = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res2.ok, res2.error
    assert _tree(ws) == before and res2.changed_files == []


def test_boot_pom_single_module_without_role_parents(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.boot_parent = ccfg.artifacts.comp_parent = None
    ws = make_ws(tmp_path, boot2_files(), "boot-single-norole")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    b = _pom(ws, "shop-boot/pom.xml")
    assert t(b, "parent/artifactId") == "shop" and t(b, "parent/relativePath") == "../pom.xml"
    r = _pom(ws)
    assert dep_keys(r) == MANDATORY and t(r, "properties/guava.version") == "33.4.8-jre"
    before = _tree(ws)
    assert Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom").changed_files == []
    assert _tree(ws) == before


# ---------------------------------------------------------------------------------------------- (f) MSA
def _msa_company_files() -> dict[str, str]:
    files = msa_files()
    files["pom.xml"] = files["pom.xml"].replace(
        "        <module>user-service</module>\n",
        "        <module>user-service</module>\n        <module>billing-service</module>\n"
        "        <module>shared-lib</module>\n").replace(
        "    <modules>", "    <properties>\n        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n"
        "    </properties>\n    <modules>")
    files["billing-service/pom.xml"] = (
        HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>platform</artifactId>\n"
        "        <version>1.0</version>\n    </parent>\n    <artifactId>billing-service</artifactId>\n"
        "    <dependencies>\n" + dep("org.springframework.boot", "spring-boot-starter-web") + "    </dependencies>\n"
        "</project>\n")
    files["shared-lib/pom.xml"] = (
        HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shared-lib</artifactId>\n    <version>1.0</version>\n"
        "</project>\n")
    return files


def _msa_company_profile(ws):
    from sbmig.core.models import ServiceInfo
    p = msa_profile(ws)
    p.services += [ServiceInfo(name="billing-service", root_pom="billing-service/pom.xml",
                               case_type=CaseType.SPRING_BOOT),
                   ServiceInfo(name="shared-lib", root_pom="shared-lib/pom.xml", case_type=CaseType.PURE_JAVA,
                               kind="library")]
    return p


def test_msa_company_parents_and_mandatory_in_roots(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _msa_company_files(), "msa-company")
    res = Restructurer().apply(ws, _msa_company_profile(ws), ccfg, mode="msa_pom")
    assert res.ok, res.error
    top = _pom(ws)
    assert _parent(top) == BASE and t(top, "parent/relativePath") == "" and dep_keys(top) == MANDATORY
    for s in ("order-service", "user-service", "billing-service"):
        x = _pom(ws, f"{s}/pom.xml")
        assert _parent(x) == BOOTP and t(x, "parent/relativePath") == ""
        assert dep_keys(x)[-2:] == MANDATORY                                   # each service root too
    bill = _pom(ws, "billing-service/pom.xml")
    assert (t(bill, "groupId"), t(bill, "version")) == ("com.acme", "1.0")    # stopped inheriting the aggregator
    assert t(bill, "properties/project.build.sourceEncoding") == "UTF-8"
    lib = _pom(ws, "shared-lib/pom.xml")
    assert _parent(lib) == COMPP and not set(MANDATORY) & set(dep_keys(lib))
    assert f"pom.xml: - → {BASE}" in res.parents_set
    assert f"billing-service/pom.xml: com.acme:platform:1.0 → {BOOTP}" in res.parents_set
    before = _tree(ws)
    res2 = Restructurer().apply(ws, _msa_company_profile(ws), ccfg, mode="msa_pom")
    assert res2.ok and _tree(ws) == before and res2.parents_set == []


def test_msa_mandatory_target_modules(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.mandatory_target = "modules"
    ws = make_ws(tmp_path, _msa_company_files(), "msa-modules")
    res = Restructurer().apply(ws, _msa_company_profile(ws), ccfg, mode="msa_pom")
    assert res.ok, res.error
    assert _parent(_pom(ws)) == BASE and dep_keys(_pom(ws)) == []
    assert dep_keys(_pom(ws, "order-service/pom.xml"))[-2:] == MANDATORY


# ---------------------------------------------------------------------------------------------- (g) target modules
def test_mandatory_target_modules_keeps_9b_placement(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.mandatory_target = "modules"
    ws = make_ws(tmp_path, _legacy_web_files(), "modules-target")
    res = Restructurer().apply(ws, _legacy_web_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert dep_keys(_pom(ws)) == []
    for mod in ("shop-boot", "shop-back", "shop-front"):
        assert dep_keys(_pom(ws, f"{mod}/pom.xml"))[-2:] == MANDATORY, mod
    assert any(n.startswith("role parents: 4 parent(s) set") and "are in each module pom" in n for n in res.notes)


@pytest.mark.parametrize("config_file,text,prefix", [
    ("src/main/webapp/WEB-INF/dispatcher-servlet.xml",
     '<beans><mvc:view-resolvers><mvc:jsp prefix="/WEB-INF/jsp/" suffix=".jsp"/></mvc:view-resolvers></beans>\n',
     "/WEB-INF/jsp/"),
    ("src/main/webapp/WEB-INF/spring/servlet.xml",
     ('<beans><bean id="v" class="org.springframework.web.servlet.view.InternalResourceViewResolver"\n'
      '      p:prefix="/WEB-INF/" p:suffix=".jsp"/></beans>\n'), "/WEB-INF/jsp/"),
    ("src/main/java/com/acme/shop/web/Mvc.java",
     "class Mvc { void v(ViewResolverRegistry registry) { registry.jsp(\"/WEB-INF/jsp/\", \".jsp\"); } }\n",
     "/WEB-INF/jsp/"),
], ids=["mvc-jsp", "p-namespace", "registry"])
def test_view_resolver_forms(tmp_path: Path, ccfg: Config, config_file: str, text: str, prefix: str) -> None:
    files = _jsp_files()
    del files["src/main/webapp/WEB-INF/dispatcher-servlet.xml"]
    files[config_file] = text
    files["src/main/webapp/WEB-INF/top.jsp"] = "<p/>\n"                    # directly under WEB-INF → jsp dir root
    ws = make_ws(tmp_path, files, "views")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="layout")
    assert res.ok, res.error
    assert (ws.root / "shop-front/src/main/resources/META-INF/resources/WEB-INF/jsp/top.jsp").is_file()
    props = (ws.root / "shop-boot/src/main/resources/application.properties").read_text()
    assert f"spring.mvc.view.prefix={prefix}\nspring.mvc.view.suffix=.jsp\n" in props


# ---------------------------------------------------------------------------------------- (h) mandatory type / scope
def _dep_el(x, aid: str, managed: bool = False):
    path = "dependencyManagement/dependencies/dependency" if managed else "dependencies/dependency"
    return next(d for d in all_(x, path) if t(d, "artifactId") == aid)


def test_mandatory_type_pom_and_import_scope(tmp_path: Path, ccfg: Config) -> None:
    """User (2026-10-21): some in-house mandatory artifacts are pom-only. `type: pom` is written into the declaration
    and aligned on an as-is declaration that lacks it (else Maven looks for a jar that does not exist);
    `scope: import` + `type: pom` is a BOM → <dependencyManagement>, never <dependencies>."""
    ccfg.artifacts.mandatory = [
        ArtifactSpec(group_id=PLATFORM, artifact_id="company-deps", version=None, type="pom"),
        ArtifactSpec(group_id=PLATFORM, artifact_id="company-bom", version="4.0.0", type="pom", scope="import"),
    ]
    files = _msa_company_files()
    files["billing-service/pom.xml"] = files["billing-service/pom.xml"].replace(
        "    <dependencies>\n", "    <dependencies>\n" + dep(PLATFORM, "company-deps", "1.0"), 1)   # as-is: jar, pinned
    ws = make_ws(tmp_path, files, "msa-mandatory-type")
    res = Restructurer().apply(ws, _msa_company_profile(ws), ccfg, mode="msa_pom")
    assert res.ok, res.error
    top = _pom(ws)
    deps = _dep_el(top, "company-deps")
    assert t(deps, "type") == "pom" and t(deps, "version") is None and t(deps, "scope") is None
    bom = _dep_el(top, "company-bom", managed=True)
    assert (t(bom, "type"), t(bom, "scope"), t(bom, "version")) == ("pom", "import", "4.0.0")
    assert all(t(d, "artifactId") != "company-bom" for d in all_(top, "dependencies/dependency"))   # never a plain dep
    bill = _pom(ws, "billing-service/pom.xml")
    aligned = _dep_el(bill, "company-deps")
    assert t(aligned, "type") == "pom" and t(aligned, "version") is None                  # as-is jar 1.0 → pom, managed
    assert any("company-deps" in n and "type=pom" in n for n in res.notes)
    assert "com.company.platform:company-bom" in res.mandatory_added
    before = _tree(ws)
    res2 = Restructurer().apply(ws, _msa_company_profile(ws), ccfg, mode="msa_pom")
    assert res2.ok and _tree(ws) == before
