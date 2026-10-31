<!-- Raw sub-agent review report (English), 2026-10-31, commit 1b30159. Consolidated Korean summary: docs/v2/PROMPT-REVIEW-2026-10-31.md. Scratchpad paths named below were session-local. -->

# Review A — the Spring Framework → Spring Boot conversion (`spring_legacy`, `servlet_tomcat`)

Commit `1b30159` (flow-v2), read-only. Sources: restructurer / webxml / analyzer / notes; e2e run dirs under `out/e2e/`;
Boot facts checked in the local 4.1.0 jars (`AutoConfiguration.imports`, `spring-configuration-metadata.json`, starter poms).
Three offline probes in the scratchpad ran the real analyzer + `Restructurer.apply` on synthetic company-shaped trees
(no Maven, no app start): `scratchpad/probe/probe.py` (Spring 5 web app: web.xml + root/servlet XML + WEB-INF config,
JNDI, `*.do`, EUC-KR filter, jsp-config), `probe2.py` (placeholder locations), `probe3.py` (EUC-KR XML bean config).
"reasoned" = follows from Spring/Boot behaviour, not observed (sbmig never starts the app).

**Bottom line.** About 4 of the 32 patterns below can break the BUILD, and the notes mostly cover them. Nearly all the
risk is at **startup**, and neither the build gate nor the LLM sees startup. Most H items come from **the conversion
itself**: Boot's defaults are not the Spring Framework defaults the as-is app ran with. sbmig creates these problems, so
sbmig should prevent them or report them precisely. They are not as-is bugs that belong to the owners. Several have a
deterministic, low-risk fix (§2b).

## 1. Patterns and what the conversion does to them (ordered by severity)

| # | as-is pattern | B/R | sbmig today (evidence) | gap · sev |
|---|---|---|---|---|
| 1 | Root `ContextLoaderListener` + `DispatcherServlet` child context (two `contextConfigLocation` XMLs) | R | `_generate_application` (restructurer.py:2461-2479) copies **every** file of `xml_bean_configs` into `legacy-config/` and `@ImportResource`s all of them into ONE context. `xml_bean_configs` = any `*.xml` with the beans ns, src/test included (source_scanner.py:553-564). The only follow-up is "verify bean overlaps". | Boot sets `allow-bean-definition-overriding=false`. Three ways to a BeanDefinitionOverrideException at start: (a) same id in root and servlet XML (probe: `messageSource`); (b) a file that is `@ImportResource`d and also `<import>`ed by another one (probe: `context-security.xml`); (c) `<mvc:annotation-driven/>` infrastructure beans (`mvcContentNegotiationManager`…) against Boot's `EnableWebMvcConfiguration` @Beans (reasoned). Test XML is imported into prod (probe: `legacy-config/test-context.xml`). **H** |
| 2 | Field-injection cycles between services (legal on Spring Framework) | R | nothing (grep: no `allow-circular-references` in sbmig/knowledge/config) | Boot ≥2.6 forbids circular references → startup fails. **H** |
| 3 | EUC-KR Spring XML (`<?xml … encoding="EUC-KR"?>`, Korean comments/values) | R | restructurer.py:2472 `c.write(…, c.ws.read(cur))` → a **new** file is written as UTF-8, and its declaration still says EUC-KR | probe3: the copy fails strict EUC-KR decoding and decodes as UTF-8; the comp original is intact → parse error or garbled values at start. **H** |
| 4 | `CharacterEncodingFilter` (encoding EUC-KR, forceEncoding) | R | Skipped (web_xml_rules.yml:13). The follow-up names `server.servlet.encoding.charset / force`. | In Boot 4.1 `server.servlet.encoding.*` is deprecated at level *error* since 4.0.0 → `spring.servlet.encoding.*` (metadata of spring-boot-web-server / spring-boot-servlet 4.1.0). Boot's default is UTF-8 → Korean form posts come out garbled. The init-params are parsed, but no value is written. **H** |
| 5 | JNDI DataSource (`<jee:jndi-lookup>`, `JndiObjectFactoryBean`, `META-INF/context.xml`) | R | Writes `spring.datasource.*=__SET_ME__` (externalized.yml:7-10; Handover of run-20261030-145445-f8d896). The XML bean stays in `legacy-config/`. | Embedded Tomcat runs with naming disabled → the lookup fails at start. `DataSourceAutoConfiguration` exists only in `spring-boot-jdbc` 4.1.0, which `spring-boot-starter-webmvc` does not bring (boot pom of run-20261030-181814-995cd8) → the placeholders bind to nothing. **H** |
| 6 | Config under `WEB-INF` (`property-placeholder location="/WEB-INF/config/x.properties"`, `/WEB-INF/*.xml` imports, MyBatis `configLocation`) | R | `_populate_front` moves WEB-INF *.properties/*.xml to boot `asis-webinf/` "not read by Boot" (restructurer.py:2841). The analyzer counts only `src/*/resources` files as definitions (properties.py:755-780). | probe: `Globals.DbType` (defined in WEB-INF/config/globals.properties) is reported `external` and gets `__SET_ME__`. At start the location is not found: a BFPP runs before the embedded ServletContext exists, and the file is not there anyway. **H** |
| 7 | `${…}` inside a `classpath:` location (`classpath:config/${spring.profiles.active}/app.properties`) | R | properties.py:433-437 records it as an external file route | probe2: `app.mode` is reported `external` although packaged → `app.mode=__SET_ME__` in application.properties. That value **wins** over the file, because PropertySourcesPlaceholderConfigurer reads the Environment first. Wrong config, no error. **H** |
| 8 | `<mvc:default-servlet-handler/>` / `configureDefaultServletHandling().enable()` | R | nothing (it is in fixture company-spring5-web; Handover of run-20261030-152216-8467a9 is silent) | Boot ≥2.4 does not register Tomcat's default servlet (`server.servlet.register-default-servlet`, in 4.1 metadata) → "Unable to locate the default servlet" at start (reasoned). **H** |
| 9 | `CommonsMultipartResolver` bean in XML | R | Nothing for XML (Java config: B, era-libraries.md:40-49). XML is not compiled, so the fix loop never sees it. | Spring 6 deleted the class → CannotLoadBeanClassException at start, unless a recipe rewrote it (unverified). Boot's 1MB default replaces the as-is `maxUploadSize`. **H** |
| 10 | `@EnableWebMvc` / `extends WebMvcConfigurationSupport` | R | Not detected (TRACKED_ANNOTATIONS, source_scanner.py:36). `_jsp_view_props` (restructurer.py:3107-3138) still writes `spring.mvc.view.*`. Fixture spring5-security-batch `MvcConfig` has it. | It turns Boot's MVC auto-configuration off, so these are ignored: `spring.mvc.view.*`, the front module's `classpath:/static`, the index.html welcome page, `spring.mvc.*`. The follow-up "update or remove the as-is resolver bean" (:3129-3132) would leave NO view resolver. **H** |
| 11 | Static files served from servlet-context locations (`<mvc:resources location="/resources/">`, `addResourceLocations("/static/")`) | R | Files go to front `src/main/resources/static/` (= `classpath:/static`, not a ServletContext path) | The as-is handler finds nothing → 404 for css/js/images. Certain under #10. Otherwise it depends on handler-mapping order (unverified). **H/M** |
| 12 | `DelegatingFilterProxy` (security and others) | R | **Every** DelegatingFilterProxy is skipped as "springSecurityFilterChain is registered by Spring Security auto-configuration" (web_xml_rules.yml:14). probe: `xssFilter` (targetBeanName `xssEscapeFilter`, `*.do`) is dropped with that reason. | `SecurityFilterAutoConfiguration` exists only in `spring-boot-security` (4.1.0 jar). The raw spring-security deps do not bring it, and run 995cd8 has no starter-security. Filter beans are then auto-registered as plain filters on `/*`, REQUEST only, at the lowest order. The as-is mapping and dispatchers are lost, and security now runs after the WebXmlRegistrations filters (orders 1..n). **M** (H if a filter reads SecurityContext) |
| 13 | `OpenEntityManagerInViewFilter` | R | Skipped as "Boot's OpenEntityManagerInViewInterceptor" (web_xml_rules.yml:17) | `spring.jpa.open-in-view` and the interceptor live in `spring-boot-jpa` → LazyInitializationException in JSPs. **M** |
| 14 | DispatcherServlet on `*.do` / `/app/*`; its init-params; `<multipart-config>`; two DispatcherServlets | R | Follow-up "set spring.mvc.servlet.path" (webxml_registrations.py:252-255). The skipped servlet's `<multipart-config>` is dropped (multipart handling sits after `skip()`, :248 vs :274). | `spring.mvc.servlet.path` takes a path prefix, not `*.do`. Spring ≤5.2 suffix matching (`/x` matched `/x.do`) is gone. The upload limit falls back to Boot's 1MB. Two servlet XMLs end up in one context (#1). **M** |
| 15 | web.xml `<jsp-config>` (`jsp-property-group` page-encoding / include-prelude / el-ignored; `<taglib>` aliases) | R | webxml.py:282 only counts taglibs; no follow-up | Boot's Tomcat reads no web.xml. EUC-KR pages without a page directive decode as ISO-8859-1, prelude taglibs are missing (tags print raw), aliased URIs do not resolve. **M** (H for EUC-KR) |
| 16 | `WebApplicationInitializer`, `Abstract*DispatcherServletInitializer`, `AbstractSecurityWebApplicationInitializer` | R | A note line only (servlet-to-boot.md:52). `_initializer_followup` (:3038) checks only SpringBootServletInitializer. | `java -jar` never calls them, so their filters / servlets / multipart settings are lost. On an external Tomcat they run next to the generated SpringBootServletInitializer → a second root context or a duplicate springSecurityFilterChain. **M** |
| 17 | WAR context root (`/app.war` → `/app`) | R | Context-path follow-up only for EAR roots (:1369-1383). A plain WAR gets only "WAR packaging converted…" (:2659). | URLs move from `/<war>/…` to `/`. **M** |
| 18 | Log4j 2 (`log4j2.xml`, `log4j-core`, `log4j-slf4j(2)-impl`) | R | `log4j2*.xml` moves to boot (config.py `boot_resources_glob`). The library is detected (libraries.py:106), but there is no advisory. | Logback (starter-logging) stays → log4j2.xml is ignored. log4j-slf4j2-impl next to Logback / log4j-to-slf4j can stop the start (unverified). **M** |
| 19 | Spring Security XML namespace (`<http>`, `<intercept-url>`, `<authentication-manager>`) | R | Imported as it is. The recipes are Java-only, and spring-security-7.md covers only the Java DSL. | Security 7 parses it at start: `/**/x.do` patterns (PathPattern), `access="ROLE_X"`, versioned XSDs, CSRF on (details unverified). **M** |
| 20 | `<tx:annotation-driven/>` / `<aop:*>` per context | R | nothing | One context: aspects of the servlet XML now also apply to services, proxies are CGLIB (AopAutoConfiguration). If `spring-boot-transaction` is on the classpath (starter-jdbc / company starters), the XML `internalTransactionAdvisor` collides with Boot's `@EnableTransactionManagement` (reasoned). **M/L** |
| 21 | `<context:component-scan>` vs `@SpringBootApplication` | R | Application in `base_package` (source_scanner.py:152: common prefix ≥2 segments, else the most frequent top 2); the XML scans are kept | Union of both: packages outside the base survive only through the XML. Classes the narrow as-is scans skipped are now instantiated too. **M/L** |
| 22 | Spring Batch XML jobs / `@EnableBatchProcessing` / psvm launcher | R | "existing main class(es) … not invoked" (:2484-2487); job XMLs imported eagerly (#1) | No Boot batch module and nothing launches the jobs. **L/M** |
| 23 | `WebMvcConfigurerAdapter`, `HandlerInterceptorAdapter`, `<mvc:interceptors>` | B | Recipes (spring5-xml `expected.json` post_checks); MappedInterceptor beans apply to every mapping | ok. **L** |
| 24 | session-timeout, error-page, welcome-file, `spring.profiles.*` context-param | R | `_session_timeout_prop` (:2628), ErrorPageRegistrar, welcome follow-up, `spring.profiles.active=dev` written (probe) | ok. **L** |
| 25 | `META-INF/context.xml` left in comp | R | Follow-up "declare the jars as Maven dependencies / move the classes" (:2933) | Wrong advice for a Tomcat context file. **L** |
| 26 | web.xml with no generated registrations | R | `_web_followups` says "registered via @ServletComponentScan" (:2494, :2507) | Wrong when the Application has no such annotation (spring5-xml run be58bd: follow-up 7 vs its `Application.java`). **L** |
| 27 | `javax.servlet` provided, explicit spring-* next to starters, spring-test in main | B | jakarta recipe, managed-version drop (run be58bd version table) | ok. **L** |
| 28 | JSP taglib URIs, tag files, `*.tld` | R | Moved to `META-INF/resources/WEB-INF/…`; JSTL 3 serves the old URIs | ok (tagdir from a resource jar unverified). **L** |
| 29 | Tiles / SiteMesh / Velocity views | B/R | advisories.yml:355-369, era-libraries.md, jakarta_libraries.yml:40-52 | covered. **L** |
| 30 | MyBatis `SqlSessionFactoryBean` + mapper XML | R | classpath mappers stay in comp; mybatis-spring floor in jakarta_libraries.yml:154-160 | ok (WEB-INF locations → #6). **L** |
| 31 | Quartz / `<task:scheduled>` | R | work in the merged context | ok (JNDI JobStore → #5). **L** |
| 32 | Existing `main` classes | R | follow-up (:2484) | ok. **L** |

## 2. What to add

### 2a BUILD-breaking gaps: what the fix-loop LLM needs

**B1. Third-party javax classes in the generated `WebXmlRegistrations`** (Lucy XSS / Kaptcha / UrlRewriteFilter-style
filters and servlets; which ones lack a jakarta build is unverified). These fail with
`incompatible types: X cannot be converted to jakarta.servlet.Filter`. servlet-to-boot.md:37-38 tells the LLM to "fix
the class (or its library), not the generated file", so a javax-only library loops until BLOCKED. Add to
servlet-to-boot.md (the second bullet needs the user's call; it conflicts with fix-policy "commenting code out"):
```
## fix-loop: generated WebXmlRegistrations
- `incompatible types: <X> cannot be converted to jakarta.servlet.Filter|Servlet|EventListener` or `cannot infer type
  arguments for FilterRegistrationBean<>` in WebXmlRegistrations.java: <X> comes from a library still built on
  javax.servlet. Fix the pom: switch to the library's jakarta build or jakarta classifier named in the repository
  facts / KB; never guess a version.
- If the facts show no jakarta build: WebXmlRegistrations.java is generated by sbmig from web.xml. Replace only that
  @Bean method by `// MIGRATION-TODO web.xml <filter|servlet> <name> (<class>, <url patterns>): javax-only library`.
  Leave every other bean and the library untouched.
```
`index.yml` keywords: `WebXmlRegistrations`, `cannot be converted to jakarta\.servlet\.(Filter|Servlet|http\.HttpServlet)`,
`(Filter|Servlet|ServletListener)RegistrationBean<`.

**B2. `CommonsMultipartResolver` in Java config.** spring-framework-7.md:35 says only "→ StandardServletMultipartResolver",
and that class has no size setters, so the LLM drops the limits. Replace with:
```
- `CommonsMultipartResolver` (removed in 6.0) → delete the bean; Boot registers StandardServletMultipartResolver.
  Move setMaxUploadSize / setMaxUploadSizePerFile / setMaxInMemorySize to spring.servlet.multipart.max-request-size /
  max-file-size / file-size-threshold in the boot module's src/main/resources/application.properties (Boot's default
  is 1MB per file). setDefaultEncoding → spring.servlet.encoding.charset.
```

**B3. Spring 4.x code.** The company has Spring 4 projects, but spring-framework-7.md covers only "5 → 6 → 7". Add
`## Spring 4 → 5 removals`:
- `AbstractExcelView` / `AbstractJExcelView` → `AbstractXlsView` (`buildExcelDocument(Map, Workbook, req, res)`).
- `org.springframework.remoting.*` (HttpInvoker / RMI / Hessian / `JaxWsPortProxyFactoryBean`, removed in 6.0) → keep
  the interface and add `// MIGRATION-TODO remoting`.
- `JdbcTemplate.queryForInt/queryForLong` and `SimpleJdbcTemplate` (absent in 7) → `queryForObject(sql, Integer.class, args…)` / `JdbcTemplate`.

Keywords: `AbstractExcelView|AbstractJExcelView|org\.springframework\.remoting|HttpInvoker|HessianServiceExporter|RmiServiceExporter|queryForInt|queryForLong|SimpleJdbcTemplate`.

**B4. `boot3-to-4.md` is limited to `cases: [spring_boot, msa]`** (index.yml:112-114). A converted legacy project is
a Boot 4 app too, and the Boot 4 relocations (`ErrorPage`, `ServletComponentScan`, `ConfigurableServletWebServerFactory`,
`MultipartConfigFactory`) are what sbmig-generated code and LLM-written customizers hit. Add `spring_legacy` and
`servlet_tomcat` to its cases, or copy its "Moved but NOT by the 4.0 umbrella" bullet into servlet-to-boot.md.

### 2b RUNTIME gaps: a deterministic fix where the risk is low, otherwise the Handover sentence (hook)

- **D1, #1/#2, low risk.** Hook: `_generate_application` → `_append_props`. Property list in YAML (e.g.
  `knowledge/config/legacy_context.yml`), switch `layout.legacy_context_defaults`. Both values only restore what the
  as-is Spring Framework context did. Write when XML is imported (or always for legacy cases):
  `spring.main.allow-bean-definition-overriding=true` and `spring.main.allow-circular-references=true`. Also list the
  duplicate `id`/`name`s across the imported XML (static regex). Handover: *"Spring Boot refuses duplicate bean names
  and circular references that the as-is Spring context accepted; sbmig set spring.main.allow-bean-definition-overriding=true
  and spring.main.allow-circular-references=true in {boot}/src/main/resources/application.properties so the merged
  context starts as before. Duplicate bean ids across the imported XML: {ids}. Remove the two lines once they are resolved."*
- **D2, #1, low-medium risk.** Hook: `_generate_application`. Import only the **entry** XMLs: the web.xml
  `contextConfigLocation` values (the context-param plus each DispatcherServlet init-param, wildcards matched against the
  moved files). Without web.xml, import the roots of the `<import>` graph. Never import `src/test/`. Name in the
  follow-up which files were left out and why (reached through `<import>`, or test-only).
- **D3, #3, trivial.** Copy bytes: replace restructurer.py:2472 with `c.copy(cur, dst)` (`_Ctx.copy`, :332).
- **D4, #4/#13/#14, low risk.** Hook: `_web_xml_registrations`, next to `_session_timeout_prop`. Rows of
  web_xml_rules.yml gain `set:` templates fed from the parsed init-params:
  - CharacterEncodingFilter → `spring.servlet.encoding.charset={init:encoding}`, `spring.servlet.encoding.force={init:forceEncoding}`
    (`force-request` / `force-response` for the split params);
  - HiddenHttpMethodFilter → `spring.mvc.hiddenmethod.filter.enabled=true`; FormContentFilter → `spring.mvc.formcontent.filter.enabled=true`;
  - the skipped DispatcherServlet's `<multipart-config>` → `spring.servlet.multipart.location|max-file-size|max-request-size|file-size-threshold`.

  Rows that rest on a Boot module gain `requires:` (DelegatingFilterProxy → spring-boot-security, OEMIV → spring-boot-jpa).
  When that module is missing, emit a follow-up instead of the note. For OEMIV: *"OpenEntityManagerInViewFilter was not
  registered: Boot's open-in-view support exists only with spring-boot-starter-data-jpa (module spring-boot-jpa); add
  it or register the filter in WebXmlRegistrations to keep lazy loading in JSPs."*
- **D5, #5, Handover (deterministic variant behind a flag, medium risk).** Hook: `_placeholder_followup`, using the ref's
  file:line. *"JNDI DataSource {name}: the bean at {legacy-config copy}:{line} fails at startup — embedded Tomcat has no
  JNDI. Add org.springframework.boot:spring-boot-starter-jdbc to {boot} (spring.datasource.* binds only with
  spring-boot-jdbc on the classpath), delete that bean (or keep its id with <alias name="dataSource" alias="{id}"/>) and
  set the spring.datasource.* values above."* The flag variant applies exactly this to the sbmig-owned copy.
- **D6, #6, low risk for the copies.** In `legacy-config/` copies, rewrite `/WEB-INF/<p>` locations whose file went to
  the stash to `classpath:asis-webinf/<p>`. In the analyzer, count a WEB-INF `*.properties` named by a location as a
  definition. For Java, Handover: *"{file}:{line} reads /WEB-INF/{p}; it now lives at classpath:asis-webinf/{p} in
  {boot} — use that location (an embedded container has no ServletContext while property sources load)."*
- **D7, #7, low risk.** properties.py:433-437: when a `classpath:` / `classpath*:` location contains `${…}`, put `*` in
  its place and append the result to `classpath_locations`. The glob match at properties.py:776 already handles `*`.
- **D8, #8, low risk.** When an imported XML has `<mvc:default-servlet-handler` or a comp source calls
  `configureDefaultServletHandling`, write `server.servlet.register-default-servlet=true`. Handover: *"Spring Boot does
  not register Tomcat's default servlet; sbmig set server.servlet.register-default-servlet=true for
  <mvc:default-servlet-handler/> in {file}."*
- **D9, #9, low-medium risk.** In the copy, remove the `CommonsMultipartResolver` bean and write
  `spring.servlet.multipart.max-request-size={maxUploadSize}`, `max-file-size={maxUploadSizePerFile|maxUploadSize}`,
  `file-size-threshold={maxInMemorySize}`. Handover: *"CommonsMultipartResolver bean {id} ({file}) removed — Spring 6
  deleted the class; Boot's multipart support replaces it with the limits written to spring.servlet.multipart.*."*
  If commons-fileupload is used but there is no such bean: *"Boot limits uploads to 1MB per file by default; set
  spring.servlet.multipart.* to the as-is limit."*
- **D10, #10/#11, Handover.** Hooks: `_jsp_view_props`, `_web_followups`; regex over comp sources:
  `@EnableWebMvc|extends\s+[\w.]*WebMvcConfigurationSupport`. *"{class} carries @EnableWebMvc: Spring Boot's MVC
  auto-configuration is off, so spring.mvc.view.* written by sbmig, the front module's classpath:/static files, the
  index.html welcome page and spring.mvc.* are ignored. Point its own view resolver / resource handlers at the moved
  locations (JSPs {mount}, static classpath:/static/), or remove @EnableWebMvc."* Do not write `spring.mvc.view.*` in
  this case. **Deterministic (copies only):** rewrite the resolver `prefix` in a `legacy-config/` XML to the mapped
  prefix. Append `classpath:/static{loc}` to each `<mvc:resources location="{loc}">` whose files the front move emptied.
  For Java, give the file:line and the new literal.
- **D11, #12, low-medium risk.** Skip DelegatingFilterProxy only when filter-name / `targetBeanName` is
  `springSecurityFilterChain` and the boot pom has spring-boot-starter-security. Otherwise generate
  `new DelegatingFilterProxyRegistrationBean("{target}")` with the web.xml patterns, dispatchers and order. Boot's
  adapter then skips the target bean (unverified detail).
- **D12, Handover only.** Each sentence below names its hook:
  - #14 extension mapping (replaces webxml_registrations.py:252-255): *"DispatcherServlet {name} was mapped to {*.do};
    Boot's serves '/'. Mappings that already end in .do keep working; mappings without it relied on suffix matching,
    which Spring 6+ removed. spring.mvc.servlet.path takes a path prefix only."*
  - #15 (`_web_xml_registrations`, after parsing `jsp-property-group` / `taglib`): *"web.xml <jsp-config> ({groups}:
    page-encoding {enc}, include-prelude {files}; {n} taglib alias(es)) is not applied — Spring Boot's Tomcat reads no
    web.xml; put the encoding / taglib directives in the JSPs."* A generated JspConfigDescriptor customizer would work,
    but it uses Tomcat-internal API: medium risk.
  - #16 (`_generate_application`, regex `implements\s+WebApplicationInitializer|extends\s+Abstract\w*(DispatcherServlet|SecurityWebApplication|ContextLoader)Initializer`):
    *"{class} is a WebApplicationInitializer: java -jar never calls it — move its filters / servlets / multipart
    settings to WebXmlRegistrations; on an external Tomcat it would run next to the generated
    SpringBootServletInitializer and create a second context, so delete it after porting."*
  - #17 (`_packaging_followups`, as-is `war`): *"The as-is WAR was served under its deployment context path
    (/{finalName or artifactId} unless deployed as ROOT); java -jar serves at '/' — set server.servlet.context-path in
    {boot}/src/main/resources/application.properties if the URLs must stay."*
  - #18 is advisory YAML `log4j2-boot-logging` (guide), keyed on `org.apache.logging.log4j:log4j-core|log4j-slf4j-impl|log4j-slf4j2-impl`:
    *"Spring Boot logs through Logback: log4j2.xml in {boot} is ignored — exclude spring-boot-starter-logging and add
    spring-boot-starter-log4j2 to keep Log4j 2."*
  - #19 (`_generate_application`, an imported XML with `schema/security`): *"{file} configures Spring Security in
    XML; recipes migrate only Java config — check it against Security 7 at first start (intercept-url patterns with **
    in the middle, access="ROLE_X", versioned spring-security-*.xsd, CSRF on by default)."*
  - #20/#21 (same hook): *"Root and DispatcherServlet contexts are one Boot context now: aspects and
    <tx:annotation-driven/> of either XML apply to every bean, proxies are class-based, and @SpringBootApplication
    also scans all of {base_package} besides the XML component-scans."*
  - #25 (`_populate_front`, `left`): *"META-INF/context.xml ({path}) is Tomcat configuration and is not read by
    embedded Tomcat — its <Resource>/<Environment> entries are listed under 'Properties to set'."*

### 2c One mechanism for most of 2b
Put the D1/D4/D6/D8/D9/D10/D12 detections in one YAML table, `knowledge/config/legacy_context_rules.yml` (invariant 2).
Each row: `match` regex over the imported XML or comp sources, `set` properties, `rewrite` (copies only), and a
`followup` template with `{file}:{line}`. One `_legacy_context_lint(c, boot, imports)` runs it at the end of
`_generate_application`. Add a post-BUILD-SUCCESS **XML bean class check**: resolve every `class="…"` in the imported
copies against the boot module's runtime classpath, and report misses as "fails at startup". That catches #9, Tiles 3,
remoting, Velocity and `orm.hibernate3/4` in XML. Optionally feed the misses to the fix loop as cases; notes already
exist for them.

**Design question for the user.** `_populate_front` moves `WEB-INF/views/x.jsp` to `WEB-INF/jsp/views/x.jsp`. That
changes every view prefix, `jsp:include`, forward, error page and Tiles path (follow-ups at restructurer.py:2927-2930, :3129).
Keeping the WEB-INF-relative path under `META-INF/resources/WEB-INF/` (as v2.1g already does for non-JSP files) would
remove that whole class of breakage, if the company rule "JSPs under WEB-INF/jsp" allows it.

## 3. Stale or wrong statements
- `templates/Application.java.j2:26` says "Packaged as a WAR for an external Tomcat". Since v2.1q it is an executable
  WAR (java -jar), and external deployment is the alternative.
- `config/default.yml:44` and `sbmig/core/config.py:195` say "the boot module is a WAR for external Tomcat". The same
  wording is in restructurer.py:26 (module docstring), the `_populate_front` docstring, the :2904-2905 comment, and the
  `_war_deps` docstring (:3006 "provided(외부 Tomcat 이 제공)").
- `servlet-to-boot.md`:
  - :55-58 says JSPs work "with JSPs moved to META-INF/resources … in an executable jar". Boot does not serve JSPs from
    an executable jar; sbmig's own follow-up says so (restructurer.py:2325-2327).
  - :57-58 "do not move JSPs" predates 9c; the restructure moves them.
  - :59 says the properties "replace" an XML resolver bean. They only do when no InternalResourceViewResolver bean
    exists (Boot's `defaultViewResolver` is @ConditionalOnMissingBean) and there is no @EnableWebMvc.
  - :62 "`war` → executable jar … in the company layout" is wrong for the JSP case (executable WAR).
  - :63 `spring-boot-starter-tomcat-runtime` provided does not match `_war_deps`, which uses `spring-boot-starter-tomcat`
    (boot3-to-4.md table has the same mismatch).
  - :64 "forward-headers ignored for WAR" holds only for external deployment.
- `web_xml_rules.yml`:
  - :13 property `server.servlet.encoding.charset / force` → `spring.servlet.encoding.*` in Boot 4.
  - :14 the DelegatingFilterProxy reason is wrong for non-security targets, and is true only with spring-boot-security.
  - :17 the OEMIV reason needs spring-boot-jpa.
- webxml_registrations.py:252-255: `spring.mvc.servlet.path` cannot express `*.do`.
- restructurer.py:2507: the "@ServletComponentScan" claim uses the config flag (`scan`, :2494) instead of `_servlet_scan(c)`.
- restructurer.py:3131-3132 "takes precedence over Boot's view resolver": Boot's resolver is never created. The
  generated properties comment cites the stash copy that `_dedupe_webinf_stash` deletes (probe: `asis-webinf/…/dispatcher-servlet.xml`).
- restructurer.py:2933: wrong advice for `META-INF/context.xml`.
- `externalized.yml:53` "JNDI DataSource 는 사라집니다: spring.datasource.* 을 설정하세요" leaves out the XML bean and
  the spring-boot-jdbc module.
- `externalized-config.md` title "on an executable boot-jar". `era-libraries.md` hard-codes versions (`2.0.0-M5`,
  velocity `2.4.1`, openpdf `2.4.0`, "SLF4J 2.0.18"), against the "never hard-code versions in note text" rule.
- `spring-framework-7.md` title "5 → 6 → 7": the company also has Spring 4 (B3).
- `out/e2e/run-20261030-150311-90aa4e/report.md` (servlet-webxml-legacy) still prints "packaged as WAR for external
  Tomcat". It predates v2.1q; rerun it before sharing outputs.

## Summary (top findings)
1. H: all XML into one Boot context (`@ImportResource` of every beans file, test XML included) + Boot's overriding=false → duplicate-id / double-import / `<mvc:annotation-driven/>` collisions crash startup. Fix: entry XMLs only + `allow-bean-definition-overriding=true`.
2. H: Boot forbids circular references the as-is allowed. Fix: write `spring.main.allow-circular-references=true` + Handover.
3. H (verified): EUC-KR XML copied to `legacy-config/` is re-encoded as UTF-8 under an EUC-KR declaration. Fix: byte copy (restructurer.py:2472).
4. H (verified): `CharacterEncodingFilter` EUC-KR is not carried over, and the hint names the Boot-4-removed `server.servlet.encoding.*`. Fix: write `spring.servlet.encoding.*` from the init-params.
5. H: JNDI DataSource — XML lookup bean left in place, and `spring.datasource.*` binds to nothing without spring-boot-jdbc (not brought by starter-webmvc).
6. H (verified): WEB-INF properties go to an unread stash and are reported `external`; `${…}` in classpath locations does the same, and the written `__SET_ME__` silently overrides packaged values.
7. H: `<mvc:default-servlet-handler/>` crashes without `server.servlet.register-default-servlet=true`; XML `CommonsMultipartResolver` crashes (no compile = no LLM).
8. H: `@EnableWebMvc` is not detected — the written `spring.mvc.view.*` and the front `static/` files are ignored, and servlet-context `<mvc:resources>` locations 404.
9. M: Boot 4 modularity — DelegatingFilterProxy/OEMIV skip rules assume spring-boot-security/-jpa that legacy deps lack (probe: an XSS DelegatingFilterProxy dropped as "security"); `*.do`, jsp-config, WebApplicationInitializer, WAR context-path, Log4j 2 have no follow-up.
10. LLM side: only 4 build-level gaps (javax filters in generated WebXmlRegistrations, multipart limits, Spring 4 removals, boot3-to-4 note excluded for legacy cases); stale "external Tomcat WAR" wording in the template, config, docstrings and servlet-to-boot.md.
