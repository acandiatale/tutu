"""Real Maven + the fake IVAS: a project whose pom is MS949 under a UTF-8 header is remediated, not refused.

Maven 3.9 reads such a pom without a warning (table in `test_pom_encoding.py`). The run goes through the real intake,
boundary check, inventory, planner, apply, `ServerMaven` (offline, local repository `~/.m2/repository`) and freeze;
the fake IVAS of `test_integration.py` answers from the uploaded bytes (it decodes the pom the way IVAS would read a
Korean pom; the bytes it received are kept as they are). Skipped without Maven, JDK 21 or the cached artifacts.
"""
from __future__ import annotations

import functools
import io
import os
import shutil
import subprocess
import zipfile
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
from sbmig.remediations.models import SUCCEEDED, RunPaths

from .fakes import FakeVersions
from .test_integration import BASE, PASSWORD, IvasServer, Rec, Vuln, World, zip_bytes, zip_contents

pytestmark = pytest.mark.integration

M2 = Path.home() / ".m2" / "repository"
NEEDED = ["org/apache/commons/commons-lang3/3.9/commons-lang3-3.9.jar",
          "org/apache/commons/commons-lang3/3.18.0/commons-lang3-3.18.0.jar",
          "org/apache/maven/plugins/maven-dependency-plugin/3.8.1/maven-dependency-plugin-3.8.1.jar"]
KOREAN = "한글 주석: 결제 모듈"
# offline Maven uses a cached file only for the repository id it came from (central), hence the mirror id
SETTINGS = ("<settings><mirrors><mirror><id>central</id><url>https://nexus.invalid/repository/public/</url>"
            "<mirrorOf>*</mirrorOf></mirror></mirrors></settings>\n")

POM = ('<?xml version="1.0" encoding="UTF-8"?>\n'
       f"<!-- {KOREAN} -->\n"
       '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
       "  <modelVersion>4.0.0</modelVersion>\n"
       "  <groupId>example</groupId>\n"
       "  <artifactId>app</artifactId>\n"
       "  <version>0.1</version>\n"
       "  <name>결제 모듈</name>\n"
       "  <properties>\n"
       "    <maven.compiler.release>21</maven.compiler.release>\n"
       "    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n"
       "  </properties>\n"
       "  <dependencies>\n"
       "    <dependency>\n"
       "      <groupId>org.apache.commons</groupId>\n"
       "      <artifactId>commons-lang3</artifactId>\n"
       "      <version>3.9</version>\n"
       "    </dependency>\n"
       "  </dependencies>\n"
       "</project>\n").encode("cp949")
APP = b"package example;\npublic class App { String s = org.apache.commons.lang3.StringUtils.trim(\" a \"); }\n"


def _jdk21() -> str | None:
    home = os.environ.get("JAVA21_HOME", "")
    if home and Path(home, "bin", "java").exists():
        return home
    try:
        out = subprocess.run(["/usr/libexec/java_home", "-v", "21"], check=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


class KoreanAwareIvas(IvasServer):
    """Reads a Korean pom the way the real scanner does: the emulation gets the decoded pom, `uploads` keeps the bytes."""

    def _findings(self, data: bytes, n: int) -> list[dict]:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            files = {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}
        decoded = {k: apply_mod.pom_xml_bytes(v) if k.endswith("pom.xml") else v for k, v in files.items()}
        return super()._findings(zip_bytes(decoded), n)


def test_ms949_pom_under_a_utf8_header_is_remediated_with_real_maven(tmp_path):
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
                          remediation=RemediationConfig(policy=RemediationPolicyConfig()))
    upload = zip_bytes({"pom.xml": POM, "src/main/java/example/App.java": APP})
    up = tmp_path / "upload.zip"
    up.write_bytes(upload)
    paths = RunPaths(tmp_path / "out" / "_remediations" / "rem-1")
    res = intake_mod.accept_zip(up, paths.root, intake_mod.limits_from_config(cfg))
    state = graph.new_state("rem-1", "app.zip", cfg, res, ["default.yml"], policy_hash=planner_mod.policy_hash(cfg))
    rec = Rec(paths)
    server = KoreanAwareIvas(tmp_path / "ivas-side", World(),
                             {"org.apache.commons:commons-lang3:3.9": Vuln("CVE-2026-1002", "3.18.0")})
    st = IvasSettings(base_url=BASE, user_id="svc-user", password=PASSWORD, poll_seconds=0, page_size=2)
    comps = Components(ivas=IvasHttpClient(st, session=server, sleep=lambda _s: None),
                       scan=functools.partial(run_scan, settings=st, sleep=lambda _s: None),
                       maven=ServerMaven(cfg, journal=rec.journal),
                       versions=FakeVersions({"org.apache.commons:commons-lang3": ["3.9", "3.10", "3.18.0"]}),
                       intake=intake_mod, inventory=inventory_mod.build_inventory, plan=planner_mod.plan_round,
                       apply=apply_mod)

    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)

    print(f"\nstatus={out.status} reason={out.reason} message={out.message!r}")
    print(f"boundary={[(b.code, b.path, b.action) for b in out.boundary]}")
    print(f"baseline_build ok={out.baseline_build.ok if out.baseline_build else None}; "
          f"rounds={[(r.round, r.build.ok if r.build else None) for r in out.rounds]}")
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert not [b for b in out.boundary if b.action == "refuse"]
    assert out.baseline_build.ok and out.rounds[0].build.ok
    delivered = zip_contents(paths.delivery_zip.read_bytes())["pom.xml"]
    # the version is bumped and nothing else moved: Korean bytes and the declaration are the original bytes
    assert delivered == POM.replace(b"<version>3.9</version>", b"<version>3.18.0</version>")
    assert KOREAN.encode("cp949") in delivered and delivered.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert server.uploads[0] == upload and server.uploads[-1] == paths.delivery_zip.read_bytes()
    print(f"delivered pom: {len(delivered)} bytes, differs from the original only in the version text")
