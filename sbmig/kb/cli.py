"""`sbmig kb …` (v2.1, docs/v2/PLAN-v2.1.md §3.4): the error list of every run (`errors list | show`), the cases and
their fix scripts (`cases list | show | retire | activate | pin | unpin`), plus stats / validate / export / import /
reindex of the whole KB."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError

from sbmig.core.config import Config, load_config
from sbmig.errors.cases import CaseStore, entry_lines, scripts_text
from sbmig.errors.entries import fmt_entry, tries_text

from .store import FileKnowledgeBase

kb_cli = typer.Typer(
    help="File knowledge base (v2.1): every run's error list (`errors`), cases with their fix scripts (`cases`); "
         "stats, validate, export / import, reindex.",
    no_args_is_help=True)
errors_cli = typer.Typer(help="Every run's error entries (<kb>/errors.jsonl).", no_args_is_help=True)
cases_cli = typer.Typer(help="Cases (one per L2 signature) and their fix scripts (<kb>/cases/<case_id>/).",
                        no_args_is_help=True)
kb_cli.add_typer(errors_cli, name="errors")
kb_cli.add_typer(cases_cli, name="cases")


def _load_cfg(configs: list[Path]) -> Config:
    """KB commands do not need the company artifacts section; use a placeholder when no config provides it."""
    try:
        return load_config(*configs)
    except ValidationError as e:
        if not any(err.get("loc") == ("artifacts",) for err in e.errors()):
            raise
        return load_config(*configs, overrides={"artifacts": {"parent": {"group_id": "unset", "artifact_id": "unset"}}})


def _kb(ctx: typer.Context) -> FileKnowledgeBase:
    """The parent CLI may pre-populate ctx.obj["cfg"] (a loaded Config) or ctx.obj["kb"]."""
    obj = ctx.ensure_object(dict)
    if "kb" not in obj:
        cfg: Config = obj.get("cfg") or _load_cfg(obj.get("configs") or [])
        kb_dir = obj.get("kb_dir")
        obj["kb"] = FileKnowledgeBase(cfg, kb_dir=Path(kb_dir) if kb_dir else None)
    return obj["kb"]


def _fail(msg: str) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(1)


@kb_cli.callback()
def _main(
    ctx: typer.Context,
    config: list[Path] = typer.Option([], "--config", "-c", help="Extra config yml (repeatable)."),
    kb_dir: Optional[Path] = typer.Option(None, "--kb-dir", help="Override kb.dir from the config."),
) -> None:
    obj = ctx.ensure_object(dict)
    if config:
        obj["configs"] = list(config)
    if kb_dir:
        obj["kb_dir"] = kb_dir


# --------------------------------------------------------------------------- whole KB
@kb_cli.command("reindex")
def reindex_cmd(ctx: typer.Context) -> None:
    """Recount every case (seen / resolved / projects / last solution) from errors.jsonl and rewrite cases.md."""
    kb = _kb(ctx)
    n = kb.reindex()
    st = kb.stats()
    typer.echo(f"{n} case(s), {st['script_versions']} script version(s) — {kb.cases.md_path}")


@kb_cli.command("stats")
def stats_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")) -> None:
    """Counts: error entries, cases, script versions by status, pinned cases."""
    st = _kb(ctx).stats()
    if as_json:
        typer.echo(json.dumps(st, indent=2, ensure_ascii=False))
        return
    for k, v in st.items():
        typer.echo(f"{k:<20} {v}")


@kb_cli.command("validate")
def validate_cmd(ctx: typer.Context) -> None:
    """Check the KB files (errors.jsonl lines, case.json files, script files, pins). Exit 1 on a problem."""
    kb = _kb(ctx)
    problems = kb.validate()
    for p in problems:
        typer.echo(f"PROBLEM: {p}")
    if problems:
        typer.echo(f"{len(problems)} problem(s) in {kb.root}")
        raise typer.Exit(1)
    st = kb.stats()
    typer.echo(f"OK: {st['errors']} error entr(ies), {st['cases']} case(s), {st['script_versions']} script "
               f"version(s) in {kb.root}")


@kb_cli.command("export")
def export_cmd(ctx: typer.Context, out: Path = typer.Argument(..., metavar="OUT.zip")) -> None:
    """Export the KB as a zip (cases/, errors.jsonl, cases.md) to move it between machines."""
    typer.echo(f"exported {_kb(ctx).export_zip(out)}")


@kb_cli.command("import")
def import_cmd(ctx: typer.Context, src: Path = typer.Argument(..., metavar="IN.zip", exists=True),
               replace: bool = typer.Option(False, "--replace", help="Replace the local KB (kept as .bak-*).")) -> None:
    """Import an exported KB: merged by default (new entries, unknown cases, scripts by their code)."""
    counters = _kb(ctx).import_zip(src, merge=not replace)
    typer.echo(", ".join(f"{k} {v}" for k, v in counters.items()))


# --------------------------------------------------------------------------- errors
@errors_cli.command("list")
def errors_list_cmd(
    ctx: typer.Context,
    run: str = typer.Option("", "--run", help="Only this run id."),
    case: str = typer.Option("", "--case", help="Only this case (id or prefix)."),
    status: str = typer.Option("", "--status", help="open | pom-verify | resolved | blocked | unresolved | note"),
    project: str = typer.Option("", "--project"),
    limit: int = typer.Option(50, "--limit", help="Newest N (0 = all)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """One line per entry: id, run, status, case, file:line message, tries, resolved."""
    rows = list(_kb(ctx).cases.iter_errors(run=run, case=case, status=status, project=project))
    total = len(rows)
    if limit > 0:
        rows = rows[-limit:]
    if as_json:
        typer.echo(json.dumps([json.loads(e.model_dump_json()) for e in rows], indent=2, ensure_ascii=False))
        return
    for e in rows:
        typer.echo(f"{e.id}  {e.run_id or '-'}  {e.status:<10} {e.case_id or '-':<10}  {fmt_entry(e)[:140]}"
                   + (f"  [{tries_text(e)}]" if e.tries else "") + (f"  → {e.resolved}" if e.resolved else ""))
    typer.echo(f"{len(rows)} of {total} entr(ies)")


@errors_cli.command("show")
def errors_show_cmd(ctx: typer.Context, entry: str = typer.Argument(..., metavar="ID", help="Entry id or prefix."),
                    diff: bool = typer.Option(True, "--diff/--no-diff")) -> None:
    """The entry with every try: 원인 / 해결 / applied files / diff."""
    hits = list(_kb(ctx).cases.iter_errors(entry=entry))
    if not hits:
        _fail(f"no entry {entry}")
    ids = list(dict.fromkeys(e.id for e in hits))
    if len(ids) > 1:
        _fail(f"{entry} matches {len(ids)} entries: " + ", ".join(ids[:10]))
    for ln in entry_lines(hits[-1], diff=diff):
        typer.echo(ln)


# --------------------------------------------------------------------------- cases
def _case_id(store: CaseStore, key: str) -> str:
    ids = store.case_ids()
    if key in ids:
        return key
    hits = [c for c in ids if c.startswith(key)] if len(key) >= 4 else []
    if len(hits) == 1:
        return hits[0]
    _fail(f"no case {key}" if not hits else f"{key} matches {len(hits)} cases: " + ", ".join(hits[:10]))
    return ""


@cases_cli.command("list")
def cases_list_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")) -> None:
    """One line per case (most widespread first): id, projects, seen / resolved, scripts, pin, name."""
    cases = _kb(ctx).cases.cases()
    if as_json:
        typer.echo(json.dumps([{**c.model_dump(mode="json"), **x} for c, x in cases], indent=2, ensure_ascii=False))
        return
    for c, x in cases:
        typer.echo(f"{c.case_id}  projects {len(c.projects):<3} seen {c.seen:<4} resolved {c.resolved:<4} "
                   f"scripts [{scripts_text(c)}]" + (f"  pin {x['pin']}" if x.get("pin") else "")
                   + f"  {c.name or c.l2}")
    typer.echo(f"{len(cases)} case(s)")


@cases_cli.command("show")
def cases_show_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                   code: bool = typer.Option(True, "--code/--no-code", help="Print the scripts."),
                   last: int = typer.Option(5, "--last", help="The last N entries of the case with their tries."),
                   diff: bool = typer.Option(False, "--diff/--no-diff")) -> None:
    """case.json, its scripts (code) and the last entries of the case from errors.jsonl."""
    store = _kb(ctx).cases
    cid = _case_id(store, key)
    case = store.find(cid)
    if case is None:
        _fail(f"case {cid}: case.json does not read (`sbmig kb validate`)")
    extra = store.extra(cid)
    typer.echo(f"case {case.case_id}  {case.name}")
    typer.echo(f"  l2: {case.l2 or '-'}  kind {case.kind.value}")
    typer.echo(f"  seen {case.seen}, resolved {case.resolved}, projects {len(case.projects)}: "
               + (", ".join(case.projects) or "-"))
    for k, v in extra.items():
        typer.echo(f"  {k}: {v}")
    typer.echo(f"  KB 탐색: {store.describe(cid)}")
    if case.last_analysis:
        typer.echo(f"  last 원인: {case.last_analysis}")
    if case.last_solution:
        typer.echo(f"  last 해결: {case.last_solution}")
    for s in sorted(case.scripts, key=lambda s: s.version):
        typer.echo(f"  script v{s.version} ({s.file}) {s.status}, {s.source}, successes {s.successes} / failures "
                   f"{s.failures}, projects {', '.join(s.projects) or '-'}, made by {s.project or '-'} "
                   f"{s.created_by or ''}".rstrip() + (f", last {s.last_result}" if s.last_result else ""))
        if code:
            body = store.code(cid, s.version)
            for ln in (body if body is not None else "(file missing)").splitlines():
                typer.echo(f"    | {ln}")
    entries = list(store.iter_errors(case=cid))
    entries = [e for e in entries if e.case_id == cid]
    if entries and last > 0:
        typer.echo(f"  last {min(last, len(entries))} of {len(entries)} entr(ies):")
        for e in entries[-last:]:
            for ln in entry_lines(e, diff=diff):
                typer.echo("  " + ln)


def _status(ctx: typer.Context, key: str, version: int, retire: bool) -> None:
    store = _kb(ctx).cases
    cid = _case_id(store, key)
    try:
        s = store.retire(cid, version) if retire else store.activate(cid, version)
    except KeyError as e:
        _fail(str(e).strip("'\""))
    typer.echo(f"case {cid} script v{s.version}: {s.status}")


@cases_cli.command("retire")
def cases_retire_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                     version: int = typer.Argument(..., metavar="N")) -> None:
    """Never run version N again (a person's decision)."""
    _status(ctx, key, version, True)


@cases_cli.command("activate")
def cases_activate_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                       version: int = typer.Argument(..., metavar="N")) -> None:
    """Undo a retire (proven when it resolved the case in another project, else unverified)."""
    _status(ctx, key, version, False)


@cases_cli.command("pin")
def cases_pin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                  spec: str = typer.Argument(..., metavar="block[:REASON]")) -> None:
    """Pin a case to `block` (never tried: no script, no LLM; Handover names the reason)."""
    store = _kb(ctx).cases
    cid = key if key in store.case_ids() or len(key) == 10 else _case_id(store, key)
    try:
        store.set_pin(cid, spec)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"case {cid} pinned: {store.pin_of(cid)}")


@cases_cli.command("unpin")
def cases_unpin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID")) -> None:
    """Remove a case's pin."""
    store = _kb(ctx).cases
    cid = _case_id(store, key)
    store.set_pin(cid, "")
    typer.echo(f"case {cid} unpinned")
