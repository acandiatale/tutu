"""``sbmig advisories …`` 명령(list | show | check) — 권고 파일을 보고 검사하는 typer 하위 앱.
권고 파일은 knowledge/libraries/advisories.yml + ``advisories.extra_files``. 실행과 무관한 읽기 전용 도구다
(state 는 guide 또는 off 뿐, block 은 없다). 주의: typer 는 명령 함수의 docstring 을 ``--help`` 화면으로 쓴다
— 한국어 첫 줄이 명령 목록의 요약이 된다 (``sbmig --help``, ``sbmig kb --help`` 와 같은 방식).

`sbmig advisories …` — typer sub-app over knowledge/libraries/advisories.yml (+ advisories.extra_files).

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
    """설정 읽기 — ``sbmig recipes`` 와 같은 규칙(사내 좌표 자리표시 허용)을 쓴다."""
    from sbmig.plan.catalog_cli import _load as load_cfg     # same placeholder-artifacts convention

    return load_cfg(configs)


def _state(entry: Any, cfg: Any) -> str:
    """항목 상태: ``advisories.policy_overrides`` 로 꺼졌으면 "off", 아니면 "guide"(LLM 힌트 + 인수인계).

    "off" when `advisories.policy_overrides` switches the entry off, else "guide" (LLM hints + Handover)."""
    from .evaluate import switched_off

    return "off" if switched_off(entry, cfg) else "guide"


_DROPPED_FIELDS = ("policy", "recipes")      # still on the core model; guide-only advisories do not use them


def _row(entry: Any, cfg: Any) -> dict[str, Any]:
    """출력용 한 줄 dict(안내 전용 권고가 쓰지 않는 ``policy`` / ``recipes`` 는 뺀다) + state."""
    data = {k: v for k, v in entry.model_dump(mode="json").items() if k not in _DROPPED_FIELDS}
    return {**data, "state": _state(entry, cfg)}


def _table(rows: list[list[str]], heads: list[str]) -> list[str]:
    """고정폭 텍스트 표(머리, 구분선, 줄들)."""
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(heads)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*heads).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip(),
            *[fmt.format(*r).rstrip() for r in rows]]


@advisories_cli.command("list")
def list_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """모든 권고를 표로(id, 라이브러리, 상태, guide | off, 대체품, 노트 파일). --json 이면 JSON.

    Every advisory: id, library, status, state (guide | off), replacement, notes file."""
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
    """권고 하나의 모든 필드(state guide | off). 모르는 id 면 exit 1.

    One advisory (state guide | off)."""
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
    """권고 파일 검사(AdvisoryCatalog.check): 분석기 라이브러리
    이름, 아티팩트 패턴, 노트 파일. 문제가 있으면 exit 1.

    Validate the advisory files: analyzer library names, artifact patterns, notes files (exit 1 on problems)."""
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
