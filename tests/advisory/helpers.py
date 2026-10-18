"""Small builders for advisory tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sbmig.core.models import GAV, CaseType, LibraryUsage, PomInfo, ProjectProfile


def write_advisories(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "advisories": entries}, sort_keys=False), encoding="utf-8")
    return path


def gav(coords: str) -> GAV:
    return GAV.parse(coords)


def profile(*, libs: tuple[LibraryUsage, ...] = (), deps: tuple[str, ...] = (), managed: tuple[str, ...] = (),
            parent: str | None = None, case: CaseType = CaseType.SPRING_BOOT) -> ProjectProfile:
    pom = PomInfo(path="pom.xml", artifact_id="p", is_root=True, dependencies=[gav(d) for d in deps],
                  dependency_management=[gav(m) for m in managed], parent=gav(parent) if parent else None)
    return ProjectProfile(root="/x", name="p", case_type=case, poms=[pom], root_pom=pom, libraries=list(libs),
                          fingerprint="f", spring_boot_version="2.7.18" if case == CaseType.SPRING_BOOT else None)
