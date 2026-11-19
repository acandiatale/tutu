"""The admin page (`sbmig/web/static/admin.html`, contract v2.1z-ao): token usage per project and the log ZIPs behind a
login. Static checks on the page source, like `test_page_remediation.py`; the browser check is done by hand with a fake
server that answers the admin API."""
from __future__ import annotations

import re
from pathlib import Path

import sbmig.web

STATIC = Path(sbmig.web.__file__).parent / "static"
PAGE = (STATIC / "admin.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
STYLE = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


def _function(name: str) -> str:
    m = re.search(r"\n  (?:async )?function " + name + r"\(.*?\n  }\n", SCRIPT, re.S)
    assert m, name
    return m.group(0)


def test_one_self_contained_file() -> None:
    # the company network is offline: nothing from another origin, no linked file
    assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", PAGE)
    assert "@import" not in STYLE and "url(http" not in PAGE
    assert "<script src" not in PAGE and 'rel="stylesheet"' not in PAGE
    assert PAGE.count("<script>") == 1 and PAGE.count("<style>") == 1


def test_same_tokens_as_the_main_page() -> None:
    main = (STATIC / "index.html").read_text(encoding="utf-8")
    for token in ("--blue: #1F5BD8", "--blue-deep: #0E2E6E", "--paper: #F4F7FC", "--line: #D5DEEE", "--ink: #13213A",
                  "--muted: #5E6E8C", "--radius: 12px", "--radius-s: 8px", '--sans: "Segoe UI", "Malgun Gothic"'):
        assert token in main and token in STYLE, token
    assert "font-variant-numeric: tabular-nums" in STYLE


def test_routes_are_the_contract_ones() -> None:
    assert "const API = '/admin/api';" in SCRIPT
    assert "API + '/session'" in SCRIPT and "API + '/login'" in SCRIPT and "API + '/logout'" in SCRIPT
    assert "API + '/usage'" in SCRIPT
    assert 'href="/admin/api/logs.zip"' in PAGE
    assert "${API}/runs/${encodeURIComponent(id)}/logs.zip" in _function("runsTable")
    # the main page opens a run by its hash
    assert 'href="/#${encodeURIComponent(id)}"' in _function("runsTable")


def test_login_messages_and_password_handling() -> None:
    assert 'type="password" id="pw"' in PAGE and ">들어가기</button>" in PAGE
    assert re.search(r'<form id="loginform"[^>]*>', PAGE) and 'type="submit"' in PAGE   # Enter submits
    assert "'비밀번호가 맞지 않습니다.'" in SCRIPT
    assert "'시도 횟수가 많습니다. 잠시 뒤에 다시 해 주세요.'" in SCRIPT
    assert "r.status === 429" in SCRIPT and "r.status === 401" in SCRIPT
    # the password goes from the field into the body, is never kept, stored or put in a URL
    assert "JSON.stringify({ password: pw.value })" in SCRIPT
    assert "pw.value = '';" in SCRIPT
    assert "localStorage" not in SCRIPT and "sessionStorage" not in SCRIPT and "document.cookie" not in SCRIPT
    assert not re.search(r"(?:let|const|var)\s+\w*(?:password|passwd)\w*\s*=", SCRIPT, re.I)


def test_401_anywhere_goes_back_to_login_keeping_the_filter() -> None:
    load = _function("load")
    assert "r.status === 401) { expired(); return; }" in load
    exp = _function("expired")
    assert "'세션이 끝났습니다. 다시 들어가 주세요.'" in exp
    assert "filter" not in exp                        # the filter text stays
    # the ZIP links ask the session first so an expired one does not land on a JSON error
    assert "a[data-zip]" in SCRIPT and "if (!ok) { expired(); return; }" in SCRIPT


def test_zip_links_ask_the_preflight_and_say_why_in_korean() -> None:
    # session check first (kept), then GET …/logs/check for the same route, then navigate
    check = _function("zipCheck")
    assert "href.replace(/logs\\.zip$/, 'logs/check')" in check
    assert "r.status === 401) return { expired: true };" in check
    assert "ZIP_ERR[code]" in check and "code === 'too_large' && kind !== 'all'" in check
    click = SCRIPT.split("document.addEventListener('click', async (e) => {", 1)[1]
    order = [click.index(s) for s in ("API + '/session'", "await zipCheck(href, kind)", "window.location.assign(href)")]
    assert order == sorted(order)
    assert "if (!pre.ok) { zipNote(pre.msg, true); return; }" in click
    for code, text in (("busy", "다른 로그 ZIP을 만드는 중입니다. 잠시 뒤에 다시 해 주세요."),
                       ("too_large", "로그가 너무 커서 한 번에 받을 수 없습니다. 실행별 로그를 받아 주세요."),
                       ("no_space", "서버에 임시 공간이 부족해 ZIP을 만들 수 없습니다.")):
        assert f"{code}: '{text}'" in SCRIPT, code
    assert "SKIPPED.txt" in click                              # a skipped runaway log is announced
    assert ".note.bad {" in STYLE and ".zipnote { margin: 0; position: sticky;" in STYLE


def test_states() -> None:
    state = _function("setState")
    assert "사용량을 불러오는 중입니다." in state
    assert "사용량을 불러오지 못했습니다." in state and ">다시 시도</button>" in state
    assert "아직 실행 기록이 없습니다." in state
    assert "setState('empty')" in _function("load") and "setState('error')" in _function("load")


def test_numbers_exact_with_compact_beside() -> None:
    assert "new Intl.NumberFormat('ko-KR')" in SCRIPT
    compact = _function("compact")
    assert "['B', 1e9], ['M', 1e6], ['K', 1e3]" in compact
    totals = _function("renderTotals")
    for label in ("합계 토큰", "입력 토큰", "출력 토큰", "호출 수", "실행 수", "프로젝트 수"):
        assert f"'{label}'" in totals, label
    assert ".num { text-align: right;" in STYLE


def test_cost_only_when_given_and_currency_only_from_the_api() -> None:
    assert "if (t.cost != null) html += tile('비용'" in _function("renderTotals")
    cost = _function("fmtCost")
    assert "currency ? `${s} ${currency}` : s" in cost
    assert "currency = d.totals && d.totals.currency" in _function("load")
    assert not re.search(r"[₩€¥£]|['\"]\$['\"\s]|\bUSD\b|\bKRW\b", SCRIPT)   # no currency made up by the page
    assert "t.unpriced_runs" in _function("renderTotals")


def test_project_table_sorts_filters_and_opens() -> None:
    # the owner (2026-11-19): projects in the order they were received, newest on top (see test_default_order_*)
    assert "let sort = { key: 'last_run_at', dir: 'desc' };" in SCRIPT
    head = _function("headCell")
    assert '<button class="sort" type="button" data-key="${col.key}"' in head
    assert "aria-sort" in head
    for key in ("project", "runs", "calls", "input_tokens", "output_tokens", "total_tokens", "cost", "last_run_at"):
        assert f"key: '{key}'" in SCRIPT, key
    render = _function("renderProjects")
    assert "$('filter').value.trim().toLowerCase()" in render
    assert 'aria-expanded="${isOpen}"' in render and 'aria-controls="${did}"' in render
    assert "title=\"${esc(name)}\"" in render                  # the full name when it is cut
    assert "b.setAttribute('aria-expanded', String(open))" in _function("toggle")


def test_runs_of_a_project() -> None:
    runs = _function("runsTable")
    for head in ("날짜", "진행 방식", "범위", "결과", "호출", "입력 토큰", "출력 토큰", "합계 토큰", "로그"):
        assert f">{head}</th>" in runs, head
    assert "r.usage_known === false" in runs and "집계 전" in runs
    assert "r.partial" in runs and "일부만 읽음" in runs
    assert "r.has_logs === false" in runs
    assert "'취약점 해결'" in SCRIPT or "취약점 해결</span>" in SCRIPT
    assert "s === 'jdk-jakarta'" in SCRIPT
    # words the server sends (result line: ok / incomplete / aborted, 'exit <n>') are shown in Korean
    for word in ("ok:", "incomplete:", "aborted:", "running:", "queued:"):
        assert word in SCRIPT, word
    assert "/^exit (-?\\d+)$/" in SCRIPT


def test_narrow_screens_and_keyboard() -> None:
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in PAGE
    assert ".tablebox { overflow-x: auto;" in STYLE
    assert ".grid .stick { position: sticky; left: 0;" in STYLE
    assert "@media (max-width: 640px)" in STYLE
    assert ":focus-visible { outline: 2px solid var(--blue);" in STYLE
    assert "again.focus()" in SCRIPT                          # sorting keeps the keyboard position


def test_text_rules() -> None:
    # Korean UI text and comments: no em dash, arrow, version tag or date; no real names
    comments = re.findall(r"/\*.*?\*/|<!--.*?-->|//[^\n]*", PAGE, re.S)
    ui = re.findall(r"[가-힣][^<>`'\"\n]*", PAGE)
    for text in comments + ui:
        assert not re.search("—|–|→|⇒|←", text), text
        assert not re.search(r"\bv2\.\d|20\d\d-\d\d-\d\d", text), text
    assert "app-" not in PAGE                                  # sample names stay in the fake server



# ---- default order: newest received first (the owner, 2026-11-19: "접수받은 순서대로 … 최신이 제일 위로")

import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402

import pytest as _pytest  # noqa: E402


def test_default_order_is_the_latest_run_newest_first_with_the_marker_on_that_column() -> None:
    assert "let sort = { key: 'last_run_at', dir: 'desc' };" in SCRIPT
    value = _function("sortValue")
    assert "if (key === 'last_run_at') { const t = Date.parse(p.last_run_at || ''); return isNaN(t) ? null : t; }" in value
    srt = _function("sorted")
    assert "if (x == null && y != null) return 1;" in srt                       # no time: last, in either direction
    assert "String(a.project || '').localeCompare(String(b.project || ''), 'ko')" in srt   # equal times: by name
    # the marker follows the sort key; columns stay clickable
    assert "const on = sort.key === col.key;" in _function("headCell")
    assert "if (sort.key === key) sort.dir = sort.dir === 'asc' ? 'desc' : 'asc';" in SCRIPT


@_pytest.mark.skipif(_shutil.which("node") is None, reason="node is not installed")
def test_default_order_runs_in_node() -> None:
    """Run the page's own sortValue / sorted on three projects whose latest runs are in another order than their
    token totals, plus a tie and a project without a time."""
    code = "\n".join([
        "const int = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };",
        "let sort = { key: 'last_run_at', dir: 'desc' };",
        _function("sortValue"), _function("sorted"),
        "const rows = " + _json.dumps([
            {"project": "big-old", "total_tokens": 9000, "last_run_at": "2026-11-17T09:00:00+00:00"},
            {"project": "small-new", "total_tokens": 10, "last_run_at": "2026-11-19T09:00:00+00:00"},
            {"project": "mid", "total_tokens": 500, "last_run_at": "2026-11-18T09:00:00+00:00"},
            {"project": "b-tie", "total_tokens": 1, "last_run_at": "2026-11-18T09:00:00+00:00"},
            {"project": "no-time", "total_tokens": 99999, "last_run_at": None},
        ]) + ";",
        "console.log(JSON.stringify(sorted(rows).map((p) => p.project)));",
        "sort = { key: 'total_tokens', dir: 'desc' };",
        "console.log(JSON.stringify(sorted(rows).map((p) => p.project)));",
    ])
    out = _subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True).stdout.splitlines()
    assert _json.loads(out[0]) == ["small-new", "b-tie", "mid", "big-old", "no-time"]
    # a click on 합계 토큰 still sorts by tokens
    assert _json.loads(out[1]) == ["no-time", "big-old", "mid", "small-new", "b-tie"]


def test_runs_inside_a_project_stay_newest_first() -> None:
    runs = _function("runsTable")
    assert "(Date.parse(b.accepted_at || '') || 0) - (Date.parse(a.accepted_at || '') || 0)" in runs
    assert "최근 실행부터" in runs
