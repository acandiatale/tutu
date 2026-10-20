---
title: Spring Cloud Netflix / Sleuth / springfox on Boot 4 (library advisories)
keywords:
  - "com\\.netflix\\.(hystrix|loadbalancer|client|zuul|feign|ribbon)"
  - "HystrixCommand|EnableHystrix|EnableCircuitBreaker|HystrixProperty|hystrix"
  - "org\\.springframework\\.cloud\\.netflix|spring-cloud-starter-netflix|spring-cloud-netflix"
  - "Ribbon|RibbonClient|IRule|IPing|ILoadBalancer|LoadBalanced"
  - "Zuul|ZuulFilter|EnableZuulProxy|zuul"
  - "spring-cloud-starter-feign|EnableFeignClients|FeignClient|openfeign"
  - "org\\.springframework\\.cloud\\.sleuth|Sleuth|spring-cloud-sleuth|micrometer-tracing|brave"
  - "spring-cloud-dependencies|spring-cloud-starter-parent|spring-cloud\\.version|Hoxton|Greenwich|Finchley"
  - "springfox|springdoc|GroupedOpenApi|io\\.swagger\\.v3"
cases: [spring_boot, spring_legacy, msa]
phases: [spring, restructure]
---
# Spring Cloud Netflix, Sleuth, springfox on Boot 4 (see docs/ADVISORIES.md)

These libraries are EOL. The run never blocks on them: the build goes as far as it can and the report's
Handover section tells the owning team what to replace. In the build-fix loop, make the code COMPILE with the
smallest change; do NOT start a library migration the advisory says the team owns (ribbon/zuul, and the
Hystrix code the recipe does not cover).

## Release train
- Spring Cloud 2025.1.x is the Boot 4.0 train (`UpgradeSpringCloud_2025_1`, part of the Boot 4.0 umbrella,
  bumps `spring-cloud-dependencies` / `spring-cloud.version` and org.springframework.cloud:* to 5.0.x).
- Trains older than 2020.0 (Hoxton, Greenwich …) still managed Netflix modules; 2020.0+ do not. A Netflix
  starter without an explicit version then fails with "dependencies.dependency.version is missing".
  Minimal fix: keep the as-is version explicitly on that dependency (carried over → handover).

## Hystrix → Resilience4j (project recipe `com.company.migrate.HystrixToResilience4j`)
- The `hystrix` advisory (policy `recipe`) prepends the composite to spring.rewrite, before the Boot umbrella:
  `spring-cloud-starter-netflix-hystrix` / `-starter-hystrix` → `spring-cloud-starter-circuitbreaker-resilience4j`
  (BOM-managed, explicit version dropped; 5.0.3 when no Spring Cloud BOM), `hystrix-javanica` / `hystrix-core` /
  the dashboard starter removed, `@HystrixCommand` → `@CircuitBreaker` (io.github.resilience4j.circuitbreaker.annotation),
  `commandKey` → `name`, otherwise `name = "default"`, `fallbackMethod` kept, Hystrix-only attributes and nested
  `@HystrixProperty` removed, `@EnableHystrix` / `@EnableCircuitBreaker` / `@EnableHystrixDashboard` deleted.
  `@CircuitBreaker` comes from `resilience4j-annotations` (the starter → spring-cloud-circuitbreaker-resilience4j →
  resilience4j-spring-boot3 → resilience4j-spring6).
- Leftover compile errors after the recipe come from what it does not cover. Fix the build with the smallest
  change and leave the migration to the team (the Handover already says so):
  * programmatic `extends HystrixCommand<T>` / `HystrixObservableCommand`, `HystrixRequestContext`,
    `HystrixRuntimeException`: re-add `com.netflix.hystrix:hystrix-core:1.5.18` explicitly (the advisory then
    reports hystrix as still present). Do not hand-write a Resilience4j port inside the loop.
  * `org.springframework.cloud.netflix.hystrix.*` (dashboard, `HystrixCommands` for WebFlux): delete the dead
    configuration if nothing else uses it, otherwise keep `spring-cloud-starter-netflix-hystrix` 2.2.10.RELEASE
    pinned.
  * `cannot find symbol CircuitBreaker` means the new starter is missing from the module that uses the
    annotation. Add `org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j` without a version
    (managed by the Spring Cloud BOM).
- Runtime work for the team (not the loop): `resilience4j.circuitbreaker.instances.<name>` / `configs.default`,
  `hystrix.command.*.execution.isolation.thread.timeoutInMilliseconds` → `resilience4j.timelimiter.*`,
  a trailing `Throwable` parameter on every fallback method, `spring-boot-starter-aspectj` for the annotation
  aspect, and no more thread-pool isolation (use a Resilience4j bulkhead if needed).

## Ribbon / Zuul
- `org.springframework.cloud.netflix.ribbon.*` / `.zuul.*` are gone from every current train. If code does not
  compile, keep the as-is starter version pinned; the team moves to Spring Cloud LoadBalancer
  (`@LoadBalanced` stays valid) / Spring Cloud Gateway server-webmvc.

## Feign
- `spring-cloud-starter-feign` → `spring-cloud-starter-openfeign` (renamed by the umbrella);
  `org.springframework.cloud.netflix.feign.*` → `org.springframework.cloud.openfeign.*`
  (`@FeignClient`, `@EnableFeignClients`). `com.netflix.feign.*` (8.x) → `feign.*` (io.github.openfeign).

## Sleuth → Micrometer Tracing
- `MigrateCloudSleuthToMicrometerTracing` (in the Boot 3.0 chain): `spring-cloud-starter-sleuth` →
  `io.micrometer:micrometer-tracing-bridge-brave` (Boot-managed — drop any explicit 1.0.x version it pinned),
  `org.springframework.cloud.sleuth.Tracer` → `io.micrometer.tracing.Tracer`, `spring.sleuth.*` →
  `management.tracing.*`, log pattern `%X{traceId}/%X{spanId}`.

## springfox → springdoc
- `SpringFoxToSpringDoc` (in the Boot 2.6 chain) removes io.springfox, converts `io.swagger.annotations` to
  `io.swagger.v3.oas.annotations` (`@Api` → `@Tag`, `@ApiOperation` → `@Operation`, `@ApiParam` → `@Parameter`,
  `@ApiModelProperty` → `@Schema`) and the Docket bean to `GroupedOpenApi` or springdoc.* properties;
  `UpgradeSpringDoc_3_0` then renames `springdoc-openapi-ui` → `springdoc-openapi-starter-webmvc-ui` 3.0.x.
- Leftover `springfox.documentation.*` imports after the recipe: delete the dead helper code (or replace
  `ApiInfo` with `io.swagger.v3.oas.models.info.Info`); never re-add springfox.
