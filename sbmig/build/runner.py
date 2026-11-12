"""Maven 빌드를 한 번 실행하고, 실패하면 로그를 파싱해 ``BuildResult``로 반환하는 모듈(``BuildRunnerLike``).
검증 빌드, 수정 루프의 라운드별 재빌드, 최종 빌드가 모두 ``MavenRunner.run``을 쓴다.
결과의 ``errors``로 오류 목록과 케이스를 만든다.
- 테스트는 항상 건너뛴다(``build.skip_test_compile: true`` -> ``-Dmaven.test.skip=true``)
- 빌드가 실패해도 예외를 던지지 않고 결과로 반환한다. 빌드 게이트가 이것을 전제로 한다
- Windows 파일 잠금 때문에 실패한 빌드는 잠시 기다렸다 다시 실행한다(``_retry_file_lock``)

MavenRunner — implements ``BuildRunnerLike``: one real ``mvn`` run, full log captured, errors parsed."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path

from sbmig.core.config import Config
from sbmig.core.models import BuildError, BuildResult, ErrorKind
from sbmig.core.workspace import Workspace

from .env_errors import file_lock_hit, http_blocker_hit
from .log_parser import (
    _is_boilerplate,
    _root_prefixes,
    _scrub,
    _split_level,
    parse_maven_log,
    reactor_artifact_ids_from_poms,
    parse_reactor_summary,
)
from .mvn import base_command, maven_available, mvn_env, run_logged
from .signatures import external_prefixes_from_config, fill_signatures, register_external_prefixes, set_reactor_modules

log = logging.getLogger("sbmig.build")

MAX_SUMMARY_ERRORS = 60
SUMMARY_TAIL = 25
# 파일 잠금 재시도 사이에 기다리는 함수, 테스트에서 교체 / the wait between file-lock retries (tests patch it)
sleep_seconds = time.sleep

__all__ = ["MavenRunner", "build_summary", "maven_available", "negative_cache_hits"]

# Maven은 실패한 다운로드를 ``*.lastUpdated``로 기억하고 갱신 주기(기본 하루) 전에는 다시 받지 않으므로, 이
# 문구가 보이면 ``-U``로 한 번 더 실행한다
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
# build extension, 부모 / import POM 해결 실패는 원인이 오래된 표식이어도 cached 문구가 나오지 않으므로 이
# 문구도 -U 재시도 대상으로 본다
# A model-level failure (build extension, parent / import POM) prints no "was cached" wording even when a stale
# `*.lastUpdated` marker is the only cause (the company PC: the pom had been unreachable behind the HTTP blocker, Nexus served
# it again, Maven kept refusing for a day) — so these wordings are -U candidates too; a really missing artifact costs one build.
_MODEL_RES_RE = re.compile(r"Failed to read artifact descriptor for (?P<a>" + _COORDS + r")|"
                           r"Non-resolvable (?:parent|import) POM[^\n]*?Could not find artifact (?P<b>" + _COORDS + r")")
# a model-level line that names a TRANSFER failure (`… Could not transfer artifact … Connection refused`) is the repository being
# down, not a stale marker — -U would only double every build while Nexus is unreachable (review 2026-11-05)
_TRANSFER_RE = re.compile(r"Could not transfer artifact|Connection refused|Connection timed out|Read timed out")


def negative_cache_hits(text: str) -> list[str]:
    """로컬 저장소에 실패가 캐시되어 해결되지 않은 좌표 목록을 반환한다.
    좌표를 읽지 못하면 ["?"]를, 그런 줄이 없으면 빈 목록을 반환한다.

    Coordinates (g:a[:type]:v as Maven printed them) whose resolution failed only because the failure was cached
    in the local repository — plus (2026-11-05) the model-level failures that print no cached wording at all
    (`Failed to read artifact descriptor for …`, `Non-resolvable parent / import POM … Could not find artifact …`),
    since a stale marker shows exactly like that. Empty when the log has no such line."""
    out: list[str] = []
    cached = False
    for line in (text or "").splitlines():
        if _NEGATIVE_CACHE_RE.search(line):
            cached = True
            for m in _NEGATIVE_GAV_RE.finditer(line):
                gav = m.group("a") or m.group("b")
                if gav and gav not in out:
                    out.append(gav)
            continue
        if _TRANSFER_RE.search(line):
            continue
        for m in _MODEL_RES_RE.finditer(line):
            gav = m.group("a") or m.group("b")
            if gav and gav not in out:
                out.append(gav)
    return out or (["?"] if cached else [])


def build_summary(text: str, project_root: Path | str | None = None) -> str:
    """빌드 로그를 요약한다.
    의미 있는 ``[ERROR]`` 줄(최대 60줄)과 마지막 25줄을 남기고 프로젝트 루트 경로는 지운다.

    All distinct meaningful ``[ERROR]`` lines (max 60) + the last 25 lines; project root stripped from paths."""
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
    """Maven 빌드를 실행하고 ``BuildResult``를 반환하는 클래스(``MavenRunner(ws, cfg).run(goal)``).
    빌드가 실패해도 예외를 던지지 않는다.
    사내 아티팩트의 groupId를 라이브러리 패키지로 등록해 L1 서명에서 ``*``로 가려지지 않게 한다.

    ``MavenRunner(ws, cfg).run("test-compile")`` → ``BuildResult``. Never raises for a failed build."""

    def __init__(self, ws: Workspace, cfg: Config, *, timeout_s: float | None = None):
        self.ws = ws
        self.cfg = cfg
        self.timeout_s = timeout_s if timeout_s is not None else float(cfg.build.timeout_minutes) * 60.0
        register_external_prefixes(external_prefixes_from_config(cfg))

        # 앞 프로젝트의 모듈 이름이 남지 않도록 <module>로 가릴 이름 목록을
        # 비우고 시작한다 / per run: start with an empty registry
        set_reactor_modules(())
    # ------------------------------------------------------------------ command
    def command(self, goal: str, *, extra_args: Iterable[str] = (), skip_test_compile: bool = False,
                skip_tests: bool = False) -> list[str]:
        """mvn 명령을 만든다. ``skip_test_compile``이면 ``-Dmaven.test.skip=true``를 붙이고, 아닐 때만
        ``skip_tests``에 따라 ``-DskipTests``를 붙인다. 두 플래그를 함께 붙이지 않는다.

        `skip_tests` (round 8 tests-skipped fallback) → `-DskipTests` even when `build.skip_tests` is off;
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
        """빌드를 한 번 실행하고 결과를 반환한다.
        ``build.retry_negative_cache``(기본 켬)가 켜져 있으면 캐시된 해결 실패 때문에 실패한 빌드를 ``-U``로
        다시 실행하고 두 번째 결과를 반환한다.

        One build. With `build.retry_negative_cache` (default on) a build that failed on a *cached* resolution
        failure is run once more with `-U` and the second result is returned."""
        extra_args = list(extra_args)
        result, text = self._run_once(goal, module=module, extra_args=extra_args, label=label,
                                      skip_test_compile=skip_test_compile, skip_tests=skip_tests)
        # Maven의 HTTP 차단 미러는 기다리거나 -U를 붙여도 해결되지 않으므로 재시도하지 않는다.
        # 수정 루프가 실행을 중단한다
        if not result.ok and http_blocker_hit(text) is not None:
            log.warning("maven: Maven's HTTP repository blocker refused a repository — not retried (fix the Maven settings)")
            return result
        # Windows 파일 잠금은 환경 문제이므로 수정 루프로 넘기지 않고 기다렸다가 같은 빌드를 다시 실행한다
        result, text = self._retry_file_lock(goal, module=module, extra_args=extra_args, label=label,
                                             skip_test_compile=skip_test_compile, skip_tests=skip_tests,
                                             result=result, text=text)
        if result.ok or not self._may_force_update(result.command):
            return result
        # 정말 없는 아티팩트면 이후 빌드가 모두 두 번씩 실행되므로 -U 재시도는 아티팩트마다 실행당 한 번만 한다
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

    def _retry_file_lock(self, goal: str, *, module: str | None, extra_args: list[str], label: str,
                         skip_test_compile: bool, skip_tests: bool, result: BuildResult,
                         text: str) -> tuple[BuildResult, str]:
        """실패한 빌드 로그에 Windows 파일 잠금이 보이면 ``build.file_lock_retry_wait_s``초 기다렸다 같은
        명령을 다시 실행한다.
        최대 ``build.file_lock_retries``번까지 재시도하고 마지막 결과를 반환한다.
        시간 초과와 시작 실패는 재시도하지 않는다.

        v2.1r: while the failed build's log shows a Windows file lock (`sbmig.build.env_errors.file_lock_hit`), wait
        `build.file_lock_retry_wait_s` and run the same command again, up to `build.file_lock_retries` times; each
        retry is logged (`maven: Windows file lock on <path> → retrying in Ns (k/N)`) and journaled
        (`build_file_lock_retry`). A timed-out build is not retried. Returns the last run's (result, text)."""
        retries = max(0, int(getattr(self.cfg.build, "file_lock_retries", 3) or 0))
        wait = max(0.0, float(getattr(self.cfg.build, "file_lock_retry_wait_s", 5.0) or 0.0))
        attempt = 0
        while not result.ok and not result.timed_out and attempt < retries:
            path = file_lock_hit(text, self.cfg)
            if path is None:
                break
            attempt += 1
            log.info("maven: 파일 잠김(다른 프로세스가 사용 중) %s → %gs 뒤 같은 빌드를 다시 (%d/%d) — Windows file lock on "
                     "%s → retrying in %gs (%d/%d)", path, wait, attempt, retries, path, wait, attempt, retries)
            self.ws.journal({"event": "build_file_lock_retry", "goal": goal, "module": module, "path": path,
                             "attempt": attempt, "wait_s": wait, "first_log": result.log_path})
            if wait:
                sleep_seconds(wait)
            result, text = self._run_once(goal, module=module, extra_args=extra_args,
                                          label=f"{label or goal}-lock{attempt}", skip_test_compile=skip_test_compile,
                                          skip_tests=skip_tests)
        if attempt and not result.ok and file_lock_hit(text, self.cfg) is not None:
            log.warning("maven: target/ 가 %d번 다시 돌려도 잠겨 있다 — 백신 실시간 검사 예외 / IDE·탐색기·java 프로세스를 "
                        "확인할 것 (still locked after %d retries)", attempt, attempt)
        return result, text

    def _may_force_update(self, cmd: list[str]) -> bool:
        """-U 재시도를 해도 되는지 확인한다. offline이거나 명령에 이미 -U가 있으면 하지 않는다.

        -U only makes sense online and once: not offline (`-o`), not when the command already forces updates."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in cmd)

    def _run_once(self, goal: str, *, module: str | None, extra_args: list[str], label: str,
                  skip_test_compile: bool, skip_tests: bool) -> tuple[BuildResult, str]:
        """빌드를 실제로 한 번 실행하고 (결과, 로그 전문)을 반환한다.
        명령 생성 -> ``run_logged`` -> 성공 여부 확인 -> 실패하면 로그 파싱 순서다.
        실패한 빌드의 오류 목록이 비지 않도록 타임아웃, 시작 실패, 오류를 찾지 못한 실패도 오류 하나로 만든다."""
        cwd, file_args = self._cwd(module)
        cmd = self.command(goal, extra_args=[*file_args, *extra_args], skip_test_compile=skip_test_compile,
                           skip_tests=skip_tests)
        log_path = self.ws.new_log_path(f"build-{label or goal}")
        log.info("mvn %s (cwd=%s)", " ".join(cmd[1:]), self.ws.rel(cwd) if cwd != self.ws.root else ".")
        pr = run_logged(cmd, cwd=cwd, env=mvn_env(self.cfg), log_path=log_path, timeout_s=self.timeout_s)
        text = pr.text
        # JVM이 죽거나 출력이 잘린 경우를 성공으로 보지 않도록 exit 0과 'BUILD SUCCESS' 문구를 함께 확인한다
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in text
        if ok:
            # 성공한 빌드는 로그를 파싱하지 않으므로 다음 프로젝트에 이름이 남지 않게 <module>로 가릴 이름
            # 목록을 여기서 비운다 / a successful build is not parsed, so the
            # reactor-module registry behind the <module> masking is cleared here instead of lingering into the next project
            set_reactor_modules(())
        errors: list[BuildError] = [] if ok else parse_maven_log(
            text, self.ws.root, known_modules=reactor_artifact_ids_from_poms(self.ws.root))
        if pr.timed_out:
            errors.append(fill_signatures(BuildError(
                kind=ErrorKind.UNKNOWN, module=module,
                message=f"build timed out after {self.timeout_s / 60:.1f} minutes (goal {goal})",
                raw="\n".join(text.splitlines()[-40:]))))
        elif pr.error:
            errors = [fill_signatures(BuildError(kind=ErrorKind.UNKNOWN, message=pr.error, raw=pr.error))]
        # 오류 목록이 비어 있으면 수정 루프가 고칠 케이스가 없어 그냥 끝나므로 오류 하나를 만든다
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

