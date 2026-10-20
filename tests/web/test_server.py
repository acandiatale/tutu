"""`sbmig serve`: upload a zip, a (fake) run subprocess writes the run directory, the SSE stream relays it."""
from __future__ import annotations

import http.client
import io
import json
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from sbmig.web import RunManager, make_server
from sbmig.web.server import RUN_ID_RE, _new_lines

FAKE_RUN = r'''
import json, sys, time
from pathlib import Path
out, rid = Path(sys.argv[1]), sys.argv[2]
rd = out / rid; rd.mkdir(parents=True)
print("console line 1", flush=True)
with open(rd / "sbmig.log", "a") as lf, open(rd / "journal.jsonl", "a") as jf:
    lf.write("2026-10-20 10:00:00,000 INFO    sbmig.cli: sbmig run x\n"); lf.flush()
    jf.write(json.dumps({"ts": "2026-10-20T10:00:00+00:00", "run_id": rid, "event": "phase_start",
                         "phase": "jdk21"}) + "\n"); jf.flush()
    time.sleep(0.3)
    lf.write("2026-10-20 10:00:01,000 DEBUG   sbmig.fix.client: llm request role=fix\n")
    lf.flush()
    jf.write(json.dumps({"ts": "2026-10-20T10:00:01+00:00", "run_id": rid, "event": "build", "ok": True}) + "\n")
    jf.flush()
(rd / "report.md").write_text("# report\n\nBUILD SUCCESS\n")
(rd / "report.json").write_text(json.dumps({"success": True, "blocked": [], "unresolved": [],
                                             "llm_usage": {"calls": 1}}))
print("console line 2", flush=True)
sys.exit(0)
'''


def fake_command(script: Path):
    def build(spec):
        return [sys.executable, "-I", str(script), str(spec.out_dir), spec.run_id, *spec.configs]
    return build


@pytest.fixture
def server(tmp_path):
    script = tmp_path / "fake_run.py"
    script.write_text(FAKE_RUN)
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "artifacts.local-e2e.yml").write_text("x: 1\n")
    (repo / "config" / "default.yml").write_text("x: 1\n")
    mgr = RunManager(tmp_path / "out", repo_root=repo, command=fake_command(script))
    srv = make_server("127.0.0.1", 0, mgr)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, mgr
    srv.shutdown()
    srv.server_close()


def _conn(srv):
    return http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("proj/pom.xml", "<project/>")
    return buf.getvalue()


def _sse(srv, run_id: str) -> list[tuple[str, object]]:
    c = _conn(srv)
    c.request("GET", f"/api/runs/{run_id}/events")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    events, event = [], None
    while True:
        line = r.readline().decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            events.append((event, json.loads(line[6:])))
            if event == "end":
                break
    c.close()
    return events


def test_page_configs_and_empty_runs(server):
    srv, _ = server
    c = _conn(srv)
    c.request("GET", "/")
    r = c.getresponse()
    assert r.status == 200 and b"EventSource" in r.read()
    c.request("GET", "/api/configs")
    d = json.loads(c.getresponse().read())
    assert d["configs"] == ["artifacts.local-e2e.yml"]          # default.yml hidden
    c.request("GET", "/api/runs")
    assert json.loads(c.getresponse().read()) == {"runs": []}
    c.request("GET", "/api/runs/../etc")
    assert c.getresponse().status in (400, 404)
    c.request("GET", "/api/runs/nope")
    assert c.getresponse().status == 404


def test_upload_run_stream_and_files(server):
    srv, mgr = server
    c = _conn(srv)
    c.request("POST", "/api/runs?name=my%20proj.zip&configs=artifacts.local-e2e.yml&stop_after=spring",
              body=_zip_bytes(), headers={"Content-Type": "application/zip"})
    r = c.getresponse()
    assert r.status == 202
    rid = json.loads(r.read())["run_id"]
    assert RUN_ID_RE.match(rid)
    spec = mgr.specs[rid]
    assert spec.configs == ["config/artifacts.local-e2e.yml"] and spec.stop_after == "spring"
    assert spec.zip_path.name.endswith("my_proj.zip")

    events = _sse(srv, rid)
    kinds = [k for k, _ in events]
    assert kinds[0] == "status" and kinds[-1] == "end"
    journal = [d for k, d in events if k == "journal"]
    assert [j["event"] for j in journal] == ["phase_start", "build"]
    logs = [d for k, d in events if k == "log"]
    assert any("llm request" in ln for ln in logs)
    console = [d for k, d in events if k == "console"]
    assert console == ["console line 1", "console line 2"]
    report = [d for k, d in events if k == "report"]
    assert report and "BUILD SUCCESS" in report[0]
    final = [d for k, d in events if k == "status"][-1]
    assert final["state"] == "finished" and final["exit_code"] == 0 and final["success"] is True \
        and final["llm_calls"] == 1 and "report.md" in final["files"]

    c.request("GET", f"/api/runs/{rid}/files/report.md")
    r = c.getresponse()
    assert r.status == 200 and b"BUILD SUCCESS" in r.read()
    c.request("GET", f"/api/runs/{rid}/files/result.zip")
    assert c.getresponse().status == 404                      # the fake run does not produce one
    c.request("GET", f"/api/runs/{rid}/files/../../secret")
    assert c.getresponse().status in (400, 404)
    c.request("GET", "/api/runs")
    runs = json.loads(c.getresponse().read())["runs"]
    assert runs[0]["run_id"] == rid and runs[0]["state"] == "finished"


def test_rejects_non_zip_and_unknown_config(server):
    srv, _ = server
    c = _conn(srv)
    c.request("POST", "/api/runs?name=a.zip", body=b"not a zip", headers={"Content-Type": "application/zip"})
    assert c.getresponse().status == 400
    c.request("POST", "/api/runs?name=a.zip&configs=missing.yml", body=_zip_bytes())
    r = c.getresponse()
    assert r.status == 400 and "unknown config" in json.loads(r.read())["error"]


def test_new_lines_waits_for_complete_lines(tmp_path):
    p = tmp_path / "f.log"
    offsets: dict = {}
    p.write_bytes(b"a\nb")
    assert _new_lines(p, offsets) == ["a"]
    p.write_bytes(b"a\nb\n")
    assert _new_lines(p, offsets) == ["b"]
    assert _new_lines(p, offsets) == []
    assert _new_lines(tmp_path / "missing", offsets) == []
