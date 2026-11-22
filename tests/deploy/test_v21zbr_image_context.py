"""v2.1z-br, visible effect 7: what enters the image build context, and `deploy/` copied once into the image.

The image cannot be built in the test suite, so these tests read the Dockerfile and the two .dockerignore files the way
Docker does: a pattern list (later lines win, `!` re-includes) matched against the path and each of its parent folders,
`*` / `?` inside one path segment, `**` across segments. A path the Dockerfile copies, or a file the running agent reads
under a copied folder, must survive the ignore file."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"
IGNORE = ROOT / ".dockerignore"
ROOT_IGNORE_EXAMPLE = ROOT / "deploy" / "docker" / "root.dockerignore.example"

# what the running agent reads below the copied folders (config.py resolves config/ and knowledge/ from the package root;
# the web pod reads its Job template from deploy/k8s; run-job.sh and the CRLF fix use deploy/docker/*.sh)
RUNTIME_ROOTS = ("sbmig", "config", "knowledge", "deploy", "pyproject.toml")
# the only file below RUNTIME_ROOTS that is kept out on purpose: the filled Secret
EXCLUDED_ON_PURPOSE = {"deploy/k8s/30-secret.yaml"}


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Docker's (moby patternmatcher) translation of one .dockerignore pattern."""
    p = posixpath.normpath(pattern.strip()).lstrip("/")
    out, i = "^", 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i + 1:i + 2] == "*":
                i += 1
                if p[i + 1:i + 2] == "/":
                    i += 1
                    out += "(.*/)?"
                else:
                    out += ".*"
            else:
                out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "\\" and i + 1 < len(p):
            i += 1
            out += re.escape(p[i])
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + "$")


def _rules(ignore_file: Path) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for line in ignore_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((negated, _pattern_regex(line[1:] if negated else line)))
    return rules


def _excluded(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """True when Docker leaves `path` (relative to the context root, `/`-separated) out of the context."""
    parts = path.strip("/").split("/")
    candidates = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    excluded = False
    for negated, rx in rules:
        if any(rx.match(c) for c in candidates):
            excluded = not negated
    return excluded


def _stages(text: str) -> list[list[str]]:
    """Dockerfile instructions (continuations joined, comments dropped), split per FROM."""
    lines, buf = [], ""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        lines.append(buf + s)
        buf = ""
    stages: list[list[str]] = [[]]
    for ln in lines:
        if ln.upper().startswith("FROM "):
            stages.append([])
        stages[-1].append(ln)
    return stages


def _arg_defaults(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^ARG (\w+)=(.*)$", text, re.M):
        out.setdefault(m.group(1), m.group(2).strip())
    return out


def _context_copies(text: str, args: dict[str, str]) -> list[str]:
    """Every context source path of every COPY / ADD without --from, ARG defaults substituted."""
    sources: list[str] = []
    for stage in _stages(text):
        for ln in stage:
            words = ln.split()
            if words[0].upper() not in ("COPY", "ADD") or any(w.startswith("--from") for w in words):
                continue
            paths = [w for w in words[1:] if not w.startswith("--")][:-1]
            for src in paths:
                for name, value in args.items():
                    src = src.replace("${" + name + "}", value).replace("$" + name, value)
                sources.append(posixpath.normpath(src))
    return sources


def _runtime_files(prefix: str = "") -> list[str]:
    out = []
    for root in RUNTIME_ROOTS:
        base = ROOT / root
        files = [base] if base.is_file() else [f for f in base.rglob("*") if f.is_file()]
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
                continue
            out.append(prefix + rel)
    return out


@pytest.mark.parametrize("path, excluded", [
    ("agent-doc/README.md", True),
    ("CLAUDE.md", True),
    ("scripts/handbook/node_modules/pptxgenjs/package.json", True),
    ("scripts/handbook/build_kb_map.py", True),
    ("out/e2e/summary.md", True),
    (".pytest_cache/v/cache/lastfailed", True),
    (".ruff_cache/CACHEDIR.TAG", True),
    (".coverage", True),
    (".coverage.host.1234", True),
    (".mypy_cache/3.11/cache.db", True),
    ("htmlcov/index.html", True),
    ("sbmig/__pycache__/cli.cpython-311.pyc", True),
    ("sbmig/core/__pycache__/config.cpython-311.pyc", True),
    ("deploy/k8s/30-secret.yaml", True),
    (".env", True),
    (".env.example", False),
    ("scripts/bounded.py", False),
    ("sbmig/cli.py", False),
    ("deploy/k8s/81-web-job.template.yaml", False),
    ("deploy/k8s/30-secret.example.yaml", False),
    ("config/default.yml", False),
    ("config/artifacts.yml", False),
    ("README.md", False),
])
def test_the_build_context_leaves_out_what_the_agent_never_reads(path: str, excluded: bool):
    assert _excluded(path, _rules(IGNORE)) is excluded, path


def test_every_path_the_dockerfile_copies_survives_the_dockerignore():
    text = DOCKERFILE.read_text(encoding="utf-8")
    rules = _rules(IGNORE)
    sources = _context_copies(text, _arg_defaults(text))
    assert "pyproject.toml" in sources and "deploy" in sources and "." in sources, sources
    for src in sources:
        if src != ".":
            assert not _excluded(src, rules), f"the Dockerfile copies {src}, .dockerignore drops it"
    # the folders copied whole: nothing the running agent reads below them may be dropped
    dropped = sorted(f for f in _runtime_files() if _excluded(f, rules))
    assert set(dropped) <= EXCLUDED_ON_PURPOSE, dropped
    # the RUN steps of the image read these paths
    for needed in ("deploy/docker/run-job.sh", "deploy/docker/pip-deps.py", "deploy/k8s/81-web-job.template.yaml",
                   "config/default.yml", "knowledge/recipes/catalog.yml", "sbmig/__init__.py"):
        assert (ROOT / needed).is_file() and not _excluded(needed, rules), needed


def test_the_company_layout_keeps_what_the_dockerfile_copies():
    """The company builds from its repository root (code under src/migration-agent/, deploy/ at the root) with
    root.dockerignore.example copied to the root as .dockerignore."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    rules = _rules(ROOT_IGNORE_EXAMPLE)
    app_dir = "src/migration-agent"
    sources = _context_copies(text, {**_arg_defaults(text), "APP_DIR": app_dir, "DEPLOY_DIR": "deploy"})
    for src in sources:
        assert not _excluded(src, rules), f"the Dockerfile copies {src}, root.dockerignore.example drops it"
    app_files = [f for f in _runtime_files(app_dir + "/") if not f.startswith(app_dir + "/deploy/")]
    deploy_files = [f[len(app_dir) + 1:] for f in _runtime_files(app_dir + "/") if f.startswith(app_dir + "/deploy/")]
    dropped = sorted(f for f in [*app_files, *deploy_files] if _excluded(f, rules))
    assert set(dropped) <= EXCLUDED_ON_PURPOSE, dropped
    # the runtime paths the RUN steps read, at their company-layout places
    for needed in (f"{app_dir}/sbmig/__init__.py", f"{app_dir}/config/default.yml", f"{app_dir}/pyproject.toml",
                   "deploy/docker/run-job.sh", "deploy/k8s/81-web-job.template.yaml"):
        assert not _excluded(needed, rules), needed


@pytest.mark.parametrize("path", [
    "src/migration-agent/agent-doc/README.md",
    "src/migration-agent/CLAUDE.md",
    "src/migration-agent/scripts/handbook/node_modules/pptxgenjs/package.json",
    "src/migration-agent/scripts/handbook/build_kb_map.py",
    "src/migration-agent/out/e2e/summary.md",
    "src/migration-agent/.pytest_cache/v/cache/lastfailed",
    "src/migration-agent/.ruff_cache/CACHEDIR.TAG",
    "src/migration-agent/.mypy_cache/3.11/x.json",
    "src/migration-agent/.coverage",
    "src/migration-agent/.coverage.host.1234",
    "src/migration-agent/htmlcov/index.html",
    "src/migration-agent/sbmig/core/__pycache__/config.cpython-311.pyc",
    "agent-doc/README.md",
    "CLAUDE.md",
    "deploy/k8s/30-secret.yaml",
])
def test_the_company_layout_leaves_out_the_same_folders_at_any_depth(path: str):
    assert _excluded(path, _rules(ROOT_IGNORE_EXAMPLE)), path


def test_the_company_layout_example_keeps_its_older_lines():
    """A repository that copied an older example keeps building as before: the new example only adds lines."""
    lines = {ln.strip() for ln in ROOT_IGNORE_EXAMPLE.read_text(encoding="utf-8").splitlines()}
    for old in (".git", "**/.git", ".venv", "**/.venv", "**/__pycache__", "**/*.pyc", "**/out/", "**/kb-v2/", "**/.env",
                "**/.env.*", "!**/.env.example", "**/*.egg-info", ".scratch", ".ruff_cache", ".pytest_cache",
                "**/.pytest_cache", "**/.ruff_cache", ".vscode", ".github", "tests/", "**/tests/", "docs/", "**/docs/",
                "guide/", "oss/", "agent_files/", "agent-doc/", "**/coding/", "**/examples/", "**/fixtures/",
                "deploy/k8s/30-secret.yaml"):
        assert old in lines, old
