"""2026-11-11: a finished migration whose build pod chained a vulnerability remediation shows one more rail stage
(`취약점 조치`, the `remediation` graph node) and a "취약점 조치" block in the result card, fed by `/api/remediations/<run id>`
(404 → no block). Static checks on the page source, like `test_page_queue.py`."""
from __future__ import annotations

import re
from pathlib import Path

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]


def _function(name: str) -> str:
    m = re.search(r"\n  (?:async )?function " + name + r"\(.*?\n  }\n", SCRIPT, re.S)
    assert m, name
    return m.group(0)


def test_rail_knows_the_remediation_node() -> None:
    assert "remediation: '취약점 조치'" in SCRIPT
    # driven by the build pod's journal lines: graph_node (start) and the node event of graph_node_end
    assert "name === 'graph_node' && ev.node === 'remediation'" in SCRIPT
    assert "name === 'remediation' && current" in SCRIPT
    # the fixed rail is unchanged: runs that never emit the node never get the stage
    assert "const FIXED = ['intake', 'analyze', 'plan'];" in SCRIPT
    assert "remediation" not in _function("resetRail")


def test_block_is_fetched_per_run_and_hidden_on_404() -> None:
    assert "/api/remediations/${encodeURIComponent(id)}" in SCRIPT
    load = _function("loadRemediation")
    assert "if (!r.ok || !st || !st.status) { $('remed').hidden = true; return; }" in load
    assert "current !== runId" in load
    assert "loadRemediation(current)" in _function("showResult")
    assert "hideRemediation()" in _function("attach")
    assert re.search(r'<div class="remed" id="remed" hidden', PAGE)
    assert "<h2>취약점 조치</h2>" in PAGE


def test_status_labels_counts_and_downloads() -> None:
    for status, label in (("QUEUED", "대기"), ("RUNNING", "진행 중"),
                          ("AWAITING_CONFIRMATION", "업로드 확인 대기"), ("STOPPED", "중지됨"), ("INTERRUPTED", "중단됨"),
                          ("SUCCEEDED", "조치 완료"), ("INCOMPLETE", "일부만 조치됨"), ("FAILED", "실패")):
        assert f"{status}: '{label}'" in SCRIPT, status
    render = _function("renderRemediation")
    for token in ("c.baseline_findings", "c.final_findings", "c.proposals_applied", "st.reasons",
                  "'report.md'", "'result.zip'", "'result-verified.zip'", "'빌드 실패본'", "st.delivery.build_ok === false",
                  "st.resume", "rs.note", "st.stale", "st.message_ko || st.message"):
        assert token in render, token






def test_server_strings_never_go_through_innerHTML() -> None:
    for name in ("renderRemediation", "loadRemediation", "confirmationView", "stoppedView", "goingView", "resumeRow",
                 "postResume"):
        body = _function(name)
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, name
    assert "e.textContent = String(text)" in _function("el")


def test_confirmation_and_stopped_actions() -> None:
    conf = _function("confirmationView")
    assert "'IVAS에서 확인했고, 접수되지 않았다 → 다시 올리기'" in conf
    assert "confirm: 'reupload'" in conf
    assert "'접수되어 있다 → projectId / analysisId 입력 후 이어가기'" in conf
    assert "confirm: 'receipt', project_id, analysis_id" in conf
    assert "pid.value.trim()" in conf and "aid.value.trim()" in conf and "!project_id || !analysis_id" in conf
    assert "rs.note" in conf
    stopped = _function("stoppedView")
    assert "st.stale" in stopped and "st.reason === 'chain_budget'" in stopped
    assert "resumeRow(runId, rs, {}, '이어서 진행')" in stopped
    row = _function("resumeRow")
    assert "rs.available" in row and "rs.note" in row
    assert "remUrl(runId, '/resume')" in _function("postResume")


def test_resuming_state_polls_and_shows_queue() -> None:
    going = _function("goingView")
    assert "'이어서 진행 중'" in going and "적용 · 빌드 · 재검사를 이어서 합니다" in going
    assert "자리가 날 때까지 대기 중 (앞에 ${ahead}건)" in going and "qpos(j.queue)" in going
    load = _function("loadRemediation")
    assert "if (remActive(st)) rem.poll = setTimeout(" in load
    assert "jobActive(st.resume_job)" in _function("remActive")


def test_no_control_labelled_resume_jargon() -> None:
    assert "재개" not in PAGE                                                  # the user did not know the word
    assert not re.search(r">\s*재개\s*<", PAGE)


def test_rail_note_names_no_approval() -> None:
    assert "'취약점 조치 중 — IVAS 검사와 버전 수정을 진행합니다.'" in SCRIPT
    assert "승인된 버전 수정" not in PAGE


# ---- v2.1z-ba: the approval step of the remediation is removed

ADMIN = (Path(sbmig.web.__file__).parent / "static" / "admin.html").read_text(encoding="utf-8")


def test_no_approval_request_or_list_is_left() -> None:
    assert "/approvals" not in SCRIPT and "/proposals" not in SCRIPT
    for gone in ("approvalsView", "decidedView", "submitDecisions", "proposalRow", "groupBox", "rem.sel", "propKey",
                 "pending_approvals", "KIND_KO", "RISK_KO", "ORIGIN_KO", "class=\"selbar\"", ".selbar", ".pgroup", ".prop ",
                 "pre.diff", ".confirm {"):
        assert gone not in PAGE, gone
    # the word 승인 (remediation approval) is gone from both pages
    assert "승인" not in PAGE and "승인" not in ADMIN


def test_an_old_waiting_state_is_shown_as_ended() -> None:
    # compared with the literal value: the server-side constant may disappear
    assert "const LEGACY_WAITING = 'AWAITING_APPROVAL';" in SCRIPT
    assert "const LEGACY_WAITING_KO = '조치 미완료';" in SCRIPT
    assert "'이 실행의 취약점 조치는 끝나지 않았습니다. 프로젝트를 다시 올리면 끝까지 진행합니다.'" in SCRIPT
    assert "[LEGACY_WAITING]: LEGACY_WAITING_KO" in SCRIPT
    # neutral tone: no class for it
    assert "LEGACY_WAITING" not in SCRIPT.split("const REM_CLS =", 1)[1].split("};", 1)[0]
    render = _function("renderRemediation")
    assert "else if (st.status === LEGACY_WAITING) act.replaceChildren(el('p', 'lead', LEGACY_WAITING_NOTE));" in render
    assert "if (LEGACY_REASONS.includes(code)) continue;" in render
    assert "const LEGACY_REASONS = ['pending_approval', 'rejected_proposals'];" in SCRIPT
    # not active: no polling, no resume control
    assert "LEGACY_WAITING" not in _function("remActive")
    # the run card's sub line and 「지난 실행」 add nothing after the migration's outcome
    assert "LEGACY_WAITING" not in SCRIPT.split("const REM_SHORT =", 1)[1].split("};", 1)[0]
    # unknown values: the raw value in the neutral tone
    assert "(REM_KO[st.status] || st.status)" in render and "(REM_CLS[st.status] || '')" in render
    # the admin page: the same neutral word
    assert "awaiting_approval: ['', '조치 미완료']" in ADMIN
