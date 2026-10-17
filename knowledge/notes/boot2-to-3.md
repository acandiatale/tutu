---
title: Spring Boot 2.x -> 3.x changes (prerequisite for Boot 4)
keywords:
  - "WebSecurityConfigurerAdapter|antMatchers|mvcMatchers|authorizeRequests"
  - "spring\\.redis|spring\\.data\\.cassandra|server\\.max-http-header-size"
  - "HttpStatus\\b|getRawStatusCode|getStatusCode|ResponseStatusException"
  - "ConstructorBinding"
  - "springfox|io\\.swagger\\.annotations|@Api(Operation|Model|Param|ModelProperty)?\\b|Docket|EnableSwagger2"
  - "spring\\.factories|EnableAutoConfiguration|AutoConfiguration\\.imports"
  - "ErrorController|getErrorPath"
  - "HttpTrace|httptrace|HttpExchange"
  - "Sleuth|brave|spring-cloud-sleuth"
  - "org\\.apache\\.http\\.|HttpClientBuilder|httpclient"
  - "flyway"
  - "PathMatchConfigurer|setUseTrailingSlashMatch|setUseSuffixPatternMatch"
  - "spring-boot-starter-webflux|WebClient"
cases: [spring_boot, msa]
phases: [spring]
---
# Spring Boot 2.x → 3.x (OpenRewrite UpgradeSpringBoot_4_0 chains 2.x → 3.5 → 4.0; residual errors below)

## Baseline
- Java 17+ (we target 21), Spring Framework 6, Jakarta EE 9/10 (`javax.*` EE → `jakarta.*`, see jakarta note),
  Hibernate 6, Flyway 9+, Spring Security 6, Micrometer Tracing (Sleuth gone).

## Security (also see spring-security-7 note)
- `WebSecurityConfigurerAdapter` removed → `@Configuration` class with
  `@Bean SecurityFilterChain filterChain(HttpSecurity http) throws Exception { … return http.build(); }`;
  `configure(WebSecurity)` → `@Bean WebSecurityCustomizer`; `configure(AuthenticationManagerBuilder)` →
  `@Bean UserDetailsService` / `@Bean AuthenticationManager` from `AuthenticationConfiguration`.
- `authorizeRequests()` → `authorizeHttpRequests()`, `antMatchers/mvcMatchers/regexMatchers` → `requestMatchers`.

## Web
- Trailing-slash matching off: `/users/` no longer matches `@GetMapping("/users")` → `// MIGRATION-TODO` (runtime), compile-neutral.
- `PathMatchConfigurer.setUseSuffixPatternMatch / setUseTrailingSlashMatch` deprecated (removed in Framework 7) → delete the call + MIGRATION-TODO.
- `ResponseStatusException.getStatus()` → `getStatusCode()` (returns `HttpStatusCode`); `ResponseEntity.getStatusCode()`
  returns `HttpStatusCode` → `HttpStatus.valueOf(code.value())` if an enum is needed, or compare `code.value()`.
- `ClientHttpResponse.getRawStatusCode()` → `getStatusCode().value()`.
- `ErrorController.getErrorPath()` removed (2.5) → delete the override (use `server.error.path`).
- RestTemplate with Apache HttpClient 4 (`org.apache.http.*`) no longer supported → `org.apache.httpcomponents.client5:httpclient5`
  (`org.apache.hc.client5.http.*`) for `HttpComponentsClientHttpRequestFactory`.
- WebFlux: same trailing-slash change; `WebClient` codecs config `spring.codec.*` unchanged.

## Configuration / auto-configuration
- `META-INF/spring.factories` key `org.springframework.boot.autoconfigure.EnableAutoConfiguration` →
  `META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports` (one FQCN per line) and
  annotate the class with `@AutoConfiguration` (instead of `@Configuration`). Other spring.factories keys still work.
- `@ConstructorBinding` moved to `org.springframework.boot.context.properties.bind.ConstructorBinding` and is only
  needed when a type has multiple constructors; on a single-constructor class/record → just delete it.
- `@ConfigurationProperties` + `@ConstructorBinding` on type level → remove annotation.
- Properties: `spring.redis.*` → `spring.data.redis.*`; `spring.data.cassandra.*` → `spring.cassandra.*`;
  `server.max-http-header-size` → `server.max-http-request-header-size`; `management.trace.http.*` → `management.httpexchanges.*`;
  `spring.jpa.hibernate.use-new-id-generator-mappings` removed; `spring.mvc.pathmatch.matching-strategy=ant_path_matcher` removed in Boot 4.
- Actuator: `HttpTraceRepository`/`InMemoryHttpTraceRepository` → `HttpExchangeRepository`/`InMemoryHttpExchangeRepository`.

## Libraries
- springfox (`springfox.documentation.*`, `@EnableSwagger2`, `Docket`) is dead → springdoc-openapi
  (`org.springdoc:springdoc-openapi-starter-webmvc-ui`; 2.x for Boot 3, 3.x for Boot 4). Annotations
  `io.swagger.annotations.*` → `io.swagger.v3.oas.annotations.*`: `@Api`→`@Tag(name=…)`, `@ApiOperation(value)`→`@Operation(summary=…)`,
  `@ApiParam`→`@Parameter`, `@ApiModel`/`@ApiModelProperty`→`@Schema`, `@ApiResponse(code=…, message=…)`→`@ApiResponse(responseCode="…", description=…)`,
  `@ApiIgnore`→`@Hidden`. `Docket` beans → `@Bean GroupedOpenApi` or delete + MIGRATION-TODO.
- Sleuth (`org.springframework.cloud.sleuth.*`) → Micrometer Tracing (`io.micrometer.tracing.Tracer`) + bridge
  (`micrometer-tracing-bridge-brave` / `-otel`).
- Ehcache 2, Hazelcast 3, Apache Solr client, JUnit 4 vintage engine no longer managed by Boot → explicit version or migrate.
- `MockBean`/`SpyBean` still exist in Boot 3 (deprecated 3.4) but are REMOVED in Boot 4 → see boot3-to-4.
