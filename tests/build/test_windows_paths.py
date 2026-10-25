"""Round 10 (B1): javac / kotlinc paths on Windows. The in-process javac prints `/C:/work/…` (plexus
`toUri().getPath()`), other tools `file:///C:/…` (percent-encoded) or a lower-case drive; all of them must come out
project-relative with "/" — an absolute `/C:/…` made `ws.abs` build `C:\\C:\\…`, the prompt lacked the source and
absolute-path patches were rejected."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import parse_maven_log
from sbmig.build.log_parser import _clean_path, _relativize, _root_prefixes, _scrub
from sbmig.build.runner import build_summary
from sbmig.core.models import ErrorKind

LOGS = Path(__file__).parent / "logs"
WIN_ROOT = "C:\\work\\proj"


@pytest.mark.parametrize("raw,clean", [
    ("/C:/work/proj/src/A.java", "C:/work/proj/src/A.java"),
    ("file:///C:/work/proj/src/A.java", "C:/work/proj/src/A.java"),
    ("file:/C:/work/proj/src/A.java", "C:/work/proj/src/A.java"),
    ("file://C:/work/proj/src/A.java", "C:/work/proj/src/A.java"),
    ("FILE:///c:/work/proj/src/A.java", "c:/work/proj/src/A.java"),
    ("file:///C:/work/%ED%95%9C%EA%B8%80/A.java", "C:/work/한글/A.java"),
    ("C:\\work\\proj\\src\\A.java", "C:/work/proj/src/A.java"),
    ("c:\\work\\proj\\src\\A.java", "c:/work/proj/src/A.java"),
    ("file:///work/proj/src/A.java", "/work/proj/src/A.java"),
    ("/work/proj/src/A.java", "/work/proj/src/A.java"),
    ("src/main/java/A.java", "src/main/java/A.java"),
])
def test_clean_path(raw, clean):
    assert _clean_path(raw) == clean


@pytest.mark.parametrize("raw", [
    "/C:/work/proj/src/main/java/A.java", "file:///C:/work/proj/src/main/java/A.java",
    "file:/C:/work/proj/src/main/java/A.java", "c:\\work\\proj\\src\\main\\java\\A.java",
    "C:\\Work\\PROJ\\src\\main\\java\\A.java", "/c:/WORK/proj/src/main/java/A.java",
])
def test_relativize_windows_forms(raw):
    assert _relativize(raw, _root_prefixes(WIN_ROOT)) == "src/main/java/A.java"


def test_relativize_keeps_foreign_paths_absolute_but_clean():
    pre = _root_prefixes(WIN_ROOT)
    assert _relativize("/D:/elsewhere/Gen.java", pre) == "D:/elsewhere/Gen.java"
    assert _relativize("/C:/work/project2/A.java", pre) == "C:/work/project2/A.java"    # not a prefix match
    assert _relativize("/C:/work/proj", pre) == "."
    # POSIX roots stay case-sensitive
    assert _relativize("/Work/proj/A.java", _root_prefixes("/work/proj")) == "/Work/proj/A.java"
    assert _relativize("file:///work/proj/A.java", _root_prefixes("/work/proj")) == "A.java"


def test_scrub_every_spelling_of_the_root():
    pre = _root_prefixes(WIN_ROOT)
    text = ("[ERROR] /C:/work/proj/src/A.java:[1,1] x\n[ERROR] C:\\work\\proj\\src\\B.java:[2,2] y\n"
            "e: file:///c:/work/proj/src/C.kt:3:4 z\n[ERROR] C:/Work/Proj/src/D.java w\n"
            "[ERROR] C:\\work\\project2\\E.java v\n")
    assert _scrub(text, pre) == ("[ERROR] src/A.java:[1,1] x\n[ERROR] src\\B.java:[2,2] y\ne: src/C.kt:3:4 z\n"
                                 "[ERROR] src/D.java w\n[ERROR] C:\\work\\project2\\E.java v\n")
    posix = _root_prefixes("/work/proj")
    assert _scrub("at /work/proj/a and file:///work/proj/b, not /work/project2/c", posix) == \
        "at a and b, not /work/project2/c"
    assert _scrub("unchanged", []) == "unchanged"


def test_windows_uri_log_fixture():
    errs = parse_maven_log((LOGS / "synthetic-windows-uri-paths.log").read_text(encoding="utf-8"), WIN_ROOT)
    assert [(e.kind, e.file, e.line, e.column) for e in errs] == [
        (ErrorKind.COMPILE, "src/main/java/com/acme/web/UserController.java", 5, 8),
        (ErrorKind.COMPILE, "src/main/java/com/acme/한글/Report.java", 12, 20),
        (ErrorKind.TEST_COMPILE, "src/test/java/com/acme/FooTest.java", 7, 3),
        (ErrorKind.COMPILE, "src/main/java/com/acme/Upper.java", 3, 1),
        (ErrorKind.COMPILE, "D:/elsewhere/gen/Gen.java", 1, 1),
    ]
    assert {e.module for e in errs} == {"win-app"}
    assert errs[0].signature_generic == "compile|package_does_not_exist|javax.servlet.http"
    assert errs[1].symbol == "class XmlRootElement"
    for e in errs[:4]:
        assert "work/proj" not in e.raw.replace("\\", "/").lower() and not e.file.startswith("/")


def test_build_summary_scrubs_windows_uri_root():
    text = (LOGS / "synthetic-windows-uri-paths.log").read_text(encoding="utf-8")
    out = build_summary(text, WIN_ROOT)
    assert "/C:/work/proj" not in out and "file:///C:/work/proj" not in out
    assert "[ERROR] src/main/java/com/acme/web/UserController.java:[5,8] package javax.servlet.http" in out
