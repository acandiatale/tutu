"""JSP / 태그 파일 안의 javax EE 이름을 jakarta 로 바꾸는 재구성 마지막 단계(v2.1h, 현업 결정 A5 "JSP 도 javax →
jakarta 로"). 왜 필요한가: OpenRewrite 는 JSP 를 파싱하지 않고(``openrewrite.exclusions``) Maven 빌드도 JSP 를
컴파일하지 않아 빌드는 통과해도 Tomcat 10+ 에서 실행 시 실패한다. 흐름상 위치: ``Restructurer.apply`` 가 모드의
일(이동·pom)을 끝낸 뒤 ``layout.jsp_jakarta_rename`` 이 켜져 있으면 :func:`rename_jsp_files` 를 부른다. 핵심
진입점: :func:`rename_jsp_javax` (텍스트 하나, 순수 함수), :func:`rename_jsp_files` (워크스페이스 전체).
지킬 것: 분석기와 같은 영역(``sbmig.analyze.jsp.jsp_code_spans``)과 같은 표(``jakarta_packages.yml``)만 쓴다 —
스크립틀릿 안 자바 문자열·주석, taglib uri, HTML, EL 은 건드리지 않는다. 파일은 자기 인코딩·줄바꿈 그대로
다시 쓰고, 바뀐 것이 있을 때만 쓴다. 두 번 돌려도 두 번째는 아무것도 바꾸지 않는다.

JSP javax → jakarta rename (v2.1h, A5): the javax EE names of the analyzer's package table inside the code regions
of every JSP / JSPF / JSPX / tag / tagx file (page / tag `import`, useBean `class` / `type`, scriptlets) become
`jakarta.…` (the table's `to`; `javax.security.cert` → `java.security.cert`). Java string literals and comments
inside scriptlets, taglib URIs (JSTL 3 still serves the `java.sun.com` URIs), HTML and EL are never touched.

Encodings: a file is decoded with the encoding its page / tag directive names (`pageEncoding`, else the
`contentType` charset, else the XML declaration of a JSP document), then UTF-8 (BOM aware), MS949 (`cp949`) and
EUC-KR — the first one that decodes the whole file strictly AND encodes back to the identical bytes wins, so every
byte outside the renamed names is preserved (line ends included). A file no candidate round-trips is left alone and
reported as failed when it names a javax EE type.
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

# the analyzer's patterns, shared (not copied) so the rename and `SourceStats.jsp_javax_files` never drift apart:
# a javax name (`javax.servlet.http.Cookie`, `javax.servlet.*`) and the Java comments / literals masked in scriptlets
JAVAX_TOKEN_RE: re.Pattern = _jsp._JAVAX_TOKEN_RE
JAVA_LITERAL_RE: re.Pattern = _jsp._JAVA_LIT_RE

# tag files (.tag / .tagx) name Java types in `<%@ tag import=… %>` and scriptlets exactly like JSPs
RENAME_SUFFIXES: tuple[str, ...] = tuple(dict.fromkeys((*JSP_SUFFIXES, ".tag", ".tagx")))
DEFAULT_ENCODINGS: tuple[str, ...] = ("utf-8", "cp949", "euc-kr")
SKIP_DIRS = frozenset({".git", ".svn", ".hg", "node_modules"})     # anywhere in the tree
SKIP_ROOT_DIRS = frozenset({"out"})                                # sbmig / IDE output at the walk root only

_UTF8_BOM = b"\xef\xbb\xbf"
_PAGE_ENCODING_RE = re.compile(
    r"""(?:<%@\s*(?:page|tag)\b|<jsp:directive\.(?:page|tag)\b)[^>]*?\bpageEncoding\s*=\s*["']\s*([\w.:-]+)""")
_CONTENT_TYPE_RE = re.compile(
    r"""(?:<%@\s*(?:page|tag)\b|<jsp:directive\.(?:page|tag)\b)[^>]*?\bcontentType\s*=\s*["'][^"']*?"""
    r"""\bcharset\s*=\s*([\w.:-]+)""", re.IGNORECASE)
_XML_DECL_RE = re.compile(r"""^\s*<\?xml\b[^>]*?\bencoding\s*=\s*["']([\w.:-]+)["']""")


@lru_cache(maxsize=1)
def jakarta_targets() -> dict[str, str]:
    """javax → jakarta 표(``sbmig/analyze/jakarta_packages.yml``)의 원래 패키지 → 새 패키지. 한 번만 읽는다.
    표에 ``to`` 가 없으면 ``jakarta.`` + 나머지(:func:`rename_jsp_javax` 가 처리).

    from-package → to-package of the analyzer's javax → jakarta table (cached; missing file → empty)."""
    table = Path(_jsp.__file__).resolve().with_name("jakarta_packages.yml")
    try:
        doc = yaml.safe_load(table.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(p["from"]): str(p["to"]) for p in doc.get("packages") or [] if p.get("from") and p.get("to")}


def _target_name(token: str, by_from: dict[str, bool], targets: dict[str, str]) -> str | None:
    """``token`` 의 새 이름: 가장 긴 표 접두어(``javax_ee_token`` 과 같은 순서)를 그 ``to`` 로 바꾼다."""
    parts = token.split(".")
    for i in range(len(parts) - 1, 1, -1):
        frm = ".".join(parts[:i])
        if frm in by_from:
            to = targets.get(frm) or "jakarta" + frm[len("javax"):]
            return to + token[len(frm):]
    return None


def rename_jsp_javax(text: str, rules: Iterable[tuple[str, bool]] | None = None) -> tuple[str, int]:
    """JSP / 태그 파일 텍스트의 코드 영역(import, useBean class / type, 스크립틀릿) 안에서 표에 있는 javax EE
    이름을 jakarta 로 바꾼 텍스트와 바꾼 참조 수. 스크립틀릿 안 자바 문자열·주석, 영역 밖(HTML, EL, taglib
    uri)은 그대로. ``rules`` = ``jakarta_rules()`` 형식의 (패키지, 하위 포함) 목록(None = 분석기 표).

    `text` with every table javax EE name inside the code regions renamed (`javax.servlet.http.Cookie` →
    `jakarta.servlet.http.Cookie`, `javax.servlet.*` → `jakarta.servlet.*`), and the number of renamed references.
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
        # 문자열·주석을 같은 길이의 공백으로 가린다(위치가 그대로라 원문에서 바로 바꿀 수 있다) — "javax.servlet"
        # 이라는 문자열은 참조가 아니다
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
    """인코딩 이름 정규화(``EUC-KR`` → ``euc_kr``); 모르는 이름이면 None."""
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def declared_encoding(data: bytes) -> str | None:
    """JSP 가 스스로 밝힌 인코딩: page / tag 지시자의 ``pageEncoding`` → ``contentType`` 의 charset → JSP
    문서(XML)의 선언 순서(JSP 명세의 우선순위). 없거나 모르는 이름이면 None.

    The encoding a JSP declares: `pageEncoding` of a page / tag directive, else the `contentType` charset, else the
    XML declaration (JSP documents); None when absent or unknown. Directives are ASCII, so latin-1 sniffing is safe."""
    head = data.decode("latin-1")
    for rx in (_PAGE_ENCODING_RE, _CONTENT_TYPE_RE, _XML_DECL_RE):
        m = rx.search(head)
        if m and (enc := _lookup(m.group(1))):
            return enc
    return None


def decode_jsp(data: bytes, encodings: Iterable[str] = DEFAULT_ENCODINGS) -> tuple[str, str] | None:
    """JSP 바이트 → (텍스트, 인코딩). BOM 이 있으면 utf-8-sig, 아니면 선언된 인코딩 → ``encodings`` 순서로
    엄격하게 풀고 다시 인코딩해 같은 바이트가 나오는 첫 인코딩. 아무것도 안 되면 None(그 파일은 건드리지 않는다).

    (text, encoding) of a JSP's bytes, or None: the BOM, then the declared encoding, then `encodings`; the first
    one that decodes strictly and re-encodes to the identical bytes (newlines kept as they are)."""
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
            if text.encode(enc) != data:     # never write back bytes that differ outside the renamed names
                continue
        except (UnicodeError, LookupError):
            continue
        return text, enc
    return None


@dataclass
class JspRenameResult:
    """워크스페이스 JSP 이름 바꾸기 한 번의 결과: 본 파일 수, 바뀐 파일별 참조 수(``renamed``), 못 바꾼 파일과
    이유(``failed``).

    One workspace pass: files scanned, `renamed` = rel path → references renamed (changed files only, sorted),
    `failed` = rel path → reason (unreadable / no round-tripping encoding / write error)."""
    scanned: int = 0
    renamed: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def files(self) -> int:
        """바뀐 파일 수."""
        return len(self.renamed)

    @property
    def refs(self) -> int:
        """바꾼 참조 수 합계."""
        return sum(self.renamed.values())


def jsp_files(ws: Any, root: str = "") -> list[str]:
    """``root`` (워크스페이스 기준, "" = 전체) 아래의 JSP / JSPF / JSPX / tag / tagx 파일(정렬). ``.git``·
    ``node_modules``·Maven 빌드 산출물 ``target/``·루트의 ``out/`` 은 보지 않는다.

    Workspace-relative JSP / tag files below `root` (sorted), outside `.git` / `.svn` / `node_modules`, Maven build
    output (`Workspace.walk` prunes `target/`) and a top-level `out/` (sbmig output)."""
    base = root.strip("/")
    out: list[str] = []
    for rel_dir, dirnames, filenames in ws.walk():
        inside = not base or rel_dir == base or rel_dir.startswith(base + "/")
        if not inside and rel_dir and not base.startswith(rel_dir + "/"):
            dirnames[:] = []                   # neither below `root` nor on the way down to it
            continue
        # os.walk 의 목록을 그 자리에서 줄여야 그 아래로 내려가지 않는다
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
    """워크스페이스(또는 ``root`` 아래)의 모든 JSP / 태그 파일에 :func:`rename_jsp_javax` 를 적용한다. 바뀐
    파일만 같은 인코딩·줄바꿈으로 다시 쓴다. 파일 하나가 실패해도 경고 로그를 남기고 다음 파일로 간다.

    Apply :func:`rename_jsp_javax` to every JSP / tag file of the workspace (below `root`); a changed file is
    written back in the encoding it was read with (`decode_jsp`), untouched files are never rewritten. A failure on
    one file is logged and recorded in `failed`, never raised."""
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
            # 어떤 인코딩으로도 바이트가 그대로 돌아오지 않는 파일: 쓰면 한글이 깨질 수 있어 두고, javax 가
            # 보이면(지시자·이름은 ASCII) 실패로 센다
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
        except Exception as e:  # noqa: BLE001 - one bad file never fails the restructure
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
