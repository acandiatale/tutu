# Fixture projects

Small but real Maven projects used as the sbmig test corpus (unit tests read them; integration tests and
`scripts/e2e.py` build and migrate them). Each fixture has a `README.md` (exact as-is build command), an
`expected.json` — what `sbmig analyze` should report **plus an `expect` block**, the asserted outcome of `sbmig run` —
and (v2, flow-v2) a `sbmig.e2e.yml` with a **replayed fake LLM** (`replay/llm-NNN.json`) and its **own KB dir** under
`out/`. Happy-path fixtures use only Maven Central; scenario fixtures may need the simulated in-house libraries in
`fixtures/libs/` (installed by `fixtures/libs/install.sh`).

> The `expect` blocks and this matrix describe the **v2 flow** (one OpenRewrite run → restructure → LLM structure
> review → build → script fix loop, docs/v2/PLAN-v2.md). The per-fixture READMEs describe the as-is project and the v2
> outcome (the v1 outcomes — handlers, KB recipes — are in tag `company-v1`); the `expect` block is what is asserted.

## Matrix (v2)

Catalog IDs refer to `docs/ERROR-HANDLING.md`. "LLM" = `llm_usage.calls` of the run (the structure review is always
one of them); "scripts" = the fix-script attempts by mode (`new` = the LLM writes v1, `augment` = the LLM extends the
previous version, `reuse` = a stored version runs again without an LLM call). Measured on real Maven, 2026-10-27.

| fixture | case | catalog | as-is JDK | review (replayed) | scripts / cases | v2 outcome (`expect`) |
|---|---|---|---|---|---|---|
| `pure-java` | `pure_java` | — | 8 | ok | new 1: JAXB `jakarta.xml.bind` (adds `jakarta.xml.bind-api` + `jaxb-runtime`, unversioned) → build_ok; the other 9 JAXB cases of the module are deferred behind that pom change and vanish with the rebuild | success, 2 LLM; **fills the shared KB `out/e2e/kb-jaxb`** (reuse scenario) |
| `servlet-tomcat` | `servlet_tomcat` | — | 8 | warning (servlet-api scope) | — | success, 1 LLM, commons-io → 2.19.0 |
| `spring5-xml` | `spring_legacy` | — | 8 | 2 warnings | new 2: `WebMvcConfigurerAdapter` → `implements WebMvcConfigurer`, `HandlerInterceptorAdapter` → `implements HandlerInterceptor` (the override / type errors of the same files are deferred and vanish) | success, 3 LLM, slf4j-api → 2.0.17 |
| `boot2` | `spring_boot` | — | 11 | warning (lombok.version) | — | success, 1 LLM, commons-lang3 → 3.17.0 |
| `boot3` | `spring_boot` | — | 21 | ok | new 1: `@JsonComponent` → Boot 4 `@JacksonComponent` | success, 2 LLM, guava → 33.4.8-jre |
| `msa` | `msa` | — | 11 / 21 | ok | — | success, 1 LLM; per-service properties as in round 9 |
| `boot2-javax-lib` | `spring_boot` | X1 | 11 (+ `install.sh`) | warning (javax in-house lib) | new 4 (2 cases × the policy's 2 requests, answered without a script) | **blocked** `external_javax_api` `com.company.legacy:legacy-web-lib` (×2 cases), exit **2**, 5 LLM |
| `boot2-javax-lib-optional` | `spring_boot` | X1 | 11 (+ `install.sh`) | ok | — | legacy-web-lib bumped `1.0.0→2.0.0`, success, 1 LLM |
| `euckr-legacy` | `pure_java` | E1 | 8 + `-Dfile.encoding=MS949` | **critical** + script: `project.build.sourceEncoding` MS949 in `module/comp-jar/pom.xml` (committed `review: …`) | — | success, 1 LLM; `Greeting.java` / `NoticeFormatter.java` still cp949 with the Korean literals |
| `enforcer-java8-werror` | `pure_java` | E4, E5 | 8 | warning | new 2: requireJavaVersion `[1.8,9)` → `[21,)`; then `-Werror` dropped (JDK 21 `[removal]` lint; `-Xlint:all` kept, MIGRATION-TODO) | success, 3 LLM |
| `missing-artifact` | `pure_java` | X3 | 8 + ghost repo tail | warning (ghost-lib unmanaged) | new 2 (no script: the artifact does not exist) | **blocked** `artifact_not_found` `com.company.nonexistent:ghost-lib`, exit **2**, 3 LLM, ≤ 240 s |
| `msa-shared-lib` | `msa` | S1 | 18 | 2 warnings | new 3: `javax.validation.constraints`, `javax.validation` → jakarta; `RestTemplateBuilder` → `org.springframework.boot.restclient` + `spring-boot-starter-restclient` | success, 4 LLM; `common` on jakarta without mandatory deps |
| `boot2-cloud-legacy` | `spring_boot` | advisories | 11 | 2 warnings (hystrix pin, springfox.version) | new 1: `@EnableCircuitBreaker` removed (MIGRATION-TODO) | success, 2 LLM; springfox / sleuth / cloud train replaced by the Boot 4 umbrella, **hystrix guide / still-present** (no advisory recipes in v2) |
| `boot2-method-rename` | `spring_boot` | X2 | 11 (+ `install.sh`) | ok | new 1: one case for both call sites (`clientIp(` → `remoteClientIp(` on the reported lines) | success, 2 LLM, legacy-web-lib `1.0.0→3.0.0`, script stored as v1 |
| `boot2-hystrix-command` | `spring_boot` | advisory | 11 | warning (hystrix pin) | — | success, 1 LLM: v2 keeps the Hystrix starter, `InventoryCommand` still compiles against it (hystrix still-present, handover) |
| `multi-module-merge` | `spring_boot` | layout `merge` | 11 | ok | — | success, 1 LLM; merge as in round 7b |
| `legacy-external-config` | `spring_legacy` | externalized config | 8 | warning (spring.version left) | — | success, 1 LLM; properties inventory + placeholders as in round 8 |
| `java6-legacy` | `pure_java` | S8 | 8 | 2 warnings (bootclasspath, surefire 2.4.3) | new 2 + **reuse 1**: `--boot-class-path` dropped; then the JAXB case **reuses pure-java's stored v1 without an LLM call** (improved — the Base64 case is still there); `sun.misc.BASE64*` → `java.util.Base64` | success, 3 LLM; **`kb.after: pure-java`** (reuse scenario) |
| `multi-module-conflict` | `spring_boot` | merge conflict | 11 | ok | new 1: `farewell(String)` from the dropped web copy (shown as reference) added to the kept `Shared.java` | success, 2 LLM |
| `legacy-ibatis` | `spring_legacy` | advisory `ibatis2` | 8 | warning (iBATIS 2) | new 12: 3 cases × 4 requests, the LLM declines a blind MyBatis port (no script) | **blocked** `repeated_failure` ×3, exit **2**, 13 LLM, ibatis2 guide / still-present |
| `legacy-role-parents` | `spring_boot` | company rule | 11 | warning (`boot-jar` not `*-boot`) | — | success, 1 LLM; poms only (round 9b/9c) |
| `company-boot-single` | `spring_boot` | company layout: wrap | 11 | ok | — | success, 1 LLM |
| `company-spring5-web` | `spring_legacy` | company layout | 11 | warning (spring.version) | — | success, 1 LLM |
| `company-servlet-jsp` | `servlet_tomcat` | company layout: WAR | 8 | 2 warnings (javax jstl, servlet-api scope) | — | success, 1 LLM |
| `servlet-webxml-legacy` | `servlet_tomcat` | W1 | 8 | ok | — | success, 1 LLM; `WebXmlRegistrations`; JSPs keep `javax.servlet` (not compiled — see open points) |
| `servlet-nonstandard-dirs` | `servlet_tomcat` | E12, W1 | 8 | **critical** + script: `nonFilteredFileExtensions` xlsx / mp4 (the review's facts list the binary files under the filtered `STATIC/resources`) | — | success, 1 LLM; `sample.xlsx` / `intro.mp4` byte-identical in `target/classes` |
| `boot2-fix-failures` | `spring_boot` | X2, error cases | 11 (+ `install.sh`) | ok | **new 1 + augment 1**: v1 replaces only `clientIp(request)` (ClientIpFilter) → **partial**, rolled back; v2 (parent 1) renames on every reported line → build_ok | success, 3 LLM; **augment scenario** |
| `ear-sibling-poms` | `servlet_tomcat` | aggregator, EAR | 11 | warning (javax jstl) | — | success, 1 LLM |
| `servlet-war-overlay` | `servlet_tomcat` | war overlay | 11 (+ `install.sh`) | ok | — | success, 1 LLM |
| `boot2-rewrite-excluded` | `spring_boot` | E13, E14 | 11 | ok | new 2: `javax.persistence` FQNs in the excluded `vendor/` file; then `ListCrudRepository<Member, Long>` added to the excluded repository (save / findAll / deleteById) | success, 3 LLM |
| `boot2-qlrm-javax` | `spring_boot` | X1b | 11 | warning (QLRM 1.7.1) | new 1: `ch.simas.qlrm:qlrm` → `org.qlrm:qlrm:4.2.1` | success, 2 LLM |
| `spring5-security-batch` | `spring_legacy` | round 10 | 8 | **3 critical** + script: the as-is `spring-batch.version` (4.3.10 — there is no `spring-batch-bom` 4.3.10) / `spring-security.version` / `spring.version` override the Boot parent's own properties → removed, explicit Spring versions dropped | new 10 over 3 rounds: `spring-security-access`, Batch 6 packages, `JobBuilder` / `StepBuilder`, `SecurityFilterChain` lambda DSL, `PathPatternRequestMatcher`, Base64 (then the `catch (IOException)` it leaves, a new case), Spring 7 path-matching / `HttpHeaders` API | success, 11 LLM |

### Reuse and augment scenarios

* **Reuse** (`pure-java` → `java6-legacy`, shared KB `out/e2e/kb-jaxb`): both fixtures hit the L2 case
  `compile|package_does_not_exist|jakarta.xml.bind` (case id `7a52b1cea8`). pure-java's replay answers it (v1 stored,
  credited `build_ok`); java6-legacy's replay has **no answer** for it — its second round runs the stored v1 (`reuse`,
  0 LLM calls) and the meta ends with 2 successes in 2 projects (`expect.scripts.stored` / `reuse_without_llm`, error
  record `fix_method: script_reuse`). Run order: `scripts/e2e.py` runs pure-java first (`DEFAULT_ORDER`) and clears
  `out/e2e/kb-jaxb` before it; java6-legacy's `expect.kb.after: pure-java` keeps the dir and turns a run without
  pure-java into a precondition mismatch (`--only pure-java,java6-legacy` to run the pair alone).
* **Augment** (`boot2-fix-failures`, own KB): the case `compile|cannot_find_symbol|clientIp` (`8bb4fd4a4a`) gets v1
  (too narrow → `partial`, rolled back before the next attempt) and then v2 with `parent: 1` (`augment`, `build_ok`);
  both versions stay in `<kb>/scripts/8bb4fd4a4a/` (v1 `partials: 1`, v2 `successes: 1`), both error records
  `resolved` by `script_augment` with the history `partial → build_ok`.

### Replay format (`replay/llm-NNN.json`)

```json
{"note": "why this answer exists", "replay_role": "structure_review", "response": "<review>{…}</review>\n<script>…</script>"}
{"note": "…", "replay_role": "fix", "match": "case: 8bb4fd4a4a", "response": "<analysis>…</analysis>\n<script>\ndef fix(p):\n    …\n</script>\n"}
```

`llm-001.json` answers the structure review (one call; `<review>` JSON as in `sbmig/review/prompts/review_v1.md`, a
`<script>` only for critical issues). Every other entry answers fix-script requests whose user prompt contains its
`match` regex — `case: <case_id>` with `case_id = sha1(L2 signature_generic)[:10]` (`sbmig.kb.catalog.case_id_for`),
or `case: (?:id1|id2)` for one answer to several cases. The first unused matching entry answers, the last matching one
repeats (so two entries for one case = v1 then the augment), no match = "no fix". An answer with only `<analysis>`
is a deliberate "no script" (blocked scenarios). `tests/fixtures/test_fixture_projects.py` parses every entry and runs
sbmig's static script check on every script; a few scripts are also run against the fixture sources.

Optional artifacts from `config/artifacts.local-e2e.yml` (exactly 8) used directly (→ "optional bump" path):

| artifact | fixtures |
|---|---|
| `org.apache.commons:commons-lang3` | pure-java, boot2, boot2-javax-lib*, boot2-method-rename, boot2-fix-failures, boot2-rewrite-excluded, boot2-qlrm-javax, euckr-legacy, enforcer-java8-werror, missing-artifact, multi-module-merge/core, java6-legacy |
| `com.google.guava:guava` | pure-java, boot3 |
| `org.projectlombok:lombok` | boot2, boot2-javax-lib*, boot2-method-rename, boot2-fix-failures, boot2-rewrite-excluded, boot2-qlrm-javax, msa/order-service, msa-shared-lib/order-service |
| `com.fasterxml.jackson.core:jackson-databind` | spring5-xml |
| `commons-io:commons-io` | servlet-tomcat |
| `org.slf4j:slf4j-api` | spring5-xml |
| `com.company.legacy:legacy-web-lib` 2.0.0 | boot2-javax-lib-optional only (its `sbmig.e2e.yml` swaps it in for slf4j-api, still 8 entries) |
| `com.company.legacy:legacy-web-lib` 3.0.0 | boot2-method-rename, boot2-fix-failures (same swap, version 3.0.0) |

## Simulated in-house libraries (`fixtures/libs/`)

* `legacy-web-lib/v1.0.0` (Java 8, `javax.servlet` API) and `legacy-web-lib/v2.0.0` (Java 17, `jakarta.servlet`):
  `com.company.legacy:legacy-web-lib`, `LegacyRequestHelper(HttpServletRequest)` + `static clientIp(HttpServletRequest)`.
  `legacy-web-lib/v3.0.0` is the 2.0.0 jakarta build with `clientIp` **renamed** to `remoteClientIp` (no alias).
  `fixtures/libs/install.sh [local-repo]` installs all three (idempotent; `~/.m2` by default, `-Dmaven.repo.local=$1`
  otherwise).
* `ghost-lib/` (`com.company.nonexistent:ghost-lib:1.2.3`) is **never installed**; `ghost-lib/make-repo.sh <dir>` lays it
  out in a throwaway repo only for the as-is build of `missing-artifact`. `scripts/e2e.py` refuses to run that fixture
  if the jar is in `~/.m2` (`requires_absent`).

## Running the e2e

```bash
scripts/e2e.sh                                    # recipes + advisories check, installs fixtures/libs, runs all fixtures
scripts/e2e.sh --only boot2-javax-lib,missing-artifact
scripts/e2e.sh --skip-libs --only euckr-legacy    # do not (re)install fixtures/libs
scripts/e2e.sh --only pure-java,java6-legacy      # the reuse pair always runs together, pure-java first
python scripts/e2e.py --expect-only               # print each fixture's expectations, run nothing
python scripts/e2e.py --install-libs --out out/e2e-x --llm-provider fake
```

For each fixture `scripts/e2e.py` runs `python -m sbmig.cli run fixtures/projects/<name> --config
config/artifacts.local-e2e.yml [--config fixtures/projects/<name>/sbmig.e2e.yml] --out out/e2e`, loads the run's
`report.json` + `journal.jsonl`, evaluates `expect` and prints a table with an `expect` PASS/FAIL column and the first
mismatch; `out/e2e/summary.md|json` hold the details (all mismatches). The exit code is non-zero if any fixture
mismatches (fixtures without `expect` must reach a successful final build). Missing report fields count as empty,
so an engine that does not yet produce e.g. `blocked` shows up as a FAIL with a readable reason, not a crash.
The table's `review` column is the structure-review verdict, `scripts` the fix-script attempts by mode
(`new 2, aug 1, reuse 1`). Fixtures run in `DEFAULT_ORDER` (then alphabetically); every fixture's KB dir is cleared
before its run except a `kb.after` fixture's, so a full run starts each scenario clean without manual cleanup.

### `expect` keys

| key | meaning |
|---|---|
| `success` / `exit_code` | `RunReport.success` and the CLI exit code (0 ok, 2 blocked/unresolved, 3 aborted) |
| `max_llm_calls` | `llm_usage.calls` ≤ n (missing = 0) |
| `max_duration_s` | wall-clock of the `sbmig run` process ≤ n |
| `blocked` | the exact set of blocked items by match: every expected `{reason, artifact "g:a", owner_class?, phase?}` must match ≥ 1 `BlockedItem` (top-level `blocked` or `phases[].blocked`), and every actual item must match an expected one; `[]` = nothing may be blocked |
| `flags` / `forbidden_flags` | each entry must (not) be in `RunReport.flags`; `regex:<re>` = full match |
| `optional_bumped` | each entry (`"g:a old→new"` or `regex:`) must appear in the bumps the engine recorded: `report.optional_bumped`, journal events carrying `optional_bumped`, or phase notes `optional bumped: …` |
| `unresolved_max` | `len(unresolved)` ≤ n |
| `journal_events` | each dict must be a subset of some `journal.jsonl` event, e.g. `{"event": "restructure", "ok": true}` |
| `phases` | `{"restructure": "ok"}` — last result of that phase (`ok`/`FAIL`/`skipped`) |
| `advisories` | each `{id, policy?, outcome?, recipe_source?, status?, library?, replacement?}` must match a `report.advisories[]` entry by `advisory_id`, every given field equal; `advisories_exact: true` forbids unlisted findings; `advisories_absent: [id]` must not be reported |
| `handover` | on `report.handover`: `changes_min`, `carried_over_min`/`carried_over_max` (`len(carried_over)`), `parent_before`/`parent_after` (`g:a[:v]` prefix), `java_*`/`spring_boot_*` (exact), `from_here_contains` (entries of `from_here`), `removed_contains`/`added_contains` (`g:a[:v]`) |
| `followups_contains` | each entry must match some `manual_followups` line |
| `kb` | `{dir, after?, candidates_min?, harvest_candidates_min?, recipes?}` — `dir` (relative to the repo, must be under `out/`; `scripts/e2e.py` deletes it before the run unless `after` is set) is the fixture's own `kb.dir` from its `sbmig.e2e.yml`; `after: <fixture>` (v2 reuse scenario) = continue the KB that fixture filled earlier in the same e2e run (not cleared; precondition mismatch when it did not run before); `candidates_min` counts the recipe documents in `<dir>/recipes/candidates/*.yml`; `harvest_candidates_min` is on `len(report.harvest.candidates_created)`; each `recipes[]` `{recipe, options, name?}` must match a document in `recipes/{candidates,active}/*.yml` (`name` equal when given) with a `recipeList` entry `{<recipe>: {…}}` whose options are a superset of `options` |
| `properties` | on `report.properties` (else `profile.properties`), the externalized-config inventory: `external_min`, `external` (keys that must be `external` findings), `absent` (keys that must not be), `external_in` (`{service: [keys]}`, MSA: external findings whose `service` is that deployable), `written` (⊆ `placeholders_written`), `placeholder_file_contains` (substrings of `placeholder_files[0]`) |
| `structure_review` | v2, on `report.structure_review`: `verdict` (or a list), `critical` / `critical_min` / `critical_max`, `warnings_min` / `warnings_max`, `issues_contain` (over `"<severity> <file>: <problem> -> <fix_hint>"`, `*_contains` rules), `sbmig_check` (an issue sbmig added itself, `(sbmig check) …`), `script_status` (`ok`/`error`/`timeout`/`rejected`, `none` = no script ran), `committed`, `reverted`, `changed_files` (⊆ the script's changed files), `llm_calls_max` |
| `scripts` | v2, on the script attempts (`phases[].attempts` with a `script_mode`) and the KB (`dir`, default `kb.dir`): `attempts` / `attempts_min` / `attempts_max` (`{new, augment, reuse}` counts), `reuse_without_llm` (every reuse attempt made 0 LLM calls, and there is one), `cases[]` `{case \| l2, modes (exact sequence), outcomes (ordered subsequence), versions (exact), llm_calls (sum)}`, `stored[]` `{case \| l2, version, parent?, status?, successes_min/max?, failures_min/max?, partials_min?, success_projects_min?, last_outcome?, code_contains?}` on `<dir>/scripts/<case_id>/v<N>.meta.json` / `.py`, `versions` (`{case or L2: number of stored versions}`); `l2` is hashed like `sbmig.kb.catalog.case_id_for` |
| `post_checks` | checks on the migrated `project/` (paths may use `**` globs): `file_decodes_as`/`file_contains` `{path, encoding, contains, not_contains}` (strict decode), `pom_parent {path, group_id, artifact_id, version}`, `pom_dependency_absent`/`pom_dependency_present {path, artifacts: ["g:a"]}`, `pom_dependency_unversioned`/`pom_dependency_versioned {path, artifacts}` (each listed dependency declared without / with a `<version>` element — e.g. a version the parent manages was dropped; absent = fail); `pom_packaging {path, packaging}` (`jar`/`war`/`pom`, absent `<packaging>` = `jar`), `pom_module_list {path, modules, any_order?}` (the exact top-level `<modules>` list, in order unless `any_order: true`); `module/comp-jar/pom.xml` / `module/boot-jar/pom.xml` are the poms the pre-9c restructure creates for a single-jar Boot app (compile dependencies live in `comp-jar`); company-layout fixtures (round 9c) check `<name>-boot|back|front/…` instead |

`*_contains` entries: `regex:<re>` must fully match an entry (DOTALL), a plain string must be a substring of one.

Top-level helpers: `catalog` (IDs), `requires_libs` (`g:a:v` that must be in the local repo — run `install.sh`),
`requires_absent` (`g:a:v` that must not be there).

## Adding a scenario

1. Create `fixtures/projects/<name>/` (≤ 10 Java files, realistic usages), verify the as-is build with its JDK
   (`JAVA_HOME=… mvn -q -DskipTests package`, plus whatever the scenario needs) and record the command in its
   `README.md`.
2. Write `expected.json` with the analyze keys and an `expect` block; add the catalog ID to `catalog` and to the
   "Fixture" column of `docs/ERROR-HANDLING.md`. If the scenario needs a different artifact policy, add a
   `sbmig.e2e.yml` override (lists replace — keep exactly 8 optional artifacts).
   Every fixture's `sbmig.e2e.yml` sets the replayed LLM (`llm.provider: fake`, `llm.base_url:
   "replay:fixtures/projects/<name>/replay"`) and its own `kb.dir: out/e2e/kb-<name>` (= `expect.kb.dir`); the replay
   holds the structure-review answer (`llm-001.json`) and one entry per case the build reports (format above). To find
   the case ids, run the fixture once with `SBMIG_LLM__RECORD_DIR=out/<dir>` and read the `case: …` header of the
   recorded fix-script prompts.
3. Add the fixture to `NAMES`/`SCENARIOS` and `BUILD_MATRIX` in `tests/fixtures/test_fixture_projects.py`, to
   `DEFAULT_ORDER` in `scripts/e2e.py`, and to the matrix above.
4. `python -m pytest tests/fixtures` (schema) and `python -m pytest tests/fixtures -m integration -k <name>` (as-is build).

Integration check: `python -m pytest tests/fixtures -m integration` builds every fixture with its documented JDK
(scenario libraries go into a throwaway repo via `maven.repo.local.tail`, so `~/.m2` is not modified).
