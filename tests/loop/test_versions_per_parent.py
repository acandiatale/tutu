"""Review T2, fix-loop side: without the post-restructure capture, `VersionResolver.managed_version` answers per module
from the company parent the module's pom really inherits (its own `<parent>`, else the parent of its top-most ancestor
inside the project — the restructurer's `_parent_managed` rule), through `ParentChainFacts.managed_for`. A version-less
dependency is never left in a module whose parent does not manage it, and a version is not compared against another
parent's line. Facts without per-parent sets keep the merged view; the reduced scope (`jdk-jakarta`) never reads the
company chain."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import parent_chain
from sbmig.core.models import Phase
from sbmig.core.workspace import Workspace
from sbmig.loop.fixloop import VersionResolver

from .fakes import FakeFixer, journal_events
from .test_v21l_versions import _captures, _fix_foo, answer, make_loop, pom_build

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
G = "com.company.platform"
BASE, BOOTJ, COMPJ = f"{G}:base-parent:4.0.0", f"{G}:boot-jar:4.0.0", f"{G}:comp-jar:4.0.0"
CTX = "org.springframework:spring-context"
TEXT = "org.apache.commons:commons-text"
FOO = "demo-back/src/main/java/Foo.java"


def _pom(aid: str, parent: str | None, *, modules: list[str] | None = None, rel: str | None = None,
         deps: list[tuple[str, str | None]] | None = None) -> str:
    par = ""
    if parent:
        g, a, v = parent.split(":")
        rp = "" if rel is None else (f"<relativePath>{rel}</relativePath>" if rel else "<relativePath/>")
        par = f"  <parent><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>{rp}</parent>\n"
    mods = "".join(f"<module>{m}</module>" for m in modules or [])
    body = "".join(f"<dependency><groupId>{k.split(':')[0]}</groupId><artifactId>{k.split(':')[1]}</artifactId>"
                   + (f"<version>{v}</version>" if v else "") + "</dependency>" for k, v in deps or [])
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n{par}'
            f"  <groupId>com.x</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0.0</version>\n"
            + (f"  <packaging>pom</packaging>\n  <modules>{mods}</modules>\n" if mods else "")
            + f"  <dependencies>{body}</dependencies>\n</project>\n")


def _ws(tmp_path: Path, extra: dict[str, str] | None = None) -> Workspace:
    files = {"pom.xml": _pom("demo", BASE, modules=["demo-boot", "demo-back", "svc"], rel=""),
             "demo-boot/pom.xml": _pom("demo-boot", BOOTJ, rel=""),
             "demo-back/pom.xml": _pom("demo-back", COMPJ, rel=""),
             # an in-project aggregator on comp-jar and a module under it that inherits the aggregator
             "svc/pom.xml": _pom("svc", COMPJ, modules=["svc-api"], rel=""),
             "svc/svc-api/pom.xml": _pom("svc-api", "com.x:svc:1.0.0"),
             FOO: "class Foo { broken }\n", **(extra or {})}
    src = tmp_path / "src" / "demo"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _facts(per_root: bool = True) -> parent_chain.ParentChainFacts:
    boot, comp = {CTX: "7.0.8"}, {TEXT: "1.12.0"}
    return parent_chain.ParentChainFacts(roots=[BASE, BOOTJ, COMPJ], managed={**comp, **boot},
                                         root_managed={BASE: {}, BOOTJ: dict(boot), COMPJ: dict(comp)} if per_root else {},
                                         sources=["test facts"], poms=3)


@pytest.fixture()
def facts(monkeypatch):
    holder: dict[str, parent_chain.ParentChainFacts] = {"f": _facts()}
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: holder["f"])
    return holder


def test_managed_version_is_the_set_of_the_modules_own_parent(cfg, tmp_path, facts):
    ws = _ws(tmp_path)
    _captures(ws, {"managed": {}})                     # no post-restructure capture
    r = VersionResolver.for_workspace(ws, cfg=cfg)
    assert r.managed_version(CTX, "com.x:demo-boot") == "7.0.8"           # boot-jar manages it
    assert r.managed_version(CTX, "com.x:demo-back") is None              # comp-jar does not
    assert r.managed_version(TEXT, "com.x:demo-back") == "1.12.0"
    assert r.managed_version(TEXT, "demo-back") == "1.12.0"               # a module named by its artifactId
    assert r.managed_version(TEXT, "com.x:demo") is None                  # base-parent manages neither
    # the parent of the top-most in-project ancestor decides for a module under an aggregator
    assert r.managed_version(TEXT, "com.x:svc-api") == "1.12.0"
    assert r.managed_version(CTX, "com.x:svc-api") is None
    # boot_version follows: the module whose parent does not manage it gets the target BOM's line, if any
    r._boot = {CTX: "7.0.9"}
    assert r.boot_version(CTX, "com.x:demo-back") == "7.0.9"
    assert r.boot_version(CTX, "com.x:demo-boot") is None


def test_without_a_module_or_per_parent_sets_the_merged_view_stays(cfg, tmp_path, facts):
    ws = _ws(tmp_path)
    _captures(ws, {"managed": {}})
    r = VersionResolver.for_workspace(ws, cfg=cfg)
    assert r.managed_version(CTX) == "7.0.8"                              # no module: merged as before
    assert r.managed_version(CTX, "com.x:not-a-module") == "7.0.8"        # a module no pom has: merged as before
    facts["f"] = _facts(per_root=False)
    r = VersionResolver.for_workspace(ws, cfg=cfg)
    assert r.managed_version(CTX, "com.x:demo-back") == "7.0.8"           # facts without per-parent sets


def test_a_version_less_dependency_added_to_a_module_its_parent_does_not_manage_gets_a_version(cfg, tmp_path, facts):
    ws = _ws(tmp_path)
    _captures(ws, {"managed": {}, "direct": {CTX: "5.3.39"}})            # the as-is build had spring-context 5.3.39
    add = {"op": "pom", "path": "demo-back/pom.xml",
           "pom": {"add_dependency": {"group_id": "org.springframework", "artifact_id": "spring-context", "version": None}}}
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo_in(), add])])
    pr = make_loop(cfg, ws, fixer, pom_build(broken_file=FOO)).run(Phase.VERIFY, "package", 3)
    from sbmig.restructure.pom_editor import PomEditor
    deps = {g.key: g.version for g in PomEditor(ws.abs("demo-back/pom.xml")).dependencies()}
    assert deps.get(CTX)                                  # comp-jar does not manage it: a version is written
    ev = [e for e in journal_events(ws) if e["event"] == "version_filled"]
    assert ev and ev[-1]["key"] == CTX and ev[-1]["source"] != "managed"
    assert pr.attempts


def _fix_foo_in() -> dict:
    e = _fix_foo()
    e["path"] = FOO
    return e


def test_the_reduced_scope_never_reads_the_company_chain(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts",
                        lambda cfg: pytest.fail("a jdk-jakarta run takes its facts from the project's own chain"))
    ws = _ws(tmp_path)
    asis = {"managed": {CTX: "5.3.39"}, "by_module": {"com.x:demo-back": {"managed": {TEXT: "1.9"}}}}
    _captures(ws, asis)
    r = VersionResolver.for_workspace(ws, cfg=cfg, reduced=True)
    assert r.after is r.asis and r.boot == {} and r.chain == {}
    assert r.managed_version(CTX, "com.x:demo-boot") == "5.3.39"         # the as-is capture, merged
    assert r.managed_version(TEXT, "com.x:demo-back") == "1.9"           # the as-is capture, by module
    assert r.managed_version(TEXT, "com.x:demo-boot") is None
