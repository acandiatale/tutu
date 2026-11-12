"""v2.1z-ai re-sign, review round: regressions for the defects an independent review reproduced (alias merge of a live
case, glued / unparsable / skipped lines re-signing forever, a damaged case.json overwritten, in-house prefixes not
registered, cut facts, the folder clean-up after a kill at every write step, streaming). Real signature rules."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from pathlib import Path

import pytest

import sbmig.errors.cases as cm
from sbmig.build import signatures as sigs
from sbmig.core.models import BuildError, ErrorEntry, ErrorKind, FixTry, ResolvedBy
from sbmig.errors import CaseStore, ErrorList, case_id_for
from sbmig.errors.cases import KB_MARKER, RESIGN_PENDING
from sbmig.errors.resign import rebuild_error, resigned_l2
from sbmig.kb.store import FileKnowledgeBase

OLD_L2 = "compile|cannot_find_symbol|keySet"
OLD = case_id_for(OLD_L2)
NEW_L2 = "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet"
NEW = case_id_for(NEW_L2)
HH = "variable headers of type org.springframework.http.HttpHeaders"
MAP = "variable m of type java.util.Map<java.lang.String,java.lang.String>"
NEW_FIELDS = ("sig", "l1", "symbol", "location", "artifact", "goal", "was")


def loc_raw(sym: str, loc: str) -> str:
    return f"[ERROR] /src/A.java:[3,9] cannot find symbol\n[ERROR]   symbol:   {sym}\n[ERROR]   location: {loc}"


RAW = loc_raw("method keySet()", HH)


def tri(n: int, v: int, result: str = "improved", source: str = "kb-script", rnd: int = 1) -> FixTry:
    return FixTry(n=n, round=rnd, source=source, script_version=v, applied="script", result=result)


def ent(eid: str, l2: str = OLD_L2, raw: str = RAW, run: str = "r1", project: str = "p1", tries=None, sig: int = 0,
        **kw) -> ErrorEntry:
    d = dict(id=eid, run_id=run, project=project, case_id=case_id_for(l2), l2=l2, kind=ErrorKind.COMPILE,
             file="src/A.java", line=3, message="cannot find symbol", seen="compile", status="resolved", raw=raw,
             tries=tries or [], sig=sig)
    d.update(kw)
    return ErrorEntry(**d)


def old_line(e: ErrorEntry) -> str:
    """The line an image before rule versions wrote (none of the new fields)."""
    d = json.loads(e.model_dump_json())
    for k in NEW_FIELDS:
        d.pop(k)
    return json.dumps(d, separators=(",", ":"))


def write_old(store: CaseStore, entries) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    with open(store.errors_path, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(old_line(e) + "\n")


def scripts(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("fix_v*.py"))}


@pytest.fixture()
def clean_prefixes():
    """The process-global in-house prefix set, emptied for the test and restored afterwards."""
    saved = set(sigs._EXTRA_EXTERNAL_PREFIXES)
    sigs._EXTRA_EXTERNAL_PREFIXES.clear()
    yield
    sigs._EXTRA_EXTERNAL_PREFIXES.clear()
    sigs._EXTRA_EXTERNAL_PREFIXES.update(saved)


# ----------------------------------------------------------------------------- D2: no alias-only merge
def test_a_live_case_under_an_old_id_is_never_merged_by_alias(tmp_path: Path, cfg):
    root = tmp_path / "kb"
    st = CaseStore(root, cfg)
    write_old(st, [ent("e1", tries=[tri(1, 1)])])
    st.add_script(OLD, OLD_L2, "def fix(p):\n    p.log('HttpHeaders')\n")
    assert st.resign().moves == [(OLD, NEW, NEW_L2)]
    # a new-rules run: keySet() on a PROJECT type keeps the bare key — the old id is a live case again
    live = "def fix(p):\n    p.log('project type keySet')\n"
    CaseStore(root, cfg).add_script(OLD, OLD_L2, live)
    write_old(st, [ent("e9", l2="compile|cannot_find_symbol|Foo", raw="cannot find symbol\n  symbol: class Foo")])
    FileKnowledgeBase(cfg, kb_dir=root).resign()
    store = CaseStore(root, cfg)
    assert store.best(OLD)[1] == live
    assert [s.version for s in store.find(NEW).scripts] == [1]


# ----------------------------------------------------------------------------- D3: the triggers converge
def test_record_run_resigns_at_most_once(tmp_path: Path, cfg):
    root = tmp_path / "kb"
    st = CaseStore(root, cfg)
    # (a) the target id is held by another signature
    write_old(st, [ent("e1", tries=[tri(1, 1)])])
    st.add_script(OLD, OLD_L2, "# hh\n")
    st.add_script(NEW, "compile|cannot_find_symbol|somethingElse", "# other\n")
    # (b) a glued line: a torn record, then a whole rules-1 record
    torn = '{"id":"zz","run_id":"r0","project":"p0","case_id":"%s","l2":"%s","kind":"comp' % (OLD, OLD_L2)
    glued_l2 = "compile|cannot_find_symbol|containsKey"
    whole = old_line(ent("g1", l2=glued_l2, raw=loc_raw("method containsKey(java.lang.Object)", HH)))
    # (c) an unparsable line that holds a changed-class l2
    bad = '{"id":"bad1","case_id":"%s","l2":"%s","kind":' % (OLD, OLD_L2)
    with open(st.errors_path, "a", encoding="utf-8") as fh:
        fh.write(torn + whole + "\n" + bad + "\n")
    kb = FileKnowledgeBase(cfg, kb_dir=root)
    calls = {"n": 0}
    real = CaseStore.resign

    def spy(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    CaseStore.resign = spy
    try:
        for i in range(3):
            kb.record_run([ent(f"n{i}", l2="compile|cannot_find_symbol|Zed", sig=2, raw="x")])
    finally:
        CaseStore.resign = real
    assert calls["n"] <= 1
    marker = json.loads((root / KB_MARKER).read_text())
    assert marker["sig"] == sigs.SIGNATURE_VERSION and OLD in marker["skipped"]
    es = {e.id: e for e in CaseStore(root, cfg).iter_errors()}
    new_g = case_id_for("compile|cannot_find_symbol|org.springframework.http.HttpHeaders.containsKey")
    assert es["g1"].case_id == new_g and es["g1"].sig == sigs.SIGNATURE_VERSION     # the glued record re-signed
    assert torn in (root / "errors.jsonl.corrupt").read_text()                      # its torn prefix kept
    assert (es["e1"].case_id, es["e1"].sig) == (OLD, 0)                              # skipped: kept for a retry
    assert any(OLD in p and "skipped" in p for p in CaseStore(root, cfg).validate())


def test_a_damaged_case_is_skipped_and_never_overwritten(tmp_path: Path, cfg):
    root = tmp_path / "kb"
    st = CaseStore(root, cfg)
    write_old(st, [ent("e1", tries=[tri(1, 1)])])
    st.add_script(OLD, OLD_L2, "def fix(p):\n    p.log('HttpHeaders')\n")
    damaged = '{"case_id": "%s", "l2": "x", "scripts": [ ' % OLD
    (root / "cases" / OLD / "case.json").write_text(damaged)
    kb = FileKnowledgeBase(cfg, kb_dir=root)
    rep = kb.resign()
    assert rep.unreadable == [OLD]
    assert (root / "cases" / OLD / "case.json").read_text() == damaged
    assert kb.reindex() >= 0
    assert (root / "cases" / OLD / "case.json").read_text() == damaged             # reindex retried, still skipped
    assert (root / "cases" / OLD / "fix_v1.py").is_file()
    kb.last_resign = None
    kb.record_run([ent("n1", l2="compile|cannot_find_symbol|Bar", sig=2, raw="x")])
    assert kb.last_resign is None and kb.cases.older_lines == 0


# ----------------------------------------------------------------------------- D4: in-house prefixes
def test_the_resign_registers_the_configured_in_house_prefixes(tmp_path: Path, cfg, clean_prefixes):
    fw = cfg.model_copy(update={"build": cfg.build.model_copy(update={"external_package_prefixes": ["com.example.fw"]})})
    l2 = "compile|cannot_find_symbol|doIt"
    root = tmp_path / "kb"
    write_old(CaseStore(root, fw), [ent("e1", l2=l2, raw=loc_raw("method doIt()",
                                                                  "variable svc of type com.example.fw.CommonService"))])
    rep = FileKnowledgeBase(fw, kb_dir=root).resign()
    want = "compile|cannot_find_symbol|com.example.fw.CommonService.doIt"
    assert rep.moves == [(case_id_for(l2), case_id_for(want), want)]


# ----------------------------------------------------------------------------- D5: cut facts
def test_a_long_location_is_stored_whole_and_a_cut_fact_is_read_from_raw(tmp_path: Path, cfg):
    longloc = "variable m of type org.springframework.util.MultiValueMap<" + ",".join(
        ["java.util.Map<java.lang.String,java.util.List<java.lang.Integer>>"] * 5) + ">"
    be = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/A.java", line=3,
                    symbol="method keySet()", location=longloc, raw=loc_raw("method keySet()", longloc))
    be.signature, be.signature_generic = sigs.signature_for(be)
    e = ErrorList(tmp_path / "run", "r1", project="p", cfg=cfg)._from_error(be, "compile")
    assert e.location == longloc
    cut = ent("e2", location=longloc[:40] + "\n…[300 more characters]", symbol="method keySet()",
              raw=loc_raw("method keySet()", HH))
    assert rebuild_error(cut).location == HH
    assert resigned_l2(cut) == NEW_L2


# ----------------------------------------------------------------------------- smaller fixes
def test_a_skipped_plan_rewrites_no_resolved_by(tmp_path: Path, cfg, monkeypatch):
    root = tmp_path / "kb"
    st = CaseStore(root, cfg)
    st.add_script(NEW, NEW_L2, "# existing\n")
    st.add_script(OLD, OLD_L2, "# old\n")
    other = ent("o1", l2="compile|cannot_find_symbol|Other", raw="x", sig=2, resolved="build",
                resolved_by=[ResolvedBy(case_id=OLD, round=1, n=1)])
    write_old(st, [ent("e1", tries=[tri(1, 1)])])
    with open(st.errors_path, "a", encoding="utf-8") as fh:
        fh.write(other.model_dump_json() + "\n")
    monkeypatch.setattr(CaseStore, "_copy_versions", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    rep = st.resign()
    assert rep.unreadable == [OLD]
    es = {e.id: e for e in st.iter_errors()}
    assert es["o1"].resolved_by[0].case_id == OLD and es["e1"].case_id == OLD


def test_merge_from_skips_a_foreign_case_and_needs_the_lock(tmp_path: Path, cfg, monkeypatch):
    theirs = CaseStore(tmp_path / "theirs", cfg)
    theirs.add_script(OLD, OLD_L2, "# theirs\n")
    mine = CaseStore(tmp_path / "mine", cfg)
    mine.add_script(OLD, "compile|cannot_find_symbol|Different", "# mine\n")
    p = mine.cases_dir / OLD / "case.json"
    data = json.loads(p.read_text())
    data["l2"] = "compile|cannot_find_symbol|Different"
    p.write_text(json.dumps(data))
    counters = mine.merge_from(theirs)
    after = json.loads(p.read_text())
    assert counters.get("cases_skipped") == 1 and after["l2"] == "compile|cannot_find_symbol|Different"
    assert [x["version"] for x in after["scripts"]] == [1] and mine.code(OLD, 1) == "# mine\n"
    assert sorted(x.name for x in (mine.cases_dir / OLD).glob("fix_v*.py")) == ["fix_v1.py"]

    from contextlib import contextmanager

    @contextmanager
    def no_lock(self):
        yield False

    write_old(theirs, [ent("e1", tries=[tri(1, 1)])])
    other = CaseStore(tmp_path / "other", cfg)
    monkeypatch.setattr(CaseStore, "lock", no_lock)
    called = {"n": 0}
    monkeypatch.setattr(CaseStore, "_resign_locked", lambda *a, **k: called.__setitem__("n", 1))
    other.merge_from(theirs)
    assert called["n"] == 0


def test_validate_does_not_call_other_a_case(tmp_path: Path, cfg):
    st = CaseStore(tmp_path / "kb", cfg)
    st.record_run([ErrorEntry(id="pv1", run_id="r1", case_id="", l2="", file="pom.xml", message="m",
                              status="pom-verify", seen="restructure")])
    assert (st.cases_dir / "_other" / "errors" / "r1-pv1.json").is_file()
    assert st.validate() == []


# ----------------------------------------------------------------------------- D2: kills at every write step
def _build(root: Path, cfg) -> None:
    ck_l2, sz_l2 = "compile|cannot_find_symbol|containsKey", "compile|cannot_find_symbol|size"
    ck_new = "compile|cannot_find_symbol|org.springframework.http.HttpHeaders.containsKey"
    st = CaseStore(root, cfg)
    write_old(st, [ent("a1", tries=[tri(1, 1)]),
                   ent("b1", l2=ck_l2, raw=loc_raw("method containsKey(java.lang.String)", HH), tries=[tri(1, 1)]),
                   ent("c1", l2=sz_l2, raw=loc_raw("method size()", HH), tries=[tri(1, 1)]),
                   ent("c2", l2=sz_l2, raw=loc_raw("method size()", MAP), tries=[tri(1, 2, "no_change")], run="r2",
                       project="p2"),
                   ent("c3", l2=sz_l2, raw="cannot find symbol\n  symbol: method size()", run="r3", project="p3")])
    st.add_script(OLD, OLD_L2, "# keyset v1\n")
    st.add_script(case_id_for(ck_l2), ck_l2, "# containsKey v1\n")
    st.set_pin(case_id_for(ck_l2), "block:owner says no")
    st.add_script(case_id_for(ck_new), ck_new, "# CK_NEW existing\n")
    for c in ("# size v1\n", "# size v2\n", "# size v3\n"):
        st.add_script(case_id_for(sz_l2), sz_l2, c)
    st.recount()
    for d in (root / "cases").iterdir():
        p = d / "case.json"
        if p.exists():
            x = json.loads(p.read_text())
            x["sig"] = 0
            p.write_text(json.dumps(x))


def _state(root: Path, cfg):
    st = CaseStore(root, cfg)
    cases = {}
    for cid in st.case_ids():
        h = st._read(cid, quiet=True)
        cases[cid] = (h[0].l2, sorted((s.version, hashlib.sha1((st.code(cid, s.version) or "").encode()).hexdigest(),
                                       s.successes, s.failures, s.status) for s in h[0].scripts), h[1].get("pin"))
    ents = sorted((e.id, e.case_id, e.sig) for e in st.iter_errors())
    return cases, ents, st.validate()


class Kill(BaseException):
    pass


def test_a_kill_at_every_write_step_converges(tmp_path: Path, cfg):
    proto = tmp_path / "proto"
    _build(proto, cfg)
    clean = tmp_path / "clean"
    shutil.copytree(proto, clean)
    FileKnowledgeBase(cfg, kb_dir=clean).resign()
    want = _state(clean, cfg)
    assert want[2] == []
    wanted_scripts = set(scripts(proto).values())
    counter = {"n": 0, "at": 0}
    orig = dict(rename=os.rename, aw=cm.atomic_write, awb=cm.atomic_write_bytes, unlink=pathlib.Path.unlink,
                rmdir=pathlib.Path.rmdir, link=os.link, replace=os.replace)

    def wrap(fn):
        def w(*a, **k):
            counter["n"] += 1
            if counter["n"] == counter["at"]:
                raise Kill()
            return fn(*a, **k)
        return w

    def patch(on: bool) -> None:
        os.rename = wrap(orig["rename"]) if on else orig["rename"]
        os.replace = wrap(orig["replace"]) if on else orig["replace"]
        cm.atomic_write = wrap(orig["aw"]) if on else orig["aw"]
        cm.atomic_write_bytes = wrap(orig["awb"]) if on else orig["awb"]
        pathlib.Path.unlink = wrap(orig["unlink"]) if on else orig["unlink"]
        pathlib.Path.rmdir = wrap(orig["rmdir"]) if on else orig["rmdir"]
        os.link = wrap(orig["link"]) if on else orig["link"]

    k, bad = 1, []
    while k < 200:
        root = tmp_path / f"crash{k}"
        shutil.copytree(proto, root)
        counter.update(n=0, at=k)
        patch(True)
        killed = False
        try:
            CaseStore(root, cfg).resign()
        except Kill:
            killed = True
        finally:
            patch(False)
        if not killed:
            break
        for p in root.glob(".lock*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        FileKnowledgeBase(cfg, kb_dir=root).resign()
        again = FileKnowledgeBase(cfg, kb_dir=root).resign()
        got = _state(root, cfg)
        if got != want or again.changed or not wanted_scripts <= set(scripts(root).values()):
            bad.append((k, got, again.summary()))
        shutil.rmtree(root)
        k += 1
    assert k > 10 and bad == []


# ----------------------------------------------------------------------------- D7: only the affected lines are parsed
def test_an_automatic_resign_parses_only_the_affected_cases(tmp_path: Path, cfg, monkeypatch):
    root = tmp_path / "kb"
    st = CaseStore(root, cfg)
    with open(st.errors_path if root.mkdir(parents=True) is None else st.errors_path, "w", encoding="utf-8") as fh:
        for i in range(400):
            fh.write(ent(f"c{i:04d}", l2=f"compile|cannot_find_symbol|K{i % 40}", raw="x", sig=2).model_dump_json()
                     + "\n")
    st._write_marker()
    write_old(st, [ent(f"old{i}", tries=[tri(1, 1)]) for i in range(3)])
    before = st.errors_path.read_bytes().split(b"\n")
    calls = {"n": 0}
    real = ErrorEntry.model_validate_json

    def counting(cls, data, **kw):
        calls["n"] += 1
        return real(data, **kw)

    monkeypatch.setattr(ErrorEntry, "model_validate_json", classmethod(counting))
    kb = FileKnowledgeBase(cfg, kb_dir=root)
    kb.record_run([ent("n1", l2="compile|cannot_find_symbol|Zed", sig=2, raw="x")])
    assert kb.last_resign is not None and kb.last_resign.moves == [(OLD, NEW, NEW_L2)]
    assert calls["n"] < 40                                         # never a parse of the 400 current lines
    after = st.errors_path.read_bytes().split(b"\n")
    assert after[:400] == before[:400]                             # untouched lines byte for byte
