"""v2.1e (2026-10-27) shared contracts: KB learning records — as-is / to-be hunks on every 수정 note, the stored
scripts' run history with the reason, lineage fields, config keys (docs/INTERFACES.md "v2.1e")."""
from __future__ import annotations

from sbmig.core.config import Config, load_config
from sbmig.core.models import CaseScript, EditHunk, EditNote, ErrorEntry, FixTry, ScriptRun


def test_edit_note_carries_whole_hunks():
    h = EditHunk(lines="~12", before="import javax.annotation.PostConstruct;",
                 after="import jakarta.annotation.PostConstruct;")
    n = EditNote(path="a/B.java", where="import 줄", change="javax → jakarta", lines="~12", hunks=[h])
    back = EditNote.model_validate_json(n.model_dump_json())
    assert back.hunks[0].before.startswith("import javax") and back.hunks[0].after.startswith("import jakarta")
    assert back.hunks[0].truncated is False and EditNote(path="x").hunks == []
    t = FixTry(n=1, source="llm-edit", edits=[n])
    e = ErrorEntry(id="e1", tries=[t])
    assert ErrorEntry.model_validate_json(e.model_dump_json()).tries[0].edits[0].hunks[0].lines == "~12"


def test_case_script_lineage_and_runs():
    r1 = ScriptRun(run_id="r1", project="p1", round=2, n=1, result="no_change",
                   reason="no_change: 3 error(s) remained: cannot find symbol …", errors_before=3, errors_left=3,
                   errors_new=0, files=["a/B.java"])
    s = CaseScript(version=2, file="fix_v2.py", source="llm-augment", parent=1,
                   reason="v1: no_change: 3 error(s) remained", last_failure=r1.reason, runs=[r1])
    back = CaseScript.model_validate_json(s.model_dump_json())
    assert back.parent == 1 and back.runs[0].errors_left == 3 and back.last_failure == r1.reason
    legacy = CaseScript(version=1, file="fix_v1.py")          # a case.json written before v2.1e still loads
    assert legacy.parent is None and legacy.reason == "" and legacy.runs == [] and legacy.last_failure == ""


def test_v21e_config_keys(repo_root):
    for cfg in (load_config(repo_root / "config" / "artifacts.example.yml", repo_root=repo_root),
                Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})):
        assert cfg.errors.snippet_lines == 0           # whole hunks: people read them (the user's call)
        assert cfg.errors.script_runs == 30
    cfg = Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}}, errors={"snippet_lines": 40})
    assert cfg.errors.snippet_lines == 40
