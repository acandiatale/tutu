"""v2.1z-j: bundled jars wired at the restructure, before the first build (sbmig.restructure.bundled_jars + the
restructurer hook). The planner's verdicts one by one, then a `spring_legacy` WAR on the company layout whose
`WEB-INF/lib` holds an unknown vendor jar (wired as system scope), an old Spring jar the target chain manages
(superseded), a servlet API copy (container) and a jar nothing references (unused)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.analyze.bundled_jars import scan_bundled_jars
from sbmig.build import parent_chain
from sbmig.core.config import Config
from sbmig.core.models import CaseType, JarCoordinate
from sbmig.core.workspace import Workspace
from sbmig.restructure import Restructurer
from sbmig.restructure.bundled_jars import (
    BundledPlan,
    apply_bundled_jars,
    plan_bundled_jars,
    repository_has_fn,
    unused_jar_followup,
)

from .helpers import HEAD, all_, dep, java, make_ws, profile, t, xml
from .test_company_layout import _roles

LIB = "src/main/webapp/WEB-INF/lib"
WEBMVC = "org.springframework:spring-webmvc"

MODULE_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>shop-back</artifactId>
    <version>1.0</version>
    <dependencies>
        <dependency>
            <groupId>com.acme</groupId>
            <artifactId>already</artifactId>
            <version>1.0</version>
        </dependency>
    </dependencies>
</project>
"""

BOOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>shop-boot</artifactId>
    <version>1.0</version>
    <dependencies>
        <dependency>
            <groupId>com.example</groupId>
            <artifactId>shop-back</artifactId>
            <version>1.0</version>
        </dependency>
    </dependencies>
    <build>
        <plugins>
            <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
            </plugin>
        </plugins>
    </build>
</project>
"""


def _jar(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


CLS = b"\xca\xfe\xba\xbe"
SEED = {"com/acme/seed/Seed.class": CLS}
WEBMVC_JAR = {"org/springframework/web/servlet/DispatcherServlet.class": CLS,
              "META-INF/maven/org.springframework/spring-webmvc/pom.properties":
                  b"groupId=org.springframework\nartifactId=spring-webmvc\nversion=4.3.30.RELEASE\n"}
SERVLET = {"javax/servlet/Servlet.class": CLS, "javax/servlet/http/HttpServlet.class": CLS}
TOOL = {"com/tool/x/Tool.class": CLS}


# ---------------------------------------------------------------------------------------------- the planner
def _plain_ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / "shop-back/src/main/java/com/example").mkdir(parents=True)
    (root / "shop-boot").mkdir(parents=True)
    (root / "shop-back/pom.xml").write_text(MODULE_POM)
    (root / "shop-boot/pom.xml").write_text(BOOT_POM)
    (root / "shop-back/src/main/java/com/example/Use.java").write_text(
        "package com.example;\nimport com.acme.seed.Seed;\nimport com.vendor.lib.Thing;\nimport com.acme.old.Old;\n"
        "public class Use {}\n")
    _jar(root / f"shop-back/{LIB}/acme-seed-1.0.jar", SEED)
    return Workspace(root, tmp_path / "run", "r1")


def _jc(name: str, provides: list[str], **kw) -> JarCoordinate:
    return JarCoordinate(path=f"shop-back/{LIB}/{name}", provides=provides, packages=provides, **kw)


def _plan(ws, cfg, jars, *, managed=(), repo=None):
    return plan_bundled_jars(ws, cfg, jars, module_pom="shop-back/pom.xml", boot_poms=["shop-boot/pom.xml"],
                             managed_keys=managed, repository=repo, source_roots=["shop-back/src"],
                             extra_dirs=["shop-back"])


def test_planner_each_verdict(tmp_path: Path, cfg: Config) -> None:
    ws = _plain_ws(tmp_path)
    jars = [
        _jc("spring-webmvc-4.3.30.jar", ["org.springframework.web.servlet"], group_id="org.springframework",
            artifact_id="spring-webmvc", version="4.3.30.RELEASE", source="pom_properties"),
        _jc("servlet-api-2.5.jar", ["javax.servlet"]),
        _jc("unused-tool-1.0.jar", ["com.tool.x"]),
        _jc("already-1.0.jar", ["com.acme.old"], group_id="com.acme", artifact_id="already", version="1.0",
            source="pom_properties"),
        _jc("thing-2.0.jar", ["com.vendor.lib"], group_id="com.vendor", artifact_id="thing", version="2.0",
            source="pom_properties"),
        _jc("acme-seed-1.0.jar", ["com.acme.seed"]),
    ]
    asked: list[tuple[str, str, str]] = []

    def repo(g, a, v):
        asked.append((g, a, v))
        return (g, a, v) == ("com.vendor", "thing", "2.0")

    plan = _plan(ws, cfg, jars, managed={WEBMVC}, repo=repo)
    got = {i.jar.path.rsplit("/", 1)[-1]: i.verdict for i in plan.items}
    assert got == {"spring-webmvc-4.3.30.jar": "superseded", "servlet-api-2.5.jar": "container",
                   "unused-tool-1.0.jar": "unused", "already-1.0.jar": "declared", "thing-2.0.jar": "compile",
                   "acme-seed-1.0.jar": "system"}
    assert all(i.why.startswith(i.verdict) for i in plan.items)
    assert asked == [("com.vendor", "thing", "2.0")]          # only trusted, complete coordinates are asked
    assert jars[2].used is None                               # the analyzer's JarCoordinate is not mutated
    # pure: nothing written
    assert "thing" not in (ws.root / "shop-back/pom.xml").read_text()


def test_planner_container_by_scope_and_unknown_jar_skip(tmp_path: Path, cfg: Config) -> None:
    ws = _plain_ws(tmp_path)
    cfg.intake.unknown_jar = "skip"
    jars = [_jc("jsp-api.jar", ["com.acme.seed"], scope="provided"), _jc("acme-seed-1.0.jar", ["com.acme.seed"])]
    plan = _plan(ws, cfg, jars)
    assert [i.verdict for i in plan.items] == ["container", "skip"]
    assert plan.skipped()[1]["why"].startswith("skip: not in the repository and intake.unknown_jar is skip")
    assert apply_bundled_jars(ws, cfg, plan) == [] and plan.wired() == []


def test_apply_system_and_compile_and_failure(tmp_path: Path, cfg: Config) -> None:
    ws = _plain_ws(tmp_path)
    jars = [_jc("acme-seed-1.0.jar", ["com.acme.seed"]),
            _jc("thing-2.0.jar", ["com.vendor.lib"], group_id="com.vendor", artifact_id="thing", version="2.0",
                source="pom_properties"),
            _jc("ghost-1.0.jar", ["com.acme.seed"])]                # not on disk → wiring fails → skip
    plan = _plan(ws, cfg, jars, repo=lambda g, a, v: a == "thing")
    assert [i.verdict for i in plan.items] == ["system", "compile", "system"]
    wired = apply_bundled_jars(ws, cfg, plan, project_group="com.example")
    assert [(w.artifact_id, w.scope) for w in wired] == [("acme-seed", "system"), ("thing", "compile")]
    assert plan.items[2].verdict == "skip" and plan.items[2].why.startswith("skip: wiring failed: ValueError")
    rows = plan.wired()
    assert rows[0] == {"path": f"shop-back/{LIB}/acme-seed-1.0.jar", "pom": "shop-back/pom.xml",
                       "coordinates": "com.example:acme-seed:1.0", "scope": "system", "placeholder": True,
                       "boot_poms": ["shop-boot/pom.xml"]}
    assert rows[1]["scope"] == "compile" and rows[1]["coordinates"] == "com.vendor:thing:2.0"
    assert [s["path"].rsplit("/", 1)[-1] for s in plan.skipped()] == ["ghost-1.0.jar"]
    back = (ws.root / "shop-back/pom.xml").read_text()
    assert "<systemPath>${project.basedir}/lib/acme-seed-1.0.jar</systemPath>" in back
    assert "<artifactId>thing</artifactId>" in back
    boot = (ws.root / "shop-boot/pom.xml").read_text()
    assert "<includeSystemScope>true</includeSystemScope>" in boot and "acme-seed" in boot
    # a second plan on the wired tree: the g:a is declared now
    again = _plan(ws, cfg, jars[1:2], repo=lambda g, a, v: True)
    assert again.items[0].verdict == "declared"


def test_same_sha1_follows_the_first(tmp_path: Path, cfg: Config) -> None:
    ws = _plain_ws(tmp_path)
    a = _jc("acme-seed-1.0.jar", ["com.acme.seed"], sha1="abc")
    b = a.model_copy(update={"path": "shop-back/lib2/acme-seed-1.0.jar"})
    plan = _plan(ws, cfg, [a, b])
    assert [i.verdict for i in plan.items] == ["system", "declared"]
    assert plan.items[1].why == f"declared: same jar as {a.path}"


def test_repository_has_fn() -> None:
    class L:
        def pom_text(self, g, a, v):
            return "<project/>" if v == "1" else None

        def versions(self, g, a):
            if a == "boom":
                raise OSError("offline")
            return ["2"]

    has = repository_has_fn(L())
    assert has("g", "a", "1") and has("g", "a", "2") and not has("g", "a", "3") and not has("g", "boom", "3")
    assert not repository_has_fn(None)("g", "a", "1")


def test_empty_plan_shapes() -> None:
    assert BundledPlan().wired() == [] and BundledPlan().skipped() == []


# ---------------------------------------------------------------------------------------------- the restructure
WAR_POM = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <packaging>war</packaging>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "5.3.30")
           + "    </dependencies>\n</project>\n")


@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    cfg.intake.jar_lookup = False                      # no sha1 lookup over the network
    return _roles(cfg)


@pytest.fixture()
def managed_webmvc(monkeypatch):
    facts = parent_chain.ParentChainFacts(roots=["com.company.platform:base-parent:4.0.0"],
                                          managed={WEBMVC: "7.0.8"}, sources=["test facts"])
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)


def _war_project(tmp_path: Path, cfg: Config):
    files = {
        "pom.xml": WAR_POM,
        "src/main/java/com/acme/shop/CryptoUtil.java": java(
            "com.acme.shop", "CryptoUtil", imports="import com.acme.seed.Seed;\n"),
        "src/main/java/com/acme/shop/web/HomeController.java": java("com.acme.shop.web", "HomeController",
                                                                     "@Controller"),
        "src/main/webapp/index.html": "<html/>\n",
        "src/main/webapp/WEB-INF/web.xml": "<web-app/>\n",
    }
    ws = make_ws(tmp_path, files, "bundled")
    for name, entries in (("acme-seed-1.0.jar", SEED), ("spring-webmvc-4.3.30.jar", WEBMVC_JAR),
                          ("servlet-api-2.5.jar", SERVLET), ("unused-tool-1.0.jar", TOOL)):
        _jar(ws.root / LIB / name, entries)
    ws.commit("as-is jars")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", packaging="war",
                   base_package="com.acme.shop", controllers=["com.acme.shop.web.HomeController"],
                   has_web_xml=True, web_xml_paths=["src/main/webapp/WEB-INF/web.xml"],
                   web_static_dirs=["src/main/webapp"], web_static_count=1)
    prof.bundled_jars = scan_bundled_jars(ws, cfg, [])
    return ws, prof


def test_restructure_wires_the_bundled_jars(tmp_path: Path, ccfg: Config, managed_webmvc) -> None:
    ws, prof = _war_project(tmp_path, ccfg)
    by_name = {j.path.rsplit("/", 1)[-1]: j for j in prof.bundled_jars}
    assert set(by_name) == {"acme-seed-1.0.jar", "spring-webmvc-4.3.30.jar", "servlet-api-2.5.jar",
                            "unused-tool-1.0.jar"}
    assert by_name["spring-webmvc-4.3.30.jar"].key == WEBMVC
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    # the back pom: the system dependency on the copy under shop-back/lib
    assert (ws.root / "shop-back/lib/acme-seed-1.0.jar").is_file()
    back = xml(ws.root / "shop-back/pom.xml")
    seed = [d for d in all_(back, "dependencies/dependency") if t(d, "artifactId") == "acme-seed"]
    assert len(seed) == 1 and t(seed[0], "scope") == "system"
    assert t(seed[0], "systemPath") == "${project.basedir}/lib/acme-seed-1.0.jar"
    assert not any(t(d, "artifactId") in ("spring-webmvc", "servlet-api", "unused-tool")
                   for d in all_(back, "dependencies/dependency"))
    # the boot pom: the same system dependency + includeSystemScope
    boot_text = (ws.root / "shop-boot/pom.xml").read_text()
    assert "<systemPath>${project.basedir}/../shop-back/lib/acme-seed-1.0.jar</systemPath>" in boot_text
    assert "<includeSystemScope>true</includeSystemScope>" in boot_text
    # the result
    assert [w["path"] for w in res.jars_wired] == [f"{LIB}/acme-seed-1.0.jar"]
    w = res.jars_wired[0]
    assert (w["pom"], w["scope"], w["placeholder"]) == ("shop-back/pom.xml", "system", True)
    assert w["coordinates"] == "com.acme:acme-seed:1.0" and w["boot_poms"] == ["shop-boot/pom.xml"]
    why = {s["path"].rsplit("/", 1)[-1]: s["why"] for s in res.jars_skipped}
    assert set(why) == {"spring-webmvc-4.3.30.jar", "servlet-api-2.5.jar", "unused-tool-1.0.jar"}
    assert why["spring-webmvc-4.3.30.jar"].startswith("superseded")
    assert why["servlet-api-2.5.jar"].startswith("container")
    assert why["unused-tool-1.0.jar"].startswith("unused")
    assert "shop-back/pom.xml" in res.changed_files and "shop-back/lib/acme-seed-1.0.jar" in res.changed_files
    # the Handover
    fu = res.manual_followups
    assert any(f.startswith("⚠ bundled jar `acme-seed-1.0.jar` (Maven coordinates unknown") for f in fu)
    assert unused_jar_followup("unused-tool-1.0.jar") in fu
    left = [f for f in fu if "under WEB-INF/lib, WEB-INF/classes or META-INF" in f]
    assert len(left) == 1 and left[0].startswith("3 file(s)")
    assert "acme-seed-1.0.jar" not in left[0]
    assert all(n in left[0] for n in ("spring-webmvc-4.3.30.jar", "servlet-api-2.5.jar", "unused-tool-1.0.jar"))
    assert any(n.startswith("bundled jars not wired (superseded): spring-webmvc-4.3.30.jar") for n in res.notes)
    assert any(n.startswith("bundled jars not wired (container): servlet-api-2.5.jar") for n in res.notes)
    # the analyzer's facts are untouched
    assert by_name["unused-tool-1.0.jar"].used is False and by_name["acme-seed-1.0.jar"].used is True


def test_restructure_wiring_off_keeps_v21zi(tmp_path: Path, ccfg: Config, managed_webmvc) -> None:
    ccfg.layout.wire_bundled_jars = False
    ws, prof = _war_project(tmp_path, ccfg)
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert res.jars_wired == [] and res.jars_skipped == []
    assert "acme-seed" not in (ws.root / "shop-back/pom.xml").read_text()
    left = [f for f in res.manual_followups if "under WEB-INF/lib, WEB-INF/classes or META-INF" in f]
    assert len(left) == 1 and left[0].startswith("4 file(s)") and "acme-seed-1.0.jar" in left[0]


def test_restructure_root_lib_jar_goes_to_the_back_module(tmp_path: Path, ccfg: Config, managed_webmvc) -> None:
    """A jar beside the root pom (`lib/`, not moved by the layout) is wired into the back module, copied to its lib/."""
    files = {"pom.xml": WAR_POM.replace("<packaging>war</packaging>", "<packaging>jar</packaging>"),
             "src/main/java/com/acme/shop/CryptoUtil.java": java("com.acme.shop", "CryptoUtil",
                                                                 imports="import com.acme.seed.Seed;\n")}
    ws = make_ws(tmp_path, files, "rootlib")
    _jar(ws.root / "lib/acme-seed-1.0.jar", SEED)
    ws.commit("as-is jar")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    prof.bundled_jars = scan_bundled_jars(ws, ccfg, [])
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    assert [(w["path"], w["pom"]) for w in res.jars_wired] == [("lib/acme-seed-1.0.jar", "shop-back/pom.xml")]
    assert (ws.root / "shop-back/lib/acme-seed-1.0.jar").is_file()
    assert "<systemPath>${project.basedir}/lib/acme-seed-1.0.jar</systemPath>" in (
        ws.root / "shop-back/pom.xml").read_text()


def test_known_coordinates_are_not_wired_blind_when_the_chain_facts_are_unknown(tmp_path):
    """Integrator (2026-11-04): with no managed keys at all (the chain's facts unreadable) nothing can be called
    superseded, so a jar with known coordinates (an old spring-webmvc, say) is skipped instead of wired; an
    unknown-coordinate vendor jar still goes system."""
    from sbmig.core.models import JarCoordinate
    from sbmig.core.workspace import Workspace
    from sbmig.restructure.bundled_jars import plan_bundled_jars

    src = tmp_path / "src" / "p"
    (src / "mod").mkdir(parents=True)
    (src / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                                 "<groupId>com.x</groupId><artifactId>p</artifactId><version>1</version></project>", encoding="utf-8")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t")
    spring = JarCoordinate(path="lib/spring-webmvc-4.3.30.jar", group_id="org.springframework", artifact_id="spring-webmvc",
                           version="4.3.30", source="pom_properties", provides=["org.springframework.web"])
    vendor = JarCoordinate(path="lib/crypt.jar", provides=["com.acme.crypt"])
    plan = plan_bundled_jars(ws, None, [spring, vendor], module_pom="pom.xml", managed_keys=(), managed_known=False,
                             repository=lambda g, a, v: True)
    verdicts = {i.jar.path: i.verdict for i in plan.items}
    assert verdicts == {"lib/spring-webmvc-4.3.30.jar": "skip", "lib/crypt.jar": "system"}
    assert "managed state unknown" in next(i.why for i in plan.items if i.verdict == "skip")
    # with the chain known and the key not managed, the same jar is a compile dependency (the repository has it)
    plan2 = plan_bundled_jars(ws, None, [spring], module_pom="pom.xml", managed_keys={"x:y"}, managed_known=True,
                              repository=lambda g, a, v: True)
    assert plan2.items[0].verdict == "compile"
