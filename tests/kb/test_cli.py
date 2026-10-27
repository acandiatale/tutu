"""`sbmig kb` top-level commands after PLAN-v2 step 4: reindex, stats, validate, export / import, reviews; the v1
commands (list / show / promote / reject / ledger / symbols / learning / match, recipe validate) are gone."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.core.models import ReviewIssue, StructureReview
from sbmig.kb import FileKnowledgeBase, kb_cli, prepare_error_cases

from .helpers import ec_attempt, ec_error, with_kb

runner = CliRunner()


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase, ws, profile) -> None:
    errs, atts = prepare_error_cases([ec_error()], [ec_attempt()], profile, ws, kb.cfg)
    kb.append_error_cases(errs, atts)


@pytest.mark.parametrize("cmd", ["list", "show", "promote", "reject", "ledger", "symbols", "learning", "match"])
def test_v1_commands_are_gone(tmp_path, cmd):
    r = runner.invoke(kb_cli, ["--kb-dir", str(tmp_path / "kb"), cmd, "x"])
    assert r.exit_code != 0 and "No such command" in r.output


def test_reindex_stats_validate(kb, ws, profile):
    r = _run(kb.root, "reindex")
    assert r.exit_code == 0 and "nothing to index" in r.output
    _seed(kb, ws, profile)
    r = _run(kb.root, "reindex")
    assert r.exit_code == 0 and "case catalog: 1 case(s) (observed 1)" in r.output
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert (st["error_cases"], st["fix_attempts"], st["catalog_cases"]) == (1, 1, 1)
    r = _run(kb.root, "validate")
    assert r.exit_code == 0 and r.output.startswith("OK: 1 error case(s), 1 fix attempt(s), 1 case(s)")
    with open(kb.root / "fix_attempts.jsonl", "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    r = _run(kb.root, "validate")
    assert r.exit_code == 1 and "PROBLEM: fix_attempts.jsonl: line 2 does not validate as FixAttemptRecord" in r.output


def test_export_import(kb, kb_cfg, ws, profile, tmp_path):
    _seed(kb, ws, profile)
    out = tmp_path / "kb.zip"
    r = _run(kb.root, "export", str(out))
    assert r.exit_code == 0 and out.exists()
    other = tmp_path / "kb2"
    r = _run(other, "import", str(out))
    assert "error_cases_added=1" in r.output and "fix_attempts_added=1" in r.output
    assert "recipes_added" not in r.output and "cases_added=" not in r.output.replace("error_cases_added", "")
    r = _run(other, "import", str(out), "--replace")
    assert "error_cases_added=1" in r.output
    assert FileKnowledgeBase(with_kb(kb_cfg, dir=str(other))).stats()["catalog_cases"] == 1


def test_reviews(kb):
    r = _run(kb.root, "reviews")
    assert r.exit_code == 0 and "no structure reviews" in r.output
    kb.record_review(StructureReview(run_id="r1", project="alpha", verdict="issues",
                                     issues=[ReviewIssue(severity="critical", file="pom.xml", problem="no parent"),
                                             ReviewIssue(problem="old plugin")]))
    kb.record_review(StructureReview(run_id="r2", project="beta", verdict="ok"))
    r = _run(kb.root, "reviews")
    assert r.exit_code == 0 and "alpha" in r.output and "critical=1  warnings=1" in r.output
    assert "critical pom.xml: no parent" in r.output and "warning  -: old plugin" in r.output and "beta" in r.output
    r = _run(kb.root, "reviews", "--verdict", "^ok$", "--json")
    assert [json.loads(x)["project"] for x in r.output.splitlines()] == ["beta"]


def test_config_option(tmp_path, repo_root):
    kb_dir = tmp_path / "kbc"
    cfg_file = tmp_path / "site.yml"
    cfg_file.write_text(f"kb:\n  dir: {kb_dir}\n")
    r = runner.invoke(kb_cli, ["--config", str(repo_root / "config/artifacts.local-e2e.yml"),
                               "--config", str(cfg_file), "stats", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["kb_dir"] == str(kb_dir)
