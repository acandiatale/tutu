# msa fixture (case 4: several deployable services in one repository)

The root `pom.xml` is a **pure aggregator** (`packaging pom`, `modules order-service, user-service`). It is *not* the
parent of the services: each service declares its own `spring-boot-starter-parent` and `spring-boot-maven-plugin`,
has its own `application.yml` and tests. Mixed generations on purpose:

| service | Boot | Java | notes |
|---|---|---|---|
| `order-service` | 2.7.18 | 11 | `javax.validation`, lombok 1.18.24 (`@Data`, `@Value`, `@Builder`, `@Slf4j`), `RestTemplate` to user-service, JUnit 5 + Mockito unit test |
| `user-service` | 3.3.13 | 17 | `jakarta.persistence` entity + `JpaRepository` (h2), `@WebMvcTest` + `@MockBean` |

## Build as-is (per service)

```bash
cd order-service && JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
cd user-service  && JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home \
  mvn -q -DskipTests package
```

The whole reactor from the root needs a JDK in **17..18** (user-service targets 17, lombok 1.18.24 breaks on ≥ 21):
`JAVA_HOME=…/corretto-18.0.2/Contents/Home mvn -q -DskipTests package` works; with JDK 21 the root build fails in
order-service with `java.lang.NoSuchFieldError: … JCTree$JCImport … qualid` (lombok; v2's one OpenRewrite run raises
lombok before the only build).

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

MSA mode → `msa_pom` only (parent / mandatory / optional on each service root pom, directory structure untouched); one
OpenRewrite run per service (order-service: Java 21 + Boot 2 → 4 incl. javax → jakarta; user-service: Boot 3 → 4).
Success, exit 0, **1 LLM call** (the structure review, verdict `ok`), no fix script; the per-service property inventory
writes `order.tax-rate=__SET_ME__` into order-service's `application.properties` only.

Note: lombok is not expected in `optional_bumped` — `UpgradeToJava21` already raises it above the configured 1.18.38 and `artifacts.optional_version_policy: floor` (default) never downgrades.
