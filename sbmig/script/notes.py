"""Edit notes (v2.1b, 2026-10-27): every modification of a try described for the business team — "edit 란에 llm 이
이렇게 xxx 를 xxx 하게 수정합니다". The LLM writes `where` / `change` (each `<edits>` item's `desc`, the `<changes>`
list for a script, `EditNote(source="llm")`); sbmig adds `lines` from the applied unified diff and, for every file the
diff touched that the LLM did not describe, a note generated from the diff itself (`source="diff"`), so the record
never shows a bare diff.

    answer_notes(changes, edits)          the LLM's notes of one answer: the `<changes>` list first, then each edit's
                                          `desc` for a path the list did not describe (the fix / review parsers)
    notes_from_diff(diff_text)            one note per file of a unified diff: `lines` "+42–47" added, "~12" changed
                                          (a run with both - and +), "-3" removed; `change` "pom.xml 42–47행 추가
                                          (6줄)" / "Foo.java 12행 수정" / "… 삭제"; new / deleted / renamed / binary
    merge_notes(llm_notes, diff_text)     the LLM's notes with `lines` from the diff (matched by path — a short path by
                                          its case-insensitive basename when that is unambiguous), plus the diff's
                                          notes for files the LLM did not mention; never empty when the diff touched
                                          a file

Line numbers: additions and changes count in the new file, removals in the old file (where the lines were). A diff
cut short (`errors.diff_chars`) is read as far as it goes. Never raises on odd input.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import EditNote

MAX_RUNS_SHOWN = 8                  # change runs listed per file in `lines` / `change` (then "… +N")
WHERE_CHARS = 300
CHANGE_CHARS = 600

_HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER_RE = re.compile(r'^diff --git (?:"?a/)(.*?)"? (?:"?b/)(.*?)"?$')
_OCTAL_RE = re.compile(r"\\([0-7]{3})")
_SIMPLE_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


def describe_language(cfg: Any) -> str:
    """The language of the change descriptions (`desc`, `<changes>`) as the prompts name it: Korean when
    `report.language` is `ko`; otherwise the language of the project's code comments, Korean when unsure (the business
    team reads the record)."""
    lang = str(getattr(getattr(cfg, "report", None), "language", "") or "").lower()
    if lang == "ko":
        return "Korean (한국어)"
    return ("the language of the project's code comments — Korean (한국어) when they are Korean or when unsure, "
            "English when they are English")


def one_line(text: Any, limit: int = CHANGE_CHARS) -> str:
    """`text` as one line (every whitespace run, newlines included, collapsed to one space), capped at `limit`."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _norm(path: str) -> str:
    p = (path or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def same_path(a: str, b: str) -> bool:
    """Two paths name the same file: equal ignoring case and `./` / `\\`, or the shorter one — with at least one
    folder — is a trailing part of the other (`comp-jar/pom.xml` vs `module/comp-jar/pom.xml`; a bare `pom.xml` is
    NOT the same as `module/comp-jar/pom.xml`: basenames are matched only where they are unambiguous)."""
    x, y = _norm(a).lower(), _norm(b).lower()
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    return "/" in short and long_.endswith("/" + short)


def _base(path: str) -> str:
    return posixpath.basename(_norm(path)).lower()


# ---------------------------------------------------------------------------
# the LLM's notes of one answer
# ---------------------------------------------------------------------------
def answer_notes(changes: Iterable[EditNote], edits: Iterable[Any]) -> list[EditNote]:
    """The LLM's notes of one answer: the `<changes>` list as given, then one note per edit with a `desc`
    (`EditNote(path, change=desc)`) for a path the `<changes>` list did not already describe. Order kept."""
    out = [x for x in (_as_note(n) for n in changes or ()) if x is not None]
    edits = list(edits or ())
    described = [n.path for n in out if n.path]
    edit_bases = [_base(str(getattr(e, "path", "") or "")) for e in edits]
    for e in edits:
        desc = one_line(getattr(e, "desc", "") or "")
        path = str(getattr(e, "path", "") or "")
        if not desc:
            continue
        if any(same_path(path, d) or ("/" not in _norm(d) and _base(d) == _base(path)
                                      and edit_bases.count(_base(path)) == 1) for d in described):
            continue
        if any(n.path == path and n.change == desc for n in out):
            continue                                       # the same edit twice
        out.append(EditNote(path=path, change=desc, source="llm"))
    return out


_PATH_KEYS = ("path", "file", "filename", "pom")
_WHERE_KEYS = ("where", "location", "position", "place", "section", "at", "line", "lines")
_CHANGE_KEYS = ("change", "what", "desc", "description", "summary", "how", "modification", "action", "edit", "note")
_LINE_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])?\s*`?(?P<path>[^\s`:]*[/.][^\s`:]*?)`?\s*"
                           r"(?P<sep>:|：|—|–|->|→|\s-\s|\s)\s*(?P<rest>\S.*)$")
MAX_NOTES = 60


def _pick(item: dict, keys: Sequence[str]) -> str:
    for k in keys:
        v = item.get(k)
        if v is None or isinstance(v, dict):
            continue
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v if x is not None and not isinstance(x, (dict, list)))
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and k in ("line", "lines"):
            v = f"{v}행 부근"
        text = one_line(v)
        if text:
            return text
    return ""


def _clean_path(raw: str) -> str:
    """The note's path as written, cleaned like an edit path (`./`, `\\`, quotes, a "(new file)" tail)."""
    p = one_line(raw, 400).strip("`'\"* ")
    p = re.sub(r"\s+\(.*\)$", "", p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def note_from_text(text: str) -> Optional[EditNote]:
    """One "path: what" / "path — what" / "path 의 … 수정합니다" line (a bullet list instead of JSON) → a note; the
    sentence is kept whole when the path is only its subject."""
    line = one_line(text)
    if not line:
        return None
    m = _LINE_ITEM_RE.match(line)
    if m and re.search(r"\.[A-Za-z0-9]{1,10}$|/", m.group("path")):
        sep = m.group("sep").strip()
        change = m.group("rest") if sep else line.lstrip("-*\u2022 ").strip()
        return EditNote(path=_clean_path(m.group("path")), change=one_line(change), source="llm")
    return EditNote(path="", change=line.lstrip("-*\u2022 ").strip(), source="llm")


def notes_from_json(obj: Any) -> tuple[list[EditNote], list[str]]:
    """The parsed `<changes>` JSON — a list of `{"path", "where", "change"}` (also `file` / `location` / `desc` …
    spellings, a `{"changes": [...]}` wrapper, one object, or plain strings) → (notes, problems). Items without any
    text are skipped with a problem; nothing is refused (the notes only describe)."""
    if isinstance(obj, dict):
        obj = obj["changes"] if isinstance(obj.get("changes"), list) else [obj]
    if obj is None:
        return [], []
    if isinstance(obj, str):
        obj = [ln for ln in obj.splitlines() if ln.strip()]
    if not isinstance(obj, list):
        return [], [(f"<changes> must be a JSON list of {{\"path\", \"where\", \"change\"}} objects, got "
                     f"{type(obj).__name__}")]
    out: list[EditNote] = []
    problems: list[str] = []
    for n, item in enumerate(obj, 1):
        if isinstance(item, str):
            note = note_from_text(item)
            if note is not None:
                out.append(note)
            continue
        if not isinstance(item, dict):
            problems.append(f"change {n}: not an object ({type(item).__name__})")
            continue
        path = _pick(item, _PATH_KEYS)
        where = _pick(item, _WHERE_KEYS)
        change = _pick(item, _CHANGE_KEYS)
        if not where and not change:
            problems.append(f"change {n}: no 'where' / 'change' text")
            continue
        out.append(EditNote(path=_clean_path(path), where=one_line(where, WHERE_CHARS),
                            change=one_line(change), source="llm"))
    if len(out) > MAX_NOTES:
        problems.append(f"{len(out) - MAX_NOTES} change(s) dropped (more than {MAX_NOTES})")
        out = out[:MAX_NOTES]
    return out, problems


# ---------------------------------------------------------------------------
# unified diff → notes
# ---------------------------------------------------------------------------
@dataclass
class _FileDiff:
    path: str = ""                        # the file as it is after the change (the old path for a deletion)
    old_path: str = ""
    new_file: bool = False
    deleted: bool = False
    renamed_from: str = ""
    binary: bool = False
    mode_only: bool = False
    runs: list[tuple[str, int, int]] = field(default_factory=list)   # (kind "+" | "~" | "-", first line, count)
    hunks: int = 0
    added: int = 0
    removed: int = 0

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.path, self.old_path, self.renamed_from) if p]


def _unquote(raw: str) -> str:
    """A git path as printed: `"a/\\355\\225\\234.java"` (core.quotepath) or a plain one with a trailing tab."""
    s = raw.rstrip("\t\r\n ")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        try:
            data = bytearray()
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "\\" and i + 1 < len(body):
                    m = _OCTAL_RE.match(body, i)
                    if m:
                        data.append(int(m.group(1), 8))
                        i += 4
                        continue
                    data += _SIMPLE_ESC.get(body[i + 1], body[i + 1]).encode("utf-8")
                    i += 2
                    continue
                data += ch.encode("utf-8")
                i += 1
            s = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - keep the text as printed
            s = body
    return s


def _side(raw: str, prefix: str) -> str:
    """`a/x/y.java` → `x/y.java`; `/dev/null` → ""."""
    s = _unquote(raw)
    if s == "/dev/null":
        return ""
    return s.removeprefix(prefix)


def _parse_diff(diff_text: str) -> list[_FileDiff]:
    """The files of a unified diff with their change runs. A hunk ends when its line counts are used up, so a removed
    line that reads "-- a/x" is never taken for a file header."""
    files: list[_FileDiff] = []
    cur: Optional[_FileDiff] = None
    old_ln = new_ln = old_left = new_left = 0
    run_plus = run_minus = run_start = run_old_start = 0

    def close_run() -> None:
        nonlocal run_plus, run_minus
        if cur is not None and (run_plus or run_minus):
            if run_plus and run_minus:
                cur.runs.append(("~", run_start, run_plus))
            elif run_plus:
                cur.runs.append(("+", run_start, run_plus))
            else:
                cur.runs.append(("-", run_old_start, run_minus))
        run_plus = run_minus = 0

    def new_file() -> _FileDiff:
        nonlocal cur
        close_run()
        cur = _FileDiff()
        files.append(cur)
        return cur

    for line in (diff_text or "").replace("\r\n", "\n").split("\n"):
        if cur is not None and (old_left > 0 or new_left > 0):          # inside a hunk
            tag = line[:1]
            if tag == "\\":                                          # "\ No newline at end of file"
                continue
            if tag in (" ", ""):                                     # context (an empty line: trailing space cut)
                close_run()
                old_ln, new_ln = old_ln + 1, new_ln + 1
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if tag in ("+", "-"):
                if not (run_plus or run_minus):
                    run_start, run_old_start = max(new_ln, 1), max(old_ln, 1)
                if tag == "+":
                    run_plus += 1
                    new_ln, new_left = new_ln + 1, new_left - 1
                    cur.added += 1
                else:
                    run_minus += 1
                    old_ln, old_left = old_ln + 1, old_left - 1
                    cur.removed += 1
                continue
            old_left = new_left = 0                                  # a header line: the hunk was cut short
        close_run()
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            f = new_file()
            m = _GIT_HEADER_RE.match(line)
            if m:
                f.old_path, f.path = _unquote(m.group(1)), _unquote(m.group(2))
            continue
        if line.startswith("--- "):
            if cur is None or cur.hunks:                             # a plain diff: `--- a/x` opens the next file
                new_file()
            assert cur is not None
            old = _side(line[4:], "a/")
            if old:
                cur.old_path = old
            else:
                cur.new_file = True
            continue
        if line.startswith("+++ "):
            if cur is None:
                new_file()
            assert cur is not None
            new = _side(line[4:], "b/")
            if new:
                cur.path = new
            else:
                cur.deleted = True
                cur.path = cur.old_path or cur.path
            continue
        m = _HUNK_RE.match(line)
        if m:
            if cur is None:
                new_file()
            assert cur is not None
            cur.hunks += 1
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
        elif line.startswith("deleted file mode"):
            cur.deleted = True
        elif line.startswith("rename from "):
            cur.renamed_from = _unquote(line[len("rename from "):])
        elif line.startswith(("rename to ", "copy to ")):
            cur.path = _unquote(line.split(" ", 2)[2])
        elif line.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        elif line.startswith(("old mode", "new mode")):
            cur.mode_only = True
    close_run()
    for f in files:
        if not f.path:
            f.path = f.old_path or f.renamed_from
        if f.renamed_from and f.old_path in ("", f.path):
            f.old_path = f.renamed_from
        if f.runs or f.binary or f.new_file or f.deleted or f.renamed_from:
            f.mode_only = False
    return [f for f in files if f.path]


def _span(first: int, count: int) -> str:
    return f"{first}" if count <= 1 else f"{first}–{first + count - 1}"


def _run_text(kind: str, first: int, count: int) -> str:
    word = {"+": "추가", "~": "수정", "-": "삭제"}[kind]
    tail = f" ({count}줄)" if count > 1 else ""
    return f"{_span(first, count)}행 {word}{tail}"


def _note_for(f: _FileDiff) -> EditNote:
    name = posixpath.basename(f.path) or f.path
    shown = f.runs[:MAX_RUNS_SHOWN]
    more = len(f.runs) - len(shown)
    lines = ", ".join(f"{k}{_span(a, n)}" for k, a, n in shown) + (f", … +{more}" if more else "")
    if f.deleted:
        change = f"{name} 파일 삭제" + (f" ({f.removed}줄)" if f.removed else "")
    elif f.new_file:
        change = f"{name} 새 파일 생성" + (f" ({f.added}줄)" if f.added else "")
    else:
        parts: list[str] = []
        if f.renamed_from:
            parts.append(f"{posixpath.basename(f.renamed_from) or f.renamed_from} → {name} 이동"
                         if posixpath.basename(f.renamed_from) != name else f"{f.renamed_from} → {f.path} 이동")
        if f.binary:
            parts.append(f"{name} 바이너리 파일 변경")
        if shown:
            runs = ", ".join(_run_text(k, a, n) for k, a, n in shown) + (f" 외 {more}곳" if more else "")
            parts.append(f"{name} {runs}" if not f.renamed_from else runs)
        if f.mode_only and not parts:
            parts.append(f"{name} 파일 권한 변경")
        change = ", ".join(parts) or f"{name} 변경"
    return EditNote(path=f.path, where="", change=one_line(change), lines=lines, source="diff")


def notes_from_diff(diff_text: str) -> list[EditNote]:
    """One `EditNote(source="diff")` per file of a unified (git) diff, in diff order — see the module docstring.
    "" / no file → []."""
    try:
        return [_note_for(f) for f in _parse_diff(diff_text or "")]
    except Exception:  # noqa: BLE001 - a note generator must never break a try's record
        return []


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
def _match(note_path: str, files: Sequence[_FileDiff]) -> Optional[int]:
    """Index of the diff file an LLM note names: exact (case-insensitive) first, then a trailing-part match, then the
    case-insensitive basename — each only when exactly one file fits."""
    want = _norm(note_path).lower()
    if not want:
        return None
    for test in (lambda p: _norm(p).lower() == want,
                 lambda p: same_path(p, want),
                 lambda p: _base(p) == posixpath.basename(want)):
        hits = [i for i, f in enumerate(files) if any(test(p) for p in f.paths)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            exact = [i for i in hits if _norm(files[i].path).lower() == want]
            if len(exact) == 1:
                return exact[0]
            return None                                     # ambiguous ("pom.xml" with three changed poms)
    return None


def _as_note(n: Any) -> Optional[EditNote]:
    if isinstance(n, EditNote):
        return n
    if isinstance(n, dict):
        try:
            return EditNote.model_validate(n)
        except Exception:  # noqa: BLE001
            return None
    return None


def merge_notes(llm_notes: Iterable[EditNote], diff_text: str) -> list[EditNote]:
    """The record's notes of one try: the LLM's notes (copies) with `lines` from the diff — matched by path; a short
    path is matched by its basename when only one changed file has it, and then takes the diff's full path — followed
    by the diff's own notes for every changed file no LLM note named. Never empty when the diff touched a file;
    without a diff the LLM's notes come back as they are."""
    llm = [x for x in (_as_note(n) for n in llm_notes or ()) if x is not None]
    try:
        files = _parse_diff(diff_text or "")
    except Exception:  # noqa: BLE001
        files = []
    out: list[EditNote] = []
    seen: set[tuple[str, str, str]] = set()
    claimed: set[int] = set()
    for n in llm:
        key = (_norm(n.path).lower(), one_line(n.where), one_line(n.change))
        if key in seen:
            continue
        seen.add(key)
        i = _match(n.path, files) if files else None
        if i is None:
            out.append(n.model_copy())
            continue
        claimed.add(i)
        f = files[i]
        out.append(n.model_copy(update={"path": f.path, "lines": _note_for(f).lines or n.lines}))
    for i, f in enumerate(files):
        if i not in claimed:
            out.append(_note_for(f))
    return out


__all__ = ["answer_notes", "describe_language", "merge_notes", "note_from_text", "notes_from_diff", "notes_from_json", "one_line",
           "same_path"]
