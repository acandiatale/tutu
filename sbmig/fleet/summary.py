"""Fleet summaries, rewritten after every project: ``fleet/summary.md``, ``summary.csv``,
``summary.json`` (= state) and ``fleet/learning.md`` (ledger summary of this fleet's runs + KB stats)."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import FleetState

log = logging.getLogger("sbmig.fleet")

COLUMNS = [
    "#",
    "name",
    "case",
    "status",
    "exit",
    "duration_s",
    "llm_calls",
    "blocked",
    "unresolved",
    "kb_used",
    "kb_created",
    "kb_promoted",
    "attempt",
    "run_id",
    "error",
]


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
                "blocked": p.blocked,
                "unresolved": p.unresolved,
                "kb_used": len(p.kb_recipes_used),
                "kb_created": len(p.kb_cases_created) + len(p.kb_candidates_created),
                "kb_promoted": len(p.kb_recipes_promoted),
                "attempt": p.attempt,
                "run_id": p.run_id or "",
                "error": (p.error or "")[:200],
            }
        )
    return out


def totals(state: FleetState) -> dict[str, Any]:
    ps = state.projects
    by: dict[str, int] = {}
    for p in ps:
        by[p.status] = by.get(p.status, 0) + 1
    return {
        "projects": len(ps),
        "by_status": by,
        "llm_calls": sum(p.llm_calls for p in ps),
        "duration_s": round(sum(p.duration_s for p in ps), 1),
        "blocked": sum(p.blocked for p in ps),
        "unresolved": sum(p.unresolved for p in ps),
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
    cols = COLUMNS[:12]
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
        f"* LLM calls: {t['llm_calls']}, blocked: {t['blocked']}, unresolved: {t['unresolved']}, duration: "
        f"{t['duration_s']:.0f}s",
        f"* KB: used {t['kb_used']}, cases created {t['kb_cases_created']}, candidates created "
        f"{t['kb_candidates_created']}, recipes promoted {t['kb_recipes_promoted']}",
    ]
    if t["running"]:
        head.append(f"* still running: {t['running']} ({t['done']} of {t['projects']} done)")
    elif not state.finished_at:
        head.append(f"* progress: {t['done']} of {t['projects']} done")
    head += ["", render_table(state), ""]
    errs = [p for p in state.projects if p.error]
    if errs:
        head += ["## Errors", ""] + [f"* **{p.name}** ({p.status}): {p.error}" for p in errs] + [""]
    blocked = [p for p in state.projects if p.blocked_reasons]
    if blocked:
        head += (
            ["## Blocked", ""] + [f"* **{p.name}**: {', '.join(p.blocked_reasons)}" for p in blocked] + [""]
        )
    if state.notes:
        head += ["## Notes", ""] + [f"* {n}" for n in state.notes] + [""]
    head.append("See `learning.md` for what the KB learned across these projects.")
    return "\n".join(head) + "\n"


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
    from sbmig.kb.store import atomic_write

    if state is None:
        return {}
    formats = list(cfg.fleet.summary_formats) if cfg is not None else ["md", "csv", "json"]
    out: dict[str, Path] = {}
    fleet_dir = Path(fleet_dir)
    if "md" in formats:
        out["md"] = fleet_dir / "summary.md"
        atomic_write(out["md"], render_summary_md(state))
    if "csv" in formats:
        out["csv"] = fleet_dir / "summary.csv"
        atomic_write(out["csv"], render_csv(state))
    if "json" in formats:
        out["json"] = fleet_dir / "summary.json"
        atomic_write(out["json"], state.model_dump_json(indent=2) + "\n")
    out["learning"] = fleet_dir / "learning.md"
    atomic_write(out["learning"], render_learning(state, kb))
    return out
