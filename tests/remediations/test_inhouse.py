"""inhouse.py + the in-house rule end to end (inventory → planner) on a synthetic company layout."""
from __future__ import annotations

import logging
import types
from pathlib import Path

from sbmig.core.config import ArtifactsConfig, RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import planner as P
from sbmig.remediations.inhouse import inhouse_keys, inhouse_on_path
from sbmig.remediations.inventory import build_inventory
from sbmig.remediations.models import (
    K_PARENT_VERSION,
    O_PARENT_MANAGED,
    O_TRANSITIVE,
    Finding,
    Inventory,
    ModuleResolution,
    ResolvedDep,
    ResolvedModel,
)

G = "com.example.inhouse"
CORE, WEB = f"{G}:inhouse-core", f"{G}:inhouse-web"
BASE, BOOT, COMP = f"{G}:base-parent", f"{G}:boot-jar", f"{G}:comp-jar"


def _spec(ga: str, aliases=()):
    g, a = ga.split(":")
    return {"group_id": g, "artifact_id": a, "version": "4.0.0", "aliases": list(aliases)}


def _cfg(groups=("mandatory", "optional", "parents"), *, pins=None, artifacts=True):
    c = types.SimpleNamespace(remediation=RemediationConfig(inhouse_groups=list(groups),
                                                            policy=RemediationPolicyConfig(pins=pins or {})))
    if artifacts:
        c.artifacts = ArtifactsConfig(parent=_spec(BASE), boot_parent=_spec(BOOT), comp_parent=_spec(COMP),
                                      mandatory=[_spec(CORE, aliases=["com.example.old:core"])],
                                      optional=[_spec(WEB)])
    return c


# ---------------------------------------------------------------------------- inhouse_keys
def test_keys_per_group_with_aliases():
    keys = inhouse_keys(_cfg(("mandatory",)))
    assert keys == {CORE: "mandatory", "com.example.old:core": "mandatory"}
    assert inhouse_keys(_cfg(("optional",))) == {WEB: "optional"}
    assert inhouse_keys(_cfg(("parents",))) == {BASE: "parents", BOOT: "parents", COMP: "parents"}
    assert set(inhouse_keys(_cfg())) == {CORE, "com.example.old:core", WEB, BASE, BOOT, COMP}


def test_parents_without_role_parents():
    c = _cfg(("parents",))
    c.artifacts.boot_parent = c.artifacts.comp_parent = None
    assert inhouse_keys(c) == {BASE: "parents"}


def test_first_group_wins_for_a_key_listed_twice():
    c = _cfg(("optional", "mandatory"))
    c.artifacts.optional.append(c.artifacts.mandatory[0])
    assert inhouse_keys(c)[CORE] == "optional"


def test_unknown_group_ignored_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="sbmig.remediations.inhouse"):
        assert inhouse_keys(_cfg(("mandatory", "bogus", "nope"))) == {CORE: "mandatory", "com.example.old:core": "mandatory"}
    assert len([r for r in caplog.records if "inhouse_groups" in r.getMessage()]) == 1


def test_empty_groups_or_missing_config_is_empty():
    assert inhouse_keys(_cfg(())) == {}
    assert inhouse_keys(_cfg(artifacts=False)) == {}
    assert inhouse_keys(types.SimpleNamespace()) == {}
    assert inhouse_keys(None) == {}
    odd = _cfg()
    odd.artifacts = "not a config"
    assert inhouse_keys(odd) == {}


# ---------------------------------------------------------------------------- inhouse_on_path
def test_on_path():
    keys = {CORE: "mandatory", WEB: "optional"}
    assert inhouse_on_path(CORE, [], keys) == [CORE]                                  # direct, itself in-house
    assert inhouse_on_path("org.example:lib", ["org.example:a:1", f"{CORE}:4.0.0", "org.example:b:2"], keys) == [CORE]
    assert inhouse_on_path("org.example:lib", [f"{WEB}:4.0.0", f"{CORE}:4.0.0", f"{WEB}:4.0.0"], keys) == [WEB, CORE]
    assert inhouse_on_path("org.example:lib", ["org.example:a:1"], keys) == []
    assert inhouse_on_path("org.example:lib", [f"{CORE}:jar:4.0.0", f"{WEB}:jar:tests:4.0.0:test"], keys) == [CORE, WEB]
    assert inhouse_on_path(CORE, [f"{CORE}:4.0.0"], keys) == [CORE]
    assert inhouse_on_path(CORE, None, {}) == [] and inhouse_on_path(CORE, ["x"], None) == []
    assert inhouse_on_path("org.example:lib", ["bad", ""], keys) == []


# ---------------------------------------------------------------------------- company layout (parents)
def _root() -> str:
    return ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>org.example</groupId>\n"
            "  <artifactId>app</artifactId>\n  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
            "  <modules>\n    <module>app-boot</module>\n    <module>app-back</module>\n  </modules>\n</project>\n")


def _module(name: str, parent: str, deps: str = "") -> str:
    g, a = parent.rsplit(":", 1)[0].split(":") if parent.count(":") == 2 else parent.split(":")
    v = parent.split(":")[2] if parent.count(":") == 2 else "4.0.0"
    return (f"<project>\n  <modelVersion>4.0.0</modelVersion>\n  <parent>\n    <groupId>{g}</groupId>\n"
            f"    <artifactId>{a}</artifactId>\n    <version>{v}</version>\n    <relativePath/>\n  </parent>\n"
            f"  <groupId>org.example</groupId>\n  <artifactId>{name}</artifactId>\n"
            f"  <dependencies>\n{deps}  </dependencies>\n</project>\n")


def _dep(ga: str, v: str | None = None) -> str:
    g, a = ga.split(":")
    ver = f"      <version>{v}</version>\n" if v else ""
    return f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n{ver}    </dependency>\n"


def _project(tmp: Path, *, boot_parent=BOOT, back_parent=COMP, back_deps="", boot_deps="") -> Path:
    files = {"pom.xml": _root(), "app-boot/pom.xml": _module("app-boot", boot_parent, boot_deps),
             "app-back/pom.xml": _module("app-back", back_parent, back_deps)}
    for rel, text in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_text(text, encoding="utf-8")
    return tmp


def _rd(ga: str, v: str, *, direct=True, path=()) -> ResolvedDep:
    g, a = ga.split(":")
    return ResolvedDep(group_id=g, artifact_id=a, version=v, direct=direct, path=list(path))


def _model(mods: dict[str, list[ResolvedDep]]) -> ResolvedModel:
    m = ResolvedModel(complete=True)
    for d, deps in mods.items():
        m.modules.append(ModuleResolution(dir=d, group_id="org.example", artifact_id=d, version="1.0.0",
                                          dependencies=deps))
    return m


def _f(ga: str, v: str, fid="CVE-1") -> Finding:
    return Finding(id=fid, kind="library", identifier=ga, version=v, patched_version="9.9.9")


class _Src:
    def versions(self, g, a):
        return ["9.9.9"]

    def evidence(self, g, a, v):
        return "repository:nexus.invalid"


def _run(project: Path, findings, resolved, cfg) -> tuple[Inventory, object]:
    inv = build_inventory(project, findings, resolved, inhouse=inhouse_keys(cfg))
    plan = P.plan_round(project, inv, cfg, _Src(), round=1, source_tree_hash="t" * 64)
    return inv, plan


def test_version_managed_by_the_company_parent_is_the_architecture_teams(tmp_path):
    p = _project(tmp_path, back_deps=_dep("org.example:managed"))
    resolved = _model({"app-back": [_rd("org.example:managed", "1.0")]})
    inv, plan = _run(p, [_f("org.example:managed", "1.0")], resolved, _cfg())
    (it,) = inv.items
    assert it.origin == O_PARENT_MANAGED and it.manager == f"{COMP}:4.0.0"
    assert it.inhouse == [COMP] and it.inhouse_modules == ["app-back"]
    assert not plan.proposals
    (r,) = plan.residuals
    assert r.reason == "inhouse_artifact" and r.inhouse == [COMP] and r.modules == ["app-back"]
    # with a server pin for the parent: the parent-version proposal (the architecture team's published fix)
    _inv, plan = _run(p, [_f("org.example:managed", "1.0")], resolved, _cfg(pins={COMP: "4.0.1"}))
    (prop,) = plan.proposals
    assert prop.kind == K_PARENT_VERSION and prop.ga == COMP and prop.to_version == "4.0.1" and prop.requires_approval
    assert not plan.residuals


def test_dependency_inherited_from_the_parent_and_its_transitive(tmp_path):
    p = _project(tmp_path)                                  # app-back declares nothing: both come with comp-jar
    resolved = _model({"app-back": [_rd("org.example:inherited", "1.0"),
                                    _rd("org.example:lib", "1.2.3", direct=False, path=["org.example:inherited:1.0"])]})
    inv, plan = _run(p, [_f("org.example:inherited", "1.0", "CVE-1"), _f("org.example:lib", "1.2.3", "CVE-2")],
                     resolved, _cfg())
    by = {i.ga: i for i in inv.items}
    assert by["org.example:inherited"].origin == O_PARENT_MANAGED and by["org.example:inherited"].inhouse == [COMP]
    assert by["org.example:lib"].origin == O_TRANSITIVE and by["org.example:lib"].inhouse == [COMP]
    assert by["org.example:lib"].inhouse_modules == ["app-back"]
    assert not plan.proposals and {r.reason for r in plan.residuals} == {"inhouse_artifact"}


def test_transitive_under_a_dependency_the_project_declares_is_not_the_parents(tmp_path):
    p = _project(tmp_path, back_deps=_dep("org.example:third", "1"))
    resolved = _model({"app-back": [_rd("org.example:third", "1"),
                                    _rd("org.example:lib", "1.2.3", direct=False, path=["org.example:third:1"])]})
    inv, plan = _run(p, [_f("org.example:lib", "1.2.3")], resolved, _cfg())
    (it,) = inv.items
    assert it.origin == O_TRANSITIVE and it.inhouse == [] and it.inhouse_modules == []
    (r,) = plan.residuals
    assert r.reason == "manual_review" and "do not inherit from the root pom.xml" in r.detail and r.inhouse == []


def test_without_the_parents_group_nothing_is_attributed_to_a_parent(tmp_path):
    p = _project(tmp_path, back_deps=_dep("org.example:managed"))
    resolved = _model({"app-back": [_rd("org.example:managed", "1.0"),
                                    _rd("org.example:lib", "1.2.3", direct=False, path=["org.example:inherited:1.0"])]})
    inv, plan = _run(p, [_f("org.example:managed", "1.0"), _f("org.example:lib", "1.2.3", "CVE-2")], resolved,
                     _cfg(("mandatory", "optional")))
    assert all(i.inhouse == [] for i in inv.items)
    assert "inhouse_artifact" not in {r.reason for r in plan.residuals}


def test_a_public_parent_is_not_in_house(tmp_path):
    boot = "org.springframework.boot:spring-boot-starter-parent:4.1.0"
    p = _project(tmp_path, boot_parent=boot, back_parent=boot, back_deps=_dep("org.example:managed"))
    resolved = _model({"app-back": [_rd("org.example:managed", "1.0"),
                                    _rd("org.example:lib", "1.2.3", direct=False, path=["org.example:inherited:1.0"])]})
    inv, plan = _run(p, [_f("org.example:managed", "1.0"), _f("org.example:lib", "1.2.3", "CVE-2")], resolved, _cfg())
    assert all(i.inhouse == [] for i in inv.items)
    assert "inhouse_artifact" not in {r.reason for r in plan.residuals}


def test_path_and_parent_attribution_union(tmp_path):
    # app-boot gets lib through the mandatory artifact it inherited from boot-jar: both g:a's are named
    p = _project(tmp_path)
    resolved = _model({"app-boot": [_rd("org.example:lib", "1.2.3", direct=False, path=[f"{CORE}:4.0.0"])]})
    inv, _plan = _run(p, [_f("org.example:lib", "1.2.3")], resolved, _cfg())
    (it,) = inv.items
    assert it.inhouse == sorted([CORE, BOOT]) and it.inhouse_modules == ["app-boot"]


# ---------------------------------------------------------------------------- migration report line
def test_migration_report_names_the_inhouse_count():
    from sbmig.report.markdown import remediation_lines
    r = types.SimpleNamespace(remediation={"status": "INCOMPLETE", "reason": "inhouse_artifact",
                                           "counts": {"residual": 2, "residual_inhouse": 2}})
    en = "\n".join(remediation_lines(r))
    assert "In-house artifacts (exception possible, contact their owners): 2" in en
    assert "사내 아티팩트(예외 처리 가능, 담당자에게 연락): 2건" in "\n".join(remediation_lines(r, "ko"))
    r.remediation["counts"]["residual_inhouse"] = 0
    assert "In-house artifacts" not in "\n".join(remediation_lines(r))
