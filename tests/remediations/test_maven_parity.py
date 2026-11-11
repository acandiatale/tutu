"""The remediation builds like the migration (first real-Maven review, 2026-11-11). Fakes only — no mvn, IVAS or network.

Pinned here, each found on a real Maven run of the chained flow:
* an MSA without a root aggregator is built per service by the migration (`verify.build@<svc>`, cwd = service); the
  remediation now gets those targets, accepts the root-pom-less tree and builds / resolves each target the same way;
* a target the root pom lists is built from the root with `-pl <t> -am` (the migration's `build_target_args` rule);
* a cached resolution failure (stale `*.lastUpdated`, incl. "Non-resolvable parent POM … Could not find artifact") is
  retried once with `-U` like the migration's `MavenRunner`;
* arguments the remediation never passes (insecure TLS / `-s` / `-f`) are a readiness refusal naming them, not a drop;
* every remediation Maven call logs `remediation mvn … (cwd=…)`;
* a baseline build that fails although the migration's final build passed says so plainly.
"""
from __future__ import annotations

import logging
import types
import zipfile
from pathlib import Path

import pytest

from sbmig.build.mvn import ProcessResult
from sbmig.core.config import BuildConfig, RemediationConfig
from sbmig.remediations import chain
from sbmig.remediations import intake as intake_mod
from sbmig.remediations.inventory import ProjectPoms, normalize_roots
from sbmig.remediations.maven import TEST_SKIP, ServerMaven
from sbmig.remediations.models import FAILED, R_BASELINE_BUILD_FAILED, IntakeRejected

from .fakes import FakeMaven, FakeScanner, make_cfg, make_components, scan_result

SVC_POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>shop</groupId>
  <artifactId>{a}</artifactId>
  <version>1.0.0-SNAPSHOT</version>
</project>
"""

ROOT_AGG = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>shop</groupId>
  <artifactId>root</artifactId>
  <version>1.0.0-SNAPSHOT</version>
  <packaging>pom</packaging>
  <modules><module>svc-a</module></modules>
</project>
"""

TREE_A = "shop:svc-a:jar:1.0.0-SNAPSHOT\n+- org.yaml:snakeyaml:jar:2.6:compile\n"
TREE_B = "shop:svc-b:jar:1.0.0-SNAPSHOT\n\\- commons-io:commons-io:jar:2.19.0:compile\n"

NEG_CACHE = ("[ERROR] Non-resolvable parent POM for shop:svc-a:1.0.0-SNAPSHOT: Could not find artifact "
             "com.inhouse:base-parent:pom:1.0.0-SNAPSHOT in nexus (https://nexus.invalid/group/)\n[INFO] BUILD FAILURE\n")


class Runner:
    """`run_logged` stand-in: records (cmd, cwd); `script` answers per call (exit, text); trees by cwd name."""

    def __init__(self, script=None, trees=None):
        self.calls: list[dict] = []
        self.script = list(script or [])
        self.trees = dict(trees or {})

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd), "log": Path(log_path)})
        out = next((a.split("=", 1)[1] for a in cmd if a.startswith("-DoutputFile=")), None)
        if out is not None:
            key = next((a for a in cmd if a in self.trees), Path(cwd).name)
            Path(out).write_text(self.trees.get(key, ""), encoding="utf-8")
            return ProcessResult(exit_code=0, text="", duration_s=0.1)
        code, text = self.script.pop(0) if self.script else (0, "[INFO] BUILD SUCCESS\n")
        return ProcessResult(exit_code=code, text=text, duration_s=1.0)


def cfg_for(tmp_path: Path, **build):
    settings = tmp_path / "settings.xml"
    settings.write_text("<settings><mirrors><mirror><id>nexus</id><url>https://nexus.invalid/group/</url>"
                        "<mirrorOf>*</mirrorOf></mirror></mirrors></settings>", encoding="utf-8")
    jdk = tmp_path / "jdk"
    jdk.mkdir(exist_ok=True)
    (jdk / "release").write_text('JAVA_VERSION="21.0.2"\n', encoding="utf-8")
    opts = {"mvn": "/opt/maven/bin/mvn", "settings_file": str(settings), "java_home": str(jdk),
            "local_repo": str(tmp_path / "m2")}
    opts.update(build)
    return types.SimpleNamespace(build=BuildConfig(**opts), remediation=RemediationConfig())


def gw(tmp_path, runner, **build):
    return ServerMaven(cfg_for(tmp_path, **build), runner, maven_probe=lambda c: (True, "Java version: 21.0.2"))


def msa_tree(root: Path, with_root: str | None = None) -> Path:
    for a in ("svc-a", "svc-b"):
        (root / a).mkdir(parents=True, exist_ok=True)
        (root / a / "pom.xml").write_text(SVC_POM.format(a=a), encoding="utf-8")
    if with_root is not None:
        (root / "pom.xml").write_text(with_root, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------- build targets
def test_no_targets_builds_the_root_once_as_before(tmp_path):
    r = Runner()
    project = msa_tree(tmp_path / "p", ROOT_AGG)
    out = gw(tmp_path, r).package(project, tmp_path / "logs" / "build.log")
    assert out.ok and [c["cwd"] for c in r.calls] == [project]
    assert "-pl" not in r.calls[0]["cmd"]


def test_per_service_targets_build_in_each_service_dir_like_the_migration(tmp_path):
    r = Runner()
    project = msa_tree(tmp_path / "p")                       # no root pom: the migration built per service
    out = gw(tmp_path, r).package(project, tmp_path / "logs" / "build.log", targets=["svc-a", "svc-b"])
    assert out.ok
    assert [c["cwd"] for c in r.calls] == [project / "svc-a", project / "svc-b"]
    for c in r.calls:
        assert c["cmd"][-1] == TEST_SKIP and "clean" in c["cmd"] and "package" in c["cmd"] and "-pl" not in c["cmd"]
    assert [c["log"].name for c in r.calls] == ["build-svc-a.log", "build-svc-b.log"]


def test_target_listed_by_the_root_pom_builds_from_the_root_with_pl_am(tmp_path):
    r = Runner()
    project = msa_tree(tmp_path / "p", ROOT_AGG)            # root lists svc-a only
    gw(tmp_path, r).package(project, tmp_path / "build.log", targets=["svc-a", "svc-b"])
    a, b = r.calls
    assert a["cwd"] == project and a["cmd"][a["cmd"].index("-pl"):a["cmd"].index("-pl") + 3] == ["-pl", "svc-a", "-am"]
    assert b["cwd"] == project / "svc-b" and "-pl" not in b["cmd"]


def test_first_failing_target_stops_and_is_named(tmp_path):
    r = Runner(script=[(1, "[ERROR] COMPILATION ERROR in svc-a\n[INFO] BUILD FAILURE\n")])
    project = msa_tree(tmp_path / "p")
    out = gw(tmp_path, r).package(project, tmp_path / "build.log", targets=["svc-a", "svc-b"])
    assert not out.ok and len(r.calls) == 1
    assert out.summary.startswith("build target svc-a:") and out.log.endswith("build-svc-a.log")


def test_resolve_per_target_records_roots_and_every_module(tmp_path):
    r = Runner(trees={"svc-a": TREE_A, "svc-b": TREE_B})
    project = msa_tree(tmp_path / "p")
    model = gw(tmp_path, r).resolve(project, tmp_path / "work", targets=["svc-a", "svc-b"])
    assert model.complete, model.notes
    assert model.roots == ["svc-a", "svc-b"]
    assert {m.dir: [d.ga for d in m.dependencies] for m in model.modules} == {
        "svc-a": ["org.yaml:snakeyaml"], "svc-b": ["commons-io:commons-io"]}
    assert [c["cwd"] for c in r.calls] == [project / "svc-a", project / "svc-b"]


def test_resolve_without_root_pom_and_without_targets_is_incomplete_not_silent(tmp_path):
    model = gw(tmp_path, Runner()).resolve(msa_tree(tmp_path / "p"), tmp_path / "work")
    assert not model.complete and model.roots == []


def test_project_poms_roots_and_normalize():
    assert normalize_roots(None) == ["."] and normalize_roots(["", "."]) == ["."]
    assert normalize_roots(["svc-a/", "./svc-b", "../x", "svc-a"]) == ["svc-a", "svc-b"]


def test_project_poms_reads_each_target_reactor(tmp_path):
    project = msa_tree(tmp_path / "p")
    pp = ProjectPoms(project, roots=["svc-a", "svc-b", "missing"])
    assert pp.reactor == ["svc-a", "svc-b"] and not pp.complete
    assert any("build target missing: no pom.xml" in n for n in pp.notes)


# ---------------------------------------------------------------------------- -U parity / refusal / logging
def test_cached_parent_failure_is_retried_once_with_U_like_the_migration(tmp_path):
    r = Runner(script=[(1, NEG_CACHE), (0, "[INFO] BUILD SUCCESS\n")])
    events = []
    g = gw(tmp_path, r)
    g.journal = events.append
    out = g.package(msa_tree(tmp_path / "p", ROOT_AGG), tmp_path / "logs" / "build.log")
    assert out.ok and len(r.calls) == 2
    second = r.calls[1]["cmd"]
    assert second[-2:] == ["-U", TEST_SKIP] and "-U" not in r.calls[0]["cmd"]
    assert r.calls[1]["log"].name == "build-U.log"
    assert [e["event"] for e in events] == ["remediation_negative_cache_retry"]
    assert events[0]["artifacts"] == ["com.inhouse:base-parent:pom:1.0.0-SNAPSHOT"]


def test_no_U_retry_offline_or_when_switched_off(tmp_path):
    for build in ({"offline": True}, {"retry_negative_cache": False}):
        r = Runner(script=[(1, NEG_CACHE)])
        out = gw(tmp_path, r, **build).package(msa_tree(tmp_path / "p", ROOT_AGG), tmp_path / "b.log")
        assert not out.ok and len(r.calls) == 1


def test_every_call_logs_command_and_cwd(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="sbmig.remediations.maven")
    project = msa_tree(tmp_path / "p")
    gw(tmp_path, Runner()).package(project, tmp_path / "b.log", targets=["svc-a"])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("remediation mvn ")]
    assert len(lines) == 1 and "clean package" in lines[0] and lines[0].endswith(f"(cwd={(project / 'svc-a').as_posix()})")


# ---------------------------------------------------------------------------- intake / chain
def _zip(tmp_path: Path, files: dict[str, str]) -> Path:
    z = tmp_path / "in.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n, t in files.items():
            zf.writestr(n, t)
    return z


def test_intake_accepts_a_rootless_tree_only_with_matching_targets(tmp_path):
    files = {"svc-a/pom.xml": SVC_POM.format(a="svc-a"), "svc-b/pom.xml": SVC_POM.format(a="svc-b")}
    limits = intake_mod.limits_from_config(make_cfg())
    with pytest.raises(IntakeRejected) as e:
        intake_mod.accept_zip(_zip(tmp_path, files), tmp_path / "r1", limits)
    assert e.value.code == "no_pom"
    with pytest.raises(IntakeRejected):
        intake_mod.accept_zip(_zip(tmp_path, files), tmp_path / "r2", limits, build_targets=["svc-a", "svc-c"])
    res = intake_mod.accept_zip(_zip(tmp_path, files), tmp_path / "r3", limits, build_targets=["svc-a", "svc-b"])
    assert res.project_root == "."


IVAS_ENV = {"IVAS_BASE_URL": "https://ivas.invalid", "IVAS_USER_ID": "tester", "IVAS_PASSWORD": "not-a-real-pw"}


@pytest.fixture()
def ivas_env(monkeypatch):
    for k, v in IVAS_ENV.items():
        monkeypatch.setenv(k, v)


def _migrated(tmp_path: Path, with_root: bool) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    root = msa_tree(run_dir / "project", ROOT_AGG if with_root else None)
    return run_dir, root


def test_chain_hands_the_migration_targets_to_every_maven_call(tmp_path, ivas_env):
    run_dir, root = _migrated(tmp_path, with_root=False)
    maven = FakeMaven()
    comps = make_components(maven=maven, scan=FakeScanner({"baseline": [scan_result()]}))
    events = []
    st = chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id="run-1", journal=events.append,
                                   stop=lambda: False, components=comps, build_targets=["svc-a", "svc-b"])
    assert st is not None and st.build_targets == ["svc-a", "svc-b"] and st.intake.project_root == "."
    assert ("package", ["svc-a", "svc-b"]) in maven.targets_seen
    assert all(t == ["svc-a", "svc-b"] for _c, t in maven.targets_seen)
    assert any(e["event"] == "remediation_build_targets" and e["targets"] == ["svc-a", "svc-b"] for e in events)


def test_chain_root_build_passes_no_targets(tmp_path, ivas_env):
    run_dir, root = _migrated(tmp_path, with_root=True)
    maven = FakeMaven()
    comps = make_components(maven=maven, scan=FakeScanner({"baseline": [scan_result()]}))
    st = chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id="run-1", journal=lambda e: None,
                                   stop=lambda: False, components=comps, build_targets=["."])
    assert st.build_targets == [] and maven.targets_seen and all(t is None for _c, t in maven.targets_seen)


def test_baseline_failure_after_migration_success_says_so(tmp_path, ivas_env):
    run_dir, root = _migrated(tmp_path, with_root=True)
    (root / "copy").mkdir()
    comps = make_components(maven=FakeMaven(outcomes=[False]))
    events = []
    st = chain.run_after_migration(run_dir, root, make_cfg(after_migration=True), run_id="run-1", journal=events.append,
                                   stop=lambda: False, components=comps, exclude_dirs=["copy"])
    assert st.status == FAILED and st.reason == R_BASELINE_BUILD_FAILED
    assert st.message.startswith("the migration's final build passed on this same tree")
    assert "without .git/, .sbmig/, target/ build output, ignored folders copy" in st.message
    assert st.message.endswith("| [ERROR] COMPILATION ERROR")                     # Maven's own lines stay
    assert any(e["event"] == "remediation_baseline_differs" for e in events)
    assert chain.load_state(run_dir).message == st.message


def test_clean_targets():
    assert chain.clean_targets(None) == [] and chain.clean_targets(["."]) == [] and chain.clean_targets([".", "."]) == []
    assert chain.clean_targets(["svc-a", ".", "svc-a/"]) == ["svc-a", "."]
