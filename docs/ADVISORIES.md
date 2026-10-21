# Library advisories

Some libraries are end of life or are no longer version-managed by the new parent or the Boot 4 BOM, but they
**still compile**. Examples are springfox, Hystrix, Sleuth and old Spring Cloud trains. Without advisories a
migration run that keeps them looks green. Advisories make these libraries visible.

The goal is not to fail the run. With 500 projects, not every one can be perfect. The run builds as far as it can.
Then it hands the owning team a compressed summary: the versions we replaced, and the work that is now theirs
("from here on, your team replaces X with Y").

Files:

* `knowledge/libraries/advisories.yml`: the curated list.
* `sbmig/advisory/`: catalog, evaluation, planner hook, verification and CLI.
* `knowledge/notes/spring-cloud.md`: fix-loop guidance for Netflix, Sleuth and springfox errors.

## Policies: `guide` and `recipe` (deliberately no `block`)

| policy | what happens |
|---|---|
| `guide` (default) | The build proceeds normally. The finding shows up in the report's Handover section, in `manual_followups` and in the ledger (`resolution=advisory`). |
| `recipe` | Same as guide. In addition, the listed OpenRewrite recipes run in `spring.rewrite`. If any recipe is unavailable, the policy falls back to `guide`. |

An advisory never fails a run and never changes the exit code. If a library cannot be replaced automatically, the
handover says what is left.

## Schema (version 1)

```yaml
version: 1
advisories:
  - id: springfox                      # unique; later files override by id
    library: springfox                 # analyzer rule name (sbmig/analyze/libraries.py LIBRARY_NAMES)
    artifacts: ["io.swagger:swagger-annotations"]   # extra g:a globs (dependency, dependencyManagement or parent)
    status: eol                        # eol | unmanaged | incompatible | superseded
    title: Springfox (Swagger 2) is discontinued
    summary: one paragraph for the report
    replacement: org.springdoc:springdoc-openapi-starter-webmvc-ui
    replacement_version: "3.x (Boot 4)"
    policy: recipe                     # guide | recipe
    recipes: [org.openrewrite.java.springdoc.SpringFoxToSpringDoc, org.openrewrite.java.springdoc.UpgradeSpringDoc_3_0]
    followup: the "from here on, your team …" sentence (concrete, short)
    docs: [https://springdoc.org/#migrating-from-springfox]
    applies_to: []                     # case types (empty = all)
    applies_when_version_below: null   # e.g. "2025.1" (unparsable versions such as Hoxton.SR12 count as below)
    resolved_min_version: null         # still present but at least this version → outcome "replaced"
    notes: knowledge/notes/spring-cloud.md
```

The model is `LibraryAdvisory` in `sbmig/core/models.py`. A finding (`AdvisoryFinding`) is what lands in
`report.json` under `advisories` and in the ledger.

## How it works

1. **Evaluate (after analyze).** `evaluate_advisories(profile, cfg)` decides which entries match:
   * An entry matches when `profile.has_library(library)` is true, or when an `artifacts` glob matches a
     dependency, a dependencyManagement entry or a parent of any pom.
   * The detected version is the library version or the matched coordinate's version. Only concrete versions
     count; `${…}` references are ignored.
   * Gates: `applies_to`, then `applies_when_version_below`.
   * Policy resolution, in order:
     1. `policy_overrides[id]` beats the catalog policy. `off` drops the entry.
     2. `allow_recipes: false` turns every entry into `guide`.
     3. Each recipe must be in the recipe catalog, with its artifact in `openrewrite.recipe_artifacts` (or
        `provided_by`, or core). Otherwise the entry becomes `guide`.

     `policy_reason` records why: `catalog`, `config override`, `advisories.allow_recipes=false`, or
     `recipe <id> unavailable → guide`.
   * Findings are ordered eol, incompatible, unmanaged, superseded, then by id.
2. **Plan.** `build_plan(..., advisories=findings)` handles each `spring.rewrite` step (single, MSA per service,
   or MSA single root) the same way:
   * It calls `advisory_recipe_refs(findings, cfg, planned_ids)`.
   * A recipe that is already planned, or that a planned composite `covers`, is not added. The finding then gets
     `recipe_source: plan`.
   * Missing recipes are **prepended**, so they run before the umbrella in the same pass. The finding then gets
     `recipe_source: advisory`.
   * Plan notes: `advisory: <id> → <recipes> (added)`, `(covered by the plan)` or `advisory: <id> → guide (<reason>)`.
   * MSA per-service steps only consider advisories whose library that service uses.
   * A `recipe` finding that no `spring.rewrite` step took becomes `guide`. This happens with pom-only projects or
     with a module that sits outside every service.
   * The LLM plan advisor treats advisory recipes as already planned.
3. **Verify (finalize, agent B side).** `verify_advisories(findings, view, cfg)` re-detects libraries on the final
   tree and sets the outcome:
   * `replaced`: the library is no longer detected, or it is still declared at `resolved_min_version` or higher.
   * `still-present`: the library is still declared or imported.
   * `unknown`: the re-scan failed.
4. **Handover.** `followups_for(findings)` returns one sentence per finding, not-replaced findings first:
   * Not replaced: `"<title>: <followup> (replacement: …)"`.
   * Replaced: `"<library> → <replacement> applied by <recipes>; <followup>"`.

## Code the recipe cannot convert: advisory-guided LLM fixes

A `recipe` advisory swaps the library, but some code has no deterministic rewrite (e.g. a programmatic
`class X extends com.netflix.hystrix.HystrixCommand<T>`; `HystrixToResilience4j` only converts `@HystrixCommand`).
After the swap that code stops compiling and lands in the build-fix loop. Round 6 makes the LLM migrate it to the
replacement, and makes the report say which files it rewrote for which library.

1. **Match (per cluster).** `sbmig.advisory.hints_for(cluster.errors, report.advisories, profile)` →
   `(hints, advisory_ids)`. A finding matches when an error's message / symbol / location / raw text / artifact
   mentions an import package of its library (`LibraryRule.imports` in `sbmig/analyze/libraries.py`, e.g.
   `com.netflix.hystrix`, segment boundary), one of its as-is artifacts (`g:a`), or its replacement artifact
   (`g:a`, or the artifactId alone when it contains a dash). Errors that only name a project class
   (`cannot find symbol method execute()` in the caller) do not match. Pure, never raises.
2. **Hint text.** One line per matched finding, ≤ 500 chars:
   `<library> (<status>, policy <policy>): replace with <replacement> (<replacement_version>). <followup>` (the
   title when there is no follow-up), plus an excerpt (≤ 600 chars) of the advisory's note (`notes:` →
   `knowledge/notes/<file>.md`): a heading `## fix-loop: <library>` wins, otherwise the first heading that names
   the library. Use the `fix-loop:` convention for loop guidance written for the LLM (how to port the leftover
   code); the hint works from replacement + follow-up alone when there is none.
3. **Prompt.** `BuildFixLoop(advisories=report.advisories)` fills `FixContext.advisory_hints`;
   `ContextPacker` renders the section "Library migration guidance (what to migrate TO)" after the sources / pom
   and before the KB hints (truncated after the KB hints, before pom and sources). The system prompt
   (`sbmig/fix/prompts/system_v1.md`, rule 9) says: those libraries were removed on purpose — port the code to the
   replacement (smallest port that compiles, public signatures kept, `// MIGRATION-TODO:` for behaviour) instead of
   re-adding the removed library.
4. **Record.** `FixAttempt.advisory_ids` (set before `propose`); the journal `attempt` event carries
   `advisory_ids`, the fixer's `llm_fix` event `advisory_hints` (number of guidance lines that reached the prompt),
   the sbmig.log DEBUG lines "advisory hints for <cluster>" / "advisory guidance in the prompt"; the ledger line of
   the attempt (`resolution: llm_patch`) carries `notes: ["advisory:<id>", …]` (same token as the
   `resolution: advisory` lines; the ledger model is unchanged); the attempts table shows `` `model` + advisory <id> ``.
5. **Attribute (finalize, before the Handover).** `Orchestrator._attribute_llm_fixes`: successful LLM attempts
   (`llm_fix`, applied, outcome `improved` / `build_ok`, cluster not left unresolved — the KB harvest predicate)
   whose `advisory_ids` contain the finding → `AdvisoryFinding.llm_fixed_files` (changed paths, as recorded by the
   attempt, i.e. before the restructure moved them) and `llm_fix_notes` (first sentence of each analysis,
   ≤ 160 chars).
6. **Handover.** "From here on, your team" gets, right after the advisory lines, one line per such finding:
   `code rewritten by the LLM for <library>: <files> — <notes> — review these changes (changes.patch, attempts table)`.

Fixture: `boot2-hystrix-command` (replayed LLM; 2 clusters → 2 calls, the first with the hystrix hint).

Note for the knowledge owners: `knowledge/notes/spring-cloud.md` and the "manual" list above still tell the loop to
re-add `com.netflix.hystrix:hystrix-core:1.5.18` for `HystrixCommand` subclasses ("Do not hand-write a
Resilience4j port inside the loop"). That contradicts this section; the note is also sent to the LLM as a
migration note. Replace it with a `## fix-loop: hystrix` section that describes the port (plain class,
`CircuitBreaker.ofDefaults(name)` / `executeCallable` / `decorateSupplier`, fallback via try/catch, keep
`execute()` / public signatures, `// MIGRATION-TODO:` for timeouts and thread-pool isolation).

## Company extras

```yaml
advisories:
  extra_files: [/etc/sbmig/company-advisories.yml]   # same schema, merged by id, later file wins
  policy_overrides: {springfox: guide, zuul: off}
  allow_recipes: true
```

Use extra files to add in-house EOL libraries (artifact globs such as `com.company:legacy-*` work without an
analyzer rule) or to rewrite a follow-up sentence for your teams.

## CLI

```
sbmig advisories list [--json] [-c cfg.yml]   # id | library | status | effective policy | replacement | recipes
sbmig advisories show <id> [--json]
sbmig advisories check                        # exit 1 on problems (unknown library, recipe policy without
                                              # recipes, recipe missing from the catalog / artifact not
                                              # configured, duplicate ids, bad applies_to)
sbmig recipes check                           # also cross-checks advisory recipes ↔ catalog `advisories:`
```

## Validating a new recipe before setting `policy: recipe`

1. Check that the recipe id exists in the configured jar: `unzip -p <rewrite-spring.jar> 'META-INF/rewrite/*.yml'`.
   Read its `recipeList` and preconditions, and find out whether `UpgradeSpringBoot_4_0` already reaches it.
2. Add a catalog entry (`advisor: false`, `advisories: [<id>]`). If the umbrella reaches the recipe, add it to the
   umbrella's `covers`. Then run `sbmig recipes check` and `sbmig advisories check`.
3. Run a fixture that uses the library with the fake LLM:
   `sbmig run <copy> -c config/artifacts.local-e2e.yml --stop-after spring`. Expect a green spring build, 0 LLM
   calls, the library gone from the pom and the replacement present.
4. Only then switch the entry to `recipe`. If the recipe leaves non-compiling code, keep the recipes listed but ship
   `guide`, so the team gets the pointer without a broken build.

### springfox validation (2026-10, rewrite-spring 6.37.1)

`fixtures/projects/boot2-cloud-legacy` (Boot 2.7.18, Spring Cloud 2021.0.9, springfox 3.0.0, sleuth, hystrix
2.2.10) was run up to `--stop-after spring` with the fake LLM and JDK 21:

* Both the jdk21 and spring builds passed on the first try, with 0 LLM calls.
* `io.springfox:springfox-boot-starter` was removed and `org.springdoc:springdoc-openapi-starter-webmvc-ui:3.0.3`
  was added. `@Api`/`@ApiOperation`/`@ApiParam` became `@Tag`/`@Operation`/`@Parameter`. The Docket bean became
  `springdoc.*` properties (`paths-to-match`, `packages-to-scan`).
* `spring-cloud-starter-sleuth` became `micrometer-tracing-bridge-brave` (no version, Boot-managed).
  `spring.sleuth.sampler.probability` became `management.tracing.sampling.probability`, and a traceId/spanId
  `logging.pattern.level` was added.
* `spring-cloud.version` went from 2021.0.9 to 2025.1.3. Hystrix stayed in the pom and still compiled (→ guide
  follow-up at the time; since 2026-10-20 the project recipe below replaces it).

The recipes left these behind:

* a dead private `apiInfo()` helper that returns `io.swagger.v3.oas.models.info.Info`;
* the unused `springfox.version` property;
* `spring.mvc.pathmatch.matching-strategy: ant_path_matcher`.

All of them compile. The springfox follow-up asks the team to review them. Result: springfox ships with
`policy: recipe`. For Boot projects all of its recipes come from the umbrella (`recipe_source: plan`).

### hystrix validation (2026-10-20, rewrite-java / rewrite-maven / rewrite-xml 8.89.0)

There is no upstream OpenRewrite recipe for Hystrix. The project composite `com.company.migrate.HystrixToResilience4j`
(`knowledge/recipes/base/hystrix-to-resilience4j.yml`) is built only from core building blocks. The Boot umbrella
does not cover it, so the planner prepends it to `spring.rewrite` for every plan (`recipe_source: advisory`). Its
`recipeList`, in order:

1. `ChangeAnnotationAttributeName` on `HystrixCommand`: `commandKey` → `name`.
2. `RemoveAnnotation @…javanica.annotation.HystrixProperty`. This removes the nested properties and their import
   (`RemoveAnnotationAttribute` does not touch imports).
3. `RemoveAnnotationAttribute` on `HystrixCommand` for `groupKey`, `threadPoolKey`, `commandProperties`,
   `threadPoolProperties`, `ignoreExceptions`, `observableExecutionMode`, `defaultFallback` and
   `raiseHystrixExceptions`. `fallbackMethod` stays.
4. `ChangeType` `HystrixCommand` → `io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker`.
5. `AddOrUpdateAnnotationAttribute` on `CircuitBreaker`: `name` = `"default"`, `addOnly: true`. A name that came
   from `commandKey` wins. This step must run after the type swap. On `HystrixCommand` the recipe skips, because
   the type is known and has no `name` attribute. After `ChangeType` the type is shallow, so the value goes in
   verbatim, which is why the quotes are part of `attributeValue`.
6. `RemoveAnnotation` for `@EnableHystrix`, `@EnableCircuitBreaker` and `@EnableHystrixDashboard`.
7. pom:
   * `xml.ChangeTagValue` sets the old starter's `<version>` to 5.0.3 (the 2025.1 train's line), so the pom stays
     resolvable.
   * `maven.ChangeDependencyGroupIdAndArtifactId` (no `newVersion`) swaps `spring-cloud-starter-netflix-hystrix` /
     `spring-cloud-starter-hystrix` → `spring-cloud-starter-circuitbreaker-resilience4j`.
   * `maven.RemoveDependency` removes `hystrix-javanica`, `hystrix-core` and the dashboard starter.
   * `RemoveRedundantDependencyVersions` (`onlyIfManagedVersionIs: ANY`) drops the version whenever a Spring Cloud
     BOM manages the starter.

`@CircuitBreaker` compiles without an extra dependency. The starter brings `resilience4j-annotations` through
spring-cloud-circuitbreaker-resilience4j → resilience4j-spring-boot3 → resilience4j-spring6.

**Why there is no `newVersion` in step 7.** A `newVersion` makes the swap download maven-metadata first. Through the
cloud proxy, 5 of 10 standalone runs left the Hystrix starter silently in the pom. The `MavenMetadataFailures`
data table showed HTTP 429 for the failing run in which it was exported. The steps above need no network beyond
normal pom resolution. A pom without a Spring Cloud BOM keeps an explicit 5.0.3.

Fixture `boot2-cloud-legacy` (now with `@EnableCircuitBreaker` and a `commandKey` / `commandProperties` /
`ignoreExceptions` method) gives these results:

* BUILD SUCCESS with 0 LLM calls; hystrix is `replaced`.
* `module/comp-jar/pom.xml` declares the resilience4j starter with no version.
* `@CircuitBreaker(name = "default", fallbackMethod = "stockFallback")` and
  `@CircuitBreaker(name = "inventoryReserve", fallbackMethod = "reserveFallback")`.

**What stays manual.** The team gets these through the follow-up text and `knowledge/notes/spring-cloud.md`:

* Runtime configuration:
  * Resilience4j instances (`resilience4j.circuitbreaker.instances.<name>` / `configs.default`);
  * `hystrix.command.*` timeouts → `resilience4j.timelimiter.*`;
  * a trailing `Throwable` parameter on every fallback (Resilience4j does not find fallbacks without it);
  * `spring-boot-starter-aspectj` for the annotation aspect;
  * lost thread-pool isolation (use a bulkhead if needed).
* Programmatic `HystrixCommand` / `HystrixObservableCommand` subclasses and `HystrixRequestContext`. If they no
  longer compile, the fix loop re-adds `hystrix-core:1.5.18`, and the advisory then reports hystrix as
  `still-present`.
* Hystrix dashboards and Turbine streams.

## Initial list

| id | status | policy | replacement | how |
|---|---|---|---|---|
| springfox | eol | recipe | springdoc-openapi-starter-webmvc-ui 3.x | SpringFoxToSpringDoc + UpgradeSpringDoc_3_0 (inside the Boot umbrella) |
| sleuth | eol | recipe | micrometer-tracing-bridge-brave (Boot-managed) | MigrateCloudSleuthToMicrometerTracing (inside the umbrella) |
| hystrix | eol | recipe | spring-cloud-starter-circuitbreaker-resilience4j (Spring Cloud BOM) | project composite `com.company.migrate.HystrixToResilience4j` (prepended; not in the umbrella) |
| ribbon | eol | guide | spring-cloud-starter-loadbalancer | no recipe exists |
| zuul | eol | guide | spring-cloud-starter-gateway-server-webmvc | no recipe exists |
| netflix-feign | eol | guide | spring-cloud-starter-openfeign | the umbrella renames the starter; com.netflix.feign code is manual |
| spring-cloud-bom | superseded | recipe | spring-cloud-dependencies 2025.1.x | UpgradeSpringCloud_2025_1 (inside the umbrella); only below 2025.1 |
| spring-security-oauth2-legacy | eol | guide | spring-boot-starter-oauth2-resource-server / -client | manual |
| log4j1 | eol | guide | logback (Boot default) or log4j2 | manual |
| commons-lang2 | eol | guide | org.apache.commons:commons-lang3 | manual |
| httpclient4 | superseded | guide | org.apache.httpcomponents.client5:httpclient5 | manual |
| jdk-removed-apis | incompatible | guide | java.util.Base64 / javax.imageio / jakarta.* API jars | `UpgradeToJava21` adds the JAXB / JAX-WS / common-annotations jars; the LLM ports `sun.misc` code with the `## fix-loop: jdk-removed-apis` hint (`knowledge/notes/jdk21.md`) |
| ibatis2 | incompatible | guide | org.mybatis:mybatis + org.mybatis:mybatis-spring | no recipe exists; the LLM ports `SqlMapClientDaoSupport` DAOs with the `## fix-loop: ibatis2` hint (`knowledge/notes/ibatis2.md`) |

The analyzer rule names added for these entries are `sleuth`, `hystrix`, `ribbon`, `zuul`, `netflix-feign`,
`spring-cloud-bom` (dependencyManagement / parent), `swagger-v1`, `spring-security-oauth2-legacy`, `log4j1`,
`commons-lang2`, `httpclient4`, `jdk-removed-apis` (import-only: `sun.misc`, `sun.security`, `com.sun.image`,
`javax.activation`; JAXB `javax.xml.bind` stays on the `jaxb` rule) and `ibatis2` (round 9).

### ibatis2: iBATIS 2 / SqlMapClient (round 9)

iBATIS 2 is common in Korean legacy Spring 2/3 apps. Spring 3.2 was the last line with `org.springframework.orm.ibatis`;
Spring 4.0 removed it. The iBATIS 2 jars stay on Maven Central, so dependency resolution still works, and the failure
is a compile error after the Spring upgrade (`package org.springframework.orm.ibatis does not exist`, `cannot find
symbol SqlMapClientDaoSupport`).

* Rule `ibatis2`: artifacts `org.apache.ibatis:ibatis-sqlmap`, `org.apache.ibatis:ibatis-core`, `org.apache.ibatis:ibatis*`,
  `ibatis:ibatis*`, `com.ibatis:*`; imports `com.ibatis`, `org.springframework.orm.ibatis`. It is distinct from `mybatis`
  (MyBatis 3: `org.mybatis:*`, `org.apache.ibatis.*` imports): a MyBatis 3 project is `mybatis` only, an iBATIS 2
  project is `ibatis2` only.
* Pom and imports only. Spring XML bean classes (`SqlMapClientFactoryBean` in a `<bean class=…>`) are not scanned,
  because the library rules see XML paths, not XML content. An XML-only iBATIS setup without the jar in the pom or
  a Java import is not detected.
* No OpenRewrite recipe exists. Policy `guide`: the advisory hint (follow-up + the `## fix-loop: ibatis2` section)
  goes to the fix LLM when a cluster mentions `com.ibatis` / `org.springframework.orm.ibatis`. The LLM keeps the
  public DAO signatures, ports to `SqlSessionTemplate` with one mapper XML per sqlmap namespace, and adds
  `org.mybatis:mybatis` + `org.mybatis:mybatis-spring`. It never re-adds iBATIS. Verify reports `replaced` once
  no source imports those packages any more.
* The cluster must mention one of the packages. A javac block for a symbol-only error (`cannot find symbol class
  SqlMapClientDaoSupport`) normally carries the import line in its raw text.

### jdk-removed-apis: pre-Java-8 code (2026-10-21, rewrite-migrate-java 3.42.0 from BOM 3.37.0)

As-is projects on source level 1.6 / 1.7 migrate like any other. sbmig never builds the as-is tree. `UpgradeToJava21`
sets release 21, and restructure strips `source/target`. The recipe tree, read from the jar's
`META-INF/rewrite/*.yml`, goes `UpgradeToJava21` → `UpgradeToJava17` → `Java8toJava11` (all `Singleton`
preconditions). `Java8toJava11` already runs these recipes, so the jdk21 recipe set needs no extra ids:

* `UpgradeToJava8` (→ `UpgradeToJava7`, `UpgradeJavaVersion 8`, `Thread.destroy()` removal);
* `lang.RenameUnderscoreIdentifier` (`_` identifiers);
* `UseJavaUtilBase64` (`sun.misc.BASE64Encoder/Decoder` → `java.util.Base64`, only when the sun types resolve);
* `javax.AddJaxbDependenciesWithRuntime` (`AddJaxbAPIDependencies`: `javax.xml.bind:jaxb-api` →
  `jakarta.xml.bind:jakarta.xml.bind-api` 2.3.x, added `onlyIfUsing javax.xml.bind`, + `AddJaxbRuntime glassfish`);
* `javax.AddJaxwsDependencies`, `javax.MigrateJaxBWSPlugin`, `javax.AddInjectDependencies`;
* `javax.AddCommonAnnotationsDependencies` (`javax.annotation-api` → `jakarta.annotation-api` 1.3.x, `provided`);
* `javax.JavaxLangModelUtil`, `UpgradeBuildToJava11`, `ThreadStopDestroy`.

`UpgradeToJava17` adds `SunNetSslPackageUnavailable`, `RemovedLegacySunJSSEProviderName` and
`DeprecatedLogRecordThreadID`. `UpgradeToJava21` adds `UpgradeBuildToJava21` and `lang.ThreadStopUnsupported`.

What the recipes cannot do: OpenRewrite parses with JDK 21, so `sun.misc.BASE64Encoder` does not resolve and
`UseJavaUtilBase64` leaves the code alone. The jdk21 compile then fails with `cannot find symbol … location: package
sun.misc`. The advisory `jdk-removed-apis` (guide) matches that cluster, because `sun.misc` is an import package of
its rule. The fix prompt gets the follow-up plus the `## fix-loop: jdk-removed-apis` section of
`knowledge/notes/jdk21.md`. The verify step reports `replaced` once no source imports those packages any more. The
Handover then lists the LLM-rewritten files.

Fixture `java6-legacy`, with a replayed LLM:

* The pre-emptive `jaxb-runtime-add` adds `jaxb-api` 2.3.1 + `jaxb-runtime` 2.3.9.
* `UpgradeToJava21` turns the API into `jakarta.xml.bind-api` 2.3.3. The spring-phase `JavaxMigrationToJakarta`
  moves `Dates` to `jakarta.xml.bind` (3.0.1 / runtime 3.0.2).
* One LLM fix ports `Codec` to `java.util.Base64`, plus one harvest recipe-candidate call (answer `NONE`).
* Result: BUILD SUCCESS, exit 0.

### jdk-generified-apis (round 9c)
Pre-Java-9 Swing tree code: JDK 9 generified `javax.swing.tree` (`DefaultMutableTreeNode.children()` and the
enumeration methods return `Enumeration<TreeNode>`), so `Enumeration<DefaultMutableTreeNode> e = node.children()`
fails on javac 21 with "incompatible types: inference variable T has incompatible equality constraints". Import-only
rule (`javax.swing.tree`), policy `guide`: the fix loop gets the `## fix-loop: jdk-generified-apis` section of
`knowledge/notes/jdk21.md` (declare `Enumeration<TreeNode>` and cast per element, or `asIterator()`); verify reports
`replaced` only when the imports are gone, so it usually stays `still-present` — the Handover lists the ported files.
