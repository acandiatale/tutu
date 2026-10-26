"""Reading the structure-review answer: `<review>{json}</review>` (+ optional `<analysis>`) and `<script>…</script>`.

Tolerant on purpose: the JSON may sit in a code fence, be followed by prose, carry trailing commas, or come without
the `<review>` tags; `issues` may be a single object instead of a list; unknown severities become `warning`. Nothing
readable → `ParsedReview(ok=False, error=…)` and the reviewer asks once more (then verdict `failed`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import ReviewIssue

MAX_ISSUES = 60
PROBLEM_CHARS = 600
HINT_CHARS = 600
ANALYSIS_CHARS = 4000
FILE_CHARS = 300

_SCRIPT_RE = re.compile(r"<script>(.*?)(?:</script>|\Z)", re.S | re.I)
_REVIEW_RE = re.compile(r"<review>(.*?)(?:</review>|\Z)", re.S | re.I)
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
    ok: bool = False
    verdict: str = "ok"                         # ok | issues (only meaningful when ok)
    issues: list[ReviewIssue] = field(default_factory=list)
    analysis: str = ""
    script: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def critical(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity == "critical"]


def _cap(text: Any, limit: int) -> str:
    s = str(text if text is not None else "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def strip_fences(code: str) -> str:
    """The body of a ```python … ``` fence (or the text as it is)."""
    s = code.strip("\n")
    m = _FENCE_RE.search(s)
    if m and s.lstrip().startswith("```"):
        return m.group(2).strip("\n") + "\n"
    return s.strip("\n") + "\n" if s.strip() else ""


def extract_script(text: str) -> tuple[str, str]:
    """(script, text without the script block). A script is kept only when it defines `fix`."""
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
    """The first JSON object (or array) in `s` that looks like a review; trailing text / commas tolerated."""
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


def parse_review_answer(text: str) -> ParsedReview:
    """`ParsedReview` of one LLM answer (see the module docstring)."""
    out = ParsedReview()
    script, rest = extract_script(text or "")
    m = _REVIEW_RE.search(rest)
    body = m.group(1) if m else rest
    obj: Optional[Any] = None
    for cand in _json_candidates(body):
        obj = _decode(cand)
        if obj is not None:
            break
    if obj is None:
        out.error = "no review JSON in the answer" if (text or "").strip() else "empty answer"
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
    if verdict == "ok" and out.issues:
        out.warnings.append("verdict ok with issues listed → issues")
    out.verdict = "issues" if out.issues else "ok"
    analysis = obj.get("analysis")
    if not (isinstance(analysis, str) and analysis.strip()):
        am = _ANALYSIS_RE.search(text or "")
        analysis = am.group(1) if am else ""
    out.analysis = _cap(analysis, ANALYSIS_CHARS)
    if not script and isinstance(obj.get("script"), str):
        cand = strip_fences(obj["script"])
        script = cand if re.search(r"^\s*def\s+fix\s*\(", cand, re.M) else ""
    out.script = script
    out.ok = True
    return out
