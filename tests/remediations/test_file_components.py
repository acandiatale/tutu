"""IVAS file components (`remediation.file_components`): `report` (default) lists them apart, `count` keeps the old rule.

Graph runs over fakes (no IVAS, Maven or network): the verdict, the counts, the inventory input, resumed runs, old
state files, and the Korean report section for the cases the owner reads.
"""
from __future__ import annotations

import json

from sbmig.remediations import graph
from sbmig.remediations import report as report_mod
from sbmig.remediations.ivas import R_FILE_UNREAD, count_view, files_reported, split_files
from sbmig.remediations.models import (
    INCOMPLETE,
    SUCCEEDED,
    Finding,
    RemediationState,
    Residual,
    RoundPlan,
    ScanResult,
    read_json,
    write_json_atomic,
)

from .fakes import (
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

JS = "src/main/webapp/js/jquery-1.8.1.min.js"


def js_finding(*, unread: bool = False, files: list[str] | None = None, cve: str = "CVE-2020-11022",
               kind: str = "file_component", action: str = "NE") -> Finding:
    return Finding(id=cve, kind=kind, identifier="jquery", version="1.8.1", patched_version="3.5.0",
                   locations=[] if unread else list(files or [JS]), component_id="11560|934982", score=6.1,
                   action=action, files_unread=unread)


def run(state, paths, comps, cfg=None, rec=None):
    rec = rec or Recorder()
    out = graph.run_remediation(state, paths, cfg or make_cfg(), comps, save=rec.save, journal=rec.journal,
                                stop=rec.stop)
    return out, rec


def lib_and_js(final_files=(), *, baseline_extra=(), file_complete=True):
    """Baseline: one fixable library finding + the given file findings; plan: 1.0 → 1.1; rescan: the file findings."""
    base = scan_result(finding(), *baseline_extra)
    base.file_complete = file_complete
    fin = scan_result(*final_files)
    fin.file_complete = file_complete
    scan = FakeScanner({"baseline": [base], "final-r1": [fin]})
    plan = ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])])
    return make_components(scan=scan, plan=plan)


def md(paths) -> str:
    return paths.report_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- helpers in ivas.py
def test_split_files_and_count_view():
    lib, js = finding(), js_finding()
    assert files_reported(None) and files_reported("report") and not files_reported("count")
    assert split_files([lib, js], "report") == ([lib], [js])
    assert split_files([lib, js], "count") == ([lib, js], [])
    s = ScanResult(label="b", zip_sha256="x", complete=True, findings=[lib, js_finding(unread=True)])
    assert count_view(s, "report") is s
    v = count_view(s, "count")
    assert not v.complete and v.incomplete_reasons == [R_FILE_UNREAD] and s.complete
    s2 = ScanResult(label="b", zip_sha256="x", complete=True, findings=[lib], file_complete=False)
    assert not count_view(s2, "count").complete


def test_an_old_scan_file_loads_with_the_new_fields_defaulted():
    old = {"label": "baseline", "zip_sha256": "x", "complete": True, "findings": [
        {"id": "CVE-1", "kind": "file_component", "identifier": "jquery", "version": "1.8.1", "locations": [JS]}]}
    s = ScanResult.from_dict(old)
    assert s.file_complete and not s.findings[0].files_unread
    assert count_view(s, "count") is s


# ---------------------------------------------------------------------------- report (default)
def test_default_mode_keeps_file_findings_out_of_inventory_verdict_and_counts(tmp_path):
    state, paths = make_run(tmp_path)
    comps = lib_and_js(final_files=[js_finding()], baseline_extra=[js_finding()])
    out, rec = run(state, paths, comps)
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert [f.kind for f in comps.inventory.calls[0]] == ["library"]
    assert out.residuals == []
    c = out.counts
    assert c["baseline_findings"] == 1 and c["final_findings"] == 0 and c["fixed"] == 1
    assert c["baseline_file_findings"] == 1 and c["final_file_findings"] == 1
    assert len(comps.plan.calls) == 1                                       # no second round for a file finding
    ev = [e for e in rec.events if e.get("event") == "file_findings_reported"]
    assert [(e["label"], e["findings"], e["unread"], e["file_complete"]) for e in ev] == [
        ("baseline", 1, 0, True), ("final-r1", 1, 0, True)]
    data = read_json(paths.report_json)
    assert data["file_components"] == "report" and len(data["file_findings"]) == 1
    assert data["file_findings"][0]["identifier"] == "jquery"


def test_only_file_findings_deliver_the_original_and_succeed(tmp_path):
    state, paths = make_run(tmp_path)
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(js_finding())]}))
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED and out.delivery.round == 0 and comps.plan.calls == []
    assert out.counts["baseline_findings"] == 0 and out.counts["baseline_file_findings"] == 1


def test_a_file_finding_with_an_ignored_code_stays_in_the_file_group(tmp_path):
    state, paths = make_run(tmp_path)
    comps = make_components(scan=FakeScanner({"baseline": [scan_result(js_finding(action="IG"))]}))
    out, _ = run(state, paths, comps, cfg=make_cfg(ignore_actions=["IG"]))
    assert out.status == SUCCEEDED
    assert out.counts["baseline_file_findings"] == 1 and out.counts["baseline_ignored"] == 0
    text = md(paths)
    assert "조치 대상에서 제외한 항목" not in text and "## 프로젝트 파일에서 찾은 취약점" in text


# ---------------------------------------------------------------------------- count (the old rule)
def test_count_mode_counts_file_findings_as_before(tmp_path):
    state, paths = make_run(tmp_path)
    cfg = make_cfg(file_components="count")
    comps = lib_and_js(final_files=[js_finding()], baseline_extra=[js_finding()])
    out, _ = run(state, paths, comps, cfg=cfg)
    assert out.status == INCOMPLETE and "residual_findings" in out.reasons
    assert {f.kind for f in comps.inventory.calls[0]} == {"library", "file_component"}
    assert out.counts["baseline_findings"] == 2 and out.counts["final_findings"] == 1
    assert "baseline_file_findings" not in out.counts and "final_file_findings" not in out.counts
    text = md(paths)
    assert "## 프로젝트 파일에서 찾은 취약점" not in text and "따로 적었" not in text
    assert "file_findings" not in read_json(paths.report_json)


def test_count_never_trusts_a_report_scan_whose_file_part_was_skipped(tmp_path):
    """A run planned under `report` and resumed under `count`: the stored baseline has unread matched files."""
    state, paths = make_run(tmp_path)
    comps = lib_and_js(final_files=[js_finding(unread=True)], baseline_extra=[js_finding(unread=True)])
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED
    out.status, out.next_node, out.reason = "INTERRUPTED", "evaluate", ""
    out, _ = run(out, paths, comps, cfg=make_cfg(file_components="count"))
    assert out.status == INCOMPLETE and "scan_incomplete" in out.reasons
    assert "baseline_findings" not in out.counts                            # an incomplete scan has no count
    # the scan files on disk are unchanged
    assert read_json(paths.root / out.baseline_scan)["complete"] is True


def test_count_plans_nothing_from_a_report_scan_whose_file_lists_were_not_read(tmp_path):
    state, paths = make_run(tmp_path)
    comps = lib_and_js(file_complete=False)
    out, _ = run(state, paths, comps)
    assert out.status == SUCCEEDED
    out.status, out.next_node, out.reason = "INTERRUPTED", "inventory", ""
    out.final_scan = ""
    out, _ = run(out, paths, comps, cfg=make_cfg(file_components="count"))
    assert out.status == INCOMPLETE and out.reason == "scan_incomplete"


# ---------------------------------------------------------------------------- old data
def test_an_old_state_with_a_file_component_residual_is_evaluated_with_the_current_setting(tmp_path):
    """A state written before the setting: the file component was a `vendored` residual inside the counts."""
    state, paths = make_run(tmp_path)
    comps = lib_and_js(final_files=[js_finding()], baseline_extra=[js_finding()])
    out, _ = run(state, paths, comps, cfg=make_cfg(file_components="count"))
    assert out.status == INCOMPLETE
    old = out.to_dict()
    old["residuals"] = [Residual(ga="jquery", version="1.8.1", origin="vendored", reason="manual_review",
                                 detail="file component in the tree; not changed automatically",
                                 finding_ids=["CVE-2020-11022"]).to_dict()]
    old["counts"] = {"baseline_findings": 2, "final_findings": 1, "fixed": 1, "residual": 1}
    for scan_rel in (old["baseline_scan"], old["final_scan"]):
        data = read_json(paths.root / scan_rel)
        data.pop("file_complete", None)
        for f in data["findings"]:
            f.pop("files_unread", None)
        write_json_atomic(paths.root / scan_rel, data)
    old["status"], old["next_node"], old["reason"], old["reasons"] = "INTERRUPTED", "evaluate", "", []
    st = RemediationState.from_dict(json.loads(json.dumps(old)))
    out, _ = run(st, paths, comps)                                          # default: report
    assert out.status == SUCCEEDED, out.reasons
    assert out.counts["final_findings"] == 0 and out.counts["final_file_findings"] == 1
    assert out.counts["residual"] == 0
    text = md(paths)
    assert "## 직접 확인이 필요한 취약점" not in text and "## 프로젝트 파일에서 찾은 취약점 — 1건" in text


# ---------------------------------------------------------------------------- the Korean report
def test_report_a_one_fixed_library_and_one_js_file(tmp_path):
    state, paths = make_run(tmp_path)
    run(state, paths, lib_and_js(final_files=[js_finding()], baseline_extra=[js_finding()]))
    text = md(paths)
    head = text.split("## 요약")[0]
    assert "취약점 1건을 모두 조치했고" in head and "jquery" not in head
    summary = text.split("## 요약")[1].split("## 해야 할 일")[0]
    assert "| 조치 전 | 1건 | 1개 |" in summary
    assert ("프로젝트 파일에서 찾은 취약점 1건은 위 개수에 넣지 않고 아래 「프로젝트 파일에서 찾은 취약점」에 따로 "
            "적었습니다.") in summary
    assert "| 조치 전 프로젝트 파일 취약점 (따로 적음) | 1 |" in text
    sec = text.split("## 프로젝트 파일에서 찾은 취약점 — 1건")[1].split("\n## ")[0]
    assert "프로젝트 안에 들어 있는 파일" in sec and "pom.xml의 라이브러리 버전만" in sec
    assert "| `jquery` | 1.8.1 | 3.5.0 |" in sec and f"`{JS}`" in sec
    assert "읽지 못했습니다" not in sec
    # the section comes after the library sections and before 변경하지 않은 것
    assert text.index("## 조치 완료") < text.index("## 프로젝트 파일에서 찾은 취약점") < text.index("## 변경하지 않은 것")


def test_report_b_matched_files_unreadable(tmp_path):
    state, paths = make_run(tmp_path)
    run(state, paths, lib_and_js(final_files=[js_finding(unread=True)], baseline_extra=[js_finding(unread=True)]))
    sec = md(paths).split("## 프로젝트 파일에서 찾은 취약점 — 1건")[1].split("\n## ")[0]
    assert "파일 위치를 읽지 못했습니다" in sec


def test_report_c_file_lists_unreadable(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, lib_and_js(file_complete=False))
    text = md(paths)
    sec = text.split("## 프로젝트 파일에서 찾은 취약점")[1].split("\n## ")[0]
    assert "파일에서 찾은 취약점 목록을 IVAS에서 읽지 못했습니다" in sec and "|" not in sec
    assert "따로 적었" not in text                                              # nothing counted, no summary line
    assert "취약점 1건을 모두 조치했고" in text
    assert "프로젝트 파일 쪽 목록은 IVAS에서 끝까지 읽지 못했습니다" in text
    assert "baseline_file_findings" not in out.counts and out.counts["file_lists_unread"] == 2


def test_report_d_only_file_findings(tmp_path):
    state, paths = make_run(tmp_path)
    run(state, paths, make_components(scan=FakeScanner({"baseline": [scan_result(js_finding())]})))
    text = md(paths)
    head = text.split("## 요약")[0]
    assert "조치 전 검사에서 조치할 라이브러리 취약점이 없었습니다." in head
    assert "## 프로젝트 파일에서 찾은 취약점 — 1건" in text


def test_report_long_file_lists_are_cut_and_components_grouped(tmp_path):
    files = [f"web/js/lib{i}.js" for i in range(7)]
    a = js_finding(files=files)
    b = js_finding(files=files, cve="CVE-2020-11023", kind="file_component_potential")
    state, paths = make_run(tmp_path)
    run(state, paths, make_components(scan=FakeScanner({"baseline": [scan_result(a, b)]})))
    sec = md(paths).split("## 프로젝트 파일에서 찾은 취약점 — 2건")[1].split("\n## ")[0]
    rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and "jquery" in ln]
    assert len(rows) == 1 and "외 4개" in rows[0] and "CVE-2020-11022" in rows[0] and "CVE-2020-11023" in rows[0]


def test_no_section_without_file_findings(tmp_path):
    state, paths = make_run(tmp_path)
    run(state, paths, lib_and_js())
    text = md(paths)
    assert "프로젝트 파일에서 찾은 취약점" not in text


def test_an_already_rendered_report_is_not_rewritten(tmp_path):
    state, paths = make_run(tmp_path)
    out, _ = run(state, paths, lib_and_js(final_files=[js_finding()], baseline_extra=[js_finding()]))
    paths.report_md.write_text("old report\n", encoding="utf-8")
    run(out, paths, make_components())                                      # terminal: nothing runs again
    assert paths.report_md.read_text(encoding="utf-8") == "old report\n"
    assert report_mod.build_report_md(out, paths).count("## 프로젝트 파일에서 찾은 취약점") == 1
