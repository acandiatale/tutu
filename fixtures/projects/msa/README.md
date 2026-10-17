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
order-service with `java.lang.NoSuchFieldError: … JCTree$JCImport … qualid` (lombok) — the pre-emptive lombok handler case.

Expected migration behaviour: MSA mode → `msa_pom` only (parent / mandatory / optional on each service root pom,
directory structure untouched); per-service sub-case recipes (order-service: Boot 2 → 4 incl. javax → jakarta and
lombok floor; user-service: Boot 3 → 4).
