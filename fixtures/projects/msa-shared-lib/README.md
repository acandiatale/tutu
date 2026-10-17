# msa-shared-lib fixture (scenario S1: MSA with a shared library module)

`msa` plus a **`common/` library module** (`com.example.shop:shop-common:1.0.0-SNAPSHOT`): its own root pom (no
parent), no Spring Boot, no boot plugin, Java 8 source level, `javax.servlet-api` 3.1.0 (provided) and
`javax.annotation-api` 1.3.2. It is used by both services:

| module | Boot | Java | uses from `common` |
|---|---|---|---|
| `common` | — | 1.8 | `web/TraceIdFilter implements javax.servlet.Filter` with a `@javax.annotation.PostConstruct` init, `TraceIds` util |
| `order-service` | 2.7.18 | 11 | registers `TraceIdFilter` as a `@Bean`, `TraceIds.newId` for order ids; javax.validation, lombok 1.18.24 (`@Value`/`@Builder`/`@Data`/`@Slf4j`), `RestTemplate` |
| `user-service` | 3.3.13 | 17 | `TraceIds.newId` only (servlet-free part); jakarta JPA entity + `JpaRepository` |

The root `pom.xml` is a pure aggregator (`modules common, order-service, user-service`), not a parent. Tests were
dropped to keep the fixture at 10 Java files (the `msa` fixture keeps them).

## Build as-is

Services resolve `shop-common` from the reactor, so build from the root. The reactor needs a JDK in **17..18**
(user-service targets 17, lombok 1.18.24 breaks on ≥ 21):

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-18.0.2/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`)

* planner treats `common` as a migration target (every in-repo root pom), MSA pom-only restructure
* `common` and both services end on **jakarta** (`TraceIdFilter` → `jakarta.servlet.*` / `jakarta.annotation.PostConstruct`)
* `common/pom.xml` gets the in-house parent (local-e2e: `org.springframework.boot:spring-boot-starter-parent:4.1.0`)
  but **none** of the mandatory dependencies (it is a library, not a service)
* `success = true`, exit 0, `llm_usage.calls == 0`
