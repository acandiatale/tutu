"""report.md rendering — written for the as-is owners who pick up whatever the agent could not fix."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Iterable, Optional

from sbmig.core.models import AdvisoryFinding, BlockedItem, BuildError, RunReport, StructureReview

from .errors import (
    case_rows,
    counts_text,
    entries_of,
    entry_text,
    notes_text,
    review_changes,
    seen_groups,
    stats_text,
    status_counts,
    tries_text,
    where,
)
from .i18n import normalize, tr, tr_line
from .result_line import report_result_line

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
    "llm-unavailable": "the LLM failed twice in a row (transport/config); the cases still needing a fix script are "
                       "listed as blocked (llm_unavailable) — stored scripts and the final build still ran",
    "aborted:parent-not-found": "the configured in-house parent POM is not resolvable — nothing can build until it is "
                                "(see Blocked)",
    "rewrite-parse-problems": "OpenRewrite could not parse some source files (see the phase notes / rewrite log): "
                              "recipes that need type information skipped them; the fix loop covered what the build "
                              "found",
    "javax-api-on-classpath": "javax EE API jars are still on the resolved classpath after the migration (section "
                              "\"javax API jars still on the classpath\"): the direct dependencies that drag them in "
                              "are compiled against javax and need Jakarta builds",
    "web-xml-registered": "web.xml servlets / filters / listeners / error pages were turned into a generated "
                          "WebXmlRegistrations @Configuration in the boot module (web.xml itself is not read by "
                          "Spring Boot) — verify the mappings in the Handover",
    "aggregator-generated": "the project had no pom.xml at its root but sibling poms that depend on each other (e.g. APP/BATCH, APP/WEB, APP/EAR): sbmig generated a root aggregator pom so they build in one reactor and are merged into the company layout (layout.generate_aggregator)",
    "ear-without-modules": "the input was an EAR pom whose modules are not in the directory (e.g. APP/EAR alone): run sbmig on the directory that contains every module",
    "ear-module-dropped": "an EAR module (application-server packaging) was dropped: its modules were merged and the boot module is deployed instead; descriptors are kept under asis-ear/ and the context root is a follow-up",
    "war-overlay-moved": "a WAR dependency (maven-war-plugin overlay) was moved from the jar modules to the boot WAR — the overlay WAR must be resolvable from your repositories",
    "web-content-left": "web content that no pom references (e.g. a STATIC/ folder with JSPs / downloads) was left in place — serve it as before or move it into the front module",
    "jdk-not-21": "`mvn -v` reported another Java version than `build.require_java_major`: set build.java_home / "
                  "JAVA_HOME to a JDK 21 and run again",
    "llm-config-error": "the LLM is not configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env, or the "
                        "gateway rejected the key / model): LLM fixes were skipped — check with `sbmig llm-probe`",
    "major-upgrade": "a fix raised a dependency across its major version or replaced a library (the build passes): "
                     "the operator verifies the runtime behaviour — see \"Operator checks\" in the Handover",
}
FLAG_PREFIX_HELP = {
    "stopped-after-": "the run was stopped on purpose after this phase (--stop-after)",
    "aborted:": "the run stopped early (see Blocked / follow-ups)",
    "infra:": "the run ended for an infrastructure reason (LLM gateway, Maven, JDK, repository / Nexus, disk), not "
              "because of the project: fix the environment and run it again (a fleet re-queues the project)",
}
BLOCKED_TITLE = "Blocked — needs human / library action"
HANDOVER_TITLE = "Handover — what we replaced, what is yours"
PROPERTIES_TITLE = "Properties to set before running"
JAVAX_JARS_TITLE = "javax API jars still on the classpath"
REVIEW_TITLE = "Structure review"
REVIEW_MAX_ROWS = 30
REVIEW_FILES_SHOWN = 8


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
        return tr(lang, "remove the declaration from the pom")
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
    """Advisories are guide-only: the library was replaced during the run (the plan's recipes / fix scripts) or the
    build kept the as-is version."""
    if f.outcome == "replaced":
        return tr(lang, "replaced during the run")
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


OPERATOR_GUIDE_LINES = 20                    # guide lines per check in report.md (report.json keeps them all)


def _guide_items(guide: str) -> list[str]:
    return [x for x in (re.sub(r"^\s*[-*•]\s+", "", ln).strip() for ln in (guide or "").splitlines()) if x]


def operator_check_lines(r: RunReport, lang: str = "en") -> list[str]:
    """v2.1d Handover block "Operator checks (N major upgrades)" (`report.operator_checks`): one item per major
    upgrade / library replacement still in the final tree — coordinates, pom, case, the try, the commit, approved —
    with the 운영자 확인 guide and who wrote it. [] without checks."""
    checks = list(getattr(r, "operator_checks", None) or [])
    if not checks:
        return []
    L = [tr(lang, "### Operator checks ({n} major upgrades)", n=len(checks)), "",
         tr(lang, "The build passes, but these changes cross a major version line or replace a library: check the "
                  "runtime behaviour (configuration, registrations, renamed packages) before you ship."), ""]
    for c in checks:
        ch = c.change
        where = [f"`{_cell(ch.path, 160)}` ({ch.section})"]
        if c.case_id:
            where.append(tr(lang, "case `{case}`", case=c.case_id))
        if c.source:
            where.append(tr(lang, "structure review") if c.source == "review" else c.source)
        if c.commit:
            where.append(tr(lang, "commit `{sha}`", sha=c.commit[:7]))
        line = f"- **{_cell(ch.text, 200)}** — " + ", ".join(where)
        if ch.approved:                          # depchanges.apply_table: "approved by the shipped / company table"
            src = "jakarta_libraries.yml" if "shipped table" in (ch.note or "") else "libraries.yml"
            line += tr(lang, " — approved ({src})", src=src)
        L.append(line)
        items = _guide_items(c.guide)
        L += [f"  - {_cell(x, 500)}" for x in items[:OPERATOR_GUIDE_LINES]]
        if len(items) > OPERATOR_GUIDE_LINES:
            L.append("  - " + tr(lang, "… {n} more (report.json)", n=len(items) - OPERATOR_GUIDE_LINES))
        if not items:
            L.append("  - " + tr(lang, "(no guide recorded)"))
        L.append("  - " + (tr(lang, "guide source: LLM") if c.guide_source == "llm"
                           else tr(lang, "guide source: sbmig (synthesized)")))
    L.append("")
    return L


def handover_lines(r: RunReport, lang: str = "en") -> list[str]:
    """The first section of report.md: operator checks (v2.1d), versions replaced, libraries needing attention,
    'from here on'."""
    lang = normalize(lang)
    L = [f"## {tr(lang, HANDOVER_TITLE)}", ""]
    L += operator_check_lines(r, lang)
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
        L += [tr(lang, "| library | as-is | status | what we did | outcome | what you do next | docs |"),
              "|---|---|---|---|---|---|---|"]
        outcomes = {"replaced": tr(lang, "✅ replaced"), "still-present": tr(lang, "⚠ still present")}
        for f in findings:
            nxt = f.followup or f.summary
            if f.replacement:
                nxt = (nxt + " " if nxt else "") + f"→ `{f.replacement}`" + (
                    f" {f.replacement_version}" if f.replacement_version else "")
            docs = ", ".join(f"[{i}]({d})" for i, d in enumerate(f.docs[:3], 1)) or "-"
            outcome = outcomes.get(f.outcome, f.outcome)
            L.append(f"| **{_cell(f.library, 60)}** | {_cell(f.detected_version or '-', 40)} | {f.status} | "
                     f"{_cell(_what_we_did(f, lang), 160)} | {outcome} | {_cell(nxt, 400)} | {docs} |")
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
    if h is None and r.operator_checks:
        from .handover import operator_checks_item
        items = [operator_checks_item(len(r.operator_checks)), *items]
    if items:
        L += [f"{i}. {_cell(tr_line(lang, x), 500)}" for i, x in enumerate(items, 1)]
    else:
        L.append(tr(lang, "Nothing beyond the usual review — the migrated project builds with the versions above."))
    L.append("")
    return L


# ---- v2.1 error list (RunReport.error_entries): one line per build error / pom correction, grouped by where it was
# first seen, and one row per case with the LLM's cause / solution, the applied changes and the stored script
ERROR_LIST_TITLE = "Error list"
FIXES_TITLE = "Fixes by case"
ERROR_LIST_MAX_ROWS = 50                     # per `seen` group; errors.md in the run directory has every entry
FIXES_MAX_ROWS = 40
SEEN_TITLES = {
    "compile": "compile — `mvn compile` after OpenRewrite",
    "restructure": "restructure — pom corrections, verified by the next build",
    "review": "review — structure review",
    "build": "build — first `mvn clean package`, new errors only",
}
SEEN_FIX_TITLE = "{seen} — new errors of round {n}'s build"


def _short(text: object, limit: int) -> str:
    """Whitespace-collapsed and capped like `_cell`, without the pipe escaping (the caller `_cell`s the whole cell)."""
    s = " ".join(str(text if text is not None else "").split())
    return s[: limit - 1] + "…" if len(s) > limit else s


def _seen_title(seen: str, n: int, lang: str = "en") -> str:
    if seen in SEEN_TITLES:
        title = tr(lang, SEEN_TITLES[seen])
    else:
        m = re.match(r"^fix r(\d+)$", seen)
        title = tr(lang, SEEN_FIX_TITLE, seen=seen, n=m.group(1)) if m else seen
    return f"{title} ({n})"


def error_list_lines(r: RunReport, lang: str = "en") -> list[str]:
    """`## Error list`: counts, one table per `seen` group (compile → restructure → review → build → fix rN), the
    last build log. Without entries: the unresolved errors of the last build grouped by file (runs that stopped
    before the error list was kept), else one line."""
    entries = entries_of(r)
    L = [f"## {tr(lang, ERROR_LIST_TITLE)}", ""]
    if entries:
        L += [tr(lang, "{n} error(s) in this run, one line each (all of them with every try and diff: `errors.md` / "
                       "`errors.jsonl` in the run directory): {counts}.", n=len(entries),
                 counts=counts_text(status_counts(entries))), ""]
        for seen, rows in seen_groups(entries).items():
            L += [f"### {_seen_title(seen, len(rows), lang)}", "",
                  tr(lang, "| # | case | file:line | message | status | tries | resolved |"),
                  "|---|---|---|---|---|---|---|"]
            for i, e in rows[:ERROR_LIST_MAX_ROWS]:
                loc = where(e)
                L.append(f"| {i} | {('`' + e.case_id[:12] + '`') if e.case_id else '-'} | "
                         f"{('`' + _cell(loc, 140) + '`') if loc else '-'} | {_cell(entry_text(e), 200)} | "
                         f"{e.status} | {_cell(tries_text(e.tries) or '-', 300)} | {_cell(e.resolved or '-', 40)} |")
            if len(rows) > ERROR_LIST_MAX_ROWS:
                L += ["", tr(lang, "… {n} more in this group — see `errors.md`", n=len(rows) - ERROR_LIST_MAX_ROWS)]
            L.append("")
    elif r.unresolved:
        L += [tr(lang, "No error list was kept for this run; {n} error(s) were left after the last build, grouped by "
                       "file:", n=len(r.unresolved)), ""]
        for key, errs in group_by_file(r.unresolved, lang).items():
            L.append(f"### `{key}`")
            for e in errs:
                loc = f"L{e.line}: " if e.line else ""
                extra = tr(lang, " — symbol: `{symbol}`", symbol=e.symbol) if e.symbol else ""
                at = tr(lang, " — location: {location}", location=e.location) if e.location else ""
                L.append(f"- [{e.kind.value}] {loc}{_cell(e.message, 300)}{extra}{at}")
            L.append("")
    else:
        L += [tr(lang, "None. The final build passed.") if r.success
              else tr(lang, "None. No structured errors were captured; check the last build log."), ""]
    if r.final_build is not None and r.final_build.log_path:
        L += [tr(lang, "Last build log: `{path}`", path=r.final_build.log_path), ""]
    return L


def _case_script_cell(row: Any, lang: str = "en") -> str:
    if row.script:
        return f"v{row.script_version} `{_cell(row.script, 120)}`" if row.script_version else f"`{_cell(row.script, 120)}`"
    if row.no_script_reason:
        return tr(lang, "no script: {reason}", reason=_cell(row.no_script_reason, 200))
    return "-"


def fixes_by_case_lines(r: RunReport, lang: str = "en") -> list[str]:
    """`## Fixes by case`: one row per case that was worked on — cause / solution of the deciding try, the files the
    kept tries changed (+/−), what each change did (수정, v2.1b: `path lines — change` per note) and the stored
    script; [] when no case had a try."""
    rows = case_rows(entries_of(r))
    if not rows:
        return []
    L = [f"## {tr(lang, FIXES_TITLE)}", "",
         tr(lang, "| case | errors | cause | solution | changes (files, +/−) | edits | script |"),
         "|---|---|---|---|---|---|---|"]
    for row in rows[:FIXES_MAX_ROWS]:
        solution = row.solution or (tr(lang, "(no solution text) {reason}", reason=row.no_script_reason)
                                    if row.no_script_reason else "")
        major = (" " + tr(lang, "⚠ major ({n}, see Operator checks)", n=row.major_upgrades)) if row.major_upgrades \
            else ""
        L.append(f"| `{row.case_id}`{major} {_cell(_short(row.name, 100), 120)} | "
                 f"{tr(lang, '{resolved}/{n} resolved', resolved=row.resolved, n=row.entries)} | "
                 f"{_cell(row.analysis or '-', 400)} | {_cell(solution or '-', 400)} | "
                 f"{_cell(stats_text(row.changes) or '-', 300)} | {_cell(notes_text(row.edits) or '-', 700)} | "
                 f"{_case_script_cell(row, lang)} |")
    if len(rows) > FIXES_MAX_ROWS:
        L += ["", tr(lang, "… {n} more case(s) — see `errors.md`", n=len(rows) - FIXES_MAX_ROWS)]
    L += ["", "_" + tr(lang, "Stored scripts live in the KB (`<kb>/cases/<case>/fix_vN.py`); every try with its diff "
                             "is in `errors.md`, the whole change set in `changes.patch`.") + "_", ""]
    return L


# ---- v2 structure review (sbmig.review): LLM check of poms / dependencies / modules before the first build
def _review_fix_pending(rv: StructureReview) -> bool:
    """The review proposed a fix (edits or a script) that did not run."""
    a = getattr(rv, "answer", None)
    return bool(rv.script) or (a is not None and bool(a.edits or a.code))


def _review_script_state(rv: StructureReview, lang: str = "en") -> str:
    """What the review's fix (v2.1: direct edits and / or a script; `script_result` either way) did."""
    r = rv.script_result
    if r is not None:
        if rv.reverted:
            return tr(lang, "fix {status}, changes restored", status=r.status)
        if rv.commit:
            return tr(lang, "fix {status}, {n} file(s) changed (commit `{commit}`)", status=r.status,
                      n=len(r.changed_files), commit=rv.commit[:8])
        return tr(lang, "fix {status}, {n} file(s) changed", status=r.status, n=len(r.changed_files))
    if _review_fix_pending(rv):
        return tr(lang, "fix not applied")
    if rv.critical:
        return tr(lang, "no fix")
    return ""


def review_summary(rv: StructureReview, lang: str = "en") -> str:
    """One line for the header table / console: verdict, counts, what the fix (edits / script) did."""
    if rv.verdict == "skipped":
        return tr(lang, "skipped ({note})", note=_short(rv.note or "-", 160))
    if rv.verdict == "failed":
        return tr(lang, "failed — no usable answer, the build went ahead ({note})", note=_short(rv.note or "-", 160))
    if not rv.issues:
        return tr(lang, "ok — no issues")
    crit = len(rv.critical)
    txt = tr(lang, "{c} critical, {w} warning(s)", c=crit, w=len(rv.issues) - crit)
    state = _review_script_state(rv, lang)
    return f"{txt}; {state}" if state else txt


def _review_script_lines(rv: StructureReview, lang: str = "en") -> list[str]:
    r = rv.script_result
    L: list[str] = []
    if r is not None:
        files = ", ".join(f"`{f}`" for f in r.changed_files[:REVIEW_FILES_SHOWN])
        if len(r.changed_files) > REVIEW_FILES_SHOWN:
            files += f" (+{len(r.changed_files) - REVIEW_FILES_SHOWN})"
        L.append(tr(lang, "- fix (edits / script): `{status}`, {n} file(s) changed{files}", status=r.status,
                    n=len(r.changed_files), files=f": {files}" if files else ""))
        if rv.commit:
            L.append(tr(lang, "- commit: `{commit}` (revert it to undo the review's changes)", commit=rv.commit[:12]))
        if rv.reverted:
            L.append(tr(lang, "- the fix's changes were restored: they are not in the result"))
        if r.error:
            L.append(tr(lang, "- fix error: {error}", error=_cell(r.error, 300)))
        if r.log:
            L.append(tr(lang, "- fix log: {lines}", lines=_cell("; ".join(r.log[:6]), 400)))
    elif _review_fix_pending(rv):
        L.append(tr(lang, "- fix (edits / script): not applied"))
    elif rv.critical:
        L.append(tr(lang, "- no fix for the critical issue(s): they are handed over"))
    if rv.note:
        L.append(tr(lang, "- note: {note}", note=_cell(rv.note, 400)))
    return L


def _review_answer_lines(r: RunReport, rv: StructureReview, lang: str = "en") -> list[str]:
    """v2.1: the review's answer (solution, direct edits, script for the KB) and what its kept tries changed."""
    L: list[str] = []
    a = getattr(rv, "answer", None)
    if a is not None:
        if a.solution:
            L.append(tr(lang, "- solution: {solution}", solution=_cell(a.solution, 600)))
        if a.edits:
            L.append(tr(lang, "- direct edits in the answer: {n}", n=len(a.edits)))
        if a.code:
            L.append(tr(lang, "- script for the KB: yes"))
        elif a.no_script_reason:
            L.append(tr(lang, "- script for the KB: none — {reason}", reason=_cell(a.no_script_reason, 300)))
    changes = review_changes(entries_of(r))
    if changes:
        L.append(tr(lang, "- changed: {files}", files=_cell(stats_text(changes, limit=REVIEW_FILES_SHOWN), 600)))
    return L


def structure_review_lines(r: RunReport, lang: str = "en") -> list[str]:
    """`## Structure review`: verdict, issues table, what the review changed (edits / script) or why not; []
    without a review."""
    rv = r.structure_review
    if rv is None:
        return []
    L = [f"## {tr(lang, REVIEW_TITLE)}", "",
         tr(lang, "After the restructure and before the first build, the LLM checked the poms, dependencies and "
                  "module layout against the company rules. It fixes critical issues itself (committed, revertible) "
                  "and leaves a script for the KB; warnings are only recorded."), "",
         tr(lang, "- verdict: `{verdict}` — {summary}", verdict=rv.verdict, summary=review_summary(rv, lang))]
    if rv.model or rv.llm_usage.calls:
        L.append(tr(lang, "- model: `{model}`, {calls} LLM call(s)", model=rv.model or "-", calls=rv.llm_usage.calls))
    if rv.issues:
        issues = sorted(rv.issues, key=lambda i: i.severity != "critical")
        L += ["", tr(lang, "| severity | file | problem | fix hint |"), "|---|---|---|---|"]
        for i in issues[:REVIEW_MAX_ROWS]:
            sev = f"**{tr(lang, 'critical')}**" if i.severity == "critical" else tr(lang, "warning")
            where = f"`{_cell(i.file, 120)}`" if i.file else tr(lang, "(project)")
            L.append(f"| {sev} | {where} | {_cell(i.problem, 400)} | {_cell(i.fix_hint or '-', 300)} |")
        if len(issues) > REVIEW_MAX_ROWS:
            L += ["", tr(lang, "… {n} more issue(s) — see `structure_review` in report.json",
                         n=len(issues) - REVIEW_MAX_ROWS)]
        L.append("")
    L += _review_script_lines(rv, lang)
    L += _review_answer_lines(r, rv, lang)
    analysis = rv.analysis or (rv.answer.analysis if getattr(rv, "answer", None) is not None else "")
    if analysis:
        L += ["", tr(lang, "Analysis: {analysis}", analysis=_cell(analysis, 800))]
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


def _to_be(r: RunReport) -> Optional[str]:
    h = r.handover
    if h is None or not (h.java_after or h.spring_boot_after or h.parent_after):
        return None
    parts = []
    if h.java_after:
        parts.append(f"java {h.java_after}")
    if h.spring_boot_after:
        parts.append(f"spring boot {h.spring_boot_after}")
    if h.parent_after:
        parts.append(f"parent `{_cell(h.parent_after, 160)}`")
    return ", ".join(parts)


def render_markdown(report: RunReport, language: str = "en", cfg: Any = None) -> str:
    """report.md for `language` (`report.language`: "en" | "ko"; anything else renders English). `cfg` (optional)
    only sharpens the javax API section's in-house detection.

    v2.1 (slim): header → Handover → Error list → Fixes by case → Structure review → Blocked → javax API jars →
    Manual follow-ups → Flags → What we changed (plan steps, OpenRewrite runs) → LLM usage and cost → Notes →
    Outputs."""
    lang = normalize(language)
    r = report
    L: list[str] = []
    ok = tr(lang, "✅ BUILD SUCCESS") if r.success else tr(lang, "❌ NOT BUILDING")
    L.append(tr(lang, "# Migration report — {name}", name=r.project_name))
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(tr(lang, "| **Result** | {ok} (final `mvn clean package`) |", ok=ok))
    L.append(f"| SBMIG-RESULT | `{report_result_line(r)}` |")
    entries = entries_of(r)
    if entries:
        L.append(tr(lang, "| **Error list** | {n} error(s): {counts} (see \"Error list\") |", n=len(entries),
                    counts=counts_text(status_counts(entries))))
    L.append(tr(lang, "| Case | `{case}` |", case=r.case_type.value))
    if r.profile is not None:
        p = r.profile
        L.append(tr(lang, "| As-is | java {java}, spring boot {boot}, spring {spring}, {poms} pom(s) |",
                    java=p.java_version or '?', boot=p.spring_boot_version or '-', spring=p.spring_version or '-',
                    poms=len(p.poms)))
    to_be = _to_be(r)
    if to_be:
        L.append(tr(lang, "| To-be | {versions} |", versions=to_be))
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
    if r.structure_review is not None:
        L.append(tr(lang, "| Structure review | {summary} |", summary=_cell(review_summary(r.structure_review, lang),
                                                                          300)))
    L.append("")

    # ---- handover first: what we replaced, what the owning team does from here on
    L += handover_lines(r, lang)

    # ---- the centre: every error of the run, then how each case was fixed
    L += error_list_lines(r, lang)
    L += fixes_by_case_lines(r, lang)

    # ---- the structure review (ran before the first build; its own entries are in the error list too)
    L += structure_review_lines(r, lang)

    # ---- blocked: deliberately skipped, each row says what a human / the library owner must do
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

    if r.manual_followups:
        L.append(tr(lang, "## Manual follow-ups"))
        L.append("")
        L += [f"- {f}" for f in r.manual_followups]
        L.append("")

    if r.flags:
        L.append(tr(lang, "## Flags"))
        L.append("")
        for f in r.flags:
            h = flag_help(f, lang)
            L.append(f"- `{f}`" + (f": {h}" if h else ""))
        L.append("")

    # ---- what we changed (the fixes themselves are in "Fixes by case", the diff in changes.patch)
    titles = {s.id: s.title for s in (r.plan.steps if r.plan else [])}
    executed = [sid for pr in r.phases for sid in pr.step_ids]
    rw = [(pr.phase.value, x) for pr in r.phases for x in pr.rewrite_results]
    if executed or rw:
        L.append(tr(lang, "## What we changed"))
        L.append("")
    if executed:
        L.append(tr(lang, "Plan steps executed:"))
        L += [f"1. `{sid}` — {titles.get(sid, '')}" for sid in executed]
        L.append("")
    if rw:
        L.append(tr(lang, "OpenRewrite runs:"))
        L.append("")
        L.append(tr(lang, "| phase | recipes | changed files | ok |"))
        L.append("|---|---|---|---|")
        for ph, x in rw:
            L.append(f"| {ph} | {_cell(', '.join(x.recipes), 200)} | {len(x.changed_files)} | "
                     f"{tr(lang, 'yes') if x.ok else tr(lang, 'no: {error}', error=_cell(x.error, 80))} |")
        L.append("")

    # ---- LLM usage and cost (2026-10-22: the company wants the API cost per project)
    L += cost_lines(r, lang)

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
