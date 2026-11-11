"""IVAS(LabradorLabs 취약점 점검) HTTP 클라이언트와 검사 절차(업로드 → 상태 조회 → 요약 → 모든 페이지 → 완전성 판정).
회사가 알려 준 HTTP 계약만 쓴다: ``POST /session`` 로그인 → ``X-API-KEY`` 헤더, ``POST /projects/SELF/sourcecode`` 로
ZIP 업로드(multipart: ``analysisRequest`` + 파일), ``…/analysis/{id}/status`` 상태, ``…/result/summary`` 요약,
``…/result/<종류>?page=&size=`` 상세 페이지. 그 밖의 것은 모른다 — 모르는 것은 추측하지 않고 "미완료"로 남긴다.

지키는 규칙:
- **업로드는 절대 자동으로 다시 하지 않는다.** 요청 본문이 나갔을 수 있는 뒤의 모든 애매함(시간 초과, 연결 끊김, 5xx,
  ID 없는 2xx)은 ``IvasUploadUncertain`` — 작업은 사람이 IVAS 에서 확인할 때까지 멈춘다(같은 ZIP 이 두 번 분석되는 것보다
  멈추는 편이 낫다). 보내기 전에 실패한 것이 분명할 때(로그인 실패, 파일을 못 읽음, 접속 자체 실패)와 분명한 4xx 거절만 ``IvasError``.
- 토큰은 메모리에만 두고, 토큰·비밀번호는 로그·예외 문장·repr 어디에도 나오지 않는다(응답 본문을 인용할 때도 지우고 300자로 자른다).
- TLS 검증은 끄지 않는다(``verify=False`` 없음; ``REQUESTS_CA_BUNDLE`` 은 requests 가 그대로 따른다).
- 검사 결과는 빠진 페이지·바뀐 전체 개수·중복 항목·요약과 맞지 않는 개수·지원하지 않는 범주(SW)·HTTP 오류가 하나라도 있으면
  ``complete=False`` — 그때 ``findings`` 가 비어 있어도 "0건"이 아니다.
- 조치 구분 코드(``action.actionDivCd``)의 뜻은 ``ACTION_MEANINGS`` 한 곳에 있다. 검사 파일에는 모든 항목을 그대로 두고, 조치
  대상에서 뺄 코드(``remediation.ignore_actions``)는 그래프가 ``split_findings`` 로 가른다. 빈 코드·모르는 코드는 조치 대상이다.
- 파일 컴포넌트 ID 에는 ``|`` 가 들어 있을 수 있다(``11560|934982``): matchedfiles 경로에 경로 조각 하나로 인코딩해 보낸다.

IVAS HTTP client and scan procedure. Only the HTTP contract the company gave is used; anything unknown makes the
scan incomplete instead of being guessed. An upload is never retried automatically: any ambiguity after the body may
have left raises `IvasUploadUncertain`. Tokens / passwords never reach a log line, an exception message or a repr.
TLS verification is never disabled. Action-code meanings live in `ACTION_MEANINGS`; the graph splits the findings by
`remediation.ignore_actions` (`split_findings`), an empty / unknown code stays actionable.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from .interfaces import Journal, Page, StopCheck
from .models import Finding, IvasError, IvasUploadUncertain, ScanResult, UploadReceipt

log = logging.getLogger("sbmig.remediations.ivas")

ENV_BASE_URL = "IVAS_BASE_URL"
ENV_USER_ID = "IVAS_USER_ID"
ENV_PASSWORD = "IVAS_PASSWORD"

# 분석 상태 코드(anlStatusDivCd) — 회사가 알려 준 값
IN_PROGRESS = ("PREPARING", "WAITING", "ANALYSIS")
DONE = "DONE"
FAILED_STATES = ("FAILED", "ERROR", "CANCELED", "CANCELLED")

# 요약의 범주 8개(summary.resultCount.vulnerabilities.<범주>.total)
SUMMARY_CATEGORIES = ("components", "swComponents", "fileComponents", "codeSnippets", "file", "functionComponents",
                      "ospkgComponents", "binaryComponents")
# 분석 ID 의 필드 이름 — 회사 문서에 lbrdAnlId 와 lbrdrAnlId 가 둘 다 적혀 있어 셋 다 받는다
ANALYSIS_ID_KEYS = ("lbrdAnlId", "lbrdrAnlId", "analysisId")

# 조치 구분 코드(action.actionDivCd) → 뜻 — 회사 IVAS 문서의 표. 코드의 뜻은 이 표 한 곳에만 둔다(보고서도 여기서 읽는다).
# 어떤 코드를 조치 대상에서 뺄지는 설정(``remediation.ignore_actions``)이 정한다 — 이 표는 뜻만 말한다
ACTION_MEANINGS: dict[str, str] = {
    "NE": "조치 필요",
    "NP": "조치계획 미등록",
    "PL": "조치예정",
    "OT": "조치일 경과",
    "PC": "패치 완료",
    "IG": "조치 예외",
}
DEFAULT_IGNORE_ACTIONS = ("PC", "IG")

# 결과 종류 → URL 꼬리(analysis/{id}/ 뒤)
PAGE_PATHS = {
    "library": ("result", "libraryvulnerabilities"),
    "sw": ("result", "swvulnerabilities"),
    "file_main": ("result", "filecomponentvulnerabilities", "main"),
    "file_potential": ("result", "filecomponentvulnerabilities", "potential"),
}

GET_TRIES = 3                  # GET 한 번의 최대 시도 횟수(연결 오류·시간 초과·5xx·429)
BACKOFF_SECONDS = (1.0, 2.0)   # 시도 사이 대기
BODY_QUOTE_CHARS = 300         # 오류 문장에 인용하는 응답 본문 길이
STATUS_ERROR_LIMIT = 3         # 상태 조회가 연달아 이만큼 실패하면 검사 미완료(http_error)
_MASK = "***"


# ---------------------------------------------------------------------------- 설정 / settings
@dataclass
class IvasSettings:
    """IVAS 접속 설정. 주소·아이디·비밀번호는 환경 변수(Secret)에서만, 나머지는 ``cfg.remediation.ivas_*`` 에서 온다.
    비밀번호는 repr 에 나오지 않는다.

    IVAS connection settings; the password is excluded from the repr."""
    base_url: str = ""
    user_id: str = ""
    password: str = field(default="", repr=False)
    timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 1800.0
    poll_seconds: float = 15.0
    analysis_timeout_seconds: float = 120 * 60.0
    page_size: int = 100
    project_prefix: str = "sbmig"


def ivas_settings(cfg: Any, env: Mapping[str, str] = os.environ) -> IvasSettings:
    """설정 객체와 환경 변수로 ``IvasSettings`` 를 만든다. 비밀 값은 환경 변수에서만 읽는다(설정 파일에 두지 않는다).

    Build `IvasSettings`: secrets from the environment only, the rest from `cfg.remediation`."""
    rem = getattr(cfg, "remediation", None)
    if rem is None:
        from sbmig.core.config import RemediationConfig
        rem = RemediationConfig()
    return IvasSettings(
        base_url=(env.get(ENV_BASE_URL) or "").strip().rstrip("/"),
        user_id=(env.get(ENV_USER_ID) or "").strip(),
        password=env.get(ENV_PASSWORD) or "",
        timeout_seconds=float(rem.ivas_timeout_seconds),
        upload_timeout_seconds=float(rem.ivas_upload_timeout_seconds),
        poll_seconds=float(rem.ivas_poll_seconds),
        analysis_timeout_seconds=float(rem.ivas_analysis_timeout_minutes) * 60.0,
        page_size=int(rem.ivas_page_size),
        project_prefix=str(rem.ivas_project_prefix),
    )


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def readiness(settings: IvasSettings) -> list[str]:
    """IVAS 를 쓸 수 없게 하는 문제들(빈 목록 = 준비됨). 이름만 말하고 값은 절대 말하지 않는다. 주소는 https 여야 한다
    (loopback 만 http 허용 — 시험용).

    Problems that prevent IVAS use (names only, never values); the base URL must be https unless loopback."""
    problems: list[str] = []
    if not settings.base_url:
        problems.append(f"{ENV_BASE_URL} is not set")
    else:
        try:
            parts = urlsplit(settings.base_url)
            host = parts.hostname or ""
        except ValueError:
            parts, host = None, ""
        if parts is None or not host or parts.scheme not in ("https", "http"):
            problems.append(f"{ENV_BASE_URL} is not a valid http(s) URL")
        else:
            if parts.scheme != "https" and not _is_loopback(host):
                problems.append(f"{ENV_BASE_URL} must use https (plain http is allowed for loopback hosts only)")
            if parts.username or parts.password:
                problems.append(f"{ENV_BASE_URL} must not embed credentials (use {ENV_USER_ID} / {ENV_PASSWORD})")
            if parts.query or parts.fragment:
                problems.append(f"{ENV_BASE_URL} must not carry a query or fragment")
    if not settings.user_id:
        problems.append(f"{ENV_USER_ID} is not set")
    if not settings.password:
        problems.append(f"{ENV_PASSWORD} is not set")
    return problems


def project_name(settings: IvasSettings, remediation_id: str, label: str) -> str:
    """IVAS 프로젝트 이름 ``<앞머리>-<작업 ID>-<label>``.

    The IVAS project name `<prefix>-<id>-<label>`."""
    return f"{settings.project_prefix}-{remediation_id}-{label}"


# ---------------------------------------------------------------------------- multipart 본문 / streamed body
class _MultipartStream:
    """multipart 본문을 파일 전체를 메모리에 올리지 않고 조각으로 내보낸다(길이를 알려 Content-Length 로 보낸다).
    실제로 내보낸 파일 바이트의 sha256 을 함께 센다 — 영수증의 zip_sha256 은 '보낸 바이트'의 해시다."""

    def __init__(self, boundary: str, analysis_json: str, fh: Any, filename: str, size: int):
        safe_name = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
        self._pre = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="analysisRequest"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{analysis_json}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            "Content-Type: application/zip\r\n\r\n").encode()
        self._post = f"\r\n--{boundary}--\r\n".encode("ascii")
        self._fh = fh
        self._size = size
        self._pos = 0
        self.file_bytes = 0
        self._hash = hashlib.sha256()

    def __len__(self) -> int:
        return len(self._pre) + self._size + len(self._post)

    @property
    def bytes_read(self) -> int:
        return self._pos

    @property
    def file_sha256(self) -> str:
        return self._hash.hexdigest()

    def read(self, n: int = -1) -> bytes:
        total = len(self)
        if n is None or n < 0:
            n = total - self._pos
        out = bytearray()
        while n > 0 and self._pos < total:
            pre, body_end = len(self._pre), len(self._pre) + self._size
            if self._pos < pre:
                chunk = self._pre[self._pos:self._pos + n]
            elif self._pos < body_end:
                chunk = self._fh.read(min(n, body_end - self._pos))
                if not chunk:          # 파일이 줄어들었다: 길이가 맞지 않는 본문을 보내지 않는다
                    raise OSError("upload file shrank while it was being sent")
                self._hash.update(chunk)
                self.file_bytes += len(chunk)
            else:
                off = self._pos - body_end
                chunk = self._post[off:off + n]
            out += chunk
            self._pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def __iter__(self):
        while True:
            chunk = self.read(64 * 1024)
            if not chunk:
                return
            yield chunk


def _connect_failure(exc: BaseException) -> bool:
    """예외 사슬 안에 '접속 자체가 안 됨'(urllib3 NewConnectionError: 거부·이름 해석 실패)이 있는가. 모르면 False(= 불확실)."""
    try:
        from urllib3.exceptions import NewConnectionError
    except ImportError:          # pragma: no cover
        return False
    seen: set[int] = set()
    todo: list[Any] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, NewConnectionError):
            return True
        if isinstance(cur, BaseException):
            todo.extend(a for a in cur.args if isinstance(a, BaseException))
            todo.append(getattr(cur, "reason", None))
            todo.append(cur.__cause__)
            todo.append(cur.__context__)
    return False


def _scalar_id(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _analysis_id(data: Mapping[str, Any]) -> str:
    for k in ANALYSIS_ID_KEYS:
        v = _scalar_id(data.get(k))
        if v:
            return v
    return ""


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------- 조치 구분 / action codes
def normalize_action(code: Any) -> str:
    """조치 구분 코드를 비교용으로: 앞뒤 공백을 지우고 대문자로. 문자열이 아니면 빈 값.

    An action code for comparison: trimmed, upper-cased ('' for a non-string)."""
    return code.strip().upper() if isinstance(code, str) else ""


def action_meaning(code: Any) -> str:
    """코드의 한국어 뜻(표에 없으면 "알 수 없는 코드", 비었으면 "코드 없음").

    The Korean meaning of an action code."""
    norm = normalize_action(code)
    if not norm:
        return "코드 없음"
    return ACTION_MEANINGS.get(norm, "알 수 없는 코드")


def ignore_set(value: Any) -> frozenset[str]:
    """설정 값(``cfg`` / ``cfg.remediation`` / 코드 목록 / None)에서 조치 대상에서 뺄 코드 집합(정규화). None 이나 설정이 없는
    객체 = 기본값(PC, IG). 빈 문자열 항목은 버린다 — 빈 코드는 절대 '뺄 코드'가 되지 않는다.

    The normalized set of ignored action codes from a config object, a code list or None (= the default)."""
    if value is not None and not isinstance(value, (list, tuple, set, frozenset, str)):
        rem = getattr(value, "remediation", value)
        value = getattr(rem, "ignore_actions", None)
    if value is None:
        value = DEFAULT_IGNORE_ACTIONS
    if isinstance(value, str):
        value = [value]
    return frozenset(n for n in (normalize_action(x) for x in value) if n)


def is_ignored(finding: Finding, ignore: frozenset[str]) -> bool:
    """이 항목의 조치 코드가 '뺄 코드'인가. 빈 코드는 늘 조치 대상이다(조용히 빼지 않는다).

    True when the finding's action code is in the ignored set; an empty code never is."""
    norm = normalize_action(finding.action)
    return bool(norm) and norm in ignore


def split_findings(findings: list[Finding], ignore: frozenset[str]) -> tuple[list[Finding], list[Finding]]:
    """(조치 대상, 조치 대상에서 뺀 항목). 순서는 그대로.

    (actionable, ignored), order kept."""
    act: list[Finding] = []
    ign: list[Finding] = []
    for f in findings:
        (ign if is_ignored(f, ignore) else act).append(f)
    return act, ign


def unknown_actions(findings: list[Finding]) -> list[str]:
    """표에 없는 조치 코드(빈 코드는 "(empty)")들 — 정렬, 중복 없음. 이런 항목은 조치 대상으로 다룬다(설정에 그 코드를 직접
    적은 경우만 뺀다).

    Action codes outside the known table ('(empty)' for a missing one), sorted."""
    out = set()
    for f in findings:
        norm = normalize_action(f.action)
        if norm not in ACTION_MEANINGS:
            out.add(norm or "(empty)")
    return sorted(out)


# ---------------------------------------------------------------------------- HTTP 클라이언트 / client
class IvasHttpClient:
    """``interfaces.IvasClient`` 의 requests 구현. 로그인은 처음 필요할 때 하고, 토큰은 메모리에만 둔다. 이후 호출이
    401/403 이면 한 번 다시 로그인해 그 호출을 한 번 더 한다(업로드는 예외: 업로드 직전에 늘 새로 로그인하고, 업로드의 401 은
    분명한 거절). GET 은 연결 오류·시간 초과·5xx·429 에 최대 3번 시도한다. 업로드는 재시도하지 않는다.

    `interfaces.IvasClient` over requests: lazy login, in-memory token, one re-login on 401/403 (not for the upload,
    which always logs in fresh first), bounded GET retries, never an upload retry."""

    def __init__(self, settings: IvasSettings, session: Any = None, sleep: Callable[[float], None] = time.sleep):
        self._settings = settings
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._token: str | None = None

    def __repr__(self) -> str:
        return f"IvasHttpClient(base_url={self._settings.base_url!r}, logged_in={self._token is not None})"

    # ---- helpers -------------------------------------------------------
    def _scrub(self, text: Any) -> str:
        """비밀 값(토큰·비밀번호)을 지우고 길이를 자른다 — 오류 문장에 응답 본문을 인용할 때."""
        s = str(text or "")
        for secret in (self._token, self._settings.password):
            if secret:
                s = s.replace(secret, _MASK)
        s = " ".join(s.split())
        return s if len(s) <= BODY_QUOTE_CHARS else s[:BODY_QUOTE_CHARS] + "…"

    def _url(self, *segments: Any) -> str:
        return self._settings.base_url + "/" + "/".join(quote(str(s), safe="") for s in segments)

    def _body_text(self, r: Any) -> str:
        try:
            return self._scrub(getattr(r, "text", ""))
        except Exception:        # 본문을 못 읽어도 오류 문장은 만든다
            return ""

    def _backoff(self, attempt: int) -> None:
        self._sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

    # ---- login -----------------------------------------------------------
    def _login(self) -> None:
        """``POST /session`` → token. 일시적 오류(연결·시간 초과·5xx·429)는 최대 3번. 거절·읽을 수 없는 답은 IvasError."""
        url = self._url("session")
        payload = {"userId": self._settings.user_id, "password": self._settings.password}
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self._session.request("POST", url, json=payload, headers={"Accept": "application/json"},
                                          timeout=self._settings.timeout_seconds, allow_redirects=False)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= GET_TRIES:
                    raise IvasError(f"IVAS login failed: {type(exc).__name__} after {attempt} tries",
                                    code="login_failed") from None
                self._backoff(attempt)
                continue
            except requests.exceptions.RequestException as exc:
                # 잘못된 주소(스킴 없음 등)처럼 보내기 전에 실패한 것: 다시 해도 같다 — 분명한 실패
                raise IvasError(f"IVAS login failed: {type(exc).__name__}", code="login_failed") from None
            code = r.status_code
            if (code == 429 or code >= 500) and attempt < GET_TRIES:
                self._backoff(attempt)
                continue
            if not 200 <= code < 300:
                raise IvasError(f"IVAS login rejected: HTTP {code}: {self._body_text(r)}", code="login_failed",
                                http_status=code)
            try:
                data = r.json()
            except ValueError:
                raise IvasError(f"IVAS login answer is not JSON (HTTP {code})", code="login_failed") from None
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token.strip():
                raise IvasError(f"IVAS login answer has no token (HTTP {code})", code="login_failed")
            self._token = token
            try:                 # 출력 정제기가 걸려 있으면 이 토큰도 지우게 알린다
                from sbmig.core.scrub import note_secret
                note_secret(token)
            except Exception:    # pragma: no cover - 정제기가 없어도 클라이언트는 동작한다
                pass
            log.debug("ivas: logged in")
            return

    def _auth_headers(self) -> dict[str, str]:
        return {"X-API-KEY": self._token or "", "Accept": "application/json"}

    # ---- GET with retries ------------------------------------------------
    def _get_json(self, segments: tuple[Any, ...], params: dict[str, Any] | None = None) -> Any:
        url = self._url(*segments)
        where = "/" + "/".join(str(s) for s in segments)
        relogged = False
        attempt = 0
        while True:
            if self._token is None:
                self._login()
            try:
                r = self._session.request("GET", url, params=params, headers=self._auth_headers(),
                                          timeout=self._settings.timeout_seconds, allow_redirects=False)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                attempt += 1
                log.debug("ivas: GET %s -> %s (try %d)", where, type(exc).__name__, attempt)
                if attempt >= GET_TRIES:
                    raise IvasError(f"IVAS GET {where}: {type(exc).__name__} after {attempt} tries",
                                    code="http_error") from None
                self._backoff(attempt)
                continue
            except requests.exceptions.RequestException as exc:
                # 그 밖의 requests 오류(잘못된 주소, 너무 많은 redirect 등)도 IvasError — 검사는 미완료가 된다(0건이 아니다)
                raise IvasError(f"IVAS GET {where}: {type(exc).__name__}", code="http_error") from None
            code = r.status_code
            log.debug("ivas: GET %s -> HTTP %s", where, code)
            if code in (401, 403) and not relogged:
                # 토큰이 만료됐을 수 있다: 한 번만 다시 로그인하고 같은 호출을 한 번 더 한다
                relogged = True
                self._token = None
                continue
            if code == 429 or code >= 500:
                attempt += 1
                if attempt >= GET_TRIES:
                    raise IvasError(f"IVAS GET {where}: HTTP {code} after {attempt} tries: {self._body_text(r)}",
                                    code="http_error", http_status=code)
                self._backoff(attempt)
                continue
            if not 200 <= code < 300:
                raise IvasError(f"IVAS GET {where}: HTTP {code}: {self._body_text(r)}", code="http_error",
                                http_status=code)
            try:
                return r.json()
            except ValueError:
                raise IvasError(f"IVAS GET {where}: answer is not JSON: {self._body_text(r)}",
                                code="http_error") from None

    # ---- IvasClient --------------------------------------------------------
    def upload(self, zip_path: Path, project_name: str) -> UploadReceipt:
        """ZIP 을 올린다(재시도 없음). 보내기 전 실패·분명한 4xx 거절은 ``IvasError``, 본문이 나갔을 수 있는 뒤의 모든 애매함은
        ``IvasUploadUncertain``. 업로드 직전에 늘 새로 로그인한다(캐시한 토큰이 업로드 도중 만료되어 결과가 애매해지지 않게).

        Upload the ZIP once; `IvasUploadUncertain` for every ambiguity after the body may have been sent."""
        zip_path = Path(zip_path)
        try:
            size = zip_path.stat().st_size
            fh = open(zip_path, "rb")
        except OSError as exc:
            raise IvasError(f"IVAS upload not sent: cannot read the ZIP ({type(exc).__name__})",
                            code="upload_not_sent") from None
        with fh:
            self._token = None
            self._login()       # 실패하면 IvasError — 아직 아무것도 보내지 않았다
            analysis_json = json.dumps({"projectNm": project_name, "source": {"type": "ZIP_FILE"}})
            boundary = "sbmig-" + uuid.uuid4().hex
            body = _MultipartStream(boundary, analysis_json, fh, zip_path.name, size)
            headers = dict(self._auth_headers())
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            headers["Content-Length"] = str(len(body))
            url = self._url("projects", "SELF", "sourcecode")
            details = {"project_name": project_name}
            log.info("ivas: uploading %s (%d bytes) as project %s", zip_path.name, size, project_name)
            try:
                r = self._session.request("POST", url, data=body, headers=headers,
                                          timeout=(self._settings.timeout_seconds, self._settings.upload_timeout_seconds),
                                          allow_redirects=False)
            except requests.exceptions.ConnectTimeout:
                if body.bytes_read == 0:
                    raise IvasError("IVAS upload not sent: connect timeout", code="upload_not_sent", **details) from None
                raise IvasUploadUncertain("IVAS upload outcome unknown: connect timeout after body bytes were read",
                                          **details) from None
            except requests.exceptions.ConnectionError as exc:
                if body.bytes_read == 0 and _connect_failure(exc):
                    raise IvasError("IVAS upload not sent: connection could not be opened", code="upload_not_sent",
                                    **details) from None
                raise IvasUploadUncertain(f"IVAS upload outcome unknown: {type(exc).__name__} while sending",
                                          **details) from None
            except Exception as exc:   # 시간 초과·끊김·파일 읽기 오류 등: 본문 일부가 나갔을 수 있다
                raise IvasUploadUncertain(f"IVAS upload outcome unknown: {type(exc).__name__} while sending",
                                          **details) from None
        code = r.status_code
        log.info("ivas: upload -> HTTP %s", code)
        if 400 <= code < 500:
            raise IvasError(f"IVAS upload rejected: HTTP {code}: {self._body_text(r)}", code="upload_rejected",
                            http_status=code, **details)
        if not 200 <= code < 300:
            raise IvasUploadUncertain(f"IVAS upload outcome unknown: HTTP {code}: {self._body_text(r)}",
                                      http_status=code, **details)
        try:
            data = r.json()
        except ValueError:
            raise IvasUploadUncertain(f"IVAS upload outcome unknown: HTTP {code} answer is not JSON: "
                                      f"{self._body_text(r)}", http_status=code, **details) from None
        if not isinstance(data, dict):
            raise IvasUploadUncertain(f"IVAS upload outcome unknown: HTTP {code} answer is not an object",
                                      http_status=code, **details)
        project_id, analysis_id = _scalar_id(data.get("projectId")), _analysis_id(data)
        if not project_id or not analysis_id:
            raise IvasUploadUncertain(f"IVAS upload outcome unknown: HTTP {code} answer lacks projectId / analysis id: "
                                      f"{self._body_text(r)}", http_status=code, **details)
        # 영수증의 sha256 은 실제로 보낸 바이트의 해시 — 끝까지 다 보내지 못했으면 빈 값(검사 쪽이 zip_hash_mismatch 로 본다)
        sent_sha = body.file_sha256 if body.file_bytes == size else ""
        return UploadReceipt(project_id=project_id, analysis_id=analysis_id, zip_sha256=sent_sha, project_name=project_name)

    def status(self, project_id: str, analysis_id: str) -> str:
        """분석 상태 코드 ``anlStatusDivCd`` 그대로. 응답의 ID 가 요청과 다르면 ``IvasError(code="id_mismatch")``.

        The raw `anlStatusDivCd`; differing ids in the answer → `IvasError(code="id_mismatch")`."""
        data = self._get_json(("projects", "SELF", project_id, "analysis", analysis_id, "status"))
        if not isinstance(data, dict):
            raise IvasError("IVAS status answer is not an object", code="http_error")
        got_project = _scalar_id(data.get("projectId"))
        if got_project and got_project != str(project_id):
            raise IvasError(f"IVAS status answer is for project {got_project}, asked {project_id}", code="id_mismatch")
        for k in ANALYSIS_ID_KEYS:
            got = _scalar_id(data.get(k))
            if got and got != str(analysis_id):
                raise IvasError(f"IVAS status answer is for analysis {got} ({k}), asked {analysis_id}",
                                code="id_mismatch")
        code = data.get("anlStatusDivCd")
        if not isinstance(code, str) or not code.strip():
            raise IvasError("IVAS status answer has no anlStatusDivCd", code="http_error")
        return code.strip()

    def summary(self, project_id: str, analysis_id: str) -> dict[str, int]:
        """요약의 범주별 total 8개. 하나라도 읽을 수 없으면 ``IvasError`` (읽지 못한 요약은 0 이 아니다).

        The eight category totals; any unreadable one raises `IvasError`."""
        data = self._get_json(("projects", "SELF", project_id, "analysis", analysis_id, "result", "summary"))
        try:
            vulns = data["summary"]["resultCount"]["vulnerabilities"]
        except (KeyError, TypeError):
            raise IvasError("IVAS summary answer lacks summary.resultCount.vulnerabilities",
                            code="summary_unreadable") from None
        out: dict[str, int] = {}
        for cat in SUMMARY_CATEGORIES:
            entry = vulns.get(cat) if isinstance(vulns, dict) else None
            total = entry.get("total") if isinstance(entry, dict) else None
            if not _is_int(total):
                raise IvasError(f"IVAS summary has no integer total for {cat}", code="summary_unreadable")
            out[cat] = total
        return out

    def page(self, project_id: str, analysis_id: str, kind: str, page: int, size: int,
             component_id: str | None = None) -> Page:
        """결과 한 쪽(page 는 0 부터). ``content`` 목록과 정수 ``totalElements`` 가 없으면 ``IvasError``.

        One result page (0-based); a missing `content` list or integer `totalElements` raises `IvasError`."""
        base = ("projects", "SELF", project_id, "analysis", analysis_id)
        if kind == "matched_files":
            if not component_id:
                raise IvasError("matched_files needs a component id", code="http_error")
            segments = base + ("result", "filecomponent", component_id, "matchedfiles")
        elif kind in PAGE_PATHS:
            segments = base + PAGE_PATHS[kind]
        else:
            raise IvasError(f"unknown IVAS result kind {kind!r}", code="http_error")
        data = self._get_json(segments, params={"page": int(page), "size": int(size)})
        if not isinstance(data, dict) or not isinstance(data.get("content"), list) or not _is_int(data.get("totalElements")):
            raise IvasError(f"IVAS {kind} page {page} lacks a content list or an integer totalElements", code="http_error")
        return Page(content=list(data["content"]), total=int(data["totalElements"]))


# ---------------------------------------------------------------------------- 검사 절차 / scan
class _Collector:
    """검사 하나의 미완료 이유·메모를 순서대로, 중복 없이 모은다."""

    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.notes: list[str] = []

    def bad(self, reason: str, note: str = "") -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        if note:
            self.note(note)

    def note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)


def _all_pages(client: Any, receipt: UploadReceipt, kind: str, size: int, col: _Collector,
               component_id: str | None = None) -> tuple[list[Any], int | None, int]:
    """한 종류의 결과를 끝까지 읽는다 → (항목들, 첫 쪽의 totalElements, 읽은 쪽 수). 빠진 쪽·바뀐 전체 개수·개수 불일치는
    ``col`` 에 이유로 남긴다. 무한 반복을 막는 쪽 수 상한(전체/크기 + 2)이 있다."""
    what = kind if component_id is None else f"{kind}({component_id})"
    items: list[Any] = []
    total: int | None = None
    page_no = 0
    cap = 2
    failed = False
    while True:
        try:
            pg = client.page(receipt.project_id, receipt.analysis_id, kind, page_no, size, component_id=component_id)
        except IvasError as exc:
            col.bad("http_error")
            col.bad("page_missing", f"{what}: page {page_no} could not be read ({exc.code}: {exc})")
            failed = True
            break
        if total is None:
            total = pg.total
            cap = max(1, -(-total // max(size, 1))) + 2
        elif pg.total != total:
            col.bad("total_mismatch", f"{what}: totalElements changed from {total} to {pg.total} at page {page_no}")
        page_no += 1
        if not pg.content:
            if len(items) < total:
                col.bad("page_missing", f"{what}: empty page {page_no - 1} after {len(items)} of {total} items")
                failed = True
            break
        items.extend(pg.content)
        if len(items) >= total:
            break
        if page_no >= cap:
            col.bad("page_missing", f"{what}: stopped after {page_no} pages (safety cap) with {len(items)} of {total} items")
            failed = True
            break
    if total is not None and not failed:
        if len(items) > total:
            col.bad("total_mismatch", f"{what}: {len(items)} items returned for totalElements {total}")
        elif len(items) < total:
            col.bad("page_missing", f"{what}: {len(items)} of {total} items returned")
    return items, total, page_no


def _max_score(item: Mapping[str, Any]) -> float | None:
    scores = []
    rating = item.get("rating")
    if isinstance(rating, list):
        for r in rating:
            s = r.get("score") if isinstance(r, dict) else None
            if isinstance(s, (int, float)) and not isinstance(s, bool):
                scores.append(float(s))
    return max(scores) if scores else None


def _action(item: Mapping[str, Any]) -> str:
    a = item.get("action")
    v = a.get("actionDivCd") if isinstance(a, dict) else None
    return str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ""


def _text(d: Any, key: str) -> str:
    v = d.get(key) if isinstance(d, dict) else None
    return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _library_finding(item: Any) -> Finding | None:
    if not isinstance(item, dict) or not _scalar_id(item.get("id")):
        return None
    lib = item.get("library") if isinstance(item.get("library"), dict) else {}
    info = lib.get("info") if isinstance(lib.get("info"), dict) else {}
    locations = []
    for loc in item.get("detectedLocations") or []:
        p = _text(loc, "filePath")
        if p and p not in locations:
            locations.append(p)
    return Finding(id=_scalar_id(item["id"]), kind="library", identifier=_text(lib, "identifier"),
                   version=_text(lib, "version"), patched_version=_text(info, "patchedVersion"),
                   locations=locations, action=_action(item), score=_max_score(item))


def _file_component_id(item: Mapping[str, Any]) -> tuple[str, str]:
    """파일 컴포넌트 ID 와 그 출처 필드. ``fileComponent.id`` 가 늘 먼저다(지금까지의 규칙). 그것이 없을 때만
    ``fileComponent.ossId`` → 최상위 ``ossId`` 를 쓴다 — 회사는 matchedfiles 경로의 ID 를 ossId 라고 부르지만 어느 필드가 맞는지는
    아직 확인되지 않았다(추측해 바꾸지 않는다). ID 에 ``|`` 가 들어 있을 수 있다(예: ``11560|934982``) — 그대로 둔다."""
    comp = item.get("fileComponent")
    comp = comp if isinstance(comp, dict) else {}
    for where, value in (("fileComponent.id", comp.get("id")), ("fileComponent.ossId", comp.get("ossId")),
                         ("ossId", item.get("ossId"))):
        v = _scalar_id(value)
        if v:
            return v, where
    return "", ""


def _file_finding(item: Any, kind: str) -> Finding | None:
    if not isinstance(item, dict) or not _scalar_id(item.get("id")):
        return None
    comp_id, _where = _file_component_id(item)
    if not comp_id:
        return None
    comp = item.get("fileComponent") if isinstance(item.get("fileComponent"), dict) else {}
    info = comp.get("info") if isinstance(comp.get("info"), dict) else {}
    # 이름 필드가 있으면 그것을, 없으면 컴포넌트 ID 를 식별자로 둔다(필드 이름은 회사 문서에 없다 — 지어내지 않는다)
    identifier = _text(comp, "identifier") or _text(comp, "name") or comp_id
    return Finding(id=_scalar_id(item["id"]), kind=kind, identifier=identifier, version=_text(comp, "version"),
                   patched_version=_text(info, "patchedVersion"), action=_action(item), score=_max_score(item),
                   component_id=comp_id)


def run_scan(client: Any, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
             receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
             stop: StopCheck | None = None, journal: Journal | None = None, settings: IvasSettings | None = None,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> ScanResult:
    """검사 한 번: (영수증이 없으면) 업로드 → 상태 조회 반복 → 요약 → 모든 쪽 → 완전성 판정. ``IvasUploadUncertain`` 만 밖으로
    나가고, 다른 실패는 ``complete=False`` 결과로 돌아온다. 중지 요청은 조회를 멈출 뿐 원격 분석은 계속된다(취소가 아니다).

    One scan (`interfaces.Scanner`); only `IvasUploadUncertain` propagates, every other failure is an incomplete result."""
    st = settings or IvasSettings()
    col = _Collector()

    def emit(event: dict[str, Any]) -> None:
        if journal is not None:
            journal(event)

    def result(complete_ok: bool, status_code: str = "", findings: list[Finding] | None = None,
               counts: dict[str, int] | None = None) -> ScanResult:
        complete = complete_ok and not col.reasons
        fs = sorted(findings or [], key=lambda f: f.key)
        emit({"event": "ivas_scan_done", "label": label, "complete": complete, "findings": len(fs),
              "reasons": list(col.reasons), "status": status_code})
        return ScanResult(label=label, zip_sha256=zip_sha256, complete=complete, receipt=receipt,
                          status_code=status_code, findings=fs, counts=dict(counts or {}),
                          incomplete_reasons=list(col.reasons), notes=list(col.notes))

    # ---- 업로드 / upload ----
    if receipt is None:
        if stop is not None and stop():
            col.bad("stop_requested", "stop requested before the upload; nothing was sent to IVAS")
            return result(False)
        try:
            receipt = client.upload(Path(zip_path), project_name)     # IvasUploadUncertain 은 그대로 나간다
        except IvasUploadUncertain:
            raise
        except IvasError as exc:
            col.bad("http_error", f"upload failed before IVAS accepted it ({exc.code}): {exc}")
            return result(False)
        if on_receipt is not None:
            on_receipt(receipt)
        emit({"event": "ivas_upload", "label": label, "project_id": receipt.project_id,
              "analysis_id": receipt.analysis_id, "zip_sha256": receipt.zip_sha256})
    if receipt.zip_sha256 != zip_sha256:
        col.bad("zip_hash_mismatch", f"the analysed ZIP sha256 {receipt.zip_sha256 or '(unknown)'} differs from the "
                                     f"expected {zip_sha256}")
        return result(False)
    where = f"IVAS analysis {receipt.analysis_id} (project {receipt.project_id})"

    # ---- 상태 조회 / polling ----
    deadline = clock() + st.analysis_timeout_seconds
    last = ""
    errors_in_row = 0
    while True:
        if stop is not None and stop():
            col.bad("stop_requested", f"stop requested: sbmig stopped polling; {where} keeps running on the IVAS "
                                      "server (it was not cancelled)")
            return result(False, last)
        try:
            code = client.status(receipt.project_id, receipt.analysis_id)
            errors_in_row = 0
        except IvasError as exc:
            if exc.code == "id_mismatch":
                col.bad("id_mismatch", str(exc))
                return result(False, last)
            errors_in_row += 1
            col.note(f"status poll failed ({exc.code}): {exc}")
            if errors_in_row >= STATUS_ERROR_LIMIT:
                col.bad("http_error", f"status could not be read {errors_in_row} times in a row; {where} may still be "
                                      "running on the IVAS server")
                return result(False, last)
            code = None
        if code is not None:
            norm = code.upper()
            if code != last:
                emit({"event": "ivas_status", "label": label, "analysis_id": receipt.analysis_id, "status": code})
                last = code
            if norm == DONE:
                break
            if norm in FAILED_STATES:
                col.bad("analysis_failed", f"{where} ended with status {code}")
                return result(False, last)
            if norm not in IN_PROGRESS:
                col.note(f"unknown anlStatusDivCd {code!r}: kept polling")
        if clock() >= deadline:
            col.bad("analysis_timeout", f"{where} did not finish within {st.analysis_timeout_seconds / 60:g} minutes; it "
                                        "keeps running on the IVAS server (it was not cancelled)")
            return result(False, last)
        sleep(st.poll_seconds)

    # ---- 결과 / results ----
    size = max(int(st.page_size), 1)
    counts: dict[str, int] = {}
    try:
        counts = client.summary(receipt.project_id, receipt.analysis_id)
    except IvasError as exc:
        col.bad("http_error", f"summary could not be read ({exc.code}): {exc}")

    findings: list[Finding] = []
    seen_keys: set[str] = set()
    page_totals: dict[str, int | None] = {}

    def add(f: Finding, what: str) -> None:
        if f.key in seen_keys:
            col.bad("duplicate_result", f"{what}: {f.key} returned more than once")
            return
        seen_keys.add(f.key)
        findings.append(f)

    def malformed(what: str, idx: int) -> None:
        col.bad("http_error", f"{what}: item {idx} is malformed (no id / component id); scan cannot be trusted")

    # library
    items, total, pages = _all_pages(client, receipt, "library", size, col)
    page_totals["library"] = total
    emit({"event": "ivas_pages", "label": label, "kind": "library", "items": len(items), "total": total, "pages": pages})
    for i, item in enumerate(items):
        f = _library_finding(item)
        if f is None:
            malformed("library", i)
        else:
            add(f, "library")

    # sw: 세부 필드를 아직 다루지 않는다 — 항목이 있으면 검사 미완료
    items, total, pages = _all_pages(client, receipt, "sw", size, col)
    page_totals["sw"] = total
    emit({"event": "ivas_pages", "label": label, "kind": "sw", "items": len(items), "total": total, "pages": pages})
    if items or (total or 0) > 0:
        ids = [_scalar_id(x.get("id")) for x in items if isinstance(x, dict)][:10]
        col.bad("unsupported_sw", f"IVAS reported {max(len(items), total or 0)} SW vulnerabilities, a category sbmig "
                                  f"does not handle yet (ids: {', '.join(i for i in ids if i) or '-'})")

    # file components (main / potential) + matched files
    matched_cache: dict[str, list[str]] = {}
    for kind, fkind in (("file_main", "file_component"), ("file_potential", "file_component_potential")):
        items, total, pages = _all_pages(client, receipt, kind, size, col)
        page_totals[kind] = total
        emit({"event": "ivas_pages", "label": label, "kind": kind, "items": len(items), "total": total, "pages": pages})
        for i, item in enumerate(items):
            f = _file_finding(item, fkind)
            if f is None:
                malformed(kind, i)
                continue
            _cid, where = _file_component_id(item)
            if where != "fileComponent.id":
                # fileComponent.id 가 없어 ossId 를 matchedfiles 의 ID 로 썼다 — 어느 필드가 맞는지 회사 확인 전이라 기록한다
                col.note(f"{kind}: file component id taken from {where} (no fileComponent.id)")
            if f.component_id not in matched_cache:
                paths: list[str] = []
                mitems, mtotal, mpages = _all_pages(client, receipt, "matched_files", size, col,
                                                    component_id=f.component_id)
                emit({"event": "ivas_pages", "label": label, "kind": "matched_files", "component_id": f.component_id,
                      "items": len(mitems), "total": mtotal, "pages": mpages})
                for j, m in enumerate(mitems):
                    p = _text(m.get("file") if isinstance(m, dict) else None, "fullPath")
                    if not p:
                        col.bad("http_error", f"matched_files({f.component_id}): item {j} has no file.fullPath")
                        continue
                    if p in paths:
                        col.bad("duplicate_result", f"matched_files({f.component_id}): {p} returned more than once")
                        continue
                    paths.append(p)
                matched_cache[f.component_id] = paths
            f.locations = list(matched_cache[f.component_id])
            add(f, kind)

    # ---- 요약 대조 / summary reconciliation ----
    if counts:
        _reconcile(counts, page_totals, col)
    return result(True, last, findings, counts)


# 요약 범주 ↔ 상세 페이지: 0 / 0 아님만 대조한다(개수의 단위 — 컴포넌트인지 취약점인지 — 는 회사 문서에 없다)
_RECONCILE = (("components", ("library",)), ("swComponents", ("sw",)), ("fileComponents", ("file_main", "file_potential")))


def _reconcile(counts: dict[str, int], page_totals: dict[str, int | None], col: _Collector) -> None:
    compared = []
    for cat, kinds in _RECONCILE:
        totals = [page_totals.get(k) for k in kinds]
        if any(t is None for t in totals):
            continue           # 그 페이지를 읽지 못했다: 이미 page_missing
        pages_total = sum(t or 0 for t in totals)
        compared.append(f"{cat}↔{'+'.join(kinds)}")
        if (counts.get(cat, 0) > 0) != (pages_total > 0):
            col.bad("summary_mismatch", f"summary {cat}.total={counts.get(cat, 0)} but the {'+'.join(kinds)} pages "
                                        f"report {pages_total} items")
    uncovered = [c for c in SUMMARY_CATEGORIES if c not in {r[0] for r in _RECONCILE}]
    col.note("summary check: compared zero / non-zero only for " + (", ".join(compared) or "nothing")
             + "; categories without a detail endpoint must be 0: " + ", ".join(uncovered))
    for cat in uncovered:
        if counts.get(cat, 0) > 0:
            col.bad("summary_mismatch", f"summary {cat}.total={counts[cat]} but sbmig has no detail endpoint for {cat}; "
                                        "those vulnerabilities are not in the findings")
