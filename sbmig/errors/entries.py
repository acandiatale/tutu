"""실행 하나에서 나온 오류 목록을 관리하는 모듈. ``<실행 디렉토리>/errors.jsonl``과 ``errors.md``를 쓴다.
오케스트레이터와 수정 루프가 빌드 오류, pom 보정, 구조 검토 메모, 수정 시도, 차단을 기록하고, 실행이 끝나면
``CaseStore.record_run``이 이 목록을 KB로 옮긴다.
- 빌드 결과에 오류가 안 보인다고 해결된 것은 아니다. 그 빌드가 그 오류를 보고할 수 있었을 때만 해결로 본다
- 텍스트는 마스킹하고 경로를 지운 뒤 저장한다
- 파일은 바뀔 때마다 통째로 다시 쓰고, 쓰기에 실패해도 실행은 계속한다

The error list of one run (v2.1, docs/v2/PLAN-v2.1.md §3.3): ``<run dir>/errors.jsonl`` + ``errors.md``.

One :class:`~sbmig.core.models.ErrorEntry` per build error (or restructure pom correction, or review note), in memory
and rewritten whole on every change (runs are sequential — no append-only machinery here; the KB's
``<kb>/errors.jsonl`` gets the finished list at the end of the run, :meth:`sbmig.errors.cases.CaseStore.record_run`).

Lifecycle (the orchestrator / fix loop call these):

    add_build(build, "compile")        the first `mvn compile` after OpenRewrite → every error, status open
    add_pom_verify(pom, note)          a restructure pom correction → status pom-verify (seen restructure)
    add_note(file, text)               a review finding → status note (seen review)
    add_build(build, "build")          `mvn clean package` → only NEW errors are added; open entries no longer
                                       reported are resolved ("build"); verify_poms(build) checks the pom-verify ones
    begin_try(case, round, source)     a fix try on every open entry of the case → FixTry n (1, 2, … per case)
    finish_try(case, n, …)             what the try did: analysis / solution / edits or script / 수정 notes
                                       (``edits``: EditNote per change, v2.1b) / diff / result
    add_build(build, "fix r3")         the build that scores round 3: absent entries whose last try is this round's
                                       (still pending) stay open for the loop's verdict …
    resolve(case, 3, n)                … which resolves the entries gone from that build ("r3 try n")
    block(case, reason) / end_of_run() blocked; open → unresolved

Matching a reported error to an entry: same (case, file, line, message) first; then same (case, file, message) at
the nearest line (an edit above the error moved it — the entry keeps its id and takes the new line); then, for
entries seen at ``compile`` (before the restructure moved the sources), the same (case, message) in a file whose
path ends with the entry's path or shares its ``src/…`` tail (``src/main/java/A.java`` → ``demo-back/src/main/java/
A.java``) — the entry takes the new file / module. An entry reported again after it was resolved is reopened (note
``reappeared in …``). Synthetic stand-ins (``mvn exited with code …``, ``build timed out``) never become entries.

An open entry missing from a failed build is only resolved when that build could have shown it (round 10 "not
visible" ≠ "resolved"): its module built, or the build got at least as far as the entry's stage (pom → resolution
→ enforcer → compile → test-compile); a module skipped after another one failed shows nothing. A build that timed
out or failed without parsed errors (``void``) resolves nothing.

Every text is masked (``kb.redact_file`` + built-ins, :mod:`sbmig.kb.redact`) and the workspace roots / home
directory are stripped before it is stored; ``raw`` is capped by ``errors.raw_chars``, a try's ``diff`` by
``errors.diff_chars`` (the full diff is in ``changes.patch``), a try's 수정 notes (``FixTry.edits``) at
``NOTES_MAX`` with their texts capped. Writing never raises (a warning + ``problems``).


"""
from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from sbmig.core.models import (
    BuildError,
    BuildResult,
    DependencyChange,
    EditHunk,
    EditNote,
    ErrorEntry,
    ErrorKind,
    FixTry,
    ResolvedBy,
    utcnow,
)

from .common import Cleaner, atomic_write, case_id_for, case_id_of, entry_id, first_line, note_text, posix

log = logging.getLogger("sbmig.errors")

ERRORS_FILE = "errors.jsonl"
ERRORS_MD = "errors.md"
MESSAGE_CHARS = 300
NOTE_CHARS = 2000
FACT_CHARS = 2000                     # v2.1z-ai signature facts of an entry (symbol / location / l1 / artifact / goal)
TEXT_CHARS = 4000                     # analysis / solution / no_script_reason of a try
MD_DIFF_CHARS = 2000                  # a try's diff in errors.md (the jsonl keeps errors.diff_chars)
NOTES_MAX = 100                       # 수정 notes kept per try (v2.1b; a script that touched more files: the diff says)
NOTE_WHERE_CHARS = 200                # EditNote.where / lines
NOTE_CHANGE_CHARS = 600               # EditNote.change
MD_NOTES = 30                         # errors.md에 시도마다 보여 줄 수정 기록 수
TRIES_CELL_CHANGE = 60                # the first note's change in the errors.md tries cell
MAJOR_MAX = 50                        # v2.1d major upgrades / library replacements kept per try
MAJOR_MARK = "⚠ 메이저 업그레이드"      # errors.md: the tries cell of a try with major upgrades (운영자 확인)
SYNTHETIC_PREFIXES = ("mvn exited with code", "build timed out")
SUCCESS_RESULTS = frozenset({"improved", "build_ok", "partial"})
# 단계 s의 오류가 사라졌다고 해결로 보려면 빌드가 s까지 진행했어야 하므로 오류 종류별로 멈춘 단계를 둔다
# how far a build got when it stopped with an error of this kind (an absent entry of stage s is only gone when the
# build reached s); plugin_execution has no stage — only its module building (or an ok build) proves it gone
def stage_of(kind: ErrorKind, goal: Optional[str] = None, l2: str = "") -> Optional[int]:
    """이 종류의 오류가 보고되는 Maven 단계 번호(``STAGE``)를 반환한다.
    플러그인 실행 실패는 goal로 정한다. resources는 compile 전, compiler는 compile, surefire / failsafe는
    테스트 컴파일 뒤, 나머지는 package 단계로 본다.

    The Maven stage at which an error of this kind is reported (`STAGE`); a plugin execution failure is placed
    by its goal — `resources:resources` runs BEFORE compile (process-resources), `compiler:*` with it,
    surefire / failsafe after the tests compile, every other plugin at package time. Company run 2026-10-27: a
    resources-filtering failure (kind plugin_execution) had no stage, so the compile errors its fix uncovered
    counted as "earlier errors hiding the target" and a correct pom fix was reverted three times."""
    if kind == ErrorKind.PLUGIN_EXECUTION:
        g = (goal or "").lower()
        if not g and l2.startswith("plugin_execution|"):
            g = l2.split("|")[1].lower() if l2.count("|") >= 1 else ""
        if "resources" in g:
            return 2
        if "compiler" in g:
            return 3
        if "surefire" in g or "failsafe" in g:
            return 6
        return 5
    return STAGE.get(kind)


def error_stage(e: BuildError) -> Optional[int]:
    """빌드 오류 하나가 보고된 단계 번호를 반환한다. target/ 아래 Windows 파일 잠금 오류는 None을 반환한다.
    환경 문제는 빌드가 어디까지 갔는지 알려 주지 않으므로, 성공한 시도를 되돌리는 근거로 쓰지 않기 위해서다.

    The stage of one build error; None for a Windows file lock under target/ (v2.1r): an environment failure proves no
    stage and hides nothing, so the loop never scores it as "a new earlier-stage error" and reverts a good attempt."""
    if _is_file_lock(e):            # also javac's "error while writing …\target\classes\X.class (… another process)"
        return None
    return stage_of(e.kind, getattr(e, "goal", None), getattr(e, "signature_generic", "") or "")


def _is_file_lock(e: BuildError) -> bool:
    try:
        from sbmig.build.env_errors import file_lock_hit
        return file_lock_hit(f"{e.message or ''}\n{getattr(e, 'raw', '') or ''}") is not None
    except Exception:  # noqa: BLE001 - a stage is never worth a crash
        return False


# 단계 번호: pom(0) -> 의존성 해결(1) -> enforcer, rewrite(2) ->
# compile(3) -> test-compile(4) -> package(5) -> 테스트(6)
STAGE: dict[ErrorKind, int] = {
    ErrorKind.POM_INVALID: 0, ErrorKind.PARENT_RESOLUTION: 0,
    ErrorKind.DEPENDENCY_RESOLUTION: 1, ErrorKind.PLUGIN_RESOLUTION: 1,
    ErrorKind.ENFORCER: 2, ErrorKind.REWRITE: 2,
    ErrorKind.COMPILE: 3, ErrorKind.UNKNOWN: 3,
    ErrorKind.TEST_COMPILE: 4,
    ErrorKind.TEST_FAILURE: 6,
}
POM_KINDS = frozenset({ErrorKind.POM_INVALID, ErrorKind.PARENT_RESOLUTION, ErrorKind.DEPENDENCY_RESOLUTION,
                       ErrorKind.PLUGIN_RESOLUTION, ErrorKind.ENFORCER})
_ROUND_RE = re.compile(r"\bfix r(\d+)\b")

BuildLike = Union[BuildResult, Iterable[BuildError], None]


def is_synthetic(e: BuildError) -> bool:
    """Maven이 죽거나 타임아웃됐을 때 빌드 실행기가 대신 넣는 오류인지 확인한다.
    코드와 무관한 오류이므로 항목으로 만들지 않는다.

    The build runner's stand-in error for a crashed / timed-out Maven (says nothing about the code)."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(SYNTHETIC_PREFIXES)


def round_of(seen: str) -> int:
    """``seen`` 값에서 라운드 번호를 꺼낸다. ``fix r3``이면 3을, 그 밖의 값이면 0을 반환한다.

    ``fix r3`` → 3; anything else → 0."""
    m = _ROUND_RE.search(seen or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------------------------------------------------
# v2.1f: an error gone from a build without a try of its own → the applied try that probably fixed it
# ---------------------------------------------------------------------------------------------------------------------
# 되돌렸거나 적용되지 않은 시도는 지금 코드에 남아 있지 않으므로 오류를 고친 시도로 볼 수 없다
FIXER_SKIP_RESULTS = ("reverted", "apply_failed", "blocked")   # a try that is not in the tree cannot have fixed it
FIXERS_MAX = 3


def _pom_covers(pom: str, file: str) -> int:
    """``pom``이 ``file``을 포함하는 깊이를 반환한다.
    루트 pom은 0, 모듈 pom은 폴더 깊이이고, 포함하지 않으면 -1이다.

    Depth of ``pom`` (a pom.xml) above ``file``: 0 for the root pom, the folder depth for a module pom; -1 when
    the pom is not above the file."""
    if posixpath.basename(pom) != "pom.xml":
        return -1
    d = posixpath.dirname(pom)
    if not d:
        return 0
    return d.count("/") + 1 if file.startswith(d + "/") else -1


def probable_fixers(e: ErrorEntry, tries: Iterable[tuple[str, FixTry]], *, rounds: Optional[Iterable[int]] = None,
                    match: str = "file", limit: int = FIXERS_MAX) -> list[ResolvedBy]:
    """수정 시도 없이 빌드에서 사라진 오류를 고쳤을 것으로 보이는 시도를 찾는다.
    오류가 난 파일이나 그 위의 pom을 바꾼 적용된 시도를 같은 파일, 깊은 pom 순으로 최대 ``limit``개
    반환한다. ``rounds``가 None이면 모든 라운드를 본다.

    The applied tries (``(case_id, try)`` pairs, any entry's) that changed the entry's file or a pom above it —
    the probable fix of an error resolved by ``build`` (no try of its own). ``rounds`` limits the tries (live: the
    round the build scores; None = any). Nearest first: the same file, then the deepest pom; a try that was reverted
    / never applied is never a fixer. ``match`` labels live ("file" / "pom") or "reindex" attribution."""
    file = posix(e.file or "")
    if not file:
        return []
    allowed = {int(r) for r in rounds} if rounds is not None else None
    found: dict[tuple[str, int, int], tuple[tuple[int, int, int, int], ResolvedBy]] = {}
    for case_id, t in tries:
        if not case_id or t.applied == "none" or t.result in FIXER_SKIP_RESULTS or not t.files:
            continue
        if allowed is not None and int(t.round) not in allowed:
            continue
        key = (case_id, int(t.round), int(t.n))
        if key in found:
            continue
        # 같은 파일이면 바로 확정하고(99는 어떤 pom보다 가깝다는 뜻), 아니면 파일을 포함하는 가장 깊은 pom을 고른다
        hit, kind, depth = "", "", -1
        for f in t.files:
            pf = posix(f)
            if pf == file:
                hit, kind, depth = pf, "file", 99
                break
            d = _pom_covers(pf, file)
            if d > depth:
                hit, kind, depth = pf, "pom", d
        if not hit:
            continue
        note = next((n for n in t.edits if posix(n.path) == hit), None)
        what = note_text(note, 160) if note is not None else hit
        rb = ResolvedBy(case_id=case_id, round=int(t.round), n=int(t.n), commit=t.commit or "", files=[hit],
                        match="reindex" if match == "reindex" else kind,
                        text=f"{case_id} r{t.round} try {t.n} — {what}")
        # 정렬 순서는 같은 파일, 깊은 pom, 이른 라운드와 시도 순이다
        found[key] = ((0 if kind == "file" else 1, -depth, int(t.round), int(t.n)), rb)
    return [rb for _, rb in sorted(found.values(), key=lambda x: x[0])][:limit]


def resolved_text(e: ErrorEntry) -> str:
    """오류가 무엇으로 해결됐는지 나타내는 문자열을 만든다.
    ``r3 try 2``, ``build``, 또는 build로 해결된 오류에 추정한 시도를 붙인 형태다.
    errors.md, 보고서의 오류 목록, cases.md가 사용한다.

    ``r3 try 2`` / ``build`` / ``build ← 7a52b1cea8 r1 try 1`` (the probable fixer of an error resolved by build)."""
    if e.resolved == "build" and e.resolved_by:
        rb = e.resolved_by[0]
        return f"build ← {rb.case_id} r{rb.round} try {rb.n}"
    return e.resolved or ""


def fmt_entry(e: ErrorEntry) -> str:
    """오류 항목을 ``파일:줄 메시지`` 한 줄로 만든다. 파일이 없으면 모듈 이름이나 '-'를 쓴다.

    ``file:line message`` (module or '-' without a file)."""
    loc = e.file or e.module or "-"
    if e.file and e.line:
        loc += f":{e.line}"
    return f"{loc} {e.message}".strip()


def tries_text(e: ErrorEntry, with_change: bool = False) -> str:
    """오류의 시도들을 한 줄로 요약한다. 시도마다 번호, 출처, 적용 방식, 결과를 쓴다.
    ``with_change``가 참이면 첫 수정 기록 내용(60자까지)과 메이저 업그레이드 표시를 붙인다.

    ``1 llm-new edits improved → 2 kb-script v2 script build_ok``; ``with_change`` (errors.md) appends each try's
    first 수정 note's change (≤ 60 characters): ``1 llm-new edits improved (jaxb-runtime 의존성을 추가합니다)``."""
    parts = []
    for t in e.tries:
        bits = [str(t.n), t.source] + ([f"v{t.script_version}"] if t.script_version else []) + [t.applied, t.result]
        first = next((" ".join((getattr(x, "change", "") or "").split()) for x in (t.edits or [])
                      if getattr(x, "change", "")), "") if with_change else ""
        if first:
            bits.append(f"({first if len(first) <= TRIES_CELL_CHANGE else first[:TRIES_CELL_CHANGE - 1] + '…'})")
        if with_change and getattr(t, "major_upgrades", None):
            bits.append(f"{MAJOR_MARK} {len(t.major_upgrades)}")
        parts.append(" ".join(bits))
    return " → ".join(parts)


def change_line(c: Any) -> str:
    """메이저 버전 변경 하나를 한 줄로 만든다.
    좌표, 이전과 이후 버전, 섹션, pom 경로를 쓰고 사람이 승인했으면 ``approved``를 붙인다.

    `org.sitemesh:sitemesh 2.4.2 → 3.2.1 (dependencies, demo-back/pom.xml), approved` — one 메이저 변경 line."""
    text = getattr(c, "text", None) or f"{getattr(c, 'key', '?')}"
    return (f"{text} ({getattr(c, 'section', 'dependencies')}, {getattr(c, 'path', '-')})"
            + (", approved" if getattr(c, "approved", False) else ""))


def _src_tail(path: str) -> str:
    """The path from its first ``src/`` segment on ("" without one)."""
    p = "/" + posix(path)
    i = p.find("/src/")
    return p[i + 1:] if i >= 0 else ""


def _moved(old: str, new: str) -> bool:
    """재구성 단계에서 ``old`` 파일이 ``new``로 옮겨졌는지 확인한다.
    한쪽 경로가 다른 쪽으로 끝나거나 ``src/`` 이후 부분이 같으면 같은 파일로 본다.

    Did the restructure move ``old`` to ``new``? One ends with the other, or both share the ``src/…`` tail."""
    old, new = posix(old), posix(new)
    if not old or not new or old == new:
        return False
    if new.endswith("/" + old) or old.endswith("/" + new):
        return True
    t = _src_tail(old)
    return bool(t) and t == _src_tail(new)


def _module_dir(pom: str) -> Optional[str]:
    """``demo-back/pom.xml`` → ``demo-back``; the root pom → None."""
    p = posix(pom).rsplit("/", 1)
    return p[0].rsplit("/", 1)[-1] if len(p) == 2 and p[0] else None


class _Build:
    """오류 목록 관점에서 빌드 하나의 결과를 담는 클래스.
    실제 오류, 결과를 믿을 수 없는 빌드(void)인지 여부, 도달한 단계(``reached``)를 가진다.

    One build as the list sees it: its real errors, whether it says anything, how far it got."""

    def __init__(self, build: BuildLike, void: bool = False):
        self.result = build if isinstance(build, BuildResult) else None
        errs = list(self.result.errors if self.result is not None else (build or []))
        self.real = [e for e in errs if not is_synthetic(e)]
        r = self.result
        self.ok = (r.ok if r is not None else not errs) and not void
        # 타임아웃됐거나 실패했는데 파싱된 오류가 없는 빌드는 void로 보고, 어떤 오류도 해결로 처리하지 않는다
        self.void = bool(void) or (r is not None and (bool(r.timed_out) or (not r.ok and not self.real))) or \
            (r is None and bool(errs) and not self.real)
        self.reached = max((error_stage(e) if error_stage(e) is not None else -1
                            for e in self.real if e.kind != ErrorKind.UNKNOWN or e.file), default=-1)

    def can_show(self, e: ErrorEntry) -> bool:
        """지금 보이지 않는 오류 ``e``를 이 빌드가 보고할 수 있었는지 확인한다.
        void 빌드나 다른 모듈의 실패로 건너뛴 모듈의 오류는 해결 여부를 알 수 없으므로 False다.

        Could this build have reported ``e`` (now absent)?"""
        if self.void:
            return False
        if self.ok or not self.real:
            return True
        r, m = self.result, e.module
        if r is not None:
            if m and m in (r.modules_built or []):
                return True
            failed = {r.failed_module} | {x.module for x in self.real if x.module}
            if m and r.failed_module and m not in failed:
                return False                              # its module never ran (skipped after another one failed)
        st = stage_of(e.kind, None, e.l2 or "")
        return st is not None and self.reached >= st


class ErrorList:
    """실행 하나의 오류 목록을 관리하는 클래스.
    실행에서는 ``ErrorList.for_workspace(ws, cfg, project)``로 만든다.
    항목은 메모리에 두고 바뀔 때마다 파일을 통째로 다시 쓴다.

    The error list of one run. ``ErrorList.for_workspace(ws, cfg, project)`` in a run; tests build it directly."""

    def __init__(self, run_dir: Union[str, Path], run_id: str, *, project: str = "", cfg: Any = None,
                 roots: Iterable[Any] = ()):
        self.run_dir = Path(run_dir)
        self.run_id = run_id or ""
        self.project = project or ""
        ec = getattr(cfg, "errors", None)
        self.diff_chars = int(getattr(ec, "diff_chars", 6000) or 0)
        self.raw_chars = int(getattr(ec, "raw_chars", 600) or 0)
        self.md_view = bool(getattr(ec, "md_view", True))
        self.clean = Cleaner(cfg, roots)
        self.entries: list[ErrorEntry] = []
        self._by_id: dict[str, ErrorEntry] = {}
        self._from_build: set[str] = set()       # entries made by add_build (the only ones a build error can match)
        self._present: set[str] = set()          # entries reported by the last build that said anything
        self._tries: dict[tuple[str, int], tuple[FixTry, list[str]]] = {}   # (case, n) → the try, its entries
        self._last: Optional[_Build] = None      # the last build that said anything
        self.builds = 0
        self.problems: list[str] = []

    @classmethod
    def for_workspace(cls, ws: Any, cfg: Any = None, project: str = "") -> "ErrorList":
        """Workspace로 오류 목록을 만든다. 프로젝트 루트와 실행 디렉토리를 텍스트에서 지울 경로로 넘긴다."""
        return cls(ws.run_dir, ws.run_id, project=project or ws.root.name, cfg=cfg, roots=(ws.root, ws.run_dir))

    @property
    def path(self) -> Path:
        """실행 디렉토리의 ``errors.jsonl`` 경로를 반환한다."""
        return self.run_dir / ERRORS_FILE

    @property
    def md_path(self) -> Path:
        """실행 디렉토리의 ``errors.md``(사람이 보는 표) 경로를 반환한다."""
        return self.run_dir / ERRORS_MD

    # ------------------------------------------------------------------ building entries
    def _message(self, e: BuildError) -> str:
        return self.clean(first_line(e.message or e.raw, MESSAGE_CHARS))

    def _new(self, *, case_id: str, l2: str, kind: ErrorKind, file: Optional[str], line: Optional[int],
             module: Optional[str], message: str, raw: str, seen: str, status: str, note: str = "") -> ErrorEntry:
        eid = entry_id(self.run_id, case_id, file, line, message)
        n = 1
        while eid in self._by_id:                         # (only on a sha1 prefix clash)
            n += 1
            eid = entry_id(self.run_id, case_id, file, line, f"{message}#{n}")
        # 현재 서명 규칙 버전을 sig에 남겨 KB 재서명이 이전 규칙으로 만든 기록만 다시 계산하게 한다
        # 메모 항목에도 남겨야 record_run이 매번 재서명하지 않는다
        e = ErrorEntry(id=eid, run_id=self.run_id, project=self.project, case_id=case_id, l2=l2, kind=kind,
                       file=file or None, line=line or None, module=module or None, message=message, raw=raw,
                       seen=seen, status=status, note=note, sig=_current_sig() if l2 else 0)
        self.entries.append(e)
        self._by_id[eid] = e
        return e

    def _from_error(self, e: BuildError, seen: str) -> ErrorEntry:
        entry = self._new(case_id=case_id_of(e), l2=e.signature_generic or "", kind=e.kind,
                          file=posix(e.file) or None, line=e.line, module=e.module, message=self._message(e),
                          raw=self.clean(e.raw or e.message, self.raw_chars), seen=seen, status="open")
        self._facts(entry, e)
        return entry

    def _facts(self, entry: ErrorEntry, e: BuildError) -> None:
        """오류의 서명 정보(L1, javac ``symbol`` / ``location``, ``g:a[:v]``, 플러그인 goal)를 항목에 기록한다.
        서명 규칙이 바뀌면 KB 재서명이 이 값으로 L2를 다시 계산한다. 값은 ``message``처럼 마스킹을 거친다.

        v2.1z-ai: the signature facts of the error (L1, javac ``symbol`` / ``location``, ``g:a[:v]``, the plugin goal),
        cleaned like ``message``; ``sig`` = the current rule version when the entry has an L2."""
        c = self.clean
        art = getattr(e, "artifact", None)
        coords = ""
        if art is not None:
            try:
                coords = art.coordinates
            except Exception:  # noqa: BLE001 - an odd GAV: no coordinates
                coords = ""
        entry.l1 = c(e.signature or "", FACT_CHARS)
        entry.symbol = c(e.symbol or "", FACT_CHARS)
        entry.location = c(e.location or "", FACT_CHARS)
        entry.artifact = c(coords, FACT_CHARS)
        entry.goal = c(e.goal or "", FACT_CHARS)
        entry.sig = _current_sig() if entry.l2 else 0

    def _match(self, errors: list[BuildError]) -> tuple[list[tuple[ErrorEntry, BuildError]], list[BuildError]]:
        """빌드 오류를 기존 항목과 짝짓는다. 정확히 일치하는 항목, 같은 파일과 메시지 중 줄이 가장 가까운
        항목, 재구성으로 옮겨진 파일의 항목 순서로 찾는다.
        (짝지은 쌍, 짝이 없는 오류)를 반환하고, 짝이 없는 오류만 새 항목이 된다.

        (matched (entry, error) pairs, unmatched errors) — exact, nearest line, moved file (see the module doc)."""
        cands = [e for e in self.entries if e.id in self._from_build]
        keyed = [(case_id_of(x), posix(x.file), x.line or 0, self._message(x), x) for x in errors]
        used: set[str] = set()
        pairs: list[tuple[ErrorEntry, BuildError]] = []
        rest = []
        exact: dict[tuple, list[ErrorEntry]] = {}
        for c in cands:
            exact.setdefault((c.case_id, posix(c.file), c.line or 0, c.message), []).append(c)
        for k in keyed:
            hit = next((c for c in exact.get(k[:4], []) if c.id not in used), None)
            if hit is not None:
                used.add(hit.id)
                pairs.append((hit, k[4]))
            else:
                rest.append(k)
        rest2 = []
        for cid, f, line, msg, x in rest:                 # same file, the line moved
            opts = [c for c in cands if c.id not in used and c.case_id == cid and posix(c.file) == f
                    and c.message == msg]
            if opts:
                hit = min(opts, key=lambda c: abs((c.line or 0) - line))
                used.add(hit.id)
                hit.line = line or None
                self._facts(hit, x)                       # the newest sighting's facts (same case, same L2)
                pairs.append((hit, x))
            else:
                rest2.append((cid, f, line, msg, x))
        unmatched = []
        for cid, f, line, msg, x in rest2:                # the restructure moved the file (entries seen at compile)
            opts = [c for c in cands if c.id not in used and c.case_id == cid and c.message == msg
                    and c.seen == "compile" and _moved(c.file or "", f)]
            if opts:
                hit = min(opts, key=lambda c: abs((c.line or 0) - line))
                used.add(hit.id)
                hit.file, hit.line, hit.module = f or None, line or None, x.module or hit.module
                self._facts(hit, x)
                pairs.append((hit, x))
            else:
                unmatched.append(x)
        return pairs, unmatched

    def add_build(self, errors: BuildLike, seen: str, *, round: Optional[int] = None, void: bool = False
                  ) -> list[ErrorEntry]:
        """빌드 하나의 오류를 목록에 합치고 새로 생긴 항목을 반환한다.
        사라진 항목은 이 빌드가 보고할 수 있었을 때만 ``build``로 해결하고, 결과를 기다리는 시도가 있으면
        ``resolve``에 맡긴다. 해결됐던 항목이 다시 보이면 다시 연다.

        Merge one build (a BuildResult — preferred: it says whether the build was ok / timed out and which modules
        built — or its error list) into the list. Returns the NEW entries. ``round`` = the fix round whose tries this
        build scores (default: from ``seen``, ``fix r3`` → 3). ``void`` = the build says nothing (also implied by a
        timed-out BuildResult, or a failed one without parsed errors): nothing is resolved."""
        b = _Build(errors, void)
        rnd = round_of(seen) if round is None else int(round)
        self.builds += 1
        pairs, unmatched = self._match(b.real)
        present: set[str] = set()
        for entry, _err in pairs:
            present.add(entry.id)
            if entry.status in ("resolved", "unresolved"):
                entry.note = _join(entry.note, f"reappeared in {seen}" + (f" (was resolved: {entry.resolved})"
                                                                            if entry.resolved else ""))
                entry.status, entry.resolved, entry.script = "open", "", ""
        new: list[ErrorEntry] = []
        taken = {(x.case_id, posix(x.file), x.line or 0, x.message) for x, _ in pairs}
        for err in unmatched:
            k = (case_id_of(err), posix(err.file), err.line or 0, self._message(err))
            if k in taken:
                continue                                  # the same error twice in one build: one entry
            taken.add(k)
            e = self._from_error(err, seen)
            self._from_build.add(e.id)
            new.append(e)
            present.add(e.id)
        resolved = 0
        if not b.void:
            for e in self.entries:
                if e.status != "open" or e.id in present or e.id not in self._from_build or not b.can_show(e):
                    continue
                last = e.tries[-1] if e.tries else None
                # 이 빌드가 결과를 평가하는 시도가 있으므로, 여기서 build로
                # 닫으면 시도의 성과가 사라져 resolve()에 맡긴다
                if last is not None and last.round == rnd and rnd and last.result == "pending":
                    continue                              # this build scores that try: the loop's resolve() decides
                by = last if last is not None and last.result in SUCCESS_RESULTS else None
                self._resolve_entry(e, by)
                # 시도 없이 사라진 오류는 같은 라운드에 그 파일이나 위의 pom을 바꾼 시도가 고친 것으로 기록한다
                if by is None and e.case_id:              # v2.1f: gone without a try → who probably fixed it
                    e.resolved_by = probable_fixers(e, self._all_tries(), rounds=[rnd])
                resolved += 1
            self._present = present
            self._last = b
        else:
            # void 빌드로는 해결 여부를 판단하지 않고 이번에 보인 항목만 추가한다
            self._present |= present
        self.write()
        log.debug("error list: build %s → %d new, %d resolved, %d open%s", seen, len(new), resolved,
                  sum(1 for e in self.entries if e.status == "open"), " (void build)" if b.void else "")
        return new

    def add_pom_verify(self, file: str, note: str) -> ErrorEntry:
        """재구성이 고친 pom 하나를 항목(status pom-verify, seen restructure)으로 추가한다.
        다음 빌드에서 ``verify_poms``가 확인한다.
        같은 pom에 같은 첫 줄의 항목이 있으면 기존 항목을 반환한다.

        A restructure pom correction (status pom-verify, seen restructure) that the next build verifies."""
        f = posix(self.clean(file)) or "pom.xml"
        text = self.clean(note, NOTE_CHARS)
        msg = first_line(text, MESSAGE_CHARS)
        old = self._by_id.get(entry_id(self.run_id, "", f, 0, msg))
        if old is not None:
            return old
        e = self._new(case_id="", l2="", kind=ErrorKind.UNKNOWN, file=f, line=None, module=_module_dir(f),
                      message=msg, raw="", seen="restructure", status="pom-verify", note=text)
        self.write()
        return e

    def add_note(self, file: Optional[str], text: str, seen: str = "review", *, status: str = "note",
                 l2: str = "", kind: ErrorKind = ErrorKind.UNKNOWN) -> ErrorEntry:
        """정보성 항목을 추가한다.
        구조 검토 warning은 status note로, 수정 시도로 고칠 지적은 ``open``으로 둔다.
        같은 파일에 같은 첫 줄의 메모가 있으면 다시 추가하지 않고 기존 항목을 반환한다.

        An informational entry (a review warning: status note; ``status`` may be ``open`` for a finding a try
        will fix). An identical note (same file and first line) is returned, not added again."""
        f = posix(self.clean(file or "")) or None
        body = self.clean(text, NOTE_CHARS)
        msg = first_line(body, MESSAGE_CHARS)
        cid = case_id_for(l2)
        old = self._by_id.get(entry_id(self.run_id, cid, f, 0, msg))
        if old is not None:
            return old
        e = self._new(case_id=cid, l2=l2, kind=kind, file=f, line=None, module=_module_dir(f) if f else None,
                      message=msg, raw="", seen=seen, status=status, note=body if body != msg else "")
        self.write()
        return e

    def verify_poms(self, build: BuildLike) -> list[ErrorEntry]:
        """pom-verify 항목 중 ``build``에서 해당 pom에 오류가 없는 것을 해결로 바꾸고 반환한다.
        오류가 있으면 pom-verify로 두고 오류를 ``note``에 기록한다. void 빌드로는 아무것도 확인하지 않는다.

        pom-verify entries whose pom shows no error in ``build`` → resolved ("build"); the others stay pom-verify
        with the build's error on that pom named in ``note``. A void build verifies nothing. Returns the resolved
        entries."""
        b = _Build(build)
        if b.void:
            return []
        done = []
        for e in self.entries:
            if e.status != "pom-verify":
                continue
            hits = [x for x in b.real if _pom_hit(e, x)]
            if hits:
                msg = f"build: {first_line(self.clean(hits[0].message or hits[0].raw), MESSAGE_CHARS)}"
                if msg not in (e.note or ""):
                    e.note = _join(e.note, msg)
            else:
                e.status, e.resolved = "resolved", "build"
                done.append(e)
        self.write()
        return done

    # ------------------------------------------------------------------ tries
    def begin_try(self, case_id: str, round: int, source: str, script_version: int = 0, *,
                  entry_ids: Optional[Iterable[str]] = None) -> FixTry:
        """케이스의 열린 항목 전부(또는 ``entry_ids``의 항목)에 새 시도 n을 추가한다.
        항목들이 FixTry 객체 하나를 함께 쓰므로 케이스의 모든 오류에 같은 시도가 기록된다.

        A new try (n = the case's next number in this run) on every open entry of the case — or on the given
        ``entry_ids`` (a review fix on its notes). The entries share the one FixTry object."""
        if entry_ids is not None:
            targets = [self._by_id[i] for i in entry_ids if i in self._by_id]
        else:
            targets = [e for e in self.entries if e.case_id == case_id and e.status == "open"]
        n = 1 + max((k[1] for k in self._tries if k[0] == case_id), default=0)
        t = FixTry(n=n, round=int(round or 0), source=source, script_version=int(script_version or 0))
        for e in targets:
            e.tries.append(t)
        self._tries[(case_id, n)] = (t, [e.id for e in targets])
        self.write()
        return t

    def get_try(self, case_id: str, n: int) -> Optional[FixTry]:
        """케이스의 n번째 시도를 반환한다. 없으면 None을 반환한다."""
        hit = self._tries.get((case_id, n))
        return hit[0] if hit else None

    def finish_try(self, case_id: str, n: int, *, analysis: Optional[str] = None, solution: Optional[str] = None,
                   no_script_reason: Optional[str] = None, applied: Optional[str] = None,
                   files: Optional[Iterable[str]] = None, diff: Optional[str] = None, commit: Optional[str] = None,
                   result: Optional[str] = None, note: Optional[str] = None, llm_calls: Optional[int] = None,
                   seconds: Optional[float] = None, script_version: Optional[int] = None,
                   edits: Optional[Iterable[Any]] = None, major_upgrades: Iterable[Any] | None = None,
                   verify: str | None = None, verify_source: str | None = None) -> Optional[FixTry]:
        """시도 n의 내용(원인, 해결, 바뀐 파일, diff, 수정 기록, 결과 등)을 채운다.
        None인 인자는 기존 값을 그대로 둔다. 적용 후 한 번, 결과 평가 후 한 번 더 호출한다.

        Fill in what try ``n`` of the case did; a field left None keeps its value (call it after applying and
        again with the scoring ``result``). Texts are masked; the diff is capped by ``errors.diff_chars``.
        ``edits`` = the 수정 notes (``EditNote`` or dicts: where / change by the LLM, lines from the diff);
        ``major_upgrades`` (v2.1d) = the try's major upgrades / library replacements (``DependencyChange`` or dicts),
        ``verify`` = the 운영자 확인 guide for them (capped at TEXT_CHARS)."""
        t = self.get_try(case_id, n)
        if t is None:
            log.warning("error list: no try %s of case %s to finish", n, case_id or "-")
            return None
        if analysis is not None:
            t.analysis = self.clean(analysis, TEXT_CHARS)
        if solution is not None:
            t.solution = self.clean(solution, TEXT_CHARS)
        if no_script_reason is not None:
            t.no_script_reason = self.clean(no_script_reason, TEXT_CHARS)
        if applied is not None:
            t.applied = applied
        if files is not None:
            t.files = list(dict.fromkeys(posix(self.clean(f)) for f in files if f))
        if diff is not None:
            t.diff = self.clean(diff, self.diff_chars)
        if edits is not None:
            t.edits = self._notes(edits)
        if major_upgrades is not None:
            t.major_upgrades = self._changes(major_upgrades)
        if verify is not None:
            t.verify = self.clean(verify, TEXT_CHARS)
        if verify_source is not None:
            t.verify_source = verify_source if verify_source in ("llm", "sbmig") else ""
        if commit is not None:
            t.commit = commit or ""
        if result is not None:
            t.result = result
        if note is not None:
            t.note = self.clean(note, NOTE_CHARS)
        if llm_calls is not None:
            t.llm_calls = int(llm_calls)
        if seconds is not None:
            t.seconds = round(float(seconds), 1)
        if script_version is not None:
            t.script_version = int(script_version)
        self.write()
        return t

    def _notes(self, notes: Iterable[Any]) -> list[EditNote]:
        """시도의 수정 기록을 마스킹하고 길이를 제한해 반환한다.
        읽을 수 없는 기록은 건너뛰고 시도를 실패로 만들지 않는다.
        변경 부분은 ``errors.snippet_lines``로 이미 잘려 있으므로 마스킹만 한다.

        The try's 수정 notes, masked and capped (an unreadable item is skipped, never fails the try). v2.1e: each
        note keeps its as-is / to-be `hunks`, masked WITHOUT a length cap (`errors.snippet_lines` already cut them
        where the notes were made); an unreadable hunk is left out, never the note."""
        out: list[EditNote] = []
        for x in notes or []:
            try:
                if isinstance(x, EditNote):
                    n, hunks = x, list(x.hunks)
                else:
                    d = x if isinstance(x, dict) else {k: getattr(x, k, "") for k in ("path", "where", "change",
                                                                                       "lines", "source", "hunks")}
                    hunks = _hunk_items(d.get("hunks"))
                    n = EditNote.model_validate(
                        {k: v for k, v in d.items() if k != "hunks" and v not in (None, "")
                         and (k != "source" or v in ("llm", "diff"))})
            except Exception:  # noqa: BLE001 - a malformed note (no path) is left out
                continue
            out.append(EditNote(path=posix(self.clean(n.path)) or "-", where=self.clean(n.where, NOTE_WHERE_CHARS),
                                change=self.clean(n.change, NOTE_CHANGE_CHARS),
                                lines=(n.lines or "")[:NOTE_WHERE_CHARS], source=n.source,
                                hunks=[EditHunk(lines=(h.lines or "")[:NOTE_WHERE_CHARS], before=self.clean(h.before),
                                                after=self.clean(h.after), truncated=bool(h.truncated))
                                       for h in hunks]))
            if len(out) >= NOTES_MAX:
                break
        return out

    def _changes(self, items: Iterable[Any]) -> list[DependencyChange]:
        """시도의 메이저 업그레이드 목록을 마스킹해 반환한다. 읽을 수 없는 항목은 건너뛴다.

        v2.1d: the try's major upgrades, masked (an unreadable item is skipped, never fails the try)."""
        out: list[DependencyChange] = []
        for x in items or []:
            try:
                c = x if isinstance(x, DependencyChange) else DependencyChange.model_validate(
                    x if isinstance(x, dict) else x.model_dump())
            except Exception as e:  # noqa: BLE001 - a malformed change (no path / key / kind) is left out
                log.debug("error list: dependency change skipped: %s: %s", type(e).__name__, e)
                continue
            out.append(c.model_copy(update={
                "path": posix(self.clean(c.path)) or "-", "key": self.clean(c.key, NOTE_WHERE_CHARS),
                "before": self.clean(c.before, NOTE_WHERE_CHARS) if c.before else c.before,
                "after": self.clean(c.after, NOTE_WHERE_CHARS) if c.after else c.after,
                "replaced_by": self.clean(c.replaced_by, NOTE_WHERE_CHARS) if c.replaced_by else c.replaced_by,
                "note": self.clean(c.note, NOTE_CHANGE_CHARS)}))
            if len(out) >= MAJOR_MAX:
                break
        return out

    def _all_tries(self) -> list[tuple[str, FixTry]]:
        """이 실행의 모든 시도를 ``(case_id, try)`` 목록으로 반환한다. (케이스, 라운드, n)마다 하나씩이다.

        Every try of the run as ``(case_id, try)``, one per (case, round, n)."""
        out: dict[tuple[str, int, int], tuple[str, FixTry]] = {}
        for x in self.entries:
            for t in x.tries:
                out.setdefault((x.case_id, int(t.round), int(t.n)), (x.case_id, t))
        return list(out.values())

    def _resolve_entry(self, e: ErrorEntry, t: Optional[FixTry], how: str = "") -> None:
        """항목을 해결 상태로 바꾼다.
        해결 방식은 ``how``가 있으면 그 값, 시도가 없으면 ``build``, 구조 검토 시도면 ``review``다.
        저장된 스크립트가 고쳤으면 ``script``에 ``cases/<id>/fix_v<N>.py``를 넣는다."""
        e.status = "resolved"
        if how:
            e.resolved = how
        elif t is None:
            e.resolved = "build"
        else:
            e.resolved = f"r{t.round} try {t.n}" if t.round else "review"
        e.script = f"cases/{e.case_id}/fix_v{t.script_version}.py" \
            if t is not None and t.applied == "script" and t.script_version and e.case_id else ""

    def resolve(self, case_id: str, round: int, n: int, *, entry_ids: Optional[Iterable[str]] = None,
                how: str = "") -> list[ErrorEntry]:
        """수정 루프가 시도 n의 결과를 반영한다.
        아직 열려 있고 마지막 빌드에서 사라진 항목을 ``r<round> try <n>``으로 해결한다.
        구조 검토 시도면 ``review``로 해결한다. ``entry_ids``가 있으면 그 항목만 해결한다.

        The loop's verdict for try ``n``: its entries that are still open and gone from the last build →
        resolved ``r<round> try <n>`` (round 0 → ``review``; ``how`` overrides), ``script`` set when a stored
        script did it. ``entry_ids`` resolves exactly those entries (a review fix). Returns the resolved entries."""
        hit = self._tries.get((case_id, n))
        t = hit[0] if hit else None
        if entry_ids is not None:
            targets = [self._by_id[i] for i in entry_ids if i in self._by_id]
        else:
            ids = hit[1] if hit else []
            targets = [self._by_id[i] for i in ids if i in self._by_id and i not in self._present]
        if t is None:
            how = how or (f"r{round} try {n}" if round else "review")
        out = []
        for e in targets:
            if e.status in ("resolved", "blocked"):
                continue
            self._resolve_entry(e, t, how)
            out.append(e)
        self.write()
        return out

    def block(self, case_id: str, reason: str) -> list[ErrorEntry]:
        """케이스의 열린 항목을 모두 차단 상태로 바꾼다. 차단 사유는 ``note``에 기록한다.

        Every open entry of the case → blocked (the reason in ``note``)."""
        text = self.clean(reason, NOTE_CHARS)
        out = []
        for e in self.entries:
            if e.case_id == case_id and e.status == "open":
                e.status = "blocked"
                e.note = _join(e.note, f"blocked: {text}" if text else "blocked")
                out.append(e)
        self.write()
        return out

    def end_of_run(self) -> dict[str, int]:
        """실행 종료 시 열린 항목을 정리하고 상태별 개수를 반환한다.
        마지막 빌드가 보고할 수 있었는데 보이지 않은 항목은 마지막 시도로 해결하고, 나머지는 unresolved로 바꾼다.

        Open entries absent from the last build that could show them (left for a verdict that never came) →
        resolved by their last try (or ``build``); every other open entry → unresolved. Returns counts by status."""
        for e in self.entries:
            if e.status != "open":
                continue
            if e.id in self._from_build and e.id not in self._present and e.tries and self._last is not None \
                    and self._last.can_show(e):
                last = e.tries[-1]
                self._resolve_entry(e, last if last.result in SUCCESS_RESULTS | {"pending"} else None)
            else:
                e.status = "unresolved"
        self.write()
        return self.counts()

    # ------------------------------------------------------------------ queries
    def entries_for(self, case_id: str) -> list[ErrorEntry]:
        """케이스의 항목을 상태와 관계없이 모두 반환한다."""
        return [e for e in self.entries if e.case_id == case_id]

    def open_cases(self, present_only: bool = False) -> dict[str, list[ErrorEntry]]:
        """열린 항목을 처음 본 순서대로 케이스별로 묶어 반환한다.
        ``present_only``가 참이면 마지막 빌드가 보고한 항목만 담는다.

        Open entries by case (first-seen order). ``present_only``: only those the last build reported."""
        out: dict[str, list[ErrorEntry]] = {}
        for e in self.entries:
            if e.status == "open" and e.case_id and (not present_only or e.id in self._present):
                out.setdefault(e.case_id, []).append(e)
        return out

    def present(self, entry: ErrorEntry) -> bool:
        """마지막 빌드가 이 항목을 보고했는지 확인한다.

        Did the last build report this entry?"""
        return entry.id in self._present

    def counts(self) -> dict[str, int]:
        """상태별 항목 수를 세어 반환한다."""
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def get(self, entry_id_: str) -> Optional[ErrorEntry]:
        """ID로 항목 하나를 찾는다. 없으면 None을 반환한다."""
        return self._by_id.get(entry_id_)

    def to_report(self) -> list[ErrorEntry]:
        """RunReport에 넣을 항목 복사본을 만든다. raw와 diff는 길이를 제한하고 변경 부분은 뺀다.

        Copies for the RunReport (raw ≤ ``errors.raw_chars``, diffs ≤ ``errors.diff_chars``). v2.1e: the notes'
        as-is / to-be ``hunks`` stay out of the report (it is size-bounded like the diff; the whole code lives in
        ``errors.jsonl``, the KB and the per-error JSON files)."""
        out = []
        for e in self.entries:
            c = e.model_copy(deep=True)
            c.raw = _cap(c.raw, self.raw_chars)
            for t in c.tries:
                t.diff = _cap(t.diff, self.diff_chars)
                for n in t.edits:
                    n.hunks = []
            out.append(c)
        return out

    # ------------------------------------------------------------------ files
    def write(self) -> None:
        """``errors.jsonl``을 통째로 원자적으로 다시 쓴다.
        ``errors.md_view``가 켜져 있으면 ``errors.md``도 쓴다.
        디스크 문제가 있어도 실행을 멈추지 않고 경고와 ``problems``만 남긴다.

        Rewrite ``errors.jsonl`` (+ ``errors.md`` with ``errors.md_view``). Never raises."""
        try:
            atomic_write(self.path, "".join(e.model_dump_json() + "\n" for e in self.entries))
            if self.md_view:
                atomic_write(self.md_path, render_md(self.entries, project=self.project, run_id=self.run_id))
        except Exception as e:  # noqa: BLE001 - the list is a record; a disk problem never fails the run
            msg = f"{type(e).__name__}: {e}"
            if msg not in self.problems:
                self.problems.append(msg)
                log.warning("error list: could not write %s: %s", self.path, msg)

    @staticmethod
    def load(path: Union[str, Path]) -> list[ErrorEntry]:
        """``errors.jsonl``에서 항목을 읽어 반환한다. 읽을 수 없는 줄은 건너뛴다.

        The entries of an ``errors.jsonl`` (unreadable lines skipped)."""
        out = []
        p = Path(path)
        if not p.is_file():
            return out
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(ErrorEntry.model_validate_json(line))
                    except ValueError:
                        continue
        return out


def _hunk_items(raw: Any) -> list[EditHunk]:
    """저장된 기록이나 외부에서 받은 기록에서 읽을 수 있는 변경 부분만 꺼낸다.
    수정 전후가 모두 빈 부분은 버린다.

    The readable hunks of a stored / foreign note (``EditHunk``, dicts, objects with the fields); the rest — and a
    hunk with neither as-is nor to-be text — is skipped."""
    out: list[EditHunk] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for h in raw:
        try:
            if isinstance(h, EditHunk):
                got = h
            elif isinstance(h, dict):
                got = EditHunk.model_validate({k: v for k, v in h.items() if v is not None})
            elif hasattr(h, "before") or hasattr(h, "after"):
                got = EditHunk(lines=str(getattr(h, "lines", "") or ""), before=str(getattr(h, "before", "") or ""),
                               after=str(getattr(h, "after", "") or ""), truncated=bool(getattr(h, "truncated", False)))
            else:
                continue
        except Exception:  # noqa: BLE001, S112 - a malformed hunk is left out
            continue
        if got.before or got.after:
            out.append(got)
    return out


def _current_sig() -> int:
    """현재 서명 규칙 버전(``sbmig.build.signatures.SIGNATURE_VERSION``)을 반환한다.
    호출할 때마다 새로 읽는다.

    The current signature rule version, read at call time."""
    from sbmig.build import signatures

    return int(getattr(signatures, "SIGNATURE_VERSION", 1) or 1)


def _cap(text: str, limit: int) -> str:
    if limit and len(text or "") > limit:
        return text[:limit] + f"\n…[{len(text) - limit} more characters]"
    return text or ""


def _join(old: str, new: str) -> str:
    return f"{old}; {new}" if old else new


def _pom_hit(e: ErrorEntry, x: BuildError) -> bool:
    """빌드 오류 ``x``가 pom-verify 항목 ``e``의 pom에 관한 오류인지 확인한다.
    파일이 같거나, 파일 없는 pom 관련 오류이면서 모듈이 같으면 참이다.

    Is build error ``x`` about the pom of pom-verify entry ``e``?"""
    f = posix(x.file)
    if f:
        return f == posix(e.file)
    return x.kind in POM_KINDS and bool(x.module) and x.module == e.module


# --------------------------------------------------------------------------- errors.md
def guide_lines(text: str) -> list[str]:
    """운영자 확인 가이드 텍스트를 줄 목록으로 바꾼다. 앞의 글머리표와 빈 줄은 뺀다.

    A 운영자 확인 guide as lines (leading bullets stripped, empty lines dropped)."""
    return [x for x in (re.sub(r"^\s*[-*•]\s+", "", ln).strip() for ln in (text or "").splitlines()) if x]


def _cell(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_md(entries: list[ErrorEntry], *, project: str = "", run_id: str = "") -> str:
    """``errors.md`` 내용을 만든다.
    오류 표와 케이스별 시도 내용(원인, 해결, 수정 기록, 메이저 변경, diff)을 쓴다.

    ``errors.md``: the table (``#``, seen, status, case, file:line, message, tries, resolved) and, per case
    with tries, what each try found and did (원인 / 해결 / 수정 — ``path lines — change`` per note — / diff)."""
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.status] = counts.get(e.status, 0) + 1
    title = " — ".join(x for x in (project, run_id) if x)
    L = [f"# Error list{' — ' + title if title else ''}", "",
         f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}: "
         + (" · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
         + f" (written {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC)", "",
         "| # | seen | status | case | file:line | message | tries | resolved |",
         "|---|---|---|---|---|---|---|---|"]
    for i, e in enumerate(entries, 1):
        loc = (e.file or e.module or "-") + (f":{e.line}" if e.file and e.line else "")
        msg = e.message + (f" — {e.note}" if e.note and e.status in ("pom-verify", "blocked", "note") else "")
        L.append(f"| {i} | {_cell(e.seen, 20)} | {e.status} | {e.case_id or '-'} | {_cell(loc, 90)} | "
                 f"{_cell(msg)} | {_cell(tries_text(e, with_change=True), 260) or '-'} | "
                 f"{_cell(resolved_text(e), 60) or '-'} |")
    seen_tries: dict[tuple[str, int], tuple[FixTry, list[int]]] = {}
    for i, e in enumerate(entries, 1):
        for t in e.tries:
            seen_tries.setdefault((e.case_id, t.n), (t, []))[1].append(i)
    if seen_tries:
        L += ["", "## Tries", ""]
        for (cid, n), (t, rows) in seen_tries.items():
            head = (f"### case {cid or '-'} try {n} — round {t.round}, {t.source}"
                    + (f" v{t.script_version}" if t.script_version else "")
                    + f", applied {t.applied}, result **{t.result}**"
                    + (f", commit {t.commit[:10]}" if t.commit else "")
                    + (f", {t.llm_calls} LLM call(s)" if t.llm_calls else "")
                    + (f", {t.seconds:.0f}s" if t.seconds else ""))
            L += [head, "", f"- entries: {', '.join('#' + str(r) for r in rows)}"]
            for label, text in (("원인", t.analysis), ("해결", t.solution), ("스크립트 없음", t.no_script_reason),
                                ("note", t.note)):
                if text:
                    L.append(f"- {label}: {_cell(text, 1200)}")
            if t.edits:
                L.append("- 수정:")
                L += [f"  - {_cell(note_text(x), 600)}" for x in t.edits[:MD_NOTES]]
                if len(t.edits) > MD_NOTES:
                    L.append(f"  - … {len(t.edits) - MD_NOTES} more (errors.jsonl)")
            if t.major_upgrades:
                L.append(f"- 메이저 변경 ({MAJOR_MARK}, 운영자 확인 필요):")
                L += [f"  - {_cell(change_line(x), 400)}" for x in t.major_upgrades[:MD_NOTES]]
            if t.verify:
                L.append("- 운영자 확인:")
                L += [f"  - {_cell(x, 600)}" for x in guide_lines(t.verify)[:MD_NOTES]]
            if t.files:
                L.append(f"- files: {', '.join(t.files)}")
            if t.diff:
                d = t.diff if len(t.diff) <= MD_DIFF_CHARS else t.diff[:MD_DIFF_CHARS] + "\n… (full diff in errors.jsonl / changes.patch)"
                # diff 안의 ```가 마크다운 코드 블록을 닫지 않도록 비슷한 문자로 바꾼다
                L += ["", "```diff", d.replace("```", "ʼʼʼ"), "```"]
            L.append("")
    return "\n".join(L).rstrip() + "\n"
