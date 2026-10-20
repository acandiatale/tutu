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
| Web UI `sbmig serve`: one page (vanilla JS) — upload zip, tick configs, LLM/stop-after, live timeline from `journal.jsonl`, log pane (`sbmig.log` + console, DEBUG toggle), report + file links, run list / stop; stdlib `ThreadingHTTPServer` + SSE, each run a `sbmig run` subprocess | `sbmig/web/server.py`, `sbmig/web/static/index.html`, `tests/web/test_app.py` (FastAPI; superseded the first stdlib version the same day) | done |
| Web UI smoke on the cloud image | `out/web/` | plumbing verified end to end (upload → subprocess → 68 journal + log events over SSE → report + `result.zip` links); the two boot3 runs ended NOT BUILDING because the cloud proxy rate-limited Maven Central (HTTP 429 → rewrite resolver `Failed to download` × 134 → literal `4.0.x` starters), reproduced identically with a direct CLI run at the same time — environment, not the UI (see CLAUDE.md follow-ups) |

## Status — 2026-10-20, company convention (LangGraph + LangChain, uvicorn)

User: the company only deploys agents built on LangGraph/LangChain and serves them with uvicorn; the web page must be
served from a `static/` directory. Decision: keep the deterministic core and the build gate untouched; make LangGraph
the driver of the phases, make the LLM a LangChain `BaseChatModel`, and serve the page from FastAPI.

| Deliverable | Where | Status |
|---|---|---|
| Graph engine: `Orchestrator._run` split into `step_intake/analyze/plan/phase/finish`; LangGraph `StateGraph` nodes `intake → analyze → plan → phase×N (self-loop per plan phase group) → finalize`; `loop.engine: graph` default, `inline` kept; `graph_node` journal events; `run_graph` / `stream_graph` | `sbmig/graph/engine.py`, `sbmig/loop/orchestrator.py`, `docs/ARCHITECTURE.md` §7.7 | done — 15 tests prove both engines give identical reports / flags / exit codes / journal sequences (boot, MSA, failure, stop_after ×4, mvn-not-found, build-loop abort, internal error); real pure-java run through the graph: BUILD SUCCESS, exit 0 |
| LangChain tier: `ChatPostModel(BaseChatModel)` (gateway wire format, usage_metadata, key never printed), `LangChainClient` (any BaseChatModel → `LLMClientLike`, role → tags, metadata → config), `resolve_langchain_model` (`llm.langchain_model` factory / class / instance, else ChatPostModel), provider `langchain`; `RecordingClient`, `LLMRequest/Response`, `FixerLike` unchanged | `sbmig/llm/langchain.py`, `sbmig/fix/client.py`, `config/llm-company.yml` (`provider: langchain`), `docs/ADAPTERS.md` | done — 15 tests; live probe through LangChain against a local fake gateway: `pong`, request body exactly `{model, messages, need_orgin}` |
| Web app: FastAPI `create_app` / `uvicorn sbmig.web.app:app`, `static/` mounted, runs **in-process** via `stream_graph` in a worker thread (per-run `sbmig.log` by thread filter, replayable event list, stop between nodes), SSE `status | node | error | journal | log | report | ping | end`; page gains a LangGraph node strip | `sbmig/web/app.py`, `sbmig/web/runs.py`, `sbmig/web/static/index.html`, `sbmig/cli.py` (`serve` → uvicorn, `attach_file_log(thread_id=)`) | done — 10 tests; real smoke: pure-java zip → 8 `node` + 65 `journal` + 19 `log` events → BUILD SUCCESS, exit 0, report.md / result.zip downloads; stop after `intake` → exit 3 |
| Offline tests | 893 passed (24 integration deselected) | green |
| Follow-ups | — | concurrent web runs share the file KB (`kb/`): keep one run at a time (RUNBOOK); stop cannot interrupt a running Maven/LLM call; `llm-probe` prints raw JSON on the LangChain path only for `ChatPostModel` |

## Status — 2026-10-20, simplification (user: "LangChain 벗기고 requests로, 키는 .env")

On the company network the gateway answered 503 to sbmig while the in-house agents and Postman worked with the same
URL and key; the user asked to remove the LangChain/httpx layers and configure everything from `.env`.

| Deliverable | Where | Status |
|---|---|---|
| Provider `company` (default): `requests.post(url, headers={Authorization}, json={model, messages, **payload_extra}, timeout)`; retries on transport errors / 5xx / 429, clear errors for a missing `LLM_BASE_URL` / `LLM_API_KEY`; reply via `LLM_RESPONSE_PATH` or auto-detect | `sbmig/fix/company.py`, `config/default.yml`, `.env.example` | done — 5 tests (exact call shape, no generation params, response path, retries/401, env wiring) |
| Removed: LangChain tier, `chat_post`, `http_backend` / `verify_ssl` knobs, `config/llm-company.yml`, the `llm:` block in `artifacts.example.yml` | — | done — the LangGraph engine and the FastAPI/uvicorn app are unchanged |
| `sbmig llm-probe` prints the exact request (masked token), proxies in effect, response headers/body; `--bare` = plain `requests.post` for comparison | `sbmig/cli.py` | done |
| Offline tests | see commit | green |

## Status — 2026-10-20, round 6 (Hystrix → Resilience4j, advisory-guided LLM fixes)

User: Hystrix/Sleuth must be migrated to Spring Cloud and reported, annotations included; what a recipe cannot convert
should be rewritten by the LLM and reported ("이렇게 바꿨다"). Decision: deterministic recipe first, build gate, then
the LLM with the advisory as guidance, and the Handover names every LLM-rewritten file.

| Deliverable | Where | Status |
|---|---|---|
| `com.company.migrate.HystrixToResilience4j`: `@HystrixCommand` → `@CircuitBreaker` (commandKey → name, else `"default"`; Hystrix-only attributes + nested `@HystrixProperty` removed), `@EnableHystrix` / `@EnableCircuitBreaker` / `@EnableHystrixDashboard` removed, starters swapped to `spring-cloud-starter-circuitbreaker-resilience4j` (BOM-managed, no network lookups), `hystrix-javanica` / `hystrix-core` / dashboard removed; advisory `hystrix` policy `recipe` with a runtime-verification followup | `knowledge/recipes/base/hystrix-to-resilience4j.yml`, `knowledge/libraries/advisories.yml`, `knowledge/recipes/catalog.yml` (+8 core building blocks, `--jars` 29/29) | done — fixture `boot2-cloud-legacy`: BUILD SUCCESS, 0 LLM calls, `@CircuitBreaker(name = "default", fallbackMethod = "stockFallback")` and `name = "inventoryReserve"` (from commandKey); Handover: added/removed starters + verify line |
| Advisory-guided LLM fixes: `sbmig.advisory.hints_for(errors, findings)` matches a cluster to advisories by library packages / artifacts and renders "Library migration guidance" (title, replacement, followup, `## fix-loop: <library>` note) into the fix prompt (`ContextPacker`, system prompt rule 9); `FixAttempt.advisory_ids`; `_finalize_advisories` attributes successful LLM fixes → `AdvisoryFinding.llm_fixed_files/llm_fix_notes` → Handover line "code rewritten by the LLM for <library>: <files> — <note>"; ledger `notes: ["advisory:<id>"]`; attempts table shows the advisory | `sbmig/advisory/hints.py`, `sbmig/fix/context.py`, `sbmig/fix/prompts/system_v1.md`, `sbmig/loop/fixloop.py`, `sbmig/loop/orchestrator.py`, `sbmig/report/handover.py`, `sbmig/kb/ledger.py`, `knowledge/notes/spring-cloud.md` (`## fix-loop: hystrix`) | done — fixture `boot2-hystrix-command` (programmatic `HystrixCommand<Integer>` subclass, replayed LLM): BUILD SUCCESS, exit 0, 2 LLM calls (1 rewrite + 1 no-change on the caller cluster), hystrix replaced, Handover names `InventoryCommand.java`; the second cluster correctly got no hint |
| e2e evaluator: `g:a (advisory:x)` handover suffixes parse; fixture count 15 | `scripts/e2e.py`, `tests/fixtures/` | done |
| Offline tests | 917 passed (25 integration deselected) | green |
| Environment note | — | the cloud proxy rate-limited Maven Central (HTTP 429) for most of the day: two reruns of `boot2-cloud-legacy` blocked on unrelated Boot starters (`Failed to download` ×197) while hystrix was `replaced` in each; the passing run was re-evaluated offline with the final expected.json; `boot2-hystrix-command` passed after pre-fetching the 14 POMs the rewrite resolver had failed on |

## Status — 2026-10-20, round 7a (end-to-end flow review: "각 노드들이 알맞게 흐름을 잘 타고 있는지")

User: read the finished code, check that every node takes the right path, fix what is wrong. A read-only reviewer
traced `graph/engine.py` → `loop/orchestrator.py` → `loop/fixloop.py` → `web/runs.py` → `kb/` and reported 4 bugs,
7 robustness gaps and 6 nits; the confirmed ones are fixed below (ids as in the review).

| Finding | Fix | Where | Test |
|---|---|---|---|
| B1 KB patch replay built the LLM fixer first → `LLMConfigError` without a key, so a run without an LLM could not replay its own knowledge | `LazyFixer.apply` falls back to `PatchApplier` when the fixer cannot be built (applying a patch needs no LLM) | `sbmig/loop/components.py` | `test_kb_patch_replay_works_without_an_llm` (0 LLM calls, BUILD SUCCESS) |
| B2 an internal error / missing component after planning skipped the final `mvn clean package` (invariant 1) | `_final_build_after_abort`: the guard's except branches run the plan's verify steps (not for dry runs, `_RunAborted`, or `always_final_verify=false`); report keeps `aborted` + the real unresolved list | `sbmig/loop/orchestrator.py` | `test_internal_error_still_attempts_the_final_build` |
| B3 LLM transport classification: unparseable 200 / bad `LLM_RESPONSE_PATH` never counted (clusters ended as `repeated_failure` instead of `llm_unavailable`); 400/413 (prompt too long) counted and could switch the LLM off for the run | regex covers `invalid JSON` / `cannot find the reply text` / `response_path`; `status=400|413|422` is the request's fault and never counts | `sbmig/loop/fixloop.py` | `test_transport_classification` ×8 |
| B4 duplicate attempt ids when a round is repeated inside one iteration (harvest mislinked cases) | `_Round.n` seeded from the attempts already recorded for the iteration | `sbmig/loop/fixloop.py` | `test_reround_keeps_attempt_ids_unique` |
| R1 a phase without a build-fix loop credited KB recipes with a *failure* (`build_ok=False` means "no evidence", not "failed") | no credit when `pr.iterations == 0`; harvest still runs | `sbmig/loop/orchestrator.py` | `test_phase_without_a_loop_does_not_credit_kb_recipes` (+ positive twin) |
| R2 web Stop clicked while the final build ran marked a finished run `aborted` (exit 3) | a stop seen after the verify phase group / finalize node finishes the run normally; an earlier stop says in the report that the final build did not run | `sbmig/web/runs.py` | `test_stop_after_the_final_build_finishes_normally`, `..._aborts_and_says_no_final_build` |
| R3 two web runs at once shared `kb/` and the in-memory index (last writer wins) | `RunManager._run_lock` serializes migrations; a waiting run is `queued` (UI badge, stop allowed) | `sbmig/web/runs.py`, `static/index.html` | `test_runs_are_serialized_and_report_queued` |
| R5 recipe-candidate LLM calls in the harvest bypassed `max_llm_calls` | `_BudgetedLLM` charges the run budget; generation skipped once the budget is exhausted | `sbmig/loop/orchestrator.py` | `test_harvest_recipe_candidate_calls_are_charged_to_the_budget`, `..._skips_candidates_when_the_budget_is_exhausted` |
| R7 stopped / aborted runs wrote `unresolved` ledger lines (mid-migration errors are not resolutions) | `record_run` writes them only when a final build ran and the run was not aborted; blocked / advisory lines unchanged | `sbmig/kb/store.py` | `test_record_run_skips_unresolved_without_a_final_build`, `tests/kb/test_ledger.py` |
| R6 every SSE stream holds an anyio worker thread (default pool 40) | pool raised to 256 at startup | `sbmig/web/app.py` | — |
| R9 `attach_file_log` outside the worker's `try` → a run stuck "running" | inside the try; the run finishes with an `error` event | `sbmig/web/runs.py` | `test_crash_before_the_body_still_finishes_the_run` |
| N1 `stop_after=report` accepted (flag `stopped-after-report`) | `sbmig.loop.parse_stop_after` shared by CLI / web / `api.migrate`: intake…verify only | `sbmig/loop/orchestrator.py`, `cli.py`, `web/runs.py`, `api.py` | `test_parse_stop_after`, `test_bad_stop_after_is_rejected` |
| N4 / N5 `_finalize_blocked` could lose the report; a reused orchestrator kept the previous run's verify results | guarded; per-run state reset in `_run_guard` | `sbmig/loop/orchestrator.py` | `test_orchestrator_reuse_starts_from_a_clean_state` |
| Looked at, no change | graph edges vs inline, `_RunAborted` paths, fix-loop termination, advisory chain order, company client, exit codes; N3 (recipe-batch retry re-handles ≤ limit clusters), N6 nits (`max_iterations=0` = config default; recursion limit 1000 ≈ 495 MSA services) | — | — |
| Offline tests | 941 passed (25 integration deselected) | | green |
| e2e smoke (real Maven, graph engine, cloud image) | `scripts/e2e.sh --only pure-java,boot3` → 2/2 BUILD SUCCESS, exit 0, 0 LLM calls | `out/e2e/summary.md` | green |
| Not done (follow-ups) | LLM-unavailable is still permanent for the run after 2 consecutive transport failures (a cool-down would suit gateway hiccups better); Handover `llm_fixed_files` keep pre-restructure paths; `RunManager.runs` is never pruned (one process = one day of runs) | | user decides |

## Status — 2026-10-20, round 7b (multi-module `merge` v2 as the default, Maven-only intake)

User: "Maven 프로젝트만 진행; gradle / ant는 intake·analyze에서 reject" and, on the multi-module question, asked for a
recommendation. Decision (recommended and implemented): the company layout is exactly comp-jar + boot-jar, so `merge`
becomes the default; an as-is `boot-jar` module is kept; collisions are resolved deterministically (first module in
`<modules>` order wins, dropped copies saved and reported) — no LLM step; `wrap` stays opt-in.

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `MergeConflict`, `RestructureResult.merged_modules / kept_boot_module / merge_conflicts`, `RunReport.merge_conflicts`, `HandoverSummary.merge_conflicts`, `LayoutConfig.multi_module_policy` default `merge` + `merge_collision: first-wins \| fail`, `ProjectProfile.build_tool` + `ant` | `sbmig/core/{models,interfaces,config}.py`, `config/default.yml`, `docs/INTERFACES.md` | done |
| WS-M `merge` v2: kept boot module (basename = `layout.boot_module`, moved to `module/boot-jar`, parent relativePath recomputed, module deps → one `<root>-comp` dep, boot plugin ensured, no Application generated), root `src/` + other modules folded into comp-jar, all decisions before the first move, first-wins collisions (identical → one note; differing → `<run dir>/merge-conflicts/<module>/src/…` + follow-up), "already restructured" under merge needs comp-jar too | `sbmig/restructure/restructurer.py`, `docs/ARCHITECTURE.md` §8 | done — 63 restructure tests (kept boot at `boot-jar` / `apps/boot-jar`, idempotent second run, no-Application boot module, two `*/boot-jar`, identical + differing duplicates, root-src wins, `fail` policy untouched tree, `module/boot-jar+core+web` merged) |
| WS-M fixture `multi-module-merge` (Boot 2.7.18 / Java 11: `core` + `web` + as-is `boot-jar`, one differing `messages.properties`, one identical `META-INF/shared.txt`, commons-lang3 3.9) + e2e `post_checks` kinds `file_exists` / `file_absent` | `fixtures/projects/multi-module-merge/`, `scripts/e2e.py`, `fixtures/projects/README.md`, `tests/fixtures/` | done — e2e: exit 0, 4/4 phases, BUILD SUCCESS, 0 LLM calls, 88.6 s, `expect=PASS` (kept `ShopApplication`, `module/boot-jar/pom.xml` depends on `shop-comp`, `core`/`web` gone, `hello from core` kept, Handover "Merge conflicts" + follow-up) |
| WS-I Maven-only intake: `sbmig.analyze.detect_build_tool` (root first, then globs; pom wins over Gradle/Ant files with an "ignored" warning), `Orchestrator.step_intake` aborts before the effective-pom capture with flag `unsupported-build-tool:<tool>`, `aborted`, exit 3, reason in `manual_followups[0]`, journal `build_tool`; `Workspace._detect_project_root` descends to Gradle/Ant roots; analyze keeps its check as fallback | `sbmig/analyze/build_tool.py`, `sbmig/analyze/analyzer.py`, `sbmig/core/workspace.py`, `sbmig/loop/orchestrator.py`, `docs/ERROR-HANDLING.md` S4, `docs/RUNBOOK.md` | done — 34 tests (detection matrix, zip root, gradle/ant/none on both engines, Maven passes) |
| WS-I Handover: `report.merge_conflicts` filled by `_step_restructure` (deduped, journal fields, one summary note), `HandoverSummary.merge_conflicts` lines, "**Merge conflicts**" block before "From here on" in report.md | `sbmig/report/handover.py`, `sbmig/report/markdown.py` | done — 6 report tests + loop tests |
| Offline tests | 991 passed (26 integration deselected) | green |
| Follow-ups | — | the dropped copy of a differing same-FQCN class is saved but not shown to the fix LLM (hint candidate); a kept boot module's component scan may not cover merged packages (runtime, noted) |

## Status — 2026-10-21, round 8 (externalized configuration: inventory, placeholders, "Properties to set") + 8b (Java 1.6/1.7)

User: legacy apps ran on an external Tomcat and the values of their profile/property keys were never in the source
(catalina.properties, -D, JNDI, web.xml context-param); after the move to an executable boot-jar those supply routes
vanish. Decision: inventory every key the code reads, write `__SET_ME__` placeholders for the undefined ones so the
packaged configuration is complete, and hand the owners a "set these values" table; allow a `-DskipTests` fallback
when only runtime test failures remain. Second decision: Java 1.6 / 1.7 as-is projects are in scope (8b).

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `PropertyRef / PropertyFinding / PropertyInventory`, `ProjectProfile.properties`, `RunReport.properties`, `HandoverSummary.properties_to_set / properties_guide`, `RestructureResult.placeholder_files / placeholders_written`, `BuildRunnerLike.run(skip_tests=)`, `Config.externalized` (`sentinel`, `write_placeholders`, `placeholder_file`, `rules_file`, `include_test_refs`, `max_refs_per_key`), `build.allow_skip_tests_fallback`, rules YAML `knowledge/config/externalized.yml` (jndi / context_param / system_property / boot_defaults / guide) | `sbmig/core/{models,interfaces,config}.py`, `config/default.yml`, `knowledge/config/externalized.yml`, `docs/INTERFACES.md` | done |
| WS-P scanner `sbmig.analyze.properties.scan_properties`: refs from `@Value`, `@ConfigurationProperties`, `Environment.getProperty`, `System.getProperty/getenv`, Spring XML / web.xml / context.xml `${…}`, `<context-param>`, JNDI (literals, lookups, `jee:jndi-lookup`, Tomcat `<Resource>/<Environment>`), `@PropertySource` / placeholder-configurer locations; definitions from packaged `*.properties|yml` (profiles, YAML documents, `classpath:` locations, test resources for test-only keys), relaxed binding, Maven filtering; verdict `defined | defaulted | external`, Boot-key mapping from the rules; note `knowledge/notes/externalized-config.md` (`## fix-loop: externalized-config`) | `sbmig/analyze/properties.py`, `sbmig/analyze/analyzer.py`, `knowledge/` | done — 30 tests; analysis time unchanged (≈ 0.02–0.06 s per fixture) |
| WS-R placeholders: idempotent marker block in `module/boot-jar/src/main/resources/application.properties` (layout: single / merge incl. kept boot module / wrap / already-restructured; `msa_pom`: per service), one `key=__SET_ME__` per boot key, context-param keeps its as-is value, `# from <source> file:line` comments, follow-up + journal `placeholders`; `report.properties` merged by the orchestrator | `sbmig/restructure/restructurer.py`, `sbmig/loop/orchestrator.py` | done — 9 tests |
| WS-R tests-skipped fallback: `BuildRunner.run(skip_tests=)` → `-DskipTests`; `BuildFixLoop._maybe_skip_tests` when only `TEST_FAILURE` errors remain → flag `tests-skipped`, failing tests as follow-ups + unresolved, final verify skips tests too (L7b) | `sbmig/build/runner.py`, `sbmig/loop/fixloop.py`, `sbmig/loop/orchestrator.py`, `docs/ERROR-HANDLING.md` | done — 12 loop tests (both engines) + 1 runner test. Note: `build.skip_tests: true` is the default, so the fallback only matters where a company config runs tests |
| WS-H Handover "### Properties to set before running" (key \| before migration \| referenced at \| written as \| set via), "How to supply them" guide, placeholder file line; no block when nothing needs a value; console line; e2e `expect.properties` (`external_min`, `external`, `absent`, `written`, `placeholder_file_contains`); fixture `legacy-external-config` (Spring 5 XML WAR on external Tomcat: `@Value` defined/external/defaulted, JNDI DataSource, `System.getProperty`, web.xml context-param, `file:${catalina.base}` placeholder location) | `sbmig/report/{handover,markdown,writer}.py`, `scripts/e2e.py`, `fixtures/projects/legacy-external-config/`, `docs/RUNBOOK.md`, `README.md` | done — e2e `legacy-external-config` PASS (exit 0, 4/4, 0 LLM, 35 s): table + guide + placeholder file with `mail.host`, `spring.datasource.*`, `catalog.home` = `__SET_ME__` and `server.servlet.context-parameters.catalog.region=kr`; `spring5-xml` / `boot2` re-run PASS with no properties block |
| WS-J Java 1.6 / 1.7 (8b): recipe coverage verified from the pinned jars (`UpgradeToJava21` → `UpgradeToJava17` → `Java8toJava11` already carries `UpgradeToJava8/7`, `RenameUnderscoreIdentifier`, `UseJavaUtilBase64`, `AddJaxbDependenciesWithRuntime`, `AddJaxwsDependencies`, `AddCommonAnnotationsDependencies`, `ThreadStopDestroy` — no catalog change); `sun.misc.*` does not resolve on JDK 21 so the recipe is a no-op there → new library rule + advisory `jdk-removed-apis` (status incompatible, policy guide; imports `sun.misc`, `sun.security`, `com.sun.image`, `javax.activation`) with a `## fix-loop: jdk-removed-apis` note section feeding the LLM; `java_level_findings` wording; fixture `java6-legacy` (source 1.6, compiler 2.3.2, surefire 2.4.3, junit 4.11, commons-lang3 3.1, `sun.misc.BASE64*`, `javax.xml.bind.DatatypeConverter`, replayed LLM, own KB dir) | `sbmig/analyze/{libraries,edges}.py`, `knowledge/libraries/advisories.yml`, `knowledge/notes/jdk21.md`, `fixtures/projects/java6-legacy/`, `docs/ADVISORIES.md`, `docs/ERROR-HANDLING.md` (S8) | done — e2e `java6-legacy` PASS (exit 0, 4/4, 2 LLM calls = 1 replayed Codec fix + 1 harvest `NONE`, advisory replaced, `java 1.6 → 21`, JAXB → `jakarta.xml.bind-api` + `jaxb-runtime`, no `<source>1.6`, commons-lang3 3.1 → 3.17.0) |
| Integration | `System.getProperty`-only keys get no placeholder (application.properties never feeds them; the Handover row says `-D<key>`); follow-up counts keys written; java6-legacy registered (18 fixtures) | `sbmig/analyze/properties.py`, `tests/`, `scripts/e2e.py`, `fixtures/projects/README.md` | done |
| Offline tests | 1,072 passed (28 integration deselected) | | green |
| e2e (real Maven, this Mac) | `legacy-external-config`, `java6-legacy`, `spring5-xml`, `boot2` PASS | `out/e2e/summary.md` | green |
| Follow-ups | — | `build.skip_tests: true` is the default, so the tests-skipped fallback only acts where a company config runs tests (decide whether the fleet should run tests); after the Jakarta step JAXB stays pinned at `jakarta.xml.bind-api` 3.0.1 / `jaxb-runtime` 3.0.2 instead of the Boot-managed versions (also `pure-java`) — a `RemoveRedundantDependencyVersions` pass after the spring rewrite would drop the pins; `${…}` inside packaged values reuse the `property-source` ref source (a `config-placeholder` literal would be clearer); MSA placeholders use project-wide definitions, not per-service |

## Status — 2026-10-21, round 9 (follow-ups + edge cases)

User: "후속 항목 이어서 작업하고 엣지케이스 있는지 살펴보자". Scope = the open follow-ups of rounds 7a–8b that are code
(not fleet policy) plus an edge-case sweep of the recently built areas and of the legacy-Java path. Contracts first
(`sbmig/core/{models,interfaces,config}.py`, `config/default.yml`), then five opus workstreams on disjoint
directories; integration, wiring, tests, e2e and the commit in the main session. Facts checked before planning: the
pinned `UpgradeSpringBoot_4_0` chain already runs `RemoveRedundantDependencyVersions` (GTE) for Boot projects, so
the JAXB pin follow-up is a non-Boot problem (the managing parent only arrives at restructure); `UpgradeSpringBoot_2_0`
(Boot 1.x) and `UpgradeSpringFramework_5_0` are reachable from the umbrellas; javac 21 rejects `-bootclasspath` /
`-Xbootclasspath` / `-source` together with `--release`.

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `layout.drop_managed_versions`, `openrewrite.download_retry(_wait_s)`, `llm.cooldown_seconds`, `FixContext.reference_notes / reference_files`, `RestructureResult.managed_versions_dropped`, `MergeConflict.union`, `PropertyFinding.service`, `PropertyInventory.services`, `PropertyRefSource` `config-placeholder` | `sbmig/core/`, `config/default.yml`, `docs/INTERFACES.md` | done |
| WS-A rewrite resilience (X8): transfer-failure retry in `OpenRewriteRunner.run` (429 / 5xx / reset / timeout, never `Could not find artifact`; `download-retry:` prefix), literal `4.0.x` / `latest.release` left by a successful run → revert to the pre-step commit, wait, re-run once, else flag `rewrite-unresolved-versions` + follow-up; KB credit skipped when a phase failed only on dependency resolution (`kb_credit_skipped`) | `sbmig/rewrite/runner.py`, `sbmig/loop/orchestrator.py`, `docs/RECIPES.md` | done — 9 runner + 5 orchestrator + 10 credit tests |
| WS-B LLM cool-down (L5): the first streak of two transport failures pauses once per run (`llm_cooldown`, flag `llm-cooldown`, the two attempts do not count against `max_llm_attempts_per_cluster`), the second streak → unavailable; skipped near the end of the time budget; `0` = old behaviour | `sbmig/loop/fixloop.py`, root `tests/conftest.py` (no real sleep in any test) | done — 5 tests |
| WS-B merge-conflict hints: `BuildFixLoop.merge_conflicts` (wired from `report.merge_conflicts` after every restructure step) → `reference_notes` + the dropped copy as a read-only `reference_files` section (by file or by class simple name; ≤ 3 copies, 12 k chars; a patch aimed at a reference copy is dropped) | `sbmig/loop/fixloop.py`, `sbmig/fix/context.py`, orchestrator wiring | done — 8 tests; e2e `multi-module-conflict` |
| WS-B Handover paths after restructure (`resolve_current_path`), web `RunManager(max_finished_runs=200)` + `web-status.json` for evicted runs | `sbmig/report/handover.py`, `sbmig/web/runs.py` | done — 5 + 3 tests |
| WS-C analyze: per-service MSA inventory (`scan_properties(services=)`, `PropertyFinding.service`), `config-placeholder` source, round-8 bug (a host entry defined the placeholder it read: `db.password=${DB_PASSWORD}` hid the secret), `@ConditionalOnProperty` reads, BOM / `\${x}` / bootstrap.yml edges; 31 edge probes (table in the module docstring) | `sbmig/analyze/properties.py`, `analyzer.py` | done — 27 tests (31 cases) |
| WS-C advisory `ibatis2` (iBATIS 2 / `org.springframework.orm.ibatis` removed in Spring 4; guide; `## fix-loop: ibatis2`), distinct from `mybatis` | `sbmig/analyze/libraries.py`, `knowledge/libraries/advisories.yml`, `knowledge/notes/ibatis2.md`, `docs/ADVISORIES.md` | done — 5 + 2 tests; 13 advisories |
| WS-D drop managed versions (S14): after the in-house parent is set, explicit versions the declaring module's chain manages at ≥ are dropped (`gte`), using a *second* effective-pom capture (`depmgmt-after-restructure.json`; the as-is capture stays for the carry-over handler); `Orchestrator._effective_pom_after` supplies it (None with a custom build runner → "cleanup skipped" note) | `sbmig/restructure/restructurer.py`, `sbmig/build/effective.py`, `pom_editor.py` (`remove_version`), orchestrator wiring | done — 8 + 2 + 1 tests; e2e `pure-java` / `java6-legacy` `pom_dependency_unversioned` |
| WS-D per-service placeholders (MSA) + merge edges S5 (outside `../x` module skipped under `merge`), S9 registry union (`META-INF/services`, `spring.factories`, `*.imports`), S10 pom-packaging sub-module (children merged, management folded into the root), S11 WAR module, S12 kept boot resources, S13 custom `<resources>` (also fixed: one module's custom `<resources>` used to hide the others' `src/main/resources`) | `sbmig/restructure/restructurer.py`, `docs/ARCHITECTURE.md` §8 | done — 9 + 3 tests |
| WS-E fixtures: `multi-module-conflict` (same-FQCN class differing across modules + services registry; replayed LLM fix uses the hint), `legacy-ibatis` (Spring 3.2 + iBATIS 2.3.4.726, builds as far as possible: exit 2, blocked `repeated_failure`, advisory in the Handover), `msa` per-service keys, `pure-java` / `java6-legacy` unversioned JAXB; e2e post-checks `pom_dependency_unversioned` / `_versioned`, `expect.properties.external_in` | `fixtures/projects/`, `scripts/e2e.py`, `tests/fixtures/` | done — 20 fixtures |
| PM edge cases: fleet — an intake-rejected Gradle / Ant project is `skipped` (not `aborted`: no `stop_on_abort`, no `retry_failed`, sticky on resume); handler `compiler-legacy-config` (E10: `<fork>` / `<executable>` / `<compilerVersion>` / `bootclasspath` / `-Xbootclasspath` / `-extdirs` / `-source` / `-target` compiler config javac 21 rejects — pre-emptive + reactive), `java6-legacy` pom now carries such a block; provider `company`: an OpenAI / Azure content-filter answer (`finish_reason=content_filter`, `prompt_filter_results`) is a request rejection, never an "LLM unavailable" streak member (the in-house GPT gateway answers in the OpenAI / Azure shape, auto-detected — no parsing change needed) | `sbmig/fleet/runner.py`, `sbmig/triage/handlers/pom_handlers.py`, `sbmig/plan/pre_handlers.py`, `sbmig/fix/company.py`, `sbmig/loop/fixloop.py` | done — 1 + 3 + 8 + 1 tests |
| Docs | `docs/ERROR-HANDLING.md` (X8, E10, L5, S5, S9–S14, §6 `ibatis2`), `docs/INTERFACES.md`, `docs/FLEET.md`, `docs/RUNBOOK.md`, `docs/RECIPES.md`, `docs/ADVISORIES.md`, `docs/ARCHITECTURE.md`, `fixtures/projects/README.md`, `CLAUDE.md` | done |
| Offline tests | 1,200 passed (30 integration deselected) | | green |
| e2e (real Maven, this Mac) | `scripts/e2e.sh --only pure-java,java6-legacy,multi-module-conflict,legacy-ibatis,msa,multi-module-merge,legacy-external-config,boot2` → 8/8 `expect=PASS` (pure-java 0 LLM, JAXB unversioned; java6-legacy 2 LLM, no `<bootclasspath>`/`<fork>` left, JAXB unversioned; multi-module-conflict exit 0, 2 LLM = 1 replayed fix with the merge hint + 1 harvest, services registry union; legacy-ibatis exit 2, 1/4 phases, blocked `repeated_failure`, advisory `ibatis2:still-present` in the Handover; msa 6/6 with `order.tax-rate=__SET_ME__` only in order-service; multi-module-merge / legacy-external-config / boot2 unchanged PASS) | `out/e2e/summary.md` | green |
| Follow-ups | — | `pluginManagement` of merged pom modules is not folded into the root (only dependencyManagement + properties); a union `spring.factories` is rewritten normalized (`key=a,\` form, comments dropped, originals saved); relaxed-binding spellings (`my-key` / `myKey` / `MY_KEY`) stay separate findings (three placeholders); `ibatis2` is detected from the pom + imports only (a bean named only in Spring XML is missed); a blocked item's attempt count still includes the two cooled-down attempts; the orchestrator re-runs a rewrite step at most once whatever `download_retry` says; cross-file clustering by symbol and the fleet test policy (`build.skip_tests`) remain open |
