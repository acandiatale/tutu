"""Flow `remediation`: the short run report's title uses the project name entered at upload (v2.1z-be) instead of
the working folder's name (`project`), read from the run folder (`control/request.json` in Job modes, the journal's
`web_run` line in-process). A direct CLI run keeps today's title. Fakes only (no IVAS, Maven, network, LLM, KB)."""
from __future__ import annotations

from sbmig.core.workspace import Workspace
from sbmig.report.markdown import render_markdown
from sbmig.web.contract import RequestRecord

from .test_remediation_flow import ivas_env, one_fix, orch_for, rem_cfg, upload  # noqa: F401 — ivas_env is a fixture

WEB_ID = "shop-api-20261120-101010-abc123"


def run(cfg, tmp_path, run_id, prepare=None, name="app.zip"):
    z = upload(tmp_path, name=name)
    ws = Workspace.from_input(z, tmp_path / "out", run_id=run_id)
    if prepare is not None:
        prepare(ws)
    report = orch_for(rem_cfg(cfg), ws, one_fix()).run(flow="remediation")
    return ws, report


def job_request(project):
    def prep(ws):
        RequestRecord(run_id=ws.run_id, accepted_at="2026-11-20T10:10:10Z", upload="source.zip", configs=[],
                      zip_sha256="0" * 64, config_digests={}, sbmig_version="test", mode="k8s-job",
                      flow="remediation", project=project).write(ws.run_dir)
    return prep


def web_run(project):
    def prep(ws):
        ws.journal({"event": "web_run", "flow": "remediation", "project": project})
    return prep


def rem_title(ws):
    return (ws.run_dir / "remediation" / "report.md").read_text(encoding="utf-8").splitlines()[0]


def test_job_mode_titles_use_the_entered_project(cfg, tmp_path, ivas_env):  # noqa: F811
    ws, report = run(cfg, tmp_path, WEB_ID, job_request("Shop_Api"), name="source.zip")
    assert report.project_name == "Shop_Api" and report.run_id == WEB_ID
    assert render_markdown(report, "ko").splitlines()[0] == "# 취약점 조치 보고서 — Shop_Api"
    assert render_markdown(report, "en").splitlines()[0] == "# Vulnerability remediation report — Shop_Api"
    assert rem_title(ws) == "# 취약점 조치 결과 — Shop_Api"


def test_in_process_titles_use_the_entered_project(cfg, tmp_path, ivas_env):  # noqa: F811
    ws, report = run(cfg, tmp_path, WEB_ID, web_run("shop-api"), name="uploaded-file.zip")
    assert report.project_name == "shop-api"
    assert rem_title(ws) == "# 취약점 조치 결과 — shop-api"


def test_web_run_without_a_name_uses_the_run_id_prefix(cfg, tmp_path, ivas_env):  # noqa: F811
    _, report = run(cfg, tmp_path, "legacy-20261120-101010-abc123", web_run(None))
    assert report.project_name == "legacy"


def test_direct_cli_run_keeps_todays_titles(cfg, tmp_path, ivas_env):  # noqa: F811
    ws, report = run(cfg, tmp_path, "run-20261120-101010-abc123")
    assert report.project_name == ws.root.name == "project"
    assert rem_title(ws) == "# 취약점 조치 결과 — app.zip"
