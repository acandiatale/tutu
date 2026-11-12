"""권고 파일(knowledge/libraries/advisories.yml과 ``advisories.extra_files``)을 보여 주고 검사하는
``sbmig advisories …`` 명령(list | show | check) 모듈.
이관 실행과 무관한 읽기 전용 도구이며 항목 상태는 guide 또는 off뿐이다.
typer는 명령 함수의 docstring을 ``--help`` 화면으로 쓰므로 한국어 첫 줄이 명령 목록의 요약이 된다.

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
    """``sbmig recipes``와 같은 규칙으로 설정을 읽는다. 사내 좌표는 플레이스홀더를 허용한다."""
    from sbmig.plan.catalog_cli import _load as load_cfg     # same placeholder-artifacts convention

    return load_cfg(configs)


def _state(entry: Any, cfg: Any) -> str:
    """항목 상태를 반환한다.
    ``advisories.policy_overrides``로 꺼졌으면 "off", 아니면 "guide"(LLM 힌트와 인수인계)다.

    "off" when `advisories.policy_overrides` switches the entry off, else "guide" (LLM hints + Handover)."""
    from .evaluate import switched_off

    return "off" if switched_off(entry, cfg) else "guide"


_DROPPED_FIELDS = ("policy", "recipes")      # still on the core model; guide-only advisories do not use them


def _row(entry: Any, cfg: Any) -> dict[str, Any]:
    """출력용 한 줄 dict를 만들고 상태를 붙인다. 쓰지 않는 ``policy`` / ``recipes``는 뺀다."""
    data = {k: v for k, v in entry.model_dump(mode="json").items() if k not in _DROPPED_FIELDS}
    return {**data, "state": _state(entry, cfg)}


def _table(rows: list[list[str]], heads: list[str]) -> list[str]:
    """머리글, 구분선, 행으로 고정폭 텍스트 표를 만든다."""
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(heads)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return [fmt.format(*heads).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip(),
            *[fmt.format(*r).rstrip() for r in rows]]


@advisories_cli.command("list")
def list_cmd(
    config: Optional[List[Path]] = ConfigOpt,
    as_json: bool = typer.Option(False, "--json", help="machine-readable output"),
) -> None:
    """모든 권고를 표로 보여 준다. --json이면 JSON으로 출력한다.

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
    """권고 하나의 모든 필드를 보여 준다. 모르는 id면 exit 1로 끝낸다.

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
    """권고 파일의 분석기 라이브러리 이름, 아티팩트 패턴, 노트 파일을 검사한다.
    문제가 있으면 exit 1로 끝낸다.

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
