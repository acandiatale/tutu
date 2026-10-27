# servlet-tomcat fixture (case 3: raw servlet/JSP on an external Tomcat)

* `packaging` war, `maven-war-plugin` 3.2.3, Java 8 (`maven.compiler.source/target` 1.8)
* `javax.servlet:javax.servlet-api` 3.1.0 `provided`
* `src/main/webapp/WEB-INF/web.xml` registers `AppContextListener`, `LoggingFilter` (`/*`) and `HelloServlet` (`/hello`)
* `src/main/webapp/index.jsp`
* plain `service` / `dao` / `model` packages, `commons-io` 2.6 (optional artifact → bump)
* JUnit 4 tests

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

Expected migration hot spots: `javax.servlet` → `jakarta.servlet` (`JavaxMigrationToJakarta`),
servlet-api dependency swap, war → Boot jar with `spring-boot-starter-webmvc` + `@ServletComponentScan`
(servlets/filters are registered in web.xml, not annotated → reported as manual follow-up), JSP not supported
in an executable jar (follow-up), JUnit 4 → 5, commons-io bump.

## Expected sbmig outcome (v2, asserted by `scripts/e2e.py`, see `expected.json` → `expect`)

Success, exit 0, **1 LLM call** (the structure review: one warning on the servlet-api scope), no fix script;
commons-io → 2.19.0. The JUnit 4 tests are not compiled (`-Dmaven.test.skip=true`).
