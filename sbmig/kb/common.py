"""KB 기록에 쓰는 공통 함수를 모은 모듈. 프로젝트 이름, 상대 경로, diff 텍스트를 만든다.

``project_name`` is also the project name of a stored script (fix loop). ``rel_posix`` always makes a path
relative to the project, so no absolute path of the operator's PC lands in the KB. ``RAW_LIMIT`` caps the
raw build-error text kept in a record.

* :func:`changes_as_diff` is the error tracker's fallback when an attempt has no git diff;
  :mod:`sbmig.kb.changes` reads this format too.
* ``sbmig.kb`` re-exports ``project_name`` / ``rel_posix`` / ``changes_as_diff``.
"""
from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Optional

from sbmig.core.models import FileChange, ProjectProfile

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.workspace import Workspace

RAW_LIMIT = 2000
_ABS_RE = re.compile(r"^[A-Za-z]:/")


def _basename(p: str) -> str:
    return PurePosixPath((p or "").replace("\\", "/")).name


def project_name(profile: Optional[ProjectProfile], ws: Optional["Workspace"] = None) -> str:
    """KB 기록에 남길 프로젝트 이름을 반환한다.
    프로필 이름을 먼저 쓴다. 작업 복사본 폴더 이름은 항상 'project'이기 때문이다. 둘 다 없으면 빈 문자열이다."""
    if profile is not None and profile.name:
        return _basename(profile.name)
    if ws is not None:
        return ws.root.name
    return ""


def rel_posix(path: Optional[str], ws: Optional["Workspace"]) -> Optional[str]:
    """경로를 작업 루트 기준 posix 상대 경로로 바꾼다. 프로젝트 밖의 절대 경로는 파일 이름만 남긴다.
    None이나 빈 문자열은 그대로 반환한다."""
    if not path:
        return path
    p = path.replace("\\", "/")
    if ws is not None and (p.startswith("/") or _ABS_RE.match(p)):
        p = ws.rel(path).replace("\\", "/")
    if p.startswith("/") or _ABS_RE.match(p):
        p = PurePosixPath(p).name
    while p.startswith("./"):
        p = p[2:]
    return p


_rel_posix = rel_posix          # 이전 이름이다. 이 이름으로 import하는 코드가 있어서 남겨 둔다


def changes_as_diff(changes: list[FileChange]) -> str:
    """FileChange 목록을 diff 형식 텍스트로 만든다.
    수정은 unified diff로 쓴다. 생성, 삭제, 이름 변경은 ``=== CREATE / DELETE / RENAME`` 블록으로 쓴다.

    A modify is diffed search vs replace (2 context lines)."""
    parts: list[str] = []
    for ch in changes:
        if ch.op == "modify":
            a = (ch.search or "").splitlines(keepends=False)
            b = (ch.replace or "").splitlines(keepends=False)
            diff = difflib.unified_diff(a, b, f"a/{ch.path}", f"b/{ch.path}", lineterm="", n=2)
            parts.append("\n".join(diff) or f"=== MODIFY {ch.path} (no textual change)")
        elif ch.op == "create":
            body = "\n".join("+" + ln for ln in (ch.content or "").splitlines())
            parts.append(f"=== CREATE {ch.path}\n{body}")
        elif ch.op == "delete":
            parts.append(f"=== DELETE {ch.path}")
        else:
            parts.append(f"=== RENAME {ch.path} -> {ch.new_path}")
    return "\n\n".join(parts)
