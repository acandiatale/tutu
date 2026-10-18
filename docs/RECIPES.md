# Recipes — how recipe lists are managed

sbmig runs OpenRewrite recipes first and the LLM build-fix loop second. Recipe ids show up in six places.
Each place owns a different decision. One of them, `knowledge/recipes/catalog.yml`, is the **managed list**:
every recipe id the other sources use must have an entry there, and `sbmig recipes check` fails when they drift apart.

## The six sources

| # | Source | Authoritative for | Read by |
|---|--------|-------------------|---------|
| 1 | `knowledge/recipes/catalog.yml` | **What a recipe id is**: origin, artifact or file, default phase, description, docs link, what a composite `covers`, the profile `gate`, whether the LLM advisor may add it (`advisor`), and when it was verified | `sbmig/plan/catalog.py` (`RecipeCatalog`), the planner's `ADVISOR_CATALOG` / `ADVISOR_COVERAGE` / `ADVISOR_GATES`, `sbmig recipes …` |
| 2 | `config/default.yml` → `openrewrite.recipes.{jdk21, jakarta, spring_boot_4, spring_framework_7, spring_security_7, junit5, extra}` | **What the rule-based plan runs** for each case, and in what order (company configs may override these) | `sbmig/plan/planner.py::spring_recipes` |
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
  file: null                 # project recipes: knowledge/recipes/base/<file>.yml
  phase: spring              # jdk21 | spring | restructure (default phase)
  description: Spring Boot 2.x/3.x → 4.0 umbrella (Boot projects only)
  docs: https://docs.openrewrite.org/recipes/java/spring/boot4/upgradespringboot_4_0
  covers: [org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta, ...]
  gate: is_boot              # named predicate in sbmig/plan/catalog.py GATES, or null
  advisor: true
  sets: [spring_boot_4]      # openrewrite.recipes sets that reference it (checked)
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

1. **Verified upstream.** Confirm the id on docs.openrewrite.org, or in a recipe jar at the configured BOM version.
2. **Catalog entry.** Add the id to `catalog.yml` with its artifact, description, `verified` date and, if needed,
   `covers` and `gate`.
3. **Make it run.**
   * In every plan for a case: add it to an `openrewrite.recipes.<set>` in config, then add that set to `sets`.
   * Only when an error appears: add a rule to `recipe_rules.yml`, then add that rule to `rules`.
   * Advisor only: set `advisor: true` and pick a `gate`.
4. **Project composite** (optional). If upstream recipes need parameters or need to be chained, write a
   `com.company.migrate.*` recipe in `knowledge/recipes/base/*.yml`. Give it an `origin: project` catalog entry
   with `file:`.
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
   there, or set `provided_by` when a configured artifact brings it in transitively.
2. Add the catalog entry.
3. Reference it from a config set, a reactive rule, or a base yml, as needed.
4. Run `sbmig recipes check`. It must print `recipe catalog OK`.
5. Run `.venv/bin/python -m pytest tests/plan`. `tests/plan/test_catalog.py` runs the same check on the repo.

`check` reports:

* ids in config sets, reactive rules, or base yml (declared names and `recipeList` ids) that are missing from the catalog;
* `sets` / `rules` fields that disagree with the config or `recipe_rules.yml`;
* project entries whose `file` does not exist or does not declare the id;
* unknown gate names and `covers` ids that are not in the catalog;
* artifacts that are not in `openrewrite.recipe_artifacts`. Core `org.openrewrite:` recipes are exempt, and so are
  entries whose `provided_by` is configured.

## Advisor whitelist and gates

With `plan.advisor: llm`, `advise_plan` asks `llm.recipe_model` to review the rule-based plan. The call is tagged
`role="plan_advisor"`. The advisor can only **add** recipes:

* The whitelist is every catalog entry with `advisor: true`, shown to the model in file order with its description.
  Any other id is ignored.
* An id that is already planned, or that a planned composite already includes (its `covers` list), is not added.
  The model is told about these covered ids in advance.
* A `gate` must hold on the `ProjectProfile`, otherwise the suggestion is dropped as "not warranted". Gates:
  `has_junit4`, `has_javax_ee`, `has_javax_xml_bind`, `has_xml_bind_any`, `has_spring_security`, `is_spring`,
  `is_boot`, `has_tests`, `always`.
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

They are `advisor: false`, so the LLM advisor never suggests them. They are listed under the umbrella's `covers`
(verified against rewrite-spring 6.37.1). Because of this, a Boot project gets them from the umbrella and the
finding records `recipe_source: plan`. For non-Boot plans (spring_legacy, MSA library roots), the planner
**prepends** them to `spring.rewrite` (`recipe_source: advisory`), so they run before the Framework/Boot recipes in
the same rewrite pass. Hystrix, Ribbon and Zuul have no OpenRewrite recipe anywhere, so they stay `guide`.

## How KB recipes are credited and promoted

`FileKnowledgeBase.credit_recipe(id, success, project_fingerprint)` is the only place credits change:

* A success increments `success_count` and records the project fingerprint in `projects` (once per project).
* A failure increments `fail_count`.
* Then, if the recipe is a candidate, `kb.auto_promote_after_projects` (N, default 2) is > 0, `fail_count == 0`,
  `success_count > 0` and `len(projects) >= N`, it is promoted to `active`. Setting N to 0 turns this off.

Credits come from three places:

* **Plan runs.** With `kb.try_candidates_in_plan`, active and candidate recipes run in the spring OpenRewrite step.
  Any that changed files are credited with that phase's build result (orchestrator `_credit_plan_recipes`).
* **Fix loop.** KB recipes tried for an error cluster are credited by the attempt's outcome.
* **Harvest.** `KB_RECIPE` decisions are credited when the phase is harvested.

Active recipes are appended to every plan's spring step, and the fix loop tries them when their signatures match.
Candidates are tried after active recipes and before the LLM.

## CLI

```
sbmig recipes list [--kb] [--json] [-c cfg.yml] [--catalog file]   # table: id, origin, phase, artifact/file, gate, advisor, sets, rules
sbmig recipes show <id> [--json]       # entry + config sets / rules / composites that reference it + KB meta
sbmig recipes check                    # cross-check every source; exit 1 on problems (CI / e2e)
sbmig recipes sets [--json]            # config sets, and per case what jdk21/spring rewrite steps get
sbmig kb list | show | promote | reject | stats …   # learned recipes (see `sbmig kb --help`)
```
