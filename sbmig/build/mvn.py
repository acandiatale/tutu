"""Maven 을 띄우는 공용 도우미 — 빌드(MavenRunner)와 변환(OpenRewriteRunner)이 같은
명령·환경·로그 방식을 쓰게 한다. 흐름상 위치: 변환 후 컴파일·검증 빌드·수정 루프 재빌드·변환(OpenRewrite 1회),
그리고 classpath / dependency:tree / effective-pom 조회까지 sbmig 가 띄우는 모든 mvn 이 여기를 지난다. 핵심
진입점: ``base_command``(mvn + settings + 로컬 저장소 + offline), ``mvn_env``(JAVA_HOME / MAVEN_OPTS),
``run_logged``(출력을 로그 파일로 흘리며 실행, 타임아웃이면 프로세스 트리째 종료), ``maven_available``(전제
점검). 지킬 것: Maven 출력은 UTF-8 로 받는다(한국어 Windows 의 MS949 콘솔 때문에 javac 오류의 한글 경로가
깨졌다, round 10); ``mvn.cmd`` 의 자식 ``java.exe`` 가
남지 않게 항상 트리째 죽인다(Windows ``taskkill /T /F``).

Shared Maven invocation helpers (used by MavenRunner and OpenRewriteRunner).

* ``base_command(cfg)``  → ``[mvn, *extra_args, -s settings, -Dmaven.repo.local=…, -o]``
* ``mvn_env(cfg)``       → inherited env + ``cfg.build.env`` + ``JAVA_HOME`` / ``PATH`` when ``java_home`` is set,
  ``MAVEN_OPTS`` += ``-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8`` (unless already set there)
* ``run_logged(...)``    → runs a command in its own process group, streams merged stdout/stderr (ANSI stripped)
  to a log file as it arrives, enforces a timeout by killing the whole group.


"""
from __future__ import annotations

import atexit
import logging
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config

from .log_parser import strip_ansi

log = logging.getLogger("sbmig.build")

HEARTBEAT_S = 60.0      # "still running" INFO line every N s while mvn / rewrite runs (operators: slow ≠ hung)


def _blank(v: str | None) -> bool:
    return v is None or str(v).strip() == ""


# 파이프로 받으면 Maven JVM 은 콘솔 코드 페이지(한국어 Windows 는 MS949)로 쓰는데 sbmig 는 UTF-8 로 읽으므로
# javac 오류의 한글 경로가 깨져 오류 파일을 못 찾았다(round
# 10). JDK 18+ 는 아래 속성을 따르고, 옛 JDK 는 무시한다.
# The Maven JVM writes its console output in the console code page when stdout is a pipe (MS949 on a Korean
# Windows PC); sbmig decodes UTF-8, so Korean paths in javac errors came back garbled and the error's file was never
# found (round 10). JDK 18+ honours these properties for System.out / System.err; older JDKs ignore them.
UTF8_OUTPUT_PROPS: tuple[str, ...] = ("stdout.encoding", "stderr.encoding")


def with_utf8_output(maven_opts: str | None) -> str:
    """MAVEN_OPTS 끝에 `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8` 을 붙인다. 이미 지정된
    속성(예: `build.env`)은 그대로 둔다.

    MAVEN_OPTS with `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8` appended; a property the value already sets
    (e.g. from `build.env`) is left alone."""
    opts = (maven_opts or "").strip()
    for prop in UTF8_OUTPUT_PROPS:
        if f"-D{prop}=" not in opts:
            opts = f"{opts} -D{prop}=UTF-8".strip()
    return opts


def mvn_env(cfg: Config, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Maven 을 띄울 환경: 현재 환경 + ``build.env`` + (``build.java_home`` 이 있으면) JAVA_HOME / PATH 앞자리
    + ``extra``, 마지막에 MAVEN_OPTS 에 UTF-8 출력 속성(``with_utf8_output``)."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (cfg.build.env or {}).items()})
    if not _blank(cfg.build.java_home):
        jh = str(Path(cfg.build.java_home).expanduser().absolute())
        env["JAVA_HOME"] = jh
        env["PATH"] = os.pathsep.join([str(Path(jh) / "bin"), env.get("PATH", "")])
    if extra:
        env.update(extra)
    env["MAVEN_OPTS"] = with_utf8_output(env.get("MAVEN_OPTS"))
    return env


def mvn_executable(cfg: Config) -> str:
    """`build.mvn` 을 셸 없이 `subprocess` 로 띄울 수 있는 경로로 바꾼다. Windows 의 Maven 은 `mvn.cmd` 라서
    `Popen(["mvn", ...])` 이 PATHEXT 를 보지 않고 `WinError 2` 로 실패했다 → `shutil.which` 로 PATH(+
    MAVEN_HOME / M2_HOME 의 bin, `.cmd` / `.bat`)에서 찾는다. 디렉토리가 붙은 경로는 그대로 쓰고, 못 찾으면
    설정값을 그대로 돌려준다(오류 메시지가 설정한 이름을 말하게).

    `build.mvn` resolved to something `subprocess` can start without a shell.

    Windows installs Maven as `mvn.cmd`; `Popen(["mvn", ...])` does not look at PATHEXT and fails with
    `WinError 2`. `shutil.which` does, so a bare name is resolved through PATH (plus `MAVEN_HOME`/`M2_HOME`/bin and
    the usual `.cmd`/`.bat` names). A path with a directory part is used as given (expanded). Unresolvable names are
    returned unchanged so the existing "mvn not found" messages still name what was configured."""
    name = (cfg.build.mvn or "mvn").strip()
    if any(sep in name for sep in ("/", "\\")):
        return str(Path(name).expanduser())
    env = mvn_env(cfg)
    search = env.get("PATH", "")
    for home_var in ("MAVEN_HOME", "M2_HOME"):
        home = env.get(home_var)
        if home:
            search = os.pathsep.join([str(Path(home).expanduser() / "bin"), search])
    candidates = [name]
    if os.name == "nt" and not name.lower().endswith((".cmd", ".bat", ".exe")):
        candidates = [name + ".cmd", name + ".bat", name]
    for c in candidates:
        hit = shutil.which(c, path=search)
        if hit:
            return hit
    return name


def base_command(cfg: Config) -> list[str]:
    """모든 mvn 명령의 앞부분: ``[mvn, *build.extra_args, -s <settings 절대경로>, -Dmaven.repo.local=…, -o]``.
    테스트 건너뛰기(``-Dmaven.test.skip=true``)는 여기가 아니라 ``MavenRunner.command`` 가 붙인다."""
    b = cfg.build
    cmd = [mvn_executable(cfg), *b.extra_args]
    # settings 는 절대경로로 넘긴다: Maven 은 프로젝트(모듈) 디렉토리에서 돌기 때문에 상대경로면 거기서 찾다가
    # 조용히 ~/.m2/settings.xml 로 넘어간다(사내 Nexus 미러가 빠진 채 빌드된다).
    # absolute (symlinks untouched — `/tmp` must stay `/tmp`): Maven runs with cwd = the project (module) directory,
    # so a relative `config/settings.nexus.xml` would be looked up there and silently fall back to ~/.m2/settings.xml
    if not _blank(b.settings_file):
        cmd += ["-s", str(Path(b.settings_file).expanduser().absolute())]
    if not _blank(b.local_repo):
        cmd.append(f"-Dmaven.repo.local={Path(b.local_repo).expanduser().absolute()}")
    if b.offline:
        cmd.append("-o")
    return cmd


_CATCH_ALL_MIRROR_RE = re.compile(r"<mirrorOf>\s*(\*|external:\*|external:http:\*)\s*</mirrorOf>")


def settings_catch_all_mirror(cfg: Config) -> Optional[bool]:
    """``build.settings_file`` 이 모든 저장소를 미러 하나로 돌리는가(``<mirrorOf>*</mirrorOf>`` /
    ``external:*``)? None = settings 파일 없음 / 못 읽음. 이런 미러가 있어야 프로젝트 pom 에 적힌 옛
    Nexus·Central 저장소가 폐쇄망에서도 풀린다(없으면 Maven 이 그 URL 로 직접 나간다, RUNBOOK §0.1).

    Does `build.settings_file` route every repository through a mirror (`<mirrorOf>*</mirrorOf>` or
    `external:*`)? None = no settings file configured / not readable. Such a mirror is what makes the
    `<repositories>` declared inside the project poms (old Nexus URLs, Central, Spring milestones) resolve on an
    air-gapped host; without it Maven contacts them directly (RUNBOOK §0.1)."""
    if _blank(cfg.build.settings_file):
        return None
    p = Path(cfg.build.settings_file).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return bool(_CATCH_ALL_MIRROR_RE.search(text))


def repository_notes(poms: Iterable[Any], cfg: Config) -> list[str]:
    """프로젝트 pom 이 선언한 저장소(``<repositories>``)에 대한 운영자 메모. 선언이 없으면 [], 있으면
    settings 미러가 그것들을 받아 주는지 한 줄. Maven 이 그 URL 로 직접 나가게 되는 경우는 ``followup:`` 으로
    시작한다(인수인계 후속 작업).

    Operator notes about repositories the project poms declare (`PomInfo.repositories`): empty when the poms
    declare none; otherwise one note saying whether the settings file mirrors them. The note starts with
    `followup:` when Maven would contact the declared URLs directly."""
    urls: list[str] = []
    for p in poms:
        for u in getattr(p, "repositories", []) or []:
            if u not in urls:
                urls.append(u)
    if not urls:
        return []
    shown = ", ".join(urls[:5]) + (f" (+{len(urls) - 5} more)" if len(urls) > 5 else "")
    mirror = settings_catch_all_mirror(cfg)
    if mirror:
        return [(f"poms declare {len(urls)} repository URL(s) ({shown}); routed through the settings mirror "
                 f"({cfg.build.settings_file})")]
    where = ("no build.settings_file configured" if mirror is None else
             f"{cfg.build.settings_file} has no catch-all <mirrorOf>*</mirrorOf> / external:* mirror")
    return [(f"followup: poms declare {len(urls)} repository URL(s) ({shown}) and {where}: Maven contacts them directly "
             "(unreachable → resolution failures / long timeouts). Copy config/settings.nexus.example.xml, set the Nexus "
             "group URL and point build.settings_file at it")]


def local_repo_path(cfg: Config) -> Path:
    """로컬 Maven 저장소 경로: ``build.local_repo`` → (설정된 / 사용자) settings.xml 의
    ``<localRepository>`` → ``~/.m2/repository`` 순서.

    cfg.build.local_repo → <localRepository> of the configured / user settings.xml → ~/.m2/repository."""
    if not _blank(cfg.build.local_repo):
        return Path(cfg.build.local_repo).expanduser()
    for settings in ([cfg.build.settings_file] if not _blank(cfg.build.settings_file) else []) + \
            [str(Path.home() / ".m2" / "settings.xml")]:
        p = Path(settings).expanduser()
        if p.is_file():
            m = re.search(r"<localRepository>\s*([^<]+?)\s*</localRepository>",
                          p.read_text(encoding="utf-8", errors="replace"))
            if m and "${" not in m.group(1):
                return Path(m.group(1)).expanduser()
    return Path.home() / ".m2" / "repository"


@dataclass
class ProcessResult:
    """``run_logged`` 의 결과: 종료 코드, 전체 출력(ANSI 제거), 걸린 시간, 타임아웃 여부, 시작
    실패 이유(실행 파일 없음 등)."""
    exit_code: int
    text: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None          # e.g. executable not found


def _popen(*args, **kwargs) -> subprocess.Popen:
    """Indirection so tests can fake the Maven process without patching subprocess globally (git uses it)."""
    return subprocess.Popen(*args, **kwargs)


IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000


def _popen_kwargs() -> dict[str, Any]:
    """Windows: Maven / OpenRewrite 에 숨은 콘솔(`CREATE_NO_WINDOW`)을 따로 준다. 운영자의 Ctrl+Break(스레드
    덤프)·Ctrl+C 가 cmd.exe / java.exe 에 바로 가서 `mvn.cmd` 가 "Terminate batch job (Y/N)?" 에서 멈추던 일을
    막는다. 죽이는 것은 sbmig 가 직접 한다.

    Windows: Maven / OpenRewrite get their own hidden console (`CREATE_NO_WINDOW`). The operator's Ctrl+Break
    (thread dump, `sbmig.core.diag`) and Ctrl+C then never reach cmd.exe / java.exe directly — a Ctrl+Break used to
    leave `mvn.cmd` at "Terminate batch job (Y/N)?"; sbmig still kills the whole tree itself (`taskkill /T /F`)."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}


def _kill_tree_windows(pid: int) -> None:
    """Windows 에는 신호를 보낼 프로세스 그룹이 없다: `mvn.cmd`(cmd.exe)만 끝내면 자식 `java.exe` 가 살아남아
    `target/` 과 소스 파일을 잠근다(round 9c) → `taskkill /T /F` 로 트리 전체를 죽인다.

    Windows has no process groups to signal: `mvn.cmd` is a cmd.exe whose child `java.exe` would survive a plain
    terminate() and keep `target/` / source files locked (round 9c) — `taskkill /T /F` kills the whole tree."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    """프로세스 트리 종료: SIGTERM(Windows 는 taskkill /T /F) → 5초 대기 →
    SIGKILL → 5초 대기. 오류는 삼킨다."""
    try:
        if IS_WINDOWS:
            _kill_tree_windows(proc.pid)
        elif hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:                             # pragma: no cover
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:                             # pragma: no cover
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:     # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# live Maven / OpenRewrite processes (web shutdown, Ctrl+C, interpreter exit)
# ---------------------------------------------------------------------------
# 살아 있는 자식 프로세스 목록: 웹 서버 종료·인터프리터 종료 때 아직 도는 mvn 을 죽이려고 둔다. 이게 없을 때
# Windows 에서 uvicorn 을 내려도 mvn.cmd 의 java.exe 가 살아남아
# target/ 을 잠그고 CPU 를 먹었다(회사 실행, 2026-10-21).
# Every long-running child goes through `run_logged`; the registry lets a server shutdown or an interpreter exit
# kill what is still running. Without it a `java.exe` started by `mvn.cmd` survived the uvicorn shutdown on
# Windows (holding `target/` and source files, burning CPU) and the shutdown itself hung on the open SSE
# streams (company run, 2026-10-21).
_LIVE: "set[subprocess.Popen]" = set()
_LIVE_LOCK = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE.discard(proc)


def live_processes() -> list[subprocess.Popen]:
    """``run_logged`` 로 띄운 자식 중 아직 끝나지 않은 것.

    Children started by `run_logged` that have not exited yet."""
    with _LIVE_LOCK:
        return [p for p in _LIVE if p.poll() is None]


def kill_live_processes(reason: str = "shutdown") -> int:
    """살아 있는 자식 전부를 트리째 죽이고 그 수를 돌려준다. ``atexit`` 에 등록돼 있고, 웹 앱
    종료(lifespan)도 이것을 부른다.

    Kill every live child (whole process tree: `taskkill /T /F` on Windows, the process group elsewhere).
    Returns how many were killed. Registered with `atexit`, called by the web app's lifespan shutdown."""
    procs = live_processes()
    for proc in procs:
        name = Path(str(proc.args[0])).name if isinstance(proc.args, (list, tuple)) and proc.args else "?"
        log.warning("%s: killing %s (pid %s) and its process tree", reason, name, proc.pid)
        _kill_group(proc)
    return len(procs)


atexit.register(lambda: kill_live_processes("interpreter exit"))


def run_logged(cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path,
               timeout_s: float | None) -> ProcessResult:
    """``cmd`` 를 자기 프로세스 그룹에서 실행하고 출력(stdout + stderr, ANSI 제거)을 줄 단위로
    ``log_path`` 에 바로 쓴다. ``HEARTBEAT_S`` 마다 "still running" INFO 줄(운영자에게: 느린 것 ≠ 멈춘 것),
    타임아웃이면 트리째 죽이고 로그 끝에 그 사실을 적는다. 프로세스가
    실패해도 예외 없이 ProcessResult(실행 파일 없음 = exit 127).

    Run ``cmd``; stream output (ANSI stripped) to ``log_path`` line by line. Never raises for process failures."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# cwd: {cwd}\n# cmd: {' '.join(cmd)}\n")
        fh.flush()
        try:
            proc = _popen(list(cmd), cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                          **_popen_kwargs())
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            msg = f"cannot start {cmd[0]!r}: {exc}"
            fh.write(f"[ERROR] {msg}\n")
            return ProcessResult(exit_code=127, text=f"[ERROR] {msg}\n", duration_s=time.monotonic() - t0,
                                 error=msg)

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                # JVM 에 UTF-8 출력을 요구했으니 UTF-8 로 읽는다. 옛 코드 페이지 바이트는 U+FFFD 로 바꾼다 —
                # 읽기 스레드가 디코딩 오류로 죽으면 파이프가 차서 Maven 이 멈춘다.
                # UTF-8 (MAVEN_OPTS asks the JVM for it); a stray legacy byte becomes U+FFFD, never a dead reader
                line = strip_ansi(raw.decode("utf-8", errors="replace"))
                chunks.append(line)
                fh.write(line)
                fh.flush()

        reader = threading.Thread(target=pump, name="mvn-log-pump", daemon=True)
        reader.start()
        timed_out = False
        _register(proc)
        try:
            while True:
                elapsed = time.monotonic() - t0
                remaining = None if timeout_s is None else max(0.0, timeout_s - elapsed)
                slot = HEARTBEAT_S if remaining is None else min(HEARTBEAT_S, remaining)
                try:
                    proc.wait(timeout=slot if slot > 0 else 0.01)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - t0
                    if timeout_s is not None and elapsed >= timeout_s:
                        timed_out = True
                        log.warning("timeout after %.0fs, killing process group of %s", timeout_s or 0, cmd[0])
                        _kill_group(proc)
                        break
                    log.info("%s still running: %.0fs elapsed%s — %d output line(s) so far, log: %s",
                             Path(cmd[0]).name, elapsed, f" (timeout {timeout_s:.0f}s)" if timeout_s else "",
                             len(chunks), log_path)
        # 기다리는 스레드에서 Ctrl+C / SystemExit 가 나도 JVM 을 남기지 않는다(먼저 죽이고 나서 다시 올린다).
        except BaseException:             # Ctrl+C / SystemExit in the waiting thread: never leave the JVM behind
            _kill_group(proc)
            raise
        finally:
            _unregister(proc)
        reader.join(timeout=10)
        if timed_out:
            fh.write(f"\n[ERROR] sbmig: killed after timeout of {timeout_s:.0f}s\n")
        try:
            proc.stdout.close()           # type: ignore[union-attr]
        except Exception:                 # pragma: no cover
            pass
    code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    return ProcessResult(exit_code=code, text="".join(chunks), duration_s=time.monotonic() - t0, timed_out=timed_out)


def maven_available(cfg: Config) -> tuple[bool, str]:
    """전제 점검(preflight): 설정된 환경으로 ``mvn -v`` 를 돌려 (성공 여부, 'Apache Maven x.y.z; Java
    version: …, runtime: …')를 돌려준다. runtime(JDK 경로)을 남기는 것은 "JDK 21 아님" 중단 메시지가 Maven 이
    실제로 쓴 JDK 를 말하게 하려는 것.

    Preflight: run ``mvn -v`` with the configured env. Returns (ok, 'Apache Maven x.y.z; Java version: …')."""
    cmd = [mvn_executable(cfg), "-v"]
    try:
        cp = subprocess.run(cmd, env=mvn_env(cfg), capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError) as exc:
        return False, f"mvn not found ({cfg.build.mvn}): {exc}"
    except subprocess.TimeoutExpired:
        return False, "mvn -v timed out"
    out = strip_ansi(cp.stdout + cp.stderr)
    if cp.returncode != 0:
        return False, out.strip().splitlines()[-1] if out.strip() else f"mvn -v exited {cp.returncode}"
    maven = next((ln.strip() for ln in out.splitlines() if ln.startswith("Apache Maven")), out.strip()[:80])
    line = next((ln.strip() for ln in out.splitlines() if ln.startswith("Java version")), "")
    # "Java version: 21.0.1, vendor: …, runtime: C:\jdk-21" → keep the version and the runtime home (the jdk-not-21
    # abort names the JDK Maven really used)
    parts = [x.strip() for x in line.split(",")]
    java = ", ".join([parts[0], *[x for x in parts[1:] if x.startswith("runtime:")]]) if line else ""
    return True, f"{maven}; {java}" if java else maven
