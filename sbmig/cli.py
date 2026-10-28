"""`sbmig` 명령줄(Typer) — 운영자가 쓰는 모든 명령의 입구.
명령: run(프로젝트 하나 이관), fleet(일괄 실행), serve(웹 UI), llm-probe(게이트웨이 점검), analyze / plan(변경
없이 보기), inventory(pom 없는 트리 설명서), build / parse-log / rewrite(디버깅), clean(디스크 정리), kb /
recipes / advisories(하위 명령), version. 명령 함수 docstring 의 첫 단락이 `sbmig --help` 목록에 나오는
설명이므로 한국어 첫 줄을 짧게 유지한다(대괄호는 rich 마크업으로 해석될 수 있어 피한다). 무거운 import 는 전부
함수 안에서 늦게 한다 — 어느 패키지가 깨져도 `sbmig --help` 는 돈다. 모든 run 은 실행 디렉토리의
sbmig.log(DEBUG)를 쓰고 마지막에 SBMIG-RESULT 한 줄(ASCII)을 찍는다 — 회사 PC 에서 운영자가 옮겨 적는 줄이다.

`sbmig` command line. Every heavy import is lazy so `sbmig --help` works even if a package is broken."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any, List, Optional

import typer

from sbmig.core.stage import stage_formatter

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="JDK 21 + Spring Boot 4.1 migration agent (OpenRewrite + triage + LLM build-fix loop).")
log = logging.getLogger("sbmig.cli")

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")


LOG_FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(name)s: %(message)s"   # v2.1: the pipeline stage


from sbmig.core.diag import dump_threads, install_thread_dump_signal  # noqa: E402,F401 - re-exported


def harden_console_streams() -> None:
    """콘솔 인코딩(한국어 Windows 의 cp949)은 그대로 두되, 못 쓰는 문자에서 죽지 않게 stdout / stderr 를
    errors="replace" 로 바꾼다. 출력 리다이렉트·작업 스케줄러에서 보고서를 다 쓰고도 UnicodeEncodeError 로
    exit 1 이 나던 문제(라운드 10).

    Keep the console encoding (cp949 on a Korean Windows console) but never crash on a character it cannot
    encode: `errors="replace"` on stdout / stderr. Without it, stdout redirected to a file, `Tee-Object` or a Task
    Scheduler job made `sbmig run` exit 1 with a UnicodeEncodeError after the report was written (✅ in the console
    summary) and every log line with an em dash printed `--- Logging error ---` (round 10)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if (getattr(stream, "errors", None) or "strict") == "strict":
                reconfigure(errors="replace")
        except (ValueError, OSError, AttributeError):   # a detached / closed stream: nothing to protect
            pass
    for h in logging.getLogger("sbmig").handlers + logging.getLogger().handlers:
        stream = getattr(h, "stream", None)
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) \
                and stream not in (sys.stdout, sys.stderr) and hasattr(stream, "reconfigure"):
            try:
                if (getattr(stream, "errors", None) or "strict") == "strict":
                    stream.reconfigure(errors="replace")
            except (ValueError, OSError, AttributeError):
                pass


class _ConsoleFilter(logging.Filter):
    """`extra={"sbmig_console": False}` 로 남긴 로그는 파일에만 간다(콘솔에는 같은 줄을 typer.echo 로 이미
    찍었다: SBMIG-RESULT, 플릿 진행).

    Records logged with `extra={"sbmig_console": False}` go to the log files only (the console already got the
    same line through typer.echo: SBMIG-RESULT, fleet progress)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "sbmig_console", True) is not False


NO_CONSOLE = {"sbmig_console": False}


def setup_logging(verbose: bool = False) -> None:
    """콘솔 로그를 설정한다(rich, INFO — `-v` 면 DEBUG, 줄마다 단계 태그). sbmig 로거 자체는 DEBUG 라서
    파일 로그는 항상 전부 받는다. 스레드 덤프 시그널도 여기서 건다."""
    from rich.logging import RichHandler

    harden_console_streams()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("sbmig")
    root.setLevel(logging.DEBUG)                       # handlers filter: console INFO (or DEBUG), file always DEBUG
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        h = RichHandler(rich_tracebacks=True, show_path=False, markup=False, show_time=True, show_level=True)
        h.setFormatter(stage_formatter("[%(stage)s] %(message)s", datefmt=None))   # v2.1: the pipeline stage
        root.addHandler(h)
    for h in root.handlers:
        if isinstance(h, RichHandler):
            h.setLevel(level)
            if not any(isinstance(f, _ConsoleFilter) for f in h.filters):
                h.addFilter(_ConsoleFilter())
    root.propagate = False
    install_thread_dump_signal()


_LOG_THREAD: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("sbmig_log_thread", default=None)


class _ThreadFilter(logging.Filter):
    """한 실행의 로그만 통과시킨다: 그 스레드, 또는 그 컨텍스트를 물려받은 스레드(LangGraph 가 노드를 도는
    실행 스레드). 웹 서버의 동시 실행이 서로의 sbmig.log 에 섞이지 않게 한다.

    Pass only records of one run: emitted by its thread, or by a thread that inherited its context (a graph engine
    running a node in an executor thread copies the context, so `_LOG_THREAD` still names the run's thread)."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id or _LOG_THREAD.get() == self.thread_id


def attach_file_log(path: Path, thread_id: Optional[int] = None, level: int = logging.DEBUG) -> Path:
    """sbmig 로그(DEBUG: LLM 호출·빌드·결정 — 키는 절대 없음)를 파일에 덧붙인다. thread_id 를 주면(웹 앱의
    동시 실행) 그 실행의 로그만 그 파일로 간다. 플릿 자신의
    fleet/sbmig.log 는 INFO(프로젝트마다 DEBUG sbmig.log 가 따로 있음).

    Append DEBUG-level sbmig logs (LLM calls, builds, decisions; never keys) to `path`. One handler per file.

    With `thread_id` (in-process runs of the web app) the file only gets records of that thread (and of threads
    running in its context), so concurrent runs keep separate `sbmig.log` files; one handler per (file, thread).
    `level` (round 10): the fleet's own `fleet/sbmig.log` is INFO — each project has its DEBUG `<run dir>/sbmig.log`."""
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
    fh.setLevel(level)
    # default asctime "2026-10-27 10:12:03,123": the web stream orders log lines by it (sbmig.web.runs._event_time)
    fh.setFormatter(stage_formatter(LOG_FILE_FORMAT, datefmt=None))
    fh.sbmig_thread = thread_id  # type: ignore[attr-defined]
    if thread_id is not None:
        fh.addFilter(_ThreadFilter(thread_id))
        if thread_id == threading.get_ident():
            _LOG_THREAD.set(thread_id)
    root.addHandler(fh)
    if thread_id is None:
        log.info("sbmig %s - log file %s", " ".join(sys.argv[1:])[:300], path)
    else:
        log.info("sbmig run (thread %s) - log file %s", thread_id, path)
    return path


_ANY_THREAD = object()


def detach_file_log(path: Path, thread_id: Any = _ANY_THREAD) -> int:
    """attach_file_log 가 단 파일 핸들러를 떼고 닫는다(전부, 또는 그 스레드 것만). 뗀 개수를 돌려준다.

    Remove (and close) the file handler(s) `attach_file_log` added for `path` — all of them, or only the one of
    `thread_id` (None = the process-wide one). Returns how many were removed."""
    path = Path(path).resolve()
    root = logging.getLogger("sbmig")
    n = 0
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path \
                and (thread_id is _ANY_THREAD or getattr(h, "sbmig_thread", None) == thread_id):
            root.removeHandler(h)
            h.close()
            n += 1
    return n


# 모든 명령 앞에 도는 콜백: 콘솔 인코딩 보호, 로그 설정, --log-file.
# Typer 앱 도움말이 바뀌지 않게 docstring 은 달지 않는다.
@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
          log_file: Optional[Path] = typer.Option(None, "--log-file",
                                                  help="also append DEBUG logs to this file (runs always log to "
                                                       "<run dir>/sbmig.log)")) -> None:
    harden_console_streams()
    setup_logging(verbose)
    if log_file:
        attach_file_log(log_file)


def _load(configs: Optional[List[Path]], overrides: Optional[dict] = None, repo_root: Optional[Path] = None):
    """설정을 읽는다. 검증 오류면 무엇이 틀렸는지 찍고 exit 2 — 흔한 원인은 `--config
    config/artifacts.yml` 을 빠뜨린 것(회사 artifacts 섹션은 기본값이 없다)."""
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
    """복사 없이 기존 디렉토리 위에 만드는 작업 공간(analyze
    / plan / build 용). zip 이면 임시 디렉토리에 푼다.

    Workspace over an existing directory (no copy) — or an extracted zip in a temp dir."""
    from sbmig.core.workspace import Workspace, new_run_id

    run_id = new_run_id(prefix)
    base = Path(out) if out else Path(tempfile.mkdtemp(prefix="sbmig-"))
    if input_path.is_dir():
        return Workspace(input_path, base / run_id, run_id)
    return Workspace.from_input(input_path, base, run_id=run_id)


# 종료 코드(운영자·플릿·작업 스케줄러가 이것으로 판정): 0 성공, 2 미완(차단·미해결 남음), 3 중단.
EXIT_SUCCESS = 0          # final `mvn clean package` passed and nothing is blocked
EXIT_INCOMPLETE = 2       # finished, but with blocked and/or unresolved errors (see report.md)
EXIT_ABORTED = 3          # aborted: no Maven / not a Maven project / configured parent not resolvable / internal error


def run_exit_code(report) -> int:
    """RunReport → 종료 코드 0 / 2 / 3. report.md 의 SBMIG-RESULT 줄도 같은 규칙을 쓴다.

    0 / 2 / 3 (see `run`); the same mapping prints the SBMIG-RESULT row of report.md."""
    from sbmig.report.result_line import report_exit_code

    return report_exit_code(report)


def _fail(msg: str, code: int = 1) -> None:
    """빨간 글씨로 메시지를 stderr 에 찍고 그 코드로 끝낸다."""
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# v2.1i: 수정 방식 스위치(``fix.mode``)의 허용 값. 설정 모델(FixConfig.mode 의 Literal)에서 읽어 CLI·일괄 실행·웹이
# 같은 목록을 쓴다 — 값이 늘면 config.py 한 곳만 고친다
FIX_MODE_HELP = ("수정 방식(fix.mode): script = 수정 스크립트 생성·저장·재사용(기본), edits = LLM 직접 수정만"
                 "(스크립트 생성·저장·재사용 없음, 토큰 비교용) / script (default) | edits")


def fix_modes() -> tuple[str, ...]:
    """``fix.mode`` 가 받는 값들(``FixConfig.mode`` 의 Literal: ``script``, ``edits``).

    The values `fix.mode` accepts, read from the config model so the CLI, the fleet and the web app never drift."""
    from typing import get_args

    from sbmig.core.config import FixConfig

    return tuple(get_args(FixConfig.model_fields["mode"].annotation))


def check_fix_mode(value: str | None, option: str = "--fix-mode") -> str | None:
    """수정 방식 값 검사: None / 빈 값은 None(설정대로), 허용 값이면 그대로, 아니면 ``typer.BadParameter``
    (사용법 오류, exit 2). 일괄 실행 CLI 도 같은 함수를 쓴다.

    None / "" → None (as configured); an accepted value → itself; anything else → `typer.BadParameter` (exit 2)."""
    if value is None or not str(value).strip():
        return None
    v = str(value).strip().lower()
    allowed = fix_modes()
    if v not in allowed:
        raise typer.BadParameter(f"must be one of {' | '.join(allowed)} (got {value!r})", param_hint=option)
    return v


# --------------------------------------------------------------------------- run
@app.command()
def run(
    input: Path = typer.Argument(..., exists=True, help="as-is project directory or zip"),
    config: Optional[List[Path]] = ConfigOpt,
    out: Path = typer.Option(Path("out"), "--out", "-o", help="output directory"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    stop_after: Optional[str] = typer.Option(None, "--stop-after",
                                             help="intake|analyze|plan|transform|restructure|verify (v2; jdk21|spring are v1 phases)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="compute and journal decisions, apply nothing"),
    no_kb: bool = typer.Option(False, "--no-kb", help="do not use or feed the knowledge base"),
    fix_mode: Annotated[str | None, typer.Option("--fix-mode", help=FIX_MODE_HELP)] = None,
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
    """프로젝트 하나를 끝까지 이관한다(변환 → 재구성 → 구조 검토 → 빌드·수정 루프 → 보고서).

    종료 코드 0 = 최종 `mvn clean package` 성공·차단 없음, 2 = 차단·미해결 오류가 남은 채 끝남(report.md 의
    오류 목록), 3 = 중단(Maven 없음, Maven 프로젝트 아님, 사내 부모 pom 해석 불가, 내부 오류). 마지막
    SBMIG-RESULT 줄이 운영자가 옮겨 적는 줄이다.

    Migrate a project end to end.

    Exit code 0 = final `mvn clean package` passed and nothing is blocked; 2 = finished with blocked and/or
    unresolved errors (see report.md "Error list" / "Blocked"); 3 = aborted (no Maven, not a Maven project,
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
        from sbmig.api import NO_KB_OVERRIDES

        overrides["kb"] = dict(NO_KB_OVERRIDES)
    # v2.1i: 수정 방식 스위치 — 주지 않으면 설정 파일의 fix.mode(기본 script)
    mode = check_fix_mode(fix_mode)
    if mode:
        overrides.setdefault("fix", {})["mode"] = mode
    try:
        phase = parse_stop_after(stop_after)
    except ValueError as e:
        _fail(f"--{e}")
    cfg = _load(config, overrides, repo_root)
    from sbmig.core.workspace import Workspace
    from sbmig.loop import NullKnowledgeBase, Orchestrator
    from sbmig.report import console_summary
    from sbmig.report.result_line import report_result_line, result_line

    ws = Workspace.from_input(input, out, run_id=run_id)
    log_path = attach_file_log(ws.run_dir / "sbmig.log")
    # 두 실험(기본 / edits + KB off)의 로그를 첫 줄에서 구별하려고
    log.info("run %s: fix_mode=%s kb=%s", ws.run_id, cfg.fix.mode, "off" if no_kb else "on")
    orch = Orchestrator(cfg, ws, kb=NullKnowledgeBase() if no_kb else None)
    try:
        report = orch.run(stop_after=phase, dry_run=dry_run)
    except Exception:
        log.exception("run aborted by an unexpected error (see %s)", log_path)
        line = result_line(ws.run_id, 1, False, attention=["crash"])
        typer.echo(line)
        log.info("%s", line, extra=NO_CONSOLE)
        raise
    console_summary(report)
    typer.echo(f"log: {log_path}   (share this file + report.md when asking for help)")
    code = run_exit_code(report)
    line = report_result_line(report, code)          # one ASCII line to retype / grep (round 10)
    typer.echo(line)
    log.info("%s", line, extra=NO_CONSOLE)
    raise typer.Exit(code)


def _mask_headers(headers) -> dict:
    """출력용 헤더 사본: Authorization / API 키 값은 앞 10자와 길이만 남긴다(키를 화면·로그에 내지 않는다)."""
    out = {}
    for k, v in dict(headers or {}).items():
        v = str(v)
        if k.lower() in ("authorization", "x-api-key", "api-key"):
            v = v[:10] + "…" + f"({len(v)} chars)"
        out[k] = v
    return out


def _echo_wire(resp, timeout) -> None:
    """실제로 보낸 요청(토큰 가림)과 받은 응답 헤더·프록시·시간
    제한을 찍는다 — 잘 되는 클라이언트와 비교하려고.

    What actually went over the wire (token masked) and what came back, for comparing with a working client."""
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
    """점검용 메시지 목록(sbmig.fix.company.probe_messages)."""
    from sbmig.fix.company import probe_messages

    return probe_messages(prompt, no_system)


def _probe_bare(cfg, prompt: str, no_system: bool = False) -> None:
    """회사가 준 코드 그대로: requests.post(url, headers=..., json=payload, timeout=...) 한 번. sbmig
    클라이언트를 거치지 않는다.

    The company snippet, verbatim: requests.post(url, headers=..., json=payload, timeout=...)."""
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
                                                               "limit, e.g. for one oversized project)"),
              token: Optional[str] = typer.Option(None, "--token",
                                                  help="access token (default: $SBMIG_WEB_TOKEN, also read from "
                                                       ".env); required when --host is not a loopback address")
              ) -> None:
    """웹 UI 를 띄운다(FastAPI + uvicorn): 프로젝트 zip 업로드 → 이관 실행 → 진행 실시간 보기 → 결과 내려받기.

    이관은 프로세스 안에서 한 번에 하나(나머지는 대기열), LangGraph 엔진으로 돈다. 127.0.0.1 이 아닌 주소로
    열려면 토큰(SBMIG_WEB_TOKEN 또는 --token)이 필요하다 — 없으면 네트워크의 누구나 result.zip(회사 소스)을
    받을 수 있어 시작을 거부한다.

    One-page web UI (FastAPI on uvicorn): upload a project zip, run the migration in-process (LangGraph engine,
    one worker thread per run), watch graph nodes / journal / log live (SSE), download report.md / result.zip.
    Same app without the CLI: `SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765`.

    Access (round 10): on 127.0.0.1 no token is needed. Any other --host (0.0.0.0 to share on the LAN) needs
    SBMIG_WEB_TOKEN (or --token): browsers open http://<host>:<port>/?token=<token> once (an HttpOnly cookie is set),
    API clients send `Authorization: Bearer <token>`. Without a token the server refuses to start on such a host."""
    import uvicorn

    from sbmig.core.config import PACKAGE_ROOT, load_dotenv_files
    from sbmig.web.app import (
        MAX_UPLOAD_ENV,
        TOKEN_ENV,
        create_app,
        describe_max_upload,
        is_loopback_host,
        resolve_max_upload,
    )

    load_dotenv_files()                       # .env is read once here for the server itself, then per run
    token = (token or os.environ.get(TOKEN_ENV, "")).strip() or None
    if not token and not is_loopback_host(host):
        typer.secho(f"refusing to serve on {host} without a token: anyone on the network could download result.zip "
                    f"(company source code) or upload a pom whose plugins Maven runs here. Set {TOKEN_ENV}=<a long "
                    f"random value> in .env or the shell (or pass --token), or bind to 127.0.0.1.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    root = Path(repo_root).resolve() if repo_root else PACKAGE_ROOT
    web_log = attach_file_log(Path(out) / "sbmig-web.log")
    typer.echo(f"sbmig web UI: http://{host}:{port}   runs: {Path(out).resolve()}   configs: {root / 'config'}   "
               f"upload limit: {describe_max_upload(resolve_max_upload(max_upload_mb))}")
    if token:
        typer.echo(f"access token required ({TOKEN_ENV}): open http://{host}:{port}/?token=<token> once per browser; "
                   f"API clients send 'Authorization: Bearer <token>'")
    env_limit = f" {MAX_UPLOAD_ENV}={max_upload_mb}" if max_upload_mb is not None else ""
    env_token = f" {TOKEN_ENV}=<token>" if token else ""
    typer.echo(f"(same app under plain uvicorn: SBMIG_WEB_OUT={out} SBMIG_WEB_REPO_ROOT={root}{env_limit}{env_token} "
               f"uvicorn sbmig.web.app:app --host {host} --port {port} --timeout-graceful-shutdown {graceful_timeout})")
    # shutdown: open SSE streams end (RunManager.shutdown via the lifespan), running Maven processes are killed, and
    # uvicorn stops waiting for connections after `graceful_timeout` — without it Ctrl+C hung on the browser tabs
    try:
        uvicorn.run(create_app(Path(out), root, max_upload_mb=max_upload_mb, token=token), host=host, port=port,
                    log_level="info", timeout_graceful_shutdown=graceful_timeout)
    finally:
        detach_file_log(web_log, None)


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
    """LLM 게이트웨이에 작은 요청 하나를 보내 응답을 그대로 보여 준다(새 게이트웨이에서 가장 먼저 돌린다).

    응답 본문을 못 꺼내면 원시 JSON 에 보이는 경로를 `llm.response_path` 로 준다. `--bare` 는 같은 payload 를
    맨 requests.post 로 보내 게이트웨이 문제와 클라이언트 문제를 가른다. 키는 출력에서 가려진다.

    Send one small request with the configured LLM provider and print what came back (raw JSON for HTTP
    providers, then the extracted reply). Run this first on a new gateway; if the reply cannot be extracted, set
    `llm.response_path` to the dot path shown in the raw JSON. `--bare` sends the identical payload with a bare
    `requests.post` (no session, no extra headers) so a gateway problem can be told apart from a client problem."""
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
    from sbmig.fix.company import probe_llm

    typer.echo(f"provider={llm.provider} model={llm.model} url={llm.base_url} key_env={llm.api_key_env}"
               f" payload_extra={llm.payload_extra} response_path={llm.response_path or '(auto)'}")
    # the same single call the fleet preflight makes (round 10); the verbose part below is only printed here
    res = probe_llm(cfg, prompt, no_system=no_system,
                    on_request=lambda payload: typer.echo("request payload: "
                                                          + json.dumps(payload, ensure_ascii=False)[:1500]))
    if res.response is not None:
        _echo_wire(res.response, llm.timeout_seconds)
        typer.echo(f"HTTP {res.status}")
        if res.data is not None:
            typer.echo(json.dumps(res.data, ensure_ascii=False, indent=2)[:4000])
        else:
            typer.echo(str(getattr(res.response, "text", ""))[:4000])
    if res.ok:
        if res.response is not None:
            typer.echo("extracted reply: " + repr(res.reply[:500]))
        else:
            typer.echo(f"reply ({res.client_name}): {res.reply[:500]!r}")
    else:
        label = {"config": "CONFIG ERROR", "transport": "TRANSPORT ERROR"}.get(res.kind, "PROBLEM")
        typer.echo(f"{label}: {res.problem}")
        for h in res.hints:
            typer.echo(f"hint: {h}")
    typer.echo(res.line())                          # short, ASCII: the line to retype when asking for help
    raise typer.Exit(0 if res.ok else 1)


# --------------------------------------------------------------------------- analyze / plan
@app.command()
def analyze(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
            as_json: bool = typer.Option(False, "--json", help="print the full profile as JSON")) -> None:
    """프로젝트를 분석만 하고(변경 없음) 프로필을 출력한다(종류, Java·Boot 버전, 라이브러리, 권고).

    Analyze a project (no changes) and print its profile."""
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
        typer.echo(f"  advisory {f.advisory_id}@{f.detected_version or '?'} — {f.title}"
                   f"{' → ' + f.replacement if f.replacement else ''}")


@app.command()
def plan(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
         as_json: bool = typer.Option(False, "--json")) -> None:
    """분석 + 계획만 하고(변경 없음) 이관 계획의 단계 목록을 출력한다.

    Analyze + plan (no changes) and print the migration plan."""
    from sbmig.analyze import analyze_project
    from sbmig.plan import build_plan

    cfg = _load(config)
    ws = _view_workspace(input, prefix="plan")
    profile = analyze_project(ws, cfg)
    p = build_plan(profile, cfg)
    if as_json:
        typer.echo(p.model_dump_json(indent=2))
        return
    typer.echo(f"plan for {profile.name} ({p.case_type.value}):")
    for i, s in enumerate(p.steps, 1):
        rec = f" [{', '.join(r.id for r in s.recipes)}]" if s.recipes else ""
        goal = f" mvn {s.build_goal}" if s.build_goal else ""
        tgt = f" @{s.target}" if s.target else ""
        typer.echo(f"  {i:2}. {s.id:<24} {s.kind.value:<15}{goal}{tgt}{rec}")
    for n in p.notes:
        typer.echo(f"  - {n}")


# --------------------------------------------------------------------------- inventory (pom-less webapps)
def inventory_console_lines(inv) -> list[str]:
    """inventory 결과를 운영자가 화면에서 옮겨 적을 수 있는
    12줄 이하로 줄인다(회사 PC 는 복사·붙여넣기가 안 된다).

    ≤ 12 lines an operator can retype from the screen (no paths longer than needed)."""
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
    """pom 없는 Tomcat 웹앱 트리를 읽기만 하고 설명서(inventory.md / .json)를 만든다.

    회사 결정(2026-10-23)으로 그런 트리는 `sbmig run` 이 거부하므로(exit 3), 이것은 소유 팀에 보낼 거부
    안내용이다. 아무것도 바꾸지 않는다.

    Read-only inventory of a Tomcat webapp tree WITHOUT a pom (JSPs, sources, WEB-INF/lib jars,
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
    """파싱된 빌드 오류를 표(종류, 위치, 메시지, L1 서명)로 찍는다."""
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
    """기존 디렉토리에서 Maven 을 돌려 파싱된 오류와 L1 서명을 출력한다(디버깅용).

    Run Maven on an existing directory and print the parsed errors + signatures (debugging aid)."""
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
    """이미 있는 Maven 로그를 구조화된 오류 목록으로 파싱한다.

    Parse an existing Maven log into structured errors."""
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
    """기존 디렉토리에 OpenRewrite 레시피를 돌린다(--dry-run 이 아니면 제자리에서 바꾼다).

    Run OpenRewrite recipes on an existing directory (in place unless --dry-run)."""
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


@app.command("clean")
def clean_cmd(out: Path = typer.Option(Path("out"), "--out", "-o",
                                       help="the --out of `sbmig run` / `sbmig serve` (e.g. out/web)"),
              fleet: Optional[Path] = typer.Option(None, "--fleet",
                                                   help="a fleet's --out directory: only that fleet's run copies"),
              older_than: float = typer.Option(0.0, "--older-than", min=0.0,
                                               help="only what finished more than DAYS ago (0 = every finished run)"),
              keep_failed: bool = typer.Option(False, "--keep-failed",
                                               help="keep runs that did not end green (exit code not 0)"),
              dry_run: bool = typer.Option(False, "--dry-run", help="list what would be freed, delete nothing"),
              records: Optional[List[Path]] = typer.Option(
                  None, "--records", help="LLM record directory (repeatable; default: llm.record_dir / LLM_RECORD_DIR)"),
              config: Optional[List[Path]] = ConfigOpt) -> None:
    """디스크를 비운다: 끝난 실행의 프로젝트 복사본, 웹 UI 업로드 zip, LLM 기록 파일.

    result.zip·report·changes.patch·logs 는 남기고, 실행 중·대기
    중인 실행은 건드리지 않는다. --dry-run 은 지울 것만 보여 준다.

    Free disk space: the project copies of FINISHED runs (`<run dir>/project`: working tree + git history;
    result.zip, report.md / .json, changes.patch, logs stay), the web UI's uploaded zips (`<out>/_uploads/`) of
    finished runs and stale partial uploads, and the LLM records (`llm-NNNN.json`). Running / queued runs are never
    touched. Prints what it frees (or would free with --dry-run)."""
    from sbmig.fleet.cleanup import execute, human, plan_clean, summarize

    target = Path(fleet) if fleet else Path(out)
    if not target.is_dir():
        _fail(f"no such directory: {target}", 2)
    if records:
        record_dirs = [Path(r) for r in records]
    else:
        cfg = _load(config) if config else _load(None, overrides=dict(_PROBE_ARTIFACTS_STUB))
        record_dirs = [cfg.resolve_path(cfg.llm.record_dir)] if (cfg.llm.record_dir or "").strip() else []
    items = plan_clean(target, older_than_days=older_than, keep_failed=keep_failed, fleet=fleet is not None,
                       record_dirs=record_dirs)
    total = sum(i.size for i in items)
    scope = (f"older than {older_than:g} day(s)" if older_than else "every finished run") \
        + (", keeping failed runs" if keep_failed else "")
    typer.echo(f"clean: {target.resolve()} ({scope}){' - dry run' if dry_run else ''}")
    for line in summarize(items):
        typer.echo(line)
    if dry_run:
        for i in items[:20]:
            typer.echo(f"    {i.kind:<15} {human(i.size):>9}  {i.path}")
        if len(items) > 20:
            typer.echo(f"    ... {len(items) - 20} more")
        typer.echo(f"would free {human(total)} ({len(items)} item(s)); nothing deleted")
        return
    freed = execute(items)
    typer.echo(f"freed {human(freed)} ({len(items)} item(s))")
    log.info("sbmig clean %s: freed %s (%d item(s))", target, human(freed), len(items), extra=NO_CONSOLE)


@app.command()
def version() -> None:
    """sbmig 버전을 출력한다.

    Print the sbmig version."""
    from sbmig import __version__

    typer.echo(f"sbmig {__version__}")


# --------------------------------------------------------------------------- sub-apps (kb, recipes, fleet)
def _mount(module: str, attr: str, name: str, help_text: str) -> None:
    """하위 명령(kb, recipes, fleet, advisories)을 붙인다. 그 패키지 import 가 실패해도 `sbmig --help` 는
    돌고, 쓰려 할 때 이유를 알려 준다.

    Mount a typer sub-app; if its package fails to import, keep `sbmig --help` working and explain on use."""
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
       "knowledge base (kb-v2/: errors.jsonl + cases/<case>/): errors list|show | cases "
       "list|show|retire|activate|pin|unpin | stats | validate | export | import | reindex")
_mount("sbmig.plan.catalog_cli", "recipes_cli", "recipes",
       "recipe catalog (knowledge/recipes/catalog.yml): list | show | check | sets")
_mount("sbmig.fleet.cli", "fleet_cli", "fleet",
       "one agent, many projects, sequential with KB learning: run | resume | status [--attention]")
_mount("sbmig.advisory.cli", "advisories_cli", "advisories",
       "library advisories (knowledge/libraries/advisories.yml): list | show | check")


def main() -> None:  # pragma: no cover
    """콘솔 스크립트 진입점(`sbmig`)."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
