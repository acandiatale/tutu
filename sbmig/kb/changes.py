"""How a change looks textually — text replace or code edit — independent of who made it (error cases, 2026-10-23).

``classify_diff(diff_text)`` → :class:`ChangeClass`. The business wants to tell LLM fixes that were only text
replaces (``clientIp`` → ``remoteClientIp``, ``javax`` → ``jakarta``, ``1.0.0`` → ``3.0.0``) from real code edits,
so that the former can become deterministic rules / commands later.

Input: a git unified diff (``diff --git``, ``new file mode``, ``deleted file mode``, ``rename from`` / ``rename to``,
``@@`` hunks, ``\\ No newline at end of file``) or the format of :func:`sbmig.kb.harvest.changes_as_diff` (unified
hunks with ``a/`` / ``b/`` headers plus ``=== CREATE`` / ``=== DELETE`` / ``=== RENAME a -> b`` /
``=== MODIFY path (no textual change)`` blocks).

Per hunk, every run of ``-`` lines is paired with the run of ``+`` lines that follows it when the counts are equal
(blank lines left out); a line pair is tokenized whitespace-insensitively and compared with
``difflib.SequenceMatcher``: only ``replace`` opcodes → a text replace (one :class:`ReplacePair` per replaced span,
in its original spelling); a token insert / delete, or runs of different length → a code edit; whitespace-only
changes are ignored. Version strings (``1.0.0``, ``5.3.31.RELEASE``, ``3.0.0-M1``) are single tokens so a version
bump reads ``1.0.0`` → ``3.0.0`` rather than ``1`` → ``3``.

Also: :func:`diff_for_file` (the section of one file, matched by path suffix — the error's diff excerpt) and
:func:`file_changes_from_diff` (FileChanges rebuilt from the hunks, so :func:`sbmig.kb.labels.label_changes` can
label a fix of which only the diff is known). All three are pure and never raise.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from sbmig.core.models import ChangeClass, FileChange, ReplacePair

log = logging.getLogger("sbmig.kb")

# version-ish atoms first (a version bump is one replaced span), then words, then single punctuation characters
_TOKEN_RE = re.compile(r"\d+(?:\.\w+)+(?:-\w+(?:\.\w+)*)*|\w+|[^\w\s]")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BLOCK_RE = re.compile(r"^=== (CREATE|DELETE|RENAME|MODIFY) (.+?)\s*$")
_NO_TEXT_CHANGE = " (no textual change)"
MAX_TOKENS_PER_LINE = 400          # minified / generated lines: compared for equality only (SequenceMatcher cost)

FILE_OPS = ("file_create", "file_delete", "file_rename")


@dataclass
class _Hunk:
    lines: list[tuple[str, str]] = field(default_factory=list)       # (" " | "-" | "+", text)


@dataclass
class _FileDiff:
    path: str = ""
    old_path: str = ""
    op: str = "modify"                    # modify | create | delete | rename
    git: bool = False                     # opened by a `diff --git` header
    seen_minus: bool = False              # its `---` header was read
    binary: bool = False
    hunks: list[_Hunk] = field(default_factory=list)
    body: list[str] = field(default_factory=list)                   # the raw lines of this section

    @property
    def main_path(self) -> str:
        return self.path or self.old_path


# --------------------------------------------------------------------------- parsing
def _norm_path(p: str) -> str:
    p = (p or "").strip()
    if "\t" in p:                          # `--- a/x<TAB>2024-01-01 …` from diff tools with timestamps
        p = p.split("\t", 1)[0]
    if len(p) >= 2 and p[0] == p[-1] == '"':
        p = p[1:-1]
    p = p.replace("\\", "/")
    if p.startswith(("a/", "b/")):
        p = p[2:]
    while p.startswith("./"):
        p = p[2:]
    return p


def _git_header_paths(rest: str) -> tuple[str, str]:
    rest = rest.strip()
    if rest.startswith("a/") and " b/" in rest:
        n = len(rest) - 5                  # symmetric "a/P b/P" (the common case; P may contain " b/")
        if n > 0 and n % 2 == 0:
            left, right = rest[2:2 + n // 2], rest[2 + n // 2 + 3:]
            if left == right and rest[2 + n // 2:2 + n // 2 + 3] == " b/":
                return left, right
        m = re.match(r"^a/(.+?) b/(.+)$", rest)
        if m:
            return _norm_path(m.group(1)), _norm_path(m.group(2))
    m = re.match(r'^"(.+?)" "(.+)"$', rest)
    if m:
        return _norm_path(m.group(1)), _norm_path(m.group(2))
    parts = rest.split(" ")
    return (_norm_path(parts[0]), _norm_path(parts[-1])) if parts else ("", "")


def _parse(text: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    cur: Optional[_FileDiff] = None
    hunk: Optional[_Hunk] = None
    old_left = new_left = 0
    in_create = False

    def open_file(**kw) -> _FileDiff:
        fd = _FileDiff(**kw)
        files.append(fd)
        return fd

    for raw in (ln.rstrip("\r") for ln in (text or "").split("\n")):
        # ---- inside a hunk: the @@ counts decide what is content (a removed "-- a/x" line is not a header)
        if hunk is not None and cur is not None and (old_left > 0 or new_left > 0):
            if raw.startswith("\\"):
                cur.body.append(raw)
                continue
            tag = raw[:1]
            if tag in (" ", "") and old_left > 0 and new_left > 0:
                hunk.lines.append((" ", raw[1:]))
                old_left -= 1
                new_left -= 1
                cur.body.append(raw)
                continue
            if tag == "-" and old_left > 0:
                hunk.lines.append(("-", raw[1:]))
                old_left -= 1
                cur.body.append(raw)
                continue
            if tag == "+" and new_left > 0:
                hunk.lines.append(("+", raw[1:]))
                new_left -= 1
                cur.body.append(raw)
                continue
            hunk, old_left, new_left = None, 0, 0           # truncated / malformed hunk: read as a header
        if in_create and cur is not None:
            if raw.startswith("+"):
                cur.hunks[-1].lines.append(("+", raw[1:]))
                cur.body.append(raw)
                continue
            in_create = False

        if raw.startswith("diff --git "):
            a, b = _git_header_paths(raw[len("diff --git "):])
            cur = open_file(path=b or a, old_path=a, git=True)
            cur.body.append(raw)
            hunk = None
            continue
        m = _BLOCK_RE.match(raw)
        if m:
            kind, rest = m.group(1), m.group(2)
            hunk = None
            if kind == "CREATE":
                cur = open_file(path=_norm_path(rest), op="create")
                cur.hunks.append(_Hunk())
                in_create = True
            elif kind == "DELETE":
                cur = open_file(path=_norm_path(rest), old_path=_norm_path(rest), op="delete")
            elif kind == "RENAME":
                a, _, b = rest.partition(" -> ")
                cur = open_file(path=_norm_path(b or a), old_path=_norm_path(a), op="rename")
            else:
                p = rest.removesuffix(_NO_TEXT_CHANGE)
                cur = open_file(path=_norm_path(p), old_path=_norm_path(p))
            cur.body.append(raw)
            continue
        if raw.startswith("--- "):
            p = raw[4:]
            if cur is None or not cur.git or cur.seen_minus:
                cur = open_file()                            # harvest format: every `---` starts a file
            cur.seen_minus = True
            if p.strip() == "/dev/null":
                if cur.op != "rename":
                    cur.op = "create"
            else:
                cur.old_path = _norm_path(p)
                if not cur.path:
                    cur.path = cur.old_path
            cur.body.append(raw)
            continue
        if raw.startswith("+++ "):
            p = raw[4:]
            if cur is None:
                cur = open_file()
            if p.strip() == "/dev/null":
                cur.op = "delete"
                cur.path = cur.path or cur.old_path
            else:
                cur.path = _norm_path(p)
            cur.body.append(raw)
            continue
        m = _HUNK_RE.match(raw)
        if m:
            if cur is None:
                cur = open_file()
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            hunk = _Hunk()
            cur.hunks.append(hunk)
            cur.body.append(raw)
            continue
        if cur is None:
            continue
        if raw.startswith("\\"):
            cur.body.append(raw)
            continue
        if cur.git and not cur.hunks:
            if raw.startswith("new file mode"):
                cur.op = "create"
            elif raw.startswith("deleted file mode"):
                cur.op = "delete"
            elif raw.startswith("rename from "):
                cur.old_path, cur.op = _norm_path(raw[len("rename from "):]), "rename"
            elif raw.startswith("rename to "):
                cur.path, cur.op = _norm_path(raw[len("rename to "):]), "rename"
            elif raw.startswith("copy to "):
                cur.path, cur.op = _norm_path(raw[len("copy to "):]), "create"
            elif raw.startswith(("Binary files ", "GIT binary patch")):
                cur.binary = True
            elif not raw.startswith(("index ", "similarity index", "dissimilarity index", "old mode", "new mode",
                                     "copy from ")):
                continue
            cur.body.append(raw)
    return [f for f in files if f.main_path]


# --------------------------------------------------------------------------- classification
def _tokens(line: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(line)]


def _squash(lines: list[str]) -> str:
    return "".join("".join(x.split()) for x in lines)


def _classify_pair(old: str, new: str, kinds: set[str], pairs: list[tuple[str, str]]) -> None:
    ta, tb = _tokens(old), _tokens(new)
    wa, wb = [t[0] for t in ta], [t[0] for t in tb]
    if wa == wb:
        return                                               # whitespace-only
    if len(wa) > MAX_TOKENS_PER_LINE or len(wb) > MAX_TOKENS_PER_LINE or not wa or not wb:
        kinds.add("code_edit")
        return
    ops = [op for op in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes() if op[0] != "equal"]
    if any(op[0] != "replace" for op in ops):
        kinds.add("code_edit")
        return
    for _, i1, i2, j1, j2 in ops:
        pairs.append((old[ta[i1][1]:ta[i2 - 1][2]], new[tb[j1][1]:tb[j2 - 1][2]]))
    kinds.add("text_replace")


def _blocks(lines: list[tuple[str, str]]) -> list[tuple[list[str], list[str]]]:
    """Runs of `-` lines with the run of `+` lines that follows them (either may be empty)."""
    out: list[tuple[list[str], list[str]]] = []
    minus: list[str] = []
    plus: list[str] = []
    for tag, text in lines:
        if tag == "-":
            if plus:
                out.append((minus, plus))
                minus, plus = [], []
            minus.append(text)
        elif tag == "+":
            plus.append(text)
        else:
            if minus or plus:
                out.append((minus, plus))
            minus, plus = [], []
    if minus or plus:
        out.append((minus, plus))
    return out


def _classify_block(minus: list[str], plus: list[str], kinds: set[str], pairs: list[tuple[str, str]]) -> None:
    m = [x for x in minus if x.strip()]
    p = [x for x in plus if x.strip()]
    if not m and not p:
        return                                               # blank lines only
    if len(m) != len(p):
        if _squash(m) != _squash(p):                         # not just a re-wrapped line
            kinds.add("code_edit")
        return
    for a, b in zip(m, p):
        _classify_pair(a, b, kinds, pairs)


def _file_kinds(fd: _FileDiff) -> tuple[set[str], list[tuple[str, str]]]:
    if fd.op == "create":
        return {"file_create"}, []
    if fd.op == "delete":
        return {"file_delete"}, []
    kinds: set[str] = set()
    pairs: list[tuple[str, str]] = []
    if fd.binary:
        kinds.add("code_edit")
    for h in fd.hunks:
        for minus, plus in _blocks(h.lines):
            _classify_block(minus, plus, kinds, pairs)
    if fd.op == "rename" and not kinds:
        kinds.add("file_rename")                             # a rename with edits counts as its edits
    return kinds, pairs


def file_type(path: str) -> str:
    name = PurePosixPath((path or "").replace("\\", "/")).name.lower()
    if name.endswith(".java"):
        return "java"
    if name == "pom.xml":
        return "pom"
    if name.endswith(".xml"):
        return "xml"
    if name.endswith(".properties"):
        return "properties"
    if name.endswith((".yml", ".yaml")):
        return "yaml"
    if name.endswith((".jsp", ".jspf", ".tag", ".tagx")):
        return "jsp"
    return "other"


def classify_diff(diff_text: str) -> ChangeClass:
    """Text replace / code edit / file operations of a diff (see the module doc). Pure, never raises."""
    try:
        files = _parse(diff_text or "")
        if not files:
            return ChangeClass()
        all_kinds: set[str] = set()
        agg: dict[tuple[str, str], ReplacePair] = {}
        paths: list[str] = []
        added = removed = 0
        for fd in files:
            path = fd.main_path
            if path not in paths:
                paths.append(path)
            for h in fd.hunks:
                for tag, _ in h.lines:
                    if tag == "+":
                        added += 1
                    elif tag == "-":
                        removed += 1
            kinds, pairs = _file_kinds(fd)
            all_kinds |= kinds
            for old, new in pairs:
                rp = agg.get((old, new))
                if rp is None:
                    rp = agg[(old, new)] = ReplacePair(old=old, new=new, count=0)
                rp.count += 1
                if path not in rp.files:
                    rp.files.append(path)
        if not all_kinds:
            kind = "none"
        elif len(all_kinds) == 1:
            kind = next(iter(all_kinds))
        else:
            kind = "mixed"
        order = {k: i for i, k in enumerate(agg)}
        pairs_sorted = sorted(agg.values(), key=lambda rp: (-rp.count, order[(rp.old, rp.new)]))
        types: list[str] = []
        for p in paths:
            t = file_type(p)
            if t not in types:
                types.append(t)
        return ChangeClass(change_kind=kind, replace_pairs=pairs_sorted, file_types=types, files=paths,
                           lines_added=added, lines_removed=removed)
    except Exception as e:  # pragma: no cover - defensive: classification is best effort
        log.debug("classify_diff failed: %s: %s", type(e).__name__, e)
        return ChangeClass()


def combine_changes(changes: list[ChangeClass]) -> ChangeClass:
    """Several changes read as one (the attempts of one resolution, 2026-10-25): kinds as in ``classify_diff`` (one
    kind → that kind, several → ``mixed``, ``none`` ignored), replace pairs merged (counts summed, first-seen order
    on ties), files / file types in first-seen order, line counts summed. Pure, never raises."""
    try:
        kinds = {c.change_kind for c in changes if c.change_kind != "none"}
        kind = "none" if not kinds else next(iter(kinds)) if len(kinds) == 1 else "mixed"
        agg: dict[tuple[str, str], ReplacePair] = {}
        files: list[str] = []
        types: list[str] = []
        for c in changes:
            for rp in c.replace_pairs:
                cur = agg.get((rp.old, rp.new))
                if cur is None:
                    agg[(rp.old, rp.new)] = rp.model_copy(update={"files": list(rp.files)})
                else:
                    cur.count += rp.count
                    cur.files += [f for f in rp.files if f not in cur.files]
            files += [f for f in c.files if f not in files]
            types += [t for t in c.file_types if t not in types]
        order = {k: i for i, k in enumerate(agg)}
        pairs = sorted(agg.values(), key=lambda rp: (-rp.count, order[(rp.old, rp.new)]))
        return ChangeClass(change_kind=kind, replace_pairs=pairs, file_types=types, files=files,
                           lines_added=sum(c.lines_added for c in changes),
                           lines_removed=sum(c.lines_removed for c in changes))
    except Exception as e:  # noqa: BLE001  # pragma: no cover - defensive
        log.debug("combine_changes failed: %s: %s", type(e).__name__, e)
        return ChangeClass()


# --------------------------------------------------------------------------- per-file helpers
def path_matches(a: Optional[str], b: Optional[str]) -> bool:
    """Same file, compared by path suffix (`src/main/java/X.java` matches `demo-back/src/main/java/X.java`)."""
    a, b = _norm_path(a or ""), _norm_path(b or "")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def diff_for_file(diff_text: str, path: Optional[str]) -> str:
    """The section(s) of ``diff_text`` for ``path`` (matched by path suffix, old or new path), "" when none."""
    try:
        if not path:
            return ""
        parts = ["\n".join(fd.body) for fd in _parse(diff_text or "")
                 if path_matches(fd.path, path) or path_matches(fd.old_path, path)]
        return "\n".join(p for p in parts if p)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("diff_for_file failed: %s: %s", type(e).__name__, e)
        return ""


def file_changes_from_diff(diff_text: str) -> list[FileChange]:
    """FileChanges rebuilt from the hunks (one search/replace per hunk: context + removed / context + added) —
    enough for :func:`sbmig.kb.labels.label_changes` when only the diff of a fix is known. Never raises."""
    out: list[FileChange] = []
    try:
        for fd in _parse(diff_text or ""):
            if fd.op == "create":
                body = [t for h in fd.hunks for tag, t in h.lines if tag != "-"]
                out.append(FileChange(path=fd.main_path, op="create", content="\n".join(body)))
                continue
            if fd.op == "delete":
                body = [t for h in fd.hunks for tag, t in h.lines if tag != "+"]
                out.append(FileChange(path=fd.main_path, op="delete", content="\n".join(body) or None))
                continue
            edits = []
            for h in fd.hunks:
                search = "\n".join(t for tag, t in h.lines if tag in (" ", "-"))
                replace = "\n".join(t for tag, t in h.lines if tag in (" ", "+"))
                if search != replace:
                    edits.append(FileChange(path=fd.main_path, op="modify", search=search, replace=replace))
            if fd.op == "rename" and not edits:
                out.append(FileChange(path=fd.old_path or fd.main_path, op="rename", new_path=fd.main_path))
            out.extend(edits)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("file_changes_from_diff failed: %s: %s", type(e).__name__, e)
    return out
