"""v2.1z-bz (docs/INTERFACES.md "v2.1z-bz"): a fix that undoes an earlier kept fix and brings its errors back is a
regression.

Attempt A (case X) fixes X and leaves Y in the same file; attempt B (case Y) removes Y by undoing A, which brings X back.
A trade-back needs both: the error A had resolved is back in a file B touched, AND B's own diff removes a line A's diff
had added or puts back a line A's diff had removed (stripped; blank / punctuation-only lines do not count).
Before this round both were `improved`; now B is `regressed` (a trade-back): reverted (also behind a later kept commit,
by `git revert`), credited as a failure, shown to Y's next try with its `introduced:` lines, journaled (`trade_back`),
counted per pair of cases, and the case that keeps bringing the other case's errors back is blocked `oscillation` at
`loop.oscillation_limit`. `loop.trade_back: off` is the scoring of before.

The twelve situations of the brief, one or more tests each:
 (1) X → Y → undo                      test_the_undo_is_regressed_reverted_shown_and_credited_as_a_failure
 (2) Y fixed properly                  test_a_proper_fix_of_y_is_improved_and_nothing_is_counted
 (3) the case's own restored errors    test_errors_a_rollback_brought_back_are_baseline_not_a_trade_back
 (4) the resolver entry               test_a_later_resolution_replaces_the_entry (an `improved` attempt never
                                       leaves the tree in production: entries are only ever replaced)
 (5) other file / never resolved       test_the_same_text_in_another_file_is_not_a_trade_back,
                                       test_an_error_never_resolved_in_this_run_is_not_a_trade_back,
                                       test_the_resolvers_own_case_is_not_a_trade_back
 (6) a build that cannot tell          test_a_build_that_cannot_tell_gives_no_trade_back_verdict
 (7) two trade-backs → blocked         test_two_trade_backs_block_the_case_that_brings_errors_back
 (8) not at HEAD                       test_a_trade_back_behind_a_kept_commit_is_reverted_by_git_revert,
                                       test_a_conflicting_trade_back_revert_is_a_note
 (9) loop.trade_back: off              test_off_is_the_scoring_of_before, test_config_reads_off_and_revert
(10) revert_on_regression: false       test_without_revert_on_regression_the_trade_back_stays_and_is_counted
(11) build-level oscillation           test_the_build_level_oscillation_check_still_sees_a_shape_this_rule_does_not
(12) the journal event                 test_the_journal_event_names_both_attempts_and_cases
plus: a bookkeeping failure never raises into the loop (test_a_bookkeeping_failure_falls_back_to_the_old_scoring);
condition (b) (test_condition_b_*); the review's reproductions (test_review_*); every test pins `loop.trade_back`
(each test sets the mode it is about)."""
from __future__ import annotations

import pytest

from sbmig.core.config import LoopConfig
from sbmig.core.models import (
    BlockReason,
    BuildError,
    BuildResult,
    DecisionKind,
    ErrorKind,
    FileChange,
    FixAttempt,
    Phase,
    PhaseResult,
    TriageDecision,
)
from sbmig.fix import ContextPacker
from sbmig.fix.context import summarize_script_attempt
from sbmig.loop import fixloop
from sbmig.loop.fixloop import _pair, _Pending

from .fakes import (
    FakeFixer,
    FakeKB,
    bad_build,
    case_of,
    compile_error,
    journal_events,
    ok_build,
    write_script,
)
from .test_v21v_y_prompt_facts import BAR, FOO, make_loop

BAZ = "src/main/java/com/x/Zed.java"   # sorts after Foo: case Y is tried first in a round
QUX = "src/main/java/com/x/Qux.java"
L2X, L2Y, L2Z = ("compile|cannot_find_symbol|BASE64Decoder", "compile|exception_never_thrown|IOException",
                 "compile|has_private_access|Zork")   # rank and id: tried after Y
CX, CY, CZ = case_of(L2X), case_of(L2Y), case_of(L2Z)
OLD_TITLE = "Previous attempts on this case (they did NOT resolve it — do not repeat them)"


def err_x(file: str = FOO, sig: str | None = None, line: int | None = 1) -> BuildError:
    return compile_error(file, "cannot find symbol", l2=L2X, line=line, sig=sig)


def err_y(file: str = FOO) -> BuildError:
    return compile_error(file, "exception java.io.IOException is never thrown", l2=L2Y, line=1)


def err_z(file: str = BAZ) -> BuildError:
    return compile_error(file, "zork has private access", l2=L2Z, line=1)


def build_foo(w, call):
    """Foo `broken` → X; Foo `half` → Y (what the fix of X leaves); Baz `zbroken` → Z; else success."""
    f = w.read(FOO)
    errs = []
    if "broken" in f and "zbroken" not in f:
        errs.append(err_x())
    if "half" in f:
        errs.append(err_y())
    if w.exists(BAZ) and "zbroken" in w.read(BAZ):
        errs.append(err_z())
    return bad_build(*errs) if errs else ok_build()


class ByCase(FakeFixer):
    """Answers per case from a list (the last answer repeats); records (case id, ctx, cluster) per call."""

    def __init__(self, answers: dict[str, list]):
        super().__init__()
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[tuple[str, object, object]] = []

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        cid = cluster.case_id or cluster.id
        self.calls.append((cid, ctx, cluster))
        lst = self.answers[cid]
        files = lst.pop(0) if len(lst) > 1 else lst[0]
        return write_script(files)


def start(ws, **files: str) -> None:
    ws.write(FOO, files.get("foo", "class Foo { broken }\n"))
    for path, key in ((BAZ, "baz"), (QUX, "qux")):
        if key in files:
            ws.write(path, files[key])
    ws.commit("as-is")


def events(ws, name: str) -> list[dict]:
    return [e for e in journal_events(ws) if e.get("event") == name]


def tracking(build_fn, seen: list[str]):
    def fn(w, call):
        seen.append(w.read(FOO))
        return build_fn(w, call)
    return fn


# ------------------------------------------------------------------------------------------- (1) X → Y → undo
def test_the_undo_is_regressed_reverted_shown_and_credited_as_a_failure(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws)
    kb = FakeKB()
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]})
    seen: list[str] = []
    loop = make_loop(cfg, ws, fixer, build=tracking(build_foo, seen), kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert pr.ok
    a, b = pr.attempts[0], pr.attempts[1]
    assert (a.script_case_id, b.script_case_id) == (CX, CY)
    assert a.outcome == "improved"
    assert b.outcome == "reverted"                                     # regressed (trade-back) → reverted
    assert [_pair(e) for e in b.introduced_errors] == [_pair(err_x())]  # introduced_errors name X
    assert any("trade-back" in n and a.id in n and CX in n for n in b.notes)
    # builds: X, Y (A), X (B), the reverted tree = the one with A (Y), success
    assert seen[:4] == ["class Foo { broken }\n", "class Foo { half }\n", "class Foo { broken }\n",
                        "class Foo { half }\n"]
    # Y's next prompt lists the undo with its `introduced:` line
    cid, ctx, cluster = fixer.calls[2]
    assert cid == CY and b in ctx.previous_attempts
    summary = summarize_script_attempt(b)
    assert "outcome: reverted" in summary and "introduced: " in summary and "cannot find symbol" in summary
    packed = dict(ContextPacker(cfg).pack_script(ws, cluster, ctx).sections)
    assert summary.splitlines()[0] in packed[OLD_TITLE] and "    introduced: " in packed[OLD_TITLE]
    # KB: the undo is a failure, A stays a success
    credits = {(c, v): r for c, v, r, _p in kb.cases.credits}
    assert credits[(CX, a.script_version)] == "improved"
    assert credits[(CY, b.script_version)] == "reverted"
    assert not any(c == CX and r != "improved" for c, _v, r, _p in kb.cases.credits)
    assert kb.cases.find(CY).scripts[0].failures == 1
    # the revert of the HEAD commit, the trade-back event
    tb = events(ws, "trade_back")
    assert len(tb) == 1 and tb[0]["attempt"] == b.id and tb[0]["resolved_by"] == [a.id]
    assert any(e.get("attempts") == [b.id] for e in events(ws, "revert"))


# ------------------------------------------------------------------------------- (2) Y fixed properly
def test_a_proper_fix_of_y_is_improved_and_nothing_is_counted(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws, baz="class Zed { zbroken }\n")
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { ok }\n"}],
                    CZ: [{BAZ: "class Zed { zbroken 1 }\n"}, {BAZ: "class Zed { zbroken 2 }\n"},
                         {BAZ: "class Zed { }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert pr.ok
    y = next(a for a in pr.attempts if a.script_case_id == CY)
    assert y.outcome == "improved"                                     # Z was still in that build
    assert not events(ws, "trade_back") and not loop._trade_backs and not loop._trade_back_blocks
    assert not any("trade-back" in n for a in pr.attempts for n in a.notes)


# ------------------------------------------------------------------------------- unit level: `_score` directly
def _att(ws, aid: str, l2: str, errs: list[BuildError], files: list[str], *, outcome: str = "pending",
         base: str | None = None, commit: str | None = None) -> FixAttempt:
    cid = case_of(l2)
    return FixAttempt(id=aid, phase=Phase.VERIFY, iteration=1, cluster_id=f"case:{cid}",
                      decision=TriageDecision(cluster_id=f"case:{cid}", kind=DecisionKind.SCRIPT), applied=True,
                      changes=[FileChange(path=f, op="create", content="") for f in files], cluster_errors=errs,
                      script_case_id=cid, script_mode="new", script_version=1, outcome=outcome,
                      base_commit=base, commit=commit, errors_before=1)


def setup_undo(cfg, ws, **kw):
    """The tree after A (X resolved, kept) and B (case Y, back to `broken`); B pending; A recorded as X's resolver."""
    start(ws)
    s0 = ws.head()
    ws.write(FOO, "class Foo { half }\n")
    s1 = ws.commit("A")
    ws.write(FOO, "class Foo { broken }\n")
    s2 = ws.commit("B")
    loop = make_loop(cfg, ws, FakeFixer(), build=build_foo, kb=FakeKB(), **kw)
    a = _att(ws, "verify-1-1", L2X, [err_x()], [FOO], outcome="improved", base=s0, commit=s1)
    b = _att(ws, "verify-2-1", L2Y, [err_y()], [FOO], base=s1, commit=s2)
    loop._resolved_by[_pair(err_x())] = a
    loop._resolved_lines[_pair(err_x())] = frozenset({1})
    return loop, a, b, _Pending(b, s1, s2), PhaseResult(phase=Phase.VERIFY)


def test_errors_a_rollback_brought_back_are_baseline_not_a_trade_back(cfg, ws):
    """(3) X is back only because a rollback before this try brought it back (`restored`): baseline, not new."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_undo(cfg, ws)
    reverted = loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()), restored=[err_x()])
    assert not reverted and b.outcome == "improved" and not events(ws, "trade_back")


def test_a_later_resolution_replaces_the_entry(cfg, ws):
    """(4) The entry of a pair names the latest attempt that resolved it: B (case Y) brought X back and stayed
    (revert_on_regression off), E (case X) resolves X again → the entry is E, and an undo of E is judged by E's diff.
    An `improved` attempt is never rolled back or reverted afterwards (NO_ROLLBACK, the revert groups need every
    attempt regressed), so in production entries are only ever replaced; `_still_in_tree` stays as a guard."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    cfg.loop.revert_on_regression = False
    loop, a, b, p, pr = setup_undo(cfg, ws)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "regressed" and loop._resolved_by[_pair(err_x())] is a
    s2 = ws.head()
    ws.write(FOO, "class Foo { half again }\n")
    s3 = ws.commit("E")
    e = _att(ws, "verify-3-1", L2X, [err_x()], [FOO], base=s2, commit=s3)
    loop._score(pr, [_Pending(e, s2, s3)], bad_build(err_y()), bad_build(err_x()))
    assert e.outcome == "improved" and loop._resolved_by[_pair(err_x())] is e


def test_the_same_text_in_another_file_is_not_a_trade_back(cfg, ws):
    """(5) X's signature text comes back in Bar (also touched), not in Foo where A resolved it: another pair."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, a, b, p, pr = setup_undo(cfg, ws)
    sig = "compile|cannot_find_symbol|class Gone"                       # file-less L1, like the real parser
    loop._resolved_by = {_pair(err_x(FOO, sig)): a}
    loop._resolved_lines = {_pair(err_x(FOO, sig)): frozenset({1})}
    b.changes.append(FileChange(path=BAR, op="create", content=""))
    loop._score(pr, [p], bad_build(err_x(BAR, sig)), bad_build(err_y()))
    assert b.outcome == "improved"                                      # its targets are gone: scored as before
    assert not any("trade-back" in n for n in b.notes) and not events(ws, "trade_back")


def test_an_error_that_is_new_elsewhere_with_x_resolved_is_still_scored_as_before(cfg, ws):
    """(5) the brought-back error is not in a file the attempt touched: not this attempt's trade-back."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_undo(cfg, ws)
    b.changes = [FileChange(path=BAR, op="create", content="")]
    b.cluster_errors = [err_y(BAR)]
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y(BAR)))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_an_error_never_resolved_in_this_run_is_not_a_trade_back(cfg, ws):
    """(5) X was never resolved in this run (no entry): a new error in a file whose targets are gone = improved."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_undo(cfg, ws)
    loop._resolved_by.clear()
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_the_resolvers_own_case_is_not_a_trade_back(cfg, ws):
    """(5) A later try of case X itself that brings back what an earlier try of X resolved: not ANOTHER case."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, a, b, p, pr = setup_undo(cfg, ws)
    b.cluster_id, b.script_case_id = a.cluster_id, a.script_case_id
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_a_build_that_cannot_tell_gives_no_trade_back_verdict(cfg, ws):
    """(6) timed out / failed without parsed errors / stopped at an earlier stage: inconclusive, never a trade-back."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_undo(cfg, ws)
    timed = bad_build(err_x()).model_copy(update={"timed_out": True})
    loop._score(pr, [p], timed, bad_build(err_y()))
    assert b.outcome == "inconclusive"
    b.outcome, b.notes = "pending", []
    loop._score(pr, [p], bad_build(), bad_build(err_y()))
    assert b.outcome == "inconclusive"
    b.outcome, b.notes = "pending", []
    early = BuildError(kind=ErrorKind.POM_INVALID, message="Non-parseable POM", file="pom.xml",
                       signature="pom_invalid|x", signature_generic="pom_invalid")
    loop._score(pr, [p], bad_build(early), bad_build(err_y()))
    assert b.outcome == "inconclusive"
    assert not events(ws, "trade_back") and not loop._trade_backs


# ------------------------------------------------------------------------------- (7) two trade-backs → blocked
def test_two_trade_backs_block_the_case_that_brings_errors_back(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws, baz="class Zed { zbroken }\n")
    z_tries: list[int] = []

    def build(w, call):
        return build_foo(w, call)

    class Fixer(ByCase):
        def propose_script(self, ws_, cluster, ctx):
            if (cluster.case_id or cluster.id) == CZ:
                z_tries.append(len(self.calls))
            return super().propose_script(ws_, cluster, ctx)

    fixer = Fixer({CX: [{FOO: "class Foo { half }\n"}],
                   CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { broken }  \n"},
                        {FOO: "class Foo { broken }   \n"}],
                   CZ: [{BAZ: f"class Zed {{ zbroken {i} }}\n"} for i in range(4)] + [{BAZ: "class Zed { }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 12)
    ys = [a for a in pr.attempts if a.script_case_id == CY]
    assert [a.outcome for a in ys[:2]] == ["reverted", "reverted"]
    tb = events(ws, "trade_back")
    assert [e["count"] for e in tb] == [1, 2] and {e["resolved_case"] for e in tb} == {CX}
    blocked = [b for b in pr.blocked if b.reason == BlockReason.OSCILLATION]
    assert len(blocked) == 1
    item = blocked[0]
    assert all(e.signature_generic == L2Y for e in item.errors)        # case Y is blocked, not X
    ev = next(n for n in item.notes if "trade-back" in n)
    assert CY in ev and CX in ev and pr.attempts[0].id in ev            # both cases and the attempt undone
    assert "kept in the tree" not in ev and f"undid the fix of case {CX}" in ev
    assert "class Foo { half }" in ws.read(FOO)                         # X stays resolved (A is in the tree)
    assert len(ys) == 2                                                 # no third try of Y
    # the loop went on with Z after the block and fixed it
    blk = next(e for e in events(ws, "blocked") if e["reason"] == "oscillation")
    assert any(a.script_case_id == CZ and a.iteration >= blk["iteration"] for a in pr.attempts)
    assert "zbroken" not in ws.read(BAZ)
    assert not events(ws, "oscillation")                                # the build-level check was not needed


# ------------------------------------------------------------------------------- (8) not at HEAD
def test_a_trade_back_behind_a_kept_commit_is_reverted_by_git_revert(cfg, ws):
    """A leaves Y in Foo and Z in Baz; in the next round B (case Y) undoes A in Foo and D (case Z) fixes Baz and is
    kept at HEAD: B is reverted by `git revert`, D stays."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws)
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n", BAZ: "class Zed { zbroken }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}],
                    CZ: [{BAZ: "class Zed { }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 6)
    assert pr.ok
    _a, b, d = pr.attempts[0], pr.attempts[1], pr.attempts[2]
    assert (b.script_case_id, d.script_case_id) == (CY, CZ) and b.iteration == d.iteration
    assert b.outcome == "reverted" and d.outcome == "improved"
    assert any("git revert: trade-back" in n for n in b.notes)
    rev = [e for e in events(ws, "revert") if e.get("mode") == "git-revert"]
    assert len(rev) == 1 and rev[0]["attempts"] == [b.id] and rev[0]["trade_back"] is True
    assert rev[0]["structural"] == []
    assert ws.read(BAZ) == "class Zed { }\n"                            # D's fix stayed


def test_a_conflicting_trade_back_revert_is_a_note(cfg, ws):
    """B (case Y) undoes A in Foo and changes Qux; D (case Z, kept at HEAD) changes the same Qux line: the revert of
    B conflicts → aborted, a note, B stays `regressed` in the tree (counted, journaled), nothing raises."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws, qux="class Qux { q }\n")
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n", BAZ: "class Zed { zbroken }\n"}],
                    CY: [{FOO: "class Foo { broken }\n", QUX: "class Qux { b }\n"}],
                    CZ: [{BAZ: "class Zed { }\n", QUX: "class Qux { d }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 2)
    b, d = pr.attempts[1], pr.attempts[2]
    assert (b.script_case_id, d.script_case_id) == (CY, CZ)
    assert d.outcome == "improved"
    assert b.outcome == "regressed"
    assert any(n.startswith("trade-back revert of ") and "failed" in n for n in b.notes)
    assert not [e for e in events(ws, "revert") if e.get("mode") == "git-revert"]
    assert [e["outcome"] for e in events(ws, "trade_back")] == ["regressed"]
    assert not ws.is_dirty()                                            # the revert was aborted cleanly
    assert pr.iterations >= 3                                           # the loop went on to its final build


# ------------------------------------------------------------------------------- (9) off
def test_off_is_the_scoring_of_before(cfg, ws):
    cfg.loop.trade_back = "off"                                         # pinned: the mode this test is about
    start(ws)
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}], CY: [{FOO: "class Foo { broken }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build_foo, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 3)
    b = pr.attempts[1]
    assert b.script_case_id == CY and b.outcome == "improved"          # the round trip is rewarded, as before
    assert not b.introduced_errors and not any("trade-back" in n for n in b.notes)
    assert not events(ws, "trade_back") and not loop._resolved_by and not loop._resolved_lines and not loop._trade_backs
    assert not any(e.get("trade_back") for e in events(ws, "revert"))


def test_config_reads_off_and_revert():
    assert LoopConfig().trade_back == "observe"                        # the default
    assert LoopConfig(trade_back="Revert").trade_back == "revert"
    assert LoopConfig(trade_back=False).trade_back == "off"            # YAML 1.1 reads a bare `off` as false
    assert LoopConfig(trade_back="OFF").trade_back == "off"
    assert LoopConfig(trade_back=True).trade_back == "revert"


# ------------------------------------------------------------------------------- (10) revert_on_regression: false
def test_without_revert_on_regression_the_trade_back_stays_and_is_counted(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    cfg.loop.revert_on_regression = False
    start(ws)
    seen: list[str] = []
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}], CY: [{FOO: "class Foo { broken }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=tracking(build_foo, seen), kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 2)
    b = pr.attempts[1]
    assert b.outcome == "regressed" and any("trade-back" in n for n in b.notes)
    assert [e["count"] for e in events(ws, "trade_back")] == [1]
    assert not events(ws, "revert")
    assert seen[2] == "class Foo { broken }\n" and len(seen) >= 3       # B stayed in the tree


# ------------------------------------------------------------------------------- (11) the build-level backstop
def test_the_build_level_oscillation_check_still_sees_a_shape_this_rule_does_not(cfg, ws):
    """X is reported in Foo, Y in Zed, both caused by Bar: A (case X) changes Bar (`v2`: X gone, Y in Zed), B (case Y)
    changes Bar back (`v1`: Y gone, X back in Foo). Neither touched the file its errors came back in → no
    trade-back; the build-level oscillation check blocks. (Y reported in Bar itself would be seen by the rule on
    the second cycle: X's next try touches Bar and brings back what B resolved there.)"""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    ws.write(FOO, "class Foo { }\n")
    ws.write(BAR, "class Bar { v1 }\n")
    ws.commit("as-is")

    def build(w, call):
        bar = w.read(BAR)
        if "v1" in bar:
            return bad_build(err_x())
        if "v2" in bar:
            return bad_build(err_y(BAZ))
        return ok_build()

    fixer = ByCase({CX: [{BAR: f"class Bar {{ v2 /* {i} */ }}\n"} for i in range(8)],
                    CY: [{BAR: f"class Bar {{ v1 /* {i} */ }}\n"} for i in range(8)]})
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert not events(ws, "trade_back")
    assert len(events(ws, "oscillation")) >= 2
    assert any(b.reason == BlockReason.OSCILLATION for b in pr.blocked)


# ------------------------------------------------------------------------------- (12) the journal event
def test_the_journal_event_names_both_attempts_and_cases(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, a, b, p, pr = setup_undo(cfg, ws)
    assert loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "reverted" and ws.read(FOO) == "class Foo { half }\n"
    (ev,) = events(ws, "trade_back")
    assert ev == {**ev, "event": "trade_back", "phase": "verify", "attempt": b.id, "case": CY,
                  "resolved_by": [a.id], "resolved_case": CX, "count": 1, "limit": 2, "outcome": "reverted",
                  "mode": "revert", "at_limit": False}
    assert len(ev["errors"]) == 1 and "cannot find symbol" in ev["errors"][0]
    scored = [e for e in events(ws, "attempt_scored") if e["attempt"] == b.id]
    assert scored[-1]["outcome"] == "reverted"


def test_a_bookkeeping_failure_falls_back_to_the_old_scoring(cfg, ws, monkeypatch, caplog):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_undo(cfg, ws)

    def boom(self, x):
        raise RuntimeError("broken bookkeeping")

    monkeypatch.setattr(fixloop.BuildFixLoop, "_still_in_tree", boom)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "improved"
    assert "trade-back check for" in caplog.text


# ------------------------------------------------------------------------------- condition (b): B undid A's change
JFOO = ("class Foo {\n"
        "    String decode(String s) {\n"
        "        try {\n"
        "            return legacyDecode(s);\n"
        "        } catch (IOException e) {\n"
        "            return null;\n"
        "        }\n"
        "    }\n"
        "}\n")
JA = JFOO.replace("return legacyDecode(s);", "return new String(Base64.getDecoder().decode(s));")


def setup_lines(cfg, ws, b_text: str, a_text: str = JA, asis: str = JFOO, line: int | None = 4):
    """As-is → A (case X, kept) → B (case Y, `b_text`) in Foo; B pending; X was reported on `line` before A."""
    ws.write(FOO, asis)
    s0 = ws.commit("as-is")
    ws.write(FOO, a_text)
    s1 = ws.commit("A")
    ws.write(FOO, b_text)
    s2 = ws.commit("B")
    loop = make_loop(cfg, ws, FakeFixer(), build=build_foo, kb=FakeKB())
    a = _att(ws, "verify-1-1", L2X, [err_x(line=line)], [FOO], outcome="improved", base=s0, commit=s1)
    b = _att(ws, "verify-2-1", L2Y, [err_y()], [FOO], base=s1, commit=s2)
    loop._resolved_by[_pair(err_x())] = a
    loop._resolved_lines[_pair(err_x())] = frozenset({line}) if line else frozenset()
    return loop, a, b, _Pending(b, s1, s2), PhaseResult(phase=Phase.VERIFY)


def x_on(line: int | None):
    """The build after B: X reported on `line` (None = without a line)."""
    return bad_build(err_x(line=line))


def test_condition_b_an_undo_that_restores_the_old_lines_is_a_trade_back(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, JFOO)
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "reverted"
    assert any("put back the lines attempt verify-1-1" in n and FOO in n for n in b.notes)
    assert ws.read(FOO) == JA


def test_condition_b_rewriting_a_line_a_added_is_not_an_undo(cfg, ws):
    """B replaces A's new line by another one without putting the as-is line back: the hunk is not reversed."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, JA.replace("            return new String(Base64.getDecoder().decode(s));\n",
                                                        "            return decodeLater(s);\n"))
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_condition_b_whitespace_only_differences_are_the_same_line(cfg, ws):
    """B restores the as-is line with other indentation and trailing blanks: still the line A had removed."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, JFOO.replace("            return legacyDecode(s);\n",
                                                          "\treturn legacyDecode(s);   \n"))
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "reverted"


def test_condition_b_a_follow_up_elsewhere_in_the_file_is_not_an_undo(cfg, ws):
    """B touches Foo without undoing A (another method), and the same (file, signature) pair appears again: not a
    trade-back, scored as before."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    two = JA + "class Other {\n    void run() { legacy(); }\n}\n"
    loop, _a, b, p, pr = setup_lines(cfg, ws, two.replace("legacy();", "readString();"), a_text=two,
                                    asis=JFOO + "class Other {\n    void run() { legacy(); }\n}\n")
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_condition_b_a_line_of_only_punctuation_does_not_count(cfg, ws):
    """A's change in Foo is a lone `}` (and B removes it again): no letter or digit → not an undo."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    asis = "class Foo {\n    void a() {\n    }\n"
    loop, _a, b, p, pr = setup_lines(cfg, ws, asis, a_text=asis + "}\n", asis=asis)
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_condition_b_an_unreadable_diff_gives_no_verdict(cfg, ws, monkeypatch, caplog):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    for how in ("missing_commit", "decoding"):
        loop, a, b, p, pr = setup_lines(cfg, ws, JFOO)
        if how == "missing_commit":
            a.commit = "0" * 40
        else:
            def undecodable(data, encodings):
                raise ValueError("the diff decodes with none of the encoding candidates")
            monkeypatch.setattr(fixloop, "_decode_strict", undecodable)
        loop._score(pr, [p], x_on(4), bad_build(err_y()))
        assert b.outcome == "improved", how
        assert "unreadable, no trade-back verdict" in caplog.text, how
        caplog.clear()
    assert not events(ws, "trade_back")


def test_condition_b_is_only_read_for_candidates_of_condition_a(cfg, ws, monkeypatch):
    """No resolved pair is back: no diff is read at all."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, JFOO)
    loop._resolved_by.clear()
    asked: list = []
    monkeypatch.setattr(fixloop.BuildFixLoop, "_diff_sides", lambda self, *args: asked.append(args) or (set(), set()))
    loop._score(pr, [p], x_on(4), bad_build(err_y()))
    assert b.outcome == "improved" and asked == []


def test_a_failed_trade_back_revert_gets_only_its_failure_note(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws, qux="class Qux { q }\n")
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n", BAZ: "class Zed { zbroken }\n"}],
                    CY: [{FOO: "class Foo { broken }\n", QUX: "class Qux { b }\n"}],
                    CZ: [{BAZ: "class Zed { }\n", QUX: "class Qux { d }\n"}]})
    pr = make_loop(cfg, ws, fixer, build=build_foo, kb=FakeKB()).run(Phase.VERIFY, "package", 2)
    b = pr.attempts[1]
    assert b.outcome == "regressed"
    assert any(n.startswith("trade-back revert of ") for n in b.notes)
    assert not any("later commits that improved things depend on it" in n for n in b.notes)


# ------------------------------------------------------------------------------- the review's reproductions
# out/scratch/trade-back-review/test_follow_up_same_pair.py: a correct fix causes a NEW `unreported exception
# java.io.IOException` in the same file; javac's L1 carries no line, so it is the pair case X had resolved.
SIG_UNREPORTED = ("compile|unreported_exception|unreported exception java.io.IOException; must be caught or declared "
                  "to be thrown||class Foo")
L2U, L2L, L2P = ("compile|unreported_exception|IOException", "compile|cannot_find_symbol|legacyDecode",
                 "compile|has_private_access|Zork")
CU, CL, CP = case_of(L2U), case_of(L2L), case_of(L2P)
FOLLOW_ASIS = ("class Foo {\n"
               "    void a() { call10(); }\n"
               "    void b() { legacy(); }\n"
               "}\n")


def _follow_build(w, call):
    f, bar = w.read(FOO), w.read(BAR)
    errs = []
    unreported = ("unreported exception java.io.IOException; must be caught or declared to be thrown")
    if "catch10" not in f:
        errs.append(BuildError(kind=ErrorKind.COMPILE, file=FOO, module="demo", line=2, message=unreported,
                               signature=SIG_UNREPORTED, signature_generic=L2U))
    if "zbroken" in bar:
        errs.append(BuildError(kind=ErrorKind.COMPILE, file=BAR, module="demo", line=1,
                               message="zork has private access", signature="compile|has_private_access|zork||class Bar",
                               signature_generic=L2P))
    if "legacy" in f and "legacy" not in bar:
        errs.append(BuildError(kind=ErrorKind.COMPILE, file=FOO, module="demo", line=3, message="cannot find symbol",
                               signature="compile|cannot_find_symbol|method legacy()|class Foo",
                               signature_generic=L2L))
    if "readString" in f and "catch40" not in f:
        errs.append(BuildError(kind=ErrorKind.COMPILE, file=FOO, module="demo", line=3, message=unreported,
                               signature=SIG_UNREPORTED, signature_generic=L2U))
    return bad_build(*errs) if errs else ok_build()


class _FollowFixer(FakeFixer):
    """U: wrap what throws (call10, and readString once it is there). L: replace legacy() by readString() (the
    correct API; it throws IOException, which is case U's business). P: the migration removes Bar.legacy."""

    def __init__(self):
        super().__init__()
        self.order: list[str] = []

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        cid = cluster.case_id or cluster.id
        self.order.append(cid)
        f = ws.read(FOO)
        if cid == CP:
            return write_script({BAR: "class Bar { }\n"})
        if cid == CU:
            if "catch10" not in f:
                f = f.replace("call10();", "try { call10(); } catch10")
            if "readString" in f and "catch40" not in f:
                f = f.replace("readString();", "try { readString(); } catch40")
        else:
            f = f.replace("legacy();", "readString();" + " " * self.order.count(cid))
        return write_script({FOO: f})


@pytest.mark.parametrize("mode", ["revert", "off"])
def test_review_a_follow_up_error_with_the_same_pair_is_not_a_trade_back(cfg, ws, mode):
    cfg.loop.trade_back = mode
    ws.write(FOO, FOLLOW_ASIS)
    ws.write(BAR, "class Bar { zbroken legacy }\n")
    ws.commit("as-is")
    pr = make_loop(cfg, ws, _FollowFixer(), build=_follow_build, kb=FakeKB()).run(Phase.VERIFY, "package", 12)
    assert pr.ok and not pr.blocked
    assert not events(ws, "trade_back")
    fix_l = next(a for a in pr.attempts if a.script_case_id == CL)
    assert fix_l.outcome == "improved"                                 # its follow-up went to case U's next try


# out/scratch/trade-back-review/test_hidden_module.py: the previous build stopped at m1 (the reactor never reached
# m2); W's correct fix of m1 makes m2 visible again and shows X back in Foo, which W touched (a call-site tidy-up)
# without undoing anything. Condition (b) clears W: Y's change, hidden behind m1, is the undo.
LIB = "m1/src/main/java/com/x/Lib.java"
L2W = "compile|has_private_access|Wonk"
CW = case_of(L2W)


def _mod_err(file, l2, msg, module):
    return BuildError(kind=ErrorKind.COMPILE, file=file, module=module, line=1, message=msg,
                      signature=f"{l2}|{file}", signature_generic=l2)


def _mod_res(errors, built, failed):
    return BuildResult(ok=not errors, goal="package", command=["mvn"], exit_code=1 if errors else 0, duration_s=0.1,
                       log_path="/tmp/b.log", errors=errors, modules_built=built, failed_module=failed)


def _hidden_build(w, call):
    lib, foo = w.read(LIB), w.read(FOO)
    if "wbroken" in lib:
        return _mod_res([_mod_err(LIB, L2W, "wonk has private access", "m1")], [], "m1")
    errs = []
    if "broken" in foo:
        errs.append(_mod_err(FOO, L2X, "cannot find symbol", "m2"))
    if "half" in foo:
        errs.append(_mod_err(FOO, L2Y, "exception java.io.IOException is never thrown", "m2"))
    return _mod_res(errs, ["m1"], "m2" if errs else None)


class _HiddenFixer(FakeFixer):
    def __init__(self):
        super().__init__()
        self.n: dict[str, int] = {}

    def propose_script(self, ws, cluster, ctx):
        cid = cluster.case_id or cluster.id
        k = self.n[cid] = self.n.get(cid, 0) + 1
        foo = ws.read(FOO)
        if cid == CX:
            return write_script({FOO: "class Foo { half }\n" if k == 1 else "class Foo { ok }\n"})
        if cid == CY:      # the culprit: undoes A in Foo AND breaks m1 (its own build cannot judge it)
            return write_script({FOO: "class Foo { broken2 }\n", LIB: "class Lib { wbroken }\n"})
        return write_script({LIB: f"class Lib {{ ok {k} }}\n", FOO: foo.rstrip("\n") + f" // w{k}\n"})


@pytest.mark.parametrize("mode", ["revert", "off"])
def test_review_the_attempt_that_makes_a_module_visible_again_is_not_blamed(cfg, ws, mode):
    cfg.loop.trade_back = mode
    ws.write(FOO, "class Foo { broken }\n")
    ws.write(LIB, "class Lib { }\n")
    ws.commit("as-is")
    pr = make_loop(cfg, ws, _HiddenFixer(), build=_hidden_build, kb=FakeKB()).run(Phase.VERIFY, "package", 12)
    w = [a for a in pr.attempts if a.script_case_id == CW]
    assert w and w[0].outcome == "improved"
    assert not any(e["case"] == CW for e in events(ws, "trade_back"))
    assert pr.ok


def test_review_the_hidden_module_blame_is_cleared_by_condition_b(cfg, ws, monkeypatch):
    """The reason W is not blamed: condition (a) holds for it (X is back in Foo, which W touched, new against the
    m1-only build) and no hunk of W reverses A's hunk (condition 3) is what says no."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    seen: list[tuple[str, bool]] = []
    real = fixloop._reverses

    def spy(g, h):
        r = real(g, h)
        seen.append(("undo", r))
        return r

    monkeypatch.setattr(fixloop, "_reverses", spy)
    ws.write(FOO, "class Foo { broken }\n")
    ws.write(LIB, "class Lib { }\n")
    ws.commit("as-is")
    pr = make_loop(cfg, ws, _HiddenFixer(), build=_hidden_build, kb=FakeKB()).run(Phase.VERIFY, "package", 12)
    assert pr.ok
    assert ("undo", False) in seen                                     # (a) held for W; (b) cleared it


# out/scratch/trade-back-review/test_stale_queue.py: a block queued in one `run` (the case had left the build because
# the trade-back's revert failed) must not reach the next `run` of the same loop (one per MSA service / target).
FOO2 = "svc2/src/main/java/com/x/Foo.java"


def test_review_a_queued_block_does_not_leak_into_the_next_run(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    start(ws, qux="class Qux { q }\n")
    state = {"svc": 1}

    def build(w, call):
        if state["svc"] == 2:
            return bad_build(err_y(FOO2)) if "fixed" not in (w.read(FOO2) if w.exists(FOO2) else "") else ok_build()
        return build_foo(w, call)

    fixer = ByCase({CX: [{FOO: "class Foo { half }\n", BAZ: "class Zed { zbroken }\n"}, {FOO: "class Foo { ok }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { broken }  \n", QUX: "class Qux { b }\n"},
                         {FOO2: "class Foo { fixed }\n"}],
                    CZ: [{BAZ: "class Zed { zbroken 1 }\n"}, {BAZ: "class Zed { }\n", QUX: "class Qux { d }\n"}]})
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    loop.run(Phase.VERIFY, "package", 10)
    assert loop._trade_back_blocks                                     # precondition: queued after run 1
    state["svc"] = 2
    ws.write(FOO2, "class Foo { legacy }\n")
    ws.commit("svc2")
    pr2 = loop.run(Phase.VERIFY, "package", 10, target="svc2")
    assert pr2.ok and not any(b.reason == BlockReason.OSCILLATION for b in pr2.blocked)
    assert not loop._trade_backs and not loop._resolved_by             # cleared at the start of run 2


# ------------------------------------------------------------------------------- whole-hunk reversal
TWO = ("class Foo {\n"
       "    String a() {\n"
       "        return legacyDecode(s);\n"
       "    }\n"
       "\n"
       "    String b() {\n"
       "        return null;\n"
       "    }\n"
       "}\n")
TWO_A = TWO.replace("return legacyDecode(s);", "return Base64.decode(s);").replace("return null;", "return fallback();")


def test_condition_b_a_matching_line_without_the_rest_of_its_hunk_is_not_an_undo(cfg, ws):
    """B adds `return null;` (a line A removed in its second hunk) in another method, while `return fallback();`
    (that hunk's added line) stays: no hunk of A is reversed."""
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    b_text = TWO_A.replace("    String b() {\n", "    String c() {\n        return null;\n    }\n\n    String b() {\n")
    loop, _a, b, p, pr = setup_lines(cfg, ws, b_text, a_text=TWO_A, asis=TWO, line=7)
    loop._score(pr, [p], x_on(7), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_condition_b_reversing_one_of_two_hunks_completely_is_a_trade_back(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    b_text = TWO_A.replace("return Base64.decode(s);", "return legacyDecode(s);")    # hunk 1 back, hunk 2 kept
    loop, _a, b, p, pr = setup_lines(cfg, ws, b_text, a_text=TWO_A, asis=TWO, line=3)
    loop._score(pr, [p], x_on(3), bad_build(err_y()))
    assert b.outcome == "reverted" and len(events(ws, "trade_back")) == 1


ADD_ASIS = "class Foo {\n    void run() {\n        work();\n    }\n}\n"
ADD_A = ADD_ASIS.replace("        work();\n", "        work();\n        init();\n        check(state);\n")


def test_condition_b_deleting_all_lines_of_a_pure_addition_is_a_trade_back(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, ADD_ASIS + "// tidy\n", a_text=ADD_A, asis=ADD_ASIS, line=3)
    loop._score(pr, [p], x_on(3), bad_build(err_y()))
    assert b.outcome == "reverted"


def test_condition_b_deleting_some_lines_of_a_pure_addition_is_not(cfg, ws):
    cfg.loop.trade_back = "revert"                                      # the mode this test is about
    loop, _a, b, p, pr = setup_lines(cfg, ws, ADD_A.replace("        check(state);\n", ""), a_text=ADD_A,
                                    asis=ADD_ASIS, line=3)
    loop._score(pr, [p], x_on(3), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_condition_b_hunks_are_read_per_hunk():
    diff = ("diff --git a/F.java b/F.java\n--- a/F.java\n+++ b/F.java\n"
            "@@ -3 +3 @@\n-        return legacyDecode(s);\n+        return Base64.decode(s);\n"
            "@@ -7 +7,2 @@\n-  return null;\n+  return fallback();\n+    }\n"
            "@@ -9,0 +11 @@\n+\n")
    h = fixloop._diff_hunks(diff)
    assert [(x.old_start, x.old_len, x.new_start, x.new_len) for x in h] == [(3, 1, 3, 1), (7, 1, 7, 2), (9, 0, 11, 1)]
    assert (h[0].added, h[0].removed) == (frozenset({"return Base64.decode(s);"}), frozenset({"return legacyDecode(s);"}))
    assert (h[1].added, h[1].removed) == (frozenset({"return fallback();"}), frozenset({"return null;"}))
    assert (h[2].added, h[2].removed) == (frozenset(), frozenset())        # blank / punctuation only
    back = fixloop._DiffHunk(3, 1, 3, 1, frozenset({"return legacyDecode(s);"}), frozenset({"return Base64.decode(s);"}),
                             ((3, "return legacyDecode(s);"),))
    assert fixloop._reverses(back, h[0]) and not fixloop._reverses(back, h[1])
    assert not fixloop._reverses(back, h[2])                                 # never reversed: nothing countable
    assert h[1].added_at == ((7, "return fallback();"),)                     # `}` takes a line number, is not listed
    assert fixloop._put_back_at(back, h[0], 3) and not fixloop._put_back_at(back, h[0], 4)


def test_git_insertion_convention_is_the_line_above_the_inserted_lines():
    """`@@ -3,0 +4,2 @@`: two lines inserted AFTER old line 3. The range is the single position 3 (the line above the
    insertion; 0 = before the first line): an error on old line 3 is covered, one on old line 4 is not. The same rule
    for the new side of a pure deletion (`+l,0`)."""
    (h,) = fixloop._diff_hunks("@@ -3,0 +4,2 @@\n+        init();\n+        check(state);\n")
    assert fixloop._covers(h.old_start, h.old_len, 3) and not fixloop._covers(h.old_start, h.old_len, 4)
    assert fixloop._covers(h.new_start, h.new_len, 4) and fixloop._covers(h.new_start, h.new_len, 5)
    assert not fixloop._covers(h.new_start, h.new_len, 6)
    (d,) = fixloop._diff_hunks("@@ -4,2 +3,0 @@\n-        init();\n-        check(state);\n")
    assert fixloop._covers(d.new_start, d.new_len, 3) and not fixloop._covers(d.new_start, d.new_len, 4)
    assert fixloop._reverses(d, h)


# ------------------------------------------------------------------------------- the four conditions, the modes
def test_the_default_mode_is_observe():
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parents[2]
    assert LoopConfig().trade_back == "observe"
    assert yaml.safe_load((root / "config" / "default.yml").read_text(encoding="utf-8"))["loop"]["trade_back"] == "observe"


def _run_undo(cfg, ws, mode: str):
    """Scenario (1): A (X) leaves Y, B (Y) restores A's hunk, then Y's proper fix (if asked)."""
    cfg.loop.trade_back = mode
    start(ws)
    kb = FakeKB()
    fixer = ByCase({CX: [{FOO: "class Foo { half }\n"}],
                    CY: [{FOO: "class Foo { broken }\n"}, {FOO: "class Foo { ok }\n"}]})
    seen: list[str] = []
    pr = make_loop(cfg, ws, fixer, build=tracking(build_foo, seen), kb=kb).run(Phase.VERIFY, "package", 6)
    return pr, kb, seen


def _journal_shape(ws) -> list[tuple]:
    """The journal without times / commit ids, and without the `trade_back` events."""
    keep = ("event", "attempt", "outcome", "case", "version", "reason", "count", "iteration", "errors", "ok",
            "cluster", "attempts")
    return [tuple((k, json_safe(e.get(k))) for k in keep if k in e) for e in journal_events(ws)
            if e.get("event") != "trade_back"]


def json_safe(v):
    return tuple(v) if isinstance(v, list) else v


def _second_ws(tmp_path):
    from sbmig.core.workspace import Workspace

    from .fakes import make_project
    root = tmp_path / "second"
    root.mkdir()
    w = Workspace.from_input(make_project(root), root / "out", run_id="t-run")
    w.git_init()
    return w


def test_observe_records_the_undo_and_changes_nothing_else(cfg, ws, tmp_path):
    """observe: B stays `improved` with a note, the journal gets `trade_back` (mode observe, count, at_limit); the
    builds, outcomes, KB credits and every other journal line are those of `off`."""
    pr_o, kb_o, seen_o = _run_undo(cfg, ws, "observe")
    ws_off = _second_ws(tmp_path)
    pr_f, kb_f, seen_f = _run_undo(cfg, ws_off, "off")
    b = pr_o.attempts[1]
    assert b.script_case_id == CY and b.outcome == "improved"
    note = next(n for n in b.notes if n.startswith("trade-back observed (loop.trade_back: observe, scored as before): "))
    assert "attempt verify-1-1" in note and FOO in note
    (ev,) = events(ws, "trade_back")
    assert ev["mode"] == "observe" and ev["count"] == 1 and ev["at_limit"] is False and ev["outcome"] == "improved"
    assert not events(ws_off, "trade_back")
    assert [(a.id, a.outcome) for a in pr_o.attempts] == [(a.id, a.outcome) for a in pr_f.attempts]
    assert seen_o == seen_f and pr_o.ok == pr_f.ok and pr_o.iterations == pr_f.iterations
    assert [c[:3] for c in kb_o.cases.credits] == [c[:3] for c in kb_f.cases.credits]
    assert [r["reason"] for r in kb_o.cases.credit_runs] == [r["reason"] for r in kb_f.cases.credit_runs]
    assert _journal_shape(ws) == _journal_shape(ws_off)
    assert not loop_blocks(pr_o)


def loop_blocks(pr):
    return [x for x in pr.blocked if x.reason == BlockReason.OSCILLATION]


def test_observe_counts_and_says_when_the_limit_is_reached_but_never_blocks(cfg, ws):
    cfg.loop.trade_back = "observe"
    cfg.loop.revert_on_regression = True
    loop, _a, b, p, pr = setup_undo(cfg, ws)
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert [e["at_limit"] for e in events(ws, "trade_back")] == [False, True]
    assert b.outcome == "improved" and not loop._trade_back_blocks and ws.read(FOO) == "class Foo { broken }\n"


def test_revert_the_scripted_undo_on_the_errors_own_lines(cfg, ws):
    cfg.loop.trade_back = "revert"
    pr, _kb, _seen = _run_undo(cfg, ws, "revert")
    assert pr.ok and pr.attempts[1].outcome == "reverted"
    assert [e["mode"] for e in events(ws, "trade_back")] == ["revert"]


def test_off_reads_no_diff_for_the_rule(cfg, ws, monkeypatch):
    cfg.loop.trade_back = "off"
    asked: list = []
    monkeypatch.setattr(fixloop.BuildFixLoop, "_diff_sides", lambda self, *a: asked.append(a) or [])
    monkeypatch.setattr(fixloop.BuildFixLoop, "_trade_back_of", lambda self, *a: asked.append(a) or [])
    pr, _kb, _seen = _run_undo(cfg, ws, "off")
    assert asked == [] and pr.attempts[1].outcome == "improved"


IMPORT_ASIS = ("package com.x;\n"
               "\n"
               "import java.io.IOException;\n"
               "\n"
               "public class Foo {\n"
               "    String a(String s) {\n"
               "        try {\n"
               "            return s.trim();\n"
               "        } catch (IOException e) {\n"
               "            return null;\n"
               "        }\n"
               "    }\n"
               "\n"
               "    String c(String s) {\n"
               "        try {\n"
               "            return s.strip();\n"
               "        } catch (IOException e) {\n"
               "            return \"\";\n"
               "        }\n"
               "    }\n"
               "}\n")
IMPORT_A = IMPORT_ASIS.replace("import java.io.IOException;\n\n", "").replace(
    "        try {\n            return s.trim();\n        } catch (IOException e) {\n            return null;\n"
    "        }\n", "        return s.trim();\n")
IMPORT_B = IMPORT_A.replace("package com.x;\n\n", "package com.x;\n\nimport java.io.IOException;\n\n")


def test_a_reversed_hunk_that_does_not_cover_the_errors_line_is_not_a_trade_back(cfg, ws):
    """The import case with the loop fakes (the javac shape of the second review): A removed the import (hunk 1) and
    fixed a() (hunk 2); X's errors were on lines 9 and 17 (the two catch clauses; line 17 was only hidden). B puts the
    import back: it reverses hunk 1, which covers neither line; X is back on c()'s catch."""
    cfg.loop.trade_back = "revert"
    loop, _a, b, p, pr = setup_lines(cfg, ws, IMPORT_B, a_text=IMPORT_A, asis=IMPORT_ASIS, line=9)
    loop._resolved_lines[_pair(err_x())] = frozenset({9, 17})
    loop._score(pr, [p], x_on(15), bad_build(err_y()))                  # c()'s catch in B's tree
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_reversing_lines_spread_over_two_hunks_of_b_is_not_a_trade_back(cfg, ws):
    """A replaced `alpha(); beta();` by `gamma();`. B removes gamma and puts alpha back there (hunk 1) and adds
    beta() in another method (hunk 2): no ONE hunk of B reverses A's hunk."""
    cfg.loop.trade_back = "revert"
    asis = ("class Foo {\n    void a() {\n        alpha();\n        beta();\n    }\n\n    void b() {\n"
            "        work();\n    }\n}\n")
    a_text = asis.replace("        alpha();\n        beta();\n", "        gamma();\n")
    b_text = a_text.replace("        gamma();\n", "        alpha();\n").replace("        work();\n",
                                                                               "        work();\n        beta();\n")
    loop, _a, b, p, pr = setup_lines(cfg, ws, b_text, a_text=a_text, asis=asis, line=3)
    loop._score(pr, [p], x_on(3), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


def test_the_error_back_on_a_line_outside_bs_hunk_is_not_a_trade_back(cfg, ws):
    cfg.loop.trade_back = "revert"
    loop, _a, b, p, pr = setup_lines(cfg, ws, JFOO)                     # B restores A's hunk (line 4) exactly
    loop._score(pr, [p], x_on(6), bad_build(err_y()))                   # … but X is reported on line 6
    assert b.outcome == "improved" and not events(ws, "trade_back")
    loop2, _a2, b2, p2, pr2 = setup_lines(cfg, ws, JFOO)
    loop2._score(pr2, [p2], x_on(4), bad_build(err_y()))                # on the restored line: a trade-back
    assert b2.outcome == "reverted"


def test_an_error_without_a_line_gives_no_verdict(cfg, ws):
    cfg.loop.trade_back = "revert"
    loop, _a, b, p, pr = setup_lines(cfg, ws, JFOO)
    loop._score(pr, [p], x_on(None), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")
    loop2, _a2, b2, p2, pr2 = setup_lines(cfg, ws, JFOO, line=None)       # resolved without a line
    loop2._score(pr2, [p2], x_on(4), bad_build(err_y()))
    assert b2.outcome == "improved"


def test_an_unreadable_diff_of_one_file_keeps_the_verdict_of_another(cfg, ws, monkeypatch, caplog):
    """B undid A in Foo and in Bar; Bar's diff does not read: the verdict for Foo stands, Bar is logged."""
    cfg.loop.trade_back = "revert"
    loop, a, b, p, pr = setup_undo(cfg, ws)
    xb = err_x(BAR)
    loop._resolved_by[_pair(xb)] = a
    loop._resolved_lines[_pair(xb)] = frozenset({1})
    b.changes.append(FileChange(path=BAR, op="create", content=""))
    real = fixloop.BuildFixLoop._diff_sides

    def sides(self, base, head, rel):
        if rel == BAR:
            raise ValueError("the diff decodes with none of the encoding candidates")
        return real(self, base, head, rel)

    monkeypatch.setattr(fixloop.BuildFixLoop, "_diff_sides", sides)
    loop._score(pr, [p], bad_build(err_x(), xb), bad_build(err_y()))
    assert b.outcome == "reverted"
    (ev,) = events(ws, "trade_back")
    assert len(ev["errors"]) == 1 and FOO in ev["errors"][0]
    assert f"diff of {BAR} unreadable" in caplog.text


def test_a_pre_try_and_the_llm_tries_of_one_case_share_one_count(cfg, ws):
    """out/scratch/trade-back-review2/test_case_key_split.py: a zero-LLM pre-try has no script case id (its key was
    `case:<id>`), the LLM tries of the same case are keyed `<id>`: one count, the limit is reached."""
    cfg.loop.trade_back = "revert"
    cfg.loop.revert_on_regression = False
    loop, a, b, p, pr = setup_undo(cfg, ws)
    b.script_case_id = ""
    loop._score(pr, [p], bad_build(err_x()), bad_build(err_y()))
    assert b.outcome == "regressed"
    loop2, _a2, b2, p2, _ = setup_undo(cfg, ws)
    loop._resolved_by = {k: a for k in loop2._resolved_by}
    b2.id = "verify-3-1"
    loop._score(pr, [p2], bad_build(err_x()), bad_build(err_y()))
    assert b2.outcome == "regressed"
    assert dict(loop._trade_backs) == {(CY, CX): 2} and CY in loop._trade_back_blocks
    assert fixloop._case_key(b) == CY == fixloop._case_key(b2)


def test_rewriting_a_line_and_adding_its_old_text_elsewhere_is_not_an_undo(cfg, ws):
    """out/scratch/trade-back-review2/test_union_of_hunks.py: B rewrites A's line (`return "";` → `return
    defaultText();`) and adds another method returning null (the line A had removed): no single hunk reverses A's."""
    cfg.loop.trade_back = "revert"
    asis = "class Foo {\n    String a() {\n        return null;\n    }\n}\n"
    a_text = asis.replace("return null;", 'return "";')
    b_text = ("class Foo {\n    String a() {\n        return defaultText();\n    }\n\n    Object find(String k) {\n"
              "        return null;\n    }\n}\n")
    loop, _a, b, p, pr = setup_lines(cfg, ws, b_text, a_text=a_text, asis=asis, line=3)
    loop._score(pr, [p], x_on(3), bad_build(err_y()))
    assert b.outcome == "improved" and not events(ws, "trade_back")


# out/scratch/trade-back-review2/test_hidden_flow_error.py with the REAL javac 21 and the real log parser: javac reports
# flow errors (`never thrown`) only when no attribution error comes first. A fixes a(), removes the import (so c()'s
# catch now hides its own `never thrown` behind `cannot find symbol class IOException`); B, the correct fix of that,
# puts the import back and c()'s error is visible again. Not a trade-back: the run ends ok.
JAVAC = "/usr/bin/javac"


def _javac_ok() -> bool:
    import os
    import subprocess
    if not os.path.exists(JAVAC):
        return False
    try:
        out = subprocess.run([JAVAC, "-version"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and " 21" in (out.stdout + out.stderr)


def _javac_build(tmp):
    import re as _re
    import subprocess

    from sbmig.build.log_parser import parse_maven_log
    pat = _re.compile(r"^(?P<f>[^:\s][^:]*\.java):(?P<l>\d+): error: (?P<m>.*)$")

    def build(w, call):
        root = w.root
        out = tmp / f"classes-{len(list(tmp.iterdir()))}"
        out.mkdir()
        pr = subprocess.run([JAVAC, "-d", str(out), FOO], cwd=root, capture_output=True, text=True, timeout=120,
                            check=False)
        if pr.returncode == 0:
            return BuildResult(ok=True, goal="package", command=["mvn"], exit_code=0, duration_s=0.1,
                               log_path="/tmp/ok", modules_built=["demo"])
        lines = ["[INFO] ---------------------< com.x:demo >---------------------",
                 "[INFO] --- compiler:3.13.0:compile (default-compile) @ demo ---",
                 "[INFO] -------------------------------------------------------------",
                 "[ERROR] COMPILATION ERROR : "]
        for ln in pr.stderr.splitlines():
            m = pat.match(ln)
            if m:
                lines.append(f"[ERROR] {root / m['f']}:[{m['l']},1] {m['m']}")
            elif ln.startswith(("  symbol:", "  location:")):
                lines.append(ln)
        lines += ["[INFO] BUILD FAILURE",
                  ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile "
                  "(default-compile) on project demo: Compilation failure")]
        return BuildResult(ok=False, goal="package", command=["mvn"], exit_code=1, duration_s=0.1, log_path="/tmp/bad",
                           errors=parse_maven_log("\n".join(lines), root), failed_module="demo")
    return build


class _KindFixer(FakeFixer):
    """X (never thrown): A first (a() fixed, the import removed), then c() fixed. Y (cannot find symbol): the import back."""

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        msg = " ".join(f"{e.signature} {e.message} {e.raw or ''}" for e in cluster.errors)
        cur = ws.read(FOO)
        if "never thrown" in msg:
            if "return s.trim();\n        } catch" in cur:
                return write_script({FOO: IMPORT_A_JAVA})
            return write_script({FOO: cur.replace("        try {\n            return s.strip();\n        } catch "
                                                  "(IOException e) {\n            return \"\";\n        }\n",
                                                  "        return s.strip();\n")})
        return write_script({FOO: cur.replace("package com.x;\n\n", "package com.x;\n\nimport java.io.IOException;\n\n", 1)
                             if "import java.io.IOException" not in cur else cur})


IMPORT_A_JAVA = IMPORT_A


@pytest.mark.skipif(not _javac_ok(), reason="JDK 21 javac not at /usr/bin/javac")
@pytest.mark.parametrize("mode", ["revert", "observe", "off"])
def test_review2_a_hidden_flow_error_made_visible_again_is_not_a_trade_back(cfg, ws, tmp_path, mode):
    cfg.loop.trade_back = mode
    ws.write(FOO, IMPORT_ASIS)
    ws.commit("as-is")
    tmp = tmp_path / "javac"
    tmp.mkdir()
    pr = make_loop(cfg, ws, _KindFixer(), build=_javac_build(tmp), kb=FakeKB()).run(Phase.VERIFY, "package", 12)
    assert pr.build_ok and not pr.blocked
    assert not events(ws, "trade_back")
