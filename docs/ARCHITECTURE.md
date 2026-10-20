# sbmig Architecture

Goal: take an as-is Maven project (zip/dir), produce a **building** JDK 21 + Spring Boot 4.1.0 project in the
company layout, using OpenRewrite first and an LLM only for what recipes cannot do — and never declare success
without `mvn` saying `BUILD SUCCESS`. Runtime behaviour is explicitly out of scope (as-is owners).

## 1. Pipeline

```
intake ─► analyze ─► advisories ─► plan ─► JDK21 phase ─► SPRING phase ─► RESTRUCTURE phase ─► verify
                                            │               │                 │                    │
                                            └ rewrite+loop  └ rewrite+loop    └ transform+loop     ▼
                                                                             finalize: verify advisories ─► handover ─► report + KB harvest
```

| Phase | What runs | Exit gate |
|---|---|---|
| intake | unzip/copy into `out/<run>/project`, `git init`, as-is commit | – |
| analyze | pom graph + source scan → `ProjectProfile` (case type, versions, libs, optional artifacts in use) | profile.json |
| advisories | `knowledge/libraries/advisories.yml` (+ `advisories.extra_files`, `policy_overrides`) matched against the profile's libraries / declared artifacts → `AdvisoryFinding`s (effective policy `guide` \| `recipe`); never an exit gate — there is no block policy | findings in profile/plan |
| plan | rules (no LLM by default) → `MigrationPlan` (ordered `PlanStep`s); `recipe`-policy advisory recipes are **prepended to `spring.rewrite`** (`recipe_source=advisory`), recipes already inside the Boot 4 umbrella chain (sleuth, Spring Cloud train) are only attributed (`recipe_source=plan`) | plan.json |
| jdk21 | OpenRewrite `UpgradeToJava21` → build-fix loop (`compile`) | BUILD SUCCESS or budget exhausted |
| spring | case-dependent recipes (jakarta / Boot 4.0 umbrella + 4.1 props + version pin / Framework 7 / Security 7 / JUnit5) + KB active recipes → build-fix loop (`test-compile`) | BUILD SUCCESS |
| restructure | non-MSA: parent swap + mandatory deps + optional bumps + `module/{comp-jar,boot-jar}` split + Application class. MSA: pom-only parent/mandatory/optional on each service root → build-fix loop (`package`) | BUILD SUCCESS |
| verify | `mvn clean package` (always) | success flag of the run |
| finalize | advisories verified on the **final tree** (`outcome`: `replaced` \| `still-present` \| `unknown`); `HandoverSummary` built from the as-is poms + as-is effective dependencyManagement vs the final poms (version changes, added/removed, ⚠ carried-over pins, "from here on" list) | – (advisories never change success or the exit code) |
| report | report.md/json (Handover first), changes.patch, result.zip, journal; KB harvest of successful LLM fixes; ledger lines (incl. `resolution=advisory`) | – |

**Externalized configuration (round 8).** `sbmig.analyze.properties.scan_properties` makes a regex inventory of every setting the as-is code reads: `${…}` in `@Value` and other annotations, `@ConfigurationProperties` prefixes, `Environment.getProperty`, `System.getProperty` / `getenv`, `${…}` in Spring XML / web.xml / context.xml, web.xml `<context-param>`, JNDI names (Java literals and lookups, `jee:jndi-lookup`, `jndiName`, Tomcat `<Resource>/<Environment>`, `<resource-ref>/<env-entry>`), `@PropertySource` and placeholder-configurer locations, and `${…}` inside packaged property values. It compares them, using relaxed binding, against the packaged definitions: `application*.properties|yml` at the resources root, `config/` and `META-INF/`, files named by `classpath:` locations, YAML profile documents, plus test resources and inline test properties for test-only keys. Each key becomes a `PropertyFinding`: `defined`, `defaulted` (every ref has a code default) or `external`. Mapping rules from `knowledge/config/externalized.yml` turn container-only keys into the Boot keys restructure writes as `__SET_ME__` placeholders (JNDI DataSource → `spring.datasource.*`, context-param → `server.servlet.context-parameters.<name>`, Boot defaults and container system properties skipped). Maven-filtered resource dirs are recorded, and `${x}` that Maven resolves there are ignored. The result lands in `ProjectProfile.properties`. Broken, oversized or unreadable files become `PropertyInventory.notes`; the scan never raises, and `externalized.enabled: false` turns it off.

Why recipes run **before** restructure: OpenRewrite needs type attribution against the *as-is* classpath
(javax types, Boot 2/3 classes) to match. Swapping the parent first would resolve a Boot 4.1 classpath and leave
old types unattributed, so many `ChangeType`-style recipes would silently no-op.

## 2. Case → recipe matrix

| Case | jdk21 | spring phase | restructure |
|---|---|---|---|
| pure_java | UpgradeToJava21 | jakarta (only if `javax.*` EE imports found); JUnit4→5 if junit4 | layout + `spring-boot-starter`, generated `Application` |
| servlet_tomcat | UpgradeToJava21 | `ServletToJakartaAndBoot` (jakarta + servlet-api swap) | layout + `spring-boot-starter-webmvc`, `@ServletComponentScan`, web.xml → followup note |
| spring_legacy (Spring 4/5, no Boot) | UpgradeToJava21 | jakarta + UpgradeSpringFramework_7_0 (+Security 7 if present) | layout + webmvc starter, `@ImportResource` for XML bean files |
| spring_boot (2.x/3.x) | UpgradeToJava21 | UpgradeSpringBoot_4_0 (chains 2.x→3.5 internally) + SpringBootProperties_4_1 + SpringBoot41Version + Boot4JacksonTwins (`@JsonComponent`→`@JacksonComponent`, `JsonObjectSerializer`→`ObjectValueSerializer` …; the umbrella already migrates bodies to Jackson 3) | layout; existing `@SpringBootApplication` class moves to boot-jar |
| msa | per service | per service by its own sub-case | `msa_pom` only, directory structure untouched |

Pre-emptive handlers from the profile (run before the first build of a phase): lombok ≥ 1.18.30 for JDK 21,
mapstruct ≥ 1.6, querydsl jakarta classifier, maven-compiler-plugin ≥ 3.13, surefire ≥ 3.2, etc.
(`sbmig/triage/handlers/`).
Reactive handlers (triggered by build errors): `dependency-relocation` (javax→jakarta artifacts, Boot 4 starter renames),
`add-known-dependency` (error references a package whose artifact is known: hamcrest, modular `*-test` starters,
`spring-boot-jackson2`, jakarta APIs, jsr305 …; version omitted when a Boot/in-house parent manages it),
`missing-dependency-version`, `jaxb-runtime-add`, plus `recipe_rules.yml` mapping error patterns to builtin recipes.

## 3. Build-fix loop (the heart)

```
for iteration in 1..max_iterations:
    result = build(goal)                      # ALWAYS a real mvn run, log captured
    if result.ok: break  ✔
    errors   = parse(log)                     # structured BuildError with L1/L2 signatures
    stall    = signature multiset == previous → stall_level += 1
    clusters = triage.cluster(errors)         # dependency/plugin/parent first, then compile by file (reactor order), then test-compile
    for cluster in clusters[:max_clusters_per_iteration]:
        match    = kb.match(cluster)          # active recipes, replayable patches, hints
        decision = triage.decide(cluster, match, ctx)   # HANDLER > KB_RECIPE > KB_PATCH > LLM_FIX > ESCALATE
        apply(decision) ; commit               # one git commit per attempt
    (KB_RECIPE decisions are batched into ONE OpenRewrite run per iteration)
    rebuild happens at the top of the next iteration; attempts are scored by whether their cluster's
    signatures disappeared (improved / no_change / regressed). Regression ⇒ revert that commit.
    stall_level ≥ stall_limit ⇒ widen context + escalation model; still stalled ⇒ ESCALATE cluster to report.
```

Hard rules:
* The orchestrator owns the loop. The LLM never decides whether to build; a phase is done only on `BUILD SUCCESS`.
* Every attempt is a git commit → revertable, diffable, attributable in the report.
* Budgets: `max_iterations` per phase, `max_llm_calls` per run, `max_total_minutes` per run.
* Test-compile fallback (`-Dmaven.test.skip=true`) only as a last resort and always surfaced as a flag.
* Tests-skipped fallback (round 8, `build.allow_skip_tests_fallback`): when only runtime `TEST_FAILURE` errors
  remain after the fix rounds and the goal runs tests, the phase is rebuilt with `-DskipTests`; on success flag
  `tests-skipped`, every failing test becomes a follow-up and an unresolved error, and the final verify also packages
  with `-DskipTests` (`-Dmaven.test.skip=true` wins when the test-compile fallback fired too).

## 4. Triage

Clusters are keyed by `(kind, file)` for compile errors and by `(kind, artifact)` for resolution errors.
Priority: PARENT_RESOLUTION < POM_INVALID < DEPENDENCY_RESOLUTION < PLUGIN_RESOLUTION < ENFORCER < COMPILE
(by reactor module order, then by error count desc) < TEST_COMPILE < PLUGIN_EXECUTION.
Resolution-type clusters mask compile clusters of the same module in the same iteration (SKIP) because
they usually disappear once the pom is fixed.

Decision order per cluster: deterministic handler → KB active recipe → KB patch replay → LLM → escalate.
A cluster that already went through an LLM attempt without improvement gets the previous attempt in its
prompt (so the model does not repeat itself) and, after `stall_limit`, the escalation model.

## 5. Signatures

* **L1 (specific)** `kind|normalized message|symbol|location-type` — e.g.
  `compile|cannot find symbol|class WebSecurityConfigurerAdapter|package org.springframework.security.config.annotation.web.configuration`
* **L2 (generic)** `kind|message-class|symbol-name` — e.g. `compile|cannot_find_symbol|WebSecurityConfigurerAdapter`
* Paths, line numbers, module names and project-specific identifiers in messages are stripped.
  Dependency errors: `dependency_resolution|g:a` (version stripped) / `…|g:a:v` as L1.

## 6. LLM fix protocol

Request = system prompt (role, hard constraints, output protocol) + user content:
profile summary, phase, the cluster's errors, the file (whole if ≤ 400 lines, else windows ±60 lines around each
error with exact text), pom excerpt when relevant, selected migration notes (`knowledge/notes/*.md` chosen by
keyword index), KB hints, previous failed attempts on this cluster, summary of other clusters.

Response protocol (parsed by `sbmig.fix.protocol`):
```
<analysis>…why…</analysis>
<patches>
=== MODIFY path/to/File.java
<<<<<<< SEARCH
exact text
=======
replacement
>>>>>>> REPLACE
=== CREATE path/to/New.java
…full content…
=== DELETE path/to/Old.java
</patches>
```
or `<need_files>a.java, b.xml</need_files>` / `<need_grep>pattern</need_grep>` (≤ `max_context_rounds` rounds).
SEARCH must match exactly once (fallback: whitespace-normalized unique match); otherwise the block is rejected
and the model gets the rejection as feedback in the same cluster's next round.

## 7. Knowledge base (file-based, git-friendly)

```
kb/
  cases/YYYY-MM/<case-id>.json        one resolved case per file (signatures, sanitized errors, changes, outcome)
  index.json                          signature → case ids / recipe ids   (rebuild: `sbmig kb reindex`)
  recipes/active/*.yml                promoted declarative OpenRewrite recipes (composed into every run)
  recipes/candidates/*.yml            harvested, awaiting review (`sbmig kb list|promote|reject`)
  patterns/*.yml                      deterministic non-OpenRewrite fixes (pom relocations, property renames)
  ledger.jsonl                        one ResolutionRecord per resolved/blocked cluster across ALL projects (fleet memory)
  symbols.json                        error-case symbol → fix labels / "old → new" / projects / recipe id
```
Harvest (end of each phase, only for attempts whose cluster was resolved and which survived to BUILD SUCCESS):
1. Create `KBCase` (sanitized paths, no secrets).
2. If `generate_recipe_candidates`: ask the LLM whether the fix is mechanical and expressible with a
   whitelisted recipe set (ChangeType, ChangePackage, ChangeMethodName, ChangeDependencyGroupIdAndArtifactId,
   UpgradeDependencyVersion, AddDependency, RemoveDependency, ChangePropertyKey, ChangeSpringPropertyKey,
   ReplaceConstant, …); validate YAML; store as candidate with the triggering signatures.
3. Next projects: `kb.match` surfaces active recipes (run before LLM), replayable patches (same L1 signature and
   the SEARCH text still present) and hints (summaries of similar cases).
4. Candidate → active automatically after success in `auto_promote_after_projects` distinct projects, or manually.
5. **Where candidates run.** OpenRewrite needs type attribution, which only exists while the as-is classpath is intact,
   and `rewrite:run` forks `compile` first (so it cannot run on a broken project at all). Therefore candidates are
   composed into the plan's `spring.rewrite` step next to the migration recipes (`kb.try_candidates_in_plan`), and the
   runner records per-recipe attribution from the plugin's `… by:` blocks (`RewriteResult.recipes_applied`). After the
   phase, every KB recipe that actually changed files is credited with the phase's build outcome
   (`Orchestrator._credit_plan_recipes`). Reactive KB recipe batches inside the build-fix loop use `rewrite:runNoFork`,
   and a forked plan-level run that fails in `compile` is retried with `runNoFork` automatically.

6. **Labels and the ledger (round 3).** Every harvested case is labeled deterministically from its diff
   (`sbmig.kb.labels`: `import-rename`, `type-rename`, `annotation-replace`, `method-rename`, `dependency-add`,
   `dependency-version`, `spring-property-rename`, … `generic-rewrite`) and keyed by the error-case **symbol** (javac
   symbol, `g:a`, owner class). Mechanical labels compile straight into whitelisted recipe candidates (no LLM; since
   round 5 also `method-rename → ChangeMethodName`, owner FQCN from `KBCase.symbol_owner`); the others go to the
   recipe model. Each resolution (handler / plan recipe / KB recipe / KB patch / LLM patch / blocked)
   is appended to `kb/ledger.jsonl`, and `kb/symbols.json` feeds "symbol X was fixed in N projects by Y: old → new"
   hints into the next prompt. `sbmig kb learning` turns the ledger into the to-do list for humans (symbols the LLM
   keeps fixing → write a recipe/handler; artifacts blocking many projects → library owners).

## 7.5 Fleet mode (one agent, sequential)
`sbmig fleet run <manifest>` (`sbmig/fleet`): projects are migrated strictly one after another with a shared KB, so
project N+1 plans with the recipes learned from 1..N. State (`fleet/state.json`) is rewritten after every project and
makes the run resumable; `summary.{md,csv,json}` + `learning.md` are regenerated each time. The per-project pipeline
is unchanged (`Orchestrator`); the fleet only sequences, budgets (`fleet.max_llm_calls_total`) and aggregates.

## 7.6 Adapter seams (in-house gateway / agent platform)
`LLMClientLike` (chat completion) and `FixerLike` (whole fixer) are the two replaceable units. `llm.provider: plugin`
+ `llm.plugin: "pkg.module:attr"` loads an `LLMAdapterLike` (`complete(LLMRequest) -> LLMResponse`, with the call's
`role` and metadata) and wraps it so recording and budgets still apply; `fix.plugin` replaces `LLMFixer` with an
in-house coding agent that receives the workspace, the error cluster and the packed context. See `docs/ADAPTERS.md`.

## 7.7 Graph engine (LangGraph)
The company only deploys agents built on LangGraph / LangChain, so a LangGraph `StateGraph` **drives** the pipeline
(`sbmig/graph/engine.py`, `loop.engine: graph`, the default). It does not re-implement it: every node calls one shared
`Orchestrator.step_*` method, and `loop.engine: inline` runs the same steps from `Orchestrator._run` without LangGraph.

```mermaid
graph TD;
    __start__([start]) --> intake;
    intake -. next .-> analyze;
    intake -. stopped / aborted .-> finalize;
    analyze -. next .-> plan;
    analyze -. stopped / aborted .-> finalize;
    plan -. groups .-> phase;
    plan -. stopped / no groups .-> finalize;
    phase -. groups remain .-> phase;
    phase -. done .-> finalize;
    finalize --> __end__([end]);
```

| Node | Step | Notes |
|---|---|---|
| `intake` | `step_intake` | git snapshot, preflight (`mvn-not-found` → abort), as-is effective depMgmt |
| `analyze` | `step_analyze` | profile + advisories; not Maven / no pom → abort |
| `plan` | `step_plan` | plan, `BuildFixLoop`, phase groups (`None` = stopped after plan) |
| `phase` | `step_phase` | one invocation per phase group (MSA: jdk21/spring repeat per service); `phase_skipped` after `stop_after`; credit + harvest; raises `_RunAborted` when the loop aborted |
| `finalize` | `step_finish` | the trailing `stop_after` record (only when the plan ran, as inline) |

- **State vs orchestrator.** `MigrationState` holds only small JSON-able values: `run_id`, `stop_after`, `dry_run`,
  `phases` (remaining group names), `phase`, `aborted`, `stopped`, `summary` (case type, per-phase outcome, LLM
  calls, blocked, unresolved). The `RunReport`, the phase groups with their `PlanStep`s and the `BuildFixLoop` stay on
  the `Orchestrator` / a per-run holder closed over by the node functions.
- **Errors and finalization.** Nodes never catch: `_RunAborted`, `MissingComponentError` and any other exception
  leave the graph and are handled by the orchestrator's run guard (`_run_guard`), which also runs `_finalize`
  (report, ledger, outputs) for both engines. `run()` opens the guard and dispatches on `loop.engine`;
  `run_graph(orch)` / `stream_graph(orch)` open it themselves when called directly (the web app consumes
  `stream_graph` → `(node, state_update)`; the report is `orch.report` afterwards, also when the stream is closed early).
- **Journal.** Each node start adds `{"event": "graph_node", "node", "phase"}`; everything else is unchanged.
- **Identical by test.** `tests/graph/test_engine.py` runs both engines on the same fake projects (boot, MSA,
  failing build, `stop_after` intake/analyze/plan/jdk21, `mvn-not-found`, parent-not-found abort, internal error)
  and asserts equal reports (minus timestamps / SHAs), flags, exit codes and journal event sequences.
- `recursion_limit` is 1000 (one graph step per node invocation; MSA plans add phase groups per service).
- **Invariant 1 on the abort paths.** An exception inside a node (missing component, bug) is caught by the guard,
  the run is flagged `aborted`, and `_final_build_after_abort` still runs the plan's verify steps (not for dry runs
  or `_RunAborted`, i.e. no Maven / unresolvable parent). The web UI's stop finalizes without the final build and
  says so; a stop observed after the verify phase group finishes the run normally.

## 8. Restructure (non-MSA)

```
root/pom.xml              packaging=pom, parent=<in-house>, modules=[module/comp-jar, module/boot-jar],
                          dependencies=[mandatory×2], properties/depMgmt carried over, optional versions bumped
root/module/comp-jar      all as-is src/main, src/test (minus boot resources / @SpringBootTest tests), as-is deps
root/module/boot-jar      Application.java (generated or moved), boot resources, spring-boot-maven-plugin repackage,
                          deps: comp-jar + starter by case
```
Multi-module as-is (non-MSA), `layout.multi_module_policy` (default **`merge`**, round 7b): every as-is module and
the root `src/` are folded into `module/comp-jar` (one pom: root + module dependencies minus the module-to-module ones).
An as-is module whose directory basename is `layout.boot_module` (`boot-jar`, `apps/boot-jar`, …) is the **kept boot
module**: moved to `module/boot-jar` (parent `relativePath` recomputed), its dependencies on merged modules replaced by
one `<root artifactId>-comp` dependency, spring-boot-maven-plugin ensured, its sources untouched and no Application
generated (first match in `<modules>` order wins; without one, boot-jar is generated as for a single module).
Collisions (same comp-jar path from two sources), `layout.merge_collision`: `first-wins` (default) keeps the copy of
the first source in `<modules>` order (`<root>` first), drops identical copies with one note and saves differing ones
as `<run dir>/merge-conflicts/<module>/src/…` (`RestructureResult.merge_conflicts`, Handover "Merge conflicts", one
manual follow-up); `fail` aborts before anything moves. Nested aggregators abort the merge. "Already restructured"
under `merge` = root packaging `pom` with both `module/comp-jar` and `module/boot-jar` among its modules. `wrap`
(opt-in) moves the existing modules under `module/` unchanged and adds a boot-jar depending on the jar modules.

Merge edge cases (round 9). **Line-list registries** (`META-INF/services/*`, `META-INF/spring.factories`,
`META-INF/spring/*.imports` / `*.factories`, in any resource dir, main or test) are never first-wins and never a
`fail` collision: the kept file is the union of every copy's lines (first copy verbatim, then new lines of the others;
`*.factories` merged per key with deduped comma values), `MergeConflict(union=True)`, every original saved under
`merge-conflicts/<module>/…`, one note, no follow-up (identical copies stay an ordinary identical drop).
`src/test/**` collisions follow the main rules. **`<packaging>pom</packaging>` sub-modules** (BOM, nested aggregator)
move nothing: their `<modules>` children are merged like any other module (depth-first, declaration order), their
`<properties>` and every merged module's `<dependencyManagement>` entries are folded into the root pom (first wins per
key / g:a, with the `${props}` the versions reference); root management entries for merged modules (an internal BOM
import) are removed; a `src/` in a pom module (never compiled by Maven) is saved under `merge-conflicts/`, not merged
(follow-up). A kept boot module whose parent was a merged pom module is re-pointed to the root pom. **Modules outside
the project root** (`<module>../sibling</module>`, root or nested) are skipped under `merge` (note + follow-up; nothing
outside the workspace is read or written); `wrap` still aborts before anything moves. **WAR modules** folded into
comp-jar behave like a single-module WAR: `src/main/webapp` lands in `module/comp-jar/src/main/webapp` (not packaged by
the jar — web.xml / JSP follow-ups name the new location, "WAR packaging converted" follow-up). **Kept boot module
resources** stay in boot-jar; the merged modules' same-named copies go to comp-jar (first-wins only applies within
comp-jar) and a follow-up says the boot-jar copy shadows them at runtime. **Custom resource directories**
(`<build><resources>` of the root or a module, e.g. `src/main/config`): the files move with the same relative path
(also top-level dirs outside `src/`) and comp-jar's `<resources>` / `<testResources>` is the union of every module's
directories (deduped by directory + targetPath), plus the Maven default directory when some module relied on it.

**Managed-version cleanup** (round 9, `layout.drop_managed_versions`, `layout` and `msa_pom`). After the parent /
mandatory / optional edits the restructurer calls `params["effective_pom"]` (zero-arg; the orchestrator passes
`sbmig.build.effective.effective_pom_provider(ws, cfg)`, which captures `help:effective-pom` of the restructured tree
into `<run dir>/effective/depmgmt-after-restructure.json` and returns the dict, None without Maven). For every
explicit `<dependencies>` version in every project pom (literal or `${prop}` resolvable from the pom chain) whose
artifact the declaring module's chain manages (`by_module`, else the merged `managed` map): `gte` drops the
`<version>` when managed ≥ explicit (unknown comparison → keep; newer pins are noted), `any` always, `off` never.
Never touched: parent / mandatory / optional / remove_when_present artifacts, the project's own modules, system scope,
non-jar types / classifiers, dependencyManagement, plugins, properties. Recorded in
`RestructureResult.managed_versions_dropped` + one note; without the callable / capture: note "managed-version
cleanup skipped: no effective pom". MSA placeholders (round 9): with a per-service inventory
(`PropertyFinding.service`) each service file gets that service's findings plus the shared (`service: null`) ones;
a project-wide inventory keeps the location-based selection.

**Externalized configuration placeholders** (round 8, `externalized.*`). Settings the as-is code reads but no packaged
resource defines (`ProjectProfile.properties`, status `external`: they came from the container — JNDI, web.xml
`context-param`, `-D`, `catalina.properties`) get one `key=__SET_ME__` line per `boot_key` (JNDI DataSource →
`spring.datasource.*`, context-param → `server.servlet.context-parameters.*`; a context-param with an as-is value
writes that value; `boot_keys=[]` writes nothing; test-only refs follow `include_test_refs`), each preceded by a
`# from <source> <file:line>` comment, sorted and deduped. Layout mode writes `module/boot-jar/src/main/resources/
<placeholder_file>` (single, merge with a generated or kept boot module, wrap, and already-restructured projects);
`msa_pom` writes each service root's `src/main/resources/<placeholder_file>` (created only when the service has
`src/main/java`) with the keys referenced inside that service plus keys only shared code outside every service reads;
`optional_bump` writes nothing. The file is created when absent (also next to an `application.yml`: Boot loads both,
a note in the block says so). The block sits between `# --- sbmig externalized-config` and `# --- end sbmig
externalized-config`; a rerun replaces the block in place and keeps every other byte and the file's line endings, so
it is idempotent. The step records `RestructureResult.placeholder_files / placeholders_written`, one note and one
follow-up; the orchestrator merges them into `RunReport.properties` (journal event `placeholders`) for the Handover.

## 9. Outputs

`out/<run_id>/{project/, logs/, journal.jsonl, profile.json, plan.json, report.json, report.md, changes.patch, result.zip}`

* `report.json` — a serialized `RunReport` (`sbmig/core/models.py`), including `advisories: [AdvisoryFinding]`
  (`advisory_id`, `library`, `status`, `title`, `policy`, `policy_reason`, `detected_version`, `recipes`,
  `recipe_source` `plan|advisory|none`, `outcome`, `replacement`, `followup`, …) and `handover: HandoverSummary`
  (`parent_before/after`, `java_before/after`, `spring_boot_before/after`, `changes: [VersionChange]`, `added`,
  `removed`, `carried_over`, `from_here`, `notes`).
* `report.md` — first section `## Handover — what we replaced, what is yours` with `### Versions replaced`
  (⚠ = carried over), `### Libraries needing your attention`, `### From here on, your team`; then summary, blocked,
  unresolved, phases, manual follow-ups (`docs/RUNBOOK.md` §2.5).
* `kb/ledger.jsonl` — one line per resolved / blocked cluster and, with `advisories.ledger`, one
  `resolution=advisory` line per finding (advisory id, outcome, policy).
* Fleet (`out/fleet/fleet/`): `summary.csv` gains `advisories` (`id:outcome` list) and `carried_over` (count);
  `learning.md` gains "Libraries needing owner action" (advisories by number of projects still affected).
