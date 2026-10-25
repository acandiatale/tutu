"""MavenRunner — implements ``BuildRunnerLike``: one real ``mvn`` run, full log captured, errors parsed."""
from __future__ import annotations

import logging
import re
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

__all__ = ["MavenRunner", "build_summary", "maven_available", "negative_cache_hits"]

# Maven remembers a failed download in the local repository (`*.lastUpdated` / `_remote.repositories`) and does not
# ask the repository again until the update interval (default: daily) has passed. After a Nexus admin uploaded the
# missing artifact, every re-run still failed the same way for a day (round 10). Maven 3.6–3.8 and 3.9 wordings:
#   Failure to find g:a:jar:1.0 in https://… was cached in the local repository, resolution will not be reattempted …
#   g:a:jar:1.0 was not found in https://… during a previous attempt. This failure was cached in the local repository …
_NEGATIVE_CACHE_RE = re.compile(r"was cached in the local repository|resolution (?:will not be|is not) reattempted "
                                r"until the update interval")
_COORDS = r"[\w.\-]+:[\w.\-]+(?::[\w.\-]+){1,3}"
_NEGATIVE_GAV_RE = re.compile(r"Failure to (?:find|transfer) (?P<a>" + _COORDS + r") (?:in|from)\b|"
                              r"(?P<b>" + _COORDS + r") (?:was not found in|failed to transfer from) \S+ during a "
                              r"previous attempt")


def negative_cache_hits(text: str) -> list[str]:
    """Coordinates (g:a[:type]:v as Maven printed them) whose resolution failed only because the failure was cached
    in the local repository. Empty when the log has no such line."""
    out: list[str] = []
    cached = False
    for line in (text or "").splitlines():
        if not _NEGATIVE_CACHE_RE.search(line):
            continue
        cached = True
        for m in _NEGATIVE_GAV_RE.finditer(line):
            gav = m.group("a") or m.group("b")
            if gav and gav not in out:
                out.append(gav)
    return out or (["?"] if cached else [])


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
    def command(self, goal: str, *, extra_args: Iterable[str] = (), skip_test_compile: bool = False,
                skip_tests: bool = False) -> list[str]:
        """`skip_tests` (round 8 tests-skipped fallback) → `-DskipTests` even when `build.skip_tests` is off;
        `skip_test_compile` wins (`-Dmaven.test.skip=true` already implies no test run), never both flags."""
        cmd = base_command(self.cfg) + goal.split() + list(extra_args)
        skip_test_compile = skip_test_compile or bool(getattr(self.cfg.build, "skip_test_compile", False))
        if (self.cfg.build.skip_tests or skip_tests) and not skip_test_compile:
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
            skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        """One build. With `build.retry_negative_cache` (default on) a build that failed on a *cached* resolution
        failure is run once more with `-U` and the second result is returned."""
        extra_args = list(extra_args)
        result, text = self._run_once(goal, module=module, extra_args=extra_args, label=label,
                                      skip_test_compile=skip_test_compile, skip_tests=skip_tests)
        if result.ok or not self._may_force_update(result.command):
            return result
        # once per artifact and run: an artifact that is really missing would otherwise double every later build
        retried: set[str] = self.ws.__dict__.setdefault("_negative_cache_retried", set())
        hits = [h for h in negative_cache_hits(text) if h not in retried]
        if not hits:
            return result
        retried.update(hits)
        log.info("maven: negative cache hit for %s → retrying once with -U", ", ".join(hits[:3])
                 + (f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""))
        self.ws.journal({"event": "build_negative_cache_retry", "goal": goal, "module": module, "artifacts": hits,
                         "first_log": result.log_path})
        retry, _text = self._run_once(goal, module=module, extra_args=[*extra_args, "-U"],
                                      label=f"{label or goal}-U", skip_test_compile=skip_test_compile,
                                      skip_tests=skip_tests)
        return retry

    def _may_force_update(self, cmd: list[str]) -> bool:
        """-U only makes sense online and once: not offline (`-o`), not when the command already forces updates."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in cmd)

    def _run_once(self, goal: str, *, module: str | None, extra_args: list[str], label: str,
                  skip_test_compile: bool, skip_tests: bool) -> tuple[BuildResult, str]:
        cwd, file_args = self._cwd(module)
        cmd = self.command(goal, extra_args=[*file_args, *extra_args], skip_test_compile=skip_test_compile,
                           skip_tests=skip_tests)
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
                         "timed_out": pr.timed_out, "skip_test_compile": skip_test_compile, "skip_tests": skip_tests,
                         "label": label})
        log.info("build %s: %s (%d errors, %.1fs)", goal, "OK" if ok else "FAILED", len(errors), pr.duration_s)
        return result, text

