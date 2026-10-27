# boot3 fixture (case 2b: Spring Boot 3.3)

* `spring-boot-starter-parent` **3.3.13**, `java.version` 17 (built with JDK 21), jar, `spring-boot-maven-plugin`
* `spring-boot-starter-web`, `-actuator`, `-data-jpa` (+ `h2` runtime, `jakarta.persistence` entity), `-aop` (`@Aspect`)
* `RestTemplate` bean built via `RestTemplateBuilder.setConnectTimeout/setReadTimeout` (deprecated in 3.4, removed in 4.0)
* `@ConfigurationProperties` **record** (`PaymentProperties`) + `@ConfigurationPropertiesScan`
* `ResponseStatusException(HttpStatus, …)`
* Jackson 2 `ObjectMapper` / `JsonSerializer` registered with `@JsonComponent` (Boot 4 → Jackson 3 `tools.jackson`, `@JacksonComponent`)
* `guava` 32.1.3-jre (optional artifact → bump)
* tests: `@WebMvcTest` + `@MockBean` (removed in Boot 4 → `@MockitoBean`; `@WebMvcTest` moves to `spring-boot-webmvc-test`)

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home \
  mvn -q -DskipTests package
```

Tests also pass (`mvn -q test`).

Expected migration hot spots: Boot 4 starter split (`spring-boot-starter-webmvc`, test autoconfigure modules),
`RestTemplateBuilder` timeout API, `@MockBean`, Jackson 3 package move / `@JsonComponent`, guava bump.

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **2 LLM calls**: the structure review (verdict `ok`) and one new fix script for the case
`compile|cannot_find_symbol|JsonComponent` (`8dc5ebcf6d`): Boot 4 renamed `@JsonComponent` to `@JacksonComponent`
(the recipe moved the serializer to `tools.jackson` but left the annotation). guava → 33.4.8-jre. `scripts/demo-kb/run.sh`
and `scripts/fleet-demo.sh` use this case to show a stored script reused without a fix LLM call.
