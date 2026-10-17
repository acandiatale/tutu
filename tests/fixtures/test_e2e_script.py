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
    assert "| boot2 | spring_boot | 0 | 1/2 | yes | 4 | 2 | 0 |" in md


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
    assert cmd.count("--config") == 1
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
    assert rc == 1                        # fake report: 4 LLM calls, 2 unresolved → both expectations fail
    md = (tmp_path / "out" / "summary.md").read_text()
    assert "boot2" in md and "boot3" in md and "2/2 fixtures reached" in md and "0/2 fixtures met" in md
    assert "| FAIL | llm_calls=4 > max 0" in md and "MISMATCH: unresolved=2 > max 0" in md
    data = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert [d["fixture"] for d in data] == ["boot2", "boot3"]
    assert e2e.discover_fixtures()[:2] == ["pure-java", "servlet-tomcat"]


# ------------------------------------------------------------------------------------------- expectations
REPORT_CLI = r"""
import json, sys, pathlib, os
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
run = out / "run-x"
(run / "project").mkdir(parents=True, exist_ok=True)
spec = json.loads(os.environ["FAKE_SPEC"])
for rel, (enc, text) in spec.get("files", {}).items():
    f = run / "project" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode(enc))
(run / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in spec.get("journal", [])) + "\nnot json\n")
rep = dict(spec["report"])
rep.setdefault("outputs", {})["project_dir"] = str(run / "project")
(run / "report.json").write_text(json.dumps(rep))
sys.exit(spec.get("exit", 0))
"""

BLOCKED_REPORT = {
    "run_id": "run-x", "project_name": "member-api", "case_type": "spring_boot", "success": False,
    "phases": [{"phase": "spring", "ok": False, "notes": []},
               {"phase": "restructure", "ok": True,
                "notes": [("restructure.apply (layout): 3 file(s) changed; mandatory added: -; "
                           "optional bumped: org.apache.commons:commons-lang3 managed→3.17.0")]}],
    "final_build": {"ok": False}, "llm_usage": {"calls": 0}, "unresolved": [],
    "blocked": [{"id": "b1", "reason": "external_javax_api", "phase": "spring", "cluster_id": "c1",
                 "artifact": {"group_id": "com.company.legacy", "artifact_id": "legacy-web-lib", "version": "1.0.0"},
                 "owner_class": "com.company.legacy.web.LegacyRequestHelper"}],
    "flags": [],
}


def _run_spec(e2e, tmp_path, monkeypatch, fixture, spec):
    cli = tmp_path / "report_cli.py"
    cli.write_text(REPORT_CLI)
    monkeypatch.setenv("FAKE_SPEC", json.dumps(spec))
    args = _args(e2e, tmp_path)
    return e2e.run_fixture(fixture, args, cmd=[sys.executable, str(cli), "x", "--out", str(args.out)])


def test_blocked_scenario_passes(e2e, tmp_path, monkeypatch):
    spec = {"report": BLOCKED_REPORT, "exit": 2,
            "journal": [{"event": "restructure", "ok": True, "optional_bumped": []}]}
    monkeypatch.setattr(e2e, "preflight", lambda *a, **k: None)
    r = _run_spec(e2e, tmp_path, monkeypatch, "boot2-javax-lib", spec)
    assert r.expect_ok is True, r.mismatches
    assert r.blocked == ["external_javax_api com.company.legacy:legacy-web-lib"]
    assert r.optional_bumped == ["org.apache.commons:commons-lang3 managed→3.17.0"]
    assert e2e.fixture_passed(r)


def test_blocked_scenario_mismatches_are_reported(e2e, tmp_path, monkeypatch):
    rep = dict(BLOCKED_REPORT, blocked=[dict(BLOCKED_REPORT["blocked"][0], reason="repeated_failure")],
               llm_usage={"calls": 3})
    monkeypatch.setattr(e2e, "preflight", lambda *a, **k: None)
    r = _run_spec(e2e, tmp_path, monkeypatch, "boot2-javax-lib", {"report": rep, "exit": 2})
    assert r.expect_ok is False
    text = "\n".join(r.mismatches)
    assert "llm_calls=3 > max 0" in text
    assert "blocked external_javax_api com.company.legacy:legacy-web-lib missing" in text
    assert "unexpected blocked repeated_failure com.company.legacy:legacy-web-lib" in text
    assert 'no journal event matching {"event": "restructure", "ok": true}' in text


def test_engine_without_new_fields_fails_readably(e2e, tmp_path, monkeypatch):
    """An engine that does not emit blocked/flags yet: mismatches, no crash."""
    rep = {"run_id": "run-x", "project_name": "p", "case_type": "pure_java", "success": True,
           "phases": "garbage", "final_build": None}
    r = _run_spec(e2e, tmp_path, monkeypatch, "enforcer-java8-werror", {"report": rep})
    assert r.expect_ok is False and r.error is None
    assert any("flag 'werror-removed' missing" in m for m in r.mismatches)
    assert any("enforcer-(relaxed|skipped)" in m for m in r.mismatches)


def test_encoding_post_checks(e2e, tmp_path, monkeypatch):
    rel = "module/comp-jar/src/main/java/com/example/greeting/Greeting.java"
    rel2 = "module/comp-jar/src/main/java/com/example/greeting/notice/NoticeFormatter.java"
    rep = {"run_id": "run-x", "project_name": "greeting-core", "case_type": "pure_java", "success": True,
           "llm_usage": {"calls": 0}, "flags": ["source-encoding-set:cp949"], "blocked": [], "unresolved": []}
    ok = {"report": rep, "files": {rel: ["cp949", "// 인사말\nString s = \"안녕하세요\";"],
                                   rel2: ["cp949", "/** 공지사항 */"]}}
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", ok)
    assert r.expect_ok is True, r.mismatches
    bad = {"report": rep, "files": {rel: ["utf-8", "String s = \"안녕하세요\";"], rel2: ["cp949", "/** 공지사항 */"]}}
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", bad)
    assert r.expect_ok is False and "does not decode as cp949" in r.mismatches[0]
    rep2 = dict(rep, flags=["source-encoding-set:utf-16"])
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", dict(ok, report=rep2))
    assert any("source-encoding-set" in m for m in r.mismatches)


def test_pom_post_checks(e2e, tmp_path):
    proj = tmp_path / "p"
    (proj / "common").mkdir(parents=True)
    (proj / "common" / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>org.springframework.boot</groupId>'
        '<artifactId>spring-boot-starter-parent</artifactId><version>4.1.0</version></parent>'
        '<dependencies><dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId>'
        '</dependency></dependencies></project>')
    parent = {"pom_parent": {"path": "common/pom.xml", "group_id": "org.springframework.boot",
                             "artifact_id": "spring-boot-starter-parent", "version": "4.1.0"}}
    assert e2e.run_post_check(parent, proj) is None
    parent["pom_parent"]["version"] = "4.0.0"
    assert "parent is org.springframework.boot:spring-boot-starter-parent:4.1.0" in e2e.run_post_check(parent, proj)
    absent = {"pom_dependency_absent": {"path": "common/pom.xml", "artifacts": ["jakarta.servlet:jakarta.servlet-api"]}}
    assert "declares jakarta.servlet:jakarta.servlet-api" in e2e.run_post_check(absent, proj)
    assert "not found" in e2e.run_post_check({"file_contains": {"path": "**/Nope.java"}}, proj)
    assert "malformed" in e2e.run_post_check({"a": {}, "b": {}}, proj)


def test_optional_bumps_collected_from_everywhere(e2e):
    report = {"optional_bumped": ["a:b 1->2"], "phases": [
        {"notes": ["x; optional bumped: c:d 1.0→2.0, e:f managed→3; more"]}, {"notes": ["optional bumped: -"]}]}
    journal = [{"event": "restructure", "optional_bumped": ["g:h 1→2"]},
               {"event": "optional_bump", "bumped": ["com.company.legacy:legacy-web-lib 1.0.0 → 2.0.0"]}]
    assert e2e.optional_bumps(report, journal) == ["a:b 1→2", "g:h 1→2",
                                                   "com.company.legacy:legacy-web-lib 1.0.0→2.0.0",
                                                   "c:d 1.0→2.0", "e:f managed→3"]


def test_fixture_config_override_is_appended(e2e, tmp_path):
    args = _args(e2e, tmp_path)
    cmd = e2e.build_command("boot2-javax-lib-optional", args, python="py")
    i = [k for k, v in enumerate(cmd) if v == "--config"]
    assert len(i) == 2 and cmd[i[0] + 1] == str(args.config)
    assert cmd[i[1] + 1].endswith("fixtures/projects/boot2-javax-lib-optional/sbmig.e2e.yml")
    assert cmd.index("--out") > i[1]


def test_preflight(e2e, tmp_path):
    repo = tmp_path / "repo"
    exp = {"requires_libs": ["com.company.legacy:legacy-web-lib:1.0.0"],
           "requires_absent": ["com.company.nonexistent:ghost-lib:1.2.3"]}
    assert "not in" in e2e.preflight("x", exp, repo)
    lib = repo / "com/company/legacy/legacy-web-lib/1.0.0/legacy-web-lib-1.0.0.jar"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"")
    assert e2e.preflight("x", exp, repo) is None
    ghost = repo / "com/company/nonexistent/ghost-lib/1.2.3/ghost-lib-1.2.3.jar"
    ghost.parent.mkdir(parents=True)
    ghost.write_bytes(b"")
    assert "must NOT be" in e2e.preflight("x", exp, repo)
    args = e2e.parse_args(["--out", str(tmp_path / "o"), "--local-repo", str(repo)])
    r = e2e.run_fixture("missing-artifact", args, cmd=["false"])
    assert r.expect_ok is False and r.exit_code is None and "must NOT be" in r.mismatches[0]


def test_expect_only_lists_all_fixtures(e2e, capsys):
    assert e2e.main(["--expect-only"]) == 0
    out = capsys.readouterr().out
    for name in e2e.discover_fixtures():
        assert out.count(f"{name} [") == 1
    assert "blocked=[external_javax_api com.company.legacy:legacy-web-lib]" in out
    assert "boot2-javax-lib-optional [X1] +sbmig.e2e.yml" in out
