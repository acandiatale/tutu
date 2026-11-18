"""S3 (review 2026-11-18), the fleet side: a manifest's per-project override wins over an `SBMIG_<SECTION>__<KEY>`
environment value (one INFO line), while the environment still wins over the project's yml files."""
from __future__ import annotations

import logging
from pathlib import Path


class _Catch(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _catch():
    h = _Catch()
    logging.getLogger("sbmig.config").addHandler(h)
    logging.getLogger("sbmig.config").setLevel(logging.INFO)
    return h


def test_a_fleet_manifest_override_beats_the_environment(fleet_cfg, monkeypatch, tmp_path: Path):
    from sbmig.fleet import FleetRunner
    from sbmig.fleet.manifest import FleetProject

    monkeypatch.setenv("SBMIG_LOOP__MAX_TOTAL_MINUTES", "240")
    monkeypatch.setenv("SBMIG_BUILD__TIMEOUT_MINUTES", "30")
    site = tmp_path / "site.yml"
    site.write_text("build:\n  timeout_minutes: 99\n", encoding="utf-8")
    runner = FleetRunner(fleet_cfg, tmp_path, tmp_path / "out")
    proj = FleetProject(input=str(tmp_path), name="p", configs=[str(site)], overrides={"loop": {"max_total_minutes": 5}})
    h = _catch()
    try:
        cfg = runner.project_config(proj, None)
    finally:
        logging.getLogger("sbmig.config").removeHandler(h)
    assert cfg.loop.max_total_minutes == 5                          # the manifest's own value
    assert cfg.build.timeout_minutes == 30                          # env still beats the project's yml
    assert any("loop.max_total_minutes" in ln and "SBMIG_LOOP__MAX_TOTAL_MINUTES" in ln and "fleet project p" in ln
               for ln in h.lines)


def test_a_fleet_level_explicit_option_is_not_undone_by_a_project_yml(fleet_cfg, monkeypatch, tmp_path: Path):
    """`sbmig fleet --settings x` built the fleet config (explicit beats env); a project's own yml must not bring the
    environment value back for keys the yml does not set."""
    from sbmig.fleet import FleetRunner
    from sbmig.fleet.manifest import FleetProject

    monkeypatch.setenv("SBMIG_BUILD__SETTINGS_FILE", "/env/settings.xml")
    cfg = fleet_cfg.model_copy(update={"build": fleet_cfg.build.model_copy(update={"settings_file": "/cli/settings.xml"})})
    site = tmp_path / "site.yml"
    site.write_text("build:\n  timeout_minutes: 99\n", encoding="utf-8")
    runner = FleetRunner(cfg, tmp_path, tmp_path / "out")
    got = runner.project_config(FleetProject(input=str(tmp_path), name="p", configs=[str(site)]), None)
    assert got.build.settings_file == "/cli/settings.xml" and got.build.timeout_minutes == 99
