"""v2.1z-ak: the page shows a framework project's reduced run (scope jdk-jakarta) — a label + one sentence while running
(after analyze) and in the result card, and the stages the plan does not have (재구성, 구조 검토) as skipped, never pending.
The page learns the scope from the journal (`framework_project` at analyze, `plan` with `scope`), which reaches the page
in every web mode (in-process and Job modes tail the run directory's journal.jsonl). Static checks on the page source;
the behaviour was checked in a headless Chromium against a scripted SSE server when the page changed."""
from __future__ import annotations

from pathlib import Path

import sbmig.web

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
SENTENCE = "JDK 21과 jakarta만 적용합니다. 재구성과 구조 검토는 하지 않습니다."


def test_label_and_sentence_in_run_and_result_cards() -> None:
    run_card = PAGE.split('id="runcard"', 1)[1].split("</section>", 1)[0]
    result_card = PAGE.split('id="resultcard"', 1)[1].split("</section>", 1)[0]
    for card, el in ((run_card, 'id="fwnote" hidden'), (result_card, 'id="fwresult" hidden')):
        assert el in card
        assert "프레임워크 프로젝트" in card and SENTENCE in card


def test_scope_comes_from_the_journal() -> None:
    assert "(name === 'framework_project' || name === 'plan') && ev.scope === 'jdk-jakarta'" in SCRIPT
    assert "setFramework(true)" in SCRIPT
    # a new run starts without the label (a full run shows nothing special)
    assert "resetRail(runFlow); setFramework(false);" in SCRIPT


def test_skipped_stages_are_drawn_and_never_current() -> None:
    assert "const SKIP_STEPS = ['restructure', 'review'];" in SCRIPT
    assert "review: '구조 검토'" in SCRIPT and "restructure: '재구성'" in SCRIPT
    assert 'class="step skipped"' in SCRIPT and "하지 않음" in SCRIPT
    assert ".rail .step.skipped .dot" in PAGE
    # re-placed after the plan draws its phases, and the next current step skips them
    assert "for (const ph of u.phases) phaseStep(ph, true);\n      placeSkipped();" in SCRIPT
    assert "steps.slice(last + 1).find((n) => !n.classList.contains('skipped'))" in SCRIPT
    # the remediation-only flow has no migration stages to skip
    assert "if (!fwReduced || railFlow === FLOW_REM) return;" in SCRIPT


def test_page_comments_are_plain() -> None:
    added = [ln for ln in PAGE.splitlines() if "프레임워크" in ln and ("//" in ln or "/*" in ln or "<!--" in ln)]
    assert added
    for ln in added:
        assert "—" not in ln and "→" not in ln and "v2." not in ln, ln
