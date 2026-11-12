"""v2.1z-ai exact-key guard: a stored case whose L2 is not the L2 asked for is not that case — nothing is reused from
or written into it (two L2s forced onto one case id here; in life a 10-character hash clash or a hand-edited
case.json)."""
from __future__ import annotations

import logging

import pytest

import sbmig.errors.common as common
from sbmig.core.models import BuildError, ErrorCluster, ErrorEntry, ErrorKind, FixTry
from sbmig.errors import CaseKeyMismatch
from sbmig.kb import FileKnowledgeBase
from sbmig.loop.entries import CaseScripts

L2_A = "compile|cannot_find_symbol|Alpha"
L2_B = "compile|cannot_find_symbol|Beta"
CID = "c0111de000"
CODE = "def fix(p):\n    p.log('a')\n"


@pytest.fixture()
def collide(monkeypatch):
    """Every L2 hashes to CID (the KB facade and the error list compute ids through sbmig.errors.common)."""
    monkeypatch.setattr(common, "case_id_for", lambda l2: CID if l2 else "")


def err(l2: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/A.java", line=3,
                      symbol="class X", signature=l2 + "|L1", signature_generic=l2)


def cluster(l2: str) -> ErrorCluster:
    return ErrorCluster(id=f"case:{CID}", kind=ErrorKind.COMPILE, errors=[err(l2)], case_id=CID, signatures=[l2])


def entry(eid: str, l2: str) -> ErrorEntry:
    return ErrorEntry(id=eid, run_id="r1", project="p1", case_id=CID, l2=l2, kind=ErrorKind.COMPILE, file="src/A.java",
                      line=3, message="cannot find symbol", seen="compile", status="resolved", sig=2,
                      tries=[FixTry(n=1, round=1, source="kb-script", script_version=1, applied="script",
                                    result="improved")])


def test_a_case_held_by_another_signature_is_never_used(kb: FileKnowledgeBase, collide, caplog):
    store = kb.cases
    store.add_script(CID, L2_A, CODE, run_id="r0", project="p0", solution="use alpha")
    store.record_run([entry("ea", L2_A)])
    path = store.cases_dir / CID / "case.json"
    before = path.read_bytes()
    caplog.set_level(logging.WARNING, logger="sbmig.errors")

    m = kb.match(cluster(L2_B))
    assert m.case_hits == [] and m.hints == []
    assert store.best(CID) is None and store.latest(CID) is None
    assert "다른 서명" in store.describe(CID)
    with pytest.raises(CaseKeyMismatch):
        store.add_script(CID, L2_B, "def fix(p):\n    p.log('b')\n")
    assert CaseScripts(store).add_script(CID, L2_B, "def fix(p):\n    pass\n", source="llm-new", run_id="r1",
                                         project="p1") is None                     # the adapter survives it
    assert store.credit(CID, 1, "improved", project="p1", run_id="r1") is None
    assert kb.record_run([entry("eb", L2_B)]) == 1                                 # the record is kept …
    assert path.read_bytes() == before                                            # … the foreign case untouched
    assert [e.id for e in store.iter_errors()] == ["ea", "eb"]
    assert sorted(p.name for p in (store.cases_dir / CID).glob("fix_v*.py")) == ["fix_v1.py"]
    warnings = [r for r in caplog.records if "is held by another signature" in r.getMessage()]
    assert len(warnings) == 1 and f'stored "{L2_A}" asked "{L2_B}"' in warnings[0].getMessage()
    assert any("held by two signatures" in p for p in store.validate())
    assert store.recount() == 1 and store.find(CID).seen == 1                      # the foreign entry is not counted

    # asked again with the stored L2: it is that case after all
    hit = kb.match(cluster(L2_A))
    assert [h.case_id for h in hit.case_hits] == [CID] and store.best(CID)[1] == CODE


def test_record_run_alone_keeps_a_foreign_entry_out(kb: FileKnowledgeBase, collide):
    store = kb.cases
    store.add_script(CID, L2_A, CODE)
    before = (store.cases_dir / CID / "case.json").read_bytes()
    assert store.record_run([entry("eb", L2_B)]) == 1
    assert (store.cases_dir / CID / "case.json").read_bytes() == before
    assert not (store.cases_dir / CID / "errors" / "r1-eb.json").exists()
