"""`sbmig` command line. Every heavy import is lazy so `sbmig --help` works even if a package is broken."""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="JDK 21 + Spring Boot 4.1 migration agent (OpenRewrite + triage + LLM build-fix loop).")
log = logging.getLogger("sbmig.cli")

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")


def setup_logging(verbose: bool = False) -> None:
    from rich.logging import RichHandler

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("sbmig")
    root.setLevel(level)
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        root.addHandler(RichHandler(rich_tracebacks=True, show_path=False, markup=False))
    root.propagate = False


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging")) -> None:
    setup_logging(verbose)


def _load(configs: Optional[List[Path]], overrides: Optional[dict] = None, repo_root: Optional[Path] = None):
    from sbmig.core.config import load_config

    return load_config(*(configs or []), overrides=overrides or None, repo_root=repo_root)


def _view_workspace(input_path: Path, out: Optional[Path] = None, prefix: str = "view"):
    """Workspace over an existing directory (no copy) — or an extracted zip in a temp dir."""
    from sbmig.core.workspace import Workspace, new_run_id

    run_id = new_run_id(prefix)
    base = Path(out) if out else Path(tempfile.mkdtemp(prefix="sbmig-"))
    if input_path.is_dir():
        return Workspace(input_path, base / run_id, run_id)
    return Workspace.from_input(input_path, base, run_id=run_id)


EXIT_SUCCESS = 0          # final `mvn clean package` passed and nothing is blocked
EXIT_INCOMPLETE = 2       # finished, but with blocked and/or unresolved errors (see report.md)
EXIT_ABORTED = 3          # aborted: no Maven / not a Maven project / configured parent not resolvable / internal error


def run_exit_code(report) -> int:
    flags = list(getattr(report, "flags", []) or [])
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return EXIT_ABORTED
    if report.success and not getattr(report, "blocked", None):
        return EXIT_SUCCESS
    return EXIT_INCOMPLETE


def _fail(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# --------------------------------------------------------------------------- run
@app.command()
def run(
    input: Path = typer.Argument(..., exists=True, help="as-is project directory or zip"),
    config: Optional[List[Path]] = ConfigOpt,
    out: Path = typer.Option(Path("out"), "--out", "-o", help="output directory"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    stop_after: Optional[str] = typer.Option(None, "--stop-after",
                                             help="intake|analyze|plan|jdk21|spring|restructure"),
    dry_run: bool = typer.Option(False, "--dry-run", help="compute and journal decisions, apply nothing"),
    no_kb: bool = typer.Option(False, "--no-kb", help="do not use or feed the knowledge base"),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider",
                                               help="openai_compatible|anthropic|fake|plugin|<entry-point name>"),
    llm_plugin: Optional[str] = typer.Option(None, "--llm-plugin",
                                             help="in-house LLM adapter 'pkg.module:attr' (implies --llm-provider plugin)"),
    fix_plugin: Optional[str] = typer.Option(None, "--fix-plugin",
                                             help="in-house fixer/agent adapter 'pkg.module:attr' (replaces the LLM fixer)"),
    java_home: Optional[str] = typer.Option(None, "--java-home", help="JDK 21 home for Maven"),
    settings: Optional[str] = typer.Option(None, "--settings", help="Maven settings.xml"),
    repo_root: Optional[Path] = typer.Option(None, "--repo-root", help="sbmig repo root (kb/, knowledge/)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Migrate a project end to end.

    Exit code 0 = final `mvn clean package` passed and nothing is blocked; 2 = finished with blocked and/or
    unresolved errors (see report.md "Blocked" / "Unresolved errors"); 3 = aborted (no Maven, not a Maven project,
    configured parent POM not resolvable, internal error)."""
    if verbose:
        setup_logging(True)
    from sbmig.core.models import Phase

    overrides: dict = {}
    if llm_provider:
        overrides.setdefault("llm", {})["provider"] = llm_provider
    if llm_plugin:
        overrides.setdefault("llm", {}).update({"provider": "plugin", "plugin": llm_plugin})
    if fix_plugin:
        overrides.setdefault("fix", {})["plugin"] = fix_plugin
    if java_home:
        overrides.setdefault("build", {})["java_home"] = java_home
    if settings:
        overrides.setdefault("build", {})["settings_file"] = settings
    if no_kb:
        overrides["kb"] = {"harvest": False, "use_active_recipes": False, "use_patch_replay": False,
                           "use_hints": False, "generate_recipe_candidates": False}
    phase = None
    if stop_after:
        try:
            phase = Phase(stop_after.lower())
        except ValueError:
            _fail(f"--stop-after must be one of: {', '.join(p.value for p in Phase)}")
    cfg = _load(config, overrides, repo_root)
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator
    from sbmig.report import console_summary

    ws = Workspace.from_input(input, out, run_id=run_id)
    orch = Orchestrator(cfg, ws, kb=NullKnowledgeBase() if no_kb else None)
    report = orch.run(stop_after=phase, dry_run=dry_run)
    console_summary(report)
    raise typer.Exit(run_exit_code(report))


# --------------------------------------------------------------------------- analyze / plan
@app.command()
def analyze(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
            as_json: bool = typer.Option(False, "--json", help="print the full profile as JSON")) -> None:
    """Analyze a project (no changes) and print its profile."""
    from sbmig.analyze import analyze_project

    cfg = _load(config)
    ws = _view_workspace(input, prefix="analyze")
    profile = analyze_project(ws, cfg)
    if as_json:
        typer.echo(profile.model_dump_json(indent=2))
        return
    typer.echo(f"{profile.name}: case {profile.case_type.value} (confidence {profile.confidence:.2f})")
    typer.echo(f"  java {profile.java_version or '?'}  boot {profile.spring_boot_version or '-'}  "
               f"spring {profile.spring_version or '-'}  poms {len(profile.poms)}  build {profile.build_tool}")
    for r in profile.reasons:
        typer.echo(f"  - {r}")
    if profile.libraries:
        typer.echo("  libraries: " + ", ".join(sorted(f"{l.name}{'@' + l.version if l.version else ''}"
                                                       for l in profile.libraries)))
    for s in profile.services:
        typer.echo(f"  service {s.name}: {s.case_type.value} ({s.root_pom})")
    for w in profile.warnings:
        typer.echo(f"  warning: {w}")
    try:
        from sbmig.advisory import evaluate_advisories
        findings = evaluate_advisories(profile, cfg) if cfg.advisories.enabled else []
    except Exception as e:  # advisories must never break `analyze`
        log.warning("advisories unavailable: %s", e)
        findings = []
    for f in findings:
        rec = f" (recipe: {', '.join(f.recipes)})" if f.recipes else ""
        typer.echo(f"  advisory [{f.policy}] {f.advisory_id}@{f.detected_version or '?'} — {f.title}"
                   f"{' → ' + f.replacement if f.replacement else ''}{rec}")


@app.command()
def plan(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Analyze + plan (no changes) and print the migration plan."""
    from sbmig.analyze import analyze_project
    from sbmig.plan import build_plan, pre_handlers_for

    cfg = _load(config)
    ws = _view_workspace(input, prefix="plan")
    profile = analyze_project(ws, cfg)
    kb = None
    try:
        from sbmig.kb import FileKnowledgeBase
        kb = FileKnowledgeBase(cfg)
    except Exception as e:
        log.info("planning without KB: %s", e)
    p = build_plan(profile, cfg, kb)
    if as_json:
        typer.echo(p.model_dump_json(indent=2))
        return
    typer.echo(f"plan for {profile.name} ({p.case_type.value}):")
    for i, s in enumerate(p.steps, 1):
        rec = f" [{', '.join(r.id for r in s.recipes)}]" if s.recipes else ""
        goal = f" mvn {s.build_goal}" if s.build_goal else ""
        tgt = f" @{s.target}" if s.target else ""
        typer.echo(f"  {i:2}. {s.id:<24} {s.kind.value:<15}{goal}{tgt}{rec}")
    pre = pre_handlers_for(profile, cfg)
    if pre:
        typer.echo("  pre-emptive handlers: " + ", ".join(pre))
    for n in p.notes:
        typer.echo(f"  - {n}")


# --------------------------------------------------------------------------- build / logs / rewrite
def _print_errors(errors) -> None:
    from rich.console import Console
    from rich.table import Table

    t = Table(title=f"{len(errors)} error(s)")
    for c in ("kind", "where", "message", "L1 signature"):
        t.add_column(c, overflow="fold")
    for e in errors:
        where = f"{e.file}:{e.line}" if e.file else (e.artifact.coordinates if e.artifact else (e.module or "-"))
        t.add_row(e.kind.value, where, e.message[:200], e.signature)
    Console().print(t)


@app.command()
def build(project_dir: Path = typer.Argument(..., exists=True, file_okay=False),
          goal: str = typer.Option("compile", "--goal", "-g"), config: Optional[List[Path]] = ConfigOpt,
          out: Optional[Path] = typer.Option(None, "--out", help="where logs go (default: temp dir)")) -> None:
    """Run Maven on an existing directory and print the parsed errors + signatures (debugging aid)."""
    from sbmig.build import MavenRunner

    cfg = _load(config)
    ws = _view_workspace(project_dir, out, prefix="build")
    res = MavenRunner(ws, cfg).run(goal, label="cli-build")
    typer.echo(f"{'BUILD SUCCESS' if res.ok else 'BUILD FAILURE'} ({res.duration_s:.1f}s) log: {res.log_path}")
    if res.errors:
        _print_errors(res.errors)
    raise typer.Exit(0 if res.ok else 2)


@app.command("parse-log")
def parse_log(logfile: Path = typer.Argument(..., exists=True, dir_okay=False),
              root: Path = typer.Option(Path("."), "--root", help="project root used to relativize paths"),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Parse an existing Maven log into structured errors."""
    from sbmig.build import parse_maven_log

    errors = parse_maven_log(logfile.read_text(encoding="utf-8", errors="replace"), root.resolve())
    if as_json:
        typer.echo(json.dumps([e.model_dump(mode="json") for e in errors], indent=2))
        return
    if not errors:
        typer.echo("no errors found")
        return
    _print_errors(errors)


@app.command()
def rewrite(project_dir: Path = typer.Argument(..., exists=True, file_okay=False),
            recipe: List[str] = typer.Option(..., "--recipe", "-r", help="recipe id (repeatable)"),
            dry_run: bool = typer.Option(False, "--dry-run"), config: Optional[List[Path]] = ConfigOpt,
            out: Optional[Path] = typer.Option(None, "--out")) -> None:
    """Run OpenRewrite recipes on an existing directory (in place unless --dry-run)."""
    from sbmig.core.models import RecipeRef
    from sbmig.rewrite import OpenRewriteRunner

    cfg = _load(config)
    ws = _view_workspace(project_dir, out, prefix="rewrite")
    try:
        from sbmig.plan.planner import recipe_ref
        refs = [recipe_ref(r, cfg) for r in recipe]
    except Exception:
        refs = [RecipeRef(id=r) for r in recipe]
    res = OpenRewriteRunner(ws, cfg).run(refs, dry_run=dry_run, label="cli-rewrite")
    typer.echo(f"{'ok' if res.ok else 'FAILED'}: {len(res.changed_files)} file(s) changed  log: {res.log_path}")
    for f in res.changed_files[:50]:
        typer.echo(f"  {f}")
    if res.error:
        typer.echo(f"error: {res.error}", err=True)
    raise typer.Exit(0 if res.ok else 2)


@app.command()
def version() -> None:
    """Print the sbmig version."""
    from sbmig import __version__

    typer.echo(f"sbmig {__version__}")


# --------------------------------------------------------------------------- sub-apps (kb, recipes, fleet)
def _mount(module: str, attr: str, name: str, help_text: str) -> None:
    """Mount a typer sub-app; if its package fails to import, keep `sbmig --help` working and explain on use."""
    try:
        import importlib
        sub = getattr(importlib.import_module(module), attr)
        app.add_typer(sub, name=name, help=help_text)
    except Exception as e:  # pragma: no cover - depends on the other workstreams
        err = f"{type(e).__name__}: {e}"

        @app.command(name, context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
        def _unavailable() -> None:
            _fail(f"{module}.{attr} is not available: {err}")

        _unavailable.__doc__ = f"{help_text} (unavailable: {module} failed to import)"


_mount("sbmig.kb", "kb_cli", "kb",
       "knowledge base: list | show | promote | reject | reindex | ledger | symbols | learning | export | import")
_mount("sbmig.plan.catalog_cli", "recipes_cli", "recipes",
       "recipe catalog (knowledge/recipes/catalog.yml): list | show | check | sets")
_mount("sbmig.fleet.cli", "fleet_cli", "fleet",
       "one agent, many projects, sequential with KB learning: run | status")
_mount("sbmig.advisory.cli", "advisories_cli", "advisories",
       "library advisories (knowledge/libraries/advisories.yml): list | show | check")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
