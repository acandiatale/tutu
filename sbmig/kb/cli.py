"""`sbmig kb …` — typer sub-app for inspecting and curating the file-based knowledge base (incl. `sbmig kb errors …`
for the error-case events, `sbmig kb cases …` for the case catalog built from them and `sbmig kb scripts …` for the
v2 fix scripts stored per case)."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError

from sbmig.core.config import Config, load_config
from sbmig.core.models import (
    BlockReason,
    BuildError,
    CaseAction,
    CaseType,
    CatalogCase,
    ErrorCluster,
    ErrorKind,
    ProjectProfile,
    ReplacePair,
)
from sbmig.core.workspace import Workspace

from .catalog import CaseCatalog, action_text, case_id_for, pairs_text
from .recipes import validate_recipe_yaml
from .scripts import SCRIPT_METHODS, ScriptStore, lineage_text, parse_script_resolver
from .store import FileKnowledgeBase

kb_cli = typer.Typer(help="File-based knowledge base: cases, recipe candidates, promotion.", no_args_is_help=True)


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
        configs: list[Path] = obj.get("configs") or []
        cfg: Config = obj.get("cfg") or _load_cfg(configs)
        kb_dir = obj.get("kb_dir")
        obj["kb"] = FileKnowledgeBase(cfg, kb_dir=Path(kb_dir) if kb_dir else None)
    return obj["kb"]


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


@kb_cli.command("list")
def list_cmd(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", "-s", help="active | candidate | rejected"),
    cases: bool = typer.Option(False, "--cases", help="List cases instead of recipes."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List recipes (default) or cases."""
    kb = _kb(ctx)
    if cases:
        rows = list(kb.iter_cases())
        if as_json:
            typer.echo(json.dumps([c.model_dump(mode="json") for c in rows], indent=2))
            return
        if not rows:
            typer.echo("no cases")
        for c in rows:
            typer.echo(f"{c.id}  {c.phase.value:<11} reused={c.reuse_count:<3} {c.summary}")
        return
    try:
        recipes = kb.list_recipes(status)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    if as_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in recipes], indent=2))
        return
    if not recipes:
        typer.echo("no recipes")
    for r in recipes:
        typer.echo(f"{r.status:<9} {r.id}  ok={r.success_count} fail={r.fail_count} "
                   f"projects={len(r.projects)}  {r.description}")


@kb_cli.command("show")
def show_cmd(ctx: typer.Context, item_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Show a case (by case id) or a recipe (by recipe id) including its YAML."""
    kb = _kb(ctx)
    case = kb.get_case(item_id)
    if case is not None:
        typer.echo(case.model_dump_json(indent=2))
        return
    r = kb.get_recipe(item_id)
    if r is None:
        typer.echo(f"not found: {item_id}", err=True)
        raise typer.Exit(1)
    data = r.model_dump(mode="json")
    data.update(kb.recipe_meta_extra(r.id))
    typer.echo(json.dumps(data, indent=2))
    typer.echo("---")
    typer.echo(Path(r.path).read_text(encoding="utf-8"))


@kb_cli.command("promote")
def promote_cmd(ctx: typer.Context, recipe_id: str = typer.Argument(..., metavar="ID")) -> None:
    """Move a candidate (or rejected) recipe to active."""
    kb = _kb(ctx)
    try:
        r = kb.promote(recipe_id)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"promoted {r.id} -> {r.path}")
    _refresh_cases_for(kb, r.signatures)


@kb_cli.command("reject")
def reject_cmd(ctx: typer.Context, recipe_id: str = typer.Argument(..., metavar="ID"),
               reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Move a recipe to rejected."""
    kb = _kb(ctx)
    try:
        r = kb.reject(recipe_id, reason)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"rejected {r.id}")
    _refresh_cases_for(kb, r.signatures)


def _refresh_cases_for(kb: FileKnowledgeBase, signatures: list[str]) -> None:
    """A recipe changed status by hand: the catalog cases it covers get their action recomputed."""
    if not kb.cfg.kb.catalog or not signatures:
        return
    cat = kb.catalog()
    if not cat.index_path.exists():
        return
    try:
        changed = cat.refresh_signatures(signatures)
        if changed:
            cat.write_views()
            typer.echo(f"case catalog: {len(changed)} case(s) updated ({', '.join(changed[:10])})")
    except Exception as e:  # the recipe change itself is done
        typer.echo(f"case catalog not refreshed ({e}); run `sbmig kb reindex`", err=True)


@kb_cli.command("reindex")
def reindex_cmd(ctx: typer.Context) -> None:
    """Rebuild index.json and symbols.json from the case and recipe files (and error_hints.json from errors.jsonl)."""
    kb = _kb(ctx)
    n = kb.reindex()
    typer.echo(f"indexed {n} cases")
    store = kb.error_case_store()
    if store.hints_path.exists():
        idx = store.hints_index()
        typer.echo(f"error hints: {len(idx.get('by_generic', {}))} L2 signature(s), "
                   f"{len(idx.get('by_symbol', {}))} symbol(s)")
    cat = kb.catalog()
    if kb.cfg.kb.catalog and cat.index_path.exists():
        counts = cat.status_counts()
        typer.echo(f"case catalog: {sum(counts.values())} case(s) ("
                   + ", ".join(f"{k} {v}" for k, v in counts.items() if v) + f") — views in {cat.views_dir}")
    if kb.scripts.scripts_dir.exists():
        sc = kb.scripts.counts()
        problems = kb.scripts.check()
        typer.echo(f"fix scripts: {sc['script_cases']} case(s), {sc['script_versions']} version(s) "
                   f"({sc['scripts_proven']} proven, {sc['scripts_retired']} retired, {sc['scripts_quarantined']} "
                   "quarantined)" + (f"; {len(problems)} problem(s) — `sbmig kb scripts validate`" if problems else ""))


@kb_cli.command("stats")
def stats_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")) -> None:
    """Counts of cases, recipes per status, patterns and indexed signatures."""
    st = _kb(ctx).stats()
    if as_json:
        typer.echo(json.dumps(st, indent=2))
        return
    for k, v in st.items():
        typer.echo(f"{k:<18} {v}")


@kb_cli.command("ledger")
def ledger_cmd(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", help="regex on the project name"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="regex on the error-case symbol"),
    label: Optional[str] = typer.Option(None, "--label", help="regex on any fix label"),
    resolution: Optional[str] = typer.Option(
        None, "--resolution",
        help="regex: handler|plan_recipe|kb_recipe|kb_patch|llm_patch|optional_bump|blocked|unresolved|advisory"),
    signature: Optional[str] = typer.Option(None, "--signature", help="regex on the L1 or L2 signature"),
    limit: int = typer.Option(50, "--limit", "-n", help="show the last N matching records (0 = all)"),
    as_json: bool = typer.Option(False, "--json", help="one JSON object per line"),
) -> None:
    """Resolution ledger: in project P, error-case symbol S was resolved (or blocked) by R."""
    kb = _kb(ctx)
    try:
        rows = list(kb.ledger().iter(project=project, symbol=symbol, label=label, resolution=resolution,
                                     signature=signature))
    except re.error as e:
        raise typer.BadParameter(f"bad regex: {e}")
    if limit > 0:
        rows = rows[-limit:]
    if as_json:
        for r in rows:
            typer.echo(r.model_dump_json())
        return
    if not rows:
        typer.echo("no ledger records")
        return
    for r in rows:
        what = r.replacement or r.resolver_id or "-"
        typer.echo(f"{r.at.strftime('%Y-%m-%d %H:%M')}  {r.project:<20} {r.phase.value:<11} "
                   f"{r.resolution:<11} {r.outcome:<10} {r.symbol or '-':<28} "
                   f"[{','.join(r.fix_labels) or '-'}] {what}")


@kb_cli.command("symbols")
def symbols_cmd(ctx: typer.Context, as_json: bool = typer.Option(False, "--json"),
                symbol: Optional[str] = typer.Option(None, "--symbol", help="regex filter")) -> None:
    """Symbol index: error-case symbol → how it was fixed, in how many projects, by which case / recipe."""
    data = _kb(ctx).symbol_index().load()
    if symbol:
        rx = re.compile(symbol)
        data = {k: v for k, v in data.items() if rx.search(k)}
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    if not data:
        typer.echo("no symbols indexed")
        return
    for sym, ent in sorted(data.items(), key=lambda kv: -max((f.get("projects", 0) for f in kv[1]["fixes"]),
                                                              default=0)):
        for f in ent.get("fixes", []):
            typer.echo(f"{sym:<32} {ent.get('error_kind', ''):<14} projects={f.get('projects', 0):<3} "
                       f"cases={len(f.get('cases', [])):<3} [{','.join(f.get('labels') or []) or '-'}] "
                       f"{f.get('replacement') or '-'}"
                       + (f"  recipe={f['recipe_id']}" if f.get("recipe_id") else ""))


@kb_cli.command("learning")
def learning_cmd(ctx: typer.Context, md: bool = typer.Option(False, "--md", help="print markdown"),
                 as_json: bool = typer.Option(False, "--json"),
                 top: int = typer.Option(15, "--top")) -> None:
    """What did we learn / what still needs the LLM / what blocks most projects (from the ledger)."""
    from .ledger import render_learning_md

    kb = _kb(ctx)
    summary = kb.ledger().summary(top=top)
    if as_json:
        typer.echo(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    if md:
        typer.echo(render_learning_md(summary, kb.stats()), nl=False)
        return
    typer.echo(f"{summary['records']} ledger record(s), {summary['projects']} project(s), "
               f"{summary['runs']} run(s)")
    typer.echo("resolutions: " + (", ".join(f"{k}={v}" for k, v in summary["by_resolution"].items()) or "-"))
    typer.echo("\nwrite a recipe/handler for these (LLM-fixed ≥2×, no KB recipe):")
    for r in summary["llm_symbols_without_recipe"] or []:
        typer.echo(f"  {r['symbol']:<32} fixed={r['count']:<3} projects={r['projects']:<3} {r['label']}: "
                   f"{r['replacement'] or '-'}")
    if not summary["llm_symbols_without_recipe"]:
        typer.echo("  (none)")
    typer.echo("\nstill resolved by the LLM:")
    for r in summary["llm_symbols"] or []:
        typer.echo(f"  {r['symbol']:<32} fixed={r['count']:<3} projects={r['projects']:<3} "
                   f"recipe={'yes' if r['has_recipe'] else 'no'}  {r['label']}: {r['replacement'] or '-'}")
    if not summary["llm_symbols"]:
        typer.echo("  (none)")
    typer.echo("\nblocking most projects:")
    for r in summary["blocked"] or []:
        typer.echo(f"  {r['symbol']:<40} {r['reason']:<26} projects={r['projects']:<3} "
                   f"{', '.join(r['project_names'][:6])}")
    if not summary["blocked"]:
        typer.echo("  (none)")
    typer.echo("\nKB recipes / patches that paid off:")
    for r in summary["kb_fired"] or []:
        typer.echo(f"  {r['id']:<48} {r['kind']:<11} projects={r['projects']:<3} count={r['count']}")
    if not summary["kb_fired"]:
        typer.echo("  (none)")
    ec = summary.get("error_cases")
    if ec:
        typer.echo(f"\nerror cases: {ec['errors']} ({', '.join(f'{k}={v}' for k, v in ec['by_status'].items())}); "
                   f"LLM text replaces: {ec['llm_text_replace']} — `sbmig kb errors summary` for details")
    top_cases = summary.get("top_cases") or []
    if top_cases:
        typer.echo("\ntop error cases (`sbmig kb cases show <id>`):")
        for r in top_cases:
            typer.echo(f"  {r['id']}  {r['status']:<10} projects={r['projects']:<3} seen={r['seen']:<4} "
                       f"{_trim(r['name'], 60)}  — {_trim(r['action'], 60)}")


@kb_cli.command("export")
def export_cmd(ctx: typer.Context, out: Path = typer.Argument(..., metavar="OUT.zip")) -> None:
    """Export the KB as a zip (to move it between machines; error cases included)."""
    p = _kb(ctx).export_zip(out)
    typer.echo(f"exported {p}")


@kb_cli.command("import")
def import_cmd(ctx: typer.Context, src: Path = typer.Argument(..., metavar="IN.zip", exists=True),
               replace: bool = typer.Option(False, "--replace", help="Replace instead of merge.")) -> None:
    """Import a KB zip (merge by default)."""
    counters = _kb(ctx).import_zip(src, merge=not replace)
    typer.echo(" ".join(f"{k}={v}" for k, v in counters.items()))


@kb_cli.command("match")
def match_cmd(ctx: typer.Context, signature: str = typer.Argument(...),
              project: Optional[Path] = typer.Option(None, "--project", "-p",
                                                     help="Project dir for patch-replay checks.")) -> None:
    """Debug: what would kb.match return for a cluster with this (L1 or L2) signature?"""
    kb = _kb(ctx)
    head = signature.split("|", 1)[0]
    try:
        kind = ErrorKind(head)
    except ValueError:
        kind = ErrorKind.UNKNOWN
    err = BuildError(kind=kind, message=signature, signature=signature, signature_generic=signature)
    cluster = ErrorCluster(id="debug", kind=kind, errors=[err], signatures=[signature])
    with tempfile.TemporaryDirectory(prefix="sbmig-kb-match-") as tmp:
        root = project.resolve() if project else Path(tmp) / "project"
        root.mkdir(parents=True, exist_ok=True)
        ws = Workspace(root, Path(tmp) / "run", "kb-match")
        profile = ProjectProfile(root=str(root), name=root.name, case_type=CaseType.UNKNOWN)
        m = kb.match(cluster, ws, profile)
    typer.echo(json.dumps({
        "recipe_ids": m.recipe_ids,
        "candidate_recipe_ids": m.candidate_recipe_ids,
        "handler_ids": m.handler_ids,
        "cases": [c.id for c in m.cases],
        "replayable": [c.id for c in m.replayable],
        "case_hits": [{"case_id": h.case_id, "name": h.name, "status": h.status, "action": action_text(h.action)}
                      for h in m.case_hits],
        "hints": m.hints,
    }, indent=2, ensure_ascii=False))


@kb_cli.command("validate")
def validate_cmd(recipe: Path = typer.Argument(..., metavar="RECIPE.yml", exists=True)) -> None:
    """Validate a declarative recipe against the KB whitelist."""
    ok, errors, _ = validate_recipe_yaml(recipe.read_text(encoding="utf-8"))
    if ok:
        typer.echo("OK")
        return
    for e in errors:
        typer.echo(f"ERROR: {e}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------- sbmig kb errors
errors_cli = typer.Typer(
    help="Error cases: every build error of every run — what was tried, how it was resolved (the code applied) "
         "or why not.", no_args_is_help=True)
kb_cli.add_typer(errors_cli, name="errors")

_STATUS_HELP = "regex: resolved|resolved_indirectly|blocked|unresolved|not_visible"
_METHOD_HELP = "regex on the fix method: handler|recipe|kb_patch|llm|plan_step|none|script|script_reuse|script_augment"
_CHANGE_HELP = "regex: text_replace|code_edit|file_create|file_delete|file_rename|mixed|none"


def _trim(text: Optional[str], n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _select_errors(kb: FileKnowledgeBase, status, method, change_kind, project, symbol, signature, run,
                   case=None, resolver=None) -> list:
    try:
        return list(kb.error_case_store().iter_errors(status=status, method=method, change_kind=change_kind,
                                                      project=project, symbol=symbol, signature=signature,
                                                      run_id=run, case=case, resolver=resolver))
    except re.error as e:
        raise typer.BadParameter(f"bad regex: {e}")


def _method_text(fix_method: str, script_version: int = 0) -> str:
    """The fix method, with the script version of a v2 script attempt (``script_reuse v2``)."""
    return fix_method + (f" v{script_version}" if script_version else "")


def _how_or_why(e) -> str:
    if e.status in ("resolved", "resolved_indirectly"):
        who = " ".join(x for x in (e.fix_method, e.resolver_id) if x and x != "none") or "gone"
        pairs = ", ".join(f"{p.old}→{p.new}" for p in e.replace_pairs[:2])
        return f"{who}" + (f" [{pairs}]" if pairs else "") + (f": {e.description}" if e.description else "")
    if e.status == "blocked":
        return f"blocked ({e.block_reason or '?'}): {e.suggestion or e.failure_reason}"
    return e.failure_reason or "-"


@errors_cli.command("list")
def errors_list_cmd(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", help=_STATUS_HELP),
    method: Optional[str] = typer.Option(None, "--method", help=_METHOD_HELP),
    change_kind: Optional[str] = typer.Option(None, "--change-kind", help=_CHANGE_HELP),
    project: Optional[str] = typer.Option(None, "--project", help="regex on the project name / fingerprint"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="regex on the error symbol"),
    signature: Optional[str] = typer.Option(None, "--signature", help="regex on the L1 or L2 signature"),
    run: Optional[str] = typer.Option(None, "--run", help="exact run id"),
    case: Optional[str] = typer.Option(None, "--case", help="case id (or id prefix) of the case catalog"),
    resolver: Optional[str] = typer.Option(None, "--resolver", help="regex on the resolver id (handler / recipes / "
                                                                    "model / case-replace:<case>:<pairs>)"),
    limit: int = typer.Option(50, "--limit", "-n", help="show the last N matching records (0 = all)"),
    as_json: bool = typer.Option(False, "--json", help="one JSON object per line"),
) -> None:
    """Error records: status, method, change kind, project, file:line, message, how resolved / why not."""
    rows = _select_errors(_kb(ctx), status, method, change_kind, project, symbol, signature, run, case, resolver)
    if limit > 0:
        rows = rows[-limit:]
    if as_json:
        for e in rows:
            typer.echo(e.model_dump_json())
        return
    if not rows:
        typer.echo("no error cases")
        return
    for e in rows:
        where = f"{e.file or '-'}:{e.line}" if e.line else (e.file or "-")
        method = _method_text(e.fix_method, e.script_version if e.fix_method in SCRIPT_METHODS else 0)
        typer.echo(f"{e.id}  {e.status:<19} {method:<14} {e.change_kind:<12} {_trim(e.project, 20):<20} "
                   f"{_trim(where, 60)}  {_trim(e.message, 80)}  — {_trim(_how_or_why(e), 120)}")


def _when(at) -> str:
    return at.strftime("%Y-%m-%d %H:%M") if at else "-"


def _script_text(a) -> str:
    """``augment v3 (from v2), error: KeyError`` of a v2 script attempt; "" for any other attempt."""
    if not (a.script_mode or a.script_version):
        return ""
    out = f"{a.script_mode or '?'} v{a.script_version}" if a.script_version else f"{a.script_mode} (not stored)"
    if a.script_parent:
        out += f" (from v{a.script_parent})"
    if a.script_status:
        out += f", {a.script_status}" + (f": {_trim(a.script_error, 160)}" if a.script_error else "")
    return out


def _attempt_line(n: int, a, resolving: bool, part_of: bool = False) -> list[str]:
    who = " ".join(x for x in (a.fix_method, a.resolver_id) if x and x != "none")
    mark = "  ← resolved it" if resolving else "  ← part of the resolution (kept in the tree)" if part_of else ""
    head = f"  {n}. {a.id}  {who or '-'}  {a.outcome}  [{a.change.change_kind}]" + mark
    out = [head]
    if _script_text(a):
        out.append(f"     script:  {_script_text(a)}")
    if a.description:
        out.append(f"     what:    {_trim(a.description, 300)}")
    if a.failure:
        out.append(f"     failure: {_trim(a.failure, 300)}")
    for x in a.introduced_errors[:5]:
        out.append(f"     new error: {_trim(x, 200)}")
    return out


def _error_view(e, attempts: list, case: Optional[CatalogCase], all_diffs: bool) -> list[str]:
    lines = [f"error {e.id} — {e.status}"]
    lines.append(f"  project:   {e.project or e.project_fingerprint or '-'} (run {e.run_id or '-'}, {_when(e.at)})")
    cid = e.case_id or case_id_for(e.signature_generic)
    if case is not None:
        lines.append(f"  case:      {case.id}  {case.effective_name()}  [{case.effective_status()}, "
                     f"{action_text(case.effective_action())}]  → sbmig kb cases show {case.id}")
    elif cid:
        lines.append(f"  case:      {cid} (not in the catalog yet — `sbmig kb reindex`)")
    where = f"{e.file}:{e.line}" if e.file and e.line else (e.file or "-")
    lines.append(f"  error:     {e.error_kind.value}  {where}" + (f"  (module {e.module})" if e.module else ""))
    lines.append(f"  message:   {_trim(e.message, 300)}")
    if e.symbol or e.location:
        lines.append(f"  symbol:    {e.symbol or '-'}" + (f"   location: {e.location}" if e.location else ""))
    if e.artifact:
        lines.append(f"  artifact:  {e.artifact}")
    lines.append(f"  L1:        {e.signature or '-'}")
    lines.append(f"  L2:        {e.signature_generic or '-'}")
    lines.append(f"  seen:      first {e.first_seen or '-'}, last {e.last_seen or '-'}, in {e.builds_present} "
                 f"build(s)")
    if e.status in ("resolved", "resolved_indirectly"):
        who = " ".join(x for x in (e.fix_method, e.resolver_id) if x and x != "none") or "gone"
        by = f" (attempt {e.resolved_by}" + (", targeted)" if e.targeted else ")") if e.resolved_by else ""
        lines.append(f"  resolved:  at {e.resolved_at or '-'} by {who}{by}")
        script = (e.script_case_id, e.script_version) if e.script_case_id else parse_script_resolver(e.resolver_id)
        if script and script[1]:
            lines.append(f"  script:    case {script[0]} v{script[1]}  → sbmig kb scripts show {script[0]} "
                         f"--version {script[1]}")
        if e.description:
            lines.append(f"  how:       {_trim(e.description, 400)}")
        change = e.change_kind + (f"; replace {pairs_text(e.replace_pairs[:5])}" if e.replace_pairs else "")
        if e.fix_labels:
            change += f"; labels {', '.join(e.fix_labels)}"
        lines.append(f"  change:    {change}" + ("  (LLM text replace → rule candidate)" if e.replace_rule_candidate
                                                 else ""))
    else:
        lines.append(f"  why not:   {_trim(e.failure_reason, 400) or '-'}")
        if e.block_reason or e.suggestion:
            lines.append(f"  blocked:   {e.block_reason or '-'}: {_trim(e.suggestion, 300) or '-'}")
        for x in e.problem_errors[:10]:
            lines.append(f"  problem:   {_trim(x, 200)}")
    for x in e.notes[:10]:
        lines.append(f"  note:      {_trim(x, 300)}")
    lines.append("")
    chain = [i for i in e.resolution_attempts if i != e.resolved_by] if len(e.resolution_attempts) > 1 else []
    if attempts:
        lines.append(f"attempts ({len(attempts)}, in order):")
        for n, a in enumerate(attempts, 1):
            lines += _attempt_line(n, a, a.id == e.resolved_by, a.id in chain)
            if all_diffs and a.id != e.resolved_by and a.id not in chain:
                lines += ["     --- diff" + (" (truncated)" if a.diff_truncated else ""), a.diff or "(no diff)"]
    else:
        lines.append("(no fix attempts recorded for this error)")
    res = next((a for a in attempts if a.id == e.resolved_by), None)
    by_id = {a.id: a for a in attempts}
    if chain and all(i in by_id for i in e.resolution_attempts):
        # several attempts together resolved it (an earlier one stayed in the tree): all their diffs, in run order
        lines += ["", (f"--- resolution: {len(e.resolution_attempts)} attempts whose changes stayed in the tree "
                       "(run order)")]
        for i in e.resolution_attempts:
            a = by_id[i]
            role = "resolved it" if i == e.resolved_by else a.outcome
            lines += [f"--- diff of {a.id} ({role})" + (" (truncated)" if a.diff_truncated else ""),
                      a.diff or "(no diff)"]
    elif res is not None:
        lines += ["", f"--- resolving diff ({res.id})" + (" (truncated)" if res.diff_truncated else ""),
                  res.diff or "(no diff)"]
    elif e.diff_excerpt:
        lines += ["", "--- resolving diff (excerpt of this file)", e.diff_excerpt]
    return lines


def _attempt_view(a) -> list[str]:
    who = " ".join(x for x in (a.fix_method, a.resolver_id) if x and x != "none")
    lines = [f"attempt {a.id} — {who or '-'} {a.outcome}",
             f"  project:   {a.project or a.project_fingerprint or '-'} (run {a.run_id or '-'}, {_when(a.at)}), "
             f"phase {a.phase.value}, iteration {a.iteration}, cluster {a.cluster_id or '-'}"]
    if a.model:
        lines.append(f"  model:     {a.model}" + (f" ({a.llm_calls} call(s), {a.input_tokens}/{a.output_tokens} "
                                                   f"tokens)" if a.llm_calls else ""))
    if _script_text(a):
        lines.append(f"  script:    {_script_text(a)}  (case {a.script_case_id or '-'})")
        for x in a.script_log[:20]:
            lines.append(f"  log:       {_trim(x, 200)}")
    ch = a.change
    lines.append(f"  applied:   {'yes' if a.applied else 'no'}; change {ch.change_kind} (+{ch.lines_added} "
                 f"-{ch.lines_removed})" + (f"; replace {pairs_text(ch.replace_pairs[:5])}" if ch.replace_pairs
                                            else ""))
    if ch.files:
        lines.append(f"  files:     {', '.join(ch.files[:10])}")
    if a.description:
        lines.append(f"  what:      {_trim(a.description, 600)}")
    if a.failure:
        lines.append(f"  failure:   {_trim(a.failure, 600)}")
    for x in a.introduced_errors[:10]:
        lines.append(f"  new error: {_trim(x, 200)}")
    for x in a.remaining_errors[:10]:
        lines.append(f"  remaining: {_trim(x, 200)}")
    if a.error_ids:
        lines.append(f"  errors:    {', '.join(a.error_ids[:20])}")
    if a.fix_labels:
        lines.append(f"  labels:    {', '.join(a.fix_labels)}" + (f" ({a.replacement})" if a.replacement else ""))
    lines += ["--- diff" + (" (truncated)" if a.diff_truncated else ""), a.diff or "(no diff)"]
    return lines


@errors_cli.command("show")
def errors_show_cmd(ctx: typer.Context, item_id: str = typer.Argument(..., metavar="ID"),
                    as_json: bool = typer.Option(False, "--json", help="the raw records"),
                    all_diffs: bool = typer.Option(False, "--all-diffs", help="also the diffs of the failed attempts")
                    ) -> None:
    """One error record (id or unique id prefix): what it was, its case, every attempt (failure reasons) and the
    resolving diff; or one attempt by its id. `--json` prints the raw records."""
    kb = _kb(ctx)
    store = kb.error_case_store()
    err = store.get_error(item_id)
    if err is None:
        hits = [e for e in store.iter_errors() if e.id.startswith(item_id)]
        if len(hits) == 1:
            err = hits[0]
        elif len(hits) > 1:
            typer.echo(f"ambiguous id prefix {item_id}: {', '.join(e.id for e in hits[:10])}", err=True)
            raise typer.Exit(1)
    if err is None:
        att = store.get_attempt(item_id)
        if att is None:
            typer.echo(f"not found: {item_id}", err=True)
            raise typer.Exit(1)
        if as_json:
            typer.echo(att.model_dump_json(indent=2))
            return
        typer.echo("\n".join(_attempt_view(att)))
        return
    attempts = store.attempts_of(err)
    if as_json:
        typer.echo(json.dumps({"error": err.model_dump(mode="json"),
                               "attempts": [a.model_dump(mode="json") for a in attempts]}, indent=2,
                              ensure_ascii=False))
        return
    case = None
    cid = err.case_id or case_id_for(err.signature_generic)
    if cid and kb.cfg.kb.catalog:
        case = kb.catalog().get(cid)
    typer.echo("\n".join(_error_view(err, attempts, case, all_diffs)))


@errors_cli.command("summary")
def errors_summary_cmd(
    ctx: typer.Context,
    run: list[str] = typer.Option([], "--run", help="restrict to these run ids (repeatable)"),
    top: int = typer.Option(15, "--top"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Counts by status / method / change kind, status × method, top failure reasons and unresolved L2s."""
    s = _kb(ctx).error_case_store().summary(run_ids=list(run) or None, top=top)
    if as_json:
        typer.echo(json.dumps(s, indent=2, ensure_ascii=False))
        return
    typer.echo(f"{s['errors']} error case(s), {s['attempts']} attempt(s), {s['projects']} project(s), "
               f"{s['runs']} run(s); LLM text replaces: {s['llm_text_replace']}")
    typer.echo("status:      " + (", ".join(f"{k}={v}" for k, v in s["by_status"].items()) or "-"))
    typer.echo("resolved by: " + (", ".join(f"{k}={v}" for k, v in s["by_method"].items()) or "-"))
    typer.echo("change kind: " + (", ".join(f"{k}={v}" for k, v in s["by_change_kind"].items()) or "-"))
    typer.echo("\nstatus x method (unresolved / blocked: method of the last attempt):")
    for st, row in s["status_method"].items():
        typer.echo(f"  {st:<20} " + ", ".join(f"{m}={n}" for m, n in sorted(row.items(), key=lambda kv: -kv[1])))
    typer.echo("\nattempt outcomes per method:")
    for m, row in s["attempt_outcomes"].items():
        typer.echo(f"  {m:<14} " + ", ".join(f"{o}={n}" for o, n in row.items()))
    if not s["attempt_outcomes"]:
        typer.echo("  (none)")
    typer.echo("\ntop failure reasons:")
    for r in s["top_failure_reasons"]:
        typer.echo(f"  {_trim(r['signature_generic'], 60):<60} errors={r['count']:<3} projects={r['projects']:<3} "
                   f"{_trim(r['reason'], 120)}")
    if not s["top_failure_reasons"]:
        typer.echo("  (none)")
    typer.echo("\ntop unresolved / blocked:")
    for r in s["top_unresolved"]:
        typer.echo(f"  {_trim(r['signature_generic'], 60):<60} errors={r['count']:<3} projects={r['projects']:<3} "
                   f"blocked={r['blocked']}")
    if not s["top_unresolved"]:
        typer.echo("  (none)")


@errors_cli.command("replace-candidates")
def errors_replace_candidates_cmd(
    ctx: typer.Context,
    min_projects: int = typer.Option(1, "--min-projects", help="only pairs seen in at least N projects"),
    limit: int = typer.Option(50, "--limit", "-n", help="0 = all"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """LLM fixes that were only text replaces, grouped by L2 + replace pair — deterministic rule / command
    candidates."""
    rows = _kb(ctx).error_case_store().replace_candidates(min_projects=min_projects)
    if limit > 0:
        rows = rows[:limit]
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no text-replace candidates")
        return
    for r in rows:
        typer.echo(f"projects={r['projects']:<3} count={r['count']:<3} {_trim(r['signature_generic'], 60):<60} "
                   f"`{r['old']}` → `{r['new']}`  e.g. {r['example_file'] or '-'} ({r['example_attempt'] or '-'})")


@errors_cli.command("export")
def errors_export_cmd(
    ctx: typer.Context,
    csv_path: Optional[Path] = typer.Option(None, "--csv", help="error records → CSV (UTF-8 with BOM, Excel)"),
    attempts_csv: Optional[Path] = typer.Option(None, "--attempts-csv", help="their fix attempts → CSV"),
    status: Optional[str] = typer.Option(None, "--status", help=_STATUS_HELP),
    method: Optional[str] = typer.Option(None, "--method", help=_METHOD_HELP),
    change_kind: Optional[str] = typer.Option(None, "--change-kind", help=_CHANGE_HELP),
    project: Optional[str] = typer.Option(None, "--project", help="regex on the project name / fingerprint"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="regex on the error symbol"),
    signature: Optional[str] = typer.Option(None, "--signature", help="regex on the L1 or L2 signature"),
    run: Optional[str] = typer.Option(None, "--run", help="exact run id"),
    case: Optional[str] = typer.Option(None, "--case", help="case id (or id prefix) of the case catalog"),
    resolver: Optional[str] = typer.Option(None, "--resolver", help="regex on the resolver id"),
) -> None:
    """Export error records (and their attempts — with a filter: every attempt in their history and resolution)
    as Excel-readable CSV. A cell is prefixed with `'` only when a spreadsheet would run it as a formula (it starts
    with = or @, or with + / - and contains ( ) | ! = @); drop that one `'` when reading the CSV in a program."""
    from .error_cases import write_error_cases_csv, write_fix_attempts_csv

    if csv_path is None and attempts_csv is None:
        raise typer.BadParameter("give --csv PATH and/or --attempts-csv PATH")
    kb = _kb(ctx)
    rows = _select_errors(kb, status, method, change_kind, project, symbol, signature, run, case, resolver)
    if csv_path is not None:
        write_error_cases_csv(rows, csv_path)
        typer.echo(f"wrote {len(rows)} error case(s) to {csv_path}")
    if attempts_csv is not None:
        store = kb.error_case_store()
        filtered = any(x is not None for x in (status, method, change_kind, project, symbol, signature, run, case,
                                               resolver))
        if filtered:
            ids: list[str] = []
            for e in rows:
                for i in [*(r.attempt_id for r in e.attempts), *e.resolution_attempts, e.resolved_by or ""]:
                    if i and i not in ids and not i.startswith("step:"):
                        ids.append(i)
            wanted = set(ids)
            selected = {e.id for e in rows}
            attempts = [a for a in store.iter_attempts() if a.id in wanted or selected.intersection(a.error_ids)]
        else:
            attempts = list(store.iter_attempts())
        write_fix_attempts_csv(attempts, attempts_csv)
        typer.echo(f"wrote {len(attempts)} fix attempt(s) to {attempts_csv}")


# --------------------------------------------------------------------------- sbmig kb cases
cases_cli = typer.Typer(
    help="Case catalog: one reusable case per error signature (L2) — what the error is, how it was fixed, what "
         "failed before, and what a hit does. Edit the `human:` block of kb/catalog/…/<symbol>--<id>.yml or use "
         "reject / pin / unpin / hint / note.", no_args_is_help=True)
kb_cli.add_typer(cases_cli, name="cases")

_ACTION_HELP = ("script:<N> (v2: a stored fix-script version of the case) | recipe:<id>[,<id>…] | "
                "replace:<old>=<new>[,<old>=<new>…] | handler:<id> | hint | block[:<reason>] (reason = a BlockReason "
                "value, or free text kept as the suggestion)")


def _catalog(ctx: typer.Context) -> CaseCatalog:
    return _kb(ctx).catalog()


def _the_case(cat: CaseCatalog, key: str) -> CatalogCase:
    ids = cat.resolve(key)
    if not ids:
        typer.echo(f"no case for {key} (id, id prefix, L2 / L1 signature or symbol)", err=True)
        raise typer.Exit(1)
    if len(ids) > 1:
        rows = [cat.get(i) for i in ids[:10]]
        typer.echo(f"ambiguous key {key}: " + "; ".join(f"{c.id} {c.effective_name()}" for c in rows if c),
                   err=True)
        raise typer.Exit(1)
    case = cat.get(ids[0])
    if case is None:
        typer.echo(f"case {ids[0]} is unreadable; run `sbmig kb reindex`", err=True)
        raise typer.Exit(1)
    return case


def _status_text(c: CatalogCase) -> str:
    return c.effective_status() + ("*" if c.human.status else "")


def _action_text(c: CatalogCase) -> str:
    return action_text(c.effective_action()) + ("*" if c.human.action else "")


def _done(cat: CaseCatalog, c: CatalogCase, what: str) -> None:
    typer.echo(f"{what} {c.id}  {c.effective_name()}: status {_status_text(c)}, action {_action_text(c)}  "
               f"({cat.path_of(c.id) or '-'})")


def parse_action(spec: str, replace_scope: str = "error-lines") -> CaseAction:
    """`--action` of `sbmig kb cases pin` → CaseAction (ValueError on a malformed spec)."""
    kind, _, arg = (spec or "").partition(":")
    kind, arg = kind.strip().lower(), arg.strip()
    if kind == "script":
        num = arg[1:] if arg[:1].lower() == "v" else arg
        if not num.isdigit() or int(num) < 1:
            raise ValueError("script:<N> needs a version number (script:2)")
        return CaseAction(kind="script", script_version=int(num))
    if kind == "recipe":
        ids = [x.strip() for x in arg.split(",") if x.strip()]
        if not ids:
            raise ValueError("recipe:<id> needs a recipe id")
        return CaseAction(kind="recipe", recipe_ids=ids)
    if kind == "replace":
        pairs = []
        for part in arg.split(","):
            old, sep, new = part.partition("=")
            if not sep or not old.strip() or old.strip() == new.strip():
                raise ValueError(f"replace pair {part!r} is not <old>=<new>")
            pairs.append(ReplacePair(old=old.strip(), new=new.strip()))
        return CaseAction(kind="replace", replace_pairs=pairs, replace_scope=replace_scope)
    if kind == "handler":
        if not arg:
            raise ValueError("handler:<id> needs a handler id")
        return CaseAction(kind="handler", handler_id=arg)
    if kind == "hint":
        return CaseAction(kind="hint")
    if kind == "block":
        if arg in {r.value for r in BlockReason}:
            return CaseAction(kind="block", block_reason=arg)
        return CaseAction(kind="block", suggestion=arg)
    raise ValueError(f"unknown action {spec!r}; expected {_ACTION_HELP}")


@cases_cli.command("list")
def cases_list_cmd(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", help="regex on the effective status: "
                                                                 "observed|candidate|active|rejected"),
    action: Optional[str] = typer.Option(None, "--action", help="regex on the effective action kind: "
                                                                 "script|hint|recipe|replace|handler|patch|block"),
    kind: Optional[str] = typer.Option(None, "--kind", help="regex on the error kind / class (compile, "
                                                             "cannot_find_symbol …)"),
    search: Optional[str] = typer.Option(None, "--search", help="regex on id, name, signature, symbol, owners"),
    limit: int = typer.Option(0, "--limit", "-n", help="first N (0 = all)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Cases, most projects first: id, status, action, seen, projects, name (`*` = set by a person)."""
    try:
        rx = {k: re.compile(v) for k, v in (("status", status), ("action", action), ("kind", kind),
                                              ("search", search)) if v}
    except re.error as e:
        raise typer.BadParameter(f"bad regex: {e}")
    rows = []
    for c in _catalog(ctx).cases():
        if "status" in rx and not rx["status"].search(c.effective_status()):
            continue
        if "action" in rx and not rx["action"].search(c.effective_action().kind):
            continue
        if "kind" in rx and not (rx["kind"].search(c.error_kind.value) or rx["kind"].search(c.error_class)):
            continue
        if "search" in rx:
            hay = [c.id, c.name, c.effective_name(), c.signature_generic, c.symbol,
                   *(v.owner for v in c.variants), *(v.signature for v in c.variants)]
            if not any(rx["search"].search(h) for h in hay if h):
                continue
        rows.append(c)
    if limit > 0:
        rows = rows[:limit]
    if as_json:
        typer.echo(json.dumps([c.model_dump(mode="json") for c in rows], indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no cases")
        return
    for c in rows:
        typer.echo(f"{c.id}  {_status_text(c):<10} seen={c.evidence.seen:<4} projects={c.evidence.projects:<3} "
                   f"{_trim(c.effective_name(), 60):<60}  {_trim(_action_text(c), 70)}")


def _event_line(ev) -> str:
    steps = " → ".join(ev.attempts)
    if not steps:
        steps = (f"{ev.fix_method} {ev.resolved_by or ''}".strip() if ev.fix_method != "none" else "no attempt")
    where = ev.file.rsplit("/", 1)[-1] if ev.file else "-"
    return (f"  {ev.at.strftime('%Y-%m-%d') if ev.at else '----------'} {_trim(ev.project or '-', 24):<24} "
            f"{steps} ({ev.status})  · {where} · error {ev.error_id}")


def case_report(cat: CaseCatalog, c: CatalogCase, store=None, diff: bool = True) -> list[str]:
    """The readable `sbmig kb cases show` text."""
    ev = c.evidence
    lines = [f"case {c.id} — {c.effective_name()}"]
    lines.append(f"  file:      {cat.path_of(c.id) or '-'}")
    lines.append(f"  key (L2):  {c.signature_generic}")
    lines.append(f"  kind:      {c.error_kind.value}" + (f" / {c.error_class}" if c.error_class else "")
                 + (f", symbol {c.symbol}" if c.symbol else ""))
    lines.append(f"  status:    {_status_text(c)}")
    a = c.effective_action()
    lines.append(f"  action:    {_action_text(c)}" + (f" (scope {a.replace_scope})" if a.kind == "replace" else ""))
    if c.human.status or c.human.action or c.human.name:
        lines.append(f"  generated: {c.name}: status {c.status}, action {action_text(c.action)}  "
                     "(`*` = the human block overrides it)")
    span = ""
    if ev.first_at:
        span = f", {ev.first_at.strftime('%Y-%m-%d')} … {ev.last_at.strftime('%Y-%m-%d') if ev.last_at else '?'}"
    lines.append(f"  evidence:  seen {ev.seen}× in {ev.projects} project(s) / {ev.runs} run(s){span}")
    lines.append("             by status: " + (", ".join(f"{k} {v}" for k, v in ev.by_status.items()) or "-")
                 + "; resolved by: " + (", ".join(f"{k} {v}" for k, v in ev.resolved_by.items()) or "-"))
    promote = cat.promote_after if cat.promote_after > 0 else "never (auto promotion off)"
    if cat.flag("case_replace") or a.kind == "replace" or ev.action_successes or ev.action_failures:
        lines.append(f"             own replace (handler case-replace): {ev.action_successes} success(es) in "
                     f"{len(ev.action_projects)} project(s), {ev.action_failures} failure(s); active after {promote} "
                     "project(s) without a failure; falls back at ≥ 2 failures and more failures than successes")
    elif a.kind == "script":
        lines.append(f"             fix script: active after {promote} project(s) without a failure of the version")
    if c.variants:
        lines += ["", "variants (owner / L1):"]
        for v in c.variants[:10]:
            lines.append(f"  {v.owner or '(no owner)'}  {v.signature or '-'}   seen {v.seen}, {v.projects} project(s)")
        if len(c.variants) > 10:
            lines.append(f"  … {len(c.variants) - 10} more")
    lines += ["", f"timeline (oldest first, last {len(ev.timeline)}):"]
    lines += [_event_line(x) for x in ev.timeline] or ["  (none)"]
    lines += ["", "fixes:"]
    for f in ev.fixes:
        who = " ".join(x for x in (f.method, f.resolver_id) if x)
        txt = f"  {who} — {f.count}× in {f.projects} project(s) [{f.change_kind}]"
        if f.description:
            txt += f": {_trim(f.description, 200)}"
        if f.replace_pairs:
            txt += f"; replace {pairs_text(f.replace_pairs[:5])}"
        txt += f"  (latest {f.attempt_id or '-'}, {f.project or '-'}, {f.at.strftime('%Y-%m-%d') if f.at else '-'})"
        lines.append(txt)
    if not ev.fixes:
        lines.append("  (none yet)")
    lines += ["", "failures:"]
    for x in ev.failures:
        lines.append(f"  {x.method} {x.outcome} — {x.count}× in {x.projects} project(s)"
                     + (f": {_trim(x.failure, 200)}" if x.failure else "") + f"  ({x.attempt_id or '-'}, "
                     f"{x.project or '-'})")
    if not ev.failures:
        lines.append("  (none)")
    lines += _script_section(cat, c)
    lines += ["", "hints (to the fix LLM, people's first):"]
    hints = [f"  [person] {h}" for h in c.human.hints] + [f"  {h}" for h in c.hints]
    lines += hints or ["  (none)"]
    if c.human.notes:
        lines += ["", "notes:"] + [f"  {ln}" for ln in c.human.notes.splitlines()]
    try:
        history = list(cat.iter_audit(c.id))[-10:]
    except Exception:  # noqa: BLE001 - the audit is informative here
        history = []
    if history:
        lines += ["", f"history (kb/{cat.audit_path.name}, last {len(history)}):"]
        for h in history:
            b, a = h.get("before") or {}, h.get("after") or {}
            if h.get("event") == "human":
                what = f"{h.get('command') or 'edit'}: {', '.join(h.get('fields') or [])}"
            elif h.get("event") == "script":
                what = f"script v{h.get('version', '?')} {h.get('action', '')}" + (
                    f" {h['outcome']}" if h.get("outcome") else "") + (
                    f" ({h['project']})" if h.get("project") else "") + (f": {h['reason']}" if h.get("reason") else "")
            else:
                what = (f"{b.get('status') or '-'} → {a.get('status')}" if b.get("status") != a.get("status") else
                        f"action {b.get('action') or '-'} → {a.get('action')}") + f" ({h.get('reason', '')})"
            lines.append(f"  {str(h.get('at', ''))[:16]}  {h.get('who', '?'):<12} {_trim(what, 160)}")
    if diff and store is not None:
        latest = max(ev.fixes, key=lambda f: f.at.timestamp() if f.at else 0.0, default=None)
        att = None
        if latest is not None and latest.attempt_id and not latest.attempt_id.startswith("step:"):
            att = next(store.iter_attempts(ids={latest.attempt_id}), None)
        chain = []
        if latest is not None and latest.attempt_ids:
            chain = list(store.iter_attempts(ids=set(latest.attempt_ids)))
        if att is not None and len(chain) > 1:
            lines += ["", (f"--- latest successful fix: {len(chain)} attempts whose changes stayed in the tree "
                           f"(run order, {att.id} resolved it)")]
            for a in chain:
                who = " ".join(x for x in (a.fix_method, a.resolver_id) if x)
                lines += [f"--- {a.id} ({a.project or '-'}, {who}, {a.outcome}, {a.change.change_kind})"
                          + (" (truncated)" if a.diff_truncated else ""), a.diff or "(no diff)"]
        elif att is not None:
            who = " ".join(x for x in (att.fix_method, att.resolver_id) if x)
            lines += ["", f"--- latest successful fix: {att.id} ({att.project or '-'}, {who}, "
                          f"{att.change.change_kind})" + (" (truncated)" if att.diff_truncated else ""),
                      att.diff or "(no diff)"]
        elif latest is not None:
            lines += ["", f"(the latest fix {latest.attempt_id or '-'} has no recorded diff)"]
    return lines


def _script_section(cat: CaseCatalog, c: CatalogCase) -> list[str]:
    """v2: the case's stored fix scripts (best first marker, record, lineage) for `cases show`."""
    store = cat.scripts()
    try:
        metas = store.versions(c.id)
        ranked = [m.version for m in store.ranked(c.id)]
    except Exception as e:  # noqa: BLE001 - informative
        return ["", f"fix scripts: unreadable ({e})"]
    if not metas:
        return []
    out = ["", f"fix scripts ({len(metas)} version(s); `sbmig kb scripts show {c.id} [--version N]`):"]
    for m in reversed(metas):
        mark = "  ← reused first" if ranked and m.version == ranked[0] else ""
        out.append(f"  {_script_row(m)}{mark}")
    return out


@cases_cli.command("show")
def cases_show_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="KEY",
                                                                  help="id, id prefix, L2 / L1 signature or symbol"),
                   as_json: bool = typer.Option(False, "--json", help="the raw case"),
                   diff: bool = typer.Option(True, "--diff/--no-diff", help="the latest successful fix's diff")
                   ) -> None:
    """One case, readable: name, key and variants, status / action, evidence, the timeline (failed attempts before
    the fix), fixes, failures, hints and the latest successful fix's diff."""
    kb = _kb(ctx)
    cat = kb.catalog()
    c = _the_case(cat, key)
    if as_json:
        typer.echo(c.model_dump_json(indent=2))
        return
    typer.echo("\n".join(case_report(cat, c, kb.error_case_store(), diff)))


_COMMAND_OF = {"rejected": "reject", "pinned": "pin", "unpinned": "unpin", "hint added to": "hint",
               "note added to": "note"}


def _set(ctx: typer.Context, key: str, what: str, **fields) -> None:
    """A human edit of a case (recorded in kb/audit.jsonl: who = the OS user, when, before → after)."""
    cat = _catalog(ctx)
    c = _the_case(cat, key)
    try:
        c = cat.set_human(c.id, audit_note=f"sbmig kb cases {_COMMAND_OF.get(what, what)}", **fields)
    except (KeyError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    _done(cat, c, what)


def _append_note(c: CatalogCase, text: str) -> str:
    return (c.human.notes.rstrip("\n") + "\n" if c.human.notes else "") + text


@cases_cli.command("reject")
def cases_reject_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="ID"),
                     reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Switch a case off: it never matches again (no action, no hints) until `unpin`."""
    c = _the_case(_catalog(ctx), key)
    fields = {"status": "rejected"}
    if reason:
        fields["notes"] = _append_note(c, f"rejected: {reason}")
    _set(ctx, c.id, "rejected", **fields)


@cases_cli.command("pin")
def cases_pin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="ID"),
                  action: str = typer.Option(..., "--action", "-a", help=_ACTION_HELP)) -> None:
    """Pin the action a hit applies (a person's decision: status active)."""
    kb = _kb(ctx)
    try:
        act = parse_action(action, kb.cfg.kb.case_replace_scope)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    for rid in act.recipe_ids:
        if kb.get_recipe(rid) is None:
            typer.echo(f"note: {rid} is not a KB recipe (kb/recipes/); the KB recipe step runs KB recipes only",
                       err=True)
    _set(ctx, key, "pinned", action=act, status="active")


@cases_cli.command("unpin")
def cases_unpin_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="ID")) -> None:
    """Back to the generated status / action (clears a pinned action and a status set by a person, `reject`
    included)."""
    _set(ctx, key, "unpinned", action=None, status=None)


@cases_cli.command("hint")
def cases_hint_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="ID"),
                   text: str = typer.Argument(..., metavar="TEXT")) -> None:
    """Add a hint for the fix LLM (people's hints go before the generated ones)."""
    c = _the_case(_catalog(ctx), key)
    text = " ".join(text.split())
    if not text:
        raise typer.BadParameter("empty hint")
    hints = list(c.human.hints) + ([text] if text not in c.human.hints else [])
    _set(ctx, c.id, "hint added to", hints=hints)


@cases_cli.command("note")
def cases_note_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="ID"),
                   text: str = typer.Argument(..., metavar="TEXT")) -> None:
    """Add a note (for people; not sent to the LLM)."""
    c = _the_case(_catalog(ctx), key)
    if not text.strip():
        raise typer.BadParameter("empty note")
    _set(ctx, c.id, "note added to", notes=_append_note(c, text.strip()))



# --------------------------------------------------------------------------- sbmig kb scripts (v2)
scripts_cli = typer.Typer(
    help="v2 fix scripts: the LLM-written fix-script versions of every case (kb/scripts/<case id>/v<N>.py + "
         "v<N>.meta.json) — record, lineage, the version a reuse runs first; retire / activate a version.",
    no_args_is_help=True)
kb_cli.add_typer(scripts_cli, name="scripts")


def _script_case(store: ScriptStore, key: str) -> str:
    """A case id of the script store by id or unique id prefix (exit 1 otherwise)."""
    key = (key or "").strip()
    ids = store.case_ids()
    if key in ids:
        return key
    hits = [i for i in ids if key and i.startswith(key)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        typer.echo(f"ambiguous case id prefix {key}: {', '.join(hits[:10])}", err=True)
    else:
        typer.echo(f"no fix scripts for case {key} (`sbmig kb scripts list`)", err=True)
    raise typer.Exit(1)


def _script_row(m) -> str:
    """One version: number (← parent), status, record, last outcome, who made it."""
    parent = f" ← v{m.parent}" if m.parent else ""
    return (f"v{m.version}{parent:<6} {m.status:<11} ok={m.successes:<3} failed={m.failures:<3} "
            f"partial={m.partials:<3} projects={len(set(m.success_projects)):<3} last={m.last_outcome or '-':<12} "
            f"{m.source}/{m.model or '-'}  {_when(m.created_at)}  {m.project or '-'}")


def _last_run(metas: list):
    ran = [m for m in metas if m.last_at is not None]
    return max(ran, key=lambda m: (m.last_at.timestamp(), m.version)) if ran else None


def _refresh_script_case(kb: FileKnowledgeBase, meta) -> None:
    """A version changed status by hand: the catalog case gets its action / views recomputed."""
    if not kb.cfg.kb.catalog or not meta.signature_generic:
        return
    cat = kb.catalog()
    if not cat.index_path.exists():
        return
    try:
        changed = cat.refresh([meta.signature_generic])
        cat.write_views()
        if changed:
            c = cat.get(meta.case_id)
            typer.echo(f"case {meta.case_id}: action {action_text(c.effective_action()) if c else '?'}")
    except Exception as e:  # the status change itself is stored
        typer.echo(f"case catalog not refreshed ({e}); run `sbmig kb reindex`", err=True)


@scripts_cli.command("list")
def scripts_list_cmd(
    ctx: typer.Context,
    case: Optional[str] = typer.Option(None, "--case", help="case id (or id prefix): that case and its versions"),
    status: Optional[str] = typer.Option(None, "--status", help="regex on the version status: "
                                                                 "active|retired|quarantined"),
    limit: int = typer.Option(0, "--limit", "-n", help="first N cases (0 = all)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Cases with stored fix scripts, most success projects first: versions, the version a reuse runs first,
    successes / failures over the versions, the last outcome, the L2 signature."""
    try:
        rx = re.compile(status) if status else None
    except re.error as e:
        raise typer.BadParameter(f"bad regex: {e}")
    store = _kb(ctx).scripts
    ids = store.case_ids()
    if case:
        ids = [i for i in ids if i.startswith(case.strip())]
    rows = []
    for cid in ids:
        metas = [m for m in store.versions(cid) if rx is None or rx.search(m.status)]
        if not metas:
            continue
        ranked = store.ranked(cid)
        last = _last_run(metas)
        rows.append({"case_id": cid, "signature_generic": next((m.signature_generic for m in reversed(metas)
                                                               if m.signature_generic), ""),
                     "versions": len(metas), "best": ranked[0].version if ranked else 0,
                     "successes": sum(m.successes for m in metas), "failures": sum(m.failures for m in metas),
                     "partials": sum(m.partials for m in metas),
                     "success_projects": len({p for m in metas for p in m.success_projects}),
                     "last_outcome": last.last_outcome if last else "", "metas": metas})
    rows.sort(key=lambda r: (-r["success_projects"], -r["successes"], r["case_id"]))
    if limit > 0:
        rows = rows[:limit]
    if as_json:
        typer.echo(json.dumps([{**{k: v for k, v in r.items() if k != "metas"},
                                "version_list": [m.model_dump(mode="json") for m in r["metas"]]} for r in rows],
                              indent=2, ensure_ascii=False))
        return
    if not rows:
        typer.echo("no fix scripts")
        return
    for r in rows:
        best = f"v{r['best']}" if r["best"] else "-"
        typer.echo(f"{r['case_id']}  versions={r['versions']:<3} best={best:<5} ok={r['successes']:<4} "
                   f"failed={r['failures']:<4} projects={r['success_projects']:<3} last={r['last_outcome'] or '-':<12} "
                   f"{_trim(r['signature_generic'], 90) or '-'}")
        if case:
            for m in reversed(r["metas"]):
                typer.echo(f"  {_script_row(m)}")


@scripts_cli.command("show")
def scripts_show_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID",
                                                                    help="case id or unique id prefix"),
                     version: Optional[int] = typer.Option(None, "--version", "-v",
                                                           help="default: the version a reuse runs first, else the "
                                                                "newest active one, else the newest"),
                     code: bool = typer.Option(True, "--code/--no-code", help="print the script"),
                     as_json: bool = typer.Option(False, "--json", help="meta + code + lineage")) -> None:
    """One fix-script version, readable: its record, lineage (v1 → v3 → v4), the LLM's analysis, the other versions
    of the case and the code."""
    kb = _kb(ctx)
    store = kb.scripts
    cid = _script_case(store, key)
    metas = store.versions(cid)
    if version is None:
        ranked = store.ranked(cid)
        pick = ranked[0] if ranked else next((m for m in reversed(metas) if m.status == "active"),
                                             metas[-1] if metas else None)
        version = pick.version if pick is not None else 0
    got = store.get(cid, version)
    if got is None:
        have = ", ".join(f"v{m.version}" for m in metas) or "none readable"
        typer.echo(f"no readable fix script {cid} v{version} (stored: {have})", err=True)
        raise typer.Exit(1)
    meta, text = got
    chain = store.lineage(cid, meta.version)
    if as_json:
        typer.echo(json.dumps({"meta": meta.model_dump(mode="json"), "code": text,
                               "lineage": [m.version for m in chain]}, indent=2, ensure_ascii=False))
        return
    order = [m.version for m in store.ranked(cid)]
    lines = [f"fix script {cid} v{meta.version} — {meta.status}"]
    case = kb.catalog().get(cid) if kb.cfg.kb.catalog else None
    lines.append(f"  case:      {cid}" + (f"  {case.effective_name()}  → sbmig kb cases show {cid}" if case else ""))
    lines.append(f"  key (L2):  {meta.signature_generic or '-'}")
    lines.append(f"  lineage:   {lineage_text(chain)}" + ("" if not meta.parent or len(chain) > 1 else
                                                          f"  (parent v{meta.parent} missing)"))
    lines.append(f"  record:    {meta.successes} ok / {meta.failures} failed / {meta.partials} partial; fixed in "
                 f"{len(set(meta.success_projects))} project(s)"
                 + (f": {', '.join(meta.success_projects[:10])}" if meta.success_projects else ""))
    if meta.last_outcome or meta.last_at:
        lines.append(f"  last run:  {meta.last_outcome or '-'} at {_when(meta.last_at)} "
                     f"(run {meta.last_run_id or '-'})")
    lines.append(f"  made by:   {meta.source} {meta.model or '-'} in run {meta.run_id or '-'} "
                 f"({meta.project or '-'}), {_when(meta.created_at)}")
    reuse = ("reused first" if order and order[0] == meta.version else
             f"#{order.index(meta.version) + 1} of the reuse order" if meta.version in order else
             "not reused (" + ("quarantined" if meta.status == "quarantined" else meta.status if meta.status != "active"
                               else "no success yet") + ")")
    lines.append(f"  reuse:     {reuse}")
    lines.append(f"  sha1:      {meta.sha1 or '-'}")
    for n in meta.notes[:10]:
        lines.append(f"  note:      {_trim(n, 300)}")
    if meta.analysis:
        lines += ["", "analysis:"] + [f"  {ln}" for ln in meta.analysis.splitlines()]
    others = [m for m in metas if m.version != meta.version]
    if others:
        lines += ["", "other versions:"] + [f"  {_script_row(m)}" for m in reversed(others)]
    if code:
        lines += ["", f"--- {store.code_path(cid, meta.version).relative_to(kb.root).as_posix()}", text.rstrip("\n")]
    typer.echo("\n".join(lines))


def _set_script_status(ctx: typer.Context, key: str, version: int, status: str, reason: str) -> None:
    kb = _kb(ctx)
    store = kb.scripts
    cid = _script_case(store, key)
    command = f"sbmig kb scripts {'retire' if status == 'retired' else 'activate'}"
    try:
        meta = store.set_status(cid, version, status, reason=reason, command=command)   # type: ignore[arg-type]
    except (KeyError, ValueError) as e:
        typer.echo(str(e).strip("'\""), err=True)
        raise typer.Exit(1)
    typer.echo(f"{'retired' if status == 'retired' else 'activated'} {cid} v{meta.version}: {_script_row(meta)}")
    if status == "retired" and store.pinned_version(cid) == meta.version:
        typer.echo(f"note: case {cid} pins v{meta.version} in its human block — the pin is ignored while the "
                   f"version is retired (`sbmig kb cases unpin {cid}` clears it)", err=True)
    _refresh_script_case(kb, meta)


@scripts_cli.command("retire")
def scripts_retire_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                       version: int = typer.Argument(..., metavar="N"),
                       reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Never reuse (or augment from) this version again (recorded in kb/audit.jsonl)."""
    _set_script_status(ctx, key, version, "retired", reason)


@scripts_cli.command("activate")
def scripts_activate_cmd(ctx: typer.Context, key: str = typer.Argument(..., metavar="CASE_ID"),
                         version: int = typer.Argument(..., metavar="N"),
                         reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Make a retired version selectable again (a quarantined one stays quarantined)."""
    _set_script_status(ctx, key, version, "active", reason)


@scripts_cli.command("validate")
def scripts_validate_cmd(ctx: typer.Context) -> None:
    """Check the stored fix scripts: every meta parses and names its case / version, the code exists and matches
    its sha1, parents exist. Exit 1 when a problem is found."""
    store = _kb(ctx).scripts
    problems = store.check()
    for p in problems:
        typer.echo(f"PROBLEM: {p}")
    sc = store.counts()
    if problems:
        typer.echo(f"{len(problems)} problem(s) in {store.scripts_dir}")
        raise typer.Exit(1)
    typer.echo(f"OK: {sc['script_cases']} case(s), {sc['script_versions']} version(s) in {store.scripts_dir}")
