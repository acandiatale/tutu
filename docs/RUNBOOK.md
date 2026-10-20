# Runbook — operating sbmig in the company environment

## 0. Prerequisites on the migration host
| Item | Requirement | Why |
|---|---|---|
| Python | ≥ 3.10 (+ `uv` or `pip`) | agent runtime |
| JDK | **21** (set `build.java_home`) | OpenRewrite plugin and all builds run on 21; as-is projects are never built on their old JDK |
| Maven | 3.9.x, `settings.xml` pointing at Nexus | builds + OpenRewrite plugin |
| Nexus proxies | Maven Central incl. `org.openrewrite.*` (plugin 6.46.1, recipe BOM 3.38.0 and its artifacts), `org.springframework.boot:*:4.1.0`, jakarta artifacts | recipes and target BOM |
| LLM endpoint | OpenAI-compatible `/v1/chat/completions` (or Anthropic Messages) | residual fixes + recipe candidates |
| git | any | per-attempt snapshots / revert / diff |

```bash
cd migration-agent
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml      # fill in parent / mandatory ×2 / optional ×8
export LLM_BASE_URL=https://llm.internal/v1 LLM_API_KEY=... LLM_MODEL=<model>
# in-house gateway that only accepts a plain chat POST ({model, need_orgin, messages}): use the profile
#   export LLM_BASE_URL=https://<gateway>/<chat endpoint> LLM_API_KEY=...
#   sbmig llm-probe --config config/llm-company.yml      # raw response + extracted reply
#   sbmig run <project> --config config/artifacts.yml --config config/llm-company.yml
```

Smoke test without touching anything: `sbmig analyze path/to/project.zip --config config/artifacts.yml`.

## 0.5 LLM roles (two models)
| Role | Config key | Suggested model | Used for |
|---|---|---|---|
| source fixes | `llm.model` | `claude-sonnet-5` (or the in-house model) | build-fix loop patches (SEARCH/REPLACE protocol) |
| recipe work | `llm.recipe_model` | `claude-haiku-4-5` | plan advisor (`plan.advisor: llm`, additive, whitelist-only) and KB recipe-candidate generation |
| escalation | `llm.escalation_model` | optional stronger model | after a stall |

`config/llm-anthropic.yml` is a ready profile for the Anthropic API (key only via `LLM_API_KEY`; never in files).
`config/llm-company.yml` is the profile for the in-house gateway (`chat_post`). Every `sbmig run` writes
`<run dir>/sbmig.log` (DEBUG: decisions, builds, LLM calls — never keys); `--log-file PATH` adds one for any
command; `llm.record_dir` keeps each request/response as `llm-NNNN.json`. When asking for help, share
`sbmig.log`, `report.md` and the relevant `llm-NNNN.json`.

`sbmig serve` (web UI): `sbmig serve --host 0.0.0.0 --port 8765 --out out/web` and open the page; upload a zip, pick
the config files (e.g. `artifacts.yml` + `llm-company.yml`), start. The page streams `journal.jsonl` (phases,
builds, decisions, LLM fixes), `sbmig.log` and the console over SSE, and links `report.md` / `report.json` /
`result.zip` / `sbmig.log` when the run ends. The server needs the same environment as the CLI (`JAVA21_HOME`,
`LLM_BASE_URL`, `LLM_API_KEY`); it binds to localhost unless `--host` says otherwise.
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
#  --stop-after jdk21|spring|restructure   stop early (always still writes a report)
#  --dry-run                                analyze + plan + decisions only, no edits
#  --java-home /usr/lib/jvm/java-21 --settings /etc/maven/nexus-settings.xml
```
Exit code 0 = final `mvn clean package` passed and nothing is blocked. 2 = finished with **blocked** and/or unresolved
errors (read `report.md`). 3 = aborted (no Maven, not a Maven project, configured in-house parent not resolvable).
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

## 2.5 Reading the Handover (versions replaced, libraries needing attention, from here on)
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
* **`### From here on, your team`** — a numbered, deduplicated to-do list (capped by `advisories.max_from_here`):
  advisories not replaced, replaced-but-verify items (e.g. "check `/swagger-ui/index.html`", tracing sampling),
  carried-over pins, pointers to blocked / unresolved items further down, and the remaining manual follow-ups.

**No-block policy.** Advisories never fail a run and never change the exit code: there is deliberately no `block`
policy (500 projects cannot all be perfect; the migration goes as far as it can and hands over the rest). A run with
hystrix still in the pom is exit 0 with hystrix listed under "From here on". How a team acts on it: work the
numbered list top-down in the migrated branch, re-run `mvn clean package` (or `sbmig run` again — replaced
libraries drop out of the list), and only then deploy to a test environment; runtime behaviour was never verified
by sbmig.

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

## 4. Tuning knobs (config/default.yml)
| Knob | Default | Effect |
|---|---|---|
| `loop.phases.<phase>.max_iterations` | 3 / 6 / 6 | build→fix rounds per phase |
| `loop.max_llm_calls`, `loop.max_total_minutes` | 60 / 180 | hard budgets per run |
| `loop.stall_limit` | 2 | identical error set N times → escalation model + widen context |
| `llm.escalation_model` | null | stronger model after a stall |
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
| `advisories.*` | see §3.7 | library advisories: policies, company files, carried-over listing; never affect the exit code |

## 5. Safety notes
* The agent only edits the copy under `out/<run>/project`; the input zip/dir is never modified.
* No LLM call contains credentials; prompts include only the files/errors needed for the cluster.
* `llm.record_dir` records every request/response for audit and offline replay tests.
