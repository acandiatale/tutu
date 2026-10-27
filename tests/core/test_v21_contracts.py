"""v2.1 contracts (docs/v2/PLAN-v2.1.md): the error list, the LLM answer with solution / edits, case scripts, the
log stage context and the new limits."""
from __future__ import annotations

import logging

from sbmig.core.config import Config, load_config
from sbmig.core.models import (CaseInfo, CaseScript, Edit, ErrorEntry, ErrorKind, FixAnswer, FixTry, RunReport,
                               ScriptProposal, CaseType)
from sbmig.core.stage import (StageFilter, current_stage, install_stage_filter, reset_stage, set_stage, stage,
                              stage_formatter)


def test_answer_carries_cause_solution_edits_and_script():
    a = FixAnswer(analysis="JAXB left the JDK", solution="declare jakarta.xml.bind-api", code="def fix(p): pass",
                  edits=[Edit(op="replace", path="pom.xml", old="<dependencies>", new="<dependencies>…"),
                         Edit(op="pom", path="pom.xml", pom={"add_dependency": {"group_id": "g", "artifact_id": "a"}})])
    assert FixAnswer is ScriptProposal and a.edits[1].pom["add_dependency"]["artifact_id"] == "a"
    assert ScriptProposal(no_script_reason="generated code differs per project").code == ""


def test_error_entry_round_trip():
    e = ErrorEntry(id="abc", run_id="r", case_id="7a52b1cea8", l2="compile|package_does_not_exist|jakarta.xml.bind",
                   kind=ErrorKind.COMPILE, file="a/B.java", line=6, message="package jakarta.xml.bind does not exist",
                   seen="compile",
                   tries=[FixTry(n=1, round=1, source="llm-new", analysis="x", solution="y", applied="edits",
                                 files=["pom.xml"], diff="+<dependency>", commit="abc", result="improved")])
    again = ErrorEntry.model_validate_json(e.model_dump_json())
    assert again.tries[0].applied == "edits" and again.status == "open"
    assert ErrorEntry(id="p", seen="restructure", status="pom-verify", note="parent → base-parent").status == "pom-verify"
    c = CaseInfo(case_id="7a52b1cea8", scripts=[CaseScript(version=1, file="fix_v1.py")])
    assert c.scripts[0].status == "unverified"
    rep = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=e.at, error_entries=[e])
    assert RunReport.model_validate_json(rep.model_dump_json()).error_entries[0].id == "abc"


def test_log_stage_context_tags_records():
    lg = logging.getLogger("sbmig.test.stage")
    install_stage_filter(logging.getLogger("sbmig"))
    seen: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append(f"[{record.stage}] {record.getMessage()}")
    h = Grab(); lg.addHandler(h)
    try:
        lg.info("outside")
        with stage("fix r1 7a52b1cea8"):
            lg.info("inside")
            with stage("kb"):
                assert current_stage() == "kb"
            assert current_stage() == "fix r1 7a52b1cea8"
        tok = set_stage("build"); lg.info("set"); reset_stage(tok)
        assert current_stage() == "-"
    finally:
        lg.removeHandler(h)
    assert seen == ["[-] outside", "[fix r1 7a52b1cea8] inside", "[build] set"]
    assert isinstance(logging.getLogger("sbmig").filters[0], StageFilter)
    # a formatter fills the stage for records of any logger (propagated records never pass a logger's filters)
    rec = logging.LogRecord("other", logging.INFO, __file__, 1, "x", None, None)
    with stage("report"):
        assert stage_formatter("[%(stage)s] %(message)s").format(rec) == "[report] x"


def test_v21_limits_and_keys(repo_root):
    for cfg in (load_config(repo_root / "config" / "artifacts.example.yml", repo_root=repo_root),
                Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})):
        assert cfg.loop.phases["verify"].max_iterations == 50
        assert cfg.loop.max_llm_calls == 0                       # 0 = unlimited
        assert cfg.errors.diff_chars == 6000 and cfg.errors.md_view
