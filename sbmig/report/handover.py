"""Handover: "here is what we replaced, from here on it is yours" — the first section of report.md.

`build_handover(profile, view, report, effective=None, cfg=None)` compares the as-is poms (`profile.poms`, plus
the as-is effective dependencyManagement captured at intake for versions the old parent managed) with the final
poms of the migrated tree (`view` = Workspace / PathView) and compresses the result for the owning team:
parent / java / spring boot before → after, one row per dependency whose version changed (⚠ carried-over pins:
the new parent / Boot BOM no longer manages them), added / removed artifacts, and an ordered "from here on, your
team" list (advisories not replaced, replaced-but-verify, carried-over pins, blocked / unresolved pointers,
manual follow-ups). Pure and defensive: never raises (→ `HandoverSummary(notes=["handover unavailable: …"])`).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sbmig.core.models import (AdvisoryFinding, GAV, HandoverSummary, PomInfo, ProjectProfile, RunReport,
                               VersionChange)

log = logging.getLogger("sbmig.report")

REASON_CARRIED = "carried over (no longer managed)"
REASON_PARENT = "parent swap"
REASON_OPTIONAL = "optional bump"
REASON_MANDATORY = "mandatory"
REASON_BOOT = "spring boot 4 upgrade"
REASON_OTHER = "recipe/handler"
CARRIED_LIST_MAX = 5

_PINNED_RE = re.compile(r"([\w.\-]+:[\w.\-]+) pinned to (\S+) \(")
_OPTIONAL_RE = re.compile(r"optional bumped: (.*)$")
_MANDATORY_RE = re.compile(r"mandatory added: ([^;]*)")

# key -> (version, source)
_Dep = tuple[Optional[str], str]


def _clean(v: Optional[str]) -> Optional[str]:
    if not v or "${" in v:
        return None
    return v.strip() or None


def _is_bom(g: GAV) -> bool:
    return (g.type or "") == "pom" and (g.scope or "") == "import"


def _own_keys(poms: list[PomInfo]) -> set[str]:
    return {f"{p.group_id}:{p.artifact_id}" for p in poms if p.group_id}


def _roots(poms: list[PomInfo]) -> list[PomInfo]:
    """Poms whose parent lives outside the repo (or that have none): the ones carrying the parent / BOM."""
    out = [p for p in poms if p.parent is not None and not p.parent_in_repo]
    return sorted(out, key=lambda p: (p.path.count("/"), p.path))


def _main_pom(poms: list[PomInfo]) -> Optional[PomInfo]:
    for p in poms:
        if p.path == "pom.xml":
            return p
    roots = [p for p in poms if p.is_root]
    return sorted(roots, key=lambda p: (p.path.count("/"), p.path))[0] if roots else (poms[0] if poms else None)


def _parents(poms: list[PomInfo]) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for p in _roots(poms):
        assert p.parent is not None
        out.setdefault(p.parent.key, _clean(p.parent.version))
    return out


def _fmt_parents(parents: dict[str, Optional[str]]) -> Optional[str]:
    if not parents:
        return None
    return ", ".join(f"{k}:{v}" if v else k for k, v in parents.items())


def _eff_lookup(effective: dict[str, Any], key: str) -> Optional[str]:
    if not effective:
        return None
    return _clean((effective.get("direct") or {}).get(key)) or _clean((effective.get("managed") or {}).get(key))


def _deps_before(poms: list[PomInfo], effective: dict[str, Any], own: set[str],
                 needs_eff: list[str]) -> dict[str, _Dep]:
    out: dict[str, _Dep] = {}
    for p in poms:
        for d in p.dependencies:
            if d.key in own or not d.group_id:
                continue
            v = _clean(d.version)
            if v:
                if out.get(d.key, (None, ""))[1] != "declared":
                    out[d.key] = (v, "declared")
                continue
            if d.key in out:
                continue
            ev = _eff_lookup(effective, d.key)
            if ev is None:
                needs_eff.append(d.key)
            out[d.key] = (ev, "managed") if ev else (None, "absent")
    return out


def _deps_after(poms: list[PomInfo], own: set[str]) -> tuple[dict[str, _Dep], dict[str, str]]:
    out: dict[str, _Dep] = {}
    where: dict[str, str] = {}
    for p in poms:
        for d in p.dependencies:
            if d.key in own or not d.group_id:
                continue
            v = _clean(d.version)
            if v and out.get(d.key, (None, ""))[1] != "declared":
                out[d.key] = (v, "declared")
                where[d.key] = p.path
            elif d.key not in out:
                out[d.key] = (None, "managed")
                where[d.key] = p.path
    return out, where


def _boms(poms: list[PomInfo]) -> dict[str, tuple[Optional[str], str]]:
    out: dict[str, tuple[Optional[str], str]] = {}
    for p in poms:
        for d in p.dependency_management:
            if _is_bom(d) and d.key not in out:
                out[d.key] = (_clean(d.version), p.path)
    return out


def _plugins(poms: list[PomInfo]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for p in poms:
        for pl in p.plugins:
            v = _clean(pl.version)
            if v and pl.key not in out:
                out[pl.key] = (v, p.path)
    return out


def _notes(report: RunReport) -> list[str]:
    out: list[str] = []
    for pr in report.phases:
        out += list(pr.notes)
        for a in pr.attempts:
            out += list(a.notes)
    return out


def _restructure_keys(notes: list[str]) -> tuple[set[str], set[str], dict[str, str]]:
    """(optional bumped keys, mandatory added keys, carried-over key → version) from phase / attempt notes."""
    optional: set[str] = set()
    mandatory: set[str] = set()
    pinned: dict[str, str] = {}
    for n in notes:
        m = _OPTIONAL_RE.search(n)
        if m:
            for entry in m.group(1).split(", "):
                tok = entry.strip().split(" ", 1)[0]
                if tok and tok != "-" and ":" in tok:
                    optional.add(tok)
        m = _MANDATORY_RE.search(n)
        if m:
            for tok in m.group(1).split(", "):
                tok = tok.strip()
                if tok and tok != "-" and ":" in tok:
                    mandatory.add(":".join(tok.split(":")[:2]))
        for m in _PINNED_RE.finditer(n):
            pinned.setdefault(m.group(1), m.group(2))
    return optional, mandatory, pinned


def _replacement_key(f: AdvisoryFinding) -> Optional[str]:
    r = (f.replacement or "").strip()
    if r and " " not in r and r.count(":") in (1, 2):
        return ":".join(r.split(":")[:2])
    return None


def _advisory_maps(findings: list[AdvisoryFinding]) -> tuple[dict[str, str], dict[str, str]]:
    """(as-is artifact key → advisory id, replacement key → advisory id)."""
    arts: dict[str, str] = {}
    repl: dict[str, str] = {}
    for f in findings:
        for a in f.artifacts:
            arts.setdefault(":".join(a.split(":")[:2]), f.advisory_id)
        k = _replacement_key(f)
        if k:
            repl.setdefault(k, f.advisory_id)
    return arts, repl


def _is_spring(key: str) -> bool:
    return key.startswith("org.springframework")


def _coord(key: str, v: Optional[str]) -> str:
    return f"{key}:{v}" if v else key


def _versions(profile: ProjectProfile, final: list[PomInfo], cfg: Any) -> dict[str, Optional[str]]:
    main = _main_pom(final)
    target = getattr(cfg, "target", None) if cfg is not None else None
    java_after = main.java_version if main else None
    if not java_after:
        java_after = next((p.java_version for p in final if p.java_version), None)
    if not java_after and target is not None:
        java_after = target.java_version          # managed by the (in-house) parent
    boot_after = main.spring_boot_version if main else None
    if not boot_after:
        boot_after = next((p.spring_boot_version for p in final if p.spring_boot_version), None)
    if not boot_after and target is not None:
        boot_after = target.spring_boot_version
    return {"java_before": profile.java_version, "java_after": java_after,
            "spring_boot_before": profile.spring_boot_version, "spring_boot_after": boot_after}


def _dedupe_add(out: list[str], item: str) -> None:
    s = " ".join(item.split())
    if not s:
        return
    low = s.lower()
    for x in out:
        xl = x.lower()
        short, long_ = (low, xl) if len(low) <= len(xl) else (xl, low)
        if long_ == short or (long_.startswith(short) and not long_[len(short)].isalnum()):
            return                                   # same sentence, possibly with a suffix ("… (replacement: …)")
    out.append(s)


def _from_here(h: HandoverSummary, report: RunReport, cfg: Any) -> list[str]:
    adv_cfg = getattr(cfg, "advisories", None) if cfg is not None else None
    out: list[str] = []
    findings = list(report.advisories or [])
    # (a) advisories not replaced
    for f in findings:
        if f.outcome == "replaced":
            continue
        txt = f"{f.title}: {f.followup}".rstrip(": ") if f.followup else f.title
        if f.replacement:
            txt += f" (replacement: {f.replacement}{' ' + f.replacement_version if f.replacement_version else ''})"
        _dedupe_add(out, txt)
    # (b) replaced → verify
    for f in findings:
        if f.outcome != "replaced":
            continue
        via = f"recipe {', '.join(f.recipes)}" if f.recipes else "replaced during the run"
        txt = f"verify {f.library} → {f.replacement or '?'} ({via})"
        if f.followup:
            txt += f": {f.followup}"
        _dedupe_add(out, txt)
    # (c) carried-over pins
    if adv_cfg is None or getattr(adv_cfg, "report_carried_over", True):
        co = h.carried_over
        if len(co) > CARRIED_LIST_MAX:
            _dedupe_add(out, f"{len(co)} dependencies are pinned at their as-is versions because the new parent / "
                             f"Boot BOM no longer manages them (⚠ in Versions replaced) — confirm or replace each; "
                             f"the first {CARRIED_LIST_MAX}:")
        for c in co[:CARRIED_LIST_MAX]:
            _dedupe_add(out, f"{c.key} pinned at {c.after} — the new parent/Boot BOM no longer manages it: "
                             "confirm the version or replace")
    # (d) pointers
    if report.blocked:
        _dedupe_add(out, f"{len(report.blocked)} cluster(s) blocked — see Blocked")
    if report.unresolved:
        _dedupe_add(out, f"{len(report.unresolved)} unresolved error(s) — see Unresolved errors")
    if "test-compile-skipped" in report.flags:
        _dedupe_add(out, "test sources do not compile yet (main code builds with -Dmaven.test.skip=true) — "
                         "fix the tests before relying on CI")
    # (e) manual follow-ups not already listed (advisory follow-ups are covered by (a)/(b))
    adv_fu: set[str] = set()
    if findings:
        try:
            from sbmig.advisory import followups_for
            adv_fu = {" ".join(x.split()).lower() for x in (followups_for(findings) or [])}
        except Exception as e:  # pragma: no cover - advisory package half-built
            log.debug("followups_for unavailable: %s", e)
    for m in report.manual_followups:
        if " ".join(m.split()).lower() in adv_fu:
            continue
        _dedupe_add(out, m)
    cap = int(getattr(adv_cfg, "max_from_here", 0) or 0) if adv_cfg is not None else 0
    if cap > 0 and len(out) > cap:
        rest = len(out) - (cap - 1)
        out = out[: cap - 1] + [f"… and {rest} more — see Manual follow-ups / Blocked / Unresolved errors"]
    return out


def _build(profile: ProjectProfile, view: Any, report: RunReport, effective: Optional[dict[str, Any]],
           cfg: Any) -> HandoverSummary:
    from sbmig.analyze.pom_parser import parse_poms

    effective = effective or {}
    h = HandoverSummary()
    before_poms = list(profile.poms)
    final, warnings = parse_poms(view)
    h.notes += [f"final poms: {w}" for w in warnings[:5]]
    own = _own_keys(before_poms) | _own_keys(final)

    # ---- parent / java / boot
    pb, pa = _parents(before_poms), _parents(final)
    h.parent_before, h.parent_after = _fmt_parents(pb), _fmt_parents(pa)
    for k, v in _versions(profile, final, cfg).items():
        setattr(h, k, v)

    notes = _notes(report)
    optional, mandatory, pinned = _restructure_keys(notes)
    adv_arts, adv_repl = _advisory_maps(list(report.advisories or []))
    changes: list[VersionChange] = []
    main = _main_pom(final)
    main_path = main.path if main else "pom.xml"

    # parent rows: same key → version change; different keys → one swap row per pair
    same = [k for k in pb if k in pa]
    for k in same:
        if pb[k] != pa[k]:
            changes.append(VersionChange(kind="parent", key=k, before=pb[k], after=pa[k], reason=REASON_PARENT,
                                         pom=main_path))
    only_b = [k for k in pb if k not in pa]
    only_a = [k for k in pa if k not in pb]
    for kb_, ka in zip(only_b, only_a):
        changes.append(VersionChange(kind="parent", key=ka, before=_coord(kb_, pb[kb_]), after=pa[ka],
                                     reason=REASON_PARENT, pom=main_path))
    for kb_ in only_b[len(only_a):]:
        h.removed.append(f"parent {_coord(kb_, pb[kb_])}")
    for ka in only_a[len(only_b):]:
        h.added.append(f"parent {_coord(ka, pa[ka])}")

    # ---- BOM imports
    bb, ba = _boms(before_poms), _boms(final)
    for k in sorted(set(bb) | set(ba)):
        if k in bb and k in ba:
            if bb[k][0] != ba[k][0]:
                changes.append(VersionChange(kind="bom", key=k, before=bb[k][0], after=ba[k][0],
                                             reason=REASON_BOOT if _is_spring(k) else REASON_OTHER, pom=ba[k][1]))
        elif k in bb:
            h.removed.append(f"bom {_coord(k, bb[k][0])}")
        else:
            h.added.append(f"bom {_coord(k, ba[k][0])}")

    # ---- dependencies
    needs_eff: list[str] = []
    db = _deps_before(before_poms, effective, own, needs_eff)
    da, where = _deps_after(final, own)
    if needs_eff and not effective:
        h.notes.append("as-is effective dependencyManagement unavailable — versions the as-is parent managed are "
                       "shown as '-'")

    def reason_for(key: str) -> str:
        if key in adv_arts:
            return f"advisory:{adv_arts[key]}"
        if key in adv_repl:
            return f"advisory:{adv_repl[key]}"
        if key in optional:
            return REASON_OPTIONAL
        if key in mandatory:
            return REASON_MANDATORY
        if _is_spring(key):
            return REASON_BOOT
        return REASON_OTHER

    for k in sorted(set(db) | set(da)):
        if k in db and k in da:
            (bv, bs), (av, as_) = db[k], da[k]
            if bs in ("managed", "absent") and as_ == "managed":
                continue                              # managed before and after: covered by the parent / BOM row
            if bv == av and bs == as_:
                continue
            carried = (bs == "managed" and as_ == "declared" and av is not None
                       and av == _eff_lookup(effective, k)) or (k in pinned and as_ == "declared")
            if bv == av and not carried:
                continue                              # same version, only the declaring place changed
            changes.append(VersionChange(kind="dependency", key=k, before=bv, after=av, before_source=bs,
                                         after_source=as_, carried_over=carried,
                                         reason=REASON_CARRIED if carried else reason_for(k),
                                         pom=where.get(k, main_path)))
        elif k in db:
            r = reason_for(k)
            h.removed.append(_coord(k, db[k][0]) + (f" ({r})" if r.startswith("advisory:") else ""))
        else:
            r = reason_for(k)
            tag = r if r.startswith("advisory:") or r in (REASON_MANDATORY, REASON_OPTIONAL) else ""
            h.added.append(_coord(k, da[k][0]) + (f" ({tag})" if tag else ""))

    # ---- plugins (version changes only; added / removed plugins are restructure noise)
    plb, pla = _plugins(before_poms), _plugins(final)
    for k in sorted(set(plb) & set(pla)):
        if plb[k][0] != pla[k][0]:
            changes.append(VersionChange(kind="plugin", key=k, before=plb[k][0], after=pla[k][0],
                                         reason=REASON_BOOT if _is_spring(k) else REASON_OTHER, pom=pla[k][1]))

    order = {"parent": 0, "bom": 1, "dependency": 2, "plugin": 3, "property": 4}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.key))
    h.changes = changes
    h.carried_over = [c for c in changes if c.carried_over]
    h.from_here = _from_here(h, report, cfg)
    return h


def build_handover(profile: Optional[ProjectProfile], view: Any, report: RunReport,
                   effective: Optional[dict[str, Any]] = None, cfg: Any = None) -> HandoverSummary:
    """As-is vs final poms + advisories → HandoverSummary. Never raises."""
    if profile is None:
        return HandoverSummary(notes=["handover unavailable: no project profile (the run stopped before analyze)"])
    try:
        return _build(profile, view, report, effective, cfg)
    except Exception as e:
        log.warning("handover failed: %s", e)
        h = HandoverSummary(notes=[f"handover unavailable: {type(e).__name__}: {e}"])
        try:
            h.from_here = _from_here(h, report, cfg)
        except Exception:  # pragma: no cover
            pass
        return h
