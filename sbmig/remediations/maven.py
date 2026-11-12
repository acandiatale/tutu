"""취약점 조치에서 서버의 Maven으로 빌드와 의존성 조회를 실행하는 모듈(``ServerMaven``,
``interfaces.MavenGateway`` 구현).
그래프의 ``baseline_build`` / ``build`` 노드가 package 빌드를, ``inventory``와 검증 단계가
``resolve``(dependency:tree)와 ``packaged``(빌드 결과물 안의 jar 목록)를 호출한다. 명령은 이관과 같은
``sbmig.build.mvn`` 함수로 만든다.
- 항상 서버에 설치된 Maven(``build.mvn``)만 쓴다.
  ``build.mvn``이 프로젝트의 wrapper를 가리키면 준비되지 않은 것으로 본다
- 서버 settings.xml(``build.settings_file``)이 반드시 있어야 하고 ``-s``로 절대 경로를 넘긴다
- ``build.extra_args``에 TLS 검증을 끄는 옵션이나 다른 ``-s`` / ``-f``가 있으면 빌드하지 않는다
- 모든 명령에 ``-Dmaven.test.skip=true``를 붙인다
- Maven은 프로젝트의 ``.mvn/`` 설정을 끌 수 없으므로 접수 단계에서
  거절한다(``remediation.project_maven_config: refuse``, 기본값). ``ignore``이면 빌드할 때마다 WARNING과
  저널 ``maven_project_config``를 남긴다

Server-managed Maven for the remediation flow: package builds, `dependency:tree` resolution and the jars inside the
built war / executable jar. Never the project's wrapper, always the server settings.xml, tests never run, insecure TLS
flags stripped. `.mvn/` cannot be disabled on Maven 3.9 (the launcher always reads it from the first ancestor holding
`.mvn`); the intake refuses it by default, and when the config says `ignore` every build logs a warning.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sbmig.build.deptree import DEPENDENCY_PLUGIN, TreeNode, parse_tree
from sbmig.build.env_errors import http_blocked_followup, http_blocker_hit
from sbmig.build.mvn import ProcessResult, base_command, maven_available, mvn_env, run_logged
from sbmig.build.repo_versions import is_blocking_mirror, read_settings

from .interfaces import Journal, StopCheck
from .inventory import ProjectPoms, _kid, _kids, _text  # 같은 패키지의 lxml pom 모델과 보조 함수
from .models import BuildOutcome, ModuleResolution, PackagedLib, ResolvedDep, ResolvedModel

log = logging.getLogger("sbmig.remediations.maven")

Runner = Callable[..., ProcessResult]               # run_logged와 같은 시그니처
MavenProbe = Callable[[Any], "tuple[bool, str]"]    # maven_available과 같은 시그니처

TEST_SKIP = "-Dmaven.test.skip=true"
RESOLVE_TIMEOUT_S = 30 * 60
MAX_NESTED_JAR_BYTES = 64 * 1024 * 1024           # 산출물 안 jar 하나를 메모리로 읽는 한도
MAX_SUMMARY_ERRORS = 12
SUMMARY_TAIL = 6
# 의존성 조회를 위해 package를 다시 실행할 때 끄는 옵션들이다. 테스트 건너뛰기도 포함한다
PACKAGE_SKIPS = ("-Dmaven.main.skip=true", TEST_SKIP, "-Dspring-boot.repackage.skip=true", "-Denforcer.skip=true",
                 "-Dmaven.javadoc.skip=true", "-Dmaven.source.skip=true", "-Dgpg.skip=true",
                 "-DfailOnMissingWebXml=false")
# TLS 검증을 끄거나 약하게 하는 속성은 서버 설정에 있어도 명령에 넣지 않는다
_INSECURE_RE = re.compile(r"^-D(maven\.wagon\.http\.ssl\.|maven\.resolver\.transport\.ssl|aether\.connector\.https\.securityMode"
                          r"|aether\.transport\.https\.securityMode|maven\.wagon\.https\.)", re.IGNORECASE)
_PROJECT_MVN_FILES = ("maven.config", "jvm.config", "extensions.xml")
_SERVER_SECRET_PREFIXES = ("IVAS_", "LLM_")          # 빌드 프로세스에 넘기지 않을 서버 비밀 변수의 접두어
_CRED_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def _scrub(text: str) -> str:
    """빌드 로그 요약이 보고서와 응답에 실리므로 URL 안의 ``user:password@``를 지운다."""
    return _CRED_URL_RE.sub(r"\1***@", text)


def strip_unsafe_args(args: list[str]) -> tuple[list[str], list[str]]:
    """``build.extra_args``에서 TLS 검증을 끄는 속성과 ``-s`` / ``--settings`` / ``-f`` / ``--file`` 인자를
    빼고 (남은 인자, 뺀 인자)를 반환한다.

    Drop insecure-TLS properties and settings / pom overrides from the configured extra args."""
    kept: list[str] = []
    dropped: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            dropped.append(a)
            skip_next = False
            continue
        if a in ("-s", "--settings", "-f", "--file"):
            dropped.append(a)
            skip_next = True
            continue
        if a.startswith(("--settings=", "--file=")) or _INSECURE_RE.match(a):
            dropped.append(a)
            continue
        kept.append(a)
    return kept, dropped


def unsafe_build_args(cfg: Any) -> list[str]:
    """``build.extra_args`` 중 취약점 조치가 쓸 수 없는 인자의 이름 목록을 반환한다.
    ``-D…=값``의 값은 빼고 이름만 담는다.
    하나라도 있으면 준비 문제로 보고 빌드를 거절한다.
    조용히 빼고 빌드하면 "artifact not found" 같은 엉뚱한 실패가 나기 때문이다.

    Arguments the migration's Maven command carries but the remediation never passes (insecure TLS / settings / pom
    overrides in `build.extra_args`); names only (values of `-D…=` dropped)."""
    try:
        _kept, dropped = strip_unsafe_args([str(a) for a in (cfg.build.extra_args or [])])
    except Exception:                                    # noqa: BLE001 - an unreadable config is checked elsewhere
        return []
    names: list[str] = []
    for a in dropped:
        name = a.split("=", 1)[0] if a.startswith("-") else ""
        if name and name not in names:
            names.append(name)
    return names


def _unsafe_problem(names: list[str]) -> str:
    return ("build.extra_args contains argument(s) the remediation never passes to Maven: " + " ".join(names)
            + " (insecure TLS / settings / pom overrides) — the migration's builds use them, so the remediation would not "
              "build like the migration; remove them from build.extra_args (put the repository certificate into the JDK "
              "trust store instead)")


def summarize(text: str, project: Path | None = None) -> str:
    """빌드 로그를 요약한다.
    서로 다른 ``[ERROR]`` 줄 최대 12개와 마지막 6줄을 담고, 프로젝트 절대 경로와 URL 속 계정 정보는 지운다.

    A few lines of the build log: distinct [ERROR] lines + the tail; project path and URL credentials removed."""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip() and not ln.startswith("# ")]
    errs: list[str] = []
    for ln in lines:
        if ln.startswith(("[ERROR]", "[FATAL]")) and ln not in errs and ln.strip() not in ("[ERROR]", "[FATAL]"):
            errs.append(ln)
    out = errs[:MAX_SUMMARY_ERRORS]
    tail = [ln for ln in lines[-SUMMARY_TAIL:] if ln not in out]
    body = "\n".join(out + (["--- tail ---"] if out and tail else []) + tail)
    if project is not None:
        for prefix in {str(project), str(Path(project).resolve())}:
            body = body.replace(prefix + os.sep, "").replace(prefix, ".")
    return _scrub(body)


def _http_blocker_line(text: str, cfg: Any) -> str:
    """로그에 Maven의 HTTP 차단 미러가 보이면 이관의 인수인계와 같은 안내 문장(``http_blocked_followup``)을
    반환하고, 아니면 빈 문자열을 반환한다.

    The migration's Handover wording for Maven's HTTP blocker when the log shows one, else ""."""
    hit = http_blocker_hit(text or "")
    return http_blocked_followup(hit, cfg) if hit is not None else ""


def _with_http_blocker(summary: str, text: str, cfg: Any) -> str:
    """HTTP 차단 안내 문장이 있으면 요약 맨 앞에 붙인다. 조치 메시지가 이 요약을 그대로 보여 주기 때문이다."""
    line = _http_blocker_line(text, cfg)
    return f"{line}\n{summary}".strip() if line else summary


def _java_major_of(version: str) -> int | None:
    m = re.match(r"\s*\"?(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):                       # 1.8.0 → 8
        major = int(m.group(2))
    return major


def java_home_major(home: str | None) -> int | None:
    """JDK 홈의 ``release`` 파일에서 Java 메이저 버전을 읽는다. 파일이 없거나 읽지 못하면 None을 반환한다.

    Major version from `<java_home>/release`; None when absent."""
    if _blank(home):
        return None
    try:
        text = (Path(str(home)).expanduser() / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^JAVA_VERSION="?([^"\n]+)"?', text, re.MULTILINE)
    return _java_major_of(m.group(1)) if m else None


def build_env(cfg: Any) -> dict[str, str]:
    """``mvn_env``에서 서버 비밀 변수(``IVAS_*`` / ``LLM_*``)를 뺀 빌드 환경 변수를 만든다.
    프로젝트의 빌드 플러그인이 환경 변수를 로그에 남기면 보고서까지 전달되기 때문이다.
    settings.xml이 ``${env.…}``로 읽는 저장소 계정 변수는 그대로 둔다.

    The build environment without this server's own secrets (`IVAS_*`, `LLM_*`): project plugins are foreign code."""
    env = mvn_env(cfg)
    return {k: v for k, v in env.items() if not k.upper().startswith(_SERVER_SECRET_PREFIXES)}


class ServerMaven:
    """서버의 Maven으로 빌드와 의존성 조회를 실행하는 클래스.
    ``runner``와 ``maven_probe``를 주입할 수 있어서 테스트는 가짜 구현을 넣는다.

    The server-managed Maven gateway. `runner` / `maven_probe` are injectable (tests never start a real mvn)."""

    def __init__(self, cfg: Any, runner: Runner | None = None, *, maven_probe: MavenProbe | None = None,
                 journal: Journal | None = None):
        self.cfg = cfg
        self.runner: Runner = runner or run_logged
        self.maven_probe: MavenProbe = maven_probe or maven_available
        self.journal = journal

    # ------------------------------------------------------------------ readiness / hosts
    def readiness(self) -> list[str]:
        """빌드를 막는 문제 목록을 반환한다.
        빈 목록이면 빌드할 준비가 된 것이고, 목록에 비밀 값은 넣지 않는다.

        Problems that prevent a build (empty = ready); no secret values."""
        problems: list[str] = []
        b = self.cfg.build
        exe = Path(str(b.mvn or "mvn")).name.lower()
        if exe.startswith("mvnw"):
            problems.append("build.mvn points at a Maven wrapper (mvnw); the server Maven installation is required")
        ok, info = False, ""
        try:
            ok, info = self.maven_probe(self.cfg)
        except Exception as exc:                        # noqa: BLE001 - a probe failure is a readiness problem
            info = f"{type(exc).__name__}"
        if not ok:
            problems.append(f"Maven not available ({_scrub(str(info))[:200]})")
        want = int(getattr(b, "require_java_major", 0) or 21)
        home = b.java_home if not _blank(b.java_home) else os.environ.get("JAVA_HOME")
        major = java_home_major(home)
        if major is None and ok:
            m = re.search(r"Java version:\s*([0-9.]+)", info or "")
            major = _java_major_of(m.group(1)) if m else None
        if major is None:
            problems.append(f"JDK {want} not found (build.java_home / JAVA_HOME has no release file and Maven did not "
                            "report a Java version)")
        elif major != want:
            problems.append(f"JDK {want} required, found Java {major}")
        unsafe = unsafe_build_args(self.cfg)
        if unsafe:
            problems.append(_unsafe_problem(unsafe))
        if _blank(b.settings_file):
            problems.append("server settings.xml not configured (build.settings_file)")
        else:
            p = Path(str(b.settings_file)).expanduser()
            if not p.is_file():
                problems.append(f"server settings.xml not found ({p})")
            elif read_settings(str(p)) is None:
                problems.append(f"server settings.xml cannot be read ({p})")
            elif not self.allowed_hosts():
                problems.append("no approved repository host (remediation.allowed_repository_hosts is empty and the "
                                "server settings.xml declares no mirror / repository)")
        return problems

    def allowed_hosts(self) -> list[str]:
        """승인된 저장소 호스트 목록을 반환한다. ``remediation.allowed_repository_hosts``가 없으면 서버
        settings.xml의 미러와 저장소 호스트를 쓰고, 차단 미러는 제외한다.

        Approved repository hosts (configured list, else the hosts of the server settings' mirrors / repositories)."""
        configured = [str(h).strip().lower() for h in (self.cfg.remediation.allowed_repository_hosts or []) if str(h).strip()]
        if configured:
            return sorted(set(configured))
        data = read_settings(self.cfg.build.settings_file) if not _blank(self.cfg.build.settings_file) else None
        if data is None:
            return []
        hosts: set[str] = set()
        for mid, url, _mirror_of, _creds in data.mirrors:
            if is_blocking_mirror(url, mid in data.blocked):
                continue
            h = urlsplit(url).hostname
            if h:
                hosts.add(h.lower())
        for _rid, url, _creds in [*data.repos, *data.plugin_repos]:
            h = urlsplit(url).hostname
            if h:
                hosts.add(h.lower())
        return sorted(hosts)

    # ------------------------------------------------------------------ command
    def _command(self, goals: list[str]) -> tuple[list[str] | None, str]:
        """실행할 Maven 명령을 만들어 (명령, 문제)를 반환한다.
        문제가 있으면 명령은 None이고 실행하지 않는다."""
        b = self.cfg.build
        if _blank(b.settings_file):
            return None, "server settings.xml not configured (build.settings_file)"
        base = base_command(self.cfg)
        n = len(b.extra_args or [])
        exe, extra, rest = base[0], base[1:1 + n], base[1 + n:]
        if Path(exe).name.lower().startswith("mvnw"):
            return None, "build.mvn points at a Maven wrapper (mvnw); refused"
        # 이관과 다른 명령으로 빌드하지 않도록, 빼야 할 인자가 있으면 조용히 빼지 않고 거절한다
        unsafe = unsafe_build_args(self.cfg)
        if unsafe:
            return None, _unsafe_problem(unsafe)
        rest = [a for a in rest if not _INSECURE_RE.match(a)]
        # resolve가 이 위치를 기준으로 goal을 끼워 넣으므로 테스트 건너뛰기는 항상 맨 끝에 한 번만 둔다
        cmd = [a for a in [exe, *extra, *rest, *goals] if a != TEST_SKIP] + [TEST_SKIP]
        return cmd, ""

    def _check_project_mvn(self, project: Path) -> None:
        """프로젝트에 ``.mvn`` 설정 파일이 있으면 WARNING과 저널을 남긴다.
        Maven처럼 상위 폴더로 올라가며 ``.mvn``이 있는 첫 폴더를 찾는다."""
        cur = Path(project).resolve()
        for d in [cur, *cur.parents]:
            mvn_dir = d / ".mvn"
            if mvn_dir.is_dir():
                found = [f".mvn/{n}" for n in _PROJECT_MVN_FILES if (mvn_dir / n).exists()]
                if found:
                    where = "project" if d == cur else "an ancestor of the project"
                    log.warning("remediation build: %s in %s — Maven 3.9 reads these files and they cannot be disabled; "
                                "remediation.project_maven_config=%s", ", ".join(found), where,
                                getattr(self.cfg.remediation, "project_maven_config", "?"))
                    if self.journal:
                        try:
                            self.journal({"event": "maven_project_config", "files": found, "where": where,
                                          "policy": getattr(self.cfg.remediation, "project_maven_config", "")})
                        except Exception:       # noqa: BLE001 - journal is best effort
                            pass
                return
        return

    def _run(self, cmd: list[str], project: Path, log_path: Path, timeout_s: float) -> ProcessResult:
        # 이관의 빌드 로그와 비교할 수 있도록 조치에서 실행한 명령과 폴더도 로그에 남긴다
        log.info("remediation mvn %s (cwd=%s)", " ".join(cmd[1:]), Path(project).as_posix())
        return self.runner(cmd, cwd=Path(project), env=build_env(self.cfg), log_path=Path(log_path), timeout_s=timeout_s)

    def _may_force_update(self, cmd: list[str]) -> bool:
        """``-U``를 붙여 다시 실행해도 되는지 판단한다. 이관의 ``MavenRunner``와 같은 규칙으로, 온라인일 때
        한 번만 허용한다(``build.retry_negative_cache``, 기본값 켬)."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in cmd)

    def _run_with_update_retry(self, cmd: list[str], cwd: Path, log_path: Path, timeout_s: float) -> ProcessResult:
        """로컬 저장소에 캐시된 다운로드 실패(``*.lastUpdated``) 때문에 빌드가 실패하면 ``-U``를 붙여 한 번
        더 실행하고 두 번째 결과를 반환한다.
        이관의 ``MavenRunner.run``과 같은 규칙이며, 재시도하면 저널 ``remediation_negative_cache_retry``를 남긴다."""
        from sbmig.build.runner import negative_cache_hits
        pr = self._run(cmd, cwd, log_path, timeout_s)
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in (pr.text or "")
        if ok or pr.timed_out or not self._may_force_update(cmd):
            return pr
        if http_blocker_hit(pr.text or "") is not None:      # v2.1z-ah: Maven's HTTP blocker — -U cannot change it
            return pr
        hits = negative_cache_hits(pr.text or "")
        if not hits:
            return pr
        log.info("remediation maven: negative cache hit for %s → retrying once with -U", ", ".join(hits[:3]))
        if self.journal:
            try:
                self.journal({"event": "remediation_negative_cache_retry", "artifacts": hits[:10],
                              "first_log": Path(log_path).as_posix()})
            except Exception:                           # journal is best effort
                log.debug("negative-cache journal line not written", exc_info=True)
        lp = Path(log_path)
        return self._run([*cmd[:-1], "-U", cmd[-1]], cwd, lp.with_name(f"{lp.stem}-U{lp.suffix}"), timeout_s)

    # ------------------------------------------------------------------ build targets
    def target_runs(self, project: Path, targets: list[str] | None) -> list[tuple[str, Path, list[str]]]:
        """빌드 대상마다 (대상, 작업 폴더, 추가 인자)를 반환한다.
        이관의 ``fixloop.build_target_args``와 같은 규칙이다.
        대상이 없으면 루트에서 한 번 실행하고, 대상이 루트 pom의 ``<modules>``에 있으면 루트에서
        ``-pl <대상> -am``으로, 아니면 그 폴더 안에서 실행한다.

        (target, cwd, extra args) per build target, decided like the migration's `build_target_args`."""
        project = Path(project)
        from .inventory import normalize_roots, parse_pom_file
        roots = normalize_roots(targets)
        if roots == ["."]:
            return [(".", project, [])]
        root_pom = parse_pom_file(project, ".") if (project / "pom.xml").is_file() else None
        listed = {m.strip("/").removeprefix("./") for m in (root_pom.modules if root_pom is not None and root_pom.ok else [])}
        out: list[tuple[str, Path, list[str]]] = []
        for t in roots:
            if t == ".":
                out.append((t, project, []))
            elif t in listed:
                out.append((t, project, ["-pl", t, "-am"]))
            else:
                out.append((t, project / t, []))
        return out

    # ------------------------------------------------------------------ package
    def package(self, project: Path, log_path: Path, stop: StopCheck | None = None,
                targets: list[str] | None = None) -> BuildOutcome:
        """빌드 대상마다 ``mvn clean package -Dmaven.test.skip=true``를 실행한다.
        모두 성공해야 ``ok``이고 첫 실패에서 멈추며, 예외를 던지지 않는다.
        - 종료 코드가 0이고 로그에 BUILD SUCCESS가 있을 때만 성공으로 본다
        - ``stop``은 시작하기 전에만 확인하고 빌드 도중에는 중단하지 않는다
        - 대상이 여럿이면 로그를 ``<이름>-<대상>.log``로 나눠 남기고, 결과의 ``log``는 실패한 대상의 로그,
          실패가 없으면 마지막 로그다

        One package build per build target (the migration's targets; the root when none); never raises."""
        log_str = Path(log_path).as_posix()
        if stop is not None and stop():
            return BuildOutcome(ok=False, log=log_str, summary="stop requested before the build started")
        cmd0, problem = self._command(["clean", "package"])
        if cmd0 is None:
            return BuildOutcome(ok=False, log=log_str, summary=f"not run: {problem}")
        self._check_project_mvn(project)
        timeout_s = float(self.cfg.remediation.build_timeout_minutes) * 60.0
        runs = self.target_runs(Path(project), targets)
        lp0 = Path(log_path)
        total = 0.0
        out: BuildOutcome | None = None
        for target, cwd, args in runs:
            lp = lp0 if len(runs) == 1 else lp0.with_name(f"{lp0.stem}-{target.replace('/', '_')}{lp0.suffix}")
            cmd = [*cmd0[:-1], *args, cmd0[-1]]
            t0 = time.monotonic()
            try:
                pr = self._run_with_update_retry(cmd, cwd, lp, timeout_s)
            except Exception as exc:                    # noqa: BLE001 - the gateway never raises for a build
                log.warning("remediation build could not run: %s", type(exc).__name__)
                return BuildOutcome(ok=False, log=lp.as_posix(), summary=f"build could not run: {type(exc).__name__}",
                                    seconds=round(total + time.monotonic() - t0, 3))
            text = pr.text or ""
            ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in text
            summary = "" if ok else summarize(text, Path(project))
            if not ok:
                summary = _with_http_blocker(summary, text, self.cfg)
            if pr.timed_out:
                summary = f"timed out after {timeout_s:.0f}s\n{summary}".strip()
            elif pr.error:
                summary = f"{_scrub(pr.error)}\n{summary}".strip()
            if not ok and len(runs) > 1:
                summary = f"build target {target}:\n{summary}".strip()
            total += float(pr.duration_s or 0.0)
            out = BuildOutcome(ok=ok, exit_code=pr.exit_code, log=lp.as_posix(), summary=summary,
                               seconds=round(total, 3), timed_out=bool(pr.timed_out))
            if not ok:
                break
        assert out is not None
        return out

    # ------------------------------------------------------------------ resolve
    def resolve(self, project: Path, work: Path, targets: list[str] | None = None) -> ResolvedModel:
        """모듈마다 Maven이 실제로 고른 의존성과 pom에 선언된 플러그인을 조회한다.
        ``dependency:tree``를 ``target_runs`` 규칙대로 대상마다 실행한다.
        트리를 얻지 못한 모듈이 있으면 ``complete=False``로 두고 그 모듈 이름을 남긴다.
        캐시된 다운로드 실패는 ``-U``로 한 번 재시도한다.

        Resolved dependencies per module (one `dependency:tree` run per build target, the root when none) + declared
        plugins. A module without a tree makes the model incomplete (named in `notes`)."""
        project, work = Path(project), Path(work)
        work.mkdir(parents=True, exist_ok=True)
        model = ResolvedModel(complete=True)
        runs = self.target_runs(project, targets)
        if [r[0] for r in runs] != ["."]:
            model.roots = [r[0] for r in runs]
        pp = ProjectPoms(project, roots=model.roots or None)
        if not pp.complete:
            model.complete = False
            model.notes += pp.notes
        mods = [pp.poms[d] for d in pp.reactor if pp.poms[d].ok]
        if not mods:
            model.complete = False
            model.notes.append("no readable root pom.xml" if not model.roots else "no readable pom.xml in the build targets")
            return model
        roots: list[TreeNode] = []
        cmd0, problem = self._command([])
        if cmd0 is None:
            model.complete = False
            model.notes.append(f"dependency:tree not run: {problem}")
        else:
            self._check_project_mvn(project)
            pkg = ["package", *[x for x in PACKAGE_SKIPS if x != TEST_SKIP]]
            for i, (target, cwd, args) in enumerate(runs):
                tag = "" if len(runs) == 1 else f"-{target.replace('/', '_')}"
                out_file = (work / f"deptree{tag}.txt").resolve()
                ok = False
                last_text = ""
                for attempt, extra in enumerate(([], pkg)):
                    out_file.unlink(missing_ok=True)
                    cmd = [*cmd0[:-1], *args, *extra, f"{DEPENDENCY_PLUGIN}:tree", f"-DoutputFile={out_file}",
                           "-DappendOutput=true", TEST_SKIP]
                    try:
                        pr = self._run_with_update_retry(cmd, cwd, work / f"deptree{tag}-{attempt + 1}.log",
                                                         RESOLVE_TIMEOUT_S)
                    except Exception as exc:    # noqa: BLE001
                        log.warning("dependency:tree could not run: %s", type(exc).__name__)
                        break
                    if pr.exit_code == 0 and out_file.is_file():
                        ok = True
                        break
                    last_text = pr.text or ""
                    if http_blocker_hit(last_text) is not None:     # v2.1z-ah: a package run would be refused the same way
                        break
                    if len(mods) == 1:          # 형제 모듈이 없으면 package를 함께 실행해도 결과가 같다
                        break
                if ok:
                    try:
                        roots += parse_tree(out_file.read_text(encoding="utf-8", errors="replace"))
                    except OSError as exc:
                        model.notes.append(f"dependency:tree output unreadable: {type(exc).__name__}")
                else:
                    where = "" if len(runs) == 1 else f" for build target {target}"
                    model.notes.append(f"dependency:tree failed{where} (see deptree{tag}-*.log)")
                    blocked = _http_blocker_line(last_text, self.cfg)
                    if blocked and blocked not in model.notes:
                        model.notes.append(blocked)
        by_ga: dict[str, TreeNode] = {}
        by_aid: dict[str, list[TreeNode]] = {}
        for r in roots:
            by_ga.setdefault(r.gav.key, r)
            by_aid.setdefault(r.gav.artifact_id, []).append(r)
        for pf in mods:
            g, a = pp.interp(pf, pf.group_id), pp.interp(pf, pf.artifact_id)
            mr = ModuleResolution(dir=pf.dir, group_id=g, artifact_id=a, version=pp.interp(pf, pf.version),
                                  packaging=pf.packaging)
            root = by_ga.get(f"{g}:{a}")
            if root is None and len(by_aid.get(a, [])) == 1:
                root = by_aid[a][0]
            if root is None:
                model.complete = False
                model.notes.append(f"module {pf.dir} ({g}:{a}): no dependency tree")
            else:
                mr.dependencies = _tree_deps(root)
                if root.gav.version:
                    mr.version = root.gav.version
            mr.plugins = _declared_plugins(pp, pf)
            model.modules.append(mr)
        if any(m.plugins for m in model.modules):
            model.notes.append("plugin versions are declared in the poms, not resolved")
        return model

    # ------------------------------------------------------------------ packaged
    def packaged(self, project: Path, targets: list[str] | None = None) -> list[PackagedLib]:
        """모듈의 war나 실행 jar 안에 들어 있는 ``WEB-INF/lib`` / ``BOOT-INF/lib``의 jar 목록을 반환한다.
        g:a와 버전은 안쪽 jar의 ``pom.properties``에서만 읽고 파일 이름으로 추측하지 않는다.
        ``targets``는 ``package``와 같은 빌드 대상이다.

        Jars inside the built war / executable jar of every module; coordinates from pom.properties only."""
        project = Path(project)
        from .inventory import normalize_roots
        roots = normalize_roots(targets)
        pp = ProjectPoms(project, roots=None if roots == ["."] else roots)
        dirs = list(pp.reactor) or ["."]
        out: list[PackagedLib] = []
        for d in dirs:
            target = (project if d == "." else project / d) / "target"
            if not target.is_dir():
                continue
            for art in sorted([*target.glob("*.war"), *target.glob("*.jar")]):
                if not art.is_file() or art.is_symlink():
                    continue
                rel = art.relative_to(project).as_posix()
                out += _libs_in(art, rel)
        out.sort(key=lambda x: (x.artifact, x.entry))
        return out


# ---------------------------------------------------------------------------- 도우미
def _gav(node: TreeNode) -> str:
    return f"{node.gav.group_id}:{node.gav.artifact_id}:{node.gav.version or ''}"


def _tree_deps(root: TreeNode) -> list[ResolvedDep]:
    """모듈의 의존성 트리를 실제로 선택된 의존성 목록으로 바꾼다.
    생략된 노드와 충돌 노드는 제외하고, ``path``에는 모듈에서 그 의존성까지 거치는 g:a:v 경로를 담는다."""
    out: list[ResolvedDep] = []

    def visit(node: TreeNode, chain: list[str]) -> None:
        for c in node.children:
            if c.omitted:
                continue                         # 다른 경로로 이미 들어온 같은 jar이므로 선택된 의존성으로 세지 않는다
            out.append(ResolvedDep(group_id=c.gav.group_id, artifact_id=c.gav.artifact_id, version=c.gav.version or "",
                                   scope=c.scope or c.gav.scope or "compile", type=c.gav.type or "jar",
                                   classifier=c.gav.classifier or "", direct=not chain, path=list(chain)))
            visit(c, [*chain, _gav(c)])

    visit(root, [])
    return out


def _declared_plugins(pp: ProjectPoms, pf: Any) -> list[ResolvedDep]:
    """pom에 선언된 빌드 플러그인과 그 플러그인의 ``<dependencies>``를 모은다.
    프로필과 ``<pluginManagement>``도 포함한다.
    버전은 Maven이 해석한 결과가 아니라 선언된 값이며, 프로젝트 속성으로 풀 수 있으면 푼 값을 쓴다."""
    out: list[ResolvedDep] = []
    seen: set[tuple[str, str, str, str]] = set()
    builds = [_kid(pf.root, "build")]
    for prof in _kids(_kid(pf.root, "profiles"), "profile"):
        builds.append(_kid(prof, "build"))
    for b in builds:
        if b is None:
            continue
        holders = [_kid(b, "plugins"), _kid(_kid(b, "pluginManagement"), "plugins")]
        for h in holders:
            for pl in _kids(h, "plugin"):
                g = pp.interp(pf, _text(_kid(pl, "groupId"))) or "org.apache.maven.plugins"
                a = pp.interp(pf, _text(_kid(pl, "artifactId")))
                if not a:
                    continue
                v = pp.interp(pf, _text(_kid(pl, "version")))
                key = (g, a, v, "")
                if key not in seen:
                    seen.add(key)
                    out.append(ResolvedDep(group_id=g, artifact_id=a, version=v, scope="plugin", type="maven-plugin",
                                           direct=True))
                for dep in _kids(_kid(pl, "dependencies"), "dependency"):
                    dg, da = pp.interp(pf, _text(_kid(dep, "groupId"))), pp.interp(pf, _text(_kid(dep, "artifactId")))
                    dv = pp.interp(pf, _text(_kid(dep, "version")))
                    k2 = (dg, da, dv, f"{g}:{a}")
                    if not dg or not da or k2 in seen:
                        continue
                    seen.add(k2)
                    out.append(ResolvedDep(group_id=dg, artifact_id=da, version=dv, scope="plugin",
                                           direct=False, path=[f"{g}:{a}:{v}"]))
    return out


def _props(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in data.decode("utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", "!")) or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _libs_in(art: Path, rel: str) -> list[PackagedLib]:
    out: list[PackagedLib] = []
    try:
        with zipfile.ZipFile(art) as zf:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir() or not name.lower().endswith(".jar"):
                    continue
                if not name.startswith(("WEB-INF/lib/", "BOOT-INF/lib/")):
                    continue
                lib = PackagedLib(artifact=rel, entry=name, file_name=name.rsplit("/", 1)[-1])
                if info.file_size <= MAX_NESTED_JAR_BYTES:
                    try:
                        lib.ga, lib.version = _coords_of(zf.read(info), lib.file_name)
                    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
                        log.info("packaged: %s!%s unreadable: %s", rel, name, type(exc).__name__)
                else:
                    log.info("packaged: %s!%s is larger than %d bytes — coordinates not read", rel, name,
                             MAX_NESTED_JAR_BYTES)
                out.append(lib)
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("packaged: %s unreadable: %s", rel, type(exc).__name__)
    return out


def _coords_of(data: bytes, file_name: str) -> tuple[str, str]:
    """안쪽 jar의 pom.properties에서 (g:a, 버전)을 읽는다. 여러 개 있으면 파일 이름이
    ``<artifactId>-<version>``으로 시작하는 것을 고르고, 고르지 못하면 빈 값을 반환한다."""
    with zipfile.ZipFile(io.BytesIO(data)) as inner:
        cands: list[tuple[str, str]] = []
        for n in inner.namelist():
            if n.startswith("META-INF/maven/") and n.endswith("/pom.properties"):
                p = _props(inner.read(n))
                g, a, v = p.get("groupId", ""), p.get("artifactId", ""), p.get("version", "")
                if g and a:
                    cands.append((f"{g}:{a}", v))
    if len(cands) == 1:
        return cands[0]
    hits = [c for c in cands if file_name.startswith(f"{c[0].split(':', 1)[1]}-{c[1]}")]
    return hits[0] if len(hits) == 1 else ("", "")
