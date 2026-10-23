"""`sbmig kb errors …` over a temp KB: list / show / summary / replace-candidates / export."""
from __future__ import annotations

import csv
import json

from typer.testing import CliRunner

from sbmig.core.models import ErrorAttemptRef
from sbmig.kb import FileKnowledgeBase, kb_cli, prepare_error_cases

from .helpers import CODE_EDIT_DIFF, EC_FILE, EC_L2, ec_attempt, ec_error

runner = CliRunner()


def _run(kb_dir, *args):
    return runner.invoke(kb_cli, ["--kb-dir", str(kb_dir), *args], catch_exceptions=False)


def _seed(kb: FileKnowledgeBase, ws, profile) -> None:
    """r1/alpha: resolved by an LLM text replace after a failed code edit; r2/beta: unresolved after a regression."""
    refs = [ErrorAttemptRef(attempt_id="r1:a0", fix_method="llm", outcome="no_change"),
            ErrorAttemptRef(attempt_id="r1:a1", fix_method="llm", outcome="improved")]
    errs, atts = prepare_error_cases(
        [ec_error("e1", attempts=refs, description="renamed the call")],
        [ec_attempt("a0", outcome="no_change", diff=CODE_EDIT_DIFF,
                    failure="the targeted error(s) were still there after the rebuild"), ec_attempt("a1")],
        profile.model_copy(update={"name": "alpha", "fingerprint": "fp-a"}), ws, kb.cfg)
    kb.append_error_cases(errs, atts)
    refs = [ErrorAttemptRef(attempt_id="r2:a0", fix_method="llm", outcome="regressed")]
    errs, atts = prepare_error_cases(
        [ec_error("e9", run_id="r2", status="unresolved", attempts=refs,
                  failure_reason="`llm` attempt a0 regressed: new errors in IpFilter.java")],
        [ec_attempt("a0", run_id="r2", outcome="regressed", error_ids=("e9",), failure="regressed: 2 new errors")],
        profile.model_copy(update={"name": "beta", "fingerprint": "fp-b"}), ws, kb.cfg)
    kb.append_error_cases(errs, atts)


def test_errors_list(kb, ws, profile):
    assert "no error cases" in _run(kb.root, "errors", "list").output
    _seed(kb, ws, profile)
    r = _run(kb.root, "errors", "list")
    assert r.exit_code == 0
    lines = r.output.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("e1  resolved") and "text_replace" in lines[0] and f"{EC_FILE}:5" in lines[0]
    assert "llm fake-model [clientIp→remoteClientIp]: renamed the call" in lines[0]
    assert lines[1].startswith("e9  unresolved") and "regressed" in lines[1]
    assert _run(kb.root, "errors", "list", "--status", "unres").output.startswith("e9")
    assert _run(kb.root, "errors", "list", "--method", "llm", "--change-kind", "text").output.startswith("e1")
    assert _run(kb.root, "errors", "list", "--project", "beta").output.startswith("e9")
    assert _run(kb.root, "errors", "list", "--run", "r1").output.startswith("e1")
    assert _run(kb.root, "errors", "list", "--signature", "nothing").output.strip() == "no error cases"
    assert len(_run(kb.root, "errors", "list", "--symbol", "clientIp", "--limit", "1").output.splitlines()) == 1
    rows = [json.loads(x) for x in _run(kb.root, "errors", "list", "--json").output.splitlines()]
    assert [x["id"] for x in rows] == ["e1", "e9"] and rows[0]["replace_rule_candidate"] is True
    assert _run(kb.root, "errors", "list", "--status", "(").exit_code != 0       # bad regex


def test_errors_show(kb, ws, profile):
    _seed(kb, ws, profile)
    r = _run(kb.root, "errors", "show", "e1")
    assert r.exit_code == 0
    assert '"id": "e1"' in r.output and "=== attempt r1:a0" in r.output and "=== attempt r1:a1" in r.output
    assert "+        return req.remoteClientIp();" in r.output and "Locale.ROOT" in r.output   # both diffs
    data = json.loads(_run(kb.root, "errors", "show", "e1", "--json").output)
    assert data["error"]["id"] == "e1" and [a["id"] for a in data["attempts"]] == ["r1:a0", "r1:a1"]
    assert _run(kb.root, "errors", "show", "e9").exit_code == 0
    r = _run(kb.root, "errors", "show", "r2:a0")                                  # an attempt id works too
    assert r.exit_code == 0 and '"outcome": "regressed"' in r.output and "--- diff" in r.output
    assert _run(kb.root, "errors", "show", "e").exit_code == 1                     # ambiguous prefix
    assert _run(kb.root, "errors", "show", "nope").exit_code == 1


def test_errors_summary_and_replace_candidates(kb, ws, profile):
    _seed(kb, ws, profile)
    r = _run(kb.root, "errors", "summary")
    assert r.exit_code == 0
    assert "2 error case(s), 3 attempt(s), 2 project(s), 2 run(s); LLM text replaces: 1" in r.output
    assert "status:      resolved=1, unresolved=1" in r.output and "resolved by: llm=1" in r.output
    assert "regressed" in r.output and EC_L2 in r.output
    s = json.loads(_run(kb.root, "errors", "summary", "--run", "r2", "--json").output)
    assert s["by_status"] == {"unresolved": 1} and s["errors"] == 1
    r = _run(kb.root, "errors", "replace-candidates")
    assert r.exit_code == 0 and "`clientIp` → `remoteClientIp`" in r.output and "projects=1" in r.output
    rows = json.loads(_run(kb.root, "errors", "replace-candidates", "--json").output)
    assert rows[0]["old"] == "clientIp" and rows[0]["example_attempt"] == "r1:a1"
    assert "no text-replace candidates" in _run(kb.root, "errors", "replace-candidates", "--min-projects",
                                                "2").output


def test_errors_export(kb, ws, profile, tmp_path):
    _seed(kb, ws, profile)
    assert _run(kb.root, "errors", "export").exit_code != 0                       # nothing to write
    out, att = tmp_path / "x" / "errors.csv", tmp_path / "x" / "attempts.csv"
    r = _run(kb.root, "errors", "export", "--csv", str(out), "--attempts-csv", str(att))
    assert r.exit_code == 0 and "wrote 2 error case(s)" in r.output and "wrote 3 fix attempt(s)" in r.output
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig", newline="")))
    assert [x["id"] for x in rows] == ["e1", "e9"] and rows[0]["attempts"] == "a0:llm:no_change; a1:llm:improved"
    r = _run(kb.root, "errors", "export", "--status", "unresolved", "--csv", str(out), "--attempts-csv", str(att))
    assert "wrote 1 error case(s)" in r.output and "wrote 1 fix attempt(s)" in r.output
    arows = list(csv.DictReader(att.open(encoding="utf-8-sig", newline="")))
    assert [x["id"] for x in arows] == ["r2:a0"] and arows[0]["outcome"] == "regressed"


def test_stats_reindex_learning_mention_error_cases(kb, ws, profile):
    _seed(kb, ws, profile)
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert st["error_cases"] == 2 and st["fix_attempts"] == 3
    kb.error_case_store().hints_path.unlink()
    r = _run(kb.root, "reindex")
    assert "error hints: 1 L2 signature(s), 1 symbol(s)" in r.output
    assert "error cases: 2 (resolved=1, unresolved=1)" in _run(kb.root, "learning").output
    assert "## Error cases" in _run(kb.root, "learning", "--md").output
