"""Advisory evaluation (after analyze), planner hook (recipe refs) and the handover follow-up sentences.

* `evaluate_advisories(profile, cfg)` — which advisories match this project, with the effective policy
  (config override > catalog; `allow_recipes=false` and unavailable recipes downgrade to guide).
* `advisory_recipe_refs(findings, cfg, planned_ids)` — the planner asks which advisory recipes are not already
  part of the planned transform.rewrite recipes (directly or through a composite's `covers`).
* `followups_for(findings)` — one "from here on, your team …" sentence per finding, not-replaced first.
"""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import GAV, AdvisoryFinding, LibraryAdvisory, ProjectProfile, RecipeRef

from .catalog import AdvisoryCatalog, load_advisories, recipe_unavailable

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
    from sbmig.triage.handlers.base import version_lt

    r = version_lt(version, bound)
    return True if r is None else bool(r)


def _recipe_catalog(cfg: Optional[Config], catalog: Any = None) -> Any:
    if catalog is not None and hasattr(catalog, "covered_by"):
        return catalog
    from sbmig.plan.catalog import default_catalog

    return default_catalog(cfg)


def _sort_key(f: AdvisoryFinding) -> tuple[int, str]:
    return STATUS_ORDER.get(f.status, 9), f.advisory_id


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------
def _resolve_policy(entry: LibraryAdvisory, cfg: Config, rcat: Any) -> Optional[tuple[str, str]]:
    """(policy, reason) or None when the entry is switched off by config."""
    override = (cfg.advisories.policy_overrides or {}).get(entry.id)
    if override == "off":
        return None
    policy, reason = (override, "config override") if override else (entry.policy, "catalog")
    if policy == "recipe":
        if not cfg.advisories.allow_recipes:
            return "guide", "advisories.allow_recipes=false"
        if not entry.recipes:
            return "guide", "no recipes listed → guide"
        for rid in entry.recipes:
            why = recipe_unavailable(rid, cfg, rcat)
            if why:
                log.info("advisory %s: recipe %s %s → guide", entry.id, rid, why)
                return "guide", f"recipe {rid} unavailable → guide"
    return policy, reason


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
    """Advisory findings for this project (policies resolved; recipe_source "none", outcome "unknown")."""
    if not cfg.advisories.enabled:
        return []
    cat = catalog if catalog is not None else load_advisories(cfg)
    if not len(cat):
        return []
    rcat = _recipe_catalog(cfg)
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
        resolved = _resolve_policy(entry, cfg, rcat)
        if resolved is None:
            log.info("advisory %s switched off by advisories.policy_overrides", entry.id)
            continue
        policy, reason = resolved
        notes = [f"see {entry.notes}"] if entry.notes else []
        out[entry.id] = AdvisoryFinding(
            advisory_id=entry.id, library=m["library"], status=entry.status, title=entry.title,
            summary=entry.summary, policy=policy, policy_reason=reason, detected_version=m["version"],
            evidence=m["evidence"], artifacts=m["artifacts"],
            recipes=list(entry.recipes) if policy == "recipe" else [], recipe_source="none", outcome="unknown",
            replacement=entry.replacement, replacement_version=entry.replacement_version,
            followup=entry.followup.strip(), docs=list(entry.docs), notes=notes)
    return sorted(out.values(), key=_sort_key)


# ---------------------------------------------------------------------------
# planner hook
# ---------------------------------------------------------------------------
def _ref(recipe_id: str, cfg: Config, rcat: Any) -> RecipeRef:
    if recipe_id.startswith("com.company."):
        from sbmig.plan.planner import recipe_ref       # lazy: the planner imports this module lazily too

        return recipe_ref(recipe_id, cfg)
    return RecipeRef(id=recipe_id, source="builtin", artifact=rcat.artifact_for(recipe_id))


def _add_note(f: AdvisoryFinding, note: str) -> None:
    if note not in f.notes:
        f.notes.append(note)


def advisory_recipe_refs(findings: Optional[list[AdvisoryFinding]], cfg: Config, planned_ids: Iterable[str],
                         catalog: Any = None) -> list[RecipeRef]:
    """Refs for policy=="recipe" findings whose recipes are not already planned (directly or via a planned
    composite's `covers`). Mutates the findings: recipe_source plan | advisory | none, plus a note.
    `catalog` may be a RecipeCatalog (default: the configured one). Called once per transform.rewrite step;
    "advisory" is sticky across calls (MSA: one service adds it, another has it covered)."""
    if not findings:
        return []
    rcat = _recipe_catalog(cfg, catalog)
    planned = set(planned_ids)
    covered = rcat.covered_by(planned)
    out: list[RecipeRef] = []
    for f in findings:
        if f.policy != "recipe" or not f.recipes:
            if f.recipe_source != "advisory":
                f.recipe_source = "none"
            continue
        already = [r for r in f.recipes if r in planned or r in covered]
        to_add = [r for r in f.recipes if r not in already]
        for r in to_add:
            if not any(x.id == r for x in out):
                out.append(_ref(r, cfg, rcat))
        if to_add:
            f.recipe_source = "advisory"
            _add_note(f, "transform.rewrite: added " + ", ".join(to_add) + " (advisory recipe, runs before the umbrella)")
        elif f.recipe_source != "advisory":
            f.recipe_source = "plan"
        for r in already:
            via = "planned directly" if r in planned else f"included in {covered[r]}"
            _add_note(f, f"transform.rewrite: {r} already {via}")
    return out


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
        by = ", ".join(r.rsplit(".", 1)[-1] for r in f.recipes) if f.recipes else "the migration"
        text = f"{f.library} → {f.replacement or 'replacement'} applied by {by}"
        if f.followup:
            text += f"; {f.followup}"
        if text not in out:
            out.append(text)
    return out
