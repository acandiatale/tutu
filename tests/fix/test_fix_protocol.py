from sbmig.core.models import FileChange
from sbmig.fix import parse_fix_response, render_protocol_instructions
from sbmig.fix.protocol import normalize_path

CLEAN = """<analysis>javax.servlet moved to jakarta</analysis>
<confidence>0.9</confidence>
<patches>
=== MODIFY src/main/java/a/A.java
<<<<<<< SEARCH
import javax.servlet.Filter;
=======
import jakarta.servlet.Filter;
>>>>>>> REPLACE
<<<<<<< SEARCH
    public void x() {
        old();
    }
=======
    public void x() {
        neu();
    }
>>>>>>> REPLACE
=== CREATE src/main/java/a/New.java
package a;

public class New {}
=== DELETE src/main/java/a/Old.java
=== RENAME src/main/java/a/B.java -> src/main/java/b/B.java
</patches>
"""


def test_render_protocol_mentions_all_ops():
    text = render_protocol_instructions()
    for kw in ("=== MODIFY", "=== CREATE", "=== DELETE", "=== RENAME", "<<<<<<< SEARCH", "=======",
               ">>>>>>> REPLACE", "<need_files>", "<need_grep>", "<confidence>"):
        assert kw in text


def test_parse_clean():
    r = parse_fix_response(CLEAN)
    assert r.analysis == "javax.servlet moved to jakarta"
    assert r.confidence == 0.9
    assert [c.op for c in r.changes] == ["modify", "modify", "create", "delete", "rename"]
    m1, m2, cr, de, rn = r.changes
    assert m1.search == "import javax.servlet.Filter;" and m1.replace == "import jakarta.servlet.Filter;"
    assert m2.search == "    public void x() {\n        old();\n    }"          # exact whitespace kept
    assert m2.replace == "    public void x() {\n        neu();\n    }"
    assert cr.content == "package a;\n\npublic class New {}\n"
    assert de.path == "src/main/java/a/Old.java"
    assert rn.path == "src/main/java/a/B.java" and rn.new_path == "src/main/java/b/B.java"
    assert r.warnings == []
    assert not r.wants_context


def test_parse_sloppy_fenced_crlf_lowercase_backticks_trailing_spaces():
    text = ("```xml\r\n<analysis>fix</analysis>  \r\n<patches>\r\n=== modify `src/A.java`   \r\n"
            "<<<<<<< search  \r\nfoo();\r\n=======   \r\nbar();\r\n>>>>>>> replace\r\n</patches>\r\n```")
    r = parse_fix_response(text)
    assert len(r.changes) == 1
    c = r.changes[0]
    assert (c.path, c.op, c.search, c.replace) == ("src/A.java", "modify", "foo();", "bar();")


def test_missing_closing_tags_and_missing_replace_marker():
    text = ("<analysis>no closing analysis\n<patches>\n=== MODIFY src/A.java\n<<<<<<< SEARCH\na\n=======\nb\n"
            "=== MODIFY src/B.java\n<<<<<<< SEARCH\nc\n=======\nd\n")
    r = parse_fix_response(text)
    assert r.analysis == "no closing analysis"
    assert [(c.path, c.search, c.replace) for c in r.changes] == [("src/A.java", "a", "b"), ("src/B.java", "c", "d")]
    assert any("REPLACE" in w for w in r.warnings)


def test_missing_replace_before_next_search():
    text = "<patches>\n=== MODIFY A.java\n<<<<<<< SEARCH\na\n=======\nb\n<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE\n</patches>"
    r = parse_fix_response(text)
    assert [(c.search, c.replace) for c in r.changes] == [("a", "b"), ("c", "d")]


def test_unterminated_search_dropped():
    r = parse_fix_response("<patches>\n=== MODIFY A.java\n<<<<<<< SEARCH\na\n</patches>")
    assert r.changes == []
    assert any("no SEARCH/REPLACE" in w or "unterminated" in w for w in r.warnings)


def test_create_with_fenced_body_and_empty_replacement():
    text = ("<patches>\n=== CREATE src/N.java\n```java\nclass N {}\n```\n=== MODIFY src/A.java\n"
            "<<<<<<< SEARCH\nimport x.Y;\n=======\n>>>>>>> REPLACE\n</patches>")
    r = parse_fix_response(text)
    assert r.changes[0].content == "class N {}\n"
    assert r.changes[1].replace == ""


def test_need_files_and_grep():
    r = parse_fix_response("<analysis>need more</analysis>\n<need_files>src/a/A.java, ./pom.xml\n - src/b/B.java</need_files>"
                           "<need_grep>class\\s+Foo\nextends Bar</need_grep>")
    assert r.need_files == ["src/a/A.java", "pom.xml", "src/b/B.java"]
    assert r.need_grep == ["class\\s+Foo", "extends Bar"]
    assert r.wants_context and r.changes == []


def test_paths_rejected_and_normalized():
    assert normalize_path("/etc/passwd")[0] is None
    assert normalize_path("C:\\x\\y.java")[0] is None
    assert normalize_path("a/../../b")[0] is None
    assert normalize_path("~/x")[0] is None
    assert normalize_path(" `./src\\main\\A.java` ") == ("src/main/A.java", None)
    assert normalize_path("``")[0] is None
    r = parse_fix_response("<patches>\n=== CREATE /abs/X.java\nx\n=== RENAME a.java\n=== RENAME ../a -> b\n</patches>")
    assert r.changes == []
    assert len(r.warnings) >= 3


def test_no_patches_tag_but_headers_present():
    r = parse_fix_response("=== DELETE src/Old.java\n")
    assert r.changes == [FileChange(path="src/Old.java", op="delete")]
    assert any("no <patches>" in w for w in r.warnings)


def test_confidence_variants_and_garbage():
    assert parse_fix_response("<confidence>85</confidence>").confidence == 0.85
    assert parse_fix_response("<confidence>high</confidence>").confidence is None
    assert parse_fix_response("<confidence>250</confidence>").confidence == 1.0
    r = parse_fix_response("I think you should change the import.")
    assert r.changes == [] and "response did not follow the protocol" in r.warnings
    assert r.analysis.startswith("I think")
    assert parse_fix_response("").changes == []


def test_preamble_and_aliases():
    r = parse_fix_response("<patches>\nHere you go:\n=== EDIT A.java\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
                           "=== REMOVE B.java\n=== MOVE C.java → D.java\n</patches>")
    assert [c.op for c in r.changes] == ["modify", "delete", "rename"]
    assert r.changes[2].new_path == "D.java"
    assert any("ignored text" in w for w in r.warnings)


def test_need_tag_after_patches():
    r = parse_fix_response("<patches>\n</patches>\n<need_files>A.java</need_files>")
    assert r.need_files == ["A.java"] and r.changes == []
