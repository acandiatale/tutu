# boot2-cloud-legacy fixture (library advisories: springfox + sleuth + hystrix + old Spring Cloud train)

* `spring-boot-starter-parent` **2.7.18**, `java.version` 11, jar
* `spring-cloud-dependencies` **2021.0.9** imported in `dependencyManagement`
* `spring-cloud-starter-sleuth` — **no version in the pom** (managed by the 2021.0.x train). Sleuth is discontinued;
  Boot 3+/4 use Micrometer Tracing (`io.micrometer:micrometer-tracing-bridge-brave`)
* `spring-cloud-starter-netflix-hystrix` **2.2.10.RELEASE**: the version is explicit because no train after Hoxton
  manages it. Hystrix is EOL; the replacement is Resilience4j (`spring-cloud-starter-circuitbreaker-resilience4j`,
  `@CircuitBreaker`). The Hystrix code has two annotation shapes:
  * `InventoryClient.stockFor` uses a plain `@HystrixCommand(fallbackMethod=…)` (no `commandKey`, so the circuit
    breaker is named `default`).
  * `InventoryClient.reserve` has `commandKey`, `groupKey`, `threadPoolKey`, `commandProperties` with a nested
    `@HystrixProperty` timeout, and `ignoreExceptions`.
  * `OrderApiApplication` carries `@EnableCircuitBreaker`.
* `springfox-boot-starter` **3.0.0** — springfox (Swagger 2) is EOL; `SwaggerConfig` declares a `Docket` bean +
  `@EnableSwagger2`, `OrderController` uses `io.swagger.annotations.@Api/@ApiOperation/@ApiParam`. The replacement is
  springdoc-openapi (`org.springdoc:springdoc-openapi-starter-webmvc-ui` 3.x on Boot 4)
* `net.sf.ehcache:ehcache` — **no version in the pom**, managed by the Boot 2.7 parent and by nothing after the
  migration. There is deliberately no advisory entry for it: the generic path (the as-is 2.10.x stays pinned, the
  Handover lists it with ⚠ "carried over") must cover libraries nobody catalogued
* a plain JUnit 5 unit test (no Spring context)

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

This is the **reference run** for library advisories and the Handover section (`docs/ADVISORIES.md`) and for the
Hystrix recipe (`docs/RECIPES.md`, "Hystrix → Resilience4j"). Success, exit 0, **1 LLM call**: the replayed structure
review (verdict `issues`, warnings on the missing `spring-boot-starter-aspectj` for the `@CircuitBreaker` aspect and on
ehcache 2 pinned in the root's dependencyManagement). No fix script: the build passes after the one OpenRewrite run and
the restructure. In that run `com.company.migrate.HystrixToResilience4j` (recipe set `hystrix_resilience4j`, before the
Boot set) swaps the starter for `spring-cloud-starter-circuitbreaker-resilience4j`, which ends **without a version** in
`module/comp-jar/pom.xml` (the as-is 2.2.10.RELEASE is removed before the swap; the Spring Cloud BOM, moved to the
2025.1 train by the umbrella, manages it), turns `stockFor` into `@CircuitBreaker(name = "default", fallbackMethod =
"stockFallback")` and `reserve` into `@CircuitBreaker(name = "inventoryReserve", fallbackMethod = "reserveFallback")`
(the Hystrix-only attributes and the nested `@HystrixProperty` go) and deletes `@EnableCircuitBreaker`. The Boot 4
umbrella replaces springfox (→ `springdoc-openapi-starter-webmvc-ui`), sleuth (→ Micrometer Tracing) and the Spring
Cloud train. Advisories `hystrix`, `springfox`, `sleuth`, `spring-cloud-bom` end `replaced`; no Hystrix artifact is
carried over. The Handover names the Resilience4j runtime review, ehcache pinned at its as-is 2.10.x (carried over)
and the owners' other follow-ups (swagger UI path, tracing configuration).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```
