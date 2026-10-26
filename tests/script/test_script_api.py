"""The helper API `p` (sbmig.script.api.FixHelper), exercised in-process: path guard, globbing, encodings / line
ends / BOM, file operations, the pom editor, logging."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbmig.script.api import FixHelper, PathRefused

SAMPLE_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<!-- company sample -->
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0</version>

  <properties>
    <!-- keep me -->
    <java.version>1.8</java.version>
    <lombok.version>1.16.20</lombok.version>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.projectlombok</groupId>
      <artifactId>lombok</artifactId>
      <version>${lombok.version}</version>
    </dependency>
    <!-- old javax api -->
    <dependency>
      <groupId>javax.servlet</groupId>
      <artifactId>javax.servlet-api</artifactId>
      <version>3.1.0</version>
      <scope>provided</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.1</version>
      </plugin>
    </plugins>
  </build>
</project>
"""


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "project"
    for rel, text in {
        "pom.xml": SAMPLE_POM,
        "A.java": "class A {}\n",
        "src/main/java/com/x/B.java": "class B {}\n",
        "src/main/java/com/x/target/T.java": "package com.x.target;\n",     # a package named target is source
        "src/main/resources/app.properties": "a=1\n",
        "module/pom.xml": "<project/>\n",
        "module/src/main/java/C.java": "class C {}\n",
        "target/classes/A.class": "x",                                      # build output beside a pom
        "module/target/gen/G.java": "class G {}\n",
        ".git/config": "[core]\n",
        "web/node_modules/lib/index.js": "x",
        ".hidden/H.java": "class H {}\n",
    }.items():
        f = r / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")
    return r


def helper(root: Path, **kw) -> FixHelper:
    return FixHelper(root, **kw)


# ---------------------------------------------------------------------- p.files
def test_files_default_lists_sources_only_sorted(root):
    files = helper(root).files()
    assert files == sorted(files)
    assert "A.java" in files and "src/main/java/com/x/target/T.java" in files
    assert not any(f.startswith((".git/", "target/", "module/target/")) or "node_modules" in f for f in files)


@pytest.mark.parametrize("glob, expected", [
    ("**/*.java", [".hidden/H.java", "A.java", "module/src/main/java/C.java", "src/main/java/com/x/B.java",
                   "src/main/java/com/x/target/T.java"]),
    ("*.java", ["A.java"]),
    ("*.xml", ["pom.xml"]),
    ("**/pom.xml", ["module/pom.xml", "pom.xml"]),
    ("src/**", ["src/main/java/com/x/B.java", "src/main/java/com/x/target/T.java",
                "src/main/resources/app.properties"]),
    ("src/", ["src/main/java/com/x/B.java", "src/main/java/com/x/target/T.java",
              "src/main/resources/app.properties"]),
    ("src/**/x/*.java", ["src/main/java/com/x/B.java"]),
    ("src/main/*/com/x/?.java", ["src/main/java/com/x/B.java"]),
    ("[AB].java", ["A.java"]),
    ("[!A].java", []),
    ("src\\main\\resources\\*.properties", ["src/main/resources/app.properties"]),
    ("./*.java", ["A.java"]),
])
def test_files_glob_semantics(root, glob, expected):
    assert helper(root).files(glob) == expected


def test_files_refuses_globs_leaving_the_project(root):
    p = helper(root)
    for bad in ("/etc/*", "../*", "C:/x/*", "a/../../*"):
        with pytest.raises(PathRefused):
            p.files(bad)


def test_files_listing_follows_creates_and_deletes(root):
    p = helper(root)
    assert "new/N.java" not in p.files()
    p.create("new/N.java", "class N {}\n")
    assert "new/N.java" in p.files("**/*.java")
    p.delete("new/N.java")
    assert "new/N.java" not in p.files("**/*.java")


# ---------------------------------------------------------------------- path guard
@pytest.mark.parametrize("bad", ["/etc/passwd", "C:/x", "C:\\Windows\\win.ini", "c:x", "\\\\server\\share\\x",
                                 "..", "../x", "a/../../x", "src/..\\..\\x", ".git/config", "sub/.git/HEAD",
                                 ".GIT/config", ".", "", "a\0b"])
def test_path_guard_refuses(root, bad):
    p = helper(root)
    with pytest.raises(PathRefused) as exc:
        p.read(bad)
    assert repr(bad) in str(exc.value)
    assert p._refusals and repr(bad) in p._refusals[-1]


def test_path_guard_accepts_backslashes_and_inner_dotdot(root):
    p = helper(root)
    assert p.read("src\\main\\java\\com\\x\\B.java") == "class B {}\n"
    assert p.read("src/main/../main/java/com/x/B.java") == "class B {}\n"
    assert p.exists("./A.java") and not p.exists("missing.txt")


def test_writes_under_build_output_are_refused_reads_are_not(root):
    p = helper(root)
    assert p.read("module/target/gen/G.java") == "class G {}\n"
    for bad in ("target/classes/A.class", "module/target/gen/G.java", "target/new.txt"):
        with pytest.raises(PathRefused, match="build output"):
            p.write(bad, "x")
    with pytest.raises(PathRefused):
        p.delete("target")
    p.write("src/main/java/com/x/target/T.java", "package com.x.target;\nclass T {}\n")     # source package
    assert "src/main/java/com/x/target/T.java" in p._changed


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_symlink_pointing_outside_is_refused(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (root / "link.txt").symlink_to(outside)
    (root / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    (root / "gitlink").symlink_to(root / ".git", target_is_directory=True)
    p = helper(root)
    for bad in ("link.txt", "linkdir/outside.txt", "gitlink/config"):
        with pytest.raises(PathRefused):
            p.read(bad)
    with pytest.raises(PathRefused):
        p.write("link.txt", "x")
    assert outside.read_text() == "secret\n"
    assert "link.txt" not in p.files() and not any(f.startswith("linkdir/") for f in p.files())


# ---------------------------------------------------------------------- encodings, line ends, BOM
def test_cp949_round_trip_keeps_cp949(root):
    f = root / "src/main/java/com/x/K.java"
    f.write_bytes("// 한글 주석\nclass K {}\n".encode("cp949"))
    p = helper(root, encoding_candidates=["utf-8", "cp949", "latin-1"])
    text = p.read("src/main/java/com/x/K.java")
    assert "한글 주석" in text
    assert p.write("src/main/java/com/x/K.java", text.replace("class K", "// 추가\nclass K"))
    raw = f.read_bytes()
    assert raw.decode("cp949") == "// 한글 주석\n// 추가\nclass K {}\n"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


def test_cp949_unrepresentable_characters(root):
    (root / "K.java").write_bytes("// 한글\nclass K {}\n".encode("cp949"))
    (root / "k.txt").write_bytes("한글\n".encode("cp949"))
    p = helper(root, encoding_candidates=["utf-8", "cp949"])
    p.write("K.java", p.read("K.java") + "// \U0001F600\n")
    assert (root / "K.java").read_bytes().decode("cp949").endswith("// \\ud83d\\ude00\n")
    p.write("k.txt", p.read("k.txt") + "\U0001F600\n")
    assert (root / "k.txt").read_bytes().decode("utf-8") == "한글\n\U0001F600\n"
    assert any("written as UTF-8" in line for line in p._log)


def test_crlf_is_kept_and_read_returns_lf(root):
    f = root / "W.java"
    f.write_bytes(b"import javax.x;\r\nclass W {\r\n}\r\n")
    p = helper(root)
    text = p.read("W.java")
    assert text == "import javax.x;\nclass W {\n}\n"
    assert p.replace("W.java", "import javax.x;\n", "import jakarta.x;\n") == 1
    assert f.read_bytes() == b"import jakarta.x;\r\nclass W {\r\n}\r\n"
    p.write("W.java", "a\r\nb\n")                      # mixed input is normalised to the file's style
    assert f.read_bytes() == b"a\r\nb\r\n"


def test_utf8_bom_is_kept(root):
    f = root / "B.properties"
    f.write_bytes(b"\xef\xbb\xbfkey=\xea\xb0\x92\n")
    p = helper(root)
    assert p.read("B.properties") == "key=값\n"
    p.write("B.properties", "key=값2\n")
    assert f.read_bytes() == b"\xef\xbb\xbf" + "key=값2\n".encode()


def test_new_file_is_utf8_lf(root):
    p = helper(root, encoding_candidates=["utf-8", "cp949"])
    assert p.create("src/main/java/com/x/N.java", "// 새 파일\r\nclass N {}\r\n")
    assert (root / "src/main/java/com/x/N.java").read_bytes() == "// 새 파일\nclass N {}\n".encode()
    assert p.write("brand/new.txt", "x\n") and (root / "brand/new.txt").read_bytes() == b"x\n"


def test_unchanged_write_is_not_a_change(root):
    p = helper(root)
    assert p.write("A.java", "class A {}\n") is False
    assert p._changed == {}
    assert p.replace("A.java", "nothing-like-this", "x") == 0
    assert p.sub("A.java", r"zzz", "y") == 0
    assert p._changed == {}


# ---------------------------------------------------------------------- replace / sub / create / delete / move
def test_replace_and_sub_counts(root):
    (root / "R.java").write_text("a a a\nb\n")
    p = helper(root)
    assert p.replace("R.java", "a", "x", count=2) == 2
    assert p.read("R.java") == "x x a\nb\n"
    assert p.sub("R.java", r"^b$", "c", flags=__import__("re").M) == 1
    assert p.sub("R.java", r"[xa]", "y") == 3
    assert p.read("R.java") == "y y y\nc\n"
    with pytest.raises(ValueError):
        p.replace("R.java", "", "x")
    with pytest.raises(FileNotFoundError, match="missing.java"):
        p.replace("missing.java", "a", "b")


def test_create_refuses_an_existing_file(root):
    p = helper(root)
    with pytest.raises(FileExistsError):
        p.create("A.java", "x")
    assert (root / "A.java").read_text() == "class A {}\n"


def test_delete_file_folder_and_missing(root):
    p = helper(root)
    assert p.delete("A.java") and not (root / "A.java").exists()
    assert p.delete("module/src") and not (root / "module/src").exists()
    assert p.delete("nothing-here") is False
    assert list(p._changed) == ["A.java", "module/src/main/java/C.java"]


def test_move_file_and_folder(root):
    p = helper(root)
    assert p.move("A.java", "src/main/java/A.java")
    assert (root / "src/main/java/A.java").is_file() and not (root / "A.java").exists()
    p.move("src/main/java/com/x", "src/main/java/com/y")
    assert (root / "src/main/java/com/y/B.java").is_file()
    assert {"A.java", "src/main/java/A.java", "src/main/java/com/x/B.java", "src/main/java/com/y/B.java",
            "src/main/java/com/y/target/T.java"} <= set(p._changed)
    with pytest.raises(FileExistsError):
        p.move("pom.xml", "module/pom.xml")
    with pytest.raises(FileNotFoundError):
        p.move("nope.txt", "x.txt")
    with pytest.raises(ValueError):
        p.move("src", "src/inner")
    with pytest.raises(PathRefused):
        p.move("pom.xml", "../pom.xml")


def test_errors_are_copies(root):
    errs = [{"file": "A.java", "line": 3, "message": "cannot find symbol"}]
    p = helper(root, errors=errs)
    p.errors[0]["line"] = 9
    assert errs[0]["line"] == 3 and p.errors[0]["file"] == "A.java"


# ---------------------------------------------------------------------- log / print
def test_log_print_and_cap(root):
    p = helper(root, log_lines=3)
    p.log("one\ntwo")
    p._print("three", 4, sep="-")
    p.log("dropped")
    p.log("x" * 5000)
    assert p._log == ["one", "two", "three-4"] and p._dropped == 2
    q = helper(root)
    q.log("y" * 5000)
    assert len(q._log[0]) < 1100 and q._log[0].endswith(" …")


def test_journal_is_flushed_line_by_line(root, tmp_path):
    j = tmp_path / "journal.jsonl"
    p = helper(root, journal_path=j)
    p.log("hello")
    p.write("A.java", "class A { }\n")
    assert j.read_text(encoding="utf-8").splitlines() == ['{"log": "hello"}', '{"changed": "A.java"}']
    p._close()


# ---------------------------------------------------------------------- pom editing
def test_pom_editing_keeps_formatting(root):
    p = helper(root)
    pom = p.pom()
    assert pom.add_dependency("jakarta.servlet", "jakarta.servlet-api", "6.1.0", scope="provided")
    assert pom.remove_dependency("javax.servlet", "javax.servlet-api")
    assert pom.set_dependency_version("org.projectlombok", "lombok", "1.18.36")     # through ${lombok.version}
    assert pom.add_exclusion("org.projectlombok", "lombok", "org.slf4j", "slf4j-api")
    assert not pom.add_exclusion("org.projectlombok", "lombok", "org.slf4j", "slf4j-api")
    assert not pom.add_exclusion("not", "declared", "a", "b")
    assert pom.set_property("java.version", "21")
    assert pom.set_property("new.prop", "x") and pom.remove_property("new.prop")
    assert pom.set_plugin_version("org.apache.maven.plugins", "maven-compiler-plugin", "3.13.0")
    assert not pom.set_property("java.version", "21")                              # second call: no change
    assert (root / "pom.xml").read_text() == SAMPLE_POM                           # nothing written before save
    assert pom.save() and not pom.save()
    text = (root / "pom.xml").read_text()
    assert text == pom.text()
    expected = (SAMPLE_POM
                .replace("<java.version>1.8</java.version>", "<java.version>21</java.version>")
                .replace("<lombok.version>1.16.20</lombok.version>", "<lombok.version>1.18.36</lombok.version>")
                .replace("""      <version>${lombok.version}</version>
    </dependency>
    <!-- old javax api -->
    <dependency>
      <groupId>javax.servlet</groupId>
      <artifactId>javax.servlet-api</artifactId>
      <version>3.1.0</version>
      <scope>provided</scope>
    </dependency>
""", """      <version>${lombok.version}</version>
      <exclusions>
        <exclusion>
          <groupId>org.slf4j</groupId>
          <artifactId>slf4j-api</artifactId>
        </exclusion>
      </exclusions>
    </dependency>
    <!-- old javax api -->
    <dependency>
      <groupId>jakarta.servlet</groupId>
      <artifactId>jakarta.servlet-api</artifactId>
      <version>6.1.0</version>
      <scope>provided</scope>
    </dependency>
""")
                .replace("<version>3.1</version>", "<version>3.13.0</version>"))
    assert text == expected
    assert list(p._changed) == ["pom.xml"]


def test_pom_add_existing_dependency_sets_fields_and_queries(root):
    p = helper(root)
    pom = p.pom("pom.xml")
    assert pom.has_dependency("org.projectlombok", "lombok")
    assert pom.add_dependency("org.projectlombok", "lombok", scope="provided")
    assert not pom.add_dependency("org.projectlombok", "lombok")
    assert pom.get_property("java.version") == "1.8"
    deps = pom.dependencies()
    assert deps[0]["artifact_id"] == "lombok" and deps[0]["scope"] == "provided"
    assert pom.add_managed_dependency("org.springframework.boot", "spring-boot-dependencies", "4.1.0",
                                      type="pom", scope="import")
    assert pom.dependencies(managed=True)[0]["type"] == "pom"
    assert pom.remove_managed_dependency("org.springframework.boot", "spring-boot-dependencies")
    assert "dependencyManagement" not in pom.text()


def test_pom_crlf_and_module_pom(root):
    crlf = SAMPLE_POM.replace("\n", "\r\n").encode("utf-8")
    (root / "module/pom.xml").write_bytes(crlf)
    p = helper(root)
    pom = p.pom("module\\pom.xml")
    assert "\r" not in pom.text()
    pom.set_property("java.version", "21")
    pom.save()
    raw = (root / "module/pom.xml").read_bytes()
    assert raw == crlf.replace(b"<java.version>1.8<", b"<java.version>21<")
    assert list(p._changed) == ["module/pom.xml"]


def test_pom_save_under_build_output_is_refused(root):
    (root / "target/pom.xml").write_text(SAMPLE_POM)
    p = helper(root)
    pom = p.pom("target/pom.xml")
    pom.set_property("java.version", "21")
    with pytest.raises(PathRefused):
        pom.save()
    with pytest.raises(FileNotFoundError):
        p.pom("nope/pom.xml")
