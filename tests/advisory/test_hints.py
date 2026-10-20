"""sbmig.advisory.hints_for (round 6): advisory findings → "what to migrate TO" lines for the fix-loop prompt."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import hints_for
from sbmig.advisory.hints import HINT_MAX, NOTE_EXCERPT_MAX, library_packages, note_section
from sbmig.core.models import GAV, AdvisoryFinding, BuildError, ErrorKind

R4J = "org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j"


def finding(**kw) -> AdvisoryFinding:
    base = dict(advisory_id="hystrix", library="hystrix", status="eol", title="Netflix Hystrix is discontinued",
                policy="recipe", replacement=R4J, replacement_version="managed by the Spring Cloud BOM",
                artifacts=["org.springframework.cloud:spring-cloud-starter-netflix-hystrix"],
                followup="Configure the circuit breakers by name; HystrixCommand subclasses stay manual.")
    base.update(kw)
    return AdvisoryFinding(**base)


def springfox() -> AdvisoryFinding:
    return AdvisoryFinding(advisory_id="springfox", library="springfox", status="eol", title="Springfox is gone",
                           policy="recipe", replacement="org.springdoc:springdoc-openapi-starter-webmvc-ui",
                           artifacts=["io.springfox:springfox-boot-starter"], followup="check the Swagger UI path")


def err(message: str = "cannot find symbol", **kw) -> BuildError:
    return BuildError(kind=kw.pop("kind", ErrorKind.COMPILE), message=message,
                      file=kw.pop("file", "src/main/java/a/InventoryCommand.java"), **kw)


def test_library_packages_reads_library_rules():
    assert library_packages("hystrix") == ("com.netflix.hystrix",)
    assert "springfox" in library_packages("springfox")
    assert library_packages("no-such-library") == ()


def test_match_on_import_package_with_segment_boundary():
    hints, ids = hints_for([err("package com.netflix.hystrix does not exist")], [springfox(), finding()])
    assert ids == ["hystrix"] and len(hints) == 1
    h = hints[0]
    assert h.startswith(f"hystrix (eol, policy recipe): replace with {R4J} (managed by the Spring Cloud BOM). ")
    assert "HystrixCommand subclasses stay manual" in h
    # symbol / location / raw count too; a longer package name or a prefix inside another package does not
    assert hints_for([err(symbol="class com.netflix.hystrix.HystrixCommand")], [finding()])[1] == ["hystrix"]
    assert hints_for([err(location="class com.netflix.hystrix.HystrixCommand")], [finding()])[1] == ["hystrix"]
    assert hints_for([err(raw="import com.netflix.hystrix.HystrixCommandGroupKey;")], [finding()])[1] == ["hystrix"]
    assert hints_for([err("package com.netflix.hystrixfoo does not exist")], [finding()]) == ([], [])
    assert hints_for([err("package org.com.netflix.hystrix does not exist")], [finding()]) == ([], [])


def test_match_on_artifact_and_replacement():
    dep = err("Could not resolve dependencies", kind=ErrorKind.DEPENDENCY_RESOLUTION, file="pom.xml",
              artifact=GAV(group_id="org.springframework.cloud", artifact_id="spring-cloud-starter-netflix-hystrix",
                           version="2.2.10.RELEASE"))
    assert hints_for([dep], [finding()])[1] == ["hystrix"]
    missing = err("cannot find symbol", symbol="class CircuitBreaker",
                  raw="add spring-cloud-starter-circuitbreaker-resilience4j to the module")
    assert hints_for([missing], [finding()])[1] == ["hystrix"]
    # the artifactId alone must stand as a word: a longer artifact name is not the replacement
    assert hints_for([err(raw="spring-cloud-starter-circuitbreaker-resilience4j-extra")], [finding()]) == ([], [])


def test_order_dedupe_and_no_match():
    errors = [err("package springfox.documentation.spi does not exist"),
              err("package com.netflix.hystrix does not exist")]
    hints, ids = hints_for(errors, [finding(), springfox(), finding()])
    assert ids == ["hystrix", "springfox"] and len(hints) == 2 and hints[1].startswith("springfox (eol")
    assert hints_for([err("cannot find symbol", symbol="method execute()")], [finding()]) == ([], [])
    assert hints_for([], [finding()]) == ([], [])
    assert hints_for([err("package com.netflix.hystrix does not exist")], None) == ([], [])


def test_hint_is_compact_and_falls_back_to_title():
    long = finding(followup="Do this. " * 200)
    h = hints_for([err("package com.netflix.hystrix does not exist")], [long])[0][0]
    assert len(h) <= HINT_MAX and h.endswith(".")
    bare = finding(followup="", replacement=None, replacement_version=None)
    h = hints_for([err("package com.netflix.hystrix does not exist")], [bare])[0][0]
    assert h == "hystrix (eol, policy recipe). Netflix Hystrix is discontinued"


def test_note_excerpt_prefers_fix_loop_heading(tmp_path: Path):
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "cloud.md").write_text(
        "---\ntitle: Cloud\nkeywords: [hystrix]\n---\n# Cloud\n\n## Release train\ntrain text\n\n"
        "## Hystrix → Resilience4j\nrecipe text about hystrix\n### detail\nsub detail\n\n"
        "## fix-loop: hystrix\nPort HystrixCommand subclasses to a plain class using CircuitBreaker.ofDefaults.\n"
        "\n## Ribbon\nribbon text\n", encoding="utf-8")
    f = finding(notes=["see knowledge/notes/cloud.md"])
    h = hints_for([err("package com.netflix.hystrix does not exist")], [f], notes_root=tmp_path)[0][0]
    assert h.endswith("Note (cloud.md § fix-loop: hystrix): Port HystrixCommand subclasses to a plain class using "
                      "CircuitBreaker.ofDefaults.")
    assert "ribbon text" not in h and "recipe text" not in h
    # without the convention heading: the first section whose heading names the library (subsections included)
    (notes / "cloud.md").write_text("## Release train\nx\n## Hystrix → Resilience4j\nrecipe text\n### detail\n"
                                    + "y" * 2000 + "\n## Ribbon\nribbon\n", encoding="utf-8")
    h = hints_for([err("package com.netflix.hystrix does not exist")], [f], notes_root=tmp_path)[0][0]
    excerpt = h.split("Note (cloud.md § Hystrix → Resilience4j): ", 1)[1]
    assert excerpt.startswith("recipe text ### detail y") and len(excerpt) <= NOTE_EXCERPT_MAX
    assert "ribbon" not in excerpt
    # a missing / unreadable note never hides the hint
    gone = finding(notes=["see knowledge/notes/missing.md"])
    h = hints_for([err("package com.netflix.hystrix does not exist")], [gone], notes_root=tmp_path)[0][0]
    assert "Note (" not in h and h.startswith("hystrix (eol")


def test_note_section_helper():
    assert note_section("# T\n## Other\nx\n", "hystrix") == ("", "")
    assert note_section("## Hystrixy\nx\n", "hystrix") == ("", "")
    assert note_section("## FIX-LOOP: Hystrix\nbody\n## next\n", "hystrix") == ("FIX-LOOP: Hystrix", "body")


def test_repo_note_for_hystrix_is_found():
    """The shipped advisory note (knowledge/notes/spring-cloud.md) has a section for hystrix (default notes root)."""
    f = finding(notes=["see knowledge/notes/spring-cloud.md"])
    h = hints_for([err("package com.netflix.hystrix does not exist")], [f])[0][0]
    assert "Note (spring-cloud.md § " in h


def test_never_raises():
    class Broken:
        advisory_id = "x"

        @property
        def library(self):
            raise RuntimeError("boom")

    hints, ids = hints_for([err("package com.netflix.hystrix does not exist")], [Broken(), finding()])  # type: ignore[list-item]
    assert ids == ["hystrix"] and len(hints) == 1
