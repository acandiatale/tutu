from __future__ import annotations

import json
import urllib.parse

import pytest

from sbmig.core import scrub
from sbmig.web import redact
from sbmig.web.redact import (
    Redactor,
    expand_values,
    install_output_redaction,
    redact_file,
    redact_run_dir,
    redact_text,
)


def test_longest_value_first():
    # "abcdef" is a prefix of the longer value: replacing it first would leave "123456" behind
    assert redact_text("x=abcdef123456 y=abcdef", ["abcdef", "abcdef123456"]) == "x=*** y=***"


def test_url_userinfo_masked_any_scheme():
    t = "clone https://user:pa55@git.example/r.git and ssh://bob@host/x and jdbc:mysql://u:p@db:3306/a"
    got = redact_text(t, [])
    assert "https://***@git.example/r.git" in got
    assert "ssh://***@host/x" in got
    assert "mysql://***@db:3306/a" in got
    assert "pa55" not in got


def test_plain_urls_untouched():
    t = "see https://example.org/a@b and mailto x@y.z"
    assert redact_text(t, []) == t


def test_redact_file_only_rewrites_on_change(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("nothing secret\n", encoding="utf-8")
    assert redact_file(p, ["abcdef123456"]) is False
    p.write_text("token abcdef123456\n", encoding="utf-8")
    assert redact_file(p, ["abcdef123456"]) is True
    assert p.read_text(encoding="utf-8") == "token ***\n"


def test_redact_file_missing_does_not_raise(tmp_path):
    assert redact_file(tmp_path / "nope.log", ["abcdef123456"]) is False


def test_non_utf8_bytes_do_not_crash(tmp_path):
    p = tmp_path / "b.log"
    p.write_bytes(b"\xff\xfe cp949 \xb0\xa1 abcdef123456\n")
    assert redact_file(p, ["abcdef123456"]) is True
    assert b"abcdef123456" not in p.read_bytes()


def test_run_dir_scope_and_count(tmp_path):
    rd = tmp_path / "run"
    (rd / "logs" / "deep").mkdir(parents=True)
    (rd / "control").mkdir()
    (rd / "project").mkdir()
    secret = "abcdef123456"
    (rd / "sbmig.log").write_text(f"k={secret}\n", encoding="utf-8")
    (rd / "report.md").write_text(f"# r {secret}\n", encoding="utf-8")
    (rd / "report.json").write_text('{"ok": true}\n', encoding="utf-8")        # unchanged → not counted
    (rd / "logs" / "deep" / "mvn.out").write_text(f"{secret}\n", encoding="utf-8")
    (rd / "control" / "request.json").write_text(f'{{"x": "{secret}"}}', encoding="utf-8")
    (rd / "project" / "app.properties").write_text(f"p={secret}\n", encoding="utf-8")
    (rd / "source.zip").write_bytes(b"PK" + secret.encode())
    (rd / "result.zip").write_bytes(b"PK" + secret.encode())

    assert redact_run_dir(rd, [secret]) == 3
    assert secret not in (rd / "sbmig.log").read_text(encoding="utf-8")
    assert secret not in (rd / "logs" / "deep" / "mvn.out").read_text(encoding="utf-8")
    assert secret in (rd / "control" / "request.json").read_text(encoding="utf-8")
    assert secret in (rd / "project" / "app.properties").read_text(encoding="utf-8")
    assert (rd / "source.zip").read_bytes() == b"PK" + secret.encode()
    assert (rd / "result.zip").read_bytes() == b"PK" + secret.encode()


def test_empty_values_returns_zero(tmp_path):
    (tmp_path / "a.log").write_text("https://u:p@h/x\n", encoding="utf-8")
    assert redact_run_dir(tmp_path, []) == 0


def test_values_none_reads_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("SBMIG_TEST_API_KEY", "zzzz-secret-zzzz")
    (tmp_path / "a.log").write_text("v=zzzz-secret-zzzz\n", encoding="utf-8")
    assert redact_run_dir(tmp_path) == 1
    assert "zzzz-secret-zzzz" not in (tmp_path / "a.log").read_text(encoding="utf-8")


# ---- 2026-11-10 (review: redaction at output time): the spellings a secret takes in output, the per-line Redactor, the hook

def test_expand_values_adds_the_spellings_a_secret_takes_in_output():
    secret = 'p@ss "w/ord\\1'
    forms = expand_values([secret])
    assert secret in forms and json.dumps(secret)[1:-1] in forms and urllib.parse.quote(secret, safe="") in forms
    assert forms == sorted(forms, key=lambda v: (-len(v), v)) and len(set(forms)) == len(forms)
    assert expand_values(["", None]) == []                              # nothing to redact
    # the JSON line of a journal, the percent-encoded form of a URL: both go
    assert redact_text(json.dumps({"k": secret}), [secret]) == '{"k": "***"}'
    assert redact_text("GET /x?token=" + urllib.parse.quote(secret, safe=""), [secret]) == "GET /x?token=***"


def test_multi_line_secret_is_redacted_line_by_line():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n}\n-----END PRIVATE KEY-----\n"
    forms = expand_values([pem])
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" in forms and "}" not in forms      # a short line would wipe the whole log
    assert redact_text("line: MIIEvQIBADANBgkqhkiG9w0BAQEFAASC }", [pem]) == "line: *** }"


def test_unicode_secret_in_an_ascii_json_line():
    secret = "비밀번호-값-1234"
    assert redact_text(json.dumps({"k": secret}), [secret]) == '{"k": "***"}'          # \uXXXX escapes
    assert redact_text(json.dumps({"k": secret}, ensure_ascii=False), [secret]) == '{"k": "***"}'


def test_redactor_reads_its_source_once_without_an_interval():
    values = ["first-secret-1"]
    r = Redactor(lambda: values)
    assert r("a first-secret-1 b") == "a *** b"
    values.append("second-secret-2")
    assert r("second-secret-2") == "second-secret-2"                   # not re-read on its own
    r.refresh()
    assert r("second-secret-2 https://u:pw@h/x") == "*** https://***@h/x"
    assert "second-secret-2" in r.values


def test_redactor_line_keeps_untouched_bytes_and_withholds_on_failure():
    r = Redactor(lambda: ["first-secret-1"])
    raw = b"plain \xff\xfe bytes\n"                                    # not UTF-8: must pass through as it is
    assert r.line(raw) is raw
    assert r.line(b"key=first-secret-1\n") == b"key=***\n"

    state = {"ok": True}

    def source():
        if not state["ok"]:
            raise RuntimeError("cannot read the secrets")
        return ["first-secret-1"]
    live = Redactor(source, refresh_s=0.0)                              # re-reads on every call
    state["ok"] = False
    with pytest.raises(RuntimeError):
        live("key=first-secret-1")
    assert live.line(b"key=first-secret-1\n") == (scrub.WITHHELD + "\n").encode()     # never the raw line


def test_install_output_redaction_hooks_env_and_noted_secrets(monkeypatch):
    noted = set(scrub._noted)
    monkeypatch.setenv("SBMIG_T_API_KEY", "env-secret-123456")
    monkeypatch.setenv("SBMIG_LLM__MAX_OUTPUT_TOKENS", "32000")         # a config override, not a secret
    try:
        r = install_output_redaction()
        assert scrub.output_scrubbing() and "env-secret-123456" in redact.live_secret_values()
        assert scrub.scrub("k=env-secret-123456 n=32000") == "k=*** n=32000"
        scrub.note_secret("exchanged-access-token")                    # told at once: no waiting for the interval
        assert "exchanged-access-token" in r.values and scrub.scrub("Bearer exchanged-access-token") == "Bearer ***"
        scrub.note_secret("abc")                                       # too short to redact safely
        assert "abc" not in redact.live_secret_values()
    finally:
        scrub.set_output_scrubber(None)
        scrub._noted.clear()
        scrub._noted.update(noted)


def test_redact_run_dir_knows_noted_secrets(tmp_path, monkeypatch):
    noted = set(scrub._noted)
    (tmp_path / "sbmig.log").write_text("Bearer exchanged-access-token\n", encoding="utf-8")
    try:
        scrub.note_secret("exchanged-access-token")
        assert redact_run_dir(tmp_path) == 1
        assert (tmp_path / "sbmig.log").read_text(encoding="utf-8") == "Bearer ***\n"
    finally:
        scrub._noted.clear()
        scrub._noted.update(noted)
