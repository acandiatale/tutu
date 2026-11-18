"""K3 / K6 / K7 / K8 of the fix answer:

- K3: an answer cut at the token limit whose <script> / <edits> block is not closed is neither used nor stored; one
  feedback round asks for a shorter answer; cut again = the attempt fails as an answer problem (not a transport
  error). A complete answer that merely forgot `</script>` (finish reason not length) stays lenient.
- K6: a requested file is cut by characters too, and the context message respects `llm.max_context_chars`.
- K7: a transport failure of the feedback call keeps the first answer's valid edits (with a note), logged.
- K8: `<script type="python">` is recognised; of several script blocks the last complete one is taken, with a warning.
"""
from __future__ import annotations

import json
import logging

from sbmig.core.interfaces import LLMResponse
from sbmig.fix import LLMFixer
from sbmig.fix.client import LLMError
from sbmig.fix.fixer import cut_chars
from sbmig.fix.script_protocol import MISSING_CLOSE_SCRIPT, parse_script_response
from sbmig.loop.fixloop import is_llm_transport_error

from .test_fix_script import CODE, case_cluster, proj, sctx  # noqa: F401 - proj is a fixture

EDITS = [{"op": "replace", "path": "demo-back/pom.xml", "old": "<artifactId>demo-back</artifactId>",
          "new": "<artifactId>demo-back</artifactId><!-- x -->", "desc": "pom 수정"}]
FULL = (f"<analysis>javax moved</analysis>\n<solution>rename</solution>\n<edits>\n{json.dumps(EDITS)}\n</edits>\n"
        "<changes>[{\"path\": \"demo-back/pom.xml\", \"where\": \"artifactId\", \"change\": \"주석\"}]</changes>\n"
        f"<script>\n{CODE}</script>\n")
CUT_SCRIPT = FULL[: FULL.index("</script>")]                      # cut inside the script, the code still parses
CUT_EDITS = FULL[: FULL.index('"desc"')]                          # cut inside the edits list


class FinishLLM:
    """Answers (text, finish_reason) in order; an Exception item is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, role=None,
                 metadata=None, **_):
        self.calls.append({"messages": list(messages)})
        a = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        text, finish = a
        return LLMResponse(content=text, model="m", input_tokens=1, output_tokens=1, finish_reason=finish)


# ---------------------------------------------------------------------------- K3
def test_the_parser_names_unclosed_blocks():
    assert parse_script_response(CUT_SCRIPT).unclosed == ["script"]
    assert "edits" in parse_script_response(CUT_EDITS).unclosed
    assert parse_script_response(FULL).unclosed == []


def test_a_cut_script_is_not_used_and_goes_to_one_feedback_round(cfg, proj):
    llm = FinishLLM((CUT_SCRIPT, "length"), (FULL, "stop"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 2
    feedback = llm.calls[1]["messages"][-1].content
    assert "cut off at the output token limit" in feedback and "SHORTER" in feedback
    assert p.code == CODE and len(p.edits) == 1 and p.error == ""


def test_cut_twice_fails_the_attempt_as_an_answer_problem(cfg, proj):
    llm = FinishLLM((CUT_SCRIPT, "length"), (CUT_SCRIPT, "length"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 2
    assert p.code == "" and p.edits == [] and p.raw_response == CUT_SCRIPT
    assert "cut off at the output token limit" in p.error and "also after asking for a shorter answer" in p.error
    assert not is_llm_transport_error(p.analysis)                       # not an outage: the ask counts


def test_cut_edits_with_max_tokens_are_not_used(cfg, proj):
    llm = FinishLLM((CUT_EDITS, "max_tokens"), (CUT_EDITS, "max_tokens"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 2 and p.code == "" and p.edits == [] and "<edits>" in p.error


def test_a_forgotten_closing_tag_without_length_stays_lenient(cfg, proj):
    llm = FinishLLM((CUT_SCRIPT, "stop"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 1 and p.code == CODE and len(p.edits) == 1


def test_a_complete_answer_cut_after_its_blocks_is_used(cfg, proj):
    llm = FinishLLM((FULL + "\nSome trailing prose that was cu", "length"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 1 and p.code == CODE and "output truncated" in p.analysis


# ---------------------------------------------------------------------------- K7
def test_a_feedback_transport_failure_keeps_the_first_answers_edits(cfg, proj, caplog):
    caplog.set_level(logging.INFO)
    broken = FULL.replace(CODE, "def fix(p):\n    p.log('x'\n")         # valid edits, a script that does not parse
    llm = FinishLLM((broken, "stop"), LLMError(503, "", "LLM request failed after 4 attempt(s) (status=503)"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert len(llm.calls) == 2
    assert len(p.edits) == 1 and p.code == "" and p.error == ""
    assert "feedback round failed (llm-error:" in p.analysis and "status=503" in caplog.text
    assert "the script could not be used" in p.no_script_reason


def test_a_feedback_transport_failure_without_usable_part_is_still_an_llm_error(cfg, proj):
    broken = "<analysis>a</analysis><script>\ndef fix(p):\n    p.log('x'\n</script>"
    llm = FinishLLM((broken, "stop"), LLMError(503, "", "LLM request failed after 4 attempt(s) (status=503)"))
    p = LLMFixer(cfg, llm).propose_script(proj, case_cluster(), sctx())
    assert p.code == "" and p.edits == [] and is_llm_transport_error(p.analysis)


# ---------------------------------------------------------------------------- K8
def test_a_script_tag_with_attributes_is_recognised():
    r = parse_script_response(f'<analysis>a</analysis>\n<script type="python">\n{CODE}</script>\n')
    assert r.code == CODE and r.warnings == []


def test_several_script_blocks_take_the_last_complete_one():
    draft = "def fix(p):\n    p.log('draft')\n"
    r = parse_script_response(f"<analysis>a</analysis>\n<script>\n{draft}</script>\nBetter:\n<script>\n{CODE}</script>\n")
    assert r.code == CODE and any("several <script> blocks (2)" in w for w in r.warnings)
    # the last block is not closed: the draft stays the script, the unclosed block is flagged (cut → K3)
    r = parse_script_response(f"<analysis>a</analysis>\n<script>\n{draft}</script>\n<script>\n{CODE}")
    assert r.code == draft and MISSING_CLOSE_SCRIPT in r.warnings and r.unclosed == ["script"]


def test_a_script_that_writes_a_script_tag_is_one_block():
    code = ("def fix(p):\n    p.replace('a.jsp', '<head>', '<head><script type=\"text/javascript\">x()</script>')\n")
    r = parse_script_response(f"<analysis>a</analysis>\n<script>\n{code}</script>\n")
    assert r.code == code and r.warnings == []


# ---------------------------------------------------------------------------- K6
def test_cut_chars_keeps_head_and_tail_and_names_the_gap():
    text = "A" * 5000 + "B" * 5000
    out = cut_chars(text, 1000)
    assert len(out) <= 1000 + 10 and out.startswith("A") and out.endswith("B") and "characters omitted" in out
    assert cut_chars("short", 1000) == "short"


def test_a_one_line_huge_file_is_cut_by_characters(cfg, proj, put):
    put("demo-back/src/main/resources/huge.properties", "k=" + "x" * 3_000_000 + "\n")
    small = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"max_context_chars": 20_000})})
    fixer = LLMFixer(small, FinishLLM(("x", "stop")))
    out = fixer.serve_context(proj, ["demo-back/src/main/resources/huge.properties"], [], 1)
    assert len(out) <= 20_000 and "characters omitted" in out and "cut to fit the context budget" in out
    many = [f"demo-back/src/main/resources/f{i}.properties" for i in range(6)]
    for f in many:
        put(f, "\n".join("line %d %s" % (n, "y" * 200) for n in range(250)) + "\n")
    out = fixer.serve_context(proj, many, [], 1)
    assert len(out) <= 20_000 and out.count("characters omitted") >= 6


def test_a_normal_file_is_served_as_before(cfg, proj):
    fixer = LLMFixer(cfg, FinishLLM(("x", "stop")))
    out = fixer.serve_context(proj, ["demo-back/pom.xml"], [], 1)
    assert "(whole file)\n```xml\n<project>\n  <artifactId>demo-back</artifactId>\n</project>\n```" in out
    assert "omitted" not in out
