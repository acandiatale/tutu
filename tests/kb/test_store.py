from __future__ import annotations

import json
from datetime import datetime, timezone

from sbmig.core.models import KBCase
from sbmig.kb import FileKnowledgeBase
from sbmig.kb.store import atomic_write, case_content_hash

from .helpers import L1, L2, make_case, security_change


def test_record_case_roundtrip_and_index(kb: FileKnowledgeBase):
    case = make_case(created_at=datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc))
    cid = kb.record_case(case)
    assert cid == f"20260309-{case_content_hash(case)}"
    path = kb.root / "cases" / "2026-03" / f"{cid}.json"
    assert path.exists()
    loaded = KBCase.model_validate_json(path.read_text())
    assert loaded.id == cid and loaded.changes == case.changes and loaded.summary == case.summary
    assert kb.get_case(cid) == loaded
    idx = json.loads((kb.root / "index.json").read_text())
    assert idx["version"] == 1
    assert idx["l1"][L1] == [cid] and idx["l2"][L2] == [cid]
    assert "updated_at" in idx and idx["recipes"] == {}
    # layout created on first use
    for d in ("patterns", "recipes/active", "recipes/candidates", "recipes/rejected"):
        assert (kb.root / d).is_dir()


def test_case_id_is_content_based(kb: FileKnowledgeBase):
    a = make_case()
    b = make_case(signatures=[L1, "compile|other|x|y"])
    c = make_case(changes=[security_change("other/Path.java")])
    assert len({case_content_hash(a), case_content_hash(b), case_content_hash(c)}) == 3
    assert case_content_hash(a) == case_content_hash(make_case(summary="different summary"))


def test_dedupe_bumps_reuse_and_keeps_first(kb: FileKnowledgeBase):
    first = make_case(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), project_fingerprint="fp-a")
    cid = kb.record_case(first)
    again = make_case(created_at=datetime(2026, 5, 1, tzinfo=timezone.utc), project_fingerprint="fp-b")
    cid2, created = kb.record_case_ex(again)
    assert cid2 == cid and created is False
    c = kb.get_case(cid)
    assert c.reuse_count == 1 and c.reuse_projects == ["fp-b"]
    assert c.created_at == first.created_at
    assert any(t.startswith("updated:") for t in c.tags)
    kb.record_case(make_case(project_fingerprint="fp-b"))
    c = kb.get_case(cid)
    assert c.reuse_count == 2 and c.reuse_projects == ["fp-b"]
    assert len([t for t in c.tags if t.startswith("updated:")]) == 1
    assert len(list(kb.iter_cases())) == 1


def test_reindex_rebuilds_from_files(kb: FileKnowledgeBase):
    ids = [kb.record_case(make_case(signatures=[f"compile|m|s{i}|l"], signatures_generic=[L2])) for i in range(3)]
    (kb.root / "index.json").unlink()
    fresh = FileKnowledgeBase(kb.cfg)
    assert fresh.reindex() == 3
    idx = json.loads((kb.root / "index.json").read_text())
    assert sorted(idx["l2"][L2]) == sorted(ids)
    assert idx["l1"]["compile|m|s1|l"] == [ids[1]]


def test_missing_or_corrupt_index_is_rebuilt_lazily(kb: FileKnowledgeBase):
    cid = kb.record_case(make_case())
    (kb.root / "index.json").write_text("{not json")
    fresh = FileKnowledgeBase(kb.cfg)
    assert fresh._load_index()["l1"][L1] == [cid]
    (kb.root / "index.json").write_text(json.dumps({"version": 99}))
    assert FileKnowledgeBase(kb.cfg)._load_index()["l1"][L1] == [cid]
    (kb.root / "index.json").unlink()
    assert FileKnowledgeBase(kb.cfg)._load_index()["l1"][L1] == [cid]


def test_unreadable_case_files_are_skipped(kb: FileKnowledgeBase):
    kb.record_case(make_case())
    bad = kb.root / "cases" / "2026-01" / "20260101-deadbeef00.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("garbage")
    assert len(list(kb.iter_cases())) == 1
    assert kb.get_case("20260101-deadbeef00") is None
    assert kb.get_case("nope") is None
    assert kb.reindex() == 1


def test_get_case_with_non_dated_id(kb: FileKnowledgeBase):
    c = make_case(id="manual-1")
    p = kb.root / "cases" / "2025-12" / "manual-1.json"
    p.parent.mkdir(parents=True)
    p.write_text(c.model_dump_json())
    assert kb.get_case("manual-1").id == "manual-1"
    assert kb.case_path("manual-1").parent.name == "misc"


def test_bump_case_reuse(kb: FileKnowledgeBase):
    cid = kb.record_case(make_case())
    assert kb.bump_case_reuse(cid, "fp-x").reuse_count == 1
    assert kb.bump_case_reuse(cid, "fp-x").reuse_projects == ["fp-x"]
    assert kb.bump_case_reuse("missing") is None


def test_atomic_write_leaves_no_temp(tmp_path):
    p = tmp_path / "a" / "b.json"
    atomic_write(p, "one")
    atomic_write(p, "two")
    assert p.read_text() == "two"
    assert [x.name for x in p.parent.iterdir()] == ["b.json"]


def test_atomic_write_cleans_up_on_failure(tmp_path, monkeypatch):
    import os

    import pytest

    p = tmp_path / "x.txt"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, "data")
    assert list(tmp_path.iterdir()) == []


def test_stats_on_empty_and_filled_kb(kb: FileKnowledgeBase):
    st = kb.stats()
    assert st["cases"] == 0 and st["recipes_active"] == 0
    kb.record_case(make_case())
    st = kb.stats()
    assert st["cases"] == 1 and st["signatures_l1"] == 1 and st["signatures_l2"] == 1
