# Recipes — what runs and how the list is managed

sbmig runs **one OpenRewrite run** (`transform.rewrite`) before the restructure, with no build in between. Only the
Java 21, javax → jakarta and Spring 7 / Boot 4 recipe sets run; everything else surfaces as a build error that the
script fix loop fixes (business rule: "레시피가 무엇을 바꿀지 모르니 넣지 않는다").

## Where recipe ids live

| Source | Decides | Read by |
|---|---|---|
| `knowledge/recipes/catalog.yml` | **What a recipe id is** — the single source of recipe ids: artifact, `provided_by`, description, docs, `covers`, `gate`, `sets` | `sbmig/plan/catalog.py` (`RecipeCatalog`), `sbmig recipes …` |
| `config/default.yml` → `openrewrite.recipes.*` (`RecipeSetConfig` in `sbmig/core/config.py` mirrors it) | **What the planner runs** per case | `sbmig/plan/planner.py` (`_case_buckets`, `transform_recipes`) |
| `config/default.yml` → `openrewrite.recipe_artifacts`, `recipe_bom_version`, `recipe_artifact_versions` | Which recipe jars are on the plugin classpath, at which versions | `sbmig/rewrite/runner.py`, `sbmig/rewrite/versions.py` |

The catalog does not change what runs; it describes every id the config sets may reference, and
`sbmig recipes check` fails when the two drift apart. Only `origin: openrewrite` entries are allowed (no project,
library or KB recipes).

## The sets and when they run

| Set | Recipes | Planned for |
|---|---|---|
| `jdk21` | `UpgradeToJava21` | every project |
| `jakarta` | `JavaxMigrationToJakarta` | `spring_legacy`, `servlet_tomcat`; `pure_java` only with Java EE `javax` imports (Boot projects: the umbrella does it) |
| `servlet_jakarta_10` | `RemovalsServletJakarta10` | every `servlet_tomcat`; `pure_java` with `javax.servlet` imports; `spring_legacy` only when `spring_framework_7` is empty (that recipe already includes it) |
| `spring_framework_7` | `UpgradeSpringFramework_7_0` | every `spring_legacy` (the restructure makes it a Boot app and the Boot 4.1 BOM brings Spring 7 / Jackson 3) |
| `spring_security_7` | `UpgradeSpringSecurity_6_0` … `6_5`, `UpgradeSpringSecurity_7_0` | `spring_legacy` whose analyzer found `spring-security` ("쓰는 경우만"). `6_0` chains 5_8 → 5_7; `6_1` … `7_0` do not chain, so all are listed |
| `spring_batch_6` | `SpringBatch5To6Migration` | `spring_legacy` with `spring-batch` |
| `hibernate_7` | `MigrateToHibernate71` | `spring_legacy` with native Hibernate ORM (`hibernate`; a JPA-only project has `jpa` and gets nothing) |
| `spring_boot_4` | `UpgradeSpringBoot_4_0`, `SpringBootProperties_4_1` | `spring_boot` projects. The umbrella already runs Framework 7, Security 6.x / 7, Spring Cloud 2025.1, Batch 6, Hibernate 7.1, springfox → springdoc, Sleuth → Micrometer (its catalog `covers`) |
| `junit5` | empty | off — tests are out of scope (`-Dmaven.test.skip=true`) |
| `extra.<case>` / `extra.all` | empty by default | company additions per case |

Run order inside the one run (`_BUCKETS`): Java 21 → jakarta (+ Servlet 6 removals) → Framework 7 → Security chain →
Batch 6 → Hibernate 7.1 → Boot 4 (Boot projects only) → JUnit 5 → extra. MSA: per service with its own sub-case.
Every decision is a plan note (`transform: spring_legacy → jakarta set + Spring Framework 7 set + Spring Security set
… (spring-security detected)`); an empty set is noted, not planned. `sbmig recipes sets` prints the sets and what each
case gets.

**Optional LLM plan advisor** (`plan.advisor: llm`, default `none`): `advise_plan` asks `llm.recipe_model`
(`LLM_RECIPE_MODEL`, else `LLM_MODEL`) to review the rule-based plan. It can only **add** recipes: catalog entries with
`advisor: true` inside the configured sets (`advisor_whitelist`), not already planned or covered by a planned
composite (`covers`), and backed by the entry's `gate` on the profile (`has_javax_ee`, `has_spring_security`, `is_boot`,
… — `GATES` in `sbmig/plan/catalog.py`). Anything else is ignored with a plan note; the call never fails the plan.

## Recipe BOM pin

`openrewrite.recipe_bom_version: "3.37.0"` (invariant 5). 3.38.0 points at artifacts Maven Central does not have, so
every run fails dependency resolution; 3.37.0 resolves rewrite-spring 6.37.0 / rewrite-migrate-java 3.42.0.
`recipe_artifact_versions` overrides one artifact's version; otherwise the BOM decides.

## Re-filling or changing a set in a company config

A company yml may empty a set (`[]` = off), fill `junit5`, or add `extra.<case>` ids. Every id must have a catalog
entry, and the entry's `sets:` must list the set name; a set emptied by the company config must be dropped from the
entries' `sets:` too. Otherwise `sbmig recipes check` reports the drift. Steps for a new id:

1. Verify the id on docs.openrewrite.org or in a recipe jar at the pinned BOM.
2. Add the catalog entry (`artifact`, or `provided_by` when a configured artifact brings it in transitively; core
   recipes `artifact: null`).
3. Add it to the config set and the set name to the entry's `sets:`.
4. `sbmig recipes check` → `recipe catalog OK`; `sbmig recipes check --jars` → `recipe jars OK`;
   `python -m pytest tests/plan` (`tests/plan/test_catalog.py` runs the same check).

## Catalog entry

```yaml
- id: org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0
  origin: openrewrite            # the only supported origin
  artifact: org.openrewrite.recipe:rewrite-spring   # null: core recipe bundled with rewrite-maven-plugin
  provided_by: null              # optional: configured artifact that brings `artifact` in transitively
  phase: spring                  # informational
  description: Spring Boot 2.x/3.x → 4.0 umbrella (Boot projects only)
  docs: https://docs.openrewrite.org/recipes/java/spring/boot4/upgradespringboot_4_0
  covers: [org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta, ...]   # what the composite already includes
  gate: is_boot                  # named predicate in sbmig/plan/catalog.py GATES, or null
  advisor: true                  # may the optional plan advisor add it?
  sets: [spring_boot_4]          # config sets that reference it (checked)
  verified: "2026-10"
  notes: knowledge/notes/boot3-to-4.md
```

The catalog also lists building blocks (`ChangeType`, `ChangeTagValue`, `ChangeDependencyGroupIdAndArtifactId`,
`RemoveDependency`, `UpgradeDependencyVersion`, …) with `sets: []` for company `extra` sets, and the library
recipes the umbrella runs (springfox, Sleuth, Spring Cloud 2025.1, HttpClient 5, Jackson 1 → 2, commons-collections 4).
If the catalog file is missing or unreadable, `default_catalog()` logs a warning and uses a small built-in catalog;
planning never fails because of it.

## `sbmig recipes check`

Offline. Reports ids in config sets missing from the catalog, `sets:` fields that disagree with the config, unknown
gates, `covers` ids not in the catalog, origins other than `openrewrite`, and artifacts not in
`openrewrite.recipe_artifacts` (core `org.openrewrite:` recipes and configured `provided_by` exempt). Exit 1 on a problem.

**`--jars`** (`sbmig/plan/jarcheck.py::check_jars`) then resolves each configured recipe artifact through
`rewrite-recipe-bom:<recipe_bom_version>` with `maven-dependency-plugin build-classpath`, scans the jars
(declarative `META-INF/rewrite/*.yml` names, imperative classes) and reports an id that no jar declares, an id
declared in a different jar than its `artifact`, or a `provided_by` that does not bring `artifact` in. Needs Maven,
JDK 21 and network (or a warm local repository); writes only under `--work-dir` (default `out/recipes-check/`).
`scripts/e2e.sh` runs it before the fixtures.

## Download failures during the recipe run

Through a proxy that answers 429 / 5xx the plugin can fail in two ways; both use `openrewrite.download_retry`
(default 1, 0 = off) and `openrewrite.download_retry_wait_s` (default 30):

- **The run fails** with transfer wording (`Could not transfer artifact`, `status code: 429`, `Connection reset`,
  `Read timed out`, …): `OpenRewriteRunner` waits and re-runs (journal `rewrite_download_retry` /
  `rewrite_download_retry_result`). `Could not find artifact` is not retried. An `OutOfMemoryError` gets one retry with
  `oom_retry_jvm_args`; a project that does not compile under the forked `compile` gets one `runNoFork` retry.
- **The run succeeds but leaves literal versions** (`4.0.x`, `latest.release`, …) in a changed pom: the orchestrator
  resets the tree, waits and re-runs once (journal `rewrite_unresolved_versions`). If they remain, the run is flagged
  `rewrite-unresolved-versions` with a report follow-up, and the build loop goes on.

A failing recipe run never stops the migration (the build shows the real problem), except a recipe id the plugin
does not know, which is a configuration error.

## CLI

```
sbmig recipes list [--json] [-c cfg.yml] [--catalog file]
sbmig recipes show <id> [--json]       # entry + the config sets that reference it
sbmig recipes check                    # catalog ↔ config sets ↔ artifacts; exit 1 on problems
sbmig recipes check --jars [--work-dir DIR] [--json] [--artifact g:a ...]
sbmig recipes sets [--json]            # config sets, and per case the transform run
sbmig rewrite <dir> ...                # run recipes on an existing directory (debugging)
```
