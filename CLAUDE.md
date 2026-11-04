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
  module an executable WAR (`java -jar` runs the embedded Tomcat — the migration's target; the same file also deploys to an
  external Tomcat; JSPs cannot be served from an executable jar), otherwise it is an executable jar. `{name}` = the root artifactId. Spring Boot
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
  generated root aggregator at intake (v2.1k: also for a single top; 2026-11-02: a lone aggregator pom is the root even when other,
  unlinked poms sit beside it — those are left out and named in the Handover); a module reached twice in the reactor is de-duplicated
  there (`reactor-duplicate-removed`). A reactor whose modules compile nothing while the tree holds the sources elsewhere (company shape:
  `APP/WEB/pom.xml` packaging a JDeveloper workspace in `DEV/`, static files in `STATIC/`) is flagged **`hollow-reactor`** at analyze
  (2026-11-02, `sbmig/analyze/hollow.py`): the run goes on but is not a success (exit 2, `attn=hollow-reactor`, Handover first line) —
  mapping those sources into the modules is the tree-profile increment. **Oracle ADF (JDeveloper) projects are not converted**
  (business decision 2026-11-02): `sbmig/analyze/adf.py` looks for four evidence families (BC4J XML, `oracle.jbo|adf|binding` imports,
  ADF Model bindings, ADF Faces / task flows) and the run stops at analyze with "ADF 프로젝트라 변환이 불가능합니다" only when **two or
  more** are present (`intake.adf_policy: stop`: exit 3, flag `adf-project`, "ADF sizing" + evidence in the report, no Maven / LLM
  call; one family = flag `adf-traces` + a note, the run goes on; `.jpr` names / `adf-config.xml` alone mean nothing).
  **Stray pom copies** (company run 2026-11-03, v2.1z-h): an IDE output / backup folder holding a whole copy of a module
  (`boot/bin/`: src, .project, pom.xml with the module's coordinates) is NOT a module — `sbmig/analyze/pom_parser.py
  stray_pom_copies` drops an unlisted pom whose g:a duplicates another's (nested under it or beside a reactor-reachable one)
  from every parse, the intake prunes its directory from every scan (`Workspace.ignored_dirs`), flag `stray-pom-ignored` +
  Handover line, and the structure review's invariant check restores an edit that lists such a copy (the LLM had
  registered `boot/bin` as an "unlisted module" → Maven `duplicated in the reactor`).
  A module reached twice in the reactor is de-duplicated
  there (`reactor-duplicate-removed`), and `duplicated in the reactor` never goes to the LLM (`reactor_duplicate`, 0 tries).
  **Bundled jars** (company run 2026-11-04, v2.1z-i): a jar in the tree that no pom declares (vendor crypto / SSO jars in
  `WEB-INF/lib`, no pom.properties — the IDE build saw them, Maven never did) is wired, not called missing:
  `sbmig/analyze/bundled_jars.py` scans every such `*.jar` at analyze (content-based, no folder rule; `intake.bundled_jars`) →
  flag `bundled-jars`, journal `bundled_jars`, `logs/bundled-jars.json`; when a build error needs one, the zero-LLM pre-try
  `sbmig-bundled` wires it (`sbmig/restructure/system_jars.py wire_bundled_jar`): a plain dependency when its own pom.properties
  coordinates are in the repository set, else copied to `<module>/lib/<name>` and declared `system` scope at
  `${project.basedir}/lib/<name>` (placeholder coordinates, never guessed) + `includeSystemScope` on the boot module's
  spring-boot-maven-plugin; the Handover asks the owners to publish the jar to Nexus and replace the entry. A bundled jar whose g:a the target
  parent chain manages (an old spring-webmvc 4.x in WEB-INF/lib) is **superseded**: never wired, the LLM is told not to
  re-add it (user, 2026-11-04: wiring every jar found would be an anti-pattern). **v2.1z-j (same day, the user's direction):**
  the restructure wires the bundled jars BEFORE the first build (`layout.wire_bundled_jars`, `sbmig/restructure/bundled_jars.py`:
  the Ant-intake rule — known coordinates the repository has → compile dependency, else lib + system scope; superseded /
  container-API / unreferenced / already-declared jars untouched and named), because runtime-only jars (drivers, filters,
  taglibs) never cause a compile error; the pre-try is the safety net. **v2.1z-k:** an as-is version property no pom
  references (`java-version=1.8`, `org.springSecurity-version=4.2.7.RELEASE` — spellings the name rules miss) is removed by
  value (`orphan_version_properties`, only with chain facts); a blocked javax item names its EOL advisory + replacement, and
  a library that arrived as a bundled jar matches advisories too (spring-mobile: `knowledge/notes/spring-mobile.md` tells the
  LLM to write the compat replacement — never a bytecode transform). **v2.1z-l:** an as-is `<dependencyManagement>` entry
  copied into a role-parent module shadows the imported Boot BOM (own entries win) — `spring-security-*:4.2.7` over 7.x —
  so the version cleanup removes own management entries the chain / imported BOM manages at an equal or newer line, and the
  review calls a Spring-family pin on another line critical. The LLM is told
  the fact ("the project bundles `<jar>` which provides package `<p>`") and a coordinates / missing-library decline while a
  bundled jar provides the package is rejected (the company run had ended BLOCKED `needs_owner`). Fixture `webapp-bundled-jar`.
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
  `sbmig kb cases list` / `sbmig kb cases show <case_id>`.
- EOL / unmanaged libraries (springfox, hystrix, sleuth, old Spring Cloud trains, …): **no `block` policy, ever**
  (user: "정책상 빌드로 보지 않는건 없을거야" — 500 projects cannot all be perfect). Advisories are guide-only in v2:
  hints in the script prompt + the report **Handover** section first.
- v2 decisions (user, 2026-10-26, `docs/v2/PLAN-v2.md` §2): recipes = `UpgradeToJava21`, `JavaxMigrationToJakarta`
  (when the case needs it), Boot projects `UpgradeSpringBoot_4_0` + `SpringBootProperties_4_1` (that umbrella already runs
  Spring Cloud 2025.1 / Security 7 / Framework 7), and since v2.1l (2026-10-30, "security 는 쓰는 경우만") the Spring Security
  chain `UpgradeSpringSecurity_6_0 … 6_5, 7_0` for a `spring_legacy` project whose analyzer found `spring-security`, and since
  v2.1o `UpgradeSpringFramework_7_0` for every `spring_legacy` project (the Boot 4.1 BOM brings Spring 7 / Jackson 3 anyway); no intermediate
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
(`transform.rewrite`, `restructure.*`, `restructure.review`, `verify.build`) → **transform** (one OpenRewrite run, no build; v2.1's `transform.compile` only with
`plan.compile_after_rewrite: true`, off since v2.1o) → **restructure** (company layout exactly as v1, optional bump inside) → **structure review** (LLM checks
poms / dependencies / modules / source encodings / binary resources under filtering against the company rules;
critical → one fix script, committed `review: …`; warnings → report + `kb-v2/reviews.jsonl`) → **verify**
(`mvn clean package -Dmaven.test.skip=true`); on failure the **script fix loop** (`loop.phases.verify`, 12 rounds):
errors grouped per **case** (L2 signature; coarse `build` / `other` keys refined from the javac line), root causes
first, cases whose files / module pom a script already touched this round wait for the rebuild; per case
**reuse** the best stored script (no LLM) → **augment** this run's last script (LLM, ≤ `script.max_augments_per_case` 15)
→ augments used up: a fresh **new** (LLM) → BLOCKED `repeated_failure` at `script.max_llm_tries_per_case` 20 asks (v2.1z); every attempt is committed, scored by the rebuild (round-10 rules: improved / partial
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
  (`logs/effective-depmgmt.json`) inside the restructure, before the first build (`RestructureResult.versions_pinned`, Handover ⚠
  "carried over"); the fix loop fills a version-less dependency the LLM added (as-is build → newest repository release) and
  tries a `missing_version` case first without the LLM (`sbmig-version`); rule 14 tells the LLM to leave unknown versions
  `null`. v1's `carry-over-managed-version` handler had been deleted with the other handlers in v2 step 4 — that gap was the
  company's "정책상 버전을 추정하여 추가할 수 없습니다" loops. Contract: `docs/INTERFACES.md` "v2.1l".
- **Binary files under Maven resource filtering (v2.1m, 2026-10-30)**: the restructure writes maven-resources-plugin
  `nonFilteredFileExtensions` (the extensions of the binary files the analyzer's scan finds under filtered resources — known
  table OR binary content, so a company `.dmdb` needs no configuration; `build.non_filtered_extensions` is only for formats that
  decode as text) into the pom that declares the filtering, before the first
  build (`sbmig/restructure/resources_filtering.py`; v1 handler E12 restored as a rule); the loop tries a
  `MalformedInputException` / `Input length = 1` case first without the LLM (`sbmig-resources`). Contract: `docs/INTERFACES.md` "v2.1m".
- **Optional artifacts by trigger (v2.1n, 2026-10-30)**: an optional in-house artifact with `when_present` (g:a keys / globs) is
  ADDED to a pom that declares one of those dependencies (`replaces: true` removes the matched declarations — the curated
  company artifact stands in for the raw library, e.g. `hibernate-core` → the company JPA starter); without `when_present` an
  optional is still only aligned when already used (S6). Contract: `docs/INTERFACES.md` "v2.1n".
- **No build before the restructure; parent-chain facts (v2.1o / v2.1p, 2026-10-30)**: `plan.compile_after_rewrite: false` is the
  default (recipes run on the as-is classpath, the restructure cleans what they leave in the poms); when the post-restructure
  `help:effective-pom` capture fails, the managed versions and the property names come from the configured company parents' own
  poms read from the repository (`sbmig/build/parent_chain.py`, their `<parent>` chain and BOM imports — the Boot BOM included); as-is
  version properties that override the chain's (`spring-boot.version=2.5.15` …) are removed, dead properties and recipe-raised
  versions dropped. `layout.parent_property_names` is an escape hatch only (default empty). Contract: `docs/INTERFACES.md` "v2.1o" / "v2.1p".
- **MSA with non-Boot WAR services** (2026-10-30): `restructure.msa_pom` gives such a service the boot parent but no Boot app /
  layout split; the user checks the real projects first ("비-boot war는 일단 그대로 두자").
- **JSP `javax` → `jakarta` (decided 2026-10-28, v2.1h)**: OpenRewrite never parses JSPs, so the restructure renames
  the javax EE names of the analyzer's table inside every JSP / tag file's code regions after the files reached their
  final place, in every mode (`layout.jsp_jakarta_rename`, default true; `sbmig/restructure/jsp_jakarta.py`). Off →
  Handover follow-up only. Taglib URIs stay (JSTL 3 serves the `java.sun.com` URIs).
- Guide advisories (hystrix, ribbon, zuul, …) leave compiling code as it is; the Handover lists them.
- javax-era libraries (v2.1z-e, 2026-11-02): sbmig looks for the jakarta build itself — the repository SET (explicit
  `build.repository_url` first, then every settings mirror by Maven's `mirrorOf` rules, pom repositories unless mirrored,
  the global settings, Central), the same coordinates' higher line (`2.0.5-jakarta`, verified by the version qualifier /
  pom / a bounded jar scan), the table's `jakarta_key`, Nexus search by stem, sibling-name probes — and the zero-LLM
  pre-try `sbmig-jakarta` swaps the declaration before any LLM call (a higher major is the one justified major change,
  ⚠ for the operator). `sbmig repo-probe g:a:v` shows the URLs asked and what each answered; `journal.jsonl` has
  `library_lookup` / `library_unmapped`. Unreadable remotes are said so (`remote repositories unreadable: …`).
- Ladder caps (v2.1z, 2026-11-02, "여유롭게 잡고 … 점차 줄여가는 방식"): ≤ 20 LLM asks per case and run
  (`script.max_llm_tries_per_case`), of which ≤ 15 augments (`script.max_augments_per_case`); to be lowered once the
  company runs show what a case needs. A case the LLM declines (legacy-ibatis) blocks after `script.max_declines_per_case`
  (1 call). `loop.blocked_policy` gives javax libraries / missing artifacts / plugins 2 script attempts (a stored
  script is reused regardless). The v2.1i switches (`fix.mode: edits`, `--no-kb`) were removed in v2.1z.
- KB matching (review 2026-11-02, not built): case id = hash of the L2, so a `signatures.py` / `triage/cluster.py` rule
  change orphans stored cases (`kb reindex` never re-derives the L2; proposed `kb reindex --resign` from the stored `raw`
  + old-id alias); `compile|cannot_find_symbol|<simple name>` merges problems sharing a method name — fix only after
  `--resign`. Reactor artifactIds are masked as `<module>` in L1 / L2 since v2.1z.
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
  **v2.1z-m (2026-11-04)**: the company's GPT agent gateway is `llm.gateway: agent` (`LLM_GATEWAY`): the payload is
  `{model: <LLM_ASSET_ID>, messages, stream: false, async: false, params: {role: <LLM_MODEL / LLM_ESCALATION_MODEL>},
  session_id, trace_id, max_tokens, temperature}` — the role names (`build_error_fixer`, `escalation_fixer`) are what the
  gateway maps to its models (gpt-5.5 / gpt-5.6-sol); `LLM_TOKEN_URL` + `LLM_APP_TOKEN` (else `LLM_API_KEY`) drive the
  token exchange (`TokenManager`: refreshed 5 min before expiry, once more after a 401). No `thread_id` until confirmed;
  no `session_id` is sent either (v2.1z-n) — the gateway creates one and returns it (`LLMResponse.session_id`). An EOL-library
  replacement by project code (Spring Mobile) is a `<no_script>` + `<edits>` answer capped by `script.max_edit_tries_per_case` (4),
  recorded as edits, never a stored script; obsolete Spring artifacts (spring-dao …) are removed, never version-pinned.
  **v2.1z-o:** `thread_id` is deprecated (never sent); a call that passes `metadata["session_id"]` continues that gateway session,
  one without it opens a new one — the case-thread increment's transport (`FixAttempt.llm_session_id` keeps the id; the mode
  itself waits for the company's answers on delta-vs-history, TTL and billing).
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
