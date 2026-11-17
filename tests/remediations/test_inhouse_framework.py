"""In-house group `framework`: the `framework.artifacts` patterns as in-house artifacts (inventory, planner, policy hash,
report). Fakes only: hand-made poms and resolutions, a fake version source."""
from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest

from sbmig.core.config import ArtifactsConfig, FrameworkConfig, RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import planner as P
from sbmig.remediations import report
from sbmig.remediations.inhouse import FRAMEWORK_GROUP, GROUPS, InhouseKeys, inhouse_keys, inhouse_on_path
from sbmig.remediations.inventory import build_inventory
from sbmig.remediations.models import (
    O_TRANSITIVE,
    Finding,
    ModuleResolution,
    RemediationState,
    ResolvedDep,
    ResolvedModel,
    RunPaths,
    ScanResult,
    write_json_atomic,
)

FW = "com.example.fw:example-fw-core"
FW_WEB = "com.example.fw:example-fw-web"
CORE = "com.example.inhouse:inhouse-core"
BASE = "com.example.inhouse:base-parent"
LIB = "org.example:lib"
OLD = ["mandatory", "optional", "parents"]
NEW = OLD + ["framework"]


def _spec(ga: str):
    g, a = ga.split(":")
    return {"group_id": g, "artifact_id": a, "version": "4.0.0"}


def _cfg(groups=NEW, patterns=(FW,), *, artifacts=True):
    c = types.SimpleNamespace(remediation=RemediationConfig(inhouse_groups=list(groups),
                                                            policy=RemediationPolicyConfig()),
                              framework=FrameworkConfig(artifacts=list(patterns)))
    if artifacts:
        c.artifacts = ArtifactsConfig(parent=_spec(BASE), mandatory=[_spec(CORE)])
    return c


# ---------------------------------------------------------------------------- the key table
def test_framework_is_a_known_group(caplog):
    assert FRAMEWORK_GROUP in GROUPS
    with caplog.at_level(logging.WARNING, logger="sbmig.remediations.inhouse"):
        inhouse_keys(_cfg())
    assert not [r for r in caplog.records if "unknown group" in r.getMessage()]


@pytest.mark.parametrize("pattern", [FW, "example-fw-core", "example-fw-*", "com.example.fw:*", "*:example-fw-c*"])
def test_pattern_shapes(pattern):
    keys = inhouse_keys(_cfg(patterns=[pattern]))
    assert FW in keys and keys.get(FW) == FRAMEWORK_GROUP and keys.group_of(f"{FW}:1.2.0") == FRAMEWORK_GROUP
    assert LIB not in keys and keys.get(LIB) is None
    assert inhouse_on_path(LIB, ["org.example:a:1", f"{FW}:jar:1.2.0"], keys) == [FW]


def test_patterns_add_no_keys_and_exact_keys_win():
    keys = inhouse_keys(_cfg(patterns=["com.example.inhouse:*"]))
    assert dict(keys) == {CORE: "mandatory", BASE: "parents"}          # iteration / equality: exact keys only
    assert keys[CORE] == "mandatory" and keys["com.example.inhouse:other"] == FRAMEWORK_GROUP
    with pytest.raises(KeyError):
        keys[LIB]


def test_group_not_listed_or_no_patterns_is_the_old_table():
    for c in (_cfg(groups=OLD), _cfg(patterns=())):
        keys = inhouse_keys(c)
        assert FW not in keys and keys.patterns == ()
        assert keys == inhouse_keys(_cfg(groups=OLD, patterns=()))
        assert inhouse_on_path(LIB, [f"{FW}:1.2.0"], keys) == []


def test_framework_only_table_is_truthy_and_works_without_artifacts():
    keys = inhouse_keys(_cfg(groups=["framework"], artifacts=False))
    assert keys == {} and bool(keys) and inhouse_on_path(FW, [], keys) == [FW]
    assert not InhouseKeys()
    assert inhouse_keys(types.SimpleNamespace(remediation=RemediationConfig(inhouse_groups=["framework"]))) == {}


# ---------------------------------------------------------------------------- policy hash
def test_policy_hash_without_patterns_is_the_old_hash():
    old = P.policy_hash(_cfg(groups=OLD, patterns=()))
    assert P.policy_hash(_cfg(groups=NEW, patterns=())) == old
    assert P.policy_hash(_cfg(groups=OLD, patterns=(FW,))) == old          # group not listed: patterns ignored
    nofw = _cfg(groups=NEW)
    del nofw.framework                                                     # an older config object
    assert P.policy_hash(nofw) == old


def test_policy_hash_covers_the_patterns():
    with_fw = P.policy_hash(_cfg())
    assert with_fw != P.policy_hash(_cfg(patterns=()))
    assert with_fw != P.policy_hash(_cfg(patterns=(FW, FW_WEB)))
    assert with_fw == P.policy_hash(_cfg(patterns=(FW, FW)))


# ---------------------------------------------------------------------------- inventory + planner
def _pom(deps: str, name: str = "app") -> str:
    return ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>org.example</groupId>\n"
            f"  <artifactId>{name}</artifactId>\n  <version>1.0.0</version>\n  <dependencies>\n{deps}  </dependencies>\n"
            "</project>\n")


def _dep(ga: str, v: str) -> str:
    g, a = ga.split(":")
    return (f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n"
            f"      <version>{v}</version>\n    </dependency>\n")


def _root(mods: list[str]) -> str:
    body = "".join(f"    <module>{m}</module>\n" for m in mods)
    return ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>org.example</groupId>\n"
            "  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
            f"  <modules>\n{body}  </modules>\n</project>\n")


def _rd(ga: str, v: str, *, direct=True, path=()) -> ResolvedDep:
    g, a = ga.split(":")
    return ResolvedDep(group_id=g, artifact_id=a, version=v, direct=direct, path=list(path))


def _model(mods: dict[str, list[ResolvedDep]]) -> ResolvedModel:
    m = ResolvedModel(complete=True)
    for d, deps in mods.items():
        m.modules.append(ModuleResolution(dir=d, group_id="org.example", artifact_id=d, version="1.0.0",
                                          dependencies=deps))
    return m


def _f(ga: str, v: str, fid="CVE-1", hint="9.9.9") -> Finding:
    return Finding(id=fid, kind="library", identifier=ga, version=v, patched_version=hint, score=9.8)


class _Src:
    def versions(self, g, a):
        return ["1.2.5", "9.9.9"]

    def evidence(self, g, a, v):
        return "repository:nexus.invalid"


def _write(tmp: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_text(text, encoding="utf-8")
    return tmp


def _run(project: Path, findings, resolved, cfg):
    inv = build_inventory(project, findings, resolved, inhouse=inhouse_keys(cfg))
    return inv, P.plan_round(project, inv, cfg, _Src(), round=1, source_tree_hash="t" * 64)


def _two_modules(tmp: Path) -> tuple[Path, ResolvedModel]:
    # app-a: the framework artifact (declared) and lib under it; app-b: lib declared by the project itself
    p = _write(tmp, {"pom.xml": _root(["app-a", "app-b"]), "app-a/pom.xml": _pom(_dep(FW, "1.2.0"), "app-a"),
                     "app-b/pom.xml": _pom(_dep(LIB, "1.2.3"), "app-b")})
    resolved = _model({"app-a": [_rd(FW, "1.2.0"), _rd(LIB, "1.2.3", direct=False, path=[f"{FW}:1.2.0"])],
                       "app-b": [_rd(LIB, "1.2.3")]})
    return p, resolved


@pytest.mark.parametrize("pattern", [FW, "example-fw-core", "example-fw-*"])
def test_framework_itself_and_its_transitive_are_residuals(tmp_path, pattern):
    p = _write(tmp_path, {"pom.xml": _root(["app-a"]), "app-a/pom.xml": _pom(_dep(FW, "1.2.0"), "app-a")})
    resolved = _model({"app-a": [_rd(FW, "1.2.0"), _rd(LIB, "1.2.3", direct=False, path=[f"{FW}:1.2.0"])]})
    inv, plan = _run(p, [_f(FW, "1.2.0", "CVE-FW"), _f(LIB, "1.2.3", "CVE-LIB")], resolved, _cfg(patterns=[pattern]))
    assert not plan.proposals
    by = {r.ga: r for r in plan.residuals}
    assert by[FW].reason == "inhouse_artifact" and by[FW].inhouse == [FW] and "in-house artifact" in by[FW].detail
    (trans,) = [i for i in inv.items if i.ga == LIB]
    assert trans.origin == O_TRANSITIVE and trans.inhouse == [FW] and trans.inhouse_modules == ["app-a"]
    assert by[LIB].reason == "inhouse_artifact" and by[LIB].inhouse == [FW] and FW in by[LIB].detail


def test_same_library_outside_the_framework_is_still_planned(tmp_path):
    p, resolved = _two_modules(tmp_path)
    _inv, plan = _run(p, [_f(FW, "1.2.0", "CVE-FW"), _f(LIB, "1.2.3", "CVE-LIB")], resolved, _cfg())
    (fw,) = [r for r in plan.residuals if r.ga == FW]
    assert fw.reason == "inhouse_artifact"
    # the project's own declaration in app-b is planned as before; the framework never gets a proposal
    assert [(x.ga, x.to_version, x.modules) for x in plan.proposals] == [(LIB, "9.9.9", ["app-b"])]
    assert all(x.ga != FW and x.kind != "managed_override" for x in plan.proposals)


def test_transitive_reached_both_under_and_outside_the_framework_is_split(tmp_path):
    p = _write(tmp_path, {"pom.xml": _root(["app-a", "app-b"]), "app-a/pom.xml": _pom(_dep(FW, "1.2.0"), "app-a"),
                          "app-b/pom.xml": _pom(_dep("org.example:other", "1"), "app-b")})
    resolved = _model({"app-a": [_rd(FW, "1.2.0"), _rd(LIB, "1.2.3", direct=False, path=[f"{FW}:1.2.0"])],
                       "app-b": [_rd("org.example:other", "1"),
                                 _rd(LIB, "1.2.3", direct=False, path=["org.example:other:1"])]})
    findings = [_f(LIB, "1.2.3", "CVE-LIB")]
    inv, plan = _run(p, findings, resolved, _cfg())
    _inv_old, old = _run(p, findings, resolved, _cfg(groups=OLD, patterns=()))
    # round "where it comes from" (contract B): app-a (under the framework) and app-b (the project's) are two items
    (inh,) = [i for i in inv.items if i.inhouse]
    (own,) = [i for i in inv.items if not i.inhouse]
    assert inh.modules == inh.inhouse_modules == ["app-a"] and inh.inhouse == [FW]
    assert own.modules == ["app-b"] and own.inhouse_modules == []
    assert [(r.reason, r.modules) for r in plan.residuals if r.reason == "inhouse_artifact"] == [
        ("inhouse_artifact", ["app-a"])]
    # app-b is planned by its own facts: an entry in its own pom (it does not inherit from the root); the old
    # whole item got one per module pom, app-a's included
    assert [(x.kind, x.edits[0].pom, x.modules) for x in plan.proposals] == [
        ("managed_override", "app-b/pom.xml", ["app-b"])]
    assert sorted(x.edits[0].pom for x in old.proposals) == ["app-a/pom.xml", "app-b/pom.xml"]
    assert [r.reason for r in plan.residuals if r.reason != "inhouse_artifact"] == [r.reason for r in old.residuals]


def test_without_the_group_the_old_behaviour(tmp_path):
    p, resolved = _two_modules(tmp_path)
    findings = [_f(FW, "1.2.0", "CVE-FW"), _f(LIB, "1.2.3", "CVE-LIB")]
    _inv, old = _run(p, findings, resolved, _cfg(groups=OLD, patterns=()))
    for cfg in (_cfg(groups=OLD), _cfg(patterns=())):
        inv, plan = _run(p, findings, resolved, cfg)
        assert all(i.inhouse == [] for i in inv.items)
        assert "inhouse_artifact" not in {r.reason for r in plan.residuals}
        assert [x.to_dict() for x in plan.proposals] == [x.to_dict() for x in old.proposals]
        assert [r.to_dict() for r in plan.residuals] == [r.to_dict() for r in old.residuals]
    assert any(x.ga == FW for x in old.proposals)        # without the rule the framework itself would be proposed


# ---------------------------------------------------------------------------- report
def _state(tmp: Path) -> tuple[RemediationState, RunPaths]:
    from sbmig.remediations.models import Residual
    paths = RunPaths(tmp / "run")
    res = [Residual(ga=FW, version="1.2.0", origin="direct_literal", reason="inhouse_artifact",
                    detail=f"{FW} is an in-house artifact: its owner publishes the fixed version", finding_ids=["CVE-FW"],
                    modules=["app-a"], inhouse=[FW]),
           Residual(ga=LIB, version="1.2.3", origin="transitive", reason="inhouse_artifact",
                    detail=f"brought in by the in-house artifact(s) {FW}: only their owner can change it",
                    finding_ids=["CVE-LIB"], modules=["app-a"], via=[f"{FW}:1.2.0", f"{LIB}:1.2.3"], inhouse=[FW]),
           Residual(ga="org.example:json", version="2.0", origin="transitive", reason="inhouse_artifact",
                    detail=f"brought in by the in-house artifact(s) {CORE}: only their owner can change it",
                    finding_ids=["CVE-J"], modules=["app-a"], via=[f"{CORE}:4.0.0", "org.example:json:2.0"],
                    inhouse=[CORE])]
    scan = ScanResult(label="baseline", zip_sha256="a" * 64, complete=True,
                      findings=[_f(FW, "1.2.0", "CVE-FW"), _f(LIB, "1.2.3", "CVE-LIB"),
                                _f("org.example:json", "2.0", "CVE-J")])
    write_json_atomic(paths.scan("baseline"), scan.to_dict())
    state = RemediationState(id="r", name="app.zip", status="INCOMPLETE", reason="inhouse_artifact",
                             reasons=["inhouse_artifact"], residuals=res, baseline_scan="scans/baseline.json")
    return state, paths


def test_report_labels_the_framework_group(tmp_path):
    state, paths = _state(tmp_path)
    keys = inhouse_keys(_cfg())
    md = report.build_report_md(state, paths, inhouse=keys)
    section = md.split("## 사내 라이브러리에서 발생한 취약점", 1)[1].split("\n## ", 1)[0]
    assert f"### `{FW}` (사내 프레임워크) — 라이브러리 2개" in section
    assert f"### `{CORE}` — 라이브러리 1개" in section                   # other groups unchanged
    assert "사내 프레임워크의 버전은 바꾸지 않습니다." in section
    assert "해당 아티팩트는 예외처리가 가능합니다." in section
    data = report.build_report_json(state, paths, inhouse=keys)
    groups = {a["ga"]: a.get("group") for a in data["inhouse"]["artifacts"]}
    assert groups == {FW: "framework", CORE: None}


def test_report_without_the_table_is_unchanged(tmp_path):
    state, paths = _state(tmp_path)
    plain = report.build_report_md(state, paths)
    assert "사내 프레임워크" not in plain
    assert plain == report.build_report_md(state, paths, inhouse=inhouse_keys(_cfg(patterns=())))
    assert "group" not in str(report.build_report_json(state, paths)["inhouse"])


def test_report_particles_and_glossary(tmp_path):
    from tests.remediations.test_report import BANNED_WORDS, _detached
    state, paths = _state(tmp_path)
    md = report.build_report_md(state, paths, inhouse=inhouse_keys(_cfg()))
    assert _detached(md) == []
    for word in BANNED_WORDS:
        assert word not in md, word
