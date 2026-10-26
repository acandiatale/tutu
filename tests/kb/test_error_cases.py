"""sbmig.kb.error_cases: prepare (sanitize / classify / denormalize), run-dir files, ErrorCaseStore (append,
queries, summary, rule candidates, hints index) and the KB wiring (match hints, stats, reindex, zip round trip)."""
from __future__ import annotations

import csv
import json
import logging
import zipfile

from sbmig.core.models import (
    KB_RECORD_SCHEMA,
    BuildError,
    CaseType,
    ErrorAttemptRef,
    ErrorCluster,
    ErrorKind,
    FixAttemptRecord,
)
from sbmig.kb import ErrorCaseStore, FileKnowledgeBase, prepare_error_cases
from sbmig.kb.error_cases import (
    ERROR_CSV_COLUMNS,
    write_error_cases_csv,
    write_error_cases_jsonl,
    write_fix_attempts_csv,
)
from sbmig.kb.ledger import Ledger, render_learning_md

from .helpers import CODE_EDIT_DIFF, EC_FILE, EC_L2, ec_attempt, ec_error, with_kb


def _prepared(kb_cfg, ws, profile, errors, attempts):
    return prepare_error_cases(errors, attempts, profile, ws, kb_cfg)


def _cluster(l2: str = EC_L2, symbol: str = "method clientIp()") -> ErrorCluster:
    e = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=EC_FILE, symbol=symbol,
                   signature=f"compile|cannot find symbol|{symbol}", signature_generic=l2)
    return ErrorCluster(id="c1", kind=ErrorKind.COMPILE, file=EC_FILE, errors=[e], signatures=[e.signature])


def _run(kb_cfg, ws, profile, run_id: str, *, name: str, fp: str, status: str = "resolved",
         method: str = "llm", diff: str | None = None, failed: str = "", eid: str = "e1"):
    """One run's prepared records: an error (resolved by `method` or not) with an optional failed attempt first."""
    refs, atts = [], []
    if failed:
        atts.append(ec_attempt("a0", run_id=run_id, outcome=failed, error_ids=(f"{run_id}-{eid}",),
                               failure=f"regressed: new error in {ws.root}/src/X.java:12" if failed == "regressed"
                               else "the targeted error(s) were still there after the rebuild"))
        refs.append(ErrorAttemptRef(attempt_id=f"{run_id}:a0", fix_method="llm", outcome=failed))
    if status == "resolved":
        kw = {"diff": diff} if diff is not None else {}
        atts.append(ec_attempt("a1", run_id=run_id, fix_method=method, error_ids=(f"{run_id}-{eid}",), **kw))
        refs.append(ErrorAttemptRef(attempt_id=f"{run_id}:a1", fix_method=method, outcome="improved"))
    err = ec_error(f"{run_id}-{eid}", run_id=run_id, status=status, fix_method=method,
                   resolver_id="fake-model" if method == "llm" else "some-handler", resolved_by=f"{run_id}:a1",
                   attempts=refs, failure_reason="" if status == "resolved" else "llm attempt a0 no_change: still there")
    p = profile.model_copy(update={"name": name, "fingerprint": fp})
    return prepare_error_cases([err], atts, p, ws, kb_cfg)


# --------------------------------------------------------------------------- prepare
def test_prepare_sanitizes_paths_and_secrets(kb_cfg, ws, profile):
    root = str(ws.root)
    err = ec_error(file=f"{root}/{EC_FILE}", message=f"{root}/{EC_FILE}:[5,20] cannot find symbol",
                   raw=f"[ERROR] {root}/{EC_FILE} password=hunter2 " + "x" * 3000,
                   description=f"edited {root}/{EC_FILE}", problem_errors=[f"{root}/{EC_FILE}:5 boom"])
    diff = ec_attempt().diff.replace("        return req.clientIp();", "        return req.clientIp(); // token=s3cr3tvalue")
    diff = diff.replace("        return req.remoteClientIp();", "        return req.remoteClientIp(); // token=n3wsecret")
    att = ec_attempt(diff=diff, description=f"LLM fake-model: changed {root}/{EC_FILE}",
                     failure="", notes=[f"log at {ws.run_dir}/logs/mvn-1.log"])
    errors, attempts = _prepared(kb_cfg, ws, profile, [err], [att])
    e, a = errors[0], attempts[0]
    assert e.file == EC_FILE and root not in e.message and e.message.startswith(EC_FILE)
    assert root not in e.description and root not in e.problem_errors[0] and root not in e.raw
    assert "hunter2" not in e.raw and "password=***" in e.raw
    assert len(e.raw) <= 2000 + len("…[truncated]") and e.raw.endswith("…[truncated]")
    assert root not in a.description and a.notes[0] == "log at logs/mvn-1.log"
    assert "s3cr3tvalue" not in a.diff and "token=***" in a.diff
    # the classification ran on the unredacted diff (the method rename is still seen) but no secret leaks
    assert a.change.change_kind == "text_replace"
    pairs = {(p.old, p.new) for p in a.change.replace_pairs}
    assert ("clientIp", "remoteClientIp") in pairs
    assert not any("s3cr3t" in p.old or "n3w" in p.new for p in a.change.replace_pairs)
    assert "s3cr3tvalue" not in e.diff_excerpt
    # round 10: the masking is visible, and a resolution whose pair touched a secret is never a rule candidate
    assert any(n.startswith("redacted: 2 secret value(s)") for n in a.notes)
    assert any("replace pair(s) dropped" in n for n in a.notes)
    assert e.replace_rule_candidate is False and any(n.startswith("redacted:") for n in e.notes)


def test_prepare_fills_project_fields_symbol_and_counts(kb_cfg, ws, profile):
    refs = [ErrorAttemptRef(attempt_id="r1:a0", fix_method="llm", outcome="no_change"),
            ErrorAttemptRef(attempt_id="r1:a1", fix_method="llm", outcome="improved")]
    errors, attempts = _prepared(kb_cfg, ws, profile, [ec_error(attempts=refs)],
                                 [ec_attempt("a0", outcome="no_change", diff=CODE_EDIT_DIFF,
                                             failure="the targeted error(s) were still there after the rebuild"),
                                  ec_attempt("a1")])
    e = errors[0]
    assert (e.project, e.project_fingerprint, e.case_type) == ("demo", "fp-demo", CaseType.SPRING_BOOT)
    assert e.error_symbol == "clientIp" and e.attempt_count == 2 and e.llm_attempts == 2
    assert [r.change_kind for r in e.attempts] == ["code_edit", "text_replace"]
    assert e.attempts[0].failure.startswith("the targeted error") and e.attempts[1].resolver_id == "fake-model"
    assert all(a.project == "demo" and a.case_type == CaseType.SPRING_BOOT for a in attempts)


def test_prepare_denormalizes_the_resolving_attempt(kb_cfg, ws, profile):
    two_files = ec_attempt().diff + (
        "diff --git a/pom.xml b/pom.xml\n--- a/pom.xml\n+++ b/pom.xml\n@@ -1,1 +1,1 @@\n"
        "-<version>1.0.0</version>\n+<version>3.0.0</version>\n")
    pom_err = ec_error("e2", file=None, error_kind=ErrorKind.DEPENDENCY_RESOLUTION, symbol=None,
                       artifact="com.x:lib:1.0.0", l2="dependency_resolution|com.x:lib")
    errors, attempts = _prepared(kb_cfg, ws, profile, [ec_error(), pom_err], [ec_attempt(diff=two_files,
                                                                                          error_ids=("e1", "e2"))])
    e, p = errors
    assert attempts[0].change.change_kind == "text_replace"
    assert e.change_kind == "text_replace" and e.replace_rule_candidate is True
    assert [(x.old, x.new) for x in e.replace_pairs] == [("clientIp", "remoteClientIp")]   # own file only
    assert e.fix_labels and "method-rename" in e.fix_labels
    assert e.diff_excerpt.startswith(f"diff --git a/{EC_FILE}") and "pom.xml" not in e.diff_excerpt
    # an error without a file gets the head of the resolving diff and all of its pairs
    assert p.error_symbol == "com.x:lib" and "pom.xml" in p.diff_excerpt
    assert {(x.old, x.new) for x in p.replace_pairs} == {("clientIp", "remoteClientIp"), ("1.0.0", "3.0.0")}


def test_replace_rule_candidate_only_for_llm_text_replace_resolutions(kb_cfg, ws, profile):
    cases = [
        (ec_error("h"), ec_attempt(fix_method="handler", error_ids=("h",)), "handler"),
        (ec_error("c"), ec_attempt(diff=CODE_EDIT_DIFF, error_ids=("c",)), "llm"),
        (ec_error("i", status="resolved_indirectly", targeted=False), ec_attempt(error_ids=("i",)), "llm"),
        (ec_error("u", status="unresolved"), ec_attempt(outcome="no_change", error_ids=("u",)), "llm"),
    ]
    for err, att, method in cases:
        err = err.model_copy(update={"fix_method": method} if err.status.startswith("resolved") else {})
        errors, _ = _prepared(kb_cfg, ws, profile, [err], [att])
        assert errors[0].replace_rule_candidate is False, err.id
    i = _prepared(kb_cfg, ws, profile, [cases[2][0]], [cases[2][1]])[0][0]
    assert i.change_kind == "text_replace" and i.diff_excerpt          # still denormalized, just not a candidate


IMPORT_DIFF = (f"diff --git a/{EC_FILE} b/{EC_FILE}\n--- a/{EC_FILE}\n+++ b/{EC_FILE}\n@@ -1,2 +1,3 @@\n"
               " package com.example.web;\n+import com.example.legacy.RequestSupport;\n \n")


def test_resolution_of_several_attempts_is_recorded_whole(kb_cfg, ws, profile, tmp_path):
    # 2026-10-25: a0 (no_change, kept in the tree) added an import, a1 renamed the call and resolved the error
    refs = [ErrorAttemptRef(attempt_id="r1:a0", fix_method="llm", outcome="no_change"),
            ErrorAttemptRef(attempt_id="r1:a1", fix_method="llm", outcome="improved")]
    err = ec_error(attempts=refs, resolution_attempts=["r1:a0", "r1:a1"])
    errors, _ = _prepared(kb_cfg, ws, profile, [err], [ec_attempt("a0", outcome="no_change", diff=IMPORT_DIFF),
                                                        ec_attempt("a1")])
    e = errors[0]
    assert e.resolution_attempts == ["r1:a0", "r1:a1"] and e.resolved_by == "r1:a1"
    assert e.diff_excerpt.startswith("# attempt r1:a0: llm fake-model → no_change\ndiff --git")
    assert "+import com.example.legacy.RequestSupport;" in e.diff_excerpt
    assert "\n# attempt r1:a1: llm fake-model → improved\ndiff --git" in e.diff_excerpt
    assert "+        return req.remoteClientIp();" in e.diff_excerpt
    # read as one change: an inserted import + a token replace = mixed, so no longer a text-replace rule candidate
    assert e.change_kind == "mixed" and e.replace_rule_candidate is False
    assert [(p.old, p.new) for p in e.replace_pairs] == [("clientIp", "remoteClientIp")]
    rows = list(csv.reader(open(write_error_cases_csv(errors, tmp_path / "e.csv"), encoding="utf-8-sig")))
    row = dict(zip(rows[0], rows[1]))
    assert row["resolution_attempts"] == "a0; a1" and "+import com.example.legacy.RequestSupport;" in row["diff_excerpt"]
    # one attempt: as before (no section header), the list holds the resolver; an unknown id falls back to it
    single = _prepared(kb_cfg, ws, profile, [ec_error()], [ec_attempt()])[0][0]
    assert single.resolution_attempts == ["r1:a1"] and single.diff_excerpt.startswith("diff --git")
    assert single.change_kind == "text_replace" and single.replace_rule_candidate is True
    lost = _prepared(kb_cfg, ws, profile, [ec_error(resolution_attempts=["r1:gone", "r1:a1"])], [ec_attempt()])[0][0]
    assert lost.resolution_attempts == ["r1:a1"] and lost.diff_excerpt.startswith("diff --git")


def test_prepare_caps_diff_and_excerpt(kb_cfg, ws, profile):
    cfg = with_kb(kb_cfg, error_diff_chars=200, error_excerpt_chars=80)
    errors, attempts = prepare_error_cases([ec_error()], [ec_attempt()], profile, ws, cfg)
    a, e = attempts[0], errors[0]
    assert a.diff_truncated and len(a.diff) <= 200 + 20 and a.diff.endswith("…[truncated]")
    assert a.change.change_kind == "text_replace"          # classified before the cap
    assert len(e.diff_excerpt) <= 80 + 20 and e.diff_excerpt.endswith("…[truncated]")
    big = with_kb(kb_cfg, error_diff_chars=0)              # 0 = no cap
    assert not prepare_error_cases([], [ec_attempt()], profile, ws, big)[1][0].diff_truncated


def test_prepare_never_raises(kb_cfg, ws, profile):
    assert prepare_error_cases([], [], None, None, None) == ([], [])
    errors, attempts = prepare_error_cases([ec_error()], [ec_attempt()], None, None, None)
    assert errors[0].project == "" and errors[0].change_kind == "text_replace"
    weird = ec_attempt(diff="@@ garbage\n-+-", error_ids=("missing",))
    assert prepare_error_cases([ec_error(resolved_by="nope")], [weird], profile, ws, kb_cfg)[0][0].change_kind == \
        "none"


# --------------------------------------------------------------------------- run-dir files
def test_jsonl_and_csv_files(kb_cfg, ws, profile, tmp_path):
    msg = "심볼을 찾을 수 없습니다\nsecond line"
    errors, attempts = _prepared(kb_cfg, ws, profile, [ec_error(message=msg), ec_error("e2", status="unresolved",
                                                                                         message="-source 8")],
                                 [ec_attempt()])
    p = write_error_cases_jsonl(errors, tmp_path / "run" / "error_cases.jsonl")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["message"] == msg
    write_error_cases_jsonl(attempts, tmp_path / "run" / "fix_attempts.jsonl")
    assert FixAttemptRecord.model_validate_json((tmp_path / "run" / "fix_attempts.jsonl").read_text()).id == "r1:a1"

    c = write_error_cases_csv(errors, tmp_path / "run" / "error_cases.csv")
    raw = c.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                                   # BOM: Excel reads UTF-8
    rows = list(csv.reader(c.open(encoding="utf-8-sig", newline="")))
    assert rows[0] == ERROR_CSV_COLUMNS and len(rows) == 3
    row = dict(zip(rows[0], rows[1]))
    assert row["message"] == msg and row["replace_pairs"] == "clientIp→remoteClientIp"
    assert row["replace_rule_candidate"] == "true" and row["attempts"] == "a1:llm:improved"
    assert row["symbol"] == "clientIp" and row["status"] == "resolved"
    assert dict(zip(rows[0], rows[2]))["message"] == "-source 8"     # round 10: a plain sign is data, not a formula
    a = write_fix_attempts_csv(attempts, tmp_path / "run" / "fix_attempts.csv")
    arows = list(csv.reader(a.open(encoding="utf-8-sig", newline="")))
    assert len(arows) == 2 and "remoteClientIp" in dict(zip(arows[0], arows[1]))["diff"]


# --------------------------------------------------------------------------- store
def test_append_dedupes_and_filters(kb_cfg, ws, profile, tmp_path):
    store = ErrorCaseStore(tmp_path / "kb")
    e1, a1 = _run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change")
    e2, a2 = _run(kb_cfg, ws, profile, "r2", name="beta", fp="fp-b", status="unresolved", failed="regressed")
    assert store.append(e1, a1) == 3
    assert store.append(e1, a1) == 0                                          # ids already stored
    assert store.append_counts(e2 + e2, a2 + a2) == (1, 1)                    # duplicates inside one batch
    assert store.count_errors() == 2 and store.count_attempts() == 3
    assert [e.id for e in store.iter_errors(status="^resolved$")] == ["r1-e1"]
    assert [e.id for e in store.iter_errors(status="unres")] == ["r2-e1"]
    assert [e.id for e in store.iter_errors(method="llm")] == ["r1-e1"]
    assert [e.id for e in store.iter_errors(change_kind="text_replace")] == ["r1-e1"]
    assert [e.id for e in store.iter_errors(project="bet")] == ["r2-e1"]
    assert [e.id for e in store.iter_errors(project="fp-a")] == ["r1-e1"]
    assert len(list(store.iter_errors(symbol="^clientIp$"))) == 2
    assert len(list(store.iter_errors(signature="cannot_find_symbol"))) == 2
    assert [e.id for e in store.iter_errors(run_id="r2")] == ["r2-e1"]
    assert [a.id for a in store.iter_attempts(outcome="regressed")] == ["r2:a0"]
    assert store.get_error("r1-e1").project == "alpha" and store.get_error("zzz") is None
    assert [a.id for a in store.attempts_of(store.get_error("r1-e1"))] == ["r1:a0", "r1:a1"]


def test_unreadable_lines_are_skipped(tmp_path, kb_cfg, ws, profile, caplog):
    store = ErrorCaseStore(tmp_path / "kb")
    e1, a1 = _run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a")
    store.append(e1, a1)
    with open(store.errors_path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert [e.id for e in ErrorCaseStore(store.root).iter_errors()] == ["r1-e1"]
    assert "unreadable line" in caplog.text


def test_summary_and_replace_candidates(kb_cfg, ws, profile, tmp_path):
    store = ErrorCaseStore(tmp_path / "kb")
    for run, name in (("r1", "alpha"), ("r2", "beta"), ("r3", "gamma")):
        store.append(*_run(kb_cfg, ws, profile, run, name=name, fp=f"fp-{name}", failed="no_change" if run == "r1"
                           else ""))
    store.append(*_run(kb_cfg, ws, profile, "r4", name="delta", fp="fp-delta", method="handler"))
    store.append(*_run(kb_cfg, ws, profile, "r5", name="eps", fp="fp-eps", status="unresolved", failed="regressed"))
    s = store.summary()
    assert s["errors"] == 5 and s["projects"] == 5 and s["runs"] == 5
    assert s["by_status"] == {"resolved": 4, "unresolved": 1}
    assert s["by_method"] == {"llm": 3, "handler": 1} and s["by_change_kind"] == {"text_replace": 4}
    assert s["status_method"] == {"resolved": {"llm": 3, "handler": 1}, "unresolved": {"llm": 1}}
    assert s["attempt_outcomes"]["llm"] == {"improved": 3, "no_change": 1, "regressed": 1}
    assert s["llm_text_replace"] == 3 and s["attempts"] == 6
    assert s["top_unresolved"][0]["signature_generic"] == EC_L2 and s["top_unresolved"][0]["projects"] == 1
    assert s["top_failure_reasons"][0]["reason"].startswith("llm attempt a0")
    assert store.summary(run_ids=["r5"])["by_status"] == {"unresolved": 1}

    cands = store.replace_candidates()
    assert len(cands) == 1
    c = cands[0]
    assert (c["signature_generic"], c["old"], c["new"], c["count"], c["projects"]) == (
        EC_L2, "clientIp", "remoteClientIp", 3, 3)
    assert c["example_file"] == EC_FILE and c["example_attempt"] == "r1:a1" and c["project_names"] == [
        "alpha", "beta", "gamma"]
    assert store.replace_candidates(min_projects=4) == []
    assert s["replace_candidates"] == cands


def test_hints_index_incremental_equals_rebuild(kb_cfg, ws, profile, tmp_path):
    store = ErrorCaseStore(tmp_path / "kb")
    runs = [("r1", "alpha", "resolved", "llm", "no_change"), ("r2", "beta", "unresolved", "llm", "regressed"),
            ("r3", "gamma", "resolved", "handler", ""), ("r4", "alpha", "resolved", "llm", "no_change"),
            ("r5", "delta", "blocked", "llm", "")]
    for run, name, status, method, failed in runs:
        store.append(*_run(kb_cfg, ws, profile, run, name=name, fp=f"fp-{name}", status=status, method=method,
                           failed=failed))
    incremental = json.loads(store.hints_path.read_text(encoding="utf-8"))
    assert store.rebuild_hints() == 2                                        # one L2 + one symbol
    rebuilt = json.loads(store.hints_path.read_text(encoding="utf-8"))
    for k in ("by_generic", "by_symbol"):
        assert incremental[k] == rebuilt[k]
    ent = rebuilt["by_generic"][EC_L2]
    assert ent["seen"] == 5 and ent["projects"] == ["fp-alpha", "fp-beta", "fp-gamma", "fp-delta"]
    assert ent["resolved"] == {"llm": 2, "handler": 1} and ent["unresolved"] == 1 and ent["blocked"] == 1
    assert ent["fixes"][0]["method"] == "llm" and ent["fixes"][0]["count"] == 2 and ent["fixes"][0]["projects"] == 1
    assert ent["fixes"][0]["replace_pairs"] == [{"old": "clientIp", "new": "remoteClientIp"}]
    assert ent["fixes"][1]["method"] == "handler"
    assert ent["failures"][0] == {"method": "llm", "outcome": "no_change", "count": 2,
                                  "failure": "the targeted error(s) were still there after the rebuild"}
    assert ent["failures"][1]["outcome"] == "regressed" and str(ws.root) not in ent["failures"][1]["failure"]
    # a missing index over existing records is rebuilt by the next append
    store.hints_path.unlink()
    store.append(*_run(kb_cfg, ws, profile, "r6", name="eps", fp="fp-eps"))
    assert json.loads(store.hints_path.read_text())["by_generic"][EC_L2]["seen"] == 6


def test_hints_for_lines_and_reload_on_change(kb_cfg, ws, profile, tmp_path):
    store = ErrorCaseStore(tmp_path / "kb")
    assert store.hints_for(EC_L2, "clientIp") == []
    store.append(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change"))
    lines = store.hints_for(EC_L2, "clientIp")
    assert lines == [
        f"[error {EC_L2}] past runs: fixed by llm fake-model in 1 project(s): renamed clientIp to remoteClientIp; "
        "replace `clientIp` → `remoteClientIp`",
        f"[error {EC_L2}] past failure ×1: llm no_change — the targeted error(s) were still there after the "
        "rebuild; do not repeat it",
    ]                                                   # the symbol entry says the same: not repeated
    assert store.hints_for(None, "clientIp") == [x.replace(f"[error {EC_L2}]", "[error clientIp]") for x in lines]
    assert store.hints_for("compile|other|x", None) == []
    # another process / instance appends: the first instance reloads the index because the file changed
    other = ErrorCaseStore(store.root)
    other.append(*_run(kb_cfg, ws, profile, "r2", name="beta", fp="fp-b"))
    assert "in 2 project(s)" in store.hints_for(EC_L2)[0]


# --------------------------------------------------------------------------- KB wiring
def test_kb_append_match_hints_and_caps(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(kb_cfg)
    errs, atts = _run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change")
    assert kb.append_error_cases(errs, atts) == 3 and kb.append_error_cases(errs, atts) == 0
    hints = kb.match(_cluster(), ws, profile).hints
    assert len(hints) == 2 and "past runs: fixed by llm" in hints[0] and "do not repeat it" in hints[1]
    assert kb.match(_cluster(l2="compile|x|y", symbol="method other()"), ws, profile).hints == []
    off = FileKnowledgeBase(with_kb(kb_cfg, error_hints=False))
    assert off.match(_cluster(), ws, profile).hints == []
    assert FileKnowledgeBase(with_kb(kb_cfg, use_hints=False)).match(_cluster(), ws, profile).hints == []
    one = FileKnowledgeBase(with_kb(kb_cfg, max_hints=1))
    assert one.match(_cluster(), ws, profile).hints == hints[:1]
    # existing hints (patterns, cases, symbols.json) stay first; max_hints caps the whole list
    kb.ensure_layout()
    (kb.patterns_dir / "p.yml").write_text("match: {signature_regex: 'cannot_find_symbol'}\nhints: [use X]\n")
    two = FileKnowledgeBase(with_kb(kb_cfg, max_hints=2))
    assert two.match(_cluster(), ws, profile).hints == ["[pattern p] use X", hints[0]]
    assert kb.append_error_cases(None, None) == 0


def test_kb_append_never_raises(kb_cfg, monkeypatch):
    kb = FileKnowledgeBase(kb_cfg)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ErrorCaseStore, "append", boom)
    assert kb.append_error_cases([ec_error()], []) == 0


def test_stats_reindex_and_learning_section(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(kb_cfg)
    assert kb.stats()["error_cases"] == 0 and kb.stats()["fix_attempts"] == 0
    assert "## Error cases" not in render_learning_md(kb.ledger().summary())
    kb.append_error_cases(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change"))
    kb.append_error_cases(*_run(kb_cfg, ws, profile, "r2", name="beta", fp="fp-b", status="unresolved",
                                failed="regressed"))
    st = kb.stats()
    assert st["error_cases"] == 2 and st["fix_attempts"] == 3
    store = kb.error_case_store()
    store.hints_path.unlink()
    kb.reindex()
    assert json.loads(store.hints_path.read_text())["by_generic"][EC_L2]["seen"] == 2

    summary = kb.ledger().summary()
    assert summary["error_cases"]["errors"] == 2
    md = render_learning_md(summary, kb.stats())
    assert "## Error cases" in md and "| resolved | 1 |" in md and "| unresolved | 1 |" in md
    assert "LLM fixes that were only text replaces — candidates for a deterministic rule" in md
    assert "`clientIp` → `remoteClientIp`" in md and "### Top failure reasons" in md
    only_r2 = Ledger(kb.ledger_path).summary(run_ids=["r2"])
    assert only_r2["error_cases"]["by_status"] == {"unresolved": 1}
    assert "## Error cases" not in render_learning_md(Ledger(kb.ledger_path).summary(run_ids=["nope"]))
    # an explicit error_cases argument wins over the summary's own
    assert "## Error cases" not in render_learning_md(summary, error_cases={})


def test_zip_round_trip_includes_error_cases(kb_cfg, ws, profile, tmp_path):
    kb = FileKnowledgeBase(kb_cfg)
    kb.append_error_cases(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change"))
    kb.append_error_cases(*_run(kb_cfg, ws, profile, "r2", name="beta", fp="fp-b"))
    out = kb.export_zip(tmp_path / "kb.zip")
    names = zipfile.ZipFile(out).namelist()
    assert {"errors.jsonl", "fix_attempts.jsonl", "error_hints.json"} <= set(names)
    src = kb.error_case_store()
    want_e = [e.id for e in src.iter_errors()]
    want_a = [a.id for a in src.iter_attempts()]

    empty = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb-empty")))
    counters = empty.import_zip(out)
    assert counters["error_cases_added"] == 2 and counters["fix_attempts_added"] == 3
    dst = empty.error_case_store()
    assert [e.id for e in dst.iter_errors()] == want_e and [a.id for a in dst.iter_attempts()] == want_a
    assert json.loads(dst.hints_path.read_text())["by_generic"][EC_L2]["seen"] == 2

    partial = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb-partial")))
    partial.append_error_cases(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change"))
    partial.append_error_cases(*_run(kb_cfg, ws, profile, "r9", name="omega", fp="fp-o"))
    counters = partial.import_zip(out)
    assert counters["error_cases_added"] == 1 and counters["fix_attempts_added"] == 1
    ids = [e.id for e in partial.error_case_store().iter_errors()]
    assert sorted(ids) == ["r1-e1", "r2-e1", "r9-e1"] and partial.error_case_store().count_errors() == 3
    assert partial.import_zip(out)["error_cases_added"] == 0
    assert json.loads(partial.error_case_store().hints_path.read_text())["by_generic"][EC_L2]["seen"] == 3

    # --replace: the zip's records replace the local ones
    partial.import_zip(out, merge=False)
    assert sorted(e.id for e in partial.error_case_store().iter_errors()) == ["r1-e1", "r2-e1"]

    # an old zip without the error-case files still imports and leaves the local records alone
    old = tmp_path / "old.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(old, "w") as zout:
        for n in zin.namelist():
            if n not in ("errors.jsonl", "fix_attempts.jsonl", "error_hints.json"):
                zout.writestr(n, zin.read(n))
    counters = partial.import_zip(old)
    assert counters["error_cases_added"] == 0 and partial.error_case_store().count_errors() == 2


# --------------------------------------------------------------------------- round 10 (2026-10-26)
def test_error_csv_column_order_snapshot():
    """The business team codes rules against these columns: the order is a contract (append, never reorder)."""
    assert ERROR_CSV_COLUMNS == [
        "id", "project", "run_id", "status", "error_kind", "file", "line", "message", "symbol", "signature_generic",
        "case_id", "first_seen", "resolved_at", "fix_method", "resolver_id", "change_kind", "replace_pairs",
        "replace_rule_candidate", "description", "failure_reason", "problem_errors", "block_reason", "suggestion",
        "attempt_count", "llm_attempts", "attempts", "resolution_attempts",
        "signature", "location", "module", "case_type", "asis_java", "asis_spring_boot", "asis_spring", "packaging",
        "targeted", "resolved_by", "fix_labels", "at", "introduced_by", "notes", "schema_version",
        "script_case_id", "script_version", "raw",                     # v2 (flow-v2): before the bulky columns
        "diff_excerpt",
    ]


def test_csv_formula_guard_keeps_plain_signed_values():
    from sbmig.kb.error_cases import _cell, formula_guarded

    for s in ("=1+1", "@SUM(A1)", "+SUM(A1)", "-cmd|' /C calc'!A0", "-HYPERLINK(\"x\")", "\tx", "\rx", "-2+3=5"):
        assert formula_guarded(s) and _cell(s) == "'" + s
    for s in ("-source 8", "-Xmx512m", "-1", "+1", "+82 10", "- removed line", "", "plain"):
        assert not formula_guarded(s) and _cell(s) == s


def test_prepare_fills_versions_and_project_context(kb_cfg, ws, profile, tmp_path):
    import sbmig

    p = profile.model_copy(update={"java_version": "1.8", "spring_boot_version": "2.7.18",
                                   "spring_version": "5.3.31", "packaging": "war"})
    errors, attempts = _prepared(kb_cfg, ws, p, [ec_error(introduced_by="step:spring.rewrite")], [ec_attempt()])
    e, a = errors[0], attempts[0]
    assert (e.asis_java, e.asis_spring_boot, e.asis_spring, e.packaging) == ("1.8", "2.7.18", "5.3.31", "war")
    assert e.schema_version == a.schema_version == KB_RECORD_SCHEMA and e.agent_version == a.agent_version == \
        sbmig.__version__
    assert e.introduced_by == "step:spring.rewrite"
    path = write_error_cases_csv(errors, tmp_path / "e.csv")
    rows = list(csv.reader(path.open(encoding="utf-8-sig", newline="")))
    row = dict(zip(rows[0], rows[1]))
    assert (row["asis_java"], row["packaging"], row["schema_version"], row["resolved_by"]) == (
        "1.8", "war", str(KB_RECORD_SCHEMA), "a1")
    assert row["signature"] == e.signature and row["targeted"] == "true" and row["introduced_by"] == "step:spring.rewrite"
    assert row["fix_labels"] and row["at"] and row["module"] == "" and row["case_type"] == "spring_boot"


def test_full_diff_keeps_a_later_files_code_edit(kb_cfg, ws, profile):
    """review-kb/repro3: the tracker used to cap the diff at 12,000 chars BEFORE the classification — B.java's code
    edit was cut off (a `text_replace` rule candidate) and B.java's own excerpt was empty."""
    hunk_a = "".join(f"-    String s{i} = helper.clientIp();\n+    String s{i} = helper.remoteClientIp();\n"
                     for i in range(150))
    diff = ("diff --git a/src/main/java/a/A.java b/src/main/java/a/A.java\n--- a/src/main/java/a/A.java\n"
            f"+++ b/src/main/java/a/A.java\n@@ -1,150 +1,150 @@\n{hunk_a}"
            "diff --git a/src/main/java/a/B.java b/src/main/java/a/B.java\n--- a/src/main/java/a/B.java\n"
            "+++ b/src/main/java/a/B.java\n@@ -10,2 +10,4 @@\n"
            "-    return helper.clientIp();\n+    String ip = helper.remoteClientIp();\n"
            "+    if (ip == null) ip = \"0.0.0.0\";\n+    return ip;\n")
    assert len(diff) > 12000
    err = ec_error(file="src/main/java/a/B.java", line=10)
    errors, attempts = _prepared(kb_cfg, ws, profile, [err], [ec_attempt(diff=diff)])
    e, a = errors[0], attempts[0]
    assert a.change.change_kind == "mixed" and a.diff_truncated               # capped for storage, after
    assert e.change_kind == "mixed" and e.replace_rule_candidate is False
    assert "B.java" in e.diff_excerpt and "if (ip == null)" in e.diff_excerpt and "A.java" not in e.diff_excerpt


def test_an_attempt_that_fixed_this_error_is_not_its_failure(kb_cfg, ws, profile, tmp_path):
    """review-kb/repro4: one attempt aimed at two errors is `no_change` for the cluster (the other one stayed) —
    for the error it fixed it is the fix, never "past failure … do not repeat it"."""
    refs = [ErrorAttemptRef(attempt_id="r1:a1", fix_method="llm", outcome="no_change"),
            ErrorAttemptRef(attempt_id="r1:a2", fix_method="llm", outcome="partial"),
            ErrorAttemptRef(attempt_id="r1:a3", fix_method="llm", outcome="inconclusive")]
    err = ec_error(attempts=refs, resolved_by="r1:a1", resolution_attempts=["r1:a1"])
    errors, attempts = _prepared(kb_cfg, ws, profile, [err], [ec_attempt("a1", outcome="no_change")])
    store = ErrorCaseStore(tmp_path / "kb")
    store.append(errors, attempts)
    lines = store.hints_for(EC_L2)
    assert lines and "fixed by llm" in lines[0] and not any("past failure" in x for x in lines)


def test_hints_rank_fixes_by_projects_not_records(kb_cfg, ws, profile, tmp_path):
    big = "diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1,1 +1,1 @@\n-x.clientIp();\n+x.getRemoteAddr();\n"
    store = ErrorCaseStore(tmp_path / "kb")
    for i in range(10):                                      # one project, 10 errors
        store.append(*_run(kb_cfg, ws, profile, f"big{i}", name="big", fp="fp-big",
                           diff=big.format(f=EC_FILE), eid=f"e{i}"))
    for n in ("b", "c", "d"):                                # three projects, one error each
        store.append(*_run(kb_cfg, ws, profile, f"r-{n}", name=n, fp=f"fp-{n}"))
    line = store.hints_for(EC_L2)[0]
    assert "in 3 project(s)" in line and "`clientIp` → `remoteClientIp`" in line


def test_replace_candidates_never_list_masked_pairs(kb_cfg, ws, profile, tmp_path):
    from sbmig.core.models import ReplacePair

    store = ErrorCaseStore(tmp_path / "kb")
    store.append(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a"))
    legacy = ec_error("old", run_id="r0", replace_rule_candidate=True, change_kind="text_replace",
                      replace_pairs=[ReplacePair(old="***", new="***"), ReplacePair(old="token=***", new="x")])
    store.append([legacy], [])
    rows = store.replace_candidates()
    assert [(r["old"], r["new"]) for r in rows] == [("clientIp", "remoteClientIp")]
    assert all("***" not in r["old"] + r["new"] for r in store.summary()["replace_candidates"])


def test_summary_streams_and_counts_like_before(kb_cfg, ws, profile, tmp_path):
    store = ErrorCaseStore(tmp_path / "kb")
    store.append(*_run(kb_cfg, ws, profile, "r1", name="alpha", fp="fp-a", failed="no_change"))
    store.append(*_run(kb_cfg, ws, profile, "r2", name="beta", fp="fp-b", status="unresolved", failed="regressed"))
    s = store.summary()
    assert (s["errors"], s["projects"], s["runs"]) == (2, 2, 2)
    assert s["by_status"] == {"resolved": 1, "unresolved": 1} and s["attempts"] == 3
    assert s["attempt_outcomes"]["llm"] == {"no_change": 1, "improved": 1, "regressed": 1}
    assert s["top_unresolved"][0]["projects"] == 1 and s["llm_text_replace"] == 1
