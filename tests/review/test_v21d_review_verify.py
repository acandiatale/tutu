"""v2.1d dependency version policy in the structure review: the prompt forbids a major upgrade / library replacement
in the review (it is left to the fix loop), and a `<verify>` the model gives anyway is carried into
`StructureReview.answer.verify`."""
from __future__ import annotations

from sbmig.review import parse_review_answer
from sbmig.review.reviewer import system_prompt

from .test_review_edits import answer, run
from .test_reviewer import make_ws

VERIFY = "운영자 확인: module/comp-jar/pom.xml 의 sitemesh 3.2.1 로 장식되는 화면이 그대로인지 확인"


def with_verify(text: str, verify: str = VERIFY) -> str:
    return text.replace("</solution>\n", f"</solution>\n<verify>{verify}</verify>\n", 1)


def test_review_prompt_states_the_policy(cfg):
    text = system_prompt(cfg)
    for part in ("never raise a dependency's major version or replace a library in the review",
                 "prefer the\n  parent-managed release (omit `version`); never re-pin what \"What the restructure did\"",
                 ("If a critical issue\n  can only be fixed by a major upgrade, say so in `<analysis>` / `fix_hint` "
                  "and leave it to the fix loop"), "`<verify>` — only if your edits nevertheless raise a dependency's major version",
                 "<verify>운영자 확인: …</verify>"):
        assert part in text, part
    assert "{{" not in text


def test_verify_is_parsed_next_to_the_review():
    p = parse_review_answer(with_verify(answer()))
    assert p.ok and p.verify == VERIFY and len(p.edits) == 1 and p.solution.startswith("declare")
    assert len(p.critical) == 1                                       # the review JSON is unaffected
    assert parse_review_answer(answer()).verify == ""
    # an unclosed <review> block ends at <verify>
    unclosed = with_verify(answer()).replace("</review>", "")
    p = parse_review_answer(unclosed)
    assert p.ok and len(p.issues) == 2 and p.verify == VERIFY
    # a <verify> with the review alone (no fix blocks) is kept as well
    p = parse_review_answer('<review>{"verdict": "ok", "issues": []}</review>\n<verify>x</verify>')
    assert p.ok and p.verdict == "ok" and p.verify == "x"


def test_verify_reaches_the_review_answer(tmp_path, cfg):
    rv, llm = run(cfg, make_ws(tmp_path), with_verify(answer()))
    assert len(llm.calls) == 1 and rv.answer is not None and rv.answer.verify == VERIFY
    rv, _ = run(cfg, make_ws(tmp_path / "b"), answer())
    assert rv.answer is not None and rv.answer.verify == ""
