"""v2.1i switches on `sbmig fleet run`: `--fix-mode script|edits` (config override) and `--no-kb` (every project runs
with a NullKnowledgeBase); state.json / summary.md / `fleet status` name them so two comparison fleets are told apart."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.core.models import FleetState
from sbmig.fleet import FleetRunner
from sbmig.fleet import runner as runner_mod
from sbmig.fleet.cli import fleet_cli
from sbmig.fleet.summary import kb_off, render_learning, render_summary_md, settings_label, settings_note
from sbmig.loop import NullKnowledgeBase

from .test_runner import FakeRun, _state, report

REPO_CFG = "config/artifacts.local-e2e.yml"
cli = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch, repo_root, projects_dir):
    seen = []

    def fake_run(self, project, cfg, out_dir, run_id):
        seen.append({"name": project.name, "fix_mode": cfg.fix.mode, "kb": self.kb})
        return report(project.name)

    monkeypatch.setattr(runner_mod.FleetRunner, "_default_run_project", fake_run)
    kb_cfg = tmp_path / "kb.yml"
    kb_cfg.write_text(f"kb: {{dir: {tmp_path / 'kb'}}}\nfleet: {{preflight: false, min_free_gb: 0}}\n")
    return {"seen": seen, "cfg": [str(repo_root / REPO_CFG), str(kb_cfg)], "projects": projects_dir,
            "out": tmp_path / "out"}


def _args(env, *extra):
    a = []
    for c in env["cfg"]:
        a += ["--config", c]
    return [*a, "--out", str(env["out"]), *extra]


def test_fix_mode_edits_and_no_kb_reach_every_project_and_the_summaries(env):
    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env, "--fix-mode", "edits", "--no-kb")],
                   catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert [s["name"] for s in env["seen"]] == ["alpha", "beta", "gamma"]
    assert all(s["fix_mode"] == "edits" and isinstance(s["kb"], NullKnowledgeBase) for s in env["seen"])
    lines = r.output.splitlines()                       # (other tests may leave a console log handler: filter)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("fleet ") and ln.endswith(": fix_mode=edits kb=off"))
    assert start < next(i for i, ln in enumerate(lines) if ln.startswith("[1/3] alpha"))   # before the first project

    fleet = env["out"] / "fleet"
    saved = json.loads((fleet / "fleet-config.json").read_text())
    assert saved["overrides"] == {"fix": {"mode": "edits"}} and saved["no_kb"] is True
    st = _state(env["out"])
    assert st.notes == ["settings: fix_mode=edits kb=off"]
    md = (fleet / "summary.md").read_text(encoding="utf-8")
    head = md.split("\n\n| #")[0]
    assert "* settings: fix_mode=edits kb=off" in head and "## Notes" not in md
    assert "KB off (`--no-kb`)" in (fleet / "learning.md").read_text(encoding="utf-8")
    assert not (env["out"].parent / "kb").exists()                      # nothing was written into the KB

    r = cli.invoke(fleet_cli, ["status", str(env["out"])], catch_exceptions=False)
    assert r.exit_code == 0 and any(ln.startswith("fleet ") and "(fix_mode=edits kb=off): 3 of 3 done" in ln
                                    for ln in r.output.splitlines())
    assert "note: settings" not in r.output


def test_default_fleet_records_script_and_kb_on(env):
    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env)], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert all(s["fix_mode"] == "script" and not isinstance(s["kb"], NullKnowledgeBase) for s in env["seen"])
    saved = json.loads((env["out"] / "fleet" / "fleet-config.json").read_text())
    assert saved["overrides"] == {} and saved["no_kb"] is False
    assert _state(env["out"]).notes == ["settings: fix_mode=script kb=on"]
    assert "* settings: fix_mode=script kb=on" in (env["out"] / "fleet" / "summary.md").read_text(encoding="utf-8")


def test_resume_keeps_the_switches(env):
    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env, "--fix-mode", "edits", "--no-kb",
                                                                   "--max-projects", "1")], catch_exceptions=False)
    assert r.exit_code == 2 and len(env["seen"]) == 1
    # the saved overrides come back (max_projects=1 too: lifted here), --no-kb from fleet-config.json
    r = cli.invoke(fleet_cli, ["resume", str(env["out"]), "--max-projects", "0"], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert [s["name"] for s in env["seen"]] == ["alpha", "beta", "gamma"]
    assert all(s["fix_mode"] == "edits" and isinstance(s["kb"], NullKnowledgeBase) for s in env["seen"])
    assert _state(env["out"]).notes.count("settings: fix_mode=edits kb=off") == 1      # same settings: one note


def test_invalid_fix_mode_is_rejected(env):
    r = cli.invoke(fleet_cli, ["run", str(env["projects"]), *_args(env, "--fix-mode", "patch")])
    assert r.exit_code == 2 and env["seen"] == []
    assert "script | edits" in r.output


def test_runner_no_kb_overrides_an_injected_kb(fleet_cfg, projects_dir, tmp_path):
    class KB:
        cases = None

    fr = FleetRunner(fleet_cfg, projects_dir, tmp_path / "out", run_project=FakeRun(), kb=KB(), no_kb=True)
    assert isinstance(fr.kb, NullKnowledgeBase) and fr.settings_label() == "fix_mode=script kb=off"
    plain = FleetRunner(fleet_cfg, projects_dir, tmp_path / "out2", run_project=FakeRun())
    assert plain.no_kb is False and plain.settings_label() == "fix_mode=script kb=on"


def test_settings_changed_on_resume_are_both_shown(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    FleetRunner(fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update={"max_projects": 1})}),
                projects_dir, out, run_project=FakeRun()).run()
    edits = fleet_cfg.model_copy(update={"fix": fleet_cfg.fix.model_copy(update={"mode": "edits"})})
    state = FleetRunner(edits, projects_dir, out, run_project=FakeRun(), no_kb=True).run()
    assert [n for n in state.notes if n.startswith("settings: ")] == ["settings: fix_mode=script kb=on",
                                                                       "settings: fix_mode=edits kb=off"]
    assert settings_label(state) == "fix_mode=script kb=on → fix_mode=edits kb=off" and kb_off(state)
    assert "(changed on resume: the projects ran under different settings)" in render_summary_md(state)


def test_a_state_without_settings_renders_as_before(tmp_path):
    state = FleetState(fleet_id="fleet-x", manifest="m.txt", notes=["max_projects=1 reached"])
    md = render_summary_md(state)
    assert "* settings:" not in md and "* max_projects=1 reached" in md
    assert settings_label(state) == "" and not kb_off(state)
    assert "KB off" not in render_learning(state)
    assert settings_note("edits", False) == "settings: fix_mode=edits kb=off"
