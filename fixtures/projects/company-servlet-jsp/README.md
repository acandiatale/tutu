# company-servlet-jsp — servlet + JSP webapp → WAR `{name}-boot` for an external Tomcat + `-back` / `-front`

Plain servlet webapp on Java 8, packaging `war` (`com.example:shop`), no Spring: `WEB-INF/web.xml` maps
`HelloServlet` (`extends HttpServlet`) to `/hello`; it forwards to `/WEB-INF/jsp/hello.jsp`, which uses the JSTL core
taglib (`<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>`, `<c:out value="${greeting}"/>`) and links
`css/site.css`. Dependencies: `javax.servlet:javax.servlet-api` 3.1.0 and `javax.servlet.jsp:javax.servlet.jsp-api`
2.3.3 (both `provided`), `javax.servlet:jstl` 1.2 (compile). `GreetingService` is a plain class.

Migration (round 9c, the company rule; `sbmig.e2e.yml` sets the company layout keys back and the role parents, every
one stood in by `spring-boot-starter-parent` 4.1.0). JSPs exist → the boot module is a **WAR** for an external Tomcat
(`layout.jsp_boot_packaging: war`):

* root `pom.xml`: aggregator, parent `artifacts.parent`, `<modules>` = `shop-back`, `shop-boot`, `shop-front` (any
  order), the two mandatory starters here only;
* `shop-boot` (parent `boot_parent`): packaging `war`, `spring-boot-starter-tomcat` (`provided`), the generated
  Application `extends SpringBootServletInitializer`; `mvn package` produces `shop-boot/target/*.war`;
* `shop-back` (parent `comp_parent`): `HelloServlet` (on `jakarta.servlet`), `GreetingService`;
* `shop-front` (parent `comp_parent`): the JSP under `src/main/resources/META-INF/resources/WEB-INF/jsp/hello.jsp`
  (Servlet 3 jar resources, so the `/WEB-INF/jsp/hello.jsp` forward still resolves), `static/css/site.css`; no Java.

Expected: BUILD SUCCESS, exit 0, 0 LLM calls.

As-is build (JDK 8):

```bash
JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```
