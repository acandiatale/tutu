# Error-case catalog — what sbmig does when things go wrong

Principle: **never loop on what we cannot fix, never hide it.** Every cluster ends in exactly one of
`fixed` (handler / recipe / KB / LLM, verified by a build), `blocked` (deliberately skipped — needs a library,
infra or human action; listed with the most actionable context we can extract), or `unresolved` (we tried and
failed within budget). Blocked clusters are remembered for the whole run (by signature) so later phases do not
retry them, and the migration **continues** with everything else (restructure, verify, report).

Legend — Policy: `H` deterministic handler, `R` OpenRewrite recipe, `K` knowledge base, `L` LLM (n = max attempts),
`B` block + report. Fixture = scenario under `fixtures/projects/` or `fixtures/scenarios/` exercised by
`scripts/e2e.py` with an **expected outcome** (success / blocked / flag), so failure paths are asserted, not just observed.

## 1. External / infrastructure blockers (B)

| ID | Symptom (detection) | Policy | Report says | Fixture |
|---|---|---|---|---|
| X1 javax API in a dependency | after jakarta migration: `incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to javax.servlet.http.HttpServletRequest`, `method/constructor … cannot be applied … required: javax.* found: jakarta.*`, `no suitable constructor`, `… does not override … (javax.* supertype)` | if the owner artifact is one of the configured optional in-house artifacts → its version bump (done right after the spring rewrite) is the fix; else **B immediately (0 LLM)**. Owner class → jar → GAV attributed via `mvn dependency:build-classpath` | "`com.company:legacy-web:1.4` (class `…RequestHelper`) still takes `javax.servlet.http.HttpServletRequest`; upgrade to a Jakarta build or add it to `artifacts.optional` with that version. Files: …" | `boot2-javax-lib` (blocked, exit 2) / `boot2-javax-lib-optional` (bumped → success) |
| X2 dependency API changed (non-javax) | `cannot find symbol … location: class <library class>`, `method … cannot be applied` with library owner | L1 (the LLM may adapt to a renamed method) then B | owner artifact + symbol | unit |
| X3 artifact not found | `Could not find artifact g:a:v in <repo>` / `The following artifacts could not be resolved` | relocation H first; if the artifact is unknown → **B (0 LLM)** — the LLM must never invent versions | artifact + repository URL; "proxy it in Nexus or fix the coordinates" | `missing-artifact` (blocked) |
| X4 parent not resolvable | `Non-resolvable parent POM` | B, abort run early with a clear preflight message when it is the configured in-house parent | parent GAV, settings file used | unit + `sbmig run --config bad-parent.yml` |
| X5 build plugin incompatible with JDK 21 | `PLUGIN_EXECUTION` from a plugin with no version floor in `versions.yml` (`Unsupported class file major version 65`, `NoSuchMethodError … javac`) | H floor if known; else L1 then B | plugin GAV + message | unit |
| X6 unsupported sources | Kotlin/Groovy/Scala compile errors, generated-source plugins (protobuf, jaxb2, openapi) failing | warn in analyze; errors → B(unsupported_source) | module + plugin | unit |

## 2. Environment / legacy quirks (H)

| ID | Symptom | Policy | Fixture |
|---|---|---|---|
| E1 legacy source encoding | `unmappable character (0x..) for encoding UTF-8` (JDK 18+ defaults to UTF-8; MS949/EUC-KR legacy sources) | H `source-encoding`: detect encoding (utf-8 → cp949 → euc-kr …), `set-property` (`project.build.sourceEncoding`) or `convert-utf8`; the Workspace reads/writes every file in its detected encoding so LLM patches never corrupt Korean comments | `euckr-legacy` (success) |
| E2 UTF-8 BOM | `illegal character: '﻿'` | H `strip-utf8-bom` | unit |
| E3 zip with CP949 file names | mojibake names after extraction | intake re-decodes cp437→cp949 when the UTF-8 flag is absent | unit |
| E4 enforcer rules | `requireJavaVersion [1.8,9)` / `dependencyConvergence` failing | H `enforcer-relax` (relax the rule in-project) → fallback `-Denforcer.skip=true` flagged | `enforcer-java8-werror` (success + flag) |
| E5 `-Werror` | deprecation warnings fatal on JDK 21 | H `remove-werror` (flag) | same |
| E6 managed versions lost by the parent swap | `'dependencies.dependency.version' … is missing` after restructure | H `carry-over-managed-version`: versions captured from the **as-is effective pom** at intake (`mvn help:effective-pom`) are pinned explicitly; else `versions.yml`; else L1 | `boot2` with a dependency managed only by the old parent (unit + e2e variant) |
| E7 `system`-scoped jars (`WEB-INF/lib`, `systemPath`) | compile needs jars outside Nexus | analyze warns; restructure moves the `lib/` dir with the sources and rewrites `systemPath`; follow-up "install into Nexus" | unit |
| E8 OpenRewrite OOM / timeout | `OutOfMemoryError` in the rewrite log | one retry with `oom_retry_jvm_args`; then continue to the build loop (the build tells what is left) | unit |
| E9 rewrite:run cannot fork compile | forked run fails in `compile` | automatic `runNoFork` retry (exists) | integration |

## 3. Loop safety (the orchestrator protects the budget)

| ID | Symptom | Policy | Test |
|---|---|---|---|
| L1 stall | identical error multiset `stall_limit` times | escalation model + widened context, then B(repeated_failure) | loop unit |
| L2 oscillation | error sets toggling A↔B↔A within `oscillation_window` | treat as stall → B(oscillation) | loop unit |
| L3 regression | errors × `regression_ratio` or **structural kinds** (`pom_invalid`, `parent_resolution`, `plugin_resolution`) appearing after a pom edit | revert that attempt (git) regardless of count | loop unit (LLM patch breaks pom XML) |
| L4 LLM protocol garbage / empty | no patches parsed, repeatedly | `max_llm_attempts_per_cluster` then B(repeated_failure); run continues | loop unit (fake LLM returns prose) |
| L5 LLM unavailable | HTTP errors after retries / missing key | B(llm_unavailable) for LLM-only clusters; handlers/recipes still run; report says the LLM was unavailable | loop unit |
| L6 budget exhausted | `max_llm_calls` / `max_total_minutes` | remaining LLM-only clusters → B(budget_exhausted); verify + report always run | loop unit |
| L7 test-compile only | only TEST_COMPILE clusters remain | `-Dmaven.test.skip=true` fallback, flag `test-compile-skipped`, errors listed as follow-ups (exists) | e2e |
| L8 pre-existing breakage | the as-is project does not compile on its own JDK | we never build as-is; errors appear after JDK21 — reported like any other; `--baseline` (optional) records the as-is build result for comparison | runbook |

## 4. Structure edge cases (analyze / restructure)

| ID | Case | Policy | Test |
|---|---|---|---|
| S1 MSA with shared library modules (`common`) | not a service (no boot plugin) but must be migrated too | planner treats every in-repo root pom as a migration target; parent swap on all root poms | plan unit + `msa` variant |
| S2 already migrated (Java 21 / Boot 4.1 / restructured) | idempotent: recipes no-op, restructure returns "already restructured" | unit |
| S3 pom-only aggregator / no sources | analyze → `warnings`, plan has no rewrite steps, restructure only poms | unit |
| S4 Gradle / Ant | reject with a clear message (exit 2, report explains) | unit |
| S5 non-standard module layout (`<module>../x</module>`, in-repo parent at odd path) | wrap policy keeps relative paths consistent; else abort restructure (ok=False) and still verify | unit |
| S6 optional artifact only transitive | not bumped (only declared usage counts) — documented | — |
| S7 as-is project is already a multi-module WAR + JAR | wrap policy; WAR → jar follow-up | unit |

## 5. Report sections produced for the above
* **Blocked — needs human/library action**: one row per `BlockedItem` (reason, artifact/owner class, required vs found type, files, suggestion, attempts made).
* **Unresolved errors**: everything else that still fails.
* **Manual follow-ups** and **flags** (`test-compile-skipped`, `enforcer-skipped`, `werror-removed`, `source-encoding-set:cp949`, `stopped-after-…`).
* Exit code: 0 success, 2 finished with blocked/unresolved, 3 aborted (bad input / parent not resolvable / no Maven).
