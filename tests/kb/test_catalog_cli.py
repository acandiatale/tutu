"""`sbmig kb cases …` over a temp KB (list / show / reject / pin / unpin / hint / note) and the case parts of
`kb errors show`, `kb reindex`, `kb stats`. Pins (v2): `script:<N>`, `hint`, `block[:<reason>]` only."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.core.models import FixAttemptRecord
from sbmig.kb import FileKnowledgeBase, kb_cli
from sbmig.kb.catalog import case_id_for
from sbmig.kb.cli import parse_action

from .helpers import EC_L2, RENAME_DIFF
from .test_catalog import L1_REQ, failed_then_fixed, rec, ref, unresolved
from .test_scripts import FIX_A

runner = CliRunner()
CID = case_id_for(EC_L2)
OTHER_L2 = "compile|package_does_not_exist|javax.persistence"
OTHER = case_id_for(OTHER_L2)


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase) -> None:
    """alpha: unresolved after a no_change; beta: fixed after an apply_failed (with the fix's diff); a proven fix
    script v1 for that case (→ candidate, action `script v1`); a second case (observed, hint)."""
    kb.scripts.add(CID, EC_L2, FIX_A, analysis="clientIp became remoteClientIp", project="order-api")
    kb.scripts.credit(CID, 1, "improved", project="order-api")
    kb.append_error_cases([unresolved("a-1", "r1", "member-api")], [])
    kb.append_error_cases(
        [failed_then_fixed("b-1", "r2", "order-api", hours=24)],
        [FixAttemptRecord(id="r2:a0", run_id="r2", project="order-api", fix_method="llm", outcome="apply_failed",
                          failure="search text not found", diff="(proposal)"),
         FixAttemptRecord(id="r2:a1", run_id="r2", project="order-api", fix_method="llm", resolver_id="fake-model",
                          outcome="improved", applied=True, diff=RENAME_DIFF)])
    kb.append_error_cases([rec("o-1", run="r3", project="pay-api", l2=OTHER_L2, status="unresolved",
                               l1="compile|package_does_not_exist|javax.persistence", hours=48, location=None,
                               symbol=None, error_symbol="package javax.persistence")], [])


def test_cases_list_and_filters(kb):
    assert "no cases" in _run(kb.root, "cases", "list").output
    _seed(kb)
    r = _run(kb.root, "cases", "list")
    assert r.exit_code == 0
    lines = r.output.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith(f"{CID}  candidate") and "projects=2" in lines[0]
    assert "Request.clientIp — cannot find symbol" in lines[0] and lines[0].rstrip().endswith("script v1")
    assert lines[1].startswith(f"{OTHER}  observed") and lines[1].rstrip().endswith("hint")
    assert _run(kb.root, "cases", "list", "--status", "obs").output.startswith(OTHER)
    assert _run(kb.root, "cases", "list", "--action", "script").output.startswith(CID)
    assert _run(kb.root, "cases", "list", "--kind", "package_does").output.startswith(OTHER)
    assert _run(kb.root, "cases", "list", "--search", "com.example.Request").output.count("\n") == 1
    assert _run(kb.root, "cases", "list", "--search", "nothing-like-this").output.strip() == "no cases"
    assert len(_run(kb.root, "cases", "list", "-n", "1").output.strip().splitlines()) == 1
    rows = json.loads(_run(kb.root, "cases", "list", "--json").output)
    assert [x["id"] for x in rows] == [CID, OTHER] and (rows[0]["action"]["kind"], rows[0]["action"]["script_version"]) == ("script", 1)
    assert _run(kb.root, "cases", "list", "--status", "(").exit_code != 0


def test_cases_show_is_readable(kb):
    _seed(kb)
    r = _run(kb.root, "cases", "show", "clientIp")
    assert r.exit_code == 0
    out = r.output
    assert out.startswith(f"case {CID} — Request.clientIp — cannot find symbol")
    assert f"key (L2):  {EC_L2}" in out and "status:    candidate" in out
    assert "action:    script v1\n" in out and "fix script: active after 2 project(s) without a failure" in out
    assert "fix scripts (1 version(s);" in out and "← reused first" in out
    assert "seen 2× in 2 project(s) / 2 run(s), 2026-10-20 … 2026-10-21" in out
    assert "by status: unresolved 1, resolved 1; resolved by: llm 1" in out
    assert f"com.example.Request  {L1_REQ}   seen 2, 2 project(s)" in out
    # the timeline: the failure in member-api, then the failed attempt and the fix in order-api
    tl = out[out.index("timeline"):out.index("fixes:")]
    assert tl.index("member-api") < tl.index("order-api")
    assert "2026-10-20 member-api" in tl and "llm:no_change (unresolved)" in tl
    assert "2026-10-21 order-api" in tl and "llm:apply_failed → llm:improved (resolved)" in tl and "error b-1" in tl
    assert "llm fake-model — 1× in 1 project(s) [text_replace]: renamed clientIp to remoteClientIp" in out
    assert "llm apply_failed — 1× in 1 project(s): search text not found in IpFilter.java  (r2:a0, order-api)" in out
    assert "past failure ×1" in out and "do not repeat it" in out
    assert "--- latest successful fix: r2:a1 (order-api, llm fake-model" in out
    assert "+        return req.remoteClientIp();" in out
    assert "latest successful fix" not in _run(kb.root, "cases", "show", CID, "--no-diff").output
    data = json.loads(_run(kb.root, "cases", "show", CID[:6], "--json").output)
    assert data["id"] == CID and data["evidence"]["seen"] == 2
    assert _run(kb.root, "cases", "show", EC_L2).output.startswith(f"case {CID}")
    assert _run(kb.root, "cases", "show", "nope").exit_code == 1


def test_cases_show_prints_every_attempt_of_the_latest_multi_attempt_fix(kb):
    # 2026-10-25: the latest fix took two attempts (a0 no_change stayed in the tree) → both diffs, in run order
    kb.append_error_cases(
        [rec("c-1", run="r5", project="card-api",
             attempts=[ref("r5:a0", "llm", "no_change", "still there"), ref("r5:a1", "llm", "improved")],
             resolution_attempts=["r5:a0", "r5:a1"])],
        [FixAttemptRecord(id="r5:a0", run_id="r5", project="card-api", fix_method="llm", resolver_id="fake-model",
                          outcome="no_change", applied=True, diff="+import com.example.legacy.RequestSupport;"),
         FixAttemptRecord(id="r5:a1", run_id="r5", project="card-api", fix_method="llm", resolver_id="fake-model",
                          outcome="improved", applied=True, diff=RENAME_DIFF)])
    case = kb.catalog().get(CID)
    assert case.evidence.fixes[0].attempt_ids == ["r5:a0", "r5:a1"]
    out = _run(kb.root, "cases", "show", CID).output
    assert "--- latest successful fix: 2 attempts whose changes stayed in the tree (run order, r5:a1 resolved it)" in out
    assert out.index("--- r5:a0 (card-api, llm fake-model, no_change") < out.index("--- r5:a1 (card-api")
    assert "+import com.example.legacy.RequestSupport;" in out and "+        return req.remoteClientIp();" in out


def test_cases_show_ambiguous_prefix(kb):
    _seed(kb)
    twin = next(f"compile|cannot_find_symbol|Twin{i}" for i in range(100000)
                if case_id_for(f"compile|cannot_find_symbol|Twin{i}")[:4] == CID[:4])
    kb.append_error_cases([rec("t-1", run="r4", project="twin-api", l2=twin, l1=twin, status="unresolved")], [])
    r = _run(kb.root, "cases", "show", CID[:4])
    assert r.exit_code == 1 and "ambiguous key" in r.output and CID in r.output and case_id_for(twin) in r.output
    assert _run(kb.root, "cases", "show", CID[:8]).exit_code == 0
    assert _run(kb.root, "cases", "reject", CID[:4]).exit_code == 1


def test_reject_pin_unpin_hint_note(kb, ws, profile):
    _seed(kb)
    r = _run(kb.root, "cases", "reject", CID, "--reason", "breaks the wrapper")
    assert r.exit_code == 0 and r.output.startswith(f"rejected {CID}") and "status rejected*" in r.output
    c = kb.catalog().get(CID)
    assert c.effective_status() == "rejected" and c.human.notes == "rejected: breaks the wrapper"
    assert kb.catalog().match([_err()]) == []
    assert "| rejected* |" in (kb.root / "views" / "cases.md").read_text(encoding="utf-8")

    r = _run(kb.root, "cases", "pin", CID, "--action", "script:1")
    assert r.exit_code == 0 and "status active*" in r.output and "action script v1*" in r.output
    c = kb.catalog().get(CID)
    assert c.effective_status() == "active" and c.effective_action() == c.action
    assert kb.catalog().match([_err()])[0].status == "active"
    assert _run(kb.root, "cases", "pin", CID, "--action", "script:9").exit_code == 1     # no stored v9

    for spec in ("replace:clientIp=remoteClientIp", "handler:jakarta-namespace-sweep", "recipe:Missing", "patch:x"):
        r = _run(kb.root, "cases", "pin", CID, "--action", spec)
        assert r.exit_code != 0 and "no longer supported" in r.output, spec
    assert _run(kb.root, "cases", "pin", CID, "--action", "block:external_api_incompatible").exit_code == 0
    a = kb.catalog().get(CID).effective_action()
    assert (a.kind, a.block_reason) == ("block", "external_api_incompatible")
    assert _run(kb.root, "cases", "pin", CID, "--action", "hint").exit_code == 0
    assert _run(kb.root, "cases", "pin", CID, "--action", "bogus").exit_code != 0
    assert _run(kb.root, "cases", "pin", CID, "--action", "script:x").exit_code != 0

    r = _run(kb.root, "cases", "unpin", CID)
    assert r.exit_code == 0 and "status candidate," in r.output
    c = kb.catalog().get(CID)
    assert c.human.action is None and c.human.status is None and c.effective_action().kind == "script"

    assert _run(kb.root, "cases", "hint", CID, "check   RequestWrapper first").exit_code == 0
    assert _run(kb.root, "cases", "hint", CID, "check RequestWrapper first").exit_code == 0     # no duplicate
    assert _run(kb.root, "cases", "note", CID, "owner: platform team").exit_code == 0
    c = kb.catalog().get(CID)
    assert c.human.hints == ["check RequestWrapper first"]
    assert c.human.notes == "rejected: breaks the wrapper\nowner: platform team"
    m = kb.match(_cluster(), ws, profile)
    assert m.hints[0] == f"[case {CID}] check RequestWrapper first"
    out = _run(kb.root, "cases", "show", CID).output
    assert "[person] check RequestWrapper first" in out and "owner: platform team" in out
    assert _run(kb.root, "cases", "note", "nope", "x").exit_code == 1


def test_pin_shows_generated_values_when_overridden(kb):
    _seed(kb)
    _run(kb.root, "cases", "pin", OTHER, "--action", "block:the persistence owner migrates it")
    out = _run(kb.root, "cases", "show", OTHER).output
    assert "status:    active*" in out
    assert "generated: javax.persistence — package does not exist: status observed, action hint" in out


def test_parse_action():
    assert parse_action("script:2").script_version == 2 and parse_action("script:v3").script_version == 3
    assert parse_action("hint").kind == "hint"
    assert parse_action("block").kind == "block" and parse_action("block:needs lib 3").suggestion == "needs lib 3"
    assert parse_action("block:external_api_incompatible").block_reason == "external_api_incompatible"
    for bad in ("script:", "script:0", "frobnicate"):
        with pytest.raises(ValueError):
            parse_action(bad)
    for gone in ("recipe:a.B", "replace:x=y", "handler:h", "patch:p"):
        with pytest.raises(ValueError, match="no longer supported"):
            parse_action(gone)


def test_errors_show_names_the_case(kb):
    _seed(kb)
    out = _run(kb.root, "errors", "show", "b-1").output
    assert f"case:      {CID}  Request.clientIp — cannot find symbol  [candidate, script v1" in out
    assert f"sbmig kb cases show {CID}" in out and "--- resolving diff (r2:a1)" in out
    assert "failure: search text not found" in out and "1. r2:a0  llm  apply_failed" in out


def test_reindex_and_stats_mention_cases(kb):
    _seed(kb)
    r = _run(kb.root, "reindex")
    assert "case catalog: 2 case(s) (observed 1, candidate 1)" in r.output
    assert "fix scripts: 1 case(s), 1 version(s) (1 proven, 0 retired, 0 quarantined)" in r.output
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert st["catalog_cases"] == 2 and st["catalog_observed"] == 1 and st["catalog_candidate"] == 1
    assert st["script_versions"] == 1


def _err():
    from .test_catalog import _err as make

    return make()


def _cluster():
    from .test_catalog_store import _cluster as make

    return make()


def test_case_edits_are_audited_and_shown(kb, monkeypatch):
    import sbmig.kb.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "_who", lambda: "operator7")
    _seed(kb)
    assert _run(kb.root, "cases", "reject", CID, "--reason", "wrong owner").exit_code == 0
    assert _run(kb.root, "cases", "unpin", CID).exit_code == 0
    lines = [json.loads(x) for x in (kb.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    human = [x for x in lines if x["event"] == "human"]
    assert [(x["who"], x["command"], x["after"].get("status")) for x in human] == [
        ("operator7", "sbmig kb cases reject", "rejected"), ("operator7", "sbmig kb cases unpin", None)]
    assert human[0]["before"] == {} and human[1]["before"]["status"] == "rejected"
    out = _run(kb.root, "cases", "show", CID, "--no-diff").output
    assert "history (kb/audit.jsonl" in out and "operator7" in out and "sbmig kb cases reject" in out
