"""v2.1z-br, without a visible effect: config/default.yml lists every key of the config model a person may set, at the
model's default value; `openrewrite.fail_on_dry_run_results` left the model and a file that still sets it loads."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from sbmig.core.config import DEFAULT_CONFIG_PATH, Config, OpenRewriteConfig, _read_yaml, _section_model, load_config

PARENT = {"group_id": "com.example", "artifact_id": "base-parent", "version": "1.0.0"}

# keys the file leaves out on purpose: the company coordinates live in the company file (required, no default);
# repo_root is set by the loader from where the package sits, never by a person; openrewrite.recipes.extra (empty by
# default) stays out because tests/core/test_recipe_set_defaults.py pins the file's recipe sets as the model's sets
# without `extra`
NOT_IN_FILE = {"artifacts", "repo_root", "openrewrite.recipes.extra"}

# keys whose value in the file differs from the model default on purpose (each one explained by its comment in the file)
DIFFERS_ON_PURPOSE = {
    # the gateway settings come from the environment (.env); the file names the variables and their fallbacks
    "llm.model": "${LLM_MODEL:-gpt-6-luna}",
    "llm.escalation_model": "${LLM_ESCALATION_MODEL:-}",
    "llm.recipe_model": "${LLM_RECIPE_MODEL:-}",
    "llm.record_dir": "${LLM_RECORD_DIR:-}",
    "llm.gateway": "${LLM_GATEWAY:-legacy}",
    "llm.asset_id": "${LLM_ASSET_ID:-}",
    "llm.token_url": "${LLM_TOKEN_URL:-}",
    "llm.response_path": "${LLM_RESPONSE_PATH:-}",
    # a field the company gateway requires on the legacy payload
    "llm.payload_extra": {"need_origin": True},
    # public list prices for the cost table, and the sentence that says so
    "llm.pricing": None,
    "llm.pricing_note": None,
}


def _file_config() -> Config:
    data = _read_yaml(DEFAULT_CONFIG_PATH)          # the file alone: no .env, no SBMIG_* variables, no ${VAR} expansion
    assert "artifacts" not in data
    return Config.model_validate({**data, "artifacts": {"parent": PARENT}})


def _model_config() -> Config:
    return Config.model_validate({"artifacts": {"parent": PARENT}})


def _leaves(a: object, b: object, path: str = ""):
    if isinstance(a, BaseModel) and type(a).__name__ != "ModelPrice":
        for name in type(a).model_fields:
            yield from _leaves(getattr(a, name), getattr(b, name), f"{path}.{name}" if path else name)
    else:
        yield path, a, b


def test_every_settable_key_of_the_model_is_in_default_yml():
    data = _read_yaml(DEFAULT_CONFIG_PATH)
    missing: list[str] = []

    def walk(model: type[BaseModel], d: dict, prefix: str) -> None:
        for name, field in model.model_fields.items():
            key = prefix + name
            if key in NOT_IN_FILE:
                continue
            if name not in d:
                missing.append(key)
                continue
            sub = _section_model(field.annotation)
            if sub is not None and isinstance(d[name], dict):
                walk(sub, d[name], key + ".")

    walk(Config, data, "")
    assert missing == [], missing


def test_default_yml_gives_the_model_defaults_except_the_documented_keys():
    differs = {path: (got, want) for path, got, want in _leaves(_file_config(), _model_config()) if got != want}
    assert set(differs) == set(DIFFERS_ON_PURPOSE), sorted(set(differs) ^ set(DIFFERS_ON_PURPOSE))
    for path, expected in DIFFERS_ON_PURPOSE.items():
        if expected is not None:
            assert differs[path][0] == expected, path
    cfg = _file_config()
    assert set(cfg.llm.pricing) == {"gpt-5.6-terra", "gpt-6-luna"} and cfg.llm.pricing_note.startswith("OpenAI list prices")


def test_the_fix_loop_switches_stay_as_shipped():
    cfg = _file_config()
    assert cfg.remediation.fix_loop is True
    assert cfg.framework.fix_loop is False
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    assert "\n  fix_loop: true\n" in text and "\n  fix_loop: false\n" in text


def test_fail_on_dry_run_results_left_the_model():
    assert "fail_on_dry_run_results" not in OpenRewriteConfig.model_fields
    assert "fail_on_dry_run_results" not in DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def test_a_company_file_that_still_sets_the_removed_key_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A company yml written while the key existed keeps loading, every other value in it still applies, and the
    loader names the key with the removed-key sentence instead of the misspelling warning."""
    company = tmp_path / "artifacts.yml"
    company.write_text(
        "artifacts:\n"
        "  parent: {group_id: com.example, artifact_id: base-parent, version: 1.0.0}\n"
        "openrewrite:\n"
        "  fail_on_dry_run_results: true\n"
        "  timeout_minutes: 55\n",
        encoding="utf-8")
    monkeypatch.delenv("SBMIG_OPENREWRITE__TIMEOUT_MINUTES", raising=False)
    cfg = load_config(company)
    assert cfg.openrewrite.timeout_minutes == 55
    assert not hasattr(cfg.openrewrite, "fail_on_dry_run_results")
    assert "fail_on_dry_run_results" not in cfg.openrewrite.model_dump()
    removed = [w for w in cfg._config_warnings if "openrewrite.fail_on_dry_run_results" in w]
    assert removed == ["config: openrewrite.fail_on_dry_run_results=True is ignored: no code read it, so the key was "
                       "removed — remove it"], cfg._config_warnings
    assert not [w for w in cfg._config_warnings if "is not a known key" in w or "check the indentation / spelling" in w]


def test_the_removed_key_set_by_an_environment_variable_is_named_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    company = tmp_path / "artifacts.yml"
    company.write_text("artifacts:\n  parent: {group_id: com.example, artifact_id: base-parent, version: 1.0.0}\n",
                       encoding="utf-8")
    monkeypatch.setenv("SBMIG_OPENREWRITE__FAIL_ON_DRY_RUN_RESULTS", "true")
    cfg = load_config(company)
    removed = [w for w in cfg._config_warnings if "fail_on_dry_run_results" in w]
    assert removed == ["config: env override SBMIG_*__*: openrewrite.fail_on_dry_run_results is ignored: no code read "
                       "it, so the key was removed — remove it"], cfg._config_warnings
    assert not [w for w in cfg._config_warnings if "is not a known key" in w]
