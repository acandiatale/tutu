"""Maven 빌드 한 번을 실제로 돌리고(전체 로그 저장) 실패면 로그를 파싱해 ``BuildResult`` 로
돌려주는 실행기(``BuildRunnerLike``). 흐름상 위치: 변환 후 컴파일, 검증 빌드(``mvn clean package
-Dmaven.test.skip=true``), 수정 루프의 매 라운드 재빌드, 마지막 최종 빌드가 모두 ``MavenRunner.run`` 이다.
결과의 ``errors`` 가 오류 목록과 케이스(L2 서명)의 재료가
된다. 핵심 진입점: ``MavenRunner.run`` / ``command``,
``build_summary``(``BuildResult.summary``), ``negative_cache_hits``. v2.1r: Windows 파일 잠금(target/)으로 실패한
빌드는 기다렸다 같은 빌드를 다시 돌린다(``_retry_file_lock``).
지킬 것: 테스트는 항상 건너뛴다(``build.skip_test_compile: true`` → ``-Dmaven.test.skip=true``, 사용자 결정
2026-10-22). 실패한 빌드도 예외가 아니라 결과다 — 빌드
게이트(실행은 늘 마지막 빌드 결과로 끝난다)가 이것을 전제로 한다.

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

from .env_errors import file_lock_hit
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
# 테스트가 바꿔 끼우는 대기 함수(파일 잠금 재시도 사이) — the wait between file-lock retries (tests patch it)
sleep_seconds = time.sleep

__all__ = ["MavenRunner", "build_summary", "maven_available", "negative_cache_hits"]

# Maven 은 실패한 다운로드를 로컬 저장소에 기억하고(``*.lastUpdated``) 갱신 주기(기본 하루)가 지나기 전에는
# 다시 묻지 않는다 → Nexus 관리자가 아티팩트를 올린 뒤에도 같은 실패가 하루 동안 이어졌다(round 10). 그래서 이
# 문구가 보이면 ``-U`` 로 한 번 더 돌린다(``MavenRunner.run``).
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
    """로컬 저장소에 '실패가 캐시돼서' 해결되지 않은 좌표들(Maven 이 찍은 g:a[:type]:v 그대로). 캐시 문구는
    있는데 좌표를 못 읽으면 ["?"], 그런 줄이 없으면 [].

    Coordinates (g:a[:type]:v as Maven printed them) whose resolution failed only because the failure was cached
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
    """빌드 로그 요약(``BuildResult.summary``): 서로 다른 의미 있는 ``[ERROR]`` 줄(최대 60) + 마지막 25줄.
    프로젝트 루트 경로는 지운다(PC 마다 다른 절대경로를 남기지 않게).

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
    """``MavenRunner(ws, cfg).run(goal)`` → ``BuildResult``. 빌드가 실패해도 예외를 내지 않는다. 만들
    때 사내 아티팩트의 groupId 를 '라이브러리 패키지' 로 등록해(``register_external_prefixes``) L1 서명에서
    ``*`` 로 가려지지 않게 한다.

    ``MavenRunner(ws, cfg).run("test-compile")`` → ``BuildResult``. Never raises for a failed build."""

    def __init__(self, ws: Workspace, cfg: Config, *, timeout_s: float | None = None):
        self.ws = ws
        self.cfg = cfg
        self.timeout_s = timeout_s if timeout_s is not None else float(cfg.build.timeout_minutes) * 60.0
        register_external_prefixes(external_prefixes_from_config(cfg))

    # ------------------------------------------------------------------ command
    def command(self, goal: str, *, extra_args: Iterable[str] = (), skip_test_compile: bool = False,
                skip_tests: bool = False) -> list[str]:
        """mvn 명령 조립. ``build.skip_test_compile``(v2 기본 true) 또는 인자 →
        ``-Dmaven.test.skip=true``(테스트 컴파일·실행 모두 안 함). 그렇지 않을 때만 ``build.skip_tests`` /
        ``skip_tests`` → ``-DskipTests``. 두 플래그를 같이 붙이지 않는다.

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
        """빌드 한 번. ``build.retry_negative_cache``(기본 켬)이면 '캐시된 해결 실패' 때문에 실패한 빌드를
        ``-U`` 로 한 번 더 돌려 두 번째 결과를 돌려준다.

        One build. With `build.retry_negative_cache` (default on) a build that failed on a *cached* resolution
        failure is run once more with `-U` and the second result is returned."""
        extra_args = list(extra_args)
        result, text = self._run_once(goal, module=module, extra_args=extra_args, label=label,
                                      skip_test_compile=skip_test_compile, skip_tests=skip_tests)
        # v2.1r: Windows 파일 잠금(target/)은 환경 문제 — 기다렸다 같은 빌드를 다시(수정 루프·LLM 까지 가지 않게)
        result, text = self._retry_file_lock(goal, module=module, extra_args=extra_args, label=label,
                                             skip_test_compile=skip_test_compile, skip_tests=skip_tests,
                                             result=result, text=text)
        if result.ok or not self._may_force_update(result.command):
            return result
        # 아티팩트마다 실행당 한 번만 -U: 정말 없는 아티팩트라면 이후 모든 빌드가 두 번씩 돌게 된다.
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
        """v2.1r: 실패한 빌드의 로그에 Windows 파일 잠금(``target/`` 의 파일을 다른 프로세스가 잡음, 패턴은
        ``knowledge/config/build_env_errors.yml``)이 보이면 ``build.file_lock_retry_wait_s`` 초 기다렸다 같은 명령을
        다시 돌린다(최대 ``build.file_lock_retries`` 번). 시간 초과·시작 실패는 다시 돌리지 않는다. 마지막 결과를 돌려준다.

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
        """-U 는 온라인에서, 한 번만 의미가 있다: offline(-o)이거나 명령에 이미 -U 가 있으면 하지 않는다.

        -U only makes sense online and once: not offline (`-o`), not when the command already forces updates."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in cmd)

    def _run_once(self, goal: str, *, module: str | None, extra_args: list[str], label: str,
                  skip_test_compile: bool, skip_tests: bool) -> tuple[BuildResult, str]:
        """실제 실행 한 번: 명령 조립 → ``run_logged`` → 성공 판정(exit 0 + 타임아웃 아님 + 로그에
        'BUILD SUCCESS') → 실패면 로그 파싱. 타임아웃·시작 실패·'오류 없는 실패' 도 오류 하나로 만든다(실패한
        빌드의 오류 목록은 비지 않는다). (결과, 로그 전문) 을 돌려준다."""
        cwd, file_args = self._cwd(module)
        cmd = self.command(goal, extra_args=[*file_args, *extra_args], skip_test_compile=skip_test_compile,
                           skip_tests=skip_tests)
        log_path = self.ws.new_log_path(f"build-{label or goal}")
        log.info("mvn %s (cwd=%s)", " ".join(cmd[1:]), self.ws.rel(cwd) if cwd != self.ws.root else ".")
        pr = run_logged(cmd, cwd=cwd, env=mvn_env(self.cfg), log_path=log_path, timeout_s=self.timeout_s)
        text = pr.text
        # exit 0 만으로는 성공이 아니다: 'BUILD SUCCESS' 문구까지 있어야
        # 한다(JVM 이 죽거나 출력이 잘린 경우를 성공으로 오인하지 않게).
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in text
        errors: list[BuildError] = [] if ok else parse_maven_log(text, self.ws.root)
        if pr.timed_out:
            errors.append(fill_signatures(BuildError(
                kind=ErrorKind.UNKNOWN, module=module,
                message=f"build timed out after {self.timeout_s / 60:.1f} minutes (goal {goal})",
                raw="\n".join(text.splitlines()[-40:]))))
        elif pr.error:
            errors = [fill_signatures(BuildError(kind=ErrorKind.UNKNOWN, message=pr.error, raw=pr.error))]
        # 실패인데 파싱된 오류가 없으면 하나를 만든다: 오류 목록이
        # 비면 수정 루프가 고칠 케이스가 없어 그냥 끝나 버린다.
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

