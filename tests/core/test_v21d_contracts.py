"""v2.1d (2026-10-27) shared contracts: dependency version policy — the models the loop, the report and the prompt
share, the seams and the config keys (docs/INTERFACES.md "v2.1d")."""
from __future__ import annotations

from sbmig.core.config import Config, load_config
from sbmig.core.interfaces import FixContext, VerifyGuideLike
from sbmig.core.jakarta_libraries import JakartaLibrary
from sbmig.core.models import (
    DecisionKind, DependencyChange, FixAttempt, FixTry, OperatorCheck, Phase, RepositoryVersions, RunReport,
    ScriptProposal, StructureReview, TriageDecision, VersionCandidate,
)


def test_dependency_change_text_and_operator_flag():
    c = DependencyChange(path="xxx-back/pom.xml", key="org.sitemesh:sitemesh", before="2.4.2", after="3.2.1",
                         kind="major")
    assert c.needs_operator and c.text == "org.sitemesh:sitemesh 2.4.2 → 3.2.1" and c.section == "dependencies"
    r = DependencyChange(path="pom.xml", key="opensymphony:sitemesh", before="2.4.2", after="3.2.1", kind="replaced",
                         replaced_by="org.sitemesh:sitemesh")
    assert r.needs_operator and r.text == "opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1"
    for kind in ("minor", "added", "removed"):
        assert not DependencyChange(path="pom.xml", key="a:b", kind=kind).needs_operator
    assert DependencyChange(path="pom.xml", key="a:b", kind="added", after="1.0").text == "a:b - → 1.0"


def test_models_round_trip_through_json():
    rv = RepositoryVersions(key="opensymphony:sitemesh", current="2.4.2", major=2,
                            same_major=[VersionCandidate(version="2.5.0", jakarta=False),
                                        VersionCandidate(version="2.6.0", jakarta=True, java=17)],
                            higher=[VersionCandidate(version="3.3.0", jakarta=True)], first_jakarta="2.6.0",
                            source="https://nexus.example.com/repository/maven-public/opensymphony/sitemesh/maven-metadata.xml")
    assert RepositoryVersions.model_validate_json(rv.model_dump_json()) == rv
    ch = DependencyChange(path="pom.xml", key="a:b", before="1.0", after="2.0", kind="major", approved=True,
                          note="approved by the company table")
    t = FixTry(n=1, source="llm-new", major_upgrades=[ch], verify="운영자 확인: a:b 2.0 의 설정 파일을 확인하세요")
    assert FixTry.model_validate_json(t.model_dump_json()).major_upgrades[0].approved
    oc = OperatorCheck(kind="major_upgrade", change=ch, case_id="abc1234567", source="fix r3 try 2", commit="1a2b3c4",
                       guide=t.verify, guide_source="llm", files=["pom.xml"])
    rep = RunReport(run_id="r", project_name="p", case_type="pure_java", started_at="2026-10-27T00:00:00Z",
                    operator_checks=[oc])
    assert RunReport.model_validate_json(rep.model_dump_json()).operator_checks[0].change.text == "a:b 1.0 → 2.0"
    assert ScriptProposal().verify == "" and StructureReview().major_upgrades == []
    att = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:x",
                     decision=TriageDecision(cluster_id="case:x", kind=DecisionKind.SCRIPT))
    assert att.major_upgrades == [] and att.verify == "" and att.verify_source == ""


def test_seams_and_table_fields():
    assert FixContext(phase=Phase.VERIFY, iteration=1).library_hints == []
    assert getattr(VerifyGuideLike, "_is_runtime_protocol", False)

    class _F:
        def verify_guide(self, ws, cluster, ctx, changes, diff):
            return ""
    assert isinstance(_F(), VerifyGuideLike)
    lib = JakartaLibrary(key="org.sitemesh:sitemesh", jakarta_since="3.2.0", version="3.3.0")
    assert lib.allow_major is False and lib.source == "shipped"
    assert JakartaLibrary(key="a:b", jakarta_since="2", version="2", allow_major=True, source="company").allow_major


def test_v21d_config_keys(repo_root):
    for cfg in (load_config(repo_root / "config" / "artifacts.example.yml", repo_root=repo_root),
                Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})):
        lib = cfg.libraries
        assert lib.lookup and lib.max_pom_checks == 6 and not lib.include_prerelease
        assert lib.max_artifacts_per_case == 3 and lib.company_file == "libraries.yml" and lib.verify_guide_llm
        assert cfg.build.repository_url is None and cfg.build.repository_timeout_seconds == 10
    cfg = Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}},
                 libraries={"lookup": False, "company_file": "/abs/libraries.yml"},
                 build={"repository_url": "https://nexus.example.com/repository/maven-public"})
    assert not cfg.libraries.lookup and cfg.build.repository_url.startswith("https://nexus")
