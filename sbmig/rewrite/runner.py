"""``rewrite-maven-plugin``으로 OpenRewrite 레시피를 적용하는 모듈. 빌드는 하지 않는다.

``OpenRewriteRunner`` implements ``RewriteRunnerLike``; the orchestrator calls it in the plan's
``transform.rewrite`` step and the changes become the ``rewrite: …`` commit. Recipe ids are kept in
``knowledge/recipes/catalog.yml``; the ids passed in are used as they are. Retries: once with
``oom_retry_jvm_args`` when the heap runs out, up to ``download_retry`` times after a wait when a download
fails, once with ``-U`` for a cached resolution failure, and once with ``runNoFork`` when the forked
``compile`` still fails (a project that does not compile). A failed conversion returns a RewriteResult and
raises nothing, so the run goes on to the build.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from sbmig.build.log_parser import parse_maven_log
from sbmig.build.mvn import base_command, mvn_env, run_logged, with_project_mvn
from sbmig.build.pomless import memo_for, run_logged_with_stubs
from sbmig.build.runner import negative_cache_hits
from sbmig.core.config import Config
from sbmig.core.interfaces import KnowledgeBaseLike
from sbmig.core.models import RecipeRef, RewriteResult
from sbmig.core.workspace import GitError, Workspace

from .compose import COMPOSITE_RECIPE_ID, compose_rewrite_yaml, recipe_documents, recipe_list_ids
from .settings_translate import PreparedSettings, define_names, openrewrite_settings
from .versions import RecipeArtifactError, resolve_recipe_artifact_versions

log = logging.getLogger("sbmig.rewrite")

PLUGIN_GA = "org.openrewrite.maven:rewrite-maven-plugin"
MIGRATE_JAVA = "org.openrewrite.recipe:rewrite-migrate-java"
SPRING = "org.openrewrite.recipe:rewrite-spring"

# 레시피 id 접두어별로 그 레시피가 들어 있는 아티팩트. 가장 긴 접두어가 우선이다
# org.openrewrite.java.*, org.openrewrite.maven.* 같은 핵심 레시피는 플러그인에 들어 있다. 아티팩트가 필요 없다
RECIPE_PREFIX_ARTIFACTS: dict[str, str | None] = {
    "org.openrewrite.java.migrate.": MIGRATE_JAVA,
    "org.openrewrite.java.spring.": SPRING,
    "org.openrewrite.java.springdoc.": SPRING,
    "org.openrewrite.java.testing.": "org.openrewrite.recipe:rewrite-testing-frameworks",
    "org.openrewrite.staticanalysis.": "org.openrewrite.recipe:rewrite-static-analysis",
    "org.openrewrite.java.dependencies.": "org.openrewrite.recipe:rewrite-java-dependencies",
    "org.openrewrite.java.logging.": "org.openrewrite.recipe:rewrite-logging-frameworks",
    "org.openrewrite.java.jackson.": "org.openrewrite.recipe:rewrite-jackson",
    "com.fasterxml.jackson.": "org.openrewrite.recipe:rewrite-jackson",
    "org.openrewrite.hibernate.": "org.openrewrite.recipe:rewrite-hibernate",
    "org.openrewrite.java.apache.": "org.openrewrite.recipe:rewrite-apache",
    "org.openrewrite.apache.": "org.openrewrite.recipe:rewrite-apache",
    "org.openrewrite.java.micronaut.": "org.openrewrite.recipe:rewrite-micronaut",
    "org.openrewrite.quarkus.": "org.openrewrite.recipe:rewrite-quarkus",
    "org.openrewrite.java.joda.": "org.openrewrite.recipe:rewrite-joda",
    "org.openrewrite.okhttp.": "org.openrewrite.recipe:rewrite-okhttp",
    "org.openrewrite.java.jspecify.": MIGRATE_JAVA,
}

_CHANGED_RE = re.compile(r"^\[WARNING\] (?:Changes have been made to|Generated new file|Deleted file) (?P<p>\S+?)"
                         r"(?: by:|:)?\s*$")
_MOVED_RE = re.compile(r"^\[WARNING\] File has been moved from (?P<a>\S+) to (?P<b>\S+) by:")
_DRY_RE = re.compile(r"^\[WARNING\] These recipes would (?:make changes to|generate new file|delete file) "
                     r"(?P<p>\S+?):?\s*$")
_DRY_MOVED_RE = re.compile(r"^\[WARNING\] These recipes would move file from (?P<a>\S+) to (?P<b>\S+?):?\s*$")
_NOT_FOUND_RE = re.compile(r"(Recipe\(s\) not found: .*|Could not find recipe.*|"
                           r"Recipes not found:.*|Did you mean: .*)")
_VALIDATION_RE = re.compile(r"Recipe validation error in (?P<recipe>\S+) for property (?P<prop>.+?)"
                            r"(?: \(in file:[^)]*\))?: (?P<msg>.*)$")
# 플러그인이 파싱하지 못한 파일을 찾는다. 그 파일에는 레시피가 적용되지 않았으니 보고서에 남긴다
# rewrite-maven-plugin 6.46.1 prints "There were problems parsing <path>" per file (sometimes with text after
# the path) and/or the summary "There were problems parsing some source files[, run with --errors …]".
_PARSE_PROBLEM_RE = re.compile(r"There were problems parsing (?P<rest>.*)$")
PARSE_PROBLEMS_UNKNOWN = "(unknown files — see the rewrite log)"
_DOES_NOT_EXIST_RE = re.compile(r"recipe '(?P<id>[^']+)' does not exist")
# 플러그인이 pom을 받지 못했을 때의 출력이다. 예외 줄과 pom 안 주석 두 형태로 나온다
_POM_FAIL_RE = re.compile(r"Unable to download POM: (?P<gav>\S+?)\.? Tried repositories:[ \t]*$")
_TRIED_RE = re.compile(r"^(?:\[ERROR\] ?)?(?P<url>[A-Za-z][A-Za-z0-9+.-]*://\S+?): (?P<answer>.+?)(?:\)~~>-->.*)?\s*$")
_ACCESS_RE = re.compile(r"Failed to access maven repository (?P<url>\S+) due to: (?P<reason>.*?)\s*$")
# OpenRewrite가 성공한 실행에도 남기는 경고다. 재시도 판단에서 뺀다
_OR_WARNINGS_RE = re.compile(r"^\[WARNING\] Failed to download .*(?:\n  - .*)*|^\[WARNING\] Failed to access maven "
                             r"repository .*$", re.MULTILINE)
# 오류 문장 뒤에 붙인 pom 부분의 시작이다. 재시도 판단에서 이 부분을 뺀다
_POM_PART_RE = re.compile(r"(?:not reachable: |Unable to download POM: )")
# 저장소에 연결하지 못한 이유 중 기다리면 풀릴 수 있는 것. 호스트 이름만 남은 이유는 DNS 실패다
_ACCESS_PROBLEM_RE = re.compile(r"timed out|timeout|reset|UnknownHost|unknown host|nodename nor servname|"
                                r"Name or service not known|name resolution|^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$",
                                re.IGNORECASE)
_HTTP_4XX_RE = re.compile(r"HTTP 4\d\d")
# 플러그인이 pom을 받지 못했을 때 남기는 문장. pom 부분이 같은 내용을 더 자세히 말하므로 뺀다
_PARSE_FAIL_RE = re.compile(r"plugin_execution: (?:Failed to parse or resolve the Maven POM file or one of its "
                            r"dependencies; We can not reliably continue without this information\.\s*)+(?:; |$)")
# 연결 실패 이유를 짧은 말로 줄인다
_SHORT_REASONS = ((re.compile(r"refused", re.I), "refused"), (re.compile(r"timed out|timeout", re.I), "timed out"),
                  (re.compile(r"reset", re.I), "reset"),
                  (re.compile(r"UnknownHost|unknown host|nodename|name resolution|not known|^[A-Za-z0-9-]+"
                              r"(?:\.[A-Za-z0-9-]+)+$", re.I), "unknown host"))
# 실행 폴더마다 마지막으로 기록한 settings 번역 결과. 같은 결과를 두 번 기록하지 않는다
_SETTINGS_RECORDS: dict[str, dict] = {}
MAX_POMS_NAMED = 3


def pom_download_problems(text: str) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]]]:
    """플러그인 출력에서 받지 못한 pom과 연결하지 못한 저장소를 찾는다.
    (좌표, [(물어본 저장소 url, 응답)]) 목록과 (저장소 url, 이유) 목록을 반환한다. 없으면 빈 목록을 반환한다.

    Both forms of `Unable to download POM: G:A:V. Tried repositories:` (the `MavenDownloadingException` line and the
    `<!--~~(…)~~>-->` marker inside the printed pom) followed by one `<url>: <answer>` line per tried repository
    (`[ERROR] ` prefix optional); a coordinate seen twice is merged. `[WARNING] Failed to access maven repository
    <url> due to: <reason>` names a repository OpenRewrite could not reach (it is never in "Tried repositories").
    Urls are returned as printed: callers strip userinfo."""
    poms: dict[str, list[tuple[str, str]]] = {}
    unreachable: dict[str, str] = {}
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        m = _POM_FAIL_RE.search(line.rstrip())
        if m:
            tried = poms.setdefault(m.group("gav"), [])
            for nxt in lines[i + 1:]:
                t = _TRIED_RE.match(nxt)
                if not t:
                    break
                entry = (t.group("url"), t.group("answer").strip())
                if entry not in tried:
                    tried.append(entry)
                if ")~~>" in nxt:
                    break
            continue
        a = _ACCESS_RE.search(line)
        # 0.0.0.0은 차단 미러의 주소라 연결하지 못한 저장소가 아니다
        if a and a.group("url") not in unreachable and "://0.0.0.0" not in a.group("url"):
            unreachable[a.group("url")] = a.group("reason")
    return list(poms.items()), list(unreachable.items())


def _host_path(url: str) -> str:
    from sbmig.build.env_errors import strip_userinfo
    return re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", strip_userinfo(url))


def pom_failure_text(text: str) -> str:
    """받지 못한 pom을 오류 문장 뒤에 붙일 한 줄로 만든다. pom 실패가 없으면 빈 문자열을 반환한다.
    연결하지 못한 저장소, pom 좌표, 물어본 저장소 순서로 쓴다.

    Order (rule 5: the `sbmig.log` line is cut at 300 characters, so what an operator needs first comes first):
    `not reachable: <host and path> (<reason>), …` (the `Failed to access maven repository` warnings, if any, the
    reason shortened to refused / timed out / reset / unknown host when it is one of those),
    then `; Unable to download POM: <g:a:v>, …` (at most `MAX_POMS_NAMED`, then `and N more`), then `; tried
    <host and path>: <answer>, …` (every distinct tried repository once, its distinct answers joined by `/`).
    Userinfo is stripped from every url. Contract "v2.1z-ci" rule 5."""
    poms, unreachable = pom_download_problems(text)
    if not poms:
        return ""
    parts = []
    if unreachable:
        parts.append("not reachable: " + ", ".join(f"{_host_path(u)} ({_short_reason(r)})" for u, r in unreachable))
    coords = ", ".join(gav for gav, _ in poms[:MAX_POMS_NAMED])
    if len(poms) > MAX_POMS_NAMED:
        coords += f" and {len(poms) - MAX_POMS_NAMED} more"
    parts.append("Unable to download POM: " + coords)
    tried: dict[str, list[str]] = {}
    for _gav, entries in poms:
        for url, answer in entries:
            answers = tried.setdefault(_host_path(url), [])
            if answer[:60] not in answers:
                answers.append(answer[:60])
    parts.append("tried " + (", ".join(f"{h}: {'/'.join(a)}" for h, a in tried.items()) or "no repository"))
    return "; ".join(parts)


def _short_reason(reason: str) -> str:
    r = reason.strip()
    return next((word for rx, word in _SHORT_REASONS if rx.search(r)), r[:30])


def _split(value: str) -> list[str]:
    import shlex
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _is_windows_abs(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:/", p))


def _is_abs(p: str) -> bool:
    return p.startswith("/") or _is_windows_abs(p)


def recipe_artifact_for(recipe_id: str) -> str | None:
    """레시피 id를 담은 레시피 아티팩트(g:a)를 가장 긴 접두어 기준으로 반환한다.
    플러그인 내장 레시피면 None을 반환한다."""
    best: str | None = None
    best_len = -1
    for prefix, art in RECIPE_PREFIX_ARTIFACTS.items():
        if recipe_id.startswith(prefix) and len(prefix) > best_len:
            best, best_len = art, len(prefix)
    return best


def catalog_recipe_files(recipes: Sequence[RecipeRef], cfg: Config) -> list[Path]:
    """카탈로그에서 ``file``이 지정된 레시피의 파일 경로를 반환한다. 요청 순서대로, 중복 없이 반환한다."""
    from sbmig.plan.catalog import default_catalog, recipe_file_path

    cat = default_catalog(cfg)
    out: list[Path] = []
    for r in recipes:
        path = recipe_file_path(cat.get(r.id), cfg)
        if path is not None and path not in out:
            out.append(path)
    return out


class OpenRewriteRunner:
    """OpenRewrite 플러그인을 실행하고 결과를 RewriteResult로 반환하는 클래스.
    실행이 실패해도 예외를 던지지 않는다. 추가 레시피 파일이 없거나 YAML이 깨졌을 때만 던진다.
    ``kb``는 받기만 하고 쓰지 않는다. 오케스트레이터의 생성자 호출과 맞추려는 것이다.

    The only raises come from `compose` (FileNotFoundError / ValueError for an extra recipe file)."""

    def __init__(self, ws: Workspace, cfg: Config, kb: KnowledgeBaseLike | None = None, *,
                 timeout_s: float | None = None):
        self.ws = ws
        self.cfg = cfg
        self.kb = kb
        self.timeout_s = timeout_s if timeout_s is not None else float(cfg.openrewrite.timeout_minutes) * 60.0
        # 합성한 rewrite-N.yml과 레시피 버전 캐시는 rewrite 로그와 같은 폴더에 둔다
        self.work_dir = getattr(ws, "logs_dir", None) or ws.run_dir / "logs"
        self._sleep = time.sleep                 # 다운로드 재시도 대기. 테스트가 기록용 함수로 바꾼다

    # ------------------------------------------------------------------ 레시피 합성
    def compose(self, recipes: Sequence[RecipeRef], extra_recipe_files: Iterable[Path] = ()) -> tuple[Path, str]:
        """이번 실행의 rewrite.yml을 새 번호로 ``logs/rewrite-<n>.yml``에 저장하고 (경로, 텍스트)를 반환한다.
        앞 실행의 파일을 덮어쓰지 않는다. 그래서 재시도마다 무엇을 실행했는지 남는다.
        카탈로그 항목에 ``file``이 있는 레시피는 그 파일도 함께 넣는다."""
        files = list(extra_recipe_files) + catalog_recipe_files(recipes, self.cfg)
        text = compose_rewrite_yaml(recipes, extra_files=files, target=self.cfg.target)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(self.work_dir.glob("rewrite-*.yml"))) + 1
        path = self.work_dir / f"rewrite-{n}.yml"
        while path.exists():
            n += 1
            path = self.work_dir / f"rewrite-{n}.yml"
        path.write_text(text, encoding="utf-8")
        return path, text

    def required_artifacts(self, recipes: Sequence[RecipeRef], composed_text: str) -> list[str]:
        """필요한 레시피 아티팩트 g:a 목록을 구한다.
        ``RecipeRef.artifact``에 지정된 것과 레시피 id에서 추론한 것을 담는다.
        합성 파일에 선언된 레시피면 rewrite-migrate-java, rewrite-spring, 그 recipeList가 가리키는 아티팩트도 담는다."""
        docs = recipe_documents(composed_text)
        needed: list[str] = []

        def add(a: str | None) -> None:
            if a and a not in needed:
                needed.append(a)

        def walk(rid: str, seen: set[str]) -> None:
            if rid in seen:
                return
            seen.add(rid)
            if rid in docs:
                for child in recipe_list_ids(docs[rid]):
                    walk(child, seen)
            else:
                add(recipe_artifact_for(rid))

        for r in recipes:
            add(r.artifact)
            if r.id in docs:
                add(MIGRATE_JAVA)
                add(SPRING)
                walk(r.id, set())
            elif not r.artifact:
                add(recipe_artifact_for(r.id))
        return needed

    # ------------------------------------------------------------------ 명령
    def env(self, jvm_args: Sequence[str] | None = None) -> dict[str, str]:
        """mvn 환경 변수를 만들고 MAVEN_OPTS에 ``jvm_args``를 더한다.
        기본값은 ``openrewrite.jvm_args``(``-Xmx4g``)다. OOM 재시도는 ``oom_retry_jvm_args``를 쓴다."""
        env = mvn_env(self.cfg)
        args = list(self.cfg.openrewrite.jvm_args if jvm_args is None else jvm_args)
        opts = " ".join(x for x in [env.get("MAVEN_OPTS", "").strip(), *args] if x)
        if opts:
            env["MAVEN_OPTS"] = opts
        return env

    def command(self, config_path: Path, coordinates: Sequence[str], *, dry_run: bool = False,
                fork: bool | None = None, extra_args: Iterable[str] = ()) -> list[str]:
        """플러그인 명령을 만든다.
        ``fork=False``면 ``runNoFork``, ``fork=True``면 ``run``, None이면 설정 ``plugin_goal``을 쓴다.
        ``runNoFork``는 compile 생명주기를 fork하지 않는다. 그래서 컴파일되지 않는 프로젝트에서도 동작한다.
        활성 레시피는 합성 레시피 하나다. 잘못된 레시피가 있으면 BUILD FAILURE로 끝난다.
        JSP, HTML은 XML 파서 경고만 낸다. ``openrewrite.exclusions``로 뺀다."""
        if dry_run:
            goal = "dryRun" if fork is not False else "dryRunNoFork"
        elif fork is None:
            goal = self.cfg.openrewrite.plugin_goal
        else:
            goal = "run" if fork else "runNoFork"
        cmd = base_command(self.cfg) + list(extra_args) + [
            f"{PLUGIN_GA}:{self.cfg.openrewrite.plugin_version}:{goal}",
            f"-Drewrite.activeRecipes={COMPOSITE_RECIPE_ID}",
            f"-Drewrite.configLocation={Path(config_path).resolve()}",
        ]
        if coordinates:
            cmd.append(f"-Drewrite.recipeArtifactCoordinates={','.join(coordinates)}")
        cmd.append("-Drewrite.exportDatatables=false")
        cmd.append("-Drewrite.failOnInvalidActiveRecipes=true")    # 모르는 레시피나 잘못된 레시피는 BUILD FAILURE
        excl = [x for x in (getattr(self.cfg.openrewrite, "exclusions", None) or []) if x]
        if excl:
            cmd.append(f"-Drewrite.exclusions={','.join(excl)}")
        return cmd

    # ------------------------------------------------------------------ 결과 읽기
    @staticmethod
    def parse_changed_files(text: str, *, dry_run: bool = False) -> list[str]:
        """플러그인 로그에서 바뀐 파일 목록을 만든다.
        'Changes have been made to …', 'Generated new file', 'Deleted file', 'File has been moved' 줄을 읽는다.
        dryRun이면 'These recipes would …' 줄을 읽는다. 역슬래시는 슬래시로 바꾸고 중복은 없앤다."""
        out: list[str] = []
        for line in text.splitlines():
            line = line.rstrip()
            if dry_run:
                m = _DRY_RE.match(line)
                if m:
                    out.append(m.group("p"))
                    continue
                m = _DRY_MOVED_RE.match(line)
            else:
                m = _CHANGED_RE.match(line)
                if m:
                    out.append(m.group("p"))
                    continue
                m = _MOVED_RE.match(line)
            if m:
                out += [m.group("a"), m.group("b")]
        return list(dict.fromkeys(p.replace("\\", "/") for p in out))

    @staticmethod
    def parse_problems(text: str, *, root: Path | str | None = None) -> list[str]:
        """플러그인이 파싱하지 못했다고 보고한 파일('There were problems parsing <path>')을 반환한다.
        로그 순서대로, 중복 없이 반환한다. 역슬래시는 슬래시로, ``root`` 아래 절대 경로는 상대 경로로 바꾼다.
        요약 줄만 있으면 ``[PARSE_PROBLEMS_UNKNOWN]``을, 아무것도 없으면 빈 리스트를 반환한다."""
        base = (str(root).replace("\\", "/").rstrip("/") + "/") if root else None
        files: list[str] = []
        summary = False
        for line in text.splitlines():
            m = _PARSE_PROBLEM_RE.search(line.rstrip())
            if not m:
                continue
            rest = m.group("rest").strip()
            if not rest or rest.startswith("some source files"):
                summary = True
                continue
            p = rest.split()[0].rstrip(",;:").strip("'\"`").replace("\\", "/")
            if not p:
                continue
            if base:
                win = _is_windows_abs(p)
                if (p.lower().startswith(base.lower()) if win else p.startswith(base)):
                    p = p[len(base):]
            files.append(p)
        files = list(dict.fromkeys(files))
        if not files and summary:
            return [PARSE_PROBLEMS_UNKNOWN]
        return files

    @staticmethod
    def parse_recipe_attribution(text: str) -> dict[str, list[str]]:
        """레시피 id마다 그 레시피가 바꾼 파일을 모은다.
        'Changes have been made to <f> by:' 아래 들여쓴 레시피 줄을 읽는다.
        합성 레시피와 그 안의 기본 레시피를 모두 기록한다."""
        out: dict[str, list[str]] = {}
        current: str | None = None
        for raw in text.splitlines():
            line = raw.rstrip()
            m = _CHANGED_RE.match(line) or _DRY_RE.match(line)
            if m and line.endswith("by:") or (m and ":" in line and _DRY_RE.match(line)):
                current = m.group("p").replace("\\", "/")
                continue
            if current is None:
                continue
            body = line[len("[WARNING]"):] if line.startswith("[WARNING]") else line
            if body.startswith("    ") and body.strip():
                rid = body.strip().split(":")[0].split(" ")[0]
                if rid and not rid.startswith("{"):
                    out.setdefault(rid, [])
                    if current not in out[rid]:
                        out[rid].append(current)
            else:
                current = None
        return out

    @staticmethod
    def validation_problems(text: str) -> tuple[list[str], list[str]]:
        """플러그인의 레시피 검증 오류 줄을 읽는다. (모르는 레시피 id 목록, 그 밖의 검증 메시지)를 반환한다."""
        unknown: list[str] = []
        other: list[str] = []
        for line in text.splitlines():
            for m in _DOES_NOT_EXIST_RE.finditer(line):
                if m.group("id") not in unknown:
                    unknown.append(m.group("id"))
            m = _VALIDATION_RE.search(line)
            if m and not _DOES_NOT_EXIST_RE.search(line) and "uninitialized recipes" not in m.group("msg"):
                msg = f"{m.group('recipe')} {m.group('prop')}: {m.group('msg').strip()}"
                if msg not in other:
                    other.append(msg)
            m = _NOT_FOUND_RE.search(line)
            if m:
                msg = re.sub(r"\s*->\s*\[Help \d+\]\s*$", "", m.group(1)).strip()
                if msg not in other:
                    other.append(msg)
        return unknown, other

    def _error_text(self, text: str) -> str:
        """실패 이유를 한 줄로 만든다.
        레시피 검증 문제가 있으면 그것을 쓴다. 없으면 Maven 로그 파서가 찾은 오류를 앞에서 10개까지 쓴다.
        플러그인이 pom을 받지 못했으면 연결하지 못한 저장소, pom 좌표, 물어본 저장소를 맨 앞에 쓴다.

        With a pom failure: `pom_failure_text`, then ` | ` and the parser's other messages, without the
        plugin's own sentence (`plugin_execution: Failed to parse or resolve the Maven POM file …`, printed
        twice), because `failure.clean_reason` cuts the `transform.rewrite: …` line at 300 characters."""
        unknown, other = self.validation_problems(text)
        parts = []
        if unknown:
            parts.append("unknown recipe(s): " + ", ".join(unknown))
        if other:
            parts.append("recipe validation: " + "; ".join(other[:8]))
        if parts:
            return " | ".join(parts)
        from sbmig.build.log_parser import reactor_artifact_ids_from_poms
        errors = parse_maven_log(text, self.ws.root, known_modules=reactor_artifact_ids_from_poms(self.ws.root))
        msgs = list(dict.fromkeys(f"{e.kind.value}: {e.message}" for e in errors))
        head = "; ".join(msgs[:10]) if msgs else "rewrite run failed (see log)"
        poms = pom_failure_text(text)
        if not poms:
            return head
        # 한 줄 요약은 300자에서 잘린다. pom 부분을 앞에 두고, 같은 말을 하는 플러그인 문장은 뺀다
        head = _PARSE_FAIL_RE.sub("", head).strip().rstrip(";")
        return poms + (f" | {head}" if head else "")

    def _head(self) -> str | None:
        try:
            return self.ws.head()
        except (GitError, OSError):
            return None

    # ------------------------------------------------------------------ 실행
    def run(self, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path] = (), module: str | None = None,
            dry_run: bool = False, label: str = "", fork: bool | None = None,
            extra_args: Iterable[str] = ()) -> RewriteResult:
        """재시도를 포함해 변환을 한 번 실행한다.
        ``extra_args``(예: ``-pl order-service -am``)로 reactor 루트에서 대상 모듈 하나만 실행한다.
        그러면 형제 모듈을 로컬 저장소가 아니라 reactor에서 찾는다.
        HTTP 차단 미러에 막혔으면 재시도하지 않는다. 그 밖에는 아래 순서로 다시 실행한다.
        - OOM이면 큰 힙으로 한 번 다시 실행한다. 아니면 다운로드 실패일 때 기다렸다가 다시 실행한다
        - 캐시된 해결 실패면 -U를 붙여 한 번 다시 실행한다
        - 그래도 fork된 compile에서 실패하면 runNoFork로 한 번 다시 실행한다"""
        self._extra_args = list(extra_args)      # 이 호출 안의 모든 _run_once가 쓴다(OOM, runNoFork 재시도 포함)
        res = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                             label=label, fork=fork)
        jvm_args: list[str] | None = None
        # Maven의 HTTP 차단 미러에 막혔다. 재시도로는 풀리지 않는다. 바로 반환하고, 오케스트레이터가 중단한다
        if not res.ok and self._http_blocked(res):
            log.warning("rewrite: Maven's HTTP repository blocker refused a repository — no retry (fix the Maven settings)")
            return res
        # 레시피 실행이 힙 부족으로 실패하면 oom_retry_jvm_args로 한 번 더 실행한다
        if not res.ok and self._is_oom(res):
            jvm_args = list(self.cfg.openrewrite.oom_retry_jvm_args)
            res = self._oom_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                  dry_run=dry_run, label=label, fork=fork, jvm_args=jvm_args)
        # 전송 실패(프록시 429 / 5xx, 연결 끊김, 타임아웃)면 기다렸다가 같은 실행을 다시 한다
        elif not res.ok and self.cfg.openrewrite.download_retry > 0 and self._download_failure(res):
            res = self._download_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                       dry_run=dry_run, label=label, fork=fork)
        # 모델 단계의 해결 실패는 오래된 `*.lastUpdated` 표식 때문일 수 있다. -U로 한 번만 다시 실행한다
        # 여기서 막히면 레시피가 실행되지 않는다
        if not res.ok and not self._download_failure(res) and self._may_force_update() and self._force_update_hits(res):
            res = self._force_update_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                           dry_run=dry_run, label=label, fork=fork, jvm_args=jvm_args)
        # run은 레시피 전에 compile을 fork한다. 컴파일되지 않는 프로젝트면 레시피까지 가지 못한다
        # runNoFork는 해석된 의존성 classpath로 소스를 파싱한다. 그래서 runNoFork로 다시 실행한다
        if not res.ok and res.forked and fork is not False and res.error and "compile" in res.error.lower():
            log.info("rewrite: forked run failed in compile → retrying with runNoFork")
            retry = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                                   label=(label or "run") + "-nofork", fork=False, jvm_args=jvm_args)
            retry.error = retry.error if not retry.ok else None
            if not retry.ok and jvm_args is not None and "oom-retry" not in (retry.error or ""):
                retry.error = f"oom-retry: {retry.error}"
            elif not retry.ok and "download-retry" in (res.error or "") \
                    and "download-retry" not in (retry.error or ""):
                retry.error = f"download-retry: {retry.error}"
            return retry
        return res

    # ------------------------------------------------------------------ 힙 부족
    _OOM_RE = re.compile(r"OutOfMemoryError|GC overhead limit exceeded")

    @staticmethod
    def _failure_text(res: RewriteResult) -> str:
        text = res.error or ""
        if res.log_path:
            try:
                text += "\n" + Path(res.log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return text

    def _http_blocked(self, res: RewriteResult) -> bool:
        """실패 텍스트에 Maven의 HTTP 차단 미러가 보이는지 확인한다(``sbmig.build.env_errors.http_blocker_hit``)."""
        from sbmig.build.env_errors import http_blocker_hit
        return http_blocker_hit(self._failure_text(res)) is not None

    def _is_oom(self, res: RewriteResult) -> bool:
        return bool(self._OOM_RE.search(self._failure_text(res)))

    # ------------------------------------------------------------------ 다운로드 실패
    # 전송 문제만 재시도한다. 실제로 없는 아티팩트는 기다려도 생기지 않는다
    # 없는 아티팩트는 오케스트레이터의 부모 확인과 빌드 루프의 해결 오류 분류가 맡는다
    # OpenRewrite의 "Failed to download" 경고는 재시도 조건이 아니다. 성공한 실행에도 나온다
    _DOWNLOAD_RE = re.compile(
        r"Could not transfer artifact|Transfer failed|status code: (?:429|5\d\d)|Too Many Requests|"
        r"Connection reset|Read timed out|Connection timed out|SocketTimeoutException|UnknownHostException|"
        r"Service Unavailable|Bad Gateway|Gateway Time-?out", re.IGNORECASE)

    def _may_force_update(self) -> bool:
        """-U 재시도를 할 수 있는지 반환한다.
        ``build.retry_negative_cache``가 켜져 있고 명령에 -U / -o가 없어야 한다."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        args = list(getattr(self, "_extra_args", ()) or []) + list(getattr(self.cfg.build, "extra_args", ()) or [])
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in args) and not self.cfg.build.offline

    def _force_update_hits(self, res: RewriteResult) -> list[str]:
        """캐시된 해결 실패나 모델 단계 해결 실패가 난 좌표를 반환한다(``negative_cache_hits``).
        없으면 빈 리스트를 반환한다."""
        return negative_cache_hits(self._failure_text(res))

    def _force_update_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                            module: str | None, dry_run: bool, label: str, fork: bool | None,
                            jvm_args: Sequence[str] | None = None) -> RewriteResult:
        """``-U``를 붙여 한 번 더 실행한다. 이 호출 안의 이후 재시도도 -U를 붙인다.
        끝내 실패하면 오류 앞에 ``force-update-retry: ``를 붙인다.
        저널에 ``rewrite_force_update_retry`` / ``rewrite_force_update_retry_result``를 기록한다."""
        hits = self._force_update_hits(first)
        log.warning("rewrite: cached / model-level resolution failure (%s) → retrying once with -U", ", ".join(hits[:3]))
        self.ws.journal({"event": "rewrite_force_update_retry", "recipes": first.recipes, "module": module,
                         "artifacts": hits, "first_log": first.log_path})
        self._extra_args = [*getattr(self, "_extra_args", ()), "-U"]
        res = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                             label=(label or ("dry" if dry_run else "run")) + "-U", fork=fork, jvm_args=jvm_args)
        if not res.ok:
            res.error = f"force-update-retry: {res.error or 'rewrite run failed'}"
        self.ws.journal({"event": "rewrite_force_update_retry_result", "ok": res.ok, "log": res.log_path, "error": res.error})
        return res

    def _download_failure(self, res: RewriteResult) -> str | None:
        """전송 문제로 실패했으면 그 문구를 반환한다. 아니면 None이다. 반환값은 저널의 ``reason``이 된다.
        경고 줄만으로는 재시도하지 않는다. pom을 받지 못한 실패는 저장소의 응답과 연결 실패 이유로만 판단한다.

        Rule 6 of "v2.1z-ci". With an `Unable to download POM` in the output it alone decides: a tried repository
        whose answer is not `HTTP 4xx` (429 counts as a problem; a bare host name is a DNS failure), or a
        `Failed to access maven repository` reason that is a timeout, a reset or an unknown host (the pom may be on
        that repository); all 404 and only refused connections: None. Otherwise `_DOWNLOAD_RE` on the error text
        (without the pom part `_error_text` appended) and the log without its `[WARNING]` lines (OpenRewrite's
        `Failed to download … Attempted URIs:` blocks included)."""
        text = self._failure_text(res)
        poms, unreachable = pom_download_problems(text)
        if poms:
            for _gav, tried in poms:
                for _url, answer in tried:
                    if not _HTTP_4XX_RE.fullmatch(answer) or answer == "HTTP 429":
                        return f"Unable to download POM ({answer[:40]})"
            for _url, reason in unreachable:
                if _ACCESS_PROBLEM_RE.search(reason.strip()):
                    return f"Unable to download POM (not reachable: {reason[:40]})"
            return None
        error = res.error or ""
        if _POM_PART_RE.match(error.removeprefix("download-retry: ").removeprefix("force-update-retry: ")):
            error = error.split(" | ", 1)[1] if " | " in error else ""
        rest = text[len(res.error or ""):] if text.startswith(res.error or "") else text
        rest = "\n".join(x for x in _OR_WARNINGS_RE.sub("", rest).splitlines() if not x.startswith("[WARNING]"))
        m = self._DOWNLOAD_RE.search(error + "\n" + rest)
        return m.group(0) if m else None

    def _download_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                        module: str | None, dry_run: bool, label: str, fork: bool | None) -> RewriteResult:
        """``download_retry_wait_s``만큼 기다렸다가 최대 ``download_retry``번 다시 실행한다.
        성공하거나 실패 이유가 더 이상 전송 문제가 아니면 멈춘다.
        끝내 실패하면 오류 앞에 ``download-retry: ``를 붙인다."""
        reason = self._download_failure(first) or ""
        wait_s = max(0, int(self.cfg.openrewrite.download_retry_wait_s))
        total = int(self.cfg.openrewrite.download_retry)
        base = label or ("dry" if dry_run else "run")
        res = first
        for attempt in range(1, total + 1):
            log.warning("rewrite: download failure (%s) → waiting %ss, retry %d/%d", reason, wait_s, attempt, total)
            self.ws.journal({"event": "rewrite_download_retry", "recipes": first.recipes, "module": module,
                             "attempt": attempt, "wait_s": wait_s, "first_log": first.log_path, "reason": reason})
            if wait_s:
                self._sleep(wait_s)
            res = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                                 label=f"{base}-dlretry" + (str(attempt) if attempt > 1 else ""), fork=fork)
            if res.ok:
                break
            again = self._download_failure(res)
            if not again:
                break           # 이제는 다른 이유로 실패한다. 더 기다려도 소용없다
            reason = again
        if not res.ok:
            res.error = f"download-retry: {res.error or 'rewrite run failed'}"
        self.ws.journal({"event": "rewrite_download_retry_result", "ok": res.ok, "log": res.log_path,
                         "error": res.error})
        return res

    def _oom_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                   module: str | None, dry_run: bool, label: str, fork: bool | None,
                   jvm_args: list[str]) -> RewriteResult:
        """힙 부족(OutOfMemoryError, GC overhead)으로 실패한 실행을 ``oom_retry_jvm_args``로 한 번 더 실행한다.
        실패하면 오류 앞에 ``oom-retry``를 붙인다."""
        log.warning("rewrite: OutOfMemoryError → retrying once with MAVEN_OPTS %s", " ".join(jvm_args))
        self.ws.journal({"event": "rewrite_oom_retry", "recipes": first.recipes, "module": module,
                         "first_log": first.log_path, "jvm_args": jvm_args})
        retry = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                               label=(label or ("dry" if dry_run else "run")) + "-oom", fork=fork, jvm_args=jvm_args)
        if not retry.ok:
            still = " (still out of memory)" if self._is_oom(retry) else ""
            retry.error = f"oom-retry{still}: {retry.error or 'rewrite run failed'}"
        self.ws.journal({"event": "rewrite_oom_retry_result", "ok": retry.ok, "log": retry.log_path,
                         "error": retry.error})
        return retry

    def _run_once(self, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path] = (), module: str | None = None,
                  dry_run: bool = False, label: str = "", fork: bool | None = None,
                  jvm_args: Sequence[str] | None = None) -> RewriteResult:
        """플러그인을 한 번 실행한다.
        rewrite.yml 생성 -> 레시피 아티팩트 버전 결정 -> 명령 실행(``run_logged``) 순서다.
        버전을 정하지 못하면 ok=False 결과를 반환한다.
        exit 0이고, 'BUILD SUCCESS'가 나오고, 레시피 검증 문제가 없어야 성공이다.
        바뀐 파일은 로그에 나온 것과 실행 전 HEAD 이후의 git 변경을 합친다. target/은 뺀다."""
        ids = [r.id for r in recipes]
        if not recipes:
            return RewriteResult(ok=True, recipes=[], dry_run=dry_run)
        config_path, text = self.compose(recipes, extra_recipe_files)
        log_path = self.ws.new_log_path(f"rewrite-{label or ('dry' if dry_run else 'run')}")
        env = self.env(jvm_args)
        cwd = self.ws.abs(module) if module else self.ws.root
        try:
            versions = resolve_recipe_artifact_versions(
                self.cfg, env, artifacts=self.required_artifacts(recipes, text),
                cache_path=self.work_dir / "rewrite-artifact-versions.json", work_dir=self.work_dir)
        except RecipeArtifactError as exc:
            log_path.write_text(f"[ERROR] {exc}\n", encoding="utf-8")
            res = RewriteResult(ok=False, recipes=ids, log_path=str(log_path), error=str(exc), dry_run=dry_run)
            self._journal(res, module)
            return res
        coords = [f"{ga}:{v}" for ga, v in versions.items()]
        cmd = self.command(config_path, coords, dry_run=dry_run, fork=fork, extra_args=getattr(self, "_extra_args", ()))
        # Maven은 프로젝트의 .mvn을 읽지 않는다. 그래서 maven.config의 -D, -P와 프로젝트 루트를 명령에 넣는다
        cmd = with_project_mvn(self.cfg, cmd, cwd, top=self.ws.root)
        goal = cmd[[i for i, c in enumerate(cmd) if c.startswith(PLUGIN_GA)][0]].rsplit(":", 1)[-1]
        forked = goal in ("run", "dryRun")
        head_before = None if dry_run else self._head()
        log.info("rewrite %s (%s)", ", ".join(ids), goal)
        # 번역한 settings 복사본을 이 호출 동안만 쓴다. OpenRewrite가 Maven과 같은 미러를 고르게 하려는 것이다
        # Maven은 MAVEN_ARGS를 명령 인자처럼 읽는다. 그래서 그 -D도 pom 값보다 우선한다
        defines = define_names([*cmd, *_split(env.get("MAVEN_OPTS", "")), *_split(env.get("MAVEN_ARGS", ""))])
        with openrewrite_settings(self.cfg, self.ws.root, defines) as prepared:
            cmd = self._with_settings_copy(cmd, prepared)
            # 저장소에 pom이 없는 라이브러리 때문에 실패하면 최소 pom을 만들어 두고 같은 명령을 다시 실행한다
            pr, log_path = run_logged_with_stubs(self.cfg, cmd, cwd=cwd, env=env, log_path=log_path,
                                                 timeout_s=self.timeout_s, journal=self.ws.journal,
                                                 memo=memo_for(self.ws), project_root=self.ws.root, runner=run_logged)
        out = pr.text
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in out
        # 이전 플러그인은 잘못된 레시피에도 성공으로 끝난다. 로그에 검증 문제가 있으면 실패로 본다
        if ok and any(self.validation_problems(out)):
            ok = False
        prefix = (module.rstrip("/") + "/") if module else ""
        changed = [prefix + p for p in self.parse_changed_files(out, dry_run=dry_run)]
        if head_before is not None:
            try:
                changed += self.ws.changed_files_since(head_before)
            except GitError:
                pass
        changed = sorted({c for c in changed if c and not c.startswith("target/") and "/target/" not in c})
        parse_problems = []
        for p in self.parse_problems(out, root=cwd):
            if _is_abs(p):          # 모듈 폴더 밖의 경로다. 프로젝트 루트 아래면 루트 기준 상대 경로로 바꾼다
                p = self.parse_problems(f"There were problems parsing {p}", root=self.ws.root)[0]
            elif p != PARSE_PROBLEMS_UNKNOWN:
                p = prefix + p
            parse_problems.append(p)
        parse_problems = list(dict.fromkeys(parse_problems))
        if parse_problems:
            log.warning("rewrite could not parse %d file(s): %s", len(parse_problems), ", ".join(parse_problems[:5]))
        error = None
        if not ok:
            if pr.timed_out:
                error = f"rewrite timed out after {self.timeout_s / 60:.1f} minutes"
            elif pr.error:
                error = pr.error
            else:
                error = self._error_text(out)
        res = RewriteResult(ok=ok, recipes=ids, changed_files=changed, log_path=str(log_path),
                            duration_s=round(pr.duration_s, 3), error=error, command=cmd, dry_run=dry_run,
                            recipes_applied=self.parse_recipe_attribution(out), forked=forked,
                            parse_problems=parse_problems)
        self._journal(res, module, config=str(config_path), artifacts=coords)
        log.info("rewrite %s: %d changed files (%.1fs)", "OK" if ok else "FAILED", len(changed), pr.duration_s)
        return res

    def _with_settings_copy(self, cmd: list[str], prepared: PreparedSettings) -> list[str]:
        """그림자 미러를 더한 settings 복사본이 있으면 명령의 ``-s`` 값을 그 복사본으로 바꾼다.
        번역 결과는 실행마다 한 번 기록한다.
        복사본이 없으면 명령을 그대로 반환한다.

        Journal `rewrite_settings {translated, shadows: [{repository, mirror}], skipped: [{repository, reason}],
        reason}` and one INFO line (a WARNING when preparing the copy raised) the first time a run sees a record, and
        again only when the record changes; nothing when the file needs no shadow. Never a url or the copy's
        content."""
        tr = prepared.translation
        if prepared.error is not None:
            rec = {"translated": False, "shadows": [], "skipped": [], "reason": f"error: {prepared.error}"}
        elif tr is not None and tr.needed:
            rec = tr.record()
        else:
            return cmd
        key = str(getattr(self.ws, "run_dir", id(self.ws)))
        if _SETTINGS_RECORDS.get(key) != rec:
            _SETTINGS_RECORDS[key] = rec
            self.ws.journal({"event": "rewrite_settings", **rec})
            name = Path(str(self.cfg.build.settings_file)).name
            skipped = ", ".join(f"{x['repository']} ({x['reason']})" for x in rec["skipped"]) or "none"
            if prepared.error is not None:
                log.warning("rewrite: no settings copy for OpenRewrite could be made from %s (%s); the original is "
                            "passed", name, prepared.error)
            elif rec["reason"]:
                log.info("rewrite: OpenRewrite gets the settings file %s unchanged: %s", name, rec["reason"])
            else:
                shadows = ", ".join(f"{x['repository']} -> {x['mirror'] or 'its own url'}" for x in rec["shadows"])
                log.info("rewrite: OpenRewrite gets a copy of the settings file %s with %d shadow mirror(s) (%s; "
                         "skipped: %s)", name, len(rec["shadows"]), shadows or "none", skipped)
        if prepared.path is None:
            return cmd
        original = str(Path(str(self.cfg.build.settings_file)).expanduser().absolute())
        for i in range(len(cmd) - 1):
            if cmd[i] == "-s" and cmd[i + 1] == original:
                return cmd[:i + 1] + [str(prepared.path)] + cmd[i + 2:]
        return cmd

    def _journal(self, res: RewriteResult, module: str | None, **extra) -> None:
        self.ws.journal({"event": "rewrite", "recipes": res.recipes, "ok": res.ok, "dry_run": res.dry_run,
                         "module": module, "changed_files_count": len(res.changed_files),
                         "parse_problems": len(res.parse_problems),
                         "duration_s": res.duration_s, "log": res.log_path, "error": res.error, **extra})
