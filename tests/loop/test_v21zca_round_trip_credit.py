"""v2.1z-ca (docs/INTERFACES.md "v2.1z-ca", **The rule.** / **The loop.**): a stored script that took part in a round
trip is credited `round_trip`, not as a success.

For an attempt B with the trade-back verdict of v2.1z-bz against an attempt A of another case: B's stored version is
credited `round_trip` (never `improved`; in `revert` instead of `reverted`); when B stays in the tree, A's version is
credited `round_trip` again with the round / n of its first credit (the store takes that success back); a reverted B
leaves A alone. Unstored (v0) scripts, edits, a dry run and no KB stay untouched; `off` credits as before. The KB
store's own handling of `round_trip` is not exercised here (the fake store records the calls)."""
from __future__ import annotations

from sbmig.core.models import Phase

from .fakes import FakeKB, bad_build, write_script
from .test_v21v_y_prompt_facts import make_loop
from .test_v21zbz_trade_back import (
    CX,
    CY,
    FOO,
    L2X,
    ByCase,
    _run_undo,
    _second_ws,
    build_foo,
    err_x,
    err_y,
    events,
    setup_undo,
    start,
    tracking,
)

NOTE = "credited as round_trip: not reused without an LLM answer"
B_REASON = (f"round trip: put back the lines attempt verify-1-1 (case {CX}) had changed in {FOO}; the error(s) it had "
            "resolved came back")


def a_reason(b_id: str) -> str:
    return (f"round trip: the error(s) this version resolved came back when attempt {b_id} (case {CY}) put back the "
            f"lines it had changed in {FOO}")


def credits_of(kb, case: str) -> list[dict]:
    return [r for r in kb.cases.credit_runs if r["case"] == case]


# ------------------------------------------------------------------------------------------- full runs
def test_observe_credits_both_versions_round_trip(cfg, ws):
    from sbmig.errors.entries import ErrorList
    from sbmig.loop.entries import RunErrors
    cfg.loop.trade_back = "observe"
    start(ws)
    kb = FakeKB()
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=kb)
    el = ErrorList.for_workspace(ws, cfg, project="demo")
    loop.errors = RunErrors(el)
    pr = loop.run(Phase.VERIFY, "package", 6)
    a, b = pr.attempts[0], pr.attempts[1]
    assert (a.script_case_id, b.script_case_id) == (CX, CY) and b.outcome == "improved"
    # A: improved at its build, then round_trip with the same try (round, n)
    ra = credits_of(kb, CX)
    assert [r["result"] for r in ra] == ["improved", "round_trip"]
    assert (ra[0]["round"], ra[0]["n"]) == (ra[1]["round"], ra[1]["n"]) and ra[0]["n"] > 0
    assert ra[1]["version"] == a.script_version and ra[1]["reason"] == a_reason(b.id)
    # B: round_trip, never improved
    rb = [r for r in credits_of(kb, CY) if r["version"] == b.script_version]
    assert [r["result"] for r in rb] == ["round_trip"] and rb[0]["reason"] == B_REASON.replace("verify-1-1", a.id)
    # the journal: script_credit with "credited": "round_trip" and the reason
    sc = [e for e in events(ws, "script_credit") if e.get("credited") == "round_trip"]
    assert {(e["attempt"], e["outcome"]) for e in sc} == {(a.id, "improved"), (b.id, "improved")}
    assert all(e["reason"].startswith("round trip: ") for e in sc)
    # the notes, once each, and the error list shows them
    for att in (a, b):
        assert [n for n in att.notes if NOTE in n] == [f"stored v{att.script_version} {NOTE}"]
    notes = {(e.case_id, t.round, t.n): t.note for e in el.entries for t in e.tries if e.case_id in (CX, CY)}
    assert sum(NOTE in (n or "") for n in notes.values()) == 2
    assert el.md_path.read_text(encoding="utf-8").count(NOTE) >= 2
    # the run itself is that of observe (v2.1z-bz)
    assert [e["mode"] for e in events(ws, "trade_back")] == ["observe"]


def test_revert_credits_b_round_trip_and_leaves_a(cfg, ws):
    pr, kb, _seen = _run_undo(cfg, ws, "revert")
    a, b = pr.attempts[0], pr.attempts[1]
    assert b.outcome == "reverted"
    assert [r["result"] for r in credits_of(kb, CX)] == ["improved"]
    rb = [r for r in credits_of(kb, CY) if r["version"] == b.script_version]
    assert [r["result"] for r in rb] == ["round_trip"]
    assert not any(NOTE in n for n in a.notes) and any(NOTE in n for n in b.notes)
    sc = [e for e in events(ws, "script_credit") if e["attempt"] == b.id]
    assert sc == [{**sc[0], "outcome": "reverted", "credited": "round_trip"}]


def test_off_credits_as_before(cfg, ws, tmp_path):
    pr, kb, _seen = _run_undo(cfg, ws, "off")
    assert not any(r["result"] == "round_trip" for r in kb.cases.credit_runs)
    assert [r["result"] for r in credits_of(kb, CX)] == ["improved"]
    assert pr.attempts[1].outcome == "improved"
    assert not any(NOTE in n for a in pr.attempts for n in a.notes)
    assert not any(e.get("credited") for e in events(ws, "script_credit"))


def test_revert_without_revert_on_regression_also_recredits_a(cfg, ws):
    """B regressed but kept (loop.revert_on_regression: false): A's errors are in the build again."""
    cfg.loop.trade_back = "revert"
    cfg.loop.revert_on_regression = False
    start(ws)
    kb = FakeKB()
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]})
    pr = make_loop(cfg, ws, fixer, build=build_foo, kb=kb).run(Phase.VERIFY, "package", 2)
    a, b = pr.attempts[0], pr.attempts[1]
    assert b.outcome == "regressed"
    assert [r["result"] for r in credits_of(kb, CX)] == ["improved", "round_trip"]
    assert [r["result"] for r in credits_of(kb, CY) if r["version"] == b.script_version] == ["round_trip"]
    assert a.script_case_id == CX


def test_reused_stored_scripts_are_credited_round_trip(cfg, ws):
    """Both cases reuse a stored version (`kb-script`, no LLM): the round trip credits both `round_trip`."""
    cfg.loop.trade_back = "observe"
    start(ws)
    kb = FakeKB()
    va = kb.cases.seed(CX, write_script({FOO: "class Foo { half }\n"}).code)
    vb = kb.cases.seed(CY, write_script({FOO: "class Foo { broken }\n"}).code)
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}], CY: [{FOO: "class Foo { ok }\n"}]})
    seen: list[str] = []
    pr = make_loop(cfg, ws, fixer, build=tracking(build_foo, seen), kb=kb).run(Phase.VERIFY, "package", 4)
    a, b = pr.attempts[0], pr.attempts[1]
    assert (a.script_case_id, a.script_version, b.script_case_id, b.script_version) == (CX, va.version, CY, vb.version)
    assert a.script_mode == b.script_mode == "reuse"
    assert [r["result"] for r in credits_of(kb, CX) if r["version"] == va.version] == ["improved", "round_trip"]
    assert [r["result"] for r in credits_of(kb, CY) if r["version"] == vb.version] == ["round_trip"]
    # every credit of a reuse says so (the store leaves the mark on a success of a reuse)
    assert [r["reused"] for r in kb.cases.credit_runs if r["version"] in (va.version, vb.version)
            and r["case"] in (CX, CY)] == [True, True, True]


def test_an_llm_answer_is_not_credited_as_a_reuse(cfg, ws):
    """`reused` goes only with an attempt whose script_mode is `reuse` (new / augment answers came from the LLM)."""
    pr, kb, _seen = _run_undo(cfg, ws, "observe")
    modes = {(a.script_case_id, a.script_version): a.script_mode for a in pr.attempts if a.script_version}
    assert kb.cases.credit_runs and "reuse" not in modes.values()
    assert not any(r["reused"] for r in kb.cases.credit_runs)


def test_the_adapter_passes_reused_to_a_store_that_takes_it_and_drops_it_for_one_that_does_not():
    from sbmig.loop.entries import CaseScripts

    class Old:
        def __init__(self):
            self.calls = []

        def best(self, case_id):                  # CaseScripts adapts a store that has `best`
            return None

        def credit(self, case_id, version, result, project="", run_id="", *, reason="", round=0, n=0):
            self.calls.append((case_id, version, result, reason, round, n))

    class New(Old):
        def credit(self, case_id, version, result, project="", run_id="", *, reason="", round=0, n=0,
                   reused=False):
            self.calls.append((case_id, version, result, reused))

    old, new = Old(), New()
    assert CaseScripts(old).credit("c", 1, "improved", project="p", run_id="r", reason="x", n=2, reused=True)
    assert CaseScripts(new).credit("c", 1, "improved", project="p", run_id="r", reason="x", n=2, reused=True)
    assert old.calls == [("c", 1, "improved", "x", 0, 2)]
    assert new.calls == [("c", 1, "improved", True)]


# ------------------------------------------------------------------------------------------- the real store
class _RealKB(FakeKB):
    """FakeKB whose `cases` is the real `CaseStore` (end to end through the adapter)."""

    def __init__(self, cases):
        super().__init__()
        self.cases = cases


def test_observe_with_the_real_store_marks_both_and_a_second_run_reuses_neither(cfg, ws, tmp_path):
    from sbmig.core.workspace import Workspace
    from sbmig.errors import CaseStore

    from .conftest import make_project
    cfg.loop.trade_back = "observe"
    answers = {CX: [{FOO: "class Foo { half }\n"}],
               CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]}
    store = CaseStore(tmp_path / "kb", cfg)
    start(ws)
    pr = make_loop(cfg, ws, ByCase(answers), build=build_foo, kb=_RealKB(store)).run(Phase.VERIFY, "package", 6)
    a, b = pr.attempts[0], pr.attempts[1]
    assert events(ws, "trade_back") and (a.script_case_id, b.script_case_id) == (CX, CY)
    for att in (a, b):
        s = next(x for x in store.find(att.script_case_id).scripts if x.version == att.script_version)
        assert (s.successes, s.round_trip) == (0, True), s
    # a second project on the same KB: no stored-script reuse of either case
    w2 = Workspace.from_input(make_project(tmp_path / "p2"), tmp_path / "out2", run_id="t-run-2")
    w2.git_init()
    start(w2)
    pr2 = make_loop(cfg, w2, ByCase(answers), build=build_foo, kb=_RealKB(store)).run(Phase.VERIFY, "package", 6)
    assert pr2.attempts
    assert not [x.id for x in pr2.attempts if x.script_mode == "reuse" and x.script_case_id in (CX, CY)]



def test_a_full_runs_llm_answer_with_the_code_of_a_marked_jdk_jakarta_version_clears_the_mark(cfg, ws, tmp_path):
    """best skips the marked version, the LLM answers with its code, add_script returns that version across scopes:
    the success of that full run clears the mark (the counts stay jdk-jakarta only)."""
    from sbmig.errors import CaseStore
    from sbmig.errors.cases import ROUND_TRIP
    start(ws)
    store = CaseStore(tmp_path / "kb", cfg)
    code = write_script({FOO: "class Foo { ok }\n"}).code.rstrip("\n")
    store.add_script(CX, L2X, code, run_id="r0", project="p0", scope="jdk-jakarta")
    store.credit(CX, 1, ROUND_TRIP, project="p9", run_id="r9", round=1, n=1, reason="round trip: x", scope="full")
    assert store.find(CX).scripts[0].round_trip and store.best(CX, "full") is None
    pr = make_loop(cfg, ws, ByCase({CX: [{FOO: "class Foo { ok }\n"}]}), build=build_foo,
                   kb=_RealKB(store)).run(Phase.VERIFY, "package", 3)
    a = pr.attempts[0]
    assert (a.script_mode, a.script_version) == ("new", 1) and a.outcome in ("improved", "build_ok")
    s = store.find(CX).scripts[0]
    assert (s.version, s.scope, s.round_trip) == (1, "jdk-jakarta", False)
    assert (s.successes, s.failures, s.projects) == (0, 0, [])
    assert [r.scope for r in s.runs][-1] == "full"

# ------------------------------------------------------------------------------------------- unit level (_score)
def _ready(loop, att, case: str, rnd: int = 1, n: int = 1, *, credited: str | None = None, ran: bool = True):
    """Mark `att` as a stored script whose run decided its outcome (and, optionally, as already credited)."""
    loop._tries[id(att)] = (case, n, rnd)
    loop._script_ran[id(att)] = ran
    if credited:
        loop._credited[id(att)] = credited


def _observe(cfg, ws, **kw):
    cfg.loop.trade_back = "observe"
    loop, a, b, p, pr = setup_undo(cfg, ws, **kw)
    _ready(loop, a, CX, 1, 1, credited="improved")
    _ready(loop, b, CY, 2, 1)
    return loop, a, b, p, pr


def test_unit_observe_a_is_recredited_during_scoring_and_b_by_after_scoring(cfg, ws):
    loop, a, b, p, pr = _observe(cfg, ws)
    kb = loop.kb
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert [(r["case"], r["result"], r["round"], r["n"]) for r in kb.cases.credit_runs] == [(CX, "round_trip", 1, 1)]
    assert kb.cases.credit_runs[0]["reason"] == a_reason(b.id)
    loop._after_scoring([b], 3)
    assert [(r["case"], r["result"]) for r in kb.cases.credit_runs] == [(CX, "round_trip"), (CY, "round_trip")]
    assert kb.cases.credit_runs[1]["reason"] == B_REASON
    # a later scoring of the same verdict and a later credit call credit nobody again
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._credit_script(a)
    loop._credit_script(b)
    loop._credit_scripts(pr, [a, b])
    assert len(kb.cases.credit_runs) == 2
    assert [n for n in a.notes if NOTE in n] == [f"stored v1 {NOTE}"] and len([n for n in b.notes if NOTE in n]) == 1


def test_unit_b_without_stored_version_credits_only_a(cfg, ws):
    loop, _a, b, p, pr = _observe(cfg, ws)
    b.script_version = 0                                               # an unstored v0 script
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert [(r["case"], r["result"]) for r in loop.kb.cases.credit_runs] == [(CX, "round_trip")]
    assert not any(NOTE in n for n in b.notes)


def test_unit_b_applied_as_edits_credits_only_a(cfg, ws):
    loop, _a, b, p, pr = _observe(cfg, ws)
    loop._script_ran[id(b)] = False                                    # a script stored next to applied edits
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert [(r["case"], r["result"]) for r in loop.kb.cases.credit_runs] == [(CX, "round_trip")]


def test_unit_a_without_stored_version_credits_only_b(cfg, ws):
    loop, a, b, p, pr = _observe(cfg, ws)
    a.script_version = 0
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert [(r["case"], r["result"]) for r in loop.kb.cases.credit_runs] == [(CY, "round_trip")]
    assert not any(NOTE in n for n in a.notes)


def test_unit_a_never_credited_before_is_credited_round_trip_once(cfg, ws):
    """A whose first credit did not happen (e.g. `_credited` empty): the round_trip credit is its only one."""
    loop, a, _b, p, pr = _observe(cfg, ws)
    loop._credited.pop(id(a))
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._credit_script(a)
    assert [(r["case"], r["result"]) for r in loop.kb.cases.credit_runs] == [(CX, "round_trip")]


def test_unit_revert_b_round_trip_a_untouched(cfg, ws):
    cfg.loop.trade_back = "revert"
    loop, a, b, p, pr = setup_undo(cfg, ws)
    _ready(loop, a, CX, credited="improved")
    _ready(loop, b, CY, 2, 1)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "reverted" and not loop.kb.cases.credit_runs
    loop._after_scoring([b], 3)
    assert [(r["case"], r["result"], r["reason"]) for r in loop.kb.cases.credit_runs] == [(CY, "round_trip", B_REASON)]
    assert id(a) not in loop._round_trips and id(b) in loop._round_trips


def test_unit_dry_run_credits_nothing(cfg, ws):
    loop, a, b, p, pr = _observe(cfg, ws)
    loop.dry_run = True
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert not loop.kb.cases.credit_runs
    assert not any(NOTE in n for n in a.notes + b.notes)


def test_unit_no_kb_credits_nothing(cfg, ws):
    from sbmig.loop.entries import CaseScripts
    loop, a, b, p, pr = _observe(cfg, ws)
    store = loop.kb.cases
    loop._cases = CaseScripts(None)                                    # a run without a KB
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert not store.credit_runs
    assert not any(NOTE in n for n in a.notes + b.notes)
    assert b.outcome == "improved"


def test_unit_off_has_no_round_trip_entry(cfg, ws):
    cfg.loop.trade_back = "off"
    loop, a, b, p, pr = setup_undo(cfg, ws)
    _ready(loop, a, CX, credited="improved")
    _ready(loop, b, CY, 2, 1)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._after_scoring([b], 3)
    assert not loop._round_trips
    assert [(r["case"], r["result"]) for r in loop.kb.cases.credit_runs] == [(CY, "improved")]


def test_unit_a_bookkeeping_failure_does_not_break_the_scoring(cfg, ws, monkeypatch, caplog):
    from sbmig.loop import fixloop
    loop, _a, b, p, pr = _observe(cfg, ws)

    def boom(self, att):
        raise RuntimeError("broken credit")

    monkeypatch.setattr(fixloop.BuildFixLoop, "_credit_script", boom)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "improved"
    assert [e["mode"] for e in events(ws, "trade_back")] == ["observe"]   # the count / journal still happened
    assert "round-trip credit bookkeeping" in caplog.text


def test_unit_the_reason_names_the_first_file_and_the_rest(cfg, ws):
    from .test_v21v_y_prompt_facts import BAR
    loop, a, b, _p, _pr = _observe(cfg, ws)
    loop._record_round_trip(b, [(err_x(), a), (err_x(BAR), a)])
    assert loop._round_trips[id(b)].endswith(f"had changed in {FOO} (+1 more); the error(s) it had resolved came back")
    assert loop._round_trips[id(a)].endswith(f"put back the lines it had changed in {FOO} (+1 more)")
    assert "\n" not in loop._round_trips[id(a)]


def test_unit_state_is_cleared_when_a_run_starts(cfg, ws):
    """The verdicts of one run (the orchestrator runs several on one BuildFixLoop) never reach the next: after a
    run with a round trip, a second run on the same loop holds none of the first run's attempts, and a first-run
    attempt credited again is credited with its own outcome."""
    cfg.loop.trade_back = "observe"
    start(ws)
    kb = FakeKB()
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=kb)
    pr1 = loop.run(Phase.VERIFY, "package", 6)
    first = {id(a) for a in pr1.attempts}                     # pr1 keeps them alive: their ids are not reused
    assert first & set(loop._round_trips)
    loop.run(Phase.VERIFY, "package", 1)
    assert not first & set(loop._round_trips)


def test_observe_and_off_differ_only_in_the_round_trip_credits(cfg, ws, tmp_path):
    pr_o, kb_o, seen_o = _run_undo(cfg, ws, "observe")
    ws_off = _second_ws(tmp_path)
    pr_f, kb_f, seen_f = _run_undo(cfg, ws_off, "off")
    assert seen_o == seen_f and [(a.id, a.outcome) for a in pr_o.attempts] == [(a.id, a.outcome) for a in pr_f.attempts]
    plain_o = [c[:3] for c in kb_o.cases.credits if c[2] != "round_trip"]
    plain_f = [c[:3] for c in kb_f.cases.credits]
    b = pr_f.attempts[1]
    assert (CY, b.script_version, "improved") in plain_f and (CY, b.script_version, "improved") not in plain_o
    assert [c for c in plain_f if c != (CY, b.script_version, "improved")] == plain_o
