"""W3 (review 2026-11-18): a fleet project given as a ZIP over `intake.max_*` ends like an intake refusal (aborted, the
reason in the report), nothing unpacked, no Maven / LLM."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from sbmig.fleet import FleetRunner

MIB = 1024 * 1024


def test_a_fleet_zip_over_the_limits_is_refused_at_intake(fleet_cfg, tmp_path: Path):
    cfg = fleet_cfg.model_copy(update={"intake": fleet_cfg.intake.model_copy(update={"max_unpacked_mb": 1})})
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("big/pom.xml", "<project/>")
        zf.writestr("big/a.bin", os.urandom(2 * MIB))
    manifest = tmp_path / "projects"
    manifest.mkdir()
    runner = FleetRunner(cfg, manifest, tmp_path / "out")
    proj = type("P", (), {"input": str(z), "name": "big"})()
    report = runner._default_run_project(proj, cfg, tmp_path / "out", "fl-001-big")
    assert "aborted" in report.flags
    assert any(f.startswith("run aborted: ZIP refused: it unpacks to 2 MiB") for f in report.manual_followups)
    assert not (tmp_path / "out" / "fl-001-big" / "project" / "a.bin").exists()
