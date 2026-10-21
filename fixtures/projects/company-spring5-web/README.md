# company-spring5-web — Spring 5 XML webapp with static resources → `{name}-boot` / `-back` / `-front`

Spring Framework 5.3.31 / Java 11, packaging `war` (`com.example:shop`), no Spring Boot: `WEB-INF/web.xml` with a
`ContextLoaderListener` (`applicationContext.xml`, scans `service`) and a `DispatcherServlet` (`dispatcher-servlet.xml`,
scans `web`, `<mvc:default-servlet-handler/>`, `InternalResourceViewResolver` `/` + `.html`); one `@Controller`
(`HomeController`, `/home` returns the view name `index`, `/api/greeting` a `@ResponseBody` text), one `@Service`
(`GreetingService`); static `src/main/webapp/index.html`, `css/site.css`, `js/app.js`; **no JSP**. Nothing the
recipes cannot convert (no `WebMvcConfigurerAdapter`, unlike `spring5-xml`) → no LLM.

Migration (round 9c, the company rule; `sbmig.e2e.yml` sets the company layout keys back and the role parents, every
one stood in by `spring-boot-starter-parent` 4.1.0):

* root `pom.xml`: aggregator (packaging `pom`), parent `artifacts.parent`, `<modules>` = `shop-back`, `shop-boot`,
  `shop-front` (asserted in any order), the two mandatory starters here only;
* `shop-boot` (parent `boot_parent`, jar — no JSP): the generated Application class and the Spring configuration —
  the XML bean configs under `src/main/resources/legacy-config/` (imported with `@ImportResource`), `web.xml` under
  `src/main/resources/asis-webinf/` (not read by Boot, follow-up);
* `shop-back` (parent `comp_parent`): `HomeController`, `GreetingService`;
* `shop-front` (parent `comp_parent`): `src/main/resources/static/index.html`, `static/css/site.css`, `static/js/app.js`;
  no Java; no root `src/`, no `module/`.

Expected: BUILD SUCCESS, exit 0, 0 LLM calls.

As-is build (JDK 11):

```bash
JAVA_HOME=$JAVA11_HOME mvn -q -DskipTests package
```
