"""Remediation approval defaults: nothing waits for approval unless an operator switches it on."""
from __future__ import annotations

from pathlib import Path

import yaml

from sbmig.core.config import DEFAULT_CONFIG_PATH, RemediationConfig, load_config


def test_code_default_always_approve_is_empty():
    rem = RemediationConfig()
    assert rem.always_approve == [] and rem.require_approval is False


def test_shipped_default_yml_loads_to_no_approval(monkeypatch):
    for key in ("SBMIG_REMEDIATION__ALWAYS_APPROVE", "SBMIG_REMEDIATION__REQUIRE_APPROVAL", "SBMIG_REMEDIATION"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config("config/artifacts.example.yml")
    assert cfg.remediation.always_approve == []
    assert cfg.remediation.require_approval is False


def test_default_yml_states_the_empty_list_literally():
    data = yaml.safe_load(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    assert data["remediation"]["always_approve"] == []
    assert data["remediation"]["require_approval"] is False
