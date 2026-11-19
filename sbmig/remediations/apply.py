"""승인된 버전 변경을 수정본의 pom에 적용하고 결과를 검증하는 모듈.
승인된 버전 문자열만 바꿔 넣은 뒤, 바뀐 것이 그것뿐인지와 Maven이 계산한 의존성 버전이 계획과 같은지 확인한다.
흐름: plan -> apply(``apply_proposals``, ``verify_text_only``) -> build -> verify(``verify_resolution``).
planner도 ``render_diff``와 ``locate_override``를 호출한다.
- 버전 교체(replace)는 줄 단위 문자열 치환이다. ``edit.line`` 줄에 ``edit.old``가 정확히 한 번 있어야 하고,
  그 밖의 바이트(인코딩, BOM, CRLF, 마지막 줄바꿈)는 그대로 둔다. XML을 다시 쓰지 않는다
- 유일한 예외는 ``K_MANAGED_OVERRIDE`` 제안의 삽입(insert)이다. 루트 ``pom.xml``이나 모듈 pom의 정해진 줄 앞에
  ``render_override``가 만든 ``<dependency>`` 항목 하나, 또는 그 항목을 담은 절 전체만 넣는다. 넣기 전에
  기준 줄, 형태, 같은 관리 키(g:a, 형식, classifier)의 기존 항목을 확인한다
- 한 pom의 변경은 모두 원래 줄 번호 기준으로 한 번에 적용한다
- 모든 제안의 변경을 먼저 검사하고, 잘못된 변경이 하나라도 있는 제안은 통째로 건너뛴다
- 경로는 프로젝트 폴더 안이어야 하고 ``old`` / ``new``는 버전 문자열 형태여야 한다

Apply + verify for the remediation feature: approved version-text replacements on a candidate copy plus the one approved
exception, the managed-version override entry inserted into the root pom or a module pom (fixed shape, validated), the
"only the planned text changed" check and the "resolution matches the plan" check. All edits of one pom are applied in
one pass against the original line numbers; inserts before the same line go in proposal-id order (two whole sections at
one place become one section holding the entries in that order). An override entry carries `<type>` (not jar) and
`<classifier>` when the resolved dependency has them; several entries of one g:a (one per variant) are separate
inserts at the same anchor, each its own proposal, applicable in any subset. A proposal with one invalid edit is
skipped whole.
Bytes outside the planned spans (encoding, BOM, CRLF, trailing newline) are preserved.
"""
from __future__ import annotations

import codecs
import difflib
import logging
import os
import re
from pathlib import Path, PurePosixPath

from .matching import same_ga, same_version
from .models import (
    E_INSERT,
    E_REPLACE,
    K_BOM_VERSION,
    K_MANAGED_OVERRIDE,
    K_PARENT_VERSION,
    K_PROPERTY_VERSION,
    ApplyResult,
    PackagedLib,
    Proposal,
    ResolvedModel,
    TextEdit,
)

log = logging.getLogger("sbmig.remediations.apply")

# 버전 문자열로 인정하는 형태. XML 특수문자, 공백, 따옴표가 없고, ``${``는 속성 참조이므로 받지 않는다
_VERSION_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-+]*")
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
# 속성, 부모, BOM 제안은 같은 값을 쓰는 다른 아티팩트도 함께 바꾸므로 그 변화는 문제가 아니라 참고 정보다
# override 항목은 g:a 하나만 고정하므로 여기에 넣지 않는다.
# 다른 직접 의존성이 함께 바뀌면 계획에 없는 변화로 본다
_SHARED_KINDS = (K_PROPERTY_VERSION, K_PARENT_VERSION, K_BOM_VERSION)
# override 항목의 groupId / artifactId로 인정하는 형태. XML 특수문자, 공백, 따옴표, ``$``가 없어야 한다
_COORD_TEXT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_WS = re.compile(r"[ \t]*")
ROOT_POM = "pom.xml"                     # 프로젝트 루트(reactor 부모) pom. override 항목은 모듈 pom에도 넣을 수 있다
_OVERRIDE_KEYS = ("groupId", "artifactId", "version")
# 해석된 의존성이 jar가 아니거나 classifier가 있을 때만 넣는 키다. Maven의 관리 키(g:a:type:classifier)를 맞추기 위해서다
_OVERRIDE_OPTIONAL = ("type", "classifier")
_DM = "dependencyManagement"


class EditError(ValueError):
    """변경 하나가 경로, 줄, 문자열, 인코딩 규칙을 어겼을 때 던지는 예외.
    메시지는 ``ApplyResult.skipped``에 건너뛴 이유로 남는다.

    An edit broke a rule; the message becomes the skip reason."""


def is_version_text(text: str) -> bool:
    """문자열이 버전 형태인지 확인한다.
    비어 있지 않고 ``<``, ``>``, ``&``, 공백, 따옴표, ``$``가 없어야 한다.

    True when `text` looks like plain version text (no markup, whitespace, quotes or property reference)."""
    return bool(text) and _VERSION_TEXT.fullmatch(text) is not None


# ---------------------------------------------------------------------------------------------------- 파일 읽기
class PomReadError(ValueError):
    """pom을 읽지 못했을 때 이유를 담아 던지는 예외. 메시지는 사람이 읽는 영어 이유이고 i18n이 한국어로 바꾼다.

    A pom could not be read; the message is the reason in words (open failure, encoding, XML syntax)."""


def _decode(data: bytes) -> tuple[bytes, str, str]:
    """pom 바이트를 (BOM, 인코딩, 문자열)로 나눈다. BOM이 없으면 이관 쪽과 같은 ``pom_encoding`` 규칙을 쓴다."""
    from sbmig.analyze.pom_parser import pom_encoding
    bom, encoding = b"", ""
    for b, enc in _BOMS:
        if data.startswith(b):
            bom, encoding = b, enc
            break
    body = data[len(bom):]
    # Maven은 선언과 다른 바이트도 받아 주므로 선언보다 실제로 읽히는 인코딩을 따른다
    encoding = encoding or pom_encoding(body)
    try:
        return bom, encoding, body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        raise PomReadError(f"character encoding unknown (the bytes do not decode as {encoding})") from None


_DECL_ENC = re.compile(r"^(\s*<\?xml\b[^>]*?\bencoding\s*=\s*[\"'])([^\"']*)([\"'])")


def pom_xml_bytes(data: bytes) -> bytes:
    """pom 바이트를 lxml이 읽을 수 있는 UTF-8 바이트로 바꿔 반환한다. 줄 번호는 그대로 두고 선언의 encoding만 UTF-8로 바꾼다.
    인코딩을 알 수 없으면 ``PomReadError``를 던진다.

    The pom as UTF-8 bytes for lxml: decoded by the migration's rule (`pom_encoding`), the declaration's `encoding=`
    rewritten to UTF-8 so it cannot contradict the bytes; line numbers unchanged; an already consistent UTF-8 pom is
    returned as it is. Raises `PomReadError`."""
    bom, encoding, text = _decode(data)
    if not bom and encoding == "utf-8":
        return data
    return _DECL_ENC.sub(lambda m: m.group(1) + "UTF-8" + m.group(3), text, count=1).encode("utf-8")


def read_pom_xml(path: Path) -> bytes:
    """pom 파일을 읽어 ``pom_xml_bytes``로 바꿔 반환한다. 열 수 없거나 인코딩을 알 수 없으면 이유를 담은 ``PomReadError``를 던진다.

    Read a pom file into UTF-8 XML bytes; `PomReadError` says why it cannot be opened or decoded."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise PomReadError(open_reason(e)) from None
    return pom_xml_bytes(data)


def open_reason(e: OSError) -> str:
    """파일을 열지 못한 이유를 사람이 읽는 문장으로 반환한다(권한, 없음, 폴더).

    Why a file could not be opened, in words."""
    import errno
    why = {errno.EACCES: "permission denied", errno.EPERM: "permission denied", errno.ENOENT: "missing",
           errno.EISDIR: "a folder, not a file"}.get(e.errno or 0, (e.strerror or "read error").lower())
    return f"cannot be opened ({why})"


def xml_reason(e: Exception) -> str:
    """XML 문법 오류를 줄 번호가 있는 문장으로 반환한다.

    A malformed-XML error as "not well-formed XML (line N): <parser message>"."""
    line = getattr(e, "lineno", None) or (getattr(e, "position", None) or (None,))[0]
    msg = re.sub(r"[;|]", ",", str(getattr(e, "msg", "") or e)).strip()[:160]
    return f"not well-formed XML (line {line})" + (f": {msg}" if msg else "") if line else \
        "not well-formed XML" + (f": {msg}" if msg else "")


class _PomText:
    """pom 파일 하나의 바이트와 문자열을 서로 변환하는 클래스. 인코딩은 BOM, 이관 쪽 ``pom_encoding`` 순으로 정한다.
    바꾼 곳 밖의 바이트를 보존하기 위해, 다시 인코딩했을 때 원래 바이트가 그대로 나오는 파일만 고친다."""

    def __init__(self, data: bytes, name: str):
        self.raw = data
        try:
            self.bom, self.encoding, self.text = _decode(data)
        except PomReadError as e:
            raise EditError(f"{name}: {e}") from None
        body = data[len(self.bom):]
        # 원래 바이트로 되돌아오지 않는 인코딩이면 한 문자만 바꿔도 다른 줄의 바이트가 달라질 수 있으므로 거부한다
        if self.text.encode(self.encoding) != body:
            raise EditError(f"{name}: {self.encoding} does not round-trip byte for byte; not edited")

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding)


def _split_lines(text: str) -> list[str]:
    """문자열을 ``\\n``에서만 나누고 줄 끝 문자는 그대로 둔다.
    ``str.splitlines``는 \\x0c 같은 문자에서도 나눠서 줄 번호가 Maven이나 편집기와 달라지기 때문이다."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _safe_path(project: Path, pom: str) -> Path:
    """pom 경로를 프로젝트 안의 일반 파일 경로로 바꿔 반환한다.
    절대 경로, ``..``, 역슬래시, 심볼릭 링크, 프로젝트 밖으로 나가는 경로는 거부한다."""
    if not pom or "\\" in pom or "\x00" in pom:
        raise EditError(f"invalid pom path {pom!r}")
    pp = PurePosixPath(pom)
    if pp.is_absolute() or re.match(r"^[A-Za-z]:", pom) or any(part in ("..", "") for part in pom.split("/")):
        raise EditError(f"pom path {pom!r} is not a plain relative path inside the project")
    base = Path(project)
    cur = base
    for part in pp.parts:
        if part == ".":
            continue
        cur = cur / part
        if cur.is_symlink():
            raise EditError(f"pom path {pom!r} goes through a symbolic link")
    try:
        cur.resolve().relative_to(base.resolve())
    except ValueError:
        raise EditError(f"pom path {pom!r} escapes the project") from None
    if not cur.is_file():
        raise EditError(f"pom {pom!r} does not exist")
    return cur


def is_coordinate_text(text: str) -> bool:
    """문자열을 override 항목의 groupId / artifactId로 쓸 수 있는지 확인한다.
    영숫자, ``_``, ``.``, ``-``만 허용한다.

    True when `text` is a plain Maven coordinate part (no markup, whitespace, quotes or property reference)."""
    return bool(text) and _COORD_TEXT.fullmatch(text) is not None


def _check_edit_shape(e: TextEdit) -> None:
    """변경 하나의 형태를 확인한다. 경로가 프로젝트 안의 pom인지는 파일을 읽을 때 ``_safe_path``가 확인한다."""
    if not isinstance(e.line, int) or e.line < 1:
        raise EditError(f"{e.pom}:{e.line}: invalid line {e.line!r}")
    op = e.op or E_REPLACE
    if op == E_INSERT:
        _override_parts(e)
        if e.old:
            raise EditError(f"{e.pom}:{e.line}: an insert edit has no old text")
        if not e.anchor or e.anchor != e.anchor.strip():
            raise EditError(f"{e.pom}:{e.line}: an insert edit needs its anchor line text")
        _override_shape(e)
        return
    if op != E_REPLACE:
        raise EditError(f"{e.pom}:{e.line}: unknown edit op {op!r}")
    for label, value in (("old", e.old), ("new", e.new)):
        if not is_version_text(value or ""):
            raise EditError(f"{e.pom}:{e.line}: {label} text {value!r} is not plain version text")
    if e.old == e.new:
        raise EditError(f"{e.pom}:{e.line}: old and new are the same ({e.old!r})")


# ---------------------------------------------------------------------------------------------------- override 항목
def _variant_parts(type_: str | None, classifier: str | None) -> tuple[str, str]:
    """(type, classifier)를 항목에 쓸 형태로 정규화한다. type이 jar거나 비어 있으면 빈 문자열이다."""
    t = (type_ or "").strip()
    return ("" if t in ("", "jar") else t), (classifier or "").strip()


def _key_text(ga: str, t: str, c: str) -> str:
    # Maven의 관리 키 표기(g:a:type[:classifier])다. 일반 jar는 g:a만 쓴다
    if not t and not c:
        return ga
    return f"{ga}:{t or 'jar'}" + (f":{c}" if c else "")


def _override_full(e: TextEdit) -> tuple[str, str, str, str, str]:
    """삽입 변경에서 (groupId, artifactId, version, type, classifier)를 꺼내 반환한다.
    type이 jar면 키를 두지 않으므로 빈 문자열이고, classifier가 없으면 빈 문자열이다.

    groupId / artifactId / version are required; `type` (never "jar") and `classifier` are optional keys, present
    only when the resolved dependency has them."""
    ov = e.override or {}
    if (not isinstance(ov, dict) or not set(_OVERRIDE_KEYS) <= set(ov)
            or not set(ov) <= set(_OVERRIDE_KEYS) | set(_OVERRIDE_OPTIONAL)):
        raise EditError(f"{e.pom}:{e.line}: override must name exactly groupId / artifactId / version")
    g, a, v = (str(ov.get(k) or "") for k in _OVERRIDE_KEYS)
    if not is_coordinate_text(g) or not is_coordinate_text(a):
        raise EditError(f"{e.pom}:{e.line}: override coordinates {g!r}:{a!r} are not plain coordinates")
    if not is_version_text(v):
        raise EditError(f"{e.pom}:{e.line}: override version {v!r} is not plain version text")
    t, c = str(ov.get("type", "")), str(ov.get("classifier", ""))
    if ("type" in ov and (t == "jar" or not is_coordinate_text(t))) or ("classifier" in ov and not is_coordinate_text(c)):
        raise EditError(f"{e.pom}:{e.line}: override type / classifier {t!r} / {c!r} is not plain coordinate text")
    return g, a, v, t, c


def _override_parts(e: TextEdit) -> tuple[str, str, str]:
    """삽입 변경에서 (groupId, artifactId, version)을 꺼내 반환한다.
    필수 키 셋이 있고 값이 허용된 형태여야 한다. type / classifier는 ``_override_full``이 함께 확인한다."""
    g, a, v, _t, _c = _override_full(e)
    return g, a, v


def _entry_lines(g: str, a: str, v: str, indent: str, unit: str, t: str = "", c: str = "") -> list[str]:
    # Maven의 관례 순서(groupId, artifactId, version, type, classifier)다. 일반 jar의 항목은 예전과 같은 바이트다
    lines = [f"{indent}<dependency>", f"{indent}{unit}<groupId>{g}</groupId>",
             f"{indent}{unit}<artifactId>{a}</artifactId>", f"{indent}{unit}<version>{v}</version>"]
    if t:
        lines.append(f"{indent}{unit}<type>{t}</type>")
    if c:
        lines.append(f"{indent}{unit}<classifier>{c}</classifier>")
    return lines + [f"{indent}</dependency>"]


def _render(entries: list[tuple[str, ...]], indent: str, eol: str, with_section: bool, unit: str) -> str:
    if eol not in ("\n", "\r\n"):
        raise EditError(f"line ending {eol!r} is not LF or CRLF")
    if _WS.fullmatch(indent or "") is None or not unit or _WS.fullmatch(unit) is None:
        raise EditError("indentation must be spaces / tabs only")
    full = [(*x, "", "")[:5] for x in entries]
    for g, a, v, t, c in full:
        if not is_coordinate_text(g) or not is_coordinate_text(a) or not is_version_text(v):
            raise EditError(f"override {g!r}:{a!r}:{v!r} is not plain coordinates / version text")
        if (t and (t == "jar" or not is_coordinate_text(t))) or (c and not is_coordinate_text(c)):
            raise EditError(f"override type / classifier {t!r} / {c!r} is not plain coordinate text")
    inner = indent + unit + unit if with_section else indent
    lines = [ln for g, a, v, t, c in full for ln in _entry_lines(g, a, v, inner, unit, t, c)]
    if with_section:
        lines = [f"{indent}<{_DM}>", f"{indent}{unit}<dependencies>", *lines, f"{indent}{unit}</dependencies>",
                 f"{indent}</{_DM}>"]
    return "".join(ln + eol for ln in lines)


def render_override(group_id: str, artifact_id: str, version: str, indent: str, eol: str, with_section: bool,
                    unit: str = "    ", *, type_: str = "jar", classifier: str = "") -> str:
    """override로 넣을 줄들(줄 끝 포함)을 정해진 형태로 만든다. ``with_section``이 False면 ``<dependency>``
    항목 하나, True면 그 항목을 담은 ``<dependencyManagement><dependencies>`` 절 전체다.
    ``indent``는 항목이나 절의 들여쓰기, ``unit``은 한 단계 들여쓰기, ``eol``은 파일의 줄 끝("\\n" /
    "\\r\\n")이다. 그 밖의 XML은 넣지 않는다.

    The only shape an override insert may have: one `<dependency>` entry (groupId / artifactId / version, then `<type>`
    when not jar and `<classifier>` when set) or, when the pom has no `<dependencyManagement>`, the whole section
    holding that one entry. A plain jar entry is byte-identical to the shape before type / classifier existed."""
    t, c = _variant_parts(type_, classifier)
    return _render([(group_id, artifact_id, version, t, c)], indent, eol, with_section, unit)


def _override_shape(e: TextEdit) -> tuple[str, str, str, bool]:
    """삽입 변경의 ``new``에서 들여쓰기, 단위, 줄 끝, 절 전체 여부를 읽고, ``render_override``로 다시 만든
    문자열과 정확히 같은지 확인한다. 다르면 ``EditError``를 던진다."""
    g, a, v, t, c = _override_full(e)
    new = e.new or ""
    eol = "\r\n" if new.endswith("\r\n") else "\n"
    lines = new[:-len(eol)].split(eol) if new.endswith(eol) else []
    if len(lines) < 2:
        raise EditError(f"{e.pom}:{e.line}: insert text is not the override entry shape")
    indent = _WS.match(lines[0]).group(0)                                    # type: ignore[union-attr]
    with_section = lines[0].strip() == f"<{_DM}>"
    second = lines[1]
    lead = _WS.match(second).group(0)                                        # type: ignore[union-attr]
    unit = lead[len(indent):] if lead.startswith(indent) else ""
    try:
        expected = render_override(g, a, v, indent, eol, with_section, unit, type_=t or "jar", classifier=c)
    except EditError as err:
        raise EditError(f"{e.pom}:{e.line}: insert text is not the override entry shape ({err})") from None
    if new != expected:
        raise EditError(f"{e.pom}:{e.line}: insert text is not exactly the override entry shape for {g}:{a}:{v}")
    return indent, unit, eol, with_section


def _eol_at(lines: list[str], n: int) -> str:
    """``n``번째 줄의 줄 끝 문자를 반환한다. 그 줄이 없거나 줄 끝이 없는 마지막 줄이면 앞 줄의 것을 쓴다."""
    for i in (n - 1, n - 2):
        if 0 <= i < len(lines) and lines[i].endswith("\n"):
            return "\r\n" if lines[i].endswith("\r\n") else "\n"
    return "\n"


def _parse_text(text: str):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, encoding="utf-8")
    return etree.fromstring(text.encode("utf-8"), parser=parser)


def _management_entries(root, ga: str, type_: str = "jar", classifier: str = "") -> list:
    """pom의 ``<dependencyManagement>``(최상위와 프로필)에서 이 관리 키(g:a:type:classifier)일 수 있는
    ``<dependency>`` 목록을 반환한다. 같은 항목을 두 번 만들지 않도록, 값에 ``${``가 있어 같은지 알 수 없는 항목도 포함한다.

    Maven's management key is g:a:type:classifier: an entry for the plain jar is not an entry for the `jakarta`
    classifier. Values holding `${` count as possible matches (never a second entry by accident)."""
    g, _, a = ga.partition(":")
    want_t, want_c = (type_ or "jar").strip() or "jar", (classifier or "").strip()
    holders = [root] + [p for prof in _kids(root, "profiles") for p in _kids(prof, "profile")]
    out = []
    for h in holders:
        for dm in _kids(h, _DM):
            for deps in _kids(dm, "dependencies"):
                for d in _kids(deps, "dependency"):
                    dg, da = _ktext(d, "groupId"), _ktext(d, "artifactId")
                    dt, dc = _ktext(d, "type") or "jar", _ktext(d, "classifier")
                    if ((dg == g or "${" in dg) and (da == a or "${" in da) and (dt == want_t or "${" in dt)
                            and (dc == want_c or "${" in dc)):
                        out.append(d)
    return out


def _kids(el, name: str) -> list:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _ktext(el, name: str) -> str:
    k = _kids(el, name)
    return (k[0].text or "").strip() if k else ""


def _check_inserts(text: str, lines: list[str], inserts: list[TextEdit], name: str) -> None:
    """삽입 변경이 원래 pom 내용과 맞는지 확인한다.
    기준 줄이 ``anchor``와 같은지, 기준 줄 종류와 줄 끝이 맞는지, 최상위 ``<dependencyManagement>`` 유무가
    넣을 형태와 맞는지, 같은 g:a의 관리 항목이 이미 없는지 본다. 항목은 ``</dependencies>`` 앞에, 절은
    ``<dependencies>`` / ``<build>`` / ``</project>`` 앞에 넣는다."""
    if not inserts:
        return
    try:
        root = _parse_text(text)
    except Exception as err:  # noqa: BLE001 - an unparsable root pom is never edited
        raise EditError(f"{name}: XML could not be parsed for the override check ({type(err).__name__})") from None
    has_dm = bool(_kids(root, _DM))
    seen: set[tuple[str, str, str]] = set()
    for e in inserts:
        g, a, _v, t, c = _override_full(e)
        _indent, _unit, eol, with_section = _override_shape(e)
        if e.line > len(lines):
            raise EditError(f"{name}:{e.line}: line does not exist ({len(lines)} lines)")
        if lines[e.line - 1].strip() != e.anchor:
            raise EditError(f"{name}:{e.line}: anchor line changed (expected {e.anchor!r})")
        if with_section:
            if not (e.anchor.startswith(("<dependencies>", "<build>")) or e.anchor == "</project>"):
                raise EditError(f"{name}:{e.line}: a new section goes before <dependencies>, <build> or </project> only")
            if has_dm:
                raise EditError(f"{name}: already has a <{_DM}> section; a second one is never added")
        else:
            if e.anchor != "</dependencies>":
                raise EditError(f"{name}:{e.line}: an override entry goes before the closing </dependencies> only")
            if not has_dm:
                raise EditError(f"{name}: has no top-level <{_DM}> to add an entry to")
        if eol != _eol_at(lines, e.line):
            raise EditError(f"{name}:{e.line}: insert line ending differs from the file's")
        ga = f"{g}:{a}"
        shown = _key_text(ga, t, c)
        if (ga, t, c) in seen:
            raise EditError(f"{name}: two override entries for {shown}")
        seen.add((ga, t, c))
        if _management_entries(root, ga, t or "jar", c):
            raise EditError(f"{name}: already has a <{_DM}> entry for {shown}; a second entry is never added")


def _apply_to_text(text: str, edits: list[TextEdit], name: str) -> str:
    """한 파일에 변경들을 원래 줄 번호 기준으로 한 번에 적용한 새 문자열을 반환한다.
    교체는 각 줄에 ``old``가 정확히 한 번 있어야 하고, 삽입은 ``edits`` 순서(호출하는 쪽이 제안 ID 순으로
    넘긴다)대로 기준 줄 앞에 넣는다. 같은 줄 앞에 절 전체를 넣는 삽입이 여럿이면 절 하나에 그 순서로 모은다."""
    lines = _split_lines(text)
    by_line: dict[int, TextEdit] = {}
    inserts: dict[int, list[TextEdit]] = {}
    for e in edits:
        _check_edit_shape(e)
        if (e.op or E_REPLACE) == E_INSERT:
            inserts.setdefault(e.line, []).append(e)
            continue
        prev = by_line.get(e.line)
        if prev is not None and (prev.old, prev.new) != (e.old, e.new):
            raise EditError(f"{name}:{e.line}: two different edits on one line")
        by_line[e.line] = e
    _check_inserts(text, lines, [e for group in inserts.values() for e in group], name)
    for n, e in by_line.items():
        if n > len(lines):
            raise EditError(f"{name}:{n}: line does not exist ({len(lines)} lines)")
        line = lines[n - 1]
        count = line.count(e.old)
        if count != 1:
            raise EditError(f"{name}:{n}: {e.old!r} occurs {count} times on the line (expected exactly once)")
        lines[n - 1] = line.replace(e.old, e.new, 1)
    for n in sorted(inserts, reverse=True):          # 뒤에서부터 넣어야 앞쪽 줄 번호가 바뀌지 않는다
        group = inserts[n]
        shapes = [_override_shape(e) for e in group]
        if len(group) > 1 and any(s[3] for s in shapes):
            if len({s for s in shapes}) != 1:
                raise EditError(f"{name}:{n}: override sections with different shapes at one place")
            indent, unit, eol, _ = shapes[0]
            block = _render([_override_full(e) for e in group], indent, eol, True, unit)
        else:
            block = "".join(e.new for e in group)
        lines[n - 1:n - 1] = [block]
    return "".join(lines)


def _unified(pom: str, before: str, after: str) -> str:
    """한 파일의 통합 diff(a/<pom> / b/<pom>)를 만든다. 마지막 줄바꿈이 없는 줄은 diff 관례대로 표시한다."""
    out: list[str] = []
    for chunk in difflib.unified_diff(_split_lines(before), _split_lines(after), fromfile=f"a/{pom}", tofile=f"b/{pom}"):
        if chunk.endswith("\n"):
            out.append(chunk)
        else:
            out.append(chunk + "\n\\ No newline at end of file\n")
    return "".join(out)


def _group(edits: list[TextEdit]) -> dict[str, list[TextEdit]]:
    grouped: dict[str, list[TextEdit]] = {}
    for e in edits:
        grouped.setdefault(e.pom, []).append(e)
    return grouped


def _order(p: Proposal) -> tuple:
    """제안을 ID 순서로 정렬하기 위한 키를 만든다.
    ``p-<round>-<n>``은 숫자 순서, 다른 형태는 그 뒤에 문자열 순서로 놓는다."""
    m = re.fullmatch(r"p-(\d+)-(\d+)", p.id or "")
    return (0, int(m.group(1)), int(m.group(2)), "") if m else (1, 0, 0, p.id or "")


def _edits_in_order(proposals: list[Proposal]) -> list[TextEdit]:
    return [e for p in sorted(proposals, key=_order) for e in p.edits]


def _overrides(edits: list[TextEdit]) -> list[tuple[str, str, str, bool, str, str]]:
    """삽입 변경마다 (groupId, artifactId, version, 절 전체 여부, type, classifier)를 반환한다."""
    out = []
    for e in edits:
        if (e.op or E_REPLACE) == E_INSERT:
            g, a, v, t, c = _override_full(e)
            out.append((g, a, v, _override_shape(e)[3], t, c))
    return out


def render_diff(project: Path, edits: list[TextEdit]) -> str:
    """변경들을 적용했을 때의 통합 diff를 pom 경로 순으로 만든다.
    파일은 쓰지 않으며, 잘못된 변경이 있으면 ``EditError``를 던진다.

    Pure: the unified diff the edits would produce (files sorted by path); raises `EditError` on an invalid edit."""
    out: list[str] = []
    for pom, group in sorted(_group(list(edits)).items()):
        path = _safe_path(Path(project), pom)
        doc = _PomText(path.read_bytes(), pom)
        out.append(_unified(pom, doc.text, _apply_to_text(doc.text, group, pom)))
    return "".join(out)


class _El:
    __slots__ = ("col", "end_line", "line", "path")

    def __init__(self, path: tuple[str, ...], line: int, col: int):
        self.path, self.line, self.col, self.end_line = path, line, col, 0


def _positions(text: str, name: str = ROOT_POM) -> list[_El]:
    """pom의 요소마다 (경로, 시작 줄, 시작 칸, 닫는 줄)을 구한다.
    lxml은 시작 줄만 알려 주므로 닫는 태그의 줄은 expat으로 얻는다.
    업로드된 pom은 믿을 수 없는 입력이므로 DTD와 엔티티 선언은 받지 않는다."""
    from xml.parsers import expat
    parser = expat.ParserCreate(encoding="utf-8", namespace_separator="}")
    out: list[_El] = []
    stack: list[_El] = []

    def refuse(*_a: object) -> None:
        raise EditError("the root pom declares a DTD / entities; not edited" if name == ROOT_POM
                        else f"{name} declares a DTD / entities; not edited")

    def start(name: str, _attrs: dict) -> None:
        local = name.rsplit("}", 1)[-1]
        el = _El((*(stack[-1].path if stack else ()), local), parser.CurrentLineNumber, parser.CurrentColumnNumber)
        out.append(el)
        stack.append(el)

    def end(_name: str) -> None:
        stack.pop().end_line = parser.CurrentLineNumber

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(text.encode("utf-8"), True)
    except expat.ExpatError as err:
        raise EditError(f"{name}: XML could not be parsed ({err})") from None
    return out


def _lead(line: str) -> str:
    return _WS.match(line).group(0)                                          # type: ignore[union-attr]


def _indent_unit(lines: list[str], project: _El, top: list[_El]) -> str:
    """파일의 들여쓰기 한 단계를 구한다. ``<project>``의 첫 자식 중 줄 맨 앞에 오는 요소의 들여쓰기에서
    ``<project>``의 들여쓰기를 뺀 값이며, 알 수 없으면 공백 네 칸을 쓴다."""
    base = _lead(lines[project.line - 1]) if 0 < project.line <= len(lines) else ""
    for e in top:
        if not 0 < e.line <= len(lines):
            continue
        lead = _lead(lines[e.line - 1])
        if e.col == len(lead) and len(lead) > len(base) and lead.startswith(base):
            return lead[len(base):]
    return "    "


def locate_override(project: Path, group_id: str, artifact_id: str, version: str, type_: str = "jar",
                    classifier: str = "", pom: str = ROOT_POM) -> TextEdit:
    """pom의 현재 내용에 맞춰 override 항목을 넣는 삽입 변경을 만든다. 파일은 쓰지 않는다.
    최상위 ``<dependencyManagement><dependencies>``가 있으면 ``</dependencies>`` 앞에 항목 하나를, 없으면
    최상위 ``<dependencies>``, ``<build>``, ``</project>`` 중 처음 나오는 줄 앞에 절 전체를 넣는다. 기준
    태그가 한 줄에 혼자 있지 않으면 ``EditError``를 던진다.

    Build the insert edit for one override entry against the pom's current text; `EditError` when it cannot be placed
    safely (closing tag not on its own line, odd section shape, an existing entry for the same management key
    g:a:type:classifier). `type_` / `classifier` are the resolved dependency's (a plain jar gives today's entry byte
    for byte). `pom` is the target pom's project-relative path: the root pom, or the own pom of a module that does
    not inherit from the root (checked by `_safe_path`: inside the project, no `..`, no symbolic link)."""
    t, c = _variant_parts(type_, classifier)
    path = _safe_path(Path(project), pom)
    doc = _PomText(path.read_bytes(), pom)
    lines = _split_lines(doc.text)
    els = _positions(doc.text, pom)
    if not els or els[0].path != ("project",):
        raise EditError(f"{pom}: the root element is not <project>")
    top = [e for e in els if len(e.path) == 2]
    unit = _indent_unit(lines, els[0], top)
    dms = [e for e in top if e.path[1] == _DM]
    if len(dms) > 1:
        raise EditError(f"{pom}: more than one <{_DM}> section")
    if dms:
        deps = [e for e in els if e.path == ("project", _DM, "dependencies")]
        if len(deps) != 1:
            raise EditError(f"{pom}: <{_DM}> does not hold exactly one <dependencies> section")
        line = deps[0].end_line
        if not (0 < line <= len(lines)) or lines[line - 1].strip() != "</dependencies>":
            raise EditError(f"{pom}:{line}: the closing </dependencies> of <{_DM}> is not on its own line")
        closing = _lead(lines[line - 1])
        kids = [e for e in els if e.path == ("project", _DM, "dependencies", "dependency")]
        indent = closing + unit
        if kids:
            last = kids[-1]
            lead = _lead(lines[last.line - 1])
            if last.col == len(lead):
                indent = lead
        with_section = False
    else:
        line, indent = 0, ""
        for name in ("dependencies", "build"):
            hit = next((e for e in top if e.path[1] == name), None)
            if hit is not None:
                lead = _lead(lines[hit.line - 1])
                if hit.col != len(lead):
                    raise EditError(f"{pom}:{hit.line}: <{name}> does not start its own line")
                line, indent = hit.line, lead
                break
        if not line:
            line = els[0].end_line
            if not (0 < line <= len(lines)) or lines[line - 1].strip() != "</project>":
                raise EditError(f"{pom}:{line}: the closing </project> is not on its own line")
            indent = _lead(lines[line - 1]) + unit
        with_section = True
    eol = _eol_at(lines, line)
    override = {"groupId": group_id, "artifactId": artifact_id, "version": version}
    # 일반 jar는 키를 더하지 않아야 예전 제안과 같은 proposal_hash가 나온다
    if t:
        override["type"] = t
    if c:
        override["classifier"] = c
    edit = TextEdit(pom=pom, line=line, old="", op=E_INSERT, anchor=lines[line - 1].strip(), override=override,
                    new=render_override(group_id, artifact_id, version, indent, eol, with_section, unit,
                                        type_=t or "jar", classifier=c))
    # 기존 항목, 형태, 기준 줄 검사를 지금 미리 실행한다
    after = doc.encode(_apply_to_text(doc.text, [edit], pom))
    problems = _structure_problems(pom, doc.raw, after, _overrides([edit]))
    if problems:
        raise EditError(problems[0])
    return edit


def override_variant(p: Proposal) -> str:
    """override 제안이 고정하는 형식과 classifier를 "<type>/<classifier>"로 반환한다. 일반 jar나 다른 제안은 빈 문자열이다.

    The variant an override proposal manages, in the inventory's "<type>/<classifier>" spelling; "" for the plain jar
    and for every proposal that is not an insert."""
    for e in p.edits or []:
        if (e.op or E_REPLACE) == E_INSERT:
            ov = e.override or {}
            t, c = _variant_parts(ov.get("type"), ov.get("classifier"))
            return f"{t or 'jar'}/{c}" if (t or c) else ""
    return ""


# ---------------------------------------------------------------------------------------------------- 적용
def apply_proposals(project: Path, proposals: list[Proposal]) -> ApplyResult:
    """수정본 프로젝트 폴더에 제안들의 변경을 적용한다.
    모든 변경을 먼저 검사하고, 잘못된 제안은 이유와 함께 ``skipped``에 넣고 통째로 건너뛴다.
    한 pom의 변경은 원래 줄 번호 기준으로 제안 ID 순서에 따라 한 번에 적용한다.
    ``candidate_tree_hash``는 그래프가 채운다.

    Validate every edit first, skip an invalid proposal whole, then apply the rest per pom in one pass (proposal-id order)."""
    project = Path(project)
    result = ApplyResult()
    docs: dict[str, _PomText] = {}
    accepted: dict[tuple[str, int], TextEdit] = {}      # (pom, line) → replace edit already accepted from an earlier proposal
    chosen: list[Proposal] = []
    for p in proposals:
        try:
            if not p.edits:
                raise EditError("proposal has no edits")
            mine: dict[tuple[str, int], TextEdit] = {}
            for e in p.edits:
                _check_edit_shape(e)
                if (e.op or E_REPLACE) == E_INSERT:
                    if p.kind != K_MANAGED_OVERRIDE:
                        raise EditError(f"{e.pom}:{e.line}: an insert edit belongs to a {K_MANAGED_OVERRIDE} proposal only")
                    continue
                key = (e.pom, e.line)
                for seen in (accepted.get(key), mine.get(key)):
                    # 같은 줄을 서로 다르게 고치는 두 변경은 어느 쪽이 맞는지 알 수 없으므로 뒤의 제안을 건너뛴다
                    if seen is not None and (seen.old, seen.new) != (e.old, e.new):
                        raise EditError(f"{e.pom}:{e.line}: conflicts with another edit on the same line")
                mine[key] = e
            # 파일을 쓰기 전에 이 제안까지 넣어도 파일마다 적용이 되는지, 넣은 항목의 XML 구조까지 미리 확인한다
            trial = _edits_in_order([*chosen, p])
            for pom in sorted({e.pom for e in p.edits}):
                if pom not in docs:
                    docs[pom] = _PomText(_safe_path(project, pom).read_bytes(), pom)
                mine_pom = [e for e in trial if e.pom == pom]
                new_text = _apply_to_text(docs[pom].text, mine_pom, pom)
                ovs = _overrides(mine_pom)
                if ovs:
                    problems = _structure_problems(pom, docs[pom].raw, docs[pom].encode(new_text), ovs)
                    if problems:
                        raise EditError(problems[0])
        except (EditError, OSError) as err:
            result.skipped[p.id] = str(err)
            log.warning("remediation apply: proposal %s skipped: %s", p.id, err)
            continue
        accepted.update(mine)
        chosen.append(p)
    patch: list[str] = []
    final = _edits_in_order(chosen)
    for pom in sorted({e.pom for e in final}):
        doc = docs[pom]
        new_text = _apply_to_text(doc.text, [e for e in final if e.pom == pom], pom)
        path = _safe_path(project, pom)
        _write_bytes(path, doc.encode(new_text))
        patch.append(_unified(pom, doc.text, new_text))
        result.changed_files.append(pom)
    result.applied = [p.id for p in chosen]
    result.patch = "".join(patch)
    return result


def _write_bytes(path: Path, data: bytes) -> None:
    """반쯤 쓰인 pom이 남지 않도록 같은 폴더의 임시 파일에 쓴 뒤 ``os.replace``로 바꾼다.
    권한 비트는 원래 파일의 것을 유지한다."""
    tmp = path.with_name(f".{path.name}.sbmig-tmp")
    mode = path.stat().st_mode
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode & 0o7777)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------------------------
# 검증: 버전 문자열만 바뀌었는지
def _all_edits(proposals: list[Proposal]) -> dict[str, list[TextEdit]]:
    seen: dict[tuple[str, int, str, str, str], TextEdit] = {}
    for e in _edits_in_order(proposals):
        seen.setdefault((e.pom, e.line, e.op or E_REPLACE, e.old, e.new), e)
    return _group(list(seen.values()))


def _xml_root(data: bytes):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False, huge_tree=False)
    # 선언과 다른 인코딩으로 저장된 pom도 읽도록 UTF-8로 바꿔 넘긴다
    return etree.fromstring(pom_xml_bytes(data), parser=parser)


def _xml_shape(data: bytes, skip_root: object = None, skip: frozenset = frozenset()) -> list[tuple]:
    """XML의 노드 순서(태그, 속성, tail)와 각 요소의 값을 목록으로 만든다.
    계획대로 넣은 override 요소를 빼고 비교할 수 있도록 ``skip``의 요소와 그 자식은 제외한다."""
    root = skip_root if skip_root is not None else _xml_root(data)
    out: list[tuple] = []
    for el in root.iter():
        if skip and (el in skip or any(a in skip for a in el.iterancestors())):
            continue
        tag = el.tag if isinstance(el.tag, str) else f"<{type(el).__name__}>"
        parent = el.getparent()
        ptag = parent.tag if parent is not None and isinstance(parent.tag, str) else ""
        out.append((tag, tuple(sorted((str(k), str(v)) for k, v in el.attrib.items())) if isinstance(el.tag, str) else (),
                    el.text, el.tail, ptag))
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def verify_text_only(original: Path, candidate: Path, proposals: list[Proposal], paths: list[str]) -> list[str]:
    """수정본이 원본과 비교해 계획한 버전 문자열 치환과 override 항목 추가만큼만 다른지 확인하고, 위반
    내용을 담은 문장 목록을 반환한다. 목록이 비어 있으면 통과다.
    - 변경이 없는 파일은 바이트가 같아야 한다
    - 변경이 있는 pom은 원본에 계획한 교체와 삽입을 제안 ID 순서로 적용한 바이트와 같아야 하고, XML 구조는
      계획한 override 요소만큼만 늘어야 한다

    Violations (empty = fine): untouched files byte-identical, edited poms equal to the original with exactly the planned
    replacements and inserts applied, the XML structure grown by exactly the planned override elements and nothing else;
    no file of `paths` missing on either side."""
    original, candidate = Path(original), Path(candidate)
    problems: list[str] = []
    edits = _all_edits(proposals)
    path_set = set(paths)
    for pom in sorted(edits):
        if pom not in path_set:
            problems.append(f"edited file {pom} is not in the original file list")
    for rel in sorted(path_set):
        a, b = original / rel, candidate / rel
        if not a.is_file() or a.is_symlink():
            problems.append(f"{rel}: missing in the original tree")
            continue
        if not b.is_file() or b.is_symlink():
            problems.append(f"{rel}: missing in the candidate")
            continue
        try:
            before, after = a.read_bytes(), b.read_bytes()
        except OSError as e:
            problems.append(f"{rel}: unreadable ({type(e).__name__})")
            continue
        if rel not in edits:
            if before != after:
                problems.append(f"{rel}: changed but no planned edit names it")
            continue
        try:
            doc = _PomText(before, rel)
            expected = doc.encode(_apply_to_text(doc.text, edits[rel], rel))
            ovs = _overrides(edits[rel])
        except EditError as e:
            problems.append(f"{rel}: planned edits do not apply to the original ({e})")
            continue
        if expected != after:
            problems.append(f"{rel}: differs from the original by more than the planned version replacements"
                            + (" and override entries" if ovs else ""))
            continue
        problems.extend(_structure_problems(rel, before, after, ovs))
    return problems


def _inserted(rel: str, rb, ra, overrides: list[tuple]) -> tuple[set, list[str]]:
    """수정본 ``ra``에서 계획대로 넣은 override 요소를 찾아 (비교에서 뺄 요소 집합, 문제 목록)을 반환한다.
    요소는 최상위 ``<dependencyManagement>/<dependencies>`` 안에 관리 키(g:a:type:classifier)마다 정확히 하나여야
    하고, 자식은 groupId / artifactId / version과 계획한 type / classifier뿐이며 값이 계획과 같아야 한다. 절 전체를
    넣었으면 원본에 절이 없었고 새 절에는 그 항목들만 있어야 한다."""
    skip: set = set()
    problems: list[str] = []
    dms_a, dms_b = _kids(ra, _DM), _kids(rb, _DM)
    sections = {o[3] for o in overrides}
    if len(sections) != 1 or len(dms_a) != 1:
        return skip, [f"{rel}: the planned override entries are not in one top-level <{_DM}>"]
    with_section = sections.pop()
    if with_section == bool(dms_b):
        return skip, [f"{rel}: <{_DM}> presence before / after does not match the planned override"]
    deps = _kids(dms_a[0], "dependencies")
    if len(deps) != 1:
        return skip, [f"{rel}: <{_DM}> does not hold exactly one <dependencies>"]
    for o in overrides:
        g, a, v, _ws, t, c = (*o, "", "")[:6]
        shown = _key_text(f"{g}:{a}", t, c)
        hits = [d for d in _kids(deps[0], "dependency") if _ktext(d, "groupId") == g and _ktext(d, "artifactId") == a
                and (_ktext(d, "type") or "jar") == (t or "jar") and _ktext(d, "classifier") == c]
        if len(hits) != 1:
            problems.append(f"{rel}: expected exactly one inserted <dependency> {shown}, found {len(hits)}")
            continue
        d = hits[0]
        kids = [(_local(k.tag), (k.text or "")) for k in d]
        want = [("groupId", g), ("artifactId", a), ("version", v)] + ([("type", t)] if t else []) \
            + ([("classifier", c)] if c else [])
        if kids != want or d.attrib:
            problems.append(f"{rel}: inserted <dependency> {shown} is not exactly groupId / artifactId / version {v}"
                            + (f" / type {t}" if t else "") + (f" / classifier {c}" if c else ""))
            continue
        skip.add(d)
    if with_section and not problems:
        sec = dms_a[0]
        if [c for c in sec if c is not deps[0]] or [c for c in deps[0] if c not in skip] or sec.attrib or deps[0].attrib:
            problems.append(f"{rel}: the inserted <{_DM}> holds more than the planned override entries")
        skip = {sec}
    return skip, problems


def _structure_problems(rel: str, before: bytes, after: bytes, overrides: list[tuple] | None = None) -> list[str]:
    """두 pom의 XML 구조를 비교해 문제 목록을 반환한다. 태그 순서, 속성, tail은 같아야 하고, 값이 바뀐
    요소는 ``<version>``이거나 ``<properties>``의 자식이어야 한다.
    ``overrides``가 있으면 수정본에서 그 요소를 빼고 비교하며, 넣은 위치 주변은 공백 변화만 허용한다."""
    try:
        rb, ra = _xml_root(before), _xml_root(after)
    except Exception as e:  # noqa: BLE001 - any parse failure is a violation, never a pass
        return [f"{rel}: XML could not be parsed for the structure check ({type(e).__name__})"]
    skip: set = set()
    if overrides:
        skip, problems = _inserted(rel, rb, ra, list(overrides))
        if problems:
            return problems
    sa, sb = _xml_shape(before, rb), _xml_shape(after, ra, frozenset(skip))
    if len(sa) != len(sb) or any(x[0] != y[0] or x[1] != y[1] or x[4] != y[4] for x, y in zip(sa, sb)):
        return [f"{rel}: XML element structure changed"]

    def blank(s: str | None) -> bool:
        return not (s or "").strip()
    out: list[str] = []
    for x, y in zip(sa, sb):
        if x[3] != y[3] and not (overrides and blank(x[3]) and blank(y[3])):
            out.append(f"{rel}: text between elements changed after <{_local(x[0])}>")
        elif x[2] != y[2] and not (_local(x[0]) == "version" or _local(x[4]) == "properties") \
                and not (overrides and blank(x[2]) and blank(y[2])):
            out.append(f"{rel}: text of <{_local(x[0])}> changed (only <version> / property values may change)")
    return out


# ---------------------------------------------------------------------------------------------------- 검증: 해석 결과
def _dep_variant(d) -> str:
    """해석된 의존성의 형식과 classifier를 인벤토리 표기("<type>/<classifier>")로 반환한다. 일반 jar는 빈 문자열이다."""
    t, c = (getattr(d, "type", "") or "jar"), (getattr(d, "classifier", "") or "")
    return "" if t == "jar" and not c else f"{t}/{c}"


def _module_map(model: ResolvedModel, variant: str | None = None) -> dict[str, dict[str, set[str]]]:
    """모듈 폴더별로 g:a마다 Maven이 정한 버전들을 모은 맵 ``{모듈: {g:a: 버전 집합}}``을 만든다.
    ``variant``를 주면 그 형식과 classifier의 의존성만 모은다(빈 문자열은 일반 jar)."""
    out: dict[str, dict[str, set[str]]] = {}
    for m in model.modules:
        deps = out.setdefault(m.dir, {})
        for d in m.dependencies:
            if variant is not None and _dep_variant(d) != variant:
                continue
            deps.setdefault(d.ga, set()).add(d.version)
    return out


def _has(versions: set[str], version: str) -> bool:
    # Maven은 버전 한정자의 대소문자를 구분하지 않으므로 같은 비교 함수를 쓴다
    return any(same_version(v, version) for v in versions)


def _direct_gas(model: ResolvedModel) -> set[str]:
    return {d.ga for m in model.modules for d in m.dependencies if d.direct}


def _packaging_module(artifact: str) -> str | None:
    """산출물 경로(예 ``app/target/app.war``)에서 그것을 만든 모듈 폴더를 구한다.
    ``/target/`` 앞부분을 쓰고, ``target/``으로 시작하면 루트(".")를, 알 수 없으면 None을 반환한다."""
    a = (artifact or "").replace("\\", "/").lstrip("/")
    while a.startswith("./"):
        a = a[2:]
    if a.startswith("target/"):
        return "."
    head, sep, _rest = a.partition("/target/")
    if not sep or not head:
        return None
    return head


def _proposal_variant(p: Proposal) -> str | None:
    """검증에서 볼 의존성의 범위를 정한다. override 제안은 그 관리 키의 의존성만, 다른 제안은 g:a 전체(None)다."""
    if p.kind == K_MANAGED_OVERRIDE and any((e.op or E_REPLACE) == E_INSERT for e in p.edits or []):
        return override_variant(p)
    return None


def _insert_pom(p: Proposal) -> str:
    return next((e.pom for e in p.edits or [] if (e.op or E_REPLACE) == E_INSERT), ROOT_POM)


def _stale_packaged(p: Proposal, lib: PackagedLib, variant: str | None) -> str:
    """패키징된 jar가 이 제안의 from 버전 그대로 남은 것인지 보고, 그렇다면 설명 문자열을, 아니면 빈 문자열을 반환한다."""
    artifact = p.ga.split(":", 1)[1] if ":" in p.ga else p.ga
    stale_name = f"{artifact}-{p.from_version}.jar"
    name = lib.file_name or ""
    classifier = (variant or "/").split("/", 1)[1] if variant else ""
    if classifier:
        # classifier 의존성의 jar는 pom.properties가 일반 jar와 같으므로 파일 이름으로만 구별한다
        if name.casefold().startswith(f"{artifact}-{p.from_version}-{classifier}.".casefold()):
            return name
        return ""
    if variant == "" and name.casefold().startswith(f"{artifact}-{p.from_version}-".casefold()):
        return ""                          # 일반 jar의 override는 classifier가 붙은 jar를 고정하지 않는다
    if same_ga(lib.ga, p.ga) and lib.ga and lib.version and same_version(lib.version, p.from_version):
        return f"{p.ga}:{p.from_version}"
    if (not lib.ga or (same_ga(lib.ga, p.ga) and not lib.version)) and name.casefold() == stale_name.casefold():
        return stale_name
    return ""


def verify_resolution(proposals: list[Proposal], before: ResolvedModel, after: ResolvedModel,
                      packaged: list[PackagedLib]) -> list[str]:
    """적용한 제안이 Maven 의존성 결과와 패키징 결과에 계획대로 나타났는지 확인하고 문제 목록을 반환한다.
    ``info:``로 시작하는 줄은 참고용이다.
    - ``p.modules``가 있으면 그 모듈에서만 from -> to 변경을 요구한다.
      다른 모듈에 남은 ``from_version``은 ``info:``로 남기고 다음 라운드에서 처리한다
    - override 제안은 그 항목의 관리 키(g:a, 형식, classifier)와 같은 의존성만 본다
    - 제안과 상관없는 버전 변화는 원래 직접 의존성이었을 때만 문제로 본다.
      ``before`` / ``after``가 불완전하면 그것도 문제로 본다

    Problems (lines starting with `info:` are informational). With an empty `p.modules` (legacy state, parent / BOM
    proposals) the ga must leave `from_version` everywhere and reach `to_version` in every module that had
    `from_version`. With `p.modules` set, only those governed modules must move; another module that stays at
    `from_version` (the same version reached through another origin) is an `info:` line left for the next round, and a
    stale packaged jar is a problem only when its packaging module is governed or cannot be derived. An override
    proposal is checked against the dependencies of its own management key (g:a + type + classifier): the plain jar
    entry looks at the plain jar, a `jakarta` classifier entry at the `jakarta` classifier dependency. Versions compare
    case-insensitively (`matching.same_version`, Maven's rule). Uncovered version changes are problems only for direct
    dependencies of `before`; an incomplete `before` / `after` is a problem."""
    problems: list[str] = []
    if not after.complete:
        problems.append("resolution after the change is incomplete: " + "; ".join(after.notes or ["(no detail)"]))
    if not before.complete:
        problems.append("resolution before the change is incomplete: " + "; ".join(before.notes or ["(no detail)"]))
    maps: dict[str | None, tuple[dict, dict]] = {}

    def maps_for(variant: str | None) -> tuple[dict, dict]:
        if variant not in maps:
            maps[variant] = (_module_map(before, variant), _module_map(after, variant))
        return maps[variant]

    covered = {p.ga for p in proposals}
    for p in proposals:
        variant = _proposal_variant(p)
        bmap, amap = maps_for(variant)
        if not p.modules:
            if any(_has(deps.get(p.ga, set()), p.from_version) for deps in amap.values()):
                problems.append(f"{p.ga} still resolves at {p.from_version} ({p.id})")
            for mdir, deps in sorted(bmap.items()):
                if not _has(deps.get(p.ga, set()), p.from_version):
                    continue
                if mdir not in amap:
                    problems.append(f"module {mdir} is missing from the resolution after the change ({p.id})")
                elif not _has(amap[mdir].get(p.ga, set()), p.to_version):
                    got = ", ".join(sorted(amap[mdir].get(p.ga, set()))) or "nothing"
                    problems.append(f"{p.ga} in module {mdir} resolves at {got}, planned {p.to_version} ({p.id})")
            continue
        governed = set(p.modules)
        # 이 버전 문자열이 관리하는 모듈에 from 버전이 남았을 때만 still resolves로 본다.
        # 다른 경로로 버전이 정해진 모듈은 다음 라운드에서 처리한다
        if any(_has(amap.get(m, {}).get(p.ga, set()), p.from_version) for m in governed):
            problems.append(f"{p.ga} still resolves at {p.from_version} ({p.id})")
        for mdir, deps in sorted(bmap.items()):
            if not _has(deps.get(p.ga, set()), p.from_version):
                continue
            now = amap.get(mdir, {}).get(p.ga, set())
            if mdir in governed:
                if mdir not in amap:
                    problems.append(f"module {mdir} is missing from the resolution after the change ({p.id})")
                elif not _has(now, p.to_version):
                    got = ", ".join(sorted(now)) or "nothing"
                    problems.append(f"{p.ga} in module {mdir} resolves at {got}, planned {p.to_version} ({p.id})")
            elif _has(now, p.from_version):
                if variant is not None:
                    # 넣은 override 항목은 그 pom이 닿는 모듈만 다루므로 버전 문자열과 다른 문장으로 남긴다
                    problems.append(f"info: {p.ga} in module {mdir} still resolves at {p.from_version} — outside the "
                                    f"modules the inserted entry in {_insert_pom(p)} governs ({p.id}); left for the "
                                    "next round")
                else:
                    problems.append(f"info: {p.ga} in module {mdir} still resolves at {p.from_version} — not governed "
                                    f"by the edited version text ({p.id}); left for the next round")
    bmap, amap = maps_for(None)
    direct = _direct_gas(before)
    shared = [p for p in proposals if p.kind in _SHARED_KINDS]
    broad = any(p.kind in (K_PARENT_VERSION, K_BOM_VERSION) for p in proposals)
    for mdir in sorted(set(bmap) | set(amap)):
        b, a = bmap.get(mdir, {}), amap.get(mdir, {})
        for ga in sorted(set(b) | set(a)):
            if ga in covered or b.get(ga, set()) == a.get(ga, set()):
                continue
            was = ", ".join(sorted(b.get(ga, set()))) or "absent"
            now = ", ".join(sorted(a.get(ga, set()))) or "absent"
            line = f"{ga} in module {mdir} changed {was} → {now}"
            if ga not in direct:
                problems.append(f"info: {line} (transitive)")
                continue
            # 같은 속성 값을 써서 함께 바뀐 직접 의존성과 부모, BOM 버전 변경으로 바뀐 것은 예상된 변화로 본다
            if broad or any(_has(b.get(ga, set()), p.from_version) and _has(a.get(ga, set()), p.to_version)
                            for p in shared):
                problems.append(f"info: {line} (moved with a property / parent / BOM change)")
            else:
                problems.append(f"{line} without a proposal covering it")
    for p in proposals:
        variant = _proposal_variant(p)
        governed = set(p.modules)
        for lib in packaged:
            what = _stale_packaged(p, lib, variant)
            if not what:
                continue
            mod = _packaging_module(lib.artifact) if governed else None
            if mod is not None and mod not in governed:
                problems.append(f"info: {lib.artifact} still packages {lib.entry} ({what}) — its module is not "
                                f"governed by {p.id}")
            else:
                problems.append(f"{lib.artifact} still packages {lib.entry} ({what}, {p.id})")
    return problems


_LINE_ID = re.compile(r"\((?:[^()]*, )?(p-\d+-\d+)\)$")


def split_ineffective(proposals: list[Proposal], lines: list[str]) -> tuple[list[Proposal], list[str]]:
    """검증 문제 중 효과가 없던 override 삽입 항목의 것을 가려 (그 제안 목록, 나머지 문제)를 반환한다.
    효과가 없다는 것은 넣은 항목이 다루는 모듈이 여전히 from 버전으로 해석된다는 뜻이다.

    Attribute the problem lines of `verify_resolution` (`info:` lines ignored) to proposals by their `(p-…)` id. A
    proposal is ineffective when it is an inserted override entry (kind `managed_override`, an insert edit) with at
    least one "still resolves at <from>" / "in module m resolves at <…from…>, planned <to>" line, and every line
    naming it is one of those or a stale packaged jar of its `from` version. Everything else is returned as the
    remaining problems: a version-text proposal that did not move the resolution, an incomplete resolution, an
    uncovered change, a missing module, an entry that moved the version somewhere else than planned."""
    by_id = {p.id: p for p in proposals}
    per: dict[str, list[str]] = {}
    other: list[str] = []
    for ln in lines:
        if ln.startswith("info:"):
            continue
        m = _LINE_ID.search(ln)
        if m is None or m.group(1) not in by_id:
            other.append(ln)
            continue
        per.setdefault(m.group(1), []).append(ln)
    out: list[Proposal] = []
    for pid, mine in per.items():
        p = by_id[pid]
        if _proposal_variant(p) is None or not all(_left_at_from(p, ln) is not None for ln in mine) \
                or not any(_left_at_from(p, ln) for ln in mine):
            other.extend(mine)
            continue
        out.append(p)
    out.sort(key=_order)
    return out, other


def _left_at_from(p: Proposal, line: str) -> bool | None:
    """문제 한 줄이 이 제안의 from 버전이 그대로 남았다는 내용이면 True(해석 결과), False(패키징된 jar)를, 아니면 None을 반환한다."""
    if line == f"{p.ga} still resolves at {p.from_version} ({p.id})":
        return True
    m = re.fullmatch(rf"{re.escape(p.ga)} in module .+ resolves at (?P<got>.+), planned {re.escape(p.to_version)} "
                     rf"\({re.escape(p.id)}\)", line)
    if m is not None:
        return True if any(same_version(v, p.from_version) for v in m.group("got").split(", ")) else None
    if " still packages " in line and line.endswith(f", {p.id})"):
        return False
    return None
