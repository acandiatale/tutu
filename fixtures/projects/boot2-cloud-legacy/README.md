# boot2-cloud-legacy fixture (library advisories: springfox + sleuth + hystrix + old Spring Cloud train)

* `spring-boot-starter-parent` **2.7.18**, `java.version` 11, jar
* `spring-cloud-dependencies` **2021.0.9** imported in `dependencyManagement`
* `spring-cloud-starter-sleuth` — **no version in the pom** (managed by the 2021.0.x train). Sleuth is discontinued;
  Boot 3+/4 use Micrometer Tracing (`io.micrometer:micrometer-tracing-bridge-brave`)
* `spring-cloud-starter-netflix-hystrix` **2.2.10.RELEASE** — explicit version because no train after Hoxton manages
  it. Hystrix is EOL; the replacement is Resilience4j (`spring-cloud-starter-circuitbreaker-resilience4j`,
  `@CircuitBreaker`). `InventoryClient` uses `@HystrixCommand(fallbackMethod=…)`
* `springfox-boot-starter` **3.0.0** — springfox (Swagger 2) is EOL; `SwaggerConfig` declares a `Docket` bean +
  `@EnableSwagger2`, `OrderController` uses `io.swagger.annotations.@Api/@ApiOperation/@ApiParam`. The replacement is
  springdoc-openapi (`org.springdoc:springdoc-openapi-starter-webmvc-ui` 3.x on Boot 4)
* `net.sf.ehcache:ehcache` — **no version in the pom**, managed by the Boot 2.7 parent and by nothing after the
  migration. There is deliberately no advisory entry for it: the generic path (`carry-over-managed-version` pins the
  as-is 2.10.x, the Handover lists it with ⚠ "carried over") must cover libraries nobody catalogued
* a plain JUnit 5 unit test (no Spring context)

What the migration is expected to do (see `expected.json`; this is the **reference run** for library advisories and
the Handover section, `docs/ADVISORIES.md` / `docs/RUNBOOK.md` §2.5): finish with **BUILD SUCCESS** without an LLM,
nothing blocked, and the report's **Handover** section must say what was replaced (Boot 2.7 → 4.1, Spring Cloud
2021.0.9 → 2025.1.x, sleuth → micrometer-tracing via the Boot 4 umbrella chain, springfox → springdoc via the
advisory's `recipe` policy) and what is still the owners' job (hystrix → Resilience4j — `guide` policy, so it stays
in the pom pinned at 2.2.10.RELEASE and shows up as a ⚠ carried-over row; swagger UI path / OpenAPI bean review;
tracing configuration). Advisories never fail the run: the exit code stays 0 although hystrix is still present.

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```
