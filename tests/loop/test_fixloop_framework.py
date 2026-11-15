"""v2.1z-ak: the fix loop in the reduced scope of a framework project (contract section 5).

A framework project keeps its own parent and its own Spring line: the version facts come from the as-is chain only
(never the company chain, never the target Boot BOM), the framework artifact is never written / changed / removed, an
attempt that changes it or any `<parent>` is not kept, `sbmig-parent` does not run and any unresolvable parent aborts
the run without an LLM call; a javax-era library that is the framework artifact ends BLOCKED with a suggestion naming
it. Full-scope runs behave as before (each test checks that side too)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sbmig.build.signatures import fill_signatures
from sbmig.core.models import (
    GAV,
    BlockedItem,
    BlockReason,
    BuildError,
    ErrorCluster,
    ErrorKind,
    FrameworkEvidence,
    FrameworkUse,
    Phase,
)
from sbmig.loop.fixloop import (
    FRAMEWORK_GUARD_PREFIX,
    SBMIG_PARENT_SOURCE,
    SCOPE_FACTS_MAX_CHARS,
    VersionResolver,
    framework_owner_suggestion,
    framework_via_suggestions,
)

from .fakes import FakeBuildRunner, FakeFixer, answer, bad_build, journal_events, make_profile
from .test_asis_parent import FOREIGN, parent_error, pom, set_root
from .test_blocked_flow import make_loop as blocked_loop
from .test_v21l_versions import (
    AFTER,
    ASIS,
    CORE,
    FOO,
    IO,
    TEXT,
    YAML,
    _captures,
    _pom,
    _ws,
    make_loop,
    pom_build,
)

FW = "com.example.fw:example-fw-core"
FW_V = "1.2.0"


def fw_cfg(cfg):
    c = cfg.model_copy(deep=True)
    c.framework.artifacts = [FW]
    return c


def fw_profile(*evidence: FrameworkEvidence, scope: str = "jdk-jakarta"):
    p = make_profile()
    ev = list(evidence) or [FrameworkEvidence(source="direct", artifact=FW, version=FW_V, where="pom.xml")]
    p.framework = FrameworkUse(patterns=[FW], evidence=ev, scope=scope, reason="test")
    return p


def reduce(loop, *evidence: FrameworkEvidence):
    loop.profile = fw_profile(*evidence)
    loop._version_resolver = None
    return loop


def fw_pom(version: str = FW_V, parent: str = "") -> str:
    """A project pom that declares the framework artifact (and IO), optionally with a parent."""
    text = _pom([(FW, version), (IO, "2.11.0")])
    if parent:
        g, a, v = parent.split(":")
        text = text.replace("  <groupId>com.x</groupId>",
                            f"  <parent>\n    <groupId>{g}</groupId>\n    <artifactId>{a}</artifactId>\n"
                            f"    <version>{v}</version>\n  </parent>\n  <groupId>com.x</groupId>", 1)
    return text


# ------------------------------------------------------------------------------------------------ 1. version facts
def test_resolver_reduced_uses_the_asis_chain_only(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("sbmig.build.boot_bom.load_boot_bom_facts",
                        lambda c: SimpleNamespace(managed={YAML: "9.9", TEXT: "9.0.0"}))
    ws = _ws(tmp_path, {"pom.xml": _pom([])})
    _captures(ws, ASIS, AFTER)
    full = VersionResolver.for_workspace(ws, cfg=cfg)
    red = VersionResolver.for_workspace(ws, cfg=cfg, reduced=True)
    # full scope unchanged: the new chain (after) manages CORE, the target Boot BOM fills YAML
    assert full.resolve(CORE, "com.x:demo") == (None, "managed")
    assert full.resolve(YAML, "com.x:demo") == ("9.9", "boot-bom")
    # reduced: the kept parent (the as-is capture) manages TEXT → no version; IO → the as-is version; never the BOM
    assert red.after is red.asis and red.has_after and red.chain == {} and red.boot == {}
    assert red.resolve(TEXT, "com.x:demo") == (None, "managed")
    assert red.resolve(IO, "com.x:demo") == ("2.11.0", "asis")
    assert red.resolve(YAML, "com.x:demo") == (None, "unknown")
    assert red.resolve(CORE, "com.x:demo") == (None, "unknown")        # the company chain's key: not managed here
    assert red.boot_version(YAML, "com.x:demo") is None


def test_version_facts_say_parent_chain_in_a_reduced_run(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(TEXT, None)])})
    _captures(ws, ASIS, AFTER)
    c = ErrorCluster(id="case:x", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=[
        BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                   artifact=GAV(group_id="org.apache.commons", artifact_id="commons-text"),
                   signature_generic=f"dependency_resolution|{TEXT}")])
    full = make_loop(cfg, ws, FakeFixer(), pom_build())
    assert not any(line.startswith("parent chain:") for line in full._version_facts(c))
    red = reduce(make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build()))
    facts = red._version_facts(c)
    assert f"parent chain: manages {TEXT} at 1.10.0" in facts
    assert not any("new parent chain" in f or "target Boot BOM" in f for f in facts)


# ------------------------------------------------------------------------------------------------ 2. untouchable
def test_protected_keys_include_the_framework_only_in_a_reduced_run(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": fw_pom()})
    c = fw_cfg(cfg)
    loop = make_loop(c, ws, FakeFixer(), pom_build())
    loop.profile = fw_profile(scope="full")             # found, but the run is a full migration
    assert FW not in loop._protected_version_keys() and not loop._is_framework_key(FW)
    reduce(loop)
    assert FW in loop._protected_version_keys()
    assert loop._is_framework_key(FW) and loop._is_framework_key(f"{FW}:jar:{FW_V}")
    c.framework.artifacts = ["com.example.fw:example-fw-*"]      # a sibling module matched by the pattern
    assert loop._is_framework_key("com.example.fw:example-fw-web") and not loop._is_framework_key(TEXT)


def test_the_missing_version_pre_try_never_pins_the_framework(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": _pom([(FW, None)])})
    _captures(ws, {"direct": {FW: FW_V}, "by_module": {"com.x:demo": {"direct": {FW: FW_V}}}})
    loop = reduce(make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build()))
    from .test_v21l_versions import missing
    c = ErrorCluster(id="case:v", kind=ErrorKind.POM_INVALID, errors=[missing(FW)])
    plan, unknown, _new, _pairs = loop._version_plan(c)
    assert plan == [] and unknown == [f"{FW} (framework artifact: its version is never written by sbmig)"]


def _guard_run(cfg, tmp_path, first_edits, *, reduced=True):
    ws = _ws(tmp_path, {"pom.xml": fw_pom(parent="com.example.fw:example-fw-parent:1.0"), FOO: "class Foo { broken }\n"})
    a1 = answer(edits=[*first_edits, {"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])
    a2 = answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])
    fixer = FakeFixer(scripts=[a1, a2])
    loop = make_loop(fw_cfg(cfg), ws, fixer, pom_build(broken_file=FOO))
    if reduced:
        reduce(loop)
    pr = loop.run(Phase.VERIFY, "package", 4)
    return ws, fixer, loop, pr


def test_an_attempt_changing_the_framework_version_is_not_kept(cfg, tmp_path):
    bump = {"op": "pom", "path": "pom.xml",
            "pom": {"set_dependency_version": {"group_id": "com.example.fw", "artifact_id": "example-fw-core",
                                               "version": "2.0.0"}}}
    ws, fixer, loop, pr = _guard_run(cfg, tmp_path, [bump])
    assert pr.ok
    first, second = pr.attempts
    assert first.outcome == "apply_failed" and not first.applied
    [why] = [e for e in first.apply_errors if e.startswith(FRAMEWORK_GUARD_PREFIX)]
    assert f"changed the version of the framework artifact {FW} in pom.xml from {FW_V} to 2.0.0" in why
    assert "must stay exactly as they are" in why
    assert f"<version>{FW_V}</version>" in ws.read("pom.xml") and "2.0.0" not in ws.read("pom.xml")
    # the next ask sees the rejected attempt and its reason
    prev = fixer.contexts[1].previous_attempts
    assert any(any(e.startswith(FRAMEWORK_GUARD_PREFIX) for e in a.apply_errors) for a in prev)
    assert second.applied and second.outcome == "build_ok"           # an attempt not touching it is kept
    assert any(e["event"] == "framework_guard" for e in journal_events(ws))


def test_an_attempt_changing_a_parent_is_not_kept(cfg, tmp_path):
    new_pom = fw_pom(parent="com.example.fw:example-fw-parent:2.0")
    ws, _fixer, _loop, pr = _guard_run(cfg, tmp_path, [{"op": "write", "path": "pom.xml", "content": new_pom}])
    first = pr.attempts[0]
    assert first.outcome == "apply_failed"
    assert any("changed the <parent> of pom.xml from com.example.fw:example-fw-parent:1.0 to "
               "com.example.fw:example-fw-parent:2.0" in e for e in first.apply_errors)
    assert "<version>1.0</version>" in ws.read("pom.xml")


def test_the_guard_is_off_in_a_full_run(cfg, tmp_path):
    bump = {"op": "pom", "path": "pom.xml",
            "pom": {"set_dependency_version": {"group_id": "com.example.fw", "artifact_id": "example-fw-core",
                                               "version": "2.0.0"}}}
    ws, _fixer, _loop, pr = _guard_run(cfg, tmp_path, [bump], reduced=False)
    assert pr.ok and pr.attempts[0].applied and "<version>2.0.0</version>" in ws.read("pom.xml")


# ------------------------------------------------------------------------------------------------ 3. parents
def test_sbmig_parent_is_not_offered_in_a_reduced_run(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": fw_pom()})
    loop = make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build())
    assert SBMIG_PARENT_SOURCE in [t.source for t in loop._pre_tries()]
    reduce(loop)
    assert SBMIG_PARENT_SOURCE not in [t.source for t in loop._pre_tries()]


def test_a_foreign_parent_aborts_a_reduced_run_without_the_llm(cfg, ws):
    set_root(ws, pom(FOREIGN.rsplit(":", 1)[0], "9.9"))
    g = FOREIGN.rsplit(":", 1)[0]
    fixer = FakeFixer(fn=lambda *a: pytest.fail("the LLM must not be asked"),
                      script_fn=lambda *a, **k: pytest.fail("the LLM must not be asked"))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(parent_error(g, "9.9")))
    loop = blocked_loop(fw_cfg(cfg), ws, fw_profile(), build=build, fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert loop.aborted and not pr.attempts
    # the same sentence as the transform step's abort (Orchestrator._reduced_parent_abort): one Korean pattern covers both
    assert loop.aborted.startswith(f"Maven cannot resolve the parent pom {FOREIGN}; a framework project keeps its own "
                                   "parent pom, so the repository must serve it (settings: ")
    from sbmig.report.i18n import tr_line
    assert "프레임워크 프로젝트는 부모 pom을 바꾸지 않으므로" in tr_line("ko", loop.aborted)
    assert "aborted:parent-not-found" in loop.flags
    assert [b.reason for b in pr.blocked] == [BlockReason.PARENT_NOT_FOUND]
    assert "keeps its own parent" in pr.blocked[0].suggestion


def test_a_foreign_parent_does_not_abort_a_full_run(cfg, ws):
    set_root(ws, pom(FOREIGN.rsplit(":", 1)[0], "9.9"))
    g = FOREIGN.rsplit(":", 1)[0]
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(parent_error(g, "9.9")))
    loop = blocked_loop(cfg, ws, make_profile(), build=build)
    loop.run(Phase.VERIFY, "package", 4)
    assert not loop.aborted and "aborted:parent-not-found" not in loop.flags


# ------------------------------------------------------------------------------------------------ 4. javax libraries
def _javax_case() -> ErrorCluster:
    e = fill_signatures(BuildError(kind=ErrorKind.COMPILE, file=FOO, line=3, module="demo",
                                   message="incompatible types: jakarta.servlet.http.HttpServletRequest cannot be "
                                           "converted to javax.servlet.http.HttpServletRequest"))
    return ErrorCluster(id="case:j", kind=ErrorKind.COMPILE, errors=[e])


def test_a_javax_case_on_the_framework_artifact_is_blocked_without_the_llm(cfg, tmp_path, monkeypatch):
    from sbmig.triage.blocked import BlockedCandidate
    ws = _ws(tmp_path, {"pom.xml": fw_pom(), FOO: "class Foo { broken }\n"})
    c = _javax_case()
    fixer = FakeFixer(fn=lambda *a: pytest.fail("the LLM must not be asked"),
                      script_fn=lambda *a, **k: pytest.fail("the LLM must not be asked"))
    loop = make_loop(fw_cfg(cfg), ws, fixer, lambda w, call: bad_build(*c.errors))
    monkeypatch.setattr(loop, "_javax_cluster", lambda cl: BlockedCandidate(
        reason=BlockReason.EXTERNAL_JAVAX_API, errors=list(cl.errors),
        required_type="javax.servlet.http.HttpServletRequest"))
    monkeypatch.setattr(loop, "_case_libraries", lambda cl: [GAV.parse(f"{FW}:{FW_V}")])
    assert loop._framework_javax_block(c) is None                      # full scope: nothing
    reduce(loop)
    pr = loop.run(Phase.VERIFY, "package", 3)
    assert not pr.attempts and len(pr.blocked) == 1
    item = pr.blocked[0]
    assert item.reason == BlockReason.EXTERNAL_JAVAX_API and item.artifact.key == FW
    assert item.suggestion == framework_owner_suggestion(f"{FW}:{FW_V}", "javax.servlet.http.HttpServletRequest")
    assert f"the owner of the framework artifact `{FW}:{FW_V}` must provide a jakarta build" in item.suggestion


def test_the_jakarta_pre_try_never_swaps_the_framework(cfg, tmp_path, monkeypatch):
    ws = _ws(tmp_path, {"pom.xml": fw_pom()})
    loop = reduce(make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build()))
    build = SimpleNamespace(coordinates=f"{FW}:9.0.0-jakarta", key=FW, version="9.0.0-jakarta")
    monkeypatch.setattr(loop, "_javax_cluster", lambda cl: object())
    monkeypatch.setattr(loop, "_case_libraries", lambda cl: [GAV.parse(f"{FW}:{FW_V}")])
    monkeypatch.setattr(loop, "_discover", lambda k, v, rv=None: SimpleNamespace(best=lambda: build,
                                                                                  searched_text=lambda: "x"))
    plan = loop._jakarta_plan(_javax_case())
    assert plan["items"] == [] and any("framework artifact of this project: never swapped" in n for n in plan["notes"])


def test_the_final_pass_names_the_framework_a_javax_jar_comes_through():
    profile = fw_profile()
    cfg = SimpleNamespace(framework=SimpleNamespace(matches_key=lambda k: k == FW))
    through = BlockedItem(id="b1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.VERIFY, cluster_id="c1",
                          artifact=GAV.parse("javax.servlet:javax.servlet-api:4.0.1"), suggestion="old",
                          via=["com.x:demo:jar:1.0.0", f"{FW}:jar:{FW_V}", "javax.servlet:javax.servlet-api:jar:4.0.1"],
                          via_direct=GAV.parse(f"{FW}:{FW_V}"))
    other = BlockedItem(id="b2", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.VERIFY, cluster_id="c2", suggestion="keep",
                        via=["com.x:demo:jar:1.0.0", "com.acme:lib:jar:1", "javax.el:javax.el-api:jar:3.0.0"])
    assert framework_via_suggestions([through, other], make_profile(), cfg) == 0      # full run: nothing
    assert framework_via_suggestions([through, other], profile, cfg) == 1
    assert f"comes through the framework artifact `{FW}:jar:{FW_V}`" in through.suggestion
    assert "must provide a jakarta build" in through.suggestion and other.suggestion == "keep"


# ------------------------------------------------------------------------------------------------ 5. prompt facts
def test_scope_facts_name_every_kind_of_evidence(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": fw_pom()})
    loop = make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build())
    assert loop._scope_facts() == [] and loop._layout_facts() == []
    reduce(loop,
           FrameworkEvidence(source="direct", artifact=FW, version=FW_V, where="pom.xml"),
           FrameworkEvidence(source="parent", artifact=FW, version=FW_V, where="com.x:web",
                             parent="com.example.fw:example-fw-parent:1.0"),
           FrameworkEvidence(source="bundled", artifact="example-fw-core-1.2.0.jar", where="WEB-INF/lib/example-fw-core-1.2.0.jar"),
           FrameworkEvidence(source="transitive", artifact=FW, version=FW_V, where="com.x:svc",
                             via=["com.acme:lib:1", f"{FW}:{FW_V}"]))
    loop.restructure_results = [object()]                 # even with results handed over: no layout facts here
    facts = loop._scope_facts()
    assert loop._layout_facts() == []
    assert facts[0].startswith(f"this run: Java {cfg.target.java_version} and javax.* to jakarta.* only")
    tail = " — never change its version, never remove or replace it"
    assert f"framework artifact {FW} {FW_V} (direct: pom.xml){tail}" in facts
    assert f"framework artifact {FW} {FW_V} (parent: com.x:web){tail}" in facts
    assert ("framework artifact example-fw-core-1.2.0.jar (bundled: WEB-INF/lib/example-fw-core-1.2.0.jar)"
            f"{tail}") in facts
    assert f"framework artifact {FW} {FW_V} (transitive: com.x:svc via com.acme:lib:1 > {FW}:{FW_V}){tail}" in facts
    assert any(f.startswith("a fix may: ") for f in facts) and any(f.startswith("a fix must not: ") for f in facts)
    assert sum(len(f) + 3 for f in facts) <= SCOPE_FACTS_MAX_CHARS


def test_scope_facts_stay_under_the_cap_with_many_evidence_lines(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": fw_pom()})
    ev = [FrameworkEvidence(source="direct", artifact=FW, version=FW_V, where=f"module-{i}/pom.xml") for i in range(30)]
    loop = reduce(make_loop(fw_cfg(cfg), ws, FakeFixer(), pom_build()), *ev)
    facts = loop._scope_facts()
    assert sum(len(f) + 3 for f in facts) <= SCOPE_FACTS_MAX_CHARS
    assert any(f.startswith("… ") and "more framework evidence line(s)" in f for f in facts)


def test_the_fix_context_carries_the_scope(cfg, tmp_path):
    ws = _ws(tmp_path, {"pom.xml": fw_pom(), FOO: "class Foo { broken }\n"})
    fixer = FakeFixer(scripts=[answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])])
    loop = reduce(make_loop(fw_cfg(cfg), ws, fixer, pom_build(broken_file=FOO)))
    assert loop.run(Phase.VERIFY, "package", 3).ok
    [ctx] = fixer.contexts
    assert ctx.scope == "jdk-jakarta" and ctx.scope_facts == loop._scope_facts() and ctx.layout_facts == []
    ws2 = _ws(tmp_path / "full", {"pom.xml": fw_pom(), FOO: "class Foo { broken }\n"})
    fixer2 = FakeFixer(scripts=[answer(edits=[{"op": "write", "path": FOO, "content": "class Foo { ok }\n"}])])
    assert make_loop(cfg, ws2, fixer2, pom_build(broken_file=FOO)).run(Phase.VERIFY, "package", 3).ok
    assert fixer2.contexts[0].scope == "full" and fixer2.contexts[0].scope_facts == []


# ------------------------------------------------------------------------------------------------ guard: what it compares
def _fw_dep_block(extra: str = "") -> tuple[str, str]:
    old = (f"      <artifactId>example-fw-core</artifactId>\n      <version>{FW_V}</version>\n")
    return old, old + extra


def test_an_exclusion_under_the_framework_dependency_is_kept(cfg, tmp_path):
    old, new = _fw_dep_block("      <exclusions>\n        <exclusion>\n          <groupId>javax.servlet</groupId>\n"
                             "          <artifactId>javax.servlet-api</artifactId>\n        </exclusion>\n"
                             "      </exclusions>\n")
    ws, _f, _l, pr = _guard_run(cfg, tmp_path, [{"op": "replace", "path": "pom.xml", "old": old, "new": new}])
    assert pr.ok and pr.attempts[0].applied and "<exclusions>" in ws.read("pom.xml")


def test_a_scope_change_on_the_framework_dependency_is_kept(cfg, tmp_path):
    old, new = _fw_dep_block("      <scope>provided</scope>\n")
    ws, _f, _l, pr = _guard_run(cfg, tmp_path, [{"op": "replace", "path": "pom.xml", "old": old, "new": new}])
    assert pr.ok and pr.attempts[0].applied and "<scope>provided</scope>" in ws.read("pom.xml")


def test_removing_the_framework_declaration_is_not_kept(cfg, tmp_path):
    block = (f"    <dependency>\n      <groupId>com.example.fw</groupId>\n      <artifactId>example-fw-core</artifactId>\n"
             f"      <version>{FW_V}</version>\n    </dependency>\n")
    ws, _f, _l, pr = _guard_run(cfg, tmp_path, [{"op": "replace", "path": "pom.xml", "old": block, "new": ""}])
    first = pr.attempts[0]
    assert first.outcome == "apply_failed"
    assert any(f"removed the framework artifact {FW} from the <dependencies> of pom.xml" in e for e in first.apply_errors)
    assert "example-fw-core" in ws.read("pom.xml")
