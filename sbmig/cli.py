"""운영자가 쓰는 `sbmig` 명령을 모두 정의하는 모듈(Typer).

Commands: run (one project), fleet (batch), serve (web UI), llm-probe (gateway check), repo-probe (repository
lookup), analyze / plan (read-only views), inventory (describes a pom-less tree), build / parse-log / rewrite
(debugging), clean (disk cleanup), kb / recipes / advisories (sub-apps), version.
The first paragraph of a command's docstring is its `sbmig --help` text, so its first Korean line stays short.
No square brackets in docstrings: rich may read them as markup.
Heavy imports live inside the functions so that `sbmig --help` works even when a package is broken.
Every run writes sbmig.log (DEBUG) into its run dir and ends with one ASCII SBMIG-RESULT line for the
operator.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional

import typer

from sbmig.core.stage import stage_formatter

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="JDK 21 + Spring Boot 4.1 migration agent (OpenRewrite + triage + LLM build-fix loop).")
log = logging.getLogger("sbmig.cli")

ConfigOpt = typer.Option(None, "--config", "-c", help="extra config yml (repeatable; later wins)")


LOG_FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(stage)s] %(name)s: %(message)s"   # stage는 파이프라인 단계


from sbmig.core.diag import dump_threads, install_thread_dump_signal  # noqa: E402,F401 - 다시 내보낸다


def harden_console_streams() -> None:
    """콘솔이 출력하지 못하는 문자가 나와도 죽지 않게 한다.
    stdout / stderr와 콘솔 로그 스트림을 errors="replace"로 바꾼다. 콘솔 인코딩(한국어 Windows의 cp949)은 그대로 둔다.
    출력을 리다이렉트하거나 작업 스케줄러로 실행할 때 UnicodeEncodeError가 나지 않게 하려는 것이다.

    Without it, cp949 output of a non-encodable character (✅ in the console summary, an em dash in a log line)
    made `sbmig run` exit 1 after the report was written, or printed `--- Logging error ---`."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if (getattr(stream, "errors", None) or "strict") == "strict":
                reconfigure(errors="replace")
        except (ValueError, OSError, AttributeError):   # 분리되었거나 닫힌 스트림은 보호할 것이 없다
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
    """`extra={"sbmig_console": False}`로 남긴 로그를 콘솔에는 내지 않고 파일에만 보내는 필터 클래스.
    SBMIG-RESULT나 일괄 실행 진행 상황은 typer.echo로 콘솔에 이미 출력했기 때문이다."""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "sbmig_console", True) is not False


NO_CONSOLE = {"sbmig_console": False}


def setup_logging(verbose: bool = False) -> None:
    """콘솔 로그를 설정한다. rich로 INFO 이상을 출력하고, `-v`면 DEBUG까지 출력한다.
    줄마다 단계 태그를 붙인다. sbmig 로거는 DEBUG라서 파일 로그는 항상 모든 줄을 받는다.
    스레드 덤프 시그널도 여기서 등록한다."""
    from rich.logging import RichHandler

    harden_console_streams()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("sbmig")
    root.setLevel(logging.DEBUG)                       # 걸러 내는 것은 핸들러다. 콘솔은 INFO, 파일은 항상 DEBUG
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        # 출력 마스킹이 켜져 있으면 rich traceback을 끈다. rich가 그리는 예외는 마스킹을 거치지 않는다
        from sbmig.core.scrub import output_scrubbing
        h = RichHandler(rich_tracebacks=not output_scrubbing(), show_path=False, markup=False, show_time=True,
                        show_level=True)
        h.setFormatter(stage_formatter("[%(stage)s] %(message)s", datefmt=None))   # stage는 파이프라인 단계
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
    """한 실행의 로그만 통과시키는 필터 클래스.
    그 실행의 스레드와, 그 컨텍스트를 물려받은 스레드(LangGraph 노드 실행 스레드)의 로그만 받는다.
    웹 서버에서 동시에 실행되는 run끼리 sbmig.log가 섞이지 않게 한다.

    A graph engine running a node in an executor thread copies the context, so `_LOG_THREAD` still names the
    run's thread."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id or _LOG_THREAD.get() == self.thread_id


def attach_file_log(path: Path, thread_id: Optional[int] = None, level: int = logging.DEBUG) -> Path:
    """sbmig 로그를 ``path`` 파일에도 남기기 시작하고 그 경로를 반환한다. 같은 파일과 스레드면 다시 달지 않는다.
    - 기본 수준은 DEBUG다. LLM 호출, 빌드, 결정을 남기고 키는 남기지 않는다
    - ``thread_id``를 주면 그 실행의 로그만 이 파일로 간다. 웹 서버가 실행마다 sbmig.log를 따로 쓸 때 쓴다
    - 일괄 실행의 fleet/sbmig.log는 ``level``을 INFO로 준다. 프로젝트마다 DEBUG sbmig.log가 따로 있다"""
    path = Path(path).resolve()
    root = logging.getLogger("sbmig")
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path \
                and getattr(h, "sbmig_thread", None) == thread_id:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if root.level == logging.NOTSET or root.level > logging.DEBUG:   # 파일은 모든 줄을 받고 콘솔은 원래 수준을 지킨다
        for h in root.handlers:
            if h.level == logging.NOTSET:
                h.setLevel(logging.INFO if root.level in (logging.NOTSET, logging.INFO) else root.level)
        root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(level)
    # datefmt는 바꾸지 않는다. 웹 스트림이 기본 asctime 형식으로 로그 줄을 정렬한다(sbmig.web.runs._event_time)
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
    """attach_file_log가 ``path``에 단 파일 핸들러를 떼고 닫는다. 뗀 개수를 반환한다.
    ``thread_id``를 주지 않으면 모두 떼고, 주면 그 스레드의 것만 뗀다. None은 프로세스 전체용 핸들러다."""
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


def install_redaction_if_requested() -> bool:
    """`SBMIG_REDACT_OUTPUT=1`이면 출력 마스킹(`sbmig.core.scrub`)을 켠다. 켜져 있는지를 반환한다.
    마스킹은 로그 줄, 저널 줄, 처리되지 않은 예외의 traceback에서 비밀 값을 쓰는 순간 지운다.
    이미 켜져 있으면 그대로 둔다. 요청했는데 켤 수 없으면 예외를 그대로 던진다. 마스킹 없이 실행하지 않는다."""
    from sbmig.core.scrub import install_scrubbed_excepthooks, output_scrubbing, redaction_requested

    if output_scrubbing():
        return True
    if not redaction_requested():
        return False
    from sbmig.web.redact import install_output_redaction

    install_output_redaction()
    install_scrubbed_excepthooks()
    return True


def final_redaction(run_dir: Path) -> int:
    """실행 폴더의 텍스트 파일에서 비밀 값을 한 번 더 지우고, 바뀐 파일 수를 반환한다.
    쓰는 순간 마스킹되지 않는 파일(`logs/`의 Maven 출력, 보고서)과 실행 중에 받은 토큰을 위한 마지막 정리다.
    출력 마스킹이 꺼져 있으면 아무것도 하지 않고 0을 반환한다. 실패해도 로그만 남기고 0을 반환한다."""
    from sbmig.core.scrub import output_scrubbing

    if not output_scrubbing():
        return 0
    try:
        from sbmig.web.redact import redact_run_dir

        n = redact_run_dir(run_dir)
        log.debug("output redaction: the final pass rewrote %d file(s) under %s", n, run_dir)
        return n
    except Exception:  # noqa: BLE001 - 마지막 정리라서 실패는 기록만 한다. 결과 줄은 그대로 출력한다
        log.exception("output redaction: the final pass over %s failed", run_dir)
        return 0


# 모든 명령 전에 실행되는 콜백이다. 콘솔 인코딩 보호, 출력 마스킹, 로그 설정, --log-file을 맡는다.
# docstring은 달지 않는다. 달면 Typer 앱 도움말이 바뀐다
@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="debug logging"),
          log_file: Optional[Path] = typer.Option(None, "--log-file",
                                                  help="also append DEBUG logs to this file (runs always log to "
                                                       "<run dir>/sbmig.log)")) -> None:
    harden_console_streams()
    try:
        install_redaction_if_requested()     # 핸들러보다 먼저 호출한다. 이 결과에 따라 rich traceback을 쓸지 정한다
    except Exception as e:  # noqa: BLE001
        typer.echo(f"ERROR: SBMIG_REDACT_OUTPUT=1 but the output redaction could not be installed "
                   f"({type(e).__name__}) - refusing to run without it", err=True)
        raise typer.Exit(EXIT_ABORTED) from None
    setup_logging(verbose)
    if log_file:
        attach_file_log(log_file)


def _load(configs: Optional[List[Path]], overrides: Optional[dict] = None, repo_root: Optional[Path] = None):
    """설정을 읽는다. 검증 오류면 무엇이 틀렸는지 출력하고 exit 2로 끝낸다.
    `--config config/artifacts.yml`을 빠뜨린 경우가 흔하다. 회사 artifacts 섹션에는 기본값이 없다."""
    from pydantic import ValidationError

    from sbmig.core.config import load_config

    try:
        return load_config(*(configs or []), overrides=overrides or None, repo_root=repo_root)
    except ValidationError as e:
        missing = [".".join(str(x) for x in err.get("loc", ())) for err in e.errors() if err.get("type") == "missing"]
        typer.echo(f"config error: {e.error_count()} problem(s) in the merged configuration", err=True)
        for err in e.errors()[:10]:
            typer.echo(f"  {'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}", err=True)
        if any(m == "artifacts" or m.startswith("artifacts.") for m in missing):
            typer.echo("hint: pass the company config: --config config/artifacts.yml "
                       "(copy config/artifacts.example.yml and fill in parent / mandatory / optional; "
                       "config/artifacts.local-e2e.yml works for a local try)", err=True)
        raise typer.Exit(2)


# --config 없이 실행한 llm-probe가 쓰는 artifacts 값. 점검은 .env만 쓴다. 필수 검증만 통과시키려는 값이다
_PROBE_ARTIFACTS_STUB = {"artifacts": {"parent": {"group_id": "sbmig.probe", "artifact_id": "no-config", "version": "0"}}}


def _view_workspace(input_path: Path, out: Optional[Path] = None, prefix: str = "view"):
    """analyze / plan / build용으로 기존 디렉토리 위에 복사 없이 작업 공간을 만든다.
    zip이면 임시 디렉토리에 푼다."""
    from sbmig.core.workspace import Workspace, new_run_id

    run_id = new_run_id(prefix)
    base = Path(out) if out else Path(tempfile.mkdtemp(prefix="sbmig-"))
    if input_path.is_dir():
        return Workspace(input_path, base / run_id, run_id)
    return Workspace.from_input(input_path, base, run_id=run_id)


def refuse_intake(orch, reason: str, flow: str = "full"):
    """파이프라인 시작 전에 접수에서 거절한 실행을 마무리하고 보고서를 반환한다.
    보고서에 ``run aborted: <reason>``을 남기고 Maven이나 LLM은 호출하지 않는다.

    Used e.g. for an uploaded ZIP over an unpacking limit. Same abort record as the orchestrator's own intake
    refusals (flag `aborted`, report, exit 3)."""
    with orch._run_guard(False, flow=flow) as report:
        orch._abort(report, reason)
    return report


# 종료 코드. 운영자, 일괄 실행, 작업 스케줄러가 이 값으로 결과를 본다. 0 성공, 2 미완, 3 중단
EXIT_SUCCESS = 0          # 최종 `mvn clean package`가 통과했고 차단된 것이 없다
EXIT_INCOMPLETE = 2       # 끝났지만 차단되었거나 해결하지 못한 오류가 남았다(report.md 참고)
EXIT_ABORTED = 3          # 중단: Maven 없음, Maven 프로젝트 아님, 설정한 부모 pom을 찾지 못함, 내부 오류


def run_exit_code(report) -> int:
    """RunReport를 종료 코드 0 / 2 / 3으로 바꾼다. report.md의 SBMIG-RESULT 줄도 같은 규칙을 쓴다."""
    from sbmig.report.result_line import report_exit_code

    return report_exit_code(report)


def _fail(msg: str, code: int = 1) -> None:
    """메시지를 빨간 글씨로 stderr에 출력하고 그 코드로 끝낸다."""
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
                                             help="intake|analyze|plan|transform|restructure|verify (v2; jdk21|spring are v1 phases)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="compute and journal decisions, apply nothing"),
    flow: str = typer.Option("full", "--flow",
                             help="full (migration, then the chained remediation when switched on) | remediation "
                                  "(no migration: remediate the project as uploaded)"),
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
    """프로젝트 하나를 끝까지 이관한다(변환, 재구성, 구조 검토, 빌드와 수정 루프, 보고서).

    종료 코드 0은 최종 `mvn clean package` 성공에 차단 없음, 2는 차단이나 미해결 오류가 남은 채 끝남(report.md의 오류
    목록), 3은 중단(Maven 없음, Maven 프로젝트 아님, 사내 부모 pom 해석 불가, 내부 오류)이다.
    `--flow remediation`이면 이관 없이 취약점 조치만 한다.
    운영자는 마지막 SBMIG-RESULT 줄로 결과를 확인한다.

    Migrate a project end to end.

    Exit code 0 = final `mvn clean package` passed and nothing is blocked; 2 = finished with blocked and/or
    unresolved errors (see report.md "Error list" / "Blocked"); 3 = aborted (no Maven, not a Maven project,
    configured parent POM not resolvable, internal error).

    `--flow remediation`: no migration — intake without project edits, then the vulnerability remediation of the
    project as uploaded (`remediation/`); exit 0 = SUCCEEDED, 2 = any other saved status, 3 = not started. It takes
    neither `--stop-after` nor `--dry-run`."""
    if verbose:
        setup_logging(True)
    from sbmig.loop import check_flow, parse_stop_after

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
    try:
        phase = parse_stop_after(stop_after)
    except ValueError as e:
        _fail(f"--{e}")
    try:
        flow = check_flow(flow, phase, dry_run)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="'--flow'") from None
    cfg = _load(config, overrides, repo_root)
    from sbmig.core.workspace import Workspace
    from sbmig.loop import Orchestrator
    from sbmig.report import console_summary
    from sbmig.report.result_line import report_result_line, result_line

    from sbmig.core.workspace import ZipLimits

    ws = Workspace.from_input(input, out, run_id=run_id, limits=ZipLimits.from_config(cfg.intake))
    log_path = attach_file_log(ws.run_dir / "sbmig.log")
    log.info("run %s", ws.run_id)
    orch = Orchestrator(cfg, ws, source=input)
    try:
        if ws.intake_refusal:
            # 풀기 한도를 넘어 아무것도 풀지 않은 zip이다. 다른 접수 거절처럼 보고서를 쓰고 종료 코드 3으로 끝낸다
            report = refuse_intake(orch, ws.intake_refusal, flow=flow)
        else:
            # flow=는 기본 흐름이 아닐 때만 넘긴다. 사내 래퍼가 이 인자를 모를 수 있다
            report = orch.run(stop_after=phase, dry_run=dry_run, **({"flow": flow} if flow != "full" else {}))
    except Exception:
        log.exception("run aborted by an unexpected error (see %s)", log_path)
        line = result_line(ws.run_id, 1, False, attention=["crash"])
        typer.echo(line)
        log.info("%s", line, extra=NO_CONSOLE)
        final_redaction(ws.run_dir)
        raise
    final_redaction(ws.run_dir)                      # SBMIG_REDACT_OUTPUT=1일 때만 동작한다(컨테이너 실행)
    console_summary(report)
    typer.echo(f"log: {log_path}   (share this file + report.md when asking for help)")
    code = run_exit_code(report)
    line = report_result_line(report, code)          # 운영자가 옮겨 적거나 grep하는 ASCII 한 줄
    typer.echo(line)
    log.info("%s", line, extra=NO_CONSOLE)
    raise typer.Exit(code)


REMEDIATION_MARK = "SBMIG-REMEDIATION"      # 취약점 조치를 이어 간 실행의 결과 줄 맨 앞에 붙는 표시. 이관 결과 줄은 SBMIG-RESULT로 시작한다


@app.command("remediation-resume")
def remediation_resume(
    run_dir: Path = typer.Option(..., "--run-dir", exists=True, file_okay=False, help="the migration run directory"),
    config: Optional[List[Path]] = ConfigOpt,
    repo_root: Optional[Path] = typer.Option(None, "--repo-root", help="sbmig repo root (kb/, knowledge/)"),
) -> None:
    """이관 뒤에 이어서 실행한 취약점 조치를 그 실행 폴더에서 다시 이어 간다(``chain.resume``).
    새 빌드 파드의 Worker가 호출한다.

    로그는 실행 폴더의 sbmig.log에, 저널은 journal.jsonl에 덧붙이고 이관의 보고서와 결과 줄은 건드리지 않는다. 실행
    폴더에 stop.request가 생기면 다음 단계 경계에서 멈춘다. 끝에 SBMIG-REMEDIATION 줄을 출력한다.
    종료 코드 0은 조치가 어떤 상태로든 끝났다는 뜻이고(결과는 remediation/state.json), 3은 이어 갈 수 없는
    상태이거나 조치 기능을 불러오지 못했다는 뜻이다.

    Continue a chained remediation in its migration run directory (`chain.resume`; called by the Worker of a new build pod).
    Exit 0 = the remediation reached a state (see remediation/state.json), 3 = not resumable / not available."""
    from sbmig.core.scrub import output_scrubbing, scrub_json
    from sbmig.core.workspace import STOP_REQUEST_FILE

    cfg = _load(config, None, repo_root)
    run_dir = run_dir.resolve()
    run_id = run_dir.name
    log_path = attach_file_log(run_dir / "sbmig.log")
    log.info("remediation resume %s (new build pod)", run_id)

    def journal(event: dict) -> None:
        # Workspace.journal과 같은 형식(ts, run_id)으로 쓴다. 웹이 이 파일을 따라 읽는다
        # 마스킹이 켜져 있으면 값 단위로 지운다
        from datetime import datetime, timezone
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id, **event}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        if output_scrubbing():
            line = json.dumps(scrub_json(json.loads(line)), ensure_ascii=False)
        with open(run_dir / "journal.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def stop() -> bool:
        return (run_dir / STOP_REQUEST_FILE).is_file()

    def finish(status: str, reason: str, code: int) -> None:
        final_redaction(run_dir)
        line = f"{REMEDIATION_MARK} run={run_id} status={status} reason={reason or '-'}"
        typer.echo(line)
        log.info("%s", line, extra=NO_CONSOLE)
        typer.echo(f"log: {log_path}")
        raise typer.Exit(code)

    try:
        from sbmig.remediations import chain
        from sbmig.remediations.models import StateConflict
    except ImportError as e:
        log.error("remediation resume %s: the remediation package is not available (%s)", run_id, type(e).__name__)
        finish("NOT_AVAILABLE", "import_error", EXIT_ABORTED)
    try:
        st = chain.resume(run_dir, cfg, journal=journal, stop=stop)
    except StateConflict as e:
        log.error("remediation resume %s refused: %s", run_id, e)
        finish("REFUSED", "state_conflict", EXIT_ABORTED)
    finish(st.status, st.reason, 0)


def _mask_headers(headers) -> dict:
    """화면에 출력할 헤더 복사본을 만든다.
    Authorization과 API 키 값은 앞 10자와 길이만 남긴다. 키가 화면이나 로그에 나가지 않게 한다."""
    out = {}
    for k, v in dict(headers or {}).items():
        v = str(v)
        if k.lower() in ("authorization", "x-api-key", "api-key"):
            v = v[:10] + "…" + f"({len(v)} chars)"
        out[k] = v
    return out


def _echo_wire(resp, timeout) -> None:
    """실제로 보낸 요청, 받은 응답 헤더, 프록시, 타임아웃을 출력한다.
    잘 동작하는 다른 클라이언트와 비교할 때 쓴다. 요청 헤더의 토큰은 가린다."""
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
    """점검용 메시지 목록(sbmig.fix.company.probe_messages)을 반환한다."""
    from sbmig.fix.company import probe_messages

    return probe_messages(prompt, no_system)


def _probe_bare(cfg, prompt: str, no_system: bool = False) -> None:
    """sbmig 클라이언트를 거치지 않고 회사가 준 코드 그대로 requests.post를 한 번 호출한다.
    결과를 출력한다. HTTP 2xx면 exit 0, 아니면 exit 1로 끝낸다."""
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
                                                       ".env); required when --host is not a loopback address "
                                                       "unless --open"),
              open_network: bool = typer.Option(False, "--open",
                                                help="serve a non-loopback --host WITHOUT a token: every client is "
                                                     "accepted (trusted intranet only; default: $SBMIG_WEB_OPEN=1)"),
              mode: Optional[str] = typer.Option(None, "--mode",
                                                 help="in-process (default) | k8s-job: one Kubernetes Job per upload, zip / "
                                                      "results / KB on the shared volume | local-job: the same flow with a local "
                                                      "subprocess as the pod (development; k8s-job falls back to it outside a "
                                                      "cluster) (default: $SBMIG_WEB_MODE)"),
              config: Optional[List[str]] = typer.Option(None, "--config", "-c",
                                                         help="config/*.yml name every upload runs with (repeatable; "
                                                              "default: $SBMIG_WEB_CONFIGS, else artifacts.yml / "
                                                              "artifacts.local-e2e.yml when present). The page "
                                                              "offers the full list only under 고급 설정")
              ) -> None:
    """웹 UI를 띄운다(FastAPI와 uvicorn).
    프로젝트 zip 업로드, 이관 실행, 진행 상황 보기, 결과 내려받기를 제공한다.

    이관은 LangGraph 엔진으로 실행하며 프로세스 안에서는 한 번에 하나, Job 모드(--mode k8s-job /
    local-job)에서는 SBMIG_JOB_MAX_PARALLEL개까지(기본 1) 실행하고 나머지는 올린 순서대로 대기열에 둔다.
    127.0.0.1이 아닌 주소로 열려면 토큰(SBMIG_WEB_TOKEN 또는 --token)이 필요하다.
    토큰이 없으면 네트워크의 누구나 result.zip(회사 소스)을 받을 수 있으므로 시작을 거부한다.
    사내망 전용 페이지는 --open(또는 SBMIG_WEB_OPEN=1)으로 토큰 없이 열고 시작할 때 경고 한 줄을 남긴다.

    One-page web UI (FastAPI on uvicorn): upload a project zip, run the migration in-process (LangGraph engine,
    one worker thread per run), watch graph nodes / journal / log live (SSE), download report.md / result.zip.
    Same app without the CLI: `SBMIG_WEB_OUT=out/web uvicorn sbmig.web.app:app --port 8765`.

    Access (round 10): on 127.0.0.1 no token is needed. Any other --host (0.0.0.0 to share on the LAN) needs
    SBMIG_WEB_TOKEN (or --token): browsers open http://<host>:<port>/?token=<token> once (an HttpOnly cookie is set),
    API clients send `Authorization: Bearer <token>`. Without a token the server refuses to start on such a host —
    unless `--open` / `SBMIG_WEB_OPEN=1` (2026-11-05: the company's page lives on its intranet only)."""
    import uvicorn

    from sbmig.core.config import PACKAGE_ROOT, load_dotenv_files
    from sbmig.web.app import (
        CONFIGS_ENV,
        MAX_UPLOAD_ENV,
        OPEN_ENV,
        TOKEN_ENV,
        create_app,
        describe_max_upload,
        is_loopback_host,
        open_network_requested,
        resolve_max_upload,
    )

    load_dotenv_files()                       # 서버용으로 여기서 한 번 읽고, 실행마다 다시 읽는다
    token = (token or os.environ.get(TOKEN_ENV, "")).strip() or None
    open_network = bool(open_network or open_network_requested())
    if not token and not open_network and not is_loopback_host(host):
        typer.secho(f"refusing to serve on {host} without a token: anyone on the network could download result.zip "
                    f"(company source code) or upload a pom whose plugins Maven runs here. Set {TOKEN_ENV}=<a long "
                    f"random value> in .env or the shell (or pass --token), bind to 127.0.0.1, or — on a trusted "
                    f"intranet only — pass --open / set {OPEN_ENV}=1.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    root = Path(repo_root).resolve() if repo_root else PACKAGE_ROOT
    web_log = attach_file_log(Path(out) / "sbmig-web.log")
    # None이면 $SBMIG_WEB_CONFIGS, 그다음 기본 이름을 쓴다
    default_configs = [c for c in (config or []) if c] or None
    typer.echo(f"sbmig web UI: http://{host}:{port}   runs: {Path(out).resolve()}   configs: {root / 'config'}   "
               f"upload limit: {describe_max_upload(resolve_max_upload(max_upload_mb))}")
    if token:
        typer.echo(f"access token required ({TOKEN_ENV}): open http://{host}:{port}/?token=<token> once per browser; "
                   f"API clients send 'Authorization: Bearer <token>'")
    elif open_network and not is_loopback_host(host):
        typer.secho(f"no access token: the UI is open to everyone who reaches http://{host}:{port} (--open / {OPEN_ENV}) "
                    f"— trusted intranet only", fg=typer.colors.YELLOW)
    env_limit = f" {MAX_UPLOAD_ENV}={max_upload_mb}" if max_upload_mb is not None else ""
    env_token = f" {TOKEN_ENV}=<token>" if token else (f" {OPEN_ENV}=1" if open_network else "")
    env_cfg = f" {CONFIGS_ENV}={','.join(default_configs)}" if default_configs else ""
    typer.echo(f"(same app under plain uvicorn: SBMIG_WEB_OUT={out} SBMIG_WEB_REPO_ROOT={root}{env_limit}{env_token}"
               f"{env_cfg} uvicorn sbmig.web.app:app --host {host} --port {port} --timeout-graceful-shutdown "
               f"{graceful_timeout})")
    # 종료할 때 graceful_timeout이 지나면 열린 연결을 더 기다리지 않는다. 기다리면 SSE 스트림 때문에 Ctrl+C가 멈춘다
    # SSE 종료와 Maven 프로세스 정리는 lifespan의 RunManager.shutdown이 한다
    try:
        uvicorn.run(create_app(Path(out), root, max_upload_mb=max_upload_mb, token=token,
                               default_configs=default_configs, open_network=open_network, mode=mode),
                    host=host, port=port, log_level="info", timeout_graceful_shutdown=graceful_timeout)
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
    """LLM 게이트웨이에 작은 요청을 하나 보내 응답을 그대로 보여 준다. 새 게이트웨이에서 가장 먼저 실행한다.

    응답 본문을 꺼내지 못하면 원시 JSON에 보이는 경로를 `llm.response_path`로 지정한다.
    `--bare`는 같은 payload를 requests.post로만 보내 게이트웨이 문제와 클라이언트 문제를 구분한다.
    키는 출력에서 가려진다.

    Send one small request with the configured LLM provider and print what came back (raw JSON for HTTP
    providers, then the extracted reply). Run this first on a new gateway; if the reply cannot be extracted, set
    `llm.response_path` to the dot path shown in the raw JSON. `--bare` sends the identical payload with a bare
    `requests.post` (no session, no extra headers) so a gateway problem can be told apart from a client problem."""
    overrides: dict = {"llm": {}}
    if llm_provider:
        overrides["llm"]["provider"] = llm_provider
    if model:
        overrides["llm"]["model"] = model
    overrides.update(_PROBE_ARTIFACTS_STUB)        # 점검은 `artifacts`를 읽지 않는다
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
        cfg.llm.payload_extra = extra               # 합치지 않고 통째로 바꾼다. 합치면 need_origin 같은 키를 뺄 수 없다
    llm = cfg.llm
    if bare:
        _probe_bare(cfg, prompt, no_system=no_system)
        return
    from sbmig.fix.company import probe_llm

    typer.echo(f"provider={llm.provider} model={llm.model} url={llm.base_url} key_env={llm.api_key_env}"
               f" payload_extra={llm.payload_extra} response_path={llm.response_path or '(auto)'}")
    gw = str(getattr(llm, "gateway", "legacy") or "legacy")
    if gw != "legacy" or getattr(llm, "token_url", None):            # agent 게이트웨이나 토큰 교환을 쓰는 경우
        from sbmig.fix.client import agent_url
        try:
            call_url = agent_url(llm.base_url, getattr(llm, "asset_id", None) or None)
        except Exception as e:  # noqa: BLE001 - 화면에만 보여 준다. 설정 오류는 아래 점검이 직접 보고한다
            call_url = f"(unresolved: {e})"
        typer.echo(f"gateway={gw} url={call_url} asset_id={getattr(llm, 'asset_id', None) or '(unset)'} "
                   f"token_url={getattr(llm, 'token_url', None) or '(none: static key)'} "
                   f"app_token_env={getattr(llm, 'app_token_env', '')} escalation_model={llm.escalation_model or '(unset)'}"
                   + (" payload_extra: legacy only (not sent in agent mode)" if gw == "agent" and llm.payload_extra else ""))
    # 일괄 실행의 사전 점검과 같은 호출 한 번이다. 아래의 자세한 출력은 이 명령에서만 한다
    res = probe_llm(cfg, prompt, no_system=no_system,
                    on_request=lambda payload: typer.echo("request payload: "
                                                          + json.dumps(payload, ensure_ascii=False)[:1500]))
    if getattr(res, "token", ""):
        typer.echo(f"token exchange: {res.token}")
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
        label = {"config": "CONFIG ERROR", "transport": "TRANSPORT ERROR", "token": "TOKEN ERROR"}.get(res.kind, "PROBLEM")
        typer.echo(f"{label}: {res.problem}")
        for h in res.hints:
            typer.echo(f"hint: {h}")
    typer.echo(res.line())                          # 문의할 때 옮겨 쓰는 짧은 ASCII 한 줄
    raise typer.Exit(0 if res.ok else 1)


# --------------------------------------------------------------------------- analyze / plan
@app.command()
def analyze(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
            as_json: bool = typer.Option(False, "--json", help="print the full profile as JSON")) -> None:
    """프로젝트를 변경 없이 분석만 하고 프로필(종류, Java와 Boot 버전, 라이브러리, 권고)을 출력한다.

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
    except Exception as e:  # 권고를 계산하지 못해도 `analyze`는 끝까지 출력한다
        log.warning("advisories unavailable: %s", e)
        findings = []
    for f in findings:
        typer.echo(f"  advisory {f.advisory_id}@{f.detected_version or '?'} — {f.title}"
                   f"{' → ' + f.replacement if f.replacement else ''}")


@app.command()
def plan(input: Path = typer.Argument(..., exists=True), config: Optional[List[Path]] = ConfigOpt,
         as_json: bool = typer.Option(False, "--json")) -> None:
    """변경 없이 분석과 계획만 하고 이관 계획의 단계 목록을 출력한다.

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


# --------------------------------------------------------------------------- inventory (pom 없는 웹앱)
def inventory_console_lines(inv) -> list[str]:
    """inventory 결과를 콘솔에 출력할 12줄 이하의 요약으로 만든다.
    회사 PC에서는 복사와 붙여넣기가 안 된다. 운영자가 화면을 보고 옮겨 적을 수 있는 분량으로 줄인다."""
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
    if getattr(inv, "adf", None) is not None and getattr(inv.adf, "families", None):   # ADF 프로젝트는 변환하지 않는다
        from sbmig.analyze.adf import adf_sizing_lines
        lines.append("  " + adf_sizing_lines(inv.adf)[0])
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

    그런 트리는 `sbmig run`이 거부하므로(exit 3) 소유 팀에 보낼 거부 안내를 만들기 위해 쓴다.
    아무것도 바꾸지 않는다.
    build.xml이 있는 Ant 프로젝트는 ``intake.ant_to_maven``이 켜져 있을 때만 `sbmig run`이 접수에서 pom.xml을 만들어 받는다.

    Read-only inventory of a Tomcat webapp tree WITHOUT a pom (JSPs, sources, WEB-INF/lib jars,
    WEB-INF/classes): webapp roots, source roots, jar coordinates guessed from the file names
    (knowledge/libraries/jar_names.yml), Java level of the class files, one Maven module proposal per webapp.
    Writes inventory.md (report.language) + inventory.json. Changes nothing; exit 0 (3 when the path is missing).
    Ant projects (build.xml) are refused by `sbmig run` (exit 3) unless `intake.ant_to_maven` is true, in which
    case a pom is synthesized at intake; trees with neither pom.xml nor build.xml are always rejected."""
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
        if tmp is not None:                    # zip을 푼 임시 디렉토리는 곧 지운다. 경로는 zip 경로로 보고한다
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


# ------------------------------------------------------------------- 디버깅용 build / parse-log / rewrite
def _print_errors(errors) -> None:
    """파싱된 빌드 오류를 표(종류, 위치, 메시지, L1 서명)로 출력한다."""
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
    """기존 디렉토리에서 Maven을 실행하고 파싱된 오류와 L1 서명을 출력한다(디버깅용).

    Run Maven on an existing directory and print the parsed errors + signatures (debugging aid)."""
    from sbmig.build import MavenRunner

    cfg = _load(config)
    ws = _view_workspace(project_dir, out, prefix="build")
    res = MavenRunner(ws, cfg).run(goal, label="cli-build")
    typer.echo(f"{'BUILD SUCCESS' if res.ok else 'BUILD FAILURE'} ({res.duration_s:.1f}s) log: {res.log_path}")
    if res.errors:
        _print_errors(res.errors)
    raise typer.Exit(0 if res.ok else 2)


@app.command("repo-probe")
def repo_probe(coordinates: str = typer.Argument(..., help="groupId:artifactId[:version] of the library to look up"),
               config: Optional[List[Path]] = ConfigOpt,
               project: Optional[Path] = typer.Option(None, "--project", exists=True, file_okay=False,
                                                      help="project dir whose poms' <repositories> join the repository set")
               ) -> None:
    """저장소 조회를 진단해 javax 라이브러리의 jakarta 빌드를 왜 찾지 못했는지 한 화면에 보여 준다.
    조회하는 저장소 URL과 순서, URL마다 maven-metadata.xml 응답(ok / 404 / 401 / timeout), 찾은 버전, 후보의
    jakarta 여부, 형제 빌드 탐색 결과를 출력한다. settings.xml을 보여 줄 수 없는 폐쇄망 PC에서 쓴다. 마지막
    줄은 ``REPO-PROBE ok|none …``이다.

    Repository lookup diagnostics: the repository set in query order, the per-URL outcome of the metadata lookup, the
    versions (prereleases marked), the candidates' jakarta verdicts (version qualifier / pom / jar scan) and the
    sibling-build discovery — run it on the air-gapped PC when a javax library's jakarta build was not found."""
    parts = [x.strip() for x in coordinates.split(":")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        typer.echo("repo-probe needs groupId:artifactId[:version]", err=True)
        raise typer.Exit(2)
    group, artifact = parts[0], parts[1]
    current = parts[2] if len(parts) > 2 and parts[2] else None
    cfg = _load(config, dict(_PROBE_ARTIFACTS_STUB))
    from sbmig.build.jakarta_discovery import probe_report
    from sbmig.build.repo_versions import RepositoryLookup
    from sbmig.loop.notes import table_entry
    lookup = RepositoryLookup(cfg, project_root=project.resolve()) if project else RepositoryLookup(cfg)
    for line in probe_report(lookup, group, artifact, current, table_entry=table_entry(f"{group}:{artifact}", cfg),
                             cfg=cfg):
        typer.echo(line)


@app.command("parse-log")
def parse_log(logfile: Path = typer.Argument(..., exists=True, dir_okay=False),
              root: Path = typer.Option(Path("."), "--root", help="project root used to relativize paths"),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """이미 있는 Maven 로그를 구조화된 오류 목록으로 파싱한다.

    Parse an existing Maven log into structured errors."""
    from sbmig.build import parse_maven_log

    from sbmig.build.log_parser import reactor_artifact_ids_from_poms
    errors = parse_maven_log(logfile.read_text(encoding="utf-8", errors="replace"), root.resolve(),
                             known_modules=reactor_artifact_ids_from_poms(root))
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
    """기존 디렉토리에 OpenRewrite 레시피를 실행한다. --dry-run이 아니면 파일을 직접 바꾼다.

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
    """디스크를 정리한다. 끝난 실행의 프로젝트 복사본, 웹 UI 업로드 zip, LLM 기록 파일을 지운다.

    result.zip, report, changes.patch, logs는 남기고 실행 중이거나 대기 중인 실행은 건드리지 않는다.
    --dry-run은 지울 대상만 보여 준다.

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
    """sbmig 버전과 코드 트리 지문을 출력한다. 웹 파드와 빌드 파드가 같은 이미지인지 비교할 때 쓴다.

    Print the sbmig version with the running tree's digest (`0.1.0+<12 hex>`, the value the Job-mode Worker compares)."""
    from sbmig.web.contract import sbmig_version

    typer.echo(f"sbmig {sbmig_version()}")


# --------------------------------------------------------------------------- 하위 명령
def _mount(module: str, attr: str, name: str, help_text: str) -> None:
    """하위 명령(kb, recipes, fleet, advisories)을 붙인다.
    패키지 import가 실패하면 같은 이름으로 대신할 명령을 등록한다. 그래도 `sbmig --help`는 동작한다.
    그 명령을 실행하면 import 실패 이유를 출력하고 exit 1로 끝낸다."""
    try:
        import importlib
        sub = getattr(importlib.import_module(module), attr)
        app.add_typer(sub, name=name, help=help_text)
    except Exception as e:  # pragma: no cover - 하위 패키지가 깨진 경우
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
    """콘솔 명령 `sbmig`의 진입점. Typer 앱을 실행한다."""
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
