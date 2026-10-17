# spring5-xml fixture (case 2a: Spring Framework 5 without Boot, XML wiring)

* war, Java 8 (`java.version` 1.8), Spring Framework **5.3.31** (`spring-context`, `spring-webmvc`, `spring-jdbc`)
* `WEB-INF/web.xml`: `ContextLoaderListener` (`applicationContext.xml`) + `DispatcherServlet` (`dispatcher-servlet.xml`),
  `CharacterEncodingFilter`
* `applicationContext.xml`: `<context:component-scan>`, `<jdbc:embedded-database>` (H2 1.4.200 runtime), ObjectMapper bean
* `dispatcher-servlet.xml`: `<mvc:annotation-driven/>`, JSP view resolver
* `WebConfig extends WebMvcConfigurerAdapter` and `AuditInterceptor extends HandlerInterceptorAdapter` — both classes
  are **removed in Spring Framework 6** → `cannot find symbol` errors that OpenRewrite
  (`UpgradeSpringFramework_7_0` chain) or the LLM must fix (→ `implements WebMvcConfigurer` / `HandlerInterceptor`).
  Good KB recipe-candidate targets.
* `jackson-databind` 2.13.5 and `slf4j-api` 1.7.36 (optional artifacts → bump)
* tests: JUnit 4.13.2 + `spring-test` MockMvc standalone + Mockito 4

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

Expected migration hot spots: jakarta servlet, removed adapters, Framework 7, XML bean configs → `@ImportResource`,
JSP / web.xml follow-ups, JUnit 4 → 5, jackson/slf4j bumps.
