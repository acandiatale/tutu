"""K1: a structure-review answer that is not whole JSON (a raw newline inside a string, an answer cut at the token
limit) is never reduced to its first issue object and accepted. Lenient decoding where it is safe; a fragment or a
cut answer is unreadable, so the reviewer's one retry runs; a second unreadable answer is a failed review."""
from __future__ import annotations

from sbmig.core.interfaces import LLMResponse
from sbmig.review import StructureReviewer
from sbmig.review.parse import parse_review_answer
from sbmig.review.reviewer import CUT_RETRY_TAIL, RETRY_MESSAGE

from .test_reviewer import OK, make_ws, profile

THREE = ('<review>\n{"verdict": "issues", "issues": [\n'
         '  {"severity": "warning", "file": "pom.xml", "problem": "first"},\n'
         '  {"severity": "critical", "file": "pom.xml", "problem": "second"},\n'
         '  {"severity": "warning", "file": "pom.xml", "problem": "third"}\n'
         '], "analysis": "Three findings."}\n</review>')


def test_raw_newline_inside_a_string_is_decoded_whole():
    text = THREE.replace('"problem": "second"', '"problem": "second\nline"')
    p = parse_review_answer(text)
    assert p.ok and [i.problem for i in p.issues] == ["first", "second\nline", "third"]
    assert p.analysis == "Three findings."


def test_raw_newline_with_prose_around_is_decoded_whole():
    text = "Here is my review:\n" + THREE.replace("<review>", "").replace("</review>", "").replace(
        '"analysis": "Three findings."', '"analysis": "Three\nfindings."') + "\nThanks."
    p = parse_review_answer(text)
    assert p.ok and len(p.issues) == 3 and p.analysis == "Three\nfindings."


def test_an_answer_cut_inside_the_json_is_a_fragment_not_one_issue():
    cut = THREE[: THREE.index('{"severity": "warning", "file": "pom.xml", "problem": "third"') + 20]
    p = parse_review_answer(cut)
    assert not p.ok and p.issues == [] and "fragment" in p.error
    p = parse_review_answer(cut, "length")
    assert not p.ok and p.issues == []


def test_finish_reason_length_is_unreadable_even_when_the_json_closed():
    p = parse_review_answer(THREE, "length")
    assert not p.ok and "token limit" in p.error and "finish_reason=length" in p.error
    assert not parse_review_answer(THREE, "max_tokens").ok
    assert parse_review_answer(THREE, "stop").ok and parse_review_answer(THREE).ok


def test_prose_braces_before_the_json_are_not_a_broken_outer_value():
    text = "The pom uses ${project.version} and {weird} text.\n" + THREE.replace("<review>", "").replace(
        "</review>", "")
    p = parse_review_answer(text)
    assert p.ok and len(p.issues) == 3


def test_a_single_issue_object_and_a_bare_list_still_read():
    one = '{"severity": "critical", "file": "pom.xml", "problem": "only"}'
    p = parse_review_answer(f"<review>{one}</review>")
    assert p.ok and [i.problem for i in p.issues] == ["only"]
    p = parse_review_answer(f"<review>[{one}, {one}]</review>")
    assert p.ok and len(p.issues) == 2


class _CutLLM:
    def __init__(self, *answers: tuple[str, str]):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, role=None,
                 metadata=None):
        self.calls.append({"messages": list(messages)})
        text, finish = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        return LLMResponse(content=text, model="gpt-review", input_tokens=10, output_tokens=10, finish_reason=finish)


def test_a_cut_review_uses_the_retry_and_asks_for_a_shorter_answer(tmp_path, cfg):
    ws = make_ws(tmp_path)
    cut = THREE[: THREE.index('"problem": "third"')]
    llm = _CutLLM((cut, "length"), (OK, "stop"))
    rv = StructureReviewer(cfg, llm, None).review(ws, profile(), cfg, run_id="r")
    assert len(llm.calls) == 2 and rv.verdict == "ok"
    ask = llm.calls[1]["messages"][-1].content
    assert ask.startswith(RETRY_MESSAGE) and ask.endswith(CUT_RETRY_TAIL)


def test_two_cut_answers_are_a_failed_review_never_one_issue(tmp_path, cfg):
    ws = make_ws(tmp_path)
    cut = THREE[: THREE.index('"problem": "third"')]
    llm = _CutLLM((cut, "length"), (cut, "length"))
    rv = StructureReviewer(cfg, llm, None).review(ws, profile(), cfg, run_id="r")
    assert len(llm.calls) == 2 and rv.verdict == "failed" and rv.issues == []
    assert "unreadable answer twice" in rv.note


def test_a_fragment_without_finish_reason_is_retried_too(tmp_path, cfg):
    ws = make_ws(tmp_path)
    cut = THREE[: THREE.index('"problem": "third"')]
    llm = _CutLLM((cut, "stop"), (THREE, "stop"))
    rv = StructureReviewer(cfg, llm, None).review(ws, profile(), cfg, run_id="r")
    assert len(llm.calls) == 2 and len(rv.issues) == 3
    assert not llm.calls[1]["messages"][-1].content.endswith(CUT_RETRY_TAIL)
