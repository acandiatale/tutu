"""v2 (flow-v2) shared contracts: fix-script and structure-review models, config keys and the v2 defaults
(docs/v2/PLAN-v2.md §2–§3). The implementations live in sbmig.script / sbmig.kb.scripts / sbmig.review / the loop."""
from __future__ import annotations

from sbmig.core.config import Config, load_config
from sbmig.core.interfaces import FixContext, ScriptFixerLike, ScriptRunnerLike, ScriptStoreLike, StructureReviewerLike
from sbmig.core.models import (KB_RECORD_SCHEMA, CaseAction, DecisionKind, ErrorAttemptRef, ErrorCaseRecord,
                               ErrorCluster, ErrorKind, FixAttempt, FixAttemptRecord, Phase, ReviewIssue, RunReport,
                               ScriptBase, ScriptMeta, ScriptProposal, ScriptRunResult, StepKind, StructureReview,
                               TriageDecision, CaseType)


def test_v2_enum_values():
    assert Phase.TRANSFORM.value == "transform"
    assert StepKind.REVIEW.value == "review"
    assert DecisionKind.SCRIPT.value == "script"
    assert CaseAction(kind="script", script_version=2).script_version == 2
    assert KB_RECORD_SCHEMA == 3


def test_script_records_round_trip():
    res = ScriptRunResult(status="error", changed_files=["src/A.java"], log=["A: import"], error="KeyError: x")
    att = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:abc",
                     decision=TriageDecision(cluster_id="case:abc", kind=DecisionKind.SCRIPT),
                     script_mode="augment", script_case_id="abc", script_version=3, script_parent=2,
                     script_code="def fix(p):\n    pass\n", script_result=res)
    again = FixAttempt.model_validate_json(att.model_dump_json())
    assert again.script_result == res and again.script_mode == "augment" and again.script_parent == 2
    rec = FixAttemptRecord(id="r:verify-1-1", fix_method="script_augment", resolver_id="script:abc:v3",
                           script_mode="augment", script_case_id="abc", script_version=3, script_parent=2,
                           script_status="error", script_error="KeyError: x", script_log=["A: import"])
    assert FixAttemptRecord.model_validate_json(rec.model_dump_json()) == rec
    assert rec.schema_version == 3
    err = ErrorCaseRecord(id="e1", fix_method="script_reuse", script_case_id="abc", script_version=1,
                          attempts=[ErrorAttemptRef(attempt_id="r:verify-1-1", fix_method="script", script_version=1)])
    assert ErrorCaseRecord.model_validate_json(err.model_dump_json()).attempts[0].script_version == 1


def test_script_meta_and_review_models():
    meta = ScriptMeta(case_id="abc", version=2, parent=1, signature_generic="compile|cannot_find_symbol|clientIp")
    assert meta.status == "active" and meta.successes == 0 and meta.schema_version == 3
    base = ScriptBase(case_id="abc", version=1, code="def fix(p): pass", remaining_errors=["A.java:3 x"])
    ctx = FixContext(phase=Phase.VERIFY, iteration=2, script_mode="augment", case_id="abc", script_base=base)
    assert ctx.script_base.version == 1
    assert ScriptProposal().code == "" and ScriptProposal(error="no <script> block").error
    rv = StructureReview(run_id="r", verdict="issues", case_type=CaseType.SPRING_LEGACY,
                         issues=[ReviewIssue(severity="critical", file="pom.xml", problem="no parent"),
                                 ReviewIssue(severity="warning", problem="duplicate dependency")])
    assert [i.problem for i in rv.critical] == ["no parent"]
    rep = RunReport(run_id="r", project_name="p", case_type=CaseType.UNKNOWN, started_at=rv.at, structure_review=rv)
    assert RunReport.model_validate_json(rep.model_dump_json()).structure_review.verdict == "issues"
    c = ErrorCluster(id="case:abc", kind=ErrorKind.COMPILE, errors=[], case_id="abc")
    assert c.case_id == "abc" and c.file is None


def test_v2_protocols_are_runtime_checkable():
    class Runner:
        def check(self, code):
            return []

        def run(self, ws, code, *, errors=(), timeout_s=None):
            return ScriptRunResult()

    class Fixer:
        def propose_script(self, ws, cluster, ctx):
            return ScriptProposal()

    assert isinstance(Runner(), ScriptRunnerLike) and isinstance(Fixer(), ScriptFixerLike)
    assert not isinstance(Fixer(), ScriptStoreLike) and not isinstance(Fixer(), StructureReviewerLike)


def test_v2_defaults_in_yml_and_code(repo_root):
    yml = load_config(repo_root / "config" / "artifacts.example.yml", repo_root=repo_root)
    for cfg in (yml, Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})):
        rs = cfg.openrewrite.recipes
        assert rs.jdk21 == ["org.openrewrite.java.migrate.UpgradeToJava21"]
        assert rs.jakarta == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"]
        assert rs.spring_boot_4 == ["org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0",
                                    "org.openrewrite.java.spring.boot4.SpringBootProperties_4_1"]
        assert rs.spring_framework_7 == rs.spring_security_7 == rs.junit5 == []
        assert not any(cfg.loop.phases[k].enabled for k in ("jdk21", "spring", "restructure"))
        assert cfg.loop.phases["verify"].enabled and cfg.loop.phases["verify"].max_iterations >= 10
        assert cfg.build.skip_test_compile is True
        assert cfg.kb.dir == "kb-v2"
        assert cfg.kb.error_cases and cfg.kb.catalog
        for gone in ("use_active_recipes", "harvest", "case_replace", "ledger"):        # v1 KB switches (step 4)
            assert not hasattr(cfg.kb, gone)
        assert cfg.script.timeout_seconds == 60 and cfg.script.max_augments_per_case == 3 and cfg.script.reuse
        assert cfg.review.enabled and cfg.review.fix_critical and cfg.review.record


def test_company_example_does_not_carry_v1_loop_overrides(repo_root):
    cfg = load_config(repo_root / "config" / "artifacts.example.yml", repo_root=repo_root)
    assert cfg.loop.phases["verify"].max_iterations == 12
    assert cfg.build.skip_test_compile is True and cfg.report.language == "ko"
