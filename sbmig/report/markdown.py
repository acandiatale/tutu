"""report.md rendering — written for the as-is owners who pick up whatever the agent could not fix."""
from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Iterable, Optional

from sbmig.core.models import AdvisoryFinding, BlockedItem, BuildError, FixAttempt, PhaseResult, RunReport

FLAG_HELP = {
    "test-compile-skipped": "main code builds, but test sources only compile with `-Dmaven.test.skip=true` "
                            "(see follow-ups) — fix the tests before relying on CI",
    "tests-skipped": "main code compiles and packages, but some tests fail at runtime: the jar was built with "
                     "`-DskipTests` (failing tests listed in follow-ups / unresolved)",
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
}
FLAG_PREFIX_HELP = {
    "source-encoding-set:": "legacy source encoding detected; project.build.sourceEncoding was set accordingly",
    "stopped-after-": "the run was stopped on purpose after this phase (--stop-after)",
    "aborted:": "the run stopped early (see Blocked / follow-ups)",
}
BLOCKED_TITLE = "Blocked — needs human / library action"
HANDOVER_TITLE = "Handover — what we replaced, what is yours"
PROPERTIES_TITLE = "Properties to set before running"


def flag_help(flag: str) -> Optional[str]:
    if flag in FLAG_HELP:
        return FLAG_HELP[flag]
    return next((h for p, h in FLAG_PREFIX_HELP.items() if flag.startswith(p)), None)


def blocked_count_line(items: list[BlockedItem]) -> str:
    reasons = Counter(b.reason.value for b in items)
    return f"{len(items)} item(s) — " + ", ".join(f"{k} {v}" for k, v in reasons.most_common())


def _blocked_who(b: BlockedItem) -> str:
    if b.artifact is not None:
        who = f"`{b.artifact.coordinates}`"
        return who + (f" (class `{b.owner_class}`)" if b.owner_class else "")
    if b.owner_class:
        return f"class `{b.owner_class}`"
    return "-"


def _blocked_types(b: BlockedItem) -> str:
    if b.required_type or b.found_type:
        return f"`{b.required_type or '?'}` → `{b.found_type or '?'}`"
    return "-"


def blocked_rows(items: list[BlockedItem]) -> list[str]:
    rows = ["| reason | artifact / owner class | required → found | files | attempts | suggestion |",
            "|---|---|---|---|---|---|"]
    for b in items:
        files = ", ".join(f"`{f}`" for f in b.files[:5]) + (f" (+{len(b.files) - 5})" if len(b.files) > 5 else "")
        rows.append(f"| `{b.reason.value}` ({b.phase.value}) | {_cell(_blocked_who(b), 160)} | "
                    f"{_cell(_blocked_types(b), 200)} | {_cell(files or '-', 400)} | {b.attempts} | "
                    f"{_cell(b.suggestion, 500)} |")
    return rows


def _cell(text: object, limit: int = 160) -> str:
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s.replace("|", "\\|") or "-"


def _dt(d: Optional[datetime]) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S UTC") if d else "-"


def _duration(r: RunReport) -> str:
    if "total" in r.timings_s:
        secs = r.timings_s["total"]
    elif r.finished_at:
        secs = (r.finished_at - r.started_at).total_seconds()
    else:
        return "-"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s" if m else f"{secs:.1f}s"


def _what(a: FixAttempt) -> str:
    d = a.decision
    if d.handler_id:
        return f"handler `{d.handler_id}`"
    if d.recipe_ids:
        return ", ".join(f"`{r.rsplit('.', 1)[-1]}`" for r in d.recipe_ids)
    if d.kb_case_ids:
        return "KB case " + ", ".join(d.kb_case_ids)
    via = f"`{a.model}`" if a.model else "-"
    if a.advisory_ids:                       # round 6: the prompt carried these advisories' migration guidance
        via += " + advisory " + ", ".join(a.advisory_ids)
    return via


def _attempt_rows(phases: Iterable[PhaseResult]) -> list[str]:
    rows = ["| phase/iter | cluster | decision | via | outcome | commit | analysis |",
            "|---|---|---|---|---|---|---|"]
    for pr in phases:
        for a in pr.attempts:
            rows.append(f"| {pr.phase.value}/{a.iteration} | `{_cell(a.cluster_id, 40)}` | {a.decision.kind.value} | "
                        f"{_cell(_what(a), 60)} | {a.outcome} | {(a.commit or '-')[:8]} | "
                        f"{_cell(a.analysis or '; '.join(a.apply_errors) or a.decision.rationale, 120)} |")
    return rows


def group_by_file(errors: Iterable[BuildError]) -> "OrderedDict[str, list[BuildError]]":
    groups: "OrderedDict[str, list[BuildError]]" = OrderedDict()
    for e in errors:
        key = e.file or (f"module {e.module}" if e.module else
                         (f"artifact {e.artifact.coordinates}" if e.artifact else "(no file)"))
        groups.setdefault(key, []).append(e)
    return groups


def _v(v: Optional[str]) -> str:
    return f"`{v}`" if v else "-"


def _what_we_did(f: AdvisoryFinding) -> str:
    if f.recipes:
        src = {"plan": "in the plan", "advisory": "added for this advisory"}.get(f.recipe_source, "")
        return ", ".join(f"`{r.rsplit('.', 1)[-1]}`" for r in f.recipes) + (f" ({src})" if src else "")
    return "built with the as-is version pinned"


def properties_lines(h: Any, r: RunReport) -> list[str]:
    """Handover block "Properties to set before running" (round 8): table of external keys, guide, placeholder file."""
    from .handover import PROPERTY_COLUMNS, split_property_row

    L = [f"### {PROPERTIES_TITLE}", "",
         "The code reads these settings but no packaged resource defines them (the external container supplied "
         "them before). Set each one before the first start.", ""]
    L.append("| " + " | ".join(PROPERTY_COLUMNS) + " |")
    L.append("|" + "---|" * len(PROPERTY_COLUMNS))
    for row in h.properties_to_set:
        c = split_property_row(row)
        L.append(f"| `{_cell(c[0], 120)}` | {_cell(c[1], 40)} | {_cell(c[2], 300)} | {_cell(c[3], 300)} | "
                 f"{_cell(c[4], 120)} |")
    L.append("")
    if h.properties_guide:
        L += ["How to supply them:", ""]
        L += [f"- {_cell(g, 500)}" for g in h.properties_guide]
        L.append("")
    files = list(getattr(r.properties, "placeholder_files", None) or []) if r.properties is not None else []
    if files:
        L.append("Packaged placeholders: " + ", ".join(f"`{f}`" for f in files) + " (grep `__SET_ME__`)")
        L.append("")
    return L


def handover_lines(r: RunReport) -> list[str]:
    """The first section of report.md: versions replaced, libraries needing attention, 'from here on'."""
    L = [f"## {HANDOVER_TITLE}", ""]
    h = r.handover
    findings = list(r.advisories or [])
    has_versions = h is not None and bool(h.changes or h.added or h.removed or h.parent_before or h.parent_after)
    if not findings and not has_versions and not (h and (h.from_here or h.merge_conflicts or h.properties_to_set)):
        L.append("No version changes and no library advisories recorded for this run."
                 + (f" ({'; '.join(h.notes)})" if h and h.notes else ""))
        L.append("")
        return L
    if h is not None:
        L += ["### Versions replaced", ""]
        if h.parent_before or h.parent_after:
            L.append(f"- parent: {_v(h.parent_before)} → {_v(h.parent_after)}")
        if h.java_before or h.java_after:
            L.append(f"- java: {_v(h.java_before)} → {_v(h.java_after)}")
        if h.spring_boot_before or h.spring_boot_after:
            L.append(f"- spring boot: {_v(h.spring_boot_before)} → {_v(h.spring_boot_after)}")
        L.append("")
        if h.changes:
            L += ["| what | before | after | why |", "|---|---|---|---|"]
            for c in h.changes:
                warn = "⚠ " if c.carried_over else ""
                before = c.before or ("managed" if c.before_source == "managed" else "-")
                if c.before_source == "managed" and c.before:
                    before += " (managed)"
                after = c.after or ("managed" if c.after_source == "managed" else "-")
                L.append(f"| {warn}{c.kind} `{_cell(c.key, 120)}` | {_cell(before, 120)} | {_cell(after, 80)} | "
                         f"{_cell(c.reason, 80)} |")
            L.append("")
            if h.carried_over:
                one = len(h.carried_over) == 1
                L.append(f"⚠ {len(h.carried_over)} dependenc{'y' if one else 'ies'} pinned at the as-is version: "
                         f"the new parent / Boot BOM no longer manages {'it' if one else 'them'}.")
                L.append("")
        if h.added:
            L.append("Added: " + ", ".join(f"`{x}`" for x in h.added))
            L.append("")
        if h.removed:
            L.append("Removed: " + ", ".join(f"`{x}`" for x in h.removed))
            L.append("")
        if not h.changes and not h.added and not h.removed:
            L.append("No dependency versions changed.")
            L.append("")
        for n in h.notes:
            L.append(f"_note: {_cell(n, 300)}_")
        if h.notes:
            L.append("")
    if findings:
        L += ["### Libraries needing your attention", ""]
        L += ["| library | as-is | status | policy | what we did | outcome | what you do next | docs |",
              "|---|---|---|---|---|---|---|---|"]
        for f in findings:
            nxt = f.followup or f.summary
            if f.replacement:
                nxt = (nxt + " " if nxt else "") + f"→ `{f.replacement}`" + (
                    f" {f.replacement_version}" if f.replacement_version else "")
            docs = ", ".join(f"[{i}]({d})" for i, d in enumerate(f.docs[:3], 1)) or "-"
            outcome = {"replaced": "✅ replaced", "still-present": "⚠ still present"}.get(f.outcome, f.outcome)
            L.append(f"| **{_cell(f.library, 60)}** | {_cell(f.detected_version or '-', 40)} | {f.status} | "
                     f"{f.policy} | {_cell(_what_we_did(f), 160)} | {outcome} | {_cell(nxt, 400)} | {docs} |")
        L.append("")
    if h is not None and h.merge_conflicts:
        L += ["**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)", ""]
        L += [f"- {_cell(x, 500)}" for x in h.merge_conflicts]
        L.append("")
    if h is not None and h.properties_to_set:
        L += properties_lines(h, r)
    L += ["### From here on, your team", ""]
    items = h.from_here if h is not None else [f"{f.title}: {f.followup}".rstrip(": ") for f in findings
                                               if f.outcome != "replaced"]
    if items:
        L += [f"{i}. {_cell(x, 500)}" for i, x in enumerate(items, 1)]
    else:
        L.append("Nothing beyond the usual review — the migrated project builds with the versions above.")
    L.append("")
    return L


def render_markdown(report: RunReport) -> str:
    r = report
    L: list[str] = []
    ok = "✅ BUILD SUCCESS" if r.success else "❌ NOT BUILDING"
    L.append(f"# Migration report — {r.project_name}")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| **Result** | {ok} (final `mvn clean package`) |")
    L.append(f"| Case | `{r.case_type.value}` |")
    if r.profile is not None:
        p = r.profile
        L.append(f"| As-is | java {p.java_version or '?'}, spring boot {p.spring_boot_version or '-'}, "
                 f"spring {p.spring_version or '-'}, {len(p.poms)} pom(s) |")
    L.append(f"| Run id | `{r.run_id}` |")
    L.append(f"| Started / finished | {_dt(r.started_at)} / {_dt(r.finished_at)} ({_duration(r)}) |")
    u = r.llm_usage
    L.append(f"| LLM | {u.calls} call(s), {u.input_tokens} in / {u.output_tokens} out tokens"
             f"{f' ({u.model})' if u.model else ''} |")
    if r.flags:
        L.append(f"| Flags | {', '.join(f'`{f}`' for f in r.flags)} |")
    if r.blocked:
        L.append(f"| **Blocked** | {blocked_count_line(r.blocked)} (see below) |")
    L.append("")

    # ---- handover first: what we replaced, what the owning team does from here on
    L += handover_lines(r)

    # ---- blocked first: deliberately skipped, each row says what a human / the library owner must do
    if r.blocked:
        L.append(f"## {BLOCKED_TITLE}")
        L.append("")
        L.append(f"{len(r.blocked)} cluster(s) were deliberately left unchanged because the fix is outside this "
                 "project (a library, a repository, the environment) or the agent gave up on them. The rest of the "
                 "migration continued.")
        L.append("")
        L += blocked_rows(r.blocked)
        L.append("")
        for b in r.blocked:
            if not b.errors:
                continue
            L.append(f"<details><summary><code>{b.id}</code> — {len(b.errors)} error(s)</summary>")
            L.append("")
            for e in b.errors[:10]:
                loc = f"{e.file}:{e.line}" if e.file and e.line else (e.file or (e.artifact.coordinates if e.artifact
                                                                                  else e.module or "-"))
                L.append(f"- `{_cell(loc, 200)}` {_cell(e.message, 300)}")
            L.append("")
            L.append("</details>")
            L.append("")

    # ---- unresolved: it is what the reader acts on
    L.append("## Unresolved errors")
    L.append("")
    if not r.unresolved:
        L.append("None. " + ("The final build passed." if r.success else "No structured errors were captured; "
                                                                          "check the last build log."))
    else:
        L.append(f"{len(r.unresolved)} error(s) left after the last build"
                 + (" (blocked clusters excluded)" if r.blocked else "") + ", grouped by file:")
        L.append("")
        for key, errs in group_by_file(r.unresolved).items():
            L.append(f"### `{key}`")
            for e in errs:
                loc = f"L{e.line}: " if e.line else ""
                extra = f" — symbol: `{e.symbol}`" if e.symbol else ""
                where = f" — location: {e.location}" if e.location else ""
                L.append(f"- [{e.kind.value}] {loc}{_cell(e.message, 300)}{extra}{where}")
            L.append("")
    if r.final_build is not None:
        L.append(f"Last build log: `{r.final_build.log_path}`")
        L.append("")

    L.append("## Manual follow-ups")
    L.append("")
    if r.manual_followups:
        L += [f"- {f}" for f in r.manual_followups]
    else:
        L.append("None.")
    L.append("")

    if r.flags:
        L.append("## Flags")
        L.append("")
        for f in r.flags:
            h = flag_help(f)
            L.append(f"- `{f}`" + (f": {h}" if h else ""))
        L.append("")

    # ---- what we changed
    L.append("## What we changed")
    L.append("")
    titles = {s.id: s.title for s in (r.plan.steps if r.plan else [])}
    executed = [sid for pr in r.phases for sid in pr.step_ids]
    if executed:
        L.append("Plan steps executed:")
        L += [f"1. `{sid}` — {titles.get(sid, '')}" for sid in executed]
        L.append("")
    rw = [(pr.phase.value, x) for pr in r.phases for x in pr.rewrite_results]
    if rw:
        L.append("OpenRewrite runs:")
        L.append("")
        L.append("| phase | recipes | changed files | ok |")
        L.append("|---|---|---|---|")
        for ph, x in rw:
            L.append(f"| {ph} | {_cell(', '.join(x.recipes), 200)} | {len(x.changed_files)} | "
                     f"{'yes' if x.ok else 'no: ' + _cell(x.error, 80)} |")
        L.append("")
    struct = [n for pr in r.phases for n in pr.notes
              if n.startswith(("restructure.", "handlers ")) or "mandatory added" in n]
    if struct:
        L.append("Restructure / deterministic edits:")
        L += [f"- {_cell(n, 400)}" for n in struct]
        L.append("")

    # ---- per phase
    L.append("## Phases")
    L.append("")
    L.append("| phase | steps | builds | build ok | attempts by decision | outcomes |")
    L.append("|---|---|---|---|---|---|")
    for pr in r.phases:
        kinds = Counter(a.decision.kind.value for a in pr.attempts)
        outs = Counter(a.outcome for a in pr.attempts)
        L.append(f"| {pr.phase.value} | {_cell(', '.join(pr.step_ids), 80)} | {pr.iterations} | "
                 f"{'✅' if pr.build_ok else '❌'} | {_cell(', '.join(f'{k} {v}' for k, v in kinds.items()))} | "
                 f"{_cell(', '.join(f'{k} {v}' for k, v in outs.items()))} |")
    L.append("")
    if any(pr.attempts for pr in r.phases):
        L.append("## Fix attempts")
        L.append("")
        L += _attempt_rows(r.phases)
        L.append("")
    escal = [(pr.phase.value, it.iteration, d) for pr in r.phases for it in pr.iteration_records
             for d in it.decisions if d.kind.value == "escalate"]
    if escal:
        L.append("## Escalated clusters")
        L.append("")
        seen: set[str] = set()
        for ph, it, d in escal:
            if d.cluster_id in seen:
                continue
            seen.add(d.cluster_id)
            L.append(f"- {ph}/{it} `{d.cluster_id}`: {_cell(d.rationale, 300)}")
        L.append("")

    # ---- KB
    L.append("## Knowledge base")
    L.append("")
    h = r.harvest
    if h is None:
        L.append("Nothing harvested.")
    else:
        L.append(f"- cases created: {', '.join(h.cases_created) or '-'}")
        L.append(f"- recipe candidates created: {', '.join(h.candidates_created) or '-'}")
        L.append(f"- recipes credited: {', '.join(h.recipes_credited) or '-'}")
        L += [f"- {n}" for n in h.notes]
    used = sorted({rid for pr in r.phases for a in pr.attempts for rid in a.decision.recipe_ids})
    if used:
        L.append(f"- recipes run by triage: {', '.join(f'`{x}`' for x in used)}")
    L.append("")

    notes = [(pr.phase.value, n) for pr in r.phases for n in pr.notes]
    if notes or (r.plan and r.plan.notes):
        L.append("## Notes")
        L.append("")
        for n in (r.plan.notes if r.plan else []):
            L.append(f"- plan: {_cell(n, 400)}")
        for ph, n in notes:
            L.append(f"- {ph}: {_cell(n, 400)}")
        L.append("")

    if r.outputs:
        L.append("## Outputs")
        L.append("")
        L += [f"- {k}: `{v}`" for k, v in r.outputs.items()]
        L.append("")
    return "\n".join(L).rstrip() + "\n"
