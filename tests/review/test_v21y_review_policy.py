"""v2.1y (prompt review 2026-10-31): the review's policy after the answer — issues that name a version / property the
material does not contain (E-M5: the four stale replays), the invariant re-check after the edits (D-M4) and the source
encoding check under role parents, re-computed after the edits (D-M7)."""
from __future__ import annotations

import pytest

from sbmig.build import parent_chain
from sbmig.core.models import ReviewIssue
from sbmig.review import StructureReviewer
from sbmig.review.inputs import invariant_violations, raw_poms
from sbmig.review.parse import UNSUPPORTED_MARK, mark_unsupported, unsupported_tokens

from .test_review_edits import answer
from .test_reviewer import COMP_POM, ROOT_POM, FakeLLM, make_ws, profile

# the four stale replays of the 2026-10-30 e2e (report E-M5) and their material
STALE = [("module/comp-jar/pom.xml", "jakarta.servlet-api:5.0.0 is pinned with scope compile"),
         ("shop-back/pom.xml", "property spring.version=5.3.31 is left in shop-back although Spring is managed"),
         ("pom.xml", "property spring.version=3.2.18.RELEASE is left although the parent manages Spring")]
MATERIAL = """# Project
- as-is (before the migration): java 1.8, spring 5.3.31 / 3.2.18.RELEASE
# What the restructure did
  - version properties removed (only the dropped explicit versions used them):
    - pom.xml: spring.version=7.0.10
# Facts sbmig collected without Maven
- jakarta.* API dependencies declared: `jakarta.servlet:jakarta.servlet-api` (compile) in `module/comp-jar/pom.xml`
- optional `org.slf4j:slf4j-api` (configured 2.0.17)
```xml
<artifactId>ibatis-sqlmap</artifactId><version>2.3.4.726</version>
```
"""
STATE = MATERIAL.replace("    - pom.xml: spring.version=7.0.10\n", "")


@pytest.mark.parametrize("file,problem", STALE)
def test_stale_statements_are_unsupported(file, problem):
    issue = ReviewIssue(severity="warning", file=file, problem=problem, fix_hint="remove it")
    assert unsupported_tokens(issue, MATERIAL, STATE)
    marked, missing = mark_unsupported([issue], MATERIAL, STATE)
    assert marked[0].unsupported and marked[0].problem == UNSUPPORTED_MARK + problem and marked[0].severity == "warning"
    assert missing and all(m in ("5.0.0", "spring.version") for m in missing)


@pytest.mark.parametrize("problem", [
    "org.apache.ibatis:ibatis-sqlmap:2.3.4.726 (iBATIS 2, EOL) is still declared",
    "slf4j-api is pinned at 2.0.17 although the parent manages slf4j",
    "jakarta.servlet-api has scope compile",
    "Spring Boot 4.1 manages Hystrix no more (Spring Cloud 2025.1)",          # no artifact / version word next to it
    "use ${project.version} for the module dependency",                       # Maven built-in
    "jakarta.servlet-api 6.1 is fine"])                     # 6.1 is the start of the material's 6.1.0
def test_supported_statements_are_kept(problem):
    material = MATERIAL + "\n- managed jakarta.servlet:jakarta.servlet-api 6.1.0\n"
    marked, missing = mark_unsupported([ReviewIssue(problem=problem)], material, material)
    assert not marked[0].unsupported and missing == [] and marked[0].problem == problem


def test_sbmig_own_issues_are_never_marked():
    own = ReviewIssue(severity="critical", problem="(sbmig check) MS949 Java sources in `x` build with UTF-8 9.9.9")
    marked, missing = mark_unsupported([own], "", "")
    assert marked == [own] and missing == []


UNSUPPORTED_CRITICAL = {"verdict": "issues", "issues": [
    {"severity": "critical", "file": "module/comp-jar/pom.xml", "problem": "jakarta.servlet-api:5.0.0 is pinned",
     "fix_hint": "set the version 5.0.1"}], "analysis": "x"}
PIN = [{"op": "pom", "path": "module/comp-jar/pom.xml",
        "add_dependency": {"group_id": "jakarta.servlet", "artifact_id": "jakarta.servlet-api", "version": "5.0.1"}}]


def test_unsupported_critical_is_reported_not_applied(tmp_path, cfg):
    ws = make_ws(tmp_path)
    llm = FakeLLM(answer(edits=PIN, script="", review=UNSUPPORTED_CRITICAL))
    rv = StructureReviewer(cfg, llm).review(ws, profile(), cfg, run_id="run-7")
    assert len(llm.calls) == 1 and not ws.is_dirty() and rv.script_result is None
    (i,) = rv.issues
    assert i.severity == "critical" and i.unsupported and i.problem.startswith(UNSUPPORTED_MARK)
    assert rv.note == "edits not applied: every critical issue names something not in the material"


# ---------------------------------------------------------------------------- D-M4 invariants
FACTS = parent_chain.ParentChainFacts(
    roots=["org.springframework.boot:spring-boot-starter-parent:4.1.0"],
    managed={"org.springframework:spring-context": "7.0.8"},
    properties={"acme.version": "4.0.0", "project.build.sourceEncoding": "UTF-8"},
    company_properties={"acme.version": "4.0.0", "project.build.sourceEncoding": "UTF-8"},
    company_property_sources={"acme.version": "com.acme:base-parent:4.0.0",
                              "project.build.sourceEncoding": "com.acme:base-parent:4.0.0"}, poms=1)
ENCODING = [{"op": "pom", "path": "module/comp-jar/pom.xml", "desc": "d",
             "set_property": {"name": "project.build.sourceEncoding", "value": "MS949"}}]
SHADOW = ENCODING + [{"op": "pom", "path": "module/comp-jar/pom.xml", "desc": "d",
                      "set_property": {"name": "acme.version", "value": "4.0.0"}}]


@pytest.fixture()
def chain(monkeypatch):
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda c: FACTS)
    return FACTS


def test_a_violation_is_restored_and_asked_once_more(tmp_path, cfg, chain):
    ws = make_ws(tmp_path)
    llm = FakeLLM(answer(edits=SHADOW, script=""), answer(edits=ENCODING, script=""))
    rv = StructureReviewer(cfg, llm).review(ws, profile(), cfg, run_id="run-7")
    assert len(llm.calls) == 2
    retry = llm.calls[1]["messages"][-1].content
    assert retry.startswith("Your edits could not be applied: review edits broke a restructure rule: "
                            "`module/comp-jar/pom.xml`: property acme.version=4.0.0 written — the parent chain defines "
                            "acme.version")
    assert "project.build.sourceEncoding" not in retry.split("Your edits could not be applied:")[1].split(" — restored")[0]
    assert rv.script_result.status == "ok" and not rv.reverted and ws.is_dirty()
    pom = ws.read("module/comp-jar/pom.xml")
    assert "<project.build.sourceEncoding>MS949</project.build.sourceEncoding>" in pom and "acme.version" not in pom
    assert "(second try; first: review edits broke a restructure rule" in rv.note


def test_a_second_violation_is_handed_over(tmp_path, cfg, chain):
    ws = make_ws(tmp_path)
    llm = FakeLLM(answer(edits=SHADOW, script=""), answer(edits=SHADOW, script=""))
    rv = StructureReviewer(cfg, llm).review(ws, profile(), cfg, run_id="run-7")
    assert len(llm.calls) == 2 and rv.reverted and not ws.is_dirty() and ws.read("module/comp-jar/pom.xml") == COMP_POM
    assert rv.critical and "broke a restructure rule" in rv.note


def test_invariant_check_can_be_turned_off(tmp_path, cfg, chain):
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"invariant_check": False})})
    ws = make_ws(tmp_path)
    rv = StructureReviewer(off, FakeLLM(answer(edits=SHADOW, script=""))).review(ws, profile(), off, run_id="run-7")
    assert rv.script_result.status == "ok" and "acme.version" in ws.read("module/comp-jar/pom.xml")


def _violations(tmp_path, cfg, before_root: str, after_root: str, path: str = "pom.xml"):
    from sbmig.analyze.pom_parser import parse_poms

    ws = make_ws(tmp_path)
    ws.write(path, before_root)
    bpoms, _ = parse_poms(ws)
    before = raw_poms(ws, bpoms)
    ws.write(path, after_root)
    apoms, _ = parse_poms(ws)
    return invariant_violations(cfg, [path], bpoms, before, apoms, raw_poms(ws, apoms), FACTS, [])


DEP = ("<dependencies><dependency><groupId>org.springframework</groupId><artifactId>spring-context</artifactId>"
       "{v}</dependency><dependency><groupId>org.springframework.boot</groupId>"
       "<artifactId>spring-boot-starter-validation</artifactId></dependency></dependencies>")


def test_invariant_rules(tmp_path, cfg):
    base = ROOT_POM.replace("</modules>", "</modules>" + DEP.format(v=""))
    # (b) an explicit version on a chain-managed artifact that had none
    out = _violations(tmp_path / "b", cfg, base, base.replace(
        "<artifactId>spring-context</artifactId>", "<artifactId>spring-context</artifactId><version>6.2.1</version>"))
    assert out == [("`pom.xml`: explicit version 6.2.1 on org.springframework:spring-context (dependencies) — the "
                    "parent chain (`org.springframework.boot:spring-boot-starter-parent:4.1.0`) manages it at 7.0.8; "
                    "leave the version out")]
    # an explicit version that was there before is not the review's doing
    pinned = base.replace("<artifactId>spring-context</artifactId>",
                          "<artifactId>spring-context</artifactId><version>6.2.1</version>")
    assert _violations(tmp_path / "b2", cfg, pinned, pinned.replace("<packaging>pom", "<name>x</name><packaging>pom")) == []
    # (c) a parent that is neither configured nor in the project
    out = _violations(tmp_path / "c", cfg, base, base.replace(
        "<groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>",
        "<groupId>com.evil</groupId><artifactId>parent</artifactId>"))
    assert out == [("`pom.xml`: parent com.evil:parent:4.1.0 is neither a configured company parent nor a pom of "
                    "the project")]
    # (d) a mandatory dependency removed
    out = _violations(tmp_path / "d", cfg, base, base.replace(
        "<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-validation</artifactId>"
        "</dependency>", ""))
    assert out == ["`pom.xml`: mandatory dependency org.springframework.boot:spring-boot-starter-validation removed"]
    # (a) a company *.version property — but never an encoding property the chain also defines
    out = _violations(tmp_path / "a", cfg, base, base.replace(
        "<packaging>pom</packaging>", "<packaging>pom</packaging><properties><acme.version>4.0.1</acme.version>"
        "<project.build.sourceEncoding>MS949</project.build.sourceEncoding></properties>"))
    assert len(out) == 1 and out[0].startswith("`pom.xml`: property acme.version=4.0.1 written")


# ---------------------------------------------------------------------------- D-M7 encodings
ROLE_COMP = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
    <version>4.1.0</version><relativePath/></parent>
  <groupId>com.x</groupId><artifactId>demo-comp</artifactId><version>1.0</version>
</project>
"""
KOREAN = "package a;\nclass A { String s = \"한글 인사\"; }\n".encode("cp949")


def _encoding_answer(file: str, edits: list) -> str:
    review = {"verdict": "issues", "issues": [{"severity": "critical", "file": file,
                                               "problem": "MS949 sources but no source encoding",
                                               "fix_hint": "set project.build.sourceEncoding"}], "analysis": "x"}
    return answer(edits=edits, script="", review=review)


def test_root_issue_does_not_cover_a_module_with_its_own_parent(tmp_path, cfg):
    ws = make_ws(tmp_path, java=KOREAN, comp_pom=ROLE_COMP)
    on_root = [{"op": "pom", "path": "pom.xml", "desc": "d",
                "set_property": {"name": "project.build.sourceEncoding", "value": "MS949"}}]
    rv = StructureReviewer(cfg, FakeLLM(_encoding_answer("pom.xml", on_root))).review(ws, profile(), cfg,
                                                                                       run_id="run-7")
    own = [i for i in rv.issues if i.problem.startswith("(sbmig check)")]
    # the root aggregator is not in the module's chain: sbmig's own issue is added, and stays after the edits
    assert len(own) == 1 and own[0].file == "module/comp-jar/pom.xml" and ws.is_dirty()
    assert "MS949" in ws.read("pom.xml")


def test_fixed_module_no_longer_counts_after_the_edits(tmp_path, cfg):
    ws = make_ws(tmp_path, java=KOREAN, comp_pom=ROLE_COMP)
    on_module = [{"op": "replace", "path": "module/comp-jar/pom.xml", "desc": "d",
                  "old": "<version>1.0</version>\n</project>",
                  "new": "<version>1.0</version>\n  <properties><project.build.sourceEncoding>MS949"
                         "</project.build.sourceEncoding></properties>\n</project>"}]
    # the LLM names the encoding on the root (not the module's chain) but its edit fixes the module pom
    rv = StructureReviewer(cfg, FakeLLM(_encoding_answer("pom.xml", on_module))).review(ws, profile(), cfg,
                                                                                         run_id="run-7")
    assert rv.script_result.status == "ok" and ws.is_dirty()
    assert not [i for i in rv.issues if i.problem.startswith("(sbmig check)")]       # re-computed: fixed


def test_material_includes_the_poms_cut_for_the_prompt(tmp_path, cfg):
    pad = "".join(f"  <!-- padding line {n:04d} to push the property past the prompt budget -->\n" for n in range(80))
    comp = COMP_POM.replace("<artifactId>demo-comp</artifactId>",
                            "<artifactId>demo-comp</artifactId>\n" + pad
                            + "  <properties><legacy-lib.version>1.2.3</legacy-lib.version></properties>\n" + pad)
    ws = make_ws(tmp_path, comp_pom=comp)
    small = cfg.model_copy(update={"review": cfg.review.model_copy(update={"max_pom_chars": 1500})})
    left = {"verdict": "issues", "issues": [
        {"severity": "warning", "file": "module/comp-jar/pom.xml", "problem": "property legacy-lib.version=1.2.3 is left",
         "fix_hint": "remove it"},
        {"severity": "warning", "file": "module/comp-jar/pom.xml", "problem": "property ghost-lib.version=9.8.7 is left",
         "fix_hint": "remove it"}], "analysis": "x"}
    llm = FakeLLM(answer(edits=None, script="", review=left))
    rv = StructureReviewer(small, llm).review(ws, profile(), small, run_id="run-7")
    user = llm.calls[0]["messages"][1].content
    assert "legacy-lib.version" not in user and "characters, cut)" in user          # the prompt did not show it …
    real, ghost = rv.issues
    assert not real.unsupported                                                     # … but the pom has it
    assert ghost.unsupported and ghost.problem.startswith(UNSUPPORTED_MARK)
