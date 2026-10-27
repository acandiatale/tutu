"""Round 10: the fleet's infrastructure guard (preflight + circuit breaker), disk lifecycle, per-project logs,
state keyed by input path, manifest encodings, summaries an Excel lock cannot stop, `status --attention`."""
from __future__ import annotations

import errno
import json
import logging
from pathlib import Path

from typer.testing import CliRunner

from sbmig.core.models import FleetState
from sbmig.fleet import FleetRunner, load_manifest
from sbmig.fleet.cleanup import build_output_dirs, cleanup_run, plan_clean
from sbmig.fleet.preflight import (Check, check_disk, check_llm, check_maven_java, check_repository, check_settings,
                                   java_major, run_preflight)

from .conftest import make_project
from .test_runner import FakeRun, report


def _cfg(fleet_cfg, **fleet):
    return fleet_cfg.model_copy(update={"fleet": fleet_cfg.fleet.model_copy(update=fleet)})


def _state(out) -> FleetState:
    return FleetState.model_validate_json((out / "fleet" / "state.json").read_text(encoding="utf-8"))


def infra(kind: str = "llm", code_aborted: bool = False):
    def run(project, cfg):
        r = report(project.name, success=False, aborted=code_aborted)
        r.flags.append(f"infra:{kind}")
        return r
    return run


class Checks:
    """A preflight stand-in: one list of checks per call (the last one repeats)."""

    def __init__(self, *rounds):
        self.rounds, self.calls = list(rounds), 0

    def __call__(self):
        self.calls += 1
        return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]


OK = [Check("maven", True, "Apache Maven 3.9.9"), Check("llm", True, "gpt-6-luna HTTP 200 0.4s")]
LLM_DOWN = [Check("llm", False, "transport cause=ConnectionError: [WinError 10061] ?", kind="llm", transient=True)]
JDK_17 = [Check("java", False, "17 but build.require_java_major is 21", kind="jdk")]


# ------------------------------------------------------------------ preflight before the first project
def test_preflight_configuration_error_stops_before_any_project(fleet_cfg, projects_dir, tmp_path):
    lines, sleeps = [], []
    fake = FakeRun()
    runner = FleetRunner(_cfg(fleet_cfg), projects_dir, tmp_path / "out", run_project=fake, echo=lines.append,
                         preflight=Checks(OK[:1] + JDK_17), sleep=sleeps.append)
    state = runner.run()
    assert fake.calls == [] and sleeps == [] and runner.exit_code == 3
    assert "preflight: maven      ok   Apache Maven 3.9.9" in lines
    assert "preflight: java       FAIL 17 but build.require_java_major is 21" in lines
    stop = "fleet stopped: infrastructure jdk unavailable since preflight - fix it and run sbmig fleet again"
    assert lines[-1] == stop and stop in state.notes and state.finished_at is None
    assert [p.status for p in _state(tmp_path / "out").projects] == ["pending"] * 3


def test_preflight_outage_waits_and_rechecks(fleet_cfg, projects_dir, tmp_path):
    lines, sleeps = [], []
    fake = FakeRun()
    pre = Checks(LLM_DOWN, OK)
    runner = FleetRunner(_cfg(fleet_cfg, infra_retry_minutes=15, infra_max_waits=4), projects_dir, tmp_path / "out",
                         run_project=fake, echo=lines.append, preflight=pre, sleep=sleeps.append)
    state = runner.run()
    assert sleeps == [900.0] and pre.calls == 2 and len(fake.calls) == 3 and runner.exit_code == 0
    assert any(x.startswith("fleet: infrastructure llm unavailable since preflight - waiting 15 min, then "
                            "re-checking (1/4)") for x in lines)
    assert "fleet: infrastructure checks passed - resuming" in lines and state.finished_at is not None


# ------------------------------------------------------------------ circuit breaker during the fleet
def test_infra_projects_go_back_to_pending_and_the_fleet_resumes_after_the_recheck(fleet_cfg, tmp_path):
    d = tmp_path / "projects"
    for n in ("p1", "p2", "p3", "p4"):
        make_project(d, n)
    lines, sleeps = [], []
    outage = {"on": True}

    def maybe_down(project, cfg):
        if outage["on"]:
            if project.name == "p2":
                outage["on"] = False       # the gateway comes back while the fleet waits (see sleep below)
            return infra("llm")(project, cfg)
        return report(project.name)

    def sleep(s):
        sleeps.append(s)

    fake = FakeRun(**{n: maybe_down for n in ("p1", "p2", "p3", "p4")})
    pre = Checks(OK)
    runner = FleetRunner(_cfg(fleet_cfg, infra_failure_limit=2, infra_retry_minutes=10), d, tmp_path / "out",
                         run_project=fake, echo=lines.append, preflight=pre, sleep=sleep)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["p1", "p2", "p1", "p2", "p3", "p4"]   # deferred ones first after the wait
    assert sleeps == [600.0] and pre.calls == 2                                 # start + the re-check
    assert [p.status for p in state.projects] == ["success"] * 4 and runner.exit_code == 0
    p1 = state.projects[0]
    assert p1.attempt == 2 and p1.run_id.endswith("-001-p1-r2") and p1.error is None
    first = next(x for x in lines if x.startswith("SBMIG-RESULT"))
    assert first.startswith(f"SBMIG-RESULT run={state.fleet_id}-001-p1 exit=2 build=FAIL") and "attn=infra:llm" in first
    assert any("p1: pending" in x and "infra:llm" in x for x in lines)
    assert "fleet: infrastructure llm unavailable since p1 - waiting 10 min, then re-checking (1/4)" in lines


def test_infra_outage_that_lasts_stops_the_fleet_and_a_rerun_resumes(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    lines, sleeps = [], []
    fake = FakeRun(alpha=infra("repository"), beta=infra("repository"), gamma=infra("repository"))
    pre = Checks(OK, [Check("repository", False, "http://nexus/ unreachable", kind="repository", transient=True)])
    runner = FleetRunner(_cfg(fleet_cfg, infra_failure_limit=2, infra_retry_minutes=5, infra_max_waits=3),
                         projects_dir, out, run_project=fake, echo=lines.append, preflight=pre, sleep=sleeps.append)
    state = runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta"] and sleeps == [300.0] * 3 and runner.exit_code == 3
    stop = "fleet stopped: infrastructure repository unavailable since alpha - fix it and run sbmig fleet again"
    assert lines[-1] == stop and stop in state.notes
    st = _state(out)
    assert [(p.status, p.error) for p in st.projects] == [("pending", "infra:repository"),
                                                          ("pending", "infra:repository"), ("pending", None)]
    assert st.finished_at is None
    # the infrastructure is back: a normal re-run resumes the pending projects (not `--retry-failed`)
    fake2 = FakeRun()
    runner2 = FleetRunner(_cfg(fleet_cfg), projects_dir, out, run_project=fake2, preflight=Checks(OK))
    state2 = runner2.run()
    assert [c[0] for c in fake2.calls] == ["alpha", "beta", "gamma"] and runner2.exit_code == 0
    assert state2.projects[0].attempt == 2 and state2.finished_at is not None


def test_retry_minutes_zero_stops_at_once_and_config_errors_never_wait(fleet_cfg, projects_dir, tmp_path):
    lines, sleeps = [], []
    fake = FakeRun(alpha=infra("maven"), beta=infra("maven"))
    runner = FleetRunner(_cfg(fleet_cfg, infra_failure_limit=2, infra_retry_minutes=0), projects_dir, tmp_path / "o1",
                         run_project=fake, echo=lines.append, preflight=Checks(OK), sleep=sleeps.append)
    runner.run()
    assert [c[0] for c in fake.calls] == ["alpha", "beta"] and sleeps == [] and runner.exit_code == 3
    assert lines[-1].startswith("fleet stopped: infrastructure maven unavailable since alpha")
    # the re-check finds a configuration error (another JDK): stop after the first wait, do not wait again
    lines.clear()
    fake = FakeRun(alpha=infra("jdk"), beta=infra("jdk"))
    runner = FleetRunner(_cfg(fleet_cfg, infra_failure_limit=2, infra_retry_minutes=1, infra_max_waits=5),
                         projects_dir, tmp_path / "o2", run_project=fake, echo=lines.append,
                         preflight=Checks(OK, JDK_17), sleep=sleeps.append)
    runner.run()
    assert sleeps == [60.0] and lines[-1].startswith("fleet stopped: infrastructure jdk unavailable since alpha")


def test_one_infra_failure_between_healthy_projects_does_not_wait(fleet_cfg, projects_dir, tmp_path):
    sleeps = []
    fake = FakeRun(beta=infra("llm"))
    runner = FleetRunner(_cfg(fleet_cfg, infra_failure_limit=2), projects_dir, tmp_path / "out", run_project=fake,
                         preflight=Checks(OK), sleep=sleeps.append)
    state = runner.run()
    assert sleeps == [] and [c[0] for c in fake.calls] == ["alpha", "beta", "gamma"]
    assert [p.status for p in state.projects] == ["success", "pending", "success"]
    assert state.projects[1].error == "infra:llm" and state.finished_at is None and runner.exit_code == 2


def test_green_run_with_llm_trouble_is_a_success_and_older_flags_count(fleet_cfg, projects_dir, tmp_path):
    def green(project, cfg):
        r = report(project.name)
        r.flags.append("llm-unavailable")
        return r

    def no_mvn(project, cfg):
        r = report(project.name, aborted=True)
        r.flags.append("mvn-not-found")
        return r

    fake = FakeRun(alpha=green, beta=no_mvn)
    state = FleetRunner(_cfg(fleet_cfg), projects_dir, tmp_path / "out", run_project=fake).run()
    assert state.projects[0].status == "success"
    assert (state.projects[1].status, state.projects[1].error) == ("pending", "infra:maven")


def test_full_disk_crash_is_infra_and_the_disk_check_runs_before_each_project(fleet_cfg, projects_dir, tmp_path):
    def disk_full(project, cfg):
        raise OSError(errno.ENOSPC, "No space left on device")

    fake = FakeRun(alpha=disk_full)
    state = FleetRunner(_cfg(fleet_cfg), projects_dir, tmp_path / "o1", run_project=fake).run()
    assert (state.projects[0].status, state.projects[0].error) == ("pending", "infra:disk")
    assert state.projects[0].exit_code == 3
    # the drive is full before beta: the fleet pauses (retry 0 → stops) without starting it
    lines = []
    checks = iter([Check("disk", True, "9 GB"), Check("disk", False, "1.2 GB free on D:\\out (min 5)", kind="disk",
                                                      transient=True)])
    fake = FakeRun()
    runner = FleetRunner(_cfg(fleet_cfg, infra_retry_minutes=0), projects_dir, tmp_path / "o2", run_project=fake,
                         echo=lines.append, disk_check=lambda: next(checks))
    runner.run()
    assert [c[0] for c in fake.calls] == ["alpha"] and runner.exit_code == 3
    assert "preflight: disk       FAIL 1.2 GB free on D:\\out (min 5)" in lines
    assert lines[-1].startswith("fleet stopped: infrastructure disk unavailable since beta")


# ------------------------------------------------------------------ preflight checks
def test_maven_java_check_parses_mvn_v(fleet_cfg):
    cfg = fleet_cfg
    ok = check_maven_java(cfg, lambda c: (True, "Apache Maven 3.9.9 (abc); Java version: 21.0.4"))
    assert [(c.name, c.ok) for c in ok] == [("maven", True), ("java", True)]
    bad = check_maven_java(cfg, lambda c: (True, "Apache Maven 3.9.9 (abc); Java version: 17.0.2"))
    assert not bad[1].ok and bad[1].kind == "jdk" and not bad[1].transient
    gone = check_maven_java(cfg, lambda c: (False, "mvn not found (mvn): [WinError 2]"))
    assert gone[0].failed and gone[0].kind == "maven" and gone[1].skipped
    assert java_major("Java version: 1.8.0_402, vendor") == 8 and java_major("nothing") is None


def test_settings_repository_llm_and_disk_checks(fleet_cfg, tmp_path):
    from requests.exceptions import ConnectionError as ReqConnectionError
    from requests.exceptions import SSLError

    settings = tmp_path / "settings.xml"
    settings.write_text("""<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors><mirror><id>nexus</id><mirrorOf>external:*</mirrorOf><url>http://nexus.local/repository/public/</url>
  </mirror></mirrors>
  <proxies><proxy><active>true</active><protocol>http</protocol><host>proxy.local</host><port>3128</port>
  <nonProxyHosts>*.local|localhost</nonProxyHosts></proxy></proxies>
</settings>""", encoding="utf-8")
    cfg = fleet_cfg.model_copy(update={"build": fleet_cfg.build.model_copy(update={"settings_file": str(settings)})})
    s = check_settings(cfg)
    assert s.ok and "mirror external:* -> http://nexus.local/repository/public/" in s.detail
    calls = []

    def get(url, proxies, timeout):
        calls.append((url, proxies))
        return 401
    r = check_repository(s, http_get=get)
    assert r.ok and "HTTP 401" in r.detail and calls == [("http://nexus.local/repository/public/", {})]  # nonProxyHosts
    r = check_repository(s, http_get=lambda *a: 503)
    assert r.failed and r.kind == "repository" and r.transient

    def refused(*a):
        raise ReqConnectionError("Failed to establish a new connection: [WinError 10061] ?")
    r = check_repository(s, http_get=refused)
    assert r.failed and "cause=ConnectionError" in r.detail and r.transient

    def tls(*a):
        raise SSLError("certificate verify failed")
    assert check_repository(s, http_get=tls).ok                     # the server answered the handshake
    missing = cfg.model_copy(update={"build": cfg.build.model_copy(update={"settings_file": str(tmp_path / "no.xml")})})
    m = check_settings(missing)
    assert m.failed and m.kind == "repository" and not m.transient
    assert check_repository(m).skipped
    assert check_llm(fleet_cfg).skipped                                # the e2e config uses the fake provider
    company = fleet_cfg.model_copy(update={"llm": fleet_cfg.llm.model_copy(update={"provider": "company"})})

    class Res:
        ok, kind, problem, transient, status, model, latency_s, hints = False, "transport", "cause=X", True, None, \
            "m", 0.1, []
    c = check_llm(company, probe=lambda cfg: Res())
    assert c.failed and c.kind == "llm" and c.transient and "transport cause=X" in c.detail
    # v2.1: max_llm_calls 0 = no limit (probed); -1 = no LLM call at all (not probed)
    unlimited = company.model_copy(update={"loop": company.loop.model_copy(update={"max_llm_calls": 0})})
    assert check_llm(unlimited, probe=lambda cfg: Res()).failed
    off = company.model_copy(update={"loop": company.loop.model_copy(update={"max_llm_calls": -1})})
    assert check_llm(off, probe=lambda cfg: Res()).skipped
    assert check_disk(tmp_path, 10 ** 6).failed and check_disk(tmp_path, 0).skipped
    lines = []
    mvn_v = "Apache Maven 3.9.9; Java version: 21.0.1"
    checks = run_preflight(company, tmp_path, echo=lines.append, maven=lambda c: (True, mvn_v),
                           probe=lambda cfg: Res())
    assert [c.name for c in checks] == ["disk", "maven", "java", "settings", "repository", "llm"]
    assert len(lines) == 6 and all(x.startswith("preflight: ") and x.isascii() for x in lines)


# ------------------------------------------------------------------ disk lifecycle
def _project_with_build_output(run_dir: Path) -> Path:
    proj = run_dir / "project"
    for mod in ("", "app-back/"):
        (proj / mod / "target" / "classes").mkdir(parents=True)
        (proj / mod / "pom.xml").write_text("<project/>")
        (proj / mod / "target" / "app.jar").write_bytes(b"x" * 4096)
    pkg = proj / "src" / "main" / "java" / "com" / "co" / "target"           # a Java package named target
    pkg.mkdir(parents=True)
    (pkg / "Target.java").write_text("package com.co.target; class Target {}")
    return proj


def test_cleanup_build_output_and_workspace(tmp_path):
    rd = tmp_path / "run1"
    proj = _project_with_build_output(rd)
    assert sorted(p.relative_to(proj).as_posix() for p in build_output_dirs(proj)) == ["app-back/target", "target"]
    res = cleanup_run(rd, "build-output")
    assert res["freed"] >= 8192 and not (proj / "target").exists() and not (proj / "app-back" / "target").exists()
    assert (proj / "src/main/java/com/co/target/Target.java").exists()
    # workspace without result.zip: the project copy is the only migrated code left → falls back to build-output
    rd2 = tmp_path / "run2"
    proj2 = _project_with_build_output(rd2)
    assert cleanup_run(rd2, "workspace")["mode"] == "build-output" and proj2.exists()
    (rd2 / "result.zip").write_bytes(b"PK..")
    (rd2 / "report.md").write_text("r")
    assert cleanup_run(rd2, "workspace")["mode"] == "workspace"
    assert not proj2.exists() and (rd2 / "result.zip").exists() and (rd2 / "report.md").exists()
    assert cleanup_run(rd2, "none")["removed"] == []


def test_fleet_cleans_up_after_each_project_and_logs_per_project(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"

    def run(project, cfg):
        rd = out / fake.calls[-1][2]                     # FakeRun recorded (name, max_llm_calls, run_id)
        _project_with_build_output(rd)
        logging.getLogger("sbmig.loop.test").debug("debug line of %s", project.name)
        return report(project.name)

    fake = FakeRun(alpha=run, beta=run, gamma=run)
    from sbmig.cli import attach_file_log, detach_file_log
    fleet_log = attach_file_log(out / "fleet" / "sbmig.log", level=logging.INFO)
    try:
        state = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    finally:
        detach_file_log(fleet_log)
    for p in state.projects:
        rd = Path(p.out_dir)
        assert not (rd / "project" / "target").exists() and (rd / "project" / "src").exists()   # build-output
        text = (rd / "sbmig.log").read_text(encoding="utf-8")
        assert f"debug line of {p.name}" in text
        others = [o.name for o in state.projects if o.name != p.name]
        assert not any(f"debug line of {o}" in text for o in others)             # detached after its project
    fleet_text = fleet_log.read_text(encoding="utf-8")
    assert "debug line of" not in fleet_text and "SBMIG-RESULT run=" in fleet_text       # fleet/sbmig.log: INFO
    assert not any(getattr(h, "baseFilename", "").startswith(str(out.resolve()))
                   for h in logging.getLogger("sbmig").handlers)                        # nothing left attached


def test_plan_clean_finished_runs_uploads_records(tmp_path):
    import os
    import time as _time

    out = tmp_path / "web"
    ok = out / "web-20261020-101010-aaaaaa"
    bad = out / "web-20261020-111111-bbbbbb"
    live = out / "web-20261026-090000-cccccc"
    for rd, success in ((ok, True), (bad, False)):
        _project_with_build_output(rd)
        (rd / "report.json").write_text(json.dumps({"success": success, "blocked": [], "flags": []}))
    _project_with_build_output(live)                                           # no report.json: still running
    up = out / "_uploads"
    up.mkdir(parents=True)
    for rd in (ok, bad, live):
        (up / f"{rd.name}-proj.zip").write_bytes(b"PK" + b"0" * 100)
    stale = up / ".incoming-dead.part"
    stale.write_bytes(b"PK")
    old = _time.time() - 3 * 86400
    os.utime(stale, (old, old))
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "llm-0001.json").write_text("{}")
    items = plan_clean(out, record_dirs=[rec])
    kinds = sorted((i.kind, i.owner) for i in items)
    assert kinds == [("llm-record", ""), ("partial-upload", ""), ("project-copy", ok.name), ("project-copy", bad.name),
                     ("upload", ok.name), ("upload", bad.name)]
    keep = plan_clean(out, keep_failed=True)
    assert {i.owner for i in keep if i.kind == "project-copy"} == {ok.name}
    assert plan_clean(out, older_than_days=30) == []                         # everything is younger than that
    assert [i.kind for i in plan_clean(out, older_than_days=2)] == ["partial-upload"]


# ------------------------------------------------------------------ state keyed by input path, manifest encodings
def test_korean_and_renamed_folders_never_inherit_another_projects_result(fleet_cfg, tmp_path):
    d = tmp_path / "projects"
    make_project(d, "주문관리")
    make_project(d, "결제관리")
    names = [p.name for p in load_manifest(d)]
    assert len(set(names)) == 2 and all(n.startswith("project-") and n.isascii() for n in names)
    out = tmp_path / "out"
    fake = FakeRun()
    FleetRunner(fleet_cfg, d, out, run_project=fake).run()
    assert len(fake.calls) == 2
    # a third Korean folder appears and one is renamed: both are new projects (the old code matched them by the
    # shared slug name `project` and skipped them as already successful)
    make_project(d, "회원관리")
    (d / "결제관리").rename(d / "결제관리2")
    fake2 = FakeRun()
    state = FleetRunner(fleet_cfg, d, out, run_project=fake2).run()
    assert len(fake2.calls) == 2
    ran = {Path(p.input).name for p in state.projects if p.name in {c[0] for c in fake2.calls}}
    assert ran == {"회원관리", "결제관리2"}


def test_old_state_files_match_by_input(fleet_cfg, projects_dir, tmp_path):
    out = tmp_path / "out"
    FleetRunner(fleet_cfg, projects_dir, out, run_project=FakeRun()).run()
    st = _state(out)
    for p in st.projects:            # an earlier round's state: other names, same (resolved) inputs
        p.name = "old-" + p.name
    (out / "fleet" / "state.json").write_text(st.model_dump_json(), encoding="utf-8")
    fake = FakeRun()
    state = FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    assert fake.calls == [] and [p.name for p in state.projects] == ["alpha", "beta", "gamma"]


def test_manifests_in_excel_powershell_and_cp949_encodings(tmp_path):
    a = make_project(tmp_path, "주문")
    b = make_project(tmp_path, "beta")
    csv_bom = tmp_path / "excel.csv"
    csv_bom.write_bytes("name,input,tags\n주문,주문,boot\n,beta,\n".encode("utf-8-sig"))
    ps = load_manifest(csv_bom)
    assert [Path(p.input).name for p in ps] == ["주문", "beta"] and all(p.ok for p in ps)
    assert ps[0].tags == ["boot"] and ps[0].name.startswith("project-")
    utf16 = tmp_path / "ps.txt"
    utf16.write_bytes(f"{a}\r\n{b}\r\n".encode("utf-16"))                     # PowerShell 5.1 `>`
    assert [Path(p.input).name for p in load_manifest(utf16)] == ["주문", "beta"]
    cp949 = tmp_path / "list.txt"
    cp949.write_bytes("주문\nbeta\n".encode("cp949"))
    assert [Path(p.input).name for p in load_manifest(cp949)] == ["주문", "beta"]
    yml = tmp_path / "fleet.yml"
    yml.write_bytes("projects:\n  - {name: 주문 v2, input: 주문}\n".encode("utf-8-sig"))
    (p,) = load_manifest(yml)
    assert p.ok and p.name.startswith("v2-") and p.name.isascii()


# ------------------------------------------------------------------ summaries and status --attention
def test_a_locked_summary_csv_does_not_stop_the_other_summaries(fleet_cfg, projects_dir, tmp_path, monkeypatch):
    from sbmig.kb import store

    real = store.atomic_write

    def locked(path, text):
        if Path(path).name == "summary.csv":
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(path, text)

    monkeypatch.setattr(store, "atomic_write", locked)
    out = tmp_path / "out"
    FleetRunner(fleet_cfg, projects_dir, out, run_project=FakeRun(beta={"success": False})).run()
    fleet = out / "fleet"
    assert not (fleet / "summary.csv").exists()
    for f in ("summary.md", "summary.json", "learning.md", "state.json"):
        assert (fleet / f).exists(), f
    md = (fleet / "summary.md").read_text(encoding="utf-8")
    assert "## Needs attention" in md and "* `unresolved` (1): beta" in md


def test_status_attention_groups_by_code(fleet_cfg, projects_dir, tmp_path):
    from sbmig.fleet.cli import fleet_cli

    make_project(projects_dir, "delta")
    out = tmp_path / "out"

    def blocked(project, cfg):
        return report(project.name, success=False, blocked=1)

    fake = FakeRun(alpha=infra("llm"), beta=blocked, gamma={"success": False})
    FleetRunner(fleet_cfg, projects_dir, out, run_project=fake).run()
    r = CliRunner().invoke(fleet_cli, ["status", str(out), "--attention"], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].endswith(": 3 of 4 project(s) need attention")
    heads = [x for x in lines if not x.startswith("  ")][1:]
    assert heads == ["infra:llm (1)", "blocked:external_javax_api (1)", "unresolved (1)"]
    assert any(x.startswith("  alpha  pending  ") and "infra:llm" in x for x in lines)
    assert not any(x.strip().startswith("delta ") for x in lines)
