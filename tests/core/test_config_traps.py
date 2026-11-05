"""2026-11-05 (company PC: `build.repository_url` set in the yml, the probe still listed Central first): the two silent
config traps — a duplicate top-level block (YAML keeps the last one) and a mis-indented key (pydantic drops unknown keys)
— are WARNINGs now, and the shipped ymls raise none."""
from __future__ import annotations

import glob
import logging
from pathlib import Path

from sbmig.core.config import _warn_unknown_keys, load_config


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.getMessage().startswith("config:")]


def test_duplicate_top_level_block_is_named_with_both_lines(tmp_path: Path, repo_root: Path, caplog):
    yml = tmp_path / "company.yml"
    yml.write_text("build:\n  mvn: C:/maven/bin/mvn.cmd\nllm:\n  model: x\nbuild:\n  repository_url: http://nexus/repo/\n",
                   encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    lines = _warnings(caplog)
    assert any(f"config: {yml}: key 'build' appears twice (lines 1 and 5) — YAML keeps the LAST block only" in ln for ln in lines)
    # what PyYAML does: the first block is gone, the last one is what the run uses
    assert cfg.build.repository_url == "http://nexus/repo/" and cfg.build.mvn == "mvn"


def test_misplaced_key_is_named_with_the_section_it_belongs_to(tmp_path: Path, repo_root: Path, caplog):
    yml = tmp_path / "company.yml"
    yml.write_text("repository_url: http://nexus/repo/\nbuild:\n  repo_url: http://nexus/repo/\n  mvn: mvn\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    lines = _warnings(caplog)
    assert any(ln.startswith("config: repository_url is not a known key and is ignored") and "did you mean build.repository_url?" in ln
               for ln in lines)
    assert any(ln.startswith("config: build.repo_url is not a known key and is ignored") and "did you mean" not in ln for ln in lines)
    assert cfg.build.repository_url is None                                 # the value really is ignored


def test_unknown_key_check_descends_into_dict_of_model_entries_only_by_their_keys():
    # entry names of loop.phases / llm.pricing are free; keys inside an entry are checked; removed keys are not reported twice
    assert _warn_unknown_keys({"loop": {"phases": {"my-phase": {"max_iterations": 3}}}, "llm": {"pricing": {"gpt-x": {"input_per_mtok": 1.0}}}}) == []
    out = _warn_unknown_keys({"loop": {"phases": {"verify": {"max_iteratons": 3}}}})
    assert out == ["config: loop.phases.verify.max_iteratons is not a known key and is ignored (check the indentation / spelling)"]
    assert _warn_unknown_keys({"fix": {"mode": "edits"}, "build": {"pom_repositories": "x"}}) == []
    assert _warn_unknown_keys({"llm": {"payload_extra": {"need_origin": True}}}) == []     # dict fields are not inspected


def test_env_variables_that_are_not_config_keys_are_not_reported(tmp_path: Path, repo_root: Path, caplog, monkeypatch):
    # SBMIG_WEB_CONFIGS / SBMIG_WEB_TOKEN / a test's leftover SBMIG_T_X reach the merged dict through the env overlay and are
    # dropped by pydantic on purpose — only the yml keys are checked (the full suite had three --json CLI tests broken by it)
    monkeypatch.setenv("SBMIG_WEB_CONFIGS", "company.yml")
    monkeypatch.setenv("SBMIG_T_LEFTOVER", "1")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    assert _warnings(caplog) == []


def test_mistyped_env_override_is_reported_and_a_right_one_applies(tmp_path: Path, repo_root: Path, caplog, monkeypatch):
    # the k8s ConfigMap carries SBMIG_<SECTION>__<KEY> overrides (2026-11-05): a typo must not pass silently
    monkeypatch.setenv("SBMIG_LOOP__STAL_LIMIT", "3")
    monkeypatch.setenv("SBMIG_SCRIPT__MAX_LLM_TRIES_PER_CASE", "7")
    monkeypatch.setenv("SBMIG_LOOP__PHASES__VERIFY__MAX_ITERATIONS", "9")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml")
    lines = _warnings(caplog)
    assert any(ln.startswith("config: env override SBMIG_*__*: loop.stal_limit is not a known key and is ignored") for ln in lines)
    assert len(lines) == 1
    assert cfg.script.max_llm_tries_per_case == 7 and cfg.loop.phases["verify"].max_iterations == 9 and cfg.loop.stall_limit == 2


def test_yaml_merge_keys_still_load_and_merge(tmp_path: Path, repo_root: Path, caplog):
    # a company yml sharing a block between sections with `<<: *anchor` must keep loading (review 2026-11-05: the duplicate-key
    # loader had constructed the merge tag as a key)
    yml = tmp_path / "company.yml"
    yml.write_text("defaults: &d\n  timeout_minutes: 45\nbuild:\n  <<: *d\n  mvn: mvn\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    assert cfg.build.timeout_minutes == 45 and cfg.build.mvn == "mvn"
    assert [ln for ln in _warnings(caplog) if "defaults" not in ln] == []           # `defaults` itself is an unknown top-level key


def test_a_non_mapping_yml_is_reported_not_silently_dropped(tmp_path: Path, repo_root: Path, caplog):
    yml = tmp_path / "list.yml"
    yml.write_text("- build\n- llm\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    assert any(f"config: {yml}: the top level is a list, not a mapping of sections — the file is ignored" == ln for ln in _warnings(caplog))
    # every sentence travels with the config so the run can re-log it into its own sbmig.log
    assert cfg._config_warnings and any("not a mapping of sections" in w for w in cfg._config_warnings)


def test_list_of_model_entries_are_checked_by_their_keys():
    out = _warn_unknown_keys({"artifacts": {"mandatory": [{"group_id": "g", "artifact_id": "a", "verison": "1"}]}})
    assert out == ["config: artifacts.mandatory[0].verison is not a known key and is ignored (check the indentation / spelling)"]
    assert _warn_unknown_keys({"artifacts": {"mandatory": [{"group_id": "g", "artifact_id": "a", "version": "1"}]}}) == []


def test_every_shipped_yml_loads_without_a_config_warning(repo_root: Path, caplog):
    ymls = sorted(glob.glob(str(repo_root / "config" / "*.yml"))) + sorted(glob.glob(str(repo_root / "fixtures" / "*" / "sbmig.e2e.yml")))
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        for yml in ymls:
            if yml.endswith("default.yml"):
                continue
            if "artifacts" in Path(yml).name or yml.endswith("sbmig.e2e.yml"):
                load_config(yml)
            else:
                load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    assert _warnings(caplog) == []
