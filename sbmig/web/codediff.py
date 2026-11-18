"""이관 결과의 코드 변경 화면에 쓸 데이터를 실행 폴더의 ``codediff/``와 ``result.zip``에서 읽어 내주는 모듈.
웹 파드는 git과 diff를 실행하지 않고, 빌드 파드가 기록한 ``index.json``, ``patch.bin``, ``summary.json``만 읽는다.
- 손상된 산출물은 500이 아니라 짧은 이유를 담은 404로 응답하고, 같은 이유는 WARNING으로 한 번만 남긴다
- 파싱한 index와 ZIP 항목 목록은 작은 LRU에 두며, 요청 사이에 파일 핸들을 열어 두지 않는다
진입점은 :func:`create_codediff_router`, :func:`load_index`, :func:`parse_hunks`, :func:`read_lines`이다.

Web side of the code-change view (contract: docs/INTERFACES.md "v2.1z-au"). The build pod writes
`<run dir>/codediff/` (`index.json`, `summary.json`, `patch.bin`); this module serves it and, for more context lines,
reads the FINAL file out of `<run dir>/result.zip`. Routes (plain `def`: they run in the thread pool):

    GET /api/runs/{id}/codediff                         the stored index.json
    GET /api/runs/{id}/codediff/files/{n}               one entry + its parsed hunks, no_newline, expandable
    GET /api/runs/{id}/codediff/files/{n}/lines?start=&end=
                                                        lines start … end (1-based, inclusive) of the final file

Errors: 400 bad run id / non-numeric start or end; 404 unknown run, `{"error": "code diff not available"}` (missing,
unreadable or another `version`), `n` outside the list, a file whose lines cannot be read (with `reason`); 409 while the
run's final redaction pass is pending. Every read error is a 404 with a short reason, logged once at WARNING.

Line ranges (`lines`): non-numeric `start` / `end` → 400; numbers are clamped (start < 1 → 1, end past the file → the
last line, a span over `MAX_LINES_PER_CALL` is cut); the response's `end` is always `start + len(lines) - 1`, so an
empty answer has `end == start - 1`.

Caches (bounded, thread-safe, no file handle kept open): the parsed index per index.json (path + mtime + size + inode,
`INDEX_CACHE` entries) and the member sizes of each result.zip (same key, `ZIP_CACHE` entries)."""
from __future__ import annotations

import codecs
import json
import logging
import os
import re
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger("sbmig.web.codediff")

VERSION = 1
ARTIFACT_DIR = "codediff"
INDEX_FILE = "index.json"
SUMMARY_FILE = "summary.json"
PATCH_FILE = "patch.bin"
RESULT_ZIP = "result.zip"
MAX_TEXT = 5_000                     # characters per shown line; longer lines end with "…"
MAX_LINES_PER_CALL = 5_000           # lines per `lines` call
DEFAULT_SPAN = 200                   # `lines` without `end`: start … start + 199
MAX_MEMBER_BYTES = 32 * 1024 * 1024  # a final file larger than this (uncompressed) is not read for context
MAX_INDEX_BYTES = 256 * 1024 * 1024  # an index.json past this is not parsed (20,000 entries are a few MiB)
MAX_SUMMARY_BYTES = 1024 * 1024      # summary.json is small; anything bigger is not what the writer wrote
MAX_HUNK_BYTES = 8 * 1024 * 1024     # one file's range in patch.bin (the writer cuts at 1 MiB)
INDEX_CACHE = 4
ZIP_CACHE = 8
UNAVAILABLE = "code diff not available"
NOSTORE = {"Cache-Control": "no-store"}
_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


class CodeDiffUnavailable(Exception):
    """코드 변경 산출물이나 그 일부를 읽을 수 없을 때 던지는 예외. ``reason``에 짧은 이유를 담는다.

    The artifact (or one part of it) cannot be served; `reason` is the short text the 404 carries."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Lru:
    # 여러 요청 스레드가 함께 쓰므로 잠금 안에서만 dict를 바꾼다
    def __init__(self, size: int):
        self.size = size
        self.data: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: str, stamp: Any) -> Any:
        with self.lock:
            hit = self.data.get(key)
            if hit is None or hit[0] != stamp:
                return None
            self.data.move_to_end(key)
            return hit[1]

    def put(self, key: str, stamp: Any, value: Any) -> None:
        with self.lock:
            self.data[key] = (stamp, value)
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.data.clear()


_index_cache = _Lru(INDEX_CACHE)
_zip_cache = _Lru(ZIP_CACHE)
_warned: set[str] = set()
_warned_lock = threading.Lock()


def clear_caches() -> None:
    """파싱한 index와 ZIP 항목 목록 캐시를 비운다.

    Drop the cached indices and ZIP member lists (tests; nothing else needs it: the keys carry mtime and size)."""
    _index_cache.clear()
    _zip_cache.clear()
    with _warned_lock:
        _warned.clear()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    # 같은 파일을 여러 번 요청해도 로그가 쌓이지 않도록 같은 이유는 한 번만 남긴다
    with _warned_lock:
        if key in _warned:
            return
        if len(_warned) >= 1024:
            _warned.clear()
        _warned.add(key)
    log.warning(msg, *args)


def _stamp(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def artifact_dir(run_dir: Path) -> Path:
    """실행 폴더 안의 코드 변경 산출물 폴더 경로를 반환한다.

    `<run dir>/codediff`."""
    return Path(run_dir) / ARTIFACT_DIR


def load_index(run_dir: Path) -> dict[str, Any]:
    """실행 폴더의 ``codediff/index.json``을 읽어 반환한다. 없거나 깨졌거나 ``version``이 1이 아니면
    ``CodeDiffUnavailable``을 던진다. 같은 파일은 캐시에서 꺼내므로 반환값을 바꾸면 안 된다.

    The parsed `index.json` (cached per path + mtime + size + inode; the caller must not mutate it). Raises
    `CodeDiffUnavailable` when it is missing, unreadable, not a JSON object with a `files` list, or of another
    `version` than 1."""
    path = artifact_dir(run_dir) / INDEX_FILE
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            stamp = _stamp(st)
            hit = _index_cache.get(str(path), stamp)
            if hit is not None:
                return hit
            if st.st_size > MAX_INDEX_BYTES:
                raise CodeDiffUnavailable("index.json too large")
            raw = f.read(MAX_INDEX_BYTES + 1)
    except FileNotFoundError:
        raise CodeDiffUnavailable("no codediff/index.json") from None
    except OSError as e:
        _warn_once(f"index-os:{path}", "code diff: cannot read %s: %s", path, e)
        raise CodeDiffUnavailable("index.json unreadable") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001  깨진 JSON은 종류와 상관없이 404로 응답한다
        _warn_once(f"index-json:{path}:{stamp}", "code diff: %s is not valid JSON: %s", path, e)
        raise CodeDiffUnavailable("index.json unreadable") from None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        _warn_once(f"index-shape:{path}:{stamp}", "code diff: %s has no files list", path)
        raise CodeDiffUnavailable("index.json unreadable")
    if data.get("version") != VERSION:
        _warn_once(f"index-version:{path}:{stamp}", "code diff: %s has version %r (this server reads %d)", path,
                   data.get("version"), VERSION)
        raise CodeDiffUnavailable(f"index.json version {data.get('version')!r} not supported")
    _index_cache.put(str(path), stamp, data)
    return data


def read_summary(run_dir: Path) -> dict[str, Any] | None:
    """``codediff/summary.json``의 ``summary`` 객체를 반환한다. 산출물이 없거나 파일이 깨졌으면 None을 반환하고
    예외를 던지지 않는다.

    The `summary` object of `codediff/summary.json`, or None (no artifact, no index.json beside it, unreadable,
    another `version`, no `summary` object). Never raises; small read, for `RunManager.status()`."""
    d = artifact_dir(run_dir)
    path = d / SUMMARY_FILE
    try:
        if not (d / INDEX_FILE).is_file():
            return None
        with open(path, "rb") as f:
            raw = f.read(MAX_SUMMARY_BYTES + 1)
        if len(raw) > MAX_SUMMARY_BYTES:
            _warn_once(f"summary-size:{path}", "code diff: %s is larger than %d bytes", path, MAX_SUMMARY_BYTES)
            return None
        data = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        _warn_once(f"summary:{path}", "code diff: cannot read %s: %s", path, e)
        return None
    except Exception as e:  # noqa: BLE001  상태 조회는 어떤 경우에도 실패하면 안 된다
        _warn_once(f"summary-x:{path}", "code diff: cannot read %s: %r", path, e)
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("summary"), dict):
        _warn_once(f"summary-shape:{path}", "code diff: %s has no summary of version %d", path, VERSION)
        return None
    return data["summary"]


def _decoder(encoding: Any) -> Callable[[bytes], str]:
    # 모르는 인코딩 이름이면 utf-8로 읽고, 깨진 바이트는 대체 문자로 바꾼다
    name = "utf-8"
    if isinstance(encoding, str) and encoding.strip():
        try:
            name = codecs.lookup(encoding.strip()).name
        except (LookupError, ValueError):
            name = "utf-8"

    def decode(b: bytes) -> str:
        if b.endswith(b"\r"):
            b = b[:-1]
        try:
            text = b.decode(name, errors="replace")
        except Exception:  # noqa: BLE001  코덱 자체가 실패하면 utf-8로 읽는다
            text = b.decode("utf-8", errors="replace")
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    return decode


def _split_lines(data: bytes) -> list[bytes]:
    # 마지막 줄 끝의 줄바꿈 뒤에는 줄이 없으므로 빈 조각을 버린다
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def parse_hunks(data: bytes, encoding: Any = "utf-8") -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """파일 하나의 unified hunk 바이트를 hunk 목록과 ``no_newline``으로 바꿔 반환한다. 형식이 맞지 않으면
    ``ValueError``를 던진다.

    Parse one file's unified hunks (from its first `@@` line to the end of its range): the bytes are split at `\\n`,
    each line decoded with `encoding` (unknown name → utf-8, `errors="replace"`), one trailing `\\r` dropped, cut at
    `MAX_TEXT` characters with `…`. A header `@@ -a[,b] +c[,d] @@ <section>` (a missing count = 1); body lines by their
    first byte (`' '` context, `'+'`, `'-'`; an empty line counts as an empty context line); `\\ No newline at end of
    file` belongs to the line before it and sets `no_newline.old` / `.new` (after a context line both). Old / new line
    numbers are counted from the header; a number is None on the side a line does not exist. Raises ValueError when the
    bytes do not start with `@@ ` or hold a line of another kind."""
    decode = _decoder(encoding)
    hunks: list[dict[str, Any]] = []
    no_newline = {"old": False, "new": False}
    lines = _split_lines(data)
    if not lines or not lines[0].startswith(b"@@ "):
        raise ValueError("hunks do not start with '@@ '")
    cur: dict[str, Any] | None = None
    old_no = new_no = 0
    last_kind: str | None = None
    for raw in lines:
        if raw.startswith(b"@@ "):
            m = _HEADER.match(raw.rstrip(b"\r"))
            if not m:
                raise ValueError(f"bad hunk header {raw[:80]!r}")
            old_start, new_start = int(m.group(1)), int(m.group(3))
            cur = {"old_start": old_start, "old_lines": int(m.group(2)) if m.group(2) is not None else 1,
                   "new_start": new_start, "new_lines": int(m.group(4)) if m.group(4) is not None else 1,
                   "section": decode(m.group(5)), "lines": []}
            hunks.append(cur)
            old_no, new_no, last_kind = old_start, new_start, None
            continue
        if cur is None:
            raise ValueError("hunk line before the first header")
        head = raw[:1]
        if head == b"\\":
            if last_kind == "-":
                no_newline["old"] = True
            elif last_kind == "+":
                no_newline["new"] = True
            elif last_kind == " ":
                no_newline["old"] = no_newline["new"] = True
            continue
        if head in (b" ", b""):
            cur["lines"].append([" ", old_no, new_no, decode(raw[1:])])
            old_no += 1
            new_no += 1
            last_kind = " "
        elif head == b"-":
            cur["lines"].append(["-", old_no, None, decode(raw[1:])])
            old_no += 1
            last_kind = "-"
        elif head == b"+":
            cur["lines"].append(["+", None, new_no, decode(raw[1:])])
            new_no += 1
            last_kind = "+"
        else:
            raise ValueError(f"unexpected hunk line {raw[:40]!r}")
    return hunks, no_newline


def read_patch(run_dir: Path, entry: dict[str, Any]) -> bytes | None:
    """항목의 ``patch`` 범위를 ``patch.bin``에서 읽어 반환한다. 범위가 없으면 None을 반환하고, 범위가 잘못되었거나
    파일을 읽을 수 없으면 ``CodeDiffUnavailable``을 던진다.

    The bytes of `entry["patch"]` (`[offset, length]`) out of `patch.bin`; None when the entry has no range
    (`omitted`). Raises CodeDiffUnavailable for a malformed range, one past the end of the file, or a read error."""
    rng = entry.get("patch")
    if rng is None:
        return None
    if not (isinstance(rng, list) and len(rng) == 2 and all(type(v) is int and v >= 0 for v in rng)):
        raise CodeDiffUnavailable("bad patch range")
    off, length = rng
    if length > MAX_HUNK_BYTES:
        raise CodeDiffUnavailable("patch range too large")
    path = artifact_dir(run_dir) / PATCH_FILE
    try:
        with open(path, "rb") as f:
            if off + length > os.fstat(f.fileno()).st_size:
                raise CodeDiffUnavailable("patch range past the end of patch.bin")
            f.seek(off)
            data = f.read(length)
    except FileNotFoundError:
        raise CodeDiffUnavailable("no codediff/patch.bin") from None
    except OSError:
        raise CodeDiffUnavailable("patch.bin unreadable") from None
    if len(data) != length:
        raise CodeDiffUnavailable("patch.bin shorter than the range")
    return data


def _safe_rel(p: Any, allow_empty: bool = False) -> str | None:
    # ZIP 밖이나 다른 항목을 가리키지 못하도록 절대 경로와 .. 구성 요소를 거부한다
    if not isinstance(p, str):
        return None
    if p == "":
        return "" if allow_empty else None
    norm = p.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or "\x00" in norm:
        return None
    parts = norm.split("/")
    if any(part == ".." for part in parts):
        return None
    parts = [part for part in parts if part not in ("", ".")]
    return "/".join(parts) if parts else ("" if allow_empty else None)


def member_name(index: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """항목의 최종 파일이 ``result.zip`` 안에서 가지는 이름(``<zip_root>/<path>``)을 반환한다. 경로가 안전하지
    않으면 None을 반환한다.

    The member `<zip_root>/<path>` of result.zip for this entry, or None when `zip_root` or `path` is absolute,
    has a `..` component or is not a string."""
    root = _safe_rel(index.get("zip_root", ""), allow_empty=True)
    path = _safe_rel(entry.get("path"))
    if root is None or path is None:
        return None
    return f"{root}/{path}" if root else path


def _zip_sizes(zip_path: Path) -> dict[str, int] | None:
    # ZIP 중앙 디렉터리만 읽어 항목 이름과 크기를 캐시하고 파일은 바로 닫는다
    try:
        st = zip_path.stat()
    except OSError:
        return None
    stamp = _stamp(st)
    hit = _zip_cache.get(str(zip_path), stamp)
    if hit is not None:
        return hit
    try:
        with zipfile.ZipFile(zip_path) as zf:
            sizes = {i.filename: i.file_size for i in zf.infolist() if not i.is_dir()}
    except Exception as e:  # noqa: BLE001  손상된 ZIP은 펼칠 수 없는 파일로 본다
        _warn_once(f"zip:{zip_path}:{stamp}", "code diff: cannot read %s: %s", zip_path, e)
        return None
    _zip_cache.put(str(zip_path), stamp, sizes)
    return sizes


def _line_source_problem(run_dir: Path, index: dict[str, Any], entry: dict[str, Any]) -> tuple[str | None, str | None]:
    # 최종 파일을 읽을 수 없는 이유와 ZIP 항목 이름을 함께 반환한다
    if entry.get("status") == "deleted":
        return "the file was deleted", None
    if entry.get("binary"):
        return "binary file", None
    member = member_name(index, entry)
    if member is None:
        return "unsafe path in the index", None
    sizes = _zip_sizes(Path(run_dir) / RESULT_ZIP)
    if sizes is None:
        return "result.zip not available", None
    size = sizes.get(member)
    if size is None:
        return "file not in result.zip", None
    if size > MAX_MEMBER_BYTES:
        return f"file larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB", None
    return None, member


def is_expandable(run_dir: Path, index: dict[str, Any], entry: dict[str, Any]) -> bool:
    """항목의 최종 파일을 ``result.zip``에서 읽을 수 있는지 확인한다. ZIP 항목은 풀지 않는다.

    Can the final file be read from result.zip (not deleted, not binary, a safe path, the member present and at
    most `MAX_MEMBER_BYTES`)? Reads only the (cached) central directory, never decompresses."""
    return _line_source_problem(run_dir, index, entry)[0] is None


def file_entry(index: dict[str, Any], n: int) -> dict[str, Any] | None:
    """index의 ``n``번째 파일 항목을 반환한다. 범위 밖이거나 객체가 아니면 None을 반환한다.

    The `n`-th entry of `files` (the id is the position), or None."""
    files = index.get("files") or []
    if n < 0 or n >= len(files) or not isinstance(files[n], dict):
        return None
    return files[n]


def file_detail(run_dir: Path, index: dict[str, Any], n: int) -> dict[str, Any] | None:
    """파일 하나의 항목과 파싱한 hunk, ``no_newline``, ``expandable``을 반환한다. ``n``이 범위 밖이면 None을
    반환한다. hunk를 읽을 수 없으면 빈 목록과 ``omitted: "unreadable"``을 담는다.

    One file's entry plus its parsed hunks (contract shape). An entry without a patch range answers empty `hunks`
    with its own `omitted`; a range that cannot be read or parsed answers empty `hunks` with `omitted: "unreadable"`
    (logged once). None when `n` is outside the list."""
    entry = file_entry(index, n)
    if entry is None:
        return None
    out = {"id": n, "path": entry.get("path"), "old_path": entry.get("old_path"), "status": entry.get("status"),
           "changed": entry.get("changed"), "binary": bool(entry.get("binary")), "added": entry.get("added"),
           "removed": entry.get("removed"), "encoding": entry.get("encoding"), "omitted": entry.get("omitted") or "",
           "expandable": is_expandable(run_dir, index, entry), "hunks": [],
           "no_newline": {"old": False, "new": False}}
    try:
        data = read_patch(run_dir, entry)
        if data is not None:
            out["hunks"], out["no_newline"] = parse_hunks(data, entry.get("encoding"))
    except Exception as e:  # noqa: BLE001  hunk를 읽지 못해도 항목은 보여 준다
        _warn_once(f"hunks:{run_dir}:{n}:{entry.get('patch')}", "code diff: run %s file %d (%s): hunks not readable: %s",
                   Path(run_dir).name, n, entry.get("path"), e)
        out["omitted"] = "unreadable"
    return out


def _parse_int(value: str | None, name: str) -> int | None:
    if value is None or value.strip() == "":
        return None
    v = value.strip()
    if not re.fullmatch(r"[+-]?\d{1,18}", v):
        raise ValueError(f"{name} must be a whole number")
    return int(v)


def read_lines(run_dir: Path, index: dict[str, Any], entry: dict[str, Any], start: int | None = None,
               end: int | None = None) -> dict[str, Any]:
    """최종 파일의 ``start``부터 ``end``까지 줄을 ``result.zip``에서 읽어 반환한다. 범위는 파일에 맞게 줄이고
    한 번에 ``MAX_LINES_PER_CALL``줄까지만 읽는다. 읽을 수 없으면 ``CodeDiffUnavailable``을 던진다.

    Lines `start … end` (1-based, inclusive; defaults 1 and start + 199) of the final file, member
    `<zip_root>/<path>` of result.zip: `{start, end, total, lines}`. Clamped to the file, at most `MAX_LINES_PER_CALL`
    lines; the returned `end` is `start + len(lines) - 1`. Decoded like the hunks. Raises CodeDiffUnavailable for a
    deleted or binary entry, an unsafe path, a missing ZIP or member, a member over `MAX_MEMBER_BYTES`, a read error."""
    problem, member = _line_source_problem(run_dir, index, entry)
    if problem is not None:
        raise CodeDiffUnavailable(problem)
    zip_path = Path(run_dir) / RESULT_ZIP
    try:
        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo(member)
            if info.file_size > MAX_MEMBER_BYTES:
                raise CodeDiffUnavailable(f"file larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB")
            with zf.open(info) as f:
                data = f.read(MAX_MEMBER_BYTES + 1)
    except KeyError:
        raise CodeDiffUnavailable("file not in result.zip") from None
    except FileNotFoundError:
        raise CodeDiffUnavailable("result.zip not available") from None
    except Exception as e:  # noqa: BLE001  손상된 ZIP의 압축 해제 오류도 404로 응답한다
        _warn_once(f"member:{zip_path}:{member}", "code diff: cannot read %s from %s: %s", member, zip_path, e)
        raise CodeDiffUnavailable("file not readable from result.zip") from None
    if len(data) > MAX_MEMBER_BYTES:
        raise CodeDiffUnavailable(f"file larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MiB")
    lines = _split_lines(data)
    total = len(lines)
    s = max(1, start if start is not None else 1)
    e = end if end is not None else s + DEFAULT_SPAN - 1
    e = min(e, total, s + MAX_LINES_PER_CALL - 1)
    decode = _decoder(entry.get("encoding"))
    shown = [decode(b) for b in lines[s - 1:e]] if e >= s else []
    return {"start": s, "end": s + len(shown) - 1, "total": total, "lines": shown}


def create_codediff_router(mgr: Any) -> APIRouter:
    """코드 변경 화면의 API 라우터를 만든다. ``mgr``는 실행 폴더와 마스킹 대기 여부를 알려 주는 ``RunManager``다.

    The three `/api/runs/{id}/codediff…` routes over `mgr` (a `RunManager`: `runs`, `run_dir`, `file_withheld`).
    Run ids go through app.py's `_check_id` (imported here, when app.py is already loaded)."""
    from .app import _check_id

    router = APIRouter()

    def err(status: int, error: str, reason: str | None = None) -> JSONResponse:
        body = {"error": error}
        if reason:
            body["reason"] = reason
        return JSONResponse(body, status_code=status, headers=NOSTORE)

    def gate(run_id: str) -> tuple[Path | None, JSONResponse | None]:
        # 실행 확인, 마스킹 대기 확인을 다른 결과 파일 라우트와 같은 순서로 한다
        rid = _check_id(run_id)
        rd = mgr.run_dir(rid)
        if rid not in mgr.runs and not rd.is_dir():
            return None, err(404, "unknown run")
        if mgr.file_withheld(rid, ARTIFACT_DIR):
            return None, err(409, "the code diff is available once the run has finished (its outputs are redacted "
                                  "at the end of the run)")
        return rd, None

    def index_of(rd: Path) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        try:
            return load_index(rd), None
        except CodeDiffUnavailable as e:
            return None, err(404, UNAVAILABLE, e.reason)

    def number(n: str) -> int | None:
        return int(n) if re.fullmatch(r"\d{1,9}", n) else None

    @router.get("/api/runs/{run_id}/codediff")
    def codediff_index(run_id: str) -> JSONResponse:
        rd, fail = gate(run_id)
        if fail is not None:
            return fail
        index, fail = index_of(rd)
        if fail is not None:
            return fail
        return JSONResponse(index, headers=NOSTORE)

    @router.get("/api/runs/{run_id}/codediff/files/{n}")
    def codediff_file(run_id: str, n: str) -> JSONResponse:
        rd, fail = gate(run_id)
        if fail is not None:
            return fail
        index, fail = index_of(rd)
        if fail is not None:
            return fail
        k = number(n)
        detail = file_detail(rd, index, k) if k is not None else None
        if detail is None:
            return err(404, "no such file in the code diff")
        return JSONResponse(detail, headers=NOSTORE)

    @router.get("/api/runs/{run_id}/codediff/files/{n}/lines")
    def codediff_lines(run_id: str, n: str, start: str | None = None, end: str | None = None) -> JSONResponse:
        rd, fail = gate(run_id)
        if fail is not None:
            return fail
        try:
            a, b = _parse_int(start, "start"), _parse_int(end, "end")
        except ValueError as e:
            return err(400, str(e))
        index, fail = index_of(rd)
        if fail is not None:
            return fail
        k = number(n)
        entry = file_entry(index, k) if k is not None else None
        if entry is None:
            return err(404, "no such file in the code diff")
        try:
            return JSONResponse(read_lines(rd, index, entry, a, b), headers=NOSTORE)
        except CodeDiffUnavailable as e:
            return err(404, "lines not available", e.reason)

    return router
