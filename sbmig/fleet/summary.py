"""Fleet summaries, rewritten after every project: ``fleet/summary.md``, ``summary.csv``,
``summary.json`` (= state) and ``fleet/learning.md`` (the error cases of this fleet's runs, the most widespread cases
of the catalog and the fix scripts stored in the KB).

Round 10: ``summary.csv`` is UTF-8 with BOM (Excel opens Korean paths correctly) and carries the ``attention`` codes
(:mod:`sbmig.report.result_line`: why a human has to look, ``;``-separated, most important first) and the ``input``
path; ``summary.md`` gets a "Needs attention" section grouped by the first code. Each file is written on its own:
a ``summary.csv`` locked by Excel is skipped with a warning and the others are still written."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import FleetProjectResult, FleetState
from sbmig.report.result_line import result_attention

log = logging.getLogger("sbmig.fleet")

COLUMNS = [
    "#",
    "name",
    "case",
    "status",
    "exit",
    "duration_s",
    "llm_calls",
    "llm_cost",
    "blocked",
    "unresolved",
    "advisories",
    "carried_over",
    "attempt",
    "run_id",
    "error",
    "attention",
    "input",
]
TABLE_COLUMNS = COLUMNS[:12] + ["attention"]     # summary.md / `fleet status` (error and input are listed apart)
ATTENTION_GROUP_LIMIT = 50                       # `fleet status --attention`: projects printed per code


def rows(state: FleetState) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(state.projects, 1):
        out.append(
            {
                "#": i,
                "name": p.name,
                "case": p.case_type.value,
                "status": p.status,
                "exit": "" if p.exit_code is None else p.exit_code,
                "duration_s": round(p.duration_s, 1),
                "llm_calls": p.llm_calls,
                "llm_cost": "" if p.llm_cost is None else round(p.llm_cost, 4),
                "blocked": p.blocked,
                "unresolved": p.unresolved,
                "advisories": len(p.advisories),
                "carried_over": p.carried_over,
                "attempt": p.attempt,
                "run_id": p.run_id or "",
                "error": (p.error or "")[:200],
                "attention": ";".join(result_attention(p)),
                "input": p.input,
            }
        )
    return out


def attention_groups(state: FleetState) -> dict[str, list[FleetProjectResult]]:
    """First attention code → projects (in state order); the code order is that of the first occurrence after
    sorting by importance (infra first, tests-not-compiled last)."""
    from sbmig.report.result_line import ATTENTION_FLAGS

    def rank(code: str) -> int:
        for i, prefix in enumerate(("infra:", "skipped", "crash", "aborted", "blocked:", "unresolved:")):
            if code.startswith(prefix):
                return i
        return 6 + (ATTENTION_FLAGS.index(code) if code in ATTENTION_FLAGS else len(ATTENTION_FLAGS))

    groups: dict[str, list[FleetProjectResult]] = {}
    for p in state.projects:
        codes = result_attention(p)
        if codes:
            key = codes[0] if not codes[0].startswith("unresolved:") else "unresolved"
            groups.setdefault(key, []).append(p)
    return dict(sorted(groups.items(), key=lambda kv: (rank(kv[0]), kv[0])))


def render_attention(state: FleetState, limit: int = ATTENTION_GROUP_LIMIT) -> list[str]:
    """`fleet status --attention`: only the projects that need a human, grouped by their first attention code."""
    groups = attention_groups(state)
    n = sum(len(v) for v in groups.values())
    lines = [f"fleet {state.fleet_id}: {n} of {len(state.projects)} project(s) need attention"]
    for code, ps in groups.items():
        lines.append(f"{code} ({len(ps)})")
        for p in ps[:limit]:
            lines.append(f"  {p.name}  {p.status}  {p.run_id or '-'}  {';'.join(result_attention(p))}  {p.input}")
        if len(ps) > limit:
            lines.append(f"  ... {len(ps) - limit} more (summary.csv, column attention)")
    return lines


def totals(state: FleetState) -> dict[str, Any]:
    ps = state.projects
    by: dict[str, int] = {}
    for p in ps:
        by[p.status] = by.get(p.status, 0) + 1
    return {
        "projects": len(ps),
        "by_status": by,
        "llm_calls": sum(p.llm_calls for p in ps),
        "llm_cost": round(sum(p.llm_cost or 0.0 for p in ps), 4),
        "duration_s": round(sum(p.duration_s for p in ps), 1),
        "blocked": sum(p.blocked for p in ps),
        "unresolved": sum(p.unresolved for p in ps),
        "advisories": sum(len(p.advisories) for p in ps),
        "carried_over": sum(p.carried_over for p in ps),
        "done": sum(1 for p in ps if p.status in ("success", "incomplete", "aborted", "skipped")),
        "running": next((p.name for p in ps if p.status == "running"), None),
    }


def _cell(v: Any) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def render_table(state: FleetState) -> str:
    rs = rows(state)
    cols = TABLE_COLUMNS
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rs:
        lines.append("| " + " | ".join(_cell(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def render_summary_md(state: FleetState) -> str:
    t = totals(state)
    status = ", ".join(f"{k} {v}" for k, v in sorted(t["by_status"].items()))
    head = [
        f"# Fleet {state.fleet_id}",
        "",
        f"* manifest: `{Path(state.manifest).name}`",
        f"* started: {state.started_at.isoformat(timespec='seconds')}, updated: "
        f"{state.updated_at.isoformat(timespec='seconds')}"
        + (f", finished: {state.finished_at.isoformat(timespec='seconds')}" if state.finished_at else ""),
        f"* projects: {t['projects']} ({status or '-'})",
        f"* LLM calls: {t['llm_calls']} (cost {t['llm_cost']:.4f}, see llm.pricing), blocked: {t['blocked']}, "
        f"unresolved: {t['unresolved']}, duration: {t['duration_s']:.0f}s",
        f"* library advisories: {t['advisories']}, dependencies carried over: {t['carried_over']}",
    ]
    if t["running"]:
        head.append(f"* still running: {t['running']} ({t['done']} of {t['projects']} done)")
    elif not state.finished_at:
        head.append(f"* progress: {t['done']} of {t['projects']} done")
    head += ["", render_table(state), ""]
    groups = attention_groups(state)
    if groups:
        head += ["## Needs attention", "",
                 "Grouped by the first attention code (`summary.csv` column `attention` has all of them).", ""]
        for code, ps in groups.items():
            names = ", ".join(p.name for p in ps[:20]) + (f" … (+{len(ps) - 20})" if len(ps) > 20 else "")
            head.append(f"* `{code}` ({len(ps)}): {_cell(names)}")
        head.append("")
    errs = [p for p in state.projects if p.error]
    if errs:
        head += ["## Errors", ""] + [f"* **{p.name}** ({p.status}): {p.error}" for p in errs] + [""]
    blocked = [p for p in state.projects if p.blocked_reasons]
    if blocked:
        head += (
            ["## Blocked", ""] + [f"* **{p.name}**: {', '.join(p.blocked_reasons)}" for p in blocked] + [""]
        )
    head += advisory_section(state)
    if state.notes:
        head += ["## Notes", ""] + [f"* {n}" for n in state.notes] + [""]
    head.append("See `learning.md` for the error cases across these projects and the fix scripts in the KB.")
    return "\n".join(head) + "\n"


def advisory_rows(state: FleetState) -> list[dict[str, Any]]:
    """advisory id → projects / replaced / still-present (from FleetProjectResult.advisories "id:outcome")."""
    agg: dict[str, dict[str, Any]] = {}
    for p in state.projects:
        for entry in p.advisories:
            aid, _, outcome = entry.rpartition(":") if ":" in entry else (entry, "", "unknown")
            d = agg.setdefault(aid, {"id": aid, "projects": [], "replaced": 0, "still_present": 0, "unknown": 0})
            if p.name not in d["projects"]:
                d["projects"].append(p.name)
            key = {"replaced": "replaced", "still-present": "still_present"}.get(outcome, "unknown")
            d[key] += 1
    return sorted(agg.values(), key=lambda d: (-len(d["projects"]), d["id"]))


def advisory_section(state: FleetState) -> list[str]:
    rs = advisory_rows(state)
    if not rs:
        return []
    out = [
        "## Libraries flagged across the fleet",
        "",
        "Library advisories (EOL / no longer managed); never block a run — each project's report.md has a "
        "Handover section telling the owners what to do next.",
        "",
        "| advisory | projects | replaced | still present | unknown | projects (first 8) |",
        "|---|---|---|---|---|---|",
    ]
    for d in rs:
        names = ", ".join(d["projects"][:8]) + (" …" if len(d["projects"]) > 8 else "")
        out.append(f"| {_cell(d['id'])} | {len(d['projects'])} | {d['replaced']} | {d['still_present']} | "
                   f"{d['unknown']} | {_cell(names)} |")
    carried = [p for p in state.projects if p.carried_over]
    if carried:
        out += ["", "Projects with dependencies pinned at as-is versions (no longer managed): "
                + ", ".join(f"{p.name} ({p.carried_over})" for p in carried)]
    return out + [""]


def render_csv(state: FleetState) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows(state):
        w.writerow(r)
    return buf.getvalue()


_EC_METHODS = ("script", "script_reuse", "script_augment", "llm", "plan_step", "none")
_EC_STATUSES = ("resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible")
TOP_CASES = 10


def _md_cell(v: Any) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def error_cases_md(ec: dict[str, Any]) -> list[str]:
    """The "Error cases across projects" section (``ErrorCaseStore.summary``): status × method, top failure
    reasons, the L2 signatures still unresolved / blocked most often."""
    lines = ["## Error cases across projects", ""]
    if not ec.get("errors"):
        return lines + ["_No error cases recorded for these projects yet._", ""]
    lines.append(f"{ec.get('errors', 0)} build error(s) recorded one by one from {ec.get('projects', 0)} project(s) / "
                 f"{ec.get('runs', 0)} run(s), {ec.get('attempts', 0)} fix attempt(s).")
    if ec.get("by_status"):
        lines.append("Status: " + ", ".join(f"{k} {v}" for k, v in ec["by_status"].items()) + ".")
    if ec.get("by_method"):
        lines.append("Resolved by: " + ", ".join(f"{k} {v}" for k, v in ec["by_method"].items()) + ".")
    matrix = ec.get("status_method") or {}
    if matrix:
        methods = [m for m in _EC_METHODS if any(m in row for row in matrix.values())]
        methods += sorted({m for row in matrix.values() for m in row} - set(methods))
        statuses = [st for st in _EC_STATUSES if st in matrix] + sorted(set(matrix) - set(_EC_STATUSES))
        lines += ["", "Status × method (resolved: the method that fixed it; otherwise the method of the last attempt, "
                      "none = never attempted):", "",
                  "| status | " + " | ".join(methods) + " |", "|---|" + "---|" * len(methods)]
        lines += [f"| {st} | " + " | ".join(str(matrix[st].get(m, 0)) for m in methods) + " |" for st in statuses]
    lines += ["", "### Top failure reasons", ""]
    if ec.get("top_failure_reasons"):
        lines += ["| L2 signature | errors | projects | why not resolved |", "|---|---|---|---|"]
        lines += [f"| {_md_cell(r['signature_generic'])} | {r['count']} | {r['projects']} | {_md_cell(r['reason'])} |"
                  for r in ec["top_failure_reasons"]]
    else:
        lines.append("_none_")
    lines += ["", "### Still unresolved / blocked most often", ""]
    if ec.get("top_unresolved"):
        lines += ["| L2 signature | symbol | errors | blocked | projects |", "|---|---|---|---|---|"]
        lines += [f"| {_md_cell(r['signature_generic'])} | {_md_cell(r.get('symbol'))} | {r['count']} | "
                  f"{r.get('blocked', 0)} | {r['projects']} |" for r in ec["top_unresolved"]]
    else:
        lines.append("_none_")
    return lines + [""]


def _top_cases(kb: Any, top: int = TOP_CASES) -> list[dict[str, Any]]:
    """The most widespread cases of the KB's case catalog (whole KB), [] without one."""
    try:
        from sbmig.kb.catalog import action_text

        cat = kb.catalog()
        if not cat.index_path.exists():
            return []
        return [{"id": c.id, "name": c.effective_name(), "status": c.effective_status(),
                 "action": action_text(c.effective_action()), "projects": c.evidence.projects,
                 "seen": c.evidence.seen} for c in cat.cases()[:top]]
    except Exception as e:  # pragma: no cover - the learning report must not fail on it
        log.warning("case catalog summary failed: %s: %s", type(e).__name__, e)
        return []


def _script_rows(kb: Any, top: int = TOP_CASES) -> list[dict[str, Any]]:
    """Cases with stored fix scripts, most success projects first (the version a reuse runs first)."""
    try:
        store = kb.scripts
        rows = []
        for cid in store.case_ids():
            metas = store.versions(cid)
            if not metas:
                continue
            ranked = store.ranked(cid)
            rows.append({"case_id": cid, "versions": len(metas), "best": ranked[0].version if ranked else 0,
                         "successes": sum(m.successes for m in metas), "failures": sum(m.failures for m in metas),
                         "projects": len({p for m in metas for p in m.success_projects}),
                         "signature_generic": next((m.signature_generic for m in reversed(metas)
                                                    if m.signature_generic), "")})
        rows.sort(key=lambda r: (-r["projects"], -r["successes"], r["case_id"]))
        return rows[:top]
    except Exception as e:  # pragma: no cover - the learning report must not fail on it
        log.warning("fix script summary failed: %s: %s", type(e).__name__, e)
        return []


def render_learning(state: FleetState, kb: Any = None) -> str:
    """``learning.md``: this fleet's projects / LLM calls, the error cases of its runs, the most widespread cases of
    the catalog and the fix scripts of the KB (both whole KB)."""
    run_ids = [p.run_id for p in state.projects if p.run_id]
    ps = state.projects
    lines = [f"# Fleet {state.fleet_id} — error cases and fix scripts", "", "## This fleet", "",
             f"* projects: {len(ps)} ({sum(1 for p in ps if p.run_id)} run)",
             f"* LLM calls: {sum(p.llm_calls for p in ps)}", ""]
    ec: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    if kb is not None:
        try:
            ec = kb.error_case_store().summary(run_ids=run_ids) if run_ids else {}
        except Exception as e:  # pragma: no cover - the learning report must not fail on it
            log.warning("error-case summary failed: %s: %s", type(e).__name__, e)
        try:
            stats = kb.stats()
        except Exception as e:  # pragma: no cover
            log.warning("kb stats failed: %s", e)
    lines += error_cases_md(ec)
    cases = _top_cases(kb) if kb is not None else []
    if cases:
        lines += ["### Top error cases (case catalog, whole KB — `sbmig kb cases show <id>`)", "",
                  "| case | name | status | action | projects | seen |", "|---|---|---|---|---|---|"]
        lines += [f"| `{r['id']}` | {_md_cell(r['name'])} | {r['status']} | {_md_cell(r['action'])} | "
                  f"{r['projects']} | {r['seen']} |" for r in cases]
        lines.append("")
    lines += ["## Fix scripts (whole KB — `sbmig kb scripts list`)", ""]
    if stats:
        lines.append(f"* cases with scripts: {stats.get('script_cases', 0)}, versions: {stats.get('script_versions', 0)} "
                     f"(proven {stats.get('scripts_proven', 0)}, retired {stats.get('scripts_retired', 0)}, "
                     f"quarantined {stats.get('scripts_quarantined', 0)})")
        lines.append(f"* catalog cases: {stats.get('catalog_cases', 0)} (active {stats.get('catalog_active', 0)}, "
                     f"candidate {stats.get('catalog_candidate', 0)}, observed {stats.get('catalog_observed', 0)}, "
                     f"rejected {stats.get('catalog_rejected', 0)})")
    rows = _script_rows(kb) if kb is not None else []
    if rows:
        lines += ["", "| case | versions | reused first | ok | failed | projects | L2 signature |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| `{r['case_id']}` | {r['versions']} | {'v' + str(r['best']) if r['best'] else '-'} | "
                  f"{r['successes']} | {r['failures']} | {r['projects']} | {_md_cell(r['signature_generic'])} |"
                  for r in rows]
    elif not stats:
        lines.append("_no KB_")
    return "\n".join(lines) + "\n"


def write_summaries(
    state: Optional[FleetState], fleet_dir: Path, cfg: Optional[Config] = None, kb: Any = None
) -> dict[str, Path]:
    """Write every summary file on its own; returns the ones written (a file Excel holds open is skipped)."""
    from sbmig.kb.store import atomic_write

    if state is None:
        return {}
    formats = list(cfg.fleet.summary_formats) if cfg is not None else ["md", "csv", "json"]
    out: dict[str, Path] = {}
    fleet_dir = Path(fleet_dir)
    jobs: list[tuple[str, str, Any]] = []
    if "md" in formats:
        jobs.append(("md", "summary.md", lambda: render_summary_md(state)))
    if "csv" in formats:
        jobs.append(("csv", "summary.csv", lambda: "\ufeff" + render_csv(state)))     # UTF-8 BOM for Excel
    if "json" in formats:
        jobs.append(("json", "summary.json", lambda: state.model_dump_json(indent=2) + "\n"))
    jobs.append(("learning", "learning.md", lambda: render_learning(state, kb)))
    for key, name, render in jobs:
        path = fleet_dir / name
        try:
            atomic_write(path, render())
        except PermissionError as e:
            log.warning("fleet: %s not written (open in Excel or another program?): %s", path, e)
            continue
        except Exception as e:  # one summary must never cost the others
            log.warning("fleet: %s not written: %s: %s", path, type(e).__name__, e)
            continue
        out[key] = path
    return out
