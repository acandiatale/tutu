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


def attempt(aid: str, errors: list[BuildError], *, kind: DecisionKind = DecisionKind.LLM_FIX, outcome="improved",
            changes: Optional[list[FileChange]] = None, applied=True, **kw) -> FixAttempt:
    d = TriageDecision(cluster_id=f"c-{aid}", kind=kind, handler_id="h1" if kind == DecisionKind.HANDLER else None,
                       recipe_ids=["org.x.R"] if kind == DecisionKind.KB_RECIPE else [])
    return FixAttempt(id=aid, phase=Phase.SPRING, iteration=1, cluster_id=f"c-{aid}", decision=d,
                      cluster_errors=list(errors), cluster_signatures=[e.signature for e in errors],
                      changes=changes if changes is not None else [FileChange(path=errors[0].file or "pom.xml",
                                                                              op="modify")],
                      applied=applied, outcome=outcome, model="m1" if kind == DecisionKind.LLM_FIX else "",
                      **kw)


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
                      kind=DecisionKind.HANDLER)
    for a in (a_target, a_touch):
        t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(e3, e4, e5), [a_target, a_touch])
    # e3 disappears after an unrelated attempt, e4 after a plan step, e5 with nothing at all
    a_other = attempt("spring-2-1", [e5], kind=DecisionKind.KB_RECIPE,
                      changes=[FileChange(path="src/main/java/Q.java", op="modify")])
    t.note_attempt(a_other)
    t.observe(Phase.SPRING, "spring-3", bad(e4, e5), [a_other])
    t.note_step("restructure.apply", Phase.RESTRUCTURE)
    t.observe(Phase.RESTRUCTURE, "restructure-1", bad(e5), [])
    t.observe(Phase.VERIFY, "verify-final", ok("clean package"), [])
    errors, attempts = t.finalize([], run_id="r1")
    r = {e.file.rsplit("/", 1)[-1]: e for e in errors}
    assert (r["E1.java"].status, r["E1.java"].targeted, r["E1.java"].fix_method) == ("resolved", True, "llm")
    assert r["E1.java"].resolved_by == "r1:spring-1-1" and r["E1.java"].resolver_id == "m1"
    assert r["E1.java"].description.startswith("LLM m1")
    assert (r["E2.java"].status, r["E2.java"].targeted, r["E2.java"].fix_method) == ("resolved", False, "handler")
    assert r["E2.java"].resolved_by == "r1:spring-1-2"
    assert (r["E3.java"].status, r["E3.java"].fix_method) == ("resolved_indirectly", "recipe")
    assert r["E3.java"].resolver_id == "org.x.R" and r["E3.java"].resolved_by == "r1:spring-2-1"
    assert (r["E4.java"].status, r["E4.java"].fix_method) == ("resolved_indirectly", "plan_step")
    assert r["E4.java"].resolved_by == "step:restructure.apply" and r["E4.java"].resolved_phase == Phase.RESTRUCTURE
    # e5 was targeted by the KB recipe attempt (spring-2-1) but only disappeared at the final build
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
                 apply_errors=["src/main/java/F0.java: search not found"],
                 proposed_changes=[FileChange(path=e_apply.file, op="modify", search="old();", replace="neu();")])
    a2 = attempt("spring-1-2", [e_nochange], outcome="no_change")
    intro = err(e_regr.file, msg="';' expected", sig="compile|semi", line=9)
    a3 = attempt("spring-1-3", [e_regr], outcome="reverted", introduced_errors=[intro],
                 notes=["regressed: 1 new error(s) in touched files", "reverted to abc"])
    for a in (a1, a2, a3):
        t.note_attempt(a)
    t.observe(Phase.SPRING, "spring-2", bad(e_apply, e_nochange, e_regr, e_dec, e_none), [a2])
    errors, attempts = t.finalize([], run_id="r1")
    r = {e.file.rsplit("/", 1)[-1]: e for e in errors}
    assert r["F0.java"].failure_reason == "llm attempt spring-1-1 apply_failed: src/main/java/F0.java: search not found"
    assert r["F0.java"].problem_errors == ["src/main/java/F0.java: search not found"]
    assert r["F1.java"].failure_reason == \
        "llm attempt spring-1-2 no_change: the targeted error(s) were still there after the rebuild"
    assert r["F1.java"].problem_errors == ["src/main/java/F1.java:3 cannot find symbol"]
    assert r["F2.java"].failure_reason.startswith("llm attempt spring-1-3 reverted: regressed: 1 new error(s)")
    assert "';' expected" in r["F2.java"].failure_reason and r["F2.java"].failure_reason.endswith("(reverted)")
    assert r["F2.java"].problem_errors == ["src/main/java/F2.java:9 ';' expected"]
    assert r["F3.java"].failure_reason == "no fix attempt: escalated: 2 LLM attempts failed"
    assert r["F4.java"].failure_reason == "no fix attempt before the loop ended"
    assert r["F0.java"].attempts[0].failure == "src/main/java/F0.java: search not found"
    assert r["F0.java"].attempt_count == 1 and r["F0.java"].llm_attempts == 1
    att = {a.attempt_id: a for a in attempts}
    assert "-old();" in att["spring-1-1"].diff and "+neu();" in att["spring-1-1"].diff
    assert att["spring-1-1"].error_ids == [r["F0.java"].id]
    assert att["spring-1-3"].introduced_errors == ["src/main/java/F2.java:9 ';' expected"]
    assert att["spring-1-2"].remaining_errors == ["src/main/java/F1.java:3 cannot find symbol"]


def test_pending_attempt_and_descriptions(cfg, ws):
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    h = attempt("spring-1-1", [e], kind=DecisionKind.HANDLER, outcome="pending", analysis="raise lombok",
                notes=["install lombok 1.18.38"])
    r = attempt("spring-1-2", [e], kind=DecisionKind.KB_RECIPE, outcome="pending")
    llm = attempt("spring-1-3", [e], outcome="pending", analysis="x" * 3000,
                  llm_usage=LLMUsage(model="m1", calls=2, input_tokens=10, output_tokens=5))
    for a in (h, r, llm):
        t.note_attempt(a)
    _, attempts = t.finalize([], run_id="r1")
    d = {a.attempt_id: a for a in attempts}
    assert d["spring-1-1"].description == "handler h1: raise lombok — install lombok 1.18.38"
    assert d["spring-1-1"].fix_method == "handler" and d["spring-1-1"].resolver_id == "h1"
    assert d["spring-1-2"].description == "OpenRewrite org.x.R" and d["spring-1-2"].fix_method == "recipe"
    assert d["spring-1-3"].description.startswith("LLM m1: xxx") and len(d["spring-1-3"].description) < 1600
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


def test_attempt_diff_is_capped(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.kb.error_diff_chars = 60
    t = ErrorTracker(ws, cfg)
    e = err(FOO)
    t.observe(Phase.SPRING, "spring-1", bad(e), [])
    base = ws.head()
    ws.write(FOO, "class Foo {\n" + "  int x;\n" * 50 + "}\n")
    a = attempt("spring-1-1", [e], base_commit=base, commit=ws.commit("big"))
    t.note_attempt(a)
    _, attempts = t.finalize([], run_id="r1")
    assert attempts[0].diff_truncated and len(attempts[0].diff) <= 60


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
