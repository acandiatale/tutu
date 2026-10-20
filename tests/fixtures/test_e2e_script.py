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
    assert e2e.discover_fixtures().index("boot2-cloud-legacy") == e2e.discover_fixtures().index("msa-shared-lib") + 1
    line = next(ln for ln in out.splitlines() if ln.startswith("boot2-cloud-legacy ["))
    assert "advisories=[springfox:policy=recipe:outcome=replaced:recipe_source=plan" in line
    assert "hystrix:policy=recipe:outcome=replaced:recipe_source=advisory" in line
    assert "handover(carried_over_min=1, spring_boot_after=4.1.0, from_here_contains=2" in line
    assert "followups=[regex:(?i).*hystrix → .*resilience4j applied by HystrixToResilience4j" in line
    assert "post_checks=7" in line


# ------------------------------------------------------------------------------------------- advisories / handover
ADVISORY_REPORT = {
    "run_id": "run-x", "project_name": "order-api", "case_type": "spring_boot", "success": True,
    "llm_usage": {"calls": 0}, "blocked": [], "unresolved": [],
    "advisories": [
        {"advisory_id": "springfox", "library": "springfox", "status": "eol", "policy": "recipe",
         "recipe_source": "advisory", "outcome": "replaced"},
        {"advisory_id": "sleuth", "library": "sleuth", "status": "eol", "policy": "guide",
         "recipe_source": "plan", "outcome": "replaced"},
        {"advisory_id": "hystrix", "library": "hystrix", "status": "eol", "policy": "guide",
         "recipe_source": "none", "outcome": "still-present"},
    ],
    "handover": {
        "parent_before": "org.springframework.boot:spring-boot-starter-parent:2.7.18",
        "parent_after": "org.springframework.boot:spring-boot-starter-parent:4.1.0",
        "spring_boot_before": "2.7.18", "spring_boot_after": "4.1.0",
        "changes": [{"key": "org.springframework.cloud:spring-cloud-dependencies", "before": "2021.0.9",
                     "after": "2025.1.0"},
                    {"key": "org.springframework.cloud:spring-cloud-starter-netflix-hystrix",
                     "before": "2.2.10.RELEASE", "after": "2.2.10.RELEASE", "carried_over": True}],
        "carried_over": [{"key": "org.springframework.cloud:spring-cloud-starter-netflix-hystrix",
                          "carried_over": True}],
        "added": ["org.springdoc:springdoc-openapi-starter-webmvc-ui:3.0.0"],
        "removed": ["io.springfox:springfox-boot-starter:3.0.0",
                    "org.springframework.cloud:spring-cloud-starter-sleuth"],
        "from_here": ["hystrix (EOL): replace @HystrixCommand with Resilience4j @CircuitBreaker",
                      "verify springdoc UI path"],
    },
    "manual_followups": ["Netflix Hystrix → Resilience4j (spring-cloud-starter-circuitbreaker-resilience4j)"],
}

ADVISORY_EXPECT = {
    "advisories": [{"id": "springfox", "policy": "recipe", "outcome": "replaced", "recipe_source": "advisory"},
                   {"id": "sleuth", "outcome": "replaced", "recipe_source": "plan"},
                   {"id": "hystrix", "policy": "guide", "outcome": "still-present"}],
    "advisories_absent": ["log4j1"],
    "handover": {"changes_min": 2, "carried_over_min": 1, "carried_over_max": 1,
                 "parent_after": "org.springframework.boot:spring-boot-starter-parent",
                 "spring_boot_after": "4.1.0",
                 "from_here_contains": ["regex:(?i).*hystrix.*resilience4j.*", "springdoc UI"],
                 "removed_contains": ["io.springfox:springfox-boot-starter",
                                      "org.springframework.cloud:spring-cloud-starter-sleuth"],
                 "added_contains": ["org.springdoc:springdoc-openapi-starter-webmvc-ui:3"]},
    "followups_contains": ["regex:(?i).*resilience4j.*"],
}


def _eval(e2e, expect, report):
    return e2e.evaluate(e2e.FixtureResult(fixture="x", exit_code=0), expect, report, [], None)


def test_advisory_and_handover_expectations_pass(e2e):
    assert _eval(e2e, ADVISORY_EXPECT, ADVISORY_REPORT) == []


def test_advisory_mismatches(e2e):
    exp = {"advisories": [{"id": "springfox", "policy": "guide", "outcome": "replaced"},
                          {"id": "zuul"}],
           "advisories_absent": ["hystrix"]}
    mm = _eval(e2e, exp, ADVISORY_REPORT)
    assert "advisory springfox: policy='recipe' expected 'guide'" in mm
    assert any(m.startswith("advisory zuul missing (got: springfox:replaced, sleuth:replaced") for m in mm)
    assert "advisory hystrix present but expected absent" in mm
    assert not any("unexpected advisory" in m for m in mm)
    exact = {"advisories": [{"id": "springfox"}], "advisories_exact": True}
    assert _eval(e2e, exact, ADVISORY_REPORT) == ["unexpected advisory sleuth", "unexpected advisory hystrix"]


def test_handover_mismatches(e2e):
    exp = {"handover": {"changes_min": 3, "carried_over_min": 2, "carried_over_max": 0,
                        "parent_after": "com.company:company-parent", "spring_boot_after": "4.1",
                        "from_here_contains": ["regex:.*zuul.*", "ribbon"],
                        "removed_contains": ["com.netflix.hystrix:hystrix-core"],
                        "added_contains": ["org.springdoc:springdoc-openapi-starter-webmvc-ui:2"]},
           "followups_contains": ["regex:.*sleuth.*"]}
    text = "\n".join(_eval(e2e, exp, ADVISORY_REPORT))
    for part in ("handover changes=2 < min 3", "handover carried_over=1 < min 2", "handover carried_over=1 > max 0",
                 "handover parent_after='org.springframework.boot:spring-boot-starter-parent:4.1.0' expected "
                 "'com.company:company-parent'", "handover spring_boot_after='4.1.0' expected '4.1'",
                 "handover from_here lacks 'regex:.*zuul.*'", "handover from_here lacks 'ribbon'",
                 "handover removed lacks com.netflix.hystrix:hystrix-core",
                 "handover added lacks org.springdoc:springdoc-openapi-starter-webmvc-ui:2",
                 "manual_followups lack 'regex:.*sleuth.*'"):
        assert part in text, part


def test_missing_advisory_fields_never_crash(e2e):
    """An engine without advisories/handover yet: readable mismatches, no exception."""
    for report in ({"success": True}, {"advisories": "garbage", "handover": "garbage", "manual_followups": None},
                   {"advisories": [None, 3, {}], "handover": {"changes": None, "from_here": [1, None]}}):
        mm = _eval(e2e, ADVISORY_EXPECT, report)
        assert any("advisory springfox missing" in m for m in mm)
        assert any("carried_over=0 < min 1" in m for m in mm)
        assert any("manual_followups lack" in m for m in mm)
    assert "no handover in report.json" in _eval(e2e, {"handover": {}}, {"success": True})


def test_carried_over_falls_back_to_flagged_changes(e2e):
    ho = dict(ADVISORY_REPORT["handover"], carried_over=[])
    assert len(e2e.carried_over_items(ho)) == 1
    assert e2e.carried_over_items(None) == []


def test_advisory_summary_columns(e2e, tmp_path, monkeypatch):
    r = _run_spec(e2e, tmp_path, monkeypatch, "boot2-cloud-legacy", {"report": ADVISORY_REPORT})
    assert r.advisories == ["springfox:replaced", "sleuth:replaced", "hystrix:still-present"]
    assert r.carried_over == 1
    table = e2e.format_table([r])
    assert "advisories" in table.splitlines()[0] and "carried" in table.splitlines()[0]
    assert "springfox:replaced,sleuth:replaced,hystrix:still-present" in table
    md = e2e.format_markdown([r], _args(e2e, tmp_path))
    assert "| springfox:replaced,sleuth:replaced,hystrix:still-present | 1 |" in md
    many = [f"advisory-{i}:still-present" for i in range(10)]
    short = e2e._fmt_advisories(many)
    assert len(short) <= 60 and short.endswith(")") and "…(+" in short
    assert e2e._fmt_advisories([]) == "-"
    r2 = e2e.FixtureResult(fixture="y")
    e2e.apply_report(r2, {"success": True})
    assert r2.advisories == [] and r2.carried_over is None


def test_boot2_cloud_legacy_expectations_against_synthetic_report(e2e):
    """The fixture's own expect block (minus post_checks) passes against a report shaped like the target run."""
    exp = dict(e2e.load_expected("boot2-cloud-legacy")["expect"])
    exp.pop("post_checks")
    # Boot projects: the springfox/sleuth/cloud-train recipes are inside the Boot 4 umbrella chain → recipe_source
    # "plan"; the hystrix project composite is not → prepended, recipe_source "advisory" (shapes as in the
    # 2026-10-20 run); the un-catalogued net.sf.ehcache dependency shows up as a carried-over pin in from_here
    hystrix = {"advisory_id": "hystrix", "library": "hystrix", "status": "eol", "policy": "recipe",
               "recipe_source": "advisory", "outcome": "replaced"}
    advisories = [dict(a, recipe_source="plan") if a["advisory_id"] == "springfox" else
                  hystrix if a["advisory_id"] == "hystrix" else a for a in ADVISORY_REPORT["advisories"]]
    advisories.append({"advisory_id": "spring-cloud-bom", "policy": "recipe", "recipe_source": "plan",
                       "outcome": "replaced"})
    starter = "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j"
    verify = (f"verify hystrix → {starter} (recipe com.company.migrate.HystrixToResilience4j): Configure the circuit "
              "breakers by name (instance \"default\" unless a commandKey existed: "
              "resilience4j.circuitbreaker.instances.<name> or configs.default), move hystrix.command.* timeouts "
              "to resilience4j.timelimiter.*")
    handover = dict(ADVISORY_REPORT["handover"],
                    added=ADVISORY_REPORT["handover"]["added"] + [f"{starter} (advisory:hystrix)"],
                    removed=ADVISORY_REPORT["handover"]["removed"]
                    + ["org.springframework.cloud:spring-cloud-starter-netflix-hystrix:2.2.10.RELEASE "
                       "(advisory:hystrix)"],
                    from_here=[verify, "verify springdoc UI path",
                               "net.sf.ehcache:ehcache pinned at 2.10.9.2 — the new parent/Boot BOM no longer "
                               "manages it: confirm the version or replace"])
    followups = [verify.replace("verify hystrix", "hystrix").replace(
        " (recipe com.company.migrate.HystrixToResilience4j): Configure", " applied by HystrixToResilience4j; "
        "Configure")]
    rep = dict(ADVISORY_REPORT, advisories=advisories, handover=handover, manual_followups=followups)
    assert _eval(e2e, exp, rep) == []
    # the old guide shape no longer satisfies the fixture
    assert "advisory hystrix: policy='guide' expected 'recipe'" in _eval(e2e, exp, dict(ADVISORY_REPORT))


# ------------------------------------------------------------------------------------------- kb (round 5)
KB_CANDIDATE_YML = """type: specs.openrewrite.org/v1beta/recipe
name: com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp
displayName: com.company.legacy.web.LegacyRequestHelper.clientIp(..) -> remoteClientIp
recipeList:
- org.openrewrite.java.ChangeMethodName:
    methodPattern: com.company.legacy.web.LegacyRequestHelper clientIp(..)
    newMethodName: remoteClientIp
    matchOverrides: true
---
type: specs.openrewrite.org/v1beta/recipe
name: com.company.kb.Other
recipeList:
- org.openrewrite.java.OrderImports
"""

KB_EXPECT = {
    "dir": "out/e2e/kb-x", "candidates_min": 1, "harvest_candidates_min": 1,
    "recipes": [{"recipe": "org.openrewrite.java.ChangeMethodName",
                 "name": "com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp",
                 "options": {"methodPattern": "com.company.legacy.web.LegacyRequestHelper clientIp(..)",
                             "newMethodName": "remoteClientIp"}}],
}
KB_REPORT = {"success": True, "harvest": {"cases_created": ["c1"],
                                          "candidates_created": ["com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp"]}}


def _kb(tmp_path, sub="candidates", text=KB_CANDIDATE_YML, name="r.yml"):
    d = tmp_path / "out" / "e2e" / "kb-x" / "recipes" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    (d / (name[:-4] + ".meta.json")).write_text("{}", encoding="utf-8")   # never parsed as a recipe
    return d


def test_kb_expectation_passes(e2e, tmp_path):
    _kb(tmp_path)
    assert e2e.evaluate_kb(KB_EXPECT, KB_REPORT, tmp_path) == []
    # via evaluate(): the `kb` block is checked together with the rest
    assert e2e.evaluate(e2e.FixtureResult(fixture="x", exit_code=0), {"success": True, "kb": KB_EXPECT},
                        KB_REPORT, [], None, tmp_path) == []
    # an active (promoted) recipe also satisfies `recipes`, but is not a candidate
    (tmp_path / "out/e2e/kb-x/recipes/candidates/r.yml").unlink()
    _kb(tmp_path, sub="active")
    mm = e2e.evaluate_kb(KB_EXPECT, KB_REPORT, tmp_path)
    assert mm == ["kb candidates=0 < min 1"]
    assert e2e.evaluate_kb(dict(KB_EXPECT, candidates_min=0), KB_REPORT, tmp_path) == []


def test_kb_expectation_option_and_name_mismatch(e2e, tmp_path):
    _kb(tmp_path)
    wrong = dict(KB_EXPECT, recipes=[{"recipe": "org.openrewrite.java.ChangeMethodName",
                                      "options": {"newMethodName": "clientAddress"}}])
    mm = e2e.evaluate_kb(wrong, KB_REPORT, tmp_path)
    assert len(mm) == 1 and mm[0].startswith("kb recipe org.openrewrite.java.ChangeMethodName "
                                             '{"newMethodName": "clientAddress"} missing')
    assert "com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp[candidates]" in mm[0]
    other_name = dict(KB_EXPECT, recipes=[dict(KB_EXPECT["recipes"][0], name="com.company.kb.Other")])
    assert "in com.company.kb.Other" in e2e.evaluate_kb(other_name, KB_REPORT, tmp_path)[0]
    other_id = dict(KB_EXPECT, recipes=[{"recipe": "org.openrewrite.java.ChangeType", "options": {}}])
    assert e2e.evaluate_kb(other_id, KB_REPORT, tmp_path)
    bare = dict(KB_EXPECT, recipes=[{"recipe": "org.openrewrite.java.OrderImports"}])   # `- id` list entry
    assert e2e.evaluate_kb(bare, KB_REPORT, tmp_path) == []


def test_kb_expectation_missing_dir_and_harvest(e2e, tmp_path):
    mm = e2e.evaluate_kb(KB_EXPECT, {}, tmp_path)
    assert mm[0] == "kb dir 'out/e2e/kb-x' missing"
    assert "kb candidates=0 < min 1" in mm and "harvest candidates_created=0 < min 1" in mm
    assert any(m.startswith("kb recipe org.openrewrite.java.ChangeMethodName") and "(got: none)" in m for m in mm)
    _kb(tmp_path, text="name: [unclosed\n", name="broken.yml")       # unparsable YAML is skipped, never a crash
    mm = e2e.evaluate_kb(KB_EXPECT, {"harvest": None}, tmp_path)
    assert "kb candidates=0 < min 1" in mm and "harvest candidates_created=0 < min 1" in mm


def test_kb_dir_is_cleared_only_under_out(e2e, tmp_path, monkeypatch):
    assert e2e.kb_dir_to_clear({"kb": {"dir": "out/e2e/kb-x"}}, tmp_path) == (tmp_path / "out/e2e/kb-x").resolve()
    for bad in ("kb", "out", "out/../kb", "/tmp/kb", "", None):
        assert e2e.kb_dir_to_clear({"kb": {"dir": bad}}, tmp_path) is None, bad
    assert e2e.kb_dir_to_clear({"success": True}, tmp_path) is None and e2e.kb_dir_to_clear(None) is None
    # run_fixture wipes the fixture's KB dir before the CLI runs (the fake CLI writes no KB → dir missing afterwards)
    stale = _kb(tmp_path)
    monkeypatch.setattr(e2e, "REPO", tmp_path)
    monkeypatch.setattr(e2e, "preflight", lambda *a, **k: None)
    expected = {"expect": {"success": True, "exit_code": 0, "kb": KB_EXPECT}}
    args = _args(e2e, tmp_path)
    cli = tmp_path / "report_cli.py"
    cli.write_text(REPORT_CLI)
    monkeypatch.setenv("FAKE_SPEC", json.dumps({"report": KB_REPORT}))
    r = e2e.run_fixture("boot2-method-rename", args, cmd=[sys.executable, str(cli), "x", "--out", str(args.out)],
                        expected=expected)
    assert not stale.exists()
    assert r.expect_ok is False and r.mismatches[0] == "kb dir 'out/e2e/kb-x' missing"


def test_boot2_method_rename_expectations(e2e, capsys):
    exp = json.loads((REPO / "fixtures/projects/boot2-method-rename/expected.json").read_text())["expect"]
    assert exp["kb"]["dir"] == "out/e2e/kb-method-rename"
    assert e2e.kb_dir_to_clear(exp) == (REPO / "out/e2e/kb-method-rename").resolve()
    assert e2e.main(["--expect-only", "--only", "boot2-method-rename"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("boot2-method-rename [X2] +sbmig.e2e.yml: success=True; exit=0; llm<=2")
    assert "optional_bumped=[com.company.legacy:legacy-web-lib 1.0.0→3.0.0]" in line
    assert ("kb(dir=out/e2e/kb-method-rename, candidates_min=1, harvest_candidates_min=1, "
            "org.openrewrite.java.ChangeMethodName@com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp)") in line


def test_ga_ignores_handover_suffixes(e2e):
    assert e2e._ga("org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j (advisory:hystrix)") == \
        "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j"
    assert e2e._gav_listed("g:a:2.2", ["g:a:2.2.10.RELEASE (advisory:hystrix)"])
    assert e2e._gav_listed("g:a", ["g:a (advisory:hystrix)"]) and not e2e._gav_listed("g:b", ["g:a (x)"])


# ---------------------------------------------------------------------------------------------- round 8: properties
PROPS_PH = "module/boot-jar/src/main/resources/application.properties"


def _props_report(external=("mail.host", "java:comp/env/jdbc/catalogDS", "catalog.home"), in_profile=False) -> dict:
    inv = {"findings": [{"key": k, "status": "external"} for k in external]
           + [{"key": "catalog.title", "status": "defined"}, {"key": "catalog.page-size", "status": "defined"}],
           "placeholder_files": [PROPS_PH], "placeholders_written": ["mail.host", "spring.datasource.url"]}
    return {"profile": {"properties": inv}} if in_profile else {"properties": inv}


def _props_project(tmp_path):
    f = tmp_path / "proj" / PROPS_PH
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# --- sbmig externalized-config\nmail.host=__SET_ME__\nspring.datasource.url=__SET_ME__\n")
    return tmp_path / "proj"


PROPS_WANT = {"external_min": 3, "external": ["mail.host", "java:comp/env/jdbc/catalogDS", "catalog.home"],
              "absent": ["catalog.title", "catalog.page-size"], "written": ["mail.host"],
              "placeholder_file_contains": ["mail.host=__SET_ME__", "spring.datasource.url=__SET_ME__"]}


def test_properties_expectation_passes(e2e, tmp_path):
    assert e2e.evaluate_properties(PROPS_WANT, _props_report(), _props_project(tmp_path)) == []
    assert e2e.evaluate_properties(PROPS_WANT, _props_report(in_profile=True), _props_project(tmp_path)) == []


def test_properties_mismatches(e2e, tmp_path):
    want = dict(PROPS_WANT, absent=["mail.host"], written=["catalog.home"],
                placeholder_file_contains=["server.servlet.context-parameters.catalog.region=kr"])
    mm = e2e.evaluate_properties(want, _props_report(external=("mail.host",)), _props_project(tmp_path))
    text = "\n".join(mm)
    assert "external=1 < min 3" in text
    assert "'catalog.home' is not an external finding" in text
    assert "'mail.host' must not be external" in text
    assert "'catalog.home' not in placeholders_written" in text
    assert "lacks 'server.servlet.context-parameters.catalog.region=kr'" in text
    assert e2e.evaluate_properties(PROPS_WANT, {}, None)[0] == "no properties inventory in report.json"
    assert any("not found in the migrated project" in m
               for m in e2e.evaluate_properties(PROPS_WANT, _props_report(), tmp_path / "nowhere"))


def test_properties_wired_into_evaluate(e2e, tmp_path):
    res = e2e.FixtureResult(fixture="legacy-external-config", exit_code=0)
    report = dict(_props_report(), success=True)
    assert e2e.evaluate(res, {"properties": PROPS_WANT}, report, [], _props_project(tmp_path)) == []
    assert e2e.evaluate(res, {"properties": dict(PROPS_WANT, external_min=9)}, report, [],
                        _props_project(tmp_path))
