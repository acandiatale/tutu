"""2026-11-05 (company run, the rerun after the mirror fix): `Unresolveable build extension: Plugin
org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 …` stopped every Maven call — OpenRewrite first, so no recipe ran and the fix
loop met the extension as its first error. The intake now removes deploy-transport extensions before the first Maven call:
commit, flag `deploy-extensions-removed`, journal `build_extensions`, one Handover line per removal."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.models import Phase
from sbmig.core.workspace import Workspace

from .fakes import FakeBuildRunner, journal_events
from .test_orchestrator import files_build, make_orch

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
WEBDAV = ("  <build>\n    <extensions>\n      <extension>\n        <groupId>org.apache.maven.wagon</groupId>\n"
          "        <artifactId>wagon-webdav</artifactId>\n        <version>1.0-beta-2</version>\n      </extension>\n"
          "    </extensions>\n  </build>\n")


def _pom(aid: str, packaging: str = "jar", modules: tuple[str, ...] = (), extra: str = "") -> str:
    mods = ("  <modules>\n" + "".join(f"    <module>{m}</module>\n" for m in modules) + "  </modules>\n") if modules else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.xxx</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0.0-SNAPSHOT</version>\n"
            f"  <packaging>{packaging}</packaging>\n{mods}{extra}</project>\n")


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "proj"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


TREE = {"pom.xml": _pom("xxx-root", "pom", ("xxx-back",), extra=WEBDAV), "xxx-back/pom.xml": _pom("xxx-back")}


def test_intake_removes_the_wagon_extension_before_the_first_maven_call(cfg, tmp_path):
    ws = _ws(tmp_path, TREE)
    seen: list[str] = []
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build))
    orch._capture_effective = lambda report: seen.append(ws.read("pom.xml"))     # the first Maven call of a run
    report = orch.run(stop_after=Phase.INTAKE)
    assert seen and "wagon" not in seen[0] and "<extensions>" not in seen[0]
    assert "deploy-extensions-removed" in report.flags and "aborted" not in report.flags
    assert any(f.startswith("build extension org.apache.maven.wagon:wagon-webdav:1.0-beta-2 was removed from pom.xml")
               for f in report.manual_followups)
    subject, _, body = ws._git("log", "-1", "--format=%B").stdout.strip().partition("\n\n")
    assert subject.startswith("intake: deploy transport extension(s) removed (1)")
    assert body == "- org.apache.maven.wagon:wagon-webdav:1.0-beta-2 in pom.xml"
    ev = [e for e in journal_events(ws) if e["event"] == "build_extensions"]
    assert len(ev) == 1 and ev[0]["removed"] == ["org.apache.maven.wagon:wagon-webdav:1.0-beta-2 in pom.xml"]
    assert ev[0]["kept"] == [] and ev[0]["poms"] == ["pom.xml"]


def test_switch_off_keeps_the_extension(cfg, tmp_path):
    cfg = cfg.model_copy(deep=True)
    cfg.intake.strip_deploy_extensions = False
    ws = _ws(tmp_path, TREE)
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "deploy-extensions-removed" not in report.flags and "wagon-webdav" in ws.read("pom.xml")
    assert not any(s.startswith("intake: deploy") for s in ws._git("log", "--format=%s").stdout.splitlines())


def test_no_extension_leaves_no_trace(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom("xxx-root", "pom", ("xxx-back",)), "xxx-back/pom.xml": _pom("xxx-back")})
    report = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=files_build)).run(stop_after=Phase.INTAKE)
    assert "deploy-extensions-removed" not in report.flags
    assert not [e for e in journal_events(ws) if e["event"] == "build_extensions"]
