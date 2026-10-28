"""KB 의 덧붙이기 전용 jsonl 파일 도구(round 10; v2.1 에서는 ``<kb>/errors.jsonl`` 하나만 남음).
누가 부르나: ``CaseStore.record_run`` / ``merge_from``(덧붙이기), ``iter_errors`` / ``validate``(읽기),
``_known_ids``(중복 검사). 왜 필요한가: 쓰는 도중 죽은 프로세스(또는 꽉 찬 디스크)는 줄바꿈 없는 마지막 줄을
남기고, 다음 덧붙이기가 그 뒤에 첫 레코드를 붙여 두 레코드가 한 줄이 된다. 그래서 ``append_lines`` 는 먼저
꼬리를 고치고(``repair_tail``: 찢긴 줄은 ``<file>.corrupt`` 로 옮김) 묶음을 한 번에 쓰고 fsync 하며,
``parse_record`` / ``line_ids`` 는 찢긴 줄 뒤에 붙은 온전한 레코드를 살려 읽는다. 호출자는 덧붙일 때 KB
잠금(:mod:`sbmig.kb.lock`)을 잡는다. 파일 기반은 의도된 것이다(DB 금지, 불변조건 3).

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
    """찢긴 줄을 옮겨 두는 파일 ``<file>.corrupt``(지우지 않고 남겨 사람이 볼 수 있게)."""
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
    """``path`` 가 줄바꿈으로 끝나게 한다. 찢긴 마지막 줄은 ``.corrupt`` 로 옮기고 잘라내며, 줄바꿈만 빠진
    온전한 레코드는 제자리에 두고 줄바꿈만 붙인다. 바뀌었으면 True. 예외를 내지 않는다.

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
            # 마지막 줄바꿈을 끝에서부터 64KB 씩 거꾸로 찾는다(큰 errors.jsonl 을 통째로 읽지 않으려고)
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
    """레코드를 한 줄씩 덧붙인다(``\\n`` 추가). 먼저 꼬리를 고치고 묶음 전체를 write 한 번 + fsync 로 쓴다 —
    도중에 죽어도 망가지는 건 마지막 줄뿐이고 다음 덧붙이기가 고친다.

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
    """한 줄을 ``model`` 로 읽는다; 실패하면 찢긴 레코드 뒤에 붙은 온전한 레코드를, 그것도 아니면 None.

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
    """온전한 한 레코드 줄의 ID 를 JSON 파싱 없이(정규식으로) 뽑는다 — 큰
    errors.jsonl 의 중복 검사를 빠르게. 진짜 파싱이 필요한 줄이면 None.

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
    """저장된 줄이 실제로 가진 ID: 읽히면 그 ID, 찢긴 줄이면 뒤에 붙은 온전한 레코드의 ID.

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
