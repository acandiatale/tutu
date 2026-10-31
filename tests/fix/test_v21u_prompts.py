"""v2.1u / v2.1v / v2.1y — the fixer side of the prompt review follow-through (docs/INTERFACES.md "v2.1u … v2.1y"):
the target rendered from `cfg.target` (never written in a prompt file), rule 8 / 10 / 14 and the `<blocked>` answer,
the B-L2 deletions, the `as-is (before sbmig)` project line, the "Project after the restructure" section
(`FixContext.layout_facts`), the plan-note filter and the shortened attempt analysis."""
from __future__ import annotations

import json
import re

import pytest

from sbmig.core.models import DecisionKind, FixAttempt, Phase, TriageDecision
from sbmig.fix import ContextPacker, FakeLLMClient, LLMFixer
from sbmig.fix.context import (
    ASIS_LABEL,
    LAYOUT_FACTS_TITLE,
    PROMPT_PLAN_NOTE_PREFIXES,
    asis_line,
    prompt_plan_notes,
    shorten,
    summarize_script_attempt,
    target_text,
)
from sbmig.fix.fixer import (
    PROMPTS_DIR,
    load_edits_prompts,
    load_script_prompts,
    load_verify_prompt,
    parse_verify_answer,
)
from sbmig.fix.script_protocol import BLOCKED_MAX_CHARS, PROTOCOL_WARNING, parse_script_response

from .test_fix_script_context import big_java, case, errs_for, sctx
from .test_v21d_verify import FILES, cluster, ctx, proj  # noqa: F401 - proj is a fixture

# a Boot / JDK / Framework version written into a prompt file (the target comes from cfg.target)
VERSION_LITERAL = re.compile(r"\b4\.[01]\b|\b21\b|\b7\.0\b")
SYSTEM_FILES = ("script_system_v1.md", "edits_system_v1.md", "verify_system_v1.md")
BLOCKER = "iBATIS 2 has no jakarta build on any line — the owning team decides the MyBatis 3 port"
EDIT = {"op": "replace", "path": FILES[0], "desc": f"{FILES[0]} 의 import 줄을 바꿉니다",
        "old": "import com.opensymphony.module.sitemesh.Page;", "new": "import org.sitemesh.Page;"}


def retargeted(cfg):
    c = cfg.model_copy(deep=True)
    c.target.java_version, c.target.spring_boot_version = "25", "4.2.0"
    return c


# ---------------------------------------------------------------------------------------------- target rendered
@pytest.mark.parametrize("name", sorted(p.name for p in PROMPTS_DIR.iterdir() if p.is_file()))
def test_no_prompt_file_writes_a_target_version(name):
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8").replace("{{ target }}", "")
    assert not VERSION_LITERAL.search(text), (name, VERSION_LITERAL.search(text))
    for stale in ("Jakarta EE 11", "Spring Framework 7", "Spring Security 7", "Hibernate 7", "Spring Boot 4.0 / 4.1",
                  "relocated\n   Spring Boot 4 packages", "Boot 4's Jackson 3"):
        assert stale not in text, (name, stale)


@pytest.mark.parametrize("name", SYSTEM_FILES)
def test_system_prompt_files_carry_the_target_placeholder(name):
    assert "{{ target }}" in (PROMPTS_DIR / name).read_text(encoding="utf-8")


def test_the_rendered_system_prompts_carry_cfg_target(cfg):
    c = retargeted(cfg)
    want = target_text(c)
    assert want == "JDK 25, Spring Boot 4.2.0"
    fx = LLMFixer(c, FakeLLMClient())
    script_system, edits_system = fx.script_prompts()[0], fx.edits_prompts()[0]
    verify_system = fx.build_verify_messages(cluster(), ctx(), [], "")[0].content
    for system in (script_system, edits_system, verify_system):
        assert want in system and "{{" not in system and target_text(None) not in system
    for system in (script_system, edits_system):
        flat = " ".join(system.split())
        assert f"migrated automatically to **{want}**" in flat
        assert 'the recipes this run executed are listed under "Plan notes"' in flat
        assert 'a non-Boot project became a Spring Boot application (see "Project after the restructure")' in flat
    # the module-level loaders keep their defaults (TargetConfig) for callers that pass no target
    assert target_text(None) in load_script_prompts("v1")[0] and target_text(None) in load_edits_prompts("v1")[0]
    assert target_text(None) in load_verify_prompt("v1")


# ---------------------------------------------------------------------------------------------- rules
@pytest.mark.parametrize("name", ["script_system_v1.md", "edits_system_v1.md"])
def test_rules_8_10_and_the_blocked_line(name):
    raw = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    flat = " ".join(raw.split())
    assert "the target Boot line's relocated packages (see the Migration notes)" in flat
    assert "Jackson (the Jackson 2 → 3 Migration note, `jackson-2-3.md`)" in flat
    assert "a module on Jackson 3 → finish the file's move to `tools.jackson.*` and never undo the recipe" in flat
    assert ("a library or module still on Jackson 2 → keep that file on 2 with `jackson-databind` "
            '`"version": null`') in flat
    assert "Never add `spring-boot-jackson2` to a converted project" in flat
    assert "or a Migration note's `fix-loop:` section asks for it" in flat
    assert 'that is not "replacing a library on your own" (rule 14)' in flat
    tail = flat[flat.index("If the case cannot be fixed inside the project"):]
    assert "answer `<analysis>` and `<blocked>one sentence naming the blocker and who decides</blocked>`" in tail
    assert tail.endswith("Any other case: fix it.")
    assert "If nothing can fix this case safely" not in flat
    assert ('`"version": null` = sbmig decides (no version when the parent chain or the target Boot BOM manages it, '
            "else it fills one)") in flat
    if name.startswith("script"):
        assert "no `<edits>`, no `<script>`" in tail
    else:
        assert "no `<edits>`." in tail and "<script>" not in raw


def test_b_l2_deletions():
    script = (PROMPTS_DIR / "script_system_v1.md").read_text(encoding="utf-8")
    edits = (PROMPTS_DIR / "edits_system_v1.md").read_text(encoding="utf-8")
    verify = (PROMPTS_DIR / "verify_system_v1.md").read_text(encoding="utf-8")
    assert "## Script or no script" not in script
    assert "`<no_script>` only\n    when the fix depends on this project's own code" in script      # folded into 12
    for text in (script, edits):
        assert "is left out when no dependency's major version changed" not in text
        assert text.count("`<verify>` is REQUIRED") == 1                       # rule 14 only
    assert "Instead of `<script>`,\nwrite `<no_script>" not in script
    assert "The script must define a module-level `def fix(p)`; it may define helper functions and constants" in script
    assert "The build passes" not in verify and "The build has\nnot been re-run yet; the runtime is unverified" in verify


# ---------------------------------------------------------------------------------------------- <blocked> parser
def test_blocked_answer_is_parsed():
    r = parse_script_response(f"<analysis>원인: com.ibatis is gone</analysis>\n<blocked>\n  {BLOCKER}\n</blocked>\n")
    assert r.blocked_reason == BLOCKER and r.analysis == "원인: com.ibatis is gone"
    assert not r.usable and not r.edits and not r.code and r.warnings == [] and PROTOCOL_WARNING not in r.warnings


def test_blocked_reason_is_one_trimmed_sentence_capped():
    r = parse_script_response("<blocked>```\nline one\n\n   line   two\n```</blocked>")
    assert r.blocked_reason == "line one line two"
    long = parse_script_response("<analysis>a</analysis><blocked>" + "word " * 200 + "</blocked>").blocked_reason
    assert len(long) == BLOCKED_MAX_CHARS == 400 and long.endswith("…")
    assert parse_script_response("<analysis>a</analysis><blocked></blocked>").blocked_reason == "(no reason given)"
    assert parse_script_response("<analysis>a</analysis>").blocked_reason == ""
    # no closing tag: ends at the next known tag
    r = parse_script_response(f"<analysis>a</analysis>\n<blocked>{BLOCKER}\n<need_grep>x</need_grep>")
    assert r.blocked_reason == BLOCKER and r.need_grep == ["x"]


def test_blocked_with_edits_keeps_both_and_never_opens_a_script():
    text = (f"<analysis>a</analysis>\n<blocked>{BLOCKER} — no <script> can port it</blocked>\n"
            f"<edits>{json.dumps([EDIT], ensure_ascii=False)}</edits>\n")
    r = parse_script_response(text)
    assert r.blocked_reason.startswith("iBATIS 2") and "<script>" in r.blocked_reason
    assert len(r.edits) == 1 and r.candidate == "" and r.code == ""
    e = parse_script_response(text, expect_script=False)                       # edits mode
    assert e.blocked_reason == r.blocked_reason and len(e.edits) == 1 and not e.script_ignored


def test_a_blocked_answer_is_not_a_verify_guide():
    assert parse_verify_answer(f"<blocked>{BLOCKER}</blocked>").text == ""


# ---------------------------------------------------------------------------------------------- <blocked> fixer
def test_fixer_passes_the_blocked_reason_without_a_feedback_round(cfg, proj):  # noqa: F811
    client = FakeLLMClient([f"<analysis>원인: iBATIS 2</analysis>\n<blocked>{BLOCKER}</blocked>\n"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1                                            # the model's decision: no feedback
    assert p.blocked_reason == BLOCKER and p.error == f"blocked: {BLOCKER}"
    assert p.code == "" and p.edits == [] and p.analysis == "원인: iBATIS 2"
    ev = [json.loads(ln) for ln in proj.journal_path.read_text().splitlines() if '"llm_script"' in ln][-1]
    assert ev["blocked_reason"] == BLOCKER


def test_fixer_keeps_edits_next_to_a_blocked_reason(cfg, proj):  # noqa: F811
    edits = json.dumps([EDIT], ensure_ascii=False)
    answer = (f"<analysis>a</analysis>\n<blocked>{BLOCKER}</blocked>\n<edits>{edits}</edits>\n"
              "<no_script>project-specific</no_script>\n")
    client = FakeLLMClient([answer])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and p.blocked_reason == BLOCKER and len(p.edits) == 1 and p.error == ""


def test_fixer_blocked_in_edits_mode(cfg, proj):  # noqa: F811
    client = FakeLLMClient([f"<analysis>a</analysis>\n<blocked>{BLOCKER}</blocked>\n"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(fix_mode="edits"))
    assert len(client.calls) == 1 and p.blocked_reason == BLOCKER and p.error == f"blocked: {BLOCKER}"
    ok = LLMFixer(cfg, FakeLLMClient([f"<analysis>a</analysis><edits>{json.dumps([EDIT])}</edits>"]))
    assert ok.propose_script(proj, cluster(), ctx(fix_mode="edits")).blocked_reason == ""


def test_a_blocked_answer_asks_for_no_more_context(cfg, proj):  # noqa: F811
    client = FakeLLMClient([f"<analysis>a</analysis>\n<blocked>{BLOCKER}</blocked>\n<need_files>x.java</need_files>"])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and p.blocked_reason == BLOCKER and p.requested_files == []


# ---------------------------------------------------------------------------------------------- packer
F = "app/src/main/java/a/D0.java"
FACTS = ["converted by sbmig: spring_legacy → Spring Boot application (company layout)",
         "boot module demo-boot: artifactId demo-boot, packaging jar, parent g:boot-parent:v, starter s",
         "- back module demo-back: every source incl. controllers, parent g:comp-parent:v"]


def test_asis_line():
    assert asis_line("case=spring_legacy java=1.8") == ASIS_LABEL + "case=spring_legacy java=1.8"
    assert asis_line(ASIS_LABEL + "case=x") == ASIS_LABEL + "case=x" and asis_line("") == ""


def test_the_header_names_the_as_is_project(cfg, ws, put):
    put(F, big_java("D0", n=20))
    packed = ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), sctx(profile_summary="case=spring_legacy"))
    header = dict(packed.sections)["Case"]
    assert f"\n{ASIS_LABEL}case=spring_legacy" in header and "project: " not in header


def test_layout_facts_section_order_and_bullets(cfg, ws, put):
    put(F, big_java("D0", n=20))
    packed = ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), sctx(layout_facts=FACTS))
    titles = [t for t, _ in packed.sections]
    assert titles[:2] == ["Case", LAYOUT_FACTS_TITLE] and titles[2].startswith("Errors of this case")
    assert LAYOUT_FACTS_TITLE == "Project after the restructure"
    assert dict(packed.sections)[LAYOUT_FACTS_TITLE] == "\n".join(
        ["- " + FACTS[0], "- " + FACTS[1], FACTS[2]])                          # an existing "- " is kept
    none = ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), sctx())
    assert LAYOUT_FACTS_TITLE not in [t for t, _ in none.sections]


def test_layout_facts_are_never_truncated_and_reach_edits_mode(cfg, ws, put):
    files = [f"app/src/main/java/a/D{i}.java" for i in range(6)]
    errors = []
    for i, f in enumerate(files):
        put(f, big_java(f"D{i}", n=300))
        errors += errs_for(f, [12, 200])
    big = sctx(layout_facts=FACTS, kb_hints=["k" * 3000], other_errors_summary="o" * 3000,
               plan_notes=["restructure: " + "p" * 2000])
    for c in (big, big.model_copy(update={"fix_mode": "edits"})):
        packed = ContextPacker(cfg).pack_script(ws, case(errors), c, budget=3000)
        assert packed.truncated and not any(t.startswith("layout_facts") for t in packed.truncated)
        assert dict(packed.sections)[LAYOUT_FACTS_TITLE].startswith("- converted by sbmig")


def test_plan_notes_filter():
    notes = ["case spring_legacy (confidence 0.75): spring dependencies …",
             "transform: one OpenRewrite run (UpgradeToJava21, JavaxMigrationToJakarta) — no build before",
             "transform [common]: pure_java with Java EE javax imports → jakarta set",
             "compile: no build before the restructure (plan.compile_after_rewrite=false, v2.1o)",
             "restructure: rule non-boot (spring_legacy) → layout split into shop-back, shop-boot",
             "optional artifacts: bumped inside the restructure step (no separate bump step in v2)",
             "review: the LLM checks poms / dependencies / modules after the restructure",
             "advisory: jdk-removed-apis → guide (LLM hints + Handover)",
             "msa service order-service (order-service): sub-case spring_boot",
             "msa: top-aggregator policy",
             "warning: boot module boot-jar is not named *-boot (company naming rule)",
             "followup: web.xml wiring is not migrated automatically",
             "verify: root aggregator pom.xml → one mvn clean package for the whole repo",
             "advisor: ignored unknown recipe x"]
    kept = prompt_plan_notes(notes)
    assert kept == [notes[i] for i in (1, 2, 4, 8, 9, 10, 11)]
    assert PROMPT_PLAN_NOTE_PREFIXES == ("transform", "restructure", "warning", "msa", "followup")


def test_only_the_kept_plan_notes_reach_the_prompt(cfg, ws, put):
    put(F, big_java("D0", n=20))
    c = sctx(plan_notes=["case spring_legacy (confidence 0.75): x", "restructure: layout split into a-back, a-boot",
                         "review: the LLM checks poms"])
    secs = dict(ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), c).sections)
    assert secs["Plan notes"] == "- restructure: layout split into a-back, a-boot"
    only_noise = sctx(plan_notes=["review: x", "optional artifacts: bumped"])
    assert "Plan notes" not in dict(ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), only_noise).sections)


def test_an_earlier_attempt_analysis_is_shortened_with_a_marker():
    long = "원인: " + " ".join(f"word{i}" for i in range(200))
    att = FixAttempt(id="r:verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:k1", analysis=long,
                     decision=TriageDecision(cluster_id="case:k1", kind=DecisionKind.SCRIPT), outcome="no_change",
                     script_mode="new", script_version=1)
    line = next(ln for ln in summarize_script_attempt(att).splitlines() if ln.strip().startswith("analysis:"))
    shown = line.split("analysis: ", 1)[1]
    assert len(shown) <= 300 and shown.endswith("…") and shown.startswith("원인: word0 word1")
    assert shorten("x" * 500, 300) == "x" * 299 + "…"                        # no word boundary: cut, not "…" alone
    assert shorten("a\n b", 300) == "a b"
