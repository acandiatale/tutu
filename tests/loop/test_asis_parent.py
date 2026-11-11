"""v2.1z-ag: an as-is version of the company parent that the repository does not have (docs/INTERFACES.md "v2.1z-ag").

Company run 2026-11-11: the project already inherited the in-house parent g:a at `1.0.0-SNAPSHOT`, which Nexus does not
serve; OpenRewrite (on the as-is poms) failed and the run aborted as "configured parent not resolvable" although the
configured version resolves. Case a (the configured version itself is missing) still aborts; case b (an as-is version)
is aligned to the configured version and the recipes run once more; case c (a foreign parent) goes on with a flag."""
from __future__ import annotations

import pytest

from sbmig.build.log_parser import parent_coordinates_in
from sbmig.cli import run_exit_code
from sbmig.core.config import ArtifactSpec
from sbmig.core.models import GAV, BlockReason, BuildError, ErrorKind, Phase, RewriteResult
from sbmig.restructure.asis_parent import align_asis_parent, classify_gav, classify_parent_misses

from .fakes import FakeBuildRunner, FakeFixer, bad_build, journal_events, make_profile, ok_build
from .test_blocked_flow import make_loop
from .test_orchestrator import make_orch

OLD = "1.0.0-SNAPSHOT"
FOREIGN = "com.acme:acme-parent:9.9"


def pom(parent: str, version: str, *, artifact: str = "demo", extra: str = "", eol: str = "\n") -> str:
    g, a = parent.split(":")
    text = (f'<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            f"  <modelVersion>4.0.0</modelVersion>\n  <parent>\n    <groupId>{g}</groupId>\n"
            f"    <artifactId>{a}</artifactId>\n    <version>{version}</version>\n  </parent>\n"
            f"  <artifactId>{artifact}</artifactId>\n{extra}</project>\n")
    return text.replace("\n", eol)


def miss_msg(coords: str, shape: int = 0) -> str:
    g, a, v = coords.split(":")
    pomc = f"{g}:{a}:pom:{v}"
    return [
        (f"Non-resolvable parent POM for com.x:demo:1: Could not find artifact {pomc} in nexus (https://nexus.example/repo) "
        "and 'parent.relativePath' points at wrong local POM @ line 5, column 11"),
        (f"Non-resolvable parent POM for com.x:demo:1: The following artifacts could not be resolved: {pomc} (absent): "
        f"Could not find artifact {pomc} in central (https://repo.maven.apache.org/maven2) and 'parent.relativePath' "
        "points at no local POM @ line 5, column 11"),
        f"Non-resolvable parent POM for com.x:demo:1: Failed to read artifact descriptor for {pomc}",
        (f"Non-resolvable parent POM for com.x:demo:1: Could not transfer artifact {pomc} from/to nexus "
        "(https://nexus.example/repo): status code: 502"),
    ][shape]


@pytest.fixture()
def parent(cfg):
    """The local e2e stand-in parent (spring-boot-starter-parent 4.1.0) with its g:a:v as `.coordinates`."""
    p = cfg.artifacts.parent
    return GAV(group_id=p.group_id, artifact_id=p.artifact_id, version=p.version)


def set_root(ws, text: str) -> None:
    ws.write("pom.xml", text)
    ws.commit("as-is pom")


# ------------------------------------------------------------------------------------------------ parsing / classes
@pytest.mark.parametrize("shape", [0, 1, 2, 3])
def test_parent_coordinates_in_every_maven_shape(shape):
    assert [g.coordinates for g in parent_coordinates_in("parent_resolution: " + miss_msg("com.c:base:1.0", shape))] \
        == ["com.c:base:1.0"]


def test_parent_coordinates_ignore_dependency_failures():
    assert parent_coordinates_in("Could not find artifact com.c:lib:jar:1.0 in nexus (https://n)") == []


def test_classification(cfg, ws, parent):
    assert classify_gav(GAV.parse(f"{parent.key}:{parent.version}"), cfg, ws).kind == "configured"
    m = classify_gav(GAV.parse(f"{parent.key}:{OLD}"), cfg, ws)
    assert m.kind == "asis-version" and m.target.coordinates == parent.coordinates and m.spec_name == "artifacts.parent"
    assert classify_gav(GAV.parse(FOREIGN), cfg, ws).kind == "foreign"
    cfg.artifacts.parent.aliases = ["com.old:old-parent"]
    assert classify_gav(GAV.parse(f"com.old:old-parent:{parent.version}"), cfg, ws).kind == "asis-version"


def test_versionless_text_is_classified_from_the_poms_on_disk(cfg, ws, parent):
    set_root(ws, pom(parent.key, OLD))
    m = classify_gav(GAV.parse(parent.key), cfg, ws)
    assert m.kind == "asis-version" and m.gav.version == OLD
    set_root(ws, pom(parent.key, parent.version))
    assert classify_gav(GAV.parse(parent.key), cfg, ws).kind == "configured"


# ------------------------------------------------------------------------------------------------ alignment
def test_align_every_pom_keeps_crlf_and_the_rest(cfg, ws, parent):
    ws.write("pom.xml", pom(parent.key, OLD, extra="  <packaging>pom</packaging>\n", eol="\r\n"))
    ws.write("a/pom.xml", pom(parent.key, OLD, artifact="a"))
    ws.write("b/pom.xml", pom("com.x:demo", "1", artifact="b"))       # the reactor parent: untouched
    ws.commit("multi")
    m = classify_parent_misses(miss_msg(f"{parent.key}:{OLD}"), cfg, ws)[0]
    r = align_asis_parent(ws, m)
    assert sorted(r.poms) == ["a/pom.xml", "pom.xml"] and not r.properties
    root = (ws.root / "pom.xml").read_bytes().decode()
    assert f"<version>{parent.version}</version>" in root and OLD not in root and "\r\n" in root
    assert root.replace(parent.version, OLD) == pom(parent.key, OLD, extra="  <packaging>pom</packaging>\n", eol="\r\n")
    assert "<relativePath" not in root                                  # nothing else written
    assert ws.read("b/pom.xml") == pom("com.x:demo", "1", artifact="b")


def test_property_version_defined_in_the_project_is_aligned_at_the_property(cfg, ws, parent):
    set_root(ws, pom(parent.key, "${base.version}", extra=f"  <properties>\n    <base.version>{OLD}</base.version>\n"
                                                           "  </properties>\n"))
    m = classify_parent_misses(miss_msg(f"{parent.key}:{OLD}"), cfg, ws)[0]
    r = align_asis_parent(ws, m)
    assert r.properties == {"pom.xml": "base.version"}
    text = ws.read("pom.xml")
    assert "<version>${base.version}</version>" in text and f"<base.version>{parent.version}</base.version>" in text


def test_property_used_elsewhere_or_defined_outside_gets_the_literal(cfg, ws, parent):
    set_root(ws, pom(parent.key, "${base.version}", extra=f"  <properties>\n    <base.version>{OLD}</base.version>\n"
                     "  </properties>\n  <description>${base.version}</description>\n"))
    m = classify_parent_misses(miss_msg(f"{parent.key}:{OLD}"), cfg, ws)[0]
    r = align_asis_parent(ws, m)
    text = ws.read("pom.xml")
    assert not r.properties and f"<version>{parent.version}</version>" in text and f"<base.version>{OLD}<" in text
    # ${revision} from .mvn/maven.config (outside the poms): Maven names the resolved version, the pom holds the property
    set_root(ws, pom(parent.key, "${revision}"))
    [m] = classify_parent_misses(miss_msg(f"{parent.key}:{OLD}"), cfg, ws)
    assert m.kind == "asis-version" and align_asis_parent(ws, m).poms == ["pom.xml"]
    assert f"<version>{parent.version}</version>" in ws.read("pom.xml")


# ------------------------------------------------------------------------------------------------ orchestrator
class ParentRewrite:
    """Fails like Maven while a pom still names an unresolvable parent; `missing` = coordinates the repository lacks."""

    def __init__(self, ws, missing: set[str]):
        self.ws, self.missing, self.runs = ws, missing, 0

    def run(self, recipes, *, extra_recipe_files=(), module=None, dry_run=False, label="", **kw):
        self.runs += 1
        ids = [r.id for r in recipes]
        for coords in sorted(self.missing):
            _g, a, v = coords.split(":")
            if f"<artifactId>{a}</artifactId>\n    <version>{v}</version>" in self.ws.read("pom.xml"):
                return RewriteResult(ok=False, recipes=ids, error="parent_resolution: " + miss_msg(coords, 1))
        return RewriteResult(ok=True, recipes=ids)


def test_case_b_aligns_retries_once_and_builds(cfg, ws, parent):
    set_root(ws, pom(parent.key, OLD))
    rw = ParentRewrite(ws, {f"{parent.key}:{OLD}"})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 2
    assert report.success and run_exit_code(report) == 0
    assert "asis-parent-aligned" in report.flags and "aborted:parent-not-found" not in report.flags
    assert not any(f.startswith("infra:") for f in report.flags) and not report.blocked
    [ev] = [e for e in journal_events(ws) if e["event"] == "asis_parent_aligned"]
    assert (ev["parent"], ev["from"], ev["to"], ev["poms"]) == (parent.key, OLD, parent.version, ["pom.xml"])
    assert f"transform: as-is parent {parent.key} {OLD} → {parent.version} (as-is version not in the repository)" \
        in ws._git("log", "--format=%s").stdout
    assert [f for f in report.manual_followups if f.startswith(f"as-is parent `{parent.key}:{OLD}`")] and \
        any("target parent's classpath" in f for f in report.manual_followups)
    assert any("asis-parent-aligned" in n for n in report.phases[0].notes)


def test_case_b_retry_failing_on_the_configured_version_aborts(cfg, ws, parent):
    set_root(ws, pom(parent.key, OLD))
    rw = ParentRewrite(ws, {f"{parent.key}:{OLD}", parent.coordinates})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 2 and "asis-parent-aligned" in report.flags
    assert "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    assert report.blocked[0].artifact.coordinates == parent.coordinates


def test_case_a_aborts_and_names_the_configured_version(cfg, ws, parent):
    set_root(ws, pom(parent.key, parent.version))
    rw = ParentRewrite(ws, {parent.coordinates})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 1 and "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    [fu] = [f for f in report.manual_followups if f.startswith("run aborted")]
    assert f"Maven cannot resolve the parent {parent.coordinates}, which is the configured version (artifacts.parent = " \
           f"{parent.coordinates})" in fu and "settings:" in fu


def test_case_c_foreign_parent_goes_on_with_a_flag(cfg, ws):
    set_root(ws, pom(FOREIGN.rsplit(":", 1)[0], "9.9"))
    rw = ParentRewrite(ws, {FOREIGN})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 1 and "aborted:parent-not-found" not in report.flags and report.success
    assert "rewrite-skipped-asis-parent" in report.flags and "asis-parent-aligned" not in report.flags
    assert any(f.startswith(f"OpenRewrite did not run: the as-is parent `{FOREIGN}`") for f in report.manual_followups)
    [ev] = [e for e in journal_events(ws) if e["event"] == "rewrite_skipped_asis_parent"]
    assert ev["parent"] == FOREIGN and ev["company_parent"] is False


def test_switch_continue_leaves_the_poms_and_goes_on(cfg, ws, parent):
    cfg.openrewrite.unresolvable_asis_parent = "continue"
    set_root(ws, pom(parent.key, OLD))
    rw = ParentRewrite(ws, {f"{parent.key}:{OLD}"})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 1 and report.success and "rewrite-skipped-asis-parent" in report.flags
    assert "asis-parent-aligned" not in report.flags and f"<version>{OLD}</version>" in ws.read("pom.xml")


def test_switch_abort_is_the_old_behaviour_with_a_plain_message(cfg, ws, parent):
    cfg.openrewrite.unresolvable_asis_parent = "abort"
    set_root(ws, pom(parent.key, OLD))
    rw = ParentRewrite(ws, {f"{parent.key}:{OLD}"})
    report = make_orch(cfg, ws, rewrite=rw, build=FakeBuildRunner(ws, fn=lambda w, c: ok_build())).run()
    assert rw.runs == 1 and "aborted:parent-not-found" in report.flags and run_exit_code(report) == 3
    [fu] = [f for f in report.manual_followups if f.startswith("run aborted")]
    assert f"the as-is parent {parent.key}:{OLD}; the configured version is {parent.coordinates}" in fu
    assert "unresolvable_asis_parent is abort" in fu


# ------------------------------------------------------------------------------------------------ build loop pre-try
def parent_error(parent_key: str, version: str) -> BuildError:
    from sbmig.build.signatures import fill_signatures
    e = BuildError(kind=ErrorKind.PARENT_RESOLUTION, file="pom.xml", module="demo",
                   message=miss_msg(f"{parent_key}:{version}"), raw=miss_msg(f"{parent_key}:{version}"),
                   artifact=GAV.parse(f"{parent_key}:{version}"))
    fill_signatures(e)
    return e


def test_build_loop_pre_try_aligns_without_an_llm_call(cfg, ws, parent):
    set_root(ws, pom(parent.key, OLD))

    def build_fn(w, call):
        return bad_build(parent_error(parent.key, OLD)) if f"<version>{OLD}</version>" in w.read("pom.xml") else ok_build()

    fixer = FakeFixer(fn=lambda *a: pytest.fail("the LLM must not be asked"),
                      script_fn=lambda *a, **k: pytest.fail("the LLM must not be asked"))
    loop = make_loop(cfg, ws, make_profile(), build=FakeBuildRunner(ws, fn=build_fn), fixer=fixer)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and not loop.aborted and not pr.blocked
    [att] = pr.attempts
    assert att.source == "sbmig-parent" and att.applied and att.llm_usage.calls == 0
    assert f"<version>{parent.version}</version>" in ws.read("pom.xml")
    assert "asis-parent-aligned" in loop.flags
    assert any(e["event"] == "asis_parent_aligned" and e["where"] == "fix loop" for e in journal_events(ws))


def test_build_loop_configured_version_still_aborts(cfg, ws, parent):
    set_root(ws, pom(parent.key, parent.version))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(parent_error(parent.key, parent.version)))
    loop = make_loop(cfg, ws, make_profile(), build=build)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert loop.aborted and "which is the configured version" in loop.aborted and not pr.attempts
    assert [b.reason for b in pr.blocked] == [BlockReason.PARENT_NOT_FOUND]


def test_build_loop_switch_continue_blocks_without_abort(cfg, ws, parent):
    cfg.openrewrite.unresolvable_asis_parent = "continue"
    set_root(ws, pom(parent.key, OLD))
    build = FakeBuildRunner(ws, fn=lambda w, c: bad_build(parent_error(parent.key, OLD)))
    loop = make_loop(cfg, ws, make_profile(), build=build)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not loop.aborted and not pr.attempts
    assert [b.reason for b in pr.blocked] == [BlockReason.PARENT_NOT_FOUND]


def test_role_parent_versions_are_compared_per_spec(cfg, ws, parent):
    cfg.artifacts.boot_parent = ArtifactSpec(group_id="com.c", artifact_id="boot-jar", version="4.0.0")
    cfg.artifacts.comp_parent = ArtifactSpec(group_id="com.c", artifact_id="comp-jar", version="4.0.0")
    assert classify_gav(GAV.parse("com.c:boot-jar:4.0.0"), cfg, ws).kind == "configured"
    m = classify_gav(GAV.parse(f"com.c:comp-jar:{OLD}"), cfg, ws)
    assert m.kind == "asis-version" and m.spec_name == "artifacts.comp_parent" and m.target.version == "4.0.0"
