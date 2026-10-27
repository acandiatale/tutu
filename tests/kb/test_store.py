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


# ------------------------------------------------------------------------------------------ v2.1e
def test_the_store_keeps_errors_script_runs_records_per_version(kb_cfg):
    cfg = kb_cfg.model_copy(update={"errors": kb_cfg.errors.model_copy(update={"script_runs": 2})})
    kb = FileKnowledgeBase(cfg)
    assert kb.cases.script_runs == 2 and FileKnowledgeBase(kb_cfg).cases.script_runs == 30
    kb.cases.add_script(CASE, L2, CODE, run_id="r1", project="demo")
    for n in (1, 2, 3):
        kb.cases.credit(CASE, 1, "no_change", project="demo", run_id="r1", round=n, n=n, reason=f"no_change: {n}")
    assert [r.n for r in kb.cases.find(CASE).scripts[0].runs] == [2, 3]


def _cli(kb, *args):
    from typer.testing import CliRunner

    from sbmig.kb import kb_cli

    res = CliRunner().invoke(kb_cli, list(args), obj={"kb": kb}, catch_exceptions=False)
    return res.exit_code, res.output


def test_cases_show_prints_lineage_reasons_and_runs(kb: FileKnowledgeBase):
    from sbmig.core.models import FixTry

    kb.cases.add_script(CASE, L2, CODE, run_id="r1", project="demo")
    kb.cases.add_script(CASE, L2, CODE + "# v2\n", source="llm-augment", run_id="r1", project="demo", parent=1,
                        reason="v1: no_change: 1 of 1 targeted error(s) remained — A.java:5 cannot find symbol")
    kb.cases.credit(CASE, 1, "no_change", project="demo", run_id="r1", round=1, n=1, errors_before=3, errors_left=3,
                    errors_new=0, files=["notes.txt"],
                    reason="no_change: 1 of 1 targeted error(s) remained — A.java:5 cannot find symbol")
    kb.cases.credit(CASE, 2, "build_ok", project="demo", run_id="r1", round=2, n=2, errors_before=3, errors_left=0,
                    errors_new=0, files=["A.java"])
    kb.record_run([entry("e1", run_id="r1", tries=[
        FixTry(n=1, round=1, source="kb-script", script_version=1, applied="script", result="no_change"),
        FixTry(n=2, round=2, source="llm-augment", script_version=2, applied="script", result="build_ok")])])
    code, out = _cli(kb, "cases", "show", CASE, "--no-code")
    assert code == 0
    lines = out.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("  script v1 "))
    assert lines[i + 1:i + 5] == [
        "    parent: - (written new)",
        "    last failure: no_change: 1 of 1 targeted error(s) remained — A.java:5 cannot find symbol",
        "    runs (1, newest last):",
        ("      run r1 demo r1 try 1: no_change — no_change: 1 of 1 targeted error(s) remained — A.java:5 cannot "
         "find symbol [errors 3 → 3, new 0; files: notes.txt]")]
    j = next(n for n, ln in enumerate(lines) if ln.startswith("  script v2 "))
    assert lines[j + 1:j + 5] == [
        "    parent: v1",
        "    reason: v1: no_change: 1 of 1 targeted error(s) remained — A.java:5 cannot find symbol",
        "    runs (1, newest last):",
        "      run r1 demo r2 try 2: build_ok [errors 3 → 0, new 0; files: A.java]"]
    code, out = _cli(kb, "reindex")
    assert code == 0 and out.strip().splitlines()[-1].startswith(
        "1 case(s), 2 script version(s); 2 script run record(s) rebuilt, 0 entr(ies) given as-is/to-be hunks — ")
    assert [len(s.runs) for s in kb.cases.find(CASE).scripts] == [1, 1]          # merged with the store's records
