"""Hystrix → Resilience4j: the project composite, its catalog entry, the `hystrix` advisory (policy recipe),
planning, composition, verification and the handover sentence."""
from __future__ import annotations

from pathlib import Path

import yaml

from sbmig.advisory import (
    advisory_recipe_refs,
    evaluate_advisories,
    followups_for,
    load_advisories,
    verify_advisories,
)
from sbmig.analyze import PathView
from sbmig.core.models import AdvisoryFinding, LibraryUsage
from sbmig.plan.catalog import default_catalog
from sbmig.rewrite.compose import compose_rewrite_yaml, recipe_documents
from tests.advisory.helpers import profile
from tests.analyze.helpers import dep, pom, write, write_java

HYSTRIX = "com.company.migrate.HystrixToResilience4j"
BOOT4 = "org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0"
COMMAND = "com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand"
CB = "io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker"
SC = "org.springframework.cloud"
NEW_STARTER = "spring-cloud-starter-circuitbreaker-resilience4j"


def _steps(cfg) -> list[tuple[str, dict]]:
    path = cfg.base_recipe_dir / "hystrix-to-resilience4j.yml"
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    [doc] = [d for d in docs if d.get("name") == HYSTRIX]
    assert doc["type"] == "specs.openrewrite.org/v1beta/recipe"
    assert "default" in doc["description"] and "commandKey" in doc["description"]
    out = []
    for item in doc["recipeList"]:
        (rid, opts), = item.items()
        out.append((rid, opts or {}))
    return out


def test_composite_steps_and_order(cfg) -> None:
    steps = _steps(cfg)
    ids = [r for r, _ in steps]
    change_type = ids.index("org.openrewrite.java.ChangeType")
    assert steps[change_type][1] == {"oldFullyQualifiedTypeName": COMMAND, "newFullyQualifiedTypeName": CB}
    # commandKey → name and the attribute removals act on HystrixCommand, before the type swap
    rename = ids.index("org.openrewrite.java.ChangeAnnotationAttributeName")
    assert rename < change_type and steps[rename][1] == {"annotationType": COMMAND, "oldAttributeName": "commandKey",
                                                         "newAttributeName": "name"}
    removed = {o["attributeName"] for r, o in steps[:change_type] if r == "org.openrewrite.java.RemoveAnnotationAttribute"}
    assert removed == {"groupKey", "threadPoolKey", "commandProperties", "threadPoolProperties", "ignoreExceptions",
                       "observableExecutionMode", "defaultFallback", "raiseHystrixExceptions"}
    # nested @HystrixProperty goes before commandProperties (RemoveAnnotation drops the import too)
    prop = ids.index("org.openrewrite.java.RemoveAnnotation")
    assert steps[prop][1]["annotationPattern"].endswith("HystrixProperty") and prop < change_type
    # name = "default" is added after the swap (shallow type → verbatim value, quotes included), add-only
    add = ids.index("org.openrewrite.java.AddOrUpdateAnnotationAttribute")
    assert add > change_type
    assert steps[add][1] == {"annotationType": CB, "attributeName": "name", "attributeValue": '"default"',
                             "addOnly": True}
    patterns = {o["annotationPattern"] for r, o in steps if r == "org.openrewrite.java.RemoveAnnotation"}
    assert {"@org.springframework.cloud.netflix.hystrix.EnableHystrix",
            "@org.springframework.cloud.client.circuitbreaker.EnableCircuitBreaker",
            "@org.springframework.cloud.netflix.hystrix.dashboard.EnableHystrixDashboard"} <= patterns


def test_composite_pom_steps_need_no_metadata_download(cfg) -> None:
    steps = _steps(cfg)
    swaps = [o for r, o in steps if r == "org.openrewrite.maven.ChangeDependencyGroupIdAndArtifactId"]
    assert {o["oldArtifactId"] for o in swaps} == {"spring-cloud-starter-netflix-hystrix", "spring-cloud-starter-hystrix"}
    assert all(o["newArtifactId"] == NEW_STARTER and o["newGroupId"] == SC for o in swaps)
    # a newVersion would make the swap fetch maven-metadata; on HTTP 429 it then silently skips the pom
    assert all("newVersion" not in o for o in swaps)
    assert {o["artifactId"] for r, o in steps if r == "org.openrewrite.maven.RemoveDependency"} == {
        "hystrix-javanica", "hystrix-core", "spring-cloud-starter-netflix-hystrix-dashboard"}
    rid, last = steps[-1]
    assert rid == "org.openrewrite.maven.RemoveRedundantDependencyVersions"
    assert last == {"groupPattern": SC, "artifactPattern": NEW_STARTER, "onlyIfManagedVersionIs": "ANY"}


def test_composite_is_catalogued_and_wired_to_the_hystrix_advisory(cfg) -> None:
    rcat = default_catalog(cfg)
    e = rcat.get(HYSTRIX)
    assert e.origin == "project" and e.phase == "spring" and e.advisories == ["hystrix"]
    assert e.file == "knowledge/recipes/base/hystrix-to-resilience4j.yml" and e.covers == []
    for rid, _ in _steps(cfg):                                     # every building block is catalogued
        assert rid in rcat, rid
    assert rcat.check(cfg) == []
    entry = next(x for x in load_advisories(cfg).entries if x.id == "hystrix")
    assert entry.policy == "recipe" and entry.recipes == [HYSTRIX]
    assert entry.replacement == f"{SC}:{NEW_STARTER}"
    for needle in ("resilience4j.circuitbreaker.instances", "resilience4j.timelimiter", "Throwable", '"default"',
                   "thread-pool isolation"):
        assert needle in entry.followup, needle


def test_hystrix_profile_gets_the_composite_prepended(cfg) -> None:
    [f] = evaluate_advisories(profile(libs=(LibraryUsage(name="hystrix", version="2.2.10.RELEASE"),)), cfg)
    assert f.advisory_id == "hystrix" and f.policy == "recipe" and f.recipes == [HYSTRIX]
    [ref] = advisory_recipe_refs([f], cfg, [BOOT4])                # the Boot umbrella does not cover it
    assert ref.id == HYSTRIX and ref.source == "project" and ref.artifact is None
    assert ref.file and ref.file.endswith("hystrix-to-resilience4j.yml")
    assert f.recipe_source == "advisory" and any(HYSTRIX in n and "added" in n for n in f.notes)
    text = compose_rewrite_yaml([ref], base_dir=cfg.base_recipe_dir, target=cfg.target)
    docs = recipe_documents(text)
    assert HYSTRIX in docs and any(HYSTRIX in str(d.get("recipeList")) for n, d in docs.items() if n != HYSTRIX)


def test_verify_marks_the_migrated_tree_replaced(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("x", props={"spring-cloud.version": "2025.1.3"}, managed=(
        dep(SC, "spring-cloud-dependencies", "${spring-cloud.version}", scope="import", type_="pom"),), deps=(
        dep(SC, NEW_STARTER),)))
    write_java(tmp_path, "", "com.a", "A", imports=(CB,))
    f = AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="Hystrix", policy="recipe",
                        recipes=[HYSTRIX], replacement=f"{SC}:{NEW_STARTER}", followup="Configure it.",
                        artifacts=[f"{SC}:spring-cloud-starter-netflix-hystrix"])
    verify_advisories([f], PathView(tmp_path), cfg)
    assert f.outcome == "replaced"
    [line] = followups_for([f])
    assert line == f"hystrix → {SC}:{NEW_STARTER} applied by HystrixToResilience4j; Configure it."
