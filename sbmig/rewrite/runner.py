"""``rewrite-maven-plugin``의 run / dryRun으로 레시피를 적용하는 변환 단계 실행기(``RewriteRunnerLike``) 모듈.
계획의 ``transform.rewrite`` 단계에서 오케스트레이터가 호출한다.
빌드는 하지 않고 바뀐 내용은 ``rewrite: …`` 커밋이 된다.
레시피 id는 ``knowledge/recipes/catalog.yml``에서 관리하고 이 모듈은 받은 id를 그대로 쓴다.
- 힙이 부족하면 ``oom_retry_jvm_args``로 한 번, 다운로드에 실패하면 기다렸다가 ``download_retry``번 다시 실행한다
- 그래도 fork된 ``compile``에서 실패하면(컴파일되지 않는 프로젝트) ``runNoFork``로 한 번 더 실행한다
- 변환이 실패해도 실행이 빌드로 이어지도록 예외 없이 RewriteResult를 반환한다

OpenRewriteRunner — implements ``RewriteRunnerLike`` with ``rewrite-maven-plugin`` (run / dryRun)."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from sbmig.build.log_parser import parse_maven_log
from sbmig.build.mvn import base_command, mvn_env, run_logged
from sbmig.build.runner import negative_cache_hits
from sbmig.core.config import Config
from sbmig.core.interfaces import KnowledgeBaseLike
from sbmig.core.models import RecipeRef, RewriteResult
from sbmig.core.workspace import GitError, Workspace

from .compose import COMPOSITE_RECIPE_ID, compose_rewrite_yaml, recipe_documents, recipe_list_ids
from .versions import RecipeArtifactError, resolve_recipe_artifact_versions

log = logging.getLogger("sbmig.rewrite")

PLUGIN_GA = "org.openrewrite.maven:rewrite-maven-plugin"
MIGRATE_JAVA = "org.openrewrite.recipe:rewrite-migrate-java"
SPRING = "org.openrewrite.recipe:rewrite-spring"

# 레시피 id 접두어별로 그 레시피를 담은 아티팩트(가장 긴 접두어 우선).
# org.openrewrite.java.* 같은 핵심 레시피는 플러그인에 들어 있어 아티팩트가 필요 없다
# recipe id prefix → recipe artifact that ships it (longest prefix wins). Core recipes (org.openrewrite.java.*,
# org.openrewrite.maven.*, org.openrewrite.text.* …) ship with the plugin itself → no artifact.
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
# 플러그인이 파싱하지 못한 파일에는 레시피가 적용되지 않았으므로 보고서에 남기기 위해 찾는다
# rewrite-maven-plugin 6.46.1: "There were problems parsing <path>" per file (sometimes with text after the path)
# and/or the summary "There were problems parsing some source files[, run with --errors …]".
_PARSE_PROBLEM_RE = re.compile(r"There were problems parsing (?P<rest>.*)$")
PARSE_PROBLEMS_UNKNOWN = "(unknown files — see the rewrite log)"
_DOES_NOT_EXIST_RE = re.compile(r"recipe '(?P<id>[^']+)' does not exist")


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


class OpenRewriteRunner:
    """OpenRewrite 플러그인을 실행하고 결과를 RewriteResult로 반환하는 클래스.
    실행이 실패해도 예외를 던지지 않는다.
    호출자가 준 추가 레시피 파일이 없거나 YAML이 깨진 경우만 예외를 던진다.
    ``kb``는 오케스트레이터 생성자와 맞추기 위해 받기만 한다.

    ``OpenRewriteRunner(ws, cfg).run([RecipeRef(...)])`` → ``RewriteResult``. Never raises for a failed run;
    raises ``RecipeCompositionError`` only when a recipe that names its own file is not defined in it. `kb` is
    accepted for the orchestrator's constructor call and not used (no KB recipes in v2)."""

    def __init__(self, ws: Workspace, cfg: Config, kb: KnowledgeBaseLike | None = None, *,
                 timeout_s: float | None = None):
        self.ws = ws
        self.cfg = cfg
        self.kb = kb
        self.timeout_s = timeout_s if timeout_s is not None else float(cfg.openrewrite.timeout_minutes) * 60.0
        # v2.1: the composed rewrite-N.yml and the recipe-version cache sit next to the rewrite logs (no rewrite/ dir)
        self.work_dir = getattr(ws, "logs_dir", None) or ws.run_dir / "logs"
        self._sleep = time.sleep                 # download retry wait; tests replace it with a recorder

    # ------------------------------------------------------------------ composition
    def compose(self, recipes: Sequence[RecipeRef], extra_recipe_files: Iterable[Path] = ()) -> tuple[Path, str]:
        """이번 실행의 rewrite.yml을 ``logs/rewrite-<n>.yml``에 새 번호로 저장하고 (경로, 텍스트)를 반환한다.
        앞 실행의 파일을 덮어쓰지 않으므로 재시도마다 무엇을 실행했는지 남는다."""
        text = compose_rewrite_yaml(recipes, extra_files=list(extra_recipe_files), target=self.cfg.target)
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
        ``RecipeRef.artifact``에 지정된 것, 내장 id에서 추론한 것, 합성 파일에 선언된 레시피면
        rewrite-migrate-java, rewrite-spring과 그 recipeList가 가리키는 것을 담는다.

        g:a recipe artifacts needed: explicit ``RecipeRef.artifact``, inferred from builtin ids, and for a recipe
        declared in a composed file rewrite-migrate-java + rewrite-spring plus whatever its recipeList references."""
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

    # ------------------------------------------------------------------ command
    def env(self, jvm_args: Sequence[str] | None = None) -> dict[str, str]:
        """mvn 환경 변수를 만들고 MAVEN_OPTS에 ``jvm_args``를 더한다.
        기본값은 ``openrewrite.jvm_args``(``-Xmx4g``)이고 OOM 재시도는 ``oom_retry_jvm_args``를 쓴다.

        mvn env with MAVEN_OPTS += `jvm_args` (default cfg.openrewrite.jvm_args; the OOM retry passes
        cfg.openrewrite.oom_retry_jvm_args)."""
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
        ``runNoFork``는 compile 생명주기를 fork하지 않으므로 컴파일되지 않는 프로젝트에서도 동작한다.
        활성 레시피는 합성 레시피 하나이며 잘못된 레시피는 BUILD FAILURE로 끝난다.
        JSP / HTML은 XML 파서 잡음만 내므로 ``openrewrite.exclusions``로 뺀다.

        `fork=False` → rewrite:runNoFork (no `compile` lifecycle fork: works on projects that do not compile);
        `fork=True` → rewrite:run; None → config `plugin_goal`."""
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
        cmd.append("-Drewrite.failOnInvalidActiveRecipes=true")    # unknown/invalid recipe ⇒ BUILD FAILURE
        excl = [x for x in (getattr(self.cfg.openrewrite, "exclusions", None) or []) if x]
        if excl:                                                    # JSP / HTML: XML-parser noise, no recipe touches them
            cmd.append(f"-Drewrite.exclusions={','.join(excl)}")
        return cmd

    # ------------------------------------------------------------------ result parsing
    @staticmethod
    def parse_changed_files(text: str, *, dry_run: bool = False) -> list[str]:
        """플러그인 로그의 'Changes have been made to …' / 'Generated new file' / 'Deleted file' / 'File has
        been moved' 줄(dryRun은 'These recipes would …')에서 바뀐 파일 목록을 만든다.
        역슬래시는 슬래시로 바꾸고 중복은 없앤다."""
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
        """플러그인이 파싱하지 못했다고 보고한 파일('There were problems
        parsing <path>')을 로그 순서대로 중복 없이 반환한다.
        ``root`` 아래 절대 경로는 상대 경로로 바꾼다.
        요약 줄만 있으면 ``[PARSE_PROBLEMS_UNKNOWN]``을, 아무것도 없으면 빈 리스트를 반환한다.

        Files the plugin reported as unparseable ("There were problems parsing <path>"), `\\` -> `/`,
        de-duplicated, log order kept. Absolute paths under `root` become relative to it; other paths stay as logged.
        Only the summary line ("... parsing some source files") -> ``[PARSE_PROBLEMS_UNKNOWN]``; nothing -> ``[]``."""
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
        """레시피 id마다 그 레시피가 바꾼 파일을 모은다. 'Changes have been made to <f> by:' 아래 들여쓴
        레시피 줄을 읽으며 합성 레시피와 그 안의 기본 레시피를 모두 기록한다.

        recipe id → files it changed, from the plugin's indented 'Changes have been made to <f> by:' blocks
        (every recipe id in the chain is credited, so a composite and the primitive it wraps both appear)."""
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
        """플러그인의 'Recipe validation error' 줄에서 (모르는 레시피 id 목록, 그 밖의 검증 메시지)를 꺼낸다.

        (unknown recipe ids, other validation messages) from the plugin's 'Recipe validation error' lines."""
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
        레시피 검증 문제가 있으면 그것을, 없으면 Maven 로그 파서가 찾은 오류 앞 10개를 쓴다."""
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
        return "; ".join(msgs[:10]) if msgs else "rewrite run failed (see log)"

    def _head(self) -> str | None:
        try:
            return self.ws.head()
        except (GitError, OSError):
            return None

    # ------------------------------------------------------------------ run
    def run(self, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path] = (), module: str | None = None,
            dry_run: bool = False, label: str = "", fork: bool | None = None,
            extra_args: Iterable[str] = ()) -> RewriteResult:
        """재시도를 포함해 변환을 한 번 실행한다.
        ``extra_args``(예 ``-pl order-service -am``)는 reactor 루트에서 대상 하나만 실행해 형제 모듈을 로컬
        저장소가 아니라 reactor에서 찾게 한다.
        재시도 순서: OOM이면 큰 힙으로 1회, 아니면 다운로드 실패 시 대기 후 재시도, 그래도 fork된
        compile에서 실패하면 runNoFork 1회.

        `extra_args` (e.g. `["-pl", "order-service", "-am"]`) run the plugin from the reactor root for one target so
        in-repo siblings resolve from the reactor instead of the local repository (per-target runs cannot see them)."""
        self._extra_args = list(extra_args)      # consumed by every _run_once of this call (incl. OOM / noFork retries)
        res = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                             label=label, fork=fork)
        jvm_args: list[str] | None = None
        # Maven의 HTTP 차단 미러에 막히면 어떤 재시도로도 해결되지 않으므로
        # 바로 반환하고 오케스트레이터가 실행을 중단한다
        if not res.ok and self._http_blocked(res):
            log.warning("rewrite: Maven's HTTP repository blocker refused a repository — no retry (fix the Maven settings)")
            return res
        # catalog E8: the recipe run itself ran out of heap → one retry with the bigger oom_retry_jvm_args
        if not res.ok and self._is_oom(res):
            jvm_args = list(self.cfg.openrewrite.oom_retry_jvm_args)
            res = self._oom_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                  dry_run=dry_run, label=label, fork=fork, jvm_args=jvm_args)
        # catalog X8: a transfer failure (proxy 429 / 5xx, reset, timeout) → wait and retry the same run
        elif not res.ok and self.cfg.openrewrite.download_retry > 0 and self._download_failure(res):
            res = self._download_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                       dry_run=dry_run, label=label, fork=fork)
        # 모델 단계의 해결 실패는 오래된 `*.lastUpdated` 표식 때문일 수 있으므로 -U로 한 번만 다시 실행한다.
        # 여기서 막히면 레시피가 실행되지 않는다
        if not res.ok and not self._download_failure(res) and self._may_force_update() and self._force_update_hits(res):
            res = self._force_update_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                           dry_run=dry_run, label=label, fork=fork, jvm_args=jvm_args)
        # run은 레시피 전에 compile을 fork하므로 컴파일되지 않는 프로젝트에서는 레시피가 실행되지 않는다.
        # 그래서 runNoFork로 다시 실행한다
        # rewrite:run forks the `compile` lifecycle first; on a project that does not compile the recipes never run.
        # Fall back to runNoFork (parses sources against the resolved dependency classpath instead).
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

    # ------------------------------------------------------------------ OOM (catalog E8)
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
        """실패 텍스트에 Maven의 HTTP 차단 미러가 보이는지
        확인한다(``sbmig.build.env_errors.http_blocker_hit``)."""
        from sbmig.build.env_errors import http_blocker_hit
        return http_blocker_hit(self._failure_text(res)) is not None

    def _is_oom(self, res: RewriteResult) -> bool:
        return bool(self._OOM_RE.search(self._failure_text(res)))

    # ------------------------------------------------------------------ download failures (catalog X8)
    # 실제로 없는 아티팩트는 기다려도 생기지 않으므로 전송 문제만 재시도한다
    # transfer problems only: a genuinely missing artifact ("Could not find artifact" / "was not found in") is not
    # retried — the orchestrator's parent check and the build loop's resolution triage handle that
    _DOWNLOAD_RE = re.compile(
        r"Failed to download|Could not transfer artifact|Transfer failed|status code: 429|Too Many Requests|"
        r"Connection reset|Read timed out|Connection timed out|SocketTimeoutException|UnknownHostException|"
        r"status code: 50[23]|Service Unavailable|Bad Gateway", re.IGNORECASE)

    def _may_force_update(self) -> bool:
        """-U 재시도를 할 수 있는지 반환한다.
        ``build.retry_negative_cache``가 켜져 있고 명령에 -U / -o가 없어야 한다."""
        if not getattr(self.cfg.build, "retry_negative_cache", True):
            return False
        args = list(getattr(self, "_extra_args", ()) or []) + list(getattr(self.cfg.build, "extra_args", ()) or [])
        return not any(a in ("-U", "--update-snapshots", "-o", "--offline") for a in args) and not self.cfg.build.offline

    def _force_update_hits(self, res: RewriteResult) -> list[str]:
        """캐시된 해결 실패나 모델 단계 해결 실패의 좌표(``negative_cache_hits``)를 반환한다.
        없으면 빈 리스트를 반환한다."""
        return negative_cache_hits(self._failure_text(res))

    def _force_update_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                            module: str | None, dry_run: bool, label: str, fork: bool | None,
                            jvm_args: Sequence[str] | None = None) -> RewriteResult:
        """``-U``를 붙여 한 번 더 실행하며 이 호출의 이후 재시도도 -U를 이어받는다.
        끝내 실패하면 오류 앞에 ``force-update-retry: ``를 붙이고 저널에
        ``rewrite_force_update_retry`` / ``…_result``를 기록한다.

        One more run with `-U` (later retries of this call inherit it); a final failure is prefixed
        `force-update-retry: `; journal `rewrite_force_update_retry` / `rewrite_force_update_retry_result`."""
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
        """The matched transfer-failure wording (journalled as `reason`), or None."""
        m = self._DOWNLOAD_RE.search(self._failure_text(res))
        return m.group(0) if m else None

    def _download_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                        module: str | None, dry_run: bool, label: str, fork: bool | None) -> RewriteResult:
        """``download_retry_wait_s``만큼 기다렸다가 최대 ``download_retry``번 다시 실행한다.
        성공하거나 실패 이유가 더 이상 전송 문제가 아니면 멈춘다.
        끝내 실패하면 오류 앞에 ``download-retry: ``를 붙인다.

        Wait `download_retry_wait_s` and re-run, up to `download_retry` times; stops early on success or when
        the failure no longer looks like a transfer problem. A final failure is prefixed `download-retry: `."""
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
                break           # failing for another reason now — further waiting will not help
            reason = again
        if not res.ok:
            res.error = f"download-retry: {res.error or 'rewrite run failed'}"
        self.ws.journal({"event": "rewrite_download_retry_result", "ok": res.ok, "log": res.log_path,
                         "error": res.error})
        return res

    def _oom_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                   module: str | None, dry_run: bool, label: str, fork: bool | None,
                   jvm_args: list[str]) -> RewriteResult:
        """힙 부족(OutOfMemoryError / GC overhead)이면 ``oom_retry_jvm_args``로 한 번 더 실행한다.
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
        exit 0, 'BUILD SUCCESS', 레시피 검증 문제 없음이 모두 맞아야 성공이다.
        바뀐 파일은 로그에 나온 것과 실행 전 HEAD 이후의 git 변경(target/ 제외)을 합친다."""
        ids = [r.id for r in recipes]
        if not recipes:
            return RewriteResult(ok=True, recipes=[], dry_run=dry_run)
        config_path, text = self.compose(recipes, extra_recipe_files)      # may raise RecipeCompositionError
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
        goal = cmd[[i for i, c in enumerate(cmd) if c.startswith(PLUGIN_GA)][0]].rsplit(":", 1)[-1]
        forked = goal in ("run", "dryRun")
        head_before = None if dry_run else self._head()
        log.info("rewrite %s (%s)", ", ".join(ids), goal)
        pr = run_logged(cmd, cwd=cwd, env=env, log_path=log_path, timeout_s=self.timeout_s)
        out = pr.text
        ok = pr.exit_code == 0 and not pr.timed_out and "BUILD SUCCESS" in out
        # 이전 플러그인은 잘못된 레시피에도 성공으로 끝나므로 로그의 검증 문제로 실패 처리한다
        if ok and any(self.validation_problems(out)):         # plugin older than failOnInvalidActiveRecipes
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
            if _is_abs(p):          # outside the module dir: relative to the project root if it is under it
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

    def _journal(self, res: RewriteResult, module: str | None, **extra) -> None:
        self.ws.journal({"event": "rewrite", "recipes": res.recipes, "ok": res.ok, "dry_run": res.dry_run,
                         "module": module, "changed_files_count": len(res.changed_files),
                         "parse_problems": len(res.parse_problems),
                         "duration_s": res.duration_s, "log": res.log_path, "error": res.error, **extra})
