"""v2.1z-cd (docs/INTERFACES.md "v2.1z-cd"): a case that shows up at a new place starts with the script that
resolved it elsewhere in this run.

Triage level: `made_here` (which earlier attempts of a case were made at the places of its errors now) and the new
first rung of `plan_script_step` (`elsewhere` = the case's attempts made at other places whose run of a stored version
resolved them). The caps are counted from `previous` = the attempts made here."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.interfaces import TriageContext
from sbmig.core.models import (
    BuildError,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FixAttempt,
    Phase,
    ScriptRunResult,
    TriageDecision,
)
from sbmig.errors import CaseStore, case_id_for
from sbmig.errors.cases import ROUND_TRIP
from sbmig.triage import Triager, plan_script_step
from sbmig.triage.decide import made_here

from .conftest import make_profile

L2 = "compile|build|resolution of annotationprocessorpath dependencies failed: cannot"
CID = case_id_for(L2)
SIG = L2 + "|processor path"
CODE = {v: f"def fix(p):\n    p.log('v{v}')\n" for v in range(1, 6)}


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def mod_err(m: str) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="processor path", module=m, signature=SIG, signature_generic=L2)


def file_err(path: str, m: str = "m1") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="processor path", file=path, module=m, line=3, signature=SIG,
                      signature_generic=L2)


def cluster(*errors: BuildError) -> ErrorCluster:
    return ErrorCluster(id=f"case:{CID}", kind=ErrorKind.COMPILE, errors=list(errors), case_id=CID,
                        signatures=[SIG])


_n = [0]


def att(mode: str, version: int, outcome: str, *errors: BuildError, ran: bool = True, edits: int = 0,
        code: str | None = None) -> FixAttempt:
    _n[0] += 1
    a = FixAttempt(id=f"verify-{_n[0]}-1", phase=Phase.VERIFY, iteration=_n[0], cluster_id=f"case:{CID}",
                   decision=TriageDecision(cluster_id=f"case:{CID}", kind=DecisionKind.SCRIPT), outcome=outcome,
                   script_mode=mode, script_version=version, script_case_id=CID,
                   script_code=CODE.get(version, "def fix(p):\n    pass\n") if code is None else code,
                   cluster_errors=list(errors), cluster_signatures=[SIG], edits_applied=edits,
                   source="kb-script" if mode == "reuse" else f"llm-{mode}")
    if ran:
        a.script_result = ScriptRunResult(status="ok")
    return a


def seed(store: CaseStore, n: int) -> None:
    for v in range(1, n + 1):
        store.add_script(CID, L2, CODE[v], run_id="r1", project="maker")


# ------------------------------------------------------------------------------------------------ places
def test_made_here_compares_the_places_of_the_targeted_errors():
    c2 = cluster(mod_err("m2"))
    assert not made_here(c2, att("new", 1, "improved", mod_err("m1")))
    assert made_here(c2, att("new", 1, "no_change", mod_err("m2")))
    assert made_here(c2, att("new", 1, "partial", mod_err("m1"), mod_err("m2")))
    # no targeted errors, or errors without a place: made here, as before
    assert made_here(c2, att("new", 1, "improved"))
    assert made_here(c2, att("new", 1, "improved", BuildError(kind=ErrorKind.UNKNOWN, message="x", signature=SIG)))
    # files: a file is a place; a moved file is the same place through `canon`
    cf = cluster(file_err("m1/src/B.java"))
    assert not made_here(cf, att("new", 1, "improved", file_err("m1/src/A.java")))
    assert made_here(cf, att("new", 1, "no_change", file_err("m1\\src\\B.java")))
    moved = {"old/B.java": "m1/src/B.java"}
    assert made_here(cf, att("new", 1, "no_change", file_err("old/B.java")), lambda p: moved.get(p or "", p or ""))


# ------------------------------------------------------------------------------------------------ rule 2
def test_nothing_here_and_a_run_elsewhere_resolved_it_reruns_that_version_ahead_of_best(store, cfg):
    seed(store, 4)
    store.credit(CID, 4, "improved", project="other", run_id="r0", round=1, n=1)     # v4 proven: the store's best
    assert store.best(CID)[0].version == 4
    elsewhere = [att("new", 3, "improved", mod_err("m1"))]
    step = plan_script_step(cluster(mod_err("m2")), [], store, cfg, elsewhere=elsewhere)
    assert (step.mode, step.version, step.code) == ("reuse", 3, CODE[3]) and not step.needs_llm
    assert step.meta is not None and step.meta.version == 3
    assert "this run's v3" in step.why and "@m1" in step.why and elsewhere[0].id in step.why
    assert step.kb_line.startswith("KB 탐색: 이번 실행의 v3") and "@m1" in step.kb_line
    assert elsewhere[0].id in step.kb_line and "@m2" in step.kb_line and step.kb_line.endswith("실행")
    # without the attempts made elsewhere: exactly the old first rung (the store's best)
    old = plan_script_step(cluster(mod_err("m2")), [], store, cfg)
    assert (old.mode, old.version) == ("reuse", 4)


def test_the_newest_successful_run_elsewhere_wins(store, cfg):
    seed(store, 3)
    elsewhere = [att("new", 1, "improved", mod_err("m1")), att("augment", 2, "build_ok", mod_err("m2")),
                 att("augment", 3, "no_change", mod_err("m2"))]
    step = plan_script_step(cluster(mod_err("m3")), [], store, cfg, elsewhere=elsewhere)
    assert (step.mode, step.version) == ("reuse", 2)


@pytest.mark.parametrize("why", ["reuse_off", "retired", "non_idempotent", "round_trip", "code_gone", "not_ran",
                                 "edits", "unstored", "failed"])
def test_the_rerun_falls_through_to_the_ordinary_ladder(store, cfg, why):
    seed(store, 2)
    a = att("new", 1, "improved", mod_err("m1"))
    if why == "reuse_off":
        cfg.script.reuse = False
    elif why == "retired":
        store.retire(CID, 1)
    elif why == "non_idempotent":
        store.mark_non_idempotent(CID, 1, "changes again")
    elif why == "round_trip":
        store.credit(CID, 1, ROUND_TRIP, project="p", run_id="r1", round=1, n=1, reason="round trip")
    elif why == "code_gone":
        (store.case_dir(CID) / "fix_v1.py").unlink()
    elif why == "not_ran":
        a = att("new", 1, "improved", mod_err("m1"), ran=False)
    elif why == "edits":
        a = att("new", 1, "improved", mod_err("m1"), edits=2)
    elif why == "unstored":
        a = att("new", 0, "improved", mod_err("m1"))
    elif why == "failed":
        a = att("new", 1, "no_change", mod_err("m1"))
    c = cluster(mod_err("m2"))
    step = plan_script_step(c, [], store, cfg, elsewhere=[a])
    assert step == plan_script_step(c, [], store, cfg)              # the ordinary ladder, byte for byte
    assert "이번 실행의" not in step.kb_line


def test_a_run_without_a_kb_changes_nothing(cfg):
    c = cluster(mod_err("m2"))
    a = att("new", 1, "improved", mod_err("m1"))
    step = plan_script_step(c, [], None, cfg, elsewhere=[a])
    assert step == plan_script_step(c, [], None, cfg) and step.mode == "new"


def test_an_attempt_made_here_keeps_todays_step(store, cfg):
    """Several ladder situations with an attempt made here: the attempts made elsewhere change nothing."""
    seed(store, 3)
    c = cluster(mod_err("m2"))
    good = [att("new", 1, "improved", mod_err("m1"))]
    for here in ([att("reuse", 3, "no_change", mod_err("m2"))],                   # → augment of the reuse
                 [att("new", 2, "partial", mod_err("m2"))],                       # → augment of the partial one
                 [att("new", 0, "apply_failed", mod_err("m2"), code="")],         # answer without code → reuse v3
                 [att("reuse", 3, "no_change", mod_err("m2"))]
                 + [att("augment", 2, "no_change", mod_err("m2"))] * 20):         # caps reached → blocked
        assert plan_script_step(c, here, store, cfg, elsewhere=good) == plan_script_step(c, here, store, cfg)
    capped = plan_script_step(c, [att("reuse", 3, "no_change", mod_err("m2"))]
                              + [att("augment", 2, "no_change", mod_err("m2"))] * 20, store, cfg, elsewhere=good)
    assert capped.mode is None


def test_twenty_tries_elsewhere_do_not_block_a_new_place(store, cfg):
    """The loop passes the attempts made here as `previous` and the decision engine counts the caps from them."""
    seed(store, 2)
    tries_m1 = [att("augment", 2, "no_change", mod_err("m1")) for _ in range(19)] + \
               [att("augment", 2, "improved", mod_err("m1"))]
    c = cluster(mod_err("m2"))
    tr = Triager(cfg)
    tr.attach_run(cases=store)
    ctx = TriageContext(phase=Phase.VERIFY, iteration=21, previous_attempts=[], resolved_elsewhere=tries_m1[-1:])
    d = tr.decide(c, None, ctx, make_profile())
    assert d.kind == DecisionKind.SCRIPT
    step = tr.script_step(c.id)
    assert (step.mode, step.version, step.llm_used, step.augments_used) == ("reuse", 2, 0, 0)
    # the same with no successful run elsewhere: the first rung of the ladder again (store best), not blocked
    ctx = TriageContext(phase=Phase.VERIFY, iteration=21, previous_attempts=[])
    assert tr.decide(c, None, ctx, make_profile()).kind == DecisionKind.SCRIPT
    assert tr.script_step(c.id).mode == "reuse" and tr.script_step(c.id).llm_used == 0
    # an older context without the field (a plugin's own TriageContext) still works
    assert "resolved_elsewhere" in TriageContext.model_fields
    assert TriageContext(phase=Phase.VERIFY, iteration=1).resolved_elsewhere == []


def test_the_rerun_is_the_stored_version_step_of_the_new_place_and_a_failed_one_is_augmented(store, cfg):
    seed(store, 1)
    c = cluster(mod_err("m2"))
    good = [att("new", 1, "improved", mod_err("m1"))]
    first = plan_script_step(c, [], store, cfg, elsewhere=good)
    assert (first.mode, first.version, first.stored_used) == ("reuse", 1, 0)
    rerun = att("reuse", 1, "no_change", mod_err("m2"))
    nxt = plan_script_step(c, [rerun], store, cfg, elsewhere=good)
    assert nxt.mode == "augment" and nxt.base_attempt is rerun and nxt.stored_used == 1
    assert nxt.llm_used == 0 and nxt.augments_used == 0
    # a rerun that changed nothing (apply_failed) leads to the same augment, never to the same rerun again
    rerun = att("reuse", 1, "apply_failed", mod_err("m2"))
    nxt = plan_script_step(c, [rerun], store, cfg, elsewhere=good)
    assert nxt.mode == "augment" and nxt.base_attempt is rerun
