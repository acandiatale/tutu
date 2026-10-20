"""`.env` at the repo root / cwd / $SBMIG_ENV_FILE feeds os.environ before a config is loaded (env wins)."""
from __future__ import annotations

import os

from sbmig.core import config as cfgmod


def test_dotenv_file_fills_missing_variables_only(tmp_path, monkeypatch):
    f = tmp_path / "my.env"
    f.write_text("# comment\nexport LLM_API_KEY='from-file'\nSBMIG_T_NEW=\"v 1\"\nSBMIG_T_KEEP=file # trailing\n")
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    monkeypatch.setenv("SBMIG_T_KEEP", "shell")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("SBMIG_T_NEW", raising=False)
    cfgmod._DOTENV_LOADED.discard(str(f.resolve()))
    found = cfgmod.load_dotenv_files()
    assert f in found
    assert os.environ["LLM_API_KEY"] == "from-file" and os.environ["SBMIG_T_NEW"] == "v 1"
    assert os.environ["SBMIG_T_KEEP"] == "shell"          # the shell wins over the file
    cfgmod._DOTENV_LOADED.discard(str(f.resolve()))


def test_load_config_reads_dotenv_for_expansion(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("LLM_BASE_URL=http://gw.local/chat\n")
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfgmod._DOTENV_LOADED.discard(str(f.resolve()))
    c = cfgmod.load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}})
    assert c.llm.base_url == "http://gw.local/chat"
    cfgmod._DOTENV_LOADED.discard(str(f.resolve()))
