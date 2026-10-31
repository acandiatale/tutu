<!-- Raw sub-agent review report (English), 2026-10-31, commit 1b30159. Consolidated Korean summary: docs/v2/PROMPT-REVIEW-2026-10-31.md. Scratchpad paths named below were session-local. -->

# Review C: are the migration notes the LLM receives accurate and complete?

Commit `1b30159` (`flow-v2`). This review only reads files; nothing in the repo was changed. Each fact was checked in one of two ways:
- **Official docs**: a URL is given.
- **The real artifacts in `~/.m2`**: `spring-boot-dependencies` 4.1.0 and its starter poms, Spring 7.0.8 jars, Security 7.1.0, Hibernate 7.4.1, Batch 6.0.4, Spring Data 4.1.0 and jakarta.servlet-api 6.1.0. These were read with `javap`, and in some cases with a small `javac`/`java` program. Those checks are marked **jar:**.

Note selection was simulated with `sbmig.fix.context.load_notes_index` / `NoteEntry.score` / `applies`, using max 4 notes and the case filter (scripts in `scratchpad/sim/`).

## 0. Context that changes how the notes should read
- **Two notes never reach the main company case.** `boot3-to-4.md` and `boot2-to-3.md` have `cases: [spring_boot, msa]`, so a **`spring_legacy`** or `servlet_tomcat` project never gets them. That is the company's main case: Framework 4/5 apps that sbmig puts on the Boot 4.1 starters. Those projects therefore never see:
  - the Jackson 3 rules;
  - the starter renames;
  - the Boot 4 package moves (which matter for `WebXmlRegistrations` and any Boot API the LLM writes).
- **What `spring_legacy` already gets from recipes.** It runs `UpgradeSpringFramework_7_0`. The jar shows this chains 5_0 → 5_1 → … → 6_2 and ends with `UpgradeJackson_2_3`, which changes `com.fasterxml.jackson` → `tools.jackson` in both code and poms. So many Framework items are already fixed by the recipe, and the notes are the fallback for what it could not resolve. Examples: WebMvcConfigurerAdapter, HandlerInterceptorAdapter, UTF8 MediaTypes, `Assert` without a message, JdbcTemplate array arguments, ResponseStatusException, ListenableFuture, Base64Utils, `-parameters`.
- **What `spring_legacy` does NOT get.** `SpringBatch5To6Migration` and `MigrateToHibernate71` have `sets: []` in `catalog.yml` and run only inside the Boot umbrella. `servlet_tomcat` does not run `RemovalsServletJakarta10`. For these projects **the notes are the only guidance** on Batch, native Hibernate and Servlet 6 removals.
- **Tests are never compiled** (`-Dmaven.test.skip=true`). Test-only guidance can only take one of the 4 note slots away from something useful.

## 1. WRONG statements (fix first)

**W1. `spring-framework-7.md:56-58`: the HttpComponents timeouts are backwards.**
- Current text: "int-millis `setConnectTimeout(int)`/`setReadTimeout(int)` overloads are deprecated/removed → `Duration` overloads (`setConnectTimeout(Duration.ofMillis(x))`); if `setReadTimeout` is missing, configure `SocketConfig`…"
- Fact (jar, spring-web 7.0.8): `HttpComponentsClientHttpRequestFactory` has **no `setConnectTimeout` at all**, in either overload. `setReadTimeout(int)`, `setReadTimeout(Duration)` and `setConnectionRequestTimeout(int|Duration)` all exist.
- The OpenRewrite recipe for this (`HttpComponentsClientHttpRequestFactoryConnectTimeout`, in the 6_2 chain) **only adds a comment**, so the compile error survives into the fix loop.
- Corrected text: "`setConnectTimeout(..)` was removed in 7.0 (both overloads) → set it on the HttpClient 5 connection manager: `PoolingHttpClientConnectionManagerBuilder.create().setDefaultConnectionConfig(ConnectionConfig.custom().setConnectTimeout(Timeout.ofMilliseconds(x)).build()).build()` → `HttpClients.custom().setConnectionManager(cm).build()` → `new HttpComponentsClientHttpRequestFactory(client)`. `setReadTimeout(int|Duration)` still compiles; keep it."
- Source: https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-7.0-Release-Notes ("The `HttpComponentsClientHttpRequestFactory#setConnectTimeout` methods have been removed").

**W2. `servlet-to-boot.md:55-58` and `:61-64`: wrong about JSPs in an executable jar, and outdated against v2.1q.**
- Current text (55-58): "only works with WAR packaging or with JSPs moved to `src/main/resources/META-INF/resources/WEB-INF/...` in an executable jar". Current text (62): "`war` → executable jar … in the company layout".
- Fact: Boot's docs say "An executable war will work when launched with `java -jar` … **JSPs are not supported when using an executable jar.**"
- sbmig itself (v2.1q, `restructurer.py:3013`) makes the boot module an executable WAR that declares **`spring-boot-starter-tomcat` + `tomcat-embed-jasper`, both `provided`**. The `-front` jar carries the JSPs under `META-INF/resources`.
- Corrected text: "JSPs need WAR packaging. sbmig makes the boot module an executable WAR (`java -jar` runs embedded Tomcat 11; `provided` jars sit in `WEB-INF/lib-provided`; the same file deploys to an external Tomcat). JSPs in a `-front` jar under `META-INF/resources/WEB-INF/jsp` are served from `WEB-INF/lib`. Never make `spring-boot-starter-tomcat` / `tomcat-embed-jasper` compile scope and never remove them. Never move JSPs to fix a build (they are not compiled)."
- Sources: https://docs.spring.io/spring-boot/reference/web/servlet.html#web.servlet.embedded-container.jsp-limitations and https://docs.spring.io/spring-boot/how-to/deployment/traditional-deployment.html (Maven sample: `spring-boot-starter-tomcat` `provided` → `lib-provided`).

**W3. `hibernate-7.md:23-25`: jboss-logging is not on the compile classpath.**
- Current text: "`org.jboss.logging.Logger.getLogger(Foo.class)` … jboss-logging stays on the classpath through hibernate-core 7".
- Fact (jar, pom): `hibernate-core` 6.5.3 / 7.2.25 / 7.4.1 declare `jboss-logging` with **`<scope>runtime</scope>`**, so project code gets `package org.jboss.logging does not exist`. Only `hibernate-validator` 9.1 brings it at compile scope.
- Corrected text: "…`org.jboss.logging.Logger.getLogger(Foo.class)`, and add `org.jboss.logging:jboss-logging` (Boot-managed, `version: null`) to the module unless `hibernate-validator` / `spring-boot-starter-validation` is already a compile dependency (hibernate-core declares it runtime-only)."
- Source: jar, `org/hibernate/orm/hibernate-core/7.4.1.Final/*.pom`; Boot 4.1.0 BOM line 1023 manages `jboss-logging`.

**W4. A leading `**` in a path pattern is now valid: `spring-security-7.md:58-59` and `spring-framework-7.md:88` are outdated.**
- Current text (security): "A literal pattern with `**` before the last segment (`/**/*.do`, `/api/**/list`) needs a `// MIGRATION-TODO:` line — PathPattern rejects it at startup". Current text (framework): "`**` only at the end of a pattern (`/api/**/x` invalid)".
- Fact (jar, verified by running `PathPatternParser.defaultInstance.parse`):
  - On 7.0.8, `/**/*.do` **parses and matches `/a/b.do`**.
  - On 7.0.8, `/api/**/x` fails with "`{*...} or ** pattern elements should be placed at the start or end of the pattern`".
  - On 6.2.12, both fail.
- Corrected text: "Spring 7 PathPattern accepts `**` / `{*x}` at the start or the end (`/**/*.do` is fine; keep it). Only a middle `**` (`/api/**/list`) is rejected → MIGRATION-TODO."
- This matters a lot for Korean `*.do` apps.
- Source: https://github.com/spring-projects/spring-framework/issues/35213 (backport https://github.com/spring-projects/spring-framework/issues/35679). The 7.0.x `PathPattern` javadoc still says "only at the end", so the jar behaviour is the authority here.

**W5. `boot40-to-41.md:22-24`: examples of "removed in 4.1" that are still present.**
- Current text: "APIs deprecated in 4.0 are REMOVED in 4.1 → … (e.g. `spring-boot-jackson2` fallbacks, old starters, `HttpMessageConverters`, `spring-boot-starter-web` alias may still exist…)".
- Fact (jar/BOM 4.1.0):
  - `spring-boot-jackson2` 4.1.0 exists, with `Jackson2AutoConfiguration` and `Jackson2ObjectMapperBuilderCustomizer`.
  - `HttpMessageConverters` exists (`@Deprecated(since="4.0")`).
  - `spring-boot-starter-web`, `-web-services`, `-oauth2-*` and `-json` are all still in the BOM.
- Corrected text: "4.1 removed what 4.0 deprecated *for removal*, plus the `layertools` jar mode. Still present in 4.1.0, deprecated: `spring-boot-jackson2`, `HttpMessageConverters`, `spring-boot-starter-web` / `-web-services` / `-oauth2-*`. Gone since 4.0: `spring-boot-starter-aop` (not in the 4.0.x / 4.1.0 BOM → `version is missing`) → `spring-boot-starter-aspectj`."
- Source: https://github.com/spring-projects/spring-boot/wiki/Spring-Boot-4.1-Release-Notes, plus jar.

**W6. `boot2-to-3.md:53`: the ant path matcher property still exists.**
- Current text: "`spring.mvc.pathmatch.matching-strategy=ant_path_matcher` removed in Boot 4."
- Fact (jar): `spring-boot-webmvc` 4.1.0 still has `WebMvcProperties.MatchingStrategy.ANT_PATH_MATCHER`. The metadata shows default `path-pattern-parser` and no deprecation.
- Corrected text: "still available in Boot 4.1 (Framework 7 deprecates `AntPathMatcher` for MVC); it only applies when Boot's MVC auto-configuration is active (not with the app's own `@EnableWebMvc` / `<mvc:annotation-driven>`)." This is a runtime matter.
- Sources: jar; https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-7.0-Release-Notes (AntPathMatcher deprecation).

**W7. `spring-framework-7.md:80-81`: versioned XSD names still resolve.**
- Current text: "use unversioned schema URLs …; old versioned xsd names may fail to resolve."
- Fact (jar): `META-INF/spring.schemas` in spring-beans/-context/-webmvc 7.0.8 maps every `spring-*-2.0.xsd` … `-4.3.xsd` to the current schema. It is also not a build error.
- Corrected text: "XML config and versioned schema names (`spring-beans-4.3.xsd`) still resolve from the jars; `<mvc:*>` is deprecated in 7.0 (warning). Never edit XML for the build; startup issues go to the Handover."
- Source: jar; the 7.0 release notes (`<mvc:*` namespace deprecated).

**W8. `era-libraries.md:81-85` (commons-collections3): describes a recipe v2 does not run.**
- Current text: "After UpgradeApacheCommonsCollections_3_4 the imports are `org.apache.commons.collections4.*` … Remaining `org.apache.commons.collections.*` imports → the collections4 class of the same name."
- Fact: `catalog.yml` gives that recipe `sets: []`, and `advisories.yml` itself says "(… not run by sbmig)". collections 3.2.2 compiles on 21.
- The current advice (3 → 4) is a library swap, which rule 14 forbids.
- Corrected text: "commons-collections 3.2.2 compiles on JDK 21; `package org.apache.commons.collections does not exist` means the jar lost its declaration → declare `commons-collections:commons-collections` (`version: null`, sbmig fills the as-is version). The move to collections4 belongs to the team (Handover)."

**W9. `ibatis2.md:32`: wrong mybatis-spring line for Spring 7.**
- Current text: "`org.mybatis:mybatis-spring` (3.0.x line or later)".
- Fact: MyBatis-Spring 3.0 targets Spring 6.x / Batch 5. **4.0 is the line for Spring 7.0+ / Batch 6**, and starter 4.0 is for Boot 4.0+. `jakarta_libraries.yml` already says 4.0/4.1.
- Corrected text: "mybatis-spring 4.x line (Spring Framework 7 / Batch 6) or the 4.x `mybatis-spring-boot-starter`; not Boot-managed: `version: null` lets sbmig / the Dependency-versions section decide."
- Sources: https://mybatis.org/spring/ and https://mybatis.org/spring-boot-starter/mybatis-spring-boot-autoconfigure/

**W10. `boot2-to-3.md:64` (low priority):** "…JUnit 4 vintage engine no longer managed by Boot" is wrong — `junit:junit` 4.13.2 and `junit-vintage-engine` (junit-bom 6.0.3) are both managed in BOM 4.1.0; delete the item (tests are skipped anyway).

**W11. `jakarta.md:83-84`: tells the LLM to change calls that still compile.**
- Current text: "`session.createQuery(hql)` → `session.createQuery(hql, Result.class)`".
- Fact (jar, Hibernate 7.4.1): the raw `Session.createQuery(String)` returning `org.hibernate.query.Query` still exists (deprecated).
- Corrected text: "when the error is only the `org.hibernate.Query` import, change the import to `org.hibernate.query.Query` and keep the call; add the result class only where javac asks."

**W12. `boot40-to-41.md:37`: wrong starter name.**
- Current text: "Starter `spring-boot-batch-data-mongo`".
- Fact (BOM 4.1.0): the artifact is `spring-boot-starter-batch-data-mongodb` (module `spring-boot-batch-data-mongodb`). The release notes use the short name.

## 2. OUTDATED or imprecise

**O1. Jackson rule 8 is stale for every Spring project.**
- `script_system_v1.md` rule 8 says "`tools.jackson.*` only when the project is already on Boot 4's Jackson 3; otherwise keep `com.fasterxml.jackson.*`".
- After the rewrite run, every `spring_legacy` and Boot project *is* on Jackson 3 wherever OpenRewrite could resolve types (UpgradeJackson_2_3 changes both imports and pom coordinates; jar).
- The "otherwise" case is therefore the exception (unresolved files, Jackson-2 library APIs), and the LLM has no rule for the mixed state. See M1 for a decision rule; rule 8 should point to it.

**O2. `servlet-to-boot.md:23-27`: the Servlet 6 removal list is incomplete.**
- Missing items (jar: jakarta.servlet-api 6.1.0 lacks them):
  - `HttpSession.getValue/putValue/removeValue/getValueNames` → `getAttribute/setAttribute/removeAttribute/getAttributeNames`;
  - `ServletRequest.getRealPath(p)` → `request.getServletContext().getRealPath(p)`;
  - the `UnavailableException(Servlet, String)` constructors → `UnavailableException(String[, int])`.
- `servlet_tomcat` projects do not run `RemovalsServletJakarta10`, so this note is their only help.

**O3. "Boot parent manages it" is true only when the parent is INHERITED (`maven-plugins.md:16,35,46-48`, `jdk21.md:94`).**
- Fact (BOM 4.1.0): the plugin versions live in `<build><pluginManagement>` of `spring-boot-dependencies`, so they apply only when it (or `spring-boot-starter-parent`) is a `<parent>`.
- The company chain imports the BOM instead (v2.1t). A `scope=import` manages no plugins and defines no `${lombok.version}` / `${maven-compiler-plugin.version}`. `-parameters` is set only by `spring-boot-starter-parent`; the Framework 6.1 recipe adds `maven.compiler.parameters`.
- Corrected text: "Plugin versions come from the parent chain's pluginManagement (see the parent-chain facts), never from the imported BOM. `${lombok.version}`-style properties exist only if the chain defines them. With maven-compiler-plugin ≥ 3.12.0, an `annotationProcessorPaths` entry may omit `<version>` and takes it from dependencyManagement (the BOM import counts)."
- Source: https://issues.apache.org/jira/browse/MCOMPILER-391 (fix version 3.12.0).

**O4. `maven-plugins.md:18-35` table: hard-coded literals that lag Boot 4.1's own pluginManagement.**
- Boot 4.1 manages: jar 3.5.0, war 3.5.1, resources 3.5.0, surefire 3.5.6, javadoc 3.12.0, enforcer 3.6.3, shade 3.6.2, assembly 3.8.0, dependency 3.10.0, antrun 3.2.0, source 3.4.0.
- This conflicts with the user rule "versions come from the parent chain / BOM facts".
- Replace the "Version" column with "JDK 21 floor" (compiler ≥ 3.13, shade ≥ 3.5.1, jacoco ≥ 0.8.11, spotbugs ≥ 4.8) plus "otherwise the parent chain's version".
- Also: maven-war-plugin ≥ 3.0 already defaults `failOnMissingWebXml=false`.

**O5. Items that still compile in 7.0 are presented as fixes (`spring-framework-7.md`).**
- Fact (jar): `JdbcTemplate.queryForObject(String, Object[], Class)` and its siblings are still in 7.0.10 (deprecated). `SimpleClientHttpRequestFactory.setConnectTimeout(int)` exists. `Environment.acceptsProfiles(String...)` compiles.
- Add: "compiles (deprecated); change only when javac reports it".

**O6. `spring-security-7.md`: two precision fixes.**
- `@EnableGlobalMethodSecurity` is still in spring-security-config 7.1.0 (deprecated; the 5.8 recipe converts it). Say "not a compile error by itself".
- Line 76: `DaoAuthenticationProvider` "setUserDetailsService deprecated/removed" → "no-arg constructor and `setUserDetailsService` are **removed** (7.0); use `new DaoAuthenticationProvider(uds)` + `setPasswordEncoder`" (jar).
- Add: "`SecurityJackson2Modules` → `SecurityJacksonModules` (Jackson 3)". Source: https://docs.spring.io/spring-security/reference/migration/index.html

**O7. `spring-framework-7.md:64-75` (Jackson section) is missing two facts.**
- The twins expose `getMapper()` (jar: `AbstractJacksonHttpMessageConverter`).
- "There is no Jackson 3.x equivalent for `Jackson2ObjectMapperBuilder`" (7.0 release notes) → `JsonMapper.builder()…build()`; the same goes for `Jackson2ObjectMapperFactoryBean`.

**O8. Test-only guidance is dead (tests are skipped).**
- Affected: `externalized-config.md` "fix-loop" (tests), `junit4-to-5.md` (whole note), and `boot3-to-4.md` "Testing".
- Their keywords fire on production errors. For example `junit4-to-5` keyword `Matchers` matches `antMatchers`.
- Restrict `junit4-to-5` to `org\.junit\.|junit:junit` keywords, or drop it from v2 selection.

**O9. Contradiction between `jdk21.md:38` and `era-libraries.md:95`.**
- jdk21.md: "pre-jakarta `javax.xml.bind:jaxb-api` 2.3 … conflicts with Boot 4: replace". era-libraries.md (jjwt): "add `javax.xml.bind:jaxb-api` 2.3.1".
- Both packages coexist. Say "replace unless a javax-only library needs it at runtime (jjwt 0.9.x)".

**O10. Literal versions as directives (user rule).**
- Examples:
  - `jdk21.md`: nashorn-core 15.4, MapStruct 1.6.3, lombok-mapstruct-binding 0.2.0, Surefire 3.5.3.
  - `era-libraries.md`: fileupload2 2.0.0-M5, velocity 2.4.1, collections4 4.5.0, jaxb-api 2.3.1, POI 5.5.1, OpenPDF 2.4.0.
  - `hibernate-7.md`: HCANN 7.0.3.Final.
  - `spring-cloud.md`: 5.0.3.
  - `jakarta.md` table: jsp-api 4.0, el-api 6.0, faces 4.1 (none of these three is Boot-managed).
- Keep compatibility facts (Lombok ≥ 1.18.30 for JDK 21; OpenPDF 3.x renames packages). Replace "use X" with "`version: null` / Dependency versions section".

**O11. `spring-framework-7.md:93-94` and `boot3-to-4.md:109-110` (retry).**
- Both list the Framework 7 retry port first. Rule 14 forbids that swap on the LLM's own initiative.
- Corrected text: "keep `org.springframework.retry:spring-retry` (`version: null`, no longer Boot-managed); the port to `core.retry` belongs to the team."

**O12. `jakarta.md` Hibernate section is incomplete.**
- `spring_legacy` gets no Hibernate recipe. Hibernate 7.4.1 (jar) has removed `@Where` (listed) and also these, which are not listed: `@WhereJoinTable`, `@Proxy`, `@LazyCollection`, `@LazyToOne`, `@IndexColumn` (→ `@OrderColumn`), `@ForeignKey` (→ `@JoinColumn(foreignKey=@jakarta.persistence.ForeignKey(..))`), `@org.hibernate.annotations.Table`, `@Loader` (→ `@SQLSelect`), `@OrderBy` (→ `@SQLOrder`), `@Sort`, `@Polymorphism`, `@SelectBeforeUpdate`, `@Persister`, `@Tuplizer`.
- `@GenericGenerator` still exists (deprecated).
- Hibernate Validator 9.1 dropped `org.hibernate.validator.constraints.NotBlank/NotEmpty/Email/SafeHtml` → `jakarta.validation.constraints.*` (no SafeHtml replacement).
- Source: jar; https://docs.jboss.org/hibernate/orm/7.0/migration-guide/migration-guide.html (unverified page fetch; facts from the jar).

**O13. `jakarta.md:23` and `servlet-to-boot.md`: JSP API advice, and no warning about javax annotations.**
- For `javax.servlet.jsp(.tagext)` in Java code, prefer `org.apache.tomcat.embed:tomcat-embed-jasper` (Boot-managed, `provided`). Its jar carries `jakarta.servlet.jsp.*` (jar). `jakarta.servlet.jsp-api` is not in the Boot BOM.
- Add a warning: "never add `javax.annotation-api` / `javax.inject` to make code compile: Spring 7 no longer supports `javax.annotation` / `javax.inject` annotations (silently ignored)". Source: 7.0 release notes.

**O14. Missing items in `boot3-to-4.md`.**
- `TomcatServletWebServerFactory` → `org.springframework.boot.tomcat.servlet` (jar) is not in the move list.
- `spring.jackson.use-jackson2-defaults` (4.0 migration guide) is not mentioned.
- The table row "`spring-boot-starter-tomcat` (provided, WAR) → `-tomcat-runtime`" matches the migration guide. But the reference doc's Maven sample and sbmig both use `spring-boot-starter-tomcat` `provided`. Add "both work; do not rename what sbmig wrote".

**O15. `boot40-to-41.md:39-42` managed-versions list.**
- Add Spring Batch 6.0.x, and that both Jackson lines are managed: `tools.jackson` 3.1.x and `com.fasterxml.jackson` 2.21.x via `jackson-2-bom` (BOM 4.1.0 lines 82-83).

**O16. Advisory `notes:` pointers lead to notes without that content.**
- `log4j1` → `boot2-to-3.md` (no log4j content, and the note is Boot-only).
- `struts2` and `egovframe` → `era-libraries.md` (no section).
- `spring-security-oauth2-legacy` → `spring-security-7.md` (nothing on `@EnableAuthorizationServer` / `OAuth2RestTemplate`).

## 3. MISSING notes or sections (only what a BUILD error needs)

**M1. `jackson-2-3.md`, "Jackson 2 vs 3 decision rule" (new note; all cases). This is the most important gap.**

When it is selected: `com.fasterxml.jackson`, `tools.jackson`, `cannot access ObjectMapper`, `MappingJackson2*`, `Jackson2ObjectMapper*`.

Content:
- **Facts:**
  - The Boot 4.1 BOM manages both Jackson 3.1.x and Jackson 2.21.x.
  - `spring-boot-starter-webmvc` → `spring-boot-starter-jackson` → `tools.jackson.core:jackson-databind` only; no Jackson 2 databind comes transitively (jar).
  - Annotations stay `com.fasterxml.jackson.annotation`.
  - The recipe run has already moved code and poms to Jackson 3 where types resolved.
- **Exact errors (javac on Spring 7.0.8 + Jackson 3 only):**
  - `cannot access ObjectMapper … class file for com.fasterxml.jackson.databind.ObjectMapper not found` when a Jackson 3 mapper is passed to `MappingJackson2HttpMessageConverter`.
  - `package com.fasterxml.jackson.databind does not exist` when a file was left on Jackson 2.
  - `new MappingJackson2HttpMessageConverter()` with no arguments still compiles (deprecated for removal).
- **Rule A (module on Jackson 3).** If the module's pom or its other files are on `tools.jackson`, finish the move in the failing file:
  - imports;
  - `JsonProcessingException` → `tools.jackson.core.JacksonException` (unchecked);
  - Spring converters/views → `JacksonJsonHttpMessageConverter(JsonMapper)` / `JacksonJsonView`;
  - `Jackson2ObjectMapperBuilder` → `JsonMapper.builder()`.
  This is not a "major change on your own": the recipe did it. Never revert the module to Jackson 2.
- **Rule B (Jackson 2 types required).** If the code must exchange Jackson 2 types with a library compiled against Jackson 2, keep that file on `com.fasterxml.*` and add `com.fasterxml.jackson.core:jackson-databind` with `version: null` (BOM 2.x) to that module. Typical cases: an in-house jar, `jjwt-jackson`, a signature naming `com.fasterxml.jackson.databind.*`. Both lines coexist on the classpath.
- **Rule C (smallest fix).** `package com.fasterxml.jackson.databind does not exist` and nothing in the module is on Jackson 3 → add Jackson 2 databind (`version: null`).
- **Rule D (spring_legacy).** Do not add `spring-boot-jackson2` to a `spring_legacy` module. It brings Boot's Jackson 2 auto-configuration, which the compile does not need. In Boot projects it is only for `org.springframework.boot.jackson2.*` types or an injected Jackson 2 `ObjectMapper` bean.
- **Runtime (Handover, not the loop).** XML beans of class `com.fasterxml…ObjectMapper` / `MappingJackson2JsonView` (eGov "jsonView") / `MappingJackson2HttpMessageConverter` / `Jackson2ObjectMapperFactoryBean` compile, but fail at startup without Jackson 2 on the classpath.
- **Sources:**
  - 4.0 migration guide "Jackson 2 Compatibility": https://github.com/spring-projects/spring-boot/wiki/Spring-Boot-4.0-Migration-Guide
  - 7.0 release notes (Jackson 2 deprecated; no Jackson 3 equivalent of the builder): https://github.com/spring-projects/spring-framework/wiki/Spring-Framework-7.0-Release-Notes

**M2. `spring-batch-6.md` (new note; all cases).**

When it is selected: `org.springframework.batch`, `JobBuilderFactory`, `ItemWriter`, … Without this note, `spring_legacy` gets no Batch recipe and no note (simulated: only the generic `spring-framework-7`). `spring5-security-batch` passes only through replayed LLM scripts.

Content:
- **Package moves** (jar, 6.0.4):
  - `org.springframework.batch.item.*` → `org.springframework.batch.infrastructure.item.*`;
  - `core.{Job,JobExecution,JobInstance}` → `core.job.*`;
  - `JobParameters(Builder)` and `launch.support.RunIdIncrementer` → `core.job.parameters.*`;
  - `core.{Step,StepExecution,StepContribution}` → `core.step.*`;
  - `Job/StepExecutionListener` and `SkipListener` → `core.listener.*`;
  - `JobExplorer` → `core.repository.explore`;
  - `repeat.*` → `infrastructure.repeat.*`.
- **Builders:** `JobBuilderFactory` / `StepBuilderFactory` are absent → `new JobBuilder(name, jobRepository)` / `new StepBuilder(name, jobRepository)` (the 1-argument constructors were removed in 6.0).
- **Chunks:** `.chunk(n)` + `.transactionManager(tm)` (the 2-argument form is deprecated); `.tasklet(t, tm)`.
- **ItemWriter:** `write(List<? extends T>)` → `write(Chunk<? extends T>)` (symptom: "does not override").
- **Removed:** `DefaultBatchConfigurer` / `BatchConfigurer` → a `DefaultBatchConfiguration` subclass or plain beans; `MapJobRepositoryFactoryBean` is gone. `SimpleJobLauncher` → `TaskExecutorJobLauncher` / `JobOperator` (`JobLauncher` is deprecated).
- **Runtime → Handover:** `@EnableBatchProcessing` is now resourceless, so JDBC metadata needs `@EnableJdbcJobRepository` or `spring-boot-starter-batch-jdbc`; the `BATCH_JOB_SEQ` rename; failed v5 jobs cannot restart.
- **Source:** https://github.com/spring-projects/spring-batch/wiki/Spring-Batch-6.0-Migration-Guide, plus jar.

**M3. New items for `spring-framework-7.md`.** All are absent in the 7.0.8 jars unless noted; they are fallbacks for what the recipes miss.
- `MediaType.APPLICATION_JSON_UTF8(_VALUE)` / `APPLICATION_PROBLEM_JSON_UTF8` → `APPLICATION_JSON(_VALUE)`.
- Removed `HttpStatus` constants (diff of 6.2.12 → 7.0.8):
  - `MOVED_TEMPORARILY` → `FOUND`;
  - `REQUEST_ENTITY_TOO_LARGE` → `CONTENT_TOO_LARGE` (`PAYLOAD_TOO_LARGE` still there);
  - `REQUEST_URI_TOO_LONG` → `URI_TOO_LONG`;
  - `METHOD_FAILURE`, `INSUFFICIENT_SPACE_ON_RESOURCE`, `DESTINATION_LOCKED`, `USE_PROXY`, `CHECKPOINT` → `HttpStatusCode.valueOf(n)`.
- `ResponseEntity.getStatusCodeValue()` → `getStatusCode().value()`. `ResponseEntity.getStatusCode()` returns `HttpStatusCode` (today this is only in Boot-only `boot2-to-3.md`).
- `org.springframework.remoting.*` is gone (HttpInvoker, RMI, Hessian, JAX-WS exporters, since 6.0). There is no successor: keep the signatures, MIGRATION-TODO, Handover.
- `OkHttp3ClientHttpRequestFactory` → `JdkClientHttpRequestFactory` / `SimpleClientHttpRequestFactory`.
- `@Required` / `RequiredAnnotationBeanPostProcessor` → delete the annotation (constructor injection later).
- Theme support (`ThemeResolver`, `ThemeChangeInterceptor`, `ResourceBundleThemeSource`) is removed → remove the beans + MIGRATION-TODO. `WebJarsResourceResolver` → `LiteWebJarsResourceResolver`.
- `Assert.notNull(x)` and the other no-message overloads are gone (6.0) → add a message argument.
- `org.springframework.util.Log4jConfigurer` → delete the call (Boot configures logging).
- `CommonsMultipartFile` → `MultipartFile`.
- `NestedIOException` → `IOException`.
- `orm.hibernate5.support.OpenSessionInViewFilter/Interceptor` → `spring.jpa.open-in-view` or `OpenEntityManagerInView*`.
- Source: 7.0 release notes ("Removed APIs": ListenableFuture, OkHttp3, Theme, WebJars locator-core), plus jar.

**M4. Spring Data 1.x → 2+ repository API (new section in `jakarta.md`, or its own note).**
- For Spring 4 projects on Spring Data JPA 1.x. No recipe covers 1.x → 2.0 (the chain starts at UpgradeSpringData_2_3; jar).
- Renames: `findOne(id)` → `findById(id).orElse(null)`, `save(Iterable)` → `saveAll`, `delete(id)` → `deleteById`, `exists` → `existsById`, `findAll(Iterable<ID>)` → `findAllById`, `delete(Iterable)` → `deleteAll`.
- `new PageRequest(..)` is now protected (jar) → `PageRequest.of`. `new Sort(..)` → `Sort.by`. `QueryDslPredicateExecutor` → `QuerydslPredicateExecutor`.

**M5. Security 3/4-era types (a section in `spring-security-7.md`).**
- The recipe chain starts at 5.7.
- `org.springframework.security.authentication.encoding.*` (Sha/Md5/PlaintextPasswordEncoder, `SaltSource`) is gone since 5.0 (jar: no package). There is no hash-compatible drop-in: port to `crypto.password.PasswordEncoder` with MIGRATION-TODO (login risk) and a `<verify>` line.
- `@EnableWebMvcSecurity` → `@EnableWebSecurity`.

**M6. New sections for `era-libraries.md`.**
- **commons-lang2:** `package org.apache.commons.lang does not exist` → declare `commons-lang:commons-lang` with `version: null`. The move to lang3 is in the Handover only; the advisory has no note today.
- **log4j1:** when the jar disappeared → declare `log4j:log4j` (as-is) or `org.slf4j:log4j-over-slf4j` (Boot-managed). `log4j.xml` is ignored at runtime (Handover).

**Not needed as build notes (they belong to the Handover):**
- **MyBatis-Spring 1.x/2.x on Spring 7.** Verified: typical `SqlSessionFactoryBean` / `SqlSessionDaoSupport` / `MapperScannerConfigurer` code compiles against Spring 7.0.8 with mybatis-spring 2.0.4. It fails at startup (`NestedIOException`), and `jakarta_libraries.yml` already carries the 4.x line.
- `@ImportResource`: XML double registration, `<context:property-placeholder>`.
- log4j configuration; Tiles/SiteMesh layout (Tiles compile errors are already covered by `era-libraries.md`; SiteMesh by `jakarta_libraries.yml`).
- Batch metadata storage; JSP taglibs.

## 4. Keyword coverage (simulated with real scoring; case `spring_legacy` unless noted)

| Error | Selected today | Should select | Keyword line to add (index.yml + front-matter) |
|---|---|---|---|
| `package com.fasterxml.jackson.databind does not exist` | none (with imports: only the `spring-framework-7` catch-all) | M1 | `"com\\.fasterxml\\.jackson\\.(databind\|core\|datatype\|module\|dataformat)"`, `"tools\\.jackson\\."` |
| `cannot access ObjectMapper … class file for com.fasterxml…` | none | M1 + fw7 | `"cannot access ObjectMapper\|class file for com\\.fasterxml\\.jackson"` |
| `tools…ObjectMapper cannot be converted to com.fasterxml…` | none | M1 | (covered by the two lines above) |
| `cannot find symbol class Jackson2ObjectMapperBuilder` | none (keyword only in Boot-only boot3-to-4) | M1 + fw7 | `"Jackson2ObjectMapperBuilder\|Jackson2ObjectMapperFactoryBean"` in fw7 |
| `JobBuilderFactory` / `org.springframework.batch.core.Job` / `ItemWriter.write` | none / generic fw7 | M2 | `"org\\.springframework\\.batch"`, `"JobBuilderFactory\|StepBuilderFactory\|\\bJobBuilder\\b\|\\bStepBuilder\\b"`, `"\\b(ItemReader\|ItemWriter\|ItemProcessor\|Tasklet\|Chunk\|StepContribution\|ChunkContext)\\b"`, `"EnableBatchProcessing\|DefaultBatchConfigurer\|BatchConfigurer\|DefaultBatchConfiguration"` |
| `APPLICATION_JSON_UTF8_VALUE` | jakarta, servlet-to-boot (import noise), fw7 (catch-all) | fw7 (M3) | `"APPLICATION_(PROBLEM_)?JSON_UTF8"` |
| `MOVED_TEMPORARILY` / `getStatusCodeValue()` | none | fw7 | `"\\b(MOVED_TEMPORARILY\|REQUEST_ENTITY_TOO_LARGE\|REQUEST_URI_TOO_LONG\|METHOD_FAILURE\|USE_PROXY)\\b"`, `"getStatusCodeValue\|getRawStatusCode\|HttpStatusCode cannot be converted to"` |
| `package org.springframework.remoting.* does not exist` | none | fw7 | `"org\\.springframework\\.remoting\|HttpInvoker\\w*\|RmiProxyFactoryBean\|RmiServiceExporter\|Hessian(Proxy\|Service)\\w*\|JaxWsPortProxyFactoryBean"` |
| `package org.springframework.util.concurrent does not exist` | none | fw7 | `"org\\.springframework\\.util\\.concurrent\|SettableListenableFuture\|ListenableFutureCallback"` |
| `class Required` / `no suitable method found for notNull(…)` | fw7 via catch-all / none | fw7 | `"annotation\\.Required\\b\|class Required\\b\|RequiredAnnotationBeanPostProcessor"`, `"no suitable method found for (notNull\|isTrue\|state\|hasText\|hasLength\|notEmpty\|isNull)\\("` |
| `OkHttp3ClientHttpRequestFactory`, Theme*, `CommonsMultipartFile`, `NestedIOException` | none / catch-all | fw7 | `"OkHttp3ClientHttpRequestFactory\|ThemeResolver\|ThemeSource\|ThemeChangeInterceptor\|WebJarsResourceResolver\|CommonsMultipartFile\|multipart\\.commons\|NestedIOException\|Log4jConfigurer"` |
| `getRealPath(String)` / `getValue(String)` on HttpSession (servlet_tomcat) | jakarta, servlet-to-boot (generic) | servlet-to-boot (O2) | `"\\b(getRealPath\|getValue\|putValue\|removeValue\|getValueNames)\\("`, `"UnavailableException\|encodeRedirectUrl\|isRequestedSessionIdFromUrl\|SingleThreadModel\|HttpUtils"` |
| `package javax.servlet.jsp.tagext does not exist` | jakarta + servlet-to-boot (OK) | same | add `"tagext\|\\b(TagSupport\|SimpleTagSupport\|BodyTagSupport)\\b"`; widen servlet-to-boot `cases` to `spring_boot, msa` (Boot 2 WARs with JSP) |
| `findOne(java.lang.Long)` / `PageRequest(...) has protected access` | none | M4 | `"\\bfindOne\\(\|\\bexists\\(\|save\\(java\\.lang\\.Iterable\|QueryDslPredicateExecutor"`, `"has protected access in org\\.springframework\\.data"` |
| `package org.springframework.security.authentication.encoding does not exist` | security-7 (generic) | security-7 (M5) | `"authentication\\.encoding\|ShaPasswordEncoder\|Md5PasswordEncoder\|SaltSource\|EnableWebMvcSecurity"` |
| `package org.apache.commons.lang does not exist` / `org.apache.log4j` | none (only import noise) | era (M6) | `"org\\.apache\\.commons\\.lang(?!3)\\b"`, `"org\\.apache\\.log4j(?!\\.core)\\b"` |
| `package org.hibernate.annotations.common…`, `org.hibernate.Criteria`, `javax.annotation.Resource`, `WebMvcConfigurerAdapter`, `antMatchers`, `AntPathRequestMatcher`, `HttpComponents…setConnectTimeout` | correct note chosen | — | — (fix the note texts per W1/W3/W4) |
| Boot class moved: `TomcatServletWebServerFactory` (spring_boot) | none | boot3-to-4 | `"TomcatServletWebServerFactory\|TomcatConnectorCustomizer\|org\\.springframework\\.boot\\.web\\.embedded"` |

Keywords that fire on unrelated text (regexes checked against samples):
- `maven-plugins.md` `release|…` matches every `…/maven-releases/` Nexus URL in a missing-artifact error → use `"--release|<release>|release version|invalid target release|maven\\.compiler\\.release"`.
- `maven-plugins.md` `…|layout` matches `com.x.layout` imports → use `"<layout>|PropertiesLauncher"`.
- `junit4-to-5.md` `assertThat|Matchers|hamcrest` matches `antMatchers` → use `"org\\.hamcrest|\\bMatchers\\.|assertThat\\("`, or drop the note (O8).
- `boot40-to-41.md` `Optional<` matches every `Optional<X> cannot be converted` (e.g. Spring Data `findById`) → use `"@ConfigurationProperties.*Optional<|ConstructorBinding"`.
- `jakarta.md` `…|Filter` matches any identifier that contains "Filter" → use `"\\bFilter(Chain|Config)?\\b"`.
- `jdk21.md` / `boot40-to-41.md` `is deprecated|has been deprecated` match javac **warnings** inside the raw block (e.g. "has been deprecated and marked for removal" on `MappingJackson2HttpMessageConverter`); keep only `-Werror` / `failOnWarning` contexts.
- `spring-framework-7.md` catch-all `org\.springframework\.(web|context|beans|core|jdbc|orm|transaction)\.` puts fw7 into nearly every Spring file. Harmless while ≤ 4 notes, but it hides the real misses during testing.

Structural recommendation: move the Boot-4 **package/starter moves** part of `boot3-to-4.md` into a note with `cases: []` (all cases), keyed on `org\.springframework\.boot\.` / `spring-boot-starter-`. Leave the Boot-3-app specifics Boot-only. Without this, `spring_legacy` and `servlet_tomcat` LLM fixes touching `WebXmlRegistrations` / `ServletComponentScan` / `ErrorPage` get no relocation facts.
Unverified (no official source found): the MapStruct ≥ 1.6 floor for JDK 21; whether querydsl-jpa 5.1.0 (jakarta) works with Hibernate 7.4; `boot3-to-4.md:125` "`server.forward-headers-strategy` ignored for WAR deployments"; the Hibernate 7 migration-guide URL in O12 (its facts come from the jar).
