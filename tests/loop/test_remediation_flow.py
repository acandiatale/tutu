"""v2.1z-aj run flow `remediation` (docs/INTERFACES.md "v2.1z-aj"): no migration — intake without project edits, then
`chain.run_only`, then the short report. Fakes only (remediation components from tests/remediations/fakes.py, a fake
Maven build runner for the orchestrator): no IVAS, no Maven, no network, no LLM, no KB."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import Phase
from sbmig.core.workspace import Workspace
from sbmig.loop import Orchestrator, check_flow, parse_flow
from sbmig.remediations import chain
from sbmig.remediations.models import RoundPlan
from sbmig.report.markdown import render_markdown
from sbmig.report.result_line import report_result_line

from ..remediations.fakes import (
    POM,
    FakeMaven,
    FakeScanner,
    ScriptedPlanner,
    deterministic_zip,
    finding,
    make_components,
    proposal,
    scan_result,
)
from .fakes import FakeBuildRunner, journal_events

IVAS_ENV = {"IVAS_BASE_URL": "https://ivas.invalid", "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}
FILES = {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"}
# a particle standing apart after a Latin word / digit / backtick / bracket reads as machine output (tests/remediations)
DETACHED = re.compile(r"[A-Za-z0-9`)\]] (은|는|이|가|을|를|과|와|로|으로|의|에|에서|도|만|이라|라)(?=[\s,.)]|$)")


@pytest.fixture()
def ivas_env(monkeypatch):
    for k, v in IVAS_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LLM_API_KEY", raising=False)


class Untouchable:
    """Stands in for the LLM client / fixer / KB: any use fails the test."""

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item):
        raise AssertionError(f"the remediation flow touched the {self._name} ({item})")


def one_fix(**kw):
    scan = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    return make_components(scan=kw.pop("scan", scan), plan=kw.pop("plan", plan), **kw)


def upload(tmp_path, files=None, name="app.zip"):
    raw = {k: v.encode("utf-8") for k, v in (FILES if files is None else files).items()}
    z = tmp_path / "uploads" / name
    z.parent.mkdir(parents=True, exist_ok=True)
    z.write_bytes(deterministic_zip(raw))
    return z


def rem_cfg(cfg, engine="graph", **remediation):
    c = cfg.model_copy(deep=True)
    c.loop.engine = engine
    c.remediation.after_migration = False            # not consulted by this flow
    c.remediation.trusted_projects = False           # not consulted either
    for k, v in remediation.items():
        setattr(c.remediation, k, v)
    return c


def orch_for(cfg, ws, comps, *, source="journal", build=None):
    kw = {"source": source} if source != "journal" else {}
    build = build if build is not None else FakeBuildRunner(ws)
    return Orchestrator(cfg, ws, build_runner=build, remediation_components=comps, llm=Untouchable("LLM client"),
                        fixer=Untouchable("fixer"), kb=Untouchable("KB"), script_runner=Untouchable("script runner"),
                        reviewer=Untouchable("reviewer"), analyzer=Untouchable("analyzer"),
                        planner=Untouchable("planner"), restructurer=Untouchable("restructurer"),
                        rewrite_runner=Untouchable("rewrite runner"), **kw)


def run_flow(cfg, tmp_path, comps, *, engine="graph", files=None, source="journal", **remediation):
    z = upload(tmp_path, files)
    ws = Workspace.from_input(z, tmp_path / "out", run_id="t-rem")
    build = FakeBuildRunner(ws)
    orch = orch_for(rem_cfg(cfg, engine, **remediation), ws, comps, source=z if source == "zip" else source,
                    build=build)
    report = orch.run(flow="remediation")
    return orch, ws, report, z, build


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------- options
def test_parse_and_check_flow():
    assert parse_flow(None) == "full" and parse_flow("") == "full" and parse_flow(" Remediation ") == "remediation"
    with pytest.raises(ValueError, match="full, remediation"):
        parse_flow("migrate")
    with pytest.raises(ValueError, match="--stop-after"):
        check_flow("remediation", Phase.INTAKE)
    with pytest.raises(ValueError, match="--dry-run"):
        check_flow("remediation", None, True)
    assert check_flow("full", Phase.INTAKE, True) == "full"


def test_orchestrator_refuses_stop_after_with_the_remediation_flow(cfg, tmp_path):
    ws = Workspace.from_input(upload(tmp_path), tmp_path / "out", run_id="t-rem")
    orch = orch_for(rem_cfg(cfg), ws, one_fix())
    with pytest.raises(ValueError, match="--stop-after cannot be combined with --flow remediation"):
        orch.run(stop_after=Phase.INTAKE, flow="remediation")
    assert orch.report is None and not (ws.run_dir / "report.json").exists()


# ---------------------------------------------------------------------------------------------- happy path
@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_succeeded_end_to_end(cfg, tmp_path, ivas_env, engine, caplog):
    comps = one_fix()
    with caplog.at_level(logging.INFO, logger="sbmig"):
        orch, ws, report, z, build = run_flow(cfg, tmp_path, comps, engine=engine)
    assert report.flow == "remediation" and orch.flow == "remediation"
    assert report.success and run_exit_code(report) == 0, (report.flags, report.remediation)
    assert report.remediation["status"] == "SUCCEEDED" and report.remediation["dir"] == "remediation"
    assert build.calls == []                                   # the orchestrator built nothing (no migration)
    line = report_result_line(report)
    assert line == ("SBMIG-RESULT run=t-rem exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=0 llm=0 attn=- "
                    "flow=remediation remediation=SUCCEEDED")
    # files exactly where a chained run puts them, state id = run id
    paths = chain.chain_paths(ws.run_dir)
    assert chain.load_state(ws.run_dir).id == "t-rem" and paths.delivery_zip.is_file()
    # the uploaded ZIP's own bytes reached the remediation's intake (nothing re-zipped)
    assert sha(paths.original_zip) == sha(z)
    assert json.loads(paths.manifest.read_text())["zip_sha256"] == sha(z)
    # nothing in the project was edited: no git snapshot, files as uploaded
    assert not (ws.root / ".git").exists()
    assert {p.relative_to(ws.root).as_posix(): p.read_text() for p in ws.root.rglob("*") if p.is_file()} == FILES
    ev = journal_events(ws)
    names = [e["event"] for e in ev]
    assert names[0] == "intake" and ev[1] == {**ev[1], "event": "flow", "flow": "remediation"}
    assert "remediation_start" in names and names[-1] == "report"
    for skipped in ("analyze", "plan", "phase_start", "pom_synthesized", "aggregator_generated", "effective_pom"):
        assert skipped not in names
    if engine == "graph":
        assert [e["node"] for e in ev if e["event"] == "graph_node_end"] == ["intake", "remediation", "finalize"]
    else:
        assert not any(e["event"].startswith("graph_node") for e in ev)
    assert (ws.run_dir / "report.md").is_file() and (ws.run_dir / "report.json").is_file()
    assert json.loads((ws.run_dir / "report.json").read_text())["flow"] == "remediation"
    assert not (ws.run_dir / "result.zip").exists() and not (ws.run_dir / "errors.jsonl").exists()


def test_both_engines_write_the_same_report(cfg, tmp_path, ivas_env):
    def comparable(report):
        d = report.model_dump(mode="json", exclude={"timings_s", "outputs", "started_at", "finished_at"})
        return d
    _, _, rg, _, _ = run_flow(cfg, tmp_path / "g", one_fix(), engine="graph")
    _, _, ri, _, _ = run_flow(cfg, tmp_path / "i", one_fix(), engine="inline")
    assert comparable(rg) == comparable(ri)


def test_source_given_explicitly_is_used(cfg, tmp_path, ivas_env):
    _, ws, report, z, _ = run_flow(cfg, tmp_path, one_fix(), source="zip")
    assert report.success and sha(chain.chain_paths(ws.run_dir).original_zip) == sha(z)


def test_directory_input_is_zipped_as_it_is(cfg, tmp_path, ivas_env):
    src = tmp_path / "src" / "app"
    for rel, text in FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t-dir")
    report = orch_for(rem_cfg(cfg), ws, one_fix(), source=src).run(flow="remediation")
    assert report.success, report.remediation
    with zipfile.ZipFile(chain.chain_paths(ws.run_dir).original_zip) as zf:
        assert sorted(zf.namelist()) == sorted(FILES)


def test_runs_with_after_migration_off_and_untrusted_projects(cfg, tmp_path, ivas_env):
    _, _, report, _, _ = run_flow(cfg, tmp_path, one_fix(), after_migration=False, trusted_projects=False)
    assert report.remediation["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------------------------- other statuses
def test_awaiting_approval_is_exit_2(cfg, tmp_path, ivas_env):
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                      proposals=[proposal(decision="pending", risks=["major"])])])
    _, ws, report, _, _ = run_flow(cfg, tmp_path, one_fix(plan=plan))
    assert report.remediation["status"] == "AWAITING_APPROVAL" and not report.success
    assert run_exit_code(report) == 2
    assert report_result_line(report).endswith(" attn=incomplete flow=remediation remediation=AWAITING_APPROVAL")
    assert " exit=2 build=ok " in report_result_line(report)


def test_incomplete_is_exit_2(cfg, tmp_path, ivas_env):
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[])])
    scan = FakeScanner({"baseline": [scan_result(finding())]})
    _, ws, report, _, _ = run_flow(cfg, tmp_path, one_fix(plan=plan, scan=scan))
    assert report.remediation["status"] == "INCOMPLETE", report.remediation
    # findings left, but every build passed: exit 2, build=ok (the line must not read as a build failure)
    assert run_exit_code(report) == 2 and " build=ok " in report_result_line(report)
    assert report.remediation["baseline_build_ok"] is True


def test_failed_baseline_is_exit_2_without_the_migration_sentence(cfg, tmp_path, ivas_env):
    comps = one_fix(maven=FakeMaven(outcomes=[False]))
    _, ws, report, _, _ = run_flow(cfg, tmp_path, comps)
    rem = report.remediation
    assert rem["status"] == "FAILED" and rem["reason"] == "baseline_build_failed"
    assert run_exit_code(report) == 2
    assert "migration's final build passed" not in chain.load_state(ws.run_dir).message
    assert " exit=2 build=FAIL " in report_result_line(report)
    assert comps.scan.calls == []                              # a project that does not build is never scanned
    assert len(comps.maven.package_calls) == 1


# ---------------------------------------------------------------------------------------------- not started
@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_not_ready_is_exit_3_without_a_state(cfg, tmp_path, monkeypatch, caplog, engine):
    for k in IVAS_ENV:
        monkeypatch.delenv(k, raising=False)
    with caplog.at_level(logging.INFO, logger="sbmig"):
        _, ws, report, _, _ = run_flow(cfg, tmp_path, one_fix(), engine=engine)
    assert run_exit_code(report) == 3 and not report.success
    assert "remediation:not-ready" in report.flags and "aborted" in report.flags
    assert report.remediation["status"] == "skipped" and report.remediation["reason"] == "not_ready"
    missing = " ".join(report.remediation["missing"])
    assert "IVAS_BASE_URL" in missing and IVAS_ENV["IVAS_PASSWORD"] not in missing
    assert not chain.chain_paths(ws.run_dir).state.exists()
    ev = [e for e in journal_events(ws) if e["event"] == "remediation_skipped"]
    assert len(ev) == 1 and ev[0]["reason"] == "not_ready" and "IVAS_BASE_URL" in " ".join(ev[0]["missing"])
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "not ready" in errors[0].getMessage()
    line = report_result_line(report)
    assert "exit=3" in line and "remediation:not-ready" in line and line.endswith(" flow=remediation remediation=skipped")
    md = (ws.run_dir / "report.md").read_text(encoding="utf-8")
    assert "IVAS_BASE_URL" in md and "remediation/report.md" not in md


# ---------------------------------------------------------------------------------------------- report
def test_short_report_english_and_korean(cfg, tmp_path, ivas_env):
    _, ws, report, _, _ = run_flow(cfg, tmp_path, one_fix())
    en = render_markdown(report, "en")
    lines = en.splitlines()
    assert lines[0].startswith("# Vulnerability remediation report — ")
    assert "The migration was not run: only the vulnerability remediation was requested." in en
    assert "## Vulnerability remediation" in en and "`SUCCEEDED`" in en
    assert en.count("remediation/report.md") == 1                    # the section lists the file: no second line
    assert "Chained after the migration" not in en
    for section in ("Handover", "Structure review", "Fixes by case", "Error list", "LLM usage", "## Flags", "## Outputs"):
        assert section not in en, section
    ko = render_markdown(report, "ko")
    assert ko.splitlines()[0].startswith("# 취약점 조치 보고서 — ")
    assert "이관은 실행하지 않았습니다. 취약점 조치만 요청된 실행입니다." in ko
    assert "## 취약점 조치" in ko and "- 자세한 결과: `remediation/report.md`" in ko
    assert ko.count("remediation/report.md") == 1
    assert "이관 빌드가 성공한 뒤" not in ko and "인수인계" not in ko
    assert [ln for ln in ko.splitlines() if DETACHED.search(ln)] == []
    for word in ("재개", "에 의해", "되어집니다"):
        assert word not in ko


def test_full_run_line_has_no_flow_field(cfg, tmp_path):
    from ..loop.fakes import make_project
    from .test_orchestrator import make_orch
    root = tmp_path / "full"
    root.mkdir()
    ws = Workspace.from_input(make_project(root), root / "out", run_id="t-full")
    ws.git_init()
    report = make_orch(cfg, ws).run()
    assert report.flow == "full" and " flow=" not in report_result_line(report)
    assert not any(e["event"] == "flow" for e in journal_events(ws))


# ---------------------------------------------------------------------------------------------- build targets
SVC_POM = POM.replace("<artifactId>app</artifactId>", "<artifactId>{a}</artifactId>")
SERVICES = {"svc-a/pom.xml": SVC_POM.format(a="svc-a"), "svc-a/src/main/java/A.java": "class A {}\n",
            "svc-b/pom.xml": SVC_POM.format(a="svc-b"), "svc-b/src/main/java/B.java": "class B {}\n"}


@pytest.mark.parametrize("wrapped", [False, True])
def test_per_service_tree_without_a_root_pom_builds_each_service(cfg, tmp_path, ivas_env, wrapped):
    files = {f"proj/{k}": v for k, v in SERVICES.items()} if wrapped else SERVICES
    comps = one_fix()
    _, ws, report, _, _ = run_flow(cfg, tmp_path, comps, files=files)
    want = ["proj/svc-a", "proj/svc-b"] if wrapped else ["svc-a", "svc-b"]
    st = chain.load_state(ws.run_dir)
    assert st.build_targets == want, (st.status, st.reason, st.message)
    assert ("package", want) in comps.maven.targets_seen
    ev = [e for e in journal_events(ws) if e["event"] == "remediation_build_targets"]
    assert ev and ev[0]["targets"] == want


def test_root_pom_means_no_targets(cfg, tmp_path, ivas_env):
    comps = one_fix()
    _, ws, _, _, _ = run_flow(cfg, tmp_path, comps)
    assert chain.load_state(ws.run_dir).build_targets == []
    assert ("package", None) in comps.maven.targets_seen


def test_targets_follow_the_modules_of_an_aggregator(cfg, tmp_path):
    files = {"app/pom.xml": POM.replace("</version>\n  <dependencies>",
                                        "</version>\n  <packaging>pom</packaging>\n  <modules><module>../core</module>"
                                        "</modules>\n  <dependencies>", 1),
             "core/pom.xml": SVC_POM.format(a="core"), "batch/pom.xml": SVC_POM.format(a="batch")}
    ws = Workspace.from_input(upload(tmp_path, files), tmp_path / "out", run_id="t-agg")
    orch = orch_for(rem_cfg(cfg), ws, one_fix())
    assert orch._remediation_targets() == ["app", "batch"]          # core is reached through app's <modules>


# ---------------------------------------------------------------------------------------------- engine
def test_stream_graph_yields_the_three_nodes(cfg, tmp_path, ivas_env):
    from sbmig.graph import REMEDIATION_NODES, stream_graph
    ws = Workspace.from_input(upload(tmp_path), tmp_path / "out", run_id="t-stream")
    orch = orch_for(rem_cfg(cfg), ws, one_fix())
    seen = [(n, u) for n, u in stream_graph(orch, flow="remediation")]
    assert [n for n, _ in seen] == list(REMEDIATION_NODES) == ["intake", "remediation", "finalize"]
    assert seen[1][1]["remediation"] == {"status": "SUCCEEDED", "reason": ""}
    json.dumps([u for _, u in seen])
    assert orch.report.flow == "remediation" and orch.report.success


def test_stream_graph_refuses_stop_after(cfg, tmp_path):
    from sbmig.graph import stream_graph
    ws = Workspace.from_input(upload(tmp_path), tmp_path / "out", run_id="t-stream")
    with pytest.raises(ValueError, match="--stop-after"):
        list(stream_graph(orch_for(rem_cfg(cfg), ws, one_fix()), stop_after=Phase.INTAKE, flow="remediation"))


@pytest.mark.parametrize("engine", ["graph", "inline"])
def test_stop_request_before_the_remediation(cfg, tmp_path, ivas_env, monkeypatch, engine):
    ws = Workspace.from_input(upload(tmp_path), tmp_path / "out", run_id="t-stop")
    comps = one_fix()
    orch = orch_for(rem_cfg(cfg, engine), ws, comps)
    real = orch.step_intake

    def intake_then_stop(report, stop_after):
        ok = real(report, stop_after)
        (ws.run_dir / "stop.request").write_text("stop")
        return ok
    monkeypatch.setattr(orch, "step_intake", intake_then_stop)
    report = orch.run(flow="remediation")
    assert "aborted" in report.flags and run_exit_code(report) == 3
    assert comps.maven.package_calls == [] and not chain.chain_paths(ws.run_dir).state.exists()


def test_an_existing_state_is_returned_untouched(cfg, tmp_path, ivas_env):
    from sbmig.remediations.models import STOPPED, RemediationState, write_json_atomic
    ws = Workspace.from_input(upload(tmp_path), tmp_path / "out", run_id="t-again")
    paths = chain.chain_paths(ws.run_dir)
    write_json_atomic(paths.state, RemediationState(id="t-again", status=STOPPED, next_node="baseline_scan").to_dict())
    before = paths.state.read_bytes()
    comps = one_fix()
    report = orch_for(rem_cfg(cfg), ws, comps).run(flow="remediation")
    assert report.remediation["status"] == STOPPED and run_exit_code(report) == 2
    assert paths.state.read_bytes() == before and comps.maven.package_calls == []


# ---------------------------------------------------------------------------------------------- CLI / API
def test_cli_flow_values(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from sbmig import loop
    from sbmig.cli import app, detach_file_log
    seen = []

    class FakeOrch:
        def __init__(self, cfg, ws, **kw):
            self.ws = ws
            seen.append(kw)

        def run(self, stop_after=None, dry_run=False, flow="full"):
            from sbmig.core.models import CaseType, RunReport, utcnow
            seen.append({"flow": flow})
            return RunReport(run_id=self.ws.run_id, project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(),
                             success=True, flow=flow,
                             remediation={"status": "SUCCEEDED", "baseline_build_ok": True})

    monkeypatch.setattr(loop, "Orchestrator", FakeOrch)
    from ..conftest import REPO_ROOT
    e2e = str(REPO_ROOT / "config" / "artifacts.local-e2e.yml")
    z = upload(tmp_path)
    out = tmp_path / "out"
    r = CliRunner().invoke(app, ["run", str(z), "--config", e2e, "--out", str(out), "--flow", "bogus"])
    assert r.exit_code == 2 and "flow must be one of: full, remediation" in r.output and not seen
    r = CliRunner().invoke(app, ["run", str(z), "--config", e2e, "--out", str(out), "--flow", "remediation",
                                 "--stop-after", "intake"])
    assert r.exit_code == 2 and "--stop-after cannot be combined" in r.output and not seen
    try:
        r = CliRunner().invoke(app, ["run", str(z), "--config", e2e, "--out", str(out), "--run-id", "rid-r",
                                     "--flow", "remediation"])
    finally:
        for f in out.glob("*/sbmig.log"):
            detach_file_log(f)
    assert r.exit_code == 0, r.output
    assert seen[0]["source"] == z and seen[1] == {"flow": "remediation"}
    assert r.output.splitlines()[-1] == ("SBMIG-RESULT run=rid-r exit=0 build=ok unresolved=0 blocked=0 open=0 resolved=0 "
                                         "llm=0 attn=- flow=remediation remediation=SUCCEEDED")


def test_api_migrate_checks_the_flow(tmp_path):
    from sbmig.api import migrate
    with pytest.raises(ValueError, match="flow must be one of"):
        migrate(upload(tmp_path), flow="bogus", out=tmp_path / "out")
    with pytest.raises(ValueError, match="--dry-run"):
        migrate(upload(tmp_path), flow="remediation", dry_run=True, out=tmp_path / "out")
