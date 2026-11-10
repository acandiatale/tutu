"""실행 폴더의 텍스트 출력에서 비밀 값을 지운다 (v2.1z-ac, 2026-11-09 — Worker 가 끝날 때 공유 볼륨에 남는 로그·보고서용).

비밀로 보이는 환경 변수(``contract.secret_values``: 이름에 TOKEN / PASSWORD / SECRET / API_KEY …, 6자 이상)의 값을 ``***`` 로 바꾸고, URL 의
사용자 정보(``https://user:pass@host``)도 가린다. 손대는 곳은 실행 폴더 최상위의 텍스트 파일(``*.md`` / ``*.json`` / ``*.jsonl`` / ``*.log`` /
``*.txt`` / ``*.yml``)과 ``logs/`` 아래뿐이다 — ``control/``(Main 의 파일), ``source.zip`` / ``result.zip``, ``project/``(이관된 소스)는 건드리지
않는다. 바뀐 것이 있을 때만 다시 쓴다.

리뷰 2026-11-10: 끝난 뒤에 지우는 것만으로는 실행 중의 출력(파드 로그, 웹 화면이 따라 읽는 ``sbmig.log`` / ``journal.jsonl``)이
지워지기 전에 보인다. 그래서 같은 규칙을 **쓰는 순간**에도 적용한다 — ``Redactor`` 는 미리 펼쳐 둔 값으로 한 줄씩 지우는 객체이고,
``install_output_redaction`` 은 그것을 이 프로세스의 로그·저널 출력 훅(``sbmig.core.scrub``)에 건다(``SBMIG_REDACT_OUTPUT=1``).
비밀 값은 적힌 그대로만 나오지 않는다: JSON 문자열 안에서는 이스케이프되고 URL 안에서는 퍼센트 인코딩되고 여러 줄짜리 값은 줄마다
따로 찍힌다 — ``expand_values`` 가 그 표기들을 함께 지울 값으로 펼친다.

Redact secret values (and URL userinfo) from a run directory's text outputs: top-level text files and `logs/`; never
`control/`, the zips or `project/`. A file is rewritten only when something changed. Review 2026-11-10: the same rule
also runs at write time — `Redactor` (pre-expanded values, one line at a time) and `install_output_redaction` (hooks it
into this process's log / journal output); `expand_values` adds the spellings a secret takes in output."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path

from sbmig.core import scrub
from sbmig.web import contract

log = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".md", ".json", ".jsonl", ".log", ".txt", ".yml"})
# scheme://userinfo@ — userinfo never holds "/", "@" or whitespace; any scheme (https, git+ssh, jdbc:…:// …)
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^\s/@:'\"<>]+(?::[^\s/@'\"<>]*)?)@")
# 여러 줄짜리 비밀(PEM 키, JSON 자격 증명)은 출력에서 줄마다 따로 찍힌다: 이 길이 이상인 줄만 따로 지운다 — "}" 같은 짧은 줄이
# 로그 전체를 지우지 않게
MIN_LINE_LEN = 2 * contract.MIN_SECRET_LEN
LIVE_REFRESH_S = 1.0               # 실행 중 정제기가 환경(비밀 값 목록)을 다시 읽는 간격


def expand_values(values: Iterable[str]) -> list[str]:
    """지울 값들을 출력에 나타나는 표기까지 펼친다(긴 것부터, 중복 없음): 값 그대로, 앞뒤 공백을 뗀 것, JSON 문자열 안의 표기
    (``"`` · ``\\`` · 비 ASCII 이스케이프), URL 퍼센트 인코딩, Python ``repr`` 안의 표기, 여러 줄짜리 값의 각 줄(``MIN_LINE_LEN`` 이상).
    펼친 표기는 ``contract.MIN_SECRET_LEN`` 보다 짧으면 버린다(받은 값 자체는 길이와 무관하게 남긴다 — 부른 쪽의 선택).

    The values plus the spellings they take in output — stripped, JSON-escaped, percent-encoded, `repr`-escaped, each line
    of a multi-line value — longest first, no duplicates. Derived spellings shorter than `MIN_SECRET_LEN` are dropped."""
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        out.add(v)
        forms = {v.strip(), json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1],
                 urllib.parse.quote(v, safe=""), repr(v)[1:-1]}
        lines = v.splitlines()
        if len(lines) > 1:
            forms.update(s for s in (ln.strip() for ln in lines) if len(s) >= MIN_LINE_LEN)
        out.update(f for f in forms if len(f) >= contract.MIN_SECRET_LEN)
    return sorted(out, key=lambda s: (-len(s), s))


def _mask_userinfo(text: str, mask: str) -> str:
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{mask}@", text)


def redact_text(text: str, values: list[str], mask: str = "***") -> str:
    """``values`` 의 모든 출현(``expand_values`` 의 표기 포함)을 ``mask`` 로 바꾸고(긴 값부터 — 짧은 값이 긴 값의 일부를 먼저 지우면
    나머지가 남는다) URL 사용자 정보를 가린다.

    Replace every occurrence of each value and of its output spellings (longest first) and mask URL userinfo
    (`https://u:p@h` → `https://***@h`)."""
    for v in expand_values(values):
        if v in text:
            text = text.replace(v, mask)
    return _mask_userinfo(text, mask)


class Redactor:
    """미리 펼쳐 둔 비밀 값으로 문자열 하나(로그 한 줄)를 지우는 호출 가능 객체 — 줄마다 값을 다시 펼치지 않으려고 둔다.
    ``source`` 가 지울 값을 돌려준다. ``refresh_s`` 를 주면 그 간격으로(그리고 ``refresh()`` 를 부를 때) 다시 읽는다: 실행 중에
    ``.env`` 를 읽거나 토큰을 새로 받으면 값이 늘어난다. ``source`` 가 예외를 내면 호출도 예외를 낸다 — 부르는 쪽
    (``sbmig.core.scrub.scrub``, ``line``)이 그 줄을 버린다(원문을 내보내지 않는다).

    A callable that redacts one string with pre-expanded values. `source` yields the values; with `refresh_s` they are
    re-read at that interval and on `refresh()`. A failing `source` raises: the callers withhold the line."""

    def __init__(self, source: Callable[[], Iterable[str]], *, refresh_s: float | None = None, mask: str = "***") -> None:
        self._source, self._refresh_s, self.mask = source, refresh_s, mask
        self._lock = threading.Lock()
        self._key: frozenset[str] | None = None
        self._values: tuple[str, ...] = ()
        self._next = 0.0
        self.refresh()

    @property
    def values(self) -> tuple[str, ...]:
        """지금 지우는 값들(펼친 표기 포함, 긴 것부터). / The values redacted right now, longest first."""
        return self._values

    def refresh(self) -> None:
        """``source`` 를 다시 읽는다(값이 그대로면 펼치기를 건너뛴다). / Re-read `source`."""
        raw = frozenset(v for v in self._source() if v)
        with self._lock:
            if raw != self._key:
                self._key, self._values = raw, tuple(expand_values(raw))
            if self._refresh_s is not None:
                self._next = time.monotonic() + self._refresh_s

    def __call__(self, text: str) -> str:
        if self._refresh_s is not None and time.monotonic() >= self._next:
            self.refresh()
        for v in self._values:
            if v in text:
                text = text.replace(v, self.mask)
        if "://" in text and "@" in text:          # 정규식은 URL 사용자 정보가 있을 수 있는 줄에만
            text = _mask_userinfo(text, self.mask)
        return text

    def line(self, chunk: bytes) -> bytes:
        """출력 한 줄(bytes)을 지운다. 바뀐 것이 없으면 받은 bytes 그대로(UTF-8 이 아닌 바이트를 건드리지 않는다), 지우다 실패하면
        원문 대신 ``scrub.WITHHELD`` 한 줄. 예외 없음.

        Redact one output line (bytes): the same bytes when nothing changed, the `WITHHELD` line when redaction failed."""
        try:
            text = chunk.decode("utf-8", errors="replace")
            new = self(text)
        except Exception:  # noqa: BLE001 — 실패해도 원문은 내보내지 않는다
            return (scrub.WITHHELD + "\n").encode("utf-8")
        return chunk if new == text else new.encode("utf-8", errors="replace")


def live_secret_values() -> list[str]:
    """지금 이 프로세스가 아는 비밀 값: 환경 변수(``contract.secret_values``) + 실행 중에 알려진 것(``scrub.note_secret``).

    The secrets this process knows now: the environment's plus the ones registered during the run."""
    noted = [v for v in scrub.noted_secrets() if len(v) >= contract.MIN_SECRET_LEN]
    return contract.secret_values() + noted


def install_output_redaction() -> Redactor:
    """이 프로세스의 로그·저널 출력에 정제기를 건다(이미 걸려 있어도 새로 건다 — 값의 출처는 같다). 그 뒤로 모든 로그 줄과
    ``Workspace.journal`` 줄에서 ``live_secret_values`` 의 값이 쓰는 순간 지워진다. 건 ``Redactor`` 를 돌려준다.

    Hook a live `Redactor` into this process's log / journal output (`sbmig.core.scrub`); returns it."""
    redactor = Redactor(live_secret_values, refresh_s=LIVE_REFRESH_S)
    scrub.set_output_scrubber(redactor, on_refresh=redactor.refresh)
    return redactor


def redact_file(path: Path, values: list[str], mask: str = "***") -> bool:
    """파일 하나를 제자리에서 지운다(utf-8, 깨진 바이트는 대체). 바뀌었을 때만 쓰고 바뀌었는지 돌려준다; OSError 는 삼킨다(debug 로그).

    Redact one file in place; returns whether it changed; never raises on OSError."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:          # a binary file: decoding with "replace" and writing back would corrupt it
            return False
        text = raw.decode("utf-8", errors="replace")
        new = redact_text(text, values, mask)
        if new == text:
            return False
        path.write_text(new, encoding="utf-8", errors="replace")
        return True
    except OSError as e:
        log.debug("redact: cannot rewrite %s: %s", path, e)
        return False


def _targets(run_dir: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for p in sorted(run_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                out.append(p)
        logs = run_dir / "logs"
        if logs.is_dir():
            out.extend(p for p in sorted(logs.rglob("*")) if p.is_file())
    except OSError as e:
        log.debug("redact: cannot list %s: %s", run_dir, e)
    return out


def redact_run_dir(run_dir: Path, values: list[str] | None = None) -> int:
    """실행 폴더의 텍스트 출력(최상위 텍스트 파일 + ``logs/``)에서 비밀을 지우고 바뀐 파일 수를 돌려준다. ``values=None`` 이면
    ``live_secret_values()``(지금 프로세스의 환경 + 실행 중에 알려진 값). 지울 값이 없으면 파일을 읽지도 않고 곧바로 0 — 비밀 env 가 없는 실행에서 큰
    로그를 훑는 비용을 들이지 않으려고.

    Redact the run dir's text outputs; `values=None` → the secret values of `os.environ`. Returns the changed-file count
    (0 at once when there is nothing to redact)."""
    vals = live_secret_values() if values is None else [v for v in values if v]
    if not vals:
        return 0
    return sum(1 for p in _targets(Path(run_dir)) if redact_file(p, vals))
