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
    for status, label in (("QUEUED", "대기"), ("RUNNING", "진행 중"), ("AWAITING_APPROVAL", "승인 대기"),
                          ("AWAITING_CONFIRMATION", "업로드 확인 대기"), ("STOPPED", "중지됨"), ("INTERRUPTED", "중단됨"),
                          ("SUCCEEDED", "조치 완료"), ("INCOMPLETE", "일부만 조치됨"), ("FAILED", "실패")):
        assert f"{status}: '{label}'" in SCRIPT, status
    render = _function("renderRemediation")
    for token in ("c.baseline_findings", "c.final_findings", "c.proposals_applied", "st.pending_approvals", "st.reasons",
                  "'report.md'", "'result.zip'", "'result-verified.zip'", "'빌드 실패본'", "st.delivery.build_ok === false",
                  "st.resume", "rs.note", "st.stale"):
        assert token in render, token


def test_one_post_with_every_pending_proposal_and_resume() -> None:
    """Wave 4 (user, 2026-11-11): checkboxes, ONE button; checked = approved, unchecked = rejected, all in one POST that also
    resumes (`resume: true`); no approver name."""
    submit = _function("submitDecisions")
    assert "remUrl(runId, '/approvals')" in submit and "method: 'POST'" in submit
    assert submit.count("fetch(") == 1
    assert "decisions: pending.map((p) =>" in submit                       # every pending proposal, not only the checked ones
    assert "decision: rem.sel.get(p.id) ? 'approved' : 'rejected'" in submit
    for field in ("proposal_id: p.id", "proposal_hash: p.proposal_hash", "source_tree_hash: p.source_tree_hash",
                  "policy_hash: p.policy_hash", "diff: p.diff"):
        assert field in submit, field
    assert "resume: true" in submit
    assert not re.search(r"\bby\b\s*[:,]", submit) and "remed-by" not in PAGE and "승인자" not in PAGE
    assert "if (rem.busy) return;" in submit                                # no double submit
    # outcomes: resumed / decisions saved but not resumed / proposals changed
    for token in ("d.resumed", "d.resume_error", "re.error", "r.status === 409 && d && d.code === 'approval_mismatch'",
                  "제안이 바뀌었습니다. 목록을 새로 불러왔습니다.", "rem.sel = new Map()"):
        assert token in submit, token


def test_checkbox_list_select_all_and_count() -> None:
    view = _function("approvalsView")
    assert "'선택한 항목 적용'" in view and "'전체 선택'" in view
    assert "all.indeterminate = n > 0 && n < m" in view
    assert "`${n}건 선택 / 전체 ${m}건`" in view
    row = _function("proposalRow")
    assert "cb.type = 'checkbox'" in row and "head.htmlFor = id" in row and "el('label', 'prophead')" in row
    assert "'변경 내용 보기'" in row and "el('details')" in row
    for code, label in (("direct_version", "직접 버전"), ("property_version", "속성 값"), ("managed_override", "관리 버전 override 추가"),
                        ("parent_version", "부모 버전"), ("bom_version", "BOM 버전"), ("major", "메이저 변경"), ("parent", "부모"),
                        ("bom", "BOM")):
        assert f"{code}: '{label}'" in SCRIPT, code
    assert "RISK_KO" in row and "KIND_KO" in row and "p.evidence" in row and "p.note" in row


def test_confirmation_wording_and_focus() -> None:
    view = _function("approvalsView")
    for text in ("${n}건을 적용하고 ${m}건은 적용하지 않습니다. 적용하지 않은 제안은 이 작업에서 다시 나오지 않습니다.",
                 "건 전부 적용하지 않습니다.", "'적용'", "'취소'"):
        assert text in view, text
    assert "ok.focus()" in view and "window.confirm" not in SCRIPT and "confirm(" not in view.replace("submitDecisions", "")


def test_diff_lines_are_text_nodes() -> None:
    pre = _function("diffPre")
    assert "el('span', cls || null, ln)" in pre and "createTextNode('\\n')" in pre
    assert "'add'" in pre and "'del'" in pre
    assert "innerHTML" not in pre


def test_server_strings_never_go_through_innerHTML() -> None:
    for name in ("renderRemediation", "approvalsView", "proposalRow", "diffPre", "submitDecisions", "loadRemediation",
                 "decidedView", "confirmationView", "stoppedView", "goingView", "resumeRow", "postResume"):
        body = _function(name)
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, name
    assert "e.textContent = String(text)" in _function("el")


def test_confirmation_and_stopped_actions() -> None:
    conf = _function("confirmationView")
    assert "'IVAS 에서 확인했고, 접수되지 않았다 → 다시 올리기'" in conf
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
    assert "'이어서 진행 중'" in going and "새 빌드 파드에서 적용 · 빌드 · 재검사" in going
    assert "자리가 날 때까지 대기 중 (앞에 ${ahead}건)" in going and "qpos(j.queue)" in going
    load = _function("loadRemediation")
    assert "if (remActive(st)) rem.poll = setTimeout(" in load
    assert "jobActive(st.resume_job)" in _function("remActive")


def test_no_control_labelled_resume_jargon() -> None:
    assert "재개" not in PAGE                                                  # the user did not know the word
    assert not re.search(r">\s*재개\s*<", PAGE)
