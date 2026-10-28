"""v2.1h report fixes (docs/v2/HANDOVER-FINDINGS-2026-10-28.md): A12 `write_report` hands `cfg` to `render_markdown`
(the javax section's in-house groups), A13 the properties console line reads the placeholder files from the same
inventory as the keys, A5 the JSP javax → jakarta rename in "What we changed" and the Handover follow-up for JSPs
left with javax references."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import ArtifactSpec
from sbmig.core.models import HandoverSummary, Phase, PhaseResult
from sbmig.core.workspace import Workspace
from sbmig.report import build_handover, render_markdown, write_report
from sbmig.report.handover import (
    JSP_RENAME_OFF,
    is_jsp_jakarta_note,
    jsp_jakarta_counts,
    jsp_jakarta_note,
    jsp_javax_item,
    jsp_javax_left,
)
from sbmig.report.i18n import tr_line
from sbmig.report.writer import properties_console_line

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report
from .test_javax_api_report import report_with_jars
from .test_properties_handover import PH, inventory

IN_HOUSE = "in-house → Jakarta build"


def _ws(tmp_path) -> Workspace:
    (tmp_path / "p").mkdir(parents=True, exist_ok=True)
    return Workspace(tmp_path / "p", tmp_path / "run", "t")


# ---------------------------------------------------------------------------------------------- A12
def test_write_report_passes_the_config_to_the_javax_section(tmp_path, cfg):
    c = cfg.model_copy(deep=True)
    c.artifacts.parent = ArtifactSpec(group_id="com.company", artifact_id="base-parent", version="4.0.0")
    r = report_with_jars()
    r.handover = None                                  # no Handover parent: only the config names the company groups
    with_cfg = write_report(r, _ws(tmp_path), c)
    assert IN_HOUSE in Path(with_cfg["report_md"]).read_text(encoding="utf-8")
    without = write_report(r, _ws(tmp_path / "x"), None)
    assert IN_HOUSE not in Path(without["report_md"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- A13
def test_properties_console_line_falls_back_to_the_profile_inventory_for_both(cfg):
    prof = as_is_profile()
    prof.properties = inventory()                      # the profile inventory carries the placeholder files here
    report = make_report(properties=None, profile=prof)
    report.handover = HandoverSummary(properties_to_set=["mail.host | unknown | - | - | -"])
    assert properties_console_line(report) == f"properties to set: 5 (placeholders in {PH})"
    report.properties = inventory(placeholders=False)  # the report's own inventory wins for keys AND files
    assert properties_console_line(report) == "properties to set: 5"


# ---------------------------------------------------------------------------------------------- A5
def _jsp_report(files: int = 3, refs: int = 12, found: int = 3, **kw):
    prof = as_is_profile()
    prof.source.jsp_javax_files = found
    report = make_report(profile=prof, **kw)
    if files:
        report.phases.append(PhaseResult(phase=Phase.RESTRUCTURE, step_ids=["restructure.apply"],
                                         notes=[jsp_jakarta_note("restructure.apply", files, refs), "other note"]))
    return report


def test_note_round_trip_and_counts():
    note = jsp_jakarta_note("restructure.apply", 3, 12)
    assert note == "restructure.apply: JSP javax → jakarta: 3 files, 12 references"
    assert jsp_jakarta_note("restructure.apply", 1, 1).endswith("JSP javax → jakarta: 1 file, 1 reference")
    assert is_jsp_jakarta_note(note) and not is_jsp_jakarta_note("JSP javax → jakarta: 3 file(s)")
    r = _jsp_report()
    r.phases.append(PhaseResult(phase=Phase.RESTRUCTURE, notes=[jsp_jakarta_note("restructure.bump", 1, 2)]))
    assert jsp_jakarta_counts(r) == (4, 14)
    assert jsp_jakarta_counts(make_report()) == (0, 0)


def test_what_we_changed_shows_the_rename_and_notes_do_not_repeat_it():
    md = render_markdown(_jsp_report())
    changed = md.split("## What we changed", 1)[1].split("\n## ", 1)[0]
    assert ("JSP javax → jakarta: 3 files, 12 references — renamed by the restructure in JSP / tag files "
            "(page imports, useBean, scriptlets); OpenRewrite does not parse JSPs") in changed
    notes = md.split("## Notes", 1)[1].split("\n## ", 1)[0]
    assert "JSP javax → jakarta" not in notes and "other note" in notes
    ko = render_markdown(_jsp_report(files=1, refs=1), language="ko")
    assert "JSP javax → jakarta: 파일 1개, 참조 1곳 — 재구성이 JSP / 태그 파일" in ko
    assert "JSP javax → jakarta" not in render_markdown(make_report())


def test_handover_follow_up_for_jsps_left_with_javax(tmp_path, cfg):
    # every file renamed → nothing left, no follow-up
    assert jsp_javax_left(_jsp_report()) == 0 and jsp_javax_item(_jsp_report(), cfg) is None
    # 4 found, 3 renamed → 1 left
    r = _jsp_report(found=4)
    assert jsp_javax_left(r) == 1
    item = jsp_javax_item(r, cfg)
    assert item is not None and item.startswith("JSP javax references remain (1 file) — ") and \
        JSP_RENAME_OFF not in item
    h = build_handover(as_is_profile(), final_tree(tmp_path), r, effective=EFFECTIVE, cfg=cfg)
    assert item in h.from_here
    # the rename is off: nothing renamed, the analyzer's count and the config named
    off = cfg.model_copy(deep=True)
    off.layout.jsp_jakarta_rename = False
    r2 = _jsp_report(files=0, found=5)
    line = jsp_javax_item(r2, off)
    assert line is not None and line.startswith("JSP javax references remain (5 files)") and line.endswith(JSP_RENAME_OFF)
    assert tr_line("ko", line).startswith("JSP 에 javax 참조가 남아 있다(파일 5개)") and "꺼져 있다" in tr_line("ko", line)
    assert tr_line("ko", item).startswith("JSP 에 javax 참조가 남아 있다(파일 1개)")
    # no JSP with javax found → nothing
    assert jsp_javax_item(_jsp_report(files=0, found=0), off) is None


def test_the_restructures_own_follow_up_line_wins(tmp_path, cfg):
    fu = "2 JSP file(s) keep javax.servlet references (unknown encoding): WEB-INF/jsp/a.jsp, WEB-INF/jsp/b.jsp"
    r = _jsp_report(files=1, found=3, manual_followups=[fu])
    assert jsp_javax_left(r) == 2 and jsp_javax_item(r, cfg) is None
    h = build_handover(as_is_profile(), final_tree(tmp_path), r, effective=EFFECTIVE, cfg=cfg)
    assert fu in h.from_here and not any(x.startswith("JSP javax references remain") for x in h.from_here)
