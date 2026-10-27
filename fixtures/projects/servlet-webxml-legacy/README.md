# servlet-webxml-legacy — pre-Servlet-3 webapp: web.xml (2.3 DTD) → generated `WebXmlRegistrations`

Plain servlet webapp on Java 8, packaging `war` (`com.example:legacy`), no Spring, `javax.servlet:servlet-api` 2.5
(`provided`). Every component is declared **only** in `WEB-INF/web.xml` (the Servlet 2.3 DTD, no namespace, no
annotation anywhere):

* filters `encoding` (`EncodingFilter`, init-param `encoding=UTF-8`, `/*`) and `audit` (`AuditFilter`, `/admin/*`,
  dispatchers `REQUEST` + `FORWARD` — a 2.4 element old containers accepted in 2.3 descriptors), in that chain order;
* listener `AppStartupListener` (`ServletContextListener`);
* servlets `front` (`FrontServlet`, init-param `defaultView`, load-on-startup 1, `/front/*` and `*.do`), `report`
  (`ReportServlet`, no servlet-mapping) and `help` (`<jsp-file>/WEB-INF/jsp/help.jsp</jsp-file>`, `/help`);
* `session-config` 30, `mime-mapping` `hwp` → `application/x-hwp`, `welcome-file-list` `index.jsp`, error pages
  `404` and `java.lang.Exception` → `/WEB-INF/jsp/error.jsp`, one `context-param` (`legacy.region=kr`).

JSPs: `index.jsp`, `WEB-INF/jsp/{help,error,list}.jsp` (`list.jsp` imports `javax.servlet.http.Cookie` and names
`javax.servlet.http.HttpSession` in a scriptlet — v2 leaves them: JSPs are not compiled by the build, Tomcat 10+
needs them on jakarta, an open point); static `css/app.css`.

Migration (company layout, `sbmig.e2e.yml` as in `company-servlet-jsp`): `legacy-boot` (WAR for an external Tomcat),
`legacy-back` (all Java on `jakarta.servlet`), `legacy-front` (JSPs under `META-INF/resources/WEB-INF/jsp`, static
files). `layout.web_xml_registrations: generate` writes `legacy-boot/src/main/java/com/example/legacy/WebXmlRegistrations.java`:
2 servlet beans (`front` with both patterns and `setLoadOnStartup(1)`, `report` with `alwaysMapUrl=false`), 2 filter
beans (orders 1 / 2, `audit` with `setDispatcherTypes(REQUEST, FORWARD)`), 1 listener bean, one `ErrorPageRegistrar`
(404 + `java.lang.Exception`); `server.servlet.session.timeout=30m` in `legacy-boot/src/main/resources/application.properties`.
Follow-ups: the jsp-file servlet `help`, the welcome file, the MIME mapping, the session timeout on an external
Tomcat, and the Handover line `web.xml (2.3) → WebXmlRegistrations: 2 servlet(s), 2 filter(s), 1 listener(s), 2 error page(s)`.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS (the generated class compiles against Spring Boot 4.1), exit 0,
**1 LLM call** (the structure review, verdict `ok`), no fix script, flag
`web-xml-registered`, journal `web_xml_registrations` (servlets 2, filters 2, listeners 1, error pages 2),
`legacy-boot/target/*.war`. Catalog id W1 (`docs/ERROR-HANDLING.md`).

As-is build (JDK 8):

```bash
JAVA_HOME=$JAVA8_HOME mvn -q -DskipTests package
```
