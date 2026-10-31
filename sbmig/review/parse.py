"""구조 검토 LLM 답변 읽기(v2.1): `<review>{json}</review>` 의 판정(verdict)·이슈 목록, 그리고 수정 답변 —
`<analysis>` 원인, `<solution>` 해결 방법, `<edits>` 편집 목록(각 `desc` 포함), `<changes>` 변경 설명, 선택
`<script>` / `<no_script>`, v2.1d `<verify>` 운영자 확인 사항. 흐름상 위치: StructureReviewer 가 LLM 답을 받을
때마다(`_ask`, `_apply_retry`) 부른다. 핵심: parse_review_answer → ParsedReview, to_issue, extract_script,
changes_note. 지킬 것: 일부러 관대하게 읽는다(코드 펜스, 뒤따르는 설명, 끝 쉼표, 태그 없는 JSON, 객체 하나뿐인
issues, 모르는 severity → warning). 수정 블록은 `sbmig.fix.script_protocol` 이 수정 루프와 같은 규칙으로
읽는다. 읽을 리뷰 JSON 이 없으면 `ok=False` → 검토기가 한 번 더 묻는다.

Reading the structure-review answer (v2.1): `<review>{json}</review>` with the verdict and the issues, plus the
fix answer of docs/v2/PLAN-v2.1.md §3.6 — `<analysis>` 원인, `<solution>` 해결 방법, `<edits>` (JSON list of `Edit`, the
reviewer applies them; each with its `desc`), `<changes>` (what the fix modifies where, v2.1b) and an optional
`<script>` (or `<no_script>`). A v2 answer (`<review>` + `<script>`) parses as before (edits [], changes []).
v2.1d: an optional `<verify>` (운영자 확인 사항, only when the review's edits raise a major version anyway) → `verify`.

Tolerant on purpose: the JSON may sit in a code fence, be followed by prose, carry trailing commas, or come without
the `<review>` tags; `issues` may be a single object instead of a list; unknown severities become `warning`. The fix
blocks are read by `sbmig.fix.script_protocol` (same repair of the edits JSON, same script extraction). Nothing
readable → `ParsedReview(ok=False, error=…)` and the reviewer asks once more (then verdict `failed`); readable review
JSON with unreadable edits → `ok=True`, `edits_problem` set.


"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import Edit, EditNote, ReviewIssue

MAX_ISSUES = 60
PROBLEM_CHARS = 600
HINT_CHARS = 600
ANALYSIS_CHARS = 4000
FILE_CHARS = 300

_SCRIPT_RE = re.compile(r"<script>(.*?)(?:</script>|\Z)", re.S | re.I)
_REVIEW_RE = re.compile(
    r"<review>(.*?)(?:</review>|(?=<(?:analysis|solution|verify|edits|changes|no_script|script)>)|\Z)", re.S | re.I)
_FIX_BLOCK_RE = re.compile(r"<(analysis|solution|verify|edits|changes|no_script|script)>.*?(?:</\1>|\Z)", re.S | re.I)
_ANALYSIS_RE = re.compile(r"<analysis>(.*?)</analysis>", re.S | re.I)
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)(?:```|\Z)", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_KEYS = ("verdict", "issues")
_ISSUE_KEYS = ("severity", "problem", "fix_hint", "issue", "message")
_FILE_KEYS = ("file", "path", "pom", "module")
_PROBLEM_KEYS = ("problem", "issue", "message", "description", "title")
_HINT_KEYS = ("fix_hint", "fix", "hint", "suggestion", "recommendation")


@dataclass
class ParsedReview:
    """구조 검토 답변 하나를 읽은 결과: 판정·이슈·요약 + 수정 답변(원인·해결·편집·변경 설명·스크립트·운영자
    확인)과 읽다가 생긴 문제(`edits_problem` 등). `ok` 가 False 면 읽을 수 있는 리뷰 JSON 이 없었다."""
    ok: bool = False
    verdict: str = "ok"                         # ok | issues (only meaningful when ok)
    issues: list[ReviewIssue] = field(default_factory=list)
    analysis: str = ""                          # the review's summary (JSON "analysis", else the <analysis> block)
    script: str = ""
    fix_analysis: str = ""                      # <analysis> 원인 (the JSON analysis when the block is missing)
    solution: str = ""                          # <solution> 해결 방법
    verify: str = ""                            # v2.1d <verify> 운영자 확인 사항 (only after a major upgrade)
    edits: list[Edit] = field(default_factory=list)
    edits_problem: str = ""                     # "edits JSON unreadable: …" / "edits invalid: …"
    changes: list[EditNote] = field(default_factory=list)   # <changes> list + each edit's desc (v2.1b)
    changes_described: bool = False             # the <changes> list itself described something
    changes_problem: str = ""                   # "changes unreadable: …"
    no_script_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    raw: str = ""                               # the answer text

    @property
    def critical(self) -> list[ReviewIssue]:
        """severity 가 critical 인 이슈만(검토는 이것만 고친다)."""
        return [i for i in self.issues if i.severity == "critical"]


def _cap(text: Any, limit: int) -> str:
    s = str(text if text is not None else "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def strip_fences(code: str) -> str:
    """```python … ``` 펜스 안의 본문(펜스가 없으면 텍스트 그대로).

    The body of a ```python … ``` fence (or the text as it is)."""
    s = code.strip("\n")
    m = _FENCE_RE.search(s)
    if m and s.lstrip().startswith("```"):
        return m.group(2).strip("\n") + "\n"
    return s.strip("\n") + "\n" if s.strip() else ""


def extract_script(text: str) -> tuple[str, str]:
    """(스크립트, 스크립트 블록을 뺀 나머지 텍스트). `def fix` 가 정의된 스크립트만 남긴다.

    (script, text without the script block). A script is kept only when it defines `fix`."""
    m = _SCRIPT_RE.search(text or "")
    if not m:
        return "", text or ""
    code = strip_fences(m.group(1))
    rest = (text[: m.start()] + text[m.end():]) if text else ""
    return (code if re.search(r"^\s*def\s+fix\s*\(", code, re.M) else ""), rest


def _json_candidates(text: str) -> list[str]:
    out = [m.group(2) for m in _FENCE_RE.finditer(text)]
    out.append(text)
    return out


def _decode(s: str) -> Optional[Any]:
    """리뷰처럼 보이는 첫 JSON: 통째로 → 끝 쉼표 제거 → 글자 위치마다 raw_decode 해서 verdict / issues 키가
    있는 객체를 먼저, 없으면 이슈처럼 생긴 첫 객체나 객체 배열.

    The first JSON object (or array) in `s` that looks like a review; trailing text / commas tolerated."""
    s = s.strip()
    if not s:
        return None
    for attempt in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        try:
            return json.loads(attempt)
        except ValueError:
            pass
    dec = json.JSONDecoder()
    first: Optional[Any] = None
    for variant in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        for i, ch in enumerate(variant):
            if ch not in "{[":
                continue
            try:
                obj, _end = dec.raw_decode(variant, i)
            except ValueError:
                continue
            if isinstance(obj, dict) and any(k in obj for k in _KEYS):
                return obj
            if isinstance(obj, dict) and any(k in obj for k in _ISSUE_KEYS):
                first = first if first is not None else obj
            if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
                first = first if first is not None else obj
    return first


def _pick(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, (dict, list)) and str(v).strip():
            return str(v)
    return ""


def to_issue(item: Any, warnings: list[str]) -> Optional[ReviewIssue]:
    """이슈 한 항목(dict 또는 문자열)을 ReviewIssue 로. severity 는 critical 이 아니면 모두 warning(모르는
    값은 `warnings` 에 기록), 문제 문장이 없으면 버린다(None). 파일 경로의 역슬래시는 `/` 로."""
    if isinstance(item, str):
        return ReviewIssue(severity="warning", problem=_cap(item, PROBLEM_CHARS)) if item.strip() else None
    if not isinstance(item, dict):
        return None
    sev_raw = str(item.get("severity") or "").strip().lower()
    sev = "critical" if sev_raw == "critical" else "warning"
    if sev_raw and sev_raw not in ("critical", "warning"):
        warnings.append(f"unknown severity {sev_raw!r} → warning")
    problem = _pick(item, _PROBLEM_KEYS)
    if not problem:
        return None
    return ReviewIssue(severity=sev, file=_cap(_pick(item, _FILE_KEYS), FILE_CHARS).replace("\\", "/"),
                       problem=_cap(problem, PROBLEM_CHARS), fix_hint=_cap(_pick(item, _HINT_KEYS), HINT_CHARS))


def _fix_blocks(text: str, out: ParsedReview) -> None:
    """`<review>` 밖의 수정 답변을 `sbmig.fix.script_protocol.parse_script_response` 로 읽어 `out` 에 옮긴다.
    태그된 블록이 하나도 없으면 아무것도 가져오지 않는다(`<verify>` 만은 보관).

    The four-part fix answer outside the <review> block (sbmig.fix.script_protocol)."""
    from sbmig.fix.script_protocol import PROTOCOL_WARNING, parse_script_response

    fx = parse_script_response(text)
    out.verify = _cap(fx.verify, ANALYSIS_CHARS)        # a <verify> alone is no fix answer, but it is kept
    if PROTOCOL_WARNING in fx.warnings:                 # no tagged block at all: nothing to take
        return
    out.fix_analysis = _cap(fx.analysis, ANALYSIS_CHARS)
    out.solution = _cap(fx.solution, ANALYSIS_CHARS)
    out.edits = list(fx.edits)
    out.edits_problem = fx.edits_problem
    out.changes = list(fx.changes)
    out.changes_described = fx.described
    out.changes_problem = fx.changes_problem
    out.no_script_reason = _cap(fx.no_script_reason, ANALYSIS_CHARS)
    code = fx.code or (fx.candidate if re.search(r"^\s*def\s+fix\s*\(", fx.candidate or "", re.M) else "")
    out.script = code                                   # a broken script that defines fix: the runner refuses it
    out.warnings += [w for w in fx.warnings if w != PROTOCOL_WARNING]


def parse_review_answer(text: str) -> ParsedReview:
    """LLM 답변 하나 → ParsedReview. `<review>` 블록(없으면 수정 블록을 지운 나머지)에서 JSON 을 찾고, 수정
    블록은 따로 읽는다. 이슈가 MAX_ISSUES 를 넘으면 critical 을
    남기고 자른다. JSON 안에 든 script / edits / changes 도 받아 준다.

    `ParsedReview` of one LLM answer (see the module docstring)."""
    text = text or ""
    out = ParsedReview(raw=text)
    m = _REVIEW_RE.search(text)
    if m:
        body = m.group(1)
        rest = text[: m.start()] + "\n" + text[m.end():]
    else:                                               # no tags: the JSON is what the fix blocks leave
        body = _FIX_BLOCK_RE.sub("\n", text)
        rest = text
    _fix_blocks(rest, out)
    obj: Optional[Any] = None
    for cand in _json_candidates(body):
        obj = _decode(cand)
        if obj is not None:
            break
    if obj is None:
        out.error = "no review JSON in the answer" if text.strip() else "empty answer"
        return out
    if isinstance(obj, list):
        obj = {"issues": obj}
    elif not any(k in obj for k in _KEYS):
        obj = {"issues": [obj]}
    raw_issues = obj.get("issues") or []
    if isinstance(raw_issues, dict):
        raw_issues = [raw_issues]
    if not isinstance(raw_issues, list):
        out.warnings.append("issues is not a list")
        raw_issues = []
    for item in raw_issues:
        issue = to_issue(item, out.warnings)
        if issue is not None:
            out.issues.append(issue)
    if len(out.issues) > MAX_ISSUES:
        out.warnings.append(f"{len(out.issues) - MAX_ISSUES} issue(s) dropped (more than {MAX_ISSUES})")
        out.issues = sorted(out.issues, key=lambda i: i.severity != "critical")[:MAX_ISSUES]
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("ok", "issues", ""):
        out.warnings.append(f"unknown verdict {verdict!r}")
    # 판정은 이슈 목록이 정한다: LLM 이 ok 라고 해도 이슈가 하나라도 있으면 issues
    if verdict == "ok" and out.issues:
        out.warnings.append("verdict ok with issues listed → issues")
    out.verdict = "issues" if out.issues else "ok"
    analysis = obj.get("analysis")
    if not (isinstance(analysis, str) and analysis.strip()):
        analysis = out.fix_analysis
    out.analysis = _cap(analysis, ANALYSIS_CHARS)
    if not out.fix_analysis:
        out.fix_analysis = out.analysis
    if not out.script and isinstance(obj.get("script"), str):
        cand = strip_fences(obj["script"])
        out.script = cand if re.search(r"^\s*def\s+fix\s*\(", cand, re.M) else ""
    if not out.edits and not out.edits_problem and isinstance(obj.get("edits"), list):
        from sbmig.script.edits import edits_from_json   # edits inside the JSON object: accepted too
        edits, problems = edits_from_json(obj["edits"])
        out.edits = edits
        out.edits_problem = ("edits invalid: " + "; ".join(problems[:8])) if problems else ""
        if edits:
            from sbmig.script.notes import answer_notes
            out.changes = answer_notes(out.changes, edits)
    if not out.changes_described and isinstance(obj.get("changes"), list):
        from sbmig.script.notes import answer_notes, notes_from_json   # changes inside the JSON object: accepted too
        notes, _ = notes_from_json(obj["changes"])
        if notes:
            out.changes = answer_notes(notes, out.edits)
            out.changes_described = any(n.change or n.where for n in notes)
    out.ok = True
    return out


# ---------------------------------------------------------------------------- unsupported issues (v2.1y, E-M5)
UNSUPPORTED_MARK = "(sbmig check) not in the material: "
_ART = r"[A-Za-z][\w]*(?:[.\-][A-Za-z0-9_]+)+"
_VER = r"\d+\.\d+(?:\.\d+)*(?:[.\-][A-Za-z][\w]*)?"
# a version string attached to an artifact-like word (`g:a:v`, `jakarta.servlet-api 6.1.0`, `slf4j-api is pinned at
# 2.0.17`, `spring.version=5.3.31`) or to the word "version" — "Spring Boot 4.1" / "Java [1.8,9)" are not checked
_ATTACHED_VERSION = re.compile(
    rf"(?:{_ART}|\bversions?\b|버전|<version>)[`'\"]?"
    r"(?:\s*[:=]\s*|\s+(?:(?:is|are|was)\s+)?(?:(?:still\s+)?(?:pinned|kept|set|declared|left|raised|downgraded)\s+)?"
    r"(?:(?:at|to|on|of)\s+)?)"
    rf"[`'\"]?(?P<v>{_VER})\b", re.I)
_PROPERTY_NAMES = re.compile(r"\$\{([^}\s]+)\}|(?<![\w.$-])([A-Za-z][\w-]*(?:\.[\w-]+)*\.version)\b")
_BUILTIN_PREFIXES = ("project.", "java.", "env.", "settings.", "maven.", "os.", "user.", "basedir", "pom.")


def issue_tokens(issue: ReviewIssue) -> tuple[list[str], list[str]]:
    """이슈 문장(problem + fix_hint)이 말하는 (버전 문자열, 속성 이름) — 버전은 아티팩트 / version 낱말에 붙은 것만, 속성은
    `${…}` 와 `<이름>.version` (Maven 내장 `project.*` / `java.*` … 제외).

    (version strings, property names) an issue's text names: versions attached to an artifact-like word or the word
    "version"; properties as `${…}` or `<name>.version` (Maven built-ins skipped)."""
    text = f"{issue.problem} {issue.fix_hint}"
    versions = list(dict.fromkeys(m.group("v") for m in _ATTACHED_VERSION.finditer(text)))
    props: list[str] = []
    for m in _PROPERTY_NAMES.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        if name and not name.startswith(_BUILTIN_PREFIXES) and name not in props:
            props.append(name)
    return versions, props


def _version_in(version: str, material: str) -> bool:
    core = re.match(r"\d+(?:\.\d+)+", version)
    core_s = core.group(0) if core else version
    return re.search(rf"(?<![\d.]){re.escape(core_s)}(?!\d)", material) is not None


def _property_in(name: str, material: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", material) is not None


def unsupported_tokens(issue: ReviewIssue, material: str, state: Optional[str] = None) -> list[str]:
    """이슈가 말하는데 재료 어디에도 없는 버전·속성. 버전은 프롬프트 전체(`material`), 속성은 지금 상태를 보여 주는 부분
    (`state`, "What the restructure did" 를 뺀 것 — 재구성이 지운 속성이 '남아 있다' 는 말을 잡는다)에서 찾는다.

    The versions / properties an issue names that the material does not contain: versions are looked up in the whole
    prompt, property names in `state` (the prompt without "What the restructure did") — a property the restructure
    removed is in no pom, so "property X is left" is caught."""
    versions, props = issue_tokens(issue)
    state = material if state is None else state
    return [v for v in versions if not _version_in(v, material)] + [p for p in props if not _property_in(p, state)]


def mark_unsupported(issues: list[ReviewIssue], material: str, state: Optional[str] = None
                     ) -> tuple[list[ReviewIssue], list[str]]:
    """v2.1y (E-M5): 재료에 없는 버전·속성을 말한 이슈에 `unsupported=True` 와 문장 앞 표시 "(sbmig check) not in the
    material: " 를 붙인다(심각도는 그대로 — 보고서에는 남고, 적용·인수인계에서는 빠진다). (표시한 목록, 없던 토큰들).

    Mark the issues that name a version / property found nowhere in the material: `unsupported=True` and the problem
    prefixed with UNSUPPORTED_MARK (severity kept). Returns (issues, the missing tokens of all marked issues)."""
    out: list[ReviewIssue] = []
    missing: list[str] = []
    for i in issues:
        if i.unsupported or i.problem.startswith("(sbmig check)"):
            out.append(i)
            continue
        miss = unsupported_tokens(i, material, state)
        if not miss:
            out.append(i)
            continue
        missing += [m for m in miss if m not in missing]
        out.append(i.model_copy(update={"unsupported": True, "problem": UNSUPPORTED_MARK + i.problem}))
    return out, missing


def changes_note(parsed: ParsedReview) -> str:
    """스크립트는 있는데 `<changes>` 목록이 없는 리뷰 답변에
    붙일 `[sbmig]` 메모(설명이 있거나 스크립트가 없으면 "").

    The `[sbmig]` note for a review answer with a script but no `<changes>` list ("" = described / no script)."""
    from sbmig.fix.script_protocol import CHANGES_MISSING

    if not parsed.script or parsed.changes_described:
        return ""
    return f"{CHANGES_MISSING} ({parsed.changes_problem[:200]})" if parsed.changes_problem else CHANGES_MISSING
