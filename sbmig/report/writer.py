"""Report writers: report.json / report.md + a rich console summary."""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Optional

from sbmig.core.models import RunReport

from .markdown import group_by_file, render_markdown

log = logging.getLogger("sbmig.report")


def write_report(report: RunReport, ws: Any) -> dict[str, str]:
    """Write report.json and report.md into the run dir; returns {"report_json": path, "report_md": path}."""
    run_dir = ws.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    jp = run_dir / "report.json"
    out["report_json"] = str(jp)
    mp = run_dir / "report.md"
    out["report_md"] = str(mp)
    report.outputs.update({k: v for k, v in out.items() if k not in report.outputs})
    jp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    mp.write_text(render_markdown(report), encoding="utf-8")
    log.info("report written: %s", mp)
    return out


def console_summary(report: RunReport, console: Optional[Any] = None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    ok = "[bold green]BUILD SUCCESS[/]" if report.success else "[bold red]NOT BUILDING[/]"
    console.print(f"\n[bold]{report.project_name}[/] ({report.case_type.value}) — {ok}   run {report.run_id}")
    t = Table(title="phases", show_lines=False)
    for col in ("phase", "builds", "build ok", "attempts", "outcomes", "left"):
        t.add_column(col)
    for pr in report.phases:
        outs = Counter(a.outcome for a in pr.attempts)
        t.add_row(pr.phase.value, str(pr.iterations), "✅" if pr.build_ok else "❌", str(len(pr.attempts)),
                  ", ".join(f"{k} {v}" for k, v in outs.items()) or "-", str(len(pr.remaining_errors)))
    console.print(t)
    u = report.llm_usage
    console.print(f"LLM: {u.calls} call(s), {u.input_tokens}/{u.output_tokens} tokens"
                  + (f"   flags: {', '.join(report.flags)}" if report.flags else ""))
    if report.unresolved:
        ut = Table(title=f"unresolved ({len(report.unresolved)})")
        ut.add_column("file")
        ut.add_column("errors")
        for key, errs in list(group_by_file(report.unresolved).items())[:15]:
            ut.add_row(key, "\n".join(e.message[:120] for e in errs[:3]) + (" …" if len(errs) > 3 else ""))
        console.print(ut)
    for f in report.manual_followups[:10]:
        console.print(f"[yellow]follow-up[/] {f}")
    if report.outputs.get("report_md"):
        console.print(f"report: {report.outputs['report_md']}")
