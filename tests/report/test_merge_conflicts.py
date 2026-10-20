"""Round 7b: multi-module `merge` conflicts → Handover lines + a "Merge conflicts" block before "From here on"."""
from __future__ import annotations

from sbmig.core.models import HandoverSummary, MergeConflict
from sbmig.report import build_handover, render_markdown
from sbmig.report.handover import merge_conflict_lines

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report

DIFFERING = MergeConflict(path="src/main/resources/messages.properties", kept_from="<root>",
                          dropped_from=["api", "web"], identical=False,
                          saved_copies=["merge-conflicts/api/src/main/resources/messages.properties",
                                        "merge-conflicts/web/src/main/resources/messages.properties"])
IDENTICAL = MergeConflict(path="src/main/resources/logback.xml", kept_from="core", dropped_from=["api"],
                          identical=True)


def test_handover_fills_merge_conflict_lines(tmp_path, cfg):
    report = make_report(merge_conflicts=[DIFFERING, IDENTICAL])
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert h.merge_conflicts == [
        "src/main/resources/messages.properties: kept the copy from <root>, dropped api, web (saved: "
        "merge-conflicts/api/src/main/resources/messages.properties, "
        "merge-conflicts/web/src/main/resources/messages.properties)",
        "1 identical duplicate(s) dropped (same content in every module): src/main/resources/logback.xml",
    ]
    report.handover = h
    md = render_markdown(report)
    assert "**Merge conflicts** (multi-module merge: first module in `<modules>` order wins)" in md
    assert "- src/main/resources/messages.properties: kept the copy from <root>, dropped api, web" in md
    assert md.index("**Merge conflicts**") < md.index("### From here on, your team")
    assert md.index("## Handover") < md.index("**Merge conflicts**")


def test_identical_line_lists_first_five():
    many = [IDENTICAL.model_copy(update={"path": f"p{i}.txt"}) for i in range(7)]
    report = make_report(merge_conflicts=many)
    lines = merge_conflict_lines(report)
    assert lines == ["7 identical duplicate(s) dropped (same content in every module): p0.txt, p1.txt, p2.txt, "
                     "p3.txt, p4.txt, …"]


def test_differing_without_saved_copies():
    c = DIFFERING.model_copy(update={"saved_copies": []})
    assert merge_conflict_lines(make_report(merge_conflicts=[c]))[0].endswith("(saved: -)")


def test_no_conflicts_no_block(tmp_path, cfg):
    report = make_report()
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert report.handover.merge_conflicts == []
    assert "Merge conflicts" not in render_markdown(report)


def test_conflicts_alone_still_render(cfg):
    report = make_report(advisories=[], phases=[], manual_followups=[])
    report.handover = HandoverSummary(merge_conflicts=["a.txt: kept the copy from core, dropped api (saved: -)"])
    md = render_markdown(report)
    assert "**Merge conflicts**" in md and "- a.txt: kept the copy from core" in md


def test_conflicts_survive_a_failed_handover(tmp_path, cfg, monkeypatch):
    import sbmig.report.handover as hmod

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(hmod, "_versions", boom)
    report = make_report(merge_conflicts=[DIFFERING])
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, cfg=cfg)
    assert h.notes and "unavailable" in h.notes[0] and len(h.merge_conflicts) == 1
