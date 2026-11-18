"""planner.py: what the IVAS patched-version hint allows (review findings M3, M4).

M3: a jump to another major version only when the hint itself points into that major; a date-stamped version
(`20030203.000550`) is never an upgrade of a normal version.
M4: a current version that already meets every hint is not bumped: residual "IVAS still reports it although ..."."""
from __future__ import annotations

import pytest

from sbmig.remediations.models import O_TRANSITIVE, InventoryItem

from .test_planner import DM_POM, FakeSource, _cfg, _direct, _plan, _write, project  # noqa: F401 - fixture


def _one(plan):
    assert len(plan.proposals) + len(plan.residuals) == 1, (plan.proposals, plan.residuals)
    return plan.proposals[0] if plan.proposals else plan.residuals[0]


# ------------------------------------------------------------------------------------------------- M3: major jumps
def test_date_stamped_build_is_never_an_upgrade(project):
    # commons-io: the repository lists the 2003 build "20030203.000550"; nothing on 2.x reaches the hint
    item = _direct(version="1.2.3", hints=("1.2.7",), ga="example:lib")
    src = FakeSource({"example:lib": ["1.2.3", "1.2.4", "20030203.000550"]})
    r = _one(_plan(project, [item], source=src))
    assert r.reason == "no_candidate_version", r
    assert "20030203.000550 (date-stamped)" in r.detail


def test_no_major_jump_when_the_hint_stays_on_the_current_major(project):
    src = FakeSource({"example:lib": ["1.2.3", "2.0.0", "2.1.0", "3.0.0"]})
    r = _one(_plan(project, [_direct(hints=("1.2.5",))], source=src))
    assert r.reason == "no_candidate_version"
    assert r.detail == ("only other major versions are available (2.0.0, 2.1.0, 3.0.0) and no IVAS patched hint "
                        "names that major (hint 1.2.5)")


def test_major_jump_when_the_hint_names_that_major(project):
    src = FakeSource({"example:lib": ["1.2.3", "2.0.0", "2.0.1", "2.1.0", "3.0.0"]})
    p = _one(_plan(project, [_direct(hints=("2.0.1",))], source=src))
    assert (p.to_version, p.risks) == ("2.0.1", ["major"])


def test_major_jump_named_by_one_line_of_a_two_line_hint(project):
    src = FakeSource({"example:lib": ["1.2.3", "1.2.8", "2.0.1", "2.0.2"]})
    p = _one(_plan(project, [_direct(hints=("1.2.9, 2.0.1",))], source=src))
    assert p.to_version == "2.0.1"


def test_hint_names_major_2_but_only_major_3_reaches_it(project):
    src = FakeSource({"example:lib": ["1.2.3", "3.0.0"]})
    r = _one(_plan(project, [_direct(hints=("2.0.1",))], source=src))
    assert r.reason == "no_candidate_version" and "(3.0.0)" in r.detail and "(hint 2.0.1)" in r.detail


def test_release_qualifier_major_jump_named_by_the_hint(project):
    item = _direct(version="4.3.30.RELEASE", hints=("5.2.24.RELEASE",))
    item.location.text = "4.3.30.RELEASE"
    (project / "pom.xml").write_text((project / "pom.xml").read_text().replace("1.2.3", "4.3.30.RELEASE"))
    src = FakeSource({"example:lib": ["4.3.30.RELEASE", "5.2.24.RELEASE", "5.3.0"]})
    assert _one(_plan(project, [item], source=src)).to_version == "5.2.24.RELEASE"


def test_allow_major_false_still_refuses_a_named_major(project):
    src = FakeSource({"example:lib": ["2.0.1"]})
    r = _one(_plan(project, [_direct(hints=("2.0.1",))], cfg=_cfg(policy={"allow_major": False}), source=src))
    assert r.reason == "no_candidate_version" and "allow_major is false" in r.detail


def test_date_stamped_current_version_is_not_compared(project):
    item = _direct(version="20030203.000550", hints=("2.7",))
    item.location.text = "20030203.000550"
    (project / "pom.xml").write_text((project / "pom.xml").read_text().replace("1.2.3", "20030203.000550"))
    src = FakeSource({"example:lib": ["2.7", "2.8.0", "20030203.000550"]})
    r = _one(_plan(project, [item], source=src))
    assert r.reason == "no_candidate_version"
    assert r.detail == ("the current version 20030203.000550 is date-stamped and cannot be compared with the IVAS "
                        "patched hint 2.7; not chosen automatically")


def test_empty_repository_listing_is_the_existing_residual(project):
    r = _one(_plan(project, [_direct(hints=("1.2.5",))], source=FakeSource({})))
    assert r.reason == "no_candidate_version" and r.detail.startswith("repository listed nothing")


def test_range_hint_unchanged(project):
    r = _one(_plan(project, [_direct(hints=("[1.2.5,2.0)",))], source=FakeSource({"example:lib": ["2.0.0"]})))
    assert r.reason == "no_candidate_version" and "[1.2.5,2.0)" in r.detail


def test_override_major_jump_follows_the_same_rule(tmp_path):
    proj = _write(tmp_path, DM_POM)
    t = InventoryItem(ga="example:t", version="1.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                      via=["example:lib:1.2.3"], finding_ids=["CVE-T"])
    r = _one(_plan(proj, [t], source=FakeSource({"example:t": ["2.0"]})))
    assert r.reason == "no_candidate_version" and "no IVAS patched hint names that major" in r.detail


# ------------------------------------------------------------------------------------------------- M4: hint already met
MEETS = "IVAS still reports {cur} although it meets the patched hint {hint}: ask the IVAS team to check the finding"


@pytest.mark.parametrize("current,hint", [
    ("2.15.0", "2.12.7.1"),                  # above the hint on a later line
    ("6.0.3", "5.3.27"),                     # the hint is on an older line
    ("1.2.5", "1.2.5"),                      # exactly the hint
    ("5.3.20.RELEASE", "5.3.20"),            # a release qualifier equals the plain version
    ("2.2.0.RELEASE", "2.2.0.release"),      # case
    ("32.0.1-jre", "32.0.0-jre"),            # -jre qualifier
    ("2.14.1", "2.12.7.1, 2.13.4.2"),        # above every line of a two-line hint
])
def test_current_version_already_meets_the_hint(project, current, hint):
    item = _direct(version=current, hints=(hint,))
    item.location.text = current
    (project / "pom.xml").write_text((project / "pom.xml").read_text().replace("1.2.3", current))
    src = FakeSource({"example:lib": [current, "99.0.0"]})
    r = _one(_plan(project, [item], source=src))
    assert r.reason == "manual_review", r
    assert r.detail == MEETS.format(cur=current, hint=hint)
    assert src.asked == []                   # nothing to look up: no version is chosen


@pytest.mark.parametrize("current,hint,listed,target", [
    ("2.13.0", "2.12.7.1, 2.13.4.2", ["2.13.4.2", "2.13.5"], "2.13.4.2"),   # below its own line's hint
    ("31.1-jre", "32.0.0-jre", ["32.0.0-jre", "32.0.1-jre"], "32.0.0-jre"),
    ("1.2.4", "1.2.5", ["1.2.5"], "1.2.5"),
])
def test_current_version_below_the_hint_is_still_planned(project, current, hint, listed, target):
    item = _direct(version=current, hints=(hint,))
    item.location.text = current
    (project / "pom.xml").write_text((project / "pom.xml").read_text().replace("1.2.3", current))
    p = _one(_plan(project, [item], source=FakeSource({"example:lib": listed})))
    assert p.to_version == target


def test_one_finding_met_and_one_not_is_planned(project):
    src = FakeSource({"example:lib": ["1.2.5", "1.2.9"]})
    p = _one(_plan(project, [_direct(version="1.2.3", hints=("1.2.1", "1.2.9"))], source=src))
    assert p.to_version == "1.2.9"


def test_transitive_already_meeting_the_hint_gets_no_override(tmp_path):
    proj = _write(tmp_path, DM_POM)
    t = InventoryItem(ga="example:t", version="2.0", origin=O_TRANSITIVE, patched_versions=["1.1"],
                      via=["example:lib:1.2.3"], finding_ids=["CVE-T"])
    r = _one(_plan(proj, [t], source=FakeSource({"example:t": ["2.0", "2.1"]})))
    assert r.reason == "manual_review" and r.detail == MEETS.format(cur="2.0", hint="1.1")


def test_policy_pin_still_wins_over_a_met_hint(project):
    cfg = _cfg(policy={"pins": {"example:lib": "1.2.9"}})
    p = _one(_plan(project, [_direct(version="1.2.3", hints=("1.2.1",))], cfg=cfg))
    assert p.to_version == "1.2.9" and p.evidence == "policy:pins"


def test_met_hint_is_never_bumped_in_a_later_round(project):
    # round 2 plans from the same tree / inventory again: still a residual, nothing proposed
    item = _direct(version="1.2.3", hints=("1.2.0",))
    src = FakeSource({"example:lib": ["1.2.4", "1.2.5"]})
    for rnd in (1, 2, 3):
        plan = _plan(project, [item], source=src, round=rnd)
        assert not plan.proposals and plan.residuals[0].reason == "manual_review"
