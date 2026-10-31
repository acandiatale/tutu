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
    assert cmd.count("--config") == 2 and cmd[cmd.index("--config", 6) + 1].endswith("boot3/sbmig.e2e.yml")
    assert e2e.build_command("no-such-fixture", args, python="py").count("--config") == 1   # no override file
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
    assert "| FAIL | llm_calls=4 > max 1" in md and "MISMATCH: unresolved=2 > max 0" in md
    assert "| review | scripts | errors |" in md and "MISMATCH: no structure_review in report.json" in md
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
for d in spec.get("dirs", []):
    pathlib.Path(d).mkdir(parents=True, exist_ok=True)
rep = dict(spec["report"])
rep.setdefault("outputs", {})["project_dir"] = str(run / "project")
(run / "report.json").write_text(json.dumps(rep))
sys.exit(spec.get("exit", 0))
"""

def _att(i: int, case: str, outcome: str = "apply_failed", mode: str = "new", version: int = 0, calls: int = 1,
         iteration: int = 1) -> dict:
    """A v2 script attempt as report.json carries it (FixAttempt)."""
    return {"id": f"verify-{iteration}-{i}", "phase": "verify", "iteration": iteration, "cluster_id": f"case:{case}",
            "script_mode": mode, "script_case_id": case, "script_version": version, "outcome": outcome,
            "llm_usage": {"calls": calls}}


JAVAX_LIB_GA = {"group_id": "com.company.legacy", "artifact_id": "legacy-web-lib", "version": "1.0.0"}
BLOCKED_REPORT = {
    "run_id": "run-x", "project_name": "member-api", "case_type": "spring_boot", "success": False,
    "phases": [{"phase": "transform", "ok": True, "notes": []},
               {"phase": "restructure", "ok": True,
                "notes": [("restructure.apply (layout): 3 file(s) changed; mandatory added: -; "
                           "optional bumped: org.apache.commons:commons-lang3 managed→3.17.0")]},
               # v2.1y: each javax case is asked once; the analysis-only answer is `declined` → outcome blocked
               {"phase": "verify", "ok": False,
                "attempts": [_att(1, "f2b133edd9", "blocked"), _att(2, "61d819f63f", "blocked")]}],
    "final_build": {"ok": False}, "llm_usage": {"calls": 3}, "unresolved": [],
    "blocked": [{"id": "b1", "reason": "external_javax_api", "phase": "verify", "cluster_id": "case:f2b133edd9",
                 "artifact": JAVAX_LIB_GA, "owner_class": "com.company.legacy.web.LegacyRequestHelper"},
                {"id": "b2", "reason": "external_javax_api", "phase": "verify", "cluster_id": "case:61d819f63f",
                 "artifact": JAVAX_LIB_GA}],
    "structure_review": {"verdict": "issues", "llm_usage": {"calls": 1}, "issues": [
        {"severity": "warning", "file": "module/comp-jar/pom.xml",
         "problem": "the in-house com.company.legacy:legacy-web-lib:1.0.0 is a javax.servlet build", "fix_hint": "x"}]},
    "flags": [],
}


def _run_spec(e2e, tmp_path, monkeypatch, fixture, spec):
    cli = tmp_path / "report_cli.py"
    cli.write_text(REPORT_CLI)
    monkeypatch.setenv("FAKE_SPEC", json.dumps(spec))
    args = _args(e2e, tmp_path)
    return e2e.run_fixture(fixture, args, cmd=[sys.executable, str(cli), "x", "--out", str(args.out)])


def _kb_spec(e2e, tmp_path, monkeypatch, fixture, spec):
    """The fixture's KB dir under a temporary repo (run_fixture clears it, the fake CLI creates it again)."""
    monkeypatch.setattr(e2e, "REPO", tmp_path)
    monkeypatch.setattr(e2e, "preflight", lambda *a, **k: None)
    kb_dir = e2e.load_expected(fixture)["expect"]["kb"]["dir"]
    return dict(spec, dirs=[str(tmp_path / kb_dir)])


def test_blocked_scenario_passes(e2e, tmp_path, monkeypatch):
    spec = {"report": BLOCKED_REPORT, "exit": 2,
            "journal": [{"event": "restructure", "ok": True, "optional_bumped": []},
                        {"event": "declined", "case": "f2b133edd9", "declines": 1},
                        {"event": "declined", "case": "61d819f63f", "declines": 1},
                        {"event": "blocked", "cluster": "case:f2b133edd9", "reason": "external_javax_api"}]}
    r = _run_spec(e2e, tmp_path, monkeypatch, "boot2-javax-lib", _kb_spec(e2e, tmp_path, monkeypatch,
                                                                           "boot2-javax-lib", spec))
    assert r.expect_ok is True, r.mismatches
    assert r.blocked == ["external_javax_api com.company.legacy:legacy-web-lib"] * 2
    assert r.optional_bumped == ["org.apache.commons:commons-lang3 managed→3.17.0"]
    assert r.review == "issues" and r.scripts == "new 2"
    assert e2e.fixture_passed(r)


def test_blocked_scenario_mismatches_are_reported(e2e, tmp_path, monkeypatch):
    rep = dict(BLOCKED_REPORT, blocked=[dict(BLOCKED_REPORT["blocked"][0], reason="repeated_failure")],
               llm_usage={"calls": 9}, phases=BLOCKED_REPORT["phases"][:2])
    rep.pop("structure_review")
    r = _run_spec(e2e, tmp_path, monkeypatch, "boot2-javax-lib",
                  _kb_spec(e2e, tmp_path, monkeypatch, "boot2-javax-lib", {"report": rep, "exit": 2}))
    assert r.expect_ok is False
    text = "\n".join(r.mismatches)
    assert "llm_calls=9 > max 3" in text
    assert "blocked external_javax_api com.company.legacy:legacy-web-lib missing" in text
    assert "unexpected blocked repeated_failure com.company.legacy:legacy-web-lib" in text
    assert 'no journal event matching {"event": "restructure", "ok": true}' in text
    assert "no structure_review in report.json" in text
    assert "script attempts new=0 expected == 2" in text
    assert "case f2b133edd9: no script attempt (cases with attempts: none)" in text


def test_engine_without_new_fields_fails_readably(e2e, tmp_path, monkeypatch):
    """An engine that does not emit the v2 fields (review, script attempts, KB) yet: mismatches, no crash."""
    rep = {"run_id": "run-x", "project_name": "p", "case_type": "pure_java", "success": True,
           "phases": "garbage", "final_build": None, "structure_review": "garbage"}
    r = _run_spec(e2e, tmp_path, monkeypatch, "enforcer-java8-werror", {"report": rep})
    assert r.expect_ok is False and r.error is None
    assert "no structure_review in report.json" in r.mismatches
    assert any(m.startswith("script attempts new=0 expected == 2") for m in r.mismatches)
    assert any(m.startswith("case ") and "no script attempt" in m for m in r.mismatches)


def test_encoding_post_checks(e2e, tmp_path, monkeypatch):
    rel = "module/comp-jar/src/main/java/com/example/greeting/Greeting.java"
    rel2 = "module/comp-jar/src/main/java/com/example/greeting/notice/NoticeFormatter.java"
    pom = "module/comp-jar/pom.xml"
    # v2.1x (D-M7): the restructure writes project.build.sourceEncoding itself; the review then answers "ok"
    review = {"verdict": "ok", "commit": None, "reverted": False, "llm_usage": {"calls": 1}, "issues": [],
              "script_result": None}
    rep = {"run_id": "run-x", "project_name": "greeting-core", "case_type": "pure_java", "success": True,
           "llm_usage": {"calls": 1}, "flags": [], "blocked": [], "unresolved": [], "structure_review": review}
    journal = [{"event": "structure_review", "verdict": "ok", "critical": 0},
               {"event": "legacy_context", "mode": "layout", "source_encodings_written": 1}]
    files = {rel: ["cp949", "// 인사말\nString s = \"안녕하세요\";"], rel2: ["cp949", "/** 공지사항 */"],
             pom: ["utf-8", "<project><properties><project.build.sourceEncoding>MS949</project.build.sourceEncoding>"
                            "</properties></project>"]}
    ok = _kb_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", {"report": rep, "journal": journal, "files": files})
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", ok)
    assert r.expect_ok is True, r.mismatches
    bad = dict(ok, files=dict(files, **{rel: ["utf-8", "String s = \"안녕하세요\";"]}))
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", bad)
    assert r.expect_ok is False and "does not decode as cp949" in r.mismatches[0]
    # the restructure missed the encoding and the review's own `(sbmig check)` issue appeared: the expectation fails
    missed = dict(review, verdict="issues",
                  issues=[{"severity": "critical", "file": pom, "problem": "(sbmig check) MS949 Java sources ...",
                           "fix_hint": "set project.build.sourceEncoding to MS949"}])
    r = _run_spec(e2e, tmp_path, monkeypatch, "euckr-legacy", dict(ok, report=dict(rep, structure_review=missed)))
    text = "\n".join(r.mismatches)
    assert "structure_review sbmig_check=True expected False" in text


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


def test_pom_packaging_and_module_list_post_checks(e2e, tmp_path):
    """Round 9c: `pom_packaging` (absent <packaging> = jar) and `pom_module_list` (exact list, `any_order` optional)."""
    proj = tmp_path / "p"
    (proj / "shop-boot").mkdir(parents=True)
    (proj / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><packaging>pom</packaging><modules>'
        '<module>shop-back</module><module>shop-boot</module></modules>'
        '<profiles><profile><modules><module>extra</module></modules></profile></profiles></project>')
    (proj / "shop-boot" / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"/>')
    run = e2e.run_post_check
    assert run({"pom_packaging": {"path": "pom.xml", "packaging": "pom"}}, proj) is None
    assert run({"pom_packaging": {"path": "shop-boot/pom.xml", "packaging": "jar"}}, proj) is None
    assert "packaging is jar, expected war" in run({"pom_packaging": {"path": "shop-boot/pom.xml", "packaging": "war"}}, proj)
    ok = {"pom_module_list": {"path": "pom.xml", "modules": ["shop-back", "shop-boot"]}}
    assert run(ok, proj) is None                                    # profile modules are not counted
    swapped = {"pom_module_list": {"path": "pom.xml", "modules": ["shop-boot", "shop-back"]}}
    assert "modules are ['shop-back', 'shop-boot']" in run(swapped, proj)
    swapped["pom_module_list"]["any_order"] = True
    assert run(swapped, proj) is None
    assert "expected ['shop-back']" in run({"pom_module_list": {"path": "pom.xml", "modules": ["shop-back"]}}, proj)
    assert "modules are []" in run({"pom_module_list": {"path": "shop-boot/pom.xml", "modules": ["x"]}}, proj)


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
    # v2: no advisory recipes — hystrix stays (guide), the Boot 4 umbrella replaces springfox / sleuth
    assert "advisories=[springfox:outcome=replaced, sleuth:outcome=replaced, hystrix:policy=guide:outcome=still-present" \
        in line
    assert "handover(carried_over_min=1, spring_boot_after=4.1.0, from_here_contains=2" in line
    assert "review(verdict=issues, critical=0, script_status=none)" in line
    assert 'scripts(attempts={"augment": 0, "new": 1, "reuse": 0}, cases=1)' in line and "post_checks=4" in line
    java6 = next(ln for ln in out.splitlines() if ln.startswith("java6-legacy ["))
    assert "kb(dir=out/e2e/kb-jaxb, after=pure-java)" in java6
    assert 'scripts(attempts={"augment": 0, "new": 2, "reuse": 1}, cases=2, stored=1, reuse_without_llm)' in java6
    aug = next(ln for ln in out.splitlines() if ln.startswith("boot2-fix-failures ["))
    assert 'scripts(attempts={"augment": 1, "new": 1, "reuse": 0}, cases=1, stored=2)' in aug


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


def test_boot2_cloud_legacy_expectations_against_synthetic_report(e2e, tmp_path):
    """The fixture's own expect block (minus post_checks) passes against a report shaped like the v2 run: the Boot 4
    umbrella replaces springfox / sleuth / the cloud train (recipe_source plan), hystrix stays (guide, still-present —
    advisories are LLM hints + Handover in v2), one fix script drops @EnableCircuitBreaker."""
    exp = dict(e2e.load_expected("boot2-cloud-legacy")["expect"])
    exp.pop("post_checks")
    advisories = [dict(a, recipe_source="plan") for a in ADVISORY_REPORT["advisories"] if a["advisory_id"] != "hystrix"]
    advisories += [{"advisory_id": "hystrix", "library": "hystrix", "status": "eol", "policy": "guide",
                    "recipe_source": "none", "outcome": "still-present"},
                   {"advisory_id": "spring-cloud-bom", "policy": "recipe", "recipe_source": "plan",
                    "outcome": "replaced"}]
    handover = dict(ADVISORY_REPORT["handover"],
                    from_here=["Netflix Hystrix is discontinued (not in any current Spring Cloud train): Configure …",
                               "net.sf.ehcache:ehcache pinned at 2.10.9.2 — the new parent/Boot BOM no longer "
                               "manages it: confirm the version or replace"])
    review = {"verdict": "issues", "llm_usage": {"calls": 1}, "issues": [
        {"severity": "warning", "file": "module/comp-jar/pom.xml", "problem": "spring-cloud-starter-netflix-hystrix "
         "is pinned: Hystrix is EOL"}, {"severity": "warning", "file": "pom.xml", "problem": "springfox.version left"}]}
    phases = [{"phase": "verify", "ok": True, "attempts": [_att(1, "ec7cf32b91", outcome="build_ok")]}]
    rep = dict(ADVISORY_REPORT, advisories=advisories, handover=handover, structure_review=review, phases=phases,
               llm_usage={"calls": 2})
    (tmp_path / exp["kb"]["dir"]).mkdir(parents=True)
    def ev(r):
        return e2e.evaluate(e2e.FixtureResult(fixture="x", exit_code=0), exp, r, [], None, tmp_path)
    assert ev(rep) == []
    # the v1 shape (hystrix converted by an advisory recipe) no longer satisfies the fixture
    v1 = dict(rep, advisories=[dict(a, policy="recipe", outcome="replaced") if a["advisory_id"] == "hystrix" else a
                               for a in advisories])
    assert "advisory hystrix: policy='recipe' expected 'guide'" in ev(v1)


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
    assert exp["kb"]["dir"] == "out/e2e/kb-boot2-method-rename"
    assert e2e.kb_dir_to_clear(exp) == (REPO / "out/e2e/kb-boot2-method-rename").resolve()
    assert e2e.main(["--expect-only", "--only", "boot2-method-rename"]) == 0
    line = capsys.readouterr().out.strip()
    # v2: 1 review call + 1 fix-script call for the one case (both call sites), the script stored as v1
    assert line.startswith("boot2-method-rename [X2] +sbmig.e2e.yml: success=True; exit=0; llm<=2")
    assert "optional_bumped=[com.company.legacy:legacy-web-lib 1.0.0→3.0.0]" in line
    assert "kb(dir=out/e2e/kb-boot2-method-rename)" in line
    assert 'scripts(attempts={"augment": 0, "new": 1, "reuse": 0}, cases=1, stored=1)' in line


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


def test_file_same_as_post_check(e2e, tmp_path):
    """2026-10-23: `file_same_as` compares every match byte for byte with the as-is fixture file."""
    proj, fixture = tmp_path / "p", tmp_path / "f"
    (proj / "back/target/classes").mkdir(parents=True)
    (fixture / "STATIC/resources").mkdir(parents=True)
    (fixture / "STATIC/resources/sample.xlsx").write_bytes(b"PK\x03\x04\xff\x00")
    (proj / "back/target/classes/sample.xlsx").write_bytes(b"PK\x03\x04\xff\x00")
    check = {"file_same_as": {"path": "back/target/classes/*.xlsx", "source": "STATIC/resources/sample.xlsx"}}
    assert e2e.run_post_check(check, proj, fixture) is None
    (proj / "back/target/classes/sample.xlsx").write_bytes(b"PK\x03\x04\xef\xbf\xbd\x00")   # filtered = corrupted
    assert "differs from STATIC/resources/sample.xlsx (6 bytes)" in e2e.run_post_check(check, proj, fixture)
    assert "as-is source" in e2e.run_post_check(check, proj, None)
    missing = {"file_same_as": {"path": "back/nope.xlsx", "source": "STATIC/resources/sample.xlsx"}}
    assert "not found" in e2e.run_post_check(missing, proj, fixture)


# ---------------------------------------------------------------- error_entries expectation (v2.1, 2026-10-27)
JAXB = "compile|package_does_not_exist|jakarta.xml.bind"


def _try(n, rnd, source, result, **kw):
    return {"n": n, "round": rnd, "source": source, "result": result, "applied": kw.pop("applied", "edits"), **kw}


EE_REPORT = {"error_entries": [
    {"id": "e1", "case_id": "c1aaaaaaaa", "file": "src/main/java/a/web/MemberController.java", "line": 4,
     "status": "resolved", "seen": "compile", "resolved": "r2 try 2", "script": "cases/c1aaaaaaaa/fix_v2.py",
     "tries": [_try(1, 1, "llm-new", "partial", script_version=1, llm_calls=1),
               _try(2, 2, "llm-augment", "build_ok", script_version=2, applied="script", llm_calls=1)]},
    {"id": "e2", "case_id": "c1aaaaaaaa", "file": "src/main/java/a/web/OtherController.java", "status": "resolved",
     "seen": "compile", "resolved": "r2 try 2",
     "tries": [_try(1, 1, "llm-new", "partial", script_version=1, llm_calls=1),
               _try(2, 2, "llm-augment", "build_ok", script_version=2, applied="script", llm_calls=1)]},
    {"id": "e3", "case_id": "7a52b1cea8", "l2": JAXB, "file": "src/main/java/com/example/legacy/util/Dates.java",
     "status": "resolved", "seen": "build", "tries": [_try(1, 1, "kb-script", "build_ok", script_version=1,
                                                             applied="script")]},
    {"id": "p1", "seen": "restructure", "status": "pom-verify", "file": "pom.xml", "note": "parent → base-parent"},
    {"id": "e4", "case_id": "dddddddddd", "file": "src/X.java", "status": "unresolved", "seen": "fix r2",
     "tries": [_try(1, 2, "llm-edit", "no_change", no_script_reason="one-off")]},
]}


def _kb_v21(kb: Path, cases: int = 1, tries: int = 2) -> None:
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "errors.jsonl").write_text("".join(json.dumps({"id": f"k{i}", "tries": [{}] * (tries if i == 0 else 0)})
                                             + "\n" for i in range(2)), encoding="utf-8")
    for i in range(cases):
        (kb / "cases" / f"case{i}").mkdir(parents=True)
        (kb / "cases" / f"case{i}" / "case.json").write_text("{}", encoding="utf-8")
    (kb / "cases.md").write_text("# KB cases\n", encoding="utf-8")


def test_error_entries_expectation_passes(e2e, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for n in ("errors.jsonl", "errors.md"):
        (run_dir / n).write_text("{}\n", encoding="utf-8")
    kb = tmp_path / "kb"
    _kb_v21(kb)
    want = {"min": 5, "max": 5, "status": {"resolved": 3, "pom-verify": 1, "unresolved": 1}, "open_max": 1,
            "status_min": {"resolved": 2}, "status_max": {"blocked": 0},
            "seen": {"compile": 2, "restructure": 1}, "seen_min": {"build": 1},
            "cases": [{"case": "c1aaaaaaaa", "entries_min": 2, "status": "resolved",
                       "sources": ["llm-new", "llm-augment"], "results": ["partial", "build_ok"], "versions": [1, 2],
                       "applied": "script", "llm_calls": 2, "resolved": "regex:r\\d+ try \\d+",
                       "script": "regex:cases/c1aaaaaaaa/fix_v\\d\\.py"},
                      {"l2": JAXB, "sources": ["kb-script"], "llm_calls": 0},
                      {"case": "dddddddddd", "status": ["unresolved", "blocked"], "sources_contain": ["llm-edit"]}],
            "records": [{"file": "regex:.*/MemberController\\.java", "status": "resolved", "seen": "compile",
                         "tries_min": 2, "sources": ["llm-new", "llm-augment"], "results": ["build_ok"]},
                        {"status": "pom-verify", "note": "regex:parent .*"}],
            "run_files": ["errors.jsonl", "errors.md"], "kb_errors_min": 2, "kb_tries_min": 2, "kb_cases_min": 1}
    assert e2e.evaluate_error_entries(want, EE_REPORT, run_dir, kb) == []
    assert e2e.format_error_counts(e2e.error_entries(EE_REPORT)) == "unresolved 1, pom-verify 1, resolved 3"
    assert len(e2e.case_tries([e for e in EE_REPORT["error_entries"] if e.get("case_id") == "c1aaaaaaaa"])) == 2


def test_error_entries_expectation_mismatches(e2e, tmp_path):
    want = {"min": 6, "status": {"resolved": 2}, "open_max": 0, "seen": {"build": 2},
            "cases": [{"case": "c1aaaaaaaa", "sources": ["llm-augment"], "results": ["build_ok", "partial"],
                       "versions": [2], "llm_calls": 1, "status": "unresolved"},
                      {"l2": "compile|x|y"}],
            "records": [{"file": "regex:.*/Nope\\.java"}],
            "run_files": ["errors.md"], "kb_errors_min": 1, "kb_cases_min": 1}
    text = "\n".join(e2e.evaluate_error_entries(want, EE_REPORT, None, tmp_path / "nokb"))
    for needle in ("error_entries=5 < min 6", "status resolved=3 expected == 2", "open=1 > max 0",
                   "seen build=1 expected == 2", "case c1aaaaaaaa: sources ['llm-new', 'llm-augment'] expected",
                   "lack the sequence ['build_ok', 'partial']", "script versions [1, 2] expected [2]",
                   "llm_calls=2 expected 1", "case c1aaaaaaaa: status ['resolved'] expected ['unresolved']",
                   f"case {e2e.case_ref('compile|x|y')}: no entry", "no error entry matching",
                   "run file errors.md missing", "kb errors.jsonl lines=0 < min 1", "kb cases=0 < min 1",
                   "kb cases.md missing"):
        assert needle in text, needle
    assert e2e.evaluate_error_entries({}, {}, None, None) == []        # missing report fields never crash


def test_legacy_error_cases_are_mapped_onto_the_error_list(e2e, tmp_path):
    """The two fixtures that still carry `error_cases` (boot2-fix-failures, java6-legacy) are evaluated against the
    v2.1 error list; fields without an equivalent are reported, never ignored."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for n in ("errors.jsonl", "errors.md"):
        (run_dir / n).write_text("{}\n", encoding="utf-8")
    kb = tmp_path / "kb"
    _kb_v21(kb)
    fix_failures = {"min": 2, "status_min": {"resolved": 2},
                    "records": [{"file": "regex:.*/web/MemberController\\.java", "status": "resolved",
                                 "fix_method": "script_augment", "attempt_outcomes": ["partial", "build_ok"]}],
                    "run_files": ["error_cases.jsonl", "fix_attempts.jsonl", "error_cases.csv"],
                    "kb_errors_min": 2, "kb_attempts_min": 2, "kb_cases_min": 1}
    assert e2e.evaluate_error_cases(fix_failures, EE_REPORT, run_dir, kb) == []
    java6 = {"records": [{"file": "regex:.*/com/example/legacy/util/Dates\\.java", "status": "resolved",
                          "fix_method": "script_reuse"}]}
    assert e2e.evaluate_error_cases(java6, EE_REPORT, run_dir, kb) == []
    mapped, gaps = e2e.legacy_error_cases_want({"status": {"resolved": 1, "resolved_indirectly": 2, "not_visible": 1},
                                                "records": [{"change_kind": "text_replace", "fix_method": "handler"}]})
    assert mapped["status"] == {"resolved": 3, "unresolved": 1}
    assert gaps == ["error_cases record field 'change_kind' has no v2.1 equivalent",
                    "error_cases record fix_method 'handler' has no v2.1 equivalent"]
    mm = e2e.evaluate_error_cases({"min": 9, "records": [{"status": "resolved", "fix_method": "script_reuse",
                                                          "file": "regex:.*/MemberController\\.java"}]},
                                  EE_REPORT, None, None)
    assert any("error_cases=5 < min 9" in m for m in mm) and any("no error entry matching" in m for m in mm)
    assert e2e.evaluate_error_cases({}, {}, None, None) == []


def test_fixture_without_root_pom_is_discovered_and_war_libs_satisfy_preconditions(e2e, tmp_path):
    """2026-10-23: ear-sibling-poms has no root pom.xml (intake generates it) — it is still an e2e fixture; a required
    library that is a WAR (legacy-base-web) counts as installed when its .war is in the local repository."""
    assert "ear-sibling-poms" in e2e.discover_fixtures()
    base = tmp_path / "com" / "company" / "legacy" / "legacy-base-web" / "1.0.0"
    base.mkdir(parents=True)
    want = {"requires_libs": ["com.company.legacy:legacy-base-web:1.0.0"]}
    assert "not in" in (e2e.preflight("x", want, tmp_path) or "")
    (base / "legacy-base-web-1.0.0.war").write_bytes(b"PK")
    assert e2e.preflight("x", want, tmp_path) is None


# ---------------------------------------------------------------------------------------------- v2 (flow-v2)
CLIENT_IP_L2 = "compile|cannot_find_symbol|clientIp"


def _store(kb: Path, case: str, version: int, code: str, **meta) -> None:
    """v2.1 KB layout: <kb>/cases/<case>/fix_v<N>.py + the CaseScript in case.json."""
    d = kb / "cases" / case
    d.mkdir(parents=True, exist_ok=True)
    (d / f"fix_v{version}.py").write_text(code, encoding="utf-8")
    cj = d / "case.json"
    data = json.loads(cj.read_text(encoding="utf-8")) if cj.is_file() else {"case_id": case, "scripts": []}
    data["scripts"].append({"version": version, "file": f"fix_v{version}.py", **meta})
    cj.write_text(json.dumps(data), encoding="utf-8")


def test_case_ref_hashes_l2_like_the_catalog(e2e):
    from sbmig.errors.common import case_id_for

    assert e2e.case_ref({"l2": CLIENT_IP_L2}) == case_id_for(CLIENT_IP_L2) == "8bb4fd4a4a"
    assert e2e.case_ref({"case": "abc0123456"}) == "abc0123456" and e2e.case_ref("8bb4fd4a4a") == "8bb4fd4a4a"
    assert e2e.case_ref(CLIENT_IP_L2) == "8bb4fd4a4a" and e2e.case_ref({}) == ""


def test_scripts_expectation_augment_and_reuse(e2e, tmp_path):
    kb = tmp_path / "kb"
    _store(kb, "8bb4fd4a4a", 1, "def fix(p):\n    p.replace('a', 'clientIp(request)', 'x')\n", source="llm-new",
           successes=0, last_result="partial", status="unverified", projects=[])
    _store(kb, "8bb4fd4a4a", 2, "def fix(p):\n    pass  # remoteClientIp(\n", source="llm-augment", successes=2,
           projects=["member-api", "shop"], last_result="build_ok", status="proven")
    report = {"phases": [{"phase": "verify", "attempts": [
        _att(1, "8bb4fd4a4a", outcome="partial", version=1),
        _att(1, "8bb4fd4a4a", outcome="build_ok", mode="augment", version=2, iteration=2),
        _att(2, "7a52b1cea8", outcome="build_ok", mode="reuse", version=1, calls=0, iteration=2),
        {"id": "verify-2-3", "cluster_id": "case:zz", "outcome": "apply_failed"}]}]}   # no script_mode: not counted
    want = {"attempts": {"new": 1, "augment": 1, "reuse": 1}, "reuse_without_llm": True,
            "cases": [{"l2": CLIENT_IP_L2, "modes": ["new", "augment"], "outcomes": ["partial", "build_ok"],
                       "versions": [1, 2], "llm_calls": 2},
                      {"case": "7a52b1cea8", "modes": ["reuse"], "llm_calls": 0}],
            "stored": [{"l2": CLIENT_IP_L2, "version": 1, "parent": None, "partials_min": 0, "successes_max": 0,
                        "code_contains": ["clientIp(request)"]},
                       {"l2": CLIENT_IP_L2, "version": 2, "parent": 1, "status": "active", "successes_min": 2,
                        "success_projects_min": 2, "last_outcome": "build_ok", "code_contains": ["remoteClientIp("]}],
            "versions": {CLIENT_IP_L2: 2}}
    assert e2e.evaluate_scripts(want, report, kb) == []
    assert e2e.format_script_modes(e2e.script_attempts(report)) == "new 1, aug 1, reuse 1"
    bad = dict(want, attempts={"new": 2}, attempts_max={"augment": 0},
               cases=[{"l2": CLIENT_IP_L2, "modes": ["augment"], "outcomes": ["build_ok", "partial"]},
                      {"l2": "compile|x|y"}],
               stored=[{"l2": CLIENT_IP_L2, "version": 2, "parent": None, "successes_min": 3},
                       {"l2": CLIENT_IP_L2, "version": 3}], versions={CLIENT_IP_L2: 3})
    text = "\n".join(e2e.evaluate_scripts(bad, report, kb))
    assert "script attempts new=1 expected == 2" in text and "script attempts augment=1 expected <= 0" in text
    assert "case 8bb4fd4a4a: modes ['new', 'augment'] expected ['augment']" in text
    assert "lack the sequence ['build_ok', 'partial']" in text
    assert f"case {e2e.case_ref('compile|x|y')}: no script attempt" in text
    assert "stored script 8bb4fd4a4a v2: parent=1 expected None" in text and "successes=2 < min 3" in text
    assert "stored script 8bb4fd4a4a v3 missing (versions: [1, 2])" in text
    assert "stored versions of case 8bb4fd4a4a: [1, 2] expected 3" in text
    report["phases"][0]["attempts"][2]["llm_usage"]["calls"] = 1
    assert any("reuse attempt verify-2-2 made 1 LLM call(s)" in m for m in e2e.evaluate_scripts(want, report, kb))
    assert e2e.evaluate_scripts({"stored": [{"case": "abc", "version": 1}]}, report, tmp_path / "nope") == \
        [f"scripts: kb dir {tmp_path / 'nope'} missing"]


def test_structure_review_expectation(e2e):
    rv = {"verdict": "issues", "commit": "c0ffee", "reverted": False, "llm_usage": {"calls": 1},
          "issues": [{"severity": "critical", "file": "module/comp-jar/pom.xml", "problem": "MS949 sources",
                      "fix_hint": "set MS949"}, {"severity": "warning", "file": "pom.xml", "problem": "old pin"}],
          "script_result": {"status": "ok", "changed_files": ["module/comp-jar/pom.xml"]}}
    want = {"verdict": ["issues", "ok"], "critical": 1, "warnings_min": 1, "warnings_max": 1, "sbmig_check": False,
            "issues_contain": ["regex:critical module/comp-jar/pom.xml: MS949.* -> set MS949", "old pin"],
            "script_status": "ok", "committed": True, "reverted": False, "changed_files": ["module/comp-jar/pom.xml"],
            "llm_calls_max": 1}
    assert e2e.evaluate_structure_review(want, {"structure_review": rv}) == []
    text = "\n".join(e2e.evaluate_structure_review(
        dict(want, verdict="ok", critical=0, script_status="error", committed=False, changed_files=["pom.xml"],
             llm_calls_max=0, issues_contain=["regex:critical pom.xml: .*"]), {"structure_review": rv}))
    assert "structure_review verdict='issues' expected 'ok'" in text and "structure_review critical=1 expected == 0" in text
    assert "script_status=ok expected error" in text and "expected committed=False" in text
    assert "did not change pom.xml" in text and "llm_calls=1 > max 0" in text
    assert "issues lack 'regex:critical pom.xml: .*'" in text
    assert e2e.evaluate_structure_review({"verdict": "ok"}, {}) == ["no structure_review in report.json"]
    plain = {"verdict": "ok", "issues": [], "llm_usage": {"calls": 1}}
    assert e2e.evaluate_structure_review({"script_status": "none", "critical": 0}, {"structure_review": plain}) == []


def test_kb_after_keeps_the_shared_kb_and_needs_its_filler(e2e, tmp_path, monkeypatch):
    """v2 reuse scenario: a fixture with `kb.after` continues the KB another fixture filled earlier in the same e2e
    run — its dir is never cleared, and without that fixture the run is a precondition mismatch."""
    exp = {"kb": {"dir": "out/e2e/kb-shared", "after": "pure-java"}}
    assert e2e.kb_dir_to_clear(exp, tmp_path) is None
    assert e2e.kb_dir_to_clear({"kb": {"dir": "out/e2e/kb-shared"}}, tmp_path) == (tmp_path / "out/e2e/kb-shared").resolve()
    assert e2e.kb_after_problem(exp, ["pure-java"]) is None and e2e.kb_after_problem(exp, None) is None
    assert "precondition: pure-java must run before" in e2e.kb_after_problem(exp, ["boot2"])
    monkeypatch.setattr(e2e, "REPO", tmp_path)
    monkeypatch.setattr(e2e, "preflight", lambda *a, **k: None)
    shared = tmp_path / "out/e2e/kb-shared/scripts"
    shared.mkdir(parents=True)
    expected = {"expect": dict(exp, success=True, exit_code=0)}
    r = e2e.run_fixture("java6-legacy", _args(e2e, tmp_path), cmd=["false"], expected=expected, ran_before=[])
    assert r.expect_ok is False and r.mismatches[0].startswith("precondition: pure-java must run before")
    assert shared.is_dir()                                                   # kept
    real = e2e.load_expected("java6-legacy")["expect"]["kb"]
    assert real == {"dir": "out/e2e/kb-jaxb", "after": "pure-java"}
    assert e2e.load_expected("pure-java")["expect"]["kb"] == {"dir": "out/e2e/kb-jaxb"}     # cleared before pure-java
