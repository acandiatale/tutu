"""이관이 끝난 프로젝트에서 권고 라이브러리를 다시 찾아, 권고마다 교체됐는지 결과를 정하는 모듈.

Finalize calls ``verify_advisories`` after the final build. The Handover picks its sentence by the outcome:
replaced asks for a check, still-present is a follow-up for the owning team. A failed re-scan leaves unknown
and never raises.

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
    """권고 항목을 id별 dict로 반환한다.
    권고 파일을 읽지 못하면 빈 dict다. 그때는 권고 결과에 담긴 정보만으로 확인한다."""
    try:
        from .catalog import load_advisories

        return {e.id: e for e in load_advisories(cfg).entries}
    except Exception as e:  # pragma: no cover - 권고 결과에 담긴 정보로만 검증한다
        log.info("advisory catalog unavailable for verification: %s", e)
        return {}


def _verify_one(f: AdvisoryFinding, libs: dict[str, LibraryUsage], gavs: list,
                entry: Optional[LibraryAdvisory]) -> None:
    """권고 하나의 결과를 정하고 근거를 notes에 남긴다.
    라이브러리와 아티팩트가 더 이상 없으면 replaced다. 남아 있어도 ``resolved_min_version`` 이상이면 replaced다.
    그 밖에는 still-present다."""
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
    """이관된 프로젝트(``view``: Workspace 또는 PathView)를 다시 검사해 모든 권고의 ``outcome``을 정한다.
    받은 리스트를 직접 고쳐서 그대로 반환한다. 예외를 던지지 않는다. 검사에 실패하면 모두 unknown이다."""
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
        except Exception as e:  # pragma: no cover - 권고 하나의 실패가 나머지를 막지 않게 한다
            f.outcome = "unknown"
            f.notes.append(f"verify: {type(e).__name__}: {e}")
    return findings
