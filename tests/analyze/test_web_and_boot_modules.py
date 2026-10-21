"""Round 9c: web resources (jsp_paths / web_static_dirs / web_static_count) and boot modules by naming rule."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.analyze.source_scanner import WEB_STATIC_EXTENSIONS, web_static_root
from sbmig.core.models import CaseType
from tests.analyze.helpers import (BOOT_PLUGIN, PSVM, dep, make_boot33, make_servlet, pom, write, write_java)

BOOT_PARENT = ("org.springframework.boot", "spring-boot-starter-parent", "3.3.5")


# ------------------------------------------------------------------------------------------------ web resources
def test_web_static_root_rules() -> None:
    assert web_static_root("src/main/webapp/index.html") == "src/main/webapp"
    assert web_static_root("web/src/main/webapp/css/a.CSS") == "web/src/main/webapp"
    assert web_static_root("src/main/webapp/WEB-INF/views/x.html") is None
    assert web_static_root("src/main/webapp/WEB-INF") is None
    assert web_static_root("src/main/webapp/index.jsp") is None          # JSPs are reported separately
    assert web_static_root("src/main/resources/static/js/app.js") == "src/main/resources/static"
    assert web_static_root("a/b/src/main/resources/public/favicon.ico") == "a/b/src/main/resources/public"
    assert web_static_root("src/main/resources/templates/home.html") == "src/main/resources/templates"
    assert web_static_root("src/main/resources/application.yml") is None
    assert web_static_root("src/main/resources/static/js") is None       # a directory (no extension)
    assert web_static_root("src/test/resources/static/x.js") is None
    assert {"html", "css", "js", "png", "woff2", "json", "txt"} <= WEB_STATIC_EXTENSIONS
    assert "jsp" not in WEB_STATIC_EXTENSIONS and "xml" not in WEB_STATIC_EXTENSIONS


def test_webapp_static_and_jsp(tmp_path: Path, cfg) -> None:
    make_servlet(tmp_path)                       # index.jsp + WEB-INF/views/list.jsp + web.xml
    write(tmp_path, "src/main/webapp/css/site.css", "body{}")
    write(tmp_path, "src/main/webapp/img/logo.png", "x")
    write(tmp_path, "src/main/webapp/WEB-INF/lib/readme.txt", "not static")
    s = analyze_path(tmp_path, cfg).source
    assert s.jsp_paths == ["src/main/webapp/WEB-INF/views/list.jsp", "src/main/webapp/index.jsp"]
    assert s.jsp_count == 2
    assert s.web_static_dirs == ["src/main/webapp"] and s.web_static_count == 2


def test_resources_static_and_templates_multi_module(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("parent", packaging="pom", modules=("app", "web")))
    write(tmp_path, "app/pom.xml", pom("app"))
    write_java(tmp_path, "app", "com.a", "A")
    write(tmp_path, "web/pom.xml", pom("web"))
    write(tmp_path, "web/src/main/resources/static/js/app.js", "1")
    write(tmp_path, "web/src/main/resources/static/js/app.js.map", "{}")
    write(tmp_path, "web/src/main/resources/templates/home.html", "<html/>")
    write(tmp_path, "web/src/main/resources/templates/mail.ftl", "x")
    write(tmp_path, "app/src/main/resources/public/robots.txt", "x")
    write(tmp_path, "app/src/main/resources/static/README", "no extension → not counted")
    s = analyze_path(tmp_path, cfg).source
    assert s.web_static_dirs == ["app/src/main/resources/public", "web/src/main/resources/static",
                                 "web/src/main/resources/templates"]
    assert s.web_static_count == 5 and s.jsp_paths == []


def test_no_web_resources(tmp_path: Path, cfg) -> None:
    make_boot33(tmp_path)
    s = analyze_path(tmp_path, cfg).source
    assert s.jsp_paths == [] and s.web_static_dirs == [] and s.web_static_count == 0


def test_web_static_ignores_target(tmp_path: Path, cfg) -> None:
    make_boot33(tmp_path)
    write(tmp_path, "target/classes/static/x.js", "1")
    write(tmp_path, "node_modules/src/main/resources/static/x.js", "1")
    assert analyze_path(tmp_path, cfg).source.web_static_count == 0


# ------------------------------------------------------------------------------------------------ boot modules
def _boot_multi(root: Path, modules: tuple[str, ...], apps: dict[str, str]) -> None:
    """Root aggregator on the Boot parent; `apps` maps module → 'plugin' | 'class' | 'none'."""
    write(root, "pom.xml", pom("shop", packaging="pom", parent=BOOT_PARENT, relative_path="", modules=modules))
    for m in modules:
        kind = apps.get(m, "none")
        write(root, f"{m}/pom.xml", pom(m, parent=("com.acme", "shop", "1.0.0"),
                                        deps=(dep("org.springframework.boot", "spring-boot-starter-web"),),
                                        plugins=(BOOT_PLUGIN,) if kind == "plugin" else ()))
        if kind in ("plugin", "class"):
            write_java(root, m, f"com.acme.{m.replace('-', '')}", "App", annotations=("SpringBootApplication",),
                       imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
        else:
            write_java(root, m, f"com.acme.{m.replace('-', '')}", "Lib")


def test_boot_modules_by_name_and_pom(tmp_path: Path, cfg) -> None:
    _boot_multi(tmp_path, ("shop-boot", "shop-core"), {"shop-boot": "plugin"})
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.SPRING_BOOT
    assert p.boot_modules == ["shop-boot"]
    assert any(r == "boot modules (*-boot): shop-boot" for r in p.reasons)
    assert not any("naming rule" in w or "not a Boot app" in w for w in p.warnings)


def test_boot_module_confirmed_by_application_class(tmp_path: Path, cfg) -> None:
    _boot_multi(tmp_path, ("api-boot", "api-core"), {"api-boot": "class"})
    assert analyze_path(tmp_path, cfg).boot_modules == ["api-boot"]


def test_boot_app_not_named_like_pattern_warns(tmp_path: Path, cfg) -> None:
    _boot_multi(tmp_path, ("app", "core"), {"app": "plugin"})
    p = analyze_path(tmp_path, cfg)
    msg = "boot module app is not named *-boot (company naming rule)"
    assert p.boot_modules == [] and msg in p.warnings and msg in p.reasons


def test_named_boot_module_without_boot_pom_warns(tmp_path: Path, cfg) -> None:
    _boot_multi(tmp_path, ("shop-boot", "shop-web"), {"shop-web": "plugin"})
    p = analyze_path(tmp_path, cfg)
    assert p.boot_modules == []
    assert "shop-boot is named like a boot module but its pom is not a Boot app" in p.warnings
    assert "boot module shop-web is not named *-boot (company naming rule)" in p.warnings


def test_boot_modules_nested_and_custom_pattern(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("shop", packaging="pom", parent=BOOT_PARENT, relative_path="", modules=("apps",)))
    write(tmp_path, "apps/pom.xml", pom("apps", packaging="pom", parent=("com.acme", "shop", "1.0.0"),
                                         modules=("shop-app",)))
    write(tmp_path, "apps/shop-app/pom.xml", pom("shop-app", plugins=(BOOT_PLUGIN,)))
    write_java(tmp_path, "apps/shop-app", "com.acme", "App", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    cfg.layout.boot_module_pattern = "*-app"
    assert analyze_path(tmp_path, cfg).boot_modules == ["apps/shop-app"]


def test_single_module_boot_has_no_boot_modules(tmp_path: Path, cfg) -> None:
    p = analyze_path(make_boot33(tmp_path), cfg)
    assert p.boot_modules == []
    assert not any("naming rule" in w for w in p.warnings)


def test_module_outside_reactor_is_not_a_candidate(tmp_path: Path, cfg) -> None:
    _boot_multi(tmp_path, ("shop-core",), {})
    write(tmp_path, "samples/demo-boot/pom.xml", pom("demo-boot", plugins=(BOOT_PLUGIN,)))   # not in <modules>
    p = analyze_path(tmp_path, cfg)
    assert p.boot_modules == [] and not any("demo-boot" in w for w in p.warnings)


def test_msa_boot_modules(tmp_path: Path, cfg) -> None:
    # two services: order (order-boot + order-core) and billing (single module, not named *-boot)
    write(tmp_path, "pom.xml", pom("platform", packaging="pom", modules=("order", "billing")))
    write(tmp_path, "order/pom.xml", pom("order", packaging="pom", modules=("order-boot", "order-core")))
    write(tmp_path, "order/order-boot/pom.xml", pom("order-boot", plugins=(BOOT_PLUGIN,)))
    write_java(tmp_path, "order/order-boot", "com.acme.order", "OrderApp", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    write(tmp_path, "order/order-core/pom.xml", pom("order-core"))
    write_java(tmp_path, "order/order-core", "com.acme.order.core", "Svc")
    write(tmp_path, "billing/pom.xml", pom("billing", plugins=(BOOT_PLUGIN,)))
    write_java(tmp_path, "billing", "com.acme.billing", "BillingApp", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA
    assert p.boot_modules == ["order/order-boot"]
    assert "boot module billing is not named *-boot (company naming rule)" in p.warnings


def test_msa_independent_roots_services_are_candidates(tmp_path: Path, cfg) -> None:
    for svc in ("a-boot", "b-boot"):
        write(tmp_path, f"{svc}/pom.xml", pom(svc, plugins=(BOOT_PLUGIN,)))
        write_java(tmp_path, svc, f"com.acme.{svc[0]}", "App", annotations=("SpringBootApplication",),
                   imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA and p.boot_modules == ["a-boot", "b-boot"]
