"""v2.1z-bf: a single-module Spring Boot app (`layout.boot_as_is: in-place`, root = the app itself) is split into the
company's modules like a non-Boot project (`layout.boot_single_module: split`, the default); `wrap` keeps the earlier
`{name}-boot` wrapping (pinned in test_company_layout.py). The split reuses the layout machinery (`_single`), so the
result is a `layout` result."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import CaseType
from sbmig.restructure import Restructurer

from .helpers import all_, dep_keys, java, make_ws, profile, t, xml
from .test_company_layout import BASE, BOOTP, COMPP, MANDATORY, _boot_multi_files, _boot_multi_profile, ccfg  # noqa: F401
from .test_restructurer import boot2_files, boot2_profile, msa_files, msa_profile


def _tree(ws) -> dict[str, str]:
    return {p.relative_to(ws.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(ws.root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def _files(ws, d: str) -> list[str]:
    base = ws.root / d
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def _pom(ws, rel: str = "pom.xml"):
    return xml(ws.root / rel)


def _parent(x) -> str:
    return f"{t(x, 'parent/groupId')}:{t(x, 'parent/artifactId')}:{t(x, 'parent/version')}"


def _boot_files() -> dict[str, str]:
    files = boot2_files()
    files["src/main/resources/templates/home.html"] = "<html th:text=\"${msg}\"/>\n"
    files["src/main/resources/mapper/ShopMapper.xml"] = "<mapper/>\n"
    files["src/main/resources/messages.properties"] = "hi=Hi\n"
    return files


# ---------------------------------------------------------------------------------------------- the split
@pytest.mark.parametrize("route", ["boot_pom", "layout"])
def test_single_boot_app_is_split(tmp_path: Path, ccfg: Config, route: str) -> None:
    """Both routes give the same split: `boot_pom` (a caller that still plans the in-place mode) switches to the
    layout split itself; the planner plans `layout` with `boot_single: True`."""
    assert ccfg.layout.boot_single_module == "split"
    ws = make_ws(tmp_path, _boot_files(), f"split-{route}")
    params = {"boot_single": True} if route == "layout" else {}
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode=route, params=params)
    assert res.ok, res.error
    root = ws.root
    assert not (root / "src").exists()
    r = _pom(ws)
    assert _parent(r) == BASE and t(r, "packaging") == "pom"
    assert [m.text for m in all_(r, "modules/module")] == ["shop-back", "shop-front", "shop-boot"]
    assert dep_keys(r) == MANDATORY
    # boot: the application class and the Boot configuration only
    assert _files(ws, "shop-boot") == [
        "pom.xml", "src/main/java/com/acme/shop/ShopApplication.java",
        "src/main/resources/application.yml", "src/main/resources/config/application-prod.properties",
        "src/main/resources/logback-spring.xml", "src/test/java/com/acme/shop/ShopApplicationTests.java",
        "src/test/resources/application-test.yml"]
    b = _pom(ws, "shop-boot/pom.xml")
    assert _parent(b) == BOOTP and t(b, "parent/relativePath") == "" and t(b, "packaging") == "jar"
    assert (t(b, "groupId"), t(b, "artifactId"), t(b, "version")) == ("com.acme", "shop-boot", "1.0.0-SNAPSHOT")
    assert dep_keys(b)[:2] == ["${project.groupId}:shop-back", "${project.groupId}:shop-front"]
    assert [t(p, "artifactId") for p in all_(b, "build/plugins/plugin")][0] == "spring-boot-maven-plugin"
    assert t(b, "build/finalName") == "shop-app"
    # back: every other source and the classpath resources code reads
    back = _files(ws, "shop-back")
    for f in ("src/main/java/com/acme/shop/web/HelloController.java", "src/main/java/com/acme/shop/service/Greeter.java",
              "src/main/java/com/acme/shop/config/WebConfig.java", "src/main/resources/mapper/ShopMapper.xml",
              "src/main/resources/messages.properties", "src/test/java/com/acme/shop/service/GreeterTest.java"):
        assert f in back, f
    k = _pom(ws, "shop-back/pom.xml")
    assert _parent(k) == COMPP and t(k, "artifactId") == "shop-back"
    assert "org.springframework.boot:spring-boot-starter-web" in dep_keys(k)
    assert "com.google.guava:guava" in dep_keys(k)
    assert "spring-boot-maven-plugin" not in [t(p, "artifactId") for p in all_(k, "build/plugins/plugin")]
    # nothing declared twice: the boot module adds no dependency the source module already has
    compile_b = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}" for d in all_(b, "dependencies/dependency")
                 if t(d, "scope") not in ("test",)}
    assert not compile_b & set(dep_keys(k))
    # front: static files and templates at the place Boot serves / resolves them from a jar
    assert _files(ws, "shop-front") == ["pom.xml", "src/main/resources/static/app.css",
                                        "src/main/resources/templates/home.html"]
    f = _pom(ws, "shop-front/pom.xml")
    assert _parent(f) == COMPP and t(f, "packaging") == "jar"
    assert (res.boot_module, res.comp_module, res.front_module, res.boot_packaging) == (
        "shop-boot", "shop-back", "shop-front", "jar")
    assert any(n.startswith("boot split: single-module Spring Boot app split into shop-back, shop-front, shop-boot")
               for n in res.notes), res.notes
    assert not any(n.startswith("boot_pom:") for n in res.notes)
    assert (root / "lombok.config").is_file() and (root / "README.md").is_file()


def test_split_is_idempotent(tmp_path: Path, ccfg: Config) -> None:
    ws = make_ws(tmp_path, _boot_files(), "split-twice")
    assert Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="layout", params={"boot_single": True}).ok
    before = _tree(ws)
    res2 = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="layout", params={"boot_single": True})
    assert res2.ok and "already restructured" in res2.notes and _tree(ws) == before


def test_split_without_web_resources_has_no_front(tmp_path: Path, ccfg: Config) -> None:
    files = boot2_files()
    del files["src/main/resources/static/app.css"]
    ws = make_ws(tmp_path, files, "split-noweb")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert [m.text for m in all_(_pom(ws), "modules/module")] == ["shop-back", "shop-boot"]
    assert not (ws.root / "shop-front").exists() and res.front_module is None


def test_split_boot_static_locations_and_webapp(tmp_path: Path, ccfg: Config) -> None:
    """Every classpath location Boot serves static content from, and a `src/main/webapp` web root, reach the front
    jar at a place that keeps the URL."""
    files = boot2_files()
    files["src/main/resources/public/robots.txt"] = "x\n"
    files["src/main/resources/resources/r.js"] = "r\n"
    files["src/main/resources/META-INF/resources/m.css"] = "m\n"
    files["src/main/resources/META-INF/spring.factories"] = "a=b\n"
    files["src/main/webapp/img/logo.png"] = "PNG\n"
    ws = make_ws(tmp_path, files, "split-static")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert _files(ws, "shop-front") == [
        "pom.xml", "src/main/resources/META-INF/resources/m.css", "src/main/resources/public/robots.txt",
        "src/main/resources/resources/r.js", "src/main/resources/static/app.css",
        "src/main/resources/static/img/logo.png"]
    assert (ws.root / "shop-back/src/main/resources/META-INF/spring.factories").is_file()


JSP_PROPS = "spring.mvc.view.prefix=/WEB-INF/views/\nspring.mvc.view.suffix=.jsp\n"


def _jsp_files() -> dict[str, str]:
    files = boot2_files()
    files["src/main/resources/application.properties"] = JSP_PROPS
    files["src/main/webapp/WEB-INF/views/home.jsp"] = (
        '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n<c:out value="hi"/>\n')
    files["src/main/webapp/css/site.css"] = "body{}\n"
    return files


def _jsp_profile(ws):
    p = boot2_profile(ws)
    p.source.jsp_count = 1
    p.source.jsp_paths = ["src/main/webapp/WEB-INF/views/home.jsp"]
    return p


def test_split_jsps_keep_their_view_names(tmp_path: Path, ccfg: Config) -> None:
    """A Boot app's JSPs keep their web path inside the front jar's META-INF/resources (the app's own
    spring.mvc.view.prefix still resolves them); JSPs make the boot module an executable WAR."""
    ws = make_ws(tmp_path, _jsp_files(), "split-jsp")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    root = ws.root
    assert (root / "shop-front/src/main/resources/META-INF/resources/WEB-INF/views/home.jsp").is_file()
    assert (root / "shop-front/src/main/resources/static/css/site.css").is_file()
    assert (root / "shop-boot/src/main/resources/application.properties").read_text() == JSP_PROPS
    b = _pom(ws, "shop-boot/pom.xml")
    assert t(b, "packaging") == "war" and res.boot_packaging == "war"
    deps = {f"{t(d, 'groupId')}:{t(d, 'artifactId')}": t(d, "scope") for d in all_(b, "dependencies/dependency")}
    assert deps["org.apache.tomcat.embed:tomcat-embed-jasper"] == "provided"
    assert not any("outside /WEB-INF/jsp/" in fu for fu in res.manual_followups)


def test_split_front_module_null_keeps_web_files_in_boot(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.front_module = None
    ws = make_ws(tmp_path, _jsp_files() | {"src/main/resources/templates/t.html": "<p/>\n"}, "split-nofront")
    res = Restructurer().apply(ws, _jsp_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    root = ws.root
    assert [m.text for m in all_(_pom(ws), "modules/module")] == ["shop-back", "shop-boot"]
    for f in ("src/main/resources/static/app.css", "src/main/resources/templates/t.html",
              "src/main/webapp/WEB-INF/views/home.jsp", "src/main/webapp/css/site.css"):
        assert (root / "shop-boot" / f).is_file(), f
        assert not (root / "shop-back" / f).exists(), f
    assert t(_pom(ws, "shop-boot/pom.xml"), "packaging") == "war"           # JSPs in the web root → executable WAR
    assert res.front_module is None


# ---------------------------------------------------------------------------------------------- what the move breaks
def test_split_moves_users_of_the_application_class_along(tmp_path: Path, ccfg: Config) -> None:
    """A source that names the application class cannot stay in the source module (it cannot depend on the boot
    module): it goes with the application class, and so does whatever names it in turn; a hand-over line says so."""
    files = boot2_files()
    files["src/main/java/com/acme/shop/support/Banner.java"] = java(
        "com.acme.shop.support", "Banner", body="    String n = com.acme.shop.ShopApplication.class.getName();")
    files["src/main/java/com/acme/shop/support/BannerUser.java"] = java(
        "com.acme.shop.support", "BannerUser", body="    Banner b;")
    files["src/main/java/com/acme/shop/support/Unrelated.java"] = java(
        "com.acme.shop.support", "Unrelated", body="    // ShopApplication in a comment only\n    String s = \"ShopApplication\";")
    files["src/test/java/com/acme/shop/web/HelloWebTest.java"] = java(
        "com.acme.shop.web", "HelloWebTest", "@WebMvcTest(HelloController.class)",
        imports="import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;")
    ws = make_ws(tmp_path, files, "split-users")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    root = ws.root
    for f in ("Banner", "BannerUser"):
        assert (root / f"shop-boot/src/main/java/com/acme/shop/support/{f}.java").is_file(), f
    assert (root / "shop-back/src/main/java/com/acme/shop/support/Unrelated.java").is_file()
    assert (root / "shop-boot/src/test/java/com/acme/shop/web/HelloWebTest.java").is_file()   # needs the app context
    assert (root / "shop-back/src/test/java/com/acme/shop/service/GreeterTest.java").is_file()
    fu = [x for x in res.manual_followups if x.startswith("boot split: 2 class(es) that use the application class")]
    assert fu and "com.acme.shop.support.Banner" in fu[0] and "com.acme.shop.support.BannerUser" in fu[0], \
        res.manual_followups
    assert "org.springframework.boot:spring-boot-starter-test" in dep_keys(_pom(ws, "shop-boot/pom.xml"))


def test_split_scan_not_covering_the_moved_classes_is_said(tmp_path: Path, ccfg: Config) -> None:
    files = boot2_files()
    files["src/main/java/com/other/lib/Helper.java"] = java("com.other.lib", "Helper", "@Component")
    ws = make_ws(tmp_path, files, "split-scan")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    fu = [x for x in res.manual_followups if x.startswith("boot split: the component scan of com.acme.shop")]
    assert fu and "com.other.lib" in fu[0], res.manual_followups
    # an explicit scan on the application class: nothing to say
    files["src/main/java/com/acme/shop/ShopApplication.java"] = java(
        "com.acme.shop", "ShopApplication", '@SpringBootApplication(scanBasePackages = {"com.acme", "com.other"})')
    ws2 = make_ws(tmp_path, files, "split-scan-explicit")
    res2 = Restructurer().apply(ws2, boot2_profile(ws2), ccfg, mode="boot_pom")
    assert res2.ok, res2.error
    assert not any(x.startswith("boot split: the component scan") for x in res2.manual_followups)


def test_split_real_path_reads_are_said(tmp_path: Path, ccfg: Config) -> None:
    files = boot2_files()
    files["src/main/webapp/files/form.xlsx"] = "XLSX\n"
    files["src/main/java/com/acme/shop/web/Download.java"] = java(
        "com.acme.shop.web", "Download", body="    String p(jakarta.servlet.ServletContext c) "
                                             "{ return c.getRealPath(\"/files/form.xlsx\"); }")
    ws = make_ws(tmp_path, files, "split-realpath")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    fu = [x for x in res.manual_followups if x.startswith("boot split: ")
          and "getRealPath" in x]
    assert fu and "shop-back/src/main/java/com/acme/shop/web/Download.java" in fu[0], res.manual_followups


# ---------------------------------------------------------------------------------------------- what the switch leaves alone
@pytest.mark.parametrize("value", ["split", "wrap"])
def test_multi_module_boot_ignores_the_switch(tmp_path: Path, ccfg: Config, value: str) -> None:
    ccfg.layout.boot_single_module = value
    ws = make_ws(tmp_path, _boot_multi_files(), f"multi-{value}")
    before = sorted(_tree(ws))
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert sorted(_tree(ws)) == before                                         # directories / files kept
    assert any(n.startswith("boot_pom: directories kept") for n in res.notes)


def test_msa_ignores_the_switch(tmp_path: Path, ccfg: Config) -> None:
    trees = []
    for value in ("split", "wrap"):
        ccfg.layout.boot_single_module = value
        ws = make_ws(tmp_path, msa_files(), f"msa-{value}")
        res = Restructurer(commit=False).apply(ws, msa_profile(ws), ccfg, mode="msa_pom")
        assert res.ok, res.error
        trees.append(_tree(ws))
    assert trees[0] == trees[1]


def test_wrap_value_still_wraps(tmp_path: Path, ccfg: Config) -> None:
    ccfg.layout.boot_single_module = "wrap"
    ws = make_ws(tmp_path, _boot_files(), "wrap")
    res = Restructurer().apply(ws, boot2_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    assert [m.text for m in all_(_pom(ws), "modules/module")] == ["shop-boot"]
    assert (ws.root / "shop-boot/src/main/resources/static/app.css").is_file()
    assert not (ws.root / "shop-back").exists() and not (ws.root / "shop-front").exists()


def test_not_a_boot_case_is_untouched_by_boot_single(tmp_path: Path, ccfg: Config) -> None:
    """`boot_single` on a layout step only adds the Boot placements; a profile with no Boot app keeps the plain
    split (no application class to move users along with)."""
    files = {k: v for k, v in boot2_files().items() if "ShopApplication" not in k}
    ws = make_ws(tmp_path, files, "plain")
    prof = profile(ws, CaseType.SPRING_BOOT, base_package="com.acme.shop")
    res = Restructurer().apply(ws, prof, ccfg, mode="layout", params={"boot_single": True})
    assert res.ok, res.error
    assert (ws.root / "shop-boot/src/main/java/com/acme/shop/Application.java").is_file()
