"""Append-only jsonl files of the KB (round 10, 2026-10-26; v2.1: only ``<kb>/errors.jsonl`` is left).

A process killed half-way through a write (or a full disk) leaves a last line without its newline; the next append
would glue its first record onto it. So:

* :func:`append_lines` repairs the tail first (:func:`repair_tail`: a torn last line is moved to
  ``<file>.corrupt`` — appended, with a warning — a complete record that only lacks its newline gets one), then
  writes the batch with one ``write``, ``flush`` and ``os.fsync``;
* :func:`parse_record` reads one line; a torn record with a whole one glued to it gives the whole one back;
* :func:`line_ids` — the id(s) a stored line really holds (dedupe on append / import).

File-based on purpose (invariant 3). Callers hold the KB lock (:mod:`sbmig.kb.lock`) around appends.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("sbmig.kb")

CORRUPT_SUFFIX = ".corrupt"
ID_HEAD = '{"id":"'
_ID_RE = re.compile(r'^\{"id":"((?:[^"\\]|\\.)*)"')
_CHUNK = 1 << 16


def corrupt_path(path: Path) -> Path:
    return path.with_name(path.name + CORRUPT_SUFFIX)


def _fsync(fh: Any) -> None:
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError:  # pragma: no cover - a file system without fsync
        pass


def _whole_json(data: bytes) -> bool:
    try:
        return isinstance(json.loads(data.decode("utf-8")), dict)
    except (ValueError, UnicodeDecodeError):
        return False


def repair_tail(path: Path) -> bool:
    """Make ``path`` end with a newline. A torn last line goes to ``<file>.corrupt`` (appended) and is cut off; a
    whole record that only lacks its newline keeps its place. Returns True when the file was changed. Never
    raises."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        with open(path, "rb+") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return False
            last_nl, pos = -1, size
            while pos > 0:
                start = max(0, pos - _CHUNK)
                fh.seek(start)
                i = fh.read(pos - start).rfind(b"\n")
                if i >= 0:
                    last_nl = start + i
                    break
                pos = start
            fh.seek(last_nl + 1)
            tail = fh.read()
            if _whole_json(tail.strip()):
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                _fsync(fh)
                log.info("%s: the last record had no newline; added one", path)
                return True
            with open(corrupt_path(path), "ab") as bad:
                bad.write(tail + b"\n")
                _fsync(bad)
            fh.truncate(last_nl + 1)
            _fsync(fh)
        log.warning("%s: a torn last line (%d bytes, a write killed half-way) was moved to %s", path, len(tail),
                    corrupt_path(path).name)
        return True
    except OSError as e:
        log.warning("%s: could not repair the last line: %s", path, e)
        return False


def append_lines(path: Path, lines: Iterable[str]) -> int:
    """Append one record per line (``\\n`` added), after :func:`repair_tail`; one write + fsync per batch."""
    batch = [ln.rstrip("\n") for ln in lines if ln]
    if not batch:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    repair_tail(path)
    data = ("\n".join(batch) + "\n").encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(data)
        _fsync(fh)
    return len(batch)


def parse_record(line: str, model: Any) -> Optional[Any]:
    """``model.model_validate_json(line)``, else the whole record glued after a torn one, else None."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        return model.model_validate_json(line)
    except ValueError:
        pass
    i = line.rfind(ID_HEAD, 1)
    if i > 0:
        try:
            return model.model_validate_json(line[i:])
        except ValueError:
            return None
    return None


def fast_id(line: str) -> Optional[str]:
    """The id of a whole, single record line without parsing it (None when the line needs a real parse)."""
    s = line.rstrip()
    if not s.endswith("}") or s.count(ID_HEAD) != 1:
        return None
    m = _ID_RE.match(s)
    if not m:
        return None
    try:
        return json.loads(f'"{m.group(1)}"')
    except ValueError:
        return None


def line_ids(line: str) -> list[str]:
    """Ids a stored line really holds: its own when it parses, the glued whole record's after a torn one."""
    fid = fast_id(line)
    if fid is not None:
        return [fid]
    s = line.strip()
    for cand in (s, s[s.rfind(ID_HEAD, 1):] if s.rfind(ID_HEAD, 1) > 0 else ""):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            return [str(obj["id"])]
    return []
