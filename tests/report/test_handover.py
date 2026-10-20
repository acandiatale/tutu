from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sbmig.analyze.analyzer import PathView
from sbmig.core.models import (GAV, AdvisoryFinding, BlockedItem, BlockReason, BuildError, CaseType, ErrorKind,
                               FixAttempt, Phase, PhaseResult, PluginInfo, PomInfo, ProjectProfile, RunReport,
                               TriageDecision, DecisionKind)
from sbmig.report import build_handover, render_markdown

ROOT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.company</groupId><artifactId>company-parent</artifactId><version>4.1.0</version></parent>
  <groupId>com.x</groupId><artifactId>demo</artifactId><version>1.0</version><packaging>pom</packaging>
  <modules><module>module/comp-jar</module><module>module/boot-jar</module></modules>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.springframework.cloud</groupId><artifactId>spring-cloud-dependencies</artifactId>
      <version>2025.1.0</version><type>pom</type><scope>import</scope></dependency>
  </dependencies></dependencyManagement>
</project>
"""
COMP_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.x</groupId><artifactId>demo</artifactId><version>1.0</version></parent>
  <artifactId>demo-comp-jar</artifactId>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><groupId>org.apache.commons</groupId><artifactId>commons-lang3</artifactId><version>3.12.0</version></dependency>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId><version>1.18.38</version></dependency>
    <dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId><version>33.0-jre</version></dependency>
    <dependency><groupId>org.springdoc</groupId><artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>
      <version>3.0.0</version></dependency>
    <dependency><groupId>com.netflix.hystrix</groupId><artifactId>hystrix-core</artifactId><version>1.5.18</version></dependency>
    <dependency><groupId>com.company</groupId><artifactId>company-logging</artifactId><version>2.0</version></dependency>
  </dependencies>
</project>
"""
BOOT_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.x</groupId><artifactId>demo</artifactId><version>1.0</version></parent>
  <artifactId>demo-boot-jar</artifactId>
  <dependencies>
    <dependency><groupId>com.x</groupId><artifactId>demo-comp-jar</artifactId><version>1.0</version></dependency>
  </dependencies>
</project>
"""


def final_tree(tmp_path: Path) -> PathView:
    root = tmp_path / "final"
    (root / "module/comp-jar").mkdir(parents=True, exist_ok=True)
    (root / "module/boot-jar").mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(ROOT_POM)
    (root / "module/comp-jar/pom.xml").write_text(COMP_POM)
    (root / "module/boot-jar/pom.xml").write_text(BOOT_POM)
    return PathView(root)


def as_is_profile() -> ProjectProfile:
    dep = lambda c, **kw: GAV.parse(c).model_copy(update=kw)  # noqa: E731
    pom = PomInfo(
        path="pom.xml", group_id="com.x", artifact_id="demo", version="1.0", is_root=True,
        parent=GAV.parse("org.springframework.boot:spring-boot-starter-parent:2.7.18"),
        java_version="1.8", spring_boot_version="2.7.18",
        dependencies=[dep("org.springframework.boot:spring-boot-starter-web"),
                      dep("org.apache.commons:commons-lang3"),
                      dep("org.projectlombok:lombok:1.18.20"),
                      dep("com.google.guava:guava:20.0"),
                      dep("io.springfox:springfox-swagger2:2.9.2"),
                      dep("com.netflix.hystrix:hystrix-core:1.5.18"),
                      dep("commons-io:commons-io:2.6")],
        dependency_management=[dep("org.springframework.cloud:spring-cloud-dependencies:Hoxton.SR12", type="pom",
                                   scope="import")],
        plugins=[PluginInfo(artifact_id="maven-compiler-plugin", version="3.8.1")])
    return ProjectProfile(root="/as-is", name="demo", case_type=CaseType.SPRING_BOOT, poms=[pom], root_pom=pom,
                          java_version="1.8", spring_boot_version="2.7.18")


EFFECTIVE = {"managed": {"org.apache.commons:commons-lang3": "3.12.0",
                         "org.springframework.boot:spring-boot-starter-web": "2.7.18"},
             "direct": {}, "plugins": {}, "properties": {}}


def findings() -> list[AdvisoryFinding]:
    return [
        AdvisoryFinding(advisory_id="springfox", library="springfox", status="eol", title="Springfox is discontinued",
                        policy="recipe", detected_version="2.9.2", artifacts=["io.springfox:springfox-swagger2"],
                        recipes=["org.openrewrite.java.springdoc.SwaggerToSpringDoc"], recipe_source="advisory",
                        outcome="replaced", replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui",
                        replacement_version="3.x", followup="check the Swagger UI path and the OpenAPI groups",
                        docs=["https://springdoc.org/#migrating-from-springfox"]),
        AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="Hystrix is discontinued",
                        policy="guide", detected_version="1.5.18", artifacts=["com.netflix.hystrix:hystrix-core"],
                        outcome="still-present", replacement="io.github.resilience4j:resilience4j-spring-boot3",
                        followup="replace @HystrixCommand with Resilience4j circuit breakers"),
    ]


def make_report(**kw) -> RunReport:
    t0 = datetime(2026, 10, 18, tzinfo=timezone.utc)
    notes = ["restructure.apply (layout): 3 file(s) changed, 1 moved, 2 generated; mandatory added: "
             "com.company:company-logging; optional bumped: org.projectlombok:lombok 1.18.20→1.18.38"]
    att = FixAttempt(id="a1", phase=Phase.RESTRUCTURE, iteration=1, cluster_id="c1",
                     decision=TriageDecision(cluster_id="c1", kind=DecisionKind.HANDLER,
                                             handler_id="carry-over-managed-version"),
                     notes=["org.apache.commons:commons-lang3 pinned to 3.12.0 (as-is effective pom) — it was "
                            "managed by the as-is parent"])
    pr = PhaseResult(phase=Phase.RESTRUCTURE, notes=notes, attempts=[att])
    base = dict(run_id="r1", project_name="demo", case_type=CaseType.SPRING_BOOT, started_at=t0, finished_at=t0,
                success=True, phases=[pr], advisories=findings(), flags=["managed-versions-carried-over"],
                manual_followups=["web.xml not migrated", "Hystrix is discontinued: replace @HystrixCommand with "
                                                          "Resilience4j circuit breakers"])
    base.update(kw)
    return RunReport(**base)


def test_versions_and_reasons(tmp_path, cfg):
    report = make_report()
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert not [n for n in h.notes if "unavailable" in n]
    assert h.parent_before == "org.springframework.boot:spring-boot-starter-parent:2.7.18"
    assert h.parent_after == "com.company:company-parent:4.1.0"
    assert (h.java_before, h.java_after) == ("1.8", cfg.target.java_version)
    assert (h.spring_boot_before, h.spring_boot_after) == ("2.7.18", cfg.target.spring_boot_version)
    by = {(c.kind, c.key): c for c in h.changes}
    parent = by[("parent", "com.company:company-parent")]
    assert parent.reason == "parent swap" and parent.before.startswith("org.springframework.boot:")
    bom = by[("bom", "org.springframework.cloud:spring-cloud-dependencies")]
    assert (bom.before, bom.after, bom.reason) == ("Hoxton.SR12", "2025.1.0", "spring boot 4 upgrade")
    lang = by[("dependency", "org.apache.commons:commons-lang3")]
    assert lang.carried_over and lang.before_source == "managed" and lang.reason.startswith("carried over")
    assert lang.pom == "module/comp-jar/pom.xml"
    assert by[("dependency", "org.projectlombok:lombok")].reason == "optional bump"
    assert by[("dependency", "com.google.guava:guava")].reason == "recipe/handler"
    assert ("dependency", "org.springframework.boot:spring-boot-starter-web") not in by   # managed → managed
    assert ("dependency", "com.netflix.hystrix:hystrix-core") not in by                   # unchanged
    assert h.carried_over == [lang]
    assert "io.springfox:springfox-swagger2:2.9.2 (advisory:springfox)" in h.removed
    assert "commons-io:commons-io:2.6" in h.removed
    assert "org.springdoc:springdoc-openapi-starter-webmvc-ui:3.0.0 (advisory:springfox)" in h.added
    assert "com.company:company-logging:2.0 (mandatory)" in h.added
    assert not any("demo-comp-jar" in x for x in h.added)                                # own modules ignored
    keys = [c.key for c in h.changes if c.kind == "dependency"]
    assert keys == sorted(keys)


def test_from_here_order_and_dedupe(tmp_path, cfg):
    blocked = BlockedItem(id="b1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c")
    report = make_report(blocked=[blocked], flags=["test-compile-skipped"],
                         unresolved=[BuildError(kind=ErrorKind.COMPILE, message="x")])
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    fh = h.from_here
    assert fh[0].startswith("Hystrix is discontinued: replace @HystrixCommand") and "resilience4j" in fh[0]
    assert fh[1].startswith("verify springfox → org.springdoc:springdoc-openapi-starter-webmvc-ui (recipe ")
    assert fh[2].startswith("org.apache.commons:commons-lang3 pinned at 3.12.0 — the new parent/Boot BOM")
    assert fh[3] == "1 cluster(s) blocked — see Blocked"
    assert fh[4].startswith("1 unresolved error(s)")
    assert fh[5].startswith("test sources do not compile yet")
    assert fh[6] == "web.xml not migrated"
    assert len(fh) == 7                     # the hystrix manual follow-up is not repeated


def test_cap_and_carried_over_summary(tmp_path, cfg):
    prof = as_is_profile()
    pom = prof.poms[0]
    extra = [GAV(group_id="org.lib", artifact_id=f"lib{i}") for i in range(7)]
    pom.dependencies += extra
    eff = {**EFFECTIVE, "managed": {**EFFECTIVE["managed"], **{g.key: f"1.{i}" for i, g in enumerate(extra)}}}
    view = final_tree(tmp_path)
    comp = view.root / "module/comp-jar/pom.xml"
    comp.write_text(comp.read_text().replace("</dependencies>", "".join(
        f"<dependency><groupId>org.lib</groupId><artifactId>lib{i}</artifactId><version>1.{i}</version>"
        "</dependency>" for i in range(7)) + "</dependencies>"))
    report = make_report(manual_followups=[f"follow-up {i}" for i in range(40)])
    h = build_handover(prof, view, report, effective=eff, cfg=cfg)
    assert len(h.carried_over) == 8
    pins = [x for x in h.from_here if " pinned at " in x and not x[0].isdigit()]
    assert len(pins) == 5
    assert any(x.startswith("8 dependencies are pinned") for x in h.from_here)
    assert len(h.from_here) == cfg.advisories.max_from_here
    assert h.from_here[-1].startswith("… and ")
    small = cfg.model_copy(update={"advisories": cfg.advisories.model_copy(
        update={"max_from_here": 3, "report_carried_over": False})})
    h2 = build_handover(prof, view, report, effective=eff, cfg=small)
    assert len(h2.from_here) == 3 and not any("pinned at" in x for x in h2.from_here)
    assert len(h2.carried_over) == 8       # the data stays, only the list skips it


def test_effective_missing_and_failure(tmp_path, cfg, monkeypatch):
    report = make_report(phases=[])
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective={}, cfg=cfg)
    assert any("effective dependencyManagement unavailable" in n for n in h.notes)
    lang = next(c for c in h.changes if c.key == "org.apache.commons:commons-lang3")
    assert lang.before is None and lang.before_source == "absent" and not lang.carried_over

    import sbmig.analyze.pom_parser as pp

    def boom(view):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pp, "parse_poms", boom)
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, cfg=cfg)
    assert h.notes == ["handover unavailable: RuntimeError: disk on fire"] and not h.changes
    assert h.from_here and h.from_here[0].startswith("Hystrix")     # the owners' list survives
    assert build_handover(None, None, report).notes[0].startswith("handover unavailable")


def test_rendered_section(tmp_path, cfg):
    report = make_report()
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    md = render_markdown(report)
    assert "## Handover — what we replaced, what is yours" in md
    assert "| ⚠ dependency `org.apache.commons:commons-lang3` | 3.12.0 (managed) | 3.12.0 | carried over" in md
    assert "| **springfox** | 2.9.2 | eol | recipe | `SwaggerToSpringDoc` (added for this advisory) | ✅ replaced |" in md
    assert "| **hystrix** | 1.5.18 | eol | guide | built with the as-is version pinned | ⚠ still present |" in md
    assert "1. Hystrix is discontinued" in md


@pytest.mark.parametrize("cap", [1])
def test_cap_one(tmp_path, cfg, cap):
    small = cfg.model_copy(update={"advisories": cfg.advisories.model_copy(update={"max_from_here": cap})})
    h = build_handover(as_is_profile(), final_tree(tmp_path), make_report(), effective=EFFECTIVE, cfg=small)
    assert len(h.from_here) == 1 and h.from_here[0].startswith("… and ")


# ---------------------------------------------------------------- round 6: code the LLM rewrote for an advisory
def test_from_here_lists_llm_rewritten_files(tmp_path, cfg):
    fs = findings()
    fs[1] = fs[1].model_copy(update={
        "llm_fixed_files": [f"src/main/java/a/F{i}.java" for i in range(7)],
        "llm_fix_notes": ["HystrixCommand subclass ported to a Resilience4j CircuitBreaker.", "second note.", "third."]})
    report = make_report(advisories=fs)
    fh = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg).from_here
    line = next(x for x in fh if x.startswith("code rewritten by the LLM for hystrix: "))
    assert "src/main/java/a/F0.java, src/main/java/a/F1.java" in line and "F4.java (+2 more)" in line
    assert "F5.java" not in line
    assert "— HystrixCommand subclass ported to a Resilience4j CircuitBreaker.; second note. —" in line
    assert line.endswith("— review these changes (changes.patch, attempts table)")
    # right after the advisory lines (a)/(b), before carried-over pins and pointers — survives the cap
    assert fh.index(line) == 2 and fh[3].startswith("org.apache.commons:commons-lang3 pinned at")
    # no line for findings without LLM-rewritten files
    assert not any(x.startswith("code rewritten by the LLM for springfox") for x in fh)
    small = cfg.model_copy(update={"advisories": cfg.advisories.model_copy(update={"max_from_here": 4})})
    capped = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=small).from_here
    assert any(x.startswith("code rewritten by the LLM for hystrix") for x in capped)


def test_attempts_table_shows_advisory_ids(tmp_path, cfg):
    att = FixAttempt(id="s1", phase=Phase.SPRING, iteration=1, cluster_id="compile:1", model="m1",
                     decision=TriageDecision(cluster_id="compile:1", kind=DecisionKind.LLM_FIX),
                     advisory_ids=["hystrix"], applied=True, outcome="build_ok", analysis="ported")
    report = make_report(phases=[PhaseResult(phase=Phase.SPRING, attempts=[att])])
    md = render_markdown(report)
    assert "`m1` + advisory hystrix" in md
