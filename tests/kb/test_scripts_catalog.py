"""v2 case catalog + error records over the fix-script KB: the auto action `script` (best proven version), its
promotion, the hint, script fixes grouped per version, no action without a proven script, the view columns, and the
script fields of the error / attempt records (prepare, CSV)."""
from __future__ import annotations

import csv

from sbmig.core.models import CaseAction, ErrorAttemptRef, FixAttemptRecord
from sbmig.kb import FileKnowledgeBase, prepare_error_cases
from sbmig.kb.catalog import CASE_CSV_COLUMNS, action_text, case_id_for
from sbmig.kb.error_cases import ATTEMPT_CSV_COLUMNS, write_error_cases_csv, write_fix_attempts_csv

from .helpers import EC_L2, ec_attempt, ec_error
from .test_catalog import add, rec, unresolved
from .test_scripts import FIX_A, FIX_B

CID = case_id_for(EC_L2)
RES1 = f"script:{CID}:v1"


def by_script(eid: str, run: str, project: str, *, method: str = "script", version: int = 1, ok: bool = True,
              hours: int = 0):
    resolver = f"script:{CID}:v{version}"
    a = ErrorAttemptRef(attempt_id=f"{run}:a1", fix_method=method, resolver_id=resolver,
                        outcome="improved" if ok else "no_change", script_version=version,
                        failure="" if ok else "the targeted error(s) were still there after the rebuild")
    if ok:
        return rec(eid, run=run, project=project, method=method, resolver=resolver, attempts=[a], pairs=(),
                   change_kind="code_edit", description="LLM gpt: rewrote the call", hours=hours,
                   script_case_id=CID, script_version=version)
    return rec(eid, run=run, project=project, status="unresolved", attempts=[a], hours=hours)


# --------------------------------------------------------------------------- the script action
def test_script_action_promotion_hint_and_fix_rows(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    st = kb.scripts
    st.add(CID, EC_L2, FIX_A, analysis="LegacyRequestHelper.clientIp became remoteClientIp", run_id="r1",
           project="alpha")
    add(kb, unresolved("e0", "r0", "zeta"))
    c = kb.catalog().get(CID)
    assert (c.action.kind, c.status) == ("hint", "observed")            # stored, not proven yet
    st.credit(CID, 1, "improved", project="alpha", run_id="r1")
    add(kb, by_script("e1", "r1", "alpha", hours=1))
    c = kb.catalog().get(CID)
    assert c.action == CaseAction(kind="script", script_version=1) and c.status == "candidate"
    assert action_text(c.action) == "script v1"
    assert c.hints[0].startswith("stored fix script v1 fixed it in 1 project(s) (1 ok / 0 failed): "
                                 "LegacyRequestHelper.clientIp became remoteClientIp")
    # project beta reuses v1 (no LLM call): one fix row per script version, whatever the mode
    st.credit(CID, 1, "build_ok", project="beta", run_id="r2")
    add(kb, by_script("e2", "r2", "beta", method="script_reuse", hours=2))
    c = kb.catalog().get(CID)
    assert c.status == "active"                                         # 2 projects (kb.auto_promote_after_projects)
    rows = [(f.method, f.resolver_id, f.count, f.projects) for f in c.evidence.fixes]
    assert rows == [("script", RES1, 2, 2)]
    assert c.evidence.resolved_by == {"script": 1, "script_reuse": 1}
    # a failure of that version demotes it to candidate; the failure row keeps the method
    st.credit(CID, 1, "no_change", project="gamma", run_id="r3")
    add(kb, by_script("e3", "r3", "gamma", method="script_reuse", ok=False, hours=3))
    c = kb.catalog().get(CID)
    assert c.status == "candidate" and c.action.script_version == 1
    assert ("script_reuse", "no_change") in [(x.method, x.outcome) for x in c.evidence.failures]
    assert [e.attempts for e in c.evidence.timeline][-3:] == [["script v1:improved"], ["script_reuse v1:improved"],
                                                             ["script_reuse v1:no_change"]]
    # a better version (more projects) takes over the action
    st.add(CID, EC_L2, FIX_B, parent=1)
    for p in ("alpha", "beta", "gamma"):
        st.credit(CID, 2, "improved", project=p)
    kb.catalog().refresh([EC_L2])
    assert kb.catalog().get(CID).action.script_version == 2
    # the audit log has the transitions with the action text
    auto = [a for a in kb.catalog().iter_audit(CID) if a["who"] == "sbmig" and a["event"] in ("status", "action")]
    assert ("candidate", "script v1") in [(a["after"]["status"], a["after"]["action"]) for a in auto]
    assert auto[-1]["after"]["action"] == "script v2"


def test_incremental_equals_rebuild_with_scripts(v2_kb_cfg):
    from .test_catalog import snapshot

    kb = FileKnowledgeBase(v2_kb_cfg)
    kb.scripts.add(CID, EC_L2, FIX_A)
    kb.scripts.credit(CID, 1, "improved", project="alpha")
    add(kb, by_script("e1", "r1", "alpha"))
    add(kb, unresolved("e2", "r2", "beta", hours=1))
    cat = kb.catalog()
    inc = snapshot(cat)
    cat.rebuild()
    assert snapshot(cat) == inc


def test_no_action_without_a_proven_script(v2_kb_cfg):
    """PLAN-v2 step 4: LLM text replaces in two projects (and v1 recipe / patch files left in an old KB) give no
    action — hint only; a proven script is the action."""
    kb = FileKnowledgeBase(v2_kb_cfg)
    for rel in ("recipes/active/RenameIt.yml", "cases/2026-10/20261020-abc.json", "index.json"):
        p = kb.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n", encoding="utf-8")
    add(kb, rec("e1"), rec("e2", run="r2", project="beta", hours=1))
    c = kb.catalog().get(CID)
    assert (c.action.kind, c.status) == ("hint", "observed")
    assert c.evidence.fixes and c.hints[0].startswith("past runs: fixed by llm fake-model")
    kb.scripts.add(CID, EC_L2, FIX_A)
    kb.catalog().refresh([EC_L2])
    assert kb.catalog().get(CID).action.kind == "hint"                      # stored, not proven
    kb.scripts.credit(CID, 1, "improved", project="alpha")
    kb.catalog().refresh([EC_L2])
    assert kb.catalog().get(CID).action == CaseAction(kind="script", script_version=1)


def test_views_have_script_columns(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    kb.scripts.add(CID, EC_L2, FIX_A)
    kb.scripts.add(CID, EC_L2, FIX_B, parent=1)
    kb.scripts.credit(CID, 2, "improved", project="alpha")
    add(kb, by_script("e1", "r1", "alpha", version=2))
    add(kb, rec("o1", l2="compile|cannot_find_symbol|Other", l1="compile|cannot find symbol|class Other",
                status="unresolved", location=None, symbol="class Other", error_symbol="Other"))
    md_path, csv_path = kb.catalog().write_views()
    md = md_path.read_text(encoding="utf-8")
    assert "| id | name | status | action | seen | projects | resolved by | latest fix | top failure | scripts | " \
           "best script |" in md
    row = next(ln for ln in md.splitlines() if ln.startswith(f"| `{CID}`"))
    assert "| script v2 |" in row and row.endswith("| 2 | v2 (1 ok / 0 failed, 1 project(s)) |")
    table = list(csv.reader(csv_path.open(encoding="utf-8-sig", newline="")))
    assert table[0] == CASE_CSV_COLUMNS and CASE_CSV_COLUMNS[-2:] == ["scripts", "best_script"]
    rows = {r[0]: dict(zip(table[0], r)) for r in table[1:]}
    assert rows[CID]["scripts"] == "2" and rows[CID]["best_script"].startswith("v2 ")
    other = case_id_for("compile|cannot_find_symbol|Other")
    assert rows[other]["scripts"] == "0" and rows[other]["best_script"] == ""
    # a person's pin is marked
    kb.catalog().set_human(CID, action=CaseAction(kind="script", script_version=1))
    rows = {r["id"]: r for r in kb.catalog().view_rows()}
    assert rows[CID]["best_script"].startswith("v1* ") and rows[CID]["action"] == "script v1*"


# --------------------------------------------------------------------------- error / attempt records
def test_prepare_passes_script_fields_through_and_masks_the_log(v2_kb_cfg, ws, profile, tmp_path):
    att = ec_attempt(fix_method="script_augment", resolver_id=RES1, diff="", script_mode="augment",
                     script_case_id=CID, script_version=3, script_parent=2, script_status="ok",
                     script_error="", script_log=[f"line {i}" for i in range(80)] + ["db.password=hunter2"])
    att.script_log = ["spring.datasource.password=hunter2"] + att.script_log[:60]
    failed = ec_attempt("a0", fix_method="script", outcome="apply_failed", diff="", script_mode="new",
                        script_case_id=CID, script_version=2, script_status="error",
                        script_error=f"KeyError at {ws.root}/src/A.java: token=abc123def")
    err = ec_error(fix_method="script_augment", resolver_id=RES1,
                   attempts=[ErrorAttemptRef(attempt_id="r1:a0", fix_method="script", outcome="apply_failed"),
                             ErrorAttemptRef(attempt_id="r1:a1", fix_method="script_augment", outcome="improved")])
    errors, attempts = prepare_error_cases([err], [failed, att], profile, ws, v2_kb_cfg)
    e = errors[0]
    a0, a1 = attempts
    assert (a1.script_mode, a1.script_case_id, a1.script_version, a1.script_parent, a1.script_status) == (
        "augment", CID, 3, 2, "ok")
    assert len(a1.script_log) == 50 and a1.script_log[0] == "spring.datasource.password=***"
    assert "abc123def" not in a0.script_error and str(ws.root) not in a0.script_error and a0.script_status == "error"
    assert a1.schema_version == e.schema_version == 3
    # the resolving attempt's script → the error; the history refs get their versions
    assert (e.script_case_id, e.script_version) == (CID, 3)
    assert [r.script_version for r in e.attempts] == [2, 3]
    assert e.llm_attempts == 2
    # CSV columns
    path = write_error_cases_csv(errors, tmp_path / "e.csv")
    table = list(csv.reader(path.open(encoding="utf-8-sig", newline="")))
    row = dict(zip(table[0], table[1]))
    assert (row["script_case_id"], row["script_version"]) == (CID, "3") and table[0][-1] == "diff_excerpt"
    path = write_fix_attempts_csv(attempts, tmp_path / "a.csv")
    table = list(csv.reader(path.open(encoding="utf-8-sig", newline="")))
    assert table[0] == ATTEMPT_CSV_COLUMNS and table[0][-1] == "diff"
    row = dict(zip(table[0], table[2]))
    assert (row["script_mode"], row["script_version"], row["script_parent"], row["script_status"]) == (
        "augment", "3", "2", "ok")
    assert row["script_log"].startswith("spring.datasource.password=***\nline 0")


def test_reuse_attempt_is_no_llm_attempt(v2_kb_cfg, ws, profile):
    att = ec_attempt(fix_method="script_reuse", resolver_id=RES1, script_mode="reuse", script_case_id=CID,
                     script_version=1)
    err = ec_error(fix_method="script_reuse", resolver_id=RES1,
                   attempts=[ErrorAttemptRef(attempt_id="r1:a1", fix_method="script_reuse", outcome="improved")])
    errors, _ = prepare_error_cases([err], [att], profile, ws, v2_kb_cfg)
    assert errors[0].llm_attempts == 0 and errors[0].script_version == 1


def test_a_minimal_attempt_never_keeps_the_raw_log(v2_kb_cfg, ws, profile, monkeypatch):
    import sbmig.kb.error_cases as mod

    monkeypatch.setattr(mod, "_prepare_attempt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    att = FixAttemptRecord(id="r1:a1", fix_method="script", script_log=["password=hunter2"],
                           script_error="password=hunter2")
    _, attempts = prepare_error_cases([], [att], profile, ws, v2_kb_cfg)
    assert attempts[0].script_log == [] and attempts[0].script_error == ""
