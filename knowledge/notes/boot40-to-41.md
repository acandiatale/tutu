---
title: Spring Boot 4.0 -> 4.1 (verified from 4.1 release notes)
keywords:
  - "layertools|jarmode"
  - "derby|Derby"
  - "dynatrace|Dynatrace"
  - "LiveReload|livereload|devtools"
  - "skipTests|maven\\.test\\.skip|process-test-aot|AOT"
  - "deferred|bootstrap-mode|AsyncTaskExecutor"
  - "build-info"
  - "batch-data-mongo"
  - "spring-messaging"
  - "Optional<"
  - "jooq|jOOQ"
  - "is deprecated|has been deprecated|deprecated and marked for removal"
cases: [spring_boot, spring_legacy, servlet_tomcat, pure_java, msa]
phases: [spring, restructure]
---
# Spring Boot 4.0 → 4.1

## Breaking / behavioural
- APIs deprecated in 4.0 are REMOVED in 4.1 → a `cannot find symbol` on a Boot class that compiled on 4.0 with a
  deprecation warning: use the replacement named in its 4.0 javadoc (e.g. `spring-boot-jackson2` fallbacks, old starters,
  `HttpMessageConverters`, `spring-boot-starter-web` alias may still exist but deprecated — prefer new names).
- `layertools` jar mode removed → `java -Djarmode=tools -jar app.jar extract --layers --launcher`.
- Maven AOT: `-DskipTests` no longer skips test AOT processing → use `-Dmaven.test.skip=true` to skip test AOT.
- JPA `spring.data.jpa.repositories.bootstrap-mode=deferred` now requires an `AsyncTaskExecutor` bean (runtime).
- Embedded Tomcat basedir is deleted on close.
- Constructor binding of `Optional<T>` properties now binds `Optional.empty()` instead of `null`.
- Gradle build-info file moved to `META-INF/build-info.properties` (Maven unchanged).
- `spring-boot-starter-data-redis` now pulls `spring-messaging`.

## Deprecations (warnings only, do not "fix" unless they error)
Derby support, Dynatrace V1 API, DevTools LiveReload.

## New
- Starter `spring-boot-batch-data-mongo` (Spring Batch metadata in MongoDB); Spock support restored.

## Managed versions in 4.1 (use them; omit `<version>` for Boot-managed artifacts)
Spring Framework 7.0.x, Spring Security 7.1, Spring Data 2026.0, Hibernate 7.4, Hibernate Validator 9.1,
jOOQ 3.21 (requires Java 21), Mockito 5.23, Flyway 12.4, maven-compiler-plugin 3.15 (Boot-managed when using the Boot parent),
Spring Kafka 4.1, Spring AMQP 4.1, Spring Integration 7.1.
