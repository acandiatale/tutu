"""The run's error list (`RunReport.error_entries`, v2.1) as the report needs it: counts, `seen` groups, the tries cell,
diff +/- per file and one row per case ("Fixes by case"). Pure data — rendering (and translation) is in markdown.py /
writer.py; everything here tolerates an empty list and entries with missing fields."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

STATUS_ORDER = ("unresolved", "open", "blocked", "pom-verify", "resolved", "note")
OPEN_STATUSES = ("open", "unresolved")         # SBMIG-RESULT `open=` (finalize turns open into unresolved)
SEEN_ORDER = ("compile", "restructure", "review", "build")
KEPT_RESULTS_EXCLUDED = ("regressed", "reverted", "apply_failed")   # rolled back / never applied: not in the tree
_FIX_ROUND_RE = re.compile(r"^fix r(\d+)\b")
_RESOLVED_TRY_RE = re.compile(r"^r(\d+) try (\d+)$")
_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def entries_of(report: Any) -> list[Any]:
    return list(getattr(report, "error_entries", None) or [])


def status_counts(entries: Iterable[Any]) -> "OrderedDict[str, int]":
    """status → count in `STATUS_ORDER` (unknown statuses after it, alphabetically); zero counts left out."""
    c = Counter(str(getattr(e, "status", "") or "open") for e in entries)
    keys = [s for s in STATUS_ORDER if c.get(s)] + sorted(k for k in c if k not in STATUS_ORDER and c[k])
    return OrderedDict((k, c[k]) for k in keys)


def open_count(entries: Iterable[Any]) -> int:
    return sum(1 for e in entries if getattr(e, "status", "") in OPEN_STATUSES)


def resolved_count(entries: Iterable[Any]) -> int:
    return sum(1 for e in entries if getattr(e, "status", "") == "resolved")


def counts_text(counts: "OrderedDict[str, int] | dict[str, int]") -> str:
    """"resolved 23, unresolved 2" (ASCII; the statuses are identifiers and stay English in every language)."""
    return ", ".join(f"{k} {v}" for k, v in counts.items() if v)


def seen_rank(seen: str) -> tuple[int, int, str]:
    s = (seen or "").strip()
    if s in SEEN_ORDER:
        return (SEEN_ORDER.index(s), 0, "")
    m = _FIX_ROUND_RE.match(s)
    if m:
        return (len(SEEN_ORDER), int(m.group(1)), s)
    return (len(SEEN_ORDER) + 1, 0, s)


def ordered(entries: Iterable[Any]) -> list[Any]:
    """The entries in pipeline order of `seen` (compile → restructure → review → build → fix r1, r2, …, then anything
    else); list order inside a group."""
    es = list(entries)
    return [e for _, e in sorted(enumerate(es), key=lambda ie: (seen_rank(str(getattr(ie[1], "seen", "") or "-")),
                                                                 ie[0]))]


def seen_groups(entries: Iterable[Any]) -> "OrderedDict[str, list[tuple[int, Any]]]":
    """`seen` → [(#, entry)] in pipeline order (see `ordered`); # numbers the entries 1… in that order, so the
    report's error list reads top to bottom."""
    groups: "OrderedDict[str, list[tuple[int, Any]]]" = OrderedDict()
    for i, e in enumerate(ordered(entries), 1):
        groups.setdefault(str(getattr(e, "seen", "") or "-"), []).append((i, e))
    return groups


def try_text(t: Any) -> str:
    """`1 llm-new edits improved`, `2 kb-script v2 build_ok`."""
    parts = [str(getattr(t, "n", "") or "?"), str(getattr(t, "source", "") or "?")]
    if getattr(t, "script_version", 0):
        parts.append(f"v{t.script_version}")
    if getattr(t, "applied", "none") == "edits":
        parts.append("edits")
    parts.append(str(getattr(t, "result", "") or "pending"))
    return " ".join(parts)


def tries_text(tries: Iterable[Any]) -> str:
    return " · ".join(try_text(t) for t in tries)


def where(e: Any) -> str:
    """`file:line`, else the module, else "" (pom-verify / note entries may have neither)."""
    f = getattr(e, "file", None)
    if f:
        line = getattr(e, "line", None)
        return f"{f}:{line}" if line else str(f)
    return str(getattr(e, "module", None) or "")


def entry_text(e: Any) -> str:
    """The message, else the note (pom-verify: what the restructure did; note entries: the warning)."""
    return str(getattr(e, "message", "") or getattr(e, "note", "") or "")


# ---------------------------------------------------------------------------- diffs
def diff_stats(diff: str, files: Iterable[str] = ()) -> "OrderedDict[str, list[int]]":
    """file → [added, removed] lines of a unified diff (`git diff` headers or bare `---` / `+++` pairs). A diff
    without file headers is credited to the only file of `files`; listed files the diff does not mention get
    [0, 0] (they changed, the capped diff just does not show it)."""
    out: "OrderedDict[str, list[int]]" = OrderedDict()
    cur: Optional[str] = None
    in_hunk = False
    for line in (diff or "").splitlines():
        m = _GIT_HEADER_RE.match(line)
        if m:
            cur, in_hunk = m.group(2), False
            out.setdefault(cur, [0, 0])
            continue
        if not in_hunk and line.startswith("--- "):
            continue
        if not in_hunk and line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p != "/dev/null":
                cur = p.removeprefix("b/")
                out.setdefault(cur, [0, 0])
            continue
        if line.startswith("@@"):
            in_hunk = True
            if cur is None:
                only = [f for f in files if f]
                cur = only[0] if len(only) == 1 else "?"
                out.setdefault(cur, [0, 0])
            continue
        if not in_hunk or cur is None:
            continue
        if line.startswith("+"):
            out[cur][0] += 1
        elif line.startswith("-"):
            out[cur][1] += 1
    for f in files or ():
        if f and f not in out:
            out[f] = [0, 0]
    if out.get("?") == [0, 0]:
        del out["?"]
    return out


def merge_stats(stats: Iterable["OrderedDict[str, list[int]]"]) -> "OrderedDict[str, list[int]]":
    out: "OrderedDict[str, list[int]]" = OrderedDict()
    for s in stats:
        for f, (a, r) in s.items():
            cur = out.setdefault(f, [0, 0])
            cur[0] += a
            cur[1] += r
    return out


def stats_text(stats: "OrderedDict[str, list[int]]", limit: int = 6, code: bool = True, minus: str = "−") -> str:
    """"`pom.xml` (+6 −0), `A.java` (+1 −1) (+2 more)"; a file whose capped diff shows nothing has no counts.
    The console passes `code=False, minus="-"` (cp949 consoles)."""
    q = "`" if code else ""
    items = []
    for f, (a, r) in list(stats.items())[:limit]:
        items.append(f"{q}{f}{q}" + (f" (+{a} {minus}{r})" if a or r else ""))
    if len(stats) > limit:
        items.append(f"(+{len(stats) - limit} more)")
    return ", ".join(items)


def kept(t: Any) -> bool:
    """The try's changes are still in the tree (not rolled back, actually applied)."""
    return getattr(t, "applied", "none") != "none" and getattr(t, "result", "") not in KEPT_RESULTS_EXCLUDED


# ---------------------------------------------------------------------------- fixes by case
@dataclass
class CaseRow:
    case_id: str
    name: str = ""                                 # the first entry's message
    entries: int = 0
    resolved: int = 0
    open: int = 0
    tries: list[Any] = field(default_factory=list)   # distinct tries of the case (first seen order)
    analysis: str = ""                             # 원인 (the deciding try's)
    solution: str = ""                             # 해결 방법
    no_script_reason: str = ""
    changes: "OrderedDict[str, list[int]]" = field(default_factory=OrderedDict)
    script: str = ""                               # cases/<id>/fix_vN.py (resolving script, else the newest stored)
    script_version: int = 0
    seen: str = ""


def _try_key(t: Any) -> tuple:
    return (getattr(t, "round", 0), getattr(t, "n", 0), getattr(t, "source", ""), getattr(t, "script_version", 0),
            getattr(t, "commit", ""))


def _deciding_try(tries: list[Any], resolved_marks: list[str]) -> Optional[Any]:
    for mark in resolved_marks:
        m = _RESOLVED_TRY_RE.match(mark or "")
        if m:
            rnd, n = int(m.group(1)), int(m.group(2))
            hit = next((t for t in tries if getattr(t, "round", 0) == rnd and getattr(t, "n", 0) == n), None)
            if hit is not None:
                return hit
    with_text = [t for t in tries if getattr(t, "analysis", "") or getattr(t, "solution", "")]
    return with_text[-1] if with_text else (tries[-1] if tries else None)


def case_rows(entries: Iterable[Any]) -> list[CaseRow]:
    """One row per case that was worked on (has tries) or resolved, in the error list's order (`ordered`). Tries are
    shared by the case's entries (one try fixes the whole case) and de-duplicated."""
    rows: "OrderedDict[str, CaseRow]" = OrderedDict()
    marks: dict[str, list[str]] = {}
    scripts: dict[str, list[str]] = {}
    for e in ordered(entries):
        tries = list(getattr(e, "tries", None) or [])
        status = getattr(e, "status", "")
        cid = str(getattr(e, "case_id", "") or "")
        if not cid or (not tries and status != "resolved"):
            continue
        row = rows.get(cid)
        if row is None:
            row = rows[cid] = CaseRow(case_id=cid, name=entry_text(e), seen=str(getattr(e, "seen", "") or ""))
        row.entries += 1
        row.resolved += status == "resolved"
        row.open += status in OPEN_STATUSES
        have = {_try_key(t) for t in row.tries}
        row.tries += [t for t in tries if _try_key(t) not in have]
        if getattr(e, "resolved", ""):
            marks.setdefault(cid, []).append(e.resolved)
        if getattr(e, "script", ""):
            scripts.setdefault(cid, []).append(e.script)
    for cid, row in rows.items():
        row.tries.sort(key=lambda t: (getattr(t, "round", 0), getattr(t, "n", 0)))
        d = _deciding_try(row.tries, marks.get(cid, []))
        if d is not None:
            row.analysis = getattr(d, "analysis", "") or ""
            row.solution = getattr(d, "solution", "") or ""
            row.no_script_reason = getattr(d, "no_script_reason", "") or ""
        if not row.no_script_reason:
            row.no_script_reason = next((t.no_script_reason for t in reversed(row.tries)
                                         if getattr(t, "no_script_reason", "")), "")
        row.changes = merge_stats(diff_stats(getattr(t, "diff", "") or "", getattr(t, "files", None) or [])
                                  for t in row.tries if kept(t))
        if scripts.get(cid):
            row.script = scripts[cid][-1]
            m = re.search(r"fix_v(\d+)\.py$", row.script)
            row.script_version = int(m.group(1)) if m else 0
        else:
            versions = [getattr(t, "script_version", 0) for t in row.tries if getattr(t, "script_version", 0)]
            if versions:
                row.script_version = max(versions)
                row.script = f"cases/{cid}/fix_v{row.script_version}.py"
    return list(rows.values())


def review_entries(entries: Iterable[Any]) -> list[Any]:
    """Entries the structure review produced or fixed (seen=review, or a try with source=review)."""
    return [e for e in entries if getattr(e, "seen", "") == "review"
            or any(getattr(t, "source", "") == "review" for t in getattr(e, "tries", None) or [])]


def review_changes(entries: Iterable[Any]) -> "OrderedDict[str, list[int]]":
    """file → [+, −] of the review's own kept tries."""
    seen: set[tuple] = set()
    stats = []
    for e in review_entries(entries):
        for t in getattr(e, "tries", None) or []:
            if getattr(t, "source", "") != "review" or not kept(t) or _try_key(t) in seen:
                continue
            seen.add(_try_key(t))
            stats.append(diff_stats(getattr(t, "diff", "") or "", getattr(t, "files", None) or []))
    return merge_stats(stats)
