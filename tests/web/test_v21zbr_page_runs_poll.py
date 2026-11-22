"""The run list's 15-second refresh rests while the tab is hidden (INTERFACES "v2.1z-br", visible effect 5).

`startRunsPoll` and `loadRuns` are cut out of `index.html` and run by node with a stubbed `document` (`hidden`,
`addEventListener`) and stubbed timers; `fetchRuns` is a counter. A hidden tab fetches nothing on the interval; the
tab becoming visible fetches once at once and the same interval goes on. The stream of an open run is not part of it."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")


def _fn(signature: str) -> str:
    return "function " + signature + SCRIPT.split(f"function {signature}", 1)[1].split("\n  }\n", 1)[0] + "\n  }\n"


HARNESS = r"""
const RUNS_COALESCE_MS = 300;
let runsTimer = null, fetched = 0;
function fetchRuns() { fetched++; }
// timers: setTimeout runs only when the test says so; setInterval is recorded, never run by itself
let timeouts = [], intervals = [];
function setTimeout(fn, ms) { timeouts.push({ fn, ms }); return timeouts.length; }
function setInterval(fn, ms) { intervals.push({ fn, ms }); return 100 + intervals.length; }
function clearInterval() { throw new Error('the interval is never cleared'); }
function flush() { const t = timeouts; timeouts = []; t.forEach((x) => x.fn()); }
const listeners = {};
const doc = { hidden: false, addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); } };
function show(hidden) { doc.hidden = hidden; (listeners.visibilitychange || []).forEach((fn) => fn({ type: 'visibilitychange' })); }
__CODE__
const log = [];
const id = startRunsPoll(doc);
const tick = () => intervals[0].fn();
log.push({ step: 'start', id, intervals: intervals.map((x) => x.ms), listeners: Object.keys(listeners) });
tick(); flush(); log.push({ step: 'visible tick', fetched, pending: timeouts.length });
show(true); flush(); log.push({ step: 'hidden', fetched, pending: timeouts.length });
tick(); tick(); tick(); log.push({ step: 'three hidden ticks', fetched, pending: timeouts.length });
flush();
show(false); log.push({ step: 'visible again', pending: timeouts.map((x) => x.ms) });
flush(); log.push({ step: 'after coalesce', fetched });
tick(); flush(); log.push({ step: 'next tick', fetched, intervals: intervals.length });
process.stdout.write(JSON.stringify(log));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_hidden_tab_does_not_fetch_and_visible_again_fetches_once(tmp_path):
    code = "const RUNS_POLL_MS = 15000;\n" + _fn("loadRuns()") + _fn("startRunsPoll(doc)")
    js = tmp_path / "poll.js"
    js.write_text(HARNESS.replace("__CODE__", code), encoding="utf-8")
    p = subprocess.run([NODE, str(js)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    log = {e["step"]: e for e in json.loads(p.stdout)}
    assert log["start"]["intervals"] == [15000] and log["start"]["listeners"] == ["visibilitychange"]
    assert log["start"]["id"] == 101                                   # the interval id is returned
    assert log["visible tick"] == {"step": "visible tick", "fetched": 1, "pending": 0}
    assert log["hidden"]["fetched"] == 1 and log["hidden"]["pending"] == 0      # hiding fetches nothing
    assert log["three hidden ticks"]["fetched"] == 1 and log["three hidden ticks"]["pending"] == 0
    assert log["visible again"]["pending"] == [300]                    # one refresh at once (through the coalescing)
    assert log["after coalesce"]["fetched"] == 2
    assert log["next tick"] == {"step": "next tick", "fetched": 3, "intervals": 1}   # the same interval goes on


def test_page_starts_the_poll_and_nothing_else_changed():
    assert "const RUNS_POLL_MS = 15000;" in SCRIPT
    assert "loadConfigs(); loadRuns(); startRunsPoll(document);" in SCRIPT
    assert "setInterval(loadRuns" not in SCRIPT and SCRIPT.count("setInterval(") == 1
    body = _fn("startRunsPoll(doc)")
    # the poll reads only the tab state; the open run's stream and the other timers are not in it
    for word in ("EventSource", "es.", "clearInterval", "clearTimeout", "fetchRuns", "document"):
        assert word not in body, word
