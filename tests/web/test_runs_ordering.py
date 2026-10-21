"""Round 9: the SSE stream merges journal events and log lines by timestamp (the UI showed a finished build's
FAILED line after the next build's start line)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sbmig.web.runs import RunManager, _event_time


def _local(dt_utc: datetime) -> str:
    return dt_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S") + ",000"


def test_event_time_parses_both_sources():
    t = datetime(2026, 10, 21, 0, 35, 5, tzinfo=timezone.utc)
    assert _event_time("journal", {"ts": t.isoformat()}) == t.timestamp()
    assert abs(_event_time("log", _local(t) + " INFO sbmig.build: build test-compile: FAILED (3 errors)") - t.timestamp()) < 1e-6
    assert _event_time("log", "    at com.example.Foo") is None and _event_time("journal", {"event": "x"}) is None


def test_stream_orders_journal_and_log_by_time(tmp_path: Path):
    out = tmp_path / "out"
    rd = out / "web-1"
    rd.mkdir(parents=True)
    t0 = datetime(2026, 10, 21, 0, 35, 5, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)
    # journal: the build event of build 1 (t0) and an LLM call at t1
    (rd / "journal.jsonl").write_text(json.dumps({"ts": t0.isoformat(), "event": "build", "goal": "test-compile", "ok": False})
                                       + "\n" + json.dumps({"ts": t1.isoformat(), "event": "llm_fix"}) + "\n",
                                       encoding="utf-8")
    # log: FAILED line (t0), its continuation line (no timestamp), the next build's start (t1 + 0.5 s)
    (rd / "sbmig.log").write_text(_local(t0) + " INFO sbmig.build: build test-compile: FAILED (3 errors, 12.0s)\n"
                                  "    detail line without timestamp\n"
                                  + _local(t1 + timedelta(milliseconds=500)).replace(",000", ",500")
                                  + " INFO sbmig.build: mvn test-compile (cwd=.)\n", encoding="utf-8")
    (rd / "report.json").write_text("{}", encoding="utf-8")        # finished on disk → one drain, then end
    mgr = RunManager(out, tmp_path)
    kinds = [(k, (d.get("event") if isinstance(d, dict) else d[:120])) for k, d in mgr.events("web-1")
             if k in ("journal", "log")]
    assert kinds == [
        ("journal", "build"),
        ("log", kinds[1][1]),            # FAILED line (same second as the build event, after it: file position)
        ("log", kinds[2][1]),            # its continuation line keeps its place
        ("journal", "llm_fix"),
        ("log", kinds[4][1]),            # the next build's start line comes last
    ]
    assert "FAILED" in kinds[1][1] and "detail" in kinds[2][1] and "mvn test-compile" in kinds[4][1]
