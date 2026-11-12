"""실행 폴더의 텍스트 출력에서 비밀 값을 마스킹하는 모듈.
Worker가 끝날 때 공유 볼륨에 남는 로그와 보고서가 대상이다.
비밀로 보이는 환경 변수(``contract.secret_values``)의 값을 마스킹 문자열로 바꾸고, URL의 사용자
정보(``https://user:pass@host``)도 가린다.
- 대상은 실행 폴더 최상위의 텍스트 파일과 ``logs/``, ``remediation/`` 아래 텍스트 파일이다.
  ``remediation/``의 ``accepted/``, ``rounds/<n>/candidate/``, ``original/``은 제외한다
- ``control/``, ZIP, ``project/``는 건드리지 않는다. 바뀐 파일만 임시 파일에 쓴 뒤 원자적으로 교체한다
- 실행 중의 출력도 같은 규칙으로 기록할 때 마스킹한다. ``SBMIG_REDACT_OUTPUT=1``이면
  ``install_output_redaction``이 ``Redactor``를 로그와 저널 출력(``sbmig.core.scrub``)에 설치한다
- 비밀 값은 JSON 이스케이프, 퍼센트 인코딩, 줄 단위 출력처럼 다른 표기로도 나타나므로 ``expand_values``가 그
  표기까지 만들어 함께 가린다

Redact secret values (and URL userinfo) from a run directory's text outputs: top-level text files, `logs/` and the text
files of a chained remediation under `remediation/` (not its project trees `accepted/` / `rounds/<n>/candidate/` nor
`original/`); never `control/`, the zips or `project/`. A file is rewritten only when something changed. Review 2026-11-10: the same rule
also runs at write time — `Redactor` (pre-expanded values, one line at a time) and `install_output_redaction` (hooks it
into this process's log / journal output); `expand_values` adds the spellings a secret takes in output."""
from __future__ import annotations

import json
import logging
import os
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
# 이관에 이어 실행한 취약점 조치의 폴더이며 sbmig.remediations.chain의 CHAIN_DIR와 같은 이름이어야 한다
REMEDIATION_DIR = "remediation"
REMEDIATION_SUFFIXES = TEXT_SUFFIXES | {".patch"}
REMEDIATION_SKIP_TOP = frozenset({"accepted", "original"})     # 프로젝트 트리와 원본(읽기 전용 ZIP + manifest)은 마스킹하지 않는다
# scheme://userinfo@ — userinfo never holds "/", "@" or whitespace; any scheme (https, git+ssh, jdbc:…:// …)
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^\s/@:'\"<>]+(?::[^\s/@'\"<>]*)?)@")
# 여러 줄짜리 비밀(PEM 키, JSON 자격 증명)은 줄마다 따로 출력될 수 있다.
# "}" 같은 짧은 줄 때문에 로그 전체가 지워지지 않도록 이 길이 이상인 줄만 따로 마스킹한다
MIN_LINE_LEN = 2 * contract.MIN_SECRET_LEN
LIVE_REFRESH_S = 1.0               # 실행 중 마스킹에서 비밀 값 목록을 다시 읽는 간격(초)


def expand_values(values: Iterable[str]) -> list[str]:
    """마스킹할 값을 출력에 나타날 수 있는 표기까지 펼쳐서 긴 것부터 중복 없이 반환한다.
    원래 값, 앞뒤 공백을 뗀 값, JSON 이스케이프, URL 퍼센트 인코딩, ``repr`` 표기, 여러 줄 값의 각
    줄(``MIN_LINE_LEN`` 이상)을 포함한다.
    펼친 표기가 ``contract.MIN_SECRET_LEN``보다 짧으면 버리지만, 받은 값 자체는 길이와 상관없이 남긴다.

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
    """텍스트에서 ``values``와 그 출력 표기를 모두 ``mask``로 바꾸고 URL 사용자 정보를 가린다.
    짧은 값이 긴 값의 일부만 지우면 나머지가 남으므로 긴 값부터 바꾼다.

    Replace every occurrence of each value and of its output spellings (longest first) and mask URL userinfo
    (`https://u:p@h` → `https://***@h`)."""
    for v in expand_values(values):
        if v in text:
            text = text.replace(v, mask)
    return _mask_userinfo(text, mask)


class Redactor:
    """미리 펼쳐 둔 비밀 값으로 로그 한 줄씩 마스킹하는 호출 가능 클래스.
    줄마다 값을 다시 펼치지 않기 위해 둔다.
    ``refresh_s``를 주면 그 간격마다, 그리고 ``refresh()``를 호출할 때 ``source``를 다시 읽는다.
    실행 중에 토큰을 새로 받으면 비밀 값이 늘어나기 때문이다.
    ``source``가 예외를 던지면 호출도 예외를 던지며, 호출하는 쪽이 그 줄을 버린다.

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
        """현재 마스킹하는 값 목록을 펼친 표기까지 포함해 긴 것부터 반환한다."""
        return self._values

    def refresh(self) -> None:
        """``source``에서 비밀 값을 다시 읽는다. 값이 바뀌지 않았으면 펼치기를 건너뛴다."""
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
        if "://" in text and "@" in text:          # URL 사용자 정보가 있을 수 있는 줄에만 정규식을 적용한다
            text = _mask_userinfo(text, self.mask)
        return text

    def line(self, chunk: bytes) -> bytes:
        """출력 한 줄(bytes)을 마스킹한다. 바뀐 것이 없으면 받은 bytes를 그대로 반환하고, 마스킹에 실패하면
        원문 대신 ``scrub.WITHHELD`` 한 줄을 반환한다. 예외를 던지지 않는다.

        Redact one output line (bytes): the same bytes when nothing changed, the `WITHHELD` line when redaction failed."""
        try:
            text = chunk.decode("utf-8", errors="replace")
            new = self(text)
        except Exception:  # noqa: BLE001 - 마스킹에 실패해도 원문은 내보내지 않는다
            return (scrub.WITHHELD + "\n").encode("utf-8")
        return chunk if new == text else new.encode("utf-8", errors="replace")


def live_secret_values() -> list[str]:
    """이 프로세스가 지금 알고 있는 비밀 값을 반환한다.
    환경 변수(``contract.secret_values``)와 실행 중에 등록된 값(``scrub.note_secret``)을 합친다.

    The secrets this process knows now: the environment's plus the ones registered during the run."""
    noted = [v for v in scrub.noted_secrets() if len(v) >= contract.MIN_SECRET_LEN]
    return contract.secret_values() + noted


def install_output_redaction() -> Redactor:
    """이 프로세스의 로그와 저널 출력에 마스킹을 설치하고, 설치한 ``Redactor``를 반환한다.
    이미 설치되어 있어도 새로 설치한다.
    이후 모든 로그 줄과 ``Workspace.journal`` 줄에서 ``live_secret_values``의 값이 기록될 때 마스킹된다.

    Hook a live `Redactor` into this process's log / journal output (`sbmig.core.scrub`); returns it."""
    redactor = Redactor(live_secret_values, refresh_s=LIVE_REFRESH_S)
    scrub.set_output_scrubber(redactor, on_refresh=redactor.refresh)
    return redactor


def redact_file(path: Path, values: list[str], mask: str = "***") -> bool:
    """파일 하나를 제자리에서 마스킹하고 바뀌었는지 여부를 반환한다.
    바뀌었을 때만 다시 쓰며, OSError는 debug 로그만 남기고 무시한다.

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
        # 웹 파드가 같은 순간 state.json을 읽어도 반쯤 쓰인 JSON을 보지 않도록 임시 파일에 쓰고 os.replace로
        # 바꾼다. 권한 비트는 원래 파일의 것을 쓴다
        tmp = path.with_name(f".{path.name}.{os.getpid()}.redact.tmp")
        try:
            tmp.write_text(new, encoding="utf-8", errors="replace")
            try:
                os.chmod(tmp, path.stat().st_mode & 0o7777)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return True
    except OSError as e:
        log.debug("redact: cannot rewrite %s: %s", path, e)
        return False


def _remediation_targets(root: Path) -> list[Path]:
    # 취약점 조치는 트리 해시로 승인을 확인하므로 텍스트 출력만 마스킹하고
    # 프로젝트 트리와 원본(original/)은 건드리지 않는다
    # ZIP은 확장자로 제외되고, 심볼릭 링크는 따라가지 않는다
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel = here.relative_to(root).parts
        dirnames[:] = sorted(d for d in dirnames
                             if not (not rel and d in REMEDIATION_SKIP_TOP)
                             and not (len(rel) == 2 and rel[0] == "rounds" and d == "candidate"))
        for name in sorted(filenames):
            p = here / name
            if p.suffix.lower() in REMEDIATION_SUFFIXES and not p.is_symlink() and p.is_file():
                out.append(p)
    return out


def _targets(run_dir: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for p in sorted(run_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
                out.append(p)
        logs = run_dir / "logs"
        if logs.is_dir():
            out.extend(p for p in sorted(logs.rglob("*")) if p.is_file())
        rem = run_dir / REMEDIATION_DIR
        if rem.is_dir() and not rem.is_symlink():
            out.extend(_remediation_targets(rem))
    except OSError as e:
        log.debug("redact: cannot list %s: %s", run_dir, e)
    return out


def redact_run_dir(run_dir: Path, values: list[str] | None = None) -> int:
    """실행 폴더의 텍스트 출력에서 비밀 값을 마스킹하고 바뀐 파일 수를 반환한다.
    ``values``가 None이면 ``live_secret_values()``를 쓴다.
    마스킹할 값이 없으면 큰 로그를 읽지 않고 바로 0을 반환한다.

    Redact the run dir's text outputs; `values=None` → the secret values of `os.environ`. Returns the changed-file count
    (0 at once when there is nothing to redact)."""
    vals = live_secret_values() if values is None else [v for v in values if v]
    if not vals:
        return 0
    return sum(1 for p in _targets(Path(run_dir)) if redact_file(p, vals))
