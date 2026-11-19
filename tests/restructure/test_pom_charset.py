"""`sbmig.restructure.pom_charset`: a pom whose bytes do not match its XML declaration is made consistent before the
first OpenRewrite run (INTERFACES "v2.1z-ay").

Measured on 2026-11-19 with the real rewrite-maven-plugin 6.46.1 (`rewrite-migrate-java` 3.42.0, recipe
`UpgradeToJava21`, goal `runNoFork`), JDK 21, Maven 3.9.10; one pom with Korean in a comment, `<name>` and
`<description>`, one Java file the recipe changes (`new Integer(5)`). "ext" = the text also holds cp949 extension
hangul (`갂똠`, lead bytes 0x81 / 0x8c), which EUC-KR cannot encode:

| case                                   | lxml   | rewrite parses | pom after the rewrite         | Maven builds |
|----------------------------------------|--------|----------------|-------------------------------|--------------|
| (g) UTF-8 header, UTF-8 bytes          | ok     | yes            | only the version lines change | yes          |
| (a) UTF-8 header, MS949 bytes          | FAIL   | NO: `Failed to parse pom: Invalid UTF-8 middle byte`, 0 files | untouched | yes (name garbled) |
| (b) no declaration, MS949 bytes        | FAIL   | NO (same)      | untouched                     | yes (name garbled) |
| (c) EUC-KR header, EUC-KR bytes        | ok     | yes            | only the version lines change | yes          |
| (c2) EUC-KR header, MS949 bytes + ext  | FAIL   | yes            | DAMAGED: Korean -> U+FFFD     | yes (garbled)|
| (d) MS949 header, MS949 bytes          | FAIL (unknown name) | yes | only the version lines change | yes        |
| (d2) MS949 header + ext                | FAIL   | yes            | DAMAGED                       | yes (garbled)|
| (e) x-windows-949 header               | FAIL (unknown name) | yes | only the version lines change | yes        |
| (e2) x-windows-949 header + ext        | FAIL   | yes            | DAMAGED                       | yes (garbled)|
| (h) CP949 header + ext (consistent)    | ok     | yes            | DAMAGED                       | yes (garbled)|
| (h2) CP949 header, MS949 bytes in EUC-KR range | ok | yes       | only the version lines change | yes          |
| (f) the same as module pom of a reactor (a, b, c, d, e) | as above | as above | as above      | as above     |

The plugin does not honour a non-UTF-8 declaration when it writes: text whose bytes stay inside EUC-KR's range
(0xA1-0xFE) comes back byte for byte, anything else is re-read as UTF-8 and replaced. So the correction writes the
declaration `EUC-KR` (the one name Maven, OpenRewrite and lxml all read: lxml refuses MS949 / x-windows-949) when
the bytes are strict EUC-KR, and saves the file again as UTF-8 otherwise, also for a consistent CP949 / MS949 pom
holding extension hangul. A consistent EUC-KR, CP949-in-range (h2) or UTF-8 pom is never touched. Re-measured with the
corrected bytes (also as the module pom of a reactor): (a) / (b) / (d) / (e) -> declaration EUC-KR, (a2) / (c2) / (d2)
/ (h) -> UTF-8: every one parses, only the version lines change, the build passes, the Korean name prints right.
"""
from __future__ import annotations

import pytest
from lxml import etree

from sbmig.analyze.pom_parser import xml_without_declaration
from sbmig.restructure.pom_charset import (
    FLAG,
    PomCharsetFix,
    correct_pom_encodings,
    diagnose,
    handover_line,
)

BODY = ("<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
        "    <!-- 한글 주석: 인사말 모듈 설명 -->\n"
        "    <modelVersion>4.0.0</modelVersion>\n"
        "    <groupId>com.example</groupId>\n    <artifactId>demo</artifactId>\n    <version>1.0</version>\n"
        "    <name>인사말 모듈{ext}</name>\n"
        "    <description>인사말 모듈 설명</description>\n"
        "</project>\n")
EXT = "갂똠"


def pom(declared: str | None, codec: str, ext: bool = False, nl: str = "\n") -> bytes:
    head = "" if declared is None else f'<?xml version="1.0" encoding="{declared}"?>\n'
    return (head + BODY.format(ext=EXT if ext else "")).replace("\n", nl).encode(codec)


def strict_ok(data: bytes) -> bool:
    try:
        etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
        return True
    except etree.XMLSyntaxError:
        return False


def text_of(data: bytes, codec: str) -> str:
    return xml_without_declaration(data.decode(codec)).lstrip("\r\n")     # an added declaration brings its line end


@pytest.mark.parametrize("declared,codec,ext,how,target", [
    ("UTF-8", "cp949", False, "declaration", "EUC-KR"),        # (a)
    (None, "cp949", False, "declaration", "EUC-KR"),           # (b)
    ("MS949", "cp949", False, "declaration", "EUC-KR"),        # (d)
    ("x-windows-949", "cp949", False, "declaration", "EUC-KR"),  # (e)
    ("UTF-8", "cp949", True, "utf-8", "UTF-8"),                # (a2)
    ("EUC-KR", "cp949", True, "utf-8", "UTF-8"),               # (c2)
    ("MS949", "cp949", True, "utf-8", "UTF-8"),                # (d2)
    ("CP949", "cp949", True, "utf-8", "UTF-8"),                # (h): consistent, but the rewrite damages it
])
def test_inconsistent_poms_are_made_consistent_with_the_korean_text_unchanged(declared, codec, ext, how, target):
    before = pom(declared, codec, ext)
    fix = diagnose(before)
    assert fix is not None and fix.how == how and fix.target == target
    assert fix.declared == (declared or "none")
    assert fix.actual == ("MS949" if ext else "EUC-KR")
    after = fix.data
    assert strict_ok(after)                                    # a strict XML reader reads it under its declaration
    assert f'encoding="{target}"' in after.decode("ascii", "replace").splitlines()[0]
    # the Korean text (everything but the declaration) is identical
    assert text_of(after, "utf-8" if target == "UTF-8" else "euc-kr") == text_of(before, "cp949")
    assert diagnose(after) is None                             # idempotent


def test_a_declaration_only_fix_changes_only_the_declaration_bytes():
    before = pom("UTF-8", "cp949")
    after = diagnose(before).data
    assert after == before.replace(b'encoding="UTF-8"', b'encoding="EUC-KR"')
    nodecl = pom(None, "cp949", nl="\r\n")
    added = diagnose(nodecl).data
    assert added == b'<?xml version="1.0" encoding="EUC-KR"?>\r\n' + nodecl       # the file's own line ending


def test_a_declaration_without_encoding_gets_one():
    before = b'<?xml version="1.0"?>\n' + BODY.format(ext="").encode("cp949")
    after = diagnose(before).data
    assert after == b'<?xml version="1.0" encoding="EUC-KR"?>\n' + BODY.format(ext="").encode("cp949")


@pytest.mark.parametrize("data", [
    pom("UTF-8", "utf-8"), pom(None, "utf-8"), pom("EUC-KR", "euc-kr"), pom("CP949", "cp949"),
    pom("UTF-8", "utf-8", ext=True), b"\xef\xbb\xbf" + pom("UTF-8", "utf-8"),
    b'<?xml version="1.0" encoding="ISO-8859-1"?>\n<project><name>caf\xe9</name></project>\n',
])
def test_a_consistent_pom_is_never_touched(data):
    assert diagnose(data) is None


def test_malformed_or_undecodable_poms_are_left_alone():
    assert diagnose(pom("UTF-8", "cp949").replace(b"</project>", b"</projec>")) is None
    assert diagnose(b'<?xml version="1.0" encoding="UTF-8"?>\n<project>\xff\xff\xff</project>') is None


def make_ws(tmp_path, files: dict[str, bytes]):
    from sbmig.core.workspace import Workspace
    src = tmp_path / "src" / "demo"
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    w = Workspace.from_input(src, tmp_path / "out", run_id="t")
    w.git_init()
    return w


def test_correct_pom_encodings_writes_every_project_pom_and_skips_src(tmp_path):
    ws = make_ws(tmp_path, {"pom.xml": pom("UTF-8", "utf-8"), "mod/pom.xml": pom("UTF-8", "cp949"),
                            "web/pom.xml": pom(None, "cp949", ext=True),
                            "mod/src/test/resources/pom.xml": pom("UTF-8", "cp949")})
    out = correct_pom_encodings(ws)
    assert [(f.pom, f.declared, f.actual, f.how) for f in out.fixes] == [
        ("mod/pom.xml", "UTF-8", "EUC-KR", "declaration"), ("web/pom.xml", "none", "MS949", "utf-8")]
    assert ws.abs("mod/pom.xml").read_bytes() == pom("EUC-KR", "cp949")
    assert ws.abs("web/pom.xml").read_bytes().decode("utf-8").startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "인사말 모듈갂똠" in ws.abs("web/pom.xml").read_text(encoding="utf-8")
    assert ws.abs("mod/src/test/resources/pom.xml").read_bytes() == pom("UTF-8", "cp949")   # test data, not a pom Maven reads
    assert out.entries() == [{"pom": "mod/pom.xml", "declared": "UTF-8", "actual": "EUC-KR", "how": "declaration"},
                             {"pom": "web/pom.xml", "declared": "none", "actual": "MS949", "how": "utf-8"}]
    assert correct_pom_encodings(ws).fixes == []                # idempotent


def test_dry_run_and_switch_off_write_nothing(tmp_path):
    ws = make_ws(tmp_path, {"pom.xml": pom("UTF-8", "cp949")})
    out = correct_pom_encodings(ws, write=False)
    assert [f.pom for f in out.fixes] == ["pom.xml"]
    assert ws.abs("pom.xml").read_bytes() == pom("UTF-8", "cp949")


def test_handover_line_and_flag():
    fixes = [PomCharsetFix("pom.xml", "UTF-8", "EUC-KR", "declaration", "EUC-KR", b""),
             PomCharsetFix("web/pom.xml", "none", "MS949", "utf-8", "UTF-8", b"")]
    assert FLAG == "pom-encoding-corrected"
    assert handover_line(fixes) == (
        "pom encodings corrected before the automatic conversion: `pom.xml` (declared UTF-8, saved as EUC-KR: "
        "declaration set to EUC-KR), `web/pom.xml` (no declaration, saved as MS949: saved again as UTF-8) — each "
        "pom's XML declaration named another encoding than the one its Korean text was saved in, which OpenRewrite "
        "cannot read; the Korean text itself is unchanged")
