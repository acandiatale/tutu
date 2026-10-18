"""Finalize: re-detect the advised libraries on the FINAL tree and set each finding's outcome.

replaced      — library (and its as-is artifacts) no longer detected, or still detected but at/above the entry's
                `resolved_min_version` (release-train bumps)
still-present — still declared / imported; the owning team gets the follow-up
unknown       — re-scan impossible (any exception; a note says why)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import AdvisoryFinding, LibraryAdvisory, LibraryUsage

from .evaluate import artifact_hits, below, pom_gavs

log = logging.getLogger("sbmig.advisory")


def _entries(cfg: Config) -> dict[str, LibraryAdvisory]:
    try:
        from .catalog import load_advisories

        return {e.id: e for e in load_advisories(cfg).entries}
    except Exception as e:  # pragma: no cover - verification degrades to the finding's own data
        log.info("advisory catalog unavailable for verification: %s", e)
        return {}


def _verify_one(f: AdvisoryFinding, libs: dict[str, LibraryUsage], gavs: list,
                entry: Optional[LibraryAdvisory]) -> None:
    lib = libs.get(f.library)
    patterns = list(entry.artifacts) if entry is not None and entry.artifacts else []
    patterns += [k for k in f.artifacts if k not in patterns]
    hits = artifact_hits(patterns, gavs) if patterns else []
    if lib is None and not hits:
        f.outcome = "replaced"
        return
    version = lib.version if lib is not None and lib.version else None
    if version is None:
        version = next((g.version for g in hits if g.version and "${" not in g.version), None)
    floor = entry.resolved_min_version if entry is not None else None
    if floor and version and not below(version, floor):
        f.outcome = "replaced"
        note = f"verify: still declared at {version} (≥ {floor}) → replaced"
    else:
        f.outcome = "still-present"
        where = ", ".join(lib.evidence[:3]) if lib is not None else ", ".join(g.coordinates for g in hits[:3])
        note = f"verify: still present after the run ({where})"
    if note not in f.notes:
        f.notes.append(note)


def verify_advisories(findings: list[AdvisoryFinding], view: Any, cfg: Config) -> list[AdvisoryFinding]:
    """Set `outcome` on every finding from a re-scan of `view` (Workspace or PathView). Returns the same list
    (mutated). Never raises."""
    if not findings:
        return findings
    try:
        from sbmig.analyze.libraries import detect_libraries
        from sbmig.analyze.pom_parser import parse_poms
        from sbmig.analyze.source_scanner import scan_sources

        poms, _ = parse_poms(view)
        scan = scan_sources(view)
        libs = {u.name: u for u in detect_libraries(poms, scan)}
        gavs = pom_gavs(poms)
    except Exception as e:
        log.warning("advisory verification failed: %s", e)
        for f in findings:
            f.outcome = "unknown"
            note = f"verify: re-scan failed ({type(e).__name__}: {e})"
            if note not in f.notes:
                f.notes.append(note)
        return findings
    entries = _entries(cfg)
    for f in findings:
        try:
            _verify_one(f, libs, gavs, entries.get(f.advisory_id))
        except Exception as e:  # pragma: no cover - defensive, per finding
            f.outcome = "unknown"
            f.notes.append(f"verify: {type(e).__name__}: {e}")
    return findings
