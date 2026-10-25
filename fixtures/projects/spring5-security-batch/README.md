# spring5-security-batch fixture (round 10: the deterministic paths for non-Boot Spring projects)

Spring Framework **5.3.31** without Boot (jar, Java 8, Java config only), **Spring Security 5.7.11** and **Spring Batch
4.3.10** — the shape of the company's `APP/BATCH` + admin-console projects. Every migration problem in it has a
deterministic path since round 10, so the run needs **no LLM call**:

| file | as-is | what migrates it (round 10 item) |
|---|---|---|
| `config/WebSecurityConfig` | `extends WebSecurityConfigurerAdapter`, `authorizeRequests().antMatchers(…)…and()`, `csrf().disable()` | plan set `spring_security_7` = `UpgradeSpringSecurity_6_0` (→ 5_8 → 5_7) … `6_5`, `7_0` → `SecurityFilterChain` bean, lambda DSL, `requestMatchers` (D1) |
| `security/UrlAccessConfig`, `security/DbUrlSecurityMetadataSource` | eGovFrame-style DB URL authorization: `AccessDecisionManager` / `AffirmativeBased` / `RoleVoter`, `FilterInvocationSecurityMetadataSource`, `ConfigAttribute`, `new AntPathRequestMatcher(url, method)` | Security 7 moved the access API to `spring-security-access` → `add-known-dependency` (D3, version aligned with the declared Security version); `AntPathRequestMatcher` removed → handler `security-path-pattern-matcher` (D6) |
| `batch/BatchConfig`, `batch/CleanupTasklet` | `JobBuilderFactory` / `StepBuilderFactory`, `org.springframework.batch.item.support.ListItemReader`, `…batch.repeat.RepeatStatus` | `knowledge/recipes/library_recipes.yml` → `SpringBatch5To6Migration` (D2): `JobBuilder` / `StepBuilder` with `JobRepository` + `PlatformTransactionManager`, `…batch.infrastructure.*` |
| `web/MvcConfig` | `setUseSuffixPatternMatch(false).setUseTrailingSlashMatch(true)`, `favorPathExtension(false)`, `HttpHeaders.containsKey / keySet` | project recipe `com.company.migrate.Spring7RemovedApis` (D9) |
| `util/TokenCodec` | `sun.misc.BASE64Encoder` / `BASE64Decoder` | handler `jdk-base64-shim` (D5): generated `sbmig.compat.*` drop-ins, call sites unchanged |

Verified 2026-10-26 by a probe outside sbmig: the planned jdk21 + spring recipe runs (rewrite-maven-plugin 6.46.1,
rewrite-spring 6.37.0, rewrite-migrate-java 3.42.0) plus the two reactive handlers compile on JDK 21 against Spring 7.0,
Security 7.0 / 7.1 and Batch 6.0 (also with the versions spring-boot-starter-parent 4.1.0 manages).

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

success, exit 0, **0 LLM calls**, nothing blocked; flags `jdk-base64-shimmed`, `security-path-matchers-rewritten`;
advisory `jdk-removed-apis` replaced; `module/comp-jar/pom.xml` declares `spring-security-access`; `post_checks` on every
migrated file.
