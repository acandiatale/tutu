"""v2.1z-ca, reuse: ``stored_best`` / ``stored_next`` never select a stored version marked ``round_trip`` (it took
part in two cases undoing each other's fix), exactly as for ``non_idempotent``; the next version is used when one
exists, and the KB search line says why nothing ran."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import ROUND_TRIP
from sbmig.triage.scripts import plan_script_step, stored_best, stored_next

L2 = "compile|cannot_find_symbol|org.example.Thing"
CID = case_id_for(L2)
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\n"
WHY = "round trip: put back the lines attempt verify-1-1 (case abc) had changed in src/A.java"


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def cluster() -> ErrorCluster:
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="A.java", line=1,
                     signature=L2 + "|L1", signature_generic=L2)
    return ErrorCluster(id="c", kind=ErrorKind.COMPILE, errors=[err], case_id=CID, signatures=[L2 + "|L1"])


def test_stored_best_and_next_skip_a_marked_version_and_fall_through(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.add_script(CID, L2, CODE2, run_id="r1", project="maker")
    store.credit(CID, 2, "improved", project="other", run_id="r2", round=1, n=1)       # v2 proven
    assert stored_best(store, CID)[0].version == 2
    store.credit(CID, 2, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    assert stored_best(store, CID)[0].version == 1
    assert stored_best(store, CID, scope="full")[0].version == 1
    assert stored_next(store, CID, ())[0].version == 1
    assert stored_next(store, CID, (), scope="full")[0].version == 1
    assert stored_next(store, CID, (1,)) is None


def test_nothing_is_reused_when_every_version_is_marked(store, cfg):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, "improved", project="other", run_id="r2", round=1, n=1)
    store.credit(CID, 1, ROUND_TRIP, project="other", run_id="r2", round=1, n=1, reason=WHY)
    assert stored_best(store, CID) is None and stored_best(store, CID, scope="full") is None
    assert stored_next(store, CID, ()) is None and stored_next(store, CID, (), scope="jdk-jakarta") is None
    step = plan_script_step(cluster(), [], store, cfg, scope="full")
    assert step.mode != "reuse"
    assert "케이스는 있으나 쓸 스크립트 없음 (v1 unverified round-trip 0/1)" in step.kb_line


def test_stored_best_skips_a_marked_best_of_a_store_that_does_not(store):
    """A store whose own `best` does not know the mark (an adapter, an older store): `stored_best` skips it."""
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.add_script(CID, L2, CODE2, run_id="r1", project="maker")
    marked = SimpleNamespace(version=2, status="proven", round_trip=True, non_idempotent=False, scope="")
    plain = SimpleNamespace(version=1, status="unverified", round_trip=False, non_idempotent=False, scope="")
    fake = SimpleNamespace(best=lambda cid: (marked, CODE2),
                           find=lambda cid: SimpleNamespace(scripts=[plain, marked]),
                           code=lambda cid, ver: {1: CODE1, 2: CODE2}[ver])
    got = stored_best(fake, CID)
    assert got is not None and got[0].version == 1


def test_a_cleared_version_is_reused_again(store):
    store.add_script(CID, L2, CODE1, run_id="r1", project="maker")
    store.credit(CID, 1, ROUND_TRIP, project="maker", run_id="r1", round=1, n=1, reason=WHY)
    assert stored_best(store, CID) is None
    store.activate(CID, 1)
    assert stored_best(store, CID)[0].version == 1
