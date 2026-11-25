"""XML 선언과 실제 바이트의 인코딩이 다른 pom을 첫 OpenRewrite 실행 전에 맞추는 모듈.
OpenRewrite는 그런 pom을 읽지 못해 변환 전체가 실패하고, EUC-KR 범위를 벗어난 한글은 다시 쓸 때 깨뜨린다.
- 두 흐름 모두 ``transform.rewrite`` 단계가 레시피 실행 전에 호출한다
- 바이트가 EUC-KR이면 선언만 ``EUC-KR``로 바꾸고, 그 밖이면 파일을 UTF-8로 다시 저장한다
- 엄격한 XML 파서가 읽고 OpenRewrite가 손상하지 않는 pom은 건드리지 않는다
- 선언을 뺀 한글 내용은 전후가 같아야 하고, 쓰기 전에 확인한다

Contract: docs/INTERFACES.md "v2.1z-ay". Measured with rewrite-maven-plugin 6.46.1 (table in
tests/restructure/test_pom_charset.py):
* the plugin refuses a pom whose bytes are not UTF-8 when the declaration says UTF-8 or nothing
  ("Failed to parse pom: Invalid UTF-8 middle byte");
* it writes a non-UTF-8 pom back byte for byte only while every byte stays in EUC-KR's range; cp949 extension
  hangul (lead bytes 0x81-0xA0) become U+FFFD even under a truthful CP949 / MS949 / x-windows-949 declaration;
* lxml refuses the names MS949 and x-windows-949. `EUC-KR` is a name Maven, OpenRewrite and lxml all read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from sbmig.analyze.pom_parser import pom_encoding, xml_codec, xml_without_declaration

FLAG = "pom-encoding-corrected"
NOTE_PREFIX = "pom encoding corrected: "
HANDOVER_PREFIX = "pom encodings corrected before the automatic conversion: "
EUC_KR = "EUC-KR"
UTF_8 = "UTF-8"

_DECL_RE = re.compile(rb"^(\s*<\?xml\b)([^>]*?)(\?>)")
_ENC_ATTR_RE = re.compile(rb"(\bencoding\s*=\s*)([\"'])([^\"']*)\2")
_VERSION_ATTR_RE = re.compile(rb"\bversion\s*=\s*([\"'])[^\"']*\1")


@dataclass
class PomCharsetFix:
    """pom 하나의 인코딩 수정 내용을 담는 클래스.

    `declared`: the declaration's name or "none". `actual`: what the bytes are (EUC-KR / MS949 / UTF-8 / …).
    `how`: "declaration" = only the declaration changed, "utf-8" = saved again as UTF-8.
    `target`: the encoding the file declares afterwards. `data`: the new bytes."""
    pom: str
    declared: str
    actual: str
    how: str
    target: str
    data: bytes = field(repr=False, default=b"")

    def describe(self) -> str:
        """인수인계 줄에 넣을 이 pom의 설명을 반환한다.

        "`<pom>` (declared X | no declaration, saved as Y: declaration set to Z | saved again as UTF-8)"."""

        return f"`{self.pom}` ({self._what()})"

    def _what(self) -> str:
        said = "no declaration" if self.declared == "none" else f"declared {self.declared}"
        done = f"declaration set to {self.target}" if self.how == "declaration" else "saved again as UTF-8"
        return f"{said}, saved as {self.actual}: {done}"

    def note(self) -> str:
        """오류 목록 항목과 단계 메모에 쓸 한 줄을 반환한다. ``pom encoding corrected:``로 시작한다."""
        return NOTE_PREFIX + self._what()


@dataclass
class PomCharsetResult:
    """인코딩 수정 단계에서 고친 pom 목록을 담는 클래스."""
    fixes: list[PomCharsetFix] = field(default_factory=list)

    def entries(self) -> list[dict[str, str]]:
        """저널 ``pom_encodings``의 항목 목록을 반환한다."""
        return [{"pom": f.pom, "declared": f.declared, "actual": f.actual, "how": f.how} for f in self.fixes]


def _strict_ok(data: bytes) -> bool:
    try:
        etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
        return True
    except (etree.XMLSyntaxError, ValueError, LookupError):
        return False


def _decodes(data: bytes, codec: str) -> bool:
    try:
        data.decode(codec)
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def _content(text: str) -> str:
    # 선언을 새로 넣으면 그 뒤 줄바꿈도 생기므로 맨 앞 줄바꿈은 비교에서 뺀다
    return xml_without_declaration(text).lstrip("\r\n")


def _with_declaration(data: bytes, name: str) -> bytes:
    """바이트의 XML 선언 encoding 값을 ``name``으로 바꾼다. 선언이나 encoding 속성이 없으면 넣는다."""
    m = _DECL_RE.match(data)
    if m is None:
        nl = b"\r\n" if b"\r\n" in data else b"\n"
        return b'<?xml version="1.0" encoding="' + name.encode("ascii") + b'"?>' + nl + data
    attrs = m.group(2)
    if _ENC_ATTR_RE.search(attrs):
        attrs = _ENC_ATTR_RE.sub(lambda a: a.group(1) + a.group(2) + name.encode("ascii") + a.group(2), attrs, count=1)
    else:
        v = _VERSION_ATTR_RE.search(attrs)
        at = v.end() if v else len(attrs.rstrip())
        attrs = attrs[:at] + b' encoding="' + name.encode("ascii") + b'"' + attrs[at:]
    return data[:m.start(2)] + attrs + data[m.end(2):]


def diagnose(data: bytes) -> PomCharsetFix | None:
    """pom 바이트가 선언과 맞지 않으면 고친 바이트를 담은 PomCharsetFix를 반환한다.
    고칠 필요가 없거나 고칠 수 없으면 None을 반환한다.
    BOM이 있는 파일, 잘못된 XML, 어떤 후보로도 디코딩되지 않는 파일은 건드리지 않는다.

    The `pom` field stays empty (the caller sets it). No fix is needed when a strict XML reader reads the pom
    under its own declaration and OpenRewrite writes it back intact (UTF-8, or Korean bytes within EUC-KR)."""
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return None
    m = _DECL_RE.match(data)
    enc = _ENC_ATTR_RE.search(m.group(2)) if m else None
    declared = enc.group(3).decode("ascii", "replace") if enc else None
    codec = xml_codec(declared) if declared else "utf-8"
    ok = _strict_ok(data)
    # 한글 계열 선언이라도 EUC-KR 범위 밖 바이트는 OpenRewrite가 다시 쓸 때 깨뜨리므로 고친다
    if ok and not (codec == "cp949" and not _decodes(data, "euc_kr")):
        return None
    actual = pom_encoding(data)
    # 한글 계열과 UTF-8만 고친다. latin-1 같은 후보는 무엇이든 디코딩하므로 원래 글자인지 알 수 없다
    if actual not in ("utf-8", "cp949") or not _decodes(data, actual):
        return None
    text = data.decode(actual)
    try:
        etree.fromstring(xml_without_declaration(text).encode("utf-8"),
                         etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    except etree.XMLSyntaxError:
        return None
    if actual == "utf-8":
        name, how, target, new = UTF_8, "declaration", UTF_8, _with_declaration(data, UTF_8)
    elif actual == "cp949" and _decodes(data, "euc_kr"):
        name, how, target, new = EUC_KR, "declaration", EUC_KR, _with_declaration(data, EUC_KR)
    else:
        name = "MS949" if actual == "cp949" else actual.upper()
        how, target = "utf-8", UTF_8
        new = _with_declaration(text.encode("utf-8"), UTF_8)
    # 선언을 뺀 내용이 그대로인지, 엄격한 파서가 새 선언으로 읽는지 확인한 뒤에만 쓴다
    if not _strict_ok(new) or _content(new.decode(xml_codec(target) or "utf-8")) != _content(text):
        return None
    return PomCharsetFix(pom="", declared=declared or "none", actual=name, how=how, target=target, data=new)


def project_poms(ws: Any) -> list[str]:
    """Maven이 읽는 프로젝트 pom 경로를 반환한다. ``src/`` 아래 pom은 테스트 데이터라서 뺀다."""
    return [p for p in ws.pom_files() if "/src/" not in f"/{p}"]


def correct_pom_encodings(ws: Any, *, write: bool = True) -> PomCharsetResult:
    """프로젝트 pom마다 ``diagnose``를 실행하고 고친 바이트를 쓴다. 읽지 못한 파일은 건너뛴다.
    ``write``가 False면 쓰지 않고 결과만 반환한다."""
    out = PomCharsetResult()
    for rel in project_poms(ws):
        try:
            data = ws.abs(rel).read_bytes()
        except OSError:
            continue
        fix = diagnose(data)
        if fix is None:
            continue
        fix.pom = rel
        if write:
            ws.abs(rel).write_bytes(fix.data)
        out.fixes.append(fix)
    return out


def handover_line(fixes: list[PomCharsetFix]) -> str:
    """고친 pom 전체를 담은 인수인계 줄 하나를 반환한다."""
    return (HANDOVER_PREFIX + ", ".join(f.describe() for f in fixes) + " — each pom's XML declaration named another "
            "encoding than the one its Korean text was saved in, which OpenRewrite cannot read; the Korean text itself "
            "is unchanged")


__all__ = ["EUC_KR", "FLAG", "HANDOVER_PREFIX", "NOTE_PREFIX", "PomCharsetFix", "PomCharsetResult",
           "correct_pom_encodings", "diagnose", "handover_line", "project_poms"]
