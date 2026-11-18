"""Framework projects (v2.1z-ak): the orchestrator's analyze step (`_check_framework`) and the steps of a reduced run
(scope jdk-jakarta: transform.rewrite → transform.jsp_jakarta → verify.build). The detection module is replaced by a fake
(`sbmig.analyze.framework` in sys.modules) so these tests pin the orchestrator only; `test_real_module_*` runs the real
module through the same method."""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import (FrameworkEvidence, FrameworkUse, MigrationPlan, Phase, PlanStep, RecipeRef,
                               RewriteResult, RunReport, StepKind, utcnow)
from sbmig.loop.orchestrator import step_stage

from .fakes import (FakeBuildRunner, FakeRestructurer, FakeReviewer, FakeRewriteRunner, journal_events, make_profile,
                    ok_build)
from .test_asis_parent import miss_msg, pom
from .test_orchestrator import make_orch

FW = "com.example.fw:example-fw-core"
JSP = "src/main/webapp/WEB-INF/jsp/a.jsp"
JSP_TEXT = '<%@ page import="javax.servlet.http.HttpServletRequest" %>\n<p>hi</p>\n'


# ------------------------------------------------------------------------------------------------ fake detection module
class FakeFw:
    """Stands in for `sbmig.analyze.framework` (contract API only)."""

    FLAG = "framework-project"

    def __init__(self, transitive=None, why=None, raises=None):
        self.transitive = list(transitive or [])
        self.why = why
        self.raises = raises
        self.calls = 0

    def transitive_evidence(self, ws, cfg, profile):
        self.calls += 1
        if self.raises:
            raise self.raises
        return list(self.transitive), self.why

    @staticmethod
    def decide_scope(use, cfg):
        fc = cfg.framework
        applies = [e for e in use.evidence if e.source != "transitive" or fc.transitive == "apply"]
        reduced = fc.scope == "jdk-jakarta" and bool(applies)
        return use.model_copy(update={"scope": "jdk-jakarta" if reduced else "full",
                                      "reason": "reduced" if reduced else "full migration"})

    @staticmethod
    def summary_text(use):
        e = use.evidence[0]
        return f"{e.artifact} {e.version or '?'} ({e.source}: {e.where})"

    @staticmethod
    def framework_followup(use):
        return f"FW-LINE {use.evidence[0].artifact}"

    def module(self):
        m = types.ModuleType("sbmig.analyze.framework")
        for name in ("FLAG", "transitive_evidence", "decide_scope", "summary_text", "framework_followup"):
            setattr(m, name, getattr(self, name))
        return m


@pytest.fixture()
def fake_fw(monkeypatch):
    fw = FakeFw()
    monkeypatch.setitem(sys.modules, "sbmig.analyze.framework", fw.module())
    return fw


def install(monkeypatch, fw: FakeFw) -> FakeFw:
    monkeypatch.setitem(sys.modules, "sbmig.analyze.framework", fw.module())
    return fw


def fw_cfg(cfg, scope="jdk-jakarta", transitive="apply"):
    c = cfg.model_copy(deep=True)
    c.framework.artifacts = ["example-fw-core"]
    c.framework.scope = scope
    c.framework.transitive = transitive
    return c


def direct_use(scope="jdk-jakarta") -> FrameworkUse:
    return FrameworkUse(patterns=["example-fw-core"], scope=scope, reason="r",
                        evidence=[FrameworkEvidence(source="direct", artifact=FW, version="1.2.0", where="pom.xml")])


def empty_use() -> FrameworkUse:
    return FrameworkUse(patterns=["example-fw-core"], unchecked=["parent: no as-is effective pom"])


def fw_profile(use: FrameworkUse | None):
    p = make_profile()
    p.framework = use
    return p


def bare_orch(cfg, ws, *, real_maven=True, dry_run=False):
    orch = make_orch(cfg, ws)
    orch._real_maven = real_maven
    orch._dry_run = dry_run
    orch.report = RunReport(run_id="t", project_name="demo", case_type=make_profile().case_type, started_at=utcnow())
    return orch


def events(ws, name):
    return [e for e in journal_events(ws) if e["event"] == name] if ws.journal_path.exists() else []


# ------------------------------------------------------------------------------------------------ _check_framework
def test_no_framework_check_is_a_no_op(cfg, ws, fake_fw):
    orch = bare_orch(cfg, ws)
    orch._check_framework(orch.report, fw_profile(None))
    assert fake_fw.calls == 0 and "framework-project" not in orch.report.flags
    assert not events(ws, "framework_project") and not events(ws, "framework_check")


def test_found_direct_reduced(cfg, ws, fake_fw, caplog):
    orch = bare_orch(cfg, ws)
    orch.report.manual_followups = ["other line"]
    prof = fw_profile(direct_use())
    with caplog.at_level(logging.INFO, logger="sbmig.loop"):
        orch._check_framework(orch.report, prof)
    assert fake_fw.calls == 0                                        # evidence already there: no dependency:tree
    assert "framework-project" in orch.report.flags
    assert orch.report.manual_followups == [f"FW-LINE {FW}", "other line"]
    [ev] = events(ws, "framework_project")
    assert ev["scope"] == "jdk-jakarta" and ev["artifacts"] == [FW] and ev["reason"] == "r"
    assert ev["evidence"][0]["source"] == "direct" and ev["evidence"][0]["where"] == "pom.xml"
    assert ev["unchecked"] == []
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("분석: 사내 프레임워크")]
    assert line == f"분석: 사내 프레임워크 프로젝트 ({FW} 1.2.0 (direct: pom.xml)) - JDK와 jakarta만 적용"
    assert "—" not in line


def test_found_with_scope_full_says_full_migration(cfg, ws, fake_fw, caplog):
    orch = bare_orch(fw_cfg(cfg, scope="full"), ws)
    with caplog.at_level(logging.INFO, logger="sbmig.loop"):
        orch._check_framework(orch.report, fw_profile(direct_use(scope="full")))
    [ev] = events(ws, "framework_project")
    assert ev["scope"] == "full" and "framework-project" in orch.report.flags
    assert any(r.getMessage().endswith("- 설정에 따라 전체 이관") for r in caplog.records)


def test_not_found_runs_the_transitive_check_and_saves_the_profile(cfg, ws, monkeypatch):
    ev = FrameworkEvidence(source="transitive", artifact=FW, version="1.2.0", where="demo",
                           via=["com.x:lib:1", f"{FW}:1.2.0"])
    fw = install(monkeypatch, FakeFw(transitive=[ev]))
    orch = bare_orch(fw_cfg(cfg), ws)
    prof = fw_profile(empty_use())
    orch.report.profile = prof
    orch._check_framework(orch.report, prof)
    assert fw.calls == 1
    assert prof.framework.reduced and prof.reduced_scope and orch.report.profile is prof
    saved = json.loads((ws.run_dir / "profile.json").read_text())
    assert saved["framework"]["scope"] == "jdk-jakarta"
    assert saved["framework"]["evidence"][0]["source"] == "transitive"
    [j] = events(ws, "framework_project")
    assert j["evidence"][0]["via"] == ["com.x:lib:1", f"{FW}:1.2.0"]
    assert not events(ws, "framework_check")


def test_transitive_note_keeps_the_full_scope(cfg, ws, monkeypatch):
    ev = FrameworkEvidence(source="transitive", artifact=FW, version="1.2.0", where="demo")
    install(monkeypatch, FakeFw(transitive=[ev]))
    orch = bare_orch(fw_cfg(cfg, transitive="note"), ws)
    prof = fw_profile(empty_use())
    orch._check_framework(orch.report, prof)
    assert not prof.reduced_scope and events(ws, "framework_project")[0]["scope"] == "full"


@pytest.mark.parametrize("why", ["dry-run", "off", "custom-runner"])
def test_transitive_check_not_made(cfg, ws, monkeypatch, why):
    fw = install(monkeypatch, FakeFw(transitive=[FrameworkEvidence(source="transitive", artifact=FW)]))
    c = fw_cfg(cfg, transitive="off" if why == "off" else "apply")
    orch = bare_orch(c, ws, real_maven=why != "custom-runner", dry_run=why == "dry-run")
    prof = fw_profile(empty_use())
    orch._check_framework(orch.report, prof)
    assert fw.calls == 0 and "framework-project" not in orch.report.flags
    [j] = events(ws, "framework_check")
    assert j == {**j, "found": False, "unchecked": ["parent: no as-is effective pom"]}
    assert not events(ws, "framework_project") and not prof.reduced_scope


def test_transitive_failure_is_unchecked_and_never_stops(cfg, ws, monkeypatch):
    fw = install(monkeypatch, FakeFw(why="mvn dependency:tree failed (exit 1)"))
    orch = bare_orch(fw_cfg(cfg), ws)
    prof = fw_profile(empty_use())
    orch._check_framework(orch.report, prof)
    assert fw.calls == 1
    [j] = events(ws, "framework_check")
    assert j["unchecked"] == ["parent: no as-is effective pom", "transitive: mvn dependency:tree failed (exit 1)"]
    assert json.loads((ws.run_dir / "profile.json").read_text())["framework"]["unchecked"][-1].startswith("transitive:")


def test_transitive_exception_is_unchecked(cfg, ws, monkeypatch):
    install(monkeypatch, FakeFw(raises=RuntimeError("boom")))
    orch = bare_orch(fw_cfg(cfg), ws)
    prof = fw_profile(empty_use())
    orch._check_framework(orch.report, prof)
    [j] = events(ws, "framework_check")
    assert j["unchecked"][-1] == "transitive: RuntimeError: boom"


def test_handover_line_goes_after_the_adf_and_hollow_lines(cfg, ws, fake_fw):
    from sbmig.analyze.adf import ADF_PREFIX
    from sbmig.analyze.hollow import HOLLOW_PREFIX
    orch = bare_orch(cfg, ws)
    adf, hollow = f"{ADF_PREFIX} x", f"{HOLLOW_PREFIX} y"
    orch.report.manual_followups = [adf, hollow, "later"]
    orch._check_framework(orch.report, fw_profile(direct_use()))
    assert orch.report.manual_followups == [adf, hollow, f"FW-LINE {FW}", "later"]
    orch.report.manual_followups = [adf]
    orch.report.flags.remove("framework-project")
    orch._check_framework(orch.report, fw_profile(direct_use()))
    assert orch.report.manual_followups == [adf, f"FW-LINE {FW}"]


# ------------------------------------------------------------------------------------------------ reduced run end to end
def reduced_plan(jsp: bool = True) -> MigrationPlan:
    steps = [PlanStep(id="transform.rewrite", kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM, title="jdk21 + jakarta",
                      recipes=[RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"),
                               RecipeRef(id="org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta")])]
    if jsp:
        steps.append(PlanStep(id="transform.jsp_jakarta", kind=StepKind.RESTRUCTURE, phase=Phase.TRANSFORM,
                              title="JSP javax -> jakarta", params={"mode": "jsp_jakarta"}))
    steps.append(PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="final mvn clean package",
                          build_goal="clean package"))
    return MigrationPlan(profile_fingerprint="fp123", case_type=make_profile().case_type, steps=steps,
                         notes=["scope: jdk-jakarta — framework project (x)"], scope="jdk-jakarta")


def reduced_orch(cfg, ws, *, rewrite=None, build=None, engine="graph"):
    c = cfg.model_copy(deep=True)
    c.loop.engine = engine
    restructurer, reviewer = FakeRestructurer(), FakeReviewer()
    build = build or FakeBuildRunner(ws, fn=lambda w, call: ok_build())
    orch = make_orch(c, ws, plan=reduced_plan(), profile=fw_profile(direct_use()), build=build,
                     rewrite=rewrite or FakeRewriteRunner(ws), restructurer=restructurer, reviewer=reviewer)
    return orch, restructurer, reviewer, build


def test_jsp_step_logs_under_the_rewrite_tag():
    step = reduced_plan().steps[1]
    assert step_stage(step) == "rewrite"
    assert step_stage(PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
                               title="r", params={"mode": "layout"})) == "restructure"


@pytest.mark.parametrize("engine", ["graph"])
def test_reduced_run_end_to_end(cfg, ws, fake_fw, engine):
    ws.write(JSP, JSP_TEXT)
    ws.commit("jsp")
    orch, restructurer, reviewer, build = reduced_orch(cfg, ws, engine=engine)
    report = orch.run()
    assert restructurer.calls == [] and reviewer.calls == []
    assert report.success and run_exit_code(report) == 0 and report.structure_review is None
    assert "restructure-failed" not in report.flags and "framework-project" in report.flags
    assert [p.phase for p in report.phases] == [Phase.TRANSFORM, Phase.VERIFY]
    assert [c["label"] for c in build.calls] == ["verify"]
    assert "jakarta.servlet.http.HttpServletRequest" in ws.read(JSP)
    log = ws._git("log", "--format=%s").stdout.splitlines()
    assert "transform: JSP javax → jakarta" in log and not any(s.startswith("restructure:") for s in log)
    ev = journal_events(ws)
    starts = [e["step"] for e in ev if e["event"] == "step_start"]
    assert starts == ["transform.rewrite", "transform.jsp_jakarta", "verify.build"]
    [plan_ev] = [e for e in ev if e["event"] == "plan"]
    assert plan_ev["scope"] == "jdk-jakarta"
    [jsp_ev] = [e for e in ev if e["event"] == "jsp_jakarta"]
    assert jsp_ev["step"] == "transform.jsp_jakarta" and jsp_ev["files"] == 1 and jsp_ev["refs"] == 1
    assert any(n == "transform.jsp_jakarta: JSP javax → jakarta: 1 file, 1 reference" for n in report.phases[0].notes)
    from sbmig.report.handover import jsp_jakarta_counts
    assert jsp_jakarta_counts(report) == (1, 1)
    assert orch.loop.restructure_results == [] and orch.loop.restructure_modes == []
    from sbmig.report.result_line import report_result_line
    assert " review=" not in report_result_line(report)


def test_reduced_run_jsp_step_in_a_dry_run_changes_nothing(cfg, ws, fake_fw):
    ws.write(JSP, JSP_TEXT)
    ws.commit("jsp")
    orch, *_ = reduced_orch(cfg, ws)
    report = orch.run(dry_run=True)
    assert ws.read(JSP) == JSP_TEXT
    assert any(n == "transform.jsp_jakarta: dry-run → JSP javax → jakarta not applied" for n in report.phases[0].notes)


def test_reduced_run_without_javax_jsps_commits_nothing(cfg, ws, fake_fw):
    orch, *_ = reduced_orch(cfg, ws)
    report = orch.run()
    assert report.success
    assert "transform: JSP javax → jakarta" not in ws._git("log", "--format=%s").stdout
    assert not [e for e in journal_events(ws) if e["event"] == "jsp_jakarta"]


def test_reduced_run_chains_the_remediation(cfg, ws, fake_fw, monkeypatch):
    from tests.graph.test_engine import ChainSpy
    from sbmig.remediations import chain
    spy = ChainSpy()
    monkeypatch.setattr(chain, "run_after_migration", spy)
    c = cfg.model_copy(deep=True)
    c.remediation.after_migration = True
    orch, *_ = reduced_orch(c, ws)
    report = orch.run()
    assert len(spy.calls) == 1 and report.remediation["status"] == "SUCCEEDED"
    assert report.success and run_exit_code(report) == 0


# ------------------------------------------------------------------------------------------------ parent in a reduced run
class ParentRewrite:
    """Fails like Maven while the root pom names `coords` as its parent."""

    def __init__(self, ws, coords: str):
        self.ws, self.coords, self.runs = ws, coords, 0

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", **kw):
        self.runs += 1
        return RewriteResult(ok=False, recipes=[r.id for r in recipes],
                             error="parent_resolution: " + miss_msg(self.coords, 1))


@pytest.mark.parametrize("which", ["foreign", "company-asis"])
def test_unresolvable_parent_aborts_a_reduced_run_without_alignment(cfg, ws, fake_fw, which):
    p = cfg.artifacts.parent
    coords = "com.acme:acme-parent:9.9" if which == "foreign" else f"{p.group_id}:{p.artifact_id}:1.0.0-SNAPSHOT"
    g, a, v = coords.split(":")
    text = pom(f"{g}:{a}", v)
    ws.write("pom.xml", text)
    ws.commit("as-is pom")
    rw = ParentRewrite(ws, coords)
    orch, restructurer, reviewer, build = reduced_orch(cfg, ws, rewrite=rw)
    report = orch.run()
    assert rw.runs == 1 and ws.read("pom.xml") == text                # the parent is kept: no alignment, no retry
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3 and not report.success
    assert "asis-parent-aligned" not in report.flags and "rewrite-skipped-asis-parent" not in report.flags
    assert build.calls == [] and restructurer.calls == [] and reviewer.calls == []
    [fu] = [f for f in report.manual_followups if f.startswith("run aborted")]
    assert fu == (f"run aborted: Maven cannot resolve the parent pom {coords}; a framework project keeps its own parent "
                  "pom, so the repository must serve it (settings: "
                  f"{cfg.build.settings_file or 'default'}). Make the repository serve it and rerun")
    assert [b.reason.value for b in report.blocked] == ["parent_not_found"]
    assert not [e for e in journal_events(ws) if e["event"] == "asis_parent_aligned"]


def test_unresolvable_company_asis_parent_is_still_aligned_in_a_full_run(cfg, ws):
    """Guard: the full-scope behaviour (align + retry) is untouched by the reduced-scope branch."""
    p = cfg.artifacts.parent
    ws.write("pom.xml", pom(f"{p.group_id}:{p.artifact_id}", "1.0.0-SNAPSHOT"))
    ws.commit("as-is pom")

    class Once(ParentRewrite):
        def run(self, recipes, **kw):
            if "1.0.0-SNAPSHOT" in self.ws.read("pom.xml"):
                return super().run(recipes, **kw)
            self.runs += 1
            return RewriteResult(ok=True, recipes=[r.id for r in recipes])

    rw = Once(ws, f"{p.group_id}:{p.artifact_id}:1.0.0-SNAPSHOT")
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 2 and "asis-parent-aligned" in report.flags and report.success


# ------------------------------------------------------------------------------------------------ the real module
def test_real_module_with_the_orchestrator(cfg, ws):
    fw = pytest.importorskip("sbmig.analyze.framework")
    orch = bare_orch(fw_cfg(cfg), ws, real_maven=False)
    prof = fw_profile(fw.decide_scope(direct_use(scope="full"), fw_cfg(cfg)))
    orch._check_framework(orch.report, prof)
    assert prof.reduced_scope and fw.FLAG in orch.report.flags
    [ev] = events(ws, "framework_project")
    assert ev["scope"] == "jdk-jakarta" and ev["artifacts"] == [FW]
    assert orch.report.manual_followups and orch.report.manual_followups[0] == fw.framework_followup(prof.framework)


# ------------------------------------------------------------------------------------------------ as-is provided scopes
SERVLET_ASIS = ('<project><modelVersion>4.0.0</modelVersion><groupId>com.x</groupId><artifactId>demo</artifactId>'
                '<version>1</version><packaging>war</packaging>\n  <dependencies>\n    <dependency>\n'
                '      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n      <version>{v}</version>\n'
                '      <scope>{s}</scope>\n    </dependency>\n  </dependencies>\n</project>\n')


def servlet_profile():
    from sbmig.core.models import GAV, PomInfo
    p = fw_profile(direct_use())
    root = PomInfo(path="pom.xml", artifact_id="demo", is_root=True,
                   dependencies=[GAV(group_id="javax.servlet", artifact_id="javax.servlet-api", version="3.1.0",
                                     scope="provided")])
    p.poms, p.root_pom = [root], root
    return p


def jakarta_recipe(w, refs):
    """What JavaxMigrationToJakarta does to a provided javax.servlet-api: jakarta 5.0.0 at compile scope."""
    w.write("pom.xml", SERVLET_ASIS.format(g="jakarta.servlet", a="jakarta.servlet-api", v="5.0.0", s="compile"))
    return ["pom.xml"]


@pytest.mark.parametrize("dry", [False, True])
def test_reduced_run_restores_the_asis_provided_scope(cfg, ws, fake_fw, dry):
    ws.write("pom.xml", SERVLET_ASIS.format(g="javax.servlet", a="javax.servlet-api", v="3.1.0", s="provided"))
    ws.commit("as-is")
    c = cfg.model_copy(deep=True)
    c.loop.engine = "graph"
    orch = make_orch(c, ws, plan=reduced_plan(jsp=False), profile=servlet_profile(),
                     build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()),
                     rewrite=FakeRewriteRunner(ws, effect=jakarta_recipe))
    report = orch.run(dry_run=dry)
    notes = report.phases[0].notes
    if dry:
        assert "transform.rewrite: dry-run → as-is provided scopes not restored" in notes
        assert not [e for e in journal_events(ws) if e["event"] == "scopes_restored"]
        return
    assert report.success and "<scope>provided</scope>" in ws.read("pom.xml")
    entry = "jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, pom.xml)"
    log = ws._git("log", "--format=%s").stdout.splitlines()
    assert log[0] == "transform: as-is provided scopes restored" and log[1] == "rewrite: transform.rewrite"
    [ev] = [e for e in journal_events(ws) if e["event"] == "scopes_restored"]
    assert ev["step"] == "transform.rewrite" and ev["entries"] == [entry]
    assert notes.count(f"as-is provided scopes restored after the recipes: {entry}") == 1
    msgs = [getattr(e, "message", None) or (e.get("message") if isinstance(e, dict) else "") for e in report.error_entries]
    assert "scope restored: provided (as-is): jakarta.servlet:jakarta.servlet-api (was compile)" in msgs


def test_full_run_does_not_restore_scopes_in_the_transform_step(cfg, ws):
    ws.write("pom.xml", SERVLET_ASIS.format(g="javax.servlet", a="javax.servlet-api", v="3.1.0", s="provided"))
    ws.commit("as-is")
    orch = make_orch(cfg, ws, profile=servlet_profile().model_copy(update={"framework": None}),
                     rewrite=FakeRewriteRunner(ws, effect=jakarta_recipe),
                     build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    orch.run()
    assert not [e for e in journal_events(ws) if e["event"] == "scopes_restored"]
    assert "transform: as-is provided scopes restored" not in ws._git("log", "--format=%s").stdout


# ------------------------------------------------------------------------------------------------ source encodings
ENC_LINE = "pom.xml: project.build.sourceEncoding=MS949 (1 of 1 sampled .java files cp949)"


def encoding_ws(ws):
    for f in ws.glob("*.java"):
        ws.delete(f)
    ws.write("pom.xml", "<project><modelVersion>4.0.0</modelVersion><groupId>com.x</groupId>"
                        "<artifactId>demo</artifactId><version>1</version></project>\n")
    ws.abs("src/main/java/com/x/Greeting.java").write_bytes(
        'package com.x;\n/** 인사 */\nclass Greeting { String s = "안녕하세요"; }\n'.encode("cp949"))
    ws.commit("as-is MS949")


@pytest.mark.parametrize("dry", [False, True])
def test_reduced_run_writes_the_source_encoding(cfg, ws, fake_fw, dry):
    encoding_ws(ws)
    orch, *_ = reduced_orch(cfg, ws)
    report = orch.run(dry_run=dry)
    notes = report.phases[0].notes
    if dry:
        assert "transform.rewrite: dry-run → source encodings not written" in notes
        assert "sourceEncoding" not in ws.read("pom.xml")
        return
    assert report.success
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in ws.read("pom.xml")
    assert ws._git("log", "--format=%s").stdout.splitlines()[0] == "transform: source encodings written"
    [ev] = [e for e in journal_events(ws) if e["event"] == "source_encodings"]
    assert ev == {**ev, "step": "transform.rewrite", "entries": [ENC_LINE]}
    assert notes.count("source encoding written: " + ENC_LINE) == 1
    msgs = [getattr(e, "message", None) or (e.get("message") if isinstance(e, dict) else "") for e in report.error_entries]
    assert "source encoding written: project.build.sourceEncoding=MS949" in msgs


def test_reduced_run_keeps_a_conflicting_parent_encoding(cfg, ws, fake_fw, monkeypatch):
    encoding_ws(ws)
    ws.write("pom.xml", "<project><modelVersion>4.0.0</modelVersion><parent><groupId>com.example.fw</groupId>"
                        "<artifactId>example-parent</artifactId><version>1.0</version></parent>"
                        "<groupId>com.x</groupId><artifactId>demo</artifactId><version>1</version></project>\n")
    ws.commit("external parent")
    orch, *_ = reduced_orch(cfg, ws)
    monkeypatch.setattr(orch, "_asis_effective", lambda: {
        "properties": {"project.build.sourceEncoding": "UTF-8"}, "parent": "com.example.fw:example-parent:1.0",
        "managed": {}, "direct": {"a:b": "1"}, "by_module": {}})
    report = orch.run()
    assert "sourceEncoding" not in ws.read("pom.xml")
    assert not [e for e in journal_events(ws) if e["event"] == "source_encodings"]
    assert any("sets project.build.sourceEncoding=UTF-8 — set it to MS949 in pom.xml" in f
               for f in report.manual_followups)


# ------------------------------------------------------------------------------------------------ order and retries
class EncodingAwareRewrite:
    """Records whether the pom declared the encoding when OpenRewrite ran; turns the provided javax.servlet-api into
    jakarta compile; `unresolved_first` leaves a literal `4.0.x` on the first run (the download retry path)."""

    def __init__(self, ws, unresolved_first: bool = False):
        self.ws, self.unresolved_first, self.seen_encoding = ws, unresolved_first, []

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", **kw):
        text = self.ws.read("pom.xml")
        self.seen_encoding.append("<project.build.sourceEncoding>MS949" in text)
        version = "4.0.x" if self.unresolved_first and len(self.seen_encoding) == 1 else "5.0.0"
        new = text.replace("<groupId>javax.servlet</groupId>", "<groupId>jakarta.servlet</groupId>") \
            .replace("<artifactId>javax.servlet-api</artifactId>", "<artifactId>jakarta.servlet-api</artifactId>") \
            .replace("<version>3.1.0</version>", f"<version>{version}</version>") \
            .replace("<scope>provided</scope>", "<scope>compile</scope>")
        self.ws.write("pom.xml", new)
        return RewriteResult(ok=True, recipes=[r.id for r in recipes], changed_files=["pom.xml"])


@pytest.mark.parametrize("retry", [False, True])
def test_reduced_run_order_encoding_rewrite_scopes(cfg, ws, fake_fw, retry):
    encoding_ws(ws)
    ws.write("pom.xml", SERVLET_ASIS.format(g="javax.servlet", a="javax.servlet-api", v="3.1.0", s="provided"))
    ws.commit("as-is servlet")
    c = cfg.model_copy(deep=True)
    c.loop.engine = "graph"
    c.openrewrite.download_retry, c.openrewrite.download_retry_wait_s = 1, 0
    rw = EncodingAwareRewrite(ws, unresolved_first=retry)
    orch = make_orch(c, ws, plan=reduced_plan(jsp=False), profile=servlet_profile(),
                     build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()), rewrite=rw)
    report = orch.run()
    assert report.success
    assert rw.seen_encoding == ([True, True] if retry else [True])     # OpenRewrite always saw the encoding
    log = ws._git("log", "--format=%s").stdout.splitlines()
    mine = [s for s in log if s.startswith(("transform:", "rewrite:"))]
    assert mine == ["transform: as-is provided scopes restored", "rewrite: transform.rewrite",
                    "transform: source encodings written"]                # newest first
    text = ws.read("pom.xml")
    assert text.count("<project.build.sourceEncoding>MS949</project.build.sourceEncoding>") == 1
    assert "<scope>provided</scope>" in text and "4.0.x" not in text
    assert len([e for e in journal_events(ws) if e["event"] == "source_encodings"]) == 1
    seen = {getattr(e, "message", ""): getattr(e, "seen", None) for e in report.error_entries}
    assert seen["source encoding written: project.build.sourceEncoding=MS949"] == "transform"
    assert seen["scope restored: provided (as-is): jakarta.servlet:jakarta.servlet-api (was compile)"] == "transform"
    assert all(getattr(e, "status", "") != "pom-verify" for e in report.error_entries)     # verified by the build


def test_second_transform_step_writes_no_encoding_again(cfg, ws, fake_fw):
    encoding_ws(ws)
    orch, *_ = reduced_orch(cfg, ws)
    orch.run()
    from sbmig.core.models import PhaseResult
    pr = PhaseResult(phase=Phase.TRANSFORM, step_ids=["transform.rewrite@b"], ok=True)
    head = ws.head()
    orch._write_encodings_reduced(pr, PlanStep(id="transform.rewrite@b", kind=StepKind.OPENREWRITE,
                                               phase=Phase.TRANSFORM, title="b"))
    assert ws.head() == head and not pr.notes
