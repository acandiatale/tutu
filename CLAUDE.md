# sbmig — working notes for Claude (read this first)

In-house **JDK 21 + Spring Boot 4.1.0 migration agent** for company Maven projects. Python 3.10+, CLI `sbmig`.
This branch (`flow-v2`) is **v2**: one OpenRewrite run, the company restructure, an LLM structure review, then
`mvn clean package`, and every build error fixed by an **LLM-written Python fix script** stored per error case and
reused on later projects. The v1 flow (deterministic handlers, KB recipes, SEARCH/REPLACE patches) is the git tag
**`company-v1`** on `main`; its code is deleted here (PLAN-v2 step 4).
Read in this order: `README.md` → `docs/v2/PLAN-v2.md` (the business request, the decisions, the step table) →
`docs/migration-agent/README.md` (the design in one place: Mermaid per flow / node, Korean; `migration-agent.pptx`
beside it, rebuilt by `scripts/handbook/build_migration_agent_deck.js`) → `docs/INTERFACES.md` (contracts of the deployed
system: a topic → section map on top, "Base contracts" = what v2 still runs from before, then one section per increment) → `docs/PLAN.md` (one "Status" table
per round, newest at the bottom) → the doc for the area you touch (`ARCHITECTURE`, `ERROR-HANDLING`, `FLEET`,
`ADAPTERS`, `RECIPES`, `ADVISORIES`, `RUNBOOK`).
**Docs describe the deployed flow-v2 system only (user, 2026-11-11: "v1 시절거는 문서 필요 없어! 현행 버전만 중요해!")**: the
v1-era handbook (`docs/handbook/`, its xlsx / pptx, scripts and test) and the dated review snapshots under `docs/v2/` were
deleted, every `docs/*.md` was cut to present behaviour (v1 history = git tag `company-v1`). State what the code does now;
no "round N / formerly" narratives, no dated change notes appended to a doc. `docs/PROGRESS.md` is the Korean checklist of
what is done (validation level per area, open items): keep it current when a larger piece of work lands.
`scripts/handbook/` builds only the flow deck and the KB map. The KB data map for the business
team is `docs/kb-map/` (`kb-map.html` source, `index.html` single file, `kb-map.md`; `scripts/handbook/build_kb_map.py`).
**`agent-doc/` (2026-11-04)** is the Korean document pack the company feeds to its in-house LLMs (Sonnet / Haiku / GPT
class) so other engineers can diagnose runs and change the code with them: README = index + rules for the LLM, 00…11 one
topic each, ≤ 350 lines / 30 KB per file, no version tags or dates except in 10; `tests/docs/test_agent_doc.py` checks
paths / config keys / CLI names / links / sizes. Update it with the code. The comments of `config/*.yml`
are Korean explanations for a human deciding a value (not technical notes, no version tags); keys / values are the contract.
Record operator-visible changes (CLI, config keys, report sections, log lines) in `docs/PLAN.md` / `docs/INTERFACES.md`
and keep `tests/docs` green.

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
- Maven projects only. **Ant projects are refused since 2026-11-12** (company decision, the user: "ant -> maven 변경하는거
  안하기로 했어 … 삭제는 하지말고 끊어놨다가 나중에 또 하자고 하면 살리기만"): `intake.ant_to_maven` is `false` by default and
  the abort reason is `intake.ant_message` ("Maven 변환 후 신청 부탁드립니다.", exit 3, flag `unsupported-build-tool:ant`).
  The v2.1j code is KEPT and runs with `intake.ant_to_maven: true` (the `ant-webapp` fixture sets it; do not delete it): a tree
  with `build.xml` and no pom.xml then gets a pom.xml synthesized at intake (`sbmig/analyze/ant.py`: layout from build.xml, jar coordinates from the jar's pom.properties →
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
  error case (L2 signature) so later projects reuse it without an LLM call: `sbmig fleet`, `kb-v2/cases/<case_id>/`,
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
critical → edits (`review.ask_script: false`; a script only when true), committed `review: …`; warnings → report + error list;
`review.record` / `reviews.jsonl` are dead) → **verify**
(`mvn clean package -Dmaven.test.skip=true`); on failure the **script fix loop** (`loop.phases.verify.max_iterations`, 50 rounds cap):
errors grouped per **case** (L2 signature; coarse `build` / `other` keys refined from the javac line), root causes
first, cases whose files / module pom a script already touched this round wait for the rebuild; per case
**reuse** the best stored script (no LLM) → **augment** this run's last script (LLM, ≤ `script.max_augments_per_case` 15)
→ augments used up: a fresh **new** (LLM) → BLOCKED `repeated_failure` at `script.max_llm_tries_per_case` 20 asks (v2.1z); every attempt is committed, scored by the rebuild (round-10 rules: improved / partial
/ no_change / regressed → reverted / inconclusive), rolled back before the case's next try unless it improved, and
credited to the stored version (`kb-v2/cases/<case_id>/fix_v<N>.py` + `case.json`: lineage, successes / failures / projects).
Then the final build again, the report (`report.language: en`: the diagnostic layout — Handover first, "Structure
review", fix attempts as `script:<case>:v<N>`; `ko`, the company setting: the requester's guide of v2.1z-as, below),
`errors.md` / `errors.jsonl` in the run dir and the KB (`errors.jsonl`, `cases.md`, `cases/<case_id>/`; the v1-era
`error_cases.*` / `fix_attempts.jsonl` / catalog / audit / quarantine files are no longer written). `SBMIG-RESULT … review=<verdict>` is the line operators retype.

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
- Guide advisories (ribbon, zuul, …) leave compiling code as it is; the report lists them. **Hystrix is replaced again
  (2026-11-18, v2.1z-at)**: the user had found Hystrix and a Sleuth artifact "pinned at the as-is version" in a company
  report while Springfox was replaced, and after hearing why (the round-6 in-house recipe was deleted with the v1 flow;
  the as-is version is carried over, so the old jar compiles and the build is green although Hystrix does not work on
  Boot 4) decided: "이게 동작하지 않는건 되살려서 동작하도록 해주는건 맞는거 같아!" Recipe set `hystrix_resilience4j`
  (`knowledge/recipes/base/hystrix-to-resilience4j.yml`, composite `com.company.migrate.HystrixToResilience4j`), planned
  when the analyzer finds `hystrix`, before the Boot / Framework sets in the one OpenRewrite run; it writes NO version
  (the upgraded Spring Cloud BOM, or the restructure's rules, decide); code it cannot port (`extends HystrixCommand`)
  reaches the fix loop with the `## fix-loop: hystrix` note; advisory `hystrix` ends `replaced`. Proven on the real
  pipeline with `boot2-cloud-legacy` and `boot2-hystrix-command`. Springfox and `spring-cloud-starter-sleuth` are
  replaced by the Boot 4 umbrella; other Sleuth-family artifacts are not (the company report's artifact name is still
  unknown). My reading of the principle, to confirm before widening it: a library that cannot work on the target and
  has a mechanical replacement should be replaced, not carried over.
- javax-era libraries (v2.1z-e, 2026-11-02): sbmig looks for the jakarta build itself — the repository SET (explicit
  `build.repository_url` first, then every settings mirror by Maven's `mirrorOf` rules, pom repositories unless mirrored,
  the global settings, Central), the same coordinates' higher line (`2.0.5-jakarta`, verified by the version qualifier /
  pom / a bounded jar scan), the table's `jakarta_key`, Nexus search by stem, sibling-name probes — and the zero-LLM
  pre-try `sbmig-jakarta` swaps the declaration before any LLM call (a higher major is the one justified major change,
  ⚠ for the operator). `sbmig repo-probe g:a:v` shows the URLs asked and what each answered; `journal.jsonl` has
  `library_lookup` / `library_unmapped`. Unreadable remotes are said so (`remote repositories unreadable: …`).
  **v2.1z-aa (2026-11-06, company run spring-mobile)**: a library whose `knowledge/notes` note carries `replacement: true` (sbmig knows the
  fix is project code) is never blocked as `external_javax_api` / `external_api_incompatible`, and the LLM's decline on it is rejected and
  re-asked; a rejected decline that still carries edits is applied, not discarded. The note is matched against the javac text plus the
  import lines of the errors' files (`note_text`).
- Ladder caps (v2.1z, 2026-11-02, "여유롭게 잡고 … 점차 줄여가는 방식"): ≤ 20 LLM asks per case and run
  (`script.max_llm_tries_per_case`), of which ≤ 15 augments (`script.max_augments_per_case`); to be lowered once the
  company runs show what a case needs. A case the LLM declines (legacy-ibatis) blocks after `script.max_declines_per_case`
  (1 call). `loop.blocked_policy` gives javax libraries / missing artifacts / plugins 2 script attempts (a stored
  script is reused regardless). The v2.1i switches (`fix.mode: edits`, `--no-kb`) were removed in v2.1z.
- KB case identity (v2.1z-ai, 2026-11-12, the user: "정확도를 올려놔야 나중에 머리 안 아플 것" — the KB goes into a DB later):
  the case id stays `sha1(L2)[:10]` (a hash of the WHOLE L2, so a longer id would not match better). `SIGNATURE_VERSION` (2)
  names the signature rules; a rule change that alters a produced L2 raises it and lists its error classes in
  `SIGNATURE_CHANGES`; replay fixtures keyed `case: <id>` and `expected.json` ids move with it. Rules 2: a missing MEMBER
  (method / field / constructor) of a library-owned type carries the type (`…|cannot_find_symbol|org.springframework.http.
  HttpHeaders.keySet`, `…|no_suitable_method|<T>.<name>` from the candidate line); class symbols keep the bare name (import
  line + usages = one case). Every entry stores its signature facts (`sig`, `l1`, `symbol`, `location`, `artifact`, `goal`).
  The KB re-signs records of older rules (`sbmig/errors/resign.py`, `CaseStore.resign`: only the changed classes are
  recomputed; cases and scripts move, old ids stay in `aliases` / `was`; `<kb>/kb.json`) — by `sbmig kb reindex
  [--dry-run]`, on a process's first `kb.match` when `kb.json` is older, and inside `record_run` when an older-image pod
  wrote later. Exact-key guard: a stored case whose L2 is not the L2 asked for is never reused or written. Still merged by
  name / phrase only (to decide from real company cases): members of project-owned types (`…|cannot_find_symbol|save`),
  syntax errors (`';' expected`), `unreported exception <type>`. Reactor artifactIds are masked as `<module>` in L1 / L2.
  Scopes (v2.1z-ap, three-pods review): a version and each credited run record their scope (`full` / `jdk-jakarta`, `""` =
  full); reuse ranks (same scope, proven, version) with at most one other-scope version per case and run, counters / status
  count only the version's own scope, a framework-guard rollback of another scope's version is `out_of_scope` (not a
  failure), and `credit` / `add_script` on an id a re-sign moved follow the `aliases` (INTERFACES "v2.1z-ap", **KB.**).
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
- Fast check (Python only): `python -m pytest` → ≈ 6,230 passed, 62 deselected, **about 2 minutes** (2026-11-18). It runs
  in parallel by default (pytest-xdist: `addopts = "-n auto --dist load -m 'not integration'"`, at most 8 workers — the
  root `conftest.py` caps `auto`); `-n 0` is the serial run (≈ 10 min), `-m integration` replaces the marker filter. After
  pulling, `pip install -e ".[dev]"` once more: without pytest-xdist pytest refuses the `-n` option. Do NOT pass
  `-o addopts=""` any more (it switches the parallel run and the marker filter off). The serial run took 16 minutes
  until 2026-11-18; an audit found that the time was git, not slow tests: 26,000 git processes per run, and macOS's
  `/usr/bin/git` is the Xcode `xcrun` shim, three times slower per call than the binary behind it. The root conftest
  now puts a private folder holding only a symlink to the real git first on `PATH` (the header's `git:` line says
  which git is used), and a workspace's git calls switch automatic maintenance off (`maintenance.auto=false`,
  `gc.auto=0` in `Workspace.GIT_CONFIG`, also in production). The same audit removed 66 tests that another test
  covers or that checked code that no longer exists, and repaired tests that could not fail; a test that pins a bug
  from a company run or a review stays even when it looks redundant. Until 2026-11-12 `tests/build` (381 tests) was silently
  left out of every root-level run — pytest skips a directory named `build` by default; `norecursedirs` in pyproject now
  keeps it in. When a total does not grow by the number of tests added, compare per-file counts (`--collect-only -q`).
  `ruff check sbmig tests` has pre-existing style findings (mostly `UP045`); lint is not a gate here, so fix only what
  you touch and never run a repo-wide `ruff --fix` inside a feature round.
- **The Korean report.md is a guide for the requester (2026-11-18, v2.1z-as).** The user, after reading a company
  report: "너무 기계적으로 리포트가 나왔다"; its purpose is "마이그레이션을 요청한 담당자한테 우리 이렇게 마이그레이션 했고
  이런거 확인해주세요~ 라고 가이드"; no error lists, no per-round fix tables ("굳이 fix r1,r2 이렇게 보여주지 않아도
  되구"), the modified files with their final diff; head table = 결과, SBMIG-RESULT, 전환 후, 실행 ID, 시작 / 종료, 플래그
  only; the words 사유, 관리됨 and "as-is 버전으로 고정했다" were named as odd. So `report.language: ko` renders
  `sbmig/report/guide_ko.py` (Korean written directly: 전환 내용 → 확인해 주세요 → 코드 수정 내역 → 해결하지 못한 오류 →
  진행하지 않은 항목 → special sections → 참고), `en` keeps `markdown.py`'s diagnostic layout byte for byte
  (`tests/report/golden/`). `RunReport.code_changes` (`sbmig/report/changes.py`) = the NET diff per file between the
  commit right after the automatic conversion and the final tree. Advisories carry `*_ko` texts
  (`knowledge/libraries/advisories.yml`); the fix / review prompts ask for their explanations in the description
  language. When Korean text for a reader is added anywhere: write it as a Korean colleague would (합니다체, no
  word-for-word code vocabulary), render a sample and read it top to bottom before shipping. A new report section
  must be added to BOTH renderers. Contract: `docs/INTERFACES.md` "v2.1z-as".
- **The code-change view (2026-11-18, v2.1z-au).** Relayed by the user: a GitHub / Bitbucket style view of the
  changed source in the result card ("지금 승인쪽 빠졌으니까 거기에 섹션하나 잡고 … 나중에 모달로 분리하기 쉽게"), for
  restructured projects shown by the converted tree with where each file came from ("어디 있던 java 파일이 이 주소로
  왔다도 표시"), with lines above and below a change ("수정분만 보여주지 말고 위 아래 코드들도 좀 보이도록"). Three
  parts: `sbmig/report/codediff.py` writes `<run dir>/codediff/` at finalize (every final file at its final path
  against its ORIGIN in the as-is tree: same path → same name and identical bytes → same name and half of the lines
  shared; no git rename detection, a base tree with the origins' blobs at the final paths and one `git diff`);
  `sbmig/web/codediff.py` serves it and reads more lines of the final file out of `result.zip` (the web pod runs no
  git); the page block 「코드 변경」 is the component `CodeDiff` (`mount(host, runId)` / `unmount()`, styles under
  `.cdiff`) so that a dialog can host it later. Not in the view: the chained remediation's pom changes; no block for
  a remediation-only run; no side-by-side view. Contract: `docs/INTERFACES.md` "v2.1z-au".
- **Image pulls (2026-11-18).** The company pushes the same image tag every time; with `IfNotPresent` each node kept
  the build it had pulled first (the approval list of v2.1z-ar "came back" because the build pods ran old code). The
  manifests now say `imagePullPolicy: Always`; after a push the operator restarts the web pod, build pods pull at
  start. When a deployed change "does not arrive", ask which image the BUILD pod runs before anything else.
- **The whole-code review (2026-11-18, v2.1z-av).** The user asked for an inspection of everything ("문제될 사항들이
  보이는지 비어있는곳이 있는지"). Seven read-only reviewers by area plus one for what nobody had reached; 51 findings,
  each reproduced before it was reported, fixed test first on disjoint files (`docs/INTERFACES.md` "v2.1z-av" lists
  every changed behaviour). What to carry forward:
  - Rules that changed and are easy to get wrong again: at the end of the fix loop the last try that did not help
    (`no_change` / `inconclusive`) is rolled back, a partial one stays; a stop request is ignored once every planned
    final build has a result; a transfer failure is `repository_unreachable` (never "not found", never an LLM try);
    managed versions are judged per company parent (`ParentChainFacts.managed_for`), not on the merged set; the
    remediation planner never jumps to a major the scanner did not name, never bumps a version that already meets
    the hint, never changes a property several artifacts share unless the target exists for all of them.
  - Main and Worker: `sbmig_version()` carries a digest of the running code, and the configuration digests and the
    version are pinned when the Job is CREATED (not at upload). After pushing an image the web pod must be
    restarted; a run uploaded in between is refused with a sentence that says so.
  - Where the gaps were: borders between two people's code (a field written on one side and never read on the
    other, a flow or mode a change forgot, a report sentence naming a file nobody writes, a config key documented
    for a path that does not read it). When a feature spans parts, check both ends of each border before calling
    it done, and render what the reader will see.
  - Left to the user: a second web pod is not detected (`replicas: 1` + `Recreate` in 40 are the control, the user
    manages it by yaml); retention of old run folders ("회의 나눠보기로 해서 아직 안나왔구"); build leftovers outside
    `target/` in `result.zip`.
- **A project's own Maven settings are ignored (2026-11-18).** A company project carried `.mvn/settings.nexus.xml` and a
  `.mvn/maven.config` pointing at it; the builds asked only the Nexus `maven-central` repository and the remediation
  refused the project. The user: "우리도 프로젝트 안에 있는 주소는 무시하도록 하면 안될까? 굳이 해당 설정에 막히는건 좀
  아닌거 같은데". Measured with real Maven 3.9.10 before deciding anything: a `-s` in `.mvn/maven.config` loses to our
  `-s`; a `-gs` there is read as the global settings and merged with ours (the hole); a `-D` there loses to ours. So
  every Maven command passes `-gs <maven home>/conf/settings.xml` beside `-s` (`build.ignore_project_settings`, default
  true), the migration notes it once (flag `project-maven-settings-ignored`, one hand-over line), and
  `remediation.project_maven_config` defaults to `ignore`. The project's file is never edited. **The same evening the
  whole folder went out of the way** (the user: "프로젝트에 .mvn이 있으면 복사파일에는 없앨까? 운영 빌드 되도록만
  맞춰주면 될거같은데 나중에 압축할때 동봉만 해주면 될거같고"): measured first — a JDK 8 `.mvn/jvm.config` stops the
  JVM on JDK 21, a 3.8-style `maven.config` stops Maven 3.9, and `MAVEN_BASEDIR` pointing at an empty folder makes
  Maven read none of it — so nothing is deleted or put back: every Maven child gets `MAVEN_BASEDIR=<empty folder>`
  (`build.ignore_project_mvn`, default true), the `-D` / `-P` options of the project's `maven.config` and
  `-Dmaven.multiModuleProjectDirectory=<project root>` go onto the command line (`with_project_mvn`), flag
  `project-mvn-ignored` and one hand-over line, and `result.zip` holds `.mvn/` byte for byte. Not checked: `mvn.cmd` on
  the company's Windows PC, a temp path with a non-ASCII user name. Lesson worth keeping: before designing around a
  tool's behaviour, run the tool (three five-line experiments decided this design, and one of them overturned the plan
  to "set the folder aside and put it back").
- **Disk walls for test runs (2026-11-17)**: a review subagent's scratch test (a writer thread with no size or time
  limit) wrote one 118 GB file and filled the developer's disk. A rule in a brief is a promise; these are the walls.
  The root `conftest.py` gives every pytest run whose tests are under the repository a per-file size cap
  (`RLIMIT_FSIZE`, 1024 MiB: the writer gets `OSError: File too large`), a free-space floor (5120 MiB) and a cap on
  the run's own basetemp (4096 MiB); the last two end the run with exit 125 and `bounded: STOPPED: <reason>`. Env
  `BOUNDED_FILE_MB` / `BOUNDED_FLOOR_MB` / `BOUNDED_GROWTH_MB`, `0` = off. The run's header prints a `bounded:` line;
  **a pytest run without that line has no wall** (its tests are outside the repository), so scratch tests live under
  `out/scratch/<topic>/` (ignored by git), never in a session scratchpad or `/tmp`. Everything else (a scratch
  script, a demo run, a test that must live outside) runs through
  `.venv/bin/python scripts/bounded.py [--file-mb N] [--seconds N] [--tmp-mb N] -- <command>`: the same caps plus a
  wall-clock cap (default 1800 s), the whole process group is killed, the command's `TMPDIR` is a folder the wrapper
  measures and removes; exit 124 = time, 125 = disk, 126 = refused to start. The walls bound the amount, not the
  judgment: a scratch run that timed out or was stopped is never started again before looking at what it wrote, and
  every brief still says so.
- **Korean handover comments (2026-10-28, the business team's request; rewritten 2026-11-12 after the same team said
  the Korean "너무 ai티 난다")**: every module under `sbmig/` starts its docstring with a Korean block and every public
  top-level class / function has Korean first in its docstring, the English original kept unchanged after it. The Korean
  is written like a Korean developer's own comment — SHORT (module 2–6 lines, function 1 line, inline 1 line = the
  reason), "~한다." endings, particles attached (`KB에`), identifiers verbatim, and never an em dash, arrow, bold,
  version tag (`v2.1…`), date, review / round label or the story of how a bug was found; that history belongs to the
  English part (the working record) and the commit message. Verbs 호출한다 / 실행한다 / 기록한다 / 예외를 던진다, not
  부른다 / 돈다 / 적는다 / 올린다. Convention, glossary and examples: `docs/v2/CODE-COMMENTS-KO.md`;
  `tests/docs/test_korean_comments.py` fails for a new module or public definition without Korean and for a Korean
  comment line with one of those tells (English lines are not checked). The page's comments and `config/*.yml` comments
  follow the same wording; `deploy/` comments were left as they are (the company keeps hand-filled copies of the
  manifests). Prompt `.md` files, tests and `coding/` stay English.
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
  trace_id, max_tokens, temperature; session_id only to continue a session}` — the role names (`build_error_fixer`, `escalation_fixer`) are what the
  gateway maps to its models (gpt-5.5 / gpt-5.6-sol); `LLM_TOKEN_URL` + `LLM_APP_TOKEN` (else `LLM_API_KEY`) drive the
  token exchange (`TokenManager`: refreshed 5 min before expiry, once more after a 401). No `thread_id` until confirmed;
  no `session_id` is sent either (v2.1z-n) — the gateway creates one and returns it (`LLMResponse.session_id`). An EOL-library
  replacement by project code (Spring Mobile) is a `<no_script>` + `<edits>` answer capped by `script.max_edit_tries_per_case` (4),
  recorded as edits, never a stored script; obsolete Spring artifacts (spring-dao …) are removed, never version-pinned.
  **v2.1z-o:** `thread_id` is deprecated (never sent); a call that passes `metadata["session_id"]` continues that gateway session,
  one without it opens a new one — the case-thread increment's transport (`FixAttempt.llm_session_id` keeps the id; the mode
  itself waits for the company's answers on delta-vs-history, TTL and billing). **v2.1z-p (same night):** the session
  remembers the system prompt and earlier turns → `llm.case_thread: true` makes a case's augments continue the base attempt's
  session with the delta only (`LLMFixer.build_thread_turn`), context / feedback rounds send one message in the session, a
  rejected continuation falls back to the full prompt; every turn logs `sent chars → prompt_tokens` (billing is undocumented —
  measure it). Default off; the replay fixtures run stateless as before. **v2.1z-x (2026-11-05):** an open session cannot
  change its model (user) → the model a session runs on travels with its id (`ScriptProposal.session_model` →
  `FixAttempt.llm_session_model` → `ScriptBase.session_model`) and an augment continues the base session only on the same
  model; the escalation rung (third ask / stall) and the way back open a new session with the full prompt.
- Logs: every `sbmig run` writes `<run dir>/sbmig.log` (DEBUG), fleets `fleet/sbmig.log`, any command takes
  `--log-file`; LLM request/response records go to `llm.record_dir`. The user shares these files to report problems;
  on the company's Windows PC without copy-paste, ask for specific lines (`SBMIG-RESULT`, `preflight: FAIL`, `cause=`).
- `coding/` is a standalone coding agent over the same gateway contract (`coding/README.md`), not part of `sbmig`.
- **Containers / Kubernetes (2026-11-04)**: `deploy/` — one image (`deploy/docker/Dockerfile`: Python + JDK 21 + Maven, code at
  `/app` on `PYTHONPATH`, never `pip install`ed into site-packages because `config/` / `knowledge/` sit beside `sbmig/`), k8s
  manifests for two operating shapes — the web-UI agent (PVCs, ConfigMap with the company yml using container paths, Secret =
  `.env`, Deployment with 1 replica) and the infra team's one-Job-per-request model (`deploy/docker/run-job.sh` runs the whole
  migration in the Job pod on an emptyDir and pushes the project branch `sbmig/<run id>`, the report and the git-hosted KB out
  before exiting; `80-build-job.template.yaml`, `submit-job.sh`). The exec-into-a-builder layout was removed (history: INTERFACES
  v2.1z-r/s); `build.setup_command` / `teardown_command` remain generic hooks. **2026-11-05: the web pod can dispatch Jobs itself** (`sbmig serve --mode k8s-job` /
  `SBMIG_WEB_MODE=k8s-job`): upload → `/data/out/<run id>/source.zip` on the shared RWX PVC → Job from `81-web-job.template.yaml` →
  the pod runs `sbmig run --run-id` into the same run dir, the web tails its journal / log (engine `graph_node_end` lines → UI `node`
  events, a '파드' step first), `stop.request` stops it between nodes; `sbmig/web/k8s.py` is a requests-based in-cluster client
  (no `kubernetes` wheel). Up to `SBMIG_JOB_MAX_PARALLEL` Jobs at a time since v2.1z-ae (below); the remediation-style `/api/migrations` is next. Comments in `deploy/` are Korean (the company's
  reviewers asked). `deploy/README.md` is the operator text. **2026-11-09 (the infra team's agent model, their temporary manifest)**: `K8sClient.connect()`
  uses the kubeconfig named by `KUBECONFIG` when present (the infra team mounts a kubeconfig Secret at `/etc/kube/config`; token / client-certificate
  users, `exec` refused) else the in-cluster service account; an explicit `KUBECONFIG` also keeps `k8s-job` from degrading to `local-job` on a dev PC.
  The web pod reads the Job template from ITS OWN IMAGE (`/app/deploy/k8s/81-web-job.template.yaml`, the code's default; since 2026-11-18 —
  the user had edited 81 and rebuilt, and the build pods kept the old setting because 40 pointed at a ConfigMap `sbmig-job-template` that has
  to be re-created by hand: "우리 이미지안에 내용을 읽게 하려고 81번 Dockerfile 만들때 복사 뜨는거 아니었어?"). The company builds from a checkout in
  which 81 is filled; a template that still carries `CHANGE_ME` stops the web pod at startup (`MIGRATION_TEMPLATE_INVALID`). The ConfigMap
  route (`SBMIG_JOB_TEMPLATE` + a mount) is the commented alternative in 40. `.dockerignore` drops only the filled `30-secret.yaml`. Company values in the manifests: namespace `agent`,
  `nfs-csi` ReadWriteMany, the image name from the infra team's registry (per push). Real company hostnames / IPs never go into the repo — placeholders. **v2.1z-ac (2026-11-09, the deployment team's spec built here — their own
  AI-written Main/Worker stays parked on a company branch, unverified)**: `sbmig/web/contract.py` is the Main ↔ Worker file contract (`control/request.json`
  pins zip sha256 / config digests / sbmig version at intake, `control/state.json` is Main's atomic control state with intent-before-create and an
  owner token, `job-status.json` carries run id + attempt); `sbmig/web/worker.py` (`python -m sbmig.web.worker`, the Job command) verifies the pins, runs
  `sbmig run`, redacts secrets (`sbmig/web/redact.py`), turns raw LLM records off unless `SBMIG_WORKER_LLM_RECORDS=1`; `RunManager` validates the template and
  its RBAC at startup (`validate_worker_template`, `can_i`), adopts an existing Job on 409 / transport errors, recovers queued / running runs after a restart,
  deletes only Jobs carrying its own `sbmig-owner` label and waits for them to vanish, and never reports an unverified result as success. **2026-11-10 (user: "일단 굽자!")**:
  the company `config/artifacts.yml` is **baked into the image** (the Dockerfile copies `config/`; the company build adds
  `--build-arg REQUIRE_CONFIG=artifacts.yml`, which stops the build when the git-ignored file is missing), ConfigMap `sbmig-config` carries
  `settings.xml` only (since v2.1z-al the source of the plain file `/data/.m2/settings.xml`: mounted at `/etc/sbmig/settings.xml` in 40 and copied once by the operator; 80 mounts it at `/data/.m2/settings.xml`), and the machine paths of a PC-tested yml are overridden by literal `SBMIG_<SECTION>__<KEY>` env lines in each manifest
  (`SBMIG_BUILD__SETTINGS_FILE` / `JAVA_HOME` / `MVN` / `LOCAL_REPO`, `SBMIG_SCRIPT__PYTHON` in 40 / 80 / 81; `SBMIG_KB__DIR` and
  `SBMIG_LLM__RECORD_DIR=${LLM_RECORD_DIR:-}` in 40 only — never in `sbmig-env`, which 80 reads by `envFrom`). The 81 template has one
  `configMapKeyRef` per `sbmig-env` key (no `envFrom` there): a new key in the ConfigMap needs its line in 81, `tests/deploy/test_manifests.py`
  pins this. `contract.is_secret_name` exempts config overrides (`…__MAX_OUTPUT_TOKENS` is not a secret); the Worker drops the record-dir
  variables from the child unless `SBMIG_WORKER_LLM_RECORDS=1`. No secret goes into the baked file. **v2.1z-ad (2026-11-10, a review of the Job
  dispatch code — four findings, all real)**: (1) `_watch_job` returns only once the Job is confirmed ended: a Kubernetes API outage never
  fails the run or frees its slot (journal `job_api_unavailable` / `job_api_back`; `SBMIG_JOB_API_GRACE_S` is a notice only), a pod
  lingering after its `job-status.json` is deleted after `SBMIG_JOB_EXIT_GRACE_S` (120); (2) a Job is ours only with BOTH the `sbmig-owner`
  and the run label — an unlabelled same-name Job is neither adopted nor deleted; (3) **env pins**: the Main that creates the Job writes
  `env_pins` (`sha256[:16]` per name: every `${VAR}` placeholder of the run's config files + every `SBMIG_<SECTION>__<KEY>` override and whole-section `SBMIG_<SECTION>`;
  secret-looking names and the record dir never; `SBMIG_KB__DIR` ↔ `SBMIG_KB_DIR` alias) and `env_pin_policy` into `control/state.json`
  with the `creating` transition, and the Worker compares its own environment (plus `SBMIG_CONFIG` / `SBMIG_STOP_AFTER` /
  `SBMIG_LLM_PROVIDER` against `request.json`) before it runs anything — `SBMIG_JOB_ENV_PIN = refuse | warn | off` on the web pod
  (default refuse in `k8s-job`, off elsewhere; refuse = exit 3 `worker_refused` naming the keys, warn = journal `worker_env_differs`).
  **Operator rule: image only → restart the web pod; `sbmig-env` (20) → apply 20, restart the web pod; 40 → apply 40; the Job
  template (81) → build and push the image, restart the web pod.** (4) redaction at write time: `SBMIG_REDACT_OUTPUT=1` (literal in 40 / 80, forced by the Worker on its child)
  hooks `sbmig/core/scrub.py` into every log / journal line (`sbmig.web.redact.install_output_redaction`; fail closed — a line that cannot
  be cleaned is withheld, the CLI exits 3 when the hook cannot be installed), the Worker redacts the child's pipe line by line, and result
  files other than `sbmig.log` / `journal.jsonl` answer HTTP 409 until the final pass is over. A `local-job` Worker takes only
  `contract.RUN_ENV` values from the template (container paths had leaked into the local run). An override value must never be left
  empty (`SBMIG_X__Y: ""` is YAML null and wipes the yml value). `LLM_RECIPE_MODEL` is forwarded by 81 like the other LLM keys.
  **Shipped defaults (2026-11-10, the user twice: "내가 다 줬는데 안채워놨엉")**: the manifests go out with what the company runs with
  switched on — `SBMIG_WEB_MODE: "k8s-job"` and `SBMIG_K8S_NAMESPACE: "agent"` in `sbmig-env`, the kubeconfig model in 40 (env
  `KUBECONFIG=/etc/kube/config`, the `/etc/kube` mount, Secret volume `agent-kubeconfig`); `45-web-rbac.yaml` + the commented
  `serviceAccountName` line are the alternative for a cluster without a kubeconfig Secret. A value or mode the user has given is
  never left as a commented "later" line; only hosts, IPs and secrets stay placeholders, and the reply lists them (file + key).
  **v2.1z-ae (2026-11-10, the user: "초반엔 무서우니까 3명까지는 빌드파드가 돌아가는 구조 … 나머지는 대기하라고 알림")**: `SBMIG_JOB_MAX_PARALLEL`
  (web pod, read at startup; code default 1, shipped `"3"` in `sbmig-env`; `in-process` is always 1 — process-global LLM counter, per-thread log)
  is the number of build Jobs at a time; `runs._FifoTurns(slots)` keeps upload order and hands out slot numbers (lowest free, from 1):
  `ControlState.slot`, Job env `SBMIG_JOB_SLOT`, re-occupied as recorded after a web-pod restart (`occupy`, over the limit allowed). The
  Worker gives slot k ≥ 2 its own Maven local repository `<base>-slot<k>` (inside `/data/.m2`; `SBMIG_BUILD__LOCAL_REPO` in the child's
  environment only, after the env-pin check; never fails a run; journal `worker_slot`) — one repository written by several Maven
  processes is the thing avoided. `status()` carries `parallel {slots, running, waiting}` and, for a waiting run, `queue {position, ahead}`;
  the page shows the place in line and a notice on the upload card when every slot is busy. The shared file KB stays one folder: every
  write goes through `<kb>/.lock` (re-read + atomic replace inside), hardened for several pods (exclusive creates for a new case and for
  `fix_v<N>.py`, lock age on the KB file system's clock, exclusive LLM record files). Not in the 81 template, not an env pin; the 80 model (`run-job.sh`, git KB) stays
  sequential. Contract: `docs/INTERFACES.md` "v2.1z-ae"; operator text: `deploy/README.md` §3 "동시에 여러 건". Not measured on the
  company cluster / NFS yet; the gateway's concurrency limit for one app token is the company's to confirm.
  **v2.1z-al (2026-11-17, the user: "/data/.m2에 뒀으면 좋겠어 일관되게")**: the Maven local repository is `/data/.m2/repository` on PVC
  `sbmig-data` (`SBMIG_BUILD__LOCAL_REPO` literal in 40 / 80 / 81, slots `repository-slot<k>` beside it); no `sbmig-m2` PVC, no `m2`
  volume in 40 / 81 (a mount nested in the NFS volume can fail under root squash), 80 mounts its own `m2` volume at `/data/.m2`.
  Right after ("Settings도 저기로 옮겨가야하지않아!?"): `SBMIG_BUILD__SETTINGS_FILE=/data/.m2/settings.xml` in 40 / 80 / 81; then
  ("configmap 에 설정되어있으면 파드 배포할때 자동으로 나가는게 아니야?", "어차피 … 웹 파드 재배포 하면 복사 필요 없는거 아냐? 만들어지는거
  아니야?"): a ConfigMap mount nested in `/data` was tried; it needed a mount point, and the initContainer that made it failed on a file
  owned by another user (`touch: … Permission denied`). Final (the user: "어차피 /data 볼륨 마운트를 하는데 … 하나 마운트 된곳에다가
  다같이 쓰면 안되는거야?", "어차피 settings.xml은 바뀔일이 거의 없잖아? 일반 파일로 두고 가는것도 좋을거 같은데!"): `/data/.m2/settings.xml`
  is a PLAIN FILE on the data volume; nothing is mounted inside `/data` in 40 / 81, no initContainer; ConfigMap `sbmig-config` is the
  reviewed source, mounted read-only at `/etc/sbmig/settings.xml` in 40, and the operator copies it once (`kubectl -n agent exec
  deploy/sbmig -- sh -c "mkdir -p /data/.m2 && cp /etc/sbmig/settings.xml /data/.m2/settings.xml"`, as the pod's user; again after
  each change + web-pod restart); no copy code in sbmig. 80 (no data PVC) keeps the ConfigMap mount at `/data/.m2/settings.xml` in its
  emptyDir. A set but missing or EMPTY `build.settings_file` aborts at intake before any Maven call (flag `settings-not-found`, exit
  3, both flows; both messages point at the copy); the web pod warns once at startup. Operator steps: `deploy/README.md` §3 "Maven 설정과 로컬 저장소".
  **v2.1z-af (same day, the user: "코드 검토 동시성 이슈 될만한거 … 찾아봐줘", five read-only reviews, every finding reproduced first)**:
  a Job run's terminal `control/state.json` is written while its slot is still held; recovery keeps the recorded slot number even when
  two runs recorded the same one (a new run only gets a number nobody holds); any failing API poll is "API unavailable"; a `Failed`
  Job whose pod is still terminating has not ended; **a stop on a waiting Job run leaves the line at once** (`_FifoTurns.withdraw`).
  The Worker handles SIGTERM (stops the child's whole process tree, Maven included, writes `job-status.json` with the reason) and
  falls back to a pod-local Maven repository (`pod_local: true`), not the shared one. KB: `_confirm_new` never overwrites a
  `case.json` the server says exists (`CaseUnreadable`, waits up to 75 s for the NFS cache), stale-lock breaking is serialized by
  `<kb>/.lock.breaker`, `kb.lock.keep_alive` refreshes the lock in long sections, `_claim_script` never replaces. LLM client:
  `llm.busy_wait_seconds` (600: a 429 / 5xx with Retry-After queues instead of failing) and `llm.auth_retries` (2: a 401 right after
  an exchange). The page re-attaches by itself after a web-pod restart. Contract: `docs/INTERFACES.md` "v2.1z-af". To ask the gateway
  team: the concurrency limit per app token, and whether a new token exchange revokes earlier tokens.
  **v2.1z-aq (2026-11-17, company cluster: a second build pod sat in `ContainerCreating` for 28 minutes)**: a stop while the Job is
  `Creating` / `Pending` deletes our Job at once (`_owned` rule, waits until gone, STOPPED, journal `job_deleted_before_start`; a
  running pod keeps the grace, a running remediation resume is never deleted). Past `SBMIG_JOB_PENDING_NOTICE_S` (300 since 2026-11-18, the user: "5분 넘게 생성 안되면 띄우도록 하자" — a healthy pod needs over 3 minutes there; 0 = off)
  Main tells the page why the pod does not start (waiting reason, `PodScheduled=False`, pod events via `K8sClient.pod_events`;
  `status().job.pending`, journal `job_pod_pending`) and never acts on it; contract `docs/INTERFACES.md` "v2.1z-aq".
- **Vulnerability remediation (2026-11-11, `sbmig/remediations/`, `/api/remediations`)**: a LangGraph flow SEPARATE from the migration
  graph. **Where it runs (user, same day: "웹파드는 안돼고 빌드 파드로 … 마이그레이션이 build 성공인 경우는 해당 취약점 노드를 타면")**: in
  the BUILD POD, chained after a migration that ended BUILD SUCCESS — the migration graph's last node `remediation` (after `finalize`)
  calls `sbmig/remediations/chain.py` when `remediation.after_migration` is on (code default false, shipped `"true"` in `sbmig-env` +
  its 81 line) and `Orchestrator.build_succeeded(report)`; files under `<run dir>/remediation/`, state id = the migration run id; the
  migration's exit code / verdict never change (`SBMIG-RESULT … remediation=<STATUS|skipped>`, one report section); skipped with a
  journal line when the IVAS values are missing; 81 reads the three IVAS keys from the Secret (optional). The web pod never builds in
  Job modes: it serves status / proposals / approvals / files for chained runs (`kind: chained`), the page shows a `취약점 조치` stage
  and a result block with 승인 / 거절 buttons; the standalone ZIP upload (`POST /api/remediations`, worker thread, own Maven repository
  `<build.local_repo>-remediation`, needs `trusted_projects`) works in `in-process` mode only (409 `not_available` in Job modes).
  A build pod cannot wait for a human: it saves `AWAITING_APPROVAL` (only when approval is switched on, v2.1z-ar) / `AWAITING_CONFIRMATION` (or `STOPPED` reason `chain_budget` after
  `remediation.chain_budget_minutes`, 90) and ends. **Resume = a NEW build pod** (same evening): `RunManager.resume_remediation` records
  the attempt in its own `control/remediation-resume.json` (action `remediation-resume`, intent before create, env pins, slot), takes a
  turn in the same FIFO line / slots as uploads, creates Job `<migration job>-rem<attempt>` with `SBMIG_WORKER_ACTION`; the Worker runs
  `sbmig remediation-resume --run-dir` (→ `chain.resume`), writes `remediation-job-status.json`, and never touches the migration's own
  files / verdict. The state on the shared volume carries the IVAS projectId / analysisId (no re-upload); an
  `AWAITING_CONFIRMATION` answer (`reupload` or `receipt` + ids) is written into state.json by the web pod before the Job exists.
  Page (user: "체크박스로 선택해서 승인 … 승인자는 필요 없고"): a checkbox list (grouped per library) + ONE apply button reading `선택한 N건 적용` → one
  `POST …/approvals` with every pending proposal (checked = approved, unchecked = rejected, `"resume": true`, no `by`) → the new pod
  continues; the word "재개" does not appear. A chained approval is checked against the policy hash the build pod recorded.
  IVAS `actionDivCd` (company document): NE 조치 필요 / NP 조치계획 미등록 / PL 조치예정 / OT 조치일 경과 / PC 패치 완료 / IG 조치 예외 —
  `remediation.ignore_actions` (default EMPTY since the user relayed that the older implementation does not filter PC / IG) lists codes
  that are reported but neither planned nor counted as remaining (part of the policy hash). `remediation.ivas_summary_check: exact`
  (default): `summary.components.total` must equal the library-vulnerability rows over all pages, else the scan is incomplete
  (`nonzero` relaxes it; the older implementation compares the same way).
  A matched-files component id may contain `|` (`11560|934982`), sent as one encoded path segment. Flow: source ZIP → `baseline_build → baseline_scan → inventory → plan →
  apply → build → freeze_delivery → rescan → evaluate → report` with the company's scanner **IVAS** (LabradorLabs; env
  `IVAS_BASE_URL` / `IVAS_USER_ID` / `IVAS_PASSWORD`, Secret only). Hard rules: only Maven **version text** changes in existing
  poms (to a version the policy or an approved repository names), plus ONE exception — a `managed_override` proposal inserts one fixed-shape `<dependency>` entry into the
  `<dependencyManagement>` of the root pom or, for a module that does not inherit from it, of that module's own pom
  (`remediation.module_overrides`, default true) for a transitive / parent- / BOM-managed library (applied on its own like every other proposal unless approval is switched
  on, v2.1z-ar); no source / structure change; plugin, vendored, unmapped → residual / manual review; original ZIP + manifest +
  `accepted/` kept apart, changes on an isolated candidate first; a failed baseline build never reaches the scan; an uncertain IVAS
  upload is never re-sent (`AWAITING_CONFIRMATION`), an incomplete scan is never "0 findings"; the frozen delivery ZIP's exact bytes are
  rescanned; approvals bind proposal hash + source tree hash + policy hash + diff (`AWAITING_APPROVAL` → approvals → resume); a restart
  turns running jobs into `INTERRUPTED` (explicit resume only). One process drives a run folder (v2.1z-ap, three-pods review):
  `remediation/claim.json` (`chain.RunClaim`, refreshed every 30 s, taken over after 600 s without refresh, a second driver
  gets `StateConflict` and touches nothing, the web pod only reads it), and a step that rewrites the candidate saves its
  decision with `next_node=apply` first (completeness marker `rounds/<n>/candidate.ready.json` guards build / freeze). `remediation.trusted_projects` (default false) must be set true by the
  operator before anything is accepted (Maven runs the project's plugins; not a sandbox); `remediation.require_approval` default false
  and `remediation.always_approve` default `[]`: nothing waits for approval (v2.1z-ar). No version is chosen without an IVAS patched-version hint AND evidence from the policy or an
  approved repository host. Contract: `sbmig/remediations/models.py` + `interfaces.py`, `docs/INTERFACES.md` "remediations"; operators:
  `agent-doc/11-remediations.md`, `deploy/README.md` §6. Tests use fakes only (`tests/remediations/`, no IVAS / Nexus / mvn). Not run
  against the real IVAS yet; no UI page. **User decisions 2026-11-11** (after comparing with the company's older implementation —
  `pipeline.py` / `self_ivas.py`, not in this repo): a candidate whose build failed IS delivered (`failed_candidate: include`:
  result.zip = the failing change set, `delivery.build_ok=false`, status FAILED, not rescanned, an earlier verified delivery kept as
  `result-verified.zip`); risky classes always need approval (the old code applied them automatically with `require_approval=false`) —
  superseded on 2026-11-18, v2.1z-ar below;
  override entries for transitives as above; upload limit 1024 MiB, analysis wait 1,800 s, poll 15 s. IVAS facts confirmed by that
  code: multipart file field `file`, analysis id `lbrdrAnlId` else `analysisId`; the user tested 2026-11-11: three separate logins →
  three tokens → three uploads at once all accepted (a new login does not revoke earlier tokens; one login per pod is fine). Still open with IVAS: token lifetime, how to check an
  uncertain upload, `patchedVersion` shape, `actionDivCd` values, what `components.total` counts.
- **An as-is version of the company parent that the repository does not have (v2.1z-ag, company run 2026-11-11)**: the
  transform runs OpenRewrite on the AS-IS poms; a project that already uses the company parent's g:a (or an alias) at another
  version Nexus lacks (`…:1.0.0-SNAPSHOT`) used to abort `aborted:parent-not-found` because the checks compared only g:a
  (`_check_parent_in_rewrite`, `BuildFixLoop._parent_abort`). Now `sbmig/restructure/asis_parent.py` sorts the missing parent:
  configured version → abort (the message names both facts); another version / alias → set the configured coordinates in the
  `<parent>` (text-preserving), commit, journal `asis_parent_aligned`, flag `asis-parent-aligned`, one Handover line, OpenRewrite
  once more — and the zero-LLM pre-try `sbmig-parent` in the build loop; a foreign parent → continue with flag
  `rewrite-skipped-asis-parent`. Switch `openrewrite.unresolvable_asis_parent: align | continue | abort` (default align).
  Fixture `boot-asis-parent-unpublished`.
- **Maven's HTTP repository blocker and the company settings.xml (v2.1z-ah; the settings shape corrected 2026-11-12 from the
  user's reading of the file)**: Maven ≥ 3.8.1 ships the blocked mirror `maven-default-http-blocker` (`external:http:*`,
  `http://0.0.0.0/`) in `<maven home>/conf/settings.xml`. The company settings.xml REDEFINES that id as an ordinary mirror
  (`external:http:*`, the release repository's URL, `<blocked>false</blocked>`; a second entry with another id and a `mirrorOf`
  that matches nothing is inert; `central` is mirrored to Maven Central). Nothing is blocked there, but every `http://`
  repository that `mirrorOf` covers is answered by the release repository: the snapshots / public repositories of the
  `<repositories>` block were never asked, so the as-is parent SNAPSHOT was missing — even the copy in `.m2`, because Maven ties
  a cached file to the repository id it came from. A mirror's `<url>` is where requests go, not an allow list; Maven takes the
  first matching mirror (exact id, then patterns in order). The user's fix, which made the run pass on the company PC with the
  code from before v2.1z-ag: `,!<repository id>` for the other repositories in that mirror's `mirrorOf` (PC settings.xml AND
  the pods' `/data/.m2/settings.xml`, whose source is ConfigMap `sbmig-config`). **The id `maven-default-http-blocker` in a Maven message is therefore
  no evidence of blocking** (`Could not find artifact … in maven-default-http-blocker (<real URL>)` is an ordinary miss with
  such settings): `http_blocker_hit` needs `Blocked mirror for repositories` or the blocker address `http://0.0.0.0`. sbmig
  never edits settings; it passes `-s` only when `build.settings_file` is set. A real `Blocked mirror for repositories` is its
  own environment abort: `attn=infra:http-blocked,aborted:http-blocked`, journal `maven_http_blocked`, a
  Handover line naming the repository, the settings file and the fix; no parent alignment / pre-try / retry / LLM runs for it; a
  preflight WARNING `maven_http_blocker_active` when the settings already route an http repository to a blocking mirror.
- **Remediation, first real-Maven runs (2026-11-11, ~15 chained runs on fixtures with a fake IVAS server)**: the remediation now
  builds the migration's own verify targets (`RemediationState.build_targets`; per-service MSA trees without a root pom are
  accepted), refuses insecure-TLS / `-s` / `-f` arguments in `build.extra_args` as a readiness problem instead of dropping them,
  retries a cached resolution failure once with `-U`, logs every `remediation mvn …` call, and explains a baseline failure that
  follows a successful migration build. Known gap: a project using git-commit-id fails the baseline (the build copy has no `.git`).
- **Remediation, first run against the real IVAS (company PC, Windows, 2026-11-12)**: on a PC the chained remediation needs
  `build.settings_file` (`SBMIG_BUILD__SETTINGS_FILE=~/.m2/settings.xml` in `.env`): the migration falls back to Maven's default
  settings, the remediation says `skipped (not ready): server settings.xml not configured` (the cluster manifests already set it).
  With it the IVAS side worked end to end: login, upload of the migrated tree's source ZIP, status polling, summary, both
  vulnerability pages, plan (1 proposal, 56 residuals), candidate build. It ended `FAILED resolution_mismatch`: one version text
  (log4j-api in one module's pom) had been planned as if it governed every module resolving that version, and three modules kept
  the old one. The next run passed the candidate build, the resolution check and the round commit and failed in
  `freeze_delivery` with `OSError: [Errno 9] Bad file descriptor`: the finished ZIP was re-opened read-only for `os.fsync`,
  which Windows refuses (fixed `115d537`: `"r+b"`; `tests/remediations/test_intake.py` has a fixture applying Windows' fsync
  rule — use it for any new write path). **Proposal scope** since then: `InventoryItem.also_modules`, `Proposal.modules` / `also_modules` (outside
  `proposal_hash`); `verify_resolution` requires from → to in the governed modules and their artifacts only, an also-module left
  behind is an `info:` memo and the next round's business (round commits, delivery exists, INCOMPLETE instead of FAILED); the
  `chained remediation <id>: <status> <reason> — <message>` line of `sbmig.log` carries the reason sentence. In the company
  layout the modules inherit the role parents, not the project root, so a root-pom `<dependencyManagement>` override reaches
  none of them. **Decided (2026-11-17, v2.1z-an)**: "일단 본인들이 선택하도록 만들자. 취약점 해결이니까 선택해서 박을지 아니면
  검토 후에 진행할지는 본인들 선택으로 할 수 있도록 해야할것 같아!" — the tool proposes the entry in each such module's own pom
  (one proposal per pom and management key); with approval switched on the owners check what to apply now, an unchecked one
  is reported as "선택하지 않아 적용하지 않은 변경" with the pom, the entry and the path (not proposed again in that run, a later
  run proposes again). `remediation.module_overrides: false` restores the residual (plan byte-identical; in `policy_hash` only
  when false). Contract: `docs/INTERFACES.md` "v2.1z-an".
  **Decided (2026-11-18, v2.1z-ar)**, relayed from the project teams: "취약점진단 승인 없애고 그냥 쭉 다 일괄 승인해서 진행해서
  결과만 알려 달래! 여기 빌드파드 제한이 3개니까 이쪽까지 승인까지 하긴 힘들다고!" — `remediation.always_approve` defaults to `[]`
  and the planner's fixed rule "an override entry always waits" is gone: `requires_approval = require_approval or (risks ∩
  always_approve)`. With the shipped configuration every proposal (major, parent, BOM, override entries in the root or a
  module pom) is applied on its own, no run saves `AWAITING_APPROVAL`, and a chained remediation ends in the migration's
  build pod. Approval is an opt-in: `SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"` in `sbmig-env` (all), or
  `remediation.always_approve` in the company yml (single classes; no env key, the Job template passes none). A change set
  that breaks the candidate build fails as a whole (`candidate_build_failed`; nothing is retried without single proposals).
  The report marks what was applied unreviewed: 조치 완료 gets the column 「주의」, 해야 할 일 the step 「자동으로 적용한 변경
  확인하기 (N건)」. A run an earlier version left waiting continues after one press of the page's apply button (the resume
  plans again under the new policy hash; unchecked proposals stay rejected). Contract: `docs/INTERFACES.md` "v2.1z-ar".
- **Remediation: in-house artifacts and the Korean report (user, 2026-11-12: "규격을 받아왔어")**: a vulnerable library that
  comes from the company's own artifacts is NOT the project's to fix — "해당 사내 라이브러리 아키텍처 담당자가 pom 을 수정해줘야
  반영 가능". In-house = the artifacts yml groups named by `remediation.inhouse_groups` (shipped `[mandatory, optional, parents]`;
  the user on parents: "parent, boot-jar, comp-jar 에서 파생된 것도 우리 아키텍처팀에 문의해야 고쳐지는거니"): a mandatory / optional
  g:a (aliases too) that is the library itself or on its `dependency:tree` path; for a module inheriting a company parent, a
  version the parent chain manages (unambiguous provenance only) or a transitive under a dependency no project pom declares.
  Such an item is a residual with reason `inhouse_artifact` (never a proposal or override; exception: a `policy.pins` entry for
  the parent still yields the parent-version proposal), `counts.residual_inhouse`, the in-house keys are in the policy hash, the
  run stays `INCOMPLETE`. The report prints `remediation.inhouse_message` ("해당 아티팩트는 예외처리가 가능합니다. 담당자에게 연락
  부탁드립니다.") over a section grouped by in-house artifact. **`report.md` is Korean** — rewritten the same day after the
  user read the first real one ("기계번역처럼 되어서 보는 것도 힘들고 읽는 것도 힘들다"): workplace Korean, not the code vocabulary
  word for word (glossary in `agent-doc/12-remediation-report.md`; particles attached, none after a data value unless
  invariant; `tests/remediations/test_report.py` rejects the old words and stand-alone particles). Sections, empty ones left
  out: result block → 요약 → 해야 할 일 → 조치 완료 → 승인 대기 → 사내 라이브러리에서 발생한 취약점 → 직접 확인이 필요한 취약점
  (grouped by reason, the explanation once per group) → 조치 대상에서 제외한 항목 → 변경하지 않은 것 → 처리 기록 (collapsed;
  the only place with status / reason codes). Stored data / API keep codes and English sentences;
  `sbmig/remediations/i18n.py` translates for display (`ko_text` rules, unknown text unchanged; a new English sentence in the
  remediation code needs a rule + a sample in `tests/remediations/test_i18n.py`). Operator doc: `agent-doc/12-remediation-report.md`.
  Severity marks (user, same day: "취약점 옆에 위험도 빨강 주황 노랑 … 눈이 가게"): 🔴 / 🟠 / 🟡 by `remediation.severity_red` /
  `severity_orange` / `severity_yellow` (9.0 / 7.0 / 4.0 on the IVAS score, nothing below yellow), a 위험도 column first in every
  library table with the worst rows on top, per-level counts (before / after) in 요약. Emoji because Markdown has no colour — whether the
  company PC's Windows renders 🟠 / 🟡 is to be checked there.
  Still the user's to decide: transitives under a dependency the project itself declares, in modules outside the root's reach.
- **Remediation: where a library comes from (v2.1z-am, 2026-11-17, after the user read a real report from the cluster)**:
  "루트 pom을 상속하지 않는 모듈이라는게 뭔지 모르겠어", "일부 모듈은 사내 라이브러리에서 가져옴. 이것도 결국 사내 라이브러리에
  속해야 하는거 아니야?", "얘도 좀 어디서 발생 되었는지 알아야 조치를 할거 같아!", "IVAS에서는 2.2.0.release의 버전을 보고했는데
  메이븐은 2.2.0.RELEASE를 사용해서 남겨뒀다는데 이건 upper건 lower건 적용하게 해줘야", "classifier가 붙은 의존성은 뭐야?". Now: IVAS
  ↔ Maven matching ignores case (Maven's spelling in `ga` / `version`, the IVAS one in `reported_*`); an item is split by module —
  only the modules that get the library through an in-house artifact become the `inhouse_artifact` residual, the rest is planned on
  its own facts (`Residual.inhouse` only on in-house residuals; the note is gone); every item / residual carries one `ModuleSource`
  per module (`how`, `via`, `declared`, `manager`, `inhouse`, `variant`); a classifier / non-jar dependency gets an override entry
  with the same `<type>` / `<classifier>` (plain-jar entries unchanged byte for byte). `report.md` prints "들어온 경로:" under the
  사내 / 직접 확인 tables (once per row when the modules share it, else per module group), "(IVAS 표기: …)" in the version cell,
  and the group `not_inheriting_root` is "모듈 pom에 직접 지정해야 하는 버전" in owner words (no "상속"). Module-level override
  entries came with v2.1z-an (below in "Remediation, first run against the real IVAS"). Contract:
  `docs/INTERFACES.md` "v2.1z-am"; report rules: `agent-doc/12-remediation-report.md`.
- **Run flows and the page (v2.1z-aj, 2026-11-12)**: a run is `full` (the migration, then the chained remediation) or
  `remediation` (no migration — the user: already-migrated projects are checked against IVAS again later, "이때는 마이그레이션을
  안타고 remediation만"): `sbmig run --flow remediation`, `POST /api/runs?flow=remediation`, on the page 전체 진행 (default) /
  취약점 해결. The remediation flow runs intake without any edit (preflight + build-tool check only) → `chain.run_only` on the
  uploaded ZIP's own bytes → a short report; graph nodes `intake → remediation → finalize`; no LLM, no KB; neither
  `remediation.after_migration` nor `trusted_projects` is asked; files under `<run dir>/remediation/` like a chained run, so
  status / approvals / resume are the chained ones. Result: `… flow=remediation remediation=<STATUS>`, exit 0 SUCCEEDED / 2
  any other saved status / 3 not started (`remediation:not-ready`), `build=` = what was built (FAIL only for a failed or
  missing build). A full run's result line is unchanged. Web: the flow is a run value like `stop_after`
  (`RequestRecord.flow`, Job env `SBMIG_FLOW` set by Main, nothing in the 81 template). Page: name `panacea-remediating-oss`
  (temporary, the user's), no description, no footer; a web run's id is `<slug of the uploaded file name>-YYYYMMDD-HHMMSS-<hex>`
  (24 characters, `[a-z0-9-]`, `web` when no letter is left — the id is the Kubernetes Job name) and the list shows the file
  name as uploaded ("어떤 프로젝트가 올라간지 모르겠어"; SSO with per-account lists comes later). Abort reasons reach the page in
  Korean through `sbmig.report.i18n.tr_line` (`abort_reason_ko`): a new `_abort(...)` reason at intake needs a pattern
  (`tests/report/test_abort_reasons_ko.py`). Limits: a ZIP nested two or more folders deep is not accepted by the remediation
  flow; not run in the cluster or against the real IVAS yet. The user announced one more feature after this.
- **Framework projects (v2.1z-ak, 2026-11-15)**: the user — a project that has the in-house framework's core artifact
  among its dependencies gets "jdk, jakarta 업그레이드만 하고 빌드하고 취약점 점검만 하는 방식"; the artifact may be declared
  directly or sit in a parent pom; decisions the same day: the fix loop runs ("jakarta 때문에 수정루프는 돌아야할거야"), the
  framework's own version is kept ("그대로 두자"), a transitive-only presence is checked too (the user was unsure whether
  it exists; it is treated like a declared one and can be switched to `note`). The real artifact name is a company
  coordinate: never in the repo — the company writes `framework: {artifacts: ["<artifactId>"]}` into its
  `config/artifacts.yml` (baked into the image → rebuild). Empty list = check off. `sbmig/analyze/framework.py` looks in
  four places (direct declaration in a project pom; inherited from a parent pom outside the project, read from the as-is
  effective pom of intake; a bundled jar no pom declares; `dependency:tree`, one more Maven call, only when the first
  three found nothing) → `ProjectProfile.framework` (`FrameworkUse`: evidence, `unchecked`, `scope`), flag
  `framework-project`, journal `framework_project` / `framework_check`, a Korean Handover line. Scope `jdk-jakarta`
  (`MigrationPlan.scope`): plan = `transform.rewrite` (the jdk21 + jakarta sets only) → `transform.jsp_jakarta` (the JSP
  rename, run by the orchestrator, not the restructurer) → `verify.build` + fix loop → the chained remediation; no
  restructure, no structure review, the parent pom is never aligned or replaced (an unresolvable parent aborts
  `aborted:parent-not-found`). The fix loop of such a run takes its version facts from the project's own chain (the
  as-is capture stands for the "parent chain"; never the company chain or the target Boot BOM), never touches a framework
  artifact's version (protected keys, pre-tries skip it, `_framework_guard` rolls back an attempt that changes it or a
  `<parent>`), blocks a javax-era library that is the framework itself (owner must ship a jakarta build), and the prompt
  has a reduced-scope system text + the section "Scope of this run". Remediation: in-house group `framework`
  (`remediation.inhouse_groups`, patterns via `InhouseKeys`) — the framework and what it brings are never proposed.
  `SBMIG-RESULT … scope=jdk-jakarta`; report block 사내 프레임워크 프로젝트; the page labels the run and shows 재구성 /
  구조 검토 as 하지 않음. Switches: `framework.scope: jdk-jakarta | full`, `framework.transitive: apply | note | off`,
  `framework.recipe_sets` (default `[jdk21, jakarta]`: the standard jakarta recipe ends on Jakarta EE 9 / servlet 5.0;
  `servlet_jakarta_10` adds the Servlet 6 removals — the company's call, depends on the Tomcat line).
  **What the real-Maven e2e found (the unit tests had not)**: a reduced run has no restructure, so the two corrections
  the restructure makes for what the UPGRADE itself breaks were missing — (1) the jakarta recipe turns
  `javax.servlet-api provided` into `jakarta.servlet-api compile` (servlet API inside the WAR) → the as-is `provided`
  scopes are restored after the rewrite (`sbmig/restructure/scope_restore.py`); (2) javac's default source encoding is
  UTF-8 since JDK 18 and OpenRewrite skips a file it cannot decode (an MS949 file kept its javax imports) →
  `project.build.sourceEncoding` is written BEFORE OpenRewrite (`sbmig/restructure/source_encoding.py`; the full flow
  still writes it after the rewrite — same skip there, not changed in this round); and the report claimed "Spring Boot
  4.1.0" for a non-Boot project (target fallback). Commit order of a reduced run: `transform: source encodings written`
  → `rewrite: transform.rewrite` → `transform: as-is provided scopes restored` → `transform: JSP javax → jakarta`; the
  corrections are error-list entries with `seen = transform`. Lesson: when a flow skips a stage, list what that stage
  repairs and sort it by cause (the upgrade vs the parent swap) before calling the flow done.
  Fixtures `fw-direct` (MS949 source), `fw-parent`, `fw-transitive` (`servlet_jakarta_10` listed),
  `fw-transitive-note` + stand-in libs `fixtures/libs/example-fw`.
  Open risk told to the user: with the framework version fixed, a framework still compiled against javax cannot work
  with jakarta project code — such runs end BLOCKED naming the framework. Contract: `docs/INTERFACES.md` "v2.1z-ak".
- **Framework projects, changed 2026-11-19 (v2.1z-aw)** — the user: "사내 프레임워크는 llm도 돌리지 말고 그냥 jdk - jakarta까지만
  하고 파일을 내려주도록 하자. llm이 이걸 해결해주기 쉽지 않을거 같아. 사내 라이브러리쪽도 개입이 많으니까!", "<이름> 프레임워크라도
  명시 할 수 있어? 사람들이 모를거 같아서. 이거 좀 강조를 해야 할 것 같아서", and "취약점은 빌드가 실패해도 이어가도록! 어차피
  오픈소스 라이브러리 검사라!". This replaced the fix-loop decision of the bullet above (everything there about the fix
  loop applies only with `framework.fix_loop: true`); the default was `true` for a few hours on 2026-11-20 (v2.1z-bi) and
  is **`false` again** (same day, below), so conversion-only is the default. A reduced run = conversion + ONE build: `Orchestrator.no_fix_loop`
  gates `_verify_fix_loop` and the plan advisor (no pre-try, stored script, LLM; `llm=0` is literal), the entries stay
  open with a note, journal `verify_loop_skipped`, one hand-over line, exit 2 on a failed build, the converted tree is
  delivered. The framework is named everywhere a person meets the run: `framework.name` or
  `FrameworkConfig.display_name` (artifact found → patterns; role words like `core` cut; `example-fw-core` → `Example`;
  the real name is a company name: never in the repo) → `FrameworkUse.name`; the sentence
  「<이름> 프레임워크는 JDK 21, Jakarta만 적용합니다.」 heads the report block (now directly under the head table) and is
  a highlighted notice on the upload card (`GET /api/configs` → `framework`), the run card and the result card
  (`GET /api/runs/{id}` → `framework`). The Korean guide of a failed conversion-only run has 「변환 뒤 남은 빌드 오류」
  instead of 「해결하지 못한 오류」 and never implies a fix was tried. **Remediation after a failed build**
  (`remediation.after_failed_build: framework | never`): `_chain_remediation` chains a conversion-only framework run
  whose final build was attempted and failed with `baseline_may_fail=True`; ordinary failed runs and the
  remediation-only flow are unchanged (my scoping; the user was told; since v2.1z-bi framework runs with the loop on chain too). In the remediation
  (`RemediationState.baseline_tolerated`) the failing baseline build is recorded and the flow goes on; a candidate build
  may fail only with the baseline's errors (`maven.compare_with_baseline`: same target, same failing module, every
  error key among the baseline's) → the round is committed, delivered (`DeliveryInfo.build_tolerated`) and rescanned;
  a new error = `candidate_build_failed`. Changed versions that no build confirmed never end `SUCCEEDED`
  (`INCOMPLETE baseline_build_failed`). Measured with Maven 3.9.10: `dependency:tree` resolves a sibling module from the
  reactor without packaging it, so the inventory works on an unbuilt two-module tree. Lessons: (1) a mode that
  "tolerates a failure" needs its own words at every place that words the failure (status chip, list line, ZIP badge,
  report head): the first cut said 일부만 조치됨 for a project with nothing left to fix; (2) the page's flag for a
  ZIP must come from the delivery (`build_tolerated`), not from the run's mode — a change set that ADDED an error is
  still a failed build. Fixture `fw-build-error` (a call removed after Java 8: builds on 8, fails on 21). Contract:
  `docs/INTERFACES.md` "v2.1z-aw".
- **First real cluster projects, 2026-11-19 (v2.1z-ax / ay / az)** — three things the fixtures never had.
  (1) **A library uploaded without its pom** (an in-house jar in the releases repository): Maven asks every repository
  for the pom; "not found" everywhere is a warning, but ONE repository answering with an error (the Central proxy of a
  closed network gives 502 for a path it never cached) stops the build with `Failed to read artifact descriptor` /
  `Could not transfer artifact G:A:pom:V from/to …`, intermittently (Maven re-asks about a missing pom about once a
  day, and does not cache a transfer error). A pom file in the local repository ends it. `sbmig/build/pomless.py`
  (`build.pomless_stub`, default on): proof first (jar obtainable, pom obtainable nowhere: `RepositoryLookup.file_answers`),
  then a minimal pom with a marker comment in the pod's own local repository (slots have separate ones), the same
  command again; wrapped around every Maven call that resolves dependencies; a later run that builds on such a file is
  told too (`pomless_stub_used`). The user's alternative, a system-scoped jar in the project pom, was rejected: not
  packaged into the WAR, and it changes the project. (2) **A pom whose Korean text is MS949 under a UTF-8 header (or
  none)**: Maven builds it without a warning; lxml raises `OSError` (the remediation refused the project with 「읽을 수
  없는 pom」); OpenRewrite cannot parse it and the whole recipe run fails — which the run used to pass over in silence
  (`exit=0 build=ok`, nothing converted). Now: the remediation reads poms with the migration's one decoding rule
  (`pom_encoding`) and edits byte-true; before the first OpenRewrite run such a pom is made consistent
  (`sbmig/restructure/pom_charset.py`: declaration → `EUC-KR` when the bytes are strict EUC-KR, else saved as UTF-8,
  because the plugin writes replacement characters over MS949-only syllables; `layout.legacy_context.correct_pom_encoding`);
  a failed conversion sets `rewrite-failed`, is the first hand-over line, and a conversion-only (framework) run whose
  conversion failed is not a success even when its build passes (`exit=2 build=ok`). The standard library's XML parser
  refuses a multi-byte declaration on bytes: decode with `pom_encoding` first, everywhere. (3) **Page**: a run a person
  stopped is orange (`user_stop`; "너무 빨가니까 문제 투성이 에이전트 같아"), 「지난 실행」 lists three days with the line
  「최근 3일 내에 등록한 프로젝트만 보입니다.」 and 「이전 실행 보기」 for older results (`GET /api/runs?days=`).
  Lessons: (a) "the tool I wrapped tolerates X" must be measured for EVERY tool in the chain — Maven, the rewrite
  plugin, lxml and the standard parser disagreed about one pom in four different ways; (b) a step that fails and lets
  the run go on must own a flag and a first line in the report on the day it is written, not when a user finds it;
  (c) an operator command I hand out (the manual stub pom) must be checked against the deployment's layout: there are
  three local repositories, my first instructions named one. Contracts: `docs/INTERFACES.md` "v2.1z-ax", "v2.1z-ay",
  "v2.1z-az".
- **The remediation has no approval step (removed 2026-11-19, v2.1z-ba)** — the user: "우리 approval awaiting 기능 아예
  빼버리자. 이렇게 안쓸거래. 환경변수도 없애고 기능도 없애고 화면에 보여주는것도 없애버리게! … 그냥 코드도 없애버려".
  Every older note in this file that speaks of `AWAITING_APPROVAL`, `require_approval`, `always_approve`, the approval
  list or `POST …/approvals` describes something that no longer exists. Gone: the settings and
  `SBMIG_REMEDIATION__REQUIRE_APPROVAL` (ignored with a WARNING when still present, `config._warn_removed_keys`; in
  `contract.ENV_PIN_SKIP`), the state, the decisions `pending` / `approved` / `rejected`, the reasons, the service's
  and API's `approvals` / `proposals`, the page's list (neither page contains 승인; a test holds it). Kept:
  `AWAITING_CONFIRMATION` and resume ("그건 일단 내버려 두자"). Old states are read and shown as ended WITHOUT the word
  (`models.LEGACY_AWAITING_APPROVAL`, 「조치 미완료」): the feature came back to the user as a question from colleagues
  when a release began to label and to pin old approval-waiting runs in 「지난 실행」. Lessons: (1) when a list starts to
  show a state it did not show before, look at what is already stored in that state on the cluster; (2) "keep it
  listed while it waits for a person" pins a run forever when nobody will ever answer. The word 승인 elsewhere
  (approved repository hosts, libraries a person approved) is another thing and stays.
- **IVAS file components (2026-11-19, v2.1z-bc)**: the matched-files address was wrong (`filecomponent` for
  `filecomponents`) and had never met the real IVAS: the fake server of the integration tests answered an empty page to
  ANY unknown address, so a misspelt path looked like "no files". It answers 404 now; when a fake stands in for an
  outside service, an address it does not know must fail. The user's policy since then: only libraries are remediated
  and judged ("우리 백엔드로직만 라이브러리 검사하고 프론트는 검사 안하려고"); findings IVAS makes in files of the project
  are reported in a section of their own and never hold the verdict (`remediation.file_components: report`, default;
  `count` = the earlier rule). A failure in the file part of a scan is a note; the library part goes on. An id with `|`
  (the form the company calls ossId) is accepted by that path: confirmed by the user on the real IVAS; the client still
  reads it from `fileComponent.id` first (whether that can differ from `ossId` is not known). A jar lying in the project outside
  the pom is a file component too: told to the user. Also from that evening: a stopped background agent cannot be
  resumed, and an agent reused for a second task still shows the FIRST task's name to the user — the user stopped the
  IVAS work thinking it was a leftover; give a reused agent's new task a new agent when the name would mislead.
- **An upload names its project (2026-11-19, v2.1z-be)**: `POST /api/runs?…&project=<name>` is required, checked on the
  page and on the server before the body is read (`runs.PROJECT_RE` = `^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$`; the user:
  "영어랑 _, - 로만 … 리눅스에서 쉘 접속 했을때 치기 귀찮아서! 공백도 비허용!"). A web run's id, and so its folder under the
  output root, is `<project>-YYYYmmdd-HHMMSS-<hex6>`; older notes in this file that say `run-…` or "a slug of the file
  name" describe older folders, which keep working. Two things the new prefix exposed: the Job name cut the TAIL of a
  long id (the unique part) and `sbmig clean`'s upload pattern knew lower-case letters only. When an identifier gets a
  new alphabet or length, grep every pattern and every truncation that reads it, in every package, before shipping.
- **A single-module Spring Boot app is split (2026-11-19, v2.1z-bf)** — the user: "parent가 spring boot로 시작하는
  프로젝트가 xxx-boot만 생성되었는데 여기 정적자원이랑 다 있거든!? … 이 케이스를 생각을 못했네!" The sentence in "Company
  constraints" above ("a single-module Boot app is wrapped into `{name}-boot`") now holds only for
  `layout.boot_single_module: wrap`; the default `split` sends such a project through the layout split of non-Boot
  projects (plan step `restructure.apply`, mode `layout`, `boot_single`; `planner.boot_single_split`,
  `Restructurer._boot_single_split`): application class + Boot configuration in `{name}-boot`, every other source in
  the source module, static folders / web root / JSPs (at their web path) / templates in the front module. Projects
  with several modules and MSA keep their directories. The module NAMES come from `layout.comp_module` /
  `front_module` (the user first wrote `xxx-comp-jar` / `xxx-front-jar` and corrected it the same day: `xxx-boot` /
  `xxx-back` / `xxx-front`, which is what the settings give). Fixtures:
  `company-boot-single` (split; its built jar is started in the check), `company-boot-wrap`. Contract and limits:
  `docs/INTERFACES.md` "v2.1z-bf".
- **Report view, report names, model names (2026-11-20, v2.1z-bg)** — the user: "보고서 미리보기 md파일 viewer로 …
  migration-report와 remediation-report로 … role에 들어가는 escalation_fixer나 build_error_fixer 이름이 나왔으면". The page
  draws reports with its own `MdView` (DOM nodes only, links only `http(s)` and files of the run, images never loaded):
  never pass report text to `innerHTML`, it comes from project files and build output. Downloads are named
  `migration-report.*` / `remediation-report.*` (`runs.DOWNLOAD_NAMES`, the remediation files route's `?download=1`);
  files in the run directory keep `report.md` / `report.json` (the web, admin, cleanup and old runs depend on them).
  `LLMResponse.model` / `LLMUsage.model` = the name ASKED with (the role on the agent gateway); the gateway's real model
  names go to `served` (log line ` served=…` after `finish=`). `llm.pricing` is looked up by the asked name first, then
  the served names. Old rows named `unknown` / `?` / empty show as 「이름 없음」. Contract: `docs/INTERFACES.md` "v2.1z-bg".
- **Uploaded build output stays out of the remediation's file list (2026-11-20, v2.1z-bh)** — a remediation-only ZIP
  carried `<module>/target/`; the files were in `original/manifest.json`, the candidate's `mvn clean package` failed,
  and freezing the failed candidate ended the run `internal_error` (`file listed in the manifest is missing`) instead
  of `candidate_build_failed`. `intake.build_output_paths`: `target/` beside a `pom.xml` is validated but left out of
  `accepted/` and the manifest (`IntakeResult.build_output_files`, one note, one report line); `source.zip` keeps it.
  Anything `mvn clean` removes must never be a manifest path. Contract: `docs/INTERFACES.md` "v2.1z-bh".
- **Build errors are fixed in two more places (2026-11-20, v2.1z-bi)** — the user: "remediation 쪽도 버전 올라갔을때 에러가 나올 수
  있으니 llm 수정기능 붙여달라고 한다. 사내 프레임워크 … 도 llm로 빌드 수정 기능 넣어달래. javax -> jakarta만 변경하는거니까 그정도는
  해주라는데..". (A) `framework.fix_loop: true` runs the loop on framework projects (pre-tries without the company parent, stored scripts,
  LLM; `_framework_guard` as before). It was the default for a few hours; the same day the user turned it off again ("사내
  프레임워크는 … 일단 다시 disable 시키고 진행하자"), so the default is `false` = the conversion-only run of v2.1z-aw. A framework run whose final build still
  fails chains the remediation whether or not the loop ran (`remediation_after_failed_build`, `remediation_always`), and
  the migration report says the remediation followed a build that failed after the automatic fix. (B) The remediation's
  candidate build failing after versions were raised (baseline passed) runs `BuildFixLoop` on a WORK COPY of the candidate
  (`sbmig/remediations/fixer.py`, `remediation.fix_loop` default true, `fix_loop_max_iterations` 10) through the new
  `edit_guard` hook: existing manifest source files only, no `pom.xml`, nothing created or deleted, `NullKnowledgeBase`.
  Fixed → the fixed bytes are copied over the candidate, confirming build, verify, commit, `fix.patch`, report section
  「빌드 오류 수정」, page chip; not fixed → the candidate was never touched, `candidate_build_failed` with
  ` | automatic build fix: <reason>`. So the older notes' "no source change" and "the remediation-only flow calls no LLM"
  no longer hold: the fix loop is the one exception for both. Its LLM calls count in `SBMIG-RESULT llm=`, the run status
  and the admin page (v2.1z-bl). Limits: no pom edits, no loop after a failed baseline, not run on the company cluster.
  Contract: `docs/INTERFACES.md` "v2.1z-bi", amended by "v2.1z-bj" / "v2.1z-bk" (next note).
- **Rules of the remediation's fix loop (2026-11-20, v2.1z-bj/bk/bm/bn/bo/bp)** — five review rounds and two verifications the same day (the user: "아 이거
  오늘 금요일이라 안급해~ 고칠 수 있는거 꼼꼼히 고치고 가자!"), every finding reproduced first, then a page round ("보고서는
  migration, remediation 각각 따로 보여질 수 있도록!"). Rules a contributor must not break:
  - The candidate is changed by the fix loop ONLY through the graph's overlay (`graph._overlay_fix`) of the bytes kept in
    `rounds/<r>/fixloop/fixed/`. The loop itself (git, scripts, builds, a crash, a kill) runs in the work copy
    `fixloop/tree/` without any `.git` path of the upload, and changed files are found by comparing bytes, not by git.
  - After EVERY build of the candidate (`graph._package`), and again right before commit and before a failed candidate
    is frozen, the tree is synchronised with what the remediation decided: `accepted/` + the applied poms in
    `rounds/<r>/applied/` + the reported fix, compared against the stored table `rounds/<r>/expected.json`
    (`intake.sync_tree_table`: each candidate file read at most once, `accepted/` never re-read).
  - A candidate is built only with the complete-apply proof: `applied/`, then `expected.json`, then the marker
    `candidate.ready.json` carrying that file's sha256 (`_Holder.vouch_problem`). Without it the round goes back
    through `apply` (journal `candidate_rebuilt` with `why`), so a run folder an older version left is re-applied,
    never "synchronised" back to `accepted/` (which had reverted its pom edits as build changes).
  - A candidate that cannot be put back to its expected state is never committed or delivered: the sync tries every
    path (read-only folders made writable once), then `intake.SyncFailed` → `CandidateNotRestored` → FAILED
    `internal_error`, nothing of that round delivered, an earlier verified delivery kept. Never swallow a sync failure.
  - State is saved BEFORE any file is deleted (fixed bytes, patches, folders), and right after the loop returns,
    before the candidate is touched.
  - A stop never discards a fix: a stop before or during the confirming build halts STOPPED with the fix kept and a
    resume confirms again; a loop that ended `stop requested` is not a verdict (a resume may run it again); a killed
    loop's folder is kept as `fixloop-interrupted-<n>/` and its LLM usage is recorded and saved before the rename, at
    the next build or report node, exactly once.
  - Anything `mvn clean` or a build plugin can change (`target/`, a formatter, sortpom) must never reach a delivery
    unreported: never a manifest path (`intake.build_output_paths`), put back after each build, named in the report.
  - The two reports are previewed separately on the page (the remediation preview shows exactly
    `remediation/report.md`); never fold one into the other. The page also no longer shows 고급 설정 or the other run
    files ("관리자에서 로그 파일들 받을 수 있으니까 그 밖의 파일 영역은 빼달래! 고급설정 영역이 빼줘!"); the start request
    sends none of the hidden 고급 설정 values (a browser restores form values).
  - A loop that ran and did not fix is worded 「시도한 소스 수정은 결과에 넣지 않았습니다.」, never "reverted": the
    candidate was never touched; a loop that made no attempt is 「빌드 오류 자동 수정을 하지 못했습니다(<사유>).」.
  - (v2.1z-bn) Every result ZIP goes through the one delivery swap (`graph._Holder.install_delivery`: frozen to
    `delivery/.result.next.zip`, marker, verified ZIP COPIED, `os.replace`, `delivery.json`, state; `recover_delivery` on
    the next entry). Never write or delete `result.zip` anywhere else; a freeze that fails leaves the previous delivery.
  - (v2.1z-bn) A final state always gets its report: a kill during the report node is repaired on the next entry
    (`graph.write_pending_report`). (v2.1z-bo) Once the replace happened the swap is rolled FORWARD (records that cannot
    be written → `DeliveryRecordNotWritten`, the marker stays); every entry that sees a run folder again runs
    `recover_delivery` / `graph.repair_final`, and the web service repairs a final state whose owner is gone in every
    mode (`RemediationManager.repair_final`), never changing its status, reasons or message.
  - (v2.1z-bo) Nothing a node sets may be missing when a mid-node save happens (`install_delivery(after=…)` sets it
    before the swap's save), so a re-entry after a kill never does what the uninterrupted run would not — in particular
    never a second IVAS upload (`_stored_baseline` reuses a complete baseline scan of the same sha256). When you add a
    save inside a node, write down what a kill right after it does (INTERFACES "v2.1z-bo" has the table).
  - (v2.1z-bo) Unknown usage is never shown as a number: `FixResult.usage_unknown` / `tokens_unknown` → 「알 수 없음」 on
    the admin page, `…_usage_unknown` in the APIs, a sentence in the report, never priced.
  - (v2.1z-bo) The fix loop (`BuildFixLoop`, the migration's too) checks the stop request and the time budget before
    every LLM request, not only between builds (`_halt_before_ask`, journal `llm_ask_skipped`).
  - (v2.1z-bn) When the intake left build output out, the baseline scan and a no-change delivery use the scan source
    `original/scan-source.zip` (a deterministic ZIP of `accepted/`), never the upload; every check that the scanned
    bytes are the bytes held compares against it. `expected.json` rows carry the exec bit (schema 2).
  - (v2.1z-bp) A decision at a node's entry rests on a marker written for that purpose, never on the reason text of
    the last record: the verification of v2.1z-bo found `_run_build` skipping the loop by the reason `discarded: the
    stored fix is not usable`, which two different situations write; now `FixResult.ended_round`, set in the same
    save only when the fix the loop had just returned was unusable.
  Open, the user's question to the business side on Monday 2026-11-23: should an upload that carries `target/` keep
  being accepted with the folder left out, or be refused? Until then the behaviour stays.
  Not checked: the company cluster, a real LLM, real IVAS, large projects and real build times, Windows (the
  permission and read retry); a project whose build needs its own `.git` (git-commit-id: the work copy has none, so its
  loop ends not fixed); the web service's repair and Job mode; the admin page. Four real-Maven smoke passes (Maven
  3.9.10 + JDK 21, fake IVAS, scripted LLM; the third also a real migration run of `pure-java`) passed; the fourth
  and fifth reviewers signed "a delivered ZIP never contains or lacks a change the report does not account for". Still
  open after v2.1z-bp: a stored fix whose files vanish between the save and a kill runs the loop once more; `fixed/` and
  `fix.patch` can stay unreferenced after a kill between the confirm-failed save and the removal; the upload page's chip
  lacks the unknown-usage marker; a killed pod's fresh claim delays the web repair up to 600 s; a call in flight when a
  pod is killed is never counted; (closed by v2.1z-bq: the "partly unknown"
  note now reaches the admin page's project / overall / model sums, and a loop killed before its first journal line is
  a known zero). Contract and as-coded notes: `docs/INTERFACES.md` "v2.1z-bj",
  "v2.1z-bk", "v2.1z-bl", "v2.1z-bm", "v2.1z-bn", "v2.1z-bo", "v2.1z-bp", "v2.1z-bq".
- **The safe sweep (2026-11-22, v2.1z-br)**: small defects, unused code, repeated reads, what enters the image
  (`docs/INTERFACES.md` "v2.1z-br" is the list a reviewer checks it against). Two things to keep: (1) `GET /api/runs` has a
  per-row net: an unexpected error while one row is built logs one WARNING (`run list: the row of run <id> could not be
  built …`) and lists that run as unreadable, so one damaged run folder never makes the list answer 500 (the single-run
  status has no net on purpose); (2) the retry-on-`TypeError` shims around builder calls are gone:
  `sbmig/loop/callee.py::takes_keyword` decides from the callee's signature, once, whether to pass a newer keyword, so a
  `TypeError` raised inside a build is never answered by a second build. Use it for any new optional keyword.
  **Same day, v2.1z-bs:** `sbmig/web/jsonsafe.py` is the place for answers and events that carry a file's content
  (a `NaN` / `Infinity` in a run file goes out as `null`, never as a 500 or a bare `NaN`).
- **Company convention (2026-10-20): agents run on LangGraph under uvicorn.** `loop.engine: graph` (default) drives
  the phases from a LangGraph `StateGraph` (`sbmig/graph/`, nodes intake → analyze → plan → phase×N → finalize;
  `inline` is the old loop, reports are identical by test); the web UI is FastAPI (`uvicorn sbmig.web.app:app` or
  `sbmig serve`), runs migrations in-process through `stream_graph` (one worker thread per run, per-run `sbmig.log`
  by thread filter) and streams `node` / `journal` / `log` / `report` events over SSE to `sbmig/web/static/index.html`
  (one file, vanilla HTML / CSS / JS, no web font or CDN — the company network is offline; redesigned 2026-11-05 in the
  company's blue: drop zone → stage rail → 진행 기록 / 로그 tabs → two download buttons report.md / result.zip; the log
  pane keeps a ring buffer and renders ≤ 1,500 lines, the fix for the browser lag). The config files every upload runs with
  are the server's choice (`sbmig serve --config` / `SBMIG_WEB_CONFIGS`, else `artifacts.yml` / `artifacts.local-e2e.yml`;
  `GET /api/configs` → `default`); the page no longer shows 고급 설정 or the list of other run files (hidden / removed 2026-11-20; logs and other files come from the admin page). Stop takes effect between graph nodes
  only. In-process: one migration at a time, further uploads are `queued`; Job mode: `SBMIG_JOB_MAX_PARALLEL` at a time (v2.1z-ae).
- **Admin page (v2.1z-ao, 2026-11-17)**, the user: "현재까지 프로젝트 별로 사용된 토큰량 볼 수 있는 페이지가 필요한데, 하드코딩으로
  [a fixed password]으로 접속 할 수 있는 url하나 fastapi에 붙여서 해당 비밀번호로 접속해서 볼 수 있도록 하나 하자. 그리고 총 로그들
  다운로드 받을 수 있도록도 하고!" → `GET /admin` (`static/admin.html`, not linked from the main page) + `/admin/api/*`
  (`sbmig/web/admin.py`). The password contains a company name, so it is built in as a salted PBKDF2 hash
  (`admin.ADMIN_PASSWORD_PBKDF2`); **the plain password never goes into the repo** (code, tests, docs, commit text) — tests
  monkeypatch the hash with a test password; a non-empty `SBMIG_ADMIN_PASSWORD` (Secret, web pod only, a secret name, dropped
  from a `local-job` child) replaces it. Session = random token in process memory, cookie `sbmig_admin` (HttpOnly, SameSite=Strict,
  Path=/admin, 12 h; a restart logs out); 5 failures per client address in 10 min → 429. Usage is read from the run folders on
  disk at every request (report.json's `llm_usage` / `llm_usage_by_model` / `llm_cost`; a run without a report counts the
  `llm call … in=… out=…` lines of its sbmig.log), grouped by the entered project name (the uploaded file name for runs without one); a cost sum is null when any run in it is
  unpriced. Log ZIPs (`sbmig.log`, `journal.jsonl`, `control/request.json`; `remediation/remediation.log` only when nothing
  can still write it — the remediation API's own withholding rule, an active resume pod included) are built in the web pod's
  LOCAL temp dir, never on `/data`: one build at a time (409 `busy`), 2 GiB total cap (413), 1 GiB free-space floor (507), a
  single file over 512 MiB is left out and listed in `SKIPPED.txt`, a growing log is copied up to its size at the start; the
  page asks `…/logs/check` first and says why in Korean. The site token guard still applies in front. Contract:
  `docs/INTERFACES.md` "v2.1z-ao" / "v2.1z-ap".
- Cloud sessions may lack JDK / Maven (the 2026-10-18 cloud image had JDK 21 at `/usr/lib/jvm/java-21-openjdk-amd64`
  and Maven at `/opt/maven`); without JDK 8/11 run `fixtures/libs/install.sh` with `JAVA8_HOME=$JAVA21_HOME` (JDK 21
  compiles the 1.8-target lib with a warning) and skip the `-m integration` as-is builds. Commit and push to a
  `claude/...` branch; the user merges locally.
