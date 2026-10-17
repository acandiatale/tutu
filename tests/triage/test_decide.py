from __future__ import annotations

from sbmig.core.interfaces import TriageContext
from sbmig.core.models import (DecisionKind, ErrorKind, FileChange, FixAttempt, KBCase, KBMatch, Phase,
                               TriageDecision)
from sbmig.triage import HandlerRegistry, Triager, cluster_errors, recipe_refs_for
from sbmig.triage.handlers import Handler

from .fakes import err


class AlwaysHandler(Handler):
    id = "always"
    description = "test handler"

    def matches(self, cluster, profile, cfg):
        return cluster.kind == ErrorKind.COMPILE

    def apply(self, ws, cluster=None, profile=None, cfg=None):
        return [FileChange(path="pom.xml", op="modify")]


class Boom(Handler):
    id = "boom"
    description = "broken matcher"

    def matches(self, cluster, profile, cfg):
        raise RuntimeError("x")

    def apply(self, ws, cluster=None, profile=None, cfg=None):  # pragma: no cover
        return []


def ctx(**kw):
    return TriageContext(phase=kw.pop("phase", Phase.SPRING), iteration=1, **kw)


def attempt(cluster, kind, outcome="no_change", **dkw):
    return FixAttempt(id="a", phase=Phase.SPRING, iteration=1, cluster_id=cluster.id,
                      decision=TriageDecision(cluster_id=cluster.id, kind=kind, **dkw), outcome=outcome,
                      cluster_signatures=cluster.signatures)


def test_order_handler_recipe_patch_llm_escalate(cfg, profile):
    reg = HandlerRegistry([AlwaysHandler(), Boom()])
    t = Triager(cfg, registry=reg)
    c = cluster_errors([err(message="package javax.persistence does not exist", file="A.java")])[0]
    case = KBCase(id="case-1", summary="did X")
    m = KBMatch(replayable=[case])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.HANDLER and d.handler_id == "always" and "always" in d.rationale
    prev = [attempt(c, DecisionKind.HANDLER, handler_id="always")]
    d = t.decide(c, m, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.KB_RECIPE
    assert d.recipe_ids == ["org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"]
    t.mark_recipes_run(d.recipe_ids, Phase.SPRING)
    d = t.decide(c, m, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.KB_PATCH and d.kb_case_ids == ["case-1"] and "did X" in d.rationale
    prev.append(attempt(c, DecisionKind.KB_PATCH, kb_case_ids=["case-1"]))
    d = t.decide(c, m, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.LLM_FIX
    d = t.decide(c, m, ctx(previous_attempts=prev, stall_level=2), profile)
    assert "stall level 2" in d.rationale
    # LLM budget
    d = t.decide(c, m, ctx(previous_attempts=prev, llm_calls_used=5, llm_calls_budget=5), profile)
    assert d.kind == DecisionKind.BLOCKED and "budget_exhausted" in d.rationale
    assert t.engine.blocked_info[c.id].reason.value == "budget_exhausted"
    # repeated LLM failures → blocked(repeated_failure) (stall_limit 2 → 3 attempts)
    llm = [attempt(c, DecisionKind.LLM_FIX) for _ in range(cfg.loop.stall_limit + 1)]
    d = t.decide(c, m, ctx(previous_attempts=prev + llm[:2]), profile)
    assert d.kind == DecisionKind.LLM_FIX and "attempt 3/3" in d.rationale
    d = t.decide(c, m, ctx(previous_attempts=prev + llm), profile)
    assert d.kind == DecisionKind.BLOCKED and "repeated_failure" in d.rationale and "needs a human" in d.rationale


def test_improved_attempts_do_not_exclude_and_kb_recipes(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([AlwaysHandler()]))
    c = cluster_errors([err(file="A.java")])[0]
    prev = [attempt(c, DecisionKind.HANDLER, outcome="improved", handler_id="always")]
    assert t.decide(c, None, ctx(previous_attempts=prev), profile).kind == DecisionKind.HANDLER
    t2 = Triager(cfg, registry=HandlerRegistry())
    m = KBMatch(recipe_ids=["kb.A"], recipe_files=["/kb/a.yml"], candidate_recipe_ids=["kb.C"],
                candidate_recipe_files=["/kb/c.yml"], handler_ids=["missing-handler"])
    d = t2.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.KB_RECIPE and d.recipe_ids == ["kb.A"]
    refs = t2.recipe_refs(d, m)
    assert refs[0].source == "kb" and refs[0].file == "/kb/a.yml"
    t2.mark_recipes_run(["kb.A"], Phase.SPRING)
    d = t2.decide(c, m, ctx(), profile)
    assert d.recipe_ids == ["kb.C"] and "candidate" in d.rationale
    # a recipe run in another phase is available again (not once_per_run)
    d = t2.decide(c, m, ctx(phase=Phase.RESTRUCTURE), profile)
    assert d.recipe_ids == ["kb.A"]
    # failed recipe attempts are excluded too
    prev = [attempt(c, DecisionKind.KB_RECIPE, recipe_ids=["kb.A", "kb.C"])]
    assert t2.decide(c, m, ctx(phase=Phase.RESTRUCTURE, previous_attempts=prev), profile).kind == DecisionKind.LLM_FIX
    # kb handler ids surface registered handlers
    t3 = Triager(cfg, registry=HandlerRegistry([AlwaysHandler()]))
    c2 = cluster_errors([err(ErrorKind.POM_INVALID, "weird", module="m")])[0]
    d = t3.decide(c2, KBMatch(handler_ids=["always"]), ctx(), profile)
    assert d.kind == DecisionKind.HANDLER


def test_recipe_refs_fallbacks(cfg):
    t = Triager(cfg, registry=HandlerRegistry())
    refs = recipe_refs_for(["org.openrewrite.java.testing.junit5.JUnit4to5Migration", "x.Y", "x.Y"],
                           KBMatch(recipe_ids=["kb.Z"]), t.rules)
    assert refs[0].artifact == "org.openrewrite.recipe:rewrite-testing-frameworks"
    assert refs[1].id == "x.Y" and refs[1].source == "builtin" and len(refs) == 2
    assert recipe_refs_for(["kb.Z"], KBMatch(recipe_ids=["kb.Z"], recipe_files=[]), t.rules)[0].file is None


def test_patch_replay_disabled(cfg, profile):
    cfg.kb.use_patch_replay = False
    cfg.kb.use_active_recipes = False
    t = Triager(cfg, registry=HandlerRegistry())
    c = cluster_errors([err(file="A.java")])[0]
    d = t.decide(c, KBMatch(replayable=[KBCase(id="k")], recipe_ids=["kb.A"]), ctx(), profile)
    assert d.kind == DecisionKind.LLM_FIX
