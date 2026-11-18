"""Hystrix → Resilience4j by recipe again (v2.1z-at): the declarative composite, its catalog entries, the recipe set
`hystrix_resilience4j`, composition into the run's rewrite.yml and the `hystrix` advisory outcome / texts."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from sbmig.advisory import load_advisories, verify_advisories
from sbmig.analyze import PathView
from sbmig.core.config import RecipeSetConfig
from sbmig.core.models import AdvisoryFinding, RecipeRef
from sbmig.plan.catalog import default_catalog, recipe_file_path
from sbmig.rewrite.compose import COMPOSITE_RECIPE_ID, recipe_documents
from sbmig.rewrite.runner import OpenRewriteRunner, catalog_recipe_files
from tests.analyze.helpers import dep, pom, write, write_java

HYSTRIX = "com.company.migrate.HystrixToResilience4j"
RECIPE_FILE = "knowledge/recipes/base/hystrix-to-resilience4j.yml"
COMMAND = "com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand"
CB = "io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker"
SC = "org.springframework.cloud"
NEW_STARTER = "spring-cloud-starter-circuitbreaker-resilience4j"
OLD_STARTERS = {"spring-cloud-starter-netflix-hystrix", "spring-cloud-starter-hystrix"}


def _doc(cfg) -> dict:
    path = recipe_file_path(default_catalog(cfg).get(HYSTRIX), cfg)
    assert path is not None and path.is_file()
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    [doc] = [d for d in docs if d.get("name") == HYSTRIX]
    assert doc["type"] == "specs.openrewrite.org/v1beta/recipe"
    return doc


def _steps(cfg) -> list[tuple[str, dict]]:
    out = []
    for item in _doc(cfg)["recipeList"]:
        (rid, opts), = item.items()
        out.append((rid, opts or {}))
    return out


def test_the_recipe_file_parses_and_converts_the_annotations(cfg) -> None:
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
    prop = ids.index("org.openrewrite.java.RemoveAnnotation")
    assert steps[prop][1]["annotationPattern"].endswith("HystrixProperty") and prop < change_type
    add = ids.index("org.openrewrite.java.AddOrUpdateAnnotationAttribute")
    assert add > change_type
    assert steps[add][1] == {"annotationType": CB, "attributeName": "name", "attributeValue": '"default"',
                             "addOnly": True}
    patterns = {o["annotationPattern"] for r, o in steps if r == "org.openrewrite.java.RemoveAnnotation"}
    assert {"@org.springframework.cloud.netflix.hystrix.EnableHystrix",
            "@org.springframework.cloud.client.circuitbreaker.EnableCircuitBreaker",
            "@org.springframework.cloud.netflix.hystrix.dashboard.EnableHystrixDashboard"} <= patterns


def test_the_recipe_swaps_the_starters_and_drops_the_hystrix_artifacts(cfg) -> None:
    steps = _steps(cfg)
    swaps = [o for r, o in steps if r == "org.openrewrite.maven.ChangeDependencyGroupIdAndArtifactId"]
    assert {o["oldArtifactId"] for o in swaps} == OLD_STARTERS
    assert all(o["newArtifactId"] == NEW_STARTER and o["newGroupId"] == SC for o in swaps)
    assert {o["artifactId"] for r, o in steps if r == "org.openrewrite.maven.RemoveDependency"} == {
        "hystrix-javanica", "hystrix-core", "spring-cloud-starter-netflix-hystrix-dashboard"}
    assert {o["artifactId"] for r, o in steps if r == "org.openrewrite.maven.RemoveManagedDependency"} == {
        *OLD_STARTERS, "hystrix-javanica", "hystrix-core"}


def test_the_recipe_writes_no_version(cfg) -> None:
    steps = _steps(cfg)
    ids = [r for r, _ in steps]
    # no option of any step carries a version literal, and no step sets one
    for rid, opts in steps:
        assert "newVersion" not in opts and "version" not in opts, rid
        for v in opts.values():
            assert not re.fullmatch(r"\d+(\.\d+)+([.-]\w+)?", str(v)), (rid, v)
    assert "org.openrewrite.xml.ChangeTagValue" not in ids
    assert "org.openrewrite.maven.RemoveRedundantDependencyVersions" not in ids
    # the as-is version of each old starter is removed BEFORE the swap (and the managed entries before as well)
    first_swap = ids.index("org.openrewrite.maven.ChangeDependencyGroupIdAndArtifactId")
    drops = [(i, o["xPath"]) for i, (r, o) in enumerate(steps) if r == "org.openrewrite.xml.RemoveXmlTag"]
    assert {x for _i, x in drops} == {f"/project/dependencies/dependency[artifactId='{a}']/version"
                                      for a in OLD_STARTERS}
    assert all(i < first_swap for i, _x in drops)
    managed = [i for i, (r, o) in enumerate(steps)
               if r == "org.openrewrite.maven.RemoveManagedDependency" and o["artifactId"] in OLD_STARTERS]
    assert len(managed) == 2 and all(i < first_swap for i in managed)
    assert "{{" not in Path(recipe_file_path(default_catalog(cfg).get(HYSTRIX), cfg)).read_text(encoding="utf-8")


def test_every_building_block_is_catalogued(cfg) -> None:
    cat = default_catalog(cfg)
    e = cat.get(HYSTRIX)
    assert e is not None and e.origin == "openrewrite" and e.artifact is None and e.file == RECIPE_FILE
    assert e.sets == ["hystrix_resilience4j"] and not e.advisor
    for rid, _ in _steps(cfg):
        b = cat.get(rid)
        assert b is not None, rid
        assert b.artifact is None, rid                           # core recipes only: no extra recipe jar
    assert cat.check(cfg) == []


def test_catalog_check_reports_a_broken_file_entry(tmp_path: Path, cfg) -> None:
    from sbmig.plan.catalog import RecipeCatalog

    f = tmp_path / "r.yml"
    f.write_text("type: specs.openrewrite.org/v1beta/recipe\nname: com.x.Local\nrecipeList:\n"
                 "  - org.openrewrite.java.ChangeType:\n      oldFullyQualifiedTypeName: a.A\n"
                 "      newFullyQualifiedTypeName: b.B\n  - com.x.Unknown\n", encoding="utf-8")
    base = default_catalog(cfg)
    entries = [e.model_dump() for e in base.entries]
    cat = RecipeCatalog.from_dict({"recipes": entries + [
        {"id": "com.x.Local", "file": str(f)}, {"id": "com.x.Gone", "file": str(tmp_path / "none.yml")},
        {"id": "com.x.Other", "file": str(f)}]})
    problems = cat.check(cfg)
    assert f"catalog com.x.Local: building block com.x.Unknown (in {f}) is not in the catalog" in problems
    assert f"catalog com.x.Gone: file {tmp_path / 'none.yml'} not found" in problems
    assert f"catalog com.x.Other: file {f} does not define it" in problems


def test_the_composite_is_in_the_default_set(cfg) -> None:
    assert RecipeSetConfig().hystrix_resilience4j == [HYSTRIX]
    assert cfg.openrewrite.recipes.hystrix_resilience4j == [HYSTRIX]


def test_the_run_rewrite_yml_inlines_the_recipe_file(tmp_path: Path, cfg, make_ws) -> None:
    refs = [RecipeRef(id="org.openrewrite.java.migrate.UpgradeToJava21"), RecipeRef(id=HYSTRIX),
            RecipeRef(id="org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0")]
    files = catalog_recipe_files(refs, cfg)
    assert [p.as_posix().endswith(RECIPE_FILE) for p in files] == [True]
    src = tmp_path / "src"
    write(src, "pom.xml", pom("x"))
    runner = OpenRewriteRunner(make_ws(src), cfg)
    path, text = runner.compose(refs)
    docs = recipe_documents(text)
    assert HYSTRIX in docs and path.read_text(encoding="utf-8") == text
    assert docs[COMPOSITE_RECIPE_ID]["recipeList"] == [r.id for r in refs]
    assert "org.openrewrite.recipe:rewrite-spring" in runner.required_artifacts(refs, text)


def test_verify_marks_the_migrated_tree_replaced(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("x", props={"spring-cloud.version": "2025.1.3"}, managed=(
        dep(SC, "spring-cloud-dependencies", "${spring-cloud.version}", scope="import", type_="pom"),), deps=(
        dep(SC, NEW_STARTER),)))
    write_java(tmp_path, "", "com.a", "A", imports=(CB,))
    f = AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="Hystrix", policy="guide",
                        artifacts=[f"{SC}:spring-cloud-starter-netflix-hystrix"])
    verify_advisories([f], PathView(tmp_path), cfg)
    assert f.outcome == "replaced"


def test_verify_keeps_still_present_while_hystrix_code_is_left(tmp_path: Path, cfg) -> None:
    # an unported HystrixCommand subclass keeps the import: the advisory is not `replaced`
    write(tmp_path, "pom.xml", pom("x", deps=(dep(SC, NEW_STARTER, "5.0.3"),)))
    write_java(tmp_path, "", "com.a", "A", imports=("com.netflix.hystrix.HystrixCommand",))
    f = AdvisoryFinding(advisory_id="hystrix", library="hystrix", status="eol", title="Hystrix", policy="guide")
    verify_advisories([f], PathView(tmp_path), cfg)
    assert f.outcome == "still-present"


def test_the_advisory_texts_say_what_was_replaced(cfg) -> None:
    e = load_advisories(cfg).get("hystrix")
    assert e is not None and e.replacement == f"{SC}:{NEW_STARTER}"
    assert HYSTRIX in e.summary and "fix loop" in e.summary
    for needle in ("resilience4j.circuitbreaker.instances", "resilience4j.timelimiter", "Throwable", '"default"',
                   "Thread-pool isolation", "spring-boot-starter-aspectj", "Turbine"):
        assert needle in e.followup, needle
    # the Korean summary says what replaces what; whether THIS run did it is said by the report item's lead
    assert "바꿔야 합니다" in e.summary_ko and "`@CircuitBreaker`" in e.summary_ko
    assert not any(w in e.summary_ko for w in ("바꿨습니다", "지웠습니다", "옮겼습니다"))
    for needle in ("resilience4j.circuitbreaker.instances.<name>", "resilience4j.timelimiter.*", "`Throwable`",
                   "spring-boot-starter-aspectj", "스레드 풀 격리", "Turbine", "MIGRATION-TODO"):
        assert needle in e.followup_ko, needle
    # nothing says Hystrix stays any more
    for text in (e.summary, e.followup):
        assert "may still compile" not in text and "stays" not in text
