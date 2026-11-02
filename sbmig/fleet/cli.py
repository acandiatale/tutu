"""``sbmig fleet …`` 명령(run / resume / status) — 일괄 실행을 운영자가 다루는 창구.
흐름상 위치: ``sbmig/cli.py`` 가 이 typer 앱(``fleet_cli``)을 ``sbmig fleet`` 으로 붙인다. ``run`` /
``resume`` 은 설정을 읽어 :class:`~sbmig.fleet.runner.FleetRunner` 를 돌리고, ``status`` 는
``<out>/fleet/state.json`` 만 읽는다(실행 중에도 다른 창에서 안전하게 볼 수 있다). 명령 함수의 docstring 이 곧
``--help`` 문구다(typer): 첫 줄 한국어가 운영자가 보는 명령 요약이 된다. 그래서 그 docstring 에는 대괄호를
쓰지 않는다(typer 의 rich 마크업으로 읽혀 사라진다). ``run`` 은 매니페스트·설정·오버라이드를
``fleet/fleet-config.json`` 에 남기고, ``resume`` 은 그것을 읽어 같은 조건으로 이어 간다.

`sbmig fleet …` — one agent, many projects, strictly sequential, KB fed between projects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

fleet_cli = typer.Typer(
    help="Sequential multi-project migration; one shared KB (error cases, fix scripts reused across projects).",
    no_args_is_help=True
)

# 재개(``fleet resume``)가 읽는 파일: 마지막 ``run`` 의 매니페스트·설정 경로·오버라이드
RUN_CONFIG = "fleet-config.json"  # <out>/fleet/fleet-config.json: configs + overrides of the last `run`


def _overrides(
    order: Optional[str],
    max_projects: Optional[int],
    retry_failed: Optional[bool],
    stop_on_abort: Optional[bool],
    llm_provider: Optional[str],
    max_llm_calls_total: Optional[int],
) -> dict:
    """준 CLI 옵션만 설정 오버라이드 dict 로 만든다(``fleet.*``, ``llm.provider``).
    안 준 옵션은 설정 파일 값이 그대로 남는다."""
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
    """설정을 읽어 :class:`FleetRunner` 를 만들고 돌린다(종료 코드를 돌려줌). 설정이 틀리면 1.
    실제 실행이면 ``fleet/sbmig.log`` (INFO) 를 붙였다가 끝나면 반드시 뗀다."""
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
            # 일괄 실행 로그는 INFO 만: 프로젝트 500 개의 DEBUG 는 한 파일에 담을 수 없다. 프로젝트별 DEBUG 는
            # 러너가 프로젝트마다 붙였다 떼는 각 실행 폴더의 sbmig.log 로 간다
            # INFO for the whole fleet (500 projects of DEBUG would not fit); each project logs DEBUG into its own
            # <run dir>/sbmig.log (FleetRunner attaches and detaches it per project)
            fleet_log = attach_file_log(Path(runner.fleet_dir) / "sbmig.log", level=logging.INFO)
        except Exception:  # pragma: no cover - logging must never stop a fleet
            # 로그 파일을 못 붙여도(권한·잠긴 파일) 일괄 실행은 멈추지 않는다
            pass
    try:
        return _run_fleet(runner, saved, manifest, configs, overrides, dry_run, cfg)
    finally:
        if fleet_log is not None:
            from sbmig.cli import detach_file_log

            detach_file_log(fleet_log)


def _run_fleet(runner, saved: Path, manifest: Path, configs: list[Path], overrides: dict, dry_run: bool, cfg) -> int:
    """재개용 ``fleet-config.json`` 을 먼저 적고 러너를 돌린 뒤, 진행 표와 요약 파일 경로를 찍는다.
    ``--dry-run`` 은 순서와 프로젝트별 결정(run / retry / keep / skip / done)만 찍고
    아무것도 실행하지 않는다."""
    from sbmig.kb.store import atomic_write

    from .summary import render_table

    # 실행 전에 적는다: 도중에 프로세스가 죽어도 ``fleet resume <out>`` 이 같은 매니페스트·설정으로 이어 간다
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
    # 인프라 가드로 멈췄으면 그 한 줄(``fleet stopped: …``)을 화면 맨 끝에 한 번 더: 운영자가 옮겨 적는 줄이다
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
    """매니페스트의 모든 프로젝트를 KB 하나를 공유하며 하나씩 차례로 이관한다(기본은 이어서 실행).

    같은 --out 으로 다시 돌리면 이미 성공한 프로젝트는 건너뛴다 (--retry-failed 가 없으면 미완료·중단
    프로젝트도 그대로 둔다). 종료 코드 0 = 전부 성공, 2 = 미완료·건너뜀·아직 안 돈 프로젝트 있음, 3 = 중단
    프로젝트(--stop-on-abort) 또는 인프라 가드로 멈춤("fleet stopped: infrastructure …" 줄):
    원인을 고치고 같은 명령을 다시 돌린다.

    Migrate every project of the manifest, one after the other, with one shared KB.

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
    """앞선 fleet run 을 같은 매니페스트·설정으로 멈춘 곳부터 이어서 돌린다.

    fleet/fleet-config.json 에 남은 매니페스트·설정·오버라이드를 다시 쓰고, 여기서 준 옵션만 그 위에 덮는다
    (-c 를 주면 이전 설정 대신 그것을 쓴다). 강제 종료·재부팅·인프라 정지 뒤에 쓴다.

    Continue a fleet (= `fleet run <same manifest> --out <out>` with the same configs)."""
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
    """일괄 실행 진행 표를 <out>/fleet/state.json 에서 읽어 찍는다(실행 중에도 다른 창에서 볼 수 있다).

    --json 은 상태 전체를, --attention 은 사람이 봐야 할 프로젝트만 주의 코드별로 묶어 보여 준다.

    Print the fleet's progress table from <out>/fleet/state.json."""
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
        f"llm calls {t['llm_calls']} (cost {t['llm_cost']:.4f}); errors open {t['errors_open']}, resolved "
        f"{t['errors_resolved']}, blocked {t['errors_blocked']}; blocked items {t['blocked']}"
        + (f"; still running: {t['running']}" if t["running"] else "")
    )
    typer.echo(render_table(state))
    for n in state.notes:
        typer.echo(f"note: {n}")
