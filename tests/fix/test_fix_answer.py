"""v2.1 fix answer (docs/v2/PLAN-v2.1.md §3.6): <analysis> 원인 · <solution> 해결 방법 · <edits> 수정 (JSON) · <script>
or <no_script> — parsing (sbmig.fix.script_protocol), the prompts' four-part instructions and mode texts, and
LLMFixer.propose_script → FixAnswer with the feedback-round rules."""
from __future__ import annotations

import json

import pytest

from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, Edit, ErrorCluster, ErrorKind, FixAnswer, Phase, ScriptBase
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.fix.fixer import load_script_prompts
from sbmig.fix.script_protocol import (EDITS_INVALID, EDITS_UNREADABLE, NO_SCRIPT, PROTOCOL_WARNING, load_edits_json,
                                       parse_edits, parse_script_response)
from sbmig.script import check_script, edits_script

CODE = ('def fix(p):\n    for e in p.errors:\n        path = e.get("file")\n        if path and p.exists(path):\n'
        '            p.replace(path, "import javax.annotation.PostConstruct;", '
        '"import jakarta.annotation.PostConstruct;")\n')
EDITS = [{"op": "replace", "path": "demo-back/src/main/java/a/S0.java",
          "old": "import javax.annotation.PostConstruct;", "new": "import jakarta.annotation.PostConstruct;"},
         {"op": "pom", "path": "demo-back/pom.xml",
          "add_dependency": {"group_id": "jakarta.annotation", "artifact_id": "jakarta.annotation-api", "version": None}}]
FULL = ("<analysis>javax.annotation left the JDK</analysis>\n"
        "<solution>switch the imports to jakarta.annotation and declare jakarta.annotation-api</solution>\n"
        f"<edits>\n{json.dumps(EDITS, indent=1)}\n</edits>\n<script>\n{CODE}</script>\n")
SIG = "compile|package_missing|javax.annotation"
FILES = [f"demo-back/src/main/java/a/S{i}.java" for i in range(2)]


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_all_four_parts():
    r = parse_script_response(FULL)
    assert r.analysis == "javax.annotation left the JDK"
    assert r.solution.startswith("switch the imports") and r.code == CODE and r.no_script_reason == ""
    assert r.edits == [Edit(op="replace", path=EDITS[0]["path"], old=EDITS[0]["old"], new=EDITS[0]["new"]),
                       Edit(op="pom", path="demo-back/pom.xml", pom={"add_dependency": EDITS[1]["add_dependency"]})]
    assert r.edits_block and r.edits_problem == "" and r.usable and r.warnings == []


def test_v2_script_only_answer_parses_as_before():
    r = parse_script_response(f"<analysis>a</analysis>\n<script>\n{CODE}</script>\n")
    assert r.code == CODE and r.solution == "" and r.edits == [] and not r.edits_block and r.edits_problem == ""
    assert r.no_script_reason == ""


def test_no_script_with_edits_is_usable():
    r = parse_script_response("<analysis>a</analysis><solution>s</solution>"
                              f"<edits>{json.dumps(EDITS[:1])}</edits><no_script>the class is this project's own"
                              "</no_script>")
    assert r.code == "" and r.problem == NO_SCRIPT and len(r.edits) == 1 and r.usable
    assert r.no_script_reason == "the class is this project's own"
    assert parse_script_response("<no_script></no_script>").no_script_reason == "(no reason given)"


@pytest.mark.parametrize("body", [
    '```json\n[{"op": "delete", "path": "a.txt"},]\n```',                        # fence + trailing comma
    '[{"op": "delete", "path": "a.txt"} // the old file\n]',                     # comment
    "[{'op': 'delete', 'path': 'a.txt', 'note': None}]",                         # Python literal
    'Here it is: [{"op": "delete", "path": "a.txt"}] — done.',                   # prose around
    '{"edits": [{"op": "delete", "path": "a.txt"}]}',                            # wrapped
])
def test_edits_json_repair(body):
    edits, problem = parse_edits(body)
    assert problem == "" and edits == [Edit(op="delete", path="a.txt")], problem


def test_edits_json_repair_inside_strings():
    raw = '[{"op": "replace", "path": "A.java", "old": "if (s.matches("\\\\d+")) {\n    go();", "new": "x"}]'
    edits, problem = parse_edits(raw.replace('"\\\\d+"', '\\"\\d+\\"'))   # raw newline + invalid escape \d
    assert problem == "" and edits[0].old == 'if (s.matches("\\d+")) {\n    go();'
    obj, err = load_edits_json("[{'old': 'it\\'s', \"new\": 'a \"b\"'}]")
    assert err == "" and obj == [{"old": "it's", "new": 'a "b"'}]


def test_unreadable_and_invalid_edits():
    r = parse_script_response('<analysis>a</analysis><edits>[{"op": "replace" "path": "x"}]</edits>')
    assert r.edits == [] and r.edits_problem.startswith(EDITS_UNREADABLE + ": Expecting ',' delimiter") and not r.usable
    r = parse_script_response('<analysis>a</analysis><edits>[{"op": "patch", "path": "x"}]</edits>')
    assert r.edits == [] and r.edits_problem == (f"{EDITS_INVALID}: edit 1: unknown op 'patch' (allowed: replace, "
                                                 "write, create, delete, move, pom)")
    r = parse_script_response('<edits>[{"op": "pom", "path": "pom.xml", "add_plugin": {}}]</edits>')
    assert "unknown pom call 'add_plugin'" in r.edits_problem
    r = parse_script_response('<edits>[{"op": "write", "path": "/etc/hosts", "content": ""}]</edits>')
    assert "absolute path" in r.edits_problem
    empty = parse_script_response("<analysis>a</analysis><edits>\n</edits><edits>[]</edits>")
    assert empty.edits_block and empty.edits == [] and empty.edits_problem == ""
    assert PROTOCOL_WARNING not in empty.warnings


def test_a_script_tag_inside_an_edit_or_the_analysis_never_opens_the_script():
    jsp = [{"op": "replace", "path": "web/a.jsp", "old": '<script src="old.js"></script>',
            "new": '<script src="new.js"></script>'}]
    text = (f"<analysis>the JSP loads old.js with a <script> tag</analysis>\n<edits>{json.dumps(jsp)}</edits>\n"
            "<no_script>one JSP of this project</no_script>")
    r = parse_script_response(text)
    assert r.code == "" and r.candidate == "" and r.edits[0].old == '<script src="old.js"></script>'
    r = parse_script_response(text.replace("<no_script>one JSP of this project</no_script>",
                                           f"<script>\n{CODE}</script>"))
    assert r.code == CODE and len(r.edits) == 1


def test_missing_closing_tags_end_at_the_next_part():
    r = parse_script_response("<analysis>cause\n<solution>how\n<edits>[]\n<script>\n" + CODE)
    assert r.analysis == "cause" and r.solution == "how" and r.edits_block and r.code == CODE


def test_synthesized_edits_script_of_a_parsed_answer_passes_the_check():
    r = parse_script_response(FULL)
    assert check_script(edits_script(r.edits)) == []


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def test_system_prompt_teaches_the_four_parts():
    system, _ = load_script_prompts("v1")
    for part in ("<analysis>원인", "<solution>해결 방법", "<edits>", "<script>", "<no_script>", "원인 / cause",
                 "해결 방법 / solution", "수정 / edits", "스크립트 / script", "## Edits (`<edits>`)",
                 "`old` = the EXACT current text", "object per change, applied in order", "relative to the project root",
                 "Change poms only through `op: pom`", '"add_dependency"', '"set_property"', '`"version": null`',
                 "## Script or no script", "fixes the same case in OTHER", "idempotent",
                 "this project's own code", "whole step fails and is rolled back"):
        assert part in system, part
    assert "{{" not in system and "{%" not in system


def test_user_prompt_mode_texts(cfg, ws, put):
    for i, f in enumerate(FILES):
        put(f, f"package a;\n\nimport javax.annotation.PostConstruct;\n\nclass S{i} {{}}\n")
    put("demo-back/pom.xml", "<project><artifactId>demo-back</artifactId></project>\n")
    client = FakeLLMClient([FULL, FULL])
    fixer = LLMFixer(cfg, client)
    fixer.propose_script(ws, cluster(), ctx())
    new = client.calls[0]["messages"][1].content
    assert new.startswith("Fix this case now and write ONE fix script for it.")
    assert "BOTH `<edits>` that fix\nthis project now" in new and "`<no_script>` with the reason" in new
    base = ScriptBase(case_id="abc123", version=1, code="def fix(p):\n    p.log('x')\n", outcome="no_change",
                      status="ok")
    fixer.propose_script(ws, cluster(), ctx(script_mode="augment", script_base=base))
    aug = client.calls[1]["messages"][1].content
    assert aug.startswith("Extend the fix script of this case (version 1)") and "COMPLETE new script" in aug
    assert "`<no_script>` with the reason and `<edits>` that fix this project directly" in aug
    assert "## Script to extend (version 1)" in aug and "outcome in this project: no_change" in aug


# ---------------------------------------------------------------------------
# propose_script
# ---------------------------------------------------------------------------
def cluster() -> ErrorCluster:
    errs = [BuildError(kind=ErrorKind.COMPILE, message="package javax.annotation does not exist", file=f, line=3,
                       column=24, module="demo-back", signature=f"s{i}", signature_generic=SIG)
            for i, f in enumerate(FILES)]
    return ErrorCluster(id="case:abc123", kind=ErrorKind.COMPILE, errors=errs, case_id="abc123",
                        signatures=[e.signature for e in errs])


def ctx(**kw) -> FixContext:
    base = dict(phase=Phase.VERIFY, iteration=1, profile_summary="case=spring_legacy java=1.8", script_mode="new",
                case_id="abc123", signature_generic=SIG)
    base.update(kw)
    return FixContext(**base)


@pytest.fixture()
def proj(ws, put):
    for i, f in enumerate(FILES):
        put(f, f"package a;\n\nimport javax.annotation.PostConstruct;\n\nclass S{i} {{}}\n")
    put("demo-back/pom.xml", "<project><artifactId>demo-back</artifactId></project>\n")
    return ws


def test_answer_with_all_four_parts(cfg, proj):
    client = FakeLLMClient([FULL])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert isinstance(a, FixAnswer) and len(client.calls) == 1
    assert a.analysis == "javax.annotation left the JDK" and a.solution.startswith("switch the imports")
    assert a.code == CODE and len(a.edits) == 2 and a.edits[1].op == "pom" and a.no_script_reason == ""
    assert a.error == "" and a.raw_response == FULL
    ev = [json.loads(ln) for ln in proj.journal_path.read_text().splitlines() if '"llm_script"' in ln][-1]
    assert ev["edits"] == 2 and ev["solution_chars"] == len(a.solution) and ev["no_script"] is False


def test_no_script_with_edits_needs_no_feedback(cfg, proj):
    ans = (f"<analysis>a</analysis><solution>s</solution><edits>{json.dumps(EDITS[:1])}</edits>"
           "<no_script>project-specific helper class</no_script>")
    client = FakeLLMClient([ans])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and a.code == "" and len(a.edits) == 1 and a.error == ""
    assert a.no_script_reason == "project-specific helper class"


def test_no_script_without_edits_is_no_answer(cfg, proj):
    client = FakeLLMClient(["<analysis>cannot</analysis><no_script>the class was deleted upstream</no_script>"])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and a.code == "" and a.edits == []
    assert a.error == "no script and no edits: the class was deleted upstream"


def test_unreadable_edits_get_one_feedback_round(cfg, proj):
    bad = FULL.replace('"op": "pom",', '"op": "pom"')
    client = FakeLLMClient([bad, FULL])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2 and a.llm_usage.calls == 2
    feedback = client.calls[1]["messages"][-1].content
    assert feedback.startswith("Your answer cannot be used. Problems:\n\n- edits JSON unreadable: ")
    assert "ONE valid JSON list" in feedback and "COMPLETE corrected answer" in feedback
    assert "<solution>, <edits> and <script>" in feedback
    assert len(a.edits) == 2 and a.code == CODE and a.error == ""


def test_edits_still_unreadable_keep_the_script(cfg, proj):
    bad = FULL.replace('"op": "pom",', '"op": "pom"')
    a = LLMFixer(cfg, FakeLLMClient([bad])).propose_script(proj, cluster(), ctx())
    assert a.code == CODE and a.edits == [] and a.error == ""
    assert "[sbmig] edits dropped: edits JSON unreadable" in a.analysis


def test_broken_script_next_to_valid_edits(cfg, proj):
    broken = FULL.replace(CODE, "def fix(p):\n    p.log('x'\n")
    client = FakeLLMClient([broken])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2                                    # one feedback round for the script
    assert "script does not parse" in client.calls[1]["messages"][-1].content
    assert a.code == "" and len(a.edits) == 2 and a.error == ""      # the edits are still the fix
    assert a.no_script_reason.startswith("[sbmig] the script could not be used: script does not parse")
    assert "[sbmig] script dropped: script does not parse" in a.analysis
    fixed = LLMFixer(cfg, FakeLLMClient([broken, FULL])).propose_script(proj, cluster(), ctx())
    assert fixed.code == CODE and fixed.no_script_reason == "" and "[sbmig]" not in fixed.analysis


def test_nothing_usable_after_the_feedback_round(cfg, proj):
    bad = "<analysis>a</analysis><edits>[{'op': 'patch'}]</edits>"
    client = FakeLLMClient([bad])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2 and a.code == "" and a.edits == []
    assert a.error.startswith("edits invalid: edit 1: unknown op 'patch'")
    assert "[sbmig] feedback round did not give a usable script or edits" in a.analysis


def test_augment_answer_with_no_script_and_edits(cfg, proj):
    base = ScriptBase(case_id="abc123", version=2, code=CODE, outcome="no_change", status="ok")
    ans = (f"<analysis>a</analysis><solution>s</solution><edits>{json.dumps(EDITS[:1])}</edits>"
           "<no_script>the remaining file needs a hand-written port</no_script>")
    client = FakeLLMClient([ans])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base))
    assert len(client.calls) == 1 and a.code == "" and len(a.edits) == 1 and a.error == ""
    assert a.no_script_reason == "the remaining file needs a hand-written port"
    # an identical script next to edits: one feedback round (augment wording), then the edits stay the fix
    same = FULL
    client = FakeLLMClient([same])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base))
    assert len(client.calls) == 2 and "identical to the version it should extend" in client.calls[1]["messages"][-1].content
    assert "the COMPLETE new <script>" in client.calls[1]["messages"][-1].content
    assert a.code == "" and len(a.edits) == 2 and a.no_script_reason.startswith("[sbmig] the script could not be used")


def test_context_request_wins_over_a_broken_edits_block(cfg, proj):
    ask = "<analysis>need the base</analysis><edits>[oops</edits><need_files>demo-back/pom.xml</need_files>"
    client = FakeLLMClient([ask, FULL])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2 and "Here is the requested context." in client.calls[1]["messages"][-1].content
    assert a.code == CODE and len(a.edits) == 2
