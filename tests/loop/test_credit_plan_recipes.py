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
