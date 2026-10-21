"""Round 9: Handover file paths recorded by fix attempts before the restructure step (`llm_fixed_files`) are mapped
to where the files live after the move to module/comp-jar | module/boot-jar (`resolve_current_path`)."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze.analyzer import PathView
from sbmig.core.config import LayoutConfig
from sbmig.report import build_handover
from sbmig.report.handover import resolve_current_path

from .test_handover import EFFECTIVE, as_is_profile, final_tree, findings, make_report


def tree(tmp_path: Path, *files: str) -> PathView:
    for f in files:
        p = tmp_path / "t" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    (tmp_path / "t").mkdir(exist_ok=True)
    return PathView(tmp_path / "t")


def test_existing_path_is_unchanged(tmp_path):
    v = tree(tmp_path, "src/main/java/a/A.java", "module/comp-jar/src/main/java/a/A.java")
    assert resolve_current_path(v, "src/main/java/a/A.java") == "src/main/java/a/A.java"
    assert resolve_current_path(None, "src/main/java/a/A.java") == "src/main/java/a/A.java"
    assert resolve_current_path(v, "") == ""


def test_single_module_as_is_path_moves_to_comp_jar(tmp_path):
    v = tree(tmp_path, "module/comp-jar/src/main/java/a/A.java")
    assert resolve_current_path(v, "src/main/java/a/A.java") == "module/comp-jar/src/main/java/a/A.java"
    assert resolve_current_path(v, "./src/main/java/a/A.java") == "module/comp-jar/src/main/java/a/A.java"


def test_multi_module_as_is_path_and_boot_jar_fallback(tmp_path):
    v = tree(tmp_path, "module/comp-jar/src/main/java/a/Svc.java", "module/boot-jar/src/main/java/a/Application.java")
    assert resolve_current_path(v, "core/src/main/java/a/Svc.java") == "module/comp-jar/src/main/java/a/Svc.java"
    assert resolve_current_path(v, "web\\src\\main\\java\\a\\Application.java") == \
        "module/boot-jar/src/main/java/a/Application.java"
    # nowhere to be found / no src component → the original path
    assert resolve_current_path(v, "core/src/main/java/a/Gone.java") == "core/src/main/java/a/Gone.java"
    assert resolve_current_path(v, "core/pom.xml") == "core/pom.xml"


def test_layout_names_from_cfg(tmp_path):
    v = tree(tmp_path, "mods/lib/src/main/java/a/A.java")
    cfg = type("C", (), {"layout": LayoutConfig(module_dir="mods", comp_module="lib", boot_module="app")})()
    assert resolve_current_path(v, "src/main/java/a/A.java", cfg) == "mods/lib/src/main/java/a/A.java"
    assert resolve_current_path(v, "src/main/java/a/A.java") == "src/main/java/a/A.java"   # defaults miss


def test_handover_names_the_current_paths(tmp_path, cfg):
    view = final_tree(tmp_path)
    for rel in ("module/comp-jar/src/main/java/a/Cmd.java", "module/comp-jar/src/main/java/a/Other.java"):
        p = view.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    fs = findings()
    fs[1] = fs[1].model_copy(update={"llm_fixed_files": ["src/main/java/a/Cmd.java", "core/src/main/java/a/Cmd.java",
                                                         "core/src/main/java/a/Other.java"]})
    fh = build_handover(as_is_profile(), view, make_report(advisories=fs), effective=EFFECTIVE, cfg=cfg).from_here
    line = next(x for x in fh if x.startswith("code rewritten by the LLM for hystrix: "))
    # both pre-restructure spellings of Cmd.java collapse into the one current path
    assert line.startswith("code rewritten by the LLM for hystrix: module/comp-jar/src/main/java/a/Cmd.java, "
                           "module/comp-jar/src/main/java/a/Other.java —")
    assert "core/src" not in line


def test_company_layout_paths_resolve_by_project_name(tmp_path):
    """Round 9c: the company layout puts the modules directly under the root as {name}-back / -boot / -front."""
    from sbmig.core.config import LayoutConfig
    from sbmig.report.handover import project_name, resolve_current_path
    v = tree(tmp_path, "shop-back/src/main/java/a/Svc.java", "shop-boot/src/main/java/a/Application.java",
             "shop-front/src/main/resources/static/index.html")
    cfg = type("C", (), {"layout": LayoutConfig()})()                   # defaults = company layout
    assert resolve_current_path(v, "src/main/java/a/Svc.java", cfg, "shop") == "shop-back/src/main/java/a/Svc.java"
    assert resolve_current_path(v, "src/main/java/a/Application.java", cfg, "shop") == \
        "shop-boot/src/main/java/a/Application.java"
    # webapp → static is a path *change* (the restructurer's move), not a module prefix: left as recorded
    assert resolve_current_path(v, "src/main/webapp/index.html", cfg, "shop") == "src/main/webapp/index.html"
    assert resolve_current_path(v, "src/main/java/a/Svc.java", cfg) == "src/main/java/a/Svc.java"   # no name → unchanged
    prof = type("P", (), {"root_pom": type("R", (), {"artifact_id": "shop"})(), "name": "x"})()
    assert project_name(prof) == "shop" and project_name(None) == ""
