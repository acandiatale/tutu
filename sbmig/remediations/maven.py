"""서버가 관리하는 Maven 으로 취약점 조치의 빌드를 돌린다(``interfaces.MavenGateway`` 의 진짜 구현 ``ServerMaven``).
흐름상 위치: 그래프의 ``baseline_build`` / ``build`` 노드(package), ``inventory`` / 검증(``resolve`` = dependency:tree,
``packaged`` = war / 실행 jar 안의 jar 목록). 명령은 이관 흐름과 같은 ``sbmig.build.mvn`` 도우미(``base_command`` /
``mvn_env`` / ``run_logged``)로 만든다 — 같은 settings / 로컬 저장소 / 프로세스 트리 종료 방식.

실행 경계(서버 설정을 프로젝트가 바꾸지 못하게):
    * 늘 서버 Maven(``build.mvn``)만 쓴다. ``./mvnw`` 는 쓰지 않는다 — ``build.mvn`` 이 wrapper 를 가리키면 준비 안 됨.
    * 서버 settings.xml(``build.settings_file``)이 **필수**다(이관 흐름과 다르다). ``-s`` 로 절대경로를 넘긴다.
    * ``build.extra_args`` 에 섞인 TLS 검증 끄기(``-Dmaven.wagon.http.ssl.insecure`` …), 다른 ``-s`` / ``-f`` 는 뺀다.
    * 테스트는 돌리지 않는다: 모든 명령에 ``-Dmaven.test.skip=true``.
    * ``.mvn/``: Maven 3.9 의 ``mvn`` 실행 스크립트는 작업 폴더(또는 ``-f`` 의 폴더)에서 위로 올라가며 ``.mvn`` 폴더가 있는 첫
      폴더를 프로젝트 기준 폴더로 삼고, 거기의 ``.mvn/maven.config`` (명령 인자 앞에 붙임), ``.mvn/jvm.config`` (JVM 옵션),
      ``.mvn/extensions.xml`` (코어 확장 로드)을 **늘** 읽는다. 끄는 플래그는 없다(``-Dmaven.multiModuleProjectDirectory``
      는 스크립트가 스스로 정하는 값이라 소용없고, ``-f`` 로 다른 곳에서 띄워도 스크립트가 그 pom 의 폴더에서 다시 찾는다).
      그래서 막는 일은 접수 단계의 경계 검사(``remediation.project_maven_config: refuse``, 기본)가 하고, 설정이 ``ignore`` 라
      그 파일들이 있는 채로 여기까지 왔으면 빌드마다 WARNING + 저널 ``maven_project_config`` 를 남긴다.

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
from sbmig.build.mvn import ProcessResult, base_command, maven_available, mvn_env, run_logged
from sbmig.build.repo_versions import is_blocking_mirror, read_settings

from .interfaces import Journal, StopCheck
from .inventory import ProjectPoms, _kid, _kids, _text  # 같은 패키지의 lxml pom 모델·도우미
from .models import BuildOutcome, ModuleResolution, PackagedLib, ResolvedDep, ResolvedModel

log = logging.getLogger("sbmig.remediations.maven")

Runner = Callable[..., ProcessResult]               # run_logged 와 같은 모양: (cmd, *, cwd, env, log_path, timeout_s)
MavenProbe = Callable[[Any], "tuple[bool, str]"]    # maven_available 과 같은 모양

TEST_SKIP = "-Dmaven.test.skip=true"
RESOLVE_TIMEOUT_S = 30 * 60
MAX_NESTED_JAR_BYTES = 64 * 1024 * 1024           # 산출물 안 jar 하나를 메모리로 읽는 한도
MAX_SUMMARY_ERRORS = 12
SUMMARY_TAIL = 6
# 빌드가 package 를 다시 돌릴 때 끄는 것들(deptree / classpath 와 같은 묶음; 테스트 건너뛰기 포함)
PACKAGE_SKIPS = ("-Dmaven.main.skip=true", TEST_SKIP, "-Dspring-boot.repackage.skip=true", "-Denforcer.skip=true",
                 "-Dmaven.javadoc.skip=true", "-Dmaven.source.skip=true", "-Dgpg.skip=true",
                 "-DfailOnMissingWebXml=false")
# TLS 검증을 끄거나 약하게 하는 속성 — 서버 설정에 섞여 있어도 명령에 넣지 않는다
_INSECURE_RE = re.compile(r"^-D(maven\.wagon\.http\.ssl\.|maven\.resolver\.transport\.ssl|aether\.connector\.https\.securityMode"
                          r"|aether\.transport\.https\.securityMode|maven\.wagon\.https\.)", re.IGNORECASE)
_PROJECT_MVN_FILES = ("maven.config", "jvm.config", "extensions.xml")
_SERVER_SECRET_PREFIXES = ("IVAS_", "LLM_")          # 빌드 프로세스에 넘기지 않는 이 서버의 비밀 변수 앞머리
_CRED_URL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def _scrub(text: str) -> str:
    """URL 안의 ``user:password@`` 를 지운다(빌드 로그 요약이 보고서·응답에 실린다)."""
    return _CRED_URL_RE.sub(r"\1***@", text)


def strip_unsafe_args(args: list[str]) -> tuple[list[str], list[str]]:
    """서버 ``build.extra_args`` 에서 TLS 검증 끄기 속성과 settings / pom 위치를 바꾸는 인자(``-s`` / ``--settings`` /
    ``-f`` / ``--file``)를 뺀다. (남은 인자, 뺀 인자).

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


def summarize(text: str, project: Path | None = None) -> str:
    """빌드 로그 요약 몇 줄: 서로 다른 ``[ERROR]`` 줄(최대 12) + 마지막 6줄. 프로젝트 절대경로와 URL 자격증명은 지운다.

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


def _java_major_of(version: str) -> int | None:
    m = re.match(r"\s*\"?(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):                       # 1.8.0 → 8
        major = int(m.group(2))
    return major


def java_home_major(home: str | None) -> int | None:
    """JDK 홈의 ``release`` 파일(``JAVA_VERSION="21.0.2"``)에서 메이저 버전. 없거나 못 읽으면 None.

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
    """빌드 프로세스의 환경: ``mvn_env`` 에서 이 서버 자신의 비밀(``IVAS_*`` / ``LLM_*`` 로 시작하는 변수)을 뺀다.
    왜: 올린 프로젝트의 빌드 플러그인은 남의 코드다 — 환경 변수를 읽어 빌드 로그에 찍으면 로그 요약을 거쳐 보고서까지 간다.
    저장소 자격증명 변수(서버 settings.xml 이 ``${env.…}`` 로 읽을 수 있다)는 건드리지 않는다.

    The build environment without this server's own secrets (`IVAS_*`, `LLM_*`): project plugins are foreign code."""
    env = mvn_env(cfg)
    return {k: v for k, v in env.items() if not k.upper().startswith(_SERVER_SECRET_PREFIXES)}


class ServerMaven:
    """서버 Maven 게이트웨이(``interfaces.MavenGateway``). ``runner`` 는 ``run_logged`` 와 같은 모양의 함수(테스트는 가짜를
    넣는다 — 진짜 mvn 은 돌리지 않는다), ``maven_probe`` 는 ``maven_available`` 과 같은 모양, ``journal`` 은 저널 한 줄.

    The server-managed Maven gateway. `runner` / `maven_probe` are injectable (tests never start a real mvn)."""

    def __init__(self, cfg: Any, runner: Runner | None = None, *, maven_probe: MavenProbe | None = None,
                 journal: Journal | None = None):
        self.cfg = cfg
        self.runner: Runner = runner or run_logged
        self.maven_probe: MavenProbe = maven_probe or maven_available
        self.journal = journal

    # ------------------------------------------------------------------ readiness / hosts
    def readiness(self) -> list[str]:
        """빌드를 못 하게 하는 문제들(빈 목록 = 준비됨). 비밀 값은 넣지 않는다.

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
        """승인된 저장소 호스트: ``remediation.allowed_repository_hosts`` 가 있으면 그것, 없으면 서버 settings.xml 의 미러·
        활성 프로필 저장소·플러그인 저장소의 호스트(소문자, 포트 없음; 차단 미러 제외).

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
        """서버 명령: (명령, 문제). 문제가 있으면 명령은 None — 돌리지 않는다."""
        b = self.cfg.build
        if _blank(b.settings_file):
            return None, "server settings.xml not configured (build.settings_file)"
        base = base_command(self.cfg)
        n = len(b.extra_args or [])
        exe, extra, rest = base[0], base[1:1 + n], base[1 + n:]
        if Path(exe).name.lower().startswith("mvnw"):
            return None, "build.mvn points at a Maven wrapper (mvnw); refused"
        extra, dropped = strip_unsafe_args(list(extra))
        if dropped:
            log.warning("remediation build: dropped %d argument(s) from build.extra_args (insecure TLS / settings / "
                        "pom overrides): %s", len(dropped), " ".join(a for a in dropped if not a.startswith("-D")) or
                        ", ".join(a.split("=", 1)[0] for a in dropped))
        rest = [a for a in rest if not _INSECURE_RE.match(a)]
        # 테스트 건너뛰기는 늘 맨 끝에 한 번(resolve 가 이 자리를 기준으로 목표를 끼운다)
        cmd = [a for a in [exe, *extra, *rest, *goals] if a != TEST_SKIP] + [TEST_SKIP]
        return cmd, ""

    def _check_project_mvn(self, project: Path) -> None:
        """``.mvn`` 설정 파일이 있으면(Maven 이 늘 읽는다) WARNING + 저널. 위로 올라가며 ``.mvn`` 이 있는 첫 폴더를 본다 —
        Maven 실행 스크립트가 그렇게 찾기 때문이다."""
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
        return self.runner(cmd, cwd=Path(project), env=build_env(self.cfg), log_path=Path(log_path), timeout_s=timeout_s)

    # ------------------------------------------------------------------ package
    def package(self, project: Path, log_path: Path, stop: StopCheck | None = None) -> BuildOutcome:
        """``mvn clean package -Dmaven.test.skip=true`` (서버 명령). ``ok`` 는 종료 코드 0 이고 로그에 BUILD SUCCESS 가
        있을 때만. 실패해도 예외 없음. ``stop`` 은 시작 전에만 본다(빌드 도중에는 끊지 않는다 — 경계에서 멈춘다).

        One package build with the server command; never raises."""
        log_str = Path(log_path).as_posix()
        if stop is not None and stop():
            return BuildOutcome(ok=False, log=log_str, summary="stop requested before the build started")
        cmd, problem = self._command(["clean", "package"])
        if cmd is None:
            return BuildOutcome(ok=False, log=log_str, summary=f"not run: {problem}")
        self._check_project_mvn(project)
        timeout_s = float(self.cfg.remediation.build_timeout_minutes) * 60.0
        t0 = time.monotonic()
        try:
            pr = self._run(cmd, project, Path(log_path), timeout_s)
        except Exception as exc:                        # noqa: BLE001 - the gateway never raises for a build
            log.warning("remediation build could not run: %s", type(exc).__name__)
            return BuildOutcome(ok=False, log=log_str, summary=f"build could not run: {type(exc).__name__}",
                                seconds=round(time.monotonic() - t0, 3))
        text = pr.text or ""
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in text
        summary = "" if ok else summarize(text, Path(project))
        if pr.timed_out:
            summary = f"timed out after {timeout_s:.0f}s\n{summary}".strip()
        elif pr.error:
            summary = f"{_scrub(pr.error)}\n{summary}".strip()
        return BuildOutcome(ok=ok, exit_code=pr.exit_code, log=log_str, summary=summary,
                            seconds=round(float(pr.duration_s or 0.0), 3), timed_out=bool(pr.timed_out))

    # ------------------------------------------------------------------ resolve
    def resolve(self, project: Path, work: Path) -> ResolvedModel:
        """모듈마다 Maven 이 실제로 고른 의존성(``dependency:tree`` 를 루트에서 한 번, 모든 모듈이 한 파일에 이어 쓴다)과
        pom 에 선언된 플러그인. 트리가 나오지 않은 모듈이 있으면 ``complete=False`` + 그 모듈 이름(조용히 짧아지지 않는다).

        Resolved dependencies per module (one root `dependency:tree` run appending every module's tree) + declared
        plugins. A module without a tree makes the model incomplete (named in `notes`)."""
        project, work = Path(project), Path(work)
        work.mkdir(parents=True, exist_ok=True)
        model = ResolvedModel(complete=True)
        pp = ProjectPoms(project)
        if not pp.complete:
            model.complete = False
            model.notes += pp.notes
        mods = [pp.poms[d] for d in pp.reactor if pp.poms[d].ok]
        if not mods:
            model.complete = False
            model.notes.append("no readable root pom.xml")
            return model
        out_file = (work / "deptree.txt").resolve()
        roots: list[TreeNode] = []
        cmd0, problem = self._command([])
        if cmd0 is None:
            model.complete = False
            model.notes.append(f"dependency:tree not run: {problem}")
        else:
            self._check_project_mvn(project)
            ok = False
            pkg = ["package", *[x for x in PACKAGE_SKIPS if x != TEST_SKIP]]
            for attempt, extra in enumerate(([], pkg)):
                out_file.unlink(missing_ok=True)
                cmd = [*cmd0[:-1], *extra, f"{DEPENDENCY_PLUGIN}:tree", f"-DoutputFile={out_file}",
                       "-DappendOutput=true", TEST_SKIP]
                try:
                    pr = self._run(cmd, project, work / f"deptree-{attempt + 1}.log", RESOLVE_TIMEOUT_S)
                except Exception as exc:        # noqa: BLE001
                    log.warning("dependency:tree could not run: %s", type(exc).__name__)
                    break
                if pr.exit_code == 0 and out_file.is_file():
                    ok = True
                    break
                if len(mods) == 1:              # 형제 모듈이 없으면 package 를 곁들여도 달라질 것이 없다
                    break
            if ok:
                try:
                    roots = parse_tree(out_file.read_text(encoding="utf-8", errors="replace"))
                except OSError as exc:
                    model.notes.append(f"dependency:tree output unreadable: {type(exc).__name__}")
            else:
                model.notes.append("dependency:tree failed (see deptree-*.log)")
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
    def packaged(self, project: Path) -> list[PackagedLib]:
        """모듈들의 ``target/*.war`` / ``target/*.jar`` 안 ``WEB-INF/lib/*.jar`` / ``BOOT-INF/lib/*.jar``. g:a·버전은 안쪽
        jar 의 ``META-INF/maven/**/pom.properties`` 에서만(파일 이름으로 추측하지 않는다). 안쪽 jar 는 크기 한도 안에서
        메모리로 읽는다.

        Jars inside the built war / executable jar of every module; coordinates from pom.properties only."""
        project = Path(project)
        pp = ProjectPoms(project)
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
    """모듈 트리 → 해석된 항목들(생략·충돌 노드 제외). ``path`` = 모듈과 항목 사이의 g:a:v 사슬."""
    out: list[ResolvedDep] = []

    def visit(node: TreeNode, chain: list[str]) -> None:
        for c in node.children:
            if c.omitted:
                continue                         # 다른 경로로 들어온 같은 jar — 해석된 항목이 아니다
            out.append(ResolvedDep(group_id=c.gav.group_id, artifact_id=c.gav.artifact_id, version=c.gav.version or "",
                                   scope=c.scope or c.gav.scope or "compile", type=c.gav.type or "jar",
                                   classifier=c.gav.classifier or "", direct=not chain, path=list(chain)))
            visit(c, [*chain, _gav(c)])

    visit(root, [])
    return out


def _declared_plugins(pp: ProjectPoms, pf: Any) -> list[ResolvedDep]:
    """pom 에 선언된 빌드 플러그인(``<plugins>`` / ``<pluginManagement>``, 프로필 포함)과 그 ``<dependencies>``.
    버전은 선언된 글자(프로젝트 속성으로 풀 수 있으면 푼 값) — 해석 결과가 아니다."""
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
    """안쪽 jar 의 pom.properties → (g:a, 버전). 여러 개(shade)면 파일 이름이 ``<artifactId>-<version>`` 로 시작하는 것,
    그래도 못 고르면 빈 값."""
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
