"""출력 직전 비밀 값 정제 훅 — 로그 줄과 저널 줄이 파일·콘솔에 닿기 직전에 지나가는 한 곳이다.

기본은 아무 일도 하지 않는다(정제기가 없으면 `scrub` 은 받은 문자열을 그대로 돌려준다). 환경 변수 `SBMIG_REDACT_OUTPUT=1`
이면 진입점(CLI 콜백, 웹 서버)이 정제기를 건다(`sbmig.web.redact.install_output_redaction`). 그때부터 모든 로그 포매터
(`sbmig.core.stage.StageFormatter`)와 `Workspace.journal` 이 쓰는 문자열에서 비밀 값이 `***` 로 바뀐다. 실행이 끝난 뒤
파일을 훑어 지우는 것만으로는 실행 중에 로그를 따라 읽는 쪽(파드 로그, 웹 화면의 로그 탭)이 지워지기 전의 값을 본다 —
그래서 쓰는 순간에 지운다(리뷰 2026-11-10). 정제기가 예외를 내면 원문 대신 고정 문구 `WITHHELD` 를 쓴다: 실패해도 새지 않게.
어떤 값이 비밀인지는 이 모듈이 모른다(env 이름 규칙은 `sbmig.web.contract` 의 일) — core 가 web 을 import 하지 않도록
훅만 둔다. 실행 중에 생기는 비밀(토큰 교환으로 받은 접근 토큰)은 `note_secret` 로 알린다.

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

REDACT_OUTPUT_ENV = "SBMIG_REDACT_OUTPUT"            # "1" 이면 진입점이 정제기를 건다
WITHHELD = "[sbmig: output redaction failed - line withheld]"
WITHHELD_EVENT = "output_withheld"                   # 정제에 실패한 저널 줄의 event 값

_scrubber: Callable[[str], str] | None = None
_on_refresh: Callable[[], None] | None = None
_noted: set[str] = set()
_lock = threading.Lock()


def redaction_requested(environ: Mapping[str, str] | None = None) -> bool:
    """환경 변수 `SBMIG_REDACT_OUTPUT` 가 `1` 인가(기본: 지금 프로세스의 환경).

    Is `SBMIG_REDACT_OUTPUT=1` set (in `environ`, default this process's environment)?"""
    env = os.environ if environ is None else environ
    return (env.get(REDACT_OUTPUT_ENV) or "").strip() == "1"


def set_output_scrubber(fn: Callable[[str], str] | None, on_refresh: Callable[[], None] | None = None) -> None:
    """정제기를 건다(None 이면 뗀다). `on_refresh` 는 비밀 값 목록이 바뀌었을 수 있을 때(`refresh_secrets`) 불린다.

    Install the scrubber (None removes it); `on_refresh` runs whenever the set of secrets may have changed."""
    global _scrubber, _on_refresh
    _scrubber, _on_refresh = fn, (on_refresh if fn is not None else None)


def output_scrubbing() -> bool:
    """정제기가 걸려 있는가. / Is a scrubber installed?"""
    return _scrubber is not None


def scrub(text: str) -> str:
    """문자열 하나를 정제한다. 정제기가 없으면 그대로, 정제기가 예외를 내면 `WITHHELD`(원문은 쓰지 않는다). 예외 없음.

    Scrub one string: unchanged without a scrubber, `WITHHELD` when the scrubber raises. Never raises."""
    fn = _scrubber
    if fn is None:
        return text
    try:
        return fn(text)
    except Exception:  # noqa: BLE001 — 로그를 쓰다 죽거나 원문을 내보내느니 그 줄을 버린다
        return WITHHELD


def scrub_json(obj: Any) -> Any:
    """JSON 값(dict / list / 문자열)의 모든 문자열(키 포함)을 정제한 사본을 돌려준다 — 직렬화한 줄을 통째로 바꾸면
    따옴표·역슬래시가 든 값에서 JSON 이 깨질 수 있어 값 단위로 한다. 숫자·bool·None 은 그대로. 정제기가 없으면 받은 객체 그대로.
    정제기가 예외를 내면 값 전체를 버리고 고정 모양을 돌려준다(dict → `{"event": WITHHELD_EVENT, "message": WITHHELD}`,
    list → `[WITHHELD]`, 문자열 → `WITHHELD`): 키마다 따로 버리면 키가 서로 겹쳐 읽는 쪽이 알 수 없는 모양이 된다.

    A copy of a JSON value with every string (keys included) scrubbed; the object itself when no scrubber is installed.
    A scrubber that raises yields one fixed shape for the whole value (`{"event": "output_withheld", …}` for a dict)."""
    fn = _scrubber
    if fn is None:
        return obj
    try:
        return _scrub_json(obj, fn)
    except Exception:  # noqa: BLE001 — 원문은 쓰지 않는다
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
    """예외 하나의 traceback 글을 정제해서 돌려준다(지역 변수 없이, 줄바꿈 없이 표준 형식).

    The standard traceback text of one exception, scrubbed (no locals, no wrapping)."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:  # noqa: BLE001 — __str__ 이 죽는 예외도 있다
        text = f"{getattr(exc_type, '__name__', exc_type)}: <unprintable>\n"
    return scrub(text)


def install_scrubbed_excepthooks() -> None:
    """잡히지 않은 예외의 traceback 도 정제기를 지나게 한다(`sys.excepthook`, `threading.excepthook`). 로그 포매터를 거치지
    않는 유일한 출력 경로다: 기본 훅(과 Typer 의 rich traceback — 지역 변수까지 찍는다)은 예외 글을 그대로 stderr 에 쓴다.
    진입점이 정제기를 건 뒤에 부른다.

    Route uncaught-exception tracebacks (main thread and other threads) through the scrubber: the default hooks (and
    Typer's rich traceback, which prints locals) write the exception text raw to stderr."""
    def _hook(exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            sys.stderr.write(format_exception_scrubbed(exc_type, exc, tb))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 — stderr 가 닫혔다: 할 수 있는 일이 없다
            pass

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        _hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def refresh_secrets() -> None:
    """비밀 값 목록이 바뀌었을 수 있다고 정제기에 알린다(`.env` 를 방금 읽었다, 새 토큰을 받았다). 예외 없음.

    Tell the scrubber its secrets may have changed (a `.env` was just read, a token was just issued). Never raises."""
    fn = _on_refresh
    if fn is None:
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 — 알림일 뿐이다: 다음 주기에 정제기가 스스로 다시 읽는다
        pass


def note_secret(value: Any) -> None:
    """실행 중에 생긴 비밀 값을 알린다(환경 변수에 없는 것 — 토큰 교환으로 받은 접근 토큰). 빈 값·문자열이 아닌 값은 무시.

    Register a secret born during the run (not in the environment: an exchanged access token)."""
    if not isinstance(value, str) or not value.strip():
        return
    with _lock:
        if value in _noted:
            return
        _noted.add(value)
    refresh_secrets()


def noted_secrets() -> list[str]:
    """`note_secret` 로 알려진 값들. / The values registered through `note_secret`."""
    with _lock:
        return sorted(_noted)
