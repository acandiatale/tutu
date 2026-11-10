"""2026-11-10 (review: redaction at output time): with `SBMIG_REDACT_OUTPUT=1` an sbmig process redacts its own log
lines, journal lines and uncaught tracebacks while it writes them, refuses to start when that cannot be set up, and
runs one last pass over the run dir for the files write-time redaction does not reach."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.cli import app, final_redaction, install_redaction_if_requested, setup_logging
from sbmig.core import config as cfgmod
from sbmig.core import scrub

REPO = Path(__file__).resolve().parents[2]
SECRET = "tok-9f8e7d6c5b4a"


@pytest.fixture
def clean_scrubber():
    """Whatever the test installs (scrubber, excepthooks, noted secrets) is removed afterwards."""
    old = sys.excepthook, threading.excepthook
    noted = set(scrub._noted)
    yield
    scrub.set_output_scrubber(None)
    sys.excepthook, threading.excepthook = old
    scrub._noted.clear()
    scrub._noted.update(noted)


def test_nothing_is_installed_without_the_switch(monkeypatch, tmp_path, clean_scrubber):
    monkeypatch.delenv("SBMIG_REDACT_OUTPUT", raising=False)
    hook = sys.excepthook
    assert install_redaction_if_requested() is False
    assert not scrub.output_scrubbing() and sys.excepthook is hook
    (tmp_path / "sbmig.log").write_text(f"k={SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("MY_API_KEY", SECRET)
    assert final_redaction(tmp_path) == 0                  # the final pass belongs to the switch too
    assert SECRET in (tmp_path / "sbmig.log").read_text(encoding="utf-8")


def test_the_switch_installs_a_live_scrubber_and_the_final_pass(monkeypatch, tmp_path, clean_scrubber):
    monkeypatch.setenv("SBMIG_REDACT_OUTPUT", "1")
    monkeypatch.setenv("MY_API_KEY", SECRET)
    hook = sys.excepthook
    assert install_redaction_if_requested() is True and scrub.output_scrubbing()
    assert sys.excepthook is not hook
    mine = sys.excepthook
    assert install_redaction_if_requested() is True and sys.excepthook is mine      # idempotent
    assert scrub.scrub(f"Authorization: Bearer {SECRET}") == "Authorization: Bearer ***"
    scrub.note_secret("exchanged-token-xyz")                # a token born during the run
    assert scrub.scrub("auth exchanged-token-xyz") == "auth ***"
    rd = tmp_path / "run"
    (rd / "logs").mkdir(parents=True)
    (rd / "logs" / "build-001.log").write_text(f"[INFO] -Dtoken={SECRET} exchanged-token-xyz\n", encoding="utf-8")
    (rd / "report.md").write_text("# clean\n", encoding="utf-8")
    assert final_redaction(rd) == 1
    assert (rd / "logs" / "build-001.log").read_text(encoding="utf-8") == "[INFO] -Dtoken=*** ***\n"


def test_a_dotenv_read_later_is_redacted_at_once(tmp_path, monkeypatch, clean_scrubber):
    f = tmp_path / "late.env"
    f.write_text("SBMIG_T_LATE_TOKEN=late-token-0123456789\n", encoding="utf-8")
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    monkeypatch.setenv("SBMIG_REDACT_OUTPUT", "1")
    monkeypatch.delenv("SBMIG_T_LATE_TOKEN", raising=False)
    assert install_redaction_if_requested()
    assert scrub.scrub("x late-token-0123456789") == "x late-token-0123456789"        # not known yet
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
    try:
        cfgmod.load_dotenv_files()
        assert os.environ["SBMIG_T_LATE_TOKEN"] == "late-token-0123456789"
        assert scrub.scrub("x late-token-0123456789") == "x ***"                     # no waiting for the refresh interval
    finally:
        cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)


def test_cli_refuses_to_run_when_redaction_cannot_be_installed(monkeypatch, clean_scrubber):
    import sbmig.web.redact as redact

    def broken():
        raise RuntimeError("cannot read the environment")
    monkeypatch.setenv("SBMIG_REDACT_OUTPUT", "1")
    monkeypatch.setattr(redact, "install_output_redaction", broken)
    r = CliRunner().invoke(app, ["version"])
    assert r.exit_code == 3, r.output
    assert "refusing to run without it" in r.output and "RuntimeError" in r.output
    assert not scrub.output_scrubbing()


def test_cli_runs_normally_with_the_switch(monkeypatch, clean_scrubber):
    monkeypatch.setenv("SBMIG_REDACT_OUTPUT", "1")
    r = CliRunner().invoke(app, ["version"])
    assert r.exit_code == 0, r.output
    assert scrub.output_scrubbing()


def test_console_handler_drops_rich_tracebacks_under_redaction(clean_scrubber):
    from rich.logging import RichHandler

    root = logging.getLogger("sbmig")
    kept = [h for h in root.handlers if isinstance(h, RichHandler)]
    for h in kept:
        root.removeHandler(h)
    try:
        scrub.set_output_scrubber(lambda t: t)
        setup_logging(False)
        mine = [h for h in root.handlers if isinstance(h, RichHandler)]
        assert len(mine) == 1 and mine[0].rich_tracebacks is False       # rich draws tracebacks outside the formatter
    finally:
        for h in [h for h in root.handlers if isinstance(h, RichHandler)]:
            root.removeHandler(h)
        for h in kept:
            root.addHandler(h)


def test_a_real_process_never_prints_the_secret(tmp_path):
    """One process end to end: console handler, file handler, a logged exception, an uncaught exception."""
    script = tmp_path / "demo.py"
    script.write_text(textwrap.dedent("""
        import logging, os, sys
        from pathlib import Path
        from sbmig.cli import attach_file_log, install_redaction_if_requested, setup_logging
        assert install_redaction_if_requested()
        setup_logging(False)
        attach_file_log(Path(sys.argv[1]))
        log = logging.getLogger("sbmig.demo")
        key = os.environ["DEMO_API_KEY"]
        log.info("calling the gateway with %s", key)
        try:
            raise ValueError("rejected: " + key)
        except ValueError:
            log.exception("the call failed")
        raise RuntimeError("uncaught " + key)
    """), encoding="utf-8")
    env = {**os.environ, "SBMIG_REDACT_OUTPUT": "1", "DEMO_API_KEY": SECRET, "COLUMNS": "200",
           "PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, str(script), str(tmp_path / "x.log")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, cwd=tmp_path, timeout=120)
    filed = (tmp_path / "x.log").read_text(encoding="utf-8")
    everything = p.stdout + p.stderr + filed
    assert p.returncode == 1, everything
    assert SECRET not in everything
    assert "calling the gateway with ***" in filed and "ValueError: rejected: ***" in filed
    assert "calling the gateway with ***" in p.stdout + p.stderr
    assert "RuntimeError: uncaught ***" in p.stderr
