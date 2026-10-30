"""v2.1i `fix.mode: edits` — the fixer side: the edits-only prompts, LLMFixer in edits mode (no script, no helper API,
always `new`, the retry history), the parser's `expect_script=False`, and script mode unchanged."""
import hashlib
import json
import re

import pytest

from sbmig.core.interfaces import FixContext
from sbmig.core.models import (
    BuildError,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FixAttempt,
    Phase,
    ScriptBase,
    TriageDecision,
)
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.fix.context import EDITS_HISTORY_TITLE, ContextPacker
from sbmig.fix.fixer import PROMPTS_DIR, effective_fix_mode, load_edits_prompts, load_script_prompts
from sbmig.fix.script_protocol import (
    NO_EDITS,
    NO_SCRIPT,
    PATCHES_WARNING,
    SCRIPT_IGNORED,
    parse_script_response,
)

SIG = "compile|package_missing|javax.annotation"
FILES = [f"demo-back/src/main/java/a/S{i}.java" for i in range(3)]
OLD, NEW = "import javax.annotation.PostConstruct;", "import jakarta.annotation.PostConstruct;"
# fix-script wording that must not reach the model in edits mode ("description" is fine: no word boundary)
SCRIPT_WORDS = re.compile(r"(?i)\bscripts?\b|<script>|def fix\(|p\.errors|p\.files|helper api|<changes>|<no_script>")


def edit(path: str, desc: str = "") -> dict:
    return {"op": "replace", "path": path, "desc": desc or f"{path} 의 import 줄에서 javax 를 jakarta 로 바꿉니다",
            "old": OLD, "new": NEW}


EDITS = json.dumps([edit(f) for f in FILES], ensure_ascii=False)
GOOD = f"<analysis>javax.annotation moved to jakarta</analysis>\n<solution>import 를 바꾼다</solution>\n<edits>\n{EDITS}\n</edits>\n"
SCRIPT_BLOCK = "<script>\ndef fix(p):\n    p.log('x')\n</script>\n"


def src(i: int) -> str:
    return f"package a;\n\n{OLD}\n\npublic class S{i} {{\n    @PostConstruct\n    void init() {{}}\n}}\n"


def case_cluster(files=FILES, case_id="abc123") -> ErrorCluster:
    errs = [BuildError(kind=ErrorKind.COMPILE, message="package javax.annotation does not exist", file=f, line=3,
                       column=24, module="demo-back", signature=f"s{i}", signature_generic=SIG)
            for i, f in enumerate(files)]
    return ErrorCluster(id=f"case:{case_id}", kind=ErrorKind.COMPILE, errors=errs, case_id=case_id,
                        signatures=[e.signature for e in errs])


def ectx(**kw) -> FixContext:
    base = {"phase": Phase.VERIFY, "iteration": 1, "profile_summary": "case=spring_legacy java=1.8",
            "script_mode": "new", "case_id": "abc123", "signature_generic": SIG}
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


@pytest.fixture()
def ecfg(cfg):
    cfg.fix.mode = "edits"
    return cfg


@pytest.fixture()
def notes(tmp_path):
    d = tmp_path / "no-notes"          # no migration notes: their technical text (javax.script, …) stays out
    d.mkdir()
    return d


def fixer(cfg, client, notes) -> LLMFixer:
    return LLMFixer(cfg, client, notes_dir=notes)


def journal(ws) -> list[dict]:
    return [json.loads(ln) for ln in ws.journal_path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def test_edits_system_prompt_has_no_script_wording_and_is_shorter():
    system, user_t = load_edits_prompts("v1")
    script_system, _ = load_script_prompts("v1")
    raw_script = (PROMPTS_DIR / "script_system_v1.md").read_text(encoding="utf-8")
    for banned in ("<script>", "def fix(p)", "Helper API", "p.errors", "p.replace", "<changes>", "<no_script>",
                   "Script or no script", "{{", "{%"):
        assert banned not in system, banned
    assert len(system) <= 0.7 * len(script_system) and len(system) <= 0.7 * len(raw_script)
    assert "NO script" in system                                              # said explicitly
    for kept in ("<analysis>", "<solution>", "<verify>", "<edits>", "<need_files>", "<need_grep>", '"desc"',
                 "## Edits (`<edits>`)", "## Describing every change", "## Output format (mandatory)",
                 "14. Dependency versions (메이저를 올리지 않는다)", "`<verify>` is REQUIRED", "src/test/",
                 "<파일> 의 <위치> 에서 <무엇을> <어떻게> 수정합니다", "set_plugin_version"):
        assert kept in system, kept
    # the rule numbers the fixer and the docs cite are where the script prompt has them
    for n, head in ((3, "Change only what the errors need"), (9, "When a runtime concern remains"),
                    (10, 'If a "Library migration guidance"'), (11, "Do not guess code"), (14, "Dependency versions")):
        assert re.search(rf"^{n}\. {re.escape(head)}", system, re.MULTILINE), n
        assert re.search(rf"^{n}\. {re.escape(head)}", script_system, re.MULTILINE), n
    rendered = user_t.render(sections=[("Case", "case: x")], truncated=[], retry=False, desc_language="Korean")
    assert not SCRIPT_WORDS.search(rendered) and "case: x" in rendered


# the script prompts as of v2.1t (2026-10-30: rule 14 — unknown versions are left null, sbmig fills them; the parent
# chain's version properties); update a hash only when that prompt is changed on purpose
SCRIPT_PROMPT_SHA256 = {"script_system_v1.md": "da6707a0eec932b8ef4d2fc0c43bc445b1b3756a066f0a3ae0fa74f5ee19fc3e",
                        "script_user_v1.md.j2": "797fec4e1c84a9b220501759e312e9489f4cf788799d00280afc787992d34d86"}


@pytest.mark.parametrize("name", ["script_system_v1.md", "edits_system_v1.md"])
def test_rule_14_leaves_unknown_versions_to_sbmig(name):
    """v2.1l: an unknown version is written null and sbmig fills it; never a reason to refuse a fix. Both prompts."""
    text = " ".join((PROMPTS_DIR / name).read_text(encoding="utf-8").split())
    assert ('If you add a dependency and do not know a version the repository has, write `"version": null`: sbmig '
            "fills it before the build (the as-is build's version, else the newest release the repository has).") in text
    assert "leave an error unfixed only because a version is unknown." in text
    assert "never name a version that section contradicts" in text            # the no-guessing sentence stays
    assert "what the as-is build used and what the new parent chain manages" in text
    assert ("When the parent chain defines a version property for the library's family (the facts list them as "
            "`parent chain property:`), write `${that property}` instead of a literal version.") in text   # v2.1t
    if name.startswith("script"):
        assert "Never refuse a fix, answer `<no_script>` or leave an error unfixed" in text
    else:
        assert "Never refuse a fix or leave an error unfixed" in text


def test_script_prompts_unchanged():
    """Script mode keeps its prompts byte for byte: v2.1i adds the edits prompts beside them."""
    system, _ = load_script_prompts("v1")
    assert "## Helper API `p`" in system and "def fix(p)" in system and "<script>" in system
    assert "p.pom(" in system and "## Script or no script" in system
    for name, sha in SCRIPT_PROMPT_SHA256.items():
        assert hashlib.sha256((PROMPTS_DIR / name).read_bytes()).hexdigest() == sha, name


def test_effective_fix_mode(cfg):
    assert effective_fix_mode(cfg, ectx()) == "script"
    assert effective_fix_mode(cfg, ectx(fix_mode="edits")) == "edits"           # the loop sets ctx
    cfg.fix.mode = "edits"
    assert effective_fix_mode(cfg, ectx()) == "edits"                          # a caller that did not
    assert effective_fix_mode(cfg) == "edits" and effective_fix_mode(None, None) == "script"


# ---------------------------------------------------------------------------
# LLMFixer in edits mode
# ---------------------------------------------------------------------------
def test_edits_mode_request_and_answer(ecfg, proj, notes):
    client = FakeLLMClient([GOOD])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    assert len(client.calls) == 1
    call = client.calls[0]
    system, user = call["messages"][0].content, call["messages"][1].content
    assert system == load_edits_prompts("v1")[0]                              # no helper API filled in anywhere
    assert "Helper API" not in system and "p.replace(" not in system
    assert user.startswith("Fix this case now with `<edits>`: the exact changes for this project")
    assert not SCRIPT_WORDS.search(user), SCRIPT_WORDS.search(user)
    assert "case: abc123 — L2 signature: " + SIG in user                       # the replay `match: "case: <id>"` line
    assert "errors: 3 in 3 file(s)\n" in user and "## Source (exact text around the error lines" in user
    for f in FILES:
        assert f"- {f}:3:24: package javax.annotation does not exist" in user and f"// {f} lines 1-8 (whole file)" in user
    assert user.rstrip().endswith('<edits> (each with its "desc") — or <need_files> / <need_grep>.')
    meta = call["metadata"]
    assert meta["fix_mode"] == "edits" and meta["case_id"] == "abc123" and "script_mode" not in meta
    assert call["role"] == "fix"
    assert p.code == "" and p.no_script_reason == "" and p.error == ""
    assert [e.path for e in p.edits] == FILES and all(e.desc for e in p.edits)
    assert [n.path for n in p.changes] == FILES                                # each edit's desc → the notes
    assert p.analysis == "javax.annotation moved to jakarta" and p.solution == "import 를 바꾼다"
    ev = [e for e in journal(proj) if e["event"] == "llm_script"][-1]
    assert ev["fix_mode"] == "edits" and ev["mode"] == "new" and ev["edits"] == 3 and ev["code_chars"] == 0
    assert ev["no_script"] is False and ev["error"] is None


def test_ctx_fix_mode_alone_switches_and_a_base_is_ignored(cfg, proj, notes):
    base = ScriptBase(case_id="abc123", version=2, code="def fix(p):\n    p.log('x')\n", outcome="partial")
    client = FakeLLMClient([GOOD])
    p = fixer(cfg, client, notes).propose_script(
        proj, case_cluster(), ectx(fix_mode="edits", script_mode="augment", script_base=base))
    user = client.calls[0]["messages"][1].content
    assert not SCRIPT_WORDS.search(user) and "Script to extend" not in user and "p.log('x')" not in user
    assert "version 2" not in user and user.startswith("Fix this case now with `<edits>`")
    meta = client.calls[0]["metadata"]
    assert meta["fix_mode"] == "edits" and "script_base_version" not in meta
    assert len(p.edits) == 3 and p.code == ""
    assert [e for e in journal(proj) if e["event"] == "llm_script"][-1]["base_version"] is None


def test_a_script_in_an_edits_answer_is_dropped_with_a_note(ecfg, proj, notes):
    client = FakeLLMClient([GOOD + SCRIPT_BLOCK])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    assert len(client.calls) == 1                                              # edits are there: no feedback round
    assert p.code == "" and len(p.edits) == 3 and p.error == "" and p.no_script_reason == ""
    assert f"[sbmig] {SCRIPT_IGNORED}" in p.analysis


def test_a_script_only_answer_gets_one_feedback_round(ecfg, proj, notes):
    client = FakeLLMClient(["<analysis>a</analysis>\n" + SCRIPT_BLOCK, GOOD])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    assert len(client.calls) == 2
    fb = client.calls[1]["messages"][-1].content
    assert fb.startswith("Your edits could not be applied. Problems:") and "the answer has no <edits>" in fb
    assert not SCRIPT_WORDS.search(fb)
    assert len(p.edits) == 3 and p.code == "" and p.error == "" and p.llm_usage.calls == 2


def test_unreadable_edits_get_one_feedback_round(ecfg, proj, notes):
    broken = "<analysis>a</analysis>\n<edits>[{\"op\": \"replace\", \"path\": </edits>"
    client = FakeLLMClient([broken, GOOD])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    fb = client.calls[1]["messages"][-1].content
    assert "Your edits could not be applied" in fb and "edits JSON unreadable" in fb and "valid JSON list" in fb
    assert not SCRIPT_WORDS.search(fb) and len(p.edits) == 3 and p.error == ""
    still = fixer(ecfg, FakeLLMClient([broken]), notes).propose_script(proj, case_cluster(), ectx())
    assert still.edits == [] and still.code == "" and still.error.startswith("edits JSON unreadable")
    assert "[sbmig] feedback round did not give usable edits" in still.analysis


def test_an_answer_without_edits_is_no_fix_without_feedback(ecfg, proj, notes):
    client = FakeLLMClient(["<analysis>cannot be fixed safely: the class was deleted upstream</analysis>"])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    assert len(client.calls) == 1 and p.edits == [] and p.code == "" and p.no_script_reason == ""
    assert p.error == NO_EDITS and p.analysis.startswith("cannot be fixed safely")


def test_context_rounds_in_edits_mode(ecfg, proj, notes):
    ask = "<analysis>need the base class</analysis><need_files>Base.java</need_files>"
    client = FakeLLMClient([ask, GOOD])
    p = fixer(ecfg, client, notes).propose_script(proj, case_cluster(), ectx())
    served = client.calls[1]["messages"][3].content
    assert "public abstract class Base" in served and "(<analysis>, <solution>, <edits>)" in served
    assert not SCRIPT_WORDS.search(served) and len(p.edits) == 3
    last = FakeLLMClient([ask])
    fixer(ecfg, last, notes).propose_script(proj, case_cluster(), ectx())
    assert "LAST context round. Answer now with <analysis>, <solution> and <edits>" in last.calls[-1]["messages"][-1].content
    assert not SCRIPT_WORDS.search(last.calls[-1]["messages"][-1].content)


def attempt(aid: str, **kw) -> FixAttempt:
    base = {"id": aid, "phase": Phase.VERIFY, "iteration": 1, "cluster_id": "case:abc123",
            "decision": TriageDecision(cluster_id="case:abc123", kind=DecisionKind.SCRIPT), "source": "llm-edit"}
    base.update(kw)
    return FixAttempt(**base)


def test_a_retry_carries_the_previous_tries_edits_and_why_they_failed(ecfg, proj, notes):
    """Every try in edits mode is a fresh ask; the second one knows what the first edited (the fixer keeps its edits by
    prompt hash — the loop's attempt carries the hash) and why it failed."""
    first_desc = "demo-back/src/main/java/a/S0.java 의 import 줄에서 PostConstruct 를 jakarta 로 바꿉니다"
    first = (f"<analysis>javax.annotation moved</analysis><solution>import 교체</solution><edits>"
             f"{json.dumps([edit(FILES[0], first_desc)], ensure_ascii=False)}</edits>")
    client = FakeLLMClient([first, GOOD])
    fx = fixer(ecfg, client, notes)
    p1 = fx.propose_script(proj, case_cluster(), ectx())
    a1 = attempt("a1", prompt_hash=p1.prompt_hash, applied=True, outcome="no_change", errors_before=3, errors_after=3,
                 analysis=p1.analysis + "\n[sbmig] a note for the report", solution=p1.solution, edits_applied=1)
    a2 = attempt("a2", iteration=2, prompt_hash="not-mine", applied=False, outcome="apply_failed", errors_before=3,
                 analysis="second idea", apply_errors=[f"edits error: old text not found in {FILES[1]}"])
    p2 = fx.propose_script(proj, case_cluster(), ectx(iteration=3, previous_attempts=[a1, a2]))
    user = client.calls[1]["messages"][1].content
    assert user.startswith("Fix this case now with `<edits>` — a fresh answer. Earlier tries of this run")
    assert f"## {EDITS_HISTORY_TITLE}" in user
    assert "- try 1 (attempt a1, iteration 1) → outcome: no_change, errors 3→3 — rolled back" in user
    assert "    cause: javax.annotation moved" in user and "    solution: import 교체" in user
    assert f"    edit: replace {FILES[0]} — {first_desc} | `{OLD}` → `{NEW}`" in user
    assert "- try 2 (attempt a2, iteration 2) → outcome: apply_failed — not applied" in user
    assert f"    failed: edits error: old text not found in {FILES[1]}" in user
    assert "[sbmig]" not in user and not SCRIPT_WORDS.search(user)
    # right after the errors: what the retry knows comes before the files
    assert user.index("## Errors of this case") < user.index(EDITS_HISTORY_TITLE) < user.index("## Files with errors")
    assert len(p2.edits) == 3 and p2.code == "" and p2.prompt_hash != p1.prompt_hash


def test_the_retry_history_is_shrunk_last(ecfg, proj, notes):
    many = [f"demo-back/src/main/java/a/S{i}.java" for i in range(3)] + \
        [f"demo-back/src/main/java/b/T{i}.java" for i in range(30)]
    for f in many[3:]:
        proj.write(f, "package b;\n\n" + OLD + "\n\n" + "\n".join(f"// line {k}" for k in range(150)) + "\n")
    hist = [attempt(f"a{i}", prompt_hash="", applied=True, outcome="regressed", errors_before=33, errors_after=40,
                    analysis="idea " + str(i)) for i in range(3)]
    ctx = ectx(previous_attempts=hist, kb_hints=[f"hint {k}: " + "x" * 300 for k in range(20)],
               other_errors_summary="\n".join(f"other case {k}: " + "y" * 200 for k in range(30)))
    packed = ContextPacker(ecfg, notes).pack_script(proj, case_cluster(many), ctx, budget=9000)
    text = packed.render()
    assert EDITS_HISTORY_TITLE in text and "- try 3 (attempt a2" in text
    assert packed.truncated and not any(t.startswith("previous_attempts") for t in packed.truncated)
    assert any(t.startswith(("other_errors", "kb_hints")) for t in packed.truncated)   # the others went first
    assert not SCRIPT_WORDS.search(text)
    assert "(not shown)" in text and "ask for them with <need_files>" in text


def test_edits_mode_records_under_its_own_prefix(ecfg, proj, notes, tmp_path):
    ecfg.llm.record_dir = str(tmp_path / "rec")
    fixer(ecfg, FakeLLMClient([GOOD]), notes).propose_script(proj, case_cluster(), ectx())
    assert list((tmp_path / "rec").glob("edits-case_abc123-*.json"))
    assert not list((tmp_path / "rec").glob("script-*.json"))


# ---------------------------------------------------------------------------
# script mode unchanged
# ---------------------------------------------------------------------------
def test_script_mode_still_sends_the_helper_api(cfg, proj, notes):
    client = FakeLLMClient([GOOD + SCRIPT_BLOCK])
    fx = fixer(cfg, client, notes)
    p = fx.propose_script(proj, case_cluster(), ectx())
    system, user = client.calls[0]["messages"][0].content, client.calls[0]["messages"][1].content
    assert system == fx.script_prompts()[0] and "## Helper API `p`" in system and "p.replace(path, old, new" in system
    assert user.startswith("Fix this case now and write ONE fix script for it.") and "p.errors" in user
    meta = client.calls[0]["metadata"]
    assert meta["script_mode"] == "new" and "fix_mode" not in meta
    assert p.code.startswith("def fix(p)") and len(p.edits) == 3
    assert "fix_mode" not in [e for e in journal(proj) if e["event"] == "llm_script"][-1]


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def test_parse_expect_script_false():
    r = parse_script_response(GOOD, expect_script=False)
    assert r.problem == "" and r.warnings == [] and len(r.edits) == 3 and r.usable and not r.script_ignored
    s = parse_script_response(GOOD)                                           # script mode: unchanged
    assert s.problem == NO_SCRIPT and len(s.edits) == 3
    dropped = parse_script_response(GOOD + SCRIPT_BLOCK, expect_script=False)
    assert dropped.code == "" and dropped.candidate == "" and dropped.script_ignored and len(dropped.edits) == 3
    assert dropped.warnings == [SCRIPT_IGNORED] and dropped.problem == ""
    fence = parse_script_response("<analysis>a</analysis>\n```python\ndef fix(p):\n    pass\n```\n", expect_script=False)
    assert fence.code == "" and not fence.script_ignored and fence.analysis == "a"
    only = parse_script_response(SCRIPT_BLOCK, expect_script=False)
    assert only.script_ignored and not only.usable and "response did not follow the protocol" not in only.warnings
    jsp = ('<analysis>a</analysis><edits>[{"op": "replace", "path": "w/a.jsp", "desc": "d", '
           '"old": "<script src=\\"a.js\\"></script>", "new": ""}]</edits>')
    j = parse_script_response(jsp, expect_script=False)
    assert len(j.edits) == 1 and not j.script_ignored                          # a <script> inside an edit is data
    v1 = parse_script_response("<analysis>x</analysis><patches>\n=== MODIFY a/A.java\n</patches>", expect_script=False)
    assert PATCHES_WARNING in v1.warnings and v1.used_patches
