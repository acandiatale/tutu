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
* `spring-cloud.version` went from 2021.0.9 to 2025.1.3. Hystrix stayed in the pom and still compiles (→ guide
  follow-up).

The recipes left these behind:

* a dead private `apiInfo()` helper that returns `io.swagger.v3.oas.models.info.Info`;
* the unused `springfox.version` property;
* `spring.mvc.pathmatch.matching-strategy: ant_path_matcher`.

All of them compile. The springfox follow-up asks the team to review them. Result: springfox ships with
`policy: recipe`. For Boot projects all of its recipes come from the umbrella (`recipe_source: plan`).

## Initial list

| id | status | policy | replacement | how |
|---|---|---|---|---|
| springfox | eol | recipe | springdoc-openapi-starter-webmvc-ui 3.x | SpringFoxToSpringDoc + UpgradeSpringDoc_3_0 (inside the Boot umbrella) |
| sleuth | eol | recipe | micrometer-tracing-bridge-brave (Boot-managed) | MigrateCloudSleuthToMicrometerTracing (inside the umbrella) |
| hystrix | eol | guide | spring-cloud-starter-circuitbreaker-resilience4j | no recipe exists |
| ribbon | eol | guide | spring-cloud-starter-loadbalancer | no recipe exists |
| zuul | eol | guide | spring-cloud-starter-gateway-server-webmvc | no recipe exists |
| netflix-feign | eol | guide | spring-cloud-starter-openfeign | the umbrella renames the starter; com.netflix.feign code is manual |
| spring-cloud-bom | superseded | recipe | spring-cloud-dependencies 2025.1.x | UpgradeSpringCloud_2025_1 (inside the umbrella); only below 2025.1 |
| spring-security-oauth2-legacy | eol | guide | spring-boot-starter-oauth2-resource-server / -client | manual |
| log4j1 | eol | guide | logback (Boot default) or log4j2 | manual |
| commons-lang2 | eol | guide | org.apache.commons:commons-lang3 | manual |
| httpclient4 | superseded | guide | org.apache.httpcomponents.client5:httpclient5 | manual |

The analyzer rule names added for these entries are `sleuth`, `hystrix`, `ribbon`, `zuul`, `netflix-feign`,
`spring-cloud-bom` (dependencyManagement / parent), `swagger-v1`, `spring-security-oauth2-legacy`, `log4j1`,
`commons-lang2` and `httpclient4`.
