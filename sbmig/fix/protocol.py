"""LLM fix response protocol: rendering of the instructions and a tolerant parser.

The model answers with tagged sections::

    <analysis>why</analysis>
    <confidence>0.8</confidence>
    <patches>
    === MODIFY path/File.java
    <<<<<<< SEARCH
    old
    =======
    new
    >>>>>>> REPLACE
    === CREATE path/New.java
    content
    === DELETE path/Old.java
    === RENAME a.java -> b.java
    </patches>

or ``<need_files>a, b</need_files>`` / ``<need_grep>regex</need_grep>`` to ask for more context.
The parser accepts sloppy variants (code fences, CRLF, lowercase keywords, missing closing tags,
backticks around paths) and never raises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sbmig.core.models import FileChange

PROTOCOL_INSTRUCTIONS = """\
## Output protocol (mandatory)

Answer with ONLY the following tagged sections, nothing before or after them:

<analysis>why the build fails and what you change, 1-5 lines</analysis>
<confidence>a number between 0.0 and 1.0</confidence>
<patches>
=== MODIFY relative/path/File.java
<<<<<<< SEARCH
exact existing text (at least 1 full line, unique in the file)
=======
replacement text
>>>>>>> REPLACE
(more SEARCH/REPLACE blocks for the same file may follow)
=== CREATE relative/path/New.java
full file content
=== DELETE relative/path/Old.java
=== RENAME old/path/Old.java -> new/path/New.java
</patches>

Rules for <patches>:
- Paths are relative to the project root, exactly as shown in the context (posix `/`, no leading `/`, no `..`).
- SEARCH text must be copied verbatim from the file content shown to you (same indentation, same
  whitespace), consist of whole lines, and match exactly ONE place in the file. Include a few
  surrounding lines if a line is not unique. Never include the `// <path> lines a-b` headers or
  line numbers in SEARCH.
- Keep SEARCH blocks small: only the lines you change plus minimal context. Use several blocks
  rather than one huge block. Blocks of one file are applied top to bottom.
- To delete lines, leave the replacement empty. To add an import, SEARCH an existing import or
  the package line and REPLACE it with itself plus the new line.
- CREATE is only for files that do not exist yet; give the complete content. Never re-create an
  existing file to change it — use MODIFY.
- Never output a truncated file or `...` placeholders.

If you cannot fix the errors without seeing more code, answer instead of <patches> with:
<analysis>what you need and why</analysis>
<need_files>relative/path/A.java, relative/path/pom.xml</need_files>
and/or
<need_grep>a Python regular expression to search in *.java, pom.xml and resources</need_grep>
"""

_KNOWN_TAGS = ("analysis", "confidence", "patches", "need_files", "need_grep")

_HEADER_RE = re.compile(
    r"^\s*(?:[#*>\-]\s*)*={3}\s*(MODIFY|CREATE|DELETE|RENAME|EDIT|UPDATE|ADD|REMOVE|MOVE)\b[:\s]*(.*?)\s*$",
    re.IGNORECASE,
)
_OP_ALIASES = {"edit": "modify", "update": "modify", "add": "create", "remove": "delete", "move": "rename"}
_SEARCH_RE = re.compile(r"^\s*<{5,}\s*SEARCH\s*$", re.IGNORECASE)
_DIVIDER_RE = re.compile(r"^\s*={5,}\s*$")
_REPLACE_RE = re.compile(r"^\s*>{5,}\s*REPLACE\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})[\w+.-]*\s*$")
_RENAME_SPLIT_RE = re.compile(r"\s*(?:->|→|=>|\s+to\s+)\s*")


@dataclass
class ParsedResponse:
    changes: list[FileChange] = field(default_factory=list)
    analysis: str = ""
    need_files: list[str] = field(default_factory=list)
    need_grep: list[str] = field(default_factory=list)
    confidence: Optional[float] = None
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def wants_context(self) -> bool:
        return bool(self.need_files or self.need_grep)


def render_protocol_instructions() -> str:
    return PROTOCOL_INSTRUCTIONS


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


def _find_tag(text: str, name: str) -> Optional[str]:
    """Content of <name>…</name>; tolerates a missing closing tag (ends at next known tag or EOF)."""
    m = re.search(rf"<\s*{name}\s*>", text, re.IGNORECASE)
    if not m:
        return None
    rest = text[m.end():]
    close = re.search(rf"<\s*/\s*{name}\s*>", rest, re.IGNORECASE)
    others = [t for t in _KNOWN_TAGS if t != name]
    nxt = re.search(r"<\s*(?:" + "|".join(others) + r")\s*>", rest, re.IGNORECASE)
    if name == "patches":
        # patch bodies may legitimately contain anything; only the closing tag or EOF ends them,
        # unless a need_* tag follows on its own line
        nxt = re.search(r"^\s*<\s*(?:need_files|need_grep)\s*>", rest, re.IGNORECASE | re.MULTILINE)
    end = len(rest)
    if close:
        end = close.start()
    if nxt and nxt.start() < end:
        end = nxt.start()
    return rest[:end]


def _strip_outer_fence(text: str) -> str:
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _FENCE_RE.match(lines[0]):
        lines.pop(0)
        if lines and _FENCE_RE.match(lines[-1]):
            lines.pop()
    return "\n".join(lines)


def _strip_body_fence(lines: list[str]) -> list[str]:
    """Strip a fence wrapped around a CREATE body (and surrounding blank lines)."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    if out and _FENCE_RE.match(out[0]):
        out.pop(0)
        if out and _FENCE_RE.match(out[-1]):
            out.pop()
    return out


def _split_list(text: str) -> list[str]:
    items = []
    for tok in re.split(r"[,\n;]+", text):
        tok = tok.strip().lstrip("-*").strip()
        if tok:
            items.append(tok)
    return items


# ---------------------------------------------------------------------------
# patches
# ---------------------------------------------------------------------------
def _parse_patches(body: str, warnings: list[str]) -> list[FileChange]:
    lines = body.split("\n")
    changes: list[FileChange] = []
    i = 0
    n = len(lines)

    # skip preamble until first header
    while i < n and not _HEADER_RE.match(lines[i]):
        if lines[i].strip() and not _FENCE_RE.match(lines[i]):
            warnings.append(f"ignored text before first operation: {lines[i].strip()[:60]}")
        i += 1

    while i < n:
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        op = m.group(1).lower()
        op = _OP_ALIASES.get(op, op)
        arg = m.group(2)
        i += 1
        # collect body lines until next header (header detection is suspended inside SEARCH/REPLACE)
        body_lines: list[str] = []
        state = "out"
        while i < n:
            line = lines[i]
            if state == "out" and _HEADER_RE.match(line):
                break
            if op == "modify":
                if _SEARCH_RE.match(line):
                    state = "search"
                elif state == "search" and _DIVIDER_RE.match(line):
                    state = "replace"
                elif _REPLACE_RE.match(line):
                    state = "out"
                elif state == "replace" and _HEADER_RE.match(line):
                    break                        # missing >>>>>>> REPLACE; handled by block parser
            body_lines.append(line)
            i += 1

        if op == "rename":
            parts = _RENAME_SPLIT_RE.split(arg.strip().strip("`"), maxsplit=1)
            if len(parts) != 2:
                warnings.append(f"bad RENAME header: {arg}")
                continue
            src, w1 = normalize_path(parts[0])
            dst, w2 = normalize_path(parts[1])
            if w1 or w2 or not src or not dst:
                warnings.append(w1 or w2 or f"bad RENAME header: {arg}")
                continue
            changes.append(FileChange(path=src, op="rename", new_path=dst))
            continue

        path, warn = normalize_path(arg)
        if warn or not path:
            warnings.append(f"{op.upper()} skipped: {warn}")
            continue

        if op == "delete":
            changes.append(FileChange(path=path, op="delete"))
        elif op == "create":
            content_lines = _strip_body_fence(body_lines)
            content = "\n".join(content_lines)
            if content and not content.endswith("\n"):
                content += "\n"
            if not content.strip():
                warnings.append(f"CREATE {path} has empty content")
            changes.append(FileChange(path=path, op="create", content=content))
        else:
            blocks = _parse_blocks(body_lines, path, warnings)
            if not blocks:
                warnings.append(f"MODIFY {path} has no SEARCH/REPLACE block")
            for search, replace in blocks:
                changes.append(FileChange(path=path, op="modify", search=search, replace=replace))
    return changes


def _parse_blocks(lines: list[str], path: str, warnings: list[str]) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    state = "out"
    search: list[str] = []
    replace: list[str] = []
    for line in lines:
        if state == "out":
            if _SEARCH_RE.match(line):
                state, search, replace = "search", [], []
            continue
        if state == "search":
            if _DIVIDER_RE.match(line):
                state = "replace"
            elif _SEARCH_RE.match(line):
                warnings.append(f"{path}: SEARCH block without divider dropped")
                search = []
            else:
                search.append(line)
            continue
        # replace
        if _REPLACE_RE.match(line):
            blocks.append(("\n".join(search), "\n".join(replace)))
            state = "out"
        elif _SEARCH_RE.match(line):
            warnings.append(f"{path}: missing '>>>>>>> REPLACE' marker (block closed implicitly)")
            blocks.append(("\n".join(search), "\n".join(replace)))
            state, search, replace = "search", [], []
        else:
            replace.append(line)
    if state == "replace":
        while replace and (not replace[-1].strip() or _FENCE_RE.match(replace[-1])):
            replace.pop()
        warnings.append(f"{path}: missing '>>>>>>> REPLACE' marker at end (block closed implicitly)")
        blocks.append(("\n".join(search), "\n".join(replace)))
    elif state == "search":
        warnings.append(f"{path}: unterminated SEARCH block dropped")
    return blocks


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse_fix_response(text: str) -> ParsedResponse:
    raw = text or ""
    res = ParsedResponse(raw=raw)
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    t = _strip_outer_fence(t)

    analysis = _find_tag(t, "analysis")
    if analysis is not None:
        res.analysis = _strip_outer_fence(analysis).strip()

    conf = _find_tag(t, "confidence")
    if conf is not None:
        m = re.search(r"[-+]?\d*\.?\d+", conf)
        if m:
            try:
                v = float(m.group(0))
                if v > 1.0 and v <= 100.0:
                    v = v / 100.0
                res.confidence = max(0.0, min(1.0, v))
            except ValueError:
                res.warnings.append(f"bad confidence: {conf.strip()[:20]}")

    nf = _find_tag(t, "need_files")
    if nf is not None:
        for item in _split_list(_strip_outer_fence(nf)):
            p, warn = normalize_path(item)
            if warn or not p:
                res.warnings.append(f"need_files: {warn}")
            elif p not in res.need_files:
                res.need_files.append(p)

    ng = _find_tag(t, "need_grep")
    if ng is not None:
        for item in _strip_outer_fence(ng).split("\n"):
            item = item.strip()
            if item and item not in res.need_grep:
                res.need_grep.append(item)

    patches = _find_tag(t, "patches")
    if patches is None:
        if any(_HEADER_RE.match(l) for l in t.split("\n")):
            res.warnings.append("no <patches> tag; parsed operations from the whole response")
            patches = t
    if patches is not None:
        res.changes = _parse_patches(_strip_outer_fence(patches), res.warnings)

    if not res.changes and not res.wants_context and not res.analysis and raw.strip():
        res.warnings.append("response did not follow the protocol")
        res.analysis = raw.strip()[:500]
    return res
