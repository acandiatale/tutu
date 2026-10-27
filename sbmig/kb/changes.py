"""How a change looks textually — text replace or code edit — independent of who made it (error cases, 2026-10-23).

``classify_diff(diff_text)`` → :class:`ChangeClass`. The business wants to tell LLM fixes that were only text
replaces (``clientIp`` → ``remoteClientIp``, ``javax`` → ``jakarta``, ``1.0.0`` → ``3.0.0``) from real code edits,
so that the former can become deterministic rules / commands later.

Input: a git unified diff (``diff --git``, ``new file mode``, ``deleted file mode``, ``rename from`` / ``rename to``,
``@@`` hunks, ``\\ No newline at end of file``) or the format of :func:`sbmig.kb.common.changes_as_diff` (unified
hunks with ``a/`` / ``b/`` headers plus ``=== CREATE`` / ``=== DELETE`` / ``=== RENAME a -> b`` /
``=== MODIFY path (no textual change)`` blocks).

Per hunk, every run of ``-`` lines is paired with the run of ``+`` lines that follows it when the counts are equal
(blank lines left out); a line pair is tokenized whitespace-insensitively and compared with
``difflib.SequenceMatcher``: only ``replace`` opcodes → a text replace (one :class:`ReplacePair` per replaced span,
in its original spelling); a token insert / delete, or runs of different length → a code edit; whitespace-only
changes are ignored. Version strings (``1.0.0``, ``5.3.31.RELEASE``, ``3.0.0-M1``) are single tokens so a version
bump reads ``1.0.0`` → ``3.0.0`` rather than ``1`` → ``3``.

Qualified names (round 10, 2026-10-26): a dotted name that looks like a package / FQCN (lower-case leading segments
before a class segment — ``javax.xml.bind.JAXBContext`` — or a whole package in ``import a.b.*;`` /
``package a.b;``) is one token, and a change inside its package part becomes ONE pair of the full package parts
(``javax.xml.bind`` → ``jakarta.xml.bind``; class segments common to both are dropped, different ones kept:
``org.a.Old`` → ``org.b.New``). The old single-token pair ``javax`` → ``jakarta`` rewrote every ``javax.*`` import
(``javax.sql``, ``javax.xml.parsers``) when handler ``case-replace`` applied it. A package part learned with two
different targets in one change (``…commons.lang`` → ``lang3`` and → ``text``) falls back to the full names. A
member / identifier rename (``req.clientIp()`` → ``req.remoteClientIp()``, ``Helper.clientIp`` → ``Helper.ip``)
stays a single-token pair.

Secrets: a replace pair whose text touches a secret value (:mod:`sbmig.kb.redact`, line-local) is dropped —
:func:`classify_diff_ex` returns how many were.

Also: :func:`diff_for_file` (the section of one file, matched by path suffix), :func:`excerpt_for` (that section cut
down to the hunk(s) containing / nearest an error line, within a size limit — the error's diff excerpt) and
:func:`file_changes_from_diff` (FileChanges rebuilt from the hunks, so :func:`sbmig.kb.labels.label_changes` can
label a fix of which only the diff is known). All are pure and never raise.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Optional

from sbmig.core.models import ChangeClass, FileChange, ReplacePair

from .redact import Redactor, get_redactor, overlaps

log = logging.getLogger("sbmig.kb")

# version-ish atoms first (a version bump is one replaced span), then dotted names (split again unless they look like
# a package / FQCN, see _tokens), then words, then single punctuation characters
_TOKEN_RE = re.compile(r"\d+(?:\.\w+)+(?:-\w+(?:\.\w+)*)*|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+|\w+|[^\w\s]")
_SEGMENT_RE = re.compile(r"\w+|\.")
_PKG_STATEMENT_RE = re.compile(r"^\s*(?:import(?:\s+static)?|package)\s+$")
TRUNCATED = "…[truncated]"
_UNSET: object = object()
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BLOCK_RE = re.compile(r"^=== (CREATE|DELETE|RENAME|MODIFY) (.+?)\s*$")
_NO_TEXT_CHANGE = " (no textual change)"
MAX_TOKENS_PER_LINE = 400          # minified / generated lines: compared for equality only (SequenceMatcher cost)

FILE_OPS = ("file_create", "file_delete", "file_rename")


@dataclass
class _Hunk:
    lines: list[tuple[str, str]] = field(default_factory=list)       # (" " | "-" | "+", text)
    old_start: Optional[int] = None       # from the `@@ -a,b +c,d @@` header (None: a changes_as_diff CREATE block)
    old_len: int = 0
    raw: list[str] = field(default_factory=list)                    # the header + content lines as written


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
    head: Optional[list[str]] = None                                # the raw lines before its first hunk

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
                hunk.raw.append(raw)
                continue
            tag = raw[:1]
            if tag in (" ", "") and old_left > 0 and new_left > 0:
                hunk.lines.append((" ", raw[1:]))
                old_left -= 1
                new_left -= 1
                cur.body.append(raw)
                hunk.raw.append(raw)
                continue
            if tag == "-" and old_left > 0:
                hunk.lines.append(("-", raw[1:]))
                old_left -= 1
                cur.body.append(raw)
                hunk.raw.append(raw)
                continue
            if tag == "+" and new_left > 0:
                hunk.lines.append(("+", raw[1:]))
                new_left -= 1
                cur.body.append(raw)
                hunk.raw.append(raw)
                continue
            hunk, old_left, new_left = None, 0, 0           # truncated / malformed hunk: read as a header
        if in_create and cur is not None:
            if raw.startswith("+"):
                cur.hunks[-1].lines.append(("+", raw[1:]))
                cur.hunks[-1].raw.append(raw)
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
                cur.head = [raw]
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
                cur = open_file()                            # changes_as_diff format: every `---` starts a file
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
            hunk = _Hunk(old_start=int(m.group(1)), old_len=old_left, raw=[raw])
            if cur.head is None:
                cur.head = list(cur.body)
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
def _pkg_len(segs: list[str], whole_package: bool) -> int:
    """Leading package segments of a dotted name: the lower-case ones before the first class (upper-case) segment;
    all of them in a package context (``import a.b.*;`` / ``package a.b;``); 0 = not a package-qualified name."""
    n = 0
    for s in segs:
        if not (s[:1].islower() or s[:1] == "_"):
            break
        n += 1
    if n == len(segs):
        return n if whole_package else 0               # `helper.clientIp`, `this.foo.bar`: a member chain
    return n


def _package_context(line: str, start: int, end: int) -> bool:
    return line[end:end + 2] == ".*" or bool(_PKG_STATEMENT_RE.match(line[:start]))


def _tokens(line: str) -> list[tuple[str, int, int]]:
    """(text, start, end); a dotted name stays one token only when it is package-qualified (module doc)."""
    out: list[tuple[str, int, int]] = []
    for m in _TOKEN_RE.finditer(line):
        text, s, e = m.group(0), m.start(), m.end()
        if "." in text and not text[:1].isdigit():
            segs = text.split(".")
            if not _pkg_len(segs, _package_context(line, s, e)):
                out += [(x.group(0), s + x.start(), s + x.end()) for x in _SEGMENT_RE.finditer(text)]
                continue
        out.append((text, s, e))
    return out


def _qualified(text: str) -> bool:
    return "." in text and not text[:1].isdigit() and bool(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", text))


def _reduce_qualified(old: str, new: str, old_pkg_ctx: bool, new_pkg_ctx: bool
                      ) -> Optional[list[tuple[str, str, Optional[str], Optional[str]]]]:
    """Pairs for one replaced qualified name: the package parts when only they differ, the full names when the
    class parts differ too, single-segment pairs when only the class / member part differs (same package); None =
    a segment was inserted / removed there (a code edit)."""
    so, sn = old.split("."), new.split(".")
    po, pn = max(1, _pkg_len(so, old_pkg_ctx)), max(1, _pkg_len(sn, new_pkg_ctx))
    if so[:po] == sn[:pn]:
        ops = [op for op in difflib.SequenceMatcher(None, so[po:], sn[pn:], autojunk=False).get_opcodes()
               if op[0] != "equal"]
        if any(op[0] != "replace" for op in ops):
            return None
        return [(".".join(so[po + i1:po + i2]), ".".join(sn[pn + j1:pn + j2]), None, None)
                for _, i1, i2, j1, j2 in ops]
    if so[po:] == sn[pn:]:
        return [(".".join(so[:po]), ".".join(sn[:pn]), old, new)]
    return [(old, new, None, None)]


def _squash(lines: list[str]) -> str:
    return "".join("".join(x.split()) for x in lines)


@dataclass
class _Acc:
    """What a classification collects: kinds, (old, new, full old, full new) pairs, pairs dropped as secrets."""
    kinds: set = field(default_factory=set)
    pairs: list = field(default_factory=list)
    dropped: int = 0
    red: Optional[Redactor] = None


def _classify_pair(old: str, new: str, acc: _Acc) -> None:
    ta, tb = _tokens(old), _tokens(new)
    wa, wb = [t[0] for t in ta], [t[0] for t in tb]
    if wa == wb:
        return                                               # whitespace-only
    if len(wa) > MAX_TOKENS_PER_LINE or len(wb) > MAX_TOKENS_PER_LINE or not wa or not wb:
        acc.kinds.add("code_edit")
        return
    ops = [op for op in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes() if op[0] != "equal"]
    if any(op[0] != "replace" for op in ops):
        acc.kinds.add("code_edit")
        return
    found: list[tuple[str, str, Optional[str], Optional[str]]] = []
    sa = acc.red.spans(old) if acc.red is not None else []
    sb = acc.red.spans(new) if acc.red is not None else []
    for _, i1, i2, j1, j2 in ops:
        os_, oe, ns, ne = ta[i1][1], ta[i2 - 1][2], tb[j1][1], tb[j2 - 1][2]
        if overlaps(sa, os_, oe) or overlaps(sb, ns, ne):
            acc.dropped += 1                                 # the pair would carry a secret value
            continue
        if i2 - i1 == 1 and j2 - j1 == 1 and _qualified(wa[i1]) and _qualified(wb[j1]):
            red = _reduce_qualified(wa[i1], wb[j1], _package_context(old, os_, oe), _package_context(new, ns, ne))
            if red is None:
                acc.kinds.add("code_edit")
                return
            found += red
        else:
            found.append((old[os_:oe], new[ns:ne], None, None))
    acc.pairs += [p for p in found if p[0] != p[1]]
    acc.kinds.add("text_replace")


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


def _classify_block(minus: list[str], plus: list[str], acc: _Acc) -> None:
    m = [x for x in minus if x.strip()]
    p = [x for x in plus if x.strip()]
    if not m and not p:
        return                                               # blank lines only
    if len(m) != len(p):
        if _squash(m) != _squash(p):                         # not just a re-wrapped line
            acc.kinds.add("code_edit")
        return
    for a, b in zip(m, p):
        _classify_pair(a, b, acc)


def _file_kinds(fd: _FileDiff, red: Optional[Redactor] = None) -> _Acc:
    acc = _Acc(red=red)
    if fd.op == "create":
        acc.kinds.add("file_create")
        return acc
    if fd.op == "delete":
        acc.kinds.add("file_delete")
        return acc
    if fd.binary:
        acc.kinds.add("code_edit")
    for h in fd.hunks:
        for minus, plus in _blocks(h.lines):
            _classify_block(minus, plus, acc)
    if fd.op == "rename" and not acc.kinds:
        acc.kinds.add("file_rename")                         # a rename with edits counts as its edits
    return acc


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


def classify_diff(diff_text: str, redactor: Any = _UNSET) -> ChangeClass:
    """Text replace / code edit / file operations of a diff (see the module doc). Pure, never raises."""
    return classify_diff_ex(diff_text, redactor)[0]


def classify_diff_ex(diff_text: str, redactor: Any = _UNSET) -> tuple[ChangeClass, int]:
    """:func:`classify_diff` + the number of replace pairs dropped because they touched a secret value.
    ``redactor``: a :class:`sbmig.kb.redact.Redactor` (default: the built-in + default yml rules), None = keep
    every pair."""
    try:
        red = get_redactor(None) if redactor is _UNSET else redactor
        files = _parse(diff_text or "")
        if not files:
            return ChangeClass(), 0
        all_kinds: set[str] = set()
        raw_pairs: list[tuple[str, str, Optional[str], Optional[str], str]] = []
        paths: list[str] = []
        added = removed = dropped = 0
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
            acc = _file_kinds(fd, red)
            all_kinds |= acc.kinds
            dropped += acc.dropped
            raw_pairs += [(*p, path) for p in acc.pairs]
        # a package part learned with two different targets (commons.lang → lang3 and → text): the full names
        targets: dict[str, set[str]] = {}
        for old, new, fold, _fnew, _ in raw_pairs:
            if fold is not None:
                targets.setdefault(old, set()).add(new)
        agg: dict[tuple[str, str], ReplacePair] = {}
        for old, new, fold, fnew, path in raw_pairs:
            if fold is not None and len(targets.get(old, ())) > 1:
                old, new = fold, fnew or new
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
                           lines_added=added, lines_removed=removed), dropped
    except Exception as e:  # pragma: no cover - defensive: classification is best effort
        log.debug("classify_diff failed: %s: %s", type(e).__name__, e)
        return ChangeClass(), 0


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


def _window(h: _Hunk, line: Optional[int], room: int) -> str:
    """The hunk header + the content lines around ``line`` (old-side numbering) that fit in ``room`` chars."""
    head, body = h.raw[0] if h.raw else "", h.raw[1:] if h.raw else []
    if not body:
        return head
    at, no = 0, h.old_start or 1
    if line is not None:
        best = None
        for i, raw in enumerate(body):
            tag = raw[:1]
            if tag in (" ", "-", ""):
                if best is None or abs(no - line) < best[0]:
                    best = (abs(no - line), i)
                no += 1
        at = best[1] if best is not None else 0
    lo, hi = at, at + 1
    size = len(head) + len(body[at]) + 2
    while True:
        grew = False
        if hi < len(body) and size + len(body[hi]) + 1 <= room:
            size += len(body[hi]) + 1
            hi += 1
            grew = True
        if lo > 0 and size + len(body[lo - 1]) + 1 <= room:
            size += len(body[lo - 1]) + 1
            lo -= 1
            grew = True
        if not grew:
            break
    lines = [head] + body[lo:hi]
    text = "\n".join(lines)
    if room > 0 and len(text) > room:
        text = text[:room]
    return text + "\n" + TRUNCATED


def _section_excerpt(fd: _FileDiff, line: Optional[int], budget: int) -> str:
    head_lines = fd.head if fd.head is not None else fd.body
    head = "\n".join(head_lines)
    hunks = [h for h in fd.hunks if h.raw]
    if not hunks:
        if budget > 0 and len(head) > budget:
            return head[:budget] + "\n" + TRUNCATED
        return head
    if budget > 0 and len(head) > budget // 3:            # a small budget goes to the code, not to `index …` lines
        head = (head_lines[0] if head_lines else "")[:max(20, budget // 3)]
    order = list(range(len(hunks)))
    if line is not None and all(h.old_start is not None for h in hunks):
        def dist(i: int) -> tuple[int, int]:
            h = hunks[i]
            lo, hi = h.old_start or 0, (h.old_start or 0) + max(h.old_len, 1) - 1
            return (0 if lo <= line <= hi else min(abs(line - lo), abs(line - hi))), i
        order.sort(key=dist)
    chosen: list[tuple[int, str]] = []
    size = len(head) + 1
    for i in order:
        text = "\n".join(hunks[i].raw)
        if budget > 0 and size + len(text) + 1 > budget:
            if not chosen:                                   # the nearest hunk alone is too big: cut around the line
                chosen.append((i, _window(hunks[i], line, max(20, budget - size - len(TRUNCATED) - 2))))
            break
        chosen.append((i, text))
        size += len(text) + 1
    out = head + ("\n" if head else "") + "\n".join(t for _, t in sorted(chosen))
    if budget > 0 and len(out) > budget:
        out = out[:budget].rstrip("\n") + "\n" + TRUNCATED
    elif len(chosen) < len(hunks) and not out.endswith(TRUNCATED):
        out += "\n" + TRUNCATED                              # more hunks of this file are in the attempt's diff
    return out


def excerpt_for(diff_text: str, path: Optional[str], line: Optional[int] = None, limit: int = 0) -> str:
    """The section(s) of ``path`` (as :func:`diff_for_file`) cut down to the hunk(s) containing / nearest ``line``
    (old-side numbering: the line the build reported before the fix), then the next nearest while they fit in
    ``limit`` chars (0 = no limit); a single hunk larger than the limit is cut around the line. "" when the diff
    does not touch ``path``. Never raises."""
    try:
        if not path:
            return ""
        parts: list[str] = []
        used = 0
        for fd in _parse(diff_text or ""):
            if not (path_matches(fd.path, path) or path_matches(fd.old_path, path)):
                continue
            budget = max(0, limit - used) if limit > 0 else 0
            if limit > 0 and budget <= 0:
                break
            part = _section_excerpt(fd, line, budget)
            if part:
                parts.append(part)
                used += len(part) + 1
        return "\n".join(parts)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("excerpt_for failed: %s: %s", type(e).__name__, e)
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
