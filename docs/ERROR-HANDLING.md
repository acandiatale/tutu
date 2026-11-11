# Error handling — how a build error travels

Principle: **never loop on what we cannot fix, never hide it.** Every case ends as `fixed` (verified by a rebuild), `blocked`
(deliberately handed over — needs a library, infrastructure or owner decision, with the most actionable context we have) or
`unresolved` (tried within the caps and failed). A blocked case is remembered for the whole run and the migration goes on;
the final build and the report always happen. Architecture: `docs/ARCHITECTURE.md` §3–§5; Korean detail:
`agent-doc/04-cases-scripts-kb.md`, `agent-doc/03-run-debug-and-logs.md`.

```
mvn log ─► log parser ─► BuildError (L1 / L2) ─► case (L2) ─► triage ─► pre-try │ reuse │ augment │ new │ BLOCKED
                                                                              ─► commit ─► rebuild scores it ─► errors.jsonl / KB / report
```

## 1. From log to case

* **Log parser** (`sbmig/build/log_parser.py`) turns each Maven / javac error into a `BuildError` with an `ErrorKind`
  (`compile`, `test_compile`, `dependency_resolution`, `plugin_resolution`, `parent_resolution`, `plugin_execution`,
  `enforcer`, `pom_invalid`, `rewrite`, `test_failure`, `unknown`), file, line, symbol, location and the raw block. A POM problem
  inherited from a parent pom names that parent pom.
* **Signatures** (`sbmig/build/signatures.py`): L1 (specific) and L2 (generic, the case key); see `docs/ARCHITECTURE.md` §5.
* **Case** = all errors of a build sharing one L2; case id = `sha1(L2)[:10]` (`sbmig/errors/common.py`). Cases are ordered by
  kind (pom / resolution first, then compile in reactor order) and root cause first (`sbmig/triage/cluster.py`). A case whose
  files, or whose module pom, an attempt already touched this round waits for the rebuild (not counted as a try).
* **Environment errors** the runner handles before the loop: a file under `target/` locked by another process (Windows) is
  retried `build.file_lock_retries` times (patterns `knowledge/config/build_env_errors.yml`); a Maven negative-cache hit
  ("was cached in the local repository") reruns the build once with `-U` (`build.retry_negative_cache`).

## 2. Zero-LLM pre-tries

Before any LLM call, `BuildFixLoop._pre_tries` (`sbmig/loop/fixloop.py`) offers the first matching deterministic fix. A pre-try
is not a ladder step and stores no script; it is committed as `fix(<source>): case:<case_id>`, once per target and run, and
scored like any attempt.

| Source | Matches | Does |
|---|---|---|
| `sbmig-parent` | a `Non-resolvable parent POM` whose g:a is a configured company parent (`artifacts.parent` / `boot_parent` / `comp_parent`, or an alias) at another version than the configured one (`openrewrite.unresolvable_asis_parent: align`) | sets the configured version in every pom whose `<parent>` is that g:a at that version (`sbmig/restructure/asis_parent.py`, text-preserving); journal `asis_parent_aligned` (`where: fix loop`), flag `asis-parent-aligned`, one Handover line. Tried even though triage blocks the case as `parent_not_found` |
| `sbmig-version` | L2 `pom_invalid\|missing_version` | fills the version: managed by the new parent chain → no version; else the as-is build's version; a dependency just added → the repository's newest release |
| `sbmig-resources` | resources / war plugin fails on a binary under filtering (`MalformedInputException`, `Input length = 1`) | adds the extension to `nonFilteredFileExtensions` in the pom that declares the filtering (`sbmig/restructure/resources_filtering.py`) |
| `sbmig-bundled` | a package the case misses is provided by a jar in the tree no pom declares (`logs/bundled-jars.json`) | plain dependency when the jar's own coordinates are in the repository, else copied to `<module>/lib/` and declared `system` scope + `includeSystemScope` on the boot module (`sbmig/restructure/system_jars.py`); a jar whose g:a the target chain manages (superseded) is never wired |
| `sbmig-jakarta` | a javax case whose library has a jakarta build sbmig verified in the repository set (`sbmig/build/jakarta_discovery.py`) | swaps the declaration (other coordinates, or the same coordinates' jakarta version / classifier); a higher major is flagged ⚠ in the Handover |

Every LLM attempt also passes `_fill_versions` before its commit: a version-less dependency the LLM added gets its version by the
same rules (the LLM is told to leave unknown versions `null`; versions are never guessed).

Most of these causes are prevented earlier, before the first build: the restructure pins as-is versions, writes
`nonFilteredFileExtensions` for binaries it finds and wires bundled jars (`docs/ARCHITECTURE.md` §8); the intake removes
duplicate reactor listings.

## 3. The script ladder (`sbmig/triage/scripts.py`)

Per case and run, in order:

1. **Reuse** (`kb-script`, no LLM): the best stored version from `kb-v2/cases/<case_id>/` (newest proven, else newest unverified;
   retired and non-idempotent excluded), up to `script.reuse_versions` (1). `script.reuse: false` skips it.
2. **Augment** (`llm-augment`): the LLM extends this run's last script for the case into a complete new script, asked on the
   reverted tree; up to `script.max_augments_per_case` (15).
3. **New** (`llm-new`): when there is nothing to augment or augments are used up. `<edits>` apply at once; the `<script>` is
   stored `unverified`.
4. **BLOCKED `repeated_failure`** once the case's LLM asks reach `script.max_llm_tries_per_case` (20).

Other rules:

* If one half of an answer fails (edits do not apply, script errors, no change) the other half of the same answer is tried.
* Direct edits only (`<no_script>` + `<edits>`, `llm-edit`) — project-specific changes such as replacing an EOL library by
  project code — are never stored; capped by `script.max_edit_tries_per_case` (4).
* A decline (`<blocked>`, or an answer with neither edits nor script) blocks after `script.max_declines_per_case` (1) with the
  reason triage already found, else `needs_owner`; sibling cases with the same cause are blocked without another call. A decline
  that contradicts facts sbmig found (a jakarta build exists, a bundled jar provides the package) or that concerns a library whose
  `knowledge/notes` note carries `replacement: true` is rejected and asked again with the facts (edits inside it still apply).
* `llm.escalation_model` is used from the case's third ask and when the loop stalls (the same error set as the previous build;
  `loop.stall_limit`). Error sets toggling within `loop.oscillation_window` builds `loop.oscillation_limit` times block the cases
  involved as `oscillation`.
* A pinned case (`sbmig kb cases pin <case_id> block[:REASON]`) is blocked without a try.

## 4. Scoring by rebuild

Only the next build judges an attempt (`BuildFixLoop._score`; outcomes `AttemptOutcome` in `sbmig/core/models.py`):

| Outcome | When | Then |
|---|---|---|
| `build_ok` | the build passed | kept, KB success |
| `improved` | every targeted error is gone and the build reached the stage that reports them ("not visible" is not "resolved") | kept, KB success |
| `partial` | some targeted errors gone, no new error in touched files | resolved items recorded; reverted before the case's next try (a pre-try's partial is kept); no KB credit either way |
| `no_change` | nothing changed | reverted, KB failure |
| `regressed` | new errors in touched files, error count above `loop.regression_ratio` (1.5) ×, or structural errors (`loop.structural_regression_kinds`) after a pom edit — a pom fix that only uncovers the next build stage is not a regression | reverted (`loop.revert_on_regression`) → `reverted` |
| `inconclusive` | the build could not tell (timeout, no parsed error, stopped at an unrelated earlier stage) | no KB credit, counts as tried |
| `apply_failed` | the script or edits did not apply | uncommitted changes discarded |

Credit goes to the stored script version (`case.json`: successes, failures, projects, runs); a script becomes `proven` when it
fixes the case on a project other than the one that made it.

## 5. Blocked reasons and policies

`classify_blocked` (`sbmig/triage/blocked.py`) only classifies, conservatively (in doubt: not blocked). How many script attempts a
reason gets first is `loop.blocked_policy.<reason>` (0 = hand over at once; a stored script is reused regardless). `BlockedItem`s
go to the report's "Blocked" section with a suggestion composed by `compose_suggestion` (`sbmig/loop/blocked_registry.py`).

| ID | Reason (`BlockReason`) | Detection | Policy | Suggestion |
|---|---|---|---|---|
| X1 | `external_javax_api` | a dependency's API still takes `javax.*` types (`… cannot be converted to javax…`, `required: javax… found: jakarta…`), the project no longer imports that javax package and the owner class is not a project class | 2 | via the dependency tree: (1) a library in `knowledge/libraries/jakarta_libraries.yml` → raise / relocate to its jakarta release; (2) an in-house artifact → needs a Jakarta build, add it to `artifacts.optional`; (3) another library → upgrade or replace; (4) the javax API jar declared in a pom → remove it; else `mvn dependency:tree -Dincludes=<api jar>` |
| X2 | `external_api_incompatible` | a library API changed (non-javax `cannot find symbol` / `cannot be applied` with a library owner) | 2 | upgrade the library or adapt the call |
| X3 | `artifact_not_found` | `Could not find artifact g:a:v` | 2 | proxy it in Nexus or fix the coordinates; the LLM never invents versions |
| X4 | `parent_not_found` | `Non-resolvable parent POM` | 0 | settings / mirror / `artifacts.parent`. Only the configured coordinates themselves (g:a and the configured version) abort the run (`aborted:parent-not-found`; the message names the coordinates Maven could not resolve, says that this is the configured version and names the settings file). A company parent g:a at another (as-is) version is aligned by `sbmig-parent` (§2) under `openrewrite.unresolvable_asis_parent: align`, blocked without abort under `continue`, and aborts only under `abort`; a foreign parent is blocked without abort |
| X5 | `plugin_incompatible` | a build plugin fails on JDK 21 | 2 | raise the plugin version or remove it |
| X6 | `unsupported_source` | Kotlin / Groovy / Scala or source-generator plugin failures | 0 | migrate by hand |
| X7 | `reactor_duplicate` | `Project … is duplicated in the reactor` that the intake could not de-duplicate | 0 | fix the `<module>` lists; never sent to the LLM |
| – | `file_lock` | `target/` still locked after the runner's retries | 0 | antivirus exclusion / close the holder, rerun (`infra:file-lock`) |
| – | `encoding` | source encoding undetermined | 0 | set `project.build.sourceEncoding` or convert to UTF-8 |
| – | `needs_owner` | the LLM declined with a blocker outside the project | 0 | the owning team decides |
| – | `repeated_failure`, `oscillation` | ladder caps (§3) | – | review the attempts in `errors.md` |
| – | `llm_unavailable`, `budget_exhausted` | §6 | – | rerun when the gateway / budget allows |

A library whose `knowledge/notes` note says `replacement: true` (e.g. spring-mobile) is never blocked as X1 / X2; the direct-edit
cap ends it instead.

## 6. Loop safety

* **LLM unavailable**: transport / configuration failures (explicit status 401 / 403 / 404 / 408 / 429 / 5xx, exception class,
  missing key) twice in a row pause the loop once for `llm.cooldown_seconds` (flag `llm-cooldown`); a second streak blocks the
  remaining LLM-only cases `llm_unavailable`. A 400 / 413 / 422 or a content-filter answer is the request's fault and never counts;
  the same 400 body for two different prompts flags `llm-config-error` and stops LLM calls for the run. A 429 / 5xx with
  `Retry-After` waits up to `llm.busy_wait_seconds`.
* **Budgets**: `loop.max_llm_calls` (0 = unlimited, -1 = stored scripts only) and `loop.max_total_minutes`; exhausted → remaining
  LLM-only cases `budget_exhausted`, flags `llm-budget-exhausted` / `time-budget-exhausted`; the final build still runs.
* **Tests**: every build runs with `-Dmaven.test.skip=true` (`build.skip_test_compile`, flag `tests-not-compiled` + follow-up).
  Where a config runs tests, `build.allow_skip_test_compile_fallback` / `allow_skip_tests_fallback` rebuild without them when only
  test errors remain (flags `test-compile-skipped` / `tests-skipped`).
* **Internal error / abort**: the run guard flags `aborted` (exit 3), still runs the planned final build (not for deliberate aborts
  such as no Maven or an unresolvable parent) and writes the report; an aborted run is never a success.
* **Restructure step failed**: flag `restructure-failed`, the verify build still runs, the run is not a success (exit 2).
* **OpenRewrite**: OOM → one retry with `openrewrite.oom_retry_jvm_args`; download failures → wait and retry
  (`openrewrite.download_retry`); literal `4.0.x`-style versions left in a pom → reset and rerun once, else flag
  `rewrite-unresolved-versions`; files the plugin could not parse → flag `rewrite-parse-problems`. The run continues — the
  build shows what is left.
* **OpenRewrite and an unresolvable parent** (the recipes run on the as-is poms, before the restructure sets the company
  parents): the configured parent coordinates missing → abort as in §5 X4. A company parent g:a at an as-is version the
  repository lacks → `openrewrite.unresolvable_asis_parent` (`align` by default: the poms are set to the configured version,
  committed `transform: as-is parent <g:a> <old> → <configured> (as-is version not in the repository)`, journal
  `asis_parent_aligned`, flag `asis-parent-aligned`, one Handover line, OpenRewrite runs once more — on the target parent's
  classpath; a retry that fails on the configured version aborts; `continue` = as for a foreign parent; `abort` = stop). A
  foreign parent → the run continues without recipes: flag `rewrite-skipped-asis-parent`, journal
  `rewrite_skipped_asis_parent`, one Handover line. The intake's as-is `help:effective-pom` fails for the same reason; it stays
  non-fatal and logs `the parent <g:a:v> is not in the repository` (journal `effective_pom.reason`).
* **Server shutdown**: open SSE streams are ended and every registered Maven process tree is killed
  (`sbmig.build.mvn.kill_live_processes`).

## 7. Infrastructure outcomes

At finalize a run that ended for an infrastructure reason gets `infra:<kind>`: `llm` (LLM unavailable / configuration error on a
failed run), `maven` (`mvn-not-found`), `jdk` (Maven on another JDK), `repository` (the configured parent not found —
`aborted:parent-not-found` — or every error of the failed final build is a resolution error with network / repository text;
an as-is parent version that sbmig aligned leaves no `infra:repository` on a run that then builds), `disk` (no space left),
`file-lock`. The code appears in
`SBMIG-RESULT … attn=` and the fleet's breaker defers such projects (`docs/FLEET.md`).

## 8. Analyzer warnings (never blocking)

The analyzer (`sbmig/analyze/analyzer.py`, `sbmig/analyze/edges.py`) records, as `profile.warnings` / `LibraryUsage`, what the
report, the structure review and the fix prompt should know: E7 `system`-scoped jars (the restructure moves `lib/` with the sources and rewrites `systemPath`); S3 pom-only
aggregators (no rewrite steps); S4 non-Maven builds (Gradle and trees without pom.xml / build.xml are rejected at intake, Ant is
converted); S5 modules outside the root (`<module>../x</module>`, skipped under `merge` with a follow-up); S6 optional in-house
artifacts used only transitively (not bumped); X6 Kotlin / Groovy / Scala and source generators; Java source levels below 1.8
(left to `UpgradeToJava21` + the fix loop); legacy (cp949 / euc-kr) source encodings without `project.build.sourceEncoding`.
After the final build, javax EE API jars still on a module's classpath are listed from `mvn dependency:tree`
(`report.javax_api_inventory`, flag `javax-api-on-classpath`).

## 9. Where errors end up

* `<run dir>/errors.jsonl` + `errors.md` (`sbmig/errors/entries.py`): every error of the run with every try — source
  (`sbmig-version`, `kb-script`, `llm-augment`, …), cause, solution, before / after snippets, diff, score.
* `kb-v2/errors.jsonl` gets the run's list at the end; `kb-v2/cases/<case_id>/case.json` and `fix_v<N>.py` carry the scripts and
  their record; `kb-v2/cases.md` is regenerated (`sbmig kb errors list`, `sbmig kb cases show <case_id>`).
* `report.md`: Handover first (blocked items and unresolved errors are pointed to from "From here on, your team"), then "Error
  list", "Fixes by case" (`script:<case>:v<N>`), "Structure review", "Blocked", "javax API jars", flags. Exit code: 0 success,
  2 finished with blocked / unresolved / attention flags, 3 aborted. EOL library advisories are guidance only and never change it
  (`docs/ADVISORIES.md`).
