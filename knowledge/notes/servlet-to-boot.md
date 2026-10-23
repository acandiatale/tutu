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

## Registration (web.xml is IGNORED by embedded Tomcat and not packaged by the Boot WAR)
- sbmig generates the registrations (2026-10-23, `layout.web_xml_registrations: generate`): the restructure parses
  the first web.xml (DTD 2.2 / 2.3, XSD 2.4 … 6.0) and writes `<pkg>.WebXmlRegistrations` into the boot module —
  `ServletRegistrationBean` (url patterns, `setName`, `setLoadOnStartup`, init params, async, multipart; a servlet
  without a mapping gets `alwaysMapUrl=false`), `FilterRegistrationBean` (url patterns / servlet names, dispatcher
  types, `setOrder(n)` = web.xml `<filter-mapping>` order), `ServletListenerRegistrationBean`, one
  `ErrorPageRegistrar` for `<error-page>`; `<session-timeout>` → `server.servlet.session.timeout=<N>m` in the boot
  `application.properties`. Every bean uses the FQCN (`new com.x.MyServlet()`), so a compile error in that file
  means the class is missing or still on `javax.servlet`: fix the class (or its library), not the generated file.
- Not generated (skip table `knowledge/config/web_xml_rules.yml`): `DispatcherServlet`, `ContextLoaderListener`,
  `CharacterEncodingFilter`, `DelegatingFilterProxy`, `HiddenHttpMethodFilter`, Jasper / Default servlet, … — Boot
  provides them (see the property hints there). Classes annotated `@WebServlet` / `@WebFilter` / `@WebListener` are
  left to `@ServletComponentScan` on the Application, so both coexist without double registration.
- Follow-ups only: `jsp-file` servlets (map in a controller or link the JSP), `<welcome-file-list>` (Boot serves a
  static `index.html` only), `<mime-mapping>` (`WebServerFactoryCustomizer` + `MimeMappings`), `<security-constraint>`
  / `<login-config>` (Spring Security), `<distributable/>`; `<context-param>` goes through the properties inventory
  (`server.servlet.context-parameters.*`). With the boot module as a WAR on an external Tomcat,
  `server.servlet.session.timeout` is not applied (embedded only) — the container's `conf/web.xml` decides.
- Manual form, for anything the generator could not do:
  `@Bean ServletRegistrationBean<MyServlet> myServlet() { var b = new ServletRegistrationBean<>(new MyServlet(), "/my/*"); b.setLoadOnStartup(1); return b; }`
  (`FilterRegistrationBean` / `ServletListenerRegistrationBean` likewise; all in `org.springframework.boot.web.servlet`,
  `ErrorPage` / `ErrorPageRegistrar` in `org.springframework.boot.web.error` since Boot 4).
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
