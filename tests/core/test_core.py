from pathlib import Path

from sbmig.core.config import load_config
from sbmig.core.models import BuildError, BuildResult, ErrorKind, GAV, ProjectProfile
from sbmig.core.workspace import Workspace


def test_load_config_layers(repo_root):
    cfg = load_config(repo_root / "config" / "artifacts.local-e2e.yml", repo_root=repo_root)
    assert cfg.target.spring_boot_version == "4.1.0"
    assert cfg.artifacts.parent.artifact_id == "spring-boot-starter-parent"
    assert len(cfg.artifacts.mandatory) == 2 and len(cfg.artifacts.optional) == 8
    assert cfg.llm.provider == "fake"
    assert "com.google.guava:guava" in cfg.optional_keys()


def test_gav_parse():
    g = GAV.parse("org.foo:bar:1.2")
    assert g.key == "org.foo:bar" and g.coordinates == "org.foo:bar:1.2"


def test_workspace_from_dir_and_git(tmp_path: Path):
    src = tmp_path / "proj"
    (src / "src").mkdir(parents=True)
    (src / "pom.xml").write_text("<project/>")
    (src / "target").mkdir()
    (src / "target" / "x.class").write_text("junk")
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1")
    assert ws.exists("pom.xml") and not ws.exists("target/x.class")
    sha0 = ws.git_init()
    ws.write("src/A.java", "class A {}")
    sha1 = ws.commit("add A")
    assert sha0 != sha1 and ws.changed_files_since(sha0) == ["src/A.java"] and "src/A.java" in ws.diff(sha0)
    ws.write("src/A.java", "class A { int x; }")
    assert ws.is_dirty()
    ws.revert_to(sha1)
    assert ws.read("src/A.java") == "class A {}"
    assert ws.zip_result().exists() and ws.write_patch().exists()
    ws.journal({"event": "x"})
    assert ws.journal_path.read_text().count("\n") == 2  # intake + x


def test_workspace_from_zip_descends_single_folder(tmp_path: Path):
    import zipfile
    z = tmp_path / "p.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("myproj/pom.xml", "<project/>")
        zf.writestr("myproj/src/Main.java", "class Main {}")
    ws = Workspace.from_input(z, tmp_path / "out", run_id="r2")
    assert ws.exists("pom.xml") and ws.exists("src/Main.java")


def test_build_result_signature_multiset():
    errs = [BuildError(kind=ErrorKind.COMPILE, message="m", signature="s1"), BuildError(kind=ErrorKind.COMPILE, message="m", signature="s1")]
    br = BuildResult(ok=False, goal="compile", command=["mvn"], exit_code=1, duration_s=1.0, log_path="/x", errors=errs)
    assert br.signature_multiset() == {"s1": 2}


def test_profile_fingerprint_stable():
    a = ProjectProfile.make_fingerprint(["pom.xml", "a/pom.xml"], "root")
    b = ProjectProfile.make_fingerprint(["a/pom.xml", "pom.xml"], "root")
    assert a == b and len(a) == 16


def test_role_parent_keys_and_specs():
    """Round 9b: boot_parent / comp_parent join the root parent wherever "a configured in-house parent" matters
    (parent-resolution abort, rewrite parent check, preflight, Boot-managed detection)."""
    from pathlib import Path as _P

    from sbmig.core.config import load_config
    repo = _P(__file__).resolve().parents[2]
    cfg = load_config(repo / "config" / "artifacts.example.yml")
    assert cfg.artifacts.role_parents
    assert [s.artifact_id for s in cfg.artifacts.parent_specs()] == ["base-parent", "boot-jar", "comp-jar"]
    assert cfg.artifacts.parent_keys() == ["com.company.platform:base-parent", "com.company.platform:boot-jar",
                                           "com.company.platform:comp-jar"]
    plain = load_config(repo / "config" / "artifacts.local-e2e.yml")
    assert not plain.artifacts.role_parents and plain.artifacts.parent_keys() == [plain.artifacts.parent.key]
