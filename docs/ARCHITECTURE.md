# sbmig Architecture

Goal: take an as-is Maven project (zip/dir), produce a **building** JDK 21 + Spring Boot 4.1.0 project in the
company layout, using OpenRewrite first and an LLM only for what recipes cannot do — and never declare success
without `mvn` saying `BUILD SUCCESS`. Runtime behaviour is explicitly out of scope (as-is owners).

## 1. Pipeline

```
intake ─► analyze ─► plan ─► JDK21 phase ─► SPRING phase ─► RESTRUCTURE phase ─► verify ─► report + KB harvest
                              │               │                 │
                              └ rewrite+loop  └ rewrite+loop    └ transform+loop
```

| Phase | What runs | Exit gate |
|---|---|---|
| intake | unzip/copy into `out/<run>/project`, `git init`, as-is commit | – |
| analyze | pom graph + source scan → `ProjectProfile` (case type, versions, libs, optional artifacts in use) | profile.json |
| plan | rules (no LLM by default) → `MigrationPlan` (ordered `PlanStep`s) | plan.json |
| jdk21 | OpenRewrite `UpgradeToJava21` → build-fix loop (`compile`) | BUILD SUCCESS or budget exhausted |
| spring | case-dependent recipes (jakarta / Boot 4.0 umbrella + 4.1 props + version pin / Framework 7 / Security 7 / JUnit5) + KB active recipes → build-fix loop (`test-compile`) | BUILD SUCCESS |
| restructure | non-MSA: parent swap + mandatory deps + optional bumps + `module/{comp-jar,boot-jar}` split + Application class. MSA: pom-only parent/mandatory/optional on each service root → build-fix loop (`package`) | BUILD SUCCESS |
| verify | `mvn clean package` (always) | success flag of the run |
| report | report.md/json, changes.patch, result.zip, journal; KB harvest of successful LLM fixes | – |

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

## 8. Restructure (non-MSA)

```
root/pom.xml              packaging=pom, parent=<in-house>, modules=[module/comp-jar, module/boot-jar],
                          dependencies=[mandatory×2], properties/depMgmt carried over, optional versions bumped
root/module/comp-jar      all as-is src/main, src/test (minus boot resources / @SpringBootTest tests), as-is deps
root/module/boot-jar      Application.java (generated or moved), boot resources, spring-boot-maven-plugin repackage,
                          deps: comp-jar + starter by case
```
Multi-module as-is (non-MSA): `wrap` policy moves existing modules under `module/` unchanged and adds boot-jar
depending on them. `merge` is opt-in.

## 9. Outputs

`out/<run_id>/{project/, logs/, journal.jsonl, profile.json, plan.json, report.json, report.md, changes.patch, result.zip}`
