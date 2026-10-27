"""Small helpers of the v2.1 error list and KB (docs/v2/PLAN-v2.1.md §3.3 / §3.4): the ids, the text cleaner (secret
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
    """A stored path: posix separators, no leading ``./``; "" for None."""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def case_id_for(l2: Optional[str]) -> str:
    """The KB case id of an L2 signature (``signature_generic``): ``sha1(l2)[:10]``; "" when there is none."""
    if not l2:
        return ""
    return hashlib.sha1(l2.encode("utf-8")).hexdigest()[:10]


def case_id_of(e: BuildError) -> str:
    """The case of a build error: its L2 id, or — no L2 — ``sha1(kind|file|L1)[:10]`` (L1 = the signature, else
    ``kind|message``), exactly like ``sbmig.triage.cluster.case_id_of``."""
    cid = case_id_for(e.signature_generic)
    if cid:
        return cid
    kind = e.kind.value if isinstance(e.kind, ErrorKind) else str(e.kind)
    key = "|".join((kind, posix(e.file), e.signature or f"{kind}|{e.message}"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def entry_id(run_id: str, case_id: str, file: Optional[str], line: Optional[int], message: str) -> str:
    """``sha1(run_id|case_id|file|line|message)[:12]`` — stable for the same inputs."""
    key = "|".join((run_id or "", case_id or "", posix(file), str(line or 0), message or ""))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def first_line(text: Optional[str], limit: int = 300) -> str:
    """The first non-empty line, whitespace collapsed, at most ``limit`` characters."""
    for ln in (text or "").splitlines():
        s = " ".join(ln.split())
        if s:
            return s[:limit]
    return ""


def note_text(n: Any, limit: int = 0) -> str:
    """One 수정 note (``EditNote``, v2.1b) as a person reads it — the one format of errors.md, report.md and the log:
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
    """``text`` between fences — a longer backtick fence when the code itself holds one."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0) if fence.startswith("`") else 0
    f = fence if longest < len(fence) else "`" * (longest + 1)
    return [f, *text.split("\n"), f]


def note_block(n: Any, *, fence: str = "```") -> list[str]:
    """v2.1e: one 수정 note with its code — the :func:`note_text` line, then per hunk ``  as-is (<lines>):`` + the
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
    """``text`` cut to ``limit`` characters (0 = no cap) with a marker."""
    t = text or ""
    if limit and limit > 0 and len(t) > limit:
        return t[:limit] + f"\n…[{len(t) - limit} more characters]"
    return t


def _root_pairs(roots: Iterable[Any]) -> list[tuple[str, str]]:
    """(needle, replacement): the workspace roots → "" (relative paths), the user's home → "~"."""
    found: set[str] = set()
    for r in roots:
        if not r:
            continue
        s = str(r)
        found |= {s, s.replace("\\", "/")}
        if s.startswith("/private/"):                     # macOS: /var/… and /tmp/… resolve to /private/…
            found.add(s[len("/private"):])
    out = [(r, "") for r in sorted((r for r in found if len(r) > 1), key=len, reverse=True)]
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - no HOME
        home = ""
    if len(home) > 3:                                     # C:\Users\<name>\.m2\… names the operator
        out += [(home, "~"), (home.replace("\\", "/"), "~")]
    return out


class Cleaner:
    """Masks secret values (built-in rules + ``kb.redact_file``) and strips the workspace roots / home directory."""

    def __init__(self, cfg: Any = None, roots: Iterable[Any] = ()):
        from sbmig.kb.redact import get_redactor          # sbmig.kb.redact is a leaf module (no cycle)

        self.red = get_redactor(cfg)
        self.pairs = _root_pairs(roots)

    def strip(self, text: Optional[str]) -> str:
        t = text or ""
        for needle, repl in self.pairs:
            if repl:
                t = t.replace(needle, repl)
            else:
                t = t.replace(needle + "/", "").replace(needle + "\\", "").replace(needle, "")
        return t

    def __call__(self, text: Optional[str], limit: int = 0) -> str:
        if not text:
            return ""
        return cap(self.red.redact(self.strip(text)) or "", limit)

    def has_secret(self, text: Optional[str]) -> bool:
        return bool(text) and bool(self.red.redact_ex(text)[1])


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Temp file in the directory + ``os.replace`` (retried briefly: Windows readers / virus scanners hold files)."""
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
    """:func:`atomic_write_bytes` of UTF-8 text (newlines as given)."""
    atomic_write_bytes(Path(path), text.encode("utf-8"))
