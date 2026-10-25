"""Round 10 (operations): cp949 consoles, the SBMIG-RESULT line, --no-kb really off, the shared LLM probe line,
`sbmig clean`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.cli import app

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "config" / "artifacts.local-e2e.yml"


def _cp949_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "cp949", "SBMIG_ENV_FILE": str(tmp_path / "none.env"), "NO_COLOR": "1",
                "PYTHONPATH": str(REPO)})
    env.pop("PYTHONUTF8", None)
    return env


def _py(code: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `code` like a Windows console with code page 949 and stdout/stderr redirected (Tee-Object, Task Scheduler)."""
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *args], cwd=str(tmp_path),
                          env=_cp949_env(tmp_path), capture_output=True, timeout=180, check=False)


def test_cp949_console_is_really_fragile_without_the_fix(tmp_path):
    """Control: the environment of these tests does reproduce the company console (an em dash cannot be printed)."""
    r = _py("print('\\u2014')", tmp_path)
    assert r.returncode != 0 and b"UnicodeEncodeError" in r.stderr


def test_cp949_logging_lines_and_probe_survive(tmp_path):
    """`--- Logging error ---` on every log line with an em dash; the probe line itself stays ASCII."""
    code = """
        import sys
        from sbmig.cli import app
        sys.argv = ["sbmig", "--log-file", sys.argv[1], "llm-probe", "--llm-provider", "fake", "--prompt", "\\u2705 \\u2014 안녕"]
        app()
    """
    r = _py(code, tmp_path, str(tmp_path / "probe.log"))
    err = r.stderr.decode("cp949", errors="replace")
    out = r.stdout.decode("cp949")                                 # decodable as the console code page
    assert r.returncode == 0, err
    assert "Logging error" not in err and "UnicodeEncodeError" not in err
    assert out.splitlines()[-1].startswith("LLM-PROBE ok provider=fake model=")
    assert "log file" in (tmp_path / "probe.log").read_text(encoding="utf-8")


def test_cp949_run_finishes_with_its_exit_code_and_result_line(tmp_path):
    """The reported bug: `sbmig run` exited 1 (UnicodeEncodeError from the console summary's check marks) after the
    report was written. Orchestrator is a stand-in (no Maven); everything else is the real CLI."""
    proj = tmp_path / "주문"
    proj.mkdir()
    (proj / "pom.xml").write_text("<project/>", encoding="utf-8")
    code = """
        import sys, logging
        import sbmig.loop as loop
        from sbmig.core.models import (BuildError, CaseType, ErrorKind, LLMUsage, Phase, PhaseResult, RunReport,
                                       utcnow)

        class FakeOrchestrator:
            def __init__(self, cfg, ws, kb=None, **kw):
                self.ws = ws
            def run(self, stop_after=None, dry_run=False):
                logging.getLogger("sbmig.loop").warning("build FAILED \\u2014 1 error(s) \\u2705 \\u274c")
                r = RunReport(run_id=self.ws.run_id, project_name="주문\\u2705", case_type=CaseType.SPRING_BOOT,
                              started_at=utcnow(), success=False, llm_usage=LLMUsage(calls=2),
                              flags=["tests-not-compiled"],
                              phases=[PhaseResult(phase=Phase.SPRING, build_ok=False)],
                              unresolved=[BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol \\u2014 X")])
                r.manual_followups.append("web.xml \\u2192 WebXmlRegistrations \\u2014 check \\u2705")
                return r

        loop.Orchestrator = FakeOrchestrator
        from sbmig.cli import app
        sys.argv = ["sbmig", "run", sys.argv[1], "--config", sys.argv[2], "--out", sys.argv[3], "--run-id", "r-1"]
        app()
    """
    r = _py(code, tmp_path, str(proj), str(E2E), str(tmp_path / "out"))
    err = r.stderr.decode("cp949", errors="replace")
    out = r.stdout.decode("cp949")
    assert r.returncode == 2, (out, err)
    assert "Logging error" not in err and "UnicodeEncodeError" not in err + out
    assert out.splitlines()[-1] == ("SBMIG-RESULT run=r-1 exit=2 build=FAIL unresolved=1 blocked=0 llm=2 "
                                    "attn=unresolved:1,tests-not-compiled")
    assert "SBMIG-RESULT run=r-1 exit=2" in (tmp_path / "out" / "r-1" / "sbmig.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------ in-process: result line, --no-kb
class _FakeOrch:
    seen: list = []

    def __init__(self, cfg, ws, kb=None, **kw):
        self.cfg, self.ws, self.kb = cfg, ws, kb
        _FakeOrch.seen.append(self)

    def run(self, stop_after=None, dry_run=False):
        from sbmig.core.models import CaseType, LLMUsage, RunReport, utcnow

        return RunReport(run_id=self.ws.run_id, project_name="p", case_type=CaseType.SPRING_BOOT, started_at=utcnow(),
                         success=True, llm_usage=LLMUsage(calls=1))


@pytest.fixture
def fake_orch(monkeypatch):
    from sbmig import loop

    _FakeOrch.seen = []
    monkeypatch.setattr(loop, "Orchestrator", _FakeOrch)
    return _FakeOrch


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "pom.xml").write_text("<project/>")
    return p


def _detach_run_logs(out: Path) -> None:
    from sbmig.cli import detach_file_log

    for f in out.glob("*/sbmig.log"):
        detach_file_log(f)


def test_run_prints_the_result_line(tmp_path, fake_orch):
    out = tmp_path / "out"
    try:
        r = CliRunner().invoke(app, ["run", str(_project(tmp_path)), "--config", str(E2E), "--out", str(out),
                                     "--run-id", "rid-7"])
    finally:
        _detach_run_logs(out)
    assert r.exit_code == 0, r.output
    assert r.output.splitlines()[-1] == "SBMIG-RESULT run=rid-7 exit=0 build=ok unresolved=0 blocked=0 llm=1 attn=-"


def test_no_kb_switches_off_everything_that_reads_the_kb(tmp_path, fake_orch):
    """--no-kb left kb.catalog / kb.case_replace on: learned text replaces still edited sources in a 'no KB'
    comparison run. CLI and sbmig.api share one override set."""
    from sbmig.api import NO_KB_OVERRIDES
    from sbmig.loop import NullKnowledgeBase

    out = tmp_path / "out"
    try:
        r = CliRunner().invoke(app, ["run", str(_project(tmp_path)), "--config", str(E2E), "--out", str(out),
                                     "--no-kb"])
    finally:
        _detach_run_logs(out)
    assert r.exit_code == 0, r.output
    orch = fake_orch.seen[-1]
    kb = orch.cfg.kb
    assert isinstance(orch.kb, NullKnowledgeBase)
    for key in ("catalog", "case_replace", "error_hints", "harvest", "use_active_recipes", "use_patch_replay",
                "use_hints", "generate_recipe_candidates", "try_candidates_in_plan"):
        assert getattr(kb, key) is False, key
        assert NO_KB_OVERRIDES[key] is False
    assert kb.error_cases is True              # the run's own error_cases.* files are still written


def test_api_no_kb_uses_the_same_overrides(tmp_path, monkeypatch):
    from sbmig import loop
    from sbmig import api

    seen = []

    class Orch(_FakeOrch):
        def __init__(self, cfg, ws, **kw):
            seen.append(cfg)
            super().__init__(cfg, ws, **kw)

    monkeypatch.setattr(loop, "Orchestrator", Orch)
    api.migrate(_project(tmp_path), configs=[E2E], out=tmp_path / "out", kb=False)
    assert seen and seen[0].kb.catalog is False and seen[0].kb.case_replace is False and not seen[0].kb.error_hints


def test_llm_probe_ends_with_the_short_line(tmp_path, monkeypatch):
    monkeypatch.setenv("SBMIG_ENV_FILE", str(tmp_path / "none.env"))
    r = CliRunner().invoke(app, ["llm-probe", "--llm-provider", "fake"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines()[-1].startswith("LLM-PROBE ok provider=fake ")
    for v in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    r = CliRunner().invoke(app, ["llm-probe"])
    assert r.exit_code == 1 and r.output.splitlines()[-1].startswith("LLM-PROBE FAIL config provider=company")


# ------------------------------------------------------------------ sbmig clean
def test_clean_dry_run_then_delete(tmp_path):
    out = tmp_path / "out"
    done = out / "run-ok"
    (done / "project" / "target").mkdir(parents=True)
    (done / "project" / "pom.xml").write_text("<project/>")
    (done / "project" / "target" / "app.jar").write_bytes(b"x" * 2048)
    (done / "report.json").write_text(json.dumps({"success": True, "blocked": [], "flags": []}))
    (done / "result.zip").write_bytes(b"PK")
    failed = out / "run-bad"
    (failed / "project").mkdir(parents=True)
    (failed / "project" / "A.java").write_text("class A {}")
    (failed / "report.json").write_text(json.dumps({"success": False, "blocked": [], "flags": []}))
    running = out / "run-live"
    (running / "project").mkdir(parents=True)
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "llm-0001.json").write_text("{}")
    r = CliRunner().invoke(app, ["clean", "--out", str(out), "--dry-run", "--keep-failed", "--records", str(rec)])
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output and "project-copy" in r.output and "would free" in r.output
    assert (done / "project").exists()
    r = CliRunner().invoke(app, ["clean", "--out", str(out), "--keep-failed", "--records", str(rec)])
    assert r.exit_code == 0, r.output
    assert "freed" in r.output
    assert not (done / "project").exists() and (done / "result.zip").exists() and (done / "report.json").exists()
    assert (failed / "project").exists() and (running / "project").exists() and not (rec / "llm-0001.json").exists()
    r = CliRunner().invoke(app, ["clean", "--out", str(tmp_path / "nope")])
    assert r.exit_code == 2
