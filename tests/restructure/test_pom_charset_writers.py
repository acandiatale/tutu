"""Later writers keep a pom the encoding correction made consistent (INTERFACES "v2.1z-ay", part A.4).

A corrected pom is either declared EUC-KR over EUC-KR bytes or UTF-8 over UTF-8 bytes. Every writer that touches a pom
after the conversion must keep it that way: the restructure / reduced-flow corrections (`PomEditor.save`, used by the
source-encoding step and the scope restore), the fix loop's pom edits (`FixHelper.pom(...).save()`, `FixHelper.write` /
`replace` — fix scripts and `<edits>` answers) and `Workspace.write`. Checked: the bytes outside the edit are unchanged,
the whole file decodes strictly under its declaration, a strict XML reader reads it, the Korean text is intact.
"""
from __future__ import annotations

import pytest
from lxml import etree

from sbmig.core.models import GAV
from sbmig.restructure.pom_charset import diagnose
from sbmig.restructure.pom_editor import PomEditor

KOREAN = ("한글 주석: 인사말 모듈 설명", "인사말 모듈")
AS_IS = ('<?xml version="1.0" encoding="UTF-8"?>\n'
         '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
         "    <!-- 한글 주석: 인사말 모듈 설명 -->\n"
         "    <modelVersion>4.0.0</modelVersion>\n"
         "    <groupId>com.example</groupId>\n    <artifactId>demo</artifactId>\n    <version>1.0</version>\n"
         "    <name>인사말 모듈{ext}</name>\n"
         "    <properties>\n        <maven.compiler.source>1.8</maven.compiler.source>\n    </properties>\n"
         "    <dependencies>\n        <dependency>\n            <groupId>org.x</groupId>\n"
         "            <artifactId>lib</artifactId>\n            <version>1.0</version>\n        </dependency>\n"
         "    </dependencies>\n"
         "</project>\n")


def corrected(ext: bool) -> tuple[bytes, str]:
    """The as-is pom (UTF-8 header over MS949 bytes) after the correction, and its codec."""
    fix = diagnose(AS_IS.format(ext="갂똠" if ext else "").encode("cp949"))
    assert fix is not None
    return fix.data, ("euc_kr" if fix.target == "EUC-KR" else "utf-8")


def assert_consistent(data: bytes, codec: str, ext: bool) -> str:
    etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))     # strict, own declaration
    text = data.decode(codec)                                                           # strict
    assert all(k in text for k in KOREAN)
    if ext:
        assert "인사말 모듈갂똠" in text
    assert diagnose(data) is None
    return text


def changed_lines(before: bytes, after: bytes) -> list[bytes]:
    b, a = before.split(b"\n"), after.split(b"\n")
    return [x for x in a if x not in b]


@pytest.mark.parametrize("ext", [False, True], ids=["euc-kr", "utf-8"])
def test_pom_editor_save_keeps_a_corrected_pom_consistent(tmp_path, ext):
    data, codec = corrected(ext)
    p = tmp_path / "pom.xml"
    p.write_bytes(data)
    ed = PomEditor(p)
    ed.set_property("project.build.sourceEncoding", "MS949")      # the source-encoding step
    ed.set_dependency_version("org.x", "lib", "2.0")              # a version edit of the restructure / fix loop
    ed.add_dependency(GAV.parse("org.y:other:3.0"))
    ed.save()
    after = p.read_bytes()
    assert_consistent(after, codec, ext)
    # every line of the original is still there byte for byte, except the edited version line
    kept = [x for x in data.split(b"\n") if x not in after.split(b"\n")]
    assert kept == [b"            <version>1.0</version>"]
    assert all(x.isascii() for x in changed_lines(data, after))


@pytest.mark.parametrize("ext", [False, True], ids=["euc-kr", "utf-8"])
def test_scope_restore_writer_keeps_a_corrected_pom_consistent(tmp_path, ext):
    data, codec = corrected(ext)
    p = tmp_path / "pom.xml"
    p.write_bytes(data.replace(b"<version>1.0</version>\n        </dependency>",
                               b"<version>1.0</version>\n            <scope>compile</scope>\n        </dependency>"))
    before = p.read_bytes()
    ed = PomEditor(p)
    for el in ed.dependency_elements(False):                     # what restore_asis_scopes does
        if ed.gav_of(el).key == "org.x:lib":
            ed.set_child_text(el, "scope", "provided")
    ed.save()
    after = p.read_bytes()
    assert_consistent(after, codec, ext)
    assert changed_lines(before, after) == [b"            <scope>provided</scope>"]


@pytest.mark.parametrize("ext", [False, True], ids=["euc-kr", "utf-8"])
def test_fix_script_pom_helper_and_text_writes_keep_a_corrected_pom_consistent(tmp_path, ext):
    from sbmig.script.api import FixHelper
    data, codec = corrected(ext)
    (tmp_path / "pom.xml").write_bytes(data)
    h = FixHelper(tmp_path)
    pom = h.pom("pom.xml")
    assert pom.set_dependency_version("org.x", "lib", "2.0")
    assert pom.save()
    after = (tmp_path / "pom.xml").read_bytes()
    assert_consistent(after, codec, ext)
    assert all(x.isascii() for x in changed_lines(data, after))
    # an `<edits>` answer / a script's text replace
    assert h.replace("pom.xml", "<version>2.0</version>", "<version>2.1</version>") == 1
    text = h.read("pom.xml")
    assert h.write("pom.xml", text.replace("1.8</maven.compiler.source>", "21</maven.compiler.source>"))
    final = (tmp_path / "pom.xml").read_bytes()
    assert_consistent(final, codec, ext)
    assert set(changed_lines(data, final)) == {b"            <version>2.1</version>",
                                               b"        <maven.compiler.source>21</maven.compiler.source>"}


@pytest.mark.parametrize("ext", [False, True], ids=["euc-kr", "utf-8"])
def test_workspace_write_keeps_a_corrected_pom_consistent(tmp_path, ext):
    from sbmig.core.workspace import Workspace
    data, codec = corrected(ext)
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "pom.xml").write_bytes(data)
    ws = Workspace.from_input(src, tmp_path / "out", run_id="t")
    text = ws.read("pom.xml")
    ws.write("pom.xml", text.replace("<version>1.0</version>\n        </dependency>",
                                     "<version>2.0</version>\n        </dependency>"))
    after = ws.abs("pom.xml").read_bytes()
    assert_consistent(after, codec, ext)
    assert changed_lines(data, after) == [b"            <version>2.0</version>"]
