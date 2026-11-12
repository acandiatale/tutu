"""회사 취약점 점검 도구 IVAS(LabradorLabs)의 HTTP 클라이언트와 검사 절차를 제공하는 모듈.
검사 절차는 업로드 -> 상태 조회 -> 요약 -> 모든 페이지 -> 결과가 빠짐없는지 확인 순서다. 회사가 알려 준
API만 쓴다: ``POST /session`` 로그인과 ``X-API-KEY`` 헤더, ``POST /projects/SELF/sourcecode``
업로드(multipart: ``analysisRequest`` + 파일), ``…/analysis/{id}/status``, ``…/result/summary``,
``…/result/<종류>?page=&size=``. 모르는 것은 추측하지 않고 미완료로 남긴다.
- 업로드는 자동으로 다시 하지 않는다. 본문이 나갔을 수 있는 애매한 상황(타임아웃, 연결 끊김, 5xx, ID 없는
  2xx)은 ``IvasUploadUncertain``으로 처리해 사람이 IVAS에서 확인할 때까지 멈춘다. 보내기 전의 실패와 분명한
  4xx 거절만 ``IvasError``다
- 토큰은 메모리에만 두고, 토큰과 비밀번호는 로그, 예외 메시지, repr에 넣지 않는다
- TLS 검증은 끄지 않는다
- 빠진 페이지, 바뀐 전체 개수, 중복 항목, 요약과 맞지 않는 개수, 지원하지 않는 범주(SW), HTTP 오류가
  하나라도 있으면 ``complete=False``이며, 이때 ``findings``가 비어 있어도 0건으로 보지 않는다
- ``remediation.ivas_summary_check: exact``면 ``components.total``이 모든 페이지에서 받은 라이브러리 취약점
  행 수(중복 제거 전)와 같아야 한다. swComponents / fileComponents와 ``nonzero`` 모드는 0인지 아닌지만 본다
- 조치 구분 코드의 뜻은 ``ACTION_MEANINGS``에만 둔다. ``remediation.ignore_actions``에 따른 제외는 그래프가
  ``split_findings``로 처리하고, 빈 코드와 모르는 코드는 조치 대상이다
- 파일 컴포넌트 ID에는 ``|``가 있을 수 있으므로(``11560|934982``) matchedfiles 경로에 경로 조각 하나로 인코딩해서 보낸다

IVAS HTTP client and scan procedure. Only the HTTP contract the company gave is used; anything unknown makes the
scan incomplete instead of being guessed. An upload is never retried automatically: any ambiguity after the body may
have left raises `IvasUploadUncertain`. Tokens / passwords never reach a log line, an exception message or a repr.
TLS verification is never disabled. `components.total` must equal the library rows returned (`ivas_summary_check: exact`).
Action-code meanings live in `ACTION_MEANINGS`; the graph splits the findings by
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

# 회사가 알려 준 분석 상태 코드(anlStatusDivCd) 값
IN_PROGRESS = ("PREPARING", "WAITING", "ANALYSIS")
DONE = "DONE"
FAILED_STATES = ("FAILED", "ERROR", "CANCELED", "CANCELLED")

# 요약의 범주 8개(summary.resultCount.vulnerabilities.<범주>.total)
SUMMARY_CATEGORIES = ("components", "swComponents", "fileComponents", "codeSnippets", "file", "functionComponents",
                      "ospkgComponents", "binaryComponents")
# 분석 ID 필드 이름. 회사 문서에 lbrdAnlId와 lbrdrAnlId가 둘 다 나오므로 셋 다 받는다
ANALYSIS_ID_KEYS = ("lbrdAnlId", "lbrdrAnlId", "analysisId")

# 조치 구분 코드(action.actionDivCd)의 뜻. 회사 IVAS 문서의 표이며 보고서도 여기서 읽는다
# 어떤 코드를 조치 대상에서 뺄지는 ``remediation.ignore_actions``가 정한다
ACTION_MEANINGS: dict[str, str] = {
    "NE": "조치 필요",
    "NP": "조치계획 미등록",
    "PL": "조치예정",
    "OT": "조치일 경과",
    "PC": "패치 완료",
    "IG": "조치 예외",
}
# 기본값이 비어 있으므로 모든 조치 구분 코드를 조치 대상으로 본다. 기존 사내 구현도 PC, IG를 거르지 않는다
DEFAULT_IGNORE_ACTIONS: tuple[str, ...] = ()
SUMMARY_CHECKS = ("exact", "nonzero")

# 결과 종류별 URL 끝부분(analysis/{id}/ 뒤)
PAGE_PATHS = {
    "library": ("result", "libraryvulnerabilities"),
    "sw": ("result", "swvulnerabilities"),
    "file_main": ("result", "filecomponentvulnerabilities", "main"),
    "file_potential": ("result", "filecomponentvulnerabilities", "potential"),
}

GET_TRIES = 3                  # GET 요청 하나의 최대 시도 횟수(연결 오류, 타임아웃, 5xx, 429일 때)
BACKOFF_SECONDS = (1.0, 2.0)   # 재시도 사이의 대기 시간(초)
BODY_QUOTE_CHARS = 300         # 오류 메시지에 인용하는 응답 본문의 최대 길이
STATUS_ERROR_LIMIT = 3         # 상태 조회가 연달아 이 횟수만큼 실패하면 검사 미완료(http_error)로 본다
_MASK = "***"


# ---------------------------------------------------------------------------- 설정 / settings
@dataclass
class IvasSettings:
    """IVAS 접속 설정을 담는 데이터 클래스.
    주소, 아이디, 비밀번호는 환경 변수(Secret)에서만 읽고 나머지는 ``cfg.remediation.ivas_*``에서 읽는다.
    비밀번호는 repr에 나오지 않는다.

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
    # exact는 요약의 components 건수와 라이브러리 취약점 상세 행 수가 같아야 하고, nonzero는 0인지 아닌지만 본다
    summary_check: str = "exact"


def ivas_settings(cfg: Any, env: Mapping[str, str] = os.environ) -> IvasSettings:
    """설정 객체와 환경 변수로 ``IvasSettings``를 만든다.
    비밀 값은 설정 파일이 아니라 환경 변수에서만 읽는다.

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
        summary_check=str(getattr(rem, "ivas_summary_check", "exact")),
    )


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def readiness(settings: IvasSettings) -> list[str]:
    """IVAS를 쓸 수 없게 하는 문제 목록을 반환한다. 빈 목록이면 쓸 수 있다.
    문제 항목의 이름만 담고 값은 넣지 않는다.
    주소는 https여야 하며, 테스트용 loopback 주소만 http를 허용한다.

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
    """IVAS 프로젝트 이름 ``<접두어>-<작업 ID>-<label>``을 만든다.

    The IVAS project name `<prefix>-<id>-<label>`."""
    return f"{settings.project_prefix}-{remediation_id}-{label}"


# ---------------------------------------------------------------------------- multipart 본문 / streamed body
class _MultipartStream:
    """파일 전체를 메모리에 올리지 않고 multipart 본문을 조각으로 내보내는 클래스.
    길이를 미리 계산해 Content-Length로 보낸다.
    실제로 보낸 파일 바이트의 sha256도 계산하며, 접수 정보의 zip_sha256은 이 값이다."""

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
                if not chunk:          # 보내는 도중 파일이 줄어든 것이므로, 길이가 맞지 않는 본문은 보내지 않는다
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
    """예외 체인 안에 접속 실패(urllib3 NewConnectionError: 연결 거부, 이름 해석 실패)가 있는지 확인한다.
    알 수 없으면 False를 반환해 불확실한 경우로 처리하게 한다."""
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
    """조치 구분 코드를 비교할 수 있도록 앞뒤 공백을 지우고 대문자로 바꾼다.
    문자열이 아니면 빈 문자열을 반환한다.

    An action code for comparison: trimmed, upper-cased ('' for a non-string)."""
    return code.strip().upper() if isinstance(code, str) else ""


def action_meaning(code: Any) -> str:
    """조치 구분 코드의 한국어 뜻을 반환한다.
    표에 없으면 "알 수 없는 코드", 비어 있으면 "코드 없음"을 반환한다.

    The Korean meaning of an action code."""
    norm = normalize_action(code)
    if not norm:
        return "코드 없음"
    return ACTION_MEANINGS.get(norm, "알 수 없는 코드")


def ignore_set(value: Any) -> frozenset[str]:
    """설정 값(``cfg`` / ``cfg.remediation`` / 코드 목록 / None)에서 조치 대상에서 뺄 코드 집합을 정규화해서 만든다.
    None이거나 설정이 없으면 기본값인 빈 집합을 반환한다.
    빈 문자열 항목은 버리므로 빈 코드는 제외 대상이 되지 않는다.

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
    """이 항목의 조치 구분 코드가 제외 대상인지 확인한다. 빈 코드는 항상 조치 대상이다.

    True when the finding's action code is in the ignored set; an empty code never is."""
    norm = normalize_action(finding.action)
    return bool(norm) and norm in ignore


def split_findings(findings: list[Finding], ignore: frozenset[str]) -> tuple[list[Finding], list[Finding]]:
    """취약점 목록을 (조치 대상, 제외 항목)으로 나눈다. 원래 순서는 유지한다.

    (actionable, ignored), order kept."""
    act: list[Finding] = []
    ign: list[Finding] = []
    for f in findings:
        (ign if is_ignored(f, ignore) else act).append(f)
    return act, ign


def unknown_actions(findings: list[Finding]) -> list[str]:
    """표에 없는 조치 구분 코드 목록을 정렬하고 중복을 빼서 반환한다. 빈 코드는 "(empty)"로 표시한다.
    이런 항목은 조치 대상으로 다루며, 설정에 그 코드를 직접 지정한 경우에만 뺀다.

    Action codes outside the known table ('(empty)' for a missing one), sorted."""
    out = set()
    for f in findings:
        norm = normalize_action(f.action)
        if norm not in ACTION_MEANINGS:
            out.add(norm or "(empty)")
    return sorted(out)


# ---------------------------------------------------------------------------- HTTP 클라이언트 / client
class IvasHttpClient:
    """requests로 ``interfaces.IvasClient``를 구현한 클래스. 처음 필요할 때 로그인하고 토큰은 메모리에만
    두며, 이후 401/403을 받으면 한 번 다시 로그인해서 그 호출을 재시도한다.
    업로드 직전에는 항상 새로 로그인하고 업로드의 401은 분명한 거절로 본다.
    GET은 연결 오류, 타임아웃, 5xx, 429일 때 최대 3번 시도하고, 업로드는 재시도하지 않는다.

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
        """오류 메시지에 응답 본문을 인용할 때 비밀 값(토큰, 비밀번호)을 지우고 길이를 자른다."""
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
        except Exception:        # 본문을 못 읽어도 오류 메시지는 만든다
            return ""

    def _backoff(self, attempt: int) -> None:
        self._sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])

    # ---- login -----------------------------------------------------------
    def _login(self) -> None:
        """``POST /session``으로 로그인해서 토큰을 받는다. 일시적인 오류(연결, 타임아웃, 5xx, 429)는 최대
        3번 시도하고, 거절되거나 응답을 읽을 수 없으면 IvasError를 던진다."""
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
                # 잘못된 주소처럼 보내기 전에 실패한 것은 다시 해도 같으므로 분명한 실패로 처리한다
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
            try:                 # 출력 마스킹이 켜져 있으면 이 토큰도 가리도록 등록한다
                from sbmig.core.scrub import note_secret
                note_secret(token)
            except Exception:    # pragma: no cover - 마스킹 기능이 없어도 클라이언트는 동작한다
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
                # 그 밖의 requests 오류(잘못된 주소, 너무 많은 redirect 등)도 IvasError로 처리하므로 검사는
                # 0건이 아니라 미완료가 된다
                raise IvasError(f"IVAS GET {where}: {type(exc).__name__}", code="http_error") from None
            code = r.status_code
            log.debug("ivas: GET %s -> HTTP %s", where, code)
            if code in (401, 403) and not relogged:
                # 토큰이 만료됐을 수 있으므로 한 번만 다시 로그인하고 같은 호출을 재시도한다
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
        """ZIP을 한 번만 올리고 재시도하지 않는다. 보내기 전의 실패와 분명한 4xx 거절은 ``IvasError``를,
        본문이 나갔을 수 있는 애매한 상황은 ``IvasUploadUncertain``을 던진다.
        캐시한 토큰이 업로드 도중 만료되지 않도록 직전에 항상 새로 로그인한다.

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
            self._login()       # 여기서 실패하면 IvasError다. 아직 아무것도 보내지 않았다
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
            except Exception as exc:   # 타임아웃, 연결 끊김, 파일 읽기 오류 등은 본문 일부가 이미 나갔을 수 있다
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
        # 접수 정보의 sha256은 실제로 보낸 바이트의 해시다.
        # 끝까지 보내지 못했으면 빈 값이 되고 검사 쪽이 zip_hash_mismatch로 처리한다
        sent_sha = body.file_sha256 if body.file_bytes == size else ""
        return UploadReceipt(project_id=project_id, analysis_id=analysis_id, zip_sha256=sent_sha, project_name=project_name)

    def status(self, project_id: str, analysis_id: str) -> str:
        """분석 상태 코드 ``anlStatusDivCd``를 그대로 반환한다.
        응답의 ID가 요청과 다르면 ``IvasError(code="id_mismatch")``를 던진다.

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
        """검사 요약에서 범주별 total 8개를 반환한다.
        읽지 못한 요약을 0으로 보지 않도록, 하나라도 읽을 수 없으면 ``IvasError``를 던진다.

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
        """검사 결과 한 페이지(page는 0부터)를 반환한다.
        ``content`` 목록과 정수 ``totalElements``가 없으면 ``IvasError``를 던진다.

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
    """검사 하나의 미완료 사유와 메모를 순서대로, 중복 없이 모으는 클래스."""

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
    """한 종류의 결과를 끝까지 읽어 (항목들, 첫 페이지의 totalElements, 읽은 페이지 수)를 반환한다.
    빠진 페이지, 바뀐 전체 개수, 개수 불일치는 ``col``에 사유로 남긴다.
    무한 반복을 막기 위해 페이지 수 상한(전체/크기 + 2)을 둔다."""
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
    """파일 컴포넌트의 ID와 그 ID를 읽은 필드 이름을 반환한다.
    ``fileComponent.id``를 먼저 쓰고, 없으면 ``fileComponent.ossId``, 최상위 ``ossId`` 순으로 쓴다.
    회사는 matchedfiles 경로의 ID를 ossId라고 부르지만 어느 필드가 맞는지 확인되지 않아 추측해서 바꾸지
    않는다. ID에 ``|``가 있을 수 있으며(예: ``11560|934982``) 그대로 둔다."""
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
    # 이름 필드가 있으면 그것을, 없으면 컴포넌트 ID를 식별자로 쓴다.
    # 필드 이름이 회사 문서에 없으므로 임의로 만들지 않는다
    identifier = _text(comp, "identifier") or _text(comp, "name") or comp_id
    return Finding(id=_scalar_id(item["id"]), kind=kind, identifier=identifier, version=_text(comp, "version"),
                   patched_version=_text(info, "patchedVersion"), action=_action(item), score=_max_score(item),
                   component_id=comp_id)


def run_scan(client: Any, zip_path: Path, zip_sha256: str, label: str, project_name: str, *,
             receipt: UploadReceipt | None = None, on_receipt: Callable[[UploadReceipt], None] | None = None,
             stop: StopCheck | None = None, journal: Journal | None = None, settings: IvasSettings | None = None,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> ScanResult:
    """IVAS 검사를 한 번 실행하고 결과를 반환한다.
    순서는 업로드(접수 정보가 없을 때) -> 상태 조회 반복 -> 요약 -> 모든 페이지 -> 결과가 빠짐없는지
    확인이다. ``IvasUploadUncertain``만 밖으로 던지고 다른 실패는 ``complete=False`` 결과로 반환한다.
    중지 요청이 오면 조회만 멈추고 원격 분석은 취소하지 않는다.

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
            receipt = client.upload(Path(zip_path), project_name)     # IvasUploadUncertain은 잡지 않고 그대로 던진다
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
    rows_returned: dict[str, int] = {}

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
    if total is not None and len(items) == total:
        # 요약과의 비교는 중복을 제거하기 전에 받은 행 수로 한다
        # 페이지를 읽을 때 이미 page_missing / total_mismatch를 남겼으면 summary_mismatch로 한 번 더 세지 않는다
        rows_returned["library"] = len(items)
    emit({"event": "ivas_pages", "label": label, "kind": "library", "items": len(items), "total": total, "pages": pages})
    for i, item in enumerate(items):
        f = _library_finding(item)
        if f is None:
            malformed("library", i)
        else:
            add(f, "library")

    # sw 결과는 세부 필드를 아직 처리하지 않으므로 항목이 있으면 검사를 미완료로 본다
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
                # fileComponent.id가 없어 ossId를 matchedfiles ID로 썼다.
                # 어느 필드가 맞는지 회사가 확인하기 전이므로 기록해 둔다
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
        _reconcile(counts, page_totals, col, rows_returned=rows_returned, mode=st.summary_check)
    return result(True, last, findings, counts)


# 요약 범주와 상세 페이지의 대응. components는 exact 모드에서 라이브러리 취약점 행 수와 같은지 비교한다
# 나머지 둘은 개수 단위가 문서에 없으므로 0인지 아닌지만 비교한다
_RECONCILE = (("components", ("library",)), ("swComponents", ("sw",)), ("fileComponents", ("file_main", "file_potential")))
_EXACT = {"components": "library"}


def _reconcile(counts: dict[str, int], page_totals: dict[str, int | None], col: _Collector, *,
               rows_returned: Mapping[str, int] | None = None, mode: str = "exact") -> None:
    rows = dict(rows_returned or {})
    exact_mode = str(mode).strip().lower() != "nonzero"
    compared = []
    for cat, kinds in _RECONCILE:
        totals = [page_totals.get(k) for k in kinds]
        if any(t is None for t in totals):
            continue           # 그 페이지를 읽지 못해 이미 page_missing으로 남겼다
        exact_kind = _EXACT.get(cat) if exact_mode else None
        if exact_kind is not None and exact_kind not in rows:
            compared.append(f"{cat}: not compared ({exact_kind} pages incomplete)")
            continue
        if exact_kind is not None:
            n = rows[exact_kind]
            compared.append(f"{cat}=={exact_kind} rows (exact)")
            if counts.get(cat, 0) != n:
                col.bad("summary_mismatch", f"summary {cat}.total={counts.get(cat, 0)} but {n} {exact_kind} vulnerability "
                                            "rows were returned over all pages (remediation.ivas_summary_check: exact)")
            continue
        pages_total = sum(t or 0 for t in totals)
        compared.append(f"{cat}↔{'+'.join(kinds)} (zero / non-zero)")
        if (counts.get(cat, 0) > 0) != (pages_total > 0):
            col.bad("summary_mismatch", f"summary {cat}.total={counts.get(cat, 0)} but the {'+'.join(kinds)} pages "
                                        f"report {pages_total} items")
    uncovered = [c for c in SUMMARY_CATEGORIES if c not in {r[0] for r in _RECONCILE}]
    col.note("summary check (" + ("exact" if exact_mode else "nonzero") + "): " + (", ".join(compared) or "nothing")
             + "; the swComponents / fileComponents mapping is not confirmed, so only zero / non-zero is compared there"
             + "; categories without a detail endpoint must be 0: " + ", ".join(uncovered))
    for cat in uncovered:
        if counts.get(cat, 0) > 0:
            col.bad("summary_mismatch", f"summary {cat}.total={counts[cat]} but sbmig has no detail endpoint for {cat}; "
                                        "those vulnerabilities are not in the findings")
