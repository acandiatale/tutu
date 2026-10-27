"""Catalog E1/E3: encoding-preserving Workspace I/O, cp949 zip entry names, the fix-script helper API (`p`) keeps
legacy encodings."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.core.workspace import Workspace, detect_encoding, fix_zip_name
from sbmig.script.api import FixHelper

KOREAN = "// 한글 주석입니다 (설명)\n"
JAVA = "package com.acme;\n" + KOREAN + "public class A {\n    String s = \"안녕하세요\";\n    int x = 1;\n}\n"


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    return Workspace(root, tmp_path / "run", "t")


def _put(ws: Workspace, rel: str, data: bytes) -> Path:
    p = ws.abs(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_detect_encoding_candidates_order_and_bom(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_bytes(JAVA.encode("utf-8"))
    assert detect_encoding(f) == "utf-8"
    f.write_bytes(JAVA.encode("cp949"))
    assert detect_encoding(f) == "cp949"
    f.write_bytes(b"\xef\xbb\xbf" + JAVA.encode("utf-8"))
    assert detect_encoding(f) == "utf-8-sig"
    f.write_bytes("café ü".encode("latin-1"))
    assert detect_encoding(f, ["utf-8", "latin-1"]) == "latin-1"
    f.write_bytes("ascii only\n".encode())
    assert detect_encoding(f) == "utf-8"
    assert detect_encoding(tmp_path / "missing.txt") == "utf-8"
    # euc-kr first in the candidate list wins for a file both can decode
    f.write_bytes("한글".encode("euc-kr"))
    assert detect_encoding(f, ["utf-8", "euc-kr", "cp949"]) == "euc-kr"


def test_detect_encoding_cache_invalidated_on_change(tmp_path: Path):
    import os
    f = tmp_path / "a.txt"
    f.write_bytes(JAVA.encode("cp949"))
    assert detect_encoding(f) == "cp949"
    f.write_bytes(JAVA.encode("utf-8") + b"   ")           # size differs
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert detect_encoding(f) == "utf-8"


def test_read_write_roundtrip_cp949_is_byte_identical(ws: Workspace):
    data = JAVA.replace("\n", "\r\n").encode("cp949")
    _put(ws, "src/A.java", data)
    assert ws.encoding_of("src/A.java") == "cp949"
    text = ws.read("src/A.java")
    assert "한글 주석" in text and "\r\n" in text            # decoded correctly, newlines untouched
    ws.write("src/A.java", text)
    assert ws.abs("src/A.java").read_bytes() == data


def test_write_keeps_encoding_and_bom(ws: Workspace):
    _put(ws, "B.java", JAVA.encode("cp949"))
    ws.write("B.java", ws.read("B.java").replace("int x = 1;", "int x = 2; // 변경"))
    raw = ws.abs("B.java").read_bytes()
    assert raw.decode("cp949").count("변경") == 1
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    bom = b"\xef\xbb\xbf" + JAVA.encode("utf-8")
    _put(ws, "C.java", bom)
    text = ws.read("C.java")
    assert not text.startswith("﻿")
    ws.write("C.java", text)
    assert ws.abs("C.java").read_bytes() == bom
    # explicit encoding wins (strip the BOM)
    ws.write("C.java", text, encoding="utf-8")
    assert ws.abs("C.java").read_bytes() == JAVA.encode("utf-8")
    # new files → utf-8
    ws.write("new/D.java", "// 새 파일\n")
    assert ws.abs("new/D.java").read_bytes() == "// 새 파일\n".encode("utf-8")


def test_write_unrepresentable_chars(ws: Workspace):
    _put(ws, "E.java", JAVA.encode("cp949"))
    ws.write("E.java", ws.read("E.java") + "// \U0001F600 ok\n")
    raw = ws.abs("E.java").read_bytes().decode("cp949")
    assert "\\ud83d\\ude00" in raw and "한글" in raw          # java \\uXXXX escape, rest stays cp949
    _put(ws, "notes.txt", "한글".encode("cp949"))
    ws.write("notes.txt", ws.read("notes.txt") + " \U0001F600")
    assert ws.abs("notes.txt").read_bytes().decode("utf-8") == "한글 \U0001F600"


def test_set_encoding_candidates(ws: Workspace):
    _put(ws, "F.java", "한글".encode("cp949"))
    ws.set_encoding_candidates(["utf-8", "latin-1"])
    assert ws.encoding_of("F.java") == "latin-1"
    ws.set_encoding_candidates(None)
    assert ws.encoding_of("F.java") == "cp949"


def test_utf8_files_unchanged_behaviour(ws: Workspace):
    ws.write("src/A.java", "class A {}")
    assert ws.read("src/A.java") == "class A {}"
    assert ws.abs("src/A.java").read_bytes() == b"class A {}"


# ---------------------------------------------------------------------------- zip names (E3)
def _legacy_zip(path: Path, names: dict[str, bytes]) -> None:
    """Write a zip whose entry names are raw cp949 bytes without the UTF-8 flag (like old Windows tools)."""
    # zipfile always flags non-ascii names as UTF-8 → write same-length ascii placeholders, then patch the bytes
    # (local headers + central directory) with the raw cp949 names.
    swaps: list[tuple[bytes, bytes]] = []
    with zipfile.ZipFile(path, "w") as zf:
        for n, (name, data) in enumerate(names.items()):
            raw = name.encode("cp949")
            placeholder = (f"#{n}#".encode() * len(raw))[:len(raw)]
            swaps.append((placeholder, raw))
            zf.writestr(placeholder.decode(), data)
    blob = path.read_bytes()
    for placeholder, raw in swaps:
        assert blob.count(placeholder) == 2
        blob = blob.replace(placeholder, raw)
    path.write_bytes(blob)
    with zipfile.ZipFile(path) as zf:
        assert all(not (i.flag_bits & 0x800) for i in zf.infolist())


def test_zip_cp949_names_redecoded(tmp_path: Path):
    z = tmp_path / "legacy.zip"
    _legacy_zip(z, {"proj/pom.xml": b"<project/>", "proj/src/main/resources/메시지.properties": b"a=1",
                    "proj/문서/설명.txt": b"x"})
    with zipfile.ZipFile(z) as zf:
        names = {fix_zip_name(i) for i in zf.infolist()}
    assert "proj/src/main/resources/메시지.properties" in names
    ws = Workspace.from_input(z, tmp_path / "out", run_id="zip")
    assert (ws.root / "src/main/resources/메시지.properties").read_bytes() == b"a=1"
    assert (ws.root / "문서" / "설명.txt").exists()


def test_zip_utf8_flag_names_untouched_and_slip_guard(tmp_path: Path):
    z = tmp_path / "u.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("p/pom.xml", b"<project/>")
        zf.writestr("p/한글.txt", b"x")                  # non-ascii → zipfile sets the UTF-8 flag
    ws = Workspace.from_input(z, tmp_path / "out", run_id="u")
    assert (ws.root / "한글.txt").exists()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", b"x")
    with pytest.raises(ValueError, match="zip slip"):
        Workspace.from_input(bad, tmp_path / "out", run_id="bad")


# ---------------------------------------------------------------------------- fix-script helper API `p` (E1)
def test_script_replace_on_cp949_crlf_file_preserves_bytes_outside_span(ws: Workspace):
    data = JAVA.replace("\n", "\r\n").encode("cp949")
    _put(ws, "src/main/java/com/acme/A.java", data)
    p = FixHelper(ws.root)
    assert "안녕하세요" in p.read("src/main/java/com/acme/A.java")          # read in its detected encoding
    assert p.replace("src/main/java/com/acme/A.java", "int x = 1;", "long x = 1L;") == 1
    after = ws.abs("src/main/java/com/acme/A.java").read_bytes()
    i = data.index(b"int x = 1;")
    assert after[:i] == data[:i]
    assert after[i:i + len(b"long x = 1L;")] == b"long x = 1L;"
    assert after[i + len(b"long x = 1L;"):] == data[i + len(b"int x = 1;"):]
    assert "한글 주석" in after.decode("cp949")


def test_script_move_keeps_source_encoding(ws: Workspace):
    data = JAVA.encode("cp949")
    _put(ws, "A.java", data)
    p = FixHelper(ws.root)
    assert p.move("A.java", "B.java")
    assert not ws.exists("A.java")
    assert ws.abs("B.java").read_bytes() == data
    assert p.replace("B.java", "int x = 1;", "int x = 2;") == 1               # the moved file keeps cp949 on write
    assert ws.abs("B.java").read_bytes() == data.replace(b"int x = 1;", b"int x = 2;")
