# spring5-security-batch fixture (non-Boot Spring 5 + Security 5.7 + Batch 4.3: the largest v2 fix-script run)

Spring Framework **5.3.31** without Boot (jar, Java 8, Java config only), **Spring Security 5.7.11** and **Spring Batch
4.3.10** — the shape of the company's `APP/BATCH` + admin-console projects. v1 (tag `company-v1`, round 10) migrated
all of it deterministically with no LLM call; v2 runs no Spring Framework 7 / Security / Batch recipes and no
handlers, so the structure review and the fix scripts do it:

| file | as-is | what fixes it in v2 (replayed answers) |
|---|---|---|
| `config/WebSecurityConfig` | `extends WebSecurityConfigurerAdapter`, `authorizeRequests().antMatchers(…)…and()`, `csrf().disable()` | v2.1l: the plan's Security chain (`UpgradeSpringSecurity_6_0` → 5_8 → 5_7 … 7_0, planned because `spring-security` is detected) turns it into a `SecurityFilterChain filterChain(HttpSecurity)` bean with the lambda DSL — no fix script any more (replay `llm-005.json` stays unused) |
| `security/UrlAccessConfig`, `security/DbUrlSecurityMetadataSource` | eGovFrame-style DB URL authorization: `AccessDecisionManager` / `AffirmativeBased` / `RoleVoter`, `FilterInvocationSecurityMetadataSource`, `ConfigAttribute`, `new AntPathRequestMatcher(url, method)` | fix scripts: declare `spring-security-access` (one answer for the nine access-API cases); `AntPathRequestMatcher` → `PathPatternRequestMatcher.pathPattern` |
| `batch/BatchConfig`, `batch/CleanupTasklet` | `JobBuilderFactory` / `StepBuilderFactory`, `org.springframework.batch.item.support.ListItemReader`, `…batch.repeat.RepeatStatus` | fix scripts: Batch 6 packages (`…batch.infrastructure.*`, `StepContribution` → `core.step`); `JobBuilder` / `StepBuilder` with the `JobRepository` bean |
| `web/MvcConfig` | `setUseSuffixPatternMatch(false).setUseTrailingSlashMatch(true)`, `favorPathExtension(false)`, `HttpHeaders.containsKey / keySet` | fix scripts: suffix-pattern / trailing-slash / path-extension matching removed; `HttpHeaders` → `containsHeader` / `headerNames` |
| `util/TokenCodec` | `sun.misc.BASE64Encoder` / `BASE64Decoder` | fix script: `java.util.Base64`; javac then reports the `catch (IOException)` it leaves as a new case, fixed in round 3 |

Before any build, the structure review finds the as-is `spring-batch.version` (4.3.10 — there is no
`spring-batch-bom` 4.3.10), `spring-security.version` and `spring.version` overriding the Boot parent's own properties:
**3 critical** issues; its script removes them and the explicit Spring versions so the parent manages Spring 7 /
Security 7 / Batch 6.

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **11 LLM calls** (review + 10 new fix scripts over 3 rounds), nothing blocked; advisory
`jdk-removed-apis` = `guide`; `module/comp-jar/pom.xml` declares `spring-security-access`, `spring-context` /
`spring-batch-core` unversioned, no `<spring-batch.version>` left; `post_checks` on every migrated file.
