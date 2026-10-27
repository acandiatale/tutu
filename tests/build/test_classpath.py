"""Class → jar → GAV attribution (catalog X1/X2)."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from sbmig.build import classpath as cpmod
from sbmig.build.classpath import (ClassLocation, gav_for_jar, locate_class, locate_type_owner, module_classpath,
                                   owner_fqcn, type_mismatch)
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import GAV, BuildError, ErrorKind
from sbmig.core.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parents[2]


def _jar(path: Path, classes: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        for c in classes:
            zf.writestr(c, b"\xca\xfe\xba\xbe")
    return path


@pytest.fixture()
def env(tmp_path: Path, cfg, monkeypatch):
    repo = tmp_path / "m2" / "repository"
    legacy = _jar(repo / "com/company/legacy-web/1.4/legacy-web-1.4.jar",
                  ["com/company/web/RequestHelper.class", "com/company/web/RequestHelper$1.class",
                   "com/company/web/RequestHelper$Builder.class"])
    servlet = _jar(repo / "javax/servlet/javax.servlet-api/4.0.1/javax.servlet-api-4.0.1.jar",
                   ["javax/servlet/http/HttpServletRequest.class"])
    classified = _jar(repo / "com/querydsl/querydsl-apt/5.1.0/querydsl-apt-5.1.0-jakarta.jar",
                      ["com/querydsl/apt/jpa/JPAAnnotationProcessor.class"])
    foreign = _jar(tmp_path / "project" / "lib" / "vendor.jar", ["com/vendor/Thing.class"])
    outside = _jar(tmp_path / "elsewhere" / "odd.jar", ["com/odd/Odd.class"])
    root = tmp_path / "project"
    src = root / "src/main/java/com/acme/web/MyController.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("package com.acme.web; public class MyController {}", encoding="utf-8")
    ws = Workspace(root, tmp_path / "run", "t")
    cfg.build.local_repo = str(repo)
    entries = [legacy, servlet, classified, foreign, outside, tmp_path / "missing.jar"]
    calls: list = []

    def fake_cp(ws_, cfg_, module=None):
        calls.append(module)
        return entries
    monkeypatch.setattr(cpmod, "module_classpath", fake_cp)
    return ws, cfg, calls


def test_gav_for_jar_layouts(tmp_path: Path):
    repo = tmp_path / "repo"
    assert gav_for_jar(repo / "org/springframework/boot/spring-boot/3.3.13/spring-boot-3.3.13.jar", repo) == \
        GAV(group_id="org.springframework.boot", artifact_id="spring-boot", version="3.3.13")
    g = gav_for_jar(repo / "com/querydsl/querydsl-apt/5.1.0/querydsl-apt-5.1.0-jakarta.jar", repo)
    assert g.classifier == "jakarta" and g.key == "com.querydsl:querydsl-apt"
    # heuristic: any .../repository/... layout even when the repo is not the configured one
    g = gav_for_jar(Path("/x/.m2/repository/a/b/c/1.0/c-1.0.jar"), repo)
    assert g.coordinates == "a.b:c:1.0"
    assert gav_for_jar(Path("/opt/lib/vendor.jar"), repo) is None
    assert gav_for_jar(repo / "a/b/1.0/other.jar", repo) is None


def test_locate_class_gav_inner_and_project(env):
    ws, cfg, _ = env
    loc = locate_class(ws, cfg, "com.company.web.RequestHelper")
    assert isinstance(loc, ClassLocation) and not loc.in_project
    assert loc.artifact.coordinates == "com.company:legacy-web:1.4" and loc.jar.endswith("legacy-web-1.4.jar")
    assert locate_class(ws, cfg, "com.company.web.RequestHelper$1").artifact.artifact_id == "legacy-web"
    assert locate_class(ws, cfg, "com.company.web.RequestHelper.Builder").artifact.artifact_id == "legacy-web"
    assert locate_class(ws, cfg, "javax.servlet.http.HttpServletRequest[]").artifact.artifact_id == \
        "javax.servlet-api"
    assert locate_class(ws, cfg, "com.querydsl.apt.jpa.JPAAnnotationProcessor").artifact.classifier == "jakarta"
    proj = locate_class(ws, cfg, "com.acme.web.MyController")
    assert proj.in_project and proj.jar == "" and proj.source_file == "src/main/java/com/acme/web/MyController.java"
    vendor = locate_class(ws, cfg, "com.vendor.Thing")              # systemPath-style jar inside the project
    assert vendor.artifact is None and vendor.in_project and vendor.jar.endswith("vendor.jar")
    odd = locate_class(ws, cfg, "com.odd.Odd")                      # foreign jar outside any repository
    assert odd.artifact is None and not odd.in_project and odd.jar.endswith("odd.jar")
    assert locate_class(ws, cfg, "com.nowhere.Nope") is None
    assert locate_class(ws, cfg, "Simple") is None


def test_owner_extraction():
    def e(msg, location=None, raw=""):
        return BuildError(kind=ErrorKind.COMPILE, message=msg, location=location, raw=raw)
    assert owner_fqcn(e("method handle in class com.company.web.RequestHelper cannot be applied to given types;",
                        raw="required: javax.servlet.http.HttpServletRequest\nfound: "
                            "jakarta.servlet.http.HttpServletRequest")) == "com.company.web.RequestHelper"
    assert owner_fqcn(e("constructor RequestHelper in class com.company.web.RequestHelper cannot be applied")) \
        == "com.company.web.RequestHelper"
    assert owner_fqcn(e("cannot find symbol", location="class com.company.web.Util")) == "com.company.web.Util"
    assert owner_fqcn(e("cannot find symbol", location="variable h of type com.company.web.RequestHelper")) == \
        "com.company.web.RequestHelper"
    assert owner_fqcn(e("incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to "
                        "javax.servlet.http.HttpServletRequest")) == "javax.servlet.http.HttpServletRequest"
    assert owner_fqcn(e("com.acme.F is not abstract and does not override abstract method doFilter(javax.servlet."
                        "ServletRequest) in com.company.web.BaseFilter")) == "com.company.web.BaseFilter"
    assert owner_fqcn(e("cannot access com.company.web.Old")) == "com.company.web.Old"
    assert owner_fqcn(e("';' expected")) is None
    req, found = type_mismatch(e("incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted "
                                 "to javax.servlet.http.HttpServletRequest"))
    assert (req, found) == ("javax.servlet.http.HttpServletRequest", "jakarta.servlet.http.HttpServletRequest")
    assert type_mismatch(e("method m in class a.B cannot be applied", raw="required: javax.x.Y\nfound: jakarta.x.Y")) \
        == ("javax.x.Y", "jakarta.x.Y")


def test_locate_type_owner(env):
    ws, cfg, calls = env
    err = BuildError(kind=ErrorKind.COMPILE, module="web",
                     message="method handle in class com.company.web.RequestHelper cannot be applied to given types;")
    loc = locate_type_owner(ws, cfg, err)
    assert loc.artifact.key == "com.company:legacy-web" and loc.fqcn == "com.company.web.RequestHelper"
    assert calls[-1] == "web"                                       # module taken from the error
    err2 = BuildError(kind=ErrorKind.COMPILE, message="incompatible types: jakarta.servlet.http.HttpServletRequest "
                      "cannot be converted to javax.servlet.http.HttpServletRequest")
    assert locate_type_owner(ws, cfg, err2, "m").artifact.artifact_id == "javax.servlet-api"
    assert calls[-1] == "m"
    assert locate_type_owner(ws, cfg, BuildError(kind=ErrorKind.COMPILE, message="';' expected")) is None


def test_module_classpath_caches_and_never_raises(tmp_path: Path, cfg, monkeypatch):
    root = tmp_path / "project"
    (root / "web").mkdir(parents=True)
    (root / "pom.xml").write_text("<project><modules><module>web</module></modules></project>", encoding="utf-8")
    (root / "web/pom.xml").write_text("<project><parent><artifactId>p</artifactId></parent>"
                                      "<artifactId>web-art</artifactId></project>", encoding="utf-8")
    ws = Workspace(root, tmp_path / "run", "t")
    runs: list[list[str]] = []

    def ok_run(cmd, *, cwd, env, log_path, timeout_s):
        runs.append(list(cmd))
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("-Dmdep.outputFile="))
        Path(out).write_text(os.pathsep.join(["/r/a.jar", "/r/b.jar"]), encoding="utf-8")
        return ProcessResult(exit_code=0, text="", duration_s=0.1)
    monkeypatch.setattr(cpmod, "run_logged", ok_run)
    assert module_classpath(ws, cfg, "web-art") == [Path("/r/a.jar"), Path("/r/b.jar")]
    assert "-pl" in runs[0] and "web" in runs[0] and (ws.run_dir / "logs" / "classpath-web.txt").is_file()
    assert module_classpath(ws, cfg, "web") == [Path("/r/a.jar"), Path("/r/b.jar")]
    assert len(runs) == 1                                           # cached
    st = (root / "web/pom.xml").stat()
    os.utime(root / "web/pom.xml", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    module_classpath(ws, cfg, "web")
    assert len(runs) == 2                                           # pom changed → re-run

    fails: list[list[str]] = []

    def bad_run(cmd, *, cwd, env, log_path, timeout_s):
        fails.append(list(cmd))
        return ProcessResult(exit_code=1, text="[ERROR] boom", duration_s=0.1)
    monkeypatch.setattr(cpmod, "run_logged", bad_run)
    os.utime(root / "web/pom.xml", ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    assert module_classpath(ws, cfg, "web") == []
    assert len(fails) == 2 and "package" in fails[1] and "-Dmaven.main.skip=true" in fails[1]   # sibling fallback
    assert module_classpath(ws, cfg, "no-such-module") == []

    def raising(*a, **k):
        raise RuntimeError("mvn exploded")
    monkeypatch.setattr(cpmod, "run_logged", raising)
    os.utime(root / "web/pom.xml", ns=(st.st_atime_ns, st.st_mtime_ns + 12_000_000_000))
    assert module_classpath(ws, cfg, "web") == []
    monkeypatch.setattr(cpmod, "module_classpath", lambda *a, **k: [])
    assert locate_class(ws, cfg, "com.x.Y") is None


@pytest.mark.integration
def test_real_build_classpath_boot3(tmp_path: Path, cfg):
    jdk21 = "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"
    if not Path(jdk21).is_dir():
        pytest.skip("JDK 21 not available")
    cfg.build.java_home = jdk21
    ws = Workspace.from_input(REPO_ROOT / "fixtures/projects/boot3", tmp_path / "out", run_id="cp")
    entries = module_classpath(ws, cfg, None)
    assert entries and all(isinstance(p, Path) for p in entries)
    loc = locate_class(ws, cfg, "org.springframework.boot.SpringApplication")
    assert loc is not None and loc.artifact is not None
    assert loc.artifact.key == "org.springframework.boot:spring-boot" and loc.artifact.version
