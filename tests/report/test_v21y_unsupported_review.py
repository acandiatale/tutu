"""v2.1y (prompt review E-M5): a structure-review issue that names a version / property the review material does not
contain (`ReviewIssue.unsupported`) stays in the report's "Structure review" section with the mark "(sbmig check) not in
the material" and never reaches the Handover."""
from __future__ import annotations

from sbmig.core.models import ReviewIssue
from sbmig.report import render_markdown
from sbmig.report.handover import review_followups
from sbmig.report.markdown import UNSUPPORTED_MARK, structure_review_lines
from sbmig.review.parse import UNSUPPORTED_MARK as REVIEW_MARK

from .test_review_report import review_report, section

STALE = ReviewIssue(severity="warning", file="module/comp-jar/pom.xml", unsupported=True,
                    problem="jakarta.servlet-api:5.0.0 is pinned with scope compile", fix_hint="drop the version")
MARKED = STALE.model_copy(update={"problem": REVIEW_MARK + STALE.problem})


def _report(*extra: ReviewIssue):
    r = review_report(script="", warnings=1, commit=None)
    r.structure_review.issues = list(r.structure_review.issues) + list(extra)
    return r


def test_mark_is_the_reviewers():
    assert UNSUPPORTED_MARK == REVIEW_MARK == "(sbmig check) not in the material: "


def test_unsupported_issue_is_not_handed_over():
    plain = review_followups(_report())
    lines = review_followups(_report(MARKED, STALE))
    assert lines == plain and not any("5.0.0" in ln for ln in lines)
    critical = MARKED.model_copy(update={"severity": "critical"})
    assert review_followups(_report(critical)) == plain


def test_unsupported_issue_stays_in_the_structure_review_section():
    for issue in (MARKED, STALE):                       # marked by the reviewer, or only flagged (older report.json)
        body = "\n".join(structure_review_lines(_report(issue)))
        row = next(ln for ln in body.splitlines() if "5.0.0" in ln)
        assert row.count(UNSUPPORTED_MARK.strip()) == 1 and "| warning |" in row
    md = render_markdown(_report(MARKED))
    assert "5.0.0" in section(md) and "5.0.0" not in md.split("## Structure review")[0]
