"""v2.1l dependency versions, fix-loop side (docs/INTERFACES.md "v2.1l"): carried over from the as-is build, never
guessed by the LLM. (a) the resolution order (`VersionResolver`: managed by the new chain → as-is → repository for an
added dependency → unknown); (b) `_fill_versions` after an applied try: a dependency the try ADDED without a version
gets the as-is / repository version in the same commit, one that was already there only the as-is one and only when
the build reported it missing, protected keys stay untouched; (c) a `pom_invalid|missing_version` case is first
tried without the LLM (`sbmig-version`, zero LLM calls) and falls through to the LLM when nothing is resolvable;
(d) the as-is / new-chain facts reach `FixContext.library_hints`."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.build.signatures import fill_signatures
from sbmig.core.models import (
    GAV,
    BuildError,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FixAttempt,
    Phase,
    ScriptRunResult,
    TriageDecision,
)
from sbmig.core.workspace import Workspace, logs_dir
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.fixloop import MISSING_VERSION_SIG, SBMIG_VERSION_SOURCE, VersionResolver, _Round
from sbmig.restructure.pom_editor import PomEditor
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeFixer,
    FakeKB,
    answer,
    bad_build,
    journal_events,
    ok_build,
)

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
TEXT = "org.apache.commons:commons-text"
IO = "commons-io:commons-io"
YAML = "org.yaml:snakeyaml"
CORE = "org.springframework:spring-core"
FOO = "src/main/java/com/x/Foo.java"


def _pom(deps: list[tuple[str, str | None]], aid: str = "demo") -> str:
    body = "".join(f"    <dependency>\n      <groupId>{k.split(':')[0]}</groupId>\n      <artifactId>{k.split(':')[1]}"
                   f"</artifactId>\n" + (f"      <version>{v}</version>\n" if v else "") + "    </dependency>\n"
                   for k, v in deps)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            f"  <groupId>com.x</groupId>\n  <artifactId>{aid}</artifactId>\n  <version>1.0.0</version>\n"
            f"  <dependencies>\n{body}  </dependencies>\n</project>\n")


def _ws(tmp_path: Path, files: dict[str, str]) -> Workspace:
    src = tmp_path / "src" / "demo"
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def _captures(ws: Workspace, asis: dict | None = None, after: dict | None = None) -> None:
    d = logs_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    if asis is not None:
        (d / "effective-depmgmt.json").write_text(json.dumps(asis), encoding="utf-8")
    if after is not None:
        (d / "effective-depmgmt-after-restructure.json").write_text(json.dumps(after), encoding="utf-8")


def _versions(ws: Workspace, rel: str = "pom.xml") -> dict[str, str | None]:
    return {g.key: g.version for g in PomEditor(ws.abs(rel)).dependencies()}


def missing(key: str, pom: str = "pom.xml", module: str = "demo") -> BuildError:
    g, a = key.split(":")
    return fill_signatures(BuildError(kind=ErrorKind.POM_INVALID, file=pom, module=module,
                                      message=f"'dependencies.dependency.version' for {key}:jar is missing.",
                                      artifact=GAV(group_id=g, artifact_id=a)))


def pom_build(managed: frozenset[str] = frozenset(), broken_file: str | None = None):
    """Maven stand-in: every version-less dependency not in `managed` is a missing-version error; with none left, a
    compile error while `broken_file` still says "broken"."""
    def fn(ws, call):
        errs = []
        for rel in ws.pom_files():
            ed = PomEditor(ws.abs(rel))
            errs += [missing(g.key, rel, ed.artifact_id()) for g in ed.dependencies()
                     if not g.version and g.key not in managed]
        if not errs and broken_file and "broken" in ws.read(broken_file):
            errs.append(BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=broken_file,
                                   module="demo", signature=f"compile|x|{broken_file}",
                                   signature_generic="compile|cannot_find_symbol|X"))
        return bad_build(*errs) if errs else ok_build()
    return fn


class PomApplier:
    """FakeEditApplier + `pom` edits (`add_dependency` / `set_dependency_version`) through the real PomEditor."""

    def __init__(self):
        self.calls: list[list] = []
        self._plain = FakeEditApplier()

    def __call__(self, ws, edits, *, cfg=None, errors=(), runner=None, timeout_s=None) -> ScriptRunResult:
        from sbmig.script.edits import pom_calls
        self.calls.append(list(edits))
        others = [e for e in edits if e.op != "pom"]
        res = self._plain(ws, others) if others else ScriptRunResult(status="ok")
        if res.status != "ok":
            return res
        changed = list(res.changed_files)
        for e in edits:
            if e.op != "pom":
                continue
            calls, problems = pom_calls(e.pom)
            assert not problems, problems
            ed = PomEditor(ws.abs(e.path))
            for name, kw in calls:
                if name == "set_dependency_version":
                    ed.set_dependency_version(kw["g"], kw["a"], kw.get("version"))
                elif name == "add_dependency":
                    ed.add_dependency(GAV(group_id=kw["g"], artifact_id=kw["a"], version=kw.get("version")),
                                      if_absent=False)
            if ed.changed:
                ed.save()
                changed.append(e.path)
        return ScriptRunResult(status="ok", changed_files=list(dict.fromkeys(changed)))


class Lookup:
    """RepositoryLookup stand-in: `versions(g, a)` from a dict (oldest → newest); records the keys asked."""

    def __init__(self, versions: dict[str, list[str]] | None = None, raises: Exception | None = None):
        self.table, self.raises, self.asked = dict(versions or {}), raises, []

    def versions(self, g, a):
        self.asked.append(f"{g}:{a}")
        if self.raises is not None:
            raise self.raises
        return list(self.table.get(f"{g}:{a}", []))

    def candidates(self, key, current):
        from sbmig.core.models import RepositoryVersions
        return RepositoryVersions(key=key, current=current, error="offline (tests)")


def use_lookup(monkeypatch, lookup: Lookup) -> Lookup:
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", lambda cfg, fetch=None: lookup)
    return lookup


def make_loop(cfg, ws, fixer, build, applier=None) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer, FakeKB(), _profile(),
                        budget=LoopBudget.from_config(cfg), edit_applier=applier or PomApplier())


def _profile():
    from .fakes import make_profile
    return make_profile()


def subjects(ws: Workspace) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()


ASIS = {"managed": {TEXT: "1.10.0"}, "direct": {TEXT: "1.10.0", IO: "2.11.0"},
        "parent": "com.company.legacy:legacy-parent:1.0.0",
        "by_module": {"com.x:demo": {"managed": {TEXT: "1.10.0"}, "direct": {TEXT: "1.10.0", IO: "2.11.0"},
                                     "parent": "com.company.legacy:legacy-parent:1.0.0"}}}
AFTER = {"managed": {CORE: "7.0.1"}, "direct": {}, "parent": "com.company:comp-jar:4.0.0",
         "by_module": {"com.x:demo": {"managed": {CORE: "7.0.1"}, "direct": {}, "parent": "com.company:comp-jar:4.0.0"}}}


# ----------------------------------------------------------------------------------------------- (a) resolution order
def test_resolution_order_managed_asis_repository_unknown():
    lookup = Lookup({YAML: ["1.33", "2.2", "2.3", "2.4-M1", "2.5-SNAPSHOT"], IO: ["9.9"]})
    r = VersionResolver(ASIS, AFTER, repository=lambda: lookup)
    assert r.has_asis and r.has_after
    assert r.resolve(CORE, "com.x:demo") == (None, "managed")                 # 1. the new chain manages it
    assert r.resolve(TEXT, "com.x:demo") == ("1.10.0", "asis")                 # 2. the as-is build resolved it
    assert r.resolve(IO, "com.x:demo", allow_repository=True) == ("2.11.0", "asis")    # as-is wins over the repository
    assert r.resolve(YAML, "com.x:demo") == (None, "unknown")                  # 3. only when the caller allows it
    assert r.resolve(YAML, "com.x:demo", allow_repository=True) == ("2.3", "repository")   # newest non-prerelease
    assert r.resolve("x:y", "com.x:demo", allow_repository=True) == (None, "unknown")      # 4.
    assert r.resolve(CORE, "com.x:demo", check_managed=False) == (None, "unknown")         # the build said: missing
    assert lookup.asked == [YAML, "x:y"]                                        # cached per key, IO never asked
    assert r.asis_fact(TEXT, "demo") == ("1.10.0", "declared")                 # the module's direct map
    assert r.asis_fact(TEXT, "other") == ("1.10.0", "managed by com.company.legacy:legacy-parent:1.0.0")
    assert r.asis_fact(IO, "demo") == ("2.11.0", "declared")
    assert r.asis_fact(YAML, "demo") == (None, "")


def test_a_version_less_declaration_takes_the_managed_version_not_a_siblings_explicit_one():
    parent = "com.company.legacy:legacy-parent:1.0.0"
    asis = {"managed": {TEXT: "1.10"}, "direct": {TEXT: "1.9"},          # merged first-wins: the sibling's explicit 1.9
            "parent": parent,
            "by_module": {"com.x:sibling": {"managed": {TEXT: "1.10"}, "direct": {TEXT: "1.9"}, "parent": parent}}}
    r = VersionResolver(asis, {})
    # the queried module declared it without a version (after the restructure: not a module of the as-is capture)
    assert r.asis_fact(TEXT, "com.x:demo-back") == ("1.10", f"managed by {parent}")
    assert r.resolve(TEXT, "com.x:demo-back") == ("1.10", "asis")
    assert r.asis_fact(TEXT, "com.x:sibling") == ("1.9", "declared")          # the sibling's own explicit version
    # a module of the capture with only a managed entry: the managed version, labelled by its own parent
    asis["by_module"]["com.x:demo"] = {"managed": {TEXT: "1.10"}, "direct": {}, "parent": "com.x:mid:2"}
    assert VersionResolver(asis, {}).asis_fact(TEXT, "demo") == ("1.10", "managed by com.x:mid:2")
    # the label names the map the hit came from, never crossed
    assert VersionResolver({"direct": {IO: "2.11.0"}}, {}).asis_fact(IO) == ("2.11.0", "declared")
    assert VersionResolver({"managed": {IO: "2.15.0"}, "direct": {IO: "2.11.0"}}, {}).asis_fact(IO) == (
        "2.15.0", "managed")


def test_the_declaring_modules_chain_decides_and_the_merged_view_is_the_fallback():
    after = {"managed": {CORE: "7.0.1", YAML: "2.4"},
             "by_module": {"com.x:demo-boot": {"managed": {CORE: "7.0.1", YAML: "2.4"}},
                           "com.x:demo-back": {"managed": {CORE: "7.0.1"}}}}
    asis = {"direct": {YAML: "1.30"}, "by_module": {"com.x:old": {"direct": {YAML: "1.33"}}}}
    r = VersionResolver(asis, after)
    assert r.managed_version(YAML, "com.x:demo-boot") == "2.4"
    assert r.managed_version(YAML, "com.x:demo-back") is None                 # another module's BOM is not this one's
    assert r.managed_version(YAML, "demo-back") is None                       # matched by artifactId too
    assert r.managed_version(YAML, "com.x:unknown") == "2.4"                  # no module entry → merged view
    assert r.resolve(YAML, "com.x:demo-back") == ("1.30", "asis")             # module not in the as-is: merged direct
    assert r.resolve(YAML, "com.x:old", check_managed=False) == ("1.33", "asis")


def test_a_repository_failure_is_unknown_and_empty_captures_have_nothing(caplog):
    r = VersionResolver(None, {}, repository=lambda: Lookup(raises=OSError("network down")))
    assert not r.has_asis and not r.has_after
    assert r.resolve(YAML, None, allow_repository=True) == (None, "unknown")
    assert VersionResolver({}, None, repository=lambda: None).resolve(YAML, allow_repository=True) == (None, "unknown")


def test_the_resolver_reads_the_run_directorys_captures(tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    assert not VersionResolver.for_workspace(ws).has_asis
    _captures(ws, ASIS, AFTER)
    r = VersionResolver.for_workspace(ws)
    assert r.has_asis and r.has_after and r.resolve(TEXT, "com.x:demo") == ("1.10.0", "asis")


# ----------------------------------------------------------------------------- (b) versions filled after an applied try
def test_an_added_version_less_dependency_gets_the_asis_version_in_the_same_commit(cfg, tmp_path, monkeypatch):
    lookup = use_lookup(monkeypatch, Lookup({YAML: ["2.2", "2.3"]}))
    ws = _ws(tmp_path, {"pom.xml": _pom([(CORE, None)]), FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS, AFTER)
    fix = answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"},
                        {"op": "pom", "path": "pom.xml", "pom": {"add_dependency": [
                            {"group_id": "org.apache.commons", "artifact_id": "commons-text", "version": None},
                            {"group_id": "org.yaml", "artifact_id": "snakeyaml", "version": None},
                            {"group_id": "org.apache.commons", "artifact_id": "commons-lang3", "version": None},
                            {"group_id": "org.springframework.boot", "artifact_id": "spring-boot-starter-validation",
                             "version": None}]}}])
    fixer = FakeFixer(scripts=[fix])
    loop = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({CORE, "org.apache.commons:commons-lang3",
                                                                   "org.springframework.boot:spring-boot-starter-validation"}),
                                               broken_file=FOO))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1
    v = _versions(ws)
    assert v[TEXT] == "1.10.0" and v[YAML] == "2.3"                           # as-is, then the repository
    assert v[CORE] is None                                                    # managed by the new chain: stays empty
    assert v["org.apache.commons:commons-lang3"] is None                      # protected (optional in-house)
    assert v["org.springframework.boot:spring-boot-starter-validation"] is None   # protected (mandatory)
    assert lookup.asked == [YAML]
    [att] = pr.attempts
    assert att.source == "llm-edit" and att.outcome == "build_ok"
    # the fill is part of the attempt's own commit (no commit of its own)
    assert len(subjects(ws)) == 2 and subjects(ws)[0].startswith("fix(edits:")
    assert "<version>1.10.0</version>" in ws.diff(att.base_commit, att.commit)
    assert f"version filled by sbmig: {TEXT} 1.10.0 in pom.xml (the as-is build)" in att.notes
    assert f"version filled by sbmig: {YAML} 2.3 in pom.xml (newest release in the repository)" in att.notes
    ev = [e for e in journal_events(ws) if e["event"] == "version_filled"]
    assert [(e["key"], e["version"], e["source"]) for e in ev] == [(TEXT, "1.10.0", "asis"),
                                                                   (YAML, "2.3", "repository")]
    assert all(e["case"] == att.script_case_id for e in ev)


def test_pre_existing_dependencies_are_filled_from_the_asis_build_only_and_only_when_reported_missing(cfg, tmp_path,
                                                                                                       monkeypatch):
    use_lookup(monkeypatch, Lookup({YAML: ["2.3"], TEXT: ["1.13.0"]}))
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None), (YAML, None), (CORE, None), (IO, "2.16.1")])})
    _captures(ws, ASIS, AFTER)
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    pre = ws.head()
    ed = PomEditor(ws.abs("pom.xml"))                     # the "attempt": an unrelated pom change (a property)
    ed.set_property("x.y", "1")
    ed.save()
    build = bad_build(missing(TEXT), missing(YAML))       # CORE: resolved by that build (managed)
    st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={})
    c = ErrorCluster(id="case:mv", kind=ErrorKind.POM_INVALID, errors=list(build.errors))
    att = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id=c.id,
                     decision=TriageDecision(cluster_id=c.id, kind=DecisionKind.SCRIPT, rationale="t"))
    got = loop._fill_versions(att, c, ScriptRunResult(status="ok", changed_files=["pom.xml"]), pre, st)
    assert got == [("pom.xml", TEXT, "1.10.0", "asis")]   # YAML: not in the as-is build, no repository for it
    v = _versions(ws)
    assert v[TEXT] == "1.10.0" and v[YAML] is None and v[CORE] is None and v[IO] == "2.16.1"
    assert ws.is_dirty()                                  # not committed here: the caller's commit takes it


def test_a_pom_whose_management_the_attempt_changed_is_left_alone_and_nothing_filled_is_silent(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS, AFTER)
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    pre = ws.head()
    ed = PomEditor(ws.abs("pom.xml"))                     # a BOM import: the new management is unknown to sbmig
    ed.add_managed_dependency(GAV(group_id="org.x", artifact_id="bom", version="1", type="pom", scope="import"))
    ed.add_dependency(GAV(group_id="org.yaml", artifact_id="snakeyaml"))
    ed.save()
    build = bad_build(missing(TEXT))
    st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={})
    c = ErrorCluster(id="case:mv", kind=ErrorKind.POM_INVALID, errors=list(build.errors))
    att = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id=c.id,
                     decision=TriageDecision(cluster_id=c.id, kind=DecisionKind.SCRIPT, rationale="t"))
    assert loop._fill_versions(att, c, ScriptRunResult(status="ok", changed_files=["pom.xml"]), pre, st) == []
    assert _versions(ws)[TEXT] is None and att.notes == []
    assert not [e for e in journal_events(ws) if e["event"] == "version_filled"]


def test_a_version_the_attempt_dropped_comes_back_only_when_the_new_chain_is_known(cfg, tmp_path):
    for after, expect in ((AFTER, "1.10.0"), (None, None)):
        ws = _ws(tmp_path / ("after" if after else "none"), {"pom.xml": _pom([(TEXT, "1.9"), (CORE, "6.0.0")])})
        _captures(ws, ASIS, after)
        loop = make_loop(cfg, ws, FakeFixer(), pom_build())
        pre = ws.head()
        ed = PomEditor(ws.abs("pom.xml"))                 # the LLM: "the parent manages them"
        ed.set_dependency_version("org.apache.commons", "commons-text", None)
        ed.set_dependency_version("org.springframework", "spring-core", None)
        ed.save()
        st = _Round(phase=Phase.VERIFY, iteration=1, build=bad_build(), target=None, stall_level=0, others={})
        c = ErrorCluster(id="case:x", kind=ErrorKind.COMPILE, errors=[])
        att = FixAttempt(id="verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id=c.id,
                         decision=TriageDecision(cluster_id=c.id, kind=DecisionKind.SCRIPT, rationale="t"))
        loop._fill_versions(att, c, ScriptRunResult(status="ok", changed_files=["pom.xml"]), pre, st)
        v = _versions(ws)
        assert v[TEXT] == expect                          # not managed by the new chain: the as-is version
        assert v[CORE] is None                            # managed by the new chain (or the chain is unknown)


def test_without_the_post_restructure_capture_an_added_dependency_is_still_filled(cfg, tmp_path, monkeypatch):
    """Left empty, the next build's pom_invalid would revert the whole try (structural regression): step 1 then only
    sees the project's own dependencyManagement."""
    lookup = use_lookup(monkeypatch, Lookup({YAML: ["2.3"], IO: ["2.16.1"]}))
    parent = _pom([]).replace(
        "  <dependencies>\n  </dependencies>\n",
        "  <packaging>pom</packaging>\n  <dependencyManagement>\n    <dependencies>\n      <dependency>\n"
        "        <groupId>commons-io</groupId>\n        <artifactId>commons-io</artifactId>\n"
        "        <version>2.15.0</version>\n      </dependency>\n    </dependencies>\n  </dependencyManagement>\n"
        "  <modules>\n    <module>app</module>\n  </modules>\n")
    app = _pom([], aid="app").replace("  <groupId>com.x</groupId>\n",
                                      "  <parent>\n    <groupId>com.x</groupId>\n    <artifactId>demo</artifactId>\n"
                                      "    <version>1.0.0</version>\n  </parent>\n")
    ws = _ws(tmp_path, {"pom.xml": parent, "app/pom.xml": app, FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS)                                   # as-is only
    fix = answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"},
                        {"op": "pom", "path": "app/pom.xml", "pom": {"add_dependency": [
                            {"group_id": "org.yaml", "artifact_id": "snakeyaml", "version": None},
                            {"group_id": "commons-io", "artifact_id": "commons-io", "version": None}]}}])
    fixer = FakeFixer(scripts=[fix])
    pr = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({IO}), broken_file=FOO)).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1
    assert [(a.source, a.outcome) for a in pr.attempts] == [("llm-edit", "build_ok")]
    v = _versions(ws, "app/pom.xml")
    assert v[YAML] == "2.3"                               # not in the as-is build: the repository's newest release
    assert v[IO] is None                                  # managed by the in-project parent: stays empty
    assert lookup.asked == [YAML]


# ---------------------------------------------------------------------------- (c) the zero-LLM missing-version try
def test_a_missing_version_case_is_fixed_without_the_llm(cfg, tmp_path, monkeypatch):
    use_lookup(monkeypatch, Lookup({YAML: ["2.2", "2.3", "3.0-RC1"]}))
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None), (YAML, None), (CORE, None)])})
    _captures(ws, ASIS)                                   # no post-restructure capture: the build says what is missing
    fixer = FakeFixer()
    loop = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({CORE})))
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok
    assert fixer.contexts == [] and loop.budget.llm_calls == 0                # zero LLM calls
    v = _versions(ws)
    assert v[TEXT] == "1.10.0" and v[YAML] == "2.3" and v[CORE] is None
    [att] = pr.attempts
    assert (att.source, att.script_mode, att.script_version, att.outcome) == (SBMIG_VERSION_SOURCE, "new", 0,
                                                                              "build_ok")
    assert att.llm_usage.calls == 0 and att.edits_applied == 2
    assert subjects(ws)[0] == f"fix(sbmig-version): {att.cluster_id}"
    assert f"version {TEXT} 1.10.0 in pom.xml (the as-is build)" in att.notes
    assert f"version {YAML} 2.3 in pom.xml (newest release in the repository)" in att.notes
    assert att.decision.rationale.startswith("sbmig-version:")
    [ev] = [e for e in journal_events(ws) if e["event"] == "version_try"]
    assert [(x["key"], x["version"], x["source"]) for x in ev["resolved"]] == [(TEXT, "1.10.0", "asis"),
                                                                                (YAML, "2.3", "repository")]
    assert ev["unknown"] == []


def test_nothing_resolvable_falls_through_to_the_llm_in_the_same_round_with_the_facts(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(YAML, None)])})
    _captures(ws, ASIS, AFTER)                            # snakeyaml: neither the as-is build nor the repository
    fix = answer(edits=[{"op": "pom", "path": "pom.xml", "pom": {
        "set_dependency_version": {"group_id": "org.yaml", "artifact_id": "snakeyaml", "version": "2.2"}}}])
    fixer = FakeFixer(scripts=[fix])
    loop = make_loop(cfg, ws, fixer, pom_build())
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(fixer.contexts) == 1 and loop.budget.llm_calls == 1
    assert [a.source for a in pr.attempts] == ["llm-edit"]                    # no sbmig-version attempt recorded
    assert pr.attempts[0].iteration == 1                                      # the same round
    ctx = fixer.contexts[0]
    assert ctx.library_hints[:1] == [f"as-is build: {YAML} not declared"]
    assert ctx.script_mode == "new"
    [ev] = [e for e in journal_events(ws) if e["event"] == "version_try"]
    assert ev["resolved"] == [] and ev["unknown"] == [YAML]


def test_the_sbmig_version_try_does_not_use_up_an_llm_try_and_runs_once_per_pom_and_artifact(cfg, tmp_path,
                                                                                              monkeypatch):
    cfg = cfg.model_copy(deep=True)
    cfg.script.max_augments_per_case = 1
    use_lookup(monkeypatch, Lookup())
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None), (YAML, None)])})
    _captures(ws, ASIS)
    fixer = FakeFixer(scripts=[answer(edits=[{"op": "pom", "path": "pom.xml", "pom": {"set_dependency_version": {
        "group_id": "org.yaml", "artifact_id": "snakeyaml", "version": "2.2"}}}])])
    loop = make_loop(cfg, ws, fixer, pom_build())
    pr = loop.run(Phase.VERIFY, "package", 4)
    # round 1: sbmig-version fills commons-text (partial: snakeyaml unknown); round 2: the one LLM try still left
    assert pr.ok and len(fixer.contexts) == 1
    assert [(a.source, a.outcome) for a in pr.attempts] == [(SBMIG_VERSION_SOURCE, "partial"), ("llm-edit", "build_ok")]
    assert _versions(ws) == {TEXT: "1.10.0", YAML: "2.2"}                     # the partial fill was not rolled back
    assert len([e for e in journal_events(ws) if e["event"] == "version_try"]) == 1
    # the LLM saw sbmig's try among the earlier ones
    assert [a.source for a in fixer.contexts[0].previous_attempts] == [SBMIG_VERSION_SOURCE]


def test_the_real_edit_applier_writes_the_missing_version(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=pom_build()), Triager(cfg), FakeFixer(), FakeKB(),
                        _profile(), budget=LoopBudget.from_config(cfg))        # edit_applier None → sbmig.script
    pr = loop.run(Phase.VERIFY, "package", 2)
    assert pr.ok and [a.source for a in pr.attempts] == [SBMIG_VERSION_SOURCE]
    assert _versions(ws)[TEXT] == "1.10.0"
    assert "<version>1.10.0</version>" in ws.read("pom.xml")


# --------------------------------------------------------------------------------------------- (d) the prompt facts
def test_the_asis_and_new_chain_facts_lead_the_dependency_versions_lines(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, "1.10.0")])})
    _captures(ws, ASIS, AFTER)
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    c = ErrorCluster(id="case:x", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=[
        BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                   artifact=GAV(group_id="org.apache.commons", artifact_id="commons-text", version="1.10.0"),
                   signature_generic=f"dependency_resolution|{TEXT}"),
        BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                   artifact=GAV(group_id="org.springframework", artifact_id="spring-core"),
                   signature_generic=f"dependency_resolution|{CORE}"),
        BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                   artifact=GAV(group_id="org.springframework.boot", artifact_id="spring-boot-starter-validation"),
                   signature_generic="dependency_resolution|x")])
    hints = loop._library_hints(c)
    assert hints[:4] == [f"as-is build: {TEXT} 1.10.0 (declared)",
                         f"as-is build: {CORE} not declared",
                         f"new parent chain: manages {CORE} at 7.0.1",
                         "as-is build: org.springframework.boot:spring-boot-starter-validation not declared"]
    # in-house / protected ones are listed too; the repository lines (offline here) follow


def test_no_capture_no_facts(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    c = ErrorCluster(id="case:x", kind=ErrorKind.POM_INVALID, errors=[missing(TEXT)])
    assert loop._version_facts(c) == []
    assert missing(TEXT).signature_generic == MISSING_VERSION_SIG


# ------------------------------------------------------------------------------------ the other paths through the loop
def test_a_fix_script_that_adds_a_version_less_dependency_gets_it_filled(cfg, tmp_path):
    from .fakes import FakeScriptRunner, write_script
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS, AFTER)
    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n", "pom.xml": _pom([(TEXT, None)])})])
    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=pom_build(broken_file=FOO)), Triager(cfg), fixer, FakeKB(),
                        _profile(), budget=LoopBudget.from_config(cfg), script_runner=FakeScriptRunner(),
                        edit_applier=PomApplier())
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert pr.ok and [(a.source, a.outcome) for a in pr.attempts] == [("llm-new", "build_ok")]
    assert _versions(ws)[TEXT] == "1.10.0"
    assert subjects(ws)[0].startswith("fix(script:") and len(subjects(ws)) == 2   # one commit: the script's own


def test_the_sbmig_version_try_runs_when_the_llm_budget_is_used_up(cfg, tmp_path):
    cfg = cfg.model_copy(deep=True)
    cfg.loop.max_llm_calls = -1                                               # no LLM call at all (fleet budget)
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    fixer = FakeFixer()
    pr = make_loop(cfg, ws, fixer, pom_build()).run(Phase.VERIFY, "package", 2)
    assert pr.ok and fixer.contexts == [] and [a.source for a in pr.attempts] == [SBMIG_VERSION_SOURCE]


def test_edits_mode_gets_the_same_zero_llm_try(cfg, tmp_path):
    cfg = cfg.model_copy(deep=True)
    cfg.fix.mode = "edits"
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    fixer = FakeFixer()
    pr = make_loop(cfg, ws, fixer, pom_build()).run(Phase.VERIFY, "package", 2)
    assert pr.ok and fixer.contexts == []
    [att] = pr.attempts
    assert att.source == SBMIG_VERSION_SOURCE and subjects(ws)[0] == f"fix(sbmig-version): {att.cluster_id}"


# --------------------------------------------------------------------------- review fixes: ordering and retries
DUP = "pom_invalid|duplicate_declaration"


def dup_error(key: str) -> BuildError:
    """A pom problem of the "Some problems were encountered while processing the POMs" block: no pom file."""
    return BuildError(kind=ErrorKind.POM_INVALID, module="demo", message=f"duplicate declaration of {key}",
                      signature=f"{DUP}|{key}", signature_generic=DUP)


def dup_then_missing_build(ws, call):
    ed = PomEditor(ws.abs("pom.xml"))
    keys = [g.key for g in ed.dependencies()]
    errs = [dup_error(k) for k in dict.fromkeys(keys) if keys.count(k) > 1]
    errs += [missing(g.key) for g in ed.dependencies() if not g.version]
    return bad_build(*errs) if errs else ok_build()


def test_the_version_try_waits_when_another_case_changed_the_pom_this_round(cfg, tmp_path, monkeypatch):
    """Round 1: case A (2 errors → first) rewrites pom.xml; case B (missing version on pom.xml) must wait for the
    rebuild — not commit onto A's pom in the same round (two tries scored on one tree, A's revert conflicting)."""
    use_lookup(monkeypatch, Lookup({TEXT: ["1.12.0"]}))   # repository only: A's own fill (steps 1–2) cannot do it
    pom = _pom([(TEXT, None), (IO, "2.11.0"), (IO, "2.11.0"), (YAML, "2.2"), (YAML, "2.2")])
    ws = _ws(tmp_path, {"pom.xml": pom})
    fixed = _pom([(TEXT, None), (IO, "2.11.0"), (YAML, "2.2")])
    fixer = FakeFixer(scripts=[answer(edits=[{"op": "write", "path": "pom.xml", "content": fixed}])])
    pr = make_loop(cfg, ws, fixer, dup_then_missing_build).run(Phase.VERIFY, "package", 4)
    assert pr.ok and len(fixer.contexts) == 1
    a, b = pr.attempts
    assert a.source == "llm-edit" and b.source == SBMIG_VERSION_SOURCE
    assert b.iteration > a.iteration                      # B only after the rebuild that scored A
    assert a.commit != b.commit and _versions(ws)[TEXT] == "1.12.0"
    deferred = [e for e in journal_events(ws) if e["event"] == "decision" and e["kind"] == "skip"
                and e["cluster_id"] == b.cluster_id and e["iteration"] == a.iteration]
    assert len(deferred) == 1 and "deferred: its module's pom was changed by case" in deferred[0]["rationale"]


def test_the_version_try_defers_on_a_pom_changed_this_round_and_runs_once_it_is_not(cfg, tmp_path):
    from sbmig.core.models import PhaseResult
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    build = bad_build(missing(TEXT))
    [c] = loop._cluster(build.errors, None)
    head = ws.head()
    for poms, covered in (({"pom.xml": "case:other"}, {}), ({}, {"pom.xml": "case:other"})):
        st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={},
                    poms=dict(poms), covered=dict(covered))
        pr = PhaseResult(phase=Phase.VERIFY)
        assert loop._handle_cluster(pr, st, c) == "deferred"
        assert pr.attempts == [] and ws.head() == head and _versions(ws)[TEXT] is None
        assert st.decisions[-1].rationale.startswith("deferred:")
    st = _Round(phase=Phase.VERIFY, iteration=1, build=build, target=None, stall_level=0, others={})
    pr = PhaseResult(phase=Phase.VERIFY)
    assert loop._handle_cluster(pr, st, c) == "attempted"            # a deferral did not use the pair up
    assert [a.source for a in pr.attempts] == [SBMIG_VERSION_SOURCE] and _versions(ws)[TEXT] == "1.10.0"


class FlakyApplier(PomApplier):
    """A PomApplier whose first `fail` calls raise (a transient file lock on the company's Windows PC)."""

    def __init__(self, fail: int = 1):
        super().__init__()
        self.fail = fail

    def __call__(self, ws, edits, **kw):
        if self.fail:
            self.fail -= 1
            raise PermissionError("[WinError 32] the file is being used by another process: pom.xml")
        return super().__call__(ws, edits, **kw)


def test_an_apply_failure_does_not_use_up_the_zero_llm_try(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    fixer = FakeFixer()                                   # its answers apply nothing
    pr = make_loop(cfg, ws, fixer, pom_build(), applier=FlakyApplier(1)).run(Phase.VERIFY, "package", 4)
    assert pr.ok and _versions(ws)[TEXT] == "1.10.0"
    assert [(a.source, a.outcome) for a in pr.attempts] == [
        (SBMIG_VERSION_SOURCE, "apply_failed"), ("llm-new", "apply_failed"), (SBMIG_VERSION_SOURCE, "build_ok")]
    assert "PermissionError" in pr.attempts[0].apply_errors[0]


def test_a_pair_whose_try_keeps_failing_to_apply_is_given_up_after_three(cfg, tmp_path):
    from sbmig.loop.fixloop import VERSION_TRY_APPLY_FAILURES
    cfg = cfg.model_copy(deep=True)
    cfg.script.max_augments_per_case = 5
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS)
    pr = make_loop(cfg, ws, FakeFixer(), pom_build(), applier=FlakyApplier(99)).run(Phase.VERIFY, "package", 6)
    assert not pr.ok
    tries = [a for a in pr.attempts if a.source == SBMIG_VERSION_SOURCE]
    assert len(tries) == VERSION_TRY_APPLY_FAILURES == 3 and all(a.outcome == "apply_failed" for a in tries)
    assert len([a for a in pr.attempts if a.source != SBMIG_VERSION_SOURCE]) == 5    # the ladder went on


def test_nothing_found_is_remembered_so_the_ladder_is_not_delayed_twice(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(YAML, None)])})
    _captures(ws, ASIS)                                   # snakeyaml: in neither the as-is build nor the repository
    pr = make_loop(cfg, ws, FakeFixer(), pom_build()).run(Phase.VERIFY, "package", 3)
    assert not pr.ok and all(a.source != SBMIG_VERSION_SOURCE for a in pr.attempts)
    assert len([e for e in journal_events(ws) if e["event"] == "version_try"]) == 1


# ------------------------------------------- an added explicit version the new chain manages (company run 2026-10-30)
WEBMVC = "org.springframework:spring-webmvc"
CHAIN_AFTER = {"managed": {WEBMVC: "7.0.8"}, "direct": {}, "parent": "com.company:comp-jar:4.0.0",
               "by_module": {"com.x:demo": {"managed": {WEBMVC: "7.0.8"}, "direct": {}}}}


def _add_with_version(key: str, version: str) -> dict:
    g, a = key.split(":")
    return {"op": "pom", "path": "pom.xml", "pom": {"add_dependency": {"group_id": g, "artifact_id": a,
                                                                      "version": version}}}


def _fix_foo() -> dict:
    return {"op": "write", "path": FOO, "content": "class Foo { ok }\n"}


def test_an_added_explicit_version_the_chain_manages_is_removed(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS, CHAIN_AFTER)
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo(), _add_with_version(WEBMVC, "7.0.0")])])
    pr = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({WEBMVC}), broken_file=FOO)).run(
        Phase.VERIFY, "package", 3)
    assert pr.ok
    [att] = pr.attempts
    assert _versions(ws) == {WEBMVC: None}                                    # managed wins
    assert "<version>7.0.0</version>" not in ws.read("pom.xml") and not ws.is_dirty()
    assert "<version>7.0.0</version>" not in (ws.file_at(att.commit, "pom.xml") or "")   # committed without it
    assert f"version removed by sbmig: {WEBMVC} 7.0.0 in pom.xml (the new parent chain manages it at 7.0.8)" \
        in att.notes
    [ev] = [e for e in journal_events(ws) if e["event"] == "version_filled"]
    assert (ev["key"], ev["source"], ev["removed"], ev["version"]) == (WEBMVC, "managed", "7.0.0", "7.0.8")


def test_without_the_capture_the_parent_chain_facts_decide(cfg, tmp_path, monkeypatch):
    from sbmig.build import parent_chain
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts",
                        lambda cfg: parent_chain.ParentChainFacts(roots=["com.company:base-parent:4.0.0"],
                                                                  managed={WEBMVC: "7.0.8"}))
    ws = _ws(tmp_path, {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS)                                   # no post-restructure capture
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo(), _add_with_version(WEBMVC, "7.0.0"),
                                             _add_with_version(YAML, "2.2")])])
    pr = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({WEBMVC}), broken_file=FOO)).run(
        Phase.VERIFY, "package", 3)
    assert pr.ok
    assert _versions(ws) == {WEBMVC: None, YAML: "2.2"}   # the chain does not manage snakeyaml: its version stays


def test_pre_existing_explicit_versions_are_untouched_and_an_unmanaged_added_one_stays(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(WEBMVC, "6.2.12")]), FOO: "class Foo { broken }\n"})
    _captures(ws, ASIS, CHAIN_AFTER)
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo(), _add_with_version(YAML, "2.2")])])
    pr = make_loop(cfg, ws, fixer, pom_build(broken_file=FOO)).run(Phase.VERIFY, "package", 3)
    assert pr.ok
    assert _versions(ws) == {WEBMVC: "6.2.12", YAML: "2.2"}   # the restructure's rules own the pre-existing pin
    assert not [e for e in journal_events(ws) if e["event"] == "version_filled"]


def test_a_version_property_the_attempt_added_only_for_it_goes_too(cfg, tmp_path):
    base = _pom([(YAML, "${snakeyaml.version}")]).replace(
        "  <dependencies>", "  <properties>\n    <snakeyaml.version>2.2</snakeyaml.version>\n  </properties>\n"
        "  <dependencies>")
    ws = _ws(tmp_path, {"pom.xml": base, FOO: "class Foo { broken }\n"})
    new = _pom([(YAML, "${snakeyaml.version}"), (WEBMVC, "${webmvc.version}"), (CORE, "${core.version}"),
                (TEXT, "${core.version}")]).replace(
        "  <dependencies>", "  <properties>\n    <snakeyaml.version>2.2</snakeyaml.version>\n"
        "    <webmvc.version>7.0.0</webmvc.version>\n    <core.version>7.0.0</core.version>\n  </properties>\n"
        "  <dependencies>")
    after = {"managed": {WEBMVC: "7.0.8", CORE: "7.0.8"}, "by_module": {"com.x:demo": {"managed": {
        WEBMVC: "7.0.8", CORE: "7.0.8"}}}}
    _captures(ws, ASIS, after)
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo(), {"op": "write", "path": "pom.xml", "content": new}])])
    pr = make_loop(cfg, ws, fixer, pom_build(managed=frozenset({WEBMVC, CORE}), broken_file=FOO)).run(
        Phase.VERIFY, "package", 3)
    assert pr.ok
    ed = PomEditor(ws.abs("pom.xml"))
    assert _versions(ws) == {YAML: "${snakeyaml.version}", WEBMVC: None, CORE: None, TEXT: "${core.version}"}
    # webmvc.version: added for webmvc only → gone; core.version: commons-text still uses it; snakeyaml's: the owner's
    assert ed.properties() == {"snakeyaml.version": "2.2", "core.version": "7.0.0"}


# ------------------------------------------- v2.1s: the TARGET Boot BOM before the as-is value (company run 78c2f2c)
SERVLET = "jakarta.servlet:jakarta.servlet-api"


def test_the_target_boot_bom_comes_before_the_asis_version():
    chain = {"managed": {CORE: "7.0.8"}}
    boot = {SERVLET: "6.1.0", CORE: "7.0.1"}
    r = VersionResolver({"managed": {SERVLET: "4.0.3"}}, {}, chain=chain, boot=boot)
    assert r.resolve(SERVLET, "com.x:demo") == ("6.1.0", "boot-bom")       # the to-be line wins over as-is 4.0.3
    assert r.resolve(CORE, "com.x:demo") == (None, "managed")               # the chain manages it: no version
    assert r.boot_version(CORE) is None and r.boot_version(SERVLET) == "6.1.0"
    newer = VersionResolver({"direct": {SERVLET: "7.0.0"}}, {}, chain=chain, boot=boot)
    assert newer.resolve(SERVLET) == ("7.0.0", "asis")                     # never a downgrade
    assert VersionResolver({}, {}, boot=boot).resolve(SERVLET, check_managed=False) == ("6.1.0", "boot-bom")
    assert VersionResolver({"direct": {YAML: "2.2"}}, {}, boot=boot).resolve(YAML) == ("2.2", "asis")   # not in the BOM


def test_the_chains_boot_add_on_is_not_the_chain_managing_it(cfg, tmp_path, monkeypatch):
    from sbmig.build import boot_bom, parent_chain
    from sbmig.loop.fixloop import chain_own_managed
    bom = {SERVLET: "6.1.0", CORE: "7.0.1"}
    facts = parent_chain.ParentChainFacts(roots=["xxx:xxx-boot-jar:1.0.0-SNAPSHOT"], boot_bom_added=True,
                                          managed={**bom, "com.company:util": "4.0.0", CORE: "7.0.8"})
    # the add-on's entries go; the chain's own (another value for spring-core) stays
    assert chain_own_managed(facts, bom) == {"com.company:util": "4.0.0", CORE: "7.0.8"}
    assert chain_own_managed(facts.model_copy(update={"boot_bom_added": False}), bom) == facts.managed
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda cfg: facts)
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts",
                        lambda cfg, version=None: boot_bom.BootBomFacts(version="4.1.0", managed=bom))
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    _captures(ws, {"managed": {SERVLET: "4.0.3"}})        # as-is managed by the old in-house boot-jar
    r = VersionResolver.for_workspace(ws, cfg=cfg)
    assert r.resolve(SERVLET, "com.x:demo") == ("6.1.0", "boot-bom")
    assert r.resolve(CORE, "com.x:demo") == (None, "managed")


def test_fills_and_pre_tries_journal_the_boot_bom_source_and_the_prompt_says_so(cfg, tmp_path, monkeypatch):
    from sbmig.build import boot_bom
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts",
                        lambda cfg, version=None: boot_bom.BootBomFacts(version="4.1.0", managed={SERVLET: "6.1.0"}))
    # the sbmig-version pre-try: the build says the version is missing → the target BOM's, not the as-is 4.0.3
    ws = _ws(tmp_path / "pre", {"pom.xml": _pom([(SERVLET, None)])})
    _captures(ws, {"managed": {SERVLET: "4.0.3"}})
    pr = make_loop(cfg, ws, FakeFixer(), pom_build()).run(Phase.VERIFY, "package", 2)
    assert pr.ok and _versions(ws) == {SERVLET: "6.1.0"}
    [ev] = [e for e in journal_events(ws) if e["event"] == "version_try"]
    assert [(x["key"], x["version"], x["source"]) for x in ev["resolved"]] == [(SERVLET, "6.1.0", "boot-bom")]
    assert "version jakarta.servlet:jakarta.servlet-api 6.1.0 in pom.xml (the target Spring Boot BOM, not managed by " \
           "the parent chain)" in pr.attempts[0].notes
    # _fill_versions: an LLM try adds it without a version
    ws = _ws(tmp_path / "fill", {"pom.xml": _pom([]), FOO: "class Foo { broken }\n"})
    _captures(ws, {"managed": {SERVLET: "4.0.3"}}, CHAIN_AFTER)
    fixer = FakeFixer(scripts=[answer(edits=[_fix_foo(), {"op": "pom", "path": "pom.xml", "pom": {"add_dependency": {
        "group_id": "jakarta.servlet", "artifact_id": "jakarta.servlet-api", "version": None}}}])])
    pr = make_loop(cfg, ws, fixer, pom_build(broken_file=FOO)).run(Phase.VERIFY, "package", 3)
    assert pr.ok and _versions(ws) == {SERVLET: "6.1.0"}
    [ev] = [e for e in journal_events(ws) if e["event"] == "version_filled"]
    assert (ev["key"], ev["version"], ev["source"]) == (SERVLET, "6.1.0", "boot-bom")
    # the prompt facts
    loop = make_loop(cfg, ws, FakeFixer(), pom_build())
    c = ErrorCluster(id="case:x", kind=ErrorKind.POM_INVALID, errors=[missing(SERVLET)])
    assert loop._version_facts(c) == [f"as-is build: {SERVLET} 4.0.3 (managed)",
                                      f"target Boot BOM: manages {SERVLET} at 6.1.0 (major 4 → 6)"]   # v2.1v
