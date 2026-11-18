"""Every Maven call sbmig starts on a project gets the empty `MAVEN_BASEDIR` and the options carried over from the
`.mvn/maven.config` Maven would have found from where the call runs: the build, OpenRewrite, `dependency:tree`,
`help:effective-pom`, the classpath check and the remediation's builds. Fake processes only."""
from __future__ import annotations

import types
from pathlib import Path

from sbmig.build import classpath as cpmod
from sbmig.build import deptree as dtmod
from sbmig.build import effective as effmod
from sbmig.build import mvn as mvnmod
from sbmig.build.mvn import ProcessResult
from sbmig.build.runner import MavenRunner
from sbmig.core.config import BuildConfig, RemediationConfig
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.remediations.maven import ServerMaven

MMPD = "-Dmaven.multiModuleProjectDirectory="
CONFIG = "-s x\n-Drevision=1.2.3\n-Pci\n"


def _ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / ".mvn").mkdir(parents=True)
    (root / ".mvn" / "maven.config").write_text(CONFIG, encoding="utf-8")
    (root / ".mvn" / "jvm.config").write_text("-XX:MaxPermSize=256m\n", encoding="utf-8")
    (root / "pom.xml").write_text("<project/>")
    (root / "svc").mkdir()
    (root / "svc" / "pom.xml").write_text("<project/>")
    return Workspace(root, tmp_path / "run", "t-run")


class Recorder:
    def __init__(self, text="[INFO] BUILD SUCCESS\n"):
        self.calls: list[dict] = []
        self.text = text

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd), "env": dict(env)})
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(self.text, encoding="utf-8")
        return ProcessResult(exit_code=0, text=self.text, duration_s=0.1)


def _assert_project_mvn_out(call: dict, root: Path) -> None:
    cmd, env = call["cmd"], call["env"]
    base = Path(env["MAVEN_BASEDIR"])
    assert base.is_dir() and not (base / ".mvn").exists()
    assert cmd[1:4] == ["-Drevision=1.2.3", "-Pci", f"{MMPD}{root.absolute()}"], cmd
    assert "x" not in cmd


def test_the_build(tmp_path, cfg, monkeypatch):
    ws = _ws(tmp_path)
    rec = Recorder()
    monkeypatch.setattr("sbmig.build.runner.run_logged", rec)
    assert MavenRunner(ws, cfg).run("clean package").ok
    _assert_project_mvn_out(rec.calls[0], ws.root)
    # a service directory of an MSA run: Maven would have found the root's .mvn from there
    MavenRunner(ws, cfg).run("clean package", module="svc/pom.xml")
    assert rec.calls[1]["cwd"] == ws.root / "svc"
    assert rec.calls[1]["cmd"][1:4] == ["-Drevision=1.2.3", "-Pci", f"{MMPD}{ws.root.absolute()}"]


def test_the_build_with_the_switch_off(tmp_path, cfg, monkeypatch):
    ws = _ws(tmp_path)
    rec = Recorder()
    monkeypatch.setattr("sbmig.build.runner.run_logged", rec)
    monkeypatch.setenv("MAVEN_BASEDIR", str(tmp_path / "leaked"))
    cfg.build.ignore_project_mvn = False
    MavenRunner(ws, cfg).run("clean package")
    call = rec.calls[0]
    assert "MAVEN_BASEDIR" not in call["env"]
    assert not any(a.startswith(MMPD) or a in ("-Drevision=1.2.3", "-Pci") for a in call["cmd"])


def test_openrewrite(tmp_path, cfg, monkeypatch):
    import sbmig.rewrite.runner as runner_mod
    from sbmig.rewrite import OpenRewriteRunner
    ws = _ws(tmp_path)
    ws.git_init()
    rec = Recorder()
    monkeypatch.setattr(runner_mod, "run_logged", rec)
    monkeypatch.setattr(runner_mod, "resolve_recipe_artifact_versions",
                        lambda cfg, env, artifacts, cache_path, work_dir: {a: "1.0" for a in artifacts})
    OpenRewriteRunner(ws, cfg).run([RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21")])
    _assert_project_mvn_out(rec.calls[0], ws.root)


def test_dependency_tree(tmp_path, cfg, monkeypatch):
    ws = _ws(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(dtmod, "run_logged", rec)
    dtmod._run_tree(ws, cfg, "", "root", tmp_path / "tree.txt", [], package=False)
    _assert_project_mvn_out(rec.calls[0], ws.root)
    dtmod._run_tree(ws, cfg, "svc", "svc", tmp_path / "tree.txt", [], package=False, standalone=True)
    assert rec.calls[1]["cmd"][1:4] == ["-Drevision=1.2.3", "-Pci", f"{MMPD}{ws.root.absolute()}"]


def test_effective_pom(tmp_path, cfg, monkeypatch):
    ws = _ws(tmp_path)
    rec = Recorder(text="")
    monkeypatch.setattr(effmod, "run_logged", rec)
    effmod.capture_effective_depmgmt(ws, cfg)
    _assert_project_mvn_out(rec.calls[0], ws.root)


def test_classpath(tmp_path, cfg, monkeypatch):
    ws = _ws(tmp_path)
    rec = Recorder()
    monkeypatch.setattr(cpmod, "run_logged", rec)
    cpmod._run_build_classpath(ws, cfg, "", "root", tmp_path / "cp.txt", package=False)
    _assert_project_mvn_out(rec.calls[0], ws.root)


def _rem_cfg(tmp_path: Path, **build):
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings/>", encoding="utf-8")
    opts = {"mvn": "/opt/maven/bin/mvn", "settings_file": str(settings), "local_repo": str(tmp_path / "m2")}
    opts.update(build)
    return types.SimpleNamespace(build=BuildConfig(**opts), remediation=RemediationConfig())


def test_remediation_builds(tmp_path):
    ws = _ws(tmp_path)
    (ws.root / ".mvn" / "maven.config").write_text(CONFIG + "-Dmaven.wagon.http.ssl.insecure=true\n", encoding="utf-8")
    rec = Recorder()
    gw = ServerMaven(_rem_cfg(tmp_path), rec, maven_probe=lambda c: (True, "Java version: 21.0.2"))
    assert gw.package(ws.root, tmp_path / "logs" / "build.log").ok
    gw.resolve(ws.root, tmp_path / "work")
    for call in rec.calls:
        _assert_project_mvn_out(call, ws.root)
        assert not any("ssl.insecure" in a for a in call["cmd"])       # never a weaker TLS from the project
    # a service built in its own directory takes the .mvn Maven would have found from there
    rec.calls.clear()
    gw.package(ws.root, tmp_path / "logs" / "build.log", targets=["svc"])
    assert rec.calls[0]["cwd"] == ws.root / "svc"
    assert rec.calls[0]["cmd"][1:4] == ["-Drevision=1.2.3", "-Pci", f"{MMPD}{ws.root.absolute()}"]


def test_remediation_switch_off(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    monkeypatch.setenv("MAVEN_BASEDIR", str(tmp_path / "leaked"))
    rec = Recorder()
    gw = ServerMaven(_rem_cfg(tmp_path, ignore_project_mvn=False), rec,
                     maven_probe=lambda c: (True, "Java version: 21.0.2"))
    gw.package(ws.root, tmp_path / "logs" / "build.log")
    assert "MAVEN_BASEDIR" not in rec.calls[0]["env"]
    assert not any(a.startswith(MMPD) for a in rec.calls[0]["cmd"])


def test_mvn_env_used_by_the_preflight_too(cfg):
    assert "MAVEN_BASEDIR" in mvnmod.mvn_env(cfg)
