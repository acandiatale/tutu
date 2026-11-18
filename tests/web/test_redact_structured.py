"""W5 (review 2026-11-18): the end-of-run redaction must not corrupt structured files.

* in `.json` / `.jsonl` a secret value is replaced inside string values only — never in keys, numbers or structure; a file
  (a jsonl line) that does not parse is handled as text exactly as before;
* a short secret value (under 6 characters: the IVAS user id is masked from 4 on) is replaced only as a whole token, in
  text files too, and by the write-time redaction of log / journal lines;
* nothing a log line had redacted before becomes visible."""
from __future__ import annotations

import json

from sbmig.core import scrub
from sbmig.web.redact import Redactor, redact_file, redact_run_dir, redact_text

SHORT = "2026"                                # an IVAS user id of four characters
LONG = "s3cr3t-token-value"


def test_a_short_id_inside_numbers_and_hashes_of_report_json_is_left_alone(tmp_path):
    rep = {"run_id": "p-20261118-101010-abcdef", "started_at": "2026-11-18T10:10:10Z", "timings_s": {"total": 20261.5},
           "llm_usage": {"calls": 2026, "input_tokens": 120260}, "hash": "ab2026cd",
           "manual_followups": ["login as 2026 failed", "user=2026", f"token {LONG} rejected"]}
    p = tmp_path / "report.json"
    p.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    assert redact_file(p, [SHORT, LONG])
    back = json.loads(p.read_text(encoding="utf-8"))                       # still valid JSON
    assert back["llm_usage"] == {"calls": 2026, "input_tokens": 120260} and back["timings_s"] == {"total": 20261.5}
    assert back["run_id"] == rep["run_id"] and back["started_at"] == rep["started_at"] and back["hash"] == "ab2026cd"
    assert back["manual_followups"] == ["login as *** failed", "user=***", "token *** rejected"]


def test_keys_are_never_rewritten_and_the_layout_is_kept(tmp_path):
    p = tmp_path / "state.json"
    text = '{\n "2026": {"user": "2026", "n": 2026},\n "s3cr3t-token-value": "s3cr3t-token-value"\n}\n'
    p.write_text(text, encoding="utf-8")
    assert redact_file(p, [SHORT, LONG])
    assert p.read_text(encoding="utf-8") == ('{\n "2026": {"user": "***", "n": 2026},\n'
                                             ' "s3cr3t-token-value": "***"\n}\n')


def test_a_secret_spelled_with_json_escapes_inside_a_string_is_still_masked(tmp_path):
    secret = 'pa"ss\\word-123'
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"msg": f"login {secret} ok"}), encoding="utf-8")
    assert redact_file(p, [secret])
    assert json.loads(p.read_text(encoding="utf-8")) == {"msg": "login *** ok"}


def test_jsonl_lines_are_redacted_value_wise_and_a_broken_line_as_text(tmp_path):
    p = tmp_path / "journal.jsonl"
    lines = [json.dumps({"ts": "2026-11-18T01:02:03", "event": "x", "n": 2026, "who": "2026"}),
             '{"event": "cut off', "token s3cr3t-token-value in a broken line 2026", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    assert redact_file(p, [SHORT, LONG])
    out = p.read_text(encoding="utf-8").split("\n")
    assert json.loads(out[0]) == {"ts": "2026-11-18T01:02:03", "event": "x", "n": 2026, "who": "***"}
    assert out[1] == '{"event": "cut off'
    assert out[2] == "token *** in a broken line ***"
    assert out[3] == ""


def test_a_json_file_that_does_not_parse_is_handled_as_text_as_before(tmp_path):
    p = tmp_path / "half.json"
    p.write_text('{"a": "s3cr3t-token-value", "b": 12', encoding="utf-8")
    assert redact_file(p, [LONG])
    assert p.read_text(encoding="utf-8") == '{"a": "***", "b": 12'


def test_short_values_are_whole_tokens_in_text_files_too(tmp_path):
    p = tmp_path / "sbmig.log"
    p.write_text("2026-11-18 run p-20261118 user 2026 logged in; id=2026, (2026) x2026 2026y\n", encoding="utf-8")
    assert redact_file(p, [SHORT])
    assert p.read_text(encoding="utf-8") == "2026-11-18 run p-20261118 user *** logged in; id=***, (***) x2026 2026y\n"


def test_long_values_are_still_replaced_anywhere():
    assert redact_text(f"x{LONG}y and {LONG}", [LONG]) == "x***y and ***"


def test_the_write_time_redactor_follows_the_same_rules():
    r = Redactor(lambda: [SHORT, LONG])
    assert r("2026-11-18 10:00 user 2026 logged in with s3cr3t-token-valueX") == \
        "2026-11-18 10:00 user *** logged in with ***X"
    # a journal line goes through scrub_json: numbers stay numbers, string values follow the token rule
    scrub.set_output_scrubber(r)
    try:
        clean = scrub.scrub_json({"ts": "2026-11-18T10:00", "n": 2026, "user": "2026", "msg": "id 2026"})
    finally:
        scrub.set_output_scrubber(None)
    assert clean == {"ts": "2026-11-18T10:00", "n": 2026, "user": "***", "msg": "id ***"}


def test_redact_run_dir_keeps_report_json_parseable(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "report.json").write_text(json.dumps({"llm_usage": {"calls": 2026}, "note": "user 2026"}), encoding="utf-8")
    (rd / "sbmig.log").write_text("2026-11-18 user 2026\n", encoding="utf-8")
    assert redact_run_dir(rd, [SHORT]) == 2
    assert json.loads((rd / "report.json").read_text(encoding="utf-8")) == {"llm_usage": {"calls": 2026}, "note": "user ***"}
    assert (rd / "sbmig.log").read_text(encoding="utf-8") == "2026-11-18 user ***\n"


def test_token_edges():
    assert redact_text("login as 2026.", [SHORT]) == "login as ***."
    assert redact_text("u-2026 user_2026 '2026' \"2026\" 2026:", [SHORT]) == "u-*** user_*** '***' \"***\" ***:"
    assert redact_text("10:20:26 v1.2026 2026-11 3.2026.1 12026", [SHORT]) == "10:20:26 v1.2026 2026-11 3.2026.1 12026"
