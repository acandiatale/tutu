"""ScriptRunner (sbmig.script.runner): the `python -I` child process end to end — results, errors, timeouts, the
runtime guards behind the static check, and what the parent process keeps untouched."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sbmig.core.config import Config
from sbmig.core.interfaces import ScriptRunnerLike
from sbmig.core.models import GAV, BuildError, ErrorKind
from sbmig.core.workspace import Workspace
from sbmig.script import ScriptRunner
from sbmig.script import runner as runner_mod
from sbmig.script.check import import_allowed
from sbmig.script.harness import guarded_import


def make_cfg(**script) -> Config:
    cfg = Config(artifacts={"parent": {"group_id": "g", "artifact_id": "p"}})
    for k, v in script.items():
        setattr(cfg.script, k, v)
    return cfg


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src-project"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text("<project>\n  <modelVersion>4.0.0</modelVersion>\n</project>\n")
    (src / "src/main/java/com/x/A.java").write_bytes(
        "import javax.annotation.PostConstruct;\r\n// 한글\r\nclass A {}\r\n".encode("cp949"))
    (src / "src/main/java/com/x/B.java").write_text("import javax.annotation.PreDestroy;\nclass B {}\n")
    w = Workspace.from_input(src, tmp_path / "out", run_id="script-test")
    w.git_init()
    return w


@pytest.fixture()
def runner() -> ScriptRunner:
    return ScriptRunner(make_cfg())


def test_is_a_script_runner(runner):
    assert isinstance(runner, ScriptRunnerLike)


def test_ok_run_changes_files_and_keeps_encoding(ws, runner):
    code = """
# sbmig-fix
def fix(p):
    for path in p.files("**/*.java"):
        n = p.replace(path, "javax.annotation", "jakarta.annotation")
        if n:
            p.log(f"{path}: {n} import(s)")
"""
    head = ws.head()
    res = runner.run(ws, code)
    assert res.status == "ok", res.error
    assert res.changed_files == ["src/main/java/com/x/A.java", "src/main/java/com/x/B.java"]
    assert sorted(ws.changed_files_since(head)) == res.changed_files      # the script's account matches git
    assert res.log == ["src/main/java/com/x/A.java: 1 import(s)", "src/main/java/com/x/B.java: 1 import(s)"]
    assert res.error == "" and res.problems == [] and 0 < res.duration_s < 10
    raw = (ws.root / "src/main/java/com/x/A.java").read_bytes()
    assert raw == "import jakarta.annotation.PostConstruct;\r\n// 한글\r\nclass A {}\r\n".encode("cp949")
    assert ws.is_dirty()                                   # uncommitted: the caller commits or restores


def test_p_errors_carry_the_contract_keys(ws, runner):
    errors = [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/main/java/com/x/A.java",
                         line=3, column=5, symbol="class PostConstruct", location="class A", module="demo",
                         signature="L1", signature_generic="L2"),
              BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="could not resolve",
                         artifact=GAV(group_id="g", artifact_id="a", version="1"))]
    code = """
import json
def fix(p):
    p.log(json.dumps(p.errors, sort_keys=True))
"""
    res = runner.run(ws, code, errors=errors)
    assert res.status == "ok", res.error
    import json
    got = json.loads(res.log[0])
    assert got[0] == {"file": "src/main/java/com/x/A.java", "line": 3, "column": 5, "message": "cannot find symbol",
                      "symbol": "class PostConstruct", "location": "class A", "kind": "compile", "module": "demo",
                      "signature": "L1", "signature_generic": "L2", "artifact": None}
    assert got[1]["kind"] == "dependency_resolution" and got[1]["artifact"] == "g:a:1"


def test_exception_is_status_error_with_script_frames_only(ws, runner):
    code = """
def helper(p):
    return p.errors[0]["nope"]

def fix(p):
    p.write("src/main/java/com/x/B.java", "class B { }\\n")
    p.log("before the crash")
    helper(p)
"""
    res = runner.run(ws, code, errors=[BuildError(kind=ErrorKind.COMPILE, message="m")])
    assert res.status == "error"
    first, *rest = res.error.splitlines()
    assert first == "KeyError: 'nope' (line 3)"
    assert rest == ["script traceback:", "  line 8, in fix: helper(p)",
                    "  line 3, in helper: return p.errors[0][\"nope\"]"]
    assert "harness" not in res.error and "api.py" not in res.error
    assert res.changed_files == ["src/main/java/com/x/B.java"]          # partial writes are reported
    assert res.log == ["before the crash"]


def test_path_refusal_is_reported_even_when_caught(ws, runner):
    code = """
def fix(p):
    try:
        p.write("../escape.txt", "x")
    except Exception:
        p.log("swallowed")
"""
    res = runner.run(ws, code)
    assert res.status == "error"
    assert res.error.startswith("PathRefused: path '../escape.txt' refused: it leaves the project folder")
    assert "(caught by the script)" in res.error and res.log == ["swallowed"]
    assert not (ws.root.parent / "escape.txt").exists()


@pytest.mark.parametrize("bad", ["C:/x.txt", "/tmp/x.txt", ".git/config", "target/x.txt"])
def test_path_escapes_through_the_runner(ws, runner, bad):
    (ws.root / "target").mkdir(exist_ok=True)
    res = runner.run(ws, f"def fix(p):\n    p.write({bad!r}, 'x')\n")
    assert res.status == "error" and repr(bad) in res.error and res.changed_files == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_symlink_escape_through_the_runner(ws, runner, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (ws.root / "link.txt").symlink_to(outside)
    res = runner.run(ws, "def fix(p):\n    p.write('link.txt', 'gone')\n")
    assert res.status == "error" and "outside the project folder" in res.error
    assert outside.read_text() == "keep\n"


def test_timeout_kills_the_process(ws, runner):
    code = """
def fix(p):
    p.write("src/main/java/com/x/B.java", "class B { }\\n")
    p.log("looping")
    while True:
        pass
"""
    t0 = time.monotonic()
    res = runner.run(ws, code, timeout_s=1)
    assert time.monotonic() - t0 < 1 + 8
    assert res.status == "timeout" and res.error == "timed out after 1s (process tree killed)"
    assert res.changed_files == ["src/main/java/com/x/B.java"] and res.log == ["looping"]   # from the journal


def test_timeout_default_comes_from_config(ws):
    res = ScriptRunner(make_cfg(timeout_seconds=1)).run(ws, "def fix(p):\n    while True:\n        pass\n")
    assert res.status == "timeout"


def test_print_goes_to_the_log_and_the_log_is_capped(ws):
    code = """
def fix(p):
    print("hello", 42)
    for i in range(50):
        p.log(f"line {i}")
"""
    res = ScriptRunner(make_cfg(log_lines=10)).run(ws, code)
    assert res.status == "ok", res.error
    assert len(res.log) == 10
    assert res.log[:2] == ["hello 42", "line 0"] and res.log[-1] == "… 42 more log line(s) not kept"


def test_rejected_script_never_starts_a_process(ws, runner, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a process was started")
    monkeypatch.setattr(runner_mod.subprocess, "Popen", boom)
    res = runner.run(ws, "import os\ndef fix(p):\n    os.remove('pom.xml')\n")
    assert res.status == "rejected"
    assert res.problems == ["line 1: import of 'os' is not allowed"]
    assert res.error == "rejected by the static check: line 1: import of 'os' is not allowed"
    assert (ws.root / "pom.xml").exists()
    assert runner.check("def fix(p):\n    pass\n") == []


def test_extra_allowed_imports_reach_the_child(ws):
    code = "import datetime\ndef fix(p):\n    p.log(str(datetime.date(2026, 10, 26)))\n"
    assert ScriptRunner(make_cfg()).run(ws, code).status == "rejected"
    res = ScriptRunner(make_cfg(extra_allowed_imports=["datetime"])).run(ws, code)
    assert res.status == "ok" and res.log == ["2026-10-26"]


@pytest.mark.parametrize("body, expected", [
    ("import sbmig", "ImportError: import of 'sbmig' is not allowed in a fix script"),
    ("import os", "ImportError: import of 'os' is not allowed in a fix script"),
    ("__import__('subprocess')", "ImportError: import of 'subprocess' is not allowed in a fix script"),
    ("open('pom.xml')", "NameError: name 'open' is not defined"),
    ("eval('1')", "NameError: name 'eval' is not defined"),
    ("type(p)", "NameError: name 'type' is not defined"),
])
def test_runtime_guards_hold_without_the_static_check(ws, runner, monkeypatch, body, expected):
    monkeypatch.setattr(runner, "check", lambda code: [])
    res = runner.run(ws, f"def fix(p):\n    {body}\n")
    assert res.status == "error" and res.error.startswith(expected), res.error


def test_guarded_import_function():
    imp = guarded_import(import_allowed, ())
    assert imp("json").__name__ == "json"
    assert imp("collections.abc", fromlist=("Mapping",)).__name__ == "collections.abc"
    for bad in ("sbmig", "sbmig.script.api", "os", "importlib"):
        with pytest.raises(ImportError):
            imp(bad)
    with pytest.raises(ImportError):
        imp("json", level=1)


def test_classes_generators_and_main_guard_work(ws, runner):
    code = """
import re
from collections import Counter

class Rule:
    def __init__(self, old, new):
        self.old, self.new = old, new

RULES = [Rule("javax.annotation", "jakarta.annotation")]

def fix(p):
    if __name__ == "__main__":
        raise RuntimeError("must not run as main")
    seen = Counter()
    for path in p.files("**/*.java"):
        for r in RULES:
            seen[path] += p.replace(path, r.old, r.new)
        yield path
    p.log(f"{sum(seen.values())} replacement(s)")
"""
    res = runner.run(ws, code)
    assert res.status == "ok", res.error
    assert res.log == ["2 replacement(s)"] and len(res.changed_files) == 2


def test_pom_edit_through_the_runner(ws, runner):
    code = """
def fix(p):
    pom = p.pom()
    pom.add_dependency("jakarta.annotation", "jakarta.annotation-api", "3.0.0")
    pom.save()
"""
    res = runner.run(ws, code)
    assert res.status == "ok" and res.changed_files == ["pom.xml"]
    assert "<artifactId>jakarta.annotation-api</artifactId>" in (ws.root / "pom.xml").read_text()


def test_parent_cwd_sys_path_and_environment_unchanged(ws, runner, tmp_path):
    cwd, path, modules = os.getcwd(), list(sys.path), set(sys.modules)
    res = runner.run(ws, "def fix(p):\n    p.log('x')\n")
    assert res.status == "ok"
    assert os.getcwd() == cwd and sys.path == path
    assert not any(m.startswith("sbmig.script.harness") for m in set(sys.modules) - modules)
    assert not list(ws.root.glob("payload.json")) and not list(ws.root.glob("fix_script.py"))


def test_secrets_are_not_passed_to_the_child(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SOME_TOKEN", "t")
    monkeypatch.setenv("PATH_LIKE_SETTING", "keep")
    env = runner_mod._child_env()
    assert "LLM_API_KEY" not in env and "SOME_TOKEN" not in env and env["PATH_LIKE_SETTING"] == "keep"


def test_crashed_harness_is_an_error_with_the_stderr_tail(ws, runner, monkeypatch, tmp_path):
    fake = tmp_path / "fake_harness.py"
    fake.write_text("import sys\nsys.stderr.write('boom: harness broke\\n')\nsys.exit(7)\n")
    monkeypatch.setattr(runner_mod, "HARNESS", fake)
    res = runner.run(ws, "def fix(p):\n    pass\n")
    assert res.status == "error"
    assert res.error.startswith("script process ended without a result (exit code 7)")
    assert "boom: harness broke" in res.error


def test_missing_interpreter_is_an_error_not_an_exception(ws, tmp_path):
    res = ScriptRunner(make_cfg(python=str(tmp_path / "no-such-python"))).run(ws, "def fix(p):\n    pass\n")
    assert res.status == "error" and res.error.startswith("cannot start ") and "no-such-python" in res.error


def test_internal_failure_never_raises(ws, runner, monkeypatch):
    monkeypatch.setattr(runner, "_execute", lambda *a, **kw: 1 / 0)
    res = runner.run(ws, "def fix(p):\n    pass\n")
    assert res.status == "error" and res.error.startswith("sbmig script runner failed: ZeroDivisionError")


def test_one_info_line_per_run(ws, runner, caplog):
    with caplog.at_level("INFO", logger="sbmig.script"):
        runner.run(ws, "def fix(p):\n    raise ValueError('bad input')\n")
    lines = [r.getMessage() for r in caplog.records if r.name == "sbmig.script"]
    assert len(lines) == 1
    assert lines[0].startswith("fix script error in ") and "0 file(s) changed — ValueError: bad input (line 2)" \
        in lines[0]


def test_harness_runs_under_isolated_mode_from_any_cwd(tmp_path):
    """The harness file needs neither the cwd nor PYTHONPATH: `-I` from an unrelated directory works."""
    payload_dir = tmp_path / "elsewhere"
    payload_dir.mkdir()
    (payload_dir / "json.py").write_text("raise SystemExit('shadowed json imported')\n")   # never picked up
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.txt").write_text("a\n")
    code = payload_dir / "fix_script.py"
    code.write_text("def fix(p):\n    p.write('a.txt', 'b\\n')\n")
    import json
    payload = {"root": str(proj), "code_path": str(code), "result_path": str(payload_dir / "r.json"),
               "journal_path": str(payload_dir / "j.jsonl"), "errors": [], "encoding_candidates": ["utf-8"],
               "log_lines": 5, "extra_imports": [], "preload": ["re"], "sbmig_root": str(runner_mod.SBMIG_ROOT)}
    (payload_dir / "payload.json").write_text(json.dumps(payload))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-I", "-X", "utf8", str(runner_mod.HARNESS),
                           str(payload_dir / "payload.json")], cwd=payload_dir, env=env, capture_output=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert json.loads((payload_dir / "r.json").read_text())["changed_files"] == ["a.txt"]
    assert (proj / "a.txt").read_text() == "b\n"


def test_process_group_settings_per_platform(monkeypatch):
    monkeypatch.setattr(runner_mod, "IS_WINDOWS", False)
    assert runner_mod._popen_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(runner_mod, "IS_WINDOWS", True)
    flags = runner_mod._popen_kwargs()["creationflags"]
    assert flags & 0x200 and flags & 0x08000000          # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def test_child_command_isolated_and_outside_the_project(ws, runner, monkeypatch):
    seen: dict = {}
    real_popen = subprocess.Popen

    def spy(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"], env=kw["env"])
        return real_popen(cmd, **kw)
    monkeypatch.setattr(runner_mod.subprocess, "Popen", spy)
    assert runner.run(ws, "def fix(p):\n    pass\n").status == "ok"
    assert seen["cmd"][:4] == [sys.executable, "-I", "-X", "utf8"] and seen["cmd"][4] == str(runner_mod.HARNESS)
    cwd = Path(seen["cwd"]).resolve()
    assert ws.root not in cwd.parents and cwd != ws.root and not cwd.exists()     # temp dir, removed afterwards
