"""레시피 jar 검사(``sbmig recipes check --jars``) — 카탈로그의
openrewrite id 가 정말 카탈로그가 말하는 jar 에 있는가. 흐름상 위치: 실행과 무관한 운영 도구. 오프라인
``RecipeCatalog.check`` 는 artifact / provided_by 를 믿기만 하므로, 설정된
``openrewrite.recipe_bom_version``(3.37.0) 에서 실제 jar 로 확인한다: 레시피 아티팩트마다 BOM 을 import 하는
pom 을 만들어 ``dependency:build-classpath`` → classpath 의 jar 마다 ``META-INF/rewrite/*.yml`` 과 ``.class``
를 훑어 → 카탈로그 항목마다 선언한 jar(found_in)와 classpath 포함 여부를 판정한다. Maven 실패는 예외가 아니라
보고서의 문제 줄(실패한 아티팩트와 로그 경로)이 된다. Maven + 네트워크가 필요하다.

Jar-checked recipe catalog: does every `origin: openrewrite` catalog id really live where the catalog says?

`RecipeCatalog.check(cfg)` is offline: it trusts the `artifact` / `provided_by` fields. `check_jars` verifies them
against the real recipe jars at the configured `openrewrite.recipe_bom_version`:

1. every configured `openrewrite.recipe_artifacts` entry (plus any distinct `provided_by`) is resolved once to its
   transitive runtime classpath with Maven (`dependency:build-classpath` on a generated pom that imports the recipe
   BOM, written under `work_dir/<group>__<artifact>/`);
2. every jar on those classpaths is scanned once: `META-INF/rewrite/*.yml` recipe declarations (`name:` of each
   `type: specs.openrewrite.org/v1beta/recipe` document) and `.class` entries (imperative recipes);
3. per catalog entry: the jar that declares the id (`found_in`), whether `artifact` is on the classpath of the
   configured artifact that is supposed to bring it, and for `provided_by` whether `artifact` is on the
   `provided_by` artifact's own transitive classpath. Core entries (`artifact: null`, `org.openrewrite.java.*`,
   `org.openrewrite.maven.*` …) are searched in the union of all classpaths.

Maven failures never raise: the report names the artifact that did not resolve and its log file.
`sbmig recipes check --jars` is the CLI (docs/RECIPES.md). Needs Maven + network (or a warm local repository).


"""
from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from xml.sax.saxutils import escape

import yaml
from pydantic import BaseModel, Field

from sbmig.core.config import Config

from .catalog import CORE_GROUP, RecipeCatalog

log = logging.getLogger("sbmig.plan.jarcheck")

DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"
RECIPE_TYPE = "specs.openrewrite.org/v1beta/recipe"
DEFAULT_WORK_DIR = "out/recipes-check"

Resolver = Callable[[str, str], list[Path]]

# Tests (and embedding code) may replace this with a fake `fn(ga, version) -> list[Path]`; None = Maven.
DEFAULT_RESOLVER: Resolver | None = None


class JarCheckRow(BaseModel):
    """카탈로그 항목 하나의 jar 검사 결과: 버전, 선언한 jar(found_in),
    소유 classpath 포함 여부, provided_by 판정, 문제 목록."""
    id: str
    origin: str = "openrewrite"
    artifact: str | None = None
    provided_by: str | None = None
    version: str = ""                              # version of `artifact` ("" if unknown / core)
    found_in: str = ""                             # "g:a:v" (or jar name) of the jar declaring the id; "" = not found
    artifact_on_classpath: bool | None = None   # None: core entry, or the owning classpath did not resolve
    provided_by_ok: bool | None = None          # None: no provided_by
    problems: list[str] = Field(default_factory=list)


class JarCheckReport(BaseModel):
    """jar 검사 보고서 전체: 항목별 줄, 문제, 아티팩트별
    classpath·버전, BOM 버전, 작업 디렉토리, Maven 로그 경로."""
    rows: list[JarCheckRow] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    classpaths: dict[str, list[str]] = Field(default_factory=dict)     # configured g:a → jar basenames
    artifact_versions: dict[str, str] = Field(default_factory=dict)    # g:a → version (configured + resolved)
    bom_version: str = ""
    work_dir: str = ""
    log_paths: dict[str, str] = Field(default_factory=dict)            # g:a → build-classpath log

    @property
    def ok(self) -> bool:
        """문제가 하나도 없는가."""
        return not self.problems

    @property
    def jars_scanned(self) -> int:
        """훑은 jar 수(중복 제외)."""
        return len({j for jars in self.classpaths.values() for j in jars})

    @property
    def declaring_jars(self) -> list[str]:
        """레시피를 선언한 jar 들."""
        return sorted({r.found_in for r in self.rows if r.found_in})


# ---------------------------------------------------------------------------
# Maven: one generated pom per artifact → dependency:build-classpath
# ---------------------------------------------------------------------------
def artifact_dir(work_dir: Path, ga: str) -> Path:
    """아티팩트별 작업 디렉토리 ``<work_dir>/<group>__<artifact>`` (Windows 에서 ``:`` 를 쓸 수 없어서)."""
    return Path(work_dir) / ga.replace(":", "__")


def _pom_xml(cfg: Config, ga: str, version: str) -> str:
    """레시피 BOM 을 import 하고 그 아티팩트 하나만 의존하는 검사용 pom(버전이 ""면 BOM 이 정한다)."""
    g, a = ga.split(":", 1)
    ver = f"\n      <version>{escape(version)}</version>" if version else ""   # "" → managed by the BOM import
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- generated by sbmig (recipes check, jar mode); safe to delete -->
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>sbmig.recipes-check</groupId>
  <artifactId>check-{escape(a)}</artifactId>
  <version>0</version>
  <packaging>pom</packaging>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.openrewrite.recipe</groupId>
        <artifactId>rewrite-recipe-bom</artifactId>
        <version>{escape(cfg.openrewrite.recipe_bom_version)}</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
  <dependencies>
    <dependency>
      <groupId>{escape(g)}</groupId>
      <artifactId>{escape(a)}</artifactId>{ver}
    </dependency>
  </dependencies>
</project>
"""


def resolve_classpath(cfg: Config, artifact_ga: str, version: str, *, work_dir: Path,
                      mvn_env: dict[str, str] | None = None, timeout_s: float = 600) -> list[Path]:
    """레시피 BOM 을 import 한 상태에서 ``artifact_ga[:version]`` 의 전이 runtime classpath(jar 목록).
    실패하면 [] (로그는 ``artifact_dir(work_dir, ga)/build-classpath.log``) — Maven 문제로 예외를 내지 않는다.

    Transitive runtime classpath (jars) of `artifact_ga[:version]`, the recipe BOM imported. [] on any failure
    (the log is `artifact_dir(work_dir, ga)/build-classpath.log`). Never raises for Maven problems."""
    from sbmig.build.classpath import _parse_classpath
    from sbmig.build.mvn import base_command, run_logged
    from sbmig.build.mvn import mvn_env as _mvn_env

    d = artifact_dir(Path(work_dir).resolve(), artifact_ga)
    d.mkdir(parents=True, exist_ok=True)
    (d / "pom.xml").write_text(_pom_xml(cfg, artifact_ga, version), encoding="utf-8")
    cp_file = d / "cp.txt"
    cp_file.unlink(missing_ok=True)
    cmd = base_command(cfg) + [f"{DEPENDENCY_PLUGIN}:build-classpath", f"-Dmdep.outputFile={cp_file}",
                               "-Dmdep.includeScope=runtime"]
    res = run_logged(cmd, cwd=d, env=mvn_env if mvn_env is not None else _mvn_env(cfg),
                     log_path=d / "build-classpath.log", timeout_s=timeout_s)
    if res.exit_code != 0 or not cp_file.is_file():
        log.warning("build-classpath for %s:%s failed (exit %s); see %s", artifact_ga, version or "(bom)",
                    res.exit_code, d / "build-classpath.log")
        return []
    return [p for p in _parse_classpath(cp_file.read_text(encoding="utf-8")) if p.suffix == ".jar"]


# ---------------------------------------------------------------------------
# jar scanning (cached per path)
# ---------------------------------------------------------------------------
_DOC_SPLIT = re.compile(r"^---[ \t]*$", re.MULTILINE)
_TOP_KEY = re.compile(r"^(type|name)[ \t]*:[ \t]*(['\"]?)([^'\"#\s]+)\2[ \t]*(?:#.*)?$", re.MULTILINE)


def _yaml_recipe_names(text: str) -> set[str]:
    """META-INF/rewrite yml 의 모든 레시피 문서의 ``name:``. PyYAML 이 못 읽는 파일은 ``---`` 문서마다
    최상위 ``type:`` / ``name:`` 줄을 훑는 방식으로 대신한다(일부 레시피 jar 의 yml 이 PyYAML 과 안 맞는다).

    `name:` of every recipe document in a META-INF/rewrite yml. Uses yaml.safe_load_all; a file PyYAML cannot
    parse falls back to a top-level `type:` / `name:` line scan per `---` document."""
    names: set[str] = set()
    try:
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("type") == RECIPE_TYPE and doc.get("name"):
                names.add(str(doc["name"]))
        return names
    except yaml.YAMLError:
        pass
    for chunk in _DOC_SPLIT.split(text):
        keys = {m.group(1): m.group(3) for m in _TOP_KEY.finditer(chunk)}
        if keys.get("type") == RECIPE_TYPE and keys.get("name"):
            names.add(keys["name"])
    return names


class _JarIndex:
    """jar 하나의 색인: yml 로 선언된 레시피 이름 + zip 항목 이름(명령형 레시피의 ``.class`` 찾기용)."""
    __slots__ = ("entries", "yaml_names")

    def __init__(self, yaml_names: set[str], entries: frozenset[str]):
        self.yaml_names = yaml_names
        self.entries = entries

    def declares(self, recipe_id: str) -> bool:
        return recipe_id in self.yaml_names or f"{recipe_id.replace('.', '/')}.class" in self.entries


_SCAN_CACHE: dict[tuple[str, float, int], _JarIndex] = {}


def scan_jar(jar: Path) -> _JarIndex:
    """jar 하나를 훑어 색인을 만든다((경로, 수정 시각, 크기) 캐시). 못 열면 빈 색인 + 경고.

    Recipe names declared in `META-INF/rewrite/*.yml` + every zip entry name (for `.class` lookups)."""
    p = Path(jar)
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime, st.st_size)
    except OSError:
        return _JarIndex(set(), frozenset())
    hit = _SCAN_CACHE.get(key)
    if hit is not None:
        return hit
    names: set[str] = set()
    entries: frozenset[str] = frozenset()
    try:
        with zipfile.ZipFile(p) as zf:
            entries = frozenset(zf.namelist())
            for n in sorted(entries):
                if n.startswith("META-INF/rewrite/") and n.endswith((".yml", ".yaml")):
                    names |= _yaml_recipe_names(zf.read(n).decode("utf-8", errors="replace"))
    except (OSError, zipfile.BadZipFile) as e:
        log.warning("cannot scan %s: %s", p, e)
    idx = _JarIndex(names, entries)
    _SCAN_CACHE[key] = idx
    return idx


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------
def _gav(jar: Path, repo: Path) -> str | None:
    """로컬 저장소 경로에서 jar 의 ``g:a:v`` 를 읽는다(못 읽으면 None)."""
    from sbmig.build.classpath import gav_for_jar

    g = gav_for_jar(jar, repo)
    return f"{g.group_id}:{g.artifact_id}:{g.version}" if g is not None else None


def _ga(gav: str) -> str:
    return ":".join(gav.split(":")[:2])


def _versions(cfg: Config, wanted: list[str], work_dir: Path, problems: list[str]) -> dict[str, str]:
    """레시피 아티팩트 버전들(``sbmig.rewrite.versions`` 와 같은 방식). 실패하면 문제로 적고 설정의 고정 버전,
    없으면 "" (BOM 이 정함)으로 계속한다."""
    from sbmig.build.mvn import mvn_env
    from sbmig.rewrite.versions import RecipeArtifactError, resolve_recipe_artifact_versions

    try:
        return resolve_recipe_artifact_versions(cfg, mvn_env(cfg), artifacts=wanted,
                                                cache_path=work_dir / "artifact-versions.json", work_dir=work_dir)
    except RecipeArtifactError as e:
        problems.append(f"recipe artifact versions: {e} (resolving with the BOM-managed version instead)")
    except Exception as e:  # noqa: BLE001  # pragma: no cover - the check must report, not crash
        problems.append(f"recipe artifact versions: {type(e).__name__}: {e}")
    overrides = dict(cfg.openrewrite.recipe_artifact_versions or {})
    return {a: overrides.get(a, "") for a in wanted}


def check_jars(catalog: RecipeCatalog, cfg: Config, *, work_dir: Path | None = None,
               artifacts: Iterable[str] | None = None, resolver: Resolver | None = None) -> JarCheckReport:
    """모든 ``origin: openrewrite`` 카탈로그 항목을 실제 레시피 jar 로 확인한다(모듈 docstring 의 3단계).
    ``resolver`` 를 주면 Maven 대신 그것을 쓴다(테스트용). core 항목은 모든 classpath 의 합집합에서 찾는다.

    Verify every `origin: openrewrite` catalog entry against the real recipe jars (see module doc)."""
    from sbmig.build.mvn import local_repo_path

    wd = (Path(work_dir) if work_dir is not None else cfg.resolve_path(DEFAULT_WORK_DIR)).resolve()
    wd.mkdir(parents=True, exist_ok=True)
    configured = list(dict.fromkeys(artifacts if artifacts is not None else cfg.openrewrite.recipe_artifacts))
    entries = [e for e in catalog.entries if e.origin == "openrewrite"]
    extra = [e.provided_by for e in entries if e.provided_by and e.provided_by not in configured]
    wanted = list(dict.fromkeys(configured + extra))
    report = JarCheckReport(bom_version=cfg.openrewrite.recipe_bom_version, work_dir=str(wd))
    head: list[str] = []

    versions = _versions(cfg, wanted, wd, head)
    report.artifact_versions = {a: versions.get(a, "") for a in wanted}
    resolve: Resolver | None = resolver or DEFAULT_RESOLVER
    if resolve is None:
        from sbmig.build.mvn import mvn_env

        env = mvn_env(cfg)

        def _maven(ga: str, version: str) -> list[Path]:
            report.log_paths[ga] = str(artifact_dir(wd, ga) / "build-classpath.log")
            return resolve_classpath(cfg, ga, version, work_dir=wd, mvn_env=env)

        resolve = _maven

    repo = local_repo_path(cfg)
    cps: dict[str, list[Path]] = {}
    cp_gavs: dict[str, dict[str, str]] = {}          # configured g:a → {g:a → g:a:v} on its classpath
    gav_of: dict[Path, str] = {}
    for ga in wanted:
        jars = list(resolve(ga, versions.get(ga, "")) or [])
        cps[ga] = jars
        report.classpaths[ga] = [j.name for j in jars]
        for j in jars:
            gav_of.setdefault(j, _gav(j, repo) or j.name)
        cp_gavs[ga] = {_ga(gav_of[j]): gav_of[j] for j in jars}
        if not jars:
            where = f" (log: {report.log_paths[ga]})" if ga in report.log_paths else ""
            head.append(f"recipe artifact {ga}:{versions.get(ga) or '(bom)'}: dependency resolution failed, "
                        f"no classpath{where}")
    union = list(dict.fromkeys(j for jars in cps.values() for j in jars))
    all_gavs = {_ga(gav_of[j]): gav_of[j] for j in union}
    for e in entries:                                # transitive catalog artifacts (rewrite-java-dependencies …)
        gav = all_gavs.get(e.artifact or "", "")
        if gav.count(":") >= 2:
            report.artifact_versions.setdefault(_ga(gav), gav.split(":")[2])

    for e in entries:
        row = JarCheckRow(id=e.id, origin=e.origin, artifact=e.artifact, provided_by=e.provided_by)
        core = not e.artifact or e.artifact.startswith(CORE_GROUP)
        if core:
            owner = None
        elif e.artifact in cps:
            owner = e.artifact
        elif e.provided_by:
            owner = e.provided_by
        else:
            owner = None
            row.problems.append(f"catalog {e.id}: artifact {e.artifact} is not in openrewrite.recipe_artifacts "
                                f"and has no provided_by")
        search = cps.get(owner, []) if owner else []
        declaring = [j for j in dict.fromkeys(search + union) if scan_jar(j).declares(e.id)]
        if e.artifact and not core:
            gav = cp_gavs.get(owner or "", {}).get(e.artifact) or all_gavs.get(e.artifact) or ""
            row.version = gav.split(":")[2] if gav.count(":") >= 2 else versions.get(e.artifact, "")
            preferred = [j for j in declaring if _ga(gav_of[j]) == e.artifact]
            if owner and cps.get(owner):
                row.artifact_on_classpath = e.artifact in cp_gavs[owner]
        else:
            preferred = [j for j in declaring if gav_of[j].startswith(CORE_GROUP)]
        hit = (preferred or declaring or [None])[0]
        row.found_in = gav_of[hit] if hit is not None else ""

        if hit is None:
            if union:
                row.problems.append(f"catalog {e.id}: recipe id not found in any resolved jar "
                                    f"({len(union)} jar(s) searched)")
        elif e.artifact and not core and not preferred:
            row.problems.append(f"catalog {e.id}: artifact {e.artifact} does not declare it; "
                                f"found in {row.found_in}")
        elif core and not preferred:
            row.problems.append(f"catalog {e.id}: artifact is null (core recipe) but it is declared in "
                                f"{row.found_in}, not in an {CORE_GROUP}* jar")
        if row.artifact_on_classpath is False and owner != e.provided_by:
            row.problems.append(f"catalog {e.id}: artifact {e.artifact} is not on the classpath of {owner}")

        if e.provided_by:
            pb_ok = True
            if e.provided_by not in configured:
                pb_ok = False
                row.problems.append(f"catalog {e.id}: provided_by {e.provided_by} is not in "
                                    f"openrewrite.recipe_artifacts")
            if cps.get(e.provided_by):
                if e.artifact not in cp_gavs[e.provided_by]:
                    pb_ok = False
                    row.problems.append(
                        f"catalog {e.id}: artifact {e.artifact} is not on the transitive classpath of provided_by "
                        f"{e.provided_by}:{versions.get(e.provided_by) or '?'}")
                row.provided_by_ok = pb_ok
            else:
                row.provided_by_ok = False if not pb_ok else None
        report.rows.append(row)

    report.problems = head + [p for r in report.rows for p in r.problems]
    return report
