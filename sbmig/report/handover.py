"""Handover: "here is what we replaced, from here on it is yours" — the first section of report.md.

`build_handover(profile, view, report, effective=None, cfg=None)` compares the as-is poms (`profile.poms`, plus
the as-is effective dependencyManagement captured at intake for versions the old parent managed) with the final
poms of the migrated tree (`view` = Workspace / PathView) and compresses the result for the owning team:
parent / java / spring boot before → after, one row per dependency whose version changed (⚠ carried-over pins:
the new parent / Boot BOM no longer manages them), added / removed artifacts, and an ordered "from here on, your
team" list (advisories not replaced, replaced-but-verify, carried-over pins, blocked / unresolved pointers,
manual follow-ups) and the multi-module merge conflicts (`report.merge_conflicts`). Round 8: one
`properties_to_set` row per `external` externalized-config finding (`report.properties`, see `property_rows`) plus the
guide lines of `knowledge/config/externalized.yml`. Round 9: file paths recorded by fix attempts before the restructure
moved the sources (`llm_fixed_files`) are mapped to where the file lives now (`resolve_current_path`). Pure and defensive: never raises (→ `HandoverSummary(notes=["handover unavailable: …"])`).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
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
LLM_FILES_MAX = 5                # files listed per "code rewritten by the LLM for <library>" line
# the orchestrator's X1b follow-up (report.javax_api_inventory, 2026-10-22)
_JAVAX_FOLLOWUP_RE = re.compile(r"^\d+ javax API jar\(s\) still on the classpath after the migration")
IDENTICAL_LIST_MAX = 5           # paths named in the "N identical duplicate(s) dropped" line
PROPERTY_SEP = " | "             # cell separator of a properties_to_set row (cells never contain "|")
PROPERTY_COLUMNS = ("key", "before migration", "referenced at", "written as", "set via")
DEFAULT_SENTINEL = "__SET_ME__"
DEFAULT_MAX_REFS = 3

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


# ---------------------------------------------------------------------------- externalized config (round 8)
def _inventory(report: RunReport) -> Any:
    inv = getattr(report, "properties", None)
    if inv is None and getattr(report, "profile", None) is not None:
        inv = getattr(report.profile, "properties", None)
    return inv


def _pcell(text: object) -> str:
    s = " ".join(str(text if text is not None else "").split()).replace("|", "/")
    return s or "-"


def env_name(key: str) -> str:
    """Relaxed-binding environment variable name: `spring.datasource.url` → `SPRING_DATASOURCE_URL`."""
    out = re.sub(r"[.\-\[\]]+", "_", key.strip()).upper()
    return out.strip("_")


def _refs_cell(refs: list[Any], max_refs: int) -> str:
    seen: list[str] = []
    for r in refs:
        where = r.file + (f":{r.line}" if r.line else "")
        if where not in seen:
            seen.append(where)
    if not seen:
        return "-"
    cell = ", ".join(seen[:max_refs])
    if len(seen) > max_refs:
        cell += f" (+{len(seen) - max_refs} more)"
    return cell


def _written_cell(f: Any, written: set[str], sentinel: str) -> str:
    if not f.boot_keys:
        return f.note or "-"
    ref = f.refs[0] if f.refs else None
    value = sentinel
    if ref is not None and ref.source == "context-param" and ref.default not in (None, ""):
        value = str(ref.default)
    keys = [k for k in f.boot_keys if k in written]
    if not keys:
        return "-"
    return ", ".join(f"{k}={value}" for k in keys)


def _set_via_cell(f: Any) -> str:
    sources = {r.source for r in f.refs}
    if sources and sources <= {"system-env"}:
        return f"environment variable {f.key}"
    if sources and sources <= {"system-property"}:
        return f"-D{f.key}"                        # System.getProperty sees neither env vars nor application.properties
    parts: list[str] = []
    if f.boot_keys:
        parts.append(env_name(f.boot_keys[0]))
    if "system-property" in sources:
        parts.append(f"-D{f.key}")
    if "system-env" in sources:
        parts.append(f"environment variable {f.key}")
    return " / ".join(parts) or "-"


def needs_value(f: Any) -> bool:
    """An external finding the owners must supply: it has Boot keys, or nothing maps it (system env, unmapped JNDI,
    `@ConfigurationProperties` prefix, -D). Rule-skipped context-params and Boot defaults need nothing."""
    if f.boot_keys:
        return True
    return f.supplied_by != "context-param" and not str(f.note or "").startswith("Boot default")


def external_to_set(inv: Any) -> list[Any]:
    """External findings that need a value (see `needs_value`)."""
    if inv is None:
        return []
    return [f for f in (getattr(inv, "findings", None) or []) if f.status == "external" and needs_value(f)]


def property_rows(inv: Any, cfg: Any = None) -> list[str]:
    """One `PROPERTY_SEP`-joined row per `external` finding (columns: `PROPERTY_COLUMNS`); findings that need no
    value (rule-skipped context-param, Boot default) are listed too, but only when at least one finding needs a
    value — otherwise there is nothing to set and the list is empty. Never raises."""
    if inv is None or not external_to_set(inv):
        return []
    x = getattr(cfg, "externalized", None) if cfg is not None else None
    sentinel = str(getattr(x, "sentinel", None) or DEFAULT_SENTINEL)
    try:
        max_refs = max(1, int(getattr(x, "max_refs_per_key", DEFAULT_MAX_REFS) or DEFAULT_MAX_REFS))
    except (TypeError, ValueError):
        max_refs = DEFAULT_MAX_REFS
    written = set(getattr(inv, "placeholders_written", None) or [])
    rows: list[str] = []
    for f in list(getattr(inv, "findings", None) or []):
        try:
            if f.status != "external":
                continue
            cells = [f.key, f.supplied_by or "unknown", _refs_cell(list(f.refs), max_refs),
                     _written_cell(f, written, sentinel), _set_via_cell(f)]
            rows.append(PROPERTY_SEP.join(_pcell(c) for c in cells))
        except Exception as e:  # pragma: no cover - a malformed finding must not drop the others
            log.debug("property row skipped: %s", e)
    return rows


def split_property_row(row: str) -> list[str]:
    """Inverse of `property_rows` for one row (always `len(PROPERTY_COLUMNS)` cells)."""
    cells = [c.strip() for c in str(row).split(PROPERTY_SEP)]
    n = len(PROPERTY_COLUMNS)
    if len(cells) > n:
        cells = cells[: n - 1] + [" / ".join(cells[n - 1:])]
    return cells + ["-"] * (n - len(cells))


def property_guide(cfg: Any) -> list[str]:
    """Guide lines from the externalized-config rules file; [] when cfg is None or the file does not load."""
    if cfg is None:
        return []
    try:
        from sbmig.analyze import load_rules
        x = cfg.externalized
        return [str(g) for g in (load_rules(cfg.resolve_path(x.rules_file)).guide or []) if str(g).strip()]
    except Exception as e:
        log.debug("externalized guide unavailable: %s", e)
        return []


def _properties_item(inv: Any, cfg: Any) -> Optional[str]:
    ext = external_to_set(inv)
    if not ext:
        return None
    x = getattr(cfg, "externalized", None) if cfg is not None else None
    sentinel = str(getattr(x, "sentinel", None) or DEFAULT_SENTINEL)
    files = list(getattr(inv, "placeholder_files", None) or [])
    where = (f"placeholders ({sentinel}) in {', '.join(files)}" if files
             else "no placeholders were written (see the table)")
    return (f"{len(ext)} externalized setting(s) need values before the first start — {where}; "
            "see 'Properties to set'")


def _properties_followup_present(report: RunReport) -> bool:
    for m in report.manual_followups or []:
        low = m.lower()
        if "externalized setting(s)" in low and ("placeholder" in low or "properties to set" in low):
            return True
    return False


def _view_exists(view: Any, path: str) -> bool:
    try:
        if hasattr(view, "exists"):
            return bool(view.exists(path))
        root = getattr(view, "root", None)
        return bool(root is not None and (Path(root) / path).exists())
    except (OSError, ValueError):
        return False


def resolve_current_path(ws: Any, path: str, cfg: Any = None, name: Optional[str] = None) -> str:
    """Where a file recorded before the restructure step lives now. Unchanged when it still exists (or `ws` is None);
    else the part from the `src/` component on is looked up under the comp, boot and front module directories of
    the configured layout (`cfg.layout.module_path(which, name)` — company `{name}-back` / `-boot` / `-front`, or
    the legacy `module/comp-jar` / `module/boot-jar`; without a config the legacy names); the original path when
    none exists. Pure (reads only `ws.exists`)."""
    if not path or ws is None:
        return path
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if _view_exists(ws, norm):
        return path
    parts = norm.split("/")
    if "src" not in parts:
        return path
    suffix = "/".join(parts[parts.index("src"):])
    lay = getattr(cfg, "layout", None) if cfg is not None else None
    bases: list[str] = []
    if lay is not None and callable(getattr(lay, "module_path", None)):
        for which in ("comp", "boot", "front"):
            mp = lay.module_path(which, name or "")
            if mp and "{name}" not in mp:
                bases.append(mp.strip("/"))
    if not bases:
        mdir = (getattr(lay, "module_dir", None) or "module").strip("/")
        for mod in (getattr(lay, "comp_module", None) or "comp-jar", getattr(lay, "boot_module", None) or "boot-jar"):
            bases.append(f"{mdir}/{mod.strip('/')}" if mdir else mod.strip("/"))
    for base in bases:
        cand = f"{base}/{suffix}"
        if cand != norm and _view_exists(ws, cand):
            return cand
    return path


def project_name(profile: Any) -> str:
    """`{name}` of the company layout: the root pom's artifactId (else the profile name)."""
    rp = getattr(profile, "root_pom", None)
    return (getattr(rp, "artifact_id", None) or getattr(profile, "name", None) or "") if profile is not None else ""


def _from_here(h: HandoverSummary, report: RunReport, cfg: Any, view: Any = None) -> list[str]:
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
    # (b2) code the LLM rewrote for a library advisory (round 6: the recipe left it behind) → review
    for f in findings:
        if not f.llm_fixed_files:
            continue
        fixed = list(dict.fromkeys(resolve_current_path(view, p, cfg, project_name(getattr(report, "profile", None)))
                                   for p in f.llm_fixed_files))
        files = ", ".join(fixed[:LLM_FILES_MAX])
        if len(fixed) > LLM_FILES_MAX:
            files += f" (+{len(fixed) - LLM_FILES_MAX} more)"
        txt = f"code rewritten by the LLM for {f.library}: {files}"
        if f.llm_fix_notes:
            txt += f" — {'; '.join(f.llm_fix_notes[:2])}"
        _dedupe_add(out, txt + " — review these changes (changes.patch, attempts table)")
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
    jars = list(getattr(report, "javax_api_jars", None) or [])
    if jars and not any(_JAVAX_FOLLOWUP_RE.match(m) for m in report.manual_followups):
        # the orchestrator's follow-up (listed under (e)) already names the libraries; the pointer stands in for it
        _dedupe_add(out, f"{len(jars)} javax API jar(s) still on the classpath — see that section")
    # (d2) externalized settings — the restructurer's follow-up says the same; keep that one when it exists
    try:
        item = None if _properties_followup_present(report) else _properties_item(_inventory(report), cfg)
        if item:
            _dedupe_add(out, item)
    except Exception as e:  # pragma: no cover
        log.debug("properties item skipped: %s", e)
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


def merge_conflict_lines(report: RunReport) -> list[str]:
    """`report.merge_conflicts` (layout `merge`) → one line per differing path, identical copies in one line."""
    out: list[str] = []
    identical: list[str] = []
    for c in list(getattr(report, "merge_conflicts", None) or []):
        if c.identical:
            identical.append(c.path)
            continue
        out.append(f"{c.path}: kept the copy from {c.kept_from}, dropped {', '.join(c.dropped_from) or '-'} "
                   f"(saved: {', '.join(c.saved_copies) or '-'})")
    if identical:
        names = ", ".join(identical[:IDENTICAL_LIST_MAX])
        if len(identical) > IDENTICAL_LIST_MAX:
            names += ", …"
        out.append(f"{len(identical)} identical duplicate(s) dropped (same content in every module): {names}")
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
    h.merge_conflicts = merge_conflict_lines(report)
    _fill_properties(h, report, cfg)
    h.from_here = _from_here(h, report, cfg, view)
    return h


def _fill_properties(h: HandoverSummary, report: RunReport, cfg: Any) -> None:
    try:
        h.properties_to_set = property_rows(_inventory(report), cfg)
        h.properties_guide = property_guide(cfg) if h.properties_to_set else []
    except Exception as e:  # pragma: no cover
        log.debug("properties rows skipped: %s", e)


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
            h.merge_conflicts = merge_conflict_lines(report)
            _fill_properties(h, report, cfg)
            h.from_here = _from_here(h, report, cfg, view)
        except Exception:  # pragma: no cover
            pass
        return h
