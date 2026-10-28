"""v2.1i switches over the web app: `POST /api/runs?fix_mode=edits&no_kb=1` → `RunManager.start_file(..., fix_mode=,
no_kb=)` → the `fix.mode` override and a NullKnowledgeBase; the run record carries `fix_mode` and `kb`.

No Maven, no LangGraph: `RunManager._run_thread_body` is the fake of test_app."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sbmig.loop import NullKnowledgeBase
from sbmig.web import RunManager, create_app
from sbmig.web.runs import parse_fix_mode, parse_flag

from .test_app import (
    _manager,
    _sse,
    _start,
    _zip_bytes,
    fake_body,
    repo,  # noqa: F401 - fixture
)


@pytest.fixture
def client(tmp_path: Path, repo: Path, monkeypatch) -> TestClient:  # noqa: F811 - `repo` is the fixture
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    return TestClient(create_app(tmp_path / "out", repo))


BASE = "name=p.zip&configs=artifacts.local-e2e.yml&llm_provider=fake"


def test_edits_and_kb_off_reach_the_run_and_its_record(client, tmp_path):
    rid = _start(client, BASE + "&fix_mode=edits&no_kb=1")
    run = _manager(client).runs[rid]
    assert run.fix_mode == "edits" and run.no_kb is True
    assert run.orch.cfg.fix.mode == "edits" and run.orch.cfg.llm.provider == "fake"     # both overrides merged
    assert isinstance(run.orch.kb, NullKnowledgeBase)
    events = _sse(client, rid)
    final = events[-2][1]
    assert final["state"] == "finished" and final["fix_mode"] == "edits" and final["kb"] == "off"

    rows = client.get("/api/runs").json()["runs"]
    assert rows[0]["run_id"] == rid and rows[0]["fix_mode"] == "edits" and rows[0]["kb"] == "off"
    one = client.get(f"/api/runs/{rid}").json()
    assert one["fix_mode"] == "edits" and one["kb"] == "off"

    rd = tmp_path / "out" / rid
    log = (rd / "sbmig.log").read_text(encoding="utf-8")
    assert f"run {rid}: fix_mode=edits kb=off" in log
    journal = [json.loads(ln) for ln in (rd / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    web = [j for j in journal if j["event"] == "web_run"]      # right after the workspace's own intake record
    assert len(web) == 1 and web[0]["fix_mode"] == "edits" and web[0]["kb"] == "off"
    assert journal.index(web[0]) <= 1
    saved = json.loads((rd / "web-status.json").read_text(encoding="utf-8"))
    assert saved["fix_mode"] == "edits" and saved["kb"] == "off"


def test_defaults_are_unchanged(client):
    rid = _start(client, BASE)
    run = _manager(client).runs[rid]
    assert run.fix_mode == "script" and run.no_kb is False
    assert run.orch.cfg.fix.mode == "script" and not isinstance(run.orch.kb, NullKnowledgeBase)
    _sse(client, rid)
    row = client.get("/api/runs").json()["runs"][0]
    assert row["fix_mode"] == "script" and row["kb"] == "on"


@pytest.mark.parametrize("query", ["fix_mode=script&no_kb=0", "fix_mode=&no_kb=", "no_kb=false", "no_kb=FALSE"])
def test_explicit_defaults(client, query):
    rid = _start(client, BASE + "&" + query)
    run = _manager(client).runs[rid]
    assert run.orch.cfg.fix.mode == "script" and run.no_kb is False


def test_no_kb_true_spelling(client):
    rid = _start(client, BASE + "&no_kb=true")
    assert _manager(client).runs[rid].no_kb is True


@pytest.mark.parametrize("query, word", [("fix_mode=patch", "fix_mode"), ("fix_mode=edits&no_kb=maybe", "no_kb"),
                                         ("no_kb=2", "no_kb")])
def test_bad_switch_values_are_400_and_start_nothing(client, tmp_path, query, word):
    r = client.post(f"/api/runs?{BASE}&{query}", content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 400 and word in r.json()["error"]
    assert client.get("/api/runs").json() == {"runs": []}
    uploads = tmp_path / "out" / "_uploads"          # rejected before the body was read: no upload file at all
    assert not uploads.exists() or not any(uploads.iterdir())


def test_start_file_validates_and_overrides(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    mgr = RunManager(tmp_path / "out", repo)
    with pytest.raises(ValueError, match="fix_mode"):
        mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], fix_mode="bogus")
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], None, "fake", fix_mode="edits", no_kb=True)
    run = mgr.runs[rid]
    run.done.wait(10)
    assert run.orch.cfg.fix.mode == "edits" and isinstance(run.orch.kb, NullKnowledgeBase)


def test_evicted_and_older_runs_answer_from_files(tmp_path, repo):  # noqa: F811
    mgr = RunManager(tmp_path / "out", repo)
    # a run of an earlier server process: web-status.json with the switches
    rd = tmp_path / "out" / "web-a"
    rd.mkdir(parents=True)
    (rd / "web-status.json").write_text(json.dumps({"exit_code": 0, "fix_mode": "edits", "kb": "off"}))
    st = mgr.status("web-a")
    assert st["fix_mode"] == "edits" and st["kb"] == "off"
    # a run from before v2.1i: only report.json (RunReport.fix_mode), else the defaults
    rd = tmp_path / "out" / "web-b"
    rd.mkdir()
    (rd / "report.json").write_text(json.dumps({"success": True, "fix_mode": "edits"}))
    st = mgr.status("web-b")
    assert st["fix_mode"] == "edits" and st["kb"] == "on"
    (tmp_path / "out" / "web-c").mkdir()
    st = mgr.status("web-c")
    assert st["fix_mode"] == "script" and st["kb"] == "on"


def test_parsers():
    assert parse_fix_mode(None) is None and parse_fix_mode("") is None and parse_fix_mode("EDITS") == "edits"
    with pytest.raises(ValueError):
        parse_fix_mode("scripts")
    assert parse_flag(None) is False and parse_flag("1") is True and parse_flag("0") is False
    with pytest.raises(ValueError, match="no_kb"):
        parse_flag("x")


def test_page_has_the_two_switches(client):
    html = client.get("/").text
    assert 'id="fixedits"' in html and "LLM 직접 수정만 (스크립트 생성·재사용 없음)" in html
    assert 'id="nokb"' in html and "KB 사용 안 함 (저장 스크립트 재사용·기록 없음)" in html
    assert "q.set('fix_mode', 'edits')" in html and "q.set('no_kb', '1')" in html
