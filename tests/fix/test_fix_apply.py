from sbmig.core.models import FileChange
from sbmig.fix import PatchApplier
from sbmig.fix.apply import apply_block, best_window

JAVA = """package a;

import javax.servlet.Filter;
import java.util.List;

public class A {
    public void x() {
        old();
    }

    public void y() {
        old();
    }
}
"""


def mod(path, s, r):
    return FileChange(path=path, op="modify", search=s, replace=r)


def test_exact_match(ws, put):
    put("A.java", JAVA)
    applied, errors = PatchApplier(ws).apply([mod("A.java", "import javax.servlet.Filter;", "import jakarta.servlet.Filter;")])
    assert errors == [] and len(applied) == 1
    assert "import jakarta.servlet.Filter;" in ws.read("A.java")
    assert ws.read("A.java").endswith("}\n")


def test_whitespace_fallback_with_reindent(ws, put):
    put("A.java", JAVA)
    search = "public void x() {\n    old();\n}"          # model lost the indentation
    replace = "public void x() {\n    neu();\n}"
    applied, errors = PatchApplier(ws).apply([mod("A.java", search, replace)])
    assert errors == []
    text = ws.read("A.java")
    assert "    public void x() {\n        neu();\n    }" in text
    assert text.count("old();") == 1


def test_ambiguous_and_not_found(ws, put):
    put("A.java", JAVA)
    errs = PatchApplier(ws).dry_run([mod("A.java", "        old();", "x")])
    assert errs and "matches 2 times (ambiguous)" in errs[0]
    errs = PatchApplier(ws).dry_run([mod("A.java", "nothing like this", "x")])
    assert errs == ["A.java block 1: search text not found"]
    errs = PatchApplier(ws).dry_run([mod("A.java", "  old();  ", "x")])   # ws-normalized also ambiguous
    assert "ambiguous" in errs[0]
    assert ws.read("A.java") == JAVA                                      # dry run never writes


def test_atomic_per_file(ws, put):
    put("A.java", JAVA)
    put("B.java", "class B {}\n")
    changes = [mod("A.java", "import java.util.List;", "import java.util.Map;"),
               mod("A.java", "does not exist", "zzz"),
               mod("B.java", "class B {}", "class B { int x; }")]
    applied, errors = PatchApplier(ws).apply(changes)
    assert ws.read("A.java") == JAVA                                       # nothing written for A
    assert ws.read("B.java") == "class B { int x; }\n"
    assert [c.path for c in applied] == ["B.java"]
    assert any("A.java block 2: search text not found" in e for e in errors)
    assert any("none of the 2 change(s) applied" in e for e in errors)


def test_sequential_blocks_and_line_delete(ws, put):
    put("A.java", JAVA)
    changes = [mod("A.java", "import java.util.List;", ""),
               mod("A.java", "    public void y() {\n        old();", "    public void y() {\n        other();")]
    applied, errors = PatchApplier(ws).apply(changes)
    assert errors == []
    text = ws.read("A.java")
    assert "java.util.List" not in text and "\n\n\npublic" not in text
    assert "other();" in text


def test_crlf_preserved(ws, put):
    put("W.java", JAVA.replace("\n", "\r\n"))
    applied, errors = PatchApplier(ws).apply([mod("W.java", "import java.util.List;\nimport x;", "y")])
    assert errors                                                       # not found
    applied, errors = PatchApplier(ws).apply([mod("W.java", "    public void x() {\n        old();",
                                                  "    public void x() {\n        neu();")])
    assert errors == []
    raw = ws.abs("W.java").read_bytes().decode()
    assert "neu();\r\n" in raw and "\n" not in raw.replace("\r\n", "")
    assert raw.endswith("}\r\n")


def test_truncation_guard(ws, put):
    body = "".join(f"    void m{i}() {{ call{i}(); }}\n" for i in range(30))
    put("A.java", "package a;\n\npublic class A {\n" + body + "}\n")
    big = "public class A {\n" + body + "}"
    errs = PatchApplier(ws).dry_run([mod("A.java", big.rstrip("\n"), "public class A {}")])
    assert errs and "shrink" in errs[0]
    errs = PatchApplier(ws, allow_overwrite=True).dry_run([FileChange(path="A.java", op="create", content="")])
    assert errs and "empty Java file" in errs[0]
    # a DELETE is fine
    applied, errors = PatchApplier(ws).apply([FileChange(path="A.java", op="delete")])
    assert errors == [] and not ws.exists("A.java")


def test_create_delete_rename(ws, put):
    put("src/Old.java", "class Old {}\n")
    put("src/R.java", "class R {}\n")
    changes = [FileChange(path="src/New.java", op="create", content="class New {}"),
               FileChange(path="src/Old.java", op="delete"),
               FileChange(path="src/R.java", op="rename", new_path="src/b/R.java"),
               FileChange(path="src/b/R.java", op="modify", search="class R {}", replace="class R { }")]
    applied, errors = PatchApplier(ws).apply(changes)
    assert errors == [], errors
    assert ws.read("src/New.java") == "class New {}\n"
    assert not ws.exists("src/Old.java") and not ws.exists("src/R.java")
    assert ws.read("src/b/R.java") == "class R { }\n"
    assert len(applied) == 4


def test_create_existing_refused_unless_identical_or_overwrite(ws, put):
    put("X.java", "class X {}\n")
    assert PatchApplier(ws).dry_run([FileChange(path="X.java", op="create", content="class X {}\n")]) == []
    errs = PatchApplier(ws).dry_run([FileChange(path="X.java", op="create", content="class X { int a; }\n")])
    assert errs and "already exists" in errs[0]
    applied, errors = PatchApplier(ws, allow_overwrite=True).apply(
        [FileChange(path="X.java", op="create", content="class X { int a; }\n")])
    assert errors == [] and ws.read("X.java") == "class X { int a; }\n"


def test_errors_for_missing_files_and_bad_paths(ws, put):
    put("T.java", "class T {}\n")
    errs = PatchApplier(ws).dry_run([
        FileChange(path="Nope.java", op="delete"),
        FileChange(path="Nope2.java", op="rename", new_path="Z.java"),
        FileChange(path="T.java", op="rename", new_path="T.java2"),
        FileChange(path="Nope3.java", op="modify", search="a", replace="b"),
        FileChange(path="../evil.java", op="delete"),
        FileChange(path="T.java", op="rename", new_path=None),
    ])
    joined = "\n".join(errs)
    assert "Nope.java: delete failed" in joined
    assert "Nope2.java: rename failed" in joined
    assert "Nope3.java block 1: file does not exist" in joined
    assert "'..'" in joined
    assert "rename target" in joined


def test_rename_onto_existing_and_empty_search(ws, put):
    put("A.java", "class A {}\n")
    put("B.java", "class B {}\n")
    assert "already exists" in PatchApplier(ws).dry_run([FileChange(path="A.java", op="rename", new_path="B.java")])[0]
    assert "empty" in PatchApplier(ws).dry_run([mod("A.java", "   \n", "x")])[0]
    # empty SEARCH on a missing file = create
    applied, errors = PatchApplier(ws).apply([mod("C.java", "", "class C {}")])
    assert errors == [] and ws.read("C.java") == "class C {}\n"


def test_delete_then_create_same_path(ws, put):
    put("D.java", "class D {}\n")
    applied, errors = PatchApplier(ws).apply([FileChange(path="D.java", op="delete"),
                                              FileChange(path="D.java", op="create", content="class D2 {}\n")])
    assert errors == [] and ws.read("D.java") == "class D2 {}\n"


def test_check_returns_structured_failures(ws, put):
    put("A.java", JAVA)
    f = PatchApplier(ws).check([mod("A.java", "public void z() {", "")])
    assert f[0].reason == "not_found" and f[0].block == 1 and f[0].path == "A.java"


def test_apply_block_and_best_window_helpers():
    assert apply_block("a\nb\n", "", "x") == (None, "empty", 0)
    new, how, n = apply_block("a\n  b\nc\n", "b", "B")
    assert how == "exact" and new == "a\n  B\nc\n"
    text = "\n".join(f"line {i}" for i in range(1, 50))
    a, b = best_window(text, "line 20\nline 21x", pad=2)
    assert a <= 20 <= b and b - a <= 6
