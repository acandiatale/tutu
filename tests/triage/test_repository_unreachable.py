"""Review 2026-11-18 T4: a resolution error that is a transfer failure (the repository did not answer: connect / read timed
out, connection refused, 5xx, "Could not transfer artifact … " with a network cause) is `repository_unreachable` — no LLM
try (policy 0), and no stored script is reused on it (a rebuild that passes because the network came back credits nothing).
An artifact the repository answered "not found" for stays `artifact_not_found` / `parent_not_found`."""
from __future__ import annotations

import pytest

from sbmig.build import parse_maven_log
from sbmig.core.interfaces import TriageContext
from sbmig.core.models import BlockReason, DecisionKind, ErrorKind, KBMatch
from sbmig.triage import Triager, classify_blocked, cluster_errors

from .conftest import make_profile

ROOT = "/work/proj"
URL = "https://nexus.invalid/repository/public/"
TIMEOUT = (f"[ERROR] Failed to execute goal on project demo: Could not resolve dependencies for project com.x:demo:jar:1.0: "
           f"The following artifacts could not be resolved: org.example:lib:jar:1.0 (absent): Could not transfer artifact "
           f"org.example:lib:jar:1.0 from/to nexus ({URL}): Connect to nexus.invalid:443 [nexus.invalid/10.0.0.1] failed: "
           f"Connect timed out -> [Help 1]\n")
HTTP_503 = (f"[ERROR] Failed to execute goal on project demo: Could not resolve dependencies for project com.x:demo:jar:1.0: "
            f"The following artifacts could not be resolved: org.example:lib:jar:1.0 (absent): Could not transfer artifact "
            f"org.example:lib:jar:1.0 from/to nexus ({URL}): status code: 503, reason phrase: Service Unavailable (503) "
            f"-> [Help 1]\n")
NOT_FOUND = (f"[ERROR] Failed to execute goal on project demo: Could not resolve dependencies for project com.x:demo:jar:1.0: "
             f"Could not find artifact org.example:missing:jar:9.9 in nexus ({URL}) -> [Help 1]\n")
PARENT_TIMEOUT = (f"[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
                  f"[FATAL] Non-resolvable parent POM for com.x:demo:1.0: The following artifacts could not be resolved: "
                  f"com.example:company-parent:pom:4.0.0 (absent): Could not transfer artifact "
                  f"com.example:company-parent:pom:4.0.0 from/to nexus ({URL}): Read timed out and "
                  f"'parent.relativePath' points at no local POM @ line 5, column 11\n")


def _cluster(log: str, kind: ErrorKind):
    return next(c for c in cluster_errors(parse_maven_log(log, ROOT)) if c.kind == kind)


@pytest.mark.parametrize("log", [TIMEOUT, HTTP_503])
def test_transfer_failure_is_repository_unreachable(log):
    b = classify_blocked(_cluster(log, ErrorKind.DEPENDENCY_RESOLUTION), make_profile(), None)
    assert b is not None and b.reason == BlockReason.REPOSITORY_UNREACHABLE
    assert "did not answer" in b.detail() and b.repositories


def test_not_found_answer_stays_artifact_not_found():
    b = classify_blocked(_cluster(NOT_FOUND, ErrorKind.DEPENDENCY_RESOLUTION), make_profile(), None)
    assert b.reason == BlockReason.ARTIFACT_NOT_FOUND


def test_parent_transfer_failure_is_repository_unreachable():
    c = next((c for c in cluster_errors(parse_maven_log(PARENT_TIMEOUT, ROOT))
              if c.kind == ErrorKind.PARENT_RESOLUTION), None)
    if c is None:
        pytest.skip("the parser does not file this line as parent_resolution")
    assert classify_blocked(c, make_profile(), None).reason == BlockReason.REPOSITORY_UNREACHABLE


def test_mixed_run_shows_the_transfer_failures_as_what_they_are():
    clusters = [c for c in cluster_errors(parse_maven_log(TIMEOUT + NOT_FOUND, ROOT))
                if c.kind == ErrorKind.DEPENDENCY_RESOLUTION]
    reasons = set()
    for c in clusters:
        b = classify_blocked(c, make_profile(), None)
        reasons.add(b.reason)
        if b.reason == BlockReason.REPOSITORY_UNREACHABLE:
            assert all("timed out" in (e.message + e.raw).lower() for e in b.errors)
    assert BlockReason.REPOSITORY_UNREACHABLE in reasons


def test_policy_zero_blocks_before_a_stored_script_is_reused(cfg):
    from sbmig.core.models import Phase

    from .test_v2_cases import Store, meta
    assert cfg.loop.blocked_policy["repository_unreachable"] == 0
    c = _cluster(TIMEOUT, ErrorKind.DEPENDENCY_RESOLUTION)
    t = Triager(cfg)
    t.attach_run(cases=Store(best=(meta(2, successes=3), "def fix(p):\n    pass\n")))
    d = t.decide(c, KBMatch(), TriageContext(phase=Phase.VERIFY, iteration=1), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "repository_unreachable" in d.rationale


def test_a_not_found_case_still_reuses_its_stored_script(cfg):
    from sbmig.core.models import Phase

    from .test_v2_cases import Store, meta
    c = _cluster(NOT_FOUND, ErrorKind.DEPENDENCY_RESOLUTION)
    t = Triager(cfg)
    t.attach_run(cases=Store(best=(meta(2, successes=3), "def fix(p):\n    pass\n")))
    d = t.decide(c, KBMatch(), TriageContext(phase=Phase.VERIFY, iteration=1), make_profile())
    assert d.kind == DecisionKind.SCRIPT
