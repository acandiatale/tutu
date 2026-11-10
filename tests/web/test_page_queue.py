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
