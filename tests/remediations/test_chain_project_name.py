"""`RemediationState.name` (the title 「취약점 조치 결과 — {name}」) of a web run is the project name entered at upload
(v2.1z-be): remediation-only runs fall back to the web run id's prefix, then to the file name; a chained remediation
uses the entered name or keeps `source.zip`; a direct CLI run keeps today's value. Fakes only."""
from __future__ import annotations

import json

import pytest

from sbmig.remediations import chain
from sbmig.remediations.models import FAILED, SUCCEEDED, RoundPlan
from sbmig.web.contract import RequestRecord

from .fakes import (
    POM,
    FakeScanner,
    ScriptedPlanner,
    deterministic_zip,
    finding,
    make_cfg,
    make_components,
    proposal,
    scan_result,
)

ENV = {"IVAS_BASE_URL": "https://ivas.invalid", "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}
WEB_ID = "shop-api-20261120-101010-abc123"
FILES = {"pom.xml": POM, "src/main/java/App.java": "class App {}\n"}


@pytest.fixture(autouse=True)
def ivas_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def comps():
    scan = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    return make_components(scan=scan, plan=plan)


def upload(tmp_path, files=FILES, name="shop.zip"):
    z = tmp_path / "up" / name
    z.parent.mkdir(parents=True, exist_ok=True)
    z.write_bytes(deterministic_zip({k: v.encode("utf-8") for k, v in files.items()}))
    return z


def job_request(run_dir, project="shop-api"):
    RequestRecord(run_id=run_dir.name, accepted_at="2026-11-20T10:10:10Z", upload="source.zip", configs=[],
                  zip_sha256="0" * 64, config_digests={}, sbmig_version="test", mode="k8s-job", flow="remediation",
                  project=project).write(run_dir)


def web_run_line(run_dir, project="shop-api"):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "web_run", "project": project}) + "\n")


def only(run_dir, source):
    return chain.run_only(run_dir, source, make_cfg(), run_id=run_dir.name, journal=lambda e: None,
                          stop=lambda: False, components=comps())


def after(run_dir, root):
    return chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id=run_dir.name,
                                     journal=lambda e: None, stop=lambda: False, components=comps())


def tree(run_dir):
    root = run_dir / "project"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# ------------------------------------------------------------------------------------- remediation only
def test_job_mode_upload_is_titled_with_the_entered_project(tmp_path):
    rd = tmp_path / WEB_ID
    job_request(rd, "Shop_Api")
    z = rd / "source.zip"
    z.write_bytes(upload(tmp_path).read_bytes())
    st = only(rd, z)
    assert st.status == SUCCEEDED and st.name == "Shop_Api" and st.id == WEB_ID
    assert "# 취약점 조치 결과 — Shop_Api" in chain.chain_paths(rd).report_md.read_text(encoding="utf-8")


def test_in_process_upload_is_titled_with_the_entered_project(tmp_path):
    rd = tmp_path / WEB_ID
    web_run_line(rd, "shop-api")
    st = only(rd, upload(tmp_path, name="whatever-file.zip"))
    assert st.status == SUCCEEDED and st.name == "shop-api"


def test_web_run_without_a_name_uses_the_run_id_prefix(tmp_path):
    rd = tmp_path / WEB_ID
    job_request(rd, None)
    assert only(rd, upload(tmp_path)).name == "shop-api"


def test_direct_cli_run_keeps_the_file_name(tmp_path):
    rd = tmp_path / "run-20261120-101010-abc123"
    assert only(rd, upload(tmp_path, name="app.zip")).name == "app.zip"


def test_intake_rejection_carries_the_entered_project(tmp_path):
    rd = tmp_path / WEB_ID
    job_request(rd, "shop-api")
    st = only(rd, upload(tmp_path, files={"README.txt": "no pom"}))
    assert st.status == FAILED and st.reason == "no_pom" and st.name == "shop-api"


# ------------------------------------------------------------------------------------- chained
def test_chained_remediation_uses_the_entered_project(tmp_path):
    rd = tmp_path / WEB_ID
    job_request(rd, "shop-api")
    st = after(rd, tree(rd))
    assert st.status == SUCCEEDED and st.name == "shop-api"


def test_chained_remediation_without_an_entered_name_keeps_source_zip(tmp_path):
    rd = tmp_path / WEB_ID
    job_request(rd, None)                         # a web run without a name: no run-id fallback for the chained case
    assert after(rd, tree(rd)).name == "source.zip"
    rd2 = tmp_path / "run-20261120-101010-abc123"  # a CLI run
    assert after(rd2, tree(rd2)).name == "source.zip"
