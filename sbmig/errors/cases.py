"""v2.1 지식베이스(KB) 저장소 ``CaseStore``: 모든 실행의 오류 목록 + 케이스마다 폴더 하나.
흐름상 위치: 수정 루프가 라운드마다 저장된 스크립트를 찾고(``best`` / ``latest``) 새 스크립트를
저장하고(``add_script``) 채점 결과를 적는다(``credit``). 실행 끝에 ``record_run`` 이 오류 목록을 덧붙이고,
``sbmig kb reindex`` 는 ``recount`` 를 부른다. 바깥 창구는 ``sbmig.kb.FileKnowledgeBase``, 사람 창구는 ``sbmig
kb`` CLI. 케이스 폴더 이름은 케이스 ID(sha1(L2)[:10]): 매칭은 L2 문자열로 하고 폴더 이름은 키일 뿐이다(사람용
이름은 case.json 의 ``name`` 과 cases.md). 스크립트 상태: 저장하면 unverified → 만든 프로젝트가 아닌 곳에서
케이스를 해결하면 proven → 사람이 끄면 retired. 쓰기는 모두 KB 잠금 + 원자적 쓰기, 텍스트는 마스킹; 읽기는
잠그지 않고 깨진 파일에 예외를 내지 않는다(파일뿐, DB 금지: 불변조건 3). ``errors.jsonl`` 이 원본이고
case.json·cases.md·에러별 JSON 은 파생이다 — 기록을 손으로 맞추지 말고 ``kb reindex`` 로 다시 만든다.

The v2.1 knowledge base (docs/v2/PLAN-v2.1.md §3.4): every run's error list + one folder per case.

Layout under the KB directory (``kb.dir``, default ``kb-v2/``)::

    errors.jsonl                 every run's entries (ErrorEntry lines, the run's errors.jsonl appended at its end)
    cases.md                     the case table for people (rewritten by every record_run / reindex)
    cases/<case_id>/case.json    CaseInfo: case_id, l2, name, kind, projects, seen, resolved, scripts[…],
                                 last_analysis, last_solution, updated_at — plus the keys people / sbmig add:
                                 "pin": "block[:<reason>]" (never tried: `sbmig kb cases pin`), "last_result"
                                 (v2.1e: every script carries parent / reason — its augment lineage and why it was
                                 made — last_failure and runs[]: one ScriptRun per scored run, newest last, capped
                                 by errors.script_runs)
    cases/<case_id>/fix_v<N>.py  the stored fix scripts (exact bytes; an identical script is stored once)
    .lock                        the cross-process write lock (web server, fleet and `sbmig kb` share one KB)

Scripts: ``unverified`` when stored → ``proven`` once a reuse resolved the case in a project other than the one
that created it → ``retired`` by a person (``sbmig kb cases retire``; never selected). :meth:`CaseStore.best` =
the newest proven version, else the newest unverified one. A script whose code holds a secret value is stored
masked and retired at once (the masked code is not the code that ran; a secret never lands in the KB).

Every write holds the KB lock and is atomic; texts are masked (:mod:`sbmig.kb.redact`). Readers never lock and
never raise on a damaged file (it is skipped with a warning; ``sbmig kb validate`` lists it). File-based on purpose
(invariant 3).


"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from sbmig.core.models import CaseInfo, CaseScript, EditNote, ErrorEntry, ErrorKind, FixTry, ScriptRun, utcnow
from sbmig.core.stage import stage

from .common import Cleaner, atomic_write, atomic_write_bytes, case_id_for, first_line, note_text, posix
from .entries import change_line, guide_lines, probable_fixers, resolved_text, round_of, tries_text

log = logging.getLogger("sbmig.errors")

CASES_DIR = "cases"
ENTRIES_DIR = "errors"            # <kb>/cases/<case_id>/errors/<run_id>-<id>.json (one readable file per error)
OTHER_DIR = "_other"              # entries without a case (pom-verify, notes)
CASE_FILE = "case.json"
KB_ERRORS = "errors.jsonl"
CASES_MD = "cases.md"
SUCCESS_RESULTS = frozenset({"improved", "build_ok"})
FAILURE_RESULTS = frozenset({"no_change", "regressed", "reverted", "apply_failed", "blocked"})
TEXT_CHARS = 4000
REASON_CHARS = 600                # ScriptRun.reason / CaseScript.reason / last_failure (one line)
RUN_FILES = 50                    # ScriptRun.files kept
SCRIPT_RUNS = 30                  # errors.script_runs default (0 = no cap)
RUN_SOURCES = frozenset({"kb-script", "llm-new", "llm-augment"})   # tries whose stored script ran (reindex)
# 시도 note 중 '실패 이유' 가 아니라 '시도 설명' 인 부분 — 실행 기록의 이유(reason)에서 뺀다
# parts of a try's note that describe the try rather than why it failed (left out of a run's reason)
NOISE_NOTES = ("reused stored v", "escalation model requested", "merge-conflict hint:")
_NOT_RUN = ("stored (unverified), not run", "not run again")        # the try's script never ran
_RAN_THEN_EDITS = "did not apply → applying the answer's edits"      # it ran, failed; the edits went in instead
SECRET_HEADER = "# sbmig: retired at once — the code held a secret value (stored masked; not the code that ran)\n"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SCRIPT_RE = re.compile(r"^fix_v([1-9][0-9]{0,8})\.py$")


def valid_case_id(case_id: Optional[str]) -> bool:
    """디렉토리 이름 하나로 쓸 수 있는 케이스 ID 인가(실제로는 16진수; 경로나 ``..`` 는 절대 아님) — 손으로
    고친 case.json 이나 CLI 인자가 KB 밖을 가리키지 못하게.

    A case id usable as one directory name (hex in practice; never a path or ``..``)."""
    return bool(case_id) and bool(_CASE_ID_RE.match(case_id or "")) and ".." not in (case_id or "")


def case_name(message: str, l2: str) -> str:
    """케이스의 사람용 이름: 오류 메시지 첫 줄, 메시지에 L2 의 핵심어가 없으면 ``메시지: 핵심어``.

    ``package jakarta.xml.bind does not exist``; ``cannot find symbol: WebSecurityConfigurerAdapter`` when the
    message does not name the L2's key."""
    msg = first_line(message, 120)
    key = (l2 or "").split("|")[-1] if (l2 or "").count("|") >= 2 else ""
    if key and key not in msg:
        return f"{msg}: {key}"[:160] if msg else key[:160]
    return msg or (l2 or "")[:160]


def _short(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def scripts_text(case: CaseInfo, *, failures: bool = False) -> str:
    """저장 스크립트 요약: ``v1 proven 3/0, v2 unverified 0/1``(성공/실패).
    ``failures``(cases.md, v2.1e)면 버전마다 마지막 실패 이유를 붙인다.

    ``v1 proven 3/0, v2 unverified 0/1`` (successes / failures); ``failures`` (cases.md, v2.1e) adds each
    version's last failure: ``v1 proven 3/0; v2 unverified 0/1 — no_change: 3 of 3 targeted error(s) remained``."""
    def one(s: CaseScript) -> str:
        t = f"v{s.version} {s.status} {s.successes}/{s.failures}"
        return t + (f" — {_short(s.last_failure, 80)}" if failures and s.last_failure else "")
    return ("; " if failures else ", ").join(one(s) for s in case.scripts) or "-"


def run_reason(result: str, note: str = "", head: str = "") -> str:
    """ScriptRun.reason(v2.1e): 저장 스크립트 실행이 케이스를 못 푼 이유 한
    줄(improved / build_ok 면 ""). 호출자가 준 ``head`` 다음에, 시도 note 에서 '시도 설명'(재사용 배너,
    에스컬레이션 요청, 병합 힌트)을 뺀 부분; 남는 게 없으면 결과 이름.

    ScriptRun.reason (v2.1e): why a run of a stored script did not resolve its case, one line — "" for improved /
    build_ok. ``head`` = the caller's own first part (the fix loop: ``no_change: 3 of 3 targeted error(s) remained —
    …``), then the try's note without the parts that describe the try rather than its failure (the reuse banner, an
    escalation request, merge-conflict hints); the bare result when nothing is left."""
    if result in SUCCESS_RESULTS:
        return ""
    parts = [" ".join(p.split()) for p in (note or "").split("; ")]
    parts = [p for p in parts if p and not p.startswith(NOISE_NOTES)]
    if head and " ".join(head.split()):
        parts.insert(0, " ".join(head.split()))
    return "; ".join(dict.fromkeys(parts)) or (result or "")


def first_reason(reason: str, parent: int | None = None, limit: int = 160) -> str:
    """로그 줄용 이유의 첫 부분(보강본 이유 앞의 ``v1: `` 는 로그 줄이 이미 말하므로 뺀다).

    The first part of a reason for a log line (``v1: `` of an augment's reason dropped: the line names it)."""
    t = " ".join((reason or "").split())
    if parent and t.startswith(f"v{parent}: "):
        t = t[len(f"v{parent}: "):]
    for sep in ("; ", ". "):
        if sep in t:
            t = t.split(sep, 1)[0]
    return _short(t, limit)


def run_line(r: ScriptRun) -> str:
    """실행 기록(ScriptRun) 한 줄(``sbmig kb cases show``):
    실행·프로젝트·라운드·시도·결과 — 이유 [오류 수 전 → 남음, 새로 생김; 바꾼 파일].

    ``run r-1 demo r2 try 1: no_change — no_change: 3 of 3 targeted error(s) remained [errors 5 → 5, new 0;
    files: a/B.java]`` (``sbmig kb cases show``)."""
    s = f"run {r.run_id or '-'} {r.project or '-'} r{r.round} try {r.n}: {r.result or '-'}"
    if r.reason:
        s += f" — {r.reason}"
    facts = []
    if r.errors_before is not None or r.errors_left is not None:
        facts.append(f"errors {'?' if r.errors_before is None else r.errors_before} → "
                     f"{'?' if r.errors_left is None else r.errors_left}")
    if r.errors_new is not None:
        facts.append(f"new {r.errors_new}")
    tail = ", ".join(facts)
    if r.files:
        tail += ("; " if tail else "") + "files: " + ", ".join(r.files[:5]) + \
            (f", … (+{len(r.files) - 5})" if len(r.files) > 5 else "")
    return s + (f" [{tail}]" if tail else "")


def _sha(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _ts(at: Any) -> float:
    try:
        return float(at.timestamp())
    except Exception:  # noqa: BLE001 - a hand-edited record: oldest
        return 0.0


def _run_key(r: ScriptRun) -> tuple[str, int, int] | None:
    """실행 기록의 중복 판정 키 (run_id, round, n) — 시도
    번호 없이 적힌 기록은 None(다른 기록과 합치지 않는다).

    (run_id, round, n) — None for a run credited without a try number (never merged with another)."""
    return (r.run_id, int(r.round), int(r.n)) if r.n else None


def _resolved_elsewhere(s: CaseScript) -> bool:
    """버전이 만든 프로젝트가 아닌 곳에서 케이스를 해결한 적이 있는가(``activate`` 의 proven 판정, A18).
    만든 프로젝트 이름이 없는 옛 기록은 ``credit`` 처럼 실행 ID 로 본다.

    Did the version resolve the case in a project other than the one that made it (``activate``'s proven rule)?
    A record without ``project`` (older KBs) falls back to the run id, as ``credit`` does: a successful run of
    another run than ``created_by``, or two or more distinct projects (one of them is not the maker's)."""
    if s.project:
        return any(p and p != s.project for p in s.projects)
    # 옛 기록: 빈 project 와 비교하면 자기 프로젝트도 "다른 곳" 이 되므로 실행 ID·프로젝트 수로만 판단한다
    if s.created_by and any(r.result in SUCCESS_RESULTS and r.run_id and r.run_id != s.created_by for r in s.runs):
        return True
    return len({p for p in s.projects if p}) > 1


class CaseStore:
    """KB 저장소: ``<kb>/cases/<case_id>/``(case.json, fix_v<N>.py, errors/) + ``<kb>/errors.jsonl`` +
    ``<kb>/cases.md``. ``script_runs``(``errors.script_runs``)는 버전마다 남길 실행 기록 수(0 = 무제한).

    ``<kb>/cases/<case_id>/`` + ``<kb>/errors.jsonl`` + ``<kb>/cases.md``."""

    def __init__(self, kb_dir: Union[str, Path], cfg: Any = None, *, lock_timeout: Optional[float] = None,
                 script_runs: int | None = None):
        self.root = Path(kb_dir)
        self.cfg = cfg
        if lock_timeout is None:
            lock_timeout = float(getattr(getattr(cfg, "kb", None), "lock_timeout_seconds", 300) or 0)
        self.lock_timeout = lock_timeout
        if script_runs is None:
            script_runs = getattr(getattr(cfg, "errors", None), "script_runs", SCRIPT_RUNS)
        try:
            self.script_runs = max(0, int(script_runs if script_runs is not None else SCRIPT_RUNS))
        except (TypeError, ValueError):
            self.script_runs = SCRIPT_RUNS
        self.clean = Cleaner(cfg)
        self.last_reindex: dict[str, int] = {}         # v2.1e: what the last recount rebuilt (runs, hunks)

    # ------------------------------------------------------------------ layout
    @property
    def cases_dir(self) -> Path:
        """``<kb>/cases/``."""
        return self.root / CASES_DIR

    @property
    def errors_path(self) -> Path:
        """``<kb>/errors.jsonl`` — 모든 실행의 모든 오류 레코드(KB 의 원본)."""
        return self.root / KB_ERRORS

    @property
    def md_path(self) -> Path:
        """``<kb>/cases.md`` — 사람용 케이스 표(파생 뷰)."""
        return self.root / CASES_MD

    def lock(self):
        """프로세스 간 KB 잠금(``<kb>/.lock``, 재진입 가능). 모든 쓰기가 이 안에서 일어난다."""
        from sbmig.kb.lock import kb_lock

        return kb_lock(self.root, self.lock_timeout)

    def case_dir(self, case_id: str) -> Path:
        """케이스 폴더 경로. 케이스 ID 가 아니면 ValueError — ``..`` 같은 값으로 KB 밖에 쓰지 못하게."""
        if not valid_case_id(case_id):
            raise ValueError(f"not a case id: {case_id!r}")
        return self.cases_dir / case_id

    def case_ids(self) -> list[str]:
        """case.json 이 있는 케이스 폴더 이름들(정렬)."""
        if not self.cases_dir.is_dir():
            return []
        return sorted(p.name for p in self.cases_dir.iterdir() if p.is_dir() and (p / CASE_FILE).is_file())

    # ------------------------------------------------------------------ case.json
    def _read(self, case_id: str) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """(케이스, case.json 의 추가 키) — 없거나 못 읽거나 다른 케이스를 가리키면 경고 후 None(예외 없음).

        (case, extra keys of case.json) — None when there is no readable case file."""
        if not valid_case_id(case_id):
            return None
        path = self.cases_dir / case_id / CASE_FILE
        if not path.is_file():
            return None
        try:
            # utf-8-sig: 사람이 메모장으로 고쳐 BOM 이 붙은 case.json 도 읽는다
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            case = CaseInfo.model_validate(data)
        except (OSError, ValueError) as e:
            log.warning("kb: %s does not read (%s) — skipped (`sbmig kb validate`)", path, type(e).__name__)
            return None
        if case.case_id != case_id:
            log.warning("kb: %s names case %s — skipped", path, case.case_id)
            return None
        # CaseInfo 에 없는 키(pin, last_result, 사람이 넣은 것)는 따로 들고 있다가 저장 때 되돌려 넣는다
        extra = {k: v for k, v in data.items() if k not in CaseInfo.model_fields}
        return case, extra

    def _save(self, case: CaseInfo, extra: Optional[dict[str, Any]] = None) -> None:
        """case.json 을 원자적으로 다시 쓴다; 추가 키는 모델 필드를 덮지 않고 뒤에 붙인다."""
        data = case.model_dump(mode="json")
        data.update({k: v for k, v in (extra or {}).items() if k not in data})
        atomic_write(self.case_dir(case.case_id) / CASE_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def find(self, case_id: str) -> Optional[CaseInfo]:
        """케이스 정보(CaseInfo), 없거나 읽을 수 없으면 None."""
        hit = self._read(case_id)
        return hit[0] if hit else None

    def extra(self, case_id: str) -> dict[str, Any]:
        """case.json 의 CaseInfo 밖 키(``pin``, ``last_result``, 사람이 넣은 것).

        The keys of case.json beyond CaseInfo (``pin``, ``last_result``, anything a person added)."""
        hit = self._read(case_id)
        return dict(hit[1]) if hit else {}

    def pin_of(self, case_id: str) -> str:
        """사람이 고정했으면 ``block`` / ``block:<이유>``, 아니면
        ""(``pin`` 의 다른 값은 무시; ``sbmig kb validate`` 가 알려 준다).

        ``block`` / ``block:<reason>`` when a person pinned the case, else "" (anything else in ``pin`` is
        ignored here; ``sbmig kb validate`` names it)."""
        p = self.extra(case_id).get("pin")
        p = p.strip() if isinstance(p, str) else ""
        return p if p == "block" or p.startswith("block:") else ""

    def _case_or_new(self, case_id: str, l2: str = "", name: str = "",
                     kind: Optional[ErrorKind] = None) -> tuple[CaseInfo, dict[str, Any]]:
        """있는 케이스(비어 있는 l2·이름·종류만 채움) 또는 새 CaseInfo, 그리고 추가 키."""
        hit = self._read(case_id)
        if hit is not None:
            case, extra = hit
            if l2 and not case.l2:
                case.l2 = l2
            if name and not case.name:
                case.name = name
            if kind is not None and case.kind == ErrorKind.UNKNOWN:
                case.kind = kind
            return case, extra
        return CaseInfo(case_id=case_id, l2=l2, name=name or case_name("", l2),
                        kind=kind or ErrorKind.UNKNOWN), {}

    def set_pin(self, case_id: str, pin: str) -> CaseInfo:
        """케이스 고정(``block`` / ``block:<이유>``) 또는 해제(""). 필요하면 case.json 을 만든다. 고정된
        케이스는 스크립트도 LLM 도 시도하지 않고 인수인계 섹션으로 넘어간다.

        Pin (``block`` / ``block:<reason>``) or unpin (``""``) a case; creates the case file when needed."""
        pin = (pin or "").strip()
        if pin and not (pin == "block" or pin.startswith("block:")):
            raise ValueError(f"a pin is `block` or `block:<reason>`, not {pin!r}")
        with stage("kb"), self.lock():
            case, extra = self._case_or_new(case_id)
            if pin:
                extra["pin"] = self.clean(pin, 500)
            else:
                extra.pop("pin", None)
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("case %s %s", case_id, f"pinned: {extra['pin']}" if pin else "unpinned")
            self._write_md_locked()
        return case

    # ------------------------------------------------------------------ scripts
    def scripts(self, case_id: str) -> list[CaseScript]:
        """케이스의 저장된 스크립트들(오래된 것 먼저).

        The case's scripts, oldest first."""
        case = self.find(case_id)
        return sorted(case.scripts, key=lambda s: s.version) if case else []

    def code(self, case_id: str, version: int) -> Optional[str]:
        """``fix_v<N>.py`` 의 코드(파일 바이트 그대로 UTF-8), 없거나 못 읽으면 None."""
        if not valid_case_id(case_id):
            return None
        path = self.cases_dir / case_id / f"fix_v{int(version)}.py"
        try:
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def best(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """먼저 돌릴 스크립트: 가장 새 proven 버전, 없으면 가장 새 unverified
        버전. retired 와 파일이 없는 버전은 고르지 않는다. None = 저장된 것 없음.

        The script to run first: the newest proven version, else the newest unverified one; retired never (nor
        a version whose file is missing). None = nothing stored."""
        ranked = sorted((s for s in self.scripts(case_id) if s.status != "retired"),
                        key=lambda s: (s.status == "proven", s.version), reverse=True)
        for s in ranked:
            code = self.code(case_id, s.version)
            if code:
                return s, code
        return None

    def latest(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """retired 가 아닌 가장 새 버전 — 보강(augment) 요청의 바탕.

        The newest non-retired version (the base of an augment request)."""
        for s in sorted(self.scripts(case_id), key=lambda s: s.version, reverse=True):
            code = self.code(case_id, s.version) if s.status != "retired" else None
            if code:
                return s, code
        return None

    def add_script(self, case_id: str, l2: str, code: str, *, source: str = "llm-new", run_id: str = "",
                   project: str = "", analysis: str = "", solution: str = "", name: str = "",
                   kind: Optional[ErrorKind] = None, parent: int | None = None, reason: str = "") -> CaseScript:
        """``code`` 를 케이스의 다음 버전(unverified)으로 저장한다; 같은 코드(sha1 비교)가 이미 있으면 그
        버전을 그대로 돌려준다. v2.1e: ``parent`` = 보강이 이은 버전, ``reason`` = 이 버전을 만든 이유(부모의
        실패 문장). 비밀값이 든 코드는 마스킹해 저장하고 바로 retired.

        Store ``code`` as the case's next version (``unverified``); identical code returns the stored version
        (unchanged). v2.1e: ``parent`` = the version an augment extends, ``reason`` = why this version was made (the
        parent's failure in words, ``v1: no_change: …``)."""
        if not (code or "").strip():
            raise ValueError("empty script")
        try:
            parent = int(parent) if parent else None
        except (TypeError, ValueError):
            parent = None
        why = self._reason(reason)
        with stage("kb"), self.lock():
            case, extra = self._case_or_new(case_id, l2, name, kind)
            masked = self.clean.red.redact(code) or ""
            # 비밀값이 든 코드는 마스킹본만 저장하고 바로 retired:
            # 마스킹된 코드는 실제로 돈 코드가 아니라 재사용하면 안 된다
            secret = masked != code
            stored = SECRET_HEADER + masked if secret else code
            for s in case.scripts:
                if _sha(self.code(case_id, s.version) or "") == _sha(stored):
                    log.info("cases/%s/fix_v%d.py: the same script is already stored", case_id, s.version)
                    return s
            v = self._next_version(case)
            atomic_write_bytes(self.case_dir(case_id) / f"fix_v{v}.py", stored.encode("utf-8"))
            s = CaseScript(version=v, file=f"fix_v{v}.py", status="retired" if secret else "unverified",
                           source=source, created_by=run_id, project=project, parent=parent, reason=why)
            case.scripts.append(s)
            if analysis:
                case.last_analysis = self.clean(analysis, TEXT_CHARS)
            if solution:
                case.last_solution = self.clean(solution, TEXT_CHARS)
            case.updated_at = utcnow()
            self._save(case, extra)
            if secret:
                log.warning("cases/%s/fix_v%d.py: the script held a secret value — stored masked and retired",
                            case_id, v)
            else:
                lineage = ""
                if parent:
                    first = first_reason(why, parent)
                    lineage = f" (v{parent} 보강: {first})" if first else f" (v{parent} 보강)"
                log.info("스크립트 v%d 저장 (unverified, %s) → cases/%s/fix_v%d.py%s", v, source, case_id, v, lineage)
        return s                                          # (cases.md: rewritten by record_run at the end of the run)

    def _disk_versions(self, case_id: str) -> list[int]:
        """케이스 폴더에 실제로 있는 ``fix_v<N>.py`` 의 번호들(case.json 목록과 무관)."""
        d = self.case_dir(case_id)
        return [int(m.group(1)) for p in (d.glob("fix_v*.py") if d.is_dir() else [])
                if (m := _SCRIPT_RE.match(p.name))]

    def _next_version(self, case: CaseInfo) -> int:
        """다음 스크립트 번호: case.json 의 버전과 폴더의 ``fix_v*.py`` 중 가장 큰 것 + 1(``add_script`` ·
        ``merge_from`` 공통). 번호를 재사용하지 않아 목록에 없는 파일을 덮어쓰지 않는다.

        The next script number: 1 + the highest of case.json's versions and the folder's ``fix_v*.py`` files
        (``add_script`` and ``merge_from``) — a number is never reused, so an unlisted file is never overwritten."""
        # 번호는 재사용하지 않는다: case.json 에 없는 fix_v*.py
        # 파일까지 세어 남의 파일을 덮어쓰지 않게 한다
        return 1 + max([s.version for s in case.scripts] + self._disk_versions(case.case_id), default=0)

    def _reason(self, text: str | None) -> str:
        """저장용 이유: 마스킹, 한 줄, 최대 REASON_CHARS 자.

        A reason as stored: masked, one line, at most REASON_CHARS."""
        t = " ".join(self.clean(text or "").split())
        return t if len(t) <= REASON_CHARS else t[: REASON_CHARS - 1] + "…"

    def _run_files(self, files: Iterable[Any]) -> list[str]:
        out = [posix(self.clean(str(f))) for f in (files or ()) if f]
        return list(dict.fromkeys(f for f in out if f))[:RUN_FILES]

    def _cap_runs(self, runs: list[ScriptRun]) -> list[ScriptRun]:
        return runs[-self.script_runs:] if self.script_runs and len(runs) > self.script_runs else runs

    def _add_run(self, s: CaseScript, run: ScriptRun) -> None:
        """``run`` 을 끝에 덧붙인다; 같은 (run_id, round, n) 기록은 바꾼다 — 판정 불가(inconclusive) 시도는
        되돌림 재빌드가 채점한 뒤 다시 크레딧되기 때문.

        Append ``run`` (newest last); a run with the same (run_id, round, n) is replaced — an inconclusive try
        is credited again after the revert rebuild scored it."""
        key = _run_key(run)
        runs = [r for r in s.runs if key is None or _run_key(r) != key]
        s.runs = self._cap_runs(runs + [run])

    def credit(self, case_id: str, version: int, result: str, project: str = "", run_id: str = "", *,
               reason: str = "", round: int = 0, n: int = 0, errors_before: int | None = None,
               errors_left: int | None = None, errors_new: int | None = None, files: Iterable[Any] = ()
               ) -> Optional[CaseScript]:
        """버전 N 실행의 채점 결과를 적는다: improved / build_ok → 성공 +1(프로젝트 추가; 만든 프로젝트가 아닌
        곳이면 proven), no_change / regressed / reverted / apply_failed / blocked → 실패 +1, partial /
        inconclusive 는 ``last_result`` 만, pending 은 무시. v2.1e: 크레딧마다 ScriptRun(라운드·시도·이유·오류
        수·파일)을 남기고 실패면 ``last_failure`` 를 쓴다.

        The scored result of a run of version N: improved / build_ok → successes +1 (project added; ``proven``
        once that is a project other than the one that created it); no_change / regressed / reverted /
        apply_failed / blocked → failures +1; anything else (partial, inconclusive) only sets ``last_result``;
        pending is ignored. v2.1e: every credit appends a ``ScriptRun`` (round / try ``n``, the reason in words,
        error counts, files; the same run_id + round + n replaces the earlier record; ``errors.script_runs`` kept)
        and a failure sets ``last_failure``."""
        if not result or result == "pending":
            return None
        why = self._reason(reason)
        with stage("kb"), self.lock():
            hit = self._read(case_id)
            if hit is None:
                return None
            case, extra = hit
            s = next((x for x in case.scripts if x.version == int(version)), None)
            if s is None:
                log.warning("cases/%s: no script v%s to credit", case_id, version)
                return None
            s.last_result = result
            if result in SUCCESS_RESULTS:
                s.successes += 1
                if project and project not in s.projects:
                    s.projects.append(project)
                # proven = 다른 프로젝트에서도 통함. 만든 프로젝트 이름이
                # 없는 옛 기록은 실행 ID 가 다르면 다른 곳으로 본다
                other = (project and s.project and project != s.project) or \
                    (not s.project and run_id and s.created_by and run_id != s.created_by)
                if s.status == "unverified" and other:
                    s.status = "proven"
                    log.info("cases/%s/fix_v%d.py: proven (resolved the case in %s)", case_id, s.version, project)
            elif result in FAILURE_RESULTS:
                s.failures += 1
                s.last_failure = why or result
            self._add_run(s, ScriptRun(run_id=run_id or "", project=project or "", round=int(round or 0),
                                       n=int(n or 0), result=result, reason=why, errors_before=errors_before,
                                       errors_left=errors_left, errors_new=errors_new, files=self._run_files(files)))
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s (성공 %d / 실패 %d)%s", case_id, s.version, result, s.successes,
                     s.failures, f" — {_short(why, 300)}" if why else "")
        return s

    def _set_status(self, case_id: str, version: int, retire: bool) -> CaseScript:
        """retire / activate 의 공통 부분: 잠금 안에서 상태를
        바꾸고 cases.md 를 다시 쓴다(없으면 KeyError)."""
        with stage("kb"), self.lock():
            hit = self._read(case_id)
            if hit is None:
                raise KeyError(f"no case {case_id}")
            case, extra = hit
            s = next((x for x in case.scripts if x.version == int(version)), None)
            if s is None:
                raise KeyError(f"case {case_id} has no script v{version}")
            if retire:
                s.status = "retired"
            else:
                s.status = "proven" if _resolved_elsewhere(s) else "unverified"
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s", case_id, s.version, s.status)
            self._write_md_locked()
        return s

    def retire(self, case_id: str, version: int) -> CaseScript:
        """버전 N 을 다시는 고르지 않게 한다(사람의 결정, ``sbmig kb cases retire``)."""
        return self._set_status(case_id, version, True)

    def activate(self, case_id: str, version: int) -> CaseScript:
        """retire 되돌리기: 다른 프로젝트에서 케이스를 해결한 적 있으면 proven, 아니면 unverified.

        Undo a retire: proven when it resolved the case in another project, else unverified."""
        return self._set_status(case_id, version, False)

    def describe(self, case_id: str) -> str:
        """KB 조회 결과를 로그 한 구절로: 저장 스크립트 없음 / 저장 스크립트 v2(상태, 성공·실패, 프로젝트 수)
        / pin / 케이스는 있으나 쓸 스크립트 없음.

        The KB lookup result as one log phrase: ``저장된 스크립트 없음 (처음 보는 케이스)``, ``저장된 스크립트 v2 (proven,
        성공 3 / 실패 0, 프로젝트 2)``, ``pin block: <reason>``, ``케이스는 있으나 쓸 스크립트 없음 (v1 retired)``."""
        case = self.find(case_id)
        if case is None:
            return "저장된 스크립트 없음 (처음 보는 케이스)"
        pin = self.pin_of(case_id)
        if pin:
            return f"pin {pin.replace(':', ': ', 1)}"
        best = self.best(case_id)
        if best is None:
            return ("케이스는 있으나 쓸 스크립트 없음 (" + scripts_text(case) + ")") if case.scripts else \
                f"저장된 스크립트 없음 (케이스는 {len(case.projects)}개 프로젝트에서 봄)"
        s = best[0]
        return (f"저장된 스크립트 v{s.version} ({s.status}, 성공 {s.successes} / 실패 {s.failures}, "
                f"프로젝트 {len(s.projects)})")

    # ------------------------------------------------------------------ errors.jsonl + cases
    def _known_ids(self) -> set[str]:
        """KB errors.jsonl 에 이미 있는 항목 ID 들(찢긴 줄 뒤에 붙은 레코드의
        ID 포함, ``line_ids``) — 같은 항목을 두 번 적지 않으려고."""
        from sbmig.kb.jsonl import line_ids

        out: set[str] = set()
        if not self.errors_path.is_file():
            return out
        with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    out.update(line_ids(line))
        return out

    def _clean_note(self, n: EditNote) -> EditNote:
        c = self.clean
        hunks = [h.model_copy(update={"before": c(h.before), "after": c(h.after)}) for h in n.hunks]
        return n.model_copy(update={"where": c(n.where), "change": c(n.change), "hunks": hunks})

    def _clean_entry(self, e: ErrorEntry) -> ErrorEntry:
        """KB 에 넣기 전 항목의 모든 텍스트(메시지·원문·시도
        원인/해결·diff·수정 기록·변경 조각)를 한 번 더 마스킹한다."""
        c = self.clean
        tries = [t.model_copy(update={"analysis": c(t.analysis), "solution": c(t.solution),
                                      "no_script_reason": c(t.no_script_reason), "diff": c(t.diff),
                                      "note": c(t.note), "edits": [self._clean_note(n) for n in t.edits]})
                 for t in e.tries]
        return e.model_copy(update={"message": c(e.message), "raw": c(e.raw), "note": c(e.note), "tries": tries})

    def record_run(self, entries: Iterable[ErrorEntry]) -> int:
        """실행이 끝난 항목들을 ``<kb>/errors.jsonl`` 에
        덧붙이고(이미 있는 ID 는 건너뜀), 속한 케이스마다 seen / resolved / 프로젝트 / 마지막 원인·해결 /
        마지막 결과를 갱신하고(L2 가 있으면 케이스 폴더를 만든다),
        에러별 JSON 과 ``cases.md`` 를 쓴다. 덧붙인 줄 수를 돌려준다.

        Append a run's finished entries to ``<kb>/errors.jsonl`` (ids already stored are skipped), update each
        case they belong to (seen / resolved / projects / last analysis + solution / last result — a case folder is
        made for entries with an L2), rewrite ``cases.md``. Returns the number of lines appended."""
        from sbmig.kb.jsonl import append_lines

        entries = [self._clean_entry(e) for e in entries]
        if not entries:
            return 0
        with stage("kb"), self.lock():
            known = self._known_ids()
            fresh = [e for e in entries if e.id not in known]
            n = append_lines(self.errors_path, [e.model_dump_json() for e in fresh])
            by_case: dict[str, list[ErrorEntry]] = {}
            # note / pom-verify 는 케이스 통계에 넣지 않는다; L2 도
            # case.json 도 없는 케이스(L1 폴백 ID)는 폴더를 만들지 않는다
            for e in fresh:
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id) \
                        and (e.l2 or (self.cases_dir / e.case_id / CASE_FILE).is_file()):
                    by_case.setdefault(e.case_id, []).append(e)
            for cid, es in by_case.items():
                case, extra = self._case_or_new(cid, es[0].l2, case_name(es[0].message, es[0].l2), es[0].kind)
                self._apply(case, extra, es)
                self._save(case, extra)
            self._write_entry_files(fresh)
            self._write_md_locked()
            log.info("errors.jsonl +%d (%d already stored), %d case(s) updated, cases.md rewritten", n,
                     len(entries) - len(fresh), len(by_case))
        return n

    @property
    def json_files(self) -> bool:
        """``errors.json_files``: 오류마다 읽기 쉬운 JSON 파일을 케이스 폴더에 쓸지(기본 true)."""
        return bool(getattr(getattr(self.cfg, "errors", None), "json_files", True))

    def entry_file(self, e: ErrorEntry) -> Path:
        """에러별 JSON 경로 ``<kb>/cases/<case_id>/errors/<run_id>-<id>.json`` — 케이스 없는
        항목(pom-verify, 메모)은 ``cases/_other/errors/``.

        ``<kb>/cases/<case_id>/errors/<run_id>-<id>.json`` — entries without a case (pom-verify, notes) go under
        ``cases/_other/errors/``."""
        folder = e.case_id if e.case_id and valid_case_id(e.case_id) else OTHER_DIR
        return self.cases_dir / folder / ENTRIES_DIR / f"{e.run_id or 'run'}-{e.id}.json"

    def _write_entry_files(self, entries: list[ErrorEntry]) -> int:
        """v2.1c: 현업이 저장된 오류를 둘러볼 수 있게 오류마다
        들여쓴 JSON 파일 하나(errors.jsonl 줄과 같은 내용).

        v2.1c (2026-10-27, the business team wants to browse the stored errors): one indented JSON file per
        error beside its case — the same record as the errors.jsonl line, readable in any editor."""
        if not self.json_files:
            return 0
        n = 0
        for e in entries:
            try:
                atomic_write(self.entry_file(e), json.dumps(e.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
                n += 1
            except OSError as ex:
                log.warning("could not write %s: %s", self.entry_file(e), ex)
        return n

    def entry_files(self, case_id: str) -> list[Path]:
        """케이스 폴더의 에러별 JSON 파일들(정렬)."""
        return sorted((self.cases_dir / case_id / ENTRIES_DIR).glob("*.json"))

    @staticmethod
    def _apply(case: CaseInfo, extra: dict[str, Any], es: list[ErrorEntry]) -> None:
        """한 실행의 그 케이스 항목들을 케이스에 셈한다(seen /
        resolved / 프로젝트 / 마지막 원인·해결 / ``last_result``).

        Count one run's entries of the case into it."""
        case.seen += len(es)
        case.resolved += sum(1 for e in es if e.status == "resolved")
        for e in es:
            if e.project and e.project not in case.projects:
                case.projects.append(e.project)
        tries = [t for e in es for t in e.tries]
        with_text = [t for t in tries if t.analysis or t.solution]
        if with_text:
            t = max(with_text, key=lambda t: (t.round, t.n))
            case.last_analysis = t.analysis or case.last_analysis
            case.last_solution = t.solution or case.last_solution
        status: dict[str, int] = {}
        for e in es:
            status[e.status] = status.get(e.status, 0) + 1
        last = es[-1]
        # v2.1f: build 로 해결된 오류면 '해결한 시도' 까지 last_result 에 보인다(cases.md 에 그대로 나옴)
        how = next((resolved_text(e) for e in es if e.status == "resolved" and e.resolved), "")
        extra["last_result"] = (", ".join(f"{k} {v}" for k, v in sorted(status.items())) + (f" ({how})" if how else "")
                                + f" — {last.project or '-'} {last.run_id or ''}".rstrip())
        case.updated_at = utcnow()

    def iter_errors(self, *, run: str = "", case: str = "", status: str = "", project: str = "",
                    entry: str = "") -> Iterator[ErrorEntry]:
        """``<kb>/errors.jsonl`` 항목들(파일 순서, 못 읽는 줄은 건너뜀)을 걸러서: ``entry`` = ID 앞부분,
        ``case`` = 케이스 ID 앞부분, ``run`` / ``project`` 는 정확히 일치.

        ``<kb>/errors.jsonl`` entries (file order; unreadable lines skipped), filtered (``entry`` = id prefix,
        ``case`` = case id prefix, ``run`` / ``project`` exact)."""
        from sbmig.kb.jsonl import parse_record

        if not self.errors_path.is_file():
            return
        with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                e = parse_record(line, ErrorEntry)
                if e is None:
                    continue
                if (run and e.run_id != run) or (case and not e.case_id.startswith(case)) or \
                        (status and e.status != status) or (project and e.project != project) or \
                        (entry and not e.id.startswith(entry)):
                    continue
                yield e

    def count_errors(self) -> int:
        """errors.jsonl 의 레코드 수(비어 있지 않은 줄)."""
        if not self.errors_path.is_file():
            return 0
        with open(self.errors_path, "rb") as fh:
            return sum(1 for line in fh if line.strip())

    def recount(self) -> int:
        """재색인(``sbmig kb reindex``): ``errors.jsonl`` 로부터 케이스마다 seen / resolved /
        프로젝트 / 마지막 텍스트를 다시 센다(스크립트 성공·실패 수와 사람이 넣은 키는 그대로; errors.jsonl
        에만 있는 L2 의 케이스는 만든다). 케이스 수를 돌려준다. v2.1e: 저장 버전마다 실행 기록(runs)·보강
        계보(parent / reason)·마지막 실패를 다시 만들고, 변경 조각 없이 저장된 옛 시도에 수정 전/후 코드를
        채운다; v2.1f: build 로 해결된 옛 항목에 '해결한 시도'(resolved_by)도 채운다. errors.jsonl 과 그
        항목들의 에러별 JSON 은 한 번만 다시 쓴다.

        Rebuild every case's seen / resolved / projects / last texts from ``<kb>/errors.jsonl`` (script counts and
        people's keys untouched; cases for L2s that only errors.jsonl knows are made). Returns the cases.

        v2.1e: every stored version's ``runs`` are rebuilt from the entries' tries (merged with the runs the store
        already has: those keep their error counts; deduplicated by run_id + round + try n), ``parent`` / ``reason``
        filled where an ``llm-augment`` try shows them, ``last_failure`` = the newest failed run's reason; tries with
        a diff whose 수정 notes lack ``hunks`` get them (``sbmig.script.notes.attach_hunks``) — errors.jsonl and those
        entries' JSON files are rewritten once. v2.1f: build-resolved entries without ``resolved_by`` get the
        probable fixer (also when ``attach_hunks`` is not importable). One INFO line says how many of each
        (``last_reindex["hunks"]`` / ``["resolved_by"]``, counted separately, fresh every call)."""
        with stage("kb"), self.lock():
            # 변경 조각·해결한 시도 채우기는 부가 기능: 실패해도 재색인(카운트)은 계속한다. 두 숫자는 따로 세고
            # 매 recount 마다 0 에서 시작한다(A15 / A16: 같은 store 로 다시 불러도 지난 숫자를 보고하지 않게)
            hunked, fixers = 0, 0
            try:
                hunked, fixers = self._backfill_entries()
            except Exception as e:  # noqa: BLE001 - the backfill is extra; the recount goes on
                log.warning("kb reindex: hunk / resolved_by backfill failed: %s: %s", type(e).__name__, e)
            groups: dict[str, list[ErrorEntry]] = {}
            for e in self.iter_errors():
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id):
                    groups.setdefault(e.case_id, []).append(e)
            ids = set(self.case_ids()) | {cid for cid, es in groups.items() if any(x.l2 for x in es)}
            rebuilt = 0
            for cid in sorted(ids):
                es = groups.get(cid, [])
                first = es[0] if es else None
                case, extra = self._case_or_new(cid, first.l2 if first else "",
                                                case_name(first.message, first.l2) if first else "",
                                                first.kind if first else None)
                # 처음부터 다시 센다: 실행별로 묶어 _apply 하면
                # record_run 이 실행 끝마다 센 것과 같은 결과가 된다
                case.seen, case.resolved, case.projects = 0, 0, []
                runs: dict[str, list[ErrorEntry]] = {}
                for e in es:
                    runs.setdefault(e.run_id, []).append(e)
                for run_es in runs.values():
                    self._apply(case, extra, run_es)
                try:
                    rebuilt += self._rebuild_runs(case, es)
                except Exception as e:  # noqa: BLE001 - the run history is extra; the counts are saved anyway
                    log.warning("kb reindex: runs of case %s not rebuilt: %s: %s", cid, type(e).__name__, e)
                self._save(case, extra)
            # 에러별 JSON 이 생기기 전에 저장된 항목(또는 사람이 지운 파일)을 errors.jsonl 로부터 다시 만든다
            # the readable per-error files for entries stored before they existed (or deleted by hand)
            missing = [e for e in self.iter_errors() if not self.entry_file(e).is_file()]
            if missing:
                log.info("writing %d per-error JSON file(s) missing under cases/*/errors/", len(missing))
                self._write_entry_files(missing)
            self._write_md_locked()
            self.last_reindex = {"cases": len(ids), "runs": rebuilt, "hunks": hunked, "resolved_by": fixers}
            log.info("reindex: %d script run record(s) rebuilt, %d entr(ies) given as-is/to-be hunks, %d build-resolved "
                     "entr(ies) given a probable fixer", rebuilt, hunked, fixers)
            return len(ids)

    # ------------------------------------------------------------------ v2.1e: runs + hunks from errors.jsonl
    @staticmethod
    def _ran(t: FixTry) -> str | None:
        """그 시도의 저장 스크립트가 실제로 돌았을 때 받은 결과 — 돌지 않았으면
        None(적용된 수정 옆에 저장만 함, 같은 코드라 다시 안 돌림, 채점 대기).

        The result the try's stored script was credited with when it ran — None when it did not run (stored
        next to applied edits, the same code again, pending)."""
        if not t.script_version or t.source not in RUN_SOURCES or t.result == "pending":
            return None
        note = t.note or ""
        # 스크립트가 돌다 실패해 답변의 수정 목록이 대신 들어간 경우: 그 스크립트 실행은 apply_failed
        if t.applied == "edits":
            return "apply_failed" if _RAN_THEN_EDITS in note else None
        if any(x in note for x in _NOT_RUN):
            return None
        return t.result

    def _rebuild_runs(self, case: CaseInfo, es: list[ErrorEntry]) -> int:
        """케이스의 시도들 → 저장 버전마다 실행 기록(저장소의 기록과 병합),
        parent / reason, last_failure. errors.jsonl 에서 만든 기록 수를 돌려준다.

        The case's tries → each stored version's runs (merged), parent / reason, last_failure. Returns the run
        records derived from errors.jsonl for versions the case has."""
        derived: dict[int, dict[tuple[str, int, int], ScriptRun]] = {}
        augments: dict[int, list[tuple[str, int, int]]] = {}
        for e in es:
            for t in e.tries:
                result = self._ran(t)
                if result is None:
                    continue
                v, key = int(t.script_version), (e.run_id, int(t.round), int(t.n))
                per = derived.setdefault(v, {})
                if key in per:                            # several entries of the case share the try: one run
                    continue
                # no_change 는 채점이 이유를 따로 적지 않으므로 결과 이름을 이유 앞에 둔다
                # a no_change note never says why (the scoring adds none): the result leads
                head = result if result == "no_change" and result not in (t.note or "") else ""
                per[key] = ScriptRun(run_id=e.run_id, project=e.project, round=int(t.round), n=int(t.n),
                                     result=result, reason=self._reason(run_reason(result, t.note, head)),
                                     files=self._run_files(t.files), at=e.at)
                if t.source == "llm-augment":
                    augments.setdefault(v, []).append(key)
        n = 0
        merged: dict[int, dict[tuple[str, int, int], ScriptRun]] = {}
        for s in case.scripts:
            mine = derived.get(s.version, {})
            n += len(mine)
            keyed = {k: r for r in s.runs if (k := _run_key(r)) is not None}
            loose = [r for r in s.runs if _run_key(r) is None]
            for key, r in mine.items():
                have = keyed.get(key)
                # 저장소가 직접 남긴 기록이 이긴다(오류 수를 가진 건 그쪽) — 재색인 기록은 빈 칸만 채운다
                if have is None:                          # the store's own record wins (it has the counts)
                    keyed[key] = r
                    continue
                fill = {f: getattr(r, f) for f in ("project", "reason", "files") if not getattr(have, f)
                        and getattr(r, f) and not (f == "reason" and have.result in SUCCESS_RESULTS)}
                if fill:
                    keyed[key] = have.model_copy(update=fill)
            merged[s.version] = keyed
            runs = sorted(loose + list(keyed.values()), key=lambda r: (_ts(r.at), r.round, r.n))
            failed = [r for r in runs if r.result in FAILURE_RESULTS]
            if failed:
                s.last_failure = failed[-1].reason or failed[-1].result
            s.runs = self._cap_runs(runs)
        for s in case.scripts:
            if s.parent is None and s.version in augments:
                self._lineage(s, augments[s.version], merged)
        return n

    def _lineage(self, s: CaseScript, keys: list[tuple[str, int, int]],
                 runs: dict[int, dict[tuple[str, int, int], ScriptRun]]) -> None:
        """보강본의 부모 추정(최선): 그 버전을 만든 시도보다 앞서 같은 실행에서 돈 더 낮은 버전 중 가장 높은
        것(버전을 만든 실행 먼저 봄); 이유 = 그 실행의 이유.

        An augment's parent (best effort): the highest lower version that ran in the same run before the try
        that made it (the run that created the version first); reason = that run's reason."""
        keys = sorted(keys, key=lambda k: (k[0] != s.created_by, k[1], k[2]))
        for run_id, rnd, n in keys:
            before = [(v, k, r) for v, per in runs.items() if v < s.version for k, r in per.items()
                      if k[0] == run_id and (k[1], k[2]) < (rnd, n)]
            if not before:
                continue
            v, _k, r = max(before, key=lambda x: (x[0], x[1][1], x[1][2]))
            s.parent = v
            if not s.reason:
                s.reason = self._reason(f"v{v}: {r.reason or r.result}")
            return

    @property
    def snippet_lines(self) -> int:
        """``errors.snippet_lines``: 변경 조각 한쪽(수정 전/후)에 남길 줄 수(0 = 전부).

        ``errors.snippet_lines``: lines kept per side of a hunk (0 = the whole hunk)."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _hunked(self, e: ErrorEntry, attach: Any, max_lines: int = 0) -> ErrorEntry | None:
        """diff 는 있는데 수정 기록에 변경 조각이 없는 시도들에 조각을 채운 ``e``; 바뀐 게 없으면 None.

        ``e`` with hunks on the tries whose diff notes lack them; None when nothing changed."""
        changed, tries = False, []
        for t in e.tries:
            if t.diff and t.edits and any(not n.hunks for n in t.edits):
                try:
                    got = attach(list(t.edits), t.diff, max_lines=max_lines)
                    notes = [self._clean_note(x if isinstance(x, EditNote) else EditNote.model_validate(x))
                             for x in (got or [])]
                except Exception as ex:  # noqa: BLE001 - a broken helper leaves the try as it is
                    log.debug("attach_hunks failed for %s try %d: %s: %s", e.id, t.n, type(ex).__name__, ex)
                    notes = []
                if notes and [x.model_dump() for x in notes] != [x.model_dump() for x in t.edits]:
                    t = t.model_copy(update={"edits": notes})
                    changed = True
            tries.append(t)
        return e.model_copy(update={"tries": tries}) if changed else None

    @staticmethod
    def _fixer_attributed(e: ErrorEntry, tries: list[tuple[str, FixTry]]) -> Optional[ErrorEntry]:
        """v2.1f 재색인 보충: 시도도 ``resolved_by`` 도 없이 ``build`` 로 해결된 항목
        → 같은 실행에서 그 ``seen`` 라운드 이후 적용된 시도 중 그 파일·위 pom 을 바꾼 것(match ``reindex``).
        재구성(pom-verify) 항목은 대상이 아니다. 없으면 None.

        v2.1f: an entry resolved by ``build`` without a try and without ``resolved_by`` → the run's applied tries
        from its ``seen`` round on that changed its file / a pom above it (match ``reindex``); None when nothing."""
        if e.resolved != "build" or e.resolved_by or e.tries or not e.case_id or e.seen == "restructure":
            return None
        # 오류가 처음 보인 라운드보다 앞선 시도는 그 오류를 고쳤을 리 없다
        start = round_of(e.seen)
        later = [(c, t) for c, t in tries if int(t.round) >= start]
        by = probable_fixers(e, later, match="reindex")
        if not by:
            return None
        by.sort(key=lambda rb: (rb.round, rb.n, rb.match))          # the earliest round after `seen` first
        return e.model_copy(update={"resolved_by": by})

    def _backfill_entries(self) -> tuple[int, int]:
        """v2.1e 이전에 저장된 errors.jsonl 항목에 diff 로부터 변경 조각(수정 전/후)을
        채운다(``attach_hunks``, 한쪽 ``errors.snippet_lines`` 줄); 같은 순회에서 v2.1f '해결한
        시도'(``_fixer_attributed``)도 채운다 — ``attach_hunks`` 를 못 불러와도 이쪽은 돈다(A16). errors.jsonl 은
        원자적으로 다시 쓰되 안 바뀐 줄·못 읽는 줄은 바이트 그대로 둔다. 호출자가 잠금을 잡고 있어야 한다.
        (변경 조각을 받은 항목 수, 해결한 시도를 받은 항목 수)를 따로 돌려준다(A15).

        errors.jsonl entries stored before v2.1e: as-is / to-be hunks from their diffs (``attach_hunks``,
        ``errors.snippet_lines`` per side; skipped when it is not there); build-resolved entries of v2.1f: the
        probable fixer (``_fixer_attributed``, done also without ``attach_hunks``). Rewrites errors.jsonl
        atomically (other lines byte for byte, a line that does not read kept as it is) and the changed entries'
        JSON files. Caller holds the lock. Returns (entries given hunks, entries given ``resolved_by``)."""
        # 지연 import: sbmig.script 가 없거나 깨져도 변경 조각만 건너뛰고 해결한 시도 채우기·재색인은 계속한다
        try:
            from sbmig.script.notes import attach_hunks
        except Exception as ex:  # noqa: BLE001 - ImportError / not there yet: no hunks, the attribution goes on
            log.debug("kb reindex: attach_hunks not importable (%s: %s) — no hunk backfill", type(ex).__name__, ex)
            attach_hunks = None
        if not callable(attach_hunks):
            attach_hunks = None
        if not self.errors_path.is_file():
            return 0, 0
        try:
            raw = self.errors_path.read_bytes()
        except OSError as ex:
            log.warning("kb reindex: %s does not read (%s) — no hunk / resolved_by backfill", self.errors_path, ex)
            return 0, 0
        out: list[bytes] = []
        changed: list[ErrorEntry] = []
        bad, cut = 0, self.snippet_lines
        parsed: list[tuple[bytes, Optional[ErrorEntry]]] = []
        tries_by_run: dict[str, dict[tuple[str, int, int], tuple[str, FixTry]]] = {}
        # 바이트 단위로 다룬다: 안 바뀐 줄은 다시 직렬화하지 않고 그대로 둬 못 읽는 줄까지 보존한다; 해결한
        # 시도 추정용으로 실행별 모든 시도를 (케이스, 라운드, n) 당 하나씩 모은다
        for b in raw.split(b"\n"):
            e = None
            if b.strip():
                try:
                    e = ErrorEntry.model_validate_json(b.decode("utf-8").strip())
                except (UnicodeDecodeError, ValueError):
                    bad += 1
            parsed.append((b, e))
            if e is not None:
                per = tries_by_run.setdefault(e.run_id, {})
                for t in e.tries:
                    per.setdefault((e.case_id, int(t.round), int(t.n)), (e.case_id, t))
        hunks, fixers = 0, 0
        for b, e in parsed:
            if e is None:
                out.append(b)
                continue
            new = self._hunked(e, attach_hunks, cut) if attach_hunks is not None else None
            if new is not None:
                hunks += 1
            # 한 항목의 해결한 시도 추정이 깨져도 그 항목만 건너뛴다(변경 조각과 서로 독립)
            try:
                fixed = self._fixer_attributed(new or e, list(tries_by_run.get(e.run_id, {}).values()))
            except Exception as ex:  # noqa: BLE001 - one entry's attribution never stops the backfill
                log.debug("kb reindex: no fixer for %s: %s: %s", e.id, type(ex).__name__, ex)
                fixed = None
            if fixed is not None:
                new = fixed
                fixers += 1
            if new is None:
                out.append(b)
                continue
            out.append(new.model_dump_json().encode("utf-8"))
            changed.append(new)
        if bad:
            log.warning("kb reindex: %d line(s) of %s are no ErrorEntry — skipped (`sbmig kb validate`)", bad,
                        KB_ERRORS)
        if changed:
            atomic_write_bytes(self.errors_path, b"\n".join(out))
            self._write_entry_files(changed)
        return hunks, fixers

    # ------------------------------------------------------------------ cases.md / stats / validate
    def cases(self) -> list[tuple[CaseInfo, dict[str, Any]]]:
        """모든 케이스 (CaseInfo, 추가 키) — 널리 퍼진 것(프로젝트 수, 그다음 seen) 먼저."""
        out = []
        for cid in self.case_ids():
            hit = self._read(cid)
            if hit is not None:
                out.append(hit)
        out.sort(key=lambda h: (-len(h[0].projects), -h[0].seen, h[0].case_id))
        return out

    def _write_md_locked(self) -> None:
        """cases.md 를 다시 쓴다(잠금은 호출자가). 실패해도
        경고만 — 파생 뷰라 ``kb reindex`` 가 다시 만든다."""
        try:
            atomic_write(self.md_path, render_cases_md(self.cases()))
        except Exception as e:  # noqa: BLE001 - the view is derived (`sbmig kb reindex` rewrites it)
            log.warning("kb: could not write %s: %s: %s", self.md_path, type(e).__name__, e)

    def write_cases_md(self) -> Path:
        """잠금을 잡고 cases.md 를 다시 쓴다."""
        with stage("kb"), self.lock():
            self._write_md_locked()
        return self.md_path

    def stats(self) -> dict[str, Any]:
        """``sbmig kb stats`` 의 숫자들: 오류 수, 케이스 수, 스크립트 버전(상태별), 고정된 케이스 수."""
        cases = self.cases()
        scripts = [s for c, _ in cases for s in c.scripts]
        return {
            "kb_dir": str(self.root),
            "errors": self.count_errors(),
            "cases": len(cases),
            "cases_with_scripts": sum(1 for c, _ in cases if c.scripts),
            "script_versions": len(scripts),
            "scripts_proven": sum(1 for s in scripts if s.status == "proven"),
            "scripts_unverified": sum(1 for s in scripts if s.status == "unverified"),
            "scripts_retired": sum(1 for s in scripts if s.status == "retired"),
            "pinned": sum(1 for _, x in cases if x.get("pin")),
        }

    def validate(self, limit: int = 20) -> list[str]:
        """``sbmig kb validate``: KB 파일의 문제를 한 줄씩 — ErrorEntry 가 아닌
        errors.jsonl 줄, 못 읽거나 다른 케이스를 가리키는 case.json, L2 해시와 맞지 않는 폴더, 파일 없는
        스크립트·목록에 없는 스크립트 파일, 잘못된 pin.

        Problems of the KB files, one line each: errors.jsonl lines that are no ErrorEntry, case.json files that
        do not read / name another case, scripts listed without a file or files no script lists, bad pins."""
        from sbmig.kb.jsonl import parse_record

        problems: list[str] = []
        if self.errors_path.is_file():
            bad = []
            with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if line.strip() and parse_record(line, ErrorEntry) is None:
                        bad.append(n)
            problems += [f"{KB_ERRORS}: line {n} is no ErrorEntry" for n in bad[:limit]]
            if len(bad) > limit:
                problems.append(f"{KB_ERRORS}: … {len(bad) - limit} more unreadable line(s)")
        if self.cases_dir.is_dir():
            found = []
            for d in sorted(p for p in self.cases_dir.iterdir() if p.is_dir()):
                rel = f"{CASES_DIR}/{d.name}"
                path = d / CASE_FILE
                if not path.is_file():
                    found.append(f"{rel}: no {CASE_FILE}")
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    case = CaseInfo.model_validate(data)
                except (OSError, ValueError) as e:
                    found.append(f"{rel}/{CASE_FILE}: does not read ({type(e).__name__})")
                    continue
                if case.case_id != d.name:
                    found.append(f"{rel}/{CASE_FILE}: names case {case.case_id}")
                if case.l2 and case_id_for(case.l2) != d.name:
                    found.append(f"{rel}/{CASE_FILE}: l2 hashes to {case_id_for(case.l2)}, not {d.name}")
                listed = {s.file for s in case.scripts}
                for s in case.scripts:
                    if not (d / s.file).is_file():
                        found.append(f"{rel}: script v{s.version} has no file {s.file}")
                for p in d.glob("fix_v*.py"):
                    if p.name not in listed:
                        found.append(f"{rel}: {p.name} is not listed in {CASE_FILE}")
                pin = data.get("pin")
                if pin is not None and not (isinstance(pin, str) and (pin.strip() in ("", "block")
                                                                      or pin.strip().startswith("block:"))):
                    found.append(f"{rel}/{CASE_FILE}: pin {pin!r} is not `block` / `block:<reason>`")
            problems += found[:limit]
            if len(found) > limit:
                problems.append(f"{CASES_DIR}: … {len(found) - limit} more problem(s)")
        return problems

    # ------------------------------------------------------------------ import (merge)
    def merge_from(self, other: "CaseStore") -> dict[str, int]:
        """다른 KB(풀어 놓은 내보내기)를 합친다: 새 ID 의 errors.jsonl 줄, 이쪽에
        없는 케이스(case.json + 스크립트 파일 복사), 아는 케이스의 스크립트는 코드로 비교(같은 코드 → 카운터
        max·프로젝트 합집합·실행 기록 합침, 다른 코드 → 이쪽의
        다음 빈 번호 — 목록에 없는 ``fix_v*.py`` 도 피한다), 끝에 :meth:`recount`. 두 번 해도 결과가 같다.

        Merge another KB (an unpacked export): errors.jsonl lines with new ids, case folders the local KB does
        not have (copied whole), scripts of known cases by their code (same code → counters max / projects union;
        other code → the next local version), then :meth:`recount`. Idempotent."""
        from sbmig.kb.jsonl import append_lines

        counters = {"errors_added": 0, "cases_added": 0, "scripts_added": 0, "scripts_merged": 0}
        with stage("kb"), self.lock():
            if other.errors_path.is_file():
                known = self._known_ids()
                lines = [e.model_dump_json() for e in other.iter_errors() if e.id not in known]
                counters["errors_added"] = append_lines(self.errors_path, lines)
            for cid in other.case_ids():
                theirs = other._read(cid)
                if theirs is None:
                    continue
                t_case, t_extra = theirs
                mine = self._read(cid)
                if mine is None and not self._disk_versions(cid):
                    for s in t_case.scripts:
                        code = other.code(cid, s.version)
                        if code is not None:
                            atomic_write_bytes(self.case_dir(cid) / s.file, code.encode("utf-8"))
                    self._save(t_case, t_extra)
                    counters["cases_added"] += 1
                    continue
                if mine is None:
                    # 이쪽 폴더에 case.json 은 없는데 fix_v*.py 가 남아 있다(case.json 저장 전에 멈춘 실행 등):
                    # 그 파일을 덮지 않게 저쪽 스크립트를 아래에서 다음 빈 번호로 넣는다
                    case, extra = t_case.model_copy(update={"scripts": []}, deep=True), dict(t_extra)
                    counters["cases_added"] += 1
                else:
                    case, extra = mine
                    for k, v in t_extra.items():
                        # 이쪽 사람이 정한 pin 등 추가 키는 덮지 않는다
                        extra.setdefault(k, v)            # a local pin wins
                by_sha = {_sha(self.code(cid, s.version) or ""): s for s in case.scripts}
                local: dict[int, int] = {}                # their version → ours (v2.1e: an augment's parent)
                for s in sorted(t_case.scripts, key=lambda x: x.version):
                    code = other.code(cid, s.version)
                    if code is None:
                        continue
                    have = by_sha.get(_sha(code))
                    if have is not None:
                        local[s.version] = have.version
                        have.successes, have.failures = max(have.successes, s.successes), max(have.failures, s.failures)
                        have.projects += [p for p in s.projects if p not in have.projects]
                        if s.status == "proven" and have.status == "unverified":
                            have.status = "proven"
                        self._merge_runs(have, s, local)
                        counters["scripts_merged"] += 1
                        continue
                    # add_script 와 같은 규칙: 목록에 없는 fix_v*.py 도 피한다(A17)
                    v = self._next_version(case)
                    atomic_write_bytes(self.case_dir(cid) / f"fix_v{v}.py", code.encode("utf-8"))
                    local[s.version] = v
                    new = s.model_copy(update={"version": v, "file": f"fix_v{v}.py",
                                               "parent": local.get(s.parent) if s.parent else None})
                    case.scripts.append(new)
                    by_sha[_sha(code)] = new
                    counters["scripts_added"] += 1
                if t_case.last_solution and not case.last_solution:
                    case.last_analysis, case.last_solution = t_case.last_analysis, t_case.last_solution
                self._save(case, extra)
            self.recount()
        return counters

    def _merge_runs(self, have: CaseScript, theirs: CaseScript, local: dict[int, int]) -> None:
        """두 KB 에 같은 스크립트: 실행 기록의 합집합(같은 기록은 한 번),
        이쪽에 계보·이유가 없을 때만 저쪽 것을 쓴다.

        The same script in both KBs: the union of their runs (same record once), lineage / reasons when ours has
        none."""
        def ident(r: ScriptRun) -> tuple:
            return _run_key(r) or (r.run_id, r.round, r.n, r.result, _ts(r.at))
        seen = {ident(r) for r in have.runs}
        runs = have.runs + [r for r in theirs.runs if ident(r) not in seen]
        have.runs = self._cap_runs(sorted(runs, key=lambda r: (_ts(r.at), r.round, r.n)))
        if have.parent is None and theirs.parent and local.get(theirs.parent):
            have.parent = local[theirs.parent]
        have.reason = have.reason or theirs.reason
        have.last_failure = have.last_failure or theirs.last_failure


# --------------------------------------------------------------------------- cases.md
def _cell(text: Any, limit: int = 120) -> str:
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_cases_md(cases: list[tuple[CaseInfo, dict[str, Any]]]) -> str:
    """``cases.md``: 케이스마다 한 줄 — 케이스 · 이름 · 프로젝트 수 · seen / resolved ·
    스크립트(버전 상태 성공/실패 — 마지막 실패) · pin · 마지막 결과 — 널리 퍼진 것 먼저.

    ``cases.md``: one row per case — case · name · projects · seen / resolved · scripts (version status
    successes/failures, v2.1e: ``— <last failure>``) · pin · last result — most widespread first."""
    scripts = [s for c, _ in cases for s in c.scripts]
    by = {k: sum(1 for s in scripts if s.status == k) for k in ("proven", "unverified", "retired")}
    L = ["# KB cases", "",
         f"{len(cases)} case(s), {len(scripts)} script version(s) (proven {by['proven']}, unverified "
         f"{by['unverified']}, retired {by['retired']}); written {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC. "
         "`sbmig kb cases show <case>` for one case; scripts: successes/failures — last failure.", "",
         "| case | name | projects | seen / resolved | scripts | pin | last result |",
         "|---|---|---|---|---|---|---|"]
    for c, extra in cases:
        L.append(f"| {c.case_id} | {_cell(c.name or c.l2)} | {len(c.projects)} | {c.seen} / {c.resolved} | "
                 f"{_cell(scripts_text(c, failures=True), 320)} | {_cell(extra.get('pin') or '', 60) or '-'} | "
                 f"{_cell(extra.get('last_result') or '', 120) or '-'} |")
    return "\n".join(L) + "\n"


def note_lines(n: Any) -> list[str]:
    """수정 기록 하나를 수정 전/후 코드와 함께(``note_block``, v2.1e); 그게 없거나 실패하면 한 줄
    ``note_text``.

    One 수정 note with its as-is / to-be code (``sbmig.errors.common.note_block``, v2.1e); the one-line
    ``note_text`` when that is not there or fails."""
    try:
        from .common import note_block
    except ImportError:
        return [note_text(n)]
    try:
        got = [str(x) for x in (note_block(n) or [])]
    except Exception:  # noqa: BLE001 - a broken renderer: the line alone
        got = []
    return got or [note_text(n)]


def entry_lines(e: ErrorEntry, *, diff: bool = True) -> list[str]:
    """항목 하나와 그 시도들을 읽기 쉬운 블록으로(``sbmig kb errors show``, ``cases show``): 수정 기록마다
    수정 전/후 코드(v2.1e), 해결한 시도(v2.1f).

    A readable block of one entry and its tries (``sbmig kb errors show``, ``cases show``); each 수정 note with
    its as-is / to-be code (v2.1e)."""
    loc = (e.file or e.module or "-") + (f":{e.line}" if e.file and e.line else "")
    L = [f"entry {e.id}  [{e.status}]  case {e.case_id or '-'}  seen {e.seen or '-'}"
         + (f"  resolved {e.resolved}" if e.resolved else ""),
         f"  run {e.run_id or '-'}  project {e.project or '-'}  kind {e.kind.value}",
         f"  {loc}  {e.message}"]
    if e.l2:
        L.append(f"  l2: {e.l2}")
    if e.note:
        L.append(f"  note: {e.note}")
    if e.script:
        L.append(f"  script: {e.script}")
    for rb in e.resolved_by:                       # v2.1f: resolved by build → the try that probably fixed it
        L.append(f"  resolved by: {rb.text}" + (" (reindex)" if rb.match == "reindex" else ""))
    if e.raw and e.raw != e.message:
        L += ["  raw:"] + ["    " + ln for ln in e.raw.splitlines()[:15]]
    if e.tries:
        L.append(f"  tries: {tries_text(e)}")
    for t in e.tries:
        L.append(f"  try {t.n} (round {t.round}, {t.source}" + (f" v{t.script_version}" if t.script_version else "")
                 + f", applied {t.applied}, result {t.result}" + (f", commit {t.commit[:10]}" if t.commit else "")
                 + (f", {t.llm_calls} LLM call(s)" if t.llm_calls else "") + (f", {t.seconds:.0f}s" if t.seconds else "")
                 + ")")
        for label, text in (("원인", t.analysis), ("해결", t.solution), ("스크립트 없음", t.no_script_reason),
                            ("note", t.note)):
            if text:
                L.append(f"    {label}: {text}")
        if t.edits:
            L += ["    수정:"] + [f"      {ln}" for x in t.edits for ln in note_lines(x)]
        if t.major_upgrades:                  # v2.1d: major upgrades / library replacements → 운영자 확인
            L += ["    메이저 변경:"] + [f"      {change_line(x)}" for x in t.major_upgrades]
        if t.verify:
            L += ["    운영자 확인:"] + [f"      {x}" for x in guide_lines(t.verify)]
        if t.files:
            L.append(f"    files: {', '.join(t.files)}")
        if diff and t.diff:
            L += ["    diff:"] + ["      " + ln for ln in t.diff.splitlines()]
    return L
