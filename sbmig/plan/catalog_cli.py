"""`sbmig recipes …` — typer sub-app over the recipe catalog (knowledge/recipes/catalog.yml). See docs/RECIPES.md."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

import typer

recipes_cli = typer.Typer(help="Recipe catalog: list | show | check | sets (knowledge/recipes/catalog.yml).",
                          no_args_is_help=True)

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")
CatalogOpt = typer.Option(None, "--catalog", help="catalog file (default: plan.catalog_file from the config)")


def _load(configs: Optional[List[Path]], catalog: Optional[Path] = None):
    """Recipe commands do not need the company artifacts section; use a placeholder when no config provides it."""
    from pydantic import ValidationError

    from sbmig.core.config import load_config

    overrides: dict[str, Any] = {"plan": {"catalog_file": str(catalog.resolve())}} if catalog else {}
    try:
        return load_config(*(configs or []), overrides=overrides or None)
    except ValidationError as e:
        if not any(err.get("loc") == ("artifacts",) for err in e.errors()):
            raise
        overrides["artifacts"] = {"parent": {"group_id": "unset", "artifact_id": "unset"}}
        return load_config(*(configs or []), overrides=overrides)


def _catalog(cfg):
    from .catalog import RecipeCatalog

    path = cfg.recipe_catalog_file
    if not path.exists():
        typer.echo(f"recipe catalog not found: {path}", err=True)
        raise typer.Exit(2)
    return RecipeCatalog.load(path)


def _where(row: dict) -> str:
    return row.get("artifact") or row.get("file") or ("(core)" if row.get("origin") == "openrewrite" else "-")


def _table(rows: list[dict], cols: list[tuple[str, Any]]) -> list[str]:
    cells = [[str(fn(r)) for _, fn in cols] for r in rows]
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, (h, _) in enumerate(cols)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*[h for h, _ in cols]).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip(),
            *[fmt.format(*c).rstrip() for c in cells]]


@recipes_cli.command("list")
def list_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    catalog: Optional[Path] = CatalogOpt,
    kb: bool = typer.Option(False, "--kb", help="also list live KB recipes (status, credits)"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """Every catalog recipe (origin, phase, artifact/file, gate, advisor, sets, rules)."""
    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    if kb:
        from sbmig.kb import FileKnowledgeBase

        rows = cat.with_kb(FileKnowledgeBase(cfg))
    else:
        rows = cat.rows()
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return
    cols: list[tuple[str, Any]] = [
        ("id", lambda r: r["id"]),
        ("origin", lambda r: r["origin"] if r["origin"] != "kb" else f"kb:{r.get('status')}"),
        ("phase", lambda r: r.get("phase") or "-"),
        ("artifact/file", _where),
        ("gate", lambda r: r.get("gate") or "-"),
        ("advisor", lambda r: "yes" if r.get("advisor") else "-"),
        ("sets", lambda r: ",".join(r.get("sets") or []) or "-"),
        ("rules", lambda r: ",".join(r.get("rules") or []) or "-"),
    ]
    if kb:
        cols.append(("credits", lambda r: f"+{r['success_count']}/-{r['fail_count']} ({r['projects']}p)"
                     if r.get("origin") == "kb" else "-"))
    for line in _table(rows, cols):
        typer.echo(line)
    n_kb = sum(1 for r in rows if r.get("origin") == "kb")
    typer.echo(f"\n{len(cat)} catalog recipe(s){f', {n_kb} kb recipe(s)' if kb else ''} — {cat.path}")


@recipes_cli.command("show")
def show_cmd(
    recipe_id: str = typer.Argument(..., help="recipe id"),
    config: Optional[List[Path]] = ConfigOpt,
    catalog: Optional[Path] = CatalogOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """One recipe: catalog entry, the config sets / reactive rules / composites that reference it, KB meta."""
    from .catalog import base_recipes, config_recipe_sets, reactive_rules

    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    entry = cat.get(recipe_id)
    out: dict[str, Any] = {"id": recipe_id, "catalog": entry.model_dump() if entry else None}
    out["referenced_by"] = {
        "config_sets": [n for n, ids in config_recipe_sets(cfg).items() if recipe_id in ids],
        "rules": [r for r, ids in reactive_rules().items() if recipe_id in ids],
        "covered_by": [e.id for e in cat.entries if recipe_id in e.covers],
        "project_recipes": [n for n, info in base_recipes(cfg.base_recipe_dir).items() if recipe_id in info["uses"]],
    }
    kb_meta = None
    try:
        from sbmig.kb import FileKnowledgeBase

        kbs = FileKnowledgeBase(cfg)
        r = kbs.get_recipe(recipe_id)
        if r is not None:
            kb_meta = {**r.model_dump(mode="json"), **kbs.recipe_meta_extra(r.id)}
    except Exception as e:  # KB is optional for `show`
        kb_meta = {"error": f"{type(e).__name__}: {e}"}
    out["kb"] = kb_meta
    if entry is None and not (kb_meta and "id" in kb_meta):
        typer.echo(f"unknown recipe {recipe_id} (not in the catalog nor in the KB)", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return
    typer.echo(recipe_id)
    if entry is not None:
        for k, v in entry.model_dump().items():
            if k != "id" and v not in (None, [], ""):
                typer.echo(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
    else:
        typer.echo("  (not in the catalog)")
    for k, v in out["referenced_by"].items():
        typer.echo(f"  referenced_by.{k}: {', '.join(v) or '-'}")
    if kb_meta:
        typer.echo("  kb:")
        for k in ("status", "path", "description", "success_count", "fail_count", "projects", "signatures",
                  "promoted_at", "error"):
            if k in kb_meta and kb_meta[k] not in (None, [], ""):
                typer.echo(f"    {k}: {kb_meta[k]}")


@recipes_cli.command("check")
def check_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    catalog: Optional[Path] = CatalogOpt,
) -> None:
    """Cross-check the catalog against config sets, reactive rules, base recipes and recipe artifacts."""
    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    problems = cat.check(cfg)
    for p in problems:
        typer.echo(f"PROBLEM {p}")
    if problems:
        typer.echo(f"{len(problems)} problem(s) in {cat.path}", err=True)
        raise typer.Exit(1)
    typer.echo(f"recipe catalog OK: {len(cat)} recipe(s), {len(cat.advisor_catalog())} advisor-eligible — {cat.path}")


@recipes_cli.command("sets")
def sets_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """The config recipe sets per case, as the rule-based planner resolves them."""
    from sbmig.core.models import CaseType

    from .catalog import config_recipe_sets
    from .planner import _Facts, spring_recipes

    cfg = _load(config)
    rs = cfg.openrewrite.recipes
    lo = ({}, set())
    hi = ({"javax.servlet": 1}, {"junit4", "spring-security", "javax-annotation"})
    resolved: dict[str, dict[str, list[str]]] = {}
    for case in (CaseType.PURE_JAVA, CaseType.SERVLET_TOMCAT, CaseType.SPRING_LEGACY, CaseType.SPRING_BOOT):
        base = [r.id for r in spring_recipes(_Facts(case, lo[1], lo[0]), cfg, [])]
        full = [r.id for r in spring_recipes(_Facts(case, hi[1], hi[0]), cfg, [])]
        resolved[case.value] = {"jdk21": list(rs.jdk21), "spring": base,
                                "spring_conditional": [i for i in full if i not in base]}
    if as_json:
        typer.echo(json.dumps({"sets": config_recipe_sets(cfg), "per_case": resolved}, indent=2))
        return
    typer.echo("config sets (openrewrite.recipes):")
    for name, ids in config_recipe_sets(cfg).items():
        typer.echo(f"  {name}: {', '.join(ids) or '-'}")
    typer.echo("\nper case (jdk21.rewrite / spring.rewrite):")
    for case, d in resolved.items():
        typer.echo(f"  {case}:")
        typer.echo(f"    jdk21:  {', '.join(d['jdk21']) or '-'}")
        typer.echo(f"    spring: {', '.join(d['spring']) or '-'}")
        if d["spring_conditional"]:
            typer.echo(f"    + when detected (EE javax imports / junit4 / spring-security): "
                       f"{', '.join(d['spring_conditional'])}")
    typer.echo("\nmsa: per service, using the service's own case; KB active recipes (and candidates when kb.try_candidates_in_plan) are appended.")
