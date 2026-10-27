"""sbmig.errors.entries: the run's error list (docs/v2/PLAN-v2.1.md §3.3) — compile → restructure (pom-verify) →
build (only new errors) → fix rounds (tries, resolve) → end of run; errors.jsonl / errors.md; ids; masking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.models import BuildError, BuildResult, ErrorKind
from sbmig.errors import ErrorList, case_id_for, case_id_of, entry_id, render_md, tries_text

JAXB = "compile|package_does_not_exist|jakarta.xml.bind"
SEC = "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"


def err(l2=JAXB, file="src/main/java/com/x/A.java", line=6, message="package jakarta.xml.bind does not exist",
        kind=ErrorKind.COMPILE, module=None, raw="") -> BuildError:
    return BuildError(kind=kind, message=message, file=file, line=line, signature=l2 + "|L1",
                      signature_generic=l2, module=module, raw=raw)


def build(*errors, ok=None, timed_out=False, modules_built=(), failed_module=None) -> BuildResult:
    return BuildResult(ok=not errors if ok is None else ok, goal="package", command=["mvn"], exit_code=0 if not errors else 1,
                       duration_s=1.0, log_path="/x.log", errors=list(errors), timed_out=timed_out,
                       modules_built=list(modules_built), failed_module=failed_module)


@pytest.fixture()
def el(tmp_path: Path, cfg) -> ErrorList:
    return ErrorList(tmp_path / "run", "run-1", project="demo", cfg=cfg, roots=[tmp_path / "run" / "project"])


def lines(el: ErrorList) -> list[dict]:
    return [json.loads(x) for x in el.path.read_text(encoding="utf-8").splitlines()]


def test_ids_are_stable_and_follow_the_formula():
    assert case_id_for(JAXB) == case_id_for(JAXB) and len(case_id_for(JAXB)) == 10 and case_id_for("") == ""
    e = err()
    assert case_id_of(e) == case_id_for(JAXB)
    no_l2 = err(l2="")
    assert case_id_of(no_l2) and case_id_of(no_l2) == case_id_of(err(l2=""))
    assert entry_id("r", "c", "a\\B.java", 3, "m") == entry_id("r", "c", "./a/B.java", 3, "m")
    assert entry_id("r", "c", "a", 3, "m") != entry_id("r", "c", "a", 4, "m") and len(entry_id("r", "c", "a", 3, "m")) == 12


def test_the_triage_and_the_kb_shim_use_the_same_case_id():
    from sbmig.kb.catalog import case_id_for as shim
    from sbmig.triage.cluster import case_id_of as triage_case_id_of

    assert shim(SEC) == case_id_for(SEC)
    for e in (err(), err(l2="", file="X.java")):
        assert triage_case_id_of(e) == case_id_of(e)


def test_lifecycle_compile_build_fix_round_end(el: ErrorList):
    a1, a2 = err(line=6), err(file="src/main/java/com/x/B.java", line=3)
    s1 = err(SEC, file="src/main/java/com/x/Sec.java", line=9, message="cannot find symbol")
    new = el.add_build(build(a1, a2, s1), "compile")
    assert [e.seen for e in new] == ["compile"] * 3 and {e.status for e in new} == {"open"}
    jaxb = case_id_for(JAXB)
    assert [e.file for e in el.open_cases()[jaxb]] == ["src/main/java/com/x/A.java", "src/main/java/com/x/B.java"]
    ids = [e.id for e in new]
    assert ids[0] == entry_id("run-1", jaxb, "src/main/java/com/x/A.java", 6, "package jakarta.xml.bind does not exist")

    # build 1: the same errors again + one new → only the new one is added
    extra = err(SEC, file="src/main/java/com/x/Web.java", line=2, message="cannot find symbol")
    added = el.add_build(build(a1, a2, s1, extra), "build")
    assert [e.file for e in added] == ["src/main/java/com/x/Web.java"] and added[0].seen == "build"
    assert len(el.entries) == 4

    # fix round 1: a try on the JAXB case (both open entries share it)
    t = el.begin_try(jaxb, 1, "llm-new")
    assert t.n == 1 and all(e.tries and e.tries[0] is t for e in el.entries_for(jaxb))
    el.finish_try(jaxb, 1, analysis="JAXB left the JDK", solution="declare jakarta.xml.bind-api", applied="edits",
                  files=["pom.xml"], diff="+<dependency>jakarta.xml.bind-api</dependency>", commit="a1b2c3d4e5f6",
                  llm_calls=1, seconds=21.4)
    # the build that scores round 1: the JAXB errors are gone, but the try is pending → open until the verdict
    el.add_build(build(s1, extra), "fix r1")
    assert {e.status for e in el.entries_for(jaxb)} == {"open"}
    el.finish_try(jaxb, 1, result="improved")
    done = el.resolve(jaxb, 1, 1)
    assert [e.resolved for e in done] == ["r1 try 1", "r1 try 1"] and all(e.status == "resolved" for e in done)
    assert done[0].script == ""                                  # edits did it, not a stored script

    # round 2: the security case by a stored script, the errors stay → no verdict; then blocked
    sec = case_id_for(SEC)
    t2 = el.begin_try(sec, 2, "kb-script", 3)
    el.finish_try(sec, t2.n, applied="script", result="no_change", note="script ran, nothing changed")
    el.add_build(build(s1, extra), "fix r2")
    assert {e.status for e in el.entries_for(sec)} == {"open"}
    assert el.block(sec, "external API: Spring Security 7") and {e.status for e in el.entries_for(sec)} == {"blocked"}
    counts = el.end_of_run()
    assert counts == {"resolved": 2, "blocked": 2}
    assert tries_text(el.entries_for(sec)[0]) == "1 kb-script v3 script no_change"
    rows = lines(el)
    assert len(rows) == 4 and rows[0]["tries"][0]["diff"].startswith("+<dependency>")
    assert rows[0]["resolved"] == "r1 try 1" and rows[0]["tries"][0]["commit"] == "a1b2c3d4e5f6"


def test_an_absent_entry_without_a_pending_try_is_resolved_by_the_build(el: ErrorList):
    a, b = err(line=6), err(SEC, file="S.java", line=1, message="cannot find symbol")
    el.add_build(build(a, b), "compile")
    el.add_build(build(b), "build")
    assert el.entries[0].status == "resolved" and el.entries[0].resolved == "build"
    assert el.entries[1].status == "open"
    # it comes back → reopened, the note says so
    el.add_build(build(a, b), "fix r1")
    assert el.entries[0].status == "open" and "reappeared in fix r1" in el.entries[0].note
    assert len(el.entries) == 2


def test_a_scored_try_resolves_its_entries_at_the_next_build_even_without_a_verdict(el: ErrorList):
    a, b = err(line=6), err(SEC, file="S.java", line=1, message="cannot find symbol")
    el.add_build(build(a, b), "build")
    cid = case_id_for(JAXB)
    el.begin_try(cid, 1, "kb-script", 2)
    el.finish_try(cid, 1, applied="script", result="improved")   # scored before the build: nothing pending
    el.add_build(build(b), "fix r1")
    e = el.entries_for(cid)[0]
    assert e.status == "resolved" and e.resolved == "r1 try 1" and e.script == f"cases/{cid}/fix_v2.py"


def test_end_of_run_resolves_a_pending_claim_and_marks_the_rest_unresolved(el: ErrorList):
    a, b = err(line=6), err(SEC, file="S.java", line=1, message="cannot find symbol")
    el.add_build(build(a, b), "build")
    cid = case_id_for(JAXB)
    el.begin_try(cid, 1, "llm-new")
    el.add_build(build(b), "fix r1")                              # the verdict never comes
    assert el.end_of_run() == {"resolved": 1, "unresolved": 1}
    assert el.entries_for(cid)[0].resolved == "r1 try 1"


def test_a_moved_line_keeps_the_entry(el: ErrorList):
    el.add_build(build(err(line=6)), "compile")
    assert el.add_build(build(err(line=7)), "build") == []        # an import added above it
    assert len(el.entries) == 1 and el.entries[0].line == 7 and el.entries[0].status == "open"


def test_the_restructure_move_keeps_compile_entries(el: ErrorList):
    el.add_build(build(err(file="src/main/java/com/x/A.java")), "compile")
    el.add_build(build(err(file="module-a/src/main/java/com/x/B.java", line=3)), "compile")
    new = el.add_build(build(err(file="demo-back/src/main/java/com/x/A.java"),
                             err(file="demo-back/src/main/java/com/x/B.java", line=3)), "build")
    assert new == [] and [e.file for e in el.entries] == ["demo-back/src/main/java/com/x/A.java",
                                                          "demo-back/src/main/java/com/x/B.java"]
    # entries first seen after the restructure never follow a path change (MSA services share src/ tails)
    el.add_build(build(err(file="svc-a/src/main/java/C.java", line=1)), "build")
    new = el.add_build(build(err(file="svc-b/src/main/java/C.java", line=1)), "build")
    assert [e.file for e in new] == ["svc-b/src/main/java/C.java"]


def test_the_same_error_twice_in_one_build_is_one_entry(el: ErrorList):
    assert len(el.add_build(build(err(), err()), "compile")) == 1
    assert el.add_build(build(err(), err()), "build") == [] and len(el.entries) == 1


def test_synthetic_and_void_builds_resolve_nothing(el: ErrorList):
    el.add_build(build(err()), "compile")
    stand_in = BuildError(kind=ErrorKind.UNKNOWN, message="build timed out after 600 s")
    assert el.add_build(build(stand_in, timed_out=True), "build") == []
    assert el.entries[0].status == "open" and len(el.entries) == 1
    el.add_build([], "build", void=True)
    el.add_build(build(ok=False), "build")                        # failed without parsed errors
    assert el.entries[0].status == "open"
    el.add_build([], "build")                                     # an empty list = a clean build
    assert el.entries[0].status == "resolved"


def test_an_error_hidden_by_an_earlier_stage_stays_open(el: ErrorList):
    el.add_build(build(err(module="demo-back")), "compile")
    pom = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not resolve x:y:1", module="demo-back")
    el.add_build(build(pom), "build")                             # dependency resolution stops before compile
    assert el.entries[0].status == "open"
    other = err(SEC, file="demo-boot/src/A.java", line=1, message="cannot find symbol", module="demo-boot")
    el.add_build(build(other, failed_module="demo-boot", modules_built=["demo-back"]), "fix r1")
    assert el.entries[0].status == "resolved"                     # its module built this time
    skipped = err(file="demo-web/src/W.java", module="demo-web")
    el.add_build(build(skipped, other, failed_module="demo-boot"), "fix r2")
    el.add_build(build(other, failed_module="demo-boot"), "fix r3")   # demo-web skipped after demo-boot failed
    assert el.entries[-1].status == "open"


def test_pom_verify_is_verified_or_linked(el: ErrorList):
    a = el.add_pom_verify("module/comp-jar/pom.xml", "parent → base-parent 4.0.0")
    b = el.add_pom_verify("pom.xml", "added mandatory dependency com.company:core")
    assert a.status == "pom-verify" and a.seen == "restructure" and a.module == "comp-jar" and a.case_id == ""
    assert el.add_pom_verify("pom.xml", "added mandatory dependency com.company:core") is b
    bad = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact com.company:core:4.0.0",
                     file="pom.xml")
    done = el.verify_poms(build(bad))
    assert done == [a] and a.resolved == "build" and b.status == "pom-verify"
    assert "build: Could not find artifact com.company:core:4.0.0" in b.note
    el.verify_poms(build(bad))
    assert b.note.count("build:") == 1
    by_module = el.add_pom_verify("demo-back/pom.xml", "parent → comp-jar")
    el.verify_poms(build(BuildError(kind=ErrorKind.PARENT_RESOLUTION, message="parent not found", module="demo-back")))
    assert by_module.status == "pom-verify"
    assert el.verify_poms(build(timed_out=True, ok=False)) == []


def test_review_note_with_a_review_fix(el: ErrorList):
    n = el.add_note("module/comp-jar/pom.xml", "sourceEncoding is MS949\nshould be UTF-8")
    w = el.add_note(None, "two boot modules")
    assert n.status == "note" and n.seen == "review" and n.message == "sourceEncoding is MS949" and "UTF-8" in n.note
    t = el.begin_try("", 0, "review", entry_ids=[n.id])
    el.finish_try("", t.n, applied="edits", diff="-MS949\n+UTF-8", result="improved")
    assert el.resolve("", 0, t.n, entry_ids=[n.id]) == [n] and n.resolved == "review"
    assert w.status == "note" and not w.tries


def test_try_numbers_count_per_case(el: ErrorList):
    el.add_build(build(err(), err(SEC, file="S.java", message="cannot find symbol")), "build")
    j, s = case_id_for(JAXB), case_id_for(SEC)
    assert [el.begin_try(j, 1, "llm-new").n, el.begin_try(s, 1, "llm-new").n, el.begin_try(j, 2, "llm-augment").n] \
        == [1, 1, 2]
    assert el.finish_try(j, 9, result="improved") is None


def test_texts_are_masked_and_roots_stripped_diff_capped(tmp_path: Path, cfg):
    cfg2 = cfg.model_copy(update={"errors": cfg.errors.model_copy(update={"diff_chars": 40, "raw_chars": 30})})
    root = tmp_path / "run" / "project"
    el = ErrorList(tmp_path / "run", "r", project="p", cfg=cfg2, roots=[root])
    e = el.add_build(build(err(raw=f"[ERROR] {root}/src/A.java password=hunter2 " + "x" * 80)), "compile")[0]
    assert "hunter2" not in e.raw and str(root) not in e.raw and len(e.raw) < 80
    cid = e.case_id
    el.begin_try(cid, 1, "llm-new")
    el.finish_try(cid, 1, analysis="spring.datasource.password=hunter2", diff="+db.password=hunter2\n" + "+x\n" * 50)
    text = el.path.read_text(encoding="utf-8")
    assert "hunter2" not in text and "more characters" in text
    assert "hunter2" not in el.md_path.read_text(encoding="utf-8")
    rep = el.to_report()
    assert rep[0].id == e.id and rep[0] is not e and len(rep[0].tries[0].diff) <= 40 + 40


def test_errors_md_table_and_tries(el: ErrorList):
    el.add_build(build(err(), err(SEC, file="S.java", line=2, message="cannot find symbol | pipes")), "compile")
    el.add_pom_verify("pom.xml", "parent → base-parent")
    cid = case_id_for(JAXB)
    el.begin_try(cid, 1, "llm-new")
    el.finish_try(cid, 1, analysis="원인: JAXB", solution="해결: 의존성 추가", applied="edits", files=["pom.xml"],
                  diff="+<dependency/>", result="improved", commit="abcdef1234567")
    md = el.md_path.read_text(encoding="utf-8")
    assert "| # | seen | status | case | file:line | message | tries | resolved |" in md
    assert f"| 1 | compile | open | {cid} | src/main/java/com/x/A.java:6 |" in md
    assert "1 llm-new edits improved" in md and "cannot find symbol \\| pipes" in md
    assert "| 3 | restructure | pom-verify | - | pom.xml |" in md and "parent → base-parent" in md
    assert f"### case {cid} try 1" in md and "- 원인: 원인: JAXB" in md and "```diff\n+<dependency/>\n```" in md
    assert render_md([]).startswith("# Error list")


def test_md_view_off_writes_only_the_jsonl(tmp_path: Path, cfg):
    cfg2 = cfg.model_copy(update={"errors": cfg.errors.model_copy(update={"md_view": False})})
    el = ErrorList(tmp_path / "run", "r", cfg=cfg2)
    el.add_build(build(err()), "compile")
    assert el.path.is_file() and not el.md_path.exists()
    assert [e.id for e in ErrorList.load(el.path)] == [el.entries[0].id]


def test_a_write_failure_never_raises(tmp_path: Path, cfg):
    blocker = tmp_path / "run"
    blocker.write_text("a file where the run dir should be")
    el = ErrorList(blocker, "r", cfg=cfg)
    el.add_build(build(err()), "compile")
    assert el.entries and el.problems


def test_for_workspace(tmp_path: Path, cfg):
    from sbmig.core.workspace import Workspace

    ws = Workspace(tmp_path / "proj", tmp_path / "run", "r9")
    el = ErrorList.for_workspace(ws, cfg, project="demo")
    assert el.run_id == "r9" and el.project == "demo" and el.path == ws.run_dir / "errors.jsonl"
    e = el.add_build(build(err(raw=f"{ws.root}/src/A.java:[6,1] error")), "compile")[0]
    assert str(ws.root) not in e.raw and e.raw.startswith("src/A.java")


# ---------------------------------------------------------------------------------------- v2.1b 수정 notes
POM_NOTE = {"path": "module/comp-jar/pom.xml", "where": "<dependencies> 블록", "lines": "+42–47",
            "change": "jakarta.xml.bind-api 와 jaxb-runtime 의존성을 추가합니다", "source": "llm"}


def test_finish_try_stores_the_edit_notes_masked(tmp_path: Path, cfg):
    from sbmig.core.models import EditNote

    root = tmp_path / "run" / "project"
    el = ErrorList(tmp_path / "run", "r", project="p", cfg=cfg, roots=[root])
    cid = el.add_build(build(err()), "compile")[0].case_id
    el.begin_try(cid, 1, "llm-new")
    diff_note = EditNote(path=f"{root}/src/A.java", lines="~3", change="A.java 3행 수정", source="diff")
    el.finish_try(cid, 1, applied="edits", diff="+x",
                  edits=[POM_NOTE, diff_note, {"where": "no path"},
                         EditNote(path="app.properties", change="db.password=hunter2 로 바꿉니다")])
    t = el.get_try(cid, 1)
    assert [n.path for n in t.edits] == ["module/comp-jar/pom.xml", "src/A.java", "app.properties"]   # no path → out
    assert t.edits[0] == EditNote(**POM_NOTE) and (t.edits[1].source, t.edits[1].lines) == ("diff", "~3")
    assert "hunter2" not in t.edits[2].change
    el.finish_try(cid, 1, result="improved")                 # edits None → kept
    assert len(el.get_try(cid, 1).edits) == 3
    row = lines(el)[0]["tries"][0]
    assert row["edits"][0]["lines"] == "+42–47" and row["edits"][1]["source"] == "diff"
    assert "hunter2" not in el.path.read_text(encoding="utf-8")
    assert el.to_report()[0].tries[0].edits[0].change.startswith("jakarta.xml.bind-api")


def test_errors_md_lists_the_edit_notes_before_the_diff(el: ErrorList):
    cid = el.add_build(build(err()), "compile")[0].case_id
    el.begin_try(cid, 1, "llm-new")
    el.finish_try(cid, 1, analysis="JAXB left the JDK", solution="의존성 추가", applied="edits",
                  files=["module/comp-jar/pom.xml"], diff="+<dependency/>", result="improved",
                  edits=[POM_NOTE, {"path": "src/A.java", "lines": "~3", "change": "A.java 3행 수정", "source": "diff"}])
    md = el.md_path.read_text(encoding="utf-8")
    block = md[md.index(f"### case {cid} try 1"):]
    assert ("- 수정:\n  - module/comp-jar/pom.xml +42–47 (<dependencies> 블록) — jakarta.xml.bind-api 와 jaxb-runtime "
            "의존성을 추가합니다\n  - src/A.java ~3 — A.java 3행 수정\n") in block
    assert block.index("- 해결: 의존성 추가") < block.index("- 수정:") < block.index("- files:") < block.index("```diff")
    # the compact tries cell: the first note's change, ≤ 60 characters
    assert "| 1 llm-new edits improved (jakarta.xml.bind-api 와 jaxb-runtime 의존성을 추가합니다) |" in md
    assert tries_text(el.entries[0]) == "1 llm-new edits improved"                  # the plain text stays plain
    long = "x" * 80
    el.finish_try(cid, 1, edits=[{"path": "a", "change": long}])
    assert f"1 llm-new edits improved ({'x' * 59}…)" in tries_text(el.entries[0], with_change=True)


def test_kb_entry_lines_show_the_edit_notes(el: ErrorList):
    from sbmig.errors import entry_lines, note_text

    cid = el.add_build(build(err()), "compile")[0].case_id
    el.begin_try(cid, 1, "llm-new")
    el.finish_try(cid, 1, applied="edits", diff="+x", edits=[POM_NOTE])
    out = "\n".join(entry_lines(el.entries[0]))
    assert "    수정:\n      module/comp-jar/pom.xml +42–47 (<dependencies> 블록) — jakarta.xml.bind-api" in out
    assert out.index("수정:") < out.index("diff:")
    assert note_text({"path": "pom.xml"}) == "pom.xml" and note_text({"path": "p", "lines": "-3"}) == "p -3"
    assert note_text(POM_NOTE, limit=30) == "module/comp-jar/pom.xml +42–4…"
