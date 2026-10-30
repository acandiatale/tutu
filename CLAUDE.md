# sbmig — working notes for Claude (read this first)

In-house **JDK 21 + Spring Boot 4.1.0 migration agent** for company Maven projects. Python 3.10+, CLI `sbmig`.
This branch (`flow-v2`) is **v2**: one OpenRewrite run, the company restructure, an LLM structure review, then
`mvn clean package`, and every build error fixed by an **LLM-written Python fix script** stored per error case and
reused on later projects. The v1 flow (deterministic handlers, KB recipes, SEARCH/REPLACE patches) is the git tag
**`company-v1`** on `main`; its code is deleted here (PLAN-v2 step 4).
Read in this order: `README.md` → `docs/v2/PLAN-v2.md` (the business request, the decisions, the step table) →
`docs/migration-agent/README.md` (the design in one place: Mermaid per flow / node, Korean; `migration-agent.pptx`
beside it, rebuilt by `scripts/handbook/build_migration_agent_deck.js`) → `docs/INTERFACES.md` (contracts; the
"v2 — flow-v2" section is the one that counts, the older sections are history) → `docs/PLAN.md` (one "Status" table
per round, newest at the bottom) → the doc for the area you touch (`ARCHITECTURE`, `ERROR-HANDLING`, `FLEET`,
`ADAPTERS`, `RECIPES`, `ADVISORIES`, `RUNBOOK`).
The Korean handbook for the company (`docs/handbook/`, README → 01…09 + `sbmig-overview.pptx` + `sbmig-reference.xlsx`)
explains the same system for operators; `tests/docs/test_handbook.py` keeps its paths / config keys / CLI names /
links in sync with the code, and `scripts/handbook/` regenerates the xlsx / pptx. The KB data map for the business
team is `docs/kb-map/` (`kb-map.html` source, `index.html` single file, `kb-map.md`; `scripts/handbook/build_kb_map.py`).
**The handbook is updated only when the user asks for it** (2026-10-27: the code keeps changing, one tidy-up pass
later); record operator-visible changes (CLI, config keys, report sections, log lines) in `docs/PLAN.md` /
`docs/INTERFACES.md` meanwhile, and keep `tests/docs` green with minimal reference fixes only.

## Why this exists
- The user takes this code into their company to test it there. Company source code is **not** reachable from here.
- The previous in-house agent failed for two reasons we must not repeat: the fix LLM "finished" without running
  the build, and recipe selection by a small LLM was weak. Hence: deterministic planning, build-gated runs.
- v2 (business team, 2026-10-26): "레시피가 무엇을 바꿀지 모르니 넣지 않는다" — only the Java 21 / jakarta / Boot 4
  recipes run; everything else is a build error the LLM fixes with a script that the KB keeps and reuses.

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
- Maven projects, plus **Ant projects since 2026-10-29 (v2.1j)**: a tree with `build.xml` and no pom.xml gets a pom.xml
  synthesized at intake (`sbmig/analyze/ant.py`: layout from build.xml, jar coordinates from the jar's pom.properties →
  `knowledge/libraries/jar_names.yml` + `<kb>/jar_names.yml` → sha1 repository lookup; unknown-but-used jars as `system`
  scope at their as-is path, listed in the Handover with `install-file` commands; `intake.*` keys) and then runs like
  any Maven project. Gradle is still rejected (exit 3), and so are trees **without pom.xml or build.xml** (company
  decision 2026-10-23: no pom synthesis for exploded Tomcat JSP + Java directories); `sbmig inventory <dir>` (read-only)
  stays as the tool that describes such a tree for the rejection note to the owners; it changes nothing.
  A Maven tree **without a root pom** (sibling poms, or one `APP/pom.xml` aggregating `../DEV` — the company shape) gets a
  generated root aggregator at intake (v2.1k: also for a single top); a module reached twice in the reactor is de-duplicated
  there (`reactor-duplicate-removed`), and `duplicated in the reactor` never goes to the LLM (`reactor_duplicate`, 0 tries).
- MSA: same parent / mandatory / optional per service, directory structure untouched (pom-only mode).
- Scope = packaging + build passing (`mvn clean package` → BUILD SUCCESS). Runtime errors belong to the as-is owners.
  Tests are out of scope entirely (user, 2026-10-22: "테스트는 다 스킵"): every build runs with
  `-Dmaven.test.skip=true` (`build.skip_test_compile: true` is the v2 default; flag `tests-not-compiled`, Handover
  follow-up for the owning team).
- Real in-house coordinates are unknown here → placeholders in `config/artifacts.example.yml`; public stand-ins in
  `config/artifacts.local-e2e.yml` for local e2e. Never invent "real" company coordinates.
- The company has its own LLM API gateway and its own agent-building framework. They plug in through the adapter
  seams (`llm.plugin` tier 1, `fix.plugin` tier 2 = `propose_script`, see `docs/ADAPTERS.md`), not through new HTTP clients.
- Operating model: **one agent migrates ~500 projects strictly sequentially**. Every fix script is recorded per
  error case (L2 signature) so later projects reuse it without an LLM call: `sbmig fleet`, `kb-v2/scripts/`,
  `sbmig kb scripts list`.
- EOL / unmanaged libraries (springfox, hystrix, sleuth, old Spring Cloud trains, …): **no `block` policy, ever**
  (user: "정책상 빌드로 보지 않는건 없을거야" — 500 projects cannot all be perfect). Advisories are guide-only in v2:
  hints in the script prompt + the report **Handover** section first.
- v2 decisions (user, 2026-10-26, `docs/v2/PLAN-v2.md` §2): recipes = `UpgradeToJava21`, `JavaxMigrationToJakarta`
  (when the case needs it), Boot projects `UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1` (that umbrella already runs
  Spring Cloud 2025.1 / Security 7 / Framework 7), and since v2.1l (2026-10-30, "security 는 쓰는 경우만") the Spring Security
  chain `UpgradeSpringSecurity_6_0 … 6_5, 7_0` for a `spring_legacy` project whose analyzer found `spring-security`; no intermediate
  builds; the 32 deterministic handlers are out; the structure review fixes only critical findings by script; fix
  scripts get a restricted API in a separate process; advisories = hints + Handover; KB in `kb-v2/`.
- LLM keys come only from the environment (`LLM_API_KEY`). Never write a key into the repo, a config file, or a log.
  The user knowingly keeps using a free Anthropic test key; do not remind them to rotate it.

## Invariants (do not break)
1. **Build gate**: a run never ends without a final `mvn clean package` attempt and a report that lists unresolved
   errors; the script fix loop scores every attempt by a rebuild and rolls back what did not help.
2. Company specifics live in config / knowledge YAML, not in Python code.
3. The knowledge base is **file-based** (no database allowed).
4. Plugin seams stay stable: `LLMRequest` / `LLMResponse` (tier 1), `FixerLike.propose_script` (tier 2),
   `sbmig.api.migrate(...)`.
5. `knowledge/recipes/catalog.yml` is the single source of recipe ids; `sbmig recipes check` and a snapshot test
   guard drift (a company config that re-fills an empty set must re-list the ids in the catalog's `sets:`). Recipe BOM
   pinned to **3.37.0** (3.38.0 is inconsistent on Maven Central), which resolves rewrite-spring 6.37.0.
6. Test and demo runs write under `out/` only, never into the repo's `kb-v2/` (local e2e KBs are `out/e2e/kb-*`).
7. Fix scripts run through `sbmig.script` only: static check, `python -I` child process, helper API `p` limited to
   the project folder, timeout + process-tree kill. A guard against mistakes, not a sandbox against a hostile author.

## The v2 flow (what the code does; details in `docs/v2/PLAN-v2.md` and `docs/INTERFACES.md`)
intake (Maven only, aggregator for sibling poms, preflight) → analyze (profile, advisories as findings) → plan
(`transform.rewrite`, `restructure.*`, `restructure.review`, `verify.build`) → **transform** (one OpenRewrite run,
no build) → **restructure** (company layout exactly as v1, optional bump inside) → **structure review** (LLM checks
poms / dependencies / modules / source encodings / binary resources under filtering against the company rules;
critical → one fix script, committed `review: …`; warnings → report + `kb-v2/reviews.jsonl`) → **verify**
(`mvn clean package -Dmaven.test.skip=true`); on failure the **script fix loop** (`loop.phases.verify`, 12 rounds):
errors grouped per **case** (L2 signature; coarse `build` / `other` keys refined from the javac line), root causes
first, cases whose files / module pom a script already touched this round wait for the rebuild; per case
**reuse** the best stored script (no LLM) → **augment** it (LLM, ≤ `script.max_augments_per_case`) → **new** (LLM)
→ BLOCKED `repeated_failure`; every attempt is committed, scored by the rebuild (round-10 rules: improved / partial
/ no_change / regressed → reverted / inconclusive), rolled back before the case's next try unless it improved, and
credited to the stored version (`kb-v2/scripts/<case_id>/v<N>.py` + meta: lineage, successes / failures / projects).
Then the final build again, the report (Handover first, "Structure review", fix attempts as `script:<case>:v<N>`),
`error_cases.*` / `fix_attempts.jsonl` in the run dir and the KB (`errors.jsonl`, `fix_attempts.jsonl`, catalog,
views, audit, quarantine). `SBMIG-RESULT … review=<verdict>` is the line operators retype.

## Status — 2026-10-27 (`flow-v2`)
- v1 history (rounds 1–10, 2026-10-18 … 10-26, all on `main` / tag `company-v1`): pipeline + fixtures, blocked-cluster
  triage, adapter seams, recipe catalog, fleet + learning ledger, library advisories + Handover, `ChangeMethodName`
  harvest, LangGraph engine + FastAPI web UI, Hystrix → Resilience4j, multi-module `merge`, externalized config
  placeholders, Java 1.6/1.7 projects, role parents, the company layout (9c), pom-less webapp inventory, web.xml
  registrations, error cases + case catalog, a 5-role review (round 10). `docs/PLAN.md` has one table per round.
- v2 step 0 (contracts) `55d776e`, step 1 (six agents: script runtime, script KB, script prompts, structure review +
  report, v2 planner, script fix loop + orchestrator) `cb9a3b1`, root-cause ordering + deferral `50e9a49`, step 2
  (e2e: every fixture on the v2 flow with replayed LLM scripts; reuse pure-java → java6-legacy, augment
  boot2-fix-failures) `43d8b36`, step 4 (v1 code deleted; pom-change deferral; refined case keys) — this commit.
  Offline tests: **2,032** passed (42 integration deselected); real-Maven e2e `scripts/e2e.sh` **32/32**
  (29 build, 3 intended BLOCKED: boot2-javax-lib, missing-artifact, legacy-ibatis).
- Step 5 (company trial vs `company-v1`) is the user's: real projects, LLM calls / time / success rate.

## Known follow-ups (the user decides)
- **Dependency versions (v2.1l, 2026-10-30)**: the versions an as-is parent managed are pinned from the as-is effective pom
  (`logs/depmgmt.json`) inside the restructure, before the first build (`RestructureResult.versions_pinned`, Handover ⚠
  "carried over"); the fix loop fills a version-less dependency the LLM added (as-is build → newest repository release) and
  tries a `missing_version` case first without the LLM (`sbmig-version`); rule 14 tells the LLM to leave unknown versions
  `null`. v1's `carry-over-managed-version` handler had been deleted with the other handlers in v2 step 4 — that gap was the
  company's "정책상 버전을 추정하여 추가할 수 없습니다" loops. Contract: `docs/INTERFACES.md` "v2.1l".
- **Binary files under Maven resource filtering (v2.1m, 2026-10-30)**: the restructure writes maven-resources-plugin
  `nonFilteredFileExtensions` (the extensions of the binary files the analyzer's scan finds under filtered resources +
  `build.non_filtered_extensions`, e.g. the company's `dmdb`) into the pom that declares the filtering, before the first
  build (`sbmig/restructure/resources_filtering.py`; v1 handler E12 restored as a rule); the loop tries a
  `MalformedInputException` / `Input length = 1` case first without the LLM (`sbmig-resources`). Contract: `docs/INTERFACES.md` "v2.1m".
- **Optional artifacts by trigger (v2.1n, 2026-10-30)**: an optional in-house artifact with `when_present` (g:a keys / globs) is
  ADDED to a pom that declares one of those dependencies (`replaces: true` removes the matched declarations — the curated
  company artifact stands in for the raw library, e.g. `hibernate-core` → the company JPA starter); without `when_present` an
  optional is still only aligned when already used (S6). Contract: `docs/INTERFACES.md` "v2.1n".
- **MSA with non-Boot WAR services** (2026-10-30): `restructure.msa_pom` gives such a service the boot parent but no Boot app /
  layout split; the user checks the real projects first ("비-boot war는 일단 그대로 두자").
- **JSP `javax` → `jakarta` (decided 2026-10-28, v2.1h)**: OpenRewrite never parses JSPs, so the restructure renames
  the javax EE names of the analyzer's table inside every JSP / tag file's code regions after the files reached their
  final place, in every mode (`layout.jsp_jakarta_rename`, default true; `sbmig/restructure/jsp_jakarta.py`). Off →
  Handover follow-up only. Taglib URIs stay (JSTL 3 serves the `java.sun.com` URIs).
- Guide advisories (hystrix, ribbon, zuul, …) leave compiling code as it is; the Handover lists them.
- A case that cannot be fixed by code (legacy-ibatis) costs up to 4 LLM calls (new + 3 augments) before BLOCKED;
  `script.max_augments_per_case` is the knob. `loop.blocked_policy` gives javax libraries / missing artifacts /
  plugins 2 script attempts (a stored script is reused regardless).
- `script.python` must have pydantic and lxml installed (else the run reports `ModuleNotFoundError`); Windows paths
  over 260 characters are not handled specially.
- Items to validate in the company environment: `docs/RUNBOOK.md` §0; the first v2 runs should be compared with
  `company-v1` on the same projects.
- Multi-module `merge` (unchanged from v1): a same-FQCN class differing across modules is first-wins (the dropped
  copy reaches the script prompt as read-only reference); `pluginManagement` of a merged pom-packaging module is not
  folded into the root; a kept boot module whose @SpringBootApplication scans only its own package will not see the
  merged beans (runtime, reported as a note). Properties: relaxed-binding spellings of one key stay separate findings;
  `ibatis2` is detected from the pom + imports only.

## How to work here
- **Working mode the user asked for**: Claude plans and acts as PM in the main session. Write shared contracts
  first (`sbmig/core/{models,config,interfaces}.py`, docs), then fan implementation out to parallel `model: opus`
  subagents on **disjoint directories** with explicit briefs. Subagents never commit. Integration, test runs and
  commits happen in the main session. Per round: append a "Status" table to `docs/PLAN.md`, update
  `docs/INTERFACES.md` on contract changes, add a fixture + `expected.json` (+ `replay/`) for every new scenario.
- Setup: `uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` (plain `pip install -e ".[dev]"` works too).
- Fast check (Python only, ~6 min): `python -m pytest -m "not integration"` → 2,032 passed, 42 deselected
  (`-q` on top of the `-q` in pyproject hides the summary line).
  `ruff check sbmig tests` has pre-existing style findings (mostly `UP045`); lint is not a gate here, so fix only what
  you touch and never run a repo-wide `ruff --fix` inside a feature round.
- **Korean handover comments (2026-10-28, the business team's request)**: every module under `sbmig/` starts its
  docstring with a Korean block and every public top-level class / function has Korean first in its docstring, the English
  original kept unchanged after it; inline Korean comments say *why*. Convention + terminology table:
  `docs/v2/CODE-COMMENTS-KO.md` (exemplar `sbmig/kb/store.py`, package map `sbmig/__init__.py`);
  `tests/docs/test_korean_comments.py` fails for a new module or public definition without Korean. Prompt `.md` files,
  YAML, tests and `coding/` stay English.
- e2e needs **JDK 21** (`JAVA21_HOME`), Maven and network to Maven Central: `scripts/e2e.sh` → `out/e2e/summary.md`
  (`--only boot2,msa` for a subset; the reuse scenario needs pure-java before java6-legacy — `kb.after` in
  `expected.json`); KB demo `scripts/demo-kb/run.sh` (run 2 reuses run 1's script); fleet `scripts/fleet-demo.sh`.
  Integration tests: `python -m pytest -q -m integration`.
- Offline LLM: `llm.provider: fake` + `llm.base_url: replay:<dir>` replays `<dir>/llm-NNN.json` (`match: "case:
  <case_id>"`, `replay_role: fix | structure_review`); the fixtures' `sbmig.e2e.yml` do this.
- Real-LLM runs: provider `company` is the default (`config/default.yml`): `requests.post(LLM_BASE_URL,
  headers={Authorization: Bearer LLM_API_KEY}, json={model, messages, need_origin}, timeout)` in
  `sbmig/fix/company.py`; URL / key / model / `LLM_RESPONSE_PATH` / `LLM_RECORD_DIR` come from `.env` (git-ignored; read
  from the package folder and the cwd up to the git root, nearest first — the company keeps sbmig under `src/migration-agent/`,
  read by `load_config`, shell variables win) — no yml profile, no LangChain, no httpx knobs (the user asked for
  this after 503s on the gateway; keep it this simple). `sbmig llm-probe` (and `--bare`) prints the exact request,
  proxies and response. `config/llm-anthropic.yml` remains for Anthropic validation runs. Keys never go into yml.
- Logs: every `sbmig run` writes `<run dir>/sbmig.log` (DEBUG), fleets `fleet/sbmig.log`, any command takes
  `--log-file`; LLM request/response records go to `llm.record_dir`. The user shares these files to report problems;
  on the company's Windows PC without copy-paste, ask for specific lines (`SBMIG-RESULT`, `preflight: FAIL`, `cause=`).
- `coding/` is a standalone coding agent over the same gateway contract (`coding/README.md`), not part of `sbmig`.
- **Company convention (2026-10-20): agents run on LangGraph under uvicorn.** `loop.engine: graph` (default) drives
  the phases from a LangGraph `StateGraph` (`sbmig/graph/`, nodes intake → analyze → plan → phase×N → finalize;
  `inline` is the old loop, reports are identical by test); the web UI is FastAPI (`uvicorn sbmig.web.app:app` or
  `sbmig serve`), runs migrations in-process through `stream_graph` (one worker thread per run, per-run `sbmig.log`
  by thread filter) and streams `node` / `journal` / `log` / `report` events over SSE to `sbmig/web/static/index.html`
  (vanilla JS). Stop takes effect between graph nodes only. One migration at a time: further uploads are `queued`
  (shared file KB).
- Cloud sessions may lack JDK / Maven (the 2026-10-18 cloud image had JDK 21 at `/usr/lib/jvm/java-21-openjdk-amd64`
  and Maven at `/opt/maven`); without JDK 8/11 run `fixtures/libs/install.sh` with `JAVA8_HOME=$JAVA21_HOME` (JDK 21
  compiles the 1.8-target lib with a warning) and skip the `-m integration` as-is builds. Commit and push to a
  `claude/...` branch; the user merges locally.
