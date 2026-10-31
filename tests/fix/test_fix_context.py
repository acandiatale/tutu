"""Context packing helpers + ContextPacker.pack_script: notes selection, related project types, truncation order.
(The one-case-many-files budget of pack_script is in test_fix_script_context.py.)"""
from sbmig.core.interfaces import FixContext
from sbmig.core.models import (BuildError, DecisionKind, ErrorCluster, ErrorKind, FileChange, FixAttempt, Phase,
                               TriageDecision)
from sbmig.fix import ContextPacker
from sbmig.fix.context import (EDITS_TRUNCATE_ORDER, SCRIPT_TRUNCATE_ORDER, merge_windows, nearest_pom,
                               pom_excerpt, summarize_attempt)
from sbmig.fix.context import summarize_script_attempt

A = "src/main/java/a/A.java"
SMALL = "package a;\n\nimport javax.persistence.Entity;\n\n@Entity\npublic class A {\n}\n"
RELATED = ContextPacker.SCRIPT_RELATED_FILES_TITLE


def ctx(**kw):
    return FixContext(phase=kw.pop("phase", Phase.VERIFY), iteration=1, script_mode="new", case_id="k1", **kw)


def case(file: str = A, lines=(3,), message: str = "cannot find symbol", symbol: str | None = "class Foo",
         **kw) -> ErrorCluster:
    errs = [BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=ln, column=5, symbol=symbol,
                       location="class a.A", signature=f"sig{ln}", **kw) for ln in lines]
    return ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=errs, case_id="k1",
                        signatures=[e.signature for e in errs])


def notes_dir(tmp_path):
    d = tmp_path / "notes"
    d.mkdir(exist_ok=True)
    (d / "fix-policy.md").write_text("---\ntitle: Fix policy\nkeywords: []\n---\nPOLICY " + "p" * 1000 + "\n")
    (d / "jak.md").write_text("---\ntitle: Jakarta\n---\nJAKARTA NOTE " + "j" * 3000 + "\n")
    (d / "sec.md").write_text("---\ntitle: Security\n---\nSECURITY NOTE\n")
    (d / "boot.md").write_text("---\ntitle: Boot only\n---\nBOOT NOTE\n")
    (d / "jdk.md").write_text("---\ntitle: JDK only\n---\nJDK NOTE\n")
    (d / "index.yml").write_text(
        "notes:\n"
        "- {file: fix-policy.md, title: Fix policy, keywords: [], cases: [], phases: [], always: true}\n"
        "- {file: jak.md, title: Jakarta, keywords: ['javax[.]persistence', 'Entity'], cases: [], "
        "phases: [restructure]}\n"
        "- {file: sec.md, title: Security, keywords: ['WebSecurityConfigurerAdapter'], cases: [], phases: []}\n"
        "- {file: boot.md, title: Boot only, keywords: ['Entity'], cases: [spring_boot], phases: []}\n"
        "- {file: jdk.md, title: JDK only, keywords: ['Entity'], cases: [], phases: [jdk21]}\n")
    return d


# ---------------------------------------------------------------- helpers
def test_merge_windows():
    assert merge_windows([100, 150], 1000, 60) == [(40, 210)]
    assert merge_windows([100, 300], 1000, 60) == [(40, 160), (240, 360)]
    assert merge_windows([5], 20, 60) == [(1, 20)]
    assert merge_windows([100, 221], 1000, 60) == [(40, 281)]          # adjacent → merged
    assert merge_windows([], 50, 60) == [(1, 50)]


def test_pom_excerpt_large():
    deps = "".join(f"    <dependency><artifactId>a{i}</artifactId></dependency>\n" for i in range(300))
    text = ("<project>\n  <parent>\n    <artifactId>p</artifactId>\n  </parent>\n  <properties>\n    <x>1</x>\n  </properties>\n"
            "  <dependencyManagement>\n    <dependencies>\n      <dependency/>\n    </dependencies>\n  </dependencyManagement>\n"
            f"  <dependencies>\n{deps}  </dependencies>\n  <build>\n    <plugins/>\n  </build>\n  <name>x</name>\n</project>\n")
    out = pom_excerpt(text, "pom.xml")
    assert "<parent>" in out and "<properties>" in out and "<dependencyManagement>" in out and "<build>" in out
    assert out.count("    <dependencies>\n      <dependency/>") == 1          # depMgmt one, not duplicated
    assert "  <dependencies>\n    <dependency><artifactId>a0<" in out        # top-level one
    assert "<name>x</name>" not in out
    assert "// pom.xml lines 2-4 <parent>" in out


def test_nearest_pom_never_spins_on_absolute_or_windows_paths(tmp_path):
    """Round 9c: an absolute error path (Windows Maven output that could not be made root-relative) made nearest_pom
    loop forever on `/`; now it is relativized when under the workspace and dropped otherwise, and backslashes work."""
    import time

    from sbmig.core.workspace import Workspace
    root = tmp_path / "project"
    (root / "core" / "src" / "main" / "java" / "a").mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    (root / "core" / "pom.xml").write_text("<project/>", encoding="utf-8")
    ws = Workspace(root, tmp_path / "run", "t")
    t0 = time.monotonic()
    assert nearest_pom(ws, "core/src/main/java/a/A.java") == "core/pom.xml"
    assert nearest_pom(ws, "core\\src\\main\\java\\a\\A.java") == "core/pom.xml"          # backslashes
    assert nearest_pom(ws, str(root / "core" / "src" / "main" / "java" / "a" / "A.java")) == "core/pom.xml"  # absolute under the root
    assert nearest_pom(ws, "/somewhere/else/A.java") == "pom.xml"                        # absolute elsewhere → root pom
    assert nearest_pom(ws, "/") == "pom.xml" and nearest_pom(ws, None) == "pom.xml"
    assert nearest_pom(ws, "C:/other/A.java") in ("pom.xml", None)
    assert time.monotonic() - t0 < 2.0


def test_summarize_attempt_of_a_non_script_attempt():
    """Old (non-script) attempts in the history still render as one block: outcome, analysis, the files changed."""
    att = FixAttempt(id="a1", phase=Phase.VERIFY, iteration=1, cluster_id="c1", outcome="no_change",
                     decision=TriageDecision(cluster_id="c1", kind=DecisionKind.ESCALATE), errors_before=3,
                     errors_after=3, analysis="tried import swap", apply_errors=["x: not applied"],
                     changes=[FileChange(path="A.java", op="modify", search="import a;", replace="import b;"),
                              FileChange(path="B.java", op="rename", new_path="C.java"),
                              FileChange(path="D.java", op="delete")])
    text = summarize_script_attempt(att)                         # no script_mode → the generic rendering
    assert text == summarize_attempt(att)
    assert "tried import swap" in text and "MODIFY A.java: `import a;` → `import b;`" in text and "errors 3→3" in text
    assert "RENAME B.java -> C.java" in text and "DELETE D.java" in text and "apply error" in text
    many = att.model_copy(update={"changes": [FileChange(path=f"F{i}.java", op="delete") for i in range(15)]})
    assert "3 more change(s)" in summarize_attempt(many)


# ---------------------------------------------------------------- pack_script: notes
def test_notes_selection_by_keywords_case_phase(cfg, ws, put, tmp_path):
    put(A, SMALL)
    packer = ContextPacker(cfg, notes_dir(tmp_path))
    cl = case(message="package javax.persistence does not exist", symbol=None)
    # the always-note (old fix policy) never reaches a script prompt; boot-only filtered by case; sec no match;
    # v2.1v: the error text names javax.persistence, so only notes with an error-text hit are taken — the boot-only
    # and jdk-only notes match the file's `Entity` import alone
    assert packer.pack_script(ws, cl, ctx(), "case=spring_legacy").notes_included == ["Jakarta"]
    assert packer.pack_script(ws, cl, ctx(), "case=spring_boot").notes_included == ["Jakarta"]
    # another phase is still narrowed by the list; with no error-text hit left, the import hit selects
    assert packer.pack_script(ws, cl, ctx(phase=Phase.JDK21), "case=spring_legacy").notes_included == ["JDK only"]
    # the imports of the erroring file are matched too (no note hits the error text here): ranked by import hits
    plain = case(message="cannot find symbol", symbol="class X")
    assert packer.pack_script(ws, plain, ctx(), "case=spring_legacy").notes_included == ["Jakarta", "JDK only"]
    assert packer.pack_script(ws, plain, ctx(), "case=spring_boot").notes_included == ["Jakarta", "Boot only",
                                                                                      "JDK only"]
    one = ContextPacker(cfg, notes_dir(tmp_path), max_notes=1).pack_script(ws, cl, ctx(), "case=spring_boot")
    assert one.notes_included == ["Jakarta"]


def test_note_scores_error_text_three_times_the_imports(cfg, tmp_path):
    """v2.1v (B-H3): a keyword in the error text counts 3, one only in the imports / pom text 1; a note with no
    error-text hit is taken only when no note has one; `score(text)` (one string) still counts keywords."""
    from sbmig.fix.context import NoteEntry
    n = NoteEntry(file="x.md", title="X", keywords=["Filter", "javax[.]servlet", "Absent"])
    assert n.score("javax.servlet.Filter") == 2 and n.score("nothing") == 0
    assert n.score_parts("cannot find symbol Filter", "import javax.servlet.Filter;") == (1, 1)
    assert n.score_parts("javax.servlet.Filter", "import javax.servlet.Filter;") == (2, 0)
    assert n.score_parts("", "import javax.servlet.Filter;") == (0, 2)
    d = tmp_path / "scored"
    d.mkdir()
    for name in ("imp", "err", "pom"):
        (d / f"{name}.md").write_text(f"---\ntitle: {name}\n---\n{name.upper()} NOTE\n")
    (d / "index.yml").write_text(
        "notes:\n"
        "- {file: imp.md, title: imp, keywords: ['org[.]a', 'org[.]b', 'org[.]c', 'org[.]d']}\n"
        "- {file: err.md, title: err, keywords: ['Frobnicator']}\n"
        "- {file: pom.md, title: pom, keywords: ['<artifactId>widget</artifactId>']}\n")
    packer = ContextPacker(cfg, d)
    imports = "import org.a.X;\nimport org.b.X;\nimport org.c.X;\nimport org.d.X;\n"
    assert [x.title for x in packer.select_notes("cannot find symbol Frobnicator", None, "verify",
                                                 context=imports)] == ["err"]
    assert [x.title for x in packer.select_notes("cannot find symbol", None, "verify",
                                                 context=imports + "<artifactId>widget</artifactId>")] == ["imp", "pom"]


def test_jdk21_tagged_repo_note_reaches_a_verify_phase_script_prompt(cfg, ws, put):
    """PLAN-v2: every fix happens in the verify loop — the repo's jdk21-only note (`phases: [jdk21]`) is selected
    there by its keywords (it never was while phases filtered)."""
    put(A, "package a;\n\nimport sun.misc.BASE64Encoder;\n\npublic class A {\n}\n")
    packer = ContextPacker(cfg)
    jdk = next(n for n in packer.notes if n.file == "jdk21.md")
    assert jdk.phases == ["jdk21"]
    assert jdk in packer.select_notes("package sun.misc does not exist", "pure_java", "verify")
    cl = case(message="package sun.misc does not exist", symbol=None)
    p = packer.pack_script(ws, cl, ctx(phase=Phase.VERIFY), "case=pure_java")
    assert jdk.title in p.notes_included
    assert any(t == f"Migration note: {jdk.title}" for t, _ in p.sections)


def test_broken_notes_index_does_not_break(cfg, ws, put, tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "index.yml").write_text("notes: [::::")
    put(A, SMALL)
    assert ContextPacker(cfg, d).pack_script(ws, case(), ctx(), "").notes_included == []


def test_default_notes_dir_is_repo_knowledge(cfg, ws, put):
    put(A, SMALL)
    cl = case(message="cannot find symbol", symbol="class HystrixCommand")
    p = ContextPacker(cfg).pack_script(ws, cl, ctx(), "case=spring_boot")
    assert any("Spring Cloud Netflix" in n for n in p.notes_included)
    assert not any("Fix policy" in n for n in p.notes_included)          # always-notes stay out of script prompts


# ---------------------------------------------------------------- pack_script: truncation order
def test_script_truncation_order():
    order = SCRIPT_TRUNCATE_ORDER
    # v2.1v (B-H3): the other cases and the plan notes go before the notes
    assert order[:3] == ("other_errors", "plan_notes", "notes") and EDITS_TRUNCATE_ORDER[:3] == order[:3]
    assert EDITS_TRUNCATE_ORDER[-1] == "previous_attempts" and "layout_facts" not in order + EDITS_TRUNCATE_ORDER
    assert order.index("kb_hints") < order.index("reference_files") < order.index("advisory_hints") \
        < order.index("files")
    assert order.index("reference_notes") < order.index("files") and "errors" not in order and "header" not in order


def test_kb_hints_shrink_before_advisory_hints(cfg, ws, put, tmp_path):
    put(A, SMALL)
    c = ctx(advisory_hints=["a" * 1500], kb_hints=["k" * 1500])
    packer = ContextPacker(cfg, notes_dir(tmp_path))
    full = packer.pack_script(ws, case(), c, "case=x", budget=10**6)
    assert full.truncated == []
    notes = sum(len(t) + len(b) + 6 for t, b in full.sections if t.startswith("Migration note"))
    p = packer.pack_script(ws, case(), c, "case=x", budget=full.chars - notes - 1000)   # notes first, then kb hints
    kinds = [t.split(" ")[0] for t in p.truncated]
    assert "kb_hints" in kinds and "advisory_hints" not in kinds
    assert "a" * 1500 in dict(p.sections)[ContextPacker.ADVISORY_TITLE]
    assert any(t.startswith("Errors of this case") for t, _ in p.sections)        # errors are never truncated


# ---------------------------------------------------------------- pack_script: related project types
def test_related_type_files_from_location(cfg, ws, put, tmp_path):
    """2026-10-22: the repository interface named in `location:` is shown so the script can edit it (Spring Data 3
    CRUD)."""
    put("src/main/java/com/x/service/MemberService.java",
        "package com.x.service;\n\nimport com.x.repo.MemberRepository;\n\npublic class MemberService {\n"
        "    MemberRepository repository;\n    void f() { repository.findAll(); }\n}\n")
    put("src/main/java/com/x/repo/MemberRepository.java",
        "package com.x.repo;\n\nimport com.x.repo.BaseRepository;\n\n"
        "public interface MemberRepository extends BaseRepository<Member, Long> {\n}\n")
    put("src/main/java/com/x/repo/BaseRepository.java",
        "package com.x.repo;\n\npublic interface BaseRepository<T, ID> {\n}\n")
    err = BuildError(kind=ErrorKind.COMPILE, message="no suitable method found for findAll(no arguments)",
                     file="src/main/java/com/x/service/MemberService.java", line=7, signature="s1",
                     location="variable repository of type com.x.repo.MemberRepository",
                     raw="no suitable method found for findAll(no arguments)\n    method org.springframework.data."
                         "repository.PagingAndSortingRepository.findAll(Sort) is not applicable\n"
                         "    method com.x.repo.BaseRepository.findAll(int) is not applicable")
    cl = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], signatures=["s1"], case_id="k1")
    p = ContextPacker(cfg, notes_dir(tmp_path)).pack_script(ws, cl, ctx(), "case=spring_boot")
    rel = dict(p.sections)[RELATED]
    assert "src/main/java/com/x/repo/MemberRepository.java" in rel and "interface MemberRepository" in rel
    assert "src/main/java/com/x/repo/BaseRepository.java" in rel          # the candidate owner, a project type
    assert "PagingAndSortingRepository" not in p.files_included            # library types are never files
    assert "src/main/java/com/x/repo/MemberRepository.java" in p.files_included
    assert rel.count("MemberService.java") == 0                            # the erroring file is never repeated


def test_related_type_files_simple_name_via_import_and_cap(cfg, ws, put, tmp_path):
    put("src/main/java/com/x/A.java", "package com.x;\n\nimport com.x.r.R1;\n\npublic class A { R1 r; }\n")
    for n in ("R1", "R2", "R3", "R4"):
        put(f"src/main/java/com/x/r/{n}.java", f"package com.x.r;\n\npublic interface {n} {{}}\n")
    put("src/main/java/com/x/Same.java", "package com.x;\n\npublic class Same {}\n")
    f = "src/main/java/com/x/A.java"
    errs = [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=f, line=5, signature="s1",
                       symbol="method save(Foo)", location="variable r of type R1"),
            BuildError(kind=ErrorKind.COMPILE, message="x", file=f, line=6, signature="s2", location="class Same"),
            BuildError(kind=ErrorKind.COMPILE, message="x", file=f, line=7, signature="s3",
                       location="variable q of type com.x.r.R2"),
            BuildError(kind=ErrorKind.COMPILE, message="x", file=f, line=8, signature="s4",
                       location="variable q of type com.x.r.R3"),
            BuildError(kind=ErrorKind.COMPILE, message="x", file=f, line=9, signature="s5",
                       location="variable q of type com.x.r.R4")]
    cl = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=errs, signatures=[e.signature for e in errs],
                      case_id="k1")
    packer = ContextPacker(cfg, notes_dir(tmp_path))
    rel = dict(packer.pack_script(ws, cl, ctx(), "").sections)[RELATED]
    assert "r/R1.java" in rel and "Same.java" in rel and "r/R2.java" in rel       # import, same package, FQCN
    assert "r/R3.java" not in rel and "r/R4.java" not in rel                     # capped at MAX_RELATED_FILES
    put(A, SMALL)
    assert RELATED not in dict(packer.pack_script(ws, case(), ctx(), "").sections)
