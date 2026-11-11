"""v2.1z-ah: Maven's HTTP repository blocker aborts the run once, as an environment problem (docs/INTERFACES.md "v2.1z-ah").

Company run 2026-11-11: the blocker refused the company parent and the run aborted as `parent-not-found` (and, since
v2.1z-ag, aligned the parent and ran OpenRewrite again — failing the same way). Now: no alignment, no retry, no pre-try,
no LLM; flag `aborted:http-blocked`, attention `infra:http-blocked`, one Handover line saying what to change in the
Maven settings."""
from __future__ import annotations

import pytest

from sbmig.build.env_errors import HTTP_BLOCKED_PREFIX
from sbmig.build.log_parser import parse_maven_log
from sbmig.cli import run_exit_code
from sbmig.core.models import Phase, RewriteResult
from sbmig.report.result_line import report_result_line

from ..build.test_http_blocker import DEP_LOG, PARENT_LOG
from .fakes import FakeBuildRunner, FakeFixer, bad_build, journal_events, make_profile, ok_build
from .test_asis_parent import OLD, pom, set_root
from .test_blocked_flow import make_loop
from .test_orchestrator import make_orch

NO_LLM = {"fn": lambda *a, **k: pytest.fail("the LLM must not be asked"),
          "script_fn": lambda *a, **k: pytest.fail("the LLM must not be asked")}


class BlockedRewrite:
    def __init__(self, log: str):
        self.log, self.runs = log, 0

    def run(self, recipes, **kw):
        self.runs += 1
        return RewriteResult(ok=False, recipes=[r.id for r in recipes], error=self.log)


def blocked_build(log: str = PARENT_LOG):
    return bad_build(*parse_maven_log(log), goal="clean package")


@pytest.fixture()
def parent(cfg):
    return cfg.artifacts.parent


def assert_http_abort(report, ws, n_repos: int):
    assert run_exit_code(report) == 3
    assert "aborted:http-blocked" in report.flags and "infra:http-blocked" in report.flags
    assert "aborted:parent-not-found" not in report.flags and "infra:repository" not in report.flags
    assert "asis-parent-aligned" not in report.flags and not report.blocked
    [ev] = [e for e in journal_events(ws) if e["event"] == "maven_http_blocked"]
    assert len(ev["repositories"]) == n_repos and "s3cret" not in str(ev)
    lines = [f for f in report.manual_followups if f.startswith(HTTP_BLOCKED_PREFIX)]
    assert len(lines) == 1 and not any(f.startswith("run aborted") for f in report.manual_followups)
    return lines[0]


def test_rewrite_failure_with_the_blocker_aborts_without_alignment_or_retry(cfg, ws, parent):
    # the as-is version of the company parent: v2.1z-ag would align it and run OpenRewrite again
    set_root(ws, pom(parent.key, OLD))
    rw = BlockedRewrite(PARENT_LOG.replace("com.example:base-parent", parent.key))
    build = FakeBuildRunner(ws, fn=lambda w, c: pytest.fail("no build after the abort"))
    report = make_orch(cfg, ws, rewrite=rw, build=build, fixer=FakeFixer(**NO_LLM)).run()
    assert rw.runs == 1 and f"<version>{OLD}</version>" in ws.read("pom.xml")
    line = assert_http_abort(report, ws, 2)
    assert "`company-nexus`" in line and "`company-snap`" in line
    assert report_result_line(report).split(" attn=")[1].split()[0] == "infra:http-blocked,aborted:http-blocked"
    # the Handover's first "from here on" line
    assert report.handover is not None and report.handover.from_here[0] == line


def test_verify_failure_with_the_blocker_aborts_before_the_fix_loop(cfg, ws):
    build = FakeBuildRunner(ws, fn=lambda w, c: blocked_build(DEP_LOG))
    report = make_orch(cfg, ws, build=build, fixer=FakeFixer(**NO_LLM)).run()
    assert len(build.calls) == 1                                    # the final build only: no loop, no rebuild
    assert_http_abort(report, ws, 1)
    attn = report_result_line(report).split(" attn=")[1].split()[0].split(",")
    assert attn[:2] == ["infra:http-blocked", "aborted:http-blocked"]
    assert not any(c.startswith(("blocked:", "infra:repository", "aborted:parent")) for c in attn)


def test_build_loop_failure_with_the_blocker_aborts_without_pre_try_or_llm(cfg, ws, parent):
    set_root(ws, pom(parent.key, OLD))                              # sbmig-parent would align this
    log = PARENT_LOG.replace("com.example:base-parent", parent.key).replace("1.0.0-SNAPSHOT", OLD)
    calls = []

    def fn(w, c):
        calls.append(c)
        return ok_build() if len(calls) == 1 else blocked_build(log)

    loop = make_loop(cfg, ws, make_profile(), build=FakeBuildRunner(ws, fn=fn), fixer=FakeFixer(**NO_LLM))
    pr = loop.run(Phase.VERIFY, "package", 4, first_build=blocked_build(log))
    assert loop.aborted and loop.aborted.startswith(HTTP_BLOCKED_PREFIX) and not pr.attempts and not pr.blocked
    assert loop.flags == ["aborted:http-blocked"] and calls == []
    assert f"<version>{OLD}</version>" in ws.read("pom.xml")
    assert [e["event"] for e in journal_events(ws)].count("maven_http_blocked") == 1


def test_preflight_warns_without_aborting(cfg, ws, monkeypatch, caplog):
    from sbmig.build import env_errors
    from sbmig.build.env_errors import ActiveBlocker

    monkeypatch.setattr(env_errors, "active_http_blockers", lambda cfg, root: [
        ActiveBlocker("company-nexus", "http://nexus.invalid/r/", "settings x", "maven-default-http-blocker", "/m/conf/settings.xml")])
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build()))
    orch._check_http_blocker()
    [ev] = [e for e in journal_events(ws) if e["event"] == "maven_http_blocker_active"]
    assert ev["repositories"][0]["id"] == "company-nexus"
    assert any("HTTP blocker covers company-nexus" in m for m in caplog.messages)
