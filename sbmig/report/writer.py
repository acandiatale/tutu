"""Report writers: report.json / report.md + a rich console summary."""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Optional

from sbmig.core.models import RunReport

from .i18n import normalize
from .markdown import BLOCKED_TITLE, blocked_count_line, group_by_file, render_markdown

log = logging.getLogger("sbmig.report")


def report_language(cfg: Any = None) -> str:
    """`cfg.report.language` ("en" | "ko"); "en" without a config or for an unknown value."""
    return normalize(getattr(getattr(cfg, "report", None), "language", None) if cfg is not None else None)


def write_report(report: RunReport, ws: Any, cfg: Any = None, *, language: Optional[str] = None) -> dict[str, str]:
    """Write report.json and report.md into the run dir; returns {"report_json": path, "report_md": path}.
    report.md is rendered in `language`, else `cfg.report.language` (see `report_language`); report.json is the
    same whatever the language."""
    run_dir = ws.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    jp = run_dir / "report.json"
    out["report_json"] = str(jp)
    mp = run_dir / "report.md"
    out["report_md"] = str(mp)
    report.outputs.update({k: v for k, v in out.items() if k not in report.outputs})
    jp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    mp.write_text(render_markdown(report, language=normalize(language) if language else report_language(cfg)),
                  encoding="utf-8")
    log.info("report written: %s", mp)
    return out


def advisory_lines(report: RunReport) -> list[str]:
    """One line per advisory finding + the handover counts (console / CLI)."""
    out: list[str] = []
    for f in report.advisories:
        via = "recipe" if f.recipes else f.policy
        out.append(f"advisory {f.advisory_id} [{via}] → {f.outcome}")
    h = report.handover
    if h is not None:
        out.append(f"handover: {len(h.changes)} versions replaced, {len(h.carried_over)} carried over, "
                   f"{len(h.from_here)} items for the owners")
    return out


def properties_console_line(report: RunReport) -> Optional[str]:
    """`properties to set: N (placeholders in <file>)` when the Handover lists external keys (round 8)."""
    from .handover import external_to_set

    h = report.handover
    if h is None or not h.properties_to_set:
        return None
    inv = report.properties if report.properties is not None else getattr(report.profile, "properties", None)
    n = len(external_to_set(inv)) or len(h.properties_to_set)
    files = list(report.properties.placeholder_files) if report.properties is not None else []
    return f"properties to set: {n}" + (f" (placeholders in {', '.join(files)})" if files else "")


def review_console_line(report: RunReport) -> Optional[str]:
    """`structure review: …` (v2) — ASCII (cp949 consoles), None without a review."""
    rv = report.structure_review
    if rv is None:
        return None
    from .markdown import review_summary
    return "structure review: " + review_summary(rv).replace(" — ", " - ").replace("`", "")


def console_summary(report: RunReport, console: Optional[Any] = None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    ok = "[bold green]BUILD SUCCESS[/]" if report.success else "[bold red]NOT BUILDING[/]"
    # console-only lines stay ASCII where it is easy: a cp949 console (Windows, redirected / Task Scheduler) cannot
    # encode an em dash or the check marks (round 10; the CLI also sets errors="replace" on stdout / stderr)
    console.print(f"\n[bold]{report.project_name}[/] ({report.case_type.value}) - {ok}   run {report.run_id}")
    if report.blocked:
        console.print(f"[bold yellow]blocked:[/] {blocked_count_line(report.blocked).replace(' — ', ' - ')}")
    for line in advisory_lines(report):
        console.print(line, markup=False, highlight=False)
    t = Table(title="phases", show_lines=False)
    for col in ("phase", "builds", "build ok", "attempts", "outcomes", "left"):
        t.add_column(col)
    for pr in report.phases:
        outs = Counter(a.outcome for a in pr.attempts)
        t.add_row(pr.phase.value, str(pr.iterations), "ok" if pr.build_ok else "FAIL", str(len(pr.attempts)),
                  ", ".join(f"{k} {v}" for k, v in outs.items()) or "-", str(len(pr.remaining_errors)))
    console.print(t)
    u = report.llm_usage
    console.print(f"LLM: {u.calls} call(s), {u.input_tokens}/{u.output_tokens} tokens"
                  + (f"   flags: {', '.join(report.flags)}" if report.flags else ""))
    rl = review_console_line(report)
    if rl:
        console.print(rl, markup=False, highlight=False)
    if report.llm_cost is not None and (report.llm_cost.by_model or u.calls):
        from .cost import fmt_money
        c = report.llm_cost
        extra = f" (+ unpriced: {', '.join(c.unpriced_models)})" if c.unpriced_models else ""
        console.print(f"LLM cost: {fmt_money(c.total, c.currency)}{extra}")
    if report.blocked:
        bt = Table(title=f"{BLOCKED_TITLE} ({len(report.blocked)})")
        for col in ("reason", "artifact / owner", "files", "suggestion"):
            bt.add_column(col, overflow="fold")
        for b in report.blocked[:15]:
            who = b.artifact.coordinates if b.artifact else (b.owner_class or "-")
            bt.add_row(b.reason.value, who, ", ".join(b.files[:3]) + (" …" if len(b.files) > 3 else ""),
                       b.suggestion[:200])
        console.print(bt)
    if report.unresolved:
        ut = Table(title=f"unresolved ({len(report.unresolved)})")
        ut.add_column("file")
        ut.add_column("errors")
        for key, errs in list(group_by_file(report.unresolved).items())[:15]:
            ut.add_row(key, "\n".join(e.message[:120] for e in errs[:3]) + (" …" if len(errs) > 3 else ""))
        console.print(ut)
    pl = properties_console_line(report)
    if pl:
        console.print(pl, markup=False, highlight=False)
    for f in report.manual_followups[:10]:
        console.print(f"[yellow]follow-up[/] {f}")
    if report.outputs.get("report_md"):
        console.print(f"report: {report.outputs['report_md']}")
