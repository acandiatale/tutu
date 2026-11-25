"""LLM의 수정 답변을 태그별로 나누어 읽는 모듈.
LLMFixer가 답변마다 `parse_script_response`를 호출하고, 구조 검토(`sbmig.review.parse`)도 이 모듈로 수정 블록을 읽는다.
`clean_code`, `script_problem`, `normalize_path`도 다른 모듈이 쓴다.
- 모든 태그는 없어도 되고, 어떤 답변에도 예외를 던지지 않는다
- 설명 글과 데이터 블록을 먼저 꺼내 지우므로 분석 글이나 JSP 편집 안의 `<script>`를 수정 스크립트로 잘못 읽지 않는다
- 여기서는 코드가 파싱되고 모듈 수준 `def fix`가 있는지만 확인한다.
  정적 검사는 `sbmig.script.check_script`, 편집 검증은 `sbmig.script.edits.edits_from_json`이 맡는다
- `<verify>`만 있는 답변은 답변으로 보지 않는다
- `<blocked>`는 한 문장으로 줄여 `blocked_reason`에 담는다.
  편집이 함께 와도 읽고, 거절로 처리할지는 수정 루프가 정한다

Fix answer protocol: docs/v2/PLAN-v2.1.md §3.6; script contract docs/INTERFACES.md "Script contract (S)".

The model answers with these parts (the change notes are what the business team reads)::

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

`<verify>operator checks</verify>` (after `<solution>`, only after a major version upgrade / a library
replaced) -> `ParsedScript.verify` -> `ScriptProposal.verify`; outer fence stripped, trimmed, masked like the
other prose blocks. A `<verify>` alone is not an answer.

`<blocked>one sentence naming the blocker and who decides</blocked>` (the case cannot be fixed inside the
project) -> `ParsedScript.blocked_reason` -> `ScriptProposal.blocked_reason`: outer fence stripped, whitespace
runs collapsed to one space, at most BLOCKED_MAX_CHARS characters ("…" when cut), "(no reason given)" for an
empty tag; masked like the other prose blocks. Edits next to it are still read (the loop decides what a
blocked answer means).

Change notes (`ParsedScript.changes` -> `FixAnswer.changes`): the `<changes>` list (`{"path", "where",
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
An opening tag may carry attributes (`<script type="python">`); of several script blocks the last one that is closed
and parses is taken (warning SEVERAL_SCRIPTS). `ParsedScript.unclosed` names a `<script>` / `<edits>` block that ran
to the end of the answer without its closing tag: with `finish_reason` length the fixer treats such an answer as cut
(not used, not stored, one feedback round asking for a shorter answer).
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
CHANGES_MISSING = "the answer did not describe its changes"   # <changes> 목록 없는 스크립트에 붙이는 [sbmig] 메모
BLOCKED_MAX_CHARS = 400                                        # <blocked> 문장의 최대 길이
MISSING_CLOSE_SCRIPT = "missing </script> (block closed at the end of the answer)"
SEVERAL_SCRIPTS = "several <script> blocks ({n}); took the last complete one"

_TAG_NAMES = ("analysis", "solution", "verify", "edits", "changes", "no_script", "blocked", "script", "need_files?",
              "need_grep", "confidence", "patches")
_OPEN_SCRIPT_RE = re.compile(r"<\s*script(?:\s+[^<>]*)?>", re.IGNORECASE)   # <script type="python">도 받는다
_CLOSE_SCRIPT_LINE_RE = re.compile(r"^[ \t]*<\s*/\s*script\s*>", re.IGNORECASE | re.MULTILINE)
_CLOSE_SCRIPT_RE = re.compile(r"<\s*/\s*script\s*>", re.IGNORECASE)
_NEXT_TAG_LINE_RE = re.compile(
    r"^[ \t]*<\s*(?:analysis|solution|verify|edits|changes|no_script|blocked|need_files?|need_grep)\s*>",
    re.IGNORECASE | re.MULTILINE)
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})[\w+.-]*\s*$")
_FENCED_BLOCK_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(?:python3?|py)?[ \t]*\n(.*?)^[ \t]*\1[ \t]*$",
                              re.IGNORECASE | re.MULTILINE | re.DOTALL)
_DEF_FIX_RE = re.compile(r"^def\s+fix\s*\(", re.MULTILINE)
# 연산이 하나 이상 있는 SEARCH/REPLACE 형식 답, 빈 <patches></patches>는 "수정 없음"이다
_PATCHES_RE = re.compile(r"^\s*(?:[#*>\-]\s*)*={3}\s*(?:MODIFY|CREATE|DELETE|RENAME)\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class ParsedScript:
    """LLM 답변 하나를 나눈 결과를 담는 데이터 클래스.
    실행할 수 있는 코드(`code`), 쓸 수 없지만 꺼내 둔 후보(`candidate`), 편집, 변경 설명, 추가 자료 요청, 경고를 담는다."""
    code: str = ""                                 # 파싱되고 모듈 수준 fix가 있는 코드, 아니면 빈 문자열
    candidate: str = ""                            # 쓸 수 없어도 꺼내 둔 스크립트 텍스트
    analysis: str = ""                             # 원인
    solution: str = ""                             # 해결 방법
    verify: str = ""                               # 메이저 업그레이드 뒤의 운영자 확인 사항, 없으면 빈 문자열
    edits: list[Edit] = field(default_factory=list)   # 검증한 편집, 없거나 읽지 못하면 빈 목록
    edits_block: bool = False                      # <edits> 블록이 있었는지, 비었거나 못 읽었어도 참
    edits_problem: str = ""                        # "edits JSON unreadable: …" / "edits invalid: …", 없으면 빈 값
    changes: list[EditNote] = field(default_factory=list)   # <changes> 목록과 편집마다의 desc
    changes_count: int = 0                         # `changes` 중 <changes> 목록에서 온 항목 수
    changes_block: bool = False                    # <changes> 블록이 있었는지
    changes_problem: str = ""                      # "changes unreadable: …"나 건너뛴 항목, 문제없으면 빈 문자열
    no_script_reason: str = ""                     # <no_script>
    blocked_reason: str = ""                       # <blocked>: 프로젝트 안에서 고칠 수 없는 이유
    need_files: list[str] = field(default_factory=list)
    need_grep: list[str] = field(default_factory=list)
    problem: str = ""                              # `code`가 빈 이유: NO_SCRIPT, NO_FIX_DEF, 파싱 오류
    # 닫는 태그 없이 답변 끝까지 이어진 블록 이름(script / edits), 토큰 한도에서 잘린 답인지 판단할 때 쓴다
    unclosed: list[str] = field(default_factory=list)
    # 토큰 한도에서 잘려 쓰지 않는 답의 이유, 파서가 아니라 LLMFixer가 finish_reason을 보고 채운다
    cut: str = ""
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def wants_context(self) -> bool:
        """LLM이 파일 내용이나 grep 결과를 더 요청했는지 반환한다."""
        return bool(self.need_files or self.need_grep)

    @property
    def usable(self) -> bool:
        """수정 루프가 적용할 내용이 있는지 반환한다. 실행할 수 있는 스크립트나 유효한 편집이 있으면 참이다."""
        return bool(self.code or self.edits)

    @property
    def used_patches(self) -> bool:
        """LLM이 이전 SEARCH/REPLACE `<patches>` 형식으로 답했는지 반환한다.
        지금 수정 루프는 이 형식을 받지 않는다."""
        return PATCHES_WARNING in self.warnings

    @property
    def described(self) -> bool:
        """`<changes>` 목록에 변경 설명이 하나라도 있는지 반환한다. 편집 항목의 desc는 세지 않는다.
        스크립트 답에는 이 설명이 있어야 한다."""
        return any(n.change or n.where for n in self.changes[:self.changes_count])

    def changes_note(self) -> str:
        """변경 설명이 없는 스크립트 답변에 붙일 `[sbmig]` 메모를 반환한다.
        설명이 있으면 빈 문자열을 반환한다. 메모가 붙으면 수정 루프가 diff로 설명을 만든다."""
        if self.described:
            return ""
        if self.changes_problem and self.changes_block:
            return f"{CHANGES_MISSING} ({self.changes_problem[:200]})"
        return CHANGES_MISSING


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def normalize_path(raw: str) -> tuple[Optional[str], Optional[str]]:
    """LLM이 준 경로를 posix 상대 경로로 정규화해 (경로, 경고)로 반환한다.
    절대 경로, 드라이브 문자, `~`, `..`는 거부하고 경로 자리에 None을 반환한다."""
    p = raw.strip().strip("`'\"*").strip()
    p = re.sub(r"\s+\(.*\)$", "", p)          # "path (new file)"에서 괄호 부분을 뗀다
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
    """앞뒤 빈 줄과 바깥을 감싼 코드 펜스 하나를 지운 텍스트를 반환한다."""
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
    """`<need_files>` 목록을 쉼표, 줄바꿈, 세미콜론으로 나누고 글머리표를 뗀 항목 목록으로 반환한다."""
    items = []
    for tok in re.split(r"[,\n;]+", text):
        tok = tok.strip().lstrip("-*").strip()
        if tok:
            items.append(tok)
    return items


def _find_simple(text: str, name: str) -> Optional[tuple[str, int]]:
    """<name>…</name>의 내용과 끝 위치를 반환한다. 태그가 없으면 None을 반환한다.
    닫는 태그가 없으면 다음 태그나 텍스트 끝까지를 내용으로 본다.

    With a closing tag the content may mention other tags ("I return a <script> …")."""
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
    """스크립트 텍스트에서 앞뒤 빈 줄과 코드 펜스만 지우고 반환한다.
    블록 전체가 들여쓰기되어 있으면 들여쓰기를 걷어 낸다. 끝에는 줄바꿈 하나를 붙이고, 나머지는 건드리지 않는다."""
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
    """코드가 파싱되고 모듈 수준 `def fix(…)`가 있는지 확인한다.
    문제가 없으면 빈 문자열을, 있으면 이유를 반환한다. 이유는 LLM 피드백과 `ScriptProposal.error`에 쓰인다."""
    if not code.strip():
        return NO_SCRIPT
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if exc.lineno else ""
        return f"script does not parse: SyntaxError: {exc.msg}{where}"
    except (ValueError, RecursionError) as exc:          # 널 바이트나 너무 깊은 중첩
        return f"script does not parse: {type(exc).__name__}: {exc}"
    if not any(isinstance(n, ast.FunctionDef) and n.name == "fix" for n in tree.body):
        return NO_FIX_DEF
    return ""


def _script_block_at(rest: str) -> tuple[str, bool]:
    """여는 태그 뒤의 텍스트에서 스크립트 내용과 닫는 태그가 있었는지를 반환한다.
    코드 안에도 `</script>`가 나올 수 있으므로 자기 줄에 있고 앞의 코드가 파싱되는 닫는 태그를 먼저 고른다.
    닫는 태그가 없으면 다음 <analysis> / <need_*> 줄이나 텍스트 끝에서 끝낸다."""
    closes = [c.start() for c in _CLOSE_SCRIPT_LINE_RE.finditer(rest)]
    closes += [c.start() for c in _CLOSE_SCRIPT_RE.finditer(rest) if c.start() not in closes]
    for pos in closes:
        if not script_problem(clean_code(rest[:pos])):
            return rest[:pos], True
    if closes:
        return rest[:closes[0]], True
    nxt = _NEXT_TAG_LINE_RE.search(rest)
    return (rest[:nxt.start()] if nxt else rest), False


def _script_from_tag(t: str, start: int, warnings: list[str]) -> Optional[str]:
    """`start` 위치부터 <script>…</script> 안의 텍스트를 꺼낸다. 여는 태그가 없으면 None을 반환한다.
    블록이 여러 개면 닫혀 있고 파싱되는 마지막 블록을 쓰고 SEVERAL_SCRIPTS 경고를 남긴다.
    완성된 블록이 없으면 첫 여는 태그의 블록을 쓴다.

    An opening tag inside a complete block's code (a script writing a JSP) never counts."""
    opens = list(_OPEN_SCRIPT_RE.finditer(t, start))
    if not opens:
        return None
    complete: list[tuple[int, int, str]] = []
    for m in opens:
        body, closed = _script_block_at(t[m.end():])
        if closed and not script_problem(clean_code(body)):
            complete.append((m.start(), m.end() + len(body), body))
    # 다른 완성 블록의 코드 안에 있는 여는 태그는 블록으로 세지 않는다
    outer = [c for c in complete if not any(o[0] < c[0] < o[1] for o in complete)]
    if outer:
        if len(outer) > 1:
            warnings.append(SEVERAL_SCRIPTS.format(n=len(outer)))
        # 마지막 완성 블록 뒤에 닫히지 않은 블록이 있으면 답이 잘렸을 수 있으므로 표시해 둔다
        if any(m.start() >= outer[-1][1] and not _script_block_at(t[m.end():])[1] for m in opens):
            warnings.append(MISSING_CLOSE_SCRIPT)
        return outer[-1][2]
    body, closed = _script_block_at(t[opens[0].end():])
    if not closed:
        warnings.append(MISSING_CLOSE_SCRIPT)
    return body


def _script_from_fence(t: str, warnings: list[str]) -> Optional[str]:
    """<script> 태그가 없을 때 `fix(…)`를 정의하는 마지막 python 코드 펜스 블록을 꺼낸다.
    답변 전체가 그런 코드이면 답변 전체를 반환한다. 둘 다 아니면 None을 반환한다."""
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
# 편집 JSON 읽기
# ---------------------------------------------------------------------------
_PY_LITERALS = {"None": "null", "True": "true", "False": "false"}


def _repair_json(s: str) -> str:
    """LLM이 자주 틀리는 JSON 표기를 고친다.
    문자열 밖의 끝 쉼표, `//` / `/* */` 주석, Python `None` / `True` / `False`를 고치고,
    작은따옴표 문자열은 JSON 문자열로, 문자열 안의 잘못된 이스케이프(`\\d`)는 `\\\\d`로 바꾼다."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":                                     # 문자열은 JSON 문자열로 바꿔 옮긴다
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
            i += 1                                        # 닫는 따옴표
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
    """편집 JSON을 읽어 (객체, 빈 문자열)을 반환한다.
    `json.loads`가 실패하면 너그럽게 다시 읽고, 그래도 실패하면 (None, 디코더 메시지)를 반환한다.

    Lenient order: raw control characters in strings, `_repair_json`, the first `[` / `{` value with
    prose around it."""
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
    # 앞뒤에 설명 글이 있으면 첫 괄호부터 읽는다. 뒤쪽 "{"부터 읽으면 목록에서 편집 하나만 가져오게 된다
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
    """`<edits>` 블록을 읽어 (검증된 편집 목록, 문제)로 반환한다. 항목 하나라도 잘못되면 편집 전체를 버린다.
    빈 블록이나 `[]`는 편집도 문제도 없는 것으로 본다."""
    obj, err = load_edits_json(text)
    if obj is None:
        return [], (f"{EDITS_UNREADABLE}: {err}" if err else "")
    from sbmig.script.edits import edits_from_json   # op / pom 호출 허용 목록은 적용하는 쪽이 관리한다
    edits, problems = edits_from_json(obj)
    if problems:
        return [], f"{EDITS_INVALID}: " + "; ".join(problems[:8]) + (f" (+{len(problems) - 8} more)"
                                                                    if len(problems) > 8 else "")
    return edits, ""


def parse_changes(text: str) -> tuple[list[EditNote], str]:
    """`<changes>` 블록을 읽어 (변경 설명 목록, 문제)로 반환한다. 읽지 못해도 답변을 거절하지 않는다.
    JSON은 편집처럼 너그럽게 읽고, JSON이 아니면 "path: what" 줄을 하나씩 읽는다."""
    from sbmig.script.notes import note_from_text, notes_from_json

    body = _strip_outer_fence(text.replace("\r\n", "\n")).strip()
    if not body:
        return [], ""
    obj, err = load_edits_json(body)
    if obj is None:
        if body[:1] not in "[{":                          # 글로 쓴 목록, 한 줄에 "path: what" 하나
            notes = [n for n in (note_from_text(ln) for ln in body.split("\n") if ln.strip()) if n is not None]
            if notes:
                return notes, ""
        return [], f"{CHANGES_UNREADABLE}: {err or 'not a JSON list'}"
    notes, problems = notes_from_json(obj)
    return notes, ("; ".join(problems[:5]) if problems else "")


def blocked_text(body: str) -> str:
    """`<blocked>` 블록의 내용을 한 문장으로 줄인다. 공백을 합치고 `BLOCKED_MAX_CHARS`자에서 자른다.
    태그가 비어 있으면 "(no reason given)"을 반환한다. 이유가 없어도 차단 답으로 처리한다."""
    text = " ".join(_strip_outer_fence(body.replace("\r\n", "\n")).split())
    if not text:
        return "(no reason given)"
    return text if len(text) <= BLOCKED_MAX_CHARS else text[:BLOCKED_MAX_CHARS - 1].rstrip() + "…"


def _unclosed(masked: str, name: str) -> bool:
    """`<name>` 블록이 있는데 닫는 태그가 없는지 확인한다."""
    m = re.search(rf"<\s*{name}\s*>", masked, re.IGNORECASE)
    return bool(m) and not re.search(rf"<\s*/\s*{name}\s*>", masked[m.end():], re.IGNORECASE)


def _mask(t: str, start: int, end: int) -> str:
    """`t`의 [start, end) 구간을 공백으로 지운다. 줄바꿈은 남기므로 위치 값은 그대로다."""
    return t[:start] + re.sub(r"[^\n]", " ", t[start:end]) + t[end:]


def _take_block(t: str, masked: str, name: str) -> tuple[Optional[str], str]:
    """`masked`에서 <name>…</name>을 찾아 그 내용과, 블록 자리를 공백으로 지운 masked를 반환한다.
    공백으로 지우므로 다른 위치 값은 바뀌지 않는다. 내용은 원문 `t`에서 가져온다."""
    m = re.search(rf"<\s*{name}\s*>", masked, re.IGNORECASE)
    if not m:
        return None, masked
    found = _find_simple(masked[m.start():], name)
    if found is None:                                     # pragma: no cover - 바로 위에서 태그를 찾았다
        return None, masked
    content_len = len(found[0])
    content_start = m.end()
    content = t[content_start:content_start + content_len]
    return content, _mask(masked, m.start(), m.start() + found[1])


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def parse_script_response(text: str) -> ParsedScript:
    """LLM 답변 하나를 ParsedScript로 나눈다. 예외를 던지지 않는다.
    설명 글과 데이터 블록 -> 추가 자료 요청 -> 스크립트 순서로 꺼낸다.
    태그가 하나도 없으면 답변 앞 500자를 analysis로 두고 PROTOCOL_WARNING을 남긴다."""
    raw = text or ""
    res = ParsedScript(raw=raw)
    t = raw.replace("\r\n", "\n").replace("\r", "\n")

    # 설명 글과 데이터 블록을 먼저 꺼내 지운다. 그 안의 <script>(분석 글, JSP 편집)를 수정 스크립트로 읽지 않으려는 것이다
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
    if _unclosed(masked, "edits"):
        res.unclosed.append("edits")
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
    body, masked = _take_block(t, masked, "blocked")
    if body is not None:
        res.blocked_reason = blocked_text(body)

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
    if MISSING_CLOSE_SCRIPT in res.warnings:
        res.unclosed.append("script")
    if body is not None:
        res.candidate = clean_code(body)
        res.problem = script_problem(res.candidate)
        if not res.code and not res.problem:
            res.code = res.candidate
    else:
        res.problem = NO_SCRIPT
        if _PATCHES_RE.search(masked):
            res.warnings.append(PATCHES_WARNING)

    # <verify>만 있는 답변은 답변으로 보지 않으므로 아래 조건에 verify를 넣지 않는다
    answered = (res.code or res.candidate or res.wants_context or res.analysis or res.solution or res.edits_block
                or res.no_script_reason or res.blocked_reason or res.changes_block)
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
    "BLOCKED_MAX_CHARS",
    "CHANGES_MISSING",
    "CHANGES_UNREADABLE",
    "EDITS_INVALID",
    "EDITS_UNREADABLE",
    "MISSING_CLOSE_SCRIPT",
    "NO_FIX_DEF",
    "NO_SCRIPT",
    "PATCHES_WARNING",
    "PROTOCOL_WARNING",
    "SEVERAL_SCRIPTS",
    "ParsedScript",
    "blocked_text",
    "clean_code",
    "load_edits_json",
    "normalize_path",
    "parse_changes",
    "parse_edits",
    "parse_script_response",
    "script_problem",
]
