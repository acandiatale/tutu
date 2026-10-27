"""ErrorTracker unit tests (2026-10-23 error cases): identity, occurrences, visibility / masking, reopen, void builds,
attribution order, plan steps, end states, failure texts, attempt records with real git diffs."""
from __future__ import annotations

import hashlib
from typing import Optional

from sbmig.core.models import (
    BlockedItem,
    BlockReason,
    BuildError,
    BuildResult,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FileChange,
    FixAttempt,
    LLMUsage,
    Phase,
    TriageDecision,
)
from sbmig.loop.error_tracker import ErrorTracker, identity_of, norm_file

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def err(file: Optional[str] = FOO, msg: str = "cannot find symbol", sig: Optional[str] = None, line: Optional[int] = 3,
        kind: ErrorKind = ErrorKind.COMPILE, module: Optional[str] = "demo") -> BuildError:
    return BuildError(kind=kind, message=msg, file=file, line=line, module=module,
                      signature=sig or f"{kind.value}|{msg}", signature_generic=f"{kind.value}|x", raw=msg)


def bad(*errors: BuildError, goal: str = "compile", failed: Optional[str] = "demo", built=(),
        command=None) -> BuildResult:
    return BuildResult(ok=False, goal=goal, command=command or ["mvn", goal], exit_code=1, duration_s=0.1,
                       log_path="/tmp/b.log", errors=list(errors), failed_module=failed, modules_built=list(built))


def ok(goal: str = "compile", command=None) -> BuildResult:
    return BuildResult(ok=True, goal=goal, command=command or ["mvn", goal], exit_code=0, duration_s=0.1,
                       log_path="/tmp/ok.log")


def attempt(aid: str, errors: list[BuildError], *, mode: str = "new", outcome="improved",
            changes: Optional[list[FileChange]] = None, applied=True, **kw) -> FixAttempt:
    """A fix-script attempt (v2: every fix loop attempt is one) of case `case-<aid>`, version 1."""
    d = TriageDecision(cluster_id=f"c-{aid}", kind=DecisionKind.SCRIPT)
    return FixAttempt(id=aid, phase=Phase.SPRING, iteration=1, cluster_id=f"c-{aid}", decision=d,
                      cluster_errors=list(errors), cluster_signatures=[e.signature for e in errors],
                      changes=changes if changes is not None else [FileChange(path=errors[0].file or "pom.xml",
                                                                              op="modify")],
                      applied=applied, outcome=outcome, model="m1" if mode != "reuse" else "",
                      script_mode=mode, script_case_id=f"case-{aid}", script_version=1, **kw)


def by_file(errors, file):
    return [e for e in errors if e.file == file]


def test_norm_file_and_identity():
    assert norm_file("shop-back/src/main/java/a/B.java") == "src/main/java/a/B.java"
    assert norm_file("APP\\src\\com\\x\\A.java") == "src/com/x/A.java"
    assert norm_file("svc-a/pom.xml") == "svc-a/pom.xml" and norm_file(None) == ""
    e = err().model_copy(update={"signature": ""})
    assert identity_of(e) == ("compile", FOO, "cannot find symbol")
    assert identity_of(err(file="demo-back/" + FOO)) == ("compile", FOO, "compile|cannot find symbol")


def test_identity_survives_the_restructure_move(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err("src/main/java/a/B.java")), [])
    t.note_step("restructure.apply", Phase.RESTRUCTURE)
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(err("shop-back/src/main/java/a/B.java", module="shop-back"),
                                                      failed="shop-back"), [])
    errors, _ = t.finalize([], run_id="r1")
    assert len(errors) == 1
    r = errors[0]
    assert r.status == "unresolved" and r.builds_present == 2 and r.file == "shop-back/src/main/java/a/B.java"
    assert r.first_seen == "spring-1" and r.last_seen == "restructure-1" and r.first_phase == Phase.SPRING
    ident = "|".join(["r1", "compile", "src/main/java/a/B.java", "compile|cannot find symbol", "0"])
    assert r.id == hashlib.sha1(ident.encode()).hexdigest()[:16]
    assert r.failure_reason == "no fix attempt before the loop ended"


def test_three_occurrences_two_disappear(cfg, ws):
    t = ErrorTracker(ws, cfg)
    occ = [err(line=n) for n in (10, 20, 30)]
    t.observe(Phase.JDK21, "jdk21-1", bad(*occ), [])
    t.observe(Phase.JDK21, "jdk21-2", bad(err(line=21)), [])      # the middle one, shifted by a line
    errors, _ = t.finalize([], run_id="r1")
    assert len(errors) == 3 and len({e.id for e in errors}) == 3
    by_line = {e.line: e for e in errors}
    assert by_line[21].status == "unresolved" and by_line[21].builds_present == 2
    assert by_line[10].status == by_line[30].status == "resolved_indirectly"
    assert by_line[10].fix_method == "none" and "gone between jdk21-1 and jdk21-2" in by_line[10].notes


def test_message_wins_over_line_when_matching_occurrences(cfg, ws):
    t = ErrorTracker(ws, cfg)
    a, b = err(msg="m1", sig="s", line=10), err(msg="m2", sig="s", line=50)
    t.observe(Phase.JDK21, "jdk21-1", bad(a, b), [])
    t.observe(Phase.JDK21, "jdk21-2", bad(err(msg="m2", sig="s", line=11)), [])
    errors, _ = t.finalize([], run_id="r1")
    m = {e.message: e for e in errors}
    assert m["m2"].status == "unresolved" and m["m2"].line == 11 and m["m1"].status == "resolved_indirectly"


def test_pom_error_masks_compile_errors_and_final_not_visible(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO), err(BAR)), [])
    pom = err("pom.xml", msg="Non-parseable POM", kind=ErrorKind.POM_INVALID, line=None, sig="pom_invalid|np")
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(pom), [])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.file: e for e in errors}
    assert st[FOO].status == st[BAR].status == "not_visible"
    assert "not seen since spring-1" in st[FOO].failure_reason and "restructure-1" in st[FOO].failure_reason
    assert st[FOO].problem_errors and "Non-parseable POM" in st[FOO].problem_errors[0]
    assert st["pom.xml"].status == "unresolved"


def test_masked_errors_close_once_a_build_gets_far_enough(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    pom = err("pom.xml", msg="bad pom", kind=ErrorKind.POM_INVALID, line=None, sig="pom|bad")
    t.observe(Phase.SPRING, "spring-2", bad(pom), [])
    t.observe(Phase.SPRING, "spring-3", bad(err(BAR)), [])         # compile stage reached: Foo is really gone
    errors, _ = t.finalize([], run_id="r1")
    st = {e.file: e for e in errors}
    assert st[FOO].status == "resolved_indirectly" and st[FOO].resolved_at == "spring-3"
    assert st["pom.xml"].status == "resolved_indirectly" and st[BAR].status == "unresolved"


def test_other_module_failed_masks_and_built_module_proves(cfg, ws):
    t = ErrorTracker(ws, cfg)
    a = err("core/src/main/java/A.java", module="core")
    b = err("web/src/main/java/B.java", module="web")
    t.observe(Phase.SPRING, "spring-1", bad(a, b, failed="core"), [])
    # core now builds, web is skipped after another module failed → web's error is masked, core's is gone
    c = err("api/src/main/java/C.java", module="api")
    t.observe(Phase.SPRING, "spring-2", bad(c, failed="api", built=["core"]), [])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.module: e.status for e in errors}
    assert st == {"core": "resolved_indirectly", "web": "not_visible", "api": "unresolved"}


def test_reopen_keeps_history(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.JDK21, "jdk21-1", bad(err(FOO), err(BAR)), [])
    t.observe(Phase.JDK21, "jdk21-2", bad(err(BAR)), [])
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO), err(BAR)), [])
    errors, _ = t.finalize([], run_id="r1")
    assert len(errors) == 2
    foo = by_file(errors, FOO)[0]
    assert foo.status == "unresolved" and foo.builds_present == 2
    assert any(n.startswith("reopened at spring-1: was resolved_indirectly at jdk21-2") for n in foo.notes)


def test_void_build_neither_opens_nor_closes(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    a = attempt("spring-1-1", [err(FOO)], outcome="reverted")
    t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(err(FOO), err(BAR), err(FOO, msg="new", sig="n")), [a], void=True)
    t.observe(Phase.SPRING, "spring-3", bad(err(FOO)), [])          # the reverted tree
    t.observe(Phase.SPRING, "spring-4", BuildResult(ok=False, goal="compile", command=[], exit_code=1,
                                                    duration_s=1, log_path="", timed_out=True), [])
    errors, attempts = t.finalize([], run_id="r1")
    assert [e.file for e in errors] == [FOO] and errors[0].builds_present == 2 and errors[0].status == "unresolved"
    assert attempts[0].remaining_errors and "cannot find symbol" in attempts[0].remaining_errors[0]
    assert "scored at spring-2" in attempts[0].notes


def test_attribution_order(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e1, e2, e3, e4, e5 = (err(f"src/main/java/E{i}.java") for i in range(1, 6))
    t.observe(Phase.SPRING, "spring-1", bad(e1, e2, e3, e4, e5), [])
    a_target = attempt("spring-1-1", [e1], changes=[FileChange(path="src/main/java/Z.java", op="modify")])
    a_touch = attempt("spring-1-2", [e5], changes=[FileChange(path="mod/src/main/java/E2.java", op="modify")],
                      mode="reuse")
    for a in (a_target, a_touch):
        t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(e3, e4, e5), [a_target, a_touch])
    # e3 disappears after an unrelated attempt, e4 after a plan step, e5 with nothing at all
    a_other = attempt("spring-2-1", [e5], mode="augment",
                      changes=[FileChange(path="src/main/java/Q.java", op="modify")])
    t.note_attempt(a_other)
    t.observe(Phase.SPRING, "spring-3", bad(e4, e5), [a_other])
    t.note_step("restructure.apply", Phase.RESTRUCTURE)
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(e5), [])
    t.observe(Phase.VERIFY, "verify-final", ok("clean package"), [])
    errors, attempts = t.finalize([], run_id="r1")
    r = {e.file.rsplit("/", 1)[-1]: e for e in errors}
    assert (r["E1.java"].status, r["E1.java"].targeted, r["E1.java"].fix_method) == ("resolved", True, "script")
    assert r["E1.java"].resolved_by == "r1:spring-1-1" and r["E1.java"].resolver_id == "script:case-spring-1-1:v1"
    assert (r["E2.java"].status, r["E2.java"].targeted, r["E2.java"].fix_method) == ("resolved", False,
                                                                                     "script_reuse")
    assert r["E2.java"].resolved_by == "r1:spring-1-2"
    assert (r["E3.java"].status, r["E3.java"].fix_method) == ("resolved_indirectly", "script_augment")
    assert r["E3.java"].resolver_id == "script:case-spring-2-1:v1" and r["E3.java"].resolved_by == "r1:spring-2-1"
    assert (r["E4.java"].status, r["E4.java"].fix_method) == ("resolved_indirectly", "plan_step")
    assert r["E4.java"].resolved_by == "step:restructure.apply" and r["E4.java"].resolved_phase == Phase.RESTRUCTURE
    # e5 was targeted by the augment attempt (spring-2-1) but only disappeared at the final build
    assert (r["E5.java"].status, r["E5.java"].fix_method) == ("resolved_indirectly", "none")
    assert r["E5.java"].resolved_at == "verify-final"
    assert [x.attempt_id for x in r["E5.java"].attempts] == ["r1:spring-1-2", "r1:spring-2-1"]
    assert {a.attempt_id for a in attempts} == {"spring-1-1", "spring-1-2", "spring-2-1"}


def test_masked_error_credits_the_attempt_of_the_masked_span(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    a = attempt("spring-1-1", [err(FOO)])
    t.note_attempt(a)
    pom = err("pom.xml", msg="bad", kind=ErrorKind.POM_INVALID, line=None, sig="pom|bad")
    t.observe(Phase.SPRING, "spring-2", bad(pom), [a])                # Foo masked by the pom error
    t.observe(Phase.SPRING, "spring-3", ok(), [])
    errors, _ = t.finalize([], run_id="r1")
    foo = by_file(errors, FOO)[0]
    assert foo.status == "resolved" and foo.targeted and foo.resolved_at == "spring-3"


def test_blocked_end_state(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err(FOO, msg="incompatible types: javax.servlet.Filter", sig="compile|incompatible|javax")
    t.observe(Phase.SPRING, "spring-1", bad(e, err(BAR)), [])
    t.observe(Phase.VERIFY, "verify-final", bad(e, err(BAR), goal="clean package"), [])
    item = BlockedItem(id="blk-1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c",
                       signatures=[e.signature], files=[f"{FOO}:3"], suggestion="upgrade lib X to its jakarta line",
                       notes=["blocked: external_javax_api — lib X"])
    errors, _ = t.finalize([item], run_id="r1")
    st = {x.file: x for x in errors}
    assert st[FOO].status == "blocked" and st[FOO].block_reason == "external_javax_api"
    assert st[FOO].suggestion == "upgrade lib X to its jakarta line"
    assert st[FOO].failure_reason == "blocked (external_javax_api): upgrade lib X to its jakarta line"
    assert st[BAR].status == "unresolved"


def test_blocked_item_by_its_errors(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err("pom.xml", msg="Could not find artifact a:b:1", kind=ErrorKind.DEPENDENCY_RESOLUTION, line=None,
            sig="dependency_resolution|a:b")
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    item = BlockedItem(id="blk-2", reason=BlockReason.ARTIFACT_NOT_FOUND, phase=Phase.SPRING, cluster_id="c",
                       errors=[e.model_copy(update={"file": "module/x/pom.xml"})], notes=["not in any repository"])
    errors, _ = t.finalize([item], run_id="r1")
    assert errors[0].status == "blocked" and errors[0].failure_reason == \
        "blocked (artifact_not_found): not in any repository"


def test_failure_reasons_and_problem_errors(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e_apply, e_nochange, e_regr, e_dec, e_none = (err(f"src/main/java/F{i}.java") for i in range(5))
    t.observe(Phase.SPRING, "spring-1", bad(e_apply, e_nochange, e_regr, e_dec, e_none), [])
    t.observe_clusters([ErrorCluster(id="c-dec", kind=ErrorKind.COMPILE, file=e_dec.file, errors=[e_dec],
                                     signatures=[e_dec.signature])])
    t.note_decision(TriageDecision(cluster_id="c-dec", kind=DecisionKind.ESCALATE,
                                   rationale="escalated: 2 LLM attempts failed"))
    a1 = attempt("spring-1-1", [e_apply], outcome="apply_failed", applied=False, changes=[],
                 apply_errors=["src/main/java/F0.java: search not found"])
    a2 = attempt("spring-1-2", [e_nochange], outcome="no_change")
    intro = err(e_regr.file, msg="';' expected", sig="compile|semi", line=9)
    a3 = attempt("spring-1-3", [e_regr], outcome="reverted", introduced_errors=[intro],
                 notes=["regressed: 1 new error(s) in touched files", "reverted to abc"])
    for a in (a1, a2, a3):
        t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(e_apply, e_nochange, e_regr, e_dec, e_none), [a2])
    errors, attempts = t.finalize([], run_id="r1")
    r = {e.file.rsplit("/", 1)[-1]: e for e in errors}
    assert r["F0.java"].failure_reason == \
        "script attempt spring-1-1 apply_failed: src/main/java/F0.java: search not found"
    assert r["F0.java"].problem_errors == ["src/main/java/F0.java: search not found"]
    assert r["F1.java"].failure_reason == \
        "script attempt spring-1-2 no_change: the targeted error(s) were still there after the rebuild"
    assert r["F1.java"].problem_errors == ["src/main/java/F1.java:3 cannot find symbol"]
    assert r["F2.java"].failure_reason.startswith("script attempt spring-1-3 reverted: regressed: 1 new error(s)")
    assert "';' expected" in r["F2.java"].failure_reason and r["F2.java"].failure_reason.endswith("(reverted)")
    assert r["F2.java"].problem_errors == ["src/main/java/F2.java:9 ';' expected"]
    assert r["F3.java"].failure_reason == "no fix attempt: escalated: 2 LLM attempts failed"
    assert r["F4.java"].failure_reason == "no fix attempt before the loop ended"
    assert r["F0.java"].attempts[0].failure == "src/main/java/F0.java: search not found"
    assert r["F0.java"].attempt_count == 1 and r["F0.java"].llm_attempts == 1
    att = {a.attempt_id: a for a in attempts}
    assert att["spring-1-1"].diff == ""                       # nothing applied: no diff
    assert att["spring-1-1"].error_ids == [r["F0.java"].id]
    assert att["spring-1-3"].introduced_errors == ["src/main/java/F2.java:9 ';' expected"]
    assert att["spring-1-2"].remaining_errors == ["src/main/java/F1.java:3 cannot find symbol"]


def test_pending_attempt_and_descriptions(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    reuse = attempt("spring-1-1", [e], mode="reuse", outcome="pending", analysis="stored: add the dependency")
    bare = attempt("spring-1-2", [e], mode="augment", outcome="pending")
    llm = attempt("spring-1-3", [e], outcome="pending", analysis="x" * 3000,
                  llm_usage=LLMUsage(model="m1", calls=2, input_tokens=10, output_tokens=5))
    other = attempt("spring-1-4", [e], outcome="pending", analysis="skipped")
    other.decision = TriageDecision(cluster_id="c-x", kind=DecisionKind.SKIP, rationale="masked")
    for a in (reuse, bare, llm, other):
        t.note_attempt(a)
    _, attempts = t.finalize([], run_id="r1")
    d = {a.attempt_id: a for a in attempts}
    assert d["spring-1-1"].description == "stored: add the dependency"
    assert d["spring-1-1"].fix_method == "script_reuse" and d["spring-1-1"].resolver_id == "script:case-spring-1-1:v1"
    assert d["spring-1-2"].description == "fix script case-spring-1-2:v1 (augment)"
    assert d["spring-1-2"].fix_method == "script_augment"
    assert d["spring-1-3"].description.startswith("xxx") and len(d["spring-1-3"].description) < 1600
    assert (d["spring-1-4"].fix_method, d["spring-1-4"].resolver_id, d["spring-1-4"].description) == \
        ("none", "", "skipped")
    assert (d["spring-1-3"].llm_calls, d["spring-1-3"].input_tokens, d["spring-1-3"].output_tokens) == (2, 10, 5)
    assert all(a.failure == "not scored (the run ended before the rebuild)" for a in attempts)


def test_attempt_diff_from_git_limited_to_its_files(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    base = ws.head()
    ws.write(FOO, "class Foo { fixed }\n")
    ws.write(BAR, "class Bar { also changed }\n")
    sha = ws.commit("fix")
    a = attempt("spring-1-1", [e], changes=[FileChange(path=FOO, op="modify")], base_commit=base, commit=sha)
    t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", ok(), [a])
    errors, attempts = t.finalize([], run_id="r1")
    diff = attempts[0].diff
    assert "+class Foo { fixed }" in diff and "-class Foo { broken }" in diff and "Bar" not in diff
    assert not attempts[0].diff_truncated and errors[0].status == "resolved"


def test_attempt_diff_is_passed_in_full_and_capped_after_classification(cfg, ws):
    """Round 10: the tracker no longer caps (a cap before the classification cut later files' code edits off);
    prepare_error_cases classifies the full diff, then applies kb.error_diff_chars (0 / negative = no cap)."""
    from sbmig.kb.error_cases import prepare_error_cases

    cfg = cfg.model_copy(deep=True)
    cfg.kb.error_diff_chars = 60
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    base = ws.head()
    ws.write(FOO, "class Foo {\n" + "  int x;\n" * 50 + "}\n")
    a = attempt("spring-1-1", [e], base_commit=base, commit=ws.commit("big"))
    t.note_attempt(a)
    errors, attempts = t.finalize([], run_id="r1")
    assert not attempts[0].diff_truncated and len(attempts[0].diff) > 300 and "int x;" in attempts[0].diff
    _, prepared = prepare_error_cases(errors, attempts, None, ws, cfg)
    assert prepared[0].diff_truncated and len(prepared[0].diff) <= 60 + 20
    assert prepared[0].change.lines_added >= 50                    # classified on the whole diff
    for cap in (0, -1):
        cfg.kb.error_diff_chars = cap
        _, full = prepare_error_cases(errors, attempts, None, ws, cfg)
        assert not full[0].diff_truncated and full[0].diff and "int x;" in full[0].diff


def test_repeated_attempt_ids_get_distinct_records(cfg, ws):
    """MSA: the jdk21 loop of every service numbers its attempts from jdk21-1-1."""
    t = ErrorTracker(ws, cfg)
    a_err = err("svc-a/src/main/java/A.java", module="svc-a")
    b_err = err("svc-b/src/main/java/B.java", module="svc-b")
    t.observe(Phase.JDK21, "jdk21-1", bad(a_err, failed="svc-a"), [], target="svc-a")
    a1 = attempt("jdk21-1-1", [a_err])
    t.note_attempt(a1)
    t.observe(Phase.JDK21, "jdk21-2", ok(), [a1], target="svc-a")
    t.observe(Phase.JDK21, "jdk21-1", bad(b_err, failed="svc-b"), [], target="svc-b")
    b1 = attempt("jdk21-1-1", [b_err], outcome="no_change")
    t.note_attempt(b1)
    errors, attempts = t.finalize([], run_id="r1")
    assert [a.id for a in attempts] == ["r1:jdk21-1-1", "r1:jdk21-1-1#2"]
    st = {e.module: e for e in errors}
    assert st["svc-a"].status == "resolved" and st["svc-a"].resolved_by == "r1:jdk21-1-1"
    assert st["svc-a"].resolved_at == "jdk21-2@svc-a"
    assert st["svc-b"].status == "unresolved" and st["svc-b"].attempts[0].attempt_id == "r1:jdk21-1-1#2"


def test_targeted_build_does_not_close_other_services(cfg, ws):
    t = ErrorTracker(ws, cfg)
    a_err = err("svc-a/src/main/java/A.java", module="svc-a")
    b_err = err("svc-b/src/main/java/B.java", module="svc-b")
    t.observe(Phase.VERIFY, "verify-final", bad(a_err, b_err, failed=None, goal="clean package"), [])
    t.observe(Phase.VERIFY, "verify-final-2", ok("clean package"), [], target="svc-a")
    errors, _ = t.finalize([], run_id="r1")
    st = {e.module: e.status for e in errors}
    assert st == {"svc-a": "resolved_indirectly", "svc-b": "unresolved"}


def test_skipped_tests_and_lower_goals_do_not_prove_test_errors_gone(cfg, ws):
    t = ErrorTracker(ws, cfg)
    tc = err("src/test/java/FooTest.java", kind=ErrorKind.TEST_COMPILE, sig="test_compile|x")
    t.observe(Phase.SPRING, "spring-1", bad(tc, goal="test-compile"), [])
    t.observe(Phase.SPRING, "spring-2", ok("compile"), [])                       # compile never reaches tests
    t.observe(Phase.VERIFY, "verify-final", ok("clean package", command=["mvn", "clean", "package",
                                                                         "-Dmaven.test.skip=true"]), [])
    errors, _ = t.finalize([], run_id="r1")
    assert errors[0].status == "unresolved"
    assert any("tests not compiled" in n for n in errors[0].notes)


def test_crashed_build_is_no_stage_evidence(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    crash = BuildError(kind=ErrorKind.UNKNOWN, message="mvn exited with code 1 without BUILD SUCCESS")
    t.observe(Phase.SPRING, "spring-2", bad(crash, failed=None), [])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.error_kind: e.status for e in errors}
    assert st == {ErrorKind.COMPILE: "not_visible", ErrorKind.UNKNOWN: "unresolved"}


def test_new_error_in_a_touched_file_is_noted(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    a = attempt("spring-1-1", [err(FOO)], outcome="regressed")
    t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(err(FOO), err(FOO, msg="';' expected", sig="semi")), [a])
    errors, _ = t.finalize([], run_id="r1")
    new = next(e for e in errors if e.message == "';' expected")
    assert "appeared at spring-2 after attempt(s) spring-1-1 changed its file" in new.notes


def test_attempt_history_is_bounded(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    for i in range(30):
        t.note_attempt(attempt(f"spring-1-{i}", [e], outcome="no_change"))
    errors, attempts = t.finalize([], run_id="r1")
    assert len(attempts) == 30 and len(errors[0].attempts) == 20 and errors[0].attempt_count == 30
    assert errors[0].attempts[0].attempt_id == "r1:spring-1-0"
    assert errors[0].attempts[-1].attempt_id == "r1:spring-1-29"


def test_public_methods_never_raise(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "x", None, [])                          # type: ignore[arg-type]
    t.observe_clusters([object()])                                  # type: ignore[list-item]
    t.note_attempt(None)                                            # type: ignore[arg-type]
    t.note_decision(None)                                           # type: ignore[arg-type]
    assert t.finalize(None, run_id="r1") == ([], [])                # type: ignore[arg-type]
    assert t.problems and all(isinstance(p, str) for p in t.problems)


def test_resolution_chain_keeps_earlier_attempts_that_stayed_in_the_tree(cfg, ws):
    # 2026-10-25: a no_change attempt (it added an import, say) stays in the tree and is part of the fix the next
    # attempt completes; reverted / unapplied attempts, other files' attempts and attempts that predate the error are not
    t = ErrorTracker(ws, cfg)
    early = attempt("jdk21-1-1", [err(BAR, msg="b")], changes=[FileChange(path=FOO, op="modify")])
    t.note_attempt(early)
    t.observe(Phase.JDK21, "jdk21-1", bad(err(FOO), err(BAR, msg="b")), [early])     # FOO first seen here
    e = err(FOO)
    kept = attempt("spring-1-1", [e], outcome="no_change")
    gone = attempt("spring-1-2", [e], outcome="reverted")
    unapplied = attempt("spring-1-3", [e], outcome="apply_failed", applied=False)
    other = attempt("spring-1-4", [err(BAR, msg="b")], outcome="no_change",
                    changes=[FileChange(path=BAR, op="modify")])
    for a in (kept, gone, unapplied, other):
        t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO), err(BAR, msg="b")), [kept, gone, unapplied, other])
    fix = attempt("spring-2-1", [e])
    t.note_attempt(fix)
    t.observe(Phase.SPRING, "spring-2", ok(), [fix])
    errors, attempts = t.finalize([], run_id="r1")
    foo, bar = by_file(errors, FOO)[0], by_file(errors, BAR)[0]
    assert foo.status == "resolved" and foo.resolved_by == "r1:spring-2-1"
    assert foo.resolution_attempts == ["r1:spring-1-1", "r1:spring-2-1"]
    assert "resolved by 2 attempts whose changes stayed in the tree: spring-1-1, spring-2-1" in foo.notes
    # BAR went away after FOO's fix: the attempt aimed at it that stayed in the tree belongs to its resolution too
    assert bar.status == "resolved_indirectly" and bar.resolution_attempts == ["r1:spring-1-4", "r1:spring-2-1"]
    assert len(attempts) == 6                              # every attempt keeps its own record either way


def test_resolution_chain_of_one_and_reset_on_reopen(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    first = attempt("spring-1-1", [err(FOO)])
    t.note_attempt(first)
    t.observe(Phase.SPRING, "spring-2", ok(), [first])
    t.observe(Phase.VERIFY, "verify-1", bad(err(FOO)), [])                           # reopened
    second = attempt("verify-1-1", [err(FOO)])
    t.note_attempt(second)
    t.observe(Phase.VERIFY, "verify-2", ok(), [second])
    errors, _ = t.finalize([], run_id="r1")
    foo = errors[0]
    assert foo.resolved_by == "r1:verify-1-1" and foo.resolution_attempts == ["r1:verify-1-1"]
    assert not any(n.startswith("resolved by ") for n in foo.notes)


# --------------------------------------------------------------------------- round 10 (2026-10-26)
def test_introduced_by_the_attempt_that_changed_its_file(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
    a = attempt("spring-1-1", [err(FOO)], changes=[FileChange(path=BAR, op="modify")], outcome="no_change")
    t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(err(FOO), err(BAR, msg="';' expected", sig="semi")), [a])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.file: e for e in errors}
    assert st[BAR].introduced_by == "r1:spring-1-1" and st[FOO].introduced_by == ""


def test_introduced_by_the_plan_steps_since_the_previous_build(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.note_step("jdk21.rewrite", Phase.JDK21)
    t.observe(Phase.JDK21, "jdk21-1", bad(err(FOO)), [])
    t.note_step("spring.rewrite", Phase.SPRING)
    t.observe(Phase.SPRING, "spring-1", bad(err(FOO), failed=None), [], void=True)    # a void build keeps them
    t.note_step("restructure.apply", Phase.RESTRUCTURE)
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(err(FOO), err(BAR, msg="x", sig="x")), [])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.file: e for e in errors}
    assert st[FOO].introduced_by == "step:jdk21.rewrite"
    assert st[BAR].introduced_by == "step:spring.rewrite,step:restructure.apply"


def test_targeted_builds_never_merge_two_services_errors(cfg, ws):
    """Round 10 (C8): svc-b's `src/main/java/X.java` matched svc-a's OPEN record of the same relative path — two
    services' errors became one record."""
    t = ErrorTracker(ws, cfg)
    a_err = err("svc-a/src/main/java/X.java", module="svc-a")
    b_err = err("svc-b/src/main/java/X.java", module="svc-b")
    t.observe(Phase.SPRING, "spring-1", bad(a_err, failed="svc-a"), [], target="svc-a")
    t.observe(Phase.SPRING, "spring-1", bad(b_err, failed="svc-b"), [], target="svc-b/pom.xml")
    t.observe(Phase.SPRING, "spring-2", ok(), [], target="svc-a")
    errors, _ = t.finalize([], run_id="r1")
    st = {e.module: e for e in errors}
    assert len(errors) == 2 and st["svc-a"].status == "resolved_indirectly" and st["svc-b"].status == "unresolved"
    assert st["svc-b"].first_seen == "spring-1@svc-b/pom.xml" and st["svc-b"].builds_present == 1


def test_a_whole_project_build_prefers_the_exact_file(cfg, ws):
    t = ErrorTracker(ws, cfg)
    a_err = err("svc-a/src/main/java/X.java", module="svc-a", line=3)
    b_err = err("svc-b/src/main/java/X.java", module="svc-b", line=30)
    t.observe(Phase.SPRING, "spring-1", bad(a_err, b_err, failed=None), [])
    # the next build: line numbers moved so that "nearest line" alone would swap them
    t.observe(Phase.SPRING, "spring-2", bad(a_err.model_copy(update={"line": 29}),
                                            b_err.model_copy(update={"line": 4}), failed=None), [])
    errors, _ = t.finalize([], run_id="r1")
    st = {e.module: e for e in errors}
    assert st["svc-a"].file.startswith("svc-a/") and st["svc-a"].line == 29 and st["svc-b"].line == 4
    # a cluster error that is not the last build's exact copy still maps to its own file's record only
    t2 = ErrorTracker(ws, cfg)
    t2.observe(Phase.SPRING, "spring-1", bad(a_err, b_err, failed=None), [])
    keys = t2._keys_for(b_err.model_copy(update={"line": 99}))
    assert [t2._recs[k].error.file for k in keys] == ["svc-b/src/main/java/X.java"]


def test_a_targeted_build_still_follows_the_restructure_move(cfg, ws):
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.SPRING, "spring-1", bad(err("src/main/java/a/B.java")), [])
    t.note_step("restructure.apply", Phase.RESTRUCTURE)
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(err("shop-back/src/main/java/a/B.java", module="shop-back"),
                                                      failed="shop-back"), [], target="shop-back")
    errors, _ = t.finalize([], run_id="r1")
    assert len(errors) == 1 and errors[0].builds_present == 2 and errors[0].file.startswith("shop-back/")


def test_partial_and_inconclusive_attempts_explain_themselves(cfg, ws):
    """Round 10 outcomes: an unresolved error's last attempt was `partial` / `inconclusive` → the loop's own note is
    the failure text and the error itself is the problem (not an empty list)."""
    for outcome, note in (("partial", "partial: 1 of 2 targeted error(s) gone, 1 remain"),
                          ("inconclusive", "inconclusive: the build timed out")):
        t = ErrorTracker(ws, cfg)
        t.observe(Phase.SPRING, "spring-1", bad(err(FOO)), [])
        a = attempt("spring-1-1", [err(FOO)], outcome=outcome, notes=[note])
        t.note_attempt(a)
        t.observe(Phase.SPRING, "spring-2", bad(err(FOO)), [a])
        errors, attempts = t.finalize([], run_id="r1")
        assert attempts[0].failure == note
        assert errors[0].status == "unresolved" and errors[0].problem_errors
        assert note in errors[0].failure_reason


def test_records_carry_the_refined_case_key_of_a_catch_all_compile_error(cfg, ws):
    """PLAN-v2 step 4: a file-less `Compilation failure` gets its refined L2 (sbmig.triage.cluster) before the
    tracker matches it — also when the raw text is long enough for the tracker to store a capped copy — so the error
    record's case is the one the fix loop and the case catalog use."""
    from sbmig.build.signatures import fill_signatures
    from sbmig.triage import cluster_cases

    raw = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.16.0:compile "
           "(default-compile) on project demo: Compilation failure: Compilation failure: \n"
           "[ERROR] error: option --boot-class-path cannot be used together with --release\n"
           + "[ERROR] use --help for a list of possible options\n" * 60)
    e = fill_signatures(BuildError(kind=ErrorKind.COMPILE, message="Compilation failure: Compilation failure:",
                                   module="demo", raw=raw))
    assert e.signature_generic == "compile|build|compilation failure: compilation failure" and len(raw) > 2000
    t = ErrorTracker(ws, cfg)
    t.observe(Phase.VERIFY, "verify-final", bad(e), [])
    errors, _ = t.finalize([], run_id="r1")
    refined = "compile|build|option --boot-class-path cannot be used together with --release"
    assert [r.signature_generic for r in errors] == [refined] and errors[0].signature == refined
    assert cluster_cases([e])[0].case_id == hashlib.sha1(refined.encode("utf-8")).hexdigest()[:10]
