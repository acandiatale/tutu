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

Expected migration hot spots: `javax.servlet` → `jakarta.servlet` (`ServletToJakartaAndBoot`),
servlet-api dependency swap, war → Boot jar with `spring-boot-starter-webmvc` + `@ServletComponentScan`
(servlets/filters are registered in web.xml, not annotated → reported as manual follow-up), JSP not supported
in an executable jar (follow-up), JUnit 4 → 5, commons-io bump.
