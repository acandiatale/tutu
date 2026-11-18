"""A project's `.mvn/` folder is out of the way of every Maven call (`build.ignore_project_mvn`, default true): the child's
environment carries `MAVEN_BASEDIR=<an empty folder of this process>`, so the launcher reads no `.mvn/jvm.config`,
`.mvn/maven.config` or `.mvn/extensions.xml` of the project; the `-D` / `-P` options of the `maven.config` Maven would
have found are carried over to the command line, and `-Dmaven.multiModuleProjectDirectory=<root>` puts the project's
root back for the poms. The folder itself is never touched."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbmig.build import mvn as mvnmod
from sbmig.core.config import load_config


def _cfg(**build):
    return load_config(overrides={"artifacts": {"parent": {"group_id": "g", "artifact_id": "a"}}, "build": build})


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


MMPD = "-Dmaven.multiModuleProjectDirectory="


# ---------------------------------------------------------------------------- config
def test_switch_default_on():
    assert _cfg().build.ignore_project_mvn is True
    assert "ignore_project_mvn: true" in (Path(__file__).resolve().parents[2] / "config" / "default.yml").read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------- environment
def test_env_carries_an_empty_base_folder_outside_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVEN_BASEDIR", str(tmp_path / "leaked"))
    env = mvnmod.mvn_env(_cfg())
    base = Path(env["MAVEN_BASEDIR"])
    assert base != tmp_path / "leaked"                       # sbmig's own value is replaced
    assert base.is_dir() and not (base / ".mvn").exists() and list(base.iterdir()) == []
    assert mvnmod.mvn_env(_cfg())["MAVEN_BASEDIR"] == str(base)    # one folder per process, reused


def test_base_folder_is_made_again_when_it_disappeared():
    base = mvnmod.maven_basedir()
    base.rmdir()
    again = mvnmod.maven_basedir()
    assert again.is_dir() and list(again.iterdir()) == []


def test_switch_off_leaves_no_maven_basedir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVEN_BASEDIR", str(tmp_path / "leaked"))
    env = mvnmod.mvn_env(_cfg(ignore_project_mvn=False))
    assert "MAVEN_BASEDIR" not in env
    # an operator who sets it on purpose in build.env keeps it
    env = mvnmod.mvn_env(_cfg(ignore_project_mvn=False, env={"MAVEN_BASEDIR": "/x"}))
    assert env["MAVEN_BASEDIR"] == "/x"


# ---------------------------------------------------------------------------- reading maven.config
@pytest.mark.parametrize("text, props, profiles", [
    ("-Drevision=1.2.3\n-Pci\n", ["-Drevision=1.2.3"], ["-Pci"]),                     # 3.9: one argument per line
    ("-Drevision=1.2.3 -Pci -T 4", ["-Drevision=1.2.3"], ["-Pci"]),                  # 3.8: words on one line
    ("-D\nrevision=1.2.3\n-P\nci,!dev\n", ["-Drevision=1.2.3"], ["-Pci,!dev"]),
    ("--define revision=1 --activate-profiles=ci --activate-profiles nexus", ["-Drevision=1"], ["-Pci", "-Pnexus"]),
    ("--define=flag -P=ci", ["-Dflag"], ["-Pci"]),
    ("-Dmsg=hello world\n", ["-Dmsg=hello world"], []),                              # 3.9: the line is one argument
    ("# -Dold=1\n-B\n", [], []),
    ("-pl a -s x.xml -o -U -q -X -fae -f other.xml -T 1C", [], []),
])
def test_properties_and_profiles_of_maven_config(tmp_path, text, props, profiles):
    found = mvnmod.project_maven_settings(_tree(tmp_path, {".mvn/maven.config": text}))
    assert found is not None
    assert (found.properties, found.profiles) == (props, profiles)


def test_options_as_written(tmp_path):
    found = mvnmod.project_maven_settings(_tree(tmp_path, {".mvn/maven.config": "-s x\n-Drevision=1.2.3 -Pci\n-T 4\n-o"}))
    assert found.options == ["-s x", "-Drevision=1.2.3", "-Pci", "-T 4", "-o"]
    assert found.settings == ["-s x"] and found.offline == ["-o"]


def test_carried_leaves_out_what_sbmig_sets(tmp_path):
    found = mvnmod.project_maven_settings(_tree(tmp_path, {".mvn/maven.config": (
        "-Dmaven.repo.local=/elsewhere -Dmaven.multiModuleProjectDirectory=/x -Dmaven.test.skip=false -DskipTests=false "
        "-Drevision=1 -Dstyle.color=always -Pci")}))
    assert found.carried(exclude={"style.color"}) == ["-Drevision=1", "-Pci"]
    assert found.carried() == ["-Drevision=1", "-Dstyle.color=always", "-Pci"]


# ---------------------------------------------------------------------------- the command
def _base(tmp_path):
    cfg = _cfg(mvn="mvn", local_repo=str(tmp_path / "m2"))
    return cfg, mvnmod.base_command(cfg)


def test_carried_options_go_first_and_the_root_comes_back(tmp_path):
    root = _tree(tmp_path / "p", {"pom.xml": "<project/>", ".mvn/maven.config": "-s x\n-Drevision=1.2.3\n-Pci\n"})
    cfg, base = _base(tmp_path)
    cmd = mvnmod.with_project_mvn(cfg, [*base, "clean", "package", "-Dmaven.test.skip=true"], root, top=root)
    assert cmd == [base[0], "-Drevision=1.2.3", "-Pci", f"{MMPD}{root.absolute()}", *base[1:], "clean", "package",
                   "-Dmaven.test.skip=true"]
    assert "-s" not in cmd[:4] and "x" not in cmd
    # idempotent (a retry builds on the same command)
    assert mvnmod.with_project_mvn(cfg, cmd, root, top=root) == cmd


def test_a_property_sbmig_passes_is_not_carried(tmp_path):
    root = _tree(tmp_path / "p", {".mvn/maven.config": "-Drewrite.exportDatatables=true -Drevision=1"})
    cfg, base = _base(tmp_path)
    cmd = mvnmod.with_project_mvn(cfg, [*base, "-Drewrite.exportDatatables=false"], root, top=root)
    assert "-Drewrite.exportDatatables=true" not in cmd and "-Drevision=1" in cmd


def test_without_mvn_folder_the_root_is_where_the_call_starts(tmp_path):
    root = _tree(tmp_path / "p", {"pom.xml": "<project/>", "svc/pom.xml": "<project/>"})
    cfg, base = _base(tmp_path)
    cmd = mvnmod.with_project_mvn(cfg, [*base, "validate"], root / "svc", top=root)
    assert cmd == [base[0], f"{MMPD}{(root / 'svc').absolute()}", *base[1:], "validate"]


def test_the_nearest_mvn_folder_from_the_call_wins(tmp_path):
    root = _tree(tmp_path / "p", {".mvn/maven.config": "-Droot=1", "svc-a/.mvn/maven.config": "-Dsvc=a",
                                  "svc-a/pom.xml": "<project/>", "svc-b/pom.xml": "<project/>"})
    cfg, base = _base(tmp_path)
    a = mvnmod.with_project_mvn(cfg, [*base], root / "svc-a", top=root)
    assert a[1:3] == ["-Dsvc=a", f"{MMPD}{(root / 'svc-a').absolute()}"]
    b = mvnmod.with_project_mvn(cfg, [*base], root / "svc-b", top=root)
    assert b[1:3] == ["-Droot=1", f"{MMPD}{root.absolute()}"]
    # -f names the pom Maven starts from (deptree runs a service root from the workspace root)
    f = mvnmod.with_project_mvn(cfg, [*base, "-f", "svc-a/pom.xml"], root, top=root)
    assert f[1:3] == ["-Dsvc=a", f"{MMPD}{(root / 'svc-a').absolute()}"]


def test_nothing_above_the_project_tree(tmp_path):
    _tree(tmp_path, {".mvn/maven.config": "-Doutside=1"})
    root = _tree(tmp_path / "p", {"pom.xml": "<project/>"})
    cfg, base = _base(tmp_path)
    cmd = mvnmod.with_project_mvn(cfg, [*base], root, top=root)
    assert "-Doutside=1" not in cmd and cmd[1] == f"{MMPD}{root.absolute()}"


def test_switch_off_leaves_the_command_as_it_is(tmp_path):
    root = _tree(tmp_path / "p", {".mvn/maven.config": "-Drevision=1"})
    cfg = _cfg(mvn="mvn", ignore_project_mvn=False)
    base = mvnmod.base_command(cfg)
    assert mvnmod.with_project_mvn(cfg, [*base, "validate"], root, top=root) == [*base, "validate"]


# ---------------------------------------------------------------------------- what the folder holds
def test_folders_and_the_note(tmp_path):
    root = _tree(tmp_path / "p", {
        "pom.xml": "<project/>",
        ".mvn/maven.config": "-s .mvn/settings.nexus.xml\n-Drevision=1.2.3\n-Pci\n",
        ".mvn/jvm.config": "# old\n-Xmx1g -XX:MaxPermSize=256m\n",
        ".mvn/extensions.xml": ("<extensions><extension><groupId>com.example</groupId><artifactId>ext</artifactId>"
                                "<version>1.0</version></extension></extensions>"),
        ".mvn/wrapper/maven-wrapper.properties": "distributionUrl=x\n",
        ".mvn/settings.nexus.xml": "<settings/>",
        "target/.mvn/maven.config": "-Dbuilt=1",
    })
    (root / "empty" / ".mvn").mkdir(parents=True)                 # an empty marker folder says nothing
    [f] = mvnmod.project_mvn_folders(root)
    assert f.rel == ".mvn" and f.jvm_options == ["-Xmx1g", "-XX:MaxPermSize=256m"]
    assert f.extensions == ["com.example:ext:1.0"] and f.wrapper and f.other_files == ["settings.nexus.xml"]
    assert f.carried() == ["-Drevision=1.2.3", "-Pci"]
    flag, line = mvnmod.project_mvn_note(_cfg(), [f])
    assert flag == "project-mvn-ignored"
    assert line == (
        "the project's .mvn/ folder was not used for the builds of this run (maven.config: -s .mvn/settings.nexus.xml, "
        "-Drevision=1.2.3, -Pci; jvm.config: -Xmx1g -XX:MaxPermSize=256m; extensions.xml: com.example:ext:1.0; "
        "Maven wrapper; other files: settings.nexus.xml); carried over to the command line: -Drevision=1.2.3 -Pci; "
        "the folder is delivered unchanged — check it against JDK 21 and the Maven version in use before building "
        "with it")


def test_note_with_nothing_carried_and_several_folders(tmp_path):
    root = _tree(tmp_path / "p", {"svc-a/.mvn/jvm.config": "-Xmx1g", "svc-b/.mvn/maven.config": "-Pci",
                                  "svc-b/.mvn/wrapper/maven-wrapper.jar": "x"})
    folders = mvnmod.project_mvn_folders(root)
    assert [f.rel for f in folders] == ["svc-a/.mvn", "svc-b/.mvn"]
    _flag, line = mvnmod.project_mvn_note(_cfg(), folders)
    assert line == ("the project's .mvn/ folders were not used for the builds of this run (svc-a/.mvn: jvm.config; "
                    "svc-b/.mvn: maven.config, Maven wrapper); carried over to the command line: -Pci (svc-b/.mvn); "
                    "the folders are delivered unchanged — check them against JDK 21 and the Maven version in use "
                    "before building with them")
    only_jvm = mvnmod.project_mvn_folders(_tree(tmp_path / "q", {".mvn/jvm.config": "-Xmx1g"}))
    assert "; carried over to the command line: nothing; " in mvnmod.project_mvn_note(_cfg(), only_jvm)[1]


def test_no_note_when_switched_off_or_nothing_there(tmp_path):
    root = _tree(tmp_path / "p", {".mvn/jvm.config": "-Xmx1g"})
    assert mvnmod.project_mvn_note(_cfg(ignore_project_mvn=False), mvnmod.project_mvn_folders(root)) is None
    assert mvnmod.project_mvn_note(_cfg(), []) is None


def test_nothing_is_written_into_the_folder(tmp_path):
    root = _tree(tmp_path / "p", {".mvn/maven.config": "-Drevision=1", ".mvn/jvm.config": "-Xmx1g"})
    before = {p: p.read_bytes() for p in (root / ".mvn").rglob("*") if p.is_file()}
    cfg, base = _base(tmp_path)
    mvnmod.with_project_mvn(cfg, [*base], root, top=root)
    mvnmod.project_mvn_note(cfg, mvnmod.project_mvn_folders(root))
    assert {p: p.read_bytes() for p in (root / ".mvn").rglob("*") if p.is_file()} == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX path check")
def test_base_folder_is_not_inside_the_project(tmp_path):
    base = mvnmod.maven_basedir()
    assert tmp_path.resolve() not in base.resolve().parents
