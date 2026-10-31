---
title: Spring Boot 4.0 -> 4.1 (verified from 4.1 release notes)
keywords:
  - "layertools|jarmode"
  - "derby|Derby"
  - "dynatrace|Dynatrace"
  - "LiveReload|livereload|devtools"
  - "skipTests|maven\\.test\\.skip|process-test-aot|AOT"
  - "bootstrap-mode|AsyncTaskExecutor"
  - "build-info"
  - "batch-data-mongo"
  - "spring-messaging"
  - "jooq|jOOQ"
  - "-Werror|failOnWarning|warnings found and -Werror specified"
cases: [spring_boot, spring_legacy, servlet_tomcat, pure_java, msa]
phases: [spring, restructure]
---
# Spring Boot 4.0 → 4.1

## Breaking / behavioural
- 4.1 removed what 4.0 deprecated *for removal*, plus the `layertools` jar mode. A `cannot find symbol` on a Boot class
  that compiled on 4.0 with a "deprecated for removal" warning → the replacement its 4.0 javadoc names.
- Still present in 4.1.0 (deprecated, they compile): `spring-boot-jackson2`, `HttpMessageConverters`,
  `spring-boot-starter-web` / `-web-services` / `-oauth2-*`. Gone since 4.0: `spring-boot-starter-aop` →
  `spring-boot-starter-aspectj` (boot4-packages.md).
- `layertools` jar mode removed → `java -Djarmode=tools -jar app.jar extract --layers --launcher`.
- Maven AOT: `-DskipTests` no longer skips test AOT processing → `-Dmaven.test.skip=true` (what sbmig's build uses).
- JPA `spring.data.jpa.repositories.bootstrap-mode=deferred` now requires an `AsyncTaskExecutor` bean (runtime).
- Embedded Tomcat basedir is deleted on close.
- Constructor binding of `Optional<T>` properties now binds `Optional.empty()` instead of `null` (runtime).
- Gradle build-info file moved to `META-INF/build-info.properties` (Maven unchanged).
- `spring-boot-starter-data-redis` now pulls `spring-messaging`.

## Deprecations (warnings only — change them only when a build sets `-Werror` / `failOnWarning`)
Derby support, Dynatrace V1 API, DevTools LiveReload.

## New
- Starter `spring-boot-starter-batch-data-mongodb` (Spring Batch metadata in MongoDB); Spock support restored.

## Managed versions in 4.1.0 (facts; omit `<version>` for Boot-managed artifacts)
Spring Framework 7.0.x, Spring Security 7.1, Spring Data 2026.0, Spring Batch 6.0, Hibernate 7.4, Hibernate Validator 9.1,
Jackson 3.1 (`tools.jackson`) and Jackson 2.21 (`com.fasterxml.jackson`, via `jackson-2-bom`), jOOQ 3.21 (requires
Java 21), Mockito 5.23, Flyway 12.4, Spring Kafka 4.1, Spring AMQP 4.1, Spring Integration 7.1.
Plugin versions (maven-compiler-plugin 3.15 …) are managed only where `spring-boot-starter-parent` /
`spring-boot-dependencies` is an inherited `<parent>` — a BOM import manages no plugin versions (maven-plugins.md).
