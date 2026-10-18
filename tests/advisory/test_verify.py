"""verify_advisories on a tmp tree (final project)."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import verify_advisories
from sbmig.analyze import PathView
from sbmig.core.models import AdvisoryFinding
from tests.advisory.helpers import write_advisories
from tests.analyze.helpers import dep, pom, write, write_java

SC = "org.springframework.cloud"


def _f(aid, library=None, artifacts=()):
    return AdvisoryFinding(advisory_id=aid, library=library or aid, status="eol", title=aid, policy="guide",
                           artifacts=list(artifacts))


def test_outcomes_on_the_final_tree(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("x", props={"spring-cloud.version": "2025.1.3"}, managed=(
        dep(SC, "spring-cloud-dependencies", "${spring-cloud.version}", scope="import", type_="pom"),), deps=(
        dep(SC, "spring-cloud-starter-netflix-hystrix", "2.2.10.RELEASE"),
        dep("org.springdoc", "springdoc-openapi-starter-webmvc-ui", "3.0.3"),
        dep("com.acme", "legacy-core", "1.0"))))
    write_java(tmp_path, "", "com.a", "A", imports=("com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand",))
    findings = [_f("springfox", artifacts=("io.springfox:springfox-boot-starter",)),
                _f("hystrix", artifacts=("org.springframework.cloud:spring-cloud-starter-netflix-hystrix",)),
                _f("spring-cloud-bom"), _f("sleuth"), _f("acme", artifacts=("com.acme:legacy-core",)),
                _f("old-train", library="spring-cloud-bom")]
    out = verify_advisories(findings, PathView(tmp_path), cfg)
    assert out is findings
    by = {f.advisory_id: f for f in findings}
    assert by["springfox"].outcome == "replaced" and by["sleuth"].outcome == "replaced"
    assert by["hystrix"].outcome == "still-present" and "still present" in by["hystrix"].notes[-1]
    assert by["spring-cloud-bom"].outcome == "replaced"               # 2025.1.3 ≥ resolved_min_version 2025.1
    assert "2025.1.3" in by["spring-cloud-bom"].notes[-1]
    assert by["acme"].outcome == "still-present"                       # artifact-only finding
    assert by["old-train"].outcome == "still-present"                  # no catalog entry → no resolved_min_version


def test_resolved_min_version_from_company_entry(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("x", deps=(dep("com.acme", "legacy-core", "2.1"),)))
    extra = write_advisories(tmp_path / "adv" / "company.yml", [
        {"id": "acme", "artifacts": ["com.acme:legacy-*"], "title": "acme", "resolved_min_version": "2.0"}])
    cfg.advisories.extra_files = [str(extra)]
    [f] = verify_advisories([_f("acme")], PathView(tmp_path), cfg)
    assert f.outcome == "replaced"


def test_unknown_on_failure_and_empty(tmp_path: Path, cfg):
    class Broken:
        root = tmp_path

        def glob(self, pattern):
            raise OSError("disk gone")

        def pom_files(self):
            raise OSError("disk gone")

        def read(self, rel, encoding="utf-8"):
            raise OSError("disk gone")

    [f] = verify_advisories([_f("hystrix")], Broken(), cfg)
    assert f.outcome == "unknown" and "re-scan failed" in f.notes[-1]
    assert verify_advisories([], Broken(), cfg) == []
