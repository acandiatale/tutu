"""Fleet summaries, rewritten after every project: ``fleet/summary.md``, ``summary.csv``,
``summary.json`` (= state) and ``fleet/learning.md`` (ledger summary of this fleet's runs + KB stats).

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
    "kb_used",
    "kb_created",
    "kb_promoted",
    "attempt",
    "run_id",
    "error",
    "attention",
    "input",
]
TABLE_COLUMNS = COLUMNS[:15] + ["attention"]     # summary.md / `fleet status` (error and input are listed apart)
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
                "kb_used": len(p.kb_recipes_used),
                "kb_created": len(p.kb_cases_created) + len(p.kb_candidates_created),
                "kb_promoted": len(p.kb_recipes_promoted),
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
        "kb_used": sum(len(p.kb_recipes_used) for p in ps),
        "kb_cases_created": sum(len(p.kb_cases_created) for p in ps),
        "kb_candidates_created": sum(len(p.kb_candidates_created) for p in ps),
        "kb_recipes_promoted": sum(len(p.kb_recipes_promoted) for p in ps),
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
        f"* KB: used {t['kb_used']}, cases created {t['kb_cases_created']}, candidates created "
        f"{t['kb_candidates_created']}, recipes promoted {t['kb_recipes_promoted']}",
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
    head.append("See `learning.md` for what the KB learned across these projects.")
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


def render_learning(state: FleetState, kb: Any = None) -> str:
    from sbmig.kb.ledger import Ledger, render_learning_md

    run_ids = [p.run_id for p in state.projects if p.run_id]
    ledger = (
        kb.ledger() if kb is not None and hasattr(kb, "ledger") else Ledger(Path("/nonexistent/ledger.jsonl"))
    )
    summary = ledger.summary(run_ids=run_ids)
    stats = None
    if kb is not None and hasattr(kb, "stats"):
        try:
            stats = kb.stats()
        except Exception as e:  # pragma: no cover
            log.warning("kb stats failed: %s", e)
    ps = state.projects
    extra = {
        "cases created": sum(len(p.kb_cases_created) for p in ps),
        "recipe candidates created": sorted({c for p in ps for c in p.kb_candidates_created}) or 0,
        "recipes auto-promoted to active": sorted({c for p in ps for c in p.kb_recipes_promoted}) or 0,
        "projects where the KB fixed something": sum(1 for p in ps if p.kb_recipes_used),
        "LLM calls": sum(p.llm_calls for p in ps),
    }
    return render_learning_md(
        summary, stats, title=f"Fleet {state.fleet_id} — what the KB learned", extra=extra
    )


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
