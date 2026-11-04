"""v2.1z: what a tier-2 fixer plugin written against v2.1i may still read after the edits mode was removed, and what an
older company yml may still contain."""
from __future__ import annotations

from pathlib import Path

import logging

from sbmig.core.config import _warn_removed_keys, load_config
from sbmig.core.interfaces import FixContext
from sbmig.core.models import Phase


def test_fix_context_keeps_read_only_v21i_names():
    ctx = FixContext(phase=Phase.VERIFY, iteration=1, stall_level=0, profile_summary="", plan_notes=[])
    assert ctx.fix_mode == "script" and ctx.previous_edits == {}
    # properties, not fields: never serialized, never rendered into a prompt section
    assert "fix_mode" not in ctx.model_dump() and "previous_edits" not in ctx.model_dump()
    assert "fix_mode" not in FixContext.model_fields and "previous_edits" not in FixContext.model_fields


def test_old_company_yml_with_the_removed_fix_keys_still_loads(tmp_path: Path, repo_root: Path):
    yml = tmp_path / "company.yml"
    yml.write_text("fix:\n  mode: edits\n  edits_max_asks_per_case: 3\nscript:\n  max_augments_per_case: 15\n", encoding="utf-8")
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", yml)
    assert cfg.script.max_augments_per_case == 15 and cfg.script.max_llm_tries_per_case == 20
    assert not hasattr(cfg.fix, "mode") and not hasattr(cfg.fix, "edits_max_asks_per_case")


def test_removed_and_changed_keys_are_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="sbmig.config"):
        lines = _warn_removed_keys({"fix": {"mode": "edits", "edits_max_asks_per_case": 3},
                                    "script": {"max_augments_per_case": 15}})
    assert [l.split(" ")[1] for l in lines] == ["fix.mode='edits'", "fix.edits_max_asks_per_case=3",
                                                 "script.max_augments_per_case=15"]
    assert "ignored since v2.1z" in lines[0] and "max_llm_tries_per_case" in lines[2] and "default 20" in lines[2]
    assert sum("ignored since v2.1z" in rec.message for rec in caplog.records) == 2
    # a yml that sets both caps, or neither, says nothing
    assert _warn_removed_keys({"script": {"max_augments_per_case": 15, "max_llm_tries_per_case": 20}}) == []
    assert _warn_removed_keys({}) == []


def test_v1_pom_repositories_key_is_named():
    # the company yml still carries the v1 X9 key: it is ignored, the warning points at the settings.xml mirror
    lines = _warn_removed_keys({"build": {"pom_repositories": "keep"}})
    assert len(lines) == 1 and lines[0].startswith("config: build.pom_repositories='keep' is ignored since v2")
    assert "mirrorOf external:*" in lines[0] and "RUNBOOK" in lines[0]
