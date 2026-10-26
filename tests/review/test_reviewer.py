"""v2 structure review (sbmig.review.StructureReviewer): fake LLM + fake script runner + fake KB on a real git
workspace — ok / warnings / critical with a script (left uncommitted) / script failure (tree restored) / unreadable
answers / LLM errors / disabled, the encoding rule, the KB record (failures swallowed)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from sbmig.core.interfaces import LLMResponse, StructureReviewerLike
from sbmig.core.models import CaseType, ProjectProfile, ScriptRunResult
from sbmig.core.workspace import Workspace
from sbmig.review import StructureReviewer
from sbmig.review.reviewer import RETRY_MESSAGE, ROLE, SBMIG_CHECK

ROOT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
    <version>4.1.0</version></parent>
  <groupId>com.x</groupId><artifactId>demo</artifactId><version>1.0</version><packaging>pom</packaging>
  <modules><module>module/boot-jar</module><module>module/comp-jar</module></modules>
</project>
"""
COMP_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.x</groupId><artifactId>demo</artifactId><version>1.0</version>
    <relativePath>../../pom.xml</relativePath></parent>
  <artifactId>demo-comp</artifactId>
</project>
"""
BOOT_POM = COMP_POM.replace("demo-comp", "demo-boot")

OK = '<review>{"verdict": "ok", "issues": [], "analysis": "All poms follow the rules."}</review>'
WARN = ('<review>{"verdict": "issues", "issues": [{"severity": "warning", "file": "module/comp-jar/pom.xml", '
        '"problem": "explicit jackson version", "fix_hint": "drop it"}], "analysis": "One leftover."}</review>\n'
        '<script>\ndef fix(p):\n    pass\n</script>')
CRITICAL = ('<review>{"verdict": "issues", "issues": [{"severity": "critical", "file": "pom.xml", '
            '"problem": "module/front-jar is listed but missing", "fix_hint": "remove the module"}, '
            '{"severity": "warning", "file": "", "problem": "old plugin", "fix_hint": ""}], '
            '"analysis": "A listed module is missing."}</review>\n'
            '<script>\n```python\ndef fix(p):\n    p.log("drop module")\n```\n</script>')


class FakeLLM:
    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, messages, *, model=None, max_tokens=None, temperature=None, stop=None, role=None,
                 metadata=None):
        self.calls.append({"messages": list(messages), "model": model, "role": role, "metadata": metadata})
        a = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return LLMResponse(content=a, model="gpt-review", input_tokens=120, output_tokens=40, finish_reason="stop")


class FakeRunner:
    """Applies `edits` ({path: text}) to the workspace and reports `status`."""

    def __init__(self, status: str = "ok", edits: Optional[dict[str, str]] = None, error: str = ""):
        self.status, self.edits, self.error = status, edits or {}, error
        self.calls: list[dict] = []

    def check(self, code: str) -> list[str]:
        return []

    def run(self, ws, code, *, errors=(), timeout_s=None) -> ScriptRunResult:
        self.calls.append({"code": code, "errors": list(errors)})
        for path, text in self.edits.items():
            ws.write(path, text)
        return ScriptRunResult(status=self.status, changed_files=sorted(self.edits), log=["drop module"],
                               error=self.error)


class FakeKB:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.reviews: list = []
        self.scripts = None

    def record_review(self, review) -> None:
        if self.fail:
            raise OSError("disk full")
        self.reviews.append(review)


def make_ws(tmp_path: Path, *, java: Optional[bytes] = None, comp_pom: str = COMP_POM) -> Workspace:
    root = tmp_path / "project"
    (root / "module/comp-jar/src/main/java/a").mkdir(parents=True)
    (root / "module/boot-jar").mkdir(parents=True)
    (root / "pom.xml").write_text(ROOT_POM)
    (root / "module/comp-jar/pom.xml").write_text(comp_pom)
    (root / "module/boot-jar/pom.xml").write_text(BOOT_POM)
    (root / "module/comp-jar/src/main/java/a/A.java").write_bytes(
        java if java is not None else b"package a;\nclass A { String s = \"hi\"; }\n")
    ws = Workspace(root, tmp_path / "run", "run-7")
    ws.git_init()
    return ws


def profile(case: CaseType = CaseType.SPRING_LEGACY) -> ProjectProfile:
    return ProjectProfile(root="/x", name="demo", case_type=case, java_version="1.8", spring_version="5.3.31")


def _review(cfg, ws, llm, runner=None, kb=None, **kw):
    rv = StructureReviewer(cfg, llm, runner, kb)
    return rv, rv.review(ws, profile(), cfg, run_id="run-7", **kw)


def test_ok_answer(tmp_path, cfg):
    ws = make_ws(tmp_path)
    llm, runner, kb = FakeLLM(OK), FakeRunner(), FakeKB()
    reviewer, rv = _review(cfg, ws, llm, runner, kb, restructure_notes=["restructure.apply (layout): 3 changed"])
    assert isinstance(reviewer, StructureReviewerLike)
    assert rv.verdict == "ok" and rv.issues == [] and rv.analysis == "All poms follow the rules."
    assert rv.id == "run-7:review" and rv.run_id == "run-7" and rv.project == "demo"
    assert rv.case_type == CaseType.SPRING_LEGACY and rv.model == "gpt-review"
    assert rv.llm_usage.calls == 1 and rv.llm_usage.input_tokens == 120 and rv.llm_usage.output_tokens == 40
    call = llm.calls[0]
    assert call["role"] == ROLE == "structure_review"
    assert call["metadata"] == {"run_id": "run-7", "project": "demo", "phase": "restructure",
                                "step": "restructure.review"}
    system, user = call["messages"][0].content, call["messages"][1].content
    assert "<review>" in system and "def fix(p)" in system and "p.pom(" in system and "{{" not in system
    for part in ("# Company rules", "`org.springframework.boot:spring-boot-starter-parent:4.1.0`",
                 "spring-boot-starter-validation", "# Module tree after the restructure", "`module/comp-jar/pom.xml`",
                 "restructure.apply (layout): 3 changed", "# Source encodings", "```xml", "<artifactId>demo-comp"):
        assert part in user, part
    assert runner.calls == [] and kb.reviews == [rv]


def test_warnings_only_never_run_the_script(tmp_path, cfg):
    ws = make_ws(tmp_path)
    runner = FakeRunner()
    _, rv = _review(cfg, ws, FakeLLM(WARN), runner, FakeKB())
    assert rv.verdict == "issues" and [i.severity for i in rv.issues] == ["warning"]
    assert rv.issues[0].file == "module/comp-jar/pom.xml" and rv.issues[0].fix_hint == "drop it"
    assert runner.calls == [] and rv.script_result is None
    assert rv.note == "script not run: the review reported no critical issue"


def test_critical_with_script_runs_and_leaves_the_change_uncommitted(tmp_path, cfg):
    ws = make_ws(tmp_path)
    head = ws.head()
    new_root = ROOT_POM.replace("<module>module/comp-jar</module>", "<module>module/comp-jar</module><!-- x -->")
    runner = FakeRunner(edits={"pom.xml": new_root})
    _, rv = _review(cfg, ws, FakeLLM(CRITICAL), runner, FakeKB())
    assert [i.severity for i in rv.critical] == ["critical"] and len(rv.issues) == 2
    assert runner.calls == [{"code": 'def fix(p):\n    p.log("drop module")\n', "errors": []}]
    assert rv.script == 'def fix(p):\n    p.log("drop module")\n'
    assert rv.script_result is not None and rv.script_result.status == "ok"
    assert not rv.reverted and rv.commit is None
    assert ws.head() == head and ws.is_dirty() and "<!-- x -->" in ws.read("pom.xml")
    assert rv.note.startswith("review script changed 1 file(s): pom.xml")


def test_script_failure_restores_the_tree(tmp_path, cfg):
    ws = make_ws(tmp_path)
    runner = FakeRunner(status="error", edits={"pom.xml": "<project>broken"}, error="KeyError: 'x'")
    _, rv = _review(cfg, ws, FakeLLM(CRITICAL), runner)
    assert rv.reverted and not ws.is_dirty() and ws.read("pom.xml") == ROOT_POM
    assert rv.script_result.status == "error" and rv.note.startswith("review script failed (error): KeyError")


@pytest.mark.parametrize("edits, words", [
    ({"module/comp-jar/src/main/java/a/A.java": "package a;\nclass A {}\n"}, "touched sources"),
    ({"pom.xml": "<project><modelVersion>4.0.0"}, "unparsable pom"),
])
def test_script_guards_restore_the_tree(tmp_path, cfg, edits, words):
    ws = make_ws(tmp_path)
    _, rv = _review(cfg, ws, FakeLLM(CRITICAL), FakeRunner(edits=edits))
    assert rv.reverted and not ws.is_dirty() and words in rv.note


def test_script_not_run_when_off_without_runner_or_dirty(tmp_path, cfg):
    ws = make_ws(tmp_path)
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"fix_critical": False})})
    runner = FakeRunner(edits={"pom.xml": ROOT_POM + "<!-- y -->"})
    rv = StructureReviewer(off, FakeLLM(CRITICAL), runner).review(ws, profile(), off, run_id="r")
    assert runner.calls == [] and rv.note == "script not run: review.fix_critical is off" and rv.script
    rv = StructureReviewer(cfg, FakeLLM(CRITICAL), None).review(ws, profile(), cfg, run_id="r")
    assert rv.note == "script not run: no script runner in this run"
    ws.write("module/boot-jar/pom.xml", BOOT_POM + "<!-- uncommitted restructure edit -->")
    rv = StructureReviewer(cfg, FakeLLM(CRITICAL), runner).review(ws, profile(), cfg, run_id="r")
    assert runner.calls == [] and "uncommitted changes" in rv.note
    assert "uncommitted restructure edit" in ws.read("module/boot-jar/pom.xml")
    no_script = CRITICAL.split("<script>")[0]
    rv = StructureReviewer(cfg, FakeLLM(no_script), runner).review(ws, profile(), cfg, run_id="r")
    assert rv.script == "" and rv.note == "no script for the critical issue(s): they are handed over"


def test_unreadable_answer_is_retried_once(tmp_path, cfg):
    ws = make_ws(tmp_path)
    llm = FakeLLM("I looked at it and it seems fine.", OK)
    _, rv = _review(cfg, ws, llm)
    assert rv.verdict == "ok" and len(llm.calls) == 2 and rv.llm_usage.calls == 2
    retry = llm.calls[1]["messages"]
    assert retry[-2].role == "assistant" and retry[-1].content == RETRY_MESSAGE
    llm = FakeLLM("<analysis>no fix</analysis><patches></patches>")
    kb = FakeKB()
    _, rv = _review(cfg, ws, llm, kb=kb)
    assert rv.verdict == "failed" and len(llm.calls) == 2 and "unreadable answer twice" in rv.note
    assert kb.reviews == [rv]                                         # failed reviews are recorded too


def test_llm_error_fails_without_retry(tmp_path, cfg):
    from sbmig.fix.client import LLMError

    ws = make_ws(tmp_path)
    llm = FakeLLM(LLMError(503, "gateway busy"))
    _, rv = _review(cfg, ws, llm, FakeRunner())
    assert rv.verdict == "failed" and len(llm.calls) == 1 and rv.note.startswith("llm-error: LLMError:")
    llm = FakeLLM("garbage", RuntimeError("boom"))
    _, rv = _review(cfg, ws, llm)
    assert rv.verdict == "failed" and "then llm-error: RuntimeError: boom" in rv.note


def test_disabled_or_no_llm_is_skipped(tmp_path, cfg):
    ws = make_ws(tmp_path)
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"enabled": False})})
    llm, kb = FakeLLM(OK), FakeKB()
    rv = StructureReviewer(off, llm, FakeRunner(), kb).review(ws, profile(), off, run_id="r")
    assert rv.verdict == "skipped" and rv.note == "review.enabled is off" and llm.calls == [] and kb.reviews == []
    rv = StructureReviewer(cfg, None, FakeRunner(), kb).review(ws, profile(), cfg, run_id="r")
    assert rv.verdict == "skipped" and "no LLM client" in rv.note and kb.reviews == []


def test_record_review_failures_are_swallowed(tmp_path, cfg, caplog):
    ws = make_ws(tmp_path)
    _, rv = _review(cfg, ws, FakeLLM(OK), kb=FakeKB(fail=True))
    assert rv.verdict == "ok" and "not recorded in the KB" in caplog.text
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"record": False})})
    kb = FakeKB()
    StructureReviewer(off, FakeLLM(OK), None, kb).review(ws, profile(), off, run_id="r")
    assert kb.reviews == []


def test_unexpected_errors_never_raise(tmp_path, cfg, monkeypatch):
    ws = make_ws(tmp_path)
    monkeypatch.setattr("sbmig.review.reviewer.collect_review_input",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad tree")))
    rv = StructureReviewer(cfg, FakeLLM(OK)).review(ws, profile(), cfg, run_id="r")
    assert rv.verdict == "failed" and rv.note == "review error: ValueError: bad tree"


def test_encoding_mismatch_is_shown_and_enforced(tmp_path, cfg):
    java = "package a;\nclass A { String s = \"한글 인사\"; }\n".encode("cp949")
    ws = make_ws(tmp_path, java=java)
    llm = FakeLLM(OK)
    _, rv = _review(cfg, ws, llm)
    user = llm.calls[0]["messages"][1].content
    assert "cp949 1 (Java charset MS949)" in user and "**ENCODING MISMATCH: MS949 sources" in user
    assert "`module/comp-jar/src/main/java/a/A.java`" in user
    # the LLM said ok → sbmig adds the critical issue itself (no script for it)
    assert rv.verdict == "issues" and len(rv.critical) == 1
    issue = rv.critical[0]
    assert issue.file == "module/comp-jar/pom.xml" and issue.problem.startswith(SBMIG_CHECK)
    assert "MS949" in issue.fix_hint
    # the LLM named it → not added twice
    named = ('<review>{"verdict": "issues", "issues": [{"severity": "critical", "file": "module/comp-jar/pom.xml", '
             '"problem": "MS949 sources but no source encoding", "fix_hint": "set project.build.sourceEncoding"}], '
             '"analysis": ""}</review>')
    _, rv = _review(cfg, ws, FakeLLM(named))
    assert len(rv.critical) == 1 and not rv.critical[0].problem.startswith(SBMIG_CHECK)


def test_declared_legacy_encoding_is_no_mismatch(tmp_path, cfg):
    java = "package a;\nclass A { String s = \"한글\"; }\n".encode("cp949")
    comp = COMP_POM.replace("<artifactId>demo-comp</artifactId>",
                            "<artifactId>demo-comp</artifactId><properties>"
                            "<project.build.sourceEncoding>MS949</project.build.sourceEncoding></properties>")
    ws = make_ws(tmp_path, java=java, comp_pom=comp)
    llm = FakeLLM(OK)
    _, rv = _review(cfg, ws, llm)
    user = llm.calls[0]["messages"][1].content
    assert "source encoding: `MS949` (`module/comp-jar/pom.xml` property project.build.sourceEncoding)" in user
    assert "ENCODING MISMATCH" not in user and rv.verdict == "ok"


def test_korean_report_language_asks_for_korean_text(tmp_path, cfg):
    ws = make_ws(tmp_path)
    ko = cfg.model_copy(update={"report": cfg.report.model_copy(update={"language": "ko"})})
    llm = FakeLLM(OK)
    StructureReviewer(ko, llm).review(ws, profile(), ko, run_id="r")
    assert "in Korean" in llm.calls[0]["messages"][1].content
