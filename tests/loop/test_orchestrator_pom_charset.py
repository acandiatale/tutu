"""The orchestrator side of INTERFACES "v2.1z-ay": a pom whose bytes do not match its declaration is corrected before
the first OpenRewrite run (both flows), and a failed conversion is said; a conversion-only run is not a success
without its conversion."""
from __future__ import annotations

import pytest

from sbmig.cli import run_exit_code
from sbmig.core.models import BuildError, ErrorKind, RewriteResult
from sbmig.report.result_line import report_result_line
from sbmig.restructure.pom_charset import diagnose

from .fakes import FakeBuildRunner, FakeRewriteRunner, bad_build, journal_events, ok_build
from .test_asis_parent import pom as parent_pom
from .test_orchestrator import make_orch
from .test_orchestrator_framework import ParentRewrite, fake_fw, reduced_orch  # noqa: F401 - fixture

BAD_POM = ('<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
           "    <!-- 인사말 모듈 -->\n    <modelVersion>4.0.0</modelVersion>\n    <groupId>com.x</groupId>\n"
           "    <artifactId>demo</artifactId>\n    <version>1</version>\n    <name>인사말 모듈</name>\n"
           "</project>\n").encode("cp949")
NOTE = "pom encoding corrected: declared UTF-8, saved as EUC-KR: declaration set to EUC-KR"
LINE = ("pom encodings corrected before the automatic conversion: `pom.xml` (declared UTF-8, saved as EUC-KR: "
        "declaration set to EUC-KR) — each pom's XML declaration named another encoding than the one its Korean text "
        "was saved in, which OpenRewrite cannot read; the Korean text itself is unchanged")
PARSE_ERR = "Failed to parse pom: Invalid UTF-8 middle byte 0xce (at char #421, byte #37)"


def bad_ws(ws):
    ws.abs("pom.xml").write_bytes(BAD_POM)
    ws.commit("as-is MS949 pom under a UTF-8 header")


class StrictRewrite:
    """Fails like rewrite-maven-plugin 6.46.1 while a project pom is inconsistent (measured), else succeeds."""

    def __init__(self, ws):
        self.ws, self.seen = ws, []

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", **kw):
        bad = [p for p in self.ws.pom_files() if diagnose(self.ws.abs(p).read_bytes()) is not None]
        self.seen.append(bad)
        if bad:
            return RewriteResult(ok=False, recipes=[r.id for r in recipes], error=PARSE_ERR)
        self.ws.write("rewritten.txt", "converted\n")
        return RewriteResult(ok=True, recipes=[r.id for r in recipes], changed_files=["rewritten.txt"])


def subjects(ws):
    return ws._git("log", "--format=%s").stdout.splitlines()


def entries(report):
    return {getattr(e, "message", ""): e for e in report.error_entries}


def full_orch(cfg, ws, rewrite, build=None):
    return make_orch(cfg, ws, rewrite=rewrite, build=build or FakeBuildRunner(ws, fn=lambda w, c: ok_build()))


# ------------------------------------------------------------------------------------------------ A: the correction
def test_full_run_corrects_the_pom_before_the_rewrite(cfg, ws):
    bad_ws(ws)
    rw = StrictRewrite(ws)
    report = full_orch(cfg, ws, rw).run()
    assert rw.seen == [[]]                                         # OpenRewrite saw a consistent pom
    assert ws.abs("pom.xml").read_bytes() == BAD_POM.replace(b'encoding="UTF-8"', b'encoding="EUC-KR"')
    log = subjects(ws)
    assert log.index("transform: pom encodings corrected") > log.index("rewrite: transform.rewrite")   # older
    [ev] = [e for e in journal_events(ws) if e["event"] == "pom_encodings"]
    assert ev == {**ev, "step": "transform.rewrite",
                  "entries": [{"pom": "pom.xml", "declared": "UTF-8", "actual": "EUC-KR", "how": "declaration"}]}
    assert "pom-encoding-corrected" in report.flags and "rewrite-failed" not in report.flags
    assert report.manual_followups.count(LINE) == 1
    assert entries(report)[NOTE].seen == "transform" and entries(report)[NOTE].file == "pom.xml"
    assert f"pom.xml: {NOTE}" in report.phases[0].notes
    assert report.success


def test_reduced_run_corrects_the_pom_before_the_source_encoding_and_the_rewrite(cfg, ws, fake_fw):
    bad_ws(ws)
    ws.abs("src/main/java/com/x/Greeting.java").parent.mkdir(parents=True, exist_ok=True)
    ws.abs("src/main/java/com/x/Greeting.java").write_bytes(
        'package com.x;\nclass Greeting { String s = "안녕하세요"; }\n'.encode("cp949"))
    ws.commit("MS949 source")
    rw = StrictRewrite(ws)
    orch, *_ = reduced_orch(cfg, ws, rewrite=rw)
    report = orch.run()
    assert rw.seen == [[]] and report.success
    mine = [s for s in subjects(ws) if s.startswith(("transform:", "rewrite:"))]
    assert mine[-3:] == ["rewrite: transform.rewrite", "transform: source encodings written",
                         "transform: pom encodings corrected"]                        # newest first
    data = ws.abs("pom.xml").read_bytes()
    assert data.startswith(b'<?xml version="1.0" encoding="EUC-KR"?>') and diagnose(data) is None
    text = data.decode("euc_kr")
    assert "<name>인사말 모듈</name>" in text and "<!-- 인사말 모듈 -->" in text
    assert "pom-encoding-corrected" in report.flags and LINE in report.manual_followups


def test_dry_run_only_notes(cfg, ws):
    bad_ws(ws)
    report = full_orch(cfg, ws, FakeRewriteRunner(ws)).run(dry_run=True)
    assert ws.abs("pom.xml").read_bytes() == BAD_POM
    assert "transform.rewrite: dry-run → pom encodings not corrected: pom.xml" in report.phases[0].notes
    assert "pom-encoding-corrected" not in report.flags
    assert not [e for e in journal_events(ws) if e["event"] == "pom_encodings"]


def test_switch_off_keeps_todays_behaviour(cfg, ws):
    bad_ws(ws)
    c = cfg.model_copy(deep=True)
    c.layout.legacy_context.correct_pom_encoding = False
    rw = StrictRewrite(ws)
    report = full_orch(c, ws, rw).run()
    assert rw.seen == [["pom.xml"]]
    assert "transform: pom encodings corrected" not in subjects(ws)
    assert "pom-encoding-corrected" not in report.flags and LINE not in report.manual_followups
    assert not [e for e in journal_events(ws) if e["event"] == "pom_encodings"]
    assert "rewrite-failed" in report.flags                          # ...and the failure is now said


def test_a_consistent_pom_is_not_touched(cfg, ws):
    head = ws.read("pom.xml")
    report = full_orch(cfg, ws, FakeRewriteRunner(ws)).run()
    assert ws.read("pom.xml").startswith(head[:40])
    assert "transform: pom encodings corrected" not in subjects(ws) and "pom-encoding-corrected" not in report.flags


# ------------------------------------------------------------------------------------------------ B: a failed conversion


def failing(ws, error=PARSE_ERR):
    return FakeRewriteRunner(ws, ok=False, error=error)


def test_full_run_failed_conversion_is_said_first_and_keeps_its_build_verdict(cfg, ws):
    from sbmig.analyze.hollow import HOLLOW_PREFIX
    orch = full_orch(cfg, ws, failing(ws, f"{PARSE_ERR} in {ws.root}/pom.xml\n[ERROR] rest"))
    report = orch.run()
    assert "rewrite-failed" in report.flags
    line = ("the automatic conversion (OpenRewrite) failed — recipe sets not applied: jdk21, spring_boot_4; reason: "
            f"{PARSE_ERR} in pom.xml — the files were not converted and the build ran on the unconverted tree; fix the "
            "cause and run the migration again")
    assert report.manual_followups[0] == line
    assert report.success and run_exit_code(report) == 0                       # build-defined verdict
    assert "rewrite-failed" in report_result_line(report).split("attn=")[1].split()[0]
    [ev] = [e for e in journal_events(ws) if e["event"] == "rewrite_failed"]
    assert ev == {**ev, "step": "transform.rewrite", "sets": ["jdk21", "spring_boot_4"]}
    # after the ADF / hollow-reactor lines
    orch.report.manual_followups = [f"{HOLLOW_PREFIX} y", "later"]
    orch._rewrite_failed_followup(orch.report)
    assert orch.report.manual_followups == [f"{HOLLOW_PREFIX} y", line, "later"]


@pytest.mark.parametrize("build_ok", [True, False])
def test_reduced_run_without_its_conversion_is_not_a_success(cfg, ws, fake_fw, build_ok):
    err = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file="src/main/java/A.java", line=1)
    build = FakeBuildRunner(ws, fn=lambda w, c: ok_build("package") if build_ok else bad_build(err, goal="package"))
    orch, *_ = reduced_orch(cfg, ws, rewrite=failing(ws), build=build)
    report = orch.run()
    assert "rewrite-failed" in report.flags
    assert not report.success and not orch.build_succeeded(report) and run_exit_code(report) == 2
    assert report.final_build.ok is build_ok
    line = report_result_line(report)
    assert ("build=ok" in line) is build_ok and "rewrite-failed" in line
    assert report.manual_followups[0].startswith("the automatic conversion (OpenRewrite) failed — recipe sets not "
                                                 "applied: jdk21, jakarta; reason: ")


def test_reduced_run_with_its_conversion_stays_a_success(cfg, ws, fake_fw):
    orch, *_ = reduced_orch(cfg, ws)
    report = orch.run()
    assert report.success and "rewrite-failed" not in report.flags


def test_runs_that_already_abort_get_no_rewrite_failed_flag(cfg, ws, fake_fw):
    coords = "com.acme:acme-parent:9.9"
    ws.write("pom.xml", parent_pom("com.acme:acme-parent", "9.9"))
    ws.commit("foreign parent")
    orch, *_ = reduced_orch(cfg, ws, rewrite=ParentRewrite(ws, coords))
    report = orch.run()
    assert "aborted:parent-not-found" in report.flags and "rewrite-failed" not in report.flags
    missing = make_orch(cfg, ws, rewrite=failing(ws, "Recipe(s) not found: com.x.Missing")).run()
    assert "rewrite-failed" not in missing.flags
