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

Success, exit 0, **1 LLM call** (the replayed structure review: one warning on the hystrix pin), no fix script. v2 has
no advisory recipes, so `spring-cloud-starter-netflix-hystrix` stays in `module/comp-jar/pom.xml` and `InventoryCommand`
still `extends HystrixCommand<Integer>` and compiles against it. Advisories exactly `hystrix` (`guide`,
`still-present` → Handover: the Resilience4j port is the owners' job) and `spring-cloud-bom` (`replaced`). Own KB
directory `out/e2e/kb-boot2-hystrix-command` (cleared by `scripts/e2e.py`).
