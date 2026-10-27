"""Fix answer protocol (v2.1, docs/v2/PLAN-v2.1.md §3.6; v2 script contract docs/INTERFACES.md "Script contract (S)").

The model answers with four parts (v2.1b: plus the change notes the business team reads)::

    <analysis>원인: what broke and why</analysis>
    <solution>해결 방법: how it is fixed (one paragraph a person can follow)</solution>
    <edits>[{"op": "replace", "path": "a/B.java", "desc": "a/B.java 의 import 줄에서 …", "old": "…", "new": "…"},
            {"op": "pom", "path": "pom.xml", "desc": "…", …}]</edits>
    <changes>[{"path": "a/B.java", "where": "import 줄", "change": "javax.annotation 을 jakarta.annotation 으로 바꿉니다"}]
    </changes>
    <script>
    def fix(p):
        ...
    </script>

`<no_script>reason</no_script>` instead of `<script>` when the fix cannot be a reusable script; `<need_files>a, b
</need_files>` / `<need_grep>regex</need_grep>` (`<need_file>` is accepted too) to ask for more context. Every part is
optional for the parser: a v2 answer with `<analysis>` + `<script>` only parses as before (solution "", edits [],
changes []).

v2.1d: `<verify>운영자 확인 사항</verify>` (after `<solution>`, only after a major version upgrade / a library replaced)
→ `ParsedScript.verify` → `ScriptProposal.verify`; outer fence stripped, trimmed, masked like the other prose blocks.
A `<verify>` alone is not an answer.

Change notes (v2.1b, `ParsedScript.changes` → `FixAnswer.changes`): the `<changes>` list (`{"path", "where",
"change"}`, required with a script, allowed with edits) first, then each edit's `desc` for a path the list did not
describe (`sbmig.script.notes.answer_notes`). Same tolerant JSON as the edits; a bullet list ("path: what") is read
too; what still does not read is `changes_problem` — never a reason to reject the answer.

The parser is tolerant and never raises. Script: a ```python fence inside `<script>`, CRLF, a block indented as a
whole, a missing `</script>`, a `</script>` inside the code (a JSP edit), a bare ```python block that defines `fix(p)`
when the tag is missing; the code is kept as written otherwise (line ends `\\n`, surrounding blank lines dropped, one
trailing newline) and only "does it parse and define a module-level `def fix`" is checked here — the static check
proper is `sbmig.script.check_script`. Edits: a ```json fence, trailing commas, comments, Python literals
(`None` / `True` / single quotes), raw newlines inside strings and prose around the list are repaired; what still does
not read is `edits_problem` "edits JSON unreadable: …"; items are validated into `Edit` models by
`sbmig.script.edits.edits_from_json` (unknown op / pom call, path outside the project → "edits invalid: …"). The
blocks are located before the script, so a `<script>` inside an edit (a JSP) or the analysis never opens the script.
"""
from __future__ import annotations

import ast
import json
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import Edit, EditNote

NO_SCRIPT = "no <script> block"
EDITS_UNREADABLE = "edits JSON unreadable"
EDITS_INVALID = "edits invalid"
NO_FIX_DEF = "script has no def fix(p)"
PROTOCOL_WARNING = "response did not follow the protocol"
PATCHES_WARNING = "answered with v1 <patches> instead of a <script>"
CHANGES_UNREADABLE = "changes unreadable"
CHANGES_MISSING = "the answer did not describe its changes"   # a script without a <changes> list ([sbmig] note)

_TAG_NAMES = ("analysis", "solution", "verify", "edits", "changes", "no_script", "script", "need_files?", "need_grep",
              "confidence", "patches")
_OPEN_SCRIPT_RE = re.compile(r"<\s*script\s*>", re.IGNORECASE)
_CLOSE_SCRIPT_LINE_RE = re.compile(r"^[ \t]*<\s*/\s*script\s*>", re.IGNORECASE | re.MULTILINE)
_CLOSE_SCRIPT_RE = re.compile(r"<\s*/\s*script\s*>", re.IGNORECASE)
_NEXT_TAG_LINE_RE = re.compile(
    r"^[ \t]*<\s*(?:analysis|solution|verify|edits|changes|no_script|need_files?|need_grep)\s*>",
    re.IGNORECASE | re.MULTILINE)
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})[\w+.-]*\s*$")
_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(?:python3?|py)?[ \t]*\n(.*?)^[ \t]*\1[ \t]*$",
                              re.IGNORECASE | re.MULTILINE | re.DOTALL)
_DEF_FIX_RE = re.compile(r"^def\s+fix\s*\(", re.MULTILINE)
# an answer in the old SEARCH/REPLACE format with at least one operation (an empty <patches></patches> = "no fix")
_PATCHES_RE = re.compile(r"^\s*(?:[#*>\-]\s*)*={3}\s*(?:MODIFY|CREATE|DELETE|RENAME)\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class ParsedScript:
    code: str = ""                                 # usable code (parses, defines a module-level fix); "" otherwise
    candidate: str = ""                            # the extracted script text even when it is not usable
    analysis: str = ""                             # 원인
    solution: str = ""                             # 해결 방법
    verify: str = ""                               # v2.1d <verify>: 운영자 확인 사항 after a major upgrade ("" = none)
    edits: list[Edit] = field(default_factory=list)   # validated; [] when absent or unreadable
    edits_block: bool = False                      # an <edits> block was there (maybe empty / unreadable)
    edits_problem: str = ""                        # "edits JSON unreadable: …" / "edits invalid: …" ("" = fine)
    changes: list[EditNote] = field(default_factory=list)   # <changes> list + each edit's desc (answer_notes)
    changes_count: int = 0                         # how many of `changes` came from the <changes> list itself
    changes_block: bool = False                    # a <changes> block was there
    changes_problem: str = ""                      # "changes unreadable: …" / skipped items ("" = fine)
    no_script_reason: str = ""                     # <no_script>
    need_files: list[str] = field(default_factory=list)
    need_grep: list[str] = field(default_factory=list)
    problem: str = ""                              # why `code` is empty: NO_SCRIPT, NO_FIX_DEF, "script does not parse…"
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def wants_context(self) -> bool:
        return bool(self.need_files or self.need_grep)

    @property
    def usable(self) -> bool:
        """Something the loop can apply: a usable script or valid edits."""
        return bool(self.code or self.edits)

    @property
    def used_patches(self) -> bool:
        return PATCHES_WARNING in self.warnings

    @property
    def described(self) -> bool:
        """The answer's `<changes>` list describes something (a script needs one)."""
        return any(n.change or n.where for n in self.changes[:self.changes_count])

    def changes_note(self) -> str:
        """"" when the changes are described; else the `[sbmig]` note text for an answer with a script but no usable
        `<changes>` list (the loop then builds the notes from the diff)."""
        if self.described:
            return ""
        if self.changes_problem and self.changes_block:
            return f"{CHANGES_MISSING} ({self.changes_problem[:200]})"
        return CHANGES_MISSING


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def normalize_path(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Return (normalized posix relative path, warning). Rejects absolute paths and `..`."""
    p = raw.strip().strip("`'\"*").strip()
    p = re.sub(r"\s+\(.*\)$", "", p)          # "path (new file)" → "path"
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p:
        return None, "empty path"
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p) or p.startswith("~"):
        return None, f"absolute path rejected: {raw.strip()}"
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in parts):
        return None, f"path with '..' rejected: {raw.strip()}"
    return "/".join(parts), None


def _strip_outer_fence(text: str) -> str:
    """The text without surrounding blank lines and without one code fence wrapped around it."""
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_LINE_RE.match(lines[0]):
        lines.pop(0)
        if lines and _FENCE_LINE_RE.match(lines[-1]):
            lines.pop()
    return "\n".join(lines)


def _split_list(text: str) -> list[str]:
    """Items of a `<need_files>` list (comma / newline / semicolon separated, bullets stripped)."""
    items = []
    for tok in re.split(r"[,\n;]+", text):
        tok = tok.strip().lstrip("-*").strip()
        if tok:
            items.append(tok)
    return items


def _find_simple(text: str, name: str) -> Optional[tuple[str, int]]:
    """(content, end offset) of <name>…</name>; a missing closing tag ends at the next known tag or EOF. With a
    closing tag the content may mention other tags ("I return a <script> …")."""
    m = re.search(rf"<\s*{name}\s*>", text, re.IGNORECASE)
    if not m:
        return None
    rest = text[m.end():]
    close = re.search(rf"<\s*/\s*{name}\s*>", rest, re.IGNORECASE)
    if close:
        return rest[:close.start()], m.end() + close.end()
    others = [t for t in _TAG_NAMES if t != name]
    nxt = re.search(r"<\s*(?:" + "|".join(others) + r")\s*>", rest, re.IGNORECASE)
    end = nxt.start() if nxt else len(rest)
    return rest[:end], m.end() + end


def clean_code(body: str) -> str:
    """The script text as written: blank lines around it and a code fence wrapped around it dropped, dedented when
    the block is indented as a whole, one trailing newline. Nothing else is touched."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_LINE_RE.match(lines[0]):
        lines.pop(0)
        if lines and _FENCE_LINE_RE.match(lines[-1]):
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
    if not lines:
        return ""
    text = "\n".join(lines)
    indents = [len(ln) - len(ln.lstrip(" \t")) for ln in lines if ln.strip()]
    if indents and min(indents) > 0:
        text = textwrap.dedent(text)
    return text.rstrip() + "\n"


def script_problem(code: str) -> str:
    """"" when `code` parses and defines a module-level `def fix(…)`; else the reason (shown to the LLM and kept in
    ScriptProposal.error)."""
    if not code.strip():
        return NO_SCRIPT
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if exc.lineno else ""
        return f"script does not parse: SyntaxError: {exc.msg}{where}"
    except (ValueError, RecursionError) as exc:          # null bytes, absurd nesting
        return f"script does not parse: {type(exc).__name__}: {exc}"
    if not any(isinstance(n, ast.FunctionDef) and n.name == "fix" for n in tree.body):
        return NO_FIX_DEF
    return ""


def _script_from_tag(t: str, start: int, warnings: list[str]) -> Optional[str]:
    """The text inside <script>…</script> searched from `start`. The closing tag is the first one on its own line
    whose block parses (a `</script>` can sit inside the code, e.g. a JSP edit), then any later one; without a
    closing tag the block ends at a following <analysis>/<need_*> line or EOF."""
    m = _OPEN_SCRIPT_RE.search(t, start)
    if not m:
        return None
    rest = t[m.end():]
    closes = [c.start() for c in _CLOSE_SCRIPT_LINE_RE.finditer(rest)]
    closes += [c.start() for c in _CLOSE_SCRIPT_RE.finditer(rest) if c.start() not in closes]
    for pos in closes:
        if not script_problem(clean_code(rest[:pos])):
            return rest[:pos]
    if closes:
        return rest[:closes[0]]
    warnings.append("missing </script> (block closed at the end of the answer)")
    nxt = _NEXT_TAG_LINE_RE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def _script_from_fence(t: str, warnings: list[str]) -> Optional[str]:
    """No <script> tag: the last fenced python block that defines a module-level fix(…), or the whole answer when
    it is nothing but such code."""
    blocks = [m.group(2) for m in _FENCED_BLOCK_RE.finditer(t)]
    for body in reversed(blocks):
        if _DEF_FIX_RE.search(textwrap.dedent(body)):
            warnings.append("no <script> tag; took the fenced python block that defines fix(p)")
            return body
    bare = _strip_outer_fence(t)
    if _DEF_FIX_RE.search(bare) and not script_problem(clean_code(bare)):
        warnings.append("no <script> tag; the whole answer is the script")
        return bare
    return None


# ---------------------------------------------------------------------------
# edits JSON
# ---------------------------------------------------------------------------
_PY_LITERALS = {"None": "null", "True": "true", "False": "false"}


def _repair_json(s: str) -> str:
    """A JSON text with the usual LLM slips repaired: outside strings trailing commas, `//` / `/* */` comments and
    Python `None` / `True` / `False`; single-quoted strings become JSON strings; inside strings invalid escapes
    (`\\d` → `\\\\d`)."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":                                     # a string: copy it as a JSON string
            quote = c
            buf: list[str] = []
            i += 1
            while i < n and s[i] != quote:
                ch = s[i]
                if ch == "\\":
                    nxt = s[i + 1] if i + 1 < n else ""
                    if quote == "'" and nxt == "'":
                        buf.append("'")
                    elif nxt and nxt in '"\\/bfnrtu':
                        buf.append(ch + nxt)
                    else:
                        buf.append("\\\\")
                        i += 1
                        continue
                    i += 2
                    continue
                buf.append('\\"' if ch == '"' and quote == "'" else ch)
                i += 1
            out.append('"' + "".join(buf) + '"')
            i += 1                                        # the closing quote
            continue
        if c == "/" and s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "]}":
                i += 1
                continue
        if c.isalpha() or c == "_":
            m = re.match(r"\w+", s[i:])
            word = m.group(0) if m else c
            out.append(_PY_LITERALS.get(word, word))
            i += len(word)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_edits_json(text: str) -> tuple[Optional[Any], str]:
    """(parsed object, "" ) or (None, the decoder's message). `json.loads` first, then lenient: raw control
    characters in strings, `_repair_json`, the first `[` / `{` value with prose around it, a Python literal."""
    s = _strip_outer_fence(text.replace("\r\n", "\n")).strip()
    if not s:
        return None, ""
    first_error = ""
    try:
        return json.loads(s), ""
    except ValueError as exc:
        first_error = str(exc)
    variants = [s]
    repaired = _repair_json(s)
    if repaired != s:
        variants.append(repaired)
    for v in variants:
        try:
            return json.loads(v, strict=False), ""
        except ValueError:
            pass
    # prose around the value: decode from the first bracket only (a later "{" would pick one edit out of a list)
    dec = json.JSONDecoder(strict=False)
    for v in variants:
        m = re.search(r"[\[{]", v)
        if m is None:
            continue
        try:
            obj, _ = dec.raw_decode(v, m.start())
        except ValueError:
            continue
        if isinstance(obj, (list, dict)):
            return obj, ""
    return None, first_error or "not a JSON list"


def parse_edits(text: str) -> tuple[list[Edit], str]:
    """The `<edits>` block → (validated edits, problem). An empty block / `[]` = no edits, no problem."""
    obj, err = load_edits_json(text)
    if obj is None:
        return [], (f"{EDITS_UNREADABLE}: {err}" if err else "")
    from sbmig.script.edits import edits_from_json   # the apply side owns the op / pom-call whitelist
    edits, problems = edits_from_json(obj)
    if problems:
        return [], f"{EDITS_INVALID}: " + "; ".join(problems[:8]) + (f" (+{len(problems) - 8} more)"
                                                                    if len(problems) > 8 else "")
    return edits, ""


def parse_changes(text: str) -> tuple[list[EditNote], str]:
    """The `<changes>` block → (notes, problem). JSON as lenient as the edits (`load_edits_json`); a text that is not
    JSON is read line by line ("path: what" bullets). An empty block = no notes, no problem."""
    from sbmig.script.notes import note_from_text, notes_from_json

    body = _strip_outer_fence(text.replace("\r\n", "\n")).strip()
    if not body:
        return [], ""
    obj, err = load_edits_json(body)
    if obj is None:
        if body[:1] not in "[{":                          # meant as text: one "path: what" per line
            notes = [n for n in (note_from_text(ln) for ln in body.split("\n") if ln.strip()) if n is not None]
            if notes:
                return notes, ""
        return [], f"{CHANGES_UNREADABLE}: {err or 'not a JSON list'}"
    notes, problems = notes_from_json(obj)
    return notes, ("; ".join(problems[:5]) if problems else "")


def _mask(t: str, start: int, end: int) -> str:
    """`t` with [start, end) blanked (newlines kept, offsets unchanged)."""
    return t[:start] + re.sub(r"[^\n]", " ", t[start:end]) + t[end:]


def _take_block(t: str, masked: str, name: str) -> tuple[Optional[str], str]:
    """(content of <name>…</name> found in `masked`, masked text with the whole block blanked)."""
    m = re.search(rf"<\s*{name}\s*>", masked, re.IGNORECASE)
    if not m:
        return None, masked
    found = _find_simple(masked[m.start():], name)
    if found is None:                                     # pragma: no cover - the tag was just found
        return None, masked
    content_len = len(found[0])
    content_start = m.end()
    content = t[content_start:content_start + content_len]
    return content, _mask(masked, m.start(), m.start() + found[1])


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse_script_response(text: str) -> ParsedScript:
    raw = text or ""
    res = ParsedScript(raw=raw)
    t = raw.replace("\r\n", "\n").replace("\r", "\n")

    # the prose / data blocks first, each blanked in `masked` so that a <script> they mention (an analysis about
    # "a <script>", a JSP edit with a <script> element) is never taken for the fix script
    masked = t
    body, masked = _take_block(t, masked, "analysis")
    if body is not None:
        res.analysis = _strip_outer_fence(body).strip()
    body, masked = _take_block(t, masked, "solution")
    if body is not None:
        res.solution = _strip_outer_fence(body).strip()
    body, masked = _take_block(t, masked, "verify")
    if body is not None:
        res.verify = _strip_outer_fence(body).strip()
    body, masked = _take_block(t, masked, "edits")
    if body is not None:
        res.edits_block = True
        res.edits, res.edits_problem = parse_edits(body)
    body, masked = _take_block(t, masked, "changes")
    if body is not None:
        res.changes_block = True
        res.changes, res.changes_problem = parse_changes(body)
    body, masked = _take_block(t, masked, "no_script")
    if body is not None:
        res.no_script_reason = _strip_outer_fence(body).strip() or "(no reason given)"

    nf = _find_simple(masked, "need_files?")
    if nf is not None:
        for item in _split_list(_strip_outer_fence(nf[0])):
            p, warn = normalize_path(item)
            if warn or not p:
                res.warnings.append(f"need_files: {warn}")
            elif p not in res.need_files:
                res.need_files.append(p)
    ng = _find_simple(masked, "need_grep")
    if ng is not None:
        for item in _strip_outer_fence(ng[0]).split("\n"):
            item = item.strip()
            if item and item not in res.need_grep:
                res.need_grep.append(item)

    body = _script_from_tag(masked, 0, res.warnings)
    if body is None:
        body = _script_from_fence(masked, res.warnings)
    if body is not None:
        res.candidate = clean_code(body)
        res.problem = script_problem(res.candidate)
        if not res.code and not res.problem:
            res.code = res.candidate
    else:
        res.problem = NO_SCRIPT
        if _PATCHES_RE.search(masked):
            res.warnings.append(PATCHES_WARNING)

    answered = (res.code or res.candidate or res.wants_context or res.analysis or res.solution or res.edits_block
                or res.no_script_reason or res.changes_block)
    if not answered and raw.strip():
        res.warnings.append(PROTOCOL_WARNING)
        res.analysis = raw.strip()[:500]
    if res.changes_problem:
        res.warnings.append(f"<changes>: {res.changes_problem[:200]}")
    res.changes_count = len(res.changes)
    from sbmig.script.notes import answer_notes
    res.changes = answer_notes(res.changes, res.edits)
    return res


__all__ = [
    "CHANGES_MISSING",
    "CHANGES_UNREADABLE",
    "EDITS_INVALID",
    "EDITS_UNREADABLE",
    "NO_FIX_DEF",
    "NO_SCRIPT",
    "PATCHES_WARNING",
    "PROTOCOL_WARNING",
    "ParsedScript",
    "clean_code",
    "load_edits_json",
    "normalize_path",
    "parse_changes",
    "parse_edits",
    "parse_script_response",
    "script_problem",
]
