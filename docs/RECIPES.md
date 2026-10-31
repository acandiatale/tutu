# Recipes — how recipe lists are managed

sbmig runs OpenRewrite recipes first and the LLM build-fix loop second. Recipe ids show up in six places.
Each place owns a different decision. One of them, `knowledge/recipes/catalog.yml`, is the **managed list**:
every recipe id the other sources use must have an entry there, and `sbmig recipes check` fails when they drift apart.

## The six sources

| # | Source | Authoritative for | Read by |
|---|--------|-------------------|---------|
| 1 | `knowledge/recipes/catalog.yml` | **What a recipe id is**: origin, artifact or file, default phase, description, docs link, what a composite `covers`, the profile `gate`, whether the LLM advisor may add it (`advisor`), and when it was verified | `sbmig/plan/catalog.py` (`RecipeCatalog`), the planner's `ADVISOR_CATALOG` / `ADVISOR_COVERAGE` / `ADVISOR_GATES`, `sbmig recipes …` |
| 2 | `config/default.yml` → `openrewrite.recipes.{jdk21, jakarta, servlet_jakarta_10, spring_boot_4, spring_framework_7, spring_security_7, spring_batch_6, hibernate_7, junit5, extra}` | **What the rule-based plan runs** for each case, and in what order (company configs may override these; `RecipeSetConfig` defaults mirror the yml) | `sbmig/plan/planner.py::spring_recipes` |
| 2b | `knowledge/recipes/library_recipes.yml` (round 10) | **Library-conditional plan recipes** for plans without the Boot umbrella: detected library → recipes appended to the case's `spring.rewrite`; checked as sets `library.<library>` | `sbmig/plan/library_recipes.py`, `planner._library_refs` |
| 3 | `knowledge/recipes/base/*.yml` | **The body of project-owned composites** (`com.company.migrate.*`), always composed into the run's `rewrite.yml` | `sbmig/rewrite/compose.py`, `planner.recipe_ref` |
| 4 | `sbmig/triage/recipe_rules.yml` | **Reactive recipes**: error pattern → builtin recipe, tried by the fix loop before the LLM | `sbmig/triage/rules.py` |
| 5 | `kb/recipes/{candidates,active,rejected}/*.yml` + `.meta.json`, `kb/patterns/*.yml` | **Learned recipes** (status, credits, signatures), plus hand-written signature → recipes/handlers/hints patterns | `sbmig/kb/store.py::FileKnowledgeBase` |
| 6 | `config/default.yml` → `openrewrite.recipe_artifacts`, `recipe_bom_version`, `recipe_artifact_versions` | **Which recipe jars are on the plugin classpath**, and which versions | `sbmig/rewrite/runner.py` |

KB recipes (source 5) are never listed in the catalog file. They change with every project, so the catalog reads
them live (`RecipeCatalog.with_kb`, `sbmig recipes list --kb`).

The catalog does **not** change what runs. The plan still comes from source 2 and reactive recipes from source 4.
The catalog's `sets` and `rules` fields mirror those sources, and `check` keeps the mirror honest.

## Catalog entry

```yaml
- id: org.openrewrite.java.spring.boot4.UpgradeSpringBoot_4_0
  origin: openrewrite        # openrewrite | project
  artifact: org.openrewrite.recipe:rewrite-spring   # null: project recipe, or a core recipe bundled with the plugin
  provided_by: null          # optional: a configured artifact that brings `artifact` in transitively
                             # (verified by `recipes check --jars`)
  file: null                 # project recipes: knowledge/recipes/base/<file>.yml
  phase: spring              # jdk21 | spring | restructure (default phase)
  description: Spring Boot 2.x/3.x → 4.0 umbrella (Boot projects only)
  docs: https://docs.openrewrite.org/recipes/java/spring/boot4/upgradespringboot_4_0
  covers: [org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta, ...]
  gate: is_boot              # named predicate in sbmig/plan/catalog.py GATES, or null
  advisor: true
  sets: [spring_boot_4]      # openrewrite.recipes sets (or library.<library> rules) that reference it (checked)
  rules: [boot4-modularized-autoconfigure]   # recipe_rules.yml rules that use it (checked)
  verified: "2026-10"
  notes: knowledge/notes/boot3-to-4.md
  advisories: []             # library advisory ids this recipe resolves (checked against advisories.yml)
```

The catalog also lists building-block recipes that project composites use internally, such as `ChangeType`,
`UpgradeParentVersion` and `UpgradeDependencyVersion`. These entries have `advisor: false`.

If the catalog file is missing or unreadable, `default_catalog()` logs a warning and falls back to a small
built-in catalog that matches the advisor constants. Planning never fails because of the catalog.

## Lifecycle of a recipe

1. **Verified upstream.** Confirm the id on docs.openrewrite.org, or in a recipe jar at the configured BOM version
   (`sbmig recipes check --jars` does the jar lookup for every catalog entry).
2. **Catalog entry.** Add the id to `catalog.yml` with its artifact, description, `verified` date and, if needed,
   `covers` and `gate`.
3. **Make it run.**
   * In every plan for a case: add it to an `openrewrite.recipes.<set>` in config, then add that set to `sets`.
   * Only when an error appears: add a rule to `recipe_rules.yml`, then add that rule to `rules`.
   * Advisor only: set `advisor: true` and pick a `gate`.
4. **Project composite** (optional). If upstream recipes need parameters or need to be chained, write a
   `com.company.migrate.*` recipe in `knowledge/recipes/base/*.yml`. Give it an `origin: project` catalog entry
   with `file:`. Every `recipeList` id it uses needs a catalog entry too (a building block: `advisor: false`,
   `sets: []`; core recipes have `artifact: null`).
   Prefer building blocks that need no network. A `newVersion` (exact or `x.y.x`) makes
   `ChangeDependency` / `ChangeDependencyGroupIdAndArtifactId` download maven-metadata first. When that download
   fails (HTTP 429 through the cloud proxy), the recipe silently leaves the dependency unchanged. Instead:
   * swap the coordinates without `newVersion`;
   * set any version text with `org.openrewrite.xml.ChangeTagValue` and an XPath predicate
     (`/project/dependencies/dependency[artifactId='x']/version`);
   * let `RemoveRedundantDependencyVersions` (`onlyIfManagedVersionIs: ANY`) drop the version when a BOM manages it.

   A later step in the same composite only sees the refreshed pom model after an
   `org.openrewrite.maven.ChangeDependencyGroupIdAndArtifactId` swap. The `org.openrewrite.java.dependencies.ChangeDependency`
   wrapper does not refresh it (verified 2026-10-20, rewrite-maven 8.89.0).
5. **Learned in the KB.** Recipes can also start in the KB. After a successful fix, harvesting may produce a
   **candidate** in `kb/recipes/candidates/`, either from the LLM (`kb.generate_recipe_candidates`) or straight
   from labelled fixes (`kb.deterministic_candidates`). Candidates are validated against a whitelist of declarative
   recipe types.
6. **Candidate → active.** Credits move a candidate to active automatically (see below). `sbmig kb promote <id>`
   does the same by hand, and `sbmig kb reject <id>` retires it.
7. **Permanent** (optional). An active KB recipe that proves generally useful can be copied into
   `knowledge/recipes/base/` and given a catalog entry (and a config set or rule). It is then versioned with the
   code instead of living in `kb/`.

## Adding a new recipe

1. Verify the id and the artifact it lives in. If that artifact is not in `openrewrite.recipe_artifacts`, add it
   there, or set `provided_by` when a configured artifact brings it in transitively (verified by
   `recipes check --jars`).
2. Add the catalog entry.
3. Reference it from a config set, a reactive rule, or a base yml, as needed.
4. Run `sbmig recipes check`. It must print `recipe catalog OK`.
5. Run `sbmig recipes check --jars` (needs Maven and network). It must print `recipe jars OK`.
6. Run `.venv/bin/python -m pytest tests/plan`. `tests/plan/test_catalog.py` runs the same check on the repo.

`check` reports:

* ids in config sets, reactive rules, or base yml (declared names and `recipeList` ids) that are missing from the catalog;
* `sets` / `rules` fields that disagree with the config or `recipe_rules.yml`;
* project entries whose `file` does not exist or does not declare the id;
* unknown gate names and `covers` ids that are not in the catalog;
* artifacts that are not in `openrewrite.recipe_artifacts`. Core `org.openrewrite:` recipes are exempt, and so are
  entries whose `provided_by` is configured.

`check` is offline: it trusts the `artifact` and `provided_by` fields. **`check --jars`** verifies them against the
real recipe jars (`sbmig/plan/jarcheck.py::check_jars`). It runs the offline check first and stops if that fails.
Then it:

1. Resolves each configured `openrewrite.recipe_artifacts` entry once, plus any `provided_by` that is not
   configured. It reads the versions from `rewrite-recipe-bom:<recipe_bom_version>` (or the
   `recipe_artifact_versions` overrides), writes `<work-dir>/<group>__<artifact>/pom.xml` (packaging `pom`, the BOM
   imported, one dependency), and runs `maven-dependency-plugin:3.8.1:build-classpath` (runtime scope) on it. Output
   goes to `cp.txt` and `build-classpath.log` in the same directory.
2. Scans every jar on those classpaths once. Declarative recipes are the `name:` of each
   `type: specs.openrewrite.org/v1beta/recipe` document in `META-INF/rewrite/*.yml`. Imperative recipes are
   `<id with />.class`.
3. Checks every `origin: openrewrite` entry. Project entries are already validated offline, so they are skipped.

It reports a problem when:

* a configured artifact does not resolve (the message names the artifact and the log file);
* a recipe id is not declared in any resolved jar;
* the id is declared, but not in the jar of its `artifact` (the message says which jar declares it);
* `artifact` is null (a core recipe) but the id is declared outside `org.openrewrite:*` jars. Core ids are looked up
  in the union of all classpaths;
* `provided_by` is not in `openrewrite.recipe_artifacts`, or `artifact` is not on the transitive classpath of the
  `provided_by` artifact.

It needs Maven, JDK 21 and network access to Maven Central (or a warm local repository). It writes only under
`--work-dir` (default `out/recipes-check/` under the repo root), never into the repo tree or `kb/`. The first run
downloads the recipe jars. Later runs take about 15 s, which is mostly Maven start-up. `scripts/e2e.sh` runs
`recipes check --jars --work-dir out/recipes-check` before the fixtures.

Verified on 2026-10-18 with BOM 3.37.0: all 21 openrewrite entries are found where the catalog says, in
rewrite-migrate-java 3.42.0, rewrite-spring 6.37.0, rewrite-testing-frameworks 3.44.0, and the core recipes in
`org.openrewrite:rewrite-java` / `rewrite-maven` 8.89.0. `rewrite-java-dependencies` 1.60.2 (`UpgradeDependencyVersion`,
`ChangeDependency`) is on rewrite-migrate-java's transitive runtime classpath, so their `provided_by` holds.
Re-verified on 2026-10-20 with 29 openrewrite entries. The new entries are the core building blocks of
`HystrixToResilience4j`: `ChangeAnnotationAttributeName`, `AddOrUpdateAnnotationAttribute`,
`RemoveAnnotationAttribute` and `RemoveAnnotation` from rewrite-java; `ChangeTagValue` from rewrite-xml;
`ChangeDependencyGroupIdAndArtifactId`, `RemoveDependency` and `RemoveRedundantDependencyVersions` from
rewrite-maven. All are 8.89.0.

## Advisor whitelist and gates

With `plan.advisor: llm`, `advise_plan` asks `llm.recipe_model` to review the rule-based plan. The call is tagged
`role="plan_advisor"`. The advisor can only **add** recipes:

* The whitelist is every catalog entry with `advisor: true`, shown to the model in file order with its description.
  Any other id is ignored.
* An id that is already planned, or that a planned composite already includes (its `covers` list), is not added.
  The model is told about these covered ids in advance.
* A `gate` must hold on the `ProjectProfile`, otherwise the suggestion is dropped as "not warranted". Gates:
  `has_junit4`, `has_javax_ee`, `has_javax_xml_bind`, `has_xml_bind_any`, `has_spring_security`, `has_spring_batch`,
  `has_hibernate` (v2.1w), `is_spring`, `is_boot`, `has_tests`, `always`.
* Each added recipe is attached to `jdk21.rewrite` or `spring.rewrite` (or their `@service` variants). The artifact
  comes from the catalog.

`advise_plan(cfg=...)` reads `cfg.recipe_catalog_file`, which is `plan.catalog_file`. `ADVISOR_CATALOG`,
`ADVISOR_COVERAGE` and `ADVISOR_GATES` in `sbmig.plan` are snapshots of the repo catalog, taken at import time.

## Advisory recipes (library advisories)

Library advisories (`knowledge/libraries/advisories.yml`, see [ADVISORIES.md](ADVISORIES.md)) can name recipes for
policy `recipe`. Those ids must be catalog entries; the entry lists the advisory ids it resolves in
`advisories: [...]` (informational). `sbmig recipes check` cross-checks both directions: every advisory recipe is
in the catalog, and every `advisories: [x]` names an existing advisory that uses the recipe.

| recipe | advisory | inside `UpgradeSpringBoot_4_0`? |
|---|---|---|
| `org.openrewrite.java.springdoc.SpringFoxToSpringDoc` | springfox | yes (4_0 → 3_5 … 3_0 → 2_7 → 2_6) |
| `org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0` | springfox | yes (listed directly) |
| `org.openrewrite.java.spring.cloud2022.MigrateCloudSleuthToMicrometerTracing` | sleuth | yes (3_0 → UpgradeSpringCloud_2022) |
| `org.openrewrite.java.spring.cloud2025.UpgradeSpringCloud_2025_1` | spring-cloud-bom | yes (listed directly) |
| `com.company.migrate.HystrixToResilience4j` (project, `base/hystrix-to-resilience4j.yml`) | hystrix | no |
| `org.openrewrite.apache.httpclient5.UpgradeApacheHttpClient_5` (rewrite-apache, `provided_by` rewrite-spring; round 10) | httpclient4 | yes, and inside `UpgradeSpringFramework_7_0` (7_0 → 6_2 → 6_1 → 6_0) |
| `org.openrewrite.java.jackson.CodehausToFasterXML` (rewrite-jackson 1.28.4, `provided_by` rewrite-spring; round 10) | jackson1 | yes, and inside `UpgradeSpringFramework_7_0` (→ `UpgradeJackson_2_3`) |
| `org.openrewrite.apache.commons.collections.UpgradeApacheCommonsCollections_3_4` (rewrite-apache, `provided_by` rewrite-spring) | commons-collections3 | no |

They are `advisor: false`, so the LLM advisor never suggests them. They are listed under the umbrella's `covers`
(verified against rewrite-spring 6.37.1). Because of this, a Boot project gets them from the umbrella and the
finding records `recipe_source: plan`. For non-Boot plans (spring_legacy, MSA library roots), the planner
**prepends** them to `spring.rewrite` (`recipe_source: advisory`), so they run before the Framework/Boot recipes in
the same rewrite pass. Hystrix has no upstream recipe either; the project composite
`com.company.migrate.HystrixToResilience4j` fills the gap. The umbrella does not cover it, so every plan prepends it
(`recipe_source: advisory`), Boot projects included. Ribbon and Zuul have no recipe, so they stay `guide`.
Round 10: `httpclient4` and `jackson1` became `recipe` for the Spring cases only (`applies_to: [spring_legacy,
spring_boot, msa]`): the Framework 7 recipe and the Boot umbrella already run their recipes, so the finding records
`recipe_source: plan` and the Handover reports what was migrated. Pure Java / servlet projects get the guide-only
twins `httpclient4-direct` / `jackson1-direct` (no Spring chain migrates them there; see ADVISORIES.md).

## Recipe sets added in round 10 (2026-10-26)

All ids verified in the pinned jars (BOM 3.37.0, `sbmig recipes check --jars` OK, 2026-10-26); the catalog now has
50 entries (43 `origin: openrewrite`, 7 project recipes).

**Spring Security chain.** `openrewrite.recipes.spring_security_7` (planned for `spring_legacy` — MSA library roots
included — when `spring-security` is detected; the Boot umbrella runs the same steps through 3_0 … 3_5 and 4_0) is
the whole chain, in order. v2 (2026-10-26) emptied the set with the other non-Boot sets; **v2.1l (2026-10-30) makes it the
default again** ("security 는 쓰는 경우만": a project without Spring Security never gets it — the planner's gate is the
analyzer's `spring-security` library, so nothing is added to a project that did not use it):

**Spring Framework 7 (v2.1o, 2026-10-30).** `openrewrite.recipes.spring_framework_7` = `UpgradeSpringFramework_7_0` is a default
set for `spring_legacy` too (user: "넣는 게 좋을 것 같아"): a non-Boot project is made Boot by the restructure, and the Boot 4.1 BOM
brings Spring 7.0.8 / Jackson 3 — the Framework 6.x removals, Jackson 2 → 3 and the other code changes of that recipe were
left to the LLM before; Boot projects already ran the same recipe inside the umbrella.

| # | recipe | what it adds |
|---|---|---|
| 1 | `org.openrewrite.java.spring.security6.UpgradeSpringSecurity_6_0` | chains 5_8 → 5_7: `WebSecurityConfigurerAdapter` → `SecurityFilterChain` bean, lambda DSL, `authorizeRequests` / `antMatchers` → `authorizeHttpRequests` / `requestMatchers`, `@EnableGlobalMethodSecurity` → `@EnableMethodSecurity` |
| 2–6 | `…security6.UpgradeSpringSecurity_6_1` … `_6_5` | the 6.x deprecation removals, one step each |
| 7 | `org.openrewrite.java.spring.security7.UpgradeSpringSecurity_7_0` | version bump, `SecurityConfigurer` `init` / `configure` lose `throws Exception`, `AuthorizationManager.check` → `authorize` |

`UpgradeSpringSecurity_7_0` alone (before round 10) only bumped versions and dropped `throws Exception`. Every step is
a `Singleton`, so running them next to the umbrella costs nothing. The reactive rule `spring-security-config` is gone
from `sbmig/triage/recipe_rules.yml`: its type-attributed visitors found nothing on a Security 7 classpath — the plan
runs the chain on the as-is classpath instead. What the recipes cannot see is left to handlers:
`security-path-pattern-matcher` (`AntPathRequestMatcher` / `MvcRequestMatcher` → `PathPatternRequestMatcher`, E17) and
`add-known-dependency` (the `spring-security-access` split: `AccessDecisionManager`, `ConfigAttribute`,
`FilterSecurityInterceptor`, `WebExpressionVoter`, vote / intercept / method packages; managed by
spring-security-bom 7.1.0, the project's `${spring-security.version}` reused).

**Conditional sets (v2.1w, 2026-10-31, decision 7 — "쓰는 경우만", the Security rule).** Before, a `spring_legacy` project
got no Batch / Hibernate recipe and a `servlet_tomcat` project no Servlet 6 removals, so those API removals reached the
LLM with no help (docs/v2/PROMPT-REVIEW-2026-10-31.md §3). The planner (`_case_buckets`) now adds:

| set | recipe | planned for | gate (analyzer library / catalog gate) |
|---|---|---|---|
| `servlet_jakarta_10` | `org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10` | every `servlet_tomcat` project; `pure_java` with `javax.servlet` imports; `spring_legacy` only when `spring_framework_7` is empty (`UpgradeSpringFramework_7_0` already includes it) | — (in the `jakarta` bucket, right after `JavaxMigrationToJakarta`: it matches `jakarta.servlet` types) |
| `spring_batch_6` | `org.openrewrite.java.spring.batch.SpringBatch5To6Migration` (chains 4 → 5) | `spring_legacy` | `spring-batch` / `has_spring_batch` |
| `hibernate_7` | `org.openrewrite.hibernate.MigrateToHibernate71` (rewrite-hibernate 2.25.0, `provided_by` rewrite-spring) | `spring_legacy` | `hibernate` / `has_hibernate` — the analyzer's native-ORM rule (`hibernate-core` / `hibernate-entitymanager` / `org.hibernate.orm:*`, or `org.hibernate.Session` / `SessionFactory` / `query` / `type` / `cfg` / `annotations` imports); a JPA-only project has `jpa` only and gets nothing |

Run order of the one transform run (`_BUCKETS`): Java 21 → jakarta (+ Servlet 6 removals) → Framework 7 → Security chain →
Batch 6 → Hibernate 7.1 → (Boot 4, Boot projects only) → JUnit 5 → extra. Boot projects get none of the three (the umbrella's
`covers`); MSA: per service with its own sub-case (top-aggregator: the union). The plan note names the reason
(`+ Spring Batch 6 set SpringBatch5To6Migration (spring-batch detected)`); an empty set is noted, not planned. A company config
that empties a set must drop the set name from the catalog entry's `sets:` (invariant 5, `sbmig recipes check`).
The round-10 library table below (`library_recipes.yml`) was deleted with the v1 code (v2 step 4); these sets are its
v2 successor for Batch and Hibernate (Thymeleaf has none).

**Library recipes** (`knowledge/recipes/library_recipes.yml`, loader `sbmig/plan/library_recipes.py`). Boot projects get
these through `UpgradeSpringBoot_4_0`; plans without the umbrella got none of them, so e.g. Spring Batch 4 code met
the Batch 6 jars the Boot 4.1 BOM brings. A rule appends its recipes to the case's `spring.rewrite` when the analyzer
detected the library, with a plan note (`caveats`: a plan note when the as-is poms declare an artifact the recipes do
not migrate). Catalog entries list the rule as set `library.<library>`; `sbmig recipes check` / `sets` treat it like a
config set. A broken table means no library recipes (logged), never a failed plan.

| library | cases | recipes |
|---|---|---|
| `spring-batch` | spring_legacy | `org.openrewrite.java.spring.batch.SpringBatch5To6Migration` (chains `SpringBatch4To5Migration`) |
| `hibernate` | spring_legacy, servlet_tomcat, pure_java (pure Java: with the jakarta migration) | `org.openrewrite.hibernate.MigrateToHibernate71` (rewrite-hibernate 2.25.0, `provided_by` rewrite-spring) |
| `thymeleaf` | spring_legacy | `org.openrewrite.java.spring.boot3.MigrateThymeleafDependencies` (spring5 → spring6; caveat notes for `thymeleaf-spring4` / `thymeleaf-extras-springsecurity4`) |

**Spring 7 / Boot 4 leftovers** (`knowledge/recipes/base/spring-7-leftovers.yml`, project recipes):

| recipe | sets | content |
|---|---|---|
| `com.company.migrate.Boot4TypeRelocations` | `spring_boot_4` | 25 `ChangeType`s the 4.0 umbrella does not make: `ErrorController`, `ErrorAttributes`, `ErrorPage(Registrar)`, `ServletWebServerFactory`, `MultipartConfigFactory`, `OrderedCharacterEncodingFilter`, `ServerProperties`, `HttpMessageConverters`, `ServletComponentScan`, … (handler `boot4-type-relocation-sweep` covers the files OpenRewrite skipped from the same table, `boot4_types.yml` — keep both in sync) |
| `com.company.migrate.Spring7RemovedApis` | `spring_boot_4`, `spring_framework_7` | removes `setUseSuffixPatternMatch` / `setUseTrailingSlashMatch` / `setUseRegisteredSuffixPatternMatch`, `favorPathExtension` / `ignoreUnknownPathExtensions`; `HttpHeaders` `containsKey` → `containsHeader`, `keySet` → `headerNames`, `entrySet` → `headerSet` |
| `com.company.migrate.Jackson3SpringConverters` | `spring_boot_4`, `spring_framework_7` | `MappingJackson2HttpMessageConverter` → `JacksonJsonHttpMessageConverter`, `MappingJackson2JsonView` → `JacksonJsonView`, and the XML twins |

**Servlet 6 removals.** `org.openrewrite.java.migrate.jakarta.RemovalsServletJakarta10` (`encodeUrl` → `encodeURL`,
`getValue` → `getAttribute`, …) joined the `jakarta` set and `com.company.migrate.ServletToJakartaAndBoot`; the
Framework 7 recipe and the Boot umbrella already include it.

## Download failures during a recipe run (round 9)

The plugin resolves recipe jars and, for `UpgradeSpringBoot_4_0` & co., Maven metadata at run time. Through a proxy
that answers HTTP 429 / 5xx this fails in two ways, both handled with `openrewrite.download_retry` (default 1,
0 = off) and `openrewrite.download_retry_wait_s` (default 30):

* **The run fails** with transfer wording (`Failed to download`, `Could not transfer artifact`, `status code: 429`,
  `Too Many Requests`, `502` / `503`, `Connection reset`, `Read timed out`, `UnknownHostException`, …, matched in the
  error and the run log). `OpenRewriteRunner.run` waits and re-runs up to `download_retry` times (journal
  `rewrite_download_retry` / `rewrite_download_retry_result`, log `rewrite-<step>-dlretry.log`); a run that still
  fails has its error prefixed `download-retry: `. `Could not find artifact … in <repo>` (a genuinely missing
  artifact) is not retried. The OOM retry wins when the log shows both; the `compile` → `runNoFork` fallback
  still applies to the retried result.
* **The run succeeds but leaves literal versions** (`<version>4.0.x</version>`, `2025.1.x`, `latest.release`,
  `latest.integration`, `latest.patch`) in a changed `pom.xml` because the recipe could not read
  `maven-metadata.xml`. The orchestrator (`_step_rewrite`) resets the tree to the commit before the step, waits,
  and re-runs the step once (journal `rewrite_unresolved_versions {…, retry: true}`). Skipped when the tree had
  uncommitted changes before the step (a reset would lose them) and in dry runs. If the versions are still there,
  the result is kept, the run is flagged `rewrite-unresolved-versions`, the phase gets a note, the report a
  follow-up ("re-run sbmig later or set the versions by hand"), and the build loop runs as usual. The plan
  recipes of that phase are not credited the failure (next section).

## How KB recipes are credited and promoted

`FileKnowledgeBase.credit_recipe(id, success, project_fingerprint)` is the only place credits change:

* A success increments `success_count` and records the project fingerprint in `projects` (once per project).
* A failure increments `fail_count`.
* Then, if the recipe is a candidate, `kb.auto_promote_after_projects` (N, default 2) is > 0, `fail_count == 0`,
  `success_count > 0` and `len(projects) >= N`, it is promoted to `active`. Setting N to 0 turns this off.

Credits come from three places:

* **Plan runs.** With `kb.try_candidates_in_plan`, active and candidate recipes run in the spring OpenRewrite step.
  Any that changed files are credited with that phase's build result (orchestrator `_credit_plan_recipes`).
  A phase that ran no build is never credited, and since round 9 a *failed* phase is not credited when it failed on
  dependency resolution rather than on code (journal `kb_credit_skipped {phase, recipes, reason}`): the flag
  `rewrite-unresolved-versions` was raised in it, every remaining error is `dependency_resolution` /
  `parent_resolution` / `plugin_resolution` (or an OpenRewrite failure prefixed `download-retry:`), or — with no
  code error left — every blocked item is `artifact_not_found` / `parent_not_found`. Success credit is unchanged.
* **Fix loop.** KB recipes tried for an error cluster are credited by the attempt's outcome (round 10: `partial` and
  `inconclusive` prove neither and credit nothing; journal `kb_credit_skipped` with `reason`).
* **Harvest.** `KB_RECIPE` decisions are credited when the phase is harvested.

Active recipes are appended to every plan's spring step, and the fix loop tries them when their signatures match.
Candidates are tried after active recipes and before the LLM.

## CLI

```
sbmig recipes list [--kb] [--json] [-c cfg.yml] [--catalog file]   # table: id, origin, phase, artifact/file, gate, advisor, sets, rules
sbmig recipes show <id> [--json]       # entry + config sets / rules / composites that reference it + KB meta
sbmig recipes check                    # cross-check every source; exit 1 on problems (CI / e2e)
sbmig recipes check --jars [--work-dir DIR] [--json] [--artifact g:a ...]
                                       # + every openrewrite id looked up in the real recipe jars (Maven + network)
sbmig recipes sets [--json]            # config sets, and per case what jdk21/spring rewrite steps get
sbmig kb list | show | promote | reject | stats …   # learned recipes (see `sbmig kb --help`)
```
