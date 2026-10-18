"""`sbmig kb …` — typer sub-app for inspecting and curating the file-based knowledge base."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError

from sbmig.core.config import Config, load_config
from sbmig.core.models import BuildError, CaseType, ErrorCluster, ErrorKind, ProjectProfile
from sbmig.core.workspace import Workspace

from .recipes import validate_recipe_yaml
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
    try:
        r = _kb(ctx).promote(recipe_id)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"promoted {r.id} -> {r.path}")


@kb_cli.command("reject")
def reject_cmd(ctx: typer.Context, recipe_id: str = typer.Argument(..., metavar="ID"),
               reason: str = typer.Option("", "--reason", "-r")) -> None:
    """Move a recipe to rejected."""
    try:
        r = _kb(ctx).reject(recipe_id, reason)
    except KeyError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"rejected {r.id}")


@kb_cli.command("reindex")
def reindex_cmd(ctx: typer.Context) -> None:
    """Rebuild index.json and symbols.json from the case and recipe files."""
    n = _kb(ctx).reindex()
    typer.echo(f"indexed {n} cases")


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
        help="regex: handler|plan_recipe|kb_recipe|kb_patch|llm_patch|blocked|unresolved"),
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


@kb_cli.command("export")
def export_cmd(ctx: typer.Context, out: Path = typer.Argument(..., metavar="OUT.zip")) -> None:
    """Export the KB as a zip (to move it between machines)."""
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
