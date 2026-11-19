"""The approval step of the remediation is gone: settings a cluster may still carry must not stop anything."""
from __future__ import annotations

import logging

from sbmig.core.config import RemediationConfig, load_config

ARTIFACTS = "config/artifacts.local-e2e.yml"


def test_the_model_has_no_approval_fields():
    assert "require_approval" not in RemediationConfig.model_fields
    assert "always_approve" not in RemediationConfig.model_fields


def test_old_yml_keys_are_ignored_with_one_explanation_each(tmp_path, caplog):
    yml = tmp_path / "old.yml"
    yml.write_text("remediation:\n  require_approval: true\n  always_approve: [major]\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(ARTIFACTS, yml)
    assert not hasattr(cfg.remediation, "require_approval")
    lines = [r.getMessage() for r in caplog.records if "approval step" in r.getMessage()]
    assert len(lines) == 2 and all("is ignored" in ln and "remove it" in ln for ln in lines)
    # explained once, not reported a second time as an unknown key
    assert not [r for r in caplog.records if "require_approval" in r.getMessage() and "not a known key" in r.getMessage()]


def test_the_old_environment_variable_is_ignored_and_explained(monkeypatch, caplog):
    for value in ("true", "false", ""):
        monkeypatch.setenv("SBMIG_REMEDIATION__REQUIRE_APPROVAL", value)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            cfg = load_config(ARTIFACTS)              # must not raise, whatever the value
        assert not hasattr(cfg.remediation, "require_approval")
        lines = [r.getMessage() for r in caplog.records if "approval step" in r.getMessage()]
        assert len(lines) == 1 and "env override" in lines[0], (value, lines)
