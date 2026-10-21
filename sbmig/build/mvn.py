"""Shared Maven invocation helpers (used by MavenRunner and OpenRewriteRunner).

* ``base_command(cfg)``  → ``[mvn, *extra_args, -s settings, -Dmaven.repo.local=…, -o]``
* ``mvn_env(cfg)``       → inherited env + ``cfg.build.env`` + ``JAVA_HOME`` / ``PATH`` when ``java_home`` is set
* ``run_logged(...)``    → runs a command in its own process group, streams merged stdout/stderr (ANSI stripped)
  to a log file as it arrives, enforces a timeout by killing the whole group.
"""
from __future__ import annotations

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


def mvn_env(cfg: Config, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (cfg.build.env or {}).items()})
    if not _blank(cfg.build.java_home):
        jh = str(Path(cfg.build.java_home).expanduser().resolve())
        env["JAVA_HOME"] = jh
        env["PATH"] = os.pathsep.join([str(Path(jh) / "bin"), env.get("PATH", "")])
    if extra:
        env.update(extra)
    return env


def mvn_executable(cfg: Config) -> str:
    """`build.mvn` resolved to something `subprocess` can start without a shell.

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
    b = cfg.build
    cmd = [mvn_executable(cfg), *b.extra_args]
    # absolute: Maven runs with cwd = the project (module) directory, so a relative `config/settings.nexus.xml`
    # would be looked up there and silently fall back to ~/.m2/settings.xml (handbook review, 2026-10-21)
    if not _blank(b.settings_file):
        cmd += ["-s", str(Path(b.settings_file).expanduser().resolve())]
    if not _blank(b.local_repo):
        cmd.append(f"-Dmaven.repo.local={Path(b.local_repo).expanduser().resolve()}")
    if b.offline:
        cmd.append("-o")
    return cmd


_CATCH_ALL_MIRROR_RE = re.compile(r"<mirrorOf>\s*(\*|external:\*|external:http:\*)\s*</mirrorOf>")


def settings_catch_all_mirror(cfg: Config) -> Optional[bool]:
    """Does `build.settings_file` route every repository through a mirror (`<mirrorOf>*</mirrorOf>` or
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
    """Operator notes about repositories the project poms declare (`PomInfo.repositories`): empty when the poms
    declare none or `build.pom_repositories: remove` strips them; otherwise one note saying whether the settings
    file mirrors them. The note starts with `followup:` when Maven would contact the declared URLs directly."""
    urls: list[str] = []
    for p in poms:
        for u in getattr(p, "repositories", []) or []:
            if u not in urls:
                urls.append(u)
    if not urls:
        return []
    shown = ", ".join(urls[:5]) + (f" (+{len(urls) - 5} more)" if len(urls) > 5 else "")
    policy = getattr(cfg.build, "pom_repositories", "keep")
    if policy == "remove":
        hint = ("" if settings_catch_all_mirror(cfg) else
                " — make sure build.settings_file mirrors everything (mirrorOf external:*), the removed URLs were the "
                "only route otherwise")
        return [(f"poms declare {len(urls)} repository URL(s) ({shown}); build.pom_repositories=remove strips them "
                 f"before the first build{hint}")]
    mirror = settings_catch_all_mirror(cfg)
    if mirror:
        return [(f"poms declare {len(urls)} repository URL(s) ({shown}); routed through the settings mirror "
                 f"({cfg.build.settings_file})")]
    where = ("no build.settings_file configured" if mirror is None else
             f"{cfg.build.settings_file} has no catch-all <mirrorOf>*</mirrorOf> / external:* mirror")
    return [(f"followup: poms declare {len(urls)} repository URL(s) ({shown}) and {where}: Maven contacts them directly "
             "(unreachable → resolution failures / long timeouts). Copy config/settings.nexus.example.xml, set the Nexus "
             "group URL and point build.settings_file at it; or set build.pom_repositories: remove")]


def local_repo_path(cfg: Config) -> Path:
    """cfg.build.local_repo → <localRepository> of the configured / user settings.xml → ~/.m2/repository."""
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
    exit_code: int
    text: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None          # e.g. executable not found


def _popen(*args, **kwargs) -> subprocess.Popen:
    """Indirection so tests can fake the Maven process without patching subprocess globally (git uses it)."""
    return subprocess.Popen(*args, **kwargs)


IS_WINDOWS = os.name == "nt"


def _kill_tree_windows(pid: int) -> None:
    """Windows has no process groups to signal: `mvn.cmd` is a cmd.exe whose child `java.exe` would survive a plain
    terminate() and keep `target/` / source files locked (round 9c) — `taskkill /T /F` kills the whole tree."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
        pass


def _kill_group(proc: subprocess.Popen) -> None:
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


def run_logged(cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path,
               timeout_s: float | None) -> ProcessResult:
    """Run ``cmd``; stream output (ANSI stripped) to ``log_path`` line by line. Never raises for process failures."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# cwd: {cwd}\n# cmd: {' '.join(cmd)}\n")
        fh.flush()
        try:
            proc = _popen(list(cmd), cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            msg = f"cannot start {cmd[0]!r}: {exc}"
            fh.write(f"[ERROR] {msg}\n")
            return ProcessResult(exit_code=127, text=f"[ERROR] {msg}\n", duration_s=time.monotonic() - t0,
                                 error=msg)

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = strip_ansi(raw.decode("utf-8", errors="replace"))
                chunks.append(line)
                fh.write(line)
                fh.flush()

        reader = threading.Thread(target=pump, name="mvn-log-pump", daemon=True)
        reader.start()
        timed_out = False
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
                log.info("%s still running: %.0fs elapsed%s — %d output line(s) so far, log: %s", Path(cmd[0]).name,
                         elapsed, f" (timeout {timeout_s:.0f}s)" if timeout_s else "", len(chunks), log_path)
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
    """Preflight: run ``mvn -v`` with the configured env. Returns (ok, 'Apache Maven x.y.z; Java version: …')."""
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
    java = next((ln.strip().split(",")[0] for ln in out.splitlines() if ln.startswith("Java version")), "")
    return True, f"{maven}; {java}" if java else maven
