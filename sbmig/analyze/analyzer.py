"""`analyze_project(ws, cfg) -> ProjectProfile`: pom graph + source scan + libraries + classification."""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

from sbmig.core.config import ArtifactSpec, Config
from sbmig.core.models import GAV, CaseType, PomInfo, ProjectProfile

from .classifier import build_services, classify, msa_deployables, service_roots
from .libraries import detect_libraries
from .pom_parser import WorkspaceView, module_order, parse_poms
from .source_scanner import scan_sources

log = logging.getLogger("sbmig.analyze")


class PathView:
    """Read-only Workspace-like view over a directory (no copy, no git, no run dir)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def abs(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def rel(self, path: str | Path) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix() if p.is_absolute() else p.as_posix()
        except ValueError:
            return str(p)

    def exists(self, rel: str | Path) -> bool:
        return self.abs(rel).exists()

    def read(self, rel: str | Path, encoding: str = "utf-8") -> str:
        return self.abs(rel).read_text(encoding=encoding, errors="replace")

    def glob(self, pattern: str) -> list[str]:
        return sorted(self.rel(p) for p in self.root.rglob(pattern)
                      if ".git" not in p.relative_to(self.root).parts
                      and "target" not in p.relative_to(self.root).parts)

    def java_files(self) -> list[str]:
        return self.glob("*.java")

    def pom_files(self) -> list[str]:
        return self.glob("pom.xml")


def _artifact_hits(poms: list[PomInfo], keys: dict[str, ArtifactSpec]) -> list[GAV]:
    out: dict[str, GAV] = {}
    for p in poms:
        for d in [*p.dependencies, *p.dependency_management]:
            if d.key in keys and (d.key not in out or (not out[d.key].version and d.version)):
                out[d.key] = GAV(group_id=d.group_id, artifact_id=d.artifact_id, version=d.version,
                                 scope=d.scope, type=d.type, classifier=d.classifier)
    return [out[k] for k in sorted(out)]


def _mandatory_keys(cfg: Config) -> dict[str, ArtifactSpec]:
    out: dict[str, ArtifactSpec] = {}
    for spec in cfg.artifacts.mandatory:
        out[spec.key] = spec
        for alias in spec.aliases:
            out[alias] = spec
    return out


def _pick(values: list[Optional[str]]) -> Optional[str]:
    vals = [v for v in values if v]
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]


def analyze_project(ws: WorkspaceView, cfg: Config) -> ProjectProfile:
    root = Path(ws.root)
    poms, warnings = parse_poms(ws)
    order = module_order(poms)
    by_path = {p.path: p for p in poms}
    poms = [by_path[p] for p in order]

    scan = scan_sources(ws)
    source = scan.stats()
    libraries = detect_libraries(poms, scan)

    # build tool
    build_tool = "maven"
    if not poms:
        if ws.glob("build.gradle") or ws.glob("build.gradle.kts"):
            build_tool = "gradle"
            warnings.append("Gradle build detected and no pom.xml: only Maven projects are supported")
        else:
            build_tool = "none"
            warnings.append("no pom.xml found (no supported build tool)")
    if ws.glob("build.xml") and not poms:
        warnings.append("Ant build.xml detected: only Maven projects are supported")
    elif ws.glob("build.xml"):
        warnings.append("Ant build.xml present next to Maven poms: it is ignored")
    if source.java_files == 0:
        warnings.append("no java sources found")
    if source.kotlin_files:
        warnings.append(f"{source.kotlin_files} Kotlin file(s) present: not migrated by sbmig")

    case, confidence, reasons = classify(poms, source, libraries, scan)
    services = []
    if case == CaseType.MSA:
        deployables, _ = msa_deployables(poms, scan)
        services = build_services(service_roots(deployables, poms), poms, scan)

    root_pom = by_path.get("pom.xml")
    if root_pom is None:
        roots = [p for p in poms if p.is_root]
        root_pom = roots[0] if len(roots) == 1 else None
    name = root_pom.artifact_id if root_pom else root.name

    java_version = (root_pom.java_version if root_pom else None) or _pick([p.java_version for p in poms])
    boot_version = (root_pom.spring_boot_version if root_pom else None) or next(
        (p.spring_boot_version for p in poms if p.spring_boot_version), None)
    spring_version = (root_pom.spring_version if root_pom else None) or next(
        (p.spring_version for p in poms if p.spring_version), None)

    profile = ProjectProfile(
        root=str(root),
        name=name,
        case_type=case,
        confidence=confidence,
        reasons=reasons,
        build_tool=build_tool,  # type: ignore[arg-type]
        poms=poms,
        root_pom=root_pom,
        source=source,
        libraries=libraries,
        java_version=java_version,
        spring_boot_version=boot_version,
        spring_version=spring_version,
        packaging=root_pom.packaging if root_pom else None,
        is_multi_module=len(poms) > 1,
        services=services,
        optional_artifacts_in_use=_artifact_hits(poms, cfg.optional_keys()),
        mandatory_already_present=_artifact_hits(poms, _mandatory_keys(cfg)),
        warnings=warnings,
        fingerprint=ProjectProfile.make_fingerprint([p.path for p in poms],
                                                    root_pom.artifact_id if root_pom else root.name),
    )
    log.info("analyzed %s: case=%s confidence=%.2f poms=%d java=%d", name, case.value, confidence,
             len(poms), source.java_files)
    return profile


def analyze_path(path: str | Path, cfg: Config) -> ProjectProfile:
    """Analyze a directory in place (no copy into a run workspace)."""
    return analyze_project(PathView(path), cfg)
