"""The Korean handbook (docs/handbook) must not drift from the code: every backticked repo path it names exists,
every backticked `section.key` config key is a real Config key, every `sbmig <command>` is a registered CLI
command, and every handbook cross-link resolves. Run-output / migrated-project paths are written with
placeholders (`out/<run id>/…`), which this test skips."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HANDBOOK = REPO / "docs" / "handbook"
DOCS = sorted(HANDBOOK.glob("*.md"))
TOP_LEVEL = ("sbmig/", "docs/", "config/", "knowledge/", "kb/", "fixtures/", "tests/", "scripts/", "coding/",
             "examples/")
ROOT_FILES = {"CLAUDE.md", "README.md", "pyproject.toml", ".env.example"}
# files the company creates locally from the examples (git-ignored / never in the repo) — legitimate to name
LOCAL_FILES = {"config/artifacts.yml", "config/settings.nexus.xml", "config/settings.xml", ".env", "coding/.env"}
# v1 files deleted in v2 step 4 (2026-10-27) that the handbook still names: the handbook describes v1 until the user
# asks for its v2 rewrite ("핸드북은 내가 말할 때만", 2026-10-27) — a path under these prefixes is not a drift finding
V1_PATHS_PENDING_REWRITE = ("knowledge/recipes/base", "knowledge/recipes/library_recipes.yml", "sbmig/kb/harvest.py",
                            "sbmig/kb/ledger.py", "sbmig/kb/recipes.py", "sbmig/kb/prompts/recipe_candidate.md",
                            "sbmig/plan/library_recipes.py", "sbmig/plan/pre_handlers.py", "sbmig/triage/handlers",
                            "sbmig/triage/recipe_rules.yml", "sbmig/triage/rules.py", "sbmig/fix/apply.py",
                            "sbmig/fix/protocol.py", "sbmig/fix/prompts/system_v1.md", "sbmig/fix/prompts/user_v1.md.j2",
                            "scripts/demo-kb/replay")
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


@pytest.fixture(scope="module")
def config_paths() -> set[str]:
    return _config_paths()


@pytest.fixture(scope="module")
def cli_words() -> set[str]:
    from sbmig.cli import app
    words = {(c.name or c.callback.__name__.replace("_", "-")) for c in app.registered_commands}
    words |= {g.name for g in app.registered_groups if g.name}
    return words


@pytest.mark.skipif(not DOCS, reason="no handbook yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_handbook_paths_exist(doc: Path) -> None:
    missing: list[str] = []
    for tok in _tokens(doc.read_text(encoding="utf-8")):
        t = tok.strip().rstrip("/")
        if not t or any(c in _SKIP_CHARS for c in t) or t.startswith(("http://", "https://", "/", "~")):
            continue
        if t in LOCAL_FILES or t.startswith(V1_PATHS_PENDING_REWRITE):
            continue
        if t.startswith(TOP_LEVEL) or t in ROOT_FILES:
            if not (REPO / t).exists():
                missing.append(tok)
    assert not missing, f"{doc.name}: backticked repo paths that do not exist: {sorted(set(missing))}"


@pytest.mark.skipif(not DOCS, reason="no handbook yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_handbook_config_keys_exist(doc: Path, config_paths: set[str]) -> None:
    sections = {p.split(".")[0] for p in config_paths}
    bad: list[str] = []
    for tok in _tokens(doc.read_text(encoding="utf-8")):
        t = tok.strip()
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+(?:\.[a-z0-9_*]+)*", t):
            continue
        head = t.split(".")[0]
        if head not in sections:
            continue                                   # java packages, file names, log phrases: not config keys
        parts = t.split(".")
        # dict-valued keys (loop.phases.<phase>.goal, artifacts.optional entries, kb.*) accept any tail after a
        # known prefix; `*` is the wildcard used in prose (`layout.*`)
        ok = False
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in config_paths or prefix.endswith(".*") and prefix[:-2] in config_paths:
                ok = True
                break
            if i < len(parts) and parts[i] == "*":
                continue
        if not ok:
            bad.append(tok)
    assert not bad, f"{doc.name}: backticked config keys that are not Config keys: {sorted(set(bad))}"


@pytest.mark.skipif(not DOCS, reason="no handbook yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_handbook_cli_commands_exist(doc: Path, cli_words: set[str]) -> None:
    bad: list[str] = []
    for tok in _tokens(doc.read_text(encoding="utf-8")):
        m = re.match(r"sbmig\s+([a-z][a-z0-9-]*)", tok.strip())
        if m and m.group(1) not in cli_words and m.group(1) not in {"run", "serve"}:
            bad.append(tok)
    assert not bad, f"{doc.name}: `sbmig <command>` that is not a CLI command: {sorted(set(bad))} (known: {sorted(cli_words)})"


@pytest.mark.skipif(not DOCS, reason="no handbook yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_handbook_links_resolve(doc: Path) -> None:
    bad: list[str] = []
    for target in _LINK.findall(doc.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (doc.parent / target).exists():
            bad.append(target)
    assert not bad, f"{doc.name}: broken relative links: {sorted(set(bad))}"
