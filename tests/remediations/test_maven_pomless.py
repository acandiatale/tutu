"""v2.1z-ax: the remediation's Maven calls (package, dependency:tree) go through `pomless.run_with_stubs`: a build
that stops on a library whose repository has the jar but no pom gets a minimal pom in the (tmp) local repository and
the same command again; the outcome's log is the last run's (the baseline comparison reads it)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build.mvn import ProcessResult
from sbmig.remediations.maven import ServerMaven

from .test_maven import make_cfg, ok_probe, write_project

DIRECT = (Path(__file__).parents[1] / "build" / "logs" / "real-pomless-direct.log").read_text(encoding="utf-8")


class SeqRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        self.calls.append((list(cmd), Path(log_path)))
        text, code = self.results.pop(0) if self.results else ("[INFO] BUILD SUCCESS\n", 0)
        out = next((a.split("=", 1)[1] for a in cmd if a.startswith("-DoutputFile=")), None)
        if out is not None and code == 0:
            Path(out).write_text("example:root:pom:1.0.0\n", encoding="utf-8")
        return ProcessResult(exit_code=code, text=text, duration_s=0.5)


class FakeRepo:
    def __init__(self, cfg, project_root=None):
        self.project_root = project_root

    def repository_urls(self):
        return [("https://nexus.invalid/releases/", None, "t")]

    def file_answers(self, g, a, v, ext):
        return [("https://nexus.invalid/releases/", "missing" if ext == "pom" else "ok", "")]


@pytest.fixture(autouse=True)
def fake_repo(monkeypatch):
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", FakeRepo)


def _stub(tmp_path) -> Path:
    return tmp_path / "m2" / "com" / "example" / "pomless" / "1.0" / "pomless-1.0.pom"


def test_package_writes_the_stub_and_reports_the_last_log(tmp_path):
    events: list[dict] = []
    runner = SeqRunner([(DIRECT, 1), ("[INFO] BUILD SUCCESS\n", 0)])
    gw = ServerMaven(make_cfg(tmp_path), runner, maven_probe=ok_probe, journal=events.append)
    out = gw.package(write_project(tmp_path / "p"), tmp_path / "logs" / "build.log")
    assert out.ok and len(runner.calls) == 2 and runner.calls[0][0] == runner.calls[1][0]
    assert out.log == (tmp_path / "logs" / "build-pomless1.log").as_posix()
    assert _stub(tmp_path).is_file()
    assert [e["event"] for e in events if e["event"].startswith("pomless")] == ["pomless_stub"]


def test_switch_off_leaves_the_remediation_build_as_before(tmp_path):
    events: list[dict] = []
    runner = SeqRunner([(DIRECT, 1), (DIRECT, 1)])
    gw = ServerMaven(make_cfg(tmp_path, pomless_stub=False), runner, maven_probe=ok_probe, journal=events.append)
    out = gw.package(write_project(tmp_path / "p"), tmp_path / "logs" / "build.log")
    assert not out.ok and not _stub(tmp_path).exists()
    assert "-U" in runner.calls[1][0]                                 # today's -U retry, unchanged
    assert out.log == (tmp_path / "logs" / "build.log").as_posix()


def test_resolve_retries_dependency_tree_after_the_stub(tmp_path):
    runner = SeqRunner([(DIRECT, 1)])
    gw = ServerMaven(make_cfg(tmp_path), runner, maven_probe=ok_probe, journal=lambda e: None)
    gw.resolve(write_project(tmp_path / "p"), tmp_path / "work")
    assert _stub(tmp_path).is_file()
    assert runner.calls[1][1].name == "deptree-1-pomless1.log"
