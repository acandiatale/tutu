"""v2.1z-i: `step_analyze` records the profile's bundled jars — flag `bundled-jars`, journal `bundled_jars` (paths +
coordinates), `logs/bundled-jars.json` — and a tree without one gets none of them."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.workspace import Workspace, logs_dir
from tests.loop.fakes import FakeBuildRunner, journal_events, ok_build
from tests.loop.test_orchestrator import make_orch

from .test_bundled_jars import SEED_JAR, jar_bytes

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
POM = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
       "  <groupId>com.example</groupId>\n  <artifactId>web</artifactId>\n  <version>1.0</version>\n"
       "  <packaging>war</packaging>\n</project>\n")
JAVA = "package com.example.web;\nimport com.acme.seed.Seed;\npublic class Login {}\n"
TREE: dict[str, str | bytes] = {".gitignore": "target/\n", "pom.xml": POM,
                                "src/main/java/com/example/web/Login.java": JAVA,
                                "src/main/webapp/WEB-INF/web.xml": '<?xml version="1.0"?><web-app version="3.1"/>'}


def _ws(tmp_path: Path, files: dict[str, str | bytes]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, body in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _run(cfg, ws):
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    return orch.run()


def test_bundled_jars_are_flagged_journaled_and_written(cfg, tmp_path):
    ws = _ws(tmp_path, {**TREE, SEED_JAR: jar_bytes()})
    report = _run(cfg, ws)
    assert "bundled-jars" in report.flags
    [ev] = [e for e in journal_events(ws) if e["event"] == "bundled_jars"]
    assert ev["jars"] == [SEED_JAR] and ev["coordinates"] == {SEED_JAR: None}
    data = json.loads((logs_dir(ws) / "bundled-jars.json").read_text(encoding="utf-8"))
    assert [d["path"] for d in data] == [SEED_JAR] and data[0]["provides"] == ["com.acme.seed"]


def test_a_tree_without_bundled_jars_has_no_flag(cfg, tmp_path):
    ws = _ws(tmp_path, TREE)
    report = _run(cfg, ws)
    assert "bundled-jars" not in report.flags
    assert not [e for e in journal_events(ws) if e["event"] == "bundled_jars"]
    assert not (logs_dir(ws) / "bundled-jars.json").exists()


def test_a_jar_the_target_chain_manages_is_marked_superseded_for_the_report(cfg, tmp_path):
    """Integrator (2026-11-04): the report's status column — a bundled jar whose known g:a the post-restructure chain
    capture manages gets the `superseded:` note; an unknown vendor jar does not."""
    from sbmig.core.models import JarCoordinate

    ws = _ws(tmp_path, {**TREE, SEED_JAR: jar_bytes()})
    orch = make_orch(cfg, ws, analyzer=analyze_project, build=FakeBuildRunner(ws, fn=lambda w, call: ok_build()))
    report = orch.run()
    report.profile.bundled_jars = [
        JarCoordinate(path="src/main/webapp/WEB-INF/lib/spring-webmvc-4.3.30.jar", group_id="org.springframework",
                      artifact_id="spring-webmvc", version="4.3.30", source="pom_properties", provides=["org.springframework.web"]),
        JarCoordinate(path=SEED_JAR, provides=["com.acme.seed"]),
    ]
    (logs_dir(ws) / "effective-depmgmt-after-restructure.json").write_text(
        json.dumps({"managed": {"org.springframework:spring-webmvc": "7.0.0"}, "by_module": {}}), encoding="utf-8")
    orch._classify_bundled_jars(report)
    notes = {j.path.rsplit("/", 1)[-1]: j.note for j in report.profile.bundled_jars}
    assert notes["spring-webmvc-4.3.30.jar"] == "superseded: org.springframework:spring-webmvc is managed by the target parent chain"
    assert notes["acme-seed-1.0.jar"] == ""
    orch._classify_bundled_jars(report)                                   # idempotent
    assert report.profile.bundled_jars[0].note.count("superseded:") == 1
