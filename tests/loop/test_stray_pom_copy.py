"""v2.1z-h (2026-11-03): a stray pom copy in the input (`boot/bin/`: src + .project + pom.xml with the module's
coordinates) is found at intake — `ws.ignored_dirs`, flag `stray-pom-ignored`, journal `stray_pom_ignored`, one Handover
follow-up — and nothing else sees it: no `sources-outside-reactor`, no generated-aggregator listing, no reactor dedupe."""
from __future__ import annotations

from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.workspace import Workspace
from sbmig.report.i18n import tr_line

from .fakes import FakeBuildRunner, journal_events, ok_build
from .test_orchestrator import make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
JAVA = "package com.example.app;\npublic class HelloServlet {}\n"
WEBXML = '<?xml version="1.0"?><web-app version="3.1"/>'
ROOT = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
        "  <groupId>com.example</groupId>\n  <artifactId>app</artifactId>\n  <version>1.0</version>\n"
        "  <packaging>pom</packaging>\n  <modules><module>boot</module></modules>\n</project>\n")
BOOT = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
        "  <parent><groupId>com.example</groupId><artifactId>app</artifactId><version>1.0</version></parent>\n"
        "  <artifactId>app-boot</artifactId>\n  <packaging>war</packaging>\n"
        "  <properties><maven.compiler.source>1.8</maven.compiler.source>"
        "<maven.compiler.target>1.8</maven.compiler.target></properties>\n"
        "  <dependencies><dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId>"
        "<version>3.1.0</version><scope>provided</scope></dependency></dependencies>\n</project>\n")
TREE = {".gitignore": "target/\n", "pom.xml": ROOT, "boot/pom.xml": BOOT,
        "boot/src/main/java/com/example/app/HelloServlet.java": JAVA, "boot/src/main/webapp/WEB-INF/web.xml": WEBXML,
        # the stray copy: the whole module again, same coordinates, listed by nothing
        "boot/bin/pom.xml": BOOT, "boot/bin/.project": "<projectDescription><name>boot</name></projectDescription>",
        "boot/bin/src/main/java/com/example/app/HelloServlet.java": JAVA,
        "boot/bin/src/main/webapp/WEB-INF/web.xml": WEBXML}
FOLLOWUP = ("pom copy ignored: `boot/bin/pom.xml` has the same coordinates as `boot/pom.xml` (`com.example:app-boot`), lies "
            "inside the directory of `boot/pom.xml` and no pom lists it as a module — treated as a stray copy (an IDE output "
            "/ backup folder): not built, not migrated, left where it is; delete it from the source, or give it its own "
            "coordinates and list it in <modules> if it is a real module")


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _run(cfg, ws):
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    return orch, orch.run()


def test_the_copy_is_ignored_at_intake_and_named_in_the_handover(cfg, tmp_path):
    ws = _ws(tmp_path, TREE)
    _orch, report = _run(cfg, ws)
    assert "stray-pom-ignored" in report.flags
    for f in ("reactor-duplicate-removed", "aggregator-generated", "sources-outside-reactor", "hollow-reactor"):
        assert f not in report.flags
    assert ws.ignored_dirs == {"boot/bin"}
    assert report.manual_followups.count(FOLLOWUP) == 1
    assert tr_line("ko", FOLLOWUP).startswith("pom 복사본 무시: `boot/bin/pom.xml` 은(는) `boot/pom.xml` 와 좌표가 같고")
    [ev] = [e for e in journal_events(ws) if e["event"] == "stray_pom_ignored"]
    assert ev["poms"] == ["boot/bin/pom.xml"] and ev["duplicates_of"] == ["boot/pom.xml"]
    assert ev["coordinates"] == ["com.example:app-boot"] and ev["dirs"] == ["boot/bin"]
    assert report.success and report.final_build is not None and report.final_build.ok
    # the copy is left where it is, untouched
    assert ws.exists("boot/bin/pom.xml") and ws.exists("boot/bin/src/main/java/com/example/app/HelloServlet.java")
    # the profile counted the module's sources once (the copy's java never reached the scan)
    assert report.profile is not None and report.profile.source.java_files == 1


def test_a_tree_without_a_copy_has_no_flag(cfg, tmp_path):
    ws = _ws(tmp_path, {k: v for k, v in TREE.items() if not k.startswith("boot/bin/")})
    _orch, report = _run(cfg, ws)
    assert "stray-pom-ignored" not in report.flags and ws.ignored_dirs == set()
    assert not [e for e in journal_events(ws) if e["event"] == "stray_pom_ignored"]
    assert not any(f.startswith("pom copy ignored") for f in report.manual_followups)
