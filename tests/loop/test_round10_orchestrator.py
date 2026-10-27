"""Round 10 (2026-10-26) orchestrator items: A4 blocked keys keep the service directory and follow the restructure
move; A7 invariant 1 after an internal error in an MSA verify; A8 per-service test-skip fallbacks; A10 the JDK 21
preflight; A13 report before zip + the `run aborted:` line; A14 `infra:<kind>` flags."""
from __future__ import annotations

import logging
import shutil

from sbmig.cli import run_exit_code
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import (GAV, BlockReason, BuildError, CaseAction, CaseHit, CaseType, ErrorKind, KBMatch,
                               MigrationPlan, Phase, PlanStep, RunReport, StepKind, utcnow)
from sbmig.loop.blocked_registry import BlockedRegistry
from sbmig.loop.orchestrator import parse_renames
from sbmig.triage import cluster_errors

from .fakes import (FakeBuildRunner, FakeFixer, FakeKB, bad_build, journal_events, make_profile, ok_build,
                    v2_msa_plan, write_fix)
from .test_orchestrator import make_orch

GONE = "compile|cannot_find_symbol|class Gone"
FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"


def l1(file: str, kind: ErrorKind = ErrorKind.COMPILE, module: str = "demo") -> BuildError:
    """File-less L1; the L2 (case) per file name, so Foo and Bar are two cases (the services' SwaggerConfig one)."""
    return BuildError(kind=kind, message="cannot find symbol", file=file, line=3, module=module, symbol="class Gone",
                      signature=GONE, signature_generic="compile|cannot_find_symbol|" + file.rsplit("/", 1)[-1])


def pinned_block(c, case_id="case-x"):
    return KBMatch(case_hits=[CaseHit(case_id=case_id, errors=list(c.errors), pinned=True,
                                      action=CaseAction(kind="block", block_reason="unsupported_source"))])


# ------------------------------------------------------------------------------------------------ A4
def test_registry_keeps_msa_services_apart():
    reg = BlockedRegistry()
    a = cluster_errors([l1("svc-a/src/main/java/com/x/SwaggerConfig.java")])[0]
    b = cluster_errors([l1("svc-b/src/main/java/com/x/SwaggerConfig.java")])[0]
    reg.block(a, BlockReason.EXTERNAL_API_INCOMPATIBLE, Phase.SPRING)
    assert reg.covering(a) is not None
    assert reg.covering(b) is None and not reg.is_blocked_error(b.errors[0])


def test_registry_follows_the_restructure_move():
    reg = BlockedRegistry()
    old = cluster_errors([l1(FOO)])[0]
    new = cluster_errors([l1("demo-back/" + FOO)])[0]
    reg.block(old, BlockReason.UNSUPPORTED_SOURCE, Phase.SPRING)
    assert reg.covering(new) is None
    assert reg.note_moves({FOO: "demo-back/" + FOO, "pom.xml": "pom.xml"}) == 1
    assert reg.covering(new) is not None and reg.canonical_file(FOO) == "demo-back/" + FOO
    # a second move (wrap, then split) is followed through the chain
    reg.note_moves({"demo-back/" + FOO: "x-back/" + FOO})
    assert reg.covering(cluster_errors([l1("x-back/" + FOO)])[0]) is not None
    assert reg.canonical_file(FOO) == "x-back/" + FOO


def test_parse_renames():
    out = "R100\0src/a/A.java\0back/src/a/A.java\0M\0pom.xml\0R087\0src/B.java\0back/src/B.java\0A\0new.txt\0"
    assert parse_renames(out) == {"src/a/A.java": "back/src/a/A.java", "src/B.java": "back/src/B.java"}
    assert parse_renames("") == {}


class MoveToBack:
    """Restructure stand-in: moves the root sources into demo-back/ (git sees renames)."""

    def __init__(self):
        self.calls = []

    def apply(self, ws, profile, cfg, *, mode, params=None):
        self.calls.append(mode)
        for f in (FOO, BAR):
            dst = ws.abs("demo-back/" + f)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(ws.abs(f)), str(dst))
        return RestructureResult(ok=True, changed_files=["pom.xml"], moved_files=2)


def test_blocked_before_the_move_stays_blocked_after_it(cfg, ws):
    paths = [FOO, BAR, "demo-back/" + FOO, "demo-back/" + BAR]

    def build(w, call):
        errs = [l1(p) for p in paths if w.exists(p) and "broken" in w.read(p)]
        return bad_build(*errs) if errs else ok_build()

    def match(c):
        return pinned_block(c) if c.errors[0].file == FOO else KBMatch()     # only the pre-move path is pinned

    fixed: list[str] = []

    def fix(w, c, ctx):
        f = c.errors[0].file
        fixed.append(f)
        return write_fix(f, "class X { ok }\n") if f.endswith("Bar.java") else write_fix(f, "x")

    plan = MigrationPlan(profile_fingerprint="fp", case_type=CaseType.SPRING_BOOT, steps=[
        PlanStep(id="jdk21.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.JDK21, title="l", build_goal="compile",
                 max_iterations=2),
        PlanStep(id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE, title="r",
                 params={"mode": "layout"}),
        PlanStep(id="restructure.loop", kind=StepKind.BUILD_FIX_LOOP, phase=Phase.RESTRUCTURE, title="l",
                 build_goal="package", max_iterations=2),
        PlanStep(id="verify.build", kind=StepKind.BUILD, phase=Phase.VERIFY, title="v", build_goal="clean package")])
    report = make_orch(cfg, ws, plan=plan, build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix),
                       kb=FakeKB(match=match), restructurer=MoveToBack()).run()
    assert ws.exists("demo-back/" + FOO)
    assert "demo-back/" + FOO not in fixed                       # never sent to the LLM after the move
    ev = [e for e in journal_events(ws) if e["event"] == "restructure_moves"]
    assert ev and ev[0]["moved"] == 2 and ev[0]["blocked_rekeyed"] == 1
    rs = next(p for p in report.phases if p.phase == Phase.RESTRUCTURE)
    assert any("only blocked cluster(s) remain" in n for n in rs.notes), rs.notes
    assert [b.reason for b in report.blocked] == [BlockReason.UNSUPPORTED_SOURCE]


def _msa_project(ws, files: dict[str, str]):
    for f, text in files.items():
        ws.write(f, text)
    ws.commit("msa services")


def _module_files(w, call, files):
    mod = call["module"] or ""
    return [f for f in files if (not mod or f.startswith(mod + "/")) and w.exists(f)]


def test_msa_block_of_one_service_does_not_cover_the_other(cfg, ws):
    files = ["svc-a/src/main/java/com/x/SwaggerConfig.java", "svc-b/src/main/java/com/x/SwaggerConfig.java"]
    _msa_project(ws, {f: "class S { broken }\n" for f in files})

    def build(w, call):
        errs = [l1(f, module=f.split("/")[0]) for f in _module_files(w, call, files) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()

    fixed: list[str] = []

    def fix(w, c, ctx):
        fixed.append(c.errors[0].file)
        return write_fix(c.errors[0].file, "class S { ok }\n")

    report = make_orch(cfg, ws, plan=v2_msa_plan(), profile=make_profile(CaseType.MSA),
                       build=FakeBuildRunner(ws, fn=build), fixer=FakeFixer(fn=fix),
                       kb=FakeKB(match=lambda c: pinned_block(c) if c.errors[0].file == files[0] else KBMatch())).run()
    assert fixed == [files[1]]                                   # svc-b's file got its LLM attempt
    assert ws.read(files[1]) == "class S { ok }\n" and "broken" in ws.read(files[0])
    assert [b.files[0].split(":")[0] for b in report.blocked] == [files[0]]


# ------------------------------------------------------------------------------------------------ A7
def test_msa_internal_error_in_a_later_verify_is_never_build_success(cfg, ws):
    """svc-a's final build passes, svc-b's verify fix loop crashes (a git timeout): the run still builds svc-b once
    more (no fix loop on top), says it aborted, is not a success, and keeps svc-b's blocked item."""
    legacy = "svc-b/src/main/java/com/x/Legacy.java"
    _msa_project(ws, {legacy: "class L { broken }\n"})
    pkg = BuildError(kind=ErrorKind.PLUGIN_EXECUTION, module="svc-b", message="war packaging failed",
                     signature="plugin_execution|maven-war-plugin", signature_generic="plugin_execution")

    def build(w, call):
        if call["label"] == "verify-2":          # the loop's first own build (build 1 = the failed final build)
            raise RuntimeError("git commit timed out after 120s")
        if call["module"] != "svc-b":
            return ok_build()
        errs = [l1(legacy, module="svc-b")]
        if "package" in call["goal"]:
            errs.append(pkg)
        return bad_build(*errs)

    build_runner = FakeBuildRunner(ws, fn=build)
    fixer = FakeFixer(fn=lambda w, c, ctx: write_fix("svc-b/war-note.txt", "tried\n"))   # changes a file → a build
    report = make_orch(cfg, ws, plan=v2_msa_plan(), profile=make_profile(CaseType.MSA), build=build_runner,
                       fixer=fixer,
                       kb=FakeKB(match=lambda c: pinned_block(c) if c.errors[0].file == legacy else KBMatch())).run()
    assert "aborted" in report.flags and not report.success and run_exit_code(report) == 3
    assert any("internal error: RuntimeError: git commit timed out" in f for f in report.manual_followups)
    labels = [c["label"] for c in build_runner.calls]
    assert labels.count("verify-svc-a") == 1 and labels.count("verify-svc-b") == 2 and labels.count("verify-2") == 1
    verify_phases = [p for p in report.phases if p.phase == Phase.VERIFY]
    assert any("phase interrupted: RuntimeError" in n for n in verify_phases[0].notes)
    assert any("after the run aborted" in n for n in verify_phases[-1].notes)
    assert [b.files[0].split(":")[0] for b in report.blocked] == [legacy]
    assert pkg.signature in {e.signature for e in report.unresolved}
    assert "BUILD SUCCESS" not in (ws.run_dir / "report.md").read_text(encoding="utf-8").split("\n", 3)[2]


# ------------------------------------------------------------------------------------------------ A8
def test_test_skip_fallback_of_one_service_does_not_reach_the_other(cfg, ws):
    cfg.build.skip_test_compile = False            # the L7 fallback only acts where tests are compiled
    cfg.loop.phases["verify"].max_iterations = 1
    test_file = "svc-a/src/test/java/com/x/T.java"
    _msa_project(ws, {test_file: "class T { broken }\n"})

    def build(w, call):
        if call["module"] == "svc-a" and any(g in call["goal"] for g in ("test-compile", "package")) \
                and not call["skip_test_compile"]:
            return bad_build(l1(test_file, kind=ErrorKind.TEST_COMPILE, module="svc-a"))
        return ok_build()

    build_runner = FakeBuildRunner(ws, fn=build)
    report = make_orch(cfg, ws, plan=v2_msa_plan(), profile=make_profile(CaseType.MSA),
                       build=build_runner).run()
    assert "test-compile-skipped" in report.flags
    verify = {c["label"]: c["skip_test_compile"] for c in build_runner.calls if c["label"].startswith("verify-svc")}
    assert verify == {"verify-svc-a": True, "verify-svc-b": False}
    assert report.success


# ------------------------------------------------------------------------------------------------ A10
def _real_preflight(monkeypatch, result):
    import sbmig.build as b
    monkeypatch.setattr(b, "maven_available", lambda *a, **k: result)


def test_preflight_aborts_on_another_jdk(cfg, ws, monkeypatch):
    _real_preflight(monkeypatch, (True, "Apache Maven 3.9.10 (abc); Java version: 17.0.9"))
    build = FakeBuildRunner(ws)
    orch = make_orch(cfg, ws, build=build)
    orch._build_runner = None                       # the real preflight path (no build happens: intake aborts)
    report = orch.run()
    assert {"jdk-not-21", "aborted", "infra:jdk"} <= set(report.flags) and run_exit_code(report) == 3
    assert report.phases == [] and build.calls == []
    fu = [f for f in report.manual_followups if "Java 17.0.9" in f]
    assert len(fu) == 1 and fu[0].startswith("run aborted: Maven runs on Java 17.0.9 (")
    assert "not Java 21: set build.java_home (or JAVA_HOME) to a JDK 21 home" in fu[0] and "\n" not in fu[0]


def test_preflight_java_check_passes_or_is_off(cfg, ws, monkeypatch):
    for detail in ("Apache Maven 3.9.10; Java version: 21.0.1", "Apache Maven 3.9.10",
                   "Apache Maven 3.9.10; Java version: 21"):
        _real_preflight(monkeypatch, (True, detail))
        orch = make_orch(cfg, ws)
        orch._build_runner = None
        r = RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow())
        orch._preflight(r)
        assert "jdk-not-21" not in r.flags, detail
    cfg.build.require_java_major = 0
    _real_preflight(monkeypatch, (True, "Apache Maven 3.9.10; Java version: 1.8.0_392"))
    orch = make_orch(cfg, ws)
    orch._build_runner = None
    r = RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow())
    orch._preflight(r)
    assert "jdk-not-21" not in r.flags
    cfg.build.require_java_major = 21
    orch._preflight(r)
    assert "jdk-not-21" in r.flags and "Java 1.8.0_392" in orch._jdk_problem and "not Java 21" in orch._jdk_problem


def test_java_home_problem_does_not_say_install_maven(cfg, ws, monkeypatch):
    _real_preflight(monkeypatch, (False, "The JAVA_HOME environment variable is not defined correctly"))
    orch = make_orch(cfg, ws)
    orch._build_runner = None
    report = orch.run()
    assert {"mvn-not-found", "aborted", "infra:jdk"} <= set(report.flags) and "infra:maven" not in report.flags
    assert not [f for f in report.manual_followups if "install Maven" in f]
    assert any("JAVA_HOME" in f and "build.java_home" in f for f in report.manual_followups)


# ------------------------------------------------------------------------------------------------ A13
class OrderKB(FakeKB):
    def __init__(self, ws):
        super().__init__()
        self.ws = ws
        self.zip_at_record = None

    def record_run(self, entries):                        # v2.1: the run's error list into the KB
        self.zip_at_record = (self.ws.run_dir / "result.zip").exists()
        return super().record_run(entries)


def test_kb_and_report_are_written_before_the_zip(cfg, ws):
    seen: dict = {}

    def writer(report, w):
        seen["zip"] = (w.run_dir / "result.zip").exists()
        seen["patch"] = (w.run_dir / "changes.patch").exists()
        seen["outputs"] = dict(report.outputs)
        return {"report_md": str(w.run_dir / "report.md")}

    kb = OrderKB(ws)
    report = make_orch(cfg, ws, kb=kb, report_writer=writer).run()
    assert seen["zip"] is False and seen["patch"] is False and kb.zip_at_record is False
    assert seen["outputs"]["zip"].endswith("result.zip") and seen["outputs"]["diff"].endswith("changes.patch")
    assert (ws.run_dir / "result.zip").exists() and report.outputs["zip"].endswith("result.zip")


def test_abort_logs_one_greppable_line(cfg, ws, caplog):
    def boom(p, c, k):
        raise ValueError("bad plan")

    with caplog.at_level(logging.ERROR, logger="sbmig"):
        make_orch(cfg, ws, planner=boom).run()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("run aborted: ")]
    assert lines == ["run aborted: internal error: ValueError: bad plan"]


# ------------------------------------------------------------------------------------------------ A14
def _dep(msg: str) -> BuildError:
    return BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, module="demo", artifact=GAV.parse("org.x:lib:1.0"),
                      message=msg, signature="dependency_resolution|org.x:lib",
                      signature_generic="dependency_resolution")


def test_infra_flags(cfg, ws):
    orch = make_orch(cfg, ws)

    def flags_for(flags=(), results=(), java_problem="", success=False):
        r = RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(), flags=list(flags),
                      success=success)
        orch._verify_results = list(results)
        orch._mvn_java_problem = java_problem
        orch._infra_flags(r)
        return {f for f in r.flags if f.startswith("infra:")}

    assert flags_for(["llm-unavailable"]) == {"infra:llm"}
    assert flags_for(["llm-config-error"]) == {"infra:llm"}
    assert flags_for(["llm-unavailable"], success=True) == set()     # the run passed anyway: not an infra ending
    assert flags_for(["mvn-not-found"]) == {"infra:maven"}
    assert flags_for(["mvn-not-found"], java_problem="JAVA_HOME …") == {"infra:jdk"}
    assert flags_for(["jdk-not-21", "aborted"]) == {"infra:jdk"}
    assert flags_for(["aborted:parent-not-found"]) == {"infra:repository"}
    transfer = bad_build(_dep("Could not transfer artifact org.x:lib:pom:1.0 from/to nexus (https://n/x): "
                              "Connection refused"))
    cached = bad_build(_dep("org.x:lib:jar:1.0 was cached in the local repository, resolution will not be "
                            "reattempted until the update interval of nexus has elapsed"))
    auth = bad_build(_dep("Could not transfer artifact org.x:lib:1.0: status code: 401, reason phrase: Unauthorized"))
    assert flags_for(results=[transfer]) == {"infra:repository"}
    assert flags_for(results=[ok_build(), cached]) == {"infra:repository"}
    assert flags_for(results=[auth]) == {"infra:repository"}
    missing = bad_build(_dep("Could not find artifact org.x:lib:jar:1.0 in nexus (https://n/x)"))
    assert flags_for(results=[missing]) == set()                # a wrong coordinate is the project's problem
    mixed = bad_build(_dep("Could not transfer artifact … Connection refused"), l1(FOO))
    assert flags_for(results=[mixed]) == set()
    assert flags_for(results=[ok_build()]) == set()


def test_infra_disk_flag_for_a_full_disk(cfg, ws):
    orch = make_orch(cfg, ws)
    r = RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(), flags=[])
    full = BuildError(kind=ErrorKind.UNKNOWN, message="java.io.IOException: No space left on device", module="demo")
    orch._verify_results = [bad_build(full)]
    orch._mvn_java_problem = ""
    orch._infra_flags(r)
    assert "infra:disk" in r.flags
    win = BuildError(kind=ErrorKind.COMPILE, message="error: 디스크 공간이 부족합니다", file="A.java", line=1)
    orch._verify_results = [bad_build(win)]
    r2 = RunReport(run_id="x", project_name="p", case_type=CaseType.UNKNOWN, started_at=utcnow(), flags=[])
    orch._infra_flags(r2)
    assert "infra:disk" in r2.flags
