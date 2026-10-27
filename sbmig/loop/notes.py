"""v2.1b (2026-10-27, business request: "edit 란에 어디의 어떤 줄을 어떻게 수정하는지"): the 수정 notes of a try — every
modification described in words (`EditNote`: path, where, change, lines, source llm | diff).

    answer_notes(answer, edits=True)   the LLM's own descriptions: the answer's `<changes>` + each applied edit's
                                       `desc` for a file `<changes>` does not describe; a script run: `<changes>` only
    try_notes(llm_notes, diff)         → `sbmig.script.notes.merge_notes` (workstream A: lines from the diff, a diff
                                       note for every file the LLM did not describe; `notes_from_diff` without LLM
                                       notes); a minimal fallback when they are missing
    note_line(note)                    `module/comp-jar/pom.xml +42–47 — jakarta.xml.bind-api 와 … 추가합니다`
                                       (`sbmig.errors.common.note_text`: one format for errors.md, report.md, the log)

Never raises: a broken helper falls back, the fallback on a garbled diff returns what it could read.
"""
from __future__ import annotations

import importlib
import logging
import re
from typing import Any, Callable, Optional

from sbmig.core.models import EditNote

log = logging.getLogger("sbmig.loop")

_HELPER_MODULES = ("sbmig.script.notes", "sbmig.script.edits", "sbmig.script")
_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _helper(name: str) -> Optional[Callable[..., Any]]:
    for mod in _HELPER_MODULES:
        try:
            fn = getattr(importlib.import_module(mod), name, None)
        except Exception:  # noqa: BLE001 - not there (yet) / broken: the next one, then the fallback
            continue
        if callable(fn):
            return fn
    return None


def note_line(n: Any) -> str:
    try:
        from sbmig.errors.common import note_text
        return note_text(n)
    except Exception:  # noqa: BLE001 - the error-list package is part of sbmig; stay defensive
        return f"{getattr(n, 'path', '-')} {getattr(n, 'lines', '')} — {getattr(n, 'change', '')}".strip()


def _norm(p: str) -> str:
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _one(text: Any) -> str:
    return " ".join(str(text or "").split())


def answer_notes(ans: Any, *, edits: bool) -> list[EditNote]:
    """The LLM's descriptions of the change that was applied. `edits` (the answer's `<edits>` were applied) → the
    `<changes>` notes and every edit's `desc` for a file they do not describe (A's `answer_notes(changes, edits)`;
    the fixer's parser has usually merged them into `answer.changes` already — the call is idempotent); otherwise
    (the answer's script ran) the `<changes>` notes without the edits' own descriptions (those edits were not
    applied). None / nothing described → []."""
    if ans is None:
        return []
    changes: list[EditNote] = []
    for c in getattr(ans, "changes", None) or []:
        try:
            changes.append(c if isinstance(c, EditNote) else EditNote.model_validate(c))
        except Exception:  # noqa: BLE001 - a malformed <changes> item
            continue
    eds = [e for e in getattr(ans, "edits", None) or [] if _one(getattr(e, "desc", "")) and getattr(e, "path", "")]
    if not edits:
        mine = {(_norm(e.path), _one(e.desc)) for e in eds}
        return [n for n in changes if (_norm(n.path), _one(n.change)) not in mine]
    fn = _helper("answer_notes")
    if fn is not None:
        try:
            got = fn(changes, list(getattr(ans, "edits", None) or []))
            if isinstance(got, list):
                return [n for n in got if isinstance(n, EditNote)]
        except Exception as e:  # noqa: BLE001 - fall back to the plain union below
            log.debug("answer_notes failed (%s: %s) — plain union", type(e).__name__, e)
    covered = {_norm(n.path) for n in changes}
    return changes + [EditNote(path=_norm(e.path), change=_one(e.desc), source="llm") for e in eds
                      if _norm(e.path) not in covered]


def try_notes(llm_notes: list[EditNote], diff: str) -> list[EditNote]:
    """The try's 수정 notes: A's `merge_notes(llm_notes, diff)` — `notes_from_diff(diff)` when the LLM described
    nothing (a reused stored script); without them (or when they fail) the fallback below."""
    llm_notes = list(llm_notes or [])
    fn = _helper("merge_notes") if llm_notes else _helper("notes_from_diff")
    if fn is not None:
        try:
            got = fn(llm_notes, diff or "") if llm_notes else fn(diff or "")
            if isinstance(got, list):
                return [n if isinstance(n, EditNote) else EditNote.model_validate(n) for n in got]
        except Exception as e:  # noqa: BLE001 - a broken helper never loses the try's record
            log.debug("merge_notes failed (%s: %s) — notes from the diff (fallback)", type(e).__name__, e)
    return _merge(list(llm_notes or []), diff or "")


# ---------------------------------------------------------------------------- fallback (A's helpers missing)
def _span(nums: list[int], mark: str) -> str:
    return f"{mark}{nums[0]}" + (f"–{nums[-1]}" if nums[-1] != nums[0] else "")


def _from_diff(diff: str) -> list[EditNote]:
    """One note per file of a unified diff: lines per hunk (`+42–47` added, `~12` changed, `-3` removed) and
    "N줄 추가, M줄 삭제" / "새 파일" / "파일 삭제"."""
    files: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    hunk: Optional[dict[str, Any]] = None
    old_no = new_no = 0

    def close() -> None:
        if cur is not None and hunk is not None and (hunk["add"] or hunk["rem"]):
            cur["spans"].append(_span(hunk["add"], "~" if hunk["rem"] else "+") if hunk["add"]
                                else _span(hunk["rem"], "-"))

    for line in (diff or "").splitlines():
        m = _GIT_RE.match(line)
        if m:
            close()
            hunk = None
            cur = {"path": m.group(2), "spans": [], "a": 0, "r": 0, "new": False, "gone": False}
            files.append(cur)
            continue
        if cur is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                cur["new"] = True
            elif line.startswith("deleted file mode"):
                cur["gone"] = True
        h = _HUNK_RE.match(line)
        if h:
            close()
            hunk, old_no, new_no = {"add": [], "rem": []}, int(h.group(1)), int(h.group(2))
            continue
        if hunk is None:
            continue
        if line.startswith("+"):
            hunk["add"].append(new_no)
            cur["a"] += 1
            new_no += 1
        elif line.startswith("-"):
            hunk["rem"].append(old_no)
            cur["r"] += 1
            old_no += 1
        elif not line.startswith("\\"):
            old_no += 1
            new_no += 1
    close()
    out = []
    for f in files:
        change = ("새 파일" + (f" ({f['a']}줄)" if f["a"] else "") if f["new"] else "파일 삭제" if f["gone"]
                  else ", ".join(x for x in ((f"{f['a']}줄 추가" if f["a"] else ""),
                                             (f"{f['r']}줄 삭제" if f["r"] else "")) if x) or "변경")
        out.append(EditNote(path=f["path"], change=change, lines=", ".join(f["spans"]), source="diff"))
    return out


def _merge(llm: list[EditNote], diff: str) -> list[EditNote]:
    """The LLM's notes with the diff's lines of their file (same path, or one path ends with the other), then a diff
    note for every changed file the LLM did not describe."""
    by_file = {n.path: n for n in _from_diff(diff)}
    out: list[EditNote] = []
    covered: set[str] = set()
    for n in llm:
        p = _norm(n.path)
        hit = by_file.get(p) or next((d for k, d in by_file.items() if p and (k.endswith("/" + p)
                                                                             or p.endswith("/" + k))), None)
        if hit is None:
            out.append(n.model_copy())
            continue
        covered.add(hit.path)
        out.append(n.model_copy(update={"path": hit.path, "lines": n.lines or hit.lines}))
    return out + [d for k, d in by_file.items() if k not in covered]


# ---------------------------------------------------------------------------- v2.1d dependency read-back
# (docs/INTERFACES.md "v2.1d"): what an applied try / the review commit changed in the poms, the major upgrades /
# library replacements among it, and the 운영자 확인 guide for them. The fix loop and the orchestrator share these;
# every call into `sbmig.build.depchanges` / `sbmig.core.jakarta_libraries` is best effort (a failure → DEBUG and the
# "nothing" value): nothing is ever reverted or blocked because of a major upgrade.
GUIDE_LOG_LINES = 8                  # `운영자 확인:` INFO lines per guide (then `… (+N)`)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+)")


def library_table(cfg: Any) -> list:
    """The shipped table + the people's `<kb>/libraries.yml` (`jakarta_libraries(company_path=…)`); [] on failure."""
    try:
        from sbmig.core.jakarta_libraries import company_libraries_file, jakarta_libraries
        return list(jakarta_libraries(company_path=company_libraries_file(cfg)) or [])
    except Exception as e:  # noqa: BLE001 - the table is a hint, never a reason to stop
        log.debug("library table unavailable: %s: %s", type(e).__name__, e)
        return []


def table_entry(key: str | None, cfg: Any) -> Any:
    """The table entry (`JakartaLibrary`) of `key` with the company file, None when unknown / on failure."""
    if not key:
        return None
    try:
        from sbmig.core.jakarta_libraries import company_libraries_file, jakarta_library_for
        return jakarta_library_for(key, company_path=company_libraries_file(cfg))
    except Exception as e:  # noqa: BLE001
        log.debug("table entry for %s unavailable: %s: %s", key, type(e).__name__, e)
        return None


def pom_changed(files: Any) -> bool:
    """Did the commit touch a pom.xml (the read-back only parses poms)?"""
    return any(_norm(str(f or "")).rsplit("/", 1)[-1] == "pom.xml" for f in (files or []))


def operator_changes(ws: Any, pre: str | None, sha: str | None, cfg: Any) -> list:
    """The `major` / `replaced` DependencyChange items of the commit `pre..sha` (`changes_in_commit`, then
    `apply_table` marks the ones a table entry allows). [] when git / the parser cannot tell; never raises."""
    if not pre or not sha:
        return []
    try:
        from sbmig.build.depchanges import changes_in_commit
        changes = list(changes_in_commit(ws, pre, sha) or [])
    except Exception as e:  # noqa: BLE001
        log.debug("dependency read-back %s..%s unavailable: %s: %s", str(pre)[:10], str(sha)[:10],
                  type(e).__name__, e)
        return []
    if not changes:
        return []
    try:
        from sbmig.build.depchanges import apply_table
        apply_table(changes, library_table(cfg))
    except Exception as e:  # noqa: BLE001 - not approved is the safe default
        log.debug("apply_table failed: %s: %s", type(e).__name__, e)
    out = []
    for c in changes:
        try:
            if c.needs_operator:
                out.append(c)
        except Exception:  # noqa: BLE001 - a foreign object without the property
            if getattr(c, "kind", "") in ("major", "replaced"):
                out.append(c)
    return out


def fallback_guide(changes: list, lang: str = "ko") -> str:
    """sbmig's guide when the LLM gave none: `synthesized_guide` (sbmig.build.depchanges), else one line per change."""
    try:
        from sbmig.build.depchanges import synthesized_guide
        text = str(synthesized_guide(list(changes), lang=lang) or "").strip()
        if text:
            return text
    except Exception as e:  # noqa: BLE001
        log.debug("synthesized_guide failed: %s: %s", type(e).__name__, e)
    out = []
    for c in changes:
        what = getattr(c, "text", None) or f"{getattr(c, 'key', '?')}"
        if lang == "en":
            out.append(f"{what} ({getattr(c, 'path', '-')}): major version crossed — check the configuration files, "
                       "registrations (web.xml / filters / listeners), renamed packages and the runtime behaviour")
        else:
            out.append(f"{what} ({getattr(c, 'path', '-')}): 메이저 버전이 바뀜 — 설정 파일, 등록(web.xml / 필터 / "
                       "리스너), 바뀐 패키지 이름, 실행 시 동작을 확인할 것")
    return "\n".join(out)


def guide_lines(guide: str | None) -> list[str]:
    """The guide as lines (bullets stripped, empty lines dropped)."""
    out = []
    for ln in str(guide or "").splitlines():
        t = _BULLET_RE.sub("", ln).strip()
        if t:
            out.append(t)
    return out


def approved_by(change: Any, cfg: Any) -> str:
    """The table file that approves `change` (`libraries.yml` for a company entry, `jakarta_libraries.yml` for a
    shipped one); "" when not approved."""
    if not getattr(change, "approved", False):
        return ""
    note = str(getattr(change, "note", "") or "")       # apply_table: "approved by the shipped / company table…"
    if "shipped table" in note:
        return "jakarta_libraries.yml"
    if "company table" in note:
        return "libraries.yml"
    for key in (getattr(change, "replaced_by", None), getattr(change, "key", None)):
        entry = table_entry(key, cfg)
        if entry is not None:
            return "jakarta_libraries.yml" if getattr(entry, "source", "company") == "shipped" else "libraries.yml"
    return "libraries.yml"


def log_major_upgrades(changes: list, guide: str, cfg: Any, offered: dict[str, str] | None = None) -> None:
    """One WARNING per change (`메이저 업그레이드: <text> (<pom>) — 운영자 확인 필요 [승인: libraries.yml]` /
    `[같은 메이저 jakarta 빌드 있음: 2.6.0]`), then the guide at INFO (`운영자 확인: …`, GUIDE_LOG_LINES lines)."""
    offered = offered or {}
    for c in changes:
        tail = ""
        src = approved_by(c, cfg)
        if src:
            tail = f" [승인: {src}]"
        elif offered.get(getattr(c, "key", "")):
            tail = f" [같은 메이저 jakarta 빌드 있음: {offered[c.key]}]"
        elif getattr(c, "note", ""):
            tail = f" [{_one(c.note)[:160]}]"
        log.warning("메이저 업그레이드: %s (%s) — 운영자 확인 필요%s", getattr(c, "text", getattr(c, "key", "?")),
                    getattr(c, "path", "-"), tail)
    lines = guide_lines(guide)
    for ln in lines[:GUIDE_LOG_LINES]:
        log.info("운영자 확인: %s", ln[:400])
    if len(lines) > GUIDE_LOG_LINES:
        log.info("운영자 확인: … (+%d)", len(lines) - GUIDE_LOG_LINES)
