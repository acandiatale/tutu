"""Catalog S4: `detect_build_tool` (Maven only) and the zip root detection for Gradle / Ant builds."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.analyze import BuildToolDetection, analyze_path, detect_build_tool
from sbmig.analyze.analyzer import PathView
from sbmig.core.workspace import Workspace, _detect_project_root

from .helpers import pom, write


def det(root: Path) -> BuildToolDetection:
    return detect_build_tool(PathView(root))


def test_maven_root(tmp_path: Path) -> None:
    write(tmp_path, "pom.xml", pom("a"))
    d = det(tmp_path)
    assert d.tool == "maven" and d.ok and "ignored" not in d.reason


def test_maven_nested_only(tmp_path: Path) -> None:
    write(tmp_path, "svc-a/pom.xml", pom("a"))
    assert det(tmp_path).tool == "maven"


@pytest.mark.parametrize("name", ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"])
def test_gradle(tmp_path: Path, name: str) -> None:
    write(tmp_path, name, "")
    d = det(tmp_path)
    assert d.tool == "gradle" and not d.ok
    assert d.reason.startswith("Gradle build detected") and "only Maven" in d.reason


def test_gradle_nested(tmp_path: Path) -> None:
    write(tmp_path, "app/build.gradle.kts", "")
    assert det(tmp_path).tool == "gradle"


def test_ant(tmp_path: Path) -> None:
    write(tmp_path, "build.xml", "<project/>")
    d = det(tmp_path)
    assert d.tool == "ant" and d.reason.startswith("Ant build.xml detected")


def test_none(tmp_path: Path) -> None:
    write(tmp_path, "src/main/java/A.java", "class A {}")
    d = det(tmp_path)
    assert d.tool == "none" and d.reason.startswith("no pom.xml found")


def test_build_files_in_target_or_test_resources_do_not_count(tmp_path: Path) -> None:
    write(tmp_path, "target/pom.xml", pom("a"))
    write(tmp_path, "src/test/resources/fixture/pom.xml", pom("b"))
    write(tmp_path, "build.gradle", "")
    assert det(tmp_path).tool == "gradle"


def test_pom_wins_over_gradle_and_ant(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("a"))
    write(tmp_path, "build.gradle", "")
    write(tmp_path, "build.xml", "<project/>")
    d = det(tmp_path)
    assert d.tool == "maven" and "ignored" in d.reason
    assert "Gradle" in d.reason and "Ant" in d.reason
    p = analyze_path(tmp_path, cfg)
    assert p.build_tool == "maven" and d.reason in p.warnings


def test_analyzer_uses_detection_reason_first(tmp_path: Path, cfg) -> None:
    write(tmp_path, "settings.gradle.kts", "")
    p = analyze_path(tmp_path, cfg)
    assert p.build_tool == "gradle" and p.warnings[0] == det(tmp_path).reason


# ------------------------------------------------------------------ Workspace._detect_project_root
@pytest.mark.parametrize("name", ["build.gradle", "settings.gradle.kts", "build.xml", "pom.xml"])
def test_detect_root_descends_into_single_folder(tmp_path: Path, name: str) -> None:
    write(tmp_path, f"wrapper/proj/{name}", "")
    write(tmp_path, "wrapper/proj/sub/x.txt", "")
    assert _detect_project_root(tmp_path / "wrapper") == tmp_path / "wrapper/proj"


@pytest.mark.parametrize("name,tool", [("build.gradle", "gradle"), ("build.xml", "ant")])
def test_zip_with_single_top_folder_lands_on_build_root(tmp_path: Path, name: str, tool: str) -> None:
    z = tmp_path / "in.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"proj/{name}", "")
        zf.writestr("proj/lib/only/x.txt", "x")
    ws = Workspace.from_input(z, tmp_path / "out", run_id="r1")
    assert (ws.root / name).exists()
    d = detect_build_tool(ws)
    assert d.tool == tool
