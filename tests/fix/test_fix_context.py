from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, FileChange, FixAttempt, Phase, TriageDecision, DecisionKind
from sbmig.fix import ContextPacker
from sbmig.fix.context import merge_windows, nearest_pom, pom_excerpt, summarize_attempt

from tests.fix.conftest import make_cluster

SMALL = "package a;\n\nimport javax.persistence.Entity;\n\n@Entity\npublic class A {\n}\n"


def ctx(**kw):
    return FixContext(phase=kw.pop("phase", Phase.SPRING), iteration=1, **kw)


def big_java(n=1000):
    return "package a;\n\nimport javax.persistence.Entity;\n\npublic class A {\n" + "".join(f"    int f{i};\n" for i in range(n)) + "}\n"


def notes_dir(tmp_path):
    d = tmp_path / "notes"
    d.mkdir(exist_ok=True)
    (d / "fix-policy.md").write_text("---\ntitle: Fix policy\nkeywords: []\n---\nPOLICY " + "p" * 1000 + "\n")
    (d / "jak.md").write_text("---\ntitle: Jakarta\n---\nJAKARTA NOTE " + "j" * 3000 + "\n")
    (d / "sec.md").write_text("---\ntitle: Security\n---\nSECURITY NOTE\n")
    (d / "boot.md").write_text("---\ntitle: Boot only\n---\nBOOT NOTE\n")
    (d / "index.yml").write_text(
        "notes:\n"
        "- {file: fix-policy.md, title: Fix policy, keywords: [], cases: [], phases: [], always: true}\n"
        "- {file: jak.md, title: Jakarta, keywords: ['javax[.]persistence', 'Entity'], cases: [], phases: [spring]}\n"
        "- {file: sec.md, title: Security, keywords: ['WebSecurityConfigurerAdapter'], cases: [], phases: []}\n"
        "- {file: boot.md, title: Boot only, keywords: ['Entity'], cases: [spring_boot], phases: []}\n")
    return d


def test_merge_windows():
    assert merge_windows([100, 150], 1000, 60) == [(40, 210)]
    assert merge_windows([100, 300], 1000, 60) == [(40, 160), (240, 360)]
    assert merge_windows([5], 20, 60) == [(1, 20)]
    assert merge_windows([100, 221], 1000, 60) == [(40, 281)]          # adjacent → merged
    assert merge_windows([], 50, 60) == [(1, 50)]


def test_small_file_whole_and_errors_first(cfg, ws, put, tmp_path):
    put("src/main/java/a/A.java", SMALL)
    cl = make_cluster(lines=(3, 5), message="package javax.persistence does not exist", symbol=None)
    p = ContextPacker(cfg, notes_dir(tmp_path)).pack(ws, cl, ctx(), "case=spring_legacy java=1.8")
    titles = [t for t, _ in p.sections]
    assert titles[0] == "Situation" and titles[1].startswith("Errors to fix (2)")
    errors = p.sections[1][1]
    assert "src/main/java/a/A.java:3:5: package javax.persistence does not exist" in errors
    assert "location: class a.A" in errors
    src = dict(p.sections)["Source (exact text; copy SEARCH blocks from here)"]
    assert "// src/main/java/a/A.java lines 1-7 (whole file)" in src
    assert "```java\npackage a;\n" in src and "@Entity\npublic class A {\n}\n```" in src
    assert "src/main/java/a/A.java" in p.files_included
    assert p.chars > 0 and p.truncated == []


def test_large_file_windows_and_import_block(cfg, ws, put, tmp_path):
    put("src/main/java/a/A.java", big_java())
    cl = make_cluster(lines=(200, 230, 800))
    p = ContextPacker(cfg, notes_dir(tmp_path)).pack(ws, cl, ctx(), "")
    src = dict(p.sections)["Source (exact text; copy SEARCH blocks from here)"]
    assert "// src/main/java/a/A.java lines 1-3 (of 1006)" in src            # import block
    assert "// src/main/java/a/A.java lines 140-290 (of 1006)" in src        # merged window
    assert "// src/main/java/a/A.java lines 740-860 (of 1006)" in src
    assert "    int f500;" not in src
    assert src.count("```java") == 3
    # no line numbers inside code
    assert "\n200:" not in src and "\n200 " not in src


def test_missing_file_reported(cfg, ws, tmp_path):
    p = ContextPacker(cfg, notes_dir(tmp_path)).pack(ws, make_cluster(file="src/Nope.java"), ctx(), "")
    assert "file not found" in dict(p.sections)["Source (exact text; copy SEARCH blocks from here)"]
    assert p.files_included == []


def test_pom_inclusion_rules(cfg, ws, put, tmp_path):
    put("pom.xml", "<project>\n<dependencies>\n</dependencies>\n</project>\n")
    put("mod/pom.xml", "<project><artifactId>mod</artifactId></project>\n")
    put("mod/src/main/java/a/A.java", SMALL)
    nd = notes_dir(tmp_path)
    packer = ContextPacker(cfg, nd)
    plain = make_cluster(file="mod/src/main/java/a/A.java", message="cannot find symbol")
    assert not any(t.startswith("POM") for t, _ in packer.pack(ws, plain, ctx(), "").sections)
    pkg = make_cluster(file="mod/src/main/java/a/A.java", message="package org.foo does not exist")
    secs = dict(packer.pack(ws, pkg, ctx(), "").sections)
    assert "POM (mod/pom.xml)" in secs and "<artifactId>mod</artifactId>" in secs["POM (mod/pom.xml)"]
    dep = ErrorCluster(id="d", kind=ErrorKind.DEPENDENCY_RESOLUTION, file="pom.xml",
                       errors=[BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not resolve x:y:1",
                                          file="pom.xml")])
    p = packer.pack(ws, dep, ctx(), "")
    secs = dict(p.sections)
    assert "POM (pom.xml)" in secs and "(whole file)" in secs["POM (pom.xml)"]
    assert "Source (exact text; copy SEARCH blocks from here)" not in secs
    assert nearest_pom(ws, "mod/src/main/java/a/A.java") == "mod/pom.xml"
    assert nearest_pom(ws, None) == "pom.xml"


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


def test_notes_selection_by_keywords_case_phase(cfg, ws, put, tmp_path):
    put("src/main/java/a/A.java", SMALL)
    packer = ContextPacker(cfg, notes_dir(tmp_path))
    cl = make_cluster(message="package javax.persistence does not exist", symbol=None)
    p = packer.pack(ws, cl, ctx(), "case=spring_legacy")
    assert p.notes_included == ["Fix policy", "Jakarta"]               # boot-only filtered by case; sec no match
    p = packer.pack(ws, cl, ctx(), "case=spring_boot")
    assert p.notes_included == ["Fix policy", "Jakarta", "Boot only"]
    p = packer.pack(ws, cl, ctx(phase=Phase.JDK21), "case=spring_legacy")
    assert p.notes_included == ["Fix policy"]                          # jakarta restricted to spring phase
    # imports of the cluster file are matched too
    cl2 = make_cluster(message="cannot find symbol", symbol="class X")
    p = packer.pack(ws, cl2, ctx(), "case=spring_legacy")
    assert "Jakarta" in p.notes_included
    p = ContextPacker(cfg, notes_dir(tmp_path), max_notes=1).pack(ws, cl, ctx(), "case=spring_boot")
    assert p.notes_included == ["Fix policy", "Jakarta"]


def test_history_hints_other_errors_and_plan(cfg, ws, put, tmp_path):
    put("src/main/java/a/A.java", SMALL)
    att = FixAttempt(id="a1", phase=Phase.SPRING, iteration=1, cluster_id="c1", outcome="no_change",
                     decision=TriageDecision(cluster_id="c1", kind=DecisionKind.LLM_FIX), errors_before=3, errors_after=3,
                     analysis="tried import swap", apply_errors=["x block 1: search text not found"],
                     changes=[FileChange(path="A.java", op="modify", search="import a;", replace="import b;"),
                              FileChange(path="B.java", op="rename", new_path="C.java"),
                              FileChange(path="D.java", op="delete")])
    c = ctx(kb_hints=["use jakarta.persistence"], previous_attempts=[att], other_errors_summary="c2: compile B.java (3)",
            plan_notes=["spring phase after OpenRewrite"])
    secs = dict(ContextPacker(cfg, notes_dir(tmp_path)).pack(ws, make_cluster(), c, "case=x").sections)
    prev = next(v for k, v in secs.items() if k.startswith("Previous attempts"))
    assert "do not repeat" in next(k for k in secs if k.startswith("Previous attempts"))
    assert "tried import swap" in prev and "MODIFY A.java: `import a;` → `import b;`" in prev and "errors 3→3" in prev
    assert "RENAME B.java -> C.java" in prev and "DELETE D.java" in prev and "apply error" in prev
    assert "use jakarta.persistence" in next(v for k, v in secs.items() if k.startswith("Knowledge-base"))
    assert "c2: compile B.java" in next(v for k, v in secs.items() if k.startswith("Other error"))
    assert "spring phase" in secs["Plan notes"]
    many = att.model_copy(update={"changes": [FileChange(path=f"F{i}.java", op="delete") for i in range(15)]})
    assert "3 more change(s)" in summarize_attempt(many)


def test_budget_truncation_order(cfg, ws, put, tmp_path):
    put("src/main/java/a/A.java", big_java(300))                       # ~4.5k chars, whole file
    nd = notes_dir(tmp_path)
    att = FixAttempt(id="a1", phase=Phase.SPRING, iteration=1, cluster_id="c1",
                     decision=TriageDecision(cluster_id="c1", kind=DecisionKind.LLM_FIX), analysis="x" * 2000)
    c = ctx(previous_attempts=[att], other_errors_summary="o" * 2000)
    cl = make_cluster(message="package javax.persistence does not exist", symbol=None)
    packer = ContextPacker(cfg, nd)
    full = packer.pack(ws, cl, c, "case=spring_boot", budget=10**6)
    assert full.truncated == []
    # small cut: only the lowest-ranked notes go
    p = packer.pack(ws, cl, c, "case=spring_boot", budget=full.chars - 20)
    assert p.truncated == ["notes:Migration note: Boot only (dropped)"]
    # bigger cut: all optional notes, fix policy shortened, then other errors
    p = packer.pack(ws, cl, c, "case=spring_boot", budget=full.chars - 4000 - 1500)
    kinds = [t.split(" ")[0] for t in p.truncated]
    assert kinds[:2] == ["notes:Migration", "notes:Migration"]
    assert any(t.startswith("other_errors") for t in p.truncated)
    assert not any(t.startswith("files") for t in p.truncated)
    # tiny budget: errors are never truncated, files shrink last
    p = packer.pack(ws, cl, c, "case=spring_boot", budget=500)
    titles = [t for t, _ in p.sections]
    assert any(t.startswith("Errors to fix") for t in titles)
    order = [t.split(" ")[0].split(":")[0] for t in p.truncated]
    first = {k: order.index(k) for k in ("notes", "other_errors", "previous_attempts", "files") if k in order}
    assert first["notes"] < first["other_errors"] < first["previous_attempts"] < first["files"]


def test_broken_notes_index_does_not_break(cfg, ws, put, tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "index.yml").write_text("notes: [::::")
    put("src/main/java/a/A.java", SMALL)
    p = ContextPacker(cfg, d).pack(ws, make_cluster(), ctx(), "")
    assert p.notes_included == []


def test_default_notes_dir_is_repo_knowledge(cfg, ws, put):
    put("src/main/java/a/A.java", SMALL)
    cl = make_cluster(message="cannot find symbol", symbol="class WebSecurityConfigurerAdapter")
    p = ContextPacker(cfg).pack(ws, cl, ctx(), "case=spring_boot")
    assert "Fix policy for the build-fix loop" in p.notes_included
    assert any("Security" in n for n in p.notes_included)
