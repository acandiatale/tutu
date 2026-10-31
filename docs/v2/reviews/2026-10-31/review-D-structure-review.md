<!-- Raw sub-agent review report (English), 2026-10-31, commit 1b30159. Consolidated Korean summary: docs/v2/PROMPT-REVIEW-2026-10-31.md. Scratchpad paths named below were session-local. -->

# Review D — the LLM structure review: prompt and inputs (flow-v2 @ 1b30159)

Read-only review of `sbmig/review/prompts/review_v1.md`, `sbmig/review/{reviewer,inputs,parse}.py` and the restructure
facts they could use. Severity H/M/L; "unverified" = no real-LLM run shows it, the risk comes from the code.

## Evidence base (how the prompts were obtained)
- The 2026-10-30 e2e runs left **no LLM records** (`llm.record_dir` = `${LLM_RECORD_DIR:-}`, config/default.yml:204,
  set by no fixture or `scripts/e2e.sh`); the only recorded review prompts are pre-v2.1 (`out/v2dev/rec/*/llm-0001.json`).
- So I rebuilt them: `git archive` of each run's last restructure commit + the run's configs, `profile.json`, journal
  and report notes → `collect_review_input(...).render()` with the current code. Script:
  `/private/tmp/claude-501/-Users-acandiatale-webWorkspace-migration-agent/fd3512c9-07a3-4a9d-b677-c78af4b6271a/scratchpad/render_review.py`;
  outputs: `/private/tmp/claude-501/-Users-acandiatale-webWorkspace-migration-agent/fd3512c9-07a3-4a9d-b677-c78af4b6271a/scratchpad/render/<run>/{user,system}.md`.
  Check: legacy-external-parent rebuilt at 12,601 chars vs the logged 12,836 (sbmig.log:24); the gap is the
  `boot_module` / `boot_packaging` fields, which the journal does not store.
- Runs rendered: 181553 legacy-external-parent, 181814 spring5-security-batch, 181617 msa-shared-lib, 181712
  company-boot-single, 170435 company-servlet-jsp (JSP WAR), 152216 company-spring5-web, 180536 legacy-role-parents.
  System prompt 12,403 chars; user prompt 6.6k–12.8k chars.

## H — high

**H1. The reviewer gets no version facts, so it could not catch the company's version symptoms.**
- Evidence: review_v1.md:24-25 *"explicit versions that override what the parent chain manages for the target (old
  Spring, Spring Boot, Hibernate, Jackson pins) are worth a warning"*. But nothing tells the LLM what the chain manages
  or which properties it defines. `fact_lines` (inputs.py:628-682) emits only mandatory / optional / javax / jakarta /
  duplicates, and none of the 7 rendered prompts has a parent-chain property or a managed version. The severity list
  (review_v1.md:38-42) has no version item, so even a seen `2.2.1.RELEASE` or `spring-boot.version=2.5.15` is a
  warning at most and is never edited. At the company the rule line also said "Spring Boot versions: managed by the
  parent chain" (inputs.py:491) while base-parent had no Boot BOM (v2.1s), so the LLM had no reason to doubt it.
- Add `version_fact_lines(poms, cfg, results)` in inputs.py, called from `collect_review_input` after `fact_lines`.
  It reads `sbmig.build.parent_chain.load_parent_chain_facts(cfg)` (cached per process; the restructure already loaded
  it), plus `sbmig.build.boot_bom.load_boot_bom_facts(cfg)` when `results[*].bom_imports` holds a Boot import. Every
  value comes from the facts; the prompt text holds no literal:
  ```
  - parent chain {roots}: {poms} pom(s) read; reaches spring-boot-dependencies: {yes|no}{; unreadable: …}
  - parent chain property: {name}={value} ({defining pom})          # company_properties, *.version, ≤ 20
  - explicit version on a managed artifact: `{pom}` {g:a} {v} — managed {m} by {the parent chain|the imported Boot BOM}
  - Spring Boot line: `{pom}` {g:a|plugin|parent|BOM import} {v} (target {cfg.target.spring_boot_version})
  - property shadows the parent chain: `{pom}` {name}={value} (chain {value}, {defining pom})
  ```
  Rules: resolve `${x}` through the project's own properties first. The managed set is `own_managed` (+ the Boot BOM
  when it was imported). Use the same company-property filter as `chain_company_properties` (fixloop.py:537), so the
  Boot BOM's own properties never appear as "company".
- Replace item 5 (review_v1.md:21-25) with:
  > 5. Dependencies and versions: no artifact declared twice in one pom with different versions / scopes; no javax EE
  > API jar next to its jakarta successor (facts: "javax.* API dependencies declared"). The facts list, read from the
  > parent chain's poms: explicit versions on artifacts the chain (or the BOM the restructure imported) manages, Spring
  > Boot artifacts / plugins / imports not on the target Boot line, and project properties that shadow a parent-chain
  > property. A Spring Boot entry on another line and a shadowing property are **critical**. Fix: remove the
  > `<version>` / the property; when the facts say nothing manages it, write `${<parent chain property>}`. Other
  > explicit versions on managed artifacts are warnings.
- Add to the critical list (line 40): *"a Spring Boot artifact, plugin or BOM import on another Boot line than the
  target, a project property that shadows a parent-chain property"*.
- Effect: a deterministic backstop behind v2.1o–t. The lines are normally empty (no token cost); when one is not, the
  restructure missed a case and the review fixes it before the first build instead of handing it over.

**H2. The "Spring Boot versions" rule contradicts what v2.1t writes and invites the shadowing it was built to remove.**
- Evidence: inputs.py:486-492. In `auto` mode the line reads *"… else spring-boot-dependencies {target} imported in the
  poms"*, and with explicit `false` *"spring-boot-dependencies {target} imported"*. Since v2.1t the restructure writes
  `spring-boot-dependencies:${spring-boot.version}` (INTERFACES "v2.1t As built"; `_record_bom_import`,
  restructurer.py:3555). The pom then shows `${spring-boot.version}` that no project pom defines, and the rule says
  "4.1.0". The obvious "fix" is `set_property spring-boot.version 4.1.0` or a literal: the v2.1o/t bug class.
  `parent_manages_spring_boot_effective` is never rendered (restructure_lines, inputs.py:549-570). Unverified with a
  real LLM.
- Pass `results` to `rule_lines` and emit, from the effective value:
  > - Spring Boot versions (decided by the restructure from the parent chain's poms): {the chain {roots} reaches
  > spring-boot-dependencies — no Boot BOM import and no explicit Boot version belongs in the project | the chain
  > imports no Boot BOM — the restructure imported spring-boot-dependencies in {poms of bom_imports} as {`${prop}`
  > (= {value}, defined by {pom}) | {literal} (the chain defines no Boot version property)}}. A `${…}` the
  > restructure wrote resolves through Maven inheritance: never define that property in a project pom.
- Add one sentence to "Fix rules" (review_v1.md:107-116): *"Never add a project property named like one the facts list
  as `parent chain property:`; reference it as `${name}`."*

**H3. The MSA rule line describes the wrong role assignment.**
- Evidence: inputs.py:503-506 says *"service modules named like `*-boot` whose pom is a Boot app → boot parent, other
  modules → comp parent"*, and the mandatory line (inputs.py:473-474) says *"in MSA each service root and the top
  aggregator"*. The code (`_msa`, restructurer.py:5224-5260) does this instead: every service root gets the boot
  parent whatever its name, plus the mandatory dependencies; library roots get the comp parent and "no mandatory
  dependencies"; the top aggregator gets the root parent and the mandatory dependencies only with role parents. Without
  role parents everything gets `artifacts.parent`, and the msa-shared-lib render still says *"the modules inherit the
  root pom"* although the aggregator has no parent at all. "A wrong or missing parent for a role" is critical
  (review_v1.md:39), so a literal reviewer is told to put `order-service` on comp-jar and to add the mandatory
  dependencies to `common`.
- Replacement (MSA branch of `rule_lines`, names from `profile.services`):
  > - layout (MSA): directories untouched, poms only. Service roots ({names}) → {boot parent | `artifacts.parent`} +
  > mandatory dependencies; library roots ({names}) → {comp parent | `artifacts.parent`}, no mandatory dependencies;
  > {top aggregator → root parent + mandatory dependencies | the top aggregator stays without a parent}. Module
  > names are not checked.
- Effect: removes a source of critical-but-wrong re-parenting edits in the company's MSA projects (role parents are
  configured there).

**H4. The packaging rule is stale (executable WAR) and too strict for Boot as-is / MSA.**
- Evidence: review_v1.md:16-17 *"the boot module is `war` only when the rules say so (JSPs for an external Tomcat),
  otherwise `jar`"*; inputs.py:508 *"war (JSPs served by an external Tomcat)"*; the severity list says "wrong packaging
  of the boot module" is critical. v2.1q decided the target is the executable WAR (`java -jar`, embedded Tomcat). The
  JSP render (170435) shows `spring-boot-starter-tomcat` / `tomcat-embed-jasper` with `<scope>provided</scope>` and no
  word on why. A reviewer that reads "embedded Tomcat" and sees `provided` may flip the scope. Also, Boot as-is and MSA
  `war` modules are kept on purpose ("비-boot war는 일단 그대로 두자"), but rule 3 says "otherwise jar".
- Replace item 3:
  > 3. Packaging: aggregators are `pom`. Every other packaging was decided by the restructure ("What the restructure
  > did": `boot_packaging`). In the split layout the boot module is an executable `war` when the project has JSPs or
  > a WAR overlay (Spring Boot serves JSPs only from a WAR; `java -jar` runs it on the embedded Tomcat, and the same
  > file deploys to an external Tomcat), otherwise an executable `jar`; back / front are `jar`. In such a WAR
  > `spring-boot-starter-tomcat` / `tomcat-embed-jasper` are `provided` on purpose (the repackage puts them in
  > `WEB-INF/lib-provided`): never change their scope. Boot as-is and MSA modules keep their as-is packaging (a `war`
  > service stays `war`). Report a packaging only when it contradicts this.
- inputs.py:508 becomes `"war (executable WAR: JSPs / a WAR overlay; java -jar on the embedded Tomcat, container jars
  provided)"`. Severity line 40: "wrong packaging of the boot module" → "a packaging that contradicts check 3".

**H5. The mandatory rule line tells the LLM that every mandatory artifact is a BOM import.**
- Evidence: inputs.py:475-476 appends *"type pom + scope import → in dependencyManagement"* unconditionally. The
  restructure uses `<dependencyManagement>` only for `scope: import` specs (`_mandatory_managed`,
  restructurer.py:3405-3411). In all 7 renders the clause sits next to two plain starters declared in
  `<dependencies>`, and the facts print them as "`pom.xml` (managed)". The company's `company-core` /
  `company-logging` (artifacts.example.yml) have no type / scope. "A missing mandatory dependency" is critical.
  Unverified, but the reading "they must be BOM imports" is the literal one.
- Write per spec: `_spec(s)` + (`" → <dependencyManagement> (scope import)"` if `s.scope == "import"` else
  `" → <dependencies>"`). Add to item 4:
  > The modules not inheriting the root's declarations (role parents) is the company rule: never move the mandatory
  > dependencies into the modules, and do not report it.

## M — medium

**M1. The user prompt's last instruction asks for a script, not edits.**
inputs.py:405-408 says *"… only for critical issues you can fix in the build files — one `<script>`."* The system prompt
asks for `<analysis>/<solution>/<edits>`. A model that follows the last instruction answers script-only. That takes the
v2 path (`_run_script`), which has no edit `desc`s. Replace it with *"… — `<analysis>`, `<solution>` and `<edits>`."*

**M2. The review's `<script>` is stored but never reused; it is about 30% of the system prompt.**
The answer's script goes into ONE KB case, `review|structure` (orchestrator.py:1626-1636, entries.py:121), and nothing
ever reads that case back (grep: no reader in sbmig/). The script-only text is roughly 3.7k of the 12.4k system chars:
the "Script — optional" paragraph (lines 100-101), the example (68-74), "Script rules" + helper API (118-127, 3.1k), and
the `<changes>` requirement. The model also pays output tokens for the script and `<changes>`. `helper_api_doc()` even
repeats the import rules with a different list than lines 121-123, and the header "(`p.errors` is empty here)" points at
an API the review cannot use. Proposal (the user decides; PLAN-v2.1 line 34 says "+ 스크립트 저장"): edits-only review.
Drop those sections; trim `RETRY_MESSAGE` / `EDITS_RETRY_MESSAGE` / `APPLY_RETRY_MESSAGE` (reviewer.py:57-68); keep
`_run_script` only for old answers. Effect: about 900 fewer input tokens per project (×500), fewer output tokens, one
answer shape.

**M3. Restructure notes are rendered twice.**
`restructure_lines` prints each result's `notes` (inputs.py:570), then the orchestrator's `step.id: <note>` copies as
"run notes" (orchestrator.py:1384, inputs.py:581-586). For legacy-external-parent that is 6 notes ×2 (about 1.1k chars).
The optional-trigger fact appears 3 times (structured list, note, run note). Pass only the summary line as run notes
(orchestrator.py:1405), or drop `("notes", "notes")`. Render the missing fields at the same time:
`parent_manages_spring_boot_effective` (H2), `non_filtered_extensions`, `jsp_jakarta_files/refs`.

**M4. Nothing re-checks the restructure's invariants after the review's edits.**
`_apply` (reviewer.py:409-453) checks sources and pom parsing only. The loop's `_fill_versions` (fixloop.py:3092;
v2.1q: drop a literal version the chain manages) and the restructure's `_protected_keys` (restructurer.py:3947) /
`_drop_bom_property_overrides` ran before the review and never again. A shadowing property with the same value builds
fine, so the build gate will not notice. Add a post-apply check before "not committed yet", reading the changed poms
against the chain facts. Refuse (restore + `APPLY_RETRY_MESSAGE` with the reason) an edit that:
- adds or sets a property the chain defines;
- adds an explicit version to a chain/BOM-managed artifact;
- changes the version of a protected key away from the spec / `${prop}`;
- sets a `<parent>` that is not in `artifacts.parent_specs()` and not a project pom.

This is not an oscillation risk (the review runs once, the restructure does not re-run); it is a one-way undo of the
deterministic work.

**M5. The optional-artifact rule is stale since v2.1n / v2.1t.**
Item 4 says *"optional in-house artifacts that are used are on the configured version"*, and inputs.py:478 says *"only
where already used, version policy `floor`"*. But `floor` keeps a newer as-is version, v2.1t writes `${<artifactId>.version}`
when the chain defines it (legacy-role-parents render: `lombok 1.18.44→${lombok.version}`), and `when_present` ADDS an
artifact and may remove its trigger (`replaces`). Item 4 replacement:
> An optional in-house artifact is correct on its configured version, on a newer one (`floor` never downgrades) or on
> `${<property>}` of the parent chain. It may have been added where its trigger is declared, and the trigger removed.

In `_spec`, print `when_present` / `replaces` / `property_name`.

**M6. The restructure's version decisions must read as final.**
spring5-security-batch: *"explicit versions dropped: spring-batch-core: 4.3.10 → managed 6.0.4"*, a major jump.
Meanwhile the fix rules (review_v1.md:111-113) forbid raising majors and prefer "a release of the same major". The reviewer
is invited to re-pin 4.3.10. Add after line 46:
> What "What the restructure did" lists (versions dropped because the parent chain manages them, versions pinned from
> the as-is build or the target Boot BOM, properties removed, BOM imports, in-house artifacts added) is final. Never
> re-add, re-pin or re-literalize it. A major jump there comes from the target line and belongs in a warning for the
> owners, not in an edit.

**M7. Encoding check: a shortcut that is wrong under role parents, and no re-check after the edits.**
`_encoding_check` treats a module as "named" when ANY encoding issue sits on `""` or `"pom.xml"`
(reviewer.py:358-361). With role parents the modules do not inherit the root (`_chain` stops at the external parent,
inputs.py:233-254), so a root-level `set_property` fixes nothing and no `(sbmig check)` issue is added: silently garbled
Korean literals. Fix: accept the root only when it is in the module's in-project chain, and recompute
`source_encodings` after `_apply`. Item 7 should also say *"with role parents set it in the module pom"*. Better (user
decides): the restructure writes `project.build.sourceEncoding` itself. It is 100% computable (as v2.1m did for
filtering), and item 7 then shrinks to one line.

**M8. Starters: say what the boot pom got, and forbid adding more.**
The reviewer is never told that the boot module gets `layout.starter_by_case[case]` (plan note only: "starter
'spring-boot-starter-webmvc'"). Adding `-security` / `-batch` / `-data-jpa` turns on auto-configuration (a default user
plus secured endpoints, Batch jobs started at launch). The as-is XML / @Configuration does not need them. Add to
`rule_lines`: *"the boot module depends on {back}{, front} and `{starter}` (`layout.starter_by_case.{case}`)"*. Add to
"Do not report": *"Boot starters for features the as-is code configures itself (Security, Batch, JPA …)"*. Missing
classes are the fix loop's job.

**M9. OpenRewrite changes container APIs from `provided` to `compile`.**
In the spring5-security-batch project, `git show a1fd9af -- pom.xml` turns `javax.servlet-api 4.0.1 provided` into
`jakarta.servlet-api 6.0.0 <scope>compile</scope>`. The same shows in the company-servlet-jsp and msa-shared-lib renders
(the as-is fixtures are `provided`). The replays flag it as a warning only. Add a fact line from `profile.poms`: *"scope
changed by the rewrite: `{pom}` {g:a} {as-is scope} → {scope}"* (warning, no edit). Better (outside the review; user
decides): the restructure restores the as-is scope of servlet / jsp / el / websocket APIs.

**M10. `spring-boot-maven-plugin` in a module other modules depend on (Boot as-is / MSA libraries).**
`repackage` moves the classes under `BOOT-INF/`, and dependents fail with "package does not exist", a cryptic case for
the fix loop. `_fix_boot_plugin` (restructurer.py:3678) only fixes versions. Add a fact line from
`PomInfo.has_spring_boot_plugin` plus the reactor dependents. Critical: remove the plugin from that pom. Unverified:
whether `has_spring_boot_plugin` excludes pluginManagement, and how often this occurs at the company.

## L — low

- L1 Remove the `<verify>` paragraph (review_v1.md:103-105, −300 chars): it forbids major raises, then explains
  what to do after one; sbmig writes the guide itself (orchestrator.py:1662-1668). L2 Drop item 6's "plugins that
  cannot run on the target JDK": no fact supports it, the build shows it, `plugin_incompatible` has a policy.
- L3 Item 2 asks for artifactIds that follow the configured names, but `rule_lines` never gives them. The e2e has
  `extparent-comp` in `extparent-back`. Add *"artifactIds `<root>{boot_artifact_suffix}` / `<root>{comp_artifact_suffix}`
  / `<root>-front` (split layout only; Boot as-is / MSA keep theirs)"*.
- L4 e2e-config-only contradictions: without role parents the split text says "parent = boot / comp parent"
  (inputs.py:510-515); MSA prints "modules live under `module/`" (inputs.py:524). L6 docs/INTERFACES.md:1297-1308
  still says "+ `<script>`". L7 a non-mandatory root `<dependencies>` entry under role parents reaches no module
  (fact + warning). L8 the javax list in item 5's prose duplicates the facts line.
- L5 The review input is not kept in the run: write `<run dir>/logs/review-input.md` (no secrets). The replays are
  canned and stale (the spring5-security-batch replay warns *"jakarta.servlet-api:5.0.0 is pinned"*; the pom has no
  version), so the e2e cannot see a prompt regression: add an offline test rendering `collect_review_input` on 2-3
  fixture trees and asserting the H1–H5 lines.

## Proposed checks NOT to add (decisions on the brief's list)

| candidate | decision | why |
|---|---|---|
| explicit `spring-*` / `spring-boot-*` with a version where the chain manages them | ADD (H1), critical for the Boot line, else warning | the company's mixed-version symptom; needs the chain facts |
| as-is `*.version` properties shadowing the chain | ADD (H1), critical | `spring-boot.version=2.5.15`; needs `property_sources` |
| `spring-webmvc` / `spring-context` without a version next to a starter | do not report | managed, same version, harmless |
| `javax.servlet-api` / `jsp-api` / `jstl` 1.2 left | keep critical when the jakarta successor is declared | company-servlet-jsp: `javax.servlet:jstl:1.2` (back) next to the jakarta JSTL (boot); both carry TLDs for the same URI (runtime risk) |
| `maven-war-plugin` / `failOnMissingWebXml` leftovers | warning at most (item 6) | inert in jar modules; a real failure shows in the build |
| `spring-boot-maven-plugin` on the wrong module | ADD (M10), critical | silent `BOOT-INF` classes break dependents |
| `spring-test` / junit in `compile` | do not report | tests are skipped; project_lines already says "warnings at most" |
| log4j 1 + logback | do not report | runtime; `knowledge/libraries/advisories.yml:167` already puts it in the Handover |
| boot module missing a web / JPA / security / batch starter | forbid (M8) | auto-configuration changes runtime behaviour; missing classes go to the fix loop |
| root `<dependencies>` left after the split | warning fact (L7) | with role parents it reaches no module |
| `<repositories>` to dead hosts | do not report | reachability is not visible in the poms; resolution errors are infra / fix-loop cases |
| `project.build.sourceEncoding` for EUC-KR | covered; fix M7 | inputs.py:50-56 maps euc_kr→`EUC-KR`, `_FAMILIES` treats cp949 / euc_kr alike (91-101) |

## Budget and flow
At most 3 calls (reviewer.py:26-28, 290-326, 455-479) is adequate. No loop with the restructure: it runs once, before the
review. The risk is a one-way undo of its invariants (M4). `_dirty` (reviewer.py:399) correctly refuses edits on an
uncommitted restructure.

## Order of work (smallest first)
1. Text-only fixes in inputs.py / review_v1.md: M1, H5, H4, H3, M3. 2. Facts: H1 + H2, plus the M5 / M6 / M8 wording.
3. Guards and decisions: M4, M7, M2 (the user decides), L5.
