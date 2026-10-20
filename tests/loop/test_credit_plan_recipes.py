from types import SimpleNamespace

from sbmig.core.models import Phase, PhaseResult, RewriteResult
from sbmig.loop.orchestrator import Orchestrator


class _KB:
    def __init__(self):
        self.credits = []
    def list_recipes(self, status=None):
        return [SimpleNamespace(id="com.company.kb.Cand"), SimpleNamespace(id="com.company.kb.Active")]
    def credit_recipe(self, rid, ok, fp):
        self.credits.append((rid, ok, fp))


def test_credit_plan_recipes_only_those_that_changed_files(tmp_path, make_ws):
    src = tmp_path / "p"; src.mkdir(); (src / "pom.xml").write_text("<project/>")
    ws = make_ws(src)
    kb = _KB()
    fake_self = SimpleNamespace(kb=kb, profile=SimpleNamespace(fingerprint="fp1"), ws=ws)
    pr = PhaseResult(phase=Phase.SPRING, build_ok=True, rewrite_results=[
        RewriteResult(ok=True, recipes=["x"], recipes_applied={
            "com.company.kb.Cand": ["src/A.java"],
            "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0": ["pom.xml"],
            "com.company.kb.Active": [],                     # listed but changed nothing → no credit
        })])
    Orchestrator._credit_plan_recipes(fake_self, pr)
    assert kb.credits == [("com.company.kb.Cand", True, "fp1")]
    pr.build_ok = False
    kb.credits.clear()
    Orchestrator._credit_plan_recipes(fake_self, pr)
    assert kb.credits == [("com.company.kb.Cand", False, "fp1")]
    journal = ws.journal_path.read_text()
    assert journal.count('"kb_credit"') == 2


# ------------------------------------------------------------------ round 9: no failure credit for resolution failures
import json  # noqa: E402

import pytest  # noqa: E402

from sbmig.core.models import BlockedItem, BlockReason, BuildError, ErrorKind  # noqa: E402


def _failed_phase(**kw) -> PhaseResult:
    return PhaseResult(phase=Phase.SPRING, build_ok=False, iterations=2, rewrite_results=[
        RewriteResult(ok=True, recipes=["x"], recipes_applied={"com.company.kb.Cand": ["pom.xml"]})], **kw)


def _blocked(reason: BlockReason) -> BlockedItem:
    return BlockedItem(id=f"b-{reason.value}", reason=reason, phase=Phase.SPRING, cluster_id="c")


def _run(tmp_path, make_ws, pr):
    src = tmp_path / "p"; src.mkdir(); (src / "pom.xml").write_text("<project/>")
    ws = make_ws(src)
    kb = _KB()
    Orchestrator._credit_plan_recipes(SimpleNamespace(kb=kb, profile=SimpleNamespace(fingerprint="fp"), ws=ws), pr)
    events = [json.loads(x) for x in ws.journal_path.read_text().splitlines()] if ws.journal_path.exists() else []
    return kb.credits, events


DEP = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact a:b:pom:4.0.x in central")
PARENT = BuildError(kind=ErrorKind.PARENT_RESOLUTION, message="Non-resolvable parent POM")
COMPILE = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/A.java")


@pytest.mark.parametrize("kw,reason", [
    ({"notes": ["spring.rewrite: rewrite-unresolved-versions — the recipe run left literal versions (pom.xml: 4.0.x)"],
      "remaining_errors": [COMPILE]}, "rewrite-unresolved-versions"),
    ({"remaining_errors": [DEP, PARENT]}, "dependency-resolution"),
    ({"remaining_errors": [BuildError(kind=ErrorKind.REWRITE, message="download-retry: status code: 429"), DEP]},
     "dependency-resolution"),
    ({"blocked": [_blocked(BlockReason.ARTIFACT_NOT_FOUND), _blocked(BlockReason.PARENT_NOT_FOUND)]},
     "dependency-resolution"),
])
def test_resolution_failures_are_not_credited(tmp_path, make_ws, kw, reason):
    credits, events = _run(tmp_path, make_ws, _failed_phase(**kw))
    assert credits == []
    (skip,) = [e for e in events if e["event"] == "kb_credit_skipped"]
    assert skip == {**skip, "phase": "spring", "recipes": ["com.company.kb.Cand"], "reason": reason}
    assert not any(e["event"] == "kb_credit" for e in events)


@pytest.mark.parametrize("kw", [
    {"remaining_errors": [COMPILE]},
    {"remaining_errors": [DEP, COMPILE]},                          # a code error is left → the recipe may be at fault
    {"remaining_errors": [BuildError(kind=ErrorKind.REWRITE, message="OpenRewrite failed: boom")]},
    {"blocked": [_blocked(BlockReason.ARTIFACT_NOT_FOUND), _blocked(BlockReason.REPEATED_FAILURE)]},
    {},
])
def test_code_failures_are_still_credited(tmp_path, make_ws, kw):
    credits, events = _run(tmp_path, make_ws, _failed_phase(**kw))
    assert credits == [("com.company.kb.Cand", False, "fp")]
    assert not any(e["event"] == "kb_credit_skipped" for e in events)


def test_success_credit_ignores_resolution_markers(tmp_path, make_ws):
    pr = _failed_phase(remaining_errors=[DEP])
    pr.build_ok = True
    credits, _ = _run(tmp_path, make_ws, pr)
    assert credits == [("com.company.kb.Cand", True, "fp")]
