from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from sbmig.build import mvn_env
from sbmig.rewrite import RecipeArtifactError, parse_bom_versions, resolve_recipe_artifact_versions
from sbmig.rewrite.versions import bom_pom_path, ensure_bom, pom_path

BOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>org.openrewrite.recipe</groupId>
  <artifactId>rewrite-recipe-bom</artifactId>
  <version>{ver}</version>
  <packaging>pom</packaging>
  <properties>
    <spring.version>6.37.0</spring.version>
    <chain>${{spring.version}}</chain>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency><groupId>org.openrewrite</groupId><artifactId>rewrite-bom</artifactId><version>8.89.0</version>
        <type>pom</type><scope>import</scope></dependency>
      <dependency><groupId>org.openrewrite</groupId><artifactId>missing-bom</artifactId><version>1.0</version>
        <type>pom</type><scope>import</scope></dependency>
      <dependency><groupId>org.openrewrite.recipe</groupId><artifactId>rewrite-migrate-java</artifactId>
        <version>3.42.0</version></dependency>
      <dependency><groupId>org.openrewrite.recipe</groupId><artifactId>rewrite-spring</artifactId>
        <version>${{chain}}</version></dependency>
      <dependency><groupId>org.openrewrite.recipe</groupId><artifactId>rewrite-self</artifactId>
        <version>${{project.version}}</version></dependency>
      <dependency><groupId>org.openrewrite.recipe</groupId><artifactId>no-version</artifactId></dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""
IMPORTED = """<project><dependencyManagement><dependencies>
<dependency><groupId>org.openrewrite</groupId><artifactId>rewrite-java</artifactId><version>8.89.0</version></dependency>
<dependency><groupId>org.openrewrite.recipe</groupId><artifactId>rewrite-spring</artifactId><version>0.0.1</version></dependency>
</dependencies></dependencyManagement></project>"""


@pytest.fixture()
def repo(tmp_path, cfg):
    r = tmp_path / "m2"
    cfg.build.local_repo = str(r)
    cfg.openrewrite.recipe_bom_version = "9.9.9"
    p = pom_path(r, "org.openrewrite.recipe", "rewrite-recipe-bom", "9.9.9")
    p.parent.mkdir(parents=True)
    p.write_text(BOM_XML.format(ver="9.9.9"))
    imp = pom_path(r, "org.openrewrite", "rewrite-bom", "8.89.0")
    imp.parent.mkdir(parents=True)
    imp.write_text(IMPORTED)
    return r


def test_parse_bom_interpolates_and_merges_imports(repo, cfg):
    v = parse_bom_versions(bom_pom_path(cfg), local_repo=repo)
    assert v == {
        "org.openrewrite:rewrite-java": "8.89.0",              # from the locally available imported BOM
        "org.openrewrite.recipe:rewrite-spring": "6.37.0",     # direct entry wins over the import
        "org.openrewrite.recipe:rewrite-migrate-java": "3.42.0",
        "org.openrewrite.recipe:rewrite-self": "9.9.9",
    }
    assert "org.openrewrite.recipe:rewrite-spring" in parse_bom_versions(bom_pom_path(cfg))


def test_resolve_with_overrides_and_cache(repo, cfg, tmp_path):
    cfg.openrewrite.recipe_artifact_versions = {"org.openrewrite.recipe:rewrite-spring": "6.0.0-override"}
    cache = tmp_path / "run" / "logs" / "rewrite-artifact-versions.json"
    got = resolve_recipe_artifact_versions(cfg, mvn_env(cfg), cache_path=cache,
                                           artifacts=["org.openrewrite.recipe:rewrite-migrate-java",
                                                      "org.openrewrite.recipe:rewrite-spring"])
    assert got == {"org.openrewrite.recipe:rewrite-migrate-java": "3.42.0",
                   "org.openrewrite.recipe:rewrite-spring": "6.0.0-override"}
    data = json.loads(cache.read_text())
    assert data["bom_version"] == "9.9.9" and data["versions"]["org.openrewrite.recipe:rewrite-migrate-java"] == "3.42.0"
    bom_pom_path(cfg).unlink()                                   # second call is served from the cache
    again = resolve_recipe_artifact_versions(cfg, mvn_env(cfg), cache_path=cache,
                                             artifacts=["org.openrewrite.recipe:rewrite-migrate-java"])
    assert again == {"org.openrewrite.recipe:rewrite-migrate-java": "3.42.0"}


def test_resolve_only_overrides_needs_no_bom(cfg, tmp_path):
    cfg.build.local_repo = str(tmp_path / "empty")
    cfg.openrewrite.recipe_artifact_versions = {a: "1.0" for a in cfg.openrewrite.recipe_artifacts}
    assert set(resolve_recipe_artifact_versions(cfg, {}).values()) == {"1.0"}


def test_resolve_unknown_artifact_raises(repo, cfg):
    with pytest.raises(RecipeArtifactError, match="rewrite-nope"):
        resolve_recipe_artifact_versions(cfg, mvn_env(cfg), artifacts=["org.openrewrite.recipe:rewrite-nope"])


def _script(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-mvn"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_ensure_bom_fetches_with_dependency_get(cfg, tmp_path):
    repo = tmp_path / "m2"
    cfg.build.local_repo = str(repo)
    cfg.openrewrite.recipe_bom_version = "1.2.3"
    target = pom_path(repo, "org.openrewrite.recipe", "rewrite-recipe-bom", "1.2.3")
    args_file = tmp_path / "args.txt"
    # mimics `dependency:get` downloading the pom and then failing on an imported BOM (exit 1)
    cfg.build.mvn = _script(tmp_path, f'echo "$@" > {args_file}\nmkdir -p {target.parent}\n'
                                      f'echo "<project/>" > {target}\nexit 1\n')
    work = tmp_path / "work"
    assert ensure_bom(cfg, mvn_env(cfg), work_dir=work) == target
    args = args_file.read_text()
    assert "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get" in args
    assert "-Dartifact=org.openrewrite.recipe:rewrite-recipe-bom:1.2.3:pom" in args
    assert "-Dtransitive=false" in args and f"-Dmaven.repo.local={repo}" in args
    assert work.is_dir()


def test_ensure_bom_failure(cfg, tmp_path):
    cfg.build.local_repo = str(tmp_path / "m2")
    cfg.build.mvn = _script(tmp_path, 'echo "[ERROR] Could not find artifact"\nexit 1\n')
    with pytest.raises(RecipeArtifactError, match="Could not find artifact"):
        ensure_bom(cfg, mvn_env(cfg), work_dir=tmp_path / "w")
    cfg.build.mvn = str(tmp_path / "no-such-mvn")
    with pytest.raises(RecipeArtifactError):
        ensure_bom(cfg, mvn_env(cfg), work_dir=tmp_path / "w")


@pytest.mark.integration
def test_real_bom_3_38_0_versions(cfg, tmp_path):
    """Resolve from the REAL rewrite-recipe-bom 3.38.0 on Maven Central (uses ~/.m2)."""
    cfg.openrewrite.recipe_bom_version = "3.38.0"
    jdk = os.environ.get("SBMIG_TEST_JAVA21_HOME",
                         "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home")
    if Path(jdk).exists():
        cfg.build.java_home = jdk
    got = resolve_recipe_artifact_versions(cfg, mvn_env(cfg), work_dir=tmp_path)
    assert got == {
        "org.openrewrite.recipe:rewrite-migrate-java": "3.43.0",
        "org.openrewrite.recipe:rewrite-spring": "6.38.0",
        "org.openrewrite.recipe:rewrite-testing-frameworks": "3.45.0",
        "org.openrewrite.recipe:rewrite-static-analysis": "2.42.0",
    }
