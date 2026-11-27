"""JSP와 태그 파일 안의 javax EE 이름을 jakarta로 바꾸는 모듈.
OpenRewrite가 JSP를 파싱하지 않기 때문에 여기서 따로 바꾼다.

Maven does not compile JSPs either: left as they are, the build passes and Tomcat 10+ fails at runtime.
Called after the file moves and the pom work when `layout.jsp_jakarta_rename` is on. Uses the analyzer's
code regions (`jsp_code_spans`) and its table (`jakarta_packages.yml`). Strings and comments inside
scriptlets, taglib URIs, HTML and EL are never changed. Only changed files are written, in their own
encoding and line endings; a second run changes nothing.

Code regions: page / tag `import`, useBean `class` / `type`, scriptlets. The table's `to` is used
(`javax.security.cert` -> `java.security.cert`). Taglib URIs stay because JSTL 3 still serves the
`java.sun.com` URIs. Also called by the orchestrator for a reduced-scope run (no restructure).

Encodings: the one the page / tag directive names (`pageEncoding`, else the `contentType` charset, else the
XML declaration of a JSP document), then UTF-8 (BOM aware), MS949 (`cp949`) and EUC-KR. The first one that
decodes strictly AND encodes back to the identical bytes wins: every byte outside the renamed names is kept.
A file no candidate round-trips is left alone and reported as failed when it names a javax EE type.
"""
from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from sbmig.analyze import jsp as _jsp
from sbmig.analyze.jsp import JSP_SUFFIXES, jakarta_rules, javax_ee_token, jsp_code_spans

log = logging.getLogger("sbmig.restructure")

# 분석기의 패턴을 복사하지 않고 그대로 쓴다. 분석기의 javax 감지 결과와 어긋나지 않게 하기 위해서다
JAVAX_TOKEN_RE: re.Pattern = _jsp._JAVAX_TOKEN_RE
JAVA_LITERAL_RE: re.Pattern = _jsp._JAVA_LIT_RE

# 태그 파일도 JSP처럼 tag import와 스크립틀릿에 Java 타입을 쓴다
RENAME_SUFFIXES: tuple[str, ...] = tuple(dict.fromkeys((*JSP_SUFFIXES, ".tag", ".tagx")))
DEFAULT_ENCODINGS: tuple[str, ...] = ("utf-8", "cp949", "euc-kr")
SKIP_DIRS = frozenset({".git", ".svn", ".hg", "node_modules"})     # 어느 깊이에 있든 뺀다
SKIP_ROOT_DIRS = frozenset({"out"})                                # 검사 루트 바로 아래에 있을 때만 뺀다

_UTF8_BOM = b"\xef\xbb\xbf"
_PAGE_ENCODING_RE = re.compile(
    r"""(?:<%@\s*(?:page|tag)\b|<jsp:directive\.(?:page|tag)\b)[^>]*?\bpageEncoding\s*=\s*["']\s*([\w.:-]+)""")
_CONTENT_TYPE_RE = re.compile(
    r"""(?:<%@\s*(?:page|tag)\b|<jsp:directive\.(?:page|tag)\b)[^>]*?\bcontentType\s*=\s*["'][^"']*?"""
    r"""\bcharset\s*=\s*([\w.:-]+)""", re.IGNORECASE)
_XML_DECL_RE = re.compile(r"""^\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([\w.:-]+)["']""")


@lru_cache(maxsize=1)
def jakarta_targets() -> dict[str, str]:
    """``jakarta_packages.yml`` 표에서 {원래 패키지: 새 패키지}를 반환한다. 표는 한 번만 읽는다.
    파일이 없으면 빈 사전이다. ``to``가 없는 항목은 사전에 넣지 않는다. 바꿀 때 ``javax``를 ``jakarta``로 바꿔 쓴다."""
    table = Path(_jsp.__file__).resolve().with_name("jakarta_packages.yml")
    try:
        doc = yaml.safe_load(table.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(p["from"]): str(p["to"]) for p in doc.get("packages") or [] if p.get("from") and p.get("to")}


def _target_name(token: str, by_from: dict[str, bool], targets: dict[str, str]) -> str | None:
    """``token``의 새 이름을 반환한다. 표에 없으면 None을 반환한다.
    표에서 가장 길게 맞는 접두어를 그 항목의 ``to``로 바꾼다. 고르는 순서는 ``javax_ee_token``과 같다."""
    parts = token.split(".")
    for i in range(len(parts) - 1, 1, -1):
        frm = ".".join(parts[:i])
        if frm in by_from:
            to = targets.get(frm) or "jakarta" + frm[len("javax"):]
            return to + token[len(frm):]
    return None


def rename_jsp_javax(text: str, rules: Iterable[tuple[str, bool]] | None = None) -> tuple[str, int]:
    """JSP 텍스트의 코드 영역에서 표에 있는 javax EE 이름을 jakarta로 바꾼다.
    (바꾼 텍스트, 바꾼 참조 수)를 반환한다. ``rules``가 None이면 분석기의 표를 쓴다.
    스크립틀릿 안의 문자열과 주석, 코드 영역 밖의 HTML, EL, taglib uri는 그대로 둔다.

    Idempotent: a renamed text has nothing left to rename."""
    rules = tuple(rules) if rules is not None else jakarta_rules()
    if "javax" not in text or not rules:
        return text, 0
    by_from = dict(rules)
    targets = jakarta_targets()
    out: list[str] = []
    pos = n = 0
    for s, e, kind in jsp_code_spans(text):
        chunk = text[s:e]
        # 문자열과 주석은 같은 길이의 공백으로 가린다. "javax.servlet" 같은 문자열은 참조가 아니다.
        # 위치는 그대로라 원문에서 바로 바꿀 수 있다
        scan = JAVA_LITERAL_RE.sub(lambda m: " " * len(m.group(0)), chunk) if kind == "code" else chunk
        for m in JAVAX_TOKEN_RE.finditer(scan):
            token = m.group(0)
            if not javax_ee_token(token, rules):
                continue
            new = _target_name(token, by_from, targets)
            if not new or new == token:
                continue
            out.append(text[pos:s + m.start()])
            out.append(new)
            pos = s + m.end()
            n += 1
    if not n:
        return text, 0
    out.append(text[pos:])
    return "".join(out), n


def _lookup(name: str | None) -> str | None:
    """인코딩 이름을 파이썬 코덱 이름으로 바꿔 반환한다. ``EUC-KR``은 ``euc_kr``이 된다. 모르면 None이다."""
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def declared_encoding(data: bytes) -> str | None:
    """JSP 파일이 선언한 인코딩을 반환한다. 선언이 없거나 모르는 이름이면 None을 반환한다.
    JSP 명세의 순서대로 ``pageEncoding``, ``contentType``의 charset, XML 선언을 본다.

    Directives are ASCII, so latin-1 sniffing is safe."""
    head = data.decode("latin-1")
    for rx in (_PAGE_ENCODING_RE, _CONTENT_TYPE_RE, _XML_DECL_RE):
        m = rx.search(head)
        if m and (enc := _lookup(m.group(1))):
            return enc
    return None


def decode_jsp(data: bytes, encodings: Iterable[str] = DEFAULT_ENCODINGS) -> tuple[str, str] | None:
    """JSP 바이트를 디코딩해 (텍스트, 인코딩)을 반환한다. 맞는 인코딩이 없으면 None을 반환한다.
    BOM, 선언된 인코딩, ``encodings`` 순으로 시도한다.
    엄격하게 디코딩한 뒤 다시 인코딩해 같은 바이트가 나오는 첫 인코딩을 쓴다."""
    cands: list[str] = []
    if data.startswith(_UTF8_BOM):
        cands.append("utf-8-sig")
    declared = declared_encoding(data)
    for enc in (declared, *encodings):
        if enc and (norm := _lookup(enc)) and norm not in cands:
            cands.append(norm)
    for enc in cands:
        try:
            text = data.decode(enc)
            if text.encode(enc) != data:     # 바꾼 이름 밖의 바이트가 달라지면 안 된다
                continue
        except (UnicodeError, LookupError):
            continue
        return text, enc
    return None


@dataclass
class JspRenameResult:
    """작업 공간 전체의 JSP 이름 변경 결과를 담는 클래스.
    검사한 파일 수, 바뀐 파일별 참조 수, 바꾸지 못한 파일과 이유를 담는다.

    `failed` reasons: read error / unknown encoding (no round-tripping candidate) / write error."""
    scanned: int = 0
    renamed: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def files(self) -> int:
        """javax 참조를 바꾼 파일 수를 반환한다."""
        return len(self.renamed)

    @property
    def refs(self) -> int:
        """모든 파일에서 바꾼 참조 수의 합계를 반환한다."""
        return sum(self.renamed.values())


def jsp_files(ws: Any, root: str = "") -> list[str]:
    """``root`` 아래의 JSP와 태그 파일 목록을 정렬해 반환한다. ``root``가 비어 있으면 작업 공간 전체다.
    ``.git``, ``node_modules``, ``target/``, 루트의 ``out/``은 뺀다.

    `target/` is pruned by `Workspace.walk` itself."""
    base = root.strip("/")
    out: list[str] = []
    for rel_dir, dirnames, filenames in ws.walk():
        inside = not base or rel_dir == base or rel_dir.startswith(base + "/")
        if not inside and rel_dir and not base.startswith(rel_dir + "/"):
            dirnames[:] = []                   # root 아래도 아니고 root로 가는 길도 아니다
            continue
        # os.walk 목록을 그 자리에서 줄여야 제외한 디렉토리 안으로 내려가지 않는다
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not (rel_dir == base and d in SKIP_ROOT_DIRS)]
        if not inside:
            continue
        for name in filenames:
            if name.lower().endswith(RENAME_SUFFIXES):
                out.append(f"{rel_dir}/{name}" if rel_dir else name)
    return sorted(out)


def rename_jsp_files(ws: Any, root: str = "", *, rules: Iterable[tuple[str, bool]] | None = None,
                     encodings: Iterable[str] = DEFAULT_ENCODINGS) -> JspRenameResult:
    """``root`` 아래의 모든 JSP와 태그 파일에 :func:`rename_jsp_javax`를 적용한다.
    바뀐 파일만 읽은 인코딩 그대로 다시 쓴다.
    파일 하나가 실패하면 경고 로그와 ``failed``에 남기고 다음 파일로 넘어간다. 예외를 던지지 않는다."""
    rules = tuple(rules) if rules is not None else jakarta_rules()
    encodings = tuple(encodings)
    res = JspRenameResult()
    for rel in jsp_files(ws, root):
        res.scanned += 1
        try:
            data = ws.abs(rel).read_bytes()
        except OSError as e:
            log.warning("JSP javax → jakarta: %s not read: %s", rel, e)
            res.failed[rel] = f"read error: {e}"
            continue
        if b"javax" not in data:
            continue
        decoded = decode_jsp(data, encodings)
        if decoded is None:
            # 원래 바이트로 되돌릴 인코딩이 없으면 그대로 둔다. 다시 쓰면 한글이 깨진다.
            # javax가 보이면 실패로 센다
            if _jsp.jsp_javax_refs(data.decode("latin-1"), rules):
                log.warning("JSP javax → jakarta: %s skipped: no encoding of %s round-trips its bytes",
                            rel, ", ".join(encodings))
                res.failed[rel] = "unknown encoding"
            continue
        text, enc = decoded
        try:
            new, n = rename_jsp_javax(text, rules)
            if not n:
                continue
            ws.abs(rel).write_bytes(new.encode(enc))
        except Exception as e:  # noqa: BLE001 - 파일 하나 때문에 재구성을 실패시키지 않는다
            log.warning("JSP javax → jakarta: %s not renamed: %s: %s", rel, type(e).__name__, e)
            res.failed[rel] = f"{type(e).__name__}: {e}"
            continue
        log.debug("JSP javax → jakarta: %s: %d reference(s) (%s)", rel, n, enc)
        res.renamed[rel] = n
    return res


__all__ = [
    "DEFAULT_ENCODINGS",
    "JAVAX_TOKEN_RE",
    "JAVA_LITERAL_RE",
    "RENAME_SUFFIXES",
    "JspRenameResult",
    "declared_encoding",
    "decode_jsp",
    "jakarta_targets",
    "jsp_files",
    "rename_jsp_files",
    "rename_jsp_javax",
]
