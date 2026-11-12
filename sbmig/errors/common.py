"""오류 목록과 KB가 함께 쓰는 도구 함수를 모은 모듈.
케이스 ID 계산, 텍스트 정리(비밀 값 마스킹, 경로 제거), 원자적 파일 쓰기를 제공한다.
``sbmig.errors.entries``, ``sbmig.errors.cases``, ``sbmig.kb.store``가 사용한다.
주요 함수: ``case_id_for``(sha1(L2)[:10]), ``case_id_of``(L2가 없으면 sha1(kind|file|L1)[:10]),
``entry_id``(sha1(run|case|file|line|message)[:12]), ``Cleaner``, ``atomic_write``, ``note_text``,
``note_block``.
L2 서명은 폴더 이름으로 쓸 수 없는 문자가 들어가므로 해시를 케이스 ID로 쓴다.
같은 L2는 어느 PC에서든 같은 폴더가 되므로 여러 프로젝트가 KB 하나를 함께 쓸 수 있다.
이 계산식은 트리아지(``sbmig.triage.cluster``)와 같아야 한다.
다르면 클러스터와 오류 목록의 케이스가 서로 어긋난다.

Small helpers of the v2.1 error list and KB (docs/v2/PLAN-v2.1.md §3.3 / §3.4): the ids, the text cleaner (secret
values masked, workspace roots and the home directory stripped) and atomic file writes.

* :func:`case_id_for` — the KB case of an L2 signature: ``sha1(l2)[:10]`` ("" without one). The same formula as the
  triage clusters (``sbmig.triage.cluster.l2_case_id``) and every KB since 2026-10-23.
* :func:`case_id_of` — the case of one build error: its L2 id, else ``sha1(kind|file|L1)[:10]`` (the triage's
  fallback for an error without an L2, so a cluster's ``case_id`` and the error list agree).
* :func:`entry_id` — one error-list entry: ``sha1(run_id|case_id|file|line|message)[:12]``.
* :class:`Cleaner` — what every stored text goes through (``kb.redact_file`` rules, :mod:`sbmig.kb.redact`).
* :func:`note_text` — one 수정 note as one line; :func:`note_block` (v2.1e) — the line plus its as-is / to-be code.


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
    """저장용으로 경로 구분자를 ``/``로 통일하고 앞의 ``./``를 제거한다. None이면 빈 문자열을 반환한다.

    A stored path: posix separators, no leading ``./``; "" for None."""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def case_id_for(l2: Optional[str]) -> str:
    """L2 서명으로 KB 케이스 ID ``sha1(l2)[:10]``을 계산한다. L2가 없으면 빈 문자열을 반환한다.

    The KB case id of an L2 signature (``signature_generic``): ``sha1(l2)[:10]``; "" when there is none."""
    if not l2:
        return ""
    return hashlib.sha1(l2.encode("utf-8")).hexdigest()[:10]


def case_id_of(e: BuildError) -> str:
    """빌드 오류 하나의 케이스 ID를 계산한다.
    L2가 있으면 그 ID를, 없으면 ``sha1(kind|file|L1)[:10]``을 쓴다.
    트리아지의 ``case_id_of``와 계산식이 같아야 클러스터와 오류 목록의 케이스가 일치한다.

    The case of a build error: its L2 id, or — no L2 — ``sha1(kind|file|L1)[:10]`` (L1 = the signature, else
    ``kind|message``), exactly like ``sbmig.triage.cluster.case_id_of``."""
    cid = case_id_for(e.signature_generic)
    if cid:
        return cid
    kind = e.kind.value if isinstance(e.kind, ErrorKind) else str(e.kind)
    key = "|".join((kind, posix(e.file), e.signature or f"{kind}|{e.message}"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def entry_id(run_id: str, case_id: str, file: Optional[str], line: Optional[int], message: str) -> str:
    """오류 목록 항목 ID ``sha1(run_id|case_id|file|line|message)[:12]``를 계산한다.
    KB에 다시 기록할 때 이미 있는 항목을 이 ID로 걸러낸다.

    ``sha1(run_id|case_id|file|line|message)[:12]`` — stable for the same inputs."""
    key = "|".join((run_id or "", case_id or "", posix(file), str(line or 0), message or ""))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def first_line(text: Optional[str], limit: int = 300) -> str:
    """비어 있지 않은 첫 줄을 찾아 공백을 정리하고 ``limit``자로 잘라 반환한다.

    The first non-empty line, whitespace collapsed, at most ``limit`` characters."""
    for ln in (text or "").splitlines():
        s = " ".join(ln.split())
        if s:
            return s[:limit]
    return ""


def note_text(n: Any, limit: int = 0) -> str:
    """수정 기록(``EditNote``) 하나를 경로, 줄 범위, 위치, 변경 내용을 담은 한 줄로 만든다.
    errors.md, report.md, 로그가 모두 이 형식을 쓴다. ``limit``이 0이면 자르지 않는다.

    One 수정 note (``EditNote``, v2.1b) as a person reads it — the one format of errors.md, report.md and the log:
    ``module/comp-jar/pom.xml +42–47 (<dependencies> 블록) — jakarta.xml.bind-api 와 jaxb-runtime 의존성을 추가합니다``
    (``(where)`` only when the LLM gave one; whitespace collapsed; ``limit`` caps the result, 0 = no cap). Duck-typed:
    a dict or any object with ``path`` / ``where`` / ``change`` / ``lines``."""
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
    """``text``를 코드 펜스로 감싼다. 코드 안에 ```가 있으면 더 긴 펜스를 쓴다.

    ``text`` between fences — a longer backtick fence when the code itself holds one."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0) if fence.startswith("`") else 0
    f = fence if longest < len(fence) else "`" * (longest + 1)
    return [f, *text.split("\n"), f]


def note_block(n: Any, *, fence: str = "```") -> list[str]:
    """수정 기록 한 줄과 변경 부분별 ``as-is`` / ``to-be`` 코드 블록을 만든다.
    ``sbmig kb errors show`` / ``cases show``가 사용한다.
    예외를 던지지 않는다.

    v2.1e: one 수정 note with its code — the :func:`note_text` line, then per hunk ``  as-is (<lines>):`` + the
    fenced ``before`` (skipped when empty) and ``  to-be (<lines>):`` + the fenced ``after`` (skipped when empty); a
    hunk cut by ``errors.snippet_lines`` adds ``  (cut: errors.snippet_lines)``. For ``sbmig kb errors show`` /
    ``cases show`` (errors.md / report.md / the log keep the one line). Duck-typed like ``note_text`` (a dict or an
    object; the hunks dicts or objects); never raises."""
    out = [note_text(n)]
    try:
        hunks = list(_field(n, "hunks", None) or [])
    except Exception:  # noqa: BLE001 - not a list: the line alone
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
        except Exception:  # noqa: BLE001, S112 - an odd hunk is skipped
            continue
    return out


def cap(text: Optional[str], limit: int) -> str:
    """``text``를 ``limit``자로 자르고 잘린 문자 수를 뒤에 표시한다. ``limit``이 0이면 자르지 않는다.

    ``text`` cut to ``limit`` characters (0 = no cap) with a marker."""
    t = text or ""
    if limit and limit > 0 and len(t) > limit:
        return t[:limit] + f"\n…[{len(t) - limit} more characters]"
    return t


def _root_pairs(roots: Iterable[Any]) -> list[tuple[str, str]]:
    """텍스트에서 바꿀 경로 쌍 목록을 만든다. 작업 루트는 빈 문자열로, 사용자 홈은 "~"로 바꾼다.
    운영자 이름이 들어간 홈 경로가 KB에 남지 않게 하기 위해서다.

    (needle, replacement): the workspace roots → "" (relative paths), the user's home → "~"."""
    found: set[str] = set()
    for r in roots:
        if not r:
            continue
        s = str(r)
        found |= {s, s.replace("\\", "/")}
        # macOS는 /var, /tmp가 /private/ 아래로 풀리므로 두 표기를 모두 지운다
        if s.startswith("/private/"):                     # macOS: /var/… and /tmp/… resolve to /private/…
            found.add(s[len("/private"):])
    # 실행 디렉토리가 다른 루트 아래에 있어도 통째로 지워지도록 긴 경로부터 바꾼다
    out = [(r, "") for r in sorted((r for r in found if len(r) > 1), key=len, reverse=True)]
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - no HOME
        home = ""
    if len(home) > 3:                                     # C:\Users\<name>\.m2\… names the operator
        out += [(home, "~"), (home.replace("\\", "/"), "~")]
    return out


class Cleaner:
    """오류 목록과 KB에 저장하는 텍스트를 정리하는 클래스.
    비밀 값을 마스킹하고 작업 루트와 홈 디렉토리 경로를 지운다.

    Masks secret values (built-in rules + ``kb.redact_file``) and strips the workspace roots / home directory."""

    def __init__(self, cfg: Any = None, roots: Iterable[Any] = ()):
        from sbmig.kb.redact import get_redactor          # sbmig.kb.redact is a leaf module (no cycle)

        self.red = get_redactor(cfg)
        self.pairs = _root_pairs(roots)

    def strip(self, text: Optional[str]) -> str:
        """텍스트에서 작업 루트와 홈 디렉토리 경로만 지운다. 비밀 값 마스킹은 하지 않는다."""
        t = text or ""
        for needle, repl in self.pairs:
            if repl:
                t = t.replace(needle, repl)
            else:
                t = t.replace(needle + "/", "").replace(needle + "\\", "").replace(needle, "")
        return t

    def __call__(self, text: Optional[str], limit: int = 0) -> str:
        """텍스트의 경로를 지우고 비밀 값을 마스킹한 뒤 ``limit``자로 자른다.
        빈 값이면 빈 문자열을 반환한다."""
        if not text:
            return ""
        return cap(self.red.redact(self.strip(text)) or "", limit)

    def has_secret(self, text: Optional[str]) -> bool:
        """``text``에 마스킹할 비밀 값이 들어 있는지 확인한다."""
        return bool(text) and bool(self.red.redact_ex(text)[1])


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """데이터를 같은 디렉토리의 임시 파일에 쓴 뒤 ``os.replace``로 바꿔 넣는다.
    중간에 프로세스가 죽어도 반쯤 쓴 파일이 남지 않는다.
    Windows에서 다른 프로그램이 파일을 잡고 있으면 잠시 후 다시 시도한다.

    Temp file in the directory + ``os.replace`` (retried briefly: Windows readers / virus scanners hold files)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # Windows에서는 백신이나 편집기가 파일을 잠깐 잡으면 replace가
        # PermissionError를 내므로 간격을 늘리며 다시 시도한다
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
    """UTF-8 텍스트를 :func:`atomic_write_bytes`로 원자적으로 쓴다. 줄바꿈은 그대로 둔다.

    :func:`atomic_write_bytes` of UTF-8 text (newlines as given)."""
    atomic_write_bytes(Path(path), text.encode("utf-8"))
