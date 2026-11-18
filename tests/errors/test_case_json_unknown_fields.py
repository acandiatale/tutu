"""K4: a case.json re-saved by a version of the code that does not know a field of a script version or of a run
record keeps that field (top-level unknown keys already survived). K5: `kb import` writes each script to its
canonical name inside the case folder whatever the imported case.json says — never outside the KB."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.errors.cases import CaseStore

CID = "a1b2c3d4e5"
L2 = "compile|package_missing|javax.annotation"
CODE = "def fix(p):\n    p.log('x')\n"


def _case_json(store: CaseStore, cid: str = CID) -> dict:
    return json.loads((store.cases_dir / cid / "case.json").read_text(encoding="utf-8"))


def _write_case_json(store: CaseStore, data: dict, cid: str = CID) -> None:
    (store.cases_dir / cid / "case.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_unknown_fields_of_a_script_version_and_a_run_survive_a_resave(tmp_path: Path, cfg):
    store = CaseStore(tmp_path / "kb", cfg)
    store.add_script(CID, L2, CODE, run_id="r1", project="p1")
    store.credit(CID, 1, "no_change", project="p1", run_id="r1", reason="3 left", round=1, n=1)
    data = _case_json(store)
    data["pin"] = "block"                                                # top level: survived before too
    data["scripts"][0]["future_field"] = {"kind": "newer", "n": 2}
    data["scripts"][0]["runs"][0]["future_run_field"] = "kept"
    _write_case_json(store, data)
    store.credit(CID, 1, "improved", project="p2", run_id="r2", round=1, n=1)   # a re-save by this code
    after = _case_json(store)
    assert after["pin"] == "block"
    assert after["scripts"][0]["future_field"] == {"kind": "newer", "n": 2}
    assert after["scripts"][0]["runs"][0]["future_run_field"] == "kept"
    assert "future_run_field" not in after["scripts"][0]["runs"][-1]         # the new run is the code's own
    assert after["scripts"][0]["successes"] == 1                             # the model fields still win


def test_a_case_without_unknown_fields_is_written_as_before(tmp_path: Path, cfg):
    store = CaseStore(tmp_path / "kb", cfg)
    store.add_script(CID, L2, CODE, run_id="r1", project="p1")
    store.credit(CID, 1, "no_change", project="p1", run_id="r1", round=1, n=1)
    before = (store.cases_dir / CID / "case.json").read_bytes()
    case, extra = store._read(CID)
    store._save(case, extra)
    assert (store.cases_dir / CID / "case.json").read_bytes() == before


def test_kb_import_writes_scripts_to_their_canonical_name_only(tmp_path: Path, cfg):
    theirs = CaseStore(tmp_path / "theirs", cfg)
    theirs.add_script(CID, L2, CODE, run_id="r1", project="p1")
    data = _case_json(theirs)
    data["scripts"][0]["file"] = "../../../escaped.py"
    _write_case_json(theirs, data)
    mine = CaseStore(tmp_path / "mine" / "kb", cfg)
    mine.merge_from(theirs)
    assert not (tmp_path / "mine" / "escaped.py").exists() and not (tmp_path / "escaped.py").exists()
    assert not list((tmp_path / "mine").rglob("escaped.py"))
    assert (mine.cases_dir / CID / "fix_v1.py").read_text(encoding="utf-8") == CODE
    assert _case_json(mine)["scripts"][0]["file"] == "fix_v1.py"
    assert mine.code(CID, 1) == CODE and mine.best(CID) is not None


def test_kb_import_with_another_file_name_keeps_the_script(tmp_path: Path, cfg):
    theirs = CaseStore(tmp_path / "theirs", cfg)
    theirs.add_script(CID, L2, CODE, run_id="r1", project="p1")
    data = _case_json(theirs)
    data["scripts"][0]["file"] = "renamed.py"
    _write_case_json(theirs, data)
    mine = CaseStore(tmp_path / "mine", cfg)
    mine.merge_from(theirs)
    assert mine.code(CID, 1) == CODE and not (mine.cases_dir / CID / "renamed.py").exists()
