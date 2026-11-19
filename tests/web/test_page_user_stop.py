"""The owner (2026-11-19): "중단 요청은 프로젝트에 표시 할 때 주황색으로 바꿔줘 … 너무 빨가니까 문제 투성이 에이전트 같아" and
"지난 실행은 3일치만 보여주도록", "최근 3일내에 등록한 프로젝트만 보입니다. 라고 문구도 하나 넣어주고!".
A run a person stopped is shown in the caution colour (`warn`) everywhere; real failures and the engine's own aborts
stay red. 「지난 실행」 asks the server for the last RUNS_DAYS days, says so in a standing line, and offers the full list.
Static checks on the page source (the behaviour was looked at in a browser with hand-built runs)."""
from __future__ import annotations

import re
from pathlib import Path

import sbmig.web
from sbmig.core.workspace import STOP_REQUEST_REASON

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
CSS = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]


def _fn(name: str) -> str:
    return SCRIPT.split(f"function {name}", 1)[1].split("\n  }\n", 1)[0]


# ---- 1. a stop a person asked for is orange, not red

def test_title_and_list_line_of_a_user_stop_are_warn() -> None:
    describe = _fn("describe(st)")
    assert "if (userStop(st)) return { cls: 'warn', text: '중단되었습니다', short: '중단' };" in describe
    assert "cls: 'bad', text: '중단되었습니다'" not in SCRIPT
    # the server's flag first, an older server's fields as before
    user_stop = _fn("userStop(st)")
    assert "st.user_stop === true" in user_stop and "st.stop_requested && st.exit_code === 3" in user_stop
    # the old red 「진행하지 못했습니다 / 진행 못 함」 is gone (see test_refusal_* below)
    assert "진행 못 함" not in SCRIPT and "'진행하지 못했습니다'" not in SCRIPT
    # the list line takes the describe() class
    assert "const cls = run.state === 'running' || run.state === 'queued' ? 'running' : s.cls;" in SCRIPT


def test_stop_reason_matches_the_engine_constant() -> None:
    m = re.search(r"function isStopReason\(r\) \{ return /(.+?)/\.test", SCRIPT)
    assert m
    js = m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\.", ".")
    assert STOP_REQUEST_REASON.startswith(js.lstrip("^"))
    assert not re.match(m.group(1), "Maven 변환 후 신청 부탁드립니다.")


def test_rail_step_and_note_of_a_user_stop() -> None:
    # a caution state for a step, drawn with the page's warn token (no new colour)
    rule = CSS.split(".rail .step.stopped .dot {", 1)[1].split("}", 1)[0]
    assert "var(--warn)" in rule and "--bad" not in rule
    assert ".rail .step.stopped .label { color: var(--warn);" in CSS and ".railnote.warn { color: var(--warn); }" in CSS
    on_node = _fn("onNode(ev)")
    assert "(isStopReason(u.aborted) ? 'stopped'" in on_node and "el.classList.add(kind);" in on_node
    assert "abortNote = kind === 'stopped' ? STOP_NOTE :" in on_node
    # a pod cleaned up after the stop (grace delete, deleted before start) is stopped, a pod that failed by itself red
    assert "(s === 'failed' || s === 'missing') && stopAsked" in on_node and "el.classList.add('stopped')" in on_node
    assert "el.classList.add('failed'); setCurrent(null); $('railnote').textContent = `실행 환경을 준비하지 못했습니다" in on_node
    # the note is toned by its content, wherever it was written
    tone = _fn("toneRailNote()")
    assert "t === STOP_NOTE || t === STOP_JOB_NOTE || t === stopNoteKo" in tone
    assert "new MutationObserver(toneRailNote).observe($('railnote')" in SCRIPT
    assert "const STOP_NOTE = '중단 요청으로 멈췄습니다. 마지막 빌드는 하지 않았습니다.';" in SCRIPT
    # every "is this step finished" check knows the new state
    assert "n.classList.contains('failed') || n.classList.contains('stopped')" in SCRIPT
    assert ":not(.done):not(.failed):not(.stopped)" in SCRIPT


def test_button_log_toast_result_box_and_remediation() -> None:
    assert ".btn.ghost.asked, .btn.ghost.asked:disabled { opacity: 1; color: var(--warn);" in CSS
    assert "$('stop').classList.toggle('asked', !!st.stop_requested);" in SCRIPT
    # the log line of the stop request is the log's warning colour, not the error colour
    assert "logLine('stop', '[중단 요청" in SCRIPT and "logLine('error', '[중단 요청" not in SCRIPT
    assert ".logbox .WARNING, .logbox .runstop { color: #FFD48A; }" in CSS
    assert "(kind === 'stop' ? 'runstop'" in SCRIPT
    # toasts of a stop request: caution; a failed request stays red
    assert SCRIPT.count("'중단을 요청했습니다.', 'warn')") == 2
    assert "toast('중단을 요청했습니다. 지금 돌고 있는 단계가 끝나면 멈춥니다.', 'warn');" in SCRIPT
    assert "toast('중단 요청이 실패했습니다.', true);" in SCRIPT and ".toast.warn { background: var(--warn); }" in CSS
    # the result card's box for a stopped run
    assert "$('abortbox').classList.toggle('stopped', stopped);" in SCRIPT
    assert ".abort.stopped, .abort.refused { border-color: var(--warn-tint); border-left-color: var(--warn); background: var(--warn-tint); }" in CSS
    # a remediation stopped on request (even when its pod is gone) is warn
    assert "const remStopped = st.reason === 'stop_requested' || st.stop_requested === true;" in SCRIPT
    assert "remStopped && (st.status === 'STOPPED' || st.status === 'INTERRUPTED') ? 'warn'" in SCRIPT


# ---- 2. 「지난 실행」: the last three days

def test_recent_list_asks_the_server_for_the_constant() -> None:
    assert "const RUNS_DAYS = 3;" in SCRIPT and "let runsAll = false;" in SCRIPT
    fetch_runs = _fn("fetchRuns()")
    assert "days: String(runsAll ? 0 : RUNS_DAYS)" in fetch_runs
    assert "if (current) q.set('keep', current);" in fetch_runs
    assert "fetch('/api/runs?' + q.toString())" in fetch_runs


def test_recent_list_says_what_is_shown() -> None:
    fetch_runs = _fn("fetchRuns()")
    # the standing line under the heading (visible with the block closed), from the same constant
    assert '<span class="scope" id="runscope"></span></summary>' in PAGE
    assert ("setText('runscope', runsAll ? '전체 실행을 보고 있습니다.' : `최근 ${RUNS_DAYS}일 내에 등록한 프로젝트만 보입니다.`);"
            in fetch_runs)
    assert "details.history summary .scope { order: 3; flex-basis: 100%; color: var(--muted);" in CSS
    # the count
    assert "$('runcount').textContent = `${d.runs.length}건 · ${runsAll ? '전체' : `최근 ${RUNS_DAYS}일`}`;" in fetch_runs
    # empty: one statement beside the standing line, never "nothing was ever run" while older runs exist
    assert "!runsAll && hidden ? '이 기간에 등록한 프로젝트가 없습니다.' : '아직 실행이 없습니다.'" in fetch_runs
    # the control at the end, only when older runs exist (or to fold the full list back)
    assert "if (runsAll || hidden) {" in fetch_runs
    assert "runsAll ? `최근 ${RUNS_DAYS}일만 보기` : '이전 실행 보기'" in fetch_runs
    assert "btn.onclick = () => { runsAll = !runsAll; fetchRuns(); };" in fetch_runs
    # server text into the list as text
    assert "el('li', 'empty'," in fetch_runs and "el('span', 'muted', `${hidden}건 더`)" in fetch_runs


def test_page_comments_stay_plain() -> None:
    for ln in PAGE.splitlines():
        if ("중단" in ln or "지난 실행" in ln or "RUNS_DAYS" in ln) and ("//" in ln or "/*" in ln or "<!--" in ln):
            comment = ln.split("//", 1)[-1] if "//" in ln else ln
            assert "→" not in comment and "v2." not in comment, ln


# ---- 3. a run the engine could not take on (owner: "'진행 못 함' 을 처리 불가 로 바꾸자 … 색은 주황색으로 하자!")

def test_refusal_title_and_list_word_are_one_warn_state() -> None:
    describe = _fn("describe(st)")
    assert ("if (st.exit_code === 3 && abortText(st)) return engineRefusal(st)\n"
            "        ? { cls: 'warn', text: '처리할 수 없습니다', short: '처리 불가' }\n"
            "        : { cls: 'bad', text: '실행 중 오류가 났습니다', short: '실행 오류' };") in describe
    refusal = _fn("engineRefusal(st)")
    # an engine abort reason that is not an internal error; a web / pod side error (no abort_reason) stays red
    assert "!!st.abort_reason" in refusal and "!isInternalReason(st.abort_reason)" in refusal
    internal = SCRIPT.split("function isInternalReason(r) {", 1)[1].split("}\n", 1)[0]
    assert "internal error|missing component" in internal and "(Error|Exception)" in internal


def test_refusal_rail_mark_note_and_box() -> None:
    # its own mark (not the pause of a user stop): a warn dot with a white bar, warn label
    rule = CSS.split(".rail .step.refused .dot {", 1)[1].split("}", 1)[0]
    assert "var(--warn)" in rule and "--bad" not in rule
    assert ".rail .step.refused .dot::after {" in CSS and ".rail .step.refused .label { color: var(--warn);" in CSS
    assert ".rail .step.refused .dot::before" not in CSS          # no second pause bar
    on_node = _fn("onNode(ev)")
    assert "const kind = !u.aborted ? 'done' : (isStopReason(u.aborted) ? 'stopped' : (isInternalReason(u.aborted) ? 'failed' : 'refused'));" in on_node
    assert "kind === 'refused' ? `처리 불가: ${u.aborted}` : `오류: ${u.aborted}`" in on_node
    assert "`중단: ${u.aborted}`" not in SCRIPT
    tone = _fn("toneRailNote()")
    assert "t.startsWith('처리 불가: ')" in tone
    # every "is this step finished" check knows the new state
    assert "n.classList.contains('stopped') || n.classList.contains('refused')" in SCRIPT
    assert ":not(.done):not(.failed):not(.stopped):not(.refused)" in SCRIPT
    # the result card's reason box in the caution tone
    assert "$('abortbox').classList.toggle('refused', !stopped && engineRefusal(st));" in SCRIPT
    assert ".abort.stopped, .abort.refused {" in CSS


def test_admin_page_uses_the_same_word() -> None:
    admin = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")
    assert "진행 못 함" not in admin
    assert "aborted: ['warn', '처리 불가'], rejected: ['warn', '처리 불가']" in admin
    assert "stopped: ['warn', '중단']" in admin and "interrupted: ['bad', '중단']" in admin
