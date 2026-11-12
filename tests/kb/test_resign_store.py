"""v2.1z-ai re-sign through the facade and the CLI: the automatic triggers (first match, record_run), never failing a
run, `sbmig kb reindex [--dry-run]`, lookups by an old id, `kb validate`, `kb stats`."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.build import signatures as sigs
from sbmig.core.models import BuildError, FixTry
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import KB_MARKER
from sbmig.kb import FileKnowledgeBase, kb_cli

from .helpers import entry, make_cluster, make_error

L2 = "compile|foo|thing"
OLD = case_id_for(L2)
NEW_L2 = L2 + ".X"
NEW = case_id_for(NEW_L2)
CODE = "def fix(p):\n    p.log('x')\n"


def x_signer(err: BuildError) -> str:
    return err.signature_generic + ".X"


@pytest.fixture(autouse=True)
def foo_rules(monkeypatch, kb):
    """Rules 2 changed class `foo`; the KB's store re-signs with the fake rule."""
    monkeypatch.setattr(sigs, "SIGNATURE_VERSION", 2)
    monkeypatch.setattr(sigs, "SIGNATURE_CHANGES", {2: frozenset({"foo"})})
    kb.cases.signer = x_signer


def old_entry(eid: str, **kw):
    kw.setdefault("tries", [FixTry(n=1, round=1, source="kb-script", script_version=1, applied="script",
                                   result="improved")])
    return entry(eid, l2=L2, **kw)


def old_kb(kb: FileKnowledgeBase) -> None:
    """A KB written by rules 1: a case with a script, two entries signed by nothing, no kb.json."""
    store = CaseStore(kb.root, kb.cfg)
    store.add_script(OLD, L2, CODE, run_id="r0", project="p0")
    store.record_run([old_entry("e1"), old_entry("e2", run_id="r2", project="p2")])
    (kb.root / KB_MARKER).unlink()


def test_first_match_resigns_once(kb: FileKnowledgeBase, monkeypatch):
    old_kb(kb)
    calls = {"marker": 0, "resign": 0}
    real_marker, real_resign = CaseStore.marker_sig, CaseStore.resign

    def marker(self):
        calls["marker"] += 1
        return real_marker(self)

    def resign(self, *a, **k):
        calls["resign"] += 1
        return real_resign(self, *a, **k)

    monkeypatch.setattr(CaseStore, "marker_sig", marker)
    monkeypatch.setattr(CaseStore, "resign", resign)
    kb.match(make_cluster())
    assert calls["resign"] == 1 and NEW in kb.cases.case_ids() and OLD not in kb.cases.case_ids()
    seen = calls["marker"]
    kb.match(make_cluster())
    FileKnowledgeBase(kb.cfg, kb_dir=kb.root).match(make_cluster())        # same directory, same process
    assert calls["resign"] == 1 and calls["marker"] == seen                # the file is not looked at again
    assert json.loads((kb.root / KB_MARKER).read_text())["sig"] == 2


def test_record_run_resigns_when_an_older_line_is_left(kb: FileKnowledgeBase):
    kb.cases.add_script(OLD, L2, CODE)
    assert kb.record_run([old_entry("e1")]) == 1                           # an older pod's line
    assert kb.last_resign is not None and kb.last_resign.moves == [(OLD, NEW, NEW_L2)]
    assert [e.case_id for e in kb.cases.iter_errors()] == [NEW]
    kb.last_resign = None
    assert kb.record_run([entry("e2", l2=NEW_L2, sig=2)]) == 1             # current lines: no re-sign
    assert kb.last_resign is None


def test_a_failing_resign_never_fails_a_run(kb: FileKnowledgeBase, monkeypatch, caplog):
    old_kb(kb)
    monkeypatch.setattr(CaseStore, "resign", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    kb.cases.add_script(case_id_for(make_error().signature_generic), make_error().signature_generic, CODE)
    assert kb.match(make_cluster()).case_hits                              # the lookup goes on
    assert kb.record_run([old_entry("e9")]) == 1
    rep = kb.resign()
    assert rep.error and "boom" in rep.error
    assert kb.reindex() >= 1


def test_without_the_lock_nothing_is_resigned(kb: FileKnowledgeBase, monkeypatch, caplog):
    old_kb(kb)

    @contextmanager
    def no_lock(self):
        yield False

    monkeypatch.setattr(CaseStore, "lock", no_lock)
    rep = kb.resign()
    assert rep.error and OLD in kb.cases.case_ids()
    assert any("not taken" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------------- CLI
def run(kb, *args):
    res = CliRunner().invoke(kb_cli, list(args), obj={"kb": kb}, catch_exceptions=False)
    return res.exit_code, res.output + (getattr(res, "stderr", "") if getattr(res, "stderr_bytes", None) else "")


def test_cli_reindex_dry_run_then_reindex(kb: FileKnowledgeBase):
    old_kb(kb)
    before = sorted(p.relative_to(kb.root).as_posix() for p in kb.root.rglob("*") if p.is_file())
    code, out = run(kb, "reindex", "--dry-run")
    assert code == 0 and "dry run" in out and f"{OLD} → {NEW}" in out
    assert sorted(p.relative_to(kb.root).as_posix() for p in kb.root.rglob("*") if p.is_file()) == before
    code, out = run(kb, "reindex")
    assert code == 0 and "2 re-signed" in out and f"{OLD} → {NEW}  {NEW_L2}" in out and "1 case(s)" in out
    assert kb.cases.case_ids() == [NEW]
    code, out = run(kb, "reindex")
    assert "nothing signed by older rules" in out


def test_cli_finds_a_case_by_its_old_id(kb: FileKnowledgeBase):
    old_kb(kb)
    kb.reindex()
    code, out = run(kb, "cases", "show", OLD)
    assert code == 0 and f"case {OLD} is now {NEW}" in out and f"case {NEW}" in out
    assert f"aliases: {OLD}" in out and "sig 2" in out
    code, out = run(kb, "cases", "pin", OLD[:6], "block:old id")
    assert code == 0 and kb.cases.pin_of(NEW) == "block:old id"
    code, out = run(kb, "cases", "retire", OLD, "1")                    # version numbers may differ: refused
    assert code == 1 and f"case {OLD} is now {NEW}: version numbers may have changed" in out
    assert f"sbmig kb cases show {NEW}" in out and kb.cases.scripts(NEW)[0].status != "retired"
    assert run(kb, "cases", "activate", OLD, "1")[0] == 1
    code, out = run(kb, "cases", "unpin", OLD)
    assert code == 0 and kb.cases.pin_of(NEW) == ""
    code, out = run(kb, "stats")
    assert "sig" in out


def test_cli_validate_names_records_of_older_rules(kb: FileKnowledgeBase):
    old_kb(kb)
    code, out = run(kb, "validate")
    assert code == 1 and "2 record(s) in 1 case(s) signed by older rules (version < 2): run sbmig kb reindex" in out
    run(kb, "reindex")
    code, out = run(kb, "validate")
    assert code == 0, out


def test_stats_name_the_marker_version(kb: FileKnowledgeBase):
    old_kb(kb)
    assert kb.stats()["sig"] == 0
    kb.reindex()
    assert kb.stats()["sig"] == 2


def test_validate_with_real_signer_counts_only_records_that_move(kb: FileKnowledgeBase, tmp_path: Path):
    # entries of a class the rules never changed are not "older" problems
    other = "compile|bar|k"
    kb.cases.record_run([entry("e1", l2=other)])
    assert not [p for p in kb.validate() if "older rules" in p]
