"""sbmig.kb.changes: text replace vs code edit of git / changes_as_diff-format diffs (error cases, 2026-10-23)."""
from __future__ import annotations

import pytest

from sbmig.core.models import FileChange
from sbmig.kb.changes import (
    classify_diff,
    combine_changes,
    diff_for_file,
    file_changes_from_diff,
    file_type,
    path_matches,
)
from sbmig.kb.common import changes_as_diff
from sbmig.kb.labels import label_changes

from .helpers import CODE_EDIT_DIFF, EC_FILE, RENAME_DIFF


def _git(path: str, *hunk_lines: str, header: str = "") -> str:
    minus = sum(1 for ln in hunk_lines if not ln.startswith("+"))
    plus = sum(1 for ln in hunk_lines if not ln.startswith("-"))
    body = "\n".join(hunk_lines)
    return (f"diff --git a/{path} b/{path}\n{header}index 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{minus} +1,{plus} @@\n{body}\n")


def _pairs(c) -> list[tuple[str, str, int]]:
    return [(p.old, p.new, p.count) for p in c.replace_pairs]


def test_method_rename_is_text_replace():
    c = classify_diff(RENAME_DIFF)
    assert c.change_kind == "text_replace"
    assert _pairs(c) == [("clientIp", "remoteClientIp", 1)]
    assert c.replace_pairs[0].files == [EC_FILE]
    assert c.files == [EC_FILE] and c.file_types == ["java"]
    assert (c.lines_added, c.lines_removed) == (1, 1)


def test_import_swap_javax_to_jakarta_aggregates_pairs():
    d = _git("src/main/java/A.java",
             " package a;",
             "-import javax.servlet.Filter;",
             "-import javax.servlet.http.HttpServletRequest;",
             "+import jakarta.servlet.Filter;",
             "+import jakarta.servlet.http.HttpServletRequest;",
             " class A {}")
    d += _git("src/main/java/B.java", "-import javax.persistence.Entity;", "+import jakarta.persistence.Entity;")
    c = classify_diff(d)
    assert c.change_kind == "text_replace"
    # round 10: the full package parts, never the bare `javax` → `jakarta` (it rewrote javax.sql / javax.xml.parsers)
    assert _pairs(c) == [("javax.servlet", "jakarta.servlet", 1), ("javax.servlet.http", "jakarta.servlet.http", 1),
                         ("javax.persistence", "jakarta.persistence", 1)]
    assert c.replace_pairs[0].files == ["src/main/java/A.java"]
    assert c.replace_pairs[2].files == ["src/main/java/B.java"]


def test_pom_version_bump_keeps_the_whole_version():
    d = _git("pom.xml", "   <artifactId>lib</artifactId>", "-  <version>1.0.0</version>",
             "+  <version>3.0.0</version>")
    c = classify_diff(d)
    assert c.change_kind == "text_replace" and c.file_types == ["pom"]
    assert _pairs(c) == [("1.0.0", "3.0.0", 1)]
    d2 = _git("pom.xml", "-<version>5.3.31.RELEASE</version>", "+<version>7.0.0-M1</version>")
    assert _pairs(classify_diff(d2)) == [("5.3.31.RELEASE", "7.0.0-M1", 1)]
    d3 = _git("pom.xml", "-<artifactId>javax.servlet-api</artifactId>", "+<artifactId>jakarta.servlet-api</artifactId>")
    assert _pairs(classify_diff(d3)) == [("javax", "jakarta", 1)]


def test_added_argument_is_code_edit():
    c = classify_diff(CODE_EDIT_DIFF)
    assert c.change_kind == "code_edit" and c.replace_pairs == []


def test_added_method_is_code_edit():
    d = _git("src/main/java/A.java", " class A {", "+    void b() {", "+        a();", "+    }", " }")
    c = classify_diff(d)
    assert c.change_kind == "code_edit" and (c.lines_added, c.lines_removed) == (3, 0)


def test_unequal_runs_are_code_edit():
    d = _git("src/main/java/A.java", "-import javax.a.X;", "+import jakarta.a.X;", "+import jakarta.a.Y;")
    assert classify_diff(d).change_kind == "code_edit"


def test_mixed_across_files_and_within_one_file():
    c = classify_diff(RENAME_DIFF + CODE_EDIT_DIFF.replace(EC_FILE, "src/main/java/Other.java"))
    assert c.change_kind == "mixed" and _pairs(c) == [("clientIp", "remoteClientIp", 1)]
    d = _git("src/main/java/A.java", "-int a = foo();", "+int a = bar();", " x", "+log();")
    assert classify_diff(d).change_kind == "mixed"


def test_git_new_deleted_and_renamed_files():
    new = ("diff --git a/src/N.java b/src/N.java\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
           "+++ b/src/N.java\n@@ -0,0 +1,2 @@\n+class N {\n+}\n")
    c = classify_diff(new)
    assert c.change_kind == "file_create" and c.files == ["src/N.java"] and c.lines_added == 2
    gone = ("diff --git a/src/D.java b/src/D.java\ndeleted file mode 100644\nindex 1111111..0000000\n"
            "--- a/src/D.java\n+++ /dev/null\n@@ -1 +0,0 @@\n-class D {}\n")
    c = classify_diff(gone)
    assert c.change_kind == "file_delete" and c.files == ["src/D.java"] and c.lines_removed == 1
    moved = "diff --git a/src/a/X.java b/src/b/X.java\nsimilarity index 100%\nrename from src/a/X.java\n" \
            "rename to src/b/X.java\n"
    c = classify_diff(moved)
    assert c.change_kind == "file_rename" and c.files == ["src/b/X.java"]
    edited = ("diff --git a/src/a/X.java b/src/b/X.java\nsimilarity index 90%\nrename from src/a/X.java\n"
              "rename to src/b/X.java\nindex 1..2 100644\n--- a/src/a/X.java\n+++ b/src/b/X.java\n"
              "@@ -1,1 +1,1 @@\n-package a;\n+package b;\n")
    c = classify_diff(edited)
    assert c.change_kind == "text_replace" and _pairs(c) == [("a", "b", 1)]      # a rename counts as its edits
    assert classify_diff(new + gone).change_kind == "mixed"


def test_changes_as_diff_format_blocks():
    changes = [
        FileChange(path="src/A.java", op="modify", search="x\nreq.clientIp();\ny", replace="x\nreq.remoteClientIp();\ny"),
        FileChange(path="src/C.java", op="create", content="class C {\n}\n"),
        FileChange(path="old.txt", op="delete"),
        FileChange(path="a.yml", op="rename", new_path="b.yml"),
        FileChange(path="same.properties", op="modify", search="k=v", replace="k=v"),
    ]
    c = classify_diff(changes_as_diff(changes))
    assert c.change_kind == "mixed"
    assert c.files == ["src/A.java", "src/C.java", "old.txt", "b.yml", "same.properties"]
    assert c.file_types == ["java", "other", "yaml", "properties"]
    assert _pairs(c) == [("clientIp", "remoteClientIp", 1)] and (c.lines_added, c.lines_removed) == (3, 1)
    assert classify_diff(changes_as_diff(changes[:1])).change_kind == "text_replace"
    assert classify_diff(changes_as_diff(changes[1:2])).change_kind == "file_create"
    assert classify_diff(changes_as_diff(changes[2:3])).change_kind == "file_delete"
    assert classify_diff(changes_as_diff(changes[3:4])).change_kind == "file_rename"
    assert classify_diff(changes_as_diff(changes[4:])).change_kind == "none"


def test_whitespace_only_and_blank_lines_are_ignored():
    d = _git("src/A.java", "-    foo(a,b);", "+  foo( a, b );", " x", "+", "+   ", "-")
    c = classify_diff(d)
    assert c.change_kind == "none" and c.replace_pairs == []
    rewrapped = _git("src/A.java", "-foo(a, b);", "+foo(a,", "+    b);")
    assert classify_diff(rewrapped).change_kind == "none"


def test_crlf_input_and_no_newline_marker():
    d = RENAME_DIFF.replace("\n", "\r\n")
    c = classify_diff(d)
    assert c.change_kind == "text_replace" and _pairs(c) == [("clientIp", "remoteClientIp", 1)]
    d2 = _git("pom.xml", "-<version>1.0</version>", "+<version>2.0</version>") + "\\ No newline at end of file\n"
    assert _pairs(classify_diff(d2)) == [("1.0", "2.0", 1)]


def test_removed_line_that_looks_like_a_header_is_content():
    d = ("--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n--- a/x\n+-- b/x\n keep\n")
    c = classify_diff(d)
    assert c.files == ["notes.txt"] and _pairs(c) == [("a", "b", 1)]


@pytest.mark.parametrize("garbage", ["", "garbage\n@@ nonsense\n+++", None, 123, "@@ -1 +1 @@\n-a\n+b\n",
                                     "diff --git\n", "=== RENAME\n"])
def test_garbage_never_raises(garbage):
    c = classify_diff(garbage)
    assert c.change_kind in ("none", "text_replace")
    assert diff_for_file(garbage, "x") == "" or isinstance(diff_for_file(garbage, "x"), str)
    assert isinstance(file_changes_from_diff(garbage), list)


def test_file_types():
    assert [file_type(p) for p in ("a/B.java", "pom.xml", "x/web.xml", "a.properties", "b.yaml", "c.yml",
                                   "WEB-INF/a.jsp", "t.tagx", "README.md")] == [
        "java", "pom", "xml", "properties", "yaml", "yaml", "jsp", "jsp", "other"]


def test_diff_for_file_matches_by_suffix():
    two = RENAME_DIFF + _git("pom.xml", "-<version>1.0.0</version>", "+<version>3.0.0</version>")
    part = diff_for_file(two, "demo-back/" + EC_FILE)          # the restructure moved the file
    assert part.startswith(f"diff --git a/{EC_FILE}") and "remoteClientIp" in part and "pom.xml" not in part
    assert "3.0.0" in diff_for_file(two, "pom.xml") and "clientIp" not in diff_for_file(two, "pom.xml")
    assert diff_for_file(two, "src/main/java/Nope.java") == "" and diff_for_file(two, None) == ""
    assert path_matches("a/src/X.java", "src/X.java") and not path_matches("src/AX.java", "X.java")


def test_file_changes_from_diff_feed_the_labels():
    changes = file_changes_from_diff(RENAME_DIFF)
    assert len(changes) == 1 and changes[0].op == "modify" and "clientIp()" in changes[0].search
    lab = label_changes(changes, [])
    assert lab.labels == ["method-rename"] and lab.replacement == "clientIp → remoteClientIp"
    new = changes_as_diff([FileChange(path="src/C.java", op="create", content="class C {}")])
    assert [(c.op, c.content) for c in file_changes_from_diff(new)] == [("create", "class C {}")]
    moved = "diff --git a/a.txt b/b.txt\nrename from a.txt\nrename to b.txt\n"
    assert [(c.op, c.path, c.new_path) for c in file_changes_from_diff(moved)] == [("rename", "a.txt", "b.txt")]


def test_combine_changes_reads_several_attempts_as_one():
    rename, edit = classify_diff(RENAME_DIFF), classify_diff(CODE_EDIT_DIFF)
    both = combine_changes([edit, rename, rename])
    assert both.change_kind == "mixed" and both.files == [EC_FILE] and both.file_types == ["java"]
    assert [(p.old, p.new, p.count) for p in both.replace_pairs] == [("clientIp", "remoteClientIp", 2)]
    assert both.lines_added == edit.lines_added + 2 * rename.lines_added
    assert combine_changes([rename, rename]).change_kind == "text_replace"
    assert combine_changes([rename, classify_diff("")]).change_kind == "text_replace"      # `none` is ignored
    assert combine_changes([]).change_kind == "none"


# --------------------------------------------------------------------------- round 10: qualified names (C6)
@pytest.mark.parametrize("old,new,want", [
    ("import javax.xml.bind.JAXBContext;", "import jakarta.xml.bind.JAXBContext;",
     [("javax.xml.bind", "jakarta.xml.bind")]),
    ("import javax.xml.bind.*;", "import jakarta.xml.bind.*;", [("javax.xml.bind", "jakarta.xml.bind")]),
    ("package com.acme.old;", "package com.acme.neu;", [("com.acme.old", "com.acme.neu")]),
    ("@javax.annotation.PostConstruct", "@jakarta.annotation.PostConstruct",
     [("javax.annotation", "jakarta.annotation")]),
    ("import org.springframework.boot.context.embedded.ServletRegistrationBean;",
     "import org.springframework.boot.web.servlet.ServletRegistrationBean;",
     [("org.springframework.boot.context.embedded", "org.springframework.boot.web.servlet")]),
    # a package of another length is still ONE replaced name, not a code edit
    ("import org.springframework.boot.autoconfigure.web.ErrorAttributes;",
     "import org.springframework.boot.web.servlet.error.ErrorAttributes;",
     [("org.springframework.boot.autoconfigure.web", "org.springframework.boot.web.servlet.error")]),
    # the class changes too: the full names
    ("import org.a.Old;", "import org.b.New;", [("org.a.Old", "org.b.New")]),
    ("import static org.junit.Assert.assertEquals;", "import static org.junit.jupiter.api.Assertions.assertEquals;",
     [("org.junit.Assert.assertEquals", "org.junit.jupiter.api.Assertions.assertEquals")]),
    # member / identifier renames stay single tokens
    ("return req.clientIp();", "return req.remoteClientIp();", [("clientIp", "remoteClientIp")]),
    ("return com.acme.Helper.clientIp(r);", "return com.acme.Helper.remoteClientIp(r);",
     [("clientIp", "remoteClientIp")]),
    ("return Helper.clientIp(r);", "return Helper.ip(r);", [("clientIp", "ip")]),
    ("this.helper.clientIp();", "this.helper.remoteClientIp();", [("clientIp", "remoteClientIp")]),
])
def test_qualified_names_become_package_pairs(old, new, want):
    c = classify_diff(_git("src/main/java/A.java", f"-{old}", f"+{new}"))
    assert c.change_kind == "text_replace" and [(p.old, p.new) for p in c.replace_pairs] == want


def test_a_package_with_two_targets_falls_back_to_the_full_names():
    d = _git("src/main/java/A.java", "-import org.apache.commons.lang.StringUtils;",
             "-import org.apache.commons.lang.StringEscapeUtils;", "+import org.apache.commons.lang3.StringUtils;",
             "+import org.apache.commons.text.StringEscapeUtils;")
    assert [(p.old, p.new) for p in classify_diff(d).replace_pairs] == [
        ("org.apache.commons.lang.StringUtils", "org.apache.commons.lang3.StringUtils"),
        ("org.apache.commons.lang.StringEscapeUtils", "org.apache.commons.text.StringEscapeUtils")]


def test_a_package_change_is_one_package_pair_not_javax_to_jakarta():
    """review-kb/repro1+2: `javax.xml.bind.JAXBContext` → jakarta used to be learned as `javax` → `jakarta` (a pair that
    also matched `javax.xml.parsers` / `javax.sql`); it is the package pair now."""
    c = classify_diff(_git("src/main/java/A.java", "-import javax.xml.bind.JAXBContext;",
                           "+import jakarta.xml.bind.JAXBContext;"))
    assert [(p.old, p.new) for p in c.replace_pairs] == [("javax.xml.bind", "jakarta.xml.bind")]


# --------------------------------------------------------------------------- round 10: excerpts (C7)
def _two_hunks(far: int = 200) -> str:
    return (f"diff --git a/{EC_FILE} b/{EC_FILE}\nindex 1..2 100644\n--- a/{EC_FILE}\n+++ b/{EC_FILE}\n"
            "@@ -3,3 +3,3 @@ class A {\n     int a;\n-    int old1;\n+    int new1;\n     int b;\n"
            f"@@ -{far},3 +{far},3 @@ class A {{\n     int c;\n-    return req.clientIp();\n+    return req.ip();\n"
            "     int d;\n")


def test_excerpt_is_the_hunk_at_the_error_line():
    from sbmig.kb.changes import excerpt_for

    d = _two_hunks()
    near = excerpt_for(d, EC_FILE, 201, 1500)
    assert "req.ip()" in near and "new1" in near                      # both fit, in file order
    assert near.index("new1") < near.index("req.ip()")
    small = excerpt_for(d, EC_FILE, 201, 200)                         # room for one hunk: the one at the line
    assert "req.ip()" in small and "new1" not in small and small.endswith("…[truncated]")
    assert len(small) <= 200 + len("\n…[truncated]")
    first = excerpt_for(d, EC_FILE, 4, 200)
    assert "new1" in first and "req.ip()" not in first
    assert excerpt_for(d, "src/Other.java", 3, 1500) == "" and excerpt_for(d, None, 3) == ""


def test_excerpt_cuts_a_huge_hunk_around_the_error_line():
    from sbmig.kb.changes import excerpt_for

    body = "".join(f"-    int v{i} = old();\n+    int v{i} = neu();\n" for i in range(300))
    d = (f"diff --git a/{EC_FILE} b/{EC_FILE}\n--- a/{EC_FILE}\n+++ b/{EC_FILE}\n@@ -1,300 +1,300 @@\n{body}")
    ex = excerpt_for(d, EC_FILE, 250, 600)
    assert len(ex) <= 600 + 40 and "v249 = old();" in ex and "v10 = old()" not in ex
    assert ex.rstrip().endswith("…[truncated]")
