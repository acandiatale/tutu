"""Resolve recipe artifact versions (``g:a`` → version) from ``rewrite-recipe-bom`` (or config overrides).

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
    """Recipe artifact versions could not be determined (BOM unavailable and no override)."""


def pom_path(local_repo: Path, group: str, artifact: str, version: str) -> Path:
    return Path(local_repo) / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.pom"


def bom_pom_path(cfg: Config, version: str | None = None) -> Path:
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
    """Managed versions of a BOM pom file: {"g:a": "version"}; imported BOMs merged when present locally."""
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
    """Make sure the recipe BOM pom is in the local repository; returns its path or raises RecipeArtifactError.

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
    """{"g:a": version} for ``artifacts`` (default: ``cfg.openrewrite.recipe_artifacts``).

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
