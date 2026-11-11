# boot-asis-parent-unpublished — the as-is version of the company parent is not in the repository (v2.1z-ag)

The `company-boot-single` sources (Spring Boot / Java 11, one jar module `com.example:shop`), but the pom inherits
`org.springframework.boot:spring-boot-starter-parent:2.7.18-NOT-PUBLISHED` — the g:a of the local stand-in for the company
parent (`artifacts.parent` in `config/artifacts.local-e2e.yml` = `spring-boot-starter-parent:4.1.0`) at a version no
repository serves. It stands for the company run of 2026-11-11: a project already on the in-house parent g:a at an old
`1.0.0-SNAPSHOT` that Nexus does not have; OpenRewrite (on the as-is poms) failed and the run aborted as "configured parent
not resolvable" although the configured 4.0.0 resolves.

Migration (`openrewrite.unresolvable_asis_parent: align`, the default):

* the intake's as-is `help:effective-pom` fails (one line: the parent is not in the repository) — non-fatal;
* the first OpenRewrite run fails on the unresolvable parent; sbmig sets the configured version `4.1.0` in `pom.xml`
  (commit `transform: as-is parent org.springframework.boot:spring-boot-starter-parent 2.7.18-NOT-PUBLISHED → 4.1.0 (as-is
  version not in the repository)`, journal `asis_parent_aligned`, flag `asis-parent-aligned`, one Handover line) and runs
  the recipes once more — on the target parent's classpath;
* then the company layout as in `company-boot-single` (wrapped into `shop-boot`).

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, no `aborted:parent-not-found`, no `infra:repository`,
**1 LLM call** (the structure review, verdict `ok`), no fix script.

The as-is build fails by design (the parent version does not exist):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
