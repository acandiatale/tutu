"""v2.1z-ak: the fix prompt of a reduced run (framework project, scope `jdk-jakarta`).

The system prompt file is a template: scope `full` renders it byte for byte as before this round (pinned by a hash of
that text), scope `jdk-jakarta` swaps the introduction and the rules that name the Boot target (the first paragraph,
rule 3's last sentence, rule 8, rule 14's fact order) for the framework rules. The user prompt shows "Scope of this run"
where "Project after the restructure" would be, and the project line's target says Java + jakarta only."""
from __future__ import annotations

import hashlib

from sbmig.fix import ContextPacker, FakeLLMClient, LLMFixer
from sbmig.fix.context import LAYOUT_FACTS_TITLE, SCOPE_FACTS_TITLE, TARGET_MARK, target_text
from sbmig.fix.fixer import load_script_prompts

from .test_fix_script_context import big_java, case, errs_for, sctx

F = "app/src/main/java/a/D0.java"
# sha256 of the full-scope system prompt as rendered by `load_script_prompts("v1")` before the reduced-scope variant was
# added (TargetConfig defaults, no extra imports). Change it only when the full prompt is changed on purpose.
FULL_SYSTEM_SHA256 = "9c23b95aa82f88a475c9cb35ee7bf7c38641d2c08087b24343eee9d0d1df6ac4"
SCOPE_FACTS = ["this run: Java 21 and javax.* to jakarta.* only",
               "framework artifact com.example.fw:example-fw-core 1.2.0 (direct: pom.xml) — never change its version, "
               "never remove or replace it"]
LAYOUT = ["converted by sbmig: demo-back (comp-jar)"]


def test_full_scope_system_prompt_is_byte_identical():
    load_script_prompts.cache_clear()
    default, _ = load_script_prompts("v1")
    full, _ = load_script_prompts("v1", (), None, "", "full")
    assert full == default
    assert hashlib.sha256(default.encode("utf-8")).hexdigest() == FULL_SYSTEM_SHA256
    assert "{{" not in default and "{%" not in default


def test_reduced_system_prompt_has_the_framework_rules_and_no_boot_target(cfg):
    reduced, _ = load_script_prompts("v1", (), None, "", "jdk-jakarta", str(cfg.target.java_version))
    flat = " ".join(reduced.split())
    assert "{{" not in reduced and "{%" not in reduced
    assert f"A Maven project built on an in-house framework is being moved automatically to **Java {cfg.target.java_version}**" in flat
    assert "sbmig did NOT restructure the project" in flat and '(see "Scope of this run")' in flat
    assert "Never change any `<parent>`, never change, remove or replace the framework artifact" in flat
    assert "never add Spring Boot, a Spring Boot starter or the company parents, never downgrade Java." in flat
    assert "8. Target: `jakarta.*` instead of Java EE `javax.*` only" in flat
    assert "EXCEPT the framework artifact itself and what reaches the classpath only through it" in flat
    assert "answer `<analysis>` and `<blocked>` naming the framework artifact" in flat
    assert "in this order: `parent chain: manages g:a at v` (the project's own parent) → no version" in flat
    for boot in ("the company restructure then set the parents", "a non-Boot project became a Spring Boot application",
                 "Project after the restructure", "never downgrade Java or Spring Boot", "relocated packages",
                 "Spring Security lambda DSL", "SecurityFilterChain", "tools.jackson", "spring-boot-jackson2",
                 "`target Boot BOM: manages g:a at v`", "`new parent chain: manages g:a at v`",
                 target_text(cfg)):
        assert boot not in flat, boot
    # what both share stays (rules 1, 2, 12, 14's version policy, the output format)
    for common in ("ONE script fixes every error of this case", "Idempotent: a second run on the fixed tree",
                   "Never change a dependency's MAJOR version", "## Output format (mandatory)"):
        assert common in flat


def test_the_fixer_picks_the_system_prompt_by_scope(cfg, ws, put):
    put(F, big_java("D0", n=20))
    client = FakeLLMClient()
    fx = LLMFixer(cfg, client)
    fx.propose_script(ws, case(errs_for(F, [5])), sctx())
    fx.propose_script(ws, case(errs_for(F, [5])), sctx(scope="jdk-jakarta", scope_facts=SCOPE_FACTS))
    full_sys, red_sys = (c["messages"][0].content for c in client.calls[:2])
    assert full_sys == fx.script_prompts()[0] and "Project after the restructure" in full_sys
    assert red_sys == fx.script_prompts("jdk-jakarta")[0] and "Scope of this run" in red_sys
    red_user = client.calls[1]["messages"][1].content
    assert f"## {SCOPE_FACTS_TITLE}\n- {SCOPE_FACTS[0]}\n- {SCOPE_FACTS[1]}" in red_user


def test_user_prompt_shows_the_scope_section_instead_of_the_layout(cfg, ws, put):
    put(F, big_java("D0", n=20))
    summary = "case=spring_legacy java=1.8" + TARGET_MARK + target_text(cfg)
    reduced = ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), sctx(
        scope="jdk-jakarta", scope_facts=SCOPE_FACTS, layout_facts=LAYOUT, profile_summary=summary))
    titles = [t for t, _ in reduced.sections]
    assert titles[:2] == ["Case", SCOPE_FACTS_TITLE] and LAYOUT_FACTS_TITLE not in titles
    assert dict(reduced.sections)[SCOPE_FACTS_TITLE] == "\n".join(f"- {x}" for x in SCOPE_FACTS)
    header = dict(reduced.sections)["Case"]
    assert target_text(cfg) not in header and f"JDK {cfg.target.java_version} and jakarta.* only" in header
    full = ContextPacker(cfg).pack_script(ws, case(errs_for(F, [5])), sctx(
        scope_facts=SCOPE_FACTS, layout_facts=LAYOUT, profile_summary=summary))
    titles = [t for t, _ in full.sections]
    assert titles[:2] == ["Case", LAYOUT_FACTS_TITLE] and SCOPE_FACTS_TITLE not in titles
    assert target_text(cfg) in dict(full.sections)["Case"]


def test_the_scope_section_is_never_truncated(cfg, ws, put):
    files = [f"app/src/main/java/a/D{i}.java" for i in range(6)]
    errors = []
    for i, f in enumerate(files):
        put(f, big_java(f"D{i}", n=300))
        errors += errs_for(f, [12, 200])
    big = sctx(scope="jdk-jakarta", scope_facts=SCOPE_FACTS, kb_hints=["k" * 3000], other_errors_summary="o" * 3000)
    packed = ContextPacker(cfg).pack_script(ws, case(errors), big, budget=3000)
    assert packed.truncated and not any(t.startswith("scope_facts") for t in packed.truncated)
    assert dict(packed.sections)[SCOPE_FACTS_TITLE].startswith("- this run: ")


def test_verify_guide_prompt_target_follows_the_scope(cfg):
    from sbmig.fix.context import scope_target_text
    from sbmig.fix.fixer import load_verify_prompt

    from .test_v21d_verify import cluster, ctx
    fx = LLMFixer(cfg, FakeLLMClient())
    summary = "case=spring_legacy java=1.8" + TARGET_MARK + target_text(cfg)
    full = fx.build_verify_messages(cluster(), ctx(profile_summary=summary), [], "")
    # full scope: the same system text the loader renders with the configured target (unchanged)
    from sbmig.fix.fixer import describe_language
    assert full[0].content == load_verify_prompt(cfg.llm.prompt_version, target_text(cfg)).replace(
        "{{ language }}", describe_language(cfg))
    assert f"migrated automatically to {target_text(cfg)}." in full[0].content
    assert target_text(cfg) in full[1].content
    red = fx.build_verify_messages(cluster(), ctx(profile_summary=summary, scope="jdk-jakarta"), [], "")
    assert f"migrated automatically to {scope_target_text(cfg)}." in red[0].content
    assert target_text(cfg) not in red[0].content and target_text(cfg) not in red[1].content
    assert scope_target_text(cfg) in red[1].content
