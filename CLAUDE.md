# sbmig — working notes for Claude (read this first)

In-house **JDK 21 + Spring Boot 4.1.0 migration agent** for company Maven projects. Python 3.10+, CLI `sbmig`.
Read in this order: `README.md` → `docs/PLAN.md` (one "Status — round N" table per round, newest at the bottom)
→ `docs/INTERFACES.md` (contracts) → the doc for the area you touch (`ARCHITECTURE`, `FLEET`, `ADAPTERS`,
`RECIPES`, `ADVISORIES`, `ERROR-HANDLING`, `RUNBOOK`).
The Korean handbook for the company (`docs/handbook/`, README → 01…09 + `sbmig-overview.pptx` + `sbmig-reference.xlsx`)
explains the same system for operators; `tests/docs/test_handbook.py` keeps its paths / config keys / CLI names /
links in sync with the code, and `scripts/handbook/` regenerates the xlsx / pptx. Update the handbook when behaviour
the operators see changes (CLI, config keys, handlers, report sections, log lines).

## Why this exists
- The user takes this code into their company to test it there. Company source code is **not** reachable from here.
- The previous in-house agent failed for two reasons we must not repeat: the fix LLM "finished" without running
  the build, and recipe selection by a small LLM was weak. Hence: deterministic planning, build-gated phases.

## Company constraints (stated by the user; not derivable from the code)
- Four project cases: pure Java, Spring 4/5 + Boot 2/3, Tomcat servlet-only, MSA (several deployable services).
- Company layout (stated 2026-10-21, round 9c): the root pom inherits the in-house **`base-parent`** (`artifacts.parent`)
  and is an aggregator; the **2 mandatory** dependencies are declared in every root pom (`artifacts.mandatory_target:
  root`); the **8 optional** in-house artifacts are bumped only when already used; all in-house artifacts are on one
  version line (4.0.0). Non-Boot projects (Spring Framework, servlet, pure Java) are split into modules directly
  under the root: `{name}-boot` (Boot configuration only; parent **`boot-jar`** = `artifacts.boot_parent`),
  `{name}-back` (every source incl. controllers; parent **`comp-jar`** = `artifacts.comp_parent`) and, only when
  web resources exist, `{name}-front` (static files under `src/main/resources/static`, JSPs under
  `src/main/resources/META-INF/resources/WEB-INF/jsp` per Servlet 3; parent `comp-jar`); JSPs make the boot
  module a WAR for external Tomcat, otherwise it is an executable jar. `{name}` = the root artifactId. Spring Boot
  as-is projects (incl. MSA, incl. projects migrated by the earlier in-house tool) keep their directories: poms
  only — root → base-parent, `*-boot` modules whose pom is a Boot app → boot-jar, every other module → comp-jar;
  a single-module Boot app is wrapped into `{name}-boot`. The pre-9c layout (`module/boot-jar` + `module/comp-jar`,
  Boot projects split) stays available through `layout.*` and is pinned by `config/artifacts.local-e2e.yml` for
  the older fixtures.
- Only Maven projects: Gradle / Ant builds are rejected at intake (exit 3) — the user's decision (2026-10-20).
- MSA: same parent / mandatory / optional per service, directory structure untouched (pom-only mode).
- Scope = packaging + build passing (`mvn clean package` → BUILD SUCCESS). Runtime errors belong to the as-is owners.
- Real in-house coordinates are unknown here → placeholders in `config/artifacts.example.yml`; public stand-ins in
  `config/artifacts.local-e2e.yml` for local e2e. Never invent "real" company coordinates.
- The company has its own LLM API gateway and its own agent-building framework. They plug in through the adapter
  seams (`llm.plugin` tier 1, `fix.plugin` tier 2, see `docs/ADAPTERS.md`), not through new HTTP clients.
- Operating model: **one agent migrates ~500 projects strictly sequentially**. Every fix is recorded
  (error-case symbol → fix label → recipe) so later projects benefit: `sbmig fleet`, `kb/ledger.jsonl`, `kb/symbols.json`.
- EOL / unmanaged libraries (springfox, hystrix, sleuth, old Spring Cloud trains, …): **no `block` policy, ever**
  (user: "정책상 빌드로 보지 않는건 없을거야" — 500 projects cannot all be perfect). Build as far as possible, then
  hand over a compressed summary: report **Handover** section first, advisory policies `guide` | `recipe` only.
- LLM keys come only from the environment (`LLM_API_KEY`). Never write a key into the repo, a config file, or a log.
  The user knowingly keeps using a free Anthropic test key; do not remind them to rotate it.

## Invariants (do not break)
1. **Build gate**: a phase is done only on BUILD SUCCESS; a run never ends without a final `mvn clean package`
   attempt and a report that lists unresolved errors.
2. Company specifics live in config / knowledge YAML, not in Python code.
3. The knowledge base is **file-based** (no database allowed).
4. Plugin seams stay stable: `LLMRequest` / `LLMResponse`, `FixerLike`, `sbmig.api.migrate(...)`.
5. `knowledge/recipes/catalog.yml` is the single source of recipe ids; `sbmig recipes check` and a snapshot test
   guard drift. Recipe BOM pinned to **3.37.0** (3.38.0 is inconsistent on Maven Central), which resolves rewrite-spring
   6.37.0 (recipe ids were verified against the 6.37.1 docs / jars).
6. Test and demo runs write under `out/` only, never into the repo's `kb/` (local e2e KB is `out/e2e/kb`).

## Status — 2026-10-21, rounds 5–9c complete (`main`)
- Round 1: pipeline + 6 fixtures e2e. Round 2: blocked-cluster triage, 6 failure fixtures, real-LLM validation
  (fixes `claude-sonnet-5`, recipe work `claude-haiku-4-5`, `config/llm-anthropic.yml`). Round 3: adapter seams,
  recipe catalog, fleet mode + learning ledger + deterministic fix labels. Round 4: library advisories + Handover. Round 5: `method-rename` → `ChangeMethodName` (owner FQCN resolved at
  harvest into `KBCase.symbol_owner`), jar-checked recipe catalog (`sbmig recipes check --jars`), fixture `boot2-method-rename`.
- 2026-10-20: `llm-probe` + per-run log files; `coding/` agent; LangGraph engine, FastAPI web UI; then simplified
  on the user's request: LLM = provider `company` (plain `requests.post`, settings from `.env`), LangChain tier removed. Round 6: Hystrix → Resilience4j by recipe (pom + annotations) and advisory-guided
  LLM fixes for what recipes cannot convert (Handover names the rewritten files). 917 offline tests green; asserted fixture e2e **14/14** (2026-10-18 tree) + 2 round-6 fixtures passing individually
  (`scripts/e2e.sh`, 15 fixtures then; 16 since round 7b); fleet demo 5/5 with 1 LLM call total.
- Round 7a (flow review, `docs/PLAN.md` last table): KB patch replay without an LLM, final build after an internal abort
  (invariant 1), LLM transport classification, unique attempt ids, no KB credit without a build, budgeted harvest
  calls, no `unresolved` ledger lines without a final build, web runs serialized (`queued`), stop after the final
  build finishes normally, `parse_stop_after` shared by CLI/web/api. 941 offline tests green.
- Round 7b: multi-module `merge` is the default (`layout.multi_module_policy`): an as-is `boot-jar` module is kept,
  every other module + root `src/` is folded into comp-jar, collisions first-wins (dropped copies under
  `<run dir>/merge-conflicts/`, Handover "Merge conflicts"), `merge_collision: fail` restores the hard error;
  Gradle / Ant / no-build projects are rejected at intake (`unsupported-build-tool:<tool>`, exit 3) before any
  Maven call. Fixture `multi-module-merge` (16 fixtures), e2e PASS. 991 offline tests green.
- Round 8 (2026-10-21): externalized configuration — `sbmig.analyze.properties` inventories every key the code reads
  (`@Value`, `Environment`, `System.getProperty`, XML `${…}`, web.xml context-param, JNDI, `@PropertySource`) against
  the packaged resources (`defined | defaulted | external`); restructure writes `__SET_ME__` placeholders for the
  external keys into boot-jar `application.properties` (rules: `knowledge/config/externalized.yml`, JNDI →
  `spring.datasource.*`, context-param → `server.servlet.context-parameters.*`, `-D`-only keys listed but not
  written); Handover "Properties to set before running" + supply guide; `-DskipTests` fallback when only runtime
  test failures remain (flag `tests-skipped`; note `build.skip_tests: true` is the default). Round 8b: Java 1.6/1.7
  as-is projects — recipe coverage verified in the pinned jars, advisory `jdk-removed-apis` (+ fix-loop note),
  fixture `java6-legacy`. Fixtures `legacy-external-config`, `java6-legacy` (18 fixtures), e2e PASS. 1,072 offline
  tests green.
- Round 9 (2026-10-21, follow-ups + edge cases): rewrite-run download retry + literal-version re-run + no KB credit
  on resolution-only failures (X8); LLM cool-down before "unavailable" (L5) and content-filter answers classified as
  request rejections; merge-conflict dropped copies shown to the fix LLM as read-only reference; Handover paths after
  restructure; web run-list pruning; per-service MSA property inventory + placeholders, `config-placeholder` source,
  scanner edge sweep; advisory `ibatis2`; restructure drops explicit versions the new parent manages (second
  effective-pom capture, `layout.drop_managed_versions`), registry-union merges, pom-packaging / outside / WAR /
  custom-resources module edges; fleet skips intake-rejected projects; handler `compiler-legacy-config`
  (fork/executable/bootclasspath). Fixtures `multi-module-conflict`, `legacy-ibatis` (20 fixtures), e2e PASS.
  1,200 offline tests green.
- Round 9b (2026-10-21, first company run feedback): role parents (`artifacts.boot_parent` / `comp_parent`) in the
  restructure incl. moving old role parents of an earlier migration; `sbmig llm-probe` on `.env` alone with
  `--model` / `--payload-extra` / `--no-system`; gateway flag `need_origin`; content-filter answers = request
  rejections; role models from `.env` (`LLM_RECIPE_MODEL`, `LLM_ESCALATION_MODEL`); operator progress logging (LLM
  call start / heartbeat / retry delay, per-cluster decisions with the error text, mvn heartbeat, web stream ordered
  by time) and a wall-clock hard deadline per LLM call; lombok handler covers inherited old lombok (unversioned
  processor path, parent-inherited dependency, inherited `annotationProcessorPaths` → `combine.self="override"`);
  an already restructured project moves to the configured parent versions. Fixture `legacy-role-parents` (21).
- Round 9c (2026-10-21): the company layout proper — root = base-parent aggregator (mandatory deps there),
  `{name}-boot` / `{name}-back` / `{name}-front` directly under the root, JSP → boot WAR + front jar (Servlet 3),
  Spring Boot as-is / MSA kept in place (`boot_pom`: poms only, `*-boot` naming + pom check), single-module Boot
  wrapped; a failed restructure step is no longer reported as success. Pre-9c layout pinned for the 21 older
  fixtures; fixtures `company-boot-single`, `company-spring5-web`, `company-servlet-jsp` (24). 1,295 offline tests green.
- Same-day follow-ups from the first company runs (Windows, 2026-10-21): gateway flag `need_origin`; JSP/HTML excluded
  from the rewrite parse; `jstl-to-jakarta`; `jdk-generified-apis`; `resources-binary-filtering` (webfonts & co under a
  filtered resource → `nonFilteredFileExtensions`, pre-emptive from the analysis — E12); git / Maven subprocess output
  decoded as UTF-8 (the cp949 console code page killed `changes.patch` on Windows); Korean handbook + PPT + xlsx in
  `docs/handbook/` (2026-10-21 evening; review found the Maven preflight tuple bug and relative Maven paths, fixed);
  web shutdown no longer hangs (SSE streams end on `RunManager.shutdown()`, live Maven children killed via the registry in
  `sbmig/build/mvn.py` + `atexit`, `sbmig serve --graceful-timeout`); mandatory artifacts take `type: pom` / `scope: import` (pom-only in-house artifacts; BOM style goes to
  dependencyManagement; an as-is declaration without `<type>` is aligned); pom-declared repositories the host
  cannot reach → `build.settings_file` catch-all mirror (`config/settings.nexus.example.xml`, RUNBOOK §0.1), plan note /
  follow-up without one, opt-in `build.pom_repositories: remove` (`strip-pom-repositories`); operator diagnostics (LLM / mvn heartbeats,
  fix-loop watchdog + milestones, `/api/threads`, auto thread dump, non-interactive git with timeout, `taskkill /T`);
  bounded `<need_grep>`; `plan.parent_phase: spring` (company: parents right after `spring.rewrite`, set in
  `artifacts.example.yml`); the hang "stuck in fixer.propose" was `nearest_pom` spinning on an absolute Windows
  path (fixed `8c41670`) — the user still has to confirm a fresh run passes `spring it2`. 2026-10-22: the final
  `clean package` of a company run failed on `org.hibernate.annotations.common` with no fix attempt (`parent_phase:
  spring` leaves no package-goal loop) → `loop.phases.verify` last-chance loop + repeated final build,
  `loop.verify_extra_llm_attempts` releases `repeated_failure` clusters (L12; released inside the loop, so a cluster the
  reactor order hid in the first verify build is covered too); advisory + note `hibernate-commons-annotations` (HCANN gone
  in Hibernate 7); JUnit 4 leftovers (`org.junit.experimental…`) → `add-known-dependency` re-adds `junit:junit` (test);
  `report.language: ko` renders `report.md` in Korean (`sbmig/report/i18n.py`; identifiers, paths, Maven messages and loop
  notes stay English; `report.json` unchanged) — the company example sets it.
- Per-round tables with what was verified: `docs/PLAN.md`.

## Known follow-ups (candidates for the next round; the user decides)
- Label heuristics are deliberately simple (whitespace-only edits → `generic-rewrite`; `override-signature` and
  `method-signature-adapt` never compile). A `method-rename` compiles only when the owner FQCN is resolvable from
  the changed file's imports; a project class overriding a renamed library method stays `override-signature`.
- `ADVISOR_CATALOG` is small by design (verified ids only); extend it with the company recipe inventory.
- Build-fix clusters are keyed by file, so one renamed library method used in N files costs N LLM calls in the
  first project (then 0 via the compiled `ChangeMethodName` candidate); cross-file clustering by symbol would cut that.
- ribbon / zuul / netflix-feign stay `guide` (Spring Cloud LoadBalancer / Gateway / OpenFeign need structural
  rewrites); hystrix is `recipe` since round 6, with the LLM (advisory-guided) handling programmatic subclasses.
  Advisory hints only exist for advisories with a `## fix-loop: <library>` note section (today: `spring-cloud.md` for
  hystrix, `ibatis2.md`, `jdk21.md` for jdk-removed-apis / jdk-generified-apis, `externalized-config.md`).
- Items to validate in the company environment: `docs/RUNBOOK.md` §0, `docs/ARCHITECTURE.md` §7.5.
- `build.skip_tests: true` is the default: builds never run unit tests, so missing externalized values cannot
  break the build and the `tests-skipped` fallback (round 8) only acts where a company config sets
  `skip_tests: false`. Decide whether the fleet should run tests (slower, catches more before runtime).
- Multi-module `merge`: a same-FQCN class differing across modules is first-wins; since round 9 the dropped copy
  reaches the fix LLM as read-only reference (`multi-module-conflict`). Still open: `pluginManagement` of a merged
  pom-packaging module is not folded into the root; a union `spring.factories` is rewritten normalized (comments
  dropped, originals saved); a kept boot module whose @SpringBootApplication scans only its own package will not
  see the merged beans (runtime, reported as a note).
- Rewrite-run download failures (HTTP 429 through the cloud proxy, 2026-10-20) are retried since round 9 (X8) and a
  literal `4.0.x` left by the recipe triggers one revert + re-run; the orchestrator re-runs a step at most once
  whatever `openrewrite.download_retry` says. L5: one cool-down per run (`llm.cooldown_seconds`), then unavailable;
  a blocked item's attempt count still includes the two cooled-down attempts.
- Role parents (9b): a kept boot module on an external parent gets no root properties (relies on `boot_parent` for
  e.g. `project.build.sourceEncoding`); under `wrap` the wrapped modules stay on the root (only the generated
  boot-jar takes `boot_parent`); merged pom modules' management goes to the comp pom only; when both role parents
  share one key the module location decides. Properties: relaxed-binding spellings of one key (`my-key` / `myKey` / `MY_KEY`) stay separate findings (three
  placeholders); `ibatis2` is detected from the pom + imports only (a `SqlMapClientFactoryBean` named only in
  Spring XML is missed).

## How to work here
- **Working mode the user asked for**: Claude plans and acts as PM in the main session. Write shared contracts
  first (`sbmig/core/{models,config,interfaces}.py`, docs), then fan implementation out to parallel `model: opus`
  subagents on **disjoint directories** with explicit briefs. Subagents never commit. Integration, test runs and
  commits happen in the main session. Per round: append a "Status — round N" table to `docs/PLAN.md`, update
  `docs/INTERFACES.md` on contract changes, add a fixture + `expected.json` for every new scenario.
- Setup: `uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` (plain `pip install -e ".[dev]"` works too).
- Fast check (Python only, ~3 min): `python -m pytest -m "not integration"` → 1,395 passed, 34 deselected
  (`-q` on top of the `-q` in pyproject hides the summary line).
  `ruff check sbmig tests` has ~1,000 pre-existing style findings (mostly `UP045`); lint is not a gate here,
  so fix only what you touch and never run a repo-wide `ruff --fix` inside a feature round.
- e2e needs **JDK 21** (`JAVA21_HOME`), Maven and network to Maven Central: `scripts/e2e.sh` → `out/e2e/summary.md`
  (`--only boot2,msa` for a subset); KB loop `scripts/demo-kb/run.sh`; fleet `scripts/fleet-demo.sh`.
  Integration tests: `python -m pytest -q -m integration`.
- Real-LLM runs: provider `company` is the default (`config/default.yml`): `requests.post(LLM_BASE_URL,
  headers={Authorization: Bearer LLM_API_KEY}, json={model, messages, need_origin}, timeout)` in
  `sbmig/fix/company.py`; URL / key / model / `LLM_RESPONSE_PATH` / `LLM_RECORD_DIR` come from `.env` (git-ignored,
  read by `load_config`, shell variables win) — no yml profile, no LangChain, no httpx knobs (the user asked for
  this after 503s on the gateway; keep it this simple). `sbmig llm-probe` (and `--bare`) prints the exact request,
  proxies and response. `config/llm-anthropic.yml` remains for Anthropic validation runs. Keys never go into yml.
- Logs: every `sbmig run` writes `<run dir>/sbmig.log` (DEBUG), fleets `fleet/sbmig.log`, any command takes
  `--log-file`; LLM request/response records go to `llm.record_dir`. The user shares these files to report problems.
- `coding/` is a standalone coding agent over the same gateway contract (`coding/README.md`), not part of `sbmig`.
- **Company convention (2026-10-20): agents run on LangGraph under uvicorn** (LangChain was dropped for the LLM
  path, see above). sbmig complies without changing behaviour: `loop.engine: graph` (default) drives the phases from a LangGraph `StateGraph`
  (`sbmig/graph/`, nodes intake → analyze → plan → phase×N → finalize; `inline` is the old loop, reports are
  identical by test); the web UI is FastAPI (`uvicorn sbmig.web.app:app` or
  `sbmig serve`), runs migrations in-process through `stream_graph` (one worker thread per run, per-run
  `sbmig.log` by thread filter) and streams `node` / `journal` / `log` / `report` events over SSE to
  `sbmig/web/static/index.html` (vanilla JS). Stop takes effect between graph nodes only (a stop before the verify
  phase aborts without the final build and says so; after it the run finishes normally). One migration at a time:
  further uploads are `queued` (shared file KB).
- Cloud sessions may lack JDK / Maven (the 2026-10-18 cloud image had JDK 21 at `/usr/lib/jvm/java-21-openjdk-amd64`
  and Maven at `/opt/maven`); without JDK 8/11 run `fixtures/libs/install.sh` with `JAVA8_HOME=$JAVA21_HOME` (JDK 21
  compiles the 1.8-target lib with a warning) and skip the `-m integration` as-is builds. Commit and push to a `claude/...` branch; the user merges to `main` locally.
