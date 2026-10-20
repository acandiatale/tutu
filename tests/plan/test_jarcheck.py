"""Jar-checked recipe catalog (`sbmig recipes check --jars`): recipe ids are looked up in the real recipe jars."""
from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbmig.plan import jarcheck, recipes_cli
from sbmig.plan.catalog import RecipeCatalog, default_catalog
from sbmig.plan.jarcheck import _yaml_recipe_names, check_jars, resolve_classpath, scan_jar

REPO = Path(__file__).resolve().parents[2]
A, B, DEPS = ("org.openrewrite.recipe:rewrite-a", "org.openrewrite.recipe:rewrite-b",
              "org.openrewrite.recipe:rewrite-deps")

A_YML = """\
type: specs.openrewrite.org/v1beta/category
name: com.a.Category
---
type: specs.openrewrite.org/v1beta/recipe
name: com.a.One
recipeList:
  - org.openrewrite.java.ChangeType:
      oldFullyQualifiedTypeName: a.Old
      newFullyQualifiedTypeName: a.New
---
type: specs.openrewrite.org/v1beta/recipe
name: com.a.Two
displayName: two
"""


def _jar(repo: Path, gav: str, *, yml: str | None = None, classes: tuple[str, ...] = ()) -> Path:
    """A jar in Maven local-repository layout (so `gav_for_jar` maps it back to its coordinates)."""
    g, a, v = gav.split(":")
    p = repo / Path(*g.split(".")) / a / v / f"{a}-{v}.jar"
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        if yml is not None:
            z.writestr("META-INF/rewrite/x.yml", yml)
        for c in classes:
            z.writestr(c.replace(".", "/") + ".class", b"\xca\xfe\xba\xbe")
    return p


@pytest.fixture()
def world(cfg, tmp_path):
    """Local repo with 4 jars; rewrite-a brings rewrite-deps transitively, rewrite-b does not."""
    repo = tmp_path / "m2"
    jars = {
        "a": _jar(repo, f"{A}:1.0", yml=A_YML),
        "b": _jar(repo, f"{B}:2.0", classes=("com.b.Imperative", "com.b.Imperative$Visitor")),
        "core": _jar(repo, "org.openrewrite:rewrite-java:8.0", classes=("org.openrewrite.java.ChangeType",)),
        "deps": _jar(repo, f"{DEPS}:1.5", yml="type: specs.openrewrite.org/v1beta/recipe\nname: com.d.Upgrade\n"
                     "---\ntype: specs.openrewrite.org/v1beta/recipe\nname: com.d.Change\n"),
    }
    cfg.build.local_repo = str(repo)
    cfg.openrewrite.recipe_artifacts = [A, B]
    cfg.openrewrite.recipe_artifact_versions = {A: "1.0", B: "2.0"}     # overrides: no BOM / Maven needed
    classpaths = {A: [jars["a"], jars["core"], jars["deps"]], B: [jars["b"], jars["core"]]}
    calls: list[tuple[str, str]] = []

    def resolver(ga: str, version: str) -> list[Path]:
        calls.append((ga, version))
        return list(classpaths.get(ga, []))

    return {"cfg": cfg, "jars": jars, "classpaths": classpaths, "resolver": resolver, "calls": calls,
            "work_dir": tmp_path / "work"}


def _catalog(*entries: dict) -> RecipeCatalog:
    return RecipeCatalog.from_dict({"version": 1, "recipes": list(entries)})


def _run(world, *entries: dict, **kw):
    return check_jars(_catalog(*entries), world["cfg"], work_dir=world["work_dir"], resolver=world["resolver"], **kw)


def _row(report, rid):
    return next(r for r in report.rows if r.id == rid)


# ---------------------------------------------------------------------------
# jar scanning
# ---------------------------------------------------------------------------
def test_scan_jar_reads_yaml_recipe_names_and_class_entries(world):
    a = scan_jar(world["jars"]["a"])
    assert a.yaml_names == {"com.a.One", "com.a.Two"}            # the category document is not a recipe
    assert a.declares("com.a.One") and not a.declares("com.a.Category")
    b = scan_jar(world["jars"]["b"])
    assert b.declares("com.b.Imperative") and not b.declares("com.b.Other")
    assert scan_jar(world["jars"]["a"]) is a                     # cached per path


def test_yaml_names_fall_back_to_a_line_scan_for_unparseable_files():
    text = ("type: specs.openrewrite.org/v1beta/recipe\nname: com.x.Good\ndescription: [unclosed\n---\n"
            "type: specs.openrewrite.org/v1beta/recipe\nname: 'com.x.Quoted'\n")
    assert _yaml_recipe_names(text) == {"com.x.Good", "com.x.Quoted"}


def test_scan_jar_tolerates_a_broken_file(tmp_path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    assert not scan_jar(bad).declares("anything")


# ---------------------------------------------------------------------------
# check_jars
# ---------------------------------------------------------------------------
def test_found_in_the_declared_artifact(world):
    rep = _run(world, {"id": "com.a.One", "artifact": A}, {"id": "com.b.Imperative", "artifact": B})
    assert rep.problems == []
    one, imp = _row(rep, "com.a.One"), _row(rep, "com.b.Imperative")
    assert (one.found_in, one.version, one.artifact_on_classpath, one.provided_by_ok) == (f"{A}:1.0", "1.0", True, None)
    assert imp.found_in == f"{B}:2.0"                             # imperative recipe: <id>.class
    assert rep.artifact_versions == {A: "1.0", B: "2.0"}
    assert rep.classpaths[B] == ["rewrite-b-2.0.jar", "rewrite-java-8.0.jar"]
    assert world["calls"] == [(A, "1.0"), (B, "2.0")]             # one resolution per configured artifact
    assert rep.declaring_jars == [f"{A}:1.0", f"{B}:2.0"] and rep.ok


def test_found_in_another_artifact_is_a_problem(world):
    rep = _run(world, {"id": "com.a.Two", "artifact": B})
    row = _row(rep, "com.a.Two")
    assert row.found_in == f"{A}:1.0"
    assert rep.problems == [f"catalog com.a.Two: artifact {B} does not declare it; found in {A}:1.0"]


def test_not_found_is_a_problem(world):
    rep = _run(world, {"id": "com.x.Missing", "artifact": A})
    assert _row(rep, "com.x.Missing").found_in == ""
    assert rep.problems == ["catalog com.x.Missing: recipe id not found in any resolved jar (4 jar(s) searched)"]


def test_provided_by_satisfied(world):
    rep = _run(world, {"id": "com.d.Upgrade", "artifact": DEPS, "provided_by": A})
    row = _row(rep, "com.d.Upgrade")
    assert rep.problems == []
    assert (row.found_in, row.version, row.artifact_on_classpath, row.provided_by_ok) == \
        (f"{DEPS}:1.5", "1.5", True, True)
    assert rep.artifact_versions[DEPS] == "1.5"                   # transitive catalog artifact gets its version
    assert [c[0] for c in world["calls"]] == [A, B]               # provided_by already configured: no extra run


def test_provided_by_not_on_its_transitive_classpath(world):
    rep = _run(world, {"id": "com.d.Change", "artifact": DEPS, "provided_by": B})
    row = _row(rep, "com.d.Change")
    assert row.found_in == f"{DEPS}:1.5"                         # declared where the catalog says …
    assert row.provided_by_ok is False and row.artifact_on_classpath is False
    assert rep.problems == [(f"catalog com.d.Change: artifact {DEPS} is not on the transitive classpath of "
                             f"provided_by {B}:2.0")]             # … but B does not bring it in


def test_provided_by_not_configured(world):
    world["cfg"].openrewrite.recipe_artifact_versions["org.example:unlisted"] = "3.0"
    rep = _run(world, {"id": "com.d.Upgrade", "artifact": DEPS, "provided_by": "org.example:unlisted"})
    assert ("org.example:unlisted", "3.0") in world["calls"]      # resolved once anyway
    assert rep.problems == [
        "recipe artifact org.example:unlisted:3.0: dependency resolution failed, no classpath",
        "catalog com.d.Upgrade: provided_by org.example:unlisted is not in openrewrite.recipe_artifacts",
    ]
    assert _row(rep, "com.d.Upgrade").provided_by_ok is False


def test_resolver_returning_nothing_names_the_artifact(world):
    world["classpaths"][B] = []
    rep = _run(world, {"id": "com.a.One", "artifact": A}, {"id": "com.b.Imperative", "artifact": B})
    assert rep.problems[0] == f"recipe artifact {B}:2.0: dependency resolution failed, no classpath"
    assert "catalog com.b.Imperative: recipe id not found in any resolved jar (3 jar(s) searched)" in rep.problems
    assert _row(rep, "com.b.Imperative").artifact_on_classpath is None
    assert _row(rep, "com.a.One").problems == []


def test_core_recipe_is_found_in_the_union(world):
    rep = _run(world, {"id": "org.openrewrite.java.ChangeType", "artifact": None})
    row = _row(rep, "org.openrewrite.java.ChangeType")
    assert rep.problems == []
    assert row.found_in == "org.openrewrite:rewrite-java:8.0" and row.version == ""
    assert row.artifact_on_classpath is None


def test_core_entry_declared_by_a_recipe_artifact_is_a_problem(world):
    rep = _run(world, {"id": "com.a.One", "artifact": None})
    assert rep.problems == [(f"catalog com.a.One: artifact is null (core recipe) but it is declared in {A}:1.0, "
                             "not in an org.openrewrite:* jar")]


def test_project_and_unconfigured_entries(world):
    rep = _run(world,
               {"id": "com.company.migrate.X", "origin": "project", "file": "knowledge/recipes/base/x.yml"},
               {"id": "com.a.One", "artifact": "org.example:elsewhere"})
    assert [r.id for r in rep.rows] == ["com.a.One"]              # project composites are validated offline
    assert rep.problems == [
        ("catalog com.a.One: artifact org.example:elsewhere is not in openrewrite.recipe_artifacts and has no "
         "provided_by"),
        f"catalog com.a.One: artifact org.example:elsewhere does not declare it; found in {A}:1.0",
    ]


def test_artifacts_argument_limits_the_resolution(world):
    rep = _run(world, {"id": "com.a.One", "artifact": A}, artifacts=[A])
    assert [c[0] for c in world["calls"]] == [A]
    assert list(rep.classpaths) == [A] and rep.problems == []


# ---------------------------------------------------------------------------
# resolve_classpath (fake mvn)
# ---------------------------------------------------------------------------
def _fake_mvn(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-mvn"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_resolve_classpath_writes_a_bom_pom_and_parses_cp(world, tmp_path):
    cfg = world["cfg"]
    a, core = world["jars"]["a"], world["jars"]["core"]
    cfg.build.mvn = _fake_mvn(tmp_path, 'for x in "$@"; do case "$x" in -Dmdep.outputFile=*) '
                              f'printf "%s" "{a}:{core}" > "${{x#-Dmdep.outputFile=}}";; esac; done\necho ok\n')
    got = resolve_classpath(cfg, A, "1.0", work_dir=tmp_path / "w")
    assert got == [a, core]
    pom = (tmp_path / "w" / "org.openrewrite.recipe__rewrite-a" / "pom.xml").read_text()
    assert "<artifactId>rewrite-recipe-bom</artifactId>" in pom and "<scope>import</scope>" in pom
    assert f"<version>{cfg.openrewrite.recipe_bom_version}</version>" in pom
    assert "<artifactId>rewrite-a</artifactId>\n      <version>1.0</version>" in pom
    log = (tmp_path / "w" / "org.openrewrite.recipe__rewrite-a" / "build-classpath.log").read_text()
    assert "maven-dependency-plugin:3.8.1:build-classpath" in log and "-Dmdep.includeScope=runtime" in log
    resolve_classpath(cfg, A, "", work_dir=tmp_path / "w")        # "" → version managed by the BOM import
    pom = (tmp_path / "w" / "org.openrewrite.recipe__rewrite-a" / "pom.xml").read_text()
    assert "<artifactId>rewrite-a</artifactId>\n    </dependency>" in pom


def test_resolve_classpath_never_raises(world, tmp_path):
    cfg = world["cfg"]
    cfg.build.mvn = _fake_mvn(tmp_path, 'echo "[ERROR] Could not resolve dependencies"\nexit 1\n')
    assert resolve_classpath(cfg, A, "1.0", work_dir=tmp_path / "w") == []
    cfg.build.mvn = str(tmp_path / "no-such-mvn")
    assert resolve_classpath(cfg, A, "1.0", work_dir=tmp_path / "w") == []


def test_default_resolver_records_log_paths(world, tmp_path):
    cfg = world["cfg"]
    cfg.build.mvn = _fake_mvn(tmp_path, "exit 1\n")
    rep = check_jars(_catalog({"id": "com.a.One", "artifact": A}), cfg, work_dir=tmp_path / "w")
    log_a = tmp_path / "w" / "org.openrewrite.recipe__rewrite-a" / "build-classpath.log"
    assert rep.log_paths[A] == str(log_a.resolve()) and log_a.is_file()
    assert rep.problems[0] == (f"recipe artifact {A}:1.0: dependency resolution failed, no classpath "
                               f"(log: {log_a.resolve()})")


# ---------------------------------------------------------------------------
# CLI: sbmig recipes check --jars (resolver monkeypatched; real repo catalog)
# ---------------------------------------------------------------------------
def _repo_world(tmp_path: Path, monkeypatch, *, broken: str | None = None) -> list[str]:
    """Fake jars declaring every openrewrite id of the REAL catalog where the catalog says it lives."""
    from sbmig.core.config import load_config

    cfg = load_config(REPO / "config" / "artifacts.local-e2e.yml", repo_root=REPO)
    cat = default_catalog(cfg)
    repo = tmp_path / "m2"
    by_artifact: dict[str, list[str]] = {}
    for e in cat.entries:
        if e.origin == "openrewrite":
            by_artifact.setdefault(e.artifact or "org.openrewrite:rewrite-java", []).append(e.id)
    jars = {ga: _jar(repo, f"{ga}:9.9", classes=tuple(ids)) for ga, ids in by_artifact.items()}
    provided = {e.artifact for e in cat.entries if e.provided_by}

    def resolver(ga: str, version: str) -> list[Path]:
        if ga == broken:
            return []
        extra = [jars[x] for x in provided] if ga == "org.openrewrite.recipe:rewrite-migrate-java" else []
        return [*([jars[ga]] if ga in jars else []), jars["org.openrewrite:rewrite-java"], *extra]

    monkeypatch.setattr(jarcheck, "DEFAULT_RESOLVER", resolver)
    extra_cfg = tmp_path / "jars.yml"
    extra_cfg.write_text(json.dumps({
        "build": {"local_repo": str(repo)},
        "openrewrite": {"recipe_artifact_versions": {ga: "9.9" for ga in cfg.openrewrite.recipe_artifacts}},
    }))
    return ["--config", str(REPO / "config" / "artifacts.local-e2e.yml"), "--config", str(extra_cfg),
            "--work-dir", str(tmp_path / "work")]


def test_cli_check_jars_ok_and_json(tmp_path, monkeypatch):
    args = _repo_world(tmp_path, monkeypatch)
    r = CliRunner()
    res = r.invoke(recipes_cli, ["check", "--jars", *args])
    assert res.exit_code == 0, res.output
    assert "recipe catalog OK" in res.output
    assert "recipe jars OK: 29 recipe(s) found in" in res.output and "rewrite-recipe-bom 3.37.0" in res.output
    assert "ok via rewrite-migrate-java" in res.output
    line = next(x for x in res.output.splitlines() if x.startswith("org.openrewrite.java.ChangeType "))
    assert "(core)" in line and "org.openrewrite:rewrite-java:9.9" in line
    res = r.invoke(recipes_cli, ["check", "--jars", "--json", *args])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["problems"] == [] and len(data["rows"]) == 29
    assert data["artifact_versions"]["org.openrewrite.recipe:rewrite-java-dependencies"] == "9.9"


def test_cli_check_jars_reports_problems(tmp_path, monkeypatch):
    args = _repo_world(tmp_path, monkeypatch, broken="org.openrewrite.recipe:rewrite-testing-frameworks")
    res = CliRunner().invoke(recipes_cli, ["check", "--jars", *args])
    assert res.exit_code == 1
    assert ("PROBLEM recipe artifact org.openrewrite.recipe:rewrite-testing-frameworks:9.9: dependency resolution "
            "failed") in res.output
    assert "PROBLEM catalog org.openrewrite.java.testing.junit5.JUnit4to5Migration: recipe id not found" in res.output
    assert "NOT FOUND" in res.output


def test_cli_check_without_jars_does_not_resolve(monkeypatch):
    def boom(ga, version):  # pragma: no cover - must not be called
        raise AssertionError("resolver called without --jars")

    monkeypatch.setattr(jarcheck, "DEFAULT_RESOLVER", boom)
    res = CliRunner().invoke(recipes_cli, ["check"])
    assert res.exit_code == 0 and "recipe catalog OK" in res.output and "recipe jars" not in res.output


# ---------------------------------------------------------------------------
# the real thing (Maven + network / warm ~/.m2)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_real_repo_catalog_against_real_recipe_jars(cfg, tmp_path):
    import os

    if shutil.which(cfg.build.mvn) is None:
        pytest.skip("mvn not available")
    jdk = os.environ.get("JAVA21_HOME") or os.environ.get("SBMIG_TEST_JAVA21_HOME") or ""
    if jdk and Path(jdk).exists():
        cfg.build.java_home = jdk
    rep = check_jars(default_catalog(cfg), cfg, work_dir=tmp_path / "recipes-check")
    assert rep.problems == [], "\n".join(rep.problems)
    assert all(r.found_in for r in rep.rows)
    deps = [r for r in rep.rows if r.provided_by]
    assert deps and all(r.provided_by_ok for r in deps)
