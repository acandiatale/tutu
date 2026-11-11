"""Shared helpers of the doc-drift tests (tests/docs/test_agent_doc.py): backticked tokens outside fenced code,
relative links, the characters that mark a token as a placeholder, and the set of real config key paths."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_TICK = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
_SKIP_CHARS = set("<>{}*…$@ \t\"'|,;()")


def _tokens(text: str) -> list[str]:
    # fenced code blocks are examples (commands, trees, yaml): only inline backticks are contracts
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return _TICK.findall(text)


def _config_paths() -> set[str]:
    from sbmig.core.config import Config, load_config

    def flatten(d: dict, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for k, v in d.items():
            key = f"{prefix}{k}"
            out.add(key)
            if isinstance(v, dict):
                out |= flatten(v, key + ".")
        return out

    paths: set[str] = set()
    for src in ("config/artifacts.example.yml", "config/artifacts.local-e2e.yml"):
        try:
            paths |= flatten(load_config(REPO / src, repo_root=REPO).model_dump())
        except Exception:  # noqa: BLE001 - one example file being unloadable must not hide the other
            continue
    paths |= set(Config.model_fields)
    return paths
