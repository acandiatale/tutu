# boot2 fixture (case 2b: Spring Boot 2.7)

* `spring-boot-starter-parent` **2.7.18**, `java.version` 11, jar, `spring-boot-maven-plugin`
* `spring-boot-starter-web`, `-security`, `-data-jpa` (+ `h2` runtime), `-validation` (javax.validation)
* `SecurityConfig extends WebSecurityConfigurerAdapter` with `authorizeRequests().antMatchers(...)` and `.and()` chaining
  (removed in Security 6/7)
* `Member` is a `javax.persistence` `@Entity` + `MemberRepository extends JpaRepository`
* **lombok 1.18.24** (`@Data`, `@Builder`, `@Slf4j`, `@RequiredArgsConstructor`) — optional artifact
* `commons-lang3` (version managed by Boot 2.7 → 3.12.0) — optional artifact
* `application.yml`: `server.port`, `spring.redis.host/port` (→ `spring.data.redis.*` in Boot 3), JPA settings
* tests: `@SpringBootTest` + `@AutoConfigureMockMvc` + `@MockBean` (deprecated in 3.4, removed in Boot 4 →
  `@MockitoBean`), a plain JUnit 5 unit test

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/azul-11.0.18/Contents/Home \
  mvn -q -DskipTests package
```

Tests also pass on JDK 11 (`mvn -q test`).

On **JDK 21** the same command fails with
`Fatal error compiling: java.lang.NoSuchFieldError: Class com.sun.tools.javac.tree.JCTree$JCImport does not have member field 'com.sun.tools.javac.tree.JCTree qualid'`
— lombok < 1.18.30 cannot run on javac 21. This is exactly the error the pre-emptive lombok handler
(lombok ≥ 1.18.30 for JDK 21) must prevent before the first build of the jdk21 phase.

Expected migration hot spots: lombok floor, javax → jakarta (persistence, validation), Security 7 DSL
(`WebSecurityConfigurerAdapter` → `SecurityFilterChain` bean, `antMatchers` → `requestMatchers`, lambda DSL),
`spring.redis.*` → `spring.data.redis.*`, `@MockBean` → `@MockitoBean`, Boot 4 starter renames.

Note: lombok is not expected in `optional_bumped` — `UpgradeToJava21` already raises it above the configured 1.18.38 and `artifacts.optional_version_policy: floor` (default) never downgrades.
