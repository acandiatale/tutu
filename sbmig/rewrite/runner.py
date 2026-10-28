"""OpenRewriteRunner — 변환(OpenRewrite 1회) 단계의 실행기(``RewriteRunnerLike``):
``rewrite-maven-plugin`` 의 run / dryRun. 흐름상 위치: 계획의 ``transform.rewrite`` 단계에서 오케스트레이터가
부른다(빌드 없음, 바뀐 것은 커밋 ``rewrite: …``). 레시피는 v2 결정대로 ``UpgradeToJava21``, (필요하면)
``JavaxMigrationToJakarta``, Boot 프로젝트는 ``UpgradeSpringBoot_4_0`` + ``SpringBootProperties_4_1`` — id 의
원본은 ``knowledge/recipes/catalog.yml``(불변조건 5)이고 이 모듈은 받은 id 를 그대로 쓴다. 핵심 진입점:
``OpenRewriteRunner.run``(재시도 포함), ``command``,
``parse_changed_files`` / ``parse_problems``.
재시도: 힙 부족(OOM) → 큰 ``oom_retry_jvm_args`` 로 1회, 아니면 다운로드 실패(429 / 5xx / 끊김) → 기다렸다
``download_retry`` 회; 그래도 fork 된 ``compile`` 에서 실패면(컴파일 안 되는 프로젝트) ``runNoFork`` 로 1회.
실패해도 예외 없이 RewriteResult — 변환이 실패해도 실행은 빌드로 이어진다.

OpenRewriteRunner — implements ``RewriteRunnerLike`` with ``rewrite-maven-plugin`` (run / dryRun)."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from sbmig.build.log_parser import parse_maven_log
from sbmig.build.mvn import base_command, mvn_env, run_logged
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

# 레시피 id 접두어 → 그 레시피를 담은 레시피 아티팩트(가장 긴 접두어가 이김). 핵심
# 레시피(org.openrewrite.java.* 등)는 플러그인에 들어 있어 아티팩트가 필요 없다.
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
# 플러그인이 파싱하지 못한 파일(파일별 줄 또는 요약 줄) — 그
# 파일에는 레시피가 적용되지 않았다는 뜻이라 보고서에 남긴다.
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
    """레시피 id 를 담은 레시피 아티팩트(g:a), 가장 긴 접두어 기준. 플러그인 내장 레시피면 None."""
    best: str | None = None
    best_len = -1
    for prefix, art in RECIPE_PREFIX_ARTIFACTS.items():
        if recipe_id.startswith(prefix) and len(prefix) > best_len:
            best, best_len = art, len(prefix)
    return best


class OpenRewriteRunner:
    """``OpenRewriteRunner(ws, cfg).run([RecipeRef(...)])`` → RewriteResult. 실패한 실행에도 예외를 내지
    않는다(호출자가 준 추가 레시피 파일이 없거나 YAML 이 깨진 경우만 예외). ``kb`` 는 오케스트레이터 생성자와
    맞추려고 받기만 한다(v2 에는 KB 레시피가 없다).

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
        """이번 실행의 rewrite.yml 을 ``logs/rewrite-<n>.yml`` 에 새 번호로 쓴다(앞 실행의 파일을 덮어쓰지
        않아 재시도마다 무엇을 돌렸는지 남는다). (경로, 텍스트) 반환."""
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
        """필요한 레시피 아티팩트 g:a: ``RecipeRef.artifact`` 명시분, 내장 id 에서 추론한 것, 합성 파일에
        선언된 레시피면 rewrite-migrate-java + rewrite-spring 과 그 recipeList 가 가리키는 것.

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
        """mvn 환경 + MAVEN_OPTS 에 ``jvm_args``(기본 ``openrewrite.jvm_args`` = ``-Xmx4g``; OOM
        재시도는 ``oom_retry_jvm_args``).

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
        """플러그인 명령. ``fork=False`` → ``runNoFork``(compile 생명주기를 fork 하지 않아 컴파일 안 되는
        프로젝트에서도 동작), ``fork=True`` → ``run``, None → 설정 ``plugin_goal``(기본 run). 활성 레시피는
        합성 레시피 하나뿐이고, 모르는 / 잘못된 레시피는 BUILD FAILURE(``failOnInvalidActiveRecipes``), JSP /
        HTML 은 ``openrewrite.exclusions`` 로 뺀다(XML 파서 잡음, 어떤 레시피도 건드리지 않음).

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
        been moved' 줄(dryRun 은 'These recipes would …')에서 바뀐 파일 목록(역슬래시 → 슬래시, 중복 제거)."""
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
        """플러그인이 파싱하지 못했다고 보고한 파일들('There were problems parsing <path>'), 로그 순서
        유지·중복 제거. ``root`` 아래 절대경로는 상대경로로.
        요약 줄만 있으면 ``[PARSE_PROBLEMS_UNKNOWN]``, 없으면 [].

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
        """레시피 id → 그 레시피가 바꾼 파일. 'Changes have been made to <f> by:' 아래 들여쓴 레시피
        줄마다(합성 레시피와 그 안의 기본 레시피 모두 기록).

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
        """플러그인의 'Recipe validation error' 줄에서 (모르는 레시피 id 목록, 그 밖의 검증 메시지).

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
        """실패 이유 한 줄: 레시피 검증 문제가 있으면 그것, 없으면 Maven 로그 파서가 찾은 오류 앞 10개."""
        unknown, other = self.validation_problems(text)
        parts = []
        if unknown:
            parts.append("unknown recipe(s): " + ", ".join(unknown))
        if other:
            parts.append("recipe validation: " + "; ".join(other[:8]))
        if parts:
            return " | ".join(parts)
        errors = parse_maven_log(text, self.ws.root)
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
        """변환 한 번(재시도 포함). ``extra_args``(예 ``-pl order-service -am``)는 reactor 루트에서 대상
        하나만 돌려 형제 모듈을 로컬 저장소가 아니라 reactor 에서 풀게 한다. 재시도 순서: OOM → 큰 힙으로 1회,
        아니면 다운로드 실패 → 대기 후 재시도; 그래도 fork 된 compile 에서 실패면 runNoFork 1회(앞 재시도의
        표시 ``oom-retry`` / ``download-retry`` 는 오류에 이어 붙인다).

        `extra_args` (e.g. `["-pl", "order-service", "-am"]`) run the plugin from the reactor root for one target so
        in-repo siblings resolve from the reactor instead of the local repository (per-target runs cannot see them)."""
        self._extra_args = list(extra_args)      # consumed by every _run_once of this call (incl. OOM / noFork retries)
        res = self._run_once(recipes, extra_recipe_files=extra_recipe_files, module=module, dry_run=dry_run,
                             label=label, fork=fork)
        jvm_args: list[str] | None = None
        # catalog E8: the recipe run itself ran out of heap → one retry with the bigger oom_retry_jvm_args
        if not res.ok and self._is_oom(res):
            jvm_args = list(self.cfg.openrewrite.oom_retry_jvm_args)
            res = self._oom_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                  dry_run=dry_run, label=label, fork=fork, jvm_args=jvm_args)
        # catalog X8: a transfer failure (proxy 429 / 5xx, reset, timeout) → wait and retry the same run
        elif not res.ok and self.cfg.openrewrite.download_retry > 0 and self._download_failure(res):
            res = self._download_retry(res, recipes, extra_recipe_files=extra_recipe_files, module=module,
                                       dry_run=dry_run, label=label, fork=fork)
        # run 은 레시피 전에 compile 을 fork 한다 → 컴파일이 안 되는 프로젝트(마이그레이션 전이라
        # 흔하다)에서는 레시피가 아예 안 돈다 → runNoFork(해결된 의존성 classpath 로 파싱)로 다시.
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

    def _is_oom(self, res: RewriteResult) -> bool:
        return bool(self._OOM_RE.search(self._failure_text(res)))

    # ------------------------------------------------------------------ download failures (catalog X8)
    # 전송 문제만 재시도한다: 정말 없는 아티팩트('Could not find artifact' / 'was not found in')는 기다려도
    # 생기지 않는다 — parent 점검과 빌드 루프의 해결 분류가 다룬다.
    # transfer problems only: a genuinely missing artifact ("Could not find artifact" / "was not found in") is not
    # retried — the orchestrator's parent check and the build loop's resolution triage handle that
    _DOWNLOAD_RE = re.compile(
        r"Failed to download|Could not transfer artifact|Transfer failed|status code: 429|Too Many Requests|"
        r"Connection reset|Read timed out|Connection timed out|SocketTimeoutException|UnknownHostException|"
        r"status code: 50[23]|Service Unavailable|Bad Gateway", re.IGNORECASE)

    def _download_failure(self, res: RewriteResult) -> str | None:
        """The matched transfer-failure wording (journalled as `reason`), or None."""
        m = self._DOWNLOAD_RE.search(self._failure_text(res))
        return m.group(0) if m else None

    def _download_retry(self, first: RewriteResult, recipes: list[RecipeRef], *, extra_recipe_files: Iterable[Path],
                        module: str | None, dry_run: bool, label: str, fork: bool | None) -> RewriteResult:
        """``download_retry_wait_s`` 기다렸다 최대 ``download_retry`` 회 다시 돌린다. 성공하거나 실패 이유가
        더 이상 전송 문제가 아니면 멈춘다. 끝내 실패면 오류 앞에 ``download-retry: ``.

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
        """힙 부족(OutOfMemoryError / GC overhead) → ``oom_retry_jvm_args`` 로 한 번 더. 실패면 오류 앞에
        ``oom-retry``(여전히 OOM 이면 그 표시까지)."""
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
        """플러그인 실행 한 번: rewrite.yml 합성 → 레시피 아티팩트 버전 결정(실패면 ok=False 결과) → 명령 →
        ``run_logged``. 성공 = exit 0 + 'BUILD SUCCESS' + 레시피 검증 문제 없음. 바뀐 파일 = 로그에 찍힌 것 +
        실행 전 HEAD 이후의 git 변경(target/ 제외)."""
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
        # 옛 플러그인은 잘못된 레시피에도 성공으로 끝나므로 로그의 검증 문제로 실패 처리한다.
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
