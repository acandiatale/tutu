---
title: Spring Boot 3.x -> 4.0 changes (verified from the official 4.0 migration guide)
keywords:
  - "tools\\.jackson|com\\.fasterxml\\.jackson|ObjectMapper|JsonMapper|Jackson2ObjectMapperBuilder"
  - "JsonComponent|JsonMixin|JsonObjectSerializer|JsonObjectDeserializer|JsonValueDeserializer|JsonSerializer|JsonDeserializer"
  - "spring\\.jackson"
  - "spring-boot-starter-(web|web-services|aop|oauth2|tomcat|batch|classic|flyway|liquibase)"
  - "MockBean|SpyBean|MockitoBean|MockitoSpyBean|MockitoTestExecutionListener"
  - "TestRestTemplate|AutoConfigureMockMvc|AutoConfigureWebTestClient|MockMvc|WebTestClient|RestTestClient"
  - "EntityScan|EnvironmentPostProcessor|BootstrapRegistry|HttpMessageConverters|PropertyMapping|PropertyMapper"
  - "org\\.springframework\\.boot\\.autoconfigure\\.(orm|jdbc|data|web|jackson|flyway|liquibase|security|http)"
  - "org\\.springframework\\.boot\\.(test\\.web|web\\.servlet|web\\.client|orm|jdbc)"
  - "Undertow|undertow"
  - "spring\\.dao|spring\\.data\\.mongodb|spring\\.session\\.redis|management\\.health\\.mongo|spring\\.kafka\\.retry"
  - "Elasticsearch|RestClientBuilder|Rest5Client|elasticsearch-java"
  - "org\\.springframework\\.lang\\.(Nullable|NonNull)|jspecify"
  - "spring-retry|org\\.springframework\\.retry|@Retryable|@EnableRetry|RetryTemplate"
  - "loaderImplementation|CLASSIC|executable|layertools|includeOptional"
  - "hibernate-jpamodelgen|hibernate-processor"
  - "spock|hazelcast|Pulsar"
cases: [spring_boot, msa]
phases: [spring, restructure]
---
# Spring Boot 3.x → 4.0 (base of 4.1)

## Baseline
Java 17+ (we use 21), Jakarta EE 11 / Servlet 6.1 (embedded Tomcat 11, Jetty 12.1), Spring Framework 7,
Spring Security 7, Spring Data 2025.1+, Hibernate 7, Hibernate Validator 9, Jackson 3 default, JUnit 6/Jupiter, Kotlin 2.2.

## Jackson 3 (default)
- Packages/groupIds `com.fasterxml.jackson.*` → `tools.jackson.*` (e.g. `tools.jackson.databind.ObjectMapper`,
  `tools.jackson.databind.json.JsonMapper`, groupId `tools.jackson.core`). EXCEPTION: annotations stay
  `com.fasterxml.jackson.annotation.*` (`@JsonProperty`, `@JsonIgnore`, `@JsonFormat` … unchanged; artifact `jackson-annotations`).
- Boot renames: `Jackson2ObjectMapperBuilderCustomizer` → `JsonMapperBuilderCustomizer`; `@JsonComponent` → `@JacksonComponent`;
  `@JsonMixin` → `@JacksonMixin`; `JsonObjectSerializer` → `ObjectValueSerializer`; `JsonValueDeserializer`/`JsonObjectDeserializer` → `ObjectValueDeserializer`.
- Override the `JsonMapper` bean (not `ObjectMapper`). `JsonSerializer`/`JsonDeserializer` → `ValueSerializer`/`ValueDeserializer`
  (`tools.jackson.databind`); `SerializerProvider` → `SerializationContext`; checked `IOException` → unchecked `JacksonException`.
- Properties: `spring.jackson.read.*`/`spring.jackson.write.*` → `spring.jackson.json.read.*`/`spring.jackson.json.write.*`.
- Jackson 2 fallback (deprecated): module `spring-boot-jackson2` + `spring.jackson2.*` properties. Use it ONLY when the
  project must stay on `com.fasterxml.jackson.databind` (e.g. Jersey needs jackson2, a library API exposes Jackson 2 types).
  Rule for the fixer: if the pom/other code is already on `tools.jackson`, migrate; otherwise keep Jackson 2 imports +
  add `spring-boot-jackson2` rather than half-migrating. Add `// MIGRATION-TODO` (serialization defaults changed, e.g. dates as ISO strings).

## Starter renames (old names deprecated or gone)
| Old | New |
|---|---|
| `spring-boot-starter-web` | `spring-boot-starter-webmvc` (old deprecated) |
| `spring-boot-starter-web-services` | `spring-boot-starter-webservices` |
| `spring-boot-starter-aop` | `spring-boot-starter-aspectj` |
| `spring-boot-starter-oauth2-client` / `-resource-server` / `-authorization-server` | `spring-boot-starter-security-oauth2-client` / `-resource-server` / `-authorization-server` |
| `spring-boot-starter-tomcat` (provided, WAR) | `spring-boot-starter-tomcat-runtime` |
| Flyway/Liquibase plain jar | `spring-boot-starter-flyway` / `spring-boot-starter-liquibase` (auto-config needs the starter) |
| `spring-boot-starter-batch` (JDBC metadata) | `spring-boot-starter-batch-jdbc` (plain starter = in-memory) |
| `org.hibernate.orm:hibernate-jpamodelgen` | `org.hibernate.orm:hibernate-processor` |
Migration aids: `spring-boot-starter-classic` / `spring-boot-starter-test-classic` pull the pre-modular jar set (temporary).
Every technology has a matching `spring-boot-starter-<tech>-test` test starter.

## Modularization: classes moved to `spring-boot-<tech>` modules / `org.springframework.boot.<tech>` packages
- `@EntityScan` → `org.springframework.boot.persistence.autoconfigure.EntityScan`.
- `EnvironmentPostProcessor` → `org.springframework.boot.EnvironmentPostProcessor` (was `…boot.env`).
- `BootstrapRegistry`/`BootstrapRegistryInitializer` → `org.springframework.boot.bootstrap`.
- `HttpMessageConverters` deprecated → `ClientHttpMessageConvertersCustomizer` / `ServerHttpMessageConvertersCustomizer` beans
  (the class itself moved to `org.springframework.boot.http.converter.autoconfigure`).
- Round 10, moved but not by the 4.0 umbrella (project recipe `com.company.migrate.Boot4TypeRelocations` + handler
  `boot4-type-relocation-sweep`): `web.servlet.error.{ErrorController, ErrorAttributes, DefaultErrorAttributes}` →
  `org.springframework.boot.webmvc.error`; `web.server.{ErrorPage, ErrorPageRegistrar, ErrorPageRegistry}` →
  `org.springframework.boot.web.error`; `web.servlet.server.{ConfigurableServletWebServerFactory, ServletWebServerFactory,
  Session, Jsp, …}` → `org.springframework.boot.web.server.servlet`; `web.servlet.MultipartConfigFactory` and
  `web.servlet.filter.Ordered*Filter` → `org.springframework.boot.servlet(.filter)`; `autoconfigure.web.ServerProperties`
  → `org.springframework.boot.web.server.autoconfigure`; `web.servlet.ServletComponentScan` →
  `org.springframework.boot.web.server.servlet.context` (modules spring-boot-webmvc / -web-server / -servlet /
  -http-converter, all inside spring-boot-starter-webmvc). Nested types (`ServerProperties.Tomcat`) may have moved again.
- `TestRestTemplate` → `org.springframework.boot.resttestclient.TestRestTemplate` + `@AutoConfigureTestRestTemplate`; deps
  `spring-boot-resttestclient` (test) and `spring-boot-restclient`.
- `@PropertyMapping` → `org.springframework.boot.test.context.PropertyMapping`.
- Auto-config classes moved out of `org.springframework.boot.autoconfigure.<tech>` into `org.springframework.boot.<tech>.autoconfigure`
  (e.g. `…boot.jdbc.autoconfigure.DataSourceProperties`, `…boot.webmvc.autoconfigure…`). `cannot find symbol` on a Boot class →
  the class still exists in a new package; ask `<need_grep>` if unsure, or add the module starter.
- `PropertyMapper.alwaysApplyingWhenNonNull()` removed: `PropertyMapper.get()` now skips null sources by default; call `.always()` on a source to also map nulls.

## Testing
- `@MockBean`/`@SpyBean` REMOVED → `@MockitoBean`/`@MockitoSpyBean` (`org.springframework.test.context.bean.override.mockito`).
  They work on test-class fields only, not in `@Configuration` classes → for shared mocks use
  `@MockitoBean(types = {A.class, B.class})` on the test class (or a meta-annotation).
- `@SpringBootTest` no longer auto-provides MockMvc/WebTestClient/TestRestTemplate → add `@AutoConfigureMockMvc`,
  `@AutoConfigureWebTestClient`, `@AutoConfigureTestRestTemplate`; preferred new client `RestTestClient` + `@AutoConfigureRestTestClient`.
- `MockitoTestExecutionListener` removed → `@ExtendWith(MockitoExtension.class)` for `@Mock`/`@InjectMocks`.
- `@WebMvcTest`/`@DataJpaTest` etc. moved to tech modules (`org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest`,
  `org.springframework.boot.data.jpa.test.autoconfigure.DataJpaTest`) and need `spring-boot-starter-<tech>-test`.
  Verified in 4.1.0 jars: `org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc` (spring-boot-webmvc-test),
  `org.springframework.boot.resttestclient.autoconfigure.AutoConfigureTestRestTemplate` + `org.springframework.boot.resttestclient.TestRestTemplate`
  (spring-boot-resttestclient), `org.springframework.boot.test.context.SpringBootTest` (unchanged),
  `@ServletComponentScan` → `org.springframework.boot.web.server.servlet.context.ServletComponentScan` (spring-boot-web-server).

## Jackson code on Boot 4 (what sbmig does)
- `UpgradeSpringBoot_4_0` migrates serializer/deserializer bodies to Jackson 3 (`tools.jackson.databind.ValueSerializer`,
  `SerializationContext`, unchecked `tools.jackson.core.JacksonException`, `writeStringProperty`/`writeNumberProperty`).
- Project recipe `com.company.migrate.Boot4JacksonTwins` renames Boot's own types: `JsonComponent`→`JacksonComponent`,
  `JsonMixin`→`JacksonMixin`, `JsonObjectSerializer`→`ObjectValueSerializer`, `JsonObjectDeserializer`→`ObjectValueDeserializer`
  (all still in `org.springframework.boot.jackson`).
- `Jackson2ObjectMapperBuilderCustomizer` → `org.springframework.boot.jackson.autoconfigure.JsonMapperBuilderCustomizer`
  (`customize(JsonMapper.Builder builder)`); rewrite the body, don't just rename.
- Only if a class must stay on Jackson 2 (e.g. Jersey): import `org.springframework.boot.jackson2.*`; the
  `add-known-dependency` handler adds the deprecated `spring-boot-jackson2` module automatically.

## Removed
Undertow (→ Tomcat/Jetty; `dependency-relocation` swaps spring-boot-starter-undertow for spring-boot-starter-tomcat,
advisory `undertow`), Spring Session Hazelcast & MongoDB, Spock (restored in 4.1), Spring Retry dependency management
(→ Framework 7 `org.springframework.core.retry.RetryTemplate`, annotations `org.springframework.resilience.annotation.Retryable`
+ `@EnableResilientMethods`; or keep `org.springframework.retry:spring-retry` with an explicit `<version>`),
classic uber-jar loader (`<loaderImplementation>CLASSIC</loaderImplementation>` must be removed), fully-executable launch
scripts (`<executable>true</executable>`), reactive Pulsar.

## Properties
- `spring.dao.exceptiontranslation.enabled` → `spring.persistence.exceptiontranslation.enabled`
- `spring.data.mongodb.{uri,host,port,database,username,password,…}` → `spring.mongodb.*` (repositories stay `spring.data.mongodb.*`)
- `spring.session.redis.*` → `spring.session.data.redis.*`; `management.health.mongo*` → `management.health.mongodb*`
- `spring.kafka.retry.topic.backoff.random` → `spring.kafka.retry.topic.backoff.jitter`
- `spring-boot-properties-migrator` (runtime) reports renamed properties — temporary aid only.

## Misc
- Elasticsearch low-level `RestClient` → `Rest5Client` (`co.elastic.clients:elasticsearch-java`).
- `org.springframework.lang.Nullable` / `NonNull` deprecated → `org.jspecify.annotations.Nullable` / `NonNull` (JSpecify).
- Optional dependencies are excluded from the fat jar (`<includeOptional>true</includeOptional>` to keep).
- Liveness/readiness probes enabled by default; `server.forward-headers-strategy` ignored for WAR deployments.
