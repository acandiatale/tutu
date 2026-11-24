"""v2.1z-cd (docs/INTERFACES.md "v2.1z-cd"): a case that shows up at a new place starts with the script that
resolved it elsewhere in this run.

Maven stops at the first failing module, so a module-level error shows for one module per build. The ladder counts
the tries made at the places of the case's errors now (`_previous(ladder=True)`); with nothing made here, the stored
version whose run resolved the case at another place runs again (source `kb-script`, no LLM call); the refusal of an
answer whose code already ran looks at the attempts made here only. The sequence is the one of
`test_v21zcb_module_place.py` (three modules, a processor path without a version in each)."""
from __future__ import annotations

from sbmig.core.models import BlockReason, Phase

from .fakes import FakeFixer, FakeKB, bad_build, journal_events, ok_build, script
from .test_v21v_y_prompt_facts import make_loop
from .test_v21zcb_module_place import FIXED, file_err, mod_err, pom, real_run_build, src, start

# the same code for every module: it fixes the pom of the module each targeted error is in
GENERIC = script("for e in p.errors:\n    if e['module'] and not e['file']:\n"
                 f"        p.write(e['module'] + '/pom.xml', {FIXED!r})")


def only(m: str):
    """A script that fixes the pom of `m` whatever the errors are."""
    return script(f"p.write({pom(m)!r}, {FIXED!r})")


def events(ws, name: str) -> list[dict]:
    return [e for e in journal_events(ws) if e.get("event") == name]


class Answers(FakeFixer):
    """Answers in order (the last one repeated); records (mode, module of the first error) per call."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)
        self.asked: list[tuple] = []

    def propose_script(self, ws, cluster, ctx):
        self.contexts.append(ctx)
        self.asked.append((ctx.script_mode, cluster.errors[0].module, cluster.errors[0].file))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return a.model_copy(deep=True)


def tries(pr) -> list[tuple]:
    return [(a.source, a.script_version, a.outcome, (a.cluster_errors[0].module if a.cluster_errors else None))
            for a in pr.attempts]


# ------------------------------------------------------------------------------------------------ rule 2
def test_the_same_code_for_every_module_is_one_llm_call_and_two_reruns(cfg, ws):
    start(ws, broken=())
    fixer = Answers(GENERIC)
    kb = FakeKB()
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert fixer.asked == [("new", "m1", None)]
    assert tries(pr) == [("llm-new", 1, "improved", "m1"), ("kb-script", 1, "improved", "m2"),
                         ("kb-script", 1, "build_ok", "m3")]
    assert pr.build_ok and not pr.blocked
    assert not any(b.reason == BlockReason.REPEATED_FAILURE for b in pr.blocked)
    assert all(ws.read(pom(m)) == FIXED for m in ("m1", "m2", "m3"))
    assert [a.script_mode for a in pr.attempts] == ["new", "reuse", "reuse"]
    # the journal: the decisions say it is this run's own version, the attempts are reuses
    dec = [e for e in events(ws, "decision") if e.get("kind") == "script"]
    assert len(dec) == 3
    assert "this run's v1" in dec[1]["rationale"] and "@m1" in dec[1]["rationale"]
    assert "this run's v1" in dec[2]["rationale"] and "@m2" in dec[2]["rationale"]
    att = events(ws, "attempt")
    assert [(e["source"], e["script"]["mode"], e["script"]["version"], e["applied"]) for e in att] == [
        ("llm-new", "new", 1, True), ("kb-script", "reuse", 1, True), ("kb-script", "reuse", 1, True)]
    # credited like any reuse
    reused = [r for r in kb.cases.credit_runs if r["reused"]]
    assert [(r["version"], r["result"]) for r in reused] == [(1, "improved"), (1, "build_ok")]


def test_the_kb_search_line_names_this_runs_version_and_the_place(cfg, ws, caplog):
    start(ws, modules=("m1", "m2"), broken=())
    loop = make_loop(cfg, ws, Answers(GENERIC), build=real_run_build, kb=FakeKB())
    with caplog.at_level("INFO", logger="sbmig"):
        pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok
    lines = [r.getMessage() for r in caplog.records if "KB 탐색" in r.getMessage()]
    assert any("이번 실행의 v1" in s and "@m1" in s and "@m2" in s for s in lines), lines


def test_a_rerun_that_does_not_help_goes_on_with_the_augment_and_identical_code_is_refused_there(cfg, ws):
    """v1 fixes m1's pom only: rerun at m2 changes nothing (apply_failed, no build), then the ordinary ladder for an
    attempt made here: augment of the rerun; the same code again is refused as today (it ran here)."""
    cfg.script.max_llm_tries_per_case = 2
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(only("m1"))
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert fixer.asked[0] == ("new", "m1", None)
    assert fixer.asked[1:] == [("augment", "m2", None)] * 2
    t = tries(pr)
    assert t[0] == ("llm-new", 1, "improved", "m1")
    assert t[1] == ("kb-script", 1, "apply_failed", "m2")
    refused = [a for a in pr.attempts[2:] if a.cluster_errors[0].module == "m2"]
    assert len(refused) == 2
    # refused before anything runs: the answer is the code of its base, the rerun made here
    assert all(a.apply_errors == ["the augmented script is identical to its base v1 — not run again"]
               for a in refused)
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]
    # the augment at the new place extends the rerun made here, not m1's attempt
    base = fixer.contexts[1].script_base
    assert base is not None and base.version == 1 and base.outcome == "apply_failed"


def test_the_prompt_history_at_a_new_place_keeps_the_attempts_made_elsewhere(cfg, ws):
    cfg.script.max_llm_tries_per_case = 1
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(only("m1"))
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    m1_attempt = pr.attempts[0]
    ctx = fixer.contexts[1]                                        # the augment at m2
    ids = [a.id for a in ctx.previous_attempts]
    assert m1_attempt.id in ids and pr.attempts[1].id in ids


def test_identical_code_answered_at_a_new_place_for_a_version_that_ran_elsewhere_runs(cfg, ws):
    """m1: v1 (code A, fixes m2's pom) does nothing for m1 → v2 (code B, m1's pom) fixes m1. m2: rerun of v2
    changes nothing, the augment answers code A = v1 again: v1 ran at m1 only, so it runs here and fixes m2."""
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(only("m2"), only("m1"), only("m2"))
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok and not pr.blocked
    assert tries(pr) == [("llm-new", 1, "no_change", "m1"), ("llm-augment", 2, "improved", "m1"),
                         ("kb-script", 2, "apply_failed", "m2"), ("llm-augment", 1, "build_ok", "m2")]
    assert not any("which already ran in this run" in n for a in pr.attempts for n in a.notes)
    stored = [e for e in events(ws, "script_stored") if e["version"] == 1 and e["mode"] == "augment"]
    assert stored and stored[0]["duplicate_of"] is None


# ------------------------------------------------------------------------------------------------ unchanged
def test_a_case_whose_error_comes_back_at_the_same_place_keeps_todays_ladder(cfg, ws):
    """After the move to m2 and a rerun that did not help: v2 does nothing at m2 (no_change) → the next step is an
    augment of v2 (made here), then v3 fixes m2."""
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(only("m1"), script("p.write('notes.txt', 'nothing')"), only("m2"))
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok
    assert tries(pr) == [("llm-new", 1, "improved", "m1"), ("kb-script", 1, "apply_failed", "m2"),
                         ("llm-augment", 2, "no_change", "m2"), ("llm-augment", 3, "build_ok", "m2")]
    assert fixer.contexts[2].script_base.version == 2                # extends v2, the try made here


def test_a_file_level_case_in_two_files_of_one_module_follows_the_same_rule(cfg, ws):
    a, b = src("m1"), "m1/src/main/java/com/example/m1/Other.java"
    ws.write(a, "class Widget { broken }\n")
    ws.write(b, "class Other { broken }\n")
    ws.commit("as-is")

    def build(w, call):
        for f in (a, b):
            if "broken" in w.read(f):
                return bad_build(file_err(f, "m1"))
        return ok_build()

    fixer = Answers(script("for e in p.errors:\n    if e['file']:\n"
                           "        p.write(e['file'], p.read(e['file']).replace('broken', 'ok'))"))
    loop = make_loop(cfg, ws, fixer, build=build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok and len(fixer.asked) == 1
    assert [(x.source, x.outcome, x.cluster_errors[0].file) for x in pr.attempts] == [
        ("llm-new", "improved", a), ("kb-script", "build_ok", b)]


def test_a_run_without_a_kb_asks_the_llm_as_before(cfg, ws):
    """No KB: LLM scripts run unstored (v0), nothing to rerun; the move to m2 is the first rung again (a new ask)."""
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(GENERIC)
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB(cases=None))
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok
    assert [m for _mode, m, _f in fixer.asked] == ["m1", "m2"]
    assert [a.source for a in pr.attempts] == ["llm-new", "llm-new"]


def test_reuse_off_at_a_new_place_augments_the_stored_version_that_resolved_the_other_place(cfg, ws):
    """Finding (reported, not changed): with `script.reuse: false` the first rung at m2 is the augment of the stored
    v1 (`base_att` None → the store's best), and that v1 resolved m1 in this run."""
    cfg.script.reuse = False
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(GENERIC, only("m2"))
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.build_ok
    assert fixer.asked == [("new", "m1", None), ("augment", "m2", None)]
    base = fixer.contexts[1].script_base
    assert base is not None and base.version == 1 and base.outcome == ""


def test_reuse_off_the_same_code_at_a_new_place_is_refused_as_identical_to_its_base(cfg, ws):
    """The same finding with the answer of the `same` variant: the code of v1, which resolved m1, is refused at m2
    as "identical to its base v1" (and `sbmig.fix.fixer._script_problems` would say "which did not fix the case")."""
    cfg.script.reuse = False
    cfg.script.max_llm_tries_per_case = 2
    start(ws, modules=("m1", "m2"), broken=())
    fixer = Answers(GENERIC)
    loop = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB())
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert not pr.build_ok and [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]
    m2 = [a for a in pr.attempts if a.cluster_errors[0].module == "m2"]
    assert m2 and m2[0].apply_errors == ["the augmented script is identical to its base v1 — not run again"]


# ------------------------------------------------------------------------------------------------ rule 4
def ring(k: int) -> str:
    return f"m1/src/R{k}.java"


def test_a_case_cycling_through_five_places_is_blocked_at_twice_the_cap(cfg, ws):
    """The reviewer's ring: each answer resolves R<k> and breaks R<k+1 mod 5>, every answer different; the cycle
    is longer than loop.oscillation_window. The asks of the case over every place stop at 2 × the cap."""
    cfg.script.max_llm_tries_per_case = 3
    n = 5
    ws.write(ring(0), "class R { broken }\n")
    for k in range(1, n):
        ws.write(ring(k), "class R { GOOD }\n")
    ws.commit("as-is")
    body = ("import re\nfor e in p.errors:\n    f = e['file']\n    k = int(re.search(r'R(\\d+)', f).group(1))\n"
            "    p.write(f, 'class R { GOOD }')\n"
            f"    p.write('m1/src/R%d.java' % ((k + 1) % {n}), 'class R {{ broken }}')\n")

    class Distinct(Answers):
        def propose_script(self, ws, cluster, ctx):
            self.contexts.append(ctx)
            self.asked.append((ctx.script_mode, cluster.errors[0].module, cluster.errors[0].file))
            return script(f"p.log('answer {len(self.asked)}')\n" + body)

    def build(w, call):
        for k in range(n):
            if "broken" in w.read(ring(k)):
                return bad_build(file_err(ring(k), "m1"))
        return ok_build()

    fixer = Distinct(GENERIC)
    pr = make_loop(cfg, ws, fixer, build=build, kb=FakeKB()).run(Phase.VERIFY, "package", 50)
    assert len(fixer.asked) == 6, (fixer.asked, pr.iterations)
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE]
    dec = [e for e in events(ws, "decision") if e.get("kind") == "blocked"]
    assert dec and "6 LLM tries" in dec[-1]["rationale"] and "= 6" in dec[-1]["rationale"]
    assert pr.iterations < 50


def test_identical_answers_over_three_modules_stay_one_ask_with_a_cap_of_one(cfg, ws):
    cfg.script.max_llm_tries_per_case = 1
    start(ws, broken=())
    fixer = Answers(GENERIC)
    pr = make_loop(cfg, ws, fixer, build=real_run_build, kb=FakeKB()).run(Phase.VERIFY, "package", 10)
    assert fixer.asked == [("new", "m1", None)]
    assert [a.source for a in pr.attempts] == ["llm-new", "kb-script", "kb-script"] and pr.build_ok


def test_one_new_ask_at_each_of_four_places_within_the_bound_is_not_blocked(cfg, ws):
    """Cap 2 → bound 4: each module needs its own answer (the rerun of the previous one changes nothing)."""
    cfg.script.max_llm_tries_per_case = 2
    mods = ("m1", "m2", "m3", "m4")
    start(ws, modules=mods, broken=())

    def build(w, call):
        for m in mods:
            if "without version" in w.read(pom(m)):
                return bad_build(mod_err(m))
        return ok_build()

    fixer = Answers(*(only(m) for m in mods))
    pr = make_loop(cfg, ws, fixer, build=build, kb=FakeKB()).run(Phase.VERIFY, "package", 20)
    assert pr.build_ok and not pr.blocked
    assert [m for _mode, m, _f in fixer.asked] == list(mods)


# ------------------------------------------------------------------------------------------------ round trips
def test_an_attempt_of_a_round_trip_is_no_rerun_candidate_although_the_store_does_not_mark_it(cfg, ws):
    start(ws, modules=("m1", "m2"), broken=())
    kb = FakeKB()
    loop = make_loop(cfg, ws, Answers(GENERIC), build=real_run_build, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 1)                       # one build: m1's attempt, scored next build
    a = pr.attempts[0]
    a.outcome = "improved"
    from sbmig.triage import cluster_cases
    [c2] = cluster_cases([mod_err("m2")], None)
    c2 = c2.model_copy(update={"id": a.cluster_id})
    assert loop._resolved_elsewhere(c2) == [a]
    loop._round_trips[id(a)] = "round trip: put back the lines …"
    assert not any(getattr(s, "round_trip", False) for s in kb.cases.scripts(a.script_case_id))
    assert loop._resolved_elsewhere(c2) == []
