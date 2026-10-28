"""v2.1i `fix.mode: edits`: the ladder never reuses or augments — `new` while the case's LLM tries of this run are
below script.max_augments_per_case (= script mode's LLM asks), then BLOCKED repeated_failure; the store is not consulted; the blocked
policy keeps counting the same LLM tries. Script mode is unchanged."""
from __future__ import annotations

from sbmig.core.models import DecisionKind
from sbmig.triage import Triager, cluster_cases, plan_script_step
from sbmig.triage.scripts import EDITS_MODE_FOUND, edits_llm_limit, edits_mode, fix_mode

from .test_blocked import LIB_SYMBOL, cases, script_attempt
from .test_v2_cases import Store, ctx, err, meta, script_att


class Spy(Store):
    """A store that records whether the ladder looked at it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.asked = 0

    def best(self, case_id):
        self.asked += 1
        return super().best(case_id)

    def describe(self, case_id):
        self.asked += 1
        return super().describe(case_id)


def test_mode_helpers(cfg):
    assert (fix_mode(cfg), edits_mode(cfg)) == ("script", False)
    assert fix_mode(object()) == "script" and edits_llm_limit(object()) == 3      # a config without the sections
    cfg.fix.mode = "edits"
    assert (fix_mode(cfg), edits_mode(cfg), edits_llm_limit(cfg)) == ("edits", True, 3)
    cfg.script.max_augments_per_case = 1
    assert edits_llm_limit(cfg) == 1


def test_edits_ladder_is_new_until_the_budget_then_none_and_never_reads_the_store(cfg):
    cfg.fix.mode = "edits"
    [c] = cluster_cases([err()], None)
    store = Spy(best=(meta(1, successes=5), "def fix(p):\n    pass\n"), describe="저장된 스크립트 v1 (proven)")
    st = plan_script_step(c, [], store, cfg)
    assert (st.mode, st.llm_used, st.llm_limit, st.base_attempt, st.code, st.meta) == ("new", 0, 3, None, "", None)
    assert st.kb_found == EDITS_MODE_FOUND and st.kb_line == f"KB 탐색: {EDITS_MODE_FOUND} → LLM 에 수정 요청 (1/3)"
    assert store.asked == 0                                                  # the proven script is not consulted
    # failed edits tries (no script code, script_mode new) → new again, with the try count
    tried = [script_att("new", 0, "no_change", c.id, code=""), script_att("new", 0, "apply_failed", c.id, code="")]
    st = plan_script_step(c, tried, store, cfg)
    assert st.mode == "new" and st.llm_used == 2 and "(3/3, 앞 시도 2건 포함)" in st.kb_line
    # a previous try WITH code (a plugin that sent a script anyway) still opens no augment
    st = plan_script_step(c, [script_att("new", 0, "partial", c.id)], store, cfg)
    assert st.mode == "new" and st.base_attempt is None
    # pending proves nothing
    st = plan_script_step(c, tried + [script_att("new", 0, "pending", c.id, code="")], store, cfg)
    assert st.llm_used == 2
    four = tried + [script_att("new", 0, "regressed", c.id, code=""), script_att("new", 0, "inconclusive", c.id)]
    st = plan_script_step(c, four, store, cfg)
    assert st.mode is None and "fix.mode edits: script.max_augments_per_case = 3" in st.why
    assert st.kb_line == f"KB 탐색: {EDITS_MODE_FOUND} → 한도 도달 (LLM 4회) → blocked"
    assert store.asked == 0


def test_script_mode_ladder_is_unchanged(cfg):
    [c] = cluster_cases([err()], None)
    st = plan_script_step(c, [], Store(best=(meta(1), "code"), describe="저장된 스크립트 v1"), cfg)
    assert st.mode == "reuse" and st.version == 1 and st.kb_line == "KB 탐색: 저장된 스크립트 v1 → 실행"
    st = plan_script_step(c, [script_att("new", 6, "no_change", c.id)], None, cfg)
    assert st.mode == "augment" and st.llm_limit == 3


def test_decide_in_edits_mode_never_reuses_and_blocks_repeated_failure(cfg, profile):
    cfg.fix.mode = "edits"
    t = Triager(cfg)
    [c] = cluster_cases([err()], None)
    t.attach_run(cases=Store(best=(meta(1, successes=9), "def fix(p):\n    pass\n")))
    d = t.decide(c, None, ctx(), profile)
    assert d.kind == DecisionKind.SCRIPT and t.script_step(c.id).mode == "new"
    assert d.rationale == f"new edits for case {c.case_id} (fix.mode edits, request 1/3)"
    four = [script_att("new", 0, "no_change", c.id, code="") for _ in range(4)]
    d = t.decide(c, None, ctx(previous_attempts=four), profile)
    assert d.kind == DecisionKind.BLOCKED and d.rationale.startswith("blocked: repeated_failure — 4 LLM tries")
    assert t.blocked_candidate(c.id).reason.value == "repeated_failure"


def test_the_blocked_policy_counts_edits_tries_like_llm_script_tries(cfg, profile):
    cfg.fix.mode = "edits"
    cfg.loop.blocked_policy["external_api_incompatible"] = 1
    t = Triager(cfg)
    c = cases(LIB_SYMBOL)
    d = t.decide(c, None, ctx(), profile)
    assert d.kind == DecisionKind.SCRIPT and "external_api_incompatible" in d.rationale and "1/1" in d.rationale
    edits_try = script_attempt(c, mode="new").model_copy(update={"script_version": 0, "script_code": "",
                                                                  "source": "llm-edit"})
    d = t.decide(c, None, ctx(previous_attempts=[edits_try]), profile)
    assert d.kind == DecisionKind.BLOCKED and "after 1 LLM script attempt" in d.rationale
