"""The Korean document pack for in-house LLMs (agent-doc/) must not drift from the code: every backticked repo
path it names exists, every backticked `section.key` config key is a real Config key, every `sbmig <command>` is a
registered CLI command, every relative link resolves, and every file stays small enough for a Haiku-class context
window (the pack is fed to models that cannot see the code — a wrong path there is worse than none). Stricter
than tests/docs/test_handbook.py: no v1-era exceptions, run-time paths must use placeholders (`out/<run id>/…`)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.docs.test_handbook import _LINK, _SKIP_CHARS, _config_paths, _tokens

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "agent-doc"
DOCS = sorted(PACK.glob("*.md"))
TOP_LEVEL = ("sbmig/", "docs/", "config/", "knowledge/", "fixtures/", "tests/", "scripts/", "coding/", "examples/",
             "deploy/", "agent-doc/")
# run-time trees (never in the repo): a doc names them with placeholders, the test does not look for them
RUNTIME_PREFIXES = ("out/", "kb-v2/")
ROOT_FILES = {"CLAUDE.md", "README.md", "pyproject.toml", ".env.example", ".dockerignore"}
# files the company creates locally from the examples (git-ignored / never in the repo) — legitimate to name
LOCAL_FILES = {"config/artifacts.yml", "config/company.yml", "config/settings.nexus.xml", "config/settings.xml", ".env",
               "coding/.env", "deploy/k8s/30-secret.yaml"}
MAX_LINES = 350
MAX_BYTES = 30_000


@pytest.fixture(scope="module")
def config_paths() -> set[str]:
    return _config_paths()


@pytest.fixture(scope="module")
def cli_words() -> set[str]:
    from sbmig.cli import app
    words = {(c.name or c.callback.__name__.replace("_", "-")) for c in app.registered_commands}
    words |= {g.name for g in app.registered_groups if g.name}
    return words


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_agent_doc_paths_exist(doc: Path) -> None:
    missing: list[str] = []
    for tok in _tokens(doc.read_text(encoding="utf-8")):
        t = tok.strip().rstrip("/")
        if not t or any(c in _SKIP_CHARS for c in t) or t.startswith(("http://", "https://", "/", "~") + RUNTIME_PREFIXES):
            continue
        if t in LOCAL_FILES:
            continue
        if t.startswith(TOP_LEVEL) or t in ROOT_FILES:
            if not (REPO / t).exists():
                missing.append(tok)
    assert not missing, f"{doc.name}: backticked repo paths that do not exist: {sorted(set(missing))}"


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_agent_doc_config_keys_exist(doc: Path, config_paths: set[str]) -> None:
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


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_agent_doc_cli_commands_exist(doc: Path, cli_words: set[str]) -> None:
    bad: list[str] = []
    for tok in _tokens(doc.read_text(encoding="utf-8")):
        m = re.match(r"sbmig\s+([a-z][a-z0-9-]*)", tok.strip())
        if m and m.group(1) not in cli_words and m.group(1) not in {"run", "serve"}:
            bad.append(tok)
    assert not bad, f"{doc.name}: `sbmig <command>` that is not a CLI command: {sorted(set(bad))} (known: {sorted(cli_words)})"


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_agent_doc_links_resolve(doc: Path) -> None:
    bad: list[str] = []
    for target in _LINK.findall(doc.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (doc.parent / target).exists():
            bad.append(target)
    assert not bad, f"{doc.name}: broken relative links: {sorted(set(bad))}"


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_agent_doc_fits_a_small_context(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    lines, size = text.count("\n") + 1, len(text.encode("utf-8"))
    assert lines <= MAX_LINES and size <= MAX_BYTES, (
        f"{doc.name}: {lines} lines / {size} bytes — split it (limits {MAX_LINES} lines / {MAX_BYTES} bytes so a "
        f"Haiku-class model can take several files at once)")


@pytest.mark.skipif(not DOCS, reason="no agent-doc pack yet")
def test_agent_doc_index_lists_every_file() -> None:
    readme = PACK / "README.md"
    assert readme.exists(), "agent-doc/README.md (the index) is missing"
    text = readme.read_text(encoding="utf-8")
    unlisted = [d.name for d in DOCS if d.name != "README.md" and d.name not in text]
    assert not unlisted, f"agent-doc/README.md does not list: {unlisted}"
