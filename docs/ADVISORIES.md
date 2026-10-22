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
| jdk-generified-apis | incompatible | guide | JDK API: `Enumeration<TreeNode>` + cast / `asIterator()` | no recipe (not a dependency); the LLM ports `javax.swing.tree` code with the `## fix-loop: jdk-generified-apis` hint (`knowledge/notes/jdk21.md`) |
| hibernate-commons-annotations | incompatible | guide | `org.jboss.logging.Logger` (LoggerFactory / Log), `java.lang.reflect` (XClass / ReflectionManager) | no recipe; HCANN came transitively with hibernate-core ≤ 6.x and Hibernate ORM 7 (Boot 4) dropped it — the LLM ports the imports with the `## fix-loop: hibernate-commons-annotations` hint (`knowledge/notes/hibernate-7.md`) |
| tiles | eol | guide | Thymeleaf + thymeleaf-layout-dialect (Boot-managed), SiteMesh 3 … (thymeleaf-layout-dialect 4.0.1 (Boot 4.1 managed) / org.sitemesh:sitemesh 3.3.0) | no recipe; `## fix-loop: tiles` (rule `tiles`) |
| velocity | incompatible | guide | Thymeleaf or FreeMarker for web views; … (velocity-engine-core 2.4.1 / velocity-tools-generic 3.1) | no recipe; `## fix-loop: velocity` (rule `velocity`) |
| thymeleaf-legacy | superseded | guide | org.thymeleaf:thymeleaf-spring6 / thymeleaf-extras-springsecurity6 / … (3.1.5.RELEASE / layout dialect 4.0.1 (Boot 4.1 managed)) | manual (artifacts) |
| commons-fileupload | incompatible | guide | StandardServletMultipartResolver / Boot multipart … (commons-fileupload2-jakarta-servlet6 2.0.0-M5 (milestone)) | no working recipe (the pinned `FileuploadToFileUpload2` misspells its package renames); `## fix-loop: commons-fileupload` (rule `commons-fileupload`) |
| jersey1 | eol | guide | Jersey 4 (org.glassfish.jersey.*, Boot-managed, spring-boot-starter-jersey) … (4.0.2 (Boot 4.1 managed)) | manual (artifacts) |
| spring-hateoas-legacy | superseded | guide | org.springframework.hateoas:spring-hateoas (Boot-managed) / … (3.1.1 (Boot 4.1 managed)) | manual (artifacts; only below 1.0) |
| spring-portlet | eol | guide | Spring MVC controllers (servlet) — the portal integration must be redesigned | manual (artifacts) |
| spring-mobile | eol | guide | User-Agent parsing in an own HandlerInterceptor (e.g. ua-parser) or a … | manual (artifacts) |
| jasperreports6 | incompatible | guide | net.sf.jasperreports:jasperreports 7.1.0 + the extension artifacts for the … | manual (artifacts; only below 7.0.0) |
| struts2 | incompatible | guide | org.apache.struts:struts2-core 7.4.0 (+ same-version plugins); long term … | manual (rule `struts2`; only below 7.0.0) |
| struts1 | eol | guide | Spring MVC (preferred) or io.github.weblegacy:struts-* 1.5.0-RC2 (RC, … (io.github.weblegacy:struts-core 1.5.0-RC2) | manual (artifacts) |
| commons-chain | unmanaged | guide | keep commons-chain 1.2 for the core; web layer → … (1.3.0) | manual (artifacts) |
| egovframe | incompatible | guide | org.egovframe.rte:egovframe-rte-* 5.0.2 (Jakarta) | manual (rule `egovframe`) |
| webjars-locator-core | superseded | guide | org.webjars:webjars-locator-lite (Boot-managed) (1.1.3 (Boot 4.1 managed)) | manual (artifacts) |
| shiro-legacy | eol | guide | org.apache.shiro:shiro-* 3.0.1 (no classifier) | the pinned `UpdateApacheShiroDependencies` leaves 2.0.x + classifier `jakarta`; 3.0.1 has no classifier jars (not in `jakarta_libraries.yml`) (artifacts; only below 3.0.0) |
| hibernate-template | incompatible | guide | org.springframework.orm.jpa.hibernate.LocalSessionFactoryBean / … (spring-orm 7.0.x (Boot-managed)) | no recipe; `## fix-loop: hibernate-template` (rule `hibernate-template`) |
| hibernate-entitymanager | superseded | guide | remove it — org.hibernate.orm:hibernate-core (Boot-managed, via … (7.4.1.Final (Boot 4.1 managed)) | manual (artifacts) |
| hibernate-ehcache | eol | guide | org.hibernate.orm:hibernate-jcache + org.ehcache:ehcache (classifier jakarta) (7.4.1.Final + 3.12.0 (both Boot 4.1 managed)) | manual (artifacts) |
| hypersistence-utils | superseded | guide | io.hypersistence:hypersistence-utils-hibernate-73 (3.16.0) | manual (artifacts) |
| hibernate-search-legacy | incompatible | guide | org.hibernate.search:hibernate-search-mapper-orm + … (8.4.0.Final) | manual (artifacts; only below 8.4.0) |
| querydsl-hibernate-native | incompatible | guide | io.github.openfeign.querydsl:querydsl-jpa / querydsl-apt (no classifier) (7.7) | manual (rule `querydsl-hibernate-native`) |
| ehcache2 | eol | guide | org.ehcache:ehcache (classifier jakarta) through JCache … (3.12.0 + cache-api 1.1.1 (both Boot 4.1 managed)) | no recipe; `## fix-loop: ehcache2` (rule `ehcache2`) |
| hazelcast-legacy | incompatible | guide | com.hazelcast:hazelcast (Boot-managed) (5.5.0) | manual (artifacts; only below 4.0) |
| commons-dbcp1 | eol | guide | org.apache.commons:commons-dbcp2 (Boot-managed) — or HikariCP (Boot default) (2.14.0 (Boot 4.1 managed)) | no recipe; `## fix-loop: dbcp1` (rule `dbcp1`) |
| druid | incompatible | guide | com.alibaba:druid-spring-boot-4-starter (Boot) / com.alibaba:druid 1.2.28 + … | manual (artifacts) |
| jasypt-spring-boot | unmanaged | guide | com.github.ulisesbocchio:jasypt-spring-boot-starter (latest) (4.0.4) | manual (artifacts) |
| atomikos | unmanaged | guide | com.atomikos:transactions-spring-boot4-starter (6.0.1) | manual (artifacts; only below 6.0.0) |
| bitronix | eol | guide | com.atomikos:transactions-spring-boot4-starter (or Narayana) (6.0.1) | manual (artifacts) |
| commons-collections3 | eol | recipe | org.apache.commons:commons-collections4 (4.5.0 (not managed by Boot)) | `UpgradeApacheCommonsCollections_3_4` (rewrite-apache 2.30.0, provided by rewrite-spring; prepended to spring.rewrite) + `## fix-loop: commons-collections3` (rule `commons-collections3`) |
| dozer | superseded | guide | com.github.dozermapper:dozer-core (short term) or MapStruct (7.0.0) | manual (artifacts) |
| orika | eol | guide | MapStruct (org.mapstruct:mapstruct) or ModelMapper (mapstruct 1.6.3) | manual (artifacts) |
| elasticsearch-rest-high-level | eol | guide | co.elastic.clients:elasticsearch-java (Boot-managed) (9.4.2 (Boot 4.1 managed)) | manual (artifacts) |
| solr-legacy | eol | guide | org.apache.solr:solr-solrj (a SolrClient bean defined by the project) (9.11.0 (Solr 9 servers) / 10.0.0) | manual (artifacts) |
| jackson1 | eol | guide | tools.jackson.core:jackson-databind (Jackson 3; Boot 4 default) or … (Boot 4.1 managed (Jackson 3.1.4 / Jackson 2 2.21.4)) | `CodehausToFasterXML` (inside the umbrella) maps 11 types; the rest via `## fix-loop: jackson1` (rule `jackson1`) |
| json-lib | eol | guide | Jackson (tools.jackson.core:jackson-databind, Boot 4 default) or … (Boot 4.1 managed) | no recipe; `## fix-loop: json-lib` (rule `json-lib`) |
| xstream | incompatible | guide | com.thoughtworks.xstream:xstream (bump) or Jackson XML … (1.4.21) | manual (artifacts) |
| jdom1 | eol | guide | org.jdom:jdom2 (2.0.6.1 (Boot 4.1 managed)) | manual (artifacts; only below 2.0) |
| axis1 | eol | guide | JAX-WS client generated from the WSDL (com.sun.xml.ws:jaxws-rt + … (jaxws-rt 4.0.5 / cxf-rt-frontend-jaxws 4.2.4 / spring-ws 5.0.2 (Boot-managed)) | no recipe; `## fix-loop: axis1` (rule `axis1`) |
| commons-email1 | incompatible | guide | org.apache.commons:commons-email2-jakarta (or Spring JavaMailSender, … (2.0.0-M1 (milestone)) | `UpdateApacheCommonsEmailDependencies` already runs inside the umbrella (JakartaEE10); milestone version unverified (artifacts) |
| commons-configuration1 | eol | guide | org.apache.commons:commons-configuration2 (2.15.1) | manual (artifacts) |
| cglib | eol | guide | net.bytebuddy:byte-buddy (Boot-managed) or MapStruct / BeanUtils.copyProperties (Boot 4.1 managed) | manual (artifacts) |
| commons-httpclient3 | eol | guide | org.apache.httpcomponents.client5:httpclient5 (or Spring RestClient) (Boot 4.1 managed (5.6.1)) | no recipe; `## fix-loop: commons-httpclient3` (rule `commons-httpclient3`) |
| jjwt-legacy | incompatible | guide | io.jsonwebtoken:jjwt-api + jjwt-impl (runtime) + jjwt-jackson (runtime) (0.13.0) | no recipe; `## fix-loop: jjwt` (rule `jjwt`; only below 0.10.0) |
| poi-legacy | superseded | guide | org.apache.poi:poi / poi-ooxml (5.5.1) | no recipe; `## fix-loop: poi` (rule `poi`; only below 5.0.0) |
| jxls1 | incompatible | guide | org.jxls:jxls-poi (2.x for minimal change, 3.x current) (2.14.0 / 3.2.0) | manual (artifacts) |
| jexcelapi | eol | guide | org.apache.poi:poi (HSSF / XSSF) (5.5.1) | manual (artifacts) |
| slf4j-1-pin | incompatible | guide | org.slf4j:slf4j-api (Boot-managed) + spring-boot-starter-logging (2.0.18 (Boot 4.1 managed)) | keys on the bindings / bridges (explicit slf4j-api pins are dropped by `layout.drop_managed_versions`); `## fix-loop: slf4j-1-pin` (artifacts) |
| mockito-legacy | superseded | guide | org.mockito:mockito-core (+ mockito-junit-jupiter) (5.23.0 (Boot 4.1 managed)) | manual (artifacts) |
| powermock | eol | guide | org.mockito:mockito-core (MockedStatic / MockedConstruction) (5.23.0 (Boot 4.1 managed)) | manual (artifacts) |

The analyzer rule names added for these entries are `sleuth`, `hystrix`, `ribbon`, `zuul`, `netflix-feign`,
`spring-cloud-bom` (dependencyManagement / parent), `swagger-v1`, `spring-security-oauth2-legacy`, `log4j1`,
`commons-lang2`, `httpclient4`, `jdk-removed-apis` (import-only: `sun.misc`, `sun.security`, `com.sun.image`,
`javax.activation`; JAXB `javax.xml.bind` stays on the `jaxb` rule), `ibatis2` (round 9), `jdk-generified-apis` (`javax.swing.tree`, round 9c)
and `hibernate-commons-annotations` (`org.hibernate.annotations.common` imports or the HCANN artifact, 2026-10-22).
The 2026-10-22 era-library rules are `hibernate-template`, `commons-fileupload`, `jackson1`, `tiles`, `velocity`,
`axis1`, `commons-httpclient3`, `commons-collections3`, `ehcache2`, `json-lib`, `jjwt`, `poi`, `struts2`, `egovframe`,
`dbcp1` and `querydsl-hibernate-native` (see "2026-10-22 era libraries" below).

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

### hibernate-commons-annotations: HCANN gone in Hibernate ORM 7 (2026-10-22, company run)

`org.hibernate.common:hibernate-commons-annotations` was a transitive dependency of `hibernate-core` 3.x–6.x (6.6.x still
declares 7.0.1.Final at runtime scope), so project code imported `org.hibernate.annotations.common.*` — usually
`util.impl.LoggerFactory` / `Log` through an IDE auto-import, sometimes the `reflection.XClass` API — without a pom
entry. Hibernate ORM 7.0 (every Boot 4.x line) replaced it with `hibernate-models` and `hibernate-core` 7.x does not
reference it any more (verified on the 7.4.0.Final pom). The last HCANN release is 7.0.3.Final.

* Rule `hibernate-commons-annotations`: artifacts `org.hibernate.common:hibernate-commons-annotations`,
  `org.hibernate:hibernate-commons-annotations`; import prefix `org.hibernate.annotations.common`. The generic
  `hibernate` rule fires on the same import (`org.hibernate.annotations`); this one names the lost jar.
* Symptom in the company run: `package org.hibernate.annotations.common.util.impl does not exist` ×2 — only in the
  final `clean package` (see ERROR-HANDLING L12 for why that build gets a fix loop now).
* Policy `guide`: the fix LLM gets the advisory hint plus `## fix-loop: hibernate-commons-annotations`
  (`knowledge/notes/hibernate-7.md`): `LoggerFactory.make(Foo.class)` → `org.jboss.logging.Logger.getLogger(Foo.class)`
  (same `BasicLogger` calls, jboss-logging stays on the classpath through hibernate-core 7), SLF4J only for plain
  `info/debug/warn/error(String …)` usage, `AssertionFailure` → `org.hibernate.AssertionFailure`, the reflection API →
  `java.lang.reflect` with a `MIGRATION-TODO` when unsure; an explicit `hibernate-commons-annotations:7.0.3.Final`
  only for code that cannot change (EOL, listed in the Handover). Verify reports `replaced` once no source imports
  `org.hibernate.annotations.common` and the artifact is not declared.

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

### 2026-10-22 era libraries (2013–2020 Korean enterprise projects)

Web-verified research (Maven Central listings, jar bytecode, the `spring-boot-dependencies` 4.1.0 BOM, the pinned recipe
jars) over ~180 libraries of the web / data / utility groups. Each library lands in exactly one place:

| group | lands in | examples |
|---|---|---|
| same coordinates, a later release is jakarta | `knowledge/libraries/jakarta_libraries.yml` (handler `jakarta-library-floor`, 44 entries) | SiteMesh 2/3, DisplayTag (old `displaytag:displaytag`), Jersey 2 → 4.0.2, CXF → 4.2.4, Spring Web Flow → 4.0.1, Tomcat API jars → 11.0.22, jOOQ → 3.21.5, MyBatis-Spring / Boot starter → 4.1.0, Quartz → 2.5.2, EclipseLink → 5.0.2, Ehcache 3 → 3.12.0 **classifier `jakarta`**, P6Spy starter → 2.0.1, Guice → 7.0.0, Axis2 → 2.0.1 |
| new coordinates, same packages | `sbmig/triage/handlers/relocations.yml` (reactive rows, no `packages`) | spring-session → spring-session-core, hibernate-envers / -jpamodelgen → org.hibernate.orm, HikariCP-java6/7, c3p0, old Oracle / MSSQL / CUBRID / DB2 driver coordinates, dom4j, iText 2.1.7 → OpenPDF 2.4.0, Bouncy Castle jdk15on → jdk18on, hamcrest-all |
| a package an old library pulled in transitively / a recipe introduces | `sbmig/triage/handlers/known_packages.yml` | fileupload2, thymeleaf-spring6, hateoas, spring-session, jakarta.ws.rs, JSP / JSTL APIs, dom4j, javassist, guava, commons-email2, collections4, dbcp2 / pool2, javax.cache, hypersistence-utils |
| source / configuration work by the owning team | advisories (49, all `guide` except `commons-collections3`) + `knowledge/notes/era-libraries.md` | table above |

* `jakarta_libraries.yml` gained `classifier`: the handler sets it on the bumped or relocated declaration (direct and
  managed — Maven matches dependencyManagement on the classifier too), and a declaration without it counts as below
  the floor whatever its version (Ehcache 3.10+ without `<classifier>jakarta</classifier>` is still the javax JAXB
  flavour). `sbmig/plan/pre_handlers.py` mirrors the selection without the classifier rule, so a classifier-only gap
  is fixed reactively (`class file for javax.xml.bind… not found`), not pre-emptively.
* Shiro is deliberately an advisory (`shiro-legacy`), not a jakarta-table entry: the pinned
  `UpdateApacheShiroDependencies` sets 2.0.x with classifier `jakarta`, and 3.0.1 publishes no classifier jars.
* QueryDSL stays on the Boot-managed `com.querydsl` 5.1.0:jakarta; only `com.querydsl.jpa.hibernate.*` users
  (`querydsl-hibernate-native`) need the OpenFeign fork 7.7. Tibero is not relocated (opt-in, company relocation file).
* Fix-loop sections (`## fix-loop: <library>` in `knowledge/notes/era-libraries.md`) are keyed on the finding's library
  — the analyzer rule name, or the advisory id for artifact-only advisories: `hibernate-template`, `commons-fileupload`,
  `jackson1`, `tiles`, `velocity`, `ehcache2`, `commons-collections3`, `json-lib`, `jjwt` (advisory `jjwt-legacy`),
  `poi` (`poi-legacy`), `commons-httpclient3`, `axis1`, `dbcp1` (`commons-dbcp1`), `slf4j-1-pin`, plus `itext` (no
  advisory; the note is picked by its keywords).
* Noise control: advisories keyed on artifacts that Boot manages and projects often declare unversioned
  (`slf4j-api`, `mockito-core`, spring-orm) do not list them — an unversioned match counts as "below" every version
  gate. `slf4j-1-pin` keys on `slf4j-log4j12` / `jcl-over-slf4j`, `mockito-legacy` on `mockito-all`,
  `hibernate-template` on the `org.springframework.orm.hibernate3|4|5` imports.
* `commons-collections3` is `policy: recipe`: `UpgradeApacheCommonsCollections_3_4` lives in rewrite-apache 2.30.0,
  a runtime dependency of rewrite-spring 6.37.0 (`provided_by` in the catalog; `sbmig recipes check --jars` →
  "ok via rewrite-spring", 2026-10-22). The other verified recipes the research found (`UpgradeApachePoi_5`,
  `Mockito1to5Migration`, `ReplacePowerMockito`, `UpgradeApacheCommonsLang_2_3`) are not in the catalog yet — the
  advisories stay `guide`.
* Skipped (research confidence `low`): vendor UI / editor servlet adapters (Nexacro, DEXT5, X-Internet, MiPlatform),
  log4jdbc as an advisory.

