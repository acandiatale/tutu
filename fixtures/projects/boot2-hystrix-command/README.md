# boot2-hystrix-command fixture (advisory-guided LLM fix: programmatic Hystrix → Resilience4j)

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

What the migration is expected to do (`expected.json`, docs/ADVISORIES.md "Code the recipe cannot convert"):

1. The `hystrix` advisory (policy `recipe`) prepends `com.company.migrate.HystrixToResilience4j` to spring.rewrite:
   the starter becomes `spring-cloud-starter-circuitbreaker-resilience4j` (BOM-managed). The recipe has no rule for
   a `HystrixCommand` subclass, so the spring build fails in two file clusters:
   `InventoryCommand.java` (`package com.netflix.hystrix does not exist`, `cannot find symbol class HystrixCommand`,
   `method does not override …`) and `StockService.java` (`cannot find symbol method execute()`).
2. The fix loop matches the advisory to the first cluster (its errors name `com.netflix.hystrix`) and sends the hint
   ("Library migration guidance (what to migrate TO)") with that prompt; the attempt records `advisory_ids: ["hystrix"]`
   (journal `attempt` event, ledger `notes: advisory:hystrix`). The second cluster gets no hint.
3. The LLM is **replayed** (`sbmig.e2e.yml`: `llm.provider: fake`, `llm.base_url: replay:…/replay`):
   `replay/llm-001.json` ports `InventoryCommand` to a plain class that runs `run()` through
   `io.github.resilience4j.circuitbreaker.CircuitBreaker.ofDefaults("inventory").executeCallable(...)` and returns
   `getFallback()` on failure, keeping `public Integer execute()` so the caller compiles unchanged (plus a
   `// MIGRATION-TODO:`); `replay/llm-002.json` answers the caller's cluster with no change (it compiles once
   response 1 is applied in the same round). 2 fix calls + 1 recipe-candidate call in the harvest (the repeated
   replay answer is rejected as a recipe), BUILD SUCCESS, nothing blocked.
4. Finalize: `hystrix` outcome `replaced`; `llm_fixed_files = [src/main/java/com/example/stock/service/InventoryCommand.java]`
   and the Handover "from here on" lists "code rewritten by the LLM for hystrix: … — review these changes
   (changes.patch, attempts table)". (Paths are as recorded by the attempt, i.e. before the restructure moved the
   sources to `module/comp-jar/`.)

Own KB directory `out/e2e/kb-hystrix-command` (cleared by `scripts/e2e.py`), so the harvested case does not leak into
the other fixtures.
