"""Error clustering (ARCHITECTURE §4).

v2 (flow-v2, docs/INTERFACES.md "Script fix loop (L)"): the fix loop works on **cases** — `cluster_cases` groups the
errors of a build by their L2 signature (`signature_generic`) ACROSS files, so one fix script fixes the same cause in
every file (v1 asked the LLM once per file). The case id is the case catalog's: ``sha1(signature_generic)[:10]``
(`sbmig.kb.catalog.case_id_for`), so ids match ``kb/catalog`` and ``kb/scripts/<case_id>/``; an error without an L2
gets ``sha1(kind|file|L1)[:10]``. A case cluster has ``id = "case:<case_id>"``, ``case_id``, ``file = None`` and the
common module (None when the case spans modules).

v1 clusters (`cluster_errors`, still used by callers outside the fix loop) are keyed by
* ``(kind, file)``          for COMPILE / TEST_COMPILE (falls back to the module when the file is unknown)
* ``(kind, artifact.key)``  for DEPENDENCY / PLUGIN / PARENT resolution and POM_INVALID with an artifact
* ``(kind, module)``        otherwise

Priorities (both, lower = handled first): PARENT_RESOLUTION 10, POM_INVALID 15, DEPENDENCY_RESOLUTION 20,
PLUGIN_RESOLUTION 25, ENFORCER 30, REWRITE 35, COMPILE 50 + reactor index, TEST_COMPILE 80 + reactor index,
PLUGIN_EXECUTION 90, TEST_FAILURE 95, UNKNOWN 99. Unknown modules get +40; a case takes the lowest priority of its
errors (its earliest reactor module). v1 ties: error count desc, then id.

v2 cases of the same priority are ordered **root causes first** by their javac error class (`CAUSE_RANK`, below):
a missing package / class file before the symbols it hides, an import before its body usages, the symbols before
the type mismatches and overrides they cause (2026-10-26 probe: one missing `jakarta.xml.bind-api` gave 10 cases in
2 files; the fix loop defers the consequences — `BuildFixLoop._deferral` — so the cause's script is asked for first).
Then error count (desc), then id.
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
    """Short human label: 'compile src/main/java/Foo.java (3)'; a case: 'compile case abc1234567
    cannot_find_symbol|clientIp (7 in 4 files)'."""
    if c.case_id:
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        sig = sig.split("|", 1)[1] if "|" in sig else sig
        return (f"{c.kind.value} case {c.case_id}{' ' + sig[:80] if sig else ''} "
                f"({len(c.errors)} in {len(case_files(c))} file(s))")
    where = c.file or (c.errors[0].artifact.key if c.errors and c.errors[0].artifact else None) or c.module or "-"
    return f"{c.kind.value} {where} ({len(c.errors)})"


# ---------------------------------------------------------------------------------------------------------------------
# v2: cases (one L2 signature across files)
# ---------------------------------------------------------------------------------------------------------------------
CASE_PREFIX = "case:"


def _posix(path: Optional[str]) -> str:
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def l2_case_id(signature_generic: Optional[str]) -> str:
    """The case catalog's id of an L2 signature (``sha1(signature_generic)[:10]``; "" without one) — the function of
    `sbmig.kb.catalog` when it imports, the same formula otherwise (ids must match kb/catalog and kb/scripts)."""
    if not signature_generic:
        return ""
    try:
        from sbmig.kb.catalog import case_id_for
        return case_id_for(signature_generic)
    except Exception:  # pragma: no cover - the KB package is ours; the formula is the contract
        return hashlib.sha1(signature_generic.encode("utf-8")).hexdigest()[:10]


def case_id_of(e: BuildError) -> str:
    """The case of one error: its L2 id, or — no L2 — ``sha1(kind|file|L1)[:10]`` (file = posix path, '' for none;
    L1 = the signature, else ``kind|message``)."""
    cid = l2_case_id(e.signature_generic)
    if cid:
        return cid
    key = "|".join((e.kind.value, _posix(e.file), e.signature or f"{e.kind.value}|{e.message}"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def case_files(c: ErrorCluster) -> list[str]:
    """The distinct files of a cluster's errors (posix, in error order)."""
    return list(dict.fromkeys(_posix(e.file) for e in c.errors if e.file))


# v2 root-cause order of compile cases (lower = earlier; the error class is the 2nd segment of the L2 signature,
# `sbmig.build.signatures.classify_javac` for an error without one). A `cannot_find_symbol` whose location is a
# package is an import line (`import a.b.Gone;`): it goes before the body usages of the same symbol.
CAUSE_RANK: dict[str, int] = {
    "package_does_not_exist": 0,
    "cannot_access": 1, "class_not_found_in_package": 1,
    "cannot_find_symbol@import": 2, "static_import_only": 2,
    "cannot_find_symbol": 3,
    "incompatible_types": 4, "cannot_be_applied": 4, "no_suitable_method": 4,
    "method_does_not_override": 5, "does_not_override_abstract": 5, "not_abstract_and_does_not_override": 5,
}
OTHER_CAUSE_RANK = 6                   # everything else (illegal_start, has_private_access, deprecated_removed, …)


def error_class(e: BuildError) -> str:
    """The javac error class of a compile error (`cannot_find_symbol`, …; `cannot_find_symbol@import` for an import
    line); "" for other kinds."""
    if e.kind not in FILE_KINDS:
        return ""
    parts = (e.signature_generic or "").split("|")
    cls = parts[1] if len(parts) > 1 and parts[0] == e.kind.value else ""
    if not cls:
        try:
            from sbmig.build.signatures import classify_javac
            cls = classify_javac(e.message or "")
        except Exception:  # pragma: no cover - the build package is ours
            cls = ""
    if cls == "cannot_find_symbol" and (e.location or "").strip().startswith("package "):
        return "cannot_find_symbol@import"
    return cls


def cause_rank(c: ErrorCluster) -> int:
    """Root-cause rank of a case (`CAUSE_RANK`; its most causal error). 0 for non-compile kinds — their priority
    (`BASE_PRIORITY`) already puts them first."""
    if c.kind not in FILE_KINDS:
        return 0
    return min((CAUSE_RANK.get(error_class(e), OTHER_CAUSE_RANK) for e in c.errors), default=OTHER_CAUSE_RANK)


def cluster_cases(errors: list[BuildError], profile: Optional[ProjectProfile] = None,
                  scope: Optional[str] = None) -> list[ErrorCluster]:
    """v2 clusters: one per case (L2) across files. `scope` (a targeted loop's build target, MSA services) is appended
    to the id (``case:<id>@<scope>``) so two services' attempts on the same case never count against each other —
    `case_id` stays the plain case id (the script store key)."""
    groups: dict[str, list[BuildError]] = {}
    for e in errors:
        groups.setdefault(case_id_of(e), []).append(e)
    suffix = f"@{_posix(scope).strip('/')}" if scope and _posix(scope).strip("/") else ""
    out: list[ErrorCluster] = []
    for cid, errs in groups.items():
        first = errs[0]
        sigs: list[str] = []
        for e in errs:
            s = e.signature or f"{e.kind.value}|{e.message}"
            if s not in sigs:
                sigs.append(s)
        modules = list(dict.fromkeys(e.module for e in errs if e.module))
        module = modules[0] if len(modules) == 1 and all(e.module for e in errs) else None
        prio = min(priority_for(e.kind, profile, e.module, e.file) for e in errs)
        out.append(ErrorCluster(id=f"{CASE_PREFIX}{cid}{suffix}", kind=first.kind, file=None, module=module,
                                errors=errs, signatures=sigs, priority=prio, case_id=cid))
    out.sort(key=lambda c: (c.priority, cause_rank(c), -len(c.errors), c.id))     # root causes first
    return out
