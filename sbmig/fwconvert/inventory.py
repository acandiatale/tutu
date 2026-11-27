"""원본 프로젝트의 파일을 종류별로 나누고 자바 클래스의 종류, 위험 점수, 전환 순서를 정하는 모듈.

Contract: docs/FW-CONVERT.md sections 2.1, 2.4 and 4 (the ``risk_score`` hand-over item).

File kinds, first match wins (content evidence before name rules):
  1. ``mapper``      path contains ``files.mapper_path_word``, extension in ``files.mapper_extensions`` and the head of
                     the file (``MAPPER_HEAD_BYTES``) has ``<mapper ... namespace=``. So a ``*-context.xml`` or an
                     ``x-service.xml`` under ``mybatis/`` that IS a mapper is a mapper; one that is not falls through.
  2. ``sql``         name ends with ``files.sql_extension`` (case ignored).
  3. ``ignored``     name in ``files.ignored_names``.
  4. ``service_xml`` name ends with ``files.service_xml_suffix``; it beats ``db_config``, so
                     ``datasource-service.xml`` is a service XML.
  5. ``db_config``   name matches a glob of ``files.db_config_names`` (case ignored).
  6. ``jsp``         ``.jsp`` only (``.jspf`` / ``.tag`` are ``other``).
  7. ``static``      extension in ``files.static_extensions``. Whether a static file sits in ``WEB-INF`` is the front-end
                     copy's business, not decided here.
  8. ``java``        ``.java``.
  9. ``other``       the rest, including a mapper-looking file under ``mybatis/`` without the namespace.

Only mapper candidates and ``.java`` files are read: a mapper candidate up to ``MAPPER_HEAD_BYTES``, a Java file up
to ``MAX_JAVA_BYTES`` (a longer one is classified on its head and noted in ``errors``). Symbolic links are never
followed (noted in ``errors``). Anything that is not a regular file (``os.lstat``: a named pipe, a device, a socket)
is left out of every list as ``<path>: not a regular file``, and every reader opens with ``O_NONBLOCK`` and checks
``fstat`` before reading (``NotRegularFile``), so a direct call never blocks on a FIFO either. A file that cannot be read is an ``errors`` entry ``<path>: <reason>`` and keeps the
kind its name gives (a mapper candidate that cannot be read is ``other``). Nothing here raises for bad input.

Component kinds use ``components.kinds`` (regexes over the raw source text, comments included, first row wins) plus
the literal class names of ``components.batch_markers`` in the ``batch`` row. An invalid regex from the overlay is
matched as a literal string. Risk = sum of the ``components.risk`` values whose key occurs literally, rounded to 4
places and capped at 1.0. Package, class name and imports are read from the text with comments and string literals
removed. The class name falls back to the file stem.

Linear time on hostile input: the kind regexes (``package .+\\.batch``, ``extends.*Dao``) backtrack over the rest of
the text from every start, so they are applied line by line to the first ``MATCH_LINE_CHARS`` characters of each line
(a pattern never spans lines; measured worst case for 2 MB of 1,000-character hostile lines: about 0.5 s). Risk keys
are plain substring searches over the whole text. Comments and strings are removed by a one-pass scanner
(``_strip_code``): the regex form was quadratic on an unclosed ``/*``. The mapper mark looks at most
``_MAPPER_TAG_CHARS`` characters past ``<mapper``. A folder ``os.walk`` cannot list is an ``errors`` entry
``<path>: cannot read the folder (<reason>)``."""
from __future__ import annotations

import fnmatch
import heapq
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from sbmig.fwconvert.model import QueueItem
from sbmig.fwconvert.rules import ConvertRules

# 들어가지 않는 폴더. 버전 관리, IDE 설정, 빌드 결과, 내려받은 패키지 폴더다
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", ".bzr", "CVS",
    ".idea", ".vscode", ".settings", ".metadata", ".gradle", ".mvn",
    "target", "build", "node_modules", "bower_components", "__pycache__",
})
KINDS = ("sql", "service_xml", "mapper", "db_config", "ignored", "jsp", "static", "java", "other")
# mapper인지 확인할 때 읽는 앞부분의 크기
MAPPER_HEAD_BYTES = 64 * 1024
# 자바 파일을 읽는 최대 크기. 넘으면 앞부분만으로 분류한다
MAX_JAVA_BYTES = 4 * 1024 * 1024
EXCERPT_CHARS = 2000
# 종류 정규식을 적용할 한 줄의 최대 길이. 더 긴 줄은 잘라서 본다
MATCH_LINE_CHARS = 1000

_MAPPER_TAG_CHARS = 2000
_MAPPER_MARK = re.compile(rb"<mapper\b[^>]{0,%d}?\bnamespace\s*=" % _MAPPER_TAG_CHARS)
_CODE_START = re.compile(r"""/[*/]|["']""")
_STRING = {q: re.compile(q + r"(?:\\.|[^" + q + r"\\\n])*" + q + "?") for q in "\"'"}
_PACKAGE = re.compile(r"^[ \t]*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT = re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)
_TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")


@dataclass
class Component:
    """자바 클래스 하나의 분류 결과."""
    path: str                       # 원본 루트 기준
    package: str = ""
    class_name: str = ""
    imports: list[str] = field(default_factory=list)
    kind: str = "unknown"
    risk: float = 0.0
    markers: list[str] = field(default_factory=list)

    @property
    def fqn(self) -> str:
        """패키지를 붙인 클래스 이름. 패키지가 없으면 클래스 이름만 반환한다."""
        return f"{self.package}.{self.class_name}" if self.package else self.class_name


@dataclass
class Inventory:
    """원본 프로젝트의 파일 목록을 종류별로 나눈 결과. 경로는 원본 루트 기준이고 정렬되어 있다."""
    sql: list[str] = field(default_factory=list)
    service_xml: list[str] = field(default_factory=list)
    mapper: list[str] = field(default_factory=list)
    db_config: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    jsp: list[str] = field(default_factory=list)
    static: list[str] = field(default_factory=list)
    java: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def files(self, kind: str) -> list[str]:
        """종류의 파일 목록을 반환한다. 모르는 종류면 ``ValueError``를 던진다."""
        if kind not in KINDS:
            raise ValueError(f"unknown file kind {kind!r}")
        return list(getattr(self, kind))

    def kind_of(self, path: str) -> str | None:
        """파일의 종류를 반환한다. 목록에 없는 파일이면 None이다."""
        for kind in KINDS:
            if path in getattr(self, kind):
                return kind
        return None


# ---- 파일 읽기 -----------------------------------------------------------------


def decode_bytes(data: bytes) -> tuple[str, str]:
    """바이트를 글로 바꾸고 쓴 인코딩과 함께 반환한다. utf-8, cp949, latin-1 순서로 시도한다."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc), ("utf-8" if enc == "utf-8-sig" else enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


class NotRegularFile(OSError):
    """일반 파일이 아닌 경로를 읽으려 할 때 던지는 예외. 이름 있는 파이프, 장치 파일, 소켓 같은 경우다."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"not a regular file: {path}")
        self.strerror = "not a regular file"


def _read_head(path: Path, limit: int) -> tuple[bytes, bool]:
    # 파이프를 열 때 기다리지 않도록 non-blocking으로 열고, 연 뒤에 일반 파일인지 확인한다
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(path)
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1
            data = fh.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    return data[:limit], len(data) > limit


def _decode_head(data: bytes, truncated: bool) -> str:
    if truncated:
        # 잘린 끝의 멀티바이트 문자 때문에 utf-8이 실패하지 않도록 끝 몇 바이트를 버리고 시도한다
        for cut in range(4):
            try:
                return data[: len(data) - cut].decode("utf-8")
            except UnicodeDecodeError:
                continue
    return decode_bytes(data)[0]


def read_java_text(path: Path) -> tuple[str, str]:
    """자바 파일을 읽어 (글, 오류)를 반환한다. 읽지 못하면 글은 빈 문자열이고 오류에 이유가 있다.
    ``MAX_JAVA_BYTES``보다 크면 앞부분만 읽고 오류에 그 사실을 남긴다."""
    try:
        data, truncated = _read_head(path, MAX_JAVA_BYTES)
    except NotRegularFile:
        return "", "not a regular file"
    except OSError as e:
        return "", f"cannot read: {e.strerror or e}"
    note = f"larger than {MAX_JAVA_BYTES} bytes, classified on its head" if truncated else ""
    return _decode_head(data, truncated), note


# ---- 파일 종류 -----------------------------------------------------------------


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i > 0 else ""


def _name_kind(rel: str, name: str, rules: ConvertRules) -> str:
    low = name.lower()
    sql_ext = rules.text("files.sql_extension").lower()
    if sql_ext and low.endswith(sql_ext):
        return "sql"
    if low in {n.lower() for n in rules.strs("files.ignored_names")}:
        return "ignored"
    svc = rules.text("files.service_xml_suffix").lower()
    if svc and low.endswith(svc):
        return "service_xml"
    if any(fnmatch.fnmatchcase(low, g.lower()) for g in rules.strs("files.db_config_names")):
        return "db_config"
    ext = _ext(name)
    if ext == ".jsp":
        return "jsp"
    if ext in {e.lower() for e in rules.strs("files.static_extensions")}:
        return "static"
    if ext == ".java":
        return "java"
    return "other"


def _is_mapper_candidate(rel: str, name: str, rules: ConvertRules) -> bool:
    word = rules.text("files.mapper_path_word").lower()
    exts = {e.lower() for e in rules.strs("files.mapper_extensions")}
    return bool(word) and word in rel.lower() and _ext(name) in exts


def _walk(root: Path, errors: list[str]):
    def unreadable(err: OSError) -> None:
        # 목록을 읽지 못한 폴더는 건너뛰고 errors에 남긴다
        where = Path(err.filename) if err.filename else root
        try:
            rel = where.relative_to(root).as_posix() or "."
        except ValueError:
            rel = str(where)
        errors.append(f"{rel}: cannot read the folder ({err.strerror or err})")

    for cur, dirs, files in os.walk(root, onerror=unreadable, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        base = Path(cur)
        for d in dirs:
            if (base / d).is_symlink():
                yield base / d, True
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
        for f in sorted(files):
            yield base / f, False


def scan_tree(src_root: Path, rules: ConvertRules) -> Inventory:
    """원본 프로젝트를 한 번 순회해서 파일을 종류별로 나누고 자바 클래스를 분류한다.
    ``SKIP_DIRS``의 폴더에는 들어가지 않는다. 심볼릭 링크는 따라가지 않고 ``errors``에 남긴다.
    읽지 못한 파일과 폴더는 예외를 던지지 않고 ``errors``에 남긴다."""
    root = Path(src_root)
    inv = Inventory()
    if not root.is_dir():
        inv.errors.append(f".: source root is not a directory: {root}")
        return inv
    buckets: dict[str, list[str]] = {k: [] for k in KINDS}
    for path, is_link_dir in _walk(root, inv.errors):
        rel = path.relative_to(root).as_posix()
        if is_link_dir or path.is_symlink():
            inv.errors.append(f"{rel}: symbolic link not followed")
            continue
        try:
            mode = os.lstat(path).st_mode
        except OSError as e:
            inv.errors.append(f"{rel}: cannot read: {e.strerror or e}")
            continue
        if not stat.S_ISREG(mode):
            # 파이프, 장치 파일, 소켓은 목록에 넣지 않는다. 읽으면 멈출 수 있다
            inv.errors.append(f"{rel}: not a regular file")
            continue
        name = path.name
        if _is_mapper_candidate(rel, name, rules):
            try:
                head, _ = _read_head(path, MAPPER_HEAD_BYTES)
            except OSError as e:
                inv.errors.append(f"{rel}: cannot read: {e.strerror or e}")
                buckets["other"].append(rel)
                continue
            if _MAPPER_MARK.search(head):
                buckets["mapper"].append(rel)
                continue
        kind = _name_kind(rel, name, rules)
        buckets[kind].append(rel)
        if kind == "java":
            text, note = read_java_text(path)
            if note:
                inv.errors.append(f"{rel}: {note}")
            if text or not note:
                inv.components.append(_component(rel, text, rules))
    for kind in KINDS:
        setattr(inv, kind, sorted(buckets[kind]))
    inv.components.sort(key=lambda c: c.path)
    inv.errors.sort()
    return inv


# ---- 자바 클래스 분류 ------------------------------------------------------------


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error:
        # 정규식으로 읽을 수 없는 값은 문자열 그대로 찾는다
        return re.compile(re.escape(pattern))


def _strip_code(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _CODE_START.search(text, i)
        if not m:
            out.append(text[i:])
            break
        j = m.start()
        out.append(text[i:j])
        tok = m.group(0)
        if tok == "/*":
            end = text.find("*/", j + 2)
            i = n if end < 0 else end + 2
        elif tok == "//":
            end = text.find("\n", j)
            i = n if end < 0 else end
        else:
            # 닫히지 않은 문자열은 줄 끝까지를 문자열로 본다
            i = _STRING[tok].match(text, j).end()
        out.append(" ")
    return "".join(out)


def _match_lines(text: str) -> list[str]:
    return [line[:MATCH_LINE_CHARS] for line in text.splitlines()]


def classify_component(text: str, rules: ConvertRules) -> tuple[str, float, list[str]]:
    """자바 소스의 종류, 위험 점수, 찾은 위험 표시를 반환한다.
    종류는 ``components.kinds`` 표에서 위부터 처음 맞는 줄로 정한다. 맞는 줄이 없으면 ``unknown``이다.
    정규식은 소스의 줄마다 적용하고, 긴 줄은 ``MATCH_LINE_CHARS``까지만 본다.
    ``components.batch_markers``의 클래스 이름이 있으면 ``batch``로 본다.
    위험 점수는 본문에 들어 있는 ``components.risk`` 항목의 합이고 1.0을 넘지 않는다."""
    src = str(text or "")
    kind = "unknown"
    batch_markers = [re.escape(m) for m in rules.strs("components.batch_markers") if m]
    lines = _match_lines(src)
    for row in rules.get("components.kinds", []) or []:
        if not isinstance(row, dict):
            continue
        patterns = [str(p) for p in (row.get("patterns") or [])]
        if row.get("kind") == "batch":
            patterns += batch_markers
        # 정규식은 줄마다 적용한다. 긴 줄은 MATCH_LINE_CHARS까지만 본다
        if any(rx.search(line) for rx in map(_compile, filter(None, patterns)) for line in lines):
            kind = str(row.get("kind") or "unknown")
            break
    total, markers = 0.0, []
    risk = rules.get("components.risk", {}) or {}
    for key, value in (risk.items() if isinstance(risk, dict) else []):
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if key and str(key) in src:
            total += score
            markers.append(str(key))
    return kind, round(min(total, 1.0), 4), markers


def _component(rel: str, text: str, rules: ConvertRules) -> Component:
    code = _strip_code(text)
    pkg = _PACKAGE.search(code)
    decl = _TYPE_DECL.search(code)
    stem = rel.rsplit("/", 1)[-1]
    stem = stem[: -len(".java")] if stem.lower().endswith(".java") else stem
    kind, risk, markers = classify_component(text, rules)
    return Component(path=rel, package=pkg.group(1) if pkg else "", class_name=decl.group(1) if decl else stem,
                     imports=_IMPORT.findall(code), kind=kind, risk=risk, markers=markers)


# ---- 전환 순서 -----------------------------------------------------------------


def _depends_on(comp: Component, by_fqn: dict[str, list[Component]], by_pkg: dict[str, list[Component]]) -> set[str]:
    deps: set[str] = set()
    for imp in comp.imports:
        if imp.endswith(".*"):
            targets = by_pkg.get(imp[:-2], [])
            # 정적 import의 와일드카드는 클래스 이름으로 끝난다
            targets = targets or by_fqn.get(imp[:-2], [])
        else:
            targets = by_fqn.get(imp, [])
            if not targets and "." in imp:
                targets = by_fqn.get(imp.rsplit(".", 1)[0], [])
        deps.update(t.path for t in targets if t.path != comp.path)
    return deps


def _topo(group: list[Component]) -> list[Component]:
    by_path = {c.path: c for c in group}
    by_fqn: dict[str, list[Component]] = {}
    by_pkg: dict[str, list[Component]] = {}
    for c in group:
        by_fqn.setdefault(c.fqn, []).append(c)
        by_pkg.setdefault(c.package, []).append(c)
    deps = {c.path: _depends_on(c, by_fqn, by_pkg) for c in group}
    users: dict[str, list[str]] = {p: [] for p in by_path}
    for p, ds in deps.items():
        for d in ds:
            users[d].append(p)
    waiting = {p: len(ds) for p, ds in deps.items()}
    ready = [p for p, n in waiting.items() if n == 0]
    heapq.heapify(ready)
    out: list[str] = []
    while ready:
        p = heapq.heappop(ready)
        out.append(p)
        for u in users[p]:
            waiting[u] -= 1
            if waiting[u] == 0:
                heapq.heappush(ready, u)
    # 순환 참조로 남은 클래스는 경로 순서로 뒤에 붙인다
    done = set(out)
    out += sorted(p for p in by_path if p not in done)
    return [by_path[p] for p in out]


def conversion_order(components: list[Component], rules: ConvertRules) -> list[Component]:
    """클래스를 전환할 순서로 정렬해서 반환한다.
    종류는 ``components.order`` 순서다. 표에 없는 종류는 이름 순서로 맨 뒤에 둔다.
    같은 종류 안에서는 import로 참조하는 클래스가 먼저 온다. 순서가 같으면 경로 순서다.
    순환 참조로 정할 수 없는 클래스는 경로 순서로 뒤에 붙인다."""
    order = [str(k) for k in rules.strs("components.order")]
    groups: dict[str, list[Component]] = {}
    for c in components:
        groups.setdefault(c.kind, []).append(c)
    kinds = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    out: list[Component] = []
    for k in kinds:
        out += _topo(groups[k])
    return out


# ---- 인수인계 항목 ---------------------------------------------------------------


def risk_queue(components: list[Component], src_root: Path, rules: ConvertRules) -> list[QueueItem]:
    """위험 점수가 ``components.risk_threshold`` 이상인 클래스마다 인수인계 항목을 하나씩 만들어 반환한다.
    항목에는 원본 소스의 앞 2,000자를 넣는다. 소스를 읽지 못하면 ``last_error``에 이유를 넣는다."""
    try:
        threshold = float(rules.get("components.risk_threshold", 0.7))
    except (TypeError, ValueError):
        threshold = 0.7
    items: list[QueueItem] = []
    for c in sorted(components, key=lambda c: c.path):
        if c.risk < threshold:
            continue
        text, note = read_java_text(Path(src_root) / c.path)
        error = note if not text else ""
        reason = f"risk_score>={threshold:g}" + (" " + ", ".join(c.markers) if c.markers else "")
        items.append(QueueItem(component=c.fqn, reason=reason, source_path=c.path, last_error=error,
                               excerpt=text[:EXCERPT_CHARS]))
    return items
