"""AdvisoryCatalog: load / merge by id / check; the repo's advisories.yml is consistent."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import AdvisoryCatalog, load_advisories
from sbmig.advisory.catalog import recipe_unavailable
from tests.advisory.helpers import write_advisories

REPO_IDS = {"springfox", "sleuth", "hystrix", "ribbon", "zuul", "netflix-feign", "spring-cloud-bom",
            "spring-security-oauth2-legacy", "log4j1", "commons-lang2", "httpclient4", "jdk-removed-apis", "ibatis2",
            "jdk-generified-apis", "hibernate-commons-annotations"}
FOX = "org.openrewrite.java.springdoc.SpringFoxToSpringDoc"


def test_repo_advisories_load_and_check_clean(cfg):
    cat = load_advisories(cfg)
    assert set(cat.ids()) == REPO_IDS
    assert cat.check(cfg) == []
    policies = {e.id: e.policy for e in cat.entries}
    assert {k for k, v in policies.items() if v == "recipe"} == {"springfox", "sleuth", "hystrix",
                                                                  "spring-cloud-bom"}
    assert all(e.followup and e.replacement and e.docs for e in cat.entries)
    assert cat.get("spring-cloud-bom").applies_when_version_below == "2025.1"


def test_merge_by_id_later_file_wins_and_missing_files_ignored(tmp_path: Path, cfg, caplog):
    base = write_advisories(tmp_path / "a.yml", [
        {"id": "springfox", "library": "springfox", "title": "base", "policy": "guide"},
        {"id": "log4j1", "library": "log4j1", "title": "log4j"}])
    extra = write_advisories(tmp_path / "b.yml", [
        {"id": "springfox", "library": "springfox", "title": "company", "policy": "guide"},
        {"id": "acme-lib", "artifacts": ["com.acme:legacy-*"], "title": "acme", "status": "unmanaged"}])
    cfg.advisories.file = str(base)
    cfg.advisories.extra_files = [str(extra), str(tmp_path / "missing.yml")]
    with caplog.at_level("INFO", logger="sbmig.advisory"):
        cat = load_advisories(cfg)
    assert cat.ids() == ["springfox", "log4j1", "acme-lib"]
    assert cat.get("springfox").title == "company"
    assert cat.sources == [base, extra]
    assert any("not found" in r.message for r in caplog.records)
    assert cat.check(cfg) == []


def test_empty_and_missing_catalog(tmp_path: Path, cfg):
    cfg.advisories.file = str(tmp_path / "nope.yml")
    cat = load_advisories(cfg)
    assert len(cat) == 0 and cat.entries == [] and cat.get("x") is None
    assert AdvisoryCatalog.from_dict({"advisories": []}).check(cfg) == []


def test_check_reports_every_problem(tmp_path: Path, cfg):
    p = write_advisories(tmp_path / "a.yml", [
        {"id": "a", "library": "no-such-rule", "title": "A"},
        {"id": "b", "library": "springfox", "title": "B", "policy": "recipe"},
        {"id": "c", "library": "springfox", "title": "C", "policy": "recipe", "recipes": ["org.example.Missing"]},
        {"id": "c", "library": "springfox", "title": "C2"},
        {"id": "d", "library": "hystrix", "title": "D", "applies_to": ["spring_boot", "spring_bootz"]},
        {"id": "e", "title": "E"},
        {"id": "f", "artifacts": ["just-a-group"], "title": "F"},
        {"id": "g", "title": "G", "status": "dead"},
    ])
    cat = AdvisoryCatalog.from_files([p])
    text = "\n".join(cat.check(cfg))
    assert "advisory a: unknown library 'no-such-rule'" in text
    assert "advisory b: policy recipe without recipes" in text
    assert "duplicate advisory id c" in text
    assert "bad applies_to spring_bootz" in text
    assert "advisory e: needs a library or artifacts" in text
    assert "artifact pattern 'just-a-group'" in text
    assert "advisory g invalid" in text
    assert cat.get("c").title == "C2"                                    # later wins even inside one file
    assert cat.get("d").applies_to and cat.get("d").applies_to[0].value == "spring_boot"


def test_recipe_artifact_availability(cfg):
    assert recipe_unavailable(FOX, cfg) is None
    assert "not in the recipe catalog" in recipe_unavailable("org.example.Nope", cfg)
    cfg.openrewrite.recipe_artifacts = ["org.openrewrite.recipe:rewrite-migrate-java"]
    assert "artifact org.openrewrite.recipe:rewrite-spring is not in openrewrite.recipe_artifacts" in \
        recipe_unavailable(FOX, cfg)
    assert recipe_unavailable("com.company.migrate.SpringBoot41Version", cfg) is None      # project recipe
    assert recipe_unavailable("org.openrewrite.java.ChangeType", cfg) is None              # core recipe
    text = "\n".join(load_advisories(cfg).check(cfg))
    assert f"advisory springfox: recipe {FOX} artifact org.openrewrite.recipe:rewrite-spring" in text
