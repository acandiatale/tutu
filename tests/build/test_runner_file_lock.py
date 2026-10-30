"""v2.1r: a build failing on a Windows file lock under target/ (antivirus / IDE / a lingering JVM holding a jar) is run
again by `MavenRunner.run` after `build.file_lock_retry_wait_s`, up to `build.file_lock_retries` times; the patterns
come from `knowledge/config/build_env_errors.yml` with a built-in fallback. Offline: `run_logged` is stubbed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sbmig.build.runner as runner_mod
from sbmig.build import MavenRunner, env_errors
from sbmig.build.env_errors import BUILTIN_FILE_LOCK, file_lock_hit
from sbmig.build.mvn import ProcessResult
from sbmig.core.workspace import Workspace

JAR = r"C:\work\proj\app\target\app-1.0\WEB-INF\lib\wagon-http-shared-1.0-beta-2.jar"
KO = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) on project "
      "app: Error assembling WAR: Problem creating war: Execution exception: java.nio.file.FileSystemException: "
      f"{JAR}: 다른 프로세스가 파일을 사용 중이기 때문에 프로세스가 파일에 액세스할 수 없습니다 -> [Help 1]\n"
      "[INFO] BUILD FAILURE\n")
CLS = r"C:\work\proj\app\target\classes\com\acme\A.class"
EN = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-clean-plugin:3.3.2:clean (default-clean) on "
      f"project app: Failed to clean project: Failed to delete {CLS}: The process cannot access the file because it "
      "is being used by another process -> [Help 1]\n[INFO] BUILD FAILURE\n")
COMPILE = ("[ERROR] /w/app/src/main/java/A.java:[3,8] cannot find symbol\n[INFO] BUILD FAILURE\n")
OK = "[INFO] BUILD SUCCESS\n"


class _Stub:
    """`run_logged` stand-in: each call returns the next (text, exit code)."""

    def __init__(self, outputs: list[tuple[str, int]]):
        self.outputs = list(outputs)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        self.calls.append(list(cmd))
        text, code = self.outputs.pop(0) if self.outputs else (OK, 0)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(text, encoding="utf-8")
        return ProcessResult(exit_code=code, text=text, duration_s=0.01)


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>")
    return Workspace(root, tmp_path / "run", "t-run")


@pytest.fixture()
def slept(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(runner_mod, "sleep_seconds", waits.append)
    return waits


def _stub(monkeypatch, outputs) -> _Stub:
    s = _Stub(outputs)
    monkeypatch.setattr(runner_mod, "run_logged", s)
    return s


def _events(ws, name: str) -> list[dict]:
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines())
            if e.get("event") == name]


def test_two_locks_then_success(ws, cfg, monkeypatch, slept, caplog):
    stub = _stub(monkeypatch, [(KO, 1), (EN, 1), (OK, 0)])
    with caplog.at_level("INFO", logger="sbmig.build"):
        res = MavenRunner(ws, cfg).run("package")
    assert res.ok and len(stub.calls) == 3 and stub.calls[0] == stub.calls[2]       # the same command again
    assert slept == [5.0, 5.0]
    rows = _events(ws, "build_file_lock_retry")
    assert [(r["attempt"], r["path"], r["wait_s"], r["goal"]) for r in rows] == [(1, JAR, 5.0, "package"),
                                                                                  (2, CLS, 5.0, "package")]
    assert len(_events(ws, "build")) == 3
    assert any(f"Windows file lock on {JAR} → retrying in 5s (1/3)" in r.getMessage() for r in caplog.records)
    assert any(r.getMessage().startswith("maven: 파일 잠김") for r in caplog.records)   # Korean first


def test_exhausted_retries_return_the_last_failure(ws, cfg, monkeypatch, slept):
    cfg.build.file_lock_retries, cfg.build.file_lock_retry_wait_s = 2, 0.5
    stub = _stub(monkeypatch, [(KO, 1), (KO, 1), (EN, 1), (OK, 0)])
    res = MavenRunner(ws, cfg).run("package")
    assert not res.ok and len(stub.calls) == 3 and slept == [0.5, 0.5]
    assert "Failed to clean project" in Path(res.log_path).read_text(encoding="utf-8")   # the last run's log
    assert len(_events(ws, "build_file_lock_retry")) == 2


def test_other_failures_and_the_switch_are_not_retried(ws, cfg, monkeypatch, slept):
    stub = _stub(monkeypatch, [(COMPILE, 1)])
    assert not MavenRunner(ws, cfg).run("package").ok and len(stub.calls) == 1
    cfg.build.file_lock_retries = 0
    stub = _stub(monkeypatch, [(KO, 1), (OK, 0)])
    assert not MavenRunner(ws, cfg).run("package").ok and len(stub.calls) == 1 and slept == []


def test_patterns_from_the_yaml_with_a_builtin_fallback(tmp_path, cfg, monkeypatch):
    env_errors.clear_cache()
    assert file_lock_hit(KO, cfg) == JAR and file_lock_hit(EN, cfg) == CLS
    assert file_lock_hit(COMPILE, cfg) is None
    assert file_lock_hit("java.nio.file.AccessDeniedException: C:\\Users\\u\\.m2\\repository\\x.jar", cfg) is None
    # a company wording added to the YAML
    rules = tmp_path / "knowledge" / "config"
    rules.mkdir(parents=True)
    (rules / "build_env_errors.yml").write_text("file_lock:\n  - 'V3 Lite blocked access to'\n  - '[unclosed'\n",
                                                encoding="utf-8")
    cfg.repo_root = str(tmp_path)
    env_errors.clear_cache()
    assert file_lock_hit("[ERROR] V3 Lite blocked access to C:\\w\\target\\x.jar", cfg) == "C:\\w\\target\\x.jar"
    assert file_lock_hit(KO, cfg) is None                       # the file's list replaces the built-in one
    # missing file → the built-in list
    (rules / "build_env_errors.yml").unlink()
    env_errors.clear_cache()
    assert file_lock_hit(KO, cfg) == JAR and len(env_errors.file_lock_patterns(cfg)) == len(BUILTIN_FILE_LOCK)
    env_errors.clear_cache()
