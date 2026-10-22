"""`mvn dependency:tree` parsing, paths and the javax API inventory (catalog X1 / X1b, 2026-10-22)."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.build import deptree as dt
from sbmig.build.deptree import (JAVAX_API_PATTERNS, dependency_tree, is_javax_api, javax_api_inventory,
                                 matches_pattern, parse_tree, paths_to)
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import GAV
from sbmig.core.workspace import Workspace

SINGLE = """com.acme:shop:jar:1.0.0
+- org.qlrm:qlrm:jar:2.1.1:compile
|  \\- javax.persistence:javax.persistence-api:jar:2.2:compile
+- org.springframework.boot:spring-boot-starter-data-jpa:jar:4.1.0:compile
|  \\- jakarta.persistence:jakarta.persistence-api:jar:3.2.0:compile (version managed from 3.1.0)
\\- javax.servlet:javax.servlet-api:jar:4.0.1:test
"""

REACTOR = """com.acme:shop:pom:1.0.0\r
\r
com.acme:shop-back:jar:1.0.0\r
+- org.qlrm:qlrm:jar:2.1.1:compile\r
|  \\- javax.persistence:javax.persistence-api:jar:2.2:compile\r
\\- com.querydsl:querydsl-apt:jar:jakarta:5.1.0:provided\r
   \\- javax.inject:javax.inject:jar:1:provided\r
com.acme:shop-boot:jar:1.0.0\r
\\- com.acme:shop-back:jar:1.0.0:compile\r
   \\- org.qlrm:qlrm:jar:2.1.1:compile\r
      \\- javax.persistence:javax.persistence-api:jar:2.2:compile\r
"""


def test_parse_single_tree_scopes_and_notes():
    roots = parse_tree(SINGLE)
    assert len(roots) == 1
    r = roots[0]
    assert r.root and r.depth == 0 and r.gav.coordinates == "com.acme:shop:1.0.0" and r.gav.type is None
    assert [c.gav.key for c in r.children] == ["org.qlrm:qlrm", "org.springframework.boot:spring-boot-starter-data-jpa",
                                               "javax.servlet:javax.servlet-api"]
    qlrm = r.children[0]
    assert qlrm.depth == 1 and qlrm.scope == "compile" and qlrm.text == "org.qlrm:qlrm:jar:2.1.1:compile"
    api = qlrm.children[0]
    assert api.depth == 2 and api.gav.coordinates == "javax.persistence:javax.persistence-api:2.2"
    managed = r.children[1].children[0]
    assert managed.note == "(version managed from 3.1.0)" and managed.gav.version == "3.2.0"
    assert r.children[2].scope == "test"


def test_parse_reactor_multi_root_classifier_and_crlf():
    roots = parse_tree(REACTOR)
    assert [r.gav.artifact_id for r in roots] == ["shop", "shop-back", "shop-boot"]
    assert roots[0].gav.type == "pom" and not roots[0].children
    apt = roots[1].children[1]
    assert apt.gav.classifier == "jakarta" and apt.gav.version == "5.1.0" and apt.scope == "provided"
    assert apt.children[0].gav.coordinates == "javax.inject:javax.inject:1"
    deep = roots[2].children[0].children[0].children[0]
    assert deep.depth == 3 and deep.gav.artifact_id == "javax.persistence-api"


def test_parse_verbose_omitted_and_garbage():
    text = ("com.acme:a:jar:1\n+- (javax.inject:javax.inject:jar:1:compile - omitted for duplicate)\n"
            "[INFO] not a tree line at all\n\\- x\n")
    roots = parse_tree(text)
    assert len(roots) == 1 and len(roots[0].children) == 1
    assert roots[0].children[0].omitted and roots[0].children[0].gav.key == "javax.inject:javax.inject"


def test_paths_to():
    roots = parse_tree(REACTOR)
    paths = paths_to(roots, lambda n: n.gav.artifact_id == "javax.persistence-api")
    assert [[n.gav.artifact_id for n in p] for p in paths] == [
        ["shop-back", "qlrm", "javax.persistence-api"],
        ["shop-boot", "shop-back", "qlrm", "javax.persistence-api"]]


def test_patterns():
    assert matches_pattern(GAV.parse("javax.persistence:javax.persistence-api:2.2"), "javax.persistence:*")
    assert matches_pattern(GAV.parse("jakarta.persistence:jakarta.persistence-api:2.2.3"),
                           "jakarta.persistence:jakarta.persistence-api:*:2.*")
    assert not is_javax_api(GAV.parse("jakarta.persistence:jakarta.persistence-api:3.2.0"))
    assert is_javax_api(GAV.parse("jakarta.servlet:jakarta.servlet-api:4.0.4"))
    assert is_javax_api(GAV.parse("com.sun.mail:javax.mail:1.6.2"))
    assert not is_javax_api(GAV.parse("com.sun.mail:jakarta.mail:2.0.1"))
    assert "javax.persistence:*" in JAVAX_API_PATTERNS


# ---------------------------------------------------------------- running Maven (faked)
def _project(tmp_path: Path, modules: bool = False) -> Workspace:
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    if modules:
        (root / "pom.xml").write_text("<project><groupId>com.acme</groupId><artifactId>shop</artifactId>"
                                      "<packaging>pom</packaging><modules><module>shop-back</module>"
                                      "<module>shop-boot</module></modules></project>")
        for m in ("shop-back", "shop-boot"):
            (root / m).mkdir()
            (root / m / "pom.xml").write_text(f"<project><parent><groupId>com.acme</groupId><artifactId>shop"
                                              f"</artifactId></parent><artifactId>{m}</artifactId></project>")
    else:
        (root / "pom.xml").write_text("<project><groupId>com.acme</groupId><artifactId>shop</artifactId></project>")
    return Workspace(root, tmp_path / "run", "t")


def test_dependency_tree_command_cache_and_journal(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd, env, log_path, timeout_s):
        calls.append(list(cmd))
        out = next(c.split("=", 1)[1] for c in cmd if c.startswith("-DoutputFile="))
        Path(out).write_text(SINGLE, encoding="utf-8")
        assert timeout_s == 600
        return ProcessResult(exit_code=0, text="", duration_s=0.1)
    monkeypatch.setattr(dt, "run_logged", fake_run)
    roots = dependency_tree(ws, cfg, None, ["javax.persistence:*", "javax.servlet:*"])
    assert len(roots) == 1 and roots[0].children
    cmd = calls[0]
    assert "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree" in cmd
    assert "-DappendOutput=true" in cmd and "-Dincludes=javax.persistence:*,javax.servlet:*" in cmd and "-q" in cmd
    assert "-pl" not in cmd
    files = sorted(p.name for p in (ws.run_dir / "deptree").iterdir())
    assert files[0].startswith("root-") and files[0].endswith(".meta.json") and files[1].endswith(".txt")
    # cached: no second Maven run while the pom is unchanged
    dependency_tree(ws, cfg, None, ["javax.servlet:*", "javax.persistence:*"])
    assert len(calls) == 1
    ev = [json.loads(x) for x in ws.journal_path.read_text().splitlines()]
    assert ev[-1]["event"] == "deptree" and ev[-1]["ok"] is True and ev[-1]["module"] is None


def test_dependency_tree_module_and_failure(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path, modules=True)
    calls: list[list[str]] = []

    def fail(cmd, *, cwd, env, log_path, timeout_s):
        calls.append(list(cmd))
        return ProcessResult(exit_code=1, text="[ERROR] boom", duration_s=0.1)
    monkeypatch.setattr(dt, "run_logged", fail)
    assert dependency_tree(ws, cfg, "shop-back", ["javax.persistence:*"]) == []
    assert calls[0][calls[0].index("-pl") + 1] == "shop-back" and "-am" in calls[0]
    assert "package" in calls[1] and "-Dmaven.test.skip=true" in calls[1]          # reactor fallback
    ev = json.loads(ws.journal_path.read_text().splitlines()[-1])
    assert ev == {**ev, "event": "deptree", "ok": False, "module": "shop-back"}
    assert dependency_tree(ws, cfg, "no-such-module") == []


def test_dependency_tree_never_raises(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(dt, "run_logged", boom)
    assert dependency_tree(ws, cfg) == []


def test_inventory_reactor_skips_siblings_and_dedupes(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path, modules=True)
    seen: list = []

    def fake_tree(ws_, cfg_, module=None, includes=()):
        seen.append((module, tuple(includes)))
        return parse_tree(REACTOR) if module is None else []
    monkeypatch.setattr(dt, "dependency_tree", fake_tree)
    inv = javax_api_inventory(ws, cfg)
    assert seen == [(None, JAVAX_API_PATTERNS)]                  # every module showed up in the root run
    by = {(j.artifact.key, j.via.key if j.via else None): j for j in inv}
    jpa = by[("javax.persistence:javax.persistence-api", "org.qlrm:qlrm")]
    assert jpa.module == "shop-back" and jpa.scope == "compile" and not jpa.declared
    assert jpa.path == ["com.acme:shop-back:jar:1.0.0", "org.qlrm:qlrm:jar:2.1.1:compile",
                        "javax.persistence:javax.persistence-api:jar:2.2:compile"]
    inject = by[("javax.inject:javax.inject", "com.querydsl:querydsl-apt")]
    assert inject.via.classifier == "jakarta" and inject.scope == "provided"
    assert len(inv) == 2                                          # shop-boot's copy (via the sibling) deduped


def test_inventory_runs_missing_modules_declared_and_test_scope(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path, modules=True)
    seen: list = []
    back = ("com.acme:shop-back:jar:1.0.0\n+- javax.persistence:javax.persistence-api:jar:2.2:compile\n"
            "\\- javax.servlet:javax.servlet-api:jar:4.0.1:test\n")
    boot = "com.acme:shop-boot:jar:1.0.0\n\\- javax.servlet:javax.servlet-api:jar:4.0.1:test\n"

    def fake_tree(ws_, cfg_, module=None, includes=()):
        seen.append(module)
        return {None: [], "shop-back": parse_tree(back), "shop-boot": parse_tree(back + boot)}[module]
    monkeypatch.setattr(dt, "dependency_tree", fake_tree)
    inv = javax_api_inventory(ws, cfg)
    assert seen == [None, "shop-back", "shop-boot"]
    keys = [(j.module, j.artifact.artifact_id, j.declared, j.scope) for j in inv]
    # shop-back: test-scope servlet dropped (a compile match exists); shop-boot: only test → kept
    assert keys == [("shop-back", "javax.persistence-api", True, "compile"),
                    ("shop-boot", "javax.servlet-api", True, "test")]


def test_inventory_never_raises(tmp_path, cfg, monkeypatch):
    ws = _project(tmp_path)
    monkeypatch.setattr(dt, "dependency_tree", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert javax_api_inventory(ws, cfg) == []
