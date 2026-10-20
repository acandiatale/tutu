"""Round 9 / catalog X8: the spring rewrite left literal `4.0.x` versions (its resolver could not download Maven
metadata) → revert + wait + one re-run; still there → flag, note, follow-up, and the build loop still runs. KB recipe
candidates are not credited a failure the phase took on dependency resolution."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sbmig.core.models import BuildError, ErrorKind, Phase, RewriteResult
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, FakeKB, bad_build, make_project
from .test_orchestrator import files_build, make_orch

SPRING = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
KB_RECIPE = "com.company.kb.Cand"
POM = ("<project><parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>"
       "<version>{v}</version></parent><artifactId>demo</artifactId></project>\n")


@pytest.fixture()
def ws(tmp_path):
    w = Workspace.from_input(make_project(tmp_path), tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


class PomRewriter:
    """Writes the parent version from `versions` (one per spring call) and appends a line to rewritten.txt."""

    def __init__(self, ws, versions):
        self.ws = ws
        self.versions = list(versions)
        self.spring_calls = 0
        self.runs: list[dict] = []

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", extra_args=()):
        ids = [r.id for r in recipes]
        self.runs.append({"label": label, "ids": ids, "dirty": self.ws.is_dirty()})
        if dry_run or SPRING not in ids:
            return RewriteResult(ok=True, recipes=ids, dry_run=dry_run)
        v = self.versions[min(self.spring_calls, len(self.versions) - 1)]
        self.spring_calls += 1
        self.ws.write("pom.xml", POM.format(v=v))
        prev = self.ws.read("rewritten.txt") if self.ws.exists("rewritten.txt") else ""
        self.ws.write("rewritten.txt", prev + "spring\n")
        return RewriteResult(ok=True, recipes=ids, changed_files=["pom.xml", "rewritten.txt"],
                             recipes_applied={KB_RECIPE: ["pom.xml"]}, log_path=f"/tmp/rw-{self.spring_calls}.log")


class CreditKB(FakeKB):
    def list_recipes(self, status=None):
        return [SimpleNamespace(id=KB_RECIPE)]


def resolution_build(ws, call):
    """Build fails on dependency resolution while the pom carries a literal `x.y.x` version."""
    if "4.0.x" in ws.read("pom.xml"):
        return bad_build(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, module="demo",
                                    message="Could not find artifact org.springframework.boot:spring-boot-starter-"
                                            "parent:pom:4.0.x in central",
                                    signature="dependency_resolution|org.springframework.boot:spring-boot-starter-"
                                              "parent:4.0.x"))
    return files_build(ws, call)


def _events(ws, name):
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text().splitlines() if x.strip())
            if e["event"] == name]


def _git_log(ws):
    return ws._git("log", "--format=%s").stdout.splitlines()  # noqa: SLF001


def test_literal_versions_trigger_one_rerun_on_a_clean_tree(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.openrewrite.download_retry_wait_s = 5
    rw = PomRewriter(ws, ["4.0.x", "4.0.1"])
    kb = CreditKB()
    orch = make_orch(cfg, ws, rewrite=rw, kb=kb)
    waits: list = []
    orch._sleep = waits.append
    report = orch.run()
    assert rw.spring_calls == 2 and waits == [5]
    spring_runs = [r for r in rw.runs if SPRING in r["ids"]]
    assert [r["dirty"] for r in spring_runs] == [False, False]     # reverted between the two runs
    assert ws.read("rewritten.txt") == "spring\n"                    # no duplicate changes
    assert "<version>4.0.1</version>" in ws.read("pom.xml")
    (ev,) = _events(ws, "rewrite_unresolved_versions")
    assert ev["retry"] is True and ev["poms"] == {"pom.xml": ["4.0.x"]} and ev["step"] == "spring.rewrite"
    assert "rewrite-unresolved-versions" not in report.flags
    spring = next(p for p in report.phases if p.phase == Phase.SPRING)
    assert len(spring.rewrite_results) == 1 and spring.rewrite_results[0].log_path == "/tmp/rw-2.log"
    assert _git_log(ws).count("rewrite: spring.rewrite") == 1
    assert report.success and kb.credits == [(KB_RECIPE, True)]


def test_literal_versions_twice_are_reported_and_the_build_still_runs(cfg, ws):
    rw = PomRewriter(ws, ["4.0.x"])
    kb = CreditKB()
    orch = make_orch(cfg, ws, rewrite=rw, kb=kb, build=FakeBuildRunner(ws, fn=resolution_build))
    orch._sleep = lambda s: None
    report = orch.run()
    assert rw.spring_calls == 2
    assert [e["retry"] for e in _events(ws, "rewrite_unresolved_versions")] == [True, False]
    assert "rewrite-unresolved-versions" in report.flags
    fu = [f for f in report.manual_followups if "could not download Maven metadata" in f]
    assert fu == ["the recipe run could not download Maven metadata (proxy / rate limit): literal versions `4.0.x` "
                  "remain in pom.xml — re-run sbmig later or set the versions by hand"]
    spring = next(p for p in report.phases if p.phase == Phase.SPRING)
    assert any("rewrite-unresolved-versions" in n for n in spring.notes)
    assert spring.iterations >= 1 and not spring.build_ok             # the build loop ran
    assert report.final_build is not None and report.phases[-1].phase == Phase.VERIFY   # invariant 1
    assert kb.credits == []                                           # resolution failure → not credited
    (skip,) = _events(ws, "kb_credit_skipped")
    assert skip["phase"] == "spring" and skip["recipes"] == [KB_RECIPE]
    assert skip["reason"] == "rewrite-unresolved-versions"


def test_retry_off_reports_without_rerun(cfg, ws):
    cfg = cfg.model_copy(deep=True)
    cfg.openrewrite.download_retry = 0
    rw = PomRewriter(ws, ["2025.1.x"])
    orch = make_orch(cfg, ws, rewrite=rw)
    waits: list = []
    orch._sleep = waits.append
    report = orch.run()
    assert rw.spring_calls == 1 and waits == []
    assert [e["retry"] for e in _events(ws, "rewrite_unresolved_versions")] == [False]
    assert "rewrite-unresolved-versions" in report.flags


def test_dirty_tree_is_not_reverted(cfg, ws):
    """Uncommitted work before the rewrite would be lost by a reset → no re-run, report only."""
    rw = PomRewriter(ws, ["4.0.x", "4.0.1"])
    orch = make_orch(cfg, ws, rewrite=rw)
    waits: list = []
    orch._sleep = waits.append
    real_pre = orch._maybe_pre_handlers

    def pre_then_dirty(step, idx):
        real_pre(step, idx)
        if step.id == "spring.rewrite":
            ws.write("local.txt", "uncommitted\n")
    orch._maybe_pre_handlers = pre_then_dirty
    report = orch.run()
    assert rw.spring_calls == 1 and waits == []
    assert ws.read("local.txt") == "uncommitted\n"
    assert [e["retry"] for e in _events(ws, "rewrite_unresolved_versions")] == [False]
    assert "rewrite-unresolved-versions" in report.flags


def test_scan_unresolved_versions(cfg, ws):
    orch = make_orch(cfg, ws)
    ws.write("pom.xml", "<project><version>1.0.0</version><properties><cloud.version>2025.1.x</cloud.version>"
                        "<x>latest.release</x><y>LATEST</y><z>4.x</z></properties>"
                        "<dependency><version>4.0.x</version></dependency></project>")
    ws.write("svc/pom.xml", "<project><version>latest.integration</version></project>")
    ws.write("svc/notes.txt", "<version>4.0.x</version>")
    assert orch._unresolved_versions(RewriteResult(ok=True, recipes=[], changed_files=["svc/pom.xml"])) == \
        {"svc/pom.xml": ["latest.integration"]}
    assert orch._unresolved_versions(RewriteResult(ok=True, recipes=[])) == {
        "pom.xml": ["2025.1.x", "latest.release", "4.x", "4.0.x"], "svc/pom.xml": ["latest.integration"]}
    assert orch._unresolved_versions(RewriteResult(ok=False, recipes=[])) == {}
    orch._dry_run = True
    assert orch._unresolved_versions(RewriteResult(ok=True, recipes=[])) == {}
