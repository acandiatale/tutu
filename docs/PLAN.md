# Delivery plan (PM view)

## Scope
Build `sbmig`, an in-house CLI/library that migrates Maven projects to JDK 21 + Spring Boot 4.1.0 and the company
`module/{boot-jar,comp-jar}` layout, with: deterministic analysis & planning, OpenRewrite execution, a
triage-driven build-fix loop that calls an LLM only for residual errors, and a file-based knowledge base that
turns resolved cases into reusable recipes. Success = `mvn clean package` passes. Runtime correctness is out of scope.

Improvements over the as-is agent:
1. Recipe selection is rule-based from a structured profile (LLM optional as reviewer) → reproducible.
2. The fixer cannot "finish" without a build: the orchestrator runs `mvn` after every change and gates phases.
3. Triage separates resolution/plugin/compile/test-compile errors, batches fixes, detects stalls and regressions
   (git revert per attempt).
4. Every resolved case is persisted (file KB); mechanical ones become OpenRewrite recipes run on the next project.

## Workstreams (parallel, disjoint directories)

| WS | Owner dirs | Deliverable |
|---|---|---|
| A analyze+plan | `sbmig/analyze`, `sbmig/plan`, `tests/analyze`, `tests/plan` | pom graph parser, source scanner, classifier, library detection, rule planner |
| B restructure | `sbmig/restructure`, `tests/restructure` | lxml `PomEditor`, layout transform, Application templates, MSA pom-only mode |
| C rewrite+build | `sbmig/rewrite`, `sbmig/build`, `tests/rewrite`, `tests/build` | OpenRewrite runner (rewrite.yml composition), Maven runner, log parser, signatures |
| D fix+notes | `sbmig/fix`, `knowledge/notes`, `tests/fix` | LLM clients (openai-compatible/anthropic/fake), context packer, protocol parser, patch applier |
| E triage+loop+report+cli | `sbmig/triage`, `sbmig/loop`, `sbmig/report`, `sbmig/cli.py`, `tests/triage`, `tests/loop` | clustering, handlers, decision engine, orchestrator, report, CLI |
| F kb+fixtures | `sbmig/kb`, `fixtures/projects`, `scripts`, `tests/kb` | file KB, matcher, harvester, promotion, 6 fixture projects, e2e script |
| G integration | everything | wire, run unit + e2e on fixtures with real mvn/OpenRewrite + fake LLM, fix seams |

Contracts all WS code against: `sbmig/core/{models,config,interfaces,workspace}.py` (owned by PM).

## Acceptance criteria
* `pytest -m "not integration"` green; each WS ≥ 80% branch coverage on its own package.
* `sbmig analyze fixtures/projects/<each>` classifies all 6 fixtures correctly.
* `sbmig run fixtures/projects/boot2 --config config/artifacts.local-e2e.yml` reaches `BUILD SUCCESS` on the
  restructured project with the fake LLM (handlers + recipes only) or reports exactly which clusters escalated.
* A run never ends without a final `mvn clean package` attempt and a report listing unresolved errors.
* KB: after one run with ≥1 LLM fix, `kb/cases/` has the case and `sbmig kb list` shows it; a second run on a
  project with the same error signature consults it before calling the LLM.

## Risks / open items for the company environment
* Exact in-house parent/mandatory/optional coordinates and whether the parent manages Boot/Java versions.
* Nexus must proxy `org.openrewrite.*` artifacts (plugin 6.46.1, recipe BOM 3.37.0) and Boot 4.1.0.
* Projects that need JDK 8 to *build as-is* are never built as-is; we only build after `UpgradeToJava21`.
* Non-Maven (Ant/Gradle) projects are detected and rejected with a clear message.
* web.xml / JSP / XML bean wiring are compile-neutral; they are reported as manual follow-ups for runtime owners.

## Status — 2026-10-18 (integration complete)

| Check | Result |
|---|---|
| Offline unit tests | 418 passed (`python -m pytest -m "not integration"`) |
| Integration tests (real mvn + OpenRewrite) | 11 passed |
| Fixture e2e (`scripts/e2e.sh`, public artifacts, fake LLM) | **6/6 BUILD SUCCESS**, 0 LLM calls, 0 unresolved: pure-java, servlet-tomcat, spring5-xml, boot2, boot3, msa |
| KB learning loop (`scripts/demo-kb/run.sh`) | run 1: 1 LLM fix → case + candidate recipe harvested; run 2: candidate runs inside the plan, **0 LLM calls, no build error**, recipe credited and auto-promoted to active |

What the e2e exercised for real: pre-emptive handlers (lombok / compiler plugin floors), `UpgradeToJava21`,
`UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1` + 4.1 version pin + Boot4JacksonTwins, jakarta migration for
servlet/legacy projects, reactive `add-known-dependency` handler (hamcrest after JUnit 4→5), the
`module/{comp-jar,boot-jar}` restructure with moved `@SpringBootApplication` / `@SpringBootTest` classes and
generated `Application` classes (`@ServletComponentScan`, `@ImportResource`), the MSA pom-only mode across two
services with different Boot generations, and the final `mvn clean package` gate.

Known limits to validate in the company environment (see RUNBOOK §0 and ARCHITECTURE §7.5):
* real parent / mandatory / optional coordinates and whether the parent manages Boot & Java versions;
* Nexus must proxy `org.openrewrite.*` (plugin 6.46.1, recipe BOM 3.37.0 — 3.38.0 is not consistent on Central yet);
* the LLM endpoint (OpenAI-compatible default) and its budget (`loop.max_llm_calls`);
* `web.xml` / JSP / XML bean wiring stay runtime follow-ups by design.

## Status — 2026-10-18, round 2 (edge / failure cases + real-LLM validation)

| Check | Result |
|---|---|
| Offline unit tests | 624+ passed |
| Asserted fixture e2e (`scripts/e2e.sh`, 12 fixtures incl. 6 failure scenarios, fake LLM) | **12/12 expectations met** (`boot2-javax-lib` → blocked `external_javax_api` attributed to `com.company.legacy:legacy-web-lib:1.0.0` / `LegacyRequestHelper`, exit 2, 0 LLM calls; `boot2-javax-lib-optional` → optional bump 1.0.0→2.0.0 fixes it; `euckr-legacy` → `source-encoding-set:cp949`; `enforcer-java8-werror` → `enforcer-relaxed`, `werror-removed`; `missing-artifact` → blocked `artifact_not_found` in 10 s; `msa-shared-lib` → shared library root migrated first, reactor-aware rewrite/build) |
| Real Anthropic API, roles | fixes `claude-sonnet-5`, recipe work `claude-haiku-4-5` (`config/llm-anthropic.yml`, key via `LLM_API_KEY` only) |
| Real-LLM KB loop (`scripts/demo-kb/run.sh … config/llm-anthropic.yml`) | run 1: Sonnet patched `@JsonComponent`→`@JacksonComponent` (first answer had an ambiguous SEARCH; the applier's feedback round got a corrected patch), Haiku produced the `ChangeType` recipe candidate; run 2: candidate ran inside the plan, **0 fix calls**, promoted to active |
| Real-LLM `boot2-javax-lib` | blocked with full attribution, **Sonnet never called** for the javax cluster (policy 0); 1 Haiku advisor call |
| Real-LLM `msa-shared-lib` | BUILD SUCCESS, 1 Haiku advisor call |
| Haiku plan advisor | additive-only, catalog + composite-coverage + profile gates; its concerns land in `plan.notes`/report. Observed: useful concerns, occasional unwarranted suggestions (now filtered deterministically) |

Known follow-ups: `ADVISOR_CATALOG` is intentionally small (verified ids only); extend it per company recipe inventory.

## Status — 2026-10-18, round 3 (adapter seams, recipe catalog, sequential fleet learning)

User asks: (1) an **adapter pattern** at the LLM call site so the in-house API gateway / agent framework can be plugged
in directly; (2) how the recipe lists are managed; (3) one agent migrating ~500 projects **sequentially**, recording
"problem → fix → success" per project (labeled by error-case symbol) so the next project benefits and new recipes appear.

| Deliverable | Where | Status |
|---|---|---|
| Tier-1 LLM adapter (`complete(LLMRequest) -> LLMResponse`, role/metadata, entry points, recording kept) | `sbmig/llm/`, `llm.provider: plugin` + `llm.plugin`, `--llm-plugin`, `docs/ADAPTERS.md`, `examples/adapters/company_gateway_adapter.py` | done; CLI smoke: scratch adapter received `role=plan_advisor` + metadata + env key, answer landed in the plan |
| Tier-2 fixer adapter (in-house coding agent replaces `LLMFixer`; `apply` optional) | `fix.plugin`, `--fix-plugin`, `examples/adapters/agent_fixer.py`, `sbmig.api.migrate(..., llm=, fixer=)` | done (unit + API tests) |
| Recipe catalog = single managed list | `knowledge/recipes/catalog.yml`, `sbmig/plan/catalog.py`, `sbmig recipes list|show|check|sets` (check wired into `scripts/e2e.sh`), `docs/RECIPES.md` | done; `ADVISOR_*` derived from the catalog, snapshot test guards drift |
| Fix labels + deterministic recipe compilation (no LLM for 1:1 renames / dependency / property changes) | `sbmig/kb/labels.py` | done |
| Cross-project ledger + symbol index + hints | `kb/ledger.jsonl`, `kb/symbols.json`, `sbmig kb ledger|symbols|learning` | done |
| Fleet mode (sequential, resumable, shared KB, fleet-wide LLM budget, summaries + learning report) | `sbmig/fleet/`, `sbmig fleet run|status|resume`, `scripts/fleet-demo.sh`, `docs/FLEET.md` | done |
| Offline tests | 733 passed (21 integration deselected) | green |
| Asserted fixture e2e on the merged tree (`scripts/e2e.sh`, now preceded by `sbmig recipes check`) | 12/12 expectations met; local e2e KB moved to `out/e2e/kb` so test runs never write into the repo's `kb/` | green |
| CLI smoke of both adapter tiers | tier 1: scratch adapter received `role=plan_advisor`, metadata and the env key; tier 2: scratch agent fixer (no `apply`) fixed boot3's `@JsonComponent` → BUILD SUCCESS, KB compiled the `ChangeType` candidate from the labels | verified |
| Fleet demo (pure-java, boot2, boot3, spring5-xml, boot3-again; replayed LLM) | 5/5 success, **1 LLM call total**: boot3's LLM fix labeled `annotation-replace, type-rename` → `ChangeType` candidate compiled from labels → boot3-again fixed by the plan recipe, 0 LLM calls, auto-promoted | verified |

Notes: a subagent deleted `out/llm-records` (gitignored recordings of the round-2 real-LLM runs); run journals and
reports under `out/llm*/` are intact. Known limits: label heuristics are deliberately simple (whitespace-only edits →
`generic-rewrite`), `method-rename` is not compiled to `ChangeMethodName` yet, the catalog's `rewrite-java-dependencies`
`provided_by` link is an assumption backed by the e2e runs (the base recipes using it resolve fine), not by a jar check.

## Status — 2026-10-18, round 4 (library advisories + handover)

User asks: libraries such as springfox, hystrix or sleuth are EOL or no longer version-managed (or declared without a
version) yet still compile — does the agent just build and leave guidance ("this has issues, use springdoc")? Decision:
**no `block` policy at all** (500 projects cannot be perfect); build as far as possible, then hand the owning team a
**compressed summary of the versions we replaced** and "from here on, your team fixes X → Y".

| Deliverable | Where | Status |
|---|---|---|
| Advisory catalog (11 entries: springfox, sleuth, hystrix, ribbon, zuul, netflix-feign, spring-cloud-bom, spring-security-oauth2-legacy, log4j1, commons-lang2, httpclient4), policies `guide` / `recipe` only, company extras + overrides | `knowledge/libraries/advisories.yml`, `sbmig/advisory/`, `advisories.*` config, `sbmig advisories list|show|check` (wired into `scripts/e2e.sh`), `docs/ADVISORIES.md` | done |
| Analyzer rules for the flagged libraries (`sleuth`, `hystrix`, `ribbon`, `zuul`, `netflix-feign`, `spring-cloud-bom` via managed BOM / parent, `swagger-v1`, `spring-security-oauth2-legacy`, `log4j1`, `commons-lang2`, `httpclient4`) | `sbmig/analyze/libraries.py` (`managed` / `parents` patterns) | done |
| Planner integration: `recipe` findings add verified OpenRewrite recipes to `spring.rewrite` unless the Boot 4 umbrella already covers them (`SpringFoxToSpringDoc`, `UpgradeSpringDoc_3_0`, `MigrateCloudSleuthToMicrometerTracing`, `UpgradeSpringCloud_2025_1` verified in rewrite-spring 6.37.1) | `sbmig/plan/planner.py`, catalog `covers`, `knowledge/recipes/catalog.yml` (+4 entries) | done |
| Verification on the final tree (`replaced` / `still-present` / `unknown`) + report **Handover** as the first section: versions replaced (parent, java, boot, dependency/BOM rows, ⚠ carried-over pins), libraries needing attention, "From here on, your team" | `sbmig/report/handover.py`, `sbmig/report/markdown.py`, `Orchestrator._finalize_advisories` | done |
| Generic safety net for un-catalogued libraries whose version the old parent managed and the new one does not (`carry-over-managed-version` pins the as-is version → handover ⚠ row + follow-up) | handover `carried_over` (fixture: `net.sf.ehcache:ehcache` 2.10.9.2) | done |
| Ledger + fleet aggregation (`resolution=advisory`, `learning.md` "Libraries needing owner action", `summary.csv` columns `advisories` / `carried_over`) | `sbmig/kb/ledger.py`, `sbmig/kb/store.py`, `sbmig/fleet/` | done |
| Offline tests | 801 passed (22 integration deselected) | green |
| Reference fixture `boot2-cloud-legacy` (Boot 2.7.18 + Spring Cloud 2021.0.9 BOM, sleuth without version, hystrix 2.2.10 explicit, springfox 3.0.0, ehcache 2 managed by Boot 2.7 only) | `fixtures/projects/boot2-cloud-legacy/` | BUILD SUCCESS, exit 0, **0 LLM calls**, 59 s: springfox → `springdoc-openapi-starter-webmvc-ui:3.0.3`, sleuth → `micrometer-tracing-bridge-brave` (Boot-managed), Spring Cloud 2021.0.9 → 2025.1.3, hystrix still present (guide → Resilience4j), ehcache pinned 2.10.9.2 ⚠ |
| Asserted fixture e2e on the merged tree (`scripts/e2e.sh`: `recipes check` + `advisories check` + 13 fixtures) | `out/e2e/summary.md` | **13/13 expectations met**, exit 0 (12 previous fixtures unchanged: no advisories, 0 carried-over; `boot2-cloud-legacy` as above); ledger has 4 `advisory` lines, `sbmig kb learning` shows the "Libraries needing owner action" table |

## Status — 2026-10-18, round 5 (`method-rename` → `ChangeMethodName`, jar-checked recipe catalog)

User decision: take the first two "Known follow-ups" of round 4 together — (1) compile the `method-rename` fix label
into `org.openrewrite.java.ChangeMethodName` so a renamed library method is fixed by the LLM **once** per fleet and by
a recipe afterwards, (2) turn the catalog's `provided_by` link from an e2e-backed assumption into a real jar check
(`sbmig recipes check --jars`). Finding that shaped (1): javac 21 prints imported types by their *simple* name
(`location: class LegacyRequestHelper`), so the owner FQCN the method pattern needs is resolved at harvest time from
the changed files' imports (`KBCase.symbol_owner`); argument types never go into the pattern (`(..)`).

| Deliverable | Where | Status |
|---|---|---|
| `method-rename` → `ChangeMethodName` compiler: all call sites agree on old/new, owner must be a library-owned FQCN (javac location when qualified, else `KBCase.symbol_owner`), pattern `<owner> <old>(..)`, `matchOverrides: true`, name `com.company.kb.<Owner><Old>To<New>`; method references `x::old` are `method-rename` too | `sbmig/kb/labels.py` (`_compile_method_rename`) | done |
| Owner resolution at harvest: `resolve_symbol_owner(changes, errors, ws, cfg)` — explicit import → static import → wildcard import via `locate_class` → same package; type symbols (`class X`) are skipped because their `location:` is the referencing class. `sanitize_case(..., cfg=)` fills `KBCase.symbol_owner` | `sbmig/kb/harvest.py`, `sbmig/core/models.py` | done |
| Jar-checked catalog: `sbmig recipes check --jars` resolves each configured recipe artifact (BOM 3.37.0) to its transitive classpath with Maven, scans `META-INF/rewrite/*.yml` + `.class` entries, verifies `artifact` / `provided_by` per entry; Maven failures are reported, never raised; wired into `scripts/e2e.sh` | `sbmig/plan/jarcheck.py`, `sbmig/plan/catalog_cli.py`, `docs/RECIPES.md` | done — real run: **21/21** openrewrite entries found in their declared jars (rewrite-migrate-java 3.42.0, rewrite-spring 6.37.0, rewrite-testing-frameworks 3.44.0, core rewrite-java/maven 8.89.0); `rewrite-java-dependencies` 1.60.2 **is** on rewrite-migrate-java's transitive classpath (round-4 assumption confirmed, no catalog entry changed) |
| Simulated in-house library `legacy-web-lib` **3.0.0** (jakarta, `clientIp` → `remoteClientIp`, no alias) + fixture `boot2-method-rename` (copy of boot2-javax-lib; `sbmig.e2e.yml`: optional bump to 3.0.0, replayed LLM, own `kb.dir: out/e2e/kb-method-rename`) | `fixtures/libs/legacy-web-lib/v3.0.0/`, `fixtures/projects/boot2-method-rename/`, `fixtures/libs/install.sh` | done |
| e2e expectation `"kb"` (`dir`, `candidates_min`, `harvest_candidates_min`, `recipes[{recipe, options, name}]`), KB dir wiped before the run (only under `out/`) | `scripts/e2e.py` (`evaluate_kb`), `tests/fixtures/` | done |
| Offline tests | 844 passed (24 integration deselected) | green |
| Asserted fixture e2e on the merged tree (`scripts/e2e.sh`: `recipes check --jars` + `advisories check` + 14 fixtures; cloud image, JDK 21 + Maven, `fixtures/libs/install.sh` with `JAVA8_HOME=$JAVA21_HOME`) | `out/e2e/summary.md` | **14/14 expectations met**, exit 0; 13 previous fixtures unchanged (0 LLM calls); `boot2-method-rename`: BUILD SUCCESS, exit 0, **2 LLM calls** (one per file-keyed cluster), optional bump 1.0.0→3.0.0, candidate `com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp` compiled from the labels with no LLM |
| Learning loop proof (manual, persistent KB `out/learn-mr/kb`, fresh copy per run) | `sbmig run fixtures/projects/boot2-method-rename` ×3 | run 1: 2 LLM calls → candidate; run 3: **0 LLM calls**, the candidate renamed both call sites in the spring rewrite (`plan_recipe build_ok`, credit ok=1). Run 2 failed for an unrelated reason: the rewrite plugin's own resolver could not download Boot POMs through the proxy (`Failed to download …:4.0.5 … Attempted URIs`), `UpgradeSpringBoot_4_0` then left literal `<version>4.0.x</version>` starters → 3 × `artifact_not_found` blocked, and the candidate was credited `fail=1` for a failure that was not its own (follow-up below) |

## Status — 2026-10-20 (in-house gateway provider + log files)

User: the company LLM API is a plain `POST(url, headers, json, timeout)` with `Authorization: Bearer` and
`{"model": "gpt-6-luna", "need_orgin": true, "messages": [...]}`; sbmig must call it, and logs must land in files that
are easy to share. Nothing in the adapter seams changed.

| Deliverable | Where | Status |
|---|---|---|
| Builtin provider `chat_post`: `POST llm.base_url` (full URL) with `{model, messages, **llm.payload_extra}`, generation params only when `llm.payload_generation_params`, reply text via `llm.response_path` or auto-detected (`choices[0].message.content`, `content`, `response`, `data.content`, …), usage from `usage` when present; HTTP errors logged with the body excerpt (never the key) | `sbmig/fix/client.py` (`ChatPostClient`, `extract_reply`), `sbmig/core/config.py`, `config/llm-company.yml` | done — smoke: local server with a non-OpenAI shape (`data.result.content`), `sbmig run boot3` → 1 LLM call through `chat_post`, BUILD SUCCESS, exit 0 |
| `sbmig llm-probe --config …`: prints the request payload, raw response JSON, extracted reply (or the auto-detect paths tried) | `sbmig/cli.py` | done |
| Log files: every `sbmig run` → `<run dir>/sbmig.log` (DEBUG, printed at the end as `log: …`), `sbmig fleet` → `fleet/sbmig.log`, any command `--log-file PATH`; `llm.record_dir` keeps `llm-NNNN.json` per call | `sbmig/cli.py` (`attach_file_log`), `sbmig/fleet/cli.py`, `docs/RUNBOOK.md` | done |
| Offline tests | 852 passed (24 integration deselected) | green |
| `coding/`: standalone coding agent over the same gateway contract (text protocol: READ/LIST/GREP, SEARCH/REPLACE edits, RUN, `<done>`), diffs + confirmation, backups, jsonl transcript, `--probe` | `coding/agent.py`, `coding/README.md`, 14 offline tests | done (not part of the `sbmig` package) |
| Web UI `sbmig serve`: one page (vanilla JS) — upload zip, tick configs, LLM/stop-after, live timeline from `journal.jsonl`, log pane (`sbmig.log` + console, DEBUG toggle), report + file links, run list / stop; stdlib `ThreadingHTTPServer` + SSE, each run a `sbmig run` subprocess | `sbmig/web/server.py`, `sbmig/web/static/index.html`, `tests/web/test_server.py` (4 tests with a fake run subprocess) | done |
| Web UI smoke on the cloud image | `out/web/` | plumbing verified end to end (upload → subprocess → 68 journal + log events over SSE → report + `result.zip` links); the two boot3 runs ended NOT BUILDING because the cloud proxy rate-limited Maven Central (HTTP 429 → rewrite resolver `Failed to download` × 134 → literal `4.0.x` starters), reproduced identically with a direct CLI run at the same time — environment, not the UI (see CLAUDE.md follow-ups) |
