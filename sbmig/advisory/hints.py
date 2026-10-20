"""Advisory-guided LLM fixes (round 6): which library advisories apply to the error cluster being fixed, as short
"what to migrate TO" guidance lines for the fixer prompt (`FixContext.advisory_hints`).

    hints_for(errors, findings, profile=None, *, notes_root=None) -> (hints, advisory_ids)

A finding matches a cluster when any error's message / symbol / location / raw text / artifact mentions
  (a) an import package of the finding's library (`LibraryRule.imports` in `sbmig.analyze.libraries`, e.g.
      `com.netflix.hystrix`), matched on a package-segment boundary,
  (b) one of the finding's as-is artifacts (`g:a`), or
  (c) the replacement artifact (`g:a` or its artifactId, e.g. a missing `spring-cloud-starter-circuitbreaker-resilience4j`).

One hint per matched finding (order of `findings`):
    "<library> (<status>, policy <policy>): replace with <replacement>[ (<replacement_version>)]. <followup>"
(≤ HINT_MAX chars; the title stands in when there is no follow-up) plus, when the advisory references a note
(`notes: knowledge/notes/<file>.md`, carried on the finding as "see <path>"), an excerpt of that note's section:
a `## fix-loop: <library>` heading wins (convention for loop guidance written for this purpose), otherwise the
first heading that mentions the library (≤ NOTE_EXCERPT_MAX chars).

Pure function: reads only the referenced note files, never raises (problems → no hint for that finding).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any, Optional

from sbmig.core.models import AdvisoryFinding, BuildError

log = logging.getLogger("sbmig.advisory.hints")

HINT_MAX = 500
NOTE_EXCERPT_MAX = 600
RAW_MAX = 4000                      # per error: enough for the javac block, bounded for big dependency dumps
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


# ---------------------------------------------------------------------------
# library rules
# ---------------------------------------------------------------------------
@cache
def library_packages(library: str) -> tuple[str, ...]:
    """Import package prefixes of a library (`LibraryRule.imports`), () for an unknown name."""
    try:
        from sbmig.analyze.libraries import LIBRARY_RULES
    except Exception as e:  # noqa: BLE001  # pragma: no cover - analyze is ours
        log.debug("LIBRARY_RULES unavailable: %s", e)
        return ()
    for rule in LIBRARY_RULES:
        if rule.name == library:
            return tuple(rule.imports)
    return ()


def _pkg_re(prefix: str) -> re.Pattern:
    # segment boundary on both sides: `com.netflix.hystrix` matches `com.netflix.hystrix.HystrixCommand` and
    # "package com.netflix.hystrix does not exist", not `com.netflix.hystrixfoo` or `x.com.netflix.hystrix`
    return re.compile(r"(?<![\w.])" + re.escape(prefix) + r"(?![\w])")


def _error_text(e: BuildError) -> str:
    parts = [e.message or "", e.symbol or "", e.location or "", (e.raw or "")[:RAW_MAX]]
    if e.artifact is not None:
        parts.append(f"{e.artifact.group_id}:{e.artifact.artifact_id}")
    return "\n".join(p for p in parts if p)


def _replacement_keys(replacement: Optional[str]) -> list[str]:
    if not replacement:
        return []
    r = replacement.strip()
    out = [r] if ":" in r else []
    aid = r.split(":")[1] if ":" in r else r
    # an artifactId alone is matched only when it is distinctive (contains a dash, e.g. "...-resilience4j")
    if aid and "-" in aid:
        out.append(aid)
    return out


def _matches(f: AdvisoryFinding, text: str) -> bool:
    for prefix in library_packages(f.library):
        if prefix and _pkg_re(prefix).search(text):
            return True
    for ga in f.artifacts:
        if ga and ga in text:
            return True
    for key in _replacement_keys(f.replacement):
        if re.search(r"(?<![\w.\-])" + re.escape(key) + r"(?![\w\-])", text):
            return True
    return False


# ---------------------------------------------------------------------------
# hint text
# ---------------------------------------------------------------------------
def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    dot = head.rfind(". ")
    if dot >= limit // 2:
        return head[: dot + 1]
    return head.rstrip() + "…"


def _note_paths(f: AdvisoryFinding) -> list[str]:
    out = []
    for n in f.notes:
        m = re.match(r"\s*see\s+(\S+\.md)\b", n or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _resolve(path: str, notes_root: Optional[Path]) -> Optional[Path]:
    p = Path(path)
    if p.is_absolute():
        cands = [p]
    else:
        cands = ([Path(notes_root) / p] if notes_root else []) + [_REPO_ROOT / p, p]
    return next((c for c in cands if c.is_file()), None)


def note_section(text: str, library: str) -> tuple[str, str]:
    """(heading, body) of the section to excerpt for `library`: `## fix-loop: <library>` first, else the first
    heading that mentions the library as a word; ("", "") when none."""
    lines = text.replace("\r\n", "\n").split("\n")
    heads = [(i, len(m.group(1)), m.group(2)) for i, ln in enumerate(lines) if (m := _HEADING_RE.match(ln))]
    lib = re.escape(library)
    loop_re = re.compile(rf"(?i)^fix-loop:\s*{lib}\s*$")
    word_re = re.compile(rf"(?i)(?<![\w\-]){lib}(?![\w\-])")
    pick = next((h for h in heads if loop_re.match(h[2])), None) or next(
        (h for h in heads if h[1] > 1 and word_re.search(h[2])), None)
    if pick is None:
        return "", ""
    i, level, title = pick
    end = next((j for j, lv, _ in heads if j > i and lv <= level), len(lines))
    return title, "\n".join(lines[i + 1:end]).strip()


def _note_excerpt(f: AdvisoryFinding, notes_root: Optional[Path]) -> str:
    for rel in _note_paths(f):
        try:
            p = _resolve(rel, notes_root)
            if p is None:
                continue
            from sbmig.fix.context import strip_front_matter
            title, body = note_section(strip_front_matter(p.read_text(encoding="utf-8")), f.library)
        except Exception as e:  # noqa: BLE001 — unreadable note → hint without excerpt
            log.debug("advisory %s: note %s unreadable: %s", f.advisory_id, rel, e)
            continue
        if body:
            return f" Note ({Path(rel).name} § {title}): {_cut(body, NOTE_EXCERPT_MAX)}"
    return ""


def hint_text(f: AdvisoryFinding, notes_root: Optional[Path] = None) -> str:
    head = f"{f.library} ({f.status}, policy {f.policy})"
    if f.replacement:
        head += f": replace with {f.replacement}"
        if f.replacement_version:
            head += f" ({f.replacement_version})"
    head += "."
    tail = f.followup.strip() or f.title.strip()
    main = _cut(f"{head} {tail}".strip(), HINT_MAX)
    return main + _note_excerpt(f, notes_root)


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def hints_for(errors: Iterable[BuildError], findings: Optional[Iterable[AdvisoryFinding]], profile: Any = None, *,
              notes_root: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """(hints, advisory ids) for the findings that match these errors, in `findings` order. `profile` is accepted
    for the contract (not needed today); `notes_root` = base directory of the advisories' `notes` paths (the
    config's repo root; defaults to this checkout). Never raises."""
    hints: list[str] = []
    ids: list[str] = []
    try:
        texts = [_error_text(e) for e in (errors or [])]
        if not texts:
            return [], []
        blob = "\n".join(texts)
        for f in findings or []:
            if f.advisory_id in ids:
                continue
            try:
                if not _matches(f, blob):
                    continue
                hints.append(hint_text(f, notes_root))
                ids.append(f.advisory_id)
            except Exception as e:  # noqa: BLE001 — one broken finding never hides the others
                log.debug("advisory hint for %s failed: %s", getattr(f, "advisory_id", "?"), e)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - defensive: never break the loop
        log.warning("advisory hints failed: %s", e)
        return [], []
    return hints, ids
