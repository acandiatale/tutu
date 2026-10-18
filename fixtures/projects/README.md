# Fixture projects

Small but real Maven projects used as the sbmig test corpus (unit tests read them; integration tests and
`scripts/e2e.py` build and migrate them). Each fixture has a `README.md` (exact as-is build command) and an
`expected.json`: what `sbmig analyze` should report **plus an `expect` block** — the asserted outcome of
`sbmig run` (success / blocked / flags / exit code / LLM calls / post-checks on the migrated sources).
Happy-path fixtures use only Maven Central; scenario fixtures may need the simulated in-house libraries in
`fixtures/libs/` (installed by `fixtures/libs/install.sh`).

## Matrix

Catalog IDs refer to `docs/ERROR-HANDLING.md`.

| fixture | case | catalog | as-is build JDK | expected outcome (`expect`) |
|---|---|---|---|---|
| `pure-java` | `pure_java` | — | 8 | success, exit 0, 0 LLM, commons-lang3 → 3.17.0, guava → 33.4.8-jre |
| `servlet-tomcat` | `servlet_tomcat` | — | 8 | success, exit 0, 0 LLM, commons-io → 2.19.0 |
| `spring5-xml` | `spring_legacy` | — | 8 | success, exit 0, ≤ 2 LLM calls (`WebMvcConfigurerAdapter` is an LLM/KB target), slf4j-api → 2.0.17 |
| `boot2` | `spring_boot` | — | 11 | success, exit 0, 0 LLM, commons-lang3 → 3.17.0, lombok → 1.18.38 |
| `boot3` | `spring_boot` | — | 21 | success, exit 0, 0 LLM, guava → 33.4.8-jre |
| `msa` | `msa` | — | 11 / 21 per service | success, exit 0, 0 LLM, lombok → 1.18.38 |
| `boot2-javax-lib` | `spring_boot` | X1 | 11 (+ `install.sh`) | **blocked** `external_javax_api` `com.company.legacy:legacy-web-lib`, restructure still runs (journal `restructure ok`), `success=false`, exit **2**, 0 LLM |
| `boot2-javax-lib-optional` | `spring_boot` | X1 | 11 (+ `install.sh`) | per-fixture `sbmig.e2e.yml` makes legacy-web-lib optional (2.0.0) → bumped `1.0.0→2.0.0`, nothing blocked, success, exit 0, 0 LLM |
| `euckr-legacy` | `pure_java` | E1 | 8 with `JAVA_TOOL_OPTIONS=-Dfile.encoding=MS949` | success, exit 0, 0 LLM, flag `source-encoding-set:(cp949\|ms949\|euc-kr\|euc_kr)`, migrated `Greeting.java` still decodes as cp949 with `안녕하세요` |
| `enforcer-java8-werror` | `pure_java` | E4, E5 | 8 | success, exit 0, 0 LLM, flags `enforcer-(relaxed\|skipped)` and `werror-removed` |
| `missing-artifact` | `pure_java` | X3 | 8 with `-Dmaven.repo.local.tail=<ghost repo>` | **blocked** `artifact_not_found` `com.company.nonexistent:ghost-lib`, `success=false`, exit **2**, 0 LLM, ≤ 240 s |
| `msa-shared-lib` | `msa` | S1 | 18 (whole reactor) | success, exit 0, 0 LLM; `common` + services on jakarta, `common/pom.xml` gets the parent but no mandatory deps |
| `boot2-cloud-legacy` | `spring_boot` | advisories (`docs/ADVISORIES.md`) | 11 | **reference run for library advisories + Handover**: success, exit 0, 0 LLM, nothing blocked; advisories `springfox` (recipe → replaced by springdoc), `sleuth` (Boot 4 umbrella → replaced by Micrometer Tracing), `spring-cloud-bom` (train upgraded → replaced), `hystrix` (guide → still-present, Resilience4j in "From here on" + manual follow-ups); ≥ 1 carried-over pin; springfox/sleuth gone from every pom, `springdoc-openapi-starter-webmvc-ui` in `module/comp-jar/pom.xml` |

Optional artifacts from `config/artifacts.local-e2e.yml` (exactly 8) used directly (→ "optional bump" path):

| artifact | fixtures |
|---|---|
| `org.apache.commons:commons-lang3` | pure-java, boot2, boot2-javax-lib*, euckr-legacy, enforcer-java8-werror, missing-artifact |
| `com.google.guava:guava` | pure-java, boot3 |
| `org.projectlombok:lombok` | boot2, boot2-javax-lib*, msa/order-service, msa-shared-lib/order-service |
| `com.fasterxml.jackson.core:jackson-databind` | spring5-xml |
| `commons-io:commons-io` | servlet-tomcat |
| `org.slf4j:slf4j-api` | spring5-xml |
| `com.company.legacy:legacy-web-lib` 2.0.0 | boot2-javax-lib-optional only (its `sbmig.e2e.yml` swaps it in for slf4j-api, still 8 entries) |

## Simulated in-house libraries (`fixtures/libs/`)

* `legacy-web-lib/v1.0.0` (Java 8, `javax.servlet` API) and `legacy-web-lib/v2.0.0` (Java 17, `jakarta.servlet`):
  `com.company.legacy:legacy-web-lib`, `LegacyRequestHelper(HttpServletRequest)` + `static clientIp(HttpServletRequest)`.
  `fixtures/libs/install.sh [local-repo]` installs both (idempotent; `~/.m2` by default, `-Dmaven.repo.local=$1` otherwise).
* `ghost-lib/` (`com.company.nonexistent:ghost-lib:1.2.3`) is **never installed**; `ghost-lib/make-repo.sh <dir>` lays it
  out in a throwaway repo only for the as-is build of `missing-artifact`. `scripts/e2e.py` refuses to run that fixture
  if the jar is in `~/.m2` (`requires_absent`).

## Running the e2e

```bash
scripts/e2e.sh                                    # recipes + advisories check, installs fixtures/libs, runs all 13 fixtures
scripts/e2e.sh --only boot2-javax-lib,missing-artifact
scripts/e2e.sh --skip-libs --only euckr-legacy    # do not (re)install fixtures/libs
python scripts/e2e.py --expect-only               # print each fixture's expectations, run nothing
python scripts/e2e.py --install-libs --out out/e2e-x --llm-provider fake
```

For each fixture `scripts/e2e.py` runs `python -m sbmig.cli run fixtures/projects/<name> --config
config/artifacts.local-e2e.yml [--config fixtures/projects/<name>/sbmig.e2e.yml] --out out/e2e`, loads the run's
`report.json` + `journal.jsonl`, evaluates `expect` and prints a table with an `expect` PASS/FAIL column and the first
mismatch; `out/e2e/summary.md|json` hold the details (all mismatches). The exit code is non-zero if any fixture
mismatches (fixtures without `expect` must reach a successful final build). Missing report fields count as empty,
so an engine that does not yet produce e.g. `blocked` shows up as a FAIL with a readable reason, not a crash.

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
| `post_checks` | checks on the migrated `project/` (paths may use `**` globs): `file_decodes_as`/`file_contains` `{path, encoding, contains, not_contains}` (strict decode), `pom_parent {path, group_id, artifact_id, version}`, `pom_dependency_absent`/`pom_dependency_present {path, artifacts: ["g:a"]}`; `module/comp-jar/pom.xml` / `module/boot-jar/pom.xml` are the poms restructure creates for a single-jar Boot app (compile dependencies live in `comp-jar`) |

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
3. Add the fixture to `NAMES`/`SCENARIOS` and `BUILD_MATRIX` in `tests/fixtures/test_fixture_projects.py`, to
   `DEFAULT_ORDER` in `scripts/e2e.py`, and to the matrix above.
4. `python -m pytest tests/fixtures` (schema) and `python -m pytest tests/fixtures -m integration -k <name>` (as-is build).

Integration check: `python -m pytest tests/fixtures -m integration` builds every fixture with its documented JDK
(scenario libraries go into a throwaway repo via `maven.repo.local.tail`, so `~/.m2` is not modified).
