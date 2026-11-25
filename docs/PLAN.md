# Work log — sbmig flow-v2 (PM view)

This file has one **Status** table per work round of the deployed system — branch `flow-v2` (v2: one OpenRewrite run, the
company restructure, an LLM structure review, `mvn clean package`, build errors fixed by stored LLM-written scripts) and,
since 2026-11-11, the vulnerability remediation (IVAS) beside it. Newest round at the bottom. Rows describe what is true
today: things a later round removed have been taken out of the earlier tables.

- Checklist view (Korean, for the business team): `docs/PROGRESS.md`.
- The business request and the v2 decisions: `docs/v2/PLAN-v2.md`. Contracts per round: `docs/INTERFACES.md`.
- The v1 flow and its rounds (2026-10-18 … 10-26) live in git only: tag `company-v1`.

## Status — 2026-10-27, v2 (branch `flow-v2`: "변환 먼저, 고치는 건 LLM 스크립트로")

User (2026-10-26, the business team's request): intake → analyze → recipe selection → ONE OpenRewrite run without a
build → the company restructure as before → the LLM checks poms / dependencies / modules → `mvn clean package`; on
failure an error list fixed one by one, "고치는 건 무조건 LLM", the LLM answers with a **Python code bundle** that is
stored per case and run first the next time the case appears (augmented when it fails); regressions roll back; tests
skipped. Decisions: recipes = Java 21 / jakarta / Boot 4 only ("레시피가 무엇을 바꿀지 모르니"), all 32 handlers out,
critical review findings only, restricted script API, advisories guide-only, the flow replaced on a branch (v1 = tag
`company-v1`). Plan and status table: `docs/v2/PLAN-v2.md`.

| Deliverable | Where | Status |
|---|---|---|
| Step 0 — contracts: `Phase.TRANSFORM`, `StepKind.REVIEW`, `DecisionKind.SCRIPT`, `ScriptMeta` / `ScriptRunResult` / `ScriptProposal` / `StructureReview`, script fields on attempts and KB records (schema 3), `ScriptRunnerLike` / `ScriptStoreLike` / `FixerLike.propose_script` / `StructureReviewerLike`, config `script.*` / `review.*`, v2 defaults | `55d776e`; `docs/INTERFACES.md` "v2 — flow-v2" | done |
| Step 1 — S script runtime (`sbmig/script`: static check, `python -I` harness, helper API `p`, timeout + tree kill); K script KB (the script KB module of that step — replaced by `sbmig/errors/cases.py`, catalog action `script`, `sbmig kb scripts …`, `reviews.jsonl`); F `LLMFixer.propose_script` + prompts + request-matched replay; R `sbmig/review` + report "Structure review"; P v2 planner; L case clusters (L2), ladder reuse → augment → new, rollback before the next try, scoring + credit, tracker fields | `cb9a3b1` | done |
| Root causes first + file-overlap deferral (a missing dependency no longer costs one LLM call per symbol) | `50e9a49` | done |
| Step 2 — every fixture on the v2 flow with replayed scripts; reuse (pure-java → java6-legacy, shared KB) and augment (boot2-fix-failures) scenarios; review scripts fix MS949 encoding, binary resources under filtering, a wrong BOM; e2e keys `structure_review` / `scripts` / `kb.after` | `43d8b36` | done — real Maven **32/32** (29 build, 3 intended BLOCKED) |
| Step 4 — v1 code deleted (handlers, recipe rules, pre-emptive planning, library / project / KB recipes, harvest, ledger, symbols, patch replay, SEARCH/REPLACE fixer, advisory recipe policy, v1 config keys / models / CLI commands); pom-change deferral; refined case keys for coarse `build` / `other` errors; notes reach script prompts again; example adapter ported | this commit | done — offline tests **2,032** |
| Step 3 — docs: design doc + deck, ARCHITECTURE / ERROR-HANDLING / RECIPES / ADVISORIES / FLEET / ADAPTERS / RUNBOOK / INTERFACES, KB map, README, demos (`scripts/demo-kb`, `scripts/fleet-demo.sh`), fixture READMEs | next commits | done |
| Step 5 — company trial vs `company-v1` (LLM calls, time, success rate on real projects) | — | the user |

## Status — 2026-10-27, v2.1d (dependency version policy: "메이저를 올리지 마라")

Company run: SiteMesh 2 (`opensymphony:sitemesh`, javax) made the LLM add `org.sitemesh:sitemesh:3.2.1` and port the
filter to `ConfigurableSiteMeshFilter`; the build passed, the runtime (decorators.xml, JSP decorator tags) is unverified
— the 3.2.1 jar has no `com.opensymphony.*` class. User decisions: the LLM never raises a major version or swaps a
library on its own (same-major jakarta release first — SiteMesh 2.6.0 is one); library knowledge is **injected by
people** in `<kb>/libraries.yml`, no redeploy per library; a major upgrade is **not blocked** (the build must pass) but
leaves an operator guide ("이런이런 거 바꿨으니 운영자가 꼭 확인"). Contract: `docs/INTERFACES.md` "v2.1d".

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `DependencyChange` / `VersionCandidate` / `RepositoryVersions` / `OperatorCheck`, `ScriptProposal.verify`, `FixTry` / `FixAttempt` / `StructureReview` / `RunReport` fields, `FixContext.library_hints`, `VerifyGuideLike`, `libraries.*` + `build.repository_url` / `repository_timeout_seconds`, `JakartaLibrary.allow_major` / `source` | `f363222`, `tests/core/test_v21d_contracts.py` | done |
| V — `sbmig/build/repo_versions.py` (settings mirror / `build.repository_url` / Central → `maven-metadata.xml`, candidate poms → jakarta ✓ / javax, min Java; local repository fallback; two failures in a row → no more calls this run), `sbmig/build/depchanges.py` (pom diff → major / minor / added / removed / replaced incl. `<parent>` and plugins, property-resolved; `apply_table`, `still_applied`, `synthesized_guide` ko / en), `jakarta_libraries.py` company merge (`<kb>/libraries.yml`, mtime-aware), `Workspace.file_at`, `knowledge/libraries/libraries.company.example.yml` | this commit | done |
| P — prompt rule 14 + `<verify>` (`script_system_v1.md`, user template, `verify_system_v1.md`), parser `ParsedScript.verify`, "Dependency versions" section from `ctx.library_hints`, `LLMFixer.verify_guide` (one call, `last_verify_usage`), review prompt policy + `<verify>` | this commit | done |
| L — `_library_hints` (errors' artifacts / owner classes → table + repository facts → prompt + INFO `의존성 버전 조회:`), read-back after every applied try and the review commit (`major_upgrades`, guide = `<verify>` → `verify_guide` → synthesized; WARNING `메이저 업그레이드: … — 운영자 확인 필요`), error list (`메이저 변경:` / `운영자 확인:`, `⚠ 메이저 업그레이드 N`), `report.operator_checks` (kept changes only), Handover block "운영자 확인 필요 (메이저 업그레이드 N건)" first, `SBMIG-RESULT … major=<n>`, flag `major-upgrade` (attention) | this commit | done |
| PM — `LazyFixer` forwards `verify_guide` / usage, `FixTry.verify_source`, root offline guard for the repository lookup in tests | this commit | done |
| Pending (the user) | — | `DB/` SQL directories: stay at the root (default) or move into `{name}-back/src/main/resources` — decided with the business team; the company KB's SiteMesh 3.2.1 case folder: delete + `sbmig kb reindex` |

## Status — 2026-10-28, v2.1e (KB learning records: why a script failed, as-is / to-be code)

User (2026-10-27): "비슷한 케이스는 스크립트로 고치고 실패하면 개선 버전을 만드는데 **왜 실패했는지 이유도 json 에**, 성공 카운트도;
코드 수정은 코드화 가망이 희박하지만 **as-is / to-be 코드**가 있으면 나중에 써먹는다; 사람이 같이 개선하는 에이전트" — snippets
whole, not capped ("사람이 보는 거면 40줄 고정 안 해도"). Contract: `docs/INTERFACES.md` "v2.1e".

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `EditHunk` / `EditNote.hunks`, `ScriptRun` / `CaseScript.parent` / `reason` / `last_failure` / `runs`, `errors.snippet_lines` 0 / `errors.script_runs` 30 | `fa19ae6`, `tests/core/test_v21e_contracts.py` | done |
| N — `sbmig/script/notes.py` hunks from the diff (`hunks_from_diff`, `attach_hunks`, notes carry as-is / to-be), `ErrorList` keeps + masks them (report copies without), `note_block` for `kb errors show` / `cases show`, review notes too | this commit | done |
| K — `CaseStore.add_script(parent, reason)` / `credit(reason, round, n, errors_*, files)` → `ScriptRun` history + `last_failure`, loop reasons (`_run_reason`), `kb reindex` rebuilds runs + backfills hunks from `errors.jsonl`, `cases show` / `cases.md` views, KB import merges lineage | this commit | done |
| PM — adapter `**optional` pass-through, package exports, blocked-flow test (v2.1d call order) | this commit | done |
| v2.1f (2026-10-28) — an error resolved by `build` without a try names the same-round try that changed its file / its module's pom (`ErrorEntry.resolved_by`, `build ← <case> r<round> try <n>` in errors.md / report / cases.md / `kb cases show`); `kb reindex` backfills old records | this commit | done |
| v2.1g (2026-10-28) — static resources all reach the front module: files of a `<resource>` dir inside the web root stay on the comp classpath AND are copied to `front_static_dir` at their web path (`layout.front_copy_resource_dirs`); non-config WEB-INF files keep their path under the front jar's `META-INF/resources/WEB-INF/` | this commit | done |

## Status — 2026-10-28, 한국어 주석 (handover comments for the business team)

User (2026-10-28): "현업이 코드에다가 한국어 주석들을 좀 달아달래 — 인수인계 받으면 다음 사람도 보면서 해야 하니까". Every
module under `sbmig/` (121 files) now opens with a Korean block (summary, place in the flow, entry points, invariants),
every public class / function and the public methods carry Korean first in their docstring, and inline Korean comments
say *why*; the English originals are kept verbatim (checked per docstring against the previous commit). Convention +
terminology: `docs/v2/CODE-COMMENTS-KO.md`; package map: `sbmig/__init__.py`; findings from reading every file:
the handover findings list of 2026-10-28 (in git history) (26 suspected bugs / policy questions, dead config keys, stale English text).

| Deliverable | Where | Status |
|---|---|---|
| Convention (Korean first, English kept, "why" comments, terminology table) + exemplar | `docs/v2/CODE-COMMENTS-KO.md`, `sbmig/kb/store.py` | done |
| Korean comments in all 121 modules (+5,547 Korean lines; code identical by AST, only four redundant `pass` lines dropped) | `sbmig/**` | done |
| `--help` of every Typer command shows the Korean summary (`sbmig`, `kb`, `fleet`, `advisories`, `recipes`) | `sbmig/cli.py`, `*/cli.py`, `plan/catalog_cli.py` | done |
| Drift test: module docstring starts in Korean, every public top-level definition has Korean | `tests/docs/test_korean_comments.py` | done |
| Findings for the user to decide (A1 EUC-KR pom skipped — reproduced; A2 highest-major verdict; A3 review call limit 2 vs 3; …) | the handover findings list of 2026-10-28 (in git history) | pending (the user) |
| Not covered: prompt `.md` files, YAML (`config/`, `knowledge/`), tests, `coding/`, `web/static/index.html` | — | by request |

## Status — 2026-10-28, v2.1h (JSP javax → jakarta + the fixes from the handover read)

User decisions on the handover findings list of 2026-10-28 (in git history): "A1 은 고쳐야겠고, A2 는 일단 이대로, A3 구조 검토는 3회로 맞추자,
A4 는 뭐가 문제인지 잘 모르겠어(설명 후 결정), A5 JSP 도 javax → jakarta 로 변경, 버그가 있는 것들은 일단 고치자".
Contract: `docs/INTERFACES.md` "v2.1h".

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `layout.jsp_jakarta_rename`, `RestructureResult.jsp_jakarta_files / _refs / _renamed`, `LLMRole` + `verify_guide`, `GAV.parse` long form (A26), `llm.provider` class default `company` (A6) | `sbmig/core/{config,interfaces,models}.py`, `config/default.yml` | done |
| A5 — `sbmig/restructure/jsp_jakarta.py` (`rename_jsp_javax`, `rename_jsp_files`: the analyzer's code regions + package table, strings / comments / taglib URIs untouched, own encoding + line ends kept, idempotent) hooked at the end of `Restructurer.apply` in every mode (note `JSP javax → jakarta: N files, M references`, commit `restructure: JSP javax -> jakarta`; off → follow-up `JSP javax references not renamed (layout.jsp_jakarta_rename: false): N files`); A24 `jsp_moved` counts moved JSPs only; `PomEditor` decodes with `pom_parser.pom_encoding`, parses without the declaration, saves with `xml_codec(declared)` (MS949 / x-windows-949 / EUC-KR + 똠 round-trip) | `sbmig/restructure/`, `tests/restructure/test_jsp_jakarta.py`, `test_pom_editor_encodings.py`, e2e post-check `servlet-nonstandard-dirs` (`report.jsp` → `jakarta.servlet.http.HttpSession`) | done |
| A1 — `pom_parser.pom_encoding / xml_codec / xml_without_declaration / read_pom_text` (BOM → strict UTF-8 → declared encoding incl. Java names `x-windows-949` → workspace candidates; EUC-KR family → cp949), `parse_pom_text` strips the declaration (also bytes), `edges.system_dependencies` / `source_scanner._xml_root` / `webxml._parse_root` share it; A25 unique service names | `sbmig/analyze/`, `tests/analyze/` | done |
| A7 (nested-pom project folders listed; a folder with no pom logged `left out`), A8 (missing input → `skipped` for every row this run would start; success rows kept), A9 (`llm_calls` / tokens / cost add up every attempt, other per-run numbers cleared per attempt; progress line `(all attempts N)`), A10 (`_FifoTurns`: uploads start in order), A11 (`::ffff:127.x` loopback) | `sbmig/fleet/`, `sbmig/web/`, `docs/FLEET.md` | done |
| A12 / A13 (`write_report` passes `cfg`; placeholder files from the same inventory), A14 (`script.reuse_versions` honoured: `stored_next` — next stored version only when every reuse of this run failed; improved / partial → augment), A20 (target from `cfg.target`: "Spring Boot 4.1.0"), A21 (role `verify_guide` typed; replay `replay_role: verify_guide`), A23 (the review's LLM calls on the first fixed critical issue only), A5 report side (step note `restructure.apply: JSP javax → jakarta: …` → "What we changed"; Handover `JSP javax references remain (N files)` unless a follow-up already says so; i18n), A3 docs (review calls 2 → 3: review + format retry + apply-failure retry; also `docs/kb-map/*`) | `sbmig/report/`, `sbmig/triage/`, `sbmig/fix/`, `sbmig/llm/`, `sbmig/loop/orchestrator.py`, `docs/ADAPTERS.md`, KB-AND-FLOW (removed; `agent-doc/04-cases-scripts-kb.md` covers it) | done |
| A15 / A16 (`_backfill_entries` → `(hunks, fixers)`, reset per `recount`, attribution independent of the hunk import; `kb reindex` prints `… H entr(ies) given as-is/to-be hunks, F build-resolved entr(ies) given a probable fixer`), A17 (`_next_version` shared by `add_script` / `merge_from`, unlisted `fix_v*.py` never overwritten), A18 (`activate` falls back to the run id like `credit`), A19 (`cases pin`: exact id or unique 4+ prefix, unknown id → exit 1 unless `--create`), A22 (`_Guarded` drops unsupported keywords by signature before the one call; a TypeError inside the callee is never retried) | `sbmig/errors/`, `sbmig/kb/`, `sbmig/loop/entries.py` | done |
| A4 — user: "C로 가자" → documented shape `fn(ws, edits, *, cfg, errors=None)` + `fixloop.call_edit_applier` passes `errors` only when the signature takes it | `sbmig/loop/fixloop.py`, `docs/ADAPTERS.md`, `tests/loop/test_v21h_edit_applier.py` | done |
| Pending (the user) | — | §B dead code / unread keys; `fixloop.py` `try_notes` / `verify_guide` still use the old TypeError-retry pattern (same shape as A22, not in scope) |

## Status — 2026-10-29, flow deck for the business contact

(The `fix.mode: script | edits` switch built in the same round, v2.1i, was removed in v2.1z — see that table.)

| Deliverable | Where | Status |
|---|---|---|
| Deck for the business contact (user, 2026-10-29: "사람이 읽는다고 생각하고… 전체 workflow 흐름도 하나랑 각 스텝별로 어떤식으로 돌아가는지와 루프 설명정도만"): 10 slides — the whole flow in one picture (titled boxes: bold title + short description), intake / analysis, plan / transform, why errors come in waves, restructure / review, the fix loop round, how one error gets fixed, the build is the judge, the KB reuse; no versions, keys, CLI, report or config slides; the four flow pictures are native PowerPoint shapes (user: "도형으로 만들어서 내가 수정 가능하게" — `node` / `seg` / `route` helpers in the build script), no image files; the unused `diagrams/v2-*` and `flow-*` sources were removed, the 37-slide design variant stays in history (`a4c3827`) | `docs/migration-agent/migration-agent.pptx`, `scripts/handbook/build_migration_agent_deck.js` | done |

## Status — 2026-10-29, v2.1j (Ant projects: pom.xml synthesized at intake)

User: "우리 ant 프로젝트도 maven 으로 변경 후 마이그레이션이 되도록 요청하시는데 … 앤트도 메이븐으로 변경하고 진행 할 수 있도록
수정하자". Contract: `docs/INTERFACES.md` "v2.1j". Trees with neither pom.xml nor build.xml stay rejected (2026-10-23).

| Deliverable | Where | Status |
|---|---|---|
| Contracts: `IntakeConfig` (`intake.*`), `JarCoordinate`, `PomSynthesis`, `RunReport.pom_synthesis` | `sbmig/core/`, `config/default.yml` | done |
| Analyzer: `describe_ant` (build.xml with properties, javac / war / jar tasks, classpaths, notes for untranslatable tasks), `resolve_jar` ladder (pom.properties → name table + `<kb>/jar_names.yml` → sha1 Nexus 3 / Central), `mark_used`, `synthesize_pom` (war / jar pom, source roots + build-helper, resources, war plugin 3.4.0 + `warSourceExcludes`, provided container APIs, system-scope unknown jars copied to `<root>/lib/` with `${maven.multiModuleProjectDirectory}`), `BuildToolDetection.ant_build_file`; a real `mvn clean package` of the synthesized pom passes | `sbmig/analyze/ant.py`, `jar_coords.py`, `build_tool.py`, `edges.py`, `tests/analyze/test_ant_synthesis.py` (19), `test_jar_coords.py` (27) | done |
| Intake wiring + report: `_synthesize_ant_pom` after the snapshot commit, commit `intake: pom.xml synthesized from build.xml`, journal, flags `pom-synthesized` / `jar-coordinates-unknown` (attention), INFO line, Handover block with `install-file` commands, "What we changed" line, i18n, `inventory --help` wording | `sbmig/loop/orchestrator.py`, `sbmig/report/{handover,markdown,i18n,result_line}.py`, `sbmig/cli.py`, `tests/loop/test_v21j_ant_intake.py`, `tests/report/test_v21j_ant_report.py` | done |
| Docs: CLAUDE.md company constraints, KB-AND-FLOW §1 note, RUNBOOK "Ant 프로젝트", deck ① 접수 wording | — | done |
| e2e fixture `ant-webapp` (build.xml + WebContent + stub jars + a real compiled unknown jar) through the whole flow | `fixtures/projects/ant-webapp/` | see the row below |

## Status — 2026-10-29, v2.1k (rootless trees: single-top aggregator, reactor de-duplication)

User: a company project (no root pom; `APP/pom.xml` aggregating `../DEV`, `./EAR`, `./BATCH`; `DEV` WAR on `../STATIC`; `DB/`)
ended with `Project … is duplicated in the reactor` after 3 LLM script augments; "루트 디렉토리 아래에 APP 디렉토리 하나만 있는
경우도 성공 못했어 — 루트에 pom 생성조차 안 되었고". Contract: `docs/INTERFACES.md` "v2.1k".

| Deliverable | Where | Status |
|---|---|---|
| Planner: aggregator also for a single top, `..\DEV` normalised, `{name}` from the listed poms + modules, unrelated groups → exit 3 `no-root-pom` at analyze (MSA excepted) | `sbmig/analyze/aggregator.py`, `sbmig/loop/orchestrator.py`, `tests/analyze/test_aggregator.py` | done |
| Reactor de-duplication at intake (`reactor_duplicates` / `dedupe_reactor`, non-root listings removed, commit + flag `reactor-duplicate-removed` + follow-up) | `sbmig/analyze/aggregator.py`, `sbmig/loop/orchestrator.py`, `tests/analyze/test_reactor_duplicates.py`, `tests/loop/test_v21k_reactor_dedupe.py` | done |
| Safety net: `BlockReason.REACTOR_DUPLICATE`, policy 0 → BLOCKED without an LLM call, suggestion in Korean | `sbmig/core/models.py`, `config/default.yml`, `sbmig/triage/blocked.py`, `sbmig/loop/blocked_registry.py`, `tests/triage/test_v21k_reactor_duplicate.py` | done |
| e2e `sibling-aggregator-dup` (company shape) + `servlet-nonstandard-dirs` PASS on real Maven; full offline suite 2,464 | `fixtures/projects/sibling-aggregator-dup/` | done |
| Pending (the user) | — | the company PC's commit (the APP+DEV aggregator seen there does not reproduce on HEAD — an older build or a path difference); delete the useless stored case for the reactor error in the company KB + `sbmig kb reindex` |


## Status — 2026-10-30, v2.1l (dependency versions: carried over from the as-is build, never guessed; gated Security chain)

User: Boot / MSA company runs and a Spring Framework 6.2.12 run ended after rounds of "add a dependency" answers, the last ones
"정책상 버전을 추정하여 추가할 수 없습니다"; "기존 pom.xml 에 쓰고 있는 것들은 전부 가져와서 넣어 주고 버전을 맞춰야 할 텐데".
Root cause: v1's `carry-over-managed-version` / `missing-dependency-version` handlers (infrastructure, not error fixes) were deleted with
the 32 handlers in v2 step 4 (`748fd0f`) and nothing took their job; rule 14 forbade the LLM to guess. Decisions (user): fix it; the LLM
structure review keeps checking the result against the company rules; Security recipes only for projects that use Spring Security
("쓰는 경우만"); Spring Cloud needs nothing (inside the Boot umbrella); MSA non-Boot WAR services stay pom-only until real projects are
checked. Contract + as-built: `docs/INTERFACES.md` "v2.1l".

| Deliverable | Where | Status |
|---|---|---|
| Restructure pins the as-is versions (`_pin_asis_versions` before `_drop_managed_versions`, four call sites; kept only when the new chain's effective pom could be captured; `versions_pinned` / `versions_unknown`, Error-list row, journal `versions_pinned`, review material) | `sbmig/restructure/restructurer.py`, `sbmig/loop/orchestrator.py` (`asis_effective` param, pom-verify row), `sbmig/core/interfaces.py`, `sbmig/review/inputs.py`, `tests/restructure/test_v21l_pin_versions.py`, `tests/loop/test_v21l_restructure_versions.py` | done (agent, 22 tests) |
| Fix loop: `VersionResolver` (managed → as-is → repository → unknown), `_fill_versions` after every applied try, zero-LLM `sbmig-version` try for `missing_version` cases, prompt facts `as-is build:` / `new parent chain:`, rule 14 "leave unknown versions null" | `sbmig/loop/fixloop.py`, `sbmig/core/models.py` (`TrySource`), `sbmig/fix/prompts/script_system_v1.md`, `tests/loop/test_v21l_versions.py` | done (agent, 20 tests) |
| Spring Security chain `6_0 … 6_5, 7_0` as the default `spring_security_7` set (planner gate: analyzer library `spring-security`, `spring_legacy` only) | `sbmig/core/config.py`, `config/default.yml`, `knowledge/recipes/catalog.yml` (`sets:` of the 7 existing entries), `docs/RECIPES.md`, tests `test_v2_contracts` / `test_planner` / `test_advisor` | done |
| e2e fixture `legacy-external-parent` (as-is parent = local stand-in `fixtures/libs/legacy-parent` managing commons-text / commons-csv) | `fixtures/projects/legacy-external-parent/`, `fixtures/libs/legacy-parent/`, `fixtures/libs/install.sh`, `tests/fixtures/test_fixture_projects.py` | PASS on real Maven: both versions pinned, BUILD SUCCESS, 1 LLM call (review), Handover ⚠ carried over |
| e2e regression: `spring5-security-batch` (the Security chain now converts `WebSecurityConfig` itself → 8 scripts instead of 9, 9 LLM calls; expected.json / README updated), `sibling-aggregator-dup`, `legacy-role-parents` (boot_pom), `boot2`, `msa` | `out/e2e/summary.md` | all PASS |
| Docs: CLAUDE.md, ERROR-HANDLING X8, KB-AND-FLOW §7.1 (Korean), RECIPES, INTERFACES as-built | — | done |
| `/code-review medium` on the diff: 10 findings → 7 fixed (unknown entries resolved from the repository before the AFTER capture so one unknown no longer undoes every pin; managed-before-direct as-is lookup; no capture when nothing pinned; kept-newer pins reported as pins; zero-LLM try after the deferral + `_version_deferral`; `_version_tried` only after a successful apply, cap 3 failed applies; planner docstring), 3 recorded as follow-ups (newest-any-major for added deps = policy, duplicated helpers, raw-byte idempotency check) | `docs/INTERFACES.md` "v2.1l" → As built | done |
| Offline suite | `python -m pytest -m "not integration"` | **2,525 passed**, 45 deselected (after the review fixes; was 2,464 before v2.1l) |
| Open (the user) | — | company data for the Boot / MSA failures and for the Spring Framework 6.2.12 project that kept `DEV/pom.xml` (report.md `\| Case \|` + `plan: restructure:` note); retest from an empty KB; MSA non-Boot WAR services; the known limit of `_fill_versions` without the post-restructure capture (INTERFACES "As built") |

## Status — 2026-10-30, v2.1m (binary files under resource filtering: `nonFilteredFileExtensions` written by sbmig)

User: with an empty KB a project whose filtered resources hold `.dmdb` files (and webfonts etc.) failed in maven-resources-plugin's
copy; the LLM wrote three different scripts for the same error and hit the cap ("3회로는 너무 부족", now 15 at the company). v1's
handler E12 had fixed this pre-emptively; v2 step 4 deleted it. Contract + as-built: `docs/INTERFACES.md` "v2.1m".

| Deliverable | Where | Status |
|---|---|---|
| Shared rule: plan (analyzer scan: known extensions + binary content, includes / excludes) + apply through `PomEditor` + reactive targets | `sbmig/restructure/resources_filtering.py`, `tests/restructure/test_v21m_resources_filtering.py` | done (agent, 19 tests) |
| Restructure: `_non_filtered_extensions` before `_pin_asis_versions` in layout / boot_pom / msa, `RestructureResult.non_filtered_extensions`, pom-verify row, journal `non_filtered_extensions`, `build.non_filtered_extensions` | `sbmig/restructure/restructurer.py`, `sbmig/core/{config,interfaces}.py`, `config/default.yml`, `sbmig/loop/orchestrator.py` | done (agent, 19 tests) |
| Fix loop: zero-LLM try `sbmig-resources` for the plugin error (after the deferral, like `sbmig-version`) | `sbmig/loop/fixloop.py`, `sbmig/core/models.py` | done (agent, 6 tests; `_pre_tries()` registry shared with `sbmig-version`) |
| e2e fixture `filtered-binary-resources` (filtered `src/main/resources` with `fonts/app.woff2` + `data/items.dmdb`; as-is pins resources plugin 3.1.0, which copies the binaries silently corrupted) | `fixtures/projects/filtered-binary-resources/` | PASS on real Maven: `dmdb, woff2` written into `binres-back/pom.xml`, both binaries byte-identical, `catalog.version` filtered, BUILD SUCCESS, 1 LLM call |
| Docs: CLAUDE.md, ERROR-HANDLING E12, KB-AND-FLOW restructure row, INTERFACES contract | — | done |

## Status — 2026-10-30, v2.1n (optional in-house artifacts applied when a trigger dependency is present)

User: "옵셔널 artifacts.yml 의 의존성에 해당하는 의존성이 pom.xml 에 있으면 그 옵셔널을 적용하는 완화책" (e.g. `hibernate-core` → the
company's JPA starter), next to the existing "used → aligned to the 4.0.0 line". Contract + as-built: `docs/INTERFACES.md` "v2.1n".

| Deliverable | Where | Status |
|---|---|---|
| `ArtifactSpec.when_present` (g:a keys / globs) + `replaces` (default true); `_apply_optional_triggers` before `_bump_optionals` in `_all_poms_pass` (every mode, MSA library roots too); `RestructureResult.optional_added` / `optional_replaced`, pom-verify row, journal `optional_triggers`, Handover reason `optional (trigger: …)`, review lines | `sbmig/core/config.py`, `sbmig/restructure/restructurer.py`, `sbmig/core/interfaces.py`, `sbmig/loop/orchestrator.py`, `sbmig/report/handover.py`, `sbmig/review/inputs.py`, `tests/restructure/test_v21n_optional_triggers.py` | done (agent, 21 tests) |
| Company example (`when_present` / `replaces` documented) | `config/artifacts.example.yml` | done |
| e2e: `legacy-external-parent` gains the trigger scenario (commons-lang3 added next to commons-text, `replaces: false`) | `fixtures/projects/legacy-external-parent/{sbmig.e2e.yml,expected.json}` | PASS on real Maven (commons-lang3 3.17.0 added to `extparent-back/pom.xml`, journal `optional_triggers`, Handover "optional (trigger: commons-text)"); 10-fixture e2e subset all PASS |
| Note | — | `optional_version_policy: floor` keeps an as-is version newer than the configured one; `pin` forces the configured version (the user decides per company yml) |
| Offline suite after v2.1m + v2.1n | `python -m pytest -m "not integration"` | **2,578 passed**, 45 deselected (was 2,525 after v2.1l) |

## Status — 2026-10-30, v2.1o (no build before the restructure; Boot BOM facts without Maven; as-is version properties)

User: migrated poms showed Boot `4.0.9` (OpenRewrite's `4.0.x`) or the as-is `2.5.15` although the company parent carries 4.1.0 /
Spring 7.0.8 — "Boot 레시피 실행 다 하고 구조화를 다 한 후에 빌드를 시작해야 맞을 거 같다". Contract + as-built: `docs/INTERFACES.md` "v2.1o".

| Deliverable | Where | Status |
|---|---|---|
| `plan.compile_after_rewrite` (default **false**: rewrite → restructure → review → verify.build, no compile on the half-migrated tree; `true` = the v2.1 step); plan note either way; existing plan tests keep the v2.1 order through an autouse fixture, one test for the default | `sbmig/core/config.py`, `config/default.yml`, `sbmig/plan/planner.py`, `tests/plan/conftest.py`, `tests/plan/test_planner.py` | done |
| `sbmig/build/boot_bom.py` — `BootBomFacts` from `spring-boot-dependencies:<target>` (+ its imported BOMs, one level) via the local repo / repository; `as_capture()` | `sbmig/build/boot_bom.py`, `tests/build/test_boot_bom.py` | done (agent, 7 tests) |
| Restructure: BOM facts as the fallback managed source for `_drop_managed_versions` and the v2.1l pins when the capture is missing (no undo then); `_drop_bom_property_overrides` removes as-is `<properties>` that override the parent chain's Boot BOM properties (`spring-boot.version=2.5.15` …), kept when still referenced; `layout.drop_bom_property_overrides`; `RestructureResult.bom_properties_removed`; pom-verify rows; journal `bom_property_overrides` | `sbmig/restructure/restructurer.py`, `sbmig/core/{config,interfaces}.py`, `sbmig/loop/orchestrator.py`, `tests/restructure/test_v21o_bom_facts.py` | done (agent, 9 tests; property names from the BOM facts; + `version_properties_removed` for properties left unused by the version drop) |
| Docs: KB-AND-FLOW (`transform.compile` row), BREAKPOINTS, CLAUDE.md flow line, INTERFACES contract | — | done |
| `openrewrite.recipes.spring_framework_7` = `UpgradeSpringFramework_7_0` as a default set for `spring_legacy` (user: "넣는 게 좋을 것 같아"); catalog `sets`; plan / advisor / contract tests updated | `sbmig/core/config.py`, `config/default.yml`, `knowledge/recipes/catalog.yml`, `docs/RECIPES.md`, `tests/plan/*`, `tests/core/test_v2_contracts.py` | done |
| e2e: full fixture set on the new default (no compile step) + `spring5-security-batch` expectations (the property overrides now removed by the restructure, review critical 0) | `out/e2e/summary.md` | full e2e (36 fixtures) on the new defaults: 32 PASS at once, 4 expectations updated to the new behaviour (`spring5-xml` 0 scripts / 1 LLM — the Framework 7 recipe converts the adapters; `servlet-nonstandard-dirs` and `spring5-security-batch` no review critical / no script — the restructure writes the extensions and removes the overriding properties; `boot2-qlrm-javax` replay gained the `<verify>` block) and the `legacy-external-parent` recipe-raised 7.0.10 fixed in code; rerun of the 10 affected fixtures all PASS |
| Offline suite after v2.1o | `python -m pytest -m "not integration"` | **2,596 passed**, 46 deselected (was 2,578 after v2.1n) |

## Status — 2026-10-30, v2.1p (the company parent chain's facts come from its poms)

User: "layout 을 parent pom 파일에서 읽어 와서 세팅해야 하는 거 아냐? … 고정으로 박으면 이상하지 않아?" / "dmdb 도 하드코딩은 꼼수잖아?"
Contract + as-built: `docs/INTERFACES.md` "v2.1p". The `.dmdb` detection is content-based already (NUL bytes / not decodable), so the
config list stays empty; `optional_version_policy` is moot when the optionals carry `version: null` (the parent manages them).

| Deliverable | Where | Status |
|---|---|---|
| `sbmig/build/parent_chain.py`: facts of the configured parents (`artifacts.parent`, `boot_parent`, `comp_parent`) through their `<parent>` chain and BOM imports — property names (child wins) + managed keys (first definition wins); the Boot BOM added when the chain misses it; `as_capture()` | `sbmig/build/parent_chain.py`, `sbmig/build/boot_bom.py` (shared helpers), `tests/build/test_parent_chain.py` | done (agent; 4 + 16 tests) |
| Restructure uses the chain facts for the fallback managed source and the override names; `layout.parent_property_names` default `[]` (escape hatch); journal `parent_chain_facts`; pom-verify row names the defining pom | `sbmig/restructure/restructurer.py`, `sbmig/core/config.py`, `config/default.yml`, `sbmig/loop/orchestrator.py`, `tests/restructure/test_v21o_bom_facts.py` | done (agent; 4 + 16 tests) |
| Docs: CLAUDE.md, INTERFACES | — | done |
| Verification | offline suite + e2e subset | e2e 8 fixtures (legacy-external-parent, spring5-security-batch, boot2, legacy-role-parents, msa, filtered-binary-resources, company-boot-single, boot2-qlrm-javax) all PASS; offline suite **2,598 passed**, 46 deselected |

## Status — 2026-10-30, v2.1q (embedded Tomcat wording; LLM-pinned managed versions removed)

| Deliverable | Where | Status |
|---|---|---|
| Executable-WAR wording (embedded Tomcat first, external as the alternative) in the restructure follow-ups + CLAUDE.md; verified by running the e2e WAR with `java -jar` (servlet + JSP 200) | `sbmig/restructure/restructurer.py`, `tests/restructure/test_{ear_sibling_poms,merge_internal_overlays,company_layout}.py`, `CLAUDE.md` | done |
| `_fill_versions` removes an explicit version the LLM wrote on an ADDED dependency the chain manages (spring-webmvc 7.0.0 → managed) | `sbmig/loop/fixloop.py`, `tests/loop/test_v21l_versions.py` | done (agent, 4 tests; the parent-chain facts are also the loop's step-1 fallback when the post-restructure capture is missing) |
| Verification | offline suite + e2e subset | e2e 7 fixtures (sibling-aggregator-dup, ear-sibling-poms, servlet-war-overlay, company-servlet-jsp, boot2-qlrm-javax, legacy-external-parent, boot2-fix-failures) all PASS; offline suite **2,608 passed**, 46 deselected |

## Status — 2026-10-30, v2.1r (Windows file locks on `target/`: retry the build, never ask the LLM)

User: a company run showed `Error assembling WAR … FileSystemException … 다른 프로세스가 파일을 사용 중` build after build, the loop asked the
LLM 15 times (한도 도달 → BLOCKED), and the final build passed once the lock was gone. Contract + as-built: `docs/INTERFACES.md` "v2.1r".

| Deliverable | Where | Status |
|---|---|---|
| Maven runner: file-lock patterns (`knowledge/config/build_env_errors.yml` + built-in) → wait `build.file_lock_retry_wait_s`, rerun up to `build.file_lock_retries`, journal `build_file_lock_retry` | `sbmig/build/runner.py`, `sbmig/core/config.py`, `config/default.yml`, `tests/build/test_runner_file_lock.py` | done (agent; 4 + 4 + 1 tests; `error_stage()` None for a lock so no false regression) |
| Triage: `BlockReason.FILE_LOCK`, `loop.blocked_policy.file_lock: 0`, Korean suggestion, `attn=infra:file-lock` | `sbmig/triage/blocked.py`, `sbmig/core/models.py`, `sbmig/report/{result_line,i18n}.py`, `tests/triage/test_v21r_file_lock.py` | done (agent; 4 + 4 + 1 tests; `error_stage()` None for a lock so no false regression) |
| Verification | offline suite + e2e subset | same run as v2.1q: e2e 7 fixtures PASS; offline suite **2,608 passed**, 46 deselected |

## Status — 2026-10-30, v2.1s (a version the new chain does not manage follows the TARGET Boot BOM; warning when the parent chain has no Boot BOM)

Company journal (78c2f2c): `parent_chain_facts … poms: 5, managed: 1872, properties: 206, boot_bom_added: true, unreadable: []` — the
company parent chain (xxx-parent → boot-jar / comp-jar) imports no Spring Boot BOM, so `spring-boot.version` is a property without effect
and the migrated poms keep mixed explicit Boot versions; a pin row read `jakarta.servlet-api 4.0.3 ← as-is managed by …` where the to-be
line (Boot 4.1, Servlet 6.1) should win ("as-is 를 보면 안 되고 to-be 를 봐야 해"). Contract + as-built: `docs/INTERFACES.md` "v2.1s".

| Deliverable | Where | Status |
|---|---|---|
| Resolution order: chain-managed → target Boot BOM (`boot-bom`, as-is kept when newer) → as-is → repository → unknown, in the restructure pins and in the loop's `VersionResolver`; rows / journal name the BOM | `sbmig/restructure/restructurer.py`, `sbmig/build/parent_chain.py` (`managed_by_chain`), `sbmig/loop/fixloop.py`, tests | done (agents; 7 restructure/report tests + 3 loop tests) |
| Warning `parent-chain-no-boot-bom` (flag, note, journal, Handover's first follow-up in Korean, `attn=`) when `parent_manages_spring_boot` is true but the chain carries no Boot BOM | `sbmig/restructure/restructurer.py`, `sbmig/report/{handover,result_line,i18n}.py` | done (agents; 7 restructure/report tests + 3 loop tests) |
| Company decision (the user / the parent's owners): add `spring-boot-dependencies:${spring-boot.version}` (import) to base-parent, or set `parent_manages_spring_boot: false` until then | company artifacts yml / base-parent | open |
| Verification | offline suite + e2e subset | e2e 8 fixtures (legacy-external-parent, spring5-security-batch, boot2, legacy-role-parents, msa, company-boot-single, boot2-qlrm-javax, filtered-binary-resources) all PASS; offline suite **2,617 passed**, 46 deselected (the one failure was the loop's `chain_own_managed` field-name test, fixed right after — `tests/loop` rerun green) |

## Status — 2026-10-30, v2.1t (BOM imports and in-house versions reference the parent chain's own properties)

User (no rights on base-parent; property names confirmed: `spring-boot.version`, `spring-framework.version`, `spring-cloud.version`,
`spring-data.version`): "어차피 에이전트 기동할 때 pom.xml 리드 가능하잖아? 그걸 불러와서 세팅하면 더 유연하지 않을까 … 사내 라이브러리 버전은 네이밍이랑
변할 수 있으니까 … 안 되면 spring 쪽만". Contract: `docs/INTERFACES.md` "v2.1t".

| Deliverable | Where | Status |
|---|---|---|
| `artifacts.parent_manages_spring_boot: auto` (chain facts decide), Boot BOM import with `${spring-boot.version}` (chain property, literal only as the last resort), Spring Cloud BOM import when used (`${spring-cloud.version}` / `knowledge/config/spring_cloud_bom.yml` pairing), in-house mandatory / optional versions as `${<artifact>.version}` when the chain defines it | `sbmig/restructure/restructurer.py`, `sbmig/core/config.py`, `config/{default,artifacts.example}.yml`, `knowledge/config/spring_cloud_bom.yml`, `fixtures/libs/legacy-parent`, `tests/restructure/test_v21t_bom_imports.py` | done (agent, 13 tests; Cloud pairing verified 4.1 → 2025.1.4) |
| Loop: chain `*.version` properties as prompt facts, rule 14 "`${property}` instead of a literal", `_fill_versions` writes `${prop}` for added in-house dependencies | `sbmig/loop/fixloop.py`, `sbmig/fix/prompts/*.md`, tests | done (agent, 4 tests) |
| Verification | offline suite + e2e subset | e2e: first run caught `boot2` writing `${lombok.version}` from the Boot BOM's properties → rule narrowed to company-defined properties; rerun of boot2, legacy-external-parent, msa-shared-lib, company-boot-single, boot2-cloud-legacy, spring5-security-batch all PASS; offline suite **2,640 passed**, 46 deselected |

## Status — 2026-10-31, v2.1u … v2.1y (the prompt-review follow-through, one weekend round)

User: "쭉 일자로 진행하자! 주말에 해서 월요일에 가서 돌려보면 되니까!" after the review the prompt review of 2026-10-31 (in git history) (commit `0051132`).
The seven §7 decisions were taken as PM defaults, each a config switch or a documented rule (table at the top of
`docs/INTERFACES.md` "v2.1u … v2.1y"). Six parallel opus agents on disjoint directories; the PM wrote the shared fields first and
integrated. Contract + "As built": `docs/INTERFACES.md` "v2.1u … v2.1y".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| v2.1u text | Fix prompts: `{{ target }}` rendered (no version literal in any prompt file, test), rule 8 → `jackson-2-3.md`, rule 10 note `fix-loop:` sections, rule 14 rewritten (2,130 → 1,397 chars, fact order chain → target BOM → as-is → `null`), `<blocked>` instead of the free `<analysis>`-only exit, B-L2 deletions (script prompt 16,978 → 15,918 chars), header `as-is (before sbmig):`, plan-note prefix filter | `sbmig/fix/prompts/*`, `sbmig/fix/fixer.py`, `sbmig/fix/context.py`, `tests/fix/test_v21u_prompts.py` (27) | done (agent P1; tests/fix 217) |
| v2.1u text | Review prompt: executable-WAR packaging rule, MSA rule as the code applies it, mandatory `→ <dependencies>` / `→ <dependencyManagement>` per spec, optional wording v2.1n/t, "version decisions are final", starter line, user prompt asks `<analysis>/<solution>/<edits>` | `sbmig/review/prompts/review_v1.md`, `sbmig/review/inputs.py` | done (agent R) |
| v2.1u text | Notes: W1–W12 + O2–O16 corrected against the local jars, `servlet-to-boot.md` rewritten (executable WAR, A-B1 / A-B2), `boot4-packages.md` split out for every case, `fix-policy.md` deleted, v1 handler wording purged (test), literal versions → `version: null` / Dependency versions, keyword gaps + false positives | `knowledge/notes/**`, `knowledge/libraries/advisories.yml`, `tests/fix/test_v21u_knowledge.py` | done (agent N; `advisories check` OK 67) |
| v2.1v facts | `FixContext.layout_facts` → section "Project after the restructure" (never truncated); `_verdict_line` no `<no_script>`; repository verdicts only for `javax.` cases; chain-/BOM-managed libraries get one line; version / chain-property facts also for located classpath GAVs; `managed artifacts under <group>` for missing packages; other-errors cut at a line; `introduced by script:…` hint; note scoring 3 / 1 and truncation order | `sbmig/loop/layout_facts.py` (new), `sbmig/loop/fixloop.py`, `sbmig/build/repo_versions.py`, `sbmig/fix/context.py`, `tests/loop/test_v21v_y_prompt_facts.py` (19) | done (agents P2 + P1) |
| v2.1v facts | Review: `version_fact_lines` (chain properties, explicit managed versions, Boot artifacts on another line / shadowing properties = critical, container API `compile`, repackaged module as a dependency), Boot rule from the effective value, every new `RestructureResult` field rendered, run notes de-duplicated, `logs/review-input.md`, offline render test | `sbmig/review/inputs.py`, `sbmig/review/reviewer.py`, `tests/review/test_v21v_inputs.py` | done (agent R) |
| v2.1w notes + recipes | `jackson-2-3.md` (rules A–D), `spring-batch-6.md`, Spring 7 removals, Spring Data 1.x → 2+, Security 3/4 encoding, commons-lang 2 / log4j 1 sections; recipe sets `spring_batch_6` / `hibernate_7` (used-only, like Security) and `servlet_jakarta_10` (every `servlet_tomcat`), catalog `sets:` + gates, `recipes check --jars` OK | `knowledge/notes/**`, `sbmig/plan/planner.py`, `sbmig/plan/catalog.py`, `knowledge/recipes/catalog.yml`, `tests/plan/test_v21w_recipe_sets.py` (14), `docs/RECIPES.md` | done (agents N + K; spring5-security-batch 9 → 7 LLM calls) |
| v2.1x conversion | `layout.legacy_context.*` (14 switches): entry XML only + no test XML (D2), overriding / circular properties (D1), byte copy of EUC-KR XML (D3), skipped-filter init-params → Boot properties and `requires` / `otherwise` rows (D4 / D11), JNDI `MIGRATION-TODO` + sentence (D5), `/WEB-INF/…` locations → `classpath:asis-webinf/…` and WEB-INF property files count as definitions (D6), `${profile}` globs when exactly one file matches (D7), default servlet (D8), XML `CommonsMultipartResolver` → `spring.servlet.multipart.*` (D9), `@EnableWebMvc` / `<mvc:resources>` (D10), Handover sentences (D12), as-is `provided` scopes restored (E-M6), webmvc starter by web evidence (E-M9), `project.build.sourceEncoding` written by the restructure (D-M7), stale "external Tomcat" wording | `sbmig/restructure/legacy_context.py` + `legacy_apply.py` (new), `restructurer.py`, `webxml_registrations.py`, templates, `knowledge/config/{web_xml_rules,legacy_context_rules,externalized}.yml`, `sbmig/analyze/properties.py`, `tests/restructure/test_v21x_legacy_context.py` (23), `tests/analyze/test_v21x_properties.py` (4) | done (agent X; tests/restructure + analyze 704) |
| v2.1x fixture | `spring5-legacy-startup` (every pattern of the contract; as-is JDK 8 `-Dfile.encoding=MS949`); `euckr-legacy` now gets its encoding from the restructure (review `ok`) | `fixtures/projects/spring5-legacy-startup/**`, `fixtures/projects/euckr-legacy/**`, `fixtures/projects/README.md` | done (e2e `exit=0 build=ok llm=1 review=ok`, 29 post-checks) |
| v2.1y policy | Declined answers → BLOCKED after `script.max_declines_per_case` (classifier's reason kept, else `needs_owner`; siblings "same cause as …" without a call); idempotency second run → `non_idempotent` versions never reused; review invariant check after the edits (restore + one retry); encoding check per in-project chain; unsupported review issues marked, never applied, never in the Handover; edits-only review (`review.ask_script: false`, prompt 12,403 → 11,431 chars); `issues_not_contain` in `expected.json`; attn code `owner-decision` | `sbmig/loop/fixloop.py`, `sbmig/errors/cases.py`, `sbmig/review/{reviewer,parse,inputs}.py`, `sbmig/report/*`, `scripts/e2e.py`, 9 fixtures' review replays, `tests/loop/test_v21v_y_prompt_facts.py`, `tests/review/test_v21y_review_policy.py`, `tests/report/test_v21y_*.py` | done (agents P2 + R; legacy-ibatis 10 → 2 LLM calls, boot2-javax-lib 5 → 3, missing-artifact 3 → 2) |
| Verification | offline suite + real-Maven e2e (all fixtures) | `scripts/e2e.sh`, `out/e2e/summary.md` | offline **2,786 passed**, 47 deselected; e2e **37/37 PASS** (34 build, 3 intended BLOCKED: boot2-javax-lib / missing-artifact / legacy-ibatis — now 3 / 2 / 2 LLM calls); `recipes check --jars` OK (43), `advisories check` OK (67) |

Integration notes: the PM applied the agents' requests in shared files (`CaseScript.non_idempotent`, `issues_not_contain` in the fixture
schema, the orchestrator's unsupported-issue note and the two new pom-verify rows, the new fixture in the fixture lists, the synthetic
blocked-scenario report of `tests/fixtures/test_e2e_script.py` aligned with the declined flow). Known limits: no fixture calls a removed
Servlet 6 API or native Hibernate (the two new recipe sets are covered by unit tests only); the e2e corpus still lacks Sitemesh / Tiles and
a `WebApplicationInitializer` fixture beyond the sentences `spring5-legacy-startup` asserts.

## Status — 2026-11-02, v2.1z (the v2.1i switches removed, generous ladder caps, reactor artifactIds masked in case keys)

User (2026-11-02): the token-comparison switches go (`fix.mode: edits`, `--no-kb`); the ladder caps are set generously and lowered
later ("여유롭게 잡고 진행하다가 점차 줄여가는 방식"); a case key must not carry a project's module name. Four parallel agents on disjoint
directories (A ladder, B switches, C docs, D case keys); the PM integrates. Contract + "As built": `docs/INTERFACES.md` "v2.1z".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Switches removed | `fix.mode` / `fix.edits_max_asks_per_case` deleted (`fix:` keeps `plugin` / `plugin_options`), `FixContext.fix_mode` / `RunReport.fix_mode` deleted, edits-mode ladder (`plan_edits_step`, `edits_llm_limit`, `edits:<case>#<n>` labels) gone; `migrate(kb=False)` and `NullKnowledgeBase` stay, the script ladder's `llm-edit` (answer without a script) stays | `sbmig/core/`, `sbmig/triage/`, `sbmig/loop/fixloop.py`, `sbmig/fix/`, `sbmig/report/` | done |
| Switches removed (operators) | `sbmig run --fix-mode` / `--no-kb`, `sbmig fleet run --fix-mode` / `--no-kb`, `fleet-config.json` `overrides.fix.mode` / `no_kb`, the fleet `settings:` note / summary header, web `POST /api/runs?fix_mode=&no_kb=` + the two checkboxes + the run-row fields, the report's `fix mode` line, ` fix=<mode>` in `SBMIG-RESULT` | `sbmig/cli.py`, `sbmig/fleet/`, `sbmig/web/`, `sbmig/report/result_line.py` | done |
| Ladder caps | `script.max_augments_per_case` 3 → **15** and augment steps only; new `script.max_llm_tries_per_case` **20** (new + augment, then BLOCKED `repeated_failure`); used-up augments → a fresh `new`; both plain caps (0 = never); `ScriptStep.augment_limit`; log lines `(3/20, 보강 2/15, v2 기준)` / `보강 한도(15) 소진 → … (17/20)` / `한도 도달 (저장본 1 + LLM 20회)` | `sbmig/core/config.py`, `config/default.yml`, `sbmig/triage/{scripts,decide}.py`, `sbmig/loop/fixloop.py` | done |
| Reactor masking | every reactor artifactId of the log (`< g:a >` headers, `@ <artifactId>` mojo markers, `on project <a>`) masked as `<module>` in L1 / L2 (`set_reactor_modules`, replaced per parse, seeded from the pom tree via `reactor_artifact_ids_from_poms`; the error's own module always, other names only when artifactId-like — separator / digit / uppercase —, ≥ 3 chars and not in the stoplist of ordinary words; longest first); `enforcer\|requirejavaversion: pricing-core must be built with` → `… <module> must be built with` | `sbmig/build/signatures.py`, `sbmig/build/log_parser.py`, `sbmig/build/runner.py` | done |
| Review follow-ups (same day) | two read-only opus reviews of `7674d61` (state / records / compatibility; loop logic / masking): no script-mode path cut, old run files load; fixed — `FixContext.fix_mode` / `previous_edits` read-only compat properties, `load_config` warnings for the removed keys and the changed `max_augments_per_case` meaning, `fleet resume` names dropped switches, own module always masked + artifactId-like rule + pom-tree seeding, escalation from the third ask (fresh `new`s after the augments too), cap-0 wording, restored rule-14 / usage-section test guards, stale comments; contract: INTERFACES "Review follow-ups (v2.1z)" | `sbmig/core/{interfaces,config}.py`, `sbmig/fleet/cli.py`, `sbmig/build/{signatures,log_parser,runner}.py`, `sbmig/rewrite/runner.py`, `sbmig/cli.py`, `sbmig/loop/fixloop.py`, `sbmig/triage/scripts.py`, tests | done |
| Docs | INTERFACES "v2.1z" (+ "removed in v2.1z" note on v2.1i, v2 ladder rows annotated), this table, CLAUDE.md ladder / follow-ups, README, CASE-AND-STATE-KO §A.4 / A.8 / A.9 / A.11 / A.12 / A.14 / B, KB-AND-FLOW §2 (KB matching findings) / §5 / §6, RUNBOOK §0.5, FLEET, PLAN-v2.1 limits row | `docs/**`, `CLAUDE.md`, `README.md` | done (agent C) |
| Rootless tree, lone aggregator (user bug, same day) | `APP/pom.xml` aggregating `./WEB` / `./EAR` beside an unlisted, unlinked pom (`APP/BATCH`, `DEV/…`) aborted `no-root-pom` ("top-level poms not linked"); now the only aggregator among the top poms is the root — linked tops join it, the rest are left out and named in the Handover (`poms left out of the generated root (…)`); ≥ 2 unlinked aggregators still stop with a reason naming them; fixture `rootless-unlisted-module` | `sbmig/analyze/aggregator.py`, `sbmig/loop/orchestrator.py` (`_intake_aggregator`), `tests/analyze/test_aggregator.py`, `fixtures/projects/rootless-unlisted-module/` | done |
| Hollow reactor (user bug, same day, v2.1z-c) | a WAR / jar reactor whose declared source · resource · web roots hold no sources while the tree holds them elsewhere (`APP/WEB/pom.xml` packaging a JDeveloper workspace in `DEV/`, static files in `STATIC/`) used to end as `success` with pom.xml / web.xml moved and nothing migrated; `sbmig/analyze/hollow.py` counts java / JSP / web files inside the poms' roots (defaults + `<sourceDirectory>` / `<resources>` / `warSourceDirectory` / `<webResources>`, `${…}` from the in-repo parent chain, unresolved expressions recorded, test roots and build output ignored, no directory name interpreted) vs outside; `hollow` → flag `hollow-reactor`, journal `hollow_reactor`, Handover first line (`the Maven reactor compiles no sources: …`, Korean via PATTERNS), success false, exit 2, `attn=hollow-reactor`; `sources_outside` (more java outside than inside) → `sources-outside-reactor` + follow-up only; fixture `hollow-reactor` | `sbmig/analyze/hollow.py`, `sbmig/loop/orchestrator.py` (`_check_hollow`, success rule), `sbmig/report/{result_line,markdown,handover,i18n}.py`, `tests/analyze/test_hollow.py`, `tests/loop/test_hollow_reactor.py`, `fixtures/projects/hollow-reactor/` | done |
| ADF projects are not converted (business decision, same day, v2.1z-d) | about ten of the company's first fifty are Oracle JDeveloper / ADF (`DEV/s10` with 1,546 java, `s10.jpr`, `adfm.xml`); such a run must end with **"ADF 프로젝트라 변환이 불가능합니다"** and nothing else is planned. Strict, auditable decision (`sbmig/analyze/adf.py`): four evidence families — `bc4j` (BC4J XML in the `bc4j` namespace), `java` (`oracle.jbo|adf|binding` imports), `model` (`adfm.xml` / `DataBindings.cpx` / `*PageDef.xml`), `faces` (ADF Faces pages with the `adf/faces` namespace, `adfc-config.xml`, bounded flows in the controller namespace) — **two or more present → rejected** (`is_adf`), one → `adf-traces` (note, the run goes on), `.jpr` names / `adf-config.xml` are hints only; `target/`, VCS / IDE dirs ignored, archives never opened. `ProjectProfile.adf` (`AdfSummary`: families, evidence paths per family (capped), counts: entities + tables, VOs, AMs, pageDefs, task flows / views, ADF pages, java total / on ADF, by directory). `intake.adf_policy: stop` (default) ends the run at analyze (exit 3, flag `adf-project`, journal `adf_project` with the evidence, Handover first line, `## ADF sizing` / `## ADF 규모` with "판정 근거 / evidence", `attn=adf-project`, no Maven / LLM call); `continue` = report only, the run goes on (not recommended); `sbmig inventory` prints the same block; fixture `adf-jdev` (all four families) | `sbmig/analyze/adf.py`, `sbmig/core/{models,config}.py`, `sbmig/analyze/{analyzer,webapp_inventory}.py`, `sbmig/loop/orchestrator.py`, `sbmig/report/{markdown,handover,i18n,result_line,inventory}.py`, `sbmig/cli.py`, tests, `fixtures/projects/adf-jdev/` | done |
| jakarta build discovery (v2.1z-e, user, same day) | a commercial javax library the LLM declared "no jakarta build" for had `2.0.5-jakarta` in Nexus under the same coordinates — the run's lookup had fallen back to `~/.m2` silently and asked one mirror only. Now: the repository SET (explicit `build.repository_url` first; then the settings mirrors by Maven's `mirrorOf` rules, active-profile / pom repositories unless mirrored, the global settings, Central), per-URL outcomes (`urls_tried`, `remote repositories unreadable: …` in hints / Handover), discovery of jakarta builds (same coordinates incl. higher majors verified by a `-jakarta` qualifier / pom / bounded jar scan, table `jakarta_key`, Nexus search by stem, sibling-name probes `grid8-jakarta` / `grid9` / `com.example.grid9:grid9`), owner-class → dependency fallbacks (package tokens, local jars) with `library_mapped` / `library_unmapped` / `library_lookup` journal events, the zero-LLM pre-try **`sbmig-jakarta`** (a higher major applied as the one justified major change, ⚠ operator line with sbmig's guide), a "no jakarta build" decline rejected when a build was found, the search record on the blocked item, `sbmig repo-probe`; `libraries.discover_jakarta_builds: false` is the kill switch (v2.1d behaviour); fixture `servlet-javax-grid` + libs `grid8` — `boot2-javax-lib` pins the switch off in its `sbmig.e2e.yml` (its local libs hold jakarta builds 2.0.0 / 3.0.0 that the pre-try would otherwise apply; it keeps the decline → BLOCKED path under test) | `sbmig/build/{jakarta_discovery,repo_versions}.py`, `sbmig/loop/fixloop.py`, `sbmig/core/{config,models,jakarta_libraries}.py`, `sbmig/cli.py`, `sbmig/report/i18n.py`, `knowledge/`, `fixtures/libs/grid8`, `fixtures/projects/servlet-javax-grid`, tests | done |
| Ranked javax owners (first diagnosis card) | the blocked suggestion / dependency mapping used the FIRST javax hit's owner (`org.slf4j.Logger`), not the vendor class that requires javax (`DEXT5Handler`-like); hits are ranked (javax-requiring first, neutral logging / JDK / Spring last), `BlockedCandidate.owner_classes` carries all, the loop maps through them in order | `sbmig/triage/blocked.py`, `sbmig/loop/fixloop.py`, `tests/triage/test_blocked.py` | done |
| Oscillation limit (user, same day) | the A↔B toggle guard blocked a `FilterConfig` case after 2 toggles while the ladder allows 20 asks; the threshold (hard-coded 2) is now `loop.oscillation_limit` (default 2; the company raises it in its yml — `oscillation_window` is the comparison range, not the limit) | `sbmig/core/config.py`, `config/default.yml`, `sbmig/loop/fixloop.py`, `tests/loop/test_blocked_flow.py` | done |
| Stray pom copies (user bug, 2026-11-03, v2.1z-h) | the first error after a good run of company projects: the input zip held `boot/bin/` — a whole copy of the boot module (src, .project, pom.xml, same coordinates); the pom scan read it as a project pom, the structure review registered it as an "unlisted module" (`review: fix critical structure issue (1)`) and Maven refused the reactor (`Project 'xxx-boot:1.0.0-SNAPSHOT' is duplicated in the reactor` → `reactor_duplicate`, 0 LLM tries, run over). Three layers: `stray_pom_copies` (an UNLISTED pom whose g:a equals another pom's, nested under it or beside a reactor-reachable one) is dropped from `parse_poms` with a `pom ignored (stray copy)` warning, so analysis / aggregator / review / restructure / Handover agree; the intake (`_intake_stray_poms`, first in `_intake_layout`) puts the copy's directory into `Workspace.ignored_dirs` (walk / glob / PathView / hollow / ADF scans prune it like `target/`), flag `stray-pom-ignored`, journal `stray_pom_ignored`, one Handover line `pom copy ignored: …` (Korean pattern); the review's invariant check gains rule (e) `reactor_duplicate_lines` — an edit that lists a copy puts two reactor poms on one g:a → the edit is restored and the LLM told why. Nothing is deleted or committed. | `sbmig/analyze/pom_parser.py` (`StrayPom`, `stray_pom_copies`, `find_stray_pom_copies`, `_read_raw_poms`, `find_pom_files` honours `ws.is_ignored`), `sbmig/core/workspace.py` + `sbmig/analyze/analyzer.py` (`ignored_dirs`, `is_ignored`), `sbmig/analyze/hollow.py`, `sbmig/analyze/adf.py`, `sbmig/loop/orchestrator.py` (`_intake_stray_poms`, `stray_pom_followup`, `STRAY_POM_TAIL`), `sbmig/review/inputs.py` (`reactor_duplicate_lines`), `sbmig/report/markdown.py` + `i18n.py`; fixture `boot-stray-pom-copy`; tests `tests/analyze/test_stray_pom_copies.py`, `tests/loop/test_stray_pom_copy.py` | done |
| Bundled jars (company card 2026-11-04, v2.1z-i) | the in-house LLM's diagnosis card, class (d) sbmig bug: a `spring_legacy` WAR kept vendor crypto / SSO jars in `src/main/webapp/WEB-INF/lib/` (no pom.properties, declared by no pom); the IDE build saw them, Maven never did; the restructure moved the folder into the back module, the build failed with `package com….crypto.seed does not exist`, and the fix LLM — never told the jars exist — declined for lack of coordinates → BLOCKED `needs_owner` (8 items) although the classes sat in the project. Now: analyze scans every `*.jar` in the tree that no pom declares as `system` (content-based, no folder rule; `intake.bundled_jars`) → `profile.bundled_jars`, flag `bundled-jars`, journal `bundled_jars`, `logs/bundled-jars.json`, report section "Bundled jars (declared by no pom)"; the fix loop's zero-LLM pre-try `sbmig-bundled` (before `sbmig-jakarta`) wires a jar a `package … does not exist` / `cannot find symbol` case needs — a plain dependency when the jar's own pom.properties coordinates are in the repository set, else copied to `<module>/lib/<name>`, `system` scope at `${project.basedir}/lib/<name>` with placeholder coordinates (never guessed) and `includeSystemScope` on the boot module's spring-boot-maven-plugin (journal `bundled_jar_wired`, Handover "bundled jar `<name>` … publish it to Nexus and replace the entry"); the LLM is told the fact ("the project bundles `<jar>` which provides package `<p>`") and a coordinates / missing-library decline while a bundled jar provides the package is rejected. | `sbmig/analyze/bundled_jars.py`, `sbmig/restructure/system_jars.py` (`wire_bundled_jar`, `ensure_include_system_scope`, `bundled_jar_followup`), `sbmig/loop/fixloop.py` (`SBMIG_BUNDLED_SOURCE`, `_bundled_targets`, `_bundled_attempt`, `_package_facts`, `_decline_contradiction`), `sbmig/loop/orchestrator.py`, `sbmig/report/markdown.py` + `i18n.py`, `sbmig/core/{models,config}.py`; fixture `webapp-bundled-jar` (`company-spring5-web` renamed `vault` + `WEB-INF/lib/acme-seed-1.0.jar`; the as-is Maven build fails by design, BUILD_MATRIX `needs=fails`) | built (offline suite green; real-Maven e2e PASS: webapp-bundled-jar, company-spring5-web, ant-webapp, company-servlet-jsp, servlet-nonstandard-dirs) |
| Bundled jars wired at the restructure (user review 2026-11-04, v2.1z-j) | the user's direction: "lib 안의 jar 는 메타데이터 / pom 으로 g:a:v 를 읽어 Nexus 에 물어보고 있으면 import, 없으면 jar 추가" — the v2.1z-i pre-try moves only on a compile error, so runtime-only jars (JDBC drivers, a sitemesh filter in web.xml, taglibs) that the as-is WAR packaged would vanish with the moved web root; now the restructure applies the Ant-intake rule to every `profile.bundled_jars` entry before the first build (`sbmig/restructure/bundled_jars.py`, `layout.wire_bundled_jars`): superseded (the target chain manages the g:a — an old spring-webmvc 4.x is never pinned or wired) / container-API copy / unreferenced (`mark_used`) / already declared → untouched and named; known coordinates the repository has → compile dependency; else `<module>/lib` + system scope + the boot pom's same dependency + `includeSystemScope` + Handover ⚠; `RestructureResult.jars_wired` / `jars_skipped`, journal `bundled_jar_wired` (`stage: restructure` | `loop`) + `bundled_jars_skipped`; the pre-try stays as the safety net and skips a g:a the pom already declares | built (see INTERFACES v2.1z-j As built) |
| Orphan version properties + EOL advisories on blocked items (company re-run 2026-11-04, v2.1z-k) | the boot pom kept as-is `java-version=1.8` / `org.springSecurity-version=4.2.7.RELEASE` (name-based cleanups miss as-is spellings) → value-based orphan rule (`VERSION_LIKE_RE`, unreferenced, chain facts read, switch on); `org.springframework.mobile` blocked with "no jakarta build" only → the blocked line names the EOL advisory + replacement, advisories also match bundled jars, `knowledge/notes/spring-mobile.md` guides the LLM to write the compat-package replacement (no bytecode transform, no old jar) | built (see INTERFACES v2.1z-k) |
| As-is dependencyManagement entries shadowing the chain / imported BOM (company re-run 2026-11-04, v2.1z-l) | role-parent modules receive the as-is root's `<dependencyManagement>`; an own entry wins over the imported Boot BOM, so `spring-security-*:${org.springSecurity-version}` (4.2.7) shadowed 7.x and kept its properties alive; the version cleanup now removes own management entries the chain / imported BOM manages at an equal or newer line (same gte rule; BOM imports, newer pins, protected, system untouched); the review's Company rules name the Spring families' chain properties and call a pin on another line critical | built (see INTERFACES v2.1z-l) |
| In-house GPT agent gateway (user, 2026-11-04, v2.1z-m) | `llm.gateway: agent` sends `{model: <LLM_ASSET_ID>, messages, stream: false, async: false, params: {role: <LLM_MODEL / LLM_ESCALATION_MODEL = build_error_fixer / escalation_fixer>}, session_id, trace_id, max_tokens, temperature}` (no thread_id yet); `TokenManager` exchanges the app token (`LLM_TOKEN_URL`, `LLM_APP_TOKEN` else `LLM_API_KEY`) for an access token, refreshed 5 min before expiry or after a 401; `sbmig llm-probe` shows the exchange; legacy gateway unchanged | built (see INTERFACES v2.1z-m) |
| Session id from the gateway, EOL replacements as direct edits, obsolete Spring artifacts (user, 2026-11-04, v2.1z-n) | no `session_id` sent — the gateway's one is read into `LLMResponse.session_id`; a Spring-Mobile-like replacement is project code → prompt rule 12 / the note ask for `<no_script>` + `<edits>` (recorded as edits in the report / case history, no script), capped by `script.max_edit_tries_per_case` (4) instead of 20 asks; `knowledge/notes/spring-removed-artifacts.md` (spring-dao & co. do not exist on the target line: remove, migrate) and `VersionResolver` never pins an unmanaged `org.springframework*` artifact from the repository | built (see INTERFACES v2.1z-n) |
| Gateway sessions: thread_id deprecated, continuation = the returned session_id sent back (user, 2026-11-04, v2.1z-o) | plumbing only: `CompanyClient.payload` sends `session_id` when the caller passes `metadata["session_id"]` (the case thread will), the answer's id is kept on `LLMResponse` / the `llm-NNNN.json` record / the fixer transcript / `ScriptProposal.session_id` / `FixAttempt.llm_session_id`; the thread mode itself waits for three answers (delta vs whole history in a continued session, session TTL, billing of earlier turns) | built (plumbing; design open — INTERFACES v2.1z-o) |
| Case thread (agreed 2026-11-02/03, built 2026-11-04, v2.1z-p) | `llm.case_thread` (default off; agent gateway): the first ask of a case opens a gateway session with the full prompt, every augment of the case continues it with the DELTA only (the base script's result, the errors now, their sources, poms, version facts — no system prompt / header / rules / base code), context and feedback rounds send one message in the session, a fresh new / another case / a stored base = a new session; a rejected continuation (4xx) falls back to the full prompt (`llm.thread_fallback_full`); every turn logs `sent chars vs prompt_tokens` so the company can see whether the history is re-billed | built (see INTERFACES v2.1z-p); to be switched on after the company's first measured run |
| Containers / Kubernetes (teammate's draft reviewed, 2026-11-04, v2.1z-r) | `deploy/`: ONE image (Python 3.11 + JDK 21 + Maven + git, deps read from pyproject.toml, code at /app with PYTHONPATH, UID 1000, `sbmig serve` = uvicorn one worker), PVCs for /data and ~/.m2, ConfigMap (company.yml with container paths: kb.dir /data/kb-v2, settings.xml), Secret (.env equivalent incl. SBMIG_WEB_TOKEN), Deployment (1 replica, Recreate, tcp probes, 6–8 GiB for -Xmx4g), Service, a Job example; the draft's WEB_API_TOKEN / image-local KB / duplicated dependency list / python-multipart corrected | built (image built and smoke-tested locally) |
| Per-run build hooks (user, 2026-11-04, v2.1z-s) | `build.setup_command` / `build.teardown_command` run once around a run with SBMIG_RUN_ID / SBMIG_RUN_DIR / SBMIG_PROJECT_DIR (a failing setup aborts, a failing teardown warns; journal `build_hook`) — generic hooks; the exec-into-a-builder layout they were made for was removed in v2.1z-u | built |
| Infra team's one-Job-per-request model (guide 2026-11-05, v2.1z-t) | sbmig as the Job pod's program: `deploy/docker/run-job.sh` (source in by git / zip / mount, KB in from its git repo, `sbmig run`, results out: project branch `sbmig/<run id>` pushed to the source repo, report / logs to a result repo or RESULT_PUSH_CMD, KB pushed back; exit 0 when a report exists), `deploy/k8s/80-build-job.template.yaml` (their CHANGE_ME points, emptyDir workspace, 4 h deadline), `deploy/k8s/submit-job.sh` (agent side: can-i, dry-run=server, create, logs -f, SBMIG-RESULT); README §6 names the three gaps — KB persistence (git repo or PVC), ~/.m2 per Job, the Agent role (CLI model now, web dispatch later) | built; entrypoint dry-run locally on a fixture |
| deploy/ trimmed + Korean comments (user, 2026-11-04, v2.1z-u) | the exec-into-a-builder layout files removed (superseded by the Job model); two shapes remain — web-UI agent (10–50) and one Job per request (20, 30, 80 + run-job.sh + submit-job.sh); all comments in deploy/ Korean; README rewritten around the two shapes with the three decisions for the 11-05 meeting | built |
| Korean config comments + `agent-doc/` pack (user, 2026-11-04, v2.1z-v) | comments of config/default.yml, artifacts.example.yml, artifacts.local-e2e.yml, llm-anthropic.yml, llm-plugin.example.yml, settings.nexus.example.xml rewritten as Korean explanations (what the value decides / who changes it when / what happens; no version tags; keys and values byte-identical by yaml equality); `agent-doc/` = 12 Korean files for the in-house LLMs (README index + rules, 00 overview, 01 repo map, 02 flow, 03 run/debug/logs, 04 cases/scripts/KB, 05 LLM gateway/prompts, 06 company rules, 07 config/knowledge, 08 conventions/tests, 09 deploy/ops, 10 history/open items) guarded by tests/docs/test_agent_doc.py; the operating model corrected (project owners upload a zip in the web UI — never the CLI) and the target Kubernetes design written up for the 11-05 infra meeting: `sbmig serve` gets an execution mode (in-process | k8s Job), the web pod is the guide's Agent, one build Job per upload runs run-job.sh, zip by one-time download URL, results POSTed back, `deploy/k8s/45-web-rbac.yaml` = the web pod's minimum permissions (deploy/README.md §3: diagram, two-pod table, exists / to-build table, six decisions, 발언용 요약) — the dispatch mode itself was built on 2026-11-05 (`sbmig serve --mode k8s-job`) | built |
| dead key `build.pom_repositories` named (user's artifacts.yml still had `keep`, 2026-11-04) | the v1 X9 key (`keep | remove`, strip handler) was silently ignored since v2 step 4 — `_warn_removed_keys` now logs `config: build.pom_repositories=… is ignored since v2` pointing at the settings.xml catch-all mirror; RUNBOOK §0.1 paragraph replaced; test | built |
| Web UI redesigned for the project owners (user, 2026-11-05, v2.1z-w) | "프론트부분 클로드 디자인 사용해서 … 파란색이 회사 메인 컬러라 블루 계열로 깔끔하게 html css js로만 … 아티팩트야 어차피 하나 고정일거니까 표시 안해줘도 … 파일 업로드 할 수 있는 창이랑 드래그앤드롭 … 리포트 파일이랑 압축된 변환 파일을 받을 수 있는 버튼 … 로그 … 너무 쌓이니까 사이트가 엄청 버벅이더라": `sbmig/web/static/index.html` rewritten (one file, HTML + CSS + JS, company blue, no web font / CDN): the drop zone is the page, `마이그레이션 시작`, the run card with a stage rail (intake → analyze → plan → one dot per planned phase → finalize; a phase that ends `ok=false` shows ✗) and 진행 기록 / 로그 tabs, the result card with `보고서 받기` (report.md) / `변환된 프로젝트 받기` (result.zip) buttons + the other files as links + 보고서 미리보기, 지난 실행 collapsed; the config files are the server's choice (`sbmig serve --config` / `SBMIG_WEB_CONFIGS` / `artifacts.yml` / `artifacts.local-e2e.yml`; `GET /api/configs.default`, `POST /api/runs` without `configs`), the list + LLM / stop_after only under 고급 설정. The log lag: lines go into a ring buffer (20,000), ≤ 1,500 rendered per animation frame and only while the tab is visible, DEBUG hidden by default, the full file by link. Verified in a browser (Playwright): landing, file chosen, a real pure-java run under JDK 21 with the fake LLM (rail, tabs, result buttons, 10 blocked shown), the JDK-25 preflight abort | `sbmig/web/static/index.html`, `sbmig/web/{app,runs}.py`, `sbmig/cli.py` (`serve --config`), `deploy/k8s/40-deployment.yaml` (`SBMIG_WEB_CONFIGS=company.yml`), `deploy/README.md` §2, `agent-doc/03-run-debug-and-logs.md`, `agent-doc/09-deploy-and-ops.md`, `tests/web/test_app.py` (+2) | done |
| Case thread: a gateway session keeps its model (user, 2026-11-05, v2.1z-x) | the company's gateway cannot change the model inside an open session; the ladder's escalation (third ask / stall) switched `params.role` while `llm.case_thread` continued the base session → the model a session runs on travels with its id (`ScriptProposal.session_model` → `FixAttempt.llm_session_model` → `ScriptBase.session_model`) and `propose_script` continues a session only on the same model, else the full prompt opens a new one (INFO line `case thread for …: the model changes`); no escalation model configured = same model = same session | done (INTERFACES v2.1z-x; `tests/fix/test_fix_thread.py` +4) |
| Inherited POM problems name the parent pom (company run, 2026-11-05, v2.1z-y) | `spring-dao … is missing` came back after 24 script versions: the version-less dependency sat in the root pom, Maven reports the inherited problem per child module with `@ <parent gav>, <parent pom>, line N`, and the parser filed the error under the child pom → the script edited the wrong pom; now `_pom_location` takes the file (when the tail has a path) and the line / column from the tail for every POM problem; signatures unchanged | done (INTERFACES v2.1z-y; parser tests +3). Open: per-artifact L2 for missing-version cases (KB impact) |
| A pom fix that uncovers the next stage is not a regression; global settings behind `build.mvn` (company run, 2026-11-05, v2.1z-z) | the real cause of the 24 spring-dao versions: the script removed the stage-0 pom problem, the build advanced to dependency resolution and showed the SSO jar's `Could not find artifact` for the first time, and the structural-regression rule reverted the fix every round → `_uncovered_stage` (targets all gone + new structural errors all of a later stage = uncovered, scored normally, one note); the repository lookup now reads `<build.mvn installation>/conf/settings.xml` (the company PC keeps Maven off PATH; `repo-probe` had shown no Nexus) | done (INTERFACES v2.1z-z; loop tests +2, repo tests +1) |
| Verification | offline suite + real-Maven e2e | `scripts/e2e.sh`, `out/e2e/summary.md` | offline **2,735 passed**, 47 deselected (after the review follow-ups; 2,729 at `7674d61`, 2,786 before v2.1z — the v2.1i tests are gone); e2e **37/37 PASS** twice (at `7674d61` and again after the review follow-ups; 34 build, 3 intended BLOCKED: boot2-javax-lib, missing-artifact, legacy-ibatis) — the only expectation that moved: `enforcer-java8-werror`'s replay / `expected.json` re-keyed from case `630b997bc5` to `8b30df5c0c` (`pricing-core` → `<module>`), exactly the id change the masking contract predicts |

Open items (the PM's KB-matching review, 2026-11-02 — proposed, not built):
(a) **Rule changes orphan stored cases.** The case id is a hash of the L2 string, so every change of `sbmig/build/signatures.py` /
`sbmig/triage/cluster.py` (6 commits since `company-v1`, last 2026-10-27; v2.1z is the 7th) orphans the stored cases of the old rule;
`sbmig kb reindex` only recounts the stored `l2` and never re-derives it. Proposed: `sbmig kb reindex --resign` — re-derive the L2 from
each record's stored `raw`, move the case folder to the new id, keep an alias of the old id.
(b) **The opposite risk — one key, different problems.** `compile|cannot_find_symbol|<simple name>` merges different problems that share a
common method name (`save`, `keySet`, `findAll` seen in the e2e KB). Changing that key needs (a) first, or every such case is orphaned.
(c) **Verified fine.** The id formula is identical in `sbmig.errors.common` / `sbmig.triage.cluster` / the store; coarse keys are refined
once per build (`refine_build`) before the error list, the clusters, the scoring and the blocked registry; e2e evidence: 59 case
folders, 455 KB entries, 450 run reports, zero id inconsistencies. javac messages stay English on Korean Windows (OpenJDK localises
javac only for ja / zh); OS-text differences (Windows `\` paths masked from one backslash, posix from two) only matter when a KB from
macOS is merged into the company's.

## Status — 2026-11-06, v2.1z-aa (replacement notes: no library block, no accepted decline — company run spring-mobile)

User (2026-11-06): "spring device 쪽 파일 만들고 유틸 클래스까지만 만들고 호출부에 적용을 안 한 뒤, 다시 에러가 나니까 이미 blocked 라고 skip" —
the log: two LLM answers, the second a decline (`declined 1/1`), then `blocked (external_javax_api) … the LLM declined` and
`blocked (needs_owner): same cause as …` for the neighbours; the compat classes had been written under
`org.springframework.mobile.device` itself. Contract + "As built": `docs/INTERFACES.md` "v2.1z-aa".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Replacement notes | `NoteEntry.replacement` (`replacement: true` in front matter / `index.yml`), `replacement_notes()`, `note_text()` = errors + the files' import lines | `sbmig/fix/context.py`, `sbmig/triage/blocked.py`, `knowledge/notes/spring-mobile.md` + `index.yml` | done |
| No library block for a noted library | X1 / X2 candidate + matching note → `loop.blocked_policy` not applied (rationale names the note); `Triager.attach_run(notes=…)`, `BuildFixLoop._knowledge_notes()` | `sbmig/triage/decide.py`, `sbmig/triage/triager.py`, `sbmig/loop/fixloop.py` | done |
| Decline rejected, edits applied | `_note_contradiction` first in `_decline_contradiction`; a rejected decline that carries edits / a script is applied as an ordinary answer (`decline_rejected.applied`) | `sbmig/loop/fixloop.py` | done |
| The note's three new rules | never `<blocked>`; compat classes under the project's package, never `org.springframework.mobile.*`; one answer = files + imports + pom | `knowledge/notes/spring-mobile.md`, `config/default.yml` comments | done |
| Tests | `tests/triage/test_blocked.py`, `tests/loop/test_v21z_jakarta.py` (2), `tests/fix/test_fix_notes.py`; loop + triage + fix + docs + kb + report 1,266 passed | — | done |

## Status — 2026-11-09, deploy: the company cluster's values in the manifests

User (2026-11-09): the namespace is `agent`, the StorageClass is `nfs-csi` (NFS CSI driver → ReadWriteMany), and the infra team will push the
image to their own registry after all (2026-11-06 we had heard there was none). Manifests only — no contract change.

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Namespace | `namespace: agent` on every resource (PVCs, ConfigMaps, Secrets, Deployment, Service, RBAC, both Job templates) — `kubectl apply -f` without `-n`, the later commands with `-n agent` | `deploy/k8s/*.yaml`, `deploy/README.md` §2 | done |
| Storage | both PVCs `ReadWriteMany` + `storageClassName: nfs-csi`; NFS notes (uid 1000 squash, `actimeo=1` for the UI tail, `.m2` slow on NFS) | `deploy/k8s/10-storage.yaml`, README §3 | done |
| Registry again | `image: CHANGE_ME_REGISTRY/sbmig:CHANGE_ME_TAG` + `imagePullPolicy: IfNotPresent` in the Deployment and both Job templates; `imagePullSecrets` comment on all three; §1.1 (node import) kept as the alternative; `save-image.sh` prints both paths | `deploy/k8s/40-*`, `80-*`, `81-*`, `deploy/docker/save-image.sh`, README §1 | done |
| Still to fill | the pushed image's full name (both files, same value) and whether pull needs a Secret | — | waiting for the infra team |
| Docs | `agent-doc/09-deploy-and-ops.md` §4 paragraph | — | done |
| kubeconfig access (v2.1z-ab) | the infra team's temporary manifest mounts a kubeconfig Secret at `/etc/kube/config` instead of a ServiceAccount → `K8sClient.from_kubeconfig` / `connect()` (`KUBECONFIG` first, else in-cluster); `k8s-job` degrades to `local-job` only without both | `sbmig/web/k8s.py`, `sbmig/web/runs.py`, tests | done |
| Job template as a ConfigMap | the image's template keeps its placeholders → `sbmig-job-template` ConfigMap at `/templates`, `SBMIG_JOB_TEMPLATE`; a missing file is a readable run failure; `.dockerignore` keeps `deploy/k8s/` (only the filled Secret out) | `deploy/k8s/40-deployment.yaml`, `.dockerignore`, README §3 | done |
| hostAliases placeholders | the manifest pinned the gateway host the cluster DNS cannot resolve — commented `CHANGE_ME_IP` / `CHANGE_ME_GATEWAY_HOST` in the Deployment and the Job template | `deploy/k8s/40-*`, `81-*` | done |
| One config file | the company edits only `artifacts.yml` (user) → ConfigMap key / mount / `SBMIG_WEB_CONFIGS` / Job `SBMIG_CONFIG` all say `artifacts.yml` (was `company.yml`) | `deploy/k8s/20-*`, `40-*`, `80-*`, README, ENV.md, agent-doc 03/06/07/09 | done |
| Windows / closed-network build | `.gitattributes` pins `*.sh` / Dockerfile / yaml to LF; the Dockerfile strips CR and chmods `deploy/docker/*.sh` after COPY; `APT_MIRROR` build arg beside `PYTHON_IMAGE` / `PIP_INDEX_URL`; README §1 PowerShell lines + build-arg table. Verified: amd64 image rebuilt from the changed Dockerfile (JDK 21.0.13, Maven 3.9.9, script LF + 755, template present), tar 651 MB | `.gitattributes`, `deploy/docker/Dockerfile`, `deploy/README.md` | done |
| Company Worker contract (photos of `worker_contract.py`, 2026-11-09) | the colleague's Worker rejects shared config with raw LLM recording, unresolved `${VAR}`, secret interpolation or credential values, and requires `loop.engine: graph`, basename config names, kb.dir outside the request dir, Maven settings that reference credentials → `llm.record_dir` / `LLM_RECORD_DIR` commented out in the ConfigMap, ENV.md / README qualified, `automountServiceAccountToken: false` on both Job templates. Their k8s.py has no KUBECONFIG handling → v2.1z-ab stands (expect a merge conflict in k8s.py / runs.py) | `deploy/k8s/20-*`, `80-*`, `81-*`, `deploy/ENV.md`, `deploy/README.md` | done |
| Worker template contract (photos of `worker_template` / `worker_secret_bindings`) | `81-web-job.template.yaml`: no `envFrom` — explicit `valueFrom` per variable (ConfigMap keys optional, secrets via `secretKeyRef`), no `LLM_RECORD_DIR` / `SBMIG_WEB_TOKEN`, header lists the rules; Secret example `immutable: true` | `deploy/k8s/81-*`, `30-secret.example.yaml`, README §3 | done |

## Status — 2026-11-09 (evening), v2.1z-ac: the deployment team's spec built in flow-v2

User: the company's deployment engineer had an AI write a Main/Worker hardening (persistent queue, ownership, hash pinning, result
verification, secret scrubbing) and left on holiday; it is unverified, the closed network cannot export it, and deployment matters more
than whose code it is → "저분이 했던 프롬프트대로만 우리가 작업해서 push, 배포 잡고". Contract + "As built": `docs/INTERFACES.md` "v2.1z-ac".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| File contract | `RequestRecord` / `ControlState` / `JobStatus`, atomic writes, digests, secret names, `verify_request` | `sbmig/web/contract.py`, `tests/web/test_contract.py` | done |
| Python Worker | `python -m sbmig.web.worker`: pinned-input verification (refuses with exit 3), `sbmig run`, redaction, attempt-aware `job-status.json`, raw LLM records off by default | `sbmig/web/worker.py`, `sbmig/web/redact.py`, `tests/web/test_worker.py`, `test_redact.py` | done |
| Main hardening | intent-before-create + adoption on 409 / transport errors, control states on disk, restart recovery (`recover()`), owner labels + refuse foreign deletes + wait for Missing, verified results only, startup validation of template + RBAC (`validate_worker_template`, `can_i`), NFS grace for a late status file | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/localjob.py`, `tests/web/test_jobmode_hardening.py` (+ adjusted job-mode tests) | done |
| Manifests / docs | template command → Worker, no revision annotation, immutable Secret, ENV rows, README §3 "Main 이 지키는 것", run-state contract doc, agent-doc 09, CLAUDE.md | `deploy/`, `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md`, `agent-doc/09-*` | done |
| Not built (the colleague's extras we chose not to adopt) | refusing configs with `llm.plugin` / `fix.plugin` / `build.setup_command` (those are our adapter seams), the Secret revision-file mechanism (Secrets are immutable + versioned by name instead), a separate remediation API | — | decided |
| Verification | full offline suite 3,121 passed; `tests/web` + `tests/deploy` + `tests/docs` with the new tests; amd64 image `sbmig:2026-11-09-2` (tar 651 MB) smoke-tested (`sbmig.web.worker` import + readable no-env error); **kind end-to-end** (real apiserver, RBAC, Job, PVC, the image's Worker) — its first run found the import-time app bug above, fixed and re-run | `out/images/`, scratchpad `kind-e2e/` | done |

## Status — 2026-11-10, deploy: the company config is baked into the image

User: "Configmap에 artifacts.yml을 넣어야해? 이거 어차피 도커 빌드 할 때 파일로 복사 되지 않아?" → "일단 굽자!". The Dockerfile already copies
`config/` into `/app/config`, so the ConfigMap copy was a second, hand-kept twin of the file the migration team tests on the PC.
Contract: `docs/INTERFACES.md` "deploy — the company config is baked into the image".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Baked config | ConfigMap `sbmig-config` = `settings.xml` only; the `artifacts.yml` key and its mounts are commented alternatives; `SBMIG_CONFIG=/app/config/artifacts.yml` in 80 | `deploy/k8s/20-*`, `40-*`, `80-*`, `81-*` | done |
| Container paths | literal env overrides per manifest (`SBMIG_BUILD__SETTINGS_FILE` / `JAVA_HOME` / `MVN` / `LOCAL_REPO`, `SBMIG_SCRIPT__PYTHON` in 40 / 80 / 81; `SBMIG_KB__DIR`, `SBMIG_LLM__RECORD_DIR=${LLM_RECORD_DIR:-}` in 40) — a PC-tested yml keeps its Windows paths, env beats yml | `deploy/k8s/40-*`, `80-*`, `81-*` | done |
| `sbmig-env` reaches the Job pod | 81 has no `envFrom` → one `configMapKeyRef {optional: true}` per documented key (LLM values, proxies, the tuning keys); the new test found `HTTP_PROXY` / `HTTPS_PROXY` forwarded but undocumented → documented in the ConfigMap | `deploy/k8s/81-*`, `20-*` | done |
| Build guard | `--build-arg REQUIRE_CONFIG=artifacts.yml`: the build stops when the git-ignored file is not in the context (ASCII message, ARG declared after the app COPY) | `deploy/docker/Dockerfile`, `save-image.sh` | done |
| Secret names | `contract.is_secret_name`: `SBMIG_<A>__<B>` overrides are not secrets (template validator + redaction) — `…__MAX_OUTPUT_TOKENS` was refused as a literal and its value redacted | `sbmig/web/contract.py`, `k8s.py` | done |
| Worker records | `worker.child_environ`: without `SBMIG_WORKER_LLM_RECORDS=1` the record-dir variables do not reach the child `sbmig run` | `sbmig/web/worker.py` | done |
| Docs | README §1 (build + the baked-config block) / §2 (path table, who reads `sbmig-env`) / §3, ENV.md (§6 new, stale `CONFIG_INVALID` paragraph removed), agent-doc 07 / 09, run-state contract §3 / §10, CLAUDE.md | `deploy/`, `agent-doc/`, `docs/` | done |
| Verification | offline suite **3,137 passed** (53 deselected); `tests/deploy` + `tests/web` + `tests/docs` 390 passed; docker (amd64, a scratch context shaped like the company repo — `src/migration-agent/` + `deploy/` + root `.dockerignore`): without `config/artifacts.yml` the build stops with the guard's message; with a PC-style `artifacts.yml` (Windows paths in all seven keys) the image builds, and inside it the config resolves to the container values under the 40 env and under the 81 env + the Worker's `worker-paths.yml` (`llm.record_dir` empty unless the switch is on) | — | done |
| pip behind TLS inspection (first company build, same day) | the company PC's `docker build` stopped at the `pip install` layer with `SSLError` while pip on the PC works (the inspection CA is in the Windows store, not in the build container); `PIP_TRUSTED_HOST` now takes several hosts separated by blanks, one `--trusted-host` each (`--build-arg "PIP_TRUSTED_HOST=pypi.org files.pythonhosted.org"`); README §1 names the symptom. Checked here: pip 24.0 in the base image against a self-signed host (fails without, passes with the flag), a full local build with and without the argument, `tests/deploy` + `tests/docs` 273 passed — not on the company network | `deploy/docker/Dockerfile`, `deploy/README.md` §1 | done |

## Status — 2026-11-10, v2.1z-ad: review of the Job dispatch code (four findings)

A review of the Main / Worker Job dispatch (`sbmig/web/`) listed four findings, all judged real: the next run could start before the
previous Job had ended, a Job without labels counted as ours, a difference between the web pod's and the Job pod's environment went
unnoticed, and secrets were visible in the live outputs before the post-run redaction. Testing found a local-job regression on top.
Contract + "As built": `docs/INTERFACES.md` "v2.1z-ad".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| 1 Job end confirmation | `_watch_job` returns only once the API confirmed the Job ended / gone (`_hold_until_ended`; exceptions: server shutdown → state kept for the next Main, a Job that is not ours); a pod lingering after its `job-status.json` is deleted after `SBMIG_JOB_EXIT_GRACE_S` (new, 120 s); the wait after a stop delete has no deadline; an API outage never fails the run nor frees the slot (journal `job_api_unavailable` / `job_api_back`, `SBMIG_JOB_API_GRACE_S` = notice only, `status().job.note`, UI `api-unavailable` / `api-back` / `ending`); uncertain create asks until the API answers (`CREATE_CONFIRM_POLLS`); `_transition_soft` once the Job exists; a Job gone after a stop request is `stopped`; tests: `test_jobmode_hardening.py` (54 in the file, rows 1 and 2 together), `test_job_mode.py` (17) | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/static/index.html`, `tests/web/test_jobmode_hardening.py`, `test_job_mode.py` | done |
| 2 Ownership labels | `_owned`: `sbmig-owner` **and** `sbmig-run-id` must match; `JobState.labels` `None` only from label-less clients (local subprocess, fakes), `{}` is not ours; an unlabelled same-name Job is neither adopted (409, restart probe / adopt) nor deleted; tests in `test_jobmode_hardening.py` (counted in row 1) | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `tests/web/test_jobmode_hardening.py` | done |
| 3 Environment pins | the Main that creates the Job writes `env_pins` (name → sha256[:16] of the values the config reads; never a secret-looking name, `ENV_PIN_SKIP`, proxies) + `env_pin_policy` into `control/state.json` with `creating`; the Worker recomputes and compares, and checks `SBMIG_CONFIG` / `SBMIG_STOP_AFTER` / `SBMIG_LLM_PROVIDER` against `request.json`; `SBMIG_JOB_ENV_PIN` refuse (k8s-job default: exit 3, `worker_refused` naming the keys) / warn (`worker_env_differs`) / off (local-job default); KB-dir alias `SBMIG_KB__DIR` ↔ `SBMIG_KB_DIR`; operator rule: after changing `sbmig-env` restart the web pod, after changing the template re-create `sbmig-job-template` (and restart); tests: `test_env_pins.py` 41, `test_env_pin_main.py` 12, `test_worker_env_pin.py` 36, `tests/deploy/test_manifests.py` 24 (the shipped 20 / 40 / 81 against their own check); a whole-section override `SBMIG_<SECTION>` is pinned too | `sbmig/web/contract.py`, `sbmig/web/runs.py`, `sbmig/web/worker.py`, `sbmig/core/config.py` (`placeholder_names`), `tests/web/` | done |
| 4 Write-time redaction | `SBMIG_REDACT_OUTPUT=1` installs the core hook `sbmig/core/scrub.py` (log formatter, `Workspace.journal`, Main's journal lines and SSE events, uncaught tracebacks); fail closed (`WITHHELD` line, CLI exit 3 when the hook cannot be installed); `expand_values` spellings, `note_secret` for the exchanged token; the Worker forces the switch on its child and redacts the pipe line by line; non-live run files answer HTTP 409 until the final pass; `SBMIG_REDACT_OUTPUT=1` in 40 / 80; tests: `test_scrub.py` 12, `test_cli_redaction.py` 7, `test_output_redaction.py` 7, `test_redact.py` 16, `test_worker.py` 18 | `sbmig/core/scrub.py` (new), `sbmig/core/{stage,workspace,config}.py`, `sbmig/cli.py`, `sbmig/fix/company.py`, `sbmig/web/{redact,worker,runs,app}.py`, `deploy/k8s/40-*`, `80-*`, `tests/core/test_scrub.py`, `test_cli_redaction.py`, `tests/web/test_output_redaction.py`, `test_redact.py`, `test_worker.py` | done |
| local-job regression | `LocalJobClient` passed every `SBMIG_*` env of the Job template to the local Worker — since f727fb7 the container paths (`/data/kb-v2`, `/opt/java21`, `/etc/sbmig/settings.xml`) and the `configMapKeyRef` entries as `""`; now only the literal `contract.RUN_ENV` values on top of the PC's environment, `SBMIG_APP_DIR` = this Main's repo root; tests: `test_local_job_mode.py` 6 | `sbmig/web/localjob.py`, `sbmig/web/contract.py` (`RUN_ENV`), `tests/web/test_local_job_mode.py` | done |
| Docs / manifests | `20-configmap.yaml` (`SBMIG_JOB_EXIT_GRACE_S` / `SBMIG_JOB_API_GRACE_S` / `SBMIG_JOB_ENV_PIN` rows, the restart rule, "never leave an override empty", `LLM_RECIPE_MODEL`) and its 81 line; INTERFACES "v2.1z-ad" (+ pointers in the v2.1z-ac text), `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md` §1–§3 / §5–§10; operator text `deploy/README.md` (the 2026-11-10 review subsection, the restart rule, stop grace 2700) and `deploy/ENV.md` (§1 / §2 / §3 / §6 / §8); `agent-doc/09-deploy-and-ops.md` (the four Job-mode guarantees + checks) and `agent-doc/10-open-items-and-history.md` (history row, known limits); `CLAUDE.md` | `deploy/`, `docs/`, `agent-doc/` | done |
| Not changed (decided) | `_apply_env_overrides` keeps reading an empty value as YAML null (documented: leave unused keys commented); no `_URL` exemption in the secret-name rule (`LLM_TOKEN_URL` stays masked and unpinned); the uvicorn access log is not scrubbed | — | decided |
| Verification | offline suite **3,298 passed** (53 deselected, 9 min 52 s); `tests/web` + `tests/core` + `tests/deploy` + `tests/docs` 674 passed | — | done |

## Status — 2026-11-10, deploy: Job mode on in the shipped ConfigMap

The user's first web pod at the company came up running migrations itself: `SBMIG_WEB_MODE: "k8s-job"` was a commented
"switch on later" line in `20-configmap.yaml`, although the company's operating model (one Job per upload) and its values
(namespace, storage class) had been given. Rule from here on: what the company runs with is switched on in the manifests.

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Shipped default | `sbmig-env` carries `SBMIG_WEB_MODE: "k8s-job"` as an active key (comment: the web pod checks the 45 permissions and ConfigMap `sbmig-job-template` at startup and does not start without them; comment the line out for the in-process mode) | `deploy/k8s/20-configmap.yaml` | done |
| Apply order | README §2 applies `45-web-rbac.yaml` and creates ConfigMap `sbmig-job-template` before 40; the header and §3 "켜는 법" 7 say the mode ships on and how a cluster applied from the older file switches (uncomment, apply 20, `rollout restart`); the 40 comment on the optional template volume no longer says the pod starts without it; ENV.md row; agent-doc 09 §5 | `deploy/README.md`, `deploy/ENV.md`, `deploy/k8s/40-deployment.yaml`, `agent-doc/09-deploy-and-ops.md` | done |
| Test | `test_the_shipped_configmap_runs_the_company_operating_mode` (active key, never a commented one, service account + template mount / path on the Deployment); the mode stays unpinned | `tests/deploy/test_manifests.py` | done |
| Verification | `tests/deploy` + `tests/docs` + `tests/web` **533 passed** | — | done |
| Kubernetes access (same day, the user: "우리 환경이 kubeconfig 라고 했었는데 이거 주석처리 되어있는데") | The infra team's kubeconfig model is the shipped one: env `KUBECONFIG`, the `/etc/kube` mount and the Secret volume `agent-kubeconfig` are active in the Deployment, `serviceAccountName: sbmig-web` is the commented alternative (45 only for a cluster without a kubeconfig Secret); `SBMIG_K8S_NAMESPACE: "agent"` is an active key (a kubeconfig context without a namespace means `default`, where PVC `sbmig-data` does not exist); README §2 checks the Secret instead of applying 45, §3 "켜는 법" 2 / 7, ENV.md rows, the 45 header, agent-doc 09; the test asserts the three places, the namespace key and that the Job template carries no kubeconfig | `deploy/k8s/{20-configmap,40-deployment,45-web-rbac}.yaml`, `deploy/README.md`, `deploy/ENV.md`, `agent-doc/09-deploy-and-ops.md`, `tests/deploy/test_manifests.py`, `docs/INTERFACES.md` | done |
| Finished Jobs (same day, the user: "50개정도 돌아갈건데 빌드 파드가 안지워지고 50개가 살아있어도 괜찮은거야?") | `ttlSecondsAfterFinished` of the web Job template 86400 → **3600**: nobody issues a delete at the end of a run (the pod just exits and stays listed, stopped, until the TTL controller removes the Job and its pod), so with tens of runs a day a day-long retention keeps tens of finished pods in the namespace listing; results and `sbmig.log` are on the volume, one hour is enough for `kubectl logs` after a failure. README §3 gained "빌드 파드는 언제 사라지나" (one pod at a time, ended ≠ deleted, the TTL, the three cases in which the web pod deletes its own Job); the contract doc's situation table has the row. The 80 template (the infra team's Job) keeps its day | `deploy/k8s/81-web-job.template.yaml`, `deploy/README.md`, `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md`, `agent-doc/09-deploy-and-ops.md`, `docs/INTERFACES.md` | done |
| Pod step (same day, the user: "adf 프로젝트라 reject 하면 파드부분도 x 표시되는데 … 이거 버그다!") | A run sbmig stops with exit 3 (ADF reject) makes the Worker exit 3, so Kubernetes marks the Job `Failed`; `_watch_job` pushed that raw phase and the page drew the pod step as failed and replaced the abort reason with `파드 실패`. Now `node: job` gets `completed` once this run / attempt's `job-status.json` is accepted without `error` (either order), and `failed` only when the pod died without a verified result within the grace or the Worker itself refused / could not start sbmig (`_close_pod_step`, one closing event per watch; log line `Failed is the Worker's exit code N … not a pod failure`). Page: `completed` ticks the step, engine node events tick it too, the abort reason is sticky, a late `running` no longer moves the marker back. Run result, exit code, `verified`, control states unchanged. Known edge: a `local-job` Worker that crashes without a file still shows `completed` (the client writes the status) | `sbmig/web/runs.py`, `sbmig/web/static/index.html`, `tests/web/test_job_pod_step.py` (10), `tests/web/test_app.py`, `tests/web/test_local_job_mode.py`; `tests/web` + `tests/docs` + `tests/deploy` **544 passed** | done |

## Status — 2026-11-10, v2.1z-ae: up to three build pods at a time, the rest wait with a notice

The user: "병렬 구성을 좀 하려고 하는데 초반엔 무서우니까 3명까지는 빌드파드가 돌아가는 구조가 되었으면 하는데 나머지는 대기하라고 알림
띠우고 / 이거 3명이 동시에 시작하면 kb-v2 리드는 괜찮은데 코드 쓰기나 케이스 쓰기 파일 락 생기지 않을까?" The web pod ran one Job at a
time (v2.1z-ad). Now a configured number run side by side, the others keep their place in line and the page says so; the
shared file KB was audited for several writers and its three weak spots closed. Contract: `docs/INTERFACES.md` "v2.1z-ae".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Contract + shipped value | `SBMIG_JOB_MAX_PARALLEL` (web pod, read at startup; code default 1; integer ≥ 1 else 1 + WARNING; `in-process` always 1), `ControlState.slot`, `SBMIG_JOB_SLOT` in `contract.RUN_ENV`; ConfigMap `sbmig-env` ships `SBMIG_JOB_MAX_PARALLEL: "3"` as an active key (the user's number); not in the 81 template, not an env pin; test `test_the_shipped_configmap_runs_three_build_pods_at_a_time` (`tests/deploy/test_manifests.py` 26) | `sbmig/web/contract.py`, `deploy/k8s/20-configmap.yaml`, `deploy/k8s/40-deployment.yaml` (comment), `deploy/docker/Dockerfile` (comment), `docs/INTERFACES.md` | done |
| Slots in the web pod | `_FifoTurns(slots)`: upload order kept, `turn()` hands out the lowest free slot number (from 1), `occupy()` re-takes the recorded slot after a web-pod restart (holding runs first, then queued, each in `accepted_at` order; over the limit allowed); the slot is written with the `creating` transition, travels as Job env `SBMIG_JOB_SLOT` and in journal `job_created`; a slot is handed on only after its Job is confirmed ended (v2.1z-ad (1) per slot); `status()` gains `parallel {slots, running, waiting}`, `queue {position, ahead}`, `slot`; `GET /api/runs` → `{runs, parallel}`, `GET /api/configs` adds `max_parallel`; SSE re-sends `status` when `(state, queue position)` changes; the Kubernetes client is built once under the manager's lock and uses one `requests.Session` per thread; tests: `test_parallel_jobs.py` 25 | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/app.py`, `sbmig/web/__init__.py`, `sbmig/cli.py`, `tests/web/test_parallel_jobs.py`, `tests/web/test_app.py` | done |
| One Maven local repository per slot | the Worker gives slot k ≥ 2 the sibling `<base>-slot<k>` (base = its own `SBMIG_BUILD__LOCAL_REPO`, else `local_repo_path(load_config(…))`: `build.local_repo` → settings.xml → `~/.m2/repository`) by setting `SBMIG_BUILD__LOCAL_REPO` in the child's environment only, after the env-pin check; slot 1 / no slot: byte-identical to before; never fails a run (falls back to the shared repository and says so); journal `worker_slot`; tests: `test_worker_slot.py` 56 | `sbmig/web/worker.py`, `tests/web/test_worker_slot.py` | done |
| The page | upload toast for a waiting run (how many are running, how many are ahead), title `대기 중 — 다음 차례입니다` / `대기 중 — 앞에 N건이 기다립니다`, rail note `동시에 N건까지 이관합니다. …`, a notice on the upload card while every slot is taken (from `/api/runs`, every 15 s), run list `대기 N번`; idempotent `setStatus` (the repeated SSE `status`); checked in a browser at 1280 / 390 px (58 scripted checks, screenshot); tests: `test_page_queue.py` 4 | `sbmig/web/static/index.html`, `tests/web/test_page_queue.py` | done |
| KB with several writers | audit of every write into the KB directory (all under `<kb>/.lock`, re-read inside, temp + replace; `errors.jsonl` appends in place under the lock). Closed: (a) a new case met by two pods — `_confirm_new`: exclusive `mkdir` of the case folder, then an `O_EXCL` probe of `case.json`, read again instead of overwriting; (b) lock age on the KB file system's clock (probe file beside the lock, offset cached 60 s, waiter only), the deadline edge (a lock turning stale within 2 s of the deadline is waited for), `_break` re-reads the token; (c) `_Recorder` creates `llm-NNNN.json` exclusively; (d) `fix_v<N>.py` claimed by hard link (never replaced; atomic replace without hard links). Tests: `test_kb_parallel_writers.py` 15 (4 real processes × 20 rounds on one KB: no lost credit, contiguous versions, no torn file, no leftover, no WARNING; the simulated stale "missing"; skewed clocks), `test_recorder_exclusive.py` 4; control: 14 of 18 comparable tests fail on the HEAD versions (the old recorder lost 46 of 100 records with 4 threads) | `sbmig/kb/lock.py`, `sbmig/errors/cases.py`, `sbmig/fix/client.py`, `tests/kb/test_kb_parallel_writers.py`, `tests/fix/test_recorder_exclusive.py` | done |
| Docs | `deploy/README.md` §3 "동시에 여러 건" (order, slot numbers, repositories, KB, restart, stop, what to ask the infra team and the gateway team, how to go back to 1) + the reworded one-at-a-time passages; `deploy/ENV.md` rows; `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md` §8; `docs/v2/API-MIGRATIONS-KO.md`; `docs/migration-agent/README.md`; `agent-doc/09-deploy-and-ops.md`, `agent-doc/04-cases-scripts-kb.md`; `CLAUDE.md` | `deploy/`, `docs/`, `agent-doc/` | done |
| Not changed (decided) | two runs meeting the same new case in the same minute both ask the LLM (v1 / v2 side by side); a stop on a waiting run takes effect at its turn; the 80 model (`run-job.sh`, git KB) and `sbmig fleet` stay sequential; `in-process` stays 1; `sbmig kb import --replace` must not run while Jobs run (documented, not enforced) | — | decided |
| Not verified here | a real cluster with three pods; the company's NFS (the lock relies on close-to-open consistency — no `nocto` — and on server-side atomic `mkdir` / `O_EXCL` / `link` / `rename`); the LLM gateway's concurrency limit for one app token; node capacity / ResourceQuota for 3 × (CPU 2, 6Gi) | — | the company's |
| Verification | offline suite **3,415 passed** (53 deselected, 10 min); `tests/web` + `tests/deploy` + `tests/docs` 630 passed | — | done |

## Status — 2026-11-10, v2.1z-af: review of the parallel round (five reviews, every finding reproduced, fixed)

The user, right after v2.1z-ae: "서브에이전트 돌리면서 코드 검토 동시성 이슈 될만한거나 로직에서 문제 생길법한거 찾아봐줘!" Five read-only
reviewers on disjoint areas, each told to prove a finding with a script; the PM re-ran the reproductions, then five fix agents
on disjoint files. Nothing critical; the list below is what was real. Contract: `docs/INTERFACES.md` "v2.1z-af".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Slots (Main) | terminal `control/state.json` written while the slot is held (`_close_job_run`); recovery honours the recorded slot even when two runs recorded it (WARNING; a new run only gets a number nobody holds); any failing API poll = "API unavailable" (`K8sClient._body` → `K8sError`, `_poll_job` catches everything); a terminal Job with a pod still running / terminating has not ended (`JobState.active_pods`); a stop on a waiting Job run leaves the line at once (`_FifoTurns.withdraw`, `_LeftLine`); shutdown does not wait for a result file; tests: `test_slot_review_fixes.py` 23 (21 fail on the previous code) | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `tests/web/test_slot_review_fixes.py`, `tests/web/test_parallel_jobs.py` | done |
| Worker | SIGTERM / SIGINT stop the child's whole process tree (Maven runs in its own session: found from the process table; 5 s, then SIGKILL, 3 s), `job-status.json` exit 3 with the reason, journal `worker_finished.terminated`; slot repository fallback = pod-local `<tempdir>/sbmig-m2-slot<k>` (`pod_local: true`) before the shared one, the sibling write-tested; tests: `test_worker_review_fixes.py` 18 (real processes incl. a grandchild that ignores SIGTERM) | `sbmig/web/worker.py`, `tests/web/test_worker_review_fixes.py`, `tests/web/test_worker_slot.py` | done |
| KB | `_confirm_new` never overwrites a `case.json` the server says exists (folder listed + opened directly for up to 75 s, `CaseUnreadable`, the mutation skipped); stale-lock breaking serialized by `.lock.breaker` with identity check and exclusive put-back (two holders were possible); no spin on an unmovable stale lock; `_claim_script` never replaces (fallback only when hard links are unsupported, exclusive create); `_release` only on our token; `keep_alive` heartbeat in long sections; non-positive `kb.lock_timeout_seconds` = 300; slow clock probe discarded; an unwritable LLM record is a WARNING; a failed `import --replace` swap restores the KB; tests: `test_kb_review_fixes.py` 33, `test_recorder_exclusive.py` 7 | `sbmig/kb/lock.py`, `sbmig/errors/cases.py`, `sbmig/kb/store.py`, `sbmig/fix/client.py`, `tests/kb/`, `tests/fix/` | done |
| LLM gateway shared by the pods | `llm.busy_wait_seconds` 600 (429 / 5xx with `Retry-After` = busy: the call queues with backoff + jitter instead of failing after 7 s), `llm.auth_retries` 2 (a 401 right after a fresh exchange: pause, exchange again; WARNING once), compare-and-invalidate of the held token, jitter on the other retry sleeps; wired in `make_llm_client`; tests: `test_company_busy.py` 14 | `sbmig/fix/company.py`, `sbmig/fix/client.py`, `sbmig/core/config.py`, `config/default.yml`, `tests/fix/test_company_busy.py` | done |
| The page | re-attaches by itself after a web-pod restart / dropped stream (2 … 30 s backoff, truthful titles, three 404s = unknown run); a stop is bound to its run and reads `stopped`; `/api/runs` answers in order, calls coalesced (300 ms); proven with a jsdom harness before / after; tests: `test_page_queue.py` 7 | `sbmig/web/static/index.html`, `tests/web/test_page_queue.py` | done |
| Reviewed, clean | every Maven call / local-repository reader follows the slot repository (end-to-end repro); no other fixed-name shared file outside the run dir; per-pod caches; XSS / path traversal | — | checked |
| Known, not changed | `journal.jsonl` appended by Main and Worker from two hosts; parent-chain facts on a slot's first runs come from the remote lookup only; six HTTP/1.1 connections per browser host; the token endpoint answering 429 | — | noted |
| To ask the gateway team | the concurrency limit per app token; whether a new token exchange revokes earlier tokens | — | the company's |
| Docs | INTERFACES "v2.1z-af"; `deploy/README.md` §3 (stop on a waiting run, pod-local fallback, `.lock.breaker`, gateway keys, restart behaviour); `docs/v2/JOB-RUN-STATE-CONTRACT-KO.md` §6; `agent-doc` 04 / 05 / 10; `CLAUDE.md` | `docs/`, `deploy/`, `agent-doc/` | done |
| Verification | offline suite **3,509 passed** (53 deselected, 10 min 41 s); not run on a real cluster, NFS or Linux `/proc` (the process-tree tests ran on macOS) | — | done |

## Status — 2026-11-11, remediations: /api/remediations, IVAS

The company asked for a second feature beside the migration: take an uploaded Maven project, scan it with IVAS, raise only the
**version text** of the vulnerable libraries the project's own poms declare (transitive / parent- / BOM-managed ones: one
approved `<dependencyManagement>` override entry in the root pom, the single exception), rebuild, rescan the same bytes and hand back the ZIP
with a report — sources and structure untouched, every risky change approved on its exact diff. Built as an independent
LangGraph flow in `sbmig/remediations/`, served by the existing web app. Contract: `docs/INTERFACES.md` "remediations (취약점 조치)".

| Increment | Deliverable | Where | Status |
|---|---|---|---|
| Contract | statuses, reason codes, dataclasses (`RemediationState`, `Proposal`, `ScanResult` …), hash rules, `RunPaths`; Protocols + `Components` bundle; `RemediationConfig` / `RemediationPolicyConfig`, `remediation:` in `config/default.yml` (Korean comments) | `sbmig/remediations/models.py`, `sbmig/remediations/interfaces.py`, `sbmig/core/config.py`, `config/default.yml` | done |
| Intake | ZIP validated before extraction (traversal, absolute paths, links, special files, encryption, duplicates / normalized collisions, entry count, sizes, ratio), original kept read-only, manifest, `accepted/`, boundary check (`.mvn` config / extensions, wrapper, links, foreign repositories, unparsable poms), deterministic delivery ZIP | `sbmig/remediations/intake.py` | done |
| IVAS | requests client on the company's contract (login → `X-API-KEY`, multipart upload streamed once, status, summary, paged library / sw / file-component / matched-file routes), completeness rules, uncertain upload never re-sent, token never logged, TLS never off | `sbmig/remediations/ivas.py` | done |
| Maven | server Maven + JDK 21 + required server settings.xml, insecure / override args stripped, tests skipped, `package`, `dependency:tree` per module, jars inside the built war / jar | `sbmig/remediations/maven.py` | done |
| Inventory / plan / apply | findings linked to resolved dependencies and the exact pom line; target only from `policy.pins` or the approved repository; approval classes; three hashes; line-level text edits, text-only and resolution verification; managed-version override (one `op="insert"` `<dependency>` entry in the root pom's `<dependencyManagement>`, fixed shape, always approved — residual for variants, non-inheriting modules, own declared versions, no safe line; an existing root entry → replace route) | `sbmig/remediations/inventory.py`, `sbmig/remediations/planner.py`, `sbmig/remediations/apply.py` | done |
| Graph + report | ten nodes, waiting states (`AWAITING_APPROVAL`, `AWAITING_CONFIRMATION`, `STOPPED`), rounds up to `remediation.max_rounds`, success decision (incl. `delivery.build_ok` / `scanned`), failed candidate `include` (frozen as `result.zip`, `build_ok=false`, not rescanned, earlier verified ZIP kept as `result-r<k>-verified.zip` → `result-verified.zip`) or `discard`; Korean `report.md` (warning first when `result.zip` is a failed candidate; proposal notes under the proposals table) + `report.json` (`delivery`, `verified_delivery`) | `sbmig/remediations/graph.py`, `sbmig/remediations/report.py` | done |
| Service + API | job manager (worker thread, `state.json`, approvals / resume / stop / delete, restart → `INTERRUPTED`) and the `/api/remediations` router behind the TokenGuard | service.py, api.py in `sbmig/remediations/` | done |
| Tests | fakes for IVAS / Maven / repository, synthetic ZIPs, `tmp_path` only — no network, no `mvn` | `tests/remediations/` | 428 passed (incl. the end-to-end tests over the real modules with fake IVAS / Maven / repository) |
| Deploy | `IVAS_BASE_URL` / `IVAS_USER_ID` / `IVAS_PASSWORD` in Secret `sbmig-llm` (web pod only: not `sbmig-env`, not the 81 Job template); `deploy/README.md` §6 | `deploy/k8s/30-secret.example.yaml`, `deploy/README.md` | done |
| Docs | INTERFACES "remediations (취약점 조치)"; `agent-doc/11-remediations.md` + README index | `docs/INTERFACES.md`, `agent-doc/` | done |
| Decided | (user, after comparing with the company's older implementation) `remediation.failed_candidate: include` default — a candidate whose build failed is delivered as `result.zip` with `build_ok=false`, `FAILED candidate_build_failed`, not rescanned, Korean warning first in `report.md`, original and `accepted/` untouched; `discard` keeps the old behaviour; text-only / resolution failures are never delivered. Managed-version override for `transitive` / `parent_managed` / `bom_managed` (hint required, approved evidence, same-major preferred), ALWAYS waiting for approval regardless of `require_approval` / `always_approve`; the note warns that later parent / BOM / direct upgrades no longer move the version. `ivas_analysis_timeout_minutes` default 30. Plans saved before the change no longer match their proposal hash (`op` / `anchor` / `override` are hashed). `require_approval` default **false** (same-major own-pom changes apply on their own; `major` / `parent` / `bom` / `managed_override` always wait; production that wants every change approved sets `SBMIG_REMEDIATION__REQUIRE_APPROVAL=true`); `.mvn` refused at intake (cannot be disabled on Maven 3.9) | — | decided |
| Not built yet | plugin / vendored / unmapped findings stay manual; not run against the real IVAS or Nexus | — | open |
| To ask the company | IVAS: the full `anlStatusDivCd` list, what summary totals count, SW item fields, file-component name field, how to check an uncertain upload, token lifetime / rate limits, internal CA; which repository is "approved"; who owns `remediation.policy`; `require_approval` in production; whether two approved overrides may share one newly created `<dependencyManagement>` section in one round (today they do) and whether the inserted indentation (copied from sibling entries) is acceptable; retention of ZIPs / scans (answered: failed-candidate policy, approval classes, multipart field `file`, analysis id `lbrdrAnlId` else `analysisId`) | — | the company's |
| Decided (later the same day) | the remediation runs in the BUILD POD, chained after a migration that ended BUILD SUCCESS (migration graph node `remediation` after `finalize` → the separate remediation graph, sbmig/remediations/chain.py), files under `<run dir>/remediation/`, id = the migration run id; the migration verdict / exit code never change; skipped (journal `remediation_skipped`) when IVAS values / Maven are missing; `SBMIG-RESULT … remediation=<STATUS\|skipped>`; the pod ends at `AWAITING_APPROVAL` / `AWAITING_CONFIRMATION` (resume in a new build pod = next increment, 409 `not_available` until then); web pod = status / proposals / approvals / files, standalone upload only in `in-process` mode; `remediation.trusted_projects` gates the standalone path only | — | decided |
| Deploy: build-pod environment | `SBMIG_REMEDIATION__AFTER_MIGRATION: "true"` shipped in ConfigMap `sbmig-env` group 3 (one source for 40 / 81 / 80, so the env-pin check sees the same value on both sides; a literal in 40 only would stop every Job) + its `configMapKeyRef` line in 81; commented `…__REQUIRE_APPROVAL` / `…__FAILED_CANDIDATE` / `…__IVAS_ANALYSIS_TIMEOUT_MINUTES` with 81 lines; `IVAS_BASE_URL` / `IVAS_USER_ID` / `IVAS_PASSWORD` by `secretKeyRef` (optional) from `sbmig-llm` in 81, `envFrom` in 80; `run-job.sh` copies the remediation's text summary into the result repository (no ZIPs in git); `activeDeadlineSeconds` kept 14400 (typical +20–75 min, worst case of the caps ≈ 8 h — README §6 "시간") | `deploy/k8s/20-configmap.yaml`, `81-web-job.template.yaml`, `80-build-job.template.yaml`, `40-deployment.yaml`, `30-secret.example.yaml`, `deploy/docker/run-job.sh`, `deploy/README.md` §6 (+ operator checklist), `deploy/ENV.md` | done |
| Worker / redaction | the Worker's child receives `IVAS_*` unchanged; `IVAS_USER_ID` is a secret name (`contract.SECRET_NAMES`, redacted from 4 characters, never pinned, refused as a literal in the Worker template); no `IVAS_*` value is env-pinned; the final redaction pass walks `remediation/` text files (+ `.patch`, atomic replace) and skips `accepted/`, `rounds/<n>/candidate/`, `original/` (read-only `source.zip`, hash-bound manifest), ZIPs and symlinks; a stop signal during the chained remediation keeps the migration's `SBMIG-RESULT` line in the Worker's error text | `sbmig/web/contract.py`, `sbmig/web/redact.py`, `sbmig/web/worker.py`, `tests/web/test_worker_remediation.py`, `tests/deploy/test_manifests.py` | done |
| Open (deploy) | in the 80 model the delivery ZIP dies with the emptyDir unless `RESULT_PUSH_CMD` exports the run dir, and there is no approval page | — | open |
| Wave 4 (evening): IVAS action codes | `actionDivCd` meanings from the company's document (NE 조치 필요 / NP 조치계획 미등록 / PL 조치예정 / OT 조치일 경과 / PC 패치 완료 / IG 조치 예외; one table `ivas.ACTION_MEANINGS`); `remediation.ignore_actions` (default `[PC, IG]` in this wave — empty since the "existing implementation facts" row below; trimmed, case-insensitive): such findings stay in `scans/*.json` and in the report's own table "조치 대상에서 뺀 항목" (+ `report.json` `ignored_findings`) but are not inventoried / planned and do not count for SUCCEEDED, the next round or `counts` (`*_findings` = actionable, new `baseline_ignored` / `final_ignored`); an empty / unknown code stays actionable, journal `ivas_action_unknown` once; `ignore_actions` is in the policy hash — **plans saved before this change are stale once** (re-made at the next resume, open proposals must be decided again) | `sbmig/remediations/ivas.py`, `graph.py`, `planner.py`, `report.py`, tests | done |
| Wave 4: chain time budget | `remediation.chain_budget_minutes` (90, 0 = none): `chain.run_after_migration` / `chain.resume` wrap `stop` so it also turns true after that much wall time in this invocation → `STOPPED` at the next boundary, reason `chain_budget` (`chain.R_CHAIN_BUDGET`; a real stop request keeps `stop_requested`), journal `remediation_budget` once; a scan in flight keeps its receipt, so the resume polls the same IVAS analysis. Keeps a chained remediation inside the Job deadline | `sbmig/remediations/chain.py`, `report.py`, `tests/remediations/test_chain.py` | done |
| Wave 4: matched-files id | a file component id may contain `\|` (`11560\|934982`, "ossId" in the company's words): pinned by tests to travel as one encoded path segment (`11560%7C934982`) and unchanged into `Finding.component_id`; `fileComponent.id` stays the source, `fileComponent.ossId` / top-level `ossId` only when it is missing (scan note) | `sbmig/remediations/ivas.py`, `tests/remediations/test_ivas.py` | done |
| Wave 4: resume in a new build pod + approval page (web layer, parallel agents) | Job-mode resume of a chained remediation dispatches a NEW Job (Worker action `remediation-resume` → `chain.resume`), one slot in the upload FIFO line, state on the shared volume (`pending_receipt` → no re-upload; for `AWAITING_CONFIRMATION` the web pod writes the confirmation into `state.json` before the Job exists); `POST …/approvals` takes `"resume": true` → `resumed` / `resume_error`, `by` optional; the page: checkbox list + one button `선택한 항목 적용` (checked = approved, unchecked = rejected, then resume; rejected proposals are not proposed again in that job). Docs updated here; the standalone upload in Job modes still answers 409 `not_available` | `docs/INTERFACES.md`, `agent-doc/11-remediations.md`, `deploy/README.md` §6, `deploy/ENV.md` | docs done |
| To ask the company (updated) | answered: action-code meanings, several logins at once. Open: which field is the matched-files id (`fileComponent.id` vs `ossId`), token lifetime, how to check an uncertain upload, the `patchedVersion` shape, what `components.total` counts (assumed = library vulnerability rows, as the older implementation compares; relax with `ivas_summary_check: nonzero` if wrong); PC / IG answered (not ignored by default, row below) | — | the company's |
| Existing implementation facts (user, 2026-11-11) | the company's existing IVAS client (a) does not filter PC / IG findings and (b) reconciles the full result rows against the summary (`components.total` vs the number of `libraryvulnerabilities` rows). → `remediation.ignore_actions` default is the EMPTY list (every code actionable; `[PC, IG]` to leave out what the security team closed; still in the policy hash, so a stored plan made under the old default is stale once); new `remediation.ivas_summary_check: exact` (default) \| `nonzero`: exact = `components.total` must equal the library rows returned over all pages (counted before de-duplication; not compared when the library pages already failed) else `summary_mismatch` with both numbers; `swComponents` / `fileComponents` stay zero / non-zero (mapping not confirmed, said in the scan note); not in the policy hash; commented `SBMIG_REMEDIATION__IVAS_SUMMARY_CHECK` in `sbmig-env` + its 81 line | `sbmig/remediations/ivas.py`, `sbmig/core/config.py`, `config/default.yml`, `deploy/k8s/20,81`, tests, docs | done |

## Status — 2026-11-11, v2.1z-ag: an as-is version of the company parent that the repository does not have

Company run: `SBMIG-RESULT … exit=3 attn=infra:repository,aborted:parent-not-found,blocked:parent_not_found` at the transform
step — the project already inherited the in-house parent g:a at `1.0.0-SNAPSHOT`, which Nexus does not serve. OpenRewrite runs
on the as-is poms; `_check_parent_in_rewrite` and `BuildFixLoop._parent_abort` compared only the g:a with
`artifacts.parent_keys()`, never the version, so a missing as-is version aborted as "configured parent not resolvable".
Contract: `docs/INTERFACES.md` "v2.1z-ag".

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Parsing | `parent_coordinates_in(text)`: the four Maven shapes after `Non-resolvable parent POM` (`Could not find artifact`, `The following artifacts could not be resolved`, `Failed to read artifact descriptor for`, `Could not transfer artifact`), relativePath tail or not; `_artifacts_in` reads the descriptor shape too | `sbmig/build/log_parser.py` | done |
| Classification + alignment | `classify_parent_misses` / `classify_gav` → configured / asis-version / foreign; `align_asis_parent` (version only, alias → all three coordinates, `${prop}` defined in the project and used only by parent versions → at the property, else the literal); `PomEditor.set_parent_version` | `sbmig/restructure/asis_parent.py`, `sbmig/restructure/pom_editor.py` | done |
| Switch | `openrewrite.unresolvable_asis_parent: align \| continue \| abort` (default align, Korean comment) | `sbmig/core/config.py`, `config/default.yml` | done |
| Transform | case a (configured coordinates) aborts with the new wording; case b aligns, commits `transform: as-is parent …`, journal `asis_parent_aligned`, flag `asis-parent-aligned`, Handover line, OpenRewrite once more (retry failing on the configured version → abort); case c / `continue`: flag `rewrite-skipped-asis-parent`, journal `rewrite_skipped_asis_parent`, Handover line | `sbmig/loop/orchestrator.py` | done |
| Fix loop | `_parent_abort` version-aware; zero-LLM pre-try `sbmig-parent` (first pre-try, also for a triage block `parent_not_found`) | `sbmig/loop/fixloop.py`, `sbmig/core/models.py` (`TrySource`) | done |
| Neighbours | intake `help:effective-pom` reason `the parent <g:a:v> is not in the repository` (non-fatal); `infra:repository` unchanged (aborts / failed final builds only); triage unchanged; `repo-probe` unchanged; flag help + Korean texts | `sbmig/build/effective.py`, `sbmig/report/markdown.py`, `sbmig/report/i18n.py` | done |
| Tests + fixture | `tests/loop/test_asis_parent.py` (20); fixture `boot-asis-parent-unpublished` (stand-in parent at `2.7.18-NOT-PUBLISHED`), real-Maven e2e PASS | `tests/`, `fixtures/projects/` | done |
| Docs | ERROR-HANDLING §2 / §5 / §6 / §7, INTERFACES "v2.1z-ag", agent-doc 02 / 03, PROGRESS | `docs/`, `agent-doc/` | done |

## Status — 2026-11-11, v2.1z-ah: Maven's HTTP repository blocker is an environment abort of its own

Company run: `exit=3 attn=infra:repository,aborted:parent-not-found,blocked:parent_not_found`, "configured parent … is not
resolvable". The real cause was Maven's built-in blocked mirror `maven-default-http-blocker` (Maven ≥ 3.8.1, global settings)
refusing the company repository's `http://` URL; sbmig's message pointed at the parent. Contract: `docs/INTERFACES.md` "v2.1z-ah".

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Classifier | `http_blocker_hit` (mirror id + blocked repositories from `Blocked mirror for repositories: [...]`, credentials removed; evidence = that phrase or the blocker address `http://0.0.0.0/` in Maven's own message shapes — the id `maven-default-http-blocker` alone is not a hit, a settings file may redefine it as an ordinary mirror), `http_blocked_reason`, `http_blocked_followup`; the exact Maven 3.9.10 texts (parent wrapper, dependency descriptor) pinned in the tests | `sbmig/build/env_errors.py` | done |
| Precedence | `BuildFixLoop.http_blocked_abort` before as-is parent alignment / `_check_parent_in_rewrite` (transform), before the fix loop (final build), before `_parent_abort` / pre-tries / LLM (loop); no file-lock / `-U` retry in `MavenRunner`, no OOM / download / `-U` / runNoFork retry in `OpenRewriteRunner` | `sbmig/loop/fixloop.py`, `sbmig/loop/orchestrator.py`, `sbmig/build/runner.py`, `sbmig/rewrite/runner.py` | done |
| Outcome | flag `aborted:http-blocked` (exit 3), `infra:http-blocked` instead of `infra:repository`, journal `maven_http_blocked`, one Handover line first (Korean in i18n), flag help | `sbmig/loop/orchestrator.py`, `sbmig/report/handover.py`, `markdown.py`, `i18n.py`, `result_line.py` | done |
| Preflight | `active_http_blockers` (user + global settings merged and mirrors selected like Maven; settings and pom `http://` repositories) → WARNING + journal `maven_http_blocker_active`, never an abort | `sbmig/build/env_errors.py`, `Orchestrator._check_http_blocker` | done |
| Repository lookup | `RepositoryLookup` merges mirrors like Maven too: a global mirror whose id the user settings redefine is dropped, so repositories excluded by `,!<id>` keep their own URLs | `sbmig/build/repo_versions.py` | done |
| Remediation | the same sentence first in `BuildOutcome.summary` / `ResolvedModel.notes`; no `-U` retry, no package-assisted deptree retry | `sbmig/remediations/maven.py` | done |
| Tests | `tests/build/test_http_blocker.py`, `tests/loop/test_http_blocker_flow.py`, pins in `test_result_line.py` / `test_report_i18n.py` | `tests/` | done |
| Not done | e2e fixture: `scripts/e2e.sh` passes no per-fixture settings file (a blocked run needs one, or a global-settings blocker on the e2e host) | — | open |
| Docs | ERROR-HANDLING §6 / §7, INTERFACES "v2.1z-ah", RUNBOOK §0, agent-doc 03, deploy README, PROGRESS | `docs/`, `agent-doc/`, `deploy/` | done |

## Status — 2026-11-12, remediations: first run against the real IVAS (company PC), proposal scope

The chained remediation ran on the company PC: readiness (needs `build.settings_file`), baseline build, login, upload, analysis
polling, every result page, inventory, plan (1 proposal, 56 residuals), candidate build — and ended `FAILED resolution_mismatch`.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Proposal scope | `InventoryItem.also_modules`, `Proposal.modules` / `also_modules` (outside `proposal_hash`): a version text is answerable only for the modules it governs | `sbmig/remediations/models.py`, `inventory.py`, `planner.py` | done |
| Verification | `verify_resolution` requires from → to in the governed modules and their artifacts; an also-module left at `from_version` is an `info:` line, the round commits, the next round plans the rest | `sbmig/remediations/apply.py` | done |
| Operator text | the governed / also modules in `report.md` and `report.json`; `chained remediation <id>: <status> <reason> — <message>` in `sbmig.log` (scrubbed, one line, 600 characters) | `sbmig/remediations/report.py`, `chain.py` | done |
| Tests | inventory / planner / apply units, the flow with the real inventory + planner + verify over fake Maven and IVAS (round 1 commits, round 2 residual, INCOMPLETE not FAILED), old plan JSON without the fields | `tests/remediations/` | done |
| Open | company layout: modules inherit the company role parents, not the project root, so a root `<dependencyManagement>` override reaches no module and every transitive finding is `manual_review` (the reason on all but one of the residual rows read out from that run) — where the entry goes is the company's decision | — | open |
| Not yet seen with the real IVAS | commit + frozen delivery ZIP rescan, approval → resume in a new build pod, the uncertain-upload path | — | open |

## Status — 2026-11-12, remediations: in-house artifacts go to their owners, the report in Korean

The company's rule (user): a vulnerability that comes from the company's own artifacts — the mandatory / optional libraries of
the artifacts yml and what derives from the parent poms — is not the project's to fix; the report says so with the company's
sentence. And the report must be Korean and easy to scan (the first real report repeated one English sentence on 55 rows).

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Contract | reason `inhouse_artifact`; `InventoryItem.inhouse` / `inhouse_modules`; `Residual.modules` / `via` / `inhouse`; `remediation.inhouse_groups` (`[mandatory, optional, parents]`), `remediation.inhouse_message` | `sbmig/remediations/models.py`, `sbmig/core/config.py`, `config/default.yml` | done |
| Attribution | `inhouse_keys`, `inhouse_on_path`; parent-derived results (`parent_managed` with a clear provenance, or a transitive under a dependency no project pom declares) for modules inheriting a company parent | `sbmig/remediations/inhouse.py`, `inventory.py` | done |
| Planning | `inhouse_artifact` residual instead of a proposal / override when the library is in-house or every module gets it through one; a `policy.pins` entry for the parent still gives the parent-version proposal; in-house keys in the policy hash; `counts.residual_inhouse` | `sbmig/remediations/planner.py`, `graph.py` | done |
| Report | Korean `report.md` in a new order (result line, 한눈에 보기, 다음에 할 일, 고친 것, 승인 대기, 사내 라이브러리, 사람이 확인할 것 grouped by reason and kind of detail, 진행 기록); `report.json` + `inhouse`; one more line in the migration report's remediation section | `sbmig/remediations/report.py`, `sbmig/report/markdown.py`, `i18n.py` | done |
| Severity marks | 🔴 / 🟠 / 🟡 by `remediation.severity_red` / `severity_orange` / `severity_yellow` (9.0 / 7.0 / 4.0) next to every CVE, a 위험도 column first in every library table (rows sorted by it), per-level counts in 한눈에 보기, `report.json` `severity` | `sbmig/remediations/report.py`, `i18n.py`, `sbmig/core/config.py` | done |
| Korean presentation | label tables, `ko_text` pattern rules (unknown text unchanged), `detail_group`; `note_ko` / `detail_ko` / `origin_ko` / `reason_ko` in `GET …/proposals`; the approval card shows the Korean note | `sbmig/remediations/i18n.py`, `service.py`, `sbmig/web/static/index.html` | done |
| Tests | `test_inhouse.py`, `test_i18n.py` (one sample sentence per rule), inventory / planner / graph / report / service additions | `tests/remediations/` | done |
| Docs | INTERFACES "remediations", agent-doc 12 (new) + 11, PROGRESS | `docs/`, `agent-doc/` | done |
| Open | transitive findings under a dependency the project itself declares, in modules that do not inherit the project root, stay `manual_review` — where an override entry would go is the company's decision | — | open |
| Not measured | how the real project's 56 residuals split between in-house and manual review (next company run) | — | open |

## Status — 2026-11-12, KB case identity: rule versions, member-symbol keys, re-sign, the exact-key guard (v2.1z-ai)

The user asked whether a longer case id (50 characters instead of 10) would match better, because the KB goes into a database
later. It would not: the id hashes the whole L2. What decides accuracy is the signature rule and whether stored cases survive
a rule change, so those were built (contract: `docs/INTERFACES.md` "v2.1z-ai").

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Rule version | `SIGNATURE_VERSION` (2), `SIGNATURE_CHANGES`, `classes_changed_since` | `sbmig/build/signatures.py` | done |
| Rules 2 | a missing member (method / field / constructor) of a library-owned type carries the type in the L2 (`cannot_find_symbol`, `no_suitable_method`); class symbols and project-owned receivers keep their key, so no other id moves | `sbmig/build/signatures.py` | done |
| Stored facts | `ErrorEntry.sig` / `l1` / `symbol` / `location` / `artifact` / `goal` / `was`; `CaseInfo.sig` / `aliases` | `sbmig/core/models.py`, `sbmig/errors/entries.py` | done |
| Re-sign | records of the changed classes recomputed, cases and scripts moved (whole move, merge, split), old ids kept as aliases; streaming, crash-convergent (`resign-pending.json`, `kb.json`); automatic on the first lookup and after `record_run`, explicit by `sbmig kb reindex [--dry-run]`, inside `kb import` | `sbmig/errors/resign.py`, `sbmig/errors/cases.py`, `sbmig/kb/store.py`, `sbmig/kb/cli.py` | done |
| Exact-key guard | a stored case whose L2 is not the L2 asked for is neither reused nor written (`same_case`, `CaseKeyMismatch`); `kb validate` findings | `sbmig/errors/cases.py`, `sbmig/kb/store.py` | done |
| Independent review | a read-only review reproduced seven defects in the first build of the re-sign (an alias-only merge that moved a live case's script into another case; triggers that never converged for skipped cases and glued lines; a CLI re-sign blind to the in-house prefixes; cut facts; `retire` through an alias hitting a renumbered script; 1 GB peak memory on a 100 MB file) — all fixed, each with a regression test that failed first | `tests/errors/test_resign_review.py` | done |
| Fixtures | replay files re-keyed for the five cases that move: `spring5-security-batch` (4), `boot2-rewrite-excluded` (1) | `fixtures/projects/*/replay/` | done |
| Test collection | `tests/build` (381 tests) had never been part of a run started at the repository root: pytest skips a directory named `build` by default. `norecursedirs` now leaves it in | `pyproject.toml` | done |
| Rehearsal | copies of two stored KBs written by rules 1: five cases moved, every script file byte-identical, untouched lines byte-identical, `kb validate` clean; an older-rules KB re-signs on its first lookup and the HttpHeaders case is found under the new id | scratch | done |
| Docs | INTERFACES "v2.1z-ai", ARCHITECTURE §5, agent-doc 04 / 10, kb-map (md, html, index.html), PROGRESS | `docs/`, `agent-doc/` | done |
| Still merged by name or phrase | members of project-owned types (`…|cannot_find_symbol|save`), syntax errors (`';' expected`), `unreported exception <type>` — to decide from real company cases | — | open |
| Not measured | the re-sign on the company KB and on NFS with three pods; a mixed old / new image roll-out | — | open |

Also this day: the chained remediation ran to its report on the company PC against the real IVAS after one more Windows fix —
`freeze_delivery` re-opened the finished ZIP read-only for `os.fsync`, which Windows refuses with EBADF (`115d537`).

## Status — 2026-11-12, remediations: the report rewritten in workplace Korean

After the first complete run against the real IVAS the user read the Korean `report.md` built earlier the same day: "기계번역처럼
되어서 보는 것도 힘들고 읽는 것도 힘들다". It was a word-for-word rendering of the code vocabulary (govern → 다스리다, path → 들어온
길, version text → 버전 글자, residual → 남긴 항목) with the step log and the codes on the first screen.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Wording | every label, rule output and report sentence rewritten with a glossary (조치 완료, 승인 대기, 직접 확인 필요, 사내 라이브러리 담당자 요청, 조치 전 / 조치 후, 수정본, 결과 ZIP, 적용 범위, 의존 경로, 버전 표기); particles attached to the word before them, none after a data value unless its form never changes | `sbmig/remediations/i18n.py`, `report.py` | done |
| Layout | result block (one verdict per status) → 요약 (two small tables) → 해야 할 일 → 조치 완료 → 승인 대기 → 사내 라이브러리에서 발생한 취약점 → 직접 확인이 필요한 취약점 → 변경하지 않은 것 → 처리 기록 (collapsed; the only place with codes); empty sections left out; the module list once above a table; 의존 경로 as artifactIds; sizes in MB | `sbmig/remediations/report.py` | done |
| Rule coverage | rules for the sentences that still came out in English (Maven readiness, resolution notes, build-setting check details, incomplete-scan reasons); a sentence without a rule is printed as `원문: …` | `sbmig/remediations/i18n.py` | done |
| Page and migration report | `message_ko` / `status_ko` in the status payload, shown by the page; the migration report's remediation section in Korean lines instead of raw count keys | `sbmig/remediations/service.py`, `sbmig/web/static/index.html`, `sbmig/report/markdown.py` | done |
| Guards | tests reject the old words, infrastructure words before the collapsed record, and stand-alone particles — over three sample states, the status × reason matrix and every rule sample | `tests/remediations/test_report.py`, `test_i18n.py`, `tests/report/test_remediation_section_ko.py` | done |
| Docs | INTERFACES "remediations" (presentation), agent-doc 12 (glossary, sections), PROGRESS | `docs/`, `agent-doc/` | done |
| Not aligned yet | the operator chapters still use the earlier coinages (기준 빌드, 후보 빌드, 전달 ZIP, 버전 글자): agent-doc 11, deploy/README §6 — after the user's feedback on the wording | — | open |
| Waiting | the user's read of the new samples; photos of the real report's hardest parts | — | open |

Also this day: pod sizes as the company set them (CPU 2 / 8Gi for the web pod and each of three build pods, `a56116f`).

## Status — 2026-11-12, run flows (full | remediation), Ant refused with a notice, the page (v2.1z-aj)

The user, after the first complete run: already-migrated projects will be checked against IVAS again and again, and
then only the remediation should run — a choice on the page, 전체 진행 / 취약점 해결. Ant → Maven conversion is no longer
wanted ("Maven 변환 후 신청 부탁드립니다." only; keep the code for later). The page gets a temporary name, loses its
description and footer, and runs must be recognisable by the uploaded file's name (everyone sees every run until SSO).

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Contract | `RUN_FLOWS`, `RunReport.flow`; `intake.ant_to_maven: false`, `intake.ant_message`; INTERFACES "v2.1z-aj" | `sbmig/core/models.py`, `config.py`, `config/default.yml`, `docs/INTERFACES.md` | done |
| Remediation flow | `sbmig run --flow remediation`, `Orchestrator.run(flow=)`, both engines: intake without any edit → `chain.run_only` on the uploaded ZIP's own bytes → a short report; no migration step, no LLM, no KB; exit 0 / 2 / 3, `flow=remediation` in the result line, `build=` says what was built; flag `remediation:not-ready` | `sbmig/cli.py`, `sbmig/api.py`, `sbmig/loop/orchestrator.py`, `sbmig/graph/engine.py`, `sbmig/remediations/chain.py`, `sbmig/report/` | done |
| Ant | refused at intake by default with the company's sentence as the reason (both flows); the synthesis kept behind the switch, the `ant-webapp` fixture switches it on | `sbmig/loop/orchestrator.py`, `fixtures/projects/ant-webapp/sbmig.e2e.yml` | done |
| Web | `POST /api/runs?flow=`, `RequestRecord.flow`, Job env `SBMIG_FLOW` checked by the Worker, in-process pass-through; run id `<slug of the file name>-…` (24 characters, `web` without a letter), `upload_name`, `abort_reason` / `abort_reason_ko` | `sbmig/web/` | done |
| Page | header `panacea-remediating-oss`, no description, no footer; 전체 진행 / 취약점 해결; the stage rail and the result follow the flow; file names in the run list; the refusal reason as a sentence | `sbmig/web/static/index.html` | done |
| Korean abort reasons | a pattern for every abort reason reachable at intake and at the start of the remediation flow | `sbmig/report/i18n.py` | done |
| Docs | README, ARCHITECTURE, RUNBOOK, PROGRESS, design README, agent-doc 00 / 02 / 03 / 06 / 07 / 09 / 10 / 11, deploy README / ENV | `docs/`, `agent-doc/`, `deploy/` | done |
| Real Maven | the remediation flow on a migrated result ZIP against the local fake IVAS (`exit=2 build=ok … flow=remediation remediation=AWAITING_APPROVAL`, the uploaded bytes unchanged, `llm=0`); `ant-webapp` with the switch on PASS; the same project refused by default in both flows | scratch, `scripts/e2e.sh --only ant-webapp` | done |
| Known limits | a ZIP whose project sits two or more folders deep is not accepted by the remediation flow; without a root pom the reactor roots are built one by one (no generated aggregator) | — | open |
| Not seen yet | the remediation flow in the cluster (Job mode) and against the real IVAS | — | open |

## Status — 2026-11-12, Korean comments rewritten as hand-written comments

The business team read the Korean docstrings / comments and said they look AI-written ("주석들 너무 ai티 난다"). The user:
keep the English (the working record), rewrite only the Korean "개발자들이 남기는 주석처럼". After a first sample
("경로에서 src/… 이후 부분만 남긴다. 등록부 키로는 쓰지 않는다.") the user added: a comment must say what the function does,
in words a newcomer knows ("등록부 키가 뭔지를 모르겠는데 … 이런 기능을 하는 함수 … 아직 부드럽지가 않네"), and approved the
second sample ("파일 경로에서 ``src/`` 이후 부분만 잘라서 반환한다."). No code changed in this round.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Convention | reader = a developer new to the project; the first sentence says what the code does; length table; plain words instead of project-only words; no em dash / arrow / bold / version tag / date / detached particle in Korean; examples | `docs/v2/CODE-COMMENTS-KO.md` | done |
| Python comments | every Korean docstring part and `#` comment of `sbmig/**/*.py` rewritten (5,286 items in 164 files): a first pass for length and notation, a second for plain purpose-first sentences, a last pass over the lines the worklists did not reach | `sbmig/` | done |
| How it was applied | worklists extracted per file, new Korean returned as JSON, applied by a script from git HEAD: the code is identical (AST without docstrings), English docstring lines and English comments are kept | — | done |
| Page | the HTML / CSS / JS comments of the single-file page (104 comments); nothing the page shows changed | `sbmig/web/static/index.html` | done |
| Config comments | the operator explanations of the five yml files in the same wording (keys, values and commented-out examples untouched) | `config/*.yml` | done |
| Guard | Korean parts of docstrings and comments may not carry an em dash, arrow, bold, `v2.x` tag, ISO date or a detached particle; English parts (also an English sentence quoting Korean) are not checked | `tests/docs/test_korean_comments.py` | done |
| Docs | CLAUDE.md working note, agent-doc 01 / 08, PROGRESS | `CLAUDE.md`, `agent-doc/`, `docs/PROGRESS.md` | done |
| Tests | offline suite 5,061 passed, 58 deselected (one test added for the guard, one for its classifier); the AST without docstrings of all 164 changed Python files equals the commit before | — | done |
| Not changed on purpose | `deploy/` comments: the company keeps hand-filled copies of the manifests, comment churn there would make their manual merge harder | `deploy/` | open (ask first) |

## Status — 2026-11-15, framework projects run in the reduced scope `jdk-jakarta` (v2.1z-ak)

The user: a project that has the in-house framework's core artifact among its dependencies gets "jdk, jakarta 업그레이드만
하고 빌드하고 취약점 점검만 하는 방식" — the artifact may be declared directly or sit in a parent pom. Decisions the same
day: the fix loop runs ("jakarta 때문에 수정루프는 돌아야할거야"), the framework's own version stays ("그대로 두자"), a
presence through another library is checked too (the user was not sure such projects exist). The real artifact name is a
company coordinate and is not in the repository: the company lists it in its `config/artifacts.yml`.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Contract | `framework.artifacts` / `scope` / `transitive` / `recipe_sets`, `FrameworkEvidence` / `FrameworkUse`, `ProjectProfile.framework`, `MigrationPlan.scope`, `FixContext.scope` / `scope_facts`, in-house group `framework` | `sbmig/core/`, `config/default.yml`, `config/artifacts.example.yml`, `docs/INTERFACES.md` "v2.1z-ak" | done |
| Detection | four places: a project pom's `<dependencies>`; inherited from a parent pom outside the project (the as-is effective pom of intake); a bundled jar no pom declares; `dependency:tree` (one more Maven call, only when the first three found nothing). Sources that could not be read are named (`unchecked`) | `sbmig/analyze/framework.py`, `sbmig/build/deptree.py` (`dependency_tree_status`), `Orchestrator._check_framework` | done |
| Plan | scope `jdk-jakarta`: `transform.rewrite` (the sets of `framework.recipe_sets`, default jdk21 + jakarta) → `transform.jsp_jakarta` → `verify.build`; no restructure, no structure review, no `transform.compile`; a full plan is byte-identical | `sbmig/plan/planner.py` | done |
| Run | the JSP rename run by the orchestrator; the parent pom never aligned or replaced (an unresolvable parent aborts); BUILD SUCCESS → success → the chained remediation | `sbmig/loop/orchestrator.py` | done |
| Corrections the restructure used to make | found by the real-Maven e2e: `project.build.sourceEncoding` written BEFORE OpenRewrite (javac's default is UTF-8 since JDK 18; OpenRewrite skipped an MS949 file and left its javax import), the as-is `provided` scopes restored after the rewrite (the jakarta recipe had put the servlet API into the WAR) | `sbmig/restructure/source_encoding.py`, `scope_restore.py`, `Orchestrator._write_encodings_reduced` / `_restore_scopes_reduced`, `ErrorList.add_pom_verify(seen=)` | done |
| Fix loop | version facts from the project's own chain (never the company chain / the target Boot BOM); the framework artifact's version protected (pre-tries skip it, `_framework_guard` rolls back an attempt that changes it or a `<parent>`); a javax-era framework blocked at once naming its owner; any unresolvable parent aborts without the LLM | `sbmig/loop/fixloop.py` | done |
| Prompt | reduced-scope introduction and rules 3 / 8 / 14 in the system prompt (the full rendering is byte-identical), the section "Scope of this run", the verify guide's target | `sbmig/fix/` | done |
| Remediation | group `framework`: the framework and what it brings are residual `inhouse_artifact` (never a proposal or an override); patterns in the policy hash only when present | `sbmig/remediations/inhouse.py` (`InhouseKeys`), `planner.py`, `report.py` | done |
| Report / result line / page | `scope=jdk-jakarta`; the block 사내 프레임워크 프로젝트; no target Boot value for a project that was not converted; sbmig's pom corrections counted apart from build errors; unchanged `X → X` rows left out; Korean rules for the new sentences; the page's label and the stages shown as 하지 않음 | `sbmig/report/`, `sbmig/web/static/index.html` | done |
| Fixtures | stand-in framework `fixtures/libs/example-fw` (core, web, parent); `fw-direct` (MS949 source with a javax import), `fw-parent`, `fw-transitive` (`servlet_jakarta_10` listed), `fw-transitive-note` (full migration by `framework.transitive: note`); new e2e checks (`journal_sequence`, `journal_events_absent`, `archive_entries`, dotted journal keys) | `fixtures/`, `scripts/e2e.py`, `tests/fixtures/` | done |
| Real-Maven e2e | the four fixtures: 4/4 PASS. The three reduced runs: plan `transform.rewrite`, `transform.jsp_jakarta`, `verify.build`, BUILD SUCCESS, 0 LLM calls, 13–15 s each; the WAR holds no servlet API jar; the MS949 file's javax import is converted and its Korean bytes are intact. `fw-transitive-note`: the full migration, 1 LLM call (the structure review) | `out/e2e/summary.md` | done |
| Real-Maven e2e, every fixture | 48 fixtures: 46 PASS in the full run; `boot2-method-rename` and `boot2-fix-failures` failed because their replay answers were still keyed by the case id of signature rules 1 (`8bb4fd4a4a`; rules 2 give `21175ead2d` — the member symbol carries its owner type since v2.1z-ai, whose round had not re-run the real-Maven e2e): replay keys, `expected.json` and the fixture test moved, both re-run PASS. No other fixture asks for an answer that is missing | `fixtures/projects/boot2-method-rename`, `boot2-fix-failures`, `tests/fixtures/test_fixture_projects.py` | done |
| Docs | `agent-doc/13-framework-projects.md` (new) and the lines in 00–07, 10–12; README; design README §3 / §4.9; RUNBOOK §0.3; `deploy/README.md` §7; PROGRESS; CLAUDE.md | `agent-doc/`, `docs/`, `deploy/README.md` | done |
| Tests | offline suite 5,356 passed, 62 deselected (295 tests added: detection, the reduced plan, the orchestrator steps, the fix loop guard and facts, the prompt variants, the remediation group, the report, the fixture checks) | — | done |
| The company's part | two lines in the company `config/artifacts.yml` (`framework:` / `artifacts: ["<artifactId>"]`), then the image rebuilt (the file is baked in) | company side | open |
| Known limits | a framework still compiled against javax cannot work with jakarta project code while its version is kept: such a run ends BLOCKED naming the framework. The standard jakarta recipe ends on Jakarta EE 9 (servlet 5.0); `servlet_jakarta_10` is the company's choice. The full flow still writes the source encoding after OpenRewrite (the same MS949 skip exists there). A parent outside the project that sets only the compiler plugin's `<encoding>` is not seen | — | open |
| Not seen yet | a real company framework project; the chained remediation on one against the real IVAS; the page in Job mode | — | open |

## Status — 2026-11-17, Maven's `.m2` (settings.xml and the local repository) lives at `/data/.m2` (v2.1z-al)

The user, about the pods: "우리 이거 파드에 두면 안되서 pvc에 두는건데 /data/.m2에 뒀으면 좋겠어 일관되게!", then
"Settings도 저기로 옮겨가야하지않아!?" — `/data/.m2` is to the pods what `~/.m2` is on a PC. Before: the local
repository on a second PVC (`sbmig-m2` at `/home/sbmig/.m2`, `SBMIG_BUILD__LOCAL_REPO: "null"`), settings.xml a
ConfigMap file at `/etc/sbmig/settings.xml`.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Local repository | `SBMIG_BUILD__LOCAL_REPO` = `/data/.m2/repository` in 40 / 80 / 81 (slots: `repository-slot2`, `-slot3`); the `m2` volume and PVC `sbmig-m2` removed from 40 / 81 / 10 — one PVC (`sbmig-data`); 80 mounts its `m2` volume at `/data/.m2` | `deploy/k8s/` | done |
| settings.xml | a real file on the data volume, `/data/.m2/settings.xml` (`SBMIG_BUILD__SETTINGS_FILE` in 40 / 81); the operator copies it there; ConfigMap `sbmig-config` is the template (80 still mounts it at the same path, inside its emptyDir). Not a ConfigMap mount nested in the NFS volume: the runtime would have to create the mount point on NFS as root, which a root-squashing export refuses | `deploy/k8s/`, `deploy/README.md` §3 | done |
| A missing settings file fails early | preflight: `build.settings_file` set and not a readable file → flags `settings-not-found`, `infra:maven`, abort at intake (exit 3) before any Maven call, in both run flows; Korean reason on the page; one WARNING at web-pod start | `sbmig/loop/orchestrator.py`, `sbmig/report/i18n.py`, `sbmig/web/runs.py` | done |
| Image / worker | `VOLUME ["/data"]`; the worker's pod-local fallback message names the volume that holds `build.local_repo` | `deploy/docker/Dockerfile`, `sbmig/web/worker.py` | done |
| Docs | the operator list for an existing deployment (copy the file first, then the manifest lines, `sbmig-job-template`, restart), `deploy/ENV.md`, INTERFACES "v2.1z-al", RUNBOOK, agent-doc 02 / 09, CLAUDE.md | `deploy/README.md`, `docs/`, `agent-doc/` | done |
| Tests | offline suite 5,375 passed, 62 deselected (19 added: the manifest invariants, the preflight check in both flows and both engines, the startup warning, the Korean reason, no path leak into a local-job run) | — | done |
| The company's part | copy the settings file while the old ConfigMap mount is still there, change the lines in the hand-filled 40 / 81, re-create ConfigMap `sbmig-job-template`, restart the web pod; the first build per slot downloads its dependencies again; PVC `sbmig-m2` may be deleted once the new location works | company side | open |
| Not seen yet | the new layout on the company cluster (NFS) | — | open |

## Status — 2026-11-17, the remediation says where a library comes from (v2.1z-am)

The user read a real report from the company cluster: 조치 완료 and 사내 라이브러리에서 발생한 취약점 came out well; four
things did not. "루트 pom을 상속하지 않는 모듈이라는게 뭔지 모르겠어." "일부 모듈은 사내 라이브러리에서 가져옴. 이것도
결국 사내 라이브러리에 속해야 하는거 아니야?" "얘도 좀 어디서 발생 되었는지 알아야 조치를 할거 같아!" "IVAS에서는
2.2.0.release … 메이븐은 2.2.0.RELEASE … upper건 lower건 적용하게 해줘야" "classifier가 붙은 의존성은 뭐야?"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Contract | `ModuleSource` (how one module gets the library); `InventoryItem.sources` / `reported_ga` / `reported_version` / `variants`; `Residual.sources` / `reported_version`; all default empty, an older `state.json` loads and renders as before | `sbmig/remediations/models.py`, `docs/INTERFACES.md` "v2.1z-am" | done |
| Matching ignores case | one helper (`version_key`, `ga_key`, `same_version`, `same_ga`): exact g:a first, case-insensitive only when no exact g:a resolves; findings differing only by case are one group; items carry Maven's spelling, the IVAS spelling goes to `reported_*`; the resolution check, the rejected bookkeeping, the counts and the residual merge use it | `sbmig/remediations/matching.py`, `inventory.py`, `apply.py`, `planner.py`, `graph.py` | done |
| In-house split by module | the modules that get a library through an in-house artifact form their own item (residual `inhouse_artifact`); the other modules are planned by their own facts. An editable version text is never split (it is the project's own). `Residual.inhouse` only on in-house residuals; the note "일부 모듈은 …" is gone | `inventory.py`, `planner.py` | done |
| Provenance | one `ModuleSource` per module (origin, path, declaration line, manager, in-house artifacts, variant) on every item and residual | `inventory.py`, `planner.py`, `graph.py` | done |
| Variant overrides | the override entry carries the dependency's `<type>` / `<classifier>`; one proposal per management key, each approved on its own; the plain-jar entry text and its `proposal_hash` unchanged; existing-entry, declared-version and resolution checks compare the full key; `locate_override(…, type_, classifier, pom)` works for any project pom (only the root is used) | `apply.py`, `planner.py` | done |
| Report | 들어온 경로 under the 직접 확인 tables (per module when sources differ) and, where the 의존 경로 column does not already say it, under the 사내 라이브러리 tables; `not_inheriting_root` retitled 모듈 pom에 직접 지정해야 하는 버전 and explained without inheritance words; a classifier explained in one sentence; `2.2.0.RELEASE (IVAS 표기: 2.2.0.release)`; Korean rules for the new sentences and for three structure-check messages that used to print as 원문 | `sbmig/remediations/report.py`, `i18n.py` | done |
| Docs | agent-doc 11 / 12 (the per-module in-house rule, the count semantics, 들어온 경로), INTERFACES, CLAUDE.md | `agent-doc/`, `docs/`, `CLAUDE.md` | done |
| Tests | offline suite 5,470 passed, 62 deselected (95 added: the matching helper, the split and the sources in the inventory, variant overrides incl. byte identity of the plain entry, the in-house planner cases, a fake flow with lower-cased IVAS versions, the report sources and wording, the i18n samples) | — | done |
| Open, the user's decision | override proposals in the module's own pom for modules that do not inherit the project root (asked 2026-11-17 with a recommendation; the user asked which cases it covers). Until then such libraries stay under 직접 확인 with their paths | — | open |
| Known gap | an existing root entry for a classifier changed by version text is verified at g:a level | `apply.verify_resolution` | open |
| Not seen yet | the changed report on a real company run | — | open |

## Status — 2026-11-17, override proposals in a module's own pom, chosen by the owners; settings.xml seeded from the ConfigMap (v2.1z-an)

The user, after asking which cases a module-level entry covers and whether it still works when a parent brings a
version: "일단 본인들이 선택하도록 만들자. 취약점 해결이니까 선택해서 박을지 아니면 검토 후에 진행할지는 본인들 선택으로
할 수 있도록 해야할것 같아!" And, reading the deployment steps of v2.1z-al: "configmap 에 설정되어있으면 파드 배포할때
자동으로 나가는게 아니야?" — the hand copy of settings.xml was an out-of-band step.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Module-level proposals | a library that reaches a module transitively (or through a parent / BOM outside the project that is not in-house) and whose module does not inherit the project root: one `managed_override` proposal per (module pom, management key) inserting the entry into that module's own pom; modules that inherit the root keep the root entry; existing-entry, declared-version and placement checks per target pom. Switch `remediation.module_overrides` (default true; in the policy hash only when false) | `sbmig/remediations/planner.py`, `apply.py`, `graph.py`, `sbmig/core/config.py`, `config/default.yml` | done |
| Always the owners' choice | every such proposal waits for approval whatever `require_approval` says; the run stays `AWAITING_APPROVAL` until they decide (at once or after their review); checked = applied, unchecked = reported as their choice with the pom, the entry and the path | unchanged rule, `report.py` | done |
| An entry without effect | an inserted entry that does not change the resolved version (a dependency declared with a version wins) is removed again, the candidate is rebuilt once and the rest of the round is kept: `decision: "ineffective"`, one residual per library naming the poms, journal `override_ineffective`. A version TEXT change without effect, any other verification problem, a second failure or no time left keep `FAILED resolution_mismatch` | `apply.split_ineffective`, `graph.py` | done |
| Approval screen | proposals grouped by library (+ type / classifier) with a group checkbox; each row says which pom is edited and how the library arrives; 전체 선택 / 전체 해제; the button reads 선택한 N건 적용; a notice that nothing is applied until then; the selection survives the status refresh; verified in a browser against a fake server (12 proposals, 390 px and wide, keyboard) | `sbmig/web/static/index.html` | done |
| Report | 승인 대기 grouped by library, one line per pom (pom, entry, 들어온 경로); new section 선택하지 않아 적용하지 않은 변경; group 버전 지정을 넣어도 바뀌지 않는 버전; the top of the report tells the owner to choose on the page; Korean rules for every new sentence | `sbmig/remediations/report.py`, `i18n.py` | done |
| settings.xml from the ConfigMap | ConfigMap `sbmig-config` is the source again: the web pod mounts it at `/etc/sbmig/settings.xml` and, at start, copies it to `/data/.m2/settings.xml` (`SBMIG_SETTINGS_SEED`, atomic replace, no write when equal, a WARNING when it cannot); Job pods read the file on the shared volume; the hand copy is gone from every document. `SBMIG_REMEDIATION__MODULE_OVERRIDES` line in `sbmig-env` (commented) and 81 | `sbmig/web/runs.py`, `contract.py`, `localjob.py`, `deploy/k8s/20`, `40`, `81`, `deploy/README.md`, `deploy/ENV.md` | done |
| Docs | INTERFACES "v2.1z-an" and the seed in "v2.1z-al", agent-doc 09 / 11 / 12, RUNBOOK, PROGRESS, CLAUDE.md | `docs/`, `agent-doc/` | done |
| Tests | offline suite 5,552 passed, 62 deselected (82 added: module-level proposals incl. approve-one-reject-other, existing entries, variants, a DTD pom, hash and plan pins; the drop-and-rebuild path; the report sections and Korean rules; the page; the settings seed and the manifest invariants) | — | done |
| Known gaps | a dropped entry's residual merges into an existing `manual_review` residual of the same library and version, whose earlier detail then stands; an existing root entry for a classifier changed by version text is verified at g:a level | `graph._merge_residuals`, `apply.verify_resolution` | open |
| Not seen yet | module-level proposals and the new approval screen on a real company run; the seeded settings file on the company cluster (NFS) | — | open |

## Status — 2026-11-17, settings.xml is the ConfigMap mounted at `/data/.m2/settings.xml`; the copy is gone

The user, on the seed copy of v2.1z-an: "40번 SBMIG_SETTINGS_SEED는 필요없는내용 아니야?" … "어차피 보내서 웹 파드 재배포
하면 복사 필요 없는거 아냐? 만들어지는거 아니야? 저 복사로직이 필요한 이유가 뭐야?" After the explanation (a ConfigMap
file attached inside the NFS volume needs its attachment point to exist there; the runtime would create it as root,
which a root-squashing export refuses): "내가 쉘로 붙어서 .m2파일을 만들어 두면 될까!?" → "지금 만들어 두었어!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Delivery | settings.xml reaches every pod that runs Maven only through the ConfigMap mount: 40 and 81 mount `sbmig-config`'s `settings.xml` at `/data/.m2/settings.xml` (inside the `/data` PVC mount), 80 inside its emptyDir; `SBMIG_BUILD__SETTINGS_FILE` unchanged | `deploy/k8s/40`, `81`, `80`, `20` | done |
| Copy removed | `seed_settings_file`, `SBMIG_SETTINGS_SEED`, their constants, tests and every sentence about seeding | `sbmig/web/runs.py`, `contract.py`, `localjob.py`, docs | done |
| Mount point on a fresh volume | 40 has an initContainer (same image and user, mounts only `data`) that runs `mkdir -p /data/.m2 && touch /data/.m2/settings.xml`, so the attachment point is created by the pod's own user and nothing has to be created on NFS as root; optional where it was created by hand (the company cluster) | `deploy/k8s/40-deployment.yaml` | done |
| An empty settings file | a zero-byte file at `build.settings_file` (the mount is missing) aborts at intake like a missing one (`settings-not-found`), with its own reason in English and Korean; the web pod's startup WARNING covers it | `sbmig/loop/orchestrator.py`, `sbmig/report/i18n.py`, `sbmig/web/runs.py` | done |
| Docs | why the mount point must exist, what a failure looks like (`describe pod` Events) and the fallback to `/etc/sbmig/settings.xml`; the operator's line list | `deploy/README.md`, `deploy/ENV.md`, INTERFACES "v2.1z-al", RUNBOOK, agent-doc 02 / 09, CLAUDE.md | done |
| Tests | offline suite 5,546 passed, 62 deselected (the manifest invariants for the nested mount and the initContainer, the empty-file preflight case in both flows, its Korean reason, the startup warning; the seed tests removed) | — | done |
| Not seen yet | the nested mount and the initContainer on a cluster — not run here (no local cluster was up) nor on the company's NFS; the fallback is one path change | — | open |

## Status — 2026-11-17, settings.xml is a plain file on the data volume

The nested ConfigMap mount of the round above was deployed on the company cluster and stopped at its first step: the
initContainer failed with `touch: cannot touch '/data/.m2/settings.xml': Permission denied` (the mount point the user had
made by hand belongs to another user than the pod's). The user then asked the simpler question — "어차피 /data 볼륨
마운트를 하는데 /data/.m2를 마운트를 별도로 해야해? 하나 마운트 된곳에다가 다같이 쓰면 안되는거야?" — and decided: "어차피
settings.xml은 바뀔일이 거의 없잖아? 일반 파일로 두고 가는것도 좋을거 같은데!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The file | `/data/.m2/settings.xml` is a plain file on PVC `sbmig-data`, like `~/.m2/settings.xml` on a PC; `SBMIG_BUILD__SETTINGS_FILE` unchanged in 40 / 80 / 81 | `deploy/k8s/40`, `81`, `80` | done |
| Nothing mounted inside `/data` | 40 and 81 mount only `data` at `/data`; the initContainer is gone; 81 has no `config` volume (it stays as commented lines for `artifacts.yml` by ConfigMap) | `deploy/k8s/40`, `81`, `tests/deploy/test_manifests.py` | done |
| Source and copy | ConfigMap `sbmig-config` stays the reviewed source: 40 mounts it read-only at `/etc/sbmig/settings.xml`, the operator copies it once as the pod's user (`mkdir -p /data/.m2 && cp /etc/sbmig/settings.xml /data/.m2/settings.xml`), again after each change; no copy code in sbmig | `deploy/README.md` §3, `20-configmap.yaml` | done |
| 80 | no data PVC, so it keeps the ConfigMap file mounted at `/data/.m2/settings.xml` inside its emptyDir | `80-build-job.template.yaml` | done |
| Messages | the missing-file and empty-file abort (`settings-not-found`) and the web pod's startup WARNING point at the copy, in English and Korean | `sbmig/loop/orchestrator.py`, `sbmig/web/runs.py`, `sbmig/report/i18n.py` | done |
| Folder owner | `/data/.m2` must be writable by the pod's user (uid 1000); the check and the remedy for a folder made by another user are in the operator text | `deploy/README.md` §3 | done |
| Image | `VOLUME ["/data"]` only since the `.m2` move; a hand-kept Dockerfile with `/home/sbmig/.m2` in that line can drop it (no effect in Kubernetes) | `deploy/docker/Dockerfile` | done |
| Not seen yet | the copy and the first build on the company cluster (the user is deploying) | — | open |

## Status — 2026-11-17, the admin page: token usage per project and log download (v2.1z-ao)

The user, while waiting for the deployment: "현재까지 프로젝트 별로 사용된 토큰량 볼 수 있는 페이지가 필요한데, 하드코딩으로
[a fixed password]으로 접속 할 수 있는 url하나 fastapi에 붙여서 해당 비밀번호로 접속해서 볼 수 있도록 하나 하자. 그리고 총
로그들 다운로드 받을 수 있도록도 하고!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Page and routes | `GET /admin` (one static file, not linked from the main page) and `/admin/api/login`, `logout`, `session`, `usage`, `logs.zip`, `runs/{run_id}/logs.zip` (+ the `…/check` preflights of the review round) | `sbmig/web/admin.py`, `sbmig/web/static/admin.html`, `sbmig/web/app.py` | done |
| Password | built in as a salted PBKDF2 hash (the plain text holds a company name and never enters the repository); a non-empty `SBMIG_ADMIN_PASSWORD` replaces it (web pod only, a secret name, dropped from a `local-job` child); session token in process memory, cookie `sbmig_admin` for 12 h; 5 failures per address in 10 minutes → 429 | `sbmig/web/admin.py`, `sbmig/web/localjob.py` | done |
| Usage | read from the run folders at each request, grouped by the uploaded file name: `report.json` (`llm_usage`, `llm_usage_by_model`, `llm_cost`), else the `llm call … in=… out=…` lines of `sbmig.log`; a cost sum is null when any run in it is unpriced | `sbmig/web/admin.py` | done |
| Logs | a ZIP per run or for all runs: `sbmig.log`, `journal.jsonl`, `control/request.json`, and `remediation/remediation.log` once nothing writes it any more | `sbmig/web/admin.py` | done |
| Docs | contract, operator text, repo map | INTERFACES "v2.1z-ao", `deploy/README.md`, `deploy/ENV.md`, agent-doc 01 / 09, `CLAUDE.md`, PROGRESS | done |
| Not seen yet | the page on the company cluster (it arrives with the next image) | — | open |

## Status — 2026-11-17, what the three-pods review found (v2.1z-ap)

The user, after the admin page: "그거 작업 끝나면 3개 병렬실행에 문제될만한 기능이 있을지 검토한번만 해줘!" Four read-only
reviewers went through everything added since the last concurrency round (v2.1z-ag … v2.1z-ao) and reproduced each
finding with a scratch test before reporting it. One of those scratch tests (a log appended without a limit while it was
zipped) filled the developer's disk; briefs now require a size cap and a time cap for any test that writes in a loop.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Maven and build environment | reviewed, nothing found for three pods (each slot its own repository, settings read-only); in-process mode only: a resumed chained remediation now uses the chained run's own repository, the standalone upload keeps `<repo>-remediation` | `sbmig/remediations/service.py` | done |
| KB: scripts of two scopes in one case | `CaseScript.scope` / `ScriptRun.scope` (`""` = `full`); reuse ranks (same scope, proven, version), a version of the other scope is tried only after the run's own and once per case and run; counters, `last_failure` and proven status count own-scope runs only; a framework-guard rollback of an other-scope version is the run result `out_of_scope`, not a failure; the hint line labels a solution from the other scope; `kb cases show` and `cases.md` print the scope | `sbmig/core/models.py`, `sbmig/errors/cases.py`, `sbmig/kb/store.py`, `sbmig/kb/cli.py`, `sbmig/triage/scripts.py`, `sbmig/loop/fixloop.py` | done |
| KB: a case id moved by a re-sign while a pod held it | `credit` follows the moved case's aliases (by the code that ran), else one WARNING; an augment is stored in the moved case when the L2 matches; a `parent` the case does not have is dropped | `sbmig/errors/cases.py` | done |
| Remediation: one driver per run folder | `<run>/remediation/claim.json` created exclusively by whatever drives the state, released on every end, refreshed every 30 s, stale after 600 s or a dead pid on the same host (journal `remediation_claim_taken_over`); a second process refuses and changes nothing; the web pod's approval write answers 409 while a live claim exists | `sbmig/remediations/chain.py`, `service.py` | done |
| Remediation: a kill during drop-and-rebuild | state (`applied`, the residual, `next_node=apply`) is saved before the candidate is removed; marker `rounds/<n>/candidate.ready.json` — no build and no delivery of a candidate without it | `sbmig/remediations/graph.py` | done |
| Remediation: proposals that come back after a policy change | `state.message` says why (English + Korean), journal `approvals_dropped` | `sbmig/remediations/graph.py`, `i18n.py` | done |
| Admin log ZIP: a log still being written | `remediation/remediation.log` follows the remediation API's own withholding rule (an active or recovered resume included) | `sbmig/web/admin.py` | done |
| Admin log ZIP: disk use | built in the pod's local temp dir, one at a time (409), 2 GiB cap (413), 1 GiB free floor (507), files over 512 MiB skipped and listed, members copied up to their size at the start, preflight routes and Korean messages on the page; the hour-old sweep is gone | `sbmig/web/admin.py`, `sbmig/web/static/admin.html` | done |
| Checked, nothing found | double resume on the Job side, slots and the FIFO line, approvals validation, env pins, the usage scan skipping `_admin` (by code reading; part of the web review's own tests never ran because of the disk incident) | — | done |
| Docs | INTERFACES "v2.1z-ap" (+ "v2.1z-ao" log rules), agent-doc 04 / 11 / 12, `deploy/README.md`, `CLAUDE.md`, PROGRESS | — | done |
| Tests | offline suite 5,639 passed, 62 deselected (this round and the two rounds above: admin page, plain settings file) | — | done |
| Known gaps | a kill between moving an earlier verified ZIP aside and freezing a failed candidate leaves the verified ZIP on disk without a pointer in the state (older than this round); the refused second resume's sentence is in `sbmig.log` only, not in `remediation-job-status.json`; the web pod's local temp dir is node disk without an `ephemeral-storage` limit in the manifests | — | open |
| Not seen yet | three pods on the company cluster / NFS | — | open |

## Status — 2026-11-17, a build pod that does not start (v2.1z-aq)

The first two uploads at once on the company cluster: one build pod ran, the other stayed in `ContainerCreating` for
28 minutes — "이거 죽이는 방법좀 알려줘 이거 왜 둘이 동시에 넣으니까 하나는 생성만 계속 시도하지?" The pods had landed on
different nodes and the second node could not mount the NFS volume (`FailedMount`, `mount.nfs: rpc.statd is not running
but is required for remote locking`): a node setup problem, the infra team's to fix. Two gaps on our side showed: the
stop button waited the whole 45-minute grace for a pod that had no Worker to read the stop file, and nothing on the
page said why the pod did not start.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Stop before the pod started | a stop while the Job is `Creating` / `Pending` deletes our Job at once (both labels, as before), ends STOPPED, frees the slot; journal `job_deleted_before_start`; a running pod keeps the grace; a running remediation resume is never deleted | `sbmig/web/runs.py`, `sbmig/web/k8s.py` | done |
| Why the pod does not start | after `SBMIG_JOB_PENDING_NOTICE_S` (120) the web pod reads the container's waiting reason, the scheduling condition and the pod's events and shows a Korean sentence by reason family plus the raw line under the pod step; `status()` carries `job.pending`; journal `job_pod_pending`, one WARNING per reason; never fails the run or frees the slot | `sbmig/web/runs.py`, `sbmig/web/k8s.py`, `sbmig/web/static/index.html` | done |
| Events permission | `list events` asked once at startup (denied = one info line, the page then points at `kubectl describe pod`); added to the service-account RBAC manifest | `sbmig/web/runs.py`, `deploy/k8s/45-web-rbac.yaml` | done |
| Operator text | what `ContainerCreating` for minutes means, the Events lines and what to do (the `rpc.statd` case of this run included: start statd on the node or mount with `nfsvers=4.1` / `nolock`; sbmig's own locks are exclusive creates and need no NFS lock service; `nodeSelector` in the 81 template as a stopgap), how to end such a run | `deploy/README.md` §3, `deploy/ENV.md`, INTERFACES "v2.1z-aq", agent-doc 03 / 09 / 10 | done |
| Tests | offline suite 5,672 passed, 62 deselected | — | done |
| Not seen yet | the notice and the immediate stop on the company cluster; whether the kubeconfig account may list events | — | open |
| Maven repository shared by the build pods | asked by the business team, decided the same day: the per-slot repositories stay ("일단!! 이걸로 가고! 나중에 얘기나오면 공용으로 해결봐야할거같아!"). If it comes back: slot 1's repository as a read-only tail for slots 2 / 3 (`maven.repo.local.tail`, Maven ≥ 3.9.0) behind a switch; one repository with Maven's `file-lock` named locks needs NFS locks that work across nodes (NFSv4.1 or statd everywhere, never `nolock`) | — | open |

## Status — 2026-11-17, disk walls for test runs

A review subagent's scratch test reproduced "zip a log while it is being appended" with a writer thread that had no
size limit and no time limit; two runs wrote one 118 GB file into pytest's temp folder and filled the developer's
disk, and every other session on the machine started failing. The briefs already got a written rule that day. The user
asked to tighten it ("응 한번 조여줘봐!"): limits that hold when a brief forgets them.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Walls on every pytest run under the repository | per-file size cap (`RLIMIT_FSIZE`, 1024 MiB: the writer fails with `File too large`), free-space floor (5120 MiB, checked before the run and every second), cap on the run's own basetemp (4096 MiB, walked at most ~5 % of the time); a trip prints `bounded: STOPPED: <reason>` past pytest's capture and exits 125; the header's `bounded:` line shows the limits in force; env `BOUNDED_FILE_MB` / `BOUNDED_FLOOR_MB` / `BOUNDED_GROWTH_MB`, `0` = off | `conftest.py` (root) | done |
| Wrapper for everything else | `scripts/bounded.py [--file-mb N] [--seconds N] [--tmp-mb N] [--floor-mb N] [--tmp DIR] [--keep] -- <command>`: the same caps in the child, a wall-clock cap (the whole process group is stopped), the command's `TMPDIR` is a folder the wrapper measures and removes, nothing is left running after the command; exit 124 time, 125 disk, 126 refused, 127 not started | `scripts/bounded.py` | done |
| Working rules | scratch tests live under `out/scratch/<topic>/` so the root conftest applies; a pytest run without the `bounded:` header line has no wall; anything else goes through the wrapper; a run that timed out or was stopped is not started again before looking at what it wrote | `CLAUDE.md` "How to work here" | done |
| Tests | `tests/devtools` (41: the wrapper end to end with small caps, the decision logic with fakes, the suite guard in a throwaway project); offline suite 5,713 passed, 62 deselected (its basetemp held about 360 MB) | `tests/devtools/` | done |
| Known limits | a test outside the repository run without the wrapper has no wall; the basetemp is read through pytest's private `_tmp_path_factory._basetemp` (a rename would be caught by `test_growth_cap_stops_the_process_with_125`); no per-test time cap inside pytest (the wrapper's `--seconds` covers a whole run); the file-size cap does not exist on Windows (the header says so) | — | open |

## Status — 2026-11-18, no proposal waits for approval (v2.1z-ar)

Relayed from the project teams: "취약점진단 승인 없애고 그냥 쭉 다 일괄 승인해서 진행해서 결과만 알려 달래! 여기 빌드파드
제한이 3개니까 이쪽까지 승인까지 하긴 힘들다고!" An approval ended the build pod and its resume took a second pod out of
three. The shipped configuration now applies every proposal on its own; approval stays in the code as an opt-in.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Defaults and the one rule | `remediation.always_approve` defaults to `[]`; `requires_approval = require_approval or (risks ∩ always_approve)`; the planner's fixed rule "an override entry always waits" and its note sentence are gone | `sbmig/core/config.py`, `config/default.yml`, `sbmig/remediations/planner.py` | done |
| One pod | with the shipped configuration no run saves `AWAITING_APPROVAL`; a chained remediation ends in the migration's build pod (a further pod only after a stop, `chain_budget` or `AWAITING_CONFIRMATION`) | `tests/remediations/test_no_approval.py` | done |
| Runs left waiting by an earlier version | one press of the page's apply button: the resume plans again under the new policy hash, unchecked proposals stay rejected, the rest is applied, no second wait | `tests/remediations/test_no_approval.py` | done |
| Report | 조치 완료 gets the column 「주의」 when an applied change carries a risk flag (unchanged bytes without one); 해야 할 일 gets 「자동으로 적용한 변경 확인하기 (N건)」 for changes applied with decision `auto` and a flag; the list under a grouped library reads "수정한 pom" there | `sbmig/remediations/report.py` | done |
| Page | the rail note no longer speaks of approved changes; the approval screen is unchanged and appears only with approval switched on | `sbmig/web/static/index.html` | done |
| Switching approval on | `SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"` in `sbmig-env` (every proposal) or `remediation.always_approve` in the company yml (single classes; no env key, the Job template passes none) | `deploy/README.md` §6, `deploy/ENV.md`, `agent-doc/11-remediations.md` | done |
| Operator text | the approval rule, how to read 「주의」 and the new step, the passage for an existing installation | `agent-doc/11-remediations.md`, `agent-doc/12-remediation-report.md`, `deploy/README.md`, `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md` | done |
| Disk walls: a faked `shutil.disk_usage` | a product test fakes `shutil.disk_usage` ("disk full"); the watch thread of the same pytest run could read the fake and stop the run with exit 125. The probe is bound when `scripts/bounded.py` is loaded | `scripts/bounded.py`, `tests/devtools/test_bounded.py` | done |
| A timing race in a test | the pod-pending test read the journal right after the status it polled for; it waits for the journal line now | `tests/web/test_pod_not_started.py` | done |
| Tests | offline suite 5,736 passed, 62 deselected | — | done |
| Known limits | a change set that breaks the candidate build fails as a whole (`candidate_build_failed`; the failed candidate is delivered and marked): nothing is retried without single proposals, so one breaking major change costs the safe changes of that round too. A deployment whose `sbmig-env` has `SBMIG_REMEDIATION__REQUIRE_APPROVAL: "true"` keeps approval on | — | open |
| Not seen yet | an unattended remediation on the company cluster (how often a major change breaks the build, whether the teams read 「주의」) | — | open |

## Status — 2026-11-18, the Korean report.md is a guide for the requester (v2.1z-as)

The user read a company report and found it hard to read: "너무 기계적으로 리포트가 나왔다". Its purpose in their words:
a guide for the person who asked for the migration ("우리 이렇게 마이그레이션 했고 이런거 확인해주세요~"); no error lists,
no per-round fix tables, the modified files with their final diff; the head table reduced to six rows; the words 사유,
관리됨 and "as-is 버전으로 고정했다" named as odd.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Two renderers | `report.language: ko` renders the guide, written in Korean directly: head table (결과, SBMIG-RESULT, 전환 후, 실행 ID, 시작 / 종료, 플래그) → 전환 내용 → 확인해 주세요 → 코드 수정 내역 → 해결하지 못한 오류 → 진행하지 않은 항목 → special sections → 참고; `en` keeps the diagnostic layout byte for byte | `sbmig/report/guide_ko.py`, `sbmig/report/markdown.py` (hook), `tests/report/test_guide_ko.py`, `tests/report/golden/` | done |
| One net diff per modified file | `RunReport.code_changes` / `code_changes_total` / `code_changes_base`: the change of each file between the commit right after the automatic conversion and the final tree, with the LLM's change sentences and the cause; caps 30 files with diff / 80 lines / 500 listed; mode-only changes left out | `sbmig/report/changes.py`, `sbmig/loop/orchestrator.py`, `sbmig/core/workspace.py`, `tests/report/test_code_changes.py`, `tests/loop/test_code_changes_flow.py` | done |
| Library advisories in Korean | `title_ko` / `summary_ko` / `followup_ko` / `replacement_ko` for all 67 entries; an item of 확인해 주세요 = the title sentence, what the run did, then 대체할 라이브러리 / 설명 / 할 일 / 수정한 파일 / 문서 | `knowledge/libraries/advisories.yml`, `sbmig/advisory/evaluate.py`, `tests/advisory/test_korean_texts.py` | done |
| The remaining translated lines | 204 of 344 phrase values and 58 of 80 line rules reworded (polite 합니다체, no word-for-word code vocabulary), 52 rules added for texts that used to stay English; tests forbid the plain style, "~할 것" endings and the banned words | `sbmig/report/i18n.py`, `tests/report/test_report_i18n.py` | done |
| Explanations of the LLM | the fix prompt asks for `<analysis>` / `<solution>`, the review prompt for its problem / hint / analysis, in the language of the change descriptions (Korean with `report.language: ko`) | `sbmig/fix/prompts/script_user_v1.md.j2`, `sbmig/review/prompts/review_v1.md` | done |
| Operator and design text | the report chapter of the design README, the agent-doc pack, RUNBOOK, ARCHITECTURE, config comments | `docs/migration-agent/README.md`, `agent-doc/`, `docs/` | done |
| Known limits | texts the LLM stored in the KB earlier stay in their language; a few follow-up families are still English (the web.xml registration lines, the operator-check guide line generated in code); the 구분 column of 진행하지 않은 항목 shows the reason code; the Korean report's remediation section and the remediation report keep their own vocabulary (라운드, 사유) | — | open |
| Not seen yet | a company run's report in the new form | — | open |

## Status — 2026-11-18, Hystrix is replaced with Resilience4j by a recipe again (v2.1z-at)

In the same report the user saw Hystrix and a Sleuth artifact kept at their as-is versions while Springfox was
replaced, and remembered an automatic replacement. It had existed (round 6, an in-house recipe) and was deleted with
the v1 flow; since then the as-is version was carried over, the old jar compiled and the build was green although
Hystrix does not work on Boot 4. Decision: "이게 동작하지 않는건 되살려서 동작하도록 해주는건 맞는거 같아!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The recipe | composite `com.company.migrate.HystrixToResilience4j`: `@HystrixCommand` → `@CircuitBreaker`, the starters swapped, the Hystrix artifacts and enable-annotations removed; it writes no version | `knowledge/recipes/base/hystrix-to-resilience4j.yml`, `knowledge/recipes/catalog.yml` | done |
| When it runs | set `openrewrite.recipes.hystrix_resilience4j` (`[]` = off), planned when the analyzer finds `hystrix`, after Java 21 / jakarta and before the Framework / Boot sets in the one OpenRewrite run; in the framework scope only when `framework.recipe_sets` names it | `config/default.yml`, `sbmig/plan/planner.py`, `sbmig/plan/catalog.py`, `sbmig/rewrite/runner.py` | done |
| Versions | the new starter leaves the recipe without a version: the upgraded Spring Cloud BOM manages it (the Boot umbrella adds the BOM when a Boot project has none; the restructure imports it for a non-Boot Spring project); otherwise the existing repository lookup | — | done |
| Code the recipe cannot port | `extends HystrixCommand` no longer compiles and is ported by the fix loop with the `## fix-loop: hystrix` note | `knowledge/notes/spring-cloud.md`, `fixtures/projects/boot2-hystrix-command/replay/` | done |
| Advisory | `hystrix` ends `replaced`; its English and Korean texts say what was replaced and what to review at runtime | `knowledge/libraries/advisories.yml` | done |
| Real pipeline | `scripts/e2e.py` (Maven + OpenRewrite): `boot2-cloud-legacy` PASS (hystrix / sleuth / springfox / spring-cloud-bom `replaced`), `boot2-hystrix-command` PASS (the command class ported), four fixtures without Hystrix unchanged | `fixtures/projects/` | done |
| Known limits | a project that declares only `hystrix-javanica` gets the annotations swapped but no Resilience4j dependency (the fix loop adds it; not run for real); Sleuth-family artifacts other than `spring-cloud-starter-sleuth` are still carried over (the artifact in the company report is not known yet) | — | open |

## Status — 2026-11-18, the flow deck is brought up to date and written for users

The user asked for the deck's latest state and, while it was being redone: "사용자들한테 나갈거라 기계적 문구 말고 한국인
사용자를 대상을 고려해서 내용을 써줘!" The deck goes to the developers who upload projects and read the results.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| 20 slides | overview of 12 steps (⑪ 취약점 조치 before ⑫ 마무리, as the code runs them), 프로젝트에 따라 길이 달라집니다 (the two run flows, the framework road, what is not accepted), the nine step slides plus ⑪ 취약점 조치, the four fix-loop slides, 보고서는 이렇게 읽습니다, 취약점 조치 결과는 이렇게 봅니다, the knowledge base | `docs/migration-agent/migration-agent.pptx`, `scripts/handbook/build_migration_agent_deck.js`, `scripts/handbook/steps/` | done |
| Facts | every slide checked against the code (Ant projects are refused with a notice; three pods; the reduced scope of framework projects; no approval in the remediation; the report sections; Hystrix / Springfox / Sleuth replacements; 50 rounds, 8 groups a round, 20 LLM asks) | — | done |
| Wording | for the user: what happens to my project, what I see, what I do; no setting keys, file / function names, flags or exit codes in slides; speaker notes as what a presenter says | — | done |
| Checks | `validate.py` passes; all 20 slides rendered to images and read | `scripts/handbook/README.md` (build and check commands) | done |
| Found on the way | the report, the KB records and `result.zip` are written after the chained remediation (the graph node `finalize` only settles a stop) — design README, ARCHITECTURE and agent-doc 02 corrected; the web page's stage rail shows 보고서 before 취약점 조치 although report.md is ready only after it | `docs/migration-agent/README.md`, `docs/ARCHITECTURE.md`, `agent-doc/02-flow-and-nodes.md` | page order open |
| Tests | offline suite 5,846 passed, 62 deselected | — | done |

## Status — 2026-11-18, the code-change view of the result card (v2.1z-au)

Relayed by the user: "마이그레이션 완료하면 github나 bitbucket처럼 소스코드 수정 화면으로 볼 수 있게 … 나중에 모달로 분리하기
쉽게. 부트쪽은 표현이 편할텐데, 스프링이나 논프레임워크 같은 경우 변환기준으로 보여주고 어디 있던 java 파일이 이 주소로 왔다도
표시 … 수정분만 보여주지 말고 위 아래 코드들도 좀 보이도록!" The place: the room the approval list left in the result card.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The record | at finalize the build side writes `<run dir>/codediff/` (`index.json`, `summary.json`, `patch.bin`): every file of the final tree at its FINAL path against its origin in the as-is tree; statuses modified / added / deleted / moved (+ whether the content changed), the folder moves grouped (`moves`), 10 context lines (`report.code_diff`, `report.code_diff_context`) | `sbmig/report/codediff.py`, `Orchestrator._finalize_code_diff`, `sbmig/core/config.py`, `config/default.yml` | done |
| Origins without rename detection | same path → same name and identical bytes → same name and at least half of the lines shared, candidates by the longest shared trailing path; a base tree holding each origin's blob at the final path makes ONE `git diff` show every file where it ended up; about twenty git calls whatever the size of the tree (1,500 moved files: under 3 s) | `sbmig/report/codediff.py`, `Workspace.snapshot_tree` / `tree_entries` / `make_tree` | done |
| Web API | `GET /api/runs/{id}/codediff`, `…/files/{n}` (parsed hunks with both line numbers), `…/files/{n}/lines` (more lines of the final file out of `result.zip`); the web pod runs no git; `code_diff` in the run status; 409 until the results are released, damaged data is a 404 with a reason, never a 500 | `sbmig/web/codediff.py`, `sbmig/web/app.py`, `sbmig/web/runs.py` | done |
| The page | block 「코드 변경」 after 「취약점 조치」: summary chips, 「폴더 구조가 바뀌었습니다」 (원래 위치 → 옮긴 위치, 파일 수), filters and path search, a file tree by final path, the files one under the other with 「원래 위치」, both line numbers, the differing part of a changed line marked, more lines above / below / between and 「파일 전체 보기」; files only moved are listed from the start when there are at most 200 of them, else behind a checkbox; drawn 30 files at a time, a file's changes fetched when it nears the viewport | `sbmig/web/static/index.html` | done |
| A modal later | the block is the component `CodeDiff` (`mount(host, runId)` / `unmount()`, its own state, DOM inside `host`, styles under `.cdiff`): a dialog calls `mount` with its body | `sbmig/web/static/index.html` | done |
| Found on the way | the temporary index of the snapshot must keep the real index's timestamp, else a same-size edit made in the index's own second is missed (git's racy-clean check) — reproduced by a test | `sbmig/core/workspace.py`, `tests/core/test_workspace_trees.py` | done |
| Real pipeline | `scripts/e2e.py` on `company-servlet-jsp`, `company-spring5-web`, `boot2`: all PASS, each wrote the record in 0.27 s; the page was opened on those run folders in a browser (open / close, moves table, moved files with their old place, more lines) | `out/e2e-codediff/` | done |
| Tests | 67 new (writer 25, workspace trees 8, API 21, page 13); offline suite 5,915 passed, 62 deselected | `tests/report/test_codediff.py`, `tests/core/test_workspace_trees.py`, `tests/web/test_codediff_api.py`, `tests/web/test_page_code_diff.py` | done |
| Not covered | the chained remediation's pom changes are not part of this view (it compares the migration's as-is and final tree); a remediation-only run has no block; a side-by-side view; a real project of thousands of files in the browser; Windows / Edge; the company cluster | — | open |

## Status — 2026-11-18, images are pulled at every pod start

The company cluster kept showing the approval list after the image with v2.1z-ar was pushed. Cause: the company pushes
the SAME tag every time (the user: "우리 태그는 계속 동일 태그 올리고있는데!") and the manifests said
`imagePullPolicy: IfNotPresent`, so a node that already had the tag never asked the registry again; each node ran
the build it had pulled first, and the web pod and a build pod could run different code.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Manifests | `imagePullPolicy: Always` in the Deployment and both Job templates: a pod start checks the registry and pulls only a changed image | `deploy/k8s/40-deployment.yaml`, `80-build-job.template.yaml`, `81-web-job.template.yaml` | done |
| Operator text | after a push: restart the web pod (`kubectl -n agent rollout restart deploy/sbmig`), build pods pull by themselves; `IfNotPresent` needs a new tag per build; `Always` needs the registry at pod start | `deploy/README.md` §1, `agent-doc/09-deploy-and-ops.md`, `docs/INTERFACES.md` | done |
| On the cluster | the company's filled copies of 40 / 81 have to be changed there and the template ConfigMap re-created | — | open (the user's) |

## Status — 2026-11-18, a review of the whole code base and its fixes (v2.1z-av)

The user, after the latest feature rounds: "전체 코드 검사 한번 해보자! 문제될 사항들이 보이는지 비어있는곳이 있는지! 그리고
테스트 케이스도 필요 없는거 있는지도 걸러내자!" Seven read-only reviews by area plus one pass over the modules nobody
had reached; every finding reproduced before it was reported (51 in all), fixed test first by five engineers on
disjoint files, then integrated. The test-suite audit is the next section.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The run | a stop request around the final build no longer turns a passed build into "the final build did not run", and a stop in the loop does not cost a second build; the last try of a case that did not help (`no_change`, `inconclusive`) is rolled back before the final build, a partial one stays; `final_build` of an MSA run is the failing one; a fixer plugin returning nothing fails the attempt, not the run | `sbmig/loop/`, `sbmig/graph/engine.py` | done |
| Framework projects | the version guard resolves properties through the project's parent chain (a root property can no longer move the framework version); a check that could not look says so (flag `framework-unchecked`, a Handover line) | `sbmig/loop/fixloop.py`, `sbmig/loop/orchestrator.py` | done |
| A repository that did not answer | blocked reason `repository_unreachable` (no LLM try, nothing credited) for transfer failures and for the `sbmig-bundled` pre-try; a bundled jar is not declared "not in the repository" on a time-out | `sbmig/triage/`, `sbmig/loop/`, `sbmig/restructure/bundled_jars.py` | done |
| The project side | the encoding sample no longer crashes on large multi-module trees (it had left every module without its source encoding); a failed restructure resets the tree; hollow-reactor detection reads inherited build directories; the optional bump edits the property the module inherits; orphan properties referenced outside the poms stay; pom text survives a wrong encoding declaration; managed versions are judged per company parent | `sbmig/review/inputs.py`, `sbmig/restructure/`, `sbmig/analyze/`, `sbmig/build/parent_chain.py` | done |
| LLM answers and the KB | a review answer that is cut or not whole JSON is retried, never reduced to its first issue; a failed token exchange is an LLM outage (retry, cool-down), not an answer; a script cut at the token limit is neither used nor stored; unknown fields of `case.json` survive a re-save; `kb import` cannot write outside the KB | `sbmig/review/parse.py`, `sbmig/fix/`, `sbmig/errors/cases.py` | done |
| Remediation | "fixed" = gone from the rescan (report and counts agree); no change of a version property that several artifacts share unless the target exists for each; no jump to a major the scanner did not name, no date-stamped build as an upgrade, no bump of a version that already meets the hint; profile-only and twice-declared dependencies are not edited; `remediation/remediation.log` exists for every remediation | `sbmig/remediations/` | done |
| Web and Job mode | requests from another web site are refused (403); a build identity (`sbmig version` = version + digest of the running code) and pins taken when the Job is created: an image update no longer strands waiting runs and remediation resumes, and a web pod / build pod split is refused with a sentence that says what to do; unpacking limits for an uploaded ZIP; a free-space floor for uploads; redaction that keeps JSON intact; `SBMIG_MAX_UPLOAD_MB` from the ConfigMap takes effect; command-line values win over environment values; `sbmig clean` recognises today's uploads | `sbmig/web/`, `sbmig/core/workspace.py`, `sbmig/core/config.py`, `sbmig/cli.py`, `sbmig/fleet/`, `deploy/` | done |
| Reports and the code-change view | the guide's head for an aborted run that built; the http-blocked abort reason on the page; build leftovers are not "added" files; no empty block for a run that changed nothing | `sbmig/report/`, `sbmig/web/runs.py` | done |
| Korean for the new sentences | the stop reason, the framework notice, the unreachable repository, the ZIP refusals, the remediation's new residual groups and structure sentences | `sbmig/report/i18n.py`, `sbmig/remediations/i18n.py` | done |
| Checks | offline suite 6,285 passed (5,915 before; about 370 regression tests added, each seen failing first); `scripts/e2e.py` on all 48 fixtures with real Maven: 48 PASS | `out/e2e-av/summary.md` | done |
| Left to the owner | no protection against a second web pod (`replicas: 1` + `Recreate` are the control); no retention of old run folders (to be decided in a meeting); leftovers of the final build outside `target/` are in `result.zip` and the remediation's ZIP; the admin password in open mode guards the aggregated views only | `docs/INTERFACES.md` "v2.1z-av" | open |
| Not reached by the review | `analyze/jar_coords.py`, `build/deptree.py`, `build/classpath.py`, `plan/planner.py` insides, `plan/jarcheck.py`, `rewrite/compose.py`, parts of `fleet/`, the rest of `fix/context.py`, renderer-by-renderer section parity of the two migration reports | — | open |

## Status — 2026-11-18, the test suite: two minutes instead of sixteen, and what was not needed is gone

The user in the same request as the code review: "테스트 케이스도 필요 없는거 있는지도 걸러내자! 테스트가 너무 오래도는데,
이정도면 회귀문제 걱정해서 저번에 대화했던 수학적으로 검증하는 스킬을 도입하는게 좋나 아직은 테스트코드로 진행하는게
좋나?" Answer given: stay with tests. The two "proof" skills looked at earlier do not check this code base (one proves
mathematics, the other compares a migrated project with its original); the problem was the run time, and that had a
plain cause.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Where the time went | an audit with per-test timings and a trace of every child process: 26,473 git processes per run (about 70 per orchestrator-level test), and on macOS `/usr/bin/git` is the Xcode `xcrun` shim, about three times slower per call than the binary it dispatches to | `out/scratch/test-audit/` (not in git) | done |
| Parallel by default | `addopts = "-n auto --dist load -m 'not integration'"`, pytest-xdist in the `dev` extras, `auto` capped at 8 workers; `-n 0` = serial; no shared state between tests was found (no fixed port, no path under the repository, environment only through monkeypatch) | `pyproject.toml`, `conftest.py` | done |
| The real git on macOS | the first process of a run puts a private folder with one symlink to the real git first on `PATH` (workers and children inherit it; nothing else of the Xcode tool folder is shadowed); the header prints a `git:` line | `conftest.py` | done |
| No automatic maintenance in a workspace | `maintenance.auto=false`, `gc.auto=0` on every workspace git call: no maintenance process per commit, no repack in the middle of a run on the NFS volume (product change) | `sbmig/core/workspace.py` | done |
| Tests that were not needed | 66 removed, each with a named test that asserts the same (the inline-engine copies whose parity `tests/graph/test_engine.py` proves, duplicates between a round's file and a later round's, tests of events and switches that no longer exist); assertions moved into the covering test where it lacked one; 13 audit candidates kept because nothing else covers them fully or because they pin a decision or a review finding | `tests/` | done |
| Tests that could not fail | an assertion ending in `or True`, a test whose failure the code under test swallowed, a test that set the value it then checked, a test of a state that cannot occur: repaired and shown to fail; a race that only load exposed (`test_slot_review_fixes`) fixed | `tests/loop/`, `tests/graph/`, `tests/review/`, `tests/web/` | done |
| Offline means offline | two tests ran the host's real Maven (one needed the network): they use a fake runner / the preflight directly | `tests/graph/test_engine.py`, `tests/loop/test_orchestrator.py` | done |
| The walls under parallel runs | every worker has its own file cap, the controller's cap on the run's temp folder holds for the sum of all workers; two `-n 2` cases added | `tests/devtools/test_suite_guard.py` | done |
| Measured | five default runs in a row green: 105, 106, 106, 124, 123 s; serial 621 s (976 s before); 6,228 tests (6,288 before) | `out/scratch/test-speed/` (not in git) | done |
| Not done | the audit's medium-risk ideas (shorter sleeps in timing tests, restructure tests without commits, one shared orchestrator run per module); `tests/remediations`, `analyze`, `core`, `errors`, `fix`, `fleet`, `plan`, `triage` were not read for unneeded tests | — | open |

## Status — 2026-11-18, the Job template comes from the image; the "pod does not start" notice after five minutes

From the company cluster the same day. The approval list kept appearing after the new image was pushed: the cluster's
ConfigMap `sbmig-job-template` still said `imagePullPolicy: IfNotPresent`. The user had edited 81 and rebuilt the image,
expecting the web pod to read the template from the image ("우리 이미지안에 내용을 읽게 하려고 81번 Dockerfile 만들때 복사
뜨는거 아니었어?"); the Deployment pointed at the ConfigMap instead, which is re-created by a command of its own.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Template from the image | the shipped Deployment sets no `SBMIG_JOB_TEMPLATE` and mounts no template: the web pod reads `/app/deploy/k8s/81-web-job.template.yaml`; the ConfigMap route stays as a commented alternative | `deploy/k8s/40-deployment.yaml`, `deploy/k8s/81-web-job.template.yaml`, `deploy/k8s/20-configmap.yaml` | done |
| An unfilled template stops the web pod | `validate_worker_template` refuses a value that still contains `CHANGE_ME` (cluster mode only; commented lines do not count) with what to do | `sbmig/web/k8s.py`, `sbmig/web/runs.py` | done |
| Operator text | one table "what you changed → what you do": image → restart the web pod; 20 → apply, restart; 40 → apply; 81 → build, push, restart | `deploy/README.md` §1, `deploy/ENV.md`, `agent-doc/09-deploy-and-ops.md`, `docs/RUNBOOK.md` | done |
| Notice after five minutes | `SBMIG_JOB_PENDING_NOTICE_S` defaults to 300 (the user: "5분 넘게 생성 안되면 띄우도록 하자"; a healthy pod needs over three minutes on that cluster, which the Kubernetes team is looking into) | `sbmig/web/runs.py`, `deploy/ENV.md` | done |
| On the cluster | the user removed the `SBMIG_JOB_TEMPLATE` line from their 40 and the pod came up; the result of that run (no approval list) is not reported yet | — | open (the user's) |

## Status — 2026-11-18, a project's own Maven settings are ignored

A company project carried `.mvn/settings.nexus.xml` and a `.mvn/maven.config` pointing at it. The user: "우리도 프로젝트
안에 있는 주소는 무시하도록 하면 안될까? 굳이 해당 설정에 막히는건 좀 아닌거 같은데".

| Item | Deliverable | Where | Status |
|---|---|---|---|
| What Maven does | measured with Maven 3.9.10: a `-s` in the project's `.mvn/maven.config` loses to the command line; a `-gs` there is read as the global settings and merged with the server's; a `-D` there loses to the command line | `tests/build/test_project_settings_maven.py` (integration) | done |
| The server's settings always win | every Maven command passes `-gs <maven home>/conf/settings.xml` beside `-s` (`build.ignore_project_settings`, default true); the Maven home comes from the preflight's `mvn -v`, else from the installation beside the executable; not found → no `-gs`, one WARNING | `sbmig/build/mvn.py`, `sbmig/core/config.py`, `config/default.yml` | done |
| Said once | journal `project_maven_settings`, flag `project-maven-settings-ignored`, one hand-over line (Korean rule added); the project's file is delivered unchanged | `sbmig/loop/orchestrator.py`, `sbmig/report/i18n.py` | done |
| The remediation does not refuse | `remediation.project_maven_config` defaults to `ignore`; the Korean report prints one line in 처리 기록, nothing under 해야 할 일 | `sbmig/remediations/`, `agent-doc/11-remediations.md` | done |
| Open | Maven 3.9 reads one argument per line of `maven.config`: a file written for 3.8 can stop Maven before the build starts (rewriting it would change the project: the user's decision); `.mvn/extensions.xml` and `.mvn/jvm.config` still act on the build; the note is written in the full flow only | — | open |

## Status — 2026-11-18, a project's `.mvn/` folder is not read by our builds

The user, after the settings change of the section above: "프로젝트에 .mvn이 있으면 복사파일에는 없앨까? 운영 빌드 되도록만
맞춰주면 될거같은데 나중에 압축할때 동봉만 해주면 될거같고".

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Why the whole folder | measured with Maven 3.9.10 on JDK 21: `.mvn/jvm.config` with JDK 8 options (`-XX:MaxPermSize`) stops the JVM before Maven starts; a `maven.config` written for 3.8 (`-gs path` on one line) stops Maven 3.9 | `tests/build/test_project_settings_maven.py` (integration) | done |
| Nothing is deleted | `MAVEN_BASEDIR=<an empty folder of this process>` in every Maven child's environment makes Maven read no `.mvn/` of the project (`build.ignore_project_mvn`, default true); the files stay where they are, so the delivered tree and `result.zip` hold them unchanged | `sbmig/build/mvn.py` (`mvn_env`, `maven_basedir`) | done |
| What a build needs is carried over | the `-D` properties and `-P` profiles of the project's `maven.config` go onto the command line (sbmig's own options win), and `-Dmaven.multiModuleProjectDirectory=<project root>` so that poms using that property still see the project (measured: it does not make Maven read `.mvn/` again) | `sbmig/build/mvn.py` (`with_project_mvn`), the build runner, OpenRewrite, deptree, effective-pom, classpath, the remediation's Maven runner | done |
| Said once | journal `project_mvn`, flag `project-mvn-ignored`, one hand-over line with what the folder holds and what was carried over (Korean rule added); the remediation's record says the folder was not used | `sbmig/loop/orchestrator.py`, `sbmig/report/i18n.py`, `sbmig/remediations/` | done |
| Tests | offline suite 6,318 passed; the integration test with the real Maven: switch off → the JVM does not start, switch on → exit 0 with `-Drevision` carried and `.mvn/` byte-identical | — | done |
| Real pipeline | `scripts/e2e.py` on pure-java, boot2, company-spring5-web, msa, fw-direct: 5 PASS; a copy of pure-java with a JDK 8 `jvm.config`, a 3.8-style `maven.config` (`-gs` at an unreachable mirror, one `-D`) and a private settings file: BUILD ok, the `-D` carried over, the Korean line in the report, `.mvn/` in `result.zip` byte-identical | — | done |
| Not checked | `mvn.cmd` on the company's Windows PC; a temp folder path with non-ASCII characters; a `.mvn/` deeper than three levels is ignored by Maven but not described in the line | — | open |

## Status — 2026-11-19, framework projects: conversion only, named, remediated even when the build fails (v2.1z-aw)

The user: "사내 프레임워크는 llm도 돌리지 말고 그냥 jdk - jakarta까지만 하고 파일을 내려주도록 하자. llm이 이걸 해결해주기 쉽지
않을거 같아. 사내 라이브러리쪽도 개입이 많으니까! 그리고 프레임워크 프로젝트로 나오는데 <이름> 프레임워크라도 명시 할 수
있어? 사람들이 모를거 같아서. 이거 좀 강조를 해야 할 것 같아서." and, told that the remediation would need a passing build:
"취약점은 빌드가 실패해도 이어가도록! 어차피 오픈소스 라이브러리 검사라!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| No fix loop | a reduced run converts and builds once; no pre-try, stored script, plan advisor or LLM call (`framework.fix_loop: false`, default; `true` brings the loop back); the errors stay open with a note, one hand-over line, exit 2, the converted tree is delivered | `sbmig/loop/orchestrator.py`, `sbmig/loop/entries.py`, `sbmig/analyze/framework.py` | done |
| The framework has a name | `framework.name` or a name derived from the artifact (`FrameworkConfig.display_name`: `example-fw-core` → `Example`); `FrameworkUse.name` / `fix_loop`; journal `framework_project` / `plan` carry both | `sbmig/core/config.py`, `sbmig/core/models.py` | done |
| The sentence | 「<이름> 프레임워크는 JDK 21, Jakarta만 적용합니다.」: report block directly under the head table (both languages), a highlighted notice on the upload card, the run card and the result card; the Korean guide of a failed run speaks of 「변환 뒤 남은 빌드 오류」, never of fixes that were tried | `sbmig/report/`, `sbmig/web/static/index.html`, `sbmig/web/runs.py` | done |
| Remediation after a failed build | `remediation.after_failed_build: framework` (default) / `never`; the graph chains a conversion-only framework run whose final build was attempted and failed (`baseline_may_fail=True`); ordinary failed runs and the remediation-only flow are unchanged (my scoping, told to the user) | `sbmig/graph/engine.py`, `sbmig/core/config.py` | done |
| The remediation on a tree that does not build | the failing baseline build is recorded and tolerated; a candidate build may fail only with the baseline's errors (same target, same module, every error key known), else `candidate_build_failed`; changed versions end `INCOMPLETE baseline_build_failed`, never a success, and read 「조치함 · 빌드 미확인」 when nothing else is left; a run that changed nothing ends as an ordinary one; the report's head says what was and was not confirmed, 해야 할 일 = fix the build errors, then build once | `sbmig/remediations/` | done |
| The page for such a run | the failure is the headline, the remediation's outcome follows it (run card, 「지난 실행」); the block's note and the ZIP badge 빌드 미확인 only for a delivery that stopped at the baseline's errors; a leftover 「취약점 조치 중」 line after a finished remediation is gone (it affected every chained run) | `sbmig/web/` | done |
| Documents and the deck | agent-doc 00, 02, 03, 04, 05, 07, 10, 11, 13, RUNBOOK, ARCHITECTURE, README files, `deploy/README.md`; deck slides 3 and 7 and seven notes | `agent-doc/`, `docs/`, `scripts/handbook/` | done |
| Tests | offline suite passes (figure in the commit); real Maven: `fw-direct`, `fw-parent`, `fw-transitive` exit 0 with 0 LLM calls, `fw-transitive-note` exit 0 (full migration, 1 review call), new `fw-build-error` exit 2 with 0 LLM calls; the remediation against the real Maven and a fake IVAS: single module and two modules (`core` fails, `web` depends on it) end `INCOMPLETE baseline_build_failed` with the bumped version delivered and rescanned, a version that does not exist ends `FAILED candidate_build_failed` | `tests/`, `fixtures/projects/fw-build-error/` | done |
| Not checked | a real framework project on the cluster; the remediation after a failed build against the real IVAS; the notice in Edge; whether the derived name is the one people use (the company sets `framework.name` if not) | — | open (the user's) |

## Status — 2026-11-19, a library without a pom, poms in Korean encodings, a failed conversion, two page changes

All from the user's first runs of real projects on the cluster that day (v2.1z-ax, v2.1z-ay, v2.1z-az).

| Item | Deliverable | Where | Status |
|---|---|---|---|
| A library uploaded without its pom (v2.1z-ax) | the user: an in-house jar in the releases repository, `Could not transfer artifact …:pom:… from/to maven-central`, 502; "자체 라이브러리라 요청이 될지 모르겠네. 이거는 해결 방법이 없어?" Measured with Maven 3.9.10: one repository answering the pom request with an error stops the build although the jar is there; a pom in the local repository ends it. sbmig proves "jar obtainable, pom obtainable nowhere", writes a minimal pom into the pod's local repository and runs the same command again (`build.pomless_stub`, default on); every Maven call site; a later run that builds on such a file is told too | `sbmig/build/pomless.py`, `sbmig/build/repo_versions.py`, the Maven callers, `sbmig/loop/orchestrator.py` | done |
| Why not a system-scoped jar in the project pom | the user's alternative: such a jar is not packaged into the WAR, and the project would change | — | decided |
| The remediation reads every pom Maven reads (v2.1z-ay) | the user: 「읽을 수 없는 pom」 `OSError`. A pom whose Korean text is MS949 under a UTF-8 header (or none) was refused; now read with the migration's decoding rule, edited byte-true, and a pom that really cannot be read is refused with the file and the reason in words | `sbmig/remediations/apply.py`, `intake.py`, `inventory.py`, `i18n.py`, `report.py` | done |
| Such a pom is corrected before the conversion | found on the way: OpenRewrite cannot parse it, the recipes were silently not applied. Only the declaration is changed (to `EUC-KR`) when the bytes allow, else the file is saved as UTF-8; recorded like the source-encoding correction (`layout.legacy_context.correct_pom_encoding`) | `sbmig/restructure/pom_charset.py`, `sbmig/loop/orchestrator.py` | done |
| A failed conversion is said | flag `rewrite-failed`, first hand-over line, the Korean guide's lead, attention code; a conversion-only (framework) run whose conversion failed is not a success even when its build passes (exit 2, `build=ok`) | `sbmig/rewrite/failure.py`, `sbmig/loop/orchestrator.py`, `sbmig/report/` | done |
| A stop a person asked for is orange (v2.1z-az) | the user: "너무 빨가니까 문제 투성이 에이전트 같아". `user_stop` in the run status; title, rail (a pause mark), note, list line, button, toasts in the caution colour; real failures stay red | `sbmig/web/runs.py`, `sbmig/web/static/index.html` | done |
| 「지난 실행」 shows three days | `GET /api/runs?days=`; the standing line 「최근 3일 내에 등록한 프로젝트만 보입니다.」 (the user's sentence); running and waiting runs always listed; 「이전 실행 보기」 keeps older results reachable (my addition, told to the user) | `sbmig/web/app.py`, `sbmig/web/runs.py`, `sbmig/web/static/index.html` | done |
| Tests | offline suite and the real-Maven runs: figures in the commit. Real Maven with two loopback repositories (one serves the jar and 404 for the pom, one answers 502): off → the build stops, on → stub, rerun, pass; pom served somewhere → no stub; jar nowhere → no stub. Remediation on an MS949 pom with the real Maven and a fake IVAS: bumped, Korean bytes unchanged. New fixture `fw-pom-ms949` | `tests/`, `fixtures/projects/fw-pom-ms949/` | done |
| Not checked | a real Nexus answering 502; hard links on the cluster's NFS (a rename fallback exists); the live stop's button and toast in a browser; the user's own projects after the new image | — | open (the user's) |
| Open | the remediation-only flow writes a stub and says so only in the journal and the log; a library reached only transitively can be missed by "a later run is told" when no full dependency tree was captured; the fleet summary names a conversion-only failure "build failed" | — | open |

## Status — 2026-11-19, the approval step of the remediation is removed (v2.1z-ba)

The user: "우리 approval awaiting 기능 아예 빼버리자. 이렇게 안쓸거래. 환경변수도 없애고 기능도 없애고 화면에 보여주는것도
없애버리게! … 그냥 코드도 없애버려 나중에 본인들이 구축하겠지 사용자한테 승인받는건". What set it off: after a deployment
people saw 승인 대기 on the page again and asked why ("내가 스트레스 받아서 없애버릴라고"). My reading of the cause (not
confirmed on the cluster): runs of the 2026-11-18 incident still stored as `AWAITING_APPROVAL`, which the release of
that day had started to label in 「지난 실행」 and to keep listed whatever their age.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| Settings and environment | `remediation.require_approval`, `remediation.always_approve` and `SBMIG_REMEDIATION__REQUIRE_APPROVAL` are gone (model, default yml, 20, 81, ENV.md); a value left in a yml or a ConfigMap is ignored with one WARNING, never an error; the variable is left out of the pods' environment comparison | `sbmig/core/config.py`, `config/default.yml`, `deploy/k8s/`, `sbmig/web/contract.py` | done |
| The mechanism | the state, the three decisions, the two reasons, the planner's and the graph's waiting, the service's and the API's approval and proposal operations, the report's sections; a default configuration plans and applies exactly what it did (pinned plan) | `sbmig/remediations/`, `sbmig/report/` | done |
| The page | the approval list and every word for it (about 370 lines); neither page contains 승인 any more (a test holds it) | `sbmig/web/static/` | done |
| Old runs | a state an earlier version left waiting is read, shown as ended without the word 승인 (「조치 미완료」, nothing in the list, no always-listed rule), its files stay, a resume is refused with 409 | `sbmig/remediations/service.py`, `graph.py`, `sbmig/web/` | done |
| Stays | `AWAITING_CONFIRMATION` (an IVAS upload whose outcome is unknown) and the resume of a stopped remediation: the user, asked the same evening: "그건 일단 내버려 두자" | — | decided |
| Documents and the deck | every description of approval removed; one place per audience says the step is gone and how to clean a cluster | `agent-doc/`, `docs/`, `deploy/README.md`, `scripts/handbook/` | done |
| The cluster | delete the key from ConfigMap `sbmig-env` if present, delete the forward line from the cluster's own 81, rebuild the image; the order does not matter | — | open (the user's) |

## Status — 2026-11-19, the admin page hands out the uploaded original (v2.1z-bb)

The user: "우리 어드민에 원본 소스 받을 수 있는 기능 하나만 만들어주라. 이거 scp를 2번을 땡겨와야해서 너무 불편해서 웹 기능으로
만들게!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The download | `GET /admin/api/runs/{id}/source` (+ `/source/check`): the uploaded bytes as a stream, named `<run id>-<upload name>` (Korean names intact), admin session required, confined to the output root, no symbolic link | `sbmig/web/admin.py` | done |
| The page | a column 「원본」 with 「원본 받기」 and the size; faded with 「원본 파일이 서버에 없습니다」 when the file is gone; nothing on the public page | `sbmig/web/static/admin.html` | done |
| Where uploads live | Job modes: the run folder's `source.zip`; in-process: the upload folder; the remediation-only flow's own copy only for that flow | `admin.find_source` | done |
| Checked | 14 tests; in a browser the downloaded files' sha256 equals the uploads' (a 3 MB Job upload, an upload with a Korean name) | `tests/web/test_admin_source.py` | done |
| Not checked | a real upload of several hundred MiB through the cluster's ingress (its timeout and body limits apply to downloads too) | — | open (the user's) |

## Status — 2026-11-19, IVAS file components: the right address, and report only (v2.1z-bc)

The user's cluster: one project ended `INCOMPLETE scan_incomplete — baseline scan incomplete: http_error, page_missing`
after a 200 on the upload. The user found the 404 and its cause ("원래는 filecomponents 여야 하는데 filecomponent로 요청을
보냈네!"), confirmed on the real IVAS that the id of that path is the ossId and accepts `|`, and set the policy: "우리
백엔드로직만 라이브러리 검사하고 프론트는 검사 안하려고 하는데 … 이건 적용은 하지말고 리포트만 보내도록 할까?"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The address | matched files are read from `…/result/filecomponents/{id}/matchedfiles`; the other addresses compared with the call table: no other slip | `sbmig/remediations/ivas.py` | done |
| Why it went unnoticed | the fake IVAS of the integration tests answered every unknown address with an empty page; it answers 404 now | `tests/remediations/test_integration.py` | done |
| Report only (`remediation.file_components: report`, default) | file findings reach neither the inventory nor the plan nor the verdict; counts of their own; a failure in the file part of a scan is a note, the library part is planned and applied; `count` keeps the earlier rule and never trusts a scan whose file part was skipped | `sbmig/remediations/graph.py`, `ivas.py`, `inventory.py` | done |
| The report | a line under the summary and a section 「프로젝트 파일에서 찾은 취약점」 with the files; one line in the migration report | `sbmig/remediations/report.py`, `i18n.py`, `sbmig/report/markdown.py` | done |
| Deployment | `SBMIG_REMEDIATION__FILE_COMPONENTS` forwarded by 81, listed in 20 and ENV.md | `deploy/` | done |
| Checked | offline suite and 11 integration tests with the real Maven and the fake IVAS (figures in the commit): library + `.js` finding → `SUCCEEDED` under `report`, `INCOMPLETE` under `count`; matched files 404 and file lists 500 → still `SUCCEEDED` with the note | `tests/remediations/test_file_components*.py` | done |
| Caveat told to the user | a jar that lies in the project outside the pom is a file component too and leaves the verdict under `report`; the section shows the paths | — | told |
| Not checked | a whole scan of sbmig through the corrected call on the cluster; the stuck project needs a new run (an `INCOMPLETE` baseline scan cannot be resumed) | — | open (the user's) |
| An interruption | the implementer was stopped by accident half-way (the task still carried the name of an earlier job, "Remove approval …"); a stopped agent cannot be resumed, so a new one finished from the partial work in the tree | — | note |

## Status — 2026-11-19, the page: 「완료」, and the code changes in a large dialog (v2.1z-bd)

The user: "우리 웹페이지에 성공, 취약점 일부만 조치 이부분 그냥 완료 로만 나오게 해줘!"; "소스 변경사항 보여주는거 크게 모달로
변경 가능해? 안에 내용 그대로 때서 모달로 만들 수 있지?"; "선택한 파일만 볼 수 있도록 해줘! 정적자원들은 위치 이동 표시만
하고 컨텐츠 내용은 없어도 돼!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| 「완료」 | a successful run reads 「완료」 in 「지난 실행」; the remediation's short form follows only for the states a person must act on (실패, 확인 필요, 멈춤, 중단, 대기, 진행 중), in its own colour (my rule, told to the user) | `sbmig/web/static/index.html` | done |
| The dialog | the result card keeps one row with the summary and 「코드 변경 보기」; the view opens in a native `<dialog>` (96 % × 92 % of the screen, full screen on a phone), closed by the button, Esc or the backdrop; nothing is fetched before it opens | same | done |
| Room for the code | the summary and the folder-structure box take at most 28 % of the dialog (the box folds); the file list and the pane take the rest: 56–71 % of the dialog's height at 1440 × 900 | same | done |
| One file at a time | the pane shows the file selected in the list; previous / next controls; the list by keyboard; drawing 30 files at a time is gone | same | done |
| Static files | by extension: never fetched, shown with their move and status only; JSP, Java, XML, properties, YAML, SQL and unknown extensions show their content | same | done |
| Checked | in a browser with records of 50 files (3 and 15 move groups) at 1440, 1024 and 400 px; no patch request for a static file; console clean; the offline suite (figure in the commit) | `tests/web/test_page_code_dialog.py`, `test_page_code_diff.py` | done |
| Not checked | Edge; a real project of thousands of files in the dialog | — | open (the user's) |

## Status — 2026-11-19, an upload needs a project name, and the run folder starts with it (v2.1z-be)

The user: "우리 접수 받을때 파일 올리고 프로젝트명 안적으면 거절당하게 해줄 수 있어!? 프론트, 백엔드 둘 다 체크 하도록!" and
"영어랑 _, - 로만 허용 가능하도록 해줄래! 리눅스에서 쉘 접속 했을때 치기 귀찮아서! 공백도 비허용!"

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The rule | `^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$`: letters, digits (my addition, told), `_`, `-`; no space; never trimmed. One pattern on the server (`runs.PROJECT_RE`), the same on the page | `sbmig/web/runs.py`, `static/index.html` | done |
| The server refuses before the body | 400 with `message_ko` for a missing name and for a mismatch; tests call the app with a counting `receive()`: 0 reads | `sbmig/web/app.py` | done |
| The page | the required field 「프로젝트명」 with its hint; the start button needs a file and a valid name; a wrong character is said at once; the run card and 「지난 실행」 show the name first | `static/index.html` | done |
| The run folder | `<project>-YYYYmmdd-HHMMSS-<hex6>` (my reading of "쉘에서 치기 귀찮아서": the name is what a person types to find the folder; told); older folders keep working | `sbmig/web/runs.py` | done |
| Job names | a long id is cut at the front, the timestamp and the random tail always stay (the earlier rule could cut the tail: two runs of one project in one second could collide) | `sbmig/web/k8s.py`, `runs.py` | done |
| `sbmig clean` | ties an upload to its run when the id has upper-case letters or `_` (it could have planned the upload of a running run for deletion) | `sbmig/fleet/cleanup.py` | done |
| The admin page | grouped by the entered name | `sbmig/web/admin.py` | done |
| Checked | 94 new tests; a real start of `order-admin` in a browser: the id and the folder carry the name | `tests/web/test_project_name.py` | done |
| Not checked | a Job started under a 30-character upper-case name on the cluster | — | open (the user's) |

## Status — 2026-11-19, a single-module Spring Boot app is split into the company's modules (v2.1z-bf)

The user: "parent가 spring boot로 시작하는 프로젝트가 xxx-boot만 생성되었는데 여기 정적자원이랑 다 있거든!? 이거 xxx-comp-jar,
xxx-front-jar 로도 변환을 해줘야해! 이 케이스를 생각을 못했네!" Since round 9c such a project was wrapped into
`{name}-boot` as a whole.

| Item | Deliverable | Where | Status |
|---|---|---|---|
| The switch | `layout.boot_single_module: split` (default) / `wrap`; several-module Boot projects and MSA keep their directories | `sbmig/core/config.py`, `config/default.yml` | done |
| The split | no second splitter: the planner sends such a project to the existing layout split (`restructure.apply`, mode `layout`, `boot_single`), which gets the Boot-only placements | `sbmig/plan/planner.py`, `sbmig/restructure/restructurer.py` | done |
| Placement | boot: the application class, what names it, the Boot configuration; source module: every other source and resource; front module: static folders, a web root, JSPs at their web path, templates; tests follow their class, context and slice tests go to boot | same | done |
| What a person must check | three hand-over lines (classes moved with the application class; packages outside the scan; file-path reads of web files) with Korean rules | `sbmig/restructure/restructurer.py`, `sbmig/report/i18n.py` | done |
| The review's inputs | describe this case as the boot / back / front layout | `sbmig/review/inputs.py` | done |
| Fixtures | `company-boot-single` pins the split (controller, template, static file, tests), `company-boot-wrap` pins `wrap`, `boot-asis-parent-unpublished` is split too | `fixtures/projects/` | done |
| Checked | the offline suite and every fixture with the real Maven (figures in the commit); the split fixture's jars unpacked and the app started with `java -jar`: the template, the CSS and the API answer | — | done |
| Module names | the user wrote `xxx-comp-jar` / `xxx-front-jar` and corrected it the same day: `xxx-boot` / `xxx-back` / `xxx-front`, what the layout settings give | — | settled |
| Limits | see INTERFACES "v2.1z-bf" (the plan's front note, test reads of moved files, scans through `@Import`, `front_module: null` with a WAR) | — | open |

## Status — 2026-11-20, a rendered report view, report names a person can tell apart, model names by role (v2.1z-bg)

The user: "우리 보고서 미리보기 md파일 viewer로 만들 수 있어? 추가로 report 이름이 동일한데, migration-report와
remediation-report로 만들 수 있도록 해줄래! 아 그리고 관리자에 모델 이름이 unknown이랑 ? 2개로 나오는데 … 우리 role에 들어가는
escalation_fixer나 build_error_fixer 이름이 나왔으면 좋겠는데..."

| Item | What | Verification | State |
|---|---|---|---|
| Report view | the migration report drawn as Markdown in the result card, 「보기」 / 「원문」, 「크게 보기」 dialog; 「보고서 보기」 opens the remediation report in the same dialog; own renderer `MdView` (DOM nodes only, links only `http(s)` and files of the run, images never loaded, HTML as text except `<details>` / `<summary>`) | `tests/web/test_page_report_view.py` (real reports, hostile text, a long report, toggle and dialog) | done |
| Download names | `migration-report.md` / `.json`, `remediation-report.md` / `.json` (`runs.DOWNLOAD_NAMES`, the remediation files route's `?download=1`); run-directory files, addresses and ZIPs unchanged; `run-job.sh`'s result branch keeps `report.md` | `tests/web/test_error_list_files.py`, `tests/remediations/test_api.py` | done |
| Names in the Korean report | sentences that point the reader at a report name `remediation-report.md` / `migration-report.json`; the English layout keeps run-directory paths | `tests/report/test_guide_ko.py`, `test_remediation_section_ko.py`; the English golden files unchanged | done |
| Model names | a usage row is named by the asked name (the agent gateway's role); the gateway's real names kept as `served` (report JSON, journal `llm_script` / `llm_verify_guide`, the `llm call` log line, `sbmig llm-probe`); price by asked name, then served names | `tests/fix/test_model_names.py`, `tests/loop/test_model_names_report.py` | done |
| Admin page | asked name with served beside it, a 모델 column per run, old nameless rows under one label 「이름 없음」; old runs' names recovered from `params.role=` log lines only on an exact match | `tests/web/test_admin_model_names.py` | done |
| Project-name hint | 「6자리 체인코드가 포함된 레포지토리 이름으로 작성해 주세요.」 + the character rule, placeholder 「레포지토리 이름」 | `tests/web/test_project_name.py` | done |
| Checked | the offline suite (figure in the commit) | — | done |
| Not checked | Edge; a dark theme (the page has no dark token set); the agent gateway's `model: unknown` answer (inferred, no recorded answer in the repo); tokens of one row are not split per served model | — | open |

## Status — 2026-11-20, build output in an uploaded ZIP is left out of the remediation's file list (v2.1z-bh)

The user, from the company cluster: a remediation-only run ended 「내부 오류」 right after the IVAS upload
(`RemediationError: file listed in the manifest is missing: …/target/classes/….class`).

| Item | What | Verification | State |
|---|---|---|---|
| Cause | the upload carried `<module>/target/`; the files were manifest paths; the candidate's `mvn clean package` failed, so freezing the failed candidate met a listed file that no longer existed | the user's screenshot of the traceback | found |
| Intake | `intake.build_output_paths`: `target/` beside a `pom.xml` is validated, then left out of `accepted/` and `original/manifest.json`; `original/source.zip` keeps it; `IntakeResult.build_output_files`, one note, one INFO line | `tests/remediations/test_intake_build_output.py` | done |
| Report | one line under 「변경하지 않은 것」; the two sentences that said the result ZIP's file list equals the original's are worded for the case | same file, `tests/remediations/test_i18n.py` | done |
| Outcome of the failing case | a failing candidate build ends `FAILED candidate_build_failed` with the frozen delivery | graph-level test with an emulator that removes `target/` on `clean`; a companion test shows the old error with the rule off | done |
| Not checked | the company cluster (new image needed); why that run's candidate build failed (a separate matter, visible once the run ends with its report); a custom `<directory>` build folder is not recognised | — | open |

## Status — 2026-11-20, build errors fixed for framework projects and for a remediation's failed candidate (v2.1z-bi)

The user: "remediation 쪽도 버전 올라갔을때 에러가 나올 수 있으니 llm 수정기능 붙여달라고 한다. 사내 프레임워크 … 도 llm로
빌드 수정 기능 넣어달래. javax -> jakarta만 변경하는거니까 그정도는 해주라는데..".

| Item | What | Verification | State |
|---|---|---|---|
| Framework default | `framework.fix_loop` defaulted to `true` (code and `config/default.yml`): pre-tries without the company parent, stored scripts, LLM, `_framework_guard`; `false` = the conversion-only run. **Turned back the same day (`039f1b9`): the default is `false` again**, the switch and the chain rule stay (status section below) | `tests/analyze/test_framework.py`, `tests/web/test_framework_scope.py` | superseded |
| Chain after a failed framework build | `Orchestrator.remediation_after_failed_build` chains whether or not the loop ran; `remediation_always` no longer reads `fix_loop`; the migration report's remediation section has its own sentence when the loop ran | `tests/loop/test_framework_no_loop.py`, `tests/web/test_framework_scope.py` | done |
| Loop hook | `BuildFixLoop(edit_guard=…)`: asked before every commit of an attempt (pre-tries included) with the touched paths; a rejection is handled like a framework-guard rejection (journal `edit_guard`); `None` = unchanged | `tests/loop/test_fixloop_edit_guard.py` | done |
| Remediation fix loop | `sbmig/remediations/fixer.py` (`run_fix_loop`, `RemediationFixer`), `graph._fix_wanted` / `_fix_candidate`: source only, no `pom.xml`, no file created or deleted, `NullKnowledgeBase`; `remediation.fix_loop` (true), `remediation.fix_loop_max_iterations` (10, 1–50) | `tests/remediations/test_fixer.py`, `tests/remediations/test_graph_fix_loop.py` | done |
| Outcome | fixed → `rounds/<r>/build-fixed.log`, verify, commit, `rounds/<r>/fix.patch` (served as `fix.patch`), `rounds[].fix`, status `fix`, journal `remediation_fix_loop`; not fixed → sources reset, `candidate_build_failed` + ` \| automatic build fix: <reason>` | same files | done |
| Report and page | section 「빌드 오류 수정」, first to-do item, 「변경하지 않은 것」 wording, the not-fixed / not-started lines, reasons in Korean; chip 「소스 <n>개 수정」, LLM count in the run header, `fix.patch` link | `tests/remediations/test_report_fix_loop.py`, `tests/remediations/test_i18n.py`, `tests/web/test_page_remediation_fix.py` | done |
| Fixtures | the `fw-*` fixtures state their `framework.fix_loop` explicitly | `tests/fixtures/test_fixture_projects.py` | done |
| Not checked | the company cluster and the real IVAS; real-Maven e2e of a remediation whose candidate the loop fixes; pom edits by the loop (an upgrade needing an exclusion or a new artifact stays a failed candidate); no loop after a failed baseline build or on the second build after an ineffective override is dropped; `SBMIG-RESULT llm=` leaves out the remediation's calls (the page adds them) | — | open |

## Status — 2026-11-20, review rounds of the remediation's fix loop; page previews; framework loop off again; the scan source; swap records, no re-upload, unknown usage, stop before an LLM request; verification and two more real-Maven passes (v2.1z-bj, v2.1z-bk, v2.1z-bl, v2.1z-bm, v2.1z-bn, v2.1z-bo, v2.1z-bp, v2.1z-bq)

The user, the same afternoon: "아 이거 오늘 금요일이라 안급해~ 고칠 수 있는거 꼼꼼히 고치고 가자! 사내 프레임워크는 … 일단 다시
disable 시키고 진행하자"; then "관리자에서 로그 파일들 받을 수 있으니까 그 밖의 파일 영역은 빼달래! 고급설정 영역이 빼줘!" and
"보고서는 migration, remediation 각각 따로 보여질 수 있도록!". Three read-only reviews of v2.1z-bh / v2.1z-bi and their fixes; every finding
reproduced first and turned into a regression test; then a fourth review and the first real-Maven smoke run (v2.1z-bn),
a fifth review and the second real-Maven smoke run (v2.1z-bo), a verification review of `4600c02`, a third and a fourth
real-Maven pass (the third also with a real migration run) and the fix of what they found (v2.1z-bp, `d85f7a4`).
Contracts and as-coded notes: `docs/INTERFACES.md` "v2.1z-bj", "v2.1z-bk", "v2.1z-bl", "v2.1z-bm", "v2.1z-bn", "v2.1z-bo", "v2.1z-bp".

| Item | What | Verification | State |
|---|---|---|---|
| Framework loop off again (`039f1b9`) | `framework.fix_loop` defaults to `false` (code and `config/default.yml`); with `true` the loop runs; with `remediation.after_failed_build: framework` a framework run whose final build fails chains the remediation whether or not the loop ran | `tests/analyze/test_framework.py`, `tests/web/test_framework_scope.py` | done |
| Work copy and overlay (v2.1z-bj) | the fix loop runs in `rounds/<r>/fixloop/tree/` (no `.git` path), changed files by byte comparison, fixed bytes in `fixloop/fixed/`, state saved, then `graph._overlay_fix` is the only change to the candidate; editable paths (no pom, also under another name; no dot path, script, archive); time `min(fix_loop_max_minutes (30), time left − reserve)`; `retry_passed`; not-started wording; a fix discarded for a remade candidate; `build_output_paths` keeps `target` under `src/`; `fix.patch` only for committed fixed rounds | `tests/remediations/test_fix_loop_review.py`, `test_fixer.py`, `test_graph_fix_loop.py`, `test_fix_patch_route.py`, `test_intake_build_output.py`, `test_report_fix_loop.py`, `test_i18n.py` | done |
| Stop, kill windows, build-side changes, one patch (v2.1z-bk) | a stop before / during the confirming build keeps the fix; state saved before files are deleted; a raising fixer still delivers the failed candidate; `rounds/<r>/applied/` and `intake.sync_tree` after every candidate build (`build_changed`, journal `build_side_changes`); a killed loop kept as `fixloop-interrupted-<n>/` with its usage recovered; cp949 sources shown as UTF-8; one combined `remediation/fix.patch` | `tests/remediations/test_fix_loop_review2.py`, `test_fix_patch_route.py`, `test_report_fix_loop.py`, `test_intake_build_output.py` | done |
| LLM usage totals (`4054960`) | `FixResult.usage` per model, priced; `fixer.fix_usage` is the one reader; `SBMIG-RESULT llm=`, `GET /api/runs/{id}` (`llm_calls`, `remediation_llm_calls`), the admin page (「조치 N회 포함」, per model, priced) and the upload page count each call once | `tests/remediations/test_fix_usage.py`, `tests/web/test_admin_model_names.py`, `tests/web/test_page_remediation_fix.py` | done |
| Two report previews (`796eec2`) | 「마이그레이션 보고서 미리보기」 at the end of the result card; 「조치 결과 보고서 미리보기」 inside the remediation card showing exactly `remediation/report.md`; a remediation-only run shows its short run report (「보고서 미리보기」) only until a remediation report exists; the 「보고서 보기」 button is gone | `tests/web/test_page_remediation_preview.py`, `tests/web/test_page_report_view.py` | done |
| Trimmed page areas (`63cece1`) | 고급 설정 hidden (defaults sent), no list of other run files (the admin page serves them) | `tests/web/test_page_trimmed_areas.py` | done |
| Complete-apply proof (v2.1z-bm, `51d3809`) | `apply` writes `rounds/<r>/applied/`, then `rounds/<r>/expected.json` (`{schema, round, applied, applied_files, files: {path: [size, sha256]}}`; schema 2 with the exec bit since v2.1z-bn), then `candidate.ready.json` with that file's sha256; `_Holder.vouch_problem` → a candidate without the proof (a kill during apply, a run folder of the previous version) goes back through `apply`, journal `candidate_rebuilt` with `why`; the previous version's run folders no longer lose their pom edits as "build changes" | `tests/remediations/test_fix_loop_review3.py` (old-version folder, every kill point, write order) | done |
| Sync by table, before commit and freeze; a failed sync fails the round (v2.1z-bm) | `intake.sync_tree_table` against `expected.json` + the fix's files (each candidate file read at most once, `accepted/` not re-read; the table is built from `original/manifest.json` or the last committed round's table when the plan's tree hash matches, else one read); runs after every build and right before commit and before a failed candidate is frozen; every path tried, read-only folders / files made writable once; `intake.SyncFailed` → journal `candidate_sync_failed`, `candidate_not_restored`, FAILED `internal_error` with nothing of the round delivered, an earlier verified delivery kept; `_fix_tree_hash` reads no tree | `tests/remediations/test_fix_loop_review3.py`, `test_intake_build_output.py` | done |
| Stop, killed loops, page, wording (v2.1z-bm) | a loop result `stop requested` moves to `fix_discarded` on re-entry and the loop may run again (`time budget used up` stays final); a leftover `fixloop/` is settled at the start of the build node, `_run_build` and the report node, saved before the rename, counted once; the start request ignores the hidden 고급 설정 controls (`advShown()`, `startQuery`, `autocomplete="off"`); `i18n.fix_failed_ko` ends 「시도한 소스 수정은 결과에 넣지 않았습니다.」 | `tests/remediations/test_fix_loop_review3.py`, `tests/web/test_page_trimmed_areas.py`, `test_i18n.py`, `test_report_fix_loop.py` | done |
| Project name in the remediation titles (`f107cd1`) | a remediation-only run's two report titles carry the entered project name (`contract.entered_project` / `display_project`, from `control/request.json` or the `web_run` journal line, display only); a chained remediation uses the entered name or keeps `source.zip`; a direct CLI run unchanged | `tests/web/test_display_project.py`, `tests/remediations/test_chain_project_name.py`, `tests/loop/test_remediation_flow_project_name.py` | done |
| Fourth review (v2.1z-bn, `9dc249f`) | one delivery swap for every result ZIP (`_Holder.install_delivery`: `delivery/.result.next.zip` → marker `.result.next.json` → verified ZIP COPIED → `os.replace` → `delivery.json` → state; `recover_delivery` first in `run_remediation`), a freeze that fails leaves the previous delivery and ends FAILED `internal_error` (`the result ZIP of round <r> could not be written …`, journal `delivery_not_written`); the build outcome recorded before the sync can raise; 「결과 파일 — 없음」 sentence per reason (`report._no_delivery_why`); a failing fix-loop folder setup is a not-started loop; usage after an exception read from the loop's journal (`tokens_unknown`, no cost); `expected.json` schema 2 with the exec bit, the sync restores the bit and makes an unreadable file readable once; ` \| automatic build fix made no attempt: <reason>` / 「빌드 오류 자동 수정을 하지 못했습니다(<사유>).」; a final state whose report was not written gets it on the next entry (`write_pending_report`: `run_remediation`, `chain.run_after_migration` / `run_only` / `resume`, in-process service start); `file_table` records non-regular paths | `tests/remediations/test_fix_loop_review4.py`, `test_i18n.py` (7,137 passed) | done |
| Scan source (v2.1z-bn, `9dc249f`) | found by the real-Maven smoke run: with left-out build output the baseline scan uploaded `original/source.zip` (still holding `target/`) and a no-change run delivered it. Now `original/scan-source.zip` (a deterministic ZIP of `accepted/`, `state.scan_source_sha256`, journal `scan_source_made`) is scanned at baseline and is the no-change delivery; a scan source that cannot be made again → FAILED `zip_hash_mismatch`; without left-out output nothing changes; a run the deployed version started keeps the original; the files route still serves the upload as `source.zip`; the report's left-out note and round-0 sentence follow what was scanned and delivered | `tests/remediations/test_scan_source.py` | done |
| Real Maven (first smoke run) | Maven 3.9.10 + JDK 21 on a developer Mac; a one-module project on `org.yaml:snakeyaml` 1.33 → 2.0 with a stale `target/` and an MS949 source; seven runs (fixed; two give-up variants; pom edit rejected; SIGKILL before the confirming build + resume; stop + resume; loop off) all ended as expected with every scripted file check passing. Fake scanner and fake LLM; builds of about 2 s, so nothing about speed | scripted checks under `out/scratch/` | done |
| Fifth review (v2.1z-bo, `4600c02`) | the swap is rolled FORWARD once `result.zip` was replaced: `install_delivery(after=…)` sets `st.delivery` and what the node sets, then `delivery.json`, then the save; records that cannot be written keep the marker, `DeliveryRecordNotWritten` → FAILED `internal_error` (`delivery/result.zip is now the result ZIP of round <r>, but … could not be written (…); the record is completed the next time this run is opened`, journal `delivery_record_not_written`); `recover_delivery` at every entry (`run_remediation`, `graph.repair_final` from `chain.resume` / `_repair_report` / `run_after_migration` / `run_only`, the service; journal `delivery_recovered`; a leftover `.result-r*-verified.zip.copy-tmp` removed); no re-upload after a kill (`_stored_baseline`, journal `baseline_scan_reused`), only a loop not started by a stop or the time budget is forgotten, no second loop after an unusable stored fix (`_run_build(rerun_ok=…)`); the web service repairs a final state with a pending report or swap whose owner is gone in every mode (`RemediationManager.repair_final`, at the top of `status` / `result` / `file_path`, at startup for standalone folders, `RunClaim(purpose="service")`, once, retried at most every 60 s, journal `remediation_final_repair`); unknown usage (`FixResult.usage_unknown`, `fix_usage()` `calls_unknown` / `tokens_unknown` / `usage_unknown`, `chain.summary.fix_usage_unknown`, `remediation_llm_usage_unknown`, `fix.llm_usage_unknown`, admin `usage_partly_unknown` + `cost: null`, report 「LLM 사용량 일부는 알 수 없음」; never priced); `_baseline_outcome` writes the report again; " \| report not written: …" once; `ScanSourceChanged` keeps `zip_hash_mismatch`; the unconfirmed kept ZIP worded 「재검사한 결과 ZIP(빌드로는 확인하지 못함)」 | `tests/remediations/test_fix_loop_review5.py`, `test_fix_usage.py`, `test_i18n.py` (7,176 passed) | done |
| Second smoke run's findings (v2.1z-bo, `4600c02`) | the fix loop (migration and remediation) checks the stop request and the time budget before every LLM request (`BuildFixLoop._halt_before_ask`, journal `llm_ask_skipped`), ending the way a check between builds does; a stop during the build fix halts STOPPED `stop requested during the round <r> build fix` (「라운드 <r>의 빌드 오류 자동 수정 중에 중지 요청을 받았습니다.」); the fix section's call sentence gives the round total (「…LLM을 M회 호출했습니다(버린 시도까지 모두 T회).」); the round-0 step says 「검사를 마친 `delivery/result.zip`…」; an interrupted loop's `tree/` is removed once its usage is recorded | `tests/loop/test_fixloop_ask_halt.py`, `tests/remediations/test_fix_loop_review5.py` | done |
| Real Maven (second smoke run, on `9dc249f`) | same Mac, Maven 3.9.10 + JDK 21, fake scanner and scripted LLM: the seven scenarios of the first run again (delivered ZIPs identical per file to the first run's) plus scan source with and without `target/` in the upload, no actionable finding, exec bit kept (also on the file the loop rewrites), a build plugin that edits a tracked file (not delivered, named in `build_changed`), SIGKILL inside the loop + resume (interrupted folder, usage 1 + 1), stop inside the loop + resume, a two-module reactor inside one top folder; all ended as expected with every scripted check passing | scripted checks under `out/scratch/` | done |
| Review verdicts | the fourth and fifth reviewers signed "a delivered ZIP never contains or lacks a change the report does not account for"; the fifth found no defect blocking a non-production deploy | — | done |
| Documents | every document brought in line (this status section, INTERFACES as-coded notes, RUNBOOK, ARCHITECTURE, PROGRESS, READMEs, deploy, agent-doc, CLAUDE.md) | `tests/docs`, `tests/deploy` | done |
| Verification of `4600c02` | one defect, "not yet safe to push": `_run_build` skipped the fix loop whenever the round's last discarded fix had the reason `discarded: the stored fix is not usable`, so a resume whose stored fix was found unusable, cut short by a stop or a kill before the loop restarted, ended FAILED `candidate_build_failed` on its next entry; confirmed that the change in `sbmig/loop/fixloop.py` leaves the migration loop unchanged when no stop is requested and the time budget has not passed | reproduced in `tests/remediations/test_fix_loop_review6.py` | done |
| Fix (v2.1z-bp, `d85f7a4`) | `FixResult.ended_round` (left out when false) set by `graph._discard_fix(…, ends_round=True)` only when a fresh loop's fix is unusable, read by `_run_build` instead of the reason; calls the budget counted beyond the attempts get their own unpriced row (`tokens_unknown`), known rows keep their price, `fix_usage_rows` keys by (name, unknown); admin run `cost` = the sum of the priced rows with `usage_partly_unknown` and 「일부 알 수 없음」 beside calls and cost, `null` / 「알 수 없음」 only when nothing is priced; strict journal reading (`admin.journal_usage_checked(strict=True)`, `fixer.journal_usage` → `(rows, currency, known)`: a damaged line is unknown with what was read kept; a missing journal is a known zero since v2.1z-bq); `MIN_LOOP_SECONDS` checked only in `graph._run_fix` (`fix_loop_max_minutes: 1` starts the loop); `chain._report_again` for `_budget_outcome` and `_baseline_outcome`; 「(버린 시도까지 알려진 것만 T회)」; `graph._without_report_note`; `REPAIR_SEEN_MAX` = 2000 | `tests/remediations/test_fix_loop_review6.py`, `test_fix_loop_review5.py`, `test_fixer.py` (7,203 passed) | done |
| Real Maven (third pass, on `4600c02`) | the 15 scenarios of the second pass again, delivered ZIPs identical per file, with the differences `4600c02` explains (no LLM ask after a stop: 0 instead of 8; the STOPPED wording; the round total; the interrupted loop's `tree/` removed; 「검사를 마친」); new: a kill right after the no-change delivery was installed → exactly one upload over run and resume (`baseline_scan_reused`); time budget 2 minutes with slow answers → no ask after the deadline, FAILED `candidate_build_failed`, sources identical to the upload; a killed loop whose journal was made unreadable → usage unknown. It found the strict-reading and one-minimum defects fixed above. A real migration run (fixture `pure-java`, its replay LLM, real Maven): a normal run `exit=0 build=ok` with the verify fix loop running one round; a stop between two LLM asks `exit=3` (aborted) with `llm_ask_skipped`, no further ask, a clean workspace, nothing half-applied | scripted checks under `out/scratch/` | done |
| Real Maven (fourth pass, on `d85f7a4`, by the coordinator) | 17 scenarios (happy, wrong, pom, noloop, kill + resume, stop + resume, notarget, nofinding, exec2, buildwrite, killloop, stoploop, reactor, nofinding-kill, killloop with a garbage journal, killloop with a deleted journal, time budget at the 1-minute minimum) ended in the expected state and passed their file checklists, except two false alarms of the checklist itself (a build duration differing by seconds between two reports; the loop's own journal labels a time-out as a stop request while the state and the report say time budget); `fix_loop_max_minutes: 1` starts the loop (2 LLM calls, `time budget used up`); a garbage or deleted journal of a killed loop → `interrupted: the run ended during the loop (usage unknown)` and 「알려진 것만」 in the report | scripted checks under `out/scratch/` | done |
| Verification of `d85f7a4` and the last fix (v2.1z-bq, `020b11f`) | the reviewer: "safe to push for a non-production deploy: yes", no change for a healthy run; two small regressions fixed: a loop killed during its setup (a `loop.json` without a journal) is a known zero again, and the 「일부 알 수 없음」 note reaches the admin page's project / overall / model sums | `tests/remediations/test_fix_loop_review6.py`, `tests/web/test_admin_partly_unknown.py`; full suite 7,211 passed; the diff read by the coordinator, no further review round | done |
| Not checked | not exercised by any pass: the company cluster; a real LLM; the real IVAS; large projects and real build times; Windows (also the permission and read retry of the sync); the web service's repair and Job mode; the admin page; the migration loop's time-budget check before an LLM request; also Edge and a project whose build needs its own `.git` (git-commit-id: the work copy has none, so its loop ends not fixed). Still open: a stored fix whose files vanish between the save and a kill runs the loop once more; after a kill between the confirm-failed save and the file removal `fixed/` and `fix.patch` stay on disk unreferenced; the upload page's chip does not show the unknown-usage marker; a killed pod's fresh claim keeps the web repair waiting up to 600 s; a call in flight when a pod is killed is never counted; the admin page's project and overall cost carry no "partly unknown" note; a loop killed between `loop.json` and its first journal line is reported as unknown although it made no call; known rows without a configured price still make a run's cost null; the loop's own journal labels a time-out as a stop request. Open with the business side (the user, Monday 2026-11-23): keep accepting an upload that carries `target/` with the folder left out, or refuse it | — | open |

## Status — 2026-11-22, the safe sweep: three small defects, unused code, repeated reads, what enters the image (v2.1z-br)

A read-only review of the whole tool listed a few small defects older than the remediation work and changes that alter
nothing a run, a report or a reader depends on. Contract (the list a reviewer checks the round against):
`docs/INTERFACES.md` "v2.1z-br". Commits `9541e2b`, `b9f4d5c`, `51f9a3c`, `d1b423e`, `05402e9`.

| Item | What | Verification | State |
|---|---|---|---|
| Run list | `GET /api/runs` never answers 500 because of one run: a `report.json` that is JSON but not an object is read like an unreadable one (also in the single-run status), a report field that should be a list and is not counts as absent, a journal line that is not an object is skipped; an unexpected error while one row is built logs one WARNING `run list: the row of run <id> could not be built (<type>); listed as unreadable` and lists the run like one whose files cannot be read (a folder the server cannot enter: state `unknown`; an entry that cannot be examined is left out); the single-run status has no net | `tests/web/test_v21zbr_server_status.py` | done |
| Framework guard | `BuildFixLoop._framework_guard` fails closed: a pom that cannot be read (`OSError`) or does not parse is left to the build; any other exception rejects the attempt like a guard finding (the sentence says the check could not be completed, the tree is restored, one WARNING with the exception type) | `tests/loop/test_v21zbr_framework_guard.py` | done |
| No second build after `TypeError` | the three call sites that retried without a keyword decide once from the callee's signature (`sbmig/loop/callee.py::takes_keyword`); every shipped builder and runner gets the call it got before; an injected component that hides a runner without the keyword behind `**kwargs` now sees the `TypeError` | `tests/loop/test_v21zbr_builder_keywords.py` | done |
| Page | `MdView`'s inline part linear in a line's length (a crafted 195 KB line from a minute to milliseconds; nested image / link labels as before); the run list's 15-second refresh skips a hidden tab and runs once when it becomes visible | `tests/web/test_v21zbr_page_inline.py`, `test_v21zbr_page_runs_poll.py` | done |
| Status JSON | no `run_dir` (no page read it); `out_dir` of `GET /api/configs` stays | `tests/web/test_v21zbr_server_status.py` | done |
| Image context | `.dockerignore` and `deploy/docker/root.dockerignore.example` leave out `agent-doc/`, `CLAUDE.md`, `scripts/handbook/`, Python and tool caches, coverage files, `out/` (the example at any depth); the Dockerfile unchanged; a company repository that copied an older example builds as before until it copies the new one | `tests/deploy/test_v21zbr_image_context.py` (every path the Dockerfile copies survives both ignore files); image builds tried locally in both layouts | done |
| Without a visible effect | unused code removed from `sbmig/remediations/` (`rerun_ok`, `intake.restore_missing`, `models.versions_of`, `planner._root_entries`, `_Holder.nothing_changed`, five constants, the `sync_tree` fallback, the `_fix_tree_hash` branch for a tree without a table); `openrewrite.fail_on_dry_run_results` leaves the config model (a file or env override that sets it gets the removed-key sentence); `compare_trees` reads only what an attempt changed; `freeze_delivery` hashes while it writes (identical ZIP bytes); "tests were skipped" from the typed state; `RunManager.status` reads its two control files once; `_fw_scans` and the admin page's `_failures` drop entries that are expired or can be computed again; `config/default.yml` lists every settable key with a Korean comment, `deploy/ENV.md` every variable the manifests set | `tests/remediations/test_fixer.py`, `test_intake.py`, `tests/core/test_v21zbr_default_config.py`, `tests/deploy/test_v21zbr_env_doc.py` | done |
| Pinned, not changed | when the raised version cannot be resolved, the candidate build fails before compilation and reaches the fix loop once: 2 LLM requests, 3 builds, then FAILED `candidate_build_failed`; the numbers decide a later round | `tests/remediations/test_v21zbr_pins.py` | recorded |
| Checked | unit tests (full suite 7,387 passed) and two independent reviews on a developer Mac; the image build tried locally in both repository layouts | — | done |
| Left out on purpose | a hard link for the delivered original (with the disk measurements on the cluster); the image's pip layer (the company build cannot be tried here); the KB credit read from note text (needs a typed failure); bounding `_run_locks` | — | open |
| Not checked | the company cluster (needs a new image), a company repository with the new ignore example | — | open |

## Status — 2026-11-22, answers that carry a file's content never fail on a non-finite number; `intake.sync_tree` removed (v2.1z-bs)

Two small leftovers of v2.1z-br; a third item was implemented and withdrawn in review. Contract: `docs/INTERFACES.md`
"v2.1z-bs". Commits `67ad5bf`, `0a45d0a`.

| Item | What | Verification | State |
|---|---|---|---|
| Non-finite numbers | `GET /api/runs/{id}`, the answer of `POST /api/runs`, the code-change routes, the admin usage answer and the `status` event of the run's event stream send a `NaN` / `Infinity` / `-Infinity` read from a run file as `null` (before: a 500, or the bare word `NaN` in the event that a browser cannot parse); `sbmig/web/jsonsafe.py` is the one place; a payload without such a value is sent with the same bytes as before | `tests/web/test_v21zbs_server_status.py` | done |
| `status()` reads once | `RunManager.status` reads `report.json` once per call | same file | done |
| `intake.sync_tree` | removed with its interface entry, its fake and the tests that covered only it (nothing called it); what they proved about the code it shared with `sync_tree_table` is tested on `sync_tree_table` | `tests/remediations/test_intake_build_output.py` | done |
| Loop skip withdrawn | skipping the remediation's fix loop when every parsed error of the failed candidate build is one only a pom or the repository can fix was implemented, reviewed and taken out: a repository that fails for a moment is parsed as a resolution error, so the skip also dropped the loop's first rebuild, which then passes and ends the round as fixed; and the recorded decision survived a resume that builds the candidate again, so the report explained one build's failure with another build's errors. The loop starts exactly as before and the pin of v2.1z-br stays; what a later round must keep true is the contract paragraph "Not changed" | `tests/remediations/test_v21zbr_pins.py` | withdrawn |
| v2.1z-br with real Maven | verified today on a developer Mac with the fake LLM: the integration-marked tests gave 72 passed and 7 skipped on both `61ff9a0` and its parent `c1ebe82` with JDK 21 (with `JAVA_HOME` unset one test fails on both, because Maven then runs on Java 25); real migration runs of the `pure-java` fixture and of a variant with a failing unit test, and four real remediation scenarios (snakeyaml 1.33 to 2.0, the candidate fails to compile: good fix, bad fix, tests compiled, tests compiled and run) launched the same Maven processes with identical command lines on both commits, every build ran once, and both `skip_tests` call sites were reached and sent `-DskipTests` as before. Not covered: a runner without the keyword (none is shipped), the company cluster | real Maven, fake LLM | done |
| Checked | unit tests (full suite 7,413 passed) on a developer Mac | — | done |
| Not checked | the company cluster | — | open |

## Status — 2026-11-23, result ZIPs received under the project name (v2.1z-bt)

The user: "변환된 프로젝트 받기랑 결과 ZIP 받기도 프로젝트 명으로 다운 받을 수 있도록 하자! 둘다 result.zip은 좀 아닌거 같아!
프로젝트명-migration.zip, 프로젝트명-remediation.zip으로!" Contract: `docs/INTERFACES.md` "v2.1z-bt".

| Item | What | Verification | State |
|---|---|---|---|
| One rule | `contract.zip_download_name(run_id, kind, project)`: `<project>-migration.zip` / `<project>-remediation.zip`, the run id when no usable project name was entered, `result.zip` only when neither fits | `tests/web/test_zip_download_name.py` | done |
| Migration download | `GET /api/runs/{id}/files/result.zip` attaches as `<project>-migration.zip` (project from memory, `request.json`, `web-status.json` or the journal's `web_run` line); every other file keeps its name; the status, the run list rows and the event stream carry `zip_name` without an extra file read | `tests/web/test_v21zbt_zip_names.py` | done |
| Remediation download | `GET /api/remediations/{id}/files/result.zip` attaches as `<project>-remediation.zip` (chained and remediation-flow runs: the migration run's entered project; standalone: its id); the status carries `zip_name`; `result-verified.zip`, the reports and the rest keep their names | `tests/remediations/test_v21zbt_zip_names.py` | done |
| Page | 「변환된 프로젝트 받기」 and 「결과 ZIP 받기」 show and save the status's `zip_name`; the code-change view's sentence no longer names `result.zip` (nor `changes.patch`, which a page user cannot get) | `tests/web/test_v21zbt_zip_names.py`, page tests | done |
| Remediation wording | the report's steps, its summary line and its failed-delivery warning, and the Korean status messages shown on the page and in 「상세:」, say 「결과 ZIP」 instead of the run-folder path `delivery/result.zip`; the collapsed processing record keeps the path | `tests/remediations/test_v21zbt_zip_names.py`, `test_report.py`, `test_i18n.py` | done |
| Review | one independent review against the contract: no break found; a flaky byte comparison in a new test and the status-message wording were fixed | — | done |
| Checked | unit tests (full suite 7,468 passed) on a developer Mac | — | done |
| Not checked | a real browser on the company PC, Job mode on the cluster | — | open |

## Status — 2026-11-23, Jackson 3 WebFlux codecs in the fix notes (v2.1z-bu)

A company run ended with a WebFlux module unfixed, blocked as `oscillation`. Read from the run and reproduced here.
Contract: `docs/INTERFACES.md` "v2.1z-bu".

| Item | What | Verification | State |
|---|---|---|---|
| Cause in the run | the recipe moved the module's code to Jackson 3 and left Spring's codec classes on Jackson 2; that module's spring-web was older than 7.0 (`cannot find symbol class JacksonJsonDecoder`), so the LLM went between `Jackson2JsonDecoder` and `JacksonJsonDecoder`, neither of which can work there | the user's log lines; `javap` on spring-web 6.1 / 6.2 / 7.0 | read |
| Note | `knowledge/notes/jackson-2-3.md` Rule A: the WebFlux codecs (`JacksonJsonDecoder` / `JacksonJsonEncoder`, Smile, CBOR) take `JsonMapper`; register with `jacksonJsonDecoder` / `jacksonJsonEncoder`; never back to Jackson 2; `cannot find symbol class JacksonJsonDecoder` means an older spring-web: remove the pin. Keywords for the codec names in the note and `index.yml` | `tests/fix/test_v21zbu_jackson_codecs.py` | done |
| Reproduction | real Maven, nine shapes: Jackson 3 goes missing only in a module that gets Jackson 2 through a non-Jackson library (the recipe rewrites declared Jackson artifacts only); every shape had spring-web 7.0.x | `out/scratch/webflux-jackson/` | done |
| Open | what kept the company module's spring-web below 7.0 (asked of the user); a deterministic repair of the transitive-Jackson shape | — | open |
| Checked | unit tests (full suite 7,474 passed) on a developer Mac | — | done |
| Not checked | a real LLM with the new note, the company project | — | open |

## Status — 2026-11-23, Jackson 3 exceptions in catch clauses (v2.1z-bv)

The user's local rerun of the same company project on `2a13d5c` was blocked as `oscillation` on exceptions after the
move to Jackson 3. Contract: `docs/INTERFACES.md` "v2.1z-bv".

| Item | What | Verification | State |
|---|---|---|---|
| Note | `jackson-2-3.md` Rule A: a catch with its own handling is kept; `exception JacksonException has already been caught` → move the Jackson block above the broader `RuntimeException` / `Exception` catch, delete neither, no multi-catch; `IOException is never thrown` around Jackson 3 calls → catch `JacksonException` instead, or drop the dead block when one exists; both messages in "Typical errors"; keyword for the `has already been caught` message of the four Jackson 3 exception classes | `tests/fix/test_v21zbv_jackson_catch.py`; messages and hierarchy checked with `javac` / `javap` on jackson 3.1.8 | done |
| Checked | unit tests (full suite 7,479 passed) on a developer Mac | — | done |
| Not checked | a real LLM with the new note, the company project | — | open |

## Status — 2026-11-24, Jackson 2 → 3 in the fix notes: one direction, Redis, Kafka, Jackson's own API (v2.1z-bw)

After the two oscillations of v2.1z-bu / v2.1z-bv the user asked which other libraries can go the same way, said the
company projects use Kafka and Redis a lot, and that a run of 2026-11-23 had oscillated on Redis too. Contract:
`docs/INTERFACES.md` "v2.1z-bw".

| Item | What | Verification | State |
|---|---|---|---|
| Survey | what `UpgradeJackson_2_3` and the Spring recipes leave: Jackson's own API, the Spring portfolio, third-party libraries; 141 compiler errors collected. The note `jackson-2-3.md` was already selected for 113 of them: the gap was its content | jars, `javap`, recipe yml, `javac` on Jackson 3.1.4 / Boot 4.1.0 under `out/scratch/jackson-libs/` (git-ignored); the real selector | done |
| Rule | `jackson-2-3.md` "Which rule applies" and system prompt rule 8: code on `tools.jackson` is never moved back, a missing line is added as a dependency; the rule is chosen by the class the failing call talks to (Rule C module, Spring class, Jackson 3's own API, another library); Rule B no longer moves a file back (fully qualified Jackson 2 types at the library's boundary); `class file … not found` no longer reads as "add Jackson 2"; `package tools.jackson.… does not exist` adds Jackson 3 | `tests/fix/test_v21zbw_jackson_rules.py`, `test_v21u_prompts.py`, `test_prompt_scope.py` | done |
| Redis, Kafka | Rule A of `jackson-2-3.md` carries the Spring Data Redis and Spring Kafka counterparts itself (three of the five Redis messages name no Redis class, so only that note reaches their prompt); `jackson-3-spring.md` (new): Redis and Kafka in full, one line per other Spring module | `tests/fix/test_v21zbw_jackson_spring.py` (every Redis message reaches a note that names the counterpart) | done |
| Jackson's own API | `jackson-3-api.md` (new): the immutable mapper, default typing, moved and removed features, the built-in modules, custom serializers / deserializers, the recipe's rewrites that do not exist in 3.1 | `tests/fix/test_v21zbw_jackson_api.py` | done |
| Review | four independent rounds with their own compiles (JDK 21) and runs on Jackson 2.21.4 / 3.1.4 and spring-data-redis 4.1.0: replacements that compiled and changed the output or the stored data format; loops and dead ends of the rule text with a stand-in in-house jar; facts; keywords. What they found and what was changed: the contract's "Review" and "Not covered" paragraphs | reviewers' reproductions under `out/scratch/jackson-libs/review-*` | done |
| Checked | unit tests (full suite 7,522 passed) on a developer Mac | — | done |
| Not checked | a real LLM with the new notes; the company projects; Maven's wording of these errors (the survey used `javac`); the full recipe chain of a run; run-time behaviour beyond the serializer and mapper runs | — | open |
| Not done | no code change: scoring, what a case's prompt says about another case's attempt, a deterministic repair of the recipe's wrong rewrites, the case identity of messages that need different rules | — | open |

## Status — 2026-11-24, Boot 4 renamed the Redis auto-configuration classes (v2.1z-bx)

The Redis oscillation of the run of 2026-11-23, as the user read it out, was about `RedisConnectionDetails`, not about
the serializers of v2.1z-bw. Contract: `docs/INTERFACES.md` "v2.1z-bx".

| Item | What | Verification | State |
|---|---|---|---|
| Cause | Boot 4 moved `RedisConnectionDetails` / `RedisProperties` to `org.springframework.boot.data.redis.autoconfigure` and renamed them `DataRedis…`; the note's "moved to `<tech>.autoconfigure`" gave a name that does not exist, the LLM took it for removed and ported the code to a type whose old name is gone too | the user's lines; jar listings and `javap` of Boot 3.3.13 and 4.1.0 | read |
| Note | `boot4-packages.md`: the Redis renames, Kafka (moved only), the JPA / Hibernate auto-configuration classes; "a moved name that is not found was renamed, not removed: never port the code to another type"; a keyword for the old and new names | `tests/fix/test_v21zbx_boot_redis_names.py` | done |
| Checked | unit tests (full suite 7,527 passed) on a developer Mac | — | done |
| Not checked | a real LLM with the note, the company project; other Boot modules with renamed classes | — | open |

## Status — 2026-11-24, what a case is told about the earlier fix its errors come from (v2.1z-by)

The first half of handling oscillation in the loop itself (the user: for something called an agent, solving the
oscillation is the better way; "일단 진동이슈부터 해결"). Contract: `docs/INTERFACES.md` "v2.1z-by".

| Item | What | Verification | State |
|---|---|---|---|
| Read in the code | case Y, left behind by a kept fix of case X, got one hint line naming that attempt: not what it was fixing, not what it changed; X is no longer in the build, so nothing told the model that undoing the fix brings X back. When X was back, its own `improved` attempt was listed under "they did NOT resolve it" | `sbmig/loop/fixloop.py`, `sbmig/fix/context.py` | read |
| Prompt | section "Earlier fix these errors come from (it is in the tree)" directly after the case's errors, never truncated: the attempt, the errors it was fixing, its diff lines in this case's files, "undoing it brings them back, leave both sets resolved"; only for an attempt that resolved its own case and is still in the tree, else the old hint line. Section "These errors were resolved before and are back" for the case's own attempt whose errors (file and signature) returned. Rule 13 says both | `tests/loop/test_v21zby_earlier_fix_context.py`, `tests/fix/test_prompt_scope.py` | done |
| Bookkeeping | `_introduced` names the latest attempt that brought an error into a file and forgets an error that is no longer in a build; a zero-LLM pre-try of the same case is not "another case" | same test file | done |
| Review | one independent review with failing scratch tests: an attempt that had NOT resolved its case was described as a fix not to undo; "resolved before and are back" was said for the same case in a file the build had not reached; a stale entry named the wrong attempt; a test that could not fail; the split by object identity. All fixed and ported as regression tests | `out/scratch/earlier-fix-review/` | done |
| Checked | unit tests (full suite 7,554 passed) on a developer Mac | — | done |
| Not checked | whether a real LLM then finds a fix that leaves both sets resolved; the fixes of the review were not reviewed again | — | open |
| Not done | scoring: a fix that undoes another one is still `improved` when its own errors disappear (next round) | — | open |

## Status — 2026-11-24, a fix that undoes another case's fix: the trade-back rule, observed first (v2.1z-bz)

The second half of handling oscillation in the loop itself. Contract: `docs/INTERFACES.md` "v2.1z-bz".

| Item | What | Verification | State |
|---|---|---|---|
| Read in the code | `_score` says `regressed` only when an attempt's own errors remain; a fix that makes its errors disappear by undoing another case's fix is `improved`, both scripts are credited as successes, and the run ends at the build-level oscillation check | `sbmig/loop/fixloop.py` | read |
| Rule | a trade-back needs all four: an error that an earlier kept fix of another case had resolved is back; that fix had changed the error's line; one hunk of the attempt's own diff puts those lines back; the error is reported on them again. An error without a line, an unreadable diff or a build that cannot tell gives no verdict | `tests/loop/test_v21zbz_trade_back.py` (53 tests) | done |
| Modes | `loop.trade_back`: `observe` (default) records the verdict in the journal (`trade_back`, `mode: observe`) and on the attempt, and changes nothing else; `revert` scores the attempt `regressed`, reverts it, credits the script as a failure and blocks the case that keeps undoing at `loop.oscillation_limit`; `off` computes nothing. State is per run of the loop | same test file; the loop, report, core and remediation test folders pass with each value in the environment | done |
| Deployment | `SBMIG_LOOP__TRADE_BACK` as an optional key of `sbmig-env` (commented in 20), its line in the 81 template, rows in `deploy/README.md` and `deploy/ENV.md` | `tests/deploy/test_manifests.py` | done |
| Real Maven | a small project with a scripted LLM (fix X leaving Y, fix Y by restoring the old lines, fix Y properly): `revert` ends BUILD SUCCESS with 4 LLM calls; `observe` and `off` end BLOCKED after 37, `observe` with the `trade_back` event | `out/scratch/trade-back/run.sh` (run by the implementer and again by the PM) | done |
| Review | two independent rounds with reproductions. The first: the (file, signature) pair alone reverted a correct fix that caused a new error of the same kind, blamed an attempt after a build that had stopped early, and carried a queued block into the next run. The second: javac hides flow errors behind attribution errors, so a correct fix that restored an import was reverted; the attempt's hunks were pooled. The rule was narrowed each time | `out/scratch/trade-back-review/`, `out/scratch/trade-back-review2/` | done |
| Why `observe` is the default | each review found a legitimate run that the rule, as it then stood, turned into a blocked case; its last form was not reviewed again. What the rule would do is read from real runs' journals first | — | decided by the PM, told to the user |
| Checked | unit tests (full suite 7,610 passed) on a developer Mac | — | done |
| Not checked | a real LLM, the company projects, multi-module and MSA projects with real Maven, the remediation's loop and framework runs, how often a real undo restores the old lines exactly | — | open |

## Status — 2026-11-24, a stored script that took part in a round trip is not a success and is not reused on its own (v2.1z-ca)

The KB side of the trade-back rule. Reproduced with real Maven before the change (`out/scratch/kb-replay/`): after the
`observe` run of `out/scratch/trade-back/`, both stored versions of the round trip read `successes 1, failures 0`, and a
second run on that KB reused both without an LLM call and made the round trip again. Contract: `docs/INTERFACES.md` "v2.1z-ca".

| Item | What | Verification | State |
|---|---|---|---|
| Credit | an attempt with a trade-back verdict that ran a stored version is credited `round_trip` (a failure), never `improved`; in `revert` it is `round_trip` instead of `reverted`. The resolver whose fix was undone gets `round_trip` too, replacing the credit of its own try, when the undo stays in the tree (`observe`, a failed revert, `loop.revert_on_regression: false`). Journal `script_credit` with `"credited": "round_trip"`, a note on each attempt | `tests/loop/test_v21zca_round_trip_credit.py` | done |
| Store | `round_trip` is in `FAILURE_RESULTS`; `CaseScript.round_trip` (default false); a success already recorded for the same try (`run_id`, `round`, `n`) and the version's own scope is taken back (`successes` −1, `projects`, `proven` → `unverified` unless resolved elsewhere); other-scope runs only in `runs[]`; a KB import keeps the mark when either side has it | `tests/errors/test_v21zca_round_trip.py` | done |
| Reuse | `CaseStore.best`, `stored_best` and `stored_next` never select a marked version (as `non_idempotent`); `latest` (the augment base) unchanged; `scripts_text` shows ` round-trip` | `tests/triage/test_v21zca_round_trip_reuse.py` | done |
| Clearing | a later success of the version's own scope (only an LLM answer with the same code can give it) or `sbmig kb cases activate <case> <version>` | same test files | done |
| `observe` | the run is unchanged (no revert, no block, the outcomes of "v2.1z-bz"); the KB credit of the two versions is the one difference. `off`: byte for byte as before | same test files | done |
| Docs | `agent-doc/04-cases-scripts-kb.md` (reuse order, the trade-back paragraph under scoring, the field, `activate`), `docs/kb-map/`, `docs/migration-agent/README.md`, `docs/PROGRESS.md`; the comment of `loop.trade_back` in `config/default.yml`, its line in `deploy/k8s/20-configmap.yaml` and its rows in `deploy/README.md` / `deploy/ENV.md` now say that `observe` marks the stored scripts | `tests/docs`, `tests/deploy` | done |
| Checked | full suite 7,671 passed on a developer Mac; real Maven 3.9.10 / JDK 21, two runs in a row on one KB with `observe` (`out/scratch/round-trip-kb/`): both versions marked after run 1 (`successes 0`), run 2 reuses neither and asks the LLM; on the KB an oscillating run had left before the change, the first run replays both once and marks them, the second reuses neither | — | done |
| Review of the change | one independent review with reproductions (`out/scratch/round-trip-review/`): a version of the other scope was not marked, a success of a reuse cleared the mark, a `kb import` of an earlier export brought the taken-back success back. All three fixed with regression tests (`credit(..., reused=)`, the mark in every scope, the take-backs a merge can see); a verification of those fixes (`out/scratch/round-trip-verify/`) confirmed the first two, found that the first form of the import fix (a recount from the capped run history) dropped the successes of a widely reused version, and that a mark set from a run of the other scope could not be cleared by an LLM answer: both fixed with regression tests, the verifier's reproductions pass | — | done |
| Review of the rule | a third independent review of the trade-back rule (`out/scratch/trade-back-review3/`, fakes and real Maven): `revert` is not fit to be the default (it always keeps the earlier fix; when that one was wrong, the correct restore is reverted and blocked). `observe` stays the default; recorded in `docs/INTERFACES.md` "v2.1z-bz" | — | done |
| Limits | a KB written before this change may hold such pairs as successes: they are marked the first time a run replays them and reaches the verdict (that run still makes the round trip once; `sbmig kb cases retire` or a new KB folder avoids it); an undo that rewrites the lines instead of restoring them gives no verdict and so no mark; a build pod on an older image does not honour the mark (it reuses such a version) but keeps the field when it rewrites the `case.json` | — | known |
| Not checked | a real LLM, the company projects, the company KB that already holds such pairs | — | open |

## Status — 2026-11-24, the same error in another module is another error (v2.1z-cb)

A company run on a multi-module company project ended blocked as oscillation after nine builds while it was fixing
the same maven-compiler-plugin error (`Cannot find version for annotation processor path
'org.projectlombok:lombok.jar'`) one module per build: Maven stops at the first failing module, the error has no
file, and the stall / oscillation checks, the scoring and the error list could not tell module 1's error from
module 2's. Contract: `docs/INTERFACES.md` "v2.1z-cb".

| Item | What | Verification | State |
|---|---|---|---|
| Place | the place of an error is its file, else `@<module>` for an error without a file, else `''` (`_place` in `sbmig/loop/fixloop.py`) | `tests/loop/test_v21zcb_module_place.py` | done |
| Scoring | `_pair` returns (place, signature): what is left of an attempt's targets, what is new, `introduced`, the resolved pairs of the trade-back rule | same test file | done |
| Stall / oscillation | the multiset compared between builds is the count per (place, signature); the same kind of error in another file or module is another error set. `IterationRecord.signatures` (the report) stays `signature_multiset()` | same test file | done |
| Error list | `_match` pairs a file-less error only with an entry of the same module; an occurrence in another module is a new entry carrying its module; two modules failing in one build are two entries | `tests/errors/test_v21zcb_module_entries.py` | done |
| Not changed | errors with a file; case ids and L1 / L2 signatures (the module stays masked, the KB reuses a case across modules and projects) | — | — |
| Checked | real Maven 3.9.10 / JDK 21, a three-module project whose modules each list a processor path without a version, scripted LLM (`out/scratch/module-place/`): with answers that differ per module the three poms are fixed one after the other, no `stall` and no `oscillation` event, one list entry per module, the build passes. Before the change the unit test of the same sequence gave two oscillation events, and the direct case scored the first fix `no_change` and rolled it back | — | done |
| Review | one independent round on a copy of the commit (`out/scratch/module-place-review/`): the checks lost nothing that costs LLM calls (50 rounds per sequence: an error alternating between two places is blocked as OSCILLATION after 3 calls where it ran to the cap of 20; an error that never repeats a place ends at the same cap in both). One defect, older for errors with a file and now reachable: after a rollback the next try's baseline counted every targeted error absent from the build as restored, also those the build could not show, so identical builds read `partial` and the change stayed. Fixed: only errors the build could have shown (`_visible_in`) count as restored | `tests/loop/test_v21zcb_restored_visible.py` | done |
| Left open | the ladder still read the occurrence in the next module as the same case going on: closed by v2.1z-cd | — | closed |
| Not checked | a real LLM, the company projects | — | open |

## Status — 2026-11-24, an annotation processor path without a version gets one in the restructure (v2.1z-cc)

The cause of the error in v2.1z-cb: the module poms list Lombok under maven-compiler-plugin's
`<annotationProcessorPaths>` without a `<version>`, the as-is parent's dependency management supplied it, and the
target chain does not manage `org.projectlombok:lombok`. The version step wrote the `<dependencies>` entry, the
processor `<path>` stayed empty, and every module cost the fix loop an LLM ask and two builds. Contract:
`docs/INTERFACES.md` "v2.1z-cc".

| Item | What | Verification | State |
|---|---|---|---|
| Rule | in every restructure mode that runs the version step, right after `_drop_managed_versions`: each `<path>` under a maven-compiler-plugin `<annotationProcessorPaths>` (build plugins and plugin management) with groupId and artifactId, no `<version>` and no `${…}` gets the first that applies: (1) managed in the pom's own chain inside the project or the company parent chain: left; (2) the pom's own dependency of the same g:a, classifier and type: the same text; (3) what the dependency step would write; (4) the one literal version of the project's other poms; (5) left with a note naming the path and the pom. Never the repository's newest release; steps 3 and 4 skipped for a classifier or a non-jar type; protected keys untouched | `tests/restructure/test_v21zcc_processor_paths.py` | done |
| Recorded | each version written is a line of `RestructureResult.versions_pinned` (`<g:a>: <version> ← <source> (annotationProcessorPaths, <pom>)`), one restructure note with the count, journal `versions_pinned` with `"section": "annotationProcessorPaths"`, error-list rows `annotation processor path version written: <g:a> <v> (<source>)` | `tests/loop/test_v21zcc_processor_path_rows.py` | done |
| Not changed | runs without a restructure (a framework run keeps its parent); paths with a version, also when it differs from the dependency's; the fix loop's handling of the message when it still appears | — | — |
| Maybe managed | without the effective pom of the new chain, a pom whose chain imports a BOM inside the project or has a `<dependencyManagement>` in a profile gets no version from steps 2 to 4 (one note per path): writing one could override what Maven would have resolved from that management | `tests/restructure/test_v21zcc_processor_paths.py` | done |
| Checked | 27 unit tests; real Maven 3.9.10 / JDK 21 with the real pipeline on a three-module project whose as-is parent managed the path's artifact (`out/scratch/processor-paths/`): with the step switched off the first builds stop with the message for one module after the other, with it three versions are written and the run ends `build=ok` | — | done |
| Review | one independent round on a copy of the commit (`out/scratch/processor-path-review/`, sbmig probes and real Maven): the refactoring of the dependency step's version choice is behaviour-preserving (72-case differential), MS949 / CRLF poms are edited byte-true. Found and fixed: an imported BOM not seen without the effective pom (measured: a Lombok path left empty builds through the BOM, the same path with the as-is 1.18.12 written fails on JDK 21), a repository-guessed version reaching paths, a classifier path left empty because the plain g:a was managed, a Boot BOM version written without an as-is capture. The fixes were not reviewed again | — | done |
| Limits | maven-compiler-plugin resolves a version-less path from the dependency management from 3.12.0 on: under an older plugin a managed path that the step leaves empty still fails; the requester's Korean report shows a written version only in the pom's diff | — | — |
| Not checked | a real LLM, the company projects, `msa_pom` mode by a test, the e2e fixtures (none has a version-less path) | — | open |

## Status — 2026-11-24, a case that shows up at a new place starts with the script that resolved it elsewhere in this run (v2.1z-cd)

The rest of the run of v2.1z-cb: the tries of a case were counted per case and run, wherever its errors were, so the
same module-level error in module 2 read as "the case is still not resolved" and the LLM was asked to augment the
script that had just resolved module 1. The real-Maven reproduction (`out/scratch/module-place/`, answers with the
same code for every module) ended blocked `repeated_failure` after 37 LLM calls. Contract: `docs/INTERFACES.md`
"v2.1z-cd".

| Item | What | Verification | State |
|---|---|---|---|
| Places | an earlier attempt of the case was made here when one of the places of the errors it targeted is a place of the case's errors in the current build, elsewhere otherwise (files through the registry's canonical-file mapping) | `tests/triage/test_v21zcd_new_place.py` | done |
| Ladder | `BuildFixLoop._previous(ladder=True)` holds the attempts made here only, so `script.max_llm_tries_per_case`, `max_augments_per_case`, `max_edit_tries_per_case`, `max_declines_per_case` and `reuse_versions` hold per case, run and set of places; the prompt's "Previous attempts on this case" keeps every attempt | `tests/loop/test_v21zcd_ladder_per_place.py` | done |
| First rung at a new place | with nothing made here, the stored version whose run resolved the case elsewhere in this run (`improved` / `build_ok`, the script ran) runs again first: a reuse, no LLM call, source `kb-script`, log line `KB 탐색: 이번 실행의 v<N> (<attempt>, <place>에서 해결) → <place>에 다시 실행`; not with `script.reuse: false`, a `retired` / `non_idempotent` / `round_trip` version or missing code. It counts as the stored-version step of the new place; if it does not help, augment, then new | same test files | done |
| Identical code | "the LLM answered with v<N> again (identical code)" is refused only for the places it ran on | same test files | done |
| Checked | real Maven 3.9.10 / JDK 21, the three-module project of v2.1z-cb, scripted LLM (`out/scratch/ladder-per-place/`): answers with the same code for every module end `build=ok` with 3 LLM calls (the structure review and one per case) where the run had ended blocked after 37; answers that differ per module: 3 calls instead of 6; full suite 7,764 passed on a developer Mac (after v2.1z-cd and the review fixes of the day) | — | done |
| Bound across places | the LLM asks of a case in one run, over every place, are capped at twice `script.max_llm_tries_per_case` (40 with the default): then the case is blocked `repeated_failure` whatever place it shows at; reruns are not LLM asks and do not count | `tests/triage/test_v21zcd_new_place.py`, `tests/loop/test_v21zcd_ladder_per_place.py` | done |
| Review | one independent round on copies of the commit and its parent (`out/scratch/ladder-review/`): no path that runs a wrong script (reruns go through the same script runner and guards, another MSA target's attempts are no candidates, a `non_idempotent` version is never rerun). Found and fixed: a case cycling through five places (more than `loop.oscillation_window`) used 45 LLM calls where the parent commit blocked after 20 (the bound across places: the same ring now ends blocked after exactly twice the cap); a rerun candidate was checked against the store's `round_trip` mark only (an attempt the loop recorded in `_round_trips` is no candidate either). The fixes were not reviewed again | — | done |
| Limits | with `script.reuse: false` there is no rerun; a rerun that changes nothing at the new place is credited `apply_failed`; the caps hold per set of places, so a case can use more LLM calls in total than `script.max_llm_tries_per_case`, up to the bound across places; a script that resolves one place and breaks a new one every time is rerun without an LLM call until the rounds end (builds only); an answer applied as edits leaves no rerun candidate | — | known |
| Not checked | a real LLM, the company projects | — | open |

## Status — 2026-11-24, a rename or a removal that Windows refuses while another program has a file open is waited for (v2.1z-ce)

The company PC run of the evening (Windows, local web run, on the head of v2.1z-cd): the migration ended BUILD
SUCCESS, 101 entries in the error list, 100 resolved. The module-level annotation processor case of v2.1z-cb was
resolved in four modules by reruns of one stored script, without an LLM call, so v2.1z-cb and v2.1z-cd hold on a
company project; the step of v2.1z-cc did not keep that error out of the first build there (the reason is in a log
line not yet read). The chained remediation then ended `FAILED internal_error` 27 seconds after its start:
`PermissionError: [WinError 5]` at the rename of `remediation/.accepted.partial` to `accepted`. The same rename had
passed on that PC on 2026-11-12 with the same code. Contract: `docs/INTERFACES.md` "v2.1z-ce".

| Item | What | Verification | State |
|---|---|---|---|
| `fsops` | `sbmig/remediations/fsops.py`: `replace`, `rename`, `unlink`, `rmtree` wait for Windows errors 5 and 32 (`rmtree` also 145) for up to 60 s, pauses from 0.05 s doubling to 2 s; a read-only entry is made writable and removed; anything else is raised at once, so Linux is unchanged | `tests/remediations/test_v21zce_fsops.py` | done |
| Call sites | every rename, replace and tree removal of `sbmig/remediations/` goes through it (the intake's rename first); left alone: the stale-claim takeover, the cleanup that ignores errors, the service's job-folder removal; a syntax-tree test keeps new bare forms out | same file | done |
| Callers that go on | an exception raised after a full wait is marked (`fsops.waited_out`): `intake._write_back` does not repair and try again for it; inside `fsops.one_wait()` (`intake.sync_tree_table`, `fixer._remove_leftovers`) later refusals wait 2 s after one give-up | same file | done |
| Log lines | `remediation: <op> of <name> refused (WinError <n>) … waiting up to <s> s`, `… went through after <t> s (<k> tries)`, WARNING `… still refused after <t> s …`; the RUNBOOK symptom table and `agent-doc/11` name them | — | done |
| Review | three rounds by one independent reviewer, every finding reproduced first: a double wait in `_write_back` (120 s per held file), a claim takeover acting on an old judgment, a process-wide give-up memory that took the wait from unrelated calls, three full waits at the end of a fix loop, test gaps; Linux compared call by call on Python 3.12 and 3.13 (450 cases, no difference) | `out/scratch/fsops-review/` | done |
| Checked | full suite 7,807 passed on a developer Mac | — | done |
| Limits | a program that keeps a file open for more than 60 s still fails the step; a refusal that can never go away and reports error 5 is waited for too; a stop request is not looked at while a call waits | — | known |
| Not checked | Windows itself (the tests raise the Windows errors from patched calls); Python 3.10, 3.11, 3.14; which program held the file on the company PC | — | open |


## Status — 2026-11-25, a refusal that lasts names the programs that have the file open (v2.1z-cf)

The company PC retest of v2.1z-ce (Windows, local web run): the intake's rename passed, the remediation reached its
second round and failed in `freeze_delivery`: `replace of .result.next.zip still refused after 60.0 s (36 tries,
WinError 5)`, the destination being round 1's `delivery/result.zip`. Minutes later the file could be renamed by
hand, so the holder lasted more than a minute and not for good. Which program it was is not known. Contract:
`docs/INTERFACES.md` "v2.1z-cf".

| Item | What | Verification | State |
|---|---|---|---|
| Holder names | `fsops.holders(paths)`: the programs that have a file open, through the Windows Restart Manager (`ctypes`, standard library); `this process` marked; base names only | `tests/remediations/test_v21zcf_holders.py` (callbacks with the documented signatures, structure layout) | done |
| When | one lookup, when the 60 s are up, in a daemon thread waited 5 s at most; none while the call is retried, none inside a `one_wait()` block after a give-up, none while an earlier one has not returned | fake clock and real clock tests | done |
| Log line | WARNING `… still refused after <t> s (…): open in <names>`; when nothing is found, the lookup failed or did not return, the old sentence with the reason in brackets; the "refused" and "went through" lines are unchanged | exact-text tests; RUNBOOK symptom row | done |
| Own descriptors | a test guard that fails when this process holds a descriptor on a path it replaces, renames or removes (POSIX never notices): two-round delivery swap, intake, fix loop, resume, the IVAS upload | `tests/remediations/test_v21zcf_own_handles.py`, proved on a reader that never closes | done: nothing of ours is held |
| Review | four rounds by one independent reviewer; round 2 led to the cut to one lookup (a lookup during the retries was itself a handle under the directory); round 3 found nothing in the production code and a guard test that failed at random (the test run's disk watch); round 4 found nothing | every finding reproduced | done |
| Checked | full suite 7,915 passed on a developer Mac | — | done |
| Not done | the delivery still replaces `result.zip` in place: a holder that lasts still fails the round on Windows (on Linux an open file can be replaced). Whether to deliver under per-round names is decided when the holder is known | — | open |
| Not checked | Windows itself (the `ctypes` part never ran there); which program held the file | — | open |

## Status — 2026-11-25, the image's JDK is Amazon Corretto 21 at a pinned version (v2.1z-cg)

Relayed by the user: a named JDK 21 build in which Java4Shell (CVE-2026-114938) is fixed, fetched directly at that
version. The change log of Corretto 21 lists the CVE under `21.0.13.1.2` (2026-11-17). Contract:
`docs/INTERFACES.md` "v2.1z-cg".

| Item | What | Verification | State |
|---|---|---|---|
| Dockerfile | Debian's `openjdk-21-jdk-headless` is gone; the Corretto package of `CORRETTO_VERSION` is downloaded from `CORRETTO_BASE_URL`, its SHA-256 compared with the argument of the architecture, installed before `maven`; the build stops on a wrong checksum, on an OpenJDK package of Debian in the image, and when `java -version` is not the pinned build; `/opt/java21` links to the Corretto folder | real builds for arm64 and amd64 (emulated), a local mirror, a wrong checksum; `tests/deploy/test_v21zcg_image_jdk.py` | done |
| Closed network | `CORRETTO_BASE_URL` for an internal mirror with the `<version>/<file>` layout; `CORRETTO_TLS_VERIFY=0` for a network that inspects HTTPS (the checksum is still compared) | the mirror build; the switch only against the public address | done |
| Operator text | `deploy/README.md`: the argument table and one paragraph (which JDK, the mirror, the certificate error, moving to a newer build) | read | done |
| Not checked | a proxy, HTTPS inspection, the company's registry and cluster | — | open |

## Status — 2026-11-25, the admin page's log ZIP of one run carries the run's diagnostic files (v2.1z-ch)

The user, reading a failed cluster run: the admin download was too thin for analysis; the name of the pom an
OpenRewrite run could not read was in `logs/`, reachable only with kubectl. Contract: `docs/INTERFACES.md` "v2.1z-ch".

| Item | What | Verification | State |
|---|---|---|---|
| Members | `GET /admin/api/runs/{id}/logs.zip`: the text files under `logs/`, `errors.md` / `errors.jsonl`, the reports, the status files, the remediation's state, journal, reports, scans and build output; never `project/`, the remediation's trees, ZIPs, jars, patches, hidden files, LLM records; the archive of all runs is unchanged byte for byte | `tests/web/test_v21zch_admin_run_logs.py`; both archives built from one fixture with HEAD's code and the new code | done |
| Redaction | every added member is redacted while it is copied (a member up to 8 MiB as a whole with the end-of-run pass's own functions, a larger one line by line), and left out with its reason in `SKIPPED.txt` when that is not possible; added members stay out while the run's pass is pending | the two ways the pass can fail, a folder without a control state, a secret split across reads, URL userinfo with no secret set | done |
| Files | planning opens no file; a member is a regular file with one name inside the run folder and must be the planned file when copied (device and inode); cut at its planned size | a folder swapped for a link, a hard link, a FIFO, growth during the copy | done |
| Caps | 2,000 members, 2 GiB (members left out in a stated order instead of a 413), 20,000 listed entries with half for `logs/`; every folder not listed gets a line | cap tests; 200 MiB log in 4 s, 2,000 files of 1 MiB in 42 s on a laptop | done |
| Review | three rounds by one independent reviewer: 7 findings, 3, none; every finding reproduced | — | done |
| Found on the way | the userinfo pattern of `redact.py` is quadratic on a long run of letters and digits (0.4 s for 16,000 characters); the archive leaves such a member out, the pattern itself is unchanged | measured | open |
| Not checked | the cluster, NFS, Windows, real 500 MiB logs | — | open |

## Status — 2026-11-25, OpenRewrite gets a settings file it reads the way Maven does (v2.1z-ci)

A cluster run (a framework project): `transform.rewrite` failed twice with `Failed to parse or resolve the Maven POM
file or one of its dependencies` while `mvn clean package` of the same tree passed; the mirrors of the settings had
been changed that day. A first diagnosis (jars uploaded without a pom) was wrong and was caught by reproducing
before coding: OpenRewrite tolerates such a library, and the `Failed to download` lines of the log are printed on
passing runs too. Measured instead: OpenRewrite applies the settings mirrors with its own code and differently
from Maven. Which pom failed on the cluster is not confirmed yet. Contract: `docs/INTERFACES.md` "v2.1z-ci".

| Item | What | Verification | State |
|---|---|---|---|
| Measurement | rewrite-maven-plugin 6.46.1: `external:http:*` matches nothing (the original url is asked), `<blocked>` is ignored, the first matching mirror in file order wins; a pom in the local repository counts only with its jar beside it; `Unable to download POM` is the fatal line | real runs against local servers with access logs (`out/scratch/rewrite-settings/`) | done |
| Shadow mirrors | the OpenRewrite call gets `-s <copy>`: the original text plus, at the front of `<mirrors>`, one `<mirrorOf>sbmig:<id></mirrorOf>` mirror per repository id whose mirror OpenRewrite would choose differently, with the url (and a `<server>` copy) of Maven's choice; `openrewrite.translate_settings`, default true | property tests (1,782 Maven cases, 330 OpenRewrite cases); an integration test with the real plugin | done |
| Maven unaffected | a shadow matches no repository in Maven; nothing existing is moved or edited | real Maven 3.9.10: identical exit codes, requests, auth headers and warnings for original and copy in every compared shape | done |
| Secrets | the copy (it holds the `<servers>`) is written 0600 into a private temporary folder and removed when the call returns; any error passes the original file | tests of modes, removal, failure paths | done |
| Failure text | `not reachable: <repository> (<reason>); Unable to download POM: <g>:<a>:<v>; tried <repository>: HTTP <code>` in `sbmig.log`, the journal and the first hand-over line | real logs as fixtures | done |
| Retry | `openrewrite.download_retry` starts for a transfer problem only (429 / 5xx, reset, timeout, unknown host), never for OpenRewrite's download warnings or a 404 | the real-log table | done |
| Review | six rounds by one independent reviewer; the first design (moving exact-id mirrors, expanding `external:http:*` to an id list) changed Maven's own choice in edge cases (reproduced with real Maven) and was replaced by the shadows; later rounds closed the property filling (a url written as `${…}` is filled only when nothing can outrank the pom) | every finding reproduced | done |
| Operator text | the rule for mirrors (`*`, `external:*`, explicit ids, `!id`; no `external:http:*`, no `<blocked>`) in `deploy/README.md`; RUNBOOK symptom row | read | done |
| Not covered | repositories declared in poms of dependencies or in a parent outside the project; settings profiles activated by a property, a file or the OS; Central without a mirror when it is not reachable | — | known |
| Not checked | the company cluster and its settings; Windows; a plugin newer than 6.46.1 (the integration test fails when its parsing changes) | — | open |
