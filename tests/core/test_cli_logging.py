"""`sbmig run` writes <run dir>/sbmig.log; `--log-file` appends anywhere; `llm-probe` shows the provider."""
from __future__ import annotations

import logging
from pathlib import Path

from typer.testing import CliRunner

from sbmig.cli import app, attach_file_log

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "config" / "artifacts.local-e2e.yml"


def _detach(path: Path) -> None:
    root = logging.getLogger("sbmig")
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path.resolve():
            root.removeHandler(h)
            h.close()


def test_attach_file_log_writes_debug_lines_once(tmp_path):
    p = tmp_path / "x" / "sbmig.log"
    try:
        assert attach_file_log(p) == p.resolve() and attach_file_log(p) == p.resolve()
        logging.getLogger("sbmig.test").debug("debug-line")
        logging.getLogger("sbmig.test").info("info-line")
        handlers = [h for h in logging.getLogger("sbmig").handlers
                    if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == p.resolve()]
        assert len(handlers) == 1
        for h in handlers:
            h.flush()
        text = p.read_text(encoding="utf-8")
        assert "DEBUG   sbmig.test: debug-line" in text and "info-line" in text and "log file" in text
    finally:
        _detach(p)


def test_llm_probe_with_fake_provider(tmp_path):
    r = CliRunner().invoke(app, ["llm-probe", "--config", str(E2E), "--llm-provider", "fake"])
    assert r.exit_code == 0, r.output
    assert "provider=fake" in r.output and "reply (FakeLLMClient)" in r.output


def test_global_log_file_option(tmp_path):
    p = tmp_path / "cli.log"
    try:
        r = CliRunner().invoke(app, ["--log-file", str(p), "recipes", "check", "--config", str(E2E)])
        assert r.exit_code == 0, r.output
        assert "log file" in p.read_text(encoding="utf-8")
    finally:
        _detach(p)
