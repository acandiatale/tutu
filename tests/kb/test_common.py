"""sbmig.kb.common: the record helpers that survived the v1 harvest / ledger modules (PLAN-v2 step 4)."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.models import CaseType, FileChange, ProjectProfile
from sbmig.core.workspace import Workspace
from sbmig.kb import changes_as_diff, project_name
from sbmig.kb.common import RAW_LIMIT, rel_posix


def test_changes_as_diff_covers_every_op():
    diff = changes_as_diff([
        FileChange(path="A.java", op="modify", search="a\nb", replace="a\nc"),
        FileChange(path="B.java", op="modify", search="same", replace="same"),
        FileChange(path="C.java", op="create", content="x\ny"),
        FileChange(path="D.java", op="delete"),
        FileChange(path="E.java", op="rename", new_path="F.java"),
    ])
    assert "-b" in diff and "+c" in diff and "no textual change" in diff
    assert "=== CREATE C.java\n+x\n+y" in diff and "=== DELETE D.java" in diff and "E.java -> F.java" in diff
    assert changes_as_diff([]) == ""


def test_project_name_prefers_the_profile(tmp_path: Path):
    ws = Workspace(tmp_path / "proj", tmp_path / "run", "r1")
    prof = ProjectProfile(root=str(ws.root), name="dir/demo-app", case_type=CaseType.PURE_JAVA)
    assert project_name(prof, ws) == "demo-app"
    assert project_name(prof.model_copy(update={"name": ""}), ws) == "proj"
    assert project_name(None, None) == ""


def test_rel_posix_never_leaks_absolute_paths(tmp_path: Path):
    ws = Workspace(tmp_path / "proj", tmp_path / "run", "r1")
    assert rel_posix(str(ws.root / "src" / "A.java"), ws) == "src/A.java"
    assert rel_posix("./src\\B.java", None) == "src/B.java"
    assert rel_posix("/elsewhere/C.java", ws) == "C.java"
    assert rel_posix("C:/Users/x/D.java", None) == "D.java"
    assert rel_posix(None, ws) is None and rel_posix("", ws) == ""
    assert RAW_LIMIT == 2000
