"""v2.1h (2026-10-28, docs/v2/HANDOVER-FINDINGS-2026-10-28.md A15–A18) — sbmig.errors.cases: `kb reindex` counts the
hunk backfill and the fixer attribution separately and afresh on every recount, attributes fixers without the hunk
helper, `merge_from` never reuses a script number that is on disk, `activate` of an old record without `project`
follows `credit`'s run-id rule."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sbmig.core.models import CaseScript, EditNote, ErrorEntry, ErrorKind, FixTry, ScriptRun
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import _resolved_elsewhere

JAXB = "compile|package_does_not_exist|jakarta.xml.bind"
XML = "compile|cannot_find_symbol|XmlElement"
CJ, CX = case_id_for(JAXB), case_id_for(XML)
A = "module/comp-jar/src/main/java/com/x/A.java"
POM = "module/comp-jar/pom.xml"
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\n"


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def _fixer(diff: str = "") -> FixTry:
    return FixTry(n=1, round=1, source="llm-new", script_version=1, applied="script", files=[POM], commit="c0ffee",
                  result="improved", diff=diff, edits=[EditNote(path=POM, lines="+28–36", change="jakarta.xml.bind-api 추가")])


def _entries(diff: str = "") -> list[ErrorEntry]:
    """r1: the JAXB entry resolved by its try (that changed the module pom); the XmlElement entry vanished from the
    build without a try (`resolved: build`) — its probable fixer is the JAXB try."""
    fixed = ErrorEntry(id="e2", run_id="r1", project="p1", case_id=CJ, l2=JAXB, kind=ErrorKind.COMPILE, file=A, line=6,
                       message="package jakarta.xml.bind does not exist", seen="compile", status="resolved",
                       resolved="r1 try 1", tries=[_fixer(diff)])
    gone = ErrorEntry(id="e1", run_id="r1", project="p1", case_id=CX, l2=XML, kind=ErrorKind.COMPILE, file=A, line=22,
                      message="cannot find symbol", seen="compile", status="resolved", resolved="build")
    return [fixed, gone]


def _write_jsonl(store: CaseStore, entries: list[ErrorEntry]) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    store.errors_path.write_text("\n".join(e.model_dump_json() for e in entries) + "\n", encoding="utf-8")


def _info(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "sbmig.errors" and r.getMessage().startswith("reindex:")]


# ------------------------------------------------------------------------------------------------ A15
def test_reindex_counts_hunks_and_fixers_separately(store: CaseStore, monkeypatch, caplog):
    """One entry given hunks, another given a fixer: 1 and 1 (the old count said 2 entries were given hunks)."""
    import sbmig.script.notes as script_notes
    from sbmig.core.models import EditHunk

    def fake_attach(notes, diff, *, max_lines=0):
        return [n.model_copy(update={"hunks": [EditHunk(lines=n.lines, before="", after="<dependency/>")]})
                for n in notes]
    monkeypatch.setattr(script_notes, "attach_hunks", fake_attach, raising=False)
    _write_jsonl(store, _entries(diff="--- a/pom.xml\n+++ b/pom.xml\n+<dependency/>"))
    with caplog.at_level(logging.INFO, logger="sbmig.errors"):
        store.recount()
    assert store.last_reindex["hunks"] == 1 and store.last_reindex["resolved_by"] == 1
    assert any("1 entr(ies) given as-is/to-be hunks, 1 build-resolved entr(ies) given a probable fixer" in m
               for m in _info(caplog))
    by_id = {e.id: e for e in store.iter_errors()}
    assert by_id["e2"].tries[0].edits[0].hunks and not by_id["e2"].resolved_by
    assert [rb.case_id for rb in by_id["e1"].resolved_by] == [CJ] and not by_id["e1"].tries


def test_a_fixer_only_change_is_no_hunk(store: CaseStore, monkeypatch):
    """attach_hunks adds nothing (the notes come back as they were): the one rewritten entry got a fixer only, so
    0 hunks and 1 resolved_by (the old count said 1 hunk)."""
    import sbmig.script.notes as script_notes
    monkeypatch.setattr(script_notes, "attach_hunks", lambda notes, diff, *, max_lines=0: notes, raising=False)
    _write_jsonl(store, _entries(diff="+x"))
    store.recount()
    assert (store.last_reindex["hunks"], store.last_reindex["resolved_by"]) == (0, 1)


# ------------------------------------------------------------------------------------------------ A16
def test_the_fixer_is_attributed_without_attach_hunks(store: CaseStore, monkeypatch, caplog):
    import sbmig.script.notes as script_notes
    monkeypatch.delattr(script_notes, "attach_hunks", raising=False)          # `from … import attach_hunks` fails
    _write_jsonl(store, _entries(diff="+x"))
    with caplog.at_level(logging.INFO, logger="sbmig.errors"):
        store.recount()
    assert any("0 entr(ies) given as-is/to-be hunks, 1 build-resolved entr(ies) given a probable fixer" in m
               for m in _info(caplog))
    gone = next(e for e in store.iter_errors() if e.id == "e1")
    assert [(rb.case_id, rb.round, rb.n, rb.match) for rb in gone.resolved_by] == [(CJ, 1, 1, "reindex")]
    assert store.entry_file(gone).is_file() and '"reindex"' in store.entry_file(gone).read_text(encoding="utf-8")


def test_every_recount_reports_its_own_counts(store: CaseStore, caplog):
    """The second recount on the same store attributes nothing new (0), also when errors.jsonl is gone — no number
    from the previous call is reported again."""
    _write_jsonl(store, _entries())
    store.recount()
    assert store.last_reindex["resolved_by"] == 1
    store.recount()
    assert store.last_reindex["resolved_by"] == 0
    _write_jsonl(store, _entries())                                          # the pre-v2.1f records again
    store.recount()
    assert store.last_reindex["resolved_by"] == 1
    store.errors_path.unlink()                                               # the backfill returns early now
    with caplog.at_level(logging.INFO, logger="sbmig.errors"):
        store.recount()
    assert (store.last_reindex["hunks"], store.last_reindex["resolved_by"]) == (0, 0)
    assert any("0 entr(ies) given as-is/to-be hunks, 0 build-resolved" in m for m in _info(caplog))


def test_a_broken_fixer_guess_leaves_the_hunks(store: CaseStore, monkeypatch):
    import sbmig.script.notes as script_notes
    from sbmig.core.models import EditHunk

    monkeypatch.setattr(script_notes, "attach_hunks", lambda notes, diff, *, max_lines=0: [
        n.model_copy(update={"hunks": [EditHunk(lines="+1", before="", after="x")]}) for n in notes], raising=False)
    monkeypatch.setattr(CaseStore, "_fixer_attributed", staticmethod(lambda e, tries: 1 / 0))
    _write_jsonl(store, _entries(diff="+x"))
    store.recount()
    assert (store.last_reindex["hunks"], store.last_reindex["resolved_by"]) == (1, 0)


# ------------------------------------------------------------------------------------------------ A17
def test_merge_from_never_overwrites_an_unlisted_script_file(tmp_path: Path, cfg):
    a, b = CaseStore(tmp_path / "a", cfg), CaseStore(tmp_path / "b", cfg)
    a.add_script(CJ, JAXB, CODE1, project="pa")
    orphan = a.case_dir(CJ) / "fix_v2.py"                                    # on disk, not in case.json
    orphan.write_text("# hand-made, not listed\n", encoding="utf-8")
    b.add_script(CJ, JAXB, CODE2, project="pb")
    assert a.merge_from(b)["scripts_added"] == 1
    assert orphan.read_text(encoding="utf-8") == "# hand-made, not listed\n"
    assert [s.version for s in a.find(CJ).scripts] == [1, 3] and a.code(CJ, 3) == CODE2
    assert a.merge_from(b)["scripts_added"] == 0                             # idempotent
    assert a.add_script(CJ, JAXB, "def fix(p):\n    pass\n").version == 4    # add_script: the same rule


def test_merge_from_a_new_case_keeps_the_local_orphan_files(tmp_path: Path, cfg):
    """A local case folder without case.json but with a fix_v1.py (a run stopped before saving case.json): the
    imported case's script goes to the next free number instead of overwriting it."""
    a, b = CaseStore(tmp_path / "a", cfg), CaseStore(tmp_path / "b", cfg)
    a.case_dir(CX).mkdir(parents=True)
    orphan = a.case_dir(CX) / "fix_v1.py"
    orphan.write_text("# left by a stopped run\n", encoding="utf-8")
    b.add_script(CX, XML, CODE1, project="pb")
    counters = a.merge_from(b)
    assert (counters["cases_added"], counters["scripts_added"]) == (1, 1)
    assert orphan.read_text(encoding="utf-8") == "# left by a stopped run\n"
    assert [(s.version, s.file) for s in a.find(CX).scripts] == [(2, "fix_v2.py")] and a.code(CX, 2) == CODE1
    assert a.merge_from(b) == {"errors_added": 0, "cases_added": 0, "scripts_added": 0, "scripts_merged": 1}
    c = CaseStore(tmp_path / "c", cfg)                                       # no orphan: the folder is copied whole
    assert c.merge_from(b)["cases_added"] == 1 and [s.version for s in c.find(CX).scripts] == [1]


# ------------------------------------------------------------------------------------------------ A18
def test_activate_of_a_record_without_project_follows_the_run_id(store: CaseStore):
    store.add_script(CJ, JAXB, CODE1, run_id="r1", project="")              # an older record: no project
    store.credit(CJ, 1, "improved", project="p1", run_id="r1")              # its own run
    store.retire(CJ, 1)
    assert store.activate(CJ, 1).status == "unverified"                      # its own project is not "another"
    store.credit(CJ, 1, "improved", project="p2", run_id="r2")              # retired: credit keeps it retired
    assert store.activate(CJ, 1).status == "proven"                          # r2 ≠ r1 (and two projects)


def test_resolved_elsewhere_rules():
    ok = ScriptRun(run_id="r2", project="p2", result="improved")
    assert _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", project="p1", projects=["p1", "p2"]))
    assert not _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", project="p1", projects=["p1"]))
    assert not _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", projects=["p1"]))           # no project
    assert not _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", created_by="r1", projects=["p1"],
                                              runs=[ScriptRun(run_id="r1", result="improved")]))
    assert not _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", created_by="r1", projects=["p1"],
                                              runs=[ScriptRun(run_id="r2", result="no_change")]))
    assert _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", created_by="r1", projects=["p2"], runs=[ok]))
    assert _resolved_elsewhere(CaseScript(version=1, file="fix_v1.py", projects=["p1", "p2"]))
