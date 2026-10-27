"""AdvisoryCatalog: load / merge by id / check; the repo's advisories.yml is consistent."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import AdvisoryCatalog, load_advisories
from tests.advisory.helpers import write_advisories

REPO = Path(__file__).resolve().parents[2]

REPO_IDS = {"springfox", "sleuth", "hystrix", "ribbon", "zuul", "netflix-feign", "spring-cloud-bom",
            "spring-security-oauth2-legacy", "log4j1", "commons-lang2", "httpclient4", "jdk-removed-apis", "ibatis2",
            "jdk-generified-apis", "hibernate-commons-annotations",
            # 2026-10-22 era libraries
            "tiles", "velocity", "thymeleaf-legacy", "commons-fileupload", "jersey1", "spring-hateoas-legacy",
            "spring-portlet", "spring-mobile", "jasperreports6", "struts2", "struts1", "commons-chain", "egovframe",
            "webjars-locator-core", "shiro-legacy", "hibernate-template", "hibernate-entitymanager",
            "hibernate-ehcache", "hypersistence-utils", "hibernate-search-legacy", "querydsl-hibernate-native",
            "ehcache2", "hazelcast-legacy", "commons-dbcp1", "druid", "jasypt-spring-boot", "atomikos", "bitronix",
            "commons-collections3", "dozer", "orika", "elasticsearch-rest-high-level", "solr-legacy", "jackson1",
            "json-lib", "xstream", "jdom1", "axis1", "commons-email1", "commons-configuration1", "cglib",
            "commons-httpclient3", "jjwt-legacy", "poi-legacy", "jxls1", "jexcelapi", "slf4j-1-pin",
            "mockito-legacy", "powermock",
            # round 10: non-Spring twins of httpclient4 / jackson1
            "httpclient4-direct", "jackson1-direct", "undertow"}


def test_repo_advisories_load_and_check_clean(cfg):
    cat = load_advisories(cfg)
    assert set(cat.ids()) == REPO_IDS
    assert cat.check(cfg) == []
    assert all(not hasattr(e, "policy") and not hasattr(e, "recipes") for e in cat.entries)   # guide-only: no policy / recipes
    raw = (REPO / "knowledge" / "libraries" / "advisories.yml").read_text(encoding="utf-8")
    assert "\n    policy:" not in raw and "\n    recipes:" not in raw
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
        {"id": "b", "library": "springfox", "title": "B", "notes": "knowledge/notes/missing.md"},
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
    assert "advisory b: notes file knowledge/notes/missing.md does not exist" in text
    assert "recipe" not in text                                           # a v1 recipe list is no longer checked
    assert "duplicate advisory id c" in text
    assert "bad applies_to spring_bootz" in text
    assert "advisory e: needs a library or artifacts" in text
    assert "artifact pattern 'just-a-group'" in text
    assert "advisory g invalid" in text
    assert cat.get("c").title == "C2"                                    # later wins even inside one file
    assert cat.get("d").applies_to and cat.get("d").applies_to[0].value == "spring_boot"
