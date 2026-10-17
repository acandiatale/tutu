from __future__ import annotations

import json

from typer.testing import CliRunner

from sbmig.core.models import KBRecipe
from sbmig.kb import FileKnowledgeBase, kb_cli

from .helpers import L1, VALID_RECIPE, make_case, recipe_yaml, with_kb

runner = CliRunner()


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase):
    cid = kb.record_case(make_case())
    kb.add_recipe(recipe_yaml(), KBRecipe(id="com.company.kb.RenameFooToBar", path="", signatures=[L1],
                                          description="rename"))
    return cid


def test_list_show_promote_reject(kb):
    cid = _seed(kb)
    r = _run(kb.root, "list")
    assert r.exit_code == 0 and "candidate com.company.kb.RenameFooToBar" in r.output
    r = _run(kb.root, "list", "--status", "active")
    assert "no recipes" in r.output
    r = _run(kb.root, "list", "--json")
    assert json.loads(r.output)[0]["id"] == "com.company.kb.RenameFooToBar"
    r = _run(kb.root, "list", "--cases")
    assert cid in r.output
    assert json.loads(_run(kb.root, "list", "--cases", "--json").output)[0]["id"] == cid
    assert _run(kb.root, "list", "--status", "bogus").exit_code != 0

    r = _run(kb.root, "show", cid)
    assert json.loads(r.output)["id"] == cid
    r = _run(kb.root, "show", "com.company.kb.RenameFooToBar")
    assert "ChangeType" in r.output and '"status": "candidate"' in r.output
    assert _run(kb.root, "show", "nope").exit_code == 1

    r = _run(kb.root, "promote", "com.company.kb.RenameFooToBar")
    assert r.exit_code == 0 and "promoted" in r.output
    assert kb.get_recipe("com.company.kb.RenameFooToBar").status == "active"
    assert _run(kb.root, "promote", "missing").exit_code == 1
    r = _run(kb.root, "reject", "com.company.kb.RenameFooToBar", "--reason", "noisy")
    assert r.exit_code == 0 and kb.get_recipe("com.company.kb.RenameFooToBar").status == "rejected"
    assert "noisy" in _run(kb.root, "show", "com.company.kb.RenameFooToBar").output
    assert _run(kb.root, "reject", "missing").exit_code == 1


def test_reindex_stats_match(kb):
    cid = _seed(kb)
    r = _run(kb.root, "reindex")
    assert "indexed 1 cases" in r.output
    r = _run(kb.root, "stats")
    assert "cases" in r.output and "recipes_candidate" in r.output
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert st["cases"] == 1 and st["recipes_candidate"] == 1
    m = json.loads(_run(kb.root, "match", L1).output)
    assert m["cases"] == [cid] and m["candidate_recipe_ids"] == ["com.company.kb.RenameFooToBar"]
    assert m["replayable"] == []
    m = json.loads(_run(kb.root, "match", "weird").output)
    assert m["cases"] == []


def test_match_with_project(kb, ws):
    cid = _seed(kb)
    m = json.loads(_run(kb.root, "match", L1, "--project", str(ws.root)).output)
    assert m["replayable"] == [cid]


def test_export_import(kb, kb_cfg, tmp_path):
    _seed(kb)
    out = tmp_path / "kb.zip"
    r = _run(kb.root, "export", str(out))
    assert r.exit_code == 0 and out.exists()
    other = tmp_path / "kb2"
    r = _run(other, "import", str(out))
    assert "cases_added=1" in r.output and "recipes_added=1" in r.output
    r = _run(other, "import", str(out), "--replace")
    assert "cases_added=1" in r.output
    assert FileKnowledgeBase(with_kb(kb_cfg, dir=str(other))).stats()["cases"] == 1


def test_validate(tmp_path):
    good = tmp_path / "good.yml"
    good.write_text(VALID_RECIPE)
    bad = tmp_path / "bad.yml"
    bad.write_text(VALID_RECIPE.replace("org.openrewrite.java.ChangeType", "org.evil.Run"))
    r = runner.invoke(kb_cli, ["validate", str(good)])
    assert r.exit_code == 0 and "OK" in r.output
    r = runner.invoke(kb_cli, ["validate", str(bad)])
    assert r.exit_code == 1 and "not allowed" in r.output


def test_config_option(tmp_path, repo_root):
    kb_dir = tmp_path / "kbc"
    cfg_file = tmp_path / "site.yml"
    cfg_file.write_text(f"kb:\n  dir: {kb_dir}\n")
    r = runner.invoke(kb_cli, ["--config", str(repo_root / "config/artifacts.local-e2e.yml"),
                               "--config", str(cfg_file), "stats", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["kb_dir"] == str(kb_dir)
