"""KB의 jsonl 파일에 레코드를 덧붙이고 읽는 모듈. 지금은 ``<kb>/errors.jsonl``에만 쓴다.
``CaseStore.record_run`` / ``merge_from``이 덧붙이고, ``iter_errors`` / ``validate``가 읽는다.
- 쓰는 도중 죽은 프로세스는 줄바꿈 없는 마지막 줄을 남긴다.
  그래서 ``append_lines``는 먼저 파일 끝을 고치고(``repair_tail``) 여러 줄을 한 번에 쓴 뒤 fsync한다
- ``parse_record`` / ``line_ids``는 깨진 줄 뒤에 붙은 온전한 레코드를 살려서 읽는다
- 덧붙일 때는 호출하는 쪽이 KB 잠금(:mod:`sbmig.kb.lock`)을 잡는다

Append-only jsonl files of the KB (round 10, 2026-10-26; v2.1: only ``<kb>/errors.jsonl`` is left).

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
    """깨진 줄을 옮겨 둘 파일 경로 ``<file>.corrupt``를 반환한다.
    사람이 확인할 수 있도록 이 파일은 지우지 않는다."""
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
    """``path`` 파일이 줄바꿈으로 끝나도록 고친다.
    깨진 마지막 줄은 ``.corrupt``로 옮기고, 줄바꿈만 빠진 온전한 레코드에는 줄바꿈을 붙인다.
    파일을 고쳤으면 True를 반환한다. 예외를 던지지 않는다.

    Make ``path`` end with a newline. A torn last line goes to ``<file>.corrupt`` (appended) and is cut off; a
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
            # 큰 errors.jsonl을 통째로 읽지 않도록 끝에서부터 64KB씩 읽으며 마지막 줄바꿈을 찾는다
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
    """레코드를 한 줄씩 파일에 덧붙인다. 파일 끝을 먼저 고친 뒤 모든 줄을 write 한 번으로 쓰고 fsync한다.
    중간에 죽어도 망가지는 것은 마지막 줄뿐이고, 다음 덧붙이기에서 고친다.

    Append one record per line (``\\n`` added), after :func:`repair_tail`; one write + fsync per batch."""
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
    """한 줄을 ``model``로 파싱한다.
    실패하면 깨진 레코드 뒤에 붙은 온전한 레코드를, 그것도 없으면 None을 반환한다.

    ``model.model_validate_json(line)``, else the whole record glued after a torn one, else None."""
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
    """온전한 레코드 한 줄에서 JSON 파싱 없이 정규식으로 ID를 뽑는다.
    큰 파일의 중복 검사에 쓰고, 실제 파싱이 필요한 줄이면 None을 반환한다.

    The id of a whole, single record line without parsing it (None when the line needs a real parse)."""
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
    """저장된 한 줄에 실제로 들어 있는 레코드 ID 목록을 반환한다.
    깨진 줄이면 뒤에 붙은 온전한 레코드의 ID를 반환한다.

    Ids a stored line really holds: its own when it parses, the glued whole record's after a torn one."""
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
