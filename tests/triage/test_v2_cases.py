"""v2 triage: clustering by case (L2) across files, masking, the v2.1 fix ladder (docs/v2/PLAN-v2.1.md §3.1: KB
search → stored script → LLM augment / new, 1 stored + 3 LLM tries per case) and the decision engine (SCRIPT / BLOCKED
/ ESCALATE)."""
from __future__ import annotations

import hashlib

from sbmig.core.interfaces import TriageContext
from sbmig.core.models import (
    GAV,
    BlockReason,
    BuildError,
    CaseAction,
    CaseHit,
    DecisionKind,
    ErrorKind,
    FixAttempt,
    KBMatch,
    Phase,
    PomInfo,
    CaseScript,
    TriageDecision,
)
from sbmig.triage import Triager, case_id_of, cluster_cases, plan_iteration, plan_script_step
from sbmig.triage.cluster import l2_case_id as case_id_for

from .conftest import make_profile

L2 = "compile|cannot_find_symbol|clientIp"


def err(file: str | None = "src/main/java/a/A.java", *, l2: str = L2, kind=ErrorKind.COMPILE,
        module: str | None = "demo", msg="cannot find symbol", sig: str | None = None, artifact=None) -> BuildError:
    return BuildError(kind=kind, file=file, module=module, message=msg, artifact=artifact,
                      signature=sig or f"{kind.value}|{msg}|{file}", signature_generic=l2)


def ctx(**kw) -> TriageContext:
    return TriageContext(phase=kw.pop("phase", Phase.VERIFY), iteration=1, **kw)


class Store:
    """Minimal case store for the ladder (`kb.cases`: best → (CaseScript, code) | None, describe)."""

    def __init__(self, best=None, describe: str = ""):
        self._best, self._describe = best, describe

    def best(self, case_id):
        return self._best

    def describe(self, case_id):
        return self._describe


def meta(v: int, successes=1, status="proven") -> CaseScript:
    return CaseScript(version=v, file=f"fix_v{v}.py", status=status, successes=successes, projects=["p"] * successes)


def script_att(mode: str, version: int, outcome: str, cid: str, code: str = "def fix(p):\n    pass\n") -> FixAttempt:
    return FixAttempt(id=f"verify-1-{version}", phase=Phase.VERIFY, iteration=1, cluster_id=cid,
                      decision=TriageDecision(cluster_id=cid, kind=DecisionKind.SCRIPT), outcome=outcome,
                      script_mode=mode, script_version=version, script_code=code)


# ----------------------------------------------------------------------------------------- clustering
def test_cases_group_one_l2_across_files_with_the_catalog_id():
    errs = [err("src/main/java/a/A.java"), err("src/main/java/a/B.java"), err("src/main/java/a/A.java"),
            err("src/main/java/a/C.java", l2="compile|cannot_find_symbol|other")]
    cs = cluster_cases(errs, make_profile())
    big = next(c for c in cs if len(c.errors) == 3)
    assert big.case_id == case_id_for(L2) == hashlib.sha1(L2.encode()).hexdigest()[:10]
    assert big.id == f"case:{big.case_id}" and big.file is None and big.module == "demo"
    assert len(cs) == 2 and cs[0] is big                      # same priority → more errors first


def test_case_without_l2_falls_back_to_kind_file_and_l1():
    e = err("src/x/A.java", l2="", sig="compile|weird|A")
    expect = hashlib.sha1("compile|src/x/A.java|compile|weird|A".encode()).hexdigest()[:10]
    assert case_id_of(e) == expect
    assert case_id_of(err("src/x/B.java", l2="", sig="compile|weird|A")) != expect


def test_case_spanning_modules_has_no_module_and_the_earliest_priority():
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="root", is_root=True),
                              PomInfo(path="a/pom.xml", artifact_id="a"), PomInfo(path="b/pom.xml", artifact_id="b")])
    cs = cluster_cases([err("b/src/B.java", module="b"), err("a/src/A.java", module="a")], prof)
    [c] = cs
    assert c.module is None and c.priority == 50 + 1       # reactor index of module a


def test_scope_suffix_keeps_services_apart():
    [c] = cluster_cases([err()], None, scope="svc-a/")
    assert c.id == f"case:{case_id_for(L2)}@svc-a" and c.case_id == case_id_for(L2)


def test_triager_clusters_by_case(cfg):
    t = Triager(cfg)
    assert [c.case_id for c in t.cluster([err("A.java"), err("B.java")], None)] == [case_id_for(L2)]


def test_a_case_in_a_module_with_a_pom_problem_is_masked():
    dep = err(None, kind=ErrorKind.DEPENDENCY_RESOLUTION, module="b", msg="Could not find artifact g:a:1",
              l2="dependency_resolution|g:a", artifact=GAV.parse("g:a:1"))
    cs = cluster_cases([err("a/A.java", module="a"), err("b/B.java", module="b"), dep], None)
    items = {i.cluster.kind: i for i in plan_iteration(cs)}
    assert items[ErrorKind.COMPILE].skip is not None and items[ErrorKind.COMPILE].masked_by
    assert items[ErrorKind.DEPENDENCY_RESOLUTION].skip is None


# ----------------------------------------------------------------------------------------- the ladder
def test_ladder_new_when_nothing_is_stored_or_tried(cfg):
    [c] = cluster_cases([err()], None)
    st = plan_script_step(c, [], None, cfg)
    assert st.mode == "new" and st.needs_llm and st.llm_limit == 3
    assert st.kb_line == "KB 탐색: 저장된 스크립트 없음 (처음 보는 케이스) → LLM 에 새 수정 요청"


def test_ladder_runs_the_stored_script_once_then_augments_it(cfg):
    [c] = cluster_cases([err()], None)
    store = Store(best=(meta(3), "code3"), describe="저장된 스크립트 v3 (proven, 성공 1 / 실패 0, 프로젝트 1)")
    st = plan_script_step(c, [], store, cfg)
    assert (st.mode, st.version, st.code) == ("reuse", 3, "code3") and not st.needs_llm
    assert st.kb_line == "KB 탐색: 저장된 스크립트 v3 (proven, 성공 1 / 실패 0, 프로젝트 1) → 실행"
    ran = script_att("reuse", 3, "no_change", c.id, code="code3")
    st = plan_script_step(c, [ran], store, cfg)        # v3 failed here → the LLM extends it (the prompt gets v3)
    assert st.mode == "augment" and st.base_attempt is ran and st.base_version == 3
    assert "이번 실행에서 v3 실패" in st.kb_line and "LLM 보강 요청 (1/3" in st.kb_line
    cfg.script.reuse = False                           # reuse off: the stored version is the augment base
    st = plan_script_step(c, [], store, cfg)
    assert st.mode == "augment" and st.base_attempt is None and st.base_version == 3 and st.code == "code3"


def test_ladder_without_describe_builds_the_phrase_and_skips_retired(cfg):
    [c] = cluster_cases([err()], None)
    st = plan_script_step(c, [], Store(best=(meta(2, successes=3), "x")), cfg)
    assert st.kb_found == "저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 3)" and st.mode == "reuse"
    assert plan_script_step(c, [], Store(best=(meta(2, status="retired"), "x")), cfg).mode == "new"


def test_ladder_augments_this_runs_last_attempt_with_code(cfg):
    [c] = cluster_cases([err()], None)
    a1 = script_att("new", 6, "no_change", c.id)
    nocode = script_att("augment", 0, "apply_failed", c.id, code="")    # an llm-edit try: no script
    st = plan_script_step(c, [a1, nocode], None, cfg)
    assert st.mode == "augment" and st.base_attempt is a1 and st.llm_used == 2 and st.augments_used == 1


def test_ladder_limits_one_stored_plus_three_llm_tries(cfg):
    assert cfg.script.max_augments_per_case == 3
    [c] = cluster_cases([err()], None)
    tried = [script_att("reuse", 1, "no_change", c.id), script_att("augment", 2, "no_change", c.id),
             script_att("augment", 3, "no_change", c.id), script_att("augment", 4, "no_change", c.id)]
    st = plan_script_step(c, tried, Store(best=(meta(1), "c")), cfg)
    assert st.mode is None and "max_augments_per_case 3" in st.why
    assert "한도 도달 (저장본 1 + LLM 3회) → blocked" in st.kb_line
    # answers without a script open no augment: new again, inside the same 3
    empty = [script_att("new", 0, "apply_failed", c.id, code="") for _ in range(2)]
    st = plan_script_step(c, empty, None, cfg)
    assert st.mode == "new" and "(3/3)" in st.kb_line
    assert plan_script_step(c, empty + [script_att("new", 0, "apply_failed", c.id, code="")], None, cfg).mode is None


# ----------------------------------------------------------------------------------------- decisions
def engine(cfg, store=None) -> Triager:
    t = Triager(cfg)
    t.attach_run(cases=store)
    return t


def test_decision_is_a_script_with_its_step(cfg):
    t = engine(cfg)
    [c] = t.cluster([err()], None)
    d = t.decide(c, None, ctx(), make_profile())
    assert d.kind == DecisionKind.SCRIPT and "new fix" in d.rationale
    assert t.script_step(c.id).mode == "new"


def missing_artifact_cluster(t):
    e = err(None, kind=ErrorKind.DEPENDENCY_RESOLUTION, msg="Could not find artifact com.x:lib:jar:1.0 in central",
            l2="dependency_resolution|com.x:lib", artifact=GAV.parse("com.x:lib:1.0"))
    return t.cluster([e], None)[0]


def test_reuse_comes_before_the_blocked_policy_llm_and_budget(cfg):
    store = Store(best=(meta(2), "stored"))
    t = engine(cfg, store)
    c = missing_artifact_cluster(t)                  # policy artifact_not_found: 0 LLM attempts
    t.set_llm_unavailable("down")
    d = t.decide(c, None, ctx(llm_calls_used=99, llm_calls_budget=1), make_profile())
    assert d.kind == DecisionKind.SCRIPT and d.rationale.startswith("reuse: stored script v2")


def test_blocked_policy_counts_llm_script_failures(cfg):
    t = engine(cfg)
    c = missing_artifact_cluster(t)
    assert cfg.loop.blocked_policy["artifact_not_found"] == 2          # v2 default: the script may fix the pom
    cfg.loop.blocked_policy["artifact_not_found"] = 0
    d = t.decide(c, None, ctx(), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "policy: no LLM script" in d.rationale
    cfg.loop.blocked_policy["artifact_not_found"] = 1
    d = t.decide(c, None, ctx(), make_profile())
    assert d.kind == DecisionKind.SCRIPT and "LLM script attempt 1/1 before blocking" in d.rationale
    reuse_failed = [script_att("reuse", 1, "no_change", c.id)]           # a reuse is no LLM attempt
    assert t.decide(c, None, ctx(previous_attempts=reuse_failed), make_profile()).kind == DecisionKind.SCRIPT
    failed = [script_att("new", 1, "no_change", c.id)]
    d = t.decide(c, None, ctx(previous_attempts=failed), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "after 1 LLM script attempt(s)" in d.rationale


def test_llm_unavailable_budget_and_exhausted_ladder_block(cfg):
    t = engine(cfg)
    [c] = t.cluster([err()], None)
    d = t.decide(c, None, ctx(llm_calls_used=5, llm_calls_budget=5), make_profile())
    assert d.kind == DecisionKind.BLOCKED and t.blocked_candidate(c.id).reason == BlockReason.BUDGET_EXHAUSTED
    # v2.1: 0 = no limit; -1 = no LLM call at all (the fleet's total is used up)
    assert t.decide(c, None, ctx(llm_calls_used=500, llm_calls_budget=0), make_profile()).kind == DecisionKind.SCRIPT
    d = t.decide(c, None, ctx(llm_calls_used=0, llm_calls_budget=-1), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "LLM budget exhausted (fleet)" in d.rationale
    t.attach_run(cases=Store(best=(meta(2), "stored")))       # a stored script still runs without the LLM
    assert t.decide(c, None, ctx(llm_calls_budget=-1), make_profile()).kind == DecisionKind.SCRIPT
    t.engine.cases = None
    cfg.script.max_augments_per_case = 0
    d = t.decide(c, None, ctx(previous_attempts=[script_att("new", 1, "no_change", c.id)]), make_profile())
    assert d.kind == DecisionKind.BLOCKED and t.blocked_candidate(c.id).reason == BlockReason.REPEATED_FAILURE
    t.set_llm_unavailable("gateway down")
    d = t.decide(c, None, ctx(), make_profile())
    assert d.kind == DecisionKind.BLOCKED and t.blocked_candidate(c.id).reason == BlockReason.LLM_UNAVAILABLE


def test_already_blocked_and_pinned_block(cfg):
    from sbmig.loop.blocked_registry import BlockedRegistry
    t = engine(cfg)
    [c] = t.cluster([err()], None)
    reg = BlockedRegistry()
    t.attach_run(blocked_registry=reg)
    hit = CaseHit(case_id="abc", name="clientIp", action=CaseAction(kind="block", block_reason="artifact_not_found"),
                  pinned=True, errors=list(c.errors))
    d = t.decide(c, KBMatch(case_hits=[hit]), ctx(), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "no script, no LLM" in d.rationale
    assert t.blocked_candidate(c.id).reason == BlockReason.ARTIFACT_NOT_FOUND
    reg.block(c, BlockReason.REPEATED_FAILURE, Phase.VERIFY)
    d = t.decide(c, None, ctx(), make_profile())
    assert d.kind == DecisionKind.BLOCKED and "already blocked" in d.rationale


def test_a_pinned_hint_action_keeps_the_script_ladder(cfg):
    """Only `block` and `script` pins are instructions; a pinned `hint` (the auto default) changes nothing."""
    t = engine(cfg)
    [c] = t.cluster([err()], None)
    hit = CaseHit(case_id="abc", action=CaseAction(kind="hint"), pinned=True)
    assert t.decide(c, KBMatch(case_hits=[hit]), ctx(), make_profile()).kind == DecisionKind.SCRIPT
