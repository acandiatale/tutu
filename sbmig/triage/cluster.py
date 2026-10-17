"""Error clustering (ARCHITECTURE §4).

Clusters are keyed by
* ``(kind, file)``          for COMPILE / TEST_COMPILE (falls back to the module when the file is unknown)
* ``(kind, artifact.key)``  for DEPENDENCY / PLUGIN / PARENT resolution and POM_INVALID with an artifact
* ``(kind, module)``        otherwise

Priorities (lower = handled first): PARENT_RESOLUTION 10, POM_INVALID 15, DEPENDENCY_RESOLUTION 20,
PLUGIN_RESOLUTION 25, ENFORCER 30, REWRITE 35, COMPILE 50 + reactor index, TEST_COMPILE 80 + reactor index,
PLUGIN_EXECUTION 90, TEST_FAILURE 95, UNKNOWN 99. Unknown modules get +40. Ties: error count desc, then id.
"""
from __future__ import annotations

import hashlib
import posixpath
from typing import Optional

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, ProjectProfile

BASE_PRIORITY: dict[ErrorKind, int] = {
    ErrorKind.PARENT_RESOLUTION: 10,
    ErrorKind.POM_INVALID: 15,
    ErrorKind.DEPENDENCY_RESOLUTION: 20,
    ErrorKind.PLUGIN_RESOLUTION: 25,
    ErrorKind.ENFORCER: 30,
    ErrorKind.REWRITE: 35,
    ErrorKind.COMPILE: 50,
    ErrorKind.TEST_COMPILE: 80,
    ErrorKind.PLUGIN_EXECUTION: 90,
    ErrorKind.TEST_FAILURE: 95,
    ErrorKind.UNKNOWN: 99,
}
UNKNOWN_MODULE_OFFSET = 40

ARTIFACT_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION,
                            ErrorKind.PARENT_RESOLUTION})
FILE_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
# resolution-type clusters mask compile clusters of the same module (they usually vanish once the pom is fixed)
RESOLUTION_KINDS = frozenset({ErrorKind.PARENT_RESOLUTION, ErrorKind.POM_INVALID,
                              ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION})


def cluster_key(e: BuildError) -> str:
    if e.kind in FILE_KINDS:
        return e.file or f"module:{e.module or '-'}"
    if e.kind in ARTIFACT_KINDS or (e.kind == ErrorKind.POM_INVALID and e.artifact is not None):
        if e.artifact is not None:
            return e.artifact.key
    return e.module or e.file or "-"


def cluster_id(kind: ErrorKind, key: str) -> str:
    return f"{kind.value}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def module_index(profile: Optional[ProjectProfile], module: Optional[str], file: Optional[str] = None) -> Optional[int]:
    """Reactor index of a module: position in `profile.poms` (analyzer keeps reactor order)."""
    if profile is None or not profile.poms:
        return None
    if module:
        for i, p in enumerate(profile.poms):
            d = posixpath.dirname(p.path)
            if module in (p.artifact_id, d, posixpath.basename(d)):
                return i
    if file:
        best: Optional[tuple[int, int]] = None      # (dir length, index): deepest pom dir containing the file
        for i, p in enumerate(profile.poms):
            d = posixpath.dirname(p.path)
            if d == "" or file == d or file.startswith(d + "/"):
                if best is None or len(d) > best[0]:
                    best = (len(d), i)
        if best is not None:
            return best[1]
    return None


def priority_for(kind: ErrorKind, profile: Optional[ProjectProfile], module: Optional[str],
                 file: Optional[str]) -> int:
    base = BASE_PRIORITY.get(kind, 99)
    if kind in FILE_KINDS:
        idx = module_index(profile, module, file)
        return base + (idx if idx is not None else UNKNOWN_MODULE_OFFSET)
    return base


def cluster_errors(errors: list[BuildError], profile: Optional[ProjectProfile] = None) -> list[ErrorCluster]:
    groups: dict[tuple[ErrorKind, str], list[BuildError]] = {}
    for e in errors:
        groups.setdefault((e.kind, cluster_key(e)), []).append(e)
    out: list[ErrorCluster] = []
    for (kind, key), errs in groups.items():
        first = errs[0]
        sigs: list[str] = []
        for e in errs:
            s = e.signature or f"{e.kind.value}|{e.message}"
            if s not in sigs:
                sigs.append(s)
        file = first.file if kind in FILE_KINDS else (first.file if all(x.file == first.file for x in errs) else None)
        module = next((e.module for e in errs if e.module), None)
        out.append(ErrorCluster(id=cluster_id(kind, key), kind=kind, file=file, module=module, errors=errs,
                                signatures=sigs, priority=priority_for(kind, profile, module, first.file)))
    out.sort(key=lambda c: (c.priority, -len(c.errors), c.id))
    return out


def cluster_label(c: ErrorCluster) -> str:
    """Short human label: 'compile src/main/java/Foo.java (3)'."""
    where = c.file or (c.errors[0].artifact.key if c.errors and c.errors[0].artifact else None) or c.module or "-"
    return f"{c.kind.value} {where} ({len(c.errors)})"
