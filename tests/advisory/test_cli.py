"""`sbmig advisories list | show | check`."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from sbmig.advisory import advisories_cli
from tests.advisory.helpers import write_advisories

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_list_show_check_on_the_repo_file():
    r = runner.invoke(advisories_cli, ["list"])
    assert r.exit_code == 0, r.output
    assert "springfox" in r.output and "hystrix" in r.output and "SpringFoxToSpringDoc" not in r.output
    assert "knowledge/notes/spring-cloud.md" in r.output                     # the notes column
    r = runner.invoke(advisories_cli, ["list", "--json"])
    rows = json.loads(r.output)
    assert {x["state"] for x in rows} == {"guide"}                           # guide-only: LLM hints + Handover
    assert not any("policy" in x or "recipes" in x or "effective_policy" in x for x in rows)
    r = runner.invoke(advisories_cli, ["show", "sleuth"])
    assert r.exit_code == 0 and "micrometer-tracing-bridge-brave" in r.output and "state: guide" in r.output
    assert json.loads(runner.invoke(advisories_cli, ["show", "zuul", "--json"]).output)["status"] == "eol"
    r = runner.invoke(advisories_cli, ["show", "nope"])
    assert r.exit_code == 1
    r = runner.invoke(advisories_cli, ["check"])
    assert r.exit_code == 0 and "advisories OK: 67 advisory(ies), guide-only" in r.output


def test_config_override_and_failing_check(tmp_path: Path):
    # an old company file may still carry `policy: recipe` / `recipes:` — loaded, ignored
    bad = write_advisories(tmp_path / "bad.yml", [{"id": "x", "library": "nope", "title": "X", "policy": "recipe",
                                                   "recipes": ["org.example.Gone"], "notes": "knowledge/notes/no.md"}])
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump({"advisories": {"extra_files": [str(bad)],
                                                  "policy_overrides": {"springfox": "guide", "zuul": "off"}}}))
    r = runner.invoke(advisories_cli, ["list", "-c", str(cfg)])
    assert r.exit_code == 0
    assert " guide " in next(x for x in r.output.splitlines() if x.startswith("springfox"))
    assert " off " in next(x for x in r.output.splitlines() if x.startswith("zuul "))
    r = runner.invoke(advisories_cli, ["check", "--config", str(cfg)])
    assert r.exit_code == 1
    assert "PROBLEM advisory x: unknown library 'nope'" in r.output
    assert "PROBLEM advisory x: notes file knowledge/notes/no.md does not exist" in r.output
    assert "recipe" not in r.output
