"""2026-11-10 (review: a secret must be redacted when a line is written, not after the run): `sbmig.core.scrub` is the
hook every `StageFormatter` line and every `Workspace.journal` line passes through. A no-op without a scrubber, fail
closed with one, and the same for the tracebacks of uncaught exceptions."""
from __future__ import annotations

import json
import logging
import sys
import threading

import pytest

from sbmig.core import scrub
from sbmig.core.stage import stage_formatter
from sbmig.core.workspace import Workspace

SECRET = "s3cr3t-value-123"


@pytest.fixture
def masked():
    """A plain scrubber for the test; removed afterwards together with whatever the test registered."""
    noted = set(scrub._noted)
    scrub.set_output_scrubber(lambda t: t.replace(SECRET, "***"))
    yield
    scrub.set_output_scrubber(None)
    scrub._noted.clear()
    scrub._noted.update(noted)


def test_no_scrubber_is_a_noop():
    assert not scrub.output_scrubbing()
    obj = {"a": [SECRET]}
    assert scrub.scrub(SECRET) == SECRET and scrub.scrub_json(obj) is obj
    scrub.refresh_secrets()                               # nothing installed: nothing to tell


def test_the_switch():
    assert scrub.redaction_requested({"SBMIG_REDACT_OUTPUT": "1"}) and scrub.redaction_requested({"SBMIG_REDACT_OUTPUT": " 1 "})
    assert not scrub.redaction_requested({}) and not scrub.redaction_requested({"SBMIG_REDACT_OUTPUT": "true"})
    assert not scrub.redaction_requested({"SBMIG_REDACT_OUTPUT": ""})


def test_scrub_json_values_and_keys(masked):
    src = {"k": f"a {SECRET}", SECRET: [1, None, True, (SECRET,)], "n": {"x": 1.5}}
    assert scrub.scrub_json(src) == {"k": "a ***", "***": [1, None, True, ["***"]], "n": {"x": 1.5}}
    assert src["k"] == f"a {SECRET}"                       # a copy: the caller's object is untouched


def test_a_failing_scrubber_withholds_the_line(tmp_path):
    def boom(text):
        raise RuntimeError("the secret source is unreadable")
    scrub.set_output_scrubber(boom)
    try:
        assert scrub.scrub(f"token {SECRET}") == scrub.WITHHELD
        # a JSON value is dropped as a whole, in one shape a reader can recognise (never key by key)
        assert scrub.scrub_json({"a": SECRET, "n": 1}) == {"event": "output_withheld", "message": scrub.WITHHELD}
        assert scrub.scrub_json([SECRET, 1]) == [scrub.WITHHELD] and scrub.scrub_json(SECRET) == scrub.WITHHELD
        assert scrub.scrub_json(7) == 7
        (tmp_path / "p").mkdir()
        ws = Workspace(tmp_path / "p", tmp_path / "run", "t")
        ws.journal({"event": "llm_call", "key": SECRET})
        text = ws.journal_path.read_text(encoding="utf-8")
        rec = json.loads(text.splitlines()[-1])
        assert SECRET not in text and rec["event"] == "output_withheld" and rec["run_id"] == "t" and rec["ts"]
    finally:
        scrub.set_output_scrubber(None)



def test_formatter_scrubs_message_and_traceback(masked):
    try:
        raise ValueError(f"bad token {SECRET}")
    except ValueError:
        rec = logging.LogRecord("sbmig.x", logging.ERROR, __file__, 1, "call failed: %s", (SECRET,), sys.exc_info())
    for fmt in (stage_formatter(for_file=True), stage_formatter("[%(stage)s] %(message)s", datefmt=None)):
        text = fmt.format(rec)
        assert SECRET not in text and "call failed: ***" in text and "ValueError: bad token ***" in text
    # RichHandler builds its line from formatMessage alone
    assert stage_formatter().formatMessage(rec).endswith("call failed: ***")


def test_formatter_without_a_scrubber_is_unchanged():
    rec = logging.LogRecord("sbmig.x", logging.INFO, __file__, 1, "value %s", (SECRET,), None)
    assert stage_formatter(for_file=True).format(rec).endswith(f"value {SECRET}")


def test_journal_line_is_scrubbed_and_stays_json(tmp_path, masked):
    (tmp_path / "p").mkdir()
    ws = Workspace(tmp_path / "p", tmp_path / "run", "t")
    ws.journal({"event": "llm_call", "url": f"https://h/x?k={SECRET}", "nested": {"v": [SECRET, 3]}, "한글": "값"})
    text = ws.journal_path.read_text(encoding="utf-8")
    rec = json.loads(text.splitlines()[-1])
    assert SECRET not in text
    assert rec["event"] == "llm_call" and rec["run_id"] == "t" and rec["url"] == "https://h/x?k=***"
    assert rec["nested"] == {"v": ["***", 3]} and rec["한글"] == "값" and "한글" in text      # ensure_ascii stays off


def test_journal_value_with_quotes_keeps_valid_json(tmp_path):
    secret = 'pa"ss\\word-1'                              # a quote and a backslash: escaped inside the JSON line
    (tmp_path / "p").mkdir()
    ws = Workspace(tmp_path / "p", tmp_path / "run", "t")
    scrub.set_output_scrubber(lambda t: t.replace(secret, "***"))
    try:
        ws.journal({"event": "x", "password": secret, "text": f"used {secret} twice {secret}"})
    finally:
        scrub.set_output_scrubber(None)
    rec = json.loads(ws.journal_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["password"] == "***" and rec["text"] == "used *** twice ***"


def test_note_secret_tells_the_scrubber_once():
    calls: list[int] = []
    noted = set(scrub._noted)
    scrub.set_output_scrubber(lambda t: t, on_refresh=lambda: calls.append(1))
    try:
        for v in ("tok-abcdef-1", "tok-abcdef-1", "", "   ", None, 12345):
            scrub.note_secret(v)
        assert calls == [1] and "tok-abcdef-1" in scrub.noted_secrets()
        scrub.refresh_secrets()
        assert calls == [1, 1]
    finally:
        scrub.set_output_scrubber(None)
        scrub._noted.clear()
        scrub._noted.update(noted)


def test_refresh_never_raises():
    scrub.set_output_scrubber(lambda t: t, on_refresh=lambda: 1 / 0)
    try:
        scrub.refresh_secrets()
    finally:
        scrub.set_output_scrubber(None)
    scrub.refresh_secrets()                               # removing the scrubber removed its refresh callback too


def test_uncaught_tracebacks_are_scrubbed(masked, capsys):
    old = sys.excepthook, threading.excepthook
    try:
        scrub.install_scrubbed_excepthooks()
        try:
            raise RuntimeError(f"gateway said no to {SECRET}")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        err = capsys.readouterr().err
        assert SECRET not in err and "Traceback" in err and "RuntimeError: gateway said no to ***" in err

        def work():
            raise RuntimeError(f"thread {SECRET}")
        t = threading.Thread(target=work)
        t.start()
        t.join()
        err = capsys.readouterr().err
        assert SECRET not in err and "RuntimeError: thread ***" in err
    finally:
        sys.excepthook, threading.excepthook = old


def test_unprintable_exception_still_yields_text(masked):
    class Bad(Exception):
        def __str__(self):
            raise RuntimeError("no text")
    text = scrub.format_exception_scrubbed(Bad, Bad(), None)
    assert "Bad" in text
