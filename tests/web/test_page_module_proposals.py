"""Override proposals in a module's own pom (contract v2.1z-an): the approval list can hold dozens of rows (one per module
pom and library). The page groups them by library, says per row what will be done and where the library comes from, keeps
"checked = applied, unchecked = not applied, then the run goes on", and tells the owners they may decide later.
Static checks on the page source, like `test_page_remediation.py`; the browser check is done by hand with a fake server."""
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


def test_rows_are_grouped_by_library_and_variant() -> None:
    key = _function("groupKey")
    assert "p.ga" in key and "o.type" in key and "o.classifier" in key
    view = _function("approvalsView")
    assert "g.items.length === 1" in view                        # a single proposal stays one row
    assert "groupBox(g, gi)" in view and "proposalRow(p, i, g.common)" in view
    box = _function("groupBox")
    assert "role', 'group'" in box and "aria-labelledby" in box and "head.htmlFor = id" in box
    assert "cb.type = 'checkbox'" in box


def test_group_checkbox_and_select_all_none() -> None:
    view = _function("approvalsView")
    assert "g.cb.checked = k === t; g.cb.indeterminate = k > 0 && k < t;" in view
    assert "for (const { p } of g.items) rem.sel.set(p.id, g.cb.checked)" in view
    assert "for (const p of pending) rem.sel.set(p.id, true)" in view
    assert "for (const p of pending) rem.sel.set(p.id, false)" in view
    assert "`${t}건 중 ${k}건 선택`" in view
    # every control is locked while the confirmation is open
    assert "...groups.map((g) => g.cb).filter(Boolean)" in view


def test_consequence_and_decide_later_notice() -> None:
    view = _function("approvalsView")
    assert "'체크하지 않은 항목은 적용하지 않고 진행합니다.'" in view
    assert "버튼을 누르기 전에는 아무것도 적용하지 않습니다." in view and "승인 대기 상태로 기다립니다." in view


def test_post_body_is_unchanged() -> None:
    submit = _function("submitDecisions")
    assert "decisions: pending.map((p) =>" in submit
    assert "decision: rem.sel.get(p.id) ? 'approved' : 'rejected'" in submit and "resume: true" in submit


def test_selection_survives_the_refresh() -> None:
    view = _function("approvalsView")
    # checks live in rem.sel by proposal id, reset only when the pending proposals change
    assert "if (key !== rem.propKey) { rem.propKey = key; rem.sel = new Map(); }" in view
    assert "if (!force && key === rem.viewKey) return;" in _function("renderRemediation")


def test_what_will_be_done_in_fixed_frames() -> None:
    todo = _function("todoText")
    assert "e.pom === 'pom.xml' ? '최상위 pom.xml'" in todo
    assert "에 버전 지정 추가" in todo and "행` : ''" in todo
    src = _function("sourceText")
    for frame in ("' 경로로 가져옴'", "'선언 위치 '", "'에 직접 선언'", "'의 dependencyManagement에서 버전 지정'",
                  "'에서 버전 지정'", "'버전 property '"):
        assert frame in src, frame
    # no particle whose form depends on the value before it (을/를, 이/가, 은/는, 으로/로 after a value)
    for body in (todo, src, _function("pathFacts")):
        assert not re.search(r"\}\s*(을|를|이|가|은|는|으로)[\s'`]", body), body


def test_classifier_and_type_are_named() -> None:
    words = _function("variantWords")
    assert "'형식 '" in words and "'classifier '" in words and "type !== 'jar'" in words
    assert "variantWords(o.type, o.classifier)" in _function("proposalRow")


def test_old_states_render_as_before() -> None:
    row = _function("proposalRow")
    assert "const fresh = Array.isArray(p.sources);" in row
    assert "const todo = fresh ? todoText(p) : '';" in row
    assert "if (p.kind && !todo) tags.appendChild(el('span', 'tag', KIND_KO[p.kind] || p.kind));" in row


def test_paths_name_modules_when_they_differ() -> None:
    facts = _function("pathFacts")
    assert "'들어온 경로'" in facts and "모듈: " in facts and "groups.size === 1" in facts
    assert "cb.setAttribute('aria-describedby', path.id)" in _function("proposalRow")


def test_server_text_goes_through_textcontent_only() -> None:
    for name in ("groupBox", "proposalRow", "todoText", "sourceText", "pathFacts", "variantWords", "groupKey"):
        body = _function(name)
        assert "innerHTML" not in body and "insertAdjacentHTML" not in body, name


def test_layout_rules_for_long_coordinates_and_focus() -> None:
    assert ".pgroup .gitems" in PAGE and ".pgroup:has(> .prophead input:focus-visible)" in PAGE
    assert ".prop:has(> .prophead input:focus-visible)" in PAGE
    # no new colour literal in the new rules: the page's tokens only
    block = PAGE[PAGE.index("  .pgroup {"):PAGE.index("  .prop .paths li")]
    assert not re.search(r"#[0-9A-Fa-f]{3,6}\b", block)


def test_new_page_comments_read_hand_written() -> None:
    names = ("groupKey", "groupBox", "proposalRow", "todoText", "variantWords", "sourceText", "pathFacts")
    start = SCRIPT.index("  // ---- 승인 대기")
    end = SCRIPT.index("  function evidenceText(ev)")
    comments = [ln.split("//", 1)[1] for ln in SCRIPT[start:end].splitlines() if "//" in ln and "://" not in ln]
    assert comments and all(n in SCRIPT[start:end] for n in names)
    for c in comments:
        assert not re.search(r"[—→]|\bv\d+\.\d|\d{4}-\d{2}-\d{2}|\*\*", c), c


def test_ineffective_proposals_are_neither_pending_nor_counted_by_the_page() -> None:
    """`decision: "ineffective"` (applied, did not change the resolved version, removed again): the list takes exactly
    `pending`, the page never counts approved / rejected proposals from the server (the 적용 chip is the server's
    `counts.proposals_applied`), and no view lists decided proposals, so no label is needed."""
    render = _function("renderRemediation")
    assert "props.proposals.filter((p) => p && p.decision === 'pending')" in render
    assert "c.proposals_applied" in render
    for name in ("renderRemediation", "decidedView", "approvalsView", "proposalRow", "groupBox"):
        body = _function(name)
        assert not re.search(r"\.decision\s*(===|!==|==)\s*'(approved|rejected|ineffective)'", body), name
    assert "ineffective" not in SCRIPT
