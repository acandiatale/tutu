"""v2.1n: an optional in-house artifact is INTRODUCED when one of its triggers (`ArtifactSpec.when_present`, g:a keys or
globs) is declared in a project pom's `<dependencies>`; `replaces: true` (default) removes the matched declarations,
`false` keeps them. Runs inside `_all_poms_pass` (every mode, right before the optional bumps). Offline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbmig.core.config import ArtifactSpec, Config
from sbmig.core.interfaces import RestructureResult
from sbmig.restructure import PomEditor, Restructurer
from sbmig.review.inputs import restructure_lines

from .helpers import HEAD, CaseType, dep, java, make_ws, profile
from .test_company_layout import _boot_multi_files, _boot_multi_profile, _roles
from .test_restructurer import msa_files, msa_profile

LANG3 = "org.apache.commons:commons-lang3"
TEXT = "org.apache.commons:commons-text"
JPA = "com.company.lib:lib-jpa"
HIB = "org.hibernate.orm:hibernate-core"
HIB_OLD = "org.hibernate:hibernate-core"
ENVERS = "org.hibernate.orm:hibernate-envers"


def _lang3(**kw) -> ArtifactSpec:
    return ArtifactSpec(group_id="org.apache.commons", artifact_id="commons-lang3", version="3.17.0",
                        when_present=[TEXT], replaces=False, **kw)


def _jpa(**kw) -> ArtifactSpec:
    base = {"group_id": "com.company.lib", "artifact_id": "lib-jpa", "version": "4.0.0",
            "when_present": ["org.hibernate*:hibernate-core"]}
    base.update(kw)
    return ArtifactSpec(**base)


def _single(deps: str, *, props: str = "", managed: str = "", parent: str = "") -> dict[str, str]:
    pom = (HEAD + parent + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           + (f"    <properties>\n{props}    </properties>\n" if props else "")
           + (f"    <dependencyManagement>\n        <dependencies>\n{managed}        </dependencies>\n"
              "    </dependencyManagement>\n" if managed else "")
           + (f"    <dependencies>\n{deps}    </dependencies>\n" if deps else "") + "</project>\n")
    return {"pom.xml": pom}


def _bump(tmp_path: Path, cfg: Config, files: dict[str, str], name: str = "trig") -> tuple:
    """mode `optional_bump` = `_all_poms_pass` alone (no moves, no parents): the trigger rule in isolation."""
    ws = make_ws(tmp_path, files, name)
    res = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="shop", version="1.0.0"), cfg,
                               mode="optional_bump")
    assert res.ok, res.error
    return ws, res


def _deps(ws, pom: str = "pom.xml") -> dict[str, tuple]:
    return {g.key: (g.version, g.scope, g.type) for g in PomEditor(ws.root / pom).dependencies()}


def _events(ws, name: str) -> list[dict]:
    if not ws.journal_path.is_file():
        return []
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text(encoding="utf-8").splitlines() if x.strip())
            if e.get("event") == name]


# ------------------------------------------------------------------------------------------------ the rule
def test_a_trigger_adds_the_optional_next_to_it(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_lang3()]
    ws, res = _bump(tmp_path, cfg, _single(dep("org.apache.commons", "commons-text", "1.10.0")))
    d = _deps(ws)
    assert d[LANG3] == ("3.17.0", None, None) and d[TEXT] == ("1.10.0", None, None)     # replaces: false keeps it
    assert res.optional_added == [f"{LANG3} → pom.xml (for {TEXT})"] and res.optional_replaced == []
    assert res.optional_bumped == []                     # added at the configured version: nothing left to bump
    assert any(n.startswith("optional artifacts added for trigger dependencies: ") for n in res.notes)
    ev = _events(ws, "optional_triggers")
    assert ev and ev[0]["added"] == res.optional_added and ev[0]["replaced"] == []
    # second run: already declared → nothing
    before = (ws.root / "pom.xml").read_bytes()
    res2 = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="shop", version="1.0.0"), cfg,
                                mode="optional_bump")
    assert res2.ok and res2.optional_added == [] and res2.changed_files == []
    assert (ws.root / "pom.xml").read_bytes() == before and len(_events(ws, "optional_triggers")) == 1


def test_replaces_removes_the_trigger_its_exclusions_and_its_property(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa(version=None)]
    excl = ("            <exclusions>\n                <exclusion>\n"
            "                    <groupId>org.jboss.logging</groupId>\n"
            "                    <artifactId>jboss-logging</artifactId>\n"
            "                </exclusion>\n            </exclusions>\n")
    files = _single(dep("org.hibernate.orm", "hibernate-core", "${hibernate.version}", extra=excl)
                    + dep("com.h2database", "h2", "2.2.224", scope="runtime"),
                    props="        <hibernate.version>6.4.4.Final</hibernate.version>\n"
                          "        <keep.me>x</keep.me>\n")
    ws, res = _bump(tmp_path, cfg, files)
    d = _deps(ws)
    assert HIB not in d and d[JPA] == (None, None, None)          # version: null → no <version>, the parent manages it
    text = (ws.root / "pom.xml").read_text(encoding="utf-8")
    assert "jboss-logging" not in text and "hibernate.version" not in text and "<keep.me>x</keep.me>" in text
    assert res.optional_added == [f"{JPA} → pom.xml (for {HIB})"]
    assert res.optional_replaced == [f"{HIB} removed from pom.xml → {JPA}"]
    assert "property hibernate.version removed from pom.xml (only the replaced trigger used it)" in res.notes
    assert _events(ws, "optional_triggers")[0]["replaced"] == res.optional_replaced
    # idempotent: the trigger is gone, the optional declared
    res2 = Restructurer().apply(ws, profile(ws, CaseType.PURE_JAVA, artifact_id="shop", version="1.0.0"), cfg,
                                mode="optional_bump")
    assert res2.optional_added == [] and res2.optional_replaced == [] and res2.changed_files == []


def test_a_property_another_dependency_uses_is_kept(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa()]
    files = _single(dep("org.hibernate.orm", "hibernate-core", "${hibernate.version}")
                    + dep("org.hibernate.orm", "hibernate-envers", "${hibernate.version}"),
                    props="        <hibernate.version>6.4.4.Final</hibernate.version>\n")
    ws, res = _bump(tmp_path, cfg, files)
    d = _deps(ws)
    assert HIB not in d and d[JPA] == ("4.0.0", None, None) and d[ENVERS] == ("${hibernate.version}", None, None)
    assert not any(n.startswith("property hibernate.version removed") for n in res.notes)
    assert PomEditor(ws.root / "pom.xml").get_property("hibernate.version") == "6.4.4.Final"


@pytest.mark.parametrize(("trigger_scope", "spec_scope", "want"),
                         [("test", None, "test"), ("provided", None, "provided"), (None, None, None),
                          ("system", None, None), ("test", "runtime", "runtime")])
def test_scope_comes_from_the_spec_else_the_trigger(tmp_path: Path, cfg: Config, trigger_scope, spec_scope,
                                                    want) -> None:
    cfg.artifacts.optional = [_jpa(scope=spec_scope, replaces=False)]
    extra = "            <systemPath>${project.basedir}/lib/h.jar</systemPath>\n" if trigger_scope == "system" else ""
    ws, _res = _bump(tmp_path, cfg, _single(dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final",
                                                 scope=trigger_scope, extra=extra)))
    assert _deps(ws)[JPA] == ("4.0.0", want, None)


def test_type_comes_from_the_spec(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa(type="pom")]
    ws, _res = _bump(tmp_path, cfg, _single(dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final")))
    assert _deps(ws)[JPA] == ("4.0.0", None, "pom")


def test_globs_match_group_and_artifact(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa()]
    ws, res = _bump(tmp_path, cfg, _single(dep("org.hibernate", "hibernate-core", "5.6.15.Final")
                                           + dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final")
                                           + dep("org.hibernate.orm", "hibernate-envers", "6.4.4.Final")))
    d = _deps(ws)
    assert HIB_OLD not in d and HIB not in d and ENVERS in d and JPA in d       # both cores matched, envers not
    assert res.optional_added == [f"{JPA} → pom.xml (for {HIB_OLD}, {HIB})"]
    assert len(res.optional_replaced) == 2


def test_spec_matching() -> None:
    spec = _jpa(aliases=["com.company.legacy:legacy-jpa"],
                when_present=["org.hibernate*:hibernate-core", "com.company.*:*"])
    assert spec.triggered_by(HIB) and spec.triggered_by(HIB_OLD) and not spec.triggered_by(ENVERS)
    assert not spec.triggered_by(JPA)                                       # its own key, though `com.company.*:*`
    assert not spec.triggered_by("com.company.legacy:legacy-jpa")          # an alias: renamed, never a trigger
    assert spec.triggered_by("com.company.other:thing")
    assert ArtifactSpec(group_id="g", artifact_id="a", when_present="x:y, z:w").when_present == ["x:y", "z:w"]
    assert ArtifactSpec(group_id="g", artifact_id="a").replaces is True


def test_dependency_management_only_is_not_a_trigger(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa()]
    managed = dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final").replace("        <", "            <")
    ws, res = _bump(tmp_path, cfg, _single(dep("com.h2database", "h2", "2.2.224"), managed=managed))
    assert JPA not in _deps(ws) and res.optional_added == []
    assert _events(ws, "optional_triggers") == []


def test_an_alias_declaration_is_renamed_not_triggered(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa(aliases=["com.company.legacy:legacy-jpa"],
                                   when_present=["com.company.legacy:*", "org.hibernate*:hibernate-core"])]
    ws, res = _bump(tmp_path, cfg, _single(dep("com.company.legacy", "legacy-jpa", "1.0.0")
                                           + dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final")))
    d = _deps(ws)
    # the alias counts as "already declared": no second lib-jpa, the trigger stays; the bump renames the alias
    assert res.optional_added == [] and HIB in d
    assert d[JPA] == ("4.0.0", None, None) and "com.company.legacy:legacy-jpa" not in d
    assert "com.company.legacy:legacy-jpa renamed to com.company.lib:lib-jpa in pom.xml" in res.notes


def test_declared_in_an_in_repo_ancestor_counts(tmp_path: Path, cfg: Config) -> None:
    cfg.artifacts.optional = [_jpa()]
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>core</module>\n    </modules>\n"
            "    <dependencies>\n" + dep("com.company.lib", "lib-jpa", "4.0.0") + "    </dependencies>\n</project>\n")
    core = (HEAD + "    <parent>\n        <groupId>com.acme</groupId>\n        <artifactId>shop</artifactId>\n"
            "        <version>1.0.0</version>\n    </parent>\n    <artifactId>core</artifactId>\n"
            "    <dependencies>\n" + dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final")
            + "    </dependencies>\n</project>\n")
    ws, res = _bump(tmp_path, cfg, {"pom.xml": root, "core/pom.xml": core})
    assert res.optional_added == [] and HIB in _deps(ws, "core/pom.xml") and JPA not in _deps(ws, "core/pom.xml")


def test_the_review_sees_both_lists() -> None:
    res = RestructureResult(ok=True, optional_bumped=["g:a 1→2"], optional_added=[f"{JPA} → pom.xml (for {HIB})"],
                            optional_replaced=[f"{HIB} removed from pom.xml → {JPA}"])
    text = "\n".join(restructure_lines([], [res]))
    assert "  - optional bumped:" in text
    assert "  - optional artifacts added for trigger dependencies:" in text and f"{JPA} → pom.xml" in text
    assert "  - trigger dependencies replaced by optional artifacts:" in text
    assert text.index("optional bumped") < text.index("optional artifacts added")


# ------------------------------------------------------------------------------------------ the modes
@pytest.fixture()
def ccfg(cfg: Config) -> Config:
    """The round-9c company layout with role parents (as `test_v21l_pin_versions.ccfg`)."""
    L = cfg.layout
    L.module_dir, L.boot_module = "", "{name}-boot"
    L.comp_module, L.front_module = "{name}-back", "{name}-front"
    L.boot_as_is = "in-place"
    L.comp_artifact_suffix = "-back"
    return _roles(cfg)


def test_layout_adds_the_optional_in_the_back_module(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.optional = [_lang3(), _jpa()]
    pom = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shop</artifactId>\n    <version>1.0.0</version>\n"
           "    <dependencies>\n" + dep("org.springframework", "spring-context", "6.1.14")
           + dep("org.apache.commons", "commons-text", "1.10.0")
           + dep("org.hibernate.orm", "hibernate-core", "6.4.4.Final") + "    </dependencies>\n</project>\n")
    ws = make_ws(tmp_path, {"pom.xml": pom, "src/main/java/com/acme/shop/Main.java": java("com.acme.shop", "Main")},
                 "layout-trig")
    prof = profile(ws, CaseType.SPRING_LEGACY, artifact_id="shop", version="1.0.0", base_package="com.acme.shop")
    res = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res.ok, res.error
    back = "shop-back/pom.xml"
    d = _deps(ws, back)
    assert d[LANG3] == ("3.17.0", None, None) and d[TEXT] == ("1.10.0", None, None)
    assert d[JPA] == ("4.0.0", None, None) and HIB not in d
    assert res.optional_added == [f"{LANG3} → {back} (for {TEXT})", f"{JPA} → {back} (for {HIB})"]
    assert res.optional_replaced == [f"{HIB} removed from {back} → {JPA}"]
    for gen in ("pom.xml", "shop-boot/pom.xml"):                # no trigger there → nothing added
        assert LANG3 not in _deps(ws, gen) and JPA not in _deps(ws, gen)
    before = (ws.root / back).read_bytes()
    res2 = Restructurer().apply(ws, prof, ccfg, mode="layout")
    assert res2.ok and res2.optional_added == [] and (ws.root / back).read_bytes() == before


def test_boot_pom_adds_the_optional_where_the_trigger_is(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.optional = [_jpa(version=None)]
    files = _boot_multi_files()
    end = "    </dependencies>"
    files["shop-core/pom.xml"] = files["shop-core/pom.xml"].replace(
        end, dep("org.hibernate.orm", "hibernate-core", scope="provided") + end)
    ws = make_ws(tmp_path, files, "boot-trig")
    res = Restructurer().apply(ws, _boot_multi_profile(ws), ccfg, mode="boot_pom")
    assert res.ok, res.error
    core = _deps(ws, "shop-core/pom.xml")
    assert core[JPA] == (None, "provided", None) and HIB not in core
    assert JPA not in _deps(ws, "shop-api/pom.xml") and JPA not in _deps(ws, "shop-boot/pom.xml")
    assert res.optional_added == [f"{JPA} → shop-core/pom.xml (for {HIB})"]


def test_msa_pom_services_and_library_roots(tmp_path: Path, ccfg: Config) -> None:
    ccfg.artifacts.optional = [_lang3()]
    files = msa_files()
    end = "    </dependencies>"
    files["user-service/pom.xml"] = files["user-service/pom.xml"].replace(
        end, dep("org.apache.commons", "commons-text", "1.10.0") + end)
    files["pom.xml"] = files["pom.xml"].replace("        <module>user-service</module>\n",
                                                "        <module>user-service</module>\n"
                                                "        <module>shared-lib</module>\n")
    files["shared-lib/pom.xml"] = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>shared-lib</artifactId>\n"
                                   "    <version>1.0</version>\n    <dependencies>\n"
                                   + dep("org.apache.commons", "commons-text", "1.10.0", scope="test")
                                   + "    </dependencies>\n</project>\n")
    ws = make_ws(tmp_path, files, "msa-trig")
    res = Restructurer().apply(ws, msa_profile(ws), ccfg, mode="msa_pom", params={"library_roots": ["shared-lib"]})
    assert res.ok, res.error
    assert _deps(ws, "user-service/pom.xml")[LANG3] == ("3.17.0", None, None)
    assert _deps(ws, "shared-lib/pom.xml")[LANG3] == ("3.17.0", "test", None)      # library root: triggers apply too
    assert LANG3 not in _deps(ws, "order-service/pom.xml")
    assert sorted(res.optional_added) == [f"{LANG3} → shared-lib/pom.xml (for {TEXT})",
                                          f"{LANG3} → user-service/pom.xml (for {TEXT})"]


# ------------------------------------------------------------------------------------------------ Handover
def test_the_handover_tags_the_added_optional_and_the_removed_trigger(tmp_path: Path, cfg: Config) -> None:
    from datetime import datetime, timezone

    from sbmig.analyze.analyzer import PathView
    from sbmig.core.models import GAV, Phase, PhaseResult, PomInfo, ProjectProfile, RunReport
    from sbmig.report import build_handover
    from sbmig.report.handover import REASON_OPTIONAL_TRIGGER

    final = tmp_path / "final"
    final.mkdir()
    (final / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        "<groupId>com.acme</groupId><artifactId>shop</artifactId><version>1.0.0</version><dependencies>"
        "<dependency><groupId>com.company.lib</groupId><artifactId>lib-jpa</artifactId><version>4.0.0</version>"
        "</dependency><dependency><groupId>org.apache.commons</groupId><artifactId>commons-text</artifactId>"
        "<version>1.10.0</version></dependency><dependency><groupId>org.apache.commons</groupId>"
        "<artifactId>commons-lang3</artifactId><version>3.17.0</version></dependency></dependencies></project>\n")
    pom = PomInfo(path="pom.xml", group_id="com.acme", artifact_id="shop", version="1.0.0", is_root=True,
                  dependencies=[GAV.parse(f"{HIB}:6.4.4.Final"), GAV.parse(f"{TEXT}:1.10.0")])
    prof = ProjectProfile(root="/as-is", name="shop", case_type=CaseType.SPRING_LEGACY, poms=[pom], root_pom=pom)
    note = (f"optional artifacts added for trigger dependencies: {JPA} → pom.xml (for {HIB}); "
            f"{LANG3} → pom.xml (for {TEXT}) — replaced: {HIB} removed from pom.xml → {JPA}")
    t0 = datetime(2026, 10, 30, tzinfo=timezone.utc)
    report = RunReport(run_id="r", project_name="shop", case_type=CaseType.SPRING_LEGACY, started_at=t0,
                       finished_at=t0, success=True, phases=[PhaseResult(phase=Phase.RESTRUCTURE, notes=[note])])
    h = build_handover(prof, PathView(final), report, effective={}, cfg=cfg)
    assert REASON_OPTIONAL_TRIGGER.format(matched=HIB) == f"optional (trigger: {HIB})"
    assert f"{JPA}:4.0.0 (optional (trigger: {HIB}))" in h.added
    assert f"{LANG3}:3.17.0 (optional (trigger: {TEXT}))" in h.added
    assert f"{HIB}:6.4.4.Final (optional (trigger: {HIB}))" in h.removed
    assert not any(x.startswith(TEXT) for x in h.added + h.removed)     # replaces: false — the trigger stays


def test_the_trigger_reason_is_translated() -> None:
    from sbmig.report.handover import tr_reason

    assert tr_reason("ko", f"optional (trigger: {HIB})") == f"선택 아티팩트 도입 (트리거: {HIB})"
    assert tr_reason("en", f"optional (trigger: {HIB})") == f"optional (trigger: {HIB})"
    assert tr_reason("ko", "optional bump") == "선택 아티팩트 버전 상향"
