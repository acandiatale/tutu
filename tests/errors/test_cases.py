"""sbmig.errors.cases: the KB (docs/v2/PLAN-v2.1.md §3.4) — cases/<id>/case.json + fix_v<N>.py, best / credit / the
proven rule / retire, record_run → <kb>/errors.jsonl + cases.md, pins, recount, validate, merge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.models import ErrorEntry, ErrorKind, FixTry
from sbmig.errors import CaseStore, case_id_for

L2 = "compile|package_does_not_exist|jakarta.xml.bind"
CID = case_id_for(L2)
CODE1 = "def fix(p):\n    p.log('v1')\n"
CODE2 = "def fix(p):\n    p.log('v2')\r\n"


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def entry(eid, *, run="r1", project="p1", status="resolved", l2=L2, tries=None, **kw) -> ErrorEntry:
    return ErrorEntry(id=eid, run_id=run, project=project, case_id=case_id_for(l2) if l2 else kw.pop("case_id", ""),
                      l2=l2, kind=ErrorKind.COMPILE, file="src/A.java", line=3,
                      message="package jakarta.xml.bind does not exist", seen="compile", status=status,
                      resolved="r1 try 1" if status == "resolved" else "",
                      tries=tries if tries is not None else
                      [FixTry(n=1, round=1, source="llm-new", analysis="JAXB left the JDK",
                              solution="declare jakarta.xml.bind-api", applied="edits", result="improved")], **kw)


def test_add_script_versions_and_identical_code(store: CaseStore):
    s1 = store.add_script(CID, L2, CODE1, source="llm-new", run_id="r1", project="p1", analysis="why",
                          solution="how", name="package jakarta.xml.bind does not exist", kind=ErrorKind.COMPILE)
    assert (s1.version, s1.file, s1.status, s1.created_by, s1.project) == (1, "fix_v1.py", "unverified", "r1", "p1")
    assert store.add_script(CID, L2, CODE1, run_id="r2", project="p2").version == 1        # the same code
    s2 = store.add_script(CID, L2, CODE2, source="llm-augment", run_id="r2", project="p2")
    assert s2.version == 2 and store.code(CID, 2) == CODE2                                  # exact bytes (CRLF kept)
    assert (store.root / "cases" / CID / "fix_v2.py").read_bytes() == CODE2.encode()
    case = store.find(CID)
    assert case.l2 == L2 and case.name == "package jakarta.xml.bind does not exist" and case.kind == ErrorKind.COMPILE
    assert case.last_analysis == "why" and case.last_solution == "how" and [s.version for s in case.scripts] == [1, 2]
    data = json.loads((store.root / "cases" / CID / "case.json").read_text(encoding="utf-8"))
    assert data["case_id"] == CID and data["scripts"][1]["source"] == "llm-augment"
    with pytest.raises(ValueError):
        store.add_script(CID, L2, "  ")
    with pytest.raises(ValueError):
        store.add_script("../evil", L2, CODE1)


def test_best_prefers_proven_then_newest_never_retired(store: CaseStore):
    assert store.best(CID) is None and store.latest(CID) is None
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    store.add_script(CID, L2, CODE2, run_id="r1", project="p1")
    assert store.best(CID)[0].version == 2                         # unverified newest
    store.credit(CID, 1, "improved", project="p2", run_id="r2")    # v1 resolved the case in another project
    assert store.scripts(CID)[0].status == "proven" and store.best(CID) == (store.scripts(CID)[0], CODE1)
    store.retire(CID, 1)
    assert store.best(CID)[0].version == 2
    store.retire(CID, 2)
    assert store.best(CID) is None and store.latest(CID) is None
    assert store.activate(CID, 1).status == "proven" and store.activate(CID, 2).status == "unverified"
    (store.root / "cases" / CID / "fix_v1.py").unlink()            # a missing file is skipped
    assert store.best(CID)[0].version == 2


def test_credit_and_the_proven_rule(store: CaseStore):
    store.add_script(CID, L2, CODE1, run_id="r1", project="p1")
    s = store.credit(CID, 1, "build_ok", project="p1", run_id="r1b")   # the same project: still unverified
    assert (s.successes, s.status, s.projects) == (1, "unverified", ["p1"])
    for res in ("no_change", "regressed", "apply_failed", "blocked", "reverted"):
        store.credit(CID, 1, res, project="p3")
    s = store.credit(CID, 1, "partial", project="p3")
    assert (s.failures, s.successes, s.last_result) == (5, 1, "partial")
    assert store.credit(CID, 1, "pending") is None and store.credit(CID, 9, "improved") is None
    assert store.credit("0000000000", 1, "improved") is None
    s = store.credit(CID, 1, "improved", project="p2", run_id="r2")
    assert s.status == "proven" and s.projects == ["p1", "p2"]
    store.retire(CID, 1)
    assert store.credit(CID, 1, "improved", project="p4").status == "retired"   # credit never revives a retired one


def test_a_script_with_a_secret_is_stored_masked_and_retired(store: CaseStore):
    s = store.add_script(CID, L2, "def fix(p):\n    p.write('a.properties', 'db.password=hunter2\\n')\n")
    body = store.code(CID, s.version)
    assert s.status == "retired" and "hunter2" not in body and body.startswith("# sbmig: retired")
    assert store.best(CID) is None


def test_record_run_appends_once_and_updates_the_cases(store: CaseStore):
    other = "compile|cannot_find_symbol|Foo"
    es = [entry("e1"), entry("e2", status="unresolved", tries=[]),
          entry("e3", l2=other, status="blocked", note="blocked: x"),
          entry("n1", l2="", status="note", case_id=""),
          ErrorEntry(id="pv", run_id="r1", status="pom-verify", seen="restructure", file="pom.xml")]
    assert store.record_run(es) == 5
    assert store.record_run(es) == 0                                # the same ids are not stored twice
    case = store.find(CID)
    assert (case.seen, case.resolved, case.projects) == (2, 1, ["p1"])
    assert case.last_solution == "declare jakarta.xml.bind-api" and case.name.startswith("package jakarta")
    assert store.extra(CID)["last_result"].startswith("resolved 1, unresolved 1 (r1 try 1) — p1 r1")
    foo = store.find(case_id_for(other))
    assert foo.name == "package jakarta.xml.bind does not exist: Foo" and foo.resolved == 0
    assert sorted(store.case_ids()) == sorted([CID, case_id_for(other)])          # no folder for notes / pom-verify
    store.record_run([entry("e9", run="r2", project="p2")])
    case = store.find(CID)
    assert (case.seen, case.resolved, case.projects) == (3, 2, ["p1", "p2"])
    md = store.md_path.read_text(encoding="utf-8")
    assert "| case | name | projects | seen / resolved | scripts | pin | last result |" in md
    assert f"| {CID} | package jakarta.xml.bind does not exist | 2 | 3 / 2 | - | - |" in md
    assert [e.id for e in store.iter_errors(run="r1", status="resolved")] == ["e1"]
    assert [e.id for e in store.iter_errors(case=CID[:5])] == ["e1", "e2", "e9"] and store.count_errors() == 6


def test_record_run_masks_what_it_stores(store: CaseStore):
    e = entry("s1", raw="password=hunter2", tries=[FixTry(n=1, round=1, source="llm-new",
                                                         diff="+spring.datasource.password=hunter2")])
    store.record_run([e])
    assert "hunter2" not in store.errors_path.read_text(encoding="utf-8")


def test_pins_survive_updates_and_people_keys_are_kept(store: CaseStore):
    store.add_script(CID, L2, CODE1)
    path = store.root / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["owner_note"] = "사람 메모"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    store.set_pin(CID, "block:external_api_incompatible")
    store.record_run([entry("e1")])
    store.credit(CID, 1, "improved", project="p9")
    x = store.extra(CID)
    assert x["pin"] == "block:external_api_incompatible" and x["owner_note"] == "사람 메모"
    assert store.describe(CID) == "pin block: external_api_incompatible"
    assert "block:external_api_incompatible" in store.md_path.read_text(encoding="utf-8")
    store.set_pin(CID, "")
    assert "pin" not in store.extra(CID) and store.pin_of(CID) == ""
    with pytest.raises(ValueError):
        store.set_pin(CID, "script:2")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pin"] = "script:2"                                        # hand-edited: not a block, ignored
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.pin_of(CID) == "" and any("pin 'script:2'" in p for p in store.validate())


def test_describe_the_lookup_result(store: CaseStore):
    assert store.describe(CID) == "저장된 스크립트 없음 (처음 보는 케이스)"
    store.record_run([entry("e1")])
    assert store.describe(CID).startswith("저장된 스크립트 없음 (케이스는 1개")
    store.add_script(CID, L2, CODE1, project="p1")
    assert store.describe(CID) == "저장된 스크립트 v1 (unverified, 성공 0 / 실패 0, 프로젝트 0)"
    store.retire(CID, 1)
    assert store.describe(CID) == "케이스는 있으나 쓸 스크립트 없음 (v1 retired 0/0)"


def test_recount_rebuilds_from_errors_jsonl(store: CaseStore):
    store.record_run([entry("e1"), entry("e2", run="r2", project="p2", status="unresolved", tries=[])])
    store.add_script(CID, L2, CODE1)
    path = store.root / "cases" / CID / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(seen=99, resolved=99, projects=["x"])
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.recount() == 1
    case = store.find(CID)
    assert (case.seen, case.resolved, case.projects, len(case.scripts)) == (2, 1, ["p1", "p2"], 1)


def test_validate_lists_damage_and_readers_skip_it(store: CaseStore):
    store.add_script(CID, L2, CODE1)
    store.record_run([entry("e1")])
    assert store.validate() == []
    with open(store.errors_path, "a", encoding="utf-8") as fh:
        fh.write('{"not": "an entry"}\n')
    (store.root / "cases" / CID / "fix_v7.py").write_text("x")
    (store.root / "cases" / CID / "fix_v1.py").unlink()
    bad = store.root / "cases" / "deadbeef00"
    bad.mkdir()
    (bad / "case.json").write_text("{broken")
    probs = store.validate()
    assert any("line 2 is no ErrorEntry" in p for p in probs)
    assert any("fix_v7.py is not listed" in p for p in probs) and any("has no file fix_v1.py" in p for p in probs)
    assert any("deadbeef00/case.json: does not read" in p for p in probs)
    assert store.find("deadbeef00") is None and [e.id for e in store.iter_errors()] == ["e1"]
    assert store.stats()["cases"] == 1


def test_merge_from_is_idempotent(tmp_path: Path, cfg):
    a, b = CaseStore(tmp_path / "a", cfg), CaseStore(tmp_path / "b", cfg)
    a.add_script(CID, L2, CODE1, project="pa")
    a.credit(CID, 1, "improved", project="pa2")
    a.record_run([entry("ea", project="pa")])
    b.add_script(CID, L2, CODE2, project="pb")
    b.add_script(CID, L2, CODE1, project="pb")
    other = "compile|x|Only"
    b.add_script(case_id_for(other), other, CODE1)
    b.record_run([entry("eb", run="rb", project="pb")])
    c1 = a.merge_from(b)
    assert c1 == {"errors_added": 1, "cases_added": 1, "scripts_added": 1, "scripts_merged": 1}
    assert a.merge_from(b) == {"errors_added": 0, "cases_added": 0, "scripts_added": 0, "scripts_merged": 3}
    case = a.find(CID)
    assert [(s.version, s.status) for s in case.scripts] == [(1, "proven"), (2, "unverified")]
    assert a.code(CID, 2) == CODE2 and case.seen == 2 and case.projects == ["pa", "pb"]
    assert a.code(case_id_for(other), 1) == CODE1
    st = a.stats()
    assert (st["cases"], st["script_versions"], st["errors"]) == (2, 3, 2)


def test_record_run_writes_one_readable_json_per_error(tmp_path, store: CaseStore):
    """v2.1c: the business team browses the stored errors as files, not as errors.jsonl lines."""
    import json
    from sbmig.core.models import ErrorEntry, ErrorKind, FixTry
    st = store
    e = ErrorEntry(id="8871c2ca3c4d", run_id="run-1", project="demo", case_id="7a52b1cea8",
                   l2="compile|package_does_not_exist|jakarta.xml.bind", kind=ErrorKind.COMPILE, file="a/B.java",
                   line=6, message="package jakarta.xml.bind does not exist", seen="compile", status="resolved",
                   tries=[FixTry(n=1, round=1, source="llm-new", analysis="JAXB left the JDK", solution="declare it",
                                 applied="edits", files=["pom.xml"], diff="+<dependency>", result="improved")])
    pv = ErrorEntry(id="deadbeef0001", run_id="run-1", project="demo", seen="restructure", status="pom-verify",
                    file="pom.xml", note="parent → base-parent")
    st.record_run([e, pv])
    f = tmp_path / "kb" / "cases" / "7a52b1cea8" / "errors" / "run-1-8871c2ca3c4d.json"
    assert f.is_file() and st.entry_files("7a52b1cea8") == [f]
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["tries"][0]["solution"] == "declare it" and "\n  \"tries\"" in f.read_text(encoding="utf-8")
    assert (tmp_path / "kb" / "cases" / "_other" / "errors" / "run-1-deadbeef0001.json").is_file()
    st.record_run([e])                                                  # already stored: no duplicate, no error
    assert len(st.entry_files("7a52b1cea8")) == 1


def test_recount_writes_the_per_error_files_that_are_missing(tmp_path, store: CaseStore):
    """`sbmig kb reindex` gives errors stored before v2.1c (or whose file a person deleted) their JSON file."""
    from sbmig.core.models import ErrorEntry, ErrorKind
    e = ErrorEntry(id="aaaa00000001", run_id="run-9", project="demo", case_id="7a52b1cea8",
                   l2="compile|package_does_not_exist|jakarta.xml.bind", kind=ErrorKind.COMPILE, message="x",
                   seen="compile", status="resolved")
    store.record_run([e])
    f = store.entry_file(e)
    f.unlink()
    assert not f.exists()
    store.recount()
    assert f.is_file()
