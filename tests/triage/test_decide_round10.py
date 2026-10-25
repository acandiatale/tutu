"""Round 10 (2026-10-26): A1 — the per-cluster limits count only scored failures of the SAME cluster; A9 — a case
action a person pinned (`CaseHit.pinned`) is obeyed: handler first, block without an LLM, recipe ids."""
from __future__ import annotations

from sbmig.core.interfaces import TriageContext
from sbmig.core.models import (BlockReason, BuildError, CaseAction, CaseHit, DecisionKind, ErrorKind, FileChange,
                               FixAttempt, KBMatch, Phase, TriageDecision)
from sbmig.loop.blocked_registry import BlockedRegistry
from sbmig.triage import HandlerRegistry, Triager, cluster_errors
from sbmig.triage.decide import same_cluster
from sbmig.triage.handlers import Handler

GONE = "compile|cannot_find_symbol|class Gone"


def l1(file: str, sig: str = GONE) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, line=3, module="demo",
                      symbol="class Gone", signature=sig, signature_generic="compile|cannot_find_symbol")


def one(file: str = "src/main/java/a/Foo.java", sig: str = GONE):
    return cluster_errors([l1(file, sig)])[0]


def ctx(**kw):
    return TriageContext(phase=kw.pop("phase", Phase.SPRING), iteration=1, **kw)


def attempt(cluster, outcome="no_change", kind=DecisionKind.LLM_FIX, aid="a", cluster_id=None, **dkw) -> FixAttempt:
    return FixAttempt(id=aid, phase=Phase.SPRING, iteration=1, cluster_id=cluster_id or cluster.id,
                      decision=TriageDecision(cluster_id=cluster.id, kind=kind, **dkw), outcome=outcome,
                      cluster_signatures=list(cluster.signatures), cluster_errors=list(cluster.errors))


class Named(Handler):
    def __init__(self, hid: str, match: bool = True):
        self.id, self.description, self._match = hid, f"handler {hid}", match

    def matches(self, cluster, profile, cfg):
        return self._match

    def apply(self, ws, cluster=None, profile=None, cfg=None):  # pragma: no cover - decide() never applies
        return [FileChange(path="pom.xml", op="modify")]


# ------------------------------------------------------------------------------------------------ A1
def test_attempts_on_other_files_with_the_same_l1_do_not_count(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    foo, bar = one("src/main/java/a/Foo.java"), one("src/main/java/a/Bar.java")
    assert foo.signatures == bar.signatures and foo.id != bar.id
    others = [attempt(bar, "no_change", aid=f"b{i}") for i in range(5)]
    d = t.decide(foo, None, ctx(previous_attempts=others), profile)
    assert d.kind == DecisionKind.LLM_FIX and "attempt" not in d.rationale       # Foo's first attempt


def test_pending_is_not_a_failure_partial_is_progress(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    c = one()
    limit = max(cfg.loop.max_llm_attempts_per_cluster, cfg.loop.stall_limit + 1)
    pend = [attempt(c, "pending", aid=f"p{i}") for i in range(limit + 2)]
    assert t.decide(c, None, ctx(previous_attempts=pend), profile).kind == DecisionKind.LLM_FIX
    part = [attempt(c, "partial", aid=f"q{i}") for i in range(limit + 2)]
    assert t.decide(c, None, ctx(previous_attempts=part), profile).kind == DecisionKind.LLM_FIX
    inc = [attempt(c, "inconclusive", aid=f"i{i}") for i in range(limit)]
    d = t.decide(c, None, ctx(previous_attempts=inc), profile)
    assert d.kind == DecisionKind.BLOCKED and "repeated_failure" in d.rationale


def test_same_file_after_the_restructure_move_is_the_same_cluster(cfg, profile):
    """The cluster id changes when the restructure moves the file; the registry's move map keeps the attempts
    counted, so a released cluster gets `verify_extra_llm_attempts` more — not a fresh limit."""
    old, new = "src/main/java/a/Foo.java", "demo-back/src/main/java/a/Foo.java"
    before, after = one(old), one(new)
    assert before.id != after.id
    a = attempt(before, "no_change")
    assert not same_cluster(after, a)
    reg = BlockedRegistry()
    reg.note_moves({old: new})
    assert same_cluster(after, a, reg.canonical_file)
    t = Triager(cfg, registry=HandlerRegistry())
    t.attach_run(blocked_registry=reg)
    limit = max(cfg.loop.max_llm_attempts_per_cluster, cfg.loop.stall_limit + 1)
    prev = [attempt(before, "no_change", aid=f"x{i}") for i in range(limit)]
    d = t.decide(after, None, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.BLOCKED and "repeated_failure" in d.rationale


def test_partial_deterministic_option_is_not_chosen_again(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([Named("h1")]))
    c = one()
    prev = [attempt(c, "partial", kind=DecisionKind.HANDLER, handler_id="h1")]
    assert t.decide(c, None, ctx(previous_attempts=prev), profile).kind != DecisionKind.HANDLER


# ------------------------------------------------------------------------------------------------ A9
def hit(action: CaseAction, cluster, pinned: bool = True, case_id: str = "case-1") -> CaseHit:
    return CaseHit(case_id=case_id, name="Gone — cannot find symbol", action=action, errors=list(cluster.errors),
                   pinned=pinned)


def test_pinned_handler_is_tried_first(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([Named("generic"), Named("pinned-one", match=False)]))
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="handler", handler_id="pinned-one"), c)])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.HANDLER and d.handler_id == "pinned-one" and "case-1" in d.rationale
    # tried without effect → the normal order goes on
    prev = [attempt(c, "no_change", kind=DecisionKind.HANDLER, handler_id="pinned-one")]
    d = t.decide(c, m, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.HANDLER and d.handler_id == "generic"
    assert "already tried" in d.rationale


def test_unknown_pinned_handler_is_a_note(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="handler", handler_id="no-such-handler"), c)])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.LLM_FIX
    assert "pinned handler 'no-such-handler' of case case-1 is not registered" in d.rationale


def test_pinned_block_with_a_reason_value_never_asks_the_llm(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([Named("generic")]))
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="block", block_reason="unsupported_source",
                                          suggestion="generated by the XYZ plugin: regenerate"), c)])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.BLOCKED and d.rationale.startswith("blocked: unsupported_source")
    cand = t.blocked_candidate(c.id)
    assert cand.reason == BlockReason.UNSUPPORTED_SOURCE and cand.case_id == "case-1"
    assert cand.suggestion == "generated by the XYZ plugin: regenerate"


def test_pinned_block_with_free_text_uses_the_generic_reason(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="block", block_reason="vendor jar, team X owns it"), c)])
    d = t.decide(c, m, ctx(), profile)
    cand = t.blocked_candidate(c.id)
    assert d.kind == DecisionKind.BLOCKED and cand.reason == BlockReason.EXTERNAL_API_INCOMPATIBLE
    assert "pinned by case case-1" in d.rationale and "vendor jar, team X owns it" in d.rationale
    assert cand.suggestion == "vendor jar, team X owns it"


def test_pinned_recipe_ids(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([Named("generic")]))
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="recipe", recipe_ids=["KbFixGone"]), c)])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.KB_RECIPE and d.recipe_ids == ["KbFixGone"] and "pinned" in d.rationale
    prev = [attempt(c, "no_change", kind=DecisionKind.KB_RECIPE, recipe_ids=["KbFixGone"])]
    d = t.decide(c, m, ctx(previous_attempts=prev), profile)
    assert d.kind == DecisionKind.HANDLER and "already ran" in d.rationale


def test_unpinned_case_actions_keep_todays_behaviour(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry([Named("generic")]))
    c = one()
    m = KBMatch(case_hits=[hit(CaseAction(kind="block", block_reason="artifact_not_found"), c, pinned=False),
                           hit(CaseAction(kind="handler", handler_id="nope"), c, pinned=False)])
    d = t.decide(c, m, ctx(), profile)
    assert d.kind == DecisionKind.HANDLER and d.handler_id == "generic" and "pinned" not in d.rationale


def test_pinned_hit_only_for_errors_still_in_the_cluster(cfg, profile):
    t = Triager(cfg, registry=HandlerRegistry())
    c = one("src/main/java/a/Foo.java")
    other = one("src/main/java/a/Other.java", "compile|cannot_find_symbol|class Else")
    m = KBMatch(case_hits=[hit(CaseAction(kind="block", block_reason="unsupported_source"), other)])
    assert t.decide(c, m, ctx(), profile).kind == DecisionKind.LLM_FIX
