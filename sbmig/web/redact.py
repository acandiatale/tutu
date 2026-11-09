"""실행 폴더의 텍스트 출력에서 비밀 값을 지운다 (v2.1z-ac, 2026-11-09 — Worker 가 끝날 때 공유 볼륨에 남는 로그·보고서용).

비밀로 보이는 환경 변수(``contract.secret_values``: 이름에 TOKEN / PASSWORD / SECRET / API_KEY …, 6자 이상)의 값을 ``***`` 로 바꾸고, URL 의
사용자 정보(``https://user:pass@host``)도 가린다. 손대는 곳은 실행 폴더 최상위의 텍스트 파일(``*.md`` / ``*.json`` / ``*.jsonl`` / ``*.log`` /
``*.txt`` / ``*.yml``)과 ``logs/`` 아래뿐이다 — ``control/``(Main 의 파일), ``source.zip`` / ``result.zip``, ``project/``(이관된 소스)는 건드리지
않는다. 바뀐 것이 있을 때만 다시 쓴다.

Redact secret values (and URL userinfo) from a run directory's text outputs: top-level text files and `logs/`; never
`control/`, the zips or `project/`. A file is rewritten only when something changed."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from sbmig.web import contract

log = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".md", ".json", ".jsonl", ".log", ".txt", ".yml"})
# scheme://userinfo@ — userinfo never holds "/", "@" or whitespace; any scheme (https, git+ssh, jdbc:…:// …)
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^\s/@:'\"<>]+(?::[^\s/@'\"<>]*)?)@")


def redact_text(text: str, values: list[str], mask: str = "***") -> str:
    """``values`` 의 모든 출현을 ``mask`` 로 바꾸고(긴 값부터 — 짧은 값이 긴 값의 일부를 먼저 지우면 나머지가 남는다) URL 사용자 정보를 가린다.

    Replace every occurrence of each value (longest first) and mask URL userinfo (`https://u:p@h` → `https://***@h`)."""
    for v in sorted((v for v in values if v), key=len, reverse=True):
        if v in text:
            text = text.replace(v, mask)
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{mask}@", text)


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
    ``contract.secret_values()``(지금 프로세스의 환경). 지울 값이 없으면 파일을 읽지도 않고 곧바로 0 — 비밀 env 가 없는 실행에서 큰
    로그를 훑는 비용을 들이지 않으려고.

    Redact the run dir's text outputs; `values=None` → the secret values of `os.environ`. Returns the changed-file count
    (0 at once when there is nothing to redact)."""
    vals = contract.secret_values() if values is None else [v for v in values if v]
    if not vals:
        return 0
    return sum(1 for p in _targets(Path(run_dir)) if redact_file(p, vals))
