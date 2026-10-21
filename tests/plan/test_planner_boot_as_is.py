"""Round 9c routing: Boot as-is → restructure.boot_pom (in place) | restructure.apply (layout); MSA boot_modules."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_path
from sbmig.core.models import Phase, StepKind
from sbmig.loop.orchestrator import Orchestrator
from sbmig.plan import build_plan
from tests.analyze.helpers import (BOOT_PLUGIN, PSVM, make_boot27, make_servlet, make_spring_legacy, pom, write,
                                   write_java)

IN_PLACE = ["jdk21.rewrite", "jdk21.loop", "spring.rewrite", "spring.optional_bump", "spring.loop",
            "restructure.boot_pom", "restructure.loop", "verify.build"]


def _ids(plan):
    return [s.id for s in plan.steps]


def _step(plan, sid):
    return next(s for s in plan.steps if s.id == sid)


def _company(cfg):
    """The company defaults of config/default.yml (the test cfg keeps the pre-9c layout)."""
    lay = cfg.layout
    lay.boot_as_is, lay.module_dir = "in-place", ""
    lay.boot_module, lay.comp_module, lay.front_module = "{name}-boot", "{name}-back", "{name}-front"
    return cfg


def _boot_multi(root: Path) -> Path:
    write(root, "pom.xml", pom("shop", packaging="pom", relative_path="",
                               parent=("org.springframework.boot", "spring-boot-starter-parent", "3.3.5"),
                               modules=("shop-boot", "shop-core")))
    write(root, "shop-boot/pom.xml", pom("shop-boot", parent=("com.acme", "shop", "1.0.0"), plugins=(BOOT_PLUGIN,)))
    write_java(root, "shop-boot", "com.acme.shop", "App", annotations=("SpringBootApplication",),
               imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    write(root, "shop-core/pom.xml", pom("shop-core", parent=("com.acme", "shop", "1.0.0")))
    write_java(root, "shop-core", "com.acme.shop.core", "Svc")
    return root


def test_boot_in_place_single_module(tmp_path: Path, cfg) -> None:
    _company(cfg)
    plan = build_plan(analyze_path(make_boot27(tmp_path), cfg), cfg)
    assert _ids(plan) == IN_PLACE
    st = _step(plan, "restructure.boot_pom")
    assert st.kind == StepKind.MSA_POM and st.phase == Phase.RESTRUCTURE
    assert st.params == {"mode": "boot_pom", "boot_modules": []}
    assert st.title == "Boot as-is: parents / mandatory / optional in place (directories kept)" and st.reason
    assert Orchestrator._step_mode(st) == "boot_pom"
    note = next(n for n in plan.notes if n.startswith("restructure: rule boot-as-is"))
    assert "single-module Boot app → wrapped into boot-app-boot" in note
    # jdk21 / spring recipe steps are the same as with the layout rule
    spring = [r.id for r in _step(plan, "spring.rewrite").recipes]
    assert spring[0] == "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"


def test_boot_in_place_multi_module_passes_boot_modules(tmp_path: Path, cfg) -> None:
    _company(cfg)
    profile = analyze_path(_boot_multi(tmp_path), cfg)
    plan = build_plan(profile, cfg)
    st = _step(plan, "restructure.boot_pom")
    assert st.params == {"mode": "boot_pom", "boot_modules": ["shop-boot"]}
    assert "restructure.apply" not in _ids(plan)
    assert any("boot module(s) shop-boot (named *-boot)" in n for n in plan.notes)


def test_boot_layout_rule_keeps_restructure_apply(tmp_path: Path, cfg) -> None:
    _company(cfg).layout.boot_as_is = "layout"
    plan = build_plan(analyze_path(make_boot27(tmp_path), cfg), cfg)
    assert "restructure.boot_pom" not in _ids(plan)
    st = _step(plan, "restructure.apply")
    assert st.kind == StepKind.RESTRUCTURE and st.params == {"mode": "layout", "starter": "auto"}
    assert st.title == "restructure into boot-app-back, boot-app-boot with the in-house parent"
    assert any(n.startswith("restructure: rule boot-as-is=layout") for n in plan.notes)


def test_pre9c_layout_title(tmp_path: Path, cfg) -> None:
    # the e2e config (pre-9c layout, boot_as_is: layout): module/comp-jar + module/boot-jar
    plan = build_plan(analyze_path(make_boot27(tmp_path), cfg), cfg)
    assert _step(plan, "restructure.apply").title == ("restructure into module/comp-jar, module/boot-jar with the "
                                                       "in-house parent")


def test_non_boot_is_split_even_when_in_place(tmp_path: Path, cfg) -> None:
    _company(cfg)
    plan = build_plan(analyze_path(make_spring_legacy(tmp_path), cfg), cfg)
    assert "restructure.boot_pom" not in _ids(plan)
    assert _step(plan, "restructure.apply").params["mode"] == "layout"
    assert any(n.startswith("restructure: rule non-boot (spring_legacy)") for n in plan.notes)


def test_servlet_with_jsp_plans_front_module(tmp_path: Path, cfg) -> None:
    _company(cfg)
    make_servlet(tmp_path)
    write(tmp_path, "src/main/webapp/css/site.css", "body{}")
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    st = _step(plan, "restructure.apply")
    assert st.title == "restructure into legacy-web-back, legacy-web-boot, legacy-web-front with the in-house parent"
    note = next(n for n in plan.notes if "web resources" in n)
    assert "2 JSP(s)" in note and "1 static file(s) in src/main/webapp" in note
    assert "legacy-web-front" in note and "JSPs → legacy-web-boot packaged as war" in note


def test_non_boot_without_web_has_no_front(tmp_path: Path, cfg) -> None:
    from tests.analyze.helpers import make_pure_java
    _company(cfg)
    plan = build_plan(analyze_path(make_pure_java(tmp_path), cfg), cfg)
    assert _step(plan, "restructure.apply").title == "restructure into util-lib-back, util-lib-boot with the in-house parent"
    assert not any("web resources" in n for n in plan.notes)


def test_msa_params_carry_boot_modules(tmp_path: Path, cfg) -> None:
    _company(cfg)
    for svc in ("a-boot", "b-boot"):
        write(tmp_path, f"{svc}/pom.xml", pom(svc, plugins=(BOOT_PLUGIN,)))
        write_java(tmp_path, svc, f"com.acme.{svc[0]}", "App", annotations=("SpringBootApplication",),
                   imports=("org.springframework.boot.autoconfigure.SpringBootApplication",), body=PSVM)
    plan = build_plan(analyze_path(tmp_path, cfg), cfg)
    st = _step(plan, "restructure.msa_pom")
    assert st.params["mode"] == "msa_pom" and st.params["boot_modules"] == ["a-boot", "b-boot"]
    assert "restructure.boot_pom" not in _ids(plan) and "restructure.apply" not in _ids(plan)
