"""레시피 아티팩트 버전 결정(``g:a`` → 버전): 레시피 BOM(``rewrite-recipe-bom``, 버전
``openrewrite.recipe_bom_version``)의 관리 버전, 또는 설정의 개별 지정. 흐름상 위치: 변환 단계에서
``OpenRewriteRunner._run_once`` 가 필요한 레시피 아티팩트(rewrite-migrate-java, rewrite-spring …)의 버전을
여기서 받아 ``-Drewrite.recipeArtifactCoordinates`` 로 넘긴다. 결과는 ``logs/rewrite-artifact-versions.json``
에 캐시된다. 방식: BOM pom 이 로컬 저장소에 없으면 ``dependency:get`` 으로 받고 ``<dependencyManagement>`` 를
직접 읽는다(``${…}`` 는 그 pom 의 ``<properties>`` 로; import 된 BOM 은 로컬에 있을 때만 낮은 우선순위로
합친다). 결정: BOM 은 3.37.0 고정(불변조건 5) — 3.38.0 은 Central 에 아직 없는 rewrite-migrate-java 3.43.0 /
rewrite-spring 6.38.0 을 가리켜 모든 실행이 아티팩트 해결에서
실패한다. 3.37.0 은 rewrite-spring 6.37.0 으로 맞물린다.

Resolve recipe artifact versions (``g:a`` → version) from ``rewrite-recipe-bom`` (or config overrides).

The BOM pom is fetched into the local repository with ``maven-dependency-plugin:get`` when missing and parsed
directly (``<dependencyManagement>``; ``${...}`` interpolated from its ``<properties>``). Imported BOMs are merged
(lower precedence) when they are available locally.


"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path

from sbmig.build.mvn import base_command, local_repo_path
from sbmig.core.config import Config

log = logging.getLogger("sbmig.rewrite")

BOM_GROUP = "org.openrewrite.recipe"
BOM_ARTIFACT = "rewrite-recipe-bom"
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"


class RecipeArtifactError(RuntimeError):
    """레시피 아티팩트 버전을 정할 수 없음(BOM 을 못 받았고 개별 지정도 없음). 러너가 받아 변환
    실패(ok=False)로 기록하고, 실행은 계속된다(빌드가 진짜 문제를 보여 준다).

    Recipe artifact versions could not be determined (BOM unavailable and no override)."""


def pom_path(local_repo: Path, group: str, artifact: str, version: str) -> Path:
    """로컬 저장소 안의 pom 경로 ``<repo>/g/r/o/u/p/artifact/version/artifact-version.pom``."""
    return Path(local_repo) / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.pom"


def bom_pom_path(cfg: Config, version: str | None = None) -> Path:
    """레시피 BOM pom 의 로컬 저장소 경로(버전 기본값 = ``openrewrite.recipe_bom_version``)."""
    return pom_path(local_repo_path(cfg), BOM_GROUP, BOM_ARTIFACT, version or cfg.openrewrite.recipe_bom_version)


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _interp(value: str, props: Mapping[str, str]) -> str:
    for _ in range(5):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def parse_bom_versions(path: Path, *, local_repo: Path | None = None, _depth: int = 0) -> dict[str, str]:
    """BOM pom 의 관리 버전 {"g:a": 버전}. import 된 BOM 은 로컬에 있으면 3단계까지
    따라가 합친다(직접 선언이 이김).

    Managed versions of a BOM pom file: {"g:a": "version"}; imported BOMs merged when present locally."""
    root = _strip_ns(ET.parse(str(path)).getroot())
    props: dict[str, str] = {}
    for tag in ("version", "groupId", "artifactId"):
        el = root.find(tag)
        if el is None and root.find("parent") is not None:
            el = root.find(f"parent/{tag}")
        if el is not None and el.text:
            props[f"project.{tag}"] = el.text.strip()
            props[f"pom.{tag}"] = el.text.strip()
    pe = root.find("properties")
    if pe is not None:
        for child in pe:
            props[str(child.tag)] = (child.text or "").strip()
    direct: dict[str, str] = {}
    imported: dict[str, str] = {}
    for dep in root.findall("dependencyManagement/dependencies/dependency"):
        g = _interp((dep.findtext("groupId") or "").strip(), props)
        a = _interp((dep.findtext("artifactId") or "").strip(), props)
        v = _interp((dep.findtext("version") or "").strip(), props)
        if not g or not a or not v:
            continue
        if (dep.findtext("scope") or "").strip() == "import" and (dep.findtext("type") or "").strip() == "pom":
            if local_repo is not None and _depth < 3:
                p = pom_path(local_repo, g, a, v)
                if p.is_file():
                    for k, val in parse_bom_versions(p, local_repo=local_repo, _depth=_depth + 1).items():
                        imported.setdefault(k, val)
                else:
                    log.debug("imported BOM %s:%s:%s not in local repo; skipped", g, a, v)
            continue
        direct[f"{g}:{a}"] = v
    return {**imported, **direct}


def ensure_bom(cfg: Config, mvn_env: Mapping[str, str], *, work_dir: Path | None = None,
               version: str | None = None, timeout_s: float = 300) -> Path:
    """레시피 BOM pom 이 로컬 저장소에 있게 하고 그 경로를 돌려준다(없으면 ``dependency:get``, 끝내 없으면
    RecipeArtifactError). 판단 기준은 종료 코드가 아니라 파일이다. 프로젝트 밖(``work_dir``)에서 돌린다 — 깨진
    pom.xml 이 이 명령까지 실패시키지 않게.

    Make sure the recipe BOM pom is in the local repository; returns its path or raises RecipeArtifactError.

    ``dependency:get`` may exit non-zero although the BOM pom itself was downloaded (e.g. one of its *imported*
    BOMs is missing upstream) — the file's presence is what counts.
    
    
    """
    version = version or cfg.openrewrite.recipe_bom_version
    path = bom_pom_path(cfg, version)
    if path.is_file():
        return path
    cmd = base_command(cfg) + [f"{DEPENDENCY_PLUGIN}:get",
                               f"-Dartifact={BOM_GROUP}:{BOM_ARTIFACT}:{version}:pom", "-Dtransitive=false"]
    cwd = Path(work_dir) if work_dir else Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)                 # never inside the project: a broken pom.xml would fail it
    log.info("fetching %s:%s:%s", BOM_GROUP, BOM_ARTIFACT, version)
    try:
        cp = subprocess.run(cmd, cwd=str(cwd), env=dict(mvn_env), capture_output=True, timeout=timeout_s,
                            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")   # never the console code page
        output = cp.stdout + cp.stderr
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
        output = str(exc)
    if path.is_file():
        return path
    tail = "\n".join(output.strip().splitlines()[-8:])
    raise RecipeArtifactError(f"could not fetch {BOM_GROUP}:{BOM_ARTIFACT}:{version} into {path.parent}:\n{tail}")


def resolve_recipe_artifact_versions(cfg: Config, mvn_env: Mapping[str, str], *,
                                     artifacts: Iterable[str] | None = None,
                                     cache_path: Path | None = None,
                                     work_dir: Path | None = None) -> dict[str, str]:
    """``artifacts``(기본 ``openrewrite.recipe_artifacts``)의 {"g:a": 버전}. 우선순위:
    ``openrewrite.recipe_artifact_versions`` 개별 지정 → BOM 관리 버전. BOM 에서 읽은 버전은 ``cache_path`` 에
    BOM 버전과 함께 캐시(개별 지정은 캐시 전에 적용되므로 키에
    들어가지 않음). 버전이 없는 아티팩트가 있으면 RecipeArtifactError.

    {"g:a": version} for ``artifacts`` (default: ``cfg.openrewrite.recipe_artifacts``).

    Precedence: ``cfg.openrewrite.recipe_artifact_versions`` override → BOM-managed version. Results are cached in
    ``cache_path`` (keyed by BOM version + overrides). Raises RecipeArtifactError if an artifact has no version.
    
    
    """
    wanted = list(dict.fromkeys(artifacts if artifacts is not None else cfg.openrewrite.recipe_artifacts))
    overrides = dict(cfg.openrewrite.recipe_artifact_versions or {})
    bom_version = cfg.openrewrite.recipe_bom_version
    out: dict[str, str] = {a: overrides[a] for a in wanted if a in overrides}
    todo = [a for a in wanted if a not in out]
    if not todo:
        return out

    cache: dict = {}
    if cache_path is not None and Path(cache_path).is_file():
        try:
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    managed: dict[str, str] = {}
    if cache.get("bom_version") == bom_version and all(a in cache.get("versions", {}) for a in todo):
        managed = cache["versions"]
    else:
        path = ensure_bom(cfg, mvn_env, work_dir=work_dir)
        managed = parse_bom_versions(path, local_repo=local_repo_path(cfg))
        if cache_path is not None:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cache_path).write_text(json.dumps({"bom_version": bom_version, "bom_pom": str(path),
                                                    "versions": managed}, indent=2, sort_keys=True),
                                        encoding="utf-8")
    missing = [a for a in todo if a not in managed]
    if missing:
        raise RecipeArtifactError(f"{', '.join(missing)} not managed by {BOM_ARTIFACT}:{bom_version}; "
                                  "set openrewrite.recipe_artifact_versions")
    for a in todo:
        out[a] = managed[a]
    return {a: out[a] for a in wanted}
