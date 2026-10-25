---
title: Spring Framework 5 -> 6 -> 7 API removals
keywords:
  - "WebMvcConfigurerAdapter|HandlerInterceptorAdapter|InstantiationAwareBeanPostProcessorAdapter"
  - "Base64Utils|SocketUtils|ListenableFuture|AsyncRestTemplate|CommonsMultipartResolver|NestedServletException"
  - "ResponseStatusException|getStatus\\(|HttpStatusCode|ResponseEntityExceptionHandler|handleExceptionInternal|handleMethodArgumentNotValid"
  - "JdbcTemplate|queryForObject|queryForList|NamedParameterJdbcTemplate"
  - "RestTemplate|RestClient|HttpComponentsClientHttpRequestFactory|setConnectTimeout|setReadTimeout|SimpleClientHttpRequestFactory"
  - "acceptsProfiles|Profiles"
  - "HttpHeaders|MultiValueMap|containsKey|keySet|entrySet"
  - "MappingJackson2HttpMessageConverter|MappingJackson2JsonView|JacksonJsonHttpMessageConverter|JacksonJsonView|JsonMapper"
  - "PathMatchConfigurer|AntPathMatcher|PathPatternParser|setUseSuffixPatternMatch|setUseTrailingSlashMatch|favorPathExtension"
  - "org\\.springframework\\.lang\\.(Nullable|NonNull|NonNullApi)|jspecify"
  - "spring-retry|org\\.springframework\\.retry|RetryTemplate|@Retryable"
  - "ImportResource|spring-beans.*\\.xsd|schema/beans"
  - "spring-jcl|commons-logging"
  - "LocalVariableTableParameterNameDiscoverer|-parameters|ParameterNameDiscoverer"
  - "org\\.springframework\\.(web|context|beans|core|jdbc|orm|transaction)\\."
cases: [spring_legacy, spring_boot, msa, servlet_tomcat]
phases: [spring]
---
# Spring Framework 5.x → 6.x → 7.0 (Boot 4.1 = Framework 7.0.x)

## Removed adapter classes (implement the interface; default methods exist)
- `WebMvcConfigurerAdapter` → `implements WebMvcConfigurer`
- `HandlerInterceptorAdapter` → `implements HandlerInterceptor` (or `AsyncHandlerInterceptor`)
- `InstantiationAwareBeanPostProcessorAdapter` → `implements SmartInstantiationAwareBeanPostProcessor`
- `JpaVendorAdapter` unchanged; `RequestMappingHandlerAdapter` unchanged (not an "adapter" class).

## Removed utilities / types
- `org.springframework.util.Base64Utils` → `java.util.Base64` (`Base64.getEncoder().encodeToString(bytes)`, `getDecoder().decode(s)`,
  url-safe: `getUrlEncoder()`).
- `SocketUtils` → `org.springframework.test.util.TestSocketUtils.findAvailableTcpPort()` (tests) or port 0.
- `ListenableFuture`/`SettableListenableFuture`/`ListenableFutureCallback` → `CompletableFuture` (`KafkaTemplate.send` returns `CompletableFuture`).
- `AsyncRestTemplate` → `WebClient` or `RestClient` + executor. `CommonsMultipartResolver` → `StandardServletMultipartResolver`.
- `NestedServletException` → `jakarta.servlet.ServletException` (cause unwrapped).
- `LocalVariableTableParameterNameDiscoverer` removed → compile with `-parameters` (Boot parent sets it; else add
  `<parameters>true</parameters>` to maven-compiler-plugin) — symptom at runtime: "Name for argument … not specified".
- `spring-jcl` module removed (7.0) → plain `commons-logging` 1.3 (transitive); remove explicit `spring-jcl` deps.

## HTTP status
- `ResponseStatusException.getStatus()` → `getStatusCode()` (`HttpStatusCode`); `getRawStatusCode()` → `getStatusCode().value()`.
- `ResponseEntityExceptionHandler` overrides use `HttpStatusCode status` instead of `HttpStatus status`, e.g.
  `protected ResponseEntity<Object> handleMethodArgumentNotValid(MethodArgumentNotValidException ex, HttpHeaders headers, HttpStatusCode status, WebRequest request)`
  and `handleExceptionInternal(Exception ex, @Nullable Object body, HttpHeaders headers, HttpStatusCode statusCode, WebRequest request)`.
  `@Override` errors "method does not override" → change the parameter type; `HttpStatus.valueOf(status.value())` if an enum is needed.
- `ClientHttpResponse.getStatusCode()` returns `HttpStatusCode`; `HttpStatus.series()` → `status.is4xxClientError()` etc.

## JdbcTemplate
- Array-args overloads deprecated since 5.3 → varargs: `queryForObject(sql, new Object[]{id}, Long.class)` →
  `queryForObject(sql, Long.class, id)`; `query(sql, new Object[]{a}, rowMapper)` → `query(sql, rowMapper, a)`;
  `queryForList(sql, args, Type.class)` → `queryForList(sql, Type.class, args...)`.

## HTTP clients
- `RestTemplate` still exists (not deprecated in 7.0); `RestClient` is the preferred synchronous client. Do not rewrite callers.
- `HttpComponentsClientHttpRequestFactory` requires Apache HttpClient 5 (`org.apache.hc.client5`); int-millis
  `setConnectTimeout(int)`/`setReadTimeout(int)` overloads are deprecated/removed → `Duration` overloads
  (`setConnectTimeout(Duration.ofMillis(x))`); if `setReadTimeout` is missing, configure `SocketConfig`/`ConnectionConfig` on the HttpClient 5 connection manager.
- `SimpleClientHttpRequestFactory.setConnectTimeout(int)` → `setConnectTimeout(Duration)`.
- `HttpHeaders` no longer implements `MultiValueMap` (7.0): `headers.containsKey(n)` → `containsHeader(n)`,
  `keySet()` → `headerNames()`, `entrySet()` → `headerSet()` (round 10: the project recipe
  `com.company.migrate.Spring7RemovedApis` renames these three), passing it where a `MultiValueMap`/`Map` is expected →
  `headers.asMultiValueMap()` (deprecated bridge) or copy; `get`, `getFirst`, `add`, `set`, `addAll`, `forEach` remain.

## Jackson 3 message converters / views (round 10)
- The Framework 7 chain (UpgradeJackson_2_3) moves the project's Jackson code to `tools.jackson.*`; Spring keeps its
  Jackson 2 classes (deprecated) whose constructors take `com.fasterxml.jackson.databind.ObjectMapper`. The project recipe
  `com.company.migrate.Jackson3SpringConverters` renames `MappingJackson2HttpMessageConverter` →
  `JacksonJsonHttpMessageConverter`, `MappingJackson2JsonView` → `JacksonJsonView`, `MappingJackson2XmlHttpMessageConverter`
  → `org.springframework.http.converter.xml.JacksonXmlHttpMessageConverter`, `MappingJackson2XmlView` → `JacksonXmlView`.
- Constructor arguments: the Jackson 3 twins take `tools.jackson.databind.json.JsonMapper` (XML: `XmlMapper`) or its
  `Builder`, not `ObjectMapper` — `new JacksonJsonHttpMessageConverter(objectMapper)` with an `ObjectMapper` does not
  compile: declare the bean as `JsonMapper` (`JsonMapper.builder()…build()`), or pass `(JsonMapper) objectMapper` when it
  is one. `setObjectMapper(…)` / `getObjectMapper()` of the Jackson 2 classes do not exist on the twins: configure the
  mapper up front and pass it to the constructor. A subclass overriding `writePrefix(JsonGenerator, Object)` must drop
  `throws IOException` (Jackson 3 exceptions are unchecked).

## Environment / config
- `env.acceptsProfiles("dev")` (String varargs, deprecated) → `env.acceptsProfiles(Profiles.of("dev"))`.
- `@Bean` methods may be package-private / non-public; `@Configuration(proxyBeanMethods = false)` is fine.
- XML config still supported (`@ImportResource("classpath:applicationContext.xml")`); use unversioned schema URLs
  (`http://www.springframework.org/schema/beans/spring-beans.xsd`); old versioned xsd names may fail to resolve.

## Path matching (MVC)
- `PathPatternParser` is the only option for annotated controllers in 7.0: `PathMatchConfigurer.setUseSuffixPatternMatch`,
  `setUseTrailingSlashMatch`, `setUseRegisteredSuffixPatternMatch` removed (`setPathMatcher` still exists, deprecated) →
  delete the call + `// MIGRATION-TODO: suffix/trailing-slash matching removed`. `ContentNegotiationConfigurer.favorPathExtension`
  / `ignoreUnknownPathExtensions` removed → delete. Round 10: `com.company.migrate.Spring7RemovedApis` deletes these calls
  (chained ones too) wherever OpenRewrite attributed the types; the fix loop only sees files it could not parse.
- PathPattern syntax: `**` only at the end of a pattern (`/api/**/x` invalid) → `/api/{*rest}` or explicit segments.

## Nullability / Kotlin / retry
- `org.springframework.lang.Nullable/NonNull/NonNullApi/NonNullFields` deprecated → JSpecify `org.jspecify.annotations.Nullable/NonNull/NullMarked` (`org.jspecify:jspecify`, transitive via spring-core 7).
- Kotlin baseline 2.2.
- Resilience in core: `org.springframework.core.retry.RetryTemplate` / `RetryPolicy`, `@org.springframework.resilience.annotation.Retryable`
  + `@ConcurrencyLimit`, enabled by `@EnableResilientMethods`. spring-retry still works but is no longer version-managed by Boot.
