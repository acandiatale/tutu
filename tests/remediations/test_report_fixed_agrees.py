"""The Korean remediation report and `state.counts` agree on what was fixed (review finding M1).

A vulnerability counts as fixed (요약, 조치 완료, 해결된 취약점) only when a complete final scan no longer reports it
for that library, whatever version (or spelling of the version) the final scan reports it at."""
from __future__ import annotations

from sbmig.remediations import graph
from sbmig.remediations.models import RoundPlan

from .fakes import FakeScanner, Recorder, ScriptedPlanner, finding, make_cfg, make_components, make_run, proposal, scan_result


def _run(tmp_path, final, *, base=None, cfg=None):
    state, paths = make_run(tmp_path)
    comps = make_components(
        scan=FakeScanner({"baseline": [scan_result(*(base or [finding()]))], "final-r1": [final]}),
        plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])]))
    rec = Recorder()
    cfg = cfg or make_cfg()
    cfg.remediation.max_rounds = 1
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    return out, paths.report_md.read_text(encoding="utf-8")


def _section(md: str, head: str) -> str:
    return md.split(f"## {head}", 1)[1].split("\n## ", 1)[0] if f"## {head}" in md else ""


def _summary_fixed(md: str) -> str:
    return next(ln for ln in _section(md, "요약").splitlines() if ln.startswith("| 조치 완료 |"))


def test_still_reported_at_the_new_version_is_not_fixed(tmp_path):
    # lib 1.0 -> 1.1 applied, the complete rescan still reports the same CVE for the library at 1.1
    out, md = _run(tmp_path, scan_result(finding(version="1.1")))
    assert out.counts["fixed"] == 0
    assert _summary_fixed(md) == "| 조치 완료 | 0건 | - |", _summary_fixed(md)
    done = _section(md, "조치 완료")
    assert "「해결된 취약점」 칸이 「-」인 변경은 적용했지만 재검사에서 같은 취약점이 아직 보고됩니다." in done
    assert done.startswith(" — 0건"), done[:40]
    row = next(ln for ln in done.splitlines() if "`example:lib`" in ln)
    assert "CVE-2026-0001" not in row, row               # 해결된 취약점 lists nothing for this change
    assert "모두 조치했고" not in md


def test_another_spelling_of_the_version_is_the_same_finding(tmp_path):
    out, md = _run(tmp_path, scan_result(finding(ga="Example:Lib", version="1.0.RELEASE")))
    assert out.counts["fixed"] == 0
    assert _summary_fixed(md).startswith("| 조치 완료 | 0건 |")


def test_gone_from_the_rescan_is_fixed_in_both(tmp_path):
    out, md = _run(tmp_path, scan_result())
    assert out.counts["fixed"] == 1
    assert _summary_fixed(md) == "| 조치 완료 | 1건 | 1개 |"
    row = next(ln for ln in _section(md, "조치 완료").splitlines() if "`example:lib`" in ln)
    assert "CVE-2026-0001" in row and "아직 보고됩니다" not in md


def test_one_of_two_cves_left_counts_one_and_lists_one(tmp_path):
    base = [finding(cve="CVE-2026-0001"), finding(cve="CVE-2026-0002")]
    state_plan = proposal()
    state_plan.finding_ids = ["CVE-2026-0001", "CVE-2026-0002"]
    state, paths = make_run(tmp_path)
    comps = make_components(
        scan=FakeScanner({"baseline": [scan_result(*base)],
                          "final-r1": [scan_result(finding(version="1.1", cve="CVE-2026-0002"))]}),
        plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[state_plan])]))
    rec = Recorder()
    cfg = make_cfg()
    cfg.remediation.max_rounds = 1
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    md = paths.report_md.read_text(encoding="utf-8")
    assert out.counts["fixed"] == 1
    assert _summary_fixed(md).startswith("| 조치 완료 | 1건 |")
    row = next(ln for ln in _section(md, "조치 완료").splitlines() if "`example:lib`" in ln)
    assert "CVE-2026-0001" in row and "CVE-2026-0002" not in row
