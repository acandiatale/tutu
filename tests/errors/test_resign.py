"""v2.1z-ai re-sign (sbmig.errors.resign + CaseStore.resign): records signed by older rules move to the ids the current
rules give. The mechanics run with an injected signer and a patched rule table (class ``foo``), so they do not depend
on the real signature rules; the last tests use the real rules."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sbmig.build import signatures as sigs
from sbmig.core.models import BuildError, ErrorEntry, ErrorKind, FixTry
from sbmig.errors import CaseStore, ErrorList, case_id_for
from sbmig.errors.cases import KB_MARKER, RESIGN_PENDING
from sbmig.errors.resign import continuation, older_line, rebuild_error, resigned_l2

L2 = "compile|foo|thing"
OLD = case_id_for(L2)
NEW_L2 = L2 + ".X"
NEW = case_id_for(NEW_L2)
CODE1 = "def fix(p):\n    p.log('one')\n"
CODE2 = "def fix(p):\n    p.log('two')\n"
CODE3 = "def fix(p):\n    p.log('three')\n"


def x_signer(err: BuildError) -> str:
    return err.signature_generic + ".X"


def sym_signer(err: BuildError) -> str:
    """A rule that keys class foo by the symbol (no symbol: unchanged)."""
    return f"compile|foo|{err.symbol}" if err.symbol else err.signature_generic


@pytest.fixture(autouse=True)
def foo_rules(monkeypatch):
    """Rules 2 changed class `foo` (only)."""
    monkeypatch.setattr(sigs, "SIGNATURE_VERSION", 2)
    monkeypatch.setattr(sigs, "SIGNATURE_CHANGES", {2: frozenset({"foo"})})


@pytest.fixture()
def store(tmp_path: Path, cfg) -> CaseStore:
    return CaseStore(tmp_path / "kb", cfg)


def tri(n: int, v: int, result: str = "improved", source: str = "kb-script", rnd: int = 1) -> FixTry:
    return FixTry(n=n, round=rnd, source=source, script_version=v, applied="script", result=result)


def ent(eid: str, *, l2: str = L2, run: str = "r1", project: str = "p1", symbol: str = "", tries=None, sig: int = 0,
        status: str = "resolved", **kw) -> ErrorEntry:
    data = dict(id=eid, run_id=run, project=project, case_id=case_id_for(l2), l2=l2, kind=ErrorKind.COMPILE,
                file="src/A.java", line=3, message="thing broke", seen="compile", status=status,
                tries=tries or [], sig=sig, symbol=symbol)
    data.update(kw)
    return ErrorEntry(**data)


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.name.startswith(".lock")}


def stored(store: CaseStore) -> dict[str, ErrorEntry]:
    return {e.id: e for e in store.iter_errors()}


def set_case_sig(store: CaseStore, cid: str, sig: int) -> None:
    path = store.cases_dir / cid / "case.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sig"] = sig
    path.write_text(json.dumps(data), encoding="utf-8")


# ----------------------------------------------------------------------------- pure parts
def test_continuation_and_rebuild_error_read_raw_for_old_entries():
    raw = ("[ERROR] /src/A.java:[3,9] cannot find symbol\n[ERROR]   symbol:   method keySet()\n"
           "[ERROR]   location: variable headers of type org.springframework.http.HttpHeaders")
    assert continuation(raw) == {"symbol": "method keySet()",
                                 "location": "variable headers of type org.springframework.http.HttpHeaders"}
    err = rebuild_error(ent("e1", raw=raw, artifact="org.example:lib:1.0", goal="compiler:compile", l1="L1"))
    assert (err.symbol, err.raw, err.signature, err.signature_generic) == ("method keySet()", raw, "L1", L2)
    assert err.artifact.coordinates == "org.example:lib:1.0" and err.goal == "compiler:compile"
    # stored facts win over raw
    assert rebuild_error(ent("e2", raw=raw, symbol="class X", location="class Y")).symbol == "class X"
    assert rebuild_error(ent("e3", l2="")) is None


def test_resigned_l2_rules():
    calls = []

    def spy(err):
        calls.append(err.signature_generic)
        return err.signature_generic + ".X"

    # unchanged class: never recomputed, even by a signer that would change it
    assert resigned_l2(ent("a", l2="compile|bar|k"), spy) == "compile|bar|k" and calls == []
    assert resigned_l2(ent("b"), spy) == NEW_L2 and calls == [L2]
    assert resigned_l2(ent("c", sig=2), spy) == L2                          # current already
    # a recomputation with another class / kind or without a key keeps the stored L2
    assert resigned_l2(ent("d"), lambda e: "compile|other|k") == L2
    assert resigned_l2(ent("e"), lambda e: "test_compile|foo|k") == L2
    assert resigned_l2(ent("f"), lambda e: "compile|foo|") == L2
    assert resigned_l2(ent("g"), lambda e: 1 / 0) == L2                      # a failing rule: kept


def test_older_line_is_a_regex_test(monkeypatch):
    assert older_line(ent("a").model_dump_json())
    assert not older_line(ent("b", sig=2).model_dump_json())
    assert not older_line(ent("c", l2="compile|bar|k").model_dump_json())     # class never changed
    assert not older_line(ent("d", l2="").model_dump_json())
    msg = ent("e", l2="compile|bar|k", message='"l2":"compile|foo|x" "sig":0')
    assert not older_line(msg.model_dump_json())                               # quotes inside text are escaped


def test_error_list_entries_carry_their_facts(tmp_path: Path, cfg):
    from sbmig.core.models import GAV

    el = ErrorList(tmp_path / "run", "r1", project="p", cfg=cfg)
    errs = [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/A.java", line=3,
                       symbol="method keySet()", location="variable h of type org.example.H", signature="L1",
                       signature_generic="compile|cannot_find_symbol|keySet", goal="compiler:compile"),
            BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="could not resolve", signature="dep|g:a:1",
                       signature_generic="", artifact=GAV(group_id="org.example", artifact_id="lib", version="1.0"))]
    a, b = el.add_build(errs, "build")
    assert (a.sig, a.l1, a.symbol, a.location, a.goal) == (sigs.SIGNATURE_VERSION, "L1", "method keySet()",
                                                           "variable h of type org.example.H", "compiler:compile")
    assert (b.sig, b.artifact, b.l1) == (0, "org.example:lib:1.0", "dep|g:a:1")    # no L2: sig stays 0
    note = el.add_note("pom.xml", "the review fixed it", l2="review|structure")
    assert note.sig == sigs.SIGNATURE_VERSION


# ----------------------------------------------------------------------------- whole moves
def test_whole_case_moves_by_rename(store: CaseStore):
    store.add_script(OLD, L2, CODE1, run_id="r0", project="p0")
    store.set_pin(OLD, "block:a person decides")
    store.record_run([ent("e1", tries=[tri(1, 1)], script=f"cases/{OLD}/fix_v1.py"),
                      ent("e2", run="r2", project="p2", tries=[tri(1, 1, "no_change")])])
    assert (store.root / KB_MARKER).is_file()                    # a new KB is marked current at once
    rep = store.resign(signer=x_signer)
    assert rep.moves == [(OLD, NEW, NEW_L2)] and (rep.entries_resigned, rep.entries_stamped) == (2, 2)
    assert rep.splits == [] and rep.merged == 0 and not rep.unreadable
    assert not (store.cases_dir / OLD).exists()
    case = store.find(NEW)
    assert (case.l2, case.sig, case.aliases) == (NEW_L2, 2, [OLD])
    assert [s.version for s in case.scripts] == [1] and store.code(NEW, 1) == CODE1
    assert store.pin_of(NEW) == "block:a person decides" and case.seen == 2
    es = stored(store)
    assert set(es) == {"e1", "e2"}                               # entry ids never change
    assert all((e.case_id, e.l2, e.was, e.sig) == (NEW, NEW_L2, [OLD], 2) for e in es.values())
    assert es["e1"].script == f"cases/{NEW}/fix_v1.py"
    files = sorted(p.name for p in (store.cases_dir / NEW / "errors").glob("*.json"))
    assert files == ["r1-e1.json", "r2-e2.json"]
    assert json.loads((store.cases_dir / NEW / "errors" / "r1-e1.json").read_text())["case_id"] == NEW
    assert json.loads((store.root / KB_MARKER).read_text())["sig"] == 2
    assert not (store.root / RESIGN_PENDING).exists()
    assert store.validate() == []
    assert any(r.run_id == "r2" and r.result == "no_change" for r in case.scripts[0].runs)


def test_move_merges_into_an_existing_case(store: CaseStore):
    store.add_script(NEW, NEW_L2, CODE2, run_id="rn", project="pn")              # target: v1 = CODE2
    store.add_script(OLD, L2, CODE1, run_id="r0", project="p0")                  # old: v1 = CODE1, v2 = CODE2
    store.add_script(OLD, L2, CODE2, run_id="r0", project="p0", source="llm-augment", parent=1)
    store.credit(OLD, 2, "improved", project="p9", run_id="r9", round=4, n=1)
    store.record_run([ent("e1", tries=[tri(1, 1, "no_change"), tri(2, 2, "improved")],
                          script=f"cases/{OLD}/fix_v2.py")])
    rep = store.resign(signer=x_signer)
    assert rep.moves == [(OLD, NEW, NEW_L2)] and rep.merged == 1
    case = store.find(NEW)
    by = {s.version: s for s in case.scripts}
    assert sorted(by) == [1, 2] and store.code(NEW, 1) == CODE2 and store.code(NEW, 2) == CODE1
    assert len(list((store.cases_dir / NEW).glob("fix_v*.py"))) == 2              # CODE2 stored once
    assert by[2].parent is None and by[1].successes >= 1 and "p9" in by[1].projects
    assert any(r.run_id == "r9" for r in by[1].runs)                              # runs merged
    e = stored(store)["e1"]
    assert [t.script_version for t in e.tries] == [2, 1]                          # old v1 → 2, old v2 → 1
    assert e.script == f"cases/{NEW}/fix_v1.py" and case.aliases == [OLD]
    assert not (store.cases_dir / OLD).exists() and store.validate() == []


# ----------------------------------------------------------------------------- splits
def _split_kb(store: CaseStore, *, keep: bool) -> dict[str, str]:
    store.add_script(OLD, L2, CODE1, run_id="r0", project="p0")
    store.add_script(OLD, L2, CODE2, run_id="r0", project="p0")
    store.add_script(OLD, L2, CODE3, run_id="r0", project="p0")
    store.set_pin(OLD, "block")
    batch = [ent("eA", symbol="A", tries=[tri(1, 1, "no_change"), tri(2, 2, "improved")]),
             ent("eA2", run="r3", project="p3", symbol="A"),
             ent("eB", run="r2", project="p2", symbol="B", tries=[tri(1, 2, "no_change")])]
    if keep:
        batch.append(ent("eC", run="r4", project="p4"))
    store.record_run(batch)
    return {"A": case_id_for("compile|foo|A"), "B": case_id_for("compile|foo|B")}


def test_split_scripts_follow_the_tries_that_ran_them(store: CaseStore):
    ids = _split_kb(store, keep=True)
    rep = store.resign(signer=sym_signer)
    assert rep.moves == [] and rep.splits == [(OLD, sorted([OLD, ids["A"], ids["B"]]))]
    a, b, o = store.find(ids["A"]), store.find(ids["B"]), store.find(OLD)
    assert [s.version for s in a.scripts] == [1, 2] and [s.version for s in b.scripts] == [2]
    assert [s.version for s in o.scripts] == [3]                              # nobody ran v3: it stays
    assert store.code(a.case_id, 2) == CODE2 == store.code(b.case_id, 2)
    assert not (store.cases_dir / OLD / "fix_v1.py").exists() and (store.cases_dir / OLD / "fix_v3.py").is_file()
    va2, vb2 = a.scripts[1], b.scripts[0]                                     # v2 is in both: counted per part
    assert (va2.successes, va2.failures, va2.projects) == (1, 0, ["p1"])
    assert (vb2.successes, vb2.failures, vb2.projects) == (0, 1, [])
    assert store.pin_of(ids["A"]) == store.pin_of(ids["B"]) == store.pin_of(OLD) == "block"
    assert a.aliases == [OLD] and o.aliases == []
    es = stored(store)
    assert (es["eA"].case_id, es["eA"].was, es["eB"].case_id, es["eC"].case_id) == (ids["A"], [OLD], ids["B"], OLD)
    assert (es["eC"].was, es["eC"].sig) == ([], 2)
    assert (store.cases_dir / ids["B"] / "errors" / "r2-eB.json").is_file()
    assert not (store.cases_dir / OLD / "errors" / "r2-eB.json").exists()
    assert (store.cases_dir / OLD / "errors" / "r4-eC.json").is_file()
    assert store.validate() == []


def test_split_without_a_kept_part_sends_unrun_versions_to_the_largest(store: CaseStore):
    ids = _split_kb(store, keep=False)
    store.resign(signer=sym_signer)
    assert not (store.cases_dir / OLD).exists()
    assert [s.version for s in store.find(ids["A"]).scripts] == [1, 2, 3]   # A holds 2 of the 3 entries
    assert [s.version for s in store.find(ids["B"]).scripts] == [2]
    assert store.validate() == []


# ----------------------------------------------------------------------------- what stays
def test_unchanged_classes_are_left_byte_for_byte(store: CaseStore):
    calls = []

    def spy(err):
        calls.append(err.signature_generic)
        return err.signature_generic + ".X"

    other = "compile|bar|k"
    store.add_script(case_id_for(other), other, CODE1)
    store.record_run([ent("e1", l2=other)])
    (store.root / KB_MARKER).unlink()                                    # an old KB: no marker
    before = store.errors_path.read_bytes()
    case_before = (store.cases_dir / case_id_for(other) / "case.json").read_bytes()
    rep = store.resign(signer=spy)
    assert calls == [] and rep.moves == [] and rep.entries_stamped == 0 and not rep.changed
    assert store.errors_path.read_bytes() == before                      # sig stays 0: the class is current
    assert (store.cases_dir / case_id_for(other) / "case.json").read_bytes() == case_before
    assert json.loads((store.root / KB_MARKER).read_text())["sig"] == 2


def test_recomputation_to_another_class_keeps_the_l2(store: CaseStore):
    store.record_run([ent("e1")])
    rep = store.resign(signer=lambda err: "compile|other|thing")
    assert rep.moves == [] and stored(store)["e1"].case_id == OLD and stored(store)["e1"].sig == 2


def test_a_case_without_entries_is_left_as_it_is(store: CaseStore):
    lonely = "compile|foo|lonely"
    store.add_script(case_id_for(lonely), lonely, CODE1)
    set_case_sig(store, case_id_for(lonely), 0)
    before = (store.cases_dir / case_id_for(lonely) / "case.json").read_bytes()
    rep = store.resign(signer=x_signer)
    assert rep.moves == [] and rep.cases_stamped == 0
    assert (store.cases_dir / case_id_for(lonely) / "case.json").read_bytes() == before


def test_an_entry_whose_l2_stays_is_stamped_once(store: CaseStore):
    store.record_run([ent("e1")])
    rep = store.resign(signer=lambda err: err.signature_generic)
    assert rep.entries_stamped == 1 and rep.entries_resigned == 0 and stored(store)["e1"].sig == 2
    assert not store.resign(signer=x_signer).changed                    # never looked at again


def test_aliases_accumulate_over_two_rule_versions(store: CaseStore, monkeypatch):
    store.add_script(OLD, L2, CODE1)
    store.record_run([ent("e1", tries=[tri(1, 1)])])
    store.resign(signer=x_signer)
    monkeypatch.setattr(sigs, "SIGNATURE_VERSION", 3)
    monkeypatch.setattr(sigs, "SIGNATURE_CHANGES", {2: frozenset({"foo"}), 3: frozenset({"foo"})})
    rep = store.resign(signer=lambda err: err.signature_generic + ".Y")
    final = case_id_for(NEW_L2 + ".Y")
    assert rep.moves == [(NEW, final, NEW_L2 + ".Y")]
    case = store.find(final)
    assert (case.aliases, case.sig) == ([OLD, NEW], 3)
    e = stored(store)["e1"]
    assert (e.case_id, e.was, e.sig) == (final, [OLD, NEW], 3)
    assert store.case_ids() == [final]


# ----------------------------------------------------------------------------- idempotency, dry run, fast path
def test_second_run_and_dry_run_change_no_byte(store: CaseStore):
    _split_kb(store, keep=True)
    before = tree(store.root)
    rep = store.resign(dry_run=True, signer=sym_signer)
    assert rep.dry_run and len(rep.splits) == 1 and rep.entries_resigned == 3
    assert tree(store.root) == before
    store.resign(signer=sym_signer)
    after = tree(store.root)
    again = store.resign(signer=sym_signer)
    assert not again.changed and tree(store.root) == after


def test_fast_path_reads_once_and_writes_nothing(store: CaseStore, monkeypatch):
    store.record_run([ent("e1", sig=2)])
    before = tree(store.root)
    monkeypatch.setattr(CaseStore, "_affected_lines", lambda *a: (_ for _ in ()).throw(AssertionError("parsed")))
    rep = store.resign(signer=x_signer)
    assert not rep.changed and tree(store.root) == before


def test_reactor_modules_are_emptied_and_restored(store: CaseStore):
    seen = []

    def spy(err):
        seen.append(sigs.reactor_modules())
        return err.signature_generic + ".X"

    store.record_run([ent("e1")])
    sigs.set_reactor_modules(["my-module"])
    try:
        store.resign(signer=spy)
        assert seen == [frozenset()] and sigs.reactor_modules() == frozenset({"my-module"})
    finally:
        sigs.set_reactor_modules(())


# ----------------------------------------------------------------------------- interruptions
def test_interrupted_before_errors_jsonl_completes_on_the_next_run(store: CaseStore, monkeypatch):
    store.add_script(NEW, NEW_L2, CODE2)
    store.add_script(OLD, L2, CODE1)
    store.add_script(OLD, L2, CODE2)
    store.record_run([ent("e1", tries=[tri(1, 1, "no_change"), tri(2, 2)], script=f"cases/{OLD}/fix_v1.py")])
    real = CaseStore._resign_write_entries
    monkeypatch.setattr(CaseStore, "_resign_write_entries",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        store.resign(signer=x_signer)
    assert stored(store)["e1"].case_id == OLD                          # nothing lost: the old case is intact
    assert len(list((store.cases_dir / NEW).glob("fix_v*.py"))) == 2   # the copy happened
    monkeypatch.setattr(CaseStore, "_resign_write_entries", real)
    store.resign(signer=x_signer)
    assert len(list((store.cases_dir / NEW).glob("fix_v*.py"))) == 2   # recognised by content: no duplicate
    e = stored(store)["e1"]
    assert (e.case_id, [t.script_version for t in e.tries], e.script) == (NEW, [2, 1], f"cases/{NEW}/fix_v2.py")
    assert not (store.cases_dir / OLD).exists() and not (store.root / RESIGN_PENDING).exists()
    assert [s.version for s in store.find(NEW).scripts] == [1, 2] and store.validate() == []


def test_interrupted_after_errors_jsonl_finishes_the_clean_up(store: CaseStore, monkeypatch):
    _split_kb(store, keep=True)
    real = CaseStore._finish_pending
    monkeypatch.setattr(CaseStore, "_resign_move_entry_files",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        store.resign(signer=sym_signer)
    assert (store.root / RESIGN_PENDING).is_file() and [s.version for s in store.find(OLD).scripts] == [1, 2, 3]
    monkeypatch.undo()
    monkeypatch.setattr(sigs, "SIGNATURE_CHANGES", {2: frozenset({"foo"})})
    assert CaseStore._finish_pending is real
    store.resign(signer=sym_signer)
    assert not (store.root / RESIGN_PENDING).exists()
    assert [s.version for s in store.find(OLD).scripts] == [3]
    assert (store.cases_dir / case_id_for("compile|foo|B") / "errors" / "r2-eB.json").is_file()
    assert store.validate() == []


def test_interrupted_rename_is_repaired(store: CaseStore, monkeypatch):
    store.add_script(OLD, L2, CODE1)
    store.record_run([ent("e1", tries=[tri(1, 1)])])
    real = CaseStore._save
    monkeypatch.setattr(CaseStore, "_save", lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        store.resign(signer=x_signer)
    assert not (store.cases_dir / OLD).exists() and (store.cases_dir / NEW / "fix_v1.py").is_file()
    monkeypatch.setattr(CaseStore, "_save", real)
    store.resign(signer=x_signer)
    case = store.find(NEW)
    assert (case.case_id, case.l2, case.aliases) == (NEW, NEW_L2, [OLD]) and store.code(NEW, 1) == CODE1
    assert stored(store)["e1"].case_id == NEW and store.validate() == []


def test_an_unreadable_case_is_skipped_and_keeps_its_records(store: CaseStore):
    store.add_script(OLD, L2, CODE1)
    store.record_run([ent("e1")])
    (store.cases_dir / OLD / "case.json").write_text("{broken", encoding="utf-8")
    rep = store.resign(signer=x_signer)
    assert rep.unreadable == [OLD] and rep.moves == []
    e = stored(store)["e1"]
    assert (e.case_id, e.sig) == (OLD, 0)                               # tried again next time


def test_merge_from_resigns_an_old_export(tmp_path: Path, cfg):
    old_kb = CaseStore(tmp_path / "old", cfg)
    old_kb.add_script(OLD, L2, CODE1, project="theirs")
    old_kb.record_run([ent("e1", project="theirs", tries=[tri(1, 1)])])
    mine = CaseStore(tmp_path / "mine", cfg)
    mine.signer = x_signer
    counters = mine.merge_from(old_kb)
    assert counters["entries_resigned"] == 1
    assert mine.case_ids() == [NEW] and mine.find(NEW).aliases == [OLD]
    assert stored(mine)["e1"].case_id == NEW
    again = mine.merge_from(old_kb)                                     # entry ids never change: no duplicate
    assert again["errors_added"] == 0 and mine.count_errors() == 1
    assert len(list((mine.cases_dir / NEW).glob("fix_v*.py"))) == 1
    # the old export's case folder comes back under its old id with no entry: nothing is merged by an alias
    assert sorted(mine.case_ids()) == sorted([NEW, OLD]) and stored(mine)["e1"].case_id == NEW


# ----------------------------------------------------------------------------- the real rules
RAW_KEYSET = ("[ERROR] /src/A.java:[3,9] cannot find symbol\n[ERROR]   symbol:   method keySet()\n"
              "[ERROR]   location: variable headers of type org.springframework.http.HttpHeaders")


def _real(eid: str, l2: str, raw: str, message: str = "cannot find symbol") -> ErrorEntry:
    return ErrorEntry(id=eid, run_id="r1", project="p1", case_id=case_id_for(l2), l2=l2, kind=ErrorKind.COMPILE,
                      file="src/A.java", line=3, message=message, raw=raw, seen="compile", status="unresolved")


@pytest.fixture()
def real_rules(monkeypatch):
    monkeypatch.undo()                     # the module's real SIGNATURE_VERSION / SIGNATURE_CHANGES


def test_real_rules_key_a_member_by_its_owner(real_rules, store: CaseStore):
    e = _real("e1", "compile|cannot_find_symbol|keySet", RAW_KEYSET)
    assert resigned_l2(e) == "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet"
    store.record_run([e])
    rep = store.resign()
    new = case_id_for("compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet")
    assert rep.moves == [(e.case_id, new, "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet")]


def test_real_rules_keep_a_class_symbol(real_rules):
    raw = "[ERROR] /src/A.java:[3,9] cannot find symbol\n[ERROR]   symbol:   class BASE64Encoder\n" \
          "[ERROR]   location: class com.example.Codec"
    e = _real("e1", "compile|cannot_find_symbol|BASE64Encoder", raw)
    assert resigned_l2(e) == e.l2


def test_real_rules_give_no_suitable_method_its_owner(real_rules):
    raw = ("[ERROR] /src/A.java:[7,20] no suitable method found for findAll(org.springframework.data.domain.Sort)\n"
           "[ERROR]     method org.springframework.data.repository.PagingAndSortingRepository.findAll("
           "org.springframework.data.domain.Sort) is not applicable")
    e = _real("e1", "compile|no_suitable_method|findAll", raw,
              message="no suitable method found for findAll(org.springframework.data.domain.Sort)")
    assert resigned_l2(e) == "compile|no_suitable_method|org.springframework.data.repository." \
                             "PagingAndSortingRepository.findAll"
