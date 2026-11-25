"""구조 검토에서 LLM 답변을 읽는 모듈.
`<review>{json}</review>`의 결과와 이슈 목록, 그리고 수정 블록을 읽는다.
StructureReviewer가 LLM 답변을 받을 때마다(`_ask`, `_apply_retry`) 호출하며, 진입점은 `parse_review_answer`다.
- 형식이 조금 틀려도 읽는다.
  코드 펜스, 뒤에 붙은 설명, 끝 쉼표, 태그 없는 JSON을 받아 주고, 모르는 severity는 warning으로 본다
- 수정 블록은 `sbmig.fix.script_protocol`이 수정 루프와 같은 규칙으로 읽는다
- 리뷰 JSON이 없으면 `ok=False`가 되고 검토기가 한 번 더 요청한다

Spec: docs/v2/PLAN-v2.1.md §3.6.
* `<review>{json}</review>`: verdict and issues; `issues` may be a single object; unknown severities
  become `warning`.
* Fix blocks: `<analysis>`, `<solution>`, `<edits>` (JSON list of `Edit`, each with its `desc`),
  `<changes>`, an optional `<script>` / `<no_script>`, and an optional `<verify>` (operator checks,
  only when the edits raise a major version anyway). `<review>` + `<script>` alone parses with
  edits [] and changes [].
* The JSON may sit in a code fence, be followed by prose, carry trailing commas, or come without
  the `<review>` tags.
* Fix blocks are read by `sbmig.fix.script_protocol` (same repair of the edits JSON, same script
  extraction).
* Nothing readable = `ParsedReview(ok=False, error=…)`, the reviewer asks once more (then verdict
  `failed`); readable review JSON with unreadable edits = `ok=True` with `edits_problem` set.
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
    r"<review>(.*?)(?:</review>|(?=<(?:analysis|solution|verify|edits|changes|no_script|script(?:\s[^<>]*)?)>)|\Z)",
    re.S | re.I)
_FIX_BLOCK_RE = re.compile(r"<(analysis|solution|verify|edits|changes|no_script|script)(?:\s[^<>]*)?>.*?(?:</\1>|\Z)",
                           re.S | re.I)
_ANALYSIS_RE = re.compile(r"<analysis>(.*?)</analysis>", re.S | re.I)
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)(?:```|\Z)", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
# 글 속 괄호가 아니라 JSON을 시작하는 여는 괄호
_JSON_START_RE = re.compile(r"[{\[]\s*(?:\"|\{|\[|\]|\}|$)")
CUT_REASONS = ("length", "max_tokens")
CUT_ERROR = "answer cut at the output token limit (finish_reason={reason})"
FRAGMENT_ERROR = "only a fragment of the review JSON could be read"
_KEYS = ("verdict", "issues")
_ISSUE_KEYS = ("severity", "problem", "fix_hint", "issue", "message")
_FILE_KEYS = ("file", "path", "pom", "module")
_PROBLEM_KEYS = ("problem", "issue", "message", "description", "title")
_HINT_KEYS = ("fix_hint", "fix", "hint", "suggestion", "recommendation")


@dataclass
class ParsedReview:
    """구조 검토 답변 하나를 읽은 결과를 담는 데이터 클래스.
    결과, 이슈, 요약, 수정 블록과 읽는 중에 생긴 문제를 담는다.
    `ok`가 False이면 읽을 수 있는 리뷰 JSON이 없었다는 뜻이다."""
    ok: bool = False
    verdict: str = "ok"                         # ok | issues, ok가 참일 때만 의미가 있다
    issues: list[ReviewIssue] = field(default_factory=list)
    analysis: str = ""                          # 검토 요약. JSON의 analysis, 없으면 <analysis> 블록
    script: str = ""
    fix_analysis: str = ""                      # <analysis> 원인, 블록이 없으면 JSON의 analysis
    solution: str = ""                          # 해결 방법
    verify: str = ""                            # <verify> 운영자 확인 사항, 메이저 버전을 올릴 때만 온다
    edits: list[Edit] = field(default_factory=list)
    edits_problem: str = ""                     # "edits JSON unreadable: …" / "edits invalid: …"
    changes: list[EditNote] = field(default_factory=list)   # <changes> 목록과 편집마다의 desc
    changes_described: bool = False             # <changes> 목록 자체에 설명이 있었다
    changes_problem: str = ""                   # "changes unreadable: …"
    no_script_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    raw: str = ""                               # 답변 원문

    @property
    def critical(self) -> list[ReviewIssue]:
        """severity가 critical인 이슈 목록을 반환한다. 검토는 이 이슈만 고친다."""
        return [i for i in self.issues if i.severity == "critical"]


def _cap(text: Any, limit: int) -> str:
    s = str(text if text is not None else "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def strip_fences(code: str) -> str:
    """```python … ``` 펜스 안의 본문을 꺼낸다. 펜스가 없으면 텍스트를 그대로 반환한다."""
    s = code.strip("\n")
    m = _FENCE_RE.search(s)
    if m and s.lstrip().startswith("```"):
        return m.group(2).strip("\n") + "\n"
    return s.strip("\n") + "\n" if s.strip() else ""


def extract_script(text: str) -> tuple[str, str]:
    """텍스트에서 스크립트를 꺼내 (스크립트, 스크립트 블록을 뺀 텍스트)를 반환한다.
    `def fix`가 없는 스크립트는 빈 문자열로 둔다."""
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


def _depth(s: str) -> int:
    """`s` 안에서 닫히지 않은 `{` / `[`의 수를 센다. 문자열 안의 괄호는 세지 않는다."""
    depth, in_str, esc = 0, False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
    return depth


def _decode(s: str) -> tuple[Optional[Any], bool]:
    """텍스트에서 리뷰로 보이는 첫 JSON을 찾아 (객체, 조각 여부)를 반환한다.
    verdict / issues 키가 있는 객체를 먼저 찾고, 없으면 이슈처럼 생긴 첫 객체나 배열을 고른다.
    앞에서 읽지 못한 JSON 안에 들어 있는 객체는 조각이므로 두 번째 값이 True가 된다.
    조각이면 호출한 쪽은 그것을 검토 결과로 쓰면 안 된다.

    Trailing text / commas and raw control characters inside strings are tolerated. A fragment comes from an
    answer cut at the token limit or a broken outer object."""
    s = s.strip()
    if not s:
        return None, False
    for attempt in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        for strict in (True, False):
            try:
                return json.loads(attempt, strict=strict), False
            except ValueError:
                pass
    dec = json.JSONDecoder(strict=False)
    first: Optional[Any] = None
    first_fragment = False
    for variant in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        failed: Optional[int] = None                    # JSON처럼 보였지만 읽지 못한 첫 여는 괄호 위치
        for i, ch in enumerate(variant):
            if ch not in "{[":
                continue
            try:
                obj, _end = dec.raw_decode(variant, i)
            except ValueError:
                # `${project.version}` 같은 글 속 괄호는 JSON 시작으로 보지 않는다
                if failed is None and _JSON_START_RE.match(variant, i):
                    failed = i
                continue
            fragment = failed is not None and _depth(variant[failed:i]) > 0
            if isinstance(obj, dict) and any(k in obj for k in _KEYS):
                return obj, fragment
            if isinstance(obj, dict) and any(k in obj for k in _ISSUE_KEYS):
                if first is None:
                    first, first_fragment = obj, fragment
            if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
                if first is None:
                    first, first_fragment = obj, fragment
    return first, first_fragment


def _pick(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, (dict, list)) and str(v).strip():
            return str(v)
    return ""


def to_issue(item: Any, warnings: list[str]) -> Optional[ReviewIssue]:
    """이슈 항목 하나를 ReviewIssue로 바꾼다.
    critical이 아닌 severity는 모두 warning으로 보고, 문제 설명이 없으면 None을 반환한다."""
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
    """`<review>` 밖의 수정 답변을 `parse_script_response`로 읽어 `out`에 옮긴다.
    태그된 블록이 없으면 `<verify>`만 보관한다."""
    from sbmig.fix.script_protocol import PROTOCOL_WARNING, parse_script_response

    fx = parse_script_response(text)
    out.verify = _cap(fx.verify, ANALYSIS_CHARS)        # <verify>만으로는 수정 답변이 아니지만 보관한다
    if PROTOCOL_WARNING in fx.warnings:                 # 태그된 블록이 하나도 없으면 가져올 것이 없다
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
    out.script = code                                   # fix가 있는 깨진 스크립트는 러너가 거부한다
    out.warnings += [w for w in fx.warnings if w != PROTOCOL_WARNING]


def parse_review_answer(text: str, finish_reason: Optional[str] = None) -> ParsedReview:
    """LLM 답변 하나를 ParsedReview로 만든다. `<review>` 블록에서 JSON을 찾고, 수정 블록은 따로 읽는다.
    이슈가 MAX_ISSUES개를 넘으면 critical 이슈를 남기고 나머지를 자른다.
    토큰 한도에서 잘린 답변이나 JSON 조각만 읽힌 답변은 `ok=False`로 두고 `error`에 이유를 쓴다.
    검토기는 일부만 읽힌 검토를 쓰지 않고 한 번 더 요청한다.

    Cut = `finish_reason` in CUT_REASONS (length / max_tokens)."""
    text = text or ""
    out = ParsedReview(raw=text)
    m = _REVIEW_RE.search(text)
    if m:
        body = m.group(1)
        rest = text[: m.start()] + "\n" + text[m.end():]
    else:                                               # 태그가 없으면 수정 블록을 뺀 나머지에서 JSON을 찾는다
        body = _FIX_BLOCK_RE.sub("\n", text)
        rest = text
    _fix_blocks(rest, out)
    obj: Optional[Any] = None
    fragment = False
    for cand in _json_candidates(body):
        obj, fragment = _decode(cand)
        if obj is not None:
            break
    if obj is None:
        out.error = "no review JSON in the answer" if text.strip() else "empty answer"
        return out
    if fragment:
        out.error = FRAGMENT_ERROR
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
    # LLM이 ok라고 답해도 이슈가 있으면 결과를 issues로 정한다
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
        from sbmig.script.edits import edits_from_json   # JSON 객체 안의 edits도 받아 준다
        edits, problems = edits_from_json(obj["edits"])
        out.edits = edits
        out.edits_problem = ("edits invalid: " + "; ".join(problems[:8])) if problems else ""
        if edits:
            from sbmig.script.notes import answer_notes
            out.changes = answer_notes(out.changes, edits)
    if not out.changes_described and isinstance(obj.get("changes"), list):
        # JSON 객체 안의 changes도 받아 준다
        from sbmig.script.notes import answer_notes, notes_from_json
        notes, _ = notes_from_json(obj["changes"])
        if notes:
            out.changes = answer_notes(notes, out.edits)
            out.changes_described = any(n.change or n.where for n in notes)
    if (finish_reason or "") in CUT_REASONS:
        # 잘린 답변은 JSON이 온전해도 뒤의 이슈나 편집이 빠졌을 수 있으므로 읽지 못한 답으로 본다
        out.error = CUT_ERROR.format(reason=finish_reason)
        return out
    out.ok = True
    return out


# ---------------------------------------------------------------------------- 자료에 없는 내용을 언급한 이슈
UNSUPPORTED_MARK = "(sbmig check) not in the material: "
_ART = r"[A-Za-z][\w]*(?:[.\-][A-Za-z0-9_]+)+"
_VER = r"\d+\.\d+(?:\.\d+)*(?:[.\-][A-Za-z][\w]*)?"
# 아티팩트 이름이나 version 단어에 붙은 버전만 검사한다. "Spring Boot 4.1", "Java [1.8,9)"는 검사하지 않는다
# 예: `g:a:v`, `jakarta.servlet-api 6.1.0`, `slf4j-api is pinned at 2.0.17`, `spring.version=5.3.31`
_ATTACHED_VERSION = re.compile(
    rf"(?:{_ART}|\bversions?\b|버전|<version>)[`'\"]?"
    r"(?:\s*[:=]\s*|\s+(?:(?:is|are|was)\s+)?(?:(?:still\s+)?(?:pinned|kept|set|declared|left|raised|downgraded)\s+)?"
    r"(?:(?:at|to|on|of)\s+)?)"
    rf"[`'\"]?(?P<v>{_VER})\b", re.I)
_PROPERTY_NAMES = re.compile(r"\$\{([^}\s]+)\}|(?<![\w.$-])([A-Za-z][\w-]*(?:\.[\w-]+)*\.version)\b")
_BUILTIN_PREFIXES = ("project.", "java.", "env.", "settings.", "maven.", "os.", "user.", "basedir", "pom.")


def issue_tokens(issue: ReviewIssue) -> tuple[list[str], list[str]]:
    """이슈 문장에 나온 (버전 문자열, 속성 이름)을 뽑는다.
    버전은 아티팩트 이름이나 version 단어 옆에 나온 것만 뽑는다.
    속성은 `${…}`나 `<name>.version` 형태를 뽑고 Maven 내장 속성은 뺀다."""
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
    """이슈가 언급하지만 프롬프트 자료 어디에도 없는 버전과 속성을 찾는다.
    속성은 "What the restructure did"를 뺀 `state`에서 찾으므로,
    재구성이 지운 속성이 남아 있다는 잘못된 지적을 잡아낸다. 버전은 프롬프트 전체에서 찾는다."""
    versions, props = issue_tokens(issue)
    state = material if state is None else state
    return [v for v in versions if not _version_in(v, material)] + [p for p in props if not _property_in(p, state)]


def mark_unsupported(issues: list[ReviewIssue], material: str, state: Optional[str] = None
                     ) -> tuple[list[ReviewIssue], list[str]]:
    """프롬프트 자료에 없는 버전이나 속성을 언급한 이슈에 `unsupported=True`와 문장 앞 표시를 붙인다.
    심각도는 바꾸지 않는다.
    (이슈 목록, 자료에 없던 토큰 목록)을 반환한다. 표시는 UNSUPPORTED_MARK다.

    Issues already marked or starting with "(sbmig check)" are passed through unchanged."""
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
    """스크립트는 있지만 `<changes>` 목록이 없는 리뷰 답변에 붙일 `[sbmig]` 메모를 반환한다.
    그 밖의 경우에는 빈 문자열을 반환한다."""
    from sbmig.fix.script_protocol import CHANGES_MISSING

    if not parsed.script or parsed.changes_described:
        return ""
    return f"{CHANGES_MISSING} ({parsed.changes_problem[:200]})" if parsed.changes_problem else CHANGES_MISSING
