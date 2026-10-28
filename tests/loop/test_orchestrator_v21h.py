"""v2.1h orchestrator fixes (docs/v2/HANDOVER-FINDINGS-2026-10-28.md): A23 the structure review's LLM calls are
attributed once (the first fixed critical issue's try carries them, the others 0) while the report's LLM usage stays
the review's real count; A5 the restructure's JSP javax → jakarta rename reaches the report ("What we changed") and a
JSP left with javax references becomes a Handover follow-up."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import LLMUsage, Phase, ReviewIssue, StructureReview
from sbmig.loop.entries import REVIEW_L2
from sbmig.report.handover import jsp_jakarta_counts
from sbmig.triage.cluster import l2_case_id

from .fakes import FakeErrorList, FakeRestructurer, FakeReviewer, journal_events, make_profile
from .test_v2_orchestrator import make_v2


# ---------------------------------------------------------------------------------------------- A23
def test_review_llm_calls_are_attributed_to_the_first_fixed_issue_only(cfg, ws):
    rv = StructureReview(verdict="issues", llm_usage=LLMUsage(model="m", calls=2, input_tokens=10, output_tokens=5),
                         issues=[ReviewIssue(severity="critical", file="pom.xml", problem="parent is not base-parent"),
                                 ReviewIssue(severity="critical", file="pom.xml", problem="sourceEncoding MS949"),
                                 ReviewIssue(severity="warning", file="pom.xml", problem="duplicate g:a")])
    reviewer = FakeReviewer(review=rv, files={"pom.xml": "<project>utf8</project>\n"})
    el = FakeErrorList()
    report = make_v2(cfg, ws, reviewer=reviewer, error_list=lambda w, c, p: el).run()
    assert report.structure_review.commit
    tries = el.tries_of(l2_case_id(REVIEW_L2))
    assert [t.llm_calls for t in tries] == [2, 0]
    assert sum(t.llm_calls for t in tries) == rv.llm_usage.calls
    assert "counted on its first fixed issue" in tries[1].note and "counted" not in tries[0].note
    assert report.llm_usage.calls >= 2                  # the report's usage still counts the review's calls


# ---------------------------------------------------------------------------------------------- A5
class JspRestructurer(FakeRestructurer):
    def __init__(self, files: int, refs: int, followups: tuple[str, ...] = ()):
        super().__init__()
        self.files, self.refs, self.followups = files, refs, list(followups)

    def apply(self, ws, profile, cfg, *, mode, params=None):
        res = super().apply(ws, profile, cfg, mode=mode, params=params)
        renamed = [f"demo-front/src/main/resources/META-INF/resources/WEB-INF/jsp/p{i}.jsp: 1"
                   for i in range(self.files)]
        return res.model_copy(update={"jsp_jakarta_files": self.files, "jsp_jakarta_refs": self.refs,
                                      "jsp_jakarta_renamed": renamed,
                                      "manual_followups": [*res.manual_followups, *self.followups]})


def jsp_profile(found: int):
    p = make_profile()
    p.source.jsp_javax_files = found
    return p


def test_jsp_rename_reaches_what_we_changed_and_the_journal(cfg, ws):
    report = make_v2(cfg, ws, restructurer=JspRestructurer(3, 7), profile=jsp_profile(3)).run()
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert "restructure.apply: JSP javax → jakarta: 3 files, 7 references" in rs.notes
    assert jsp_jakarta_counts(report) == (3, 7)
    md = Path(report.outputs["report_md"]).read_text(encoding="utf-8")
    assert "JSP javax → jakarta: 3 files, 7 references — renamed by the restructure" in md
    assert not any(x.startswith("JSP javax references remain") for x in report.handover.from_here)
    [ev] = [e for e in journal_events(ws) if e["event"] == "jsp_jakarta"]
    assert (ev["step"], ev["files"], ev["refs"], len(ev["renamed"])) == ("restructure.apply", 3, 7, 3)


def test_jsps_left_with_javax_are_a_handover_follow_up(cfg, ws):
    cfg.layout.jsp_jakarta_rename = False
    report = make_v2(cfg, ws, restructurer=JspRestructurer(0, 0), profile=jsp_profile(4)).run()
    assert jsp_jakarta_counts(report) == (0, 0)
    [line] = [x for x in report.handover.from_here if x.startswith("JSP javax references remain")]
    assert line.startswith("JSP javax references remain (4 files)") and "layout.jsp_jakarta_rename" in line
    assert not any(e["event"] == "jsp_jakarta" for e in journal_events(ws))


def test_the_restructures_jsp_follow_up_is_not_repeated(cfg, ws):
    fu = "1 JSP file(s) keep javax references (unknown encoding): WEB-INF/jsp/legacy.jsp"
    report = make_v2(cfg, ws, restructurer=JspRestructurer(2, 4, (fu,)), profile=jsp_profile(3)).run()
    assert fu in report.manual_followups and fu in report.handover.from_here
    assert not any(x.startswith("JSP javax references remain") for x in report.handover.from_here)


def test_results_without_the_jsp_fields_change_nothing(cfg, ws):
    class OldResult(FakeRestructurer):
        def apply(self, ws, profile, cfg, *, mode, params=None):
            r = super().apply(ws, profile, cfg, mode=mode, params=params)
            return RestructureResult(**{k: v for k, v in r.model_dump().items() if not k.startswith("jsp_")})

    report = make_v2(cfg, ws, restructurer=OldResult()).run()
    assert jsp_jakarta_counts(report) == (0, 0) and report.success
