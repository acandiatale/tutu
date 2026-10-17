import json

import pytest

from sbmig.core.interfaces import FixContext, FixerLike
from sbmig.core.models import (CaseType, LibraryUsage, Phase, PomInfo, ProjectProfile, ServiceInfo, SourceStats)
from sbmig.fix import FakeLLMClient, LLMFixer, RecordingClient
from sbmig.fix.client import LLMError
from sbmig.fix.fixer import load_prompts, profile_summary

from tests.fix.conftest import make_cluster

A = "src/main/java/a/A.java"
SRC = "package a;\n\nimport javax.servlet.Filter;\n\npublic class A {\n    Filter f;\n}\n"
GOOD = (f"<analysis>servlet moved to jakarta</analysis>\n<confidence>0.9</confidence>\n<patches>\n=== MODIFY {A}\n"
        "<<<<<<< SEARCH\nimport javax.servlet.Filter;\n=======\nimport jakarta.servlet.Filter;\n>>>>>>> REPLACE\n</patches>")
BAD = (f"<analysis>try</analysis>\n<patches>\n=== MODIFY {A}\n<<<<<<< SEARCH\nimport javax.servlet.Filtr;\n"
       "=======\nimport jakarta.servlet.Filter;\n>>>>>>> REPLACE\n</patches>")


def ctx(**kw):
    return FixContext(phase=Phase.SPRING, iteration=1, profile_summary="case=servlet_tomcat java=1.8", **kw)


@pytest.fixture()
def setup(ws, put):
    put(A, SRC)
    put("src/main/java/b/Helper.java", "package b;\n\npublic class Helper {\n    public static void help() {}\n}\n")
    put("pom.xml", "<project>\n  <artifactId>x</artifactId>\n</project>\n")
    put("src/main/resources/application.properties", "spring.redis.host=localhost\n")
    return ws


def test_prompts_loaded():
    system, user_t = load_prompts("v1")
    assert "senior Java / Spring migration engineer" in system and "=== MODIFY" in system
    assert "MIGRATION-TODO" in system and "@MockitoBean" in system and "jackson2" in system
    assert "{{" not in system
    with pytest.raises(FileNotFoundError):
        load_prompts("v999")


def test_direct_patch(cfg, setup):
    client = FakeLLMClient([GOOD])
    fixer = LLMFixer(cfg, client)
    assert isinstance(fixer, FixerLike)
    p = fixer.propose(setup, make_cluster(file=A, lines=(3,)), ctx())
    assert len(client.calls) == 1
    msgs = client.calls[0]["messages"]
    assert [m.role for m in msgs] == ["system", "user"]
    assert "import javax.servlet.Filter;" in msgs[1].content and "Errors to fix" in msgs[1].content
    assert client.calls[0]["max_tokens"] == cfg.llm.max_output_tokens
    assert client.calls[0]["temperature"] == cfg.llm.temperature
    assert p.analysis == "servlet moved to jakarta" and p.confidence == 0.9
    assert [c.op for c in p.changes] == ["modify"]
    assert p.llm_usage.calls == 1 and p.llm_usage.input_tokens > 0
    assert len(p.prompt_hash) == 64 and p.raw_response == GOOD and p.model == "fake"
    applied, errors = fixer.apply(setup, p.changes)
    assert errors == [] and "jakarta.servlet.Filter" in setup.read(A)
    journal = [json.loads(l) for l in setup.journal_path.read_text().splitlines()]
    ev = [e for e in journal if e["event"] == "llm_fix"][-1]
    assert ev["cluster_id"] == "c1" and ev["rounds"] == 0 and ev["changes"] == [{"op": "modify", "path": A}]
    assert ev["tokens"]["input"] > 0


def test_prompt_hash_stable(cfg, setup):
    p1 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose(setup, make_cluster(file=A), ctx())
    p2 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose(setup, make_cluster(file=A), ctx())
    p3 = LLMFixer(cfg, FakeLLMClient([GOOD])).propose(setup, make_cluster(file=A, message="other"), ctx())
    assert p1.prompt_hash == p2.prompt_hash != p3.prompt_hash


def test_need_files_round_then_patch(cfg, setup):
    client = FakeLLMClient(["<analysis>need helper</analysis><need_files>Helper.java, b.Helper, nope/X.java</need_files>"
                            "<need_grep>spring\\.redis\n[unclosed</need_grep>", GOOD])
    p = LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    assert len(client.calls) == 2
    second = client.calls[1]["messages"]
    assert [m.role for m in second] == ["system", "user", "assistant", "user"]
    assert second[2].content.startswith("<analysis>need helper")
    served = second[3].content
    assert "// src/main/java/b/Helper.java lines 1-5 (whole file)" in served
    assert "nope/X.java: file not found" in served
    assert "src/main/resources/application.properties:1: spring.redis.host=localhost" in served
    assert "grep /[unclosed/ → 0 matching line(s)" in served
    assert "1 more time(s)" in served
    assert p.requested_files == ["Helper.java", "b.Helper", "nope/X.java"]
    assert p.changes and p.llm_usage.calls == 2


def test_need_files_exhausted(cfg, setup):
    cfg.llm.max_context_rounds = 1
    client = FakeLLMClient(["<need_files>pom.xml</need_files>"])
    p = LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    assert len(client.calls) == 2
    assert "LAST context round" in client.calls[1]["messages"][-1].content
    assert p.changes == [] and "still requesting context" in p.analysis
    assert p.requested_files == ["pom.xml"]


def test_large_file_served_head_tail_and_ambiguous_candidates(cfg, setup, put):
    put("src/main/java/c/Big.java", "class Big {\n" + "".join(f"  int f{i};\n" for i in range(400)) + "}\n")
    put("src/test/java/b/Helper.java", "package b;\nclass Helper {}\n")
    fixer = LLMFixer(cfg, FakeLLMClient())
    served = fixer.serve_context(setup, ["Big.java", "Helper.java"] + [f"x{i}.java" for i in range(6)], [], 0)
    assert "lines 1-200 (of 402)" in served and "omitted" in served and "lines 323-402" in served
    assert "Helper.java: not found exactly; candidates:" in served
    assert "only the first 6 files" in served
    assert "LAST context round" in served


def test_invalid_search_feedback_then_corrected(cfg, setup):
    client = FakeLLMClient([BAD, GOOD])
    p = LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    assert len(client.calls) == 2
    fb = client.calls[1]["messages"][-1].content
    assert "block 1 of src/main/java/a/A.java: search text not found" in fb
    assert "import javax.servlet.Filter;" in fb and "current exact text of lines" in fb
    assert "Re-send the COMPLETE corrected answer" in fb
    assert p.changes[0].search == "import javax.servlet.Filter;"
    assert "[sbmig]" not in p.analysis


def test_feedback_still_failing_is_reported(cfg, setup):
    client = FakeLLMClient([BAD, BAD])
    p = LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    assert p.changes and "still do not apply" in p.analysis


def test_feedback_without_patches_keeps_first(cfg, setup):
    client = FakeLLMClient([BAD, "<analysis>sorry</analysis>"])
    p = LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    assert p.changes and "keeping first proposal" in p.analysis


def test_feedback_messages_for_other_failures(cfg, setup, put):
    put("src/main/java/a/Dup.java", "x();\nx();\n")
    resp = ("<patches>\n=== MODIFY src/main/java/a/Dup.java\n<<<<<<< SEARCH\nx();\n=======\ny();\n>>>>>>> REPLACE\n"
            f"=== CREATE {A}\nclass A {{}}\n=== DELETE src/main/java/zz/Helper.java\n</patches>")
    client = FakeLLMClient([resp, resp])
    LLMFixer(cfg, client).propose(setup, make_cluster(file=A), ctx())
    fb = client.calls[1]["messages"][-1].content
    assert "matches 2 times (ambiguous)" in fb
    assert "Use MODIFY" in fb
    assert "delete failed" in fb


def test_llm_error_gives_empty_proposal(cfg, setup):
    p = LLMFixer(cfg, FakeLLMClient([LLMError(503, "overloaded")])).propose(setup, make_cluster(file=A), ctx())
    assert p.changes == [] and p.analysis.startswith("llm-error:") and p.confidence == 0.0
    p = LLMFixer(cfg, FakeLLMClient([RuntimeError("kaboom")])).propose(setup, make_cluster(file=A), ctx())
    assert p.changes == [] and "kaboom" in p.analysis
    journal = setup.journal_path.read_text()
    assert '"error": "llm-error' in journal


def test_unparseable_response(cfg, setup):
    p = LLMFixer(cfg, FakeLLMClient(["Sure! Just change the import."])).propose(setup, make_cluster(file=A), ctx())
    assert p.changes == [] and p.analysis.startswith("llm-error: response did not follow the protocol")
    p = LLMFixer(cfg, FakeLLMClient()).propose(setup, make_cluster(file=A), ctx())
    assert p.changes == [] and p.analysis == "no fix"


def test_escalation_model_selection(cfg, setup):
    client = FakeLLMClient([GOOD])
    fixer = LLMFixer(cfg, client)
    cfg.llm.model = "base-model"
    fixer.propose(setup, make_cluster(file=A), ctx())
    cfg.llm.escalation_model = "big-model"
    fixer.propose(setup, make_cluster(file=A), ctx(use_escalation_model=True))
    fixer.propose(setup, make_cluster(file=A), ctx())
    cfg.llm.escalation_model = None
    fixer.propose(setup, make_cluster(file=A), ctx(use_escalation_model=True))
    cfg.llm.model = "default"
    fixer.propose(setup, make_cluster(file=A), ctx())
    assert [c["model"] for c in client.calls] == ["base-model", "big-model", "base-model", "base-model", "fake"]


def test_truncated_output_noted(cfg, setup):
    class Trunc(FakeLLMClient):
        def complete(self, messages, **kw):
            r = super().complete(messages, **kw)
            return r.model_copy(update={"finish_reason": "length"})
    p = LLMFixer(cfg, Trunc([GOOD])).propose(setup, make_cluster(file=A), ctx())
    assert "output truncated" in p.analysis


def test_record_dir(cfg, setup, tmp_path):
    cfg.llm.record_dir = str(tmp_path / "rec")
    LLMFixer(cfg, FakeLLMClient([GOOD])).propose(setup, make_cluster(file=A, cid="c/1"), ctx())
    files = list((tmp_path / "rec").glob("fix-c_1-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["cluster_id"] == "c/1" and data["proposal"]["changes"]
    # a RecordingClient already records → fixer does not duplicate
    rc = RecordingClient(FakeLLMClient([GOOD]), tmp_path / "rec2")
    LLMFixer(cfg, rc).propose(setup, make_cluster(file=A, cid="c2"), ctx())
    assert list((tmp_path / "rec2").glob("llm-*.json")) and not list((tmp_path / "rec").glob("fix-c2-*.json"))


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
