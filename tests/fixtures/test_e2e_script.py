"""scripts/e2e.py against a fake CLI (no real pipeline)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("sbmig_e2e_script", REPO / "scripts" / "e2e.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sbmig_e2e_script"] = mod
    spec.loader.exec_module(mod)
    return mod


FAKE_CLI = r'''
import json, sys, pathlib, os
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
name = pathlib.Path(sys.argv[1]).name
if name == "boom":
    print("Traceback: kaboom", file=sys.stderr); sys.exit(3)
run = out / f"run-{name}"
run.mkdir(parents=True, exist_ok=True)
rep = {"run_id": run.name, "project_name": name, "case_type": "spring_boot", "success": True,
       "phases": [{"phase": "jdk21", "ok": True}, {"phase": "spring", "ok": False},
                  {"phase": "restructure", "ok": True, "skipped": True}],
       "final_build": {"ok": name != "bad"}, "llm_usage": {"calls": 4}, "unresolved": [{}, {}],
       "flags": ["test-compile-skipped"], "env_provider": os.environ.get("SBMIG_LLM__PROVIDER")}
(run / "report.json").write_text(json.dumps(rep))
print("run dir:", run)
'''


def _args(e2e, tmp_path, *extra):
    return e2e.parse_args(["--out", str(tmp_path / "out"), "--llm-provider", "fake", *extra])


def test_run_fixture_parses_report(e2e, tmp_path):
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE_CLI)
    args = _args(e2e, tmp_path)
    r = e2e.run_fixture("boot2", args, cmd=[sys.executable, str(cli), str(REPO / "fixtures/projects/boot2"),
                                            "--out", str(args.out)])
    assert r.exit_code == 0 and r.error is None
    assert r.case_type == "spring_boot" and r.phases_ok == "1/2" and r.final_build_ok is True
    assert r.llm_calls == 4 and r.unresolved == 2 and r.flags == ["test-compile-skipped"]
    assert r.phases == ["jdk21:ok", "spring:FAIL", "restructure:skipped"]
    rep = json.loads(Path(r.report_path).read_text())
    assert rep["env_provider"] == "fake"
    table = e2e.format_table([r])
    assert "boot2" in table and "spring_boot" in table and "1/2" in table
    md = e2e.format_markdown([r], args)
    assert "| boot2 | spring_boot | 0 | 1/2 | yes | 4 | 2 |" in md


def test_run_fixture_degrades_gracefully(e2e, tmp_path):
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE_CLI)
    args = _args(e2e, tmp_path)
    r = e2e.run_fixture("boom", args, cmd=[sys.executable, str(cli), "x/boom", "--out", str(args.out)])
    assert r.exit_code == 3 and "no run dir created" in r.error and "kaboom" in r.error
    r = e2e.run_fixture("x", args, cmd=[str(tmp_path / "does-not-exist")])
    assert r.error.startswith("cannot start CLI")
    r = e2e.run_fixture("nomod", args, cmd=[sys.executable, "-m", "sbmig_no_such_cli_module"])
    assert r.exit_code != 0 and r.error


def test_build_command_and_env(e2e, tmp_path):
    args = _args(e2e, tmp_path, "--stop-after", "spring", "--java-home", "/opt/jdk21", "--extra", "--foo", "1")
    cmd = e2e.build_command("boot3", args, python="py")
    assert cmd[:4] == ["py", "-m", "sbmig.cli", "run"] and cmd[4].endswith("fixtures/projects/boot3")
    assert "--stop-after" in cmd and cmd[-2:] == ["--foo", "1"] and "--llm-provider" not in cmd
    args.llm_provider_flag = True
    assert "--llm-provider" in e2e.build_command("boot3", args)
    env = e2e.build_env(args)
    assert env["JAVA_HOME"] == "/opt/jdk21" and env["JAVA21_HOME"] == "/opt/jdk21"
    assert env["SBMIG_LLM__PROVIDER"] == "fake"


def test_main_with_unknown_fixture(e2e, tmp_path, capsys):
    assert e2e.main(["--only", "nope", "--out", str(tmp_path / "o")]) == 2


def test_main_writes_summary(e2e, tmp_path, monkeypatch):
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE_CLI)

    def fake_build(fixture, args, python=sys.executable):
        return [python, str(cli), str(e2e.FIXTURES_DIR / fixture), "--out", str(args.out)]

    monkeypatch.setattr(e2e, "build_command", fake_build)
    rc = e2e.main(["--only", "boot2,boot3", "--out", str(tmp_path / "out")])
    assert rc == 0
    md = (tmp_path / "out" / "summary.md").read_text()
    assert "boot2" in md and "boot3" in md and "2/2 fixtures" in md
    data = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert [d["fixture"] for d in data] == ["boot2", "boot3"]
    assert e2e.discover_fixtures()[:2] == ["pure-java", "servlet-tomcat"]
