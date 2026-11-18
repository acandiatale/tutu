# boot2-hystrix-command fixture (programmatic Hystrix: a `HystrixCommand` subclass, advisory `hystrix`)

* `spring-boot-starter-parent` **2.7.18**, `java.version` 11, jar
* `spring-cloud-dependencies` **2021.0.9** imported in `dependencyManagement`
* `spring-cloud-starter-netflix-hystrix` **2.2.10.RELEASE** (explicit: no train after Hoxton manages it)
* Hystrix is used **programmatically only**: `InventoryCommand extends com.netflix.hystrix.HystrixCommand<Integer>`
  with `run()` / `getFallback()`, executed by `StockService.stockFor` as `new InventoryCommand(gateway, sku).execute()`.
  No `@HystrixCommand` annotation anywhere.
* a plain JUnit 5 test of `StockService` (known SKU → stock, unknown SKU → fallback 0)

As-is build:

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **3 LLM calls**: the replayed structure review (verdict `ok`) and two fix-script answers. The recipe
`com.company.migrate.HystrixToResilience4j` (recipe set `hystrix_resilience4j`) swaps the starter for
`spring-cloud-starter-circuitbreaker-resilience4j`, left **without a version** in `module/comp-jar/pom.xml` (the Spring
Cloud 2025.1 BOM manages it). It cannot port the subclass: with the Hystrix artifacts gone, `InventoryCommand` fails
with `package com.netflix.hystrix does not exist` (case `d4ff1cf456`) and `StockService` with `cannot find symbol`
for `execute()` (case `cc167595c4`). The fix loop gets the `## fix-loop: hystrix` note: `replay/llm-002.json` ports every
class that still extends `HystrixCommand` to a plain class with a shared Resilience4j `CircuitBreaker`
(`CircuitBreaker.ofDefaults("InventoryCommand")`), a public `execute()` that runs the former `run()` through
`executeCallable` and returns `getFallback()` on failure, and a `MIGRATION-TODO` comment (build_ok);
`replay/llm-003.json` answers the caller's case with the same script, which finds nothing left to port (the class was
ported in the same round), and the rebuild resolves it. Advisories exactly `hystrix` (`replaced`) and
`spring-cloud-bom` (`replaced`). Own KB directory `out/e2e/kb-boot2-hystrix-command` (cleared by `scripts/e2e.py`).
