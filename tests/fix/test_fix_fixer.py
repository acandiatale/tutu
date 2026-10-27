"""LLMFixer building blocks shared by every script request: <need_files> / <need_grep> serving, profile summary.
(propose_script itself: test_fix_script.py.)"""
import pytest

from sbmig.core.models import (CaseType, LibraryUsage, PomInfo, ProjectProfile, ServiceInfo, SourceStats)
from sbmig.fix import FakeLLMClient, LLMFixer
from sbmig.fix.fixer import profile_summary

A = "src/main/java/a/A.java"
SRC = "package a;\n\nimport javax.servlet.Filter;\n\npublic class A {\n    Filter f;\n}\n"


@pytest.fixture()
def setup(ws, put):
    put(A, SRC)
    put("src/main/java/b/Helper.java", "package b;\n\npublic class Helper {\n    public static void help() {}\n}\n")
    put("pom.xml", "<project>\n  <artifactId>x</artifactId>\n</project>\n")
    put("src/main/resources/application.properties", "spring.redis.host=localhost\n")
    return ws


def test_serve_context_files_grep_and_round_hints(cfg, setup):
    fixer = LLMFixer(cfg, FakeLLMClient())
    served = fixer.serve_context(setup, ["Helper.java", "b.Helper", "nope/X.java"], ["spring\\.redis", "[unclosed"], 1)
    assert "// src/main/java/b/Helper.java lines 1-5 (whole file)" in served          # by name and by FQCN
    assert served.count("public class Helper") == 2
    assert "nope/X.java: file not found" in served
    assert "src/main/resources/application.properties:1: spring.redis.host=localhost" in served
    assert "grep /[unclosed/ → 0 matching line(s)" in served                        # bad regex → literal search
    assert "1 more time(s), but prefer answering now (<analysis>, <solution>, <edits>, <script>)." in served
    assert "<patches>" not in served
    last = fixer.serve_context(setup, ["pom.xml"], [], 0)
    assert "This was the LAST context round. Answer now with <analysis>, <solution>, <edits> and <script>" in last
    assert "<no_script>" in last


def test_large_file_served_head_tail_and_ambiguous_candidates(cfg, setup, put):
    put("src/main/java/c/Big.java", "class Big {\n" + "".join(f"  int f{i};\n" for i in range(400)) + "}\n")
    put("src/test/java/b/Helper.java", "package b;\nclass Helper {}\n")
    fixer = LLMFixer(cfg, FakeLLMClient())
    served = fixer.serve_context(setup, ["Big.java", "Helper.java"] + [f"x{i}.java" for i in range(6)], [], 0)
    assert "lines 1-200 (of 402)" in served and "omitted" in served and "lines 323-402" in served
    assert "Helper.java: not found exactly; candidates:" in served
    assert "only the first 6 files" in served
    assert "LAST context round" in served


def test_profile_summary():
    prof = ProjectProfile(
        root="/x", name="demo", case_type=CaseType.SPRING_BOOT, java_version="1.8", spring_boot_version="2.7.18",
        spring_version="5.3.31", packaging="war", poms=[PomInfo(path="pom.xml", artifact_id="demo")],
        services=[ServiceInfo(name="s", root_pom="s/pom.xml", case_type=CaseType.SPRING_BOOT)],
        libraries=[LibraryUsage(name="lombok", version="1.18.20"), LibraryUsage(name="jpa")],
        source=SourceStats(has_web_xml=True, jsp_count=3, javax_imports={"javax.servlet": 4}))
    s = profile_summary(prof)
    for part in ("case=spring_boot", "java=1.8", "boot=2.7.18", "spring=5.3.31", "packaging=war", "modules=1",
                 "services=1", "libs=lombok:1.18.20,jpa", "web.xml", "jsp=3", "javax-imports=javax.servlet",
                 "target: JDK 21, Spring Boot 4.1"):
        assert part in s


def test_grep_reads_each_file_once_and_respects_the_budget(setup, cfg, monkeypatch):
    """Round 9c: a <need_grep> over a big tree on Windows crawled for minutes — one pass per file, big files skipped,
    a time budget with an honest note."""
    import sbmig.fix.fixer as fx
    fixer = LLMFixer(cfg, FakeLLMClient())
    out = fixer.grep(setup, ["class", "nothing-matches-this"])
    assert "grep /class/ →" in out and "grep /nothing-matches-this/ → 0 matching line(s)" in out
    assert "grep stopped" not in out
    monkeypatch.setattr(fx, "GREP_BUDGET_S", -1.0)                 # budget already spent → first file stops it
    out = fixer.grep(setup, ["class"])
    assert "grep stopped after 0 of" in out and "time budget" in out
    monkeypatch.setattr(fx, "GREP_BUDGET_S", 45.0)
    monkeypatch.setattr(fx, "MAX_GREP_FILE_BYTES", 1)              # every file is "too big" → skipped, no crash
    out = fixer.grep(setup, ["class"])
    assert "grep /class/ → 0 matching line(s)" in out


def test_no_search_replace_path_left():
    """The SEARCH/REPLACE protocol is gone: the fixer only answers script requests (and applies nothing itself)."""
    import sbmig.fix as fix_pkg

    for name in ("propose", "apply", "feedback_message", "build_messages"):
        assert not hasattr(LLMFixer, name), name
    for name in ("PatchApplier", "parse_fix_response", "render_protocol_instructions", "load_prompts"):
        assert not hasattr(fix_pkg, name), name
