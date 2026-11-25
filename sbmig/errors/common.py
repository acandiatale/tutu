"""케이스 ID 계산, 저장할 텍스트 정리, 원자적 파일 쓰기처럼 오류 목록과 KB가 함께 쓰는 함수를 모은 모듈.
``sbmig.errors.entries``, ``sbmig.errors.cases``, ``sbmig.kb.store``가 사용한다.
- L2 서명에는 폴더 이름에 쓸 수 없는 문자가 있으므로 L2의 해시를 케이스 ID로 쓴다
- 케이스 ID 계산식은 트리아지(``sbmig.triage.cluster``)와 같아야 한다. 다르면 클러스터와 오류 목록의 케이스가 어긋난다

Spec: docs/v2/PLAN-v2.1.md §3.3 / §3.4.

* case_id_for: ``sha1(l2)[:10]`` ("" without an L2), the same formula as sbmig.triage.cluster.l2_case_id.
* case_id_of: the L2 id, else ``sha1(kind|file|L1)[:10]``, the same fallback as sbmig.triage.cluster.case_id_of,
  so a cluster's ``case_id`` and the error list agree. Change both modules or neither.
* entry_id: ``sha1(run_id|case_id|file|line|message)[:12]``, stable for the same inputs.
* Cleaner: what every stored text goes through (``kb.redact_file`` rules, sbmig.kb.redact).
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from sbmig.core.models import BuildError, ErrorKind

_REPLACE_TRIES = 6


def posix(path: Optional[str]) -> str:
    """경로 구분자를 ``/``로 통일하고 앞의 ``./``를 제거한다. None이면 빈 문자열을 반환한다."""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def case_id_for(l2: Optional[str]) -> str:
    """L2 서명으로 KB 케이스 ID ``sha1(l2)[:10]``을 계산한다. L2가 없으면 빈 문자열을 반환한다."""
    if not l2:
        return ""
    return hashlib.sha1(l2.encode("utf-8")).hexdigest()[:10]


def case_id_of(e: BuildError) -> str:
    """빌드 오류 하나의 케이스 ID를 계산한다. L2가 없으면 ``sha1(kind|file|L1)[:10]``을 쓴다.

    L1 is ``e.signature``, else ``kind|message``, exactly as in sbmig.triage.cluster.case_id_of."""
    cid = case_id_for(e.signature_generic)
    if cid:
        return cid
    kind = e.kind.value if isinstance(e.kind, ErrorKind) else str(e.kind)
    key = "|".join((kind, posix(e.file), e.signature or f"{kind}|{e.message}"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def entry_id(run_id: str, case_id: str, file: Optional[str], line: Optional[int], message: str) -> str:
    """오류 목록 항목 ID ``sha1(run_id|case_id|file|line|message)[:12]``를 계산한다.
    입력이 같으면 ID도 같으므로 KB에 다시 기록할 때 이미 있는 항목을 이 ID로 걸러낸다."""
    key = "|".join((run_id or "", case_id or "", posix(file), str(line or 0), message or ""))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def first_line(text: Optional[str], limit: int = 300) -> str:
    """비어 있지 않은 첫 줄의 공백을 정리하고 ``limit``자로 잘라 반환한다."""
    for ln in (text or "").splitlines():
        s = " ".join(ln.split())
        if s:
            return s[:limit]
    return ""


def note_text(n: Any, limit: int = 0) -> str:
    """수정 기록(``EditNote``) 하나를 경로, 줄 범위, 위치, 변경 내용을 담은 한 줄로 만든다.
    errors.md, report.md, 로그가 모두 이 형식을 쓴다. ``limit``이 0이면 자르지 않는다.

    Shape: ``module/comp-jar/pom.xml +42–47 (<dependencies> 블록) — <change>``; ``(where)`` only when the LLM gave one,
    whitespace collapsed, a capped result ends with "…". Duck-typed: a dict or any object with ``path`` / ``where`` /
    ``change`` / ``lines``."""
    def get(k: str) -> str:
        v = n.get(k, "") if isinstance(n, dict) else getattr(n, k, "")
        return " ".join(str(v or "").split())
    head = " ".join(x for x in (get("path") or "-", get("lines")) if x)
    where, change = get("where"), get("change")
    s = head + (f" ({where})" if where else "") + (f" — {change}" if change else "")
    return s[: limit - 1] + "…" if limit and len(s) > limit else s


def _field(obj: Any, key: str, default: Any = "") -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _fenced(text: str, fence: str) -> list[str]:
    """``text``를 코드 펜스로 감싼다. 코드 안에 펜스만큼 긴 백틱이 있으면 펜스를 더 길게 만든다."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0) if fence.startswith("`") else 0
    f = fence if longest < len(fence) else "`" * (longest + 1)
    return [f, *text.split("\n"), f]


def note_block(n: Any, *, fence: str = "```") -> list[str]:
    """수정 기록 한 줄 뒤에 변경 부분마다 ``as-is`` / ``to-be`` 코드 블록을 붙여 반환한다.
    ``sbmig kb errors show``와 ``cases show``가 사용한다. 예외를 던지지 않는다.

    Per hunk: ``  as-is (<lines>):`` + the fenced ``before``, then ``  to-be (<lines>):`` + the fenced ``after``; an
    empty side is left out, a hunk cut by ``errors.snippet_lines`` adds ``  (cut: errors.snippet_lines)``.
    errors.md, report.md and the log keep the note_text line only. Hunks may be dicts or objects; an odd one is
    skipped."""
    out = [note_text(n)]
    try:
        hunks = list(_field(n, "hunks", None) or [])
    except Exception:  # noqa: BLE001 - 목록이 아니면 수정 기록 한 줄만 반환한다
        hunks = []
    for h in hunks:
        try:
            lines = " ".join(str(_field(h, "lines") or "").split())
            label = f" ({lines})" if lines else ""
            before, after = str(_field(h, "before") or ""), str(_field(h, "after") or "")
            if before:
                out += [f"  as-is{label}:", *_fenced(before, fence)]
            if after:
                out += [f"  to-be{label}:", *_fenced(after, fence)]
            if _field(h, "truncated", False) is True:
                out.append("  (cut: errors.snippet_lines)")
        except Exception:  # noqa: BLE001, S112 - 형식이 맞지 않는 변경 부분은 건너뛴다
            continue
    return out


def cap(text: Optional[str], limit: int) -> str:
    """``text``를 ``limit``자로 자르고 잘린 문자 수를 뒤에 붙인다. ``limit``이 0이면 자르지 않는다."""
    t = text or ""
    if limit and limit > 0 and len(t) > limit:
        return t[:limit] + f"\n…[{len(t) - limit} more characters]"
    return t


def _root_pairs(roots: Iterable[Any]) -> list[tuple[str, str]]:
    """텍스트에서 바꿀 경로 쌍을 만든다. 작업 루트는 빈 문자열로, 사용자 홈은 ``~``로 바꾼다."""
    found: set[str] = set()
    for r in roots:
        if not r:
            continue
        s = str(r)
        found |= {s, s.replace("\\", "/")}
        # macOS는 /var, /tmp가 /private/ 아래로 풀리므로 두 표기를 모두 지운다
        if s.startswith("/private/"):
            found.add(s[len("/private"):])
    # 실행 디렉토리가 다른 루트 아래에 있어도 통째로 지워지도록 긴 경로부터 바꾼다
    out = [(r, "") for r in sorted((r for r in found if len(r) > 1), key=len, reverse=True)]
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - 홈 디렉토리를 알 수 없는 환경
        home = ""
    if len(home) > 3:                                     # 홈 경로에는 운영자 이름이 들어 있으므로 남기지 않는다
        out += [(home, "~"), (home.replace("\\", "/"), "~")]
    return out


class Cleaner:
    """오류 목록과 KB에 저장할 텍스트에서 비밀 값을 마스킹하고 작업 루트와 홈 경로를 지우는 클래스."""

    def __init__(self, cfg: Any = None, roots: Iterable[Any] = ()):
        from sbmig.kb.redact import get_redactor

        self.red = get_redactor(cfg)
        self.pairs = _root_pairs(roots)

    def strip(self, text: Optional[str]) -> str:
        """텍스트에서 작업 루트와 홈 경로만 지운다. 비밀 값은 마스킹하지 않는다."""
        t = text or ""
        for needle, repl in self.pairs:
            if repl:
                t = t.replace(needle, repl)
            else:
                t = t.replace(needle + "/", "").replace(needle + "\\", "").replace(needle, "")
        return t

    def __call__(self, text: Optional[str], limit: int = 0) -> str:
        """텍스트의 경로를 지우고 비밀 값을 마스킹한 뒤 ``limit``자로 자른다."""
        if not text:
            return ""
        return cap(self.red.redact(self.strip(text)) or "", limit)

    def has_secret(self, text: Optional[str]) -> bool:
        """``text``에 마스킹할 비밀 값이 들어 있는지 확인한다."""
        return bool(text) and bool(self.red.redact_ex(text)[1])


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """같은 디렉토리의 임시 파일에 쓴 뒤 ``os.replace``로 바꿔 넣어 반쯤 쓴 파일이 남지 않게 한다.
    대상 파일이 잠겨 있으면 간격을 늘리며 다시 시도하고, 끝내 실패하면 ``PermissionError``를 던진다.

    PermissionError is how Windows reports a scanner or editor holding the target; other errors are not retried.
    The temp file is removed on every failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        delay = 0.05
        for i in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == _REPLACE_TRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write(path: Path, text: str) -> None:
    """UTF-8 텍스트를 :func:`atomic_write_bytes`로 쓴다. 줄바꿈은 바꾸지 않는다."""
    atomic_write_bytes(Path(path), text.encode("utf-8"))
