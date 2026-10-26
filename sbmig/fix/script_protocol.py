"""v2 fix-script answer protocol (docs/v2/PLAN-v2.md §3.2, docs/INTERFACES.md "Script contract (S)").

The model answers with::

    <analysis>what broke, why, and how the script fixes it</analysis>
    <script>
    def fix(p):
        ...
    </script>

or ``<need_files>a, b</need_files>`` / ``<need_grep>regex</need_grep>`` (as v1; ``<need_file>`` is accepted too) to ask
for more context. The parser is tolerant — a ```python fence inside ``<script>``, CRLF, a block indented as a whole, a
missing ``</script>``, a ``</script>`` inside the code (a JSP edit), a bare ```python block that defines ``fix(p)``
when the tag is missing — and never raises. The code is kept as written otherwise (line ends normalized to ``\\n``,
surrounding blank lines dropped, one trailing newline). Only "does it parse and define a module-level ``def fix``" is
checked here; the static check proper (imports, names, dunder access) is `sbmig.script.check_script`.
"""
from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from typing import Optional

from .protocol import _split_list, _strip_outer_fence, normalize_path

NO_SCRIPT = "no <script> block"
NO_FIX_DEF = "script has no def fix(p)"
PROTOCOL_WARNING = "response did not follow the protocol"
PATCHES_WARNING = "answered with v1 <patches> instead of a <script>"

_TAG_NAMES = ("analysis", "script", "need_files?", "need_grep", "confidence", "patches")
_OPEN_SCRIPT_RE = re.compile(r"<\s*script\s*>", re.IGNORECASE)
_CLOSE_SCRIPT_LINE_RE = re.compile(r"^[ \t]*<\s*/\s*script\s*>", re.IGNORECASE | re.MULTILINE)
_CLOSE_SCRIPT_RE = re.compile(r"<\s*/\s*script\s*>", re.IGNORECASE)
_NEXT_TAG_LINE_RE = re.compile(r"^[ \t]*<\s*(?:analysis|need_files?|need_grep)\s*>", re.IGNORECASE | re.MULTILINE)
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})[\w+.-]*\s*$")
_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(?:python3?|py)?[ \t]*\n(.*?)^[ \t]*\1[ \t]*$",
                              re.IGNORECASE | re.MULTILINE | re.DOTALL)
_DEF_FIX_RE = re.compile(r"^def\s+fix\s*\(", re.MULTILINE)
# a v1 answer with at least one operation (an empty <patches></patches> is a deliberate "no fix")
_PATCHES_RE = re.compile(r"^\s*(?:[#*>\-]\s*)*={3}\s*(?:MODIFY|CREATE|DELETE|RENAME)\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class ParsedScript:
    code: str = ""                                 # usable code (parses, defines a module-level fix); "" otherwise
    candidate: str = ""                            # the extracted script text even when it is not usable
    analysis: str = ""
    need_files: list[str] = field(default_factory=list)
    need_grep: list[str] = field(default_factory=list)
    problem: str = ""                              # why `code` is empty: NO_SCRIPT, NO_FIX_DEF, "script does not parse…"
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def wants_context(self) -> bool:
        return bool(self.need_files or self.need_grep)

    @property
    def used_patches(self) -> bool:
        return PATCHES_WARNING in self.warnings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
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
# entry point
# ---------------------------------------------------------------------------
def parse_script_response(text: str) -> ParsedScript:
    raw = text or ""
    res = ParsedScript(raw=raw)
    t = raw.replace("\r\n", "\n").replace("\r", "\n")

    after_analysis = 0
    found = _find_simple(t, "analysis")
    if found is not None:
        res.analysis = _strip_outer_fence(found[0]).strip()
        after_analysis = found[1]

    nf = _find_simple(t, "need_files?")
    if nf is not None:
        for item in _split_list(_strip_outer_fence(nf[0])):
            p, warn = normalize_path(item)
            if warn or not p:
                res.warnings.append(f"need_files: {warn}")
            elif p not in res.need_files:
                res.need_files.append(p)
    ng = _find_simple(t, "need_grep")
    if ng is not None:
        for item in _strip_outer_fence(ng[0]).split("\n"):
            item = item.strip()
            if item and item not in res.need_grep:
                res.need_grep.append(item)

    body = _script_from_tag(t, after_analysis, res.warnings)
    if body is None and after_analysis:                   # <script> before <analysis>
        body = _script_from_tag(t, 0, res.warnings)
    if body is None:
        body = _script_from_fence(t, res.warnings)
    if body is not None:
        res.candidate = clean_code(body)
        res.problem = script_problem(res.candidate)
        if not res.problem:
            res.code = res.candidate
    else:
        res.problem = NO_SCRIPT
        if _PATCHES_RE.search(t):
            res.warnings.append(PATCHES_WARNING)

    if not res.code and not res.candidate and not res.wants_context and not res.analysis and raw.strip():
        res.warnings.append(PROTOCOL_WARNING)
        res.analysis = raw.strip()[:500]
    return res


__all__ = [
    "NO_FIX_DEF",
    "NO_SCRIPT",
    "PATCHES_WARNING",
    "PROTOCOL_WARNING",
    "ParsedScript",
    "clean_code",
    "parse_script_response",
    "script_problem",
]
