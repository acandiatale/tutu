"""FastAPI web app: upload a zip, an in-process (fake) run writes the run directory, the SSE stream relays it.

No Maven, no LangGraph: `RunManager._run_thread_body` is replaced by a fake (or `sbmig.graph` by a fake module)."""
from __future__ import annotations

import io
import json
import logging
import shutil
import sys
import threading
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sbmig.cli import attach_file_log
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import RunManager, create_app
from sbmig.web.runs import RUN_ID_RE, _jsonable, _new_lines


from datetime import datetime, timezone


def _now_iso() -> str:
    """Journal timestamps like Workspace.journal (the web stream sorts each tick by them, round 9)."""
    return datetime.now(timezone.utc).isoformat()


def fake_body(self: RunManager, run) -> SimpleNamespace:
    """Stands in for the graph engine: two node updates, a journal, a log line via logging, the reports."""
    rd = run.run_dir
    logging.getLogger("sbmig.fake").debug("llm request role=fix")          # → this run's sbmig.log (thread filter)
    with open(rd / "journal.jsonl", "a", encoding="utf-8") as jf:
        jf.write(json.dumps({"ts": _now_iso(), "run_id": run.run_id, "event": "phase_start",
                             "phase": "jdk21"}) + "\n")
    self._push(run, "node", {"node": "intake", "update": {"run_id": run.run_id}})
    self._push(run, "node", {"node": "phase", "update": {"phase": "jdk21", "phases": ["spring"]}})
    with open(rd / "journal.jsonl", "a", encoding="utf-8") as jf:
        jf.write(json.dumps({"ts": _now_iso(), "run_id": run.run_id, "event": "build",
                             "ok": True}) + "\n")
    (rd / "report.md").write_text("# report\n\nBUILD SUCCESS\n", encoding="utf-8")
    (rd / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": [],
                                                "llm_usage": {"calls": 1}}), encoding="utf-8")
    return SimpleNamespace(success=True, blocked=[], flags=[])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(RunManager, "_run_thread_body", fake_body)
    return TestClient(create_app(tmp_path / "out", repo))


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/pom.xml", "<project/>")
    return buf.getvalue()


def _start(client: TestClient, query: str = "name=my%20proj.zip&configs=artifacts.local-e2e.yml") -> str:
    r = client.post(f"/api/runs?{query}", content=_zip_bytes(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    assert RUN_ID_RE.match(rid)
    return rid


def _sse(client: TestClient, run_id: str) -> list[tuple[str, object]]:
    events, event = [], None
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                events.append((event, json.loads(line[6:])))
                if event == "end":
                    break
    return events


def _manager(client: TestClient) -> RunManager:
    return client.app.state.manager


def test_page_static_configs_and_empty_runs(client):
    r = client.get("/")
    assert r.status_code == 200 and "EventSource" in r.text and 'id="graph"' in r.text
    assert client.get("/static/index.html").status_code == 200
    d = client.get("/api/configs").json()
    assert d["configs"] == ["artifacts.local-e2e.yml"]          # default.yml hidden
    assert client.get("/api/runs").json() == {"runs": []}
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404
    assert client.get("/api/runs/..%2Fetc").status_code in (400, 404)
    assert client.get("/api/runs/bad$id").status_code == 400


def test_upload_run_stream_and_files(client, tmp_path):
    rid = _start(client, "name=my%20proj.zip&configs=artifacts.local-e2e.yml&stop_after=spring&llm_provider=fake")
    run = _manager(client).runs[rid]
    assert run.configs == ["artifacts.local-e2e.yml"] and run.stop_after == "spring"
    assert run.zip_path.name.endswith("my_proj.zip") and run.zip_path.parent == tmp_path / "out" / "_uploads"
    assert run.orch.cfg.llm.provider == "fake"
    assert (tmp_path / "out" / rid / "project" / "pom.xml").is_file()

    events = _sse(client, rid)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "end" and kinds[-2] == "status" and kinds[-3] == "report"
    assert {"node", "journal", "log"} <= set(kinds)
    assert max(kinds.index("node"), kinds.index("journal"), kinds.index("log")) < kinds.index("report")
    nodes = [d for k, d in events if k == "node"]
    assert [n["node"] for n in nodes] == ["intake", "phase"] and nodes[1]["update"]["phase"] == "jdk21"
    assert [j["event"] for k, j in events if k == "journal"][-2:] == ["phase_start", "build"]   # after "intake"
    logs = [d for k, d in events if k == "log"]
    assert any("llm request role=fix" in ln for ln in logs)
    report = [d for k, d in events if k == "report"]
    assert "BUILD SUCCESS" in report[0]
    final = events[-2][1]
    assert final["state"] == "finished" and final["exit_code"] == 0 and final["success"] is True \
        and final["llm_calls"] == 1 and "report.md" in final["files"] and final["node"] == "phase"
    assert "error" not in kinds

    st = client.get(f"/api/runs/{rid}").json()
    assert st["state"] == "finished" and st["exit_code"] == 0
    r = client.get(f"/api/runs/{rid}/files/report.md")
    assert r.status_code == 200 and "BUILD SUCCESS" in r.text
    assert client.get(f"/api/runs/{rid}/files/result.zip").status_code == 404     # the fake run makes none
    assert client.get(f"/api/runs/{rid}/files/..%2F..%2Fsecret").status_code in (400, 404)
    assert client.get(f"/api/runs/{rid}/files/project").status_code == 404        # not whitelisted
    (tmp_path / "out" / rid / "result.zip").write_bytes(_zip_bytes())
    r = client.get(f"/api/runs/{rid}/files/result.zip")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["run_id"] == rid and runs[0]["state"] == "finished"
    assert client.post(f"/api/runs/{rid}/stop").json() == {"stopped": False}      # already finished


def test_run_log_holds_only_its_own_thread(client, tmp_path):
    rid = _start(client)
    _sse(client, rid)
    text = (tmp_path / "out" / rid / "sbmig.log").read_text()
    assert "llm request role=fix" in text and "log file" in text
    assert not any(getattr(h, "sbmig_thread", None) for h in logging.getLogger("sbmig").handlers)   # detached


def test_rejects_bad_requests(client):
    r = client.post("/api/runs?name=a.zip", content=b"not a zip")
    assert r.status_code == 400 and "not a zip" in r.json()["error"]
    assert client.post("/api/runs?name=a.zip", content=b"").status_code == 400
    r = client.post("/api/runs?name=a.zip&configs=missing.yml", content=_zip_bytes())
    assert r.status_code == 400 and "unknown config" in r.json()["error"]
    r = client.post("/api/runs?name=a.zip&configs=../secret.yml", content=_zip_bytes())
    assert r.status_code == 400
    r = client.post("/api/runs?name=a.zip&stop_after=nowhere", content=_zip_bytes())
    assert r.status_code == 400 and "stop_after" in r.json()["error"]
    assert client.get("/api/runs").json() == {"runs": []}


def test_worker_error_is_streamed(client, monkeypatch):
    def boom(self, run):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(RunManager, "_run_thread_body", boom)
    rid = _start(client)
    events = _sse(client, rid)
    errors = [d for k, d in events if k == "error"]
    assert errors == [{"message": "RuntimeError: engine exploded"}]
    final = events[-2][1]
    assert final["state"] == "finished" and final["exit_code"] == 3 and "engine exploded" in final["error"]


def test_stream_graph_updates_and_stop_between_nodes(tmp_path, repo, monkeypatch):
    """The real worker body over a fake `sbmig.graph.stream_graph`: updates become node events; a stop request is
    honoured after the next update, flags the report aborted and closes the graph iterator."""
    go, first_sent, closed = threading.Event(), threading.Event(), []

    def stream_graph(orch, stop_after=None, dry_run=False):
        orch.report = SimpleNamespace(flags=[], manual_followups=[], finished_at=None, success=False, blocked=[])
        try:
            yield "intake", {"run_id": orch.ws.run_id, "phases": []}
            first_sent.set()
            go.wait(10)
            yield "analyze", {"summary": {"case_type": "pure-java"}, "path": Path("x")}
            yield "plan", {"phases": ["jdk21"]}
        finally:                                  # like sbmig.graph: closing early still finalizes the report
            closed.append(list(orch.report.flags))
            orch.report.finished_at = "now"

    monkeypatch.setitem(sys.modules, "sbmig.graph", types.SimpleNamespace(stream_graph=stream_graph))
    mgr = RunManager(tmp_path / "out", repo)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], stop_after="plan")
    assert first_sent.wait(10)
    assert mgr.stop(rid) is True
    go.set()
    events = list(mgr.events(rid))
    nodes = [d for k, d in events if k == "node"]
    assert [n["node"] for n in nodes] == ["intake", "analyze"]                    # stopped after "analyze"
    assert nodes[1]["update"] == {"summary": {"case_type": "pure-java"}, "path": "x"}
    assert closed == [["aborted"]]                                                 # flagged before close()
    run = mgr.runs[rid]
    assert any("stopped by user" in f for f in run.report.manual_followups)
    final = events[-2][1]
    assert final["stop_requested"] is True and final["exit_code"] == 3            # aborted


def test_inline_fallback_without_graph(tmp_path, repo, monkeypatch):
    from sbmig.loop import Orchestrator

    calls = []

    def fake_run(self, stop_after=None, dry_run=False):
        calls.append(stop_after)
        return SimpleNamespace(success=False, blocked=[], flags=[])

    monkeypatch.setitem(sys.modules, "sbmig.graph", None)                       # import → ImportError
    monkeypatch.setattr(Orchestrator, "run", fake_run)
    mgr = RunManager(tmp_path / "out", repo)
    rid = mgr.start(_zip_bytes(), "p.zip", ["artifacts.local-e2e.yml"], stop_after="analyze")
    events = list(mgr.events(rid))
    assert [c.value for c in calls] == ["analyze"]
    assert "node" not in [k for k, _ in events]
    assert events[-2][1]["exit_code"] == 2                                        # finished, not successful


def test_runs_of_an_earlier_process_are_listed_and_replayed(tmp_path, repo):
    rd = tmp_path / "out" / "old-run"
    rd.mkdir(parents=True)
    (rd / "journal.jsonl").write_text(json.dumps({"event": "build", "ok": False}) + "\nnot json\n")
    (rd / "report.md").write_text("# old\n")
    (rd / "report.json").write_text(json.dumps({"success": False, "blocked": [{}], "unresolved": []}))
    mgr = RunManager(tmp_path / "out", repo)
    (row,) = mgr.list_runs()
    assert row["run_id"] == "old-run" and row["state"] == "finished" and row["exit_code"] is None \
        and row["blocked"] == 1
    kinds = [k for k, _ in mgr.events("old-run")]
    assert kinds == ["status", "journal", "log", "report", "status", "end"]


def test_attach_file_log_filters_other_threads(tmp_path):
    root = logging.getLogger("sbmig")
    lg = logging.getLogger("sbmig.test_web_thread")
    path = tmp_path / "run.log"
    ready, done = threading.Event(), threading.Event()
    out: dict = {}

    def worker():
        out["path"] = attach_file_log(path, thread_id=threading.get_ident())
        assert attach_file_log(path, thread_id=threading.get_ident()) == out["path"]     # idempotent
        lg.info("from the run thread")
        ready.set()
        done.wait(10)

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert ready.wait(10)
        lg.info("from another thread")
        done.set()
        t.join(10)
        handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)
                    and Path(h.baseFilename) == out["path"]]
        assert len(handlers) == 1
        for h in handlers:
            h.flush()
        text = path.read_text()
        assert "from the run thread" in text and "from another thread" not in text
    finally:
        for h in [h for h in root.handlers if isinstance(h, logging.FileHandler)
                  and Path(h.baseFilename) == path.resolve()]:
            root.removeHandler(h)
            h.close()


def test_jsonable_and_new_lines(tmp_path):
    from sbmig.core.models import Phase

    assert _jsonable({"phase": Phase.JDK21, "s": {1}, "p": Path("a")}) == {"phase": "jdk21", "s": [1], "p": "a"}
    big = _jsonable({"k": "x" * 30_000})
    assert big["keys"] == ["k"] and big["_truncated"] > 30_000
    p = tmp_path / "f.log"
    offsets: dict = {}
    p.write_bytes(b"a\nb")
    assert _new_lines(p, offsets) == ["a"]
    p.write_bytes(b"a\nb\n")
    assert _new_lines(p, offsets) == ["b"]
    assert _new_lines(p, offsets) == []
    assert _new_lines(tmp_path / "missing", offsets) == []


def test_shutdown_ends_open_streams_and_stops_running_runs(tmp_path, repo, monkeypatch):
    """Server shutdown (lifespan): the open event stream of a running run ends with `end` (reason server shutdown)
    instead of holding the uvicorn connection open, the run is asked to stop, live Maven children are killed."""
    import sbmig.web.runs as runs_mod
    started = threading.Event()

    def slow_body(self, run):
        started.set()
        run.cancel.wait(10)                       # a migration in progress: it ends when asked to stop
        return None

    monkeypatch.setattr(RunManager, "_run_thread_body", slow_body)
    killed: list[str] = []
    monkeypatch.setattr("sbmig.build.mvn.kill_live_processes", lambda reason="x": killed.append(reason) or 2)
    with TestClient(create_app(tmp_path / "out", repo)) as client:
        rid = _start(client)
        assert started.wait(5)
        mgr = _manager(client)
        events = mgr.events(rid)
        first = next(events)
        assert first[0] == "status" and first[1]["state"] == "running"
        info = mgr.shutdown()
        assert info["stopped"] == [rid] and info["killed"] == 2 and killed == ["server shutdown"]
        rest = list(events)                                           # ends promptly, no blocking poll loop
        assert rest[-1] == ("end", {"run_id": rid, "reason": "server shutdown"})
        run = mgr.runs[rid]
        assert run.cancel.is_set() and run.done.wait(5)
        assert mgr.closing.is_set()
    assert mgr.closing.is_set()                                       # the lifespan exit called shutdown() too
