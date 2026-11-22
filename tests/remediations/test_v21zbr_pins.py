"""Pins of v2.1z-br (docs/INTERFACES.md "v2.1z-br", "Pinned, not changed"): what the remediation does TODAY.

A pin records behaviour; it is not a statement that the behaviour is right. When a later round changes what is pinned
here, the test changes with it, on purpose and in that round's contract.

The raised version cannot be resolved: the baseline build passes, the planner raised `example:lib` 1.0 → 1.1, and the
candidate build fails before compilation with Maven's dependency-resolution error. The graph's Maven is `FakeMaven`;
the fix loop is the real `run_fix_loop` (the real `BuildFixLoop`, the real Maven log parser on the same log), with the
loop tests' fake build runner and fake LLM (`tests.loop.fakes.FakeFixer`, answering every request with a script
that changes a comment in a source file and leaves the error as it was). No network: a socket connect fails the test.
"""
from __future__ import annotations

import json
import socket
import zipfile
from pathlib import Path

from sbmig.build.log_parser import parse_maven_log
from sbmig.core.models import BuildResult
from sbmig.remediations import fixer as fixer_mod
from sbmig.remediations.models import FAILED, BuildOutcome
from tests.loop.fakes import FakeBuildRunner, FakeFixer, FakeScriptRunner, make_profile, write_script

from .fakes import FakeMaven, make_run
from .test_graph import one_fix_components, run

APP = "src/main/java/App.java"
RESOLUTION_LOG = """[INFO] Scanning for projects...
[INFO]
[INFO] ---------------------------< example:app >----------------------------
[INFO] Building app 0.1
[INFO]   from pom.xml
[INFO] --------------------------------[ jar ]---------------------------------
[WARNING] The POM for example:lib:jar:1.1 is missing, no dependency information available
[INFO] ------------------------------------------------------------------------
[INFO] BUILD FAILURE
[INFO] ------------------------------------------------------------------------
[INFO] Total time:  1.234 s
[INFO] Finished at: 2026-11-22T10:00:00+09:00
[INFO] ------------------------------------------------------------------------
[ERROR] Failed to execute goal on project app: Could not resolve dependencies for project example:app:jar:0.1: \
Could not find artifact example:lib:jar:1.1 in nexus (https://nexus.invalid/repository/public) -> [Help 1]
[ERROR]
[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.
[ERROR] Re-run Maven using the -X switch to enable full debug logging.
[ERROR]
[ERROR] For more information about the errors and possible solutions, please read the following articles:
[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/DependencyResolutionException
"""
BASELINE_LOG = "[INFO] BUILD SUCCESS\n"


class SequencedMaven(FakeMaven):
    """`FakeMaven` that records the order of its `package` / `resolve` calls."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sequence: list[str] = []

    def package(self, project, log_path, stop=None, targets=None):
        self.sequence.append("package")
        return super().package(project, log_path, stop, targets)

    def resolve(self, project, work, targets=None):
        self.sequence.append("resolve")
        return super().resolve(project, work, targets)


class LoopFixer:
    """The graph's `Fixer`: the real `run_fix_loop` with the loop tests' fakes for Maven, the LLM and scripts."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = 0
        self.builds: list[FakeBuildRunner] = []
        self.llm = FakeFixer(script_fn=lambda ws, cluster, ctx: write_script(
            {APP: f"class App {{ /* try {len(self.llm.script_calls)} */ }}\n"}))

    def _runner(self, ws):
        log = Path(ws.root).parent / "candidate-build.log"
        log.write_text(RESOLUTION_LOG, encoding="utf-8")

        def build(ws_, call):
            return BuildResult(ok=False, goal=call["goal"], command=["mvn", call["goal"]], exit_code=1,
                               duration_s=0.1, log_path=str(log),
                               errors=parse_maven_log(RESOLUTION_LOG, ws_.root))
        r = FakeBuildRunner(ws, fn=build)
        self.builds.append(r)
        return r

    def run(self, cfg, tree, work_dir, *, run_id, targets, applied, manifest_paths, first_outcome, stop, deadline,
            journal):
        self.calls += 1
        return fixer_mod.run_fix_loop(self.cfg, tree, work_dir, run_id=run_id, targets=targets, applied=applied,
                                      manifest_paths=manifest_paths, first_outcome=first_outcome, stop=stop,
                                      deadline=deadline, journal=journal, build_runner=self._runner,
                                      llm_fixer=self.llm, script_runner=FakeScriptRunner(), profile=make_profile())


def test_pin_an_unresolvable_raised_version_reaches_the_fix_loop_and_two_llm_requests(tmp_path, monkeypatch, cfg):
    """Today: (1) no resolve check runs between applying the raised version and the candidate build, so nothing stops
    the run before the build; (2) the candidate build's resolution failure calls the fix loop once; (3) the loop
    makes 2 LLM requests (the `artifact_not_found` entry of `loop.blocked_policy`), builds 3 times, rolls both tries
    back (`no_change`), blocks the case and ends not fixed; the run ends FAILED `candidate_build_failed` with the
    change set delivered unbuilt and the candidate's source untouched."""
    def no_network(*a, **k):
        raise AssertionError("network access in a pin test")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    assert cfg.loop.blocked_policy["artifact_not_found"] == 2          # what decides the number of requests below
    state, paths = make_run(tmp_path)
    failed = BuildOutcome(ok=False, exit_code=1, log="build.log",
                          summary="[ERROR] Failed to execute goal on project app: Could not resolve dependencies")
    maven = SequencedMaven(outcomes=[True, failed], logs=[lambda p: BASELINE_LOG, lambda p: RESOLUTION_LOG])
    fx = LoopFixer(cfg)
    out, rec = run(state, paths, one_fix_components(maven=maven, fixer=fx))

    # the error as the loop sees it: one dependency-resolution error naming the raised version
    errors = parse_maven_log(RESOLUTION_LOG, tmp_path)
    assert [(e.kind.value, e.signature) for e in errors] == [("dependency_resolution", "dependency_resolution|example:lib:1.1")]

    # (1) baseline build, the baseline's resolve (inventory), the candidate build; no resolve before the candidate build
    assert maven.sequence == ["package", "resolve", "package"]
    assert len(maven.resolve_calls) == 1
    # (2) the fix loop is called once for the round
    assert fx.calls == 1
    # (3) two LLM requests, three loop builds (first + one per try), then the case is blocked
    assert len(fx.llm.script_calls) == 2
    assert [len(b.calls) for b in fx.builds] == [3]
    fix = out.rounds[0].fix
    assert (fix.attempted, fix.fixed, fix.attempts, fix.iterations, fix.llm_calls, fix.remaining) == (True, False, 2, 3,
                                                                                                    2, 1)
    assert fix.reason == "not fixed: 1 error(s) left" and fix.changed == []
    loop_events = [json.loads(x) for x in (paths.round_dir(1) / "fixloop" / "journal.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [e["outcome"] for e in loop_events if e["event"] == "attempt_scored"] == ["no_change", "no_change"]
    assert [e["reason"] for e in loop_events if e["event"] == "blocked"] == ["artifact_not_found"]

    # end state of the run
    assert (out.status, out.reason) == (FAILED, "candidate_build_failed")
    assert out.message.endswith("| automatic build fix: not fixed: 1 error(s) left")
    assert out.delivery is not None and not out.delivery.build_ok
    with zipfile.ZipFile(paths.delivery_zip) as zf:
        assert b"<version>1.1</version>" in zf.read("pom.xml")
        assert zf.read(APP) == b"class App {}\n"
    assert [e["phase"] for e in rec.events if e.get("event") == "remediation_fix_loop"] == ["start", "end"]
