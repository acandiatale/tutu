---
title: Raw servlet / JSP (Tomcat WAR) apps inside Spring Boot 4
keywords:
  - "HttpServlet|@WebServlet|@WebFilter|@WebListener|ServletComponentScan|ServletRegistrationBean|FilterRegistrationBean|ServletListenerRegistrationBean"
  - "javax\\.servlet|jakarta\\.servlet"
  - "web\\.xml|WEB-INF|webapp"
  - "jsp|JSP|jasper|jstl|taglib|InternalResourceViewResolver"
  - "SpringBootServletInitializer|WebApplicationInitializer|ServletContainerInitializer"
  - "javax\\.servlet-api|servlet-api|tomcat-embed|spring-boot-starter-tomcat"
  - "<packaging>war</packaging>|maven-war-plugin|failOnMissingWebXml"
cases: [servlet_tomcat, spring_legacy]
phases: [spring, restructure]
---
# Servlet / JSP application → Spring Boot 4.1 (embedded Tomcat 11, Jakarta Servlet 6.1)

## Compile-level changes
- All `javax.servlet.*` imports → `jakarta.servlet.*` (`HttpServlet`, `HttpServletRequest/Response`, `Filter`, `FilterChain`,
  `ServletContextListener`, `HttpSessionListener`, `@WebServlet`, `@WebFilter`, `@WebListener`, `@WebInitParam`, `@MultipartConfig`).
- Remove `javax.servlet:javax.servlet-api` / `javax.servlet:servlet-api` (provided). Embedded Tomcat via
  `spring-boot-starter-webmvc` (or `spring-boot-starter-tomcat` for a non-MVC servlet app) provides `jakarta.servlet-api`.
- `javax.servlet.jsp.*` → `jakarta.servlet.jsp.*`; JSTL taglib URI `http://java.sun.com/jsp/jstl/core` → `jakarta.tags.core`
  (old URI still works with the jakarta JSTL impl's compatibility TLDs — leave JSP files unless they break the build).
- Servlet 6 removals: `HttpServletRequest.isRequestedSessionIdFromUrl()` → `isRequestedSessionIdFromURL()`,
  `HttpServletResponse.encodeUrl/encodeRedirectUrl` → `encodeURL/encodeRedirectURL`, `setStatus(int, String)` → `setStatus(int)`
  (+ `sendError(int,String)` if the message matters), `HttpSessionContext`/`getSessionContext()` removed,
  `HttpUtils` removed, `SingleThreadModel` removed (delete `implements SingleThreadModel` + MIGRATION-TODO),
  `ServletContext.getServlet/getServlets/getServletNames/log(Exception,String)` removed.
- `jakarta.servlet.http.Cookie` setters `setComment`/`setVersion` deprecated (no-ops) — warnings only.

## Registration (web.xml is IGNORED by embedded Tomcat)
- Option A (minimal): keep `@WebServlet/@WebFilter/@WebListener` annotations and put `@ServletComponentScan` on the
  `@SpringBootApplication` class (restructure adds it).
- Option B: `@Bean ServletRegistrationBean<MyServlet> myServlet() { var b = new ServletRegistrationBean<>(new MyServlet(), "/my/*"); b.setLoadOnStartup(1); return b; }`
  `FilterRegistrationBean` / `ServletListenerRegistrationBean` likewise (use for servlets declared only in web.xml).
- web.xml `<servlet-mapping>`, `<filter-mapping>` order, `<context-param>`, `<error-page>`, `<security-constraint>`,
  `<session-config>` are NOT applied → runtime concern: `// MIGRATION-TODO: web.xml mapping <x> must be re-declared`
  (or `server.servlet.session.timeout`, `server.error.*`, `server.servlet.context-parameters.*` properties).
- `WebApplicationInitializer` implementations are not invoked by embedded containers → convert to beans + MIGRATION-TODO.

## JSP
- Needs `org.apache.tomcat.embed:tomcat-embed-jasper` (+ JSTL: `jakarta.servlet.jsp.jstl:jakarta.servlet.jsp.jstl-api`
  and `org.glassfish.web:jakarta.servlet.jsp.jstl`) and only works with WAR packaging or with JSPs moved to
  `src/main/resources/META-INF/resources/WEB-INF/...` in an executable jar. Files under `src/main/webapp` are not
  packaged into a jar → report as MIGRATION-TODO, do not move JSPs to fix compilation (they do not compile at build time).
- `spring.mvc.view.prefix=/WEB-INF/jsp/` + `spring.mvc.view.suffix=.jsp` replace an `InternalResourceViewResolver` XML bean.

## Packaging
- `war` → executable jar (`spring-boot-maven-plugin` repackage) in the company layout; remove `maven-war-plugin` config
  (`failOnMissingWebXml`) from jar modules. If WAR must stay: `SpringBootServletInitializer` + `spring-boot-starter-tomcat-runtime`
  as provided (Boot 4 name) and `server.forward-headers-strategy` is ignored for WAR.
- `context.xml` / JNDI `DataSource` lookups (`java:comp/env/jdbc/x`) do not exist in embedded Tomcat →
  `spring.datasource.*` + MIGRATION-TODO.
