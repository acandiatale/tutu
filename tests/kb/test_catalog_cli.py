"""`sbmig kb cases …` over a temp KB (list / show / reject / pin / unpin / hint / note) and the case parts of
`kb errors show`, `kb learning`, `kb reindex`, `kb match`, `kb promote`."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.core.models import FixAttemptRecord, KBRecipe
from sbmig.kb import FileKnowledgeBase, kb_cli
from sbmig.kb.catalog import case_id_for
from sbmig.kb.cli import parse_action

from .helpers import EC_L2, RENAME_DIFF, recipe_yaml
from .test_catalog import L1_REQ, failed_then_fixed, rec, unresolved

runner = CliRunner()
CID = case_id_for(EC_L2)
OTHER_L2 = "compile|package_does_not_exist|javax.persistence"
OTHER = case_id_for(OTHER_L2)


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase) -> None:
    """alpha: unresolved after a no_change; beta: fixed after an apply_failed (with the fix's diff); a second case."""
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
    assert "Request.clientIp — cannot find symbol" in lines[0] and "replace `clientIp` → `remoteClientIp`" in lines[0]
    assert lines[1].startswith(f"{OTHER}  observed") and lines[1].rstrip().endswith("hint")
    assert _run(kb.root, "cases", "list", "--status", "obs").output.startswith(OTHER)
    assert _run(kb.root, "cases", "list", "--action", "replace").output.startswith(CID)
    assert _run(kb.root, "cases", "list", "--kind", "package_does").output.startswith(OTHER)
    assert _run(kb.root, "cases", "list", "--search", "com.example.Request").output.count("\n") == 1
    assert _run(kb.root, "cases", "list", "--search", "nothing-like-this").output.strip() == "no cases"
    assert len(_run(kb.root, "cases", "list", "-n", "1").output.strip().splitlines()) == 1
    rows = json.loads(_run(kb.root, "cases", "list", "--json").output)
    assert [x["id"] for x in rows] == [CID, OTHER] and rows[0]["action"]["kind"] == "replace"
    assert _run(kb.root, "cases", "list", "--status", "(").exit_code != 0


def test_cases_show_is_readable(kb):
    _seed(kb)
    r = _run(kb.root, "cases", "show", "clientIp")
    assert r.exit_code == 0
    out = r.output
    assert out.startswith(f"case {CID} — Request.clientIp — cannot find symbol")
    assert f"key (L2):  {EC_L2}" in out and "status:    candidate" in out
    assert "action:    replace `clientIp` → `remoteClientIp` (scope error-lines)" in out
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

    r = _run(kb.root, "cases", "pin", CID, "--action", "replace:clientIp=remoteClientIp,Request=HttpRequest")
    assert r.exit_code == 0 and "status active*" in r.output
    c = kb.catalog().get(CID)
    assert c.effective_status() == "active" and c.effective_action().kind == "replace"
    assert [(p.old, p.new) for p in c.effective_action().replace_pairs] == [("clientIp", "remoteClientIp"),
                                                                            ("Request", "HttpRequest")]
    assert kb.catalog().match([_err()])[0].status == "active"

    assert _run(kb.root, "cases", "pin", CID, "--action", "handler:jakarta-namespace-sweep").exit_code == 0
    assert kb.catalog().get(CID).effective_action().handler_id == "jakarta-namespace-sweep"
    r = _run(kb.root, "cases", "pin", CID, "--action", "recipe:com.company.kb.Missing")
    assert r.exit_code == 0 and kb.catalog().get(CID).effective_action().recipe_ids == ["com.company.kb.Missing"]
    assert _run(kb.root, "cases", "pin", CID, "--action", "block:external_api_incompatible").exit_code == 0
    a = kb.catalog().get(CID).effective_action()
    assert (a.kind, a.block_reason) == ("block", "external_api_incompatible")
    assert _run(kb.root, "cases", "pin", CID, "--action", "hint").exit_code == 0
    assert _run(kb.root, "cases", "pin", CID, "--action", "bogus").exit_code != 0
    assert _run(kb.root, "cases", "pin", CID, "--action", "replace:a").exit_code != 0

    r = _run(kb.root, "cases", "unpin", CID)
    assert r.exit_code == 0 and "status candidate," in r.output
    c = kb.catalog().get(CID)
    assert c.human.action is None and c.human.status is None and c.effective_action().kind == "replace"

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
    _run(kb.root, "cases", "pin", OTHER, "--action", "replace:javax.persistence=jakarta.persistence")
    out = _run(kb.root, "cases", "show", OTHER).output
    assert "status:    active*" in out
    assert "generated: javax.persistence — package does not exist: status observed, action hint" in out


def test_parse_action():
    assert parse_action("recipe:a.B, c.D").recipe_ids == ["a.B", "c.D"]
    a = parse_action("replace:x=y", "file")
    assert (a.kind, a.replace_scope, a.replace_pairs[0].old, a.replace_pairs[0].new) == ("replace", "file", "x", "y")
    assert parse_action("block").kind == "block" and parse_action("block:needs lib 3").suggestion == "needs lib 3"
    for bad in ("recipe:", "handler", "replace:x=x", "replace:=y", "frobnicate"):
        with pytest.raises(ValueError):
            parse_action(bad)


def test_errors_show_names_the_case(kb):
    _seed(kb)
    out = _run(kb.root, "errors", "show", "b-1").output
    assert f"case:      {CID}  Request.clientIp — cannot find symbol  [candidate, replace" in out
    assert f"sbmig kb cases show {CID}" in out and "--- resolving diff (r2:a1)" in out
    assert "failure: search text not found" in out and "1. r2:a0  llm  apply_failed" in out


def test_reindex_learning_match_and_promote_mention_cases(kb):
    _seed(kb)
    r = _run(kb.root, "reindex")
    assert "case catalog: 2 case(s) (observed 1, candidate 1)" in r.output
    out = _run(kb.root, "learning").output
    assert "top error cases" in out and CID in out and "Request.clientIp — cannot find symbol" in out
    md = _run(kb.root, "learning", "--md").output
    assert "### Top error cases (case catalog" in md and f"| `{CID}` |" in md
    for sig in (L1_REQ, EC_L2):
        m = json.loads(_run(kb.root, "match", sig).output)
        assert [h["case_id"] for h in m["case_hits"]] == [CID] and m["case_hits"][0]["action"].startswith("replace")
        assert any(h.startswith(f"[case {CID}]") for h in m["hints"])
    assert json.loads(_run(kb.root, "match", "compile|nothing").output)["case_hits"] == []
    # a recipe promoted by hand refreshes the cases it covers
    kb.add_recipe(recipe_yaml("com.company.kb.RenameClientIp"),
                  KBRecipe(id="com.company.kb.RenameClientIp", path="", signatures=[EC_L2]))
    _run(kb.root, "reindex")
    assert kb.catalog().get(CID).status == "candidate" and kb.catalog().get(CID).action.kind == "recipe"
    r = _run(kb.root, "promote", "com.company.kb.RenameClientIp")
    assert r.exit_code == 0 and f"case catalog: 1 case(s) updated ({CID})" in r.output
    assert kb.catalog().get(CID).status == "active"
    r = _run(kb.root, "reject", "com.company.kb.RenameClientIp")
    assert f"({CID})" in r.output and kb.catalog().get(CID).action.kind == "replace"
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert st["catalog_cases"] == 2 and st["catalog_observed"] == 1


def _err():
    from .test_catalog import _err as make

    return make()


def _cluster():
    from .test_catalog_store import _cluster as make

    return make()
