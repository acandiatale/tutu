"""KB에 오류 기록과 수정 스크립트를 저장하는 모듈. 모든 실행의 오류 목록과 케이스별 폴더를 관리한다.
바깥에서는 파사드 ``sbmig.kb.FileKnowledgeBase``로 쓴다.

Callers: the fix loop finds scripts with ``best`` / ``latest``, stores them with ``add_script`` and records
results with ``credit``; a run's end calls ``record_run``; ``sbmig kb reindex`` calls ``recount``. A case
folder is named by its case id ``sha1(L2)[:10]``; two records are the same case when their L2 strings are
equal. A re-sign keeps the old ids in ``was`` (entries) and ``aliases`` (cases). ``errors.jsonl`` is the
source; every other file here is derived from it: never edit them by hand, rebuild them with ``kb reindex``.
The KB is file-based only (no database).

Spec: docs/v2/PLAN-v2.1.md §3.4. Every run's error list + one folder per case.

Layout under the KB directory (``kb.dir``, default ``kb-v2/``)::

    errors.jsonl                 every run's entries (ErrorEntry lines, the run's errors.jsonl appended at its end)
    cases.md                     the case table for people (rewritten by every record_run / reindex)
    cases/<case_id>/case.json    CaseInfo: case_id, l2, name, kind, projects, seen, resolved, scripts[…],
                                 last_analysis, last_solution, updated_at — plus the keys people / sbmig add:
                                 "pin": "block[:<reason>]" (never tried: `sbmig kb cases pin`), "last_result"
                                 (every script carries parent / reason — its augment lineage and why it was
                                 made — last_failure and runs[]: one ScriptRun per scored run, newest last, capped
                                 by errors.script_runs)
    cases/<case_id>/fix_v<N>.py  the stored fix scripts (exact bytes; an identical script is stored once)
    .lock                        the cross-process write lock (web server, fleet and `sbmig kb` share one KB)

Scripts: ``unverified`` when stored → ``proven`` once a reuse resolved the case in a project other than the
one that created it → ``retired`` by a person (``sbmig kb cases retire``; never selected).
:meth:`CaseStore.best` = the newest proven version, else the newest unverified one. Never selected: a version
the loop found ``non_idempotent`` (:meth:`CaseStore.mark_non_idempotent`: a second run right after ``ok``
changed the tree again), and a version credited ``round_trip`` (it took part in two cases undoing each other's
fix) until a success of its own scope that was not a reuse, or ``activate``, clears the mark. A script whose
code holds a secret value is stored masked and retired at once (the masked code is not the code that ran; a
secret never lands in the KB).

Scopes: a version records the scope of the run that wrote it (``full`` / ``jdk-jakarta``; ``""`` = written
before scopes were recorded = ``full``), every credited run its own scope. ``best(case_id, scope)`` ranks
(same scope, proven, version); counters, ``last_failure``, ``projects`` and the status count only runs of the
version's own scope; a framework-guard rollback of another scope's version is ``out_of_scope`` (runs[] only).

Every write holds the KB lock and is atomic; texts are masked (:mod:`sbmig.kb.redact`). Readers never lock and
never raise on a damaged file (it is skipped with a warning; ``sbmig kb validate`` lists it).

Several writers (several build pods on one NFS volume): inside the lock every mutator reads the case again
from disk before it writes. A case that reads as missing is confirmed by exclusive creates — ``mkdir`` of its
folder, then an ``O_EXCL`` probe of its ``case.json`` (:meth:`CaseStore._confirm_new`: an NFS client may
answer "no such file" from its cache for a ``case.json`` another pod has just written; the server decides an
exclusive create): when it is there the case is read again, never overwritten blind — the folder is listed and
the file read by ``open()`` for up to ``CONFIRM_SECONDS`` (an NFS client trusts a cached "no such file" until
the directory's attributes are checked again), and a case.json the server has but this client never sees
raises :class:`CaseUnreadable` (that one KB write is skipped with a WARNING; nothing is written over it). A
script file is claimed by a hard link of a temp file (exclusive: an existing ``fix_v<N>.py`` is never replaced
— the next number is taken), by an exclusive create where the file system has no hard links; any other link
error is raised.

Signature rule versions (docs/INTERFACES.md "v2.1z-ai"): entries and case.json files carry the rule version of
their L2 (``sig``); a rule change is followed by :meth:`CaseStore.resign`, which moves records and scripts to
the ids the current rules give (``<kb>/kb.json`` = the version the KB was last re-signed to and the cases it
skipped, written at the end of every re-sign; ``<kb>/resign-pending.json`` = the folder clean-up a re-sign
still owes, executed first by the next one). Only records of classes the rules changed are touched; nothing is
ever merged by an alias (aliases are a lookup aid for people). The re-sign streams errors.jsonl: memory
follows the affected cases, not the file. The exact-key guard: a stored case whose L2 is not the L2 asked for
is not that case — nothing is reused from or written into it (:meth:`CaseStore.same_case`,
:class:`CaseKeyMismatch`).
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
ENTRIES_DIR = "errors"            # 오류 하나당 읽기 쉬운 파일 하나: <kb>/cases/<case_id>/errors/<run_id>-<id>.json
OTHER_DIR = "_other"              # 케이스가 없는 항목(pom-verify, 메모)
CASE_FILE = "case.json"
KB_ERRORS = "errors.jsonl"
CASES_MD = "cases.md"
KB_MARKER = "kb.json"             # {"sig": <KB를 마지막으로 재서명한 규칙 버전>, "resigned_at": <iso>}
RESIGN_PENDING = "resign-pending.json"   # 재서명이 아직 못 한 폴더 정리. errors.jsonl보다 먼저 기록한다
SUCCESS_RESULTS = frozenset({"improved", "build_ok"})
# 다른 케이스와 서로의 수정을 되돌린 시도의 결과. 실패로 세고, 같은 시도의 성공 기록은 되돌린다
# docs/INTERFACES.md "v2.1z-ca"
ROUND_TRIP = "round_trip"
FAILURE_RESULTS = frozenset({"no_change", "regressed", "reverted", "apply_failed", "blocked", ROUND_TRIP})
# 범위를 줄인 실행의 프레임워크 검사가 되돌린 다른 범위 버전의 실행. 그 버전의 실패로 세지 않는다
# 같은 범위 버전이 되돌려지면 실제 실패다. apply_failed로 기록한다
OUT_OF_SCOPE = "out_of_scope"
SCOPE_FULL = "full"
SCOPE_REDUCED = "jdk-jakarta"
# 프레임워크 검사 문장의 시작 부분. sbmig.loop.fixloop.FRAMEWORK_GUARD_PREFIX와 같아야 한다
GUARD_NOTE = "framework project:"
TEXT_CHARS = 4000
REASON_CHARS = 600                # ScriptRun.reason / CaseScript.reason / last_failure 길이 제한
RUN_FILES = 50                    # ScriptRun.files에 남기는 파일 수
SCRIPT_RUNS = 30                  # errors.script_runs 기본값. 0이면 제한하지 않는다
RUN_SOURCES = frozenset({"kb-script", "llm-new", "llm-augment"})   # 저장된 스크립트가 실행된 시도 출처
# 실패 이유가 아니라 시도를 설명하는 note다. 실행 기록의 reason에서 뺀다
NOISE_NOTES = ("reused stored v", "escalation model requested", "merge-conflict hint:")
_NOT_RUN = ("stored (unverified), not run", "not run again")        # 스크립트가 실행되지 않은 시도
_RAN_THEN_EDITS = "did not apply → applying the answer's edits"      # 실행은 했지만 실패해서 응답의 edits를 적용한 시도
SECRET_HEADER = "# sbmig: retired at once — the code held a secret value (stored masked; not the code that ran)\n"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SCRIPT_RE = re.compile(r"^fix_v([1-9][0-9]{0,8})\.py$")
# 서버에는 있는데 이 클라이언트가 못 보는 case.json을 찾는 시간. NFS 디렉토리 속성 캐시 acdirmax 60초보다 길게 잡는다
CONFIRM_SECONDS = 75.0
REPAIR_SECONDS = 1.0              # 열리지만 이 시간 동안 비어 있거나 파싱되지 않는 case.json은 새로 쓴다
CONFIRM_MAX_DELAY = 2.0           # 확인하는 동안 두 번 사이에 쉬는 최대 시간
CLAIM_TRIES = 1000                # 다른 쓰기가 가져간 스크립트 번호 다음으로 시도해 볼 번호 수
# os.link가 낸 이 오류들만 하드 링크를 지원하지 않는 파일 시스템으로 본다.
# EIO, ESTALE, EACCES, ENOSPC는 실제 오류다
_NO_LINK_ERRNOS = frozenset(e for e in (getattr(errno, n, None) for n in
                                        ("EPERM", "EOPNOTSUPP", "ENOTSUP", "ENOSYS", "EMLINK", "EXDEV")) if e)
# Windows: ERROR_INVALID_FUNCTION(1), ERROR_NOT_SAME_DEVICE(17), ERROR_NOT_SUPPORTED(50), ERROR_TOO_MANY_LINKS(1142)
_NO_LINK_WINERRORS = frozenset({1, 17, 50, 1142})


class CaseUnreadable(OSError):
    """서버에 있는 case.json을 ``CONFIRM_SECONDS`` 동안 읽지 못했을 때 던지는 예외 클래스.
    그 위에 새 case.json을 쓰지 않고 이번 KB 쓰기만 건너뛴다.
    OSError의 하위 클래스라서 I/O 오류를 잡는 호출부가 WARNING으로 끝낸다.

    "The server has it" = an exclusive create answered EEXIST."""


class CaseKeyMismatch(OSError):
    """케이스 ID는 같은데 저장된 case.json의 L2가 요청한 L2와 다를 때 던지는 예외 클래스.
    해시 충돌이나 손으로 고친 case.json에서 생긴다. 그 케이스에는 아무것도 쓰지 않는다.
    OSError의 하위 클래스라서 호출부가 ``CaseUnreadable``처럼 WARNING으로 끝낸다."""


# 키 불일치를 이미 경고한 (KB 디렉토리, 케이스 ID). 경고는 프로세스마다 한 번만 남긴다
_MISMATCH_SAID: set[tuple[str, str]] = set()


def scope_of(value: Any) -> str:
    """기록된 이관 범위를 반환한다. 값이 비어 있으면 범위를 기록하기 전의 기록이라 ``full``로 본다."""
    v = str(value or "").strip()
    return v or SCOPE_FULL


def own_run(s: CaseScript, r: ScriptRun) -> bool:
    """실행 기록이 그 버전과 같은 범위에서 실행된 것인지 확인한다. 버전의 횟수와 상태는 이런 실행만 센다."""
    return scope_of(r.scope) == scope_of(s.scope)


def scope_label(scope: Any) -> str:
    """로그와 힌트에 쓸 범위 이름을 반환한다(``full migration`` / ``jdk-jakarta (reduced scope)``)."""
    v = scope_of(scope)
    return "full migration" if v == SCOPE_FULL else f"{v} (reduced scope)"


def _no_hard_links(e: OSError) -> bool:
    # 하드 링크를 지원하지 않는다는 뜻의 오류인지 확인한다. 그 밖의 오류는 실제 실패로 본다
    if getattr(e, "winerror", None) in _NO_LINK_WINERRORS:
        return True
    return e.errno in _NO_LINK_ERRNOS


def valid_case_id(case_id: Optional[str]) -> bool:
    """케이스 ID가 디렉토리 이름으로 쓸 수 있는 형식인지 확인한다.
    손으로 고친 case.json이나 CLI 인자가 경로나 ``..``로 KB 밖을 가리키지 못하게 한다."""
    return bool(case_id) and bool(_CASE_ID_RE.match(case_id or "")) and ".." not in (case_id or "")


def case_name(message: str, l2: str) -> str:
    """사람이 읽을 케이스 이름을 만든다.
    오류 메시지 첫 줄을 쓰고, 메시지에 L2 핵심어가 없으면 ``메시지: 핵심어`` 형태로 만든다.

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
    """저장된 스크립트의 성공 / 실패 요약 문자열을 만든다.
    ``failures``면 버전마다 마지막 실패 이유를 붙인다.

    ``v1 proven 3/0, v2 unverified 0/1`` (successes / failures); ``failures`` (cases.md) adds each
    version's last failure: ``v1 proven 3/0; v2 unverified 0/1 — no_change: 3 of 3 targeted error(s) remained``.
    A version written by a reduced-scope run says so (``v2 jdk-jakarta proven 1/0``); full ones stay bare.
    A version marked ``round_trip`` reads ``v1 unverified round-trip 0/1``."""
    def one(s: CaseScript) -> str:
        sc = "" if scope_of(s.scope) == SCOPE_FULL else f" {scope_of(s.scope)}"
        marks = (" non-idempotent" if s.non_idempotent else "") + (" round-trip" if s.round_trip else "")
        t = f"v{s.version}{sc} {s.status}{marks} {s.successes}/{s.failures}"
        return t + (f" — {_short(s.last_failure, 80)}" if failures and s.last_failure else "")
    return ("; " if failures else ", ").join(one(s) for s in case.scripts) or "-"


def run_reason(result: str, note: str = "", head: str = "") -> str:
    """저장된 스크립트를 실행했는데 케이스를 해결하지 못한 이유를 한 줄로 만든다.
    결과가 improved / build_ok면 빈 문자열을 반환한다.
    ``head`` 뒤에 시도 note를 붙인다. 시도를 설명하는 부분은 뺀다. 남는 것이 없으면 결과 이름을 반환한다.

    Stored as ScriptRun.reason. ``head`` example from the fix loop: ``no_change: 3 of 3 targeted error(s)
    remained — …``. The parts cut from the note are ``NOISE_NOTES`` (reuse banner, escalation request,
    merge-conflict hints); duplicates are dropped, order kept."""
    if result in SUCCESS_RESULTS:
        return ""
    parts = [" ".join(p.split()) for p in (note or "").split("; ")]
    parts = [p for p in parts if p and not p.startswith(NOISE_NOTES)]
    if head and " ".join(head.split()):
        parts.insert(0, " ".join(head.split()))
    return "; ".join(dict.fromkeys(parts)) or (result or "")


def first_reason(reason: str, parent: int | None = None, limit: int = 160) -> str:
    """로그 줄에 쓸 이유의 첫 부분만 잘라서 반환한다.
    보강본 이유 앞의 ``v<parent>: ``는 로그 줄에 이미 있어서 뺀다."""
    t = " ".join((reason or "").split())
    if parent and t.startswith(f"v{parent}: "):
        t = t[len(f"v{parent}: "):]
    for sep in ("; ", ". "):
        if sep in t:
            t = t.split(sep, 1)[0]
    return _short(t, limit)


def run_line(r: ScriptRun) -> str:
    """``sbmig kb cases show``에 출력할 실행 기록 한 줄을 만든다.

    ``run r-1 demo r2 try 1: no_change — no_change: 3 of 3 targeted error(s) remained [errors 5 → 5, new 0;
    files: a/B.java]``."""
    s = f"run {r.run_id or '-'} {r.project or '-'} r{r.round} try {r.n}: {r.result or '-'}" + \
        (f" (scope {r.scope})" if r.scope else "")
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
    """실행 기록의 중복을 판단하는 키 (run_id, round, n)를 반환한다.
    시도 번호가 없는 기록은 None이다. 그런 기록은 다른 기록과 합치지 않는다."""
    return (r.run_id, int(r.round), int(r.n)) if r.n else None


def _resolved_elsewhere(s: CaseScript) -> bool:
    """그 버전을 만든 프로젝트가 아닌 다른 프로젝트에서 케이스를 해결한 적이 있는지 확인한다.
    ``activate``가 proven으로 되돌릴지 정할 때 쓴다.
    프로젝트 이름이 없는 이전 기록은 ``credit``처럼 실행 ID로 판단한다.

    Fallback without ``project``: a successful own-scope run of another run than ``created_by``, or two or
    more distinct projects (one of them is not the maker's)."""
    if s.project:
        return any(p and p != s.project for p in s.projects)
    # 이전 기록은 project가 비어 있어서 자기 프로젝트도 남의 것으로 보인다. 실행 ID와 프로젝트 수로 판단한다
    if s.created_by and any(r.result in SUCCESS_RESULTS and r.run_id and r.run_id != s.created_by and own_run(s, r)
                            for r in s.runs):
        return True
    return len({p for p in s.projects if p}) > 1


def _recount_from_runs(s: CaseScript) -> None:
    """버전의 성공, 실패 수와 프로젝트, 상태를 자기 범위의 실행 기록으로 다시 센다. retired는 그대로 둔다.

    Approximate when the runs were capped at ``errors.script_runs``. Used for a version split across cases
    (:meth:`CaseStore._recount_versions`)."""
    s.successes = sum(1 for r in s.runs if r.result in SUCCESS_RESULTS and own_run(s, r))
    s.failures = sum(1 for r in s.runs if r.result in FAILURE_RESULTS and own_run(s, r))
    s.projects = list(dict.fromkeys(r.project for r in s.runs if r.result in SUCCESS_RESULTS and r.project
                                    and own_run(s, r)))
    if s.status != "retired":
        s.status = "proven" if _resolved_elsewhere(s) else "unverified"


def _unknown(obj: Any, fields: Any) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in fields} if isinstance(obj, dict) else {}


def _keep_nested_extra(case: CaseInfo, data: dict[str, Any]) -> None:
    """case.json의 스크립트 버전과 실행 기록에 있는, 모델이 모르는 키를 객체에 붙여 둔다.
    새 버전의 코드가 쓴 필드를 이전 버전의 코드가 다시 저장해도 잃지 않게 한다.

    Unknown keys of a script version / a run record (written by a newer version of the code) are attached to the
    parsed objects (`__pydantic_extra__`; the models ignore unknown keys) so that `_save` writes them back and a
    model_copy / a re-sign move carries them along. Matched by position: the models keep the list order."""
    raw_scripts = data.get("scripts")
    if not isinstance(raw_scripts, list) or len(raw_scripts) != len(case.scripts):
        return
    for s, raw in zip(case.scripts, raw_scripts):
        extra = _unknown(raw, CaseScript.model_fields)
        if extra:
            object.__setattr__(s, "__pydantic_extra__", extra)
        raw_runs = raw.get("runs") if isinstance(raw, dict) else None
        if not isinstance(raw_runs, list) or len(raw_runs) != len(s.runs):
            continue
        for r, raw_r in zip(s.runs, raw_runs):
            extra = _unknown(raw_r, ScriptRun.model_fields)
            if extra:
                object.__setattr__(r, "__pydantic_extra__", extra)


def _put_nested_extra(case: CaseInfo, data: dict[str, Any]) -> None:
    """`_keep_nested_extra`가 붙여 둔 키를 저장할 JSON에 다시 넣는다. 모델 필드는 덮어쓰지 않는다."""
    for s, d in zip(case.scripts, data.get("scripts") or []):
        for k, v in (getattr(s, "__pydantic_extra__", None) or {}).items():
            d.setdefault(k, v)
        for r, dr in zip(s.runs, d.get("runs") or []):
            for k, v in (getattr(r, "__pydantic_extra__", None) or {}).items():
                dr.setdefault(k, v)


class CaseStore:
    """KB의 케이스와 오류 기록을 읽고 쓰는 클래스.
    ``<kb>/cases/<case_id>/``, ``<kb>/errors.jsonl``, ``<kb>/cases.md``를 다룬다.
    ``script_runs``는 버전마다 남길 실행 기록 수다(0이면 무제한)."""

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
        self.last_reindex: dict[str, int] = {}         # 마지막 recount가 다시 만든 것(runs, hunks)의 수
        # 이 프로세스에서 다른 L2가 쓰고 있다고 확인한 케이스 ID. best / latest / credit이 건너뛴다
        self._foreign: set[str] = set()
        self.older_lines = 0                           # 마지막 record_run 이후 이전 규칙으로 서명된 줄 수
        self.signer: Optional[Signer] = None           # 재서명에 쓸 규칙. None이면 현재 규칙

    # ------------------------------------------------------------------ layout
    @property
    def cases_dir(self) -> Path:
        """케이스 폴더들이 있는 ``<kb>/cases/`` 경로를 반환한다."""
        return self.root / CASES_DIR

    @property
    def errors_path(self) -> Path:
        """모든 실행의 오류 레코드를 담은 KB 원본 파일 ``<kb>/errors.jsonl``의 경로를 반환한다."""
        return self.root / KB_ERRORS

    @property
    def md_path(self) -> Path:
        """사람이 읽는 케이스 표 ``<kb>/cases.md``의 경로를 반환한다."""
        return self.root / CASES_MD

    def lock(self):
        """프로세스 간에 KB를 동시에 수정하지 않도록 잠금(``<kb>/.lock``)을 건다.
        재진입할 수 있고 모든 쓰기는 이 잠금 안에서 한다."""
        from sbmig.kb.lock import kb_lock

        return kb_lock(self.root, self.lock_timeout)

    def case_dir(self, case_id: str) -> Path:
        """케이스 폴더 경로를 반환한다. 케이스 ID 형식이 아니면 KB 밖에 쓰지 못하도록 ValueError를 던진다."""
        if not valid_case_id(case_id):
            raise ValueError(f"not a case id: {case_id!r}")
        return self.cases_dir / case_id

    def case_ids(self) -> list[str]:
        """case.json이 있는 케이스 폴더 이름 목록을 정렬해서 반환한다."""
        if not self.cases_dir.is_dir():
            return []
        return sorted(p.name for p in self.cases_dir.iterdir() if p.is_dir() and (p / CASE_FILE).is_file())

    # ------------------------------------------------------------------ case.json
    def _read(self, case_id: str, *, quiet: bool = False) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """케이스와 case.json의 추가 키를 함께 읽어 반환한다.
        파일이 없거나 읽을 수 없거나 다른 케이스를 가리키면 경고를 남기고 None을 반환한다.
        ``quiet``면 경고 대신 DEBUG 로그로 남긴다.

        An empty (0-byte) file is "not there yet" (DEBUG): a lock-free reader may open it in another pod's
        confirmation probe window."""
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
        """case.json을 ``open()``으로 바로 읽어 (결과, 상태, 설명)을 반환한다.
        상태는 ok / missing / empty / damaged / error 중 하나다.
        NFS 캐시가 '없음'으로 답하는 경우는 ``is_file()`` 조회에서 생기므로 미리 확인하지 않는다.

        empty = 0 bytes; damaged = opens but does not parse or names another case; error = any other
        OSError."""
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
            # 메모장으로 고쳐서 BOM이 붙은 case.json도 읽을 수 있도록 utf-8-sig로 디코딩한다
            data = json.loads(raw.decode("utf-8-sig"))
            case = CaseInfo.model_validate(data)
        except ValueError as e:                           # UnicodeDecodeError / JSONDecodeError / ValidationError
            return None, "damaged", f"does not read ({type(e).__name__})"
        if case.case_id != case_id:
            return None, "damaged", f"names case {case.case_id}"
        # CaseInfo에 없는 키(pin, last_result, 사람이 넣은 값)는 따로 두었다가 저장할 때 다시 넣는다
        extra = {k: v for k, v in data.items() if k not in CaseInfo.model_fields}
        _keep_nested_extra(case, data)
        return (case, extra), "ok", ""

    def _keep_alive(self) -> None:
        """긴 작업 중에 KB 잠금이 오래된 것으로 처리되지 않도록 잠금 파일의 수정 시각을 갱신한다.
        잠금 모듈이 실제 갱신을 몇 초에 한 번으로 줄인다. 그래서 자주 호출해도 된다."""
        from sbmig.kb.lock import keep_alive

        keep_alive(self.root)

    def _save(self, case: CaseInfo, extra: Optional[dict[str, Any]] = None) -> None:
        """case.json을 원자적으로 다시 쓴다. 추가 키는 모델 필드를 덮어쓰지 않도록 뒤에 붙인다."""
        data = case.model_dump(mode="json")
        _put_nested_extra(case, data)
        data.update({k: v for k, v in (extra or {}).items() if k not in data})
        atomic_write(self.case_dir(case.case_id) / CASE_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def find(self, case_id: str) -> Optional[CaseInfo]:
        """케이스 정보(CaseInfo)를 반환한다. 없거나 읽을 수 없으면 None을 반환한다."""
        hit = self._read(case_id)
        return hit[0] if hit else None

    def extra(self, case_id: str) -> dict[str, Any]:
        """case.json에서 CaseInfo에 없는 키(``pin``, ``last_result``, 사람이 넣은 값)를 반환한다."""
        hit = self._read(case_id)
        return dict(hit[1]) if hit else {}

    def pin_of(self, case_id: str) -> str:
        """사람이 차단으로 고정한 케이스면 ``block`` 또는 ``block:<이유>``를 반환한다. 아니면 빈 문자열이다.
        ``pin``의 다른 값은 여기서 무시한다. 그런 값은 ``sbmig kb validate``가 알려 준다."""
        p = self.extra(case_id).get("pin")
        p = p.strip() if isinstance(p, str) else ""
        return p if p == "block" or p.startswith("block:") else ""

    # ------------------------------------------------------------------ L2 일치 확인
    def _mismatch(self, case_id: str, stored: str, asked: str) -> None:
        """케이스 ID를 다른 서명이 쓰는 것으로 표시하고 경고를 남긴다.
        경고는 ID와 프로세스마다 한 번만 남긴다."""
        self._foreign.add(case_id)
        key = (str(self.root), case_id)
        if key not in _MISMATCH_SAID:
            _MISMATCH_SAID.add(key)
            log.warning('case id %s is held by another signature: stored "%s" asked "%s" — nothing is reused from or '
                        "written into it", case_id, cut(stored, 160), cut(asked, 160))

    def same_case(self, case_id: str, l2: str, case: Optional[CaseInfo] = None) -> bool:
        """저장된 케이스가 이 L2의 케이스인지 확인한다.
        두 L2가 모두 있고 서로 다를 때만 False를 반환한다. 예외는 던지지 않는다.
        False면 그 ID를 이 프로세스 동안 다른 서명의 것으로 기억한다.
        그러면 ``best`` / ``latest`` / ``credit`` / ``describe``가 그 ID를 건너뛴다.

        A later matching ask clears the mark. True when there is no case or either L2 is empty."""
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
        """이 프로세스에서 다른 L2가 쓰고 있는 것으로 확인된 케이스 ID인지 확인한다."""
        return case_id in self._foreign

    def _case_or_new(self, case_id: str, l2: str = "", name: str = "",
                     kind: Optional[ErrorKind] = None) -> tuple[CaseInfo, dict[str, Any]]:
        """케이스를 읽어 (케이스, 추가 키)를 반환한다. 잠금 안에서 호출한다.
        기존 케이스는 비어 있는 l2, 이름, 종류만 채운다. 없으면 새 CaseInfo를 만든다.
        없다고 읽히면 :meth:`_confirm_new`로 정말 없는지 확인한 뒤에만 새로 만든다."""
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
        """잠금 안에서 케이스가 정말 없는지 확인한다.
        NFS 캐시는 '없음'으로 잘못 답할 수 있다. 그래서 배타적 생성으로 서버에 직접 확인한다.
        - 폴더 ``mkdir``이 성공하면 새 케이스다. None을 반환한다
        - case.json이 이미 있으면 ``CONFIRM_SECONDS`` 동안 다시 읽어 그 케이스를 반환한다
        - 열리지만 ``REPAIR_SECONDS`` 동안 비었거나 깨져 있으면 None을 반환한다. 그러면 새로 쓴다
        - 끝내 읽지 못하면 :class:`CaseUnreadable`을 던진다

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
            # 오류별 JSON만 있는 폴더는 record_run이 L2 없는 항목을 기록한 폴더다. 경고 없이 새 케이스로 만든다
            if any(n != ENTRIES_DIR for n in names):
                log.warning("kb: cases/%s holds no %s (a writer stopped before saving it?) — a fresh %s is written; "
                            "the scripts already there keep their numbers", case_id, CASE_FILE, CASE_FILE)
            return None
        # 배타적 생성이 실패해도 클라이언트 캐시가 바로 고쳐지지는 않는다.
        # 폴더를 열어 속성을 다시 가져오고 open()으로 반복해서 읽는다
        start = time.monotonic()
        deadline = start + (CONFIRM_SECONDS if there else REPAIR_SECONDS)
        delay, bad_since, state, detail, said = 0.05, None, "missing", "", False
        while True:
            try:
                os.listdir(d)                             # 폴더를 열어 디렉토리 속성을 서버에서 다시 가져오게 한다
            except OSError:
                pass
            hit = self._read(case_id, quiet=True)
            if hit is not None:
                log.info("kb: cases/%s was created by another writer meanwhile — that case is used, not a fresh one",
                         case_id)
                return hit
            _, state, detail = self._load(case_id)
            now = time.monotonic()
            # 원자적 교체 중에는 비었거나 깨진 파일이 보일 수 없다. 잠시 뒤에도 그렇게 보이면 새로 쓴다
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
            self._keep_alive()                            # 기다리는 동안 다른 파드가 잠금을 오래된 것으로 깨지 않게 한다
            time.sleep(max(0.01, min(delay, deadline - now)))
            delay = min(delay * 2, CONFIRM_MAX_DELAY)
        if state in ("empty", "damaged"):
            log.warning("kb: cases/%s/%s is there but %s after %.0f s — a fresh one is written over it", case_id,
                        CASE_FILE, "stays empty" if state == "empty" else detail or "does not read", REPAIR_SECONDS)
            return None
        if there is None and state == "missing":
            return None                                   # 서버에 확인하지 못했고 읽히는 것도 없으면 새 케이스로 본다
        # 서버에는 있는데 끝내 읽지 못했다.
        # 그 위에 쓰면 다른 파드가 남긴 스크립트 목록과 성공 / 실패 기록이 사라지므로 쓰지 않는다
        log.warning("kb: cases/%s/%s exists on the server but this client could not read it for %.0f s (%s) — nothing "
                    "is written over it; this KB write is skipped", case_id, CASE_FILE, time.monotonic() - start,
                    detail or state)
        raise CaseUnreadable(errno.EAGAIN, f"cases/{case_id}/{CASE_FILE} exists but does not read on this client "
                                           f"({detail or state}) — not overwritten")

    def _case_file_on_server(self, case_id: str) -> Optional[bool]:
        """case.json이 정말 있는지 ``O_EXCL`` 생성으로 서버에 확인한다.
        생성이 성공하면 만든 파일을 바로 지우고 False를 반환한다. 이미 있으면 True, 알 수 없으면 None이다."""
        p = self.case_dir(case_id) / CASE_FILE
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return True
        except OSError as e:                              # Windows는 교체 중인 파일에 PermissionError를 낸다
            log.debug("kb: cases/%s: %s not probed (%s)", case_id, CASE_FILE, e)
            return None
        os.close(fd)
        try:
            os.unlink(str(p))
        except OSError:
            pass
        return False

    def set_pin(self, case_id: str, pin: str) -> CaseInfo:
        """케이스를 차단으로 고정(``block`` / ``block:<이유>``)하거나 해제("")한다.
        필요하면 case.json을 만든다.
        고정된 케이스는 스크립트도 LLM도 시도하지 않고 인수인계로 넘어간다.
        그 밖의 값이면 ValueError를 던진다."""
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
        """케이스에 저장된 스크립트 목록을 오래된 것부터 반환한다."""
        case = self.find(case_id)
        return sorted(case.scripts, key=lambda s: s.version) if case else []

    def code(self, case_id: str, version: int) -> Optional[str]:
        """``fix_v<N>.py``의 코드(UTF-8)를 반환한다. 없거나 읽을 수 없으면 None을 반환한다."""
        if not valid_case_id(case_id):
            return None
        path = self.cases_dir / case_id / f"fix_v{int(version)}.py"
        try:
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def best(self, case_id: str, scope: str = "") -> Optional[tuple[CaseScript, str]]:
        """먼저 실행할 스크립트를 고른다.
        묻는 실행과 범위가 같은 버전이 먼저다. 그 안에서는 최신 proven, 최신 unverified 순이다.
        retired, 비멱등(``non_idempotent``), 왕복(``round_trip``) 버전과 파일이 없는 버전은 고르지 않는다.
        고를 것이 없으면 None을 반환한다.

        Rank = (same scope as ``scope``, proven, version), newest first: a version of the asking run's scope
        always comes before one of the other scope (``""`` = ``full`` on both sides). ``non_idempotent`` = a
        second run changed the tree again; ``round_trip`` = it took part in a round trip. Also None for a case
        id held by another signature (:meth:`same_case`)."""
        if case_id in self._foreign:
            return None
        want = scope_of(scope)
        # 두 번째 실행에서 또 파일을 바꾸는 버전은 다음 프로젝트를 반쯤 고친 채로 망가뜨린다. 재사용하지 않는다
        # 다른 케이스와 서로의 수정을 되돌린 버전도 다시 실행하면 같은 왕복을 되풀이한다. 재사용하지 않는다
        ranked = sorted((s for s in self.scripts(case_id)
                         if s.status != "retired" and not s.non_idempotent and not s.round_trip),
                        key=lambda s: (scope_of(s.scope) == want, s.status == "proven", s.version), reverse=True)
        for s in ranked:
            code = self.code(case_id, s.version)
            if code:
                return s, code
        return None

    def latest(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """retired가 아닌 가장 최신 버전을 반환한다. 보강 요청의 바탕으로 쓴다.
        다른 L2가 쓰는 케이스 ID면 None을 반환한다."""
        if case_id in self._foreign:
            return None
        for s in sorted(self.scripts(case_id), key=lambda s: s.version, reverse=True):
            code = self.code(case_id, s.version) if s.status != "retired" else None
            if code:
                return s, code
        return None

    def add_script(self, case_id: str, l2: str, code: str, *, source: str = "llm-new", run_id: str = "",
                   project: str = "", analysis: str = "", solution: str = "", name: str = "",
                   kind: Optional[ErrorKind] = None, parent: int | None = None, reason: str = "",
                   scope: str = "") -> CaseScript:
        """``code``를 케이스의 다음 버전(unverified)으로 저장한다.
        같은 코드가 이미 있으면 그 버전을 반환한다.
        비밀 값이 든 코드는 마스킹해서 저장하고 바로 retired로 둔다.
        ``scope``는 스크립트를 만든 실행의 이관 범위다.
        보강본의 케이스 ID가 재서명으로 옮겨 갔으면 같은 L2의 새 케이스에 저장한다.
        케이스에 없는 ``parent``는 버린다.

        ``parent`` = the version an augment extends, ``reason`` = why this version was made (the parent's
        failure in words, ``v1: no_change: …``). ``scope`` is recorded on the version, and as
        ``solution_scope`` beside ``last_solution``. Raises ValueError for empty code, CaseKeyMismatch when
        the id holds another L2. An augment's case id a re-sign moved while the caller held it (``parent``
        given, no case.json, the id in another case's ``aliases``) is resolved to that case when its L2 is the
        L2 asked for; a ``parent`` the case does not have is dropped (a case created fresh under the old id
        never names a version of the moved case — nor itself — as parent) and the new number is taken above
        it, so the caller's tries that name the old number never land on this script."""
        if not (code or "").strip():
            raise ValueError("empty script")
        try:
            parent = int(parent) if parent else None
        except (TypeError, ValueError):
            parent = None
        why = self._reason(reason)
        floor = 0
        with stage("kb"), self.lock():
            # 부모가 있는데 케이스가 없으면 이 실행이 본 뒤에 재서명으로 옮겨 간 케이스다
            if parent and self._read(case_id, quiet=True) is None:
                moved = self._alias_case(case_id, l2)
                if moved:
                    log.info("kb: case %s was moved to %s by a re-sign — the script is stored there", case_id, moved)
                    case_id = moved
            case, extra = self._case_or_new(case_id, l2, name, kind)
            if parent and all(s.version != parent for s in case.scripts):
                log.warning("kb: cases/%s has no v%d — the new script's parent is dropped (the case was moved or "
                            "created fresh meanwhile)", case_id, parent)
                floor, parent = parent, None
            # 같은 ID를 다른 L2가 쓰고 있으면 그 케이스에 쓰지 않는다. 어댑터가 OSError를 WARNING으로 끝낸다
            if (l2 and case.l2 and case.l2 != l2) or (not l2 and case_id in self._foreign):
                self._mismatch(case_id, case.l2, l2)
                raise CaseKeyMismatch(errno.EEXIST, f"cases/{case_id} holds another signature — the script is not "
                                                    "stored")
            if l2 and case.l2 == l2:
                case.sig = max(int(case.sig or 0), current_version())   # 요청한 L2는 현재 규칙의 서명이다
            masked = self.clean.red.redact(code) or ""
            # 마스킹된 코드는 실제로 실행된 코드가 아니다. 재사용하지 않도록 바로 retired로 둔다
            secret = masked != code
            stored = SECRET_HEADER + masked if secret else code
            for s in case.scripts:
                if _sha(self.code(case_id, s.version) or "") == _sha(stored):
                    log.info("cases/%s/fix_v%d.py: the same script is already stored", case_id, s.version)
                    return s
            v = self._claim_script(case_id, max(self._next_version(case), floor + 1), stored.encode("utf-8"))
            s = CaseScript(version=v, file=f"fix_v{v}.py", status="retired" if secret else "unverified",
                           source=source, created_by=run_id, project=project, parent=parent, reason=why,
                           scope=(scope or "").strip())
            case.scripts.append(s)
            if analysis:
                case.last_analysis = self.clean(analysis, TEXT_CHARS)
            if solution:
                case.last_solution = self.clean(solution, TEXT_CHARS)
                if (scope or "").strip():
                    extra["solution_scope"] = scope_of(scope)
                else:
                    extra.pop("solution_scope", None)
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
        return s                                          # cases.md는 실행이 끝날 때 record_run이 다시 쓴다

    def _disk_versions(self, case_id: str) -> list[int]:
        """케이스 폴더에 실제로 있는 ``fix_v<N>.py``의 번호 목록을 반환한다. case.json은 보지 않는다."""
        d = self.case_dir(case_id)
        return [int(m.group(1)) for p in (d.glob("fix_v*.py") if d.is_dir() else [])
                if (m := _SCRIPT_RE.match(p.name))]

    def _next_version(self, case: CaseInfo) -> int:
        """다음 스크립트 번호를 정한다. case.json의 버전과 폴더의 ``fix_v*.py`` 중 가장 큰 값에 1을 더한다.
        번호를 재사용하지 않으니 목록에 없는 파일도 덮어쓰지 않는다. ``add_script``와 ``merge_from``이 쓴다."""
        return 1 + max([s.version for s in case.scripts] + self._disk_versions(case.case_id), default=0)

    def _claim_script(self, case_id: str, version: int, data: bytes) -> int:
        """``fix_v<version>.py``를 다른 파일을 덮어쓰지 않게 만들고 사용한 번호를 반환한다.
        같은 폴더의 임시 파일에 쓴 뒤 하드 링크로 이름을 붙인다.
        번호가 이미 있으면 경고하고 다음 번호를 쓴다. 하드 링크를 지원하지 않으면 ``O_EXCL`` 생성으로 대신한다.

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
                        # NFS는 응답을 잃고 다시 보낸 LINK가 성공했어도 EEXIST로 답할 수 있다.
                        # 링크 수가 2면 우리가 만든 파일이다
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
                        # 하드 링크를 지원하지 않을 때만 대체 경로를 쓴다.
                        # EIO, ESTALE, EACCES, ENOSPC에서 덮어쓰면 다른 파일을 잃기 때문이다
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
        """하드 링크를 쓸 수 없을 때 ``O_EXCL``로 파일을 만들고 내용을 쓴다.
        파일이 이미 있으면 False를 반환한다.
        쓰다가 실패하면 만든 파일을 지우고 예외를 그대로 던진다."""
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except BaseException:
            try:
                os.unlink(str(target))                    # 방금 배타적으로 만든 파일이므로 지워도 된다
            except OSError:
                pass
            raise
        return True

    def _reason(self, text: str | None) -> str:
        """저장할 이유 문자열을 만든다. 마스킹한 뒤 한 줄로 합치고 최대 REASON_CHARS자까지만 남긴다."""
        t = " ".join(self.clean(text or "").split())
        return t if len(t) <= REASON_CHARS else t[: REASON_CHARS - 1] + "…"

    def _run_files(self, files: Iterable[Any]) -> list[str]:
        out = [posix(self.clean(str(f))) for f in (files or ()) if f]
        return list(dict.fromkeys(f for f in out if f))[:RUN_FILES]

    def _cap_runs(self, runs: list[ScriptRun]) -> list[ScriptRun]:
        return runs[-self.script_runs:] if self.script_runs and len(runs) > self.script_runs else runs

    def _add_run(self, s: CaseScript, run: ScriptRun) -> None:
        """``run``을 실행 기록 끝에 추가한다. 같은 (run_id, round, n) 기록이 있으면 교체한다.
        inconclusive 시도는 되돌린 뒤 재빌드에서 결과를 다시 평가받기 때문이다."""
        key = _run_key(run)
        runs = [r for r in s.runs if key is None or _run_key(r) != key]
        s.runs = self._cap_runs(runs + [run])

    def credit(self, case_id: str, version: int, result: str, project: str = "", run_id: str = "", *,
               reason: str = "", round: int = 0, n: int = 0, errors_before: int | None = None,
               errors_left: int | None = None, errors_new: int | None = None, files: Iterable[Any] = (),
               scope: str = "", code: str = "", reused: bool = False) -> Optional[CaseScript]:
        """버전 N을 실행한 결과를 그 버전에 기록한다.
        - improved / build_ok: 성공 수를 1 늘린다. 그 버전을 만든 프로젝트가 아니면 proven으로 바꾼다
        - no_change / regressed / reverted / apply_failed / blocked / round_trip: 실패 수를 1 늘린다
        - round_trip은 실행 범위와 관계없이 버전에 표시를 남긴다
        - round_trip인데 같은 범위의 같은 시도가 성공으로 기록돼 있으면 그 성공을 되돌린다
        - 표시된 버전이 실행 범위와 관계없이 다시 성공하면 표시를 지운다. 재사용(``reused``)의 성공은 지우지 않는다
        - partial / inconclusive는 ``last_result``만 바꾼다. pending은 무시한다
        - 실행 범위(``scope``)가 버전의 범위와 다르면 실행 기록에만 남긴다. 성공, 실패 수와 상태는 그대로 둔다
        - 케이스나 버전이 없으면 재서명으로 옮겨 간 케이스에서 같은 코드(``code``)의 버전을 찾아 기록한다

        ``scope`` = the scope of the run (``""`` = ``full``). A run in the other scope is kept in ``runs[]``
        with its scope and touches neither ``successes`` / ``failures`` / ``projects`` / ``last_failure`` nor
        the status (they mean "in the version's own scope"). ``out_of_scope`` (the framework guard rolled the
        run back) on a version of the run's own scope is a real failure there and is recorded as
        ``apply_failed``.

        Every credit appends a ``ScriptRun`` (round / try ``n``, the reason in words, error counts, files; the
        same run_id + round + n replaces the earlier record; ``errors.script_runs`` kept) and a failure sets
        ``last_failure``.

        Moved ids: a case id that no longer reads, or a version the case does not have (a re-sign moved the
        case while the caller held the id): the case whose ``aliases`` hold the id is credited instead — the
        version whose stored code is ``code`` (the same number when no code is given and one case holds the
        alias); nothing found → one WARNING naming the id, None. When ``code`` is given and the version found
        under the id holds other code, the alias is looked up too.

        Round trips (docs/INTERFACES.md "v2.1z-ca"): ``round_trip`` (a failure) sets
        ``CaseScript.round_trip``. When the version already holds a run of the same try (run_id, round, n; n
        non-zero) with a success result of its own scope, that success is taken back first: ``successes`` −1
        (never below 0), the project leaves ``projects`` unless another kept own-scope success run names it,
        ``proven`` → ``unverified`` unless :func:`_resolved_elsewhere` still holds (retired stays). A try
        already recorded as an own-scope failure (``round_trip`` included) is not counted a second time. No
        other result changes what an earlier credit of the same try counted. A success clears the mark unless
        ``reused`` (the loop ran the stored version without an LLM answer; another pod may have planned that
        reuse before the mark was set): it still counts as a success. The clearing holds whatever the scope of
        the run (``add_script`` finds a version by its code across scopes, so a mark only an own-scope success
        could clear would never clear for runs of the other scope); the counts stay own-scope only. A
        ``round_trip`` run of the other scope: ``runs[]`` only for the counts and the status, but it sets the
        mark (the mark is about reuse, and a full run may reuse a ``jdk-jakarta`` version)."""
        if not result or result == "pending" or case_id in self._foreign:
            return None
        why = self._reason(reason)
        run_scope = scope_of(scope)
        with stage("kb"), self.lock():
            hit = self._read(case_id)
            s = None
            if hit is not None:
                s = next((x for x in hit[0].scripts if x.version == int(version)), None)
                if s is not None and code and not self._same_code(case_id, s.version, code):
                    s = None
            if s is None:
                moved = self._moved_version(case_id, int(version), code)
                if moved is None:
                    log.warning("cases/%s: no script v%s to credit (%s) — the %s run is not recorded", case_id,
                                version, "no such case" if hit is None else "no such version", result)
                    return None
                old, (case_id, hit, s) = case_id, moved
                log.info("kb: case %s v%s was moved to %s v%d by a re-sign — credited there", old, version, case_id,
                         s.version)
            case, extra = hit
            own = scope_of(s.scope) == run_scope
            if result == OUT_OF_SCOPE and own:
                result = "apply_failed"                   # 같은 범위 스크립트를 검사가 되돌렸으면 실제 실패다
            # 다른 범위의 실행은 실행 기록에만 남긴다. 성공, 실패 수와 상태는 그 버전 범위의 실행으로만 센다
            if own:
                s.last_result = result
            prev = None
            if own and result == ROUND_TRIP and n:
                key = (run_id or "", int(round or 0), int(n))
                prev = next((r for r in s.runs if _run_key(r) == key and own_run(s, r)), None)
            took_back = prev is not None and prev.result in SUCCESS_RESULTS
            if took_back:
                # 같은 시도가 앞서 성공으로 기록됐으면 그 성공을 되돌린 뒤 실패로 센다
                s.successes = max(0, s.successes - 1)
                gone = prev.project or project
                if gone and gone in s.projects and not any(
                        r is not prev and r.result in SUCCESS_RESULTS and r.project == gone and own_run(s, r)
                        for r in s.runs):
                    s.projects.remove(gone)
            # 왕복 표시는 재사용에 관한 것이라 다른 범위의 실행에서도 남긴다
            if result == ROUND_TRIP:
                s.round_trip = True
            # 같은 코드의 LLM 답은 범위가 달라도 이 버전에 기록된다. 그래서 범위와 관계없이 표시를 지운다.
            # 재사용은 표시 전에 계획됐을 수 있다. 재사용의 성공으로는 지우지 않는다
            if result in SUCCESS_RESULTS and s.round_trip and not reused:
                s.round_trip = False
                log.info("cases/%s/fix_v%d.py: round-trip mark cleared (a success that was not a reuse%s)", case_id,
                         s.version, "" if own else f", {run_scope} run")
            if own and result in SUCCESS_RESULTS:
                s.successes += 1
                if project and project not in s.projects:
                    s.projects.append(project)
                # 다른 프로젝트에서도 통했으면 proven이다.
                # 프로젝트 이름이 없는 이전 기록은 실행 ID가 다르면 다른 프로젝트로 본다
                other = (project and s.project and project != s.project) or \
                    (not s.project and run_id and s.created_by and run_id != s.created_by)
                if s.status == "unverified" and other:
                    s.status = "proven"
                    log.info("cases/%s/fix_v%d.py: proven (resolved the case in %s)", case_id, s.version, project)
            elif own and result in FAILURE_RESULTS:
                # 같은 시도를 이미 실패로 센 왕복 기록은 두 번 세지 않는다
                if not (prev is not None and prev.result in FAILURE_RESULTS):
                    s.failures += 1
                s.last_failure = why or result
            self._add_run(s, ScriptRun(run_id=run_id or "", project=project or "", round=int(round or 0),
                                       n=int(n or 0), result=result, reason=why, errors_before=errors_before,
                                       errors_left=errors_left, errors_new=errors_new, files=self._run_files(files),
                                       scope=(scope or "").strip()))
            if took_back:
                if s.status == "proven" and not _resolved_elsewhere(s):
                    s.status = "unverified"
                log.info("cases/%s/fix_v%d.py: the %s of run %s r%d try %d taken back (round trip), %s", case_id,
                         s.version, prev.result, run_id or "-", int(round or 0), int(n), s.status)
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s (성공 %d / 실패 %d)%s%s", case_id, s.version, result, s.successes,
                     s.failures, "" if own else f" [{run_scope} 실행, {scope_of(s.scope)} 버전: 집계 제외]",
                     f" — {_short(why, 300)}" if why else "")
        return s

    def _same_code(self, case_id: str, version: int, code: str) -> bool:
        """저장된 ``fix_v<N>.py``가 ``code``를 저장한 결과와 같은지 확인한다. 비밀 값 마스킹도 반영한다."""
        return _sha(self.code(case_id, version) or "") == _sha(self._stored_form(code))

    def _stored_form(self, code: str) -> str:
        """``add_script``가 ``code``를 저장할 때의 내용을 반환한다(비밀 값이 있으면 마스킹과 머리말)."""
        masked = self.clean.red.redact(code) or ""
        return SECRET_HEADER + masked if masked != code else code

    def _alias_case(self, case_id: str, l2: str = "") -> str:
        """재서명으로 ``case_id``가 옮겨 간 케이스의 ID를 반환한다. ``aliases``에 그 ID를 가진 케이스다.
        없거나 여럿이거나 L2가 다르면 빈 문자열을 반환한다.

        Reads every case.json: called only on a miss."""
        hits = [c.case_id for c, _ in self.cases() if case_id in c.aliases and c.case_id != case_id
                and (not l2 or not c.l2 or c.l2 == l2)]
        return hits[0] if len(hits) == 1 else ""

    def _moved_version(self, case_id: str, version: int, code: str
                       ) -> Optional[tuple[str, tuple[CaseInfo, dict[str, Any]], CaseScript]]:
        """재서명으로 옮겨 간 케이스에서 그 스크립트 버전을 찾는다.
        (케이스 ID, (케이스, 추가 키), 버전)을 반환한다.
        ``code``가 있으면 같은 코드의 버전을, 없으면 같은 번호를 쓴다. 찾지 못하면 None을 반환한다.

        By code because numbers may change in a merge; by number only when exactly one case holds the
        alias."""
        targets = [(c, x) for c, x in self.cases() if case_id in c.aliases and c.case_id != case_id]
        want = _sha(self._stored_form(code)) if (code or "").strip() else ""
        for c, x in targets:
            if want:
                s = next((s for s in c.scripts if _sha(self.code(c.case_id, s.version) or "") == want), None)
            else:
                s = next((s for s in c.scripts if s.version == version), None) if len(targets) == 1 else None
            if s is not None:
                return c.case_id, (c, x), s
        return None

    def mark_non_idempotent(self, case_id: str, version: int, note: str = "") -> Optional[CaseScript]:
        """버전 N을 비멱등으로 표시한다. ok로 끝난 직후 다시 실행했더니 파일이 또 바뀐 버전이다.
        ``best``는 다시 고르지 않지만 보강의 바탕(``latest``)으로는 남긴다.
        버전이 없으면 None을 반환한다. 예외는 던지지 않는다.

        Example of such a script: one that adds an argument every run (``new JobBuilder(.., jobRepository,
        jobRepository)``). ``note`` goes to ``last_failure``."""
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
        """retire와 activate의 공통 처리를 한다. 잠금 안에서 상태를 바꾸고 cases.md를 다시 쓴다.
        케이스나 버전이 없으면 KeyError를 던진다. activate는 왕복 표시(``round_trip``)도 지운다."""
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
                # 사람이 다시 켠 버전은 왕복 표시도 지운다
                if s.round_trip:
                    s.round_trip = False
                    log.info("cases/%s/fix_v%d.py: round-trip mark cleared (activate)", case_id, s.version)
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s", case_id, s.version, s.status)
            self._write_md_locked()
        return s

    def retire(self, case_id: str, version: int) -> CaseScript:
        """버전 N을 다시는 고르지 않도록 retired로 바꾼다. ``sbmig kb cases retire``가 호출한다."""
        return self._set_status(case_id, version, True)

    def activate(self, case_id: str, version: int) -> CaseScript:
        """retire를 취소한다.
        다른 프로젝트에서 케이스를 해결한 적이 있으면 proven으로, 아니면 unverified로 되돌린다. 왕복 표시도 지운다."""
        return self._set_status(case_id, version, False)

    def describe(self, case_id: str, scope: str = "") -> str:
        """KB 조회 결과를 로그용 한 구절로 만든다.

        Shapes: ``저장된 스크립트 없음 (처음 보는 케이스)``, ``저장된 스크립트 v2 (proven, 성공 3 / 실패 0,
        프로젝트 2)``, ``pin block: <reason>``, ``케이스는 있으나 쓸 스크립트 없음 (v1 retired)``. Uses
        ``best(case_id, scope)``; a version of the other scope says so (``…, 다른 범위: full)``)."""
        if case_id in self._foreign:
            return "저장된 스크립트 없음 (케이스 ID 를 다른 서명이 차지함 — 재사용·기록 안 함)"
        case = self.find(case_id)
        if case is None:
            return "저장된 스크립트 없음 (처음 보는 케이스)"
        pin = self.pin_of(case_id)
        if pin:
            return f"pin {pin.replace(':', ': ', 1)}"
        best = self.best(case_id, scope)
        if best is None:
            return ("케이스는 있으나 쓸 스크립트 없음 (" + scripts_text(case) + ")") if case.scripts else \
                f"저장된 스크립트 없음 (케이스는 {len(case.projects)}개 프로젝트에서 봄)"
        s = best[0]
        other = f", 다른 범위: {scope_of(s.scope)}" if scope_of(s.scope) != scope_of(scope) else ""
        return (f"저장된 스크립트 v{s.version} ({s.status}, 성공 {s.successes} / 실패 {s.failures}, "
                f"프로젝트 {len(s.projects)}{other})")

    # ------------------------------------------------------------------ errors.jsonl + cases
    def _known_ids(self) -> set[str]:
        """KB errors.jsonl에 이미 있는 항목 ID를 반환한다. 같은 항목을 두 번 기록하지 않으려고 쓴다."""
        return self._scan_lines()[0]

    def _scan_lines(self, skipped: Iterable[str] = ()) -> tuple[set[str], int]:
        """errors.jsonl을 한 번 읽어 항목 ID 집합과, 재서명이 필요한 레코드 수를 반환한다.
        이전 규칙으로 서명됐는데 그 뒤 규칙이 바뀐 분류의 레코드가 재서명 대상이다.
        줄마다 정규식으로 먼저 거르고 후보만 파싱한다. ``skipped`` 케이스의 줄은 세지 않는다."""
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
        """KB에 넣기 전에 항목의 모든 텍스트를 한 번 더 마스킹한다."""
        c = self.clean
        tries = [t.model_copy(update={"analysis": c(t.analysis), "solution": c(t.solution),
                                      "no_script_reason": c(t.no_script_reason), "diff": c(t.diff),
                                      "note": c(t.note), "edits": [self._clean_note(n) for n in t.edits]})
                 for t in e.tries]
        return e.model_copy(update={"message": c(e.message), "raw": c(e.raw), "note": c(e.note), "tries": tries,
                                    "l1": c(e.l1), "symbol": c(e.symbol), "location": c(e.location),
                                    "artifact": c(e.artifact), "goal": c(e.goal)})

    def record_run(self, entries: Iterable[ErrorEntry]) -> int:
        """실행이 끝난 항목들을 ``<kb>/errors.jsonl``에 추가하고 추가한 줄 수를 반환한다.
        이미 있는 ID는 건너뛴다.
        케이스마다 seen / resolved / 프로젝트 / 마지막 원인과 해결 방법을 갱신한다. 오류별 JSON과 ``cases.md``도 쓴다.
        이전 규칙으로 서명된 줄이 남으면 그 수를 ``older_lines``에 둔다. 파사드가 이 값을 보고 재서명을 호출한다.

        A case folder is made only for entries with an L2 (or an existing case.json). Entries whose L2 is not
        the stored case's stay in errors.jsonl only (exact-key guard)."""
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
            # 추가한 뒤에도 이전 규칙의 줄이 남아 있으면 FileKnowledgeBase가 재서명을 호출한다
            self.older_lines = older + sum(1 for e in fresh if e.l2 and int(e.sig or 0) < v
                                           and l2_parts(e.l2)[1] in changed_since(e.sig)
                                           and e.case_id not in skipped)
            if new_file and n:
                self._write_marker()                      # 새로 만든 KB는 현재 규칙이라 재서명하지 않는다
            by_case: dict[str, list[ErrorEntry]] = {}
            skip_files: set[str] = set()
            # note / pom-verify는 케이스 통계에서 뺀다. L2도 case.json도 없는 케이스는 폴더를 만들지 않는다
            for e in fresh:
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id) \
                        and (e.l2 or (self.cases_dir / e.case_id / CASE_FILE).is_file()):
                    by_case.setdefault(e.case_id, []).append(e)
            for cid, es in by_case.items():
                self._keep_alive()
                try:
                    case, extra = self._case_or_new(cid, es[0].l2, case_name(es[0].message, es[0].l2), es[0].kind)
                except CaseUnreadable as ex:
                    # 이 케이스만 건너뛴다. errors.jsonl에는 이미 기록했으니 `sbmig kb reindex`가 나중에 다시 센다
                    log.warning("kb: case %s not updated by this run (%s) — `sbmig kb reindex` recounts it from %s",
                                cid, ex, KB_ERRORS)
                    continue
                # 저장된 L2와 다른 L2의 항목은 errors.jsonl에만 남기고 이 케이스에는 세지 않는다
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
        """오류마다 JSON 파일을 케이스 폴더에 쓸지 반환한다.
        설정 ``errors.json_files``를 따르고 기본값은 true다."""
        return bool(getattr(getattr(self.cfg, "errors", None), "json_files", True))

    def entry_file(self, e: ErrorEntry) -> Path:
        """오류별 JSON 파일의 경로를 반환한다.
        케이스가 없는 항목(pom-verify, 메모)은 ``cases/_other/errors/`` 아래에 둔다.

        ``<kb>/cases/<case_id>/errors/<run_id>-<id>.json``."""
        folder = e.case_id if e.case_id and valid_case_id(e.case_id) else OTHER_DIR
        return self.cases_dir / folder / ENTRIES_DIR / f"{e.run_id or 'run'}-{e.id}.json"

    def _write_entry_files(self, entries: list[ErrorEntry]) -> int:
        """현업이 저장된 오류를 볼 수 있도록 오류마다 들여쓴 JSON 파일을 하나씩 쓴다.
        내용은 errors.jsonl의 줄과 같다. 쓰기에 실패한 파일은 경고만 남기고 넘어간다. 쓴 파일 수를 반환한다."""
        if not self.json_files:
            return 0
        n = 0
        for e in entries:
            self._keep_alive()                            # reindex는 NFS에서 이 파일을 수천 개 다시 쓴다
            try:
                atomic_write(self.entry_file(e), json.dumps(e.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
                n += 1
            except OSError as ex:
                log.warning("could not write %s: %s", self.entry_file(e), ex)
        return n

    def entry_files(self, case_id: str) -> list[Path]:
        """케이스 폴더에 있는 오류별 JSON 파일 경로를 정렬해서 반환한다."""
        return sorted((self.cases_dir / case_id / ENTRIES_DIR).glob("*.json"))

    @staticmethod
    def _apply(case: CaseInfo, extra: dict[str, Any], es: list[ErrorEntry]) -> None:
        """한 실행에서 나온 그 케이스의 항목들을 케이스 통계에 반영한다."""
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
            if t.solution and t.solution != case.last_solution:
                case.last_solution = t.solution
                # 해결 문장의 범위는 그 시도가 저장한 버전의 범위다. 없으면 같은 실행이 만든 버전의 범위를 쓴다
                run_id = es[-1].run_id
                src = next((s for s in case.scripts if t.script_version and s.version == int(t.script_version)), None) \
                    or next((s for s in reversed(case.scripts) if run_id and s.created_by == run_id), None)
                if src is not None and src.scope:
                    extra["solution_scope"] = scope_of(src.scope)
                else:
                    extra.pop("solution_scope", None)
        status: dict[str, int] = {}
        for e in es:
            status[e.status] = status.get(e.status, 0) + 1
        last = es[-1]
        # build로 해결된 오류면 해결한 시도까지 last_result에 넣는다. cases.md에 그대로 나온다
        how = next((resolved_text(e) for e in es if e.status == "resolved" and e.resolved), "")
        extra["last_result"] = (", ".join(f"{k} {v}" for k, v in sorted(status.items())) + (f" ({how})" if how else "")
                                + f" — {last.project or '-'} {last.run_id or ''}".rstrip())
        case.updated_at = utcnow()

    def iter_errors(self, *, run: str = "", case: str = "", status: str = "", project: str = "",
                    entry: str = "") -> Iterator[ErrorEntry]:
        """``<kb>/errors.jsonl`` 항목을 파일 순서대로 조건에 맞게 걸러서 반환한다.
        읽을 수 없는 줄은 건너뛴다.
        ``entry``와 ``case``는 ID 앞부분으로 거른다. ``run``과 ``project``는 값이 정확히 같아야 한다."""
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
        """errors.jsonl의 레코드 수(비어 있지 않은 줄 수)를 반환한다."""
        if not self.errors_path.is_file():
            return 0
        with open(self.errors_path, "rb") as fh:
            return sum(1 for line in fh if line.strip())

    def recount(self, skip: Iterable[str] = ()) -> int:
        """``errors.jsonl``을 바탕으로 케이스 통계를 다시 계산하고 케이스 수를 반환한다.
        ``sbmig kb reindex``가 호출한다.
        - 스크립트 성공, 실패 수와 사람이 넣은 키는 그대로 둔다
        - 버전마다 실행 기록, 보강 계보, 마지막 실패를 다시 만든다
        - 이전 항목에 변경 조각과 해결한 시도를 채운다
        - ``skip``에 있는 케이스는 건드리지 않는다

        Cases for L2s that only errors.jsonl knows are made. ``skip`` = cases a re-sign could not read.

        Every stored version's ``runs`` are rebuilt from the entries' tries (merged with the runs the store
        already has: those keep their error counts; deduplicated by run_id + round + try n), ``parent`` / ``reason``
        filled where an ``llm-augment`` try shows them, ``last_failure`` = the newest failed run's reason; tries with
        a diff whose 수정 notes lack ``hunks`` get them (``sbmig.script.notes.attach_hunks``) — errors.jsonl and those
        entries' JSON files are rewritten once. Build-resolved entries without ``resolved_by`` get the
        probable fixer (also when ``attach_hunks`` is not importable). One INFO line says how many of each
        (``last_reindex["hunks"]`` / ``["resolved_by"]``, counted separately, fresh every call)."""
        with stage("kb"), self.lock():
            return self._recount_locked(skip=set(skip or ()))

    @staticmethod
    def _slim(e: ErrorEntry) -> ErrorEntry:
        # 메모리는 항목 수만큼 든다. 다시 세는 데 필요 없는 큰 텍스트(raw, 시도의 diff)는 버린다
        return e.model_copy(update={"raw": "", "tries": [t.model_copy(update={"diff": ""}) for t in e.tries]})

    def _recount_locked(self, skip: Iterable[str] = ()) -> int:
        """:meth:`recount`의 실제 처리를 한다. 잠금은 호출하는 쪽이 잡는다.
        저장된 L2와 다른 L2의 항목은 그 케이스에 세지 않는다."""
        skip = set(skip or ())
        # 변경 조각과 해결한 시도 채우기는 부가 기능이다. 실패해도 재집계는 계속한다.
        # 두 숫자는 recount마다 0에서 시작한다
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
        # 오류별 JSON이 생기기 전에 저장된 항목이나 사람이 지운 파일을 errors.jsonl로부터 다시 만든다
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
        """자동 재서명 뒤에 일부 케이스만 다시 센다.
        errors.jsonl을 한 번 읽으면서 그 케이스의 줄만 파싱한다.
        빠진 오류별 JSON은 읽는 김에 쓴다. 변경 조각은 채우지 않는다.
        전체 재집계는 ``sbmig kb reindex``가 한다."""
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
        """한 케이스를 그 항목들로 다시 세고 저장한다. 만든 실행 기록 수를 반환한다.
        읽을 수 없는 케이스는 경고를 남기고 0을 반환한다."""
        self._keep_alive()                                # case.json을 모두 다시 쓰므로 잠금을 갱신한다
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
        # 처음부터 다시 센다. 실행별로 묶어서 _apply하면 record_run이 실행마다 센 것과 같은 결과가 된다
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

    # ------------------------------------------------------------------ errors.jsonl로 실행 기록과 변경 조각 다시 만들기
    @staticmethod
    def _ran(t: FixTry) -> str | None:
        """그 시도에서 저장된 스크립트가 실제로 실행됐을 때 받은 결과를 반환한다.
        실행하지 않은 시도면 None이다. 저장만 하고 edits를 적용한 시도, 같은 코드라 다시 실행하지 않은 시도,
        pending인 시도가 그렇다."""
        if not t.script_version or t.source not in RUN_SOURCES or t.result == "pending":
            return None
        note = t.note or ""
        # 스크립트가 실패해서 답변의 수정 목록을 대신 적용한 경우다. 그 스크립트 실행은 apply_failed다
        if t.applied == "edits":
            return "apply_failed" if _RAN_THEN_EDITS in note else None
        if any(x in note for x in _NOT_RUN):
            return None
        # 프레임워크 검사가 되돌린 시도는 범위를 줄인 실행에서만 생긴다(_rebuild_runs가 범위를 정한다)
        if t.result == "apply_failed" and GUARD_NOTE in note:
            return OUT_OF_SCOPE
        return t.result

    def _rebuild_runs(self, case: CaseInfo, es: list[ErrorEntry]) -> int:
        """케이스의 시도들로 저장된 버전마다 실행 기록, parent / reason, last_failure를 만든다.
        저장소에 있던 기록과 합친다. errors.jsonl에서 만든 기록 수를 반환한다."""
        derived: dict[int, dict[tuple[str, int, int], ScriptRun]] = {}
        augments: dict[int, list[tuple[str, int, int]]] = {}
        for e in es:
            for t in e.tries:
                result = self._ran(t)
                if result is None:
                    continue
                v, key = int(t.script_version), (e.run_id, int(t.round), int(t.n))
                per = derived.setdefault(v, {})
                if key in per:                            # 케이스의 여러 항목이 같은 시도를 공유하면 실행 하나로 센다
                    continue
                # no_change 결과에는 이유가 따로 남지 않는다. 결과 이름을 이유 앞에 둔다
                head = result if result == "no_change" and result not in (t.note or "") else ""
                per[key] = ScriptRun(run_id=e.run_id, project=e.project, round=int(t.round), n=int(t.n),
                                     result=result, reason=self._reason(run_reason(result, t.note, head)),
                                     files=self._run_files(t.files), at=e.at,
                                     scope=SCOPE_REDUCED if result == OUT_OF_SCOPE else "")
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
                # 오류 수가 들어 있는 저장소 자체 기록이 우선이다. 재집계한 기록으로는 빈 칸만 채운다
                if have is None:
                    keyed[key] = self._derived_scope(s, r)
                    continue
                fill = {f: getattr(r, f) for f in ("project", "reason", "files") if not getattr(have, f)
                        and getattr(r, f) and not (f == "reason" and have.result in SUCCESS_RESULTS)}
                if fill:
                    keyed[key] = have.model_copy(update=fill)
            merged[s.version] = keyed
            runs = sorted(loose + list(keyed.values()), key=lambda r: (_ts(r.at), r.round, r.n))
            failed = [r for r in runs if r.result in FAILURE_RESULTS and own_run(s, r)]
            if failed:
                s.last_failure = failed[-1].reason or failed[-1].result
            s.runs = self._cap_runs(runs)
        for s in case.scripts:
            if s.parent is None and s.version in augments:
                self._lineage(s, augments[s.version], merged)
        return n

    @staticmethod
    def _derived_scope(s: CaseScript, r: ScriptRun) -> ScriptRun:
        """errors.jsonl에서 다시 만든 실행 기록의 범위를 정한다.
        항목에는 범위가 없다. 그 버전을 만든 실행이면 버전의 범위를 쓰고, 나머지는 ``""``(full)로 둔다.
        같은 범위에서 검사가 되돌린 ``out_of_scope``는 :meth:`credit`처럼 apply_failed로 바꾼다."""
        if not r.scope and s.scope and r.run_id and r.run_id == s.created_by:
            r = r.model_copy(update={"scope": s.scope})
        if r.result == OUT_OF_SCOPE and own_run(s, r):
            r = r.model_copy(update={"result": "apply_failed"})
        return r

    def _lineage(self, s: CaseScript, keys: list[tuple[str, int, int]],
                 runs: dict[int, dict[tuple[str, int, int], ScriptRun]]) -> None:
        """보강본이 어느 버전을 고친 것인지 추정한다.
        그 버전을 만든 시도보다 먼저 같은 실행에서 실행된 낮은 버전 중 가장 높은 버전을 부모로 본다.
        이유는 그 실행의 이유를 가져온다. 정확하지 않을 수 있다.

        The run that created the version is looked at first."""
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
        """변경 조각의 수정 전, 수정 후에 각각 남길 줄 수를 반환한다.
        설정 ``errors.snippet_lines``를 따르고 0이면 전부 남긴다."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _hunked(self, e: ErrorEntry, attach: Any, max_lines: int = 0) -> ErrorEntry | None:
        """diff는 있는데 수정 기록에 변경 조각이 없는 시도에 조각을 채운 ``e``를 반환한다.
        바뀐 것이 없으면 None을 반환한다."""
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
        """시도도 ``resolved_by``도 없이 ``build``로 해결된 항목에 해결한 시도를 추정해 붙인다.
        같은 실행에서 ``seen`` 라운드 이후 그 파일이나 상위 pom을 바꾼 시도를 찾는다.
        재구성 단계에서 나온 항목은 대상이 아니다. 찾지 못하면 None을 반환한다.

        Match kind ``reindex`` (sbmig.errors.entries.probable_fixers)."""
        if e.resolved != "build" or e.resolved_by or e.tries or not e.case_id or e.seen == "restructure":
            return None
        # 오류가 처음 나온 라운드보다 앞선 시도는 그 오류를 고쳤을 수 없다
        start = round_of(e.seen)
        later = [(c, t) for c, t in tries if int(t.round) >= start]
        by = probable_fixers(e, later, match="reindex")
        if not by:
            return None
        by.sort(key=lambda rb: (rb.round, rb.n, rb.match))          # `seen` 이후 가장 이른 라운드부터
        return e.model_copy(update={"resolved_by": by})

    def _backfill_entries(self) -> tuple[int, int]:
        """이전에 저장된 errors.jsonl 항목에 변경 조각과 해결한 시도를 채운다. 두 개수를 따로 반환한다.
        호출하는 쪽이 잠금을 잡고 있어야 한다.
        바뀌지 않은 줄은 바이트 그대로 두고 파일을 원자적으로 다시 쓴다.

        Hunks come from the tries' diffs (``attach_hunks``, ``errors.snippet_lines`` per side; skipped when it
        is not importable); the probable fixer (``_fixer_attributed``) is done also without ``attach_hunks``.
        A line that does not read is kept as it is; the changed entries' JSON files are rewritten. Returns
        (entries given hunks, entries given ``resolved_by``)."""
        # sbmig.script가 없거나 깨져도 변경 조각만 건너뛰도록 여기서 import한다
        try:
            from sbmig.script.notes import attach_hunks
        except Exception as ex:  # noqa: BLE001 - 변경 조각만 건너뛰고 해결한 시도 추정은 계속한다
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
        # 바이트 단위로 처리한다. 바뀌지 않은 줄은 다시 직렬화하지 않고, 읽을 수 없는 줄도 그대로 둔다
        # 해결한 시도를 추정하려고 실행별로 시도를 (케이스, 라운드, n)마다 하나씩 모은다
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
            # 한 항목의 추정이 실패해도 그 항목만 건너뛴다. 변경 조각 채우기와는 서로 독립이다
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
        """모든 케이스를 (CaseInfo, 추가 키) 목록으로 반환한다. 프로젝트 수와 seen이 많은 순서로 정렬한다."""
        out = []
        for cid in self.case_ids():
            hit = self._read(cid)
            if hit is not None:
                out.append(hit)
        out.sort(key=lambda h: (-len(h[0].projects), -h[0].seen, h[0].case_id))
        return out

    def _write_md_locked(self) -> None:
        """cases.md를 다시 쓴다. 잠금은 호출하는 쪽이 잡는다.
        원본에서 다시 만들 수 있는 파일이라 실패해도 경고만 남긴다."""
        try:
            atomic_write(self.md_path, render_cases_md(self.cases()))
        except Exception as e:  # noqa: BLE001 - the view is derived (`sbmig kb reindex` rewrites it)
            log.warning("kb: could not write %s: %s: %s", self.md_path, type(e).__name__, e)

    def write_cases_md(self) -> Path:
        """잠금을 잡고 cases.md를 다시 쓴다."""
        with stage("kb"), self.lock():
            self._write_md_locked()
        return self.md_path

    def stats(self) -> dict[str, Any]:
        """``sbmig kb stats``에 보여 줄 숫자를 반환한다.
        오류 수, 케이스 수, 상태별 스크립트 버전 수, 고정된 케이스 수다."""
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
            "sig": self.marker_sig(),                  # kb.json에 기록된 서명 규칙 버전. 없으면 0
        }

    def validate(self, limit: int = 20) -> list[str]:
        """KB 파일의 문제를 찾아 한 줄씩 반환한다(``sbmig kb validate``).
        읽을 수 없는 errors.jsonl 줄, 읽을 수 없거나 다른 케이스를 가리키는 case.json, 파일이 없는 스크립트,
        목록에 없는 스크립트 파일, 잘못된 pin, 재서명이 필요한 기록을 찾는다. 종류마다 ``limit``줄까지 남긴다."""
        from sbmig.kb.jsonl import parse_record

        problems: list[str] = []
        l2s: dict[str, set[str]] = {}                     # 케이스 ID마다 errors.jsonl에 저장된 L2들
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
                    # 이전 규칙의 기록 중 재서명으로 실제로 옮겨질 것만 센다.
                    # 버전 표시만 갱신될 기록은 문제가 아니다
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
            # cases/_other는 케이스 없는 항목의 오류별 JSON 폴더다. 케이스로 보지 않는다
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

    # ------------------------------------------------------------------ 서명 규칙 버전과 재서명
    @property
    def marker_path(self) -> Path:
        """KB가 마지막으로 재서명된 규칙 버전을 기록하는 ``<kb>/kb.json``의 경로를 반환한다."""
        return self.root / KB_MARKER

    @property
    def pending_path(self) -> Path:
        """재서명이 아직 못 끝낸 폴더 정리 목록(``<kb>/resign-pending.json``)의 경로를 반환한다.
        다음 재서명이 이 정리를 먼저 마친다."""
        return self.root / RESIGN_PENDING

    def marker(self) -> dict[str, Any]:
        """``kb.json``의 내용을 반환한다. 없거나 읽을 수 없으면 빈 dict를 반환한다."""
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def marker_sig(self) -> int:
        """``kb.json``에 기록된 규칙 버전을 반환한다. 없거나 읽을 수 없으면 0을 반환한다.
        첫 ``match``는 이 작은 파일 하나만 읽어서 재서명이 필요한지 확인한다."""
        try:
            return int(self.marker().get("sig") or 0)
        except (TypeError, ValueError):
            return 0

    def skipped_cases(self) -> dict[str, str]:
        """지난 재서명이 건너뛴 케이스(ID와 이유)를 반환한다.
        자동 재서명은 이 케이스를 다시 시도하지 않는다. ``sbmig kb reindex``만 다시 시도한다.

        ``kb.json`` "skipped": id → reason."""
        got = self.marker().get("skipped")
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}

    def _write_marker(self, skipped: Optional[dict[str, str]] = None, *, sig: Optional[int] = None,
                      resigning: Optional[Iterable[str]] = None) -> None:
        data: dict[str, Any] = {"sig": current_version() if sig is None else sig, "resigned_at": utcnow().isoformat(),
                                "skipped": dict(sorted((skipped or {}).items()))}
        if resigning is not None:
            data["resigning"] = sorted(resigning)        # 재서명 중이다. 중단되면 다음 재서명이 이 케이스들을 다시 센다
        atomic_write(self.marker_path, json.dumps(data, ensure_ascii=False) + "\n")

    def has_records(self) -> bool:
        """errors.jsonl이 있고 비어 있지 않은지 파일 크기만으로 확인한다."""
        try:
            return self.errors_path.stat().st_size > 0
        except OSError:
            return False

    def _register_prefixes(self) -> None:
        """설정에 있는 사내 라이브러리 접두어를 서명 규칙에 등록한다.
        ``MavenRunner``와 같은 호출이다. 그래서 CLI의 재서명도 실행 중과 같은 키를 만든다.
        설정이 없으면 WARNING을 한 번 남긴다.

        Without the prefixes, members of in-house types would be keyed like library types differently from a
        live run (sbmig.build.signatures.register_external_prefixes)."""
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
        """errors.jsonl을 한 번 읽어 (이전 규칙의 레코드 수, 그 케이스 ID 집합)을 반환한다.
        줄마다 정규식으로 먼저 거르고 후보만 파싱한다."""
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
        """case.json을 폴더 이름과 맞는지 확인하지 않고 읽는다.
        폴더 이름 변경이 중단되면 이전 ID를 가리키는 case.json이 남는다. 그 파일을 읽을 때 쓴다."""
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
        """``case_id``가 ``ids``에 있는 줄만 파싱해 항목마다 재서명 계획을 정한다.
        메모리를 아끼려고 큰 텍스트(raw, diff)를 버린 항목만 남긴다.
        중간에 끊긴 쓰기가 줄 앞에 남긴 깨진 바이트는 따로 모은다. 다시 쓸 때 ``.corrupt``로 옮긴다.

        Returns (line number → slim entry, line number → torn prefix bytes, the EntryPlan: new L2 / stamp)."""
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
        """이전 규칙으로 서명된 기록을 현재 규칙의 케이스 ID로 옮긴다. KB 잠금 안에서 실행한다.
        - 옮길 줄이 없고 ``kb.json``이 현재 버전이면 아무것도 쓰지 않는다. ``dry_run``이면 보고만 만든다
        - ``retry_skipped``면 지난번에 건너뛴 케이스도 다시 시도한다. 자동 재서명에서는 False다
        - 잠금을 잡지 못하면 WARNING을 남기고 ``error``가 든 보고를 반환한다

        Only classes the rules changed since are moved (docs/INTERFACES.md "v2.1z-ai"). Fast path = one
        streaming read. ``signer`` = the rule (tests inject one). ``recount``: the affected cases are
        recounted."""
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
        """:meth:`resign`의 실제 처리를 한다. 잠금은 호출하는 쪽이 잡는다.
        ``force``면 빠른 경로를 건너뛴다. KB 가져오기에서 쓴다.

        Write order (each step survives a kill; the next re-sign finishes it): ``kb.json`` with sig 0 + the
        cases under way -> new folders and scripts -> case.json saves -> ``resign-pending.json`` (folders to
        clean) -> errors.jsonl rewritten once -> per-error files moved -> pending clean-up -> recount ->
        ``kb.json``."""
        v = current_version()
        signer = signer or self.signer
        rep = ResignReport(version=v, dry_run=dry_run)
        marker = self.marker()
        prev_skipped = {} if retry_skipped else self.skipped_cases()
        recover: set[str] = set()
        if not dry_run:
            if self.pending_path.exists():
                recover |= self._finish_pending()         # 중단된 재서명의 폴더 정리부터 마친다
            if isinstance(marker.get("resigning"), list):
                recover |= {str(x) for x in marker["resigning"] if valid_case_id(str(x))}
        n_older, affected = self._older_scan(v, prev_skipped)
        try:
            msig = int(marker.get("sig") or 0)
        except (TypeError, ValueError):
            msig = 0
        if not (force or recover) and msig >= v and n_older == 0:
            return rep                                    # 빠른 경로: 한 번 읽기만 하고 아무것도 쓰지 않는다
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
                # 읽을 수 없는 케이스의 항목은 이전 ID와 버전 그대로 둔다.
                # 다음에 명시적으로 재서명할 때 다시 시도한다
                self._resign_skip(rep, old, f"cases/{old}/{CASE_FILE} {detail or state}")
                skipped.update(idxs)
                continue
            # L2가 case.json과 다른 항목은 자기 ID로 옮기되, 이 케이스의 스크립트는 가져가지 않는다
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
        # 쓰기 전에 진행 중인 케이스를 kb.json에 기록한다.
        # 어디서 중단되든 다음 재서명이 빠른 경로로 건너뛰지 않고 이 케이스들을 다시 센다
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
            except OSError as ex:                         # CaseUnreadable이나 복사 실패는 이 케이스만 건너뛴다
                self._resign_skip(rep, p.old, f"{type(ex).__name__}: {ex}")
                skipped.update(groups.get(p.old, []))
        moved = self._resign_entries(entries, eplan, skipped, ctx, v)
        for n, versions in ctx.shared.items():            # 여러 케이스로 복사된 버전은 케이스마다 다시 센다
            if n in ctx.saves and versions:
                part = [entries[i].model_copy(update=moved[i]) if i in moved else entries[i]
                        for i in ctx.parts.get(n, []) if i in entries]
                self._recount_versions(ctx.saves[n][0], versions, part)
        # 1) 새 폴더와 스크립트는 위에서 처리 -> 2) 옮겨 받았거나 다시 계산된 케이스의 case.json 저장
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
        # 3) 정리할 폴더 목록 기록(중단되면 다음 재서명이 마저 처리)
        # -> 4) errors.jsonl을 스트리밍 복사로 한 번에 원자적으로 다시 쓰기
        if ctx.journal["remove"] or ctx.journal["trim"]:
            atomic_write(self.pending_path, json.dumps(ctx.journal, ensure_ascii=False, indent=2) + "\n")
        if moved or ctx.whole:
            self._resign_write_entries(moved, torn, ctx.whole)
        # 5) 오류별 JSON을 새 케이스 폴더로 이동 -> 6) 비운 이전 폴더 정리 -> 7) 다시 세기 -> 8) kb.json 갱신
        self._resign_move_entry_files(entries, moved)
        entries.clear()                                   # 아래 재집계는 항목을 따로 읽는다. 메모리를 비운다
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
                # 건너뛴 케이스는 다시 세지 않는다. 읽을 수 없는 case.json 위에 새 파일을 쓰지 않는다
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
        """옮겨 갈 대상 케이스들이 기록을 받을 수 있는지 확인한다.
        읽을 수 없거나 다른 L2가 쓰고 있으면 그 이유를 반환한다. 괜찮으면 빈 문자열이다.
        중단된 이름 변경으로 이전 ID를 가리키는 case.json은 문제로 보지 않는다. 이번에 고친다."""
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
        """재서명 계획을 바탕으로 보고서의 숫자와 목록을 채운다.
        ``dry_run``이면 쓰기 전에, 실제 실행이면 쓴 뒤에 호출한다."""
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
        """재서명의 첫 단계로 한 케이스의 폴더와 스크립트를 옮긴다.
        케이스 전체가 옮겨 가면 폴더 이름을 바꾸거나 대상에 합친다.
        여러 케이스로 나뉘면 버전마다 그 버전을 실행한 항목 쪽으로 보낸다.
        이전 폴더 정리는 errors.jsonl을 다시 쓴 뒤에 한다.

        Whole move: rename when the target does not exist, else merge (same code once, a clashing number takes
        the next free one). Split: a version nobody ran stays with the old case, or goes to the largest part.
        The old folder's clean-up is queued in ``ctx.journal``. Every target is resolved before any copy, so a
        CaseUnreadable skips the case before anything is written."""
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
                # 이전 폴더가 없는 경우다.
                # 항목만 있던 케이스이거나, 이름을 바꾼 뒤 case.json을 고치기 전에 멈춘 재서명이다
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
        # 대상 케이스를 먼저 모두 확인한다. CaseUnreadable이면 스크립트를 복사하기 전에 이 케이스를 건너뛴다
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
            for k, val in o_extra.items():                # pin처럼 사람이 넣은 키도 옮긴다. 대상에 있는 값이 우선이다
                tx.setdefault(k, val)
            self._become(tc, n, p.l2[n], old, o_case.aliases if o_case else [], "", v)
            if o_case is not None and not tc.last_solution and o_case.last_solution:
                tc.last_analysis, tc.last_solution = o_case.last_analysis, o_case.last_solution
            staged[n] = (tc, tx, vmap)
        # 모든 대상이 끝난 뒤에만 계획에 반영한다. 중간에 실패하면 이 케이스 전체를 건너뛴 것이 된다
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
        """케이스의 ID를 ``n``, L2를 ``l2``로 바꾸고 서명 규칙 버전을 현재 버전으로 올린다.
        이전 ID들은 aliases에 추가한다. aliases는 사람이 조회할 때만 쓴다. 재서명은 이 값으로 케이스를 합치지 않는다."""
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
        """``old`` 케이스의 스크립트 버전들을 케이스 ``tc``로 복사한다. 이전 번호와 새 번호의 대응을 반환한다.
        대상에 내용이 같은 파일이 있으면 그 번호를 쓰고 기록을 합친다.
        이때 성공, 실패 수는 둘 중 큰 값을 쓴다. 정확한 값은 아니다.

        Merge rules as ``merge_from``: counts = max of the two records (not a sum), projects united, runs
        merged. A byte-identical file may also come from an interrupted re-sign. In a new folder the numbers
        are kept, in an existing one a clash takes the next free."""
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
                if have is None:                          # 중단된 재서명이 파일만 만들고 목록에는 넣지 못한 경우
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
        """여러 케이스로 나뉜 버전의 실행 기록을 이 케이스의 항목으로 다시 만든다.
        성공, 실패, 프로젝트, 상태도 다시 센다. 결과는 근사치다.
        실행 기록은 ``errors.script_runs``개로 잘려 있고, 시도 번호 없는 기록은 버리기 때문이다.

        Stored runs are kept when one of these entries' tries made them; retired stays retired."""
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
            _recount_from_runs(s)

    def _resign_entries(self, entries: dict[int, ErrorEntry], eplan: Any, skipped: set[int],
                        ctx: "_ResignCtx", v: int) -> dict[int, dict[str, Any]]:
        """재서명으로 바뀌는 항목과 바꿀 필드를 줄 번호별로 정한다. 항목 ID는 그대로 둔다.
        바꾸는 필드는 ``sig``, 새 ``case_id`` / ``l2``, ``was``의 이전 ID, 시도의 스크립트 번호, ``script`` 경로다.
        통째로 옮긴 케이스를 가리키는 ``resolved_by``도 바꾼다.

        The tries' renumbering travels as ``_vmap`` and is applied to the full record on the rewrite."""
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
        """errors.jsonl을 한 번에 원자적으로 다시 쓴다.
        바뀐 줄만 다시 직렬화하고 나머지 줄은 바이트 그대로 둔다.
        중간에 끊긴 쓰기가 줄 앞에 남긴 깨진 바이트는 ``errors.jsonl.corrupt``에 추가한다.

        A streaming copy into a temp file, then ``os.replace``. Lines whose ``resolved_by`` names a case moved
        whole are fixed too. ``errors.jsonl.corrupt`` is where ``repair_tail`` puts torn lines as well."""
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
                            # 붙어 버린 줄은 앞의 깨진 부분을 그대로 두고 레코드 부분만 바꾼다
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
        """케이스가 바뀐 항목의 오류별 JSON을 새 케이스 폴더에 쓰고 이전 파일을 지운다.
        원본에서 다시 만들 수 있는 파일이라 실패하면 WARNING만 남긴다. 빠진 파일은 다시 셀 때 만든다.

        The new file is built from the rewritten errors.jsonl (those lines only)."""
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
        """``resign-pending.json``에 남은 폴더 정리를 마친다. 다시 셀 케이스 ID들을 반환한다.
        여러 번 실행해도 결과가 같다.
        항목마다 errors.jsonl 다시 쓰기가 끝났을 때만 정리한다. 끝나지 않았으면 그 정리는 버린다.

        "Finished" = the item's ``expect`` entry carries the new case id. A dropped item is safe: the next
        plan starts over from intact folders."""
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
        """전체가 옮겨 간 케이스의 이전 폴더를 지운다.
        모든 스크립트가 대상에 그대로 있고 다시 만들 수 있는 파일만 남았을 때만 지운다.
        스크립트를 먼저, case.json을 마지막에 지운다. 그래서 중단돼도 다시 실행하면 끝까지 처리된다.
        조건이 맞지 않으면 WARNING을 남기고 폴더를 그대로 둔다.

        "Derived files" = case.json, per-entry JSON, temp files; scripts are compared byte for byte."""
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
        (d / CASE_FILE).unlink(missing_ok=True)           # 여기까지는 케이스로 보여야 해서 마지막에 지운다
        try:
            d.rmdir()
        except OSError as ex:
            log.debug("kb re-sign: cases/%s not removed (%s)", old, ex)
        log.info("kb re-sign: cases/%s removed (moved to %s)", old, ", ".join(targets))

    def _trim_moved(self, old: str, item: dict[str, Any], targets: list[str]) -> None:
        """여러 케이스로 나뉜 케이스 중 남는 쪽을 정리한다.
        옮겨 간 버전을 목록에서 빼고, 대상에 그대로 있는 ``fix_v*.py``를 지운다.
        여러 케이스에 남은 버전은 남은 항목으로 다시 센다.
        파일을 기준으로 판단하므로 중단 뒤 다시 실행해도 끝까지 처리된다."""
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

    # ------------------------------------------------------------------ KB 가져오기와 합치기
    def merge_from(self, other: "CaseStore") -> dict[str, int]:
        """다른 KB(내보낸 것을 푼 폴더)를 이 KB에 합치고 끝에 :meth:`recount`를 호출한다.
        여러 번 실행해도 결과가 같다.
        이미 있는 케이스의 스크립트는 코드로 비교한다. 같으면 기록을 합치고, 다르면 다음 빈 번호로 넣는다.
        가져온 기록은 같은 잠금 안에서 재서명한다. 다른 L2가 쓰는 케이스 ID에는 합치지 않는다.

        Same code → counters max / projects union; a missing local case is copied whole. Round trips
        (docs/INTERFACES.md "v2.1z-ca"): the larger counts are corrected by the take-backs the merge can see.
        For every try (run_id, round, n; n non-zero) that one side records as ``round_trip`` and the other as
        an own-scope success, the side that still counts the success loses it (successes −1, not below 0) and
        gains the failure before the larger counts are taken; the project of that run leaves ``projects``
        unless another kept own-scope success run names it, and ``proven`` goes back to ``unverified`` unless
        :func:`_resolved_elsewhere` still holds (retired stays). Without such a try the merge is unchanged.
        The counts are not recounted from the runs: the history is capped at ``errors.script_runs``."""
        from sbmig.kb.jsonl import append_lines

        counters = {"errors_added": 0, "cases_added": 0, "scripts_added": 0, "scripts_merged": 0}
        with stage("kb"), self.lock() as held:
            if other.errors_path.is_file():
                known = self._known_ids()
                lines = [e.model_dump_json() for e in other.iter_errors() if e.id not in known]
                counters["errors_added"] = append_lines(self.errors_path, lines)
            for cid in other.case_ids():
                self._keep_alive()                        # 가져오기는 NFS에서 케이스를 많이 복사한다
                theirs = other._read(cid)
                if theirs is None:
                    continue
                t_case, t_extra = theirs
                mine = self._read(cid)
                if mine is None:
                    try:
                        mine = self._confirm_new(cid)    # 다른 쪽이 방금 만든 케이스 위에 복사하지 않는다
                    except CaseUnreadable as ex:
                        # 이 케이스만 건너뛴다. 가져오기는 여러 번 해도 결과가 같아서 나중에 다시 하면 합쳐진다
                        log.warning("kb import: case %s skipped (%s) — import the same file again later", cid, ex)
                        counters["cases_skipped"] = counters.get("cases_skipped", 0) + 1
                        continue
                if mine is None and not self._disk_versions(cid):
                    for s in t_case.scripts:
                        # 가져온 case.json의 file 값은 믿지 않는다. 항상 케이스 폴더 안의 정해진 이름으로 쓴다
                        s.file = f"fix_v{int(s.version)}.py"
                        code = other.code(cid, s.version)
                        if code is not None:
                            atomic_write_bytes(self.case_dir(cid) / s.file, code.encode("utf-8"))
                    self._save(t_case, t_extra)
                    counters["cases_added"] += 1
                    continue
                if mine is not None and mine[0].l2 and t_case.l2 and mine[0].l2 != t_case.l2:
                    # 같은 ID를 다른 L2가 쓰고 있는 케이스에는 합치지 않는다
                    self._mismatch(cid, mine[0].l2, t_case.l2)
                    counters["cases_skipped"] = counters.get("cases_skipped", 0) + 1
                    continue
                if mine is None:
                    # case.json 없이 fix_v*.py만 남은 폴더다.
                    # 가져온 스크립트는 그 파일을 덮어쓰지 않도록 아래에서 다음 빈 번호로 넣는다
                    case, extra = t_case.model_copy(update={"scripts": []}, deep=True), dict(t_extra)
                    counters["cases_added"] += 1
                else:
                    case, extra = mine
                    for k, v in t_extra.items():
                        # 이쪽에서 정한 pin 같은 추가 키는 덮어쓰지 않는다
                        extra.setdefault(k, v)
                by_sha = {_sha(self.code(cid, s.version) or ""): s for s in case.scripts}
                local: dict[int, int] = {}                # 가져온 쪽 버전 번호와 이쪽 번호의 대응. 보강본 parent에 쓴다
                for s in sorted(t_case.scripts, key=lambda x: x.version):
                    code = other.code(cid, s.version)
                    if code is None:
                        continue
                    have = by_sha.get(_sha(code))
                    if have is not None:
                        local[s.version] = have.version
                        kl, kt, gone = self._merge_runs(have, s, local)
                        # 한쪽이 왕복으로 되돌린 성공은 아직 세고 있는 쪽에서 실패로 바꾼다. 그다음 큰 수를 고른다
                        have.successes = max(0, max(have.successes - kl, s.successes - kt))
                        have.failures = max(have.failures + kl, s.failures + kt)
                        have.projects += [p for p in s.projects if p not in have.projects]
                        if s.status == "proven" and have.status == "unverified":
                            have.status = "proven"
                        if kl or kt:
                            self._took_back_on_merge(have, gone)
                        counters["scripts_merged"] += 1
                        continue
                    # add_script와 같은 규칙이다. 목록에 없는 fix_v*.py 번호도 피한다
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
            # 가져온 기록은 이전 규칙으로 서명됐을 수 있다. 같은 잠금 안에서 재서명한 뒤 한 번만 다시 센다
            # 항목 ID는 바뀌지 않아서 중복 거르기는 그대로 동작한다
            if not held:
                log.warning("kb import: the KB lock was not taken — no re-sign; run `sbmig kb reindex` later")
            else:
                try:
                    rep = self._resign_locked(False, None, recount=False, force=True)
                    if rep.entries_resigned:
                        counters["entries_resigned"] = rep.entries_resigned
                except Exception as e:  # noqa: BLE001 - 가져오기는 유지하고 재서명은 나중에 `sbmig kb reindex`로 한다
                    log.warning("kb import: re-sign failed (%s: %s) — run `sbmig kb reindex`", type(e).__name__, e)
            self._recount_locked(skip=self.skipped_cases())
        return counters

    def _merge_runs(self, have: CaseScript, theirs: CaseScript, local: dict[int, int]
                    ) -> tuple[int, int, list[str]]:
        """두 KB에 같은 스크립트가 있을 때 실행 기록을 합친다. 계보와 이유는 이쪽에 없을 때만 가져온다.
        한쪽은 round_trip, 다른 쪽은 성공으로 기록한 시도를 쪽마다 센다.
        그 두 수와 되돌린 성공의 프로젝트 목록을 반환한다.

        The same script in both KBs: the union of their runs (same record once), lineage / reasons when ours has
        none. A try (run_id, round, n; n non-zero) that one side records as ``round_trip`` and the other as
        a success of the version's own scope keeps the ``round_trip`` record, in both directions (``credit`` took
        that success back on that side). Returns (tries whose success only ours still counts, tries whose success
        only theirs still counts, the projects of those taken-back runs) for :meth:`merge_from`."""
        def ident(r: ScriptRun) -> tuple:
            return _run_key(r) or (r.run_id, r.round, r.n, r.result, _ts(r.at))

        def pair(ok: ScriptRun, trip: ScriptRun) -> bool:
            return _run_key(ok) is not None and ok.result in SUCCESS_RESULTS and own_run(have, ok) \
                and trip.result == ROUND_TRIP

        mine = {ident(r): r for r in have.runs}
        kl = kt = 0
        gone: list[str] = []
        # 같은 시도는 round_trip 기록이 성공 기록을 대신한다. credit에서 되돌린 성공을 다시 가져오지 않는다
        trips: dict[tuple, ScriptRun] = {}
        for r in theirs.runs:
            ours = mine.get(ident(r))
            if ours is None:
                continue
            if pair(ours, r):
                trips[ident(r)] = r
                kl += 1
                gone.append(ours.project or r.project)
            elif pair(r, ours):
                kt += 1
                gone.append(r.project or ours.project)
        runs = [trips.get(ident(r), r) for r in have.runs] + [r for r in theirs.runs if ident(r) not in mine]
        have.runs = self._cap_runs(sorted(runs, key=lambda r: (_ts(r.at), r.round, r.n)))
        if have.parent is None and theirs.parent and local.get(theirs.parent):
            have.parent = local[theirs.parent]
        have.reason = have.reason or theirs.reason
        have.last_failure = have.last_failure or theirs.last_failure
        # 어느 쪽이든 비멱등이었으면 합친 KB에서도 재사용하지 않는다
        have.non_idempotent = bool(have.non_idempotent or theirs.non_idempotent)
        # 왕복 표시도 어느 쪽이든 있으면 남긴다
        have.round_trip = bool(have.round_trip or theirs.round_trip)
        return kl, kt, gone

    @staticmethod
    def _took_back_on_merge(have: CaseScript, gone: Iterable[str]) -> None:
        """합치면서 되돌린 성공의 프로젝트를 빼고 proven을 다시 판단한다. retired는 그대로 둔다.

        A project stays when another kept own-scope success run names it."""
        for p in dict.fromkeys(x for x in gone if x):
            if p in have.projects and not any(r.result in SUCCESS_RESULTS and r.project == p and own_run(have, r)
                                              for r in have.runs):
                have.projects.remove(p)
        if have.status == "proven" and not _resolved_elsewhere(have):
            have.status = "unverified"


# --------------------------------------------------------------------------- 재서명 보조 함수
@dataclass
class _CasePlan:
    """재서명 계획 한 건을 담는 클래스. 이전 케이스 ``old``와, 옮겨 갈 케이스마다 항목 위치와 L2를 담는다."""
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
    """줄 바이트를 레코드로 읽어 반환한다.
    깨진 바이트 뒤에 붙은 레코드도 읽는다. 읽을 수 없으면 None을 반환한다."""
    from sbmig.kb.jsonl import parse_record

    return parse_record(body.decode("utf-8", "replace"), ErrorEntry)


def _updated(full: ErrorEntry, upd: dict[str, Any]) -> ErrorEntry:
    """``_resign_entries``가 정한 변경 필드를 전체 레코드에 적용한다.
    시도의 스크립트 번호는 새 번호로 다시 매기고 diff는 유지한다."""
    u = {k: val for k, val in upd.items() if k not in ("tries", "_vmap")}
    if "_vmap" in upd:
        u["tries"] = _renumbered(full.tries, upd["_vmap"])
    return full.model_copy(update=u)


def _moved_resolved_by(e: ErrorEntry, whole: dict[str, str]) -> Optional[list[Any]]:
    """``resolved_by`` 중 전체가 옮겨 간 케이스를 새 ID로 바꾼 목록을 반환한다.
    바꿀 것이 없으면 None을 반환한다."""
    if not whole or not any(r.case_id in whole for r in e.resolved_by):
        return None
    return [r.model_copy(update={"case_id": whole[r.case_id]}) if r.case_id in whole else r for r in e.resolved_by]


def _moved_script(text: str, old: str, new: str, vmap: dict[int, int]) -> str:
    """케이스가 옮겨진 항목의 스크립트 경로를 새 케이스와 ``vmap``의 새 번호로 바꾼다."""
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
    """케이스마다 한 줄씩 쓴 ``cases.md`` 내용을 만든다. 많이 나온 케이스가 먼저 온다.
    열은 케이스, 이름, 프로젝트 수, seen / resolved, 스크립트, pin, 마지막 결과다.

    Scripts column: version status successes/failures ``— <last failure>``."""
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
    """수정 기록 하나를 수정 전후 코드와 함께 줄 목록으로 반환한다(``sbmig.errors.common.note_block``).
    ``note_block``이 없거나 실패하면 한 줄짜리 ``note_text``를 반환한다."""
    try:
        from .common import note_block
    except ImportError:
        return [note_text(n)]
    try:
        got = [str(x) for x in (note_block(n) or [])]
    except Exception:  # noqa: BLE001 - 출력이 실패하면 한 줄 요약만 쓴다
        got = []
    return got or [note_text(n)]


def entry_lines(e: ErrorEntry, *, diff: bool = True) -> list[str]:
    """항목 하나와 그 시도들을 읽기 쉬운 블록으로 만든다. ``sbmig kb errors show``와 ``cases show``가 쓴다.
    수정 기록은 수정 전후 코드와 함께 보여 준다. ``diff``가 False면 시도의 diff는 뺀다."""
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
    for rb in e.resolved_by:                       # 빌드로 해결된 오류를 고쳤을 것으로 보이는 시도
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
        if t.major_upgrades:                  # 메이저 업그레이드나 라이브러리 교체는 운영자 확인 대상이다
            L += ["    메이저 변경:"] + [f"      {change_line(x)}" for x in t.major_upgrades]
        if t.verify:
            L += ["    운영자 확인:"] + [f"      {x}" for x in guide_lines(t.verify)]
        if t.files:
            L.append(f"    files: {', '.join(t.files)}")
        if diff and t.diff:
            L += ["    diff:"] + ["      " + ln for ln in t.diff.splitlines()]
    return L
