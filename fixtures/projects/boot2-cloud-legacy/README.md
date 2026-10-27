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

This is the **reference run** for library advisories and the Handover section (`docs/ADVISORIES.md`). Success, exit 0,
**2 LLM calls**: the replayed structure review (verdict `issues`, warnings on the hystrix pin and the `springfox.version`
property) and one new fix script for the only build error, `@EnableCircuitBreaker` (removed from spring-cloud-commons;
case `compile|cannot_find_symbol|EnableCircuitBreaker`, `ec7cf32b91`), which it deletes with a `MIGRATION-TODO`. The
Boot 4 umbrella run replaces springfox (→ `springdoc-openapi-starter-webmvc-ui`), sleuth (→ Micrometer Tracing) and the
Spring Cloud train (advisories `replaced`). v2 has no advisory recipes, so **Hystrix stays**: the starter and both
`@HystrixCommand` methods are kept and compile; advisory `hystrix` is `guide` / `still-present` and the Handover names
it, next to ehcache pinned at its as-is 2.10.x (carried over) and the owners' follow-ups (swagger UI path, tracing
configuration, the Resilience4j port).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```
