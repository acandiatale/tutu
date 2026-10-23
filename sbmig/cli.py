"""`sbmig` command line. Every heavy import is lazy so `sbmig --help` works even if a package is broken."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="JDK 21 + Spring Boot 4.1 migration agent (OpenRewrite + triage + LLM build-fix loop).")
log = logging.getLogger("sbmig.cli")

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")


LOG_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


from sbmig.core.diag import dump_threads, install_thread_dump_signal  # noqa: E402,F401 - re-exported


def setup_logging(verbose: bool = False) -> None:
    from rich.logging import RichHandler

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("sbmig")
    root.setLevel(logging.DEBUG)                       # handlers filter: console INFO (or DEBUG), file always DEBUG
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        root.addHandler(RichHandler(rich_tracebacks=True, show_path=False, markup=False))
    for h in root.handlers:
        if isinstance(h, RichHandler):
            h.setLevel(level)
    root.propagate = False
    install_thread_dump_signal()


_LOG_THREAD: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("sbmig_log_thread", default=None)


class _ThreadFilter(logging.Filter):
    """Pass only records of one run: emitted by its thread, or by a thread that inherited its context (a graph engine
    running a node in an executor thread copies the context, so `_LOG_THREAD` still names the run's thread)."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id or _LOG_THREAD.get() == self.thread_id


def attach_file_log(path: Path, thread_id: Optional[int] = None) -> Path:
    """Append DEBUG-level sbmig logs (LLM calls, builds, decisions; never keys) to `path`. One handler per file.

    With `thread_id` (in-process runs of the web app) the file only gets records of that thread (and of threads
    running in its context), so concurrent runs keep separate `sbmig.log` files; one handler per (file, thread)."""
    path = Path(path).resolve()
    root = logging.getLogger("sbmig")
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path \
                and getattr(h, "sbmig_thread", None) == thread_id:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if root.level == logging.NOTSET or root.level > logging.DEBUG:   # the file gets everything; console keeps its level
        for h in root.handlers:
            if h.level == logging.NOTSET:
                h.setLevel(logging.INFO if root.level in (logging.NOTSET, logging.INFO) else root.level)
        root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FILE_FORMAT))
    fh.sbmig_thread = thread_id  # type: ignore[attr-defined]
    if thread_id is not None:
        fh.addFilter(_ThreadFilter(thread_id))
        if thread_id == threading.get_ident():
            _LOG_THREAD.set(thread_id)
    root.addHandler(fh)
    if thread_id is None:
        log.info("sbmig %s — log file %s", " ".join(sys.argv[1:])[:300], path)
    else:
        log.info("sbmig run (thread %s) — log file %s", thread_id, path)
    return path


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
          log_file: Optional[Path] = typer.Option(None, "--log-file",
                                                  help="also append DEBUG logs to this file (runs always log to "
                                                       "<run dir>/sbmig.log)")) -> None:
    setup_logging(verbose)
    if log_file:
        attach_file_log(log_file)


def _load(configs: Optional[List[Path]], overrides: Optional[dict] = None, repo_root: Optional[Path] = None):
    from pydantic import ValidationError

    from sbmig.core.config import load_config

    try:
        return load_config(*(configs or []), overrides=overrides or None, repo_root=repo_root)
    except ValidationError as e:
        # the usual cause: no `--config config/artifacts.yml` (the company artifacts section has no default)
        missing = [".".join(str(x) for x in err.get("loc", ())) for err in e.errors() if err.get("type") == "missing"]
        typer.echo(f"config error: {e.error_count()} problem(s) in the merged configuration", err=True)
        for err in e.errors()[:10]:
            typer.echo(f"  {'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}", err=True)
        if any(m == "artifacts" or m.startswith("artifacts.") for m in missing):
            typer.echo("hint: pass the company config: --config config/artifacts.yml "
                       "(copy config/artifacts.example.yml and fill in parent / mandatory / optional; "
                       "config/artifacts.local-e2e.yml works for a local try)", err=True)
        raise typer.Exit(2)


# `sbmig llm-probe` without --config: the probe needs .env (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL) only, so the
# required company artifacts section is stubbed — nothing in the probe reads it
_PROBE_ARTIFACTS_STUB = {"artifacts": {"parent": {"group_id": "sbmig.probe", "artifact_id": "no-config", "version": "0"}}}


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
                                             help="intake|analyze|plan|jdk21|spring|restructure|verify"),
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
    from sbmig.loop import parse_stop_after

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
    try:
        phase = parse_stop_after(stop_after)
    except ValueError as e:
        _fail(f"--{e}")
    cfg = _load(config, overrides, repo_root)
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator
    from sbmig.report import console_summary

    ws = Workspace.from_input(input, out, run_id=run_id)
    log_path = attach_file_log(ws.run_dir / "sbmig.log")
    orch = Orchestrator(cfg, ws, kb=NullKnowledgeBase() if no_kb else None)
    try:
        report = orch.run(stop_after=phase, dry_run=dry_run)
    except Exception:
        log.exception("run aborted by an unexpected error (see %s)", log_path)
        raise
    console_summary(report)
    typer.echo(f"log: {log_path}   (share this file + report.md when asking for help)")
    raise typer.Exit(run_exit_code(report))


def _mask_headers(headers) -> dict:
    out = {}
    for k, v in dict(headers or {}).items():
        v = str(v)
        if k.lower() in ("authorization", "x-api-key", "api-key"):
            v = v[:10] + "…" + f"({len(v)} chars)"
        out[k] = v
    return out


def _echo_wire(resp, timeout) -> None:
    """What actually went over the wire (token masked) and what came back, for comparing with a working client."""
    req = getattr(resp, "request", None)
    if req is not None:
        body = getattr(req, "body", None) or getattr(req, "content", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        typer.echo(f"sent: {getattr(req, 'method', 'POST')} {getattr(req, 'url', '?')}")
        typer.echo(f"sent headers: {json.dumps(_mask_headers(getattr(req, 'headers', {})), ensure_ascii=False)}")
        typer.echo(f"sent body: {str(body)[:1500]}")
    try:
        import requests.utils as ru
        url = str(getattr(req, "url", "")) if req is not None else ""
        typer.echo(f"proxies in effect for this url: {ru.get_environ_proxies(url) if url else '?'}  "
                   f"(NO_PROXY={os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or '-'})")
    except Exception:
        pass
    typer.echo(f"response headers: {json.dumps(dict(resp.headers), ensure_ascii=False)[:800]}")
    typer.echo(f"timeout: {timeout}s")


def _probe_messages(prompt: str, no_system: bool = False) -> list[dict]:
    msgs = [] if no_system else [{"role": "system", "content": "You are a helpful assistant."}]
    return msgs + [{"role": "user", "content": prompt}]


def _probe_bare(cfg, prompt: str, no_system: bool = False) -> None:
    """The company snippet, verbatim: requests.post(url, headers=..., json=payload, timeout=...)."""
    import requests

    llm = cfg.llm
    url = llm.base_url
    key = os.environ.get(llm.api_key_env or "", "")
    payload = {"model": llm.model, "messages": _probe_messages(prompt, no_system), **(llm.payload_extra or {})}
    headers = {"Authorization": f"Bearer {key}"}
    typer.echo(f"bare requests.post → {url}  key_env={llm.api_key_env} key_set={bool(key)}")
    typer.echo("payload: " + json.dumps(payload, ensure_ascii=False)[:1500])
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=float(llm.timeout_seconds))
    except Exception as e:
        typer.echo(f"TRANSPORT ERROR: {type(e).__name__}: {e}")
        raise typer.Exit(1)
    _echo_wire(resp, llm.timeout_seconds)
    typer.echo(f"HTTP {resp.status_code}")
    typer.echo(resp.text[:4000])
    raise typer.Exit(0 if resp.status_code < 300 else 1)


@app.command("serve")
def serve_cmd(host: str = typer.Option("127.0.0.1", "--host", help="bind address (0.0.0.0 to share on the LAN)"),
              port: int = typer.Option(8765, "--port"),
              out: Path = typer.Option(Path("out/web"), "--out", "-o", help="where uploads and runs go"),
              repo_root: Optional[Path] = typer.Option(None, "--repo-root",
                                                       help="sbmig repo root (config/, kb/)"),
              graceful_timeout: int = typer.Option(5, "--graceful-timeout",
                                                   help="seconds uvicorn waits for open connections (SSE streams) "
                                                        "on shutdown before closing them"),
              max_upload_mb: Optional[int] = typer.Option(None, "--max-upload-mb", min=0,
                                                          help="largest project zip the UI accepts, in MB "
                                                               "(default: $SBMIG_MAX_UPLOAD_MB or 512; 0 = no "
                                                               "limit, e.g. for one oversized project)")) -> None:
    """One-page web UI (FastAPI on uvicorn): upload a project zip, run the migration in-process (LangGraph engine,
    one worker thread per run), watch graph nodes / journal / log live (SSE), download report.md / result.zip.
    Same app without the CLI: `SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765`."""
    import uvicorn

    from sbmig.core.config import PACKAGE_ROOT, load_dotenv_files
    from sbmig.web.app import MAX_UPLOAD_ENV, create_app, describe_max_upload, resolve_max_upload

    load_dotenv_files()                       # .env is read once here for the server itself, then per run
    root = Path(repo_root).resolve() if repo_root else PACKAGE_ROOT
    attach_file_log(Path(out) / "sbmig-web.log")
    typer.echo(f"sbmig web UI: http://{host}:{port}   runs: {Path(out).resolve()}   configs: {root / 'config'}   "
               f"upload limit: {describe_max_upload(resolve_max_upload(max_upload_mb))}")
    env_limit = f" {MAX_UPLOAD_ENV}={max_upload_mb}" if max_upload_mb is not None else ""
    typer.echo(f"(same app under plain uvicorn: SBMIG_WEB_OUT={out} SBMIG_WEB_REPO_ROOT={root}{env_limit} "
               f"uvicorn sbmig.web.app:app --host {host} --port {port} --timeout-graceful-shutdown {graceful_timeout})")
    # shutdown: open SSE streams end (RunManager.shutdown via the lifespan), running Maven processes are killed, and
    # uvicorn stops waiting for connections after `graceful_timeout` — without it Ctrl+C hung on the browser tabs
    uvicorn.run(create_app(Path(out), root, max_upload_mb=max_upload_mb), host=host, port=port, log_level="info",
                timeout_graceful_shutdown=graceful_timeout)


@app.command("llm-probe")
def llm_probe(config: Optional[List[Path]] = ConfigOpt,
              prompt: str = typer.Option("Reply with the single word: pong", "--prompt"),
              llm_provider: Optional[str] = typer.Option(None, "--llm-provider"),
              model: Optional[str] = typer.Option(None, "--model", help="override llm.model / LLM_MODEL for this probe"),
              payload_extra: Optional[str] = typer.Option(
                  None, "--payload-extra", help="JSON object that REPLACES llm.payload_extra for this probe, e.g. "
                                                "'{}' to send only model + messages, '{\"need_origin\": false}'"),
              no_system: bool = typer.Option(False, "--no-system", help="send only the user message (no system message)"),
              bare: bool = typer.Option(False, "--bare", help="bypass sbmig's client: plain requests.post(url, "
                                                              "headers={Authorization}, json=payload, timeout=) "
                                                              "with the same payload — compare with the normal probe")
              ) -> None:
    """Send one small request with the configured LLM provider and print what came back (raw JSON for HTTP
    providers, then the extracted reply). Run this first on a new gateway; if the reply cannot be extracted, set
    `llm.response_path` to the dot path shown in the raw JSON. `--bare` sends the identical payload with a bare
    `requests.post` (no session, no extra headers) so a gateway problem can be told apart from a client problem."""
    import json

    from sbmig.core.interfaces import ChatMessage
    from sbmig.fix import CompanyClient, LLMConfigError, LLMError, make_llm_client

    overrides: dict = {"llm": {}}
    if llm_provider:
        overrides["llm"]["provider"] = llm_provider
    if model:
        overrides["llm"]["model"] = model
    overrides.update(_PROBE_ARTIFACTS_STUB)        # the probe reads nothing from `artifacts`
    cfg = _load(config, overrides)
    if payload_extra is not None:
        try:
            extra = json.loads(payload_extra)
        except ValueError as e:
            typer.echo(f"--payload-extra must be a JSON object: {e}", err=True)
            raise typer.Exit(2)
        if not isinstance(extra, dict):
            typer.echo("--payload-extra must be a JSON object", err=True)
            raise typer.Exit(2)
        cfg.llm.payload_extra = extra               # replaces (a merge could not drop a key such as need_origin)
    llm = cfg.llm
    if bare:
        _probe_bare(cfg, prompt, no_system=no_system)
        return
    typer.echo(f"provider={llm.provider} model={llm.model} url={llm.base_url} key_env={llm.api_key_env}"
               f" payload_extra={llm.payload_extra} response_path={llm.response_path or '(auto)'}")
    try:
        client = make_llm_client(cfg)
    except LLMConfigError as e:          # missing key / URL / model: say which variable, no traceback
        typer.echo(f"CONFIG ERROR: {e}")
        typer.echo("hint: put LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env at the repo root (copy .env.example) "
                   "or export them in the shell; `--llm-provider fake` runs the probe without a gateway")
        raise typer.Exit(1)
    inner = getattr(client, "inner", client)
    msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in _probe_messages(prompt, no_system)]
    if isinstance(inner, CompanyClient):
        payload = inner.payload(msgs)
        typer.echo("request payload: " + json.dumps(payload, ensure_ascii=False)[:1500])
        try:
            resp = inner.post_once(payload)
        except Exception as e:  # transport-level failure: the class and message say proxy / DNS / refused
            typer.echo(f"TRANSPORT ERROR: {type(e).__name__}: {e}")
            typer.echo("hints: URL (the full endpoint, http://...), proxy (NO_PROXY=<gateway host> in .env)")
            raise typer.Exit(1)
        _echo_wire(resp, inner.timeout)
        typer.echo(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            typer.echo(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        except ValueError:
            typer.echo(resp.text[:4000])
            raise typer.Exit(1)
        if resp.status_code >= 300:
            raise typer.Exit(1)
        try:
            typer.echo("extracted reply: " + repr(inner._parse(data, inner.model).content[:500]))
        except LLMError as e:
            typer.echo(f"PROBLEM: {e}")
            from sbmig.fix.client import _REPLY_PATHS
            typer.echo("auto-detect paths tried: " + ", ".join(_REPLY_PATHS)
                       + "  → set LLM_RESPONSE_PATH in .env to the dot path of the text in the JSON above")
            raise typer.Exit(1)
        return
    out = client.complete(msgs, role="other")
    typer.echo(f"reply ({type(client).__name__}): {out.content[:500]!r}")


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


# --------------------------------------------------------------------------- inventory (pom-less webapps)
def inventory_console_lines(inv) -> list[str]:
    """≤ 12 lines an operator can retype from the screen (no paths longer than needed)."""
    jars = [j for wr in inv.webapp_roots for j in wr.lib_jars]
    active = [j for j in jars if not j.ignored]
    unknown = sorted(j.name for j in active if not j.artifact_guess)
    ignored = sum(len(wr.ignored_files) for wr in inv.webapp_roots)
    levels = sorted({wr.java_version_from_classes for wr in inv.webapp_roots if wr.java_version_from_classes})
    no_src = sum(wr.classes_without_source for wr in inv.webapp_roots)
    webapps = ", ".join(f"{wr.dir or '.'} ({wr.jsp_count} jsp, servlet {wr.servlet_spec or '?'}, "
                        f"{'/'.join(wr.frameworks) or '-'})" for wr in inv.webapp_roots) or "none"
    sources = ", ".join(f"{s.dir or '.'} ({s.java_files} java, {s.encoding})" for s in inv.source_roots) or "none"
    lines = [f"inventory: {inv.root}",
             f"  webapp roots {len(inv.webapp_roots)}: {webapps}",
             f"  source roots {len(inv.source_roots)}: {sources}",
             f"  java from classes: {', '.join(levels) or '-'}",
             f"  jars: {len(active) - len(unknown)} known / {len(unknown)} unknown / {ignored} ignored"
             + (f" (unknown: {', '.join(unknown[:3])}{', …' if len(unknown) > 3 else ''})" if unknown else ""),
             f"  classes without source: {no_src}   duplicate classes: {inv.duplicate_classes}",
             "  proposals: " + (", ".join(f"{p.name} ({p.packaging}, java {p.java_version})"
                                          for p in inv.proposals) or "none")]
    if inv.build_files:
        lines.append("  build files: " + ", ".join(inv.build_files[:5]) + (" …" if len(inv.build_files) > 5 else ""))
    if inv.warnings:
        lines.append(f"  warnings: {len(inv.warnings)} (first: {inv.warnings[0][:100]})")
    for key in ("inventory_md", "inventory_json"):
        if key in inv.outputs:
            lines.append(f"  {key.split('_')[1]}: {inv.outputs[key]}")
    return lines[:12]


@app.command()
def inventory(input: Path = typer.Argument(..., help="webapp directory without a pom, or a zip of it"),
              out: Path = typer.Option(Path("out/inventory"), "--out", "-o", help="where inventory.md / .json go"),
              config: Optional[List[Path]] = ConfigOpt,
              as_json: bool = typer.Option(False, "--json", help="print inventory.json to stdout instead")) -> None:
    """Read-only inventory of a Tomcat webapp tree WITHOUT a pom (JSPs, sources, WEB-INF/lib jars,
    WEB-INF/classes): webapp roots, source roots, jar coordinates guessed from the file names
    (knowledge/libraries/jar_names.yml), Java level of the class files, one Maven module proposal per webapp.
    Writes inventory.md (report.language) + inventory.json. Changes nothing; exit 0 (3 when the path is missing)."""
    import shutil
    import zipfile

    from sbmig.analyze.webapp_inventory import scan_webapp_tree
    from sbmig.core.workspace import _detect_project_root, _safe_extract
    from sbmig.report.inventory import write_inventory

    if not input.exists():
        _fail(f"no such file or directory: {input}", EXIT_ABORTED)
    cfg = _load(config) if config else _load(None, overrides=dict(_PROBE_ARTIFACTS_STUB))
    tmp: Optional[Path] = None
    try:
        if input.is_dir():
            root = input
        elif zipfile.is_zipfile(input):
            tmp = Path(tempfile.mkdtemp(prefix="sbmig-inventory-"))
            with zipfile.ZipFile(input) as zf:
                _safe_extract(zf, tmp)
            root = _detect_project_root(tmp)
        else:
            _fail(f"input must be a directory or a zip: {input}", EXIT_ABORTED)
        inv = scan_webapp_tree(root, name=input.stem if root == tmp else None)
        if tmp is not None:                    # report the zip, not the temp dir that is deleted below
            inv.root = str(input.resolve()) + ("" if root == tmp else f"!/{root.relative_to(tmp).as_posix()}")
        write_inventory(inv, out, cfg)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    if as_json:
        typer.echo(inv.model_dump_json(indent=2))
        return
    for line in inventory_console_lines(inv):
        typer.echo(line)


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
