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
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
    found = cfgmod.load_dotenv_files()
    assert f in found
    assert os.environ["LLM_API_KEY"] == "from-file" and os.environ["SBMIG_T_NEW"] == "v 1"
    assert os.environ["SBMIG_T_KEEP"] == "shell"          # the shell wins over the file
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)


def test_load_config_reads_dotenv_for_expansion(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("LLM_BASE_URL=http://gw.local/chat\n")
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
    c = cfgmod.load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}})
    assert c.llm.base_url == "http://gw.local/chat"
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)


def test_dotenv_value_that_is_only_a_comment_is_empty(tmp_path, monkeypatch):
    """2026-10-23 (company .env): `LLM_RECIPE_MODEL=   # cheaper model` made the comment text the model name — the
    gateway answered HTTP 400 `"# cheaper model" is an unsupported model` on the first recipe call."""
    f = tmp_path / "roles.env"
    f.write_text("SBMIG_T_RECIPE=                        # cheaper model for recipe candidates (empty = LLM_MODEL)\n"
                 "SBMIG_T_ESC= #model\n"
                 "SBMIG_T_FIX=gpt-5.6-terra # fix model\n"
                 "SBMIG_T_HASH='#literal'\n", encoding="utf-8")
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    for k in ("SBMIG_T_RECIPE", "SBMIG_T_ESC", "SBMIG_T_FIX", "SBMIG_T_HASH"):
        monkeypatch.delenv(k, raising=False)
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
    cfgmod.load_dotenv_files()
    assert os.environ["SBMIG_T_RECIPE"] == "" and os.environ["SBMIG_T_ESC"] == ""
    assert os.environ["SBMIG_T_FIX"] == "gpt-5.6-terra"
    assert os.environ["SBMIG_T_HASH"] == "#literal"        # quoted values are taken verbatim
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)


def test_llm_model_names_are_cleaned_at_config_load(caplog):
    """Shell variables bypass the .env parser (`set LLM_RECIPE_MODEL=# x` on Windows): the config cleans the names."""
    caplog.set_level("WARNING", logger="sbmig.config")
    c = cfgmod.load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                      "llm": {"model": "gpt-5.6-terra  # fix", "escalation_model": "gpt-x # strong",
                                              "recipe_model": " # cheaper model"}})
    assert (c.llm.model, c.llm.escalation_model, c.llm.recipe_model) == ("gpt-5.6-terra", "gpt-x", None)
    assert sum("is not a plain model name" in r.message for r in caplog.records) == 3
    # untouched: plain names and unexpanded placeholders (the fixer treats `${…}` as unset)
    c = cfgmod.load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                      "llm": {"model": "gpt-6-luna", "recipe_model": "${LLM_RECIPE_MODEL}"}})
    assert (c.llm.model, c.llm.recipe_model) == ("gpt-6-luna", "${LLM_RECIPE_MODEL}")


def test_dotenv_encodings_and_reload_after_an_edit(tmp_path, monkeypatch):
    """Round 10: Notepad's UTF-8 BOM, PowerShell 5.1 `>` (UTF-16) and cp949 files are read; an edited file is read
    again (the web server reads .env per run), but a shell variable still wins."""
    f = tmp_path / "w.env"
    monkeypatch.setenv("SBMIG_ENV_FILE", str(f))
    for k in ("SBMIG_T_BOM", "SBMIG_T_U16", "SBMIG_T_KO", "SBMIG_T_SHELL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SBMIG_T_SHELL", "shell")
    f.write_bytes("SBMIG_T_BOM=one\nSBMIG_T_SHELL=file\n".encode("utf-8-sig"))
    cfgmod.load_dotenv_files()
    assert os.environ["SBMIG_T_BOM"] == "one" and os.environ["SBMIG_T_SHELL"] == "shell"
    f.write_bytes("SBMIG_T_BOM=two\nSBMIG_T_U16=x\n".encode("utf-16"))
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    cfgmod.load_dotenv_files()
    assert os.environ["SBMIG_T_BOM"] == "two" and os.environ["SBMIG_T_U16"] == "x"
    f.write_bytes("SBMIG_T_KO=한글\n".encode("cp949"))
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    cfgmod.load_dotenv_files()
    assert os.environ["SBMIG_T_KO"] == "한글"
    cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)


def test_dotenv_is_found_up_to_the_git_root(tmp_path, monkeypatch):
    """The package may live in a sub-folder of another repo (`<root>/src/migration-agent/sbmig`): the root's `.env`
    is read; a nearer `.env` wins a key; nothing above the folder that holds `.git` is read (2026-10-28)."""
    root = tmp_path / "company-repo"; (root / ".git").mkdir(parents=True)
    pkg = root / "src" / "migration-agent"; pkg.mkdir(parents=True)
    (tmp_path / ".env").write_text("SBMIG_T_ABOVE=leak\n")                 # above the git root: must not be read
    (root / ".env").write_text("SBMIG_T_ROOT=root\nSBMIG_T_BOTH=root\n")
    (pkg / ".env").write_text("SBMIG_T_BOTH=near\n")
    for k in ("SBMIG_T_ABOVE", "SBMIG_T_ROOT", "SBMIG_T_BOTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("SBMIG_ENV_FILE", raising=False)
    monkeypatch.setattr(cfgmod, "PACKAGE_ROOT", pkg)
    monkeypatch.chdir(pkg)
    for f in (tmp_path / ".env", root / ".env", pkg / ".env"):
        cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
    try:
        found = cfgmod.load_dotenv_files()
        assert root / ".env" in found and pkg / ".env" in found and (tmp_path / ".env") not in found
        assert os.environ["SBMIG_T_ROOT"] == "root"
        assert os.environ["SBMIG_T_BOTH"] == "near"              # the nearer file wins
        assert "SBMIG_T_ABOVE" not in os.environ
        chain = cfgmod.dotenv_chain(pkg)
        assert chain[0] == pkg / ".env" and chain[-1] == root / ".env"
    finally:
        for f in (tmp_path / ".env", root / ".env", pkg / ".env"):
            cfgmod._DOTENV_LOADED.pop(str(f.resolve()), None)
        for k in ("SBMIG_T_ROOT", "SBMIG_T_BOTH"):
            cfgmod._DOTENV_SET.pop(k, None)

