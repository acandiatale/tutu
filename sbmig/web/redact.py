"""실행 폴더의 텍스트 출력에서 비밀 값을 마스킹하는 모듈.
비밀로 보이는 환경 변수의 값과 URL의 사용자 정보(``https://user:pass@host``)를 가린다.

Targets: the text files at the top of the run folder and the text files under ``logs/`` and
``remediation/``. Left out: ``accepted/``, ``rounds/<n>/candidate/`` and ``original/`` of ``remediation/``,
because the remediation checks them by their tree hash; ``control/``, ZIPs and ``project/`` are never
touched. Only a changed file is rewritten, through a temp file and an atomic replace. With
``SBMIG_REDACT_OUTPUT=1``, ``install_output_redaction`` puts the same rules on the log and journal output,
so lines are masked as they are written. In ``.json`` and ``.jsonl`` files only string values change; keys,
numbers and the structure stay.

`Redactor` = pre-expanded values, one line at a time; `expand_values` adds the spellings a secret takes in
output."""
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
# 이관에 이어 실행한 취약점 조치의 폴더. sbmig.remediations.chain의 CHAIN_DIR와 같은 이름이어야 한다
REMEDIATION_DIR = "remediation"
REMEDIATION_SUFFIXES = TEXT_SUFFIXES | {".patch"}
REMEDIATION_SKIP_TOP = frozenset({"accepted", "original"})     # 프로젝트 파일과 원본(ZIP, manifest)은 마스킹하지 않는다
# scheme://userinfo@ 형태. userinfo에는 "/", "@", 공백이 없다. scheme은 무엇이든 된다(https, git+ssh, jdbc:…:// 등)
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^\s/@:'\"<>]+(?::[^\s/@'\"<>]*)?)@")
# 여러 줄짜리 비밀(PEM 키, JSON 자격 증명)은 줄마다 따로 출력될 수 있다.
# 이 길이 이상인 줄만 따로 마스킹한다. "}" 같은 짧은 줄까지 가리면 로그 전체가 지워진다
MIN_LINE_LEN = 2 * contract.MIN_SECRET_LEN
LIVE_REFRESH_S = 1.0               # 실행 중 마스킹에서 비밀 값 목록을 다시 읽는 간격, 초


def expand_values(values: Iterable[str]) -> list[str]:
    """마스킹할 값을 출력에 나올 수 있는 표기까지 펼쳐서 반환한다. 긴 것부터, 중복 없이 반환한다.
    - 원래 값, 앞뒤 공백을 뗀 값, JSON 이스케이프, URL 퍼센트 인코딩, ``repr`` 표기를 넣는다
    - 여러 줄 값은 ``MIN_LINE_LEN`` 이상인 각 줄도 넣는다
    - 펼친 표기가 ``contract.MIN_SECRET_LEN``보다 짧으면 버린다. 받은 값 자체는 길이와 상관없이 남긴다"""
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


def _token_pattern(value: str) -> re.Pattern[str]:
    # 짧은 값은 앞뒤에 영문자나 숫자가 붙지 않은 토큰일 때만 바꾼다. 날짜, 해시, 숫자 안에서 우연히 맞는 경우를 피한다.
    # 숫자 사이의 - . : 는 날짜, 시각, 버전을 잇는 문자로 본다
    return re.compile(r"(?<![A-Za-z0-9])(?<![0-9][-.:])" + re.escape(value) + r"(?![A-Za-z0-9])(?![-.:][0-9])")


def _compile(values: Iterable[str]) -> tuple[tuple[str, re.Pattern[str] | None], ...]:
    # 펼친 값마다 바꾸는 방법을 정한다. MIN_SECRET_LEN 이상은 문자열 그대로, 짧은 값은 토큰 정규식으로 바꾼다
    return tuple((v, _token_pattern(v) if len(v) < contract.MIN_SECRET_LEN else None) for v in values)


def _replace(text: str, compiled: Iterable[tuple[str, re.Pattern[str] | None]], mask: str) -> str:
    for v, pattern in compiled:
        if v in text:
            text = text.replace(v, mask) if pattern is None else pattern.sub(mask, text)
    return text


def redact_text(text: str, values: list[str], mask: str = "***") -> str:
    """텍스트에서 ``values``와 그 출력 표기를 모두 ``mask``로 바꾼다. URL 사용자 정보도 가린다.
    긴 값부터 바꾼다. 짧은 값이 긴 값의 일부만 지우면 나머지가 남기 때문이다.
    ``contract.MIN_SECRET_LEN``보다 짧은 값(4자 IVAS 사용자 id 등)은 하나의 토큰일 때만 바꾼다.

    `https://u:p@h` -> `https://***@h`."""
    return _mask_userinfo(_replace(text, _compile(expand_values(values)), mask), mask)


# JSON 문자열 리터럴 하나. 올바른 JSON에서 큰따옴표는 문자열 경계에만 쓰인다. 그래서 왼쪽부터 찾으면 정확하다
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


def redact_json_text(text: str, values: list[str], mask: str = "***") -> str | None:
    """JSON 텍스트의 문자열 값 안에서만 비밀 값을 바꾼다. 키, 숫자, 구조와 나머지 바이트는 그대로 둔다.
    JSON으로 읽히지 않으면 None을 반환한다. 그때는 호출한 쪽이 일반 텍스트로 마스킹한다.

    A plain substring pass over `report.json` would replace a short id inside numbers and hashes and leave the
    file unparsable."""
    try:
        json.loads(text)
    except ValueError:
        return None
    compiled = _compile(expand_values(values))
    out: list[str] = []
    pos = 0
    for m in _JSON_STRING.finditer(text):
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == ":":
            continue                                   # 키는 바꾸지 않는다
        raw = m.group(0)
        value = json.loads(raw)
        new = _mask_userinfo(_replace(value, compiled, mask), mask)
        if new != value:
            out.append(text[pos:m.start()])
            out.append(json.dumps(new, ensure_ascii=not _has_non_ascii(raw)))
            pos = m.end()
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _has_non_ascii(raw: str) -> bool:
    return any(ord(ch) > 127 for ch in raw)


def _redact_structured(path: Path, text: str, values: list[str], mask: str) -> str:
    # .json은 파일 전체를, .jsonl은 줄마다 JSON으로 읽어 문자열 값 안에서만 바꾼다. 읽히지 않으면 텍스트로 바꾼다
    suffix = path.suffix.lower()
    if suffix == ".json":
        new = redact_json_text(text, values, mask)
        return redact_text(text, values, mask) if new is None else new
    if suffix == ".jsonl":
        parts: list[str] = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            end = line[len(body):]
            new = redact_json_text(body, values, mask) if body.strip() else body
            parts.append((redact_text(body, values, mask) if new is None else new) + end)
        return "".join(parts)
    return redact_text(text, values, mask)


class Redactor:
    """미리 펼쳐 둔 비밀 값으로 로그를 한 줄씩 마스킹하는 호출 가능 클래스. 줄마다 값을 다시 펼치지 않는다.
    ``refresh_s``를 주면 그 간격마다 ``source``를 다시 읽는다. 실행 중에 새 토큰을 받으면 비밀 값이 늘기 때문이다.
    ``source``가 예외를 던지면 호출도 예외를 던진다. 그 줄은 호출하는 쪽이 내보내지 않는다."""

    def __init__(self, source: Callable[[], Iterable[str]], *, refresh_s: float | None = None, mask: str = "***") -> None:
        self._source, self._refresh_s, self.mask = source, refresh_s, mask
        self._lock = threading.Lock()
        self._key: frozenset[str] | None = None
        self._values: tuple[str, ...] = ()
        self._compiled: tuple[tuple[str, re.Pattern[str] | None], ...] = ()
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
                self._compiled = _compile(self._values)
            if self._refresh_s is not None:
                self._next = time.monotonic() + self._refresh_s

    def __call__(self, text: str) -> str:
        if self._refresh_s is not None and time.monotonic() >= self._next:
            self.refresh()
        # 짧은 값은 하나의 토큰일 때만 바꾼다(redact_text와 같은 규칙)
        text = _replace(text, self._compiled, self.mask)
        if "://" in text and "@" in text:          # URL 사용자 정보가 있을 수 있는 줄에만 정규식을 적용한다
            text = _mask_userinfo(text, self.mask)
        return text

    def line(self, chunk: bytes) -> bytes:
        """출력 한 줄(bytes)을 마스킹한다. 바뀐 것이 없으면 받은 bytes를 그대로 반환한다.
        마스킹에 실패하면 원문 대신 ``scrub.WITHHELD`` 한 줄을 반환한다. 예외를 던지지 않는다."""
        try:
            text = chunk.decode("utf-8", errors="replace")
            new = self(text)
        except Exception:  # noqa: BLE001 - 마스킹에 실패해도 원문은 내보내지 않는다
            return (scrub.WITHHELD + "\n").encode("utf-8")
        return chunk if new == text else new.encode("utf-8", errors="replace")


def live_secret_values() -> list[str]:
    """이 프로세스가 지금 아는 비밀 값을 반환한다.
    환경 변수의 값(``contract.secret_values``)에 실행 중에 등록된 값(``scrub.note_secret``)을 더한다."""
    noted = [v for v in scrub.noted_secrets() if len(v) >= contract.MIN_SECRET_LEN]
    return contract.secret_values() + noted


def install_output_redaction() -> Redactor:
    """이 프로세스의 로그와 저널 출력(``sbmig.core.scrub``)에 마스킹을 설치하고 그 ``Redactor``를 반환한다.
    이미 설치되어 있어도 새로 설치한다. 이후 모든 로그 줄과 저널 줄은 기록될 때 마스킹된다."""
    redactor = Redactor(live_secret_values, refresh_s=LIVE_REFRESH_S)
    scrub.set_output_scrubber(redactor, on_refresh=redactor.refresh)
    return redactor


def redact_file(path: Path, values: list[str], mask: str = "***") -> bool:
    """파일 하나를 제자리에서 마스킹한다. 내용이 바뀌었으면 True를 반환한다.
    바뀌었을 때만 다시 쓴다. 바이너리 파일은 건너뛴다. OSError는 debug 로그만 남기고 False를 반환한다."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:          # 바이너리 파일을 replace로 읽고 다시 쓰면 망가진다
            return False
        text = raw.decode("utf-8", errors="replace")
        new = _redact_structured(path, text, values, mask)
        if new == text:
            return False
        # 임시 파일에 쓰고 os.replace로 바꾼다. 웹 파드가 같은 순간에 읽어도 반쯤 쓰인 파일을 보지 않는다.
        # 권한 비트는 원래 파일의 것을 쓴다
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
    # 텍스트 출력만 마스킹한다. 프로젝트 파일과 original/은 건드리지 않는다. 취약점 조치가 이 파일들의 해시로 결과를 확인한다.
    # ZIP은 확장자로 빠지고, 심볼릭 링크는 따라가지 않는다
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
    ``values``가 None이면 ``live_secret_values()``를 쓴다. 마스킹할 값이 없으면 파일을 읽지 않고 0이다."""
    vals = live_secret_values() if values is None else [v for v in values if v]
    if not vals:
        return 0
    return sum(1 for p in _targets(Path(run_dir)) if redact_file(p, vals))
