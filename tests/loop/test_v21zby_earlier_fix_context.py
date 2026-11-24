"""v2.1z-by (docs/INTERFACES.md "v2.1z-by"): what a case is told about the earlier fix its errors come from.

* a case whose errors first appeared in a file a kept attempt of ANOTHER case touched gets the section "Earlier fix these
  errors come from (it is in the tree)" right after its errors: the attempt, the errors it was fixing, its diff lines in
  this case's files, the sentences; the one-line `introduced by` hint is not given for that attempt;
* an attempt that is not in the tree (reverted, rolled back, no commit range) or whose diff does not read keeps the old
  hint and no section;
* the section is capped (`EARLIER_FIX_CHARS`), diff lines go first, the sentences stay;
* an `improved` attempt of the case itself is listed under "These errors were resolved before and are back", not under
  "they did NOT resolve it";
* journal `earlier_fix_context`; the prompt of a case without either is unchanged; rule 13 says both;
* review findings: only an attempt that resolved its own case (`improved` / `build_ok`) is described, a kept
  regressed one keeps the hint; "resolved before" needs the same (file, signature) back; `_introduced` names the
  latest attempt and forgets errors a build no longer shows; the packer splits by attempt id; a pre-try of the same
  case is not another case; case files beyond the shown ones are named as such."""
from __future__ import annotations

import logging

import pytest

from sbmig.core.interfaces import FixContext
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
from sbmig.fix import ContextPacker
from sbmig.fix.context import (
    EARLIER_FIX_TITLE,
    RESOLVED_BEFORE_TITLE,
    EarlierFix,
    earlier_fix_sentences,
    earlier_fix_text,
    format_error_line,
    summarize_script_attempt,
)
from sbmig.fix.fixer import load_script_prompts
from sbmig.loop import fixloop
from sbmig.loop.fixloop import EARLIER_FIX_CHARS, ROLLED_BACK_NOTE

from .fakes import FakeFixer, FakeKB, bad_build, case_of, compile_error, journal_events, ok_build, write_script
from .test_v21v_y_prompt_facts import BAR, FOO, make_loop

L2A, L2B = "compile|cannot_find_symbol|BASE64Decoder", "compile|exception_never_thrown|IOException"
OLD_TITLE = "Previous attempts on this case (they did NOT resolve it — do not repeat them)"


class CapturingFixer(FakeFixer):
    """FakeFixer that also keeps the cluster of each request (to pack the real prompt afterwards)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.clusters: list[ErrorCluster] = []

    def propose_script(self, ws, cluster, ctx):
        self.clusters.append(cluster)
        return super().propose_script(ws, cluster, ctx)


def build(w, call):
    """`broken` → case X (L2A); `half` → case Y (L2B), which the fix of X leaves in the same file; else success."""
    text = w.read(FOO)
    if "broken" in text:
        return bad_build(compile_error(FOO, "cannot find symbol", l2=L2A, line=1))
    if "half" in text:
        return bad_build(compile_error(FOO, "exception java.io.IOException is never thrown", l2=L2B, line=1))
    return ok_build()


def start(ws, body: str = "class Foo { broken }\n") -> None:
    ws.write(FOO, body)
    ws.commit("as-is Foo")


def remember(loop, cluster_y: ErrorCluster, a: FixAttempt) -> None:
    """What `_score` recorded while Y was in the build (the final green build forgets it)."""
    for e in cluster_y.errors:
        loop._introduced[fixloop._pair(e)] = a


def run_x_then_y(cfg, ws, first_files: dict[str, str] | None = None):
    """X is fixed by a script that leaves Y in Foo.java (and changes Bar.java); Y is then fixed."""
    start(ws)
    fixer = CapturingFixer(scripts=[write_script(first_files or {FOO: "class Foo { half }\n", BAR: "class Bar {}\n"}),
                                    write_script({FOO: "class Foo { ok }\n"})])
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 4)
    return loop, pr, fixer


# ------------------------------------------------------------------------------------------- (1) the section
def test_a_case_left_by_a_kept_fix_of_another_case_gets_the_section_after_its_errors(cfg, ws):
    loop, pr, fixer = run_x_then_y(cfg, ws)
    assert pr.ok and len(fixer.contexts) == 2
    first = pr.attempts[0]
    assert first.script_case_id == case_of(L2A) and first.outcome == "improved"
    ctx = fixer.contexts[1]
    body = ctx.earlier_fix
    assert body.startswith(f"Attempt {first.id} (script {case_of(L2A)}:v1 (new)) fixed these errors of another case "
                           "and is kept in the tree:")
    assert format_error_line(first.cluster_errors[0]) in body                   # the errors it was fixing
    assert f"What it changed in {FOO}:\n```diff\n@@ -1 +1 @@\n-class Foo {{ broken }}\n+class Foo {{ half }}\n```" in body
    assert f"It also changed (not shown): {BAR}" in body and "class Bar" not in body   # other files: named only
    assert body.endswith("\n".join(earlier_fix_sentences(1)))
    # the hint line for the same attempt is gone; X's own (first) prompt has no section
    assert not any(h.startswith("introduced by") for h in ctx.kb_hints)
    assert fixer.contexts[0].earlier_fix == "" and fixer.contexts[0].resolved_before == []
    # the packed prompt: right after "Errors of this case", never truncated
    packed = ContextPacker(cfg).pack_script(ws, fixer.clusters[1], ctx)
    assert packed.keys[packed.keys.index("errors") + 1] == "earlier_fix"
    assert dict(packed.sections)[EARLIER_FIX_TITLE] == body


# ------------------------------------------------------------------- (2) not in the tree: the old hint, no section
@pytest.mark.parametrize("how", ["reverted", "rolled_back", "rolled_back_note", "no_base_commit", "no_commit",
                                 "git_fails", "decoding_fails"])
def test_an_attempt_not_in_the_tree_or_unreadable_keeps_the_old_hint(cfg, ws, how, monkeypatch, caplog):
    loop, pr, fixer = run_x_then_y(cfg, ws)
    first, cluster_y, att_y = pr.attempts[0], fixer.clusters[1], pr.attempts[1]
    remember(loop, cluster_y, first)
    if how == "reverted":
        first.outcome = "reverted"
    elif how == "rolled_back":
        loop._rolled_back.add(id(first))
    elif how == "rolled_back_note":
        first.notes.append(ROLLED_BACK_NOTE + "the final build (it did not help)")
    elif how == "no_base_commit":
        first.base_commit = None
    elif how == "no_commit":
        first.commit = None
    elif how == "git_fails":
        first.commit = "0" * 40
    else:
        def undecodable(data, encodings):
            raise ValueError("the diff decodes with none of the encoding candidates")
        monkeypatch.setattr(fixloop, "_decode_strict", undecodable)
    before = len([e for e in journal_events(ws) if e["event"] == "earlier_fix_context"])
    with caplog.at_level(logging.DEBUG, logger="sbmig.loop"):
        text, described = loop._earlier_fix_context(cluster_y, att_y)
    assert (text, described) == ("", set())
    hints = loop._introduced_hints(cluster_y, skip=described)
    assert hints == [f"introduced by script:{case_of(L2A)}:v1 (attempt {first.id}) — prefer augmenting that script"]
    assert len([e for e in journal_events(ws) if e["event"] == "earlier_fix_context"]) == before   # no new event
    if how in ("git_fails", "decoding_fails"):
        assert any("earlier fix of" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


def test_the_section_and_the_hint_never_name_the_same_attempt(cfg, ws):
    loop, pr, fixer = run_x_then_y(cfg, ws)
    remember(loop, fixer.clusters[1], pr.attempts[0])
    text, described = loop._earlier_fix_context(fixer.clusters[1], pr.attempts[1])
    assert text and described == {id(pr.attempts[0])}
    assert loop._introduced_hints(fixer.clusters[1], skip=described) == []


# --------------------------------------------------------------------------------------------- (3) the cap
def test_a_long_diff_is_cut_to_the_cap_and_the_sentences_survive(cfg, ws, monkeypatch):
    monkeypatch.setattr(fixloop, "EARLIER_FIX_CHARS", 900)
    long_half = "class Foo { half }\n" + "".join(f"// line {i} " + "x" * 60 + "\n" for i in range(80))
    loop, pr, fixer = run_x_then_y(cfg, ws, first_files={FOO: long_half})
    body = fixer.contexts[1].earlier_fix
    assert 0 < len(body) <= 900
    assert body.endswith("\n".join(earlier_fix_sentences(1)))
    assert f"more changed line(s) in {FOO} not shown" in body
    assert body.count("```") % 2 == 0                                     # the fence is closed
    assert format_error_line(pr.attempts[0].cluster_errors[0]) in body      # the error line outlives the diff lines


def test_earlier_fix_text_drops_diff_lines_then_names_then_errors_and_fences_backticks():
    fx = EarlierFix(attempt_id="verify-1-1", name="script abc:v1 (new)",
                    error_lines=[f"- src/A.java:{i}: cannot find symbol" for i in range(3)],
                    diffs=[("src/A.java", ["@@ -1,3 +1,3 @@", "-String s = \"```\";", "+String s = \"````\";"]
                            + [f"+int v{i} = {i};" for i in range(200)], 0)],
                    other_files=["pom.xml"])
    full = earlier_fix_text([fx], 100_000)
    assert "`````diff\n" in full and "\n`````" in full                      # longer than any backtick run inside
    assert "It also changed (not shown): pom.xml" in full
    for cap in (EARLIER_FIX_CHARS, 1500, 600):
        out = earlier_fix_text([fx], cap)
        assert len(out) <= cap and out.endswith("\n".join(earlier_fix_sentences(1)))
        assert out.startswith("Attempt verify-1-1 (script abc:v1 (new))")
    tiny = earlier_fix_text([fx], 10)                                       # nothing left to drop: head + sentences
    assert tiny.split("\n")[0].startswith("Attempt verify-1-1") and tiny.endswith(earlier_fix_sentences(1)[-1])
    assert "diff" not in tiny.split("\n")[1] and "pom.xml" not in tiny
    assert earlier_fix_text([], 1000) == ""
    assert earlier_fix_sentences(2)[0] == "Undoing those changes brings the errors they fixed back."


# ------------------------------------------------------------------------- (4) resolved before and back
def test_an_improved_attempt_of_the_case_is_listed_as_resolved_before_in_an_oscillation(cfg, ws):
    """X fixed (leaves Y) → Y fixed by undoing X's change (X back) → X's prompt: the earlier fix (Y's attempt) and
    X's own improved attempt under "resolved before and are back", not under "did NOT resolve it"."""
    start(ws)
    fixer = CapturingFixer(scripts=[write_script({FOO: "class Foo { half }\n"}),
                                    write_script({FOO: "class Foo { broken }\n"}),
                                    write_script({FOO: "class Foo { ok }\n"})])
    pr = make_loop(cfg, ws, fixer, build=build, kb=FakeKB()).run(Phase.VERIFY, "package", 6)
    assert pr.ok and len(fixer.contexts) == 3
    x1, y1 = pr.attempts[0], pr.attempts[1]
    ctx = fixer.contexts[2]
    assert ctx.resolved_before == [x1] and x1 in ctx.previous_attempts      # previous_attempts stays complete
    assert ctx.earlier_fix.startswith(f"Attempt {y1.id} (")
    assert "-class Foo { half }\n+class Foo { broken }" in ctx.earlier_fix
    packed = ContextPacker(cfg).pack_script(ws, fixer.clusters[2], ctx)
    secs = dict(packed.sections)
    assert x1.id in secs[RESOLVED_BEFORE_TITLE]
    assert secs[RESOLVED_BEFORE_TITLE].startswith("An earlier attempt on this case made these errors disappear")
    assert OLD_TITLE not in secs                                            # its only earlier attempt was resolved


def attempt(aid: str, outcome: str, version: int) -> FixAttempt:
    return FixAttempt(id=aid, phase=Phase.VERIFY, iteration=1, cluster_id="case:k1",
                      decision=TriageDecision(cluster_id="case:k1", kind=DecisionKind.SCRIPT), outcome=outcome,
                      script_mode="new", script_version=version, script_case_id="k1",
                      script_result=ScriptRunResult(status="ok"))


def cluster() -> ErrorCluster:
    e = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/main/java/com/x/Foo.java", line=1,
                   signature="s1", signature_generic="compile|cannot_find_symbol|X")
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[e], case_id="k1")


def ctx_of(**kw) -> FixContext:
    return FixContext(phase=Phase.VERIFY, iteration=2, script_mode="new", case_id="k1", **kw)


def test_the_packer_splits_resolved_attempts_and_leaves_the_others_under_the_old_title(cfg, ws):
    good, bad = attempt("verify-1-1", "improved", 1), attempt("verify-2-1", "no_change", 2)
    packed = ContextPacker(cfg).pack_script(ws, cluster(), ctx_of(previous_attempts=[good, bad],
                                                                 resolved_before=[good]))
    secs = dict(packed.sections)
    assert "verify-1-1" in secs[RESOLVED_BEFORE_TITLE] and "verify-1-1" not in secs[OLD_TITLE]
    assert "verify-2-1" in secs[OLD_TITLE] and "verify-2-1" not in secs[RESOLVED_BEFORE_TITLE]
    assert packed.keys.index("resolved_before") == packed.keys.index("previous_attempts") - 1
    # the reduced-scope prompt gets the same sections
    red = ContextPacker(cfg).pack_script(ws, cluster(), ctx_of(previous_attempts=[good, bad], resolved_before=[good],
                                                              earlier_fix="Attempt a (b) fixed …", scope="jdk-jakarta"))
    assert {RESOLVED_BEFORE_TITLE, OLD_TITLE, EARLIER_FIX_TITLE} <= set(dict(red.sections))


# ------------------------------------------------------------------------------------------- (5) the journal
def test_the_journal_names_the_case_the_attempt_asked_for_and_the_attempts_described(cfg, ws):
    loop, pr, fixer = run_x_then_y(cfg, ws)
    events = [e for e in journal_events(ws) if e["event"] == "earlier_fix_context"]
    assert len(events) == 1
    ev = events[0]
    assert ev["case"] == case_of(L2B) and ev["attempt"] == pr.attempts[1].id
    assert ev["described"] == [pr.attempts[0].id] and ev["chars"] == len(fixer.contexts[1].earlier_fix)


# --------------------------------------------------------------------------- (6) no new fact: unchanged prompt
@pytest.mark.parametrize("scope", ["full", "jdk-jakarta"])
def test_a_case_without_either_fact_packs_the_old_previous_attempts_section(cfg, ws, scope):
    """The expectation is built from the old packer's rule (title + one `summarize_script_attempt` block per earlier
    attempt, in order), not from a second pack: an `improved` attempt whose errors are NOT back stays there too."""
    ws.write(FOO, "class Foo { broken }\n")
    prev = [attempt("verify-1-1", "improved", 1), attempt("verify-2-1", "no_change", 2),
            attempt("verify-3-1", "regressed", 3)]
    packed = ContextPacker(cfg).pack_script(ws, cluster(), ctx_of(previous_attempts=prev, kb_hints=["a hint"],
                                                                 scope=scope, other_errors_summary="case b: 1 error"))
    secs = dict(packed.sections)
    expected = "\n".join(summarize_script_attempt(a) for a in prev)
    assert secs[OLD_TITLE] == expected
    assert expected.startswith("- attempt verify-1-1 (iteration 1): script new v1 → outcome: improved")
    assert packed.keys == ["header", "errors", "file_index", "files", "kb_hints", "previous_attempts", "other_errors"]
    assert EARLIER_FIX_TITLE not in secs and RESOLVED_BEFORE_TITLE not in secs
    assert f"## {OLD_TITLE}\n{expected}\n\n## Other error cases in this build" in packed.render()


def test_a_round_tripped_context_splits_by_attempt_id(cfg, ws):
    """A copied FixContext (a tier-2 plugin, `model_validate(model_dump())`) holds new objects: the split matches on
    the attempt's `id`, so the resolved attempt is not listed in both sections."""
    ws.write(FOO, "class Foo { broken }\n")
    good, bad = attempt("verify-1-1", "improved", 1), attempt("verify-2-1", "no_change", 2)
    ctx = FixContext.model_validate(ctx_of(previous_attempts=[good, bad], resolved_before=[good]).model_dump())
    secs = dict(ContextPacker(cfg).pack_script(ws, cluster(), ctx).sections)
    assert "verify-1-1" in secs[RESOLVED_BEFORE_TITLE] and "verify-1-1" not in secs[OLD_TITLE]
    assert "verify-2-1" in secs[OLD_TITLE]


def test_the_loop_leaves_the_new_fields_empty_for_a_case_nobody_introduced(cfg, ws):
    start(ws)
    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n"})])
    assert make_loop(cfg, ws, fixer, build=build, kb=FakeKB()).run(Phase.VERIFY, "package", 3).ok
    assert fixer.contexts[0].earlier_fix == "" and fixer.contexts[0].resolved_before == []
    assert not [e for e in journal_events(ws) if e["event"] == "earlier_fix_context"]


# ------------------------------------------------------------------------------------------------ (7) rule 13
@pytest.mark.parametrize("scope", ["full", "jdk-jakarta"])
def test_rule_13_says_both_statements_in_both_scopes(cfg, scope):
    load_script_prompts.cache_clear()
    text, _ = load_script_prompts("v1", (), None, "", scope, str(cfg.target.java_version))
    flat = " ".join(text.split())
    assert '13. Attempts listed under "Previous attempts on this case" did not resolve it: do something different.' \
        in flat
    assert ("Where the prompt names an earlier fix these errors come from, or says these errors were resolved before "
            "and are back, two sets of errors depend on each other: your answer must leave both sets resolved and must "
            "never simply undo the other change.") in flat
    assert ("must never simply undo the other change. This is the one case in which your answer may change more than "
            "its own case's errors need.") in flat
    assert "Earlier attempts listed in the prompt did not work." not in flat


# ------------------------------------------------------- review findings: what is a fix, what is back, latest wins
QUX = "src/main/java/com/x/Qux.java"
L2C = "compile|incompatible_types|Foo"


def test_a_regressed_attempt_kept_behind_a_later_commit_keeps_the_hint_not_the_section(cfg, ws):
    """Default config: X's try does not fix X and leaves Y in A1 (regressed), but a later commit of the same round
    improved (W fixed), so it stays in the tree. Y's prompt must not call it "a fix … kept in the tree"."""
    f1, f2 = "src/main/java/com/x/A1.java", "src/main/java/com/x/Z9.java"
    l2y, l2x, l2w = "compile|cannot_find_symbol|S2", "compile|cannot_find_symbol|S5", "compile|cannot_find_symbol|S6"
    assert cfg.loop.revert_on_regression
    ws.write(f1, "class A1 { xbroken }\n")
    ws.write(f2, "class Z9 { wbroken }\n")
    ws.commit("as-is")

    def build2(w, call):
        a, z = w.read(f1), w.read(f2)
        errs = []
        if "half" in a:
            errs.append(compile_error(f1, "exception java.io.IOException is never thrown", l2=l2y, line=2))
        if "xbroken" in a:
            errs.append(compile_error(f1, "cannot find symbol", l2=l2x, line=1))
        if "wbroken" in z:
            errs.append(compile_error(f2, "incompatible types", l2=l2w, line=1))
        return bad_build(*errs) if errs else ok_build()

    seen: list[tuple[str, FixContext]] = []

    def answer(w, c, ctx):
        seen.append((c.case_id, ctx))
        if c.case_id == case_of(l2x) and sum(1 for k, _ in seen if k == c.case_id) == 1:
            return write_script({f1: "class A1 { xbroken half }\n"})
        if c.case_id == case_of(l2w):
            return write_script({f2: "class Z9 { ok }\n"})
        return write_script({f1: "class A1 { ok }\n"})

    pr = make_loop(cfg, ws, FakeFixer(script_fn=answer), build=build2, kb=FakeKB()).run(Phase.VERIFY, "package", 4)
    x1 = pr.attempts[0]
    assert x1.outcome == "regressed" and x1.commit
    y = [ctx for k, ctx in seen if k == case_of(l2y)]
    assert y and all(ctx.earlier_fix == "" for ctx in y)
    assert f"introduced by script:{case_of(l2x)}:v1 (attempt {x1.id}) — prefer augmenting that script" \
        in y[0].kb_hints


def test_a_regressed_attempt_kept_with_revert_off_keeps_the_hint(cfg, ws):
    cfg.loop.revert_on_regression = False
    start(ws, "class Foo { xbroken }\n")

    def build2(w, call):
        t = w.read(FOO)
        errs = []
        if "xbroken" in t:
            errs.append(compile_error(FOO, "cannot find symbol", l2=L2A, line=1))
        if "half" in t:
            errs.append(compile_error(FOO, "exception java.io.IOException is never thrown", l2=L2B, line=2))
        return bad_build(*errs) if errs else ok_build()

    fixer = CapturingFixer(script_fn=lambda w, c, ctx: write_script({FOO: "class Foo { xbroken half }\n"}))
    pr = make_loop(cfg, ws, fixer, build=build2, kb=FakeKB()).run(Phase.VERIFY, "package", 3)
    assert pr.attempts[0].outcome == "regressed"
    y = [ctx for cl, ctx in zip(fixer.clusters, fixer.contexts) if cl.case_id == case_of(L2B)]
    assert y and all(ctx.earlier_fix == "" for ctx in y)
    assert any(h.startswith(f"introduced by script:{case_of(L2A)}:v1") for h in y[0].kb_hints)
    assert not [e for e in journal_events(ws) if e["event"] == "earlier_fix_context"]


def test_a_reverted_attempt_through_scoring_gets_neither_the_section_nor_a_hint(cfg, ws):
    """The real path of "reverted": X's try leaves Y and does not fix X → regressed → reverted by `_score`; Y never
    reaches a prompt and X's next prompt has no earlier-fix section."""
    start(ws, "class Foo { xbroken }\n")

    def build2(w, call):
        t = w.read(FOO)
        errs = []
        if "xbroken" in t:
            errs.append(compile_error(FOO, "cannot find symbol", l2=L2A, line=1))
        if "half" in t:
            errs.append(compile_error(FOO, "exception java.io.IOException is never thrown", l2=L2B, line=2))
        return bad_build(*errs) if errs else ok_build()

    fixer = CapturingFixer(scripts=[write_script({FOO: "class Foo { xbroken half }\n"}),
                                    write_script({FOO: "class Foo { ok }\n"})])
    loop = make_loop(cfg, ws, fixer, build=build2, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.attempts[0].outcome == "reverted" and pr.ok
    assert all(ctx.earlier_fix == "" for ctx in fixer.contexts)
    assert not any(h.startswith("introduced by") for ctx in fixer.contexts for h in ctx.kb_hints)
    assert loop._introduced == {}                                    # the final build is green: nothing remembered


def test_the_same_case_in_a_file_the_build_had_not_reached_is_not_called_back(cfg, ws):
    """Maven stops at the first failing module: X's try fixes X in Foo (improved), the next build reaches Qux with the
    same case. Nothing brought Foo's error back: the improved attempt stays under the old title."""
    ws.write(FOO, "class Foo { broken }\n")
    ws.write(QUX, "class Qux { broken }\n")
    ws.commit("as-is")

    def build2(w, call):
        if "broken" in w.read(FOO):
            return bad_build(compile_error(FOO, "cannot find symbol", l2=L2A, line=1))
        if "broken" in w.read(QUX):
            return bad_build(compile_error(QUX, "cannot find symbol", l2=L2A, line=1))
        return ok_build()

    fixer = CapturingFixer(scripts=[write_script({FOO: "class Foo { ok }\n"}),
                                    write_script({QUX: "class Qux { ok }\n"}),
                                    write_script({QUX: "class Qux { ok }\n"})])
    pr = make_loop(cfg, ws, fixer, build=build2, kb=FakeKB()).run(Phase.VERIFY, "package", 5)
    first = pr.attempts[0]
    assert first.outcome == "improved" and len(fixer.contexts) >= 2
    later = fixer.contexts[1:]
    assert all(ctx.resolved_before == [] for ctx in later)
    assert first in later[0].previous_attempts                      # previous_attempts as before this round
    packed = ContextPacker(cfg).pack_script(ws, fixer.clusters[1], later[0])
    assert RESOLVED_BEFORE_TITLE not in dict(packed.sections)
    assert f"- attempt {first.id} (iteration 1): script new v1 → outcome: improved" in dict(packed.sections)[OLD_TITLE]


def test_an_error_that_went_and_came_back_names_the_attempt_that_brought_it_back(cfg, ws):
    """A (case X) leaves Y in Foo; Y1 fixes Y and leaves Z in Bar; C (case Z) brings Y back in Foo. Y's next prompt
    names C, not A: the entry of A was forgotten when the build no longer showed Y."""
    ws.write(FOO, "class Foo { xbroken }\n")
    ws.write(BAR, "class Bar { }\n")
    ws.commit("as-is")

    def build2(w, call):
        f, b = w.read(FOO), w.read(BAR)
        errs = []
        if "xbroken" in f:
            errs.append(compile_error(FOO, "cannot find symbol", l2=L2A, line=1))
        if "half" in f:
            errs.append(compile_error(FOO, "exception java.io.IOException is never thrown", l2=L2B, line=1))
        if "zbroken" in b:
            errs.append(compile_error(BAR, "incompatible types", l2=L2C, line=1))
        return bad_build(*errs) if errs else ok_build()

    fixer = CapturingFixer(scripts=[write_script({FOO: "class Foo { half }\n"}),
                                    write_script({FOO: "class Foo { ok }\n", BAR: "class Bar { zbroken }\n"}),
                                    write_script({BAR: "class Bar { }\n", FOO: "class Foo { half again }\n"}),
                                    write_script({FOO: "class Foo { ok }\n"})])
    pr = make_loop(cfg, ws, fixer, build=build2, kb=FakeKB()).run(Phase.VERIFY, "package", 6)
    a, y1, c = pr.attempts[0], pr.attempts[1], pr.attempts[2]
    assert (a.script_case_id, y1.script_case_id, c.script_case_id) == (case_of(L2A), case_of(L2B), case_of(L2C))
    last = fixer.contexts[3]
    assert fixer.clusters[3].case_id == case_of(L2B)
    assert last.earlier_fix.startswith(f"Attempt {c.id} (")
    assert a.id not in last.earlier_fix
    assert "-class Foo { ok }\n+class Foo { half again }" in last.earlier_fix


def test_a_pre_try_of_the_same_case_is_not_another_case(cfg, ws):
    """A zero-LLM pre-try has no script case id: it is matched by its cluster id, never described as another case."""
    from sbmig.loop.fixloop import SBMIG_VERSION_SOURCE, _pair
    loop = make_loop(cfg, ws, FakeFixer(), kb=FakeKB())
    ws.write("pom.xml", "<project><a/></project>\n")
    base = ws.commit("b")
    ws.write("pom.xml", "<project><a>1</a></project>\n")
    head = ws.commit("c")
    e_old = compile_error("pom.xml", "'dependencies.dependency.version' for a:a is missing", l2="pom|missing_version")
    e_new = compile_error("pom.xml", "'dependencies.dependency.version' for b:b is missing", l2="pom|missing_version",
                          sig="pom|b")
    c = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[e_new], case_id="k1")
    pre = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:k1",
                     decision=TriageDecision(cluster_id="case:k1", kind=DecisionKind.SCRIPT), outcome="improved",
                     applied=True, base_commit=base, commit=head, source=SBMIG_VERSION_SOURCE, cluster_errors=[e_old])
    loop._introduced[_pair(e_new)] = pre
    assert loop._earlier_fix_context(c, pre) == ("", set())
    assert loop._introduced_hints(c) == []


def test_case_files_beyond_the_shown_ones_are_named_as_files_of_this_case(cfg, ws, monkeypatch):
    monkeypatch.setattr(fixloop, "EARLIER_FIX_FILES", 1)
    loop = make_loop(cfg, ws, FakeFixer(), kb=FakeKB())
    ws.write(FOO, "class Foo { a }\n")
    ws.write(BAR, "class Bar { a }\n")
    base = ws.commit("b")
    ws.write(FOO, "class Foo { b }\n")
    ws.write(BAR, "class Bar { b }\n")
    ws.write("pom.xml", "<project/>\n")
    head = ws.commit("c")
    errs = [compile_error(FOO, "never thrown", l2=L2B, line=1), compile_error(BAR, "never thrown", l2=L2B, line=1)]
    c = ErrorCluster(id=f"case:{case_of(L2B)}", kind=ErrorKind.COMPILE, errors=errs, case_id=case_of(L2B))
    a = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id=f"case:{case_of(L2A)}",
                   decision=TriageDecision(cluster_id=f"case:{case_of(L2A)}", kind=DecisionKind.SCRIPT),
                   outcome="improved", applied=True, base_commit=base, commit=head, script_case_id=case_of(L2A),
                   script_version=1, script_mode="new", cluster_errors=[compile_error(FOO, "cannot find symbol")])
    fx = loop._earlier_fix(a, {FOO, BAR})
    text = earlier_fix_text([fx], EARLIER_FIX_CHARS)
    shown, rest = sorted([FOO, BAR])
    assert f"What it changed in {shown}:" in text and f"What it changed in {rest}:" not in text
    assert f"It also changed these files of this case (diff not shown): {rest}" in text
    assert "It also changed (not shown): pom.xml" in text
