---
title: Spring Boot 4 package and starter relocations (every case; the generated boot module is Boot 4.1)
keywords:
  - "org\\.springframework\\.boot\\."
  - "spring-boot-starter-"
  - "ErrorPage|ErrorPageRegistrar|ErrorController|ErrorAttributes"
  - "ServletComponentScan|ServletWebServerFactory|WebServerFactoryCustomizer|MultipartConfigFactory|ServerProperties"
  - "TomcatServletWebServerFactory|TomcatConnectorCustomizer|TomcatContextCustomizer|org\\.springframework\\.boot\\.web\\.embedded"
  - "JsonComponent|JsonMixin|JsonObjectSerializer|JsonObjectDeserializer|JsonValueDeserializer|Jackson2ObjectMapperBuilderCustomizer|JsonMapperBuilderCustomizer"
  - "RestTemplateBuilder|RestTemplateCustomizer|RestClientCustomizer"
  - "EntityScan|EnvironmentPostProcessor|BootstrapRegistry|HttpMessageConverters|DataSourceProperties|DataSourceBuilder"
  - "MockBean|SpyBean"
cases: []
phases: [spring, restructure]
---
# Spring Boot 4.x packages and starters (all cases)

Every project ends on Spring Boot 4.1: a converted `spring_legacy` / `servlet_tomcat` project gets a generated boot
module (Application, `WebXmlRegistrations`, `legacy-config/`), a Boot project is upgraded. Boot 4 split `spring-boot` /
`spring-boot-autoconfigure` into one module per technology (`spring-boot-<tech>`, packages `org.springframework.boot.<tech>`);
the starters bring them. `cannot find symbol` / `package … does not exist` on a Boot class → the class moved: change the
import (list below); when its module is not on the classpath, add the module's starter with `version: null`
(Boot-managed). Ask `<need_grep>` when unsure.

## Moved classes (verified in the 4.1.0 jars: old `org.springframework.boot.` suffix → new package, module)
- `web.server.ErrorPage`, `ErrorPageRegistrar`, `ErrorPageRegistry` → `org.springframework.boot.web.error` (spring-boot)
- `web.servlet.error.ErrorController`, `ErrorAttributes`, `DefaultErrorAttributes` → `org.springframework.boot.webmvc.error`
  (spring-boot-webmvc)
- `web.servlet.ServletComponentScan` → `org.springframework.boot.web.server.servlet.context.ServletComponentScan`
  (spring-boot-web-server)
- `web.servlet.server.{ConfigurableServletWebServerFactory, ServletWebServerFactory, Session, Jsp}` →
  `org.springframework.boot.web.server.servlet`; `WebServerFactoryCustomizer`, `MimeMappings` keep
  `org.springframework.boot.web.server` (both now in spring-boot-web-server)
- `web.embedded.tomcat.TomcatServletWebServerFactory` → `org.springframework.boot.tomcat.servlet`;
  `TomcatConnectorCustomizer`, `TomcatContextCustomizer` → `org.springframework.boot.tomcat` (spring-boot-tomcat)
- `web.servlet.MultipartConfigFactory` → `org.springframework.boot.servlet`; `web.servlet.filter.Ordered*Filter` →
  `org.springframework.boot.servlet.filter` (spring-boot-servlet)
- `autoconfigure.web.ServerProperties` → `org.springframework.boot.web.server.autoconfigure` (nested types may have moved)
- `web.client.RestTemplateBuilder`, `RestTemplateCustomizer` → `org.springframework.boot.restclient` (also
  `RestClientCustomizer`); module spring-boot-restclient, starter `spring-boot-starter-restclient`
- `autoconfigure.domain.EntityScan` → `org.springframework.boot.persistence.autoconfigure.EntityScan`
- `autoconfigure.jdbc.DataSourceProperties` → `org.springframework.boot.jdbc.autoconfigure`; `jdbc.DataSourceBuilder`
  unchanged (spring-boot-jdbc, starter `spring-boot-starter-jdbc`)
- `env.EnvironmentPostProcessor` → `org.springframework.boot.EnvironmentPostProcessor` (the old one is deprecated);
  `BootstrapRegistry` / `BootstrapRegistryInitializer` → `org.springframework.boot.bootstrap`
- `autoconfigure.http.HttpMessageConverters` → `org.springframework.boot.http.converter.autoconfigure` (deprecated →
  `ServerHttpMessageConvertersCustomizer` / `ClientHttpMessageConvertersCustomizer` beans)
- Auto-configuration classes moved from `org.springframework.boot.autoconfigure.<tech>` to
  `org.springframework.boot.<tech>.autoconfigure` (e.g. `…boot.webmvc.autoconfigure.WebMvcAutoConfiguration`).
- Unchanged: `ServletRegistrationBean`, `FilterRegistrationBean`, `ServletListenerRegistrationBean`,
  `DelegatingFilterProxyRegistrationBean`, `ServletContextInitializer` (`org.springframework.boot.web.servlet`),
  `SpringBootServletInitializer` (`…web.servlet.support`), `@SpringBootApplication`, `@ConfigurationProperties`,
  `@ConstructorBinding` (`…context.properties.bind`).

## Boot's Jackson support types (the Jackson 2 / 3 decision itself: jackson-2-3.md)
- `@JsonComponent` → `@JacksonComponent`, `@JsonMixin` → `@JacksonMixin`, `JsonObjectSerializer` → `ObjectValueSerializer`,
  `JsonObjectDeserializer` / `JsonValueDeserializer` → `ObjectValueDeserializer` (all `org.springframework.boot.jackson`).
- `Jackson2ObjectMapperBuilderCustomizer` → `org.springframework.boot.jackson.autoconfigure.JsonMapperBuilderCustomizer`
  (`customize(JsonMapper.Builder)`; rewrite the body, do not just rename). The Jackson 2 originals live on in
  `org.springframework.boot.jackson2` (module `spring-boot-jackson2`, deprecated) — only for a Boot project that must
  stay on Jackson 2; never for a converted project.

## Starters (old → new; versions come from the Boot BOM, so `version: null`)
- `spring-boot-starter-web` → `spring-boot-starter-webmvc` (the old name still exists in 4.1, deprecated)
- `spring-boot-starter-aop` → `spring-boot-starter-aspectj` (`-aop` is gone since 4.0: `dependencies.dependency.version is missing`)
- `spring-boot-starter-web-services` → `-webservices`; `-oauth2-client` / `-oauth2-resource-server` /
  `-oauth2-authorization-server` → `-security-oauth2-client` / `-security-oauth2-resource-server` /
  `-security-oauth2-authorization-server` (old names deprecated)
- `RestTemplateBuilder` / `RestClient.Builder` injection → `spring-boot-starter-restclient`; `WebClient` → `-webclient`
- Spring Batch with JDBC metadata → `spring-boot-starter-batch-jdbc` (plain `-batch` keeps the metadata in memory);
  MongoDB metadata → `spring-boot-starter-batch-data-mongodb`
- Flyway / Liquibase auto-configuration needs `spring-boot-starter-flyway` / `-liquibase` (the plain jar is not enough)
- `spring-boot-starter-tomcat` stays as sbmig wrote it (`provided` in an executable WAR); `-tomcat-runtime` is the
  4.0 guide's name for the same jars — never rename either.
- `org.hibernate.orm:hibernate-jpamodelgen` → `org.hibernate.orm:hibernate-processor`.
- `spring-boot-starter-classic` pulls the whole pre-modular jar set — never add it to fix one import.
- The converted boot module's starter was chosen by sbmig: do not add Security / Batch / JPA starters to it to compile
  a class of another module — fix that module's own dependencies.

## Tests (not compiled by sbmig's build: `-Dmaven.test.skip=true`)
`@MockBean` / `@SpyBean` were removed → `@MockitoBean` / `@MockitoSpyBean`
(`org.springframework.test.context.bean.override.mockito`); this matters only when a project compiles its tests.
