"""v2 fix scripts: answer parsing (sbmig.fix.script_protocol) and LLMFixer.propose_script (ScriptFixerLike)."""
import json

import pytest

from sbmig.core.interfaces import FixContext, FixerLike, ScriptFixerLike
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, Phase, ScriptBase
from sbmig.fix import FakeLLMClient, LLMFixer, RecordingClient
from sbmig.fix.client import LLMError
from sbmig.fix.fixer import load_script_prompts
from sbmig.fix.script_protocol import (
    NO_FIX_DEF,
    NO_SCRIPT,
    PATCHES_WARNING,
    PROTOCOL_WARNING,
    clean_code,
    parse_script_response,
    script_problem,
)
from tests.fix.conftest import make_cluster

CODE = ('import re\n\n\ndef fix(p):\n    for e in p.errors:\n        path = e.get("file")\n'
        '        if path and p.exists(path):\n'
        '            n = p.replace(path, "import javax.annotation.PostConstruct;", '
        '"import jakarta.annotation.PostConstruct;")\n'
        '            p.log(f"{path}: {n}")\t# tab + ünïcode kept\n')
GOOD = f"<analysis>javax.annotation moved to jakarta</analysis>\n<script>\n{CODE}</script>\n"
OTHER = GOOD.replace('p.log(f"{path}: {n}")', 'p.log(f"{path}: PostConstruct ({n})")')
SIG = "compile|package_missing|javax.annotation"


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_parse_keeps_the_code_byte_exact():
    r = parse_script_response(GOOD)
    assert r.code == CODE and r.problem == "" and r.analysis == "javax.annotation moved to jakarta"
    assert not r.wants_context and r.warnings == []


def test_parse_fence_inside_script_and_crlf():
    fenced = GOOD.replace("<script>\n", "<script>\n```python\n").replace("</script>", "```\n</script>")
    assert parse_script_response(fenced).code == CODE
    assert parse_script_response(fenced.replace("\n", "\r\n")).code == CODE
    tilde = "<analysis>a</analysis><script>\n~~~py\ndef fix(p):\n    pass\n~~~\n</script>"
    assert parse_script_response(tilde).code == "def fix(p):\n    pass\n"


def test_parse_dedents_a_block_indented_as_a_whole():
    r = parse_script_response("<analysis>a</analysis>\n<script>\n    def fix(p):\n        p.log('x')\n\n"
                              "    def helper():\n        return 1\n</script>")
    assert r.code == "def fix(p):\n    p.log('x')\n\ndef helper():\n    return 1\n"
    assert clean_code("def fix(p):\n    pass") == "def fix(p):\n    pass\n"       # not indented: untouched


def test_parse_missing_script_keeps_the_analysis():
    r = parse_script_response("<analysis>no safe script: the class was deleted upstream</analysis>")
    assert r.code == "" and r.problem == NO_SCRIPT and r.analysis.startswith("no safe script")
    assert PROTOCOL_WARNING not in r.warnings


def test_parse_script_without_fix_or_not_parsing():
    r = parse_script_response("<analysis>a</analysis><script>\ndef repair(p):\n    pass\n</script>")
    assert r.code == "" and r.problem == NO_FIX_DEF and r.candidate.startswith("def repair")
    nested = parse_script_response("<analysis>a</analysis><script>\nclass X:\n    def fix(self, p):\n        pass\n"
                                   "</script>")
    assert nested.problem == NO_FIX_DEF                                   # only a module-level def counts
    bad = parse_script_response("<analysis>a</analysis><script>\ndef fix(p):\n    p.log('x'\n</script>")
    assert bad.code == "" and bad.problem.startswith("script does not parse: SyntaxError") and "(line 2)" in bad.problem
    assert script_problem("") == NO_SCRIPT


def test_parse_closing_tag_inside_the_code_and_missing_closing_tag():
    jsp = ("<analysis>jsp</analysis>\n<script>\ndef fix(p):\n    snippet = '''\n</script>\n'''\n"
           "    p.log(snippet)\n</script>\n")
    r = parse_script_response(jsp)
    assert r.problem == "" and r.code == "def fix(p):\n    snippet = '''\n</script>\n'''\n    p.log(snippet)\n"
    cut = parse_script_response("<analysis>a</analysis>\n<script>\ndef fix(p):\n    p.log('x')\n")
    assert cut.code == "def fix(p):\n    p.log('x')\n" and any("missing </script>" in w for w in cut.warnings)


def test_parse_analysis_may_mention_the_script_tag():
    r = parse_script_response("<analysis>I return a <script> that edits imports</analysis>\n" + GOOD.split("\n", 1)[1])
    assert r.code == CODE and "<script>" in r.analysis


def test_parse_need_file_and_need_grep():
    r = parse_script_response("<analysis>need the base class</analysis><need_file>a/Base.java, ../x</need_file>"
                              "<need_grep>extends\\s+Base\n</need_grep>")
    assert r.need_files == ["a/Base.java"] and r.need_grep == ["extends\\s+Base"] and r.wants_context
    assert any("'..'" in w for w in r.warnings)
    assert parse_script_response("<need_files>A.java\nB.java</need_files>").need_files == ["A.java", "B.java"]


def test_parse_fenced_block_without_tag_prose_and_patches():
    r = parse_script_response("Here you go:\n```python\ndef fix(p):\n    p.log('x')\n```\nDone.")
    assert r.code == "def fix(p):\n    p.log('x')\n" and any("fenced python block" in w for w in r.warnings)
    bare = parse_script_response("def fix(p):\n    p.log('x')\n")
    assert bare.code == "def fix(p):\n    p.log('x')\n"
    prose = parse_script_response("Sure! Just change the import.")
    assert prose.code == "" and PROTOCOL_WARNING in prose.warnings and prose.analysis == "Sure! Just change the import."
    v1 = parse_script_response("<analysis>x</analysis><patches>\n=== MODIFY a/A.java\n<<<<<<< SEARCH\na\n=======\nb\n"
                               ">>>>>>> REPLACE\n</patches>")
    assert v1.code == "" and v1.used_patches and PATCHES_WARNING in v1.warnings


# ---------------------------------------------------------------------------
# propose_script
# ---------------------------------------------------------------------------
FILES = [f"demo-back/src/main/java/a/S{i}.java" for i in range(3)]


def src(i: int) -> str:
    return f"package a;\n\nimport javax.annotation.PostConstruct;\n\npublic class S{i} {{\n    @PostConstruct\n" \
           "    void init() {}\n}\n"


def case_cluster(files=FILES, case_id="abc123") -> ErrorCluster:
    errs = [BuildError(kind=ErrorKind.COMPILE, message="package javax.annotation does not exist", file=f, line=3,
                       column=24, module="demo-back", signature=f"s{i}", signature_generic=SIG)
            for i, f in enumerate(files)]
    return ErrorCluster(id=f"case:{case_id}", kind=ErrorKind.COMPILE, errors=errs, case_id=case_id,
                        signatures=[e.signature for e in errs])


def sctx(**kw) -> FixContext:
    base = dict(phase=Phase.VERIFY, iteration=1, profile_summary="case=spring_legacy java=1.8", script_mode="new",
                case_id="abc123", signature_generic=SIG)
    base.update(kw)
    return FixContext(**base)


@pytest.fixture()
def proj(ws, put):
    for i, f in enumerate(FILES):
        put(f, src(i))
    put("demo-back/pom.xml", "<project>\n  <artifactId>demo-back</artifactId>\n</project>\n")
    put("pom.xml", "<project>\n  <artifactId>demo</artifactId>\n</project>\n")
    put("demo-back/src/main/java/a/Base.java", "package a;\n\npublic abstract class Base {\n}\n")
    return ws


def journal(ws) -> list[dict]:
    return [json.loads(ln) for ln in ws.journal_path.read_text().splitlines()]


def test_prompts_loaded_with_the_helper_api():
    system, user_t = load_script_prompts("v1")
    assert "Python fix script" in system and "def fix(p)" in system and "<script>" in system
    assert "p.pom(path=\"pom.xml\")" in system and "p.errors" in system and "`fnmatch`" in system
    assert "idempotent" in system.lower() and "COMPLETE new script" in system and "src/test/" in system
    assert "{{" not in system and "{%" not in system
    with pytest.raises(FileNotFoundError):
        load_script_prompts("v999")


def test_prompt_fallback_table_when_sbmig_script_is_missing(monkeypatch):
    import sbmig.fix.fixer as fixer_mod

    monkeypatch.setattr(fixer_mod, "_script_module_attr", lambda name: None)
    load_script_prompts.cache_clear()
    try:
        system, _ = load_script_prompts("v1", ("xml.etree.ElementTree",))
    finally:
        load_script_prompts.cache_clear()
    assert "| `p.pom(path=\"pom.xml\")` |" in system and "`xml.etree.ElementTree`" in system
    assert "`posixpath`" in system and "set_plugin_version" in system


def test_propose_script_new(cfg, proj):
    client = FakeLLMClient([GOOD])
    fixer = LLMFixer(cfg, client)
    assert isinstance(fixer, ScriptFixerLike) and isinstance(fixer, FixerLike)
    p = fixer.propose_script(proj, case_cluster(), sctx())
    assert len(client.calls) == 1
    call = client.calls[0]
    msgs = call["messages"]
    assert [m.role for m in msgs] == ["system", "user"]
    assert "p.replace(path, old, new" in msgs[0].content                      # the helper API table
    user = msgs[1].content
    assert user.startswith("Write ONE fix script for this case")
    assert "case: abc123 — L2 signature: " + SIG in user and "errors: 3 in 3 file(s)" in user
    for f in FILES:                                                           # every erroring file: error line + text
        assert f"- {f}:3:24: package javax.annotation does not exist" in user
        assert f"// {f} lines 1-8 (whole file)" in user
    assert "## POM (demo-back/pom.xml)" in user                               # "package … does not exist" → pom
    assert "Fix policy" not in user                                           # the v1 always-note stays out
    assert call["role"] == "fix" and call["max_tokens"] == cfg.llm.max_output_tokens
    assert call["metadata"]["script_mode"] == "new" and call["metadata"]["case_id"] == "abc123"
    assert call["metadata"]["cluster_id"] == "case:abc123" and call["metadata"]["run_id"] == "t-run"
    assert p.code == CODE and p.error == "" and p.analysis == "javax.annotation moved to jakarta"
    assert p.llm_usage.calls == 1 and p.llm_usage.input_tokens > 0 and len(p.prompt_hash) == 64
    assert p.raw_response == GOOD and p.model == cfg.llm.model
    ev = [e for e in journal(proj) if e["event"] == "llm_script"][-1]
    assert ev["case_id"] == "abc123" and ev["mode"] == "new" and ev["code_chars"] == len(CODE)
    assert ev["error"] is None and ev["files_shown"] >= 3


def test_propose_script_augment(cfg, proj):
    base = ScriptBase(case_id="abc123", version=2, code="def fix(p):\n    p.log('only S0')\n", outcome="partial",
                      status="ok", log=["S0.java: import"], changed_files=[FILES[0]],
                      remaining_errors=[f"{FILES[1]}:3 package javax.annotation does not exist"],
                      introduced_errors=[f"{FILES[0]}:6 cannot find symbol PostConstruct"],
                      history=["v1: 0 ok / 1 failed — no file changed"])
    client = FakeLLMClient([GOOD])
    p = LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx(script_mode="augment", script_base=base))
    user = client.calls[0]["messages"][1].content
    assert user.startswith("Extend the fix script of this case (version 2)") and "COMPLETE new script" in user
    assert "WITHOUT its changes" in user
    assert "## Script to extend (version 2)" in user and "p.log('only S0')" in user
    assert "outcome in this project: partial" in user and "S0.java: import" in user
    assert f"- {FILES[1]}:3 package javax.annotation does not exist" in user
    assert "cannot find symbol PostConstruct" in user and "v1: 0 ok / 1 failed" in user
    assert user.index("Script to extend") < user.index("Source (exact text")
    assert client.calls[0]["metadata"]["script_mode"] == "augment"
    assert client.calls[0]["metadata"]["script_base_version"] == 2
    assert p.code == CODE and p.error == ""


def test_augment_of_a_kept_base_says_its_changes_are_still_in_the_tree(cfg, proj):
    """An improved base stays in the tree (the loop rolls back only what did not work): the prompt must not claim it
    was rolled back, and the new script must also be a no-op for what is already applied."""
    base = ScriptBase(case_id="abc123", version=1, code="def fix(p):\n    p.log('S0')\n", outcome="improved",
                      status="ok", remaining_errors=[f"{FILES[1]}:3 package javax.annotation does not exist"])
    client = FakeLLMClient([GOOD])
    LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx(script_mode="augment", script_base=base))
    user = client.calls[0]["messages"][1].content
    assert "STILL in the tree" in user and "idempotent" in user and "WITHOUT its changes" not in user


def test_augment_returning_the_same_script_gets_one_feedback_round(cfg, proj):
    base = ScriptBase(case_id="abc123", version=1, code=CODE, outcome="no_change", status="ok")
    client = FakeLLMClient([GOOD, OTHER])
    p = LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx(script_mode="augment", script_base=base))
    assert len(client.calls) == 2 and "identical to the version it should extend" in client.calls[1]["messages"][-1].content
    assert p.code != CODE and "PostConstruct ({n})" in p.code and p.llm_usage.calls == 2
    same = LLMFixer(cfg, FakeLLMClient([GOOD])).propose_script(proj, case_cluster(),
                                                               sctx(script_mode="augment", script_base=base))
    assert same.code == "" and same.error.startswith("the script is identical")


def test_augment_without_base_asks_for_a_new_script(cfg, proj):
    client = FakeLLMClient([GOOD])
    p = LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx(script_mode="augment"))
    assert client.calls[0]["messages"][1].content.startswith("Write ONE fix script")
    assert client.calls[0]["metadata"]["script_mode"] == "new" and p.code == CODE


def test_need_files_round_then_script(cfg, proj):
    client = FakeLLMClient([("<analysis>need the base class</analysis><need_files>Base.java</need_files>"
                             "<need_grep>@PostConstruct</need_grep>"), GOOD])
    p = LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx())
    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert [m.role for m in second] == ["system", "user", "assistant", "user"]
    served = second[3].content
    assert "public abstract class Base" in served and "grep /@PostConstruct/ → 3 matching line(s)" in served
    assert "prefer answering with <script> now" in served and "<patches>" not in served
    assert p.code == CODE and p.requested_files == ["Base.java"] and p.llm_usage.calls == 2


def test_still_requesting_context_after_the_rounds(cfg, proj):
    ask = "<analysis>need more</analysis><need_files>Base.java</need_files>"
    client = FakeLLMClient([ask])
    p = LLMFixer(cfg, client).propose_script(proj, case_cluster(), sctx())
    assert len(client.calls) == 1 + cfg.llm.max_context_rounds
    assert "This was the LAST context round. Answer now with <analysis> and <script>" in \
        client.calls[-1]["messages"][-1].content
    assert p.code == "" and p.error.startswith("still requesting context") and "(still requesting context" in p.analysis


def test_static_check_rejection_gets_one_feedback_round(cfg, proj):
    seen: list[str] = []

    def checker(code: str) -> list[str]:
        seen.append(code)
        return ["line 1: import of 'os' is not allowed"] if "import os" in code else []

    bad = "<analysis>a</analysis><script>\nimport os\n\ndef fix(p):\n    os.remove('x')\n</script>"
    client = FakeLLMClient([bad, GOOD])
    p = LLMFixer(cfg, client, script_checker=checker).propose_script(proj, case_cluster(), sctx())
    assert len(client.calls) == 2 and len(seen) == 2
    feedback = client.calls[1]["messages"][-1].content
    assert "import of 'os' is not allowed" in feedback and "COMPLETE corrected answer" in feedback
    assert p.code == CODE and p.error == ""
    twice = LLMFixer(cfg, FakeLLMClient([bad]), script_checker=checker).propose_script(proj, case_cluster(), sctx())
    assert twice.code == "" and twice.error == "script rejected by the static check: line 1: import of 'os' is not allowed"
    assert "[sbmig] feedback round did not give a usable script" in twice.analysis


def test_default_static_check_is_sbmig_script(cfg, proj):
    pytest.importorskip("sbmig.script")
    bad = "<analysis>a</analysis><script>\nimport subprocess\n\ndef fix(p):\n    subprocess.run(['ls'])\n</script>"
    p = LLMFixer(cfg, FakeLLMClient([bad])).propose_script(proj, case_cluster(), sctx())
    assert p.code == "" and p.error.startswith("script rejected by the static check:") and "subprocess" in p.error


def test_unusable_answers(cfg, proj):
    syntax = "<analysis>a</analysis><script>\ndef fix(p):\n    p.log('x'\n</script>"
    p = LLMFixer(cfg, FakeLLMClient([syntax])).propose_script(proj, case_cluster(), sctx())
    assert p.code == "" and p.error.startswith("script does not parse") and p.llm_usage.calls == 2
    nofix = "<analysis>renames</analysis><script>\ndef repair(p):\n    pass\n</script>"
    p = LLMFixer(cfg, FakeLLMClient([nofix])).propose_script(proj, case_cluster(), sctx())
    assert p.code == "" and p.error == NO_FIX_DEF and p.analysis.startswith("renames")
    only = FakeLLMClient(["<analysis>no safe script exists</analysis>"])
    p = LLMFixer(cfg, only).propose_script(proj, case_cluster(), sctx())
    assert len(only.calls) == 1 and p.code == "" and p.error == NO_SCRIPT and p.analysis == "no safe script exists"
    v1 = FakeLLMClient([("<analysis>x</analysis><patches>\n=== MODIFY a.java\n<<<<<<< SEARCH\na\n=======\nb\n"
                         ">>>>>>> REPLACE\n</patches>"), GOOD])
    p = LLMFixer(cfg, v1).propose_script(proj, case_cluster(), sctx())
    assert len(v1.calls) == 2 and "SEARCH/REPLACE" in v1.calls[1]["messages"][-1].content and p.code == CODE
    p = LLMFixer(cfg, FakeLLMClient()).propose_script(proj, case_cluster(), sctx())    # the fake's "no fix" answer
    assert p.code == "" and p.analysis == "no fix"


def test_failure_texts_match_propose(cfg, proj, put):
    """The loop classifies script failures with the v1 functions: the analysis texts must be the same."""
    from sbmig.loop.fixloop import is_llm_transport_error, llm_400_body

    put("src/main/java/a/A.java", "package a;\n\nimport javax.servlet.Filter;\n\npublic class A {\n}\n")
    v1_cluster = make_cluster(file="src/main/java/a/A.java")
    cases = [LLMError(503, "overloaded"), LLMError(None, "ConnectError: refused",
                                                    "LLM request failed after 4 attempt(s) (status=None, cause=ConnectError): x"),
             LLMError(400, '{"error": "unsupported model"}'), RuntimeError("kaboom"), TimeoutError("read timed out"),
             "Sure! Just change the import."]
    for item in cases:
        v1 = LLMFixer(cfg, FakeLLMClient([item])).propose(proj, v1_cluster,
                                                          FixContext(phase=Phase.VERIFY, iteration=1))
        v2 = LLMFixer(cfg, FakeLLMClient([item])).propose_script(proj, case_cluster(), sctx())
        assert v2.analysis == v1.analysis, item
        assert v2.code == "" and v2.error, item
        assert is_llm_transport_error(v2.analysis) == is_llm_transport_error(v1.analysis), item
        assert llm_400_body(v2.analysis) == llm_400_body(v1.analysis), item
    p = LLMFixer(cfg, FakeLLMClient([LLMError(503, "overloaded")])).propose_script(proj, case_cluster(), sctx())
    assert p.analysis.startswith("llm-error:") and is_llm_transport_error(p.analysis)
    assert p.error == "LLM call failed (status=503)"
    p = LLMFixer(cfg, FakeLLMClient([LLMError(400, "bad request body")])).propose_script(proj, case_cluster(), sctx())
    assert llm_400_body(p.analysis) == "bad request body" and not is_llm_transport_error(p.analysis)
    ev = [e for e in journal(proj) if e["event"] == "llm_script"][-1]
    assert ev["llm_error"].startswith("llm-error") and ev["error"] == "LLM call failed (status=400)"


def test_never_raises_when_packing_fails(cfg, proj):
    class Broken:
        def pack_script(self, *a, **kw):
            raise ValueError("bad packer")

    p = LLMFixer(cfg, FakeLLMClient([GOOD]), packer=Broken()).propose_script(proj, case_cluster(), sctx())
    assert p.code == "" and p.analysis == "llm-error: ValueError: bad packer" and p.error == "LLM call failed (ValueError)"


def test_escalation_model_and_role(cfg, proj):
    client = FakeLLMClient([GOOD])
    fixer = LLMFixer(cfg, client)
    cfg.llm.model = "base-model"
    cfg.llm.escalation_model = "big-model"
    fixer.propose_script(proj, case_cluster(), sctx())
    fixer.propose_script(proj, case_cluster(), sctx(use_escalation_model=True))
    cfg.llm.escalation_model = None
    fixer.propose_script(proj, case_cluster(), sctx(use_escalation_model=True))
    assert [c["model"] for c in client.calls] == ["base-model", "big-model", "base-model"]
    assert [c["role"] for c in client.calls] == ["fix", "fix_escalation", "fix_escalation"]


def test_truncated_output_and_record_dir(cfg, proj, tmp_path):
    class Trunc(FakeLLMClient):
        def complete(self, messages, **kw):
            return super().complete(messages, **kw).model_copy(update={"finish_reason": "length"})

    p = LLMFixer(cfg, Trunc([GOOD])).propose_script(proj, case_cluster(), sctx())
    assert p.code == CODE and "[sbmig] output truncated (finish_reason=length)" in p.analysis
    cfg.llm.record_dir = str(tmp_path / "rec")
    LLMFixer(cfg, FakeLLMClient([GOOD])).propose_script(proj, case_cluster(case_id="c/1"), sctx())
    files = list((tmp_path / "rec").glob("script-case_c_1-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["cluster_id"] == "case:c/1" and data["proposal"]["code"] == CODE
    assert data["calls"][0]["response"] == GOOD
    rc = RecordingClient(FakeLLMClient([GOOD]), tmp_path / "rec2")         # the recorder already records
    LLMFixer(cfg, rc).propose_script(proj, case_cluster(case_id="c2"), sctx())
    assert list((tmp_path / "rec2").glob("llm-*.json")) and not list((tmp_path / "rec").glob("script-case_c2-*"))


def test_prompt_is_deterministic(cfg, proj):
    c = case_cluster()
    p1 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose_script(proj, c, sctx())
    shuffled = c.model_copy(update={"errors": list(reversed(c.errors + c.errors))})     # order + reactor duplicates
    p2 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose_script(proj, shuffled, sctx())
    p3 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose_script(proj, case_cluster(files=FILES[:2]), sctx())
    assert p1.prompt_hash == p2.prompt_hash != p3.prompt_hash
