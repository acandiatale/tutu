"""Failure-case handling in the loop/orchestrator (docs/ERROR-HANDLING.md §5, §6), v2 fix scripts: blocked cases,
loop safety (oscillation, structural regression, garbage / unavailable LLM, budgets), optional_bump dispatch,
intake effective-pom capture, aborts and exit codes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sbmig.build import parse_maven_log
from sbmig.cli import run_exit_code
from sbmig.core.config import ArtifactSpec
from sbmig.core.models import (GAV, BlockReason, BuildError, CaseType, DecisionKind, ErrorKind, FileChange,
                               LLMUsage, MigrationPlan, Phase, PlanStep, RunReport, StepKind)
from sbmig.loop import BuildFixLoop, LazyFixer, LoopBudget
from sbmig.triage import Triager

from .fakes import (FixProposal, FakeBuildRunner, FakeFixer, FakeKB, FakeRestructurer, FakeRewriteRunner, FakeScriptRunner,
                    bad_build, case_of, compile_error, journal_events, make_profile, ok_build, v2_msa_plan, v2_plan,
                    write_fix, write_script)
from .test_orchestrator import make_orch


@pytest.fixture(autouse=True)
def javax_library_blocks_at_once(cfg):
    """These scenarios are about blocking: a library that still exposes javax types is blocked without an LLM call
    (`blocked_policy.external_javax_api: 0`; the v2 default 2 lets a script try to swap the library first)."""
    cfg.loop.blocked_policy["external_javax_api"] = 0

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
HDR = "[INFO] --- maven-compiler-plugin:3.13.0:compile (default-compile) @ demo ---\n[ERROR] COMPILATION ERROR : \n"
X1_BLOCK = (
    "[ERROR] {root}/" + FOO + ":[12,9] constructor LegacyRequestHelper in class "
    "com.company.legacy.web.LegacyRequestHelper cannot be applied to given types;\n"
    "  required: javax.servlet.http.HttpServletRequest\n"
    "  found:    jakarta.servlet.http.HttpServletRequest\n"
    "  reason: argument mismatch; jakarta.servlet.http.HttpServletRequest cannot be converted to "
    "javax.servlet.http.HttpServletRequest\n")
PLAIN_FOO = ("[ERROR] {root}/" + FOO + ":[30,5] cannot find symbol\n  symbol:   class Missing\n"
             "  location: class com.x.Foo\n")


def javac(ws, *blocks: str) -> list[BuildError]:
    text = HDR + "".join(b.format(root=ws.root) for b in blocks) + "[INFO] BUILD FAILURE\n"
    return parse_maven_log(text, ws.root)


def make_loop(cfg, ws, profile, *, build, fixer=None, triager=None, budget=None, locator=None, kb=None):
    triager = triager or Triager(cfg)
    return BuildFixLoop(cfg, ws, build, triager, fixer or FakeFixer(), kb or FakeKB(),
                        profile, budget=budget or LoopBudget.from_config(cfg),
                        locator=locator, script_runner=FakeScriptRunner())


def own_case(f: str):
    """compile errors of `f` in a case of their own (L2 per file)."""
    return compile_error(f, l2=f"compile|x|{f.rsplit('/', 1)[-1]}")


class FakeLocator:
    def __init__(self, gav="com.company:legacy-web:1.4"):
        self.gav = GAV.parse(gav)
        self.calls: list[tuple] = []

    def locate_class(self, ws, cfg, fqcn, module=None):
        self.calls.append(("class", fqcn, module))
        return SimpleNamespace(artifact=self.gav, jar=f"/m2/{self.gav.artifact_id}.jar", in_project=False, fqcn=fqcn)

    def locate_type_owner(self, ws, cfg, error, module=None):  # pragma: no cover - owner is always known here
        self.calls.append(("owner", error.message, module))
        return None


def javax_lib_build(ws, call):
    """Foo calls a library that still takes javax; Bar has a plain error while it says `broken`."""
    blocks = [X1_BLOCK] + ([PLAIN_FOO.replace(FOO, BAR).replace("com.x.Foo", "com.x.Bar")]
                           if "broken" in ws.read(BAR) else [])
    return bad_build(*javac(ws, *blocks))


# ------------------------------------------------------------------------------------------------ blocked flow
def test_blocked_case_is_reported_and_the_rest_is_fixed(cfg, ws):
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix(BAR, "class Bar { ok }\n"))
    restr = FakeRestructurer()
    loc = FakeLocator()
    build = FakeBuildRunner(ws, fn=javax_lib_build)
    report = make_orch(cfg, ws, build=build, fixer=fixer, restructurer=restr, locator=loc).run()

    assert not report.success and run_exit_code(report) == 2
    assert restr.calls == ["layout"]
    assert build.calls[-1]["label"] == "verify"
    assert "class Foo { broken }" in ws.read(FOO)                      # never touched Foo
    assert len(fixer.contexts) == 1                                     # only Bar's case went to the LLM
    [item] = report.blocked
    assert item.reason == BlockReason.EXTERNAL_JAVAX_API and item.phase == Phase.VERIFY
    assert item.artifact.coordinates == "com.company:legacy-web:1.4"
    assert item.owner_class == "com.company.legacy.web.LegacyRequestHelper"
    assert (item.required_type, item.found_type) == ("javax.servlet.http.HttpServletRequest",
                                                     "jakarta.servlet.http.HttpServletRequest")
    assert item.files == [f"{FOO}:12"] and item.attempts == 0 and item.errors
    assert "upgrade `com.company:legacy-web` to a Jakarta build" in item.suggestion
    assert "left unchanged on purpose" in item.suggestion
    # v2.1d: the fix loop's library hints ask the locator too (locate_type_owner for Bar's case) before the blocked
    # registry attributes Foo's case — the attribution call is still there, no longer the first one
    assert ("class", "com.company.legacy.web.LegacyRequestHelper") in [c[:2] for c in loc.calls]
    verify = report.phases[-1]
    assert [b.id for b in verify.blocked] == [item.id]
    assert any("only blocked cluster(s) remain" in n for n in verify.notes)
    assert report.unresolved == []                                      # blocked errors are not "unresolved"
    md = (ws.run_dir / "report.md").read_text()
    assert md.index("## Error list") < md.index("## Blocked — needs human / library action")
    assert "| **Blocked** | 1 item(s) — external_javax_api 1" in md
    assert "`com.company:legacy-web:1.4` (class `com.company.legacy.web.LegacyRequestHelper`)" in md
    blocked_ev = [e for e in journal_events(ws) if e["event"] == "blocked"]
    assert [e["created"] for e in blocked_ev] == [True] and blocked_ev[0]["reason"] == "external_javax_api"
    back = RunReport.model_validate_json((ws.run_dir / "report.json").read_text())
    assert back.blocked == report.blocked


def test_optional_artifact_suggestion_points_at_the_bump(cfg, ws):
    cfg.artifacts.optional.append(ArtifactSpec(group_id="com.company", artifact_id="legacy-web", version="2.0.0"))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(*javac(w, X1_BLOCK)))
    loop = make_loop(cfg, ws, make_profile(), build=build, locator=FakeLocator())
    pr = loop.run(Phase.SPRING, "test-compile", 3)
    s = pr.blocked[0].suggestion
    assert "`com.company:legacy-web` is a configured optional artifact" in s
    assert "bump to `2.0.0` happens in the restructure" in s and "Jakarta build" in s


def test_unattributed_owner_suggestion(cfg, ws):
    loop = make_loop(cfg, ws, make_profile(), build=FakeBuildRunner(ws, fn=lambda w, c: bad_build(*javac(w, X1_BLOCK))))
    pr = loop.run(Phase.SPRING, "compile", 3)
    item = pr.blocked[0]
    assert item.artifact is None
    assert "owner class `com.company.legacy.web.LegacyRequestHelper` could not be attributed to an artifact; check the " \
           "module classpath" in item.suggestion


def test_only_blocked_remaining_ends_phase_early(cfg, ws, profile):
    fixer = FakeFixer()
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(*javac(w, X1_BLOCK)))
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer)
    pr = loop.run(Phase.SPRING, "test-compile", 5)
    assert not pr.ok and not pr.build_ok and pr.iterations == 1 and not fixer.contexts
    assert any("only blocked cluster(s) remain" in n for n in pr.notes)
    pr2 = loop.run(Phase.RESTRUCTURE, "package", 5)                    # next phase: skipped without deciding
    assert pr2.iterations == 1 and pr2.iteration_records[0].decisions == []
    assert [b.id for b in pr2.blocked] == [b.id for b in pr.blocked]
    assert len(loop.blocked) == 1
    assert sum(e["event"] == "only_blocked" for e in journal_events(ws)) == 2


def test_partially_blocked_case_rest_goes_to_the_script(cfg, ws, profile):
    """One case (same L2) whose errors the classifier covers only in part: the javax one is blocked, the rest of the
    case goes to the script."""
    seen = []

    def fix(w, c, ctx):
        seen.append([e.message for e in c.errors])
        return write_fix(FOO, "class Foo { fixed }\n")

    def build_fn(w, call):
        blocks = [X1_BLOCK] + ([PLAIN_FOO] if "broken" in w.read(FOO) else [])
        errs = javac(w, *blocks)
        # one L2 for both (a key segment: a catch-all `compile|mixed` would be refined into two cases)
        return bad_build(*[e.model_copy(update={"signature_generic": "compile|mixed|case"}) for e in errs])

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert seen == [["cannot find symbol"]]                             # only the fixable error was sent
    assert pr.attempts[0].outcome == "improved" and len(pr.blocked) == 1
    d = pr.iteration_records[0].decisions
    assert [x.kind for x in d] == [DecisionKind.BLOCKED, DecisionKind.SCRIPT]
    assert "rest of a partially blocked case" in d[1].rationale
    assert pr.iterations == 2 and any("only blocked" in n for n in pr.notes)


# ------------------------------------------------------------------------------------------------ loop safety
def test_oscillation_blocks_involved_clusters(cfg, ws, profile):
    def build_fn(w, call):
        msg = "cannot find symbol A" if "A" in w.read(FOO) else "cannot find symbol B"
        return bad_build(compile_error(FOO, msg, sig=f"sig-{msg[-1]}"))

    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_fix(FOO, f"class Foo {{ {'B' if 'A' in w.read(FOO) else 'A'} {n['i']} }}\n")

    ws.write(FOO, "class Foo { A }\n")
    ws.commit("A")
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert [r.signatures for r in pr.iteration_records][:4] == [{"sig-A": 1}, {"sig-B": 1}, {"sig-A": 1}, {"sig-B": 1}]
    assert [r.stalled for r in pr.iteration_records][:4] == [False, False, True, True]
    assert pr.iterations == 4 and n["i"] == 3
    assert [b.reason for b in pr.blocked] == [BlockReason.OSCILLATION]
    assert sum(e["event"] == "oscillation" for e in journal_events(ws)) == 2


def _pom_invalid(_):
    return BuildError(kind=ErrorKind.POM_INVALID, message="Non-parseable POM pom.xml: unexpected end tag", file="pom.xml",
                      signature="pom_invalid|non-parseable")


def test_structural_regression_after_pom_edit_is_reverted(cfg, ws, profile):
    good_pom = ws.read("pom.xml")

    def build_fn(w, call):
        if "<broken" in w.read("pom.xml"):
            return bad_build(_pom_invalid(w))                      # 1 error before, 1 after: no ratio regression
        return bad_build(compile_error(FOO)) if "broken" in w.read(FOO) else ok_build()

    proposals = [FixProposal(changes=[FileChange(path="pom.xml", op="create", content="<project><broken"),
                                      FileChange(path=FOO, op="create", content="class Foo { ok }\n")],
                             model="m", llm_usage=LLMUsage(calls=1)),
                 write_fix(FOO, "class Foo { ok }\n")]
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(proposals))
    pr = loop.run(Phase.VERIFY, "package", 4)
    first = pr.attempts[0]
    # its compile error is gone, but the build stopped at the pom (round 10: unknown, not resolved) — reverted
    assert first.cluster_resolved is None and first.outcome == "reverted"
    assert any("structural error kind(s) pom_invalid" in n for n in first.notes)
    assert pr.ok and ws.read("pom.xml") == good_pom


def _dep_resolution(_):
    return BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, file="pom.xml", module="demo",
                      message="Could not find artifact com.company:sso-client:jar:1.0.0 in nexus (https://nexus/repository/maven-public/)",
                      artifact=GAV(group_id="com.company", artifact_id="sso-client", version="1.0.0"),
                      signature="dependency_resolution|com.company:sso-client:1.0.0",
                      signature_generic="dependency_resolution|com.company:sso-client")


def test_pom_fix_that_uncovers_the_next_stage_is_not_a_regression(cfg, ws, profile):
    """Company run 2026-11-05: the script removed the version-less spring-dao (a stage-0 pom problem), the build advanced to
    dependency resolution and showed `Could not find artifact …:SSOClient_Common:1.0.0` for the first time — a structural kind
    the previous build never reached. That is the next stage uncovered, not a regression caused by the pom edit: the attempt
    stays (improved) instead of being reverted round after round (the case had reached script version 24)."""
    def build_fn(w, call):
        pom = w.read("pom.xml")
        if "<broken" in pom:                                  # stage 0: the pom problem
            return bad_build(_pom_invalid(w))
        if "<missing-sso" in pom:                             # stage 1: visible only once the pom problem is gone
            return bad_build(_dep_resolution(w))
        return ok_build()

    ws.write("pom.xml", "<project><broken<missing-sso")
    ws.commit("as-is with a pom problem that hides a dependency problem")
    proposals = [FixProposal(changes=[FileChange(path="pom.xml", op="create", content="<project><missing-sso")],
                             model="m", llm_usage=LLMUsage(calls=1)),
                 FixProposal(changes=[FileChange(path="pom.xml", op="create", content="<project>")],
                             model="m", llm_usage=LLMUsage(calls=1))]
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(proposals))
    pr = loop.run(Phase.VERIFY, "package", 4)
    first = pr.attempts[0]
    assert first.outcome == "improved" and first.cluster_resolved is True
    assert any("not a regression" in n and "dependency_resolution" in n for n in first.notes)
    assert not any("structural error kind" in n for n in first.notes)
    assert "<broken" not in ws.read("pom.xml")                # the pom fix was kept
    assert pr.ok


def test_new_structural_kind_of_the_same_stage_is_still_a_regression(cfg, ws, profile):
    """The symmetric case keeps its verdict: a pom edit that fixes the dependency-resolution problem (stage 1) but makes a
    plugin unresolvable (a new structural kind of the SAME stage) is a regression and is reverted."""
    good_pom = ws.read("pom.xml")

    def bad_plugin(_):
        return BuildError(kind=ErrorKind.PLUGIN_RESOLUTION, file="pom.xml", module="demo",
                          message="Plugin com.company:made-up-plugin:9 or one of its dependencies could not be resolved",
                          artifact=GAV(group_id="com.company", artifact_id="made-up-plugin", version="9"),
                          signature="plugin_resolution|com.company:made-up-plugin:9",
                          signature_generic="plugin_resolution|com.company:made-up-plugin")

    def build_fn(w, call):
        pom = w.read("pom.xml")
        if "<bad-plugin" in pom:
            return bad_build(bad_plugin(w))
        if "<missing-sso" in pom:
            return bad_build(_dep_resolution(w))
        return ok_build()

    ws.write("pom.xml", good_pom + "<missing-sso")
    ws.commit("as-is with a dependency problem")
    proposals = [FixProposal(changes=[FileChange(path="pom.xml", op="create", content=good_pom + "<bad-plugin")],
                             model="m", llm_usage=LLMUsage(calls=1)),
                 FixProposal(changes=[FileChange(path="pom.xml", op="create", content=good_pom)],
                             model="m", llm_usage=LLMUsage(calls=1))]
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(proposals))
    pr = loop.run(Phase.VERIFY, "package", 4)
    first = pr.attempts[0]
    assert first.outcome == "reverted" and any("structural error kind(s) plugin_resolution" in n for n in first.notes)


def test_structural_regression_reverted_even_when_not_the_last_commit(cfg, ws, profile):
    """Foo's script breaks the pom, Bar's case (a stored script: a reuse is never deferred by the pom change) is
    committed after it in the same round → the structural revert of Foo's commit is a `git revert`."""
    good_pom = ws.read("pom.xml")
    cfg.loop.max_clusters_per_iteration = 2
    kb = FakeKB()
    kb.cases.seed(case_of(f"compile|x|{BAR.rsplit('/', 1)[-1]}"),
                    write_script({BAR: "class X { ok }\n"}).code)

    def build_fn(w, call):
        errs = []
        if "<broken" in w.read("pom.xml"):
            return bad_build(_pom_invalid(w))
        errs += [compile_error(FOO, line=n, l2="compile|x|foo") for n in (1, 2) if "broken" in w.read(FOO)]
        errs += [own_case(BAR)] if "broken" in w.read(BAR) else []      # Foo's case (2 errors) comes first
        return bad_build(*errs) if errs else ok_build()

    calls = {FOO: 0}

    def fix(w, c, ctx):
        f = c.errors[0].file
        if f == FOO:
            calls[FOO] += 1
            if calls[FOO] == 1:
                return FixProposal(changes=[FileChange(path="pom.xml", op="create", content="<project><broken")],
                                   model="m", llm_usage=LLMUsage(calls=1))
        return write_fix(f, "class X { ok }\n")

    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(fn=fix), kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 4)
    foo1 = pr.attempts[0]
    assert pr.attempts[1].script_mode == "reuse" and pr.attempts[1].iteration == foo1.iteration
    assert foo1.outcome == "reverted" and any("git revert" in n for n in foo1.notes)
    assert any(e["event"] == "revert" and e.get("mode") == "git-revert" for e in journal_events(ws))
    assert pr.ok and ws.read("pom.xml") == good_pom


def test_garbage_llm_is_blocked_as_repeated_failure_within_budget(cfg, ws, profile):
    prose = FixProposal(changes=[], analysis="llm-error: response did not follow the protocol: Sure! Here is how…",
                        model="m", llm_usage=LLMUsage(calls=1))
    fixer = FakeFixer(fn=lambda w, c, ctx: prose)
    build = FakeBuildRunner(ws, script=[bad_build(compile_error(FOO))])
    cfg.script.max_llm_tries_per_case = 3
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 6)
    n = cfg.script.max_llm_tries_per_case                              # no script ever: `new` again, same total
    assert len(fixer.contexts) == n and {c.script_mode for c in fixer.contexts} == {"new"}
    assert len(build.calls) == 1                                       # re-asked on the same build, no rebuild
    assert [b.reason for b in pr.blocked] == [BlockReason.REPEATED_FAILURE] and pr.blocked[0].attempts == n
    assert loop.llm_unavailable is None and "llm-unavailable" not in loop.flags
    assert loop.budget.llm_calls == n and loop.budget.llm_allowed()      # v2.1: no LLM cap by default
    assert any(e["event"] == "reround" for e in journal_events(ws))


def test_llm_unavailable_blocks_llm_cases_stored_scripts_keep_running(cfg, ws, profile):
    cfg.llm.cooldown_seconds = 0                                       # pre-round-9 behaviour: no cool-down
    down = FixProposal(changes=[], analysis="llm-error: LLM request failed after 4 attempt(s) (status=503): busy",
                       model="m", llm_usage=LLMUsage(calls=1))
    fixer = FakeFixer(fn=lambda w, c, ctx: down)
    kb = FakeKB()
    build = FakeBuildRunner(ws, script=[bad_build(own_case(FOO), own_case(BAR))])
    loop = make_loop(cfg, ws, profile, build=build, fixer=fixer, kb=kb)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert len(fixer.contexts) == 2 and loop.llm_unavailable and "status=503" in loop.llm_unavailable
    assert "llm-unavailable" in loop.flags
    assert {b.reason for b in pr.blocked} == {BlockReason.LLM_UNAVAILABLE} and len(pr.blocked) == 2
    assert "LLM was unavailable" in pr.blocked[0].suggestion
    assert any(e["event"] == "llm_unavailable" for e in journal_events(ws))
    # next loop: a new LLM-only case is blocked without calling the fixer; a case with a proven script is reused
    baz, qux = "src/main/java/com/x/Baz.java", "src/main/java/com/x/Qux.java"
    ws.write(baz, "class Baz { broken }\n")
    ws.commit("baz")
    kb.cases.seed(case_of("compile|x|Baz.java"), write_script({baz: "class Baz { ok }\n"}).code)

    def build2(w, call):
        errs = [own_case(qux)] + ([own_case(baz)] if "broken" in w.read(baz) else [])
        return bad_build(*errs)

    loop.build_runner = FakeBuildRunner(ws, fn=build2)
    pr2 = loop.run(Phase.VERIFY, "package", 4)
    assert len(fixer.contexts) == 2                                    # the LLM is not called again
    assert [a.script_mode for a in pr2.attempts] == ["reuse"] and pr2.attempts[0].outcome == "improved"
    assert any(b.reason == BlockReason.LLM_UNAVAILABLE and qux in b.files[0] for b in pr2.blocked)


def test_llm_client_construction_failure_marks_llm_unavailable(cfg, ws, profile):
    cfg.llm.cooldown_seconds = 0
    def factory():
        raise ValueError("llm.base_url is not set (LLM_BASE_URL)")

    build = FakeBuildRunner(ws, script=[bad_build(own_case(FOO), own_case(BAR))])
    loop = make_loop(cfg, ws, profile, build=build, fixer=LazyFixer(factory))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert loop.llm_unavailable and "LLM_BASE_URL" in loop.llm_unavailable
    assert {b.reason for b in pr.blocked} == {BlockReason.LLM_UNAVAILABLE}


def test_time_budget_blocks_remaining_clusters(cfg, ws, profile):
    t = {"now": 0.0}
    budget = LoopBudget(max_llm_calls=10, max_minutes=1, clock=lambda: t["now"])

    def fix(w, c, ctx):
        t["now"] += 120
        return write_fix(c.errors[0].file, "class X { broken 2 }\n")

    def build_fn(w, call):
        return bad_build(*[own_case(f) for f in (FOO, BAR)])

    cfg.loop.max_clusters_per_iteration = 1
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(fn=fix), budget=budget)
    pr = loop.run(Phase.VERIFY, "package", 5)
    assert "time-budget-exhausted" in loop.flags
    assert {b.reason for b in pr.blocked} == {BlockReason.BUDGET_EXHAUSTED}
    assert sorted(b.files[0] for b in pr.blocked) == sorted([FOO, BAR])
    assert "loop.max_total_minutes" in pr.blocked[0].suggestion


# ------------------------------------------------------------------------------------------------ orchestrator glue
def bump_plan() -> MigrationPlan:
    """v2: the optional bump step runs inside the restructure phase."""
    plan = v2_plan()
    i = next(i for i, s in enumerate(plan.steps) if s.id == "restructure.apply")
    plan.steps.insert(i, PlanStep(id="restructure.optional_bump", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
                                  title="bump in-house optional artifacts", params={"mode": "optional_bump",
                                                                                    "only": ["c:d"]}))
    return plan


def test_optional_bump_step_dispatch(cfg, ws):
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, plan=bump_plan(), restructurer=restr,
                       build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert restr.calls == ["optional_bump", "layout"]
    assert restr.params[0] == {"mode": "optional_bump", "only": ["c:d"]}
    ev = [e for e in journal_events(ws) if e["event"] == "restructure"]
    assert [e["mode"] for e in ev] == ["optional_bump", "layout"] and ev[0]["step"] == "restructure.optional_bump"
    assert report.success and run_exit_code(report) == 0


def test_optional_bump_failure_does_not_abort_the_phase(cfg, ws):
    restr = FakeRestructurer(fail_modes=("optional_bump",))
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    report = make_orch(cfg, ws, plan=bump_plan(), restructurer=restr, build=build).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert rs.ok
    assert any("restructure.optional_bump: failed — continuing" in n for n in rs.notes)
    assert restr.calls == ["optional_bump", "layout"] and report.success


def test_intake_captures_effective_depmgmt_before_any_change(cfg, ws, tmp_path):
    seen = []

    def capture(w, c):
        seen.append((w._git("log", "--format=%s").stdout, w.is_dirty()))
        p = w.run_dir / "effective" / "depmgmt.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        return p

    report = make_orch(cfg, ws, effective_capture=capture, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    [(log, dirty)] = seen
    assert not dirty and not any(k in log for k in ("handlers:", "rewrite:", "fix(", "restructure:"))  # as-is only
    assert report.outputs["effective_depmgmt"].endswith("depmgmt.json")
    ev = [e for e in journal_events(ws) if e["event"] == "effective_depmgmt"]
    assert ev and ev[0]["ok"] is True

    def boom(w, c):
        raise RuntimeError("mvn exploded")

    report = make_orch(cfg, ws, effective_capture=boom, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert report.success                                               # never fatal


def test_intake_capture_lazy_import(cfg, ws, monkeypatch):
    import sbmig.build.effective as eff
    from sbmig.loop import Orchestrator

    calls = []
    monkeypatch.setattr(eff, "capture_effective_depmgmt", lambda w, c, module=None: calls.append(w) or None)
    orch = Orchestrator(cfg, ws)                                       # real-Maven mode → lazy import
    report = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=ws_started())
    orch._capture_effective(report)
    assert calls == [ws] and "effective_depmgmt" not in report.outputs
    orch2 = make_orch(cfg, ws)                                         # injected build runner → skipped
    orch2._capture_effective(report)
    assert len(calls) == 1


def ws_started():
    from sbmig.core.models import utcnow
    return utcnow()


def test_configured_parent_not_resolvable_aborts(cfg, ws):
    parent = cfg.artifacts.parent
    perr = BuildError(kind=ErrorKind.PARENT_RESOLUTION, file="pom.xml",
                      message=f"Non-resolvable parent POM for com.x:demo:1: Could not find artifact "
                              f"{parent.key}:pom:{parent.version} in nexus (https://nexus.example/repo)",
                      raw=f"[FATAL] Non-resolvable parent POM ... in nexus (https://nexus.example/repo)",
                      artifact=GAV(group_id=parent.group_id, artifact_id=parent.artifact_id, version=parent.version),
                      signature=f"parent_resolution|{parent.key}")
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(perr))
    restr = FakeRestructurer()
    report = make_orch(cfg, ws, build=build, restructurer=restr).run()
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    # v2: the final build is the first build; its loop (build 1 = that build) aborts — no looping, no repeat
    assert len(build.calls) == 1 and restr.calls == ["layout"]
    [item] = report.blocked
    assert item.reason == BlockReason.PARENT_NOT_FOUND and item.artifact.key == parent.key
    assert "nexus (https://nexus.example/repo)" in item.suggestion
    assert any("run aborted" in f and parent.key in f for f in report.manual_followups)


def test_parent_failure_inside_rewrite_aborts(cfg, ws):
    parent = cfg.artifacts.parent
    rw = FakeRewriteRunner(ws, ok=False, error=f"Non-resolvable parent POM for com.x:demo:1: The following artifacts "
                                               f"could not be resolved: {parent.key}:pom:{parent.version} (absent)")
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM]
    assert report.blocked[0].artifact.coordinates == f"{parent.key}:{parent.version}"


WAGON_EXT = "org.apache.maven.wagon:wagon-webdav:1.0-beta-2"
EXT_MSG = (f"Unresolveable build extension: Plugin {WAGON_EXT} or one of its dependencies could not be "
           f"resolved: Failed to read artifact descriptor for {WAGON_EXT.replace(':1.0', ':jar:1.0')}")


def _chain_with_wagon(monkeypatch):
    from sbmig.build import parent_chain
    facts = parent_chain.ParentChainFacts(roots=["com.company:base-parent:4.0.0"], build_extensions=[WAGON_EXT],
                                          build_extension_sources={WAGON_EXT: "com.company:base-parent:4.0.0"})
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)


def test_build_extension_inherited_from_the_company_parent_aborts_without_an_llm_call(cfg, ws, monkeypatch):
    """2026-11-05 (company PC): base-parent 4.0.0 declares wagon-webdav as a build extension; when Maven cannot download it nothing
    builds and no script can change a parent pom in Nexus — the run aborts like an unresolvable parent, the LLM is never asked."""
    _chain_with_wagon(monkeypatch)
    err = BuildError(kind=ErrorKind.POM_INVALID, file="pom.xml", message=EXT_MSG, raw=EXT_MSG,
                     artifact=GAV(group_id="org.apache.maven.wagon", artifact_id="wagon-webdav", version="1.0-beta-2"))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(err))
    fixer = FakeFixer(script_fn=lambda *a, **k: pytest.fail("the LLM must not be asked for an inherited extension"))
    report = make_orch(cfg, ws, build=build, fixer=fixer).run()
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3 and len(build.calls) == 1
    [item] = report.blocked
    assert item.reason == BlockReason.PARENT_NOT_FOUND and item.artifact.coordinates == WAGON_EXT
    assert any("com.company:base-parent:4.0.0 declares the build extension " + WAGON_EXT in f and "drop the extension" in f
               for f in report.manual_followups)


def test_a_project_declared_extension_is_not_an_abort(cfg, ws, monkeypatch):
    # the same error for an extension the chain does NOT declare: the loop handles it (the note tells the LLM to remove it)
    from sbmig.build import parent_chain
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: parent_chain.ParentChainFacts())
    err = BuildError(kind=ErrorKind.POM_INVALID, file="pom.xml", message=EXT_MSG, raw=EXT_MSG,
                     artifact=GAV(group_id="org.apache.maven.wagon", artifact_id="wagon-webdav", version="1.0-beta-2"))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(err) if len(w.read("pom.xml")) > 0 and not (w.root / "fixed").exists() else ok_build())
    asked = []

    def fix(w, c, ctx):
        asked.append(c.errors[0].artifact.key)
        (w.root / "fixed").write_text("x")
        return write_script({"pom.xml": w.read("pom.xml") + "\n"}, analysis="removed the extension")
    report = make_orch(cfg, ws, build=build, fixer=FakeFixer(script_fn=fix)).run()
    assert "aborted:parent-not-found" not in report.flags and asked == ["org.apache.maven.wagon:wagon-webdav"]


def test_another_version_of_the_chains_extension_is_the_projects_own_case(cfg, ws, monkeypatch):
    # Maven merges <extensions> by g:a (child wins): the project pins wagon-webdav 2.0 while the chain declares 1.0-beta-2 → the
    # failing declaration is the project's → the LLM path, not an abort (review 2026-11-05: g:a-only matching aborted it)
    _chain_with_wagon(monkeypatch)
    msg = EXT_MSG.replace("1.0-beta-2", "2.0")
    err = BuildError(kind=ErrorKind.POM_INVALID, file="pom.xml", message=msg, raw=msg,
                     artifact=GAV(group_id="org.apache.maven.wagon", artifact_id="wagon-webdav", version="2.0"))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(err) if not (w.root / "fixed").exists() else ok_build())
    asked = []

    def fix(w, c, ctx):
        asked.append(c.errors[0].artifact.coordinates)
        (w.root / "fixed").write_text("x")
        return write_script({"pom.xml": w.read("pom.xml") + "\n"}, analysis="removed the project's extension")
    report = make_orch(cfg, ws, build=build, fixer=FakeFixer(script_fn=fix)).run()
    assert "aborted:parent-not-found" not in report.flags and asked == ["org.apache.maven.wagon:wagon-webdav:2.0"]


def test_extension_text_with_the_parents_location_is_never_a_parent_abort(cfg, ws, monkeypatch):
    # the chain does not list the extension (unreadable chain, or another one): the OpenRewrite failure text still ends with Maven's
    # location tail naming the parent pom — the parent regex must not turn that into "configured parent not resolvable"
    from sbmig.build import parent_chain
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: parent_chain.ParentChainFacts())
    parent = cfg.artifacts.parent
    text = (f"[ERROR] {EXT_MSG} @ {parent.key}:{parent.version}, /m2/{parent.artifact_id}-{parent.version}.pom, line 40, column 7")
    rw = FakeRewriteRunner(ws, ok=False, error=text)
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert "aborted:parent-not-found" not in report.flags and not report.blocked


def test_extension_failure_inside_rewrite_aborts(cfg, ws, monkeypatch):
    _chain_with_wagon(monkeypatch)
    rw = FakeRewriteRunner(ws, ok=False, error="[ERROR] " + EXT_MSG + " @ ")
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM]
    assert report.blocked[0].artifact.coordinates == WAGON_EXT


def test_mvn_missing_aborts(cfg, ws, monkeypatch):
    import sbmig.build as b
    monkeypatch.setattr(b, "maven_available", lambda *a, **k: False)
    orch = make_orch(cfg, ws)
    orch._build_runner = None                                         # use the real preflight check
    report = orch.run()
    assert "mvn-not-found" in report.flags and "aborted" in report.flags and report.phases == []
    assert run_exit_code(report) == 3


def test_run_exit_codes():
    from sbmig.core.models import BlockedItem, utcnow
    r = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(), success=True)
    assert run_exit_code(r) == 0
    r.blocked = [BlockedItem(id="b", reason=BlockReason.ARTIFACT_NOT_FOUND, phase=Phase.SPRING, cluster_id="c")]
    assert run_exit_code(r) == 2
    r.blocked, r.success = [], False
    assert run_exit_code(r) == 2
    r.flags = ["aborted"]
    assert run_exit_code(r) == 3
    r.flags = ["aborted:parent-not-found"]
    assert run_exit_code(r) == 3


@pytest.mark.parametrize("flags,blocked,success,code", [([], False, True, 0), ([], True, True, 2),
                                                         (["aborted:parent-not-found"], True, False, 3)])
def test_cli_run_exit_code(tmp_path, monkeypatch, repo_root, flags, blocked, success, code):
    from typer.testing import CliRunner

    import sbmig.loop
    from sbmig.cli import app
    from sbmig.core.models import BlockedItem, utcnow

    class Orch:
        def __init__(self, cfg, ws, **kw):
            pass

        def run(self, stop_after=None, dry_run=False):
            b = [BlockedItem(id="b", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c",
                             suggestion="upgrade it")] if blocked else []
            return RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(),
                             success=success, flags=list(flags), blocked=b)

    monkeypatch.setattr(sbmig.loop, "Orchestrator", Orch)
    src = tmp_path / "proj"
    src.mkdir()
    (src / "pom.xml").write_text("<project/>")
    res = CliRunner().invoke(app, ["run", str(src), "--config", str(repo_root / "config/artifacts.local-e2e.yml"),
                                   "--out", str(tmp_path / "out"), "--run-id", f"x{code}"])
    assert res.exit_code == code, res.output
    if blocked:
        assert "blocked:" in res.output and "external_javax_api 1" in res.output


# ------------------------------------------------------------------------------------------------ G1 wiring
def test_msa_targets_build_from_the_aggregator_with_pl_am(cfg, ws):
    from sbmig.core.models import PomInfo

    prof = make_profile(CaseType.MSA)
    prof.poms = [PomInfo(path="pom.xml", artifact_id="root", packaging="pom", modules=["svc-a", "./svc-b", "common"],
                         is_root=True, is_aggregator=True),
                 PomInfo(path="svc-a/pom.xml", artifact_id="svc-a"), PomInfo(path="svc-b/pom.xml", artifact_id="svc-b")]
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build())
    rw = FakeRewriteRunner(ws)
    report = make_orch(cfg, ws, plan=v2_msa_plan(), build=build, rewrite=rw, profile=prof).run()
    assert report.success
    assert {c["module"] for c in build.calls} == {None}
    assert [c["extra_args"] for c in build.calls if c["label"].startswith("verify")] == [
        ["-pl", "svc-a", "-am"], ["-pl", "svc-b", "-am"]]
    assert [r["module"] for r in rw.runs] == ["svc-a", "svc-b"]         # OpenRewrite stays per target


def test_oscillation_limit_is_configurable(cfg, ws, profile):
    """v2.1z-f: `loop.oscillation_limit` (default 2) — a higher value lets the ladder keep trying through more A↔B
    toggles before the involved cases are BLOCKED oscillation; a value above the rounds never blocks for it."""
    def build_fn(w, call):
        msg = "cannot find symbol A" if "A" in w.read(FOO) else "cannot find symbol B"
        return bad_build(compile_error(FOO, msg, sig=f"sig-{msg[-1]}"))

    n = {"i": 0}

    def fix(w, c, ctx):
        n["i"] += 1
        return write_fix(FOO, f"class Foo {{ {'B' if 'A' in w.read(FOO) else 'A'} {n['i']} }}\n")

    ws.write(FOO, "class Foo { A }\n")
    ws.commit("A")
    cfg.loop.oscillation_limit = 4
    cfg.script.max_llm_tries_per_case = 20
    loop = make_loop(cfg, ws, profile, build=FakeBuildRunner(ws, fn=build_fn), fixer=FakeFixer(fn=fix))
    pr = loop.run(Phase.VERIFY, "package", 10)
    assert pr.iterations == 6 and n["i"] == 5                      # toggles at builds 3..6 → the 4th blocks
    assert [b.reason for b in pr.blocked] == [BlockReason.OSCILLATION]
    assert sum(e["event"] == "oscillation" for e in journal_events(ws)) == 4
    assert any("4 time(s)" in note and "loop.oscillation_limit" in note for b in pr.blocked for note in b.notes) or \
        any("loop.oscillation_limit" in (b.suggestion or "") for b in pr.blocked)
