"""v2.1 지식베이스(KB) 저장소 ``CaseStore``: 모든 실행의 오류 목록 + 케이스마다 폴더 하나.
흐름상 위치: 수정 루프가 라운드마다 저장된 스크립트를 찾고(``best`` / ``latest``) 새 스크립트를
저장하고(``add_script``) 채점 결과를 적는다(``credit``). 실행 끝에 ``record_run`` 이 오류 목록을 덧붙이고,
``sbmig kb reindex`` 는 ``recount`` 를 부른다. 바깥 창구는 ``sbmig.kb.FileKnowledgeBase``, 사람 창구는 ``sbmig
kb`` CLI. 케이스 폴더 이름은 케이스 ID(sha1(L2)[:10]): 매칭은 L2 문자열로 하고 폴더 이름은 키일 뿐이다(사람용
이름은 case.json 의 ``name`` 과 cases.md). 여러 파드가 같이 쓸 때: 서버에는 있는데 이 클라이언트가 끝내 못 읽는
case.json 위에는 새 파일을 쓰지 않는다(``CaseUnreadable`` — 그 KB 쓰기만 건너뜀). 스크립트 상태: 저장하면 unverified → 만든 프로젝트가 아닌 곳에서
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
the newest proven version, else the newest unverified one; v2.1y: a version the loop found ``non_idempotent``
(:meth:`CaseStore.mark_non_idempotent`: a second run right after ``ok`` changed the tree again) is never selected. A script whose code holds a secret value is stored
masked and retired at once (the masked code is not the code that ran; a secret never lands in the KB).

Every write holds the KB lock and is atomic; texts are masked (:mod:`sbmig.kb.redact`). Readers never lock and
never raise on a damaged file (it is skipped with a warning; ``sbmig kb validate`` lists it). File-based on purpose
(invariant 3).

Several writers (v2.1z-ae: up to three build pods on one NFS volume): inside the lock every mutator reads the case
again from disk before it writes. A case that reads as missing is confirmed by exclusive creates — ``mkdir`` of its
folder, then an ``O_EXCL`` probe of its ``case.json`` (:meth:`CaseStore._confirm_new`: an NFS client may answer "no
such file" from its cache for a ``case.json`` another pod has just written; the server decides an exclusive create):
when it is there the case is read again, never overwritten blind — the folder is listed and the file read by ``open()``
for up to ``CONFIRM_SECONDS`` (an NFS client trusts a cached "no such file" until the directory's attributes are
checked again), and a case.json the server has but this client never sees raises :class:`CaseUnreadable` (that one
KB write is skipped with a WARNING; nothing is written over it). A script file is claimed by a hard link of a temp file
(exclusive: an existing ``fix_v<N>.py`` is never replaced — the next number is taken), by an exclusive create where
the file system has no hard links; any other link error is raised.

서명 규칙 버전(v2.1z-ai): 항목과 case.json 은 자기 L2 를 만든 규칙 버전(``sig``)을 갖는다. 규칙이 바뀌면 :meth:`CaseStore.resign`
(``sbmig.errors.resign`` 의 계획 + 이 파일의 쓰기)이 기록을 지금 규칙의 ID 로 옮긴다(옛 ID 는 ``was`` / ``aliases`` 에). 정확한
키 확인: 저장된 case.json 의 L2 가 물어본 L2 와 다르면 그 케이스가 아니다 — 재사용도 기록도 하지 않는다(:meth:`CaseStore.same_case`).

Signature rule versions (v2.1z-ai): entries and case.json files carry the rule version of their L2 (``sig``); a rule
change is followed by :meth:`CaseStore.resign`, which moves records and scripts to the ids the current rules give
(``<kb>/kb.json`` = the version the KB was last re-signed to and the cases it skipped, written at the end of every
re-sign; ``<kb>/resign-pending.json`` = the folder clean-up a re-sign still owes, executed first by the next one).
Only records of classes the rules changed are touched; nothing is ever merged by an alias (aliases are a lookup aid
for people). The re-sign streams errors.jsonl: memory follows the affected cases, not the file. The exact-key guard: a stored case whose L2 is not the L2 asked
for is not that case — nothing is reused from or written into it (:meth:`CaseStore.same_case`, :class:`CaseKeyMismatch`).
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from sbmig.core.models import CaseInfo, CaseScript, EditNote, ErrorEntry, ErrorKind, FixTry, ScriptRun, utcnow
from sbmig.core.stage import stage

from .common import Cleaner, atomic_write, atomic_write_bytes, case_id_for, first_line, note_text, posix
from .entries import change_line, guide_lines, probable_fixers, resolved_text, round_of, tries_text
from .resign import (ResignReport, Signer, changed_since, current_version, cut, l2_parts, line_case_id, line_case_ids,
                     older_record, plan_entries, resigned_l2)

log = logging.getLogger("sbmig.errors")

CASES_DIR = "cases"
ENTRIES_DIR = "errors"            # <kb>/cases/<case_id>/errors/<run_id>-<id>.json (one readable file per error)
OTHER_DIR = "_other"              # entries without a case (pom-verify, notes)
CASE_FILE = "case.json"
KB_ERRORS = "errors.jsonl"
CASES_MD = "cases.md"
KB_MARKER = "kb.json"             # {"sig": <rule version the KB was re-signed to>, "resigned_at": <iso>}
RESIGN_PENDING = "resign-pending.json"   # the folder clean-up a re-sign still owes (written before errors.jsonl)
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
CONFIRM_SECONDS = 75.0            # a case.json the server has but this client does not see is looked for this long
#                                   (longer than an NFS client's directory attribute cache may lie: acdirmax 60 s)
REPAIR_SECONDS = 1.0              # a case.json that opens but stays empty / does not parse this long is written fresh
CONFIRM_MAX_DELAY = 2.0           # the longest pause between two looks while confirming
CLAIM_TRIES = 1000                # script numbers tried past one taken by another writer
# os.link 의 이 오류들만 '하드 링크가 없는 파일 시스템' 이다 — EIO / ESTALE / EACCES / ENOSPC 는 진짜 오류
_NO_LINK_ERRNOS = frozenset(e for e in (getattr(errno, n, None) for n in
                                        ("EPERM", "EOPNOTSUPP", "ENOTSUP", "ENOSYS", "EMLINK", "EXDEV")) if e)
# Windows: ERROR_INVALID_FUNCTION(1), ERROR_NOT_SAME_DEVICE(17), ERROR_NOT_SUPPORTED(50), ERROR_TOO_MANY_LINKS(1142)
_NO_LINK_WINERRORS = frozenset({1, 17, 50, 1142})


class CaseUnreadable(OSError):
    """서버는 case.json 이 있다고 하는데(배타적 생성이 EEXIST) 이 클라이언트가 ``CONFIRM_SECONDS`` 동안 읽지 못했다 —
    새 case.json 을 그 위에 쓰지 않고 이 KB 쓰기를 건너뛴다. OSError 라서 KB 를 부르는 쪽(수정 루프의 어댑터,
    ``FileKnowledgeBase.record_run`` / ``reindex``)이 이미 잡아 WARNING 으로 끝낸다.

    The server says ``case.json`` exists (an exclusive create answered EEXIST) but this client could not read it for
    ``CONFIRM_SECONDS``: no fresh file is written over it, this KB write is skipped. An OSError, so every KB caller
    that already survives an I/O error survives this one."""


class CaseKeyMismatch(OSError):
    """케이스 ID 는 같은데 저장된 case.json 의 L2 가 물어본 L2 와 다르다(10자 해시 충돌, 손으로 고친 case.json) — 그
    케이스가 아니므로 아무것도 쓰지 않는다. OSError 라서 수정 루프의 어댑터가 ``CaseUnreadable`` 처럼 WARNING 으로 끝낸다.

    The case id matches but the stored case's L2 is not the L2 asked for: nothing is written into it. An OSError,
    so every KB caller that survives ``CaseUnreadable`` survives this one."""


# (KB 디렉토리, 케이스 ID) — 정확한 키 경고는 프로세스마다 한 번
_MISMATCH_SAID: set[tuple[str, str]] = set()


def _no_hard_links(e: OSError) -> bool:
    # 이 파일 시스템에 하드 링크가 없다는 뜻의 오류인가(그 밖의 오류는 진짜 실패)
    if getattr(e, "winerror", None) in _NO_LINK_WINERRORS:
        return True
    return e.errno in _NO_LINK_ERRNOS


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
        t = f"v{s.version} {s.status}{' non-idempotent' if s.non_idempotent else ''} {s.successes}/{s.failures}"
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
        # v2.1z-ai: 다른 L2 가 차지한 케이스 ID(이 프로세스에서 본 것) — best / latest / credit 이 건너뛴다
        self._foreign: set[str] = set()                # case ids held by another signature (seen this process)
        self.older_lines = 0                           # lines signed by older rules after the last record_run
        self.signer: Optional[Signer] = None           # the rule a re-sign uses (None = the current rules)

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
    def _read(self, case_id: str, *, quiet: bool = False) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """(케이스, case.json 의 추가 키) — 없거나 못 읽거나 다른 케이스를 가리키면 경고 후 None(예외 없음).
        ``quiet`` 는 경고 대신 DEBUG(:meth:`_confirm_new` 의 다시 읽기가 같은 경고를 여러 번 내지 않게).

        (case, extra keys of case.json) — None when there is no readable case file. An empty (0-byte) file is "not
        there yet" (DEBUG): a lock-free reader may open it in another pod's confirmation probe window."""
        hit, state, detail = self._load(case_id)
        if hit is not None:
            return hit
        path = self.cases_dir / case_id / CASE_FILE
        if state == "empty":
            log.debug("kb: %s is empty (being created?) — not there yet", path)
        elif state in ("damaged", "error"):
            (log.debug if quiet else log.warning)("kb: %s %s — skipped (`sbmig kb validate`)", path, detail)
        return None

    def _load(self, case_id: str) -> tuple[Optional[tuple[CaseInfo, dict[str, Any]]], str, str]:
        """case.json 을 ``open()`` 으로 바로 읽는다(먼저 ``is_file()`` 을 믿지 않는다 — NFS 의 캐시된 '없음' 은 그
        lookup 에서 나온다). (결과, 상태, 설명): 상태 ok / missing(열리지 않음: 없음) / empty(0 바이트) /
        damaged(열리지만 JSON·CaseInfo 가 아니거나 다른 케이스) / error(다른 OSError — EACCES, EIO, 폴더 …).

        Read ``case.json`` by ``open()`` (no prior ``is_file()``: a cached negative lookup is what lies on NFS).
        (hit, state, detail): ok / missing / empty (0 bytes) / damaged (opens, does not parse or names another
        case) / error (any other OSError)."""
        if not valid_case_id(case_id):
            return None, "missing", ""
        path = self.cases_dir / case_id / CASE_FILE
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None, "missing", ""
        except OSError as e:
            return None, "error", f"does not read ({type(e).__name__})"
        if not raw.strip():
            return None, "empty", ""
        try:
            # utf-8-sig: 사람이 메모장으로 고쳐 BOM 이 붙은 case.json 도 읽는다
            data = json.loads(raw.decode("utf-8-sig"))
            case = CaseInfo.model_validate(data)
        except ValueError as e:                           # UnicodeDecodeError / JSONDecodeError / ValidationError
            return None, "damaged", f"does not read ({type(e).__name__})"
        if case.case_id != case_id:
            return None, "damaged", f"names case {case.case_id}"
        # CaseInfo 에 없는 키(pin, last_result, 사람이 넣은 것)는 따로 들고 있다가 저장 때 되돌려 넣는다
        extra = {k: v for k, v in data.items() if k not in CaseInfo.model_fields}
        return (case, extra), "ok", ""

    def _keep_alive(self) -> None:
        """긴 구간(재색인·가져오기·case.json 확인 대기)에서 KB 잠금이 낡아 깨지지 않게 mtime 을 새로 찍는다(잠금
        모듈이 몇 초에 한 번으로 줄인다 — 항목마다 불러도 싸다)."""
        from sbmig.kb.lock import keep_alive

        keep_alive(self.root)

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

    # ------------------------------------------------------------------ v2.1z-ai: the exact-key guard
    def _mismatch(self, case_id: str, stored: str, asked: str) -> None:
        """케이스 ID 를 남의 것으로 표시하고 경고(ID·프로세스마다 한 번).

        Mark ``case_id`` as held by another signature; one WARNING per id and process."""
        self._foreign.add(case_id)
        key = (str(self.root), case_id)
        if key not in _MISMATCH_SAID:
            _MISMATCH_SAID.add(key)
            log.warning('case id %s is held by another signature: stored "%s" asked "%s" — nothing is reused from or '
                        "written into it", case_id, cut(stored, 160), cut(asked, 160))

    def same_case(self, case_id: str, l2: str, case: Optional[CaseInfo] = None) -> bool:
        """저장된 케이스가 정말 이 L2 의 케이스인가. 저장된 L2 와 물어본 L2 가 둘 다 있고 다르면 False — 그 ID 는 이
        프로세스에서 '남의 것' 으로 기억되어 ``best`` / ``latest`` / ``credit`` / ``describe`` 가 아무것도 주지도 쓰지도
        않는다(같은 L2 로 다시 물어 맞으면 풀린다). 케이스가 없거나 어느 한쪽 L2 가 비면 True. 예외 없음.

        Is the stored case really the case of ``l2``? False when both L2s are there and differ — the id is then
        remembered as foreign for this process (``best`` / ``latest`` / ``credit`` / ``describe`` give / write nothing;
        a later matching ask clears it). True when there is no case or either L2 is empty. Never raises."""
        if not l2 or not valid_case_id(case_id):
            return True
        if case is None:
            case = self.find(case_id)
        if case is None or not case.l2:
            self._foreign.discard(case_id)
            return True
        if case.l2 != l2:
            self._mismatch(case_id, case.l2, l2)
            return False
        self._foreign.discard(case_id)
        return True

    def foreign(self, case_id: str) -> bool:
        """이 프로세스에서 다른 L2 가 차지한 케이스 ID 로 확인된 것인가.

        Was ``case_id`` found held by another signature in this process?"""
        return case_id in self._foreign

    def _case_or_new(self, case_id: str, l2: str = "", name: str = "",
                     kind: Optional[ErrorKind] = None) -> tuple[CaseInfo, dict[str, Any]]:
        """있는 케이스(비어 있는 l2·이름·종류만 채움) 또는 새 CaseInfo, 그리고 추가 키. 없다고 읽히면
        :meth:`_confirm_new` 로 정말 없는지 확인한 뒤에만 새로 만든다(잠금 안에서 부른다)."""
        hit = self._read(case_id)
        if hit is None:
            hit = self._confirm_new(case_id)
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

    def _confirm_new(self, case_id: str) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """잠금 안에서 '케이스 없음' 을 확인한다. NFS 클라이언트는 다른 파드가 방금 만든 case.json 을 캐시 때문에
        아직 '없음' 으로 답할 수 있다 — 그래서 '만들기' 로 서버에 묻는다(배타적 생성은 클라이언트 캐시가 아니라 서버가
        판정한다). 케이스 폴더를 ``mkdir`` 해 보고 만들어지면 정말 새 케이스(None). 폴더가 있으면 case.json 을
        ``O_EXCL`` 로 만들어 본다(만들어지면 바로 지운다): 이미 있으면 폴더를 열어(목록 읽기 — NFS 가 폴더 속성을 다시
        가져와 캐시된 '없음' 을 버린다) ``open()`` 으로 다시 읽기를 백오프로 ``CONFIRM_SECONDS`` 동안 반복하고(그동안
        :func:`keep_alive` 로 잠금을 살려 둠), 읽히면 그 케이스를 돌려준다. 열리지만 ``REPAIR_SECONDS`` 동안 비어 있거나
        깨져 있으면 예전처럼 경고 후 None(새로 씀 — 죽은 쓰기가 남긴 파일). 끝내 보이지 않으면 아무것도 쓰지 않고
        :class:`CaseUnreadable`. 없으면 에러별 JSON(``errors/``)만 있는 폴더는 조용히, 다른 것이 있는 폴더(case.json 을
        쓰기 전에 멈춘 실행)는 경고와 함께 None — 이미 있는 스크립트 파일은 번호를 지킨다.

        Inside the lock, confirm that a case reading as missing really is new: an NFS client may still answer "no
        such file" from its cache for a ``case.json`` another pod has just written, while an exclusive create is
        decided by the server. ``mkdir`` of the case folder: made → new (None). The folder exists → an ``O_EXCL``
        create of ``case.json`` (removed at once when it succeeds). It exists → list the folder (close-to-open: the
        directory's attributes are fetched again, a cached negative entry goes) and read by ``open()``, with backoff,
        for up to ``CONFIRM_SECONDS`` (the lock kept alive meanwhile); the case read is returned. A file that opens but
        stays empty / does not parse for ``REPAIR_SECONDS`` → WARNING and None (a fresh one is written, as before: a
        crashed writer's file). Never seen → nothing is written, :class:`CaseUnreadable` is raised (WARNING). It does
        not exist → None, quietly for a folder of per-error files (``errors/``), with a WARNING for one a writer left
        before saving its case.json (the scripts already there keep their numbers)."""
        d = self.case_dir(case_id)
        try:
            self.cases_dir.mkdir(parents=True, exist_ok=True)
            os.mkdir(d)                                   # no exist_ok: the exclusive create IS the check
            return None
        except FileExistsError:
            pass
        except OSError as e:
            log.debug("kb: cases/%s: a new case not confirmed (%s) — treated as new", case_id, e)
            return None
        there = self._case_file_on_server(case_id)
        if there is False:
            try:
                names = os.listdir(d)
            except OSError:
                names = []
            # 에러별 JSON 만 있는 폴더(L2 없는 항목을 record_run 이 적음)는 쓰다 멈춘 사람이 없다 — 조용히 새 케이스
            if any(n != ENTRIES_DIR for n in names):
                log.warning("kb: cases/%s holds no %s (a writer stopped before saving it?) — a fresh %s is written; "
                            "the scripts already there keep their numbers", case_id, CASE_FILE, CASE_FILE)
            return None
        # 있다(또는 모름): 실패한 배타적 생성이 이 클라이언트의 캐시를 고친다는 보장은 없다(NFS 는 폴더 속성이 다시
        # 확인될 때까지 캐시된 '없음' 을 믿는다) — 폴더를 열어 속성을 다시 가져오게 하고 open() 으로 읽기를 반복한다
        start = time.monotonic()
        deadline = start + (CONFIRM_SECONDS if there else REPAIR_SECONDS)
        delay, bad_since, state, detail, said = 0.05, None, "missing", "", False
        while True:
            try:
                os.listdir(d)                             # close-to-open on the directory: its attributes again
            except OSError:
                pass
            hit = self._read(case_id, quiet=True)
            if hit is not None:
                log.info("kb: cases/%s was created by another writer meanwhile — that case is used, not a fresh one",
                         case_id)
                return hit
            _, state, detail = self._load(case_id)
            now = time.monotonic()
            # 열리지만 비었거나 깨진 파일: 원자적 교체 중에는 이렇게 보이지 않는다 — 잠깐 뒤에도 그러면 고친다
            if state in ("empty", "damaged"):
                bad_since = now if bad_since is None else bad_since
                if now - bad_since >= REPAIR_SECONDS:
                    break
            else:
                bad_since = None
            if now >= deadline:
                break
            if not said and now - start >= 2.0:
                said = True
                log.info("kb: cases/%s/%s exists on the server but this client does not see it yet — looking again "
                         "for up to %.0f s", case_id, CASE_FILE, CONFIRM_SECONDS)
            self._keep_alive()                            # inside the KB lock: nobody may break it as stale meanwhile
            time.sleep(max(0.01, min(delay, deadline - now)))
            delay = min(delay * 2, CONFIRM_MAX_DELAY)
        if state in ("empty", "damaged"):
            log.warning("kb: cases/%s/%s is there but %s after %.0f s — a fresh one is written over it", case_id,
                        CASE_FILE, "stays empty" if state == "empty" else detail or "does not read", REPAIR_SECONDS)
            return None
        if there is None and state == "missing":
            return None                                   # could not ask the server and nothing reads: as before
        # 서버는 있다고 하는데 끝내 못 읽음: 새 파일을 그 위에 쓰면 다른 파드의 스크립트 목록·실적이 사라진다 — 안 쓴다
        log.warning("kb: cases/%s/%s exists on the server but this client could not read it for %.0f s (%s) — nothing "
                    "is written over it; this KB write is skipped", case_id, CASE_FILE, time.monotonic() - start,
                    detail or state)
        raise CaseUnreadable(errno.EAGAIN, f"cases/{case_id}/{CASE_FILE} exists but does not read on this client "
                                           f"({detail or state}) — not overwritten")

    def _case_file_on_server(self, case_id: str) -> Optional[bool]:
        """case.json 이 정말 있나 — ``O_EXCL`` 생성으로 서버에 묻는다(만들어지면 바로 지움). 모르면 None.

        Does ``case.json`` exist — asked by an ``O_EXCL`` create (removed at once when it succeeds); None = unknown."""
        p = self.case_dir(case_id) / CASE_FILE
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return True
        except OSError as e:                              # Windows: a file being replaced reads as PermissionError
            log.debug("kb: cases/%s: %s not probed (%s)", case_id, CASE_FILE, e)
            return None
        os.close(fd)
        try:
            os.unlink(str(p))
        except OSError:
            pass
        return False

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
        버전. retired·비멱등(v2.1y ``non_idempotent``)·파일이 없는 버전은 고르지 않는다. None = 저장된 것 없음.

        The script to run first: the newest proven version, else the newest unverified one; retired never, nor a
        version marked ``non_idempotent`` (v2.1y: a second run changed the tree again), nor one whose file is
        missing. None = nothing stored (also for a case id held by another signature, :meth:`same_case`)."""
        if case_id in self._foreign:
            return None
        # v2.1y (E-H3): 두 번째 실행에서 또 바꾸는 스크립트는 반쯤 고친 다음 프로젝트를 망가뜨린다 — 기록만 남기고 재사용 안 함
        ranked = sorted((s for s in self.scripts(case_id) if s.status != "retired" and not s.non_idempotent),
                        key=lambda s: (s.status == "proven", s.version), reverse=True)
        for s in ranked:
            code = self.code(case_id, s.version)
            if code:
                return s, code
        return None

    def latest(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """retired 가 아닌 가장 새 버전 — 보강(augment) 요청의 바탕.

        The newest non-retired version (the base of an augment request); None for a foreign case id."""
        if case_id in self._foreign:
            return None
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
            # 정확한 키: 같은 ID 를 다른 L2 가 차지하고 있으면 그 케이스에 쓰지 않는다(어댑터가 OSError 를 WARNING 으로 끝냄)
            if (l2 and case.l2 and case.l2 != l2) or (not l2 and case_id in self._foreign):
                self._mismatch(case_id, case.l2, l2)
                raise CaseKeyMismatch(errno.EEXIST, f"cases/{case_id} holds another signature — the script is not "
                                                    "stored")
            if l2 and case.l2 == l2:
                case.sig = max(int(case.sig or 0), current_version())   # the asked L2 is the current rules'
            masked = self.clean.red.redact(code) or ""
            # 비밀값이 든 코드는 마스킹본만 저장하고 바로 retired:
            # 마스킹된 코드는 실제로 돈 코드가 아니라 재사용하면 안 된다
            secret = masked != code
            stored = SECRET_HEADER + masked if secret else code
            for s in case.scripts:
                if _sha(self.code(case_id, s.version) or "") == _sha(stored):
                    log.info("cases/%s/fix_v%d.py: the same script is already stored", case_id, s.version)
                    return s
            v = self._claim_script(case_id, self._next_version(case), stored.encode("utf-8"))
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

    def _claim_script(self, case_id: str, version: int, data: bytes) -> int:
        """``fix_v<version>.py`` 를 배타적으로 만든다: 같은 폴더의 임시 파일에 쓰고 하드 링크로 이름을 붙인다(이름이
        이미 있으면 실패 — 원자적이고 덮어쓰지 않음). 그 번호가 이미 있으면(잠금 없이 쓴 다른 쓰기) 경고 후 다음
        번호. 하드 링크가 안 되는 파일 시스템이면 예전처럼 원자적 교체(번호는 잠금이 지킨다). 쓴 번호를 돌려준다.

        Create ``fix_v<version>.py`` exclusively — a temp file in the folder, then a hard link to the name (fails
        when the name exists: atomic, never overwrites); a number already taken (a write outside the lock) → a
        WARNING and the next one. Without hard links (EPERM / EOPNOTSUPP / ENOSYS / EMLINK / EXDEV, Windows' "not
        supported", ``NotImplementedError``): an exclusive create of the name (``O_EXCL``) — an existing file is never
        replaced there either. Any other link error (EIO, ESTALE, EACCES, ENOSPC …) is raised."""
        d = self.case_dir(case_id)
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".fix.", suffix=".tmp", dir=str(d))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            v = int(version)
            linked = True
            for _ in range(CLAIM_TRIES):
                target = d / f"fix_v{v}.py"
                if linked:
                    try:
                        os.link(tmp, target)
                        return v
                    except FileExistsError:
                        # NFS: 응답을 잃어 다시 보낸 LINK 는 성공했어도 EEXIST 로 답할 수 있다 — 링크 수가 2 면 우리 것
                        try:
                            if os.stat(tmp).st_nlink >= 2:
                                return v
                        except OSError:
                            pass
                        self._number_taken(case_id, v)
                        v += 1
                        continue
                    except NotImplementedError:
                        linked = False
                    except OSError as e:
                        # '하드 링크 없음' 만 대체 경로로: EIO·ESTALE·EACCES·ENOSPC 에 덮어쓰면 남의 파일을 잃는다
                        if not _no_hard_links(e):
                            raise
                        linked = False
                        log.debug("kb: cases/%s: no hard links here (%s) — exclusive create instead", case_id, e)
                if self._create_exclusive(target, data):
                    return v
                self._number_taken(case_id, v)
                v += 1
            raise FileExistsError(errno.EEXIST, f"cases/{case_id}: {CLAIM_TRIES} script numbers in a row are taken")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _number_taken(case_id: str, v: int) -> None:
        log.warning("kb: cases/%s/fix_v%d.py already exists (written outside the lock?) — v%d is used",
                    case_id, v, v + 1)

    @staticmethod
    def _create_exclusive(target: Path, data: bytes) -> bool:
        """하드 링크가 없는 곳의 대체: ``O_EXCL`` 로 이름을 만들고 쓴다(이미 있으면 False — 덮어쓰지 않음). 쓰다 실패하면
        우리가 만든 그 파일을 지우고 예외를 그대로 올린다.

        Without hard links: create the name with ``O_EXCL`` and write it (False when it exists — never replaced); a
        failed write removes the file we made and raises."""
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(str(target))                    # ours: created exclusively a moment ago
            except OSError:
                pass
            raise
        return True

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
        if not result or result == "pending" or case_id in self._foreign:
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

    def mark_non_idempotent(self, case_id: str, version: int, note: str = "") -> Optional[CaseScript]:
        """v2.1y: 버전 N 을 비멱등으로 표시한다(수정 루프가 ok 직후 같은 스크립트를 한 번 더 돌렸더니 트리를 또 바꿈).
        기록은 남기되 ``best`` 가 다시는 고르지 않는다(보강의 바탕 ``latest`` 로는 남는다 — LLM 이 고칠 수 있게).
        ``note`` 는 ``last_failure`` 에(사람이 case.json 만 보고 알게). 케이스·버전이 없으면 None(예외 없음).

        v2.1y (prompt review E-H3): mark version N ``non_idempotent`` — the loop ran it once more right after an
        ``ok`` run and it changed the tree again (``new JobBuilder(.., jobRepository, jobRepository)``). Kept for the
        record and as an augment base (``latest``), never selected by ``best``; ``note`` goes to ``last_failure``.
        None when the case / version is not stored. Never raises."""
        try:
            with stage("kb"), self.lock():
                hit = self._read(case_id)
                if hit is None:
                    return None
                case, extra = hit
                s = next((x for x in case.scripts if x.version == int(version)), None)
                if s is None:
                    log.warning("cases/%s: no script v%s to mark non-idempotent", case_id, version)
                    return None
                if s.non_idempotent and not note:
                    return s
                s.non_idempotent = True
                if note:
                    s.last_failure = self._reason(note)
                case.updated_at = utcnow()
                self._save(case, extra)
                log.warning("cases/%s/fix_v%d.py: non-idempotent (a second run changed the tree again) — kept, "
                            "never reused", case_id, s.version)
            return s
        except Exception as e:  # noqa: BLE001 - the loop only records it; the KB never stops a run
            log.warning("cases/%s: marking v%s non-idempotent failed: %s: %s", case_id, version, type(e).__name__, e)
            return None

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
        if case_id in self._foreign:
            return "저장된 스크립트 없음 (케이스 ID 를 다른 서명이 차지함 — 재사용·기록 안 함)"
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
        return self._scan_lines()[0]

    def _scan_lines(self, skipped: Iterable[str] = ()) -> tuple[set[str], int]:
        """errors.jsonl 한 번 읽기: (항목 ID 들, 옛 규칙으로 서명되고 그 분류가 바뀐 레코드 줄 수). 줄마다 정규식, 후보만
        파싱(:func:`sbmig.errors.resign.older_record`); ``skipped`` 케이스의 줄은 세지 않는다.

        One pass over errors.jsonl: (the entry ids, the records signed by older rules whose class changed since); a
        regex per line, a parse for candidates only; lines of the ``skipped`` cases are not counted."""
        from sbmig.kb.jsonl import line_ids

        out: set[str] = set()
        older = 0
        if not self.errors_path.is_file():
            return out, 0
        v = current_version()
        skip = set(skipped or ())
        with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    out.update(line_ids(line))
                    if older_record(line, v, skip) is not None:
                        older += 1
        return out, older

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
        return e.model_copy(update={"message": c(e.message), "raw": c(e.raw), "note": c(e.note), "tries": tries,
                                    "l1": c(e.l1), "symbol": c(e.symbol), "location": c(e.location),
                                    "artifact": c(e.artifact), "goal": c(e.goal)})

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
        self.older_lines = 0
        if not entries:
            return 0
        with stage("kb"), self.lock():
            new_file = not self.errors_path.exists()
            skipped = self.skipped_cases()
            known, older = self._scan_lines(skipped)
            fresh = [e for e in entries if e.id not in known]
            n = append_lines(self.errors_path, [e.model_dump_json() for e in fresh])
            v = current_version()
            # v2.1z-ai: 덧붙인 뒤에도 옛 규칙의 줄이 있으면 FileKnowledgeBase 가 재서명을 부른다(이미 읽은 줄 + 새 줄)
            self.older_lines = older + sum(1 for e in fresh if e.l2 and int(e.sig or 0) < v
                                           and l2_parts(e.l2)[1] in changed_since(e.sig)
                                           and e.case_id not in skipped)
            if new_file and n:
                self._write_marker()                      # a brand-new KB is current: it never re-signs
            by_case: dict[str, list[ErrorEntry]] = {}
            skip_files: set[str] = set()
            # note / pom-verify 는 케이스 통계에 넣지 않는다; L2 도
            # case.json 도 없는 케이스(L1 폴백 ID)는 폴더를 만들지 않는다
            for e in fresh:
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id) \
                        and (e.l2 or (self.cases_dir / e.case_id / CASE_FILE).is_file()):
                    by_case.setdefault(e.case_id, []).append(e)
            for cid, es in by_case.items():
                self._keep_alive()
                try:
                    case, extra = self._case_or_new(cid, es[0].l2, case_name(es[0].message, es[0].l2), es[0].kind)
                except CaseUnreadable as ex:
                    # 이 케이스만 건너뛴다: errors.jsonl 에는 이미 적혔으니 `sbmig kb reindex` 가 나중에 다시 센다
                    log.warning("kb: case %s not updated by this run (%s) — `sbmig kb reindex` recounts it from %s",
                                cid, ex, KB_ERRORS)
                    continue
                # 정확한 키: 저장된 L2 와 다른 L2 의 항목은 errors.jsonl 에만 남고 그 케이스에는 셈하지 않는다
                mine = [e for e in es if not (e.l2 and case.l2 and e.l2 != case.l2)]
                if len(mine) < len(es):
                    other = next(e for e in es if e not in mine)
                    self._mismatch(cid, case.l2, other.l2)
                    skip_files.update(e.id for e in es if e not in mine)
                if not mine:
                    continue
                if any(e.l2 and e.l2 == case.l2 and int(e.sig or 0) >= v for e in mine):
                    case.sig = max(int(case.sig or 0), v)
                self._apply(case, extra, mine)
                self._save(case, extra)
            self._write_entry_files([e for e in fresh if e.id not in skip_files])
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
            self._keep_alive()                            # reindex rewrites thousands of these over NFS
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

    def recount(self, skip: Iterable[str] = ()) -> int:
        """재색인(``sbmig kb reindex``): ``errors.jsonl`` 로부터 케이스마다 seen / resolved /
        프로젝트 / 마지막 텍스트를 다시 센다(스크립트 성공·실패 수와 사람이 넣은 키는 그대로; errors.jsonl
        에만 있는 L2 의 케이스는 만든다). 케이스 수를 돌려준다. v2.1e: 저장 버전마다 실행 기록(runs)·보강
        계보(parent / reason)·마지막 실패를 다시 만들고, 변경 조각 없이 저장된 옛 시도에 수정 전/후 코드를
        채운다; v2.1f: build 로 해결된 옛 항목에 '해결한 시도'(resolved_by)도 채운다. errors.jsonl 과 그
        항목들의 에러별 JSON 은 한 번만 다시 쓴다. ``skip`` 의 케이스(재서명이 못 읽어 건너뛴 것)는 건드리지 않는다.

        Rebuild every case's seen / resolved / projects / last texts from ``<kb>/errors.jsonl`` (script counts and
        people's keys untouched; cases for L2s that only errors.jsonl knows are made). Returns the cases. The cases in
        ``skip`` (a re-sign could not read them) are left alone.

        v2.1e: every stored version's ``runs`` are rebuilt from the entries' tries (merged with the runs the store
        already has: those keep their error counts; deduplicated by run_id + round + try n), ``parent`` / ``reason``
        filled where an ``llm-augment`` try shows them, ``last_failure`` = the newest failed run's reason; tries with
        a diff whose 수정 notes lack ``hunks`` get them (``sbmig.script.notes.attach_hunks``) — errors.jsonl and those
        entries' JSON files are rewritten once. v2.1f: build-resolved entries without ``resolved_by`` get the
        probable fixer (also when ``attach_hunks`` is not importable). One INFO line says how many of each
        (``last_reindex["hunks"]`` / ``["resolved_by"]``, counted separately, fresh every call)."""
        with stage("kb"), self.lock():
            return self._recount_locked(skip=set(skip or ()))

    @staticmethod
    def _slim(e: ErrorEntry) -> ErrorEntry:
        # 다시 세기에 필요 없는 큰 텍스트(raw, 시도의 diff)는 들고 있지 않는다 — 기억 공간은 항목 수에 비례
        return e.model_copy(update={"raw": "", "tries": [t.model_copy(update={"diff": ""}) for t in e.tries]})

    def _recount_locked(self, skip: Iterable[str] = ()) -> int:
        """:meth:`recount` 의 본문(잠금은 호출자가 — 재서명·가져오기가 같은 잠금 안에서 부른다). 저장된 L2 와 다른
        L2 의 항목(정확한 키)은 그 케이스에 셈하지 않는다.

        The body of :meth:`recount` (the caller holds the lock); entries whose L2 is not the case's are not counted
        into it (the exact-key guard)."""
        skip = set(skip or ())
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
                groups.setdefault(e.case_id, []).append(self._slim(e))
        ids = (set(self.case_ids()) | {cid for cid, es in groups.items() if any(x.l2 for x in es)}) - skip
        rebuilt = 0
        self._keep_alive()
        for cid in sorted(ids):
            rebuilt += self._recount_one(cid, groups.get(cid, []))
        # 에러별 JSON 이 생기기 전에 저장된 항목(또는 사람이 지운 파일)을 errors.jsonl 로부터 다시 만든다
        # the readable per-error files for entries stored before they existed (or deleted by hand)
        missing = [e for e in self.iter_errors() if e.case_id not in skip and not self.entry_file(e).is_file()]
        if missing:
            log.info("writing %d per-error JSON file(s) missing under cases/*/errors/", len(missing))
            self._write_entry_files(missing)
        self._write_md_locked()
        self.last_reindex = {"cases": len(ids), "runs": rebuilt, "hunks": hunked, "resolved_by": fixers}
        log.info("reindex: %d script run record(s) rebuilt, %d entr(ies) given as-is/to-be hunks, %d build-resolved "
                 "entr(ies) given a probable fixer", rebuilt, hunked, fixers)
        return len(ids)

    def _recount_cases(self, ids: Iterable[str]) -> int:
        """케이스 몇 개만 다시 센다(자동 재서명 뒤): errors.jsonl 을 한 번 흘려 읽으며 그 케이스의 줄만 파싱한다(그
        항목의 빠진 에러별 JSON 도 이때 쓴다). 변경 조각 채우기는 없다 — 전체 재색인은 ``sbmig kb reindex`` 의 몫.

        Recount a few cases only (after an automatic re-sign): one streaming pass over errors.jsonl parses only their
        lines (writing their missing per-entry files on the way); no hunk backfill — the full recount is what
        ``sbmig kb reindex`` does."""
        from sbmig.kb.jsonl import parse_record

        want = {c for c in ids if valid_case_id(c)}
        if not want:
            return 0
        groups: dict[str, list[ErrorEntry]] = {}
        if self.errors_path.is_file():
            with open(self.errors_path, "rb") as fh:
                for b in fh:
                    text = b.decode("utf-8", "replace")
                    if line_case_id(text) not in want:
                        continue
                    e = parse_record(text, ErrorEntry)
                    if e is None or e.case_id not in want:
                        continue
                    if self.json_files and not self.entry_file(e).is_file():
                        self._write_entry_files([e])
                    if e.status not in ("note", "pom-verify"):
                        groups.setdefault(e.case_id, []).append(self._slim(e))
        n = 0
        for cid in sorted(want):
            if (self.cases_dir / cid / CASE_FILE).is_file() or any(x.l2 for x in groups.get(cid, [])):
                n += self._recount_one(cid, groups.get(cid, []))
        self._write_md_locked()
        return n

    def _recount_one(self, cid: str, es: list[ErrorEntry]) -> int:
        """한 케이스를 그 항목들로 다시 세고 저장한다(만든 실행 기록 수를 돌려줌).

        Recount one case from its entries and save it; returns the run records derived."""
        self._keep_alive()                                # every case.json is rewritten: keep the lock fresh
        first = es[0] if es else None
        try:
            case, extra = self._case_or_new(cid, first.l2 if first else "",
                                            case_name(first.message, first.l2) if first else "",
                                            first.kind if first else None)
        except CaseUnreadable as ex:
            log.warning("kb reindex: case %s skipped (%s) — run `sbmig kb reindex` again later", cid, ex)
            return 0
        if case.l2 and any(x.l2 and x.l2 != case.l2 for x in es):
            other = next(x for x in es if x.l2 and x.l2 != case.l2)
            self._mismatch(cid, case.l2, other.l2)
            es = [x for x in es if not (x.l2 and x.l2 != case.l2)]
        # 처음부터 다시 센다: 실행별로 묶어 _apply 하면
        # record_run 이 실행 끝마다 센 것과 같은 결과가 된다
        case.seen, case.resolved, case.projects = 0, 0, []
        runs: dict[str, list[ErrorEntry]] = {}
        for e in es:
            runs.setdefault(e.run_id, []).append(e)
        for run_es in runs.values():
            self._apply(case, extra, run_es)
        rebuilt = 0
        try:
            rebuilt = self._rebuild_runs(case, es)
        except Exception as e:  # noqa: BLE001 - the run history is extra; the counts are saved anyway
            log.warning("kb reindex: runs of case %s not rebuilt: %s: %s", cid, type(e).__name__, e)
        self._save(case, extra)
        return rebuilt

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
            self._keep_alive()
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
            "sig": self.marker_sig(),                  # v2.1z-ai: the rule version kb.json says (0 = none yet)
        }

    def validate(self, limit: int = 20) -> list[str]:
        """``sbmig kb validate``: KB 파일의 문제를 한 줄씩 — ErrorEntry 가 아닌
        errors.jsonl 줄, 못 읽거나 다른 케이스를 가리키는 case.json, L2 해시와 맞지 않는 폴더, 파일 없는
        스크립트·목록에 없는 스크립트 파일, 잘못된 pin.

        Problems of the KB files, one line each: errors.jsonl lines that are no ErrorEntry, case.json files that
        do not read / name another case, scripts listed without a file or files no script lists, bad pins."""
        from sbmig.kb.jsonl import parse_record

        problems: list[str] = []
        l2s: dict[str, set[str]] = {}                     # v2.1z-ai: the L2s stored under each case id
        older: set[str] = set()
        older_cases: set[str] = set()
        v = current_version()
        skipped = self.skipped_cases()
        if self.errors_path.is_file():
            self._register_prefixes()
            bad = []
            with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    e = parse_record(line, ErrorEntry)
                    if e is None:
                        bad.append(n)
                        continue
                    if e.l2 and valid_case_id(e.case_id):
                        l2s.setdefault(e.case_id, set()).add(e.l2)
                    # 옛 규칙의 기록 중 재서명이 실제로 옮길 것만 센다(도장만 받을 기록은 문제가 아니다)
                    if e.l2 and int(e.sig or 0) < v and e.case_id not in skipped \
                            and resigned_l2(e, self.signer) != e.l2:
                        older.add(e.id)
                        older_cases.add(e.case_id)
            problems += [f"{KB_ERRORS}: line {n} is no ErrorEntry" for n in bad[:limit]]
            if len(bad) > limit:
                problems.append(f"{KB_ERRORS}: … {len(bad) - limit} more unreadable line(s)")
            if older:
                problems.append(f"{KB_ERRORS}: {len(older)} record(s) in {len(older_cases)} case(s) signed by older "
                                f"rules (version < {v}): run sbmig kb reindex")
        for cid, why in sorted(skipped.items())[:limit]:
            problems.append(f"{KB_MARKER}: case {cid} was skipped by the last re-sign ({why}) — fix it, then run "
                            "sbmig kb reindex")
        if self.cases_dir.is_dir():
            found = []
            # cases/_other 은 케이스 없는 항목의 에러별 JSON 폴더다 — 케이스가 아니다
            for d in sorted(p for p in self.cases_dir.iterdir() if p.is_dir() and p.name != OTHER_DIR):
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
                others = sorted(x for x in l2s.get(d.name, ()) if case.l2 and x != case.l2)
                if others:
                    found.append(f'{rel}/{CASE_FILE}: l2 "{cut(case.l2, 100)}" is not the L2 of {len(others)} '
                                 f'signature(s) stored under its id in {KB_ERRORS} ("{cut(others[0], 100)}") — the '
                                 "case id is held by two signatures")
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

    # ------------------------------------------------------------------ v2.1z-ai: signature versions, re-sign
    @property
    def marker_path(self) -> Path:
        """``<kb>/kb.json`` — KB 가 마지막으로 재서명된 규칙 버전(``{"sig": N, "resigned_at": …, "skipped": {…}}``)."""
        return self.root / KB_MARKER

    @property
    def pending_path(self) -> Path:
        """``<kb>/resign-pending.json`` — 재서명이 아직 끝내지 못한 폴더 정리(다음 재서명이 먼저 마친다)."""
        return self.root / RESIGN_PENDING

    def marker(self) -> dict[str, Any]:
        """``kb.json`` 의 내용(없거나 못 읽으면 빈 dict).

        The content of ``kb.json`` ({} when missing / unreadable)."""
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def marker_sig(self) -> int:
        """``kb.json`` 의 규칙 버전(없거나 못 읽으면 0). 작은 파일 하나 읽기 — 첫 ``match`` 의 확인이 이것뿐이다.

        The rule version ``kb.json`` names (0 when missing / unreadable); one small file read."""
        try:
            return int(self.marker().get("sig") or 0)
        except (TypeError, ValueError):
            return 0

    def skipped_cases(self) -> dict[str, str]:
        """지난 재서명이 건너뛴 케이스(ID → 이유, ``kb.json`` 의 ``skipped``). 자동 재서명은 이것들을 다시 보지 않는다 —
        ``sbmig kb reindex`` 만 다시 해 본다.

        The cases the last re-sign skipped (id → reason, ``kb.json`` "skipped"); only ``sbmig kb reindex`` retries
        them, the automatic paths leave them."""
        got = self.marker().get("skipped")
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}

    def _write_marker(self, skipped: Optional[dict[str, str]] = None, *, sig: Optional[int] = None,
                      resigning: Optional[Iterable[str]] = None) -> None:
        data: dict[str, Any] = {"sig": current_version() if sig is None else sig, "resigned_at": utcnow().isoformat(),
                                "skipped": dict(sorted((skipped or {}).items()))}
        if resigning is not None:
            data["resigning"] = sorted(resigning)        # a re-sign is under way: these cases are recounted after it
        atomic_write(self.marker_path, json.dumps(data, ensure_ascii=False) + "\n")

    def has_records(self) -> bool:
        """errors.jsonl 이 있고 비어 있지 않은가(크기만 본다).

        ``errors.jsonl`` exists and is not empty (a stat)."""
        try:
            return self.errors_path.stat().st_size > 0
        except OSError:
            return False

    def _register_prefixes(self) -> None:
        """설정의 사내 라이브러리 접두어를 서명 규칙에 등록한다(``MavenRunner`` 가 하는 것과 같은 호출) — 그래야 CLI
        재서명도 실행 중의 서명과 같은 '라이브러리 타입' 키를 만든다. 설정이 없으면 WARNING 한 번.

        Register the configured in-house package prefixes with the signature rules (the call ``MavenRunner`` makes),
        so a CLI re-sign keys in-house owner types like a live run. Without a cfg: one WARNING."""
        from sbmig.build.signatures import external_prefixes_from_config, register_external_prefixes

        try:
            register_external_prefixes(external_prefixes_from_config(self.cfg))
            return
        except Exception as e:  # noqa: BLE001 - no cfg / no artifacts section
            why = f"{type(e).__name__}: {e}"
        key = (str(self.root), "prefixes")
        if key not in _MISMATCH_SAID:
            _MISMATCH_SAID.add(key)
            log.warning("kb re-sign: the in-house package prefixes are unknown here (%s) — members of in-house types "
                        "are keyed with what is registered in this process", why)

    def _older_scan(self, v: int, skipped: Iterable[str]) -> tuple[int, set[str]]:
        """errors.jsonl 한 번 흘려 읽기: (옛 규칙의 레코드 줄 수, 그 줄들의 케이스 ID). 줄마다 정규식, 후보만 파싱.

        One streaming pass: (older records, their case ids); a regex per line, a parse for candidates only."""
        n, ids = 0, set()
        if not self.errors_path.is_file():
            return 0, ids
        skip = set(skipped or ())
        with open(self.errors_path, "rb") as fh:
            for b in fh:
                e = older_record(b.decode("utf-8", "replace"), v, skip)
                if e is not None:
                    n += 1
                    if valid_case_id(e.case_id):
                        ids.add(e.case_id)
        return n, ids

    def _load_any(self, case_id: str) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """case.json 을 이름 확인 없이 읽는다(중단된 폴더 이름 바꾸기가 남긴, 옛 ID 를 가리키는 파일을 알아보려고).

        Read ``case.json`` without the name check (an interrupted folder rename leaves one naming the old id)."""
        if not valid_case_id(case_id):
            return None
        try:
            data = json.loads((self.cases_dir / case_id / CASE_FILE).read_bytes().decode("utf-8-sig"))
            case = CaseInfo.model_validate(data)
        except (OSError, ValueError):
            return None
        return case, {k: v for k, v in data.items() if k not in CaseInfo.model_fields}

    def _affected_lines(self, ids: set[str], signer: Optional[Signer], v: int
                        ) -> tuple[dict[int, ErrorEntry], dict[int, bytes], Any]:
        """두 번째 흘려 읽기: 맨 위 ``case_id`` 가 ``ids`` 인 줄만 파싱하고, 읽는 자리에서 그 항목의 재서명 계획(새 L2 /
        도장)을 정한 뒤 큰 텍스트(raw, diff)를 버린 항목만 들고 있는다 — 기억 공간은 영향받은 케이스의 항목 수에
        비례한다. 찢긴 접두어 뒤의 레코드를 살린 줄은 그 접두어 바이트도 — 다시 쓸 때 ``.corrupt`` 로 옮긴다.

        Second streaming pass: parse only the lines whose top-level ``case_id`` is in ``ids``, decide each entry's
        plan (new L2 / stamp) right there, keep the entry without its big texts (raw, diffs) — memory proportional to
        the affected cases' entries. For a record recovered after a torn prefix, the prefix bytes too (moved to
        ``.corrupt`` on the rewrite). Returns (line number → slim entry, line number → prefix, the EntryPlan)."""
        from sbmig.kb.jsonl import parse_record

        from .resign import EntryPlan

        out: dict[int, ErrorEntry] = {}
        torn: dict[int, bytes] = {}
        plan = EntryPlan()
        if not ids or not self.errors_path.is_file():
            return out, torn, plan
        with open(self.errors_path, "rb") as fh:
            for i, b in enumerate(fh):
                text = b.decode("utf-8", "replace")
                if line_case_id(text) not in ids:
                    continue
                body = text.strip()
                try:
                    e = ErrorEntry.model_validate_json(body)
                except ValueError:
                    e = parse_record(body, ErrorEntry)
                    if e is not None:
                        torn[i] = body[: body.rfind('{"id":"', 1)].encode("utf-8")
                if e is not None and e.case_id in ids:
                    one = plan_entries({i: e}, signer, v)
                    plan.stamp |= one.stamp
                    plan.new_l2.update(one.new_l2)
                    out[i] = self._slim(e)
                if i % 2000 == 0:
                    self._keep_alive()
        return out, torn, plan

    def resign(self, dry_run: bool = False, signer: Optional[Signer] = None, *, recount: bool = True,
               retry_skipped: bool = True) -> ResignReport:
        """재서명: 옛 규칙으로 서명된 기록(그 뒤로 규칙이 바뀐 분류만)을 지금 규칙의 케이스 ID 로 옮긴다(KB 잠금 안).
        그런 줄이 없고 ``kb.json`` 이 지금 버전이면 errors.jsonl 한 번 흘려 읽고 아무것도 쓰지 않는다. ``dry_run`` 은
        같은 보고만 만들고 아무것도 쓰지 않는다. ``signer`` 는 테스트가 규칙을 바꿔 끼우는 자리(기본 = 지금 규칙).
        ``recount`` 면 옮긴 케이스들만 다시 센다. ``retry_skipped``(명시적 호출)면 지난번에 건너뛴 케이스도 다시 해
        본다 — 자동 경로는 False. 잠금을 못 잡으면 WARNING 과 ``error`` 가 든 보고(아무것도 안 씀).

        Re-sign: move the records signed by older rules (only classes the rules changed since) to the case ids the
        current rules give, under the KB lock (docs/INTERFACES.md "v2.1z-ai"). Fast path: no such line and
        ``kb.json`` current → one streaming read, nothing written. ``dry_run``: the same report, nothing written.
        ``signer`` = the rule (tests inject one). ``recount``: the affected cases are recounted. ``retry_skipped``
        (explicit calls): the cases the last re-sign skipped are tried again; the automatic paths pass False.
        Without the lock: a WARNING and a report with ``error``."""
        if not self.root.exists():
            return ResignReport(version=current_version(), dry_run=dry_run)
        with stage("kb"), self.lock() as held:
            if not held and not dry_run:
                rep = ResignReport(version=current_version(), error="the KB lock was not taken — nothing re-signed")
                log.warning("kb re-sign skipped: the KB lock of %s was not taken — the KB stays as it is", self.root)
                return rep
            return self._resign_locked(dry_run, signer, recount, retry_skipped=retry_skipped)

    def _resign_locked(self, dry_run: bool, signer: Optional[Signer], recount: bool = True, *,
                       force: bool = False, retry_skipped: bool = True) -> ResignReport:
        """:meth:`resign` 의 본문(잠금은 호출자가). ``force`` 는 빠른 길을 건너뛴다(가져오기).

        The body of :meth:`resign` (the caller holds the lock); ``force`` skips the fast path (an import)."""
        v = current_version()
        signer = signer or self.signer
        rep = ResignReport(version=v, dry_run=dry_run)
        marker = self.marker()
        prev_skipped = {} if retry_skipped else self.skipped_cases()
        recover: set[str] = set()
        if not dry_run:
            if self.pending_path.exists():
                recover |= self._finish_pending()         # an interrupted re-sign's clean-up first
            if isinstance(marker.get("resigning"), list):
                recover |= {str(x) for x in marker["resigning"] if valid_case_id(str(x))}
        n_older, affected = self._older_scan(v, prev_skipped)
        try:
            msig = int(marker.get("sig") or 0)
        except (TypeError, ValueError):
            msig = 0
        if not (force or recover) and msig >= v and n_older == 0:
            return rep                                    # the fast path: one streaming read, nothing written
        self._register_prefixes()
        entries, torn, eplan = self._affected_lines(affected, signer, v)
        groups: dict[str, list[int]] = {}
        for i, e in sorted(entries.items()):
            if e.l2 and valid_case_id(e.case_id):
                groups.setdefault(e.case_id, []).append(i)
        skipped: set[int] = set()
        plans: list[_CasePlan] = []
        existed: set[str] = set()

        def target(i: int) -> str:
            return case_id_for(eplan.new_l2[i]) if i in eplan.new_l2 else entries[i].case_id

        for old in sorted(groups):
            self._keep_alive()
            idxs = groups[old]
            hit, state, detail = self._load(old)
            if state in ("empty", "damaged", "error"):
                # 못 읽는 케이스의 항목은 옛 ID·옛 버전 그대로(다음 명시적 재서명이 다시 해 본다)
                self._resign_skip(rep, old, f"cases/{old}/{CASE_FILE} {detail or state}")
                skipped.update(idxs)
                continue
            # 정확한 키: case.json 의 L2 가 아닌 항목은 자기 ID 로 옮겨 가되 이 케이스의 스크립트를 데려가지 않는다
            owners = [i for i in idxs if hit is None or not hit[0].l2 or entries[i].l2 == hit[0].l2]
            targets: dict[str, list[int]] = {}
            for i in owners:
                targets.setdefault(target(i), []).append(i)
            if set(targets) <= {old}:
                continue
            l2s = {n: (eplan.new_l2[ix[0]] if ix[0] in eplan.new_l2 else entries[ix[0]].l2)
                   for n, ix in targets.items()}
            why = self._resign_targets_bad(old, hit, l2s)
            if why:
                self._resign_skip(rep, old, why)
                skipped.update(idxs)
                continue
            existed |= {n for n in targets if n != old and self._load(n)[1] == "ok"}
            plans.append(_CasePlan(old, hit, targets, l2s))
        if dry_run:
            self._resign_fill(rep, eplan, plans, skipped, existed)
            rep.cases_stamped = sum(1 for o in groups if o not in {p.old for p in plans if p.whole}
                                    and any(i in eplan.stamp and i not in skipped for i in groups[o]))
            return rep

        touched = set(groups) | {n for p in plans for n in p.targets} | recover
        # 쓰기를 시작하기 전에 '진행 중' 을 kb.json 에 적는다: 어디서 죽든 다음 재서명이 빠른 길로 빠지지 않고 이
        # 케이스들을 다시 센다
        if plans or eplan.stamp:
            self._write_marker(dict(prev_skipped), sig=0, resigning=touched)
        ctx = _ResignCtx()
        done: list[_CasePlan] = []
        for p in plans:
            self._keep_alive()
            try:
                self._resign_place(p, entries, ctx)
                done.append(p)
                if p.whole:
                    ctx.whole[p.old] = next(iter(p.targets))
            except OSError as ex:                         # CaseUnreadable / a failing copy: this case only
                self._resign_skip(rep, p.old, f"{type(ex).__name__}: {ex}")
                skipped.update(groups.get(p.old, []))
        moved = self._resign_entries(entries, eplan, skipped, ctx, v)
        for n, versions in ctx.shared.items():            # a version copied to several parts: counted per part
            if n in ctx.saves and versions:
                part = [entries[i].model_copy(update=moved[i]) if i in moved else entries[i]
                        for i in ctx.parts.get(n, []) if i in entries]
                self._recount_versions(ctx.saves[n][0], versions, part)
        # 1) 새 폴더·스크립트는 위에서 → 2) case.json 들(옮겨 받은 케이스, 다시 계산된 기록을 가진 케이스만)
        whole_sources = {p.old for p in done if p.whole}
        for n, (case, extra) in ctx.saves.items():
            self._keep_alive()
            case.sig = max(int(case.sig or 0), v)
            case.updated_at = utcnow()
            self._save(case, extra)
        for old, idxs in sorted(groups.items()):
            if old in ctx.saves or old in whole_sources or old in rep.unreadable:
                continue
            if not any(i in eplan.stamp and i not in skipped for i in idxs):
                continue
            hit = self._read(old, quiet=True)
            if hit is None or int(hit[0].sig or 0) >= v:
                continue
            self._keep_alive()
            self._save(hit[0].model_copy(update={"sig": v}), hit[1])
            rep.cases_stamped += 1
        # 3) 정리할 폴더 목록(중단되면 다음 재서명이 마저 함) → 4) errors.jsonl 을 흘려 복사하며 한 번 원자적으로
        if ctx.journal["remove"] or ctx.journal["trim"]:
            atomic_write(self.pending_path, json.dumps(ctx.journal, ensure_ascii=False, indent=2) + "\n")
        if moved or ctx.whole:
            self._resign_write_entries(moved, torn, ctx.whole)
        # 5) 에러별 JSON 을 새 케이스 폴더로 → 6) 비운 옛 폴더 정리 → 7) 다시 세기 → 8) kb.json
        self._resign_move_entry_files(entries, moved)
        entries.clear()                                   # the recount below reads its own (slim) entries
        if self.pending_path.exists():
            self._finish_pending()
        self._resign_fill(rep, eplan, done, skipped, existed)
        for o, n, l2 in rep.moves:
            log.info("kb re-sign: case %s → %s (%s)", o, n, cut(l2))
        for o, ns in rep.splits:
            log.info("kb re-sign: case %s split → %s", o, ", ".join(ns))
        new_skipped = {k: why for k, why in prev_skipped.items() if k not in touched}
        new_skipped.update(rep.reasons)
        if rep.changed or recover:
            if recount:
                # 건너뛴 케이스는 다시 세지 않는다(못 읽는 case.json 위에 새 파일을 쓰지 않게)
                self._recount_cases((touched | ctx.saves.keys()) - set(new_skipped))
            else:
                self._write_md_locked()
        self._write_marker(new_skipped)
        log.info("%s", rep.summary())
        return rep

    @staticmethod
    def _resign_skip(rep: ResignReport, case_id: str, why: str) -> None:
        if case_id not in rep.unreadable:
            rep.unreadable.append(case_id)
        rep.reasons[case_id] = why
        log.warning("kb re-sign: case %s skipped (%s) — its records keep their ids; `sbmig kb reindex` tries again",
                    case_id, why)

    def _resign_targets_bad(self, old: str, hit: Optional[tuple[CaseInfo, dict[str, Any]]],
                            l2s: dict[str, str]) -> str:
        """옮겨 갈 케이스들이 받을 수 있나: 못 읽는 case.json, 다른 L2 가 차지한 케이스면 이유 문장("" = 괜찮음).
        옛 ID 를 가리키는 case.json(중단된 폴더 이름 바꾸기)은 괜찮다 — 이번에 고친다.

        Can every target take the case? The reason when one does not read or holds another L2 ("" = fine). A
        case.json naming the old id (an interrupted rename) is fine: it is repaired now."""
        for n, l2 in l2s.items():
            if n == old:
                continue
            t, state, detail = self._load(n)
            if state == "ok" and t is not None:
                if t[0].l2 and l2 and t[0].l2 != l2:
                    self._mismatch(n, t[0].l2, l2)
                    return f"case {n} holds another signature"
            elif state in ("empty", "damaged", "error"):
                anyhit = self._load_any(n)
                if hit is None and anyhit is not None and anyhit[0].case_id == old:
                    continue
                return f"cases/{n}/{CASE_FILE} {detail or state}"
        return ""

    @staticmethod
    def _resign_fill(rep: ResignReport, eplan: Any, plans: list["_CasePlan"], skipped: set[int],
                     existed: set[str]) -> None:
        """보고의 숫자·목록을 계획에서 채운다(``dry_run`` 은 쓰기 전에, 실제 실행은 쓴 뒤에).

        Fill the report from the plans (before writing for a dry run, after it otherwise)."""
        rep.entries_stamped = len(eplan.stamp - skipped)
        rep.entries_resigned = len(set(eplan.new_l2) - skipped)
        rep.moves, rep.splits, rep.merged = [], [], 0
        for p in plans:
            if p.whole:
                (n,) = p.targets
                rep.moves.append((p.old, n, p.l2.get(n, "")))
            else:
                rep.splits.append((p.old, sorted(p.targets)))
            rep.merged += sum(1 for n in p.targets if n != p.old and n in existed)

    def _resign_place(self, p: "_CasePlan", entries: dict[int, ErrorEntry], ctx: "_ResignCtx") -> None:
        """한 케이스의 폴더·스크립트를 옮긴다(재서명 1단계). 통째 이동: 대상 폴더가 없으면 이름 바꾸기, 있으면 합치기
        (내용이 같은 스크립트는 하나, 겹치는 번호는 다음 빈 번호). 나뉨: 버전마다 그것을 돌린 시도가 있는 쪽으로(아무도
        안 돌린 것은 남는 쪽 또는 가장 큰 쪽). 옛 폴더 정리는 errors.jsonl 다시 쓰기 뒤에(``ctx.journal``).

        Move one case's folder / scripts (re-sign step 1): a whole move renames the folder when the target does not
        exist, else merges (same code once, a clashing number takes the next free one); a split sends each version
        to every part one of whose entries ran it (a version nobody ran stays with the old case, or goes to the
        largest part). The old folder's clean-up is queued in ``ctx.journal`` (after errors.jsonl)."""
        v = current_version()
        old = p.old
        o_case = p.hit[0] if p.hit else None
        o_extra = dict(p.hit[1]) if p.hit else {}
        versions = sorted(o_case.scripts, key=lambda s: s.version) if o_case else []
        first = next((entries[i] for ix in p.targets.values() for i in ix if i in entries), None)
        msg = first.message if first is not None else ""
        if p.whole:
            (n,) = p.targets
            if o_case is None:
                # 옛 폴더가 없다: 항목만 있던 케이스, 또는 이름 바꾸기 뒤 case.json 을 고치기 전에 멈춘 재서명
                anyhit = self._load_any(n)
                if anyhit is not None and anyhit[0].case_id == old and n not in ctx.saves:
                    case = anyhit[0].model_copy(deep=True)
                    self._become(case, n, p.l2[n], old, [], msg, v)
                    ctx.saves[n] = (case, dict(anyhit[1]))
                ctx.vmaps[(old, n)] = {}
                return
            nd = self.cases_dir / n
            if n not in ctx.saves and not os.path.lexists(nd):
                try:
                    os.rename(self.cases_dir / old, nd)
                except OSError as e:
                    log.debug("kb re-sign: cases/%s not renamed to %s (%s) — copied instead", old, n, e)
                else:
                    case = o_case.model_copy(deep=True)
                    self._become(case, n, p.l2[n], old, o_case.aliases, msg, v)
                    ctx.saves[n] = (case, o_extra)
                    ctx.vmaps[(old, n)] = {}
                    return
            assign = {s.version: {n} for s in versions}
        else:
            kept = old in p.targets
            ran: dict[int, set[str]] = {}
            for n, ix in p.targets.items():
                for i in ix:
                    for t in (entries[i].tries if i in entries else []):
                        if t.script_version and t.source in RUN_SOURCES:
                            ran.setdefault(int(t.script_version), set()).add(n)
            largest = sorted(p.targets, key=lambda n: (-len(p.targets[n]), n))[0]
            home = old if kept else largest
            assign = {s.version: (ran.get(s.version) or {home}) for s in versions}
        shared = {ver for ver, ns in assign.items() if len(ns) > 1}
        # 대상 케이스를 먼저 모두 확인한다(CaseUnreadable 이면 아무 스크립트도 복사하기 전에 이 케이스를 건너뜀)
        resolved: dict[str, tuple[CaseInfo, dict[str, Any]]] = {}
        for n in sorted(x for x in p.targets if x != old):
            if n in ctx.saves:
                resolved[n] = ctx.saves[n]
                continue
            hit = self._read(n, quiet=True) or self._confirm_new(n)
            if hit is None:
                kind = o_case.kind if o_case else (first.kind if first is not None else ErrorKind.UNKNOWN)
                hit = (CaseInfo(case_id=n, l2=p.l2[n], name=case_name(msg, p.l2[n]), kind=kind), {})
            else:
                hit = (hit[0].model_copy(deep=True), dict(hit[1]))
            resolved[n] = hit
        staged: dict[str, tuple[CaseInfo, dict[str, Any], dict[int, int]]] = {}
        for n, (tc, tx) in resolved.items():
            self._keep_alive()
            vs = [s for s in versions if n in assign.get(s.version, set())]
            vmap = self._copy_versions(old, vs, tc)
            for k, val in o_extra.items():                # pin and other human keys travel; the target's win
                tx.setdefault(k, val)
            self._become(tc, n, p.l2[n], old, o_case.aliases if o_case else [], "", v)
            if o_case is not None and not tc.last_solution and o_case.last_solution:
                tc.last_analysis, tc.last_solution = o_case.last_analysis, o_case.last_solution
            staged[n] = (tc, tx, vmap)
        # 모든 대상이 끝난 뒤에만 계획에 반영한다(중간에 실패하면 이 케이스는 통째로 건너뛴 것이 된다)
        for n, (tc, tx, vmap) in staged.items():
            ctx.vmaps[(old, n)] = vmap
            ctx.saves[n] = (tc, tx)
            ctx.parts.setdefault(n, []).extend(p.targets[n])
            if shared:
                ctx.shared.setdefault(n, set()).update(vmap[x] for x in shared if x in vmap)
        if o_case is None:
            return
        moved_ix = [i for n, ix in p.targets.items() if n != old for i in ix if i in entries]
        expect = None
        if moved_ix:
            expect = [entries[moved_ix[0]].id, next(n for n, ix in p.targets.items() if moved_ix[0] in ix)]
        others = sorted(x for x in p.targets if x != old)
        if old in p.targets:
            keep = sorted(ver for ver, ns in assign.items() if old in ns)
            ctx.journal["trim"][old] = {"keep": keep, "shared": sorted(x for x in shared if x in keep),
                                        "targets": others, "expect": expect}
        else:
            ctx.journal["remove"][old] = {"targets": others, "expect": expect}

    @staticmethod
    def _become(case: CaseInfo, n: str, l2: str, old: str, old_aliases: Iterable[str], msg: str, v: int) -> None:
        """케이스를 ID ``n`` / L2 ``l2`` / 지금 버전으로 고치고 옛 ID 들을 aliases 에 더한다(순서 유지, 중복 없음).
        aliases 는 사람의 조회용일 뿐이다 — 재서명은 그것으로 아무것도 합치지 않는다.

        Make ``case`` the case ``n`` (L2, current version) and add the old ids to its aliases (a lookup aid for
        people only: nothing is ever merged by an alias)."""
        renamed = case.case_id != n
        case.case_id = n
        if l2 and (renamed or not case.l2):
            if case.l2 != l2 and msg:
                case.name = case_name(msg, l2)
            case.l2 = l2
        case.sig = max(int(case.sig or 0), v)
        case.aliases = [a for a in dict.fromkeys([*case.aliases, old, *old_aliases]) if a and a != n]

    def _script_bytes(self, case_id: str, version: int) -> Optional[bytes]:
        try:
            return (self.case_dir(case_id) / f"fix_v{int(version)}.py").read_bytes()
        except (OSError, ValueError):
            return None

    def _copy_versions(self, old: str, versions: list[CaseScript], tc: CaseInfo) -> dict[int, int]:
        """``old`` 의 스크립트 버전들을 케이스 ``tc`` 로 복사하고 (옛 번호 → 새 번호)를 돌려준다. 대상 폴더에 바이트가
        같은 파일이 이미 있으면(앞선 재서명이 복사했거나 같은 코드) 그 번호를 쓰고 기록을 합친다 — ``merge_from`` 의
        규칙 그대로라 성공·실패 수는 두 기록의 max(합이 아님, 정확한 수가 아니다), 프로젝트 합집합, 실행 기록 합침.
        새 폴더면 번호를 지키고, 있는 폴더면 다음 빈 번호.

        Copy ``old``'s versions into ``tc``; returns old number → new number. A byte-identical file already in the
        target (an earlier, interrupted re-sign, or the same code) is reused and the records merged by the rules
        ``merge_from`` uses — the counts are the max of the two records (not a sum: not exact), projects united,
        runs merged. In a new folder the numbers are kept, in an existing one a clash takes the next free."""
        n = tc.case_id
        vmap: dict[int, int] = {}
        disk: dict[str, int] = {}
        for dv in sorted(self._disk_versions(n)) if (self.cases_dir / n).is_dir() else []:
            data = self._script_bytes(n, dv)
            if data is not None:
                disk.setdefault(hashlib.sha1(data).hexdigest(), dv)
        fresh = not tc.scripts and not disk
        listed = {s.version: s for s in tc.scripts}
        for s in sorted(versions, key=lambda x: x.version):
            self._keep_alive()
            data = self._script_bytes(old, s.version)
            if data is None:
                log.warning("kb re-sign: cases/%s/fix_v%d.py does not read — that version is not copied to %s", old,
                            s.version, n)
                continue
            h = hashlib.sha1(data).hexdigest()
            nv = disk.get(h)
            if nv is not None:
                vmap[s.version] = nv
                have = listed.get(nv)
                if have is None:                          # claimed by an interrupted re-sign, never listed
                    have = self._copied(s, nv, vmap)
                    tc.scripts.append(have)
                    listed[nv] = have
                else:
                    have.successes, have.failures = max(have.successes, s.successes), max(have.failures, s.failures)
                    have.projects += [x for x in s.projects if x not in have.projects]
                    if s.status == "proven" and have.status == "unverified":
                        have.status = "proven"
                    self._merge_runs(have, s, vmap)
                continue
            nv = self._claim_script(n, s.version if fresh else self._next_version(tc), data)
            vmap[s.version] = nv
            new = self._copied(s, nv, vmap)
            tc.scripts.append(new)
            listed[nv] = new
            disk[h] = nv
        tc.scripts.sort(key=lambda x: x.version)
        return vmap

    @staticmethod
    def _copied(s: CaseScript, nv: int, vmap: dict[int, int]) -> CaseScript:
        return s.model_copy(deep=True, update={"version": nv, "file": f"fix_v{nv}.py",
                                               "parent": vmap.get(s.parent) if s.parent else None})

    def _recount_versions(self, case: CaseInfo, versions: set[int], entries: list[ErrorEntry]) -> None:
        """여러 케이스로 나뉜 버전: 이 케이스 몫의 항목들로 실행 기록을 다시 만들고(그 항목들의 시도에 해당하는 저장
        기록만 남김) 성공·실패·프로젝트·상태를 다시 센다(retired 는 그대로). 근사치다: 실행 기록은
        ``errors.script_runs`` 로 잘려 있고 시도 번호 없이 적힌 기록은 버려지므로, 나뉜 버전의 수는 정확한 수가 아니다.

        A version that ended in several cases: its runs rebuilt from this case's entries only (the stored runs kept
        when one of these entries' tries made them), successes / failures / projects / status recounted (retired
        stays). Approximate: the runs are capped at ``errors.script_runs`` and runs without a try number are dropped,
        so the counts of a split version are not exact."""
        keys: dict[int, set[tuple[str, int, int]]] = {}
        for e in entries:
            for t in e.tries:
                if t.script_version and int(t.script_version) in versions:
                    keys.setdefault(int(t.script_version), set()).add((e.run_id, int(t.round), int(t.n)))
        for s in case.scripts:
            if s.version not in versions:
                continue
            mine = keys.get(s.version, set())
            s.runs = [r for r in s.runs if _run_key(r) in mine]
            self._rebuild_runs(CaseInfo(case_id=case.case_id, scripts=[s]), entries)
            s.successes = sum(1 for r in s.runs if r.result in SUCCESS_RESULTS)
            s.failures = sum(1 for r in s.runs if r.result in FAILURE_RESULTS)
            s.projects = list(dict.fromkeys(r.project for r in s.runs if r.result in SUCCESS_RESULTS and r.project))
            if s.status != "retired":
                s.status = "proven" if _resolved_elsewhere(s) else "unverified"

    def _resign_entries(self, entries: dict[int, ErrorEntry], eplan: Any, skipped: set[int],
                        ctx: "_ResignCtx", v: int) -> dict[int, dict[str, Any]]:
        """바뀌는 항목들(줄 번호 → 바꿀 필드): 도장(``sig``), 새 ``case_id`` / ``l2``, ``was`` 에 옛 ID, 시도의 스크립트
        번호(``tries`` 는 번호만 바꾼 목록이 아니라 '옛 번호 → 새 번호' 로 적용됨)와 ``script`` 경로, 통째로 옮긴
        케이스를 가리키는 ``resolved_by``. 항목 ID 는 그대로.

        The entries that change (line number → the fields to update): ``sig``, the new ``case_id`` / ``l2``, the old
        id in ``was``, the tries' script numbers (as ``_vmap``, applied to the full record on the rewrite) and the
        ``script`` path, ``resolved_by`` of a case moved whole."""
        out: dict[int, dict[str, Any]] = {}
        for i, e in entries.items():
            if i in skipped:
                continue
            upd: dict[str, Any] = {}
            if i in eplan.stamp:
                upd["sig"] = v
            if i in eplan.new_l2:
                o, n = e.case_id, case_id_for(eplan.new_l2[i])
                vm = ctx.vmaps.get((o, n), {})
                upd.update(case_id=n, l2=eplan.new_l2[i], was=list(dict.fromkeys([*e.was, o])))
                upd["tries"] = _renumbered(e.tries, vm)
                upd["script"] = _moved_script(e.script, o, n, vm)
                upd["_vmap"] = vm
            rb = _moved_resolved_by(e, ctx.whole)
            if rb is not None:
                upd["resolved_by"] = rb
            if upd:
                out[i] = upd
        return out

    def _resign_write_entries(self, moved: dict[int, dict[str, Any]], torn: dict[int, bytes],
                              whole: dict[str, str]) -> None:
        """errors.jsonl 을 흘려 복사하며 한 번 원자적으로 다시 쓴다: 바뀐 줄만 새로 직렬화, 나머지(못 읽는 줄 포함)는
        바이트 그대로. 통째로 옮긴 케이스를 ``resolved_by`` 로 가리키는 다른 줄도 고친다. 찢긴 접두어 뒤에서 살린
        레코드를 다시 쓰면 그 접두어 바이트는 ``errors.jsonl.corrupt`` 에 덧붙인다(``repair_tail`` 과 같은 곳).

        Rewrite errors.jsonl once by a streaming copy into a temp file, atomically: changed lines re-serialised, every
        other line byte for byte; lines whose ``resolved_by`` names a case moved whole are fixed too. The torn prefix
        of a record recovered from a glued line goes to ``errors.jsonl.corrupt`` (where ``repair_tail`` puts torn
        lines)."""
        from sbmig.kb.jsonl import corrupt_path, parse_record

        self._keep_alive()
        prefixes = [torn[i] for i in sorted(moved) if i in torn and torn[i].strip()]
        keys = set(whole)
        fd, tmp = tempfile.mkstemp(prefix=f".{KB_ERRORS}.", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as dst, open(self.errors_path, "rb") as src:
                for i, b in enumerate(src):
                    nl = b"\n" if b.endswith(b"\n") else b""
                    body = b[:-1] if nl else b
                    if i in moved:
                        full = _full_record(body)
                        if full is not None:
                            body = _updated(full, moved[i]).model_dump_json().encode("utf-8")
                    elif keys and line_case_ids(body) & keys:
                        e = parse_record(body.decode("utf-8", "replace"), ErrorEntry)
                        rb = _moved_resolved_by(e, whole) if e is not None else None
                        if rb is not None:
                            # (a glued line here keeps its torn prefix as it is: only the record part is replaced)
                            text = body.decode("utf-8", "replace")
                            j = text.rfind('{"id":"', 1)
                            try:
                                ErrorEntry.model_validate_json(text.strip())
                                head = ""
                            except ValueError:
                                head = text[:j] if j > 0 else ""
                            body = (head + e.model_copy(update={"resolved_by": rb}).model_dump_json()).encode("utf-8")
                    dst.write(body + nl)
                    if i % 2000 == 0:
                        self._keep_alive()
                dst.flush()
                try:
                    os.fsync(dst.fileno())
                except OSError:
                    pass
            if prefixes:
                with open(corrupt_path(self.errors_path), "ab") as bad:
                    for t in prefixes:
                        bad.write(t + b"\n")
            os.replace(tmp, self.errors_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _resign_move_entry_files(self, entries: dict[int, ErrorEntry], moved: dict[int, dict[str, Any]]) -> None:
        """케이스가 바뀐 항목의 에러별 JSON 을 새 케이스 폴더에 쓰고 옛 파일을 지운다(다시 쓴 errors.jsonl 에서 그 줄만
        읽는다; 파생 파일 — 실패는 WARNING, 빠진 파일은 다시 세기가 만든다).

        The per-entry JSON of every entry whose case changed: written under the new case (read back from the
        rewritten errors.jsonl, those lines only), the old file removed (derived files: a failure is a WARNING, the
        recount writes missing ones)."""
        want = {i for i, u in moved.items() if "case_id" in u and i in entries and u["case_id"] != entries[i].case_id}
        if not want:
            return
        with open(self.errors_path, "rb") as fh:
            for i, b in enumerate(fh):
                if i not in want:
                    continue
                ne = _full_record(b.rstrip(b"\n"))
                if ne is None:
                    continue
                old = entries[i]
                self._keep_alive()
                if self.json_files:
                    try:
                        atomic_write(self.entry_file(ne), json.dumps(ne.model_dump(mode="json"), ensure_ascii=False,
                                                                     indent=2) + "\n")
                    except OSError as ex:
                        log.warning("could not write %s: %s", self.entry_file(ne), ex)
                try:
                    self.entry_file(old).unlink()
                except FileNotFoundError:
                    pass
                except OSError as ex:
                    log.warning("kb re-sign: %s not removed: %s", self.entry_file(old), ex)

    def _finish_pending(self) -> set[str]:
        """``resign-pending.json`` 의 폴더 정리를 마친다 — 단, 그 항목의 errors.jsonl 다시 쓰기가 끝났을 때만(확인용
        항목 하나가 새 케이스 ID 를 갖고 있나). 끝나지 않았으면 그 정리는 버린다(다음 계획이 처음부터 다시 세움). 몇
        번을 다시 해도 같은 결과다. 손댄 케이스 ID 들을 돌려준다(다시 세기용).

        Finish the clean-up queued in ``resign-pending.json`` — each item only when its errors.jsonl rewrite
        completed (the item's check entry carries the new case id); an item whose rewrite did not happen is dropped
        (the next plan starts over from intact folders). Re-runnable. Returns the case ids involved (to recount)."""
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as ex:
            log.warning("kb re-sign: %s does not read (%s) — dropped", self.pending_path, ex)
            data = {}
        items = [(sec, old, item) for sec in ("remove", "trim") for old, item in sorted((data.get(sec) or {}).items())
                 if isinstance(item, dict) and valid_case_id(old)]
        want = {str(it["expect"][0]) for _, _, it in items if isinstance(it.get("expect"), list) and it["expect"]}
        now: dict[str, str] = {}
        if want:
            from sbmig.kb.jsonl import parse_record

            with open(self.errors_path, "rb") as fh:
                for b in fh:
                    text = b.decode("utf-8", "replace")
                    if not any(f'"{w}"' in text for w in want):
                        continue
                    e = parse_record(text, ErrorEntry)
                    if e is not None and e.id in want:
                        now[e.id] = e.case_id
        ids: set[str] = set()
        for sec, old, item in items:
            self._keep_alive()
            exp = item.get("expect")
            if isinstance(exp, list) and len(exp) == 2 and now.get(str(exp[0])) != str(exp[1]):
                log.info("kb re-sign: the clean-up of cases/%s is dropped (its records were not rewritten)", old)
                continue
            targets = [t for t in (item.get("targets") or []) if valid_case_id(str(t))]
            ids |= {old, *targets}
            try:
                if sec == "remove":
                    self._remove_moved(old, targets)
                else:
                    self._trim_moved(old, item, targets)
            except OSError as ex:
                log.warning("kb re-sign: cases/%s not cleaned up (%s) — left as it is", old, ex)
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
        return ids

    def _script_shas(self, ids: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for n in ids:
            if not (self.cases_dir / n).is_dir():
                continue
            for dv in self._disk_versions(n):
                data = self._script_bytes(n, dv)
                if data is not None:
                    out.add(hashlib.sha1(data).hexdigest())
        return out

    def _remove_moved(self, old: str, targets: list[str]) -> None:
        """통째로 옮긴 케이스의 옛 폴더를 지운다 — 모든 ``fix_v*.py`` 가 대상 케이스에 바이트 그대로 있을 때만, 그리고
        파생 파일(case.json, 에러별 JSON, 임시 파일)밖에 없을 때만. 아니면 WARNING 과 함께 그대로 둔다. 스크립트를
        먼저, case.json 을 마지막에 지우므로 도중에 죽어도 다시 하면 끝난다.

        Remove the old folder of a case moved whole — only when every ``fix_v*.py`` exists byte for byte in a target
        and nothing but derived files (case.json, per-entry JSON, temp files) is left; else a WARNING, kept. Scripts
        first, case.json last: re-runnable after a kill."""
        d = self.case_dir(old)
        if not d.is_dir():
            return
        have = self._script_shas(targets)
        scripts, junk, unknown = [], [], []
        for p in sorted(d.iterdir()):
            if p.is_file() and _SCRIPT_RE.match(p.name):
                scripts.append(p)
            elif p.is_file() and (p.name == CASE_FILE or (p.name.startswith(".") and p.name.endswith(".tmp"))):
                junk.append(p)
            elif p.is_dir() and p.name == ENTRIES_DIR and all(x.is_file() and x.suffix in (".json", ".tmp")
                                                              for x in p.iterdir()):
                junk.append(p)
            else:
                unknown.append(p.name)
        lost = [p.name for p in scripts if hashlib.sha1(p.read_bytes()).hexdigest() not in have]
        if lost or unknown:
            log.warning("kb re-sign: cases/%s kept (%s) — remove it by hand once checked", old,
                        f"{', '.join(lost)} not found in {', '.join(targets) or '-'}" if lost
                        else f"it holds {', '.join(unknown[:5])}")
            return
        for p in scripts:
            p.unlink()
        for p in junk:
            if p.is_dir():
                for x in p.iterdir():
                    x.unlink()
                p.rmdir()
            elif p.name != CASE_FILE:
                p.unlink()
        (d / CASE_FILE).unlink(missing_ok=True)           # last: the folder is a case until here
        try:
            d.rmdir()
        except OSError as ex:
            log.debug("kb re-sign: cases/%s not removed (%s)", old, ex)
        log.info("kb re-sign: cases/%s removed (moved to %s)", old, ", ".join(targets))

    def _trim_moved(self, old: str, item: dict[str, Any], targets: list[str]) -> None:
        """나뉜 케이스 중 남는 쪽: 떠난 버전을 목록에서 빼고, 여러 쪽에 있는 버전은 남은 항목으로 다시 센 뒤, 폴더에서
        목록에 없는 ``fix_v*.py`` 중 대상에 바이트 그대로 있는 것을 지운다(파일 기준이라 case.json 저장과 지우기 사이에
        죽어도 다시 하면 끝난다).

        The part a split case keeps: the versions that left are unlisted, a version kept in several parts is
        recounted from the remaining entries, then every unlisted ``fix_v*.py`` that a target has byte for byte is
        removed (decided from the files, so a re-run after a kill between the save and the removals finishes it)."""
        hit = self._read(old)
        if hit is None:
            return
        case, extra = hit
        keep = {int(x) for x in item.get("keep") or []}
        shared = {int(x) for x in item.get("shared") or []}
        have = self._script_shas(targets)
        scripts = []
        for s in case.scripts:
            if s.version not in keep:
                data = self._script_bytes(old, s.version)
                if data is None or hashlib.sha1(data).hexdigest() in have:
                    continue
                keep.add(s.version)
                log.warning("kb re-sign: cases/%s/fix_v%d.py kept (no copy found in %s)", old, s.version,
                            ", ".join(targets) or "-")
            scripts.append(s)
        if len(scripts) != len(case.scripts) or shared & keep:
            case.scripts = scripts
            if shared & keep:
                es = [e for e in self.iter_errors(case=old) if e.case_id == old]
                self._recount_versions(case, shared & keep, es)
            case.updated_at = utcnow()
            self._save(case, extra)
        listed = {s.version for s in case.scripts}
        for dv in self._disk_versions(old):
            if dv in listed:
                continue
            data = self._script_bytes(old, dv)
            if data is not None and hashlib.sha1(data).hexdigest() in have:
                (self.case_dir(old) / f"fix_v{dv}.py").unlink(missing_ok=True)

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
        with stage("kb"), self.lock() as held:
            if other.errors_path.is_file():
                known = self._known_ids()
                lines = [e.model_dump_json() for e in other.iter_errors() if e.id not in known]
                counters["errors_added"] = append_lines(self.errors_path, lines)
            for cid in other.case_ids():
                self._keep_alive()                        # an import copies many cases over NFS
                theirs = other._read(cid)
                if theirs is None:
                    continue
                t_case, t_extra = theirs
                mine = self._read(cid)
                if mine is None:
                    try:
                        mine = self._confirm_new(cid)    # never copy over a case another writer just made
                    except CaseUnreadable as ex:
                        # 이 케이스만 건너뛴다(가져오기는 다시 해도 같은 결과 — 나중에 다시 하면 합쳐진다)
                        log.warning("kb import: case %s skipped (%s) — import the same file again later", cid, ex)
                        counters["cases_skipped"] = counters.get("cases_skipped", 0) + 1
                        continue
                if mine is None and not self._disk_versions(cid):
                    for s in t_case.scripts:
                        code = other.code(cid, s.version)
                        if code is not None:
                            atomic_write_bytes(self.case_dir(cid) / s.file, code.encode("utf-8"))
                    self._save(t_case, t_extra)
                    counters["cases_added"] += 1
                    continue
                if mine is not None and mine[0].l2 and t_case.l2 and mine[0].l2 != t_case.l2:
                    # 정확한 키: 같은 ID 를 다른 L2 가 차지한 케이스에는 합치지 않는다
                    self._mismatch(cid, mine[0].l2, t_case.l2)
                    counters["cases_skipped"] = counters.get("cases_skipped", 0) + 1
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
                    v = self._claim_script(cid, self._next_version(case), code.encode("utf-8"))
                    local[s.version] = v
                    new = s.model_copy(update={"version": v, "file": f"fix_v{v}.py",
                                               "parent": local.get(s.parent) if s.parent else None})
                    case.scripts.append(new)
                    by_sha[_sha(code)] = new
                    counters["scripts_added"] += 1
                if t_case.last_solution and not case.last_solution:
                    case.last_analysis, case.last_solution = t_case.last_analysis, t_case.last_solution
                self._save(case, extra)
            # v2.1z-ai: 가져온 기록은 옛 규칙으로 서명됐을 수 있다 — 같은 잠금 안에서 재서명한 뒤 한 번만 다시 센다
            # (옛 내보내기를 지금 KB 에 넣어도 지금 ID 로 자리 잡는다; 항목 ID 는 바뀌지 않아 중복 거르기는 그대로)
            if not held:
                log.warning("kb import: the KB lock was not taken — no re-sign; run `sbmig kb reindex` later")
            else:
                try:
                    rep = self._resign_locked(False, None, recount=False, force=True)
                    if rep.entries_resigned:
                        counters["entries_resigned"] = rep.entries_resigned
                except Exception as e:  # noqa: BLE001 - the import stands; `sbmig kb reindex` re-signs later
                    log.warning("kb import: re-sign failed (%s: %s) — run `sbmig kb reindex`", type(e).__name__, e)
            self._recount_locked(skip=self.skipped_cases())
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
        # v2.1y: 어느 한쪽이라도 비멱등을 봤으면 합친 KB 에서도 재사용하지 않는다
        have.non_idempotent = bool(have.non_idempotent or theirs.non_idempotent)


# --------------------------------------------------------------------------- re-sign helpers
@dataclass
class _CasePlan:
    """재서명 계획 한 건: 옛 케이스 ``old``(case.json, 없으면 None), 옮겨 갈 케이스마다 그 항목 위치들과 L2.

    One case of a re-sign plan: the old case, every target with its entry positions and its L2."""
    old: str
    hit: Optional[tuple[CaseInfo, dict[str, Any]]]
    targets: dict[str, list[int]]
    l2: dict[str, str]

    @property
    def whole(self) -> bool:
        return self.old not in self.targets and len(self.targets) == 1


@dataclass
class _ResignCtx:
    vmaps: dict[tuple[str, str], dict[int, int]] = field(default_factory=dict)
    saves: dict[str, tuple[CaseInfo, dict[str, Any]]] = field(default_factory=dict)
    shared: dict[str, set[int]] = field(default_factory=dict)
    parts: dict[str, list[int]] = field(default_factory=dict)
    whole: dict[str, str] = field(default_factory=dict)
    journal: dict[str, dict[str, Any]] = field(default_factory=lambda: {"remove": {}, "trim": {}})


def _renumbered(tries: list[FixTry], vmap: dict[int, int]) -> list[FixTry]:
    return [t.model_copy(update={"script_version": vmap.get(int(t.script_version), int(t.script_version))})
            if t.script_version and vmap else t for t in tries]


def _full_record(body: bytes) -> Optional[ErrorEntry]:
    """줄 바이트의 레코드(찢긴 접두어 뒤에 붙은 것 포함), 못 읽으면 None.

    The record of a line's bytes (also one glued after a torn prefix); None when it does not read."""
    from sbmig.kb.jsonl import parse_record

    return parse_record(body.decode("utf-8", "replace"), ErrorEntry)


def _updated(full: ErrorEntry, upd: dict[str, Any]) -> ErrorEntry:
    """``_resign_entries`` 의 바꿀 필드를 온전한 레코드에 적용한다(시도는 번호표로 다시 매김 — 온전한 레코드의 diff 를
    지키려고).

    Apply ``_resign_entries``' fields to the full record (tries renumbered by the map, keeping their diffs)."""
    u = {k: val for k, val in upd.items() if k not in ("tries", "_vmap")}
    if "_vmap" in upd:
        u["tries"] = _renumbered(full.tries, upd["_vmap"])
    return full.model_copy(update=u)


def _moved_resolved_by(e: ErrorEntry, whole: dict[str, str]) -> Optional[list[Any]]:
    """``resolved_by`` 중 통째로 옮긴 케이스를 가리키는 것을 새 ID 로 바꾼 목록(바꿀 것이 없으면 None).

    ``resolved_by`` with the cases moved whole renamed; None when nothing names one."""
    if not whole or not any(r.case_id in whole for r in e.resolved_by):
        return None
    return [r.model_copy(update={"case_id": whole[r.case_id]}) if r.case_id in whole else r for r in e.resolved_by]


def _moved_script(text: str, old: str, new: str, vmap: dict[int, int]) -> str:
    """``cases/<old>/fix_v<N>.py`` → ``cases/<new>/fix_v<새 N>.py`` (그 밖의 ``cases/<old>/`` 접두어도 바꿈).

    Rewrite the script path of an entry whose case moved (version renumbered by ``vmap``)."""
    if not text:
        return text
    t = re.sub(rf"cases/{re.escape(old)}/fix_v(\d+)\.py",
               lambda m: f"cases/{new}/fix_v{vmap.get(int(m.group(1)), int(m.group(1)))}.py", text)
    return t.replace(f"cases/{old}/", f"cases/{new}/")


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
