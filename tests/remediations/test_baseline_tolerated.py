"""The remediation after a failed build (`baseline_may_fail`, `RemediationState.baseline_tolerated`): a framework project
whose migration build failed still gets its vulnerability check. Fakes only — no IVAS, Nexus, Maven or network."""
from __future__ import annotations

from pathlib import Path

from sbmig.remediations import chain, graph
from sbmig.remediations.maven import compare_with_baseline
from sbmig.remediations.models import (
    FAILED,
    INCOMPLETE,
    STOPPED,
    SUCCEEDED,
    BuildOutcome,
    DeliveryInfo,
    RemediationState,
    Residual,
    ResolvedModel,
    RoundPlan,
    read_json,
)

from .fakes import (
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)
from .test_chain import ENV, Journal, project

BASE_ERR = ("src/main/java/App.java", 10, "FrameworkOnly")
OTHER_ERR = ("src/main/java/App.java", 20, "AnotherOne")


def javac_log(*errors, module="app", earlier=()):
    """A Maven log of a build that stopped in `module` with javac errors; `earlier` = modules that built first."""
    def make(project: Path) -> str:
        lines = ["[INFO] Scanning for projects..."]
        for m in earlier:
            lines += [f"[INFO] ------------------------< example:{m} >------------------------",
                      f"[INFO] Building {m} 0.1", f"[INFO] --- compiler:3.13.0:compile (default-compile) @ {m} ---"]
        lines += [f"[INFO] ------------------------< example:{module} >------------------------",
                  f"[INFO] Building {module} 0.1",
                  f"[INFO] --- compiler:3.13.0:compile (default-compile) @ {module} ---",
                  "[ERROR] COMPILATION ERROR : "]
        for f, ln, sym in errors:
            lines += [f"[ERROR] {project}/{f}:[{ln},9] cannot find symbol", f"  symbol:   variable {sym}",
                      "  location: class App"]
        lines += [f"[INFO] {len(errors)} errors", "[INFO] BUILD FAILURE",
                  ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile "
                   f"(default-compile) on project {module}: Compilation failure")]
        for f, ln, sym in errors:
            lines += [f"[ERROR] {project}/{f}:[{ln},9] cannot find symbol", f"[ERROR]   symbol:   variable {sym}",
                      "[ERROR]   location: class App"]
        lines += ["[ERROR] -> [Help 1]"]
        return "\n".join(lines) + "\n"
    return make


def unresolved_log(version="1.1"):
    def make(project: Path) -> str:
        return ("[INFO] Scanning for projects...\n"
                "[INFO] ------------------------< example:app >------------------------\n"
                "[INFO] Building app 0.1\n[INFO] BUILD FAILURE\n"
                "[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project "
                f"example:app:jar:0.1: The following artifacts could not be resolved: example:lib:jar:{version} (absent): "
                f"Could not find artifact example:lib:jar:{version} in central (https://repo.maven.apache.org/maven2) "
                "-> [Help 1]\n")
    return make


def fail(log_name="build.log"):
    return BuildOutcome(ok=False, exit_code=1, log=log_name, summary="[ERROR] COMPILATION ERROR")


def run(state, paths, comps, cfg=None):
    rec = Recorder()
    out = graph.run_remediation(state, paths, cfg or make_cfg(), comps, save=rec.save, journal=rec.journal,
                                stop=rec.stop)
    return out, rec


def tolerated_components(candidate_log, *, scan=None, maven_outcomes=(False, False)):
    scan = scan or FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    maven = FakeMaven(outcomes=list(maven_outcomes), logs=[javac_log(BASE_ERR), candidate_log])
    return make_components(scan=scan, plan=plan, maven=maven)


def tolerated_run(tmp_path):
    state, paths = make_run(tmp_path)
    state.baseline_tolerated = True
    return state, paths


# ---------------------------------------------------------------------------- the state field
def test_an_older_state_reads_as_not_tolerated():
    assert RemediationState.from_dict({"id": "x"}).baseline_tolerated is False
    assert DeliveryInfo.from_dict({"path": "d", "sha256": "s", "size": 1, "files": 1,
                                   "tree_hash": "t"}).build_tolerated is False


# ---------------------------------------------------------------------------- the comparison
def _cmp(tmp_path, base_text, cand_text, *, base=None, cand=None):
    broot, croot = tmp_path / "b", tmp_path / "c"
    return compare_with_baseline(base or fail(), base_text(broot), broot, cand or fail(), cand_text(croot), croot)


def test_same_errors_at_the_same_place_are_the_baseline_errors(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR, OTHER_ERR), javac_log(BASE_ERR, OTHER_ERR))
    assert out["same"] is True and out["new"] == []
    assert out["baseline_errors"] == 2 and out["candidate_errors"] == 2 and out["module"] == "app"


def test_fewer_errors_than_the_baseline_is_still_no_new_error(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR, OTHER_ERR), javac_log(BASE_ERR))
    assert out["same"] is True and out["candidate_errors"] == 1


def test_a_new_compile_error_is_new(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR), javac_log(BASE_ERR, OTHER_ERR))
    assert out["same"] is False and len(out["new"]) == 1 and "AnotherOne" in out["new"][0]


def test_a_version_that_does_not_resolve_is_new(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR), unresolved_log("1.1"))
    assert out["same"] is False and any("example:lib" in x for x in out["new"])


def test_a_failure_in_an_earlier_module_is_new_even_with_a_known_message(tmp_path):
    base = javac_log(BASE_ERR, module="web", earlier=("core",))
    cand = javac_log(BASE_ERR, module="core")
    out = _cmp(tmp_path, base, cand)
    assert out["same"] is False and out["module"] == "core" and out["baseline_module"] == "web"


def test_a_timed_out_or_unlogged_candidate_is_never_the_baseline(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR), javac_log(BASE_ERR),
               cand=BuildOutcome(ok=False, exit_code=None, log="build.log", timed_out=True))
    assert out["same"] is False and out["why"]
    broot = tmp_path / "b"
    out = compare_with_baseline(fail(), javac_log(BASE_ERR)(broot), broot, fail(), "", tmp_path / "c")
    assert out["same"] is False and out["why"]


def test_another_build_target_is_never_the_baseline(tmp_path):
    out = _cmp(tmp_path, javac_log(BASE_ERR), javac_log(BASE_ERR), base=fail("build-svc-a.log"),
               cand=fail("build-svc-b.log"))
    assert out["same"] is False and out["why"]


# ---------------------------------------------------------------------------- the graph with the mode on
def test_baseline_failure_goes_on_and_the_candidate_with_the_same_errors_is_delivered_and_rescanned(tmp_path):
    state, paths = tolerated_run(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    out, rec = run(state, paths, comps)
    assert out.status == INCOMPLETE, (out.reason, out.reasons, out.message)
    assert out.reason == "baseline_build_failed" and out.reasons[0] == "baseline_build_failed"
    assert "candidate_build_failed" not in out.reasons
    assert out.baseline_build is not None and out.baseline_build.ok is False
    ev = [e for e in rec.events if e["event"] == "remediation_baseline_failed"]
    assert ev and ev[0]["tolerated"] is True and ev[0]["errors"] == 1 and ev[0]["log"] == "baseline/build.log"
    rs = out.rounds[0]
    assert rs.committed and rs.build is not None and not rs.build.ok and rs.build_tolerated
    assert rs.build_compare["same"] is True and rs.build_compare["candidate_errors"] == 1
    assert next(e for e in rec.events if e["event"] == "remediation_candidate_compared")["same"] is True
    d = out.delivery
    assert d is not None and d.build_ok is False and d.build_tolerated is True and d.scanned is True
    assert [c.label for c in comps.scan.calls] == ["baseline", "final-r1"]
    assert "<version>1.1</version>" in (paths.accepted / "pom.xml").read_text()
    assert out.counts["fixed"] == 1
    assert "no build confirmed" in out.message and "none new" in out.message


def test_a_new_error_in_the_candidate_is_candidate_build_failed(tmp_path):
    state, paths = tolerated_run(tmp_path)
    comps = tolerated_components(unresolved_log("1.1"))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.reasons == ["candidate_build_failed", "baseline_build_failed"]
    assert out.delivery is not None and out.delivery.build_ok is False and not out.delivery.build_tolerated
    assert not out.delivery.scanned and [c.label for c in comps.scan.calls] == ["baseline"]
    assert not out.rounds[0].committed and out.rounds[0].build_compare["same"] is False
    assert "the baseline build did not have" in out.message


def test_a_passing_baseline_with_the_mode_on_is_an_ordinary_remediation(tmp_path):
    state, paths = tolerated_run(tmp_path)
    scan = FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    out, rec = run(state, paths, make_components(scan=scan, plan=plan, maven=FakeMaven(outcomes=[True, True])))
    assert out.status == SUCCEEDED and out.delivery.build_ok and not out.delivery.build_tolerated
    assert not [e for e in rec.events if e["event"] == "remediation_baseline_failed"]


def test_a_failing_candidate_after_a_passing_baseline_is_never_compared(tmp_path):
    state, paths = tolerated_run(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR), maven_outcomes=(True, False))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.rounds[0].build_compare == {}


def test_mode_off_baseline_failure_is_unchanged(tmp_path):
    state, paths = make_run(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    out, rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "baseline_build_failed" and out.reasons == ["baseline_build_failed"]
    assert comps.scan.calls == [] and not [e for e in rec.events if e["event"] == "remediation_baseline_failed"]


NOTHING_CONFIRMED = "이 프로젝트는 조치 전에도 빌드되지 않았지만, 바꾼 라이브러리 버전이 없어 빌드로 확인할 것은 없습니다."


def test_nothing_found_after_a_failed_baseline_succeeds_with_the_original(tmp_path):
    """Case A: no version was changed, so there is nothing a build would have had to confirm."""
    state, paths = tolerated_run(tmp_path)
    comps = make_components(maven=FakeMaven(outcomes=[False], logs=[javac_log(BASE_ERR)]))
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED and out.reason == "" and out.reasons == []
    assert out.delivery.round == 0 and out.delivery.build_ok is False and out.delivery.build_tolerated is True
    assert out.delivery.scanned is True
    md = paths.report_md.read_text(encoding="utf-8")
    head = md.split("## 요약")[0]
    assert "✅ **조치를 마쳤습니다.**" in head and NOTHING_CONFIRMED in head and "⚠️" not in head
    assert "빌드로 확인하지 못했습니다" not in md and "바꾼 버전으로 한 번 빌드하기" not in md
    assert md.count("조치 전에도 빌드되지 않았") == 1
    assert "원본 그대로, 빌드되지 않음, 재검사 완료" in md
    # the result comes first, the build aside after it; nothing claims a changed version text
    assert head.index("조치할 취약점이 없었습니다") < head.index(NOTHING_CONFIRMED)
    assert "- 결과 ZIP은 원본 ZIP과 같은 파일입니다." in md and "버전 표기뿐입니다" not in md
    # the stored English sentence (what the migration report and the page show) says the same
    from sbmig.remediations import i18n
    assert out.message.endswith("no version was changed, so there was nothing for a build to confirm")
    assert i18n.ko_text(out.message) == NOTHING_CONFIRMED


def test_nothing_changeable_after_a_failed_baseline_keeps_only_its_own_reasons(tmp_path):
    state, paths = tolerated_run(tmp_path)
    left = Residual(ga="example:lib", version="1.0", origin="plugin", reason="manual_review",
                    detail="build plugins are not changed automatically", finding_ids=["CVE-2026-0001"])
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", residuals=[left])])
    scan = FakeScanner({"baseline": [scan_result(finding())]})
    comps = make_components(scan=scan, plan=plan, maven=FakeMaven(outcomes=[False], logs=[javac_log(BASE_ERR)]))
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and "baseline_build_failed" not in out.reasons
    assert out.reasons[0] == "residual_findings" and "manual_review" in out.reasons
    head = paths.report_md.read_text(encoding="utf-8").split("## 요약")[0]
    assert NOTHING_CONFIRMED in head and "빌드로 확인하지 못했습니다" not in head


def test_an_incomplete_resolution_changes_nothing_and_keeps_the_scan_numbers(tmp_path):
    state, paths = tolerated_run(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    comps.maven.models = [ResolvedModel(complete=False, notes=["dependency:tree failed (see deptree-*.log)"])]
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and out.reasons == ["manual_review"]
    assert out.counts["baseline_findings"] == 1
    md = paths.report_md.read_text(encoding="utf-8")
    assert "취약점 1건을 찾았습니다" in md and "검사하지 않습니다" not in md and NOTHING_CONFIRMED in md


def test_changed_versions_still_never_succeed(tmp_path):
    """Case B keeps the machine status: applied changes without a build confirmation are INCOMPLETE."""
    state, paths = tolerated_run(tmp_path)
    out, _ = run(state, paths, tolerated_components(javac_log(BASE_ERR)))
    assert out.status == INCOMPLETE and out.reasons == ["baseline_build_failed"]
    from sbmig.remediations.report import build_unconfirmed
    assert build_unconfirmed(chain.summary(out, paths.root.parent)) is True


def test_a_candidate_that_builds_confirms_the_changes(tmp_path):
    """The baseline failed but the candidate build PASSES (a bumped version removed the error): a build did confirm
    the changes, so the run ends as an ordinary one and nobody reads "not confirmed by a build"."""
    state, paths = tolerated_run(tmp_path)
    out, _ = run(state, paths, tolerated_components(javac_log(BASE_ERR), maven_outcomes=(False, True)))
    assert out.status == SUCCEEDED and out.reasons == [], (out.status, out.reasons, out.message)
    rs = out.rounds[0]
    assert rs.committed and rs.build is not None and rs.build.ok and not rs.build_tolerated
    assert out.delivery is not None and out.delivery.build_ok is True and out.delivery.build_tolerated is False
    from sbmig.remediations.report import build_unconfirmed
    assert build_unconfirmed(chain.summary(out, paths.root.parent)) is False


def test_build_unconfirmed_is_only_the_fixed_but_unbuilt_case():
    from sbmig.remediations.report import build_unconfirmed
    b = {"status": "INCOMPLETE", "reasons": ["baseline_build_failed"], "baseline_tolerated": True}
    assert build_unconfirmed(b) is True
    assert build_unconfirmed({**b, "reasons": ["baseline_build_failed", "residual_findings"]}) is False
    assert build_unconfirmed({**b, "baseline_tolerated": False}) is False
    assert build_unconfirmed({**b, "status": "FAILED"}) is False
    assert build_unconfirmed({**b, "reasons": None, "reason": "baseline_build_failed"}) is False
    assert build_unconfirmed(None) is False


def test_stop_then_resume_keeps_the_mode(tmp_path):
    state, paths = tolerated_run(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    rec = Recorder(stop_after_saves=4)
    out = graph.run_remediation(state, paths, make_cfg(), comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    assert out.status == STOPPED
    saved = RemediationState.from_dict(rec.saves[-1])
    assert saved.baseline_tolerated is True
    out, _ = run(saved, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "baseline_build_failed"
    assert "candidate_build_failed" not in out.reasons


# ---------------------------------------------------------------------------- the report
def test_report_head_and_steps_of_the_tolerated_case(tmp_path):
    state, paths = tolerated_run(tmp_path)
    run(state, paths, tolerated_components(javac_log(BASE_ERR)))
    md = paths.report_md.read_text(encoding="utf-8")
    head = md.split("## 요약")[0]
    assert "검사하지 않습니다" not in md
    assert "조치 전에도 빌드되지 않았습니다" in head
    assert "다시 검사했습니다" in head and "빌드로 확인하지 못했습니다" in head
    assert "조치 전과 같은 오류에서 멈췄습니다" in head and "새로 생긴 오류는 없습니다" in head
    steps = md.split("## 해야 할 일")[1].split("##")[0]
    assert "빌드 오류 고치기" in steps and "변환 뒤 남은 빌드 오류" in steps and "한 번 빌드" in steps
    # v2.1z-bg: the migration report is named as the person receives it from the page
    assert "이관 보고서(`migration-report.md`)" in steps and "(`report.md`)" not in steps
    # the step points at a heading of the MIGRATION report: the two texts must not drift apart
    from sbmig.report.guide_ko import NO_FIX_TITLE
    assert f"「{NO_FIX_TITLE}」" in steps
    assert "빌드에 실패한 수정본" not in md
    assert md.count("조치 전에도 빌드되지 않았습니다") == 1


def test_report_of_a_new_error_says_what_was_new(tmp_path):
    state, paths = tolerated_run(tmp_path)
    run(state, paths, tolerated_components(javac_log(BASE_ERR, OTHER_ERR)))
    md = paths.report_md.read_text(encoding="utf-8")
    head = md.split("## 요약")[0]
    assert "조치 전에는 없던 오류" in head and "검사하지 않습니다" not in md


# ---------------------------------------------------------------------------- the chain
def _ivas(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def test_chain_persists_the_mode_and_the_summary_carries_it(tmp_path, monkeypatch):
    _ivas(monkeypatch)
    run_dir, root = project(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    j = Journal()
    out = chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id="run-1", journal=j,
                                    stop=lambda: False, components=comps, baseline_may_fail=True)
    assert out.status == INCOMPLETE and out.reason == "baseline_build_failed"
    assert read_json(chain.chain_paths(run_dir).state)["baseline_tolerated"] is True
    s = chain.summary(out, run_dir)
    assert s["baseline_tolerated"] is True and s["delivery"]["build_tolerated"] is True
    assert "remediation_baseline_differs" not in j.names()
    assert "migration's final build passed" not in out.message


def test_chain_without_the_mode_has_no_tolerated_key(tmp_path, monkeypatch):
    _ivas(monkeypatch)
    run_dir, root = project(tmp_path)
    out = chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id="run-1", journal=Journal(),
                                    stop=lambda: False, components=tolerated_components(javac_log(BASE_ERR)))
    assert out.status == FAILED and out.reason == "baseline_build_failed"
    assert "baseline_tolerated" not in chain.summary(out, run_dir)


def test_run_only_still_refuses_a_project_that_does_not_build(tmp_path, monkeypatch):
    _ivas(monkeypatch)
    run_dir, root = project(tmp_path)
    comps = tolerated_components(javac_log(BASE_ERR))
    out = chain.run_only(run_dir, root, make_cfg(), run_id="run-1", journal=Journal(), stop=lambda: False,
                         components=comps)
    assert out.status == FAILED and out.reason == "baseline_build_failed" and not out.baseline_tolerated
    assert comps.scan.calls == []
    md = chain.chain_paths(run_dir).report_md.read_text(encoding="utf-8")
    assert "빌드되지 않는 프로젝트는 검사하지 않습니다" in md
