"""Advisory `jdk-removed-apis` (round 8b): pre-Java-8 code that imports JDK-internal / removed packages."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import evaluate_advisories, followups_for, hints_for, verify_advisories
from sbmig.analyze import PathView
from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage
from tests.advisory.helpers import profile
from tests.analyze.helpers import pom, write, write_java


def _finding(cfg):
    p = profile(libs=(LibraryUsage(name="jdk-removed-apis", evidence=["import:sun.misc"]),),
                case=CaseType.PURE_JAVA)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "jdk-removed-apis"]
    return f


def test_detected_from_imports(cfg):
    f = _finding(cfg)
    assert f.library == "jdk-removed-apis" and f.status == "incompatible"
    assert f.evidence == ["import:sun.misc"]
    assert "java.util.Base64" in f.followup and "jakarta.xml.bind-api" in f.followup
    assert "jaxb-runtime" in f.followup and "Thread.stop" in f.followup and "`_`" in f.followup
    assert f.notes == ["see knowledge/notes/jdk21.md"]


def test_not_detected_without_the_library(cfg):
    p = profile(libs=(LibraryUsage(name="jaxb", evidence=["import:javax.xml.bind"]),), case=CaseType.PURE_JAVA)
    assert "jdk-removed-apis" not in [f.advisory_id for f in evaluate_advisories(p, cfg)]


def test_hint_for_sun_misc_compile_error_carries_the_fix_loop_note(cfg):
    f = _finding(cfg)
    e = BuildError(kind=ErrorKind.COMPILE, message="package sun.misc does not exist",
                   file="src/main/java/com/acme/Codec.java")
    hints, ids = hints_for([e], [f])
    assert ids == ["jdk-removed-apis"] and len(hints) == 1
    assert hints[0].startswith("jdk-removed-apis (incompatible): replace with java.util.Base64")
    assert "Note (jdk21.md § fix-loop: jdk-removed-apis)" in hints[0]
    assert "getMimeEncoder" in hints[0]
    other = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", symbol="class Codec",
                       file="src/main/java/com/acme/App.java")
    assert hints_for([other], [f]) == ([], [])


def test_verify_replaced_once_the_imports_are_gone(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.acme", "Codec", imports=("sun.misc.BASE64Encoder",))
    f = _finding(cfg)
    verify_advisories([f], PathView(tmp_path), cfg)
    assert f.outcome == "still-present"
    write_java(tmp_path, "", "com.acme", "Codec", imports=("java.util.Base64",))
    f2 = _finding(cfg)
    verify_advisories([f2], PathView(tmp_path), cfg)
    assert f2.outcome == "replaced"
    assert followups_for([f2])[0].startswith("jdk-removed-apis → java.util.Base64")
