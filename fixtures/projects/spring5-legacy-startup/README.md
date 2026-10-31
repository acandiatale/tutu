# spring5-legacy-startup — the legacy Spring context at conversion time (v2.1x)

Spring Framework 5.3.31 / Java 8, packaging `war` (`com.example:shop`), no Spring Boot. Every pattern below builds on
JDK 21 after the migration but would break (or silently change) the **startup** of the converted Boot application —
neither the build gate nor the LLM sees startup, so the restructure handles them itself (docs/INTERFACES.md "v2.1x",
prompt review 2026-10-31 §4 / report A §2b):

* `web.xml` (Servlet 3.1): root context `contextConfigLocation` = `classpath*:spring/context-*.xml` (matches
  `context-root.xml` and `context-common.xml`), DispatcherServlet `action` on **`*.do`** with
  `/WEB-INF/config/action-servlet.xml`, `CharacterEncodingFilter` **EUC-KR** + `forceEncoding`, a **non-security
  `DelegatingFilterProxy`** (`xssFilter` → bean `xssEscapeFilter`, `*.do`), `spring.profiles.active=dev` context-param,
  `<jsp-config>` page-encoding EUC-KR;
* `spring/context-root.xml` is **EUC-KR** (Korean comments and a Korean property value), reads
  `/WEB-INF/config/app.properties` and `classpath:config-${env}.properties` (only `config-dev.properties` is packaged),
  `<import resource="include/context-datasource.xml"/>` (a `<jee:jndi-lookup>` DataSource + JdbcTemplate), defines
  `messageSource` — the servlet XML defines it again (duplicate id across the contexts);
* `action-servlet.xml`: `<mvc:default-servlet-handler/>`, `<mvc:resources mapping="/resources/**" location="/resources/">`,
  a **`CommonsMultipartResolver`** (10 MB / 1 MB), an `InternalResourceViewResolver` `/WEB-INF/views/` + `.jsp`;
* `src/test/resources/spring/test-context.xml` (test only), `src/main/resources/log4j.properties` (log4j 1),
  `javax.servlet-api` **provided**, two JSPs under `WEB-INF/views/`, the package `com.example.shop.notice` in **MS949**
  (no `project.build.sourceEncoding` — the as-is team built on Korean Windows).

> `NoticeFormatter.java` / `NoticeMessages.java` are intentionally **MS949**, `spring/context-root.xml` **EUC-KR**. Do
> not open-and-save them with a UTF-8 editor.

Migration (company layout; `sbmig.e2e.yml` sets the company layout keys back and the role parents, every one stood in by
`spring-boot-starter-parent` 4.1.0; `layout.legacy_context.*` at its defaults):

* `shop-boot` (executable WAR, `spring-boot-starter-webmvc` — web evidence: web.xml, spring-webmvc, servlet-api, JSP,
  `@Controller`): `@ImportResource` lists only `legacy-config/context-common.xml`, `context-root.xml`,
  `action-servlet.xml` (D2); `include/context-datasource.xml` is copied next to them (reached by `<import>`), the test XML
  is not copied; `context-root.xml` is a **byte copy** (still EUC-KR, D3); in the copies: the JNDI bean gets
  `<!-- MIGRATION-TODO (sbmig): … -->` (D5), `/WEB-INF/config/app.properties` → `classpath:asis-webinf/config/app.properties`
  (D6), `classpath:config-${env}.properties` → `classpath*:config-*.properties` (D7, one packaged file), the
  CommonsMultipartResolver bean removed (D9), the view prefix → `/WEB-INF/jsp/views/` and `classpath:/static/resources/`
  added to `<mvc:resources>` (D10); `application.properties` gets `spring.main.allow-bean-definition-overriding` /
  `allow-circular-references` (D1), `spring.servlet.encoding.charset=EUC-KR` / `force=true` (D4),
  `server.servlet.register-default-servlet=true` (D8), `spring.servlet.multipart.*` 10MB / 1MB (D9),
  `spring.mvc.view.*`; `WebXmlRegistrations` holds `new DelegatingFilterProxyRegistrationBean("xssEscapeFilter")` on
  `*.do` (D11);
* `shop-back`: every source, `jakarta.servlet-api` **provided** again (E-M6; the recipe made it compile),
  `project.build.sourceEncoding` **MS949** (D-M7); the as-is XML stays untouched in `src/main/resources/spring/`;
* `shop-front`: the JSPs under `META-INF/resources/WEB-INF/jsp/views/`, `static/resources/css/notice.css`.

Expected (v2, `expected.json` → `expect`): BUILD SUCCESS, exit 0, **1 LLM call** (the structure review: `ok`), no fix
script; the journal events `legacy_context` / `legacy_xml_imports` carry the facts, `followups_contains` asserts the
Handover sentences (D1 + duplicate ids, JNDI, `${env}`, CommonsMultipartResolver, `<mvc:resources>`, default servlet,
`*.do`, `<jsp-config>`, `spring.profiles.active`, log4j 1, WAR context root, one merged context), `properties` asserts
that the WEB-INF / profile keys are definitions (no `__SET_ME__` overrides the packaged values).

As-is build (JDK 8; the platform encoding of the Korean Windows build):

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
JAVA_TOOL_OPTIONS=-Dfile.encoding=MS949 \
  mvn -q -DskipTests package
```
