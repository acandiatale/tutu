---
title: Spring Boot 3.x -> 4.0 changes (verified from the official 4.0 migration guide)
keywords:
  - "spring\\.jackson"
  - "Undertow|undertow"
  - "spring\\.dao|spring\\.data\\.mongodb|spring\\.session\\.redis|management\\.health\\.mongo|spring\\.kafka\\.retry"
  - "Elasticsearch|RestClientBuilder|Rest5Client|elasticsearch-java"
  - "org\\.springframework\\.lang\\.(Nullable|NonNull)|jspecify"
  - "spring-retry|org\\.springframework\\.retry|@Retryable|@EnableRetry|RetryTemplate"
  - "loaderImplementation|<executable>|layertools|includeOptional"
  - "PropertyMapper|alwaysApplyingWhenNonNull|PropertyMapping"
  - "spock|hazelcast|Pulsar"
cases: [spring_boot, msa]
phases: [spring, restructure]
---
# Spring Boot 3.x → 4.0 (base of 4.1) — Boot projects

Package / starter relocations that every case needs are in boot4-packages.md; the Jackson 2 / 3 rules in
jackson-2-3.md. This note keeps what only an upgraded Boot project meets.

## Baseline
Java 17+ (we use 21), Jakarta EE 11 / Servlet 6.1 (embedded Tomcat 11, Jetty 12.1), Spring Framework 7,
Spring Security 7, Spring Data 2025.1+, Hibernate 7, Hibernate Validator 9, Jackson 3 default, JUnit 6/Jupiter, Kotlin 2.2.

## Jackson 3 in a Boot project
- `UpgradeSpringBoot_4_0` already moved serializer / deserializer code to Jackson 3 (`tools.jackson.databind.ValueSerializer`,
  `SerializationContext`, unchecked `tools.jackson.core.JacksonException`, `writeStringProperty` / `writeNumberProperty`);
  finish what it left (jackson-2-3.md rule A), never revert it.
- Override the `JsonMapper` bean (not `ObjectMapper`); `JsonSerializer` / `JsonDeserializer` → `ValueSerializer` /
  `ValueDeserializer` (`tools.jackson.databind`); `SerializerProvider` → `SerializationContext`.
- Properties: `spring.jackson.read.*` / `spring.jackson.write.*` → `spring.jackson.json.read.*` / `spring.jackson.json.write.*`;
  `spring.jackson.use-jackson2-defaults=true` restores Jackson 2's defaults (runtime choice: `# MIGRATION-TODO`).

## Removed
- Undertow (→ Tomcat / Jetty; `dependency-relocation` swaps spring-boot-starter-undertow for spring-boot-starter-tomcat,
  advisory `undertow`), Spring Session Hazelcast & MongoDB, Spock (restored in 4.1), reactive Pulsar.
- Spring Retry dependency management: keep `org.springframework.retry:spring-retry` with `version: null` (sbmig fills
  the as-is version); porting to Framework 7 `org.springframework.core.retry` / `@EnableResilientMethods` is the
  owners' decision (Handover), not a build fix.
- Classic uber-jar loader (`<loaderImplementation>CLASSIC</loaderImplementation>` must be removed) and fully-executable
  launch scripts (`<executable>true</executable>`).
- `PropertyMapper.alwaysApplyingWhenNonNull()` removed: `PropertyMapper.get()` skips null sources by default; call
  `.always()` on a source to also map nulls. `@PropertyMapping` → `org.springframework.boot.test.context.PropertyMapping`.

## Properties
- `spring.dao.exceptiontranslation.enabled` → `spring.persistence.exceptiontranslation.enabled`
- `spring.data.mongodb.{uri,host,port,database,username,password,…}` → `spring.mongodb.*` (repositories stay `spring.data.mongodb.*`)
- `spring.session.redis.*` → `spring.session.data.redis.*`; `management.health.mongo*` → `management.health.mongodb*`
- `spring.kafka.retry.topic.backoff.random` → `spring.kafka.retry.topic.backoff.jitter`
- `spring-boot-properties-migrator` (runtime) reports renamed properties — temporary aid only.

## Misc
- Elasticsearch low-level `RestClient` → `Rest5Client` (`co.elastic.clients:elasticsearch-java`).
- `org.springframework.lang.Nullable` / `NonNull` deprecated (still compile) → `org.jspecify.annotations.Nullable` / `NonNull`.
- Optional dependencies are excluded from the fat jar (`<includeOptional>true</includeOptional>` to keep).
- Liveness / readiness probes are enabled by default.
