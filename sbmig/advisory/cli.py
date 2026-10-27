"""`sbmig advisories …` — typer sub-app over knowledge/libraries/advisories.yml (+ advisories.extra_files).

See docs/ADVISORIES.md."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

import typer

advisories_cli = typer.Typer(help="Library advisories: list | show | check (knowledge/libraries/advisories.yml).",
                             no_args_is_help=True)

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")


def _load(configs: Optional[List[Path]]):
    from sbmig.plan.catalog_cli import _load as load_cfg     # same placeholder-artifacts convention

    return load_cfg(configs)


def _state(entry: Any, cfg: Any) -> str:
    """"off" when `advisories.policy_overrides` switches the entry off, else "guide" (LLM hints + Handover)."""
    from .evaluate import switched_off

    return "off" if switched_off(entry, cfg) else "guide"


_DROPPED_FIELDS = ("policy", "recipes")      # still on the core model; guide-only advisories do not use them


def _row(entry: Any, cfg: Any) -> dict[str, Any]:
    data = {k: v for k, v in entry.model_dump(mode="json").items() if k not in _DROPPED_FIELDS}
    return {**data, "state": _state(entry, cfg)}


def _table(rows: list[list[str]], heads: list[str]) -> list[str]:
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(heads)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*heads).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip(),
            *[fmt.format(*r).rstrip() for r in rows]]


@advisories_cli.command("list")
def list_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """Every advisory: id, library, status, state (guide | off), replacement, notes file."""
    from .catalog import load_advisories

    cfg = _load(config)
    cat = load_advisories(cfg)
    rows = [_row(e, cfg) for e in cat.entries]
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    table = [[r["id"], r.get("library") or ",".join(r.get("artifacts") or []) or "-", r["status"], r["state"],
              r.get("replacement") or "-", r.get("notes") or "-"] for r in rows]
    for line in _table(table, ["id", "library", "status", "state", "replacement", "notes"]):
        typer.echo(line)
    typer.echo(f"\n{len(cat)} advisory(ies) — " + (", ".join(str(s) for s in cat.sources) or "no advisory file"))


@advisories_cli.command("show")
def show_cmd(
    advisory_id: str = typer.Argument(..., help="advisory id"),
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """One advisory (state guide | off)."""
    from .catalog import load_advisories

    cfg = _load(config)
    entry = load_advisories(cfg).get(advisory_id)
    if entry is None:
        typer.echo(f"unknown advisory {advisory_id}", err=True)
        raise typer.Exit(1)
    data = _row(entry, cfg)
    if as_json:
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return
    typer.echo(advisory_id)
    for k, v in data.items():
        if k != "id" and v not in (None, [], ""):
            typer.echo(f"  {k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")


@advisories_cli.command("check")
def check_cmd(config: Optional[List[Path]] = ConfigOpt) -> None:
    """Validate the advisory files: analyzer library names, artifact patterns, notes files (exit 1 on problems)."""
    from .catalog import load_advisories

    cfg = _load(config)
    cat = load_advisories(cfg)
    problems = cat.check(cfg)
    for p in problems:
        typer.echo(f"PROBLEM {p}")
    if problems:
        typer.echo(f"{len(problems)} problem(s)", err=True)
        raise typer.Exit(1)
    typer.echo(f"advisories OK: {len(cat)} advisory(ies), guide-only — "
               + (", ".join(str(s) for s in cat.sources) or "no advisory file"))
