"""Fleet summaries, rewritten after every project: ``fleet/summary.md``, ``summary.csv``,
``summary.json`` (= state) and ``fleet/learning.md`` (the KB cases these projects met, with their stored scripts).

Round 10: ``summary.csv`` is UTF-8 with BOM (Excel opens Korean paths correctly) and carries the ``attention`` codes
(:mod:`sbmig.report.result_line`: why a human has to look, ``;``-separated, most important first) and the ``input``
path; ``summary.md`` gets a "Needs attention" section grouped by the first code. Each file is written on its own:
a ``summary.csv`` locked by Excel is skipped with a warning and the others are still written.

v2.1: per project the error-list counts ``open`` / ``resolved`` / ``blocked`` (``FleetProjectResult.error_counts``,
the statuses of ``RunReport.error_entries``; a state written before v2.1 falls back to ``unresolved`` / ``blocked``);
``learning.md`` lists the cases of ``<kb>/cases/*/case.json`` (CaseInfo) with their script versions."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import CaseInfo, FleetProjectResult, FleetState
from sbmig.report.result_line import counts_open_resolved, result_attention

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
    "open",
    "resolved",
    "blocked",
    "advisories",
    "carried_over",
    "attempt",
    "run_id",
    "error",
    "attention",
    "input",
]
TABLE_COLUMNS = COLUMNS[:13] + ["attention"]     # summary.md / `fleet status` (error and input are listed apart)
ATTENTION_GROUP_LIMIT = 50                       # `fleet status --attention`: projects printed per code


def error_columns(p: FleetProjectResult) -> tuple[int, int, int]:
    """(open, resolved, blocked) of a project's error list; without counts (a pre-v2.1 state, a run that never
    reported) the old `unresolved` / `blocked` fields."""
    counts = dict(getattr(p, "error_counts", None) or {})
    if not counts:
        return p.unresolved, 0, p.blocked
    o, r = counts_open_resolved(counts)
    return o, r, int(counts.get("blocked", 0) or 0)


def rows(state: FleetState) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(state.projects, 1):
        o, r, b = error_columns(p)
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
                "open": o,
                "resolved": r,
                "blocked": b,
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
        "errors_open": sum(error_columns(p)[0] for p in ps),
        "errors_resolved": sum(error_columns(p)[1] for p in ps),
        "errors_blocked": sum(error_columns(p)[2] for p in ps),
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
        f"* LLM calls: {t['llm_calls']} (cost {t['llm_cost']:.4f}, see llm.pricing), duration: {t['duration_s']:.0f}s",
        f"* error list: open {t['errors_open']}, resolved {t['errors_resolved']}, blocked {t['errors_blocked']} "
        f"(blocked items: {t['blocked']})",
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
    head.append("See `learning.md` for the KB cases these projects met and their stored fix scripts; each project's "
                "error list is `errors.md` in its run directory.")
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


TOP_CASES = 40


def _md_cell(v: Any, limit: int = 160) -> str:
    s = " ".join(str(v if v not in (None, "") else "-").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _scripts_cell(c: CaseInfo) -> str:
    """``v1 proven 3/0, v2 unverified 0/1`` (successes / failures)."""
    return ", ".join(f"v{s.version} {s.status} {s.successes}/{s.failures}" for s in c.scripts) or "-"


def read_cases(kb: Any = None, kb_dir: Optional[Path] = None) -> list[tuple[CaseInfo, dict[str, Any]]]:
    """(CaseInfo, extra keys such as "pin") of every case, most widespread first: through the KB's case store
    (`kb.cases.cases()`), else straight from `<kb_dir>/cases/*/case.json`; damaged files are skipped."""
    store = getattr(kb, "cases", None) if kb is not None else None
    if store is not None and callable(getattr(store, "cases", None)):
        try:
            return list(store.cases())
        except Exception as e:  # pragma: no cover - the learning report must not fail on it
            log.warning("fleet: kb cases unreadable: %s: %s", type(e).__name__, e)
            return []
    out: list[tuple[CaseInfo, dict[str, Any]]] = []
    root = Path(kb_dir) / "cases" if kb_dir is not None else None
    if root is None or not root.is_dir():
        return out
    for f in sorted(root.glob("*/case.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            case = CaseInfo.model_validate({k: v for k, v in data.items() if k in CaseInfo.model_fields})
            out.append((case, {k: v for k, v in data.items() if k not in CaseInfo.model_fields}))
        except Exception as e:  # noqa: BLE001 - one damaged case never costs the report
            log.warning("fleet: %s skipped: %s: %s", f, type(e).__name__, e)
    out.sort(key=lambda h: (-len(h[0].projects), -h[0].seen, h[0].case_id))
    return out


_RUN_CASES: dict[str, tuple[float, frozenset[str]]] = {}     # errors.jsonl path → (mtime, case ids): read once


def _run_case_ids(run_dir: Optional[str]) -> frozenset[str]:
    """Case ids of a run's `<run dir>/errors.jsonl` (cached by mtime; a summary is rewritten after every project)."""
    if not run_dir:
        return frozenset()
    f = Path(run_dir) / "errors.jsonl"
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return frozenset()
    hit = _RUN_CASES.get(str(f))
    if hit is not None and hit[0] == mtime:
        return hit[1]
    ids: set[str] = set()
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                cid = json.loads(line).get("case_id") if line.strip() else None
            except ValueError:
                continue
            if cid:
                ids.add(str(cid))
    except OSError:
        return frozenset()
    _RUN_CASES[str(f)] = (mtime, frozenset(ids))
    return _RUN_CASES[str(f)][1]


def fleet_case_projects(state: FleetState, cases: list[tuple[CaseInfo, dict[str, Any]]]) -> dict[str, list[str]]:
    """case id → this fleet's projects that met it: from each project's run errors.jsonl, plus the case's own
    project list (CaseInfo.projects holds the profile name, usually the fleet name too)."""
    out: dict[str, list[str]] = {}
    for p in state.projects:
        for cid in _run_case_ids(p.out_dir):
            out.setdefault(cid, []).append(p.name)
    names = {p.name for p in state.projects}
    for c, _ in cases:
        for n in c.projects:
            if n in names and n not in out.setdefault(c.case_id, []):
                out[c.case_id].append(n)
    return {k: v for k, v in out.items() if v}


def render_learning(state: FleetState, kb: Any = None, kb_dir: Optional[Path] = None) -> str:
    """``learning.md``: this fleet's error-list counts, then the KB cases its projects met (most projects first;
    the whole KB when none is known) with projects / seen / resolved and every stored script version (status
    successes/failures)."""
    ps = state.projects
    t = totals(state)
    lines = [f"# Fleet {state.fleet_id} — cases and fix scripts", "", "## This fleet", "",
             f"* projects: {len(ps)} ({sum(1 for p in ps if p.run_id)} run)",
             f"* LLM calls: {t['llm_calls']}",
             f"* error list: open {t['errors_open']}, resolved {t['errors_resolved']}, blocked {t['errors_blocked']}",
             ""]
    cases = read_cases(kb, kb_dir)
    lines += ["## Cases across projects (`sbmig kb cases show <case>`)", ""]
    if not cases:
        return "\n".join(lines + ["_No cases in the KB yet._", ""]) + "\n"
    met = fleet_case_projects(state, cases)
    scripts = [s for c, _ in cases for s in c.scripts]
    by = {k: sum(1 for s in scripts if s.status == k) for k in ("proven", "unverified", "retired")}
    lines += [f"{len(cases)} case(s) in the KB, {sum(1 for c, _ in cases if c.case_id in met)} met by this fleet's "
              f"projects; {len(scripts)} script version(s) (proven {by['proven']}, unverified {by['unverified']}, "
              f"retired {by['retired']}).", ""]
    shown = [h for h in cases if h[0].case_id in met] or cases           # this fleet's cases, else the whole KB
    shown.sort(key=lambda h: -len(met.get(h[0].case_id, [])))           # stable: KB order (most widespread) next
    lines += ["| case | name | this fleet | KB projects | seen / resolved | scripts (status ok/failed) | pin |",
              "|---|---|---|---|---|---|---|"]
    for c, extra in shown[:TOP_CASES]:
        here = met.get(c.case_id, [])
        fleet_cell = f"{len(here)}: " + ", ".join(here[:5]) + (" …" if len(here) > 5 else "") if here else "-"
        lines.append(f"| `{c.case_id}` | {_md_cell(c.name or c.l2, 120)} | {_md_cell(fleet_cell, 140)} | "
                     f"{len(c.projects)} | {c.seen} / {c.resolved} | {_md_cell(_scripts_cell(c), 200)} | "
                     f"{_md_cell(extra.get('pin'), 60)} |")
    if len(shown) > TOP_CASES:
        lines += ["", f"… {len(shown) - TOP_CASES} more case(s) — `<kb>/cases.md` has every one"]
    return "\n".join(lines) + "\n"


def write_summaries(
    state: Optional[FleetState], fleet_dir: Path, cfg: Optional[Config] = None, kb: Any = None
) -> dict[str, Path]:
    """Write every summary file on its own; returns the ones written (a file Excel holds open is skipped)."""
    from sbmig.kb.store import atomic_write          # tmp file + os.replace, retried while Windows holds the file

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
    kb_dir = cfg.resolve_path(cfg.kb.dir) if cfg is not None else None
    jobs.append(("learning", "learning.md", lambda: render_learning(state, kb, kb_dir)))
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
