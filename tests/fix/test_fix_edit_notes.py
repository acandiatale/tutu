"""v2.1b (2026-10-27): every modification described for the business team — each `<edits>` item's `desc` and, with a
script, the `<changes>` list (`{"path", "where", "change"}`) → `FixAnswer.changes` (EditNote); the prompts teach the
"<파일> 의 <위치> 에서 <무엇을> <어떻게> 수정합니다" rule in the report language; a script without `<changes>` is not an
error (an `[sbmig]` note in `analysis`); v2 / v2.1 answers parse as before (changes [])."""
from __future__ import annotations

import json
import re

from sbmig.core.models import EditNote
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.fix.fixer import describe_language, load_script_prompts
from sbmig.fix.script_protocol import CHANGES_MISSING, parse_changes, parse_script_response
from sbmig.script import check_script, edits_script

from .test_fix_answer import CODE, FILES, FULL, cluster, ctx, proj  # noqa: F401 - `proj` is a fixture

POM_DESC = "demo-back/pom.xml 의 <dependencies> 끝에 jakarta.annotation-api 의존성을 버전 없이 추가합니다"
S0_DESC = f"{FILES[0]} 의 import 줄에서 javax.annotation.PostConstruct 를 jakarta.annotation.PostConstruct 로 바꿉니다"
EDITS = [{"op": "replace", "path": FILES[0], "desc": S0_DESC,
          "old": "import javax.annotation.PostConstruct;", "new": "import jakarta.annotation.PostConstruct;"},
         {"op": "pom", "path": "demo-back/pom.xml", "desc": POM_DESC,
          "add_dependency": {"group_id": "jakarta.annotation", "artifact_id": "jakarta.annotation-api",
                             "version": None}}]
CHANGES = [{"path": FILES[0], "where": "import 줄", "change": "javax.annotation 을 jakarta.annotation 으로 바꿉니다"},
           {"path": FILES[1], "where": "import 줄 (<script> 아님)", "change": "같은 import 를 바꿉니다"}]
DESCRIBED = ("<analysis>javax.annotation left the JDK</analysis>\n<solution>switch to jakarta</solution>\n"
             f"<edits>\n{json.dumps(EDITS, ensure_ascii=False, indent=1)}\n</edits>\n"
             f"<changes>\n{json.dumps(CHANGES, ensure_ascii=False)}\n</changes>\n<script>\n{CODE}</script>\n")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_desc_on_edits_and_changes_with_a_script():
    r = parse_script_response(DESCRIBED)
    assert r.code == CODE and r.warnings == [] and [e.desc for e in r.edits] == [S0_DESC, POM_DESC]
    assert r.edits[1].pom == {"add_dependency": EDITS[1]["add_dependency"]}     # "desc" is no pom call
    assert r.changes_block and r.changes_problem == "" and r.described and r.changes_note() == ""
    # the <changes> list first; then the desc of every edit whose path the list did not describe (the pom)
    assert r.changes == [EditNote(path=FILES[0], where="import 줄", change=CHANGES[0]["change"]),
                         EditNote(path=FILES[1], where="import 줄 (<script> 아님)", change="같은 import 를 바꿉니다"),
                         EditNote(path="demo-back/pom.xml", change=POM_DESC)]
    assert all(n.source == "llm" and n.lines == "" for n in r.changes)


def test_edits_alone_give_their_descs_as_notes():
    text = (f"<analysis>a</analysis><solution>s</solution><edits>{json.dumps(EDITS, ensure_ascii=False)}</edits>"
            "<no_script>one-off</no_script>")
    r = parse_script_response(text)
    assert not r.changes_block and not r.described and r.code == ""
    assert [(n.path, n.change, n.where) for n in r.changes] == [(FILES[0], S0_DESC, ""),
                                                                ("demo-back/pom.xml", POM_DESC, "")]
    # <changes> next to edits may summarize them; an edit on a path it names adds no second note
    summary = [{"file": "./demo-back/pom.xml", "location": "<dependencies>", "desc": "의존성 추가"}]
    r = parse_script_response(text + f"<changes>{json.dumps(summary, ensure_ascii=False)}</changes>")
    assert [(n.path, n.where, n.change) for n in r.changes] == [("demo-back/pom.xml", "<dependencies>", "의존성 추가"),
                                                                (FILES[0], "", S0_DESC)]


def test_legacy_answers_have_no_changes():
    for text in (f"<analysis>a</analysis>\n<script>\n{CODE}</script>\n", FULL,
                 "<analysis>cannot</analysis><no_script>gone</no_script>"):
        r = parse_script_response(text)
        assert r.changes == [] and not r.changes_block and not r.described and r.changes_problem == ""
    assert parse_script_response(FULL).changes_note() == CHANGES_MISSING


def test_changes_tolerant_forms():
    # JSON slips repaired like the edits; one object; a {"changes": [...]} wrapper; numbers as the location
    notes, problem = parse_changes('```json\n[{"path": "a/B.java", "where": "import", "change": "x",},]\n```')
    assert problem == "" and notes == [EditNote(path="a/B.java", where="import", change="x")]
    assert parse_changes('{"path": "pom.xml", "change": "add"}')[0] == [EditNote(path="pom.xml", change="add")]
    notes, _ = parse_changes('{"changes": [{"path": "p.xml", "line": 42, "what": ["a", "b"]}]}')
    assert notes == [EditNote(path="p.xml", where="42행 부근", change="a; b")]
    # a bullet list instead of JSON
    notes, problem = parse_changes("- a/B.java: import 줄에서 javax 를 jakarta 로 바꿉니다\n"
                                   "- pom.xml 의 <dependencies> 끝에 X 를 추가합니다\n")
    assert problem == "" and [(n.path, n.change) for n in notes] == [
        ("a/B.java", "import 줄에서 javax 를 jakarta 로 바꿉니다"),
        ("pom.xml", "pom.xml 의 <dependencies> 끝에 X 를 추가합니다")]
    assert parse_changes("") == ([], "") and parse_changes("[]") == ([], "")
    # an item without text is skipped with a problem; the answer is never refused for its notes
    notes, problem = parse_changes('[{"path": "a"}, {"path": "b", "change": "c"}]')
    assert notes == [EditNote(path="b", change="c")] and "change 1: no 'where' / 'change' text" in problem


def test_unreadable_changes_are_a_note_not_an_error():
    text = f"<analysis>a</analysis><changes>[{{]</changes><script>\n{CODE}</script>"
    r = parse_script_response(text)
    assert r.code == CODE and r.changes_block and r.changes == [] and not r.described
    assert r.changes_problem.startswith("changes unreadable: ")
    assert r.changes_note().startswith(CHANGES_MISSING + " (")
    assert any(w.startswith("<changes>:") for w in r.warnings)


def test_a_script_tag_inside_the_changes_never_opens_the_script():
    changes = [{"path": "web/a.jsp", "where": "<script> 블록", "change": "javax 를 jakarta 로 바꿉니다"}]
    text = (f"<analysis>a</analysis><changes>{json.dumps(changes, ensure_ascii=False)}</changes>\n"
            f"<script>\n{CODE}</script>")
    r = parse_script_response(text)
    assert r.code == CODE and r.changes[0].where == "<script> 블록"


def comment_above(code: str, n: int) -> str:
    """The desc comment lines of edit `n` in a synthesized script, joined."""
    lines = code.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f"    # edit {n}: "))
    out = []
    for ln in lines[i + 1:]:
        if not ln.startswith("    # "):
            break
        out.append(ln[len("    # "):])
    return " ".join(out)


def test_desc_survives_the_synthesized_script_and_its_check():
    r = parse_script_response(DESCRIBED)
    code = edits_script(r.edits)
    assert check_script(code) == [] and comment_above(code, 1) == S0_DESC and comment_above(code, 2) == POM_DESC
    assert re.search(r"# edit 2: pom demo-back/pom.xml\n    # demo-back/pom.xml 의 <dependencies>", code)


# ---------------------------------------------------------------------------
# propose_script
# ---------------------------------------------------------------------------
def test_answer_carries_the_change_notes(cfg, proj):  # noqa: F811
    a = LLMFixer(cfg, FakeLLMClient([DESCRIBED])).propose_script(proj, cluster(), ctx())
    assert a.code == CODE and len(a.edits) == 2 and a.error == ""
    assert a.analysis == "javax.annotation left the JDK"                     # described: no [sbmig] note
    assert [n.path for n in a.changes] == [FILES[0], FILES[1], "demo-back/pom.xml"]
    assert a.edits[0].desc == S0_DESC


def test_script_without_changes_gets_a_note_not_an_error(cfg, proj):  # noqa: F811
    client = FakeLLMClient([FULL])
    a = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and a.code == CODE and a.error == ""      # no feedback round for missing notes
    assert a.analysis.endswith("[sbmig] the answer did not describe its changes") and a.changes == []
    # edits only (with descs, no script): nothing missing
    text = (f"<analysis>a</analysis><solution>s</solution><edits>{json.dumps(EDITS, ensure_ascii=False)}</edits>"
            "<no_script>one-off</no_script>")
    a = LLMFixer(cfg, FakeLLMClient([text])).propose_script(proj, cluster(), ctx())
    assert a.analysis == "a" and [n.change for n in a.changes] == [S0_DESC, POM_DESC]
    # nothing usable → no notes either
    nothing = '<analysis>no</analysis><changes>[{"path": "x", "change": "y"}]</changes>'
    a = LLMFixer(cfg, FakeLLMClient([nothing])).propose_script(proj, cluster(), ctx())
    assert a.error and a.changes == []


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def test_system_prompt_teaches_the_description_rule():
    system, _ = load_script_prompts("v1")
    for part in ("business team", "<파일> 의 <위치> 에서 <무엇을> <어떻게> 수정합니다", '`desc` on EVERY `<edits>` object',
                 "`<changes>` is REQUIRED whenever you write a `<script>`", '{"path": "<file>", "where":',
                 'Every object also takes `"desc"`', "Korean when it names none", "## Describing every change"):
        assert part in system, part
    # the output example itself parses: every edit has a desc, the changes list describes the script
    example = system[system.index("## Output format"):]
    r = parse_script_response(example[example.index("<analysis>"):example.index("The script must define")])
    assert r.code and len(r.edits) == 2 and all(e.desc for e in r.edits) and r.described
    assert [n.path for n in r.changes[:2]] == ["demo-back/src/main/java/a/S0.java", "demo-back/src/main/java/a/S1.java"]


def test_user_prompt_names_the_language(cfg, proj):  # noqa: F811
    cfg.report.language = "ko"
    client = FakeLLMClient([DESCRIBED])
    LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    user = client.calls[0]["messages"][1].content
    assert "The business team reads the record of every modification" in user
    assert '"<파일> 의 <위치> 에서 <무엇을> <어떻게> 수정합니다", written in Korean (한국어).' in user
    assert describe_language(cfg) == "Korean (한국어)"
    cfg.report.language = "en"
    assert describe_language(cfg).startswith("the language of the project's code comments — Korean (한국어)")
    assert describe_language(None).startswith("the language of the project's code comments")
