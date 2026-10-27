"""Advisory `jdk-generified-apis` (round 9c): pre-Java-9 javax.swing.tree code on javac 21."""
from __future__ import annotations

from sbmig.advisory import evaluate_advisories, hints_for
from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage
from tests.advisory.helpers import profile


def _finding(cfg):
    p = profile(libs=(LibraryUsage(name="jdk-generified-apis", evidence=["import:javax.swing.tree"]),),
                case=CaseType.PURE_JAVA)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "jdk-generified-apis"]
    return f


def test_detected_from_imports(cfg):
    f = _finding(cfg)
    assert f.status == "incompatible"
    assert "Enumeration<TreeNode>" in f.followup and f.notes == ["see knowledge/notes/jdk21.md"]


def test_hint_for_the_javac21_inference_error(cfg):
    f = _finding(cfg)
    e = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/com/acme/TreeUtil.java", line=12,
                   message="incompatible types: inference variable T has incompatible equality constraints "
                           "javax.swing.tree.DefaultMutableTreeNode,javax.swing.tree.TreeNode",
                   raw="import javax.swing.tree.DefaultMutableTreeNode;")
    hints, ids = hints_for([e], [f])
    assert ids == ["jdk-generified-apis"] and len(hints) == 1
    assert "Note (jdk21.md § fix-loop: jdk-generified-apis)" in hints[0] and "asIterator" in hints[0] or \
        "Enumeration<TreeNode>" in hints[0]
    other = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", symbol="class Codec",
                       file="src/main/java/com/acme/App.java")
    assert hints_for([other], [f]) == ([], [])
