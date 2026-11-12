"""The remediation LangGraph (sbmig/remediations/graph.py) driven by fakes: no IVAS, Nexus, Maven or network."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sbmig.remediations import graph
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_APPROVED,
    D_PENDING,
    D_REJECTED,
    FAILED,
    INCOMPLETE,
    NODES,
    STOPPED,
    SUCCEEDED,
    BoundaryFinding,
    BuildOutcome,
    Inventory,
    Residual,
    RoundPlan,
    ScanResult,
    UploadReceipt,
    read_json,
    write_json_atomic,
)

from .fakes import (
    POM,
    STOP,
    UNCERTAIN,
    FakeApply,
    FakeIntakeFns,
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedInventory,
    ScriptedPlanner,
    file_sha,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)

POM2 = POM.replace("  </dependencies>", """    <dependency>
      <groupId>example</groupId>
      <artifactId>other</artifactId>
      <version>2.0</version>
    </dependency>
  </dependencies>""")


def run(state, paths, comps, rec=None, cfg=None):
    rec = rec or Recorder()
    out = graph.run_remediation(state, paths, cfg or make_cfg(), comps, save=rec.save, journal=rec.journal,
                                stop=rec.stop)
    return out, rec


def one_fix_components(**kw):
    """Baseline: one finding; plan: one auto proposal 1.0 → 1.1; rescan: clean."""
    scan = kw.pop("scan", None) or FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]})
    plan = kw.pop("plan", None) or ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                              proposals=[proposal()])])
    return make_components(scan=scan, plan=plan, **kw)


def tree_hash(paths):
    fi = FakeIntakeFns()
    return fi.tree_hash(paths.accepted, fi.manifest_paths(paths.manifest))


# ---------------------------------------------------------------------------- structure
def test_graph_nodes_are_route_plus_the_contract_nodes():
    g = graph.build_graph(SimpleNamespace(state=None))
    names = [n for n in g.get_graph().nodes if not n.startswith("__")]
    assert names == ["route", *NODES]


def test_new_state_and_can_resume(tmp_path):
    state, _ = make_run(tmp_path, cfg=make_cfg(require_approval=True))
    assert state.round == 1 and state.next_node == NODES[0] and state.require_approval
    assert state.policy_hash == "policy-hash-1" and state.created > 0
    assert not graph.can_resume(state)
    state.status = STOPPED
    assert graph.can_resume(state)


# ---------------------------------------------------------------------------- happy paths
def test_normal_flow_succeeds_with_the_scanned_bytes_delivered(tmp_path):
    state, paths = make_run(tmp_path)
    orig_sha = file_sha(paths.original_zip)
    comps = one_fix_components()
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED, (out.reason, out.message)
    assert out.reasons == [] and out.reason == ""
    final = ScanResult.from_dict(read_json(paths.root / out.final_scan))
    assert final.zip_sha256 == out.delivery.sha256 == file_sha(paths.delivery_zip)
    assert not (paths.accepted / "target").exists()
    assert "<version>1.1</version>" in (paths.accepted / "pom.xml").read_text()
    assert file_sha(paths.original_zip) == orig_sha
    assert paths.patch(1).read_text().count("+      <version>1.1</version>") == 1
    assert paths.report_md.is_file() and paths.report_json.is_file()
    assert out.counts["baseline_findings"] == 1 and out.counts["final_findings"] == 0 and out.counts["fixed"] == 1
    assert out.rounds[0].committed and out.rounds[0].applied == ["p-1-1"]
    assert comps.scan.uploads == 2
    # the baseline was built in an isolated copy, the candidate in rounds/1/candidate
    assert comps.maven.package_calls == [paths.root / "baseline" / "tree", paths.candidate(1)]


def test_zero_baseline_findings_delivers_the_original_bytes(tmp_path):
    state, paths = make_run(tmp_path)
    comps = make_components()
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED
    assert paths.delivery_zip.read_bytes() == paths.original_zip.read_bytes()
    assert out.delivery.sha256 == state.intake.zip_sha256 and out.delivery.round == 0
    assert comps.scan.uploads == 1
    assert out.final_scan == out.baseline_scan
    assert comps.plan.calls == []


def test_state_is_saved_at_every_node_boundary(tmp_path):
    state, paths = make_run(tmp_path)
    out, rec = run(state, paths, one_fix_components())
    assert out.status == SUCCEEDED
    seen = [s["next_node"] for s in rec.saves]
    order = [n for i, n in enumerate(seen) if i == 0 or seen[i - 1] != n]
    assert order == list(NODES)
    starts = [e["node"] for e in rec.events if e["event"] == "node_start"]
    ends = [e["node"] for e in rec.events if e["event"] == "node_end"]
    assert starts == ends == list(NODES)
    assert len(rec.saves) >= 2 * len(NODES)


# ---------------------------------------------------------------------------- early failures
def test_baseline_build_failure_stops_before_scan_and_plan(tmp_path):
    state, paths = make_run(tmp_path)
    orig = paths.original_zip.read_bytes()
    comps = one_fix_components(maven=FakeMaven(outcomes=[False]))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "baseline_build_failed"
    assert comps.scan.calls == [] and comps.plan.calls == []
    assert paths.report_md.is_file()
    assert paths.original_zip.read_bytes() == orig
    assert out.delivery is None and not paths.delivery_zip.exists()


def test_not_ready(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(maven=FakeMaven(readiness=["JDK 21 not found"]))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "not_ready" and "JDK 21" in out.message
    assert comps.maven.package_calls == [] and comps.scan.calls == []
    assert paths.report_md.is_file()


def test_boundary_refusal(tmp_path):
    state, paths = make_run(tmp_path)
    b = [BoundaryFinding(code="project_maven_config", path=".mvn/maven.config", action="refuse"),
         BoundaryFinding(code="maven_wrapper", path="mvnw", action="ignore")]
    comps = one_fix_components(intake=FakeIntakeFns(boundary=b))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "execution_boundary_refused"
    assert len(out.boundary) == 2 and comps.maven.package_calls == []


def test_boundary_ignore_is_recorded_and_the_run_goes_on(tmp_path):
    state, paths = make_run(tmp_path)
    b = [BoundaryFinding(code="maven_wrapper", path="mvnw", action="ignore")]
    out, _ = run(state, paths, one_fix_components(intake=FakeIntakeFns(boundary=b)))
    assert out.status == SUCCEEDED and out.boundary[0].code == "maven_wrapper"


def test_incomplete_baseline_scan_is_never_zero_findings(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(scan=FakeScanner({"baseline": [scan_result(complete=False, reasons=["page_missing"])]}))
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "scan_incomplete"
    assert comps.plan.calls == [] and out.delivery is None
    assert paths.report_md.is_file()


# ---------------------------------------------------------------------------- upload uncertain / stop
def test_upload_uncertain_waits_and_never_reuploads(tmp_path):
    state, paths = make_run(tmp_path)
    scanner = FakeScanner({"baseline": [UNCERTAIN, scan_result()]})
    comps = make_components(scan=scanner)
    out, _ = run(state, paths, comps)
    assert out.status == AWAITING_CONFIRMATION and out.reason == "upload_uncertain"
    assert out.next_node == "baseline_scan" and "baseline" in out.pending_upload
    assert scanner.uploads == 1 and not paths.report_md.exists()
    # resuming without a human decision does not upload again
    out, _ = run(out, paths, comps)
    assert out.status == AWAITING_CONFIRMATION and len(scanner.calls) == 1
    # a human found the analysis in IVAS and supplied its receipt: continue it, zero uploads
    out.pending_upload.clear()
    out.pending_receipt["baseline"] = UploadReceipt(project_id="p", analysis_id="a",
                                                    zip_sha256=state.intake.zip_sha256).to_dict()
    out, _ = run(out, paths, comps)
    assert out.status == SUCCEEDED
    assert scanner.uploads == 1 and scanner.calls[-1].receipt.analysis_id == "a"
    assert out.pending_upload == {} and out.pending_receipt == {}


def test_upload_uncertain_cleared_by_a_human_uploads_once_more(tmp_path):
    state, paths = make_run(tmp_path)
    scanner = FakeScanner({"baseline": [UNCERTAIN, scan_result()]})
    out, _ = run(state, paths, make_components(scan=scanner))
    out.pending_upload.clear()                 # the human confirmed nothing reached IVAS
    out, _ = run(out, paths, make_components(scan=scanner))
    assert out.status == SUCCEEDED and scanner.uploads == 2


def test_stop_before_a_node_then_resume_continues_there(tmp_path):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    maven = FakeMaven(on_package=lambda project: setattr(rec, "stop_flag", True))
    comps = one_fix_components(maven=maven)
    out, _ = run(state, paths, comps, rec)
    assert out.status == STOPPED and out.reason == "stop_requested" and out.next_node == "baseline_scan"
    assert comps.scan.calls == [] and not paths.report_md.exists()
    assert graph.can_resume(out)
    rec.stop_flag = False
    maven.on_package = None
    out, _ = run(out, paths, comps, rec)
    assert out.status == SUCCEEDED
    assert len(maven.package_calls) == 2       # baseline once + candidate once: the baseline was not rebuilt


def test_stop_requested_flag_on_state_halts(tmp_path):
    state, paths = make_run(tmp_path)
    state.stop_requested = True
    comps = one_fix_components()
    out, _ = run(state, paths, comps)
    assert out.status == STOPPED and out.next_node == "baseline_build" and comps.maven.package_calls == []


def test_stop_during_scan_keeps_the_receipt_and_never_says_cancelled(tmp_path):
    state, paths = make_run(tmp_path)
    scanner = FakeScanner({"baseline": [STOP, scan_result()]})
    comps = make_components(scan=scanner)
    out, rec = run(state, paths, comps)
    assert out.status == STOPPED and out.next_node == "baseline_scan"
    assert out.pending_receipt["baseline"]["analysis_id"] == "anl-baseline-1" and out.pending_upload == {}
    text = json.dumps(out.to_dict()) + json.dumps(rec.events)
    assert "cancel" not in text.lower()
    out, _ = run(out, paths, comps)
    assert out.status == SUCCEEDED and scanner.uploads == 1
    assert scanner.calls[-1].receipt.analysis_id == "anl-baseline-1"


# ---------------------------------------------------------------------------- approvals / plans
def _two_pending_plan():
    return RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[
        proposal(1, decision=D_PENDING, risks=["major"]),
        proposal(2, ga="example:other", old="2.0", new="3.0", decision=D_PENDING, risks=["major"])])


def _decide(paths, decisions):
    data = read_json(paths.plan(1))
    for p in data["proposals"]:
        p["decision"] = decisions[p["id"]]
    write_json_atomic(paths.plan(1), data)


def test_approval_pending_then_resume_applies_approved_only(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding(), finding("example:other", "2.0"))],
                                              "final-r1": [scan_result(finding("example:other", "2.0"))]}),
                            plan=ScriptedPlanner([_two_pending_plan()]))
    out, _ = run(state, paths, comps, cfg=make_cfg(max_rounds=1))
    assert out.status == AWAITING_APPROVAL and out.reason == "pending_approval" and out.next_node == "plan"
    assert comps.apply.applied_calls == [] and not paths.report_md.exists()
    _decide(paths, {"p-1-1": D_APPROVED, "p-1-2": D_REJECTED})
    out, _ = run(out, paths, comps, cfg=make_cfg(max_rounds=1))
    assert len(comps.plan.calls) == 1          # the decided plan was reused, not re-planned
    assert comps.apply.applied_calls == [["p-1-1"]]
    pom = (paths.accepted / "pom.xml").read_text()
    assert "<version>1.1</version>" in pom and "<version>2.0</version>" in pom
    assert out.status == INCOMPLETE and out.reason == "rejected_proposals"
    assert "residual_findings" in out.reasons


def test_all_rejected_applies_nothing(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}),
                            plan=ScriptedPlanner([_two_pending_plan()]))
    out, _ = run(state, paths, comps)
    _decide(paths, {"p-1-1": D_REJECTED, "p-1-2": D_REJECTED})
    before = tree_hash(paths)
    out, _ = run(out, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "rejected_proposals"
    assert comps.apply.applied_calls == [] and tree_hash(paths) == before
    assert len(comps.maven.package_calls) == 1
    assert out.delivery.sha256 == state.intake.zip_sha256       # nothing changed: the original bytes
    assert out.counts["proposals_rejected"] == 2


def test_stale_plan_policy_hash_is_replanned(tmp_path):
    state, paths = make_run(tmp_path)
    planner = ScriptedPlanner([_two_pending_plan(), RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                                              proposals=[proposal()])])
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}), plan=planner)
    out, _ = run(state, paths, comps)
    assert out.status == AWAITING_APPROVAL
    _decide(paths, {"p-1-1": D_APPROVED, "p-1-2": D_REJECTED})
    out.policy_hash = planner.policy_hash = "policy-hash-2"       # the server policy changed meanwhile
    out, _ = run(out, paths, comps)
    assert len(planner.calls) == 2
    assert ("example:other", "3.0") in planner.calls[1]["rejected"]
    assert (paths.round_dir(1) / "plan.stale.json").is_file()
    assert out.status == SUCCEEDED


def test_stale_plan_tree_hash_is_replanned(tmp_path):
    state, paths = make_run(tmp_path)
    planner = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="",
                                         proposals=[proposal(decision=D_PENDING)])])
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}), plan=planner)
    out, _ = run(state, paths, comps)
    _decide(paths, {"p-1-1": D_APPROVED})
    (paths.accepted / "src/main/java/App.java").write_text("class App { }\n")
    out, _ = run(out, paths, comps)
    assert len(planner.calls) == 2
    # the approval of the stale plan is not used: the re-plan (default: no proposal) leaves the finding
    assert comps.apply.applied_calls == []
    assert out.status == INCOMPLETE and out.reason == "residual_findings"


# ---------------------------------------------------------------------------- candidate failures
def test_candidate_build_failure_discards_the_candidate(tmp_path):
    state, paths = make_run(tmp_path)
    before = tree_hash(paths)
    comps = one_fix_components(maven=FakeMaven(outcomes=[True, False]))
    out, _ = run(state, paths, comps, cfg=make_cfg(failed_candidate="discard"))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is None and not paths.delivery_zip.exists()
    assert tree_hash(paths) == before
    assert "1.1" in paths.patch(1).read_text()
    assert paths.report_md.is_file()
    assert [c.label for c in comps.scan.calls] == ["baseline"]


def _zip_files(path):
    import zipfile
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.mark.parametrize("failure", ["failure", "timeout"])
def test_candidate_build_failure_is_delivered_by_default(tmp_path, failure):
    state, paths = make_run(tmp_path)
    before = tree_hash(paths)
    orig = paths.original_zip.read_bytes()
    bad = BuildOutcome(ok=False, exit_code=1, timed_out=failure == "timeout", summary="[ERROR] COMPILATION ERROR")
    comps = one_fix_components(maven=FakeMaven(outcomes=[True, bad]))
    out, rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "candidate_build_failed" and out.reasons == ["candidate_build_failed"]
    d = out.delivery
    assert d is not None and d.build_ok is False and d.scanned is False and d.round == 1
    assert paths.delivery_zip.is_file() and file_sha(paths.delivery_zip) == d.sha256
    files = _zip_files(paths.delivery_zip)
    assert set(files) == {"pom.xml", "src/main/java/App.java"}
    assert b"<version>1.1</version>" in files["pom.xml"] and b"<version>1.0</version>" not in files["pom.xml"]
    assert read_json(paths.delivery_json)["build_ok"] is False
    # accepted/ stays the last committed state (the original here); the original ZIP is byte-identical
    assert tree_hash(paths) == before and "<version>1.0</version>" in (paths.accepted / "pom.xml").read_text()
    assert paths.original_zip.read_bytes() == orig
    # no rescan of a failed candidate
    assert [c.label for c in comps.scan.calls] == ["baseline"] and out.final_scan == ""
    assert "1.1" in paths.patch(1).read_text() and paths.build_log(1).is_file()
    assert "FAILED" in out.message and "not rescanned" in out.message and "source.zip" in out.message
    assert any(e.get("event") == "failed_candidate_delivered" for e in rec.events)
    assert not out.rounds[0].committed


def _two_round_components(fail_round_two=True):
    scan = FakeScanner({"baseline": [scan_result(finding(), finding("example:other", "2.0"))],
                        "final-r1": [scan_result(finding("example:other", "2.0"))],
                        "final-r2": [scan_result()]})
    plan = ScriptedPlanner([
        RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(1)]),
        RoundPlan(round=2, source_tree_hash="", policy_hash="",
                  proposals=[proposal(1, round=2, ga="example:other", old="2.0", new="2.1")])])
    return make_components(scan=scan, plan=plan, maven=FakeMaven(outcomes=[True, True, not fail_round_two]))


def test_round_two_failure_include_keeps_the_verified_round_one_zip(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components()
    out, rec = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery.round == 2 and not out.delivery.build_ok and not out.delivery.scanned
    pom = _zip_files(paths.delivery_zip)["pom.xml"]
    assert b"<version>2.1</version>" in pom and b"<version>1.1</version>" in pom
    verified = graph.verified_zip_path(paths, 1)
    assert verified.name == "result-r1-verified.zip" and graph.find_verified_zip(paths) == verified
    final_r1 = ScanResult.from_dict(read_json(paths.scan("final-r1")))
    assert file_sha(verified) == final_r1.zip_sha256
    assert read_json(verified.with_suffix(".json"))["round"] == 1
    assert b"<version>2.0</version>" in _zip_files(verified)["pom.xml"]
    # accepted/ is round 1's committed state, not the failed round 2
    acc = (paths.accepted / "pom.xml").read_text()
    assert "<version>1.1</version>" in acc and "<version>2.0</version>" in acc
    assert out.final_scan == "scans/final-r1.json"
    assert [c.label for c in comps.scan.calls] == ["baseline", "final-r1"]
    kept = [e for e in rec.events if e.get("event") == "verified_delivery_kept"]
    assert kept and kept[0]["file"] == "delivery/result-r1-verified.zip"
    assert "delivery/result-r1-verified.zip" in out.message


def test_round_two_failure_discard_keeps_round_one_as_result(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    comps = _two_round_components()
    out, rec = run(state, paths, comps, cfg=make_cfg(failed_candidate="discard"))
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery.round == 1 and out.delivery.build_ok and out.delivery.scanned
    assert file_sha(paths.delivery_zip) == ScanResult.from_dict(read_json(paths.scan("final-r1"))).zip_sha256
    assert graph.find_verified_zip(paths) is None
    assert any(e.get("event") == "earlier_delivery_kept" for e in rec.events)


def test_text_only_violation_is_never_delivered_under_include(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(apply=FakeApply(text_violations=["x changed"]))
    out, _ = run(state, paths, comps, cfg=make_cfg(failed_candidate="include"))
    assert out.status == FAILED and out.delivery is None and not paths.delivery_zip.exists()


def test_succeeded_delivery_is_built_and_scanned(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components())
    assert out.status == SUCCEEDED and out.delivery.build_ok and out.delivery.scanned
    assert read_json(paths.delivery_json)["scanned"] is True


def test_zero_findings_shortcut_is_scanned(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, make_components())
    assert out.status == SUCCEEDED and out.delivery.scanned and out.delivery.build_ok


@pytest.mark.parametrize("flip", ["build_ok", "scanned_sha"])
def test_evaluate_requires_build_ok_and_scanned(tmp_path, flip):
    state, paths = make_run(tmp_path)
    out, rec = run(state, paths, one_fix_components())
    assert out.status == SUCCEEDED
    if flip == "build_ok":
        out.delivery.build_ok = False
        expected = "candidate_build_failed"
    else:
        out.delivery.sha256 = "0" * 64     # the final scan no longer covers exactly this delivery
        expected = "zip_hash_mismatch"
    h = graph._Holder(out, paths, make_cfg(), one_fix_components(), rec.save, rec.journal, rec.stop)
    assert graph._evaluate(h) == "report"
    assert out.status == INCOMPLETE and expected in out.reasons and not out.delivery.scanned


def test_text_only_violation(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(apply=FakeApply(text_violations=["src/main/java/App.java changed"]))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "text_only_violation"
    assert len(comps.maven.package_calls) == 1 and out.delivery is None


def test_resolution_mismatch(tmp_path):
    state, paths = make_run(tmp_path)
    before = tree_hash(paths)
    comps = one_fix_components(apply=FakeApply(resolution_lines=["info: 1 module", "example:lib resolved 1.0"]))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "resolution_mismatch"
    assert out.delivery is None and tree_hash(paths) == before
    assert out.rounds[0].verify_problems == ["info: 1 module", "example:lib resolved 1.0"]


def test_info_lines_are_notes_only(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components(apply=FakeApply(resolution_lines=["info: packaged lib-1.1.jar"])))
    assert out.status == SUCCEEDED and out.rounds[0].verify_problems == ["info: packaged lib-1.1.jar"]


def test_skipped_proposal_cannot_succeed(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[
        proposal(1), proposal(2, ga="example:other", old="2.0", new="2.1")])])
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}), plan=plan,
                            apply=FakeApply(skip={"p-1-2": "line moved"}))
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and "manual_review" in out.reasons
    assert any("p-1-2" in r.detail for r in out.residuals)


def test_rescan_zip_tampered_after_freeze_fails(tmp_path):
    state, paths = make_run(tmp_path)
    tamper = FakeIntakeFns(after_freeze=lambda z: z.write_bytes(z.read_bytes() + b"x"))
    comps = one_fix_components(intake=tamper)
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "zip_hash_mismatch"
    assert [c.label for c in comps.scan.calls] == ["baseline"]


def test_rescan_reports_another_sha_fails(tmp_path):
    state, paths = make_run(tmp_path)
    other = ScanResult(label="", zip_sha256="0" * 64, complete=True)
    comps = one_fix_components(scan=FakeScanner({"baseline": [scan_result(finding())], "final-r1": [other]}))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "zip_hash_mismatch"


def test_incomplete_rescan(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(scan=FakeScanner({"baseline": [scan_result(finding())],
                                                 "final-r1": [scan_result(complete=False, reasons=["analysis_timeout"])]}))
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "scan_incomplete"
    assert paths.delivery_zip.is_file() and out.delivery is not None


# ---------------------------------------------------------------------------- residuals / rounds
def test_residual_after_the_last_round(tmp_path):
    state, paths = make_run(tmp_path)
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()],
                                      residuals=[Residual(ga="example:vendored", version="0.9", origin="vendored",
                                                          reason="manual_review", detail="WEB-INF/lib jar")])])
    out, _ = run(state, paths, one_fix_components(plan=plan))
    assert out.status == INCOMPLETE and out.reasons == ["manual_review"]
    assert out.residuals[0].ga == "example:vendored"


def test_two_rounds(tmp_path):
    state, paths = make_run(tmp_path, {"pom.xml": POM2})
    scan = FakeScanner({"baseline": [scan_result(finding(), finding("example:other", "2.0"))],
                        "final-r1": [scan_result(finding("example:other", "2.0"))],
                        "final-r2": [scan_result()]})
    plan = ScriptedPlanner([
        RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal(1)]),
        RoundPlan(round=2, source_tree_hash="", policy_hash="",
                  proposals=[proposal(1, round=2, ga="example:other", old="2.0", new="2.1")])])
    comps = make_components(scan=scan, plan=plan)
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED, out.reasons
    assert [r.round for r in out.rounds] == [1, 2] and all(r.committed for r in out.rounds)
    assert out.delivery.round == 2
    # round 2's "before" model came from round 1's built candidate
    assert comps.maven.resolve_calls[2] == paths.candidate(1)
    assert comps.inventory.calls[1][0].identifier == "example:other"
    pom = (paths.accepted / "pom.xml").read_text()
    assert "<version>1.1</version>" in pom and "<version>2.1</version>" in pom


def test_max_rounds_exhausted(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(scan=FakeScanner({"baseline": [scan_result(finding())],
                                                 "final-r1": [scan_result(finding("example:lib", "1.1"))]}))
    out, _ = run(state, paths, comps, cfg=make_cfg(max_rounds=1))
    assert out.status == INCOMPLETE and out.reason == "residual_findings"
    assert "rounds_exhausted" in out.reasons and len(comps.plan.calls) == 1


def test_inventory_incomplete_is_manual_review(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(inventory=ScriptedInventory([Inventory(complete=False, notes=["module x unread"])]))
    out, _ = run(state, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "manual_review" and "module x unread" in out.message
    assert comps.plan.calls == []


# ---------------------------------------------------------------------------- internal errors
def test_exception_in_a_node_fails_with_a_report_and_no_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("IVAS_PASSWORD", "hunter2-secret-value")

    state, paths = make_run(tmp_path)

    def boom(project, findings, resolved, **_kw):
        raise RuntimeError(f"login failed for https://user:hunter2-secret-value@ivas.invalid at {paths.root}/x")

    out, _ = run(state, paths, one_fix_components(inventory=boom))
    assert out.status == FAILED and out.reason == "internal_error"
    assert out.message.startswith("RuntimeError")
    assert "hunter2" not in out.message and str(paths.root) not in out.message
    assert "ivas.invalid" in out.message and "hunter2" not in caplog.text
    report = paths.report_md.read_text()
    assert "hunter2" not in report and "internal_error" in report


def test_terminal_state_is_not_rerun(tmp_path):
    state, paths = make_run(tmp_path)
    comps = make_components()
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED
    out, rec = run(out, paths, comps)
    assert rec.saves == [] and comps.scan.uploads == 1


@pytest.mark.parametrize("node", ["apply", "build"])
def test_stop_inside_the_round_resumes_at_that_node(tmp_path, monkeypatch, node):
    state, paths = make_run(tmp_path)
    rec = Recorder()
    comps = one_fix_components()
    real = graph._NODE_FNS["plan"] if node == "apply" else graph._NODE_FNS["apply"]

    def stopper(h):
        nxt = real(h)
        rec.stop_flag = True
        return nxt

    monkeypatch.setitem(graph._NODE_FNS, "plan" if node == "apply" else "apply", stopper)
    out, _ = run(state, paths, comps, rec)
    monkeypatch.undo()
    assert out.status == STOPPED and out.next_node == node
    rec.stop_flag = False
    out, _ = run(out, paths, comps, rec)
    assert out.status == SUCCEEDED


def test_inventory_never_plans_from_a_missing_scan(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components()
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED
    # a resume that lands on inventory with an unreadable scan file: never "zero findings"
    out.status, out.next_node, out.reason = "INTERRUPTED", "inventory", ""
    (paths.root / out.final_scan).write_text("{broken", encoding="utf-8")
    out, _ = run(out, paths, comps)
    assert out.status == INCOMPLETE and out.reason == "scan_incomplete"


def test_baseline_scan_of_other_bytes_is_a_hash_mismatch(tmp_path):
    state, paths = make_run(tmp_path)
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(complete=False, reasons=["zip_hash_mismatch"])]}))
    out, _ = run(state, paths, comps)
    assert out.status == FAILED and out.reason == "zip_hash_mismatch"


def test_build_outcome_log_is_run_relative_and_summary_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("IVAS_PASSWORD", "hunter2-build-secret")
    state, paths = make_run(tmp_path)
    from sbmig.remediations.models import BuildOutcome
    bad = BuildOutcome(ok=False, exit_code=1, log=str(paths.baseline_log),
                       summary=f"[ERROR] https://u:hunter2-build-secret@nexus.invalid at {paths.root}/baseline/tree")
    out, _ = run(state, paths, one_fix_components(maven=FakeMaven(outcomes=[bad])))
    assert out.baseline_build.log == "baseline/build.log"
    assert "hunter2" not in out.baseline_build.summary and str(paths.root) not in out.baseline_build.summary
    assert "hunter2" not in paths.report_md.read_text() + paths.report_json.read_text()


# ---------------------------------------------------------------------------- IVAS action codes (ignore_actions)
def coded(cve, action, ga="example:lib", version="1.0"):
    f = finding(ga=ga, version=version, cve=cve)
    f.action = action
    return f


def test_ignored_action_codes_are_not_planned_and_not_remaining(tmp_path):
    state, paths = make_run(tmp_path)
    base = [coded("CVE-1", "NE"), coded("CVE-2", "pc ", ga="example:other"), coded("CVE-3", "IG", ga="example:third")]
    final = [coded("CVE-2", "PC", ga="example:other"), coded("CVE-3", "IG", ga="example:third")]
    scan = FakeScanner({"baseline": [scan_result(*base)], "final-r1": [scan_result(*final)]})
    comps = one_fix_components(scan=scan)
    out, _ = run(state, paths, comps, cfg=make_cfg(ignore_actions=["PC", "IG"]))
    assert out.status == SUCCEEDED, (out.reason, out.reasons)
    assert [f.id for f in comps.inventory.calls[0]] == ["CVE-1"]
    assert out.counts["baseline_findings"] == 1 and out.counts["baseline_ignored"] == 2
    assert out.counts["final_findings"] == 0 and out.counts["final_ignored"] == 2 and out.counts["fixed"] == 1
    # the scan files keep every finding
    assert len(ScanResult.from_dict(read_json(paths.root / out.final_scan)).findings) == 2
    md = paths.report_md.read_text(encoding="utf-8")
    assert "## 조치 대상에서 뺀 항목" in md and "| PC | 패치 완료 |" in md and "| IG | 조치 예외 |" in md
    rj = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert rj["ignore_actions"] == ["IG", "PC"] and len(rj["ignored_findings"]) == 2
    assert rj["final_scan"]["actionable"] == 0 and rj["final_scan"]["ignored"] == 2


def test_only_ignored_findings_deliver_the_original(tmp_path):
    state, paths = make_run(tmp_path)
    comps = one_fix_components(scan=FakeScanner({"baseline": [scan_result(coded("CVE-9", "IG"))]}))
    out, _ = run(state, paths, comps, cfg=make_cfg(ignore_actions=["PC", "IG"]))
    assert out.status == SUCCEEDED and comps.plan.calls == [] and comps.inventory.calls == []
    assert paths.delivery_zip.read_bytes() == paths.original_zip.read_bytes()


def test_unknown_or_empty_action_codes_stay_actionable_and_are_noted_once(tmp_path):
    state, paths = make_run(tmp_path)
    scan = FakeScanner({"baseline": [scan_result(coded("CVE-1", ""), coded("CVE-2", "ZZ", ga="example:other"))],
                        "final-r1": [scan_result(coded("CVE-2", "ZZ", ga="example:other"))]})
    out, rec = run(state, paths, one_fix_components(scan=scan), cfg=make_cfg(max_rounds=1))
    assert out.status == INCOMPLETE and "residual_findings" in out.reasons
    assert out.counts["final_findings"] == 1 and out.counts["final_ignored"] == 0
    noted = [e for e in rec.events if e["event"] == "ivas_action_unknown"]
    assert [e["codes"] for e in noted] == [["(empty)", "ZZ"]]
    assert "알 수 없는 조치 코드(조치 대상으로 다룸): ZZ" in paths.report_md.read_text(encoding="utf-8")


@pytest.mark.parametrize("explicit", [True, False])
def test_ignore_actions_empty_list_ignores_nothing(tmp_path, explicit):
    # the default (2026-11-11) is the empty list: PC / IG findings are planned like any other
    state, paths = make_run(tmp_path)
    scan = FakeScanner({"baseline": [scan_result(coded("CVE-1", "PC"))], "final-r1": [scan_result()]})
    comps = one_fix_components(scan=scan)
    out, _ = run(state, paths, comps, cfg=make_cfg(ignore_actions=[]) if explicit else None)
    assert out.status == SUCCEEDED and [f.id for f in comps.inventory.calls[0]] == ["CVE-1"]
    assert out.counts["baseline_findings"] == 1 and out.counts["baseline_ignored"] == 0


# ---------------------------------------------------------------------------- module scope (company run 2026-11-12)
def _scope_project():
    """Company layout: an aggregator root, modules inheriting an EXTERNAL role parent (not the root). app-domain pins
    the library with a literal; app-service / app-store get the same version from their parent's management."""
    def module(name, deps):
        return ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <parent>\n    <groupId>com.example.ext</groupId>\n"
                "    <artifactId>comp-jar</artifactId>\n    <version>4.0.0</version>\n    <relativePath/>\n  </parent>\n"
                f"  <groupId>com.example</groupId>\n  <artifactId>{name}</artifactId>\n  <version>1.0.0</version>\n"
                f"  <dependencies>\n{deps}  </dependencies>\n</project>\n")

    def dep(a, v=None):
        ver = f"      <version>{v}</version>\n" if v else ""
        return (f"    <dependency>\n      <groupId>com.example.logging</groupId>\n      <artifactId>{a}</artifactId>\n"
                f"{ver}    </dependency>\n")
    root = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>com.example</groupId>\n"
            "  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
            "  <modules>\n    <module>app-domain</module>\n    <module>app-service</module>\n"
            "    <module>app-store</module>\n  </modules>\n</project>\n")
    return {"pom.xml": root,
            "app-domain/pom.xml": module("app-domain", dep("log-api", "2.25.4")),
            "app-service/pom.xml": module("app-service", dep("log-api")),
            "app-store/pom.xml": module("app-store", dep("log-api"))}


def _scope_model(domain, service, store):
    from sbmig.remediations.models import ModuleResolution, ResolvedDep, ResolvedModel

    def mod(d, v):
        return ModuleResolution(dir=d, group_id="com.example", artifact_id=d, version="1.0.0",
                                dependencies=[ResolvedDep(group_id="com.example.logging", artifact_id="log-api",
                                                          version=v, direct=True)])
    return ResolvedModel(modules=[mod("app-domain", domain), mod("app-service", service), mod("app-store", store)])


def test_edit_that_does_not_govern_every_module_commits_and_leaves_the_rest_to_round_two(tmp_path):
    """Real inventory / planner / apply (incl. verify_resolution) over fake Maven + IVAS: round 1 edits app-domain's
    literal, the parent-managed modules stay at 2.25.4 → info lines, the round commits and is delivered; the rescan
    still reports the finding → round 2 inventories app-service / app-store only → no project text, no root inheritance
    → a manual_review residual; the run ends INCOMPLETE, never FAILED resolution_mismatch."""
    from sbmig.remediations import apply as apply_mod
    from sbmig.remediations import inventory as inventory_mod
    from sbmig.remediations import planner as planner_mod
    from sbmig.remediations.models import Finding

    from .fakes import FakeVersions

    ga = "com.example.logging:log-api"
    f = Finding(id="CVE-2026-0001", kind="library", identifier=ga, version="2.25.4", patched_version="2.25.5")
    state, paths = make_run(tmp_path, _scope_project())
    maven = FakeMaven(models=[_scope_model("2.25.4", "2.25.4", "2.25.4"),      # round 1 before
                              _scope_model("2.25.5", "2.25.4", "2.25.4"),      # round 1 after (candidate)
                              _scope_model("2.25.5", "2.25.4", "2.25.4")])     # round 2 before
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(f)], "final-r1": [scan_result(f)]}),
                            maven=maven, versions=FakeVersions({ga: ["2.25.4", "2.25.5"]}),
                            inventory=inventory_mod.build_inventory, plan=planner_mod.plan_round, apply=apply_mod)
    out, _ = run(state, paths, comps)

    assert out.status == INCOMPLETE, (out.reason, out.message)
    assert out.reason != "resolution_mismatch" and "resolution_mismatch" not in out.reasons
    plan1 = RoundPlan.from_dict(read_json(paths.plan(1)))
    (p,) = plan1.proposals
    assert (p.id, p.from_version, p.to_version, p.decision) == ("p-1-1", "2.25.4", "2.25.5", "auto")
    assert p.modules == ["app-domain"] and p.also_modules == ["app-service", "app-store"]
    first = out.rounds[0]
    assert first.committed and first.applied == ["p-1-1"]
    assert first.verify_problems and all(x.startswith("info:") for x in first.verify_problems)
    assert sum("not governed by the edited version text" in x for x in first.verify_problems) == 2
    # round 1 was delivered (the scanned bytes of the committed tree)
    assert out.delivery is not None and out.delivery.round == 1 and paths.delivery_zip.is_file()
    assert "<version>2.25.5</version>" in (paths.accepted / "app-domain/pom.xml").read_text()
    # round 2: only the two parent-managed modules remain; they do not inherit from the root → residual, no proposal
    inv2 = Inventory.from_dict(read_json(paths.round_dir(2) / "inventory.json"))
    (item,) = inv2.items
    assert item.origin == "parent_managed" and item.modules == ["app-service", "app-store"] and item.also_modules == []
    plan2 = RoundPlan.from_dict(read_json(paths.plan(2)))
    assert plan2.proposals == [] and [r.reason for r in plan2.residuals] == ["manual_review"]
    assert "do not inherit from the root pom.xml" in plan2.residuals[0].detail
    assert {"manual_review", "residual_findings"} <= set(out.reasons) and out.status != FAILED


# ---------------------------------------------------------------------------- in-house artifacts
INH = "com.example.inhouse:inhouse-core"


def test_findings_all_through_an_inhouse_artifact_end_incomplete_with_the_count(tmp_path):
    from sbmig.core.config import ArtifactsConfig
    from sbmig.remediations import planner as planner_mod
    from sbmig.remediations.models import InventoryItem

    cfg = make_cfg()
    cfg.artifacts = ArtifactsConfig(parent={"group_id": "com.example.inhouse", "artifact_id": "base-parent"},
                                    mandatory=[{"group_id": "com.example.inhouse", "artifact_id": "inhouse-core"}])
    state, paths = make_run(tmp_path, cfg=cfg)
    item = InventoryItem(ga="example:lib", version="1.0", origin="transitive", finding_ids=["CVE-2026-0001"],
                         patched_versions=["1.1"], modules=["app-domain", "app-web"], via=[f"{INH}:4.0.0"],
                         inhouse=[INH], inhouse_modules=["app-domain", "app-web"])
    inv = ScriptedInventory([Inventory(items=[item])])
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(finding())]}), inventory=inv,
                            plan=planner_mod.plan_round)
    out, _ = run(state, paths, comps, cfg=cfg)
    assert inv.inhouse[0].get(INH) == "mandatory" and inv.inhouse[0].get("com.example.inhouse:base-parent") == "parents"
    assert out.status == INCOMPLETE and "inhouse_artifact" in out.reasons
    plan = RoundPlan.from_dict(read_json(paths.round_dir(1) / "plan.json"))
    assert plan.proposals == []
    (r,) = [x for x in out.residuals if x.reason == "inhouse_artifact"]
    assert r.modules == ["app-domain", "app-web"] and r.inhouse == [INH] and r.via == [f"{INH}:4.0.0"]
    assert out.counts["residual_inhouse"] == 1


def test_merge_residuals_unions_the_report_facts():
    st = SimpleNamespace(residuals=[Residual(ga="g:a", version="1", origin="transitive", reason="inhouse_artifact",
                                             modules=["app-web"], via=[], inhouse=["x:y"])])
    graph._merge_residuals(st, [Residual(ga="g:a", version="1", origin="transitive", reason="inhouse_artifact",
                                         modules=["app-domain"], via=["x:y:1"], inhouse=["x:z"]),
                                Residual(ga="g:b", version="1", origin="transitive", reason="manual_review")])
    a, b = st.residuals
    assert a.modules == ["app-domain", "app-web"] and a.inhouse == ["x:y", "x:z"] and a.via == ["x:y:1"]
    assert b.ga == "g:b" and b.modules == []


def test_counts_have_residual_inhouse_zero_by_default(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, one_fix_components())
    assert out.counts["residual_inhouse"] == 0


def test_old_state_json_without_residual_facts_loads():
    from sbmig.remediations.models import RemediationState
    d = {"id": "rem-1", "residuals": [{"ga": "g:a", "version": "1", "origin": "transitive", "reason": "manual_review"}]}
    r = RemediationState.from_dict(d).residuals[0]
    assert (r.modules, r.via, r.inhouse) == ([], [], [])
