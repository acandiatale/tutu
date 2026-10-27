"""sbmig.kb.store.FileKnowledgeBase (v2.1): the facade over sbmig.errors.CaseStore — match (stored script / pin /
nothing), record_run, reindex, stats, validate, export / import (merge, replace with a backup)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.core.models import KBMatch
from sbmig.kb import FileKnowledgeBase, atomic_write
from sbmig.kb.lock import LOCK_NAME

from .helpers import CASE, L2, entry, make_cluster, make_error

CODE = "def fix(p):\n    p.log('x')\n"


def test_match_on_an_empty_kb_creates_nothing(kb: FileKnowledgeBase):
    m = kb.match(make_cluster(), None, None)
    assert isinstance(m, KBMatch) and m.case_hits == [] and m.hints == [] and not kb.root.exists()


def test_match_gives_the_best_script_and_the_last_solution(kb: FileKnowledgeBase):
    kb.record_run([entry("e1")])
    m = kb.match(make_cluster(), None, None)
    assert m.case_hits == [] and m.hints and "use a SecurityFilterChain bean" in m.hints[0]   # no script: hint only
    kb.cases.add_script(CASE, L2, CODE, run_id="r1", project="demo")
    m = kb.match(make_cluster(), None, None)
    (hit,) = m.case_hits
    assert (hit.case_id, hit.status, hit.action.kind, hit.action.script_version, hit.pinned) == \
        (CASE, "candidate", "script", 1, False)
    assert hit.errors == make_cluster().errors
    kb.cases.credit(CASE, 1, "improved", project="other")
    assert kb.match(make_cluster(), None, None).case_hits[0].status == "active"
    other = make_error(l2="compile|cannot_find_symbol|Nope", file="B.java")
    assert kb.match(make_cluster(other), None, None).case_hits == []


def test_a_block_pin_is_a_pinned_hit_the_triage_obeys(kb: FileKnowledgeBase):
    from sbmig.triage.decide import DecisionEngine  # noqa: F401 - the consumer exists

    kb.cases.add_script(CASE, L2, CODE)
    kb.cases.set_pin(CASE, "block:needs a person")
    (hit,) = kb.match(make_cluster(), None, None).case_hits
    assert hit.pinned and hit.action.kind == "block" and hit.action.block_reason == "needs a person"
    kb.cases.set_pin(CASE, "block")
    assert kb.match(make_cluster(), None, None).case_hits[0].action.block_reason == ""


def test_match_never_raises(kb: FileKnowledgeBase, monkeypatch):
    kb.cases.add_script(CASE, L2, CODE)
    monkeypatch.setattr(type(kb.cases), "find", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert kb.match(make_cluster(), None, None).case_hits == []


def test_record_run_reindex_stats_validate(kb: FileKnowledgeBase, monkeypatch):
    assert kb.reindex() == 0 and not kb.root.exists()
    assert kb.record_run([entry("e1"), entry("e2", status="unresolved", tries=[])]) == 2
    st = kb.stats()
    assert (st["errors"], st["cases"], st["script_versions"]) == (2, 1, 0)
    assert kb.reindex() == 1 and kb.cases.md_path.is_file() and kb.validate() == []
    monkeypatch.setattr(type(kb.cases), "record_run", lambda *a: (_ for _ in ()).throw(OSError("full")))
    assert kb.record_run([entry("e3")]) == 0                     # never raises


def test_export_import_merge_and_replace(kb_cfg, tmp_path: Path):
    other = FileKnowledgeBase(kb_cfg, kb_dir=tmp_path / "other")
    other.record_run([entry("o1", run_id="ro", project="theirs")])
    other.cases.add_script(CASE, L2, CODE, project="theirs")
    (other.root / "junk.txt").write_text("not exported")
    zp = other.export_zip(tmp_path / "other.zip")
    names = zipfile.ZipFile(zp).namelist()
    assert set(names) == {"errors.jsonl", "cases.md", f"cases/{CASE}/case.json", f"cases/{CASE}/fix_v1.py",
                          f"cases/{CASE}/errors/ro-o1.json"}          # v2.1c: the readable per-error file travels too

    local = FileKnowledgeBase(kb_cfg)
    local.record_run([entry("l1")])
    c = local.import_zip(zp)
    assert c["errors_added"] == 1 and c["scripts_added"] == 1
    assert local.import_zip(zp)["errors_added"] == 0              # idempotent
    case = local.cases.find(CASE)
    assert case.seen == 2 and sorted(case.projects) == ["demo", "theirs"] and len(case.scripts) == 1

    (local.root / "mine.txt").write_text("mine")
    c = local.import_zip(zp, merge=False)
    backups = sorted(local.root.parent.glob("kb.bak-*"))
    assert len(backups) == 1 and c["backup"] == str(backups[0]) and (backups[0] / "mine.txt").read_text() == "mine"
    assert [e.id for e in local.cases.iter_errors()] == ["o1"] and not (local.root / "mine.txt").exists()
    assert not (local.root / LOCK_NAME).exists() and not (backups[0] / LOCK_NAME).exists()
    assert not list(local.root.parent.glob(".kb.import-*"))


def test_a_failed_replace_leaves_the_kb_untouched(kb_cfg, tmp_path: Path, monkeypatch):
    other = FileKnowledgeBase(kb_cfg, kb_dir=tmp_path / "other")
    other.record_run([entry("o1")])
    zp = other.export_zip(tmp_path / "o.zip")
    local = FileKnowledgeBase(kb_cfg)
    local.record_run([entry("l1")])
    monkeypatch.setattr(FileKnowledgeBase, "_import_into",
                        staticmethod(lambda kb, path: (_ for _ in ()).throw(RuntimeError("disk full"))))
    with pytest.raises(RuntimeError):
        local.import_zip(zp, merge=False)
    assert [e.id for e in local.cases.iter_errors()] == ["l1"]
    assert not list(local.root.parent.glob("kb.bak-*")) and not list(local.root.parent.glob(".kb.import-*"))


def test_export_of_a_missing_kb_is_empty_and_zip_slip_is_blocked(kb: FileKnowledgeBase, tmp_path: Path):
    assert zipfile.ZipFile(kb.export_zip(tmp_path / "empty.zip")).namelist() == []
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        kb.import_zip(evil)


def test_atomic_write_is_reexported(tmp_path: Path):
    atomic_write(tmp_path / "a" / "b.txt", "한글\n")
    assert (tmp_path / "a" / "b.txt").read_text(encoding="utf-8") == "한글\n"
