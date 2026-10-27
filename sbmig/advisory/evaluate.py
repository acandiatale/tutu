"""Advisory evaluation (after analyze) and the handover follow-up sentences. Advisories are guide-only: they never
add a recipe; the fix loop gets their hints (`sbmig.advisory.hints`), the Handover their follow-ups.

* `evaluate_advisories(profile, cfg)` — which advisories match this project (`advisories.policy_overrides
  {id: off}` switches an entry off).
* `followups_for(findings)` — one "from here on, your team …" sentence per finding, not-replaced first.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import GAV, AdvisoryFinding, LibraryAdvisory, ProjectProfile

from .catalog import AdvisoryCatalog, load_advisories

log = logging.getLogger("sbmig.advisory")

STATUS_ORDER = {"eol": 0, "incompatible": 1, "unmanaged": 2, "superseded": 3}
_POM_EVIDENCE = ("pom:", "pom-managed:", "pom-parent:")


# ---------------------------------------------------------------------------
# matching helpers
# ---------------------------------------------------------------------------
def _concrete(v: Optional[str]) -> Optional[str]:
    return v if v and "${" not in v else None


def gav_matches(pattern: str, g: GAV) -> bool:
    pg, _, pa = pattern.partition(":")
    return fnmatch.fnmatchcase(g.group_id, pg) and fnmatch.fnmatchcase(g.artifact_id, pa or "*")


def pom_gavs(poms: Iterable[Any]) -> list[GAV]:
    """Dependencies, dependencyManagement entries and parents of every pom (in that order)."""
    deps: list[GAV] = []
    managed: list[GAV] = []
    parents: list[GAV] = []
    for p in poms:
        deps += list(p.dependencies)
        managed += list(p.dependency_management)
        if p.parent is not None:
            parents.append(p.parent)
    return deps + managed + parents


def artifact_hits(patterns: Iterable[str], gavs: list[GAV]) -> list[GAV]:
    pats = list(patterns)
    return [g for g in gavs if any(gav_matches(pt, g) for pt in pats)]


def _evidence_keys(evidence: Iterable[str]) -> list[str]:
    out: list[str] = []
    for ev in evidence:
        for pre in _POM_EVIDENCE:
            if ev.startswith(pre):
                parts = ev[len(pre):].split(":")
                if len(parts) >= 2:
                    k = f"{parts[0]}:{parts[1]}"
                    if k not in out:
                        out.append(k)
                break
    return out


def below(version: Optional[str], bound: str) -> bool:
    """True when `version` < `bound`; unknown / unparsable versions (release-train names) count as below."""
    from sbmig.plan.versions import version_lt

    r = version_lt(version, bound)
    return True if r is None else bool(r)


def _sort_key(f: AdvisoryFinding) -> tuple[int, str]:
    return STATUS_ORDER.get(f.status, 9), f.advisory_id


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------
def switched_off(entry: LibraryAdvisory, cfg: Config) -> bool:
    """True when `advisories.policy_overrides` sets the entry to "off" (any other value changes nothing)."""
    return (cfg.advisories.policy_overrides or {}).get(entry.id) == "off"


def _match(entry: LibraryAdvisory, profile: ProjectProfile, gavs: list[GAV]) -> Optional[dict[str, Any]]:
    lib = next((u for u in profile.libraries if entry.library and u.name == entry.library), None)
    hits = artifact_hits(entry.artifacts, gavs) if entry.artifacts else []
    if lib is None and not hits:
        return None
    evidence = list(lib.evidence) if lib is not None else []
    keys = _evidence_keys(evidence)
    for g in hits:
        ev = f"pom:{g.coordinates}"
        if ev not in evidence:
            evidence.append(ev)
        if g.key not in keys:
            keys.append(g.key)
    version = _concrete(lib.version) if lib is not None else None
    if version is None:
        version = next((_concrete(g.version) for g in hits if _concrete(g.version)), None)
    return {"library": entry.library if lib is not None else (entry.library or entry.id),
            "evidence": evidence, "artifacts": keys, "version": version}


def evaluate_advisories(profile: ProjectProfile, cfg: Config,
                        catalog: Optional[AdvisoryCatalog] = None) -> list[AdvisoryFinding]:
    """Advisory findings for this project (guide-only, outcome "unknown" until `verify_advisories`)."""
    if not cfg.advisories.enabled:
        return []
    cat = catalog if catalog is not None else load_advisories(cfg)
    if not len(cat):
        return []
    gavs = pom_gavs(profile.poms)
    out: dict[str, AdvisoryFinding] = {}
    for entry in cat.entries:
        if entry.id in out:
            continue
        if entry.applies_to and profile.case_type not in entry.applies_to:
            continue
        m = _match(entry, profile, gavs)
        if m is None:
            continue
        if entry.applies_when_version_below and not below(m["version"], entry.applies_when_version_below):
            log.debug("advisory %s: version %s ≥ %s; not applicable", entry.id, m["version"],
                      entry.applies_when_version_below)
            continue
        if switched_off(entry, cfg):
            log.info("advisory %s switched off by advisories.policy_overrides", entry.id)
            continue
        notes = [f"see {entry.notes}"] if entry.notes else []
        out[entry.id] = AdvisoryFinding(
            advisory_id=entry.id, library=m["library"], status=entry.status, title=entry.title,
            summary=entry.summary, policy="guide", detected_version=m["version"],
            evidence=m["evidence"], artifacts=m["artifacts"], outcome="unknown",
            replacement=entry.replacement, replacement_version=entry.replacement_version,
            followup=entry.followup.strip(), docs=list(entry.docs), notes=notes)
    return sorted(out.values(), key=_sort_key)


# ---------------------------------------------------------------------------
# follow-ups ("from here on, your team …")
# ---------------------------------------------------------------------------
def _replacement_hint(f: AdvisoryFinding) -> str:
    if not f.replacement:
        return ""
    return f" (replacement: {f.replacement}" + (f" {f.replacement_version}" if f.replacement_version else "") + ")"


def followups_for(findings: Optional[list[AdvisoryFinding]]) -> list[str]:
    """One actionable sentence per finding: not replaced (still-present / unknown) first, then replaced-but-verify."""
    out: list[str] = []
    items = list(findings or [])
    pending = [f for f in items if f.outcome != "replaced"]
    done = [f for f in items if f.outcome == "replaced"]
    for f in pending:
        text = f"{f.title}: {f.followup}".strip().rstrip(":") if f.followup else f.title
        if f.replacement and f.replacement.split()[0].rsplit(":", 1)[-1] not in text:
            text += _replacement_hint(f)
        if text not in out:
            out.append(text)
    for f in done:
        text = f"{f.library} → {f.replacement or 'replacement'} applied by the migration"
        if f.followup:
            text += f"; {f.followup}"
        if text not in out:
            out.append(text)
    return out
