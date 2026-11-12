"""Maven을 실행하는 공용 함수를 모은 모듈.
빌드(MavenRunner)와 변환(OpenRewriteRunner)이 같은 명령, 환경 변수, 로그 방식을 쓰게 한다.
sbmig가 실행하는 모든 mvn 명령이 이 모듈을 거친다.
주요 함수는 ``base_command``, ``mvn_env``, ``run_logged``, ``maven_available``이다.
- 한국어 Windows의 MS949 콘솔에서는 javac 오류의 한글 경로가 깨지므로 Maven 출력을 UTF-8로 받는다
- ``mvn.cmd``가 띄운 ``java.exe``가 남지 않도록 항상 프로세스 트리 전체를 종료한다

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


# 파이프로 출력을 받으면 Maven JVM이 콘솔 코드 페이지(MS949)로 출력해 한글 경로가 깨지므로 UTF-8 속성을
# 준다. JDK 18 이상만 이 속성을 따른다
# The Maven JVM writes its console output in the console code page when stdout is a pipe (MS949 on a Korean
# Windows PC); sbmig decodes UTF-8, so Korean paths in javac errors came back garbled and the error's file was never
# found (round 10). JDK 18+ honours these properties for System.out / System.err; older JDKs ignore them.
UTF8_OUTPUT_PROPS: tuple[str, ...] = ("stdout.encoding", "stderr.encoding")


def with_utf8_output(maven_opts: str | None) -> str:
    """MAVEN_OPTS 끝에 UTF-8 출력 속성을 붙여 반환한다. 이미 지정된 속성은 그대로 둔다.

    MAVEN_OPTS with `-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8` appended; a property the value already sets
    (e.g. from `build.env`) is left alone."""
    opts = (maven_opts or "").strip()
    for prop in UTF8_OUTPUT_PROPS:
        if f"-D{prop}=" not in opts:
            opts = f"{opts} -D{prop}=UTF-8".strip()
    return opts


def mvn_env(cfg: Config, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Maven을 실행할 환경 변수를 만든다.
    현재 환경에 ``build.env``, JAVA_HOME / PATH, ``extra``, UTF-8 출력 속성을 더한다."""
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
    """`build.mvn`을 셸 없이 `subprocess`로 실행할 수 있는 경로로 바꾼다.
    Windows의 Maven은 `mvn.cmd`여서 `Popen`이 찾지 못하므로 `shutil.which`로 찾는다.
    찾지 못하면 오류 메시지에 설정한 이름이 나오도록 설정값을 그대로 반환한다.

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
    """모든 mvn 명령의 앞부분을 만든다. ``-Dmaven.test.skip=true``는 ``MavenRunner.command``가 붙인다."""
    b = cfg.build
    cmd = [mvn_executable(cfg), *b.extra_args]
    # Maven은 모듈 디렉토리에서 실행되므로 상대경로를 주면 settings를 찾지 못하고 조용히
    # ~/.m2/settings.xml을 쓴다. 그래서 절대경로로 넘긴다
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
    """``build.settings_file``에 모든 저장소 요청을 받는 미러(``<mirrorOf>*</mirrorOf>`` 등)가 있는지
    확인한다. 파일이 없거나 읽지 못하면 None을 반환한다.
    이런 미러가 있어야 프로젝트 pom에 선언된 저장소가 폐쇄망에서도 해결된다.

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
    """프로젝트 pom이 선언한 ``<repositories>``에 대해 운영자에게 보여 줄 안내 문구를 만든다.
    선언이 없으면 빈 목록을 반환한다.
    Maven이 그 URL에 직접 접속하게 되는 경우에는 문구가 ``followup:``으로 시작한다.

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
    """로컬 Maven 저장소 경로를 반환한다.
    ``build.local_repo`` -> settings.xml의 ``<localRepository>`` -> ``~/.m2/repository`` 순서로 찾는다.

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
    """``run_logged``의 실행 결과를 담는 데이터 클래스.
    종료 코드, 전체 출력, 걸린 시간, 타임아웃 여부, 시작 실패 사유를 담는다."""
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
    """Windows에서 Maven / OpenRewrite 프로세스에 숨겨진 별도 콘솔을 붙이는 옵션을 반환한다.
    운영자가 누른 Ctrl+Break, Ctrl+C 때문에 ``mvn.cmd``가 멈추지 않게 하기 위해서다.

    Windows: Maven / OpenRewrite get their own hidden console (`CREATE_NO_WINDOW`). The operator's Ctrl+Break
    (thread dump, `sbmig.core.diag`) and Ctrl+C then never reach cmd.exe / java.exe directly — a Ctrl+Break used to
    leave `mvn.cmd` at "Terminate batch job (Y/N)?"; sbmig still kills the whole tree itself (`taskkill /T /F`)."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}


def _kill_tree_windows(pid: int) -> None:
    """Windows에서 프로세스 트리 전체를 `taskkill /T /F`로 종료한다.
    Windows에는 프로세스 그룹이 없어 ``mvn.cmd``만 끝내면 ``java.exe``가 남아 파일을 잠그기 때문이다.

    Windows has no process groups to signal: `mvn.cmd` is a cmd.exe whose child `java.exe` would survive a plain
    terminate() and keep `target/` / source files locked (round 9c) — `taskkill /T /F` kills the whole tree."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    """프로세스 트리를 종료한다.
    SIGTERM을 보내고 5초 기다린 뒤, 남아 있으면 SIGKILL을 보내고 5초 더 기다린다. 오류는 무시한다."""
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
# 웹 서버나 인터프리터가 종료될 때 아직 실행 중인 mvn을 함께 종료하기 위해 자식 프로세스 목록을 관리한다
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
    """``run_logged``로 시작한 자식 프로세스 중 아직 끝나지 않은 것을 반환한다.

    Children started by `run_logged` that have not exited yet."""
    with _LIVE_LOCK:
        return [p for p in _LIVE if p.poll() is None]


def kill_live_processes(reason: str = "shutdown") -> int:
    """살아 있는 자식 프로세스를 모두 트리째 종료하고 종료한 개수를 반환한다.
    ``atexit``와 웹 앱이 종료될 때 호출된다.

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
    """``cmd``를 별도 프로세스 그룹에서 실행하고 출력을 줄 단위로 ``log_path``에 기록한다.
    ``HEARTBEAT_S``마다 "still running" 줄을 남기고, 타임아웃이 되면 프로세스 트리를 종료한다.
    프로세스가 실패해도 예외를 던지지 않고 ProcessResult를 반환한다(실행 파일이 없으면 exit 127).

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
                # 읽기 스레드가 디코딩 오류로 멈추면 파이프가 가득 차 Maven도 멈추므로 깨진 바이트는 U+FFFD로 바꾼다
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
        # Ctrl+C / SystemExit에도 JVM이 남지 않도록 먼저 종료하고 예외를 다시 던진다
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
    """preflight 점검으로 ``mvn -v``를 실행해 (성공 여부, 버전 문구)를 반환한다.
    JDK 21 확인이 실패했을 때 Maven이 실제로 쓴 JDK를 보여 주기 위해 runtime 줄을 남긴다.

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
