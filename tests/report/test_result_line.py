"""Round 10: the one-line result (`SBMIG-RESULT …`) and the attention codes shared by `sbmig run` and the fleet."""
from __future__ import annotations

from sbmig.core.models import (GAV, BlockedItem, BlockReason, BuildError, CaseType, ErrorEntry, ErrorKind,
                               FleetProjectResult, LLMUsage, Phase, RunReport, utcnow)
from sbmig.report.markdown import flag_help
from sbmig.report.result_line import (attention_codes, counts_open_resolved, entry_counts, infra_kind,
                                      report_exit_code, report_result_line, result_attention, result_line)


def _report(**kw) -> RunReport:
    r = RunReport(run_id=kw.pop("run_id", "run-1"), project_name="p", case_type=CaseType.SPRING_BOOT,
                  started_at=utcnow(), success=kw.pop("success", True), llm_usage=LLMUsage(calls=kw.pop("llm", 0)))
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _blocked(reason: BlockReason) -> BlockedItem:
    return BlockedItem(id="b", reason=reason, phase=Phase.SPRING, cluster_id="c",
                       artifact=GAV(group_id="com.co", artifact_id="legacy"))


def test_green_run_line():
    line = report_result_line(_report(llm=3, flags=["tests-not-compiled"]), 0)
    assert line == ("SBMIG-RESULT run=run-1 exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=0 llm=3 "
                    "attn=tests-not-compiled")
    assert report_result_line(_report(), 0).endswith("attn=-")
    assert report_result_line(_report()) == report_result_line(_report(), 0)       # exit code from the report


def test_incomplete_run_codes_are_ordered_and_ascii():
    r = _report(success=False, llm=2, run_id="주문-run",
                flags=["infra:repository", "restructure-failed", "tests-not-compiled"],
                blocked=[_blocked(BlockReason.EXTERNAL_JAVAX_API), _blocked(BlockReason.EXTERNAL_JAVAX_API)],
                unresolved=[BuildError(kind=ErrorKind.COMPILE, message="x")] * 3)
    line = report_result_line(r, 2)
    assert line.isascii() and " run=?-run " in line
    assert line.endswith("build=FAIL unresolved=3 blocked=2 open=0 resolved=0 llm=2 attn=infra:repository,"
                         "blocked:external_javax_api,unresolved:3,restructure-failed,tests-not-compiled")


def test_open_and_resolved_count_the_error_list():
    es = [ErrorEntry(id=str(i), status=st) for i, st in
          enumerate(["open", "unresolved", "resolved", "resolved", "resolved", "pom-verify", "note", "blocked"])]
    line = report_result_line(_report(success=False, error_entries=es), 2)
    assert " blocked=0 open=2 resolved=3 llm=0 " in line
    assert entry_counts([{"status": "open"}, {"status": "resolved"}]) == (1, 1)
    assert counts_open_resolved({"unresolved": 4, "open": 1, "resolved": 7, "blocked": 2}) == (5, 7)
    assert counts_open_resolved(None) == (0, 0)
    assert result_line("r", 2, False, open_errors=5, resolved_errors=7).endswith(
        "unresolved=0 blocked=0 open=5 resolved=7 llm=0 attn=-")


def test_exit_code_mapping():
    assert report_exit_code(_report()) == 0
    assert report_exit_code(_report(blocked=[_blocked(BlockReason.EXTERNAL_JAVAX_API)])) == 2
    assert report_exit_code(_report(success=False)) == 2
    assert report_exit_code(_report(flags=["aborted:parent-not-found"])) == 3
    assert report_exit_code(_report(success=False, flags=["mvn-not-found"])) == 3
    from sbmig.cli import run_exit_code
    assert run_exit_code(_report(flags=["aborted"])) == 3


def test_infra_kinds_and_green_runs():
    assert infra_kind(["x", "infra:llm", "infra:maven"]) == "llm"
    assert infra_kind(["llm-unavailable"]) == "llm" and infra_kind(["mvn-not-found"]) == "maven"
    assert infra_kind(["jdk-not-21"]) == "jdk" and infra_kind(["llm-config-error"]) == "llm"
    assert infra_kind(["aborted"]) is None
    # a green run keeps its success even though the LLM was unavailable for a while
    assert attention_codes(flags=["llm-unavailable"], exit_code=0) == []
    assert attention_codes(flags=["mvn-not-found", "aborted"], exit_code=3) == ["infra:maven"]
    assert attention_codes(flags=["aborted:parent-not-found"], exit_code=3) == ["aborted:parent-not-found"]
    assert attention_codes(flags=["aborted"], exit_code=3) == ["aborted"]
    assert attention_codes(exit_code=2) == ["incomplete"]


def test_fleet_result_attention():
    pending_infra = FleetProjectResult(name="a", input="/a", status="pending", error="infra:llm", exit_code=2,
                                       flags=["infra:llm"])
    assert result_attention(pending_infra) == ["infra:llm"]
    assert result_attention(FleetProjectResult(name="b", input="/b", status="pending")) == []
    assert result_attention(FleetProjectResult(name="b", input="/b", status="running")) == []
    gradle = FleetProjectResult(name="c", input="/c", status="skipped", exit_code=3, flags=["aborted"],
                                error="unsupported build tool: gradle (only Maven projects are migrated)")
    assert result_attention(gradle) == ["skipped:gradle"]
    missing = FleetProjectResult(name="d", input="/d", status="skipped", error="input not found: /d")
    assert result_attention(missing) == ["skipped:input"]
    crash = FleetProjectResult(name="e", input="/e", status="aborted", exit_code=3, error="crash: RuntimeError: x")
    assert result_attention(crash) == ["crash", "aborted"]
    blocked = FleetProjectResult(name="f", input="/f", status="incomplete", exit_code=2, blocked=1, unresolved=2,
                                 blocked_reasons=["artifact_not_found:com.co:x"])
    assert result_attention(blocked) == ["blocked:artifact_not_found", "unresolved:2"]
    ok = FleetProjectResult(name="g", input="/g", status="success", exit_code=0, flags=["llm-unavailable"])
    assert result_attention(ok) == []


def test_result_line_without_a_report():
    assert result_line("r", None, False, attention=["crash"]) == (
        "SBMIG-RESULT run=r exit=- build=FAIL unresolved=0 blocked=0 llm=0 attn=crash")


def test_flag_help_for_infra_flags():
    assert "infrastructure" in flag_help("infra:llm") and "infrastructure" in flag_help("infra:repository")
    assert "JDK 21" in flag_help("jdk-not-21") and "llm-probe" in flag_help("llm-config-error")
    assert "인프라" in flag_help("infra:disk", "ko")
