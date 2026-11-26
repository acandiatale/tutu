"""INTERFACES "v2.1z-ck": what a REAL reduced run (the orchestrator with the fakes) records, read by
`conversion_complete`. The rule tests in tests/report build reports by hand; these drive the engine."""
from __future__ import annotations

import json

import pytest

from sbmig.core.models import MigrationPlan, StepKind
from sbmig.report.result_line import conversion_complete, report_exit_code, report_result_line

from .fakes import (
    FakeBuildRunner,
    FakeFixer,
    FakeRewriteRunner,
    bad_build,
    compile_error,
    make_profile,
    ok_build,
)
from .test_framework_no_loop import NoLLM, use
from .test_http_blocker_flow import DEP_LOG, blocked_build
from .test_orchestrator import FOO, fix_all, make_orch
from .test_orchestrator_framework import reduced_plan
from .test_orchestrator_pom_charset import failing


def _failing_build(ws, call):
    return bad_build(compile_error(FOO))


def _passing_build(ws, call):
    return ok_build()


def _run(cfg, ws, *, build_fn, plan=None, rewrite=None, clock=None, minutes=None):
    c = cfg.model_copy(deep=True)
    c.loop.engine = "graph"
    if minutes is not None:
        c.loop.max_total_minutes = minutes
    prof = make_profile()
    prof.framework = use(fix_loop=False)
    build = FakeBuildRunner(ws, fn=build_fn)
    kw = {"clock": clock} if clock is not None else {}
    orch = make_orch(c, ws, plan=plan or reduced_plan(jsp=False), profile=prof, build=build,
                     rewrite=rewrite or FakeRewriteRunner(ws), fixer=FakeFixer(script_fn=fix_all), llm=NoLLM(), **kw)
    return orch.run(), build


def _both(report) -> tuple[bool, bool]:
    """The rule on the object and on the dict the web reads from report.json."""
    return conversion_complete(report), conversion_complete(json.loads(report.model_dump_json()))


# ---------------------------------------------------------------------------------------------- a
def test_converted_and_the_build_fails(cfg, ws):
    report, build = _run(cfg, ws, build_fn=_failing_build)
    assert [c["label"] for c in build.calls] == ["verify"]
    assert _both(report) == (True, True)
    assert not report.success and report_exit_code(report) == 2
    line = report_result_line(report)
    assert " exit=2 build=FAIL " in line and line.endswith(" scope=jdk-jakarta")


# ---------------------------------------------------------------------------------------------- b
@pytest.mark.parametrize("build_fn", [_passing_build, _failing_build], ids=["build-passed", "build-failed"])
def test_failed_conversion(cfg, ws, build_fn):
    report, _ = _run(cfg, ws, build_fn=build_fn, rewrite=failing(ws))
    assert "rewrite-failed" in report.flags
    assert _both(report) == (False, False)


# ---------------------------------------------------------------------------------------------- c
def _two_service_plan() -> MigrationPlan:
    """A reduced plan with one rewrite step per service, the shape the planner gives an MSA tree."""
    plan = reduced_plan(jsp=False)
    rewrite = plan.steps[0]
    steps = [rewrite.model_copy(update={"id": f"transform.rewrite@{svc}", "target": svc}) for svc in ("svc-a", "svc-b")]
    plan.steps = steps + [s for s in plan.steps if s.kind != StepKind.OPENREWRITE]
    return plan


def test_time_budget_skips_the_second_service(cfg, ws):
    t = {"now": 0.0}

    def slow(w, refs):
        t["now"] += 120                          # the first conversion uses up the one-minute budget
        return []

    rw = FakeRewriteRunner(ws, effect=slow)
    report, build = _run(cfg, ws, build_fn=_failing_build, plan=_two_service_plan(), rewrite=rw,
                         clock=lambda: t["now"], minutes=1)
    assert len(rw.runs) == 1 and "time-budget-exhausted" in report.flags
    assert any("transform.rewrite@svc-b skipped: run time budget exhausted" in n for p in report.phases for n in p.notes)
    assert [x for p in report.phases for x in p.rewrite_results if not x.dry_run] and report.final_build is not None
    assert build.calls and build.calls[-1]["label"] == "verify"          # the build still ran
    assert _both(report) == (False, False)


def test_both_services_converted(cfg, ws):
    """Guard for the test above: the same plan without the budget is complete."""
    report, _ = _run(cfg, ws, build_fn=_failing_build, plan=_two_service_plan())
    assert len([x for p in report.phases for x in p.rewrite_results if not x.dry_run]) == 2
    assert _both(report) == (True, True)


# ---------------------------------------------------------------------------------------------- d
def test_stop_request_after_the_conversion(cfg, ws):
    rw = FakeRewriteRunner(ws)
    ws.stop_requested = lambda: bool(rw.runs)          # type: ignore[method-assign]
    report, _ = _run(cfg, ws, build_fn=_failing_build, rewrite=rw)
    assert rw.runs and "aborted" in report.flags and report_exit_code(report) == 3
    assert _both(report) == (False, False)


def test_aborted_after_the_conversion_with_a_build_result(cfg, ws):
    report, _ = _run(cfg, ws, build_fn=lambda w, c: blocked_build(DEP_LOG))
    assert "aborted:http-blocked" in report.flags and report.final_build is not None
    assert [x for p in report.phases for x in p.rewrite_results if x.ok]
    assert _both(report) == (False, False)


# ---------------------------------------------------------------------------------------------- e
def test_converted_and_the_build_passes(cfg, ws):
    report, _ = _run(cfg, ws, build_fn=_passing_build)
    assert report.success and report_exit_code(report) == 0
    assert _both(report) == (True, True)



# ---------------------------------------------------------------------------------------------- files OpenRewrite could not read
PARSE = ["src/main/java/com/x/vendor/Legacy.java", "src/main/java/com/x/Old.java"]
PARSE_LINE = ("OpenRewrite could not read 2 source file(s) (src/main/java/com/x/vendor/Legacy.java, "
              "src/main/java/com/x/Old.java): the JDK 21 / Jakarta recipes skipped them, so they are not converted — "
              "check and convert them by hand")


class ParseProblemRewrite(FakeRewriteRunner):
    """Every real rewrite run reports the same files it could not parse (like one service after another)."""

    def __init__(self, ws, files=PARSE):
        super().__init__(ws)
        self.files = list(files)

    def run(self, recipes, **kw):
        res = super().run(recipes, **kw)
        if not kw.get("dry_run"):
            res.parse_problems = list(self.files)
        return res


def test_parse_problems_in_a_reduced_run(cfg, ws):
    from sbmig.report import render_markdown
    from sbmig.report.markdown import flag_help
    report, _ = _run(cfg, ws, build_fn=_failing_build, rewrite=ParseProblemRewrite(ws))
    assert "rewrite-parse-problems" in report.flags
    assert report.manual_followups.count(PARSE_LINE) == 1
    # decision (v2.1z-ck): such files are follow-ups, they do not undo the label
    assert _both(report) == (True, True)
    note = next(n for p in report.phases for n in p.notes if "could not parse 2 source file(s)" in n)
    assert "fix scripts" not in note and note.endswith("the build shows what follows")
    md = render_markdown(report, "ko")
    check = md[md.index("## 확인해 주세요"):md.index("## ", md.index("## 확인해 주세요") + 3)]
    assert ("**변환되지 않은 파일** — OpenRewrite가 읽지 못한 소스 파일이 2개 있습니다: "
            "src/main/java/com/x/vendor/Legacy.java, src/main/java/com/x/Old.java. 이 파일들에는 JDK 21, Jakarta 변환이 "
            "적용되지 않았습니다. 직접 확인해 변환해 주세요.") in check
    # the flag line no longer claims a fix step
    for lang in ("en", "ko"):
        h = flag_help("rewrite-parse-problems", lang)
        assert "fix loop" not in h and "수정 단계" not in h
    assert "빌드 오류 수정 단계" not in md


def test_parse_problems_in_two_services_are_named_once(cfg, ws):
    report, _ = _run(cfg, ws, build_fn=_failing_build, plan=_two_service_plan(), rewrite=ParseProblemRewrite(ws))
    lines = [f for f in report.manual_followups if f.startswith("OpenRewrite could not read")]
    assert lines == [PARSE_LINE]


def test_parse_problems_follow_up_caps_the_list(cfg, ws):
    files = [f"src/main/java/com/x/F{i}.java" for i in range(7)]
    report, _ = _run(cfg, ws, build_fn=_failing_build, rewrite=ParseProblemRewrite(ws, files))
    [line] = [f for f in report.manual_followups if f.startswith("OpenRewrite could not read")]
    assert line.startswith("OpenRewrite could not read 7 source file(s) (src/main/java/com/x/F0.java, ")
    assert "F4.java, … (+2)): " in line and "F5.java" not in line


def test_parse_problems_in_a_full_run_add_no_follow_up(cfg, ws):
    orch = make_orch(cfg, ws, rewrite=ParseProblemRewrite(ws))
    report = orch.run()
    assert report.success and "rewrite-parse-problems" in report.flags
    assert not any(f.startswith("OpenRewrite could not read") for f in report.manual_followups)
    assert not any(f.startswith("OpenRewrite could not read") for f in report.handover.from_here)
    note = next(n for p in report.phases for n in p.notes if "could not parse 2 source file(s)" in n)
    assert note.endswith("the fix scripts cover what the build finds")


# ---------------------------------------------------------------------------------------------- only the summary line
class SummaryOnlyRewrite(ParseProblemRewrite):
    """The plugin printed only its summary line: the runner's placeholder, and one rewrite log per step."""

    def __init__(self, ws, files=None):
        from sbmig.rewrite.runner import PARSE_PROBLEMS_UNKNOWN
        super().__init__(ws, files if files is not None else [PARSE_PROBLEMS_UNKNOWN])

    def run(self, recipes, **kw):
        res = super().run(recipes, **kw)
        res.log_path = str(self.ws.run_dir / "logs" / f"rewrite-{kw.get('label') or 'x'}.log")
        return res


def _unknown_line(step: str) -> str:
    return ("OpenRewrite could not read some source files (their names are in the rewrite log "
            f"`logs/rewrite-{step}.log`): the JDK 21 / Jakarta recipes skipped them, so they are not converted — "
            "check and convert them by hand")


def _read_lines(report) -> list[str]:
    return [f for f in report.manual_followups if f.startswith("OpenRewrite could not read")]


def test_summary_line_only(cfg, ws):
    from sbmig.report import render_markdown
    report, _ = _run(cfg, ws, build_fn=_failing_build, rewrite=SummaryOnlyRewrite(ws))
    assert _read_lines(report) == [_unknown_line("transform.rewrite")]
    assert _both(report) == (True, True)
    md = render_markdown(report, "ko")
    [item] = [x for x in md.splitlines() if "변환되지 않은 파일" in x]
    assert item.endswith("**변환되지 않은 파일** — OpenRewrite가 읽지 못한 소스 파일이 있습니다. 파일 이름은 rewrite 로그 "
                         "`logs/rewrite-transform.rewrite.log`에 있습니다. 이 파일들에는 JDK 21, Jakarta 변환이 적용되지 "
                         "않았습니다. 직접 확인해 변환해 주세요.")
    # the phase note keeps the runner's placeholder; it reaches only the collapsed diagnostic record
    readable = md[:md.index("<summary>진단용 기록</summary>")]
    assert "unknown files" not in readable and "see the rewrite log" not in readable
    assert not any(ch.isdigit() for ch in item.split("—", 1)[1].replace("21", "").split("`")[0])


def test_summary_line_only_in_two_services(cfg, ws):
    report, _ = _run(cfg, ws, build_fn=_failing_build, plan=_two_service_plan(), rewrite=SummaryOnlyRewrite(ws))
    assert _read_lines(report) == [_unknown_line("transform.rewrite@svc-a"), _unknown_line("transform.rewrite@svc-b")]


def test_summary_placeholder_beside_real_names(cfg, ws):
    from sbmig.rewrite.runner import PARSE_PROBLEMS_UNKNOWN
    rw = SummaryOnlyRewrite(ws, ["src/main/java/com/x/Old.java", PARSE_PROBLEMS_UNKNOWN])
    report, _ = _run(cfg, ws, build_fn=_failing_build, rewrite=rw)
    assert _read_lines(report) == [
        ("OpenRewrite could not read 1 source file(s) (src/main/java/com/x/Old.java): the JDK 21 / Jakarta recipes "
         "skipped them, so they are not converted — check and convert them by hand"),
        _unknown_line("transform.rewrite")]
