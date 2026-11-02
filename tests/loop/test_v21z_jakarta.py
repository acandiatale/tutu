"""v2.1z-e jakarta build discovery in the fix loop (docs/INTERFACES.md "v2.1z-e"): a case whose library still takes
javax types is first tried WITHOUT the LLM — `sbmig-jakarta` swaps the declaration to the jakarta build sbmig verified
in the repository (the same coordinates' `2.0.5-jakarta` higher line, by its version qualifier) and the rebuild scores it;
the owner class → declared dependency fallback when the classpath cannot attribute it; a "no jakarta build" decline is
rejected when the repository has one; the blocked item carries what was searched; journal events tell the story."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from sbmig.build import parse_maven_log
from sbmig.core.models import (GAV, BlockReason, DecisionKind, LLMUsage, Phase, RepositoryVersions, ScriptProposal,
                               VersionCandidate)
from sbmig.core.workspace import Workspace
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.fixloop import JAKARTA_SEARCH_PREFIX, SBMIG_JAKARTA_SOURCE, UNMAPPED_PREFIX, VersionResolver
from sbmig.triage import Triager

from .fakes import FakeFixer, FakeBuildRunner, FakeKB, FakeScriptRunner, bad_build, journal_events, make_profile, ok_build

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'
FOO = "src/main/java/com/x/ExportServlet.java"
GRID = "com.example.grid8:grid8"
HDR = "[INFO] --- maven-compiler-plugin:3.13.0:compile (default-compile) @ demo ---\n[ERROR] COMPILATION ERROR : \n"
X1_BLOCK = (
    "[ERROR] {root}/" + FOO + ":[18,17] incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to "
    "javax.servlet.http.HttpServletRequest\n")
OWNER = "com.example.grid.GridExporter"


def pom(dep_version: Optional[str] = "1.0.6", group: str = "com.example.grid8", artifact: str = "grid8") -> str:
    ver = f"<version>{dep_version}</version>" if dep_version else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n'
            "  <groupId>com.x</groupId>\n  <artifactId>demo</artifactId>\n  <version>1.0.0</version>\n"
            "  <dependencies>\n"
            f"    <dependency>\n      <groupId>{group}</groupId>\n      <artifactId>{artifact}</artifactId>\n      {ver}\n"
            "    </dependency>\n  </dependencies>\n</project>\n")


SERVLET = """package com.x;

import java.io.IOException;

import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServlet;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

import com.example.grid.GridExporter;

public class ExportServlet extends HttpServlet {

    private final GridExporter exporter = new GridExporter();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        exporter.export(req, resp);
    }
}
"""


def _ws(tmp_path: Path, pom_text: str = pom()) -> Workspace:
    src = tmp_path / "src" / "demo"
    (src / "src/main/java/com/x").mkdir(parents=True)
    (src / "pom.xml").write_text(pom_text, encoding="utf-8")
    (src / FOO).write_text(SERVLET, encoding="utf-8")              # the call `exporter.export(req, resp)` is line 18
    w = Workspace.from_input(src, tmp_path / "out", run_id="t-run")
    w.git_init()
    return w


def javax_build(fixed=None):
    """The servlet hands a jakarta request to the javax grid API until the pom declares the jakarta build
    (`fixed(pom_text)` decides; default: `2.0.5-jakarta` is declared)."""
    fixed = fixed or (lambda text: "2.0.5-jakarta" in text)

    def fn(ws, call):
        if fixed(ws.read("pom.xml")):
            return ok_build()
        text = HDR + X1_BLOCK.format(root=ws.root) + "[INFO] BUILD FAILURE\n"
        return bad_build(*parse_maven_log(text, ws.root))
    return fn


class FakeLocator:
    """classpath attribution: the owner class comes from the grid jar."""

    def __init__(self, gav: str = f"{GRID}:1.0.6"):
        self.gav = GAV.parse(gav)
        self.asked: list = []

    def owner_fqcn(self, e):
        return OWNER

    def type_mismatch(self, e):
        return None, None

    def locate_class(self, ws, cfg, fqcn, module=None):
        self.asked.append(fqcn)
        return SimpleNamespace(artifact=self.gav, jar="/m2/grid8-1.0.6.jar", in_project=False, fqcn=fqcn)

    def locate_type_owner(self, ws, cfg, error, module=None):
        return SimpleNamespace(artifact=self.gav, jar="/m2/grid8-1.0.6.jar", in_project=False, fqcn=OWNER)


class VendorLookup:
    """The company repository: grid8 1.0.6 / 1.1.3 / 1.1.15 (javax poms) and 2.0.5-jakarta (silent pom)."""
    include_prerelease = False

    def __init__(self, versions=("1.0.6", "1.1.3", "1.1.15", "2.0.5-jakarta"), url="https://nexus.example/repository/maven-public/"):
        self.vers = list(versions)
        self.url = url
        self.asked: list = []
        self.settings_files = ["/home/x/.m2/settings.xml (~/.m2/settings.xml)"]
        self.url_notes: list[str] = []

    def repository_url(self):
        return self.url

    def repository_urls(self):
        return [(self.url, None, "settings mirror 'nexus' (mirrorOf *) covering central")]

    def versions(self, g, a):
        self.asked.append(("versions", f"{g}:{a}"))
        return list(self.vers) if (g, a) == ("com.example.grid8", "grid8") else []

    def pom_text(self, g, a, v):
        javax = "<dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId></dependency>"
        deps = "" if v.endswith("-jakarta") else javax
        return f'<project {NS}><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version><dependencies>{deps}</dependencies></project>'

    def lookup_trace(self, g, a):
        return [(self.url, f"ok ({len(self.vers)} release(s))")]

    def remote_failures(self, g, a):
        return ""

    def _fetch(self, url, credentials=None, base=None):
        return None

    def candidates(self, key, current):
        self.asked.append(("candidates", key, current))
        same = [VersionCandidate(version=v, jakarta=False) for v in self.vers if v.startswith("1.") and v != current]
        higher = [VersionCandidate(version=v, jakarta=None) for v in self.vers if v.startswith("2.")]
        return RepositoryVersions(key=key, current=current, major=1, same_major=same, higher=higher, source=self.url,
                                  urls_tried=[f"{self.url} → ok ({len(self.vers)} release(s))"])


def make_loop(cfg, ws, build, fixer=None, *, locator=None, lookup=None, profile=None) -> BuildFixLoop:
    loop = BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build), Triager(cfg), fixer or FakeFixer(), FakeKB(),
                        profile or make_profile(), budget=LoopBudget.from_config(cfg), script_runner=FakeScriptRunner(),
                        locator=locator)
    if lookup is not None:
        loop._repo_lookup = lookup                       # the run's repository lookup (conftest's default is offline)
    return loop


def subjects(ws) -> list[str]:
    return ws._git("log", "--format=%s").stdout.splitlines()[::-1]


# ---------------------------------------------------------------------------------------------------------------------
# the pre-try
# ---------------------------------------------------------------------------------------------------------------------
def test_the_jakarta_build_is_applied_before_any_llm_call_as_a_major_change(cfg, ws_grid, caplog):
    lookup = VendorLookup()
    fixer = FakeFixer()
    loop = make_loop(cfg, ws_grid, javax_build(), fixer, locator=FakeLocator(), lookup=lookup)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and fixer.script_calls == []                       # no LLM call at all
    [a] = pr.attempts
    assert a.source == SBMIG_JAKARTA_SOURCE and a.script_mode == "new" and a.outcome == "build_ok" and a.applied
    assert a.script_version == 0 and a.decision.kind == DecisionKind.SCRIPT
    assert "sbmig-jakarta: the repository has a jakarta build" in a.decision.rationale
    text = ws_grid.read("pom.xml")
    assert "<version>2.0.5-jakarta</version>" in text and "1.0.6" not in text
    assert any(s.startswith(f"fix({SBMIG_JAKARTA_SOURCE}): case:") for s in subjects(ws_grid))
    # a higher major: the read-back lists it for the operator with sbmig's synthesized guide (no LLM call for it)
    assert a.major_upgrades and a.major_upgrades[0].key == GRID and a.major_upgrades[0].after == "2.0.5-jakarta"
    assert a.verify_source == "sbmig" and a.verify
    assert any(n.startswith(f"jakarta build: {GRID} → {GRID}:2.0.5-jakarta in pom.xml (version qualifier; found by same") for n in a.notes)
    ev = journal_events(ws_grid)
    [jt] = [e for e in ev if e["event"] == "jakarta_try"]
    assert jt["plan"] == [{"pom": "pom.xml", "from": GRID, "to": f"{GRID}:2.0.5-jakarta", "version": "2.0.5-jakarta",
                           "managed_by": "", "source": "same", "how": "version qualifier", "major": True}]
    assert any(e["event"] == "major_upgrade" and e["guide_source"] == "sbmig" for e in ev)
    # `candidates` was asked once for the library in the whole run
    assert [x for x in lookup.asked if x[0] == "candidates"] == [("candidates", GRID, "1.0.6")]


def test_version_less_when_the_new_chain_manages_the_build(cfg, ws_grid):
    lookup = VendorLookup()
    loop = make_loop(cfg, ws_grid, javax_build(lambda text: "1.0.6" not in text), FakeFixer(), locator=FakeLocator(),
                     lookup=lookup)
    loop._version_resolver = VersionResolver(after={"managed": {GRID: "2.0.5-jakarta"}})
    pr = loop.run(Phase.VERIFY, "package", 4)
    [a] = pr.attempts
    assert a.source == SBMIG_JAKARTA_SOURCE and a.applied
    text = ws_grid.read("pom.xml")
    assert "<version>" not in text.split("<dependencies>")[1]        # managed by the chain: no version written
    [jt] = [e for e in journal_events(ws_grid) if e["event"] == "jakarta_try"]
    assert jt["plan"][0]["version"] is None and jt["plan"][0]["managed_by"] == "2.0.5-jakarta"


def test_the_owner_class_is_mapped_to_the_declared_dependency_without_a_classpath(cfg, ws_grid):
    """The company run: no classpath attribution → the package tokens of the owner class (`com.example.grid.…` vs
    `com.example.grid8:grid8`) name the dependency; the pre-try still applies."""
    lookup = VendorLookup()
    loop = make_loop(cfg, ws_grid, javax_build(), FakeFixer(), locator=None, lookup=lookup)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert pr.ok and pr.attempts[0].source == SBMIG_JAKARTA_SOURCE
    ev = journal_events(ws_grid)
    [m] = [e for e in ev if e["event"] == "library_mapped"]
    assert m["owner_class"] == OWNER and m["key"] == GRID and m["how"] == "package tokens / local jar"


def test_owner_dependency_fallbacks(cfg, ws_grid, tmp_path):
    loop = make_loop(cfg, ws_grid, javax_build(), FakeFixer(), lookup=VendorLookup())
    deps = loop._declared_dependencies()
    assert [(p, g.key) for p, g in deps] == [("pom.xml", GRID)]
    assert loop._owner_dependency(OWNER, deps).key == GRID                      # tokens: example, grid ~ grid8
    assert loop._owner_dependency("org.other.thing.Klass", deps) is None
    # the local repository's jar of a declared dependency holds the class → mapped (and the version taken from the jar dir)
    cfg.build.local_repo = str(tmp_path / "m2")
    d = tmp_path / "m2" / "org" / "vendor" / "widgets" / "3.1"
    d.mkdir(parents=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("net/foo/Widget.class", b"\xca\xfe")
    (d / "widgets-3.1.jar").write_bytes(buf.getvalue())
    deps2 = [("pom.xml", GAV(group_id="org.vendor", artifact_id="widgets", version="${w.version}"))]
    g = loop._owner_dependency("net.foo.Widget", deps2)
    assert g is not None and g.key == "org.vendor:widgets" and g.version == "3.1"


# ---------------------------------------------------------------------------------------------------------------------
# the LLM path when nothing can be applied: facts, declines, the blocked item
# ---------------------------------------------------------------------------------------------------------------------
def declining(reason: str = "the vendor grid library has no jakarta build; its owners must provide one"):
    return ScriptProposal(code="", analysis="javax grid API.", model="fake", blocked_reason=reason,
                          llm_usage=LLMUsage(model="fake", calls=1))


def test_a_no_jakarta_build_decline_is_rejected_when_the_repository_has_one(cfg, tmp_path):
    """The dependency is transitive (declared in no project pom): nothing to swap, so the LLM is asked — its decline
    "no jakarta build" contradicts the discovery and is not accepted as a decline."""
    ws = _ws(tmp_path, pom(dep_version="1.0", group="com.x", artifact="other"))
    lookup = VendorLookup()
    fixer = FakeFixer(scripts=[declining(), declining()])
    cfg.script.max_llm_tries_per_case = 2
    loop = make_loop(cfg, ws, javax_build(), fixer, locator=FakeLocator(), lookup=lookup)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok and len(fixer.script_calls) == 2                      # asked again instead of needs_owner at once
    first = pr.attempts[0]
    assert first.outcome == "apply_failed" and any(n.startswith("decline rejected: the repository has "
                                                                 f"{GRID}:2.0.5-jakarta") for n in first.notes)
    assert not any(n.startswith("declined:") for n in first.notes)
    ev = journal_events(ws)
    dr = [e for e in ev if e["event"] == "decline_rejected"][0]             # both declines were rejected
    assert dr["fact"].startswith(f"the repository has {GRID}:2.0.5-jakarta (version qualifier; found by same)")
    # the prompt carried the discovery line
    hints = fixer.script_calls[0][1].library_hints
    assert any(h.startswith(f"repository also has {GRID} 2.0.5-jakarta (jakarta ✓ — version qualifier") for h in hints)
    assert any(e["event"] == "library_lookup" and e["key"] == GRID and e["jakarta_builds"] == [f"{GRID}:2.0.5-jakarta"]
               and e["urls_tried"] and e["settings_files"] for e in ev)


def test_nothing_found_the_blocked_item_says_what_was_searched(cfg, ws_grid):
    lookup = VendorLookup(versions=("1.0.6", "1.1.15"))                      # javax releases only
    fixer = FakeFixer()                                                      # answers without a script → the policy blocks
    loop = make_loop(cfg, ws_grid, javax_build(), fixer, locator=FakeLocator(), lookup=lookup)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok and not any(a.source == SBMIG_JAKARTA_SOURCE for a in pr.attempts)
    [item] = loop.blocked.items()
    assert item.reason == BlockReason.EXTERNAL_JAVAX_API and item.artifact.key == GRID
    assert f"; {JAKARTA_SEARCH_PREFIX} {GRID}: " in item.suggestion and "none verified" in item.suggestion
    assert "same coordinates: 1 release(s) newer than 1.0.6 checked, none jakarta" in item.suggestion
    assert any(n.startswith(f"{JAKARTA_SEARCH_PREFIX} {GRID}:") for n in item.notes)
    hints = fixer.script_calls[0][1].library_hints
    assert any(h.startswith(f"no jakarta build of {GRID} was found in the repository (") for h in hints)


def test_an_owner_class_nobody_can_match_is_said_so(cfg, tmp_path):
    ws = _ws(tmp_path, pom(dep_version="1.0", group="com.x", artifact="other"))
    fixer = FakeFixer()
    loop = make_loop(cfg, ws, javax_build(), fixer, locator=None, lookup=VendorLookup())
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok
    [item] = loop.blocked.items()
    assert item.artifact is None and f"{UNMAPPED_PREFIX} {OWNER} could not be matched to a dependency" in item.suggestion
    ev = journal_events(ws)
    [um] = [e for e in ev if e["event"] == "library_unmapped"]
    assert um["owner_class"] == OWNER and um["declared"] == 1


@pytest.fixture()
def ws_grid(tmp_path) -> Workspace:
    return _ws(tmp_path)


def test_the_switch_off_keeps_the_v21d_path(cfg, ws_grid):
    """`libraries.discover_jakarta_builds: false` (the company's kill switch, and boot2-javax-lib's e2e pin): no
    `sbmig-jakarta` pre-try even though the repository holds 2.0.5-jakarta, the LLM's "no jakarta build" decline is
    accepted as in v2.1d, the blocked item records that the discovery was off."""
    cfg.libraries.discover_jakarta_builds = False
    lookup = VendorLookup()
    fixer = FakeFixer(scripts=[declining()])
    loop = make_loop(cfg, ws_grid, javax_build(), fixer, locator=FakeLocator(), lookup=lookup)
    pr = loop.run(Phase.VERIFY, "package", 4)
    assert not pr.ok and len(fixer.script_calls) == 1
    assert not any(a.source == SBMIG_JAKARTA_SOURCE for a in pr.attempts)
    assert "1.0.6" in ws_grid.read("pom.xml")
    [item] = loop.blocked.items()
    assert item.reason == BlockReason.EXTERNAL_JAVAX_API and item.artifact.key == GRID
    assert "discovery off (libraries.discover_jakarta_builds: false)" in item.suggestion
    ev = journal_events(ws_grid)
    assert not any(e["event"] in ("jakarta_try", "decline_rejected") for e in ev)
    assert any(e["event"] == "library_lookup" and e["key"] == GRID and e["jakarta_builds"] == [] for e in ev)
    hints = fixer.script_calls[0][1].library_hints
    assert not any(h.startswith("repository also has") or h.startswith("no jakarta build of") for h in hints)
