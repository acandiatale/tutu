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


def test_approvals_post_carries_the_three_hashes_and_the_diff() -> None:
    decide = _function("decide")
    assert "remUrl(runId, '/approvals')" in decide and "method: 'POST'" in decide
    for field in ("proposal_id: p.id", "decision", "proposal_hash: p.proposal_hash", "source_tree_hash: p.source_tree_hash",
                  "policy_hash: p.policy_hash", "diff: p.diff"):
        assert field in decide, field
    assert "by, decisions:" in decide
    card = _function("proposalCard")
    assert "'승인'" in card and "'거절'" in card and "'approved'" in card and "'rejected'" in card


def test_server_strings_never_go_through_innerHTML() -> None:
    for name in ("renderRemediation", "proposalCard", "decide", "loadRemediation"):
        body = _function(name)
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, name
    card = _function("proposalCard")
    assert "el('pre', 'diff'); pre.textContent = p.diff" in card
    assert "e.textContent = String(text)" in _function("el")


def test_resume_button_is_disabled_with_the_reason() -> None:
    assert re.search(r'<button class="btn quiet" id="remed-resume" type="button" disabled>재개</button>', PAGE)
    render = _function("renderRemediation")
    assert "$('remed-resume').disabled = !rs.available || pend > 0;" in render
    assert "remUrl(runId, '/resume')" in SCRIPT
