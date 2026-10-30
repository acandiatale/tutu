# legacy-external-parent — versions managed by the OLD in-house parent (v2.1l)

A Spring Framework 6.2.12 jar app (`@Configuration` + `@Service`, no web layer) whose as-is `<parent>` is the simulated old
in-house parent `com.company.legacy:legacy-parent:1.0.0` (`fixtures/libs/legacy-parent`, installed into the local
repository by `fixtures/libs/install.sh`). The parent manages `org.apache.commons:commons-text:1.10.0` and
`org.apache.commons:commons-csv:1.10.0`; the project declares both **without a version** and uses them
(`ReportService`).

The company shape behind it (2026-10-30): once the restructure moves the pom to the role parents, every version the old
parent managed is gone — the Spring Boot 4.1 BOM manages neither library — and before v2.1l the build failed with
`'dependencies.dependency.version' for org.apache.commons:commons-text:jar is missing`, the LLM answered "cannot add a
version by policy", and the run ended after rounds of dependency-adding answers.

Expected: `spring_legacy`, `restructure.apply` (layout) → `extparent-back` / `extparent-boot`, the two versions pinned
from the as-is effective pom (`logs/depmgmt.json`) in `extparent-back/pom.xml`, `spring-context` left to the Boot BOM
(explicit 6.2.12 dropped), BUILD SUCCESS with ONE LLM call (the structure review), the Handover listing both as
⚠ carried over.

As-is build (needs the stand-in parent installed):

    fixtures/libs/install.sh
    JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home mvn -q -DskipTests package

v2.1n (optional triggers): the fixture's `sbmig.e2e.yml` gives the local optional `commons-lang3` a `when_present` trigger on
`commons-text` (`replaces: false`), so the restructure also ADDS `org.apache.commons:commons-lang3:3.17.0` to
`extparent-back/pom.xml` (journal `optional_triggers`); the company mapping (`hibernate-core` → the in-house JPA starter) uses
the default `replaces: true`, covered by unit tests.
