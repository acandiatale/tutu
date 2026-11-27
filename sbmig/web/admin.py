"""관리자 페이지의 서버 쪽 모듈. 로그인, 프로젝트별 LLM 토큰 사용량, 로그 ZIP과 올린 원본 내려받기를 맡는다.

Usage and logs are read again from the run folders on disk at every request. A web-pod restart logs every
session out. The site token guard still applies in front of these routes.

Routes (all under /admin; `GET /admin` itself, the static page, is served by app.py):

    POST /admin/api/login              {"password": "…"} → 200 + cookie `sbmig_admin` | 401 bad_password | 429
    POST /admin/api/logout             forget the session, clear the cookie
    GET  /admin/api/session            {"authenticated": bool}
    GET  /admin/api/usage              the usage of every run folder under the output root (contract shape)
    GET  /admin/api/logs.zip           sbmig.log / journal.jsonl / control/request.json / remediation/remediation.log of
                                       every run, `<run id>/<file>`, deflated, built in a local temp file and streamed
                                       | 409 busy | 413 too_large | 507 no_space
    GET  /admin/api/logs/check         the same checks without building: {"ok": true, "bytes", "runs", "skipped"}
    GET  /admin/api/runs/{id}/logs.zip one run, with its diagnostic files too (`logs/`, reports, error
                                       lists, status files, the remediation's text files; `plan_run_logs_zip`)
                                       (+ /runs/{id}/logs/check, which may undercount the files the copy
                                       leaves out: it reads no file)
    GET  /admin/api/runs/{id}/source   the ZIP the person uploaded for that run, streamed unchanged | 400 bad
                                       run id | 404 unknown run | 404 source not available (+ /runs/{id}/source/check)

Log ZIP limits: one archive at a time per process (`ZIP_GATE`, held until the temp file is deleted), the
temp file in the pod's local temp dir (never the shared volume), a free-space floor there, a total cap,
files over a per-file limit listed in `SKIPPED.txt`, each member copied only up to the size it had when
planned, and `remediation/remediation.log` left out while anything may still write that run's
remediation.

The password is built in as a salted PBKDF2-HMAC-SHA256 hash (`ADMIN_PASSWORD_PBKDF2`; the plain text never enters the
repository); a non-empty `SBMIG_ADMIN_PASSWORD` (web pod, from the Secret) replaces it. Sessions are random tokens in
this process's memory (12 hours); failed logins wait `fail_delay_s` and an address with `FAIL_LIMIT` failures inside
`FAIL_WINDOW_S` gets 429 until the oldest of them leaves the window (behind a Kubernetes Service every client may share
one address: then the limit is global). Contract: docs/INTERFACES.md "v2.1z-ao"."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from sbmig.core import scrub
from sbmig.core.models import merge_model_names, real_model_name

from . import redact
from .jsonsafe import FiniteJSONResponse
from .redact import REMEDIATION_DIR, TEXT_SUFFIXES
from .runs import FLOW_REMEDIATION, RUN_ID_RE, SOURCE_ZIP

log = logging.getLogger("sbmig.web.admin")

ADMIN_PASSWORD_ENV = "SBMIG_ADMIN_PASSWORD"
# 회사가 정한 비밀번호의 해시. 원문은 저장소 어디에도 두지 않는다
ADMIN_PASSWORD_PBKDF2 = {            # PBKDF2-HMAC-SHA256
    "iterations": 200_000,
    "salt_hex": "bfadb8a1a0a6a71e0571005ea6840ae3",
    "hash_hex": "39baec1e83416b257be2de09e9ae13461ba0dfea532602f0f4a2445f6c763930",
}
ADMIN_COOKIE = "sbmig_admin"
ADMIN_COOKIE_PATH = "/admin"
SESSION_TTL_S = 12 * 3600
FAIL_LIMIT = 5
FAIL_WINDOW_S = 10 * 60
FAIL_DELAY_S = 1.0
MAX_PASSWORD_CHARS = 1024
MAX_SESSIONS = 1000

# 전체 실행 ZIP에 넣는 파일. 실행 하나의 ZIP은 여기에 진단 파일을 더한다(plan_run_logs_zip)
LOG_MEMBERS = ("sbmig.log", "journal.jsonl", "control/request.json", "remediation/remediation.log")
# 기록할 때 마스킹되지 않는 파일. 그 실행의 조치를 쓰는 곳이 하나도 없을 때만 넣는다
FINAL_PASS_MEMBERS = ("remediation/remediation.log",)
LEGACY_TMP_DIR = "_admin"            # 이전 임시 폴더(<out root>/_admin). 시작할 때 남은 ZIP만 지우고 쓰지 않는다
ZIP_TMP_PREFIX = "sbmig-logs-"
ZIP_TMP_SUFFIX = ".zip"
ZIP_TMP_NAME = re.compile(r"^sbmig-logs-[A-Za-z0-9_-]+\.zip$")
# 압축 전 합계 한도. 넘으면 전체 실행 ZIP은 413으로 거절한다. 실행 하나의 ZIP은 정한 순서대로 파일을 뺀다
ZIP_TOTAL_LIMIT = 2 * 1024 ** 3
ZIP_FILE_LIMIT = 512 * 1024 ** 2     # 이보다 큰 파일은 넣지 않고 SKIPPED.txt에 기록한다
ZIP_FREE_FLOOR = 1024 ** 3           # 임시 폴더에 최악의 경우를 쓰고도 이만큼은 남아야 한다
ZIP_CHUNK = 64 * 1024
UPLOADS_DIR = "_uploads"             # in-process 모드가 올린 ZIP을 두는 폴더(RunManager.start_file)
REMEDIATION_ORIGINAL = "remediation/original/source.zip"   # 조치만 하는 실행이 올린 ZIP을 그대로 복사해 둔 곳
CHAIN_STATE = "remediation/state.json"                    # 조치의 상태 파일. 수정 루프의 사용량을 여기서 읽는다
SOURCE_ERROR = "source not available"
EMPTY_README = "No run under this output root has a log file yet.\n"
SKIPPED_NAME = "SKIPPED.txt"
# 한 프로세스에서 ZIP은 한 번에 하나만 만든다. 내려받기가 끝나 임시 파일을 지울 때까지 잡고 있다
ZIP_GATE = threading.Lock()

# sbmig.log에서 LLM 호출 한 번마다 남는 줄(sbmig.fix.company, sbmig.fix.client)
LLM_CALL_LINE = re.compile(rb"llm call provider=\S+ model=(\S*) in=(\d+) out=(\d+)")
# 줄 끝의 응답 모델 목록(served=a,b). 이 값이 없는 이전 줄도 위 식으로 읽는다
LLM_SERVED = re.compile(rb" served=(\S+)\s*$")
# 회사 게이트웨이 클라이언트가 요청마다 남기는 줄의 params.role. 이전 줄의 모델 이름을 찾을 때만 쓴다
LLM_ATTEMPT_ROLE = re.compile(rb"llm call provider=\S+ role=\S+ model=\S* params\.role=(\S+) attempt ")
LLM_JOURNAL_EVENTS = ("llm_script", "llm_verify_guide")
_RUN_SLUG = re.compile(r"^(.+)-\d{8}-\d{6}-[0-9a-f]+$")
_EXIT_RESULT = {0: "ok", 2: "incomplete", 3: "aborted"}


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
def password_matches(given: str) -> bool:
    """입력한 비밀번호가 맞는지 확인한다. ``SBMIG_ADMIN_PASSWORD``가 있으면 그 값과, 없으면 내장 해시와 비교한다.
    비어 있거나 너무 긴 입력은 False다. 어느 쪽이든 ``hmac.compare_digest``로 비교한다."""
    if not isinstance(given, str) or not given or len(given) > MAX_PASSWORD_CHARS:
        return False
    override = os.environ.get(ADMIN_PASSWORD_ENV, "")
    if override:
        a = hashlib.sha256(given.encode("utf-8")).digest()
        b = hashlib.sha256(override.encode("utf-8")).digest()
        return hmac.compare_digest(a, b)
    spec = ADMIN_PASSWORD_PBKDF2
    try:
        digest = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"), bytes.fromhex(spec["salt_hex"]),
                                     int(spec["iterations"]))
        return hmac.compare_digest(digest, bytes.fromhex(spec["hash_hex"]))
    except (KeyError, ValueError, TypeError):
        return False


class AdminAuth:
    """관리자 세션(토큰과 만료 시각)과 주소별 로그인 실패 시각을 이 프로세스의 메모리에 두는 클래스.
    테스트는 ``clock``(monotonic 초)과 ``fail_delay_s``를 바꿀 수 있다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, fail_delay_s: float = FAIL_DELAY_S):
        self.clock = clock
        self.fail_delay_s = fail_delay_s
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def new_session(self) -> str:
        """새 세션 토큰을 만들어 등록하고 반환한다."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._sessions = {t: exp for t, exp in self._sessions.items() if exp > now}
            if len(self._sessions) >= MAX_SESSIONS:              # 가장 먼저 끝나는 세션부터 버린다
                for t in sorted(self._sessions, key=self._sessions.get)[:len(self._sessions) - MAX_SESSIONS + 1]:
                    del self._sessions[t]
            self._sessions[token] = now + SESSION_TTL_S
        return token

    def valid(self, token: str | None) -> bool:
        """세션 토큰이 등록되어 있고 만료되지 않았는지 확인한다."""
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if exp <= self.clock():
                del self._sessions[token]
                return False
            return True

    def end(self, token: str | None) -> None:
        """세션을 지운다. 없는 토큰이면 아무것도 하지 않는다."""
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _recent(self, addr: str, now: float) -> list[float]:
        times = [t for t in self._failures.get(addr, []) if now - t < FAIL_WINDOW_S]
        if times:
            self._failures[addr] = times
        else:
            self._failures.pop(addr, None)
        return times

    def blocked_for(self, addr: str) -> float:
        """이 주소가 아직 막혀 있으면 남은 초를, 아니면 0을 반환한다."""
        now = self.clock()
        with self._lock:
            times = self._recent(addr, now)
            if len(times) < FAIL_LIMIT:
                return 0.0
            return max(1.0, FAIL_WINDOW_S - (now - times[-FAIL_LIMIT]))

    def failed(self, addr: str) -> int:
        """로그인 실패를 기록하고 ``FAIL_WINDOW_S`` 안의 실패 횟수를 반환한다."""
        now = self.clock()
        with self._lock:
            # 기간이 지난 실패만 남은 다른 주소는 지운다. 기간 안의 실패만 세므로 결과는 같다
            for other in [a for a, ts in self._failures.items() if a != addr and not any(now - t < FAIL_WINDOW_S for t in ts)]:
                del self._failures[other]
            times = self._recent(addr, now)
            times.append(now)
            self._failures[addr] = times
            return len(times)

    def succeeded(self, addr: str) -> None:
        """로그인에 성공한 주소의 실패 기록을 지운다."""
        with self._lock:
            self._failures.pop(addr, None)


def _client_addr(request: Request) -> str:
    return request.client.host if request.client else "-"


def _set_cookie(request: Request, resp: JSONResponse, token: str) -> None:
    # 사내망은 http로 서비스한다. 그래서 Secure는 https 요청일 때만 붙인다
    resp.set_cookie(ADMIN_COOKIE, token, max_age=SESSION_TTL_S, path=ADMIN_COOKIE_PATH, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")


# ---------------------------------------------------------------------------
# 실행 폴더 읽기
# ---------------------------------------------------------------------------
def safe_file(run_dir: Path, rel: str) -> Path | None:
    """실행 폴더 안의 파일 경로를 반환한다. 경로의 어느 부분이라도 심볼릭 링크이거나 일반 파일이 아니면 None을 반환한다."""
    p = run_dir
    try:
        for part in rel.split("/"):
            p = p / part
            if p.is_symlink():
                return None
        return p if p.is_file() else None
    except OSError:
        return None


def iter_run_dirs(out_root: Path) -> Iterator[Path]:
    """출력 폴더 바로 아래의 실행 폴더 후보를 이름순으로 내보낸다.
    심볼릭 링크, 폴더가 아닌 항목, ``_``나 ``.``로 시작하는 이름, 실행 id 형식이 아닌 이름은 건너뛴다."""
    try:
        entries = sorted(os.scandir(out_root), key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        try:
            if e.name.startswith(("_", ".")) or not RUN_ID_RE.match(e.name) or e.is_symlink() \
                    or not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        yield Path(e.path)


def _read_json(run_dir: Path, rel: str) -> tuple[dict[str, Any] | None, bool]:
    # 내용과 파일이 깨졌는지를 반환한다. 파일이 없으면 (None, False)다
    p = safe_file(run_dir, rel)
    if p is None:
        return None, False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, True
    return (data, False) if isinstance(data, dict) else (None, True)


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def project_of(upload: str | None, project_name: str | None, run_id: str, entered: str | None = None) -> str:
    """실행의 프로젝트 이름을 정한다. 아래 순서로 처음 있는 값을 쓴다.
    업로드할 때 입력한 프로젝트명 -> 올린 파일 이름에서 확장자를 뺀 값 -> 보고서의 프로젝트 이름 -> 실행 id의 앞부분."""
    if entered and entered.strip():
        return entered.strip()
    if upload:
        name = upload.replace("\\", "/").rsplit("/", 1)[-1].strip()
        stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
        if stem.strip():
            return stem.strip()
    if project_name and project_name.strip():
        return project_name.strip()
    m = _RUN_SLUG.match(run_id)
    return m.group(1) if m else run_id


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _model_key(name: Any) -> str:
    # 실제 모델 이름이면 그대로 반환한다. 이전 실행의 `unknown`, `?`, 빈 이름이면 ""(이름 없음)다
    return real_model_name(name if isinstance(name, str) else "")


def _new_row(model: str) -> dict[str, Any]:
    return {"model": model, "calls": 0, "input_tokens": 0, "output_tokens": 0, "served": []}


def _add_usage(rows: dict[str, dict[str, Any]], model: Any, calls: Any, inp: Any, outp: Any,
               served: Any = None) -> None:
    # 같은 이름의 행에 더한다. 응답이 알려 준 모델 이름은 순서를 지키며 합친다
    key = _model_key(model)
    r = rows.setdefault(key, _new_row(key))
    r["calls"] += _int(calls)
    r["input_tokens"] += _int(inp)
    r["output_tokens"] += _int(outp)
    if isinstance(served, list):
        r["served"] = merge_model_names(r["served"], served)


def model_row(r: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """모델 행 하나를 API의 ``by_model`` 형태로 만든다. ``served``는 알 때만, ``unnamed``는 이름 없는 행에만 넣는다.

    `model` is the asked name; "" for older runs whose name was `unknown` / `?` / empty."""
    out = {"model": r["model"], "calls": r["calls"], "input_tokens": r["input_tokens"],
           "output_tokens": r["output_tokens"], **extra}
    if r.get("served"):
        out["served"] = list(r["served"])
    if not r["model"]:
        out["unnamed"] = True
    return out


def _usage_from_report(rep: dict[str, Any], run_dir: Path | None = None) \
        -> tuple[dict[str, int], list[dict[str, Any]], float | None, str | None]:
    # 보고서의 합계, 모델별 사용량, 비용, 통화를 읽는다. 값이 이상해도 예외를 던지지 않는다.
    # 이름 없는 행(이전 실행)은 sbmig.log의 role로 이름을 붙인다. 호출마다 role이 있고 수가 꼭 맞을 때만 한다
    total = rep.get("llm_usage") if isinstance(rep.get("llm_usage"), dict) else {}
    rows: dict[str, dict[str, Any]] = {}
    for u in rep.get("llm_usage_by_model") or []:
        if not isinstance(u, dict):
            continue
        _add_usage(rows, u.get("model"), u.get("calls"), u.get("input_tokens"), u.get("output_tokens"),
                   u.get("served"))
    sums = {k: sum(r[k] for r in rows.values()) for k in ("calls", "input_tokens", "output_tokens")}
    usage = {k: _int(total.get(k)) or sums[k] for k in ("calls", "input_tokens", "output_tokens")}
    if not rows and any(usage.values()):
        _add_usage(rows, total.get("model"), usage["calls"], usage["input_tokens"], usage["output_tokens"],
                   total.get("served"))
    cost = rep.get("llm_cost") if isinstance(rep.get("llm_cost"), dict) else None
    model_cost: dict[str, float | None] = {}
    run_cost: float | None = None
    currency: str | None = None
    if cost is not None:
        for m in cost.get("by_model") or []:
            if not isinstance(m, dict):
                continue
            key = _model_key(m.get("model"))
            c = float(m["cost"]) if isinstance(m.get("cost"), (int, float)) else None
            # 이름 없는 행 여러 개가 합쳐지면 비용도 더한다. 하나라도 없으면 비용은 모르는 값으로 둔다
            if key in model_cost:
                prev = model_cost[key]
                model_cost[key] = None if prev is None or c is None else prev + c
            else:
                model_cost[key] = c
        if not cost.get("unpriced_models") and not cost.get("usage_missing") \
                and isinstance(cost.get("total"), (int, float)):
            run_cost = float(cost["total"])
            currency = _str(cost.get("currency"))
    if "" in rows and run_dir is not None:
        named = _names_from_old_log(run_dir, rows[""])
        if named:
            del rows[""]
            model_cost.pop("", None)
            for r in named.values():
                _add_usage(rows, r["model"], r["calls"], r["input_tokens"], r["output_tokens"])
    by_model = []
    for r in rows.values():
        c = model_cost.get(r["model"])
        if c is None and run_cost is not None and not (r["input_tokens"] or r["output_tokens"]):
            c = 0.0
        by_model.append(model_row(r, total_tokens=r["input_tokens"] + r["output_tokens"], cost=c))
    return usage, by_model, run_cost, currency


def _scan_log(run_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    # sbmig.log의 호출 줄을 읽는다. 모델별 행, role로 이름을 찾은 이전 줄의 행, role을 모르는 이전 줄 수를 반환한다
    rows: dict[str, dict[str, Any]] = {}
    recovered: dict[str, dict[str, Any]] = {}
    orphans = 0
    p = safe_file(run_dir, "sbmig.log")
    if p is None:
        return rows, recovered, orphans
    pending: str | None = None                       # 바로 앞 요청 줄의 params.role
    try:
        with open(p, "rb") as fh:
            for line in fh:
                if b"llm call provider=" not in line:
                    continue
                m = LLM_CALL_LINE.search(line)
                if not m:
                    a = LLM_ATTEMPT_ROLE.search(line)
                    if a:
                        pending = _model_key(a.group(1).decode("utf-8", "replace")) or None
                    continue
                model = m.group(1).decode("utf-8", "replace")
                served_m = LLM_SERVED.search(line)
                served = served_m.group(1).decode("utf-8", "replace").split(",") if served_m else None
                _add_usage(rows, model, 1, m.group(2), m.group(3), served)
                if not _model_key(model):
                    if pending:
                        _add_usage(recovered, pending, 1, m.group(2), m.group(3))
                    else:
                        orphans += 1
                pending = None
    except OSError:
        pass
    return rows, recovered, orphans


def _names_from_old_log(run_dir: Path, unnamed: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    # 이전 실행의 이름 없는 사용량을 sbmig.log의 요청 줄(params.role)로 나눈다. 확실할 때만 반환한다.
    # 이름 없는 응답 줄마다 앞에 role이 있고, 그 합계가 보고서의 이름 없는 행과 호출, 토큰까지 같아야 한다
    _, recovered, orphans = _scan_log(run_dir)
    if orphans or not recovered:
        return None
    for k in ("calls", "input_tokens", "output_tokens"):
        if sum(r[k] for r in recovered.values()) != unnamed[k]:
            return None
    return recovered


def usage_from_log(run_dir: Path) -> dict[str, dict[str, Any]]:
    """보고서가 없는 실행의 LLM 사용량을 sbmig.log의 ``llm call`` 줄에서 모델별로 모은다.
    큰 로그도 한 줄씩 읽는다. 이전 줄의 이름 없는 모델은 모든 줄에 앞 요청 줄의 role이 있을 때만 그 이름으로 센다.

    Line shape: `llm call provider=... model=... in=... out=...` (one per answered call); ` served=a,b`
    at the end of a newer line goes to `served`. Without a role for every placeholder line the calls stay
    under "" (unnamed)."""
    rows, recovered, orphans = _scan_log(run_dir)
    if "" in rows and recovered and not orphans:
        del rows[""]
        for r in recovered.values():
            _add_usage(rows, r["model"], r["calls"], r["input_tokens"], r["output_tokens"])
    return {k: _plain(r) for k, r in rows.items()}


def _plain(r: dict[str, Any]) -> dict[str, Any]:
    out = {"calls": r["calls"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}
    if r.get("served"):
        out["served"] = list(r["served"])
    return out


def usage_from_journal(run_dir: Path) -> dict[str, dict[str, Any]]:
    """journal.jsonl의 수정 루프 LLM 이벤트에서 모델별 사용량을 모은다. sbmig.log에 호출 줄이 없을 때 쓴다.
    대상 이벤트는 ``llm_script``, ``llm_verify_guide``다. 읽지 못한 줄은 건너뛴다."""
    return journal_usage_checked(run_dir)[0]


def journal_usage_checked(run_dir: Path, strict: bool = False) -> tuple[dict[str, dict[str, Any]], bool]:
    """journal.jsonl에서 수정 루프의 LLM 사용량을 모은다. 저널을 끝까지 읽었는지도 함께 반환한다.
    - 파일을 끝까지 읽지 못했거나 파싱한 줄이 JSON 객체가 아니면 ``complete``가 False다. 읽은 행은 그래도 반환한다
    - ``strict``가 아니면 LLM 이벤트가 들어 있는 줄만 파싱한다. ``strict``면 빈 줄이 아닌 모든 줄을 파싱한다
    - 저널이 없으면 ``({}, True)``를 반환한다"""
    rows: dict[str, dict[str, Any]] = {}
    complete = True
    p = safe_file(run_dir, "journal.jsonl")
    if p is None:
        return {}, True
    try:
        with open(p, "rb") as fh:
            for line in fh:
                if b'"llm_' not in line and not (strict and line.strip()):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    complete = False                 # 읽지 못한 줄이 있다
                    continue
                if strict and not isinstance(ev, dict):
                    complete = False
                    continue
                if not isinstance(ev, dict) or ev.get("event") not in LLM_JOURNAL_EVENTS:
                    continue
                tok = ev.get("tokens") if isinstance(ev.get("tokens"), dict) else {}
                calls = _int(ev.get("calls"))
                if not (calls or tok):
                    continue
                _add_usage(rows, ev.get("model"), calls, tok.get("input"), tok.get("output"), ev.get("served"))
    except OSError:
        complete = False
    return {k: _plain(r) for k, r in rows.items()}, complete


def _result_word(exit_code: Any, rep: dict[str, Any] | None) -> str | None:
    # 결과 줄과 같은 단어를 쓴다. exit 0은 ok, 2는 incomplete, 3은 aborted다
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        word = _EXIT_RESULT.get(exit_code, f"exit {exit_code}")
        return _converted_or(word, rep)
    if rep is None:
        return None
    flags = [f for f in rep.get("flags") or [] if isinstance(f, str)]
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return "aborted"
    if rep.get("success") is True and not rep.get("blocked"):
        return "ok"
    return _converted_or("incomplete", rep)


def _converted_or(word: str, rep: Any) -> str:
    # 사내 프레임워크 실행이 JDK 21, Jakarta 변환을 마쳤으면 빌드가 실패해도 미완료 대신 변환 완료로 보여 준다
    if word != "incomplete" or not isinstance(rep, dict):
        return word
    from sbmig.report.result_line import conversion_complete
    try:
        return "converted" if conversion_complete(rep) is True else word
    except Exception:  # noqa: BLE001 - 보고서 모양 때문에 사용량 응답이 실패하면 안 된다
        return word


def _scope_of(rep: dict[str, Any] | None) -> str | None:
    if rep is None:
        return None
    plan = rep.get("plan")
    if isinstance(plan, dict):
        return _str(plan.get("scope")) or "full"
    profile = rep.get("profile")
    if isinstance(profile, dict) and profile.get("reduced_scope"):
        return "jdk-jakarta"
    return None


def recorded_upload_name(run_dir: Path) -> str | None:
    """실행 기록(request.json, web-status.json)에서 사람이 올린 파일 이름을 찾는다. 없으면 None을 반환한다."""
    req, _ = _read_json(run_dir, "control/request.json")
    web, _ = _read_json(run_dir, "web-status.json")
    return _str((req or {}).get("upload_name")) or _str((req or {}).get("upload")) or _str((web or {}).get("upload"))


def _recorded_flow(run_dir: Path) -> str | None:
    # 실행 방식을 request.json, web-status.json, report.json 순으로 찾는다
    for rel in ("control/request.json", "web-status.json", "report.json"):
        data, _ = _read_json(run_dir, rel)
        flow = _str((data or {}).get("flow"))
        if flow:
            return flow
    return None


def _upload_file_name(run_id: str, upload: str | None) -> str | None:
    # in-process 모드가 _uploads에 저장한 이름(<실행 id>-<정리한 원래 이름>)을 만든다. 규칙은 RunManager.start_file과 같다
    if not upload:
        return None
    base = re.split(r"[\\/]", upload)[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "project.zip"
    return f"{run_id}-{safe}"


def find_source(run_dir: Path, upload: str | None = None, live_zip: Path | None = None) -> Path | None:
    """실행에 사람이 올린 원본 ZIP의 경로를 찾는다. 없으면 None을 반환한다. 심볼릭 링크를 거치는 경로는 고르지 않는다.
    - Job 모드의 ``<run dir>/source.zip``을 먼저 본다
    - 다음은 in-process의 ``_uploads/<실행 id>-<이름>``이다. ``live_zip``, 기록된 이름, 하나뿐인 ``<실행 id>-*`` 순으로 찾는다
    - 마지막으로 조치만 하는 실행의 ``remediation/original/source.zip``을 본다
    - 전체 실행의 ``remediation/original/``은 쓰지 않는다. 이관을 마친 프로젝트라 원본이 아니다"""
    run_dir = Path(run_dir)
    hit = safe_file(run_dir, SOURCE_ZIP)
    if hit is not None:
        return hit
    out_root = run_dir.parent
    rid = run_dir.name
    names: list[str] = []
    if live_zip is not None and Path(live_zip).parent == out_root / UPLOADS_DIR:
        names.append(Path(live_zip).name)
    built = _upload_file_name(rid, upload if upload is not None else recorded_upload_name(run_dir))
    if built:
        names.append(built)
    for name in names:
        hit = safe_file(out_root, f"{UPLOADS_DIR}/{name}")
        if hit is not None:
            return hit
    # 기록된 이름으로 찾지 못하면 이 실행 id로 시작하는 업로드가 하나뿐일 때만 그것을 쓴다
    try:
        found = [e.name for e in os.scandir(out_root / UPLOADS_DIR)
                 if e.name.startswith(rid + "-") and not e.name.startswith(".")]
    except OSError:
        found = []
    if len(found) == 1:
        hit = safe_file(out_root, f"{UPLOADS_DIR}/{found[0]}")
        if hit is not None:
            return hit
    if _recorded_flow(run_dir) == FLOW_REMEDIATION:
        return safe_file(run_dir, REMEDIATION_ORIGINAL)
    return None


def source_info(run_dir: Path, upload: str | None = None) -> tuple[bool, int | None]:
    """사용량 행에 넣을 원본 유무와 크기를 반환한다. 예외를 던지지 않는다."""
    try:
        p = find_source(run_dir, upload)
        return (True, p.stat().st_size) if p is not None else (False, None)
    except Exception:  # 행 하나 때문에 목록이 실패하면 안 된다
        log.debug("admin usage: source of %s not readable", run_dir, exc_info=True)
        return False, None


def source_download_name(run_id: str, upload: str | None) -> str:
    """내려받을 원본의 파일 이름을 만든다. 원래 이름을 알면 ``<실행 id>-<원래 이름>``, 모르면 ``<실행 id>-source.zip``이다.
    원래 이름에서 경로와 제어 문자는 뺀다."""
    base = re.split(r"[\\/]", str(upload or ""))[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    return f"{run_id}-{base}" if base else f"{run_id}-source.zip"


def content_disposition(filename: str) -> str:
    """첨부 파일 헤더를 만든다. ASCII가 아닌 이름은 RFC 5987의 ``filename*``로 보내고 ASCII 대체 이름을 함께 둔다."""
    from urllib.parse import quote
    fallback = "".join(ch if " " <= ch < "\x7f" and ch not in '"\\' else "_" for ch in filename)
    if fallback == filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def has_logs(run_dir: Path) -> bool:
    """실행 폴더에 ZIP에 넣을 로그 파일이 하나라도 있는지 확인한다. sbmig.log, journal.jsonl, remediation.log를 본다."""
    return any(safe_file(run_dir, m) is not None for m in LOG_MEMBERS if m != "control/request.json")


def run_usage(run_dir: Path, status: dict[str, Any] | None = None, live: bool = False) -> dict[str, Any] | None:
    """실행 폴더 하나의 사용량 행을 만든다. 실행 폴더가 아니면 None을 반환한다.
    - ``status``는 ``RunManager.status``의 결과다. 상태 단어, 종료 코드, 올린 이름을 여기서 가져온다
    - ``live``는 메모리에 있는 실행이라는 뜻이다. in-process 실행에는 request.json이 없다
    - 사용량과 비용에는 취약점 조치의 수정 루프 호출도 들어간다. 그 몫은 ``remediation_calls``에 있다

    `usage_partly_unknown: true` (only then) = a part of the remediation's usage is unknown; `cost` is
    then the sum of the priced rows, or None when no row with calls is priced."""
    rid = run_dir.name
    req, req_bad = _read_json(run_dir, "control/request.json")
    rep, rep_bad = _read_json(run_dir, "report.json")
    web, _ = _read_json(run_dir, "web-status.json")
    if req is None and rep is None and not req_bad and not rep_bad and web is None and not live:
        return None
    st = status or {}
    upload = _str((req or {}).get("upload_name")) or _str((req or {}).get("upload")) or _str(st.get("upload")) \
        or _str((web or {}).get("upload"))
    entered = _str((req or {}).get("project")) or _str(st.get("project")) or _str((web or {}).get("project"))
    project = project_of(upload, _str((rep or {}).get("project_name")), rid, entered)
    accepted = _parse_time((req or {}).get("accepted_at")) or _parse_time((rep or {}).get("started_at"))
    if accepted is None:
        try:
            accepted = datetime.fromtimestamp(run_dir.stat().st_mtime, timezone.utc)
        except OSError:
            accepted = datetime.fromtimestamp(0, timezone.utc)
    flow = _str((req or {}).get("flow")) or _str((rep or {}).get("flow")) or _str(st.get("flow")) or "full"
    exit_code = st.get("exit_code")
    if exit_code is None:
        job, _ = _read_json(run_dir, "job-status.json")
        exit_code = (job or {}).get("exit_code", (web or {}).get("exit_code"))
    cost: float | None = None
    currency: str | None = None
    rem_calls = 0
    rem_unknown = False
    if rep is not None:
        usage, by_model, cost, currency = _usage_from_report(rep, run_dir)
        usage, by_model, cost, currency, rem_calls = add_remediation_usage(run_dir, usage, by_model, cost, currency)
        known = True
        rem_unknown = remediation_usage_unknown(run_dir)
    else:
        found = usage_from_log(run_dir) or usage_from_journal(run_dir)
        by_model = [model_row({"model": m, **u}, total_tokens=u["input_tokens"] + u["output_tokens"], cost=None)
                    for m, u in sorted(found.items())]
        usage = {k: sum(r[k] for r in by_model) for k in ("calls", "input_tokens", "output_tokens")}
        known = bool(found)
    if known and cost is None and not any(usage.values()) and not rem_unknown:
        cost = 0.0                                   # 호출이 없었으면 가격이 없어도 비용은 0이다
    state = _str(st.get("state")) or ("finished" if rep is not None or exit_code is not None else "unknown")
    return {"run_id": rid, "project": project, "accepted_at": accepted.isoformat(timespec="seconds"),
            "_t": accepted, "_currency": currency,
            "flow": flow, "scope": _scope_of(rep), "status": state, "result": _result_word(exit_code, rep),
            **usage, "total_tokens": usage["input_tokens"] + usage["output_tokens"], "cost": cost,
            **({"remediation_calls": rem_calls} if rem_calls else {}),
            # 조치 수정 루프의 사용량 일부를 알 수 없으면 화면이 호출 칸과 비용 칸에 그렇게 표시한다
            **({"usage_partly_unknown": True} if rem_unknown else {}),
            "by_model": sorted(by_model, key=lambda r: (-r["total_tokens"], r["model"])),
            "usage_known": known, "partial": bool(req_bad or rep_bad), "has_logs": has_logs(run_dir),
            **dict(zip(("has_source", "source_bytes"), source_info(run_dir, upload)))}


def add_remediation_usage(run_dir: Path, usage: dict[str, int], by_model: list[dict[str, Any]], cost: float | None,
                          currency: str | None) \
        -> tuple[dict[str, int], list[dict[str, Any]], float | None, str | None, int]:
    """이관 보고서의 사용량에 취약점 조치의 수정 루프가 쓴 사용량을 모델별로 더한다. 보고서 파일은 바꾸지 않는다.
    조치 몫은 ``remediation/state.json``에서 읽는다. 단가를 모르는 조치 행이 있으면 실행 비용은 모르는 값으로 둔다.
    조치가 호출하지 않았으면 받은 값을 그대로 반환한다.

    Rules:
    - rows of the same asked name are merged (calls / tokens added, `served` merged, cost = the sum or
      None when either side is unknown); other models get rows of their own;
    - the run's cost stays known only when every remediation row is priced in the report's currency (a
      migration without calls counts 0);
    - partly unknown usage (`fix_usage()["usage_unknown"]`): calls whose tokens are unknown
      (`by_model[].tokens_unknown`) add their calls to the row of their name and nothing to any cost, and
      that row gets `usage_partly_unknown: true`; the run's cost is the sum of the priced rows when at
      least one row with calls is priced, else None (never a number for nothing known).
    Returns (usage, by_model, cost, currency, remediation calls). The log fallback for runs without a
    report already holds these calls (the fix loop's `llm call` lines are in sbmig.log), so it is never
    combined with this."""
    data, _ = _read_json(run_dir, CHAIN_STATE)
    if data is None:
        return usage, by_model, cost, currency, 0
    from sbmig.remediations.fixer import fix_usage
    fx = fix_usage(data)
    priced = cost is not None and any(_int(r.get("calls")) for r in by_model)   # 비용을 아는 이관 행이 있는지
    if fx.get("usage_unknown") and not (fx["calls"] or fx["input_tokens"] or fx["output_tokens"]):
        # 사용량을 하나도 읽지 못한 루프만 있으면 숫자는 그대로 둔다. 비용은 아는 행이 있을 때만 남긴다
        return usage, by_model, cost if priced else None, currency, 0
    if not (fx["calls"] or fx["input_tokens"] or fx["output_tokens"]):
        return usage, by_model, cost, currency, 0
    usage = {k: usage[k] + _int(fx[k]) for k in ("calls", "input_tokens", "output_tokens")}
    rows = {r["model"]: dict(r) for r in by_model}
    base = cost if cost is not None else (0.0 if not by_model else None)
    run_cost = base
    # 토큰을 아는 행을 먼저 더한다. 토큰 수를 모르는 호출은 같은 이름의 행에 호출 수만 더한다
    for f in sorted(fx["by_model"], key=lambda f: bool(f.get("tokens_unknown"))):
        key = _model_key(f.get("model"))
        if f.get("tokens_unknown"):
            r = rows.get(key)
            if r is None:
                r = rows[key] = model_row({"model": key, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                                           "served": []}, total_tokens=0, cost=None)
            r["calls"] += _int(f.get("calls"))
            r["usage_partly_unknown"] = True         # 이 행의 호출 일부는 토큰과 비용을 모른다
            continue
        c = f.get("cost") if isinstance(f.get("cost"), (int, float)) else None
        fcur = _str(f.get("currency"))
        if c is not None and fcur and currency and fcur != currency:
            c = None                                 # 통화가 다르면 더하지 않는다
        if c is not None and fcur and not currency:
            currency = fcur
        run_cost = None if run_cost is None or c is None else run_cost + c
        priced = priced or (c is not None and _int(f.get("calls")) > 0)
        r = rows.get(key)
        if r is None:
            r = model_row({"model": key, "calls": 0, "input_tokens": 0, "output_tokens": 0, "served": []},
                          total_tokens=0, cost=0.0)
            rows[key] = r
        r["calls"] += _int(f.get("calls"))
        r["input_tokens"] += _int(f.get("input_tokens"))
        r["output_tokens"] += _int(f.get("output_tokens"))
        r["total_tokens"] = r["input_tokens"] + r["output_tokens"]
        r["cost"] = None if r.get("cost") is None or c is None else round(r["cost"] + c, 6)
        served = merge_model_names(r.get("served") or [], f.get("served") or [])
        served = [n for n in served if n.lower() != key.lower()]
        if served:
            r["served"] = served
    if fx.get("usage_unknown") and not priced:
        run_cost = None                              # 비용을 아는 행이 하나도 없으면 비용도 모른다
    return (usage, list(rows.values()), None if run_cost is None else round(run_cost, 6), currency,
            _int(fx["calls"]))


def remediation_usage_unknown(run_dir: Path) -> bool:
    """실행 폴더의 취약점 조치 사용량 일부를 알 수 없는지 확인한다(``fixer.fix_usage``의 ``usage_unknown``).
    사용량을 읽지 못한 루프나 토큰 수가 없는 호출이 있으면 True다. 상태 파일이 없거나 읽지 못하면 False다."""
    data, _ = _read_json(run_dir, CHAIN_STATE)
    if data is None:
        return False
    from sbmig.remediations.fixer import fix_usage
    return bool(fix_usage(data).get("usage_unknown"))


def _sum_cost(costs: Iterable[float | None]) -> float | None:
    # 하나라도 가격이 없으면 합계를 내지 않는다. 일부만 더한 합계는 오해를 부른다
    total = 0.0
    for c in costs:
        if c is None:
            return None
        total += c
    return round(total, 6)


def _add(into: dict[str, Any], row: dict[str, Any]) -> None:
    for k in ("calls", "input_tokens", "output_tokens", "total_tokens"):
        into[k] += row[k]


def collect_usage(out_root: Path, status_of: Callable[[str], dict[str, Any]] | None = None,
                  live_ids: Iterable[str] = ()) -> dict[str, Any]:
    """출력 폴더의 모든 실행을 지금 디스크에서 읽어 ``GET /admin/api/usage`` 응답을 만든다.
    읽지 못한 파일이 있어도 요청은 실패하지 않는다. 그 실행에 ``partial``을 표시한다.
    ``status_of``는 ``RunManager.status``다. ``live_ids``는 메모리에 있는 실행이다.

    A project row, the `totals` and a `by_model` row that sum a run with `usage_partly_unknown` (for a
    model row: a run row of that model that received calls without token counts) carry
    `usage_partly_unknown: true` (left out when false): their numbers, the cost included, are the known
    part."""
    out_root = Path(out_root)
    live = set(live_ids)
    runs: list[dict[str, Any]] = []
    for rd in iter_run_dirs(out_root):
        st: dict[str, Any] | None = None
        if status_of is not None:
            try:
                st = status_of(rd.name)
            except Exception:  # 상태를 읽지 못해도 사용량은 보여 준다
                log.debug("admin usage: status of %s failed", rd.name, exc_info=True)
        try:
            row = run_usage(rd, st, rd.name in live)
        except Exception:  # 실행 하나 때문에 페이지 전체가 실패하면 안 된다
            log.warning("admin usage: run folder %s could not be read", rd.name, exc_info=True)
            row = {"run_id": rd.name, "project": project_of(None, None, rd.name), "accepted_at": None,
                   "_t": datetime.fromtimestamp(0, timezone.utc), "_currency": None, "flow": "full", "scope": None,
                   "status": "unknown", "result": None, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "total_tokens": 0, "cost": None, "by_model": [], "usage_known": False, "partial": True,
                   "has_logs": False, "has_source": False, "source_bytes": None}
        if row is not None:
            runs.append(row)

    zero = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    projects: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    model_costs: dict[str, list[float | None]] = {}
    totals = {"projects": 0, "runs": len(runs), **zero, "_unknown": False}
    for r in runs:
        p = projects.setdefault(r["project"], {"project": r["project"], "runs": 0, **zero, "_costs": [],
                                               "_times": [], "run_list": [], "_unknown": False})
        p["runs"] += 1
        _add(p, r)
        _add(totals, r)
        if r.get("usage_partly_unknown"):
            p["_unknown"] = totals["_unknown"] = True
        p["_costs"].append(r["cost"])
        p["_times"].append(r["_t"])
        p["run_list"].append(r)
        for m in r["by_model"]:
            agg = models.setdefault(m["model"], {"model": m["model"], **zero, "served": [], "_unknown": False})
            _add(agg, m)
            if m.get("usage_partly_unknown"):
                agg["_unknown"] = True
            agg["served"] = sorted(merge_model_names(agg["served"], m.get("served") or []), key=str.lower)
            model_costs.setdefault(m["model"], []).append(m["cost"])
    currencies = {r["_currency"] for r in runs if r["_currency"]}
    total_cost = _sum_cost(r["cost"] for r in runs) if runs else None
    if len(currencies) > 1:                          # 통화가 섞이면 더하지 않는다
        total_cost = None
    # 가격이 매겨진 실행이 모두 같은 통화면 합계가 없어도 통화를 알려 준다. 행마다 비용을 보여 주기 위해서다
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    project_rows = []
    for p in projects.values():
        p["run_list"].sort(key=lambda r: r["_t"], reverse=True)
        times = sorted(p.pop("_times"))
        costs = p.pop("_costs")
        p["cost"] = _sum_cost(costs) if len(currencies) <= 1 else None
        p["unpriced_runs"] = sum(1 for c in costs if c is None)
        p["first_run_at"] = times[0].isoformat(timespec="seconds")
        p["last_run_at"] = times[-1].isoformat(timespec="seconds")
        p["run_list"] = [{k: v for k, v in r.items() if not k.startswith("_") and k != "project"}
                         for r in p["run_list"]]
        project_rows.append({**{k: p[k] for k in ("project", "runs", "calls", "input_tokens", "output_tokens",
                                                   "total_tokens", "cost", "unpriced_runs", "first_run_at",
                                                   "last_run_at", "run_list")},
                             # 사용량 일부를 알 수 없는 실행이 들어 있으면 표시한다. 비용은 아는 만큼의 합계다
                             **({"usage_partly_unknown": True} if p["_unknown"] else {})})
    project_rows.sort(key=lambda p: (-p["total_tokens"], p["project"]))
    totals["projects"] = len(project_rows)
    unknown = totals.pop("_unknown")
    by_model = []
    for name, m in models.items():
        by_model.append(model_row(m, total_tokens=m["total_tokens"],
                                  cost=_sum_cost(model_costs[name]) if len(currencies) <= 1 else None,
                                  **({"usage_partly_unknown": True} if m["_unknown"] else {})))
    by_model.sort(key=lambda m: (-m["total_tokens"], m["model"]))
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "out_root": str(out_root),
            "totals": {**totals, "cost": total_cost, "currency": currency,
                       "unpriced_runs": sum(1 for r in runs if r["cost"] is None),
                       **({"usage_partly_unknown": True} if unknown else {})},
            "by_model": by_model,
            "projects": project_rows}


# ---------------------------------------------------------------------------
# 로그 ZIP
# ---------------------------------------------------------------------------
def run_log_files(run_dir: Path, final_pass_pending: bool = False) -> list[tuple[str, Path]]:
    """실행 하나의 ZIP 항목 (ZIP 안 경로, 파일)을 반환한다.
    ``final_pass_pending``이면 remediation.log는 넣지 않는다. 마지막 마스킹 전이기 때문이다.

    Why each member is safe to ship: `sbmig.log` and `journal.jsonl` are redacted line by line as they are
    written when `SBMIG_REDACT_OUTPUT=1` (the Job modes; the same rule as the run's file route,
    `LIVE_FILES`); `control/request.json` is written once by Main at intake and holds only names, digests
    and the sbmig version (no secret). `remediation/remediation.log` is redacted only by the final pass,
    so `final_pass_pending` (the migration run before its final pass, or an active remediation resume)
    leaves it out."""
    out = []
    for rel in LOG_MEMBERS:
        if final_pass_pending and rel in FINAL_PASS_MEMBERS:
            continue
        p = safe_file(run_dir, rel)
        if p is not None:
            out.append((f"{run_dir.name}/{rel}", p))
    return out


def zip_tmp_dir() -> Path:
    """ZIP 임시 파일을 둘 폴더를 반환한다. 파드의 로컬 임시 폴더이고 공유 볼륨(/data)이 아니다.
    출력 폴더로 대신하지 않는다. ZIP 때문에 실행 폴더와 Maven 저장소가 있는 볼륨이 차면 안 된다."""
    return Path(tempfile.gettempdir())


def remove_legacy_tmp(out_root: Path) -> int:
    """시작할 때 이전 임시 폴더 ``<out root>/_admin``에 남은 ZIP 임시 파일을 지운다. 지운 개수를 반환한다.
    이름이 ``ZIP_TMP_NAME``에 맞는 일반 파일만 지우고 링크는 따라가지 않는다. 폴더는 비었을 때만 지운다."""
    d = Path(out_root) / LEGACY_TMP_DIR
    removed = 0
    try:
        if d.is_symlink() or not d.is_dir():
            return 0
        for e in os.scandir(d):
            try:
                if ZIP_TMP_NAME.match(e.name) and not e.is_symlink() and e.is_file(follow_symlinks=False):
                    os.unlink(e.path)
                    removed += 1
            except OSError:
                log.debug("admin: leftover %s not removed", e.path, exc_info=True)
        try:
            d.rmdir()                                 # 비어 있지 않으면 OSError이고 그대로 둔다
        except OSError:
            pass
    except OSError:
        return removed
    if removed:
        log.info("admin: %d leftover log ZIP(s) removed from %s", removed, d)
    return removed


class ZipPlan:
    """ZIP에 넣을 항목과 크기를 미리 정리한 계획 클래스. 만들기 전에 크기 한도와 남은 공간을 이것으로 확인한다.
    실행 하나의 계획은 뺀 파일과 이유(``left_out``)도 담는다. 복사할 때 실행 폴더 밖을 가리키는 항목은 넣지 않는다.
    ``text_checked`` 항목은 복사할 때 줄마다 마스킹한다. ``identity``로 계획한 그 파일인지 다시 확인한다.

    `members` = (arc, path, size when planned: the bound of its copy); `skipped` = files over
    `ZIP_FILE_LIMIT`. Per-run plans: `left_out` = (arc, English reason) (then `skipped` stays empty and
    `SKIPPED.txt` lists the reasons), `root` = the run folder, `identity` = arc -> (`st_dev`, `st_ino`) from
    the planning `lstat`, `text_checked` = every member but `sbmig.log`, `journal.jsonl` and
    `control/request.json`. The archive of all runs leaves those four empty. See `write_logs_zip`."""

    def __init__(self) -> None:
        self.members: list[tuple[str, Path, int]] = []
        self.skipped: list[tuple[str, int]] = []
        self.left_out: list[tuple[str, str]] = []
        self.root: Path | None = None
        self.text_checked: set[str] = set()
        self.identity: dict[str, tuple[int, int]] = {}
        self.total = 0
        self.runs = 0

    def skipped_count(self) -> int:
        """``SKIPPED.txt``에 기록될 파일 수를 반환한다."""
        return len(self.skipped) + len(self.left_out)

    def worst_case(self) -> int:
        """압축이 전혀 안 될 때의 ZIP 크기 추정값을 반환한다. deflate 블록, 헤더, 추가 항목의 여유를 더한다."""
        return self.total + self.total // 1000 + 1024 * (len(self.members) + 2) + 64 * 1024


def plan_logs_zip(run_dirs: Iterable[Path], withheld: Callable[[str], bool] | None = None) -> ZipPlan:
    """실행 폴더들에서 전체 ZIP에 넣을 파일과 크기를 모은다. ``ZIP_FILE_LIMIT``보다 큰 파일은 건너뛴 목록에 둔다.
    ``withheld(run_id)``가 참인 실행은 remediation.log를 뺀다. 로그가 하나도 없는 실행은 넣지 않는다."""
    plan = ZipPlan()
    for rd in run_dirs:
        pending = bool(withheld(rd.name)) if withheld is not None else False
        files = run_log_files(rd, pending)
        if not any(not arc.endswith("/control/request.json") for arc, _ in files):
            continue                                  # 로그가 없는 실행은 넣지 않는다
        took = False
        for arc, p in files:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > ZIP_FILE_LIMIT:
                plan.skipped.append((arc, size))
                continue
            plan.members.append((arc, p, size))
            plan.total += size
            took = True
        plan.runs += took
    return plan


# 실행 하나의 ZIP에 더 넣는 진단 파일. 마지막 마스킹이 다시 쓰는 텍스트 확장자만 넣는다(redact.TEXT_SUFFIXES)
RUN_ZIP_MAX_FILES = 2000             # 실행 하나의 ZIP에 넣는 파일 수 한도. 넘으면 logs/의 번호가 낮은 파일부터 뺀다
RUN_ZIP_SCAN_LIMIT = 20000           # 계획할 때 보는 폴더 항목 수 한도. logs/는 그 절반까지만 쓴다
RUN_LINE_LIMIT = 1024 * 1024         # 줄 단위로 마스킹하는 줄 길이 한도. 더 긴 줄이 있는 파일은 넣지 않는다
RUN_WHOLE_REDACT_BYTES = 8 * 1024 * 1024   # 이 크기 이하의 파일은 마지막 마스킹처럼 통째로 마스킹한다
# URL 사용자 정보를 찾는 redact의 정규식은 영문자와 숫자가 길게 이어지면 제곱으로 느려진다.
# 이보다 긴 덩어리가 있는 파일은 넣지 않는다
RUN_TOKEN_LIMIT = 4096
_TOKEN_RUN = re.compile(rb"[A-Za-z0-9+.\-]+")
RUN_LIVE_MEMBERS = ("sbmig.log", "journal.jsonl")          # 기록할 때 마스킹되는 파일이라 언제나 넣는다
RUN_REQUEST = "control/request.json"                       # 접수 때 한 번 쓰고 이름과 해시만 담는다. 언제나 넣는다
RUN_TEXT_SUFFIXES = TEXT_SUFFIXES
# 이 이름의 폴더로는 내려가지 않는다. 프로젝트 복사본, 빌드 산출물, LLM 원문 기록이다
RUN_PRUNE_DIRS = frozenset({"accepted", "original", "candidate", "applied", "fixed", "tree", "target", "project",
                            "codediff", "merge-conflicts", "llm-records"})
# LLM에 보낸 요청 원문(구조 검토의 user 프롬프트, llm.record_dir의 기록). 대소문자는 가리지 않는다
RUN_LLM_RECORD = re.compile(r"^(?:review-input\.md|llm-\d+\.json)$", re.IGNORECASE)
_LOG_NUMBER = re.compile(r"^(\d+)-")
REASON_PENDING = "not redacted yet: the run's end-of-run redaction pass is not over or a remediation resume is active"
REASON_NOT_TEXT = "not a text file"
REASON_REPLACED = "not the file that was planned (it was replaced after planning)"
REASON_NAME = "the file name is not UTF-8"
# 마지막 마스킹(redact.redact_file)이 바이너리로 보고 건너뛰는 기준과 같다. 앞 8 KiB에 NUL 바이트가 있으면 바이너리다
BINARY_HEAD_BYTES = 8192
# 계획이 폴더를 읽을 때 쓰는 함수. 테스트가 이 모듈이 읽는 폴더만 셀 수 있도록 이름을 따로 둔다
_scandir = os.scandir


def _binary_head(head: bytes) -> bool:
    # redact.redact_file과 같은 기준이다. 어긋나면 test_v21zch_admin_run_logs의 일치 테스트가 실패한다
    return b"\x00" in head[:BINARY_HEAD_BYTES]


class _LeftOut(Exception):
    # 복사할 때 넣지 않기로 한 항목. 이유는 SKIPPED.txt에 그대로 기록한다
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reason_large(size: int) -> str:
    return f"too large ({size} bytes; the limit is {ZIP_FILE_LIMIT} bytes)"


def _reason_cap() -> str:
    return f"over the per-run file limit ({RUN_ZIP_MAX_FILES} files)"


def _reason_total() -> str:
    return f"over the archive's total size limit ({ZIP_TOTAL_LIMIT} bytes)"


def _reason_links(n: int) -> str:
    return f"a hard link (the file has {n} names)"


def _reason_long() -> str:
    return f"a line longer than {RUN_LINE_LIMIT} bytes; it cannot be redacted line by line"


def _reason_token() -> str:
    return (f"holds a run of more than {RUN_TOKEN_LIMIT} letters and digits; "
            "redacting it would take too long")


def _reason_scan(where: str) -> str:
    return (f"the listing of {where} is not complete (at most {RUN_ZIP_SCAN_LIMIT} folder entries are listed, "
            f"{RUN_ZIP_SCAN_LIMIT // 2} of them under logs/); files here may be missing")


def _reason_unreadable(e: OSError) -> str:
    return f"could not be read ({type(e).__name__})"


def _utf8_name(arc: str) -> bool:
    try:
        arc.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _text_name(name: str) -> bool:
    # 숨은 파일, 마스킹하지 않는 확장자, LLM 원문 기록은 넣지 않는다
    if name.startswith(".") or RUN_LLM_RECORD.match(name):
        return False
    return os.path.splitext(name)[1].lower() in RUN_TEXT_SUFFIXES


def _pruned(name: str) -> bool:
    # 숨은 폴더, 프로젝트 복사본, 산출물 폴더로는 내려가지 않는다. 대소문자는 가리지 않는다
    return name.startswith(".") or name.lower() in RUN_PRUNE_DIRS


class _Scan:
    # 읽은 폴더 항목 수를 센다. 한도에 닿아 다 읽지 못한 폴더와 읽을 수 없는 폴더는 이유와 함께 stopped에 남긴다
    def __init__(self, limit: int) -> None:
        self.left = limit
        self.stopped: list[tuple[str, str]] = []

    def entries(self, d: str, rel: str, part: list[int] | None = None) -> list[os.DirEntry]:
        def spent() -> bool:
            return self.left <= 0 or (part is not None and part[0] <= 0)

        out: list[os.DirEntry] = []
        if spent():
            self.stopped.append((rel, _reason_scan(rel)))     # 한도가 남지 않아 읽지 않은 폴더
            return out
        try:
            with _scandir(d) as it:
                for e in it:
                    if spent():
                        self.stopped.append((rel, _reason_scan(rel)))
                        break
                    self.left -= 1
                    if part is not None:
                        part[0] -= 1
                    out.append(e)
        except OSError as e:
            self.stopped.append((rel, _reason_unreadable(e)))
            return []
        return sorted(out, key=lambda e: e.name)


def _walk_text(run_dir: Path, top: str, scan: _Scan, part: list[int] | None = None) -> list[str]:
    # run_dir/top 아래의 텍스트 파일을 실행 폴더 기준 경로로 반환한다.
    # 링크는 따라가지 않는다. RUN_PRUNE_DIRS와 숨은 폴더로는 내려가지 않는다
    out: list[str] = []
    stack = [top]
    while stack:
        rel = stack.pop()
        subdirs = []
        for e in scan.entries(os.path.join(str(run_dir), *rel.split("/")), rel, part):
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not _pruned(e.name):
                        subdirs.append(f"{rel}/{e.name}")
                elif e.is_file(follow_symlinks=False) and _text_name(e.name):
                    out.append(f"{rel}/{e.name}")
            except OSError:
                continue
        stack.extend(reversed(subdirs))
    return out


def run_diagnostic_files(run_dir: Path, scan_limit: int | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """실행 하나의 ZIP에 넣을 수 있는 파일 목록을 반환한다. 다 읽지 못한 폴더와 그 이유도 함께 반환한다.
    - 파일은 열지 않고 링크는 따라가지 않는다
    - 프로젝트 복사본, 결과 ZIP, diff, LLM 원문 기록은 넣지 않는다
    - 폴더 항목은 ``RUN_ZIP_SCAN_LIMIT``까지 읽는다. logs/는 그 절반까지만 써서 remediation/까지 꼭 읽는다

    The candidate members of one run (run-folder-relative, forward slashes), in this order: `sbmig.log`,
    `journal.jsonl`, `control/request.json`, `remediation/remediation.log`; the other text files at the top of the
    run folder; every text file under `logs/`; the text files under `remediation/` outside its project trees
    (`accepted/`, `original/`, `baseline/tree/`, a round's `candidate/` / `applied/`, a fix loop's `tree/` /
    `fixed/`). A text file = a suffix of `redact.TEXT_SUFFIXES` (what the end-of-run pass rewrites; `.patch` is left
    out on purpose: diffs of source), not hidden, not an LLM request record (`review-input.md`, `llm-<n>.json`);
    names and pruned folders compare without regard to case. `control/` holds nothing else that is shipped.
    Nothing is opened; entries are listed with `admin._scandir` and never followed through a link. Budget: at most
    `scan_limit` (`RUN_ZIP_SCAN_LIMIT`) entries in all, of which `logs/` may use half, so `remediation/` is always
    reached. The second value lists (folder, English reason) for every folder not listed completely: the one
    where a budget ran out, each folder known (pushed for a visit) but not listed after that, and each folder
    whose listing raised `OSError` (`could not be read (<error type>)`)."""
    run_dir = Path(run_dir)
    limit = RUN_ZIP_SCAN_LIMIT if scan_limit is None else scan_limit
    scan = _Scan(limit)
    fixed = [*RUN_LIVE_MEMBERS, RUN_REQUEST, *FINAL_PASS_MEMBERS]
    out = [rel for rel in fixed if safe_file(run_dir, rel) is not None]
    tops = scan.entries(str(run_dir), ".")
    walk: list[str] = []
    for e in tops:
        try:
            if e.is_symlink():
                continue
            if e.is_file(follow_symlinks=False):
                if e.name not in fixed and _text_name(e.name):
                    out.append(e.name)
            elif e.is_dir(follow_symlinks=False) and e.name in ("logs", REMEDIATION_DIR):
                walk.append(e.name)
        except OSError:
            continue
    for name in sorted(walk):
        part = [limit // 2] if name == "logs" else None
        out += [rel for rel in _walk_text(run_dir, name, scan, part) if rel not in fixed]
    return out, scan.stopped


def _drop_order(rels: list[str], keep: set[str]) -> list[str]:
    # 한도를 넘을 때 뺄 순서. logs/ 바로 아래의 번호가 낮은 파일, 번호 없는 logs/ 파일, 나머지의 뒤쪽 순이다
    numbered, plain, rest = [], [], []
    for rel in rels:
        if rel in keep:
            continue
        if rel.startswith("logs/"):
            m = _LOG_NUMBER.match(rel[5:]) if "/" not in rel[5:] else None
            if m:
                numbered.append((int(m.group(1)), rel))
            else:
                plain.append(rel)
        else:
            rest.append(rel)
    return [r for _, r in sorted(numbered)] + sorted(plain) + list(reversed(rest))


def plan_run_logs_zip(run_dir: Path, withheld: Callable[[str], bool] | None = None) -> ZipPlan:
    """실행 하나의 ZIP 계획을 만든다. 로그 네 가지에 더해 logs/, 보고서, 오류 목록, 상태 파일, 조치의 진단 파일을 넣는다.
    - 마지막 마스킹 전이거나 조치 재개가 진행 중이면 두 로그와 request.json만 넣는다. 두 로그는 기록할 때 마스킹된다
    - 파일 수나 전체 크기 한도를 넘으면 정한 순서대로 뺀다. 하드 링크와 UTF-8이 아닌 이름은 넣지 않는다
    - 뺀 파일은 모두 이유와 함께 ``left_out``에 둔다. 파일은 열지 않는다. 크기는 ``lstat``으로만 구한다

    The per-run archive (`GET /admin/api/runs/{id}/logs.zip` and its check). Members: `run_diagnostic_files`. While
    `withheld(run_id)` (the remediation API's rule: the migration's end-of-run redaction pass is still to come, or a
    remediation resume is active) only `sbmig.log`, `journal.jsonl` and `control/request.json` are members and every
    other candidate is named with `REASON_PENDING`. A run without `sbmig.log`, `journal.jsonl` and
    `remediation/remediation.log` gives an empty plan (the README archive). Per candidate: a name that is
    not UTF-8, a hard link (`st_nlink` > 1) and a file over `ZIP_FILE_LIMIT` are named; one that vanished or is no
    regular file is left out quietly. Then, in the drop order (`_drop_order`: the numbered files directly under
    `logs/` lowest first, the other `logs/` files, the rest from the end; never the three always-in files): over
    `RUN_ZIP_MAX_FILES` members, then over `ZIP_TOTAL_LIMIT` bytes (so `check_logs_zip` answers 413 only when the
    always-in files alone are too big). `plan.identity[arc]` = (`st_dev`, `st_ino`) of the planned file, checked
    after opening it for the copy. What the copy itself refuses (binary head, a line over `RUN_LINE_LIMIT`, a file
    swapped after planning) is decided then, so the plan and the check do not count those."""
    run_dir = Path(run_dir)
    plan = ZipPlan()
    plan.root = run_dir
    rid = run_dir.name
    if not any(safe_file(run_dir, rel) is not None for rel in (*RUN_LIVE_MEMBERS, *FINAL_PASS_MEMBERS)):
        return plan                                   # 로그가 없는 실행은 README만 준다
    pending = bool(withheld(rid)) if withheld is not None else False
    rels, stopped = run_diagnostic_files(run_dir)
    always = {*RUN_LIVE_MEMBERS, RUN_REQUEST}
    eligible: dict[str, tuple[str, int, tuple[int, int]]] = {}
    for rel in rels:
        arc = f"{rid}/{rel}"
        if pending and rel not in always:
            plan.left_out.append((arc, REASON_PENDING))
            continue
        if not _utf8_name(arc):
            plan.left_out.append((arc, REASON_NAME))
            continue
        try:
            st = os.lstat(os.path.join(str(run_dir), *rel.split("/")))
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if st.st_nlink > 1:
            plan.left_out.append((arc, _reason_links(st.st_nlink)))
            continue
        if st.st_size > ZIP_FILE_LIMIT:
            plan.left_out.append((arc, _reason_large(st.st_size)))
            continue
        eligible[rel] = (arc, st.st_size, (st.st_dev, st.st_ino))
    # 파일 수 한도와 전체 크기 한도는 실제로 넣을 수 있는 파일만 센다. 빼는 순서는 둘 다 같다
    order = _drop_order(list(eligible), always)
    dropped: dict[str, str] = {}
    over = len(eligible) - RUN_ZIP_MAX_FILES
    for rel in order[:max(over, 0)]:
        dropped[rel] = _reason_cap()
    total = sum(size for rel, (_, size, _) in eligible.items() if rel not in dropped)
    for rel in order:
        if total <= ZIP_TOTAL_LIMIT:
            break
        if rel not in dropped:
            dropped[rel] = _reason_total()
            total -= eligible[rel][1]
    for rel, (arc, size, ident) in eligible.items():
        if rel in dropped:
            plan.left_out.append((arc, dropped[rel]))
            continue
        plan.members.append((arc, run_dir / rel, size))
        plan.total += size
        plan.identity[arc] = ident
        if rel not in always:
            plan.text_checked.add(arc)
    for where, why in stopped:
        plan.left_out.append((f"{rid}/{where}/" if where != "." else f"{rid}/", why))
    plan.runs = 1 if plan.members else 0
    return plan


def _inside(root: Path, path: Path) -> bool:
    # 복사하기 직전에 다시 확인한다. 링크이거나 실행 폴더 밖으로 풀리는 경로는 넣지 않는다
    try:
        if os.path.islink(path):
            return False
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root
    except (OSError, ValueError):
        return False


def check_logs_zip(plan: ZipPlan, tmp_dir: Path | None = None) -> tuple[int, dict[str, Any]] | None:
    """계획대로 ZIP을 만들어도 되는지 확인한다. 안 되면 (HTTP 상태, 오류 본문)을, 되면 None을 반환한다.
    - 압축 전 합계가 ``ZIP_TOTAL_LIMIT``를 넘으면 413 ``too_large``다
    - 임시 폴더의 여유 공간에서 최악의 ZIP 크기를 뺀 값이 ``ZIP_FREE_FLOOR``보다 작으면 507 ``no_space``다"""
    if plan.total > ZIP_TOTAL_LIMIT:
        return 413, {"error": "too_large", "bytes": plan.total, "limit_bytes": ZIP_TOTAL_LIMIT}
    tmp_dir = tmp_dir or zip_tmp_dir()
    try:
        free = int(shutil.disk_usage(str(tmp_dir)).free)
    except OSError:
        free = 0                                      # 확인할 수 없으면 만들지 않는다
    needed = plan.worst_case() + ZIP_FREE_FLOOR
    if free < needed:
        return 507, {"error": "no_space", "needed_bytes": needed, "free_bytes": free}
    return None


def _copy_bounded(zf: zipfile.ZipFile, arc: str, path: Path, limit: int) -> None:
    # 계획할 때의 크기까지만 읽는다. 그 사이 늘어난 부분은 넣지 않는다. 계속 쓰이는 로그가 ZIP을 키우면 안 된다
    with open(path, "rb") as src:
        limit = min(limit, os.fstat(src.fileno()).st_size)
        info = zipfile.ZipInfo.from_file(path, arc)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.file_size = limit                        # zip64 판단에 쓰고 실제 값은 zipfile이 다시 기록한다
        with zf.open(info, "w") as dst:
            left = limit
            while left > 0:
                chunk = src.read(min(ZIP_CHUNK, left))
                if not chunk:
                    break                             # 그 사이 줄어든 파일은 있는 만큼만 넣는다
                dst.write(chunk)
                left -= len(chunk)


def _open_planned(path: Path, ident: tuple[int, int] | None):
    # 계획한 파일을 링크를 따라가지 않고 연다. 일반 파일이 아니거나, 계획한 파일(장치, inode)이 아니거나,
    # 이름이 둘 이상인 파일이면 닫고 _LeftOut을 던진다
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    src = os.fdopen(os.open(path, flags), "rb")
    try:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file any more")
        if ident is not None and (st.st_dev, st.st_ino) != tuple(ident):
            raise _LeftOut(REASON_REPLACED)
        if st.st_nlink > 1:
            raise _LeftOut(_reason_links(st.st_nlink))
    except BaseException:
        src.close()
        raise
    return src, st


def _zip_info(arc: str, st: os.stat_result, size: int) -> zipfile.ZipInfo:
    # ZipInfo.from_file과 같은 값을 열린 파일의 fstat으로 만든다(경로를 다시 찾지 않는다)
    stamp = time.localtime(st.st_mtime)[:6]
    info = zipfile.ZipInfo(arc, stamp if stamp[0] >= 1980 else (1980, 1, 1, 0, 0, 0))
    info.external_attr = (st.st_mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = size                             # zip64 판단에 쓰고 실제 값은 zipfile이 다시 기록한다
    return info


def _check_head(src) -> None:
    # 앞 8 KiB에 NUL이 있으면 마지막 마스킹처럼 바이너리로 보고 넣지 않는다. 계획 크기와 상관없이 본다
    if _binary_head(src.read(BINARY_HEAD_BYTES)):
        raise _LeftOut(REASON_NOT_TEXT)
    src.seek(0)


def _token_runs(chunk: bytes, carry: int) -> int:
    # 조각 안에서 영문자와 숫자가 이어진 가장 긴 길이를 본다. 한도를 넘으면 _LeftOut을 던진다
    # 조각 끝에서 이어지는 길이(다음 조각에 더할 값)를 반환한다. 정규식은 덩어리마다 한 번만 맞추므로 선형이다
    end = 0
    for m in _TOKEN_RUN.finditer(chunk):
        n = m.end() - m.start() + (carry if m.start() == 0 else 0)
        if n > RUN_TOKEN_LIMIT:
            raise _LeftOut(_reason_token())
        end = n if m.end() == len(chunk) else 0
    return end if chunk else carry


def _scan_lines(src, limit: int) -> None:
    # 읽을 범위에 RUN_LINE_LIMIT보다 긴 줄이나 RUN_TOKEN_LIMIT보다 긴 영문자, 숫자 덩어리가 있으면
    # _LeftOut을 던진다. 메모리는 조각 하나만 쓴다
    run, left, carry = 0, limit, 0
    while left > 0:
        chunk = src.read(min(ZIP_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        carry = _token_runs(chunk, carry)
        pos = 0
        while True:
            nl = chunk.find(b"\n", pos)
            if nl < 0:
                run += len(chunk) - pos
                break
            if run + nl - pos > RUN_LINE_LIMIT:
                raise _LeftOut(_reason_long())
            run, pos = 0, nl + 1
        if run > RUN_LINE_LIMIT:
            raise _LeftOut(_reason_long())
    src.seek(0)


def _grew(src) -> bool:
    # 계획 크기까지 읽은 뒤 한 바이트를 더 읽어 본다. 나오면 그 사이 파일이 늘어났다
    return bool(src.read(1))


def _copy_redacted(src, dst, limit: int, line: Callable[[bytes], bytes]) -> None:
    # 계획 크기까지 읽은 바이트를 줄마다 마스킹해 쓴다. 미리 본 뒤 파일이 바뀌어 긴 줄이 나오면 그 줄 대신 표시 한 줄을 쓴다
    # 파일이 그 사이 늘어났으면 잘렸을 수 있는 마지막 줄은 넣지 않는다
    buf = bytearray()
    left = limit
    skipping = False
    while left > 0:
        chunk = src.read(min(ZIP_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        buf += chunk
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            if skipping:
                skipping = False
            else:
                dst.write(line(bytes(buf[start:nl + 1])))
            start = nl + 1
        del buf[:start]
        if len(buf) > RUN_LINE_LIMIT:
            if not skipping:
                dst.write((scrub.WITHHELD + "\n").encode("utf-8"))
            skipping = True
            buf.clear()
    cut = left == 0 and _grew(src)
    if buf and not skipping and not cut:
        dst.write(line(bytes(buf)))


def _redact_whole_text(name: str, text: str, values: list[str]) -> str:
    # 마지막 마스킹(redact._redact_structured)과 같은 규칙이다. .json은 파일 전체를, .jsonl은 줄마다 JSON 문자열 안에서 바꾼다
    # JSON으로 읽히지 않으면 텍스트로 바꾼다. 어긋나면 일치 테스트가 실패한다
    suffix = os.path.splitext(name)[1].lower()
    if suffix == ".json":
        new = redact.redact_json_text(text, values)
        return redact.redact_text(text, values) if new is None else new
    if suffix == ".jsonl":
        parts: list[str] = []
        for row in text.splitlines(keepends=True):
            body = row.rstrip("\r\n")
            end = row[len(body):]
            new = redact.redact_json_text(body, values) if body.strip() else body
            parts.append((redact.redact_text(body, values) if new is None else new) + end)
        return "".join(parts)
    return redact.redact_text(text, values)


def _redact_whole(name: str, data: bytes, values: list[str]) -> bytes:
    # 파일 하나를 통째로 마지막 마스킹과 같은 방법으로 바꾼다. 바뀐 것이 없으면 받은 바이트를 그대로 반환한다
    text = data.decode("utf-8", errors="replace")
    try:
        new = _redact_whole_text(name, text, values)
    except Exception as e:  # noqa: BLE001 - 마스킹하지 못한 파일은 원문 대신 넣지 않는다
        raise _LeftOut(f"cannot be redacted ({type(e).__name__})") from None
    return data if new == text else new.encode("utf-8", errors="replace")


class _Masker:
    # ZIP 하나를 만드는 동안 쓰는 비밀 값과 줄 단위 마스킹 함수. 값은 한 번만 읽는다
    def __init__(self) -> None:
        self.values = list(redact.live_secret_values())
        self.line = redact.Redactor(lambda: self.values).line


def _copy_member(zf: zipfile.ZipFile, arc: str, path: Path, limit: int, ident: tuple[int, int] | None,
                 masker: _Masker | None) -> None:
    # 실행 하나의 ZIP 항목 하나를 복사한다. masker가 None이면 바이트 그대로 쓴다.
    # 아니면 RUN_WHOLE_REDACT_BYTES 이하는 파일 전체를, 그보다 큰 파일은 줄마다 마스킹해서 쓴다
    src, st = _open_planned(path, ident)
    with src:
        size = min(limit, st.st_size)
        if masker is not None:
            _check_head(src)
            if size <= RUN_WHOLE_REDACT_BYTES:
                data = src.read(size)
                if len(data) == size and _grew(src):
                    data = data[:data.rfind(b"\n") + 1]      # 잘렸을 수 있는 마지막 줄은 넣지 않는다
                _token_runs(data, 0)
                out = _redact_whole(arc, data, masker.values)
                with zf.open(_zip_info(arc, st, len(out)), "w") as dst:
                    dst.write(out)
                return
            _scan_lines(src, size)
        with zf.open(_zip_info(arc, st, size), "w") as dst:
            if masker is not None:
                _copy_redacted(src, dst, size, masker.line)
                return
            left = size
            while left > 0:
                chunk = src.read(min(ZIP_CHUNK, left))
                if not chunk:
                    break                             # 그 사이 줄어든 파일은 있는 만큼만 넣는다
                dst.write(chunk)
                left -= len(chunk)


def _skipped_name(arc: str) -> str:
    # SKIPPED.txt에 기록할 이름. UTF-8로 쓸 수 없는 문자는 \x.. 표기로 바꾼다
    return arc.encode("utf-8", "backslashreplace").decode("utf-8")


def write_logs_zip(plan: ZipPlan, tmp_dir: Path | None = None) -> Path:
    """계획대로 deflate ZIP 임시 파일을 만들고 그 경로를 반환한다. 파일은 호출한 쪽이 지운다.
    - 실행 하나의 계획이면 더한 항목을 복사하면서 마스킹한다. 작은 파일은 통째로, 큰 파일은 줄마다 한다
    - 마스킹할 수 없는 항목은 원문 그대로 넣지 않고 뺀다
    - 넣지 못한 파일은 이유와 함께 ``SKIPPED.txt``에 기록한다. 넣은 것이 없으면 ``README.txt``만 넣는다

    Each member is copied in chunks up to its planned size; `SKIPPED.txt` lists the files over `ZIP_FILE_LIMIT`
    (`<arc>\t<bytes>`), or for the plan of one run every left-out file with its reason (`<arc>\t<reason>`).
    The archive of all runs (`plan.root` None) copies bytes unchanged.

    One run (`plan.root` set), per member: the path must still pass `_inside` (no link, resolving inside the run
    folder); it is opened with `O_NOFOLLOW | O_NONBLOCK` and `fstat` must give a regular file with one name and the
    (device, inode) of `plan.identity` (`REASON_REPLACED` / a hard-link line otherwise: a folder or file swapped after
    planning). The three always-in files are then copied byte for byte. Every member of `plan.text_checked` is
    redacted with the values `redact.live_secret_values()` gives once for this archive (`_Masker`); URL userinfo is
    masked even with no value set. It is left out, never copied raw, when its first `BINARY_HEAD_BYTES` hold a NUL
    (`not a text file`), when it holds a run of more than `RUN_TOKEN_LIMIT` letters and digits (the userinfo regex
    of `redact` is quadratic in such a run: 16,000 characters took 0.4 s, 1 MiB would take about 25 minutes), or
    when its redaction raises (`cannot be redacted (<type>)`, e.g. the RecursionError of a deeply nested `.json`).
    - Up to `RUN_WHOLE_REDACT_BYTES`: the planned bytes are read whole and redacted as the end-of-run pass would
      (`_redact_whole_text` mirrors the private `redact._redact_structured`: `redact_json_text` for `.json` and per
      `.jsonl` line, `redact_text` otherwise; an agreement test compares it with `redact.redact_file`). Unchanged
      text keeps its bytes. No line-length rule here: the whole text is redacted at once.
    - Larger: line by line through `redact.Redactor` after `_scan_lines` (a line over `RUN_LINE_LIMIT` = left out;
      one that grew past it after the look is replaced by `scrub.WITHHELD`). A short line of a multi-line value
      and a value behind a JSON `\\u` escape are not recognised there (rule 4 says so).
    The planned size bounds the bytes READ; then one more byte is read (`_grew`) and, when the file holds more, the
    last line (possibly cut) is not written. No `_Masker` (its source raised): every such member is left out. A
    name that is not UTF-8 is left out and named with `\\x..` escapes. The late reasons go into `SKIPPED.txt`,
    written after the members."""
    tmp_dir = tmp_dir or zip_tmp_dir()
    fd, name = tempfile.mkstemp(prefix=ZIP_TMP_PREFIX, suffix=ZIP_TMP_SUFFIX, dir=str(tmp_dir))
    path = Path(name)
    masker: _Masker | None = None
    no_redactor = ""
    if plan.root is not None and plan.text_checked:
        try:
            masker = _Masker()
        except Exception as e:  # noqa: BLE001 - 마스킹할 수 없으면 더한 항목은 하나도 넣지 않는다
            no_redactor = f"cannot be redacted now ({type(e).__name__})"
    try:
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED,
                                                         allowZip64=True) as zf:
            count = 0
            late: list[tuple[str, str]] = []          # 복사하면서 정한 이유. SKIPPED.txt는 항목 뒤에 쓰므로 함께 기록된다
            for arc, p, size in plan.members:
                try:
                    if plan.root is None:
                        _copy_bounded(zf, arc, p, size)
                    else:
                        if not _utf8_name(arc):
                            raise _LeftOut(REASON_NAME)
                        if arc in plan.text_checked and masker is None:
                            raise _LeftOut(no_redactor or REASON_PENDING)
                        if not _inside(plan.root, p):
                            raise OSError("not a file inside the run folder any more")
                        _copy_member(zf, arc, p, size, plan.identity.get(arc),
                                     masker if arc in plan.text_checked else None)
                    count += 1
                except _LeftOut as e:
                    late.append((arc, e.reason))
                except OSError as e:
                    log.warning("admin logs zip: %r skipped: %s", arc, e)
            if plan.skipped:
                lines = [(f"Files larger than {ZIP_FILE_LIMIT} bytes are left out of this archive; "
                          "download them from the server directly."), ""]
                lines += [f"{arc}\t{size}" for arc, size in plan.skipped]
                zf.writestr(SKIPPED_NAME, "\n".join(lines) + "\n")
            if plan.left_out or late:
                lines = ["These files of the run are left out of this archive; the reason is beside each name.", ""]
                lines += [f"{_skipped_name(arc)}\t{reason}" for arc, reason in (*plan.left_out, *late)]
                zf.writestr(SKIPPED_NAME, "\n".join(lines) + "\n")
            if count == 0 and not plan.skipped and not plan.left_out and not late:
                zf.writestr("README.txt", EMPTY_README)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


class ZipRefused(Exception):
    """ZIP을 만들지 않기로 했을 때 던지는 예외 클래스. HTTP 상태와 오류 본문을 담는다."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("error"))
        self.status = status
        self.body = body


def build_logs_zip(run_dirs: Iterable[Path], withheld: Callable[[str], bool] | None = None,
                   tmp_dir: Path | None = None) -> Path:
    """계획, 확인, 만들기를 한 번에 하고 ZIP 임시 파일 경로를 반환한다. 한도에 걸리면 ``ZipRefused``를 던진다.
    호출하는 쪽이 ``ZIP_GATE``를 잡고 있어야 한다. 만든 파일도 호출하는 쪽이 지운다."""
    plan = plan_logs_zip(run_dirs, withheld)
    refusal = check_logs_zip(plan, tmp_dir)
    if refusal is not None:
        raise ZipRefused(*refusal)
    return write_logs_zip(plan, tmp_dir)


class TempFileResponse(StreamingResponse):
    """임시 파일을 조각으로 내보내는 응답 클래스. 응답이 끝나거나 연결이 끊기거나 오류가 나도 파일을 지운다.
    ``on_close``가 있으면 파일을 지운 뒤 한 번 호출한다."""

    def __init__(self, path: Path, filename: str, media_type: str = "application/zip",
                 on_close: Callable[[], None] | None = None):
        self.path = Path(path)
        self._on_close = on_close
        headers = {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"}
        try:
            headers["Content-Length"] = str(self.path.stat().st_size)
        except OSError:
            pass
        super().__init__(self._chunks(), media_type=media_type, headers=headers)

    async def _chunks(self):
        async with await anyio.open_file(self.path, "rb") as fh:
            while True:
                chunk = await fh.read(ZIP_CHUNK)
                if not chunk:
                    return
                yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:                          # Windows에서 아직 열려 있으면 지우지 못한다
                log.warning("admin logs zip: %s could not be removed", self.path)
            self.close()

    def close(self) -> None:
        """``on_close``를 한 번만 호출한다."""
        cb, self._on_close = self._on_close, None
        if cb is not None:
            cb()


class SourceFileResponse(StreamingResponse):
    """올린 원본 ZIP을 조각으로 내보내는 응답 클래스. 파일은 지우지 않는다.
    통째로 읽지 않는다. 응답을 만든 시점의 크기(알린 ``Content-Length``)까지만 보낸다."""

    def __init__(self, path: Path, filename: str, size: int, media_type: str = "application/zip"):
        self.path = Path(path)
        self.size = int(size)
        headers = {"Content-Disposition": content_disposition(filename), "Cache-Control": "no-store",
                   "Content-Length": str(self.size)}
        super().__init__(self._chunks(), media_type=media_type, headers=headers)

    async def _chunks(self):
        left = self.size
        async with await anyio.open_file(self.path, "rb") as fh:
            while left > 0:
                chunk = await fh.read(min(ZIP_CHUNK, left))
                if not chunk:
                    return
                left -= len(chunk)
                yield chunk


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
def remediation_rule(remediations: Any) -> Callable[[str], bool]:
    """remediation.log를 ZIP에서 뺄지 정하는 함수를 반환한다. 조치 API의 규칙(``RemediationManager._withheld``)을 쓴다.
    규칙을 찾지 못하거나 확인에 실패하면 빼는 쪽을 고른다.

    The rule: withheld while the migration run's final pass is still to come OR a remediation resume of the
    run is queued / creating / running (in memory, or recovered from `control/remediation-resume.json` after
    a restart)."""
    rule = getattr(remediations, "_withheld", None) if remediations is not None else None

    def withheld(run_id: str) -> bool:
        if not callable(rule):
            return True
        try:
            return bool(rule(run_id))
        except Exception:  # 모르면 안전한 쪽(넣지 않음)을 고른다
            log.debug("admin logs zip: withheld(%s) failed", run_id, exc_info=True)
            return True
    return withheld


def create_admin_router(manager: Any, auth: AdminAuth, remediations: Any = None) -> APIRouter:
    """관리자 API 라우터를 만든다. ``manager``는 출력 폴더와 실행 상태를 알려 주는 ``RunManager``다.
    ``remediations``가 없으면 요청마다 ``app.state.remediations``를 쓴다. 만들 때 이전 임시 폴더를 한 번 정리한다."""
    router = APIRouter(prefix="/admin/api", include_in_schema=False)
    try:
        remove_legacy_tmp(Path(manager.out_dir))
    except Exception:  # 정리에 실패해도 서버는 뜬다
        log.debug("admin: legacy temp clean-up failed", exc_info=True)

    def logged_in(request: Request) -> bool:
        return auth.valid(request.cookies.get(ADMIN_COOKIE))

    def denied() -> JSONResponse:
        return JSONResponse({"error": "login_required"}, status_code=401, headers={"Cache-Control": "no-store"})

    def withheld_for(request: Request) -> Callable[[str], bool]:
        rem = remediations if remediations is not None else getattr(request.app.state, "remediations", None)
        return remediation_rule(rem)

    def find_run(run_id: str) -> Path | None:
        # 경로는 클라이언트 문자열로 만들지 않고 출력 폴더의 실제 실행 폴더 목록에서 고른다
        return next((d for d in iter_run_dirs(Path(manager.out_dir)) if d.name == run_id), None)

    async def logs_zip(request: Request, dirs: list[Path], filename: str, check_only: bool, one: bool = False):
        nostore = {"Cache-Control": "no-store"}
        if not ZIP_GATE.acquire(blocking=False):
            return JSONResponse({"error": "busy"}, status_code=409, headers=nostore)
        release = True
        try:
            tmp = zip_tmp_dir()
            # 실행 하나의 ZIP은 진단 파일까지 넣는다. 전체 실행의 ZIP은 크기 한도 때문에 로그 네 가지만 넣는다
            if one:
                plan = await run_in_threadpool(plan_run_logs_zip, dirs[0], withheld_for(request))
            else:
                plan = await run_in_threadpool(plan_logs_zip, dirs, withheld_for(request))
            refusal = await run_in_threadpool(check_logs_zip, plan, tmp)
            if refusal is not None:
                log.warning("admin logs zip refused: %s", refusal[1])
                return JSONResponse(refusal[1], status_code=refusal[0], headers=nostore)
            if check_only:
                return JSONResponse({"ok": True, "bytes": plan.total, "runs": plan.runs,
                                     "skipped": plan.skipped_count()}, headers=nostore)
            path = await run_in_threadpool(write_logs_zip, plan, tmp)
            resp = TempFileResponse(path, filename, on_close=ZIP_GATE.release)
            release = False                           # 잠금은 응답이 파일을 지운 뒤 푼다
            return resp
        finally:
            if release:
                ZIP_GATE.release()

    async def all_runs(request: Request, check_only: bool):
        if not logged_in(request):
            return denied()
        out_root = Path(manager.out_dir)
        dirs = await run_in_threadpool(lambda: list(iter_run_dirs(out_root)))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return await logs_zip(request, dirs, f"sbmig-logs-{stamp}.zip", check_only)

    async def one_run(request: Request, run_id: str, check_only: bool):
        if not logged_in(request):
            return denied()
        hit = await run_in_threadpool(find_run, run_id)
        if hit is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return await logs_zip(request, [hit], f"sbmig-logs-{hit.name}.zip", check_only, one=True)

    @router.post("/login")
    async def login(request: Request) -> JSONResponse:
        addr = _client_addr(request)
        wait = auth.blocked_for(addr)
        if wait:
            return JSONResponse({"error": "too_many_attempts", "retry_after_s": int(wait + 0.999)}, status_code=429,
                                headers={"Retry-After": str(int(wait + 0.999))})
        try:
            body = await request.json()
        except ValueError:
            body = None
        given = body.get("password") if isinstance(body, dict) else None
        ok = isinstance(given, str) and await run_in_threadpool(password_matches, given)
        if not ok:
            n = auth.failed(addr)
            log.warning("admin login failed from %s (%d in %d min)", addr, n, FAIL_WINDOW_S // 60)
            if auth.fail_delay_s > 0:
                await asyncio.sleep(auth.fail_delay_s)
            return JSONResponse({"error": "bad_password"}, status_code=401, headers={"Cache-Control": "no-store"})
        auth.succeeded(addr)
        log.info("admin login from %s", addr)
        resp = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
        _set_cookie(request, resp, auth.new_session())
        return resp

    @router.post("/logout")
    def logout(request: Request) -> JSONResponse:
        auth.end(request.cookies.get(ADMIN_COOKIE))
        resp = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
        resp.delete_cookie(ADMIN_COOKIE, path=ADMIN_COOKIE_PATH, httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")
        return resp

    @router.get("/session")
    def session(request: Request) -> JSONResponse:
        return JSONResponse({"authenticated": logged_in(request)}, headers={"Cache-Control": "no-store"})

    @router.get("/usage")
    def usage(request: Request) -> JSONResponse:
        if not logged_in(request):
            return denied()
        data = collect_usage(manager.out_dir, manager.status, list(getattr(manager, "runs", {}) or {}))
        # 보고서의 NaN이나 무한대 비용은 null로 보낸다
        return FiniteJSONResponse(data, headers={"Cache-Control": "no-store"})

    @router.get("/logs.zip")
    async def all_logs(request: Request):
        return await all_runs(request, False)

    @router.get("/logs/check")
    async def all_logs_check(request: Request):
        return await all_runs(request, True)

    @router.get("/runs/{run_id}/logs.zip")
    async def run_logs(request: Request, run_id: str):
        return await one_run(request, run_id, False)

    @router.get("/runs/{run_id}/logs/check")
    async def run_logs_check(request: Request, run_id: str):
        """실행 하나의 ZIP을 만들지 않고 확인만 한다.
        파일을 읽지 않아서 복사할 때 빠질 파일은 세지 못한다.

        What the copy refuses (binary head, a run over `RUN_TOKEN_LIMIT`, a line over `RUN_LINE_LIMIT`, a
        redaction that raised, a file swapped after planning, a vanished one) is unknown here: `skipped` may
        be lower and `bytes` higher than in the archive by exactly those files; `bytes` is the size READ,
        before redaction."""
        return await one_run(request, run_id, True)

    def locate_source(run_id: str) -> tuple[int, dict[str, Any]] | tuple[Path, str, int]:
        # 원본의 (경로, 내려받을 이름, 크기)를 반환한다. 찾지 못하면 (상태 코드, 오류 본문)이다
        if not RUN_ID_RE.match(run_id):
            return 400, {"error": "bad run id"}
        rd = find_run(run_id)
        if rd is None:
            return 404, {"error": "unknown run"}
        run = (getattr(manager, "runs", None) or {}).get(run_id)
        live = getattr(run, "zip_path", None) if run is not None else None
        upload = _str(getattr(run, "upload", None)) if run is not None else None
        upload = upload or recorded_upload_name(rd)
        path = find_source(rd, upload, Path(live) if live else None)
        if path is None:
            return 404, {"error": SOURCE_ERROR}
        try:
            size = path.stat().st_size
        except OSError:
            return 404, {"error": SOURCE_ERROR}
        return path, source_download_name(run_id, upload), size

    async def source(request: Request, run_id: str, check_only: bool):
        if not logged_in(request):
            return denied()
        nostore = {"Cache-Control": "no-store"}
        found = await run_in_threadpool(locate_source, run_id)
        if isinstance(found[0], int):
            return JSONResponse(found[1], status_code=found[0], headers=nostore)
        path, filename, size = found
        if check_only:
            return JSONResponse({"ok": True, "bytes": size, "filename": filename}, headers=nostore)
        log.info("admin source download: run %s, %d bytes", run_id, size)
        return SourceFileResponse(path, filename, size)

    @router.get("/runs/{run_id}/source")
    async def run_source(request: Request, run_id: str):
        return await source(request, run_id, False)

    @router.get("/runs/{run_id}/source/check")
    async def run_source_check(request: Request, run_id: str):
        return await source(request, run_id, True)

    return router
