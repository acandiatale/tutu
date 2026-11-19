"""v2.1z-ax: when a Maven call of the run wrote (or used) a minimal pom for a library whose repository has the jar but
no pom, the report carries flag `pomless-library` and ONE hand-over line naming the libraries; nothing otherwise."""
from __future__ import annotations

from sbmig.build.pomless import handover_line, memo_for

from .fakes import FakeBuildRunner, ok_build
from .test_orchestrator import make_orch
from .test_project_maven_settings import POM, _ws


def _stubbing_build(stubs):
    def fn(ws, call):
        memo = memo_for(ws)
        for ga, v in stubs:
            if not any(s["ga"] == ga for s in memo.stubs):          # what pomless.run_with_stubs records
                memo.stubs.append({"ga": ga, "version": v, "path": "/m2/x.pom", "jar_from": "https://nexus.invalid/",
                                   "created": True})
        return ok_build()
    return fn


def test_flag_and_one_handover_line_naming_the_libraries(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM})
    stubs = [("com.example:pomless", "1.0"), ("com.example:other", "2.0")]
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=_stubbing_build(stubs))).run()
    assert report.flags.count("pomless-library") == 1
    line = handover_line(["com.example:pomless:1.0", "com.example:other:2.0"])
    assert report.manual_followups.count(line) == 1
    assert sum("no pom in the repository" in f for f in report.manual_followups) == 1
    assert report.success


def test_nothing_when_no_stub_was_needed(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": POM})
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build())).run()
    assert "pomless-library" not in report.flags
    assert not any("no pom in the repository" in f for f in report.manual_followups)


def test_a_later_run_is_told_about_a_stub_an_earlier_run_wrote(cfg, tmp_path):
    """The library builds silently on the stub of an earlier run: the report still names it (journal
    `pomless_stub_used`), in the same flag and the same single line as a stub written in this run."""
    import json

    from sbmig.build.pomless import write_stub
    lib_pom = POM.replace("</project>", "<dependencies><dependency><groupId>com.example</groupId><artifactId>old</artifactId>"
                          "<version>1.0</version></dependency></dependencies></project>")
    ws = _ws(tmp_path, {"pom.xml": lib_pom})
    c = cfg.model_copy(deep=True)
    c.build.local_repo = str(tmp_path / "m2")
    old = tmp_path / "m2" / "com" / "example" / "old" / "1.0" / "old-1.0.pom"
    assert write_stub(old, "com.example", "old", "1.0")
    report = make_orch(c, ws, build=FakeBuildRunner(ws, fn=_stubbing_build([("com.example:pomless", "1.0")]))).run()
    assert report.flags.count("pomless-library") == 1
    line = handover_line(["com.example:pomless:1.0", "com.example:old:1.0"])
    assert report.manual_followups.count(line) == 1
    assert sum("no pom in the repository" in f for f in report.manual_followups) == 1
    used = [json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines()
            if '"pomless_stub_used"' in x]
    assert [{k: e[k] for k in ("ga", "version", "path")} for e in used] == [
        {"ga": "com.example:old", "version": "1.0", "path": str(old)}]
