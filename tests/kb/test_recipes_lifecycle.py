from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from sbmig.core.models import KBRecipe
from sbmig.kb import FileKnowledgeBase

from .helpers import L1, make_case, recipe_yaml, with_kb


def _cand(kb, name="RenameFooToBar", sigs=(L1,), status="candidate"):
    return kb.add_recipe(recipe_yaml(name), KBRecipe(id=name, status=status, path="", signatures=list(sigs)))


def test_add_and_list(kb):
    r = _cand(kb)
    assert Path(r.path) == kb.root / "recipes" / "candidates" / f"{r.id}.yml"
    assert (kb.root / "recipes" / "candidates" / f"{r.id}.meta.json").exists()
    assert [x.id for x in kb.list_recipes("candidate")] == [r.id]
    assert kb.list_recipes("active") == []
    with pytest.raises(ValueError):
        kb.list_recipes("bogus")
    idx = json.loads((kb.root / "index.json").read_text())
    assert idx["recipes"][L1] == [r.id]


def test_promote_and_reject(kb):
    r = _cand(kb)
    p = kb.promote(r.id)
    assert p.status == "active" and p.promoted_at is not None
    assert Path(p.path).parent.name == "active" and Path(p.path).exists()
    assert not (kb.root / "recipes" / "candidates" / f"{r.id}.yml").exists()
    assert not (kb.root / "recipes" / "candidates" / f"{r.id}.meta.json").exists()
    assert kb.active_recipe_files() == [Path(p.path)]
    assert kb.active_recipe_ids() == [r.id]
    assert kb.promote(r.id).status == "active"          # idempotent
    rej = kb.reject(r.id, "too broad")
    assert rej.status == "rejected"
    assert kb.recipe_meta_extra(r.id)["rejected_reason"] == "too broad"
    assert kb.active_recipe_files() == []
    idx = json.loads((kb.root / "index.json").read_text())
    assert L1 not in idx["recipes"]
    # rejected -> active again keeps the extra meta
    again = kb.promote(r.id)
    assert again.status == "active" and kb.recipe_meta_extra(r.id)["rejected_reason"] == "too broad"
    with pytest.raises(KeyError):
        kb.promote("Missing")
    with pytest.raises(KeyError):
        kb.reject("Missing")


def test_active_recipe_ids_parses_multi_doc_and_hand_added(kb):
    kb.ensure_layout()
    (kb.root / "recipes" / "active" / "hand.yml").write_text(
        recipe_yaml("First") + "---\n" + recipe_yaml("Second"))
    (kb.root / "recipes" / "active" / "broken.yml").write_text("a: [x")
    ids = kb.active_recipe_ids()
    assert ids == ["First", "Second"]   # broken.yml skipped
    assert [p.name for p in kb.active_recipe_files()] == ["hand.yml"]
    assert kb.get_recipe("broken").status == "active"       # still listed (id falls back to the stem)
    rec = kb.get_recipe("First")
    assert rec.status == "active" and rec.path.endswith("hand.yml")
    assert kb.get_recipe("hand").id == "First"


def test_unreadable_meta_falls_back(kb):
    r = _cand(kb)
    kb._meta_path(Path(r.path)).write_text("{bad")
    assert kb.get_recipe(r.id).id == r.id
    kb._meta_path(Path(r.path)).write_text(json.dumps({"id": r.id, "success_count": "x"}))
    assert kb.get_recipe(r.id) is None


def test_credit_and_auto_promote(kb):
    r = _cand(kb)
    kb.credit_recipe(r.id, True, "p1")
    kb.credit_recipe(r.id, True, "p1")
    cur = kb.get_recipe(r.id)
    assert cur.status == "candidate" and cur.success_count == 2 and cur.projects == ["p1"]
    kb.credit_recipe(r.id, True, "p2")                       # 2 distinct projects (default threshold 2)
    cur = kb.get_recipe(r.id)
    assert cur.status == "active" and cur.success_count == 3 and cur.promoted_at is not None
    kb.credit_recipe("Unknown", True, "p1")   # ignored


def test_no_auto_promote_after_failure(kb):
    r = _cand(kb)
    kb.credit_recipe(r.id, False, "p0")
    kb.credit_recipe(r.id, True, "p1")
    kb.credit_recipe(r.id, True, "p2")
    cur = kb.get_recipe(r.id)
    assert cur.status == "candidate" and cur.fail_count == 1 and cur.projects == ["p1", "p2"]


def test_auto_promote_disabled(kb_cfg):
    kb = FileKnowledgeBase(with_kb(kb_cfg, auto_promote_after_projects=0))
    r = _cand(kb)
    for p in ("a", "b", "c"):
        kb.credit_recipe(r.id, True, p)
    assert kb.get_recipe(r.id).status == "candidate"


def test_export_import_merge(kb, kb_cfg, tmp_path):
    cid = kb.record_case(make_case())
    _cand(kb)
    kb.ensure_layout()
    (kb.root / "patterns" / "p.yml").write_text("match: {signature_regex: x}\n")
    out = kb.export_zip(tmp_path / "export" / "kb.zip")
    names = zipfile.ZipFile(out).namelist()
    assert "index.json" in names and any(n.startswith("cases/") for n in names)
    assert any(n.endswith(".meta.json") for n in names) and "patterns/p.yml" in names

    other = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb2")))
    other_cid = other.record_case(make_case(signatures=["compile|x"], signatures_generic=["compile|x"]))
    _cand(other, "OtherRecipe", sigs=("compile|x",), status="active")
    # same case + recipe present locally with different counters
    other.record_case(make_case(project_fingerprint="zz"))
    other.credit_recipe
    counters = other.import_zip(out, merge=True)
    assert counters["cases_added"] == 0 and counters["cases_merged"] == 1
    assert counters["recipes_added"] == 1 and counters["patterns_added"] == 1
    assert {c.id for c in other.iter_cases()} == {cid, other_cid}
    assert other.get_recipe("RenameFooToBar").status == "candidate"
    assert other.get_recipe("OtherRecipe").status == "active"
    idx = json.loads((other.root / "index.json").read_text())
    assert idx["l1"][L1] == [cid] and idx["recipes"][L1] == ["RenameFooToBar"]

    # merge again: recipe now merged, local status wins, counters merged
    kb.credit_recipe("RenameFooToBar", True, "proj-z")
    out2 = kb.export_zip(tmp_path / "kb-2.zip")
    other.promote("RenameFooToBar")
    counters = other.import_zip(out2)
    assert counters["recipes_merged"] == 1
    r = other.get_recipe("RenameFooToBar")
    assert r.status == "active" and r.success_count == 1 and r.projects == ["proj-z"]


def test_import_replace(kb, kb_cfg, tmp_path):
    kb.record_case(make_case())
    out = kb.export_zip(tmp_path / "kb.zip")
    other = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb2")))
    other.record_case(make_case(signatures=["compile|x"]))
    other.import_zip(out, merge=False)
    assert other.stats()["cases"] == 1
    assert [c.signatures for c in other.iter_cases()] == [[L1]]


def test_import_rejects_zip_slip(kb, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        kb.import_zip(bad)


def test_export_empty_kb(kb, tmp_path):
    out = kb.export_zip(tmp_path / "empty.zip")
    assert zipfile.ZipFile(out).namelist() == []
