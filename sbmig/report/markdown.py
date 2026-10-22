"""report.md rendering — written for the as-is owners who pick up whatever the agent could not fix."""
from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Iterable, Optional

from sbmig.core.models import AdvisoryFinding, BlockedItem, BuildError, FixAttempt, PhaseResult, RunReport

from .i18n import normalize, tr, tr_line

FLAG_HELP = {
    "test-compile-skipped": "main code builds, but test sources only compile with `-Dmaven.test.skip=true` "
                            "(see follow-ups) — fix the tests before relying on CI",
    "tests-skipped": "main code compiles and packages, but some tests fail at runtime: the jar was built with "
                     "`-DskipTests` (failing tests listed in follow-ups / unresolved)",
    "tests-not-compiled": "every build ran with `-Dmaven.test.skip=true` (build.skip_test_compile): test sources were "
                          "neither compiled nor run — the owning team compiles and fixes them",
    "time-budget-exhausted": "the run hit `loop.max_total_minutes`; remaining loops were skipped, verify still ran",
    "llm-budget-exhausted": "the run used all `loop.max_llm_calls`; remaining clusters were escalated",
    "dry-run": "dry run: decisions were computed but nothing was applied",
    "aborted": "the run stopped early (see follow-ups for the reason)",
    "mvn-not-found": "Maven was not runnable on this machine",
    "llm-unavailable": "the LLM failed twice in a row (transport/config); LLM-only clusters are listed as blocked "
                       "(llm_unavailable) — handlers and recipes still ran",
    "aborted:parent-not-found": "the configured in-house parent POM is not resolvable — nothing can build until it is "
                                "(see Blocked)",
    "enforcer-relaxed": "a maven-enforcer rule that fails on JDK 21 was relaxed in the pom",
    "enforcer-skipped": "maven-enforcer is skipped (-Denforcer.skip=true) — review the rules",
    "werror-removed": "-Werror was removed from the compiler configuration (JDK 21 deprecations would be fatal)",
    "managed-versions-carried-over": "some dependencies were pinned at the version the as-is parent managed, because "
                                     "the new parent / Boot BOM no longer manages them (⚠ in Handover) — the owners "
                                     "must confirm or replace them",
    "rewrite-parse-problems": "OpenRewrite could not parse some source files (see the phase notes / rewrite log): "
                              "recipes that need type information skipped them; the source handlers and the fix loop "
                              "covered what the build found",
    "jakarta-leftovers-renamed": "javax.* Jakarta EE references the recipes had left in the sources were renamed to "
                                 "jakarta.* by the jakarta-namespace-sweep handler (string literals / resource files "
                                 "are listed as follow-ups)",
    "spring-data-crud-restored": "repository interfaces that extended only PagingAndSortingRepository got "
                                 "ListCrudRepository<T, ID> back (Spring Data 3 removed its CRUD methods)",
    "javax-api-on-classpath": "javax EE API jars are still on the resolved classpath after the migration (section "
                              "\"javax API jars still on the classpath\"): the direct dependencies that drag them in "
                              "are compiled against javax and need Jakarta builds",
    "jakarta-library-bumped": "a third-party library with a known jakarta.* release was raised to it "
                              "(knowledge/libraries/jakarta_libraries.yml, handler jakarta-library-floor)",
}
FLAG_PREFIX_HELP = {
    "source-encoding-set:": "legacy source encoding detected; project.build.sourceEncoding was set accordingly",
    "stopped-after-": "the run was stopped on purpose after this phase (--stop-after)",
    "aborted:": "the run stopped early (see Blocked / follow-ups)",
}
BLOCKED_TITLE = "Blocked — needs human / library action"
HANDOVER_TITLE = "Handover — what we replaced, what is yours"
PROPERTIES_TITLE = "Properties to set before running"
JAVAX_JARS_TITLE = "javax API jars still on the classpath"


def flag_help(flag: str, lang: str = "en") -> Optional[str]:
    if flag in FLAG_HELP:
        return tr(lang, FLAG_HELP[flag])
    h = next((h for p, h in FLAG_PREFIX_HELP.items() if flag.startswith(p)), None)
    return tr(lang, h) if h is not None else None


def blocked_count_line(items: list[BlockedItem], lang: str = "en") -> str:
    reasons = Counter(b.reason.value for b in items)
    return tr(lang, "{n} item(s) — ", n=len(items)) + ", ".join(f"{k} {v}" for k, v in reasons.most_common())


def _blocked_who(b: BlockedItem, lang: str = "en") -> str:
    if b.artifact is not None:
        who = f"`{b.artifact.coordinates}`"
        if b.via_direct is not None:            # the library that drags the javax API jar in (dependency:tree)
            who += f" ← `{b.via_direct.coordinates}`"
        elif len(b.via) == 2:
            who += tr(lang, " ← declared in the pom")
        return who + (tr(lang, " (class `{owner}`)", owner=b.owner_class) if b.owner_class else "")
    if b.owner_class:
        return tr(lang, "class `{owner}`", owner=b.owner_class)
    return "-"


def _blocked_types(b: BlockedItem) -> str:
    if b.required_type or b.found_type:
        return f"`{b.required_type or '?'}` → `{b.found_type or '?'}`"
    return "-"


def blocked_rows(items: list[BlockedItem], lang: str = "en") -> list[str]:
    rows = [tr(lang, "| reason | artifact / owner class | required → found | files | attempts | suggestion |"),
            "|---|---|---|---|---|---|"]
    for b in items:
        files = ", ".join(f"`{f}`" for f in b.files[:5]) + (f" (+{len(b.files) - 5})" if len(b.files) > 5 else "")
        rows.append(f"| `{b.reason.value}` ({b.phase.value}) | {_cell(_blocked_who(b, lang), 160)} | "
                    f"{_cell(_blocked_types(b), 200)} | {_cell(files or '-', 400)} | {b.attempts} | "
                    f"{_cell(b.suggestion, 500)} |")
    return rows


def _in_house_groups(r: RunReport, cfg: Any = None) -> list[str]:
    """Company groups for "in-house → Jakarta build": the configured parent / mandatory / optional groups when the
    config is at hand, else the group(s) of the final parent(s) in the Handover (never Spring's own groups)."""
    out: list[str] = []
    arts = getattr(cfg, "artifacts", None) if cfg is not None else None
    if arts is not None:
        try:
            out += [s.group_id for s in [*arts.parent_specs(), *arts.mandatory, *arts.optional]]
        except Exception:  # pragma: no cover
            pass
    h = r.handover
    if h is not None and h.parent_after:
        out += [c.strip().split(":", 1)[0] for c in h.parent_after.split(",") if ":" in c]
    return [g for g in dict.fromkeys(out) if g and not g.startswith("org.springframework")]


def _javax_what_to_do(j: Any, groups: list[str], lang: str = "en") -> str:
    via = j.via
    if via is not None:
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            lib = jakarta_library_for(via.key)
        except Exception:  # pragma: no cover - knowledge table unreadable
            lib = None
        if lib is not None:
            if via.key != lib.key:                       # earlier coordinates (ch.simas.qlrm → org.qlrm)
                txt = tr(lang, "coordinates changed: relocate to `{key}:{version}` (jakarta.* since {since})",
                         key=lib.key, version=lib.version, since=lib.jakarta_since)
            else:
                txt = tr(lang, "raise to {version} (jakarta.* since {since})", version=lib.version,
                         since=lib.jakarta_since)
            if lib.requires_java:
                txt += tr(lang, ", requires Java {java}", java=lib.requires_java)
            return txt + (f" — {lib.note}" if lib.note else "")
        if any(via.group_id == g or via.group_id.startswith(g + ".") for g in groups):
            return tr(lang, "in-house → Jakarta build")
        return tr(lang, "upgrade / replace")
    if j.declared:
        return tr(lang, "remove the declaration (handler dependency-relocation)")
    return tr(lang, "upgrade / replace")


def javax_api_lines(r: RunReport, lang: str = "en", cfg: Any = None) -> list[str]:
    """`## javax API jars still on the classpath` (report.javax_api_inventory, 2026-10-22); [] when none."""
    jars = list(r.javax_api_jars or [])
    if not jars:
        return []
    groups = _in_house_groups(r, cfg)
    L = [f"## {tr(lang, JAVAX_JARS_TITLE)}", "",
         tr(lang, "{n} javax EE API jar(s) are still on the resolved classpath after the migration "
                  "(`mvn dependency:tree`). The library in \"via\" is compiled against javax: code that passes "
                  "jakarta.* types to it does not compile, and it fails at runtime.", n=len(jars)), "",
         tr(lang, "| artifact | module | scope | via (direct dependency) | declared in pom | what to do |"),
         "|---|---|---|---|---|---|"]
    for j in jars:
        via = f"`{j.via.coordinates}`" if j.via is not None else "-"
        L.append(f"| `{_cell(j.artifact.coordinates, 120)}` | {_cell(j.module or tr(lang, '(root)'), 60)} | "
                 f"{j.scope or '-'} | {_cell(via, 120)} | {tr(lang, 'yes') if j.declared else tr(lang, 'no')} | "
                 f"{_cell(_javax_what_to_do(j, groups, lang), 400)} |")
    L.append("")
    return L


def _cell(text: object, limit: int = 160) -> str:
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s.replace("|", "\\|") or "-"


def _dt(d: Optional[datetime]) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S UTC") if d else "-"


def _duration(r: RunReport, lang: str = "en") -> str:
    if "total" in r.timings_s:
        secs = r.timings_s["total"]
    elif r.finished_at:
        secs = (r.finished_at - r.started_at).total_seconds()
    else:
        return "-"
    m, s = divmod(int(secs), 60)
    return tr(lang, "{m}m {s}s", m=m, s=f"{s:02d}") if m else tr(lang, "{secs}s", secs=f"{secs:.1f}")


def _what(a: FixAttempt, lang: str = "en") -> str:
    d = a.decision
    if d.handler_id:
        return tr(lang, "handler `{handler}`", handler=d.handler_id)
    if d.recipe_ids:
        return ", ".join(f"`{r.rsplit('.', 1)[-1]}`" for r in d.recipe_ids)
    if d.kb_case_ids:
        return tr(lang, "KB case {cases}", cases=", ".join(d.kb_case_ids))
    via = f"`{a.model}`" if a.model else "-"
    if a.advisory_ids:                       # round 6: the prompt carried these advisories' migration guidance
        via += tr(lang, " + advisory {ids}", ids=", ".join(a.advisory_ids))
    return via


def _attempt_rows(phases: Iterable[PhaseResult], lang: str = "en") -> list[str]:
    rows = [tr(lang, "| phase/iter | cluster | decision | via | outcome | commit | analysis |"),
            "|---|---|---|---|---|---|---|"]
    for pr in phases:
        for a in pr.attempts:
            rows.append(f"| {pr.phase.value}/{a.iteration} | `{_cell(a.cluster_id, 40)}` | {a.decision.kind.value} | "
                        f"{_cell(_what(a, lang), 60)} | {a.outcome} | {(a.commit or '-')[:8]} | "
                        f"{_cell(a.analysis or '; '.join(a.apply_errors) or a.decision.rationale, 120)} |")
    return rows


def group_by_file(errors: Iterable[BuildError], lang: str = "en") -> "OrderedDict[str, list[BuildError]]":
    groups: "OrderedDict[str, list[BuildError]]" = OrderedDict()
    for e in errors:
        key = e.file or (tr(lang, "module {module}", module=e.module) if e.module else
                         (tr(lang, "artifact {coords}", coords=e.artifact.coordinates) if e.artifact
                          else tr(lang, "(no file)")))
        groups.setdefault(key, []).append(e)
    return groups


def _v(v: Optional[str]) -> str:
    return f"`{v}`" if v else "-"


def _what_we_did(f: AdvisoryFinding, lang: str = "en") -> str:
    if f.recipes:
        src = {"plan": tr(lang, "in the plan"), "advisory": tr(lang, "added for this advisory")}.get(f.recipe_source, "")
        return ", ".join(f"`{r.rsplit('.', 1)[-1]}`" for r in f.recipes) + (f" ({src})" if src else "")
    return tr(lang, "built with the as-is version pinned")


def properties_lines(h: Any, r: RunReport, lang: str = "en") -> list[str]:
    """Handover block "Properties to set before running" (round 8): table of external keys, guide, placeholder file."""
    from .handover import PROPERTY_COLUMNS, split_property_row

    L = [f"### {tr(lang, PROPERTIES_TITLE)}", "",
         tr(lang, "The code reads these settings but no packaged resource defines them (the external container "
                  "supplied them before). Set each one before the first start."), ""]
    L.append("| " + " | ".join(tr(lang, c) for c in PROPERTY_COLUMNS) + " |")
    L.append("|" + "---|" * len(PROPERTY_COLUMNS))
    for row in h.properties_to_set:
        c = split_property_row(row)
        L.append(f"| `{_cell(c[0], 120)}` | {_cell(c[1], 40)} | {_cell(c[2], 300)} | {_cell(c[3], 300)} | "
                 f"{_cell(c[4], 120)} |")
    L.append("")
    if h.properties_guide:
        L += [tr(lang, "How to supply them:"), ""]
        L += [f"- {_cell(g, 500)}" for g in h.properties_guide]
        L.append("")
    files = list(getattr(r.properties, "placeholder_files", None) or []) if r.properties is not None else []
    if files:
        L.append(tr(lang, "Packaged placeholders: {files} (grep `__SET_ME__`)",
                    files=", ".join(f"`{f}`" for f in files)))
        L.append("")
    return L


def handover_lines(r: RunReport, lang: str = "en") -> list[str]:
    """The first section of report.md: versions replaced, libraries needing attention, 'from here on'."""
    lang = normalize(lang)
    L = [f"## {tr(lang, HANDOVER_TITLE)}", ""]
    h = r.handover
    findings = list(r.advisories or [])
    has_versions = h is not None and bool(h.changes or h.added or h.removed or h.parent_before or h.parent_after)
    if not findings and not has_versions and not (h and (h.from_here or h.merge_conflicts or h.properties_to_set)):
        L.append(tr(lang, "No version changes and no library advisories recorded for this run.")
                 + (f" ({'; '.join(tr_line(lang, n) for n in h.notes)})" if h and h.notes else ""))
        L.append("")
        return L
    if h is not None:
        L += [tr(lang, "### Versions replaced"), ""]
        if h.parent_before or h.parent_after:
            L.append(tr(lang, "- parent: {before} → {after}", before=_v(h.parent_before), after=_v(h.parent_after)))
        if h.java_before or h.java_after:
            L.append(tr(lang, "- java: {before} → {after}", before=_v(h.java_before), after=_v(h.java_after)))
        if h.spring_boot_before or h.spring_boot_after:
            L.append(tr(lang, "- spring boot: {before} → {after}", before=_v(h.spring_boot_before),
                        after=_v(h.spring_boot_after)))
        L.append("")
        if h.changes:
            L += [tr(lang, "| what | before | after | why |"), "|---|---|---|---|"]
            managed = tr(lang, "managed")
            for c in h.changes:
                warn = "⚠ " if c.carried_over else ""
                before = c.before or (managed if c.before_source == "managed" else "-")
                if c.before_source == "managed" and c.before:
                    before += tr(lang, " (managed)")
                after = c.after or (managed if c.after_source == "managed" else "-")
                L.append(f"| {warn}{c.kind} `{_cell(c.key, 120)}` | {_cell(before, 120)} | {_cell(after, 80)} | "
                         f"{_cell(tr(lang, c.reason), 80)} |")
            L.append("")
            if h.carried_over:
                if len(h.carried_over) == 1:
                    L.append(tr(lang, "⚠ 1 dependency pinned at the as-is version: the new parent / Boot BOM no "
                                      "longer manages it."))
                else:
                    L.append(tr(lang, "⚠ {n} dependencies pinned at the as-is version: the new parent / Boot BOM "
                                      "no longer manages them.", n=len(h.carried_over)))
                L.append("")
        if h.added:
            L.append(tr(lang, "Added: {items}", items=", ".join(f"`{x}`" for x in h.added)))
            L.append("")
        if h.removed:
            L.append(tr(lang, "Removed: {items}", items=", ".join(f"`{x}`" for x in h.removed)))
            L.append("")
        if not h.changes and not h.added and not h.removed:
            L.append(tr(lang, "No dependency versions changed."))
            L.append("")
        for n in h.notes:
            L.append(tr(lang, "_note: {note}_", note=_cell(tr_line(lang, n), 300)))
        if h.notes:
            L.append("")
    if findings:
        L += [tr(lang, "### Libraries needing your attention"), ""]
        L += [tr(lang, "| library | as-is | status | policy | what we did | outcome | what you do next | docs |"),
              "|---|---|---|---|---|---|---|---|"]
        outcomes = {"replaced": tr(lang, "✅ replaced"), "still-present": tr(lang, "⚠ still present")}
        for f in findings:
            nxt = f.followup or f.summary
            if f.replacement:
                nxt = (nxt + " " if nxt else "") + f"→ `{f.replacement}`" + (
                    f" {f.replacement_version}" if f.replacement_version else "")
            docs = ", ".join(f"[{i}]({d})" for i, d in enumerate(f.docs[:3], 1)) or "-"
            outcome = outcomes.get(f.outcome, f.outcome)
            L.append(f"| **{_cell(f.library, 60)}** | {_cell(f.detected_version or '-', 40)} | {f.status} | "
                     f"{f.policy} | {_cell(_what_we_did(f, lang), 160)} | {outcome} | {_cell(nxt, 400)} | {docs} |")
        L.append("")
    if h is not None and h.merge_conflicts:
        L += [tr(lang, "**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)"), ""]
        L += [f"- {_cell(tr_line(lang, x), 500)}" for x in h.merge_conflicts]
        L.append("")
    if h is not None and h.properties_to_set:
        L += properties_lines(h, r, lang)
    L += [tr(lang, "### From here on, your team"), ""]
    items = h.from_here if h is not None else [f"{f.title}: {f.followup}".rstrip(": ") for f in findings
                                               if f.outcome != "replaced"]
    if items:
        L += [f"{i}. {_cell(tr_line(lang, x), 500)}" for i, x in enumerate(items, 1)]
    else:
        L.append(tr(lang, "Nothing beyond the usual review — the migrated project builds with the versions above."))
    L.append("")
    return L


def _cost_summary(c: Any, lang: str = "en") -> str:
    from .cost import fmt_money
    txt = fmt_money(c.total, c.currency)
    if c.unpriced_models:
        txt += " " + tr(lang, "(+ unpriced: {models})", models=", ".join(c.unpriced_models))
    if c.usage_missing:
        txt += " " + tr(lang, "(token counts missing for some calls)")
    return txt


def cost_lines(r: RunReport, lang: str = "en") -> list[str]:
    """`## LLM usage and cost`: one row per model (fix / escalation / recipe), list prices from `llm.pricing`."""
    from .cost import fmt_money
    c = r.llm_cost
    if c is None or not (c.by_model or r.llm_usage.calls):
        return []
    L = [tr(lang, "## LLM usage and cost"), ""]
    L.append(tr(lang, "| Model | Calls | Input tokens | Output tokens | Price in / out (per 1M) | Cost |"))
    L.append("|---|---|---|---|---|---|")
    for m in c.by_model:
        price = (f"{fmt_money(m.input_per_mtok, c.currency)} / {fmt_money(m.output_per_mtok, c.currency)}"
                 if m.cost is not None else tr(lang, "no price configured"))
        L.append(f"| `{m.model}` | {m.calls} | {m.input_tokens:,} | {m.output_tokens:,} | {price} | "
                 f"{fmt_money(m.cost, c.currency) if m.cost is not None else '-'} |")
    if not c.by_model:
        L.append(tr(lang, "| (no per-model usage recorded) | {calls} | {inp:,} | {out:,} | - | - |",
                    calls=r.llm_usage.calls, inp=r.llm_usage.input_tokens, out=r.llm_usage.output_tokens))
    L.append(tr(lang, "| **Total** | | | | | **{total}** |", total=fmt_money(c.total, c.currency)))
    L.append("")
    notes = []
    if c.note:
        notes.append(tr(lang, "Prices: {note}.", note=c.note))
    if c.unpriced_models:
        notes.append(tr(lang, "No price for {models} in `llm.pricing` — not included in the total.",
                        models=", ".join(f"`{m}`" for m in c.unpriced_models)))
    if c.usage_missing:
        notes.append(tr(lang, "Some calls came back without token counts (the gateway did not report `usage`); "
                              "their cost is not included."))
    notes.append(tr(lang, "Cost = input tokens × input price + output tokens × output price, per 1M tokens, in "
                          "{currency}; cached-input discounts are not visible to sbmig.", currency=c.currency))
    L += [f"_{n}_" for n in notes]
    L.append("")
    return L


def render_markdown(report: RunReport, language: str = "en", cfg: Any = None) -> str:
    """report.md for `language` (`report.language`: "en" | "ko"; anything else renders English). `cfg` (optional)
    only sharpens the javax API section's in-house detection."""
    lang = normalize(language)
    r = report
    L: list[str] = []
    ok = tr(lang, "✅ BUILD SUCCESS") if r.success else tr(lang, "❌ NOT BUILDING")
    L.append(tr(lang, "# Migration report — {name}", name=r.project_name))
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(tr(lang, "| **Result** | {ok} (final `mvn clean package`) |", ok=ok))
    L.append(tr(lang, "| Case | `{case}` |", case=r.case_type.value))
    if r.profile is not None:
        p = r.profile
        L.append(tr(lang, "| As-is | java {java}, spring boot {boot}, spring {spring}, {poms} pom(s) |",
                    java=p.java_version or '?', boot=p.spring_boot_version or '-', spring=p.spring_version or '-',
                    poms=len(p.poms)))
    L.append(tr(lang, "| Run id | `{run_id}` |", run_id=r.run_id))
    L.append(tr(lang, "| Started / finished | {started} / {finished} ({duration}) |", started=_dt(r.started_at),
                finished=_dt(r.finished_at), duration=_duration(r, lang)))
    u = r.llm_usage
    L.append(tr(lang, "| LLM | {calls} call(s), {inp} in / {out} out tokens{model} |", calls=u.calls,
                inp=u.input_tokens, out=u.output_tokens, model=f" ({u.model})" if u.model else ""))
    c = r.llm_cost
    if c is not None and (c.by_model or u.calls):
        L.append(tr(lang, "| LLM cost | {cost} (see \"LLM usage and cost\") |",
                    cost=_cost_summary(c, lang)))
    if r.flags:
        L.append(tr(lang, "| Flags | {flags} |", flags=", ".join(f"`{f}`" for f in r.flags)))
    if r.blocked:
        L.append(tr(lang, "| **Blocked** | {line} (see below) |", line=blocked_count_line(r.blocked, lang)))
    if r.javax_api_jars:
        L.append(tr(lang, "| javax API jars | {n} |", n=len(r.javax_api_jars)))
    L.append("")

    # ---- handover first: what we replaced, what the owning team does from here on
    L += handover_lines(r, lang)

    # ---- blocked first: deliberately skipped, each row says what a human / the library owner must do
    if r.blocked:
        L.append(f"## {tr(lang, BLOCKED_TITLE)}")
        L.append("")
        L.append(tr(lang, "{n} cluster(s) were deliberately left unchanged because the fix is outside this project (a "
                          "library, a repository, the environment) or the agent gave up on them. The rest of the "
                          "migration continued.", n=len(r.blocked)))
        L.append("")
        L += blocked_rows(r.blocked, lang)
        L.append("")
        for b in r.blocked:
            if not b.errors:
                continue
            L.append(tr(lang, "<details><summary><code>{id}</code> — {n} error(s)</summary>", id=b.id,
                        n=len(b.errors)))
            L.append("")
            for e in b.errors[:10]:
                loc = f"{e.file}:{e.line}" if e.file and e.line else (e.file or (e.artifact.coordinates if e.artifact
                                                                                  else e.module or "-"))
                L.append(f"- `{_cell(loc, 200)}` {_cell(e.message, 300)}")
            L.append("")
            L.append("</details>")
            L.append("")

    # ---- javax API jars still on the classpath (X1b): the libraries the Blocked rows point at
    L += javax_api_lines(r, lang, cfg)

    # ---- unresolved: it is what the reader acts on
    L.append(tr(lang, "## Unresolved errors"))
    L.append("")
    if not r.unresolved:
        L.append(tr(lang, "None. The final build passed.") if r.success
                 else tr(lang, "None. No structured errors were captured; check the last build log."))
    else:
        if r.blocked:
            L.append(tr(lang, "{n} error(s) left after the last build (blocked clusters excluded), grouped by file:",
                        n=len(r.unresolved)))
        else:
            L.append(tr(lang, "{n} error(s) left after the last build, grouped by file:", n=len(r.unresolved)))
        L.append("")
        for key, errs in group_by_file(r.unresolved, lang).items():
            L.append(f"### `{key}`")
            for e in errs:
                loc = f"L{e.line}: " if e.line else ""
                extra = tr(lang, " — symbol: `{symbol}`", symbol=e.symbol) if e.symbol else ""
                where = tr(lang, " — location: {location}", location=e.location) if e.location else ""
                L.append(f"- [{e.kind.value}] {loc}{_cell(e.message, 300)}{extra}{where}")
            L.append("")
    if r.final_build is not None:
        L.append(tr(lang, "Last build log: `{path}`", path=r.final_build.log_path))
        L.append("")

    L.append(tr(lang, "## Manual follow-ups"))
    L.append("")
    if r.manual_followups:
        L += [f"- {f}" for f in r.manual_followups]
    else:
        L.append(tr(lang, "None."))
    L.append("")

    if r.flags:
        L.append(tr(lang, "## Flags"))
        L.append("")
        for f in r.flags:
            h = flag_help(f, lang)
            L.append(f"- `{f}`" + (f": {h}" if h else ""))
        L.append("")

    # ---- what we changed
    L.append(tr(lang, "## What we changed"))
    L.append("")
    titles = {s.id: s.title for s in (r.plan.steps if r.plan else [])}
    executed = [sid for pr in r.phases for sid in pr.step_ids]
    if executed:
        L.append(tr(lang, "Plan steps executed:"))
        L += [f"1. `{sid}` — {titles.get(sid, '')}" for sid in executed]
        L.append("")
    rw = [(pr.phase.value, x) for pr in r.phases for x in pr.rewrite_results]
    if rw:
        L.append(tr(lang, "OpenRewrite runs:"))
        L.append("")
        L.append(tr(lang, "| phase | recipes | changed files | ok |"))
        L.append("|---|---|---|---|")
        for ph, x in rw:
            L.append(f"| {ph} | {_cell(', '.join(x.recipes), 200)} | {len(x.changed_files)} | "
                     f"{tr(lang, 'yes') if x.ok else tr(lang, 'no: {error}', error=_cell(x.error, 80))} |")
        L.append("")
    struct = [n for pr in r.phases for n in pr.notes
              if n.startswith(("restructure.", "handlers ")) or "mandatory added" in n]
    if struct:
        L.append(tr(lang, "Restructure / deterministic edits:"))
        L += [f"- {_cell(n, 400)}" for n in struct]
        L.append("")

    # ---- per phase
    L.append(tr(lang, "## Phases"))
    L.append("")
    L.append(tr(lang, "| phase | steps | builds | build ok | attempts by decision | outcomes |"))
    L.append("|---|---|---|---|---|---|")
    for pr in r.phases:
        kinds = Counter(a.decision.kind.value for a in pr.attempts)
        outs = Counter(a.outcome for a in pr.attempts)
        L.append(f"| {pr.phase.value} | {_cell(', '.join(pr.step_ids), 80)} | {pr.iterations} | "
                 f"{'✅' if pr.build_ok else '❌'} | {_cell(', '.join(f'{k} {v}' for k, v in kinds.items()))} | "
                 f"{_cell(', '.join(f'{k} {v}' for k, v in outs.items()))} |")
    L.append("")
    if any(pr.attempts for pr in r.phases):
        L.append(tr(lang, "## Fix attempts"))
        L.append("")
        L += _attempt_rows(r.phases, lang)
        L.append("")
    escal = [(pr.phase.value, it.iteration, d) for pr in r.phases for it in pr.iteration_records
             for d in it.decisions if d.kind.value == "escalate"]
    if escal:
        L.append(tr(lang, "## Escalated clusters"))
        L.append("")
        seen: set[str] = set()
        for ph, it, d in escal:
            if d.cluster_id in seen:
                continue
            seen.add(d.cluster_id)
            L.append(f"- {ph}/{it} `{d.cluster_id}`: {_cell(d.rationale, 300)}")
        L.append("")

    # ---- LLM usage and cost (2026-10-22: the company wants the API cost per project)
    L += cost_lines(r, lang)

    # ---- KB
    L.append(tr(lang, "## Knowledge base"))
    L.append("")
    h = r.harvest
    if h is None:
        L.append(tr(lang, "Nothing harvested."))
    else:
        L.append(tr(lang, "- cases created: {x}", x=", ".join(h.cases_created) or "-"))
        L.append(tr(lang, "- recipe candidates created: {x}", x=", ".join(h.candidates_created) or "-"))
        L.append(tr(lang, "- recipes credited: {x}", x=", ".join(h.recipes_credited) or "-"))
        L += [f"- {n}" for n in h.notes]
    used = sorted({rid for pr in r.phases for a in pr.attempts for rid in a.decision.recipe_ids})
    if used:
        L.append(tr(lang, "- recipes run by triage: {x}", x=", ".join(f"`{x}`" for x in used)))
    L.append("")

    notes = [(pr.phase.value, n) for pr in r.phases for n in pr.notes]
    if notes or (r.plan and r.plan.notes):
        L.append(tr(lang, "## Notes"))
        L.append("")
        for n in (r.plan.notes if r.plan else []):
            L.append(tr(lang, "- plan: {note}", note=_cell(n, 400)))
        for ph, n in notes:
            L.append(f"- {ph}: {_cell(n, 400)}")
        L.append("")

    if r.outputs:
        L.append(tr(lang, "## Outputs"))
        L.append("")
        L += [f"- {k}: `{v}`" for k, v in r.outputs.items()]
        L.append("")
    return "\n".join(L).rstrip() + "\n"
