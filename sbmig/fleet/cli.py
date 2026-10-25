"""`sbmig fleet …` — one agent, many projects, strictly sequential, KB fed between projects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

fleet_cli = typer.Typer(
    help="Sequential multi-project migration with cross-project KB learning.", no_args_is_help=True
)

RUN_CONFIG = "fleet-config.json"  # <out>/fleet/fleet-config.json: configs + overrides of the last `run`


def _overrides(
    order: Optional[str],
    max_projects: Optional[int],
    retry_failed: Optional[bool],
    stop_on_abort: Optional[bool],
    llm_provider: Optional[str],
    max_llm_calls_total: Optional[int],
) -> dict:
    ov: dict = {}
    fleet: dict = {}
    if order:
        if order not in ("manifest", "simple-first"):
            raise typer.BadParameter("--order must be 'manifest' or 'simple-first'")
        fleet["order"] = order
    if max_projects is not None:
        fleet["max_projects"] = max_projects
    if retry_failed is not None:
        fleet["retry_failed"] = retry_failed
    if stop_on_abort is not None:
        fleet["stop_on_abort"] = stop_on_abort
    if max_llm_calls_total is not None:
        fleet["max_llm_calls_total"] = max_llm_calls_total
    if fleet:
        ov["fleet"] = fleet
    if llm_provider:
        ov["llm"] = {"provider": llm_provider}
    return ov


def _run(manifest: Path, out: Path, configs: list[Path], overrides: dict, dry_run: bool) -> int:
    from pydantic import ValidationError

    from sbmig.core.config import load_config

    from .runner import FleetRunner

    saved = out / "fleet" / RUN_CONFIG
    try:
        cfg = load_config(*configs, overrides=overrides or None)
    except ValidationError as e:
        typer.secho(f"invalid configuration: {e}", fg=typer.colors.RED, err=True)
        return 1
    runner = FleetRunner(cfg, manifest, out, echo=typer.echo, dry_run=dry_run)
    fleet_log: Optional[Path] = None
    if not dry_run:
        try:
            import logging

            from sbmig.cli import attach_file_log
            # INFO for the whole fleet (500 projects of DEBUG would not fit); each project logs DEBUG into its own
            # <run dir>/sbmig.log (FleetRunner attaches and detaches it per project)
            fleet_log = attach_file_log(Path(runner.fleet_dir) / "sbmig.log", level=logging.INFO)
        except Exception:  # pragma: no cover - logging must never stop a fleet
            pass
    try:
        return _run_fleet(runner, saved, manifest, configs, overrides, dry_run, cfg)
    finally:
        if fleet_log is not None:
            from sbmig.cli import detach_file_log

            detach_file_log(fleet_log)


def _run_fleet(runner, saved: Path, manifest: Path, configs: list[Path], overrides: dict, dry_run: bool, cfg) -> int:
    from sbmig.kb.store import atomic_write

    from .summary import render_table

    if not dry_run:
        atomic_write(
            saved,
            json.dumps(
                {
                    "manifest": str(manifest.resolve()),
                    "configs": [str(Path(c).resolve()) for c in configs],
                    "overrides": overrides,
                },
                indent=2,
            )
            + "\n",
        )
    try:
        state = runner.run()
    except Exception as e:  # manifest errors etc.
        typer.secho(f"fleet failed: {type(e).__name__}: {e}", fg=typer.colors.RED, err=True)
        return 1
    if dry_run:
        typer.echo(f"fleet {state.fleet_id} (dry run, order: {cfg.fleet.order}) - nothing executed:")
        for idx, r, action in runner.planned:
            typer.echo(
                f"  {idx:3d}. {r.name:<30} {r.status:<10} -> {action}" + (f"  ({r.error})" if r.error else "")
            )
        return 0
    typer.echo("")
    typer.echo(render_table(state))
    typer.echo("")
    typer.echo(f"summary: {runner.fleet_dir / 'summary.md'}   learning: {runner.fleet_dir / 'learning.md'}")
    if runner.stopped_on_infra:          # the reason once more, as the last line on the screen
        typer.echo(next((n for n in reversed(state.notes) if n.startswith("fleet stopped: ")), ""))
    return runner.exit_code


@fleet_cli.command("run")
def run_cmd(
    manifest: Path = typer.Argument(
        ..., exists=True, help="directory | projects.txt | fleet.yml | projects.csv"
    ),
    out: Path = typer.Option(Path("out"), "--out", "-o", help="output dir (runs + fleet/state.json)"),
    config: Optional[List[Path]] = typer.Option(None, "--config", "-c", help="extra config yml (repeatable)"),
    order: Optional[str] = typer.Option(None, "--order", help="manifest | simple-first"),
    max_projects: Optional[int] = typer.Option(None, "--max-projects", help="run at most N projects now"),
    retry_failed: Optional[bool] = typer.Option(
        None, "--retry-failed/--no-retry-failed", help="re-run incomplete/aborted projects"
    ),
    stop_on_abort: Optional[bool] = typer.Option(None, "--stop-on-abort/--no-stop-on-abort"),
    max_llm_calls_total: Optional[int] = typer.Option(
        None, "--max-llm-calls-total", help="fleet-wide LLM budget (0 = unlimited)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the order / resume decisions, run nothing"),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider"),
) -> None:
    """Migrate every project of the manifest, one after the other, with one shared KB.

    Resumable by default: re-running with the same --out skips projects that already succeeded (and, unless
    --retry-failed, the incomplete/aborted ones). Exit 0 = all projects succeeded, 2 = some incomplete /
    skipped / not run yet, 3 = stopped on an aborted project (--stop-on-abort) or by the infrastructure guard
    ("fleet stopped: infrastructure <kind> unavailable since <project> …": fix it and run the same command again)."""
    ov = _overrides(order, max_projects, retry_failed, stop_on_abort, llm_provider, max_llm_calls_total)
    raise typer.Exit(_run(manifest, out, list(config or []), ov, dry_run))


@fleet_cli.command("resume")
def resume_cmd(
    out: Path = typer.Argument(
        ..., exists=True, file_okay=False, help="the --out dir of a previous `fleet run`"
    ),
    config: Optional[List[Path]] = typer.Option(
        None, "--config", "-c", help="config yml (default: the ones of the previous run)"
    ),
    max_projects: Optional[int] = typer.Option(None, "--max-projects"),
    retry_failed: Optional[bool] = typer.Option(None, "--retry-failed/--no-retry-failed"),
    stop_on_abort: Optional[bool] = typer.Option(None, "--stop-on-abort/--no-stop-on-abort"),
) -> None:
    """Continue a fleet (= `fleet run <same manifest> --out <out>` with the same configs)."""
    saved = out / "fleet" / RUN_CONFIG
    state_path = out / "fleet" / "state.json"
    if not state_path.exists():
        typer.secho(f"no fleet state in {out}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    prev = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    manifest = Path(prev.get("manifest") or json.loads(state_path.read_text(encoding="utf-8"))["manifest"])
    configs = list(config) if config else [Path(c) for c in prev.get("configs") or []]
    from sbmig.core.config import deep_merge

    ov = deep_merge(
        prev.get("overrides") or {}, _overrides(None, max_projects, retry_failed, stop_on_abort, None, None)
    )
    raise typer.Exit(_run(manifest, out, configs, ov, False))


@fleet_cli.command("status")
def status_cmd(
    out: Path = typer.Argument(Path("out"), help="the --out dir of a fleet run"),
    as_json: bool = typer.Option(False, "--json"),
    attention: bool = typer.Option(False, "--attention",
                                   help="only the projects that need a human, grouped by attention code "
                                        "(infra:<kind>, blocked:<reason>, unresolved, restructure-failed, ...)"),
) -> None:
    """Print the fleet's progress table from <out>/fleet/state.json."""
    from sbmig.core.models import FleetState

    from .summary import render_attention, render_table, totals

    p = out / "fleet" / "state.json"
    if not p.exists():
        typer.secho(f"no fleet state in {out}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    state = FleetState.model_validate_json(p.read_text(encoding="utf-8"))
    if as_json:
        typer.echo(state.model_dump_json(indent=2))
        return
    if attention:
        for line in render_attention(state):
            typer.echo(line)
        return
    t = totals(state)
    typer.echo(
        f"fleet {state.fleet_id}: {t['done']} of {t['projects']} done "
        f"({', '.join(f'{k} {v}' for k, v in sorted(t['by_status'].items()))}); "
        f"llm calls {t['llm_calls']}; kb used {t['kb_used']}, candidates {t['kb_candidates_created']}, "
        f"promoted {t['kb_recipes_promoted']}" + (f"; still running: {t['running']}" if t["running"] else "")
    )
    typer.echo(render_table(state))
    for n in state.notes:
        typer.echo(f"note: {n}")
