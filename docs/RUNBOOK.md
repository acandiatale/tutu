# Runbook — operating sbmig in the company environment

## 0. Prerequisites on the migration host
| Item | Requirement | Why |
|---|---|---|
| Python | ≥ 3.10 (+ `uv` or `pip`) | agent runtime |
| JDK | **21** (set `build.java_home`) | OpenRewrite plugin and all builds run on 21; as-is projects are never built on their old JDK |
| Maven | 3.9.x; `build.settings_file` = a settings.xml with a **catch-all mirror** to the Nexus group (`config/settings.nexus.example.xml`, §0.1) | builds + OpenRewrite plugin; the mirror also covers the repositories declared inside the project poms |
| Nexus proxies | Maven Central incl. `org.openrewrite.*` (plugin 6.46.1, recipe BOM **3.37.0** and its artifacts (3.38.0 is inconsistent on Central), rewrite-recipe-bom 3.37.0 → rewrite-migrate-java 3.42.0, rewrite-spring 6.37.0, rewrite-testing-frameworks 3.44.0, rewrite-static-analysis 2.41.0, rewrite-java-dependencies 1.60.2), `org.springframework.boot:*:4.1.0`, jakarta artifacts | recipes and target BOM |
| LLM endpoint | the in-house gateway: `.env` with `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` (provider `company`, plain `requests.post`) | residual fixes + recipe candidates |
| git | any | per-attempt snapshots / revert / diff |

### 0.1 Repositories declared inside the project poms (air-gapped host)
Legacy poms declare `<repositories>` / `<pluginRepositories>` of their own (an old Nexus host, Maven Central, Spring
milestones). Maven contacts those URLs in addition to the settings, so on the migration host every artifact costs a
resolution failure or a connect timeout. Two levers, both config (invariant 2):

* **Mirror everything** (recommended): copy `config/settings.nexus.example.xml`, set the group URL, point
  `build.settings_file` at it. Its `<mirrorOf>external:*</mirrorOf>` rewrites every remote repository — the ones in
  the poms included — to the group; `file://` / localhost repositories stay untouched and Maven 3.8+'s default
  `http://` blocker ("Blocked mirror for repositories") is overridden for them. sbmig passes `-s` to every Maven
  call (builds, OpenRewrite, effective pom, BOM fetch). The analysis notes which URLs the poms declare; when no
  catch-all mirror is configured the note becomes a follow-up in the report.
* **Strip them**: `build.pom_repositories: remove` — the pre-emptive handler `strip-pom-repositories` deletes the
  blocks from every pom before the first build (profiles included, comments kept), so the migrated poms no longer
  carry dead URLs. It needs the mirror above: without it the removed URLs may have been the only route.

If a build then says an artifact "was cached from a remote repository ID that is unavailable in current build
context", the local repository remembers the old repository id (`_remote.repositories`): add `-llr` to
`build.extra_args` for that project or use a fresh `build.local_repo`.

```bash
cd migration-agent
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml      # fill in parent / mandatory ×2 / optional ×8
cp .env.example .env                                     # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL (git-ignored)
sbmig llm-probe --prompt "안녕"                            # exact request, raw response, extracted reply (needs .env only)
sbmig llm-probe --prompt "안녕" --payload-extra '{}' --no-system --model <postman model>   # compare with Postman
sbmig run <project> --config config/artifacts.yml
```

The gateway URL is used verbatim (no scheme change, no path appended): `LLM_BASE_URL=http://...` exactly as the
working in-house agents call it. If `requests.post` works by hand but sbmig gets an error, run `sbmig llm-probe`
(and `--bare`): it prints the exact request, the proxies in effect and the response headers/body.
Response shape: an OpenAI / Azure chat-completions answer (`choices[0].message.content`, `usage.prompt_tokens`,
`finish_reason`, Azure's `prompt_filter_results` / `content_filter_result`, a gateway `routing` field) is read without
any setting — `LLM_RESPONSE_PATH` is only for gateways that wrap the text elsewhere (`data.result.content`). A
content-filter answer (`finish_reason=content_filter`, null content) is reported as that request's rejection, not
as an outage (round 9).
Windows: Maven is `mvn.cmd`; sbmig resolves `build.mvn` through PATH / `MAVEN_HOME` (`shutil.which`), so `mvn` works
when `where mvn` finds it — otherwise set `build.mvn: C:/apache-maven-3.9.x/bin/mvn.cmd` and `build.java_home`.
Windows / no shell profile: put the variables in `.env` at the repo root (copy `.env.example`; git-ignored) or point
`SBMIG_ENV_FILE` at a file; `load_config` reads it before anything else and never overrides variables already set.
`llm.api_key_env` names the variable (`LLM_API_KEY`), the token itself never goes into a yml.
Stopping the web server: `sbmig serve` passes `--timeout-graceful-shutdown 5` to uvicorn (option `--graceful-timeout`),
the lifespan shutdown ends every open event stream, asks running runs to stop after their current graph node and
kills the Maven / OpenRewrite processes still running (`taskkill /T /F` on Windows). Under plain `uvicorn …` add
`--timeout-graceful-shutdown 5` yourself, otherwise Ctrl+C waits for the browser tabs' SSE connections forever
("Waiting for connections to close"). A run interrupted this way is reported `aborted` and has no final build.
Windows / console code page (cp949): sbmig decodes git and Maven output as UTF-8 itself and writes
`changes.patch` as the raw `git diff` bytes (an MS949 source stays MS949 in the patch). A `UnicodeDecodeError: 'cp949'
codec can't decode byte …` traceback from a subprocess reader thread, followed by `could not write changes.patch: data
must be str, not NoneType`, is the pre-2026-10-21 checkout — pull.

## "Is it stuck?" — three places to look (round 9c)

1. `tail -f <run dir>/sbmig.log`. While something slow runs the log keeps talking: `llm call … → POST` then
   `llm call still in flight: Ns elapsed` every 30 s (an LLM answer), `mvn still running: Ns elapsed … log: <file>`
   every 60 s (a build / recipe run). A call is abandoned after `llm.timeout_seconds + 30` s and retried; a build is
   killed after `build.timeout_minutes`.
2. The fix loop's watchdog: when no milestone was passed for 60 s it logs
   `[spring] fix loop quiet for 120s since '<milestone>' …` — the milestone names the code that is running
   (`deciding <cluster>`, `advisory hints`, `merge-conflict hints`, `fixer.propose … (prompt packing + LLM call)`,
   `LLM answered: applying`, `git commit`, `mvn package (build 3)`). `fix prompt for <cluster>: packing …` /
   `… packed: N chars → calling the LLM` bracket the prompt construction.
3. Nothing at all for minutes: get a thread dump — on Windows (no `kill -USR1`) open
   `http://127.0.0.1:8765/api/threads` (also written to `<out>/threads-<timestamp>.txt`); on POSIX
   `kill -USR1 <pid of sbmig / uvicorn>` writes `out/threads-<timestamp>.txt`. The fix loop also writes one by itself
   into the run directory after 5 minutes of silence. Send that file with the last 30 lines of `sbmig.log`.
   Order of phases: jdk21 → spring → restructure (parents / layout) → verify. With `plan.parent_phase: spring`
   (set in `config/artifacts.example.yml` for company runs) the parents / layout are applied right after
   `spring.rewrite`, before the spring build-fix loop: the loop then fixes against the company parent chain and a
   missing parent aborts early instead of after the spring loop.
4. A run that stops right after the journal's `llm_fix` event (answer received, no `attempt` event) was blocked in
   the patch apply / `git commit` step. Workspace git is non-interactive since round 9c (prompts, hooks, GPG signing
   disabled per call, 10-minute timeout) — if it still hangs there, an antivirus / indexer is holding the files.

Smoke test without touching anything: `sbmig analyze path/to/project.zip --config config/artifacts.yml`.
`token recognition error at: '<'` lines in a rewrite log come from OpenRewrite's XML parser, which also reads
.jsp/.jspx/.html/.htm (scriptlets, `<` in attribute strings): warnings only, the file is skipped. Since round 9c those
globs are excluded from the parse (`openrewrite.exclusions`); clear the list only if a company recipe must edit JSPs.
Every command except `llm-probe` needs `--config config/artifacts.yml` (the company artifacts section has no
default); without it the CLI stops with `config error … artifacts: Field required` and the hint, exit 2.

## 0.5 LLM roles (two models)
| Role | Config key (`.env` variable) | Suggested model | Used for |
|---|---|---|---|
| source fixes | `llm.model` (`LLM_MODEL`) | the strongest coding model you have (`claude-sonnet-5` on Anthropic) | build-fix loop patches (SEARCH/REPLACE protocol) — this is where quality matters most |
| recipe work | `llm.recipe_model` (`LLM_RECIPE_MODEL`, empty = `LLM_MODEL`) | a cheaper / faster model (`claude-haiku-4-5` on Anthropic) | plan advisor (`plan.advisor: llm`, additive, whitelist-only) and KB recipe-candidate generation (round 9: from `.env`) |
| escalation | `llm.escalation_model` (`LLM_ESCALATION_MODEL`, empty = `LLM_MODEL`) | optional stronger model | a cluster that stalled `loop.stall_limit` rounds (round 9: from `.env`) |

`config/llm-anthropic.yml` is a ready profile for the Anthropic API (key only via `LLM_API_KEY`; never in files).
LLM calls are recorded when `LLM_RECORD_DIR` is set (e.g. `out/llm-records`, in `.env.example`). Every `sbmig run` writes
`<run dir>/sbmig.log` (DEBUG: decisions, builds, LLM calls — never keys); `--log-file PATH` adds one for any
command; `llm.record_dir` keeps each request/response as `llm-NNNN.json`. When asking for help, share
`sbmig.log`, `report.md` and the relevant `llm-NNNN.json`.

`sbmig serve` (web UI): `sbmig serve --host 0.0.0.0 --port 8765 --out out/web` (FastAPI on uvicorn; the same app
is `SBMIG_WEB_OUT=out/web SBMIG_WEB_REPO_ROOT=<sbmig repo> uvicorn sbmig.web.app:app --host 0.0.0.0 --port 8765` for
the company's uvicorn hosting) and open the page; upload a zip, pick the config files (e.g. `artifacts.yml` +
`llm-company.yml`), start. The upload cap is 512 MB: `--max-upload-mb N` (or `SBMIG_MAX_UPLOAD_MB`; `0` = no
limit) raises it for one oversized project — the body is streamed to `<out>/_uploads/`, not held in memory. Each run executes **in the server process** through the LangGraph engine
(`sbmig.graph.stream_graph`, one worker thread per run; without LangGraph it falls back to the inline engine and the
graph strip stays dark). The page streams the graph node updates, `journal.jsonl` (phases, builds, decisions, LLM
fixes) and the run's `sbmig.log` over SSE (`status | node | error | journal | log | report | ping | end`), and links
`report.md` / `report.json` / `result.zip` / `sbmig.log` when the run ends; the server-wide log is
`<out>/sbmig-web.log`. "중단" (stop) takes effect after the current graph node: a running Maven build, OpenRewrite run
or LLM call is not interrupted; the run is then finalized as aborted (exit code 3) without the final `mvn clean
package` — a stop that arrives once the verify phase has run changes nothing. The server needs the same
environment as the CLI (`JAVA21_HOME`, `LLM_BASE_URL`, `LLM_API_KEY`, or the `.env` next to it); it binds to localhost unless `--host` says
otherwise. Runs share the file KB (`kb/`), so the server executes **one migration at a time**: further uploads
wait as `queued` and start in order.
Every call is recorded under `llm.record_dir` without secrets; the advisor's tokens are included in the report's LLM usage.

## 0.6 Plugging in the in-house LLM gateway or agent (adapter pattern)
Two seams, both configured without code changes in sbmig (details and templates: `docs/ADAPTERS.md`,
`examples/adapters/`, `config/llm-plugin.example.yml`):

| Tier | Config | You implement | sbmig keeps doing |
|---|---|---|---|
| 1. chat completion | `llm.provider: plugin`, `llm.plugin: "mypkg.adapter:make_adapter"` (or an entry point in group `sbmig.llm_adapters`) | `complete(LLMRequest) -> LLMResponse` (messages, model, role `fix` / `fix_escalation` / `plan_advisor` / `recipe_candidate`, metadata) | prompts, SEARCH/REPLACE protocol, context packing, feedback rounds, budgets, recording, KB harvest |
| 2. whole fixer | `fix.plugin: "mypkg.agent:make_fixer"` | `propose(ws, cluster, ctx) -> FixProposal` (your agent decides the patch; `sbmig.fix.ContextPacker` gives you our packed context) | triage, blocked rules, build loop, revert/oscillation safety, KB harvest of what worked |

Keys stay in environment variables (`llm.api_key_env`); the plugin receives them in `options["api_key"]` and sbmig
never writes them anywhere. Programmatic use from your own agent code: `sbmig.api.migrate(path, configs=[...], llm=MyAdapter())`.

## 1. Running a migration
```bash
sbmig run path/to/project.zip --config config/artifacts.yml --out out/
#  --stop-after intake|analyze|plan|jdk21|spring|restructure|verify   stop early (always still writes a report)
#  --dry-run                                analyze + plan + decisions only, no edits
#  --java-home /usr/lib/jvm/java-21 --settings /etc/maven/nexus-settings.xml
```
Exit code 0 = final `mvn clean package` passed and nothing is blocked. 2 = finished with **blocked** and/or unresolved
errors (read `report.md`). 3 = aborted (no Maven, not a Maven project, configured in-house parent not resolvable).
Gradle and Ant projects (no `pom.xml` anywhere) are rejected at intake, before any Maven call, CLI and web alike:
exit code 3, flag `unsupported-build-tool:<gradle|ant|none>` and the reason in `report.md` (manual follow-ups).
Library advisories (EOL / unmanaged libraries, §3.7) never change the exit code — they go to the Handover (§2.5).

Outputs in `out/<run_id>/`:
* `project/` migrated working copy (git branch `sbmig/<run_id>`, one commit per step/attempt) and `result.zip`
* `report.md` / `report.json` — **Handover** first (versions replaced, libraries needing attention, "from here on, your team" — §2.5), then what changed, per-phase iterations, every fix attempt with outcome + commit, **unresolved errors**, **manual follow-ups** for the as-is owner (web.xml, JSP, XML bean wiring, skipped tests…)
* `changes.patch`, `journal.jsonl` (machine-readable event log), `logs/` (every build / rewrite / LLM call)

## 2. Reading a failed run
0. **"Blocked — needs human / library action"** is the first section after the summary. These clusters were skipped on
   purpose (see `docs/ERROR-HANDLING.md`): a dependency that still exposes `javax.*` in its API (owner class and the
   artifact it came from are named, with the concrete action: bump/upgrade the library or add it to `artifacts.optional`),
   an artifact missing from Nexus, an incompatible build plugin, LLM/budget exhaustion. The rest of the migration
   (restructure, verify, report) still ran, so once the library is fixed a re-run usually completes.
1. `report.md` → "Unresolved errors" lists clusters by file with the last javac/maven messages.
2. `sbmig parse-log out/<run>/logs/0NN-build-*.log` re-parses any log with signatures (useful to craft a KB pattern).
3. `git -C out/<run>/project log --oneline` shows the attempt chain; `reverted` attempts are gone from the tree but in the report.
4. Typical causes: Nexus missing an artifact (DEPENDENCY_RESOLUTION cluster → fix proxy, rerun), in-house parent not managing a version (`'dependencies.dependency.version' is missing` → add to `optional` with a version), LLM budget exhausted (`loop.max_llm_calls`).

## 2.5 Reading the Handover (versions replaced, libraries needing attention, properties to set, from here on)
The first section of every `report.md` is `## Handover — what we replaced, what is yours` (`report.json`:
`handover` + `advisories`). It is written for the owning team, green run or not, and compresses the run into what
they must know before they take the project back. Reference run: the fixture `boot2-cloud-legacy`
(`scripts/e2e.sh --only boot2-cloud-legacy`) — springfox, sleuth, hystrix and an old Spring Cloud train in one jar.

* **`### Versions replaced`** — parent / Java / Spring Boot as-is → final (e.g. `spring-boot-starter-parent:2.7.18`
  → the in-house parent, `11 → 21`, `2.7.18 → 4.1.0`), then a table of every dependency / BOM / plugin / property
  whose version changed (as-is version: declared, else the one the old parent managed; final: declared, else
  "managed" by the new parent/BOM), with the reason (`parent swap`, `recipe`, `optional bump`, `mandatory`,
  `advisory:<id>`). Rows marked **⚠ carried over** are dependencies the old parent/BOM managed and the new one does
  not: sbmig pinned them **at the as-is version** so the build keeps working. That pin is a stop-gap — confirm the
  version is still right for Boot 4.1 or replace the library. Below the table: dependencies added and removed.
* **`### Libraries needing your attention`** — one row per library advisory (EOL or no longer version-managed
  libraries that still compile, so the build alone would look green): library, as-is version, status
  (`eol` / `unmanaged` / `incompatible` / `superseded`), policy (`guide` / `recipe`), what we did (recipe ids and
  whether they came from the Boot 4 umbrella chain = `plan` or were added for the advisory = `advisory`), outcome
  on the final tree (`replaced` = the old artifact is gone and the replacement present, `still-present`,
  `unknown`), what you do next, docs links.
* **`### Properties to set before running`** (round 8) — settings the code reads that no packaged resource defines.
  Before the migration the external Tomcat supplied them (JNDI `<Resource>` / `<Environment>` in `context.xml`,
  web.xml `<context-param>`, `-D` flags in `setenv.sh`, `$CATALINA_BASE/conf/*.properties`); the executable boot-jar
  reads none of that. One row per key: `key`, `before migration` (the as-is supply route: `jndi`, `context-param`,
  `system-property`, `property-source:file`, `unknown`), `referenced at` (first `externalized.max_refs_per_key`
  places, `+N more`), `written as` (the placeholder lines sbmig wrote, or the rule note when nothing was written,
  e.g. `contextConfigLocation` → `@ImportResource`), `set via` (environment variable name in relaxed-binding form,
  `-D<key>` for `System.getProperty` keys, the variable itself for `System.getenv`). Below: the supply guide from
  `knowledge/config/externalized.yml` and the packaged placeholder file.
* **`### From here on, your team`** — a numbered, deduplicated to-do list (capped by `advisories.max_from_here`):
  advisories not replaced, replaced-but-verify items (e.g. "check `/swagger-ui/index.html`", tracing sampling),
  carried-over pins, pointers to blocked / unresolved items further down, and the remaining manual follow-ups.

**No-block policy.** Advisories never fail a run and never change the exit code: there is deliberately no `block`
policy (500 projects cannot all be perfect; the migration goes as far as it can and hands over the rest). A run with
hystrix still in the pom is exit 0 with hystrix listed under "From here on". How a team acts on it: work the
numbered list top-down in the migrated branch, re-run `mvn clean package` (or `sbmig run` again — replaced
libraries drop out of the list), and only then deploy to a test environment; runtime behaviour was never verified
by sbmig.

### Externalized settings — what the owners do before the first start
1. **Find the placeholders**: `module/boot-jar/src/main/resources/application.properties` (each MSA service: its own
   `src/main/resources/application.properties`) has a block between `# --- sbmig externalized-config` and
   `# --- end sbmig externalized-config`, one `key=__SET_ME__` per key, each with a `# from …` comment naming where
   the code reads it. JNDI DataSources became `spring.datasource.url|username|password|driver-class-name`; a web.xml
   `<context-param>` became `server.servlet.context-parameters.<name>` **with its as-is value** (no sentinel).
2. **Supply the values outside the jar** (higher wins): `-Dkey=value` on the java command line, environment variables
   (`spring.datasource.url` → `SPRING_DATASOURCE_URL`, dots and dashes → underscores, upper case), an
   `application.properties` / `config/application.properties` next to the jar or
   `--spring.config.additional-location=…`. Keys read with `System.getProperty` only see `-D`. Do not commit real
   URLs or passwords into the packaged file.
3. **Check before deploying**: `grep -rn __SET_ME__ module/*/src/main/resources` on the source tree, or
   `unzip -p module/boot-jar/target/*.jar BOOT-INF/classes/application.properties | grep __SET_ME__` on the
   artifact — any hit left without an external override starts the app with the literal `__SET_ME__`.
4. **`tests-skipped` flag**: main code compiles and packages, but some tests failed at runtime (often a test that
   loads the Spring context and needs one of these keys); sbmig packaged the jar with `-DskipTests` and lists every
   failing test under Manual follow-ups / Unresolved errors. Give those tests test values
   (`@SpringBootTest(properties = …)` / `src/test/resources/application.properties`) and re-run
   `mvn clean package` without `-DskipTests` before relying on CI. `build.allow_skip_tests_fallback: false` turns the
   fallback off (the run then reports the failing tests instead of packaging).

## 3. Knowledge base workflow (no DB — files in `kb/`, keep them in git or sync as zip)
* Every successful LLM fix becomes `kb/cases/YYYY-MM/<id>.json`. Mechanical ones also produce `kb/recipes/candidates/<name>.yml`.
* Review candidates: `sbmig kb list --status candidate`, `sbmig kb show <id>`, then `sbmig kb promote <id>` (or `reject`). Promoted recipes are composed into **every** run's OpenRewrite execution and tried before the LLM when their signature matches.
* Auto-promotion: a candidate that succeeded in `kb.auto_promote_after_projects` distinct projects with no failures is promoted automatically. Set to 0 to require manual review.
* Move the KB between machines: `sbmig kb export kb-YYYYMMDD.zip` → `sbmig kb import kb-YYYYMMDD.zip`. `sbmig kb reindex` after manual edits.
* Hand-written shortcuts go in `kb/patterns/*.yml` (signature regex → recipes / handler ids / hints).
* Offline demonstration of the whole loop: `scripts/demo-kb/run.sh` (replayed LLM answers in `scripts/demo-kb/replay/`; run 1 harvests a case + candidate recipe, run 2 is fixed by that recipe without the LLM and promotes it).
* Recording real sessions: set `llm.record_dir` (every request/response is saved as `llm-<n>.json`); a recorded directory can be replayed with `llm.provider: fake` + `llm.base_url: replay:<dir>` for regression tests.
* **Cross-project memory** (`docs/FLEET.md`): every resolved or blocked cluster appends a line to `kb/ledger.jsonl`
  (project × error-case symbol × resolution × fix labels × "old → new"); `kb/symbols.json` aggregates per symbol.
  Labeled mechanical fixes (`type-rename`, `dependency-add`, `dependency-version`, `spring-property-rename`, …) are
  compiled into recipe candidates **without the LLM**; the rest go through the recipe model. Inspect with
  `sbmig kb ledger --symbol JsonComponent`, `sbmig kb symbols`, `sbmig kb learning` (what still needs the LLM → write a
  recipe/handler; which artifacts block the most projects → library owners).
* Recipe inventory: `sbmig recipes list --kb` (catalog + live KB recipes), `sbmig recipes check` (every id used by the
  config sets / reactive rules / base yml must be in `knowledge/recipes/catalog.yml`) — see `docs/RECIPES.md`.

## 3.5 Fleet mode — one agent, hundreds of projects, sequentially
```bash
sbmig fleet run projects.yml --config config/artifacts.yml --out out/fleet       # manifest: dir | .txt | .yml | .csv
sbmig fleet status out/fleet                                                     # progress table from fleet/state.json
sbmig fleet run projects.yml --config config/artifacts.yml --out out/fleet       # same command resumes; --retry-failed re-runs incomplete ones
```
Projects run strictly one after another; the KB is updated after every project, so project N+1 already plans with
the recipes learned from projects 1..N. `out/fleet/fleet/summary.{md,csv,json}` and `learning.md` are rewritten after
each project. Knobs: `fleet.order` (`manifest` | `simple-first`), `fleet.max_llm_calls_total`, `fleet.stop_on_abort`,
`fleet.reindex_every`.

## 3.7 Library advisories
`knowledge/libraries/advisories.yml` lists libraries that still compile after the migration but are EOL or no longer
managed by the new parent / Boot BOM (springfox, sleuth, hystrix, ribbon, zuul, netflix feign, old Spring Cloud
trains, legacy spring-security-oauth2, log4j 1, commons-lang 2, httpclient 4 — schema and catalog in
`docs/ADVISORIES.md`, per-library actions in `docs/ERROR-HANDLING.md` §6). `sbmig analyze` prints the matching
advisories; `sbmig run` adds them to the plan, verifies them on the final tree and writes the Handover (§2.5).
```bash
sbmig advisories list  --config config/artifacts.yml       # id, status, policy, replacement (effective, after overrides)
sbmig advisories show springfox --config config/artifacts.yml   # full entry: recipes, follow-up, docs
sbmig advisories check --config config/artifacts.yml       # schema + every recipe id is in knowledge/recipes/catalog.yml
```
Config (`advisories:` in `config/default.yml`):

| Knob | Default | Effect |
|---|---|---|
| `advisories.enabled` | true | false → no findings, no Handover library table (versions table stays) |
| `advisories.file` / `advisories.extra_files` | `knowledge/libraries/advisories.yml` / `[]` | company additions or overrides, same schema, merged by `id` (later file wins) — keep them in the company config repo, e.g. an in-house EOL logging library |
| `advisories.allow_recipes` | true | false → every advisory is `guide` (no advisory recipes added to the spring rewrite) |
| `advisories.policy_overrides` | `{}` | `{springfox: guide, hystrix: off}` — `guide` \| `recipe` \| `off` per id (`off` = not reported) |
| `advisories.report_carried_over` | true | list the ⚠ carried-over pins in the Handover |
| `advisories.ledger` | true | one `resolution=advisory` line per finding in `kb/ledger.jsonl` → fleet `learning.md` "Libraries needing owner action" |
| `advisories.max_from_here` | 40 | cap of the "From here on, your team" list |

Policies: `guide` (default) = build, then guidance in the Handover / manual follow-ups / ledger; `recipe` = also run
the entry's verified OpenRewrite recipes in the spring phase (prepended to the spring rewrite; falls back to `guide`
when a recipe is unavailable). Sleuth and the Spring Cloud train are already covered by the Boot 4 umbrella chain
(`MigrateCloudSleuthToMicrometerTracing`, `UpgradeSpringCloud_2025_1`), so they show `recipe_source=plan`.

**Before switching an entry to `recipe`** (in `extra_files` or `policy_overrides`): (1) add the recipe id to
`knowledge/recipes/catalog.yml` (with its artifact coordinates) and run `sbmig advisories check` and
`sbmig recipes check`; (2) run it on a representative project with `sbmig run … --stop-after spring` and read the
diff (`changes.patch`) and the spring-phase build; (3) run the reference fixture
`scripts/e2e.sh --only boot2-cloud-legacy` (and `--llm-provider fake`, 0 LLM calls expected) — the advisory must end
`outcome=replaced` without new unresolved errors; (4) only then roll it out in fleet mode. A recipe that leaves the
project uncompilable costs LLM budget on every project — keep it `guide` until it is proven.

## 3.8 Webapps without a pom — `sbmig inventory` (2026-10-23, round 1: inventory only)
Classic exploded Tomcat WARs with no build file (JSPs, sources in `WEB-INF/src` or elsewhere, `WEB-INF/lib` jars and
`*.jar.old` backups, `WEB-INF/classes`, sometimes two webapp directories under one root) cannot go through `sbmig run`
(intake rejects them). Inventory them first:
```bash
sbmig inventory path/to/webapps-root --out out/inventory/<name>            # directory: read in place
sbmig inventory path/to/webapps.zip  --out out/inventory/<name> --config config/artifacts.yml   # zip; report.language
```
* Read only, no Maven, exit 0 (3 only when the path does not exist). `--config` is optional (only `report.language`
  is read); `--json` prints the full `WebappInventory` instead of the console summary.
* Console summary (≤ 12 lines, retypeable): root, webapp roots with JSP count / servlet spec / frameworks, source
  roots with file count and encoding, Java level from the class files, jars known / unknown / ignored (first 3
  unknown names), classes without source, duplicate classes, module proposals, build files, warnings, output paths.
* `inventory.md` (en | ko) + `inventory.json`: per webapp root (web.xml spec, servlets / filters / listeners /
  taglibs, frameworks, JNDI names from `META-INF/context.xml` + `resource-ref`, JSP / static counts, Spring XML,
  class-file Java levels, classes without source), the jar table (unknown coordinates first, sha1 for a Nexus
  lookup), source roots (encoding, javax / framework imports, `sun.*`), duplicate classes, one Maven module proposal
  per webapp root with notes, and the next steps.
* What the operator does: check the "unknown coordinates" jars and add the company ones to
  `knowledge/libraries/jar_names.yml` (`stem` → `groupId:artifactId`; the version comes from the file name), re-run,
  confirm the module split. A pom found anywhere in the tree is a warning: that module goes through `sbmig run`.
  Round 2 (pom synthesis from `inventory.json`) is not built yet.

## 4. Tuning knobs (config/default.yml)
| Knob | Default | Effect |
|---|---|---|
| `loop.phases.<phase>.max_iterations` | 3 / 6 / 6 / 3 | build→fix rounds per phase (jdk21 / spring / restructure / **verify** — the verify loop only runs when the final `clean package` failed, then the final build is repeated; `verify: {max_iterations: 0}` turns it off) |
| `loop.max_llm_calls`, `loop.max_total_minutes` | 60 / 180 | hard budgets per run |
| `loop.stall_limit` | 2 | identical error set N times → escalation model + widen context |
| `llm.escalation_model` | null | stronger model after a stall |
| `llm.pricing.<model>` / `llm.pricing_currency` / `llm.pricing_note` | OpenAI list prices (2026-10-22) for `gpt-5.6-terra`, `gpt-6-luna` | per-project API cost in the report ("LLM usage and cost", `report.json` `llm_cost`) and the fleet `summary.md` (`llm_cost` column); unpriced models are listed, never guessed |
| `build.skip_test_compile` | false (company: true) | `-Dmaven.test.skip=true` on every build — test sources neither compiled nor run (flag `tests-not-compiled`, Handover follow-up); the company's decision (2026-10-22) |
| `build.allow_skip_test_compile_fallback` | true | last resort `-Dmaven.test.skip=true`, flagged in the report |
| `layout.split_policy` | minimal | `web-layer` moves controllers/config into boot-jar |
| `plan.intermediate_builds` | true | build after the JDK 21 phase before Spring recipes |
| `kb.generate_recipe_candidates` | true | LLM proposes declarative recipes from fixes |
| `artifacts.optional_version_policy` | floor | never downgrade an optional artifact that is already newer (`pin` = exact version) |
| `plan.optional_bump_phase` | spring | bump in-house optional artifacts right after the spring rewrite (Jakarta builds of in-house libs before the first build) |
| `build.source_encoding_policy` | set-property | legacy MS949/EUC-KR sources: set `project.build.sourceEncoding` (or `convert-utf8`); files are always read/written in their own encoding |
| `build.enforcer_policy` / `build.werror_policy` | relax-rule / remove | enforcer `requireJavaVersion` etc. and `-Werror` on JDK 21 |
| `loop.blocked_policy.<reason>` | 0–1 | LLM attempts allowed before a cluster of that reason is blocked and reported |
| `loop.max_llm_attempts_per_cluster` | 2 | after that the cluster is blocked (`repeated_failure`) |
| `loop.verify_extra_llm_attempts` | 2 | final verify loop only: clusters blocked as `repeated_failure` / `oscillation` are released and get this many more LLM attempts (escalation model); 0 = never release |
| `advisories.*` | see §3.7 | library advisories: policies, company files, carried-over listing; never affect the exit code |

## 5. Safety notes
* The agent only edits the copy under `out/<run>/project`; the input zip/dir is never modified.
* No LLM call contains credentials; prompts include only the files/errors needed for the cluster.
* `llm.record_dir` records every request/response for audit and offline replay tests.
