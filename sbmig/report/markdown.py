"""report.md rendering — written for the as-is owners who pick up whatever the agent could not fix."""
from __future__ import annotations

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Iterable, Optional

from sbmig.core.models import BuildError, FixAttempt, PhaseResult, RunReport

FLAG_HELP = {
    "test-compile-skipped": "main code builds, but test sources only compile with `-Dmaven.test.skip=true` "
                            "(see follow-ups) — fix the tests before relying on CI",
    "time-budget-exhausted": "the run hit `loop.max_total_minutes`; remaining loops were skipped, verify still ran",
    "llm-budget-exhausted": "the run used all `loop.max_llm_calls`; remaining clusters were escalated",
    "dry-run": "dry run: decisions were computed but nothing was applied",
    "aborted": "the run stopped early (see follow-ups for the reason)",
    "mvn-not-found": "Maven was not runnable on this machine",
}


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
    return f"`{a.model}`" if a.model else "-"


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
    L.append("")

    # ---- unresolved first: it is what the reader acts on
    L.append("## Unresolved errors")
    L.append("")
    if not r.unresolved:
        L.append("None. " + ("The final build passed." if r.success else "No structured errors were captured; "
                                                                          "check the last build log."))
    else:
        L.append(f"{len(r.unresolved)} error(s) left after the last build, grouped by file:")
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
            L.append(f"- `{f}`" + (f": {FLAG_HELP[f]}" if f in FLAG_HELP else ""))
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
