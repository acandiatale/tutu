"""보고서 쓰기 — 실행 디렉토리에 report.json / report.md 를 쓰고, 콘솔에 실행 요약(rich 표)을 찍는다.
흐름상 위치: 최종 빌드와 KB 기록 뒤 오케스트레이터가 ``write_report`` 를, CLI 가 ``console_summary`` 를 부른다
(그 다음 CLI 가 ``SBMIG-RESULT`` 줄을 찍는다). 핵심 진입점: ``write_report``, ``console_summary``,
``report_language``, 콘솔 한 줄 함수들(``*_console_line``). report.json 은 언어와 무관하게 같고, report.md 만
``report.language`` 로 번역된다. 콘솔 줄은 가능한 한 ASCII — 회사 Windows 콘솔(cp949, 리다이렉트·작업
스케줄러)이 em dash 등을 못 찍는다.

Report writers: report.json / report.md + a rich console summary."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sbmig.core.models import RunReport

from .errors import (
    OPEN_STATUSES,
    case_rows,
    counts_text,
    entries_of,
    entry_text,
    seen_groups,
    stats_text,
    status_counts,
    tries_text,
    where,
)
from .i18n import normalize
from .markdown import BLOCKED_TITLE, blocked_count_line, group_by_file, render_markdown

log = logging.getLogger("sbmig.report")


def report_language(cfg: Any = None) -> str:
    """보고서 언어 ``cfg.report.language``("en" | "ko"). 설정이 없거나 모르는 값이면 "en".

    `cfg.report.language` ("en" | "ko"); "en" without a config or for an unknown value."""
    return normalize(getattr(getattr(cfg, "report", None), "language", None) if cfg is not None else None)


def write_report(report: RunReport, ws: Any, cfg: Any = None, *, language: Optional[str] = None) -> dict[str, str]:
    """실행 디렉토리에 report.json 과 report.md 를 쓰고 두 경로를 돌려준다.
    report.md 는 ``language``(없으면 설정의 언어)로 그리고, report.json 은 언어와 무관하다.

    Write report.json and report.md into the run dir; returns {"report_json": path, "report_md": path}.
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
    """콘솔 / CLI 용: 권고마다 한 줄(``advisory <id> → <결과>``) + 인수인계 개수 한 줄.

    One line per advisory finding + the handover counts (console / CLI)."""
    out: list[str] = []
    for f in report.advisories:
        out.append(f"advisory {f.advisory_id} → {f.outcome}")
    h = report.handover
    if h is not None:
        out.append(f"handover: {len(h.changes)} versions replaced, {len(h.carried_over)} carried over, "
                   f"{len(h.from_here)} items for the owners")
    return out


def properties_console_line(report: RunReport) -> Optional[str]:
    """인수인계에 외부 설정 키가 있으면 콘솔 한 줄 ``properties to set: N (placeholders in <파일>)``(round 8).

    `properties to set: N (placeholders in <file>)` when the Handover lists external keys (round 8)."""
    from .handover import external_to_set

    h = report.handover
    if h is None or not h.properties_to_set:
        return None
    inv = report.properties if report.properties is not None else getattr(report.profile, "properties", None)
    n = len(external_to_set(inv)) or len(h.properties_to_set)
    files = list(report.properties.placeholder_files) if report.properties is not None else []
    return f"properties to set: {n}" + (f" (placeholders in {', '.join(files)})" if files else "")


def review_console_line(report: RunReport) -> Optional[str]:
    """구조 검토 결과 콘솔 한 줄(v2). cp949 콘솔용으로 em dash·백틱을 뺀다. 검토가 없었으면 None.

    `structure review: …` (v2) — ASCII (cp949 consoles), None without a review."""
    rv = report.structure_review
    if rv is None:
        return None
    from .markdown import review_summary
    return "structure review: " + review_summary(rv).replace(" — ", " - ").replace("`", "")


def error_list_console_line(report: RunReport) -> Optional[str]:
    """오류 목록 콘솔 한 줄: 전체 수 - 상태별 수 (단계별 수). 오류 목록이 없으면 None.

    `error list: 25 - resolved 23, unresolved 2 (compile 23, restructure 1, build 1)` (ASCII); None without one."""
    entries = entries_of(report)
    if not entries:
        return None
    by_seen = ", ".join(f"{k} {len(v)}" for k, v in seen_groups(entries).items())
    return f"error list: {len(entries)} - {counts_text(status_counts(entries))} ({by_seen})"


CONSOLE_CASES = 15
CONSOLE_OPEN = 10


def console_summary(report: RunReport, console: Optional[Any] = None) -> None:
    """``sbmig run`` 끝의 콘솔 요약: 결과 → 오류 목록 수 → 차단 → 권고 → 케이스별 수정(짧게) → 아직 열린 오류
    → 구조 검토 → LLM 사용량·비용 → 차단 표 → 외부 설정 →
    후속 작업. ``SBMIG-RESULT`` 줄은 이 다음에 CLI 가 찍는다.

    End of `sbmig run`: the result, the error-list counts, the fixes by case (short), what is still open, the
    review line, LLM usage / cost, blocked, follow-ups. The SBMIG-RESULT line is printed by the CLI after it."""
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    ok = "[bold green]BUILD SUCCESS[/]" if report.success else "[bold red]NOT BUILDING[/]"
    # console-only lines stay ASCII where it is easy: a cp949 console (Windows, redirected / Task Scheduler) cannot
    # encode an em dash or the check marks (round 10; the CLI also sets errors="replace" on stdout / stderr)
    console.print(f"\n[bold]{report.project_name}[/] ({report.case_type.value}) - {ok}   run {report.run_id}")
    el = error_list_console_line(report)
    if el:
        console.print(el, markup=False, highlight=False)
    if report.blocked:
        console.print(f"[bold yellow]blocked:[/] {blocked_count_line(report.blocked).replace(' — ', ' - ')}")
    for line in advisory_lines(report):
        console.print(line, markup=False, highlight=False)
    entries = entries_of(report)
    rows = case_rows(entries)
    if rows:
        t = Table(title=f"fixes by case ({len(rows)})", show_lines=False)
        for col in ("case", "errors", "tries", "changes", "script"):
            t.add_column(col, overflow="fold")
        for row in rows[:CONSOLE_CASES]:
            script = (f"v{row.script_version} {row.script}" if row.script_version else row.script) or \
                ("no script" if row.no_script_reason else "-")
            t.add_row(row.case_id, f"{row.resolved}/{row.entries} resolved",
                      tries_text(row.tries).replace(" · ", "; ")[:120] or "-",
                      stats_text(row.changes, limit=3, code=False, minus="-") or "-", script)
        if len(rows) > CONSOLE_CASES:
            t.caption = f"... {len(rows) - CONSOLE_CASES} more (report.md, Fixes by case)"
        console.print(t)
    left = [(i, e) for rows_ in seen_groups(entries).values() for i, e in rows_ if e.status in OPEN_STATUSES]
    if left:
        ut = Table(title=f"unresolved ({len(left)})")
        for col in ("#", "where", "case", "message"):
            ut.add_column(col, overflow="fold")
        for i, e in left[:CONSOLE_OPEN]:
            ut.add_row(str(i), where(e) or "-", e.case_id or "-", entry_text(e)[:160])
        if len(left) > CONSOLE_OPEN:
            ut.caption = f"... {len(left) - CONSOLE_OPEN} more (report.md, Error list)"
        console.print(ut)
    # 오류 목록을 남기기 전에 멈춘 실행(예전 형식)만 예전 unresolved 표를 쓴다
    elif report.unresolved and not entries:          # a run that stopped before the error list was kept
        ut = Table(title=f"unresolved ({len(report.unresolved)})")
        ut.add_column("file")
        ut.add_column("errors")
        for key, errs in list(group_by_file(report.unresolved).items())[:15]:
            ut.add_row(key, "\n".join(e.message[:120] for e in errs[:3]) + (" …" if len(errs) > 3 else ""))
        console.print(ut)
    rl = review_console_line(report)
    if rl:
        console.print(rl, markup=False, highlight=False)
    u = report.llm_usage
    console.print(f"LLM: {u.calls} call(s), {u.input_tokens}/{u.output_tokens} tokens"
                  + (f"   flags: {', '.join(report.flags)}" if report.flags else ""))
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
    pl = properties_console_line(report)
    if pl:
        console.print(pl, markup=False, highlight=False)
    for f in report.manual_followups[:10]:
        console.print(f"[yellow]follow-up[/] {f}")
    if report.outputs.get("report_md"):
        console.print(f"report: {report.outputs['report_md']}")
