"""Real Maven + the fake IVAS: one fixable library finding and one file component (a `.js` file).

`remediation.file_components: report` (default) ends SUCCEEDED with the file listed apart; `count` stays INCOMPLETE as
before; with the matched files answering 404 or the file lists answering 500 the run still succeeds, the scan carries
the note and the report section says what could not be read. The run goes through the real intake, inventory, planner,
apply and `ServerMaven` (offline, local repository `~/.m2/repository`). Skipped without Maven, JDK 21 or the cached
artifacts.
"""
from __future__ import annotations

import functools
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbmig.core.config import BuildConfig, RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import apply as apply_mod
from sbmig.remediations import graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations import inventory as inventory_mod
from sbmig.remediations import planner as planner_mod
from sbmig.remediations.interfaces import Components
from sbmig.remediations.ivas import IvasHttpClient, IvasSettings, run_scan
from sbmig.remediations.maven import ServerMaven
from sbmig.remediations.models import INCOMPLETE, SUCCEEDED, RunPaths, read_json

from .fakes import FakeVersions
from .test_integration import BASE, PASSWORD, IvasServer, Rec, Vuln, World, zip_bytes
from .test_pom_encoding_maven import M2, NEEDED, SETTINGS, _jdk21

pytestmark = pytest.mark.integration

POM = b"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>app</artifactId>
  <version>0.1</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>3.9</version>
    </dependency>
  </dependencies>
</project>
"""
APP = b"package example;\npublic class App { String s = org.apache.commons.lang3.StringUtils.trim(\" a \"); }\n"
JS = "src/main/resources/static/js/jquery-1.8.1.min.js"
FILE_COMPONENTS = {"jquery-1.8.1.min.js": ("11560|934982", "jquery", "1.8.1", Vuln("CVE-2020-11022", "3.5.0"))}


def _run(tmp_path: Path, mode: str, broken: str = ""):
    mvn, jdk = shutil.which("mvn"), _jdk21()
    if mvn is None or jdk is None:
        pytest.skip("needs Maven and JDK 21")
    missing = [p for p in NEEDED if not (M2 / p).is_file()]
    if missing:
        pytest.skip(f"not in the local repository: {missing}")
    settings = tmp_path / "server" / "settings.xml"
    settings.parent.mkdir(parents=True)
    settings.write_text(SETTINGS, encoding="utf-8")
    cfg = SimpleNamespace(build=BuildConfig(mvn=mvn, settings_file=str(settings), java_home=jdk, local_repo=str(M2),
                                            offline=True),
                          remediation=RemediationConfig(policy=RemediationPolicyConfig(), file_components=mode))
    upload = zip_bytes({"pom.xml": POM, "src/main/java/example/App.java": APP, JS: b"/*! jQuery v1.8.1 */\n"})
    up = tmp_path / "upload.zip"
    up.write_bytes(upload)
    paths = RunPaths(tmp_path / "out" / "_remediations" / "rem-1")
    res = intake_mod.accept_zip(up, paths.root, intake_mod.limits_from_config(cfg))
    state = graph.new_state("rem-1", "app.zip", cfg, res, ["default.yml"], policy_hash=planner_mod.policy_hash(cfg))
    rec = Rec(paths)
    server = IvasServer(tmp_path / "ivas-side", World(),
                        {"org.apache.commons:commons-lang3:3.9": Vuln("CVE-2026-1002", "3.18.0")},
                        file_components=FILE_COMPONENTS)
    if broken == "matched_404":
        server.matched_status = 404
    elif broken == "list_500":
        server.file_list_status = 500
    st = IvasSettings(base_url=BASE, user_id="svc-user", password=PASSWORD, poll_seconds=0, page_size=2,
                      file_components=mode)
    comps = Components(ivas=IvasHttpClient(st, session=server, sleep=lambda _s: None),
                       scan=functools.partial(run_scan, settings=st, sleep=lambda _s: None),
                       maven=ServerMaven(cfg, journal=rec.journal),
                       versions=FakeVersions({"org.apache.commons:commons-lang3": ["3.9", "3.10", "3.18.0"]}),
                       intake=intake_mod, inventory=inventory_mod.build_inventory, plan=planner_mod.plan_round,
                       apply=apply_mod)
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    text = paths.report_md.read_text(encoding="utf-8")
    section = text.split("## 프로젝트 파일에서 찾은 취약점")[1].split("\n## ")[0] if "## 프로젝트 파일에서" in text else ""
    print(f"\n[{mode} {broken or 'ok'}] status={out.status} reasons={out.reasons} counts={out.counts}")
    if section:
        print("## 프로젝트 파일에서 찾은 취약점" + section)
    return out, paths, read_json(paths.scan("baseline")), text, section


def test_report_mode_succeeds_and_lists_the_js_file(tmp_path):
    out, _paths, scan, text, section = _run(tmp_path, "report")
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert out.baseline_build.ok and out.rounds[0].build.ok and out.counts["fixed"] == 1
    assert out.counts["baseline_file_findings"] == 1 and out.counts["final_file_findings"] == 1
    js = next(f for f in scan["findings"] if f["kind"] == "file_component")
    assert js["locations"] == [JS] and not js["files_unread"]
    assert "| `jquery` | 1.8.1 | 3.5.0 |" in section and f"`{JS}`" in section
    assert "프로젝트 파일에서 찾은 취약점 1건은 위 개수에 넣지 않고 아래" in text


def test_count_mode_is_incomplete_as_before(tmp_path):
    out, _paths, _scan, _text, section = _run(tmp_path, "count")
    assert out.status == INCOMPLETE and "manual_review" in out.reasons and "residual_findings" in out.reasons
    assert any(r.origin == "vendored" and r.ga == "jquery" for r in out.residuals)
    assert section == ""


def test_report_mode_matched_files_404(tmp_path):
    out, _paths, scan, _text, section = _run(tmp_path, "report", "matched_404")
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert scan["complete"] and scan["file_complete"] and scan["incomplete_reasons"] == []
    assert any("matched_files(11560|934982)" in n and "404" in n for n in scan["notes"])
    assert "파일 위치를 읽지 못했습니다" in section


def test_report_mode_file_lists_500(tmp_path):
    out, _paths, scan, _text, section = _run(tmp_path, "report", "list_500")
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert scan["complete"] and not scan["file_complete"] and scan["incomplete_reasons"] == []
    assert any("file components are only listed" in n for n in scan["notes"])
    assert "파일에서 찾은 취약점 목록을 IVAS에서 읽지 못했습니다" in section
