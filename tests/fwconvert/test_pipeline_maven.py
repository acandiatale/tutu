"""Scenario A14 with real Maven: a converted project builds with the real Spring Boot 4.1.0 parent under JDK 21.

The project of test_chain gets an H2 datasource bean (placeholders from a properties file, a password), so that the
driver dependency and application.yml are part of the build. The MyBatis starter's version is the newest one in
``~/.m2`` or, failing that, the newest release the repository code (``sbmig/build/repo_versions.py``) finds.
Skipped with a reason when Maven, a JDK 21 or the network is missing."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_chain import OVERLAY, _project

from sbmig.build import mvn as mvnmod
from sbmig.build.repo_versions import (
    RepositoryLookup as RealLookup,  # the real class (conftest patches the name)
)
from sbmig.build.repo_versions import is_prerelease, parse_version
from sbmig.core.config import load_config
from sbmig.fwconvert.model import ConvertOptions
from sbmig.fwconvert.pipeline import convert_project
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.skeleton import MYBATIS_STARTER

pytestmark = pytest.mark.integration

BOOT_VERSION = "4.1.0"
DEFAULT_JDK21 = "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"
NETWORK_WORDS = ("Could not transfer", "Unknown host", "UnknownHostException", "Connection refused",
                 "Network is unreachable", "connect timed out")


def _jdk21() -> str | None:
    for home in (os.environ.get("JAVA21_HOME", ""), os.environ.get("SBMIG_TEST_JAVA21_HOME", ""), DEFAULT_JDK21):
        java = Path(home.strip()) / "bin" / "java" if home.strip() else None
        if java is None or not java.is_file():
            continue
        cp = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=60, check=False)
        if re.search(r'version "21[."]', cp.stderr + cp.stdout):
            return home.strip()
    return None


def _mybatis_version() -> tuple[str, str]:
    local = Path.home() / ".m2/repository/org/mybatis/spring/boot/mybatis-spring-boot-starter"
    found = [d.name for d in local.iterdir() if d.is_dir() and (d / f"mybatis-spring-boot-starter-{d.name}.pom")
             .is_file() and not is_prerelease(d.name)] if local.is_dir() else []
    if found:
        return max(found, key=parse_version), "~/.m2"
    cfg = load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}})
    lookup = RealLookup(cfg)
    g, a = MYBATIS_STARTER.split(":")
    versions = [v for v in lookup.versions(g, a) if not is_prerelease(v)]
    if not versions:
        return "", lookup.remote_failures(g, a) or "no release found"
    return max(versions, key=parse_version), "repository lookup"


def test_a_converted_project_builds_with_real_maven(tmp_path):
    if shutil.which("mvn") is None:
        pytest.skip("Maven (mvn) is not on PATH")
    jdk = _jdk21()
    if jdk is None:
        pytest.skip("no JDK 21 (set JAVA21_HOME)")
    mybatis, where = _mybatis_version()
    if not mybatis:
        pytest.skip(f"no version of {MYBATIS_STARTER} in ~/.m2 and the repository lookup failed: {where}")

    src, out = tmp_path / "src", tmp_path / "out"
    _project(src)
    (src / "src/main/resources/applicationContext.xml").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans" xmlns:p="http://www.springframework.org/schema/p">
  <bean id="dataSource" class="org.apache.commons.dbcp2.BasicDataSource"
        p:driverClassName="${jdbc.driver}" p:url="${jdbc.url}" p:username="${jdbc.user}" p:password="${jdbc.pw}"/>
</beans>
""", encoding="utf-8")
    (src / "src/main/resources/jdbc.properties").write_text(
        "jdbc.driver=org.h2.Driver\njdbc.url=jdbc:h2:mem:shop;MODE=Oracle\njdbc.user=sa\njdbc.pw=example\n",
        encoding="utf-8")
    options = ConvertOptions(source_name="sample-shop-clean.zip", domain="d1", boot_version=BOOT_VERSION,
                             java_version="21", versions={MYBATIS_STARTER: mybatis},
                             jdbc_drivers={"h2": "com.h2database:h2"})
    result = convert_project(src, out, load_rules("Example", OVERLAY), options)
    assert result.converted and result.errors == [], result.errors
    assert result.datasource is not None and result.datasource.configured and result.datasource.vendor == "h2"
    p = result.identity.project_name

    cfg = load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}},
                                 "build": {"mvn": "mvn", "java_home": jdk}})
    ok, detail = mvnmod.maven_available(cfg)
    if not ok:
        pytest.skip(f"Maven does not start: {detail}")
    cmd = mvnmod.with_project_mvn(cfg, mvnmod.base_command(cfg) + ["-q", "-B", "-DskipTests", "package"], out, top=out)
    cp = subprocess.run(cmd, cwd=str(out), check=False, env=mvnmod.mvn_env(cfg), capture_output=True, timeout=600,
                        stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    text = cp.stdout + cp.stderr
    print(f"\n$ {' '.join(cmd)}  (MyBatis starter {mybatis} from {where}, JDK {jdk}) -> exit {cp.returncode}")
    if cp.returncode != 0 and any(w in text for w in NETWORK_WORDS):
        pytest.skip(f"Maven could not reach a repository: {text[-1500:]}")
    assert cp.returncode == 0, text[-6000:]
    war = out / f"{p}-boot" / "target" / f"{p}.war"
    assert war.is_file(), sorted(x.name for x in (out / f"{p}-boot" / "target").iterdir())
    for module in ("domain", "service", "store"):
        assert (out / f"{p}-{module}" / "target" / f"{p}-{module}-1.0.0.jar").is_file()
    with zipfile.ZipFile(war) as zf:
        names = zf.namelist()
    libs = [n.rsplit("/", 1)[-1] for n in names if n.startswith("WEB-INF/lib")]
    for jar in ("h2-", "mybatis-spring-boot-starter-", "tomcat-embed-jasper-", f"{p}-service-1.0.0.jar"):
        assert any(x.startswith(jar) for x in libs), (jar, libs)
    # the embedded Tomcat brings the servlet API; a second copy in the WAR would duplicate its classes
    assert not [x for x in libs if x.startswith("jakarta.servlet-api-")], libs
    assert f"WEB-INF/static/{p}/shop/order-list.jsp" in names
    assert any(n.endswith("mybatis/mapper/order-mapper.xml") for n in names)
