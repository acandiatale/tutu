"""관리자 페이지의 서버 쪽 모듈. 비밀번호 로그인, 프로젝트별 LLM 토큰 사용량 집계, 로그 ZIP 내려받기를 맡는다.
사용량과 로그는 요청할 때마다 출력 폴더의 실행 폴더를 디스크에서 다시 읽는다.
비밀번호는 솔트를 넣은 PBKDF2 해시로만 코드에 두고, ``SBMIG_ADMIN_PASSWORD``가 있으면 그 값을 쓴다.
세션은 이 프로세스의 메모리에만 있으므로 웹 파드를 다시 띄우면 모두 로그아웃된다.
진입점은 :func:`create_admin_router`, :class:`AdminAuth`, :func:`collect_usage`, :func:`build_logs_zip`이다.

The admin page's server side (v2.1z-ao, the user 2026-11-17: token usage per project so far, behind a fixed password,
and a download of all logs). Routes (all under /admin; `GET /admin` itself, the static page, is served by app.py):

    POST /admin/api/login              {"password": "…"} → 200 + cookie `sbmig_admin` | 401 bad_password | 429
    POST /admin/api/logout             forget the session, clear the cookie
    GET  /admin/api/session            {"authenticated": bool}
    GET  /admin/api/usage              the usage of every run folder under the output root (contract shape)
    GET  /admin/api/logs.zip           sbmig.log / journal.jsonl / control/request.json / remediation/remediation.log of
                                       every run, `<run id>/<file>`, deflated, built in a local temp file and streamed
                                       | 409 busy | 413 too_large | 507 no_space
    GET  /admin/api/logs/check         the same checks without building: {"ok": true, "bytes", "runs", "skipped"}
    GET  /admin/api/runs/{id}/logs.zip the same for one run (+ /runs/{id}/logs/check)

Log ZIP limits (v2.1z-ap, the three-pods review): one archive at a time per process (`ZIP_GATE`, held until the temp file
is deleted), the temp file in the pod's local temp dir (never the shared volume), a free-space floor there, a total cap,
files over a per-file limit listed in `SKIPPED.txt`, each member copied only up to the size it had when planned, and
`remediation/remediation.log` left out while anything may still write that run's remediation.

The password is built in as a salted PBKDF2-HMAC-SHA256 hash (`ADMIN_PASSWORD_PBKDF2`; the plain text never enters the
repository); a non-empty `SBMIG_ADMIN_PASSWORD` (web pod, from the Secret) replaces it. Sessions are random tokens in
this process's memory (12 hours); failed logins wait `fail_delay_s` and an address with `FAIL_LIMIT` failures inside
`FAIL_WINDOW_S` gets 429 until the oldest of them leaves the window (behind a Kubernetes Service every client may share
one address: then the limit is global). The site token guard (`TokenGuard`) still applies in front of all of this."""
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

from .runs import RUN_ID_RE

log = logging.getLogger("sbmig.web.admin")

ADMIN_PASSWORD_ENV = "SBMIG_ADMIN_PASSWORD"
# 회사가 정한 비밀번호의 해시다. 원문은 저장소 어디에도 두지 않는다
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

# 로그 ZIP에 넣는 파일이다. 원본 LLM 기록, result.zip, Maven 빌드 로그, report.md는 넣지 않는다
LOG_MEMBERS = ("sbmig.log", "journal.jsonl", "control/request.json", "remediation/remediation.log")
# 기록할 때 마스킹되지 않으므로 그 실행의 조치를 쓰는 곳이 하나도 없을 때만 넣는다
FINAL_PASS_MEMBERS = ("remediation/remediation.log",)
LEGACY_TMP_DIR = "_admin"            # 예전 임시 폴더(<out root>/_admin)다. 시작할 때 남은 ZIP만 지우고 더는 쓰지 않는다
ZIP_TMP_PREFIX = "sbmig-logs-"
ZIP_TMP_SUFFIX = ".zip"
ZIP_TMP_NAME = re.compile(r"^sbmig-logs-[A-Za-z0-9_-]+\.zip$")
ZIP_TOTAL_LIMIT = 2 * 1024 ** 3      # 압축 전 합계가 이보다 크면 413으로 거절한다
ZIP_FILE_LIMIT = 512 * 1024 ** 2     # 이보다 큰 파일 하나는 넣지 않고 SKIPPED.txt에 적는다
ZIP_FREE_FLOOR = 1024 ** 3           # 임시 폴더에 최악의 경우를 쓰고도 이만큼은 남아야 한다
ZIP_CHUNK = 64 * 1024
EMPTY_README = "No run under this output root has a log file yet.\n"
SKIPPED_NAME = "SKIPPED.txt"
# 한 프로세스에서 ZIP은 한 번에 하나만 만든다. 내려받기가 끝나 임시 파일을 지울 때까지 잡고 있다
ZIP_GATE = threading.Lock()

# sbmig.log에서 LLM 호출 한 번마다 남는 줄이다(sbmig.fix.company / sbmig.fix.client)
LLM_CALL_LINE = re.compile(rb"llm call provider=\S+ model=(\S*) in=(\d+) out=(\d+)")
LLM_JOURNAL_EVENTS = ("llm_script", "llm_verify_guide")
_RUN_SLUG = re.compile(r"^(.+)-\d{8}-\d{6}-[0-9a-f]+$")
_EXIT_RESULT = {0: "ok", 2: "incomplete", 3: "aborted"}


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------
def password_matches(given: str) -> bool:
    """입력한 비밀번호가 맞는지 확인한다. ``SBMIG_ADMIN_PASSWORD``가 있으면 그 값과, 없으면 내장 해시와 비교한다.

    `SBMIG_ADMIN_PASSWORD` (non-empty) replaces the built-in hash; compared with `hmac.compare_digest` either way."""
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
    """관리자 세션과 로그인 실패 횟수를 이 프로세스의 메모리에 두는 클래스.
    테스트는 ``clock``과 ``fail_delay_s``를 바꿀 수 있다.

    Sessions (random token → expiry) and login failures (address → times), in memory, per process. `clock` (monotonic
    seconds) and `fail_delay_s` are attributes so tests can drive them."""

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
        """로그인 실패를 기록하고 창 안의 실패 횟수를 반환한다."""
        now = self.clock()
        with self._lock:
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
    # 사내망은 http로 서비스하므로 Secure는 https 요청일 때만 붙인다
    resp.set_cookie(ADMIN_COOKIE, token, max_age=SESSION_TTL_S, path=ADMIN_COOKIE_PATH, httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")


# ---------------------------------------------------------------------------
# 실행 폴더 읽기
# ---------------------------------------------------------------------------
def safe_file(run_dir: Path, rel: str) -> Path | None:
    """실행 폴더 안의 파일 경로를 반환한다. 경로의 어느 부분이라도 심볼릭 링크이거나 일반 파일이 아니면 None이다.

    `run_dir/rel` when every component is a real (non-symlink) entry and the last one a regular file, else None."""
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
    심볼릭 링크, 폴더가 아닌 항목, ``_``나 ``.``로 시작하는 이름, 실행 id 형식이 아닌 이름은 건너뛴다.

    Candidate run folders directly under the output root: real directories (never a symlink) whose name is a run id."""
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
    # (내용, 깨짐 여부)를 반환한다. 파일이 없으면 (None, False)다
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


def project_of(upload: str | None, project_name: str | None, run_id: str) -> str:
    """실행의 프로젝트 이름을 정한다. 올린 파일 이름에서 확장자를 뺀 값, 보고서의 프로젝트 이름, 실행 id의 앞부분 순이다.

    The uploaded file name without its extension, else the report's project name, else the run id's slug."""
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


def _usage_from_report(rep: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]], float | None, str | None]:
    # 보고서의 합계, 모델별 사용량, 비용, 통화를 읽는다. 값이 이상해도 예외를 던지지 않는다
    total = rep.get("llm_usage") if isinstance(rep.get("llm_usage"), dict) else {}
    rows: dict[str, dict[str, Any]] = {}
    for u in rep.get("llm_usage_by_model") or []:
        if not isinstance(u, dict):
            continue
        model = str(u.get("model") or "?")
        r = rows.setdefault(model, {"model": model, "calls": 0, "input_tokens": 0, "output_tokens": 0})
        for k in ("calls", "input_tokens", "output_tokens"):
            r[k] += _int(u.get(k))
    sums = {k: sum(r[k] for r in rows.values()) for k in ("calls", "input_tokens", "output_tokens")}
    usage = {k: _int(total.get(k)) or sums[k] for k in ("calls", "input_tokens", "output_tokens")}
    if not rows and any(usage.values()):
        model = str(total.get("model") or "?")
        rows[model] = {"model": model, **usage}
    cost = rep.get("llm_cost") if isinstance(rep.get("llm_cost"), dict) else None
    model_cost: dict[str, float] = {}
    run_cost: float | None = None
    currency: str | None = None
    if cost is not None:
        for m in cost.get("by_model") or []:
            if isinstance(m, dict) and isinstance(m.get("cost"), (int, float)):
                model_cost[str(m.get("model") or "?")] = float(m["cost"])
        if not cost.get("unpriced_models") and not cost.get("usage_missing") \
                and isinstance(cost.get("total"), (int, float)):
            run_cost = float(cost["total"])
            currency = _str(cost.get("currency"))
    by_model = []
    for r in rows.values():
        c = model_cost.get(r["model"])
        if c is None and run_cost is not None and not (r["input_tokens"] or r["output_tokens"]):
            c = 0.0
        by_model.append({**r, "total_tokens": r["input_tokens"] + r["output_tokens"], "cost": c})
    return usage, by_model, run_cost, currency


def usage_from_log(run_dir: Path) -> dict[str, dict[str, int]]:
    """보고서가 없는 실행의 LLM 사용량을 sbmig.log의 ``llm call`` 줄에서 모델별로 모은다.
    큰 로그도 한 줄씩 읽어 메모리를 쓰지 않는다.

    Per-model usage from the `llm call provider=… model=… in=… out=…` lines (one per answered call, written by the
    built-in LLM clients), read line by line."""
    out: dict[str, dict[str, int]] = {}
    p = safe_file(run_dir, "sbmig.log")
    if p is None:
        return out
    try:
        with open(p, "rb") as fh:
            for line in fh:
                if b"llm call provider=" not in line:
                    continue
                m = LLM_CALL_LINE.search(line)
                if not m:
                    continue
                model = m.group(1).decode("utf-8", "replace") or "?"
                r = out.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                r["calls"] += 1
                r["input_tokens"] += int(m.group(2))
                r["output_tokens"] += int(m.group(3))
    except OSError:
        pass
    return out


def usage_from_journal(run_dir: Path) -> dict[str, dict[str, int]]:
    """sbmig.log에 호출 줄이 없을 때 journal.jsonl의 수정 루프 LLM 이벤트에서 모델별 사용량을 모은다.

    Fallback: the fix loop's `llm_script` / `llm_verify_guide` journal events (calls + tokens per event)."""
    out: dict[str, dict[str, int]] = {}
    p = safe_file(run_dir, "journal.jsonl")
    if p is None:
        return out
    try:
        with open(p, "rb") as fh:
            for line in fh:
                if b'"llm_' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("event") not in LLM_JOURNAL_EVENTS:
                    continue
                tok = ev.get("tokens") if isinstance(ev.get("tokens"), dict) else {}
                calls = _int(ev.get("calls"))
                if not (calls or tok):
                    continue
                model = str(ev.get("model") or "?")
                r = out.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                r["calls"] += calls
                r["input_tokens"] += _int(tok.get("input"))
                r["output_tokens"] += _int(tok.get("output"))
    except OSError:
        pass
    return out


def _result_word(exit_code: Any, rep: dict[str, Any] | None) -> str | None:
    # 결과 줄과 같은 낱말을 쓴다(exit 0 ok, 2 incomplete, 3 aborted)
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return _EXIT_RESULT.get(exit_code, f"exit {exit_code}")
    if rep is None:
        return None
    flags = [f for f in rep.get("flags") or [] if isinstance(f, str)]
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return "aborted"
    if rep.get("success") is True and not rep.get("blocked"):
        return "ok"
    return "incomplete"


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


def has_logs(run_dir: Path) -> bool:
    """실행 폴더에 ZIP에 넣을 로그 파일(sbmig.log, journal.jsonl, remediation.log)이 하나라도 있는지 확인한다."""
    return any(safe_file(run_dir, m) is not None for m in LOG_MEMBERS if m != "control/request.json")


def run_usage(run_dir: Path, status: dict[str, Any] | None = None, live: bool = False) -> dict[str, Any] | None:
    """실행 폴더 하나의 사용량 행을 만든다. 실행 폴더가 아니면 None을 반환한다.
    ``status``는 ``RunManager.status``의 결과이며 상태 낱말과 종료 코드를 여기서 가져온다.

    One run's row of the usage answer, None for a folder that is not a run. `status` = `RunManager.status(run_id)`
    (state word, exit code, upload); `live` = the manager holds this run in memory (an in-process run has no
    request.json)."""
    rid = run_dir.name
    req, req_bad = _read_json(run_dir, "control/request.json")
    rep, rep_bad = _read_json(run_dir, "report.json")
    web, _ = _read_json(run_dir, "web-status.json")
    if req is None and rep is None and not req_bad and not rep_bad and web is None and not live:
        return None
    st = status or {}
    upload = _str((req or {}).get("upload_name")) or _str((req or {}).get("upload")) or _str(st.get("upload")) \
        or _str((web or {}).get("upload"))
    project = project_of(upload, _str((rep or {}).get("project_name")), rid)
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
    if rep is not None:
        usage, by_model, cost, currency = _usage_from_report(rep)
        known = True
    else:
        found = usage_from_log(run_dir) or usage_from_journal(run_dir)
        by_model = [{"model": m, **u, "total_tokens": u["input_tokens"] + u["output_tokens"], "cost": None}
                    for m, u in sorted(found.items())]
        usage = {k: sum(r[k] for r in by_model) for k in ("calls", "input_tokens", "output_tokens")}
        known = bool(found)
    if known and cost is None and not any(usage.values()):
        cost = 0.0                                   # 호출이 없었으면 가격이 없어도 비용은 0이다
    state = _str(st.get("state")) or ("finished" if rep is not None or exit_code is not None else "unknown")
    return {"run_id": rid, "project": project, "accepted_at": accepted.isoformat(timespec="seconds"),
            "_t": accepted, "_currency": currency,
            "flow": flow, "scope": _scope_of(rep), "status": state, "result": _result_word(exit_code, rep),
            **usage, "total_tokens": usage["input_tokens"] + usage["output_tokens"], "cost": cost,
            "by_model": sorted(by_model, key=lambda r: (-r["total_tokens"], r["model"])),
            "usage_known": known, "partial": bool(req_bad or rep_bad), "has_logs": has_logs(run_dir)}


def _sum_cost(costs: Iterable[float | None]) -> float | None:
    # 하나라도 가격이 없으면 합계를 내지 않는다(일부만 더한 합계는 오해를 부른다)
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
    """출력 폴더의 모든 실행을 읽어 프로젝트별 사용량을 계약 형태로 반환한다.
    읽지 못한 파일은 요청을 실패시키지 않고 그 실행에 ``partial``을 표시한다.

    The `GET /admin/api/usage` answer, read from disk now. `status_of(run_id)` = `RunManager.status` (optional);
    `live_ids` = runs the manager holds in memory."""
    out_root = Path(out_root)
    live = set(live_ids)
    runs: list[dict[str, Any]] = []
    for rd in iter_run_dirs(out_root):
        st: dict[str, Any] | None = None
        if status_of is not None:
            try:
                st = status_of(rd.name)
            except Exception:  # 상태를 못 읽어도 사용량은 보여 준다
                log.debug("admin usage: status of %s failed", rd.name, exc_info=True)
        try:
            row = run_usage(rd, st, rd.name in live)
        except Exception:  # 실행 하나 때문에 페이지 전체가 실패하지 않게 한다
            log.warning("admin usage: run folder %s could not be read", rd.name, exc_info=True)
            row = {"run_id": rd.name, "project": project_of(None, None, rd.name), "accepted_at": None,
                   "_t": datetime.fromtimestamp(0, timezone.utc), "_currency": None, "flow": "full", "scope": None,
                   "status": "unknown", "result": None, "calls": 0, "input_tokens": 0, "output_tokens": 0,
                   "total_tokens": 0, "cost": None, "by_model": [], "usage_known": False, "partial": True,
                   "has_logs": False}
        if row is not None:
            runs.append(row)

    zero = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    projects: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    model_costs: dict[str, list[float | None]] = {}
    totals = {"projects": 0, "runs": len(runs), **zero}
    for r in runs:
        p = projects.setdefault(r["project"], {"project": r["project"], "runs": 0, **zero, "_costs": [],
                                               "_times": [], "run_list": []})
        p["runs"] += 1
        _add(p, r)
        _add(totals, r)
        p["_costs"].append(r["cost"])
        p["_times"].append(r["_t"])
        p["run_list"].append(r)
        for m in r["by_model"]:
            agg = models.setdefault(m["model"], {"model": m["model"], **zero})
            _add(agg, m)
            model_costs.setdefault(m["model"], []).append(m["cost"])
    currencies = {r["_currency"] for r in runs if r["_currency"]}
    total_cost = _sum_cost(r["cost"] for r in runs) if runs else None
    if len(currencies) > 1:                          # 통화가 섞이면 더하지 않는다
        total_cost = None
    # 가격이 매겨진 실행이 모두 같은 통화면 합계가 없어도 통화를 알려 준다(행마다 비용을 보이기 위해)
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
        project_rows.append({k: p[k] for k in ("project", "runs", "calls", "input_tokens", "output_tokens",
                                                "total_tokens", "cost", "unpriced_runs", "first_run_at",
                                                "last_run_at", "run_list")})
    project_rows.sort(key=lambda p: (-p["total_tokens"], p["project"]))
    totals["projects"] = len(project_rows)
    by_model = []
    for name, m in models.items():
        by_model.append({**m, "cost": _sum_cost(model_costs[name]) if len(currencies) <= 1 else None})
    by_model.sort(key=lambda m: (-m["total_tokens"], m["model"]))
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "out_root": str(out_root),
            "totals": {**totals, "cost": total_cost, "currency": currency,
                       "unpriced_runs": sum(1 for r in runs if r["cost"] is None)},
            "by_model": by_model,
            "projects": project_rows}


# ---------------------------------------------------------------------------
# 로그 ZIP
# ---------------------------------------------------------------------------
def run_log_files(run_dir: Path, final_pass_pending: bool = False) -> list[tuple[str, Path]]:
    """실행 하나의 ZIP 항목 (ZIP 안 경로, 파일)을 반환한다. 조치를 아직 쓰는 곳이 있으면 remediation.log는 넣지 않는다.

    The (archive name, file) pairs of one run. Why each member is safe to ship: `sbmig.log` and `journal.jsonl` are
    redacted line by line as they are written when `SBMIG_REDACT_OUTPUT=1` (the Job modes; the same rule as the run's
    file route, `LIVE_FILES`); `control/request.json` is written once, atomically, by Main at intake and holds only
    names, digests and the sbmig version (no secret). `remediation/remediation.log` is redacted only by the final pass,
    so `final_pass_pending` (the migration run before its final pass, or an active remediation resume) leaves it out."""
    out = []
    for rel in LOG_MEMBERS:
        if final_pass_pending and rel in FINAL_PASS_MEMBERS:
            continue
        p = safe_file(run_dir, rel)
        if p is not None:
            out.append((f"{run_dir.name}/{rel}", p))
    return out


def zip_tmp_dir() -> Path:
    """ZIP 임시 파일을 둘 폴더를 반환한다. 파드의 로컬 임시 폴더이며 공유 볼륨(/data)이 아니다.

    The pod's local temp dir (`tempfile.gettempdir()`), never the shared output volume: an archive must not be able to
    fill the volume the runs and Maven's repository live on. There is no fallback to the output root."""
    return Path(tempfile.gettempdir())


def remove_legacy_tmp(out_root: Path) -> int:
    """예전 임시 폴더 ``<out root>/_admin``에 남은 ZIP 임시 파일을 지우고 지운 개수를 반환한다.
    이름이 ZIP 임시 파일 형식인 일반 파일만 지우고, 폴더는 비었을 때만 지운다.

    Once at startup: the archive temp files an earlier version left in `<out root>/_admin/` (names matching
    `ZIP_TMP_NAME`, regular files, never through a symlink); nothing else is touched; the folder itself goes only when it
    is empty afterwards."""
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
    """ZIP에 넣을 항목과 크기를 미리 정리한 계획. 만들기 전에 크기 한도와 남은 공간을 이것으로 확인한다.

    The members of one archive with the size each had when planned (the bound of its copy), the files skipped for
    `ZIP_FILE_LIMIT`, the uncompressed total and the number of runs with a member."""

    def __init__(self) -> None:
        self.members: list[tuple[str, Path, int]] = []
        self.skipped: list[tuple[str, int]] = []
        self.total = 0
        self.runs = 0

    def worst_case(self) -> int:
        """압축이 전혀 안 될 때의 ZIP 크기 추정값을 반환한다.

        Upper estimate of the archive: the members stored plus deflate's block overhead, headers and the extra members."""
        return self.total + self.total // 1000 + 1024 * (len(self.members) + 2) + 64 * 1024


def plan_logs_zip(run_dirs: Iterable[Path], withheld: Callable[[str], bool] | None = None) -> ZipPlan:
    """실행 폴더들에서 ZIP에 넣을 파일과 각 크기를 모은다. ``ZIP_FILE_LIMIT``보다 큰 파일은 건너뛴 목록에 둔다.

    `withheld(run_id)` = something may still write that run's remediation (its log is then left out). A run without a log
    file (only `control/request.json`) is left out."""
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


def check_logs_zip(plan: ZipPlan, tmp_dir: Path | None = None) -> tuple[int, dict[str, Any]] | None:
    """계획대로 ZIP을 만들어도 되는지 확인한다. 안 되면 (HTTP 상태, 오류 본문)을, 되면 None을 반환한다.

    413 `too_large` when the uncompressed total is over `ZIP_TOTAL_LIMIT`; 507 `no_space` when the temp dir's free space
    minus the worst-case archive would fall below `ZIP_FREE_FLOOR` (`needed_bytes` = worst case + floor)."""
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
    # 계획할 때의 크기까지만 읽는다. 그 사이 늘어난 줄은 넣지 않는다(계속 쓰이는 로그가 ZIP을 키우지 못한다)
    with open(path, "rb") as src:
        limit = min(limit, os.fstat(src.fileno()).st_size)
        info = zipfile.ZipInfo.from_file(path, arc)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.file_size = limit                        # zip64 판단에 쓰고 실제 값은 zipfile이 다시 적는다
        with zf.open(info, "w") as dst:
            left = limit
            while left > 0:
                chunk = src.read(min(ZIP_CHUNK, left))
                if not chunk:
                    break                             # 그 사이 줄어든 파일은 있는 만큼만 넣는다
                dst.write(chunk)
                left -= len(chunk)


def write_logs_zip(plan: ZipPlan, tmp_dir: Path | None = None) -> Path:
    """계획대로 deflate ZIP 임시 파일을 만들고 그 경로를 반환한다. 파일을 지우는 것은 호출한 쪽이다.

    Each member is copied in chunks up to its planned size; `SKIPPED.txt` lists the files over `ZIP_FILE_LIMIT`;
    `README.txt` alone when nothing was taken."""
    tmp_dir = tmp_dir or zip_tmp_dir()
    fd, name = tempfile.mkstemp(prefix=ZIP_TMP_PREFIX, suffix=ZIP_TMP_SUFFIX, dir=str(tmp_dir))
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED,
                                                         allowZip64=True) as zf:
            count = 0
            for arc, p, size in plan.members:
                try:
                    _copy_bounded(zf, arc, p, size)
                    count += 1
                except OSError as e:
                    log.warning("admin logs zip: %s skipped: %s", arc, e)
            if plan.skipped:
                lines = [(f"Files larger than {ZIP_FILE_LIMIT} bytes are left out of this archive; "
                          "download them from the server directly."), ""]
                lines += [f"{arc}\t{size}" for arc, size in plan.skipped]
                zf.writestr(SKIPPED_NAME, "\n".join(lines) + "\n")
            if count == 0 and not plan.skipped:
                zf.writestr("README.txt", EMPTY_README)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


class ZipRefused(Exception):
    """ZIP을 만들지 않기로 했을 때 던지는 예외. HTTP 상태와 오류 본문을 담는다."""

    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(body.get("error"))
        self.status = status
        self.body = body


def build_logs_zip(run_dirs: Iterable[Path], withheld: Callable[[str], bool] | None = None,
                   tmp_dir: Path | None = None) -> Path:
    """계획, 확인, 만들기를 한 번에 하고 ZIP 임시 파일 경로를 반환한다. 한도에 걸리면 ``ZipRefused``를 던진다.

    `plan_logs_zip` + `check_logs_zip` + `write_logs_zip`; the caller holds `ZIP_GATE` and deletes the file."""
    plan = plan_logs_zip(run_dirs, withheld)
    refusal = check_logs_zip(plan, tmp_dir)
    if refusal is not None:
        raise ZipRefused(*refusal)
    return write_logs_zip(plan, tmp_dir)


class TempFileResponse(StreamingResponse):
    """임시 파일을 조각으로 내보내고, 응답이 끝나거나 연결이 끊겨도 파일을 지우는 응답 클래스.
    ``on_close``가 있으면 파일을 지운 뒤 한 번 호출한다.

    Streams a temp file and deletes it on every path (finished, client gone, error), then calls `on_close` once."""

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


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
def remediation_rule(remediations: Any) -> Callable[[str], bool]:
    """remediation.log를 ZIP에서 뺄지 정하는 함수를 반환한다. 조치 API가 쓰는 규칙(``RemediationManager._withheld``)을 그대로 쓴다.
    규칙을 찾지 못하거나 확인에 실패하면 빼는 쪽을 고른다.

    The remediation API's own rule object: withheld while the migration run's final pass is still to come OR a
    remediation resume of the run is queued / creating / running (in memory, or recovered from
    `control/remediation-resume.json` after a web-pod restart). No rule reachable, or an error: withhold."""
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
    ``remediations``가 없으면 요청마다 ``app.state.remediations``를 쓴다. 만들 때 예전 임시 폴더를 한 번 정리한다.

    The `/admin/api/*` routes over `manager` (a `RunManager`: `out_dir`, `status`, `runs`) and the remediation service
    (`RemediationManager`, for the remediation.log rule; default `request.app.state.remediations`). Removes the
    leftovers of `<out root>/_admin/` once."""
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

    async def logs_zip(request: Request, dirs: list[Path], filename: str, check_only: bool):
        nostore = {"Cache-Control": "no-store"}
        if not ZIP_GATE.acquire(blocking=False):
            return JSONResponse({"error": "busy"}, status_code=409, headers=nostore)
        release = True
        try:
            tmp = zip_tmp_dir()
            plan = await run_in_threadpool(plan_logs_zip, dirs, withheld_for(request))
            refusal = await run_in_threadpool(check_logs_zip, plan, tmp)
            if refusal is not None:
                log.warning("admin logs zip refused: %s", refusal[1])
                return JSONResponse(refusal[1], status_code=refusal[0], headers=nostore)
            if check_only:
                return JSONResponse({"ok": True, "bytes": plan.total, "runs": plan.runs,
                                     "skipped": len(plan.skipped)}, headers=nostore)
            path = await run_in_threadpool(write_logs_zip, plan, tmp)
            resp = TempFileResponse(path, filename, on_close=ZIP_GATE.release)
            release = False                           # 응답이 파일을 지운 뒤 놓는다
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
        return await logs_zip(request, [hit], f"sbmig-logs-{hit.name}.zip", check_only)

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
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

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
        return await one_run(request, run_id, True)

    return router
