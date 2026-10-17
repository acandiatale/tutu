"""MavenRunner — implements ``BuildRunnerLike``: one real ``mvn`` run, full log captured, errors parsed."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from sbmig.core.config import Config
from sbmig.core.models import BuildError, BuildResult, ErrorKind
from sbmig.core.workspace import Workspace

from .log_parser import (
    _is_boilerplate,
    _root_prefixes,
    _scrub,
    _split_level,
    parse_maven_log,
    parse_reactor_summary,
)
from .mvn import base_command, maven_available, mvn_env, run_logged
from .signatures import external_prefixes_from_config, fill_signatures, register_external_prefixes

log = logging.getLogger("sbmig.build")

MAX_SUMMARY_ERRORS = 60
SUMMARY_TAIL = 25

__all__ = ["MavenRunner", "build_summary", "maven_available"]


def build_summary(text: str, project_root: Path | str | None = None) -> str:
    """All distinct meaningful ``[ERROR]`` lines (max 60) + the last 25 lines; project root stripped from paths."""
    seen: set[str] = set()
    errs: list[str] = []
    for line in text.splitlines():
        level, body = _split_level(line)
        if level in ("ERROR", "FATAL") and not _is_boilerplate(body):
            b = body.rstrip()
            if b not in seen:
                seen.add(b)
                errs.append(f"[{level}] {b}")
    tail = [ln for ln in text.splitlines() if not ln.startswith("# ")][-SUMMARY_TAIL:]
    out = "\n".join(errs[:MAX_SUMMARY_ERRORS])
    if len(errs) > MAX_SUMMARY_ERRORS:
        out += f"\n... ({len(errs) - MAX_SUMMARY_ERRORS} more [ERROR] lines)"
    out = (out + "\n--- tail ---\n" if out else "") + "\n".join(tail)
    return _scrub(out, _root_prefixes(project_root))


class MavenRunner:
    """``MavenRunner(ws, cfg).run("test-compile")`` → ``BuildResult``. Never raises for a failed build."""

    def __init__(self, ws: Workspace, cfg: Config, *, timeout_s: float | None = None):
        self.ws = ws
        self.cfg = cfg
        self.timeout_s = timeout_s if timeout_s is not None else float(cfg.build.timeout_minutes) * 60.0
        register_external_prefixes(external_prefixes_from_config(cfg))

    # ------------------------------------------------------------------ command
    def command(self, goal: str, *, extra_args: Iterable[str] = (), skip_test_compile: bool = False) -> list[str]:
        cmd = base_command(self.cfg) + goal.split() + list(extra_args)
        if self.cfg.build.skip_tests and not skip_test_compile:
            cmd.append("-DskipTests")
        if skip_test_compile:
            cmd.append("-Dmaven.test.skip=true")
        return cmd

    def _cwd(self, module: str | None) -> tuple[Path, list[str]]:
        if not module:
            return self.ws.root, []
        p = self.ws.abs(module)
        if p.is_file():                                   # "service-a/pom.xml" → -f pom.xml in its dir
            return p.parent, ["-f", p.name]
        return p, []

    # ------------------------------------------------------------------ run
    def run(self, goal: str, *, module: str | None = None, extra_args: Iterable[str] = (), label: str = "",
            skip_test_compile: bool = False) -> BuildResult:
        cwd, file_args = self._cwd(module)
        cmd = self.command(goal, extra_args=[*file_args, *extra_args], skip_test_compile=skip_test_compile)
        log_path = self.ws.new_log_path(f"build-{label or goal}")
        log.info("mvn %s (cwd=%s)", " ".join(cmd[1:]), self.ws.rel(cwd) if cwd != self.ws.root else ".")
        pr = run_logged(cmd, cwd=cwd, env=mvn_env(self.cfg), log_path=log_path, timeout_s=self.timeout_s)
        text = pr.text
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in text
        errors: list[BuildError] = [] if ok else parse_maven_log(text, self.ws.root)
        if pr.timed_out:
            errors.append(fill_signatures(BuildError(
                kind=ErrorKind.UNKNOWN, module=module,
                message=f"build timed out after {self.timeout_s / 60:.1f} minutes (goal {goal})",
                raw="\n".join(text.splitlines()[-40:]))))
        elif pr.error:
            errors = [fill_signatures(BuildError(kind=ErrorKind.UNKNOWN, message=pr.error, raw=pr.error))]
        if not ok and not errors:                         # e.g. exit≠0 after "BUILD SUCCESS", or a crashed JVM
            errors = [fill_signatures(BuildError(
                kind=ErrorKind.UNKNOWN, module=module,
                message=f"mvn exited with code {pr.exit_code}" + ("" if "BUILD SUCCESS" in text
                                                                  else " without BUILD SUCCESS"),
                raw="\n".join(text.splitlines()[-40:])))]
        modules_built, failed_module = parse_reactor_summary(text)
        result = BuildResult(
            ok=ok, goal=goal, command=cmd, exit_code=pr.exit_code, duration_s=round(pr.duration_s, 3),
            log_path=str(log_path), errors=errors, modules_built=modules_built,
            failed_module=None if ok else failed_module, summary=build_summary(text, self.ws.root),
            timed_out=pr.timed_out,
        )
        self.ws.journal({"event": "build", "goal": goal, "module": module, "ok": ok, "exit_code": pr.exit_code,
                         "duration_s": result.duration_s, "error_count": len(errors), "log": str(log_path),
                         "timed_out": pr.timed_out, "skip_test_compile": skip_test_compile, "label": label})
        log.info("build %s: %s (%d errors, %.1fs)", goal, "OK" if ok else "FAILED", len(errors), pr.duration_s)
        return result

