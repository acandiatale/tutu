# legacy-external-config fixture (case 2a: Spring 5 XML web app that ran on an external Tomcat)

* war, Java 8 (`java.version` 1.8), Spring Framework **5.3.31** (`spring-context`, `spring-webmvc`, `spring-jdbc`),
  deployed to an external Tomcat whose configuration supplied most settings — nothing of it is in the repo:
  * `applicationContext.xml`: `<context:property-placeholder location="classpath:catalog.properties,
    file:${catalina.base}/conf/catalog-local.properties" ignore-resource-not-found="true"/>` (per-server overrides
    in `$CATALINA_BASE/conf`) and `<jee:jndi-lookup id="dataSource" jndi-name="java:comp/env/jdbc/catalogDS"/>`
    (the `<Resource>` lives in Tomcat's `context.xml`); `CatalogDao` only stores the `DataSource`
  * `CatalogService`: `@Value("${catalog.title}")` (packaged in `catalog.properties` → **defined**),
    `@Value("${mail.host}")` (only in the server's `catalog-local.properties` → **external**),
    `@Value("${catalog.page-size:10}")` (packaged → defined), `System.getProperty("catalog.home")` with a null check
    (`-Dcatalog.home` in `setenv.sh` → **external**)
  * `web.xml`: `contextConfigLocation` (rule-skipped: the XML is imported via `@ImportResource`) and
    `<context-param>catalog.region=kr</context-param>` (→ `server.servlet.context-parameters.catalog.region=kr`)
* `jackson-databind` 2.13.5 and `slf4j-api` 1.7.36 (optional artifacts → bump); no `WebMvcConfigurerAdapter`, so the
  run needs no LLM
* tests: one plain JUnit 4 test (no Spring context), so the test phase runs without any container value

## Build as-is

```bash
JAVA_HOME=/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home \
  mvn -q -DskipTests package
```

Expected migration result: builds (exit 0, 0 LLM calls); `module/boot-jar/src/main/resources/application.properties`
gets the `# --- sbmig externalized-config` block with `mail.host`, `catalog.home`, `spring.datasource.*` as
`__SET_ME__` and `server.servlet.context-parameters.catalog.region=kr`; the Handover lists them under
"Properties to set before running".
