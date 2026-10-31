"""``sbmig recipes …`` 명령(list | show | check | sets) — 레시피
카탈로그(knowledge/recipes/catalog.yml)를 보고 검사한다. 흐름상 위치: 실행과 무관한 운영 도구. ``check`` 는
카탈로그 ↔ 설정 레시피 세트 ↔ 레시피 아티팩트의 어긋남을 잡고 (불변조건 5, 스냅숏 테스트와 같은 역할), ``check
--jars`` 는 Maven 으로 실제 레시피 jar 를 받아 id 를 확인한다. ``sets`` 는 케이스별로 계획기가 고를 변환
레시피를 보여 준다. 주의: typer 는 명령 함수의 docstring 을 ``--help`` 화면으로 쓴다 — 한국어 첫 줄이 명령
목록의 요약이 된다 (``sbmig --help``, ``sbmig kb --help`` 와 같은 방식).

`sbmig recipes …` — typer sub-app over the recipe catalog (knowledge/recipes/catalog.yml). See docs/RECIPES.md."""
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
    """설정 읽기. 레시피 명령에는 사내 아티팩트(artifacts) 설정이 필요 없으므로, 어느 설정에도 없으면 자리표시
    parent 로 채워 검증 오류를 피한다(회사 좌표를 지어내지 않는다).

    Recipe commands do not need the company artifacts section; use a placeholder when no config provides it."""
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
    """설정의 카탈로그 파일을 읽는다. 없으면 exit 2."""
    from .catalog import RecipeCatalog

    path = cfg.recipe_catalog_file
    if not path.exists():
        typer.echo(f"recipe catalog not found: {path}", err=True)
        raise typer.Exit(2)
    return RecipeCatalog.load(path)


def _where(row: dict) -> str:
    """artifact 칸: jar 좌표, 없으면 ``(core)``(rewrite-maven-plugin 에 들어 있는 레시피)."""
    return row.get("artifact") or ("(core)" if row.get("origin") == "openrewrite" else "-")


def _table(rows: list[dict], cols: list[tuple[str, Any]]) -> list[str]:
    """고정폭 텍스트 표(머리, 구분선, 줄들)."""
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
    """카탈로그의 모든 레시피를 표로(출처, 단계, jar, 게이트, 조언자 허용, 세트). --json 이면 JSON.

    Every catalog recipe (origin, phase, artifact, gate, advisor, sets)."""
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
    """레시피 하나: 카탈로그 항목과, 그것을 참조하는 설정 세트 / 묶음 레시피. 모르는 id 면 exit 1.

    One recipe: catalog entry and the config sets / composites that reference it."""
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
    """카탈로그를 설정 레시피 세트·레시피 아티팩트와 대조한다(오프라인). --jars 면 Maven 으로 레시피 jar 를
    받아 모든 openrewrite id 가 정말 그 jar 에 있는지도 본다(BOM openrewrite.recipe_bom_version, 네트워크
    필요). 문제가 있으면 exit 1.

    Cross-check the catalog against the config recipe sets and the recipe artifacts.

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
    """``check --jars`` 의 본체: jarcheck 결과를 표(또는 JSON)로 찍고, 문제가 있으면 exit 1."""
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
    """설정 레시피 세트와, 케이스마다 v2 계획기가 고를 변환
    1회의 레시피(조건부 레시피는 따로: EE javax import / javax.servlet / junit4 / spring-security / spring-batch /
    hibernate 가 있을 때).

    The config recipe sets and, per case, the one transform run the v2 planner resolves from them."""
    from sbmig.core.models import CaseType

    from .catalog import config_recipe_sets
    from .planner import _Facts, transform_recipes

    cfg = _load(config)
    lo = ({}, set())
    hi = ({"javax.servlet": 1}, {"junit4", "spring-security", "spring-batch", "hibernate", "javax-annotation"})
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
            typer.echo(f"    + when detected (EE javax / javax.servlet imports, libraries junit4 / spring-security / "
                       f"spring-batch / hibernate): "
                       f"{', '.join(d['transform_conditional'])}")
    typer.echo("\nmsa: one run per library root / service with its own case (library roots never get the Boot set); "
               "top-aggregator: one run at the root with the union.")
