"""v2.1z-ax: every Maven call of `sbmig.build` that resolves dependencies goes through `pomless.run_with_stubs` —
`MavenRunner.run`, the effective-pom capture, `dependency:tree`, `dependency:build-classpath`. Maven itself is a
fake process (`mvn._popen`) that prints the REAL captured descriptor failure first; the repository is a fake lookup;
`build.local_repo` is a tmp folder (nothing reaches `~/.m2`)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.build import MavenRunner
from sbmig.core.workspace import Workspace

DIRECT = (Path(__file__).parent / "logs" / "real-pomless-direct.log").read_text(encoding="utf-8")
OK = "[INFO] BUILD SUCCESS\n"


class SeqPopen:
    outputs: list[tuple[str, int]] = []
    calls: list[list[str]] = []

    def __init__(self, cmd, **kw):
        SeqPopen.calls.append(list(cmd))
        text, self._code = SeqPopen.outputs.pop(0) if SeqPopen.outputs else (OK, 0)
        self.stdout = io.BytesIO(text.encode("utf-8"))
        self.returncode = None
        self.pid = -424245

    def wait(self, timeout=None):
        self.returncode = self._code
        return self._code

    def poll(self):
        return self.returncode


class FakeRepo:
    roots: list = []

    def __init__(self, cfg, project_root=None):
        FakeRepo.roots.append(project_root)

    def repository_urls(self):
        return [("https://nexus.invalid/releases/", None, "t"), ("https://nexus.invalid/central/", None, "t")]

    def file_answers(self, g, a, v, ext):
        if ext == "pom":
            return [("https://nexus.invalid/releases/", "missing", ""), ("https://nexus.invalid/central/", "failed", "")]
        return [("https://nexus.invalid/releases/", "ok", "")]


@pytest.fixture()
def seq(monkeypatch):
    SeqPopen.outputs, SeqPopen.calls = [], []
    FakeRepo.roots = []
    monkeypatch.setattr(mvn_mod, "_popen", SeqPopen)
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", FakeRepo)
    return SeqPopen


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>")
    return Workspace(root, tmp_path / "run", "t-run")


@pytest.fixture()
def lcfg(cfg, tmp_path):
    cfg.build.local_repo = str(tmp_path / "m2")
    return cfg


def _events(ws):
    return [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines()]


def _stub(tmp_path) -> Path:
    return tmp_path / "m2" / "com" / "example" / "pomless" / "1.0" / "pomless-1.0.pom"


def test_maven_runner_writes_the_stub_and_runs_the_same_build_again(ws, lcfg, seq, tmp_path):
    seq.outputs = [(DIRECT, 1), (OK, 0)]
    res = MavenRunner(ws, lcfg).run("clean package", label="verify")
    assert res.ok and len(seq.calls) == 2 and seq.calls[0] == seq.calls[1]       # the same command, no -U
    assert "-U" not in seq.calls[1] and res.log_path.endswith("build-verify-pomless1.log")
    assert _stub(tmp_path).is_file()
    assert [e["event"] for e in _events(ws)] == ["build", "pomless_stub", "build"]
    assert FakeRepo.roots == [ws.root]                                            # the project poms' <repositories> join


def test_switch_off_keeps_todays_behaviour(ws, lcfg, seq, tmp_path):
    """With `build.pomless_stub: false` the build runs as before: the descriptor line is a -U candidate today."""
    lcfg.build.pomless_stub = False
    seq.outputs = [(DIRECT, 1), (DIRECT, 1)]
    res = MavenRunner(ws, lcfg).run("clean package", label="verify")
    assert not res.ok and len(seq.calls) == 2 and "-U" in seq.calls[1]
    assert not _stub(tmp_path).exists() and FakeRepo.roots == []
    assert [e["event"] for e in _events(ws)] == ["build", "pomless_check", "build_negative_cache_retry", "build"]


def test_pomless_comes_before_the_force_update_retry(ws, lcfg, seq, monkeypatch):
    """A library the proof refuses (jar nowhere) leaves the run to the -U retry exactly as today."""
    class NoJar(FakeRepo):
        def file_answers(self, g, a, v, ext):
            return [("https://nexus.invalid/releases/", "missing", "")]
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", NoJar)
    seq.outputs = [(DIRECT, 1), (DIRECT, 1)]
    res = MavenRunner(ws, lcfg).run("clean package")
    assert not res.ok and len(seq.calls) == 2 and "-U" in seq.calls[1]
    assert [e.get("reason") for e in _events(ws) if e["event"] == "pomless_check"] == ["jar-unavailable"]


def test_dependency_tree_and_classpath_and_effective_pom_go_through_it(ws, lcfg, seq, tmp_path):
    from sbmig.build import classpath, deptree, effective
    seq.outputs = [(DIRECT, 1), (OK, 0)]
    pr, log_path = deptree._run_tree(ws, lcfg, "", "root", tmp_path / "tree.txt", [], package=False)
    assert pr.exit_code == 0 and log_path.name.endswith("-pomless1.log") and len(seq.calls) == 2
    assert _stub(tmp_path).is_file()
    # the library is handled once per run: a second failure on it is not retried again
    seq.outputs = [(DIRECT, 1)]
    pr, _lp = classpath._run_build_classpath(ws, lcfg, "", "root", tmp_path / "cp.txt", package=False)
    assert pr.exit_code == 1 and len(seq.calls) == 3
    # a fresh run (new workspace memo) on another library: the effective-pom capture retries too
    ws2 = Workspace(ws.root, tmp_path / "run2", "t-run2")
    seq.outputs = [(DIRECT.replace("pomless", "other"), 1), (OK, 0)]
    effective.capture_effective_depmgmt(ws2, lcfg)
    assert len(seq.calls) == 5
    assert [e["event"] for e in _events(ws2) if e["event"].startswith("pomless")] == ["pomless_stub"]


def test_with_the_offline_test_fake_nothing_is_written(ws, cfg, monkeypatch, tmp_path):
    """The suite's default: `tests/conftest.py`'s offline RepositoryLookup → `no-repository`, no file, no retry."""
    SeqPopen.outputs, SeqPopen.calls = [(DIRECT, 1), (DIRECT, 1)], []
    monkeypatch.setattr(mvn_mod, "_popen", SeqPopen)
    cfg.build.local_repo = str(tmp_path / "m2")
    MavenRunner(ws, cfg).run("clean package")
    assert not (tmp_path / "m2").exists()
    assert [e.get("reason") for e in _events(ws) if e["event"] == "pomless_check"] == ["no-repository"]
