---
title: Spring Framework 4/5 -> 6 -> 7 API removals
keywords:
  - "WebMvcConfigurerAdapter|HandlerInterceptorAdapter|InstantiationAwareBeanPostProcessorAdapter"
  - "Base64Utils|SocketUtils|AsyncRestTemplate|CommonsMultipartResolver|NestedServletException"
  - "org\\.springframework\\.util\\.concurrent|ListenableFuture|ListenableFutureCallback"
  - "ResponseStatusException|getStatus\\(|ResponseEntityExceptionHandler|handleExceptionInternal|handleMethodArgumentNotValid"
  - "\\b(MOVED_TEMPORARILY|REQUEST_ENTITY_TOO_LARGE|REQUEST_URI_TOO_LONG|METHOD_FAILURE|USE_PROXY|INSUFFICIENT_SPACE_ON_RESOURCE|DESTINATION_LOCKED|CHECKPOINT)\\b"
  - "getStatusCodeValue|getRawStatusCode|HttpStatusCode cannot be converted to|HttpStatusCode"
  - "APPLICATION_(PROBLEM_)?JSON_UTF8"
  - "JdbcTemplate|queryForObject|queryForList|NamedParameterJdbcTemplate|queryForInt|queryForLong|SimpleJdbcTemplate"
  - "RestTemplate|RestClient|HttpComponentsClientHttpRequestFactory|setConnectTimeout|setReadTimeout|SimpleClientHttpRequestFactory|OkHttp3ClientHttpRequestFactory"
  - "org\\.springframework\\.remoting|HttpInvoker\\w*|RmiProxyFactoryBean|RmiServiceExporter|Hessian(Proxy|Service)\\w*|JaxWsPortProxyFactoryBean"
  - "annotation\\.Required\\b|class Required\\b|RequiredAnnotationBeanPostProcessor"
  - "no suitable method found for (notNull|isTrue|state|hasText|hasLength|notEmpty|isNull|noNullElements|isInstanceOf)\\("
  - "ThemeResolver|ThemeSource|ThemeChangeInterceptor|WebJarsResourceResolver|CommonsMultipartFile|multipart\\.commons|NestedIOException|Log4jConfigurer|OpenSessionInView"
  - "AbstractExcelView|AbstractJExcelView|DefaultAnnotationHandlerMapping|AnnotationMethodHandlerAdapter|SimpleFormController|MultiActionController|AbstractCommandController"
  - "acceptsProfiles|Profiles"
  - "HttpHeaders|MultiValueMap|containsKey|keySet|entrySet"
  - "MappingJackson2HttpMessageConverter|MappingJackson2JsonView|JacksonJsonHttpMessageConverter|JacksonJsonView|Jackson2ObjectMapperBuilder|Jackson2ObjectMapperFactoryBean"
  - "PathMatchConfigurer|AntPathMatcher|PathPatternParser|setUseSuffixPatternMatch|setUseTrailingSlashMatch|favorPathExtension|placed at the start or end of the pattern"
  - "org\\.springframework\\.lang\\.(Nullable|NonNull|NonNullApi)|jspecify"
  - "spring-retry|org\\.springframework\\.retry|RetryTemplate|@Retryable"
  - "ImportResource|spring-beans.*\\.xsd|schema/beans"
  - "spring-jcl|commons-logging"
  - "LocalVariableTableParameterNameDiscoverer|-parameters|ParameterNameDiscoverer"
  - "org\\.springframework\\.(web|context|beans|core|jdbc|orm|transaction)\\."
cases: [spring_legacy, spring_boot, msa, servlet_tomcat]
phases: [spring]
---
# Spring Framework 4.x / 5.x → 6.x → 7.0 (Boot 4.1 = Framework 7.0.x)

`UpgradeSpringFramework_7_0` (5_0 … 6_2, then `UpgradeJackson_2_3`) already fixed what its types resolved; what
reaches you is the rest. Everything below is absent from the 7.0.x jars unless it says "compiles".

## Removed adapter classes (implement the interface; default methods exist)
- `WebMvcConfigurerAdapter` → `implements WebMvcConfigurer`; `HandlerInterceptorAdapter` → `implements HandlerInterceptor`
  (or `AsyncHandlerInterceptor`); `InstantiationAwareBeanPostProcessorAdapter` → `implements SmartInstantiationAwareBeanPostProcessor`.

## Removed utilities / types
- `org.springframework.util.Base64Utils` → `java.util.Base64` (`getEncoder().encodeToString(b)`, `getDecoder().decode(s)`,
  url-safe `getUrlEncoder()`); `SocketUtils` → `TestSocketUtils.findAvailableTcpPort()` (tests) or port 0.
- `org.springframework.util.concurrent.*` (`ListenableFuture`, `SettableListenableFuture`, `ListenableFutureCallback`)
  → `CompletableFuture` (`addCallback(s, f)` → `whenComplete((r, ex) -> …)`; `KafkaTemplate.send` returns one).
- `AsyncRestTemplate` → `RestClient` / `WebClient` + an executor; `OkHttp3ClientHttpRequestFactory` →
  `JdkClientHttpRequestFactory` or `SimpleClientHttpRequestFactory`.
- `CommonsMultipartResolver` → delete the bean; the limits move to `spring.servlet.multipart.*` (mapping in
  servlet-to-boot.md); `CommonsMultipartFile` (`web.multipart.commons`) → `MultipartFile`.
- `NestedServletException` → `jakarta.servlet.ServletException` (cause unwrapped); `NestedIOException` → `java.io.IOException`.
- `@Required` / `RequiredAnnotationBeanPostProcessor` → delete the annotation / the bean (the setter stays).
- `Assert.notNull(x)`, `isTrue(b)`, `state(b)`, `hasText(s)` … without a message were removed (6.0) → add a message
  argument (`Assert.notNull(x, "x must not be null")`).
- `org.springframework.util.Log4jConfigurer` / `web.util.Log4jConfigListener` → delete the call / registration (Boot configures logging).
- Theme support (`ThemeResolver`, `ThemeSource`, `ThemeChangeInterceptor`, `ResourceBundleThemeSource`) removed →
  remove those beans / the interceptor + `// MIGRATION-TODO: theme support removed`. `WebJarsResourceResolver` →
  `LiteWebJarsResourceResolver`.
- `orm.hibernate5.support.OpenSessionInViewFilter` / `…Interceptor` → `org.springframework.orm.jpa.support.OpenEntityManagerInViewFilter`
  / `…Interceptor`, or `spring.jpa.open-in-view` (Boot).
- `org.springframework.remoting.*` (HttpInvoker, RMI, Hessian, `JaxWsPortProxyFactoryBean`; removed in 6.0) has no
  successor: keep the service interface and its callers compiling, replace the proxy / exporter bean by a stub with
  `// MIGRATION-TODO: Spring remoting removed`; when the client cannot compile otherwise, answer `<blocked>` (owners).
- `LocalVariableTableParameterNameDiscoverer` removed → compile with `-parameters` (maven-plugins.md); runtime symptom
  "Name for argument … not specified".
- `spring-jcl` removed (7.0) → plain `commons-logging` (transitive); remove explicit `spring-jcl` dependencies.
- `javax.annotation` / `javax.inject` annotations (`@Resource`, `@PostConstruct`, `@Inject`) are ignored by Spring 7 →
  `jakarta.annotation` / `jakarta.inject`; never add `javax.annotation-api` / `javax.inject` just to make code compile.

## Spring 3.2 / 4 → 5 removals
- `AbstractExcelView` / `AbstractJExcelView` → `AbstractXlsView` / `AbstractXlsxView` (`buildExcelDocument(Map, Workbook, req, res)`, POI).
- `JdbcTemplate.queryForInt` / `queryForLong` → `queryForObject(sql, Integer.class, args…)` / `Long.class`;
  `SimpleJdbcTemplate` → `JdbcTemplate` / `NamedParameterJdbcTemplate`.
- XML beans `DefaultAnnotationHandlerMapping` / `AnnotationMethodHandlerAdapter` → `RequestMappingHandlerMapping` /
  `RequestMappingHandlerAdapter`; `SimpleFormController` / `MultiActionController` / `AbstractCommandController` →
  `@Controller` + `@RequestMapping` keeping the URLs (MIGRATION-TODO where the form workflow is not ported).

## HTTP status
- `MediaType.APPLICATION_JSON_UTF8(_VALUE)` / `APPLICATION_PROBLEM_JSON_UTF8(_VALUE)` → `APPLICATION_JSON(_VALUE)` /
  `APPLICATION_PROBLEM_JSON(_VALUE)`.
- Removed `HttpStatus` constants: `MOVED_TEMPORARILY` → `FOUND`; `REQUEST_ENTITY_TOO_LARGE` → `CONTENT_TOO_LARGE`
  (`PAYLOAD_TOO_LARGE` still exists); `REQUEST_URI_TOO_LONG` → `URI_TOO_LONG`; `METHOD_FAILURE`,
  `INSUFFICIENT_SPACE_ON_RESOURCE`, `DESTINATION_LOCKED`, `USE_PROXY`, `CHECKPOINT` → `HttpStatusCode.valueOf(<code>)`.
- `ResponseEntity.getStatusCodeValue()` → `getStatusCode().value()`. `ResponseEntity.getStatusCode()` /
  `ClientHttpResponse.getStatusCode()` return `HttpStatusCode` (`HttpStatus.valueOf(code.value())` when an enum is
  needed); `getRawStatusCode()` → `getStatusCode().value()`; `ResponseStatusException.getStatus()` → `getStatusCode()`.
- `ResponseEntityExceptionHandler` overrides take `HttpStatusCode status` (e.g. `handleMethodArgumentNotValid(
  MethodArgumentNotValidException ex, HttpHeaders headers, HttpStatusCode status, WebRequest request)`); "method does not
  override" → change the parameter type.

## JdbcTemplate
- Array-args overloads (`queryForObject(sql, new Object[]{id}, Long.class)`, `query(sql, new Object[]{a}, rowMapper)`)
  compile (deprecated) — change them to varargs only where javac reports them.

## HTTP clients
- `RestTemplate` still exists (not deprecated in 7.0); do not rewrite callers.
- `HttpComponentsClientHttpRequestFactory` needs Apache HttpClient 5 (`org.apache.hc.client5`). Its
  `setConnectTimeout(..)` was REMOVED in 7.0 (both overloads) → set it on the client:
  `PoolingHttpClientConnectionManagerBuilder.create().setDefaultConnectionConfig(ConnectionConfig.custom()
  .setConnectTimeout(Timeout.ofMilliseconds(x)).build()).build()` → `HttpClients.custom().setConnectionManager(cm).build()`
  → `new HttpComponentsClientHttpRequestFactory(client)`. `setReadTimeout(int|Duration)` and
  `setConnectionRequestTimeout(int|Duration)` still exist — keep them.
- `SimpleClientHttpRequestFactory.setConnectTimeout(int)` / `setReadTimeout(int)` compile — leave them.
- `HttpHeaders` no longer implements `MultiValueMap` (7.0): `containsKey(n)` → `containsHeader(n)`, `keySet()` →
  `headerNames()`, `entrySet()` → `headerSet()`; where a `MultiValueMap` / `Map` is expected → `headers.asMultiValueMap()`;
  `get`, `getFirst`, `add`, `set`, `addAll`, `forEach` remain.

## Jackson converters / views
The Jackson 2 / 3 rules and the twins (`JacksonJsonHttpMessageConverter(JsonMapper)`, `JacksonJsonView`,
`JsonMapper.builder()` for `Jackson2ObjectMapperBuilder`) are in jackson-2-3.md. A subclass overriding
`writePrefix(JsonGenerator, Object)` drops `throws IOException` (Jackson 3 exceptions are unchecked).

## Environment / XML config
- `env.acceptsProfiles("dev")` compiles (deprecated); `Profiles.of("dev")` only when the file is touched anyway.
- XML config and versioned schema names (`spring-beans-4.3.xsd`) still resolve from the jars (`META-INF/spring.schemas`);
  `<mvc:*>` is deprecated in 7.0 (warning). Never edit XML for the build; startup issues go to the Handover.

## Path matching (MVC)
- `PathMatchConfigurer.setUseSuffixPatternMatch` / `setUseTrailingSlashMatch` / `setUseRegisteredSuffixPatternMatch`
  removed (`setPathMatcher` deprecated) → delete the call + `// MIGRATION-TODO: suffix/trailing-slash matching removed`;
  `ContentNegotiationConfigurer.favorPathExtension` / `ignoreUnknownPathExtensions` removed → delete (chained calls too).
- PathPattern (7.0.x, checked by parsing): `**` / `{*x}` are accepted at the start or the end (`/**/*.do`, `/api/**` —
  keep them); only a `**` in the middle (`/api/**/list`, `/admin/**/*.do`) is rejected at startup ("… should be placed
  at the start or end of the pattern") → `/api/{*rest}` or explicit segments + MIGRATION-TODO.

## Nullability / retry
- `org.springframework.lang.Nullable/NonNull/NonNullApi/NonNullFields` are deprecated (they compile) → JSpecify
  `org.jspecify.annotations.*` (transitive via spring-core 7) only where a file is touched anyway.
- `org.springframework.retry:spring-retry` is no longer Boot-managed: keep it with `version: null` (sbmig fills the
  as-is version). Porting to `org.springframework.core.retry` / `@org.springframework.resilience.annotation.Retryable`
  is the owners' decision (Handover).
