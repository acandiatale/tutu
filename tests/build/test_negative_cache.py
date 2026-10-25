"""Round 10 (B5): Maven's negative cache ("… was cached in the local repository, resolution will not be reattempted
until the update interval …") kept a run failing for a day after a Nexus admin uploaded the artifact. With
`build.retry_negative_cache` the runner re-runs that build once with -U and returns the second result."""
from __future__ import annotations

import io
import json
import logging

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.build import MavenRunner
from sbmig.build.runner import negative_cache_hits
from sbmig.core.workspace import Workspace

M36 = ("[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project com.acme:app:jar:1.0: "
       "Failure to find com.acme:acme-common:jar:4.0.0 in https://nexus.acme.local/repository/public/ was cached in "
       "the local repository, resolution will not be reattempted until the update interval of nexus has elapsed or "
       "updates are forced -> [Help 1]\n[INFO] BUILD FAILURE\n")
M39 = ("[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project com.acme:app:jar:1.0: "
       "The following artifacts could not be resolved: com.acme:acme-common:jar:4.0.0 (absent): "
       "com.acme:acme-common:jar:4.0.0 was not found in https://nexus.acme.local/repository/public/ during a previous "
       "attempt. This failure was cached in the local repository and resolution is not reattempted until the update "
       "interval of nexus has elapsed or updates are forced -> [Help 1]\n[INFO] BUILD FAILURE\n")
PARENT = ("[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n[FATAL] Non-resolvable parent POM "
          "for com.acme:app:1.0: Failure to find com.acme:base-parent:pom:4.0.0 in https://nexus/ was cached in the "
          "local repository, resolution will not be reattempted until the update interval of nexus has elapsed or "
          "updates are forced and 'parent.relativePath' points at no local POM @ line 5, column 11\n")
TRANSFER = ("[ERROR] Plugin org.acme:acme-maven-plugin:1.2 or one of its dependencies could not be resolved: Failure to "
            "transfer org.acme:acme-maven-plugin:pom:1.2 from https://nexus/ was cached in the local repository, "
            "resolution will not be reattempted until the update interval of nexus has elapsed or updates are forced. "
            "Original error: Could not transfer artifact org.acme:acme-maven-plugin:pom:1.2\n")
REAL_MISS = ("[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project "
             "com.acme:app:jar:1.0: Could not find artifact com.acme:nope:jar:1.0 in nexus (https://nexus/)\n"
             "[INFO] BUILD FAILURE\n")
OK = "[INFO] BUILD SUCCESS\n"


def test_negative_cache_hits_both_maven_wordings():
    assert negative_cache_hits(M36) == ["com.acme:acme-common:jar:4.0.0"]
    assert negative_cache_hits(M39) == ["com.acme:acme-common:jar:4.0.0"]
    assert negative_cache_hits(PARENT) == ["com.acme:base-parent:pom:4.0.0"]
    assert negative_cache_hits(TRANSFER) == ["org.acme:acme-maven-plugin:pom:1.2"]
    assert negative_cache_hits(REAL_MISS) == [] and negative_cache_hits("") == []
    assert negative_cache_hits("[ERROR] x was cached in the local repository, resolution will not be reattempted") \
        == ["?"]


class SeqPopen:
    """Fake Maven process: each start consumes the next output."""
    outputs: list[tuple[str, int]] = []
    calls: list[list[str]] = []

    def __init__(self, cmd, **kw):
        SeqPopen.calls.append(list(cmd))
        text, self._code = SeqPopen.outputs.pop(0) if SeqPopen.outputs else (OK, 0)
        self.stdout = io.BytesIO(text.encode("utf-8"))
        self.returncode = None
        self.pid = -424243

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode


@pytest.fixture()
def seq(monkeypatch):
    SeqPopen.outputs, SeqPopen.calls = [], []
    monkeypatch.setattr(mvn_mod, "_popen", SeqPopen)
    return SeqPopen


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>")
    return Workspace(root, tmp_path / "run", "t-run")


def _events(ws):
    return [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("first", [M36, M39])
def test_retry_once_with_force_update_returns_second_result(ws, cfg, seq, caplog, first):
    seq.outputs = [(first, 1), (OK, 0)]
    with caplog.at_level(logging.INFO, logger="sbmig.build"):
        res = MavenRunner(ws, cfg).run("compile", label="spring-1")
    assert res.ok and res.errors == []
    assert len(seq.calls) == 2 and "-U" not in seq.calls[0] and seq.calls[1].count("-U") == 1
    assert [a for a in seq.calls[1] if a != "-U"] == seq.calls[0]              # the same build, plus -U
    assert res.command == seq.calls[1] and res.log_path.endswith("002-build-spring-1-U.log")
    assert ("maven: negative cache hit for com.acme:acme-common:jar:4.0.0 → retrying once with -U"
            in caplog.messages)
    ev = [e["event"] for e in _events(ws)]
    assert ev == ["build", "build_negative_cache_retry", "build"]


def test_retry_failing_again_returns_second_failure_and_never_loops(ws, cfg, seq):
    seq.outputs = [(M39, 1), (REAL_MISS, 1), (M39, 1)]
    r = MavenRunner(ws, cfg)
    res = r.run("compile")
    assert not res.ok and len(seq.calls) == 2
    assert res.errors[0].artifact.coordinates == "com.acme:nope:1.0"           # the second build's errors
    again = MavenRunner(ws, cfg).run("compile")                                # same artifact later in the run
    assert not again.ok and len(seq.calls) == 3                                # no second -U for it


def test_no_retry_when_disabled_offline_or_already_forced(ws, cfg, seq):
    cfg.build.retry_negative_cache = False
    seq.outputs = [(M36, 1)]
    assert not MavenRunner(ws, cfg).run("compile").ok and len(seq.calls) == 1
    cfg.build.retry_negative_cache = True
    cfg.build.offline = True
    seq.outputs = [(M36, 1)]
    MavenRunner(ws, cfg).run("compile")
    assert len(seq.calls) == 2
    cfg.build.offline = False
    seq.outputs = [(M36, 1)]
    MavenRunner(ws, cfg).run("compile", extra_args=["-U"])
    assert len(seq.calls) == 3


def test_no_retry_for_other_failures_or_success(ws, cfg, seq):
    seq.outputs = [(REAL_MISS, 1), (OK, 0)]
    r = MavenRunner(ws, cfg)
    assert not r.run("compile").ok and len(seq.calls) == 1
    assert r.run("compile").ok and len(seq.calls) == 2
