"""수정 답변 프로토콜 파서(v2.1): LLM 답 텍스트를 `<analysis>` 원인, `<solution>` 해결 방법, `<verify>` 운영자
확인 사항, `<edits>` 편집 JSON, `<changes>` 변경 설명, `<script>` 스크립트(또는 `<no_script>` 이유),
`<need_files>` / `<need_grep>` 문맥 요청으로 나눈다. 흐름상 위치: LLMFixer 가 답마다 `parse_script_response`
를 부르고, 구조 검토(`sbmig.review.parse`)도 수정 블록을 이것으로 읽는다. 핵심: parse_script_response →
ParsedScript, parse_edits / load_edits_json(관대한 JSON), parse_changes,
clean_code, script_problem, normalize_path.
지킬 것: 관대하고 절대 예외를 내지 않는다. 모든 부분이 선택이라 v2 답(`<analysis>` + `<script>`)도 그대로
읽힌다. 산문·데이터 블록을 먼저 꺼내 지우므로(masking) 분석이나 JSP 편집 속의 `<script>` 가 수정 스크립트로
오인되지 않는다. 여기서는 "파싱되고 모듈 수준 `def fix` 가 있는가" 만 보고, 진짜 정적 검사는
`sbmig.script.check_script`, 편집 항목의 op / pom 호출 / 경로 검증은 `sbmig.script.edits.edits_from_json`
몫이다. `<verify>` 만 있는 답은 답으로 치지 않는다.

Fix answer protocol (v2.1, docs/v2/PLAN-v2.1.md §3.6; v2 script contract docs/INTERFACES.md "Script contract (S)").

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
    """답 하나를 나눈 결과: 쓸 수 있는 코드(`code`)와 못 써도 꺼낸 후보(`candidate`), 원인·해결·운영자 확인,
    검증된 편집과 그 문제, 변경 설명, no_script 이유, 문맥 요청, 경고."""
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
        """LLM 이 파일·grep 을 더 달라고 했는가."""
        return bool(self.need_files or self.need_grep)

    @property
    def usable(self) -> bool:
        """루프가 적용할 것이 있는가: 쓸 수 있는 스크립트 또는 유효한 편집.

        Something the loop can apply: a usable script or valid edits."""
        return bool(self.code or self.edits)

    @property
    def used_patches(self) -> bool:
        """v1 SEARCH/REPLACE `<patches>` 형식으로 답했는가(v2 루프는 받지 않는다)."""
        return PATCHES_WARNING in self.warnings

    @property
    def described(self) -> bool:
        """`<changes>` 목록 자체가 무언가를 설명했는가(스크립트에는 필요; 편집의 desc 는 세지 않는다).

        The answer's `<changes>` list describes something (a script needs one)."""
        return any(n.change or n.where for n in self.changes[:self.changes_count])

    def changes_note(self) -> str:
        """설명이 있으면 ""; 없으면 스크립트 답에 붙일 `[sbmig]`
        메모 문구(그러면 루프가 diff 로 설명을 만든다).

        "" when the changes are described; else the `[sbmig]` note text for an answer with a script but no usable
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
    """(정규화한 posix 상대 경로, 경고). 절대 경로·드라이브
    문자·`~`·`..` 는 거부한다 — 프로젝트 밖은 손대지 않는다.

    Return (normalized posix relative path, warning). Rejects absolute paths and `..`."""
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
    """<name>…</name> 의 (내용, 끝 위치). 닫는 태그가 없으면 다음 알려진 태그나 끝에서 닫는다. 닫는 태그가
    있으면 내용이 다른 태그를 언급해도 된다.

    (content, end offset) of <name>…</name>; a missing closing tag ends at the next known tag or EOF. With a
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
    """스크립트 텍스트를 쓴 그대로: 앞뒤 빈 줄과 감싼 코드 펜스만 지우고, 통째로 들여쓴 블록이면 dedent, 끝
    줄바꿈 하나. 그 밖엔 손대지 않는다.

    The script text as written: blank lines around it and a code fence wrapped around it dropped, dedented when
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
    """코드가 파싱되고 모듈 수준 `def fix(…)` 를 정의하면 ""; 아니면 이유(LLM 에 피드백으로 보여 주고
    ScriptProposal.error 에 남는다).

    "" when `code` parses and defines a module-level `def fix(…)`; else the reason (shown to the LLM and kept in
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
    """`start` 부터 찾은 <script>…</script> 안의 텍스트. 닫는 태그는 "자기 줄에 있고 그 앞 블록이 파싱되는" 첫
    것(코드 안에 `</script>` 가 있을 수 있다 — JSP 편집), 다음은 아무 닫는 태그. 닫는 태그가 없으면 다음
    <analysis> / <need_*> 줄이나 끝까지(경고).

    The text inside <script>…</script> searched from `start`. The closing tag is the first one on its own line
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
    """<script> 태그가 없을 때: `fix(…)` 를 정의하는 마지막 python
    펜스 블록, 또는 답 전체가 그런 코드면 답 전체(경고와 함께).

    No <script> tag: the last fenced python block that defines a module-level fix(…), or the whole answer when
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
    """LLM 이 흔히 틀리는 JSON 을 고친다: 문자열 밖의 끝 쉼표·`//` `/* */` 주석·Python `None` / `True` /
    `False`, 작은따옴표 문자열 → JSON 문자열, 문자열 안의 잘못된 escape 는 역슬래시를 하나 더 붙여 살린다.

    A JSON text with the usual LLM slips repaired: outside strings trailing commas, `//` / `/* */` comments and
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
    """(파싱한 객체, "") 또는 (None, 디코더 메시지). `json.loads` 먼저, 다음은 관대하게: 문자열 안의 제어 문자
    허용, _repair_json, 앞뒤에 설명이 붙었으면 첫 `[` / `{` 부터 읽기.

    (parsed object, "" ) or (None, the decoder's message). `json.loads` first, then lenient: raw control
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
    # 첫 괄호부터만 읽는다 — 뒤의 "{" 부터 읽으면 목록에서 편집 하나만 집어 오게 된다
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
    """`<edits>` 블록 → (검증된 편집, 문제). 빈 블록 / `[]` 는 편집 없음·문제 없음. 항목 하나라도 잘못되면
    편집 전체를 버린다(일부만 적용하지 않는다).

    The `<edits>` block → (validated edits, problem). An empty block / `[]` = no edits, no problem."""
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
    """`<changes>` 블록 → (설명 목록, 문제). 편집과 같은 관대한 JSON, JSON 이 아니면 줄마다 "path: what" 으로
    읽는다. 읽히지 않아도 답을 거절하는 이유가 되지 않는다.

    The `<changes>` block → (notes, problem). JSON as lenient as the edits (`load_edits_json`); a text that is not
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
    """(`masked` 에서 찾은 <name>…</name> 의 내용 — 원문 `t` 의 같은 위치에서 잘라 옴, 그 블록 전체를 공백으로
    지운 masked). 공백으로 지우므로 오프셋이 그대로다.

    (content of <name>…</name> found in `masked`, masked text with the whole block blanked)."""
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
    """LLM 답 하나 → ParsedScript(절대 예외 없음). 순서가 중요하다: 산문·데이터 블록(analysis → solution →
    verify → edits → changes → no_script)을 먼저 꺼내 지우고, 다음 문맥 요청, 마지막에 스크립트(태그, 없으면
    펜스). 태그된 것이 하나도 없으면 PROTOCOL_WARNING 과 함께 답 앞 500자를 analysis 로."""
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

    # <verify> 만 있는 답은 답으로 치지 않는다(아래 목록에 verify 가 없는 이유)
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
