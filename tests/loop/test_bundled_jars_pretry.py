"""v2.1z-i bundled jars, fix-loop side (docs/INTERFACES.md "v2.1z-i", "Fix loop"): a `package <p> does not exist` /
`cannot find symbol … class X` case whose package a jar bundled in the tree provides is wired WITHOUT the LLM
(`sbmig-bundled`: `wire_bundled_jar` per (module pom, jar), committed `fix(sbmig-bundled): …`, journal
`bundled_jar_wired`, one Handover line), once per (pom, jar) per run; the prompt facts name the jar; a decline that
calls the library missing / without coordinates is rejected while a bundled jar provides the package.

`providers` / `wire_bundled_jar` / `bundled_jar_followup` are monkeypatched on `sbmig.loop.fixloop` (their bodies are
the analyze / restructure agents')."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import CaseType, ErrorCluster, ErrorKind, JarCoordinate, Phase
from sbmig.core.workspace import Workspace
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop import fixloop as fl
from sbmig.loop.fixloop import SBMIG_BUNDLED_SOURCE, SBMIG_JAKARTA_SOURCE
from sbmig.restructure.system_jars import WiredJar
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeFixer,
    FakeKB,
    bad_build,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
)

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
JAR = "app-back/src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar"
USE = "app-back/src/main/java/com/x/Use.java"
OTHER = "app-back/src/main/java/com/x/Other.java"
BACK = "app-back/pom.xml"
WIRED_MARK = "<!-- wired: acme-seed -->"


def _pom(artifact: str, modules: list[str] | None = None, group: str = "com.x") -> str:
    mods = "".join(f"<module>{m}</module>" for m in modules or [])
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>{group}</groupId>\n  <artifactId>{artifact}</artifactId>\n  <version>1.0.0</version>\n"
            + (f"  <packaging>pom</packaging>\n  <modules>{mods}</modules>\n" if mods else "") + "</project>\n")


def _ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src" / "demo"
    files = {"pom.xml": _pom("demo", ["app-back"]), BACK: _pom("app-back"),
             USE: "package com.x;\nimport com.acme.seed.Seed;\npublic class Use { String s = Seed.encrypt(\"a\"); }\n",
             OTHER: "package com.x;\nimport com.acme.seed.util.Hex;\npublic class Other { Hex h; }\n"}
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    jar = src / JAR
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def seed_jar(**kw) -> JarCoordinate:
    return JarCoordinate(path=JAR, provides=["com.acme.seed", "com.acme.seed.util"], packages=["com.acme.seed"], **kw)


def fake_providers(jars, package):
    return [j for j in jars if package in j.provides]


@pytest.fixture()
def wired(monkeypatch):
    """`providers` by exact package; `wire_bundled_jar` appends a marker to the module pom; the follow-up line."""
    calls: list[dict] = []

    def wire(ws, cfg, *, module_pom, jar, repository_has, boot_poms=None, project_group=""):
        calls.append({"pom": module_pom, "jar": jar.path, "repository_has": repository_has,
                      "boot_poms": list(boot_poms or []), "group": project_group})
        p = ws.abs(module_pom)
        text = p.read_text(encoding="utf-8")
        if WIRED_MARK not in text:
            p.write_text(text.replace("</project>", f"  {WIRED_MARK}\n</project>"), encoding="utf-8")
        return WiredJar(jar=jar, module_pom=module_pom, group_id=project_group or "com.x", artifact_id="acme-seed",
                        version="1.0", scope="compile" if repository_has else "system",
                        system_path="" if repository_has else "${project.basedir}/lib/acme-seed-1.0.jar",
                        placeholder=not repository_has)

    monkeypatch.setattr(fl, "providers", fake_providers)
    monkeypatch.setattr(fl, "wire_bundled_jar", wire)
    monkeypatch.setattr(fl, "bundled_jar_followup",
                        lambda w: f"bundled jar `acme-seed-1.0.jar` wired into `{w.module_pom}` as a {w.scope} dependency")
    return calls


def missing(file: str = USE, pkg: str = "com.acme.seed"):
    return compile_error(file=file, msg=f"package {pkg} does not exist", l2="compile|package_missing")


def symbol(file: str = OTHER, cls: str = "Hex"):
    e = compile_error(file=file, msg="cannot find symbol", l2="compile|package_missing")
    e.raw = f"{file}:[3,8] cannot find symbol\n  symbol:   class {cls}\n  location: class com.x.Other"
    return e


def wired_build(*errors):
    def fn(ws, call):
        return ok_build() if WIRED_MARK in ws.read(BACK) else bad_build(*(errors or (missing(),)))
    return fn


def make_loop(cfg, ws, build, jars=None, fixer=None) -> BuildFixLoop:
    profile = make_profile(case=CaseType.SPRING_LEGACY)
    profile.bundled_jars = list(jars if jars is not None else [seed_jar()])
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer or FakeFixer(), FakeKB(), profile,
                        budget=LoopBudget.from_config(cfg), edit_applier=FakeEditApplier())


def cluster(*errors) -> ErrorCluster:
    return ErrorCluster(id="c1", kind=ErrorKind.COMPILE, errors=list(errors), case_id="case1")


def subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()


# ---------------------------------------------------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------------------------------------------------
def test_targets_none_without_a_provider(cfg, tmp_path, wired):
    loop = make_loop(cfg, _ws(tmp_path), wired_build())
    assert loop._bundled_targets(cluster(missing(pkg="org.other.lib"))) is None
    assert loop._bundled_targets(cluster(missing(pkg="javax.servlet"))) is None     # the jakarta pre-try's business


def test_targets_none_without_bundled_jars_or_with_the_config_off(cfg, tmp_path, wired):
    ws = _ws(tmp_path)
    assert make_loop(cfg, ws, wired_build(), jars=[])._bundled_targets(cluster(missing())) is None
    cfg.intake.bundled_jars = False
    assert make_loop(cfg, ws, wired_build())._bundled_targets(cluster(missing())) is None


def test_targets_are_the_owning_module_poms(cfg, tmp_path, wired):
    loop = make_loop(cfg, _ws(tmp_path), wired_build())
    assert loop._bundled_targets(cluster(missing())) == [BACK]
    # cannot find symbol: the package comes from the error file's import
    assert loop._bundled_missing_packages(cluster(symbol())) == {"com.acme.seed.util": [OTHER]}
    assert loop._bundled_targets(cluster(symbol())) == [BACK]
    # a file outside every module: the root pom
    assert loop._bundled_targets(cluster(missing(file="tools/X.java"))) == ["pom.xml"]
    # a container API copy (resolve_jar: scope provided) is never wired
    loop.profile.bundled_jars = [seed_jar(scope="provided")]
    assert loop._bundled_targets(cluster(missing())) is None and loop._package_facts(cluster(missing())) == []


def test_the_bundled_try_comes_before_the_jakarta_one(cfg, tmp_path):
    sources = [p.source for p in make_loop(cfg, _ws(tmp_path), wired_build())._pre_tries()]
    assert sources.index(SBMIG_BUNDLED_SOURCE) == sources.index(SBMIG_JAKARTA_SOURCE) - 1
    assert SBMIG_BUNDLED_SOURCE in fl.PRE_TRY_SOURCES


# ---------------------------------------------------------------------------------------------------------------------
# the attempt
# ---------------------------------------------------------------------------------------------------------------------
def test_a_bundled_jar_is_wired_without_the_llm(cfg, tmp_path, wired):
    ws = _ws(tmp_path)
    fixer = FakeFixer()
    loop = make_loop(cfg, ws, wired_build(), fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and fixer.contexts == [] and loop.budget.llm_calls == 0
    [att] = pr.attempts
    assert (att.source, att.outcome, att.llm_usage.calls) == (SBMIG_BUNDLED_SOURCE, "build_ok", 0)
    assert subjects(ws)[0] == f"fix(sbmig-bundled): {att.cluster_id}"
    assert any(n.startswith(f"bundled jar {JAR} provides package com.acme.seed → wired into {BACK} as a system")
               for n in att.notes)
    # the offline repository has nothing → system scope; the root groupId for the placeholder
    assert wired == [{"pom": BACK, "jar": JAR, "repository_has": False, "boot_poms": [], "group": "com.x"}]
    [ev] = [e for e in journal_events(ws) if e["event"] == "bundled_jar_wired"]
    want = {"case": att.script_case_id, "jar": JAR, "pom": BACK, "coordinates": "com.x:acme-seed:1.0",
            "scope": "system", "placeholder": True}
    assert {k: ev.get(k) for k in want} == want
    assert f"bundled jar `acme-seed-1.0.jar` wired into `{BACK}` as a system dependency" in loop.followups


def test_the_same_pom_and_jar_are_wired_once_per_run(cfg, tmp_path, wired):
    ws = _ws(tmp_path)

    def always(ws, call):
        return bad_build(missing())
    fixer = FakeFixer()
    pr = make_loop(cfg, ws, always, fixer=fixer).run(Phase.VERIFY, "package", 4)
    assert not pr.ok
    tries = [a for a in pr.attempts if a.source == SBMIG_BUNDLED_SOURCE]
    assert len(tries) == 1 and len(wired) == 1
    assert all(a.source != SBMIG_BUNDLED_SOURCE for a in pr.attempts[1:])      # the ladder went on


def test_a_jar_the_repository_has_is_a_compile_dependency(cfg, tmp_path, wired, monkeypatch):
    jar = seed_jar(group_id="com.acme", artifact_id="acme-seed", version="1.0", source="pom_properties")
    loop = make_loop(cfg, _ws(tmp_path), wired_build(), jars=[jar])

    class Repo:
        def pom_text(self, g, a, v):
            return "<project/>" if (g, a, v) == ("com.acme", "acme-seed", "1.0") else None

        def versions(self, g, a):
            return []
    monkeypatch.setattr(loop, "_repository", lambda: Repo())
    assert loop._bundled_in_repository(jar) is True
    assert loop._bundled_in_repository(seed_jar(group_id="com.acme", artifact_id="acme-seed", version="1.0",
                                                source="manifest")) is False      # untrusted source: never looked up
    monkeypatch.setattr(loop, "_repository", lambda: None)                          # no / unreadable repository
    assert loop._bundled_in_repository(jar) is False


def test_unknown_jar_skip_wires_nothing_and_says_so(cfg, tmp_path, wired):
    cfg.intake.unknown_jar = "skip"
    ws = _ws(tmp_path)
    fixer = FakeFixer()
    loop = make_loop(cfg, ws, lambda ws, call: bad_build(missing()), fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 2)
    assert not pr.ok and wired == [] and not any(a.source == SBMIG_BUNDLED_SOURCE for a in pr.attempts)
    assert any("intake.unknown_jar is skip" in f for f in loop.followups)


# ---------------------------------------------------------------------------------------------------------------------
# prompt facts and declines
# ---------------------------------------------------------------------------------------------------------------------
def test_package_facts_name_the_bundled_jar(cfg, tmp_path, wired):
    loop = make_loop(cfg, _ws(tmp_path), wired_build())
    facts = loop._package_facts(cluster(missing()))
    assert facts[0] == (f"the project bundles `{JAR}` which provides package `com.acme.seed` (coordinates: unknown — "
                        "no pom.properties, not in the jar name table) — wire it as a dependency (system scope at "
                        "`${project.basedir}/lib/acme-seed-1.0.jar` when the repository lacks it) instead of calling "
                        "the library missing")
    known = seed_jar(group_id="com.acme", artifact_id="acme-seed", version="1.0", source="pom_properties")
    loop.profile.bundled_jars = [known]
    assert "(coordinates: com.acme:acme-seed:1.0)" in loop._package_facts(cluster(missing()))[0]
    assert loop._package_facts(cluster(missing(pkg="org.other.lib"))) == []


def test_a_coordinates_decline_is_rejected_when_a_jar_provides_the_package(cfg, tmp_path, wired):
    loop = make_loop(cfg, _ws(tmp_path), wired_build())
    fact = loop._decline_contradiction(cluster(missing()), "no coordinates for com.acme.seed: the library is unknown")
    assert fact == (f"`{JAR}` in the project provides package `com.acme.seed` — the library is not missing, it is "
                    "bundled; wire it")
    assert loop._decline_contradiction(cluster(missing()), "좌표를 알 수 없습니다").startswith(f"`{JAR}`")
    assert loop._decline_contradiction(cluster(missing()), "the owners must decide") == ""      # not about a library
    assert loop._decline_contradiction(cluster(missing(pkg="org.other.lib")), "no coordinates") == ""
    loop.profile.bundled_jars = []
    assert loop._decline_contradiction(cluster(missing()), "no coordinates") == ""


# ---------------------------------------------------------------------------------------------------------------------
# boot poms that receive the system dependency (integrator, 2026-11-04): only the ones depending on the module
# ---------------------------------------------------------------------------------------------------------------------
BOOT_PLUGIN_XML = ("  <build><plugins><plugin><groupId>org.springframework.boot</groupId>"
                   "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>\n")


def _boot_pom(artifact: str, depends_on: str | None) -> str:
    dep = (f"  <dependencies><dependency><groupId>com.x</groupId><artifactId>{depends_on}</artifactId>"
           f"<version>1.0.0</version></dependency></dependencies>\n" if depends_on else "")
    return _pom(artifact).replace("</project>", dep + BOOT_PLUGIN_XML + "</project>")


def _with_boot_poms(ws: Workspace) -> None:
    for rel, text in {"svc-a-boot/pom.xml": _boot_pom("svc-a-boot", "app-back"),
                      "svc-b-boot/pom.xml": _boot_pom("svc-b-boot", None)}.items():
        p = ws.abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    root = ws.abs("pom.xml")
    root.write_text(root.read_text(encoding="utf-8").replace("<module>app-back</module>",
                    "<module>app-back</module><module>svc-a-boot</module><module>svc-b-boot</module>"), encoding="utf-8")


def test_only_the_boot_pom_depending_on_the_module_gets_the_system_dependency(cfg, tmp_path, wired):
    ws = _ws(tmp_path)
    _with_boot_poms(ws)
    loop = make_loop(cfg, ws, wired_build())
    assert sorted(loop._bundled_boot_poms()) == ["svc-a-boot/pom.xml", "svc-b-boot/pom.xml"]
    assert loop._bundled_boot_poms(BACK) == ["svc-a-boot/pom.xml"]


def test_every_boot_pom_is_kept_when_none_depends_on_the_module(cfg, tmp_path, wired):
    ws = _ws(tmp_path)
    _with_boot_poms(ws)
    ws.abs("svc-a-boot/pom.xml").write_text(_boot_pom("svc-a-boot", None), encoding="utf-8")
    loop = make_loop(cfg, ws, wired_build())
    assert sorted(loop._bundled_boot_poms(BACK)) == ["svc-a-boot/pom.xml", "svc-b-boot/pom.xml"]


def test_the_module_itself_is_kept_when_it_is_a_boot_pom(cfg, tmp_path, wired):
    ws = _ws(tmp_path)
    _with_boot_poms(ws)
    loop = make_loop(cfg, ws, wired_build())
    assert loop._bundled_boot_poms("svc-b-boot/pom.xml") == ["svc-b-boot/pom.xml"]


def test_every_pre_try_source_is_a_valid_try_source():
    """A pre-try whose source is missing from `TrySource` never reaches the error list (`begin_try` raises) — the
    e2e of v2.1z-i found `sbmig-bundled` missing; keep the two in step."""
    import typing

    from sbmig.core.models import TrySource

    assert set(fl.PRE_TRY_SOURCES) <= set(typing.get_args(TrySource))
