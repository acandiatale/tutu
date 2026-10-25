"""Round 10 (B6): on a Korean Windows PC the Maven JVM writes MS949 to a pipe; sbmig decodes UTF-8 → garbled Korean
paths, the error's file was never found. MAVEN_OPTS now asks the JVM for UTF-8 (JDK 18+), and the reader decodes
with replacement characters. Also: Windows children get their own hidden console (Ctrl+Break = thread dump)."""
from __future__ import annotations

import io

import sbmig.build.mvn as mvn_mod
from sbmig.build.mvn import mvn_env, run_logged, with_utf8_output

FLAGS = "-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8"


def test_with_utf8_output_appends_and_respects_existing():
    assert with_utf8_output(None) == FLAGS
    assert with_utf8_output("  -Xmx2g ") == f"-Xmx2g {FLAGS}"
    assert with_utf8_output("-Dstdout.encoding=MS949") == "-Dstdout.encoding=MS949 -Dstderr.encoding=UTF-8"
    assert with_utf8_output(FLAGS) == FLAGS                                   # idempotent


def test_mvn_env_maven_opts_inherited_and_build_env_precedence(cfg, monkeypatch):
    monkeypatch.delenv("MAVEN_OPTS", raising=False)
    assert mvn_env(cfg)["MAVEN_OPTS"] == FLAGS
    monkeypatch.setenv("MAVEN_OPTS", "-Xmx1g")
    assert mvn_env(cfg)["MAVEN_OPTS"] == f"-Xmx1g {FLAGS}"                    # appended, never clobbered
    cfg.build.env = {"MAVEN_OPTS": "-Xmx4g -Dstderr.encoding=x-windows-949"}   # build.env wins over the shell
    assert mvn_env(cfg)["MAVEN_OPTS"] == "-Xmx4g -Dstderr.encoding=x-windows-949 -Dstdout.encoding=UTF-8"
    assert mvn_env(cfg, {"MAVEN_OPTS": "-Xss4m"})["MAVEN_OPTS"] == f"-Xss4m {FLAGS}"


class _Proc:
    seen: dict = {}

    def __init__(self, cmd, **kw):
        _Proc.seen = kw
        self.stdout = io.BytesIO("[ERROR] C:\\작업\\A.java:[1,1] x\n".encode("cp949") + "한글 OK\n".encode("utf-8"))
        self.returncode = None
        self.pid = -1

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def test_reader_never_dies_on_legacy_bytes_and_windows_gets_hidden_console(tmp_path, monkeypatch):
    monkeypatch.setattr(mvn_mod, "_popen", _Proc)
    monkeypatch.setattr(mvn_mod, "IS_WINDOWS", False)
    pr = run_logged(["mvn", "-v"], cwd=tmp_path, env={}, log_path=tmp_path / "a.log", timeout_s=10)
    assert "\ufffd" in pr.text and pr.text.endswith("한글 OK\n") and "creationflags" not in _Proc.seen
    monkeypatch.setattr(mvn_mod, "IS_WINDOWS", True)
    run_logged(["mvn", "-v"], cwd=tmp_path, env={}, log_path=tmp_path / "b.log", timeout_s=10)
    assert _Proc.seen["creationflags"] == 0x08000000                         # CREATE_NO_WINDOW
