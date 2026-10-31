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
# Externalized configuration on the embedded Tomcat (executable jar / WAR)

## Why keys vanish
On the as-is external Tomcat many values never lived in the source: `catalina.properties` / `setenv.sh` `-D` flags,
JNDI `<Resource>` / `<Environment>` in `server.xml` or `META-INF/context.xml`, web.xml `<context-param>`,
`file:${catalina.base}/conf/*.properties` placeholder locations, jars in `tomcat/lib`. An embedded Tomcat reads
none of them: no JNDI, no web.xml, no `catalina.*` system properties. The code still reads the keys, so startup
fails with `Could not resolve placeholder` or a JNDI `NameNotFoundException` — at runtime, not in the build.

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
A missing key is a startup error, never a build error: sbmig's build runs `-Dmaven.test.skip=true`, so
`Could not resolve placeholder` / JNDI `NameNotFoundException` reach the loop only when a project runs its tests.
- Never replace `__SET_ME__` with a guessed value, never edit or remove the sbmig placeholder block in
  `src/main/resources`, never add `ignoreUnresolvablePlaceholders` / `ignore-unresolvable="true"`, never delete the
  `@Value` field or the XML bean that reads the key.
- When tests do run: give the failing test the key (`@SpringBootTest(properties = "<key>=<value>")`, or
  `src/test/resources/application.properties`) with an obviously fake value that parses (`test`, `0`, `false`,
  `jdbc:h2:mem:test`); a JNDI lookup in a test → `spring.datasource.url=jdbc:h2:mem:test` or a mock, never a JNDI provider.
