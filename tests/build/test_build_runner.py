from __future__ import annotations

import io
import json
import os
import stat
import time
from pathlib import Path

import pytest

import sbmig.build.mvn as mvn_mod
from sbmig.build import MavenRunner, build_summary, local_repo_path, maven_available
from sbmig.core.models import ErrorKind
from sbmig.core.workspace import Workspace

LOGS = Path(__file__).parent / "logs"
JDK21 = Path(os.environ.get("SBMIG_TEST_JAVA21_HOME",
                            "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"))


class FakePopen:
    calls: list[dict] = []
    output: bytes = b"[INFO] BUILD SUCCESS\n"
    code: int = 0

    def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None, stdin=None, start_new_session=False, **kw):
        FakePopen.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "start_new_session": start_new_session,
                                "stderr": stderr})
        self.stdout = io.BytesIO(FakePopen.output)
        self.returncode = None
        self.pid = -424242

    def wait(self, timeout=None):
        self.returncode = FakePopen.code
        return self.returncode


@pytest.fixture()
def fake(monkeypatch):
    FakePopen.calls = []
    FakePopen.output = b"[INFO] BUILD SUCCESS\n"
    FakePopen.code = 0
    monkeypatch.setattr(mvn_mod, "_popen", FakePopen)
    return FakePopen


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    (root / "svc").mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>")
    (root / "svc" / "pom.xml").write_text("<project/>")
    return Workspace(root, tmp_path / "run", "t-run")


def journal(ws: Workspace) -> list[dict]:
    return [json.loads(line) for line in ws.journal_path.read_text().splitlines()]


def script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


# ------------------------------------------------------------------------------------------------ composition
def test_command_env_and_cwd_composition(ws, cfg, fake, tmp_path):
    cfg.build.settings_file = "/etc/m2/settings.xml"
    cfg.build.local_repo = str(tmp_path / "repo")
    cfg.build.offline = True
    cfg.build.java_home = "/opt/jdk21"
    cfg.build.env = {"FOO": "bar"}
    res = MavenRunner(ws, cfg).run("clean package", extra_args=["-pl", "core"], label="verify")
    call = fake.calls[0]
    assert call["cmd"] == ["mvn", "-B", "-ntp", "-Dstyle.color=never", "-s", "/etc/m2/settings.xml",
                           f"-Dmaven.repo.local={tmp_path / 'repo'}", "-o", "clean", "package", "-pl", "core",
                           "-DskipTests"]
    assert res.command == call["cmd"]
    assert call["cwd"] == str(ws.root)
    assert call["start_new_session"] is True
    assert call["stderr"] is mvn_mod.subprocess.STDOUT
    assert call["env"]["JAVA_HOME"] == "/opt/jdk21"
    assert call["env"]["PATH"].startswith("/opt/jdk21/bin" + os.pathsep)
    assert call["env"]["FOO"] == "bar"
    assert res.ok and res.exit_code == 0 and res.errors == []
    assert Path(res.log_path).name == "001-build-verify.log"
    assert "BUILD SUCCESS" in Path(res.log_path).read_text()
    ev = journal(ws)[-1]
    assert {k: ev[k] for k in ("event", "goal", "ok", "exit_code", "error_count", "log")} == {
        "event": "build", "goal": "clean package", "ok": True, "exit_code": 0, "error_count": 0,
        "log": res.log_path}
    assert "duration_s" in ev


def test_blank_java_home_and_skip_flags(ws, cfg, fake):
    cfg.build.java_home = ""
    r = MavenRunner(ws, cfg)
    assert r.command("compile")[-1] == "-DskipTests"
    cmd = r.command("package", skip_test_compile=True)
    assert cmd[-1] == "-Dmaven.test.skip=true" and "-DskipTests" not in cmd
    cfg.build.skip_tests = False
    assert r.command("compile") == ["mvn", "-B", "-ntp", "-Dstyle.color=never", "compile"]
    r.run("compile")
    assert fake.calls[0]["env"].get("JAVA_HOME") == os.environ.get("JAVA_HOME")


def test_skip_tests_flag(ws, cfg, fake):
    """Round 8 tests-skipped fallback: `skip_tests=True` → -DskipTests even with build.skip_tests off; never together
    with -Dmaven.test.skip=true (skip_test_compile wins)."""
    cfg.build.java_home = ""
    cfg.build.skip_tests = False
    r = MavenRunner(ws, cfg)
    assert "-DskipTests" not in r.command("package")
    cmd = r.command("package", skip_tests=True)
    assert cmd[-1] == "-DskipTests" and cmd.count("-DskipTests") == 1 and "-Dmaven.test.skip=true" not in cmd
    both = r.command("package", skip_tests=True, skip_test_compile=True)
    assert both[-1] == "-Dmaven.test.skip=true" and "-DskipTests" not in both
    cfg.build.skip_tests = True                          # already on: no duplicate flag
    assert r.command("package", skip_tests=True).count("-DskipTests") == 1
    cfg.build.skip_tests = False
    res = r.run("clean package", label="verify", skip_tests=True)
    assert fake.calls[-1]["cmd"][-1] == "-DskipTests" and res.command[-1] == "-DskipTests"
    ev = journal(ws)[-1]
    assert ev["skip_tests"] is True and ev["skip_test_compile"] is False


def test_module_dir_and_module_pom(ws, cfg, fake):
    r = MavenRunner(ws, cfg)
    r.run("compile", module="svc")
    assert fake.calls[-1]["cwd"] == str(ws.root / "svc")
    r.run("compile", module="svc/pom.xml")
    assert fake.calls[-1]["cwd"] == str(ws.root / "svc")
    assert fake.calls[-1]["cmd"][4:7] == ["compile", "-f", "pom.xml"]
    assert Path(r.run("compile").log_path).name == "003-build-compile.log"


# ------------------------------------------------------------------------------------------------ ok detection
@pytest.mark.parametrize("output,code,ok", [
    (b"[INFO] BUILD SUCCESS\n", 0, True),
    (b"[INFO] something\n", 0, False),              # exit 0 but no BUILD SUCCESS (e.g. killed wrapper)
    (b"[INFO] BUILD SUCCESS\n", 1, False),
])
def test_ok_detection(ws, cfg, fake, output, code, ok):
    fake.output, fake.code = output, code
    res = MavenRunner(ws, cfg).run("compile")
    assert res.ok is ok
    if not ok:
        assert res.errors and res.errors[0].kind == ErrorKind.UNKNOWN


def test_failed_build_parses_errors_relative_to_ws_root(ws, cfg, fake):
    text = (LOGS / "real-multimodule-failure.log").read_text().replace("/work/proj", str(ws.root))
    fake.output, fake.code = text.encode(), 1
    res = MavenRunner(ws, cfg).run("compile", label="jdk21-1")
    assert not res.ok and res.exit_code == 1
    assert [e.file for e in res.errors] == ["api/src/main/java/com/example/api/Api.java"] * 2
    assert res.modules_built == ["multi-parent", "core"] and res.failed_module == "api"
    assert str(ws.root) not in res.summary
    assert "[ERROR] api/src/main/java/com/example/api/Api.java:[4,24] cannot find symbol" in res.summary
    assert "--- tail ---" in res.summary
    assert journal(ws)[-1]["error_count"] == 2
    assert res.signature_multiset() == {"compile|cannot_find_symbol|class Missing|package *": 1,
                                        "compile|cannot_find_symbol|method nme()|class *": 1}


def test_summary_caps_error_lines():
    text = "\n".join(f"[ERROR] e{i}" for i in range(80)) + "\n[ERROR] e1\n[INFO] BUILD FAILURE\n"
    s = build_summary(text, None)
    assert s.count("[ERROR] e1\n") >= 1 and "... (20 more [ERROR] lines)" in s
    assert s.splitlines()[-1] == "[INFO] BUILD FAILURE"


# ------------------------------------------------------------------------------------------------ real processes
def test_missing_executable(ws, cfg):
    cfg.build.mvn = "/nonexistent/bin/mvn"
    res = MavenRunner(ws, cfg).run("compile")
    assert not res.ok and res.exit_code == 127
    assert res.errors[0].kind == ErrorKind.UNKNOWN and "cannot start" in res.errors[0].message


def test_timeout_kills_process_group_and_keeps_log(ws, cfg, tmp_path):
    marker = tmp_path / "child-alive"
    cfg.build.mvn = script(tmp_path, "slow-mvn",
                           f'echo "[INFO] Scanning for projects..."\n(sleep 3; touch {marker}) &\nsleep 30\n')
    t0 = time.monotonic()
    res = MavenRunner(ws, cfg, timeout_s=1).run("compile")
    assert time.monotonic() - t0 < 15
    assert res.timed_out and res.exit_code == -1 and not res.ok
    assert res.errors[-1].kind == ErrorKind.UNKNOWN and "timed out" in res.errors[-1].message
    log_text = Path(res.log_path).read_text()
    assert "[INFO] Scanning for projects..." in log_text and "killed after timeout" in log_text
    time.sleep(3.5)
    assert not marker.exists(), "background child survived: process group was not killed"
    assert journal(ws)[-1]["timed_out"] is True


def test_ansi_is_stripped_from_log(ws, cfg, tmp_path):
    cfg.build.mvn = script(tmp_path, "color-mvn", "printf '\\033[1;32m[INFO] BUILD SUCCESS\\033[0m\\n'\n")
    res = MavenRunner(ws, cfg).run("compile")
    assert res.ok
    assert "\x1b" not in Path(res.log_path).read_text()


def test_maven_available(cfg, tmp_path):
    cfg.build.mvn = script(tmp_path, "mvn-ok", 'echo "Apache Maven 3.9.10 (abc)"\n'
                                               'echo "Java version: 21.0.1, vendor: Amazon.com Inc."\n')
    assert maven_available(cfg) == (True, "Apache Maven 3.9.10 (abc); Java version: 21.0.1")
    cfg.build.mvn = script(tmp_path, "mvn-bad", 'echo "boom" >&2\nexit 3\n')
    assert maven_available(cfg) == (False, "boom")
    cfg.build.mvn = str(tmp_path / "missing")
    ok, msg = maven_available(cfg)
    assert not ok and "not found" in msg


def test_local_repo_path(cfg, tmp_path, monkeypatch):
    cfg.build.local_repo = str(tmp_path / "r1")
    assert local_repo_path(cfg) == tmp_path / "r1"
    cfg.build.local_repo = None
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings><localRepository>/data/m2</localRepository></settings>")
    cfg.build.settings_file = str(settings)
    assert local_repo_path(cfg) == Path("/data/m2")
    cfg.build.settings_file = None
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert local_repo_path(cfg) == tmp_path / "home" / ".m2" / "repository"


# ------------------------------------------------------------------------------------------------ integration
POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>com.example</groupId><artifactId>{aid}</artifactId><version>1.0.0</version>
<properties><maven.compiler.release>21</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin></plugins></build>
</project>
"""


def _real_ws(tmp_path: Path, aid: str, files: dict[str, str]) -> Workspace:
    root = tmp_path / aid
    for rel, content in {"pom.xml": POM.format(aid=aid), **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    return Workspace(root, tmp_path / f"run-{aid}", f"it-{aid}")


@pytest.fixture()
def real_cfg(cfg):
    if not JDK21.exists():
        pytest.skip("JDK 21 not available")
    ok, msg = maven_available(cfg)
    if not ok:
        pytest.skip(f"mvn not available: {msg}")
    cfg.build.java_home = str(JDK21)
    return cfg


@pytest.mark.integration
def test_real_mvn_compile_errors(tmp_path, real_cfg):
    ws = _real_ws(tmp_path, "it-compile", {
        "src/main/java/com/example/web/Hello.java":
            "package com.example.web;\n\nimport javax.servlet.http.HttpServlet;\n\n"
            "public class Hello extends HttpServlet {\n}\n"})
    res = MavenRunner(ws, real_cfg).run("compile")
    assert not res.ok and res.exit_code == 1
    assert [(e.kind, e.file, e.line, e.column) for e in res.errors] == [
        (ErrorKind.COMPILE, "src/main/java/com/example/web/Hello.java", 3, 26),
        (ErrorKind.COMPILE, "src/main/java/com/example/web/Hello.java", 5, 28)]
    assert res.errors[0].signature == "compile|package_does_not_exist|javax.servlet.http"
    assert res.errors[1].symbol == "class HttpServlet"
    assert res.failed_module == "it-compile"
    assert journal(ws)[-1]["ok"] is False


@pytest.mark.integration
def test_real_mvn_package_success_and_test_skip(tmp_path, real_cfg):
    ws = _real_ws(tmp_path, "it-ok", {
        "src/main/java/com/example/A.java": "package com.example; public class A {}\n",
        "src/test/java/com/example/ATest.java": "package com.example; public class ATest { Broken b; }\n"})
    broken = MavenRunner(ws, real_cfg).run("package")
    assert not broken.ok and broken.errors[0].kind == ErrorKind.TEST_COMPILE
    res = MavenRunner(ws, real_cfg).run("package", skip_test_compile=True, label="fallback")
    assert res.ok and res.errors == [] and res.modules_built == ["it-ok"]
    assert "-Dmaven.test.skip=true" in res.command
    assert (ws.root / "target" / "it-ok-1.0.0.jar").exists()
