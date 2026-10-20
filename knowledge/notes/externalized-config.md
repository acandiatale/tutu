---
title: Externalized configuration (keys the external Tomcat used to supply)
keywords:
  - "Could not resolve placeholder|PlaceholderResolutionException|unresolvable placeholder"
  - "__SET_ME__"
  - "NameNotFoundException|NoInitialContextException|javax\\.naming|jakarta\\.naming|InitialContext|JndiObjectFactoryBean|jndi-lookup"
  - "PropertySource|PropertySourcesPlaceholderConfigurer|PropertyPlaceholderConfigurer|property-placeholder"
  - "Failed to bind properties under|ConfigurationPropertiesBindException|Binding to target"
  - "context-param|catalina\\.(home|base)"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [spring, restructure]
---
# Externalized configuration on an executable boot-jar

## Why keys vanish
On an external Tomcat many values never lived in the source: `catalina.properties` / `setenv.sh` `-D` flags,
JNDI `<Resource>` / `<Environment>` in `server.xml` or `META-INF/context.xml`, web.xml `<context-param>`,
`file:${catalina.base}/conf/*.properties` placeholder locations, jars in `tomcat/lib`. An embedded Tomcat reads
none of them: no JNDI, no web.xml, no `catalina.*` system properties. The code still reads the keys, so startup
(and every `@SpringBootTest`) fails with `Could not resolve placeholder` or a JNDI `NameNotFoundException`.

## What sbmig does
- The analyzer (`sbmig.analyze.properties`) lists every key read by `@Value` / other annotations `${…}`,
  `@ConfigurationProperties`, `Environment.getProperty`, `System.getProperty` / `getenv`, Spring XML `${…}`, web.xml
  `<context-param>`, JNDI names and `@PropertySource`; each key is `defined` (a packaged resource defines it,
  relaxed binding), `defaulted` (code default) or `external` (nowhere).
- Mapping rules (`knowledge/config/externalized.yml`, company additions go there):
  JNDI `java:comp/env/jdbc/*` → `spring.datasource.url|username|password|driver-class-name`; `mail/*` → `spring.mail.*`;
  `jms/*` → manual; other `java:comp/env/<path>` → property `<leaf>`. A `<context-param>` becomes
  `server.servlet.context-parameters.<name>` with its as-is value (known names such as `contextConfigLocation` are
  skipped). `catalina.*` / JDK system properties are never placeholders; Boot defaults (`server.port`) neither.
- Sentinel convention: restructure appends `key=__SET_ME__` lines for every external key between
  `# --- sbmig externalized-config` and `# --- end sbmig externalized-config` in the boot-jar (or each MSA service)
  `src/main/resources/application.properties`. The Handover's "Properties to set before running" table tells the
  owners which values to set and how (environment variable, `-D`, external `application.properties`).
- `__SET_ME__` is a deliberate marker, not a value: the owners replace it before deployment.

## fix-loop: externalized-config
When a test fails with `Could not resolve placeholder '<key>'` (or `Failed to bind properties under '<prefix>'`):
- Supply the key for the TEST only: add it to the failing test's `@SpringBootTest(properties = "<key>=<test value>")`
  (or `@TestPropertySource(properties = …)`), or to `src/test/resources/application.properties` when several tests
  need it. Use an obviously fake test value (`test`, `0`, `jdbc:h2:mem:test`), never a real host, URL or password.
- Never invent production values and never edit or remove the sbmig placeholder block in `src/main/resources`;
  do not replace `__SET_ME__` with a guessed value.
- Do not add `ignoreUnresolvablePlaceholders` / `ignore-unresolvable="true"` to hide the error, and do not delete the
  `@Value` field or the XML bean that reads the key.
- A JNDI lookup failing in a test (`NameNotFoundException`, `NoInitialContextException`): give the test a
  `spring.datasource.url=jdbc:h2:mem:test` (when H2 is on the test classpath) or mock the bean; do not add a JNDI
  provider dependency.
- If the value is a number/boolean, the test value must parse (`0`, `false`), otherwise the next error is a
  conversion failure.
