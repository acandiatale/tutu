"""수정 스크립트를 요청하는 LLM 프롬프트에 넣을 문맥을 만드는 모듈.
오류, 오류 파일과 pom 발췌, 이관 노트, 이전 시도를 정해진 문자 수 안에 담는다.

`ContextPacker.pack_script` packs ONE case (L2 signature) that may span many files: the budget is
spread over the erroring files (import block + windows around the error lines, whole file when small);
files beyond it are listed by name only (the script reaches them through `p.errors` / `p.files`).

The budget is `llm.max_context_chars` (default 60,000 characters). `LLMFixer.propose_script` calls
`pack_script` right before the LLM call; an in-house fixer plugin can reuse the same context through
`PackedContext.render()`. The header, the error list, the script being augmented and the project facts after
the restructure are never truncated; the rest shrinks in `SCRIPT_TRUNCATE_ORDER`. The same input must give
the same prompt. Keep the format of the header's `case: <case_id>` line: the replay fixtures' `match` looks
for it. Reference files (copies a module merge dropped) are not in the build; a script must never write
them.

- `FixContext.reference_notes` / `reference_files`: module-merge notes and the dropped first-wins
  copies, read-only.
- `FixContext.library_hints` -> section "Dependency versions" (table entry + what the repository has),
  right after the poms, dropped late; the script prompt's rule 14 is the policy.
- The header's project line is labelled `as-is (before sbmig): …` (`asis_line`); `ctx.layout_facts`
  -> section "Project after the restructure" (order 0.5, never truncated).
- Plan notes reach the prompt only when their first word starts with one of `PROMPT_PLAN_NOTE_PREFIXES`.
- A note scores 3 per keyword found in the error text (messages / symbols / locations / raw / artifacts)
  and 1 per keyword found only in the imports / pom text (`NoteEntry.score_parts`); a note without an
  error-text hit is taken only when no note has one. Truncation starts with `other_errors, plan_notes,
  notes`. Contract: docs/INTERFACES.md "v2.1u … v2.1y".
"""
from __future__ import annotations

import logging
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

from sbmig.core.config import Config, TargetConfig
from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, FixAttempt, ScriptBase
from sbmig.core.workspace import Workspace

log = logging.getLogger("sbmig.fix.context")

WHOLE_FILE_MAX_LINES = 400
WINDOW_RADIUS = 60
POM_WHOLE_MAX_LINES = 250
DEFAULT_MAX_NOTES = 4
POM_KINDS = {ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION,
             ErrorKind.POM_INVALID, ErrorKind.ENFORCER}
POM_MESSAGE_RE = re.compile(r"package \S+ does not exist|cannot access|class file for \S+ not found|"
                            r"Could not (resolve|find artifact)|non-resolvable", re.IGNORECASE)
_LANG = {".java": "java", ".xml": "xml", ".kt": "kotlin", ".yml": "yaml", ".yaml": "yaml",
         ".properties": "properties", ".gradle": "groovy", ".jsp": "jsp", ".json": "json", ".sql": "sql"}


# ---------------------------------------------------------------------------
# 이관 노트 목록
# ---------------------------------------------------------------------------
@dataclass
class NoteEntry:
    """이관 노트 하나를 담는 클래스.
    `knowledge/notes/index.yml` 항목이나 .md 앞머리에서 읽는다. 키워드 정규식이 많이 맞는 노트를 고른다."""
    file: str
    title: str
    keywords: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    always: bool = False
    # 프로젝트 코드로 대체할 수 있는 라이브러리의 노트다. 이 라이브러리는 차단하지 않고 LLM의 거절도 받지 않는다
    replacement: bool = False
    path: Optional[Path] = None
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def compiled(self) -> list[re.Pattern]:
        """키워드 정규식 목록을 반환한다. 잘못된 정규식은 escape해서 문자 그대로 찾는다."""
        if not self._compiled and self.keywords:
            for k in self.keywords:
                try:
                    self._compiled.append(re.compile(k))
                except re.error:
                    self._compiled.append(re.compile(re.escape(k)))
        return self._compiled

    def score(self, text: str) -> int:
        """`text`에서 맞는 키워드 수를 센다."""
        return sum(1 for rx in self.compiled() if rx.search(text))

    def score_parts(self, error_text: str, context_text: str = "") -> tuple[int, int]:
        """오류 본문에 맞은 키워드 수와 import / pom에만 맞은 키워드 수를 센다.
        문맥을 만들 때 앞의 값에 3, 뒤의 값에 1을 곱해서 점수를 낸다."""
        err = ctx = 0
        for rx in self.compiled():
            if rx.search(error_text):
                err += 1
            elif context_text and rx.search(context_text):
                ctx += 1
        return err, ctx

    def applies(self, case: Optional[str], phase: Optional[str]) -> bool:
        """이 노트를 주어진 케이스와 단계에 쓸 수 있는지 확인한다.
        `cases`가 있으면 그 케이스에만 쓴다. `verify` 단계는 항상 허용한다. 모든 오류를 이 단계에서 고친다.

        `phases` is advisory (the phase the note was written for); a phase other than `verify` is
        still narrowed by the list."""
        if self.cases and case and case not in self.cases:
            return False
        if self.phases and phase and phase != "verify" and phase not in self.phases:
            return False
        return True

    def body(self) -> str:
        """노트 본문(앞머리 제외)을 읽는다. 파일이 없으면 빈 문자열을 반환한다."""
        if not self.path or not self.path.exists():
            return ""
        return strip_front_matter(self.path.read_text(encoding="utf-8")).strip()


def split_front_matter(text: str) -> tuple[dict, str]:
    """`---`로 감싼 YAML 앞머리와 본문을 나눈다. 앞머리가 없거나 YAML이 깨졌으면 빈 dict를 반환한다."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def strip_front_matter(text: str) -> str:
    """노트 텍스트에서 앞머리(front matter)를 떼고 본문만 반환한다."""
    return split_front_matter(text)[1]


def load_notes_index(notes_dir: str | Path) -> list[NoteEntry]:
    """노트 목록을 읽는다. `index.yml`이 없으면 디렉토리의 모든 *.md 앞머리로 목록을 만든다."""
    d = Path(notes_dir)
    idx = d / "index.yml"
    entries: list[NoteEntry] = []
    if idx.exists():
        data = yaml.safe_load(idx.read_text(encoding="utf-8")) or {}
        raw = data.get("notes", data) if isinstance(data, dict) else data
        for item in raw or []:
            if not isinstance(item, dict) or "file" not in item:
                continue
            p = d / item["file"]
            meta = split_front_matter(p.read_text(encoding="utf-8"))[0] if p.exists() else {}
            entries.append(NoteEntry(
                file=item["file"],
                title=item.get("title") or meta.get("title") or item["file"],
                keywords=list(item.get("keywords") if item.get("keywords") is not None else meta.get("keywords") or []),
                cases=list(item.get("cases") or meta.get("cases") or []),
                phases=list(item.get("phases") or meta.get("phases") or []),
                always=bool(item.get("always", False)),
                replacement=bool(item.get("replacement", meta.get("replacement", False))),
                path=p,
            ))
    elif d.is_dir():
        for p in sorted(d.glob("*.md")):
            meta = split_front_matter(p.read_text(encoding="utf-8"))[0]
            entries.append(NoteEntry(file=p.name, title=meta.get("title", p.stem),
                                     keywords=list(meta.get("keywords") or []), cases=list(meta.get("cases") or []),
                                     phases=list(meta.get("phases") or []), always=bool(meta.get("always", False)),
                                     replacement=bool(meta.get("replacement", False)), path=p))
    return entries


def replacement_notes(notes: list[NoteEntry], text: str, case: Optional[str] = None,
                      phase: Optional[str] = None) -> list[NoteEntry]:
    """프로젝트 코드로 대체할 수 있는 라이브러리의 노트(``replacement: true``)를 고른다.
    ``text``에 키워드가 맞고 이 케이스에 쓸 수 있는 노트만 고른다.
    이런 노트가 맞는 케이스는 라이브러리 차단도 LLM의 거절도 받지 않는다.

    Libraries sbmig knows how to replace with project code (v2.1z-aa)."""
    out: list[NoteEntry] = []
    for n in notes or []:
        if getattr(n, "replacement", False) and n.applies(case, phase) and n.score(text or "") > 0:
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
TARGET_MARK = " | target: "           # `profile_summary`의 뒷부분(목표 버전)
ASIS_LABEL = "as-is (before sbmig): "  # `profile_summary`의 앞부분(분석 시점 프로젝트)
# Plan notes 중 이 접두어로 시작하는 줄만 프롬프트에 넣는다. LLM의 답에 영향을 주는 줄은 이것뿐이다
PROMPT_PLAN_NOTE_PREFIXES = ("transform", "restructure", "warning", "msa", "followup")
LAYOUT_FACTS_TITLE = "Project after the restructure"   # `FixContext.layout_facts` 절 제목. 이 절은 줄이지 않는다
# 프레임워크 프로젝트(범위 jdk-jakarta)에서 위 절 대신 넣는 절의 제목. 이 절도 줄이지 않는다
SCOPE_FACTS_TITLE = "Scope of this run"
_FIRST_WORD_RE = re.compile(r"^\s*([^\s:\[]+)")


def asis_line(summary: str) -> str:
    """프로젝트 요약 앞에 `as-is (before sbmig): ` 표시를 붙인다.
    이미 붙어 있으면 그대로 두고, 비어 있으면 빈 문자열을 반환한다.
    LLM이 분석 시점의 as-is 정보를 지금 상태로 오해하지 않게 한다."""
    s = (summary or "").strip()
    if not s:
        return ""
    return s if s.startswith(ASIS_LABEL.strip()) else ASIS_LABEL + s


def prompt_plan_notes(notes: Sequence[str]) -> list[str]:
    """프롬프트에 넣을 Plan notes를 고른다.
    첫 단어가 `PROMPT_PLAN_NOTE_PREFIXES`로 시작하는 줄만 순서대로 남긴다.

    The first word ends before `:`, a space or `[`."""
    out: list[str] = []
    for n in notes or ():
        m = _FIRST_WORD_RE.match(str(n))
        if m and m.group(1).lower().startswith(PROMPT_PLAN_NOTE_PREFIXES):
            out.append(str(n))
    return out


def target_text(cfg: Config | None = None) -> str:
    """``cfg.target``으로 프롬프트에 넣을 목표 버전 문구를 만든다. 설정이 없으면 기본값을 쓴다."""
    t = getattr(cfg, "target", None) if cfg is not None else None
    t = t if t is not None else TargetConfig()
    return f"JDK {t.java_version}, Spring Boot {t.spring_boot_version}"


def scope_target_text(cfg: Config | None = None) -> str:
    """프레임워크 프로젝트(범위 jdk-jakarta) 프롬프트에 넣을 목표 문구를 만든다.

    Java from `cfg.target`, jakarta.*, the project's own Spring line kept (v2.1z-ak)."""
    t = getattr(cfg, "target", None) if cfg is not None else None
    t = t if t is not None else TargetConfig()
    return f"JDK {t.java_version} and jakarta.* only (framework project: its own Spring / Spring Boot line is kept)"


def with_target(summary: str, cfg: Config | None = None) -> str:
    """프로젝트 요약의 `` | target: …`` 부분을 설정의 목표 버전으로 바꾼다.
    그 부분이 없거나 요약이 비어 있으면 그대로 둔다.
    수정 루프는 설정 없이 요약을 만든다. 그래서 프롬프트를 만들 때 목표 버전을 고친다.

    Used by the packer and by the verify prompt."""
    if not summary or TARGET_MARK not in summary:
        return summary
    return summary.rsplit(TARGET_MARK, 1)[0] + TARGET_MARK + target_text(cfg)


def fence_for(text: str) -> str:
    """코드 펜스 문자열을 반환한다. 본문에 ```가 있으면 펜스가 중간에 닫히지 않게 ````를 쓴다."""
    return "````" if "```" in text else "```"


def lang_for(path: str) -> str:
    """확장자에 맞는 코드 펜스 언어 이름을 반환한다. 모르는 확장자면 빈 문자열을 반환한다."""
    return _LANG.get(PurePosixPath(path).suffix.lower(), "")


def merge_windows(lines: list[int], total: int, radius: int = WINDOW_RADIUS) -> list[tuple[int, int]]:
    """오류 줄마다 앞뒤 `radius`줄 범위를 만든다. 겹치거나 붙은 범위는 합친다.
    줄 번호가 없으면 파일 앞부분 범위를 반환한다.

    1-based inclusive spans."""
    spans: list[tuple[int, int]] = []
    for ln in sorted(set(l for l in lines if l and l > 0)):
        a, b = max(1, ln - radius), min(total, ln + radius)
        if spans and a <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    if not spans:
        spans = [(1, min(total, 2 * radius))]
    return spans


def summarize_attempt(a: FixAttempt) -> str:
    """스크립트가 아닌 이전 시도를 결과, 분석, 바꾼 파일 순서로 몇 줄로 요약한다.

    For old records that are not script attempts."""
    lines = [f"- attempt {a.id} (iteration {a.iteration}, model {a.model or '-'}) → outcome: {a.outcome}"
             + (f", errors {a.errors_before}→{a.errors_after}" if a.errors_after is not None else "")]
    if a.analysis:
        lines.append(f"    analysis: {a.analysis.strip()[:400]}")
    for c in a.changes[:12]:
        if c.op == "modify":
            s = (c.search or "").strip().splitlines()
            r = (c.replace or "").strip().splitlines()
            lines.append(f"    MODIFY {c.path}: `{(s[0] if s else '')[:100]}` → `{(r[0] if r else '')[:100]}`")
        elif c.op == "rename":
            lines.append(f"    RENAME {c.path} -> {c.new_path}")
        else:
            lines.append(f"    {c.op.upper()} {c.path}")
    if len(a.changes) > 12:
        lines.append(f"    … {len(a.changes) - 12} more change(s)")
    for err in a.apply_errors[:5]:
        lines.append(f"    apply error: {err}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 수정 스크립트 문맥 도우미 (케이스 하나가 여러 파일에 걸친다)
# ---------------------------------------------------------------------------
SCRIPT_MAX_ERROR_LINES = 80          # 프롬프트에 넣는 오류 줄 수. 전체는 `p.errors`에 있다
SCRIPT_ERRORS_SHARE = 0.30           # 오류 목록에 쓰는 예산 비율
SCRIPT_INDEX_SHARE = 0.15            # "Files with errors" 목록에 쓰는 예산 비율
SCRIPT_MAX_INDEX_FILES = 200         # "Files with errors" 목록에 이름을 쓰는 파일 수
SCRIPT_MAX_FILES_SHOWN = 40          # 본문을 보여 주는 최대 파일 수
SCRIPT_MIN_FILE_SHARE = 1500         # 보여 주는 파일 하나에 주는 최소 문자 수
SCRIPT_WINDOW_RADII = (60, 25, 10, 3, 0)   # 오류 줄 주변 범위. 넓은 것부터 시도한다
SCRIPT_OPTIONAL_RESERVE = 0.35       # 파일보다 먼저 힌트 / 노트 / pom 몫으로 남겨 두는 예산 비율
SCRIPT_MAX_POMS = 4
SCRIPT_BASE_MAX_LINES = 60           # 보강 기준 스크립트의 로그 / 남은 오류 / 새 오류 줄 수
# 다른 케이스와 Plan notes를 노트보다 먼저 줄인다. 오류로 고른 노트가 끝까지 남게 한다
SCRIPT_TRUNCATE_ORDER = ("other_errors", "plan_notes", "notes", "previous_attempts", "resolved_before", "kb_hints",
                         "reference_files",
                         "advisory_hints", "reference_notes", "library_hints", "related_files", "files", "pom",
                         "base_result")
ATTEMPT_ANALYSIS_CHARS = 300         # 이전 시도 분석의 최대 길이. 단어 경계에서 자르고 "…"를 붙인다


def format_error_line(e: BuildError, max_message: int = 300) -> str:
    """스크립트 프롬프트에 넣을 오류 한 줄을 만든다. 메시지는 `max_message`자까지 쓴다.

    Shape: `- file:line:col: message | symbol: … | location: …`."""
    loc = (e.file or "(no file)").replace("\\", "/")
    if e.line:
        loc += f":{e.line}"
        if e.column:
            loc += f":{e.column}"
    msg = " ".join((e.message or e.raw or "").split())
    if len(msg) > max_message:
        msg = msg[:max_message - 1] + "…"
    parts = [f"- {loc}: {msg}"]
    if e.symbol:
        parts.append(f"symbol: {e.symbol}")
    if e.location:
        parts.append(f"location: {e.location}")
    if e.artifact:
        parts.append(f"artifact: {e.artifact.coordinates}")
    if e.module and not e.file:
        parts.append(f"module: {e.module}")
    return " | ".join(parts)


def dedupe_errors(errors: list[BuildError]) -> list[BuildError]:
    """케이스의 오류에서 중복을 빼고 파일, 줄, 열, 메시지 순으로 정렬한다.
    리액터 빌드는 같은 오류를 여러 번 보고한다."""
    seen: set[tuple] = set()
    out: list[BuildError] = []
    for e in errors:
        key = ((e.file or "").replace("\\", "/"), e.line or 0, e.column or 0, e.message or "", e.symbol or "",
               e.location or "", e.artifact.coordinates if e.artifact else "")
        if key not in seen:
            seen.add(key)
            out.append(e)
    return sorted(out, key=lambda e: ((e.file or "~").replace("\\", "/"), e.line or 0, e.column or 0, e.message or ""))


def summarize_script_attempt(a: FixAttempt) -> str:
    """이 케이스의 이전 시도를 스크립트 프롬프트용으로 요약한다.
    스크립트 시도면 모드, 버전, 실행 상태를 쓴다. 아니면 `summarize_attempt`를 호출한다."""
    if not a.script_mode:
        return summarize_attempt(a)
    head = (f"- attempt {a.id} (iteration {a.iteration}): script {a.script_mode}"
            + (f" v{a.script_version}" if a.script_version else "") + f" → outcome: {a.outcome}"
            + (f", errors {a.errors_before}→{a.errors_after}" if a.errors_after is not None else ""))
    lines = [head]
    r = a.script_result
    if r is not None:
        lines.append(f"    run: {r.status}" + (f", {len(r.changed_files)} file(s) changed" if r.changed_files else "")
                     + (f" — {r.error.strip()[:300]}" if r.error else ""))
    if a.analysis:
        lines.append(f"    analysis: {shorten(a.analysis, ATTEMPT_ANALYSIS_CHARS)}")
    for ie in a.introduced_errors[:5]:
        lines.append(f"    introduced: {format_error_line(ie, 200)[2:]}")
    return "\n".join(lines)


# 다른 케이스의 시도가 남긴 오류를 설명하는 절, 해결됐다가 다시 나타난 오류를 알리는 절의 문구
EARLIER_FIX_TITLE = "Earlier fix these errors come from (it is in the tree)"
RESOLVED_BEFORE_TITLE = "These errors were resolved before and are back"
RESOLVED_BEFORE_LEAD = ("An earlier attempt on this case made these errors disappear and was kept; a later change "
                        "brought them back. Repeating that attempt alone may be undone again: find what brought the "
                        "errors back and leave both that change's errors and these errors resolved.")


def earlier_fix_sentences(n: int) -> list[str]:
    """"Earlier fix" 절의 마지막 문장들을 반환한다. 설명한 시도 수에 맞춰 단수와 복수를 고른다."""
    if n > 1:
        return ["Undoing those changes brings the errors they fixed back.",
                "Your answer must leave both sets of errors resolved: the errors those attempts fixed and the errors "
                "of this case.",
                "If one of those fixes went the wrong way, replace it in this answer with a change that leaves neither "
                "set of errors."]
    return ["Undoing that change brings the errors it fixed back.",
            "Your answer must leave both sets of errors resolved: the errors that attempt fixed and the errors of "
            "this case.",
            "If that fix went the wrong way, replace it in this answer with a change that leaves neither set of "
            "errors."]


@dataclass
class EarlierFix:
    """이 케이스의 오류를 남긴 다른 케이스의 시도를 프롬프트에 보여 줄 내용으로 담는 클래스.

    `error_lines`: the lines it was fixing (`format_error_line`); `diffs`: per file of this case
    (path, lines, lines not shown); `other_files`: other files of its commit, named only."""
    attempt_id: str
    name: str
    error_lines: list[str] = field(default_factory=list)
    more_errors: int = 0
    diffs: list[tuple[str, list[str], int]] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)
    case_files: list[str] = field(default_factory=list)      # diff를 보여 주지 않은 이 케이스의 파일, 이름만 쓴다


def diff_fence(body: str) -> str:
    """본문의 가장 긴 백틱 연속보다 긴 코드 펜스를 반환한다. 최소 길이는 3이다.
    diff 줄이 펜스를 닫지 않게 한다."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", body or "")), default=0)
    return "`" * max(3, longest + 1)


def earlier_fix_text(fixes: Sequence[EarlierFix], max_chars: int) -> str:
    """이전 수정을 설명하는 절의 본문을 `max_chars`자 안으로 만든다. 시도가 없으면 빈 문자열을 반환한다.
    넘치면 diff 줄, 파일 이름 줄, 오류 줄 순서로 하나씩 뺀다. 시도의 첫 줄과 마지막 문장들은 항상 남긴다.

    Per attempt: head line, the error lines it was fixing, a fenced `diff` block per file of this case,
    the files of this case whose diff is not shown, the other files of its commit; then the closing
    sentences. Diff lines go last attempt, last file, last line first, counted in that file's
    "not shown" line. Contract: docs/INTERFACES.md "v2.1z-by"."""
    if not fixes:
        return ""
    work = [EarlierFix(f.attempt_id, f.name, list(f.error_lines), f.more_errors,
                       [(p, list(ls), more) for p, ls, more in f.diffs], list(f.other_files), list(f.case_files))
            for f in fixes]
    sentences = earlier_fix_sentences(len(work))
    dropped = 0

    def render() -> str:
        out: list[str] = []
        for f in work:
            out.append(f"Attempt {f.attempt_id} ({f.name}) fixed these errors of another case and is kept in the tree:")
            out.extend(f.error_lines)
            if f.more_errors:
                out.append(f"… {f.more_errors} more error(s) of that case")
            for path, lines, more in f.diffs:
                if lines:
                    body = "\n".join(lines)
                    fence = diff_fence(body)
                    out.append(f"What it changed in {path}:")
                    out.append(f"{fence}diff\n{body}\n{fence}")
                if more:
                    out.append(f"… {more} more changed line(s) in {path} not shown")
            if f.case_files:
                out.append("It also changed these files of this case (diff not shown): " + ", ".join(f.case_files))
            if f.other_files:
                out.append("It also changed (not shown): " + ", ".join(f.other_files))
        if dropped:
            out.append(f"(… {dropped} line(s) left out to fit)")
        out.extend(sentences)
        return "\n".join(out)

    def drop_one() -> bool:
        nonlocal dropped
        for f in reversed(work):
            for i in range(len(f.diffs) - 1, -1, -1):
                path, lines, more = f.diffs[i]
                if lines:
                    f.diffs[i] = (path, lines[:-1], more + 1)
                    return True
        for f in reversed(work):
            if f.other_files:
                f.other_files = []
                dropped += 1
                return True
            if f.case_files:
                f.case_files = []
                dropped += 1
                return True
        for f in reversed(work):
            if f.error_lines:
                f.error_lines.pop()
                f.more_errors += 1
                return True
        return False

    text = render()
    while len(text) > max_chars and drop_one():
        text = render()
    return text


def shorten(text: str, width: int) -> str:
    """`text`를 단어 경계에서 잘라 `width`자 이하의 한 줄로 줄인다.
    공백 없는 긴 문자열(경로, 스택)은 "…"만 남지 않도록 문자 단위로 자른다."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    out = textwrap.shorten(flat, width, placeholder="…")
    if len(out) < width // 2:
        out = flat[:width - 1].rstrip() + "…"
    return out


def _first_line(text: str, limit: int = 100) -> str:
    """비어 있지 않은 첫 줄을 `limit`자에서 잘라 반환한다. 모두 비어 있으면 빈 문자열을 반환한다."""
    line = next((ln.strip() for ln in (text or "").split("\n") if ln.strip()), "")
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _capped(items: list[str], limit: int, what: str = "line(s)") -> list[str]:
    out = list(items[:limit])
    if len(items) > limit:
        out.append(f"… {len(items) - limit} more {what}")
    return out


def script_base_result(base: ScriptBase) -> str:
    """보강할 스크립트가 이 프로젝트에서 한 일을 보강 프롬프트용으로 정리한다.
    결과, 실행 상태와 오류, 바꾼 파일, 로그 뒷부분, 남은 오류, 새로 생긴 오류, 이전 버전을 담는다."""
    lines = [f"outcome in this project: {base.outcome or 'not run in this project'}"]
    if base.status:
        lines.append(f"run status: {base.status}")
    if base.error:
        lines.append("error: " + base.error.strip()[:2000])
    if base.changed_files:
        lines.append(f"changed files ({len(base.changed_files)}): " + ", ".join(base.changed_files[:40])
                     + (" …" if len(base.changed_files) > 40 else ""))
    if base.log:
        shown = base.log[-SCRIPT_BASE_MAX_LINES:]
        lines.append(f"log ({len(base.log)} line(s)" + (f", last {len(shown)} shown" if len(shown) < len(base.log)
                                                          else "") + "):")
        lines += [f"    {ln}" for ln in shown]
    if base.remaining_errors:
        lines.append(f"errors of this case still present after it ({len(base.remaining_errors)}):")
        lines += [f"- {x}" for x in _capped(base.remaining_errors, SCRIPT_BASE_MAX_LINES, "error(s)")]
    if base.introduced_errors:
        lines.append(f"errors it introduced in the files it touched ({len(base.introduced_errors)}):")
        lines += [f"- {x}" for x in _capped(base.introduced_errors, SCRIPT_BASE_MAX_LINES, "error(s)")]
    if base.history:
        lines.append("earlier versions:")
        lines += [f"- {x}" for x in _capped(base.history, 20, "version(s)")]
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n…[truncated {len(text) - max_chars} chars]"
    return text[: max(0, max_chars - len(marker))] + marker


_PARENT_BLOCK_RE = re.compile(r"<parent>.*?</parent>", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ARTIFACT_ID_RE = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>")


def pom_artifact_id(ws: Workspace, pom: str) -> Optional[str]:
    """pom 자신의 artifactId를 읽는다. parent의 것은 보지 않는다. 읽지 못하면 None을 반환한다."""
    try:
        text = _PARENT_BLOCK_RE.sub("", _COMMENT_RE.sub("", ws.read(pom)))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    m = _ARTIFACT_ID_RE.search(text)
    return m.group(1) if m else None


def nearest_pom(ws: Workspace, rel: Optional[str]) -> Optional[str]:
    """파일이 속한 모듈의 pom을 찾는다. 루트 쪽으로 최대 128단계까지 올라가고, 없으면 None을 반환한다.
    역슬래시는 '/'로 바꾼다. 절대 경로는 워크스페이스 안이면 상대 경로로 바꾼다. 밖이면 루트 pom을 쓴다.

    `PurePosixPath("/").parent` is `/`: an absolute path must never reach the walk, which is
    depth-bounded."""
    if not rel:
        return "pom.xml" if ws.exists("pom.xml") else None
    norm = str(rel).replace("\\", "/")
    p = PurePosixPath(norm)
    if p.is_absolute() or (len(norm) > 1 and norm[1] == ":"):          # posix 절대 경로나 Windows 드라이브 경로
        try:
            norm = Path(norm).resolve().relative_to(Path(ws.root).resolve()).as_posix()
        except (ValueError, OSError):
            return "pom.xml" if ws.exists("pom.xml") else None
        p = PurePosixPath(norm)
    if p.name == "pom.xml" and ws.exists(norm):
        return norm
    cur = p.parent
    for _ in range(128):                                               # 무한 루프를 막으려고 깊이를 제한한다
        cand = (cur / "pom.xml").as_posix()
        cand = cand[2:] if cand.startswith("./") else cand
        if ws.exists(cand):
            return cand
        if str(cur) in ("", ".", "/") or cur.parent == cur:
            return None
        cur = cur.parent
    return None


_POM_SECTIONS = ("parent", "properties", "dependencyManagement", "dependencies", "build")


def pom_excerpt(text: str, path: str) -> str:
    """pom 발췌를 만든다. `POM_WHOLE_MAX_LINES`줄 이하면 통째로 쓴다.
    더 길면 parent / properties / dependencyManagement / dependencies / build 섹션만 줄 번호와 함께 보여 준다."""
    lines = text.split("\n")
    if len(lines) <= POM_WHOLE_MAX_LINES:
        return f"// {path} (whole file)\n```xml\n{text.rstrip()}\n```"
    out = []
    taken: list[tuple[int, int]] = []
    for tag in _POM_SECTIONS:
        for m in re.finditer(rf"<{tag}>.*?</{tag}>", text, re.DOTALL):
            a, b = m.start(), m.end()
            if any(x <= a and b <= y for x, y in taken):
                continue                                 # dependencyManagement 안의 <dependencies> 같은 중첩은 건너뛴다
            # 줄 단위로 넓힌다
            ls = text.rfind("\n", 0, a) + 1
            le = text.find("\n", b)
            le = len(text) if le < 0 else le
            taken.append((ls, le))
            l1 = text.count("\n", 0, ls) + 1
            l2 = text.count("\n", 0, le) + 1
            out.append((l1, f"// {path} lines {l1}-{l2} <{tag}>\n```xml\n{text[ls:le]}\n```"))
            break
    out.sort()
    return "\n".join(s for _, s in out) or f"// {path} (no standard sections found)"


# ---------------------------------------------------------------------------
# 문맥 만들기
# ---------------------------------------------------------------------------
@dataclass
class PackedContext:
    """만들어진 문맥을 담는 클래스.
    프롬프트 순서의 섹션 목록, 보여 준 파일, 이름만 쓴 파일, 넣은 노트, 줄인 섹션, 문자 수를 가진다."""
    sections: list[tuple[str, str]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)          # 섹션마다의 키. 세션을 이어 갈 때 이 키로 섹션을 고른다
    files_included: list[str] = field(default_factory=list)
    notes_included: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    chars: int = 0
    files_listed: list[str] = field(default_factory=list)   # 이름만 쓰고 본문은 보여 주지 않은 오류 파일

    def render(self) -> str:
        """섹션들을 `## 제목`과 본문으로 된 markdown 하나로 합친다."""
        return "\n\n".join(f"## {t}\n{body}" for t, body in self.sections)


@dataclass
class _Section:
    key: str
    title: str
    text: str
    order: float
    truncatable: bool = True
    always: bool = False


class ContextPacker:
    """수정 스크립트 프롬프트에 넣을 문맥을 예산 안에서 만드는 클래스(`pack_script`).
    노트는 처음 쓸 때 `knowledge/notes`에서 읽는다."""
    # `SCRIPT_TRUNCATE_ORDER`의 앞쪽부터 줄인다. "errors"와 "header"는 줄이지 않는다
    MAX_RELATED_FILES = 3
    ADVISORY_TITLE = "Library migration guidance (what to migrate TO)"
    # `FixContext.library_hints` 절 제목. 표의 항목과 저장소에 있는 버전(`sbmig.build.repo_versions`)을 보여 준다
    LIBRARY_HINTS_TITLE = "Dependency versions (what the repository has; policy: same major first, see rule 14)"
    REFERENCE_NOTES_TITLE = "Module merge notes (first-wins; the dropped copy is below as reference)"

    def __init__(self, cfg: Config, notes_dir: Optional[str | Path] = None, max_notes: int = DEFAULT_MAX_NOTES):
        self.cfg = cfg
        self.notes_dir = Path(notes_dir) if notes_dir else cfg.resolve_path("knowledge/notes")
        self.max_notes = max_notes
        self._notes: Optional[list[NoteEntry]] = None

    @property
    def notes(self) -> list[NoteEntry]:
        """노트 목록을 반환한다. 처음 쓸 때 읽는다.
        인덱스가 깨졌으면 경고만 남기고 빈 목록을 반환한다. 수정은 계속돼야 한다."""
        if self._notes is None:
            try:
                self._notes = load_notes_index(self.notes_dir)
            except Exception as exc:  # 인덱스가 깨져도 수정은 멈추지 않는다
                log.warning("cannot load notes index from %s: %s", self.notes_dir, exc)
                self._notes = []
        return self._notes

    # ------------------------------------------------------------------ public
    def select_notes(self, text: str, case: Optional[str], phase: Optional[str], *,
                     context: str = "") -> list[NoteEntry]:
        """점수가 높은 노트를 최대 `max_notes`개 고른다. always 노트가 앞에 온다.
        `text`는 오류 본문, `context`는 import 줄과 pom 발췌다.
        오류 본문에 맞은 노트가 있으면 그런 노트만 고른다.

        Score = 3 x keywords in the error text + 1 x keywords only in the context. `pack_script` leaves the
        always-notes out."""
        scored = []
        always = []
        for idx, n in enumerate(self.notes):
            if n.always:
                always.append(n)
                continue
            if not n.applies(case, phase):
                continue
            err, ctx_hits = n.score_parts(text, context)
            s = 3 * err + ctx_hits
            if s > 0:
                scored.append((-s, idx, n, err))
        if any(err for *_, err in scored):
            scored = [x for x in scored if x[3]]
        scored.sort(key=lambda t: (t[0], t[1]))
        top = [n for _, _, n, _ in scored[: self.max_notes]]
        return always + top            # always 노트는 맨 앞에 쓰고 맨 나중에 줄인다

    # ------------------------------------------------------------------ 수정 스크립트 문맥
    SCRIPT_FILES_TITLE = "Source (exact text around the error lines; line numbers as in p.errors)"
    SCRIPT_INDEX_TITLE = "Files with errors"
    SCRIPT_REFERENCE_FILES_TITLE = ("Reference files (read-only, NOT in the build — the script must never write "
                                    "these paths)")
    SCRIPT_RELATED_FILES_TITLE = ("Related project types named in the errors (exact text — the script edits these when "
                                  "the fix belongs there, e.g. a repository interface that lost its CRUD methods)")

    def pack_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, profile_summary: str = "",
                    *, case_type: Optional[str] = None, budget: Optional[int] = None) -> PackedContext:
        """수정 스크립트 요청에 넣을 문맥을 만든다. 여러 파일에 걸칠 수 있는 케이스 하나를 다룬다.
        섹션 순서는 머리말, 재구성 뒤 프로젝트, 오류 목록, 보강할 스크립트와 결과, 소스 발췌, 참고 파일,
        POM, 의존성 버전, 권고, KB 힌트, 이전 시도, 다른 케이스, Plan notes, 노트다.
        같은 입력이면 같은 결과가 나온다.

        `llm.max_context_chars` is spread over the erroring files (small files first, their surplus
        goes to the big ones); files beyond the budget are listed by name only. Poms of the modules
        involved when the case is a resolution / pom error. The header's project line is
        `asis_line(summary)`; notes are selected by
        `select_notes(error_text, …, context=imports + pom text)`."""
        budget = budget or self.cfg.llm.max_context_chars
        summary = with_target(profile_summary or ctx.profile_summary, self.cfg)     # 목표 버전은 설정에서 가져온다
        reduced = getattr(ctx, "scope", "full") == "jdk-jakarta"
        if reduced and summary and TARGET_MARK in summary:   # 프레임워크 프로젝트는 Boot 목표가 없다
            summary = summary.rsplit(TARGET_MARK, 1)[0] + TARGET_MARK + scope_target_text(self.cfg)
        if case_type is None:
            m = re.search(r"case=(\w+)", summary or "")
            case_type = m.group(1) if m else None
        packed = PackedContext()
        secs: list[_Section] = []
        errors = dedupe_errors(cluster.errors)
        by_file: dict[str, list[int]] = {}
        counts: dict[str, int] = {}
        for e in errors:
            if not e.file:
                continue
            f = e.file.replace("\\", "/")
            by_file.setdefault(f, [])
            counts[f] = counts.get(f, 0) + 1
            if e.line and e.line not in by_file[f]:
                by_file[f].append(e.line)
        for lines_of in by_file.values():
            lines_of.sort()
        base = ctx.script_base
        mode = ctx.script_mode or ("augment" if base is not None else "new")
        if mode == "augment" and base is None:
            mode = "new"

        # 머리말과 오류 목록은 줄이지 않고 형식도 바꾸지 않는다
        # 재생 픽스처의 `match: "case: <case_id>"`가 이 줄로 답을 고른다
        case_id = ctx.case_id or cluster.case_id or cluster.id
        sig = ctx.signature_generic or ", ".join(cluster.generic_signatures[:3])
        modules = sorted({e.module for e in errors if e.module} | ({cluster.module} if cluster.module else set()))
        if mode == "augment" and base is not None:
            what = (f"augment version {base.version} of this case's fix script" if base.version
                    else "augment this run's fix script for the case") + \
                " — return the COMPLETE new script (or <no_script> + <edits>)"
        else:
            what = "fix this case now (<edits>) and write a reusable fix script for it (no stored script exists for it)"
        header = [f"request: {what}", f"case: {case_id}" + (f" — L2 signature: {sig}" if sig else ""),
                  f"kind: {cluster.kind.value}" + (f", module(s): {', '.join(modules)}" if modules else ""),
                  f"errors: {len(errors)} in {len(by_file)} file(s) — every one of them is in p.errors"]
        if summary:
            header.append(asis_line(summary))
        header.append(f"phase: {ctx.phase.value}, iteration {ctx.iteration}"
                      + (f", stall level {ctx.stall_level}" if ctx.stall_level else ""))
        secs.append(_Section("header", "Case", "\n".join(header), 0, truncatable=False))
        # 재구성 뒤 프로젝트 정보(모듈 역할, 생성 파일, 사내 규칙)는 머리말 바로 뒤에 두고 줄이지 않는다
        # 프레임워크 프로젝트는 재구성을 하지 않는다. 그 자리에 이번 실행의 범위를 넣는다
        if reduced and getattr(ctx, "scope_facts", None):
            secs.append(_Section("scope_facts", SCOPE_FACTS_TITLE, "\n".join(
                ln if ln.lstrip().startswith("- ") else f"- {ln}" for ln in ctx.scope_facts if str(ln).strip()),
                0.5, truncatable=False))
        elif ctx.layout_facts and not reduced:
            secs.append(_Section("layout_facts", LAYOUT_FACTS_TITLE, "\n".join(
                ln if ln.lstrip().startswith("- ") else f"- {ln}" for ln in ctx.layout_facts if str(ln).strip()),
                0.5, truncatable=False))
        err_cap = max(2000, int(budget * SCRIPT_ERRORS_SHARE))
        err_lines: list[str] = []
        used = 0
        for e in errors[:SCRIPT_MAX_ERROR_LINES]:
            line = format_error_line(e)
            if err_lines and used + len(line) + 1 > err_cap:
                break
            err_lines.append(line)
            used += len(line) + 1
        if len(errors) > len(err_lines):
            err_lines.append(f"… {len(errors) - len(err_lines)} more error(s) — the script gets all of them in p.errors")
        secs.append(_Section("errors", f"Errors of this case ({len(errors)})", "\n".join(err_lines), 1,
                             truncatable=False))
        # 다른 케이스의 시도가 남긴 오류면 그 시도를 오류 목록 바로 뒤에 둔다. 길이는 수정 루프가 이미 맞췄다
        earlier = (getattr(ctx, "earlier_fix", "") or "").strip()
        if earlier:
            secs.append(_Section("earlier_fix", EARLIER_FIX_TITLE, earlier, 1.1, truncatable=False))

        # 보강 답은 이 스크립트를 대체하는 완전한 새 스크립트여야 한다. 그래서 본문을 줄이지 않는다
        if mode == "augment" and base is not None:
            label = f"version {base.version}" if base.version else "this run's script, not stored"
            code = base.code.replace("\r\n", "\n").rstrip("\n")
            fence = fence_for(code)
            secs.append(_Section("base_code", f"Script to extend ({label}) — answer with the COMPLETE new script",
                                 f"{fence}python\n{code}\n{fence}", 1.5, truncatable=False))
            secs.append(_Section("base_result", f"What {label} did in this project", script_base_result(base), 1.6))

        # 의존성 해석이나 pom 케이스면 관련 모듈의 pom을 넣는다
        files = list(by_file)
        if self._wants_pom(cluster, files):
            for i, pom in enumerate(self._script_poms(ws, cluster, files)):
                try:
                    secs.append(_Section("pom", f"POM ({pom})", pom_excerpt(ws.read(pom), pom), 3 + i / 100))
                    packed.files_included.append(pom)
                except (OSError, UnicodeDecodeError, ValueError):
                    pass

        related = self._related_type_files(ws, cluster, files)
        if related:
            blocks = [self._file_block(ws, f, [])[0] for f in related]
            secs.append(_Section("related_files", self.SCRIPT_RELATED_FILES_TITLE, "\n\n".join(blocks), 2.2))
        if ctx.reference_files:
            secs.append(_Section("reference_files", self.SCRIPT_REFERENCE_FILES_TITLE,
                                 "\n\n".join(self._reference_block(label, text)
                                             for label, text in sorted(ctx.reference_files.items())), 2.5))
        if ctx.library_hints:                          # pom 절 바로 뒤, 권고 절 앞에 둔다
            secs.append(_Section("library_hints", self.LIBRARY_HINTS_TITLE,
                                 "\n".join(f"- {h}" for h in ctx.library_hints), 3.5))
        if ctx.advisory_hints:
            who = ("The script rewrites the code that still uses them to the named replacement (public signatures "
                 "kept so callers still compile); it never re-adds the removed library.")
            secs.append(_Section("advisory_hints", self.ADVISORY_TITLE,
                                 "The libraries below were removed or replaced by the migration on purpose. " + who + "\n"
                                 + "\n".join(f"- {h}" for h in ctx.advisory_hints), 4))
        if ctx.reference_notes:
            secs.append(_Section("reference_notes", self.REFERENCE_NOTES_TITLE,
                                 "\n".join(f"- {n}" for n in ctx.reference_notes), 4.5))
        if ctx.kb_hints:
            secs.append(_Section("kb_hints", "Knowledge-base hints (similar cases)",
                                 "\n".join(f"- {h}" for h in ctx.kb_hints), 5))
        # 오류를 없앴던 시도는 "did NOT resolve it" 절이 아니라 다시 나타난 오류의 절에 둔다
        back = list(getattr(ctx, "resolved_before", None) or [])
        # 객체가 아니라 시도 id로 비교한다. FixContext를 복사해도 같은 시도로 본다
        back_ids = {a.id for a in back}
        not_resolved = [a for a in ctx.previous_attempts if a.id not in back_ids]
        if back:
            secs.append(_Section("resolved_before", RESOLVED_BEFORE_TITLE,
                                 RESOLVED_BEFORE_LEAD + "\n" + "\n".join(summarize_script_attempt(a) for a in back),
                                 5.9))
        if not_resolved:
            secs.append(_Section("previous_attempts",
                                 "Previous attempts on this case (they did NOT resolve it — do not repeat them)",
                                 "\n".join(summarize_script_attempt(a) for a in not_resolved), 6))
        if ctx.other_errors_summary:
            secs.append(_Section("other_errors", "Other error cases in this build (for awareness; fix only this case)",
                                 ctx.other_errors_summary.strip(), 7))
        plan_lines = prompt_plan_notes(ctx.plan_notes)      # 답에 영향을 주는 줄만 넣는다
        if plan_lines:
            secs.append(_Section("plan_notes", "Plan notes", "\n".join(f"- {n}" for n in plan_lines), 8))

        # 오류 파일은 다른 절이 쓰고 남은 예산으로 보여 준다
        renderings, import_text = self._script_renderings(ws, by_file)
        # 노트를 고를 때 오류 본문(가중치 3)과 import / pom(가중치 1)을 따로 본다
        error_text = "\n".join(
            [e.message for e in errors] + [e.symbol or "" for e in errors] + [e.location or "" for e in errors]
            + [e.raw[:2000] for e in errors[:40]] + [e.artifact.coordinates for e in errors if e.artifact])
        context_text = "\n".join([import_text] + [s.text for s in secs if s.key == "pom"])
        # 수정 정책은 스크립트 시스템 프롬프트에 이미 있다. always 노트는 빼고 키워드로 고른 노트만 넣는다
        notes = [n for n in self.select_notes(error_text, case_type, ctx.phase.value, context=context_text)
                 if not n.always]
        for i, n in enumerate(notes):
            body = n.body()
            if body:
                secs.append(_Section("notes", f"Migration note: {n.title}", body, 10 + i, always=n.always))

        def size(s: _Section) -> int:
            return len(s.title) + len(s.text) + 6

        # 오류 파일에는 남은 예산을 준다. 줄일 수 없는 절, base_result, pom, 선택 절 몫(최대 35%)을 뺀 나머지다
        # 오류 파일 예산은 최소 `SCRIPT_MIN_FILE_SHARE`자다
        late = ("base_result", "pom")
        fixed = sum(size(s) for s in secs if not s.truncatable or s.key in late)
        optional = sum(size(s) for s in secs if s.truncatable and s.key not in late)
        index_cap = max(1500, int(budget * SCRIPT_INDEX_SHARE))
        index_estimate = len(self._file_index(by_file, counts, set(), set(), index_cap)) + 40 \
            if by_file else 0
        avail = budget - fixed - index_estimate
        file_budget = max(SCRIPT_MIN_FILE_SHARE, avail - min(optional, int(max(avail, 0) * SCRIPT_OPTIONAL_RESERVE)))
        chosen = self._allocate_files(by_file, counts, renderings, file_budget)
        shown = sorted(chosen)
        missing = sorted(f for f in by_file if PurePosixPath(f).name != "pom.xml" and not ws.exists(f))
        packed.files_included = [*shown, *packed.files_included]
        packed.files_listed = sorted(f for f in by_file if f not in chosen and f not in missing
                                     and PurePosixPath(f).name != "pom.xml")
        packed.files_included += [f for f in related if f not in packed.files_included]
        if by_file:
            secs.append(_Section("file_index", f"{self.SCRIPT_INDEX_TITLE} ({len(by_file)})",
                                 self._file_index(by_file, counts, set(chosen), set(missing), index_cap),
                                 1.8, truncatable=False))
        if shown:
            secs.append(_Section("files", self.SCRIPT_FILES_TITLE,
                                 "\n\n".join(chosen[f] for f in shown), 2))

        self._fit(secs, budget, packed, order=SCRIPT_TRUNCATE_ORDER)
        secs.sort(key=lambda s: s.order)
        packed.sections = [(s.title, s.text) for s in secs]
        packed.keys = [s.key for s in secs]
        packed.notes_included = [s.title.removeprefix("Migration note: ") for s in secs if s.key == "notes"]
        packed.chars = sum(len(t) + len(b) + 4 for t, b in packed.sections)
        return packed

    def _script_poms(self, ws: Workspace, cluster: ErrorCluster, files: list[str]) -> list[str]:
        """케이스와 관련된 모듈의 pom 목록을 최대 `SCRIPT_MAX_POMS`개 만든다.
        - 오류가 난 pom, 오류 파일의 모듈 pom, 파일 없는 오류가 가리킨 모듈의 pom 순서로 담는다
        - 의존성 해석이나 pom 오류, 또는 고른 pom이 없으면 루트 pom을 더한다
        """
        out: list[str] = []

        def add(p: Optional[str]) -> None:
            if p and p not in out:
                out.append(p)

        for f in files:
            if PurePosixPath(f).name == "pom.xml" and ws.exists(f):
                add(f)
        for f in files:
            add(nearest_pom(ws, f))
        mods = sorted({e.module for e in cluster.errors if e.module and not e.file})
        if mods:
            try:
                poms = sorted(p.replace("\\", "/") for p in ws.pom_files())
            except Exception:
                poms = []
            for m in mods:
                # 오류의 모듈 이름은 artifactId다. 폴더 이름과 다를 때가 많다
                # 폴더 이름으로 먼저 찾고, 없으면 pom의 artifactId로 찾는다
                add(next((p for p in poms if PurePosixPath(p).parent.name == m), None)
                    or next((p for p in poms if pom_artifact_id(ws, p) == m), None))
        if ws.exists("pom.xml") and (not out or cluster.kind in POM_KINDS):
            add("pom.xml")
        return out[:SCRIPT_MAX_POMS]

    def _script_renderings(self, ws: Workspace, by_file: dict[str, list[int]]) -> tuple[dict[str, list[str]], str]:
        """오류 파일마다 보여 줄 수 있는 형태를 자세한 것부터 만든다.
        작으면 파일 전체, 다음은 import 블록과 점점 좁아지는 범위, 마지막은 import 없이 범위만 쓴다.
        노트 선택에 쓸 import 줄도 함께 반환한다. pom은 POM 절이 따로 있어서 뺀다."""
        out: dict[str, list[str]] = {}
        imports_all: list[str] = []
        for f in sorted(by_file):
            if PurePosixPath(f).name == "pom.xml":
                continue
            try:
                if not ws.exists(f):
                    continue
                text = ws.read(f).replace("\r\n", "\n")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            imports_all += [ln for ln in lines if ln.startswith(("import ", "package "))]
            total = len(lines)
            imp = self._import_range(lines) if f.endswith(".java") else None
            cands: list[str] = []
            if total <= WHOLE_FILE_MAX_LINES:
                cands.append(self._render_spans(f, lines, [(1, total)] if total else []))
            for r in SCRIPT_WINDOW_RADII:
                cands.append(self._render_spans(f, lines, self._spans(imp, by_file[f], r, total)))
            if imp:
                for r in (3, 0):
                    cands.append(self._render_spans(f, lines, self._spans(None, by_file[f], r, total)))
            out[f] = list(dict.fromkeys(c for c in cands if c))
        return out, "\n".join(imports_all) + "\n"

    @staticmethod
    def _allocate_files(by_file: dict[str, list[int]], counts: dict[str, int], renderings: dict[str, list[str]],
                        budget: int) -> dict[str, str]:
        """오류 파일에 문자 예산을 나눈다.
        오류가 많은 파일부터 최대 `SCRIPT_MAX_FILES_SHOWN`개를 고른다. 파일 하나에 최소 `SCRIPT_MIN_FILE_SHARE`자를 준다.
        작은 파일이 남긴 예산은 큰 파일이 쓴다. 예산에 맞는 형태가 없는 파일은 이름만 쓴다.

        Water-filling: each file takes its most informative rendering that fits its share."""
        ranked = sorted((f for f in renderings if renderings[f]), key=lambda f: (-counts.get(f, 0), f))
        max_shown = max(1, min(SCRIPT_MAX_FILES_SHOWN, budget // SCRIPT_MIN_FILE_SHARE))
        cands = ranked[:max_shown]
        order = sorted(cands, key=lambda f: (len(renderings[f][0]), f))
        chosen: dict[str, str] = {}
        remaining, left = budget, len(order)
        for f in order:
            share = remaining // max(left, 1)
            left -= 1
            pick = next((r for r in renderings[f] if len(r) + 2 <= share), None)
            if pick is None:
                continue
            chosen[f] = pick
            remaining -= len(pick) + 2
        return chosen

    @staticmethod
    def _file_index(by_file: dict[str, list[int]], counts: dict[str, int], shown: set[str], missing: set[str],
                    max_chars: int = 30000) -> str:
        """오류 파일 목록을 만든다. 파일마다 오류 수와 줄 번호를 쓰고, 보여 준 파일이 먼저 온다.
        보여 주지 않은 파일은 "not shown"으로 표시한다. 스크립트는 그 내용을 `p.errors` / `p.files`로 읽는다.

        Within `max_chars` and `SCRIPT_MAX_INDEX_FILES` entries."""
        lines = [("Files marked \"not shown\" are not in the source section — the script must reach them through "
                  "`p.errors` / `p.files(...)`.")]
        names = sorted(by_file, key=lambda f: (f not in shown, f))
        used = len(lines[0])
        listed = 0
        for f in names[:SCRIPT_MAX_INDEX_FILES]:
            ls = by_file[f]
            where = (", line(s) " + ", ".join(str(x) for x in ls[:12]) + (" …" if len(ls) > 12 else "")) if ls else ""
            if PurePosixPath(f).name == "pom.xml":
                mark = " (see the POM section)"
            elif f in missing:
                mark = " (file not found in the project)"
            elif f in shown:
                mark = ""
            else:
                mark = " (not shown)"
            entry = f"- {f} — {counts.get(f, 0)} error(s){where}{mark}"
            if listed and used + len(entry) + 1 > max_chars:
                break
            lines.append(entry)
            used += len(entry) + 1
            listed += 1
        if len(names) > listed:
            lines.append(f"… {len(names) - listed} more file(s), not shown (all of them in p.errors)")
        return "\n".join(lines)

    @staticmethod
    def _import_range(lines: list[str]) -> Optional[tuple[int, int]]:
        """Java 파일의 첫 package / import 줄과 마지막 import 줄 번호를 반환한다. 없으면 None을 반환한다.

        1-based."""
        first = next((i for i, ln in enumerate(lines) if ln.startswith(("package ", "import "))), None)
        if first is None:
            return None
        last = max((i for i, ln in enumerate(lines) if ln.startswith("import ")), default=first)
        return first + 1, max(first, last) + 1

    @staticmethod
    def _spans(imp: Optional[tuple[int, int]], err_lines: list[int], radius: int, total: int) -> list[tuple[int, int]]:
        """import 블록과 오류 줄 앞뒤 `radius`줄 범위를 만든다. 간격이 3줄 이하면 합친다.
        줄 번호가 없거나 맞지 않으면 파일 앞부분을 쓴다."""
        if total <= 0:
            return []
        spans: list[tuple[int, int]] = [imp] if imp else []
        lines = sorted({ln for ln in err_lines if 0 < ln <= total})
        if lines:
            spans += [(max(1, ln - radius), min(total, ln + radius)) for ln in lines]
        else:                                            # 줄 번호가 없거나 맞지 않으면 파일 앞부분을 쓴다
            spans.append((1, min(total, max(2 * radius, 20))))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 4:          # 간격이 3줄 이하면 합친다. 머리줄을 하나 더 쓰는 것보다 짧다
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    @staticmethod
    def _render_spans(path: str, lines: list[str], spans: list[tuple[int, int]]) -> str:
        """범위마다 `// path lines a-b (of N)` 머리줄과 코드 펜스를 만든다. 줄 번호는 `p.errors`와 같다."""
        if not spans:
            return f"// {path} (empty file)"
        total = len(lines)
        lang = lang_for(path)
        if spans == [(1, total)]:
            body = "\n".join(lines)
            fence = fence_for(body)
            return f"// {path} lines 1-{total} (whole file)\n{fence}{lang}\n{body}\n{fence}"
        out = []
        for a, b in spans:
            body = "\n".join(lines[a - 1:b])
            fence = fence_for(body)
            out.append(f"// {path} lines {a}-{b} (of {total})\n{fence}{lang}\n{body}\n{fence}")
        return "\n".join(out)

    # ------------------------------------------------------------------ 내부 도우미
    _LOC_TYPE_RES = (re.compile(r"location:\s*variable\s+[\w$]+\s+of\s+type\s+([\w$.]+)"),
                     re.compile(r"location:\s*(?:class|interface|enum|record)\s+([\w$.]+)"),
                     re.compile(r"(?:method|constructor)\s+([\w$.]+)\.[\w$]+\([^)]*\)\s+is not applicable"))
    _IMPORT_LINE_RE = re.compile(r"^[ \t]*import\s+(?!static\b)([\w.]+)\s*;", re.M)
    _PACKAGE_LINE_RE = re.compile(r"^[ \t]*package\s+([\w.]+)\s*;", re.M)

    def _related_type_files(self, ws: Workspace, cluster: ErrorCluster, cluster_files: list[str]) -> list[str]:
        """오류가 가리킨 프로젝트 타입의 소스 파일을 찾는다. FQCN이나 오류 파일의 import / package로 찾는다.
        케이스 파일 자신과 라이브러리 타입은 빼고 최대 `MAX_RELATED_FILES`개를 반환한다.

        Type names come from `location: variable x of type T`, `location: class T` and
        `method T.m(…) is not applicable`."""
        try:
            java_files = ws.java_files()
        except Exception:
            return []
        if not java_files:
            return []
        out: list[str] = []
        skip = {f.replace("\\", "/") for f in cluster_files}
        for e in cluster.errors:
            texts = [t for t in (e.location and f"location: {e.location}", e.raw, e.message) if t]
            names: list[str] = []
            for t in texts:
                for rx in self._LOC_TYPE_RES:
                    names += [re.sub(r"<.*", "", m.group(1)).strip(".") for m in rx.finditer(t)]
            if not names:
                continue
            imports: dict[str, str] = {}
            own_pkg = ""
            if e.file and ws.exists(e.file):
                try:
                    src = ws.read(e.file)
                    imports = {imp.rsplit(".", 1)[-1]: imp for imp in self._IMPORT_LINE_RE.findall(src)}
                    pm = self._PACKAGE_LINE_RE.search(src)
                    own_pkg = pm.group(1) if pm else ""
                except OSError:
                    pass
            for n in dict.fromkeys(names):
                for fq in self._candidate_fqcns(n, imports, own_pkg):
                    f = self._project_source(java_files, fq)
                    if f and f not in out and f.replace("\\", "/") not in skip:
                        out.append(f)
                        break
                if len(out) >= self.MAX_RELATED_FILES:
                    return out
        return out

    @staticmethod
    def _candidate_fqcns(name: str, imports: dict[str, str], own_pkg: str) -> list[str]:
        """타입 이름에 대한 후보 FQCN 목록을 만든다.
        이미 FQCN이면 그대로 쓰고, 아니면 import와 같은 패키지에서 찾는다."""
        if "." in name and name.split(".")[0][:1].islower():          # 이미 FQCN이다
            return [name]
        simple = name.split(".")[0]
        cands = []
        if simple in imports:
            cands.append(imports[simple])
        if own_pkg:
            cands.append(f"{own_pkg}.{simple}")
        return cands

    @staticmethod
    def _project_source(java_files: list[str], fqcn: str) -> Optional[str]:
        """FQCN에 맞는 프로젝트 소스 파일을 찾는다.
        JDK, jakarta, Spring, Hibernate, lombok 타입이면 None을 반환한다. 파일이 없어도 None이다."""
        if fqcn.startswith(("java.", "javax.", "jakarta.", "org.springframework.", "org.hibernate.", "lombok.")):
            return None
        parts = fqcn.split(".")
        for i in range(len(parts), 1, -1):                 # Outer.Inner는 Outer.java에서 찾는다
            rel = "/".join(parts[:i]) + ".java"
            if parts[i - 1][:1].islower():
                break
            for f in java_files:
                g = f.replace("\\", "/")
                if g == rel or g.endswith("/" + rel):
                    return f
        return None

    @staticmethod
    def _reference_block(label: str, text: str) -> str:
        body = text.replace("\r\n", "\n").rstrip("\n")
        fence = fence_for(body)
        return f"// reference copy (read-only, not in the build): {label}\n{fence}{lang_for(label)}\n{body}\n{fence}"

    @staticmethod
    def _wants_pom(cluster: ErrorCluster, files: list[str]) -> bool:
        """POM 섹션이 필요한 케이스인지 확인한다.
        의존성 해석이나 pom 관련 오류, 소스 파일 없는 컴파일 실패, 의존성 문제로 보이는 메시지면 필요하다."""
        if cluster.kind in POM_KINDS:
            return True
        if any(PurePosixPath(f).name == "pom.xml" for f in files):
            return True
        if cluster.kind == ErrorKind.COMPILE and any(not e.file for e in cluster.errors):
            return True                  # 소스 파일 없는 컴파일 실패는 컴파일러나 플러그인 설정 문제다
        return any(POM_MESSAGE_RE.search(e.message or "") for e in cluster.errors)

    def _file_block(self, ws: Workspace, path: str, err_lines: list[int]) -> tuple[str, str]:
        """관련 타입 파일을 보여 줄 파일 블록과 import 줄을 만든다.
        `WHOLE_FILE_MAX_LINES`줄 이하면 통째로 보여 준다. 더 길면 import 블록과 오류 줄 주변만 보여 준다."""
        if not ws.exists(path):
            return f"// {path}: (file not found in workspace)", ""
        text = ws.read(path).replace("\r\n", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        imports = "\n".join(l for l in lines if l.startswith("import ") or l.startswith("package ")) + "\n"
        lang = lang_for(path)
        if len(lines) <= WHOLE_FILE_MAX_LINES:
            body = "\n".join(lines)
            fence = fence_for(body)
            return f"// {path} lines 1-{len(lines)} (whole file)\n{fence}{lang}\n{body}\n{fence}", imports
        blocks = []
        # 범위에 import 블록이 없으면 앞에 붙인다
        spans = merge_windows(err_lines, len(lines))
        last_import = max((i + 1 for i, l in enumerate(lines) if l.startswith("import ")), default=0)
        if last_import and spans[0][0] > 1:
            spans.insert(0, (1, min(last_import, spans[0][0] - 1)))
        for a, b in spans:
            body = "\n".join(lines[a - 1:b])
            fence = fence_for(body)
            blocks.append(f"// {path} lines {a}-{b} (of {len(lines)})\n{fence}{lang}\n{body}\n{fence}")
        return "\n".join(blocks), imports

    def _fit(self, secs: list[_Section], budget: int, packed: PackedContext,
             order: Optional[tuple[str, ...]] = None) -> None:
        """문맥이 예산을 넘으면 `order` 순서로 절을 줄인다. 200자 미만이 되는 절은 뺀다.
        노트는 순위가 낮은 것부터 빼고, `truncatable=False`인 절은 건드리지 않는다.
        줄인 내용은 `packed.truncated`와 INFO 로그에 남긴다."""
        def total() -> int:
            return sum(len(s.title) + len(s.text) + 6 for s in secs)

        over = total() - budget
        if over <= 0:
            return
        for key in order or SCRIPT_TRUNCATE_ORDER:
            if over <= 0:
                break
            group = [s for s in secs if s.key == key and s.truncatable]
            # 노트는 순위가 낮은 것부터 뺀다
            if key == "notes":
                for s in sorted(group, key=lambda x: -x.order):
                    if over <= 0:
                        break
                    if s.always and over < len(s.text) - 400:
                        # always 노트는 빼지 않고 줄여서 남긴다
                        keep = len(s.text) - over
                        s.text = _truncate(s.text, keep)
                        packed.truncated.append(f"notes:{s.title} (shortened)")
                    else:
                        secs.remove(s)
                        packed.truncated.append(f"notes:{s.title} (dropped)")
                    over = total() - budget
                continue
            for s in group:
                if over <= 0:
                    break
                keep = len(s.text) - over
                if keep < 200:
                    secs.remove(s)
                    packed.truncated.append(f"{key} (dropped)")
                else:
                    s.text = _truncate(s.text, keep)
                    packed.truncated.append(f"{key} (truncated)")
                over = total() - budget
        if packed.truncated:
            log.info("context over budget (%d chars): %s", budget, ", ".join(packed.truncated))
