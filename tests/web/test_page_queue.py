"""v2.1z-ae (2026-11-10): up to N build Jobs at a time, the rest wait — the page (`sbmig/web/static/index.html`) tells a waiting
person that they wait, how many are ahead and that the run starts by itself, from `parallel` / `queue` in the status answers; a
notice on the upload card says when every slot is taken. Static checks on the page source (the browser behaviour was checked
with a headless Chromium when the page changed); `tests/web/test_app.py` already pins the offline rule for `src="http` /
`href="http` / `@import` / web fonts."""
from __future__ import annotations

import re
from pathlib import Path

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_page_reads_parallel_and_queue() -> None:
    for token in (".parallel", ".queue", ".position", ".ahead", ".slots", ".running", ".waiting"):
        assert token in PAGE, token
    # the upload card notice: a polite live region, driven by /api/runs (polled) and status events
    assert re.search(r'<p\b[^>]*\bid="busynote"[^>]*\brole="status"', PAGE) or re.search(r'<p\b[^>]*\brole="status"[^>]*\bid="busynote"', PAGE)
    assert "setParallel(d.parallel)" in PAGE and "setParallel(st.parallel)" in PAGE


def test_waiting_texts() -> None:
    for text in ("대기 중 — 다음 차례입니다", "대기 중 — 앞에 ${q.ahead}건이 기다립니다", "대기 ${q.position}번",
                 "동시에 ${p.slots}건까지 이관합니다", "이 창을 닫아도 차례는 유지됩니다",
                 "한 번에 한 프로젝트만 옮깁니다. 차례가 오면 자동으로 시작합니다.",
                 "지금 올리면 대기 ${p.waiting + 1}번째로 기다렸다가 자동으로 시작합니다.",
                 "자리가 나면 자동으로 시작합니다.", "차례가 오면 자동으로 시작합니다."):
        assert text in PAGE, text
    # older servers (no parallel / queue): today's sentences stay as the fallback
    assert "대기 중 — 앞의 실행이 끝나면 시작합니다" in PAGE and "올렸습니다. 앞의 실행이 끝나면 시작합니다." in PAGE


def test_one_at_a_time_wording_gone() -> None:
    for old in ("지식 베이스를 함께 쓰기 때문", "다음 이관은 기다립니다", "끝난 것을 확인한 뒤 다음 이관을 시작합니다"):
        assert old not in PAGE, old


def test_no_external_url_in_css_or_single_quoted_attributes() -> None:
    assert not re.search(r"""url\(\s*['"]?\s*(?:https?:)?//""", PAGE)
    assert not re.search(r"""\b(?:src|href)\s*=\s*'(?:https?:)?//""", PAGE)


def _script() -> str:
    return PAGE.split("<script>", 1)[1].split("</script>", 1)[0]


def test_reconnects_after_server_restart() -> None:
    """An `end` with a `reason` (web-pod restart) or a dropped connection on an unfinished run re-attaches to the same run with
    backoff instead of freezing the card; a run the server no longer knows (404 a few times) stops the retries."""
    js = _script()
    assert "d.reason" in js and "lost(runId, 'shutdown')" in js and "lost(runId, 'network')" in js
    assert re.search(r"RETRY_MIN_MS\s*=\s*2000", js) and re.search(r"RETRY_MAX_MS\s*=\s*30000", js) and "RETRY_GONE" in js
    assert "Math.min(retry.delay * 2, RETRY_MAX_MS)" in js
    assert "r.status === 404" in js and "attach(runId, true)" in js
    assert "if (!resume) cancelRetry();" in js                      # picking another run stops the retry
    assert "lastState === 'finished'" in js                         # a finished run is never re-attached after its stream ends
    for text in ("서버가 다시 시작되는 중입니다", "서버와 연결이 끊겼습니다", "다시 연결하는 중…",
                 "자동으로 다시 연결합니다 (${Math.round(retry.delay / 1000)}초 뒤).",
                 "서버가 이 실행을 더 이상 알지 못합니다", "다시 연결하지 않습니다. 지난 실행 목록에서 확인해 주세요."):
        assert text in js, text
    # the old dead end is gone: no "pick it again in the history to re-attach" message
    assert "지난 실행에서 다시 고르면 다시 붙습니다" not in js


def test_stop_is_bound_to_its_run_and_reads_stopped() -> None:
    js = _script()
    block = js.split("$('stop').addEventListener('click'", 1)[1].split("async function refreshStatus", 1)[0]
    assert "const runId = current;" in block and "current === runId" in block
    assert "d.stopped === false" in block and "이미 끝난 이관입니다. 중단할 것이 없습니다." in block
    assert "refreshStatus(runId)" in block


def test_run_list_answers_in_order_and_coalesced() -> None:
    js = _script()
    assert "RUNS_COALESCE_MS = 300" in js
    assert "const seq = ++runsSeq;" in js and "if (seq !== runsSeq) return;" in js
