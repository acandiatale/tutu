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
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sbmig.core.config import Config

from .log_parser import strip_ansi

log = logging.getLogger("sbmig.build")


def _blank(v: str | None) -> bool:
    return v is None or str(v).strip() == ""


def mvn_env(cfg: Config, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (cfg.build.env or {}).items()})
    if not _blank(cfg.build.java_home):
        jh = str(Path(cfg.build.java_home).expanduser())
        env["JAVA_HOME"] = jh
        env["PATH"] = os.pathsep.join([str(Path(jh) / "bin"), env.get("PATH", "")])
    if extra:
        env.update(extra)
    return env


def base_command(cfg: Config) -> list[str]:
    b = cfg.build
    cmd = [b.mvn, *b.extra_args]
    if not _blank(b.settings_file):
        cmd += ["-s", str(Path(b.settings_file).expanduser())]
    if not _blank(b.local_repo):
        cmd.append(f"-Dmaven.repo.local={Path(b.local_repo).expanduser()}")
    if b.offline:
        cmd.append("-o")
    return cmd


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


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:                             # pragma: no cover - windows
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
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning("timeout after %.0fs, killing process group of %s", timeout_s or 0, cmd[0])
            _kill_group(proc)
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
    cmd = [cfg.build.mvn, "-v"]
    try:
        cp = subprocess.run(cmd, env=mvn_env(cfg), capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL)
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
