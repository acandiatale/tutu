"""javac compiles the generated domain-module classes (no third-party jars needed): A6, A10 and hostile strings."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_javagen import (
    a6_resource,
    hostile_resource,
    make_layout,
    make_rules,
    mapper_resource,
    stub_resource,
)

from sbmig.fwconvert.javagen import generate_resource, write_generated

pytestmark = pytest.mark.integration


def _javac() -> str | None:
    home = os.environ.get("JAVA21_HOME", "").strip()
    if home and (Path(home) / "bin" / "javac").is_file():
        return str(Path(home) / "bin" / "javac")
    return shutil.which("javac")


def test_domain_classes_compile(tmp_path: Path):
    javac = _javac()
    if not javac:
        pytest.skip("no javac (set JAVA21_HOME or put javac on PATH)")
    lay = make_layout()
    files = []
    for res in (a6_resource(), stub_resource(), mapper_resource(), hostile_resource()):
        generated, _ = generate_resource(res, lay, make_rules())
        files += [f for f in generated if f.path.startswith(f"{lay.identity.modules.domain}/")]
    names = sorted(f.path.rsplit("/", 1)[-1] for f in files)
    for x in ("Order", "Notice"):
        assert {f"{x}Service.java", f"{x}Store.java", f"{x}Logic.java"} <= set(names)
    src = tmp_path / "src"
    assert write_generated(src, files) == []
    sources = sorted(str(p) for p in src.rglob("*.java"))
    out = tmp_path / "classes"
    proc = subprocess.run([javac, "--release", "21", "-encoding", "UTF-8", "-Xlint:all", "-Werror",
                           "-d", str(out), *sources], check=False, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    classes = {p.name for p in out.rglob("*.class")}
    assert {"OrderLogic.class", "NoticeLogic.class", "WeirdLogic.class"} <= classes
