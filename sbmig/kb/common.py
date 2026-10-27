"""Small helpers the KB records share (PLAN-v2 step 4: what survived of the v1 ``harvest`` / ``ledger`` modules).

* :func:`project_name` — the project name of every KB record (the profile name; a workspace copy is always called
  ``project``). The fix loop names the project of a stored fix script with it.
* :func:`rel_posix` — a path as stored in the KB: relative to the project, posix separators, never absolute.
* :func:`changes_as_diff` — a unified-diff-like text of a ``FileChange`` list (the error tracker's fallback when an
  attempt has no git diff; :mod:`sbmig.kb.changes` reads this format too).
* :data:`RAW_LIMIT` — characters of a build error's raw text kept in a record.
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
    """Project name for KB records: the profile name (a workspace copy is always called 'project')."""
    if profile is not None and profile.name:
        return _basename(profile.name)
    if ws is not None:
        return ws.root.name
    return ""


def rel_posix(path: Optional[str], ws: Optional["Workspace"]) -> Optional[str]:
    """``path`` relative to the workspace root with posix separators; an absolute path outside the project keeps
    only its file name (never leak absolute paths); None / "" as given."""
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


_rel_posix = rel_posix          # the name error_cases.py used (it came from sbmig.kb.harvest)


def changes_as_diff(changes: list[FileChange]) -> str:
    """A unified diff per modify (search → replace), ``=== CREATE / DELETE / RENAME`` blocks for the other ops."""
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
