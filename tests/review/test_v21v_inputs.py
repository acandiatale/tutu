"""v2.1v / v2.1u (prompt review 2026-10-31, report D): what the structure review shows the LLM — the version facts read
from the parent chain's poms (D-H1), the Spring Boot line from the restructure's effective decision (D-H2, never the
target literal as a rule), the MSA rule as the code applies it (D-H3), the executable WAR (D-H4), the mandatory specs'
`<dependencies>` / `<dependencyManagement>` (D-H5), the new `RestructureResult` facts without the repeated notes (D-M3),
the optional / starter / final-decision rules (D-M5 / D-M6 / D-M8), container APIs on `compile` (D-M9), a repackaged
module other modules depend on (D-M10), the edits-only last line (D-M1) and `review.save_input` (D-L5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.build import boot_bom, parent_chain
from sbmig.core.config import ArtifactSpec
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import CaseType, ProjectProfile, ServiceInfo, SourceStats
from sbmig.core.workspace import Workspace
from sbmig.review import StructureReviewer, collect_review_input
from sbmig.review.reviewer import REVIEW_INPUT_FILE

from .test_reviewer import OK, FakeLLM

BASE = "com.acme:base-parent:4.0.0"
NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(body: str) -> str:
    return f"<project {NS}>\n  <modelVersion>4.0.0</modelVersion>\n{body}\n</project>\n"


ROOT = _pom("""  <parent><groupId>com.acme</groupId><artifactId>base-parent</artifactId><version>4.0.0</version>
    <relativePath/></parent>
  <groupId>com.example</groupId><artifactId>shop</artifactId><version>1.0</version><packaging>pom</packaging>
  <modules><module>shop-back</module><module>shop-boot</module><module>shop-lib</module></modules>
  <dependencies><dependency><groupId>com.acme</groupId><artifactId>acme-core</artifactId></dependency></dependencies>
  <dependencyManagement><dependencies><dependency><groupId>com.acme</groupId><artifactId>acme-bom</artifactId>
    <version>${acme-bom.version}</version><type>pom</type><scope>import</scope></dependency></dependencies>
  </dependencyManagement>""")
BACK = _pom("""  <parent><groupId>com.acme</groupId><artifactId>comp-jar</artifactId><version>4.0.0</version>
    <relativePath/></parent>
  <groupId>com.example</groupId><artifactId>shop-back</artifactId><version>1.0</version>
  <properties>
    <spring-boot.version>2.5.15</spring-boot.version>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-context</artifactId>
      <version>6.2.1</version></dependency>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-webmvc</artifactId>
      <version>7.0.10</version></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId>
      <version>2.7.18</version></dependency>
    <dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId>
      <scope>compile</scope></dependency>
    <dependency><groupId>com.acme</groupId><artifactId>acme-jpa</artifactId><version>${acme-jpa.version}</version>
    </dependency>
    <dependency><groupId>${project.groupId}</groupId><artifactId>shop-lib</artifactId>
      <version>${project.version}</version></dependency>
  </dependencies>""")
BOOT = _pom("""  <parent><groupId>com.acme</groupId><artifactId>boot-jar</artifactId><version>4.0.0</version>
    <relativePath/></parent>
  <groupId>com.example</groupId><artifactId>shop-boot</artifactId><version>1.0</version>
  <dependencies><dependency><groupId>com.example</groupId><artifactId>shop-back</artifactId><version>1.0</version>
  </dependency></dependencies>
  <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId>
  </plugin></plugins></build>""")
LIB = _pom("""  <parent><groupId>com.acme</groupId><artifactId>comp-jar</artifactId><version>4.0.0</version>
    <relativePath/></parent>
  <groupId>com.example</groupId><artifactId>shop-lib</artifactId><version>1.0</version>
  <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId>
    <executions><execution><goals><goal>repackage</goal></goals></execution></executions>
  </plugin></plugins></build>""")

FACTS = parent_chain.ParentChainFacts(
    roots=[BASE, "com.acme:boot-jar:4.0.0", "com.acme:comp-jar:4.0.0"],
    managed={"org.springframework:spring-context": "7.0.8", "org.springframework:spring-webmvc": "7.0.8",
             "org.springframework.boot:spring-boot-starter-web": "4.1.0",
             "jakarta.servlet:jakarta.servlet-api": "6.1.0"},
    properties={"spring-boot.version": "4.1.0", "acme-jpa.version": "4.0.0", "acme-bom.version": "4.0.0",
                "project.build.sourceEncoding": "UTF-8"},
    company_properties={"spring-boot.version": "4.1.0", "acme-jpa.version": "4.0.0", "acme-bom.version": "4.0.0",
                        "project.build.sourceEncoding": "UTF-8"},
    company_property_sources={k: BASE for k in ("spring-boot.version", "acme-jpa.version", "acme-bom.version",
                                                "project.build.sourceEncoding")},
    poms=3, reaches_boot_bom=True)


@pytest.fixture()
def company(cfg):
    """The local-e2e config with company-like role parents, a BOM-style mandatory spec and a triggered optional."""
    a = cfg.artifacts.model_copy(update={
        "parent": ArtifactSpec(group_id="com.acme", artifact_id="base-parent", version="4.0.0"),
        "boot_parent": ArtifactSpec(group_id="com.acme", artifact_id="boot-jar", version="4.0.0"),
        "comp_parent": ArtifactSpec(group_id="com.acme", artifact_id="comp-jar", version="4.0.0"),
        "mandatory": [ArtifactSpec(group_id="com.acme", artifact_id="acme-core"),
                      ArtifactSpec(group_id="com.acme", artifact_id="acme-bom", type="pom", scope="import")],
        "optional": [ArtifactSpec(group_id="com.acme", artifact_id="acme-jpa", property_name="acme-jpa.version",
                                  when_present=["org.hibernate*:hibernate-core"])],
        "parent_manages_spring_boot": "auto"})
    return cfg.model_copy(update={"artifacts": a, "layout": cfg.layout.model_copy(update={"module_dir": ""})})


@pytest.fixture()
def chain(monkeypatch):
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", lambda c: FACTS)
    return FACTS


def _ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "p"
    for d, text in (("", ROOT), ("shop-back", BACK), ("shop-boot", BOOT), ("shop-lib", LIB)):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "pom.xml").write_text(text)
    return Workspace(root, tmp_path / "run", "r")


def _profile(case: CaseType = CaseType.SPRING_LEGACY, jsp: int = 0, services=()) -> ProjectProfile:
    return ProjectProfile(root="/x", name="shop", case_type=case, java_version="1.8", spring_version="5.3.31",
                          source=SourceStats(java_files=3, jsp_count=jsp), services=list(services))


def _result(**kw) -> RestructureResult:
    dropped = "org.springframework:spring-webmvc 7.0.10 (as-is 5.3.31, managed 7.0.8, shop-back/pom.xml)"
    base = {"ok": True, "boot_module": "shop-boot", "comp_module": "shop-back", "boot_packaging": "jar",
            "starter": "spring-boot-starter-webmvc", "web_evidence": ["web.xml", "@Controller ×2"],
            "parent_manages_spring_boot_effective": True,
            "scope_restored": ["jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, shop-back/pom.xml)"],
            "source_encodings_written": ["shop-back/pom.xml: project.build.sourceEncoding=MS949 (12 files)"],
            "xml_imports": ["classpath:legacy-config/root-context.xml"],
            "xml_skipped": ["src/test/resources/test-context.xml (test)"],
            "conversion_facts": {"enable_web_mvc": "false", "dispatcher_mappings": ["*.do", "/api/*"]},
            "recipe_versions_dropped": [dropped],
            "notes": [f"recipe-raised version dropped: {dropped}", "front module shop-front: 3 static web file(s) moved"]}
    base.update(kw)
    return RestructureResult(**base)


def _section(text: str, title: str) -> str:
    start = text.index(f"# {title}")
    nxt = text.find("\n# ", start + 2)
    return text[start: nxt if nxt != -1 else len(text)]


RUN_NOTES = [("restructure.apply: recipe-raised version dropped: org.springframework:spring-webmvc 7.0.10 (as-is "
              "5.3.31, managed 7.0.8, shop-back/pom.xml)"),
             "restructure.apply: front module shop-front: 3 static web file(s) moved",
             "restructure.apply (layout): 9 file(s) changed, 4 moved, 3 generated"]


def test_spring_legacy_role_parents_input(tmp_path, company, chain):
    text = collect_review_input(_ws(tmp_path), _profile(), company, notes=RUN_NOTES, results=[_result()]).render()
    facts = _section(text, "Facts sbmig collected")
    # D-H1: the chain's version properties (company poms only, *.version only) and the problem lines
    assert f"- parent chain property: spring-boot.version=4.1.0 ({BASE})" in facts
    assert f"- parent chain property: acme-jpa.version=4.0.0 ({BASE})" in facts
    assert "parent chain property: project.build.sourceEncoding" not in facts
    assert ("- explicit version on a managed artifact: `shop-back/pom.xml` org.springframework:spring-context 6.2.1 — "
            f"managed 7.0.8 by the parent chain (`{BASE}`)") in facts
    assert ("- Spring Boot artifact on another line: `shop-back/pom.xml` org.springframework.boot:"
            "spring-boot-starter-web 2.7.18 (chain 4.1.0)") in facts
    assert (f"- property shadows the parent chain: `shop-back/pom.xml` spring-boot.version=2.5.15 (chain: 4.1.0 by "
            f"{BASE})") in facts
    assert "shadows the parent chain: `shop-back/pom.xml` project.build.sourceEncoding" not in facts
    assert "spring-webmvc 7.0.10 —" not in facts                     # a newer pin is the restructure's (gte) decision
    assert "acme-jpa" not in facts.split("parent chain property: acme-jpa.version")[1].split("\n", 1)[1]  # ${prop} form ok
    # D-M9 / D-M10
    assert ("- container API declared `compile` (the embedded Tomcat provides it; `provided` is right): "
            "`jakarta.servlet:jakarta.servlet-api` in `shop-back/pom.xml` (scope compile)") in facts
    assert ("- repackaged module used as a dependency: `shop-lib/pom.xml` (`com.example:shop-lib`) runs "
            "spring-boot-maven-plugin repackage without a classifier and `shop-back/pom.xml` depend on it") in facts
    assert "`shop-boot/pom.xml` (`com.example:shop-boot`) runs" not in facts      # nobody depends on the boot module
    rules = _section(text, "Company rules")
    # D-H2: from the effective decision — no target literal as a rule
    assert ("- Spring Boot versions: the parent chain reaches the Boot BOM: no Boot BOM import and no Boot version "
            "property belongs in the project poms") in rules
    assert company.target.spring_boot_version not in rules and "imported in the poms" not in rules
    # D-H5: per spec, and the role-parent sentence
    assert "  - `com.acme:acme-core` (version managed) → `<dependencies>`" in rules
    assert "  - `com.acme:acme-bom` (version managed) [type pom, scope import] → `<dependencyManagement>` (BOM import)" in rules
    assert "with role parents a module does not inherit the root pom's declarations" in rules
    assert "type pom + scope import → in dependencyManagement" not in rules
    # D-M5 / D-M6 / D-M8
    assert "`${<artifact>.version}` when the parent chain defines that property" in rules
    assert ("  - `com.acme:acme-jpa` (version managed) (added for org.hibernate*:hibernate-core; replaces them; "
            "property `acme-jpa.version`)") in rules
    assert "are final: never re-add, re-pin or re-literalize them" in rules
    assert ("the boot module depends on `shop-back` and the starter `spring-boot-starter-webmvc`; do not add Security / "
            "Batch / JPA starters") in rules
    assert "packaging jar (executable jar), parent = boot parent" in rules
    # D-M3 / E-L1: the new facts, notes de-duplicated
    did = _section(text, "What the restructure did")
    for part in ("  - boot starter: `spring-boot-starter-webmvc`", "  - web evidence: web.xml, @Controller ×2",
                 "  - parent chain manages Spring Boot (effective): yes", "  - scopes restored from the as-is build:",
                 "  - source encodings written:", "  - XML bean configs imported via @ImportResource:",
                 "  - XML bean configs not imported:", "  - conversion facts:", "    - enable_web_mvc: false",
                 "    - dispatcher_mappings: *.do, /api/*"):
        assert part in did, part
    assert did.count("spring-webmvc 7.0.10") == 1                     # the structured line only
    assert did.count("3 static web file(s) moved") == 1               # the result note, not the run note again
    assert "- run notes:\n  - restructure.apply (layout): 9 file(s) changed" in did
    # D-M1: the last line asks for the edits
    assert text.rstrip().endswith("— `<analysis>`, `<solution>` and `<edits>`.")


def test_boot_bom_import_line(tmp_path, company, chain):
    imports = [f"{p}: org.springframework.boot:spring-boot-dependencies ${{spring-boot.version}} (property of {BASE})"
               for p in ("shop-back/pom.xml", "shop-boot/pom.xml")]
    res = _result(parent_manages_spring_boot_effective=False, bom_imports=imports)
    rules = _section(collect_review_input(_ws(tmp_path), _profile(), company, results=[res]).render(), "Company rules")
    assert ("- BOM import by the restructure: `org.springframework.boot:spring-boot-dependencies` `${spring-boot.version}` "
            f"(= 4.1.0, defined by {BASE}) in `shop-back/pom.xml`, `shop-boot/pom.xml` — the property resolves through "
            "Maven inheritance: never define `spring-boot.version` in a project pom") in rules
    assert "the parent chain does not reach the Boot BOM: the restructure imported it" in rules
    assert company.target.spring_boot_version not in rules.replace("(= 4.1.0,", "")


def test_msa_line_follows_the_code(tmp_path, company, chain):
    svcs = [ServiceInfo(name="order-service", root_pom="order-service/pom.xml", case_type=CaseType.SPRING_BOOT,
                        main_classes=["a.App"], spring_boot_version="2.7.18"),
            ServiceInfo(name="common", root_pom="common/pom.xml", case_type=CaseType.PURE_JAVA, kind="library")]
    rules = _section(collect_review_input(_ws(tmp_path), _profile(CaseType.MSA, services=svcs), company).render(),
                     "Company rules")
    assert ("Service roots (`order-service`) → the boot parent + the mandatory dependencies; library roots (`common`) → "
            "the comp parent, no mandatory dependencies; the top aggregator → the root parent + the mandatory "
            "dependencies") in rules
    assert "a `war` service keeps its packaging" in rules and "named like" not in rules
    assert "mandatory dependencies, declared in the service roots (see the layout line)" in rules
    plain = company.model_copy(update={"artifacts": company.artifacts.model_copy(update={"boot_parent": None,
                                                                                         "comp_parent": None})})
    rules = _section(collect_review_input(_ws(tmp_path), _profile(CaseType.MSA, services=svcs), plain).render(),
                     "Company rules")
    assert "→ the root parent + the mandatory dependencies; library roots (`common`) → the root parent" in rules
    assert "the top aggregator keeps its own parent (not changed)" in rules
    assert "every service / library root gets the root parent" in rules


def test_jsp_boot_module_is_an_executable_war(tmp_path, company, chain):
    rules = _section(collect_review_input(_ws(tmp_path), _profile(CaseType.SERVLET_TOMCAT, jsp=2), company).render(),
                     "Company rules")
    assert ("packaging war (executable WAR: `java -jar` runs the embedded Tomcat, the same file deploys to an external "
            "Tomcat too; `spring-boot-starter-tomcat` / `tomcat-embed-jasper` stay `provided`") in rules
    assert "JSPs served by an external Tomcat" not in rules


def test_no_repository_means_no_version_lines(tmp_path, company):
    # the conftest stand-in returns empty facts (no ~/.m2): no line, nothing raised
    text = collect_review_input(_ws(tmp_path), _profile(), company, results=[_result()]).render()
    for head in ("parent chain property:", "explicit version on a managed artifact:", "Spring Boot artifact on another",
                 "property shadows the parent chain:"):
        assert head not in text, head
    assert "container API declared `compile`" in text                # read from the poms, no facts needed


def test_facts_loader_errors_never_reach_the_review(tmp_path, company, monkeypatch):
    def boom(c):
        raise RuntimeError("repository down")
    monkeypatch.setattr(parent_chain, "load_parent_chain_facts", boom)
    monkeypatch.setattr(boot_bom, "load_boot_bom_facts", lambda c, version=None: (_ for _ in ()).throw(OSError("x")))
    text = collect_review_input(_ws(tmp_path), _profile(), company, results=[_result()]).render()
    assert "parent chain property:" not in text and "# Facts sbmig collected" in text


def test_ask_script_last_line(tmp_path, company, chain):
    asking = company.model_copy(update={"review": company.review.model_copy(update={"ask_script": True})})
    text = collect_review_input(_ws(tmp_path), _profile(), asking).render()
    assert text.rstrip().endswith("`<analysis>`, `<solution>` and `<edits>`, optionally `<script>` with `<changes>`.")


def test_save_input_writes_the_user_prompt(tmp_path, cfg):
    from .test_reviewer import make_ws, profile

    ws = make_ws(tmp_path)
    llm = FakeLLM(OK)
    StructureReviewer(cfg, llm).review(ws, profile(), cfg, run_id="run-7")
    saved = tmp_path / "run" / "logs" / REVIEW_INPUT_FILE
    assert saved.read_text(encoding="utf-8") == llm.calls[0]["messages"][1].content
    off = cfg.model_copy(update={"review": cfg.review.model_copy(update={"save_input": False})})
    saved.unlink()
    StructureReviewer(off, FakeLLM(OK)).review(ws, profile(), off, run_id="run-7")
    assert not saved.exists()


def test_save_input_without_a_run_dir_never_raises(tmp_path, cfg):
    from .test_reviewer import make_ws, profile

    ws = make_ws(tmp_path)
    ws.run_dir = tmp_path / "file-not-dir"
    (tmp_path / "file-not-dir").write_text("x")                       # logs/ cannot be created under a file
    rv = StructureReviewer(cfg, FakeLLM(OK)).review(ws, profile(), cfg, run_id="run-7")
    assert rv.verdict == "ok"
