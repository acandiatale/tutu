"""The case catalog inside FileKnowledgeBase: append → case files + views, match → case_hits + hints, reindex,
stats, export / import (human blocks), prepare_error_cases → case_id."""
from __future__ import annotations

import csv
import zipfile

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind
from sbmig.kb import FileKnowledgeBase, prepare_error_cases
from sbmig.kb.catalog import CaseCatalog, case_id_for
from sbmig.kb.error_cases import write_error_cases_csv

from .helpers import EC_FILE, EC_L2, ec_attempt, ec_error, with_kb
from .test_catalog import L1_REQ, rec, unresolved

CID = case_id_for(EC_L2)


def _cluster(l2: str = EC_L2, l1: str = L1_REQ) -> ErrorCluster:
    e = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=EC_FILE, line=5,
                   symbol="method clientIp()", location="class com.example.Request", signature=l1,
                   signature_generic=l2)
    return ErrorCluster(id="c1", kind=ErrorKind.COMPILE, file=EC_FILE, errors=[e], signatures=[l1])


def test_prepare_fills_case_id_and_csv_column(kb_cfg, ws, profile, tmp_path):
    errors, _ = prepare_error_cases([ec_error(), ec_error("e2", l2="", status="unresolved")], [ec_attempt()],
                                    profile, ws, kb_cfg)
    assert errors[0].case_id == CID and errors[1].case_id == ""
    p = write_error_cases_csv(errors, tmp_path / "e.csv")
    rows = list(csv.DictReader(p.open(encoding="utf-8-sig", newline="")))
    assert rows[0]["case_id"] == CID and rows[1]["case_id"] == ""
    # a record from before the catalog (no case_id) still gets its column filled
    old = ec_error().model_copy(update={"case_id": ""})
    p = write_error_cases_csv([old], tmp_path / "old.csv")
    assert next(csv.DictReader(p.open(encoding="utf-8-sig", newline="")))["case_id"] == CID


def test_append_writes_catalog_and_views(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(kb_cfg)
    errs, atts = prepare_error_cases([ec_error()], [ec_attempt()], profile, ws, kb_cfg)
    assert kb.append_error_cases(errs, atts) == 2                    # the count is the events' count
    assert (kb.root / "catalog" / "index.json").exists()
    assert (kb.root / "views" / "cases.md").exists() and (kb.root / "views" / "cases.csv").exists()
    c = kb.catalog().get(CID)
    assert c.evidence.seen == 1 and c.evidence.projects == 1 and c.action.kind == "replace"
    assert kb.append_error_cases(errs, atts) == 0                    # nothing new, catalog unchanged
    assert kb.catalog().get(CID).evidence.seen == 1
    # kb.catalog: false → events only
    off = FileKnowledgeBase(with_kb(kb_cfg, dir=str(kb.root.parent / "kb-off"), catalog=False))
    assert off.append_error_cases(errs, atts) == 2
    assert not (off.root / "catalog").exists() and not (off.root / "views").exists()


def test_append_survives_a_catalog_failure(kb, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(CaseCatalog, "write_views", boom)
    assert kb.append_error_cases([rec("e1")], []) == 1
    assert kb.error_case_store().count_errors() == 1


def test_match_case_hits_and_hints(kb, ws, profile, kb_cfg):
    kb.append_error_cases([unresolved("e0", "r0", "zeta")], [])
    kb.append_error_cases([rec("e1", hours=1)], [])
    m = kb.match(_cluster(), ws, profile)
    assert [h.case_id for h in m.case_hits] == [CID] and m.case_hits[0].action.kind == "replace"
    assert m.hints == [
        f"[case {CID}] past runs: fixed by llm fake-model in 1 project(s): renamed clientIp to remoteClientIp; "
        "replace `clientIp` → `remoteClientIp`",
        f"[case {CID}] past failure ×1: llm no_change — the targeted error(s) were still there after the rebuild; "
        "do not repeat it",
    ]
    assert not any(h.startswith("[error ") for h in m.hints)         # the raw error-hint lines are not repeated
    # people's hints first, then the generated ones; existing pattern hints stay in front; max_hints caps
    kb.catalog().set_human(CID, hints=["check RequestWrapper first"])
    kb.ensure_layout()
    (kb.patterns_dir / "p.yml").write_text("match: {signature_regex: 'cannot_find_symbol'}\nhints: [use X]\n")
    m = kb.match(_cluster(), ws, profile)
    assert m.hints[:3] == ["[pattern p] use X", f"[case {CID}] check RequestWrapper first",
                           f"[case {CID}] past runs: fixed by llm fake-model in 1 project(s): renamed clientIp to "
                           "remoteClientIp; replace `clientIp` → `remoteClientIp`"]
    assert FileKnowledgeBase(with_kb(kb_cfg, max_hints=2)).match(_cluster(), ws, profile).hints == m.hints[:2]
    # kb.error_hints: false keeps only people's hints
    only_human = FileKnowledgeBase(with_kb(kb_cfg, error_hints=False)).match(_cluster(), ws, profile).hints
    assert only_human == ["[pattern p] use X", f"[case {CID}] check RequestWrapper first"]
    # kb.catalog: false → the old raw error-hint lines, no case hits
    old = FileKnowledgeBase(with_kb(kb_cfg, catalog=False)).match(_cluster(), ws, profile)
    assert old.case_hits == [] and any(h.startswith(f"[error {EC_L2}]") for h in old.hints)
    # a rejected case gives neither hits nor hints
    kb.catalog().set_human(CID, status="rejected")
    m = kb.match(_cluster(), ws, profile)
    assert m.case_hits == [] and m.hints == ["[pattern p] use X"]


def test_match_without_catalog_index_falls_back_to_error_hints(kb, ws, profile):
    kb.append_error_cases([rec("e1")], [])
    import shutil

    shutil.rmtree(kb.root / "catalog")
    m = kb.match(_cluster(), ws, profile)
    assert m.case_hits == [] and m.hints and m.hints[0].startswith(f"[error {EC_L2}]")


def test_reindex_rebuilds_catalog_and_keeps_human(kb):
    kb.append_error_cases([rec("e1"), unresolved("e2", "r2", "beta", hours=1)], [])
    cat = kb.catalog()
    cat.set_human(CID, notes="keep me")
    p = cat.path_of(CID)
    p.write_text(p.read_text(encoding="utf-8").replace("  notes: keep me\n", "  notes: keep me   # comment\n"),
                 encoding="utf-8")
    (kb.root / "views" / "cases.md").unlink()
    (kb.root / "catalog" / "index.json").unlink()
    stray = kb.root / "catalog" / "compile" / "cannot_find_symbol" / "stray--abcdef0123.yml"
    stray.write_text("id: abcdef0123\nsignature_generic: compile|x|y\n", encoding="utf-8")   # no events, no human
    kb.reindex()
    assert (kb.root / "views" / "cases.md").exists() and (kb.root / "catalog" / "index.json").exists()
    assert not stray.exists()
    c = cat.get(CID)
    assert c.human.notes == "keep me" and c.evidence.seen == 2
    assert "# comment" in cat.path_of(CID).read_text(encoding="utf-8")
    st = kb.stats()
    assert st["catalog_cases"] == 1 and st["catalog_candidate"] == 1 and st["catalog_rejected"] == 0
    cat.set_human(CID, status="rejected")
    assert kb.stats()["catalog_rejected"] == 1 and kb.stats()["catalog_candidate"] == 0


def test_export_import_round_trip_with_human_blocks(kb, kb_cfg, tmp_path):
    other_l2 = "compile|cannot_find_symbol|Other"
    other_id = case_id_for(other_l2)
    kb.append_error_cases([rec("e1"), rec("o1", run="r2", project="beta", l2=other_l2,
                                          l1="compile|cannot find symbol|class Other", status="unresolved")], [])
    kb.catalog().set_human(CID, notes="from the zip")
    kb.catalog().set_human(other_id, status="rejected")
    out = kb.export_zip(tmp_path / "kb.zip")
    names = set(zipfile.ZipFile(out).namelist())
    assert "catalog/index.json" in names and "views/cases.md" in names and "views/cases.csv" in names
    assert f"catalog/compile/cannot_find_symbol/clientIp--{CID}.yml" in names

    # an empty KB takes everything, human blocks included
    empty = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb-empty")))
    counters = empty.import_zip(out)
    assert counters["case_humans_added"] == 2 and counters["error_cases_added"] == 2
    assert empty.catalog().get(CID).human.notes == "from the zip"
    assert empty.catalog().get(other_id).effective_status() == "rejected"

    # a KB with its own version of CID keeps its human block; the zip's is taken only for the unknown case
    local = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb-local")))
    local.append_error_cases([rec("l1", run="r9", project="omega")], [])
    local.catalog().set_human(CID, notes="local decision")
    counters = local.import_zip(out)
    assert counters["case_humans_added"] == 1
    c = local.catalog().get(CID)
    assert c.human.notes == "local decision" and c.evidence.seen == 2 and c.evidence.projects == 2
    assert local.catalog().get(other_id).effective_status() == "rejected"
    assert local.import_zip(out)["case_humans_added"] == 0           # idempotent

    # --replace: the zip's catalog replaces the local one (rebuilt from the zip's events)
    local.import_zip(out, merge=False)
    c = local.catalog().get(CID)
    assert c.human.notes == "from the zip" and c.evidence.seen == 1


def test_a_rejected_case_also_stops_patch_replay_recipes_and_symbol_hints(kb, ws, profile):
    """Round 10 (C5): `sbmig kb cases reject` used to stop only the case's own action / hints."""
    from sbmig.core.models import FileChange, KBRecipe

    from .helpers import make_case, recipe_yaml

    target = "src/main/java/com/example/web/IpFilter.java"
    ws.write(target, "class IpFilter { String ip(Request req) { return req.clientIp(); } }\n")
    change = FileChange(path=target, op="modify", search="return req.clientIp();",
                        replace="return req.remoteClientIp();")
    case = make_case(signatures=[L1_REQ], signatures_generic=[EC_L2], changes=[change], symbol="clientIp",
                     fix_labels=["method-rename"], replacement="clientIp → remoteClientIp")
    kb.record_case(case)
    kb.add_recipe(recipe_yaml("RenameClientIp"), KBRecipe(id="RenameClientIp", status="active", path="",
                                                            signatures=[EC_L2]))
    idx = kb.symbol_index()
    idx.add(kb.get_case(next(iter(kb._load_index()["l2"][EC_L2]))))
    idx.save()
    kb.append_error_cases([rec("e1")], [])
    m = kb.match(_cluster(), ws, profile)
    assert m.replayable and m.recipe_ids == ["RenameClientIp"] and any(h.startswith("[symbol clientIp]")
                                                                      for h in m.hints)
    kb.catalog().set_human(CID, status="rejected")
    m = kb.match(_cluster(), ws, profile)
    assert m.case_hits == [] and m.replayable == [] and m.cases == [] and m.recipe_ids == []
    assert not any(h.startswith(("[symbol", "[case")) for h in m.hints)
    assert kb.catalog().rejected_signatures([EC_L2, L1_REQ, "other"]) == {EC_L2, L1_REQ}


def test_first_catalog_build_of_a_fresh_kb_audits_created_cases(tmp_path):
    """2026-10-26: the first update of a fresh KB builds the whole catalog (no index yet) and wrote no `created`
    lines to audit.jsonl for its candidate cases; `kb reindex` still logs nothing for cases it merely recomputes."""
    import json

    from sbmig.core.config import load_config
    from sbmig.kb.catalog import CaseCatalog

    from .test_catalog import rec

    kb = tmp_path / "kb"
    kb.mkdir()
    cfg = load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                 "kb": {"dir": str(kb), "case_replace": True}})      # v1 learned replaces (off in v2)
    r = rec("e1", run="r1", project="alpha")                       # resolved by an LLM text replace → candidate
    (kb / "errors.jsonl").write_text(r.model_dump_json() + "\n", encoding="utf-8")
    cat = CaseCatalog(kb, cfg)
    assert cat.update_from([r]) and (kb / "catalog" / "index.json").exists()
    lines = [json.loads(x) for x in (kb / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(x["event"], x["reason"], x["after"]["status"]) for x in lines] == [("status", "created", "candidate")]
    CaseCatalog(kb, cfg).rebuild()                                  # kb reindex: nothing new to log
    assert len((kb / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 1
