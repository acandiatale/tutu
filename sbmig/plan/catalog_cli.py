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
WorkDirOpt = typer.Option(None, "--work-dir", help="with --jars: generated poms, cp.txt and Maven logs "
                          "(default: out/recipes-check under the repo root)")
ArtifactOpt = typer.Option(None, "--artifact", help="with --jars: g:a to resolve instead of "
                           "openrewrite.recipe_artifacts (repeatable)")


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
    return row.get("artifact") or ("(core)" if row.get("origin") == "openrewrite" else "-")


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
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """Every catalog recipe (origin, phase, artifact, gate, advisor, sets)."""
    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    rows = cat.rows()
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return
    cols: list[tuple[str, Any]] = [
        ("id", lambda r: r["id"]),
        ("origin", lambda r: r["origin"]),
        ("phase", lambda r: r.get("phase") or "-"),
        ("artifact", _where),
        ("gate", lambda r: r.get("gate") or "-"),
        ("advisor", lambda r: "yes" if r.get("advisor") else "-"),
        ("sets", lambda r: ",".join(r.get("sets") or []) or "-"),
    ]
    for line in _table(rows, cols):
        typer.echo(line)
    typer.echo(f"\n{len(cat)} catalog recipe(s) — {cat.path}")


@recipes_cli.command("show")
def show_cmd(
    recipe_id: str = typer.Argument(..., help="recipe id"),
    config: Optional[List[Path]] = ConfigOpt,
    catalog: Optional[Path] = CatalogOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """One recipe: catalog entry and the config sets / composites that reference it."""
    from .catalog import config_recipe_sets

    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    entry = cat.get(recipe_id)
    if entry is None:
        typer.echo(f"unknown recipe {recipe_id} (not in the catalog)", err=True)
        raise typer.Exit(1)
    out: dict[str, Any] = {"id": recipe_id, "catalog": entry.model_dump()}
    out["referenced_by"] = {
        "config_sets": [n for n, ids in config_recipe_sets(cfg).items() if recipe_id in ids],
        "covered_by": [e.id for e in cat.entries if recipe_id in e.covers],
    }
    if as_json:
        typer.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return
    typer.echo(recipe_id)
    for k, v in entry.model_dump().items():
        if k != "id" and v not in (None, [], ""):
            typer.echo(f"  {k}: {', '.join(v) if isinstance(v, list) else v}")
    for k, v in out["referenced_by"].items():
        typer.echo(f"  referenced_by.{k}: {', '.join(v) or '-'}")


@recipes_cli.command("check")
def check_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    catalog: Optional[Path] = CatalogOpt,
    jars: bool = typer.Option(False, "--jars", help="also verify every openrewrite id against the real recipe jars "
                              "(Maven + network; resolves openrewrite.recipe_artifacts at the BOM version)"),
    work_dir: Path | None = WorkDirOpt,
    as_json: bool = typer.Option(False, "--json", help="with --jars: print the jar check report as JSON"),
    artifact: list[str] | None = ArtifactOpt,
) -> None:
    """Cross-check the catalog against the config recipe sets and the recipe artifacts.

    With --jars, also resolve the configured recipe artifacts with Maven and check every openrewrite id
    against the jars that declare it (artifact, provided_by; see docs/RECIPES.md)."""
    cfg = _load(config, catalog)
    cat = _catalog(cfg)
    problems = cat.check(cfg)
    if jars and as_json and problems:
        typer.echo(json.dumps({"offline_problems": problems}, indent=2, ensure_ascii=False))
        raise typer.Exit(1)
    for p in problems:
        typer.echo(f"PROBLEM {p}")
    if problems:
        typer.echo(f"{len(problems)} problem(s) in {cat.path}", err=True)
        raise typer.Exit(1)
    if not (jars and as_json):
        typer.echo(f"recipe catalog OK: {len(cat)} recipe(s), {len(cat.advisor_catalog())} advisor-eligible — "
                   f"{cat.path}")
    if jars:
        _check_jars(cat, cfg, work_dir, artifact or None, as_json)


def _check_jars(cat, cfg, work_dir: Path | None, artifacts: list[str] | None, as_json: bool) -> None:
    from .jarcheck import check_jars

    report = check_jars(cat, cfg, work_dir=work_dir, artifacts=artifacts)
    if as_json:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        def _pb(r) -> str:
            if not r.provided_by:
                return "-"
            state = {True: "ok", False: "FAIL", None: "?"}[r.provided_by_ok]
            return f"{state} via {r.provided_by.split(':', 1)[-1]}"

        rows = [r.model_dump() | {"_row": r} for r in report.rows]
        cols: list[tuple[str, Any]] = [
            ("id", lambda d: d["id"]),
            ("artifact", lambda d: (d["artifact"] or "(core)").replace("org.openrewrite.recipe:", "")),
            ("version", lambda d: d["version"] or "-"),
            ("found_in", lambda d: d["found_in"] or "NOT FOUND"),
            ("provided_by", lambda d: _pb(d["_row"])),
        ]
        for line in _table(rows, cols):
            typer.echo(line)
        typer.echo("")
        for p in report.problems:
            typer.echo(f"PROBLEM {p}")
    if report.problems:
        typer.echo(f"{len(report.problems)} jar check problem(s) (rewrite-recipe-bom {report.bom_version}; "
                   f"poms and logs under {report.work_dir})", err=True)
        raise typer.Exit(1)
    if not as_json:
        found = sum(1 for r in report.rows if r.found_in)
        typer.echo(f"recipe jars OK: {found} recipe(s) found in {len(report.declaring_jars)} jar(s) "
                   f"(rewrite-recipe-bom {report.bom_version}; {report.jars_scanned} jar(s) scanned from "
                   f"{len(report.classpaths)} artifact classpath(s)) — {report.work_dir}")


@recipes_cli.command("sets")
def sets_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """The config recipe sets and, per case, the one transform run the v2 planner resolves from them."""
    from sbmig.core.models import CaseType

    from .catalog import config_recipe_sets
    from .planner import _Facts, transform_recipes

    cfg = _load(config)
    lo = ({}, set())
    hi = ({"javax.servlet": 1}, {"junit4", "spring-security", "javax-annotation"})
    resolved: dict[str, dict[str, list[str]]] = {}
    for case in (CaseType.PURE_JAVA, CaseType.SERVLET_TOMCAT, CaseType.SPRING_LEGACY, CaseType.SPRING_BOOT):
        base = [r.id for r in transform_recipes(_Facts(case, lo[1], lo[0]), cfg, [])]
        full = [r.id for r in transform_recipes(_Facts(case, hi[1], hi[0]), cfg, [])]
        resolved[case.value] = {"transform": base, "transform_conditional": [i for i in full if i not in base]}
    if as_json:
        typer.echo(json.dumps({"sets": config_recipe_sets(cfg), "per_case": resolved}, indent=2))
        return
    typer.echo("config sets (openrewrite.recipes):")
    for name, ids in config_recipe_sets(cfg).items():
        typer.echo(f"  {name}: {', '.join(ids) or '-'}")
    typer.echo("\nper case (transform.rewrite — one OpenRewrite run before the restructure):")
    for case, d in resolved.items():
        typer.echo(f"  {case}: {', '.join(d['transform']) or '-'}")
        if d["transform_conditional"]:
            typer.echo(f"    + when detected (EE javax imports / junit4 / spring-security): "
                       f"{', '.join(d['transform_conditional'])}")
    typer.echo("\nmsa: one run per library root / service with its own case (library roots never get the Boot set); "
               "top-aggregator: one run at the root with the union.")
