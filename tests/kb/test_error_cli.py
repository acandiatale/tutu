"""`sbmig kb errors …` over a temp KB: list / show / summary / export (`replace-candidates` is gone with the
learned text replaces, PLAN-v2 step 4)."""
from __future__ import annotations

import csv
import json

from typer.testing import CliRunner

from sbmig.core.models import ErrorAttemptRef
from sbmig.kb import FileKnowledgeBase, kb_cli, prepare_error_cases
from sbmig.kb.catalog import case_id_for

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
    assert [x["id"] for x in rows] == ["e1", "e9"] and rows[0]["change_kind"] == "text_replace"
    assert _run(kb.root, "errors", "list", "--status", "(").exit_code != 0       # bad regex


def test_errors_show(kb, ws, profile):
    _seed(kb, ws, profile)
    r = _run(kb.root, "errors", "show", "e1")
    assert r.exit_code == 0
    out = r.output
    # readable by default: header fields, the case, the attempt history with failure reasons, the resolving diff
    assert out.startswith("error e1 — resolved") and "project:   alpha (run r1," in out
    assert f"error:     compile  {EC_FILE}:5" in out and "L2:        " + EC_L2 in out
    assert "resolved:  at " in out and "by llm fake-model (attempt r1:a1, targeted)" in out
    assert "how:       renamed the call" in out and "replace `clientIp` → `remoteClientIp`" in out
    assert "case:      " in out and "Request.clientIp — cannot find symbol" in out and "sbmig kb cases show" in out
    assert "1. r1:a0  llm fake-model  no_change  [code_edit]" in out
    assert "failure: the targeted error(s) were still there after the rebuild" in out
    assert "2. r1:a1  llm fake-model  improved  [text_replace]  ← resolved it" in out
    assert "--- resolving diff (r1:a1)" in out and "+        return req.remoteClientIp();" in out
    assert "Locale.ROOT" not in out and '"id"' not in out                       # the failed diff only on demand
    assert "Locale.ROOT" in _run(kb.root, "errors", "show", "e1", "--all-diffs").output
    data = json.loads(_run(kb.root, "errors", "show", "e1", "--json").output)
    assert data["error"]["id"] == "e1" and [a["id"] for a in data["attempts"]] == ["r1:a0", "r1:a1"]
    assert data["error"]["case_id"] == case_id_for(EC_L2)
    r = _run(kb.root, "errors", "show", "e9")
    assert r.exit_code == 0 and r.output.startswith("error e9 — unresolved")
    assert "why not:   `llm` attempt a0 regressed: new errors in IpFilter.java" in r.output
    assert "1. r2:a0  llm fake-model  regressed" in r.output and "resolving diff" not in r.output
    r = _run(kb.root, "errors", "show", "r2:a0")                                  # an attempt id works too
    assert r.exit_code == 0 and r.output.startswith("attempt r2:a0 — llm fake-model regressed")
    assert "failure:   regressed: 2 new errors" in r.output and "--- diff" in r.output
    assert json.loads(_run(kb.root, "errors", "show", "r2:a0", "--json").output)["outcome"] == "regressed"
    assert _run(kb.root, "errors", "show", "e").exit_code == 1                     # ambiguous prefix
    assert _run(kb.root, "errors", "show", "nope").exit_code == 1


def test_errors_show_prints_every_attempt_of_a_multi_attempt_resolution(kb, ws, profile):
    # 2026-10-25: a0 (no_change) stayed in the tree and is part of the fix → both diffs by default, in run order
    refs = [ErrorAttemptRef(attempt_id="r1:a0", fix_method="llm", outcome="no_change"),
            ErrorAttemptRef(attempt_id="r1:a1", fix_method="llm", outcome="improved")]
    errs, atts = prepare_error_cases(
        [ec_error("e1", attempts=refs, resolution_attempts=["r1:a0", "r1:a1"])],
        [ec_attempt("a0", outcome="no_change", diff=CODE_EDIT_DIFF), ec_attempt("a1")], profile, ws, kb.cfg)
    kb.append_error_cases(errs, atts)
    out = _run(kb.root, "errors", "show", "e1").output
    assert "1. r1:a0  llm fake-model  no_change  [code_edit]  ← part of the resolution (kept in the tree)" in out
    assert "2. r1:a1  llm fake-model  improved  [text_replace]  ← resolved it" in out
    assert "--- resolution: 2 attempts whose changes stayed in the tree (run order)" in out
    assert out.index("--- diff of r1:a0 (no_change)") < out.index("--- diff of r1:a1 (resolved it)")
    assert "Locale.ROOT" in out and "+        return req.remoteClientIp();" in out
    assert "--- resolving diff" not in out


def test_errors_summary(kb, ws, profile):
    _seed(kb, ws, profile)
    r = _run(kb.root, "errors", "summary")
    assert r.exit_code == 0
    assert "2 error case(s), 3 attempt(s), 2 project(s), 2 run(s)\n" in r.output and "text replaces" not in r.output
    assert "status:      resolved=1, unresolved=1" in r.output and "resolved by: llm=1" in r.output
    assert "regressed" in r.output and EC_L2 in r.output
    s = json.loads(_run(kb.root, "errors", "summary", "--run", "r2", "--json").output)
    assert s["by_status"] == {"unresolved": 1} and s["errors"] == 1
    r = runner.invoke(kb_cli, ["--kb-dir", str(kb.root), "errors", "replace-candidates"])
    assert r.exit_code != 0 and "No such command" in r.output


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


def test_stats_and_reindex_mention_error_cases(kb, ws, profile):
    _seed(kb, ws, profile)
    st = json.loads(_run(kb.root, "stats", "--json").output)
    assert st["error_cases"] == 2 and st["fix_attempts"] == 3
    r = _run(kb.root, "reindex")
    assert "case catalog: 1 case(s) (observed 1)" in r.output and "error hints" not in r.output
    assert not (kb.root / "error_hints.json").exists()


def test_errors_export_filters_by_case_and_resolver_with_the_resolution_chain(kb, ws, profile, tmp_path):
    """Round 10 (C9): a filtered `--attempts-csv` includes every attempt of the selected errors' resolution (an
    earlier `no_change` that stayed in the tree), and `--case` / `--resolver` select errors."""
    refs = [ErrorAttemptRef(attempt_id="r5:a1", fix_method="llm", outcome="improved")]
    errs, atts = prepare_error_cases(
        [ec_error("e5", run_id="r5", resolved_by="r5:a1", attempts=refs, resolution_attempts=["r5:a0", "r5:a1"])],
        [ec_attempt("a0", run_id="r5", outcome="no_change", diff=CODE_EDIT_DIFF, error_ids=()),
         ec_attempt("a1", run_id="r5", error_ids=("e5",))],
        profile, ws, kb.cfg)
    kb.append_error_cases(errs, atts)
    _seed(kb, ws, profile)
    out, att = tmp_path / "e.csv", tmp_path / "a.csv"
    r = _run(kb.root, "errors", "export", "--case", case_id_for(EC_L2)[:6], "--resolver", "^fake",
             "--status", "^resolved$", "--csv", str(out), "--attempts-csv", str(att))
    assert r.exit_code == 0 and "wrote 2 error case(s)" in r.output
    arows = {x["id"] for x in csv.DictReader(att.open(encoding="utf-8-sig", newline=""))}
    assert {"r5:a0", "r5:a1", "r1:a1"} <= arows              # r5:a0 is only in e5's resolution chain
    assert "wrote 0 error case(s)" in _run(kb.root, "errors", "export", "--case", "ffffff", "--csv",
                                           str(out)).output
    assert "wrote 0 error case(s)" in _run(kb.root, "errors", "export", "--resolver", "case-replace", "--csv",
                                           str(out)).output
    lines = _run(kb.root, "errors", "list", "--case", case_id_for(EC_L2)).output.strip().splitlines()
    assert len(lines) == 3
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig", newline="")))
    assert rows == [] and "schema_version" in out.read_text(encoding="utf-8-sig").splitlines()[0]
