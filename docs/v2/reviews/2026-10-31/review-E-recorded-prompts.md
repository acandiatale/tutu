<!-- Raw sub-agent review report (English), 2026-10-31, commit 1b30159. Consolidated Korean summary: docs/v2/PROMPT-REVIEW-2026-10-31.md. Scratchpad paths named below were session-local. -->

# Review E: recorded prompts and Handover, spring_legacy + servlet e2e fixtures (flow-v2 @ 1b30159)

## How the evidence was obtained

- No run dir has LLM records (`llm.record_dir` = `${LLM_RECORD_DIR:-}`, no `.env`, no file with `"messages"`); the
  "10 records" are the replay answers only. I re-ran the 14 fixtures with an extra config (`llm.record_dir` + `kb.dir`)
  into `…/scratchpad/regen/{rec,runs,kb}/<fixture>/`; nothing was written to the repo (`find -newer`, `git status`).
- The spring5-security-batch prompts are **byte-identical** to yesterday's: all 8/8 `prompt_hash` values match
  `run-20261030-181814-995cd8/journal.jsonl`.
- Pre-18:00 runs (v2.1n era) were regenerated at HEAD (today's prompts; SBMIG-RESULT identical except `resolved=`).
  "rec X/llm-000N" = `…/scratchpad/regen/rec/X/llm-000N.json`.

## 1. Per run: what reached the LLM

| fixture (yesterday's run) | LLM calls | cases → calls (mode) | answer kinds | final build |
|---|---|---|---|---|
| spring5-security-batch (995cd8) | 9 | 27 cases → 8 fix calls, all `new` (72ccc463b0 security-access, 868c33643d / 1b37194675 Batch, 26271fddc9 BASE64, 8e787ba7f1 AntPath, 59bc06d915 PathMatch, c4ff664861 HttpHeaders, 6954585468 follow-on IOException); 19 cases resolved as side effects | fix: `<analysis>`+`<script>` only (no `<edits>`/`<solution>`/`<changes>`); review: `<review>` (1 warning) | ok |
| legacy-ibatis (ac4317) | 10 | 3 sibling cases (70cf5d17c7, 4b636ce784, f67d8b305c), **3 × `new` each = 9 calls** | `<analysis>` only (decline), 9 times | **FAIL**, BLOCKED repeated_failure ×3 |
| euckr-legacy (c67cc7) | 1 | review only | `<review>` critical + `<script>` (no edits) | ok |
| legacy-external-parent, spring5-xml, company-spring5-web, legacy-external-config, legacy-role-parents, servlet-webxml-legacy, servlet-war-overlay, servlet-nonstandard-dirs, company-servlet-jsp, ant-webapp, servlet-tomcat | 1 each | review only (fix loop needed no LLM) | `<review>` (ok or warnings) | ok |

Cases that needed more than one call:

- **spring5-security-batch, TokenCodec.** Script 26271fddc9 replaced the decoder but left `catch (IOException e)`. The
  next build then raised a new case, 6954585468 ("IOException is never thrown"), which cost one more LLM call.
  The first prompt already said so (jdk21.md § fix-loop, "drop the `IOException` from `throws`/catch only if nothing
  else throws it"); the replay ignored it, and the follow-on prompt does not name the script that caused it (L9).
- **legacy-ibatis.** Every call is the same decline ("needs the owning team's decisions … no script"); asking again
  cannot change it, and the two siblings are the same root cause in CatalogDao.java (M1).

## H — high

**H1. The "Dependency versions" section tells the LLM that Spring 7 has no jakarta build and to answer `<no_script>`, in
API-removal cases.**
- Evidence:
  - rec spring5-security-batch/llm-0007, case setUseSuffixPatternMatch:
    `- org.springframework:spring-webmvc — current 7.0.8; same major (7.x) in the repository, newer than current: 7.0.9, 7.0.10`
    and `- org.springframework:spring-webmvc → verdict: no jakarta build is known on any line — stay on 7.x, say so in
    <analysis> and answer <no_script> if the case cannot be fixed without it`.
  - llm-0008 says the same for spring-web.
- This contradicts the rest of the same prompt:
  - rule 14: "Never refuse a fix, answer `<no_script>` … only because a version is unknown";
  - rule 3: prefer the managed version;
  - v2.1o, which drops exactly that recipe-raised 7.0.10 ("recipe-raised version dropped: spring-webmvc 7.0.10 …
    managed 7.0.8", review prompt).
- Cause:
  - `FixLoop._case_libraries` attaches the owner artifact of any compile error.
  - `repo_versions.pom_is_jakarta` returns `None`, because Spring's published poms declare no `jakarta.*` dependency.
  - `_verdict_line` then reaches `if rv.same_major or rv.higher:` and turns "unknown" into "no jakarta build".
- Proposal: in `_verdict_line`, return `""` unless some candidate has `jakarta is False`, and:
  - In `_library_hints`, when `self._versions().managed_version(key, module)` is set, emit
    `new parent chain: manages org.springframework:spring-webmvc at 7.0.8 — the API changed; fix the code, keep the
    managed version`. Drop the "newer than current" listing for that artifact.
  - List repository releases only when the case's errors name `javax.*` or a dependency resolution.
- Effect: removes a false refusal path from every Spring / Security API-removal prompt.

**H2. Fix prompts carry no version facts for compile errors, and there is no Spring Batch note.**
- Evidence on version facts:
  - Across all 17 fix prompts, not one `as-is build:` / `new parent chain:` / `parent chain property:` line appears
    (grep over rec/*/).
  - `_version_facts` only reads `e.artifact`, which compile errors do not have.
  - The review prompt does have the fact: "org.springframework.batch:spring-batch-core: 4.3.10 → managed 6.0.4". The
    fix prompts only say `libs=…spring-batch:4.3.10 … | target: JDK 21, Spring Boot 4.1.0`.
- Evidence on the missing note: `index.yml` has no spring-batch entry; llm-0003 (`package
  org.springframework.batch.item.support does not exist`) carried **zero** notes, llm-0004 (JobBuilderFactory) only the
  Framework 7 note; the fixture comment names Batch recipes ("SpringBatch5To6Migration …") v2 does not run.
- The replay worked only because its author knew the answer: "Spring Batch 6 package moves (verified against
  spring-batch-core / -infrastructure 6.0.4)". A real LLM is likely to guess Batch 5 imports or add a dependency, and
  then need an augment.
- Proposal:
  - Let `_version_facts` also cover the GAVs `_case_libraries` located, e.g.
    `as-is build: org.springframework.batch:spring-batch-core 4.3.10; new parent chain: manages … at 6.0.4 (major 4 → 6)`.
  - Add `knowledge/notes/spring-batch-6.md` (keywords `org\.springframework\.batch`,
    `JobBuilderFactory|StepBuilderFactory|EnableBatchProcessing`, cases spring_legacy / spring_boot) with:
    - the package moves the e2e build verified: `core.Job` → `core.job.Job`, `core.Step` → `core.step.Step`,
      `item.` / `repeat.` → `infrastructure.item.` / `infrastructure.repeat.`;
    - the builder API: `new JobBuilder(name, jobRepository)`;
    - no version numbers.

**H3. A KB script is not idempotent.**
- I ran case 1b37194675 `fix_v1.py` on the already-fixed BatchConfig.java (FakeP harness,
  `…/scratchpad/idem/idem.py`). Its second run writes
  `new JobBuilder("cleanupJob", jobRepository, jobRepository)` and
  `new StepBuilder("cleanupStep", jobRepository, jobRepository)`.
- The regex `new (Job|Step)Builder\(([^()]*)\)` runs unconditionally, which breaks rule 2.
- The other 6 stored scripts are idempotent (same test).
- Nothing in sbmig checks idempotency: grep `idempot` over `sbmig/script`, `fixloop.py` and `fixer.py` finds nothing.
- This version is reused by case id on the next project (for example a half-ported Batch 5 config with
  `new StepBuilder(name, jobRepository)` that still imports `core.Step`). Result: a new compile error → reverted →
  LLM augment.
- Proposal:
  - In `FixLoop` right after `res.status == "ok"`, run the script a second time on the result. Any `changed_files`
    marks the stored version `non_idempotent`: kept but never reused, and shown as "introduced: second run changed X"
    in the next prompt. Fix the replay with `if "jobRepository" in m.group(2): return m.group(0)`.

**H4. The JNDI Handover advice is wrong for XML configs.**
- In legacy-external-config the Handover says "JNDI DataSource 는 사라집니다: spring.datasource.url/username/password/
  driver-class-name 을 설정하세요".
- But `<jee:jndi-lookup id="dataSource" jndi-name="java:comp/env/jdbc/catalogDS"/>` stays in
  `module/boot-jar/src/main/resources/legacy-config/applicationContext.xml`, which `@ImportResource` imports.
- Embedded Tomcat runs without JNDI naming, so this bean's startup lookup fails whatever `spring.datasource.*` says.
  This is unverified at runtime; `JndiObjectFactoryBean` looks up on startup by default.
- Proposal:
  - Wherever the restructure writes `spring.datasource.*` placeholders for a `jee:jndi-lookup` / `JndiObjectFactoryBean`
    in an imported XML, add `<!-- MIGRATION-TODO: embedded Tomcat has no JNDI; remove this bean so the
    spring.datasource.* DataSource is used -->` next to the element.
  - Add the Handover line "remove `<jee:jndi-lookup id="dataSource">` from legacy-config/applicationContext.xml once
    spring.datasource.* are set".

**H5. Stale v1-handler text reaches the LLM and the owners.**
- spring-security-7.md:64 says "`add-known-dependency` adds it (Boot-managed through spring-security-bom)". It sits in
  rec spring5-security-batch/llm-0002 (the very case that needs the dependency) and in llm-0006. That handler only
  exists in this note.
- The `jdk-removed-apis` advisory (advisories.yml:255-270) says "the handler jdk-base64-shim (round 10) points at
  generated drop-in classes (sbmig.compat.*…)" and "now the generated sbmig.compat shims, which keep the 76-character
  line breaks".
  - This text appears in llm-0005 under "Library migration guidance" (cut mid-word: `jdk.unsuppo…`).
  - It also appears in the Handover (row "jdk-removed-apis" and follow-up 1).
  - In fact the final TokenCodec.java uses `Base64.getEncoder().encodeToString(...)`: the line breaks are gone and no
    `MIGRATION-TODO` was added (rule 9; the replay script 26271fddc9 also breaks rule 9).
- advisories.yml:739 says "handler querydsl-jakarta-classifier".
- Proposal:
  - Replace the security note text with "declare `org.springframework.security:spring-security-access` without a
    version (the Security BOM of the parent chain manages it)".
  - In the jdk-removed-apis summary, drop the shim sentence. Make the followup: "sun.misc.BASE64Encoder →
    java.util.Base64.getEncoder() drops the 76-char line breaks the JDK 8 encoder inserted; use getMimeEncoder() where
    stored or compared output must stay identical".
  - Add a `tests/` check that greps `knowledge/` for the deleted v1 handler ids.

## M — medium

**M1. legacy-ibatis: 9 LLM calls for one root cause.**
- The 2nd and 3rd prompts add "Previous attempts …: `- attempt verify-1-1 (iteration 1): script new → outcome:
  apply_failed`". The analysis is cut at 300 characters with no marker ("…instead of the sqlmap files and the ";
  `context.summarize_script_attempt`, `a.analysis.strip()[:300]`).
- Rule 13 "Do something different" then pushes the model toward a blind port, against rule 11.
- The siblings get no guidance: the log shows `4b636ce784 … (0 advisory hint(s))` while 70cf5d17c7 has one.
- The Blocked table says only "3 fix attempt(s) did not resolve it; needs a human".
- CLAUDE.md says "up to 4 LLM calls"; the run made 9.
- Proposal: an answer with no edits and no script → outcome `declined` (not `apply_failed`) → BLOCKED `needs_owner`
  after the first decline (knob `script.max_declines_per_case: 1`); siblings whose files lie inside a declined case's
  files are deferred, then BLOCKED "same cause as 70cf5d17c7" with no call; `summarize_script_attempt` prints
  `apply_errors[0]` and uses `textwrap.shorten(..., 300, placeholder=" …")`; the Blocked "suggestion" column shows the
  analysis's first sentence.
- Effect: 9 calls → 1.

**M2. The fix prompt never says the project was converted to Boot.**
- The system prompt says: "OpenRewrite ran once (Java 21, javax → jakarta and, for Spring Boot projects, the Spring Boot
  4.0 / 4.1 recipes)". For spring_legacy that is wrong (it ran Framework 7 + Security 6.0…7.0), and it implies the
  project stays non-Boot.
- Plan notes give only "layout split into module/comp-jar, module/boot-jar, starter 'spring-boot-starter-webmvc'".
- Missing:
  - `module/boot-jar/src/main/java/com/example/ops/Application.java` (`@SpringBootApplication`, package
    `com.example.ops`);
  - that auto-configuration is now active;
  - the module roles;
  - the `@ImportResource` list.
- The `@EnableWebMvc` in MvcConfig is visible in llm-0007, but nothing says what it does under Boot.
- Proposal:
  - Add a "conversion:" fact line in `FixContext` built from `RestructureResult`:
    `conversion: Spring Framework app (no Boot) → Spring Boot 4.1 app; entry point <path> (@SpringBootApplication,
    scans <pkg>); XML via @ImportResource(<list>); boot module <rel> (Application + application.properties only),
    comp module <rel> (all as-is sources)`.
  - In script_system_v1.md, replace the recipe clause with "the recipes listed in Plan notes".

**M3. `@EnableWebMvc` is not covered anywhere.**
- grep for `EnableWebMvc` in `knowledge/` and `sbmig/` finds nothing.
- The spring5-security-batch as-is MvcConfig.java has `@EnableWebMvc`. Under Boot it switches off
  `WebMvcAutoConfiguration`: `spring.mvc.*` / `spring.web.resources.*` are ignored and classpath:/static is not served.
- The Handover says nothing about it.
- Proposal: the analyzer flags `@EnableWebMvc` / `extends WebMvcConfigurationSupport` (spring_legacy / servlet_tomcat)
  → restructure follow-up "<file>: @EnableWebMvc disables Spring Boot's MVC auto-configuration — keep it only if the
  as-is MVC setup is complete"; the same line in spring-framework-7.md.

**M4. Contradictions inside the review prompt; stale external-Tomcat wording.**
- Review system rule 3 (review_v1.md:16): "the boot module is `war` only when the rules say so (JSPs for an external
  Tomcat)".
- The user prompt's company rules (`review/inputs.py:508`): "packaging war (JSPs served by an external Tomcat)".
- The same prompt's follow-up: "boot module packaged as an executable WAR (java -jar runs the embedded Tomcat; an
  external Tomcat works too)" (rec company-servlet-jsp).
- The generated `shop-boot/.../Application.java` says "Packaged as a WAR for an external Tomcat"
  (`restructure/templates/Application.java.j2:26`).
- servlet-to-boot.md (in fix prompts, e.g. llm-0006):
  - "`war` → executable jar … in the company layout";
  - "JSPs … only work with WAR packaging or with JSPs moved to … META-INF/resources … in an executable jar".
  - CLAUDE.md says JSPs cannot be served from an executable jar.
- `inputs.py:405-408` "Your task: … one `<review>` JSON block and — only for critical issues … — one `<script>`"
  contradicts the system prompt, where `<edits>` are the fix and the script is optional.
- Proposal: rule 3 → "`war` when the project has JSPs (an executable WAR run with `java -jar`; the same file deploys
  to an external Tomcat), otherwise `jar`"; `inputs.py:508` → `"war (executable WAR: JSPs, java -jar)"`; template →
  "Executable WAR: `java -jar` runs the embedded Tomcat; SpringBootServletInitializer serves an external deployment";
  task line → "… and, for critical issues you can fix in the build files, `<analysis>`, `<solution>`, `<edits>`".
  Same for `config/default.yml:44`, `restructurer.py:1434`, servlet-to-boot.md §Packaging / §JSP.

**M5. Stale review replays put false warnings into the Handover (4 of 13 runs). e2e stays green.**
- spring5-security-batch: "jakarta.servlet-api:5.0.0 is pinned with scope compile". 5.0.0 appears nowhere in the
  prompt, and the pom has no version. Handover follow-up 3 repeats it.
- company-spring5-web, legacy-external-config, legacy-ibatis: "property spring.version=… is left". The same prompts
  say "version properties removed: … spring.version=…", and the shown poms contain no `<spring.version>`
  (`…/scratchpad/regen/revcheck.py`).
- `expected.json` checks only counts / regex, so nothing fails.
- Proposal:
  - Post-check in `sbmig.review.parse`: version literals and `property X` names in `problem` / `fix_hint` must occur
    in the shown poms or facts. If not, prefix `(sbmig check) not in the material` and keep the issue out of the
    Handover.
  - Regenerate those four replay/llm-001 files.
  - Add `issues_not_contain` assertions.

**M6. `jakarta.servlet-api` provided → compile, never restored.**
- In 8 of the 10 web fixtures the as-is javax servlet-api was `provided`. The migrated comp module has `compile`.
- spring5-security-batch rewrite commit a1fd9af shows
  `- <scope>provided</scope>` / `+ <version>6.0.0</version><scope>compile</scope>`.
- The review flagged it in only 3 runs (warning); the other runs pass it on silently.
- Proposal: the restructure's pom cleanup restores the as-is scope of `jakarta.servlet:jakarta.servlet-api` /
  `jakarta.servlet.jsp:jakarta.servlet.jsp-api` when the javax predecessor was `provided` (as-is pom fact, no version),
  with an error-list row "scope restored: provided (as-is)".

**M7. Literal versions in prompt text clash with rule 3 / 14 (`version: null`, `${property}`).**
- jakarta.md table: `jakarta.servlet:jakarta.servlet-api:6.1.0`, `jakarta.persistence-api:3.2.0`,
  `jakarta.annotation-api:3.0.0`, `querydsl … (5.1.0)`.
- jdk21.md: `nashorn-core:15.4`, "maven-compiler-plugin ≥ 3.13 (3.14.0 / Boot-managed)", while the recipe wrote
  3.16.0 (Handover).
- ibatis guidance "mybatis 3.5.19 + mybatis-spring 4.1.0 (Spring 7 line …)" against ibatis2.md "mybatis-spring (3.0.x
  line or later)", in the same prompt (rec legacy-ibatis/llm-0002).
- Note headers "Boot 4.1 = Security 7.1 / Hibernate 7.4 / Framework 7.0.x".
- script_system_v1.md:2 hard-codes "JDK 21 + Jakarta EE 11 + Spring Boot 4.1 (Spring Framework 7, …)", although
  `cfg.target` exists (the review renders it from config).
- No secrets, keys, `.env` content or absolute local paths appear in any of the 31 records (grep).
- Proposal: artifact column → "managed by the parent chain (omit `<version>`)"; unmanaged → "`version: null` (sbmig
  fills the newest release)"; system header from `cfg.target`; numbers only in the people's tables (libraries.yml).

**M8. Handover wording: wrong, noisy, duplicated.**
- Wrong text in spring5-xml #7, company-spring5-web #4, legacy-external-config #7: "web.xml … not read by embedded
  Tomcat; servlets/filters are registered via @ServletComponentScan — verify mappings" (`restructurer.py:2510`).
  `@ServletComponentScan` never reads web.xml.
  - → "web.xml entries are either provided by Boot (DispatcherServlet, CharacterEncodingFilter, ContextLoaderListener:
    the XML contexts are imported into one Boot context) or listed above".
- Noise: "no InternalResourceViewResolver prefix / suffix found … set spring.mvc.view.prefix" (`restructurer.py:3121`)
  appears in 5 raw-servlet fixtures that have 0 `@Controller`. Emit it only when spring-webmvc is present.
- Duplicates: servlet-webxml-legacy #1 / #10; legacy-external-config #2 ("4 settings") / #9 ("6 keys");
  company-servlet-jsp #1 / #8; ant-webapp #2 / #4 plus the Ant section (three paths for example-util-1.0.jar).
- legacy-ibatis: "Review every ported DAO" while nothing was ported (BLOCKED). When the library outcome is "still
  present", use the advisory's summary instead of the followup.
- In spring5-xml / legacy-external-config the root context (ContextLoaderListener) and the servlet context (dispatcher)
  are merged into one context, and `<mvc:annotation-driven/>` sits next to Boot's MVC auto-configuration. Only "verify
  bean overlaps" is said. Add one explicit sentence (runtime effect unverified).

**M9. A non-web spring_legacy app becomes a web server.**
- legacy-ibatis is a CLI: Main.java runs `new ClassPathXmlApplicationContext("applicationContext.xml")`.
- `starter_by_case.spring_legacy: spring-boot-starter-webmvc` (default.yml:84) adds embedded Tomcat.
- The XML exists twice: `module/comp-jar/src/main/resources/applicationContext.xml` and
  `module/boot-jar/…/legacy-config/applicationContext.xml`.
- The Handover only says "Main … not invoked".
- Proposal: the webmvc starter only with web evidence (spring-webmvc / servlet-api / web.xml), otherwise
  `spring-boot-starter`; plus a follow-up naming both XML copies.

**M10. A per-server properties file is silently skipped (legacy-external-config).**
- `<context:property-placeholder location="classpath:catalog.properties,file:${catalina.base}/conf/catalog-local.properties"
  ignore-resource-not-found="true"/>` is kept.
- Under `java -jar` the file is not loaded; its keys are missing silently (unverified; `catalina.base` is unset when
  the placeholders are resolved).
- "Properties to set" lists only `mail.host`.
- Proposal: an inventory row for the location: "per-server file `file:${catalina.base}/conf/catalog-local.properties`
  — pass it with `--spring.config.additional-location=file:<path>` or `-Dcatalina.base=<dir>`".

## L — low

- **L1.** Every review prompt (14/14) repeats all restructure "notes" again under "run notes" (0.5–2.2 KB, about 10%;
  `review/inputs.py:583`). Print run notes only when they are not already in `notes`.
- **L2.** Notes are 50–71% of the fix user prompt.
  - jakarta.md (8.7 KB) is in the Security case llm-0002 only because `jakarta\.` matched the pom.
  - servlet-to-boot.md (5.8 KB) is in the AntPathRequestMatcher case llm-0006 only because of `HttpServletRequest`.
  - Fix: match keywords on errors, symbols and the error files' imports, not on pom text.
- **L3.** The review script is stored under the generic case `review|structure` (8528874739). It hard-codes
  `module/comp-jar/pom.xml` and MS949, and nothing ever reuses it (`orchestrator._review_entries`). Drop `<script>`
  from review_v1.md, or key it per issue type.
- **L4.** The replays break the answer contract: all 9 fix replays plus the review lack `<edits>` / `<solution>` /
  `<changes>`, so the KB records "[sbmig] the answer did not describe its changes". spring5-security-batch replay
  llm-005 (df607b0899…) is never used.
- **L5.** Fixture sources leak answers or stale names into real prompts:
  - UrlAccessConfig "(add-known-dependency adds it)";
  - TokenCodec "the handler jdk-base64-shim…";
  - MvcConfig "project recipe com.company.migrate.Spring7RemovedApis".
- **L6.** Script 8e787ba7f1 converts DB-loaded patterns (`load("/admin/**", …)`) to PathPattern without the
  `MIGRATION-TODO` the note requires: `/**/x` is rejected at startup (rule 9).
- **L7.** The ibatis Plan note gives the as-is path "xml bean configs: src/main/resources/applicationContext.xml".
  After the move, `<need_files>` with that path would miss. Print post-restructure paths.
- **L8.** Review rule 5 against company rules: spring5-xml warns "slf4j-api is pinned at 2.0.17 although the parent
  manages slf4j", but that is the configured optional floor. Add "except optional in-house artifacts on their
  configured version".
- **L9.** The follow-on prompt (6954585468) does not say that script 26271fddc9 v1 introduced the error. Add
  "introduced by: script:26271fddc9:v1 (round 2)" and prefer an augment of that script, so the KB holds one script for
  the pattern instead of two in sequence.
- Outside this scope: legacy-role-parents ends with lombok 1.18.38 as the dependency but 1.18.44 in
  `annotationProcessorPaths`. The review prompt says both "optional bumped: managed→1.18.38" and "kept newer as-is
  version 1.18.44".

Fixture coverage gaps: none of the 14 fixtures contains log4j 1 configs, `WebApplicationInitializer`,
`CommonsMultipartResolver`, Sitemesh / Tiles or a `spring.profiles.active` context-param (grep). The Handover can
therefore not be judged for those patterns; consider adding one fixture that has them.
