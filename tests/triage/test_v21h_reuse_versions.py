"""A14 (docs/v2/HANDOVER-FINDINGS-2026-10-28.md): `script.reuse_versions` > 1 — when the best stored version failed
in this run, the ladder runs the next stored version (proven newest first, then unverified newest; retired never)
before the LLM, up to `reuse_versions` stored versions per case and run. A reuse that helped goes to the augment."""
from __future__ import annotations

from types import SimpleNamespace

from sbmig.errors.cases import CaseStore
from sbmig.loop.entries import CaseScripts
from sbmig.triage import cluster_cases, plan_script_step
from sbmig.triage.scripts import stored_next

from .test_v2_cases import err, meta, script_att


class ListStore:
    """A case store with several versions: `best` (the store's own pick), `find(...).scripts`, `code`."""

    def __init__(self, scripts, codes):
        self.scripts_ = list(scripts)
        self.codes = dict(codes)

    def best(self, case_id):
        ranked = sorted((s for s in self.scripts_ if s.status != "retired"),
                        key=lambda s: (s.status == "proven", s.version), reverse=True)
        return (ranked[0], self.codes[ranked[0].version]) if ranked else None

    def find(self, case_id):
        return SimpleNamespace(scripts=list(self.scripts_))

    def code(self, case_id, version):
        return self.codes.get(version, "")


def store4() -> ListStore:
    # v4 proven, v2 proven, v3 unverified, v5 unverified, v1 retired, v6 unverified without code
    s = [meta(1, status="retired"), meta(2), meta(3, successes=0, status="unverified"), meta(4),
         meta(5, successes=0, status="unverified"), meta(6, successes=0, status="unverified")]
    return ListStore(s, {1: "c1", 2: "c2", 3: "c3", 4: "c4", 5: "c5"})


def test_next_version_order_is_proven_newest_then_unverified_newest():
    st = store4()
    order, done = [], set()
    while (nxt := stored_next(st, "cid", done)) is not None:
        order.append(nxt[0].version)
        done.add(nxt[0].version)
    assert order == [4, 2, 5, 3]                       # retired v1 and code-less v6 never
    assert stored_next(None, "cid", ()) is None and stored_next(st, "", ()) is None


def test_reuse_versions_one_keeps_one_stored_try(cfg):
    assert cfg.script.reuse_versions == 1
    [c] = cluster_cases([err()], None)
    st = plan_script_step(c, [script_att("reuse", 4, "no_change", c.id, code="c4")], store4(), cfg)
    assert st.mode == "augment" and st.base_version == 4


def test_reuse_versions_three_runs_the_next_stored_versions_first(cfg):
    cfg.script.reuse_versions = 3
    [c] = cluster_cases([err()], None)
    store = store4()
    st = plan_script_step(c, [], store, cfg)
    assert (st.mode, st.version, st.code) == ("reuse", 4, "c4")
    tried = [script_att("reuse", 4, "no_change", c.id, code="c4")]
    st = plan_script_step(c, tried, store, cfg)
    assert (st.mode, st.version, st.code) == ("reuse", 2, "c2") and not st.needs_llm
    assert "after v4 failed in this run (stored version 2/3)" in st.why
    assert "이번 실행에서 v4 실패 → 다음 저장본 저장된 스크립트 v2 (proven" in st.kb_line and st.kb_line.endswith("(2/3)")
    tried.append(script_att("reuse", 2, "regressed", c.id, code="c2"))
    st = plan_script_step(c, tried, store, cfg)
    assert (st.mode, st.version) == ("reuse", 5)       # no proven left: the newest unverified
    tried.append(script_att("reuse", 5, "apply_failed", c.id, code="c5"))
    st = plan_script_step(c, tried, store, cfg)        # 3 stored versions used → the LLM extends the last one
    assert st.mode == "augment" and st.base_version == 5 and st.stored_used == 3
    assert "이번 실행에서 v5 실패" in st.kb_line


def test_a_reuse_that_helped_is_augmented_not_replaced(cfg):
    cfg.script.reuse_versions = 3
    [c] = cluster_cases([err()], None)
    st = plan_script_step(c, [script_att("reuse", 4, "partial", c.id, code="c4")], store4(), cfg)
    assert st.mode == "augment" and st.base_version == 4


def test_with_the_real_case_store_and_the_loop_adapter(tmp_path, cfg):
    cfg.script.reuse_versions = 2
    [c] = cluster_cases([err()], None)
    kb = CaseStore(tmp_path / "kb", cfg)
    cid = c.case_id
    for i in (1, 2, 3):
        kb.add_script(cid, "compile|cannot_find_symbol|clientIp", f"def fix(p):\n    x = {i}\n", run_id="r0",
                      project="a")
    kb.credit(cid, 1, "improved", project="b", run_id="r1")          # v1 proven (another project)
    for store in (kb, CaseScripts(kb)):
        st = plan_script_step(c, [], store, cfg)
        assert (st.mode, st.version) == ("reuse", 1)
        st = plan_script_step(c, [script_att("reuse", 1, "no_change", c.id, code=st.code)], store, cfg)
        assert (st.mode, st.version) == ("reuse", 3) and "x = 3" in st.code
