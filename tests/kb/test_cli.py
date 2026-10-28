"""`sbmig kb …` (v2.1): errors list / show, cases list / show / retire / activate / pin / unpin, stats, validate,
export / import, reindex."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sbmig.core.models import FixTry
from sbmig.kb import FileKnowledgeBase, kb_cli

from .helpers import CASE, L2, entry

CODE = "def fix(p):\n    p.log('한글 로그')\n"


@pytest.fixture()
def seeded(kb_cfg):
    kb = FileKnowledgeBase(kb_cfg)
    kb.record_run([entry("aaaa11112222"),
                   entry("bbbb33334444", run_id="r2", project="other", status="unresolved",
                         tries=[FixTry(n=1, round=2, source="kb-script", script_version=1, applied="script",
                                       diff="-old\n+new", result="no_change", note="nothing changed")])])
    kb.cases.add_script(CASE, L2, CODE, run_id="r1", project="demo", analysis="원인", solution="해결")
    return kb


def run(kb, *args):
    res = CliRunner().invoke(kb_cli, list(args), obj={"kb": kb}, catch_exceptions=False)
    return res.exit_code, res.output


def test_errors_list_and_show(seeded):
    code, out = run(seeded, "errors", "list")
    assert code == 0 and "aaaa11112222  r1  resolved" in out and "[1 llm-new edits improved]" in out
    assert "2 of 2 entr(ies)" in out
    code, out = run(seeded, "errors", "list", "--status", "unresolved", "--case", CASE[:5])
    assert "bbbb33334444" in out and "aaaa11112222" not in out
    code, out = run(seeded, "errors", "list", "--run", "r1", "--json")
    assert [r["id"] for r in json.loads(out)] == ["aaaa11112222"]
    code, out = run(seeded, "errors", "show", "bbbb")
    assert code == 0 and "try 1 (round 2, kb-script v1, applied script, result no_change)" in out
    assert "+new" in out and "note: nothing changed" in out
    assert "+new" not in run(seeded, "errors", "show", "bbbb", "--no-diff")[1]
    assert run(seeded, "errors", "show", "zzzz")[0] == 1


def test_cases_list_show_retire_activate(seeded):
    code, out = run(seeded, "cases", "list")
    assert code == 0 and f"{CASE}  projects 2" in out and "scripts [v1 unverified 0/0]" in out
    code, out = run(seeded, "cases", "show", CASE[:6])
    assert code == 0 and f"case {CASE}" in out and "| def fix(p):" in out and "한글 로그" in out
    assert "KB 탐색: 저장된 스크립트 v1 (unverified" in out and "last 2 of 2 entr(ies)" in out
    assert "| def fix(p)" not in run(seeded, "cases", "show", CASE, "--no-code")[1]
    # an earlier test's console log handler may print "[kb] …" lines first: the command's own line is the last one
    assert run(seeded, "cases", "retire", CASE, "1")[1].strip().splitlines()[-1] == f"case {CASE} script v1: retired"
    assert seeded.cases.best(CASE) is None
    assert "unverified" in run(seeded, "cases", "activate", CASE, "1")[1]
    assert run(seeded, "cases", "retire", CASE, "7")[0] == 1
    assert run(seeded, "cases", "show", "nope")[0] == 1


def test_cases_pin_unpin(seeded):
    code, out = run(seeded, "cases", "pin", CASE, "block:handled by the owners")
    assert code == 0 and "pinned: block:handled by the owners" in out
    assert seeded.cases.pin_of(CASE) == "block:handled by the owners"
    assert run(seeded, "cases", "pin", CASE, "script:1")[0] == 1
    assert run(seeded, "cases", "unpin", CASE)[0] == 0 and seeded.cases.pin_of(CASE) == ""
    assert run(seeded, "cases", "pin", CASE[:6], "block")[1].strip().splitlines()[-1] == f"case {CASE} pinned: block"


def test_cases_pin_refuses_an_unknown_case_unless_create(seeded):
    """v2.1h (A19): a typo never makes a new pinned case folder; `--create` pins a case the KB has not seen yet."""
    typo = "0123456789"
    code, out = run(seeded, "cases", "pin", typo, "block:typo")
    assert code == 1 and f"unknown case {typo}; use --create to pin a case the KB has not seen" in out
    assert not (seeded.cases.cases_dir / typo).exists() and typo not in seeded.cases.case_ids()
    assert run(seeded, "cases", "pin", "zz", "block")[0] == 1                      # too short for a prefix
    code, out = run(seeded, "cases", "pin", typo, "block:pre-pinned", "--create")
    assert code == 0 and f"case {typo} pinned: block:pre-pinned" in out and seeded.cases.pin_of(typo) == "block:pre-pinned"
    code, out = run(seeded, "cases", "pin", "abc", "block", "--create")           # not a case id: no folder
    assert code == 1 and "abc is no case id" in out and not (seeded.cases.cases_dir / "abc").exists()
    assert run(seeded, "cases", "pin", "../../evil", "block", "--create")[0] == 1


def test_stats_validate_reindex_export_import(seeded, tmp_path):
    code, out = run(seeded, "stats", "--json")
    st = json.loads(out)
    assert (st["errors"], st["cases"], st["script_versions"]) == (2, 1, 1)
    assert "errors" in run(seeded, "stats")[1]
    code, out = run(seeded, "validate")
    assert code == 0 and out.startswith("OK: 2 error entr(ies), 1 case(s)")
    out = run(seeded, "reindex")[1]
    assert "1 case(s), 1 script version(s)" in out
    assert "entr(ies) given as-is/to-be hunks, 0 build-resolved entr(ies) given a probable fixer — " in out   # A15
    zp = tmp_path / "kb.zip"
    assert run(seeded, "export", str(zp))[0] == 0 and zp.is_file()
    code, out = run(seeded, "import", str(zp))
    assert code == 0 and "errors_added 0" in out
    with open(seeded.cases.errors_path, "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    code, out = run(seeded, "validate")
    assert code == 1 and "PROBLEM: errors.jsonl: line 3 is no ErrorEntry" in out


def test_the_kb_dir_option(kb_cfg, tmp_path):
    other = tmp_path / "elsewhere"
    FileKnowledgeBase(kb_cfg, kb_dir=other).record_run([entry("cccc")])
    res = CliRunner().invoke(kb_cli, ["--kb-dir", str(other), "errors", "list"], obj={"cfg": kb_cfg})
    assert res.exit_code == 0 and "cccc" in res.output
