"""로그 줄과 저널 줄을 기록하기 직전에 비밀 값을 가리는(마스킹) 훅을 제공하는 모듈.
- 기본으로는 아무것도 하지 않는다. `SBMIG_REDACT_OUTPUT=1`이면 진입점이 마스킹 함수를 등록한다
- 실행 중에 로그를 읽는 쪽(파드 로그, 웹 로그 탭)도 원래 값을 보지 못하도록 기록하는 시점에 마스킹한다
- 마스킹 함수가 예외를 던지면 원문 대신 `WITHHELD`를 기록한다
- 무엇이 비밀 값인지는 `sbmig.web.contract`가 판단한다. core가 web을 import하지 않도록 여기에는 훅만 둔다
- 실행 중에 생긴 비밀 값은 `note_secret`으로 등록한다

A process-wide hook every log line and journal line passes through just before it is written (review 2026-11-10: a
pass over the files after the run leaves the live readers — the pod log, the web UI's log tab — with the raw values).
A no-op until an entry point installs a scrubber (`SBMIG_REDACT_OUTPUT=1` → `sbmig.web.redact.install_output_redaction`).
Fail closed: a scrubber that raises yields `WITHHELD`, never the raw text. Which values are secret is not decided here
(`sbmig.web.contract` owns the env-name rule); secrets born during a run (an exchanged access token) arrive through
`note_secret`.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from typing import Any

REDACT_OUTPUT_ENV = "SBMIG_REDACT_OUTPUT"            # "1"이면 진입점이 마스킹 함수를 등록한다
WITHHELD = "[sbmig: output redaction failed - line withheld]"
WITHHELD_EVENT = "output_withheld"                   # 마스킹에 실패한 저널 줄의 event 값

_scrubber: Callable[[str], str] | None = None
_on_refresh: Callable[[], None] | None = None
_noted: set[str] = set()
_lock = threading.Lock()


def redaction_requested(environ: Mapping[str, str] | None = None) -> bool:
    """환경 변수 `SBMIG_REDACT_OUTPUT`이 `1`인지 확인한다.
    environ을 주지 않으면 현재 프로세스의 환경을 본다.

    Is `SBMIG_REDACT_OUTPUT=1` set (in `environ`, default this process's environment)?"""
    env = os.environ if environ is None else environ
    return (env.get(REDACT_OUTPUT_ENV) or "").strip() == "1"


def set_output_scrubber(fn: Callable[[str], str] | None, on_refresh: Callable[[], None] | None = None) -> None:
    """마스킹 함수를 등록하고, None을 주면 해제한다.
    `on_refresh`는 비밀 값 목록이 바뀌었을 수 있을 때 호출된다.

    Install the scrubber (None removes it); `on_refresh` runs whenever the set of secrets may have changed."""
    global _scrubber, _on_refresh
    _scrubber, _on_refresh = fn, (on_refresh if fn is not None else None)


def output_scrubbing() -> bool:
    """마스킹 함수가 등록되어 있는지 반환한다. / Is a scrubber installed?"""
    return _scrubber is not None


def scrub(text: str) -> str:
    """문자열 하나를 마스킹해서 반환한다. 예외를 던지지 않는다.
    마스킹 함수가 없으면 문자열을 그대로, 마스킹 함수가 예외를 던지면 `WITHHELD`를 반환한다.

    Scrub one string: unchanged without a scrubber, `WITHHELD` when the scrubber raises. Never raises."""
    fn = _scrubber
    if fn is None:
        return text
    try:
        return fn(text)
    except Exception:  # noqa: BLE001, 원문을 내보내지 않도록 그 줄을 버린다
        return WITHHELD


def scrub_json(obj: Any) -> Any:
    """JSON 값 안의 모든 문자열(키 포함)을 마스킹한 복사본을 반환한다.
    직렬화한 줄을 통째로 바꾸면 JSON이 깨질 수 있으므로 값 단위로 처리한다.
    마스킹 함수가 예외를 던지면 값 전체를 정해진 형태로 바꾼다.

    A copy of a JSON value with every string (keys included) scrubbed; the object itself when no scrubber is installed.
    A scrubber that raises yields one fixed shape for the whole value (`{"event": "output_withheld", …}` for a dict)."""
    fn = _scrubber
    if fn is None:
        return obj
    try:
        return _scrub_json(obj, fn)
    except Exception:  # noqa: BLE001, 원문이 기록되지 않도록 한다
        if isinstance(obj, dict):
            return {"event": WITHHELD_EVENT, "message": WITHHELD}
        if isinstance(obj, (list, tuple)):
            return [WITHHELD]
        return WITHHELD if isinstance(obj, str) else obj


def _scrub_json(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {(fn(k) if isinstance(k, str) else k): _scrub_json(v, fn) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_json(v, fn) for v in obj]
    return obj


def format_exception_scrubbed(exc_type: Any, exc: Any, tb: Any) -> str:
    """예외 하나의 traceback 텍스트를 마스킹해서 반환한다. 지역 변수 값은 넣지 않는다.

    The standard traceback text of one exception, scrubbed (no locals, no wrapping)."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:  # noqa: BLE001, 예외 객체의 __str__에서 다시 예외가 날 수 있다
        text = f"{getattr(exc_type, '__name__', exc_type)}: <unprintable>\n"
    return scrub(text)


def install_scrubbed_excepthooks() -> None:
    """잡히지 않은 예외의 traceback도 마스킹을 거치도록 `sys.excepthook`과 `threading.excepthook`을 바꾼다.
    기본 훅과 Typer의 rich traceback은 예외 텍스트를 그대로 stderr에 출력하기 때문이다.
    진입점이 마스킹 함수를 등록한 뒤에 호출한다.

    Route uncaught-exception tracebacks (main thread and other threads) through the scrubber: the default hooks (and
    Typer's rich traceback, which prints locals) write the exception text raw to stderr."""
    def _hook(exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            sys.stderr.write(format_exception_scrubbed(exc_type, exc, tb))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001, stderr가 닫혀 있으면 할 수 있는 일이 없다
            pass

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        _hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def refresh_secrets() -> None:
    """비밀 값 목록이 바뀌었을 수 있다고 마스킹 함수에 알린다.
    `.env`를 읽었거나 새 토큰을 받았을 때 호출한다. 예외를 던지지 않는다.

    Tell the scrubber its secrets may have changed (a `.env` was just read, a token was just issued). Never raises."""
    fn = _on_refresh
    if fn is None:
        return
    try:
        fn()
    except Exception:  # noqa: BLE001, 알림이 실패해도 다음 주기에 마스킹 함수가 목록을 다시 읽는다
        pass


def note_secret(value: Any) -> None:
    """실행 중에 생긴 비밀 값을 마스킹 대상으로 등록한다. 빈 값이나 문자열이 아닌 값은 무시한다.

    Register a secret born during the run (not in the environment: an exchanged access token)."""
    if not isinstance(value, str) or not value.strip():
        return
    with _lock:
        if value in _noted:
            return
        _noted.add(value)
    refresh_secrets()


def noted_secrets() -> list[str]:
    """`note_secret`으로 등록된 값 목록을 반환한다. / The values registered through `note_secret`."""
    with _lock:
        return sorted(_noted)
