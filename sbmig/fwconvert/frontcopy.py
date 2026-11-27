"""원본 프로젝트의 JSP와 정적 파일을 생성할 프로젝트의 정적 파일 폴더로 복사하는 모듈.
복사한 JSP 안의 include 경로도 새 위치로 고친다.

Contract: docs/FW-CONVERT.md sections 2.1 and 3.6 (scenario A12). Which files: ``*.jsp`` (``files.jsp_extensions``,
default ``[.jsp]``, not in the shared rules file yet) and ``files.static_extensions``. The target path is the part after
the LAST path segment named like one of ``files.webapp_roots`` (compared without case); a leading ``WEB-INF`` and a
``jsp`` folder directly after it are dropped; without a webapp root only the file name is kept. Everything lands under
``layout.static_dir``. A static file (not a JSP) inside ``WEB-INF`` is refused. Two sources with the same target (the
comparison ignores case, because the result is opened on Windows and macOS too): the first in sorted order wins, the
next is an error entry; a file already on disk is never overwritten (exclusive create). Bytes are copied unchanged.
A file whose real path (symbolic links resolved) leaves the source root is refused. Bad input never raises: it
becomes an ``errors`` entry. ``decode_source`` is the one decoding rule of the brief, ``scan_limit``
(``frontend.max_scan_kb``, default 4096, not in the shared rules file yet) the size cap of every text scan; both are
shared with ``apimap`` and ``ajax``.

``fix_jsp_paths`` runs after the copy: absolute web paths in ``<%@ include file>``, ``<jsp:include page>``,
``<jsp:forward page>`` and ``<c:import url>`` are mapped from the source's web path (after its webapp root) to the
copy's (``/WEB-INF/static/{p}/…``); a path to a file that was not copied stays and is a ``[JSP_PATH]`` item."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from sbmig.fwconvert.model import Layout, QueueItem
from sbmig.fwconvert.rules import ConvertRules

# 직접 순회할 때 들어가지 않는 폴더. 버전 관리, 빌드 결과, IDE 설정 폴더다
SKIP_DIRS = frozenset({".git", ".svn", ".hg", "target", "build", "node_modules", ".idea", ".vscode", ".settings",
                       "nbproject", ".gradle", ".mvn"})

DECODINGS = ("utf-8", "cp949", "latin-1")
EXCERPT_CHARS = 2000
DEFAULT_SCAN_KB = 4096


def scan_limit(rules: ConvertRules) -> int:
    """JSP, JS 파일 본문을 검사할 때 쓰는 파일 크기 제한을 바이트로 반환한다.
    ``frontend.max_scan_kb``가 없으면 4096KB다. 이보다 큰 파일은 검사하지 않는다."""
    try:
        kb = float(rules.get("frontend.max_scan_kb", DEFAULT_SCAN_KB) or DEFAULT_SCAN_KB)
    except (TypeError, ValueError):
        kb = DEFAULT_SCAN_KB
    return int(kb * 1024)


def is_regular(path: Path) -> bool:
    """링크를 따라간 뒤 일반 파일이면 True를 반환한다. 이름 있는 파이프, 장치 파일, 폴더는 False다.

    Raises ``OSError`` when the path cannot be stat-ed (callers turn it into an error entry)."""
    return stat.S_ISREG(path.stat().st_mode)


def _write_in_place(path: Path, data: bytes) -> None:
    """파일을 임시 파일에 쓴 뒤 바꿔치기한다. 권한은 원래 파일 것을 쓴다."""
    mode = path.stat().st_mode
    fd, tmp = tempfile.mkstemp(prefix=".fwc-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def decode_source(raw: bytes) -> tuple[str, str]:
    """원본 파일의 바이트를 문자열로 바꾸고 (문자열, 인코딩 이름)을 반환한다.
    utf-8, cp949, latin-1 순서로 시도한다. latin-1은 실패하지 않으므로 항상 결과가 있다.

    No newline translation: CRLF and a UTF-8 BOM survive a decode / encode round trip."""
    for enc in DECODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"  # pragma: no cover - latin-1 never fails


@dataclass
class CopyResult:
    """프론트엔드 복사 결과를 담는 클래스.
    ``copied``는 (원본 경로, 출력 루트 기준 대상 경로) 목록이다."""
    copied: list[tuple[str, str]] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)       # jsp, static별 복사한 파일 수
    rejected_web_inf: list[str] = field(default_factory=list)   # WEB-INF 안에 있어서 복사하지 않은 정적 파일
    errors: list[str] = field(default_factory=list)


def file_kind(rel: str, rules: ConvertRules) -> Optional[str]:
    """파일 경로로 종류를 정한다. JSP면 ``jsp``, 정적 파일이면 ``static``, 둘 다 아니면 None을 반환한다."""
    ext = PurePosixPath(rel).suffix.lower()
    if not ext:
        return None
    jsp_exts = {e.lower() for e in (rules.strs("files.jsp_extensions") or [".jsp"])}
    if ext in jsp_exts:
        return "jsp"
    if ext in {e.lower() for e in rules.strs("files.static_extensions")}:
        return "static"
    return None


def _in_web_inf(parts: list[str]) -> bool:
    return any(p.upper() == "WEB-INF" for p in parts[:-1])


def target_rel(rel: str, rules: ConvertRules) -> str:
    """원본 경로에서 정적 파일 폴더 안의 경로를 만든다.
    webapp 루트 폴더가 여러 번 나오면 마지막 것 뒤의 경로를 쓴다. 바로 뒤의 ``WEB-INF``와 그 뒤의 ``jsp`` 폴더는 뺀다.
    webapp 루트 폴더가 없으면 파일 이름만 남긴다."""
    parts = [p for p in rel.split("/") if p]
    roots = {r.lower() for r in rules.strs("files.webapp_roots")}
    last = max((i for i, p in enumerate(parts[:-1]) if p.lower() in roots), default=-1)
    if last < 0:
        return parts[-1] if parts else ""
    tail = parts[last + 1:]
    if len(tail) > 1 and tail[0].upper() == "WEB-INF":
        tail = tail[1:]
        # WEB-INF 바로 뒤의 jsp 폴더만 뺀다. 파일 이름이 jsp인 경우는 남긴다
        if len(tail) > 1 and tail[0] == "jsp":
            tail = tail[1:]
    return "/".join(tail)


def _walk(src_root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        for name in filenames:
            found.append((base / name).relative_to(src_root).as_posix())
    return sorted(found)


def _clean_rel(rel: str) -> Optional[str]:
    s = str(rel).replace("\\", "/")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return None
    parts = [p for p in s.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def copy_frontend(src_root: Path, out_root: Path, layout: Layout, rules: ConvertRules,
                  files: Optional[list[str]] = None) -> CopyResult:
    """원본의 JSP와 정적 파일을 ``layout.static_dir`` 아래로 복사한다.
    ``files``가 None이면 ``src_root``를 직접 순회한다. ``.git``, ``target`` 같은 폴더는 건너뛴다.
    대상 경로가 겹치면 정렬 순서로 먼저 온 파일만 복사하고 나머지는 ``errors``에 남긴다.
    이미 있는 파일은 덮어쓰지 않는다. 읽지 못한 파일도 ``errors``에 남기고 계속 진행한다."""
    src_root, out_root = Path(src_root), Path(out_root)
    result = CopyResult()
    if files is None:
        candidates = _walk(src_root) if src_root.is_dir() else []
        if not src_root.is_dir():
            result.errors.append(f"source folder not found: {src_root}")
    else:
        candidates = []
        for rel in files:
            clean = _clean_rel(rel)
            if clean is None:
                result.errors.append(f"{rel}: not a relative path inside the source folder")
                continue
            candidates.append(clean)
        candidates = sorted(set(candidates))

    static_dir = layout.static_dir.strip("/")
    taken: dict[str, str] = {}      # 대소문자를 무시한 대상 경로와 그 경로를 차지한 원본 경로
    for rel in candidates:
        kind = file_kind(rel, rules)
        if kind is None:
            continue
        parts = rel.split("/")
        if kind == "static" and _in_web_inf(parts):
            result.rejected_web_inf.append(rel)
            continue
        inner = target_rel(rel, rules)
        target = f"{static_dir}/{inner}" if static_dir else inner
        dest = out_root / target
        if not inner or not _inside(dest, out_root):
            result.errors.append(f"{rel}: target path {target} leaves the output folder")
            continue
        key = target.lower()
        if key in taken:
            result.errors.append(f"{rel}: same target {target} as {taken[key]}, not copied")
            continue
        src = src_root / rel
        # 심볼릭 링크 폴더나 파일을 거쳐 원본 폴더 밖을 가리키는 파일은 복사하지 않는다
        if not _inside(src, src_root):
            result.errors.append(f"{rel}: resolves to a file outside the source folder, not copied")
            continue
        try:
            # 이름 있는 파이프나 장치 파일은 읽으면 멈출 수 있다. 일반 파일만 복사한다
            if not is_regular(src):
                result.errors.append(f"{rel}: not a regular file, not copied")
                continue
            data = src.read_bytes()
        except OSError as e:
            result.errors.append(f"{rel}: cannot read ({e.__class__.__name__}: {e})")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            result.errors.append(f"{rel}: target {target} already exists, not overwritten")
            continue
        except OSError as e:
            result.errors.append(f"{rel}: cannot write {target} ({e.__class__.__name__}: {e})")
            continue
        taken[key] = rel
        result.copied.append((rel, target))
        result.by_kind[kind] = result.by_kind.get(kind, 0) + 1
    return result


# ---- JSP 안의 절대 경로 ----------------------------------------------------------

# include 지시자와 태그. 값은 (경로를 담은 속성 이름)이다
_JSP_PATH_TAGS = re.compile(r"<%@\s*include\b|<jsp:include\b|<jsp:forward\b|<c:import\b", re.I)
_JSP_PATH_ATTR = {"<%@": "file", "<jsp:include": "page", "<jsp:forward": "page", "<c:import": "url"}
_ATTR = re.compile(r"""\s*([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TAG_WINDOW = 4000
_MAX_PATH = 300


@dataclass
class JspPathResult:
    """JSP 안의 절대 경로를 고친 결과를 담는 클래스. 경로는 출력 루트 기준이다."""
    files_changed: list[str] = field(default_factory=list)
    replaced: int = 0
    queue: list[QueueItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _web_path(rel: str, rules: ConvertRules) -> Optional[str]:
    """webapp 루트 폴더 기준의 웹 경로를 ``/``로 시작하게 반환한다. webapp 루트가 없으면 None이다."""
    parts = [p for p in rel.split("/") if p]
    roots = {r.lower() for r in rules.strs("files.webapp_roots")}
    last = max((i for i, p in enumerate(parts[:-1]) if p.lower() in roots), default=-1)
    if last < 0:
        return None
    return "/" + "/".join(parts[last + 1:])


def _path_attrs(text: str) -> list[tuple[int, int, str]]:
    """include, forward, import의 경로 속성 값을 (시작, 끝, 값) 목록으로 반환한다."""
    found: list[tuple[int, int, str]] = []
    for m in _JSP_PATH_TAGS.finditer(text):
        head = m.group(0).lower()
        want = _JSP_PATH_ATTR["<%@" if head.startswith("<%@") else head]
        pos, stop = m.end(), min(len(text), m.end() + _TAG_WINDOW)
        while pos < stop:
            a = _ATTR.match(text, pos, stop)
            if not a:
                break
            if a.group(1).lower() == want:
                g = 2 if a.group(2) is not None else 3
                found.append((a.start(g), a.end(g), a.group(g)))
                break
            pos = a.end()
    return found


def fix_jsp_paths(out_root: Path, layout: Layout, rules: ConvertRules,
                  copied: list[tuple[str, str]]) -> JspPathResult:
    """복사한 JSP 안의 include, forward, import 절대 경로를 복사한 뒤의 경로로 바꾼다.
    - ``copied``로 원본 웹 경로와 새 웹 경로를 짝짓는다
    - 복사하지 않은 파일을 가리키는 경로는 그대로 두고 JSP마다 인수인계 항목을 하나 남긴다
    - ``<%``, ``${``, ``#{``가 들어간 값과 상대 경로는 건드리지 않는다
    - 복사한 파일이 아닌 화면 주소(``.mvc``)와 REST 주소(``/api`` 아래)는 컨트롤러 주소이므로 건드리지 않는다
    - ``/api`` 아래라도 JSP나 정적 파일 확장자가 있는데 복사한 파일이 아니면 인수인계 항목에 남긴다
    파일의 인코딩, 줄바꿈과 나머지 바이트는 그대로 둔다.

    The web path of a target is the part after the last webapp root in ``layout.static_dir`` (``/WEB-INF/static/{p}/…``).
    A query or fragment after the path is kept. A path that maps to itself is not counted."""
    out_root = Path(out_root)
    result = JspPathResult()
    static_dir = layout.static_dir.strip("/")
    static_web = _web_path(static_dir + "/x", rules)
    if static_web is None:
        result.errors.append(f"{static_dir}: no webapp folder in the static folder's path")
        return result
    static_web = static_web[:-2]
    moved: dict[str, str] = {}
    for src, target in copied:
        old = _web_path(src, rules)
        if old is not None and target.startswith(static_dir + "/"):
            moved.setdefault(old, static_web + target[len(static_dir):])
    limit = scan_limit(rules)
    page_suffix = rules.text("frontend.page_suffix", ".mvc")
    api_prefix = rules.text("frontend.api_prefix", "/api").rstrip("/") or "/api"
    for src, target in sorted(copied, key=lambda st: st[1]):
        if file_kind(target, rules) != "jsp":
            continue
        path = out_root / target
        try:
            if not is_regular(path):
                result.errors.append(f"{target}: not a regular file, not read")
                continue
            if path.stat().st_size > limit:
                result.errors.append(f"{target}: larger than the scan limit of {limit // 1024} KB, not read")
                continue
            raw = path.read_bytes()
        except OSError as e:
            result.errors.append(f"{target}: cannot read ({e.__class__.__name__}: {e})")
            continue
        text, enc = decode_source(raw)
        edits: list[tuple[int, int, str]] = []
        missing: list[str] = []
        for start, end, value in _path_attrs(text):
            if "<%" in value or "${" in value or "#{" in value:
                continue
            if not value.startswith("/") or value.startswith("//"):
                continue
            # 줄바꿈이 있거나 아주 긴 값은 경로가 아니다. 인수인계 이유에 파일의 글이 들어가지 않게 건너뛴다
            if "\n" in value or "\r" in value or len(value) > _MAX_PATH:
                continue
            cut = min((i for i in (value.find("?"), value.find("#")) if i >= 0), default=len(value))
            web, extra = value[:cut], value[cut:]
            new = moved.get(web)
            if new is None:
                # 복사한 파일이 아니면서 화면 주소(.mvc)면 컨트롤러 주소다. 그대로 둔다
                if page_suffix and web.endswith(page_suffix):
                    continue
                # API 경로 아래도 컨트롤러 주소다. 다만 JSP나 정적 파일 확장자가 있으면 없어진 파일로 본다
                if (web == api_prefix or web.startswith(api_prefix + "/")) and file_kind(web, rules) is None:
                    continue
                if web not in missing:
                    missing.append(web)
            elif new != web:
                edits.append((start, end, new + extra))
        rel_static = target[len(static_dir) + 1:] if target.startswith(static_dir + "/") else target
        if missing:
            result.queue.append(QueueItem(component=f"jsp_path::{rel_static}",
                                          reason="[JSP_PATH] " + ", ".join(missing),
                                          source_path=target, excerpt=text[:EXCERPT_CHARS]))
        if not edits:
            continue
        pieces, pos = [], 0
        for start, end, new in edits:
            pieces.append(text[pos:start])
            pieces.append(new)
            pos = end
        pieces.append(text[pos:])
        try:
            same = text.encode(enc) == raw
            data = "".join(pieces).encode(enc)
        except UnicodeEncodeError:
            same = False
        if not same:
            result.errors.append(f"{target}: the {enc} text does not reproduce the file's bytes, not rewritten")
            continue
        try:
            _write_in_place(path, data)
        except OSError as e:
            result.errors.append(f"{target}: cannot write ({e.__class__.__name__}: {e})")
            continue
        result.files_changed.append(target)
        result.replaced += len(edits)
    return result
