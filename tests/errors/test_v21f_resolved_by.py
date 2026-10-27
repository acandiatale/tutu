"""v2.1f (2026-10-28): an error that disappears from a build without a try of its own names the same-round try that
changed its file or a pom above it (`ErrorEntry.resolved_by`); views show `build ← <case> r<round> try <n>`."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import BuildError, BuildResult, EditNote, ErrorKind
from sbmig.errors import ErrorList, case_id_for, render_md
from sbmig.errors.entries import probable_fixers, resolved_text

JAXB = "compile|package_does_not_exist|jakarta.xml.bind"
XML = "compile|cannot_find_symbol|XmlElement"
SEC = "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"
A = "module/comp-jar/src/main/java/com/x/A.java"
SECF = "module/comp-jar/src/main/java/com/x/Sec.java"


def err(l2, file=A, line=6, message="cannot find symbol") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=line, signature=l2 + "|L1",
                      signature_generic=l2)


def build(*errors) -> BuildResult:
    return BuildResult(ok=not errors, goal="package", command=["mvn"], exit_code=0 if not errors else 1,
                       duration_s=1.0, log_path="/x.log", errors=list(errors))


@pytest.fixture()
def el(tmp_path: Path, cfg) -> ErrorList:
    return ErrorList(tmp_path / "run", "run-1", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])


def _try(el: ErrorList, case_id: str, rnd: int, files: list[str], *, result="improved", commit="abc1234",
         note_path=None, change="jakarta.xml.bind-api 의존성을 추가합니다") -> int:
    t = el.begin_try(case_id, rnd, "llm-new", script_version=1)
    edits = [EditNote(path=note_path or files[0], lines="+28–36", change=change)]
    el.finish_try(case_id, t.n, applied="script", files=files, commit=commit, result=result, edits=edits)
    return t.n


def test_an_error_gone_without_a_try_names_the_round_try_that_touched_its_module_pom(el: ErrorList):
    jaxb, xml, sec = err(JAXB, line=6, message="package jakarta.xml.bind does not exist"), err(XML, line=22), \
        err(SEC, file=SECF, line=9)
    el.add_build(build(jaxb, xml, sec), "compile")
    cj, cx, cs = case_id_for(JAXB), case_id_for(XML), case_id_for(SEC)
    n = _try(el, cj, 1, ["module/comp-jar/pom.xml"])
    # round 1's build: the jaxb try resolves its own entry (resolve()), the XmlElement entry is simply gone
    el.add_build(build(sec), "fix r2", round=1)
    el.resolve(cj, 1, n)
    by_case = {e.case_id: e for e in el.entries}
    x = by_case[cx]
    assert x.status == "resolved" and x.resolved == "build"
    assert [(r.case_id, r.round, r.n, r.match, r.files) for r in x.resolved_by] == \
        [(cj, 1, n, "pom", ["module/comp-jar/pom.xml"])]
    assert x.resolved_by[0].text == f"{cj} r1 try {n} — module/comp-jar/pom.xml +28–36 — jakarta.xml.bind-api 의존성을 추가합니다"
    assert x.resolved_by[0].commit == "abc1234"
    assert resolved_text(x) == f"build ← {cj} r1 try {n}"
    j = by_case[cj]
    assert j.resolved == f"r1 try {n}" and j.resolved_by == [] and resolved_text(j) == f"r1 try {n}"
    assert by_case[cs].status == "open"
    md = render_md(el.entries, project="demo", run_id="run-1")
    assert f"| build ← {cj} r1 try {n} |" in md
    # the record round-trips through errors.jsonl
    el.write()
    text = el.path.read_text(encoding="utf-8")
    assert '"resolved_by":[{"case_id":"' + cj in text.replace(" ", "")


def test_the_same_file_wins_over_a_pom_and_a_reverted_try_never_fixes(el: ErrorList):
    jaxb, xml, sec = err(JAXB, line=6), err(XML, line=22), err(SEC, file=SECF, line=9)
    el.add_build(build(jaxb, xml, sec), "compile")
    cj, cx, cs = case_id_for(JAXB), case_id_for(XML), case_id_for(SEC)
    pom_try = _try(el, cj, 1, ["pom.xml"], commit="p0m")                                # root pom: depth 0
    file_try = _try(el, cs, 1, [A, SECF], commit="f1le", change="import 를 jakarta 로 바꿉니다")
    _try(el, cs, 1, [A], result="reverted", commit="rev")                               # reverted: never a fixer
    _try(el, cj, 0, [A], commit="old")                                                  # another round: not scored now
    el.add_build(build(sec), "fix r2", round=1)
    x = next(e for e in el.entries if e.case_id == cx)
    assert [(r.case_id, r.match) for r in x.resolved_by] == [(cs, "file"), (cj, "pom")]
    assert x.resolved_by[0].n == file_try and x.resolved_by[1].n == pom_try


def test_probable_fixers_helper_edge_cases():
    from sbmig.core.models import ErrorEntry, FixTry
    e = ErrorEntry(id="e", case_id="c", file="a/b/C.java", resolved="build")
    tries = [("k1", FixTry(n=1, round=2, source="llm-new", applied="edits", files=["a/b/C.java"], commit="x")),
             ("k2", FixTry(n=1, round=2, source="llm-new", applied="none", files=["a/b/C.java"])),
             ("k3", FixTry(n=2, round=2, source="kb-script", applied="script", files=["a/pom.xml"], commit="y")),
             ("k4", FixTry(n=1, round=2, source="llm-new", applied="script", files=["z/pom.xml"], commit="z")),
             ("k5", FixTry(n=1, round=3, source="llm-new", applied="script", files=["a/b/C.java"], commit="w"))]
    got = probable_fixers(e, tries, rounds=[2])
    assert [(r.case_id, r.match) for r in got] == [("k1", "file"), ("k3", "pom")]
    assert [r.case_id for r in probable_fixers(e, tries)] == ["k1", "k5", "k3"]          # any round, file first
    assert probable_fixers(e, tries, rounds=[2], match="reindex")[0].match == "reindex"
    assert probable_fixers(ErrorEntry(id="n", case_id="c", resolved="build"), tries) == []   # no file: nothing
    assert probable_fixers(e, tries, rounds=[2], limit=1) == got[:1]


def test_reindex_backfills_the_probable_fixer_for_old_records(tmp_path: Path, cfg, caplog):
    """A KB written before v2.1f: `resolved: build` entries without tries get the run's later applied try that
    touched their file / module pom (match `reindex`); pom-verify entries and entries with tries are untouched;
    a second reindex changes nothing."""
    import json
    import logging

    from sbmig.core.models import ErrorEntry, FixTry
    from sbmig.errors import CaseStore
    cj, cx = case_id_for(JAXB), case_id_for(XML)
    pom = "module/comp-jar/pom.xml"
    fixer = FixTry(n=1, round=1, source="llm-new", script_version=1, applied="script", files=[pom], commit="c0ffee",
                   result="improved", edits=[EditNote(path=pom, lines="+28–36", change="jakarta.xml.bind-api 추가")])
    gone = ErrorEntry(id="e1", run_id="r1", project="p1", case_id=cx, l2=XML, kind=ErrorKind.COMPILE, file=A, line=22,
                      message="cannot find symbol", seen="compile", status="resolved", resolved="build")
    fixed = ErrorEntry(id="e2", run_id="r1", project="p1", case_id=cj, l2=JAXB, kind=ErrorKind.COMPILE, file=A,
                       line=6, message="package jakarta.xml.bind does not exist", seen="compile", status="resolved",
                       resolved="r1 try 1", tries=[fixer])
    pomv = ErrorEntry(id="e3", run_id="r1", project="p1", case_id="", file="pom.xml", message="parent set",
                      seen="restructure", status="resolved", resolved="build", note="parent set")
    other_run = ErrorEntry(id="e4", run_id="r2", project="p2", case_id=cx, l2=XML, kind=ErrorKind.COMPILE, file=A,
                           line=22, message="cannot find symbol", seen="fix r2", status="resolved", resolved="build")
    store = CaseStore(tmp_path / "kb", cfg)
    assert store.record_run([other_run, gone, fixed, pomv]) == 4      # r2 first: r1 is the case's latest run
    caplog.set_level(logging.INFO, logger="sbmig.errors")
    store.recount()
    assert any("1 build-resolved entr(ies) given a probable fixer" in r.getMessage() for r in caplog.records)
    e1 = json.loads(store.entry_file(gone).read_text(encoding="utf-8"))
    assert [(r["case_id"], r["round"], r["n"], r["match"], r["files"]) for r in e1["resolved_by"]] == \
        [(cj, 1, 1, "reindex", [pom])]
    assert e1["resolved_by"][0]["text"].startswith(f"{cj} r1 try 1 — {pom} +28–36")
    assert json.loads(store.entry_file(fixed).read_text(encoding="utf-8"))["resolved_by"] == []
    assert json.loads(store.entry_file(other_run).read_text(encoding="utf-8"))["resolved_by"] == []   # no try in r2
    e3_line = next(ln for ln in store.errors_path.read_text(encoding="utf-8").splitlines() if '"id":"e3"' in ln)
    assert '"resolved_by":[]' in e3_line.replace(" ", "")
    assert store.find(cx) is not None
    assert "build ← " + cj in store.md_path.read_text(encoding="utf-8")
    caplog.clear()
    store.recount()
    assert any("0 build-resolved entr(ies) given a probable fixer" in r.getMessage() for r in caplog.records)
