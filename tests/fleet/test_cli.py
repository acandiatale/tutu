from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.fleet import runner as runner_mod
from sbmig.fleet.cli import fleet_cli

from .test_runner import report

REPO_CFG = "config/artifacts.local-e2e.yml"
cli = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch, repo_root, projects_dir):
    calls = []

    def fake_run(self, project, cfg, out_dir, run_id):
        calls.append((project.name, run_id, cfg.llm.provider))
        return report(
            project.name,
            success=project.name != "beta",
            aborted=project.name == "gamma" and getattr(self.cfg.fleet, "stop_on_abort", False),
        )

    monkeypatch.setattr(runner_mod.FleetRunner, "_default_run_project", fake_run)
    kb_cfg = tmp_path / "kb.yml"
    # no real preflight (mvn -v / LLM probe / disk) in unit tests: tests/fleet/test_infra.py covers it with fakes
    kb_cfg.write_text(f"kb: {{dir: {tmp_path / 'kb'}}}\nfleet: {{preflight: false, min_free_gb: 0}}\n")
    return {
        "calls": calls,
        "cfg": [str(repo_root / REPO_CFG), str(kb_cfg)],
        "projects": projects_dir,
        "out": tmp_path / "out",
    }


def _args(env, *extra):
    a = []
    for c in env["cfg"]:
        a += ["--config", c]
    return [*a, "--out", str(env["out"]), *extra]


def test_run_status_resume(env):
    r = cli.invoke(
        fleet_cli,
        ["run", str(env["projects"]), *_args(env), "--llm-provider", "fake"],
        catch_exceptions=False,
    )
    assert r.exit_code == 2, r.output  # beta incomplete
    assert [c[0] for c in env["calls"]] == ["alpha", "beta", "gamma"] and env["calls"][0][2] == "fake"
    assert "[2/3] beta: incomplete" in r.output and "| alpha |" in r.output
    saved = json.loads((env["out"] / "fleet" / "fleet-config.json").read_text())
    assert saved["overrides"] == {"llm": {"provider": "fake"}} and len(saved["configs"]) == 2

    r = cli.invoke(fleet_cli, ["status", str(env["out"])], catch_exceptions=False)
    assert r.exit_code == 0 and "3 of 3 done" in r.output and "incomplete 1" in r.output
    st = json.loads(cli.invoke(fleet_cli, ["status", str(env["out"]), "--json"]).output)
    assert [p["status"] for p in st["projects"]] == ["success", "incomplete", "success"]

    r = cli.invoke(fleet_cli, ["resume", str(env["out"])], catch_exceptions=False)
    assert r.exit_code == 2 and len(env["calls"]) == 3  # nothing re-run
    r = cli.invoke(fleet_cli, ["resume", str(env["out"]), "--retry-failed"], catch_exceptions=False)
    assert [c[0] for c in env["calls"]][3:] == ["beta"] and env["calls"][3][2] == "fake"


def test_dry_run_and_order(env):
    r = cli.invoke(
        fleet_cli,
        ["run", str(env["projects"]), *_args(env), "--dry-run", "--max-projects", "1"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0 and env["calls"] == []
    assert "dry run" in r.output and "alpha" in r.output and "-> run" in r.output
    assert (
        cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env), "--order", "bogus"]).exit_code != 0
    )


def test_stop_on_abort_exit_3(env):
    r = cli.invoke(
        fleet_cli, ["run", str(env["projects"]), *_args(env), "--stop-on-abort"], catch_exceptions=False
    )
    assert r.exit_code == 3 and [c[0] for c in env["calls"]] == ["alpha", "beta", "gamma"]


def test_all_success_exit_0_and_errors(env, tmp_path):
    single = tmp_path / "one.txt"
    single.write_text(str(env["projects"] / "alpha") + "\n")
    r = cli.invoke(fleet_cli, ["run", str(single), *_args(env)], catch_exceptions=False)
    assert r.exit_code == 0
    assert cli.invoke(fleet_cli, ["status", str(tmp_path / "nothing")]).exit_code == 1
    assert cli.invoke(fleet_cli, ["resume", str(tmp_path)]).exit_code == 1
    bad = tmp_path / "bad.yml"
    bad.write_text("projects: [{name: x}]\n")
    r = cli.invoke(fleet_cli, ["run", str(bad), *_args(env)])
    assert r.exit_code == 1 and "fleet failed" in r.output


def test_mounted_on_main_cli():
    from sbmig.cli import app

    r = cli.invoke(app, ["fleet", "--help"])
    assert r.exit_code == 0 and "status" in r.output


# ------------------------------------------------------------------ v2.1z: --fix-mode / --no-kb are gone
@pytest.mark.parametrize("opt", [["--fix-mode", "edits"], ["--no-kb"]])
def test_v21i_switches_are_gone(env, opt):
    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env), *opt])
    assert r.exit_code == 2 and not env["calls"]                      # typer: no such option


def test_fleet_runs_with_the_kb_and_an_old_fleet_config_still_resumes(env):
    """The fleet always uses the shared file KB; fleet-config.json has no `no_kb` key, and a v2.1i fleet-config.json
    (`no_kb`, a `fix.mode` override) still resumes — both keys are ignored."""
    from sbmig.loop import NullKnowledgeBase

    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env), "--llm-provider", "fake"],
                   catch_exceptions=False)
    assert r.exit_code == 2 and "fix_mode=" not in r.output and "kb=off" not in r.output
    path = env["out"] / "fleet" / "fleet-config.json"
    saved = json.loads(path.read_text())
    assert "no_kb" not in saved
    saved["no_kb"] = True
    saved["overrides"]["fix"] = {"mode": "edits"}
    path.write_text(json.dumps(saved))
    seen = []
    orig = runner_mod.FleetRunner._default_run_project

    def spy(self, project, cfg, out_dir, run_id):
        seen.append(self.kb)
        return orig(self, project, cfg, out_dir, run_id)

    runner_mod.FleetRunner._default_run_project = spy
    try:
        r = cli.invoke(fleet_cli, ["resume", str(env["out"]), "--retry-failed"], catch_exceptions=False)
    finally:
        runner_mod.FleetRunner._default_run_project = orig
    assert r.exit_code == 2 and [c[0] for c in env["calls"]][3:] == ["beta"]
    assert seen and not any(isinstance(kb, NullKnowledgeBase) for kb in seen)
    # v2.1z review follow-up: the dropped switches are named once (stderr), never silently
    assert "v2.1z: no_kb, overrides.fix.mode in fleet-config.json ignored" in r.output
    saved2 = json.loads(path.read_text())                       # the rewritten fleet-config carries neither key
    assert "no_kb" not in saved2 and "mode" not in (saved2.get("overrides") or {}).get("fix", {})


def test_an_old_state_settings_note_is_just_a_note(tmp_path):
    from sbmig.core.models import FleetState
    from sbmig.fleet.summary import render_learning, render_summary_md

    state = FleetState(fleet_id="fleet-x", manifest="m.txt", notes=["settings: fix_mode=edits kb=off"])
    md = render_summary_md(state)
    assert "* settings: fix_mode=edits kb=off" in md and "## Notes" in md
    assert "KB off" not in render_learning(state)
