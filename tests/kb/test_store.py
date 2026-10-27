"""FileKnowledgeBase after PLAN-v2 step 4: atomic writes, v2 stats, reindex = catalog rebuild, match = case hits,
validate, and the v1 files (cases/, recipes/, patterns/, index.json, ledger.jsonl, symbols.json, error_hints.json)
neither read, written nor exported."""
from __future__ import annotations

import json
import zipfile

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind
from sbmig.kb import FileKnowledgeBase, prepare_error_cases
from sbmig.kb.catalog import case_id_for
from sbmig.kb.store import atomic_write

from .helpers import EC_FILE, EC_L2, ec_attempt, ec_error, with_kb

CID = case_id_for(EC_L2)
V1_FILES = ("index.json", "ledger.jsonl", "symbols.json", "error_hints.json", "cases/2026-10/20261020-abc.json",
            "recipes/active/RenameFoo.yml", "patterns/p.yml")


def _seed(kb: FileKnowledgeBase, ws, profile) -> None:
    errs, atts = prepare_error_cases([ec_error()], [ec_attempt()], profile, ws, kb.cfg)
    assert kb.append_error_cases(errs, atts) == 2


def _cluster() -> ErrorCluster:
    e = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=EC_FILE, line=5,
                   symbol="method clientIp()", signature="compile|cannot find symbol|method clientIp()",
                   signature_generic=EC_L2)
    return ErrorCluster(id="c1", kind=e.kind, file=e.file, errors=[e], signatures=[e.signature])


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


def test_stats_on_empty_and_filled_kb(kb: FileKnowledgeBase, ws, profile):
    st = kb.stats()
    assert (st["error_cases"], st["fix_attempts"], st["catalog_cases"], st["script_versions"], st["reviews"]) == \
        (0, 0, 0, 0, 0)
    assert not {"cases", "recipes_active", "patterns", "symbols", "ledger_entries", "signatures_l1"} & set(st)
    _seed(kb, ws, profile)
    st = kb.stats()
    assert (st["error_cases"], st["fix_attempts"], st["catalog_cases"], st["catalog_observed"]) == (1, 1, 1, 1)


def test_append_writes_no_error_hints_index(kb: FileKnowledgeBase, ws, profile):
    _seed(kb, ws, profile)
    assert (kb.root / "errors.jsonl").exists() and (kb.root / "views" / "cases.md").exists()
    assert not (kb.root / "error_hints.json").exists()


def test_reindex_rebuilds_the_catalog(kb: FileKnowledgeBase, ws, profile, kb_cfg):
    assert kb.reindex() == 0                                  # nothing to build from
    _seed(kb, ws, profile)
    path = kb.catalog().path_of(CID)
    path.unlink()
    assert kb.reindex() == 1 and kb.catalog().path_of(CID).exists()
    assert FileKnowledgeBase(with_kb(kb_cfg, catalog=False)).reindex() == 0


def test_match_gives_case_hits_and_their_hints(kb: FileKnowledgeBase, ws, profile):
    assert kb.match(_cluster(), ws, profile).case_hits == []          # no KB yet
    _seed(kb, ws, profile)
    m = kb.match(_cluster(), ws, profile)
    assert [h.case_id for h in m.case_hits] == [CID] and not m.case_hits[0].pinned
    assert m.hints and all(h.startswith(f"[case {CID}]") for h in m.hints)
    kb.catalog().set_human(CID, hints=["check RequestWrapper first"], action={"kind": "block"})
    m = kb.match(_cluster(), ws, profile)
    assert m.hints[0] == f"[case {CID}] check RequestWrapper first"
    assert m.case_hits[0].pinned and m.case_hits[0].action.kind == "block"
    off = FileKnowledgeBase(with_kb(kb.cfg, catalog=False))
    assert off.match(_cluster(), ws, profile).case_hits == [] and off.match(_cluster(), ws, profile).hints == []


def test_v1_files_are_ignored_and_not_exported(kb: FileKnowledgeBase, ws, profile, tmp_path):
    for rel in V1_FILES:
        p = kb.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n" if rel.endswith((".json", ".jsonl")) else "x: 1\n", encoding="utf-8")
    _seed(kb, ws, profile)
    names = set(zipfile.ZipFile(kb.export_zip(tmp_path / "kb.zip")).namelist())
    assert {"errors.jsonl", "fix_attempts.jsonl", "views/cases.md"} <= names
    assert not names & set(V1_FILES)
    assert kb.validate() == []
    other = FileKnowledgeBase(with_kb(kb.cfg, dir=str(tmp_path / "kb2")))
    counters = other.import_zip(tmp_path / "kb.zip")
    assert counters["error_cases_added"] == 1 and counters["fix_attempts_added"] == 1
    assert not any((other.root / rel).exists() for rel in V1_FILES)


def test_validate_names_unreadable_lines_and_cases(kb: FileKnowledgeBase, ws, profile):
    _seed(kb, ws, profile)
    with open(kb.root / "errors.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"id": "broken", "status": 7}\n')
    (kb.root / "audit.jsonl").write_text('{"event": "human"}\nnot json\n', encoding="utf-8")
    path = kb.catalog().path_of(CID)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\nhuman:", "\nhuman: [unclosed", 1), encoding="utf-8")
    (kb.root / "catalog" / "index.json").write_text("{", encoding="utf-8")
    problems = kb.validate()
    assert "errors.jsonl: line 2 does not validate as ErrorCaseRecord" in problems
    assert "audit.jsonl: line 2 is no JSON object" in problems
    assert any("index.json: does not parse" in p for p in problems)
    assert any(p.endswith("the `human:` block is invalid (ignored until fixed)") for p in problems)
    json.loads((kb.root / "fix_attempts.jsonl").read_text(encoding="utf-8").splitlines()[0])   # untouched
