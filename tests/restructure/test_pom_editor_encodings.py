"""v2.1h (A1, the restructure side): PomEditor edits poms whose XML declaration names a Korean encoding — MS949,
x-windows-949 (a Java name Python / lxml do not know), EUC-KR over cp949-only Hangul (똠), UTF-8 over cp949 bytes —
instead of failing with PomEditError (→ restructure ok=False). Decoding follows `pom_encoding`, the declaration line
is written back unchanged, and the bytes stay in the declared family (`xml_codec`: cp949 for EUC-KR / MS949)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.models import GAV
from sbmig.restructure import PomEditor, Restructurer

from .helpers import make_ws
from .test_company_layout import _legacy_web_files, _legacy_web_profile, _roles

PARENT = GAV(group_id="com.company.platform", artifact_id="base-parent", version="4.0.0")


def _pom(decl: str, korean: str = "한글 프로젝트") -> str:
    return (f'<?xml version="1.0" encoding="{decl}"?>\r\n'
            "<!-- 사내 공통 pom: 담당 홍길동 -->\r\n"
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\r\n'
            "    <modelVersion>4.0.0</modelVersion>\r\n"
            "    <groupId>com.acme</groupId>\r\n    <artifactId>shop</artifactId>\r\n    <version>1.0</version>\r\n"
            f"    <name>{korean}</name>\r\n"
            f"    <description>{korean} 설명</description>\r\n"
            "</project>\r\n")


@pytest.mark.parametrize("decl", ["MS949", "x-windows-949", "EUC-KR", "euc-kr"])
def test_korean_declared_pom_round_trips_an_edit(tmp_path: Path, decl: str) -> None:
    korean = "똠방각하 한글 프로젝트"                 # 똠: MS949 only, not in EUC-KR (KS X 1001)
    text = _pom(decl, korean)
    p = tmp_path / "pom.xml"
    p.write_bytes(text.encode("cp949"))
    ed = PomEditor(p)
    assert ed.encoding == decl and ed.codec == "cp949" and not ed.changed
    assert ed.to_string() == text
    ed.set_parent(PARENT)
    assert ed.changed
    ed.save()
    out = p.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        out.decode("utf-8")                                          # still cp949 bytes, not converted
    got = out.decode("cp949")
    assert got.startswith(f'<?xml version="1.0" encoding="{decl}"?>\r\n<!-- 사내 공통 pom: 담당 홍길동 -->\r\n')
    assert f"<name>{korean}</name>" in got and f"<description>{korean} 설명</description>" in got
    assert "<artifactId>base-parent</artifactId>" in got and "\r\n" in got and "\n" not in got.replace("\r\n", "")
    # lxml alone cannot open these files (MS949 / x-windows-949 unknown, 똠 is not EUC-KR): re-read with PomEditor
    again = PomEditor(p)
    assert again.get_parent() == PARENT and again.to_string() == got
    again.set_parent(PARENT)                                          # idempotent
    assert not again.changed


def test_utf8_declared_cp949_bytes_edit_saves_in_the_declared_family(tmp_path: Path) -> None:
    text = _pom("UTF-8")
    p = tmp_path / "pom.xml"
    p.write_bytes(text.encode("cp949"))                              # declaration says UTF-8, editor saved MS949
    ed = PomEditor(p)
    assert ed.codec == "utf-8" and "한글 프로젝트" in ed.to_string()
    ed.set_property("k", "v")
    ed.save()
    got = p.read_bytes().decode("utf-8")                             # now consistent with its declaration
    assert got.startswith('<?xml version="1.0" encoding="UTF-8"?>\r\n') and "<name>한글 프로젝트</name>" in got


def test_from_string_with_java_encoding_name() -> None:
    ed = PomEditor.from_string(_pom("x-windows-949"))
    ed.set_property("a", "b")
    assert "<a>b</a>" in ed.to_string() and ed.to_string().startswith('<?xml version="1.0" encoding="x-windows-949"?>')


def test_utf8_bom_kept(tmp_path: Path) -> None:
    p = tmp_path / "pom.xml"
    p.write_bytes(b"\xef\xbb\xbf" + _pom("UTF-8").encode("utf-8"))
    ed = PomEditor(p)
    ed.set_property("a", "b")
    ed.save()
    assert p.read_bytes().startswith(b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>')
    assert "한글 프로젝트" in p.read_bytes().decode("utf-8-sig")


def test_layout_restructure_of_an_ms949_pom(tmp_path: Path, cfg: Config) -> None:
    L = cfg.layout
    L.module_dir, L.boot_module, L.comp_module, L.front_module = "", "{name}-boot", "{name}-back", "{name}-front"
    L.comp_artifact_suffix = "-back"
    _roles(cfg)
    files = _legacy_web_files()
    pom = files["pom.xml"].replace('encoding="UTF-8"', 'encoding="MS949"', 1).replace(
        "<packaging>war</packaging>", "<name>똠방 쇼핑몰</name>\n    <packaging>war</packaging>", 1)
    assert 'encoding="MS949"' in pom
    files["pom.xml"] = "<project/>\n"                                 # replaced by the cp949 bytes below
    ws = make_ws(tmp_path, files, "ms949")
    (ws.root / "pom.xml").write_bytes(pom.encode("cp949"))
    ws.commit("ms949 pom")
    res = Restructurer().apply(ws, _legacy_web_profile(ws), cfg, mode="layout")
    assert res.ok, res.error
    root = (ws.root / "pom.xml").read_bytes().decode("cp949")
    assert root.startswith('<?xml version="1.0" encoding="MS949"?>') and "<name>똠방 쇼핑몰</name>" in root
    assert "<artifactId>base-parent</artifactId>" in root
