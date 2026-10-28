"""수정 스크립트 요청의 프롬프트 문맥 꾸리기(ContextPacker): 케이스 하나(L2 서명)의 오류들, 오류 파일 발췌,
pom 발췌, 의존성 버전 사실, 권고, 마이그레이션 노트, 이전 시도 — 글자 예산(`llm.max_context_chars`, 기본
60,000) 안에서. 흐름상 위치: LLMFixer.propose_script 가 LLM 을 부르기 직전에 `pack_script(ws, cluster, ctx,
ctx.profile_summary)` 를 부른다. 사내 fixer 플러그인도 같은 문맥을 재사용할 수 있다(`PackedContext.render()`).
예산 배분: 머리말·오류 목록·보강할 스크립트는 줄이지 않는다. 오류 파일에는 import 블록 + 오류 줄 주변 창(작은
파일은 통째로)을 주고, 작은 파일이 남긴 몫은 큰 파일이 쓴다. 예산을 넘는 파일은 이름만 적는다(스크립트는
`p.errors` / `p.files` 로 닿는다). 넘치면 SCRIPT_TRUNCATE_ORDER 순서로 줄이거나 뺀다. 지킬 것: 같은 입력이면
같은 프롬프트(결정적). 머리말의 `case: <case_id>` 줄은 재생 픽스처의 `match: "case: <id>"` 가 찾는 자리라
형식을 바꾸지 말 것. 참고 파일(모듈 병합에서 빠진 사본)은 읽기 전용 — 빌드에 없고 스크립트가 쓰면 안 된다.
v2.1d `library_hints` → "Dependency versions" 섹션(사람이 넣은 `libraries.yml` 항목 + 저장소에 있는 버전;
정책은 스크립트 프롬프트 규칙 14), pom 바로 뒤에 두고 늦게 버린다.

Context packing for the LLM fixer: errors, file windows, pom excerpts, library migration guidance (advisories),
migration notes, history — within a budget.

`ContextPacker.pack_script` packs ONE case (L2 signature) that may span many files for a fix-script request — the
budget is spread over the erroring files (import block + windows around the error lines, whole file when small),
files beyond it are listed by name only (the script reaches them through `p.errors` / `p.files`).

Round 9: module-merge notes (`FixContext.reference_notes`) and the dropped first-wins copies as read-only reference text
(`FixContext.reference_files`): never part of the build, never written by a script.

v2.1d: `FixContext.library_hints` → the section "Dependency versions" (the people's table entry and what the repository
has for the libraries behind the case; the script prompt's rule 14 is the policy), right after the poms, dropped late."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

from sbmig.core.config import Config
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
# notes index
# ---------------------------------------------------------------------------
@dataclass
class NoteEntry:
    """마이그레이션 노트 하나(`knowledge/notes/index.yml` 항목 또는 .md 앞머리): 키워드 정규식으로 오류 문장에
    점수를 매겨 고른다."""
    file: str
    title: str
    keywords: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    always: bool = False
    path: Optional[Path] = None
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def compiled(self) -> list[re.Pattern]:
        """키워드 정규식들(잘못된 정규식은 글자 그대로 찾도록 escape 해서)."""
        if not self._compiled and self.keywords:
            for k in self.keywords:
                try:
                    self._compiled.append(re.compile(k))
                except re.error:
                    self._compiled.append(re.compile(re.escape(k)))
        return self._compiled

    def score(self, text: str) -> int:
        """`text` 에서 맞는 키워드 수."""
        return sum(1 for rx in self.compiled() if rx.search(text))

    def applies(self, case: Optional[str], phase: Optional[str]) -> bool:
        """이 케이스 종류·단계에 쓸 수 있는가. `cases` 는 제한이고 `phases` 는 참고일 뿐이다: v2 는 모든
        오류를 `verify` 스크립트 루프 하나에서 고치므로 verify 는 언제나 허용.

        `cases` restricts the note; `phases` is advisory (the v1 phase the note was written for): v2 fixes every
        error — Java 21, jakarta, Spring, restructure — in the one `verify` script loop, so `verify` is always
        allowed; another phase is still narrowed by the list."""
        if self.cases and case and case not in self.cases:
            return False
        if self.phases and phase and phase != "verify" and phase not in self.phases:
            return False
        return True

    def body(self) -> str:
        """노트 본문(앞머리 제외; 파일이 없으면 "")."""
        if not self.path or not self.path.exists():
            return ""
        return strip_front_matter(self.path.read_text(encoding="utf-8")).strip()


def split_front_matter(text: str) -> tuple[dict, str]:
    """`---` YAML 앞머리와 본문을 나눈다(앞머리가 없거나 깨진 YAML 이면 빈 dict)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def strip_front_matter(text: str) -> str:
    """앞머리를 뺀 본문."""
    return split_front_matter(text)[1]


def load_notes_index(notes_dir: str | Path) -> list[NoteEntry]:
    """노트 목록: `index.yml` 을 읽고, 없으면 디렉토리의 모든 *.md 앞머리에서 만든다.

    Read `index.yml` (falls back to front-matter of every *.md when the index is missing)."""
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
                path=p,
            ))
    elif d.is_dir():
        for p in sorted(d.glob("*.md")):
            meta = split_front_matter(p.read_text(encoding="utf-8"))[0]
            entries.append(NoteEntry(file=p.name, title=meta.get("title", p.stem),
                                     keywords=list(meta.get("keywords") or []), cases=list(meta.get("cases") or []),
                                     phases=list(meta.get("phases") or []), always=bool(meta.get("always", False)),
                                     path=p))
    return entries


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fence_for(text: str) -> str:
    """본문에 ``` 가 있으면 ```` 로 감싼다(프롬프트 속 코드 펜스가 중간에 닫히지 않게)."""
    return "````" if "```" in text else "```"


def lang_for(path: str) -> str:
    """확장자 → 코드 펜스 언어 이름(모르면 "")."""
    return _LANG.get(PurePosixPath(path).suffix.lower(), "")


def merge_windows(lines: list[int], total: int, radius: int = WINDOW_RADIUS) -> list[tuple[int, int]]:
    """각 줄 ±radius 의 창(1부터, 양끝 포함), 겹치거나 붙으면 합친다. 줄 번호가 없으면 파일 앞부분.

    1-based inclusive windows of ±radius around each line, merged when overlapping or adjacent."""
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
    """스크립트 시도가 아닌 이전 시도(옛 기록)의 요약: 결과, 분석, 바꾼 파일.

    An earlier attempt that is not a script attempt (old records): outcome, analysis, the files it changed."""
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
# v2 fix scripts: helpers (one case across many files)
# ---------------------------------------------------------------------------
SCRIPT_MAX_ERROR_LINES = 80          # error lines in the prompt (all of them are in p.errors) …
SCRIPT_ERRORS_SHARE = 0.30           # … within this share of the budget
SCRIPT_INDEX_SHARE = 0.15            # the "Files with errors" index within this share of the budget
SCRIPT_MAX_INDEX_FILES = 200         # files named in the "Files with errors" index
SCRIPT_MAX_FILES_SHOWN = 40          # files whose text is shown at most …
SCRIPT_MIN_FILE_SHARE = 1500         # … and at least this many chars per shown file (fewer files, readable windows)
SCRIPT_WINDOW_RADII = (60, 25, 10, 3, 0)   # windows around the error lines, widest first
SCRIPT_OPTIONAL_RESERVE = 0.35       # share of the budget kept for hints / notes / poms before the files take the rest
SCRIPT_MAX_POMS = 4
SCRIPT_BASE_MAX_LINES = 60           # log / remaining / introduced lines of an augment base
SCRIPT_TRUNCATE_ORDER = ("notes", "plan_notes", "other_errors", "previous_attempts", "kb_hints", "reference_files",
                         "advisory_hints", "reference_notes", "library_hints", "related_files", "files", "pom",
                         "base_result")


def format_error_line(e: BuildError, max_message: int = 300) -> str:
    """스크립트 프롬프트용 오류 한 줄(메시지는 `max_message` 자까지).

    One compact line per error for a script prompt: `- file:line:col: message | symbol: … | location: …`."""
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
    """케이스의 오류를 한 번씩만(리액터 빌드는 같은 오류를 되풀이한다), 파일·줄·열·메시지 순으로.

    The case's errors once each (reactor builds repeat them), sorted by file / line / column / message."""
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
    """이 케이스의 이전 시도 요약(스크립트 시도면 모드·버전·실행
    상태·만든 새 오류) — 프롬프트의 "되풀이하지 말 것" 섹션이 된다.

    An earlier attempt on the case for a script prompt (script attempts: mode, version, run status)."""
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
        lines.append(f"    analysis: {a.analysis.strip()[:300]}")
    for ie in a.introduced_errors[:5]:
        lines.append(f"    introduced: {format_error_line(ie, 200)[2:]}")
    return "\n".join(lines)


def _capped(items: list[str], limit: int, what: str = "line(s)") -> list[str]:
    out = list(items[:limit])
    if len(items) > limit:
        out.append(f"… {len(items) - limit} more {what}")
    return out


def script_base_result(base: ScriptBase) -> str:
    """보강할 스크립트가 이 프로젝트에서 한 일(보강 프롬프트): 결과, 실행 상태·오류, 바꾼 파일, 로그 끝
    SCRIPT_BASE_MAX_LINES 줄, 아직 남은 이 케이스의 오류, 새로 만든 오류, 이전 버전들.

    What the script to extend did in this project (augment prompt)."""
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
    """pom 자신의 artifactId(parent 의 것이 아님); 못 읽으면 None.

    The pom's own artifactId (not its parent's); None when unreadable."""
    try:
        text = _PARENT_BLOCK_RE.sub("", _COMMENT_RE.sub("", ws.read(pom)))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    m = _ARTIFACT_ID_RE.search(text)
    return m.group(1) if m else None


def nearest_pom(ws: Workspace, rel: Optional[str]) -> Optional[str]:
    """파일이 속한 모듈 pom: 파일 디렉토리에서 루트까지 올라가며 찾는다(최대 128단계). 역슬래시는 정규화, 절대
    경로는 워크스페이스 안이면 상대 경로로 바꾸고 밖이면 버린다 — 예전에 절대 경로가 무한 루프를 돌아 Windows
    에서 "fixer.propose 에서 멈춤" 으로 보였다(round 9c).

    The module pom of a file: walk up from the file's directory to the project root. Root-relative posix paths
    only; a path with backslashes is normalized, an absolute one is made root-relative when it lies under the
    workspace and dropped otherwise (round 9c: `PurePosixPath("/").parent` is `/`, so an absolute path made the old
    `while True` loop spin forever — seen as a run "stuck in fixer.propose" on Windows)."""
    if not rel:
        return "pom.xml" if ws.exists("pom.xml") else None
    norm = str(rel).replace("\\", "/")
    p = PurePosixPath(norm)
    if p.is_absolute() or (len(norm) > 1 and norm[1] == ":"):          # posix-absolute or a Windows drive path
        try:
            norm = Path(norm).resolve().relative_to(Path(ws.root).resolve()).as_posix()
        except (ValueError, OSError):
            return "pom.xml" if ws.exists("pom.xml") else None
        p = PurePosixPath(norm)
    if p.name == "pom.xml" and ws.exists(norm):
        return norm
    cur = p.parent
    for _ in range(128):                                               # never spin: depth-bounded walk to the root
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
    """pom 발췌: POM_WHOLE_MAX_LINES 줄 이하면 통째로, 아니면 parent / properties / dependencyManagement /
    dependencies / build 섹션만(줄 번호와 함께, 파일 순서대로)."""
    lines = text.split("\n")
    if len(lines) <= POM_WHOLE_MAX_LINES:
        return f"// {path} (whole file)\n```xml\n{text.rstrip()}\n```"
    out = []
    taken: list[tuple[int, int]] = []
    for tag in _POM_SECTIONS:
        for m in re.finditer(rf"<{tag}>.*?</{tag}>", text, re.DOTALL):
            a, b = m.start(), m.end()
            if any(x <= a and b <= y for x, y in taken):
                continue                                 # nested (e.g. <dependencies> inside depMgmt)
            # expand to whole lines
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
# packer
# ---------------------------------------------------------------------------
@dataclass
class PackedContext:
    """꾸려진 문맥: (제목, 본문) 섹션 목록(프롬프트 순서), 보여 준 파일, 이름만 적은 파일, 넣은 노트, 줄이거나
    뺀 섹션, 글자 수."""
    sections: list[tuple[str, str]] = field(default_factory=list)
    files_included: list[str] = field(default_factory=list)
    notes_included: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    chars: int = 0
    files_listed: list[str] = field(default_factory=list)   # v2 scripts: erroring files named but not shown

    def render(self) -> str:
        """섹션들을 `## 제목` + 본문의 markdown 한 덩어리로."""
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
    """수정 스크립트 프롬프트의 문맥을 예산 안에서 꾸린다(`pack_script`).
    노트는 `knowledge/notes` 에서 처음 쓸 때 읽는다."""
    # 줄이는 순서는 SCRIPT_TRUNCATE_ORDER(앞이 먼저 줄어든다); "errors" 와 "header" 는 절대 줄이지 않는다
    # truncation priority: SCRIPT_TRUNCATE_ORDER (first = dropped/shrunk first); "errors" and "header" never shrink
    MAX_RELATED_FILES = 3
    ADVISORY_TITLE = "Library migration guidance (what to migrate TO)"
    # v2.1d: FixContext.library_hints — the people's table entry + what the repository has (sbmig.build.repo_versions)
    LIBRARY_HINTS_TITLE = "Dependency versions (what the repository has; policy: same major first, see rule 14)"
    REFERENCE_NOTES_TITLE = "Module merge notes (first-wins; the dropped copy is below as reference)"

    def __init__(self, cfg: Config, notes_dir: Optional[str | Path] = None, max_notes: int = DEFAULT_MAX_NOTES):
        self.cfg = cfg
        self.notes_dir = Path(notes_dir) if notes_dir else cfg.resolve_path("knowledge/notes")
        self.max_notes = max_notes
        self._notes: Optional[list[NoteEntry]] = None

    @property
    def notes(self) -> list[NoteEntry]:
        """노트 목록(지연 로드). 인덱스가 깨져도 수정은 계속한다 — 빈 목록 + 경고."""
        if self._notes is None:
            try:
                self._notes = load_notes_index(self.notes_dir)
            except Exception as exc:  # broken index must never break fixing
                log.warning("cannot load notes index from %s: %s", self.notes_dir, exc)
                self._notes = []
        return self._notes

    # ------------------------------------------------------------------ public
    def select_notes(self, text: str, case: Optional[str], phase: Optional[str]) -> list[NoteEntry]:
        """`text`(오류 문장·import 줄)로 점수를 매긴 노트 상위 `max_notes` 개, always 노트는 앞에(pack_script
        는 always 노트를 뺀다)."""
        scored = []
        always = []
        for idx, n in enumerate(self.notes):
            if n.always:
                always.append(n)
                continue
            if not n.applies(case, phase):
                continue
            s = n.score(text)
            if s > 0:
                scored.append((-s, idx, n))
        scored.sort(key=lambda t: (t[0], t[1]))
        top = [n for _, _, n in scored[: self.max_notes]]
        return always + top            # always-notes first (rendered first, dropped last)

    # ------------------------------------------------------------------ v2 fix scripts
    SCRIPT_FILES_TITLE = "Source (exact text around the error lines; line numbers as in p.errors)"
    SCRIPT_INDEX_TITLE = "Files with errors"
    SCRIPT_REFERENCE_FILES_TITLE = ("Reference files (read-only, NOT in the build — the script must never write "
                                    "these paths)")
    SCRIPT_RELATED_FILES_TITLE = ("Related project types named in the errors (exact text — the script edits these when "
                                  "the fix belongs there, e.g. a repository interface that lost its CRUD methods)")

    def pack_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, profile_summary: str = "",
                    *, case_type: Optional[str] = None, budget: Optional[int] = None) -> PackedContext:
        """수정 스크립트 요청의 문맥: 여러 파일에 걸칠 수 있는 케이스(L2) 하나. 섹션(순서 값): Case 머리말(0)
        → 오류 목록(1) → 보강이면 보강할 스크립트(1.5)와 그 결과(1.6) → 오류 파일 색인(1.8) → 소스 발췌(2) →
        관련 타입 파일(2.2) → 참고 파일(2.5) → POM(3.x) → Dependency versions(3.5) → 권고(4) → 병합 메모(4.5)
        → KB 힌트(5) → 이전 시도(6) → 다른 케이스(7) → 계획 메모(8) → 노트(10+). 같은 입력이면 같은 결과.

        Context for a fix-script request: ONE case (L2) that may span many files. `llm.max_context_chars` is
        spread over the erroring files (import block + windows around the error lines, the whole file when small;
        small files first, their surplus goes to the big ones); files beyond the budget are listed by name only.
        Poms of the modules involved when the case is a resolution / pom error. Deterministic for the same input."""
        budget = budget or self.cfg.llm.max_context_chars
        summary = profile_summary or ctx.profile_summary
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

        # 머리말 + 오류 목록은 절대 줄이지 않는다. `case: <id>` 줄 형식을 바꾸지 말 것 — 재생 픽스처의 `match:
        # "case: <case_id>"` 가 이 줄로 답을 고른다
        # header + errors (never truncated)
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
            header.append(f"project: {summary}")
        header.append(f"phase: {ctx.phase.value}, iteration {ctx.iteration}"
                      + (f", stall level {ctx.stall_level}" if ctx.stall_level else ""))
        secs.append(_Section("header", "Case", "\n".join(header), 0, truncatable=False))
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
            err_lines.append(f"… {len(errors) - len(err_lines)} more error(s) — the script gets all of them in "
                             "p.errors")
        secs.append(_Section("errors", f"Errors of this case ({len(errors)})", "\n".join(err_lines), 1,
                             truncatable=False))

        # 보강: 고칠 스크립트 본문은 줄이지 않는다 — 보강 답은 그것을 대체하는 완전한 새 스크립트여야 한다
        # augment: the script to extend (never truncated) and what it did here
        if mode == "augment" and base is not None:
            label = f"version {base.version}" if base.version else "this run's script, not stored"
            code = base.code.replace("\r\n", "\n").rstrip("\n")
            fence = fence_for(code)
            secs.append(_Section("base_code", f"Script to extend ({label}) — answer with the COMPLETE new script",
                                 f"{fence}python\n{code}\n{fence}", 1.5, truncatable=False))
            secs.append(_Section("base_result", f"What {label} did in this project", script_base_result(base), 1.6))

        # poms of the modules involved (resolution / pom cases)
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
        if ctx.library_hints:                          # right after the poms (3.x), before the advisories (4)
            secs.append(_Section("library_hints", self.LIBRARY_HINTS_TITLE,
                                 "\n".join(f"- {h}" for h in ctx.library_hints), 3.5))
        if ctx.advisory_hints:
            secs.append(_Section("advisory_hints", self.ADVISORY_TITLE,
                                 "The libraries below were removed or replaced by the migration on purpose. The script "
                                 "rewrites the code that still uses them to the named replacement (public signatures "
                                 "kept so callers still compile); it never re-adds the removed library.\n"
                                 + "\n".join(f"- {h}" for h in ctx.advisory_hints), 4))
        if ctx.reference_notes:
            secs.append(_Section("reference_notes", self.REFERENCE_NOTES_TITLE,
                                 "\n".join(f"- {n}" for n in ctx.reference_notes), 4.5))
        if ctx.kb_hints:
            secs.append(_Section("kb_hints", "Knowledge-base hints (similar cases)",
                                 "\n".join(f"- {h}" for h in ctx.kb_hints), 5))
        if ctx.previous_attempts:
            secs.append(_Section("previous_attempts",
                                 "Previous attempts on this case (they did NOT resolve it — do not repeat them)",
                                 "\n".join(summarize_script_attempt(a) for a in ctx.previous_attempts), 6))
        if ctx.other_errors_summary:
            secs.append(_Section("other_errors", "Other error cases in this build (for awareness; fix only this case)",
                                 ctx.other_errors_summary.strip(), 7))
        if ctx.plan_notes:
            secs.append(_Section("plan_notes", "Plan notes", "\n".join(f"- {n}" for n in ctx.plan_notes), 8))

        # the erroring files, sized to what the other sections leave
        renderings, import_text = self._script_renderings(ws, by_file)
        match_text = "\n".join(
            [e.message for e in errors] + [e.symbol or "" for e in errors] + [e.location or "" for e in errors]
            + [e.raw[:2000] for e in errors[:40]] + [e.artifact.coordinates for e in errors if e.artifact]
            + [import_text])
        # always-notes (the old fix policy note: "fix tests so they compile" …) are left out — the script system
        # prompt carries the fix policy; keyword-selected technical notes stay
        notes = [n for n in self.select_notes(match_text, case_type, ctx.phase.value) if not n.always]
        for i, n in enumerate(notes):
            body = n.body()
            if body:
                secs.append(_Section("notes", f"Migration note: {n.title}", body, 10 + i, always=n.always))

        def size(s: _Section) -> int:
            return len(s.title) + len(s.text) + 6

        # 예산 계산: 줄일 수 없는 섹션(+ base_result, pom)을 먼저 빼고, 선택 섹션 몫(남는 것의 최대 35%)을
        # 남긴 나머지를 오류 파일이 쓴다(최소 SCRIPT_MIN_FILE_SHARE 자)
        fixed = sum(size(s) for s in secs if not s.truncatable or s.key in ("base_result", "pom"))
        optional = sum(size(s) for s in secs if s.truncatable and s.key not in ("base_result", "pom"))
        index_cap = max(1500, int(budget * SCRIPT_INDEX_SHARE))
        index_estimate = len(self._file_index(by_file, counts, set(), set(), index_cap)) + 40 if by_file else 0
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
                                 self._file_index(by_file, counts, set(chosen), set(missing), index_cap), 1.8,
                                 truncatable=False))
        if shown:
            secs.append(_Section("files", self.SCRIPT_FILES_TITLE, "\n\n".join(chosen[f] for f in shown), 2))

        self._fit(secs, budget, packed, order=SCRIPT_TRUNCATE_ORDER)
        secs.sort(key=lambda s: s.order)
        packed.sections = [(s.title, s.text) for s in secs]
        packed.notes_included = [s.title.removeprefix("Migration note: ") for s in secs if s.key == "notes"]
        packed.chars = sum(len(t) + len(b) + 4 for t, b in packed.sections)
        return packed

    def _script_poms(self, ws: Workspace, cluster: ErrorCluster, files: list[str]) -> list[str]:
        """케이스가 닿는 모듈의 pom 들(최대 SCRIPT_MAX_POMS): 오류 난 pom, 오류 파일마다 그 모듈 pom, 파일
        없는 오류가 이름 댄 모듈의 pom(디렉토리 이름, 다음 artifactId), 해석 오류면 루트 pom.

        The poms of the modules a case touches: erroring poms, the module pom of every erroring file, the pom of a
        module named by a file-less error (directory name), and the root pom for resolution errors."""
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
                # Maven names the module by its artifactId; the folder often differs (`module/comp-jar` holds
                # `<name>-comp`, company `{name}-back` holds `<name>-comp`): the folder name first, then the poms'
                # own artifactIds
                add(next((p for p in poms if PurePosixPath(p).parent.name == m), None)
                    or next((p for p in poms if pom_artifact_id(ws, p) == m), None))
        if ws.exists("pom.xml") and (not out or cluster.kind in POM_KINDS):
            add("pom.xml")
        return out[:SCRIPT_MAX_POMS]

    def _script_renderings(self, ws: Workspace, by_file: dict[str, list[int]]) -> tuple[dict[str, list[str]], str]:
        """오류 파일마다(pom 제외 — POM 섹션이 따로 있다) 보여 줄 수 있는 모양들을 자세한 순으로: 작으면 통째
        → import 블록 + 점점 좁은 창 → import 없이 창. 그리고 노트 고르기에 쓸 모든 import 줄.

        Per erroring file (poms excluded — they get a POM section): its possible renderings, most informative
        first (whole file when small, then import block + ever narrower windows around the error lines, then windows
        without the import block), and the import lines of all files (note selection)."""
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
        """물 채우기(water-filling) 배분: 오류 많은 순으로 최대 SCRIPT_MAX_FILES_SHOWN 개, 파일당 최소
        SCRIPT_MIN_FILE_SHARE 자. 짧은 파일부터 몫을 받고 남긴 몫은 큰 파일로 간다(각자 몫에 들어가는 가장
        자세한 모양). 들어갈 모양이 없으면 이름만 적힌다.

        Water-filling: at most SCRIPT_MAX_FILES_SHOWN files (most errors first, then path), each at least
        SCRIPT_MIN_FILE_SHARE chars; files that need little take it and leave the rest to the big ones (each takes
        its most informative rendering that fits its share); a file with no rendering that fits is listed by name
        only."""
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
        """오류 파일 색인: 파일마다 오류 수와 줄 번호(보여 준 파일 먼저, 다음은 경로 순). 안 보여 준 파일은
        "not shown" — 스크립트는 `p.errors` / `p.files` 로 닿는다.

        Every erroring file with its error count and lines (shown files first, then by path), within `max_chars`
        and SCRIPT_MAX_INDEX_FILES entries."""
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
        """1-based (first package/import line, last import line) of a Java file, or None."""
        first = next((i for i, ln in enumerate(lines) if ln.startswith(("package ", "import "))), None)
        if first is None:
            return None
        last = max((i for i, ln in enumerate(lines) if ln.startswith("import ")), default=first)
        return first + 1, max(first, last) + 1

    @staticmethod
    def _spans(imp: Optional[tuple[int, int]], err_lines: list[int], radius: int, total: int) -> list[tuple[int, int]]:
        """import 블록 + 오류 줄 ±radius 창(간격이 3줄 이하면 합침). 줄 번호가 없거나 낡았으면 파일 앞부분."""
        if total <= 0:
            return []
        spans: list[tuple[int, int]] = [imp] if imp else []
        lines = sorted({ln for ln in err_lines if 0 < ln <= total})
        if lines:
            spans += [(max(1, ln - radius), min(total, ln + radius)) for ln in lines]
        else:                                            # no line numbers (or stale ones): the head of the file
            spans.append((1, min(total, max(2 * radius, 20))))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 4:          # a gap of ≤ 3 lines costs less than another header
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    @staticmethod
    def _render_spans(path: str, lines: list[str], spans: list[tuple[int, int]]) -> str:
        """창들을 `// path lines a-b (of N)` 머리줄 + 코드 펜스로(줄 번호는 p.errors 와 같다)."""
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

    # ------------------------------------------------------------------ internals
    _LOC_TYPE_RES = (re.compile(r"location:\s*variable\s+[\w$]+\s+of\s+type\s+([\w$.]+)"),
                     re.compile(r"location:\s*(?:class|interface|enum|record)\s+([\w$.]+)"),
                     re.compile(r"(?:method|constructor)\s+([\w$.]+)\.[\w$]+\([^)]*\)\s+is not applicable"))
    _IMPORT_LINE_RE = re.compile(r"^[ \t]*import\s+(?!static\b)([\w.]+)\s*;", re.M)
    _PACKAGE_LINE_RE = re.compile(r"^[ \t]*package\s+([\w.]+)\s*;", re.M)

    def _related_type_files(self, ws: Workspace, cluster: ErrorCluster, cluster_files: list[str]) -> list[str]:
        """오류가 이름 댄 프로젝트 타입의 소스 파일(예: CRUD 메서드를 잃은 repository 인터페이스): FQCN 또는
        오류 파일의 import / package 로 찾고, 케이스 파일
        자신과 라이브러리 타입은 빼고 최대 MAX_RELATED_FILES 개.

        Project source files of the types the errors name (`location: variable x of type T`, `location: class T`,
        `method T.m(…) is not applicable`), resolved by FQCN or through the error file's imports / package; never the
        cluster files themselves, never library types, at most MAX_RELATED_FILES."""
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
        """타입 이름의 후보 FQCN: 이미 FQCN 이면 그대로, 아니면 import 와 같은 패키지에서."""
        if "." in name and name.split(".")[0][:1].islower():          # already fully qualified
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
        """FQCN 의 프로젝트 소스 파일(Outer.Inner → Outer.java);
        JDK·jakarta·Spring·Hibernate·lombok 타입은 None."""
        if fqcn.startswith(("java.", "javax.", "jakarta.", "org.springframework.", "org.hibernate.", "lombok.")):
            return None
        parts = fqcn.split(".")
        for i in range(len(parts), 1, -1):                 # Outer.Inner → Outer.java
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
        """POM 섹션이 필요한 케이스인가: 해석·pom 계열 오류, pom 파일 오류, 소스 파일 없는 컴파일
        실패(컴파일러·플러그인 설정), "package … does not exist" 처럼 의존성 문제로 보이는 메시지."""
        if cluster.kind in POM_KINDS:
            return True
        if any(PurePosixPath(f).name == "pom.xml" for f in files):
            return True
        if cluster.kind == ErrorKind.COMPILE and any(not e.file for e in cluster.errors):
            return True                  # a compile failure without a source file = compiler / plugin configuration
        return any(POM_MESSAGE_RE.search(e.message or "") for e in cluster.errors)

    def _file_block(self, ws: Workspace, path: str, err_lines: list[int]) -> tuple[str, str]:
        """(파일 블록, import 줄): WHOLE_FILE_MAX_LINES 줄 이하면 통째로, 아니면 import 블록 + 오류 줄 창.
        관련 타입 파일에 쓴다."""
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
        # always show the import block when the window does not include it
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
        """예산을 넘으면 `order` 순서로 섹션을 줄이고(200자 미만이 되면 뺀다), 노트는 순위 낮은 것부터 뺀다.
        줄일 수 없는 섹션(truncatable=False)은 건드리지 않는다.
        줄인 것은 `packed.truncated` 와 INFO 로그에 남는다."""
        def total() -> int:
            return sum(len(s.title) + len(s.text) + 6 for s in secs)

        over = total() - budget
        if over <= 0:
            return
        for key in order or SCRIPT_TRUNCATE_ORDER:
            if over <= 0:
                break
            group = [s for s in secs if s.key == key and s.truncatable]
            # notes: drop lowest-ranked first (they are in rank order; always-notes come last)
            if key == "notes":
                for s in sorted(group, key=lambda x: -x.order):
                    if over <= 0:
                        break
                    if s.always and over < len(s.text) - 400:
                        # keep a shortened always-note (fix policy) rather than dropping it
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
