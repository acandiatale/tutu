"""v2.1k (2026-10-29): "Project 'g:a:v' is duplicated in the reactor" is a pom structure problem — classified as
`reactor_duplicate` (blocked_policy 0: straight to the Handover, no LLM), with the duplicated coordinates as evidence."""
from __future__ import annotations

from sbmig.build import parse_maven_log
from sbmig.core.interfaces import TriageContext
from sbmig.core.models import BlockedItem, BlockReason, DecisionKind, Phase
from sbmig.loop.blocked_registry import compose_suggestion
from sbmig.report.i18n import tr_line
from sbmig.report.markdown import blocked_rows
from sbmig.triage import Triager, classify_blocked, cluster_cases, cluster_errors
from sbmig.triage.blocked import reactor_duplicate_coordinates

from .conftest import make_profile

# the company's output (Maven 3.9, `mvn -B compile` on a root pom listing APP + DEV with APP listing ../DEV)
DUP_LOG = """[INFO] Scanning for projects...
[ERROR] [ERROR] Project 'com.xxx.xxx:xxx:1.0.0-SNAPSHOT' is duplicated in the reactor @
[ERROR] Project 'com.xxx.xxx:xxx:1.0.0-SNAPSHOT' is duplicated in the reactor -> [Help 1]
[ERROR]
[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.
[ERROR] Re-run Maven using the -X switch to enable full debug logging.
[ERROR]
[ERROR] For more information about the errors and possible solutions, please read the following articles:
[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/DuplicateProjectException
"""


def _cluster(log: str = DUP_LOG):
    clusters = cluster_errors(parse_maven_log(log, "/work/proj"))
    assert len(clusters) == 1
    return clusters[0]


def test_duplicated_in_the_reactor_is_blocked_with_the_coordinates():
    c = _cluster()
    b = classify_blocked(c, make_profile(), None)
    assert b is not None and b.reason == BlockReason.REACTOR_DUPLICATE
    assert b.errors == c.errors
    assert b.detail().startswith("project com.xxx.xxx:xxx:1.0.0-SNAPSHOT is duplicated in the reactor: it is reached "
                                 "through more than one <module> listing")
    assert reactor_duplicate_coordinates(DUP_LOG) == "com.xxx.xxx:xxx:1.0.0-SNAPSHOT"


def test_older_wording_is_recognised_too():
    log = ("[INFO] Scanning for projects...\n[ERROR] The projects in the reactor contain a duplicate project "
           "'com.xxx:dev' -> [Help 1]\n")
    errs = parse_maven_log(log, "/work/proj")
    assert errs, "the parser keeps the line"
    b = classify_blocked(cluster_errors(errs)[0], make_profile(), None)
    assert b is not None and b.reason == BlockReason.REACTOR_DUPLICATE


def test_the_default_policy_blocks_at_once_without_the_llm(cfg, profile):
    assert cfg.loop.blocked_policy["reactor_duplicate"] == 0
    t = Triager(cfg)
    case = cluster_cases(parse_maven_log(DUP_LOG, "/work/proj"))[0]
    d = t.decide(case, None, TriageContext(phase=Phase.VERIFY, iteration=1), profile)
    assert d.kind == DecisionKind.BLOCKED and d.rationale.startswith("blocked: reactor_duplicate — project "
                                                                     "com.xxx.xxx:xxx:1.0.0-SNAPSHOT")


def test_suggestion_names_the_coordinates_in_english_and_korean():
    c = _cluster()
    item = BlockedItem(id="b1", reason=BlockReason.REACTOR_DUPLICATE, phase=Phase.VERIFY, cluster_id=c.id,
                       errors=list(c.errors))
    s = compose_suggestion(item)
    assert s.startswith("`com.xxx.xxx:xxx:1.0.0-SNAPSHOT` is reached more than once through the reactor's <module> "
                        "lists") and s.endswith("the LLM is not asked")
    ko = tr_line("ko", s)
    assert ko != s and ko.startswith("`com.xxx.xxx:xxx:1.0.0-SNAPSHOT` 가 reactor 의 <module> 목록으로 두 번 이상 닿는다")
    item.suggestion = s
    assert any(ko in row for row in blocked_rows([item], "ko"))
    assert any(s in row for row in blocked_rows([item], "en"))
