"""sbmig.kb.catalog: one reusable case per L2 signature — auto rules (name, variants, evidence, timeline, action /
status priority, promotion, fallback, hints), the human block, match, incremental == rebuild, views, caching."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from sbmig.core.config import Config
from sbmig.core.models import (
    KB_RECORD_SCHEMA,
    BuildError,
    CaseAction,
    ErrorAttemptRef,
    ErrorCaseRecord,
    ErrorKind,
    KBRecipe,
    ReplacePair,
)
from sbmig.kb import FileKnowledgeBase
from sbmig.kb.catalog import (
    CASE_CSV_COLUMNS,
    CaseCatalog,
    case_id_for,
    owner_of,
    slug,
    split_l2,
)

from .helpers import EC_FILE, EC_L2, make_case, recipe_yaml, with_kb

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
L1_REQ = "compile|cannot find symbol|method clientIp()|class *"
L1_WRAP = "compile|cannot find symbol|method clientIp()|class com.lib.RequestWrapper"
CID = case_id_for(EC_L2)


def ref(aid: str, method: str = "llm", outcome: str = "improved", failure: str = "", resolver: str = "") -> ErrorAttemptRef:
    return ErrorAttemptRef(attempt_id=aid, fix_method=method, outcome=outcome, failure=failure,
                           resolver_id=resolver or ("fake-model" if method == "llm" else ""))


def rec(eid: str, *, run: str = "r1", project: str = "alpha", status: str = "resolved", method: str = "llm",
        resolver: str = "fake-model", l2: str = EC_L2, l1: str = L1_REQ, location: str = "class com.example.Request",
        pairs=(("clientIp", "remoteClientIp"),), change_kind: str = "text_replace", attempts=None, hours: int = 0,
        description: str = "LLM fake-model: renamed clientIp to remoteClientIp", **kw) -> ErrorCaseRecord:
    resolved = status in ("resolved", "resolved_indirectly")
    if attempts is None:
        attempts = [ref(f"{run}:a1", method, "improved", resolver=resolver)] if resolved else []
    data = dict(
        id=eid, at=T0 + timedelta(hours=hours), project=project, project_fingerprint=f"fp-{project}", run_id=run,
        error_kind=ErrorKind.COMPILE, message="cannot find symbol", file=EC_FILE, line=5,
        symbol="method clientIp()", location=location, signature=l1, signature_generic=l2, error_symbol="clientIp",
        status=status, fix_method=method if resolved else "none", resolver_id=resolver if resolved else "",
        resolved_by=f"{run}:a1" if resolved else None, targeted=status == "resolved",
        description=description if resolved else "",
        change_kind=change_kind if resolved else "none",
        replace_pairs=[ReplacePair(old=o, new=n) for o, n in pairs] if resolved else [],
        replace_rule_candidate=status == "resolved" and method == "llm" and change_kind == "text_replace",
        failure_reason="" if resolved else "llm attempt a0 no_change: still there", attempts=attempts,
        attempt_count=len(attempts), case_id=case_id_for(l2))
    data.update(kw)
    return ErrorCaseRecord(**data)


def failed_then_fixed(eid: str, run: str, project: str, hours: int = 0) -> ErrorCaseRecord:
    return rec(eid, run=run, project=project, hours=hours,
               attempts=[ref(f"{run}:a0", "llm", "apply_failed", "search text not found in IpFilter.java"),
                         ref(f"{run}:a1", "llm", "improved")])


def unresolved(eid: str, run: str, project: str, hours: int = 0, outcome: str = "no_change") -> ErrorCaseRecord:
    return rec(eid, run=run, project=project, status="unresolved", hours=hours,
               attempts=[ref(f"{run}:a0", "llm", outcome, "the targeted error(s) were still there after the rebuild")])


def by_case_replace(eid: str, run: str, project: str, ok: bool = True, hours: int = 0) -> ErrorCaseRecord:
    a = ref(f"{run}:a1", "handler", "improved" if ok else "no_change",
            "" if ok else "the targeted error(s) were still there after the rebuild", resolver="case-replace")
    if ok:
        return rec(eid, run=run, project=project, method="handler", resolver="case-replace", attempts=[a],
                   change_kind="text_replace", description="handler `case-replace`: replaced clientIp", hours=hours)
    return rec(eid, run=run, project=project, status="unresolved", attempts=[a], hours=hours)


def add(kb: FileKnowledgeBase, *records: ErrorCaseRecord) -> list[str]:
    """Append like the orchestrator does (events first, then the catalog)."""
    kb.error_case_store().append(list(records), [])
    return kb.catalog().update_from(list(records), [])


def snapshot(cat: CaseCatalog) -> dict:
    """Every case file (updated_at lines dropped) + the index (updated_at dropped)."""
    files = {p.relative_to(cat.catalog_dir).as_posix(): "\n".join(
        ln for ln in p.read_text(encoding="utf-8").splitlines() if not ln.startswith("updated_at:"))
        for p in sorted(cat.catalog_dir.rglob("*.yml"))}
    idx = json.loads(cat.index_path.read_text(encoding="utf-8"))
    idx.pop("updated_at")
    return {"files": files, "index": idx}


# --------------------------------------------------------------------------- helpers
def test_case_id_and_l2_helpers():
    assert case_id_for("") == "" and case_id_for(None) == ""
    assert CID == hashlib.sha1(EC_L2.encode()).hexdigest()[:10] and len(CID) == 10
    assert split_l2(EC_L2) == ("compile", "cannot_find_symbol", "clientIp")
    assert split_l2("dependency_resolution|com.x:lib") == ("dependency_resolution", "", "com.x:lib")
    assert split_l2("compile|build|fatal error compiling") == ("compile", "build", "fatal error compiling")
    assert split_l2("compile|incompatible_types|Query->Query") == ("compile", "incompatible_types", "Query->Query")
    assert owner_of("class com.example.Request") == "com.example.Request"
    assert owner_of("variable req of type com.lib.Wrapper<java.lang.String>") == "com.lib.Wrapper"
    assert owner_of("package org.springframework.web") == "org.springframework.web" and owner_of(None) == ""
    assert slug("com.x:lib") == "com.x-lib" and slug("") == "unknown" and slug("a -- b/c") == "a-b-c"


# --------------------------------------------------------------------------- auto rules
def test_name_variants_evidence_and_timeline(kb):
    # project alpha fails (LLM no_change, unresolved), project beta fixes it after a failed attempt, gamma too
    add(kb, unresolved("a-1", "r1", "alpha", hours=0))
    add(kb, failed_then_fixed("b-1", "r2", "beta", hours=1),
        rec("b-2", run="r2", project="beta", l1=L1_WRAP, location="variable w of type com.lib.RequestWrapper",
            hours=1))
    add(kb, rec("c-1", run="r3", project="gamma", hours=2))
    cat = kb.catalog()
    c = cat.get(CID)
    assert c is not None and c.id == CID and c.signature_generic == EC_L2
    assert (c.error_kind, c.error_class, c.symbol) == (ErrorKind.COMPILE, "cannot_find_symbol", "clientIp")
    assert c.name == "Request.clientIp — cannot find symbol"
    assert [(v.owner, v.signature, v.seen, v.projects) for v in c.variants] == [
        ("com.example.Request", L1_REQ, 3, 3), ("com.lib.RequestWrapper", L1_WRAP, 1, 1)]
    ev = c.evidence
    assert (ev.seen, ev.projects, ev.runs) == (4, 3, 3)
    assert ev.first_at == T0 and ev.last_at == T0 + timedelta(hours=2)
    assert ev.by_status == {"resolved": 3, "unresolved": 1} and ev.resolved_by == {"llm": 3}
    assert len(ev.fixes) == 1
    f = ev.fixes[0]
    assert (f.method, f.resolver_id, f.change_kind, f.count, f.projects) == ("llm", "fake-model", "text_replace", 3, 2)
    assert [(p.old, p.new) for p in f.replace_pairs] == [("clientIp", "remoteClientIp")]
    assert f.description == "renamed clientIp to remoteClientIp"          # the "LLM <model>: " prefix is dropped
    assert (f.attempt_id, f.error_id, f.project) == ("r3:a1", "c-1", "gamma")   # the latest one as the example
    assert [(x.method, x.outcome, x.count, x.projects, x.project) for x in ev.failures] == [
        ("llm", "no_change", 1, 1, "alpha"), ("llm", "apply_failed", 1, 1, "beta")]
    # the timeline shows the failure in alpha before the fix in beta (oldest first, attempts in order)
    assert [(e.project, e.status, e.attempts) for e in ev.timeline] == [
        ("alpha", "unresolved", ["llm:no_change"]),
        ("beta", "resolved", ["llm:apply_failed", "llm:improved"]),
        ("beta", "resolved", ["llm:improved"]),
        ("gamma", "resolved", ["llm:improved"]),
    ]
    assert ev.timeline[1].error_id == "b-1" and ev.timeline[1].resolved_by == "r2:a1" and ev.timeline[1].file == EC_FILE
    # learned text replace → replace candidate; hints use the error-hint wording
    assert c.status == "candidate" and c.action.kind == "replace"
    assert [(p.old, p.new, p.count) for p in c.action.replace_pairs] == [("clientIp", "remoteClientIp", 3)]
    assert c.action.replace_scope == "error-lines"
    assert c.hints == [
        "past runs: fixed by llm fake-model in 2 project(s): renamed clientIp to remoteClientIp; "
        "replace `clientIp` → `remoteClientIp`",
        "past failure ×1: llm no_change — the targeted error(s) were still there after the rebuild; do not repeat it",
    ]
    # file layout + index
    assert cat.path_of(CID) == kb.root / "catalog" / "compile" / "cannot_find_symbol" / f"clientIp--{CID}.yml"
    idx = json.loads(cat.index_path.read_text())
    assert idx["version"] == 1 and idx["cases"] == {CID: f"compile/cannot_find_symbol/clientIp--{CID}.yml"}
    assert idx["by_l2"] == {EC_L2: CID} and idx["by_l1"] == {L1_REQ: CID, L1_WRAP: CID}
    assert idx["by_symbol"] == {"clientIp": [CID]}


def test_timeline_keeps_the_last_twenty_oldest_first(kb):
    add(kb, *[unresolved(f"e{i:02}", f"r{i:02}", f"p{i:02}", hours=30 - i) for i in range(25)])
    tl = kb.catalog().get(CID).evidence.timeline
    assert len(tl) == 20 and [e.error_id for e in tl] == [f"e{i:02}" for i in range(19, -1, -1)]


def test_other_kinds_name_and_layout(kb):
    dep = "dependency_resolution|com.x:legacy-lib"
    add(kb, rec("d-1", l2=dep, l1=dep + ":1.0", location=None, status="unresolved", error_symbol="com.x:legacy-lib",
                error_kind=ErrorKind.DEPENDENCY_RESOLUTION, file=None, line=None))
    c = kb.catalog().get(case_id_for(dep))
    assert c.name == "com.x:legacy-lib — dependency resolution" and c.error_class == "" and c.status == "observed"
    assert kb.catalog().path_of(c.id).relative_to(kb.root).as_posix() == \
        f"catalog/dependency_resolution/other/com.x-legacy-lib--{c.id}.yml"


def test_action_recipe_wins_and_follows_its_status(kb):
    add(kb, rec("e1"))                                               # would be a replace candidate on its own
    kb.add_recipe(recipe_yaml("RenameClientIp"),
                  KBRecipe(id="RenameClientIp", path="", signatures=[L1_REQ]))
    cat = kb.catalog()
    assert cat.refresh([EC_L2]) == [CID]
    c = cat.get(CID)
    assert (c.action.kind, c.action.recipe_ids, c.status) == ("recipe", ["RenameClientIp"], "candidate")
    kb.promote("RenameClientIp")
    assert cat.refresh_signatures([L1_REQ]) == [CID]
    assert cat.get(CID).status == "active"
    kb.reject("RenameClientIp")
    cat.refresh([EC_L2])
    assert cat.get(CID).action.kind == "replace"                     # a rejected recipe no longer counts


def test_action_patch_for_replayable_llm_patch_cases(kb):
    l2 = "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"
    kid = kb.record_case(make_case(signatures_generic=[l2]))
    add(kb, rec("e1", l2=l2, change_kind="code_edit", pairs=()))     # a code edit: no learned replace
    c = kb.catalog().get(case_id_for(l2))
    assert (c.action.kind, c.action.patch_case_ids, c.status) == ("patch", [kid], "candidate")


def test_plain_hint_observed(kb):
    add(kb, unresolved("e1", "r1", "alpha"))
    c = kb.catalog().get(CID)
    assert (c.action.kind, c.status) == ("hint", "observed")
    assert c.hints == ["past failure ×1: llm no_change — the targeted error(s) were still there after the rebuild; "
                       "do not repeat it"]
    assert c.evidence.fixes == []


def test_promotion_after_distinct_project_successes(kb, kb_cfg):
    add(kb, rec("e1", run="r1", project="alpha"))
    cat = kb.catalog()
    assert cat.get(CID).status == "candidate"
    add(kb, by_case_replace("e2", "r2", "beta", hours=1))
    add(kb, by_case_replace("e3", "r3", "beta", hours=2))            # same project twice: still one project
    c = cat.get(CID)
    assert (c.evidence.action_successes, c.evidence.action_projects, c.status) == (2, ["fp-beta"], "candidate")
    add(kb, by_case_replace("e4", "r4", "gamma", hours=3))
    c = cat.get(CID)
    assert c.status == "active" and c.action.kind == "replace" and c.evidence.action_projects == ["fp-beta", "fp-gamma"]
    assert c.evidence.resolved_by == {"llm": 1, "handler": 3}
    # kb.auto_promote_after_projects decides (0 = never)
    for n, want in ((3, "candidate"), (0, "candidate"), (2, "active")):
        other = CaseCatalog(kb.root, with_kb(kb_cfg, auto_promote_after_projects=n))
        other.rebuild()
        assert other.get(CID).status == want, n
    # one failure of the case's own replace → back to candidate (the action stays)
    add(kb, by_case_replace("e5", "r5", "delta", ok=False, hours=4))
    c = cat.get(CID)
    assert (c.evidence.action_failures, c.status, c.action.kind) == (1, "candidate", "replace")


def test_fallback_after_two_action_failures_without_success(kb):
    add(kb, rec("e1"))
    add(kb, by_case_replace("e2", "r2", "beta", ok=False, hours=1))
    assert kb.catalog().get(CID).action.kind == "replace"            # one failure: still a candidate
    add(kb, by_case_replace("e3", "r3", "gamma", ok=False, hours=2))
    c = kb.catalog().get(CID)
    assert (c.evidence.action_failures, c.evidence.action_successes) == (2, 0)
    assert (c.action.kind, c.status) == ("hint", "observed")
    assert "past failure ×2: handler no_change" in c.hints[1]
    # round 10: a failure RATE — one later success does not bring it back (2 failures > 1 success) …
    add(kb, by_case_replace("e4", "r4", "delta", hours=3))
    c = kb.catalog().get(CID)
    assert (c.action.kind, c.status) == ("hint", "observed")
    # … a second one does (2 successes, 2 failures)
    add(kb, by_case_replace("e5", "r5", "epsilon", hours=4))
    c = kb.catalog().get(CID)
    assert (c.action.kind, c.status) == ("replace", "candidate")
    assert (c.evidence.action_successes, c.evidence.action_failures) == (2, 2)


def test_replace_scope_from_config(kb_cfg):
    kb = FileKnowledgeBase(with_kb(kb_cfg, case_replace_scope="file"))
    add(kb, rec("e1"))
    assert kb.catalog().get(CID).action.replace_scope == "file"


def test_most_common_pair_set_and_masked_pairs(kb):
    add(kb, rec("e1", pairs=(("clientIp", "ip"),)),
        rec("e2", run="r2", project="beta", pairs=(("clientIp", "remoteClientIp"),)),
        rec("e3", run="r3", project="gamma", pairs=(("clientIp", "remoteClientIp"),)),
        rec("e4", run="r4", project="delta", pairs=(("***", "***"),)))
    c = kb.catalog().get(CID)
    assert [(p.old, p.new, p.count) for p in c.action.replace_pairs] == [("clientIp", "remoteClientIp", 2)]


# --------------------------------------------------------------------------- human block
def test_human_block_survives_update_and_rebuild(kb):
    add(kb, rec("e1"))
    cat = kb.catalog()
    path = cat.path_of(CID)
    text = path.read_text(encoding="utf-8")
    assert text.index("human:") < text.index("# ---- generated") < text.index("\nname:")
    edited = text.replace(
        "  status: null\n",
        "  # decided in the 10-23 review: the wrapper needs a real fix\n  status: rejected\n").replace(
        "  hints: []\n", "  hints:\n  - check RequestWrapper first   # team note\n")
    path.write_text(edited, encoding="utf-8")
    c = cat.get(CID)
    assert c.human.status == "rejected" and c.effective_status() == "rejected" and c.status == "candidate"
    assert c.human.hints == ["check RequestWrapper first"]
    add(kb, rec("e2", run="r2", project="beta", hours=1))           # incremental update
    after = path.read_text(encoding="utf-8")
    assert "# decided in the 10-23 review" in after and "# team note" in after and "  status: rejected" in after
    assert cat.get(CID).evidence.seen == 2
    cat.rebuild()                                                    # rebuild keeps it too
    assert "# decided in the 10-23 review" in path.read_text(encoding="utf-8")
    assert cat.get(CID).human.status == "rejected"
    # a CLI / API edit re-renders the block (its comments are not kept)
    c = cat.set_human(CID, notes="owner: platform team")
    assert c.human.notes == "owner: platform team" and c.human.status == "rejected"
    assert "# decided in the 10-23 review" not in path.read_text(encoding="utf-8")


def test_invalid_human_block_is_kept_verbatim_and_ignored(kb, caplog):
    add(kb, rec("e1"))
    cat = kb.catalog()
    path = cat.path_of(CID)
    bad = path.read_text(encoding="utf-8").replace("  status: null\n", "  status: maybe-later\n")
    path.write_text(bad, encoding="utf-8")
    c = cat.get(CID)
    assert c is not None and c.human.status is None and c.effective_status() == "candidate"
    add(kb, rec("e2", run="r2", project="beta", hours=1))
    assert "  status: maybe-later\n" in path.read_text(encoding="utf-8")   # not lost
    assert cat.get(CID).evidence.seen == 2
    assert "human:` block is invalid" in caplog.text


def test_set_human_validation(kb):
    add(kb, rec("e1"))
    cat = kb.catalog()
    with pytest.raises(ValueError):
        cat.set_human(CID, colour="red")
    with pytest.raises(ValueError):
        cat.set_human(CID, status="maybe")
    with pytest.raises(KeyError):
        cat.set_human("0000000000", status="rejected")
    c = cat.set_human(CID[:6], action={"kind": "handler", "handler_id": "jakarta-namespace-sweep"}, status="active")
    assert c.effective_action().kind == "handler" and c.effective_status() == "active"
    c = cat.set_human(CID, action=None, status=None)
    assert c.human.action is None and c.effective_action().kind == "replace"


# --------------------------------------------------------------------------- match
def _err(l1: str = L1_REQ, l2: str = EC_L2, symbol: str = "method clientIp()", location: str = "class com.example.Request",
         kind: ErrorKind = ErrorKind.COMPILE) -> BuildError:
    return BuildError(kind=kind, message="cannot find symbol", file=EC_FILE, line=7, symbol=symbol, location=location,
                      signature=l1, signature_generic=l2)


def test_match_by_l1_l2_and_symbol(kb):
    add(kb, rec("e1"), rec("e2", run="r2", project="beta", l1=L1_WRAP, location="class com.lib.RequestWrapper"))
    cat = CaseCatalog(kb.root, kb.cfg)                               # a fresh instance, like the triage handler
    hits = cat.match([_err()])
    assert len(hits) == 1
    h = hits[0]
    assert (h.case_id, h.name, h.status, h.action.kind, h.variant_owner) == (
        CID, "Request.clientIp — cannot find symbol", "candidate", "replace", "com.example.Request")
    assert [(p.old, p.new) for p in h.action.replace_pairs] == [("clientIp", "remoteClientIp")]
    assert h.errors[0].line == 7 and len(h.hints) == 1 and h.hints[0].startswith(f"[case {CID}] past runs: fixed by")
    assert cat.match([_err(l1=L1_WRAP, location="class com.lib.RequestWrapper")])[0].variant_owner == \
        "com.lib.RequestWrapper"
    # L2 only (an L1 never seen), owner unknown to the variants
    h = cat.match([_err(l1="compile|cannot find symbol|method clientIp()|class com.other.X",
                        location="class com.other.X")])[0]
    assert (h.case_id, h.variant_owner) == (CID, "")
    # error symbol only (another L2 with the same symbol)
    h = cat.match([_err(l1="x", l2="compile|cannot_find_symbol_other|zzz")])
    assert [x.case_id for x in h] == [CID]
    # several errors of one case → one hit; unknown errors → no hit
    errs = [_err(), _err(l1=L1_WRAP), _err(l1="n", l2="compile|cannot_find_symbol|nothing", symbol="method nothing()")]
    hits = cat.match(errs)
    assert len(hits) == 1 and len(hits[0].errors) == 2
    assert cat.match([]) == [] and CaseCatalog(kb.root / "nope").match([_err()]) == []


def test_rejected_cases_never_match(kb):
    add(kb, rec("e1"))
    cat = kb.catalog()
    cat.set_human(CID, status="rejected")
    assert cat.match([_err()]) == []
    assert CaseCatalog(kb.root).match([_err(l1="x"), _err(l1="y", l2="compile|other|clientIp")]) == []
    cat.set_human(CID, status=None)
    assert [h.case_id for h in cat.match([_err()])] == [CID]


def test_match_uses_effective_human_values(kb):
    add(kb, rec("e1"))
    cat = kb.catalog()
    cat.set_human(CID, name="Request.clientIp renamed in lib 2.0", hints=["use remoteClientIp()"],
                  action=CaseAction(kind="replace", replace_pairs=[ReplacePair(old="clientIp", new="ip")]),
                  status="active")
    h = cat.match([_err()])[0]
    assert (h.name, h.status) == ("Request.clientIp renamed in lib 2.0", "active")
    assert [(p.old, p.new) for p in h.action.replace_pairs] == [("clientIp", "ip")]
    assert h.hints[0] == f"[case {CID}] use remoteClientIp()" and len(h.hints) == 2


def test_get_and_resolve(kb):
    add(kb, rec("e1"), rec("e2", l2="compile|cannot_be_applied|Request.clientIp", run="r2"))
    cat = kb.catalog()
    other = case_id_for("compile|cannot_be_applied|Request.clientIp")
    assert cat.get(CID).id == CID and cat.get(CID[:5]).id == CID and cat.get(EC_L2).id == CID
    assert cat.get(L1_REQ) is not None
    assert cat.get("clientIp").id == CID and cat.get("Request.clientIp").id == other
    assert cat.get("nothing") is None and cat.resolve("") == []
    assert [c.id for c in cat.cases()] == sorted([CID, other])        # same projects / seen: by id


# --------------------------------------------------------------------------- incremental == rebuild
def test_incremental_update_equals_rebuild(kb):
    batches = [
        [unresolved("a-1", "r1", "alpha"), rec("a-2", run="r1", project="alpha", l2="compile|incompatible_types|Q->Q",
                                               l1="compile|incompatible_types|q", change_kind="code_edit", pairs=())],
        [failed_then_fixed("b-1", "r2", "beta", hours=1),
         rec("b-2", run="r2", project="beta", l1=L1_WRAP, location="class com.lib.RequestWrapper", hours=1)],
        [by_case_replace("c-1", "r3", "gamma", hours=2), unresolved("c-2", "r3", "gamma", hours=2, outcome="regressed")],
        [by_case_replace("d-1", "r4", "delta", hours=3)],
    ]
    for b in batches:
        add(kb, *b)
    cat = kb.catalog()
    incremental = snapshot(cat)
    assert len(incremental["files"]) == 2 and cat.get(CID).status == "active"
    assert cat.rebuild() == 2
    assert snapshot(cat) == incremental
    # from scratch (no catalog at all) too
    import shutil
    shutil.rmtree(cat.catalog_dir)
    assert cat.rebuild() == 2
    assert snapshot(cat) == incremental
    # update_from with records already stored changes nothing
    assert cat.update_from(batches[1], []) == []


def test_first_update_over_existing_events_rebuilds_everything(kb):
    other_l2 = "compile|package_does_not_exist|javax.persistence"
    kb.error_case_store().append([rec("old-1", l2=other_l2, l1="compile|package_does_not_exist|javax.persistence",
                                      status="unresolved")], [])
    cat = kb.catalog()
    assert not cat.index_path.exists()
    changed = add(kb, rec("new-1", run="r2", project="beta"))
    assert changed == [CID]
    assert {c.signature_generic for c in cat.cases()} == {EC_L2, other_l2}


def test_update_from_never_raises(kb, monkeypatch):
    add(kb, rec("e1"))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(CaseCatalog, "compute", boom)
    assert kb.catalog().update_from([rec("e2", run="r2")], []) == []
    assert kb.catalog().update_from(None, None) == []
    assert kb.catalog().update_from([rec("e3", l2="")], []) == []   # no L2: no case


def test_unchanged_cases_are_not_rewritten(kb):
    add(kb, rec("e1"))
    cat = kb.catalog()
    p = cat.path_of(CID)
    before = (p.stat().st_mtime_ns, p.read_text())
    idx_before = cat.index_path.stat().st_mtime_ns
    assert cat.refresh([EC_L2]) == []
    assert (p.stat().st_mtime_ns, p.read_text()) == before and cat.index_path.stat().st_mtime_ns == idx_before


# --------------------------------------------------------------------------- views
def test_views_content(kb):
    add(kb, rec("e1"), rec("e2", run="r2", project="beta", hours=1), unresolved("e3", "r3", "gamma", hours=2))
    add(kb, rec("x1", l2="compile|cannot_find_symbol|Other", l1="compile|cannot find symbol|class Other",
                location=None, status="unresolved", symbol="class Other", error_symbol="Other"))
    cat = kb.catalog()
    cat.set_human(CID, hints=["x"])
    md_path, csv_path = cat.write_views()
    assert (md_path, csv_path) == (kb.root / "views" / "cases.md", kb.root / "views" / "cases.csv")
    md = md_path.read_text(encoding="utf-8")
    assert md.startswith("# Error case catalog / 오류 케이스 카탈로그")
    assert "**How to read.**" in md and "**How to edit.**" in md and "**읽는 법.**" in md and "**고치는 법.**" in md
    assert "sbmig kb cases reject|pin|unpin|hint|note <id>" in md and "2 case(s)." in md
    assert "| id | name | status | action | seen | projects | resolved by | latest fix | top failure |" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("| `")]
    assert len(rows) == 2 and rows[0].startswith(f"| `{CID}` | Request.clientIp — cannot find symbol | candidate |")
    assert "replace `clientIp` → `remoteClientIp`" in rows[0] and "| 3 | 3 | llm 2 |" in rows[0]
    assert "(beta, 2026-10-20)" in rows[0] and "llm no_change ×1" in rows[0]
    assert rows[1].startswith("| `") and "Other — cannot find symbol | observed | hint |" in rows[1]
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    table = list(csv.reader(csv_path.open(encoding="utf-8-sig", newline="")))
    assert table[0] == CASE_CSV_COLUMNS and len(table) == 3
    row = dict(zip(table[0], table[1]))
    assert row["id"] == CID and row["status"] == "candidate" and row["seen"] == "3" and row["human"] == "yes"
    assert row["file"] == f"catalog/compile/cannot_find_symbol/clientIp--{CID}.yml"
    # a person's status shows with a star
    cat.set_human(CID, status="rejected")
    assert "| rejected* |" in md_path.read_text(encoding="utf-8")


def test_empty_views(tmp_path):
    cat = CaseCatalog(tmp_path / "kb")
    md, _ = cat.write_views()
    assert "0 case(s)." in md.read_text(encoding="utf-8") and cat.cases() == [] and cat.status_counts() == {
        "observed": 0, "candidate": 0, "active": 0, "rejected": 0}


# --------------------------------------------------------------------------- caching
def test_index_and_case_reload_when_files_change(kb):
    add(kb, rec("e1"))
    reader = CaseCatalog(kb.root)                                    # e.g. the handler's own instance
    assert [h.case_id for h in reader.match([_err()])] == [CID]
    # another process rewrites the index (the case is gone from it): the reader reloads by the file stamp
    idx_path = reader.index_path
    idx = json.loads(idx_path.read_text())
    idx["by_l2"], idx["by_l1"], idx["by_symbol"] = {}, {}, {}
    idx_path.write_text(json.dumps(idx, indent=2) + "\n")
    assert reader.match([_err()]) == []
    kb.catalog().rebuild()
    assert [h.case_id for h in reader.match([_err()])] == [CID]
    # a person edits the case file by hand: the next match sees it (the file has its own stamp)
    p = reader.path_of(CID)
    p.write_text(p.read_text(encoding="utf-8").replace("  status: null\n", "  status: rejected\n"), encoding="utf-8")
    assert reader.match([_err()]) == []


def test_parsed_cases_are_cached(kb, monkeypatch):
    add(kb, rec("e1"))
    cat = CaseCatalog(kb.root)
    assert cat.match([_err()])
    import sbmig.kb.catalog as mod

    calls = []
    real = mod._parse_case_text
    monkeypatch.setattr(mod, "_parse_case_text", lambda *a: calls.append(1) or real(*a))
    for _ in range(5):
        assert CaseCatalog(kb.root).match([_err()])
    assert calls == []                                               # nothing re-parsed while the files stay


def test_catalog_without_cfg_or_store(tmp_path):
    """The handler builds CaseCatalog(cfg.kb_dir, cfg); a bare directory works too (no recipes, no index)."""
    root = tmp_path / "kb"
    root.mkdir()
    from sbmig.kb.error_cases import ErrorCaseStore

    r = rec("e1")
    ErrorCaseStore(root).append([r], [])
    cat = CaseCatalog(root)
    assert cat.update_from([r]) == [CID]
    # no config = the KBConfig defaults: since v2 the learned replace (kb.case_replace) is off → hint
    assert cat.get(CID).action.kind == "hint"
    assert (root / "catalog" / "index.json").exists()
    v1 = CaseCatalog(root, Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}},
                                  kb={"dir": str(root), "case_replace": True}))
    v1.refresh([r.signature_generic])
    assert v1.get(CID).action.kind == "replace"


# --------------------------------------------------------------------------- round 10 (2026-10-26)
from sbmig.kb.catalog import (  # noqa: E402
    case_replace_note,
    case_replace_resolver,
    pairset_hash,
    parse_case_replace_resolver,
    resolver_from_notes,
)


def test_an_attempt_that_resolved_the_error_is_not_its_failure(kb):
    """review-kb/repro4: attempt a1 fixed this error (its cluster stayed `no_change` because of another error)."""
    r = rec("e1", attempts=[ref("r1:a1", "llm", "no_change"), ref("r1:a2", "llm", "partial"),
                            ref("r1:a3", "llm", "inconclusive")],
            resolution_attempts=["r1:a1"])
    c = kb.catalog().compute(EC_L2, [r])
    assert [(f.method, f.count) for f in c.evidence.fixes] == [("llm", 1)]
    assert c.evidence.failures == [] and not any("do not repeat" in h for h in c.hints)
    other = rec("e2", status="unresolved", attempts=[ref("r1:a1", "llm", "no_change")])
    c = kb.catalog().compute(EC_L2, [r, other])
    assert [(x.outcome, x.count) for x in c.evidence.failures] == [("no_change", 1)]      # a failure for e2 only


def test_learned_replace_is_chosen_by_projects_not_records(kb):
    """review-kb/repro2: one project with 10 errors fixed one way must not outrank 3 projects fixed another way."""
    big = [rec(f"b{i}", run="rb", project="big", pairs=(("clientIp", "getRemoteAddr"),)) for i in range(10)]
    small = [rec(f"s{i}", run=f"r{i}", project=p) for i, p in enumerate(["b", "c", "d"])]
    c = kb.catalog().compute(EC_L2, big + small)
    assert [(p.old, p.new, p.count) for p in c.action.replace_pairs] == [("clientIp", "remoteClientIp", 3)]
    assert "in 3 project(s)" in c.hints[0]


def test_a_failing_replace_falls_back_by_rate_and_the_next_set_is_tried(kb):
    """review-kb/repro2: 1 success + 6 failures used to keep applying (fallback only at 0 successes)."""
    learned = [rec(f"s{i}", run=f"r{i}", project=p) for i, p in enumerate(["b", "c", "d"])]
    alt = [rec("alt", run="ralt", project="alt", pairs=(("clientIp", "getRemoteAddr"),))]
    ok = by_case_replace("ok", "rok", "e1")
    fails = [by_case_replace(f"f{i}", f"rf{i}", f"f{i}", ok=False) for i in range(6)]
    c = kb.catalog().compute(EC_L2, learned + alt + [ok] + fails)
    # the legacy plain `case-replace` outcomes count for the top-ranked set → it falls back; the next set is tried
    assert [(p.old, p.new) for p in c.action.replace_pairs] == [("clientIp", "getRemoteAddr")]
    assert (c.status, c.evidence.action_successes, c.evidence.action_failures) == ("candidate", 0, 0)
    c = kb.catalog().compute(EC_L2, learned + [ok] + fails)
    assert c.action.kind == "hint" and (c.evidence.action_successes, c.evidence.action_failures) == (1, 6)


def test_pairset_hash_and_resolver_ids():
    a = [ReplacePair(old="clientIp", new="remoteClientIp"), ReplacePair(old="javax.a", new="jakarta.a")]
    assert pairset_hash(a) == pairset_hash(list(reversed(a)) + a[:1]) == pairset_hash([("javax.a", "jakarta.a"),
                                                                                       ("clientIp", "remoteClientIp")])
    assert len(pairset_hash(a)) == 8 and pairset_hash(a) != pairset_hash(a[:1])
    rid = case_replace_resolver("abc1234567", a)
    assert rid == f"case-replace:abc1234567:{pairset_hash(a)}"
    assert parse_case_replace_resolver(f"{rid},case-replace:ffff:00000000,other") == [
        ("abc1234567", pairset_hash(a)), ("ffff", "00000000")]
    assert parse_case_replace_resolver("case-replace") == [("", "")] and parse_case_replace_resolver("h1") == []
    assert resolver_from_notes(["x", case_replace_note("abc1234567", a), case_replace_note("abc1234567", a)]) == rid


def test_outcomes_stay_with_the_pair_set_that_was_applied(kb):
    """A silently changed action must not inherit the old pairs' successes (handler resolver ids carry the hash)."""
    old_pairs = (("clientIp", "getRemoteAddr"),)
    new_pairs = (("clientIp", "remoteClientIp"),)
    h_old = case_replace_resolver(CID, [ReplacePair(old=o, new=n) for o, n in old_pairs])
    h_new = case_replace_resolver(CID, [ReplacePair(old=o, new=n) for o, n in new_pairs])
    learned = [rec(f"s{i}", run=f"r{i}", project=p) for i, p in enumerate(["b", "c"])]       # new_pairs: 2 projects
    old_ok = [rec(f"o{i}", run=f"ro{i}", project=f"o{i}", method="handler", resolver=h_old, change_kind="text_replace",
                  attempts=[ref(f"ro{i}:a1", "handler", "improved", resolver=h_old)]) for i in range(3)]
    c = kb.catalog().compute(EC_L2, learned + old_ok)
    assert [(p.old, p.new) for p in c.action.replace_pairs] == list(new_pairs)
    assert (c.evidence.action_successes, c.status) == (0, "candidate")          # the old set's successes don't count
    new_ok = [rec(f"n{i}", run=f"rn{i}", project=f"n{i}", method="handler", resolver=h_new, change_kind="text_replace",
                  attempts=[ref(f"rn{i}:a1", "handler", "improved", resolver=h_new)]) for i in range(2)]
    c = kb.catalog().compute(EC_L2, learned + old_ok + new_ok)
    assert (c.evidence.action_successes, c.evidence.action_projects, c.status) == (2, ["fp-n0", "fp-n1"], "active")
    other_case = case_replace_resolver("0000000000", [ReplacePair(old=o, new=n) for o, n in new_pairs])
    bad = rec("x", run="rx", project="x", status="unresolved",
              attempts=[ref("rx:a1", "handler", "no_change", resolver=other_case)])
    assert kb.catalog().compute(EC_L2, learned + new_ok + [bad]).evidence.action_failures == 0   # another case


def test_pinned_hits(kb):
    add(kb, rec("e1"))
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=EC_FILE, line=5,
                     signature=L1_REQ, signature_generic=EC_L2)
    cat = kb.catalog()
    assert cat.match([err])[0].pinned is False
    cat.set_human(CID, status="active")
    assert cat.match([err])[0].pinned is False                                  # a status alone pins no action
    cat.set_human(CID, action=CaseAction(kind="handler", handler_id="jakarta-namespace-sweep"))
    hit = cat.match([err])[0]
    assert hit.pinned is True and hit.action.kind == "handler" and hit.action.handler_id == "jakarta-namespace-sweep"


def test_audit_log_of_human_edits_and_automatic_transitions(kb, monkeypatch):
    import sbmig.kb.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "_who", lambda: "jdoe")
    kb.cfg.kb.auto_promote_after_projects = 2
    add(kb, unresolved("e0", "r0", "zeta"))                                    # observed: nothing to audit
    cat = kb.catalog()
    assert list(cat.iter_audit()) == []
    add(kb, rec("e1", hours=1))                                                # observed → candidate
    add(kb, by_case_replace("e2", "r2", "beta", hours=2), by_case_replace("e3", "r3", "gamma", hours=3))
    auto = [a for a in cat.iter_audit(CID) if a["who"] == "sbmig"]
    assert [(a["before"]["status"], a["after"]["status"]) for a in auto] == [("observed", "candidate"),
                                                                             ("candidate", "active")]
    assert auto[1]["reason"] == "promoted" and auto[1]["evidence"]["action_projects"] == 2
    assert all(a["schema_version"] == KB_RECORD_SCHEMA and a["agent_version"] and a["at"] for a in auto)
    cat.set_human(CID, audit_note="sbmig kb cases reject", status="rejected", notes="rejected: wrong owner")
    human = [a for a in cat.iter_audit(CID) if a["event"] == "human"]
    assert len(human) == 1 and human[0]["who"] == "jdoe" and human[0]["command"] == "sbmig kb cases reject"
    assert human[0]["before"] == {} and human[0]["after"] == {"status": "rejected", "notes": "rejected: wrong owner"}
    cat.set_human(CID, status="rejected", notes="rejected: wrong owner")           # no change → no line
    assert len([a for a in cat.iter_audit(CID) if a["event"] == "human"]) == 1


def test_fall_back_is_audited(kb):
    add(kb, rec("e1"))
    add(kb, by_case_replace("e2", "r2", "beta", ok=False, hours=1), by_case_replace("e3", "r3", "gamma", ok=False,
                                                                                   hours=2))
    last = [a for a in kb.catalog().iter_audit(CID) if a["who"] == "sbmig"][-1]
    assert last["before"]["status"] == "candidate" and last["after"]["status"] == "observed"
    assert last["reason"].startswith("fallen back: replace `clientIp` → `remoteClientIp`: 2 failure(s) > 0")


def test_unreadable_event_lines_are_quarantined_and_their_case_is_kept(kb, caplog):
    import logging

    from sbmig.kb.catalog import clear_caches

    clear_caches()                                             # the warning is once per line and process
    add(kb, rec("e1"))
    cat = kb.catalog()
    path = cat.path_of(CID)
    good = kb.error_case_store().errors_path.read_text(encoding="utf-8")
    broken = good.replace('"status":"resolved"', '"status":"some_future_status"')
    kb.error_case_store().errors_path.write_text(broken, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert list(cat.iter_records()) == []
    assert cat.bad_lines and cat.bad_lines[0]["id"] == "e1" and cat.bad_lines[0]["signature_generic"] == EC_L2
    assert any("could not be read" in r.getMessage() for r in caplog.records)
    cat.rebuild()
    assert path.exists() and cat.get(CID) is not None                          # never deleted over a bad line
    q = [json.loads(x) for x in (kb.root / "quarantine.jsonl").read_text().splitlines()]
    assert len(q) == 1 and q[0]["id"] == "e1" and q[0]["file"] == "errors.jsonl"
    cat.rebuild()
    assert len((kb.root / "quarantine.jsonl").read_text().splitlines()) == 1    # noted once
    # a torn line of unknown L2 protects every case file
    kb.error_case_store().errors_path.write_text('{"id":"zz","sche\n', encoding="utf-8")
    cat.rebuild()
    assert path.exists()
    # readable again: normal rebuild
    kb.error_case_store().errors_path.write_text(good, encoding="utf-8")
    cat.rebuild()
    assert cat.get(CID).evidence.seen == 1
