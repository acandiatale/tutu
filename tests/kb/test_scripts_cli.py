"""`sbmig kb scripts list | show | retire | activate | validate` over a temp v2 KB, and the script parts of
`kb cases pin / show`, `kb errors list / show`, `kb reindex`, `kb stats`."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from sbmig.core.models import FixAttemptRecord
from sbmig.kb import FileKnowledgeBase, kb_cli
from sbmig.kb.catalog import case_id_for
from sbmig.kb.cli import parse_action

from .helpers import EC_L2
from .test_catalog import unresolved
from .test_scripts import FIX_A, FIX_B, FIX_C, SECRET_FIX
from .test_scripts_catalog import add, by_script

runner = CliRunner()
CID = case_id_for(EC_L2)
OTHER = "0123456789"


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase) -> None:
    """CID: v1 (alpha, beta ok), v2 augments v1 (one failure), v3 quarantined; OTHER: v1 never proven."""
    st = kb.scripts
    st.add(CID, EC_L2, FIX_A, analysis="rename clientIp → remoteClientIp", model="gpt", run_id="r1",
           project="alpha")
    st.add(CID, EC_L2, FIX_B, parent=1, model="gpt", run_id="r2", project="beta")
    st.add(CID, EC_L2, SECRET_FIX, run_id="r3")
    st.credit(CID, 1, "improved", project="alpha", run_id="r1")
    st.credit(CID, 1, "build_ok", project="beta", run_id="r2")
    st.credit(CID, 2, "no_change", project="beta", run_id="r2")
    st.add(OTHER, "compile|package_does_not_exist|javax.persistence", FIX_C)
    add(kb, by_script("e1", "r1", "alpha"))


def test_scripts_list(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    assert _run(kb.root, "scripts", "list").output.strip() == "no fix scripts"
    _seed(kb)
    r = _run(kb.root, "scripts", "list")
    assert r.exit_code == 0
    lines = r.output.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(f"{CID}  versions=3   best=v1    ok=2    failed=1    projects=2")
    assert "last=no_change" in lines[0] and lines[0].rstrip().endswith(EC_L2)
    assert lines[1].startswith(f"{OTHER}  versions=1   best=-") and "last=-" in lines[1]
    # one case with its versions (newest first)
    out = _run(kb.root, "scripts", "list", "--case", CID[:4]).output.splitlines()
    assert out[0].startswith(CID) and [ln.split()[0] for ln in out[1:]] == ["v3", "v2", "v1"]
    assert "v2 ← v1" in out[2] and "quarantined" in out[1] and "ok=2" in out[3] and "llm/gpt" in out[3]
    # status filter: counts over the matching versions only
    q = _run(kb.root, "scripts", "list", "--status", "quarantined").output.strip().splitlines()
    assert len(q) == 1 and q[0].startswith(f"{CID}  versions=1")
    rows = json.loads(_run(kb.root, "scripts", "list", "--json").output)
    assert [x["case_id"] for x in rows] == [CID, OTHER] and rows[0]["best"] == 1
    assert [m["version"] for m in rows[0]["version_list"]] == [1, 2, 3]
    assert _run(kb.root, "scripts", "list", "--status", "(").exit_code != 0
    assert len(_run(kb.root, "scripts", "list", "-n", "1").output.strip().splitlines()) == 1


def test_scripts_show(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    _seed(kb)
    r = _run(kb.root, "scripts", "show", CID[:5])                 # default: the version a reuse runs first
    assert r.exit_code == 0
    out = r.output
    assert out.startswith(f"fix script {CID} v1 — active")
    assert "Request.clientIp — cannot find symbol" in out and f"sbmig kb cases show {CID}" in out
    assert f"key (L2):  {EC_L2}" in out and "lineage:   v1\n" in out
    assert "record:    2 ok / 0 failed / 0 partial; fixed in 2 project(s): alpha, beta" in out
    assert "made by:   llm gpt in run r1 (alpha)" in out and "reuse:     reused first" in out
    assert "analysis:\n  rename clientIp → remoteClientIp" in out
    assert "other versions:" in out and f"--- scripts/{CID}/v1.py\n{FIX_A.rstrip()}" in out
    r = _run(kb.root, "scripts", "show", CID, "--version", "2", "--no-code")
    assert "lineage:   v1 → v2" in r.output and "not reused (no success yet)" in r.output
    assert "--- scripts/" not in r.output
    q = _run(kb.root, "scripts", "show", CID, "-v", "3").output
    assert "— quarantined" in q and "not reused (quarantined)" in q and "hunter2" not in q
    assert "note:      quarantined:" in q
    data = json.loads(_run(kb.root, "scripts", "show", CID, "--version", "2", "--json").output)
    assert data["meta"]["version"] == 2 and data["code"] == FIX_B and data["lineage"] == [1, 2]
    assert _run(kb.root, "scripts", "show", CID, "--version", "9").exit_code == 1
    assert _run(kb.root, "scripts", "show", "ffff").exit_code == 1
    nothing = _run(kb.root, "scripts", "show", OTHER).output          # no proven version: the newest active one
    assert nothing.startswith(f"fix script {OTHER} v1 — active")


def test_scripts_retire_activate_refresh_the_case(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    _seed(kb)
    assert kb.catalog().get(CID).action.script_version == 1
    r = _run(kb.root, "scripts", "retire", CID, "1", "--reason", "touches web.xml")
    assert r.exit_code == 0 and r.output.startswith(f"retired {CID} v1:") and "action hint" in r.output
    assert kb.scripts.get_meta(CID, 1).status == "retired" and kb.catalog().get(CID).action.kind == "hint"
    assert "| hint |" in (kb.root / "views" / "cases.md").read_text(encoding="utf-8")
    r = _run(kb.root, "scripts", "activate", CID, "1")
    assert r.exit_code == 0 and "action script v1" in r.output
    assert kb.catalog().get(CID).action.script_version == 1
    r = _run(kb.root, "scripts", "activate", CID, "3")
    assert r.exit_code == 1 and "quarantined" in r.output
    assert _run(kb.root, "scripts", "retire", CID, "9").exit_code == 1
    audit = [json.loads(x) for x in (kb.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    mine = [a for a in audit if a.get("event") == "script" and a["action"] in ("retired", "activated")]
    assert [a["action"] for a in mine] == ["retired", "activated"]
    assert mine[0]["reason"] == "touches web.xml" and mine[0]["command"] == "sbmig kb scripts retire"
    # retiring a pinned version says it stays pinned
    _run(kb.root, "cases", "pin", CID, "--action", "script:2")
    r = _run(kb.root, "scripts", "retire", CID, "2")
    assert r.exit_code == 0 and "the pin is ignored while the version is retired" in r.output


def test_scripts_validate(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    _seed(kb)
    r = _run(kb.root, "scripts", "validate")
    assert r.exit_code == 0 and r.output.startswith("OK: 2 case(s), 4 version(s)")
    kb.scripts.meta_path(CID, 2).write_text("{broken", encoding="utf-8")
    r = _run(kb.root, "scripts", "validate")
    assert r.exit_code == 1 and f"PROBLEM: scripts/{CID}/v2.meta.json: does not parse" in r.output
    assert "fix scripts: 2 case(s), 3 version(s) (1 proven, 0 retired, 1 quarantined); 1 problem(s)" in \
        _run(kb.root, "reindex").output
    out = _run(kb.root, "stats", "--json").stdout
    # the warning about the broken meta may reach the captured output when an earlier test installed a log handler
    st, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
    assert st["script_cases"] == 2 and st["script_versions"] == 3 and st["reviews"] == 0


def test_cases_pin_script_and_show_section(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    _seed(kb)
    r = _run(kb.root, "cases", "pin", CID, "--action", "script:v2")
    assert r.exit_code == 0 and "action script v2*" in r.output
    assert [m.version for m, _ in kb.scripts.best(CID, limit=3)] == [2, 1]
    r = _run(kb.root, "cases", "pin", CID, "--action", "script:7")
    assert r.exit_code == 1 and "no stored fix script v7" in r.output
    assert _run(kb.root, "cases", "pin", CID, "--action", "script:x").exit_code != 0
    out = _run(kb.root, "cases", "show", CID, "--no-diff").output
    sec = out[out.index("fix scripts (3 version(s)"):]
    assert f"`sbmig kb scripts show {CID} [--version N]`" in sec
    assert "v2 ← v1" in sec and "← reused first" in sec.splitlines()[2] and "quarantined" in sec.splitlines()[1]
    hist = out[out.index("history"):]
    assert "script v1 credited improved (alpha)" in hist and "sbmig kb cases pin: action, status" in hist
    assert "script script:" in out                                      # the fix row of the script version
    assert _run(kb.root, "cases", "list", "--action", "script").output.startswith(CID)


def test_parse_script_action():
    a = parse_action("script:3")
    assert a.kind == "script" and a.script_version == 3 and parse_action("script:v2").script_version == 2
    for bad in ("script", "script:0", "script:-1", "script:vx"):
        try:
            parse_action(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_errors_list_and_show_name_the_script(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    _seed(kb)
    kb.append_error_cases([by_script("e2", "r2", "beta", method="script_reuse", hours=1),
                           unresolved("e3", "r3", "gamma", hours=2)],
                          [FixAttemptRecord(id="r2:a1", run_id="r2", project="beta", fix_method="script_reuse",
                                            resolver_id=f"script:{CID}:v1", outcome="improved", applied=True,
                                            script_mode="reuse", script_case_id=CID, script_version=1,
                                            script_status="ok", script_log=["src/A.java: 1 replacement"],
                                            diff="(diff)")])
    lines = _run(kb.root, "errors", "list").output.strip().splitlines()
    e2 = next(ln for ln in lines if ln.startswith("e2"))
    assert "script_reuse v1" in e2 and f"script:{CID}:v1" in e2
    assert "script v1" in next(ln for ln in lines if ln.startswith("e1"))
    assert "script_reuse" in _run(kb.root, "errors", "list", "--method", "script_reuse").output
    out = _run(kb.root, "errors", "show", "e2").output
    assert f"script:    case {CID} v1  → sbmig kb scripts show {CID} --version 1" in out
    assert "script:  reuse v1, ok" in out
    att = _run(kb.root, "errors", "show", "r2:a1").output
    assert f"script:    reuse v1, ok  (case {CID})" in att and "log:       src/A.java: 1 replacement" in att
    summary = _run(kb.root, "errors", "summary").output
    assert "script_reuse   improved=1" in summary
